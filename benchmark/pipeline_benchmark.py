"""
S3.6 — Pipeline Benchmark & Evaluation Harness.

Measures the FULL incident-handling pipeline outcome (suggested vs.
escalated), not just retrieval score, against a versioned dataset of
answerable incidents and negative controls.

-----------------------------------------------------------------------------
STUB NOTICE (read this before trusting the numbers)
-----------------------------------------------------------------------------
The real reasoning core (S3 task: Celery worker + ReAct agent over
searchKB / addWorkNote / suggestAnswer / requestHR) is a teammate's branch
that isn't merged yet. `stub_pipeline_agent()` below stands in for it:
it calls the REAL retrieval code (retriever.retrieve), then applies a
deterministic confidence-threshold gate to decide the terminal outcome,
mirroring the real agent's contract:

    best retrieval score >= threshold  ->  "suggested"  (== suggestAnswer)
    best retrieval score <  threshold  ->  "escalated"   (== requestHR)

This means retrieval quality is measured honestly (it's real), but the
*decision* layer is a simplified stand-in for an LLM ReAct loop — it has
no re-querying, no multi-turn reasoning, no confidence self-assessment.
Swap the body of `stub_pipeline_agent()` for a call into the real
Celery task / agent entry point once that branch lands. The function
signature (incident in, PipelineResult out) is the seam — nothing else
in this file needs to change.
-----------------------------------------------------------------------------

Usage:
    uv run python benchmark/pipeline_benchmark.py                  # stub embeddings
    uv run python benchmark/pipeline_benchmark.py --real            # real Gemini embeddings
    uv run python benchmark/pipeline_benchmark.py --threshold 0.70 --real
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from barq_ai_support.config import settings  # noqa: E402
from barq_ai_support.retrieval.retriever import retrieve  # noqa: E402

# Reuse the embedding-backend selection already built for the S2.6 harness
# instead of duplicating it.
from run_benchmark import (  # noqa: E402
    get_embedding_fn,
    embedding_model_name,
    embedding_dim,
    load_dataset,
    build_benchmark_query,
)


# --------------------------------------------------------------------------
# Stubbed pipeline decision layer (see module docstring)
# --------------------------------------------------------------------------

@dataclass
class PipelineResult:
    outcome: str  # "suggested" | "escalated"
    confidence: float
    matched_article_numbers: list[str] = field(default_factory=list)
    retrieved_count: int = 0
    filter_empty: bool = False


def stub_pipeline_agent(
    query: str,
    category: str | None,
    top_k: int,
    threshold: float,
    embedding_fn,
) -> PipelineResult:
    """Stand-in for the real agent's terminal decision. See module docstring."""
    res = retrieve(
        query=query,
        top_k=top_k,
        score_threshold=0.0,  # ungated retrieval; we gate ourselves below
        category=category,
        embedding_fn=embedding_fn,
    )
    best_score = res.best_score if res.best_score is not None else 0.0
    filter_empty = len(res.chunks) == 0
    outcome = "suggested" if (not filter_empty and best_score >= threshold) else "escalated"

    return PipelineResult(
        outcome=outcome,
        confidence=best_score,
        matched_article_numbers=[c.article_number for c in res.chunks],
        retrieved_count=len(res.chunks),
        filter_empty=filter_empty,
    )


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

def evaluate_pipeline_benchmark(
    dataset_path: Path,
    top_k: int = 3,
    threshold: float | None = None,
    use_real_embeddings: bool = False,
) -> dict[str, Any]:
    if threshold is None:
        threshold = settings.retrieval_score_threshold

    embedding_fn = get_embedding_fn(use_real=use_real_embeddings)
    embedding_model = embedding_model_name(embedding_fn)
    vector_dim = embedding_dim(embedding_fn)

    cases = load_dataset(dataset_path)

    rows: list[dict[str, Any]] = []
    positive_scores: list[float] = []
    negative_scores: list[float] = []

    print("=" * 80)
    print("  BARQ AI SUPPORT ASSISTANT - S3.6 PIPELINE BENCHMARK")
    print(f"  Embedding Model: {embedding_model} ({vector_dim}-dim)   Top-K: {top_k}   Threshold: {threshold:.2f}")
    print(f"  Total Incidents: {len(cases)}")
    print("  NOTE: agent decision layer is STUBBED (see file header) — real")
    print("        Celery/ReAct agent branch not yet merged.")
    print("=" * 80)
    print()

    for idx, item in enumerate(cases, 1):
        item_id = item.get("id", f"CASE-{idx:02d}")
        item_type = item.get("type", "answerable")
        incident = item.get("incident", {}) or {}
        query = build_benchmark_query(item)
        expected = item.get("expected_article_numbers", []) or []

        result = stub_pipeline_agent(
            query=query,
            category=item.get("category"),
            top_k=top_k,
            threshold=threshold,
            embedding_fn=embedding_fn,
        )

        if item_type == "answerable":
            positive_scores.append(result.confidence)
            hit = result.outcome == "suggested" and any(
                exp in result.matched_article_numbers for exp in expected
            )
            status = "PASS" if hit else "FAIL"
            expected_outcome = "suggested"
        else:
            negative_scores.append(result.confidence)
            hit = result.outcome == "escalated"
            status = "PASS" if hit else "FAIL"
            expected_outcome = "escalated"

        rows.append(
            {
                "id": item_id,
                "type": item_type,
                "incident_number": incident.get("number", "?"),
                "short_description": incident.get("short_description", ""),
                "expected_outcome": expected_outcome,
                "actual_outcome": result.outcome,
                "expected_articles": expected,
                "top_k_retrieved": result.matched_article_numbers,
                "confidence": round(result.confidence, 4),
                "status": status,
            }
        )

        print(f"[{idx:02d}] {item_id} ({item_type}) - {status}")
        print(f"     Expected outcome: {expected_outcome}   Actual: {result.outcome}   Confidence: {result.confidence:.4f}")
        print(f"     Expected articles: {expected}   Top-{top_k} retrieved: {result.matched_article_numbers}")
        print()

    answerable_rows = [r for r in rows if r["type"] == "answerable"]
    negative_rows = [r for r in rows if r["type"] != "answerable"]
    hit_rate = (
        sum(1 for r in answerable_rows if r["status"] == "PASS") / len(answerable_rows)
        if answerable_rows else 0.0
    )
    refusal_correctness = (
        sum(1 for r in negative_rows if r["status"] == "PASS") / len(negative_rows)
        if negative_rows else 0.0
    )

    print("=" * 80)
    print(f"  TOP-{top_k} HIT RATE:        {hit_rate * 100:.1f}%  "
          f"({sum(1 for r in answerable_rows if r['status'] == 'PASS')}/{len(answerable_rows)})")
    print(f"  REFUSAL CORRECTNESS:   {refusal_correctness * 100:.1f}%  "
          f"({sum(1 for r in negative_rows if r['status'] == 'PASS')}/{len(negative_rows)})")
    print("=" * 80)

    # Threshold sweep, recomputed on the SAME retrieved scores (no re-querying).
    sweep_thresholds = [round(0.50 + 0.05 * i, 2) for i in range(9)]
    sweep = []
    for t in sweep_thresholds:
        a_pass = sum(
            1 for r in answerable_rows
            if r["confidence"] >= t and any(e in r["top_k_retrieved"] for e in r["expected_articles"])
        )
        n_pass = sum(1 for r in negative_rows if r["confidence"] < t)
        sweep.append(
            {
                "threshold": t,
                "hit_rate": (a_pass / len(answerable_rows)) if answerable_rows else 0.0,
                "hit_count": f"{a_pass}/{len(answerable_rows)}",
                "refusal_correctness": (n_pass / len(negative_rows)) if negative_rows else 0.0,
                "refusal_count": f"{n_pass}/{len(negative_rows)}",
            }
        )

    def _dist(scores: list[float]) -> dict[str, float | None]:
        if not scores:
            return {"min": None, "max": None, "avg": None}
        return {"min": min(scores), "max": max(scores), "avg": sum(scores) / len(scores)}

    return {
        "provenance": {
            "embedding_model": embedding_model,
            "embedding_dim": vector_dim,
            "collection": settings.qdrant_collection_name,
            "top_k": top_k,
            "threshold_used": threshold,
            "agent_decision_layer": "STUB (threshold gate) — see file header",
        },
        "metrics": {
            "top_3_hit_rate": hit_rate,
            "refusal_correctness": refusal_correctness,
            "answerable_total": len(answerable_rows),
            "negative_total": len(negative_rows),
        },
        "score_distribution": {
            "answerable": _dist(positive_scores),
            "negative": _dist(negative_scores),
        },
        "threshold_sweep": sweep,
        "rows": rows,
    }


def write_markdown_table(results: dict[str, Any], out_path: Path) -> None:
    rows = results["rows"]
    lines = [
        "# S3.6 Pipeline Benchmark — Per-Incident Results",
        "",
        f"Top-3 Hit Rate: **{results['metrics']['top_3_hit_rate']*100:.1f}%** "
        f"({sum(1 for r in rows if r['type']=='answerable' and r['status']=='PASS')}/{results['metrics']['answerable_total']})  ",
        f"Refusal Correctness: **{results['metrics']['refusal_correctness']*100:.1f}%** "
        f"({sum(1 for r in rows if r['type']!='answerable' and r['status']=='PASS')}/{results['metrics']['negative_total']})  ",
        f"Agent decision layer: {results['provenance']['agent_decision_layer']}",
        "",
        "| ID | Type | Expected Outcome | Actual Outcome | Confidence | Expected Article(s) | Retrieved (Top-K) | Status |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['id']} | {r['type']} | {r['expected_outcome']} | {r['actual_outcome']} | "
            f"{r['confidence']:.4f} | {', '.join(r['expected_articles']) or '—'} | "
            f"{', '.join(r['top_k_retrieved']) or '—'} | {r['status']} |"
        )
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="S3.6 pipeline benchmark harness")
    parser.add_argument("dataset", nargs="?", default="benchmark_dataset.json")
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--top-k", type=int, default=3, help="Default 3 for Top-3 Hit Rate.")
    args = parser.parse_args(argv)

    raw_dataset = Path(args.dataset)
    dataset_path = raw_dataset if raw_dataset.is_absolute() or raw_dataset.exists() else (
        Path(__file__).resolve().parent / args.dataset
    )
    threshold = args.threshold if args.threshold is not None else settings.retrieval_score_threshold

    results = evaluate_pipeline_benchmark(
        dataset_path=dataset_path,
        top_k=args.top_k,
        threshold=threshold,
        use_real_embeddings=args.real,
    )

    out_dir = Path(__file__).resolve().parent
    json_path = out_dir / "pipeline_benchmark_results.json"
    md_path = out_dir / "PIPELINE_RESULTS.md"
    json_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    write_markdown_table(results, md_path)
    print(f"\n[Saved JSON results to: {json_path}]")
    print(f"[Saved per-incident table to: {md_path}]")


if __name__ == "__main__":
    main()
