import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

# Ensure imports like `from models.test_step import ...` work under pytest.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from models.test_report import FailedStepDetails, StepExecution, TestReport
from models.test_step import StepAction, TestStep


def test_goto_requires_url():
    with pytest.raises(ValidationError):
        TestStep(action=StepAction.goto)


def test_click_requires_selector():
    with pytest.raises(ValidationError):
        TestStep(action=StepAction.click, selector=None)


def test_fill_requires_text():
    with pytest.raises(ValidationError):
        TestStep(action=StepAction.fill, selector="input[name='q']", text=None)


def test_press_requires_key():
    with pytest.raises(ValidationError):
        TestStep(action=StepAction.press, key=None)


def test_assert_text_requires_expected_text():
    with pytest.raises(ValidationError):
        TestStep(action=StepAction.assert_text, selector="h1", expected_text=None)


def test_step_execution_supports_page_url_and_screenshot_path():
    step = TestStep(action=StepAction.goto, url="https://example.com")
    execution = StepExecution(
        step_index=0,
        step=step,
        status="ok",
        page_url="https://example.com",
        screenshot_path="/tmp/example.png",
    )
    assert execution.step_index == 0
    assert execution.page_url == "https://example.com"
    assert execution.screenshot_path == "/tmp/example.png"


def test_report_can_include_failed_step_details():
    step = TestStep(action=StepAction.click, selector="text=Login")
    report = TestReport(
        run_id="run-1",
        url="https://example.com",
        overall_status="FAIL",
        failed_step=FailedStepDetails(
            step_index=1,
            step=step,
            error_message="Button not found",
            page_url="https://example.com/login",
            screenshot_path="/tmp/failure.png",
        ),
    )
    assert report.failed_step is not None
    assert report.failed_step.step_index == 1
    assert report.failed_step.error_message == "Button not found"

