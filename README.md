# LLM Observability — Inference Logging & Ingestion System

A lightweight, end-to-end system for an LLM application: a multi-turn
chatbot, a thin SDK that captures inference metadata for every model
call, an ingestion service that validates and stores those logs, and a
dashboard to view latency / throughput / error metrics.

Built as a take-home project. The guiding principle throughout is
**observability must never degrade the user-facing chat** — logging is
fire-and-forget and isolated from the request path.

---

## Table of contents

1. [What you get](#what-you-get)
2. [Quick start (Docker — one command)](#quick-start-docker--one-command)
3. [Running locally without Docker](#running-locally-without-docker)
4. [Architecture overview](#architecture-overview)
5. [Ingestion flow](#ingestion-flow)
6. [Logging strategy](#logging-strategy)
7. [Schema design decisions](#schema-design-decisions)
8. [Tradeoffs made](#tradeoffs-made)
9. [Scaling considerations](#scaling-considerations)
10. [Failure handling assumptions](#failure-handling-assumptions)
11. [What I would improve with more time](#what-i-would-improve-with-more-time)
12. [Project layout](#project-layout)

---

## What you get

| Component        | Folder                    | Role |
|------------------|---------------------------|------|
| Chatbot backend  | `services/chat-api`       | Multi-turn chat, conversation management, streaming |
| Observability SDK| `observability_sdk`       | Wraps LLM calls, captures metadata, publishes log events |
| Event broker     | Redis (Streams)           | Decouples the SDK from ingestion |
| Ingestion API    | `services/ingestion-api`  | Stream consumer: validates, redacts PII, stores, serves metrics |
| Database         | `db/schema.sql`           | PostgreSQL: conversations, messages, inference_logs |
| Frontend         | `frontend/index.html`     | Single-file React UI: chat + dashboard |
| Kubernetes       | `k8s/`                    | Manifests + deploy script for a self-hosted cluster |

Bonus features included: multi-provider support (Gemini + OpenAI),
streaming responses (SSE), a latency/throughput/error dashboard,
Docker Compose one-command setup, **event-based architecture** (Redis
Streams between the SDK and ingestion), PII redaction, Kubernetes
deployment, and conversation cancel / list / resume in the UI.

---

## Quick start (Docker — one command)

**Prerequisites:** Docker and Docker Compose.

1. Copy the environment template and add your key:

   ```bash
   cp .env.example .env
   # edit .env — set GEMINI_API_KEY=your-key
   ```

2. Bring up the whole stack:

   ```bash
   docker compose up --build
   ```

3. Open the UI: <http://localhost:3000>

   - Chat API:      <http://localhost:8000>
   - Ingestion API: <http://localhost:8001>
   - Postgres:      `localhost:5432` (user `llm`, password `llm`)

**No API key?** Set `PROVIDER=fake` in `.env`. The stack then runs with
a canned-response provider so you can explore the full pipeline,
dashboard included, without calling a real LLM.

---

## Running locally without Docker

Useful for development in VS Code.

1. **Start PostgreSQL** (any local instance) and load the schema:

   ```bash
   createdb llm_obs
   psql llm_obs -f db/schema.sql
   ```

2. **Install dependencies** (a virtualenv is recommended):

   ```bash
   pip install -r services/chat-api/requirements.txt
   pip install -r services/ingestion-api/requirements.txt
   ```

3. **Start the ingestion API** (terminal 1):

   ```bash
   cd services/ingestion-api
   DB_HOST=localhost DB_USER=llm DB_PASSWORD=llm DB_NAME=llm_obs \
   PYTHONPATH=$(pwd)/../.. \
   uvicorn main:app --port 8001
   ```

4. **Start the chat API** (terminal 2):

   ```bash
   cd services/chat-api
   DB_HOST=localhost DB_USER=llm DB_PASSWORD=llm DB_NAME=llm_obs \
   INGESTION_URL=http://localhost:8001 \
   PROVIDER=google GEMINI_API_KEY=your-key \
   PYTHONPATH=$(pwd)/../.. \
   uvicorn main:app --port 8000
   ```

5. **Open the frontend.** Open `frontend/index.html` directly in a
   browser, or serve it: `python -m http.server 3000 -d frontend`.

6. **Run the tests:**

   ```bash
   pip install pytest
   pytest tests/
   ```

> `PYTHONPATH` points at the repo root so each service can import the
> `observability_sdk` package. In Docker this is handled by copying the
> SDK into each image.

---

## Deploying to Kubernetes (self-hosted)

The `k8s/` folder deploys the whole stack to a single-node cluster
(minikube or kind). Images are built straight into the cluster's docker
daemon, so no image registry is needed.

```bash
bash k8s/deploy.sh
```

This starts minikube, enables the ingress addon, builds the three
service images, applies `k8s/manifests.yaml` (16 resources: namespace,
secret, config, Postgres, Redis, the three services, and an ingress),
and waits for every deployment to become ready. When it finishes:

```bash
minikube service frontend -n llm-obs    # opens the UI
kubectl -n llm-obs get pods             # see all pods
kubectl delete namespace llm-obs        # tear down
```

For real Gemini on the cluster, set `PROVIDER: "google"` in the
`llm-config` ConfigMap and put your key in the `llm-secrets` Secret
before applying. Default is `PROVIDER: fake`, so it runs with no key.

---

## Architecture overview

```
  ┌───────────┐   HTTP    ┌──────────────┐   wraps   ┌──────────────┐
  │  Frontend │ ────────> │   Chat API   │ ────────> │ Observability│
  │ (React UI)│ <──────── │  (FastAPI)   │ <──────── │     SDK      │
  └───────────┘  SSE      └──────┬───────┘           └──────┬───────┘
                                 │                          │
                          chat messages          publish log EVENT
                                 │                  (background thread)
                                 v                          v
                                 │                  ┌────────────────┐
                                 │                  │  Redis Stream  │
                                 │                  │  (event broker)│
                                 │                  └────────┬───────┘
                                 │                           │ consume
                                 v                           v
                          ┌──────────────────────────────────────┐
                          │       Ingestion API (FastAPI)         │
                          │  stream consumer + HTTP /ingest        │
                          │  validate → redact PII → store         │
                          └──────────────────┬────────────────────┘
                                             │
                                             v
                                    ┌──────────────────┐
                                    │    PostgreSQL    │
                                    │  conversations   │
                                    │  messages        │
                                    │  inference_logs  │
                                    └────────┬─────────┘
                                             │ metrics queries
                          ┌──────────────────┘
                   ┌──────────────┐
                   │  Dashboard   │  (a tab in the same frontend)
                   └──────────────┘
```

The SDK and the ingestion service are decoupled by a **Redis Stream**
acting as an event broker (the event-based architecture bonus). The SDK
*publishes* log events; the ingestion service *consumes* them. They
never call each other directly. A direct-HTTP transport is also kept
(`LOG_TRANSPORT=http`) for environments with no broker.

Three independently deployable services plus a database. The chat API
and ingestion API share nothing except the database and the SDK's log
contract, so they scale and fail independently.

---

## Ingestion flow

1. The user sends a message; the chat API stores it and rebuilds the
   short conversational context (last N turns).
2. The chat API calls the LLM **through the SDK wrapper**.
3. The wrapper records start time, runs the provider call, measures
   latency, and captures token usage + success/error status.
4. The wrapper assembles a log payload and submits it to a background
   thread pool. **The chat response returns to the user immediately —
   it does not wait for logging.**
5. The background thread **publishes the payload as an event to a Redis
   Stream** (`XADD`). With `LOG_TRANSPORT=http` it instead POSTs directly
   to `/ingest` — same payload, same downstream handling.
6. The ingestion service runs a **background consumer** that reads events
   from the stream (`XREADGROUP` on a consumer group), validates each
   with Pydantic, redacts PII server-side, inserts a row into
   `inference_logs`, and **acknowledges** the event (`XACK`) only after a
   successful store — giving at-least-once delivery.
7. The dashboard periodically queries the ingestion API's `/metrics/*`
   endpoints, which aggregate `inference_logs` with SQL.

---

## Logging strategy

- **Event-based, decoupled.** The SDK publishes log events to a Redis
  Stream; the ingestion service consumes them independently. Producer and
  consumer share only the broker — neither needs the other to be up.
- **Fire-and-forget on the chat path.** Publishing happens on a
  `ThreadPoolExecutor`, never awaited on the request path. A slow or dead
  broker cannot slow down or break chat.
- **Durable across outages.** If the ingestion service is down, events
  accumulate safely in the stream and are processed on recovery. (Tested:
  publish with the consumer stopped, restart it, all events land.) This
  is the key gain over the direct-HTTP transport, where a dropped log was
  lost forever.
- **At-least-once delivery.** A consumed event is `XACK`-ed only after it
  is stored. A crash before the ack means the event is redelivered.
- **Previews, not full payloads.** The log stores truncated, PII-redacted
  previews of input/output. Full message content lives in the `messages`
  table. This keeps `inference_logs` lean and limits sensitive-data
  exposure in the observability store.
- **Two-layer PII redaction.** Redaction runs in the SDK (before data
  leaves the process) and again in the ingestion service (the real
  guarantee, enforced regardless of which SDK version sent the log).
- **Provider-agnostic.** The SDK logs `provider` and `model` on every
  call, so multi-provider traffic is comparable in one dashboard.

---

## Schema design decisions

Three tables: `conversations`, `messages`, `inference_logs`.

**Why `inference_logs` is separate from `messages`.** This is the central
schema decision. They are created together but kept apart because:

- *Different read patterns.* `messages` are read sequentially to rebuild
  conversation context. `inference_logs` are read as aggregates (AVG,
  COUNT, percentiles) for dashboards. Merging them would bloat the hot
  chat-read path with operational columns it never needs.
- *Different lifecycle.* Chat content may warrant long user-facing
  retention; operational logs can be aggregated and aged out.
- *Different write owners.* `messages` are written by the chat API;
  `inference_logs` by the ingestion API. Separation keeps ownership clean.

**Indexes are chosen to match queries.**

- `messages (conversation_id, created_at)` — a composite index that
  serves both the filter (*this conversation*) and the sort (*oldest
  first*) in a single index scan.
- `inference_logs (created_at)` — dashboards query by time window.
- `inference_logs (provider, model)` and `(status)` — dashboards slice
  by provider/model and filter errors.

**`conversations.status`** (`active` / `cancelled`) is a deliberate
column, not a derived value — it directly powers the UI's cancel/resume
feature and is cheap to flip.

**Foreign keys are nullable where reality demands it.**
`inference_logs.conversation_id` can be NULL: an LLM call may fail before
a conversation row is confirmed, and observability data is best-effort —
we store the log rather than reject it.

---

## Tradeoffs made

| Decision | Chosen | Gave up | Why |
|----------|--------|---------|-----|
| Log transport | Event broker (Redis Stream) | Direct synchronous call | Decouples producer/consumer; survives ingestion outages |
| Chat-path logging | Fire-and-forget background thread | Guaranteed publish | Chat must never block on logging |
| Broker choice | Redis Streams | Kafka | Real event semantics in one small container; Kafka is the production swap |
| Frontend | Single-file React via CDN | Build tooling, code-splitting | Zero-setup, one readable file |
| PII redaction | Regex patterns | ML-based NER coverage | Predictable, fast, explainable |
| Context window | Fixed last-N turns | Full-history recall | Bounded prompt size and cost |
| Streaming token usage | Reported after stream ends | Per-chunk usage | Providers only send usage at the end |

---

## Scaling considerations

- **Stateless services.** Chat API and ingestion API hold no local state,
  so both scale horizontally behind a load balancer — covered by the k8s
  manifests (`replicas` on the Deployments).
- **Connection pooling.** Each service uses an asyncpg pool with a capped
  max size, so a traffic spike cannot exhaust Postgres connection slots.
- **Ingestion absorbs spikes via the broker.** Every LLM call produces
  one log event. The Redis Stream buffers bursts, and the consumer drains
  at its own pace; multiple ingestion replicas can share one consumer
  group to process the stream in parallel. Batching the consumer's
  inserts is the next throughput step (see "what I would improve").
- **Dashboard queries.** `/metrics/*` aggregate over `inference_logs`.
  At scale these should read from a pre-aggregated rollup table (per
  minute / per hour) instead of scanning raw rows.
- **Log table growth.** `inference_logs` grows fastest. Time-based
  partitioning plus a retention policy keeps it manageable.

---

## Failure handling assumptions

- **Ingestion down → events wait in the stream, chat is unaffected.**
  The SDK publishes to Redis regardless; the consumer processes the
  backlog when it restarts. Nothing is lost (verified by test).
- **Broker (Redis) down → publish fails, chat is unaffected.** The SDK
  catches the failure on its background thread. In this case the log
  *is* lost — Redis being down is the remaining single point of failure
  for logging, mitigated in production by Redis replication.
- **Provider error → captured, not hidden.** A failed LLM call is logged
  with `status='error'` and the error message, then surfaced to the
  caller. Errors are first-class data in the dashboard.
- **Bad event / payload → dropped, consumer continues.** Pydantic
  rejects malformed payloads; the consumer logs and `XACK`s the bad
  event so it does not redeliver forever (a production system routes it
  to a dead-letter stream instead).
- **Consumer crashes mid-event → event is redelivered.** Because `XACK`
  happens only after a successful store, an un-acked event is reprocessed
  — at-least-once delivery. The insert is idempotent (`ON CONFLICT DO
  NOTHING` on the log id), so redelivery does not create duplicates.
- **Unknown `conversation_id` on a log → stored as NULL**, not rejected.
- **Streaming interrupted → a log is still emitted** with whatever was
  collected and an error status.

---

## What I would improve with more time

1. **Idempotent insert hardening.** The log insert already uses
   `ON CONFLICT DO NOTHING`; pairing this with a dead-letter stream for
   poison events would make the consumer fully production-grade.
2. **Batched consumer writes.** The consumer reads up to 20 events per
   loop but inserts them one at a time; a single multi-row insert per
   batch would cut database round-trips significantly.
3. **Kafka for very high volume.** Redis Streams is the right scope here;
   at large scale Kafka adds partitioning and longer retention. The
   producer/consumer code would change very little.
4. **Better PII redaction.** Add named-entity recognition for free-form
   PII (names, addresses) that regex cannot catch.
5. **Pre-aggregated metrics.** A rollup table refreshed on a schedule so
   the dashboard never scans raw logs.
6. **Auth.** API keys / JWT on all endpoints; none today.
7. **Tracing.** Propagate a trace ID from the UI through every service.
8. **More tests.** Integration tests for the chat and ingestion APIs;
   currently the redaction module and the event pipeline are tested.

---

## Project layout

```
llm-observability/
├── docker-compose.yml          one-command stack (5 services)
├── .env.example                environment template
├── db/
│   └── schema.sql              PostgreSQL schema (3 tables)
├── observability_sdk/          the lightweight SDK / wrapper
│   ├── __init__.py
│   ├── wrapper.py              ObservedLLM — times calls, emits log events
│   ├── providers.py            multi-provider abstraction
│   ├── events.py               Redis Stream event publisher
│   └── redaction.py            PII redaction
├── services/
│   ├── chat-api/               chatbot backend
│   │   ├── main.py
│   │   └── db.py
│   └── ingestion-api/          log ingestion + metrics
│       ├── main.py             HTTP endpoint + Redis Stream consumer
│       ├── models.py           Pydantic validation models
│       └── db.py
├── frontend/
│   └── index.html              single-file React UI (chat + dashboard)
├── k8s/
│   ├── manifests.yaml          all 16 Kubernetes resources
│   └── deploy.sh               one-command minikube deploy
├── tests/
│   ├── test_redaction.py       PII redaction unit tests
│   ├── test_event_pipeline.py  event publish/round-trip tests
│   └── fake_provider.py        no-API-key provider for tests
└── ARCHITECTURE.md             extended architecture notes
```
