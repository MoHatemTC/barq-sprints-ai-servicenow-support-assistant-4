"""
S3.3 — Incident receiver with HMAC-SHA256 verification, Redis dedup,
and Celery dispatch.

Order of operations (do not reorder):
  1. Read raw body bytes.
  2. Verify HMAC-SHA256 signature over those raw bytes, constant-time.
     Bad/missing signature -> 401, before any JSON parsing.
  3. Parse + validate JSON (Pydantic).
  4. Dedup check (Redis SETNX-equivalent). Replay -> 202, no Celery task.
  5. First-seen -> enqueue Celery task, return 202.
     If the enqueue FAILS, release the dedup claim from step 4 and return
     503, so the sender's retry of the same event_id is processed instead of
     being mistaken for a duplicate (and silently dropped) for the whole TTL.

The endpoint never runs agent/retrieval/embedding work itself, and never
calls ServiceNow directly — claiming the incident happens inside the
Celery task, not here.
"""
import hashlib
import hmac
import logging
import secrets as secrets_module

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ValidationError
import redis

from .config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

_redis_client: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


class IncidentEvent(BaseModel):
    incident_sys_id: str
    number: str
    short_description: str
    description: str | None = ""
    event_id: str  # required for dedup — must be unique per event


def _verify_signature(raw_body: bytes, provided_signature: str | None) -> bool:
    """Constant-time HMAC-SHA256 verification over raw request bytes."""
    if not provided_signature:
        return False
    if not settings.incident_signing_secret:  
        return False  
    expected = hmac.new(
        key=settings.incident_signing_secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    try:
        return secrets_module.compare_digest(expected, provided_signature)
    except TypeError:
        return False


def _dedup_key(event_id: str) -> str:
    return f"{settings.dedup_key_prefix}{event_id}"


def _claim_event_once(event_id: str) -> bool:
    """
    Atomically claim an event_id in Redis. Returns True if this is the
    first time we've seen it (should process), False if it's a replay.
    """
    client = _get_redis()
    was_set = client.set(_dedup_key(event_id), "1", nx=True, ex=settings.dedup_ttl_seconds)
    return bool(was_set)


def _release_event_claim(event_id: str) -> None:
    """
    Undo _claim_event_once, so a redelivery of the same event_id is treated
    as first-seen. Best effort: if Redis is itself what's failing, the claim
    simply expires at its TTL — logged loudly rather than raised, so it can
    never mask the original dispatch error.
    """
    key = _dedup_key(event_id)
    try:
        _get_redis().delete(key)
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not release dedup claim %s (expires at TTL): %s", key, exc)


@router.post("/api/v1/events/servicenow", status_code=202)
async def receive_incident_event(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("X-Signature")

    if not _verify_signature(raw_body, signature):
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        payload = IncidentEvent.model_validate_json(raw_body)
    except ValidationError:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    is_first_seen = _claim_event_once(payload.event_id)

    if not is_first_seen:
        print(f"Duplicate event_id={payload.event_id}, ack without dispatch")
        return Response(status_code=status.HTTP_202_ACCEPTED)

    try:
        # local import avoids circular import at module load
        from .tasks import process_incident

        process_incident.delay(payload.model_dump())
    except Exception:  # noqa: BLE001
        # Nothing reached a worker. Give the event back so the sender's
        # retry isn't swallowed as a "duplicate".
        _release_event_claim(payload.event_id)
        logger.error(
            "Dispatch failed for event_id=%s sys_id=%s; dedup claim released",
            payload.event_id,
            payload.incident_sys_id,
            exc_info=True,
        )
        return Response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": "30"},
        )

    print(f"Dispatched event_id={payload.event_id} sys_id={payload.incident_sys_id} to Celery")
    return {"status": "accepted", "number": payload.number}
