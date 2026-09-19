"""
Tests for S2.2 - embedding, Qdrant storage, and deduplication.

Runs entirely against an in-memory Qdrant instance, using the same
stub embedding function as S2.3's tests, so nothing here needs a real
Qdrant Cloud connection or a real embedding model to run.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qdrant_client import QdrantClient

from barq_ai_support.ingestion.chunker import chunk_articles
from barq_ai_support.ingestion.embed_and_store import (
    build_payload,
    embed_and_store,
    make_point_id,
)

COLLECTION = "test_kb_chunks"

SAMPLE_ARTICLES = [
    {
        "sys_id": "abc123",
        "number": "KB0001",
        "short_description": "VPN authentication fails after a password change",
        "text": "<h2>Symptom</h2><p>VPN client reports authentication failure.</p>",
        "workflow_state": "published",
        "kb_category": "Network",
    },
    {
        "sys_id": "def456",
        "number": "KB0005",
        "short_description": "Account is locked after repeated failed sign-ins",
        "text": "<h2>Symptom</h2><p>User cannot sign in, account shows locked.</p>",
        "workflow_state": "published",
        "kb_category": "Identity & Access",
    },
]


def _fresh_client():
    return QdrantClient(":memory:")


def test_make_point_id_is_deterministic():
    id1 = make_point_id("KB0001", 0)
    id2 = make_point_id("KB0001", 0)
    assert id1 == id2


def test_make_point_id_differs_by_article_and_chunk_index():
    id_a = make_point_id("KB0001", 0)
    id_b = make_point_id("KB0001", 1)
    id_c = make_point_id("KB0005", 0)
    assert len({id_a, id_b, id_c}) == 3


def test_build_payload_maps_servicenow_field_names():
    """S2.3's retrieve() reads 'article_number' and 'category' -
    S2.1's raw chunk metadata has 'number' and 'kb_category' instead."""
    chunk = {
        "text": "some chunk text",
        "metadata": {"number": "KB0001", "kb_category": "Network", "workflow_state": "published"},
    }
    payload = build_payload(chunk)
    assert payload["article_number"] == "KB0001"
    assert payload["category"] == "Network"
    assert payload["workflow_state"] == "published"
    assert payload["text"] == "some chunk text"


def test_embed_and_store_creates_points():
    chunks = chunk_articles(SAMPLE_ARTICLES, chunk_size=400, overlap=50)
    client = _fresh_client()

    summary = embed_and_store(chunks, client=client, collection_name=COLLECTION)

    assert summary["points_upserted"] == len(chunks)
    assert client.count(COLLECTION).count == len(chunks)


def test_rerunning_ingestion_does_not_create_duplicates():
    """NFR-10 / the S2.2 dedup requirement: re-running on unchanged
    chunks must not increase the point count."""
    chunks = chunk_articles(SAMPLE_ARTICLES, chunk_size=400, overlap=50)
    client = _fresh_client()

    embed_and_store(chunks, client=client, collection_name=COLLECTION)
    count_after_first_run = client.count(COLLECTION).count

    embed_and_store(chunks, client=client, collection_name=COLLECTION)
    count_after_second_run = client.count(COLLECTION).count

    assert count_after_first_run == count_after_second_run


def test_stored_points_carry_correct_provenance():
    chunks = chunk_articles(SAMPLE_ARTICLES, chunk_size=400, overlap=50)
    client = _fresh_client()
    embed_and_store(chunks, client=client, collection_name=COLLECTION)

    points, _ = client.scroll(collection_name=COLLECTION, limit=10, with_payload=True)
    article_numbers = {p.payload["article_number"] for p in points}
    assert article_numbers == {"KB0001", "KB0005"}
