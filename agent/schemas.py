"""
agent/schemas.py
────────────────
BARQ G4 · S2.5 — Input / Output Data Schemas & Validation.

Uses Pydantic v2 for strict validation of every data structure that crosses
module boundaries.  Keeping validation here avoids scattering isinstance()
checks across executor.py and validators.py.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator
from pydantic import BaseModel, Field

# ── KB chunk ID format ─────────────────────────────────────────────────────────
# Accepted: KB-101, KB-999, KB-1, KB-1234  (case-insensitive input, stored as-is)
_KB_ID_RE = re.compile(r"^KB-\d+$", re.IGNORECASE)


class KnowledgeChunk(BaseModel):
    """A single retrieved knowledge base chunk supplied to the agent."""

    id: str
    """Chunk identifier, e.g. 'KB-101'. Must match KB-\\d+ pattern."""

    title: str
    """Human-readable article title."""

    text: str
    """Full chunk body text. Must not be blank."""

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        v = v.strip()
        if not _KB_ID_RE.match(v):
            raise ValueError(
                f"Invalid KB chunk id '{v}'. "
                "Expected format: KB-<digits> (e.g. KB-101)."
            )
        return v

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Knowledge chunk title must not be blank.")
        return v

    @field_validator("text")
    @classmethod
    def validate_text(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Knowledge chunk text must not be blank.")
        return v


# ── Agent input ────────────────────────────────────────────────────────────────

class AgentInput(BaseModel):
    """Validated input to run_agent()."""

    incident_text: str
    """Raw incident description. Treated as UNTRUSTED. Must not be blank."""

    retrieved_chunks: list[KnowledgeChunk]
    """Pre-retrieved knowledge chunks. May be empty (triggers decline)."""

    @field_validator("incident_text")
    @classmethod
    def validate_incident_text(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("incident_text must not be blank.")
        return v

    @model_validator(mode="after")
    def check_duplicate_ids(self) -> "AgentInput":
        seen: set[str] = set()
        duplicates: list[str] = []
        for chunk in self.retrieved_chunks:
            if chunk.id in seen:
                duplicates.append(chunk.id)
            seen.add(chunk.id)
        if duplicates:
            raise ValueError(
                f"Duplicate KB chunk IDs detected: {duplicates}. "
                "Each chunk ID must be unique."
            )
        return self


# ── Agent output ───────────────────────────────────────────────────────────────

class ResolutionStep(BaseModel):
    """A single step in a grounded resolution procedure."""

    step: int
    """1-based step number."""

    instruction: str
    """The procedural instruction for this step."""

    citations: list[str]
    """KB IDs cited for this step, e.g. ['KB-101']."""


class AgentOutput(BaseModel):
    """Structured output from the agent."""

    status: Literal["grounded", "declined", "error"]
    """
    'grounded'  — a validated resolution procedure was produced.
    'declined'  — no usable grounding context; clean refusal issued.
    'error'     — an unrecoverable error occurred (see error_message).
    """

    incident_summary: str = ""
    """Short description extracted from or derived from the incident text."""

    steps: list[ResolutionStep] = []
    """Parsed and validated procedural steps (populated when status='grounded')."""

    cited_ids: list[str] = []
    """Distinct KB IDs that appear in the generated steps."""

    raw_response: str = ""
    """Raw LLM response string, preserved for debugging."""

    decline_message: str = ""
    """The exact decline message (populated when status='declined')."""

    error_message: str = ""
    """Error description (populated when status='error')."""

    validation_warnings: list[str] = []
    """Non-fatal validation notes (e.g. steps without citations detected)."""

    def as_display_text(self) -> str:
        """Render the output as the human-readable string shown to support agents."""
        if self.status == "declined":
            return self.decline_message
        if self.status == "error":
            return f"[Agent Error] {self.error_message}"
        # grounded
        lines: list[str] = []
        if self.incident_summary:
            lines.append(
                f"Suggested Resolution Procedure for: {self.incident_summary}"
            )
            lines.append("-" * 60)
        for s in self.steps:
            cites = " ".join(f"[{c}]" for c in s.citations)
            lines.append(f"{s.step}. {s.instruction} {cites}".rstrip())
        lines.append("")
        lines.append(
            "Note: This procedure is a suggestion for support agent review only."
        )
        lines.append("No ticket actions have been taken.")
        return "\n".join(lines)
