"""
S2.3 - Semantic Retrieval & Threshold Gate

Step 1: Qdrant client connection.
"""

from functools import lru_cache

from qdrant_client import QdrantClient

from qdrant_client import models as qmodels

from ..config import settings

from dataclasses import dataclass, field


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

import hashlib


def default_embedding_fn(text: str, dim: int = 384) -> list[float]:
    """
    Stub embedding function — NOT a real AI model. Turns text into a
    deterministic fake vector using a hash, just so we have *something*
    consistent to test retrieve() against.

    Per the brief: "You may build and test against stub vectors" — real
    embeddings (e.g. sentence-transformers) can replace this later without
    changing any other code, since retrieve() just calls whatever function
    is passed to it.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    raw_bytes = (digest * (dim // len(digest) + 1))[:dim]
    vector = [(b / 127.5) - 1.0 for b in raw_bytes]
    return vector

import os
from google import genai

_gemini_client = None


def gemini_embedding_fn(text: str) -> list[float]:
    """
    Real embedding function using Google's Gemini API — matches Mathew's
    S2.2 ingestion setup exactly (same model, same output dimension),
    so search queries are embedded the same way his article chunks were.

    Requires GEMINI_API_KEY in .env. Falls back to raising a clear error
    if the key isn't set, rather than failing with a confusing API error.
    """
    global _gemini_client
    if _gemini_client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Add it to your .env file to "
                "use real embeddings instead of the stub."
            )
        _gemini_client = genai.Client(api_key=api_key)

    response = _gemini_client.models.embed_content(
        model="gemini-embedding-001",
        contents=text,
        config={"output_dimensionality": 768},
    )
    return response.embeddings[0].values

def retrieve(
    query: str,
    top_k: int | None = None,
    category: str | None = None,
    score_threshold: float | None = None,
    embedding_fn=gemini_embedding_fn,
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

    chunks = [
        RetrievedChunk(
            chunk_id=str(hit.id),
            score=hit.score,
            text=hit.payload.get("short_description", "") or hit.payload.get("text", ""),
            article_number=hit.payload.get("number", "") or hit.payload.get("article_number", ""),
            category=(hit.payload.get("kb_category") or {}).get("display_value")
                if isinstance(hit.payload.get("kb_category"), dict)
                else hit.payload.get("category"),
        )
        for hit in hits
    ]

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
