from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from agents.bug_analyzer import BugAnalysis, analyze_failure
from agents.report_generator import generate_report
from agents.test_planner import plan_test_steps
from browser.browser_session import BrowserSession
from browser.executor import BrowserExecutor
from config.settings import get_settings
from models.test_report import TestReport
from utils.file_helpers import ensure_dir, new_run_id, write_json
from utils.runtime import ensure_runtime_directories


def run_qa_test_pipeline(*, url: str, test_notes: str) -> TestReport:
    """
    Synchronous QA pipeline.

    Stages:
      1) Test planner agent -> structured TestStep JSON
      2) Browser controller -> deterministic Playwright execution
      3) Bug analyzer agent -> likely failure reasons (LLM)
      4) Report generator agent -> final QA report (LLM)
    """

    settings = get_settings()
    run_id = new_run_id()

    ensure_runtime_directories(artifacts_dir=settings.artifacts_dir, logs_dir=settings.logs_dir)
    run_artifacts_dir = ensure_dir(Path(settings.artifacts_dir) / run_id)
    logs_dir = ensure_dir(settings.logs_dir)
    log_path = logs_dir / f"{run_id}.log"

    logger = logging.getLogger(f"qa_pipeline_{run_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    # Avoid duplicate handlers if reloaded.
    if not logger.handlers:
        file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
        stream_handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        file_handler.setFormatter(formatter)
        stream_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        logger.addHandler(stream_handler)

    logger.info("Planning steps with LLM...")
    steps = plan_test_steps(url=url, test_notes=test_notes)
    write_json(run_artifacts_dir / "planned_steps.json", [s.model_dump(mode="json") for s in steps])

    logger.info("Executing steps in Playwright...")
    with BrowserSession(headless=settings.playwright_headless, artifacts_dir=run_artifacts_dir, run_id=run_id) as session:
        executor = BrowserExecutor(artifacts_dir=run_artifacts_dir)
        execution_result = executor.execute(session=session, url=url, steps=steps, run_id=run_id)

    logger.info("Generating failure analysis...")
    if execution_result.success:
        bug_analysis = BugAnalysis(
            failure_summary="PASS: All planned steps completed successfully.",
            likely_failure_cause="",
            reproduction_notes="",
            severity_guess="low",
            likely_causes=[],
            suggestions=[],
        )
    else:
        bug_analysis = analyze_failure(
            url=url,
            test_notes=test_notes,
            execution_result=execution_result,
            artifacts_dir=run_artifacts_dir,
        )

    logger.info("Generating report...")
    report = generate_report(
        run_id=run_id,
        url=url,
        execution_result=execution_result,
        bug_analysis=bug_analysis,
    )

    # Save final report.
    write_json(run_artifacts_dir / "report.json", report.model_dump(mode="json"))
    write_json(run_artifacts_dir / "input.json", {"url": url, "test_notes": test_notes})

    return report

