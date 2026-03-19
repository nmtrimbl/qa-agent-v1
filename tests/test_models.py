import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

# Ensure imports like `from models.test_step import ...` work under pytest.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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

