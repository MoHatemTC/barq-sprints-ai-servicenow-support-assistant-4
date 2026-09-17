"""
Basic smoke tests for the /webhook endpoint.
Run with: pytest

Note on the secret: tests do NOT hardcode or read the real production
secret (the one in businessRule/business_rule.js / your .env). Instead,
each test uses pytest's `monkeypatch` fixture to set a test-only secret
on the settings object for the duration of that test, then FastAPI's
dependency_overrides is not needed since we patch the settings instance
directly. This means:
  - the tests never duplicate the real secret anywhere in committed code
  - the tests pass the same way regardless of what's in any developer's
    real .env file
  - we are testing the *comparison logic*, not "does this literal happen
    to match today's .env value"
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi.testclient import TestClient
from barq_ai_support.main import app
from barq_ai_support.config import settings

client = TestClient(app)

TEST_SECRET = "test-only-secret-not-the-real-one"


def _payload():
    return {
        "incident_sys_id": "abc123",
        "number": "INC0010099",
        "short_description": "Test incident",
    }


def test_webhook_rejects_missing_secret(monkeypatch):
    monkeypatch.setattr(settings, "servicenow_webhook_secret", TEST_SECRET)
    r = client.post("/webhook", json=_payload())
    assert r.status_code == 401


def test_webhook_rejects_wrong_secret(monkeypatch):
    monkeypatch.setattr(settings, "servicenow_webhook_secret", TEST_SECRET)
    r = client.post(
        "/webhook", json=_payload(), headers={"X-ServiceNow-Secret": "wrong-value"}
    )
    assert r.status_code == 401


def test_webhook_accepts_correct_secret(monkeypatch):
    monkeypatch.setattr(settings, "servicenow_webhook_secret", TEST_SECRET)
    r = client.post(
        "/webhook", json=_payload(), headers={"X-ServiceNow-Secret": TEST_SECRET}
    )
    assert r.status_code == 202
    assert r.json()["status"] == "accepted"


def test_webhook_rejects_when_no_secret_configured(monkeypatch):
    """If the server has no secret configured at all, fail closed (500),
    never fail open by accepting any request."""
    monkeypatch.setattr(settings, "servicenow_webhook_secret", "")
    r = client.post(
        "/webhook", json=_payload(), headers={"X-ServiceNow-Secret": "anything"}
    )
    assert r.status_code == 500
