# BARQ AI ServiceNow Support Assistant

An event-driven, RAG-powered ServiceNow assistant that retrieves trusted
knowledge, drafts cited resolutions, and routes responses for human approval.

Built as part of the Sprints × BARQ Systems internship (Team G4).

## How it works, end to end

```
ServiceNow incident created
        |
        v
Business Rule (async, after-insert)   businessRule/business_rule.js
        |  POSTs minimal payload + X-ServiceNow-Secret header
        v
FastAPI /webhook                      src/barq_ai_support/webhook.py
        |  validates secret, returns 202 Accepted immediately
        |  dispatches a background task
        v
(Sprint 2 pieces -- in progress)
        |
        +-- Chunking            src/barq_ai_support/ingestion/chunking.py
        +-- Embedding + Qdrant  src/barq_ai_support/ingestion/embed_and_store.py
        +-- Semantic retrieval  src/barq_ai_support/retrieval/search.py
        +-- Agent + tools       src/barq_ai_support/agent/
                |
                v
        Suggested resolution written back to the incident,
        flagged for human review. The agent never resolves,
        closes, or reassigns a ticket on its own.
```

## Project layout

```
businessRule/            ServiceNow Business Rule script + config screenshots
src/barq_ai_support/
  main.py                 FastAPI app -- mounts the webhook + KB routes
  webhook.py               S1.4: receives incident events from ServiceNow
  config.py                Settings, loaded from .env
  servicenow_client.py      ServiceNow Table API client (KB articles)
  ingestion/                S2.1 chunking, S2.2 embedding & Qdrant storage
  retrieval/                S2.3 semantic search + threshold gate
  agent/                    S2.4 tool definitions, S2.5 agent executor + prompt
tests/                    Pytest suite
docs/, documentation/     Sprint 1 setup notes and screenshots (historical)
screenshots(*)/           Per-teammate deliverable screenshots
```

## Setup

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/).

1. Clone the repo and install dependencies:
   ```bash
   git clone https://github.com/MoHatemTC/barq-sprints-ai-servicenow-support-assistant-4.git
   cd barq-sprints-ai-servicenow-support-assistant-4
   uv sync
   ```

2. Copy the environment template and fill in your values:
   ```bash
   cp .env.example .env
   ```
   You'll need:
   - `SERVICENOW_INSTANCE_URL`, `SERVICENOW_USERNAME`, `SERVICENOW_PASSWORD` --
     credentials for your ServiceNow PDI's integration user (never an admin
     account -- see NFR-05 in the project PRD).
   - `SERVICENOW_KNOWLEDGE_BASE_SYS_ID` -- the sys_id of the KB the assistant
     reads from.
   - `SERVICENOW_WEBHOOK_SECRET` -- a shared secret. Must match the value
     hardcoded in `businessRule/business_rule.js`'s `X-ServiceNow-Secret`
     header.

3. Run the API locally:
   ```bash
   uv run uvicorn barq_ai_support.main:app --reload
   ```
   This starts the server at `http://127.0.0.1:8000`.

4. Point your Business Rule's `endpointUrl` at your local server (e.g. via
   ngrok) and create a test incident in ServiceNow to see the full flow.

## Running tests

```bash
uv run pytest
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/webhook` | Receives incident events from the ServiceNow Business Rule. Requires a valid `X-ServiceNow-Secret` header. Returns `202 Accepted` immediately. |
| `GET` | `/kb/articles` | Fetches published knowledge base articles from ServiceNow. |
| `GET` | `/health` | Basic liveness check. |

## Status

- Done -- Sprint 1: ServiceNow PDI, AI incident fields, integration user,
  Business Rule + webhook trigger
- In progress -- Sprint 2: chunking, embedding/Qdrant, semantic retrieval,
  agent tool definitions, agent executor + grounded generation
- Later -- Langfuse tracing, benchmark harness, full test suite, Docker
  Compose, demo video
