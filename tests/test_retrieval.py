"""
Tests for the S2.3 semantic retrieval module (retrieve()).

These tests run against an in-memory Qdrant instance (no real Qdrant
Cloud connection needed) seeded with a small set of stub-embedded sample
chunks, using retrieve()'s own stub embedding function. This proves the
retrieval logic itself (search, scoring, category filtering, threshold
gating) works correctly, independent of what real embedding model or
real Qdrant collection the team ends up using.

NOTE: retrieve()'s real default is now gemini_embedding_fn (Mathew's
real model), so every retrieve() call below explicitly passes
embedding_fn=default_embedding_fn to keep these tests fast, offline,
and independent of any API key or network access.
"""
import sys
import os

import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qdrant_client import QdrantClient
from qdrant_client import models as qmodels

from barq_ai_support.config import settings
from barq_ai_support.retrieval.retriever import retrieve, default_embedding_fn


COLLECTION = "test_kb_chunks"

SAMPLE_CHUNKS = [
    {"id": 1, "text": "Access denied to resource", "article_number": "KB0010088", "category": "Identity & Access"},
    {"id": 2, "text": "Forgotten password", "article_number": "KB0010087", "category": "Identity & Access"},
    {"id": 3, "text": "Locked out, unable to sign in", "article_number": "KB0010092", "category": "Identity & Access"},
    {"id": 4, "text": "MFA failure after phone change", "article_number": "KB0010094", "category": "Identity & Access"},
    {"id": 5, "text": "low disk space", "article_number": "KB0010033", "category": "Devices & Performance"},
]


def _seeded_client(monkeypatch):
    """
    Creates a fresh in-memory Qdrant client with SAMPLE_CHUNKS uploaded,
    using the stub embedding function. A new client per call keeps tests
    isolated from each other (no shared state).

    Also points settings.qdrant_collection_name at our test collection
    for the duration of the test (via monkeypatch), since retrieve()
    reads the collection name from settings rather than taking it as
    a parameter.
    """
    monkeypatch.setattr(settings, "qdrant_collection_name", COLLECTION)
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config=qmodels.VectorParams(size=384, distance=qmodels.Distance.COSINE),
    )
    client.upsert(
        collection_name=COLLECTION,
        points=[
            qmodels.PointStruct(
                id=chunk["id"],
                vector=default_embedding_fn(chunk["text"]),
                payload=chunk,
            )
            for chunk in SAMPLE_CHUNKS
        ],
    )
    return client


def test_retrieve_returns_ranked_results_with_score_and_provenance(monkeypatch):
    """
    A low threshold should let a real search through. Every returned
    chunk must carry its similarity score and article_number, per the
    brief's provenance requirement.
    """
    client = _seeded_client(monkeypatch)
    result = retrieve(
        "How to reset your ServiceNow password",
        client=client,
        score_threshold=0.0,
        embedding_fn=default_embedding_fn,
    )
    assert result.ok is True
    assert len(result.chunks) > 0
    for chunk in result.chunks:
        assert isinstance(chunk.score, float)
        assert chunk.article_number != ""


def test_retrieve_refuses_below_threshold(monkeypatch):
    """
    A refusal under low relevance is normal, successful execution, not
    an error (per the brief). The refusal must carry full diagnostic
    context: the query, best score achieved, and threshold compared
    against.
    """
    client = _seeded_client(monkeypatch)
    result = retrieve(
        "I forgot my password",
        client=client,
        score_threshold=0.75,
        embedding_fn=default_embedding_fn,
    )
    assert result.ok is False
    assert result.chunks == []
    assert result.refusal_message is not None
    assert "I forgot my password" in result.refusal_message
    assert "0.75" in result.refusal_message


def test_retrieve_filters_by_category(monkeypatch):
    """
    When a category is specified, only chunks from that category should
    ever be returned, regardless of how their scores rank.
    """
    client = _seeded_client(monkeypatch)
    result = retrieve(
        "issue",
        client=client,
        category="Identity & Access",
        score_threshold=0.0,
        embedding_fn=default_embedding_fn,
    )
    assert result.ok is True
    assert len(result.chunks) > 0
    for chunk in result.chunks:
        assert chunk.category == "Identity & Access"


def test_retrieve_refuses_completely_unrelated_query(monkeypatch):
    """
    A query with zero relation to any KB article (e.g. a pancake recipe
    request against IT support articles) should be refused, with a
    clear 'nothing relevant found' message — the exact real-world
    scenario the threshold gate exists to handle.
    """
    client = _seeded_client(monkeypatch)
    result = retrieve(
        "best pancake recipe with blueberries",
        client=client,
        score_threshold=0.75,
        embedding_fn=default_embedding_fn,
    )
    assert result.ok is False
    assert result.chunks == []
    assert result.refusal_message is not None
    assert "pancake" in result.refusal_message


def test_retrieve_uses_config_default_threshold_when_not_specified(monkeypatch):
    """
    Confirms retrieve() actually falls back to settings.retrieval_score_threshold
    (settings.retrieval_score_threshold by default) when the caller doesn't pass score_threshold at all —
    proving the config default is really wired up, not just documented.
    """
    client = _seeded_client(monkeypatch)

    # No score_threshold passed at all — should use settings default.
    result = retrieve(
        "best pancake recipe with blueberries",
        client=client,
        embedding_fn=default_embedding_fn,
    )
    assert result.threshold == settings.retrieval_score_threshold
    assert result.ok is False  # pancake query shouldn't clear the configured bar


def test_retrieve_returns_results_in_descending_score_order(monkeypatch):
    """
    The brief says 'returning the top-k most similar chunks' — results
    must be ordered by relevance, most similar first, not in arbitrary
    or insertion order.
    """
    client = _seeded_client(monkeypatch)
    result = retrieve(
        "How to reset your ServiceNow password",
        client=client,
        score_threshold=0.0,
        embedding_fn=default_embedding_fn,
    )
    scores = [chunk.score for chunk in result.chunks]
    assert scores == sorted(scores, reverse=True)


def test_retrieve_respects_top_k_limit(monkeypatch):
    """
    The brief asks for 'the top-k most similar chunks' — requesting
    fewer results than the total available data should actually cap
    the number of chunks returned, not just rank them.
    """
    client = _seeded_client(monkeypatch)
    result = retrieve(
        "issue",
        client=client,
        top_k=2,
        score_threshold=0.0,
        embedding_fn=default_embedding_fn,
    )
    assert result.ok is True
    assert len(result.chunks) == 2


def test_get_qdrant_client_fails_closed_when_url_unset(monkeypatch):
    """
    get_qdrant_client() is designed to fail loudly if QDRANT_URL isn't
    configured, matching the team's existing 'fail closed' pattern in
    webhook.py's secret check. This pins that deliberate behavior down
    with a test, rather than leaving it unverified.
    """
    from barq_ai_support.retrieval.retriever import get_qdrant_client

    monkeypatch.setattr(settings, "qdrant_url", "")
    get_qdrant_client.cache_clear()  # clear the lru_cache so it re-checks settings

    with pytest.raises(RuntimeError):
        get_qdrant_client()

    get_qdrant_client.cache_clear()  # clean up so later tests aren't affected
