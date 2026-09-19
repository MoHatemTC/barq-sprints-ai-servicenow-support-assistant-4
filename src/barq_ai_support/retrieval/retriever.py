"""
S2.3 - Semantic Retrieval & Threshold Gate

Step 1: Qdrant client connection.
"""

from functools import lru_cache

from qdrant_client import QdrantClient

from qdrant_client import models as qmodels

from ..config import settings
from ..embeddings import default_embedding_fn, sync_embedding_fn

from dataclasses import dataclass, field

# Re-exported for backwards compatibility (tests import it from here).
__all__ = [
    "default_embedding_fn",
    "sync_embedding_fn",
    "get_qdrant_client",
    "retrieve",
    "RetrievedChunk",
    "RetrievalResult",
]


@lru_cache(maxsize=1)
def get_qdrant_client() -> QdrantClient:
    """
    Returns a singleton QdrantClient connected to the team's Qdrant Cloud
    cluster, using QDRANT_URL / QDRANT_API_KEY from settings (.env).

    Fails loudly (not silently) if QDRANT_URL isn't configured, same
    "fail closed" philosophy as the webhook secret check in webhook.py -
    better to error clearly now than get a confusing connection error later.
    """
    if not settings.qdrant_url:
        raise RuntimeError(
            "QDRANT_URL is not set. Copy .env.example to .env and fill in "
            "your Qdrant Cloud cluster URL and API key."
        )
    return QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key or None,
    )


@dataclass
class RetrievedChunk:
    """One retrieved chunk, with its score and article provenance."""
    chunk_id: str
    score: float
    text: str
    article_number: str
    category: str | None = None
    short_description: str | None = None
    heading_path: list[str] | None = None


@dataclass
class RetrievalResult:
    """
    Result of a retrieve() call. Either a set of relevant chunks (ok=True),
    or a refusal when nothing met the score threshold (ok=False), with
    diagnostic context either way.
    """
    ok: bool
    query: str
    threshold: float
    best_score: float | None
    chunks: list[RetrievedChunk] = field(default_factory=list)
    refusal_message: str | None = None


def retrieve(
    query: str,
    top_k: int | None = None,
    category: str | None = None,
    score_threshold: float | None = None,
    embedding_fn=sync_embedding_fn,
    client: QdrantClient | None = None,
) -> RetrievalResult:
    """
    Semantic search against the Qdrant collection.

    Returns the top-k most similar chunks to `query`, each with its score
    and article provenance. If the best score doesn't meet the threshold,
    returns a refusal instead (see threshold gating, added next).
    """
    if top_k is None:
        top_k = settings.retrieval_top_k
    if score_threshold is None:
        score_threshold = settings.retrieval_score_threshold
    if client is None:
        client = get_qdrant_client()

    query_vector = embedding_fn(query)

    # Fail fast on embedding/collection dimension mismatch (e.g. 384-dim
    # stub vectors against a 768-dim Gemini collection) instead of letting
    # Qdrant return a cryptic 400.
    try:
        collection_info = client.get_collection(
            collection_name=settings.qdrant_collection_name
        )
        expected_size = collection_info.config.params.vectors.size
        if len(query_vector) != expected_size:
            raise ValueError(
                f"Embedding dimension mismatch: query vector has "
                f"{len(query_vector)} dims but collection "
                f"'{settings.qdrant_collection_name}' expects "
                f"{expected_size}. Re-ingest with the same embedding "
                f"function used for retrieval."
            )
    except ValueError:
        raise
    except Exception:
        pass  # Collection may not exist yet / server unreachable — let query surface it.

    query_filter = None
    if category is not None:
        query_filter = qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="category",
                    match=qmodels.MatchValue(value=category),
                )
            ]
        )

    hits = client.query_points(
        collection_name=settings.qdrant_collection_name,
        query=query_vector,
        limit=top_k,
        query_filter=query_filter,
        with_payload=True,
    ).points

    chunks = []
    for hit in hits:
        payload = hit.payload or {}

        # Support both payload formats:
        # - Our format: text, article_number, category, short_description, heading_path
        # - S2.2 format (embedding.py/qdrant_store.py): short_description, number, kb_category dict
        text = payload.get("text") or payload.get("short_description", "")
        article_number = payload.get("article_number") or payload.get("number", "")
        category_val = payload.get("category")
        if isinstance(category_val, dict):
            category_val = category_val.get("display_value") or category_val.get("value")
        short_description = payload.get("short_description")
        heading_path = payload.get("heading_path")

        chunks.append(
            RetrievedChunk(
                chunk_id=str(hit.id),
                score=hit.score,
                text=text,
                article_number=article_number,
                category=category_val,
                short_description=short_description,
                heading_path=heading_path,
            )
        )

    best_score = max((c.score for c in chunks), default=None)

    if best_score is None or best_score < score_threshold:
        refusal_message = (
            f"No relevant knowledge articles found for query: {query!r}. "
            f"Best score achieved: {best_score}, "
            f"required threshold: {score_threshold}."
        )
        return RetrievalResult(
            ok=False,
            query=query,
            threshold=score_threshold,
            best_score=best_score,
            chunks=[],
            refusal_message=refusal_message,
        )

    return RetrievalResult(
        ok=True,
        query=query,
        threshold=score_threshold,
        best_score=best_score,
        chunks=chunks,
    )