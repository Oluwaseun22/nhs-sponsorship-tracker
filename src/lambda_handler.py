"""
lambda_handler.py — AWS Lambda Entry Point
===========================================
Triggered every 6 hours by EventBridge.

Pipeline:
  scrape (XML API) → deduplicate (DynamoDB) → store (S3)
  → generate CV (python-docx / Claude API) → notify (SES)
"""

import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from scraper      import run_pipeline
from storage      import save_jobs
from notifier     import notify
from cv_generator import generate_cv
import config

log = logging.getLogger()
log.setLevel(logging.INFO)


def handler(event, context):
    """Main Lambda handler — called by EventBridge every 6 hours."""
    log.info("Lambda triggered — NHS Job Sponsorship Tracker")

    try:
        # ── 1. Scrape + filter (dedup handled inside via DynamoDB) ────────────
        sponsored_jobs = run_pipeline()

        if not sponsored_jobs:
            log.info("No new sponsored jobs — nothing to do.")
            return _ok(0)

        # ── 2. Persist all results to S3 ──────────────────────────────────────
        s3_key = save_jobs(sponsored_jobs)
        log.info(f"Saved {len(sponsored_jobs)} job(s) → {s3_key}")

        # ── 3. Generate tailored CV for the top result ─────────────────────────
        cv_path = None
        try:
            cv_path = generate_cv(sponsored_jobs[0], mode=config.CV_MODE)
            log.info(f"CV generated: {cv_path}")
        except Exception as cv_err:
            log.warning(f"CV generation failed (will still notify): {cv_err}")

        # ── 4. Send email alert ────────────────────────────────────────────────
        notify(sponsored_jobs, cv_path=cv_path)
        log.info("Notification sent.")

        return _ok(len(sponsored_jobs))

    except Exception as e:
        log.error(f"Pipeline failed: {e}", exc_info=True)
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}


def _ok(count: int) -> dict:
    return {
        "statusCode": 200,
        "body": json.dumps({
            "message":        "Pipeline complete",
            "new_jobs_found": count,
        }),
    }


if __name__ == "__main__":
    result = handler({}, None)
    print(json.dumps(result, indent=2))
