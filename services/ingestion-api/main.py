"""
main.py — Ingestion API
=======================

Responsibilities (deliverable #3):
    1. receive logs from the SDK            -> events OR POST /ingest
    2. validate / parse payloads            -> Pydantic (models.py)
    3. extract useful metadata + redact PII -> done in store_log()
    4. store processed data in the database -> inference_logs table

Plus dashboard read endpoints (bonus): /metrics/* aggregate inference_logs
so the frontend dashboard is a thin client over SQL.

TWO INGESTION PATHS
-------------------
This service can ingest logs two ways, and runs BOTH at once:

  event-based (primary):  a background task consumes a Redis Stream.
      SDK --XADD--> Redis Stream --XREADGROUP--> this consumer --> DB
      The producer (SDK) and consumer (this service) are fully
      decoupled; if this service is down, events wait in the stream.

  HTTP (kept for compatibility):  POST /ingest does the same thing
      synchronously. Useful with no broker, and for direct testing.

Both paths funnel into ONE function -- store_log() -- so validation,
PII redaction, and the database insert are identical regardless of how
the log arrived.
"""
from __future__ import annotations
import sys
import os
import json
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# Make the SDK importable for its redact_pii helper (shared redaction).
sys.path.insert(0, "/app")
from observability_sdk import redact_pii  # noqa: E402

from models import InferenceLogIn, IngestResponse  # noqa: E402
import db  # noqa: E402

# --- event transport configuration ---
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
STREAM_KEY = os.environ.get("LOG_STREAM_KEY", "inference-logs")
CONSUMER_GROUP = "ingestion-workers"   # consumer group name
CONSUMER_NAME = os.environ.get("HOSTNAME", "ingestion-1")  # this worker
# Whether to run the Redis consumer. 'events' = yes; 'http' = HTTP only.
LOG_TRANSPORT = os.environ.get("LOG_TRANSPORT", "events")


# ----------------------------------------------------------------------
# SHARED STORE LOGIC — used by BOTH the HTTP endpoint and the consumer.
# ----------------------------------------------------------------------
async def store_log(log: InferenceLogIn) -> None:
    """Validate-time has already happened (Pydantic). Redact PII and
    insert one inference log. Raises on DB failure so the caller can
    decide whether to retry / NACK."""
    # server-side PII redaction (enforced regardless of how the log arrived)
    input_preview = redact_pii(log.input_preview or "")
    output_preview = redact_pii(log.output_preview or "")

    # Guard the conversation FK: store NULL rather than fail the insert
    # if the conversation_id is unknown. Observability data is best-effort.
    async with db.pool().acquire() as conn:
        convo_exists = False
        if log.conversation_id:
            convo_exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM conversations WHERE id = $1)",
                log.conversation_id,
            )
        await conn.execute(
            """
            INSERT INTO inference_logs (
                id, conversation_id, provider, model,
                latency_ms, prompt_tokens, completion_tokens, total_tokens,
                status, error_message, input_preview, output_preview,
                request_started_at
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            ON CONFLICT (id) DO NOTHING
            """,
            log.log_id,
            log.conversation_id if convo_exists else None,
            log.provider, log.model,
            log.latency_ms, log.prompt_tokens,
            log.completion_tokens, log.total_tokens,
            log.status, log.error_message,
            input_preview, output_preview,
            log.request_started_at,
        )


# ----------------------------------------------------------------------
# REDIS STREAM CONSUMER — the event-based ingestion path.
# ----------------------------------------------------------------------
async def consume_events(stop: asyncio.Event) -> None:
    """Background task: read log events from the Redis Stream and store
    them. Uses a CONSUMER GROUP so the work could be split across many
    ingestion replicas, and each event is ACKnowledged only after it is
    safely stored (at-least-once delivery)."""
    import redis.asyncio as aioredis

    client = aioredis.from_url(REDIS_URL, decode_responses=True)

    # Create the consumer group. mkstream=True creates the stream if it
    # does not exist yet. If the group already exists, ignore the error.
    try:
        await client.xgroup_create(
            STREAM_KEY, CONSUMER_GROUP, id="0", mkstream=True
        )
    except Exception as exc:  # noqa: BLE001
        if "BUSYGROUP" not in str(exc):
            print(f"[consumer] xgroup_create: {exc}")

    print(f"[consumer] listening on stream '{STREAM_KEY}' "
          f"as '{CONSUMER_NAME}' in group '{CONSUMER_GROUP}'")

    while not stop.is_set():
        try:
            # Block up to 2s waiting for new events. '>' means "messages
            # never delivered to any consumer in this group".
            resp = await client.xreadgroup(
                CONSUMER_GROUP, CONSUMER_NAME,
                {STREAM_KEY: ">"}, count=20, block=2000,
            )
            if not resp:
                continue
            for _stream, entries in resp:
                for entry_id, fields in entries:
                    try:
                        payload = json.loads(fields["data"])
                        log = InferenceLogIn(**payload)  # Pydantic validation
                        await store_log(log)
                        # ACK only after a successful store. If we crash
                        # before this, the event stays pending and is
                        # redelivered -> at-least-once delivery.
                        await client.xack(STREAM_KEY, CONSUMER_GROUP, entry_id)
                    except Exception as exc:  # noqa: BLE001
                        # A bad event must not stall the consumer. Log it
                        # and ACK so it does not redeliver forever. A
                        # production system would route it to a dead-
                        # letter stream instead.
                        print(f"[consumer] dropping bad event {entry_id}: {exc}")
                        await client.xack(STREAM_KEY, CONSUMER_GROUP, entry_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[consumer] read loop error: {exc}")
            await asyncio.sleep(1)

    await client.aclose()
    print("[consumer] stopped")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup: open the DB pool, and start the consumer if events are on
    await db.init_pool()
    stop = asyncio.Event()
    consumer_task = None
    if LOG_TRANSPORT == "events":
        consumer_task = asyncio.create_task(consume_events(stop))
    yield
    # shutdown: stop the consumer, then close the pool
    if consumer_task is not None:
        stop.set()
        await consumer_task
    await db.close_pool()


app = FastAPI(title="LLM Observability — Ingestion API", lifespan=lifespan)

# The dashboard is served from a different origin in dev, so allow CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------------------------
# health
# ----------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "transport": LOG_TRANSPORT}


# ----------------------------------------------------------------------
# HTTP INGESTION ENDPOINT — same store_log() as the consumer.
# ----------------------------------------------------------------------
@app.post("/ingest", response_model=IngestResponse)
async def ingest(log: InferenceLogIn):
    """Receive one inference log over HTTP, redact, and persist it.

    `log` is already validated by Pydantic by the time we get here.
    This path is kept for compatibility and direct testing; the
    event-based consumer is the primary ingestion route.
    """
    try:
        await store_log(log)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"store failed: {exc}")
    return IngestResponse(accepted=True, log_id=log.log_id)


# ----------------------------------------------------------------------
# DASHBOARD READ ENDPOINTS
# These aggregate inference_logs. The frontend dashboard just renders them.
# ----------------------------------------------------------------------
@app.get("/metrics/summary")
async def metrics_summary():
    """Top-line numbers: total calls, error rate, avg + p95 latency, tokens."""
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                                          AS total_calls,
                COUNT(*) FILTER (WHERE status = 'error')          AS errors,
                AVG(latency_ms)::INT                              AS avg_latency_ms,
                PERCENTILE_CONT(0.95) WITHIN GROUP (
                    ORDER BY latency_ms)::INT                     AS p95_latency_ms,
                COALESCE(SUM(total_tokens), 0)                    AS total_tokens
            FROM inference_logs
            """
        )
    total = row["total_calls"] or 0
    return {
        "total_calls": total,
        "errors": row["errors"] or 0,
        "error_rate": round((row["errors"] or 0) / total, 4) if total else 0,
        "avg_latency_ms": row["avg_latency_ms"] or 0,
        "p95_latency_ms": row["p95_latency_ms"] or 0,
        "total_tokens": row["total_tokens"] or 0,
    }


@app.get("/metrics/by_provider")
async def metrics_by_provider():
    """Per provider/model breakdown — powers the dashboard table."""
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT provider, model,
                   COUNT(*)                                 AS calls,
                   COUNT(*) FILTER (WHERE status='error')   AS errors,
                   AVG(latency_ms)::INT                     AS avg_latency_ms,
                   COALESCE(SUM(total_tokens),0)            AS total_tokens
            FROM inference_logs
            GROUP BY provider, model
            ORDER BY calls DESC
            """
        )
    return [dict(r) for r in rows]


@app.get("/metrics/timeseries")
async def metrics_timeseries():
    """Per-minute throughput + latency for the last hour — powers the charts."""
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT date_trunc('minute', created_at)        AS minute,
                   COUNT(*)                                AS calls,
                   COUNT(*) FILTER (WHERE status='error')  AS errors,
                   AVG(latency_ms)::INT                    AS avg_latency_ms
            FROM inference_logs
            WHERE created_at > now() - interval '1 hour'
            GROUP BY minute
            ORDER BY minute
            """
        )
    return [
        {
            "minute": r["minute"].isoformat(),
            "calls": r["calls"],
            "errors": r["errors"],
            "avg_latency_ms": r["avg_latency_ms"] or 0,
        }
        for r in rows
    ]


@app.get("/logs/recent")
async def logs_recent(limit: int = 50):
    """Most recent raw logs — powers the dashboard's live log table."""
    limit = max(1, min(limit, 200))
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, conversation_id, provider, model, latency_ms,
                   total_tokens, status, error_message,
                   input_preview, output_preview, created_at
            FROM inference_logs
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [
        {**dict(r),
         "id": str(r["id"]),
         "conversation_id": str(r["conversation_id"]) if r["conversation_id"] else None,
         "created_at": r["created_at"].isoformat()}
        for r in rows
    ]
