# Run the CLI on a PDF (manual-only)

Script: `cli/ingest_pdf.py` on `challenge/BARQ_IT_Service_Desk_Manual_Ed5.1.pdf` (52 pp).
Env comes from `.env` (never pass keys on the CLI):
`LITELLM_BASE_URL`, `LITELLM_API_KEY`, `EMBEDDING_MODEL`, `EMBEDDING_DIM=768`,
`VISION_MODEL`, `QDRANT_URL`, `QDRANT_API_KEY`. Tests use `--collection manual_test`
(never the shared `kb_chunks`, currently 211 pts).

## 1. Full ingest (first run)

```bash
python cli/ingest_pdf.py challenge/BARQ_IT_Service_Desk_Manual_Ed5.1.pdf \
  --collection manual_test --create-collection --json-summary \
  --dump-chunks out/manual-chunks.jsonl 2>&1 | tee out/manual-ingest-run1.log
```

Expected terminal (measured 2026-09-25):

```
INFO [STEP 1/7] opened BARQ_IT_Service_Desk_Manual_Ed5.1.pdf: 52 pages, doc_id=pdf-barq-it-service-desk-manual-ed5-1
INFO [STEP 2/7] pages normalised: 52 pages upright (rotation reset)
INFO [STEP 3/7] pass 1: scanning text blocks + table rects (no LLM, no OCR) ...
INFO [STEP 4/7] pass 2: extracting text -> tables -> images/vision per page ...
INFO [STEP 5/7] extraction done: 208 chunks (85 text / 100 table / 23 image_ocr)
INFO [STEP 6/7] embed + upsert into collection 'manual_test' (dry_run=False) ...
INFO [STEP 7/7] done: upserted=208 skipped=0 pruned=0
INFO SUCCESS pdf-barq-it-service-desk-manual-ed5-1: 208 chunks (upserted=208 skipped=0 pruned=0 vision_calls=21) in 296.9s
```

Fail paths print `ERROR FAIL ...` (file not found, missing `EMBEDDING_MODEL`,
unreadable PDF); a run with losses prints
`ERROR FAIL completed with losses: vision_failed=N pages_empty=N tables_degraded=N`.

## 2. Re-run (idempotency proof)

```bash
python cli/ingest_pdf.py challenge/BARQ_IT_Service_Desk_Manual_Ed5.1.pdf \
  --collection manual_test --json-summary 2>&1 | tee out/manual-ingest-rerun.log
```

Expected: `"upserted": 0, "unchanged_skipped": 208` (vision calls 0 — cache hits).
Re-runs never duplicate: point IDs are deterministic (`uuid5`), hashes are compared,
stale IDs pruned. Collection holds exactly 208 manual points.

## 3. Parse-only (no Qdrant, no embeddings)

```bash
python cli/ingest_pdf.py challenge/BARQ_IT_Service_Desk_Manual_Ed5.1.pdf \
  --pages 1-2 --dry-run --no-vision --json-summary
```

## 4. Search what was indexed

```bash
python cli/search_pdf.py --collection manual_test \
  --doc-id pdf-barq-it-service-desk-manual-ed5-1 \
  "who receives an escalation rejected twice"
python cli/search_pdf.py --collection manual_test --inspect --top-k 3 --full
```

Expected top hit: `#1 score=0.736 ... BARQ_IT_Service_Desk_Manual_Ed5.1.pdf page=14 chunk=1`
(§4 service-owner rule).

### Reranked run (retrieved vs reranked vs truth)

```bash
python cli/search_pdf.py --collection manual_test \
  --doc-id pdf-barq-it-service-desk-manual-ed5-1 \
  --rerank --expect-page 18 "VPN says authentication failed since password reset"
```

Every run prints: `RETRIEVED (dense, Qdrant order)` with scores,
`RETRIEVED verdict (dense #1 == expected pN): TRUE/FALSE` plus the TRUE hit's
rank/score/gap and any `DISTRACTORS scoring higher than TRUE`, then the same trio
for `RERANKED (LLM order)` with `was=#N` positions. Omit `--expect-page` and it
prints `RERANKED truth: #1 is pN <type>` instead. Rerank uses `RERANK_MODEL`
(falls back to `VISION_MODEL`) from `.env`.

## 5. Eval — NOT runnable (fixtures removed 2026-09-26)

`cli/run_eval.py` requires the deleted synthetics and exits 2 with
`FAIL eval fixtures missing: ...`. Frozen evidence deleted — manual-only by owner decision.

## 6. Missing screenshots (take these — shell has no display)

Existing shots in `cli/screenshots/` cover the ingest run, EN search, KB0001 rerank,
and inspect payloads. Still missing:

```bash
# 6a. AR search (Arabic path) → expect p14 #1 (0.652)
python cli/search_pdf.py --collection manual_test \
  --doc-id pdf-barq-it-service-desk-manual-ed5-1 "ما الذي يحدث عند رفض التصعيد مرتين"

# 6b. KB0010 (v2 surfaces) → expect p24 #1 (0.637); flag proof lives in out/retired-check.log
python cli/search_pdf.py --collection manual_test \
  --doc-id pdf-barq-it-service-desk-manual-ed5-1 "Order service returning 500s under load"

# 6c. New verdict format (TRUE hit + DISTRACTORS) → expect verdict FALSE, 4 distractors above TRUE
python cli/search_pdf.py --collection manual_test \
  --doc-id pdf-barq-it-service-desk-manual-ed5-1 --rerank --expect-page 18 --expect-type table \
  "VPN says authentication failed since password reset"

# 6d. Diagram query (now returns 2 manual diagram chunks since prompt v3)
python cli/search_pdf.py --collection manual_test \
  --doc-id pdf-barq-it-service-desk-manual-ed5-1 \
  "steps in the escalation flow when not resolved in an hour" --type diagram
```
Expect p39 #1 (0.628), p40 #2 (0.558). (Pre-v3 this returned zero hits.)

Stale (show deleted challenge points, keep as historical or delete):
`search-en-escalation-mixed-top3.png`, `search-en-escalation-mixed-repeat.png`,
`inspect-payload-point2-challenge-rotated.png`, `inspect-payload-point2-rotation-meta.png`.
