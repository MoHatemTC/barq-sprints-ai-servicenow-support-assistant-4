"""
S2.3 - Semantic Retrieval & Threshold Gate

Step 1: Qdrant client connection.
"""

from functools import lru_cache

from qdrant_client import QdrantClient

from ..config import settings


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