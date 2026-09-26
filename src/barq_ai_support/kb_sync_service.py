"""
S3.3 — KB sync orchestration. Implements all four sync paths, reusing the
existing chunker/embedding/qdrant_store code from Sprint 2 rather than
duplicating it.

Also runnable standalone for a single article:
    uv run python -m src.barq_ai_support.kb_sync_service <sys_id> <insert|update|delete>
"""
import asyncio
import hashlib
import sys

from qdrant_client.models import Filter, FieldCondition, MatchValue

from .ingestion.chunker import chunk_article
from .ingestion import qdrant_store  # reuse client, COLLECTION_NAME, generate_point_id, upsert_chunks
from .servicenow_client import ServiceNowClient
from . import kb_state_store as state_store


def _hash_body(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _article_filter(sys_id: str) -> Filter:
    return Filter(must=[FieldCondition(key="sys_id", match=MatchValue(value=sys_id))])


def _ensure_sys_id_index() -> None:
    """
    Qdrant requires a payload index before filtering by a field. Idempotent —
    safe to call on every sync, only actually creates the index once.
    """
    try:
        qdrant_store.client.create_payload_index(
            collection_name=qdrant_store.COLLECTION_NAME,
            field_name="sys_id",
            field_schema="keyword",
        )
    except Exception:
        pass  # index already exists — safe to ignore


def _delete_points_for_article(sys_id: str) -> None:
    qdrant_store.client.delete(
        collection_name=qdrant_store.COLLECTION_NAME,
        points_selector=_article_filter(sys_id),
    )


def _patch_metadata_only(sys_id: str, metadata: dict) -> None:
    """Path 3: update payload fields on existing points, no embedding calls."""
    patch_fields = {
        "short_description": state_store._flatten(metadata.get("short_description")),
        "kb_category": state_store._flatten(metadata.get("kb_category")),
        "workflow_state": state_store._flatten(metadata.get("workflow_state")),
    }
    qdrant_store.client.set_payload(
        collection_name=qdrant_store.COLLECTION_NAME,
        payload=patch_fields,
        points=_article_filter(sys_id),
    )

def sync_article(sys_id: str, operation: str) -> dict:
    state_store.init_db()
    qdrant_store.create_collection()
    _ensure_sys_id_index()

    if operation == "delete":
        _delete_points_for_article(sys_id)
        state_store.delete_article_state(sys_id)
        return {"sys_id": sys_id, "action": "deleted"}
    
    # insert or update: pull full current article via Table API
    client = ServiceNowClient()
    article = asyncio.run(client.get_kb_article(sys_id))

    body = article.get("text", "")
    new_hash = _hash_body(body)
    existing = state_store.get_article_state(sys_id)

    if existing is None or existing.body_hash != new_hash:
        # Path 2: new article or body changed -> delete old points, re-chunk, re-embed, upsert
        _delete_points_for_article(sys_id)
        chunks = chunk_article(article, chunk_size=1000, overlap=100)
        qdrant_store.upsert_chunks(chunks)
        state_store.upsert_article_state(sys_id, new_hash, article)
        return {"sys_id": sys_id, "action": "re_embedded", "chunk_count": len(chunks)}

    # Body unchanged — check if metadata actually differs
    metadata_changed = (
        existing.short_description != article.get("short_description")
        or existing.kb_category != article.get("kb_category")
        or existing.workflow_state != article.get("workflow_state")
    )

    if metadata_changed:
        # Path 3: metadata-only -> patch payloads, no embedding calls
        _patch_metadata_only(sys_id, article)
        state_store.upsert_article_state(sys_id, new_hash, article)
        return {"sys_id": sys_id, "action": "metadata_patched"}

    # Path 4: unchanged -> no-op
    return {"sys_id": sys_id, "action": "skipped_unchanged"}


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[2] not in ("insert", "update", "delete"):
        print("Usage: uv run python -m src.barq_ai_support.kb_sync_service <sys_id> <insert|update|delete>")
        sys.exit(1)

    result = sync_article(sys.argv[1], sys.argv[2])
    print(result)