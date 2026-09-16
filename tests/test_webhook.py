"""
Basic smoke tests for the /webhook endpoint.
Run with: pytest
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi.testclient import TestClient
from barq_ai_support.main import app

client = TestClient(app)

VALID_SECRET = "barq-g4-secure-token"  # must match SERVICENOW_WEBHOOK_SECRET in your .env


def _payload():
    return {
        "incident_sys_id": "abc123",
        "number": "INC0010099",
        "short_description": "Test incident",
    }


def test_webhook_rejects_missing_secret():
    r = client.post("/webhook", json=_payload())
    assert r.status_code == 401


def test_webhook_rejects_wrong_secret():
    r = client.post(
        "/webhook", json=_payload(), headers={"X-ServiceNow-Secret": "wrong"}
    )
    assert r.status_code == 401


def test_webhook_accepts_correct_secret():
    r = client.post(
        "/webhook", json=_payload(), headers={"X-ServiceNow-Secret": VALID_SECRET}
    )
    assert r.status_code == 202
    assert r.json()["status"] == "accepted"
