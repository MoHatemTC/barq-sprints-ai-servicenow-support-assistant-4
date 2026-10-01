"""
Tests for S3.4 - Agent Tools & Autonomous Execution.

Uses a scripted fake chat model (returns pre-programmed tool_calls in
sequence) rather than a real LLM, and a mocked ServiceNowClient - no real
ServiceNow, Qdrant, or LLM connection needed to run these.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from barq_ai_support.agent.s3_worker import (
    _build_incident_block,
    _select_embedding_fn,
    RunState,
    TERMINAL_TOOL_NAMES,
    build_tools,
    process_incident_event,
    run_agent_loop,
)
from barq_ai_support.retrieval.retriever import RetrievalResult, RetrievedChunk
from barq_ai_support.config import settings


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

INCIDENT = {
    "sys_id": "abc123",
    "number": "INC0010099",
    "short_description": "VPN authentication fails after a password change",
    "description": "Cannot connect to VPN since resetting my password this morning.",
    "category": "network",
}


def make_tool_call(name: str, args: dict, call_id: str = "call_1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


class ScriptedLLM:
    """A fake chat model returning pre-programmed AIMessages in sequence."""

    def __init__(self, responses: list[AIMessage]):
        self._responses = list(responses)
        self.call_count = 0
        self.seen_messages = []

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.call_count += 1
        self.seen_messages.append(list(messages))
        if not self._responses:
            return AIMessage(content="(no more scripted responses)")
        return self._responses.pop(0)


def _fake_retrieval_result(ok=True, chunks=None, query="test query"):
    if chunks is None:
        chunks = [
            RetrievedChunk(
                chunk_id="1", score=0.91, text="Clear the cached credential and reconnect.",
                article_number="KB0001", category="Network",
            )
        ]
    return RetrievalResult(
        ok=ok,
        query=query,
        threshold=0.0,
        best_score=max((c.score for c in chunks), default=None),
        chunks=chunks,
    )


def _mock_sn_client():
    client = AsyncMock()
    client.get_incident.return_value = INCIDENT
    client.update_incident.return_value = {"sys_id": "abc123"}
    client.add_work_note.return_value = {"sys_id": "abc123"}
    return client


# ---------------------------------------------------------------------------
# Registry boundary
# ---------------------------------------------------------------------------

def test_exactly_four_tools_in_registry():
    run = RunState(incident_sys_id="abc123")
    tools = build_tools(run, _mock_sn_client())
    names = {t.name for t in tools}
    assert names == {"searchKB", "addworknote", "suggestAnswer", "requestHR"}


def test_no_resolve_close_reassign_tool():
    run = RunState(incident_sys_id="abc123")
    tools = build_tools(run, _mock_sn_client())
    forbidden = {"resolve", "close", "cancel", "reassign", "reprioritize", "email"}
    for t in tools:
        assert not any(word in t.name.lower() for word in forbidden)


def test_terminal_tool_names_match_registry():
    assert TERMINAL_TOOL_NAMES == {"suggestAnswer", "requestHR"}


def test_embedding_backend_uses_gemini_key_loaded_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "configured-in-env-file")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    from barq_ai_support.retrieval.retriever import gemini_embedding_fn

    assert _select_embedding_fn() is gemini_embedding_fn


def test_incident_block_masks_credentials_and_personal_data():
    incident = {
        **INCIDENT,
        "short_description": (
            'Login failed; token=hunter2; client_secret="my long secret phrase"'
        ),
        "description": "Contact jane.doe@example.com, SSN 123-45-6789",
    }

    block = _build_incident_block(incident)

    assert "hunter2" not in block
    assert "my long secret phrase" not in block
    assert "jane.doe@example.com" not in block
    assert "123-45-6789" not in block
    assert "[REDACTED]" in block
    assert "[REDACTED_EMAIL]" in block
    assert "[REDACTED_SSN]" in block


@pytest.mark.asyncio
async def test_retrieved_tool_output_is_masked_in_tool_message():
    sn_client = _mock_sn_client()
    llm = ScriptedLLM([
        make_tool_call("searchKB", {"query": "VPN login"}, "call_1"),
        make_tool_call("requestHR", {"reason": "No safe resolution found."}, "call_2"),
    ])
    result_with_sensitive_text = _fake_retrieval_result(
        chunks=[
            RetrievedChunk(
                chunk_id="1",
                score=0.91,
                text=(
                    "Use api_key=hunter2; contact jane.doe@example.com "
                    "or call 555-123-4567."
                ),
                article_number="KB0001",
                category="Network",
            )
        ]
    )

    with (
        patch(
            "barq_ai_support.agent.s3_worker.retrieve",
            return_value=result_with_sensitive_text,
        ),
        patch(
            "barq_ai_support.agent.s3_worker.ToolMessage",
            wraps=ToolMessage,
        ) as tool_message_factory,
    ):
        await run_agent_loop(INCIDENT, sn_client, llm=llm)

    tool_message = tool_message_factory.call_args.kwargs["content"]
    assert "hunter2" not in tool_message
    assert "jane.doe@example.com" not in tool_message
    assert "555-123-4567" not in tool_message


# ---------------------------------------------------------------------------
# High-confidence path -> suggestAnswer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_grounded_run_ends_in_suggest_answer():
    sn_client = _mock_sn_client()
    llm = ScriptedLLM([
        make_tool_call("searchKB", {"query": "VPN authentication fails"}, "call_1"),
        make_tool_call(
            "suggestAnswer",
            {
                "procedure": "1. Clear the cached credential. 2. Reconnect.",
                "sources": "KB0001",
                "confidence": 0.91,
            },
            "call_2",
        ),
    ])

    with patch(
        "barq_ai_support.agent.s3_worker.retrieve",
        return_value=_fake_retrieval_result(),
    ):
        result = await run_agent_loop(INCIDENT, sn_client, llm=llm)

    assert result["tool"] == "suggestAnswer"
    assert result["status"] == "suggested"
    assert result["model_reported_confidence"] == 0.91
    assert result["observed_max_retrieval_score"] == 0.91

    # Confirm the actual atomic PATCH was made with the right fields
    sn_client.update_incident.assert_awaited_once()
    call_args = sn_client.update_incident.await_args
    assert call_args.args[0] == "abc123"
    written_fields = call_args.args[1]
    assert written_fields[f"{settings.ai_field_prefix}ai_status"] == "suggested"
    assert written_fields[f"{settings.ai_field_prefix}human_review_required"] is True
    assert written_fields[f"{settings.ai_field_prefix}ai_processed"] is True

@pytest.mark.asyncio
async def test_confidence_is_bounded_even_if_model_reports_out_of_range():
    """The tool schema itself constrains 0-1, but this proves the write
    path also clamps defensively."""
    sn_client = _mock_sn_client()
    run = RunState(incident_sys_id="abc123")
    from barq_ai_support.agent.s3_worker import _terminal_suggest_answer

    result = await _terminal_suggest_answer(run, sn_client, "procedure", "KB0001", confidence=1.5)
    assert result["model_reported_confidence"] == 1.0


# ---------------------------------------------------------------------------
# Unanswerable path -> requestHR
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unanswerable_run_ends_in_request_hr():
    sn_client = _mock_sn_client()
    llm = ScriptedLLM([
        make_tool_call("searchKB", {"query": "unrelated printer jam"}, "call_1"),
        make_tool_call("requestHR", {"reason": "No relevant knowledge base article found."}, "call_2"),
    ])

    with patch(
        "barq_ai_support.agent.s3_worker.retrieve",
        return_value=_fake_retrieval_result(ok=False, chunks=[]),
    ):
        result = await run_agent_loop(INCIDENT, sn_client, llm=llm)

    assert result["tool"] == "requestHR"
    assert result["status"] == "escalated"
    assert "No relevant" in result["reason"]

    written_fields = sn_client.update_incident.await_args.args[1]
    assert written_fields[f"{settings.ai_field_prefix}ai_status"] == "escalated"
    assert written_fields[f"{settings.ai_field_prefix}human_review_required"] is True
    # Escalation reason is appended as a work note
    sn_client.add_work_note.assert_awaited_once()
    note_text = sn_client.add_work_note.await_args.args[1]
    assert "No relevant" in note_text


# ---------------------------------------------------------------------------
# searchKB tool-call trace
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_searchkb_trace_is_recorded_and_observation_returned():
    sn_client = _mock_sn_client()
    llm = ScriptedLLM([
        make_tool_call("searchKB", {"query": "VPN password issue"}, "call_1"),
        make_tool_call("requestHR", {"reason": "test"}, "call_2"),
    ])

    with patch(
        "barq_ai_support.agent.s3_worker.retrieve",
        return_value=_fake_retrieval_result(),
    ) as mock_retrieve:
        await run_agent_loop(INCIDENT, sn_client, llm=llm)

    mock_retrieve.assert_called_once()
    assert mock_retrieve.call_args.args[0] == "VPN password issue" or mock_retrieve.call_args.kwargs.get("query") == "VPN password issue"


# ---------------------------------------------------------------------------
# Step-budget fail-safe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_step_budget_exhausted_triggers_failsafe_escalation():
    sn_client = _mock_sn_client()
    # Model never calls a terminal tool - just keeps producing plain text.
    llm = ScriptedLLM([AIMessage(content="thinking...") for _ in range(10)])

    result = await run_agent_loop(INCIDENT, sn_client, llm=llm, max_steps=3)

    assert result["tool"] == "requestHR"
    assert result["status"] == "escalated"
    assert result["reason"] == "No knowledge article found for this incident."
    assert llm.call_count == 3  # respected the max_steps bound


@pytest.mark.asyncio
async def test_searchkb_result_does_not_end_the_run():
    """A good searchKB hit is handed back to the agent; it must still decide."""
    sn_client = _mock_sn_client()
    llm = ScriptedLLM([
        make_tool_call("searchKB", {"query": "VPN"}, "call_1"),
        make_tool_call("searchKB", {"query": "VPN resolution steps"}, "call_2"),
        make_tool_call(
            "suggestAnswer",
            {"procedure": "1. Reconnect.", "sources": "KB0001", "confidence": 0.91},
            "call_3",
        ),
    ])

    with patch(
        "barq_ai_support.agent.s3_worker.retrieve",
        return_value=_fake_retrieval_result(),
    ):
        result = await run_agent_loop(INCIDENT, sn_client, llm=llm)

    assert llm.call_count == 3
    assert result["tool"] == "suggestAnswer"
    assert result["procedure"] == "1. Reconnect."


@pytest.mark.asyncio
async def test_searchkb_uses_agent_threshold_and_returns_retrieved_chunks():
    run = RunState(incident_sys_id="abc123")
    tools = {t.name: t for t in build_tools(run, _mock_sn_client())}
    chunks = [
        RetrievedChunk(chunk_id="2", score=0.80, text="strong", article_number="KB0001", category="x"),
        RetrievedChunk(chunk_id="3", score=0.70, text="ok", article_number="KB0003", category="x"),
    ]

    with patch(
        "barq_ai_support.agent.s3_worker.retrieve",
        return_value=_fake_retrieval_result(chunks=chunks),
    ) as mock_retrieve:
        output = await tools["searchKB"].ainvoke({"query": "VPN"})

    assert mock_retrieve.call_args.kwargs["score_threshold"] == settings.agent_chunk_threshold
    assert [c["article_number"] for c in output] == ["KB0001", "KB0003"]
    assert run.max_retrieval_score == 0.80


@pytest.mark.asyncio
async def test_escalates_after_three_searches_without_suggest_answer():
    sn_client = _mock_sn_client()
    llm = ScriptedLLM([
        make_tool_call("searchKB", {"query": f"attempt {i}"}, f"call_{i}")
        for i in range(1, 6)
    ])

    with patch(
        "barq_ai_support.agent.s3_worker.retrieve",
        return_value=_fake_retrieval_result(ok=False, chunks=[]),
    ) as mock_retrieve:
        result = await run_agent_loop(INCIDENT, sn_client, llm=llm, max_steps=10)

    assert mock_retrieve.call_count == 3
    assert result["tool"] == "requestHR"
    assert result["reason"] == "No knowledge article found for this incident."
    note_text = sn_client.add_work_note.await_args.args[1]
    assert "No knowledge article found for this incident." in note_text


# ---------------------------------------------------------------------------
# Retry-once behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_retried_once_on_failure_then_succeeds():
    sn_client = _mock_sn_client()
    sn_client.update_incident.side_effect = [Exception("transient"), {"sys_id": "abc123"}]

    run = RunState(incident_sys_id="abc123")
    from barq_ai_support.agent.s3_worker import _terminal_suggest_answer

    result = await _terminal_suggest_answer(run, sn_client, "procedure", "KB0001", confidence=0.8)

    assert result["status"] == "suggested"
    assert sn_client.update_incident.await_count == 2  # first failed, retry succeeded


@pytest.mark.asyncio
async def test_write_failing_twice_propagates():
    sn_client = _mock_sn_client()
    sn_client.update_incident.side_effect = Exception("persistent failure")

    run = RunState(incident_sys_id="abc123")
    from barq_ai_support.agent.s3_worker import _terminal_suggest_answer

    with pytest.raises(Exception, match="persistent failure"):
        await _terminal_suggest_answer(run, sn_client, "procedure", "KB0001", confidence=0.8)

    assert sn_client.update_incident.await_count == 2  # one try + one retry, both failed


# ---------------------------------------------------------------------------
# Error containment: recoverable failure, worker continuity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unhandled_failure_leaves_incident_recoverable_and_writes_failure_note():
    sn_client = _mock_sn_client()
    sn_client.get_incident.side_effect = Exception("ServiceNow unreachable")

    result = await process_incident_event({"sys_id": "abc123"}, sn_client=sn_client)

    assert result["status"] == "error"
    assert "ServiceNow unreachable" in result["error"]
    # ai_status / ai_confidence / ai_suggested_response must NEVER be touched on this path
    sn_client.update_incident.assert_not_awaited()
    # A failure work note is written (best-effort)
    sn_client.add_work_note.assert_awaited_once()
    note_text = sn_client.add_work_note.await_args.args[1]
    assert "failed" in note_text.lower()


@pytest.mark.asyncio
async def test_missing_sys_id_returns_error_without_raising():
    result = await process_incident_event({"number": "INC0010099"})
    assert result["status"] == "error"
    assert "sys_id" in result["error"]


@pytest.mark.asyncio
async def test_worker_survives_one_bad_job_and_processes_the_next():
    """The core continuity requirement: one failed job must not prevent
    subsequent jobs in the same process from succeeding."""
    failing_client = _mock_sn_client()
    failing_client.get_incident.side_effect = Exception("boom")

    succeeding_client = _mock_sn_client()
    llm = ScriptedLLM([make_tool_call("requestHR", {"reason": "test"}, "call_1")])

    result_1 = await process_incident_event({"sys_id": "job1"}, sn_client=failing_client, llm=llm)
    assert result_1["status"] == "error"

    # Same process, next job, different (healthy) client - must still work.
    result_2 = await process_incident_event({"sys_id": "job2"}, sn_client=succeeding_client, llm=llm)
    assert result_2["status"] == "escalated"
    assert result_2["tool"] == "requestHR"