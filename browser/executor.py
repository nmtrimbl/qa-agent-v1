from __future__ import annotations

import re
import traceback
import unicodedata
from pathlib import Path
from typing import Optional

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

    FOOTER_SELECTORS = ("footer", "[role='contentinfo']", ".footer")

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
                    self._capture_full_page_screenshot(page, failure_shot_path)
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

        # Always attach one final full-page screenshot for the report so the
        # user can inspect the full end state of the page, regardless of where
        # the planner inserted screenshot steps.
        final_report_screenshot = screenshots_dir / "final_report_full_page.png"
        try:
            self._capture_full_page_screenshot(page, final_report_screenshot)
            screenshot_paths.append(str(final_report_screenshot))
        except Exception:
            # The run result is still useful even if the final report screenshot fails.
            pass

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
            self._assert_text(page=page, step=step)
            return

        if step.action == StepAction.screenshot:
            name = step.screenshot_name or "step_screenshot"
            file_path = screenshots_dir / f"{safe_filename(name)}.png"
            # Always store a full-page screenshot so reports include footer/content
            # below the initial viewport.
            self._capture_full_page_screenshot(page, file_path)
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

    def _assert_text(self, page: Page, step: TestStep) -> None:
        """
        Assert text using a few simple layers:
        1) direct locator lookup
        2) full-page DOM/body/footer text search
        3) scroll and retry for footer/below-the-fold content
        """

        selector = step.selector or ""
        expected = step.expected_text or ""
        mode = step.assertion_mode

        locator_text = self._try_get_assert_text(page, selector=selector, timeout_ms=step.timeout_ms)
        if locator_text is not None and self._text_matches(actual=locator_text, expected=expected, mode=mode):
            return

        if self._find_text_anywhere_on_page(page, expected=expected, mode=mode, timeout_ms=step.timeout_ms):
            return

        self._scroll_intelligently_for_text(page, selector=selector, expected=expected)

        locator_text = self._try_get_assert_text(page, selector=selector, timeout_ms=step.timeout_ms)
        if locator_text is not None and self._text_matches(actual=locator_text, expected=expected, mode=mode):
            return

        if self._find_text_anywhere_on_page(page, expected=expected, mode=mode, timeout_ms=step.timeout_ms):
            return

        actual_preview = locator_text if locator_text is not None else "<locator text not found>"
        raise AssertionError(
            f"Expected text ({mode}) '{expected}', but assertion failed. "
            f"Last locator text was: '{actual_preview}'."
        )

    def _try_get_assert_text(self, page: Page, selector: str, timeout_ms: int) -> Optional[str]:
        try:
            return self._get_assert_text(page, selector=selector, timeout_ms=timeout_ms)
        except Exception:
            return None

    def _find_text_anywhere_on_page(self, page: Page, expected: str, mode: str, timeout_ms: int) -> bool:
        """
        Search text beyond the initial viewport.

        `body.inner_text()` lets us search the page text as a whole, and common
        footer selectors give us a reliable fallback for footer checks.
        """

        text_candidates: list[str] = []

        body_text = self._safe_inner_text(page.locator("body"), timeout_ms=timeout_ms)
        if body_text:
            text_candidates.append(body_text)

        for selector in self.FOOTER_SELECTORS:
            footer_text = self._safe_inner_text(page.locator(selector).first, timeout_ms=timeout_ms)
            if footer_text:
                text_candidates.append(footer_text)

        return any(self._text_matches(actual=text, expected=expected, mode=mode) for text in text_candidates)

    def _scroll_intelligently_for_text(self, page: Page, selector: str, expected: str) -> None:
        """
        Scroll toward likely text location before a second assertion attempt.

        Footer-ish text gets a direct scroll to bottom. Otherwise we do a small
        progressive scroll and then end at the bottom.
        """

        if self._looks_like_footer_check(selector=selector, expected=expected):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(300)
            return

        page.evaluate("window.scrollTo(0, Math.floor(document.body.scrollHeight * 0.5))")
        page.wait_for_timeout(250)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(300)

    def _looks_like_footer_check(self, selector: str, expected: str) -> bool:
        footer_signals = ("footer", "contentinfo", "copyright", "all rights reserved", "privacy", "terms", "©", "®")
        combined = f"{selector} {expected}".lower()
        return any(signal in combined for signal in footer_signals)

    def _capture_full_page_screenshot(self, page: Page, file_path: Path) -> None:
        """
        Capture a true full-page screenshot.

        Some sites only render footer or below-the-fold sections after scrolling,
        so we walk down the page first to trigger lazy content, then capture the
        full-page image and restore the previous scroll position.
        """

        metrics = self._get_page_metrics(page)
        original_y = metrics["current_y"]
        viewport_height = metrics["viewport_height"]
        scroll_height = metrics["scroll_height"]

        # Trigger lazy-loaded sections before the final full-page screenshot.
        step_size = max(viewport_height - 120, 250)
        scroll_positions = list(range(0, max(scroll_height, 1), step_size))

        for position in scroll_positions:
            page.evaluate(f"window.scrollTo(0, {position})")
            page.wait_for_timeout(120)

        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(250)

        page.screenshot(path=str(file_path), full_page=True)

        # Restore the user's prior scroll location so later steps behave predictably.
        page.evaluate(f"window.scrollTo(0, {original_y})")
        page.wait_for_timeout(100)

    def _get_page_metrics(self, page: Page) -> dict[str, int]:
        try:
            return page.evaluate(
                """() => {
                    const body = document.body;
                    const doc = document.documentElement;
                    const scrollHeight = Math.max(
                        body ? body.scrollHeight : 0,
                        doc ? doc.scrollHeight : 0,
                        body ? body.offsetHeight : 0,
                        doc ? doc.offsetHeight : 0,
                    );

                    return {
                        current_y: Math.floor(window.scrollY || 0),
                        viewport_height: Math.floor(window.innerHeight || 800),
                        scroll_height: Math.floor(scrollHeight || 0),
                    };
                }"""
            )
        except Exception:
            # Safe fallback if page metrics are unavailable for some reason.
            return {"current_y": 0, "viewport_height": 800, "scroll_height": 2000}

    def _safe_inner_text(self, locator, timeout_ms: int) -> Optional[str]:
        try:
            count = locator.count()
            if count < 1:
                return None
            return locator.inner_text(timeout=timeout_ms).strip()
        except Exception:
            return None

    def _text_matches(self, actual: str, expected: str, mode: str) -> bool:
        normalized_actual = self._normalize_text(actual)
        normalized_expected = self._normalize_text(expected)

        if mode == "equals":
            return normalized_actual == normalized_expected

        return normalized_expected in normalized_actual

    @staticmethod
    def _normalize_text(text: str) -> str:
        """
        Normalize small formatting differences so footer text is easier to match.
        """

        replacements = {
            "\u2018": "'",
            "\u2019": "'",
            "\u201c": '"',
            "\u201d": '"',
            "\u00a0": " ",
        }

        normalized = unicodedata.normalize("NFKC", text)
        for source, target in replacements.items():
            normalized = normalized.replace(source, target)

        normalized = re.sub(r"\s*([©®])\s*", r"\1", normalized)
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized.strip().lower()

