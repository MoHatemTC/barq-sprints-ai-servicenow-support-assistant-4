"""
agent/executor.py
─────────────────
BARQ G4 · S2.5 — LangChain Agent Executor & Callable Interface.

Architecture
────────────
The agent uses a ChatModel chain (SystemMessage + HumanMessage) rather than a
ReAct tool-loop, because the grounding requirement means the agent must reason
ONLY over the explicitly supplied knowledge chunks — not over tool outputs
retrieved at inference time.

Data flow
─────────
  run_agent(incident_text, retrieved_chunks)
      │
      ├─ 1. Input validation      (Pydantic schemas — deterministic)
      ├─ 2. Early-exit check      (empty / all-empty chunks → decline, no LLM call)
      ├─ 3. Chunk normalization   (strip whitespace, format for prompt)
      ├─ 4. Prompt assembly       (SystemMessage + HumanMessage)
      ├─ 5. LLM invocation        (ChatOpenAI — live endpoint)
      ├─ 6. Citation validation   (deterministic — validators.py)
      └─ 7. Structured output     (AgentOutput → display text)

The callable `run_agent(incident_text, retrieved_chunks)` is the public API
required by the S2.5 brief.

Secrets / configuration
───────────────────────
All credentials and model settings are loaded from environment variables.
A `.env` file (copied from `.env.example`) is the recommended way to supply
them locally. python-dotenv loads it into `os.environ`; secrets are never
printed or logged.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import AIMessage
from langchain_core.agents import AgentFinish
from langchain_core.runnables import RunnableLambda
from langchain_classic.agents import AgentExecutor

from agent.prompt import CLEAN_DECLINE, HUMAN_TURN_TEMPLATE, SYSTEM_PROMPT
from agent.schemas import AgentInput, AgentOutput, KnowledgeChunk, ResolutionStep
from agent.validators import validate_response

# ── Load environment variables from .env (no-op if already set) ───────────────
load_dotenv()

logger = logging.getLogger(__name__)


# ── Custom exceptions ──────────────────────────────────────────────────────────

class AgentConfigError(RuntimeError):
    """Raised when required environment variables are missing or invalid."""


class AgentLLMError(RuntimeError):
    """Raised when the LLM call fails (auth, quota, network, etc.)."""


# ── LLM factory ───────────────────────────────────────────────────────────────

def _build_llm() -> ChatOpenAI:
    """
    Construct and return a ChatOpenAI instance from environment variables.

    Required env vars
    -----------------
    OPENAI_API_KEY  — API key for the configured OpenAI-compatible endpoint
                      (Gemini keys are valid when LLM_BASE_URL points at Google)

    Optional env vars
    -----------------
    LLM_MODEL       — model string          (default: gemini-3.5-flash)
    LLM_TEMPERATURE — sampling temperature  (default: 0)
    LLM_BASE_URL    — OpenAI-compatible base URL (Gemini, Azure, proxy, …)

    Raises
    ------
    AgentConfigError  if OPENAI_API_KEY is not set.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise AgentConfigError(
            "OPENAI_API_KEY environment variable is not set. "
            "Copy .env.example to .env and add your key."
        )

    model = os.environ.get("LLM_MODEL", "gemini-3.5-flash").strip() or "gemini-3.5-flash"
    try:
        temperature = float(os.environ.get("LLM_TEMPERATURE", "0"))
    except ValueError:
        logger.warning("LLM_TEMPERATURE is not a valid float; defaulting to 0.")
        temperature = 0.0
    base_url: str | None = os.environ.get("LLM_BASE_URL", "").strip() or None

    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "api_key": api_key,
        # Avoid burning free-tier quota on automatic SDK retries.
        "max_retries": 0,
        "timeout": 90,
    }
    if base_url:
        kwargs["base_url"] = base_url

    logger.debug("Building LLM — model=%s temperature=%s", model, temperature)
    return ChatOpenAI(**kwargs)


def _message_text(content: Any) -> str:
    """Normalize ChatOpenAI / Gemini message content to a plain string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") in (None, "text") and block.get("text"):
                    parts.append(str(block["text"]))
                elif block.get("type") == "output_text" and block.get("text"):
                    parts.append(str(block["text"]))
            else:
                text = getattr(block, "text", None)
                if text:
                    parts.append(str(text))
        return "\n".join(p for p in parts if p)
    return str(content)


# ── Chunk normalization ────────────────────────────────────────────────────────

def _normalize_chunks(chunks: list[KnowledgeChunk]) -> tuple[str, set[str]]:
    """
    Serialize validated KnowledgeChunk objects into the KNOWLEDGE CONTEXT
    text block and return the set of valid chunk IDs.

    Returns
    -------
    (context_text, valid_ids)
        context_text  — formatted string injected into the prompt
        valid_ids     — set of KB IDs present in the chunks
    """
    sections: list[str] = []
    valid_ids: set[str] = set()

    for chunk in chunks:
        valid_ids.add(chunk.id)
        sections.append(f"[{chunk.id}] {chunk.title}\n{chunk.text}")

    context_text = "\n\n".join(sections)
    return context_text, valid_ids


# ── Decline helper ─────────────────────────────────────────────────────────────

def _make_decline() -> AgentOutput:
    """Return an AgentOutput with status='declined' and the canonical message."""
    return AgentOutput(
        status="declined",
        decline_message=CLEAN_DECLINE,
    )


# ── Output parser ──────────────────────────────────────────────────────────────

def _parse_steps(validation_result: Any) -> list[ResolutionStep]:
    """Convert parsed_steps dicts from ValidationResult into ResolutionStep objects."""
    steps: list[ResolutionStep] = []
    for s in validation_result.parsed_steps:
        steps.append(
            ResolutionStep(
                step=s["step"],
                instruction=s["instruction"],
                citations=s["citations"],
            )
        )
    return steps


def _extract_incident_summary(incident_text: str) -> str:
    """
    Pull a short summary from the incident text.
    Looks for a 'Short Description:' field; falls back to first non-blank line.
    """
    for line in incident_text.splitlines():
        line = line.strip()
        if line.lower().startswith("short description"):
            parts = line.split(":", 1)
            if len(parts) == 2 and parts[1].strip():
                return parts[1].strip()
    for line in incident_text.splitlines():
        line = line.strip()
        if line:
            return line[:120]
    return "Incident"



# ── Grounded LCEL agent factory ────────────────────────────────────────────────

def _build_grounded_agent(llm: ChatOpenAI) -> AgentExecutor:
    """
    Build a LangChain AgentExecutor wired to a live LLM endpoint.

    The agent uses a single-step LCEL chain (ChatPromptTemplate | LLM |
    AgentFinishParser) rather than a ReAct tool-loop.  No tool schema is
    sent to the LLM, so this is fully compatible with the Gemini
    OpenAI-compatible endpoint.

    The returned AgentExecutor accepts one input key ``human_content`` and
    returns ``{"output": <text>}`` after a single LLM pass.
    """

    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("human", "{human_content}"),
    ])

    def _parse_to_agent_finish(ai_message: AIMessage) -> AgentFinish:
        text = _message_text(ai_message.content)
        return AgentFinish(return_values={"output": text}, log=text)

    # LCEL chain: prompt → LLM → AgentFinish
    chain = prompt | llm | RunnableLambda(_parse_to_agent_finish)

    # AgentExecutor wraps the chain — no tools needed for grounded generation
    return AgentExecutor(agent=chain, tools=[], return_intermediate_steps=False)


# ── Public callable ────────────────────────────────────────────────────────────

def run_agent(
    incident_text: str,
    retrieved_chunks: list[dict],
    *,
    llm: ChatOpenAI | None = None,
) -> AgentOutput:
    """
    Run the BARQ AI Support Agent for a single incident.

    Parameters
    ----------
    incident_text : str
        Raw text describing the incident. Treated as UNTRUSTED.
        Must not be blank.

    retrieved_chunks : list[dict]
        Pre-retrieved knowledge base chunks. Each dict must have:
          - "id"    : str  — e.g. "KB-101"
          - "title" : str  — article title
          - "text"  : str  — chunk body (non-empty)
        Pass an empty list to trigger the deterministic clean-decline.

    llm : ChatOpenAI | None
        Optional pre-built LLM instance. If None, _build_llm() is called.
        Inject a fake/mock LLM in unit tests to avoid API calls.

    Returns
    -------
    AgentOutput
        Structured output. Call .as_display_text() for the human-readable form.

    Raises
    ------
    AgentConfigError   — missing / invalid environment configuration.
    AgentLLMError      — LLM API failure (auth, quota, network, etc.).
    pydantic.ValidationError — malformed input chunks.
    """

    # ── Step 1: Validate inputs ───────────────────────────────────────────────
    parsed_input = AgentInput(
        incident_text=incident_text,
        retrieved_chunks=[KnowledgeChunk(**c) for c in retrieved_chunks],
    )
    logger.info(
        "run_agent called — chunks=%d incident_len=%d",
        len(parsed_input.retrieved_chunks),
        len(parsed_input.incident_text),
    )

    # ── Step 2: Early-exit — no usable chunks ────────────────────────────────
    if not parsed_input.retrieved_chunks:
        logger.info("No retrieved chunks — returning deterministic decline.")
        return _make_decline()

    # ── Step 3: Normalize chunks ──────────────────────────────────────────────
    knowledge_context, valid_ids = _normalize_chunks(parsed_input.retrieved_chunks)
    logger.info("Valid KB IDs: %s", sorted(valid_ids))

    # ── Step 4: Assemble human turn content ───────────────────────────────────
    human_content = HUMAN_TURN_TEMPLATE.format(
        knowledge_context=knowledge_context,
        incident_text=parsed_input.incident_text,
    )

    # ── Step 5: Invoke LLM via AgentExecutor ──────────────────────────────────
    owns_llm = llm is None
    if owns_llm:
        llm = _build_llm()

    agent_executor = _build_grounded_agent(llm)

    raw_text = ""
    last_exc: Exception | None = None
    attempts = 3 if owns_llm else 1
    for attempt in range(attempts):
        try:
            logger.debug("Invoking LLM via AgentExecutor (attempt %d)…", attempt + 1)
            response = agent_executor.invoke({"human_content": human_content})
            raw_text = _message_text(response.get("output", ""))
            if not raw_text.strip():
                raise AgentLLMError("LLM returned empty content.")
            logger.debug("LLM responded — response_len=%d", len(raw_text))
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            lowered = str(exc).lower()
            auth_fail = (
                "api_key" in lowered
                or "authentication" in lowered
                or "401" in lowered
            )
            if auth_fail:
                break
            if attempt < attempts - 1:
                delay = 2 * (attempt + 1)
                logger.warning(
                    "LLM invoke failed (%s); retrying in %ss",
                    type(exc).__name__,
                    delay,
                )
                time.sleep(delay)

    if last_exc is not None:
        exc = last_exc
        # Categorise the error — never expose the API key
        exc_str = str(exc)
        lowered = exc_str.lower()
        if "api_key" in lowered or "authentication" in lowered or "401" in lowered:
            raise AgentLLMError(
                "LLM authentication failed. Check OPENAI_API_KEY."
            ) from exc
        if (
            "rate" in lowered
            or "quota" in lowered
            or "429" in lowered
            or "resource_exhausted" in lowered
        ):
            raise AgentLLMError(
                "LLM rate limit or quota exceeded. Please retry later."
            ) from exc
        raise AgentLLMError(f"LLM call failed: {type(exc).__name__}") from exc

    # ── Step 6: Check if the model self-declined ──────────────────────────────
    if CLEAN_DECLINE.strip() in raw_text.strip():
        logger.info("Model returned decline message.")
        return AgentOutput(
            status="declined",
            decline_message=CLEAN_DECLINE,
            raw_response=raw_text,
        )

    # ── Step 7: Citation validation ───────────────────────────────────────────
    val = validate_response(raw_text, valid_ids)
    logger.info(
        "Validation — valid=%s cited=%s fabricated=%s uncited_steps=%s",
        val.is_valid,
        val.cited_ids,
        val.fabricated_ids,
        val.uncited_steps,
    )

    incident_summary = _extract_incident_summary(parsed_input.incident_text)

    if not val.is_valid:
        # Build a detailed warning list but still return the raw response
        # so the caller can decide how to surface the issue.
        warnings: list[str] = []
        if val.fabricated_ids:
            warnings.append(
                f"Fabricated citation IDs detected and flagged: {val.fabricated_ids}"
            )
        if val.uncited_steps:
            warnings.append(
                f"Procedural steps missing citations: {val.uncited_steps}"
            )
        warnings.extend(val.warnings)

        return AgentOutput(
            status="grounded",
            incident_summary=incident_summary,
            steps=_parse_steps(val),
            cited_ids=val.cited_ids,
            raw_response=raw_text,
            validation_warnings=warnings,
        )

    # ── Step 8: Return clean structured output ────────────────────────────────
    return AgentOutput(
        status="grounded",
        incident_summary=incident_summary,
        steps=_parse_steps(val),
        cited_ids=val.cited_ids,
        raw_response=raw_text,
        validation_warnings=val.warnings,
    )
