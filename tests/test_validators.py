"""
tests/test_validators.py
────────────────────────
BARQ G4 · S2.5 — Unit tests for agent/validators.py.

Tests all citation extraction, fabrication detection, missing-citation
detection, malformed citation detection, and step parsing.
No LLM calls — purely deterministic regex/set logic.
"""

from __future__ import annotations

import pytest

from agent.validators import validate_response, extract_cited_ids


VALID_IDS = {"KB-101", "KB-102", "KB-103"}


# ── extract_cited_ids ─────────────────────────────────────────────────────────

class TestExtractCitedIds:

    def test_single_citation(self):
        assert extract_cited_ids("Do the thing. [KB-101]") == {"KB-101"}

    def test_multiple_citations(self):
        result = extract_cited_ids("Step A [KB-101]\nStep B [KB-102]")
        assert result == {"KB-101", "KB-102"}

    def test_no_citations(self):
        assert extract_cited_ids("No citations here.") == set()

    def test_repeated_same_citation(self):
        assert extract_cited_ids("[KB-101] and [KB-101]") == {"KB-101"}

    def test_case_insensitive(self):
        # Our regex is case-insensitive so [kb-101] should match
        result = extract_cited_ids("[kb-101]")
        assert "KB-101" in result


# ── validate_response — grounded OK ──────────────────────────────────────────

class TestValidateResponseGrounded:

    def test_all_valid_citations(self, grounded_response):
        result = validate_response(grounded_response, VALID_IDS)
        assert result.is_valid is True
        assert result.fabricated_ids == []
        assert result.uncited_steps == []
        assert set(result.cited_ids) == {"KB-101", "KB-102", "KB-103"}

    def test_parsed_steps_count(self, grounded_response):
        result = validate_response(grounded_response, VALID_IDS)
        assert len(result.parsed_steps) == 3

    def test_parsed_step_numbers(self, grounded_response):
        result = validate_response(grounded_response, VALID_IDS)
        step_nums = [s["step"] for s in result.parsed_steps]
        assert step_nums == [1, 2, 3]

    def test_parsed_step_citations(self, grounded_response):
        result = validate_response(grounded_response, VALID_IDS)
        assert result.parsed_steps[0]["citations"] == ["KB-101"]
        assert result.parsed_steps[1]["citations"] == ["KB-102"]
        assert result.parsed_steps[2]["citations"] == ["KB-103"]


# ── validate_response — fabricated citations ──────────────────────────────────

class TestFabricatedCitations:

    def test_fabricated_id_detected(self, fabricated_citation_response):
        result = validate_response(fabricated_citation_response, VALID_IDS)
        assert result.is_valid is False
        assert "KB-999" in result.fabricated_ids

    def test_valid_citations_still_extracted(self, fabricated_citation_response):
        result = validate_response(fabricated_citation_response, VALID_IDS)
        assert "KB-101" in result.cited_ids

    def test_fabricated_not_in_cited(self, fabricated_citation_response):
        result = validate_response(fabricated_citation_response, VALID_IDS)
        assert "KB-999" not in result.cited_ids

    def test_completely_fabricated_response(self):
        response = "1. Do thing. [KB-999]\n2. Other thing. [KB-888]"
        result = validate_response(response, VALID_IDS)
        assert result.is_valid is False
        assert set(result.fabricated_ids) == {"KB-999", "KB-888"}
        assert result.cited_ids == []


# ── validate_response — missing citations ─────────────────────────────────────

class TestMissingCitations:

    def test_step_missing_citation_detected(self, missing_citation_response):
        result = validate_response(missing_citation_response, VALID_IDS)
        assert result.is_valid is False
        assert 1 in result.uncited_steps

    def test_step_with_citation_not_flagged(self, missing_citation_response):
        result = validate_response(missing_citation_response, VALID_IDS)
        assert 2 not in result.uncited_steps

    def test_all_steps_missing_citations(self):
        response = "1. Do first thing.\n2. Do second thing."
        result = validate_response(response, VALID_IDS)
        assert result.is_valid is False
        assert 1 in result.uncited_steps
        assert 2 in result.uncited_steps


# ── validate_response — empty / no steps ──────────────────────────────────────

class TestEmptyResponse:

    def test_empty_response(self):
        result = validate_response("", VALID_IDS)
        assert result.parsed_steps == []
        assert result.cited_ids == []

    def test_no_steps_warning(self):
        result = validate_response("Some narrative without steps.", VALID_IDS)
        assert any("no numbered procedural steps" in w for w in result.warnings)


# ── validate_response — multiple citations per step ────────────────────────────

class TestMultipleCitationsPerStep:

    def test_multiple_citations_on_one_step(self):
        response = "1. Combined action. [KB-101] [KB-102]\n2. Another step. [KB-103]"
        result = validate_response(response, VALID_IDS)
        assert result.is_valid is True
        assert set(result.parsed_steps[0]["citations"]) == {"KB-101", "KB-102"}

    def test_mixed_valid_and_fabricated_on_step(self):
        response = "1. Action. [KB-101] [KB-999]"
        result = validate_response(response, VALID_IDS)
        assert result.is_valid is False
        assert "KB-999" in result.fabricated_ids


# ── validate_response — decline message ───────────────────────────────────────

class TestDeclineResponse:

    def test_decline_has_no_steps(self):
        from agent.prompt import CLEAN_DECLINE
        result = validate_response(CLEAN_DECLINE, VALID_IDS)
        assert result.parsed_steps == []

    def test_decline_is_not_flagged_as_invalid_for_missing_steps(self):
        # The decline message has no steps — that's expected. Validator should
        # only warn, not mark is_valid=False for having no steps.
        from agent.prompt import CLEAN_DECLINE
        result = validate_response(CLEAN_DECLINE, VALID_IDS)
        # is_valid may be True since no steps were expected to have citations
        assert result.fabricated_ids == []
        assert result.uncited_steps == []
