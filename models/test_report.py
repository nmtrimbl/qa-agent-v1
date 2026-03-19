from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from models.test_step import TestStep


class StepExecution(BaseModel):
    """
    Captures what happened when executing one planned step.
    """

    step_index: int
    step: TestStep
    status: Literal["ok", "failed"]
    page_url: Optional[str] = None
    screenshot_path: Optional[str] = None
    error_message: Optional[str] = None


class ConsoleError(BaseModel):
    """
    Represents a console error captured from the browser.
    """

    kind: Literal["console_error", "page_error"] = "console_error"
    message: str
    location: Optional[str] = None
    page_url: Optional[str] = None


class FailedStepDetails(BaseModel):
    """
    A focused failure summary for the step that stopped the run.
    """

    step_index: int
    step: TestStep
    error_message: str
    page_url: Optional[str] = None
    screenshot_path: Optional[str] = None


class TestReport(BaseModel):
    """
    The final human-readable QA report (structured JSON).
    """

    run_id: str
    url: str

    overall_status: Literal["PASS", "FAIL"]
    failure_summary: str = ""

    steps_executed: list[StepExecution] = Field(default_factory=list)
    failed_step: Optional[FailedStepDetails] = None

    console_errors: list[ConsoleError] = Field(default_factory=list)
    screenshot_paths: list[str] = Field(default_factory=list)
    page_url_at_failure: Optional[str] = None

