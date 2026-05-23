"""
events.py
=========

Event-based logging transport.

This is the "event-based architecture" piece. Instead of the SDK calling
the ingestion API directly over HTTP, the SDK PUBLISHES a log event to a
Redis Stream. The ingestion service is a CONSUMER that reads from that
stream on its own schedule.

    SDK (producer) --XADD--> Redis Stream --XREADGROUP--> ingestion (consumer)

Why this is better than direct HTTP:
    * Decoupling. The SDK no longer needs to know the ingestion API's
      address or even that it exists. It only knows the broker.
    * Durability. If the ingestion service is down, events accumulate
      safely IN THE STREAM instead of being dropped. When ingestion
      restarts, it resumes consuming where it left off.
    * Back-pressure & batching. A consumer can read many events at once
      and batch its database writes.

Why Redis Streams (not Kafka):
    * A Redis Stream is a real append-only log with consumer groups and
      acknowledgements -- genuine event semantics -- but Redis is a
      single small container. Kafka would need a multi-container setup.
    * For production scale, Kafka is the natural swap; the producer/
      consumer code here would change very little.

Transport selection:
    The SDK supports BOTH transports. LOG_TRANSPORT=events uses this;
    LOG_TRANSPORT=http uses the original direct POST. This keeps the
    change backward-compatible and easy to demo either way.
"""
from __future__ import annotations
import json
import os

# The stream (think: topic) that all inference-log events are written to.
STREAM_KEY = os.environ.get("LOG_STREAM_KEY", "inference-logs")


class EventPublisher:
    """Publishes inference-log events to a Redis Stream.

    Lazily connects on first use so importing the SDK never requires a
    live Redis. Like the rest of the SDK, publishing failures are
    swallowed by the caller -- a lost log must never break chat.
    """

    def __init__(self, redis_url: str | None = None):
        self.redis_url = redis_url or os.environ.get(
            "REDIS_URL", "redis://localhost:6379"
        )
        self._client = None

    def _connect(self):
        """Create the Redis client on first use."""
        if self._client is None:
            import redis  # imported lazily
            self._client = redis.from_url(self.redis_url, decode_responses=True)
        return self._client

    def publish(self, payload: dict) -> None:
        """Append one log event to the stream.

        XADD is the Redis Streams 'append to log' command. The payload is
        JSON-encoded into a single field because stream entries are flat
        key/value maps. maxlen caps the stream so it cannot grow without
        bound if no consumer is running (approximate trimming, '~', is
        cheaper for Redis than exact trimming).
        """
        client = self._connect()
        client.xadd(
            STREAM_KEY,
            {"data": json.dumps(payload)},
            maxlen=100_000,
            approximate=True,
        )
