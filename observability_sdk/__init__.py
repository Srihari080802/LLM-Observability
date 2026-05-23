"""
observability_sdk
=================

A lightweight wrapper around LLM provider calls that captures inference
metadata (model, provider, latency, tokens, status, timestamps, previews)
and ships it to an ingestion endpoint -- WITHOUT blocking the chat response.

Public API:
    ObservedLLM        -- the main wrapper class
    LLMResult          -- normalized result returned to the caller
    redact_pii         -- PII redaction helper (also used server-side)

Design goals:
    * Provider-agnostic   -> swap Gemini/OpenAI behind one interface
    * Non-blocking        -> logging failure must never break chat
    * Streaming-capable   -> can wrap streaming calls and still log totals
"""
from .wrapper import ObservedLLM, LLMResult
from .redaction import redact_pii

__all__ = ["ObservedLLM", "LLMResult", "redact_pii"]
__version__ = "0.1.0"
