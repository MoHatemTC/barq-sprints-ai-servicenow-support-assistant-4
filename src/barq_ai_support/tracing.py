"""S3.6 — Distributed tracing via Langfuse."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Iterator

from langfuse import Langfuse

from .config import settings

_langfuse: Langfuse | None = None


def tracing_enabled() -> bool:
    """Return True when all required Langfuse configuration exists."""

    return bool(
        settings.langfuse_public_key
        and settings.langfuse_secret_key
        and settings.langfuse_host
    )


def get_langfuse_client() -> Langfuse:
    """Return the shared Langfuse client."""

    global _langfuse

    if _langfuse is None:
        _langfuse = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )

    return _langfuse


@contextmanager
def incident_trace(
    payload: dict[str, Any],
) -> Iterator[Any]:
    """
    Create the parent incident trace.

    All child observations created while this context is active become
    nested under the incident-run trace.
    """

    if not tracing_enabled():
        yield None
        return

    lf = get_langfuse_client()

    name = (
        payload.get("number")
        or payload.get("sys_id")
        or "UNKNOWN"
    )

    with lf.start_as_current_observation(
        name=f"incident-run-{name}",
        as_type="chain",
        input=payload,
    ) as trace:
        try:
            yield trace
        finally:
            lf.flush()


@contextmanager
def observation(
    name: str,
    as_type: str,
    input_data: Any,
) -> Iterator[Any]:
    """Create a nested Langfuse observation."""

    if not tracing_enabled():
        yield None
        return

    lf = get_langfuse_client()

    with lf.start_as_current_observation(
        name=name,
        as_type=as_type,
        input=input_data,
    ) as span:
        yield span


def traced_incident_run(
    incident: dict[str, Any],
    query: str,
    category: str | None,
    retrieve_fn: Callable[..., Any],
    decide_fn: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    """
    Backwards-compatible tracing helper used by the benchmark/demo.

    The production agent uses incident_trace() and observation() directly.
    """

    with incident_trace(incident) as trace:

        with observation(
            "incident-fetch",
            "tool",
            {"number": incident.get("number")},
        ) as span:
            if span is not None:
                span.update(output=incident)

        with observation(
            "kb-retrieval",
            "retriever",
            {
                "query": query,
                "category": category,
            },
        ) as span:

            result = retrieve_fn(
                query=query,
                category=category,
            )

            if span is not None:
                span.update(
                    output={
                        "retrieved_articles": [
                            chunk.article_number
                            for chunk in result.chunks
                        ],
                        "best_score": result.best_score,
                    }
                )

        with observation(
            "agent-decision",
            "generation",
            {
                "retrieved_articles": [
                    chunk.article_number
                    for chunk in result.chunks
                ],
            },
        ) as span:

            decision = decide_fn(result)

            if span is not None:
                span.update(output=decision)

        with observation(
            "servicenow-writeback",
            "tool",
            {
                "incident_number": incident.get("number"),
                "fields": decision,
            },
        ) as span:

            if span is not None:
                span.update(output=decision)

        if trace is not None:
            trace.update(output=decision)

    return decision