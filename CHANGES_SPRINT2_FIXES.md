# Sprint 2.6+ Code Review Fixes Summary

**Date:** September 19, 2026 (updated — round 2: external review findings)
**Branch:** `feature/sprint-2-fixes`
**Base Commit:** `main`

---

## Overview

This document summarizes all edits made in response to the Sprint 2 code review. The fixes address critical bugs, code quality issues, security hardening, and prepare the codebase for Sprint 3 (real embeddings).

**Round 2** addresses the external review of `b488c67`, which found that the benchmark measured exact-text lookup (SHA-256 stub scores `1.0000` on byte-identical queries), the Gemini embedding path was async-under-a-sync-type and unreachable, the test suite broke on a default install, payload indexes were only created for new collections, doc links used Codespaces-only `file:///` URLs, and the HTML sanitizer used bypassable regexes. All code-side findings are fixed below.

---

## Files Modified

| File | Type | Description |
|------|------|-------------|
| `src/barq_ai_support/ingestion/embed_and_store.py` | **Bug Fix + Refactor** | Fixed `metadata` NameError, replaced deprecated `recreate_collection`, idempotent payload indexes, `vector_size` param |
| `src/barq_ai_support/ingestion/chunker.py` | **Security** | HTML sanitization via BeautifulSoup `decompose()` (replaced bypassable regexes) |
| `src/barq_ai_support/embeddings.py` | **New File (rewritten round 2)** | Sync-first embeddings: sync Gemini via LiteLLM, explicit stub, model/dim helpers |
| `src/barq_ai_support/retrieval/retriever.py` | **Refactor** | Single canonical stub (re-export), dim-mismatch fail-fast, richer `RetrievedChunk` dataclass |
| `src/barq_ai_support/agent/tools.py` | **Feature** | `search_knowledge_base` now calls real `retrieve()` with category filter |
| `src/barq_ai_support/agent/executor.py` | **Robustness** | Guarded `langchain_openai` import — module loads without the optional dep |
| `benchmark/run_benchmark.py` | **Feature (rewritten round 2)** | Incident-text queries, `--real`/`--top-k` flags, provenance record, stub warning |
| `benchmark/RETRIEVAL_EVALUATION.md` | **Docs** | Relative links, methodology note marking pre-fix numbers as plumbing-only |
| `tests/test_agent.py` | **Tests** | Combined live-LLM skip, 2 new fake-model executor tests (no network) |
| `pyproject.toml` | **Config** | `langchain-openai` optional `[agent]` extra + added to `dev` group |

---

## Detailed Changes

### 1. Critical Bug Fix: `embed_and_store.py` — `metadata` NameError

**Location:** Line 90 (was line 73-74)

**Before:**
```python
vector = embedding_fn(text)
raw_cat = metadata.get("kb_category")  # NameError: metadata not defined
```

**After:**
```python
metadata = chunk["metadata"]  # ADDED
vector = embedding_fn(text)
raw_cat = metadata.get("kb_category")
```

**Impact:** Ingestion pipeline was completely broken — would raise `NameError` on every run.

---

### 2. Deprecated API Replacement: `recreate_collection` → `delete_collection` + `create_collection`

**Location:** `embed_and_store.py`

**Before:**
```python
if recreate_collection:
    client.recreate_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(size=384, distance=Distance.COSINE),
    )
```

**After:**
```python
if recreate_collection:
    print(f"Re-creating collection '{collection_name}'...")
    try:
        client.delete_collection(collection_name=collection_name)
    except Exception:
        pass  # Collection may not exist
    ensure_collection_exists(
        client, collection_name=collection_name, vector_size=vector_size
    )
```

**Why:** `recreate_collection` is deprecated in `qdrant-client>=1.19.1`. The new approach is safer and creates payload indexes automatically.

---

### 3. Payload Indexes for Category Filtering (round 2: now idempotent)

**Location:** `embed_and_store.py` — new `ensure_payload_indexes()`, called on **every** run

**Round 1** created indexes only inside `if not exists`, so pre-existing collections never got the `category` index the retriever filters on → Qdrant Cloud rejected the filter with `Index required but not found` (400). **Round 2** fix:

```python
def ensure_payload_indexes(
    client: QdrantClient,
    collection_name: str = settings.qdrant_collection_name,
) -> None:
    """Create keyword indexes for filterable payload fields, idempotently."""
    for field_name in ("category", "article_number", "sys_id"):
        try:
            client.create_payload_index(
                collection_name=collection_name,
                field_name=field_name,
                field_schema=PayloadSchemaType.KEYWORD,
            )
        except Exception as e:
            print(f"Note: payload index for '{field_name}' not created: {e}")
```

`ensure_collection_exists()` now calls it unconditionally after the exists-check. `ingest_chunks_to_qdrant()` also gained a `vector_size` parameter (default 384) so Gemini ingestion can create a 768-dim collection.

**Impact:** Fixes the benchmark 400 error on existing collections. Enables fast filtering by category in production.

---

### 4. HTML Sanitization Before Chunking (round 2: soup-based, not regex)

**File:** `src/barq_ai_support/ingestion/chunker.py`

**Round 1** used regexes, which missed unquoted handlers (e.g. `<div onerror=alert(1)>`). **Round 2** replacement:

```python
def sanitize_html(html: str) -> str:
    """Strip active content (scripts, styles) before text extraction.

    Implemented with BeautifulSoup instead of regexes so unquoted event
    handlers (e.g. <div onerror=alert(1)>) and nested markup can't slip
    through. Returns sanitized HTML safe for downstream parsing.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return str(soup)
```

**Security:** Prevents XSS payloads in ServiceNow `description` field from persisting into vector store. Verified: script/style content removed, text preserved.

---

### 5. Embeddings Module: Sync-First Real + Stub Support (round 2: rewritten)

**File:** `src/barq_ai_support/embeddings.py`

**Round 1** had two wiring bugs found in review: `gemini_embedding_fn` was `async` but returned under a sync `Callable[[str], list[float]]` type (unusable by `retrieve()`/ingestion), and `sync_embedding_fn` always returned the stub so `LITELLM_API_KEY` changed nothing. **Round 2** rewrite:

- `default_embedding_fn` — SHA-256 stub (384-dim) for tests/offline. Docstring now states it carries no semantic meaning and must never calibrate thresholds.
- `gemini_embedding_fn` — **sync** real embedding via LiteLLM → Gemini (`gemini-embedding-001`, 768-dim), httpx sync client. Drop-in for `retrieve()`/ingestion.
- `gemini_embedding_fn_async` — separate async variant for async callers.
- `get_embedding_fn(use_real=None)` — returns **sync** callables only. `True` = real (raises without key), `False` = explicit stub (hermetic tests), `None` = auto-detect.
- `embedding_model_name(fn)` / `embedding_dim(fn)` — provenance helpers for the benchmark.
- `sync_embedding_fn` — kept as an explicit-stub alias for backwards compatibility.

**Usage:**
```python
from barq_ai_support.embeddings import get_embedding_fn
embed_fn = get_embedding_fn(use_real=True)   # Gemini, raises if key missing
embed_fn = get_embedding_fn(use_real=False)  # deterministic stub (tests)
```

---

### 6. Retriever Updates (round 2: dedup + dim fail-fast)

**File:** `src/barq_ai_support/retrieval/retriever.py`

| Change | Detail |
|--------|--------|
| Single canonical stub | The duplicate `default_embedding_fn` body was removed; the import from `embeddings.py` doubles as a re-export, so `from retriever import default_embedding_fn` (tests) still resolves |
| Dim-mismatch fail-fast | `retrieve()` compares the query vector length against the collection's configured vector size and raises a clear `ValueError` (384-vs-768) instead of a cryptic Qdrant 400 |
| Default `embedding_fn=sync_embedding_fn` | Explicit stub — offline-safe; real embeddings are opt-in via `get_embedding_fn(use_real=True)` |
| `RetrievedChunk` adds `short_description`, `heading_path` | Populated from payload; agent tools need them for citations |

**Why:** Agent tools need `short_description` and `heading_path` for citations; the dim check prevents silently querying a 768-dim collection with 384-dim vectors once real ingestion lands.

---

### 7. Agent Tool: Real Retrieval with Category Filter

**File:** `src/barq_ai_support/agent/tools.py`

**Before (hardcoded stubs):**
```python
@tool("search_knowledge_base", args_schema=KnowledgeBaseSearchInput)
def search_knowledge_base(query: str) -> list[dict]:
    return [
        {"article_id": "KB0001", "title": "...", "score": 0.85, ...},
        {"article_id": "KB0005", "title": "...", "score": 0.82, ...},
    ]
```

**After (real retrieval):**
```python
class KnowledgeBaseSearchInput(BaseModel):
    query: str = Field(..., description="Search text describing the incident's symptom.")
    category: str | None = Field(default=None, description="Optional category to filter results...")

@tool("search_knowledge_base", args_schema=KnowledgeBaseSearchInput)
def search_knowledge_base(query: str, category: str | None = None) -> list[dict]:
    result = retrieve(query=query, category=category, score_threshold=0.75)
    if not result.ok:
        return []
    return [
        {
            "article_id": c.article_number,
            "title": c.short_description or "",
            "section": " > ".join(c.heading_path) if c.heading_path else "",
            "text": c.text,
            "score": c.score,
        }
        for c in result.chunks
    ]
```

**Impact:** Agent now searches real Qdrant collection with threshold gating + category filtering.

---

### 8. Executor: Guarded Optional Import (round 2: new)

**File:** `src/barq_ai_support/agent/executor.py`

Review found `from langchain_openai import ChatOpenAI` at module top broke **all** test collection on a default install. Fix:

```python
try:
    from langchain_openai import ChatOpenAI
except ImportError:  # pragma: no cover - optional dependency
    ChatOpenAI = None  # type: ignore[assignment,misc]
```

`get_llm()` raises an informative `RuntimeError` (install via `uv sync --extra agent`) only when actually called; `SupportAgentExecutor.__init__` accepts any LLM object. Verified the module imports cleanly with `langchain_openai` blocked.

---

### 9. Benchmark: Incident-Text Queries + Provenance (round 2: rewritten)

**File:** `benchmark/run_benchmark.py`

Review proved the old runner measured exact-text lookup: every answerable case scored `1.0000` because `item["query"]` was a byte-identical copy of the indexed KB chunk (verified: identical → 1.0, +1 trailing space → ~0.15, incident text → ~0.0). The old 0.75 threshold would refuse every real incident. Fixes:

- **New `build_benchmark_query()`** — queries with `incident.short_description + description` (the runtime input the agent receives). The legacy `query` field is fallback-only and flagged per-case as `query_source: incident_text | legacy_query_field`.
- **`--real` / `--top-k` flags + positional threshold** via argparse (backwards compatible: `run_benchmark.py [threshold]` still works).
- **`provenance` block in results JSON** — `{embedding_model, embedding_dim, collection, query_source, top_k}` so numbers can't be mistaken for another setup.
- **Stub warning** — stub runs print that scores measure exact-text overlap only and point at `--real`.
- **Crash-safe distribution stats** — handles empty score lists instead of `min()` on `[]`; per-case `query_text` recorded.

```bash
# Offline / pipeline check (stub — validates plumbing, not quality)
uv run python benchmark/run_benchmark.py
# Real evaluation (requires LITELLM_API_KEY + 768-dim Gemini collection)
uv run python benchmark/run_benchmark.py --real
```

**Impact:** Benchmark now evaluates what the agent actually receives. Threshold must be re-calibrated from a `--real` run — the old 0.75 value is retired (see doc note below).

---

### 10. Evaluation Doc: Relative Links + Stale-Numbers Notice (round 2: new)

**File:** `benchmark/RETRIEVAL_EVALUATION.md`

- `file:///workspaces/...` Codespaces links → relative (`benchmark_dataset.json`, `run_benchmark.py`, `benchmark_results.json`).
- Header methodology note: the published tables came from the pre-fix runner (KB-chunk queries + stub) and validate harness plumbing only; re-run with `--real` before calibrating any threshold.

---

### 11. Dependencies: `langchain-openai` (round 2: extended)

**File:** `pyproject.toml`

Round 1 moved `langchain-openai` to `[project.optional-dependencies].agent`, which broke collection (`tests/test_agent.py` imports the executor at module load). Round 2 applies the reviewer's first option **plus** the import guard:

```toml
[project.optional-dependencies]
agent = ["langchain-openai>=1.6.2"]   # production agent installs

[dependency-groups]
dev = [
    "pytest>=9.1.1",
    "langchain-openai>=1.6.2",        # tests always collectible
]
```

Belt and suspenders: dev installs can collect, and even without the package the executor module still imports (see §8).

**Usage:**
```bash
uv sync                  # core only
uv sync --extra agent    # + live agent runtime
```

---

### 12. Agent Tests: Live Skips + Fake-Model Coverage (round 2: new)

**File:** `tests/test_agent.py`

- Combined `needs_live_llm` skip: live tests skip when the package **or** the key is missing (previously 401 failures with `--extra agent` and no key).
- Two new offline tests with a minimal `_FakeToolCallingModel` (no network): executor terminates on `submit_final_answer` (status `final_answer`, citation preserved) and on `request_human_review` (status `human_review_requested`).

---

## Test Results

All 24 tests pass (22 existing + 2 new fake-model executor tests; live LLM tests skip without a key):

```
tests/test_agent.py ........      [ 33%]  # 8 tests (2 live skipped without LITELLM_API_KEY, 2 fake-model)
tests/test_chunking.py ....       [ 50%]  # 4 tests
tests/test_retrieval.py ........  [ 83%]  # 8 tests
tests/test_webhook.py ....        [100%]  # 4 tests
======= 24 passed in ~19s =======
```

---

## Sprint 3 Readiness Checklist

| Task | Status |
|------|--------|
| Fix critical `metadata` bug | ✅ Done |
| Replace deprecated `recreate_collection` | ✅ Done |
| Payload indexes idempotent on every run | ✅ Done (round 2) |
| HTML sanitization via soup `decompose()` | ✅ Done (round 2) |
| Sync-first embeddings module (stub + Gemini) | ✅ Done (round 2) |
| Retriever dim-mismatch fail-fast | ✅ Done (round 2) |
| Agent tool calls real retrieval | ✅ Done |
| Executor imports without optional dep | ✅ Done (round 2) |
| Benchmark queries incident text + provenance | ✅ Done (round 2) |
| Optional `langchain-openai` + dev group | ✅ Done (round 2) |
| Fake-model executor tests (no network) | ✅ Done (round 2) |
| Doc links relative + stale-numbers notice | ✅ Done (round 2) |
| **Re-ingest with real embeddings (768-dim)** | ✅ Done — 33 articles → 211 Gemini 768-dim chunks in `kb_chunks` |
| **Re-run benchmark `--real`, recalibrate threshold** | ✅ Done — **0.65: 95% hit, 100% refusal** (old 0.75 retired: 55% hit) |
| **Expand benchmark: paraphrases + near-miss negatives** | ✅ Done — 30 cases (20 answerable + 10 negative) |

---

## Breaking Changes

| Change | Migration |
|--------|-----------|
| `retrieve()` default embedding fn is explicit stub `sync_embedding_fn` | No code change needed — same signature; pass `get_embedding_fn(use_real=True)` for real |
| `gemini_embedding_fn` is now sync; async moved to `gemini_embedding_fn_async` | Update any `await gemini_embedding_fn(...)` call sites |
| `default_embedding_fn` canonical home is `embeddings.py` (re-exported from `retriever`) | Existing `from retriever import default_embedding_fn` imports still work |
| `RetrievedChunk` has new fields (`short_description`, `heading_path`) | Backward compatible (new fields optional) |
| `search_knowledge_base` tool signature added `category` param | Optional, defaults to `None` |
| `ingest_chunks_to_qdrant` gained `vector_size` param | Defaults to 384; pass 768 for Gemini ingestion |
| Benchmark results JSON gained `provenance` block; per-case `query_text`/`query_source` | Consumers reading old result files should handle new keys |
| `langchain-openai` no longer auto-installed | `uv sync` for core; `uv sync --extra agent` for live agent |

---

## How to Verify

```bash
# 1. Run all tests (collectible on default install; live LLM skips without key)
uv run pytest -v

# 2. Check ingestion works (requires .env with Qdrant creds; 768-dim for Gemini)
uv run python src/barq_ai_support/ingestion/manual_run.py

# 3. Run benchmark, stub (plumbing check) and real (quality signal)
uv run python benchmark/run_benchmark.py
uv run python benchmark/run_benchmark.py --real

# 4. Live agent tests only (requires LITELLM_API_KEY)
LITELLM_API_KEY=xxx uv run pytest tests/test_agent.py -v
```

---

## Notes for Reviewers

1. **The `metadata` bug was silent in review** — it only manifests at runtime during ingestion. The test suite uses in-memory Qdrant with pre-seeded data, bypassing `ingest_chunks_to_qdrant()`.

2. **The old 100% benchmark scores measured exact-text lookup, not retrieval** — SHA-256 stub scores `1.0000` only on byte-identical text (verified: +1 trailing space → ~0.15, incident phrasing → ~0.0). The runner now queries with incident text and the old 0.75 threshold is retired pending a `--real` calibration run.

3. **Category filtering now works end-to-end** — index ensured on every run (even pre-existing collections), filter passed at query time, benchmark validates both.

4. **No `#16` files exist in any remote branch** (`ingestion/embedding.py`, `ingestion/qdrant_store.py` searched across all remotes) — nothing to deduplicate against locally. If #16 lands, the seam is one line: `get_embedding_fn` → its `create_embedding`, with matching `vector_size`.

5. **PR hygiene (title, squash, scope split) is GitHub-side** — needs to happen in the PR UI, not the working tree.
