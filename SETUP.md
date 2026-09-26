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

## 1. Clone and configure
```bash
git clone <this repo>
cd barq-sprints-ai-servicenow-support-assistant-4
cp .env.example .env
```
Fill in `.env`: ServiceNow fields can be placeholders if you're not testing real ServiceNow
writeback yet. `QDRANT_URL`/`QDRANT_API_KEY`, embedding credentials, and Langfuse keys should
be real.

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
With the stack running, in a second terminal:
```bash
curl -X POST localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -H "X-ServiceNow-Secret: <your SERVICENOW_WEBHOOK_SECRET value>" \
  -d '{
    "incident_sys_id": "demo-001",
    "number": "INC0099001",
    "short_description": "No internet connection",
    "description": "User cannot reach any websites, Wi-Fi icon shows connected"
  }'
```
The API returns `202 Accepted` immediately. Watch `docker compose logs worker -f` to see the
Celery task run: fetch (stub) -> retrieval (real, against Qdrant) -> decision (stub
threshold gate) -> writeback (stub, logs the intended ServiceNow PATCH payload — not sent,
pending real scoped field names from the S3.1 ServiceNow task).

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
`agent-decision`, `servicenow-writeback`).

## Known stubs (see individual file headers for details)
- **Agent decision**: threshold-gate stand-in for the real ReAct agent (searchKB/
  addWorkNote/suggestAnswer/requestHR) — teammate branch not yet merged.
- **Celery consumer**: no Redis dedup gate or claim PATCH yet — teammate branch not yet merged.
- **Writeback**: logs the intended payload only; doesn't call the real ServiceNow Table API.
  Field names are placeholders pending the real scoped field names from the S3.1 ServiceNow task.
- **KB content**: fixture articles (`seed_fixtures.py`), not the real ServiceNow knowledge base.
