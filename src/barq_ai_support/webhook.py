"""
S1.4 — ServiceNow Business Rule webhook receiver.

Moved here from the old root-level main.py so the webhook and the
knowledge-base endpoints live in ONE FastAPI app instead of two
disconnected ones.

Also fixes a real gap found during review: the Business Rule
(businessRule/business_rule.js) sends an "X-ServiceNow-Secret" header,
but the original webhook never checked it. Anyone who found the ngrok
URL could POST fake incidents. This version validates that header.
"""

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from pydantic import BaseModel

from .config import settings

router = APIRouter()


class IncidentEvent(BaseModel):
    incident_sys_id: str
    number: str
    short_description: str
    description: str | None = ""


def _verify_webhook_secret(x_servicenow_secret: str | None) -> None:
    """Reject the request if the shared secret header is missing or wrong."""
    if not settings.servicenow_webhook_secret:
        # No secret configured yet — fail closed rather than silently open.
        raise HTTPException(
            status_code=500,
            detail="SERVICENOW_WEBHOOK_SECRET is not configured on the server.",
        )
    if x_servicenow_secret != settings.servicenow_webhook_secret:
        raise HTTPException(status_code=401, detail="Invalid or missing webhook secret.")


@router.post("/webhook", status_code=202)
async def webhook(
    payload: IncidentEvent,
    background_tasks: BackgroundTasks,
    x_servicenow_secret: str | None = Header(default=None),
):
    _verify_webhook_secret(x_servicenow_secret)

    print(f"Received event for {payload.number} (sys_id: {payload.incident_sys_id})")
    background_tasks.add_task(handle_event, payload)
    return {"status": "accepted", "number": payload.number}


def handle_event(payload: IncidentEvent) -> None:
    # Sprint 1 scope ends here: just prove the event was received.
    # Retrieval, agent reasoning, and write-back are wired in later sprints
    # (see src/barq_ai_support/ingestion, retrieval, and agent packages).
    print(f"Background task ran for {payload.number}: {payload.short_description}")
