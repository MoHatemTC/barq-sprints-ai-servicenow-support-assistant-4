# Decision Log

The technical decisions that shape the system on `main`. Each entry gives the
choice, the reason, and the cost we accepted. For how the parts connect, see
[Architecture](ARCHITECTURE.md).

| # | Area | Decision |
|---|---|---|
| D1 | Integration | ServiceNow pushes events; we do not poll |
| D2 | Security | HMAC-SHA256 over the raw body, verified before parsing |
| D3 | Throughput | Receiver acks with 202 and queues on Celery |
| D4 | Reliability | Redis `SET NX` dedup on `event_id`, released when dispatch fails |
| D5 | Reliability | Claim the incident before the agent runs |
| D6 | Agent | Bounded ReAct loop with exactly four non-destructive tools |
| D7 | Governance | A human reviews every output |
| D8 | Agent | The agent decides after each search (no short-circuit) |
| D9 | Retrieval | Per-chunk threshold inside `retrieve()`, default 0.65 |
| D10 | Agent | At most 3 searches, then escalate with a fixed note |
| D11 | Agent | Any helpful chunk can be suggested; the prompt names no article sections |
| D12 | Safety | Incident text is untrusted; regex masking before the LLM |
| D13 | Safety | Passwords are masked in ServiceNow, not in Python |
| D14 | Reliability | Retry transient errors once; fail permanent ones cleanly |
| D15 | Retrieval | One embedding model for ingestion and queries: `gemini-embedding-2`, 768-d |
| D16 | Storage | Qdrant (cosine), Cloud by default, local container as fallback |
| D17 | LLM | LLM through the Sprints LiteLLM proxy, temperature 0 |
| D18 | Observability | One Langfuse trace per incident, four span types, optional |
| D19 | KB sync | Content-hash diff in SQLite, four idempotent sync paths |
| D20 | Evaluation | Benchmarks score retrieval only, on a synthetic KB |
| D21 | Access | Non-admin integration user |
| D22 | Ops | One Docker image for API and worker; config only from `.env` |

---

### D1 — ServiceNow pushes events; we do not poll

- **Decision:** An **async** Business Rule on `incident` (Insert) POSTs each new
  incident to `/api/v1/events/servicenow`.
- **Why:** Incidents get processed seconds after creation, with no polling load on
  the instance. An async rule also never slows down the user saving the form.
- **Trade-off:** ServiceNow must be able to reach the API, so development needs a
  public tunnel (ngrok), and the endpoint URL lives in the rule script.

### D2 — HMAC-SHA256 over the raw body, verified before parsing

- **Decision:** The Business Rule signs the exact body with a shared secret and sends
  `X-Signature` (lowercase hex). The receiver recomputes it over the **raw bytes**,
  compares in constant time, and rejects with `401` before parsing JSON. This
  replaced the earlier plain `X-ServiceNow-Secret` header.
- **Why:** A plain shared header can be replayed or leaked in logs, and it proves
  nothing about the body. A signature proves the body was not changed. Checking
  before parsing means unauthenticated input never reaches the JSON parser.
  Separate secrets for incidents and KB events limit the damage if one leaks.
- **Trade-off:** ServiceNow needs an HMAC Script Include (`HmacSha256`), which lives
  on the instance, not in this repo. Both sides must sign exactly the same bytes.

### D3 — Receiver acks with 202 and queues on Celery

- **Decision:** The receiver only authenticates, validates, deduplicates and
  enqueues. The agent, retrieval and all ServiceNow calls run in a Celery worker.
  Redis is both broker and result backend.
- **Why:** An agent run takes several seconds (LLM and search calls), longer than a
  ServiceNow outbound request should wait. Queuing keeps the API fast, lets work
  survive API restarts, and lets workers scale separately.
- **Trade-off:** Two more moving parts (Redis and a worker), and results are not
  returned to the caller. They appear on the incident later.

### D4 — Redis `SET NX` dedup on `event_id`, released when dispatch fails

- **Decision:** `SET evt:<event_id> NX EX 86400` before enqueueing. A replay gets
  `202` and is not queued. If enqueueing fails, the key is deleted and the
  receiver returns `503 Retry-After: 30`.
- **Why:** ServiceNow can resend the same event. Without dedup, one incident could
  get two AI runs and two writebacks. Releasing the key on failure stops a
  legitimate retry from being swallowed as a "duplicate" for 24 hours.
- **Trade-off:** Dedup depends on Redis being up. The KB receiver has no dedup yet;
  its sync is idempotent, so duplicates only cost extra work.

### D5 — Claim the incident before the agent runs

- **Decision:** The task first PATCHes `ai_status = in_progress`, then runs the agent.
- **Why:** People looking at the incident can see the AI is working on it, and an
  incident left in `in_progress` after a failure is easy to find and re-run.
- **Trade-off:** One more API call per incident. The claim currently hard-codes the
  team PDI's field name instead of `AI_FIELD_PREFIX` (labeled Partial in
  Architecture).

### D6 — Bounded ReAct loop with exactly four non-destructive tools

- **Decision:** A tool-calling loop (`agent/s3_worker.py`) with `searchKB`,
  `addworknote`, `suggestAnswer` and `requestHR`, limited to `AGENT_MAX_STEPS = 6`
  LLM turns. This replaced the Sprint 2 single-shot chain in the root `agent/`
  folder.
- **Why:** The agent must be able to refine its search (rephrase, try again), which
  a single prompt over pre-fetched chunks cannot do. A fixed tool registry makes
  the capability boundary explicit: no tool exists that can resolve, close,
  reassign or email. The step budget guarantees every run ends.
- **Trade-off:** More LLM calls per incident than a single-shot chain. Behaviour
  depends on the model following the prompt, which is why limits are enforced in
  code, not only in the prompt.

### D7 — A human reviews every output

- **Decision:** Every writeback sets `human_review_required = true`. The AI only
  suggests or escalates and never changes the incident's state.
- **Why:** Suggestions come from an LLM and can be incomplete or wrong. A support
  agent stays accountable for what reaches the requester. This was a project
  requirement from the start.
- **Trade-off:** No full automation, even for easy cases.

### D8 — The agent decides after each search (no short-circuit)

- **Decision:** `searchKB` returns its chunks to the agent, and the agent chooses
  `suggestAnswer` or another search. An earlier version ended the run on the first
  hit and wrote the raw top chunk to ServiceNow; that was removed after review.
- **Why:** The short-circuit skipped the agent's reasoning entirely. It sent raw,
  unformatted chunk text to the requester's incident and made the ReAct loop
  pointless.
- **Trade-off:** At least two LLM calls per suggested answer instead of zero.

### D9 — Per-chunk threshold inside `retrieve()`, default 0.65

- **Decision:** `retrieve()` drops every chunk scoring at or below the threshold and
  returns the rest, best first. `searchKB` passes `AGENT_CHUNK_THRESHOLD`
  (default `0.65`, configurable in `.env`).
- **Why:** The earlier gate only checked the **best** score, so weak chunks rode
  along whenever one strong chunk passed. Callers worked around it by passing
  `0.0` and filtering again themselves. One filter in one place removes that
  duplication. 0.65 was chosen so the agent sees loosely related guidance and
  judges relevance itself.
- **Trade-off:** 0.65 was not measured. The S3.6 benchmark's strongest unrelated
  case scored 0.672 (with the older embedding model), so some off-topic chunks
  can pass. Raise the threshold if unrelated incidents get suggestions.

### D10 — At most 3 searches, then escalate with a fixed note

- **Decision:** After `AGENT_MAX_SEARCHES = 3` searches without `suggestAnswer`, a
  further search attempt ends the run. The same happens when the step budget runs
  out. Either way the run escalates with the work note
  *"No knowledge article found for this incident."*
- **Why:** It caps the cost and time of a run, and gives the reviewer the same clear
  reason every time instead of a model-written explanation.
- **Trade-off:** A question that needs more than three searches is escalated, not
  answered.

### D11 — Any helpful chunk can be suggested; the prompt names no article sections

- **Decision:** The prompt lets the agent suggest **anything** in the chunks that
  helps with the same problem: steps, checks, workarounds, causes, escalation or
  prevention guidance. It must keep the original order and use only what the
  chunks say. The rule does not name article sections.
- **Why:** A test incident (high CPU load) was escalated even though a relevant chunk
  scored 0.71, because that chunk was the article's escalation section, not its
  resolution. Not naming sections keeps the rule working when articles use a
  different structure.
- **Trade-off:** More partial suggestions (for example, escalation advice only)
  instead of escalations. Human review (D7) covers this.

### D12 — Incident text is untrusted; regex masking before the LLM

- **Decision:** Incident text is wrapped in an "untrusted, requester-submitted"
  block, and the prompt forbids following instructions inside it.
  `mask_sensitive_text` redacts tokens, API keys, auth headers, emails, phone, ID
  and card numbers in the incident and in every tool output before the LLM sees
  them.
- **Why:** Requesters can type anything, including prompt-injection attempts and
  personal data. Masking keeps that data away from the LLM provider.
- **Trade-off:** Regex masking only catches known formats. It can miss unusual
  secrets and occasionally redact harmless text.

### D13 — Passwords are masked in ServiceNow, not in Python

- **Decision:** Password keywords were removed from the Python regex. Passwords are
  to be masked by a ServiceNow **before** Business Rule, so they are never saved.
  An LLM-based check was tried first, but scoped apps cannot make outbound HTTP
  calls from a before rule, so the rule is regex-based.
- **Why:** Masking after saving still leaves the password in the database and the
  audit history. Masking before saving means it never exists in the record, the
  webhook or the agent.
- **Trade-off:** The rule's script is not in this repository (it was removed), so on
  `main` nothing in the repo masks passwords; that depends on the instance
  configuration. The regex does not cover Arabic text or passwords with no
  separator such as "is" or ":".

### D14 — Retry transient errors once; fail permanent ones cleanly

- **Decision:** `retry_policy.is_transient_error` treats timeouts, connection errors
  and HTTP 429/5xx, anywhere in the cause chain, as transient: retry once after
  10 s. Everything else fails at once. "Fail cleanly" means log it, add a
  best-effort work note, and return an error result. ServiceNow writes are also
  retried once.
- **Why:** Retrying a 401 or 404 never helps. One retry rides out short outages
  without hammering a service that is down. Failures stay visible on the incident,
  and the worker keeps serving other jobs.
- **Trade-off:** After a longer outage the event is not retried again; the work
  note shows it failed, and someone has to re-trigger it.

### D15 — One embedding model for ingestion and queries: `gemini-embedding-2`, 768-d

- **Decision:** KB chunks and search queries are both embedded with Gemini
  `gemini-embedding-2` at 768 dimensions (`ingestion/embedding.py`,
  `retriever.gemini_embedding_fn`).
- **Why:** Vectors from different models cannot be compared. Earlier, queries and
  stored chunks used different models (`-001` vs `-2`), which made the scores
  meaningless. 768 dimensions keep the collection small while retaining quality.
- **Trade-off:** Changing the model means re-seeding the whole collection.
  `GEMINI_API_KEY` is required, and the LiteLLM embedding path in
  `run_benchmark.py` still uses `-001`.

### D16 — Qdrant (cosine), Cloud by default, local container as fallback

- **Decision:** One collection, `barq_kb_chunks`, 768-d cosine, on the team's Qdrant
  Cloud cluster. `docker-compose.yml` also runs a local Qdrant used when
  `QDRANT_URL` is unset.
- **Why:** Qdrant supports payload filters (category, sys_id) and point updates,
  which KB sync needs. The cloud cluster gives everyone the same data, and the
  local container lets a new reader run the stack with no cloud account.
- **Trade-off:** The local container starts empty and must be seeded.

### D17 — LLM through the Sprints LiteLLM proxy, temperature 0

- **Decision:** LangChain `ChatOpenAI` pointed at the Sprints LiteLLM proxy
  (OpenAI-compatible) with a Gemini model (`LLM_MODEL`, with a `gemini/` prefix),
  temperature `0.0`, no client-side retries.
- **Why:** The proxy gives the team free model access behind one OpenAI-style API,
  so the model can change without code changes. Temperature 0 makes runs
  repeatable for testing and review.
- **Trade-off:** It depends on the proxy's availability. The agent reads the key as
  `OPENAI_API_KEY`, the same value as `LITELLM_API_KEY`.

### D18 — One Langfuse trace per incident, four span types, optional

- **Decision:** Each run is one trace (`incident-run-<sys_id>`) with
  `incident-fetch`, `kb-retrieval`, `agent-decision` and `servicenow-writeback`
  spans. Without keys, tracing is a no-op.
- **Why:** A mentor or developer can reconstruct any decision (what was retrieved,
  which tool was chosen, what was written) from the trace alone, without
  re-running the code.
- **Trade-off:** Traces contain incident text (the `incident-fetch` span records the
  short description as fetched, before masking) and KB chunk text, so the
  Langfuse project needs access control.

### D19 — Content-hash diff in SQLite, four idempotent sync paths

- **Decision:** KB events are handled by comparing a SHA-256 of the article body
  with the last stored hash. The paths are: delete, re-embed (new or changed body),
  metadata-only payload patch, or no-op.
- **Why:** Re-embedding costs API calls. Metadata-only edits (title, category,
  state) skip embedding entirely, and repeated events are harmless.
- **Trade-off:** The SQLite file lives in the worker container and is lost when the
  container is recreated (the next event re-embeds). The ServiceNow rule that
  sends KB events is not in this repo.

### D20 — Benchmarks score retrieval only, on a synthetic KB

- **Decision:** `pipeline_benchmark.py` measures Top-3 Hit Rate and Refusal
  Correctness with a threshold gate (`RETRIEVAL_SCORE_THRESHOLD`, 0.75), without
  calling the LLM agent. It runs against 24 synthetic fixture articles.
- **Why:** Running the real agent 30 times per benchmark would be slow and cost LLM
  calls. Retrieval quality is the main driver of answer quality, and production KB
  content was not available in the benchmark environment.
- **Trade-off:** The results (100% / 100%) describe retrieval on easy, well-matched
  data, not the live agent at 0.65. They must be re-run on real KB content before
  being trusted.

### D21 — Non-admin integration user

- **Decision:** All Table API calls use an integration user with
  `snc_platform_rest_api_access` and the scope's user role, never an admin.
  `snc_internal` is not assigned, per mentor guidance
  ([`pdi_guide.md`](pdi_guide.md)).
- **Why:** Leaked credentials can then only do what the assistant needs: read
  incidents and KB articles, and write AI fields and work notes.
- **Trade-off:** Each new table or field needs an explicit grant.

### D22 — One Docker image for API and worker; config only from `.env`

- **Decision:** A single `python:3.14-slim` image built with uv, running as a
  non-root user. Compose runs it twice (API and worker) next to Redis and Qdrant.
  All settings come from `.env` through pydantic-settings; `.env.example` holds
  placeholders only.
- **Why:** The API and worker share the same code, so one image keeps them in sync.
  Celery refuses to run cleanly as root. Keeping secrets only in a git-ignored
  `.env` means no credentials are ever committed.
- **Trade-off:** Any code change rebuilds both services. Python 3.14 is new, and some
  setups have only pre-release builds.
