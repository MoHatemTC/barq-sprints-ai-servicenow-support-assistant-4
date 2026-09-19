"""
S2.6 — Retrieval Benchmark & Evaluation Runner.

Loads benchmark_dataset.json, executes semantic retrieval for each incident,
evaluates granular per-incident performance (Hit Rate and Refusal Correctness),
computes score distribution statistics across positive and negative controls,
and analyzes optimal score thresholds.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

# Ensure src/ is on sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

from barq_ai_support.config import settings
from barq_ai_support.retrieval.retriever import retrieve


def load_dataset(dataset_path: Path) -> list[dict[str, Any]]:
    with open(dataset_path, "r", encoding="utf-8") as f:
        return json.load(f)


def evaluate_retrieval_benchmark(
    dataset_path: Path,
    top_k: int = 5,
    threshold: float | None = None,
) -> dict[str, Any]:
    """
    Executes the benchmark evaluation against the active Qdrant collection.
    """
    if threshold is None:
        threshold = settings.retrieval_score_threshold

    cases = load_dataset(dataset_path)

    answerable_results = []
    negative_results = []

    positive_scores = []
    negative_scores = []

    print("=" * 80)
    print("  BARQ AI SUPPORT ASSISTANT - RETRIEVAL BENCHMARK EVALUATION")
    print(f"  Qdrant Collection: {settings.qdrant_collection_name}")
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
        query = item["query"]
        expected_arts = item.get("expected_article_numbers", [])

        # Execute retrieval with the configured threshold
        res = retrieve(
            query=query,
            top_k=top_k,
            score_threshold=threshold,
            category=item.get("category"),
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
    print("  SCORE DISTRIBUTION ANALYSIS:")
    print(f"  - Answerable Scores: min={min(positive_scores):.4f}, max={max(positive_scores):.4f}, avg={sum(positive_scores)/len(positive_scores):.4f}")
    print(f"  - Negative Scores:   min={min(negative_scores):.4f}, max={max(negative_scores):.4f}, avg={sum(negative_scores)/len(negative_scores):.4f}")
    print(f"  - Margin Separation: {min(positive_scores) - max(negative_scores):.4f} (Positive Min - Negative Max)")
    print("=" * 80)

    summary = {
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
            "answerable": {
                "min": min(positive_scores),
                "max": max(positive_scores),
                "avg": sum(positive_scores) / len(positive_scores),
            },
            "negative": {
                "min": min(negative_scores),
                "max": max(negative_scores),
                "avg": sum(negative_scores) / len(negative_scores),
            },
            "separation_margin": min(positive_scores) - max(negative_scores),
        },
        "answerable_details": answerable_results,
        "negative_details": negative_results,
    }

    return summary


def main():
    dataset_path = Path(__file__).resolve().parent / "benchmark_dataset.json"
    threshold = float(sys.argv[1]) if len(sys.argv) > 1 else settings.retrieval_score_threshold

    results = evaluate_retrieval_benchmark(dataset_path=dataset_path, threshold=threshold)

    output_path = Path(__file__).resolve().parent / "benchmark_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\n[Saved full granular evaluation JSON to: {output_path}]")


if __name__ == "__main__":
    main()
