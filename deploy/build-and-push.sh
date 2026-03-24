#!/usr/bin/env bash
# build-and-push.sh — Build Docker image, push to ECR, update Lambda.
#
# Usage:
#   ./deploy/build-and-push.sh              # defaults to "latest" tag
#   ./deploy/build-and-push.sh v1.0.0       # use a specific tag
#   GIT_SHA=abc123 ./deploy/build-and-push.sh  # use git SHA as tag
#
# Prerequisites:
#   - Docker running
#   - AWS CLI v2 with "stockbeat" profile configured
#   - ECR repo and Lambda function already created (run create-infrastructure.sh first)

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
AWS_PROFILE="stockbeat"
AWS_REGION="us-east-1"
ECR_REPO_NAME="civicledger"
LAMBDA_FUNCTION_NAME="civicledger-mcp"

# Image tag: CLI arg > GIT_SHA env > git rev-parse > "latest"
IMAGE_TAG="${1:-${GIT_SHA:-$(git rev-parse --short HEAD 2>/dev/null || echo "latest")}}"

export AWS_PROFILE AWS_REGION

# Resolve paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"

# Get account ID and ECR URI
ACCOUNT_ID=$(aws sts get-caller-identity --query "Account" --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}"

echo "=== CivicLedger Deploy ==="
echo "Image:    ${ECR_URI}:${IMAGE_TAG}"
echo "Lambda:   ${LAMBDA_FUNCTION_NAME}"
echo "Region:   ${AWS_REGION}"
echo ""

# ---------------------------------------------------------------------------
# 1. Authenticate Docker to ECR
# ---------------------------------------------------------------------------
echo "--- ECR login ---"
aws ecr get-login-password --region "${AWS_REGION}" \
    | docker login --username AWS --password-stdin \
      "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
echo ""

# ---------------------------------------------------------------------------
# 2. Build Docker image
# ---------------------------------------------------------------------------
echo "--- Building image ---"
docker build \
    --platform linux/amd64 \
    -t "${ECR_REPO_NAME}:${IMAGE_TAG}" \
    "${PROJECT_DIR}"

# Also tag as latest for convenience
docker tag "${ECR_REPO_NAME}:${IMAGE_TAG}" "${ECR_URI}:${IMAGE_TAG}"
docker tag "${ECR_REPO_NAME}:${IMAGE_TAG}" "${ECR_URI}:latest"
echo ""

# ---------------------------------------------------------------------------
# 3. Push to ECR
# ---------------------------------------------------------------------------
echo "--- Pushing to ECR ---"
docker push "${ECR_URI}:${IMAGE_TAG}"
docker push "${ECR_URI}:latest"
echo ""

# ---------------------------------------------------------------------------
# 4. Update Lambda function
# ---------------------------------------------------------------------------
echo "--- Updating Lambda function ---"
aws lambda update-function-code \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --region "${AWS_REGION}" \
    --image-uri "${ECR_URI}:${IMAGE_TAG}"

echo "Waiting for function update to complete..."
aws lambda wait function-updated-v2 \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --region "${AWS_REGION}"
echo ""

# ---------------------------------------------------------------------------
# 5. Verify deployment
# ---------------------------------------------------------------------------
echo "--- Verifying deployment ---"
FUNCTION_URL=$(aws lambda get-function-url-config \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --region "${AWS_REGION}" \
    --query "FunctionUrl" --output text 2>/dev/null || echo "")

if [ -n "${FUNCTION_URL}" ] && [ "${FUNCTION_URL}" != "None" ]; then
    echo "Testing health endpoint..."
    HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${FUNCTION_URL}health" --max-time 30 || echo "000")

    if [ "${HTTP_STATUS}" = "200" ]; then
        echo "Health check PASSED (HTTP ${HTTP_STATUS})"
    else
        echo "WARNING: Health check returned HTTP ${HTTP_STATUS}"
        echo "The function may still be initializing. Try again in a few seconds:"
        echo "  curl ${FUNCTION_URL}health"
    fi
    echo ""
    echo "Function URL: ${FUNCTION_URL}"
else
    echo "No Function URL configured. Health check skipped."
fi

echo ""
echo "=== Deploy complete ==="
echo "Image: ${ECR_URI}:${IMAGE_TAG}"
echo "Lambda: ${LAMBDA_FUNCTION_NAME} updated"
