"""
scraper.py — NHS Jobs XML API Scraper + Sponsorship Filter
===========================================================
Uses the public NHS Jobs XML API endpoint:
  GET https://www.jobs.nhs.uk/api/v1/search_xml

No API key required. Returns structured XML — far more reliable
than HTML scraping (no layout changes, no parser drift).

Flow:
  1. Query API for each target keyword (paginated)
  2. Parse XML → Job dataclass
  3. Fetch full job description page for sponsorship keyword check
  4. Flag and return jobs that mention visa sponsorship / CoS
"""

import time
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from datetime import datetime

import requests
from bs4 import BeautifulSoup

import config
from deduplication import DynamoSeenJobs

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

NHS_API_URL  = "https://www.jobs.nhs.uk/api/v1/search_xml"
NHS_BASE_URL = "https://www.jobs.nhs.uk"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/xml, text/xml, */*",
    "Accept-Language": "en-GB,en;q=0.9",
}

# Target job title keywords — sent as `keyword` param to the API
JOB_KEYWORDS = [
    "data analyst",
    "information analyst",
    "bi analyst",
    "business intelligence analyst",
    "digital analyst",
    "data officer",
    "analytics officer",
    "junior analyst",
    "assistant analyst",
]

# Sponsorship detection phrases — checked against full job description text
SPONSORSHIP_KEYWORDS = [
    "visa sponsorship",
    "certificate of sponsorship",
    "skilled worker visa",
    "cos available",
    "cos will be",
    "overseas applicants",
    "sponsorship available",
    "tier 2 sponsorship",
    "skilled worker route",
    "right to work sponsorship",
    "work visa",
    "sponsorship provided",
    "we can sponsor",
    "sponsorship considered",
    "eligible for sponsorship",
]


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class Job:
    job_id:                    str
    title:                     str
    employer:                  str
    location:                  str
    salary:                    str
    closing_date:              str
    url:                       str
    summary:                   str
    full_description:          str = ""
    sponsorship_keywords_found: list = field(default_factory=list)
    scraped_at:                str = ""

    def __post_init__(self):
        if not self.scraped_at:
            self.scraped_at = datetime.utcnow().isoformat()


# ── XML API Client ─────────────────────────────────────────────────────────────

class NHSJobsAPIClient:
    """
    Thin wrapper around the NHS Jobs public XML API.

    Endpoint: GET https://www.jobs.nhs.uk/api/v1/search_xml
    Params:
        keyword  (str)  — search term, e.g. "data analyst"
        language (str)  — always "en"
        page     (int)  — 1-based page number
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def search(self, keyword: str, page: int = 1) -> list:
        """
        Fetch one page of results from the XML API.
        Returns a list of Job objects (without full_description yet).
        """
        params = {
            "keyword":  keyword,
            "language": "en",
            "page":     str(page),
        }
        try:
            resp = self.session.get(NHS_API_URL, params=params, timeout=15)
            resp.raise_for_status()
            return self._parse_xml(resp.text)

        except requests.RequestException as e:
            log.error(f"API request failed for '{keyword}' page {page}: {e}")
            return []
        except ET.ParseError as e:
            log.error(f"XML parse error for '{keyword}' page {page}: {e}")
            return []

    def _parse_xml(self, xml_text: str) -> list:
        """
        Parse the NHS Jobs XML API response.

        Expected structure (confirmed from live endpoint):
          <jobs>
            <vacancy>
              <id>...</id>
              <title>...</title>
              <employer>...</employer>
              <location>...</location>
              <salary>...</salary>
              <closingDate>...</closingDate>
              <url>...</url>
              <description>...</description>
            </vacancy>
            ...
          </jobs>

        Tag name aliases are tried in order so the parser stays
        resilient to minor API version differences.
        """
        root = ET.fromstring(xml_text)
        jobs = []

        # Root element may be <jobs>, <vacancies>, or <results>
        # Vacancy elements may be direct children or one level deep
        vacancies = (
            root.findall("vacancyDetails")
            or root.findall("vacancy")
            or root.findall("job")
            or root.findall(".//vacancyDetails")
            or root.findall(".//vacancy")
            or root.findall(".//job")
        )

        for v in vacancies:
            def get(*tags) -> str:
                """Return text of the first matching tag, or empty string."""
                for tag in tags:
                    el = v.find(tag)
                    if el is not None and el.text:
                        return el.text.strip()
                return ""

            raw_url = get("url", "link", "jobUrl", "job_url")
            url = (
                raw_url if raw_url.startswith("http")
                else NHS_BASE_URL + raw_url
            )

            job_id = get("id", "jobId", "job_id", "vacancyId")
            if not job_id:
                m = re.search(r"/jobadvert/([^/?#]+)", url)
                job_id = m.group(1) if m else url.split("/")[-1]

            # Handle nested <locations><location> structure
            location = get("location", "locationName", "city")
            if not location:
                loc_el = v.find("locations/location")
                if loc_el is not None and loc_el.text:
                    location = loc_el.text.strip()

            jobs.append(Job(
                job_id       = job_id,
                title        = get("title", "jobTitle", "job_title"),
                employer     = get("employer", "organisation", "trust"),
                location     = location,
                salary       = get("salary", "salaryRange", "pay"),
                closing_date = get("closingDate", "closing_date", "closeDate"),
                url          = url,
                summary      = get("description", "summary", "snippet"),
            ))

        if not jobs:
            log.warning(f'  Raw response (first 500 chars): {xml_text[:500]}')
        log.info(f"  XML API returned {len(jobs)} vacancies")
        return jobs

    def fetch_description(self, url: str) -> str:
        """
        Fetch full job description from the individual NHS Jobs page.
        Targets the known description container — avoids nav/footer noise.
        """
        try:
            time.sleep(config.REQUEST_DELAY_SECONDS)
            resp = self.session.get(url, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            # Try selectors in priority order
            for sel in [
                "div#job-overview",
                "section.job-overview",
                "div.job-description",
                "div[data-test='job-overview']",
                "main article",
                "main",
            ]:
                el = soup.select_one(sel)
                if el:
                    return el.get_text(separator=" ", strip=True)

            return soup.get_text(separator=" ", strip=True)[:6000]

        except requests.RequestException as e:
            log.warning(f"Could not fetch job page {url}: {e}")
            return ""


# ── Sponsorship filter ─────────────────────────────────────────────────────────

class SponsorshipFilter:

    @staticmethod
    def matched_keywords(text: str) -> list:
        text_lower = text.lower()
        return [kw for kw in SPONSORSHIP_KEYWORDS if kw in text_lower]

    @staticmethod
    def is_sponsored(job: Job) -> bool:
        combined = f"{job.summary} {job.full_description}"
        matches  = SponsorshipFilter.matched_keywords(combined)
        job.sponsorship_keywords_found = matches
        return bool(matches)


# ── Main pipeline ──────────────────────────────────────────────────────────────

def run_pipeline() -> list:
    """
    Full pipeline:
      1. Query NHS Jobs XML API for each keyword (paginated)
      2. Deduplicate within run using seen_urls set (fast, in-memory)
      3. Check DynamoDB for cross-run deduplication (stateless Lambda-safe)
      4. Fetch full description for each genuinely new job
      5. Apply sponsorship keyword filter
      6. Return list of new sponsored job dicts (JSON-serialisable)
    """
    client    = NHSJobsAPIClient()
    tracker   = DynamoSeenJobs()      # DynamoDB, falls back to local JSON
    sponsored = []
    seen_urls = set()                 # within-run dedup (avoids re-checking
                                      # the same URL returned by multiple keywords)

    log.info("=" * 60)
    log.info("NHS Sponsorship Tracker — Pipeline Start")
    log.info(f"Source   : NHS Jobs XML API ({NHS_API_URL})")
    log.info(f"Keywords : {len(JOB_KEYWORDS)}  |  Pages/keyword: {config.MAX_PAGES_PER_KEYWORD}")
    log.info("=" * 60)

    for keyword in JOB_KEYWORDS:
        log.info(f"\nKeyword: '{keyword}'")

        for page in range(1, config.MAX_PAGES_PER_KEYWORD + 1):
            jobs = client.search(keyword, page=page)
            if not jobs:
                log.info(f"  No results on page {page} — next keyword")
                break

            for job in jobs:
                # 1. Skip if already seen in this run
                if job.url in seen_urls:
                    continue
                seen_urls.add(job.url)

                # 2. Skip if already recorded in DynamoDB (previously notified)
                if not tracker.is_new(job.job_id):
                    log.debug(f"  Already seen: {job.title}")
                    continue

                # 3. Fetch full JD for proper sponsorship detection
                log.info(f"  Checking: {job.title} @ {job.employer}")
                job.full_description = client.fetch_description(job.url)

                # 4. Sponsorship filter
                if SponsorshipFilter.is_sponsored(job):
                    kws = ", ".join(job.sponsorship_keywords_found)
                    log.info(f"  MATCH [{kws}]")
                    sponsored.append(asdict(job))

                # 5. Mark seen in DynamoDB regardless of sponsorship result
                #    (prevents re-fetching non-sponsored jobs on every run)
                tracker.mark_seen(
                    job.job_id,
                    title    = job.title,
                    employer = job.employer,
                    url      = job.url,
                )

            time.sleep(1)   # polite inter-page delay

    log.info("\n" + "=" * 60)
    log.info(f"Done. {len(sponsored)} new sponsored job(s) found.")
    log.info("=" * 60)

    return sponsored


# ── Local test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )
    results = run_pipeline()

    if results:
        print(f"\n{'='*60}")
        print(f"NEW SPONSORED JOBS: {len(results)}")
        print(f"{'='*60}")
        for job in results:
            print(f"\n  Title    : {job['title']}")
            print(f"  Employer : {job['employer']}")
            print(f"  Location : {job['location']}")
            print(f"  Salary   : {job['salary']}")
            print(f"  Closing  : {job['closing_date']}")
            print(f"  Keywords : {', '.join(job['sponsorship_keywords_found'])}")
            print(f"  URL      : {job['url']}")
    else:
        print("\nNo new sponsored jobs found.")
