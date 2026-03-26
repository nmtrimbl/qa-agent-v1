from __future__ import annotations

import sys
from pathlib import Path

# Ensure repo root is importable so `from config...` works when running
# `streamlit run ui/streamlit_app.py` from any working directory.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from utils.runtime import ensure_project_root_on_path

ensure_project_root_on_path(PROJECT_ROOT)

import requests
import streamlit as st

from config.settings import get_settings
from models.test_report import TestReport
from utils.site_memory import build_planner_memory_views, normalize_domain, record_feedback


def _safe_image(path: str):
    try:
        if path:
            st.image(path, caption=path, width="stretch")
    except Exception:
        st.write(f"Screenshot: {path}")


def _render_summary(report: TestReport) -> None:
    status_col, severity_col, url_col = st.columns(3)
    status_col.metric("Status", report.overall_status)
    severity_col.metric("Severity", (report.severity_guess or "n/a").upper())
    url_col.metric("Executed Steps", str(len(report.steps_executed)))

    if report.test_summary:
        st.subheader("Test Summary")
        st.write(report.test_summary)

    if report.failure_summary:
        st.subheader("Failure Summary")
        st.write(report.failure_summary)

    if report.likely_failure_cause:
        st.subheader("Likely Failure Cause")
        st.write(report.likely_failure_cause)

    if report.reproduction_notes:
        st.subheader("Reproduction Notes")
        st.write(report.reproduction_notes)

    st.caption(f"Run ID: {report.run_id}")
    st.caption(f"Start URL: {report.url}")
    if report.final_url:
        st.caption(f"Final URL: {report.final_url}")

    if report.memory_consulted:
        st.subheader("Memory Usage")
        if report.memory_domain:
            st.write(f"Domain memory: `{report.memory_domain}`")
        if report.domain_hint_ids_consulted:
            st.write("Domain hints consulted:")
            for hint_id in report.domain_hint_ids_consulted:
                st.write(f"- `{hint_id}`")
        if report.global_hint_ids_consulted:
            st.write("Global hints consulted:")
            for hint_id in report.global_hint_ids_consulted:
                st.write(f"- `{hint_id}`")
        if report.fallback_paths_used:
            st.write("Fallback paths used:")
            for path in report.fallback_paths_used:
                st.write(f"- {path}")


def _render_steps(report: TestReport) -> None:
    st.subheader("Executed Steps")
    rows = []
    for step_exec in report.steps_executed:
        rows.append(
            {
                "Step": step_exec.step_index + 1,
                "Action": step_exec.step.action.value,
                "Status": step_exec.status,
                "Selector": step_exec.step.selector or "",
                "Expected Text": step_exec.step.expected_text or "",
                "Page URL": step_exec.page_url or "",
                "Fallback Notes": " | ".join(step_exec.resolution_notes),
            }
        )

    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
    else:
        st.write("No executed steps were recorded.")

    for step_exec in report.steps_executed:
        if step_exec.error_message:
            with st.expander(f"Step {step_exec.step_index + 1} error details"):
                st.code(step_exec.error_message)
                if step_exec.resolution_notes:
                    st.write("Fallback notes:")
                    for note in step_exec.resolution_notes:
                        st.write(f"- {note}")
                if step_exec.screenshot_path:
                    _safe_image(step_exec.screenshot_path)


def _render_failure_details(report: TestReport) -> None:
    st.subheader("Failure Details")
    if not report.failed_step:
        st.write("No failed step details were recorded.")
        return

    failed_step = report.failed_step
    st.write(f"Failed step: {failed_step.step_index + 1}")
    st.write(f"Action: {failed_step.step.action.value}")
    if failed_step.step.selector:
        st.write(f"Selector: `{failed_step.step.selector}`")
    if failed_step.step.expected_text:
        st.write(f"Expected text: `{failed_step.step.expected_text}`")
    if failed_step.page_url:
        st.write(f"Page URL: {failed_step.page_url}")
    if failed_step.memory_hint_ids_used:
        st.write("Memory hints tied to the failed step:")
        for hint_id in failed_step.memory_hint_ids_used:
            st.write(f"- `{hint_id}`")
    if failed_step.resolution_notes:
        st.write("Fallback notes:")
        for note in failed_step.resolution_notes:
            st.write(f"- {note}")
    st.code(failed_step.error_message)
    if failed_step.screenshot_path:
        _safe_image(failed_step.screenshot_path)


def _render_console_errors(report: TestReport) -> None:
    st.subheader("Console Errors")
    if not report.console_errors:
        st.write("No console errors captured.")
        return

    rows = []
    for err in report.console_errors:
        rows.append(
            {
                "Kind": err.kind,
                "Message": err.message,
                "Location": err.location or "",
                "Page URL": err.page_url or "",
            }
        )
    st.dataframe(rows, width="stretch", hide_index=True)


def _render_screenshots(report: TestReport) -> None:
    st.subheader("Screenshots")
    if not report.screenshot_paths:
        st.write("No screenshots captured.")
        return

    for path in report.screenshot_paths:
        _safe_image(path)


def _render_developer_details(report: TestReport) -> None:
    st.subheader("Planner Output JSON")
    if report.planner_output:
        st.json(report.planner_output, expanded=2)
    else:
        st.write("No planner output was attached to the report.")


def _load_memory_for_display(url: str):
    settings = get_settings()
    try:
        return build_planner_memory_views(url=url, artifacts_dir=settings.artifacts_dir)
    except Exception:
        return None, None


def _render_memory_hints(url: str) -> None:
    if not url.strip():
        return

    domain_memory, global_memory = _load_memory_for_display(url)
    if domain_memory is None or global_memory is None:
        return

    with st.expander("Feedback-Assisted Memory", expanded=False):
        st.write(
            "These are soft hints learned from previous feedback and successful runs. "
            "They guide planning and fallbacks, but they are not treated as hard rules."
        )
        st.caption(f"Domain: {normalize_domain(url)}")

        st.markdown("**Previous site feedback**")
        if domain_memory.recent_feedback:
            for entry in reversed(domain_memory.recent_feedback):
                st.write(f"- Issue: {entry.issue}")
                st.write(f"  Correction: {entry.correction}")
        else:
            st.write("No saved feedback for this site yet.")

        st.markdown("**Global soft hints**")
        if global_memory.top_hints:
            for hint in global_memory.top_hints:
                st.write(
                    f"- `{hint.intent or 'general'}` via {hint.kind}: "
                    + ", ".join(hint.candidates[:4])
                )
        else:
            st.write("No shared cross-site hints yet.")


def _save_feedback(
    *,
    url: str,
    report: TestReport,
    issue: str,
    correction: str,
    intent: str,
) -> None:
    settings = get_settings()
    payload = {
        "url": url,
        "run_id": report.run_id,
        "issue": issue,
        "correction": correction,
        "intent": intent,
        "failed_step": report.failed_step.model_dump(mode="json") if report.failed_step else None,
    }

    feedback_url = settings.fastapi_url.rsplit("/", 1)[0] + "/feedback"
    try:
        response = requests.post(feedback_url, json=payload, timeout=30)
        response.raise_for_status()
    except Exception:
        record_feedback(
            url=url,
            run_id=report.run_id,
            issue=issue,
            correction=correction,
            intent=intent,
            failed_step=payload["failed_step"],
            artifacts_dir=settings.artifacts_dir,
        )


def _render_feedback_form(url: str, report: TestReport) -> None:
    st.subheader("Submit Feedback")
    st.write(
        "Help the system improve for future runs. Feedback is stored as a hint, "
        "not as a hard rule, so later runs can strengthen or weaken it."
    )
    issue = st.text_area(
        "What went wrong?",
        key="feedback_issue",
        placeholder="Example: The system only looked for 'Login' text.",
    )
    correction = st.text_area(
        "What should it do instead?",
        key="feedback_correction",
        placeholder="Example: Open the account icon first, then click Sign In.",
    )
    intent = st.text_input(
        "Intent (optional)",
        key="feedback_intent",
        placeholder="Example: login",
    )

    if st.button("Submit Feedback", key="submit_feedback"):
        if not issue.strip() or not correction.strip():
            st.error("Please fill in both feedback fields.")
            return
        _save_feedback(url=url, report=report, issue=issue, correction=correction, intent=intent)
        st.success("Feedback saved. It will be used as a soft hint in future runs.")


def main() -> None:
    st.set_page_config(page_title="AI QA Testing Platform", layout="wide")

    st.title("AI QA Testing Platform (Beginner MVP)")

    settings = get_settings()

    url = st.text_input("Website URL", placeholder="https://example.com")
    test_notes = st.text_area("Manual QA test notes", height=200, placeholder="Example:\n- Go to login page\n- Click login button\n- Verify error message appears")

    _render_memory_hints(url)

    run_clicked = st.button("Run Test", type="primary")

    if run_clicked:
        if not url.strip():
            st.error("Please enter a URL.")
            return

        if not test_notes.strip():
            st.error("Please paste your manual QA test notes.")
            return

        st.info("Running test. This may take a minute...")
        with st.spinner("Planning, running Playwright, and generating report..."):
            report: TestReport
            payload = {"url": url, "test_notes": test_notes}

            # Prefer calling the FastAPI server (as requested), but fall back to local execution.
            try:
                resp = requests.post(settings.fastapi_url, json=payload, timeout=600)
                resp.raise_for_status()
                report = TestReport.model_validate(resp.json())
            except Exception:
                from workflows.qa_pipeline import run_qa_test_pipeline

                report = run_qa_test_pipeline(url=url, test_notes=test_notes)

        st.session_state["last_report"] = report.model_dump(mode="json")
        st.session_state["last_url"] = url

    stored_report = st.session_state.get("last_report")
    stored_url = st.session_state.get("last_url", url)
    if not stored_report:
        return

    report = TestReport.model_validate(stored_report)

    if report.overall_status == "PASS":
        st.success("Test passed.")
    else:
        st.error("Test failed.")

    summary_tab, steps_tab, failure_tab, console_tab, screenshots_tab, developer_tab = st.tabs(
        ["Summary", "Steps", "Failure Details", "Console Errors", "Screenshots", "Developer"]
    )

    with summary_tab:
        _render_summary(report)

    with steps_tab:
        _render_steps(report)

    with failure_tab:
        _render_failure_details(report)

    with console_tab:
        _render_console_errors(report)

    with screenshots_tab:
        _render_screenshots(report)

    with developer_tab:
        _render_developer_details(report)

    _render_feedback_form(stored_url, report)


if __name__ == "__main__":
    main()

