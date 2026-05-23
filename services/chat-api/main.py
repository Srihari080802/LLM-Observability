"""
main.py — Chat API
==================

The chatbot backend (deliverable #1). It:
    * creates / lists / resumes / cancels conversations
    * stores chat messages
    * rebuilds short conversational context (last N turns)
    * calls the LLM through the observability SDK (so every call is logged)
    * supports streaming responses (Server-Sent Events)

MULTI-TURN CONTEXT
------------------
"Short conversational context" = we fetch the last CONTEXT_WINDOW messages
for the conversation and pass them to the model. We cap it (a sliding
window) instead of sending the whole history so prompts stay small and
cheap. The cap is the tradeoff: very old turns drop out of context.
"""
from __future__ import annotations
import os
import sys
import json
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

sys.path.insert(0, "/app")
from observability_sdk import ObservedLLM          # noqa: E402
from observability_sdk.providers import get_provider, register_provider  # noqa: E402

import db  # noqa: E402

# --- configuration from environment ---
PROVIDER = os.environ.get("PROVIDER", "google")
MODEL = os.environ.get("MODEL")  # None -> provider default
INGESTION_URL = os.environ.get("INGESTION_URL", "http://ingestion-api:8001")
CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", "10"))

# Optional: PROVIDER=fake runs the whole stack with NO API key, using a
# canned-response provider. Handy for demos, CI, and local exploration.
if PROVIDER == "fake":
    import time as _t
    from observability_sdk.providers import Provider as _P

    class _FakeProvider(_P):
        name = "fake"

        def __init__(self, model=None):
            self.model = model or "fake-model-1"
            self.last_usage = {}

        def complete(self, messages):
            _t.sleep(0.05)
            last = messages[-1]["content"] if messages else ""
            return {"text": f"(demo reply) You said: {last}",
                    "prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17}

        def stream(self, messages):
            last = messages[-1]["content"] if messages else ""
            for w in f"(demo reply) You said: {last}".split():
                _t.sleep(0.04)
                yield w + " "
            self.last_usage = {"prompt_tokens": 10,
                               "completion_tokens": 7, "total_tokens": 17}

    register_provider("fake", _FakeProvider)

# Build the wrapped LLM once at import time.
# transport/redis_url default to the LOG_TRANSPORT / REDIS_URL env vars;
# passed explicitly here for clarity.
_llm = ObservedLLM(
    provider=get_provider(PROVIDER, MODEL),
    ingestion_url=INGESTION_URL,
    enable_redaction=True,
    transport=os.environ.get("LOG_TRANSPORT", "events"),
    redis_url=os.environ.get("REDIS_URL", "redis://redis:6379"),
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.init_pool()
    yield
    await db.close_pool()


app = FastAPI(title="LLM Observability — Chat API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"],
    allow_methods=["*"], allow_headers=["*"],
)


# ----------------------------------------------------------------------
# request/response models
# ----------------------------------------------------------------------
class NewConversation(BaseModel):
    title: str | None = None


class ChatRequest(BaseModel):
    message: str


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
async def _context(conn, conversation_id: str) -> list[dict]:
    """Fetch the last CONTEXT_WINDOW messages, oldest-first, as
    {"role","content"} dicts ready for the provider."""
    rows = await conn.fetch(
        """
        SELECT role, content FROM messages
        WHERE conversation_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        conversation_id, CONTEXT_WINDOW,
    )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def _require_active(conn, conversation_id: str):
    """Load a conversation and ensure it exists and is not cancelled."""
    row = await conn.fetchrow(
        "SELECT id, status FROM conversations WHERE id = $1", conversation_id
    )
    if row is None:
        raise HTTPException(404, "conversation not found")
    if row["status"] == "cancelled":
        raise HTTPException(409, "conversation is cancelled")
    return row


# ----------------------------------------------------------------------
# conversation management — powers the UI's list / resume / cancel
# ----------------------------------------------------------------------
@app.post("/conversations")
async def create_conversation(body: NewConversation):
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO conversations (title) VALUES ($1) RETURNING id, title, status, created_at",
            body.title or "New conversation",
        )
    return {"id": str(row["id"]), "title": row["title"],
            "status": row["status"], "created_at": row["created_at"].isoformat()}


@app.get("/conversations")
async def list_conversations():
    """List all conversations, newest first — for the UI sidebar."""
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.title, c.status, c.created_at, c.updated_at,
                   COUNT(m.id) AS message_count
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            GROUP BY c.id
            ORDER BY c.updated_at DESC
            """
        )
    return [
        {"id": str(r["id"]), "title": r["title"], "status": r["status"],
         "created_at": r["created_at"].isoformat(),
         "updated_at": r["updated_at"].isoformat(),
         "message_count": r["message_count"]}
        for r in rows
    ]


@app.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    """Full message history — used when the UI RESUMES a conversation."""
    async with db.pool().acquire() as conn:
        convo = await conn.fetchrow(
            "SELECT id, title, status FROM conversations WHERE id = $1",
            conversation_id,
        )
        if convo is None:
            raise HTTPException(404, "conversation not found")
        msgs = await conn.fetch(
            """
            SELECT id, role, content, created_at FROM messages
            WHERE conversation_id = $1 ORDER BY created_at
            """,
            conversation_id,
        )
    return {
        "id": str(convo["id"]), "title": convo["title"], "status": convo["status"],
        "messages": [
            {"id": str(m["id"]), "role": m["role"], "content": m["content"],
             "created_at": m["created_at"].isoformat()}
            for m in msgs
        ],
    }


@app.post("/conversations/{conversation_id}/cancel")
async def cancel_conversation(conversation_id: str):
    """CANCEL a conversation — sets status so the UI locks its input."""
    async with db.pool().acquire() as conn:
        result = await conn.execute(
            "UPDATE conversations SET status='cancelled', updated_at=now() WHERE id=$1",
            conversation_id,
        )
    if result.endswith("0"):
        raise HTTPException(404, "conversation not found")
    return {"id": conversation_id, "status": "cancelled"}


@app.post("/conversations/{conversation_id}/resume")
async def resume_conversation(conversation_id: str):
    """RESUME a cancelled conversation — flips status back to active."""
    async with db.pool().acquire() as conn:
        result = await conn.execute(
            "UPDATE conversations SET status='active', updated_at=now() WHERE id=$1",
            conversation_id,
        )
    if result.endswith("0"):
        raise HTTPException(404, "conversation not found")
    return {"id": conversation_id, "status": "active"}


# ----------------------------------------------------------------------
# chat — non-streaming
# ----------------------------------------------------------------------
@app.post("/conversations/{conversation_id}/chat")
async def chat(conversation_id: str, body: ChatRequest):
    """Send a message, get the full reply at once."""
    async with db.pool().acquire() as conn:
        await _require_active(conn, conversation_id)
        # store the user's message
        await conn.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES ($1,'user',$2)",
            conversation_id, body.message,
        )
        context = await _context(conn, conversation_id)

    # The SDK call is blocking (provider HTTP). Run it off the event loop
    # so we don't stall other requests.
    result = await asyncio.to_thread(_llm.chat, context, conversation_id)

    if result.status == "error":
        raise HTTPException(502, f"LLM error: {result.error_message}")

    async with db.pool().acquire() as conn:
        msg = await conn.fetchrow(
            "INSERT INTO messages (conversation_id, role, content) "
            "VALUES ($1,'assistant',$2) RETURNING id",
            conversation_id, result.text,
        )
        await conn.execute(
            "UPDATE conversations SET updated_at=now() WHERE id=$1", conversation_id
        )
    return {
        "message_id": str(msg["id"]),
        "reply": result.text,
        "latency_ms": result.latency_ms,
        "total_tokens": result.total_tokens,
    }


# ----------------------------------------------------------------------
# chat — streaming (Server-Sent Events)
# ----------------------------------------------------------------------
@app.post("/conversations/{conversation_id}/chat/stream")
async def chat_stream(conversation_id: str, body: ChatRequest):
    """Send a message, get the reply as a token stream (SSE).

    We collect the streamed text, persist the assistant message once the
    stream ends, then send a final 'done' event with metadata.
    """
    async with db.pool().acquire() as conn:
        await _require_active(conn, conversation_id)
        await conn.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES ($1,'user',$2)",
            conversation_id, body.message,
        )
        context = await _context(conn, conversation_id)

    async def event_stream():
        loop = asyncio.get_event_loop()
        # The provider stream is a sync generator; bridge it to async by
        # pulling chunks in a worker thread one at a time.
        gen = _llm.chat_stream(context, conversation_id)
        collected: list[str] = []

        def _next():
            try:
                return next(gen)
            except StopIteration:
                return None

        while True:
            chunk = await loop.run_in_executor(None, _next)
            if chunk is None:
                break
            collected.append(chunk)
            yield f"data: {json.dumps({'type': 'chunk', 'text': chunk})}\n\n"

        full = "".join(collected)
        async with db.pool().acquire() as conn:
            msg = await conn.fetchrow(
                "INSERT INTO messages (conversation_id, role, content) "
                "VALUES ($1,'assistant',$2) RETURNING id",
                conversation_id, full,
            )
            await conn.execute(
                "UPDATE conversations SET updated_at=now() WHERE id=$1",
                conversation_id,
            )
        yield f"data: {json.dumps({'type': 'done', 'message_id': str(msg['id'])})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
