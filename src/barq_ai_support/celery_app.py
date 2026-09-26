"""
S3.6 — Celery application wiring.

The Celery worker:
1. Receives the authenticated ServiceNow incident sys_id.
2. Marks the incident as in_progress through the ServiceNow Table API.
3. Runs the real S3.4 ReAct agent.
4. The agent performs the real incident fetch, KB retrieval, reasoning,
   and terminal ServiceNow writeback.
"""

from __future__ import annotations

import asyncio

from celery import Celery

from .agent.s3_worker import process_incident_event as run_agent
from .config import settings
from .servicenow_client import ServiceNowClient

celery_app = Celery(
    "barq_ai_support",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)


async def _run_incident(incident_payload: dict) -> dict:
    """
    Execute one incident through the real S3.4 agent.

    The first operation performed by the worker is the authenticated
    ServiceNow status claim, as required by the S3.6 integration flow.
    """

    sys_id = incident_payload.get("sys_id")

    if not sys_id:
        raise ValueError("Incident payload must contain sys_id")

    client = ServiceNowClient()

    await client.update_incident(
        sys_id,
        {
            f"{settings.ai_field_prefix}ai_status": "in_progress",
        },
    )

    return await run_agent(
        {"sys_id": sys_id},
        sn_client=client,
    )


@celery_app.task(name="barq_ai_support.process_incident_event")
def process_incident_event(incident_payload: dict) -> dict:
    """
    Celery entry point for a ServiceNow incident event.

    The webhook intentionally sends only the incident identifier.
    The agent then fetches the authoritative incident data directly
    from ServiceNow.
    """

    if not isinstance(incident_payload, dict):
        raise ValueError("Incident payload must be a dictionary")

    if not incident_payload.get("sys_id"):
        raise ValueError("Incident payload must contain sys_id")

    return asyncio.run(_run_incident(incident_payload))