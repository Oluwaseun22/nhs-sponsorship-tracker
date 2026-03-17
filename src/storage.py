"""
storage.py — AWS S3 Job Data Storage
"""
import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

MASTER_KEY = "nhs_jobs_data/all_jobs.json"


def save_jobs(jobs, local_fallback=False):
    import config

    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    daily_key = f"{config.S3_PREFIX}/{date_str}_jobs.json"

    if not jobs:
        log.info("No new jobs to save.")
        return None

    if local_fallback:
        payload = _build_payload(jobs)
        return _save_local(json.dumps(payload, indent=2, ensure_ascii=False), f"{date_str}_jobs.json")

    try:
        import boto3
        s3 = boto3.client("s3")

        # 1. Save daily file (current scan only)
        daily_payload = _build_payload(jobs)
        s3.put_object(
            Bucket=config.S3_BUCKET_NAME,
            Key=daily_key,
            Body=json.dumps(daily_payload, indent=2, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json"
        )
        log.info(f"Saved daily file: s3://{config.S3_BUCKET_NAME}/{daily_key}")

        # 2. Load existing master file and merge
        existing_jobs = _load_master(s3, config.S3_BUCKET_NAME)
        existing_ids = {j["job_id"] for j in existing_jobs}

        new_jobs = [j for j in jobs if j["job_id"] not in existing_ids]
        all_jobs = existing_jobs + new_jobs

        # 3. Save updated master file
        master_payload = {
            "last_updated": datetime.utcnow().isoformat(),
            "total_jobs": len(all_jobs),
            "jobs": all_jobs
        }
        s3.put_object(
            Bucket=config.S3_BUCKET_NAME,
            Key=MASTER_KEY,
            Body=json.dumps(master_payload, indent=2, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json"
        )
        log.info(f"Master file updated: {len(existing_jobs)} existing + {len(new_jobs)} new = {len(all_jobs)} total jobs")

        return daily_key

    except Exception as e:
        log.warning(f"S3 upload failed ({e}), falling back to local storage")
        payload = _build_payload(jobs)
        return _save_local(json.dumps(payload, indent=2, ensure_ascii=False), f"{date_str}_jobs.json")


def _build_payload(jobs):
    return {
        "scan_date": datetime.utcnow().isoformat(),
        "total_jobs": len(jobs),
        "jobs": jobs
    }


def _load_master(s3, bucket):
    try:
        obj = s3.get_object(Bucket=bucket, Key=MASTER_KEY)
        data = json.loads(obj["Body"].read())
        return data.get("jobs", [])
    except s3.exceptions.NoSuchKey:
        log.info("No master file yet — starting fresh.")
        return []
    except Exception as e:
        log.warning(f"Could not load master file ({e}) — starting fresh.")
        return []


def _save_local(data, filename):
    output_dir = Path("outputs")
    output_dir.mkdir(exist_ok=True)
    filepath = output_dir / filename
    filepath.write_text(data, encoding="utf-8")
    log.info(f"Saved locally to {filepath}")
    return str(filepath)


def load_seen_jobs_from_s3():
    import config
    try:
        import boto3
        s3 = boto3.client("s3")
        obj = s3.get_object(Bucket=config.S3_BUCKET_NAME, Key="seen_jobs.json")
        return set(json.loads(obj["Body"].read()))
    except Exception:
        return set()


def save_seen_jobs_to_s3(seen):
    import config
    try:
        import boto3
        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=config.S3_BUCKET_NAME,
            Key="seen_jobs.json",
            Body=json.dumps(list(seen)).encode(),
            ContentType="application/json"
        )
    except Exception as e:
        log.warning(f"Could not save seen_jobs to S3: {e}")
