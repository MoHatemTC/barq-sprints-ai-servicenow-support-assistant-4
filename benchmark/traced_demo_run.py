"""
S3.6 — Tracing demo.

Standalone from the benchmark harness on purpose: this exists to produce
clean, easy-to-find demo evidence (a trace view covering fetch, retrieval,
model, and writeback spans under a single trace), not to change how the
harness runs.

Runs a handful of sample incidents (default: one answerable, one negative
control, pulled straight from benchmark_dataset.json) through
tracing.traced_incident_run(), each producing one Langfuse trace with four
spans: incident-fetch, kb-retrieval, agent-decision, servicenow-writeback.

Requires:
  - LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST in .env
  - A populated Qdrant collection (see seed_fixtures.py) and a real
    embedding backend (GEMINI_API_KEY or LITELLM_BASE_URL/LITELLM_API_KEY)

Usage:
    uv run python benchmark/traced_demo_run.py
    uv run python benchmark/traced_demo_run.py --count 5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from barq_ai_support.config import settings  # noqa: E402
from barq_ai_support.retrieval.retriever import retrieve  # noqa: E402
from barq_ai_support.tracing import traced_incident_run  # noqa: E402

from run_benchmark import get_embedding_fn, embedding_model_name, load_dataset, build_benchmark_query  # noqa: E402


def run_one(item: dict, top_k: int, threshold: float, embedding_fn) -> dict:
    incident = item.get("incident", {}) or {}
    query = build_benchmark_query(item)
    category = item.get("category")

    def _retrieve(query: str, category: str | None):
        return retrieve(
            query=query,
            top_k=top_k,
            score_threshold=0.0,
            category=category,
            embedding_fn=embedding_fn,
        )

    def _decide(res) -> dict:
        best_score = res.best_score if res.best_score is not None else 0.0
        filter_empty = len(res.chunks) == 0
        outcome = "suggested" if (not filter_empty and best_score >= threshold) else "escalated"
        return {
            "outcome": outcome,
            "confidence": best_score,
            "matched_article_numbers": [c.article_number for c in res.chunks],
        }

    return traced_incident_run(
        incident=incident,
        query=query,
        category=category,
        retrieve_fn=_retrieve,
        decide_fn=_decide,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="S3.6 tracing demo — a few traced incident runs")
    parser.add_argument(
        "--dataset", default=str(Path(__file__).resolve().parent / "benchmark_dataset.json")
    )
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument(
        "--count",
        type=int,
        default=2,
        help="How many sample incidents to trace (split evenly between answerable/negative). Default 2.",
    )
    args = parser.parse_args()

    threshold = args.threshold if args.threshold is not None else settings.retrieval_score_threshold
    embedding_fn = get_embedding_fn(use_real=True)
    cases = load_dataset(Path(args.dataset))

    answerable = [c for c in cases if c.get("type") == "answerable"]
    negative = [c for c in cases if c.get("type") != "answerable"]
    n_each = max(1, args.count // 2)
    sample = answerable[:n_each] + negative[: args.count - n_each]

    print("=" * 80)
    print("  S3.6 TRACING DEMO")
    print(f"  Embedding backend: {embedding_model_name(embedding_fn)}   Threshold: {threshold:.2f}")
    print(f"  Sending {len(sample)} traced incident run(s) to: {settings.langfuse_host}")
    print("=" * 80)
    print()

    for item in sample:
        incident = item.get("incident", {}) or {}
        number = incident.get("number", item.get("id", "?"))
        decision = run_one(item, top_k=args.top_k, threshold=threshold, embedding_fn=embedding_fn)
        print(f"[{item.get('type')}] {number}: {json.dumps(decision)}")

    print()
    print(f"Done. Open {settings.langfuse_host} -> your project -> Traces to view them.")
    print("Each trace should show 4 nested spans: incident-fetch, kb-retrieval, agent-decision, servicenow-writeback.")


if __name__ == "__main__":
    main()
