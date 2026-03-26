import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.test_planner import _canonicalize_steps
from models.test_step import StepAction, TestStep


def test_canonicalize_moves_comma_separated_selector_to_candidate_selectors():
    steps = [
        TestStep(action=StepAction.goto, url="https://example.com"),
        TestStep(
            action=StepAction.click,
            selector="input[type='search'], input[name='q'], input[placeholder*='Search'], input[aria-label*='Search']",
        ),
    ]

    normalized = _canonicalize_steps(url="https://example.com", steps=steps)
    click_step = normalized[1]

    assert click_step.selector is None
    assert click_step.candidate_selectors == [
        "input[type='search']",
        "input[name='q']",
        "input[placeholder*='Search']",
        "input[aria-label*='Search']",
    ]


def test_canonicalize_keeps_fill_selector_as_single_required_field():
    steps = [
        TestStep(action=StepAction.goto, url="https://example.com"),
        TestStep(
            action=StepAction.fill,
            selector="input[type='search'], input[name='q']",
            text="pool filter",
        ),
    ]

    normalized = _canonicalize_steps(url="https://example.com", steps=steps)
    fill_step = normalized[1]

    assert fill_step.selector == "input[type='search'], input[name='q']"
    assert fill_step.candidate_selectors == []
