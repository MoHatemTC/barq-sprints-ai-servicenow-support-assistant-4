from unittest.mock import patch

from barq_ai_support.ingestion.qdrant_store import (
    generate_point_id,
    upsert_chunks,
)


def test_generate_point_id_is_deterministic():
    chunk = {
        "text": "Password is not working.",
        "metadata": {
            "sys_id": "article-001",
            "chunk_index": 0,
        },
    }

    first_id = generate_point_id(chunk)
    second_id = generate_point_id(chunk)

    assert first_id == second_id


def test_different_chunks_have_different_ids():
    chunk_1 = {
        "text": "Password is not working.",
        "metadata": {
            "sys_id": "article-001",
            "chunk_index": 0,
        },
    }

    chunk_2 = {
        "text": "Reset the password.",
        "metadata": {
            "sys_id": "article-001",
            "chunk_index": 1,
        },
    }

    assert generate_point_id(chunk_1) != generate_point_id(chunk_2)


@patch(
    "barq_ai_support.ingestion.qdrant_store.create_embedding"
)
@patch(
    "barq_ai_support.ingestion.qdrant_store.client.upsert"
)
def test_upsert_chunks(
    mock_upsert,
    mock_create_embedding,
):
    mock_create_embedding.return_value = [0.1] * 768

    chunks = [
        {
            "text": "Cause > Password is not working.",
            "metadata": {
                "sys_id": "article-001",
                "number": "KB001",
                "workflow_state": "published",
                "category": "Password",
                "heading_path": ["Cause"],
                "chunk_index": 0,
            },
        }
    ]

    upsert_chunks(chunks)

    mock_create_embedding.assert_called_once_with(
        "Cause > Password is not working."
    )

    mock_upsert.assert_called_once()

    call_kwargs = mock_upsert.call_args.kwargs

    assert call_kwargs["collection_name"] == "barq_kb_chunks"

    points = call_kwargs["points"]

    assert len(points) == 1

    assert len(points[0].vector) == 768

    assert points[0].payload["sys_id"] == "article-001"
    assert points[0].payload["number"] == "KB001"
    assert points[0].payload["workflow_state"] == "published"
    assert points[0].payload["category"] == "Password"
    assert points[0].payload["heading_path"] == ["Cause"]
    assert points[0].payload["chunk_index"] == 0