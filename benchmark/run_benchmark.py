"""
S2.6 — Retrieval Benchmark & Evaluation Runner.

Queries each case with the INCIDENT text (short_description + description) —
the same input the agent receives at runtime — never the stored KB chunk text.
Supports stub embeddings (default, offline) and real Gemini embeddings via
`--real` (requires LITELLM_API_KEY). Results record the embedding model,
dimension, and collection so numbers can't be mistaken for another setup.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Ensure src/ is on sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

from barq_ai_support.config import settings
from barq_ai_support.embeddings import (
    embedding_dim,
    embedding_model_name,
    get_embedding_fn,
)
from barq_ai_support.retrieval.retriever import retrieve


def load_dataset(dataset_path: Path) -> list[dict[str, Any]]:
    with open(dataset_path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_benchmark_query(item: dict[str, Any]) -> str:
    """Build the retrieval query from the incident text (runtime input).

    Falls back to the legacy `query` field only when incident text is absent,
    and flags that fallback so stale cases can't silently pass as incident queries.
    """
    incident = item.get("incident", {}) or {}
    parts = [
        incident.get("short_description", ""),
        incident.get("description", ""),
    ]
    query = " ".join(p for p in (s.strip() for s in parts) if p)
    if query:
        return query
    return item.get("query", "")


def evaluate_retrieval_benchmark(
    dataset_path: Path,
    top_k: int = 5,
    threshold: float | None = None,
    use_real_embeddings: bool = False,
) -> dict[str, Any]:
    """
    Executes the benchmark evaluation against the active Qdrant collection.
    """
    if threshold is None:
        threshold = settings.retrieval_score_threshold

    embedding_fn = get_embedding_fn(use_real=True if use_real_embeddings else False)
    embedding_model = embedding_model_name(embedding_fn)
    vector_dim = embedding_dim(embedding_fn)

    cases = load_dataset(dataset_path)

    answerable_results = []
    negative_results = []

    positive_scores = []
    negative_scores = []

    print("=" * 80)
    print("  BARQ AI SUPPORT ASSISTANT - RETRIEVAL BENCHMARK EVALUATION")
    print(f"  Qdrant Collection: {settings.qdrant_collection_name}")
    print(f"  Embedding Model:   {embedding_model} ({vector_dim}-dim)")
    print(f"  Score Threshold:   {threshold:.2f}")
    print(f"  Top-K:             {top_k}")
    print(f"  Total Incidents:   {len(cases)}")
    print("=" * 80)
    print()

    for idx, item in enumerate(cases, 1):
        item_id = item["id"]
        item_type = item["type"]
        incident_num = item["incident"]["number"]
        short_desc = item["incident"]["short_description"]
        query = build_benchmark_query(item)
        query_fallback = not (item.get("incident", {}).get("short_description") or item.get("incident", {}).get("description"))
        expected_arts = item.get("expected_article_numbers", [])

        # Execute retrieval with the configured threshold
        res = retrieve(
            query=query,
            top_k=top_k,
            score_threshold=threshold,
            category=item.get("category"),
            embedding_fn=embedding_fn,
        )

        retrieved_arts = [c.article_number for c in res.chunks]
        best_score = res.best_score or 0.0

        if item_type == "answerable":
            positive_scores.append(best_score)
            # A hit occurs if the expected article is among the retrieved chunks that met the threshold
            is_hit = res.ok and any(exp in retrieved_arts for exp in expected_arts)
            status = "PASS" if is_hit else "FAIL"
            
            answerable_results.append({
                "id": item_id,
                "incident_number": incident_num,
                "short_description": short_desc,
                "status": status,
                "best_score": best_score,
                "retrieved_count": len(res.chunks),
                "expected": expected_arts,
                "retrieved": retrieved_arts,
                "res_ok": res.ok,
                "query_text": query,
                "query_source": "legacy_query_field" if query_fallback else "incident_text",
            })

            print(f"[{idx:02d}] {item_id} ({incident_num}) - {status}")
            print(f"     Description: {short_desc}")
            print(f"     Expected:    {expected_arts}")
            print(f"     Retrieved:   {retrieved_arts} (Best Score: {best_score:.4f})")
            if not is_hit:
                print(f"     Reason:      Threshold not met ({best_score:.4f} < {threshold:.2f}) or wrong article.")
            print()

        else:
            negative_scores.append(best_score)
            # Correct refusal occurs when the gate refuses (ok=False) because score < threshold
            is_correct_refusal = not res.ok
            status = "PASS" if is_correct_refusal else "FAIL"

            negative_results.append({
                "id": item_id,
                "incident_number": incident_num,
                "short_description": short_desc,
                "status": status,
                "best_score": best_score,
                "retrieved_count": len(res.chunks),
                "expected": "CLEAN REFUSAL",
                "retrieved": retrieved_arts if res.ok else "REFUSED",
                "res_ok": res.ok,
                "query_text": query,
                "query_source": "legacy_query_field" if query_fallback else "incident_text",
            })

            print(f"[{idx:02d}] {item_id} ({incident_num}) [NEGATIVE CONTROL] - {status}")
            print(f"     Description: {short_desc}")
            print(f"     Expected:    CLEAN REFUSAL (Score < {threshold:.2f})")
            print(f"     Retrieved:   {'REFUSED' if not res.ok else retrieved_arts} (Best Score: {best_score:.4f})")
            print()

    # Aggregate Metrics Calculation
    num_answerable = len(answerable_results)
    num_answerable_pass = sum(1 for r in answerable_results if r["status"] == "PASS")
    hit_rate = (num_answerable_pass / num_answerable) if num_answerable > 0 else 0.0

    num_negative = len(negative_results)
    num_negative_pass = sum(1 for r in negative_results if r["status"] == "PASS")
    refusal_correctness = (num_negative_pass / num_negative) if num_negative > 0 else 0.0

    print("=" * 80)
    print("  AGGREGATE BENCHMARK SUMMARY")
    print("=" * 80)
    print(f"  Total Incidents Evaluated:    {len(cases)}")
    print(f"  Answerable Incidents:         {num_answerable} (Passed: {num_answerable_pass})")
    print(f"  Negative Control Incidents:   {num_negative} (Passed: {num_negative_pass})")
    print("-" * 80)
    print(f"  >>> HIT RATE:                 {hit_rate * 100:.1f}% ({num_answerable_pass}/{num_answerable})")
    print(f"  >>> REFUSAL CORRECTNESS:      {refusal_correctness * 100:.1f}% ({num_negative_pass}/{num_negative})")
    print("-" * 80)
    if positive_scores and negative_scores:
        print("  SCORE DISTRIBUTION ANALYSIS:")
        print(f"  - Answerable Scores: min={min(positive_scores):.4f}, max={max(positive_scores):.4f}, avg={sum(positive_scores)/len(positive_scores):.4f}")
        print(f"  - Negative Scores:   min={min(negative_scores):.4f}, max={max(negative_scores):.4f}, avg={sum(negative_scores)/len(negative_scores):.4f}")
        print(f"  - Margin Separation: {min(positive_scores) - max(negative_scores):.4f} (Positive Min - Negative Max)")
    else:
        print("  SCORE DISTRIBUTION ANALYSIS: insufficient data (one side empty).")
    if embedding_model == "sha256-stub-384":
        print("  NOTE: stub embeddings carry no semantic meaning — scores measure")
        print("        exact-text overlap only. Re-run with --real for a threshold")
        print("        that generalizes to real incident phrasing.")
    print("=" * 80)

    def _dist(scores: list[float]) -> dict[str, float | None]:
        if not scores:
            return {"min": None, "max": None, "avg": None}
        return {"min": min(scores), "max": max(scores), "avg": sum(scores) / len(scores)}

    separation = None
    if positive_scores and negative_scores:
        separation = min(positive_scores) - max(negative_scores)

    summary = {
        "provenance": {
            "embedding_model": embedding_model,
            "embedding_dim": vector_dim,
            "collection": settings.qdrant_collection_name,
            "query_source": "incident_text",
            "top_k": top_k,
        },
        "metrics": {
            "total_incidents": len(cases),
            "hit_rate": hit_rate,
            "refusal_correctness": refusal_correctness,
            "answerable_passed": num_answerable_pass,
            "answerable_total": num_answerable,
            "negative_passed": num_negative_pass,
            "negative_total": num_negative,
            "score_threshold_used": threshold,
        },
        "score_distribution": {
            "answerable": _dist(positive_scores),
            "negative": _dist(negative_scores),
            "separation_margin": separation,
        },
        "answerable_details": answerable_results,
        "negative_details": negative_results,
    }

    return summary


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="S2.6 retrieval benchmark harness")
    parser.add_argument(
        "threshold",
        nargs="?",
        type=float,
        default=None,
        help="Score threshold (default: settings.retrieval_score_threshold)",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="Use real Gemini embeddings via LiteLLM (requires LITELLM_API_KEY).",
    )
    parser.add_argument(
        "--top-k", type=int, default=5, help="Top-K retrieved chunks per incident."
    )
    args = parser.parse_args(argv)

    dataset_path = Path(__file__).resolve().parent / "benchmark_dataset.json"
    threshold = args.threshold if args.threshold is not None else settings.retrieval_score_threshold

    results = evaluate_retrieval_benchmark(
        dataset_path=dataset_path,
        threshold=threshold,
        top_k=args.top_k,
        use_real_embeddings=args.real,
    )

    output_path = Path(__file__).resolve().parent / "benchmark_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\n[Saved full granular evaluation JSON to: {output_path}]")


if __name__ == "__main__":
    main()
