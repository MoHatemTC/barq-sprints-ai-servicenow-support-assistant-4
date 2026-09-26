# What is new (S3.5 PDF ingestion CLI — this round)

## The route (end to end, nothing skipped)
`challenge/BARQ_IT_Service_Desk_Manual_Ed5.1.pdf` (52 pp)
→ `cli/ingest_pdf.py` STEP 1–7 (open → normalise → pass 1 blocks → pass 2 extract →
embed+upsert → SUCCESS) → Qdrant `manual_test`, exactly 208 points
(`doc_id=pdf-barq-it-service-desk-manual-ed5-1`)
→ `cli/search_pdf.py` (dense → optional LLM rerank → TRUE/FALSE verdict + distractor report)
→ evidence in `out/` + 31 screenshots in `cli/screenshots/`.
`kb_chunks` untouched at 211 throughout. `.env` never modified (all config via env names).

## New in code
- **`cli/ingest_pdf.py`**: visible `[STEP 1/7]…[STEP 7/7]` log on every run;
  `SUCCESS <doc> chunks/upserted/skipped/vision_calls/seconds` on clean finish;
  `FAIL …` on fatal paths (file not found, missing `EMBEDDING_MODEL`, unreadable PDF)
  and on runs with losses (`vision_failed/pages_empty/tables_degraded`).
- **`cli/search_pdf.py`**: `--doc-id` (per-document proof inside mixed collections);
  `--rerank` (LLM listwise, `RERANK_MODEL`→`VISION_MODEL` fallback, prints `was=#N`);
  `--expect-page/--expect-type` → `RETRIEVED/RERANKED verdict TRUE/FALSE`, TRUE hit
  rank/score/gap, and every `DISTRACTOR scoring higher than TRUE`.
- **`cli/run_eval.py`**: missing-fixture guard — exits 2 with
  `FAIL eval fixtures missing: …` + regenerate commands instead of a traceback.
- **`cli/PDF_INGESTION_ANALYSIS.md`** (recreated manual-grounded after cleanup deletion):
  tool comparison with sample outputs (§1: lattice contention, OCR selection, diagram
  before/after, embedding canonicalisation) + 6 failure cases F1–F6 with unhandled
  output and applied workarounds (F1 fence strip, F2 non-JSON fallback, F3 hash
  canonicalisation, F4 Qdrant-400 client-side filter; F5 partial diagram flip and
  F6 cache-miss trigger as accepted residuals).
- **`tests/test_pdf_selfcheck.py`**: retargeted from deleted held-out set to the manual;
  `pytest` → 2 passed, zero API cost.

## New in docs/runbook
- **`script_run_cli.md`** (repo root): copy-paste runbook — ingest, idempotent re-run,
  dry-run, search, reranked runs, unrunnable-eval notice, and §6 missing-screenshot
  shot list with exact commands + expected scores.
- **`out/RUNLOG.md`**: run numbers, retired flag, manual-only scope change, recapture log,
  and the challenge-document justification (manual covers nested tables, Arabic-in-image,
  diagrams; v3 update: 2 structured summaries live, p37/p38 stay text — accepted residual).
- **`cli/PDF_INGESTION_ANALYSIS.md`**: recreated manual-grounded (was deleted in cleanup):
  tool comparison with samples + F1–F6 failure cases (details above).
- **`cli/ingest_pdf.py` diagram + determinism round (prompt v3)**: flow figures now route
  to structured summaries (`diagrams_summarised` 0 → 2: p39, p40); fence sanitiser
  (0 fenced in 21 fresh analyses); whitespace-canonicalised `embed_text` after measured
  3/206 rerun churn (canon-run2: `upserted=0 skipped=206`); retrieval ranks identical.

## New in evidence
- **`out/` 42M → 368K**: recaptured in the final 208-point state —
  `manual-ingest-rerun.log`, `search-en.log`, `search-en-manualonly.log`, `search-ar.log`,
  `search-diagram.log`, `search-kb0010.log`, `search-kb0001.log`,
  `qdrant-payloads.log`, `retired-check.log` (exactly 1 `retired=true`: p23 idx1 →
  `KB0010 v2`, + 11 hints, live). Secret sweep clean.
- **`cli/screenshots/` 31 files, all renamed by content**: 7 ingest, 12 search/EN+KB,
  5 AR, 3 verdict-rerank (incl. the honest FALSE + 4 distractors), 1 diagram-empty,
  5 inspect payloads, 1 shell history. 4 shots still show deleted challenge points —
  kept as historical, listed in `script_run_cli.md` §6.

## Proven this round (measured, not claimed)
- Idempotency: reruns → `upserted=0 skipped=206` (206 after v3 re-chunking); redump of
  `manual-chunks.jsonl` byte-identical; collection count stable across runs (no duplication:
  `uuid5` IDs + content-hash skip + stale prune).
- Retrieval: escalation EN 0.738 / AR 0.651 → p14 #1; KB0001 0.684 → p18 #1;
  KB0010 0.626 → p24 v2 #1 (v1 never bare); KB0001 card table #5 (0.628, +0.068 gap);
  diagram flow query → p39 #1 (0.628), p40 #2.
- Rerank: order-preserving on all article queries; verdict layer reports it instead of
  hiding it.
