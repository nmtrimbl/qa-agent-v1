from __future__ import annotations

import json
import re
from typing import Any

from openai import OpenAI

from config.settings import get_settings
from models.test_step import StepAction, TestStep


def _extract_json_object(text: str) -> dict[str, Any]:
    """
    Best-effort JSON extraction for beginners.

    The LLM should output pure JSON, but sometimes it wraps in code fences.
    """

    text = text.strip()
    # Remove ```json fences if present.
    text = re.sub(r"^```(?:json)?\\s*", "", text)
    text = re.sub(r"```\\s*$", "", text)

    # If there is extra text, try to find the first {...} block.
    if not (text.startswith("{") and text.endswith("}")):
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            text = match.group(0)

    return json.loads(text)


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

    system_prompt = (
        "You are a QA test planner. Convert manual QA notes into deterministic browser test steps.\n"
        "Return ONLY valid JSON (no explanations).\n"
        "The JSON must be of the form: {\"steps\": [ ... ]}.\n"
        "Each step must use one of these actions: goto, click, fill, press, assert_text, screenshot.\n"
        "Use these conventions for `selector`:\n"
        "  - CSS selectors (e.g. \"button[type='submit']\"), OR\n"
        "  - text selector format: \"text=Visible text\" (exact match).\n"
        "For `fill`, use ONLY CSS selectors for inputs.\n"
        "For `assert_text`, use `selector` to locate an element, and `expected_text` for the expected string.\n"
        "For `press`, use `key` like \"Enter\".\n"
    )

    user_prompt = (
        f"URL: {url}\n\n"
        "Manual QA test notes:\n"
        f"{test_notes}\n\n"
        "Rules:\n"
        "1) The first step must be an action `goto` with the same URL.\n"
        "2) Include at least one `screenshot` step near the end (after the main assertions if possible).\n"
        "3) Keep the steps short and beginner-friendly (max 20 steps).\n"
        "4) Prefer text=... for clicks/assertions when it is unambiguous.\n"
    )

    temperature = 0.2

    # Try to force JSON output. If the model/provider doesn't support response_format,
    # we fall back to parsing JSON from the returned content.
    response_text = None
    try:
        resp = client.chat.completions.create(
            model=settings.openai_model,
            temperature=temperature,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            response_format={"type": "json_object"},
        )
        response_text = resp.choices[0].message.content or ""
    except TypeError:
        # Older OpenAI SDK / provider might not accept response_format for chat.completions.
        resp = client.chat.completions.create(
            model=settings.openai_model,
            temperature=temperature,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        )
        response_text = resp.choices[0].message.content or ""

    data = _extract_json_object(response_text)
    raw_steps = data.get("steps", [])
    if not isinstance(raw_steps, list):
        raise ValueError("Planner returned JSON without a list field `steps`.")

    steps: list[TestStep] = [TestStep.model_validate(s) for s in raw_steps]

    # Deterministic guardrails for beginner reliability.
    steps = steps[:20]
    if not steps or steps[0].action != StepAction.goto:
        steps.insert(0, TestStep(action=StepAction.goto, url=url))
    else:
        steps[0] = TestStep(action=StepAction.goto, url=url)

    if not any(s.action == StepAction.screenshot for s in steps):
        steps.append(TestStep(action=StepAction.screenshot, screenshot_name="final", full_page=True))

    return steps

