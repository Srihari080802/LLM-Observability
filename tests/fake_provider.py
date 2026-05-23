"""
fake_provider.py
================

A Provider implementation that returns canned responses instead of
calling a real LLM. Used by the test suite so the SDK, ingestion, and
chat flow can all be exercised without an API key or network access.

It conforms to the same Provider interface as GeminiProvider, which is
the whole point of the abstraction: tests swap the provider, nothing
else changes.
"""
import time
from typing import Iterator
from observability_sdk.providers import Provider


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, model: str = "fake-model-1", fail: bool = False):
        self.model = model
        self.fail = fail
        self.last_usage = {}

    def complete(self, messages: list[dict]) -> dict:
        if self.fail:
            raise RuntimeError("simulated provider failure")
        time.sleep(0.02)  # simulate a little latency
        last = messages[-1]["content"] if messages else ""
        return {
            "text": f"Echo: {last}",
            "prompt_tokens": 12,
            "completion_tokens": 8,
            "total_tokens": 20,
        }

    def stream(self, messages: list[dict]) -> Iterator[str]:
        if self.fail:
            raise RuntimeError("simulated provider failure")
        last = messages[-1]["content"] if messages else ""
        for word in f"Echo: {last}".split():
            time.sleep(0.01)
            yield word + " "
        self.last_usage = {"prompt_tokens": 12,
                           "completion_tokens": 8, "total_tokens": 20}
