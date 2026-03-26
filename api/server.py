from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from config.settings import get_settings
from models.site_memory import PlannerMemoryView
from models.test_report import TestReport
from workflows.qa_pipeline import run_qa_test_pipeline
from utils.site_memory import build_planner_memory_views, record_feedback


app = FastAPI(title="AI QA Testing Platform", version="1.0")

logger = logging.getLogger("ai_qa_platform")


class RunTestRequest(BaseModel):
    url: str = Field(description="Website URL to test")
    test_notes: str = Field(description="Manual QA notes to turn into structured steps")


class FeedbackRequest(BaseModel):
    url: str = Field(description="Website URL tied to the feedback")
    run_id: str = Field(description="Run ID for the test that produced this feedback")
    issue: str = Field(description="What the tester observed going wrong")
    correction: str = Field(description="What the system should have done instead")
    intent: str = Field(default="", description="Optional high-level intent like login or checkout")
    failed_step: dict[str, Any] | None = Field(default=None, description="Optional failed step details")


class FeedbackResponse(BaseModel):
    status: str
    domain: str


class MemoryResponse(BaseModel):
    domain_memory: PlannerMemoryView
    global_memory: PlannerMemoryView


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run-test", response_model=TestReport)
def run_test(payload: RunTestRequest) -> TestReport:
    """
    Run an automated QA test:
      1) LLM plans steps (structured JSON)
      2) Playwright executes steps deterministically
      3) LLM analyzes failures
      4) LLM generates a final report
    """

    try:
        return run_qa_test_pipeline(url=payload.url, test_notes=payload.test_notes)
    except Exception as e:
        logger.exception("Run test failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/feedback", response_model=FeedbackResponse)
def submit_feedback(payload: FeedbackRequest) -> FeedbackResponse:
    settings = get_settings()
    try:
        domain_memory, _global_memory = record_feedback(
            url=payload.url,
            run_id=payload.run_id,
            issue=payload.issue,
            correction=payload.correction,
            intent=payload.intent,
            failed_step=payload.failed_step,
            artifacts_dir=settings.artifacts_dir,
        )
        return FeedbackResponse(status="ok", domain=domain_memory.domain)
    except Exception as e:
        logger.exception("Saving feedback failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/memory/{domain}", response_model=MemoryResponse)
def get_memory(domain: str) -> MemoryResponse:
    settings = get_settings()
    try:
        domain_memory, global_memory = build_planner_memory_views(
            url=domain,
            artifacts_dir=settings.artifacts_dir,
        )
        return MemoryResponse(domain_memory=domain_memory, global_memory=global_memory)
    except Exception as e:
        logger.exception("Loading memory failed")
        raise HTTPException(status_code=500, detail=str(e))

