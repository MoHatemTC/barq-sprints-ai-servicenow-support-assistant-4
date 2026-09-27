"""
tests/test_schemas.py
─────────────────────
BARQ G4 · S2.5 — Unit tests for agent/schemas.py.

Tests:
- KnowledgeChunk validation (valid, empty text, malformed ID, missing fields)
- AgentInput validation (blank incident, duplicate IDs)
- AgentOutput display text rendering
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.prompt import CLEAN_DECLINE
from agent.schemas import AgentInput, AgentOutput, KnowledgeChunk, ResolutionStep


# ── KnowledgeChunk ─────────────────────────────────────────────────────────────

class TestKnowledgeChunk:

    def test_valid_chunk(self):
        chunk = KnowledgeChunk(id="KB-101", title="VPN Guide", text="Step 1: ...")
        assert chunk.id == "KB-101"
        assert chunk.title == "VPN Guide"

    def test_id_stripped(self):
        chunk = KnowledgeChunk(id="  KB-101  ", title="T", text="body")
        assert chunk.id == "KB-101"

    def test_invalid_id_format(self):
        with pytest.raises(ValidationError, match="Invalid KB chunk id"):
            KnowledgeChunk(id="KB101", title="T", text="body")

    def test_invalid_id_no_digits(self):
        with pytest.raises(ValidationError, match="Invalid KB chunk id"):
            KnowledgeChunk(id="KB-", title="T", text="body")

    def test_invalid_id_alpha_suffix(self):
        with pytest.raises(ValidationError, match="Invalid KB chunk id"):
            KnowledgeChunk(id="KB-101a", title="T", text="body")

    def test_invalid_id_arbitrary_string(self):
        with pytest.raises(ValidationError):
            KnowledgeChunk(id="ARTICLE-5", title="T", text="body")

    def test_blank_title(self):
        with pytest.raises(ValidationError, match="title must not be blank"):
            KnowledgeChunk(id="KB-101", title="   ", text="body")

    def test_blank_text(self):
        with pytest.raises(ValidationError, match="text must not be blank"):
            KnowledgeChunk(id="KB-101", title="T", text="   ")

    def test_empty_text(self):
        with pytest.raises(ValidationError, match="text must not be blank"):
            KnowledgeChunk(id="KB-101", title="T", text="")

    def test_missing_text_field(self):
        with pytest.raises(ValidationError):
            KnowledgeChunk(id="KB-101", title="T")

    def test_text_stripped(self):
        chunk = KnowledgeChunk(id="KB-101", title="T", text="  body content  ")
        assert chunk.text == "body content"


# ── AgentInput ────────────────────────────────────────────────────────────────

class TestAgentInput:

    def test_valid_input(self):
        inp = AgentInput(
            incident_text="VPN is down.",
            retrieved_chunks=[
                KnowledgeChunk(id="KB-101", title="T", text="body"),
            ],
        )
        assert inp.incident_text == "VPN is down."
        assert len(inp.retrieved_chunks) == 1

    def test_blank_incident_text(self):
        with pytest.raises(ValidationError, match="must not be blank"):
            AgentInput(incident_text="   ", retrieved_chunks=[])

    def test_empty_incident_text(self):
        with pytest.raises(ValidationError, match="must not be blank"):
            AgentInput(incident_text="", retrieved_chunks=[])

    def test_empty_chunks_allowed(self):
        inp = AgentInput(incident_text="VPN is down.", retrieved_chunks=[])
        assert inp.retrieved_chunks == []

    def test_duplicate_chunk_ids(self):
        with pytest.raises(ValidationError, match="Duplicate KB chunk IDs"):
            AgentInput(
                incident_text="VPN is down.",
                retrieved_chunks=[
                    KnowledgeChunk(id="KB-101", title="T1", text="body1"),
                    KnowledgeChunk(id="KB-101", title="T2", text="body2"),
                ],
            )

    def test_multiple_valid_chunks(self):
        inp = AgentInput(
            incident_text="Incident.",
            retrieved_chunks=[
                KnowledgeChunk(id="KB-101", title="T1", text="b1"),
                KnowledgeChunk(id="KB-102", title="T2", text="b2"),
                KnowledgeChunk(id="KB-103", title="T3", text="b3"),
            ],
        )
        assert len(inp.retrieved_chunks) == 3


# ── AgentOutput ───────────────────────────────────────────────────────────────

class TestAgentOutput:

    def test_declined_display(self):
        out = AgentOutput(status="declined", decline_message=CLEAN_DECLINE)
        assert out.as_display_text() == CLEAN_DECLINE

    def test_error_display(self):
        out = AgentOutput(status="error", error_message="LLM quota exceeded")
        assert "LLM quota exceeded" in out.as_display_text()

    def test_grounded_display(self):
        out = AgentOutput(
            status="grounded",
            incident_summary="VPN cannot connect",
            steps=[
                ResolutionStep(step=1, instruction="Verify version.", citations=["KB-101"]),
                ResolutionStep(step=2, instruction="Check ports.", citations=["KB-102"]),
            ],
            cited_ids=["KB-101", "KB-102"],
        )
        text = out.as_display_text()
        assert "1. Verify version." in text
        assert "[KB-101]" in text
        assert "2. Check ports." in text
        assert "[KB-102]" in text
        assert "suggestion for support agent review" in text
        assert "No ticket actions have been taken." in text

    def test_grounded_display_contains_summary(self):
        out = AgentOutput(
            status="grounded",
            incident_summary="Cannot connect to VPN",
            steps=[
                ResolutionStep(step=1, instruction="Check version.", citations=["KB-101"]),
            ],
            cited_ids=["KB-101"],
        )
        text = out.as_display_text()
        assert "Cannot connect to VPN" in text
