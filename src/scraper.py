"""
scraper.py — NHS Jobs XML API Scraper + Sponsorship Filter
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

JOB_KEYWORDS = [
    "data analyst", "information analyst", "bi analyst",
    "business intelligence analyst", "digital analyst",
    "data officer", "analytics officer", "junior analyst",
    "assistant analyst", "graduate analyst", "graduate data",
    "junior data", "data technician", "data engineer",
    "data intern", "analytics intern", "data placement",
    "performance analyst", "reporting analyst",
    "clinical data", "informatics", "data scientist",
    "cloud engineer", "cloud architect", "devops",
    "software developer", "software engineer",
    "it support", "ict support", "desktop support",
    "systems analyst", "business analyst",
]

# Title must contain one of these EXACT relevant terms
RELEVANT_TITLE_KEYWORDS = [
    "data analyst", "data engineer", "data scientist", "data technician",
    "data officer", "data manager", "data architect",
    "information analyst", "information officer", "information manager",
    "bi analyst", "business intelligence", "analytics",
    "performance analyst", "reporting analyst", "systems analyst",
    "business analyst", "digital analyst", "clinical analyst",
    "informatics", "data intern", "data placement", "graduate data",
    "junior data", "junior analyst", "assistant analyst",
    "cloud engineer", "cloud architect", "cloud developer",
    "devops", "software engineer", "software developer",
    "it engineer", "it analyst", "it support analyst",
    "ict analyst", "ict engineer", "ict support",
    "desktop engineer", "desktop analyst",
    "network engineer", "cyber", "infrastructure engineer",
    "database", "sql developer", "python developer",
    "digital transformation", "digital project",
    "data quality", "data governance", "data warehouse",
]

SPONSORSHIP_POSITIVE = [
    "visa sponsorship available",
    "sponsorship available",
    "certificate of sponsorship will be",
    "certificate of sponsorship is available",
    "cos will be provided",
    "cos is available",
    "we can offer sponsorship",
    "we are able to sponsor",
    "sponsorship can be provided",
    "sponsorship is available",
    "eligible for sponsorship",
    "sponsorship provided",
    "we can sponsor",
    "sponsorship considered",
    "overseas applicants are welcome",
    "welcome applications from overseas",
    "tier 2 sponsorship",
    "we welcome applications from candidates who require skilled worker",
    "applications from job seekers who require current skilled worker sponsorship",
    "sponsorship for this role",
    "certificates of sponsorship",
]

SPONSORSHIP_NEGATIVE = [
    "does not come with a visa sponsorship",
    "does not come with visa sponsorship",
    "does not offer visa sponsorship",
    "unable to offer visa sponsorship",
    "cannot offer visa sponsorship",
    "no visa sponsorship",
    "sponsorship is not available",
    "sponsorship will not be provided",
    "we are unable to sponsor",
    "not eligible for sponsorship",
    "this role is not available for skilled worker",
    "this vacancy is not eligible for visa sponsorship",
    "this post is not eligible",
    "not available for sponsorship",
    "applicants must already have the legal right to work",
    "applicants must have the right to work",
    "please do not apply unless you have",
]


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
    contract_type:             str = ""
    full_description:          str = ""
    sponsorship_keywords_found: list = field(default_factory=list)
    scraped_at:                str = ""

    def __post_init__(self):
        if not self.scraped_at:
            self.scraped_at = datetime.utcnow().isoformat()


class NHSJobsAPIClient:

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def search(self, keyword: str, page: int = 1) -> list:
        params = {"keyword": keyword, "language": "en", "page": str(page)}
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
        root = ET.fromstring(xml_text)
        jobs = []
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
                for tag in tags:
                    el = v.find(tag)
                    if el is not None and el.text:
                        return el.text.strip()
                return ""

            raw_url = get("url", "link", "jobUrl", "job_url")
            url = raw_url if raw_url.startswith("http") else NHS_BASE_URL + raw_url
            job_id = get("id", "jobId", "job_id", "vacancyId")
            if not job_id:
                m = re.search(r"/jobadvert/([^/?#]+)", url)
                job_id = m.group(1) if m else url.split("/")[-1]

            jobs.append(Job(
                job_id        = job_id,
                title         = get("title", "jobTitle", "job_title"),
                employer      = get("employer", "organisation", "trust"),
                location      = get("location", "locationName", "city"),
                salary        = get("salary", "salaryRange", "pay"),
                closing_date  = get("closingDate", "closing_date", "closeDate"),
                contract_type = get("contractType", "contract_type", "type"),
                url           = url,
                summary       = get("description", "summary", "snippet"),
            ))
        log.info(f"  XML API returned {len(jobs)} vacancies")
        return jobs

    def fetch_description(self, url: str) -> str:
        try:
            time.sleep(config.REQUEST_DELAY_SECONDS)
            resp = self.session.get(url, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            for sel in ["div#job-overview", "section.job-overview",
                        "div.job-description", "main article", "main"]:
                el = soup.select_one(sel)
                if el:
                    return el.get_text(separator=" ", strip=True)
            return soup.get_text(separator=" ", strip=True)[:6000]
        except requests.RequestException as e:
            log.warning(f"Could not fetch job page {url}: {e}")
            return ""


class SponsorshipFilter:

    @staticmethod
    def is_relevant_title(title: str) -> bool:
        title_lower = title.lower()
        return any(kw in title_lower for kw in RELEVANT_TITLE_KEYWORDS)

    @staticmethod
    def is_sponsored(job: Job) -> bool:
        combined = f"{job.summary} {job.full_description}".lower()
        for neg in SPONSORSHIP_NEGATIVE:
            if neg in combined:
                log.info(f"  EXCLUDED (negative: '{neg[:50]}')")
                job.sponsorship_keywords_found = []
                return False
        matches = [kw for kw in SPONSORSHIP_POSITIVE if kw in combined]
        job.sponsorship_keywords_found = matches
        return bool(matches)


def run_pipeline() -> list:
    client    = NHSJobsAPIClient()
    tracker   = DynamoSeenJobs()
    sponsored = []
    seen_urls = set()

    log.info("=" * 60)
    log.info("NHS Sponsorship Tracker — Pipeline Start")
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
                if job.url in seen_urls:
                    continue
                seen_urls.add(job.url)
                if not tracker.is_new(job.job_id):
                    log.debug(f"  Already seen: {job.title}")
                    continue
                if not SponsorshipFilter.is_relevant_title(job.title):
                    log.info(f"  SKIPPED (irrelevant title): {job.title}")
                    tracker.mark_seen(job.job_id, title=job.title,
                                      employer=job.employer, url=job.url)
                    continue
                log.info(f"  Checking: {job.title} @ {job.employer}")
                job.full_description = client.fetch_description(job.url)
                if SponsorshipFilter.is_sponsored(job):
                    kws = ", ".join(job.sponsorship_keywords_found)
                    log.info(f"  MATCH [{kws}]: {job.title}")
                    sponsored.append(asdict(job))
                tracker.mark_seen(job.job_id, title=job.title,
                                  employer=job.employer, url=job.url)
            time.sleep(1)

    log.info("\n" + "=" * 60)
    log.info(f"Done. {len(sponsored)} new sponsored job(s) found.")
    log.info("=" * 60)
    return sponsored


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    results = run_pipeline()
    if results:
        print(f"\nNEW SPONSORED JOBS: {len(results)}")
        for job in results:
            print(f"\n  {job['title']} @ {job['employer']}")
            print(f"  {job['salary']} | Closes {job['closing_date']}")
            print(f"  Keywords: {', '.join(job['sponsorship_keywords_found'])}")
            print(f"  {job['url']}")
    else:
        print("\nNo new sponsored jobs found.")
