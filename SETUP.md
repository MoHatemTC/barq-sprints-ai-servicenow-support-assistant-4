# S3.6 — Running the Integrated Stack

Setup from a clean checkout of this repo.

## Prerequisites
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (with WSL2 backend on Windows)
- A real embedding credential: either a `GEMINI_API_KEY`, or `LITELLM_BASE_URL`/`LITELLM_API_KEY`
  for the Sprints LiteLLM proxy
- A Qdrant Cloud cluster URL + API key (or use the local `qdrant` service in
  `docker-compose.yml` instead — see `.env`'s `QDRANT_URL`)
- A free [Langfuse](https://cloud.langfuse.com) project (public/secret keys)
- A ServiceNow PDI with the 5 scoped AI fields already added to Incident (`ai_status`,
  `ai_suggested_response`, `ai_confidence`, `human_review_required`, `ai_processed`) — see
  `AI_FIELD_PREFIX` below
- A LiteLLM-proxy-compatible key for the agent's LLM calls (`OPENAI_API_KEY`, same value as
  `LITELLM_API_KEY` — the agent's client and the retrieval/benchmark scripts read different
  env var names for the same underlying key)

## 1. Clone and configure
```bash
git clone <this repo>
cd barq-sprints-ai-servicenow-support-assistant-4
cp .env.example .env
```
Fill in `.env`: `QDRANT_URL`/`QDRANT_API_KEY`, embedding credentials, and Langfuse keys should
be real. `AI_FIELD_PREFIX` must match the scoped field prefix on your specific ServiceNow
instance (System Definition > Dictionary, filter Table=incident — every intern's PDI has a
different scope number). `OPENAI_API_KEY` must be set to the same LiteLLM proxy key as
`LITELLM_API_KEY` (the agent's LLM client reads a different env var name than the retrieval
scripts do for the same underlying credential). `INCIDENT_SIGNING_SECRET` (and
`KB_SIGNING_SECRET` for the KB sync path) are the current signing secrets — do not use the old
`SERVICENOW_WEBHOOK_SECRET`, which no longer matches the live receiver contract.

## 2. Install dependencies
```bash
uv lock
uv sync
```

## 3. Seed the knowledge base (first time only)
If your Qdrant collection is empty, populate it with the fixture KB articles matching the
benchmark dataset:
```bash
uv run python benchmark/seed_fixtures.py
```

## 4. Bring up the stack
```bash
docker compose up --build
```
This builds and starts four containers: `api` (FastAPI, port 8000), `worker` (Celery), `redis`,
and `qdrant`. First build takes a few minutes; subsequent builds are cached.

Verify:
```bash
docker compose ps            # all 4 should show Up (redis: healthy)
curl localhost:8000/health   # {"status": "ok"}
```

## 5. Run an end-to-end incident

**Endpoint:** `POST /api/v1/events/servicenow`

**Payload shape:**
```json
{
  "event_id": "<any unique string, e.g. a GUID — this is the dedup key>",
  "incident_sys_id": "<a real Incident record's sys_id on your PDI>",
  "number": "INC00xxxxx",
  "short_description": "...",
  "description": "..."
}
```

The receiver verifies an HMAC-SHA256 signature computed over the **exact raw request body**,
sent in the `X-Signature` header (lowercase hex digest, no `sha256=` prefix). The signing
secret is `INCIDENT_SIGNING_SECRET` from `.env` — must match on both the sender and receiver
side.

A PowerShell helper script, `test_webhook.ps1`, builds the payload, computes the signature,
and sends the request in one step — the reliable way to test this locally (a native `curl.exe`
one-liner reliably mangles the JSON body's quoting on Windows; don't fight it, use the script):
```powershell
.\test_webhook.ps1
```
Edit the `$sysId` variable inside it to point at a real Incident sys_id first.

**Dedup behavior:** the dedup key is `evt:{event_id}` in Redis (24h expiry). Sending the same
`event_id` again returns `202 Accepted` with an **empty response body** — not a `"duplicate"`
JSON payload — since the request was already fully handled the first time.

Watch `docker compose logs worker -f` to see the real agent run: fetch (real Table API GET) ->
ReAct loop over searchKB/addWorkNote (real Qdrant retrieval) -> terminal suggestAnswer or
requestHR -> real writeback (one atomic PATCH to the scoped AI fields on the incident, matching
`AI_FIELD_PREFIX`).

## 6. Run the benchmark harness
```bash
uv run python benchmark/pipeline_benchmark.py --real
```
Outputs `benchmark/PIPELINE_RESULTS.md` (per-incident table) and
`benchmark/pipeline_benchmark_results.json`.

## 7. Send tracing demo data
```bash
uv run python benchmark/traced_demo_run.py
```
Sends a couple of traced incident runs to Langfuse. Check your project's **Traces** tab —
each run should show one trace with 4 nested spans (`incident-fetch`, `kb-retrieval`,
`agent-decision`, `servicenow-writeback`). This same 4-span structure now also appears on
real live runs through the `/api/v1/events/servicenow` path (Step 5) — the production
`run_agent_loop()` wraps its real LLM call in the `agent-decision` span, not just this
standalone demo script.

## Known stubs / open items (see individual file headers for details)
- **KB content**: fixture articles (`seed_fixtures.py`), not the real ServiceNow knowledge
  base. Real KB content would need re-seeding/re-validation of benchmark numbers.
- **`AI_FIELD_PREFIX`**: must be set per-instance in `.env`; not a code stub, just
  configuration that varies per ServiceNow PDI.
- **Benchmark harness** (`pipeline_benchmark.py`) still uses its own threshold-gate stub
  decision layer, separate from the real agent above — it evaluates retrieval quality
  directly rather than invoking the full LLM agent per benchmark case (would be slow/costly
  to run the real agent 30x per benchmark run). This is intentional, not a gap to fix.
- **`businessRule/business_rule.js`** (the real ServiceNow-side HMAC sender) is not part of
  this PR — the receiver above has been verified against a manually-signed test request
  (`test_webhook.ps1`), not against the actual ServiceNow Business Rule's output. These should
  produce identical signatures given the same secret and body, but that hasn't been confirmed
  end-to-end from a real ServiceNow-triggered event yet.

## Resolved since the last revision (real, not stubbed anymore)
- **Agent decision**: the real S3.4 ReAct agent (searchKB/addWorkNote/suggestAnswer/
  requestHR via `agent/s3_worker.py`) is merged and wired into the Celery task — no more
  threshold-gate stand-in in the live pipeline.
- **Celery consumer**: real Redis SETNX dedup (24h expiry, `evt:` key prefix) and real
  incident claim (PATCH `ai_status=in_progress`) before the agent runs.
- **Writeback**: real, atomic Table API PATCH to the scoped AI fields — not a log-only stub.
- **Tracing**: the real production `run_agent_loop()` now wraps its actual LLM call in the
  `agent-decision` span — a live run produces the full 4-span structure, not just the
  standalone benchmark/demo tracing helper.
