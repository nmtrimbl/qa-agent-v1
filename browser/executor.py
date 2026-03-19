from __future__ import annotations

import traceback
from pathlib import Path
from typing import Optional

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import Page
from pydantic import BaseModel

from browser.browser_session import BrowserSession
from models.test_report import ConsoleError, StepExecution
from models.test_step import StepAction, TestStep
from utils.file_helpers import ensure_dir, safe_filename, write_json


class FailureInfo(BaseModel):
    error_message: str
    exception_type: Optional[str] = None
    stack_trace: Optional[str] = None
    page_url_at_failure: Optional[str] = None
    failure_screenshot_paths: list[str] = []


class ExecutionResult(BaseModel):
    success: bool
    steps_executed: list[StepExecution]
    failure: Optional[FailureInfo] = None

    console_errors: list[ConsoleError] = []
    screenshot_paths: list[str] = []


class BrowserExecutor:
    """
    Executes planned `TestStep`s deterministically using Playwright.
    """

    def __init__(self, artifacts_dir: str | Path):
        self.artifacts_dir = Path(artifacts_dir)

    def execute(
        self,
        session: BrowserSession,
        url: str,
        steps: list[TestStep],
        run_id: str,
    ) -> ExecutionResult:
        if session.page is None:
            raise RuntimeError("BrowserSession has not been started.")

        page = session.page
        screenshots_dir = ensure_dir(self.artifacts_dir / "screenshots" / run_id)

        steps_executed: list[StepExecution] = []
        screenshot_paths: list[str] = []

        failure_info: Optional[FailureInfo] = None

        for step_index, step in enumerate(steps):
            try:
                self._execute_single_step(
                    page=page,
                    step=step,
                    screenshots_dir=screenshots_dir,
                    screenshot_paths=screenshot_paths,
                )
                steps_executed.append(StepExecution(step=step, status="ok"))
            except Exception as e:
                tb = traceback.format_exc()
                exc_type = type(e).__name__
                # Take a failure screenshot immediately.
                failure_shot_path = screenshots_dir / f"failure_step_{step_index}_{safe_filename(step.action.value)}.png"
                try:
                    page.screenshot(path=str(failure_shot_path), full_page=True)
                    screenshot_paths.append(str(failure_shot_path))
                except Exception:
                    # If screenshot fails, still proceed with error reporting.
                    pass

                failure_info = FailureInfo(
                    error_message=str(e),
                    exception_type=exc_type,
                    stack_trace=tb,
                    page_url_at_failure=page.url,
                    failure_screenshot_paths=[str(failure_shot_path)] if failure_shot_path.exists() else [],
                )
                steps_executed.append(StepExecution(step=step, status="failed", error_message=str(e)))

                # Requirement: stop on failure to keep results clear for beginner MVP.
                break

        # Console errors captured during the run.
        console_errors = session.console_errors

        result = ExecutionResult(
            success=failure_info is None,
            steps_executed=steps_executed,
            failure=failure_info,
            console_errors=console_errors,
            screenshot_paths=screenshot_paths,
        )

        # Save raw execution result for debugging / bug analysis.
        write_json(self.artifacts_dir / "execution_result.json", result.model_dump(mode="json"))
        return result

    def _execute_single_step(
        self,
        page: Page,
        step: TestStep,
        screenshots_dir: Path,
        screenshot_paths: list[str],
    ) -> None:
        if step.action == StepAction.goto:
            page.goto(step.url, wait_until="load", timeout=step.timeout_ms)
            return

        if step.action == StepAction.click:
            locator = self._resolve_click_target(page, step.selector)
            locator.click(timeout=step.timeout_ms)
            return

        if step.action == StepAction.fill:
            if step.selector and step.selector.startswith("text="):
                raise ValueError("fill does not support `text=` selectors. Use a CSS selector for inputs.")
            if not step.selector:
                raise ValueError("fill requires `selector`.")
            page.locator(step.selector).fill(step.text or "", timeout=step.timeout_ms)
            return

        if step.action == StepAction.press:
            # Focus is optional. If a selector is given, click it first.
            if step.selector:
                locator = self._resolve_click_target(page, step.selector)
                locator.click(timeout=step.timeout_ms)
            page.keyboard.press(step.key, timeout=step.timeout_ms)
            return

        if step.action == StepAction.assert_text:
            if not step.selector:
                raise ValueError("assert_text requires `selector`.")
            expected = step.expected_text or ""

            actual_text = self._get_assert_text(page, selector=step.selector, timeout_ms=step.timeout_ms)

            if step.assertion_mode == "equals":
                if actual_text != expected:
                    raise AssertionError(f"Expected text equals '{expected}', got '{actual_text}'.")
            else:
                # contains
                if expected not in actual_text:
                    raise AssertionError(
                        f"Expected text to contain '{expected}', got '{actual_text}'."
                    )
            return

        if step.action == StepAction.screenshot:
            name = step.screenshot_name or "step_screenshot"
            file_path = screenshots_dir / f"{safe_filename(name)}.png"
            page.screenshot(path=str(file_path), full_page=step.full_page)
            screenshot_paths.append(str(file_path))
            return

        raise ValueError(f"Unknown action: {step.action}")

    def _resolve_click_target(self, page: Page, selector: Optional[str]):
        if not selector:
            raise ValueError("click requires `selector`.")

        # Support a beginner-friendly selector style:
        #   "text=Login" -> click the element that has visible text "Login" (exact match).
        if selector.startswith("text="):
            text_value = selector[len("text=") :]
            return page.get_by_text(text_value, exact=True).first

        # Otherwise treat selector as a CSS selector.
        return page.locator(selector).first

    def _get_assert_text(self, page: Page, selector: str, timeout_ms: int) -> str:
        if selector.startswith("text="):
            text_value = selector[len("text=") :]
            # Use first match; MVP keeps this simple/reliable.
            locator = page.get_by_text(text_value, exact=True).first
        else:
            locator = page.locator(selector).first

        return locator.inner_text(timeout=timeout_ms).strip()

