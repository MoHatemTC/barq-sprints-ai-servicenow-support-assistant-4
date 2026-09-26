"""S3.6 — authenticated ServiceNow webhook receiver."""

from __future__ import annotations

import hashlib
import hmac
import json

import redis.asyncio as redis
from fastapi import APIRouter, Header, HTTPException, Request

from .celery_app import process_incident_event
from .config import settings

router = APIRouter()


def _verify_signature(
    raw_body: bytes,
    signature: str | None,
) -> None:
    """Verify HMAC-SHA256 against the exact raw request body."""

    if not settings.servicenow_webhook_secret:
        raise HTTPException(
            status_code=500,
            detail="Webhook secret is not configured",
        )

    if not signature:
        raise HTTPException(
            status_code=401,
            detail="Missing X-Signature",
        )

    supplied = signature.removeprefix("sha256=")

    expected = hmac.new(
        settings.servicenow_webhook_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid signature",
        )


@router.post("/webhook", status_code=202)
async def webhook(
    request: Request,
    x_signature: str | None = Header(default=None),
):
    """Receive, authenticate, deduplicate, and enqueue a ServiceNow event."""

    raw_body = await request.body()

    _verify_signature(raw_body, x_signature)

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail="Webhook body must be a JSON object",
        )

    sys_id = payload.get("sys_id") or payload.get("incident_sys_id")

    if not sys_id:
        raise HTTPException(
            status_code=400,
            detail="Missing sys_id",
        )

    redis_client = redis.from_url(
        settings.celery_broker_url,
        decode_responses=True,
    )

    try:
        dedup_key = f"barq:s3.6:webhook:{sys_id}"

        claimed = await redis_client.set(
            dedup_key,
            "1",
            nx=True,
            ex=24 * 60 * 60,
        )

        if not claimed:
            return {
                "status": "duplicate",
                "sys_id": sys_id,
            }

        process_incident_event.delay(
            {
                "sys_id": sys_id,
                "number": payload.get("number", ""),
            }
        )

        return {
            "status": "accepted",
            "sys_id": sys_id,
        }

    finally:
        await redis_client.aclose()