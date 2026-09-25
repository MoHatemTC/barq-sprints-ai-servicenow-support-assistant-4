"""
S3.4 Demo Evidence Capture Script.

Runs the REAL agent pipeline - real ServiceNow (get_incident, add_work_note,
update_incident), real Qdrant retrieval - with only the final LLM call
substituted for a scripted response. This is because live LLM access via
the Sprints LiteLLM proxy is currently blocked by a proxy token issue
(token_not_found_in_db), outside my control - reported, awaiting a fix.
Everything except the LLM's own reasoning is 100% live and real.
"""
from dotenv import load_dotenv
load_dotenv()

import asyncio
from langchain_core.messages import AIMessage

from barq_ai_support.agent.s3_worker import (
    process_incident_event,
    run_agent_loop,
    _field_name,
)
from barq_ai_support.servicenow_client import ServiceNowClient


def make_tool_call(name: str, args: dict, call_id: str = "call_1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


class ScriptedLLM:
    """Stands in for the real LLM only - everything else in this run is real."""

    def __init__(self, responses):
        self._responses = list(responses)

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages):
        if not self._responses:
            return AIMessage(content="(no more scripted responses)")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def print_tool_call(tool_name: str, args: dict, result) -> None:
    print(f"  [TOOL CALL] {tool_name}({args})")
    print(f"  [OBSERVATION] {result}")


REAL_SYS_ID = "a9e428cac61122760075710592216c58"


def banner(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


async def scenario_1_high_confidence_suggest_answer():
    banner("SCENARIO 1: High-confidence run -> suggestAnswer (real writeback)")
    sn_client = ServiceNowClient()

    llm = ScriptedLLM([
        make_tool_call("searchKB", {"query": "reset forgotten password"}, "call_1"),
        make_tool_call(
            "suggestAnswer",
            {
                "procedure": "1. Verify the user's identity per standard procedure. "
                             "2. Reset the password in the identity console. "
                             "3. Ask the user to sign in with the new password and confirm access.",
                "sources": "KB0010087",
                "confidence": 0.64,
            },
            "call_2",
        ),
    ])

    incident = await sn_client.get_incident(REAL_SYS_ID)
    print("Real incident fetched:", incident["number"], "-", incident["short_description"])

    result = await run_agent_loop(incident, sn_client, llm=llm, on_tool_call=print_tool_call)
    print()
    print("Final result:", result)
    print(">>> This write is REAL - check ServiceNow now.")


async def scenario_2_unanswerable_request_hr():
    banner("SCENARIO 2: Unanswerable run -> requestHR (real escalation writeback)")
    sn_client = ServiceNowClient()

    llm = ScriptedLLM([
        make_tool_call("searchKB", {"query": "office coffee machine broken"}, "call_1"),
        make_tool_call(
            "requestHR",
            {"reason": "No relevant knowledge base article found for this symptom."},
            "call_2",
        ),
    ])

    incident = await sn_client.get_incident(REAL_SYS_ID)
    result = await run_agent_loop(incident, sn_client, llm=llm, on_tool_call=print_tool_call)
    print()
    print("Final result:", result)
    print(">>> This write is REAL - check ServiceNow now.")


async def scenario_3_searchkb_trace():
    banner("SCENARIO 3: searchKB tool-call trace (real Qdrant query + observation)")
    sn_client = ServiceNowClient()

    llm = ScriptedLLM([
        make_tool_call("searchKB", {"query": "forgotten password reset"}, "call_1"),
        make_tool_call("requestHR", {"reason": "Demo trace capture only."}, "call_2"),
    ])

    incident = await sn_client.get_incident(REAL_SYS_ID)
    result = await run_agent_loop(incident, sn_client, llm=llm, on_tool_call=print_tool_call)
    print()
    print("Final result:", result)
    print(">>> The [OBSERVATION] line above is a REAL Qdrant response.")


async def scenario_4_recoverable_failure_and_continuity():
    banner("SCENARIO 4: Recoverable failure + worker continuity (all real ServiceNow calls)")
    sn_client = ServiceNowClient()

    # Step 1: simulate what the eligibility webhook already does before this
    # worker ever runs - claim the incident as in_progress, unprocessed.
    print("--- Claiming incident as in_progress (simulating the webhook's claim step) ---")
    await sn_client.update_incident(REAL_SYS_ID, {
        _field_name("ai_status"): "in_progress",
        _field_name("ai_processed"): False,
    })
    print("Claimed. Now forcing a real failure mid-run...")

    # Step 2: force a genuine failure during the LLM call itself.
    failing_llm = ScriptedLLM([RuntimeError("Simulated LLM failure for demo purposes.")])

    print()
    print("--- Job A: incident claimed, then the LLM call fails ---")
    result_a = await process_incident_event({"sys_id": REAL_SYS_ID}, llm=failing_llm)
    print("Result A:", result_a)

    # Step 3: verify the incident was left exactly as claimed - untouched.
    incident_after = await sn_client.get_incident(REAL_SYS_ID)
    print()
    print("--- Verifying incident state after the failure (re-fetched from ServiceNow) ---")
    print("Incident:", incident_after)
    print(">>> ai_status should still be 'in_progress', ai_processed still False,")
    print(">>> no suggested response written. Check the ServiceNow UI to confirm.")

    # Step 4: prove worker continuity - process a different job right after.
    print()
    print("--- Job B: a different, healthy run, right after Job A's failure ---")
    healthy_llm = ScriptedLLM([make_tool_call("requestHR", {"reason": "Continuity check."}, "call_1")])
    result_b = await process_incident_event({"sys_id": REAL_SYS_ID}, llm=healthy_llm)
    print("Result B:", result_b)
    print(">>> This proves the worker survived Job A's failure and processed Job B normally.")


async def main():
    import sys

    scenarios = {
        "1": scenario_1_high_confidence_suggest_answer,
        "2": scenario_2_unanswerable_request_hr,
        "3": scenario_3_searchkb_trace,
        "4": scenario_4_recoverable_failure_and_continuity,
    }

    if len(sys.argv) < 2 or sys.argv[1] not in scenarios:
        print("Usage: python demo_evidence.py <1|2|3|4>")
        print("  1 = high-confidence suggestAnswer")
        print("  2 = unanswerable requestHR")
        print("  3 = searchKB trace")
        print("  4 = recoverable failure + continuity")
        return

    await scenarios[sys.argv[1]]()


if __name__ == "__main__":
    asyncio.run(main())