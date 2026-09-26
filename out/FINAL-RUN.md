# FINAL RUN — manual as test PDF, clean state, scratch collection (2026-09-25)

Test PDF: `challenge/BARQ_IT_Service_Desk_Manual_Ed5.1.pdf` (52 pages, sha256 `c535243f…`).
Collection: `manual_test` (new, `--create-collection`). Original `kb_chunks` untouched (211 pts, verified
before/after). `.env` not modified (collection passed via `--collection` flag).
`out/` was wiped first; every file below is from this run only.

## 1. All edits made in this session

### Code
- `cli/search_pdf.py` — added `--collection` flag (was env-only). Why: tests must never touch the
  shared KB collection. Result: all searches run with `--collection manual_test`.
- `cli/run_eval.py` — added `--collection` (default `pdf_test` env `PDF_TEST_COLLECTION`); replaced all
  hardcoded `"kb_chunks"` / `"pdf_heldout"` reads; fixed the truncated-file crash (`fresh_vision` defined,
  sweep print fixed). Why: eval must work against any scratch collection. Result: full eval runs clean
  (exit 0) against `manual_test`.
- `cli/ingest_pdf.py` — no logic changes this session (dual-hash cache, lattice challenger, text-align
  probe, hint tiers from prior sessions verified unchanged: `py_compile` clean, behaviour identical).

### Docs
- `cli/PDF_INGESTION_ANALYSIS.md` — §6: new "Manual full run, clean state (collection manual_test)"
  paragraph with the numbers below; failure case 11 + item-4 note updated with fresh-cycle status
  (222→223 issue NOT present: 208→208+0); §5 checklist already used `$TEST_COL` convention.
- `out/RUNLOG.md` — fresh per-run log for this cycle (replaces wiped logs).
- This file (`out/FINAL-RUN.md`) — session summary.

### Qdrant hygiene
- Deleted 13 stale challenge points from `kb_chunks` → pristine 211 (verified 0 pdf points remain).
- Dropped obsolete `pdf_heldout`; created `manual_test` (now 208 + 13 + 10 + 5 + 5 = 241 pts).
- Regenerated the missing synthetic fixture (`Docs/pdf_ocr/barq_challenge.pdf` had been deleted from
  disk) deterministically via `cli/make_challenge_pdf.py` (seed 7) for the eval's dev-diagram rows.

## 2. Final run results (from `out/` logs)

| run | chunks | upserted | skipped | vision calls | cache hits | time |
|---|---|---|---|---|---|---|
| manual run1 | 208 (85 text / 23 ocr / 100 table) | 208 | 0 | 21 | 0 | 296.9 s |
| manual run2 | 208 | **0** | 208 | **0** | 21 | 108.7 s |
| challenge run1 | 13 | 13 | 0 | 5 | 0 | 41.6 s |
| challenge run2 | 13 | **0** | 13 | **0** | 5 | — |
| held-out A/B/C run1 | 10 / 5 / 5 | 10 / 5 / 5 | 0 | 2 / 3 / 2 | 0 | 19.9 / 28.6 / 14.3 s |
| held-out A/B/C run2 | — | **0** | all | **0** | 2 / 3 / 2 | — |

- Tables: 72 structured + 3 via vision, 0 degraded, 0 empty pages, `vision_failed=0`.
- Retired: exactly 1 point flagged (`retired=true, superseded_by="KB0010 v2"`, p23 idx1 — verified live
  in the stored payload) + 11 `retired_hint`s. Neither restart query surfaces v1 bare
  (`out/retired-check.log`).
- Searches: EN escalation 0.729→0.688 range / AR 0.698 / diagram 0.722 — expected chunk ranks #1 in
  every query (`out/search-*.log`); KB0001 query → p18 #1 (0.751).
- Eval (`out/eval/eval-run.log`, exit 0): tables dev best 20/20 (m-p30 via lattice) / held-out C 15/15
  (text-align); funnel dev 25/20/4/1 + lattice 3, held-out 5/5/0/0 + textalign 1; gate P 1.0 / R 0.071 dev,
  0 flags held-out; OCR macro fast 0.316 → selected 0.159; diagrams recall 5/9–12/16 + precision 1.0
  (0 hallucinations), transcription fallbacks node recall 12/12, 6/6, 4/4; sweep deriv 11/12 + frozen 2/2
  on every grid cell (thresholds kept, "indicative only").
- Scratch sweep set regenerated (seeds 101–110, `out/eval/scratch/`); isolated tool deps reinstalled
  after `/tmp` wipe (`pdfplumber`, `camelot`, `opencv`).
- Self-check: `pytest tests/test_pdf_selfcheck.py` → 2 passed. Secret sweep: clean (only ctor kwargs
  and masking-guidance text).

## 3. What changed last (most recent actions, in order)

1. Wiped `out/` and dropped `pdf_test`; restored `kb_chunks` to pristine 211.
2. Added `--collection` to `search_pdf.py` / `run_eval.py` (no `.env` edits).
3. Regenerated the deleted synthetic fixture; ingested manual (run1/run2) + challenge + held-out A/B/C
   (run1/run2 each) into `manual_test`.
4. Refreshed searches, retired check, inspect, full eval, scratch sweep set.
5. Updated analysis doc (§6 manual_test paragraph, item-4/11 status) and wrote `out/RUNLOG.md`.
6. Verified: `kb_chunks`=211 untouched, `manual_test`=241 test points, secret sweep clean.

## 4. Open items (unchanged)

- Qdrant dashboard screenshot → `out/screenshots/` (needs a human browser; steps in analysis §5c).
- Known residuals (documented, not shipped as hacks): gate misses misaligned grids; B-nested structure
  loss (content kept); chart/org summaries absent (transcriptions kept); whole-page-scan Arabic order;
  first-ingest vision nondeterminism; thin sweep (N=12+2).
