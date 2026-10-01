"""
S3.3/S3.6 — Celery tasks. All ServiceNow API calls happen here, never on the
FastAPI request thread.

servicenow_client's methods are async (httpx.AsyncClient), but Celery tasks
run synchronously by default, so we bridge with asyncio.run().

Failure handling (per the S3.4 brief):
  - transient failures (timeouts, connection errors, 429, 5xx) are retried
    ONCE, then fail cleanly;
  - permanent failures (401/403/404, malformed payloads, config errors) skip
    retries entirely and fail cleanly;
  - "fail cleanly" = log it, best-effort work note on the incident, return an
    error result instead of raising, so the worker survives and keeps
    consuming later jobs.

Scope note: the real S3.4 agent already contains its own failures (it never
raises), so this wrapper's retry logic only ever fires on the claim step that
runs before the agent.
"""
import asyncio
import logging

from .agent.s3_worker import process_incident_event as run_real_agent
from .celery_app import celery_app
from .kb_sync_service import sync_article
from .retry_policy import is_transient_error
from .servicenow_client import ServiceNowClient

logger = logging.getLogger(__name__)

# One retry, per the brief ("retried once before failing").
MAX_RETRIES = 1
RETRY_DELAY_SECONDS = 10


async def _claim_and_run_agent(sys_id: str) -> dict:
    """
    Claim the incident (S3.3), then hand it to the real S3.4 ReAct agent.
    Both run under one event loop so the LLM/HTTP clients created inside
    the agent stay valid for their full lifetime.
    """
    client = ServiceNowClient()

    await client.claim_incident(sys_id, status_value="in_progress")

    return await run_real_agent(
        {"sys_id": sys_id},
        sn_client=client,
    )


async def _write_failure_note(sys_id: str, reason: str) -> None:
    client = ServiceNowClient()
    await client.add_work_note(
        sys_id, f"AI processing failed and was not completed: {reason}"
    )


def _record_failure(sys_id: str, exc: BaseException) -> None:
    """Best-effort work note so a human can see the run stalled. Never raises:
    if ServiceNow is the thing that's down, this write may fail too."""
    try:
        asyncio.run(_write_failure_note(sys_id, str(exc)))
    except Exception as note_exc:  # noqa: BLE001
        logger.error(
            "Also failed to write failure work note for %s: %s", sys_id, note_exc
        )


@celery_app.task(
    name="process_incident",
    bind=True,
    max_retries=MAX_RETRIES,
    default_retry_delay=RETRY_DELAY_SECONDS,
)
def process_incident(self, incident_payload: dict) -> dict:
    """
    Entry point for a first-seen incident event.
      1. Claim the incident in ServiceNow (PATCH ai_status=in_progress).
      2. Run the real S3.4 agent (searchKB / addWorkNote / suggestAnswer / requestHR).
    """
    sys_id = incident_payload.get("incident_sys_id")
    if not sys_id:
        # Malformed payload: permanent, never worth retrying.
        logger.error("process_incident called without incident_sys_id: %s", incident_payload)
        return {"status": "error", "reason": "missing incident_sys_id"}

    try:
        result = asyncio.run(_claim_and_run_agent(sys_id))
    except Exception as exc:  # noqa: BLE001
        if is_transient_error(exc) and self.request.retries < self.max_retries:
            logger.warning(
                "Transient failure for %s (retry %d/%d): %s",
                sys_id, self.request.retries + 1, self.max_retries, exc,
            )
            raise self.retry(exc=exc)

        # Permanent failure, or a transient one that already used its retry.
        logger.error(
            "process_incident failed for %s and will not be retried: %s",
            sys_id, exc, exc_info=True,
        )
        _record_failure(sys_id, exc)
        return {"status": "error", "error": str(exc)}

    logger.info("Processed incident %s: %s", sys_id, result.get("status", "unknown"))
    return result


@celery_app.task(
    name="process_kb_event",
    bind=True,
    max_retries=MAX_RETRIES,
    default_retry_delay=RETRY_DELAY_SECONDS,
)
def process_kb_event(self, sys_id: str, operation: str) -> dict:
    """
    Entry point for a KB change event (insert/update/delete on kb_knowledge).
    Runs the diff-and-sync logic against Qdrant, never on the request thread.
    Safe to retry: the sync is idempotent (content-hash check).
    """
    try:
        result = sync_article(sys_id, operation)
    except Exception as exc:  # noqa: BLE001
        if is_transient_error(exc) and self.request.retries < self.max_retries:
            logger.warning(
                "Transient KB sync failure for sys_id=%s (retry %d/%d): %s",
                sys_id, self.request.retries + 1, self.max_retries, exc,
            )
            raise self.retry(exc=exc)

        logger.error(
            "KB sync failed for sys_id=%s and will not be retried: %s",
            sys_id, exc, exc_info=True,
        )
        return {"status": "error", "sys_id": sys_id, "error": str(exc)}

    logger.info("KB sync complete for sys_id=%s: %s", sys_id, result)
    return result
