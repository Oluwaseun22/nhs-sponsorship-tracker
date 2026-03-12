"""
deduplication.py — DynamoDB-backed Job Deduplication
======================================================
Replaces the local seen_jobs.json file with a DynamoDB table.
This makes the Lambda function fully stateless — no file system
state, no S3 workaround. DynamoDB is the single source of truth.

Table schema:
  Table name : nhs-seen-jobs          (configurable via config.py)
  Partition key : job_id  (String)
  TTL attribute : expires_at (Number)  — auto-deletes old items after 90 days

Free tier: 25 GB storage + 200M requests/month — more than enough.

Usage (drop-in replacement for SeenJobsTracker in scraper.py):

    from deduplication import DynamoSeenJobs

    tracker = DynamoSeenJobs()
    if tracker.is_new("job-123"):
        # process job
        tracker.mark_seen("job-123", title="Data Analyst", employer="NHS Trust")

Local fallback:
    If DynamoDB is unreachable (e.g. local dev without AWS creds),
    it automatically falls back to a local JSON file — same behaviour
    as before, zero code change needed.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# TTL: keep seen job records for 90 days, then DynamoDB auto-deletes them.
# This means jobs re-posted after ~3 months will trigger a fresh alert.
TTL_DAYS = 90


class DynamoSeenJobs:
    """
    DynamoDB-backed store for seen job IDs.
    Falls back to local JSON if DynamoDB is unavailable.
    """

    def __init__(self):
        import config
        self.table_name   = config.DYNAMODB_TABLE_NAME
        self.region       = config.DYNAMODB_REGION
        self._table       = None          # lazy-initialised
        self._fallback    = False         # True if we had to fall back to local
        import tempfile, os
        self._local_path  = os.path.join(tempfile.gettempdir(), 'seen_jobs.json')
        self._local_cache: Optional[set] = None

    # ── DynamoDB connection ────────────────────────────────────────────────────

    def _get_table(self):
        """Lazy-initialise the DynamoDB table resource."""
        if self._table is not None:
            return self._table
        try:
            import boto3
            dynamodb = boto3.resource("dynamodb", region_name=self.region)
            self._table = dynamodb.Table(self.table_name)
            # Ping the table to confirm it exists and we have access
            self._table.load()
            log.info(f"DynamoDB table '{self.table_name}' connected.")
            return self._table
        except Exception as e:
            log.warning(
                f"DynamoDB unavailable ({e}). "
                f"Falling back to local JSON: {self._local_path}"
            )
            self._fallback = True
            return None

    # ── Public API ─────────────────────────────────────────────────────────────

    def is_new(self, job_id: str) -> bool:
        """Return True if this job_id has NOT been seen before."""
        if self._fallback or self._get_table() is None:
            return job_id not in self._load_local()

        try:
            resp = self._table.get_item(Key={"job_id": job_id})
            return "Item" not in resp
        except Exception as e:
            log.warning(f"DynamoDB get_item failed ({e}), checking local cache.")
            return job_id not in self._load_local()

    def mark_seen(
        self,
        job_id:   str,
        title:    str = "",
        employer: str = "",
        url:      str = "",
    ):
        """
        Record a job_id as seen. Stores metadata alongside the ID
        so the table is useful for auditing / dashboard queries too.
        TTL is set 90 days from now — DynamoDB will auto-delete after that.
        """
        if self._fallback or self._get_table() is None:
            self._mark_local(job_id)
            return

        expires_at = int(
            (datetime.utcnow() + timedelta(days=TTL_DAYS)).timestamp()
        )
        try:
            self._table.put_item(Item={
                "job_id":     job_id,
                "title":      title,
                "employer":   employer,
                "url":        url,
                "seen_at":    datetime.utcnow().isoformat(),
                "expires_at": expires_at,          # DynamoDB TTL attribute
            })
        except Exception as e:
            log.warning(f"DynamoDB put_item failed ({e}), writing to local cache.")
            self._mark_local(job_id)

    def batch_mark_seen(self, jobs: list[dict]):
        """
        Mark multiple jobs as seen in a single DynamoDB batch write.
        More efficient than calling mark_seen() in a loop for large batches.
        """
        if not jobs:
            return

        if self._fallback or self._get_table() is None:
            for job in jobs:
                self._mark_local(job.get("job_id", ""))
            return

        expires_at = int(
            (datetime.utcnow() + timedelta(days=TTL_DAYS)).timestamp()
        )
        try:
            with self._table.batch_writer() as batch:
                for job in jobs:
                    batch.put_item(Item={
                        "job_id":     job.get("job_id", ""),
                        "title":      job.get("title", ""),
                        "employer":   job.get("employer", ""),
                        "url":        job.get("url", ""),
                        "seen_at":    datetime.utcnow().isoformat(),
                        "expires_at": expires_at,
                    })
            log.info(f"Batch marked {len(jobs)} jobs as seen in DynamoDB.")
        except Exception as e:
            log.warning(f"DynamoDB batch_write failed ({e}).")

    # ── Local JSON fallback ────────────────────────────────────────────────────

    def _load_local(self) -> set:
        """Load seen job IDs from the local JSON fallback file."""
        if self._local_cache is not None:
            return self._local_cache
        try:
            with open(self._local_path) as f:
                self._local_cache = set(json.load(f))
        except (FileNotFoundError, ValueError):
            self._local_cache = set()
        return self._local_cache

    def _mark_local(self, job_id: str):
        """Add a job_id to the local JSON fallback and persist."""
        cache = self._load_local()
        cache.add(job_id)
        with open(self._local_path, "w") as f:
            json.dump(list(cache), f)


# ── AWS setup helper ───────────────────────────────────────────────────────────

def create_dynamodb_table(region: str = None, table_name: str = None):
    """
    One-time setup: create the DynamoDB table with TTL enabled.
    Run this manually once during deployment — NOT in the Lambda handler.

    Example:
        python -c "from deduplication import create_dynamodb_table; create_dynamodb_table()"
    """
    import config
    import boto3

    region     = region     or config.DYNAMODB_REGION
    table_name = table_name or config.DYNAMODB_TABLE_NAME

    dynamodb = boto3.client("dynamodb", region_name=region)

    # Create table
    try:
        dynamodb.create_table(
            TableName            = table_name,
            AttributeDefinitions = [
                {"AttributeName": "job_id", "AttributeType": "S"},
            ],
            KeySchema = [
                {"AttributeName": "job_id", "KeyType": "HASH"},
            ],
            BillingMode = "PAY_PER_REQUEST",   # on-demand — no provisioned capacity cost
        )
        log.info(f"Created DynamoDB table: {table_name}")
    except dynamodb.exceptions.ResourceInUseException:
        log.info(f"Table '{table_name}' already exists — skipping creation.")

    # Wait for table to become active
    waiter = dynamodb.get_waiter("table_exists")
    waiter.wait(TableName=table_name)
    log.info("Table is active.")

    # Enable TTL on the expires_at attribute
    dynamodb.update_time_to_live(
        TableName              = table_name,
        TimeToLiveSpecification = {
            "Enabled":       True,
            "AttributeName": "expires_at",
        },
    )
    log.info("TTL enabled on 'expires_at' attribute (90-day auto-expiry).")
    print(f"\nDynamoDB table '{table_name}' is ready in region '{region}'.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    create_dynamodb_table()
