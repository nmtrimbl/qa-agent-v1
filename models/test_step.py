from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StepAction(str, Enum):
    goto = "goto"
    click = "click"
    fill = "fill"
    press = "press"
    assert_text = "assert_text"
    screenshot = "screenshot"


class TestStep(BaseModel):
    """
    A single deterministic browser action.

    The Browser Executor reads these fields and performs the action in Playwright.
    """

    action: StepAction

    # Enforce strict planner output: only known fields are allowed in JSON.
    # This makes the planner retry/repair more reliable.
    model_config = ConfigDict(extra="forbid")

    # For `goto`
    url: Optional[str] = None

    # For `click`, `fill`, `press` (optional), `assert_text`
    selector: Optional[str] = None

    # Optional semantic planning fields.
    #
    # These make the step more flexible without changing the executor into an
    # autonomous agent. The executor still tries them in a fixed deterministic
    # order.
    intent: Optional[str] = None
    candidate_labels: list[str] = Field(default_factory=list)
    candidate_selectors: list[str] = Field(default_factory=list)
    menu_hints: list[str] = Field(default_factory=list)
    fallback_actions: list[str] = Field(default_factory=list)
    memory_hint_ids: list[str] = Field(default_factory=list)
    optional: bool = False

    # For `fill`
    text: Optional[str] = None

    # For `press` (keyboard key, like "Enter", "Escape", etc.)
    key: Optional[str] = None

    # For `assert_text`
    expected_text: Optional[str] = None
    assertion_mode: Literal["contains", "equals"] = "contains"

    # For all actions that may wait
    timeout_ms: int = Field(default=5000, ge=1, le=60000)

    # For `screenshot`
    screenshot_name: Optional[str] = None
    full_page: bool = False

    @model_validator(mode="after")
    def validate_required_fields(self) -> "TestStep":
        if self.action == StepAction.goto:
            if not self.url:
                raise ValueError("goto steps require `url`.")
        elif self.action == StepAction.click:
            if not self.selector and not self.candidate_labels and not self.candidate_selectors:
                raise ValueError("click steps require `selector` or semantic fallback candidates.")
        elif self.action == StepAction.fill:
            if not self.selector:
                raise ValueError("fill steps require `selector`.")
            if self.text is None:
                raise ValueError("fill steps require `text`.")
        elif self.action == StepAction.press:
            if not self.key:
                raise ValueError("press steps require `key`.")
        elif self.action == StepAction.assert_text:
            if not self.selector and not self.candidate_labels and not self.candidate_selectors:
                raise ValueError("assert_text steps require `selector` or semantic fallback candidates.")
            if self.expected_text is None:
                raise ValueError("assert_text steps require `expected_text`.")
        elif self.action == StepAction.screenshot:
            # screenshot steps can omit a name; executor will pick a default.
            pass

        return self

