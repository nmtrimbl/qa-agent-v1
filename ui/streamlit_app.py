from __future__ import annotations

import sys
from pathlib import Path

# Ensure repo root is importable so `from config...` works when running
# `streamlit run ui/streamlit_app.py` from any working directory.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import requests
import streamlit as st

from config.settings import get_settings
from models.test_report import TestReport


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


def main() -> None:
    st.set_page_config(page_title="AI QA Testing Platform", layout="wide")

    st.title("AI QA Testing Platform (Beginner MVP)")

    settings = get_settings()

    url = st.text_input("Website URL", placeholder="https://example.com")
    test_notes = st.text_area("Manual QA test notes", height=200, placeholder="Example:\n- Go to login page\n- Click login button\n- Verify error message appears")

    run_clicked = st.button("Run Test", type="primary")

    if not run_clicked:
        return

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

    if report.overall_status == "PASS":
        st.success("Test passed.")
    else:
        st.error("Test failed.")

    summary_tab, steps_tab, failure_tab, console_tab, screenshots_tab = st.tabs(
        ["Summary", "Steps", "Failure Details", "Console Errors", "Screenshots"]
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


if __name__ == "__main__":
    main()

