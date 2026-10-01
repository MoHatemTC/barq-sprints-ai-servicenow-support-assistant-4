# S3.6 — Operational Confidence Threshold Recommendation

## Results this run

- **Top-3 Hit Rate (answerable cases):** 100.0% (20/20) — target was 8/10
- **Refusal Correctness (negative controls):** 100.0% (10/10) — target was 3/3
- Threshold used: **0.75**
- Score distribution:
  - Lowest confidence among answerable cases: **0.7573** (INC-BENCH-008)
  - Highest confidence among negative controls: **0.6720** (INC-BENCH-030)
  - No overlap between the two classes in this run — a clean gap of ~0.085

## IMPORTANT — scope of this measurement

These numbers come from a **synthetic fixture dataset**, not the team's real ServiceNow
knowledge base. Real KB content wasn't available in this environment (no ServiceNow
credentials), so 24 fixture articles were written to match the exact titles the existing
`benchmark_dataset.json` already expected (e.g. "No internet connection" → `KB0010010`).
That wording alignment makes retrieval meaningfully *easier* than it will be against real,
messier production KB articles and real incident phrasing (the dataset's own "paraphrase
with typos" cases hint at the kind of noise real input carries). A 100%/100% result here
should be read as **"the pipeline mechanics work correctly end-to-end,"** not as proof the
threshold will hold at this level against real data.

**Recommendation:** re-run this exact harness (`pipeline_benchmark.py --real`) against the
real Qdrant collection from S2.2 the moment it's accessible, and treat that run's numbers —
not this one's — as the basis for any production threshold decision.

## Recommended threshold: keep 0.75

Given the clean separation observed (0.6720 to 0.7573), 0.75 is a reasonable, slightly
conservative choice — it sits close to the answerable-side edge of the gap rather than the
midpoint, which trades a small amount of hit-rate margin for a larger safety margin against
false positives.

### Trade-offs of shifting the threshold

**Raising the threshold (e.g., toward 0.80+):**
- Pro: further reduces risk of a wrong answer being surfaced to a human reviewer with false
  confidence (fewer false "suggested" outcomes).
- Con: starts pushing genuinely answerable cases into unnecessary escalation — in this run,
  raising above 0.7573 would immediately flip INC-BENCH-008 from a correct suggestion to a
  false escalation, directly costing Top-3 Hit Rate. Every escalation is extra human-review
  workload even when the system actually knew the answer.

**Lowering the threshold (e.g., toward 0.65 or below):**
- Pro: fewer unnecessary escalations, less human-review load.
- Con: this is the more dangerous direction. Lowering below 0.6720 would pull
  INC-BENCH-030 (a genuine negative control — no matching article exists) above threshold,
  causing the system to confidently suggest an answer where none is correct. A false
  "suggested" outcome is worse than a false escalation: it reaches a human reviewer framed
  as a high-confidence answer rather than a flagged unknown, which is exactly the failure
  mode the human-review gate exists to prevent.

**Why 0.75 rather than the exact midpoint (~0.71):** biasing slightly toward the
answerable-side edge of the gap prioritizes refusal safety over hit-rate maximization,
consistent with the system's own design principle that a human reviews every suggestion
and "the AI suggests, a human decides" — an unnecessary escalation costs review time, but a
false suggestion costs trust in the AI's confidence signal.

## Known limitation carried into this recommendation

This threshold has not been validated against:
- Real ServiceNow KB article content
- Real (non-templated) incident phrasing
- The real agent's decision layer (this run uses a threshold-gate stub standing in for the
  ReAct agent — see `pipeline_benchmark.py`'s file header)

Both should be re-validated once the relevant teammate branches (Celery worker + agent;
real KB access) land.
