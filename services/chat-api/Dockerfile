# Chat API image.
FROM python:3.12-slim

WORKDIR /app

COPY services/chat-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The chat service calls the LLM THROUGH the SDK, so the SDK package
# must be importable inside the image.
COPY observability_sdk /app/observability_sdk

COPY services/chat-api/*.py /app/

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
