"""
providers.py
============

The multi-provider abstraction.

Each provider is an adapter that conforms to the `Provider` interface.
The wrapper code in wrapper.py only ever talks to this interface, so
adding a new provider = writing one new class. The chat-api picks a
provider by name at startup.

Every adapter returns a normalized dict:
    {
        "text": str,                 # the model's reply
        "prompt_tokens": int|None,
        "completion_tokens": int|None,
        "total_tokens": int|None,
    }

so the wrapper never has to know provider-specific response shapes.
"""
from __future__ import annotations
import os
from abc import ABC, abstractmethod
from typing import Iterator


class Provider(ABC):
    """Interface every provider adapter must implement."""

    #: short name stored in inference_logs.provider
    name: str = "base"
    #: the model identifier this adapter is configured with
    model: str = ""

    @abstractmethod
    def complete(self, messages: list[dict]) -> dict:
        """Non-streaming completion. `messages` is a list of
        {"role": "...", "content": "..."} dicts. Returns the
        normalized dict described in the module docstring."""

    @abstractmethod
    def stream(self, messages: list[dict]) -> Iterator[str]:
        """Streaming completion. Yields text chunks. After the
        generator is exhausted, `last_usage` holds token counts."""


# ----------------------------------------------------------------------
# Google Gemini adapter
# ----------------------------------------------------------------------
class GeminiProvider(Provider):
    """Adapter for Google's Gemini models via the google-genai SDK."""

    name = "google"

    def __init__(self, model: str = "gemini-2.5-flash", api_key: str | None = None):
        from google import genai  # imported lazily so SDK install is optional

        self.model = model
        self._client = genai.Client(api_key=api_key or os.environ["GEMINI_API_KEY"])
        self.last_usage: dict = {}

    @staticmethod
    def _to_gemini(messages: list[dict]) -> list[dict]:
        """Gemini uses 'user'/'model' roles and a 'parts' list."""
        out = []
        for m in messages:
            role = "model" if m["role"] == "assistant" else "user"
            out.append({"role": role, "parts": [{"text": m["content"]}]})
        return out

    def complete(self, messages: list[dict]) -> dict:
        resp = self._client.models.generate_content(
            model=self.model,
            contents=self._to_gemini(messages),
        )
        usage = getattr(resp, "usage_metadata", None)
        return {
            "text": resp.text or "",
            "prompt_tokens": getattr(usage, "prompt_token_count", None),
            "completion_tokens": getattr(usage, "candidates_token_count", None),
            "total_tokens": getattr(usage, "total_token_count", None),
        }

    def stream(self, messages: list[dict]) -> Iterator[str]:
        self.last_usage = {}
        stream = self._client.models.generate_content_stream(
            model=self.model,
            contents=self._to_gemini(messages),
        )
        for chunk in stream:
            if chunk.text:
                yield chunk.text
            usage = getattr(chunk, "usage_metadata", None)
            if usage:  # final chunks carry cumulative usage
                self.last_usage = {
                    "prompt_tokens": getattr(usage, "prompt_token_count", None),
                    "completion_tokens": getattr(usage, "candidates_token_count", None),
                    "total_tokens": getattr(usage, "total_token_count", None),
                }


# ----------------------------------------------------------------------
# OpenAI adapter — included to prove the abstraction is real.
# Not exercised in the demo unless OPENAI_API_KEY is set.
# ----------------------------------------------------------------------
class OpenAIProvider(Provider):
    """Adapter for OpenAI chat models via the openai SDK."""

    name = "openai"

    def __init__(self, model: str = "gpt-4o-mini", api_key: str | None = None):
        from openai import OpenAI

        self.model = model
        self._client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))
        self.last_usage = {}

    def complete(self, messages: list[dict]) -> dict:
        resp = self._client.chat.completions.create(
            model=self.model, messages=messages,
        )
        usage = resp.usage
        return {
            "text": resp.choices[0].message.content or "",
            "prompt_tokens": usage.prompt_tokens if usage else None,
            "completion_tokens": usage.completion_tokens if usage else None,
            "total_tokens": usage.total_tokens if usage else None,
        }

    def stream(self, messages: list[dict]) -> Iterator[str]:
        self.last_usage = {}
        stream = self._client.chat.completions.create(
            model=self.model, messages=messages, stream=True,
            stream_options={"include_usage": True},
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
            if chunk.usage:
                self.last_usage = {
                    "prompt_tokens": chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                    "total_tokens": chunk.usage.total_tokens,
                }


# Registry: maps a provider name -> adapter class.
# The chat-api reads PROVIDER from env and looks it up here.
PROVIDERS: dict[str, type[Provider]] = {
    "google": GeminiProvider,
    "openai": OpenAIProvider,
}


def register_provider(name: str, cls: type[Provider]) -> None:
    """Add a provider adapter to the registry at runtime.

    This keeps the registry OPEN for extension: a new provider (or a
    test-only fake) can be plugged in without editing this file. The
    chat-api calls this at startup if PROVIDER points to something not
    built in (see its bootstrap code)."""
    PROVIDERS[name] = cls


def get_provider(name: str, model: str | None = None) -> Provider:
    """Factory: build a configured provider adapter by name."""
    if name not in PROVIDERS:
        raise ValueError(f"Unknown provider '{name}'. Known: {list(PROVIDERS)}")
    cls = PROVIDERS[name]
    return cls(model=model) if model else cls()
