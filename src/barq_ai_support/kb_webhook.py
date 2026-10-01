"""
S3.3 — KB event receiver.

POST /api/v1/events/kb

Same HMAC pattern as the incident receiver: verify raw-byte signature
before any parsing, ack fast with 202, do zero sync work inline. The
actual diff/sync logic runs inside a Celery task (process_kb_event).
"""
import hashlib
import hmac
import secrets as secrets_module

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ValidationError

from .config import settings

router = APIRouter()


class KBEvent(BaseModel):
    event_id: str
    sys_id: str
    operation: str  # insert | update | delete
    timestamp: str


def _verify_kb_signature(raw_body: bytes, provided_signature: str | None) -> bool:
    if not provided_signature:
        return False
    expected = hmac.new(
        key=settings.kb_signing_secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    try:
        return secrets_module.compare_digest(expected, provided_signature)
    except TypeError:
        return False

@router.post("/api/v1/events/kb", status_code=202)
async def receive_kb_event(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("X-Signature")

    if not _verify_kb_signature(raw_body, signature):
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        payload = KBEvent.model_validate_json(raw_body)
    except ValidationError:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    if payload.operation not in ("insert", "update", "delete"):
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    from .tasks import process_kb_event  # local import, same pattern as incidents
    process_kb_event.delay(payload.sys_id, payload.operation)

    print(f"Dispatched KB event_id={payload.event_id} sys_id={payload.sys_id} op={payload.operation}")
    return {"status": "accepted", "sys_id": payload.sys_id, "operation": payload.operation}