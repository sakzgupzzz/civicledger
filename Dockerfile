# CivicLedger Lambda Container Image
#
# Multi-stage build for minimal image size.
# Stage 1: Install all dependencies + package into a clean target dir
# Stage 2: Copy only runtime artifacts into the Lambda base image
#
# Build:  docker build -t civicledger-lambda .
# Test:   docker run -p 9000:8080 civicledger-lambda

# ---------------------------------------------------------------------------
# Stage 1: Build dependencies
# ---------------------------------------------------------------------------
FROM public.ecr.aws/lambda/python:3.12 AS builder

# Install build tools for compiled deps (lxml, pandas, etc.)
RUN dnf install -y gcc gcc-c++ libxml2-devel libxslt-devel && \
    dnf clean all

WORKDIR /tmp/build

# Copy full project (filtered by .dockerignore)
COPY pyproject.toml README.md ./
COPY civicledger/ ./civicledger/

# Install the package + [server] extra into a target directory.
# Skip [dev] dependencies entirely. --no-cache-dir keeps the layer small.
RUN pip install --no-cache-dir --target /tmp/deps ".[server]"

# ---------------------------------------------------------------------------
# Stage 2: Final Lambda image
# ---------------------------------------------------------------------------
FROM public.ecr.aws/lambda/python:3.12

# Copy installed packages from builder
COPY --from=builder /tmp/deps ${LAMBDA_TASK_ROOT}

# Lambda needs a writable data directory for sqlite
RUN mkdir -p ${LAMBDA_TASK_ROOT}/data

# Environment
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Lambda handler: module.function
CMD ["civicledger.lambda_handler.handler"]
