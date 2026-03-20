from __future__ import annotations

from browser.executor import ExecutionResult


def get_final_url(*, url: str, execution_result: ExecutionResult) -> str:
    """
    Best-effort final browser URL for reports and bug analysis.
    """

    if execution_result.failed_step and execution_result.failed_step.page_url:
        return execution_result.failed_step.page_url

    if execution_result.steps_executed:
        last_step = execution_result.steps_executed[-1]
        if last_step.page_url:
            return last_step.page_url

    if execution_result.failure and execution_result.failure.page_url_at_failure:
        return execution_result.failure.page_url_at_failure

    return url
