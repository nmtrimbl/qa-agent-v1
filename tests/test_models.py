import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

# Ensure imports like `from models.test_step import ...` work under pytest.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from models.test_report import FailedStepDetails, StepExecution, TargetElementDetails, TestReport
from models.test_step import StepAction, TestStep


def test_goto_requires_url():
    with pytest.raises(ValidationError):
        TestStep(action=StepAction.goto)


def test_click_requires_selector():
    with pytest.raises(ValidationError):
        TestStep(action=StepAction.click, selector=None)


def test_click_allows_semantic_candidates_without_exact_selector():
    step = TestStep(
        action=StepAction.click,
        intent="login",
        candidate_labels=["Login", "Sign In", "Account"],
        menu_hints=["Account"],
    )
    assert step.intent == "login"
    assert "Sign In" in step.candidate_labels


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
        resolution_notes=["clicked button role by label Sign In"],
        memory_hint_ids_used=["global:login_account_icon"],
        target_element=TargetElementDetails(
            tag_name="a",
            text="Sign In",
            outer_html='<a title="Sign In">Sign In</a>',
        ),
    )
    assert execution.step_index == 0
    assert execution.page_url == "https://example.com"
    assert execution.screenshot_path == "/tmp/example.png"
    assert execution.memory_hint_ids_used == ["global:login_account_icon"]
    assert execution.target_element is not None
    assert execution.target_element.tag_name == "a"


def test_step_execution_can_store_filled_text():
    step = TestStep(action=StepAction.fill, selector="input[name='q']", text="pool filter")
    execution = StepExecution(
        step_index=1,
        step=step,
        status="ok",
        filled_text="pool filter",
        target_element=TargetElementDetails(
            tag_name="input",
            text="",
            outer_html="<input name='q' />",
        ),
    )
    assert execution.filled_text == "pool filter"
    assert execution.target_element is not None
    assert execution.target_element.tag_name == "input"


def test_report_can_include_failed_step_details():
    step = TestStep(action=StepAction.click, selector="text=Login")
    report = TestReport(
        run_id="run-1",
        url="https://example.com",
        final_url="https://example.com/login",
        overall_status="FAIL",
        test_summary="The login check failed.",
        likely_failure_cause="The login button was not found.",
        reproduction_notes="Open the login page and try the Login button again.",
        severity_guess="medium",
        memory_consulted=True,
        memory_domain="example.com",
        domain_hint_ids_consulted=["domain:example.com:login"],
        global_hint_ids_consulted=["global:login"],
        fallback_paths_used=["opened menu hint Account via clicked partial text candidate Account"],
        planner_output=[{"action": "goto", "url": "https://example.com"}],
        failed_step=FailedStepDetails(
            step_index=1,
            step=step,
            error_message="Button not found",
            page_url="https://example.com/login",
            screenshot_path="/tmp/failure.png",
            resolution_notes=["opened menu hint Account via clicked partial text candidate Account"],
            memory_hint_ids_used=["global:login"],
        ),
    )
    assert report.failed_step is not None
    assert report.final_url == "https://example.com/login"
    assert report.test_summary == "The login check failed."
    assert report.severity_guess == "medium"
    assert report.memory_consulted is True
    assert report.planner_output == [{"action": "goto", "url": "https://example.com"}]
    assert report.failed_step.step_index == 1
    assert report.failed_step.error_message == "Button not found"

