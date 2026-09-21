"""
S2.6 — Retrieval Benchmark & Evaluation Runner.

Queries each case with the INCIDENT text (short_description + description) —
the same input the agent receives at runtime — never the stored KB chunk text.
Supports stub embeddings (default, offline) and real Gemini embeddings via
`--real` (GEMINI_API_KEY, or the LiteLLM proxy when only LITELLM_* is set). Results record the embedding model,
dimension, and collection so numbers can't be mistaken for another setup.

Benchmark-only: uses the existing retrieval + embedding functions in
src/barq_ai_support without modifying agent or ingestion code.
"""

import argparse
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

# Ensure src/ is on sys.path (repo root / benchmark layout).
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

from barq_ai_support.config import settings
from barq_ai_support.retrieval.retriever import (
    default_embedding_fn,
    gemini_embedding_fn,
    retrieve,
)

STUB_MODEL_NAME = "sha256-stub-384"
STUB_DIM = 384
REAL_MODEL_NAME = "gemini-embedding-001"
REAL_DIM = 768
PROXY_MODEL_NAME = "gemini-embedding-001 (litellm-proxy)"
PROXY_GEMINI_MODEL = "gemini/gemini-embedding-001"


def _litellm_config() -> tuple[str, str]:
    """Return (base_url, api_key) for the LiteLLM proxy, or ('', '')."""
    base = os.environ.get("LITELLM_BASE_URL", "")
    key = os.environ.get("LITELLM_API_KEY", "")
    if not (base and key):
        try:
            from dotenv import dotenv_values

            vals = dotenv_values(BASE_DIR / ".env")
            base = base or vals.get("LITELLM_BASE_URL", "") or ""
            key = key or vals.get("LITELLM_API_KEY", "") or ""
        except ImportError:
            pass
    return base, key


def litellm_proxy_embedding_fn(text: str) -> list[float]:
    """Real Gemini embeddings via the team's LiteLLM proxy (768-dim).

    Used when GEMINI_API_KEY is absent but LITELLM_BASE_URL/LITELLM_API_KEY
    are configured. Stdlib-only (urllib) so the harness gains no new deps.
    """
    import json as _json
    import urllib.request as _urlreq

    base, key = _litellm_config()
    if not (base and key):
        raise RuntimeError("LiteLLM proxy is not configured (LITELLM_BASE_URL/LITELLM_API_KEY).")
    body = _json.dumps(
        {"model": PROXY_GEMINI_MODEL, "input": text, "dimensions": REAL_DIM}
    ).encode("utf-8")
    req = _urlreq.Request(
        base.rstrip("/") + "/embeddings",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with _urlreq.urlopen(req, timeout=120) as resp:
        data = _json.loads(resp.read().decode("utf-8"))
    return data["data"][0]["embedding"]


def get_embedding_fn(use_real: bool = False) -> Callable[[str], list[float]]:
    """Return the embedding function for this benchmark run.

    Stub (default) is offline/deterministic but MUST match the collection's
    vector dimension or Qdrant rejects the query. Real mode prefers
    GEMINI_API_KEY (google-genai, same as ingestion) and falls back to the
    LiteLLM proxy embedding endpoint when only LITELLM_* is configured.
    """
    if not use_real:
        return default_embedding_fn
    if os.environ.get("GEMINI_API_KEY"):
        return gemini_embedding_fn
    try:
        from dotenv import dotenv_values

        if dotenv_values(BASE_DIR / ".env").get("GEMINI_API_KEY"):
            return gemini_embedding_fn
    except ImportError:
        pass
    base, key = _litellm_config()
    if base and key:
        return litellm_proxy_embedding_fn
    raise RuntimeError(
        "No real embedding backend configured: set GEMINI_API_KEY or "
        "LITELLM_BASE_URL/LITELLM_API_KEY (see .env.example)."
    )


def embedding_model_name(embedding_fn: Callable[..., Any]) -> str:
    if embedding_fn is gemini_embedding_fn:
        return REAL_MODEL_NAME
    if embedding_fn is litellm_proxy_embedding_fn:
        return PROXY_MODEL_NAME
    return STUB_MODEL_NAME


def embedding_dim(embedding_fn: Callable[..., Any]) -> int:
    """Vector dimension without calling a paid API.

    The stub is probed directly (cheap, offline). The Gemini dims are the
    fixed output_dimensionality used at ingestion time (768), so they are
    reported as constants.
    """
    if embedding_fn is gemini_embedding_fn:
        return REAL_DIM
    if embedding_fn is litellm_proxy_embedding_fn:
        return REAL_DIM
    try:
        sig = inspect.signature(embedding_fn)
        if "dim" in sig.parameters:
            return len(embedding_fn("dim-probe"))
        return len(embedding_fn("dim-probe"))
    except Exception:
        return STUB_DIM


def load_dataset(dataset_path: Path) -> list[dict[str, Any]]:
    with open(dataset_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Benchmark dataset must be a JSON list: {dataset_path}")
    return data


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
    query = " ".join(p for p in (s.strip() for s in parts if isinstance(s, str)) if p)
    if query:
        return query
    return item.get("query", "")


def is_incident_query(item: dict[str, Any]) -> bool:
    incident = item.get("incident", {}) or {}
    return bool(
        (incident.get("short_description") or "").strip()
        or (incident.get("description") or "").strip()
    )


def evaluate_retrieval_benchmark(
    dataset_path: Path,
    top_k: int = 5,
    threshold: float | None = None,
    use_real_embeddings: bool = False,
) -> dict[str, Any]:
    """Execute the benchmark evaluation against the active Qdrant collection."""
    if threshold is None:
        threshold = settings.retrieval_score_threshold

    embedding_fn = get_embedding_fn(use_real=use_real_embeddings)
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
        item_id = item.get("id", f"CASE-{idx:02d}")
        item_type = item.get("type", "answerable")
        incident = item.get("incident", {}) or {}
        incident_num = incident.get("number", "?")
        short_desc = incident.get("short_description", "")
        query = build_benchmark_query(item)
        query_fallback = not is_incident_query(item)
        expected_arts = item.get("expected_article_numbers", []) or []

        # Retrieve WITHOUT gating (threshold=0.0) so ranking quality is
        # recorded even when the score falls below the evaluation threshold.
        # The gate is applied below in pure Python — identical semantics.
        res = retrieve(
            query=query,
            top_k=top_k,
            score_threshold=0.0,
            category=item.get("category"),
            embedding_fn=embedding_fn,
        )

        retrieved_arts = [c.article_number for c in res.chunks]
        best_score = res.best_score if res.best_score is not None else 0.0
        # Empty result set = filter matched nothing (e.g. unknown category).
        filter_empty = len(res.chunks) == 0
        # Gate applied here (equivalent to retrieve(threshold=threshold)).
        passed_gate = res.best_score is not None and res.best_score >= threshold

        if item_type == "answerable":
            positive_scores.append(best_score)
            ranked_hit = any(exp in retrieved_arts for exp in expected_arts)
            # A hit requires the expected article to be retrieved AND gated.
            is_hit = passed_gate and ranked_hit and not filter_empty
            status = "PASS" if is_hit else "FAIL"

            answerable_results.append(
                {
                    "id": item_id,
                    "incident_number": incident_num,
                    "short_description": short_desc,
                    "status": status,
                    "best_score": best_score,
                    "retrieved_count": len(res.chunks),
                    "expected": expected_arts,
                    "retrieved": retrieved_arts,
                    "res_ok": passed_gate,
                    "ranked_hit": ranked_hit,
                    "filter_empty": filter_empty,
                    "query_text": query,
                    "query_source": "legacy_query_field" if query_fallback else "incident_text",
                }
            )

            print(f"[{idx:02d}] {item_id} ({incident_num}) - {status}")
            print(f"     Description: {short_desc}")
            print(f"     Expected:    {expected_arts}")
            print(f"     Retrieved:   {retrieved_arts} (Best Score: {best_score:.4f})")
            if not is_hit:
                if filter_empty:
                    print("     Reason:      Category filter matched nothing — check 'category' label.")
                elif not passed_gate:
                    print(f"     Reason:      Threshold not met ({best_score:.4f} < {threshold:.2f}).")
                else:
                    print("     Reason:      Wrong article ranked above expected.")
            print()

        else:
            negative_scores.append(best_score)
            # Correct refusal occurs when the gate refuses (score < threshold).
            # A filter matching nothing also refuses — flagged, not celebrated.
            is_correct_refusal = not passed_gate
            status = "PASS" if is_correct_refusal else "FAIL"

            negative_results.append(
                {
                    "id": item_id,
                    "incident_number": incident_num,
                    "short_description": short_desc,
                    "status": status,
                    "best_score": best_score,
                    "retrieved_count": len(res.chunks),
                    "expected": "CLEAN REFUSAL",
                    "retrieved": retrieved_arts if passed_gate else "REFUSED",
                    "res_ok": passed_gate,
                    "filter_empty": filter_empty,
                    "query_text": query,
                    "query_source": "legacy_query_field" if query_fallback else "incident_text",
                }
            )

            print(f"[{idx:02d}] {item_id} ({incident_num}) [NEGATIVE CONTROL] - {status}")
            print(f"     Description: {short_desc}")
            print(f"     Expected:    CLEAN REFUSAL (Score < {threshold:.2f})")
            print(f"     Retrieved:   {'REFUSED' if not passed_gate else retrieved_arts} (Best Score: {best_score:.4f})")
            if filter_empty:
                print("     Note:        Filter matched nothing (unknown category) — vacuous refusal.")
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
        print(
            f"  - Answerable Scores: min={min(positive_scores):.4f}, "
            f"max={max(positive_scores):.4f}, avg={sum(positive_scores)/len(positive_scores):.4f}"
        )
        print(
            f"  - Negative Scores:   min={min(negative_scores):.4f}, "
            f"max={max(negative_scores):.4f}, avg={sum(negative_scores)/len(negative_scores):.4f}"
        )
        print(f"  - Margin Separation: {min(positive_scores) - max(negative_scores):.4f} (Positive Min - Negative Max)")
    else:
        print("  SCORE DISTRIBUTION ANALYSIS: insufficient data (one side empty).")
    if embedding_model == STUB_MODEL_NAME:
        print("  NOTE: stub embeddings carry no semantic meaning — scores measure")
        print("        exact-text overlap only. Re-run with --real for a threshold")
        print("        that generalizes to real incident phrasing.")

    # Threshold sweep on the SAME retrieval pass: recompute hit/refusal at
    # each candidate threshold without re-querying.
    answerable_cases = [c for c in cases if c.get("type") == "answerable"]
    sweep_thresholds = [round(0.50 + 0.05 * i, 2) for i in range(9)]  # 0.50..0.90
    sweep = []
    for t in sweep_thresholds:
        a_pass = sum(
            1
            for r, c in zip(answerable_results, answerable_cases)
            if r["best_score"] is not None
            and r["best_score"] >= t
            and any(exp in r["retrieved"] for exp in (c.get("expected_article_numbers", []) or []))
            and not r["filter_empty"]
        )
        n_pass = sum(1 for r in negative_results if r["best_score"] < t)
        sweep.append(
            {
                "threshold": t,
                "hit_rate": (a_pass / num_answerable) if num_answerable else 0.0,
                "hit_count": f"{a_pass}/{num_answerable}",
                "refusal_correctness": (n_pass / num_negative) if num_negative else 0.0,
                "refusal_count": f"{n_pass}/{num_negative}",
            }
        )

    print("  THRESHOLD SWEEP (same retrieval pass):")
    print("  thresh | hit rate  | refusal")
    for row in sweep:
        mark = " <-- evaluated" if abs(row["threshold"] - threshold) < 1e-9 else ""
        print(
            f"  {row['threshold']:.2f}   | {row['hit_rate']*100:5.1f}% ({row['hit_count']:>5}) "
            f"| {row['refusal_correctness']*100:5.1f}% ({row['refusal_count']:>5}){mark}"
        )

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
        "threshold_sweep": sweep,
        "answerable_details": answerable_results,
        "negative_details": negative_results,
    }

    return summary


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="S2.6 retrieval benchmark harness")
    parser.add_argument(
        "dataset",
        nargs="?",
        default="benchmark_dataset.json",
        help="Benchmark dataset JSON file (default: benchmark_dataset.json in the benchmark/ dir)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Score threshold (default: settings.retrieval_score_threshold)",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="Use real Gemini embeddings (GEMINI_API_KEY, or LITELLM_* proxy fallback).",
    )
    parser.add_argument(
        "--top-k", type=int, default=5, help="Top-K retrieved chunks per incident."
    )
    args = parser.parse_args(argv)

    raw_dataset = Path(args.dataset)
    dataset_path = raw_dataset if raw_dataset.is_absolute() or raw_dataset.exists() else (
        Path(__file__).resolve().parent / args.dataset
    )
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
