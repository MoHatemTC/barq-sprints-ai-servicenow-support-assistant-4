"""
S2.2 - Embedding, Qdrant Setup & Dedup

Takes chunks produced by S2.1's chunk_articles() (or any chunks in the
same {"text": ..., "metadata": {...}} shape) and stores them in Qdrant
so S2.3's retrieve() can search them.

Compatibility note: S2.3's retrieve() expects each point's payload to
have "text", "article_number" and "category" keys, a collection with
384-dimensional vectors, and Cosine distance (see
tests/test_retrieval.py). This module builds points in exactly that
shape, remapping S2.1's raw ServiceNow field names ("number",
"kb_category") onto the names S2.3 reads.

Uses the same stub embedding function as S2.3 (default_embedding_fn)
so vectors stored here and vectors used at query time are produced the
same way. Swap embedding_fn for a real model later (e.g.
sentence-transformers) without changing anything else - both this
module and retrieve() take embedding_fn as a parameter for exactly
this reason.
"""

import uuid

from qdrant_client import QdrantClient
from qdrant_client import models as qmodels

from ..config import settings
from ..retrieval.retriever import default_embedding_fn, get_qdrant_client

VECTOR_SIZE = 384

# Fixed namespace so make_point_id() is deterministic across runs and
# across machines - the same (article_number, chunk_index) always
# produces the same UUID.
_POINT_ID_NAMESPACE = uuid.UUID("a3f1c2d4-5b6e-4f7a-8c9d-0e1f2a3b4c5d")


def make_point_id(article_number: str, chunk_index: int) -> str:
    """
    Deterministic point ID for one (article, chunk) pair.

    Re-running ingestion on the same chunk always produces the same ID,
    so upserting it again overwrites the existing point instead of
    creating a duplicate (NFR-10 / the S2.2 dedup requirement).
    """
    key = f"{article_number}:{chunk_index}"
    return str(uuid.uuid5(_POINT_ID_NAMESPACE, key))


def build_payload(chunk: dict) -> dict:
    """
    Turn one S2.1-shaped chunk into the payload S2.3's retrieve() reads.

    S2.1's chunk_article() copies every article field except "text"
    straight into metadata, using ServiceNow's raw field names. This
    remaps the ones retrieve() actually looks up, and keeps the rest
    around too (harmless extra payload data), including the article's
    workflow state so a "published only" filter can be added later
    without re-ingesting.
    """
    metadata = chunk.get("metadata", {})

    article_number = metadata.get("article_number") or metadata.get("number", "")
    category = metadata.get("category") or metadata.get("kb_category")
    workflow_state = metadata.get("workflow_state")

    payload = {
        **metadata,
        "text": chunk["text"],
        "article_number": article_number,
        "category": category,
        "workflow_state": workflow_state,
    }
    return payload


def ensure_collection(
    client: QdrantClient,
    collection_name: str,
    vector_size: int = VECTOR_SIZE,
) -> None:
    """Create the collection if it doesn't already exist. Safe to call every run."""
    if not client.collection_exists(collection_name):
        client.create_collection(
            collection_name=collection_name,
            vectors_config=qmodels.VectorParams(
                size=vector_size,
                distance=qmodels.Distance.COSINE,
            ),
        )


def embed_and_store(
    chunks: list[dict],
    client: QdrantClient | None = None,
    embedding_fn=default_embedding_fn,
    collection_name: str | None = None,
) -> dict:
    """
    Embed each chunk and upsert it into Qdrant.

    Returns a small summary dict so a caller (or the manual run script)
    can report what happened without needing to inspect Qdrant itself.
    """
    if client is None:
        client = get_qdrant_client()
    if collection_name is None:
        collection_name = settings.qdrant_collection_name

    ensure_collection(client, collection_name)

    points = []
    for chunk in chunks:
        metadata = chunk.get("metadata", {})
        article_number = metadata.get("article_number") or metadata.get("number", "")
        chunk_index = metadata.get("chunk_index", 0)

        points.append(
            qmodels.PointStruct(
                id=make_point_id(article_number, chunk_index),
                vector=embedding_fn(chunk["text"]),
                payload=build_payload(chunk),
            )
        )

    if points:
        client.upsert(collection_name=collection_name, points=points)

    return {
        "collection_name": collection_name,
        "chunks_processed": len(chunks),
        "points_upserted": len(points),
    }
