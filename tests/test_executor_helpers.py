import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from browser.executor import BrowserExecutor
from models.test_step import StepAction, TestStep


def test_normalize_text_handles_footer_symbols_and_spacing():
    raw = "Copyright  ©   2025   Doheny’s   ®"
    normalized = BrowserExecutor._normalize_text(raw)
    assert normalized == "copyright©2025 doheny's®"


def test_text_matches_handles_minor_footer_formatting_differences():
    executor = BrowserExecutor(artifacts_dir="artifacts")
    actual = "Copyright © 2025 Doheny's®"
    expected = "Copyright © 2025 Doheny’s ®"
    assert executor._text_matches(actual=actual, expected=expected, mode="contains") is True


def test_footer_detection_uses_expected_text_and_selector():
    executor = BrowserExecutor(artifacts_dir="artifacts")
    assert executor._looks_like_footer_check(selector="footer", expected="") is True
    assert executor._looks_like_footer_check(selector="div.notice", expected="Copyright © 2025") is True
    assert executor._looks_like_footer_check(selector="main h1", expected="Welcome") is False


class FakePage:
    def __init__(self):
        self.evaluate_calls = []
        self.wait_calls = []
        self.screenshot_calls = []
        self.goto_calls = []
        self.url = "about:blank"
        self.viewport_size = {"width": 1280, "height": 800}
        self.viewport_size_calls = []

    def goto(self, url, wait_until, timeout):
        self.goto_calls.append({"url": url, "wait_until": wait_until, "timeout": timeout})
        self.url = url

    def evaluate(self, script, arg=None):
        self.evaluate_calls.append(script)
        if "scroll_root" in script:
            return {
                "scroll_root": "body",
                "current_y": 140,
                "viewport_width": self.viewport_size["width"],
                "viewport_height": self.viewport_size["height"],
                "scroll_height": 2200,
                "content_width": 1280,
            }
        return None

    def wait_for_timeout(self, ms):
        self.wait_calls.append(ms)

    def wait_for_load_state(self, state, timeout):
        self.wait_calls.append((state, timeout))

    def set_viewport_size(self, viewport):
        self.viewport_size_calls.append(viewport)
        self.viewport_size = dict(viewport)

    def screenshot(self, *, path, full_page):
        self.screenshot_calls.append({"path": path, "full_page": full_page})

    def locator(self, selector):
        return FakeLocator()


class FakeLocator:
    @property
    def first(self):
        return self

    def count(self):
        return 0

    def is_visible(self):
        return False

    def click(self, timeout):
        return None


def test_capture_full_page_screenshot_scrolls_and_restores_position(tmp_path):
    executor = BrowserExecutor(artifacts_dir="artifacts")
    page = FakePage()
    output_path = tmp_path / "full-page.png"

    executor._capture_full_page_screenshot(page, output_path)

    assert page.screenshot_calls == [{"path": str(output_path), "full_page": False}]
    assert page.viewport_size_calls[0] == {"width": 1280, "height": 2200}
    assert page.viewport_size_calls[-1] == {"width": 1280, "height": 800}


class FakeSession:
    def __init__(self, page):
        self.page = page
        self.console_errors = []


def test_execute_always_adds_final_report_screenshot(tmp_path):
    executor = BrowserExecutor(artifacts_dir=tmp_path)
    page = FakePage()
    session = FakeSession(page)

    result = executor.execute(
        session=session,
        url="https://example.com",
        steps=[TestStep(action=StepAction.goto, url="https://example.com")],
        run_id="run-123",
    )

    assert result.success is True
    assert any(path.endswith("final_report_full_page.png") for path in result.screenshot_paths)
