"""
S2.5 — LangChain Agent Executor for BARQ AI Support Assistant.

Wired to real LLM endpoint via LiteLLM / Gemini proxy, utilizing tools defined in tools.py.
"""

from typing import Any, Sequence
from langchain_openai import ChatOpenAI
from langchain_core.messages import (
    BaseMessage,
    SystemMessage,
    HumanMessage,
    AIMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool

from ..config import settings
from .prompts import SYSTEM_PROMPT
from .tools import AGENT_TOOLS


def get_llm(
    model: str | None = None,
    temperature: float | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> ChatOpenAI:
    """
    Constructs and returns the configured ChatOpenAI client pointed at the LiteLLM proxy.
    """
    return ChatOpenAI(
        model=model or settings.llm_model,
        temperature=temperature if temperature is not None else settings.llm_temperature,
        base_url=base_url or settings.litellm_base_url,
        api_key=api_key or settings.litellm_api_key,
    )


class SupportAgentExecutor:
    """
    Agent executor that runs the reasoning loop:
    1. Sends system instructions + conversation context to LLM with tools bound.
    2. Executes tool calls made by the LLM.
    3. Detects terminal actions (`submit_final_answer`, `request_human_review`)
       or continues until a max iteration limit is reached.
    """

    def __init__(
        self,
        llm: ChatOpenAI | None = None,
        tools: Sequence[BaseTool] | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        max_iterations: int = 6,
    ) -> None:
        self.llm = llm or get_llm()
        self.tools = list(tools if tools is not None else AGENT_TOOLS)
        self.tools_by_name = {tool.name: tool for tool in self.tools}
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        self.llm_with_tools = self.llm.bind_tools(self.tools)

    def run(
        self,
        incident: dict[str, Any],
        retrieved_chunks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """
        Executes the agent loop on an incoming incident and optional retrieved chunks.

        Returns a dictionary containing:
          - 'status': 'final_answer' | 'human_review_requested' | 'max_iterations_reached'
          - 'result': output of the terminal tool call
          - 'messages': complete message trajectory
          - 'work_notes': internal notes added during the run
        """
        # Format the user prompt containing incident details and any pre-retrieved context
        user_content_parts = [
            f"Incident Number: {incident.get('number', 'N/A')}",
            f"Short Description: {incident.get('short_description', '')}",
            f"Description: {incident.get('description', '')}",
        ]

        if retrieved_chunks is not None:
            user_content_parts.append("\nRetrieved Knowledge Base Chunks:")
            if not retrieved_chunks:
                user_content_parts.append("No knowledge base chunks retrieved.")
            else:
                for idx, chunk in enumerate(retrieved_chunks, 1):
                    art_id = chunk.get("article_id") or chunk.get("article_number", "KB-Unknown")
                    title = chunk.get("title") or chunk.get("short_description", "")
                    sec = chunk.get("section", "")
                    text = chunk.get("text", "")
                    user_content_parts.append(
                        f"--- Chunk {idx} (Article: {art_id} - '{title}', Section: '{sec}') ---\n{text}"
                    )

        user_message = HumanMessage(content="\n".join(user_content_parts))
        messages: list[BaseMessage] = [
            SystemMessage(content=self.system_prompt),
            user_message,
        ]

        work_notes: list[str] = []
        terminal_status: str = "max_iterations_reached"
        terminal_result: Any = None

        for step in range(self.max_iterations):
            response: AIMessage = self.llm_with_tools.invoke(messages)
            messages.append(response)

            tool_calls = getattr(response, "tool_calls", None)
            if not tool_calls:
                # Model stopped calling tools
                break

            terminal_encountered = False

            for tc in tool_calls:
                name = tc["name"]
                args = tc["args"]
                tool_call_id = tc["id"]

                tool = self.tools_by_name.get(name)
                if not tool:
                    tool_output = {"error": f"Tool '{name}' not found."}
                else:
                    try:
                        tool_output = tool.invoke(args)
                    except Exception as e:
                        tool_output = {"error": f"Tool execution failed: {str(e)}"}

                messages.append(
                    ToolMessage(
                        content=str(tool_output),
                        tool_call_id=tool_call_id,
                        name=name,
                    )
                )

                if name == "add_work_note":
                    note = args.get("note_text")
                    if note:
                        work_notes.append(note)

                elif name in ("submit_final_answer", "request_human_review"):
                    terminal_status = tool_output.get("status", name)
                    terminal_result = tool_output
                    terminal_encountered = True
                    break

            if terminal_encountered:
                break

        return {
            "status": terminal_status,
            "result": terminal_result,
            "work_notes": work_notes,
            "messages": messages,
        }
