from langchain_core.tools import tool
from pydantic import BaseModel, Field


class KnowledgeBaseSearchInput(BaseModel):
    query: str = Field(..., description="Search text describing the incident's symptom.")


@tool("search_knowledge_base", args_schema=KnowledgeBaseSearchInput)
def search_knowledge_base(query: str) -> list[dict]:
    """Search the knowledge base for articles matching the query.
    Repeatable - can be called multiple times without ending the run."""
    return [
        {
            "article_id": "KB0001",
            "title": "VPN authentication fails after a password change",
            "section": "Resolution",
            "score": 0.85,
        },
        {
            "article_id": "KB0005",
            "title": "Account is locked after repeated failed sign-ins",
            "section": "Symptom",
            "score": 0.62,
        },
    ]


class WorkNoteInput(BaseModel):
    note_text: str = Field(..., description="Internal note text to add to the incident.")


@tool("add_work_note", args_schema=WorkNoteInput)
def add_work_note(note_text: str) -> dict:
    """Add an internal work note to the incident.
    Repeatable - can be called multiple times without ending the run."""
    return {"status": "work_note_recorded", "note_text": note_text}


class Citation(BaseModel):
    article_number: str = Field(..., description="e.g. KB0001")
    title: str = Field(..., description="Article title")
    section: str | None = Field(default=None, description="e.g. Resolution")


class FinalAnswerInput(BaseModel):
    resolution_steps: str = Field(..., description="Numbered resolution procedure.")
    sources: list[Citation] = Field(..., description="Articles that support the answer.", min_length=1)


@tool("submit_final_answer", args_schema=FinalAnswerInput)
def submit_final_answer(resolution_steps: str, sources: list[Citation]) -> dict:
    """Record the final grounded answer and its sources.
    Terminal - ends the run, no further tool calls after this."""
    return {
        "status": "final_answer",
        "resolution_steps": resolution_steps,
        "sources": [c.model_dump() for c in sources],
    }


class HumanReviewHandoffInput(BaseModel):
    reason: str = Field(..., description="Why this incident needs a human to review it.")


@tool("request_human_review", args_schema=HumanReviewHandoffInput)
def request_human_review(reason: str) -> dict:
    """Hand the incident off to a human for review.
    Terminal - ends the run, no further tool calls after this."""
    return {"status": "human_review_requested", "reason": reason}


AGENT_TOOLS = [
    search_knowledge_base,
    add_work_note,
    submit_final_answer,
    request_human_review,
]