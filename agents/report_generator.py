from __future__ import annotations

import json
import re
from typing import Any, Optional

from openai import OpenAI
from pydantic import BaseModel, Field

from agents.bug_analyzer import BugAnalysis
from browser.executor import ExecutionResult
from config.settings import get_settings
from models.test_report import TestReport


class ReportText(BaseModel):
    failure_summary: str = Field(default="")


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\\s*", "", text)
    text = re.sub(r"```\\s*$", "", text)
    if not (text.startswith("{") and text.endswith("}")):
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            text = match.group(0)
    return json.loads(text)


def generate_report(*, run_id: str, url: str, execution_result: ExecutionResult, bug_analysis: BugAnalysis) -> TestReport:
    """
    LLM-based report agent.

    Deterministic fields:
      - steps_executed
      - console errors
      - screenshot paths
      - overall PASS/FAIL

    LLM fields:
      - failure_summary (more beginner-friendly wording)
    """

    overall_status = "PASS" if execution_result.success else "FAIL"

    failure_summary = ""
    if overall_status == "PASS":
        failure_summary = "PASS: All planned steps completed successfully."
    else:
        failure_summary = bug_analysis.failure_summary or "FAIL: A test step failed."

    # Improve summary with LLM if key is present.
    settings = get_settings()
    if overall_status == "FAIL" and settings.openai_api_key:
        client = OpenAI(api_key=settings.openai_api_key)

        system_prompt = (
            "You are generating a QA report section for a failed automated test.\n"
            "Write a short, beginner-friendly failure summary.\n"
            "Return ONLY JSON: {\"failure_summary\": \"...\"}."
        )

        failure_step = execution_result.steps_executed[-1] if execution_result.steps_executed else None
        failure_step_info = failure_step.step.model_dump(mode="json") if failure_step else None

        user_prompt = {
            "url": url,
            "test_failed_step": failure_step_info,
            "exception_type": execution_result.failure.exception_type if execution_result.failure else None,
            "error_message": execution_result.failure.error_message if execution_result.failure else None,
            "likely_causes": bug_analysis.likely_causes,
            "suggestions": bug_analysis.suggestions,
            "console_errors": [e.model_dump(mode="json") for e in execution_result.console_errors][:10],
        }

        try:
            resp = client.chat.completions.create(
                model=settings.openai_model,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
            )
            data = _extract_json_object(resp.choices[0].message.content or "")
            report_text = ReportText.model_validate(data)
            if report_text.failure_summary.strip():
                failure_summary = report_text.failure_summary.strip()
        except Exception:
            # If report generation fails, keep the deterministic summary.
            pass

    report = TestReport(
        run_id=run_id,
        url=url,
        overall_status=overall_status,  # type: ignore[arg-type]
        failure_summary=failure_summary,
        steps_executed=execution_result.steps_executed,
        failed_step=execution_result.failed_step,
        console_errors=execution_result.console_errors,
        screenshot_paths=execution_result.screenshot_paths,
        page_url_at_failure=execution_result.failure.page_url_at_failure if execution_result.failure else None,
    )
    return report

