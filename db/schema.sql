-- ============================================================
-- LLM Observability — Database Schema
-- ============================================================
-- Three tables, each with a deliberately distinct purpose:
--
--   conversations  -> session-level state (powers list/resume/cancel)
--   messages       -> chat content, one row per turn (rebuilds context)
--   inference_logs -> operational telemetry, one row per LLM API call
--
-- KEY DESIGN DECISION
-- -------------------
-- messages and inference_logs are SEPARATE tables even though they are
-- created together. Reasons:
--   1. Different read patterns. messages are read sequentially to rebuild
--      conversation context. inference_logs are aggregated (AVG, COUNT,
--      percentiles) for dashboards. Mixing them would bloat the hot
--      chat-read path with columns it never needs.
--   2. Different lifecycle. Chat content may need long retention for the
--      user; operational logs can be aggregated and aged out.
--   3. Different write owners. messages are written by the chat-api.
--      inference_logs are written by the ingestion-api. Separation keeps
--      ownership clean.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- for gen_random_uuid()

-- ------------------------------------------------------------
-- conversations: one row per chat session
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title       TEXT NOT NULL DEFAULT 'New conversation',
    -- status drives the UI's cancel/resume feature.
    --   active    -> normal, can receive messages
    --   cancelled -> user cancelled; frontend hides input
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'cancelled')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- messages: one row per turn in a conversation
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Composite index: the core query is
--   "give me messages for conversation X, oldest first"
-- A single index on (conversation_id, created_at) serves both the
-- filter and the sort, so Postgres never has to sort separately.
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages (conversation_id, created_at);

-- ------------------------------------------------------------
-- inference_logs: one row per LLM API call (the observability data)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inference_logs (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id    UUID REFERENCES conversations(id) ON DELETE CASCADE,
    -- message_id may be NULL when a call fails before a message is stored.
    message_id         UUID REFERENCES messages(id) ON DELETE SET NULL,

    -- --- identity of the call ---
    provider           TEXT NOT NULL,            -- 'google', 'openai', ...
    model              TEXT NOT NULL,            -- 'gemini-2.0-flash', ...

    -- --- performance ---
    latency_ms         INTEGER,                  -- wall-clock of the API call
    prompt_tokens      INTEGER,
    completion_tokens  INTEGER,
    total_tokens       INTEGER,

    -- --- outcome ---
    status             TEXT NOT NULL             -- 'success' | 'error'
                       CHECK (status IN ('success', 'error')),
    error_message      TEXT,                     -- populated when status='error'

    -- --- previews (truncated + PII-redacted by the ingestion service) ---
    -- We store PREVIEWS, not full payloads, to keep this table lean and
    -- to reduce exposure of sensitive content. Full content lives in
    -- the messages table.
    input_preview      TEXT,
    output_preview     TEXT,

    -- --- timestamps ---
    request_started_at TIMESTAMPTZ,              -- when the SDK began the call
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()  -- when ingested
);

-- Dashboards query logs by time window, so index created_at.
CREATE INDEX IF NOT EXISTS idx_logs_created_at
    ON inference_logs (created_at);

-- Dashboards also slice by provider/model and filter errors.
CREATE INDEX IF NOT EXISTS idx_logs_provider_model
    ON inference_logs (provider, model);
CREATE INDEX IF NOT EXISTS idx_logs_status
    ON inference_logs (status);
