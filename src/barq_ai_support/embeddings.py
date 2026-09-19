"""
Embedding functions — stub for offline tests, Gemini via LiteLLM for real use.

All public embedding callables are SYNCHRONOUS: Callable[[str], list[float]].
The Gemini path uses httpx's sync client so it can be passed directly to
retrieve() and ingest_chunks_to_qdrant() without an event loop.
"""

from __future__ import annotations

import hashlib
from typing import Callable

import httpx

from barq_ai_support.config import settings

STUB_DIM = 384
STUB_MODEL = "sha256-stub-384"
GEMINI_MODEL = "gemini/gemini-embedding-001"
GEMINI_DIM = 768

EmbeddingFn = Callable[[str], list[float]]


def default_embedding_fn(text: str, dim: int = STUB_DIM) -> list[float]:
    """
    Stub embedding — NOT a real AI model. Deterministic SHA-256 hash vector
    for offline tests. Carries no semantic meaning: identical text scores
    1.0, anything else is effectively random. Never use for threshold
    calibration or production retrieval.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    raw_bytes = (digest * (dim // len(digest) + 1))[:dim]
    return [(b / 127.5) - 1.0 for b in raw_bytes]


def _post_litellm_embeddings(texts: list[str]) -> list[list[float]]:
    """POST a batch of texts to the LiteLLM /embeddings endpoint."""
    if not settings.litellm_base_url or not settings.litellm_api_key:
        raise RuntimeError(
            "LITELLM_BASE_URL and LITELLM_API_KEY must be set for real embeddings. "
            "Copy .env.example to .env and fill them in, or pass the stub "
            "embedding_fn explicitly for offline use."
        )
    url = f"{settings.litellm_base_url.rstrip('/')}/embeddings"
    headers = {
        "Authorization": f"Bearer {settings.litellm_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GEMINI_MODEL,
        "input": texts,
        "encoding_format": "float",
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    items = sorted(data["data"], key=lambda d: d["index"])
    return [item["embedding"] for item in items]


def gemini_embedding_fn(text: str, dim: int = GEMINI_DIM) -> list[float]:
    """
    Real sync embedding via LiteLLM proxy -> Gemini (gemini-embedding-001).
    Returns a 768-dimensional vector. Drop-in for retrieve()/ingestion.
    """
    (embedding,) = _post_litellm_embeddings([text])
    if len(embedding) != dim:
        raise ValueError(f"Expected {dim}-dim embedding, got {len(embedding)}")
    return embedding


async def gemini_embedding_fn_async(text: str, dim: int = GEMINI_DIM) -> list[float]:
    """Async variant (same endpoint). Prefer the sync version unless awaiting."""
    if not settings.litellm_base_url or not settings.litellm_api_key:
        raise RuntimeError(
            "LITELLM_BASE_URL and LITELLM_API_KEY must be set for real embeddings."
        )
    url = f"{settings.litellm_base_url.rstrip('/')}/embeddings"
    headers = {
        "Authorization": f"Bearer {settings.litellm_api_key}",
        "Content-Type": "application/json",
    }
    payload = {"model": GEMINI_MODEL, "input": text, "encoding_format": "float"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    embedding = data["data"][0]["embedding"]
    if len(embedding) != dim:
        raise ValueError(f"Expected {dim}-dim embedding, got {len(embedding)}")
    return embedding


def get_embedding_fn(use_real: bool | None = None) -> EmbeddingFn:
    """
    Returns a SYNC embedding function.

    - use_real=True: Gemini via LiteLLM (raises if credentials missing).
    - use_real=False: deterministic stub (offline-safe, hermetic tests).
    - use_real=None: real if LITELLM_API_KEY is set, else stub.
      NOTE: callers that must stay hermetic (unit tests, default
      retrieve()/ingest signatures) should pass use_real=False explicitly
      instead of relying on auto-detect.
    """
    if use_real is True:
        if not settings.litellm_api_key:
            raise RuntimeError("Real embeddings requested but LITELLM_API_KEY not set")
        return gemini_embedding_fn
    if use_real is False:
        return default_embedding_fn
    if settings.litellm_api_key:
        return gemini_embedding_fn
    return default_embedding_fn


def embedding_model_name(fn: EmbeddingFn) -> str:
    """Human-readable model label for benchmark provenance records."""
    if fn is gemini_embedding_fn:
        return GEMINI_MODEL
    return STUB_MODEL


def embedding_dim(fn: EmbeddingFn) -> int:
    """Vector dimension produced by the given embedding function."""
    if fn is gemini_embedding_fn:
        return GEMINI_DIM
    return STUB_DIM


# Backwards-compatible alias: explicit stub for non-async contexts.
def sync_embedding_fn(text: str, dim: int = STUB_DIM) -> list[float]:
    """Explicit stub embedding (offline-safe). For auto/real, use get_embedding_fn()."""
    return default_embedding_fn(text, dim)
