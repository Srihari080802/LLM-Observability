# Ingestion API image.
FROM python:3.12-slim

WORKDIR /app

# Install service dependencies first (better layer caching).
COPY services/ingestion-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The SDK is copied in because the ingestion service reuses its
# redact_pii helper (shared redaction logic, single source of truth).
COPY observability_sdk /app/observability_sdk

# Service code.
COPY services/ingestion-api/*.py /app/

EXPOSE 8001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
