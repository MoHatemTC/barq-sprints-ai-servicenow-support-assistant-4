# Architecture

This document describes the BARQ AI ServiceNow Support Assistant **as it exists on
`main`** (commit `849e871`, after PR #27). Where something is stubbed, partial, or
lives outside this repository, it is labeled **Partial**, **Stub**, or
**Not in repo**. Section 9 lists all of them in one place.

To run the system, see the [Run Guide](RUN_GUIDE.md). For why it is built this
way, see the [Decision Log](DECISION_LOG.md).

---

## 1. System context

```
                         ┌──────────────────────── ServiceNow PDI ─────────────────────────┐
                         │  Incident created ──► Business Rule (async, after insert)         │
                         │                          signs body with HMAC-SHA256              │
                         │  ▲ Table API (GET incident / KB article, PATCH AI fields, notes)  │
                         └──┼───────────────────────┬──────────────────────────────────────────┘
                            │                       │ POST /api/v1/events/servicenow
                            │                       ▼ (X-Signature)
┌───────────────────────────┼──────── Docker Compose (this repo) ─────────────────────────────┐
│                           │        ┌──────────────────────┐   SET NX evt:<id>  ┌───────┐  │
│                           │        │  api  (FastAPI)       │──────────────────►│ redis │  │
│                           │        │  webhook.py           │   enqueue task    │ :6379 │  │
│                           │        │  kb_webhook.py        │──────────────────►│       │  │
│                           │        └──────────────────────┘                   └───┬───┘  │
│                           │                                                  consume│      │
│                           │        ┌──────────────────────────────────────────────▼───┐  │
│                           └────────│  worker (Celery)  tasks.py                         │  │
│                                    │   process_incident → agent/s3_worker.py (ReAct)    │  │
│                                    │   process_kb_event → kb_sync_service.py            │  │
│                                    └────┬──────────────┬──────────────┬─────────────────┘  │
│                                         │ search/upsert│              │                    │
│                                     ┌───▼────┐         │              │                    │
│                                     │ qdrant │ (or Qdrant Cloud)      │                    │
│                                     └────────┘         │              │                    │
└────────────────────────────────────────────────────────┼──────────────┼────────────────────┘
                                         embeddings      │              │ LLM (OpenAI-compatible)
                                   Gemini API ◄──────────┘              └──► Sprints LiteLLM proxy
                                   gemini-embedding-2                        (Gemini chat model)
                                                     traces ──► Langfuse (optional)
```

---

## 2. Components

| Component | Code | Responsibility | Status |
|---|---|---|---|
| Incident Business Rule | `businessRule/business_rule.js` (runs in ServiceNow) | On incident insert, POST a signed event to the receiver | Real; endpoint URL hard-coded (see 9) |
| HMAC Script Include `HmacSha256` | ServiceNow instance only | Compute the signature used by the Business Rule | **Not in repo** |
| API app | `src/barq_ai_support/main.py` | FastAPI app; mounts both receivers, `/health`, `/kb/articles` | Real |
| Incident receiver | `webhook.py` | Verify HMAC → validate → dedup in Redis → enqueue `process_incident` | Real |
| KB receiver | `kb_webhook.py` | Verify HMAC → validate → enqueue `process_kb_event` | Real; no dedup (see 9) |
| Celery app and tasks | `celery_app.py`, `tasks.py` | Broker and backend on Redis; retry and failure policy | Real |
| Retry classifier | `retry_policy.py` | Decide whether an error is transient (retry once) or permanent | Real |
| Agent | `agent/s3_worker.py` | Fetch incident, bounded ReAct loop over 4 tools, atomic writeback | Real |
| Privacy masking | `agent/privacy.py` | Regex-mask tokens, API keys, auth headers, emails, phone, ID and card numbers | Real; passwords **not** masked here (see 9) |
| Retriever | `retrieval/retriever.py` | Embed query, search Qdrant top-k, drop chunks at or below the threshold | Real |
| Ingestion | `ingestion/chunker.py`, `embedding.py`, `qdrant_store.py` | HTML → chunks; Gemini embeddings (768-d); create collection and upsert | Real |
| KB sync | `kb_sync_service.py`, `kb_state_store.py` | Keep Qdrant in step with KB inserts, updates and deletes; SQLite hash store | Real; trigger **not in repo** |
| ServiceNow client | `servicenow_client.py` | Table API calls with basic auth (integration user) | Real; claim field hard-coded (see 9) |
| Tracing | `tracing.py` | Langfuse trace per incident with 4 span types; no-op when keys are unset | Real |
| Settings | `config.py` + `.env` | All configuration via pydantic-settings | Real |

Outside the live flow:

| Folder | What it is |
|---|---|
| `benchmark/` | Retrieval (S2.6) and pipeline (S3.6) benchmarks, fixture KB seeder, tracing demo |
| `cli/` | S3.5 offline PDF ingestion CLI (Gemini Vision per page → chunks → Qdrant) |
| `agent/` (repo root) | S2.5 single-shot executor and prompt; still covered by tests, **not used** at runtime |

---

## 3. Incident flow, step by step

### 3.1 ServiceNow event

1. An incident is inserted. The **async** Business Rule on `incident` (Insert only) runs.
2. It builds the body
   `{event_id, incident_sys_id, number, short_description, description}`, where
   `event_id = gs.generateGUID()`.
3. It reads the secret from the system property `x_2215387_sprint_0.webhook.secret`,
   computes `HmacSha256().calculate(secret, body)` (lowercase hex), and POSTs to
   `endpointUrl` with header `X-Signature`.
4. It logs success on `202`, otherwise a warning with the response body.

### 3.2 Receiver (`POST /api/v1/events/servicenow`)

The order is fixed, and each step returns before the next:

| # | Step | Outcome |
|---|---|---|
| 1 | Read the **raw** body bytes | — |
| 2 | HMAC-SHA256 over the raw bytes with `INCIDENT_SIGNING_SECRET`; constant-time compare to `X-Signature` | mismatch, missing header, or empty secret → **401** (before any parsing) |
| 3 | Parse and validate JSON (`IncidentEvent`, Pydantic) | invalid → **400** |
| 4 | `SET evt:<event_id> 1 NX EX 86400` in Redis | key already exists → **202, empty body**, nothing queued |
| 5 | `process_incident.delay(payload)` | queued → **202** `{"status":"accepted","number":…}` |
| 5a | …if enqueueing raises | delete the dedup key, return **503** with `Retry-After: 30` so a resend is not mistaken for a duplicate |

The receiver never calls ServiceNow, the LLM, or Qdrant.

### 3.3 Celery task `process_incident`

1. **Claim:** PATCH `x_2215387_sprint_0_ai_status = in_progress` on the incident.
2. Run the agent: `process_incident_event({"sys_id": …})`.
3. **Failure policy** (`tasks.py` + `retry_policy.py`):
   - **Transient** (timeouts, connection errors, HTTP 429/5xx anywhere in the
     cause chain): retry **once** after 10 s.
   - **Permanent** (other 4xx, config errors, malformed payload), or a second
     transient failure: log it, add the work note
     `AI processing failed and was not completed: <error>` (best effort), and
     return an error result. The worker keeps consuming other jobs.
   - The agent contains its own errors (see 3.5), so in practice the retry only
     protects the claim step.

Both steps run under a single `asyncio.run()`, so the agent's HTTP and LLM clients
live in one event loop.

### 3.4 Agent (`agent/s3_worker.py`)

**Preload (deterministic, before any LLM call):** GET the incident
(`sys_id, number, short_description, description, category`) through the Table
API. The trace span is `incident-fetch`.

**Prompt:** a system prompt with 7 rules (grounding, termination, search-or-answer,
confidence, capability boundary, untrusted input, search limit), plus the incident
wrapped in an `--- INCIDENT (untrusted, requester-submitted text) ---` block. The
block is passed through `mask_sensitive_text` first.

**Tools** (the complete registry; no others exist):

| Tool | Effect | Ends the run? |
|---|---|---|
| `searchKB(query)` | `retrieve(query, score_threshold=AGENT_CHUNK_THRESHOLD)` → chunks above the threshold, best first: `{article_number, text, score, category}` | no |
| `addworknote(note)` | append a work note | no |
| `suggestAnswer(procedure, sources, confidence)` | writeback as "suggested" (see 3.5) | **yes** |
| `requestHR(reason)` | writeback as "escalated" + work note | **yes** |

There is no tool that can resolve, close, cancel, reassign, reprioritise or email
an incident.

**Loop** (bounded ReAct, `run_agent_loop`):

```
for step in 1..AGENT_MAX_STEPS (6):
    LLM call (span: agent-decision)
    no tool call      → add "Call searchKB again or call suggestAnswer." and continue
    for each tool call:
        a 4th searchKB (after AGENT_MAX_SEARCHES = 3) → escalate: "No knowledge article found for this incident."
        run the tool; its output is masked before it goes back to the LLM
        suggestAnswer / requestHR → return
step budget used up → escalate: "No knowledge article found for this incident."
```

**Retrieval** (`retrieve()`): embed the query (`gemini-embedding-2`, 768-d) →
Qdrant `query_points` on `barq_kb_chunks` (cosine, `top_k = RETRIEVAL_TOP_K`,
default 5) → keep chunks with `score > threshold`, sorted by score; empty → refusal
result. Each search produces a `kb-retrieval` span with the passing chunks.

### 3.5 Writeback

One Table API **PATCH** per outcome, retried once on failure. Field names are
`AI_FIELD_PREFIX` + name:

| Outcome | Fields written | Plus |
|---|---|---|
| `suggestAnswer` | `ai_status = suggested`, `ai_suggested_response = "<steps>\n\nSources: <KB…>"`, `ai_confidence` (clamped to 0–1), `human_review_required = true`, `ai_processed = true` | — |
| `requestHR` or a limit reached | `ai_status = escalated`, `human_review_required = true`, `ai_processed = true` | work note `Escalated to human review: <reason>` |
| Unexpected exception inside the agent | nothing in the AI fields (`ai_status` stays `in_progress`, so the incident can be recovered) | work note `AI processing failed and was not completed: …` |

Span: `servicenow-writeback`.

### 3.6 Tracing

When the `LANGFUSE_*` keys are set, each incident produces one trace
`incident-run-<sys_id>` containing:

```
incident-run-<sys_id>
  ├─ incident-fetch          (tool)
  ├─ agent-decision          (generation) × number of LLM turns
  ├─ kb-retrieval            (retriever)  × number of searches
  └─ servicenow-writeback    (tool)
```

Without keys, tracing is a no-op and the flow is unchanged.

---

## 4. KB sync flow

`POST /api/v1/events/kb` with body `{event_id, sys_id, operation, timestamp}`,
signed with `KB_SIGNING_SECRET`: 401 on a bad signature, 400 on bad JSON or an
unknown `operation`, otherwise `process_kb_event.delay()` and 202.

`kb_sync_service.sync_article(sys_id, operation)` keeps one SQLite row per article
(`kb_state_store.py`: `sys_id`, SHA-256 of the body, `short_description`,
`kb_category`, `workflow_state`):

| Path | Condition | Action |
|---|---|---|
| Delete | `operation = delete` | delete the article's Qdrant points and its state row |
| Re-embed | new article, or body hash changed | delete old points → chunk (1000 / 100 overlap) → embed → upsert → save hash |
| Metadata only | body unchanged, metadata changed | patch the points' payloads, no embedding calls |
| No-op | nothing changed | nothing |

The sync is idempotent, so retries are safe (same retry policy as incidents).

---

## 5. Data and state

| Store | What | Lifetime |
|---|---|---|
| Redis db 0 | `evt:<event_id>` dedup keys; Celery broker queue | dedup: 24 h (`DEDUP_TTL_SECONDS`) |
| Redis db 1 | Celery results | Celery default |
| Qdrant `barq_kb_chunks` | 768-d cosine vectors; payload = the article's fields (`sys_id`, `number`, `short_description`, category…) plus `text`, `heading_path`, `chunk_index` | until re-seeded or synced |
| SQLite `kb_state.db` | KB sync body hashes and metadata (`KB_STATE_DB_URL`) | inside the worker container (lost on recreate) |
| ServiceNow incident | 5 scoped AI fields + work notes | permanent |
| Langfuse | traces | per Langfuse project |

---

## 6. Security model

- **Authentication of events:** HMAC-SHA256 over the raw body, a constant-time
  compare, checked before parsing. The incident receiver rejects everything when
  its secret is empty (fails closed). The incident and KB flows use separate
  secrets.
- **Replay and duplicate protection:** Redis `SET NX` dedup on `event_id` for
  incidents (24 h).
- **Least privilege:** ServiceNow is accessed by a non-admin integration user;
  see [`pdi_guide.md`](pdi_guide.md).
- **Bounded agent:** four tools, none destructive; a step and search budget; every
  output has `human_review_required = true`.
- **Prompt injection:** incident text is fenced as untrusted data and the prompt
  forbids following it.
- **Data minimisation:** `mask_sensitive_text` redacts credential-like tokens,
  emails, phone, ID and card numbers before text reaches the LLM.
- **Secrets:** read only from `.env` (git-ignored); `.env.example` holds placeholders.

---

## 7. Configuration that changes behaviour

| Setting | Default | Effect |
|---|---|---|
| `AGENT_CHUNK_THRESHOLD` | 0.65 | minimum score for a chunk to reach the agent |
| `AGENT_MAX_SEARCHES` | 3 | searches before escalation |
| `AGENT_MAX_STEPS` | 6 | LLM turns before escalation |
| `RETRIEVAL_TOP_K` | 5 | Qdrant hits per search |
| `RETRIEVAL_SCORE_THRESHOLD` | 0.4 in code, 0.75 in `.env.example` | used by the benchmark scripts and as `retrieve()`'s fallback; not by the live agent |
| `LLM_MODEL`, `LLM_TEMPERATURE` | `gemini/gemini-3.5-flash-lite`, 0.0 | agent model via the LiteLLM proxy |
| `AI_FIELD_PREFIX` | `x_2066139_ai_triag_` in code | must match the instance's AI field scope |
| `DEDUP_TTL_SECONDS` | 86400 | how long a duplicate `event_id` is ignored |

---

## 8. Deployment

`docker-compose.yml` runs four services from one image (`Dockerfile`:
`python:3.14-slim`, uv, a non-root `appuser`):

| Service | Command | Ports |
|---|---|---|
| `api` | `uvicorn barq_ai_support.main:app` | 8000 |
| `worker` | `celery -A barq_ai_support.celery_app worker` | — |
| `redis` | `redis:7-alpine`, with healthcheck | 6379 |
| `qdrant` | `qdrant/qdrant:latest`, volume `qdrant_storage` | 6333 |

`api` and `worker` read `.env`. Compose overrides the Celery URLs to point at the
`redis` service. `QDRANT_URL` from `.env` wins, falling back to the local `qdrant`
service. ServiceNow reaches the API through a public tunnel (ngrok in development).

---

## 9. What is real, partial, stubbed, or not in this repo

| Item | Label | Detail |
|---|---|---|
| Receiver, dedup, Celery dispatch, claim, agent loop, writeback, tracing | **Real** | Wired end to end on `main` |
| `HmacSha256` Script Include | **Not in repo** | Exists only on the ServiceNow instance; the Business Rule depends on it |
| KB-change Business Rule (sender for `/api/v1/events/kb`) | **Not in repo** | Receiver and worker exist; nothing in the repo sends these events |
| Password-masking Business Rule | **Not in repo** | Passwords were removed from the Python regex in favour of a ServiceNow before-insert rule; that rule's script was deleted from the repo, so on `main` nothing in the repo masks passwords |
| Business Rule `endpointUrl` | **Partial** | A hard-coded ngrok URL; must be edited per environment |
| Claim field name | **Partial** | `claim_incident` writes `x_2215387_sprint_0_ai_status` directly instead of using `AI_FIELD_PREFIX`; works only on the team PDI's scope |
| KB receiver | **Partial** | No `event_id` dedup (the sync is idempotent, so duplicates only cost work); unlike the incident receiver it does not refuse requests when `KB_SIGNING_SECRET` is empty |
| KB sync state store | **Partial** | SQLite file inside the worker container; lost when the container is recreated (the next event re-embeds) |
| Knowledge base content | **Stub** | `benchmark/seed_fixtures.py` loads 24 synthetic articles written to match the benchmark dataset, not the production KB |
| Benchmark vs. live agent | **Partial** | The benchmarks score retrieval with `RETRIEVAL_SCORE_THRESHOLD` and a threshold-gate decision, without calling the LLM agent; the live agent uses `AGENT_CHUNK_THRESHOLD` |
| Chunking parameters | **Partial** | Seeder 400 / 50 characters, KB sync 1000 / 100: the same article is chunked differently by the two paths |
| Embedding without a Gemini key | **Partial** | `_select_embedding_fn` returns `create_embedding` (Gemini) even when only LiteLLM keys are set, and `ingestion/embedding.py` needs `GEMINI_API_KEY` at import; the LiteLLM embedding path in `run_benchmark.py` still uses `gemini-embedding-001` |
| `default_embedding_fn` | **Stub** | Hash-based fake vectors (384-d) for offline tests only |
| Celery task module path | **Partial** | `celery_app` includes `src.barq_ai_support.tasks` while the worker runs `barq_ai_support.celery_app`; it works because tasks are sent by name |
| Root `agent/` (S2.5 executor) | **Not used at runtime** | Superseded by `agent/s3_worker.py`; kept with its tests |
| `cli/` PDF ingestion | **Separate tool** | Offline CLI; not part of the event-driven flow |
