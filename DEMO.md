# Demo

This file is the demo deliverable. Add your screenshots or a Loom link
below; the script and checklist here show what to capture.

## Running the demo

```bash
cp .env.example .env          # PROVIDER=fake works with no API key
docker compose up --build
```

Then open <http://localhost:3000>.

> For a real-LLM demo, set `PROVIDER=google` and `GEMINI_API_KEY` in
> `.env`. For a guaranteed-reliable recording, `PROVIDER=fake` is fine —
> it exercises the entire pipeline (SDK, event broker, ingestion,
> database, dashboard) with canned replies.

## Suggested 3-minute walkthrough

1. **Chat (multi-turn).** Create a conversation, ask a question, then
   ask a follow-up that depends on the first — show the model keeping
   context. Toggle "Stream response" to show streaming.
2. **Conversation management.** Create a second conversation (show the
   list). Cancel one (input locks). Resume it (input returns). Click
   between them to show resume reloading history.
3. **Dashboard.** Switch to the Dashboard tab. Show total calls, error
   rate, latency percentiles, the throughput chart, the per-provider
   table, and the recent-logs table.
4. **The event pipeline.** In a terminal:
   ```bash
   docker compose exec redis redis-cli XLEN inference-logs
   docker compose exec postgres psql -U llm -d llm_obs \
     -c "SELECT provider,status,latency_ms FROM inference_logs ORDER BY created_at DESC LIMIT 5;"
   ```
   Send a chat message, re-run — show a new event flowing through to a
   new database row.
5. **Durability (optional, strong).** `docker compose stop ingestion-api`,
   send a message, show the event waiting in the stream
   (`redis-cli XLEN`), then `docker compose start ingestion-api` and show
   the backlog drained into the database.

## Screenshots

<!-- Add screenshots here, e.g.: -->
<!-- ![Chat](demo/chat.png) -->
<!-- ![Dashboard](demo/dashboard.png) -->

## Loom / video link

<!-- Paste your Loom or video URL here -->
