# Architecture Notes

Companion to the README. This document focuses on the four things the
brief asks to be explained: ingestion flow, logging strategy, scaling
considerations, and failure handling assumptions.

---

## System shape


Three stateless services, an event broker, and one database:

- **chat-api** — the chatbot. Owns conversations and messages.
- **ingestion-api** — owns inference logs and the metrics endpoints; runs
  a background consumer of the log-event stream.
- **redis** — the event broker (Redis Streams) between the SDK and
  ingestion.
- **frontend** — a single-file React UI with a chat tab and a dashboard tab.
- **PostgreSQL** — the single source of truth.

The **observability_sdk** is not a service; it is a library imported by
the chat-api. It is the seam where every LLM call is intercepted.

The key structural property: chat-api and ingestion-api share *nothing*
except the database and the event broker. They are built, deployed,
scaled, and fail independently. The SDK does not even know the ingestion
service exists — it only knows the broker.

---

## Ingestion flow (step by step)

```
user types ──▶ chat-api: store user message
            ──▶ chat-api: SELECT last N messages  (rebuild context)
            ──▶ SDK.chat(context)
                   │  t0 = now
                   │  provider.complete(context)   ◀── the LLM call
                   │  latency = now - t0
                   │  build log payload
                   │  POOL.submit(publish)  ───────────┐  (returns now)
            ◀──── reply returned to user               │
                                                        ▼  background thread
                                          XADD ──▶ Redis Stream
                                                        │
                                       (decoupled — consumer reads
                                        independently, at its own pace)
                                                        │
                                          ingestion-api consumer:
                                          XREADGROUP from the stream
                                                        │
                                          Pydantic validation
                                                        │
                                          redact_pii(previews)
                                                        │
                                          INSERT INTO inference_logs
                                                        │
                                          XACK  (only after a good store)
```

Two important splits. First, after `POOL.submit` the reply path and the
logging path diverge: the user gets the answer on the fast path; the log
is published on a separate thread. Second, the Redis Stream fully
decouples the producer (SDK) from the consumer (ingestion) — they run on
independent schedules and neither needs the other to be alive.

A direct-HTTP transport (`LOG_TRANSPORT=http`, `POST /ingest`) is kept
for environments with no broker; it funnels into the same `store_log()`.

The ingestion-api also exposes read endpoints (`/metrics/summary`,
`/metrics/by_provider`, `/metrics/timeseries`, `/logs/recent`) that
aggregate `inference_logs` with SQL. The dashboard polls these every
five seconds — "near real-time" without websockets.

---

## Logging strategy

**What is captured per call:** provider, model, latency in ms, prompt /
completion / total tokens, status (success or error), error message if
any, truncated and PII-redacted input/output previews, the request start
timestamp, and the conversation id.

**Why an event broker.** If the SDK called ingestion directly and awaited
it, every chat response would be as slow as the ingestion service, and an
ingestion outage would become a chat outage. Publishing a log *event* to
a Redis Stream instead means: (a) the chat path never waits, (b) the SDK
and ingestion are decoupled, and (c) if ingestion is down, events wait
safely in the stream and are processed on recovery rather than being
lost. The last point is the real win — it was verified by stopping the
consumer, publishing events, and confirming they all landed on restart.

**Why fire-and-forget on the chat path.** Even the publish call runs on a
thread pool and is never awaited. A slow broker cannot slow chat. The
residual risk is the broker itself being down, in which case that one
publish is lost — mitigated in production by Redis replication.

**At-least-once delivery.** The consumer reads from a Redis consumer
group and `XACK`s an event only after it is successfully stored. A crash
before the ack means the event is redelivered. The insert is idempotent
(`ON CONFLICT DO NOTHING` on the log id), so redelivery is safe.

**Why previews instead of full payloads.** Full prompt and completion
text already live in the `messages` table. Duplicating them into
`inference_logs` would double storage and double the sensitive-data
surface. Previews are capped at 500 characters and redacted.

**Why redaction runs twice.** Once in the SDK (so raw PII never leaves
the chat process) and once in the ingestion-api (so redaction is
guaranteed even if a log arrives from an old or untrusted SDK). The
server-side pass is the real guarantee; the client-side pass is defense
in depth.

**Provider tagging.** Every log row records which provider and model
served the call, so a multi-provider deployment produces one comparable
dataset and the dashboard can break latency/error down per provider.

---

## Scaling considerations

**Stateless app tier.** chat-api and ingestion-api keep no in-process
state beyond a DB connection pool, so they scale by adding replicas. The
k8s manifests run two replicas of each; a HorizontalPodAutoscaler is the
natural next step.

**Connection pooling.** Each replica holds an asyncpg pool with a capped
maximum. This bounds total connections to Postgres so a spike in traffic
cannot exhaust the database's connection slots — the pool queues instead.

**Ingestion is the pressure point.** There is one log insert per LLM
call. At low and moderate volume a direct insert is fine. At high volume
the fix is to place a durable queue (Kafka or Redis Streams) between the
SDK and the database: the SDK produces to the queue, a consumer batches
inserts. This also smooths spikes and adds back-pressure.

**Read-side scaling.** The dashboard endpoints aggregate raw rows. As
`inference_logs` grows, those scans get slower. The fix is a rollup
table (calls, errors, latency percentiles per minute and per hour)
refreshed on a schedule; the dashboard then reads small pre-aggregated
rows instead of scanning history.

**Storage growth.** `inference_logs` grows fastest. Time-based table
partitioning (e.g. monthly partitions) plus a retention policy that
drops or archives old partitions keeps query plans and disk in check.

**Database as the single bottleneck.** One Postgres primary serves both
services. Scaling further means read replicas for the dashboard queries,
and eventually separating the two services onto separate databases since
they share no tables that need joining.

---

## Failure handling assumptions

The design assumes failures are normal and sorts them by who must not be
affected — the chatting user always wins.

| Failure | Behaviour | Rationale |
|---------|-----------|-----------|
| Ingestion-api is down | Events accumulate in the Redis Stream; consumer drains them on restart | Decoupling via the broker means downtime delays, not loses, logs |
| Redis (broker) is down | SDK's publish fails on its background thread; that one log is lost; chat continues | Remaining SPOF for logging; mitigated by Redis replication in production |
| LLM provider errors | Call is logged with `status='error'` + message, error surfaced to caller | Errors are first-class observable data |
| Malformed event/payload | Pydantic rejects it; consumer logs and `XACK`s so it does not redeliver forever | A poison event must not stall the consumer |
| Consumer crashes mid-event | Event is un-acked, so Redis redelivers it; idempotent insert prevents duplicates | At-least-once delivery |
| Log references an unknown conversation | `conversation_id` stored as NULL, row still inserted | Best-effort observability beats rejecting data |
| Streaming response interrupted | A log is still emitted with the partial text and error status | A partial call is still worth observing |
| Chat-api or ingestion-api replica crashes | Kubernetes restarts it; other replicas serve traffic meanwhile | Stateless + multi-replica = no single point of failure in the app tier |
| Postgres is down | Both services fail their readiness probes; chat is unavailable | Accepted single point of failure; HA Postgres is out of scope |

**What is explicitly NOT handled** (and called out honestly): there is
no authentication, no dead-letter stream for poison events, and no
distributed tracing. Each is listed in the README's "what I would
improve" section.
