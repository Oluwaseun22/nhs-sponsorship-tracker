#!/usr/bin/env bash
# =============================================================================
# deploy.sh — NHS Job Sponsorship Tracker — Full AWS Deployment
# =============================================================================
# Runs top-to-bottom on a fresh AWS account.
# Safe to re-run — every step checks if the resource already exists.
#
# Prerequisites:
#   - AWS CLI installed and configured (aws configure)
#   - Python 3.11 available locally
#   - pip available
#   - Your SES sender email already verified in AWS console
#
# Usage:
#   chmod +x deploy.sh
#   ./deploy.sh
#
# Or with overrides:
#   AWS_REGION=eu-west-1 SES_SENDER=seguntoriola25@gmail.com ./deploy.sh
# =============================================================================

set -euo pipefail   # exit on error, unset variable, or pipe failure

# ── Configuration (override with environment variables) ───────────────────────
AWS_REGION="${AWS_REGION:-eu-west-2}"                          # London
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
FUNCTION_NAME="${FUNCTION_NAME:-nhs-job-tracker}"
S3_BUCKET="${S3_BUCKET:-nhs-job-tracker-data}"
DYNAMO_TABLE="${DYNAMO_TABLE:-nhs-seen-jobs}"
ROLE_NAME="${ROLE_NAME:-nhs-job-tracker-role}"
RULE_NAME="${RULE_NAME:-nhs-job-tracker-schedule}"
SES_SENDER="${SES_SENDER:-seguntoriola25@gmail.com}"
SES_RECIPIENT="${SES_RECIPIENT:-seguntoriola25@gmail.com}"
CV_MODE="${CV_MODE:-basic}"                                    # "basic" or "ai"
RUNTIME="python3.11"
HANDLER="lambda_handler.handler"
TIMEOUT=600        # 10 min — generous for multi-keyword scrape
MEMORY=256         # MB — enough for BeautifulSoup + python-docx
SCHEDULE="rate(6 hours)"

# Colours for output
GREEN="\033[0;32m"; YELLOW="\033[1;33m"; RED="\033[0;31m"; NC="\033[0m"
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
success() { echo -e "${GREEN}[DONE]${NC}  $*"; }

echo ""
echo "================================================="
echo "  NHS Job Sponsorship Tracker — AWS Deployment"
echo "================================================="
echo "  Region   : $AWS_REGION"
echo "  Account  : $AWS_ACCOUNT_ID"
echo "  Function : $FUNCTION_NAME"
echo "  S3       : $S3_BUCKET"
echo "  DynamoDB : $DYNAMO_TABLE"
echo "  Schedule : $SCHEDULE"
echo "================================================="
echo ""

# =============================================================================
# STEP 1 — S3 bucket
# =============================================================================
info "Step 1/7 — S3 bucket"

if aws s3api head-bucket --bucket "$S3_BUCKET" 2>/dev/null; then
  warn "S3 bucket '$S3_BUCKET' already exists — skipping creation."
else
  if [ "$AWS_REGION" = "us-east-1" ]; then
    # us-east-1 does not accept LocationConstraint
    aws s3api create-bucket \
      --bucket "$S3_BUCKET" \
      --region "$AWS_REGION"
  else
    aws s3api create-bucket \
      --bucket "$S3_BUCKET" \
      --region "$AWS_REGION" \
      --create-bucket-configuration LocationConstraint="$AWS_REGION"
  fi

  # Block all public access
  aws s3api put-public-access-block \
    --bucket "$S3_BUCKET" \
    --public-access-block-configuration \
      "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

  success "S3 bucket created: $S3_BUCKET"
fi

# =============================================================================
# STEP 2 — DynamoDB table
# =============================================================================
info "Step 2/7 — DynamoDB table"

if aws dynamodb describe-table --table-name "$DYNAMO_TABLE" --region "$AWS_REGION" 2>/dev/null; then
  warn "DynamoDB table '$DYNAMO_TABLE' already exists — skipping creation."
else
  aws dynamodb create-table \
    --table-name "$DYNAMO_TABLE" \
    --attribute-definitions AttributeName=job_id,AttributeType=S \
    --key-schema AttributeName=job_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region "$AWS_REGION"

  info "Waiting for table to become active..."
  aws dynamodb wait table-exists \
    --table-name "$DYNAMO_TABLE" \
    --region "$AWS_REGION"

  # Enable TTL — auto-deletes records after 90 days
  aws dynamodb update-time-to-live \
    --table-name "$DYNAMO_TABLE" \
    --time-to-live-specification "Enabled=true,AttributeName=expires_at" \
    --region "$AWS_REGION"

  success "DynamoDB table created with TTL: $DYNAMO_TABLE"
fi

# =============================================================================
# STEP 3 — IAM role + policy
# =============================================================================
info "Step 3/7 — IAM role"

TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "lambda.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}'

if aws iam get-role --role-name "$ROLE_NAME" 2>/dev/null; then
  warn "IAM role '$ROLE_NAME' already exists — skipping creation."
  ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)
else
  ROLE_ARN=$(aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST_POLICY" \
    --query Role.Arn \
    --output text)

  # Inline policy with all permissions the Lambda needs
  INLINE_POLICY=$(cat iam_policy.json | \
    sed "s/nhs-job-tracker-data/$S3_BUCKET/g" | \
    sed "s/nhs-seen-jobs/$DYNAMO_TABLE/g" | \
    sed "s/eu-west-2/$AWS_REGION/g")

  aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name "nhs-tracker-policy" \
    --policy-document "$INLINE_POLICY"

  success "IAM role created: $ROLE_ARN"
  info "Waiting 10s for IAM role to propagate..."
  sleep 10
fi

# =============================================================================
# STEP 4 — Package Lambda (install deps + zip)
# =============================================================================
info "Step 4/7 — Packaging Lambda"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SRC_DIR="$REPO_DIR/src"
PACKAGE_DIR="$SCRIPT_DIR/lambda_package"
ZIP_FILE="$SCRIPT_DIR/nhs_tracker.zip"

rm -rf "$PACKAGE_DIR" "$ZIP_FILE"
mkdir -p "$PACKAGE_DIR"

pip3 install \
  -r "$SCRIPT_DIR/requirements.txt" \
  --target "$PACKAGE_DIR" \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.11 \
  --only-binary=:all: \
  --upgrade \
  --quiet

cp "$SRC_DIR"/*.py "$PACKAGE_DIR/"
cp "$SRC_DIR/cv_template.docx" "$PACKAGE_DIR/"

cd "$PACKAGE_DIR"
zip -r "$ZIP_FILE" . -q
cd "$SCRIPT_DIR"

ZIP_SIZE_MB=$(du -sh "$ZIP_FILE" | cut -f1)
echo "[DONE]  Lambda package created: $ZIP_FILE ($ZIP_SIZE_MB)"
info "Step 5/7 — Lambda function"

ENV_VARS="Variables={\
S3_BUCKET_NAME=$S3_BUCKET,\
DYNAMODB_TABLE_NAME=$DYNAMO_TABLE,\
DYNAMODB_REGION=$AWS_REGION,\
SES_SENDER=$SES_SENDER,\
SES_RECIPIENT=$SES_RECIPIENT,\
SES_REGION=$AWS_REGION,\
EMAIL_PROVIDER=ses,\
CV_MODE=$CV_MODE\
}"

if aws lambda get-function --function-name "$FUNCTION_NAME" --region "$AWS_REGION" 2>/dev/null; then
  warn "Lambda '$FUNCTION_NAME' exists — updating code and config."

  aws lambda update-function-code \
    --function-name "$FUNCTION_NAME" \
    --zip-file "fileb://$ZIP_FILE" \
    --region "$AWS_REGION" \
    --output table

  # Wait for update to complete before changing config
  aws lambda wait function-updated \
    --function-name "$FUNCTION_NAME" \
    --region "$AWS_REGION"

  aws lambda update-function-configuration \
    --function-name "$FUNCTION_NAME" \
    --timeout "$TIMEOUT" \
    --memory-size "$MEMORY" \
    --environment "$ENV_VARS" \
    --region "$AWS_REGION" \
    --output table

else
  aws lambda create-function \
    --function-name "$FUNCTION_NAME" \
    --runtime "$RUNTIME" \
    --handler "$HANDLER" \
    --role "$ROLE_ARN" \
    --zip-file "fileb://$ZIP_FILE" \
    --timeout "$TIMEOUT" \
    --memory-size "$MEMORY" \
    --environment "$ENV_VARS" \
    --region "$AWS_REGION" \
    --output table

  success "Lambda function created: $FUNCTION_NAME"
fi

FUNCTION_ARN="arn:aws:lambda:$AWS_REGION:$AWS_ACCOUNT_ID:function:$FUNCTION_NAME"

# =============================================================================
# STEP 6 — EventBridge schedule
# =============================================================================
info "Step 6/7 — EventBridge schedule ($SCHEDULE)"

aws events put-rule \
  --name "$RULE_NAME" \
  --schedule-expression "$SCHEDULE" \
  --state ENABLED \
  --region "$AWS_REGION" \
  --output table

# Grant EventBridge permission to invoke Lambda
aws lambda add-permission \
  --function-name "$FUNCTION_NAME" \
  --statement-id "allow-eventbridge" \
  --action "lambda:InvokeFunction" \
  --principal "events.amazonaws.com" \
  --source-arn "arn:aws:events:$AWS_REGION:$AWS_ACCOUNT_ID:rule/$RULE_NAME" \
  --region "$AWS_REGION" 2>/dev/null || \
  warn "Permission already exists — skipping."

RULE_ARN="arn:aws:events:$AWS_REGION:$AWS_ACCOUNT_ID:rule/$RULE_NAME"

aws events put-targets \
  --rule "$RULE_NAME" \
  --targets "Id=nhs-tracker-target,Arn=$FUNCTION_ARN" \
  --region "$AWS_REGION" \
  --output table

success "EventBridge rule created: $RULE_NAME"

# =============================================================================
# STEP 7 — Smoke test (manual invoke)
# =============================================================================
info "Step 7/7 — Smoke test (invoking Lambda once)"

aws lambda invoke \
  --function-name "$FUNCTION_NAME" \
  --region "$AWS_REGION" \
  --log-type Tail \
  --payload '{}' \
  response.json \
  --output text \
  --query 'LogResult' | base64 --decode | tail -20

echo ""
info "Lambda response:"
cat response.json
rm -f response.json

# =============================================================================
# DONE
# =============================================================================
echo ""
echo "================================================="
echo -e "  ${GREEN}Deployment complete!${NC}"
echo "================================================="
echo ""
echo "  Resources created:"
echo "    S3 bucket   : s3://$S3_BUCKET"
echo "    DynamoDB    : $DYNAMO_TABLE ($AWS_REGION)"
echo "    Lambda      : $FUNCTION_NAME"
echo "    Schedule    : every 6 hours (EventBridge)"
echo ""
echo "  Useful commands:"
echo "    Tail logs  :  aws logs tail /aws/lambda/$FUNCTION_NAME --follow"
echo "    Run now    :  aws lambda invoke --function-name $FUNCTION_NAME --payload '{}' out.json"
echo "    View jobs  :  aws s3 ls s3://$S3_BUCKET/nhs_jobs_data/"
echo "    DynamoDB   :  aws dynamodb scan --table-name $DYNAMO_TABLE --region $AWS_REGION"
echo ""
echo "  IMPORTANT: Verify your SES sender address if you haven't already:"
echo "    https://console.aws.amazon.com/ses/home#/verified-identities"
echo ""
