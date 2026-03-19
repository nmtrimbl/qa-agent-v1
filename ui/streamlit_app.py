from __future__ import annotations

import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import requests
import streamlit as st

from config.settings import get_settings
from models.test_report import TestReport


def _safe_image(path: str):
    try:
        if path:
            st.image(path, caption=path, use_column_width=True)
    except Exception:
        st.write(f"Screenshot: {path}")


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

    st.success(f"Overall: {report.overall_status}")
    st.caption(f"Run ID: {report.run_id}")

    if report.failure_summary:
        st.subheader("Failure Summary")
        st.write(report.failure_summary)

    st.subheader("Steps Executed")
    for idx, step_exec in enumerate(report.steps_executed, start=1):
        st.write(f"{idx}. {step_exec.step.action.value} - {step_exec.status}")
        if step_exec.error_message:
            st.code(step_exec.error_message)

    st.subheader("Console Errors")
    if report.console_errors:
        for err in report.console_errors:
            st.write(f"- {err.kind}: {err.message}")
            if err.location:
                st.write(f"  location: {err.location}")
            if err.page_url:
                st.write(f"  page_url: {err.page_url}")
    else:
        st.write("No console errors captured.")

    st.subheader("Screenshots")
    if report.screenshot_paths:
        for path in report.screenshot_paths:
            _safe_image(path)
    else:
        st.write("No screenshots captured.")


if __name__ == "__main__":
    main()

