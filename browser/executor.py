from __future__ import annotations

from contextlib import contextmanager
import io
import re
import traceback
import unicodedata
from pathlib import Path
import time
from typing import Optional

from PIL import Image as _PILImage


@contextmanager
def _nullctx():
    """No-op context manager used as a placeholder when page is unavailable."""
    yield

from playwright.sync_api import Page
from pydantic import BaseModel

from browser.browser_session import BrowserSession
from models.test_report import ConsoleError, FailedStepDetails, StepExecution, TargetElementDetails
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


class StepRunInfo(BaseModel):
    screenshot_path: Optional[str] = None
    resolution_notes: list[str] = []
    memory_hint_ids_used: list[str] = []
    filled_text: Optional[str] = None
    target_element: Optional[TargetElementDetails] = None


class ClickAttemptResult(BaseModel):
    clicked: bool
    target_element: Optional[TargetElementDetails] = None


class ClickResolution(BaseModel):
    note: str
    target_element: Optional[TargetElementDetails] = None


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
        # Only close overlays that are clearly non-content (newsletters, cookie
        # modals, lightboxes). Avoid broad selectors like [role='dialog'] or
        # text=Close that also match ATC confirmation popups and other legitimate
        # product interactions the user may want to see in a screenshot.
        ".modal-popup button[aria-label='Close']",
        ".modal-popup button[aria-label='close']",
        ".newsletter-popup button[aria-label='Close']",
        ".newsletter-popup button[aria-label='close']",
        ".fancybox-close-small",
        "text=No Thanks",
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
    MIN_NAVIGATION_TIMEOUT_MS = 15000

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
            url_before = page.url
            try:
                step_run_info = self._execute_single_step(
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
                        page_url_before=url_before,
                        page_url=page.url,
                        screenshot_path=step_run_info.screenshot_path,
                        filled_text=step_run_info.filled_text,
                        resolution_notes=step_run_info.resolution_notes,
                        memory_hint_ids_used=step_run_info.memory_hint_ids_used,
                        target_element=step_run_info.target_element,
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
                    # Use Playwright's native full_page=True (scroll-stitch) rather than
                    # _capture_full_page_screenshot (viewport-resize). Scroll-stitch
                    # preserves the viewport size so fixed overlays (popups, modals)
                    # render at their correct screen position in the capture.
                    page.screenshot(path=str(failure_shot_path), full_page=True)
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
                        page_url_before=url_before,
                        page_url=page.url,
                        screenshot_path=failure_screenshot_str,
                        error_message=str(e),
                        filled_text=step.text if step.action == StepAction.fill else None,
                        memory_hint_ids_used=list(step.memory_hint_ids),
                        target_element=None,
                    )
                )
                failed_step_details = FailedStepDetails(
                    step_index=step_index,
                    step=step,
                    error_message=str(e),
                    page_url=page.url,
                    screenshot_path=failure_screenshot_str,
                    filled_text=step.text if step.action == StepAction.fill else None,
                    memory_hint_ids_used=list(step.memory_hint_ids),
                    target_element=None,
                )

                # Requirement: stop on failure to keep results clear for beginner MVP.
                break

        # Console errors captured during the run.
        console_errors = session.console_errors

        # On success, attach one final full-page screenshot for the report.
        # On failure we already capture a dedicated failure screenshot, so we
        # skip a second end-of-run full-page capture to avoid long delays.
        if failure_info is None:
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
    ) -> StepRunInfo:
        """
        Execute one validated test step.

        Returns:
        - screenshot path for `screenshot` steps
        - resolution notes describing which deterministic fallback worked
        """

        result = StepRunInfo(memory_hint_ids_used=list(step.memory_hint_ids))

        if step.action == StepAction.goto:
            navigation_timeout_ms = max(step.timeout_ms, self.MIN_NAVIGATION_TIMEOUT_MS)
            # `domcontentloaded` is a safer first navigation gate for slower
            # staging sites. The settle helper below still waits for extra page
            # readiness signals after the initial response arrives.
            page.goto(step.url, wait_until="domcontentloaded", timeout=navigation_timeout_ms)
            self._wait_after_page_change(page, timeout_ms=navigation_timeout_ms)
            return result

        if step.action == StepAction.click:
            click_result = self._click_with_fallbacks(page=page, step=step)
            if click_result:
                result.resolution_notes.append(click_result.note)
                result.target_element = click_result.target_element
            self._wait_after_page_change(page, timeout_ms=step.timeout_ms)
            return result

        if step.action == StepAction.fill:
            if step.selector and step.selector.startswith("text="):
                raise ValueError("fill does not support `text=` selectors. Use a CSS selector for inputs.")
            if not step.selector:
                raise ValueError("fill requires `selector`.")
            fill_target = page.locator(step.selector).first
            result.target_element = self._describe_locator_element(fill_target)
            result.filled_text = step.text or ""
            fill_target.fill(step.text or "", timeout=step.timeout_ms)
            page.wait_for_timeout(150)
            return result

        if step.action == StepAction.press:
            # Focus is optional. If a selector is given, click it first.
            if step.selector:
                click_result = self._click_with_fallbacks(page=page, step=step)
                if click_result:
                    result.resolution_notes.append(click_result.note)
                    result.target_element = click_result.target_element
            page.keyboard.press(step.key)
            self._wait_after_page_change(page, timeout_ms=step.timeout_ms)
            return result

        if step.action == StepAction.assert_text:
            resolution_notes = self._assert_text(page=page, step=step)
            result.resolution_notes.extend(resolution_notes)
            return result

        if step.action == StepAction.screenshot:
            name = step.screenshot_name or "step_screenshot"
            file_path = screenshots_dir / f"{safe_filename(name)}.png"
            # Capture only the current viewport — this is what the user actually
            # sees at this moment in the test. Avoid full_page=True because
            # Playwright implements it by temporarily expanding the viewport height
            # to match the full document, which repositions sticky/fixed headers,
            # triggers accessibility overlays, and causes layout reflow. Popups and
            # modals are viewport-relative and will be captured correctly here.
            # Do not dismiss overlays or scroll — either would close the popup or
            # modal that this step may be verifying.
            self._wait_for_page_ready(page, timeout_ms=5000)
            page.screenshot(path=str(file_path))
            result.screenshot_path = str(file_path)
            screenshot_paths.append(result.screenshot_path)
            return result

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

        if selector.startswith("text="):
            text_value = selector[len("text=") :]
            return page.get_by_text(text_value, exact=False).first

        return page.locator(selector).first

    def _get_assert_text(self, page: Page, selector: str, timeout_ms: int) -> str:
        if selector.startswith("text="):
            text_value = selector[len("text=") :]
            # Use first match; MVP keeps this simple/reliable.
            locator = page.get_by_text(text_value, exact=True).first
        else:
            locator = page.locator(selector).first

        return locator.inner_text(timeout=timeout_ms).strip()

    def _assert_text(self, page: Page, step: TestStep) -> list[str]:
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
        resolution_notes: list[str] = []

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
                resolution_notes.append("assert_text matched direct locator")
                return resolution_notes

            if self._find_expected_text_with_playwright(
                page=page,
                expected=expected,
                mode=mode,
                timeout_ms=min(remaining_ms, 1200),
                candidate_labels=step.candidate_labels,
            ):
                resolution_notes.append("assert_text matched Playwright text fallback")
                return resolution_notes

            if self._find_text_anywhere_on_page(
                page,
                expected=expected,
                mode=mode,
                timeout_ms=min(remaining_ms, 1200),
                candidate_selectors=step.candidate_selectors,
            ):
                resolution_notes.append("assert_text matched full-page fallback")
                return resolution_notes

            self._scroll_intelligently_for_text(page, selector=selector, expected=expected)
            page.wait_for_timeout(250)

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

    def _find_text_anywhere_on_page(
        self,
        page: Page,
        expected: str,
        mode: str,
        timeout_ms: int,
        candidate_selectors: Optional[list[str]] = None,
    ) -> bool:
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

        for selector in candidate_selectors or []:
            candidate_text = self._safe_inner_text(page.locator(selector).first, timeout_ms=timeout_ms)
            if candidate_text:
                text_candidates.append(candidate_text)

        return any(self._text_matches(actual=text, expected=expected, mode=mode) for text in text_candidates)

    def _find_expected_text_with_playwright(
        self,
        page: Page,
        expected: str,
        mode: str,
        timeout_ms: int,
        candidate_labels: Optional[list[str]] = None,
    ) -> bool:
        """
        Ask Playwright to find the expected text directly.

        This is especially useful when the planner chose a broad selector like
        `body`, but the real text lives in a specific button, banner, or card
        that is already present in the DOM.
        """

        labels_to_try = [expected] + [label for label in (candidate_labels or []) if label != expected]
        for label in labels_to_try:
            is_candidate_label = label != expected
            try:
                locator = page.get_by_text(label, exact=False).first
                if locator.count() < 1:
                    continue
            except Exception:
                continue

            try:
                locator.scroll_into_view_if_needed(timeout=timeout_ms)
            except Exception:
                pass

            try:
                actual = locator.inner_text(timeout=timeout_ms).strip()
                if not actual:
                    raise ValueError("empty")
            except Exception:
                actual = None

            if actual:
                # Candidate labels are acceptable phrasings of the expected text —
                # finding one visible on the page is a match in its own right.
                if is_candidate_label or self._text_matches(actual=actual, expected=expected, mode=mode):
                    return True

            try:
                text_content = locator.text_content(timeout=timeout_ms)
            except Exception:
                text_content = None

            if text_content:
                if is_candidate_label or self._text_matches(actual=text_content, expected=expected, mode=mode):
                    return True

        return False

    def _click_with_fallbacks(self, page: Page, step: TestStep) -> Optional[ClickResolution]:
        """
        Deterministic click resolution.

        The order is fixed so the behavior stays explainable:
        1) explicit selector
        2) candidate selectors
        3) semantic label strategies
        4) optional menu hints, then retry
        """

        deadline = time.monotonic() + (step.timeout_ms / 1000.0)
        attempts = self._build_click_attempts(page=page, step=step)
        for locator, note in attempts:
            attempt_timeout_ms = self._remaining_timeout_ms(deadline=deadline, minimum_ms=250)
            if attempt_timeout_ms is None:
                break
            click_attempt = self._try_click_locator(locator, timeout_ms=min(attempt_timeout_ms, 1200), page=page)
            if click_attempt.clicked:
                return ClickResolution(note=note, target_element=click_attempt.target_element)

        for menu_hint in self._normalize_candidate_labels(step.menu_hints):
            attempt_timeout_ms = self._remaining_timeout_ms(deadline=deadline, minimum_ms=250)
            if attempt_timeout_ms is None:
                break
            menu_note = self._open_menu_hint(page=page, label=menu_hint, timeout_ms=min(attempt_timeout_ms, 1200))
            if menu_note:
                attempts = self._build_click_attempts(page=page, step=step)
                for locator, note in attempts:
                    retry_timeout_ms = self._remaining_timeout_ms(deadline=deadline, minimum_ms=250)
                    if retry_timeout_ms is None:
                        break
                    click_attempt = self._try_click_locator(locator, timeout_ms=min(retry_timeout_ms, 1200), page=page)
                    if click_attempt.clicked:
                        return ClickResolution(
                            note=f"{menu_note}; {note}",
                            target_element=click_attempt.target_element,
                        )

        raise AssertionError(
            "Could not resolve click target. "
            f"selector={step.selector!r}, candidate_labels={step.candidate_labels}, "
            f"candidate_selectors={step.candidate_selectors}, menu_hints={step.menu_hints}"
        )

    def _build_click_attempts(self, page: Page, step: TestStep):
        attempts = []

        if step.selector:
            attempts.append((self._resolve_click_target(page, step.selector), f"clicked selector {step.selector}"))

        for selector in step.candidate_selectors:
            attempts.append((self._resolve_click_target(page, selector), f"clicked candidate selector {selector}"))

        for label in self._normalize_candidate_labels(step.candidate_labels):
            attempts.extend(self._semantic_locators_for_label(page=page, label=label))

        # Remove duplicate notes while preserving order.
        deduped = []
        seen_notes = set()
        for locator, note in attempts:
            if note in seen_notes:
                continue
            deduped.append((locator, note))
            seen_notes.add(note)
        return deduped

    def _semantic_locators_for_label(self, page: Page, label: str):
        css_safe_label = label.replace("'", "\\'")
        return [
            (page.get_by_role("button", name=label, exact=False).first, f"clicked button role by label {label}"),
            (page.get_by_role("link", name=label, exact=False).first, f"clicked link role by label {label}"),
            (page.get_by_role("radio", name=label, exact=False).first, f"clicked radio role by label {label}"),
            (page.get_by_label(label, exact=False).first, f"clicked aria label {label}"),
            (page.locator(f"label:has-text('{css_safe_label}')").first, f"clicked label text candidate {label}"),
            (
                page.locator(
                    f"[aria-label*='{css_safe_label}' i], [title*='{css_safe_label}' i], img[alt*='{css_safe_label}' i]"
                ).first,
                f"clicked aria/title/alt candidate {label}",
            ),
            (page.get_by_text(label, exact=False).first, f"clicked partial text candidate {label}"),
        ]

    def _try_click_locator(self, locator, timeout_ms: int, page: Optional[Page] = None) -> ClickAttemptResult:
        try:
            match_count = locator.count()
            if match_count < 1:
                return ClickAttemptResult(clicked=False)
        except Exception:
            return ClickAttemptResult(clicked=False)

        # Some sites render both hidden and visible copies of the same label
        # for desktop/mobile navigation. Prefer visible matches before giving up.
        max_candidates_to_try = min(match_count, 5)
        visible_candidates = []
        fallback_candidates = []

        for index in range(max_candidates_to_try):
            try:
                candidate = locator.nth(index)
            except Exception:
                if index == 0:
                    candidate = locator
                else:
                    break

            try:
                if candidate.is_visible():
                    visible_candidates.append(candidate)
                    continue
            except Exception:
                pass

            fallback_candidates.append(candidate)

        for candidate in visible_candidates + fallback_candidates:
            for click_target in self._expand_click_targets(candidate):
                element_details = self._describe_locator_element(click_target)
                try:
                    click_target.scroll_into_view_if_needed(timeout=timeout_ms)
                except Exception:
                    pass

                url_before_click = page.url if page else None
                try:
                    # Use expect_navigation so Playwright treats frame teardown as
                    # expected rather than an error. The timeout here is a ceiling —
                    # if no navigation starts within it we fall through to the
                    # TimeoutError handler below.
                    with page.expect_navigation(wait_until="commit", timeout=timeout_ms) if page else _nullctx():
                        click_target.click(timeout=timeout_ms)
                    return ClickAttemptResult(clicked=True, target_element=element_details)
                except Exception as exc:
                    exc_str = str(exc).lower()
                    # expect_navigation timed out — no navigation occurred, but the
                    # click itself may have succeeded (modal open, AJAX action, etc.).
                    if "timeout" in exc_str and page and page.url == url_before_click:
                        return ClickAttemptResult(clicked=True, target_element=element_details)
                    # Navigation-related errors where the frame was destroyed before
                    # Playwright could confirm the click. Only trust the URL change
                    # for these specific signals to avoid masking real click failures.
                    _nav_signals = (
                        "frame was detached",
                        "framedetached",
                        "execution context was destroyed",
                        "net::err_",
                        "target closed",
                    )
                    if page and page.url != url_before_click and any(s in exc_str for s in _nav_signals):
                        return ClickAttemptResult(clicked=True, target_element=element_details)
                    continue

        return ClickAttemptResult(clicked=False)

    def _expand_click_targets(self, locator) -> list:
        """
        Try a few nearby clickable variants for the matched locator.

        This helps swatch/radio UIs where the visible text sits inside a `<div>`
        or hidden `<input>`, but the actual interactive surface is the wrapping
        `<label>`.
        """

        targets = [locator]

        try:
            label_target = locator.locator("xpath=ancestor-or-self::label[1]").first
            if label_target.count() > 0:
                targets.append(label_target)
        except Exception:
            pass

        deduped = []
        seen_ids = set()
        for target in targets:
            target_id = id(target)
            if target_id in seen_ids:
                continue
            deduped.append(target)
            seen_ids.add(target_id)
        return deduped

    def _describe_locator_element(self, locator) -> Optional[TargetElementDetails]:
        """
        Capture a small structured description of the element before clicking it.

        We do this before the click because the page may navigate immediately
        after interaction, which can detach the element from the DOM.
        """

        try:
            payload = locator.evaluate(
                """(el) => ({
                    tag_name: (el.tagName || '').toLowerCase(),
                    text: ((el.innerText || el.textContent || '')).replace(/\\s+/g, ' ').trim(),
                    outer_html: el.outerHTML || '',
                })"""
            )
        except Exception:
            return None

        if not isinstance(payload, dict):
            return None

        return TargetElementDetails(
            tag_name=str(payload.get("tag_name") or ""),
            text=str(payload.get("text") or ""),
            outer_html=str(payload.get("outer_html") or ""),
        )

    def _open_menu_hint(self, page: Page, label: str, timeout_ms: int) -> Optional[str]:
        for locator, note in self._semantic_locators_for_label(page=page, label=label):
            click_attempt = self._try_click_locator(locator, timeout_ms=timeout_ms, page=page)
            if click_attempt.clicked:
                page.wait_for_timeout(250)
                return f"opened menu hint {label} via {note}"
        return None

    def _remaining_timeout_ms(self, *, deadline: float, minimum_ms: int) -> Optional[int]:
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms < minimum_ms:
            return None
        return remaining_ms

    @staticmethod
    def _normalize_candidate_labels(labels: list[str]) -> list[str]:
        """
        Trim and normalize whitespace in semantic labels before matching.

        Example:
        - `"  Sign In\\n"` becomes `"Sign In"`
        """

        normalized: list[str] = []
        seen: set[str] = set()
        for label in labels:
            clean = re.sub(r"\s+", " ", label or "").strip()
            if not clean or clean in seen:
                continue
            normalized.append(clean)
            seen.add(clean)
        return normalized

    def _scroll_intelligently_for_text(self, page: Page, selector: str, expected: str) -> None:
        """
        Scroll toward likely text location before a second assertion attempt.

        Footer-ish text gets a direct scroll to bottom. Otherwise we do a small
        progressive scroll and then end at the bottom.
        """

        # Never scroll for popup/modal/overlay assertions — scrolling can trigger
        # Alpine.js or JS scroll-listeners that dismiss the overlay we're checking.
        if self._looks_like_popup_check(selector=selector):
            return

        if self._looks_like_footer_check(selector=selector, expected=expected):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(300)
            return

        page.evaluate("window.scrollTo(0, Math.floor(document.body.scrollHeight * 0.5))")
        page.wait_for_timeout(250)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(300)

    def _looks_like_popup_check(self, selector: str) -> bool:
        popup_signals = ("popup", "modal", "overlay", "toast", "notification", "dialog", "alert")
        return any(signal in selector.lower() for signal in popup_signals)

    def _looks_like_footer_check(self, selector: str, expected: str) -> bool:
        footer_signals = ("footer", "contentinfo", "copyright", "all rights reserved", "privacy", "terms", "©", "®")
        combined = f"{selector} {expected}".lower()
        return any(signal in combined for signal in footer_signals)

    def _capture_full_page_screenshot(self, page: Page, file_path: Path) -> None:
        """
        Capture a full-page screenshot showing the page as a user would see it
        after it has fully loaded, with cookie and popup overlays removed.

        Uses scroll-stitch rather than Playwright's full_page=True. Playwright's
        full_page=True works by temporarily expanding the viewport height to match
        document.body.scrollHeight. This causes any CSS using min-height:100vh or
        vh units to recompute — the page layout grows, pushing the footer and other
        bottom sections outside the clipped screenshot bounds. Scroll-stitching
        takes viewport-size screenshots at successive scroll positions and assembles
        them, so no viewport resize occurs and layouts remain stable.
        """
        self._wait_for_page_ready(page, timeout_ms=5000)
        self._dismiss_blocking_overlays(page)

        # Disable CSS transitions and animations before scrolling. Many sites use
        # scroll-reveal patterns where IntersectionObserver adds a class causing an
        # opacity/transform transition. Without this, scrolling back to the top after
        # loading content causes those elements to re-animate to their hidden state,
        # leaving the section backgrounds (e.g. #f8fafc, #fafafa) visible but the
        # content invisible in the screenshot.
        try:
            page.add_style_tag(content=(
                "*, *::before, *::after {"
                "  transition-duration: 0s !important;"
                "  animation-duration: 0s !important;"
                "  animation-delay: 0s !important;"
                "}"
            ))
        except Exception:
            pass

        # Scroll through the page incrementally to trigger IntersectionObserver-based
        # lazy loaders and Alpine.js x-intersect directives (e.g. recommendations
        # blocks, promo sliders). Wait for network to settle at the bottom so that
        # AJAX-driven sections finish fetching before the screenshot pass.
        try:
            viewport_height = (page.viewport_size or {}).get("height", 800)
            position = 0
            while True:
                page_height = page.evaluate("document.body.scrollHeight")
                if position >= page_height:
                    break
                position = min(position + viewport_height, page_height)
                page.evaluate(f"window.scrollTo(0, {position})")
                page.wait_for_timeout(200)
            try:
                page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass
            new_height = page.evaluate("document.body.scrollHeight")
            if new_height > position:
                while True:
                    page_height = page.evaluate("document.body.scrollHeight")
                    if position >= page_height:
                        break
                    position = min(position + viewport_height, page_height)
                    page.evaluate(f"window.scrollTo(0, {position})")
                    page.wait_for_timeout(200)
                try:
                    page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    pass
        except Exception:
            pass

        self._dismiss_blocking_overlays(page)
        self._scroll_stitch_screenshot(page, file_path)

    def _scroll_stitch_screenshot(self, page: Page, file_path: Path) -> None:
        """
        Assemble a full-page image by stitching viewport screenshots taken at
        successive scroll positions.

        Unlike full_page=True (which resizes the viewport), each screenshot here
        is taken at the real viewport dimensions. This keeps vh-based CSS layouts
        stable — elements such as min-height:100vh containers don't recompute,
        so the footer and bottom sections remain at their correct document positions.

        Fixed and sticky elements (headers, success banners, etc.) are captured
        in the first strip only. From the second strip onwards they are hidden via
        visibility:hidden so they don't repeat throughout the stitched image.
        The scroll step equals the full viewport height so there are no gaps or
        overlaps in the assembled content.
        """
        vp = page.viewport_size or {"width": 1280, "height": 800}
        vw = vp.get("width", 1280)
        vh = vp.get("height", 800)

        total_height = 0
        try:
            total_height = page.evaluate("document.body.scrollHeight")
        except Exception:
            pass

        if total_height <= 0:
            try:
                page.screenshot(path=str(file_path))
            except Exception:
                pass
            return

        # Build scroll positions stepping one full viewport at a time. Always
        # include a position that puts the very bottom of the page in view.
        scroll_positions: list[int] = []
        y = 0
        while y < total_height:
            scroll_positions.append(y)
            y += vh
        bottom_y = max(total_height - vh, 0)
        if scroll_positions[-1] < bottom_y:
            scroll_positions.append(bottom_y)

        strips: list[_PILImage.Image] = []
        prev_doc_end = 0
        fixed_hidden = False

        try:
            for i, sy in enumerate(scroll_positions):
                try:
                    page.evaluate(f"window.scrollTo(0, {sy})")
                    page.wait_for_timeout(150)
                except Exception:
                    pass

                # From the second strip onwards hide all fixed/sticky elements so
                # they don't repeat. position:fixed headers and banners are only
                # shown in strip 0 where they appear at the top of the image.
                # position:sticky elements behave the same when scrolled — hiding
                # them prevents duplicates across strips.
                if i == 1 and not fixed_hidden:
                    try:
                        page.evaluate("""
                            window.__stitchFixedEls = [];
                            for (const el of document.querySelectorAll('*')) {
                                const s = window.getComputedStyle(el);
                                if (s.position !== 'fixed' && s.position !== 'sticky') continue;
                                if (s.display === 'none') continue;
                                window.__stitchFixedEls.push({
                                    el,
                                    origStyle: el.getAttribute('style') || ''
                                });
                                el.style.setProperty('visibility', 'hidden', 'important');
                            }
                        """)
                        fixed_hidden = True
                    except Exception:
                        pass

                try:
                    img_bytes = page.screenshot()
                    img = _PILImage.open(io.BytesIO(img_bytes))
                except Exception:
                    continue

                doc_start = max(sy, prev_doc_end)
                doc_end = min(sy + vh, total_height)

                if doc_start >= doc_end:
                    continue

                sr_start = max(doc_start - sy, 0)
                sr_end = min(doc_end - sy, img.height)

                if sr_start >= sr_end:
                    continue

                strip = img.crop((0, sr_start, vw, sr_end))
                strips.append(strip)
                prev_doc_end = doc_end

        finally:
            if fixed_hidden:
                try:
                    page.evaluate("""
                        for (const {el, origStyle} of window.__stitchFixedEls || []) {
                            if (origStyle) {
                                el.setAttribute('style', origStyle);
                            } else {
                                el.removeAttribute('style');
                            }
                        }
                        delete window.__stitchFixedEls;
                    """)
                except Exception:
                    pass

        if not strips:
            try:
                page.screenshot(path=str(file_path))
            except Exception:
                pass
            return

        total_h = sum(s.height for s in strips)
        final_img = _PILImage.new("RGB", (vw, total_h), (255, 255, 255))
        offset = 0
        for strip in strips:
            final_img.paste(strip, (0, offset))
            offset += strip.height
        final_img.save(str(file_path))

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

