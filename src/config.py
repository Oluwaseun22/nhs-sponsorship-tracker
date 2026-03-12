import os

MAX_PAGES_PER_KEYWORD = 3
REQUEST_DELAY_SECONDS = 1.5

SEEN_JOBS_FILE      = "seen_jobs.json"
DYNAMODB_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "nhs-seen-jobs")
DYNAMODB_REGION     = os.environ.get("DYNAMODB_REGION",     "eu-west-2")

S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "nhs-job-tracker-data")
S3_PREFIX      = "nhs_jobs_data"

EMAIL_PROVIDER = os.environ.get("EMAIL_PROVIDER", "ses")
SES_REGION    = os.environ.get("SES_REGION",    "eu-west-2")
SES_SENDER    = os.environ.get("SES_SENDER",    "")
SES_RECIPIENT = os.environ.get("SES_RECIPIENT", "")

SMTP_HOST      = "smtp.gmail.com"
SMTP_PORT      = 587
SMTP_USER      = os.environ.get("SMTP_USER",      "")
SMTP_PASSWORD  = os.environ.get("SMTP_PASSWORD",  "")
SMTP_RECIPIENT = os.environ.get("SMTP_RECIPIENT", "")

CV_TEMPLATE_PATH  = "cv_template.docx"
CV_OUTPUT_DIR     = "/tmp/generated_cvs"
CV_MODE           = os.environ.get("CV_MODE", "basic")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
