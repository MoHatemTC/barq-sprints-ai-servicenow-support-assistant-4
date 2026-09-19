"""
agent/validators.py
───────────────────
BARQ G4 · S2.5 — Deterministic Post-Generation Citation Validation.

This module is the trust-but-verify layer between the raw LLM output and the
final response returned to the caller.  It performs no LLM calls — everything
here is deterministic regex/set logic.

Responsibilities
────────────────
1. Extract every [KB-XXX] citation that appears in the LLM response.
2. Compare them against the set of valid IDs from the retrieved chunks.
3. Detect fabricated (hallucinated) citation IDs.
4. Detect procedural steps that are missing citations entirely.
5. Parse individual numbered steps for structured output.
6. Detect malformed citation formats.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── Regex patterns ─────────────────────────────────────────────────────────────

# Matches [KB-101], [KB-1], [KB-9999] — valid citation format
_VALID_CITATION_RE = re.compile(r"\[KB-(\d+)\]", re.IGNORECASE)

# Matches anything that looks like it's trying to be a citation but is malformed
# e.g. [KB 101], [kb101], [KB101], [KB-], [KB-abc]
_MALFORMED_CITATION_RE = re.compile(
    r"\[(?:KB[\s\-]*\d*[a-zA-Z]*|[Kk][Bb][^\]]*)\]"
)

# Matches a numbered procedural step line: "1. ...", "2. ..." etc.
_STEP_LINE_RE = re.compile(r"^\s*(\d+)\.\s+(.+)", re.MULTILINE)


# ── Result dataclass ───────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    """Outcome of validating one LLM response."""

    is_valid: bool
    """True only if grounding is confirmed and no fabrications detected."""

    cited_ids: list[str] = field(default_factory=list)
    """All distinct valid KB IDs found in the response."""

    fabricated_ids: list[str] = field(default_factory=list)
    """IDs cited by the model that were NOT in the retrieved chunks."""

    uncited_steps: list[int] = field(default_factory=list)
    """Step numbers that have no citation."""

    malformed_citations: list[str] = field(default_factory=list)
    """Strings that look like broken citation attempts."""

    warnings: list[str] = field(default_factory=list)
    """Non-fatal diagnostic messages."""

    parsed_steps: list[dict] = field(default_factory=list)
    """
    Parsed step dicts: {"step": int, "instruction": str, "citations": list[str]}
    """


# ── Public API ─────────────────────────────────────────────────────────────────

def validate_response(
    response_text: str,
    valid_chunk_ids: set[str],
) -> ValidationResult:
    """
    Validate the LLM-generated response against the set of supplied KB chunk IDs.

    Parameters
    ----------
    response_text : str
        The raw string returned by the LLM.
    valid_chunk_ids : set[str]
        The exact set of KB IDs that were supplied as retrieved chunks,
        e.g. {'KB-101', 'KB-102', 'KB-103'}.

    Returns
    -------
    ValidationResult
        Detailed validation outcome. Callers should check ``is_valid`` and
        inspect ``fabricated_ids`` / ``uncited_steps`` for failures.
    """
    result = ValidationResult(is_valid=True)

    # ── 1. Extract all [KB-XXX] occurrences ──────────────────────────────────
    raw_matches: list[str] = _VALID_CITATION_RE.findall(response_text)
    # raw_matches is a list of digit strings ("101", "102", ...)
    found_ids = {f"KB-{n}" for n in raw_matches}

    # ── 2. Detect fabricated citations ───────────────────────────────────────
    fabricated = sorted(found_ids - valid_chunk_ids)
    if fabricated:
        result.fabricated_ids = fabricated
        result.is_valid = False
        logger.warning("Fabricated citation IDs detected: %s", fabricated)

    result.cited_ids = sorted(found_ids & valid_chunk_ids)

    # ── 3. Detect malformed citation attempts ─────────────────────────────────
    all_bracket_matches = _MALFORMED_CITATION_RE.findall(response_text)
    malformed = [
        m for m in all_bracket_matches
        if not _VALID_CITATION_RE.fullmatch(m)
    ]
    if malformed:
        result.malformed_citations = malformed
        result.warnings.append(f"Malformed citation format(s) detected: {malformed}")
        logger.warning("Malformed citations: %s", malformed)

    # ── 4. Parse numbered steps ───────────────────────────────────────────────
    step_matches = _STEP_LINE_RE.findall(response_text)
    for step_num_str, step_body in step_matches:
        step_num = int(step_num_str)
        step_citations_raw = _VALID_CITATION_RE.findall(step_body)
        step_citations = [f"KB-{n}" for n in step_citations_raw]

        result.parsed_steps.append({
            "step": step_num,
            "instruction": _strip_citations(step_body).strip(),
            "citations": step_citations,
        })

        if not step_citations:
            result.uncited_steps.append(step_num)
            result.is_valid = False
            logger.warning("Step %d has no citation.", step_num)

    # ── 5. Warn if no steps found at all but response is non-empty ────────────
    if not step_matches and response_text.strip():
        result.warnings.append(
            "Response contains no numbered procedural steps. "
            "Either this is a decline or the format is unexpected."
        )

    logger.debug(
        "Validation complete — cited: %s, fabricated: %s, uncited steps: %s",
        result.cited_ids,
        result.fabricated_ids,
        result.uncited_steps,
    )
    return result


def extract_cited_ids(response_text: str) -> set[str]:
    """
    Return the set of [KB-XXX] IDs mentioned anywhere in response_text.
    Does not cross-check validity — use validate_response() for that.
    """
    return {f"KB-{n}" for n in _VALID_CITATION_RE.findall(response_text)}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _strip_citations(text: str) -> str:
    """Remove all [KB-XXX] citation tags from a string."""
    return _VALID_CITATION_RE.sub("", text).strip()
