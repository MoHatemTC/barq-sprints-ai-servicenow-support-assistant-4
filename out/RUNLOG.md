# RUNLOG — manual full run, clean state (2026-09-25)

Input: `challenge/BARQ_IT_Service_Desk_Manual_Ed5.1.pdf` (52 pages, sha256 `c535243f…`).
Collection: `manual_test` (new, `--create-collection`). Original `kb_chunks` untouched (211 pts).
`.env` not modified (collection passed via `--collection` flag).

## Run 1 (`out/manual-ingest-run1.log`, dump `out/manual-chunks.jsonl`)
- 208 chunks: 85 text, 23 image_ocr, 100 table. 0 empty pages.
- 72 tables structured + 3 via vision (`tables_via_vision=3`), 0 degraded.
- 21 vision calls, 0 cache hits (cold), 0 failed. 208 upserted, 296.9 s.
- 1 chunk flagged `retired=true, superseded_by="KB0010 v2"` (p23 idx1, verified live);
  11 `retired_hint`s.

## Run 2 (`out/manual-ingest-run2.log`)
- 208 skipped, **0 upserted**, 0 vision calls, 21 cache hits, 108.7 s. Idempotent.

## Retrieval (`out/search-*.log`, `out/retired-check.log`, `out/qdrant-payloads.log`)
- Escalation (EN 0.729 / AR 0.652): §4 "rejected escalation → service owner" chunk ranks #1.
- KB0010 pool query: v2 "Do not restart" chunk ranks top; v1 chunk (p23 idx1, 0.622) carries
  `retired=true` in its stored payload — never surfaced bare.
- VPN/KB0001 query: p18 chunks rank #1 (0.751).
- Inspect shows `source_type/doc_id/source_filename/page_number` on every point.

## Errors seen
- Qdrant 400 filtering on unindexed bool key `retired` (fresh collection has only the CLI's
  keyword indexes): worked around client-side; not a data problem, no fix needed in code.
- No crashes, no empty pages, no degraded tables, `vision_failed=0` throughout.

## Scope change (2026-09-26): manual-only
- Synthetic fixtures (`challenge/barq_challenge.pdf`, `challenge-heldout/`) deleted from disk
  per owner decision — only `challenge/BARQ_IT_Service_Desk_Manual_Ed5.1.pdf` is used.
- Their 33 points pruned from `manual_test` (13 challenge + 10/5/5 held-out); collection now
  holds exactly the 208 manual points. `kb_chunks` still 211.
- Frozen challenge/held-out evidence deleted (all `heldout-*`, `ingest-run*`,
  `chunks.vision.jsonl`, `eval/`): only the manual set remains (`manual-chunks.jsonl`,
  `manual-kb-index.md`) plus docs. `cli/PDF_INGESTION_ANALYSIS.md` §2–5 text is retained
  as the written record; its referenced data files no longer exist on disk.

## Challenge document justification (for the PR)
The manual itself is the challenge document. It natively contains three of the four
hostile features: nested/intersecting tables (§5 catalogue cards with nested Owner/Group/Window grids, escalation matrix with rowspans, catalogue spanning pages),
Arabic-in-image (KB0005 archived scan p18, Arabic run report p44 — both recovered via the
Tesseract ara+eng + vision selection path), and infographics/diagrams (§11 flow figures,
§9 whiteboard photo, desk card — transcribed as image_ocr text). Residual gap, stated
plainly: the manual has 0 rotated pages and yields 0 diagram-summary chunks, so
orientation-normalisation and flow-summary retrieval currently have no in-PR live proof —
only the frozen analysis text. No synthetic stand-in will be generated; this gap is
accepted by owner decision.
- Manual-only search re-verified after pruning: escalation query → p14 #1 (0.736), all 5 hits
  manual (`out/search-en-manualonly.log`).
- Recaptured terminal logs pruned 2026-09-26, then recaptured same day in the final
  208-point state: `out/manual-ingest-rerun.log` (STEP 1–7, SUCCESS, upserted=0/skipped=208),
  `out/search-en.log`, `out/search-en-manualonly.log`, `out/search-ar.log`,
  `out/search-diagram.log` (zero hits — the manual yields 0 diagram chunks, honest empty),
  `out/search-kb0010.log`, `out/search-kb0001.log`, `out/qdrant-payloads.log`
  (`source_type/doc_id/source_filename/page_number` on every point), `out/retired-check.log`
  (exactly 1 `retired=true` point: p23 idx1, `superseded_by="KB0010 v2"` + 11 hints, live).
  Secret sweep clean (`sk-` hits are `desk-manual` substrings only). Redump of
  `manual-chunks.jsonl` byte-identical → determinism re-proven.

## Diagram summaries + determinism fix (2026-09-26, prompt v3)
- Flow figures were classified `kind=text` (0 diagram summaries). Fix (generic, no
  per-figure rules): tightened kind rules in `VISION_PROMPT` (boxes/arrows showing
  sequence/decision/connectivity ⇒ diagram, `VISION_PROMPT_VERSION=3`), summary-based
  routing fallback, fence sanitiser. Result (`out/manual-ingest-canon.log`): 21 fresh
  vision calls, `diagrams_summarised` 0 → **2** (p39 decision ladder, p40 retrieval
  pipeline), 0 fenced transcriptions; p37/p38 stay text (empty summaries) — partial
  flip stated in `cli/PDF_INGESTION_ANALYSIS.md` F5, not overclaimed.
- Same round exposed rerun churn: 3/206 chunks re-upserted from newline placement alone
  (4 vision-cache misses → fresh nondeterministic calls). Fix: whitespace-canonicalised
  `embed_text` for hash+embed (stored text untouched). Proof: migration run upserted 206
  once; `out/manual-ingest-canon-run2.log` → `upserted=0 skipped=206` with 1 flaky call
  absorbed. Retrieval re-verified, ranks identical (Δ≤0.013); diagram query now returns
  p39/p40 (`out/search-diagram.log` recaptured, superseded v3 logs pruned).
- `cli/PDF_INGESTION_ANALYSIS.md` rewritten manual-grounded: tool comparison with sample
  outputs (§1) + 6 failure cases with unhandled output and applied workarounds (F1–F4
  fixed, F5–F6 accepted residuals).
