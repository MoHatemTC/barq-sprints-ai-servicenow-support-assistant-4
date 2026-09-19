"""
S2.2 — Ingestion: Embedding generation and Qdrant vector storage.
"""

import uuid
from typing import Callable, Any
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, PayloadSchemaType

from ..config import settings
from ..embeddings import sync_embedding_fn
from ..retrieval.retriever import get_qdrant_client


def ensure_payload_indexes(
    client: QdrantClient,
    collection_name: str = settings.qdrant_collection_name,
) -> None:
    """Create keyword indexes for filterable payload fields, idempotently.

    Safe to call on every run: existing indexes are left untouched, and
    failures for one field don't block the others.
    """
    for field_name in ("category", "article_number", "sys_id"):
        try:
            client.create_payload_index(
                collection_name=collection_name,
                field_name=field_name,
                field_schema=PayloadSchemaType.KEYWORD,
            )
            print(f"Created payload index for '{field_name}'.")
        except Exception as e:
            # Index already exists or server rejected it — log and continue.
            print(f"Note: payload index for '{field_name}' not created: {e}")


def ensure_collection_exists(
    client: QdrantClient,
    collection_name: str = settings.qdrant_collection_name,
    vector_size: int = 384,
    distance: Distance = Distance.COSINE,
) -> None:
    """
    Checks if the collection exists in Qdrant Cloud; if not, creates it.
    Payload indexes for filterable fields are ensured on EVERY run, so an
    existing collection created before filtering was added still gets them.
    """
    collections = client.get_collections().collections
    exists = any(c.name == collection_name for c in collections)

    if not exists:
        print(f"Collection '{collection_name}' does not exist. Creating...")
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=vector_size, distance=distance),
        )
        print(f"Collection '{collection_name}' created successfully.")
    else:
        print(f"Collection '{collection_name}' already exists.")

    ensure_payload_indexes(client, collection_name=collection_name)


def ingest_chunks_to_qdrant(
    chunks: list[dict[str, Any]],
    client: QdrantClient | None = None,
    collection_name: str = settings.qdrant_collection_name,
    embedding_fn: Callable[[str], list[float]] = sync_embedding_fn,
    recreate_collection: bool = False,
    vector_size: int = 384,
) -> int:
    """
    Embeds list of article chunks and upserts them into Qdrant collection.
    
    Each chunk dict format:
    {
        "text": "...chunk text...",
        "metadata": {
            "sys_id": "...",
            "number": "KB0010001",
            "short_description": "...",
            "kb_category": "...",
            "heading_path": [...],
            "chunk_index": 0
        }
    }
    """
    if client is None:
        client = get_qdrant_client()

    if recreate_collection:
        print(f"Re-creating collection '{collection_name}'...")
        try:
            client.delete_collection(collection_name=collection_name)
        except Exception:
            pass  # Collection may not exist
        ensure_collection_exists(
            client, collection_name=collection_name, vector_size=vector_size
        )
    else:
        ensure_collection_exists(
            client, collection_name=collection_name, vector_size=vector_size
        )

    points = []
    for chunk in chunks:
        text = chunk["text"]
        metadata = chunk["metadata"]
        vector = embedding_fn(text)

        raw_cat = metadata.get("kb_category")
        if isinstance(raw_cat, dict):
            category = raw_cat.get("display_value") or raw_cat.get("value") or ""
        elif raw_cat is not None:
            category = str(raw_cat).strip()
        else:
            category = ""

        payload = {
            "text": text,
            "sys_id": str(metadata.get("sys_id", "")),
            "article_number": str(metadata.get("number", "")),
            "short_description": str(metadata.get("short_description", "")),
            "category": category,
            "heading_path": metadata.get("heading_path", []),
            "chunk_index": metadata.get("chunk_index", 0),
        }

        # Deterministic UUID or standard v4 string
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{payload['sys_id']}_{payload['chunk_index']}"))

        points.append(
            PointStruct(
                id=point_id,
                vector=vector,
                payload=payload,
            )
        )

    if points:
        client.upsert(
            collection_name=collection_name,
            points=points,
        )
        print(f"Successfully upserted {len(points)} chunks into Qdrant collection '{collection_name}'.")

    return len(points)
