"""
wrapper.py
==========

ObservedLLM — the lightweight wrapper that is the heart of this project.

It wraps a Provider adapter and, around every call, it:
    1. records a start timestamp
    2. runs the actual LLM call
    3. measures latency
    4. captures token usage + status (success/error)
    5. builds a log payload and SHIPS IT to the ingestion endpoint
       on a background thread — so the caller gets its answer
       immediately and logging can never slow down or break chat.

THE CENTRAL DESIGN DECISION
---------------------------
Logging is "fire-and-forget". `_ship_log` is submitted to a thread pool
and we never await its result on the request path. If the ingestion
service is down, the chat still works; the log is simply lost (and the
failure is printed). This is the deliberate failure-handling tradeoff:
we favor chat availability over guaranteed log delivery. The README's
"what I'd improve" section upgrades this to a durable local queue.
"""
from __future__ import annotations
import os
import time
import uuid
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterator

import httpx

from .providers import Provider
from .redaction import redact_pii

# A small shared pool. Logging is I/O-bound and infrequent relative to
# compute, so a handful of threads is plenty. daemon threads so the
# process can exit cleanly.
_LOG_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="obs-log")

_PREVIEW_CHARS = 500  # previews are truncated; full content lives in messages


@dataclass
class LLMResult:
    """Normalized result handed back to the caller of ObservedLLM."""
    text: str
    provider: str
    model: str
    latency_ms: int
    status: str                         # 'success' | 'error'
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    error_message: str | None = None
    log_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class ObservedLLM:
    """Wraps a Provider and emits an inference log for every call.

    Parameters
    ----------
    provider          : a Provider adapter (Gemini, OpenAI, ...)
    ingestion_url     : base URL of the ingestion service (http transport)
    enable_redaction  : redact PII in previews before they leave the process
    transport         : 'events' (publish to Redis Stream) or 'http'
                        (POST directly to the ingestion API). Default is
                        read from LOG_TRANSPORT, falling back to 'events'.
    redis_url         : Redis connection URL (events transport only)

    The two transports are interchangeable. 'events' is the event-based
    architecture: the SDK publishes to a broker and the ingestion service
    consumes independently. 'http' is the original direct call, kept so
    the system still works with no broker (and for easy comparison).
    """

    def __init__(
        self,
        provider: Provider,
        ingestion_url: str = "",
        enable_redaction: bool = True,
        transport: str | None = None,
        redis_url: str | None = None,
    ):
        self.provider = provider
        self.ingestion_url = ingestion_url.rstrip("/")
        self.enable_redaction = enable_redaction
        self.transport = transport or os.environ.get("LOG_TRANSPORT", "events")

        # For the event transport, build a publisher up front.
        self._publisher = None
        if self.transport == "events":
            from .events import EventPublisher
            self._publisher = EventPublisher(redis_url=redis_url)

    # ------------------------------------------------------------------
    # internal: build a preview string (truncate + optionally redact)
    # ------------------------------------------------------------------
    def _preview(self, text: str) -> str:
        preview = (text or "")[:_PREVIEW_CHARS]
        if self.enable_redaction:
            preview = redact_pii(preview)
        return preview

    # ------------------------------------------------------------------
    # internal: ship one log payload. Runs on a background thread.
    # Swallows all errors on purpose — a logging failure must never
    # surface to the chat caller. Branches on the configured transport.
    # ------------------------------------------------------------------
    def _ship_log(self, payload: dict) -> None:
        try:
            if self.transport == "events":
                # event-based: publish to the Redis Stream
                self._publisher.publish(payload)
            else:
                # http: POST directly to the ingestion API
                httpx.post(
                    f"{self.ingestion_url}/ingest",
                    json=payload,
                    timeout=5.0,
                )
        except Exception as exc:  # noqa: BLE001 - intentional broad catch
            # Last-resort visibility. In production this would increment
            # a metric / write to a local dead-letter file.
            print(f"[observability_sdk] log shipping failed: {exc}")

    def _emit(self, payload: dict) -> None:
        """Hand the payload to the background pool and return immediately."""
        _LOG_POOL.submit(self._ship_log, payload)

    # ------------------------------------------------------------------
    # public: non-streaming chat call
    # ------------------------------------------------------------------
    def chat(
        self,
        messages: list[dict],
        conversation_id: str | None = None,
    ) -> LLMResult:
        """Run a non-streaming completion and emit a log."""
        started = dt.datetime.now(dt.timezone.utc)
        t0 = time.perf_counter()
        status, error_message = "success", None
        out = {"text": "", "prompt_tokens": None,
               "completion_tokens": None, "total_tokens": None}

        try:
            out = self.provider.complete(messages)
        except Exception as exc:  # noqa: BLE001
            status, error_message = "error", str(exc)

        latency_ms = int((time.perf_counter() - t0) * 1000)

        result = LLMResult(
            text=out["text"],
            provider=self.provider.name,
            model=self.provider.model,
            latency_ms=latency_ms,
            status=status,
            prompt_tokens=out.get("prompt_tokens"),
            completion_tokens=out.get("completion_tokens"),
            total_tokens=out.get("total_tokens"),
            error_message=error_message,
        )

        # Last user message is the "input"; the reply is the "output".
        last_user = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        self._emit(self._build_payload(result, conversation_id,
                                       last_user, result.text, started))
        return result

    # ------------------------------------------------------------------
    # public: streaming chat call
    # ------------------------------------------------------------------
    def chat_stream(
        self,
        messages: list[dict],
        conversation_id: str | None = None,
    ) -> Iterator[str]:
        """Run a streaming completion. Yields text chunks to the caller.

        Latency and token usage are only known once the stream finishes,
        so the log is emitted AFTER the generator is exhausted. We still
        emit a log on error.
        """
        started = dt.datetime.now(dt.timezone.utc)
        t0 = time.perf_counter()
        chunks: list[str] = []
        status, error_message = "success", None

        try:
            for piece in self.provider.stream(messages):
                chunks.append(piece)
                yield piece
        except Exception as exc:  # noqa: BLE001
            status, error_message = "error", str(exc)

        latency_ms = int((time.perf_counter() - t0) * 1000)
        usage = getattr(self.provider, "last_usage", {}) or {}
        full_text = "".join(chunks)

        result = LLMResult(
            text=full_text,
            provider=self.provider.name,
            model=self.provider.model,
            latency_ms=latency_ms,
            status=status,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            error_message=error_message,
        )
        last_user = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        self._emit(self._build_payload(result, conversation_id,
                                       last_user, full_text, started))

    # ------------------------------------------------------------------
    # internal: assemble the JSON log payload sent to /ingest
    # ------------------------------------------------------------------
    def _build_payload(
        self,
        result: LLMResult,
        conversation_id: str | None,
        input_text: str,
        output_text: str,
        started: dt.datetime,
    ) -> dict:
        return {
            "log_id": result.log_id,
            "conversation_id": conversation_id,
            "provider": result.provider,
            "model": result.model,
            "latency_ms": result.latency_ms,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
            "status": result.status,
            "error_message": result.error_message,
            "input_preview": self._preview(input_text),
            "output_preview": self._preview(output_text),
            "request_started_at": started.isoformat(),
        }
