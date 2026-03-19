from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from models.test_report import TestReport
from workflows.qa_pipeline import run_qa_test_pipeline


app = FastAPI(title="AI QA Testing Platform", version="1.0")

logger = logging.getLogger("ai_qa_platform")


class RunTestRequest(BaseModel):
    url: str = Field(description="Website URL to test")
    test_notes: str = Field(description="Manual QA notes to turn into structured steps")


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

