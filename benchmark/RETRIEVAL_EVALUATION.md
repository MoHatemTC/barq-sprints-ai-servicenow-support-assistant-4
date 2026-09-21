# S2.6 — Retrieval Evaluation (methodology + how to run)

Benchmark-only harness. It does **not** modify agent, embedding, or
retrieval code — it calls the existing `retrieve()` with incident text.

## Files

| File | Purpose |
|---|---|
| `benchmark_dataset.json` | 30 cases: 20 answerable + 10 negative controls |
| `run_benchmark.py` | Runner: incident-text queries, ungated retrieval + Python gate, threshold sweep |
| `benchmark_results.json` | Generated output (provenance, metrics, distributions, per-case detail) |
| `RETRIEVAL_EVALUATION.md` | This report: methodology + threshold guidance |
| `S2.6_TASK.md` | Task-to-deliverable map |

## Methodology

1. Each case is queried with **incident text** (`short_description +
   description`) — the runtime input — filtered by its `category`.
2. Retrieval runs **ungated** (`threshold=0.0`) so ranking is recorded even
   below the bar; the evaluation gate is then applied in Python (identical
   semantics to `retrieve(threshold=…)`).
3. Answerable PASS = expected article in top-K **and** best score ≥ threshold.
   Negative PASS = best score < threshold (clean refusal).
4. A 0.50–0.90 threshold sweep is recomputed from the **same pass** (no
   re-querying) and saved in `threshold_sweep`.

## How to run

```bash
# Offline plumbing check (SHA-256 stub — validates the harness, not quality)
uv run python benchmark/run_benchmark.py

# Real evaluation (Gemini gemini-embedding-001, requires GEMINI_API_KEY
# and a 768-dim collection populated by the S2.2 ingestion pipeline)
uv run python benchmark/run_benchmark.py --real

# Custom threshold / top-k
uv run python benchmark/run_benchmark.py --threshold 0.65 --real --top-k 5
```

Requires `.env` with `QDRANT_URL` (+ `QDRANT_API_KEY` for Cloud) and, for
`--real`, `GEMINI_API_KEY` — or the `LITELLM_BASE_URL` / `LITELLM_API_KEY`
proxy fallback (768-dim `dimensions` request), which the runner picks
automatically and records in provenance. Collection defaults to
`settings.qdrant_collection_name`.

> Stub mode is a plumbing check only: the stub emits 384-dim vectors, so it
> cannot query a 768-dim collection (Qdrant rejects the dimension mismatch).
> It only runs where the collection was built with stub vectors.

## Results (real embeddings, 2026-09-21)

Setup: 33 ServiceNow articles → 211 chunks → 768-dim vectors in `kb_chunks`,
queried with incident text, `top_k=5`, evaluated threshold 0.75 (config default).

| Metric | Result |
|---|---|
| Hit Rate @ 0.75 | 65.0% (13/20) |
| Refusal Correctness @ 0.75 | 100.0% (10/10) |
| Ranking accuracy (threshold ignored) | **100% (20/20)** — every miss ranked the expected article first |

Distributions: answerable 0.6887–0.8721 (avg 0.7767); negative
0.4874–0.6592 (avg 0.5917); separation margin +0.0295.

Threshold sweep (same retrieval pass):

| Threshold | Hit Rate | Refusal |
|---|---|---|
| 0.50 | 100% | 10% |
| 0.60 | 100% | 50% |
| 0.65 | 100% | 70% |
| **0.70** | **95%** | **100%** |
| 0.75 (current default) | 65% | 100% |

Full detail: `benchmark_results.json` (`provenance`, `metrics`,
`score_distribution`, `threshold_sweep`, per-case details).

## Threshold guidance

- **Recommendation from the 2026-09-21 run: lower
  `RETRIEVAL_SCORE_THRESHOLD` 0.75 → 0.70.** It is Pareto-optimal on measured
  data: 95% hit + 100% refusal, vs 65%/100% at 0.75. The only sub-threshold
  answerable at 0.70 (`INC-BENCH-012`, 0.6887) still ranks correctly and fails
  safe (refusal, not a wrong answer). The 0.75 bar refuses 7 correctly-ranked
  articles (0.69–0.74, incl. paraphrase/typo variants) — genuine traffic lost.
- Re-run `--real` after any KB or embedding change before touching the
  threshold; the sweep in `benchmark_results.json` shows the tradeoff without
  re-querying.
- The stub run is a plumbing check only: stub scores measure exact-text
  overlap, not semantic relevance — do not tune the production threshold
  from stub numbers.
