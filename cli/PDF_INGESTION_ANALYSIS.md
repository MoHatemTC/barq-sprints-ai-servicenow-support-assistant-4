# PDF Ingestion — Evaluation: tool comparison, samples, failure cases

Scope: manual-only (`challenge/BARQ_IT_Service_Desk_Manual_Ed5.1.pdf`, 52 pp → 206 chunks:
85 text / 100 table / 19 image_ocr / 2 diagram). Every sample below is quoted from files
present in this repo (`out/manual-chunks.jsonl`, `out/*.log`) — rerun the commands in
`script_run_cli.md` to reproduce. Collection `manual_test` holds exactly these 206 points
(`doc_id=pdf-barq-it-service-desk-manual-ed5-1`); `kb_chunks` untouched at 211.

## 1. Tool comparison with sample output

### 1.1 Tables: PyMuPDF grids vs Camelot-lattice challenger — KEEP BOTH, contend
Each ruled grid is contested: both candidates go through the same `table_to_structured`
+ `_grid_score` (`empty_ratio + 0.5 × placeholder_header_ratio`, lower wins, ties → PyMuPDF).
Live proof this matters on the manual (from `out/manual-ingest-canon.log`):

```
INFO p9 lattice grid kept (score 0.00)
INFO p17 lattice grid kept (score 0.00)
INFO p30 lattice grid kept (score 0.00)
INFO p32 lattice grid kept (score 0.00)
INFO p33 lattice grid kept (score 0.00)
INFO p39 lattice grid kept (score 0.00)
INFO p44 lattice grid kept (score 0.00)
INFO p52 lattice grid kept (score 0.00)
```

`score 0.00` = zero empty cells, zero placeholder headers. Result: 72 structured tables,
3 via vision, **0 degraded** across 52 pages. Sample output (p17 article index,
`out/manual-chunks.jsonl`):

```
| ARTICLE | REPORTED AS | SERVICE | CATEGORY | OWNER |
|---|---|---|---|---|
| KB0009 | "Wi-Fi keeps dropping on the 5 GHz network" | corporate-wifi | network | Network Ops |
| KB0010 v2 | "Order service is returning 500s under load" | order- processing | software | Platform Eng |

Rows as records:
ARTICLE: KB0009; REPORTED AS: "Wi-Fi keeps dropping on the 5 GHz network"; SERVICE: corporate-wifi; ...
```

Header paths (`Group > Sub`), rowspan forward-fill, and phantom-column merges are preserved
in both the Markdown (embedded) and the `table_json` payload (kept for audit).

### 1.2 OCR: Tesseract ara+eng vs vision LLM — SELECT PER REGION, don't pick one
Rule (`select_transcription`): prefer vision only on material disagreement
(token-disagree > 0.10 or line-coverage < 0.80); else keep deterministic Tesseract.
Both scripts survive end to end: p44 Arabic run report
(`تقرير تشغيل النظام … القرار النهائي لموظف الدعم دائمًا`, via=vision) and the
KB0005 archived scan (p18, via=tesseract/OCR path). Per-region `via` + disagreement
score stored in every chunk meta (`out/manual-kb-index.md` column).

### 1.3 Diagrams: raw transcription (before) vs structured summary (after)
Before (prompt v2): flow figures classified `kind=text`; one figure's whole JSON reply was
stored verbatim as chunk text (see F2). After (prompt v3 + summary-fallback routing):
`diagrams_summarised` 0 → **2** (p39 decision ladder, p40 retrieval pipeline).
Sample (p39, `out/manual-chunks.jsonl`), exactly the required shape —
steps, `If→else` decisions, `A→B` connectivity, purpose:

```
Diagram summary:
(1) 1. Gate 1: Evidence, 2. Gate 2: Risk, 3. Gate 3: Confidence.
(2) If no chunk clears the score threshold -> Refuse and hand off, else -> Gate 2.
If risk is high or confidence sits below the floor -> Escalate to a human, else -> Gate 3 / Suggest.
If all three gates pass -> Suggest.
(3) Gate 1 (Evidence) -> Gate 2 (Risk), Gate 2 (Risk) -> Gate 3 (Confidence).
(4) The diagram shows the three-gate evaluation process determining whether to generate
a suggested response, escalate to a human, or refuse and hand off.

Visible labels:
The decision ladder
Three gates stand between a retrieved chunk and a written suggestion. ...
```

Retrieval effect (`out/search-diagram.log`): `--type diagram` flow query → p39 #1 (0.628),
p40 #2 (0.558). Previously zero hits.

### 1.4 Embeddings: raw vs whitespace-canonicalised text
`embed_text` canonicalises whitespace (`\s+` → single space) for hash+embed; stored text
keeps newlines. Rank impact measured (before → after): EN 0.736→0.738, KB0001
0.697→0.684, KB0010 0.637→0.626, AR 0.652→0.651 — ranks identical everywhere, Δ≤0.013.

## 2. Failure cases: unhandled output → workaround applied

**F1. Vision wraps JSON in code fences; wrappers stored in Qdrant.**
Unhandled output (stored payload, p3 pre-fix): transcription beginning
```` ```json\n{\n "kind": "text",\n "transcription": "BARQ Systems · IT Service Operations Manual … ````
Workaround applied: `_strip_code_fences` at parse time + value sanitiser
(`cli/ingest_pdf.py`, `Models.vision`). Verified: 0 fenced transcriptions across all
21 fresh v3 analyses.

**F2. Vision returns non-JSON at all (`JSONDecodeError`, in `out/manual-ingest-v3.log`).**
Unhandled output: no parseable object. Workaround applied (pre-existing, kept): fall back
to `transcription=raw`, kind=text — content preserved as searchable text, never dropped;
a `WARNING vision non-JSON reply` line marks it. Pre-fix instance: the p38 event-exchange
figure's raw JSON stored as chunk text; post-fix the same region parses to a clean
transcription.

**F3. Rerun churn: 3/206 chunks re-upserted from newline placement alone.**
Unhandled output (disk dump vs live Qdrant, identical inputs):
```
-  ...agreed with a
-  7 of 52 ·
+  ...agreed with a ... 7 of 52 ·          (p7 idx1, 264 chars both sides)
-  ServiceNow Save Resolve Delete Number: INC0010023
+  ServiceNow Save / Resolve / Delete Number: INC0010023   (p25 idx3)
```
Root cause: 4 vision-cache misses → fresh (nondeterministic) model calls → same words,
different line breaks → different content hash. Workaround applied: canonicalised
`embed_text` (§1.4). Proof (`out/manual-ingest-canon*.log`): migration run upserted 206
once; next run `upserted=0 skipped=206` with 1 flaky fresh call absorbed (same hash →
skipped). No duplication possible: deterministic `uuid5` IDs + hash compare + stale prune.

**F4. Qdrant 400 filtering on unindexed bool key `retired`.**
Unhandled output: fresh collections carry only the CLI's keyword indexes, so a
`retired`-filtered query is rejected. Workaround applied: filter client-side
(reported in `out/RUNLOG.md`); no code/data change needed. Retired correctness held
throughout: exactly 1 `retired=true` point (p23 idx1, `superseded_by="KB0010 v2"`,
`out/retired-check.log`), restart queries surface v2.

**F5 (residual, accepted). Partial diagram flip: 2 of 4 flow figures.**
p39/p40 route to diagram; p37 (§11.2 end-to-end) and p38 (event exchange) stay
`kind=text` with empty summaries — the label-noise fallback only fires on non-empty
summaries, so these correctly stay text rather than being forced. Stated, not hidden;
generic prompt wording, no per-figure rules.

**F6 (residual, accepted). ~4 vision-cache misses per rerun, trigger unproven.**
Bytes differ somewhere upstream on a few regions each run (candidates: render-level
variance, OSD/deskew threshold flip — the mechanism documented in code at
`image_chunks`). Harmless post-F3 (whitespace variance absorbed; word-level model
variance would still, correctly, trigger re-upsert). Runs converge: 0 upserts.
