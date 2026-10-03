# Run Guide

How to set up and run the BARQ AI ServiceNow Support Assistant, from `git clone`
to an incident that has been processed by the agent.

At the end you will have:

- the API, the Celery worker, Redis and Qdrant running in Docker;
- the knowledge base loaded into Qdrant;
- a ServiceNow incident that, when created, gets an AI suggestion or an escalation
  written back to it, with a trace in Langfuse.

For how the pieces fit together, see [Architecture](ARCHITECTURE.md).

---

## 1. Prerequisites

**Tools on your machine**

| Tool | Why | Check |
|---|---|---|
| Git | clone the repo | `git --version` |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | installs Python 3.14 and the dependencies | `uv --version` |
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) (WSL2 backend on Windows) | runs the stack | `docker compose version` |
| [ngrok](https://ngrok.com/download) or a similar tunnel | lets ServiceNow reach your local API | `ngrok version` |

**Accounts and keys** (you get the values from the team or your own accounts; never commit them)

| What | Used for |
|---|---|
| ServiceNow PDI and an integration user (not an admin) | reading incidents and KB articles, writing AI fields |
| Gemini API key | embeddings (`gemini-embedding-2`, 768 dimensions) |
| Sprints LiteLLM proxy key | the agent's LLM calls |
| Qdrant Cloud cluster URL and API key (or the local Qdrant container, see 4.2) | vector search |
| Langfuse project keys (optional) | tracing |

> **Windows users:** clone the repo **outside OneDrive** (for example `C:\dev`).
> OneDrive locks files inside `.git` and makes `git checkout` and `git am` fail.

---

## 2. Clone and install

```bash
git clone https://github.com/MoHatemTC/barq-sprints-ai-servicenow-support-assistant-4.git
cd barq-sprints-ai-servicenow-support-assistant-4
uv sync
```

`uv sync` reads `.python-version` (3.14), downloads that Python if needed, and
installs the dependencies from `uv.lock` into `.venv`.

---

## 3. Configure `.env`

```bash
cp .env.example .env        # Windows cmd: copy .env.example .env
```

Open `.env` and replace the placeholders. `.env` is git-ignored; **never commit it,
and never paste real values into docs, issues or PRs.**

| Variable | Required | What to put |
|---|---|---|
| `SERVICENOW_INSTANCE_URL` | yes | `https://<your-instance>.service-now.com/` |
| `SERVICENOW_USERNAME`, `SERVICENOW_PASSWORD` | yes | the integration user, not an admin |
| `SERVICENOW_KNOWLEDGE_BASE_SYS_ID` | yes | sys_id of the knowledge base the assistant reads |
| `INCIDENT_SIGNING_SECRET` | yes | a long random string; must equal the ServiceNow property in step 5.3 |
| `KB_SIGNING_SECRET` | for KB sync | a different long random string |
| `GEMINI_API_KEY` | yes | your Gemini key. The worker cannot start without it. |
| `LITELLM_BASE_URL` | yes | `https://management.sprints.ai/litellm` |
| `LITELLM_API_KEY`, `OPENAI_API_KEY` | yes | **both** set to your LiteLLM proxy key (the agent reads `OPENAI_API_KEY`) |
| `LLM_MODEL` | yes | e.g. `gemini/gemini-3.5-flash` (keep the `gemini/` prefix) |
| `QDRANT_URL`, `QDRANT_API_KEY` | yes | your Qdrant Cloud cluster (see 4.2 for local Qdrant) |
| `QDRANT_COLLECTION_NAME` | yes | keep `barq_kb_chunks` (the seeder writes to this name) |
| `AI_FIELD_PREFIX` | yes | the scope prefix of the AI fields on Incident, e.g. `x_2215387_sprint_0_` (see 5.1) |
| `REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` | yes | keep the defaults for Docker (`redis://redis:6379/...`) |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | no | leave empty to turn tracing off |
| `AGENT_CHUNK_THRESHOLD` | no | minimum chunk score the agent sees; default `0.65` |
| `AGENT_MAX_SEARCHES` | no | searches before escalation; default `3` |
| `AGENT_MAX_STEPS` | no | LLM turns before escalation; default `6` |
| `RETRIEVAL_SCORE_THRESHOLD` | no | threshold used by the **benchmark scripts only**; `0.75` |

To generate a signing secret:

```bash
uv run python -c "import secrets; print(secrets.token_hex(32))"
```

---

## 4. Load the knowledge base into Qdrant

The agent searches a Qdrant collection named `barq_kb_chunks`. Load it once
before the first run, and again whenever you change the embedding model.

### 4.1 With Qdrant Cloud (recommended)

```bash
uv run python benchmark/seed_fixtures.py
```

This creates the collection (if needed), chunks the **24 fixture KB articles**
defined in the script, embeds them with Gemini and upserts them. The last line
reads `Upserted <n> points. Collection 'barq_kb_chunks' now has <m> points total.`
Re-running is safe: point IDs are
deterministic, so existing points are updated, not duplicated.

> These are synthetic articles written to match the benchmark dataset, not the
> production knowledge base. To keep Qdrant in sync with real ServiceNow articles
> afterwards, use the KB sync endpoint (step 9).

### 4.2 With the local Qdrant container (no cloud account)

1. Start only Qdrant: `docker compose up -d qdrant`
2. In `.env`, set `QDRANT_URL=http://localhost:6333` and `QDRANT_API_KEY=` (empty).
3. Run the seeder from step 4.1.
4. Before step 6, change `.env` to `QDRANT_URL=http://qdrant:6333`, so the
   containers reach Qdrant by its service name.

---

## 5. Prepare ServiceNow

Skip the parts your instance already has. The team's shared PDI already has 5.1–5.4.

### 5.1 AI fields on Incident

The incident table needs these scoped fields (prefix = your app scope, e.g.
`x_2215387_sprint_0_`):

| Field | Type | Written by |
|---|---|---|
| `<prefix>ai_status` | string | `in_progress` (claim), then `suggested` or `escalated` |
| `<prefix>ai_suggested_response` | string (long) | the suggested steps and `Sources: KB…` |
| `<prefix>ai_confidence` | decimal | highest retrieval score seen, 0–1 |
| `<prefix>human_review_required` | true/false | always `true` |
| `<prefix>ai_processed` | true/false | `true` when the run finishes |

Find your prefix in **System Definition → Dictionary**, filter *Table = incident*,
and set `AI_FIELD_PREFIX` in `.env` to it.

> **Partial:** the claim step (`ServiceNowClient.claim_incident`) writes to the
> hard-coded field `x_2215387_sprint_0_ai_status`, ignoring `AI_FIELD_PREFIX`.
> On an instance with a different scope, the claim PATCH fails and the worker
> retries once, then writes a failure work note. Writeback uses `AI_FIELD_PREFIX`
> correctly.

### 5.2 Integration user

A non-admin user with `snc_platform_rest_api_access` and your scope's user role
(e.g. `x_2215387_sprint_0.user`), with read access to `incident` and `kb_knowledge`
and write access to the AI fields and work notes. Put its credentials in `.env`.

### 5.3 Signing secret property

Create a system property **`x_2215387_sprint_0.webhook.secret`** (type: password)
whose value is exactly your `INCIDENT_SIGNING_SECRET`.

### 5.4 HMAC Script Include

The Business Rule signs requests with `new HmacSha256().calculate(secret, body)`.
That Script Include must exist on the instance and return the **lowercase hex**
HMAC-SHA256 of the body.

> **Not in this repo:** the `HmacSha256` Script Include lives only on the
> ServiceNow instance. If yours lacks it, create one that returns the hex digest;
> otherwise every request is rejected with `401`.

### 5.5 Incident Business Rule

Create (or update) a Business Rule with the script in
[`businessRule/business_rule.js`](../businessRule/business_rule.js):

| Setting | Value |
|---|---|
| Table | Incident [incident] |
| When | **async** |
| Insert | ✔ (Update: off) |
| Advanced | ✔, paste the script |

The script has the public endpoint **hard-coded** on its third line
(`endpointUrl`). You set it in step 7.

---

## 6. Start the stack

```bash
docker compose up --build
```

This builds one image and starts four containers: `api` (FastAPI on port 8000),
`worker` (Celery), `redis` and `qdrant`. The first build takes a few minutes.

Check it in a second terminal:

```bash
docker compose ps              # 4 services Up, redis "healthy"
curl localhost:8000/health     # {"status":"ok"}
```

Follow the worker, which is where the agent runs:

```bash
docker compose logs -f worker
```

### Without Docker (optional)

You need a Redis server on `localhost:6379`, and in `.env`:
`REDIS_URL=redis://localhost:6379/0`, `CELERY_BROKER_URL=redis://localhost:6379/0`,
`CELERY_RESULT_BACKEND=redis://localhost:6379/1`. Then, in two terminals:

```bash
uv run uvicorn barq_ai_support.main:app --port 8000
uv run celery -A barq_ai_support.celery_app worker --loglevel=info --pool=solo
```

(`--pool=solo` is needed on Windows.)

---

## 7. Expose the API to ServiceNow

```bash
ngrok http 8000
```

Copy the `https://….ngrok-free.app` (or `.dev`) URL. In the Business Rule from
step 5.5, set:

```js
var endpointUrl = "https://<your-ngrok-host>/api/v1/events/servicenow";
```

and save. The free ngrok URL changes every time ngrok restarts, so update the
Business Rule each time.

---

## 8. Process an incident end to end

### 8.1 From ServiceNow

Create an incident in ServiceNow, for example:

| Short description | Expected result |
|---|---|
| `No internet since restart, websites don't load` | `ai_status = suggested`, steps from KB0010010 |
| `Outlook hangs on loading profile then closes` | `ai_status = suggested`, steps from KB0010015 |
| `Office coffee machine shows error E04` | `ai_status = escalated`, work note *No knowledge article found for this incident.* |

What should happen, in order (watch `docker compose logs -f worker`):

1. The `api` log shows `Dispatched event_id=… sys_id=… to Celery`.
2. The worker claims the incident (`ai_status = in_progress`).
3. The agent calls `searchKB` 1–3 times, then `suggestAnswer` or escalates.
4. The incident's AI fields are updated in one PATCH; escalations also add a work
   note.

Then check:

- **In ServiceNow:** open the incident and look at the AI fields and **Work notes**.
- **In Langfuse** (if configured): one trace `incident-run-<sys_id>` with
  `incident-fetch`, `kb-retrieval`, `agent-decision` and `servicenow-writeback`
  spans. `kb-retrieval` shows which chunks passed the threshold.

### 8.2 Without the Business Rule (signed test request)

To test the receiver and worker without triggering ServiceNow, send a correctly
signed request yourself. Save this as `send_test_event.py` anywhere (do not commit
it):

```python
"""Send one signed incident event to the local receiver."""
import hashlib, hmac, json, sys, uuid

import httpx
from dotenv import dotenv_values

secret = dotenv_values(".env")["INCIDENT_SIGNING_SECRET"]
body = json.dumps({
    "event_id": str(uuid.uuid4()),        # dedup key: a new one per test
    "incident_sys_id": sys.argv[1],       # a REAL incident sys_id on your instance
    "number": "INC-TEST",
    "short_description": "signed test event",
    "description": "",
}).encode()
signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

response = httpx.post(
    "http://localhost:8000/api/v1/events/servicenow",
    content=body,
    headers={"Content-Type": "application/json", "X-Signature": signature},
)
print(response.status_code, response.text)
```

Run it from the repo root (it reads `.env` there):

```bash
uv run python path/to/send_test_event.py <incident_sys_id>
```

Expected: `202 {"status":"accepted","number":"INC-TEST"}`. The worker then fetches
the **real** incident by sys_id from ServiceNow; the text in the payload is not
used by the agent.

| Response | Meaning |
|---|---|
| `202` + JSON body | accepted and queued |
| `202`, empty body | duplicate `event_id`, ignored on purpose (kept 24 h) |
| `401` | signature missing or wrong: check the secret |
| `400` | body is not valid JSON or a field is missing |
| `503` | Redis or Celery unreachable; nothing was queued, safe to resend |

---

## 9. Keep Qdrant in sync with ServiceNow KB changes (optional)

`POST /api/v1/events/kb` accepts a signed event (`X-Signature`, using
`KB_SIGNING_SECRET`) and queues `process_kb_event`, which re-chunks and re-embeds
the article, or deletes its points:

```json
{"event_id": "<unique>", "sys_id": "<kb_knowledge sys_id>", "operation": "insert | update | delete", "timestamp": "<ISO-8601>"}
```

> **Partial:** there is no ServiceNow Business Rule in this repo that sends these
> events. Send them yourself (adapt the script in 8.2: change the URL, the secret
> and the body), or add a rule on `kb_knowledge`. Unlike the incident receiver,
> this endpoint does not deduplicate events; the sync itself is idempotent.

---

## 10. Tests and benchmarks

**Unit tests** (no live services needed; `.env` must exist, and the ServiceNow
values may be placeholders):

```bash
uv run pytest
```

Two tests check Gemini-key handling (`test_embedding_module_imports_without_gemini_key`,
`test_embedding_backend_uses_gemini_key_loaded_from_settings`) and can fail when
`GEMINI_API_KEY` is a placeholder.

**Pipeline benchmark** (needs Qdrant loaded and a real Gemini key):

```bash
uv run python benchmark/pipeline_benchmark.py --real
```

It writes `benchmark/PIPELINE_RESULTS.md` and
`benchmark/pipeline_benchmark_results.json` (Top-3 Hit Rate and Refusal
Correctness). It scores retrieval with `RETRIEVAL_SCORE_THRESHOLD`, not the live
agent's `AGENT_CHUNK_THRESHOLD`, and does not call the LLM agent.

**Tracing demo** (sends two traced runs to Langfuse):

```bash
uv run python benchmark/traced_demo_run.py
```

---

## 11. Day-to-day operation

| Task | Command |
|---|---|
| Stop the stack | `Ctrl+C`, then `docker compose down` |
| Start again (no code change) | `docker compose up` |
| Rebuild after a code or `.env` change | `docker compose up --build` |
| Logs of one service | `docker compose logs -f api` (or `worker`, `redis`, `qdrant`) |
| Change the agent threshold | set `AGENT_CHUNK_THRESHOLD` in `.env`, then `docker compose up --build` |
| Rotate the signing secret | change `INCIDENT_SIGNING_SECRET` **and** the ServiceNow property (5.3) together |
| Wipe local Qdrant data | `docker compose down -v` (deletes the `qdrant_storage` volume) |

---

## 12. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Every event returns `401` | `INCIDENT_SIGNING_SECRET` ≠ ServiceNow property, or `HmacSha256` missing / not hex | make both secrets identical; check 5.4 |
| ServiceNow logs a timeout or 404 | ngrok stopped or its URL changed | restart ngrok and update `endpointUrl` (step 7) |
| Worker exits with `KeyError: 'GEMINI_API_KEY'` | key missing in `.env` | set it, then `docker compose up --build` |
| Worker crashes on start with `QDRANT_URL` / `QDRANT_API_KEY` `KeyError` | variable missing from `.env` | set both (the key may be empty for local Qdrant) |
| Every incident escalates with *No knowledge article found* | empty collection, or the KB was embedded with a different model | re-run `seed_fixtures.py` (step 4) |
| Unrelated incidents get suggestions | threshold too low | raise `AGENT_CHUNK_THRESHOLD` (e.g. `0.7`) |
| Worker log shows 401/403 from ServiceNow | wrong integration-user credentials or roles | check `.env` and step 5.2 |
| Claim fails, then a work note *AI processing failed…* | scope differs from `x_2215387_sprint_0_` (see 5.1) | use the team instance or fix the claim field name |
| AI fields stay empty but the run finished | `AI_FIELD_PREFIX` does not match the field names | fix it (step 5.1), rebuild |
| `503` from the receiver | Redis/worker down | `docker compose ps`; restart the stack |
| No traces in Langfuse | Langfuse keys empty or wrong host | set the three `LANGFUSE_*` variables |
| `git` "Deletion of directory … failed" on Windows | OneDrive or an editor is locking files | close editors, pause OneDrive, or clone outside OneDrive |
