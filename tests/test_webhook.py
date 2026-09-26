"""
Smoke tests for the /webhook endpoint (S3.6 contract: HMAC-SHA256 over the
raw request body via X-Signature, sys_id-based payload, Redis SETNX dedup).

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

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi.testclient import TestClient
from barq_ai_support.main import app
from barq_ai_support import webhook as webhook_module
from barq_ai_support.config import settings

client = TestClient(app)

TEST_SECRET = "test-only-secret-not-the-real-one"


def _payload(sys_id="abc123"):
    return {"sys_id": sys_id, "number": "INC0010099", "short_description": "Test incident"}


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class _FakeRedis:
    """Minimal in-memory stand-in for redis.asyncio's SETNX-with-expiry
    behavior, so tests don't require a live Redis instance."""

    _store: set[str] = set()

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self._store:
            return None  # key already exists -> SETNX fails, matches real Redis
        self._store.add(key)
        return True

    async def aclose(self):
        pass


@pytest.fixture(autouse=True)
def _fake_infra(monkeypatch):
    """Every test gets a fresh fake Redis and a no-op Celery enqueue, so
    tests never depend on Docker/Redis/a running worker being up."""
    _FakeRedis._store.clear()
    monkeypatch.setattr(webhook_module.redis, "from_url", lambda *a, **k: _FakeRedis())
    monkeypatch.setattr(webhook_module.process_incident_event, "delay", lambda *a, **k: None)


def test_webhook_rejects_missing_signature(monkeypatch):
    monkeypatch.setattr(settings, "servicenow_webhook_secret", TEST_SECRET)
    r = client.post("/webhook", json=_payload())
    assert r.status_code == 401


def test_webhook_rejects_wrong_signature(monkeypatch):
    monkeypatch.setattr(settings, "servicenow_webhook_secret", TEST_SECRET)
    body = json.dumps(_payload()).encode("utf-8")
    r = client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Signature": "0" * 64},
    )
    assert r.status_code == 401


def test_webhook_accepts_correct_signature(monkeypatch):
    monkeypatch.setattr(settings, "servicenow_webhook_secret", TEST_SECRET)
    body = json.dumps(_payload(), separators=(",", ":")).encode("utf-8")
    signature = _sign(body, TEST_SECRET)
    r = client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Signature": signature},
    )
    assert r.status_code == 202
    assert r.json()["status"] == "accepted"


def test_webhook_dedup_blocks_replay(monkeypatch):
    """Same event id (sys_id) sent twice: second call must be marked
    duplicate and must NOT enqueue a second agent run."""
    monkeypatch.setattr(settings, "servicenow_webhook_secret", TEST_SECRET)
    body = json.dumps(_payload(sys_id="dup-001"), separators=(",", ":")).encode("utf-8")
    signature = _sign(body, TEST_SECRET)
    headers = {"Content-Type": "application/json", "X-Signature": signature}

    first = client.post("/webhook", content=body, headers=headers)
    second = client.post("/webhook", content=body, headers=headers)

    assert first.status_code == 202
    assert first.json()["status"] == "accepted"
    assert second.status_code == 202
    assert second.json()["status"] == "duplicate"


def test_webhook_rejects_when_no_secret_configured(monkeypatch):
    """If the server has no secret configured at all, fail closed (500),
    never fail open by accepting any request."""
    monkeypatch.setattr(settings, "servicenow_webhook_secret", "")
    r = client.post("/webhook", json=_payload(), headers={"X-Signature": "anything"})
    assert r.status_code == 500


def test_webhook_rejects_missing_sys_id(monkeypatch):
    monkeypatch.setattr(settings, "servicenow_webhook_secret", TEST_SECRET)
    body = json.dumps({"number": "INC0010099"}, separators=(",", ":")).encode("utf-8")
    signature = _sign(body, TEST_SECRET)
    r = client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Signature": signature},
    )
    assert r.status_code == 400
