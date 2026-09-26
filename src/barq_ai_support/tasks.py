"""
S3.3 — Celery tasks. All ServiceNow API calls happen here, never on the
FastAPI request thread.

servicenow_client's methods are async (httpx.AsyncClient), but Celery tasks
run synchronously by default, so we bridge with asyncio.run().
"""
import asyncio

from .celery_app import celery_app
from .servicenow_client import ServiceNowClient

from .kb_sync_service import sync_article

def run_agent(incident_payload: dict) -> None:
    """
    Stub for the actual agent entry point. Replace with the real call into
    the agent pipeline (src/barq_ai_support/agent) once ready.
    """
    print(f"[agent-stub] would now run agent for {incident_payload.get('number')}")


@celery_app.task(name="process_incident", bind=True, max_retries=3, default_retry_delay=10)
def process_incident(self, incident_payload: dict) -> dict:
    """
    Entry point for a first-seen incident event.
      1. Claim the incident in ServiceNow (PATCH ai_status=in_progress).
      2. Hand off to the agent (stub for now).
    """
    sys_id = incident_payload.get("incident_sys_id")
    if not sys_id:
        print(f"process_incident called without incident_sys_id: {incident_payload}")
        return {"status": "error", "reason": "missing incident_sys_id"}

    client = ServiceNowClient()

    try:
        claim_result = asyncio.run(client.claim_incident(sys_id, status_value="in_progress"))
    except Exception as exc:  # noqa: BLE001 - retry on any transient failure
        print(f"Failed to claim incident {sys_id}, retrying: {exc}")
        raise self.retry(exc=exc)

    print(f"Claimed incident {sys_id}: {claim_result.get('ai_status', 'unknown')}")

    run_agent(incident_payload)

    return {"status": "claimed_and_dispatched", "sys_id": sys_id}

@celery_app.task(name="process_kb_event", bind=True, max_retries=3, default_retry_delay=10)
def process_kb_event(self, sys_id: str, operation: str) -> dict:
    """
    Entry point for a KB change event (insert/update/delete on kb_knowledge).
    Runs the diff-and-sync logic against Qdrant, never on the request thread.
    """
    try:
        result = sync_article(sys_id, operation)
    except Exception as exc:  # noqa: BLE001 - retry on any transient failure
        print(f"KB sync failed for sys_id={sys_id}, retrying: {exc}")
        raise self.retry(exc=exc)

    print(f"KB sync complete for sys_id={sys_id}: {result}")
    return result