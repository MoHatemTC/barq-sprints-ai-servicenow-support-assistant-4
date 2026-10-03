# BARQ AI ServiceNow Support Assistant

An event-driven assistant for ServiceNow. When an incident is created, it
searches the team's knowledge base, drafts a grounded, cited suggestion, and
writes it back to the incident **for a human to review**. It never resolves,
closes, reassigns, or emails a ticket on its own.

Built by Team G4 in the Sprints × BARQ Systems internship (Sprints 1–4).

---

## Documentation

| Document | Read it to… |
|---|---|
| [Run Guide](documentation/RUN_GUIDE.md) | set up and run the system, from `git clone` to a processed incident |
| [Architecture](documentation/ARCHITECTURE.md) | understand the components, the runtime flow, and what is stubbed or partial |
| [Decision Log](documentation/DECISION_LOG.md) | see the main technical choices and why they were made |

---

## What it does

1. A ServiceNow **Business Rule** sends a signed event when an incident is created.
2. The **FastAPI receiver** checks the HMAC signature, drops duplicate events
   (Redis), and queues the work on **Celery**. It replies `202` right away.
3. A **Celery worker** marks the incident `in_progress` and runs the **agent**.
4. The agent (a bounded ReAct loop on an LLM via the LiteLLM proxy) searches the
   knowledge base in **Qdrant**, up to 3 times. It sees only chunks scoring above
   `AGENT_CHUNK_THRESHOLD` (default `0.65`).
5. The agent then either:
   - **suggests an answer** built only from the retrieved chunks, with the source
     article numbers, or
   - **escalates to a human** (`No knowledge article found for this incident.`).
6. The result is written back to the incident's scoped AI fields in one PATCH,
   always with `human_review_required = true`. Every run is traced in **Langfuse**.

```
ServiceNow incident ──► Business Rule ──► POST /api/v1/events/servicenow
                         (HMAC-signed)        │ verify signature → dedup (Redis) → enqueue
                                              ▼
                                   Celery worker (process_incident)
                                              │ claim incident (ai_status = in_progress)
                                              ▼
                                   ReAct agent ──► searchKB ──► Qdrant
                                              │
                         suggestAnswer / requestHR
                                              ▼
                     PATCH scoped AI fields on the incident (human review required)
```

A second, smaller flow keeps Qdrant in step with the ServiceNow knowledge base:
`POST /api/v1/events/kb` → Celery `process_kb_event` → re-chunk, re-embed and
upsert or delete the article's chunks. See [Architecture](documentation/ARCHITECTURE.md)
for both flows and their current status.

---

## Quick start

The full steps, prerequisites and troubleshooting are in the
**[Run Guide](documentation/RUN_GUIDE.md)**. In short:

```bash
git clone https://github.com/MoHatemTC/barq-sprints-ai-servicenow-support-assistant-4.git
cd barq-sprints-ai-servicenow-support-assistant-4
cp .env.example .env              # fill in your own values; never commit .env
uv sync
uv run python benchmark/seed_fixtures.py   # first time only: load KB articles into Qdrant
docker compose up --build         # api :8000, Celery worker, Redis, Qdrant
curl localhost:8000/health        # {"status": "ok"}
```

Then expose port 8000 (for example with ngrok), point the ServiceNow Business Rule
at `https://<your-host>/api/v1/events/servicenow`, and create an incident.

Run the test suite with:

```bash
uv run pytest
```

---

## Tech stack

| Area | Technology |
|---|---|
| Ticketing platform | ServiceNow PDI (Business Rules, Table API, scoped AI fields) |
| API / receiver | FastAPI + Uvicorn |
| Queue and dedup | Celery, Redis |
| Agent | LangChain `ChatOpenAI` against the Sprints LiteLLM proxy (Gemini model) |
| Embeddings | Gemini `gemini-embedding-2`, 768 dimensions |
| Vector store | Qdrant (Qdrant Cloud, or the local container in `docker-compose.yml`) |
| Tracing | Langfuse |
| Packaging and runtime | Python 3.14, uv, Docker Compose |

---

## HTTP endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/events/servicenow` | Incident events from ServiceNow. Requires a valid `X-Signature` (HMAC-SHA256 of the raw body). Returns `202`, or `401` / `400` / `503`. |
| `POST` | `/api/v1/events/kb` | Knowledge-base change events (insert / update / delete). Same HMAC scheme with its own secret. |
| `GET` | `/kb/articles` | Lists published KB articles from ServiceNow (debug helper). |
| `GET` | `/health` | Liveness check. |

---

## Repository layout

```
src/barq_ai_support/        The running system
  main.py                     FastAPI app; mounts both receivers + /health, /kb/articles
  webhook.py                  Incident receiver: HMAC check, Redis dedup, Celery dispatch
  kb_webhook.py               KB-change receiver
  celery_app.py, tasks.py     Celery app; process_incident and process_kb_event tasks
  agent/s3_worker.py          The ReAct agent: tools, prompt, loop, writeback
  agent/privacy.py            Regex masking of tokens, keys, emails, phone numbers
  retrieval/retriever.py      Qdrant search with a per-chunk score threshold
  ingestion/                  Chunking, embedding, Qdrant storage
  kb_sync_service.py          KB → Qdrant sync logic (with kb_state_store.py)
  servicenow_client.py        ServiceNow Table API client
  tracing.py                  Langfuse trace and spans
  config.py                   All settings, read from .env
businessRule/               ServiceNow Business Rule that signs and sends incident events
benchmark/                  Retrieval / pipeline benchmarks, fixture KB seeder, results
cli/                        S3.5 offline PDF ingestion CLI (separate from the live flow)
agent/                      S2.5 executor (earlier sprint; not used by the live flow)
tests/                      Pytest suite
documentation/              Run guide, architecture, decision log, ServiceNow PDI notes
Dockerfile, docker-compose.yml
```

---

## Project status

The incident flow runs end to end: receiver, dedup, Celery, the real agent,
writeback, and tracing are all wired up. Known limits, all described in
[Architecture](documentation/ARCHITECTURE.md):

- **Fixture knowledge base:** the benchmark results come from 24 synthetic KB
  articles (`benchmark/seed_fixtures.py`), not the production knowledge base.
- **Benchmark vs. live threshold:** the benchmarks measure
  `RETRIEVAL_SCORE_THRESHOLD` (0.75), while the live agent uses
  `AGENT_CHUNK_THRESHOLD` (0.65).
- **KB sync trigger:** the receiver and worker for KB changes exist, but the
  ServiceNow Business Rule that would send those events is not in this repository.
- **Password masking:** the Python side masks tokens, API keys, emails and phone
  numbers, but not passwords. Passwords are meant to be masked by a ServiceNow
  Business Rule before saving, and that rule is not in this repository.

---

## Security

- Real credentials, signing secrets and API keys are **never** committed.
  `.env` is git-ignored; `.env.example` holds placeholders only.
- Both receivers reject any request whose HMAC-SHA256 signature does not match
  (constant-time comparison, checked before the body is parsed).
- The ServiceNow integration user is least-privilege, not an admin (see
  [`documentation/pdi_guide.md`](documentation/pdi_guide.md)).
- The agent has exactly four tools: `searchKB`, `addworknote`, `suggestAnswer`,
  `requestHR`. None can resolve, close, reassign, or email an incident, and every
  suggestion is flagged for human review.
- Incident text is treated as untrusted input in the agent prompt.
