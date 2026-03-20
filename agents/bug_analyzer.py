from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal, Optional

from openai import OpenAI
from pydantic import BaseModel, Field

from config.settings import get_settings
from browser.executor import ExecutionResult


class BugAnalysis(BaseModel):
    failure_summary: str = Field(default="")
    likely_failure_cause: str = Field(default="")
    reproduction_notes: str = Field(default="")
    severity_guess: Literal["low", "medium", "high"] = "medium"
    likely_causes: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\\s*", "", text)
    text = re.sub(r"```\\s*$", "", text)
    if not (text.startswith("{") and text.endswith("}")):
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            text = match.group(0)
    return json.loads(text)


def analyze_failure(
    *,
    url: str,
    test_notes: str,
    execution_result: ExecutionResult,
    artifacts_dir: str | Path | None = None,
) -> BugAnalysis:
    """
    LLM-planning agent (analysis).

    Uses structured execution details plus artifacts to suggest likely causes.
    """

    settings = get_settings()
    if not settings.openai_api_key:
        # Keep the app runnable without an LLM key.
        failure = execution_result.failure
        failed_step = execution_result.failed_step
        final_url = _get_final_url(url=url, execution_result=execution_result)
        return BugAnalysis(
            failure_summary=(failure.error_message if failure else "Test failed."),
            likely_failure_cause=(failure.error_message if failure else "The automated check failed."),
            reproduction_notes=_build_fallback_reproduction_notes(failed_step=failed_step, final_url=final_url),
            severity_guess="medium",
            likely_causes=[],
            suggestions=[],
        )

    # Optional: reload execution_result from artifacts on disk.
    # This satisfies the "reads artifacts" requirement while keeping the MVP simple.
    if artifacts_dir is not None:
        artifacts_dir = Path(artifacts_dir)
        execution_path = artifacts_dir / "execution_result.json"
        if execution_path.exists():
            try:
                raw = json.loads(execution_path.read_text(encoding="utf-8"))
                execution_result = ExecutionResult.model_validate(raw)
            except Exception:
                # If parsing fails, fall back to the in-memory execution_result.
                pass

    failure = execution_result.failure
    if not failure:
        return BugAnalysis(failure_summary="No failure details found.")

    failed_step = execution_result.failed_step
    final_url = _get_final_url(url=url, execution_result=execution_result)

    client = OpenAI(api_key=settings.openai_api_key)

    system_prompt = (
        "You are a debugging assistant for web UI automation.\n"
        "You will receive:\n"
        "- the URL\n"
        "- manual QA notes\n"
        "- the exception from the failed Playwright step\n"
        "- the executed steps\n"
        "- the specific failed step\n"
        "- captured console errors\n"
        "- screenshot file paths\n"
        "- the final browser URL\n"
        "Your job: explain the most likely failure cause in beginner-friendly language.\n"
        "Return ONLY valid JSON with keys: "
        "failure_summary, likely_failure_cause, reproduction_notes, severity_guess, likely_causes, suggestions.\n"
        "severity_guess must be one of: low, medium, high."
    )

    executed_steps = [
        {
            "step_index": step.step_index,
            "action": step.step.action.value,
            "status": step.status,
            "selector": step.step.selector,
            "expected_text": step.step.expected_text,
            "page_url": step.page_url,
            "error_message": step.error_message,
        }
        for step in execution_result.steps_executed
    ]

    console_errors = [
        {"message": e.message, "location": e.location, "page_url": e.page_url}
        for e in execution_result.console_errors
    ]

    stack_trace = (failure.stack_trace or "")
    stack_trace = stack_trace[-8000:] if len(stack_trace) > 8000 else stack_trace

    user_prompt = (
        f"URL: {url}\n\n"
        f"Manual QA notes:\n{test_notes}\n\n"
        f"Executed steps:\n{json.dumps(executed_steps, ensure_ascii=False)}\n\n"
        f"Failed step:\n{json.dumps(failed_step.model_dump(mode='json') if failed_step else None, ensure_ascii=False)}\n\n"
        f"Failed step exception:\n"
        f"- exception_type: {failure.exception_type}\n"
        f"- error_message: {failure.error_message}\n"
        f"- stack_trace (tail):\n{stack_trace}\n\n"
        f"Console errors captured:\n{json.dumps(console_errors, ensure_ascii=False)}\n\n"
        f"Screenshot paths:\n{json.dumps(execution_result.screenshot_paths, ensure_ascii=False)}\n\n"
        f"Final URL: {final_url}\n"
    )

    response_text: Optional[str] = None
    try:
        resp = client.chat.completions.create(
            model=settings.openai_model,
            temperature=0.2,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            response_format={"type": "json_object"},
        )
        response_text = resp.choices[0].message.content or ""
    except TypeError:
        resp = client.chat.completions.create(
            model=settings.openai_model,
            temperature=0.2,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        )
        response_text = resp.choices[0].message.content or ""

    try:
        data = _extract_json_object(response_text)
        return BugAnalysis.model_validate(data)
    except Exception:
        return BugAnalysis(
            failure_summary=failure.error_message,
            likely_failure_cause=failure.error_message,
            reproduction_notes=_build_fallback_reproduction_notes(failed_step=failed_step, final_url=final_url),
            severity_guess="medium",
            likely_causes=[],
            suggestions=[],
        )


def _get_final_url(*, url: str, execution_result: ExecutionResult) -> str:
    if execution_result.failed_step and execution_result.failed_step.page_url:
        return execution_result.failed_step.page_url
    if execution_result.steps_executed:
        last_step = execution_result.steps_executed[-1]
        if last_step.page_url:
            return last_step.page_url
    if execution_result.failure and execution_result.failure.page_url_at_failure:
        return execution_result.failure.page_url_at_failure
    return url


def _build_fallback_reproduction_notes(*, failed_step, final_url: str) -> str:
    if failed_step is None:
        return f"Open the page and repeat the planned flow until the failure appears. Final URL: {final_url}"

    return (
        f"Open the page, repeat the steps until step {failed_step.step_index + 1} "
        f"({failed_step.step.action.value}) fails, and verify the browser state at {final_url}."
    )

