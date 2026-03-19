from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from openai import OpenAI
from pydantic import BaseModel, Field

from config.settings import get_settings
from browser.executor import ExecutionResult


class BugAnalysis(BaseModel):
    failure_summary: str = Field(default="")
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

    Uses artifacts (console errors + screenshot paths + exception details) to suggest likely causes.
    """

    settings = get_settings()
    if not settings.openai_api_key:
        # Keep the app runnable without an LLM key.
        failure = execution_result.failure
        return BugAnalysis(
            failure_summary=(failure.error_message if failure else "Test failed."),
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

    client = OpenAI(api_key=settings.openai_api_key)

    system_prompt = (
        "You are a debugging assistant for web UI automation.\n"
        "You will receive:\n"
        "- the URL\n"
        "- manual QA notes\n"
        "- the exception from the failed Playwright step\n"
        "- captured console errors\n"
        "- screenshot file paths\n"
        "Your job: summarize the most likely causes of the failure and what to check/fix.\n"
        "Return ONLY valid JSON with keys: failure_summary, likely_causes, suggestions."
    )

    console_errors = [
        {"message": e.message, "location": e.location, "page_url": e.page_url}
        for e in execution_result.console_errors
    ]

    stack_trace = (failure.stack_trace or "")
    stack_trace = stack_trace[-8000:] if len(stack_trace) > 8000 else stack_trace

    user_prompt = (
        f"URL: {url}\n\n"
        f"Manual QA notes:\n{test_notes}\n\n"
        f"Failed step exception:\n"
        f"- exception_type: {failure.exception_type}\n"
        f"- error_message: {failure.error_message}\n"
        f"- stack_trace (tail):\n{stack_trace}\n\n"
        f"Console errors captured:\n{json.dumps(console_errors, ensure_ascii=False)}\n\n"
        f"Screenshot paths:\n{json.dumps(execution_result.screenshot_paths, ensure_ascii=False)}\n"
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

    data = _extract_json_object(response_text)
    return BugAnalysis.model_validate(data)

