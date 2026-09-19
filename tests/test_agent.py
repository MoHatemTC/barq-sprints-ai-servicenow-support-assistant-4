"""
Tests for S2.4 - S2.5 Agent Executor, System Prompt, and Tools.

Validates:
1. Grounded resolution with citations (sources must be present).
2. Clean decline / human review handoff when context is ungrounded or absent.
3. System prompt constraints (suggests, never resolves autonomously).
4. Tool definitions and schemas.
"""

import pytest
from langchain_core.messages import AIMessage

from barq_ai_support.agent.tools import (
    AGENT_TOOLS,
    search_knowledge_base,
    add_work_note,
    submit_final_answer,
    request_human_review,
    Citation,
)
from barq_ai_support.agent.prompts import SYSTEM_PROMPT
from barq_ai_support.agent import executor as executor_module
from barq_ai_support.agent.executor import SupportAgentExecutor, get_llm
from barq_ai_support.config import settings

needs_live_llm = pytest.mark.skipif(
    executor_module.ChatOpenAI is None or not settings.litellm_api_key,
    reason="Live LLM test requires langchain-openai and LITELLM_API_KEY",
)


def test_agent_tool_definitions():
    tool_names = [t.name for t in AGENT_TOOLS]
    assert "search_knowledge_base" in tool_names
    assert "add_work_note" in tool_names
    assert "submit_final_answer" in tool_names
    assert "request_human_review" in tool_names


def test_submit_final_answer_tool():
    citations = [
        Citation(article_number="KB0010087", title="VPN Resolution", section="Steps")
    ]
    res = submit_final_answer.invoke({
        "resolution_steps": "1. Reset credentials.",
        "sources": citations,
    })
    assert res["status"] == "final_answer"
    assert len(res["sources"]) == 1
    assert res["sources"][0]["article_number"] == "KB0010087"


def test_request_human_review_tool():
    res = request_human_review.invoke({"reason": "No relevant KB articles found."})
    assert res["status"] == "human_review_requested"
    assert "No relevant KB" in res["reason"]


def test_system_prompt_governance_rules():
    prompt = SYSTEM_PROMPT.lower()
    assert "suggest" in prompt
    assert "never resolve" in prompt
    assert "human" in prompt
    assert "citation" in prompt or "cite" in prompt


@needs_live_llm
def test_agent_grounded_resolution():
    incident = {
        "number": "INC0010042",
        "short_description": "User unable to authenticate to VPN",
        "description": "User changed domain password this morning and now VPN client rejects login.",
    }
    retrieved_chunks = [
        {
            "article_id": "KB0010087",
            "title": "VPN Authentication Troubleshooting",
            "section": "Resolution Steps",
            "text": "1. Verify user updated stored credentials in Windows Credential Manager. 2. Open Cisco AnyConnect and delete cached server profile. 3. Re-enter new Active Directory password.",
        }
    ]

    executor = SupportAgentExecutor()
    output = executor.run(incident=incident, retrieved_chunks=retrieved_chunks)

    assert output["status"] == "final_answer"
    assert output["result"] is not None
    assert len(output["result"]["sources"]) >= 1
    # Check citation matches provenance
    article_numbers = [s["article_number"] for s in output["result"]["sources"]]
    assert "KB0010087" in article_numbers


@needs_live_llm
def test_agent_clean_decline_when_no_grounding():
    incident = {
        "number": "INC0099999",
        "short_description": "Hypothetical warp drive malfunction",
        "description": "The antimatter injectors are overflowing with dilithium crystals.",
    }
    # Completely empty knowledge context
    retrieved_chunks = []

    executor = SupportAgentExecutor()
    output = executor.run(incident=incident, retrieved_chunks=retrieved_chunks)

    # Agent must request human review and not hallucinate a resolution procedure
    assert output["status"] == "human_review_requested"
    assert output["result"] is not None
    assert "reason" in output["result"]


class _FakeToolCallingModel:
    """Minimal fake chat model: returns one canned AIMessage, no network."""

    def __init__(self, tool_call: dict):
        self._tool_call = tool_call

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        return AIMessage(content="", tool_calls=[self._tool_call])


def test_agent_executor_terminates_on_submit_final_answer():
    """Executor loop works end-to-end with a fake model (no live LLM)."""
    fake_llm = _FakeToolCallingModel(
        tool_call={
            "name": "submit_final_answer",
            "args": {
                "resolution_steps": "1. Reset credentials.",
                "sources": [
                    {
                        "article_number": "KB0010087",
                        "title": "VPN Resolution",
                        "section": "Steps",
                    }
                ],
            },
            "id": "call_fake_1",
            "type": "tool_call",
        }
    )
    incident = {
        "number": "INC0010042",
        "short_description": "User unable to authenticate to VPN",
        "description": "VPN client rejects login.",
    }
    executor = SupportAgentExecutor(llm=fake_llm)
    output = executor.run(incident=incident, retrieved_chunks=[])

    assert output["status"] == "final_answer"
    assert output["result"]["sources"][0]["article_number"] == "KB0010087"


def test_agent_executor_terminates_on_request_human_review():
    """Refusal path also terminates the loop with a fake model."""
    fake_llm = _FakeToolCallingModel(
        tool_call={
            "name": "request_human_review",
            "args": {"reason": "No relevant KB articles found."},
            "id": "call_fake_2",
            "type": "tool_call",
        }
    )
    executor = SupportAgentExecutor(llm=fake_llm)
    output = executor.run(
        incident={"number": "INC0099999", "short_description": "x", "description": "y"},
        retrieved_chunks=[],
    )

    assert output["status"] == "human_review_requested"
    assert "No relevant KB" in output["result"]["reason"]
