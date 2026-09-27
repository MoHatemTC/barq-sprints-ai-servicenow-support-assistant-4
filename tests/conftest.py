"""
tests/conftest.py
─────────────────
BARQ G4 · S2.5 — Shared pytest fixtures and mock helpers.

All test fixtures that require LLM responses use a FakeChatModel so that
the test suite runs WITHOUT any paid OpenAI API credits.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from agent.prompt import CLEAN_DECLINE

from langchain_core.language_models import FakeListChatModel

class FakeLLM(FakeListChatModel):
    """
    Drop-in replacement for ChatOpenAI used in unit tests.
    """
    def __init__(self, response_text: str) -> None:
        super().__init__(responses=[response_text])

    def bind_tools(self, tools: Any, **kwargs: Any) -> FakeLLM:
        return self


class FailingFakeLLM(FakeListChatModel):
    """
    A FakeLLM that always raises an exception when invoked.

    Used to test that AgentLLMError is raised and that raw exception messages
    containing secrets are not leaked through to the caller.

    Parameters
    ----------
    exc : Exception
        The exception to raise on every call.
    """

    def __init__(self, exc: Exception) -> None:
        # FakeListChatModel needs at least one response entry; it won't be used.
        super().__init__(responses=["<never returned>"])
        self._exc = exc

    def _generate(self, messages: Any, **kwargs: Any) -> Any:
        raise self._exc

    def _stream(self, messages: Any, **kwargs: Any):  # type: ignore[override]
        raise self._exc

    def bind_tools(self, tools: Any, **kwargs: Any) -> FailingFakeLLM:
        return self


# ── Reusable chunk fixtures ────────────────────────────────────────────────────

@pytest.fixture()
def vpn_chunks() -> list[dict]:
    return [
        {
            "id": "KB-101",
            "title": "VPN Connectivity Troubleshooting Guide",
            "text": (
                "Verify the user's Cisco AnyConnect client version is 5.2.1 or "
                "higher by navigating to Help → About."
            ),
        },
        {
            "id": "KB-102",
            "title": "VPN Firewall and Split-Tunnel Policy",
            "text": (
                "Confirm that UDP port 4500 and TCP port 443 are not blocked by "
                "the user's home router or ISP."
            ),
        },
        {
            "id": "KB-103",
            "title": "Collecting AnyConnect Diagnostic Logs",
            "text": (
                "If the above steps do not resolve the issue, collect the "
                "AnyConnect diagnostic log bundle via AnyConnect → Diagnostics."
            ),
        },
    ]


@pytest.fixture()
def printer_chunks() -> list[dict]:
    """Irrelevant chunks — about printers, not VPN."""
    return [
        {
            "id": "KB-305",
            "title": "Network Printer Not Found",
            "text": (
                "Confirm the printer is powered on and the network status "
                "light is solid green."
            ),
        },
    ]


@pytest.fixture()
def vpn_incident() -> str:
    return (
        "Incident Number : INC0001\n"
        "Caller          : Ahmed Hassan\n"
        "Priority        : 2 - High\n"
        "Short Description: Cannot connect to corporate VPN from home\n\n"
        "Description:\n"
        "User reports that the Cisco AnyConnect client displays the error "
        "'Unable to establish VPN' when attempting to connect from their home "
        "network. Issue started after Windows update on 2026-09-15."
    )


@pytest.fixture()
def erp_incident() -> str:
    return (
        "Incident Number : INC0047\n"
        "Short Description: ERP module crashes on launch after latest patch\n\n"
        "The Finance ERP module crashes immediately on launch after the patch "
        "applied on 2026-09-17."
    )


@pytest.fixture()
def grounded_response() -> str:
    """A well-formed LLM response with correct citations."""
    return (
        "Suggested Resolution Procedure for: Cannot connect to corporate VPN from home\n"
        "────────────────────────────────────────────────────────────\n"
        "1. Verify the Cisco AnyConnect client version is 5.2.1 or higher. [KB-101]\n"
        "2. Confirm UDP port 4500 and TCP port 443 are not blocked. [KB-102]\n"
        "3. Collect the AnyConnect diagnostic log bundle for Level 2 review. [KB-103]\n\n"
        "Note: This procedure is a suggestion for support agent review only.\n"
        "No ticket actions have been taken."
    )


@pytest.fixture()
def fabricated_citation_response() -> str:
    """LLM response that cites a KB ID that was not in the retrieved chunks."""
    return (
        "Suggested Resolution Procedure for: Cannot connect to VPN\n"
        "1. Check the VPN version. [KB-101]\n"
        "2. Restart the network adapter. [KB-999]\n"
        "Note: This procedure is a suggestion for support agent review only.\n"
        "No ticket actions have been taken."
    )


@pytest.fixture()
def missing_citation_response() -> str:
    """LLM response where one step is missing its citation."""
    return (
        "Suggested Resolution Procedure for: Cannot connect to VPN\n"
        "1. Verify the VPN client version.\n"
        "2. Check firewall ports. [KB-102]\n"
        "Note: This procedure is a suggestion for support agent review only.\n"
        "No ticket actions have been taken."
    )
