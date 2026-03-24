#!/usr/bin/env bash
# create-infrastructure.sh — One-time setup for CivicLedger Lambda deployment.
#
# Creates:
#   1. ECR repository
#   2. IAM role for Lambda execution
#   3. Lambda function (placeholder — image updated by build-and-push.sh)
#   4. Lambda Function URL (for MCP SSE transport)
#   5. EventBridge rules for scheduled data refresh
#
# Usage:
#   ./deploy/create-infrastructure.sh
#
# Prerequisites:
#   - AWS CLI v2 installed
#   - AWS profile "stockbeat" configured with us-east-1
#   - CIVICLEDGER_FRED_API_KEY set (or pass as argument)

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
AWS_PROFILE="stockbeat"
AWS_REGION="us-east-1"
ECR_REPO_NAME="civicledger"
LAMBDA_FUNCTION_NAME="civicledger-mcp"
LAMBDA_ROLE_NAME="civicledger-lambda-role"
LAMBDA_MEMORY=1024
LAMBDA_TIMEOUT=300

# Environment variables for the Lambda function
FRED_API_KEY="${CIVICLEDGER_FRED_API_KEY:-}"
EDGAR_IDENTITY="${CIVICLEDGER_EDGAR_IDENTITY:-CivicLedger admin@civicledger.dev}"

export AWS_PROFILE AWS_REGION

echo "=== CivicLedger Infrastructure Setup ==="
echo "Profile: ${AWS_PROFILE}"
echo "Region:  ${AWS_REGION}"
echo ""

# ---------------------------------------------------------------------------
# 1. ECR Repository
# ---------------------------------------------------------------------------
echo "--- Creating ECR repository: ${ECR_REPO_NAME} ---"
aws ecr describe-repositories \
    --repository-names "${ECR_REPO_NAME}" \
    --region "${AWS_REGION}" 2>/dev/null \
|| aws ecr create-repository \
    --repository-name "${ECR_REPO_NAME}" \
    --region "${AWS_REGION}" \
    --image-scanning-configuration scanOnPush=true \
    --image-tag-mutability MUTABLE

# Get the ECR URI
ACCOUNT_ID=$(aws sts get-caller-identity --query "Account" --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}"
echo "ECR URI: ${ECR_URI}"
echo ""

# Set lifecycle policy to keep only last 5 images
echo "--- Setting ECR lifecycle policy ---"
aws ecr put-lifecycle-policy \
    --repository-name "${ECR_REPO_NAME}" \
    --region "${AWS_REGION}" \
    --lifecycle-policy-text '{
        "rules": [
            {
                "rulePriority": 1,
                "description": "Keep only last 5 images",
                "selection": {
                    "tagStatus": "any",
                    "countType": "imageCountMoreThan",
                    "countNumber": 5
                },
                "action": {
                    "type": "expire"
                }
            }
        ]
    }'
echo ""

# ---------------------------------------------------------------------------
# 2. IAM Role for Lambda
# ---------------------------------------------------------------------------
echo "--- Creating IAM role: ${LAMBDA_ROLE_NAME} ---"

# Trust policy for Lambda
TRUST_POLICY='{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "Service": "lambda.amazonaws.com"
            },
            "Action": "sts:AssumeRole"
        }
    ]
}'

# Create role (ignore if exists)
aws iam get-role \
    --role-name "${LAMBDA_ROLE_NAME}" 2>/dev/null \
|| aws iam create-role \
    --role-name "${LAMBDA_ROLE_NAME}" \
    --assume-role-policy-document "${TRUST_POLICY}" \
    --description "Execution role for CivicLedger Lambda function"

# Attach managed policies
aws iam attach-role-policy \
    --role-name "${LAMBDA_ROLE_NAME}" \
    --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" 2>/dev/null || true

ROLE_ARN=$(aws iam get-role \
    --role-name "${LAMBDA_ROLE_NAME}" \
    --query "Role.Arn" --output text)
echo "Role ARN: ${ROLE_ARN}"
echo ""

# ---------------------------------------------------------------------------
# 3. Lambda Function
# ---------------------------------------------------------------------------
echo "--- Creating Lambda function: ${LAMBDA_FUNCTION_NAME} ---"

# Check if function exists
if aws lambda get-function --function-name "${LAMBDA_FUNCTION_NAME}" --region "${AWS_REGION}" 2>/dev/null; then
    echo "Lambda function already exists, updating configuration..."
    aws lambda update-function-configuration \
        --function-name "${LAMBDA_FUNCTION_NAME}" \
        --region "${AWS_REGION}" \
        --memory-size "${LAMBDA_MEMORY}" \
        --timeout "${LAMBDA_TIMEOUT}" \
        --environment "Variables={CIVICLEDGER_FRED_API_KEY=${FRED_API_KEY},CIVICLEDGER_EDGAR_IDENTITY=${EDGAR_IDENTITY}}"
else
    # Need a placeholder image to create the function.
    # First push — build-and-push.sh will update this.
    # We need to wait for IAM role propagation.
    echo "Waiting 10s for IAM role propagation..."
    sleep 10

    # Build and push a minimal image first
    echo "Building and pushing initial image..."
    aws ecr get-login-password --region "${AWS_REGION}" \
        | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"

    docker build -t "${ECR_REPO_NAME}:latest" "${PROJECT_DIR}"
    docker tag "${ECR_REPO_NAME}:latest" "${ECR_URI}:latest"
    docker push "${ECR_URI}:latest"

    aws lambda create-function \
        --function-name "${LAMBDA_FUNCTION_NAME}" \
        --region "${AWS_REGION}" \
        --package-type Image \
        --code "ImageUri=${ECR_URI}:latest" \
        --role "${ROLE_ARN}" \
        --memory-size "${LAMBDA_MEMORY}" \
        --timeout "${LAMBDA_TIMEOUT}" \
        --environment "Variables={CIVICLEDGER_FRED_API_KEY=${FRED_API_KEY},CIVICLEDGER_EDGAR_IDENTITY=${EDGAR_IDENTITY}}" \
        --architectures x86_64

    echo "Waiting for function to become Active..."
    aws lambda wait function-active-v2 \
        --function-name "${LAMBDA_FUNCTION_NAME}" \
        --region "${AWS_REGION}"
fi
echo ""

# ---------------------------------------------------------------------------
# 4. Lambda Function URL
# ---------------------------------------------------------------------------
echo "--- Creating Lambda Function URL ---"

# Check if URL config exists
FUNCTION_URL=$(aws lambda get-function-url-config \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --region "${AWS_REGION}" \
    --query "FunctionUrl" --output text 2>/dev/null) \
|| FUNCTION_URL=""

if [ -z "${FUNCTION_URL}" ] || [ "${FUNCTION_URL}" = "None" ]; then
    aws lambda create-function-url-config \
        --function-name "${LAMBDA_FUNCTION_NAME}" \
        --region "${AWS_REGION}" \
        --auth-type NONE \
        --invoke-mode RESPONSE_STREAM \
        --cors '{
            "AllowOrigins": ["*"],
            "AllowMethods": ["GET", "POST", "OPTIONS"],
            "AllowHeaders": ["Content-Type", "Authorization"],
            "MaxAge": 86400
        }'

    # Grant public access to the Function URL
    aws lambda add-permission \
        --function-name "${LAMBDA_FUNCTION_NAME}" \
        --region "${AWS_REGION}" \
        --statement-id FunctionURLAllowPublicAccess \
        --action lambda:InvokeFunctionUrl \
        --principal "*" \
        --function-url-auth-type NONE 2>/dev/null || true

    FUNCTION_URL=$(aws lambda get-function-url-config \
        --function-name "${LAMBDA_FUNCTION_NAME}" \
        --region "${AWS_REGION}" \
        --query "FunctionUrl" --output text)
fi

echo "Function URL: ${FUNCTION_URL}"
echo ""

# ---------------------------------------------------------------------------
# 5. EventBridge Rules for Scheduled Refresh
# ---------------------------------------------------------------------------
echo "--- Creating EventBridge rules ---"

LAMBDA_ARN=$(aws lambda get-function \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --region "${AWS_REGION}" \
    --query "Configuration.FunctionArn" --output text)

# Rule 1: Daily fundamentals refresh at 7 AM ET (12:00 UTC in EDT, 12:00 UTC)
# Using 12:00 UTC which is 7 AM ET during EDT / 7 AM during EST would be 12:00 UTC
# ET = UTC-5 (EST) or UTC-4 (EDT). 7 AM ET = 11:00 UTC (EDT) or 12:00 UTC (EST)
# Use 12:00 UTC to cover EST (7 AM)
aws events put-rule \
    --name "civicledger-daily-fundamentals" \
    --region "${AWS_REGION}" \
    --schedule-expression "cron(0 12 * * ? *)" \
    --state ENABLED \
    --description "CivicLedger: Refresh fundamentals data daily at ~7 AM ET"

aws events put-targets \
    --rule "civicledger-daily-fundamentals" \
    --region "${AWS_REGION}" \
    --targets "[{
        \"Id\": \"civicledger-fundamentals\",
        \"Arn\": \"${LAMBDA_ARN}\",
        \"Input\": \"{\\\"source\\\": \\\"aws.events\\\", \\\"detail\\\": {\\\"command\\\": \\\"fundamentals\\\"}}\"
    }]"

# Grant EventBridge permission to invoke Lambda
aws lambda add-permission \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --region "${AWS_REGION}" \
    --statement-id EventBridgeDailyFundamentals \
    --action lambda:InvokeFunction \
    --principal events.amazonaws.com \
    --source-arn "arn:aws:events:${AWS_REGION}:${ACCOUNT_ID}:rule/civicledger-daily-fundamentals" 2>/dev/null || true

# Rule 2: Weekly full refresh on Sunday at 12:00 UTC (~7 AM ET)
aws events put-rule \
    --name "civicledger-weekly-all" \
    --region "${AWS_REGION}" \
    --schedule-expression "cron(0 12 ? * SUN *)" \
    --state ENABLED \
    --description "CivicLedger: Full data refresh every Sunday at ~7 AM ET"

aws events put-targets \
    --rule "civicledger-weekly-all" \
    --region "${AWS_REGION}" \
    --targets "[{
        \"Id\": \"civicledger-all\",
        \"Arn\": \"${LAMBDA_ARN}\",
        \"Input\": \"{\\\"source\\\": \\\"aws.events\\\", \\\"detail\\\": {\\\"command\\\": \\\"all\\\"}}\"
    }]"

aws lambda add-permission \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --region "${AWS_REGION}" \
    --statement-id EventBridgeWeeklyAll \
    --action lambda:InvokeFunction \
    --principal events.amazonaws.com \
    --source-arn "arn:aws:events:${AWS_REGION}:${ACCOUNT_ID}:rule/civicledger-weekly-all" 2>/dev/null || true

echo ""

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "========================================="
echo "  CivicLedger Infrastructure Complete"
echo "========================================="
echo ""
echo "ECR Repository:  ${ECR_URI}"
echo "Lambda Function: ${LAMBDA_FUNCTION_NAME}"
echo "Function URL:    ${FUNCTION_URL}"
echo "IAM Role:        ${ROLE_ARN}"
echo ""
echo "EventBridge Rules:"
echo "  - civicledger-daily-fundamentals (daily 12:00 UTC)"
echo "  - civicledger-weekly-all (Sunday 12:00 UTC)"
echo ""
echo "Next steps:"
echo "  1. Set secrets in Lambda environment:"
echo "     CIVICLEDGER_FRED_API_KEY"
echo "     CIVICLEDGER_EDGAR_IDENTITY"
echo "  2. Run: ./deploy/build-and-push.sh"
echo "  3. Test: curl ${FUNCTION_URL}health"
echo ""
