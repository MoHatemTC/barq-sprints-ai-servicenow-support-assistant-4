"""
agent/__init__.py
─────────────────
BARQ G4 · S2.5 — Public API of the agent package.

Import from here for the cleanest interface:

    from agent import run_agent, AgentOutput, CLEAN_DECLINE
"""

from agent.executor import AgentConfigError, AgentLLMError, run_agent
from agent.prompt import CLEAN_DECLINE
from agent.schemas import AgentInput, AgentOutput, KnowledgeChunk, ResolutionStep

__all__ = [
    "run_agent",
    "AgentOutput",
    "AgentInput",
    "KnowledgeChunk",
    "ResolutionStep",
    "CLEAN_DECLINE",
    "AgentConfigError",
    "AgentLLMError",
]
