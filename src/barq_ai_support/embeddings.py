"""
Embedding functions for Sprint 3+ — real neural embeddings via LiteLLM proxy.

Replaces the SHA-256 stub with Gemini embeddings (gemini-embedding-001, 768-dim).
Falls back to stub if LiteLLM is unavailable (for offline tests).
"""
import hashlib
from typing import Callable

import httpx

from barq_ai_support.config import settings


# Default stub — kept for tests and offline fallback
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


async def gemini_embedding_fn(text: str, dim: int = 768) -> list[float]:
    """
    Real embedding via LiteLLM proxy → Gemini (gemini-embedding-001).
    Returns 768-dimensional vector.
    """
    if not settings.litellm_base_url or not settings.litellm_api_key:
        raise RuntimeError(
            "LITELLM_BASE_URL and LITELLM_API_KEY must be set for real embeddings. "
            "Configure in .env or pass stub embedding_fn explicitly."
        )

    url = f"{settings.litellm_base_url.rstrip('/')}/embeddings"
    headers = {
        "Authorization": f"Bearer {settings.litellm_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "gemini/gemini-embedding-001",
        "input": text,
        "encoding_format": "float",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

    embedding = data["data"][0]["embedding"]
    if len(embedding) != dim:
        raise ValueError(f"Expected {dim}-dim embedding, got {len(embedding)}")
    return embedding


def get_embedding_fn(use_real: bool | None = None) -> Callable[[str], list[float]]:
    """
    Returns the configured embedding function.
    
    Args:
        use_real: If True, force real embeddings (raises if not configured).
                  If False, force stub. If None (default), auto-detect:
                  uses real if LITELLM_API_KEY is set, else stub.
    """
    if use_real is True:
        if not settings.litellm_api_key:
            raise RuntimeError("Real embeddings requested but LITELLM_API_KEY not set")
        return gemini_embedding_fn
    
    if use_real is False:
        return default_embedding_fn
    
    # Auto-detect
    if settings.litellm_api_key:
        return gemini_embedding_fn
    return default_embedding_fn


# Sync wrapper for non-async contexts (e.g., manual ingestion)
def sync_embedding_fn(text: str, dim: int = 384) -> list[float]:
    """Sync embedding fn — uses stub by default, override in production."""
    return default_embedding_fn(text, dim)