"""
storage.py — AWS S3 Job Data Storage
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

def save_jobs(jobs, local_fallback=False):
    import config
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    filename = f"{date_str}_jobs.json"
    s3_key = f"{config.S3_PREFIX}/{filename}"
    payload = {"scan_date": datetime.utcnow().isoformat(), "total_jobs": len(jobs), "jobs": jobs}
    data = json.dumps(payload, indent=2, ensure_ascii=False)
    if local_fallback:
        return _save_local(data, filename)
    try:
        import boto3
        s3 = boto3.client("s3")
        s3.put_object(Bucket=config.S3_BUCKET_NAME, Key=s3_key, Body=data.encode("utf-8"), ContentType="application/json")
        log.info(f"Saved {len(jobs)} jobs to s3://{config.S3_BUCKET_NAME}/{s3_key}")
        return s3_key
    except Exception as e:
        log.warning(f"S3 upload failed ({e}), falling back to local storage")
        return _save_local(data, filename)

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
        s3.put_object(Bucket=config.S3_BUCKET_NAME, Key="seen_jobs.json", Body=json.dumps(list(seen)).encode(), ContentType="application/json")
    except Exception as e:
        log.warning(f"Could not save seen_jobs to S3: {e}")
