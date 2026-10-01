"""
Tests for the receiver's behavior when Celery dispatch fails after the
dedup claim has already been taken.

The bug these guard against: the event_id is claimed in Redis BEFORE the
task is enqueued. If the enqueue fails and the claim is left in place, the
sender's retry of the same event_id looks like a duplicate and is dropped
for the full dedup TTL — the event is lost even though no worker ever saw it.

Redis is faked and the Celery enqueue is patched, so no infrastructure is
needed.
"""
import hashlib
import hmac
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi.testclient import TestClient

from barq_ai_support import tasks as tasks_module
from barq_ai_support import webhook as webhook_module
from barq_ai_support.config import settings
from barq_ai_support.main import app

client = TestClient(app)

SECRET = "test-only-signing-secret"
URL = "/api/v1/events/servicenow"


class _FakeRedis:
    """In-memory stand-in for the two Redis calls the receiver makes."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.fail_delete = False

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def delete(self, key):
        if self.fail_delete:
            raise ConnectionError("redis unavailable")
        self.store.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(webhook_module, "_get_redis", lambda: fake)
    monkeypatch.setattr(settings, "incident_signing_secret", SECRET)
    return fake


def _post(event_id: str):
    body = json.dumps(
        {
            "event_id": event_id,
            "incident_sys_id": "abc123",
            "number": "INC0000001",
            "short_description": "test",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return client.post(
        URL,
        content=body,
        headers={"Content-Type": "application/json", "X-Signature": signature},
    )


def _dispatch_recorder(monkeypatch):
    calls = []
    monkeypatch.setattr(tasks_module.process_incident, "delay", lambda payload: calls.append(payload))
    return calls


def _dispatch_failure(monkeypatch):
    def boom(payload):
        raise ConnectionError("broker unavailable")

    monkeypatch.setattr(tasks_module.process_incident, "delay", boom)


def test_failed_dispatch_returns_503_and_releases_claim(fake_redis, monkeypatch):
    _dispatch_failure(monkeypatch)

    response = _post("evt-1")

    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "30"
    assert fake_redis.store == {}, "dedup claim must be released when dispatch fails"


def test_retry_after_failed_dispatch_is_processed_not_dropped(fake_redis, monkeypatch):
    """The actual event-loss scenario: dispatch fails, sender retries the
    same event_id, and that retry must reach a worker."""
    _dispatch_failure(monkeypatch)
    assert _post("evt-2").status_code == 503

    calls = _dispatch_recorder(monkeypatch)
    retry = _post("evt-2")

    assert retry.status_code == 202
    assert retry.json()["status"] == "accepted"
    assert len(calls) == 1, "the retried event must actually be dispatched"


def test_successful_dispatch_keeps_claim_so_replays_are_still_blocked(fake_redis, monkeypatch):
    calls = _dispatch_recorder(monkeypatch)

    assert _post("evt-3").status_code == 202
    assert _post("evt-3").status_code == 202  # replay: acked...

    assert len(calls) == 1, "...but never dispatched a second time"
    assert len(fake_redis.store) == 1


def test_failure_to_release_claim_does_not_mask_the_dispatch_error(fake_redis, monkeypatch):
    """If Redis is the thing that's down, releasing the claim fails too.
    The receiver must still answer 503 cleanly, not crash with a 500."""
    _dispatch_failure(monkeypatch)
    fake_redis.fail_delete = True

    response = _post("evt-4")

    assert response.status_code == 503
