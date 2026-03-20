from __future__ import annotations

from io import BytesIO
import re
import traceback
import unicodedata
from pathlib import Path
import time
from typing import Optional

from playwright.sync_api import Page
from PIL import Image
from pydantic import BaseModel

from browser.browser_session import BrowserSession
from models.test_report import ConsoleError, FailedStepDetails, StepExecution
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
    failed_step: Optional[FailedStepDetails] = None

    console_errors: list[ConsoleError] = []
    screenshot_paths: list[str] = []


class BrowserExecutor:
    """
    Executes planned `TestStep`s deterministically using Playwright.

    Important design rule:
    - This executor is deterministic and non-LLM-driven.
    - It only reads validated `TestStep` objects and performs them in order.
    """

    def __init__(self, artifacts_dir: str | Path):
        self.artifacts_dir = ensure_dir(artifacts_dir)
        self._auto_accept_cookies = True

    FOOTER_SELECTORS = ("footer", "[role='contentinfo']", ".footer")
    COOKIE_SIGNAL_WORDS = ("cookie", "cookies", "consent", "privacy", "gdpr")
    COOKIE_ACCEPT_SELECTORS = (
        "button.cky-btn-accept",
        "text=Acknowledge",
        "#onetrust-accept-btn-handler",
        "[data-testid='uc-accept-all-button']",
    )
    POPUP_CLOSE_SELECTORS = (
        ".modal-popup button[aria-label='Close']",
        ".modal-popup button[aria-label='close']",
        "[role='dialog'] button[aria-label='Close']",
        "[role='dialog'] button[aria-label='close']",
        ".newsletter-popup button[aria-label='Close']",
        ".newsletter-popup button[aria-label='close']",
        ".fancybox-close-small",
        "text=No Thanks",
        "text=Close",
    )
    REMOVABLE_BLOCKER_SELECTORS = (
        ".cky-consent-container",
        ".cky-modal",
        ".cky-overlay",
        "#attentive_creative",
        "iframe[src*='attn.tv']",
        ".newsletter-popup",
        ".modal-popup._show",
    )
    MAX_VIEWPORT_CAPTURE_HEIGHT = 12000

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
        self._auto_accept_cookies = self._should_auto_accept_cookies(steps)

        steps_executed: list[StepExecution] = []
        screenshot_paths: list[str] = []

        failure_info: Optional[FailureInfo] = None
        failed_step_details: Optional[FailedStepDetails] = None

        for step_index, step in enumerate(steps):
            try:
                step_screenshot_path = self._execute_single_step(
                    page=page,
                    step=step,
                    screenshots_dir=screenshots_dir,
                    screenshot_paths=screenshot_paths,
                )
                steps_executed.append(
                    StepExecution(
                        step_index=step_index,
                        step=step,
                        status="ok",
                        page_url=page.url,
                        screenshot_path=step_screenshot_path,
                    )
                )
            except Exception as e:
                tb = traceback.format_exc()
                exc_type = type(e).__name__
                # If a step fails, capture the page exactly as it failed so the
                # user can inspect the visible browser state.
                failure_shot_path = screenshots_dir / f"failure_step_{step_index}_{safe_filename(step.action.value)}.png"
                failure_screenshot_str: Optional[str] = None
                try:
                    self._capture_full_page_screenshot(page, failure_shot_path)
                    failure_screenshot_str = str(failure_shot_path)
                    screenshot_paths.append(failure_screenshot_str)
                except Exception:
                    # If screenshot fails, still proceed with error reporting.
                    pass

                failure_info = FailureInfo(
                    error_message=str(e),
                    exception_type=exc_type,
                    stack_trace=tb,
                    page_url_at_failure=page.url,
                    failure_screenshot_paths=[failure_screenshot_str] if failure_screenshot_str else [],
                )
                steps_executed.append(
                    StepExecution(
                        step_index=step_index,
                        step=step,
                        status="failed",
                        page_url=page.url,
                        screenshot_path=failure_screenshot_str,
                        error_message=str(e),
                    )
                )
                failed_step_details = FailedStepDetails(
                    step_index=step_index,
                    step=step,
                    error_message=str(e),
                    page_url=page.url,
                    screenshot_path=failure_screenshot_str,
                )

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
            failed_step=failed_step_details,
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
    ) -> Optional[str]:
        """
        Execute one validated test step.

        Returns:
        - screenshot path for `screenshot` steps
        - None for other step types
        """

        if step.action == StepAction.goto:
            page.goto(step.url, wait_until="load", timeout=step.timeout_ms)
            self._wait_after_page_change(page, timeout_ms=step.timeout_ms)
            return None

        if step.action == StepAction.click:
            locator = self._resolve_click_target(page, step.selector)
            locator.click(timeout=step.timeout_ms)
            self._wait_after_page_change(page, timeout_ms=step.timeout_ms)
            return None

        if step.action == StepAction.fill:
            if step.selector and step.selector.startswith("text="):
                raise ValueError("fill does not support `text=` selectors. Use a CSS selector for inputs.")
            if not step.selector:
                raise ValueError("fill requires `selector`.")
            page.locator(step.selector).fill(step.text or "", timeout=step.timeout_ms)
            page.wait_for_timeout(150)
            return None

        if step.action == StepAction.press:
            # Focus is optional. If a selector is given, click it first.
            if step.selector:
                locator = self._resolve_click_target(page, step.selector)
                locator.click(timeout=step.timeout_ms)
            page.keyboard.press(step.key, timeout=step.timeout_ms)
            self._wait_after_page_change(page, timeout_ms=step.timeout_ms)
            return None

        if step.action == StepAction.assert_text:
            if not step.selector:
                raise ValueError("assert_text requires `selector`.")
            self._assert_text(page=page, step=step)
            return None

        if step.action == StepAction.screenshot:
            name = step.screenshot_name or "step_screenshot"
            file_path = screenshots_dir / f"{safe_filename(name)}.png"
            # Always store a full-page screenshot so reports include footer/content
            # below the initial viewport.
            self._capture_full_page_screenshot(page, file_path)
            screenshot_path = str(file_path)
            screenshot_paths.append(screenshot_path)
            return screenshot_path

        raise ValueError(f"Unknown action: {step.action}")

    def _wait_after_page_change(self, page: Page, timeout_ms: int) -> None:
        """
        Small deterministic wait helper used after steps that may trigger new
        content, navigation, or async UI updates.
        """

        self._wait_for_page_ready(page, timeout_ms=timeout_ms)

    def _wait_for_page_ready(self, page: Page, timeout_ms: int) -> None:
        """
        Best-effort page settle helper.

        This waits for normal browser load states and then gives images/fonts a
        short chance to finish so screenshots look closer to a real user view.
        """

        try:
            page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 5000))
        except Exception:
            pass

        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 3000))
        except Exception:
            pass

        try:
            page.evaluate(
                """async (timeoutMs) => {
                    const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                    const withTimeout = async (promise) => {
                        await Promise.race([promise, wait(timeoutMs)]);
                    };

                    try {
                        if (document.fonts && document.fonts.ready) {
                            await withTimeout(document.fonts.ready);
                        }
                    } catch (err) {
                        // Ignore font readiness problems.
                    }

                    try {
                        const pendingImages = Array.from(document.images || [])
                            .filter((img) => !img.complete)
                            .slice(0, 50)
                            .map(
                                (img) =>
                                    new Promise((resolve) => {
                                        img.addEventListener("load", resolve, { once: true });
                                        img.addEventListener("error", resolve, { once: true });
                                        setTimeout(resolve, timeoutMs);
                                    })
                            );
                        if (pendingImages.length) {
                            await withTimeout(Promise.all(pendingImages));
                        }
                    } catch (err) {
                        // Ignore image readiness problems.
                    }
                }""",
                min(timeout_ms, 2000),
            )
        except Exception:
            pass

        # Give the browser a short extra moment for UI updates like carousels
        # or delayed hydration after the main load event.
        page.wait_for_timeout(250)

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
        2) Playwright text lookup for the expected text itself
        3) full-page DOM/body/footer text search
        4) scroll and retry for dynamic or below-the-fold content
        """

        selector = step.selector or ""
        expected = step.expected_text or ""
        mode = step.assertion_mode

        deadline = time.monotonic() + (step.timeout_ms / 1000.0)
        last_locator_text: Optional[str] = None
        attempt = 0

        # Retry across the timeout window because some homepage sections mount
        # after hydration, carousel init, or scrolling into view.
        while time.monotonic() < deadline:
            remaining_ms = max(int((deadline - time.monotonic()) * 1000), 200)
            last_locator_text = self._try_get_assert_text(
                page,
                selector=selector,
                timeout_ms=min(remaining_ms, 1200),
            )
            if last_locator_text is not None and self._text_matches(
                actual=last_locator_text,
                expected=expected,
                mode=mode,
            ):
                return

            if self._find_expected_text_with_playwright(
                page=page,
                expected=expected,
                mode=mode,
                timeout_ms=min(remaining_ms, 1200),
            ):
                return

            if self._find_text_anywhere_on_page(
                page,
                expected=expected,
                mode=mode,
                timeout_ms=min(remaining_ms, 1200),
            ):
                return

            self._scroll_intelligently_for_text(page, selector=selector, expected=expected)
            page.wait_for_timeout(250)
            attempt += 1

        actual_preview = last_locator_text if last_locator_text is not None else "<locator text not found>"
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

    def _find_expected_text_with_playwright(self, page: Page, expected: str, mode: str, timeout_ms: int) -> bool:
        """
        Ask Playwright to find the expected text directly.

        This is especially useful when the planner chose a broad selector like
        `body`, but the real text lives in a specific button, banner, or card
        that is already present in the DOM.
        """

        try:
            locator = page.get_by_text(expected, exact=False).first
            if locator.count() < 1:
                return False
        except Exception:
            return False

        try:
            locator.scroll_into_view_if_needed(timeout=timeout_ms)
        except Exception:
            pass

        try:
            actual = locator.inner_text(timeout=timeout_ms).strip()
            if actual and self._text_matches(actual=actual, expected=expected, mode=mode):
                return True
        except Exception:
            pass

        try:
            text_content = locator.text_content(timeout=timeout_ms)
        except Exception:
            text_content = None

        return bool(text_content) and self._text_matches(actual=text_content, expected=expected, mode=mode)

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

        Some sites do not use the normal browser scroll root, so a naive
        `page.screenshot(full_page=True)` can miss content. We first prepare the
        page like a real user would experience it, then capture the whole height
        by resizing the viewport to the content size. If that is too tall for one
        shot, we fall back to stitching viewport images together.
        """

        metrics_before = self._get_page_metrics(page)
        original_y = metrics_before["current_y"]
        original_viewport = getattr(page, "viewport_size", None) or {
            "width": metrics_before["viewport_width"],
            "height": metrics_before["viewport_height"],
        }

        try:
            self._prepare_page_for_full_page_capture(page)
            metrics = self._get_page_metrics(page)

            capture_width = max(metrics["content_width"], original_viewport["width"])
            capture_height = max(metrics["scroll_height"], metrics["viewport_height"])

            if capture_height <= self.MAX_VIEWPORT_CAPTURE_HEIGHT:
                self._capture_with_resized_viewport(
                    page=page,
                    file_path=file_path,
                    width=capture_width,
                    height=capture_height,
                )
            else:
                self._capture_with_stitching(page=page, file_path=file_path, metrics=metrics)
        finally:
            try:
                if hasattr(page, "set_viewport_size"):
                    page.set_viewport_size(original_viewport)
            except Exception:
                pass

            self._scroll_page(page, y=original_y, scroll_root=metrics_before["scroll_root"])
            page.wait_for_timeout(150)

    def _prepare_page_for_full_page_capture(self, page: Page) -> None:
        self._wait_for_page_ready(page, timeout_ms=5000)

        previous_height = 0
        for _ in range(3):
            self._dismiss_blocking_overlays(page)
            self._wait_for_page_ready(page, timeout_ms=2500)

            metrics = self._get_page_metrics(page)
            step_size = max(metrics["viewport_height"] - 120, 300)
            for position in range(0, max(metrics["scroll_height"], 1), step_size):
                self._scroll_page(page, y=position, scroll_root=metrics["scroll_root"])
                page.wait_for_timeout(180)

            self._scroll_page(page, y=metrics["scroll_height"], scroll_root=metrics["scroll_root"])
            page.wait_for_timeout(350)
            self._wait_for_page_ready(page, timeout_ms=2000)

            refreshed_metrics = self._get_page_metrics(page)
            if refreshed_metrics["scroll_height"] <= previous_height + 80:
                break
            previous_height = refreshed_metrics["scroll_height"]

        final_metrics = self._get_page_metrics(page)
        self._scroll_to_true_top(page)
        self._dismiss_blocking_overlays(page)
        self._wait_for_page_ready(page, timeout_ms=2000)

    def _dismiss_blocking_overlays(self, page: Page) -> None:
        """
        Accept cookies by default and close common blocking overlays before a
        screenshot. The selectors stay intentionally narrow to avoid hiding the
        real page content.
        """

        if hasattr(page, "locator") and self._auto_accept_cookies:
            for selector in self.COOKIE_ACCEPT_SELECTORS:
                try:
                    locator = page.locator(selector).first
                    if locator.count() > 0 and locator.is_visible():
                        locator.click(timeout=1500)
                        page.wait_for_timeout(250)
                except Exception:
                    pass

        if hasattr(page, "locator"):
            for selector in self.POPUP_CLOSE_SELECTORS:
                try:
                    locator = page.locator(selector).first
                    if locator.count() > 0 and locator.is_visible():
                        locator.click(timeout=1500)
                        page.wait_for_timeout(250)
                except Exception:
                    pass

        try:
            page.evaluate(
                """({ selectors, removeCookies }) => {
                    const shouldRemove = (selector) => {
                        if (!removeCookies && selector.startsWith('.cky')) {
                            return false;
                        }
                        return true;
                    };

                    for (const selector of selectors) {
                        if (!shouldRemove(selector)) {
                            continue;
                        }
                        for (const node of document.querySelectorAll(selector)) {
                            node.remove();
                        }
                    }
                }""",
                {
                    "selectors": list(self.REMOVABLE_BLOCKER_SELECTORS),
                    "removeCookies": self._auto_accept_cookies,
                },
            )
        except Exception:
            pass

    def _should_auto_accept_cookies(self, steps: list[TestStep]) -> bool:
        """
        Auto-accept cookies unless the planned test appears to explicitly care
        about cookie/privacy UI.
        """

        for step in steps:
            text_parts = [
                step.selector or "",
                step.text or "",
                step.expected_text or "",
                step.screenshot_name or "",
            ]
            combined = " ".join(text_parts).lower()
            if any(word in combined for word in self.COOKIE_SIGNAL_WORDS):
                return False
        return True

    def _capture_with_resized_viewport(self, page: Page, file_path: Path, width: int, height: int) -> None:
        if hasattr(page, "set_viewport_size"):
            page.set_viewport_size({"width": int(width), "height": int(height)})
            page.wait_for_timeout(500)
            self._wait_for_page_ready(page, timeout_ms=1500)
        self._scroll_to_true_top(page)
        page.wait_for_timeout(150)
        page.screenshot(path=str(file_path), full_page=False)

    def _capture_with_stitching(self, page: Page, file_path: Path, metrics: dict[str, int | str]) -> None:
        """
        Fallback for very tall pages when one giant viewport would be too large.
        """

        viewport_height = int(metrics["viewport_height"])
        capture_width = int(metrics["content_width"])
        scroll_height = int(metrics["scroll_height"])
        scroll_root = str(metrics["scroll_root"])

        if hasattr(page, "set_viewport_size"):
            page.set_viewport_size({"width": int(capture_width), "height": int(viewport_height)})
            page.wait_for_timeout(300)

        stitched = Image.new("RGB", (capture_width, scroll_height), "white")
        y = 0
        while y < scroll_height:
            self._scroll_page(page, y=y, scroll_root=scroll_root)
            page.wait_for_timeout(200)
            screenshot_bytes = page.screenshot(full_page=False)
            slice_image = Image.open(BytesIO(screenshot_bytes)).convert("RGB")

            remaining_height = scroll_height - y
            crop_height = min(slice_image.height, remaining_height)
            if crop_height != slice_image.height:
                slice_image = slice_image.crop((0, 0, slice_image.width, crop_height))

            stitched.paste(slice_image, (0, y))
            y += crop_height

        stitched.save(file_path)

    def _scroll_page(self, page: Page, y: int, scroll_root: str) -> None:
        try:
            page.evaluate(
                """({ y, scrollRoot }) => {
                    const nextY = Math.max(0, Math.floor(y || 0));
                    if (scrollRoot === "body" && document.body) {
                        document.body.scrollTop = nextY;
                    } else {
                        document.documentElement.scrollTop = nextY;
                        window.scrollTo(0, nextY);
                    }
                }""",
                {"y": y, "scrollRoot": scroll_root},
            )
        except Exception:
            pass

    def _scroll_to_true_top(self, page: Page) -> None:
        try:
            page.evaluate(
                """() => {
                    if (document.body) {
                        document.body.scrollTop = 0;
                    }
                    if (document.documentElement) {
                        document.documentElement.scrollTop = 0;
                    }
                    window.scrollTo(0, 0);
                }"""
            )
        except Exception:
            pass

    def _get_page_metrics(self, page: Page) -> dict[str, int | str]:
        try:
            return page.evaluate(
                """() => {
                    const body = document.body;
                    const doc = document.documentElement;
                    const bodyScrollHeight = body ? body.scrollHeight : 0;
                    const docScrollHeight = doc ? doc.scrollHeight : 0;
                    const scrollRoot =
                        bodyScrollHeight > docScrollHeight + 100 ? "body" : "documentElement";
                    const currentY =
                        scrollRoot === "body"
                            ? Math.floor(body ? body.scrollTop : 0)
                            : Math.floor((window.scrollY || (doc ? doc.scrollTop : 0)) || 0);
                    const scrollHeight = Math.max(
                        bodyScrollHeight,
                        docScrollHeight,
                        body ? body.offsetHeight : 0,
                        doc ? doc.offsetHeight : 0,
                    );
                    const contentWidth = Math.max(
                        body ? body.scrollWidth : 0,
                        doc ? doc.scrollWidth : 0,
                        body ? body.clientWidth : 0,
                        doc ? doc.clientWidth : 0,
                        Math.floor(window.innerWidth || 0),
                    );

                    return {
                        scroll_root: scrollRoot,
                        current_y: currentY,
                        viewport_width: Math.floor(window.innerWidth || 1280),
                        viewport_height: Math.floor(window.innerHeight || 800),
                        scroll_height: Math.floor(scrollHeight || 0),
                        content_width: Math.floor(contentWidth || 1280),
                    };
                }"""
            )
        except Exception:
            # Safe fallback if page metrics are unavailable for some reason.
            return {
                "scroll_root": "documentElement",
                "current_y": 0,
                "viewport_width": 1280,
                "viewport_height": 800,
                "scroll_height": 2000,
                "content_width": 1280,
            }

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

