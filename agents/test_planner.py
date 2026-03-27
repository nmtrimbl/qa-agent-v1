from __future__ import annotations

import json
import re
from typing import Any, Optional

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, ValidationError

from config.settings import get_settings
from models.site_memory import PlannerMemoryView
from models.test_step import StepAction, TestStep
from utils.json_helpers import strip_markdown_code_fences


class PlannerOutput(BaseModel):
    """
    The strict shape the planner must output.

    We set `extra="forbid"` so the planner cannot return extra keys.
    """

    steps: list[TestStep]
    model_config = ConfigDict(extra="forbid")


SUPPORTED_ACTIONS = {a.value for a in StepAction}


def _load_planner_output(text: str) -> PlannerOutput:
    """
    Parse JSON and validate it against `PlannerOutput` (Pydantic).
    """

    clean = strip_markdown_code_fences(text)
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
    steps = [_normalize_selector_fields(step) for step in steps]

    # Ensure first step is `goto` for the provided URL.
    if not steps or steps[0].action != StepAction.goto:
        steps.insert(0, TestStep(action=StepAction.goto, url=url, timeout_ms=15000))
    else:
        timeout_ms = max(steps[0].timeout_ms, 15000)
        steps[0] = TestStep(action=StepAction.goto, url=url, timeout_ms=timeout_ms)

    # Ensure at least one screenshot exists; append if missing.
    if not any(s.action == StepAction.screenshot for s in steps):
        steps.append(TestStep(action=StepAction.screenshot, screenshot_name="final", full_page=True))

    return steps


def _normalize_selector_fields(step: TestStep) -> TestStep:
    """
    Keep single `selector` and list-based `candidate_selectors` separate.

    The planner sometimes returns a comma-separated selector list inside the
    single `selector` field. For click/assert flows we normalize that into
    `candidate_selectors`, because the executor treats these as fallback
    choices. For `fill`, we keep `selector` unchanged because Playwright allows
    comma-separated CSS selectors there and the field is required.
    """

    if not step.selector or step.action == StepAction.fill:
        return step

    selector_parts = [part.strip() for part in re.split(r"\s*,\s*", step.selector) if part.strip()]
    if len(selector_parts) <= 1:
        return step

    merged_candidate_selectors: list[str] = []
    seen: set[str] = set()
    for selector in selector_parts + list(step.candidate_selectors):
        clean = selector.strip()
        if not clean or clean in seen:
            continue
        merged_candidate_selectors.append(clean)
        seen.add(clean)

    step_data = step.model_dump(mode="python")
    step_data["selector"] = None
    step_data["candidate_selectors"] = merged_candidate_selectors
    return TestStep.model_validate(step_data)


def plan_test_steps(
    url: str,
    test_notes: str,
    *,
    domain_memory: Optional[PlannerMemoryView] = None,
    global_memory: Optional[PlannerMemoryView] = None,
) -> list[TestStep]:
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

    domain_memory = domain_memory or PlannerMemoryView(domain="unknown", scope="domain")
    global_memory = global_memory or PlannerMemoryView(domain="_global", scope="global")

    # First prompt: ask for strict JSON shape.
    system_prompt = (
        "You are a QA test planner.\n"
        "Convert the user's manual QA notes into deterministic browser test steps.\n"
        "Interpret instructions by user intent, not only by literal visible text.\n"
        "If the notes say something like 'click login', you may use semantic hints\n"
        "such as account icons, sign-in labels, or opening a menu first.\n"
        "Memory is advisory, not guaranteed truth. Prefer domain memory before\n"
        "global memory, but always keep fallbacks soft and deterministic.\n"
        "Return ONLY valid JSON with this top-level shape:\n"
        '{ "steps": [ ... ] }\n'
        "Each step object must match the Pydantic `TestStep` schema exactly "
        "(no unknown keys). You may omit fields with defaults.\n"
        "Mandatory fields per action:\n"
        "- goto: must include `url`\n"
        "- click: should prefer `candidate_selectors` and `candidate_labels` for alternatives; use `selector` only for one exact selector string\n"
        "- fill: must include `selector` and `text`, and only enters text without submitting the form\n"
        "- press: must include `key` (optional: `selector`)\n"
        "- assert_text: must include `expected_text`, plus `selector` or semantic fallback candidates\n"
        "- screenshot: optional `screenshot_name`, optional `full_page`\n"
        "Optional semantic fields:\n"
        "- intent: short summary like `login`, `open_navigation`, `account_access`\n"
        "- candidate_labels: alternate UI text such as `Login`, `Sign In`, `Account`\n"
        "- candidate_selectors: alternate selectors or aria/title selectors\n"
        "- menu_hints: labels that may need to be opened before the main target is clickable\n"
        "- fallback_actions: short notes about fallback strategy\n"
        "- memory_hint_ids: IDs of memory hints that influenced this step\n"
        "- optional: true only if failing the step should not stop the main intent flow\n"
        "Supported actions: " + ", ".join(sorted(SUPPORTED_ACTIONS)) + ".\n"
        "No markdown. No extra keys."
    )

    user_prompt = (
        f"URL: {url}\n\n"
        "Manual QA test notes:\n"
        f"{test_notes}\n\n"
        "Domain memory hints:\n"
        f"{json.dumps(_serialize_memory_view(domain_memory), ensure_ascii=False)}\n\n"
        "Global memory hints:\n"
        f"{json.dumps(_serialize_memory_view(global_memory), ensure_ascii=False)}\n\n"
        "Rules:\n"
        "1) Output must be exactly the JSON schema. No explanations.\n"
        "2) The first step should be action `goto` with the same URL.\n"
        "3) Prefer flexible semantic steps for ambiguous instructions. Use exact selectors only when they are obvious.\n"
        "4) For click/assert_text, include multiple candidate labels when wording may vary.\n"
        "5) If a menu or account icon might need to open first, add `menu_hints` or an intermediate click step.\n"
        "6) Use `memory_hint_ids` only for relevant hints. Do not copy every hint into every step.\n"
        "7) For `fill`, use CSS selectors only.\n"
        "8) `fill` only enters text and does not submit. To submit a search or form after `fill`, add a `press` step with `key: \"Enter\"`. Do NOT add a `click` on a search/submit button after `fill`, as it may hit an autocomplete dropdown instead.\n"
        "9) Include at least one `screenshot` step near the end.\n"
        "10) `selector` must be a single selector string only, never a comma-separated list.\n"
        "11) If you want multiple selector options, put them in `candidate_selectors` as a JSON array.\n"
        "12) For click and assert_text, prefer `candidate_selectors` over `selector` when there are multiple possible targets.\n"
        "13) Max 20 steps."
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
                "Use `selector` only for one selector string. If there are multiple selectors, put them in `candidate_selectors`.\n"
                "A `fill` step only enters text and must not submit the form. To submit after fill, use a `press` step with `key: \"Enter\"`. Never add a `click` on a search/submit button after `fill`.\n"
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


def _serialize_memory_view(memory: PlannerMemoryView) -> dict[str, Any]:
    return {
        "domain": memory.domain,
        "scope": memory.scope,
        "top_hints": [hint.model_dump(mode="json") for hint in memory.top_hints],
        "recent_feedback": [entry.model_dump(mode="json") for entry in memory.recent_feedback],
    }

