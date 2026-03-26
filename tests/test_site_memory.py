import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from models.test_step import StepAction, TestStep
from models.test_report import StepExecution
from utils.site_memory import (
    GLOBAL_MEMORY_DOMAIN,
    build_planner_memory_views,
    load_domain_memory,
    load_global_memory,
    normalize_domain,
    record_feedback,
    update_memory_strength_from_steps,
)


def test_normalize_domain_handles_urls():
    assert normalize_domain("https://staging4.doheny.com/account/login") == "staging4.doheny.com"


def test_record_feedback_creates_domain_and_global_memory(tmp_path):
    domain_memory, global_memory = record_feedback(
        url="https://staging4.doheny.com/",
        run_id="run-1",
        issue="The system looked for Login text only.",
        correction="Open the account icon, then click Sign In.",
        intent="login",
        failed_step={
            "step_index": 1,
            "step": {"action": "click", "selector": "text=Login"},
        },
        artifacts_dir=tmp_path,
    )

    assert domain_memory.domain == "staging4.doheny.com"
    assert global_memory.domain == GLOBAL_MEMORY_DOMAIN
    assert domain_memory.feedback_history
    assert global_memory.feedback_history
    assert domain_memory.hints
    assert global_memory.hints


def test_update_memory_strength_from_steps_adjusts_hint_scores(tmp_path):
    record_feedback(
        url="https://staging4.doheny.com/",
        run_id="run-1",
        issue="Login was behind an icon.",
        correction="Open account, then click Sign In.",
        intent="login",
        failed_step=None,
        artifacts_dir=tmp_path,
    )

    domain_memory = load_domain_memory(url="https://staging4.doheny.com/", artifacts_dir=tmp_path)
    hint_id = domain_memory.hints[0].id
    original_strength = domain_memory.hints[0].strength

    step = TestStep(
        action=StepAction.click,
        candidate_labels=["Sign In"],
        memory_hint_ids=[hint_id],
    )
    executed_step = StepExecution(step_index=0, step=step, status="ok")
    update_memory_strength_from_steps(
        url="https://staging4.doheny.com/",
        executed_steps=[executed_step],
        artifacts_dir=tmp_path,
    )

    refreshed_memory = load_domain_memory(url="https://staging4.doheny.com/", artifacts_dir=tmp_path)
    assert refreshed_memory.hints[0].strength > original_strength


def test_build_planner_memory_views_returns_recent_feedback_and_hints(tmp_path):
    record_feedback(
        url="https://example.com/",
        run_id="run-1",
        issue="Login is under the account icon.",
        correction="Open Account, then select Sign In.",
        intent="login",
        failed_step=None,
        artifacts_dir=tmp_path,
    )

    domain_view, global_view = build_planner_memory_views(url="https://example.com/", artifacts_dir=tmp_path)
    assert domain_view.domain == "example.com"
    assert domain_view.top_hints
    assert domain_view.recent_feedback
    assert global_view.top_hints
