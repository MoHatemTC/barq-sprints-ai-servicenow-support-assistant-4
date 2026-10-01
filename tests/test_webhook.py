"""
Smoke tests for the /api/v1/events/servicenow endpoint (S3.3 contract:
HMAC-SHA256 over the raw request body via X-Signature, event_id-based
payload for dedup, Redis SETNX dedup, Celery dispatch to process_incident).

Run with: pytest

Redis is faked (see _FakeRedis / the autouse fixture below) so these tests
never need a live Redis instance or a real Celery broker connection --
they test the webhook's own logic, not infrastructure availability.
"""
import sys
import os
import hashlib
import hmac
import json
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi.testclient import TestClient
from barq_ai_support.main import app
from barq_ai_support import webhook as webhook_module
from barq_ai_support.tasks import process_incident
from barq_ai_support.config import settings

client = TestClient(app)

TEST_SECRET = "test-only-secret-not-the-real-one"


def _payload(sys_id="abc123", event_id=None):
    return {
        "incident_sys_id": sys_id,
        "number": "INC0010099",
        "short_description": "Test incident",
        "description": "",
        "event_id": event_id or str(uuid.uuid4()),
    }


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class _FakeRedis:
    """Minimal in-memory stand-in for redis's SETNX-with-expiry behavior,
    so tests don't require a live Redis instance."""

    _store: set[str] = set()

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self._store:
            return None  # key already exists -> SETNX fails, matches real Redis
        self._store.add(key)
        return True


@pytest.fixture(autouse=True)
def _fake_infra(monkeypatch):
    """Every test gets a fresh fake Redis and a no-op Celery enqueue, so
    tests never depend on Docker/Redis/a running worker being up."""
    _FakeRedis._store.clear()
    monkeypatch.setattr(webhook_module, "_redis_client", None)
    monkeypatch.setattr(webhook_module.redis, "from_url", lambda *a, **k: _FakeRedis())
    monkeypatch.setattr(process_incident, "delay", lambda *a, **k: None)
    monkeypatch.setattr(settings, "incident_signing_secret", TEST_SECRET)


def test_webhook_rejects_missing_signature():
    r = client.post("/api/v1/events/servicenow", json=_payload())
    assert r.status_code == 401


def test_webhook_rejects_wrong_signature():
    body = json.dumps(_payload()).encode("utf-8")
    r = client.post(
        "/api/v1/events/servicenow",
        content=body,
        headers={"Content-Type": "application/json", "X-Signature": "0" * 64},
    )
    assert r.status_code == 401


def test_webhook_accepts_correct_signature():
    body = json.dumps(_payload(), separators=(",", ":")).encode("utf-8")
    signature = _sign(body, TEST_SECRET)
    r = client.post(
        "/api/v1/events/servicenow",
        content=body,
        headers={"Content-Type": "application/json", "X-Signature": signature},
    )
    assert r.status_code == 202
    assert r.json()["status"] == "accepted"


def test_webhook_dedup_returns_202_on_replay():
    """Same event_id sent twice: both calls are acked with 202. The real
    dedup guarantee (no second dispatch) is checked separately below,
    since this contract has no "duplicate" marker in the response body."""
    event_id = "dup-001"
    body = json.dumps(_payload(sys_id="abc123", event_id=event_id), separators=(",", ":")).encode("utf-8")
    signature = _sign(body, TEST_SECRET)
    headers = {"Content-Type": "application/json", "X-Signature": signature}

    first = client.post("/api/v1/events/servicenow", content=body, headers=headers)
    second = client.post("/api/v1/events/servicenow", content=body, headers=headers)

    assert first.status_code == 202
    assert first.json()["status"] == "accepted"
    assert second.status_code == 202


def test_webhook_dedup_blocks_second_dispatch(monkeypatch):
    """Confirms the dedup gate actually blocks a second Celery dispatch,
    not just that the endpoint returns 202 twice."""
    calls = []
    monkeypatch.setattr(process_incident, "delay", lambda payload: calls.append(payload))

    event_id = "dup-002"
    body = json.dumps(_payload(sys_id="abc456", event_id=event_id), separators=(",", ":")).encode("utf-8")
    signature = _sign(body, TEST_SECRET)
    headers = {"Content-Type": "application/json", "X-Signature": signature}

    client.post("/api/v1/events/servicenow", content=body, headers=headers)
    client.post("/api/v1/events/servicenow", content=body, headers=headers)

    assert len(calls) == 1


def test_webhook_rejects_missing_required_field():
    """Missing a required field (event_id) fails Pydantic validation -> 400."""
    body = json.dumps(
        {"incident_sys_id": "abc123", "number": "INC0010099", "short_description": "Test"},
        separators=(",", ":"),
    ).encode("utf-8")
    signature = _sign(body, TEST_SECRET)
    r = client.post(
        "/api/v1/events/servicenow",
        content=body,
        headers={"Content-Type": "application/json", "X-Signature": signature},
    )
    assert r.status_code == 400

def test_webhook_rejects_when_no_secret_configured(monkeypatch):
    """With no signing secret configured, fail closed: a request signed
    with an empty key must NOT be accepted."""
    monkeypatch.setattr(settings, "incident_signing_secret", "")
    body = json.dumps(_payload(), separators=(",", ":")).encode("utf-8")
    signature = _sign(body, "")
    r = client.post(
        "/api/v1/events/servicenow",
        content=body,
        headers={"Content-Type": "application/json", "X-Signature": signature},
    )
    assert r.status_code == 401