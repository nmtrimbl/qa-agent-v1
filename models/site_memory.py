from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


MemoryKind = Literal["known_flow", "label_mapping", "selector_hint", "menu_hint", "feedback_note"]
MemorySource = Literal["user_feedback", "successful_run", "system_inference"]
MemoryScope = Literal["domain", "global"]


class MemoryHint(BaseModel):
    """
    A small reusable hint that may help the planner or executor.

    Important: this is advisory memory, not a hard rule. The executor can try a
    hint as a fallback, and repeated successes or failures adjust its strength.
    """

    id: str
    intent: str = ""
    kind: MemoryKind = "feedback_note"
    candidates: list[str] = Field(default_factory=list)
    source: MemorySource = "user_feedback"
    scope: MemoryScope = "domain"
    strength: float = Field(default=0.35, ge=0.0, le=1.0)
    success_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)
    last_used_at: Optional[str] = None
    notes: str = ""


class FeedbackEntry(BaseModel):
    """
    Raw tester feedback tied to a site and, when available, a failed step.
    """

    created_at: str
    run_id: str
    issue: str
    correction: str
    intent: str = ""
    failed_step_index: Optional[int] = None
    failed_action: Optional[str] = None
    failed_selector: Optional[str] = None


class SiteMemory(BaseModel):
    """
    Feedback-assisted memory for one domain or for the shared global pool.
    """

    domain: str
    scope: MemoryScope = "domain"
    hints: list[MemoryHint] = Field(default_factory=list)
    feedback_history: list[FeedbackEntry] = Field(default_factory=list)


class PlannerMemoryView(BaseModel):
    """
    Reduced view passed into the planner prompt.
    """

    domain: str
    scope: MemoryScope
    top_hints: list[MemoryHint] = Field(default_factory=list)
    recent_feedback: list[FeedbackEntry] = Field(default_factory=list)
