from __future__ import annotations

import json
import re
from typing import Any, Optional

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, ValidationError

from config.settings import get_settings
from models.test_step import StepAction, TestStep


class PlannerOutput(BaseModel):
    """
    The strict shape the planner must output.

    We set `extra="forbid"` so the planner cannot return extra keys.
    """

    steps: list[TestStep]
    model_config = ConfigDict(extra="forbid")


SUPPORTED_ACTIONS = {a.value for a in StepAction}


def _strip_code_fences(text: str) -> str:
    """
    Remove ```json ... ``` fences if the model included them.
    """

    text = text.strip()
    # If the LLM wraps JSON in markdown code fences, remove them before json.loads().
    # Note: use `\s*` (not `\\s*`) so regex understands "whitespace".
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    return text


def _load_planner_output(text: str) -> PlannerOutput:
    """
    Parse JSON and validate it against `PlannerOutput` (Pydantic).
    """

    clean = _strip_code_fences(text)
    data = json.loads(clean)
    return PlannerOutput.model_validate(data)


def _canonicalize_steps(url: str, steps: list[TestStep]) -> list[TestStep]:
    """
    Make planner output deterministic and safe for the executor.

    This does NOT change the browser executor architecture; it just ensures
    the produced steps follow MVP guardrails.
    """

    # Keep it small and beginner-friendly.
    steps = steps[:20]

    # Ensure first step is `goto` for the provided URL.
    if not steps or steps[0].action != StepAction.goto:
        steps.insert(0, TestStep(action=StepAction.goto, url=url))
    else:
        steps[0] = TestStep(action=StepAction.goto, url=url)

    # Ensure at least one screenshot exists; append if missing.
    if not any(s.action == StepAction.screenshot for s in steps):
        steps.append(TestStep(action=StepAction.screenshot, screenshot_name="final", full_page=True))

    return steps


def plan_test_steps(url: str, test_notes: str) -> list[TestStep]:
    """
    LLM-planning agent.

    Inputs:
      - url: website URL
      - test_notes: manual QA notes

    Output:
      - list[TestStep] (validated with Pydantic)
    """

    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("Missing OPENAI_API_KEY. Copy `.env.example` to `.env` and set it.")

    client = OpenAI(api_key=settings.openai_api_key)

    # We keep temperature low for deterministic and repeatable JSON.
    temperature = 0.0

    def call_llm(*, system: str, user: str) -> str:
        """
        Call the LLM and return raw text content.

        We try to ask for JSON-only via `response_format`. If the provider
        doesn't support it, we still parse strictly from the returned text.
        """

        try:
            resp = client.chat.completions.create(
                model=settings.openai_model,
                temperature=temperature,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={"type": "json_object"},
            )
            return resp.choices[0].message.content or ""
        except TypeError:
            resp = client.chat.completions.create(
                model=settings.openai_model,
                temperature=temperature,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
            return resp.choices[0].message.content or ""

    # First prompt: ask for strict JSON shape.
    system_prompt = (
        "You are a QA test planner.\n"
        "Convert the user's manual QA notes into deterministic browser test steps.\n"
        "Return ONLY valid JSON with this top-level shape:\n"
        '{ "steps": [ ... ] }\n'
        "Each step object must match the Pydantic `TestStep` schema exactly "
        "(no unknown keys). You may omit fields with defaults.\n"
        "Mandatory fields per action:\n"
        "- goto: must include `url`\n"
        "- click: must include `selector`\n"
        "- fill: must include `selector` and `text`\n"
        "- press: must include `key` (optional: `selector`)\n"
        "- assert_text: must include `selector` and `expected_text`\n"
        "- screenshot: optional `screenshot_name`, optional `full_page`\n"
        "Supported actions: " + ", ".join(sorted(SUPPORTED_ACTIONS)) + ".\n"
        "No markdown. No extra keys."
    )

    user_prompt = (
        f"URL: {url}\n\n"
        "Manual QA test notes:\n"
        f"{test_notes}\n\n"
        "Rules:\n"
        "1) Output must be exactly the JSON schema. No explanations.\n"
        "2) The first step should be action `goto` with the same URL.\n"
        "3) Prefer `selector` using CSS selectors, or `text=Visible text` for click/assert_text.\n"
        "4) For `fill`, use CSS selectors only.\n"
        "5) Include at least one `screenshot` step near the end.\n"
        "6) Max 20 steps."
    )

    # Retry logic (only once) for invalid JSON or schema mismatch.
    #
    # Important for beginners:
    # - The LLM is allowed to be messy, but THIS function is not:
    #   we parse JSON + validate with Pydantic.
    # - If parsing/validation fails, we do exactly one repair attempt
    #   (still asking for JSON only).
    last_response: Optional[str] = None
    last_error: Optional[str] = None
    for attempt in range(2):
        if attempt == 0:
            response_text = call_llm(system=system_prompt, user=user_prompt)
        else:
            # Repair prompt: provide the previous invalid output + the parser error.
            # Repair prompt is only used for attempt=1 and should be explicit
            # about what failed. We make it error-aware to reduce repeat failures.
            extra_repair_rule = ""
            if last_error and "assert_text steps require `expected_text`" in last_error:
                extra_repair_rule = (
                    "\nImportant repair rule: Every step with `action: \"assert_text\"` must include "
                    "`expected_text` as a string. Do not omit it.\n"
                    "Example step: {\"action\": \"assert_text\", \"selector\": \"h1\", \"expected_text\": \"Welcome\"}\n"
                )

            repair_prompt = (
                "Your previous output was invalid or did not match the required JSON schema.\n\n"
                f"Previous output:\n{last_response}\n\n"
                f"Error:\n{last_error}\n\n"
                "Return ONLY corrected JSON that matches:\n"
                '{ "steps": [ ... ] }\n'
                "Each step must only use fields supported by the Pydantic TestStep schema.\n"
                f"{extra_repair_rule}\n"
                "Supported actions: " + ", ".join(sorted(SUPPORTED_ACTIONS)) + ".\n"
                "No markdown, no extra keys."
            )
            response_text = call_llm(system=system_prompt, user=repair_prompt)

        last_response = response_text

        try:
            # Strict parse + validation.
            parsed = _load_planner_output(response_text)
            steps = _canonicalize_steps(url=url, steps=parsed.steps)
            return steps
        except (json.JSONDecodeError, ValidationError, ValueError) as e:
            last_error = str(e)
            if attempt == 1:
                raise ValueError(
                    "Planner failed to produce strict valid JSON for TestStep after one retry. "
                    f"Last error: {last_error}"
                ) from e

    # Unreachable due to raise above, but keeps type-checkers happy.
    raise RuntimeError("Planner failed unexpectedly.")

