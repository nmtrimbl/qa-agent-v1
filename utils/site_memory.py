from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from models.site_memory import FeedbackEntry, MemoryHint, PlannerMemoryView, SiteMemory
from models.test_step import StepAction, TestStep
from utils.file_helpers import ensure_dir, safe_filename, write_json


GLOBAL_MEMORY_DOMAIN = "_global"
SITE_MEMORY_DIRNAME = "site_memory"


def normalize_domain(url_or_domain: str) -> str:
    """
    Convert a URL or raw hostname into a stable lowercase domain key.
    """

    parsed = urlparse(url_or_domain)
    candidate = parsed.netloc or parsed.path or url_or_domain
    candidate = candidate.strip().lower()
    if ":" in candidate:
        candidate = candidate.split(":", 1)[0]
    return candidate or "unknown_domain"


def load_domain_memory(*, url: str, artifacts_dir: str | Path) -> SiteMemory:
    domain = normalize_domain(url)
    return _load_memory_file(domain=domain, scope="domain", artifacts_dir=artifacts_dir)


def load_global_memory(*, artifacts_dir: str | Path) -> SiteMemory:
    return _load_memory_file(domain=GLOBAL_MEMORY_DOMAIN, scope="global", artifacts_dir=artifacts_dir)


def save_site_memory(memory: SiteMemory, *, artifacts_dir: str | Path) -> Path:
    path = _memory_file_path(domain=memory.domain, artifacts_dir=artifacts_dir)
    return write_json(path, memory.model_dump(mode="json"))


def get_feedback_entries_for_url(*, url: str, artifacts_dir: str | Path, limit: int = 10) -> list[FeedbackEntry]:
    memory = load_domain_memory(url=url, artifacts_dir=artifacts_dir)
    return memory.feedback_history[-limit:]


def build_planner_memory_views(
    *,
    url: str,
    artifacts_dir: str | Path,
    max_hints: int = 5,
    max_feedback: int = 5,
) -> tuple[PlannerMemoryView, PlannerMemoryView]:
    domain_memory = load_domain_memory(url=url, artifacts_dir=artifacts_dir)
    global_memory = load_global_memory(artifacts_dir=artifacts_dir)
    return _build_planner_memory_view(domain_memory, max_hints=max_hints, max_feedback=max_feedback), _build_planner_memory_view(
        global_memory, max_hints=max_hints, max_feedback=max_feedback
    )


def record_feedback(
    *,
    url: str,
    run_id: str,
    issue: str,
    correction: str,
    intent: str = "",
    failed_step: Optional[dict] = None,
    artifacts_dir: str | Path,
) -> tuple[SiteMemory, SiteMemory]:
    """
    Save tester feedback into both domain memory and global soft memory.
    """

    domain_memory = load_domain_memory(url=url, artifacts_dir=artifacts_dir)
    global_memory = load_global_memory(artifacts_dir=artifacts_dir)

    feedback_entry = FeedbackEntry(
        created_at=_utc_now(),
        run_id=run_id,
        issue=issue.strip(),
        correction=correction.strip(),
        intent=intent.strip().lower(),
        failed_step_index=_get_failed_step_value(failed_step, "step_index"),
        failed_action=_get_failed_step_action(failed_step),
        failed_selector=_get_failed_step_selector(failed_step),
    )

    domain_memory.feedback_history.append(feedback_entry)
    global_memory.feedback_history.append(feedback_entry)

    domain_hint = _feedback_to_hint(
        feedback=feedback_entry,
        scope="domain",
        domain=domain_memory.domain,
        base_strength=0.55,
    )
    global_hint = _feedback_to_hint(
        feedback=feedback_entry,
        scope="global",
        domain=global_memory.domain,
        base_strength=0.25,
    )

    _upsert_hint(domain_memory, domain_hint)
    _upsert_hint(global_memory, global_hint)

    save_site_memory(domain_memory, artifacts_dir=artifacts_dir)
    save_site_memory(global_memory, artifacts_dir=artifacts_dir)
    return domain_memory, global_memory


def update_memory_strength_from_steps(
    *,
    url: str,
    executed_steps,
    artifacts_dir: str | Path,
) -> None:
    """
    Strengthen or weaken any memory hints referenced by executed steps.
    """

    domain_memory = load_domain_memory(url=url, artifacts_dir=artifacts_dir)
    global_memory = load_global_memory(artifacts_dir=artifacts_dir)

    for step_execution in executed_steps:
        hint_ids = list(step_execution.step.memory_hint_ids or [])
        if not hint_ids:
            continue

        succeeded = step_execution.status == "ok"
        for hint_id in hint_ids:
            if hint_id.startswith("global:"):
                _update_hint_strength(global_memory, hint_id, succeeded=succeeded)
            else:
                _update_hint_strength(domain_memory, hint_id, succeeded=succeeded)

    save_site_memory(domain_memory, artifacts_dir=artifacts_dir)
    save_site_memory(global_memory, artifacts_dir=artifacts_dir)


def memory_summary_for_report(*, url: str, executed_steps, artifacts_dir: str | Path) -> dict[str, object]:
    """
    Build a small structured summary of memory usage for the final report.
    """

    domain = normalize_domain(url)
    domain_ids: list[str] = []
    global_ids: list[str] = []
    fallback_paths: list[str] = []

    for step_execution in executed_steps:
        for hint_id in step_execution.step.memory_hint_ids or []:
            if hint_id.startswith("global:"):
                if hint_id not in global_ids:
                    global_ids.append(hint_id)
            else:
                if hint_id not in domain_ids:
                    domain_ids.append(hint_id)

        for note in step_execution.resolution_notes:
            if note not in fallback_paths:
                fallback_paths.append(note)

    return {
        "memory_consulted": bool(domain_ids or global_ids),
        "memory_domain": domain,
        "domain_hint_ids_consulted": domain_ids,
        "global_hint_ids_consulted": global_ids,
        "fallback_paths_used": fallback_paths,
    }


def _load_memory_file(*, domain: str, scope: str, artifacts_dir: str | Path) -> SiteMemory:
    path = _memory_file_path(domain=domain, artifacts_dir=artifacts_dir)
    if path.exists():
        try:
            import json

            return SiteMemory.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass
    return SiteMemory(domain=domain, scope=scope)


def _memory_file_path(*, domain: str, artifacts_dir: str | Path) -> Path:
    root = ensure_dir(Path(artifacts_dir) / SITE_MEMORY_DIRNAME)
    return root / f"{safe_filename(domain)}.json"


def _build_planner_memory_view(memory: SiteMemory, *, max_hints: int, max_feedback: int) -> PlannerMemoryView:
    top_hints = sorted(memory.hints, key=lambda hint: (-hint.strength, -hint.success_count, hint.id))[:max_hints]
    recent_feedback = memory.feedback_history[-max_feedback:]
    return PlannerMemoryView(
        domain=memory.domain,
        scope=memory.scope,
        top_hints=top_hints,
        recent_feedback=recent_feedback,
    )


def _feedback_to_hint(*, feedback: FeedbackEntry, scope: str, domain: str, base_strength: float) -> MemoryHint:
    intent = feedback.intent or _infer_intent_from_feedback(feedback.issue, feedback.correction)
    candidates = _extract_candidate_phrases(feedback.correction)
    kind = "feedback_note"
    if any("menu" in candidate.lower() or "icon" in candidate.lower() for candidate in candidates):
        kind = "menu_hint"
    elif candidates:
        kind = "label_mapping"

    prefix = "global" if scope == "global" else f"domain:{safe_filename(domain)}"
    hint_id = f"{prefix}:{safe_filename(intent or feedback.correction[:40])}"
    return MemoryHint(
        id=hint_id,
        intent=intent,
        kind=kind,  # type: ignore[arg-type]
        candidates=candidates,
        source="user_feedback",
        scope=scope,  # type: ignore[arg-type]
        strength=base_strength,
        notes=f"Issue: {feedback.issue} Correction: {feedback.correction}",
        last_used_at=feedback.created_at,
    )


def _upsert_hint(memory: SiteMemory, hint: MemoryHint) -> None:
    for index, existing in enumerate(memory.hints):
        if existing.id == hint.id:
            merged_candidates = list(dict.fromkeys(existing.candidates + hint.candidates))
            memory.hints[index] = existing.model_copy(
                update={
                    "candidates": merged_candidates,
                    "strength": max(existing.strength, hint.strength),
                    "notes": hint.notes or existing.notes,
                    "last_used_at": hint.last_used_at or existing.last_used_at,
                }
            )
            return
    memory.hints.append(hint)


def _update_hint_strength(memory: SiteMemory, hint_id: str, *, succeeded: bool) -> None:
    for index, hint in enumerate(memory.hints):
        if hint.id != hint_id:
            continue

        delta = 0.08 if succeeded else -0.08
        new_strength = min(1.0, max(0.0, hint.strength + delta))
        memory.hints[index] = hint.model_copy(
            update={
                "strength": new_strength,
                "success_count": hint.success_count + (1 if succeeded else 0),
                "failure_count": hint.failure_count + (0 if succeeded else 1),
                "last_used_at": _utc_now(),
            }
        )
        return


def _get_failed_step_value(failed_step: Optional[dict], key: str):
    if not failed_step:
        return None
    return failed_step.get(key)


def _get_failed_step_action(failed_step: Optional[dict]) -> Optional[str]:
    if not failed_step:
        return None
    step = failed_step.get("step") or {}
    return step.get("action")


def _get_failed_step_selector(failed_step: Optional[dict]) -> Optional[str]:
    if not failed_step:
        return None
    step = failed_step.get("step") or {}
    return step.get("selector")


def _infer_intent_from_feedback(issue: str, correction: str) -> str:
    combined = f"{issue} {correction}".lower()
    for keyword in ("login", "log in", "sign in", "signin"):
        if keyword in combined:
            return "login"
    for keyword in ("account", "profile"):
        if keyword in combined:
            return "account_access"
    for keyword in ("menu", "navigation", "nav"):
        if keyword in combined:
            return "navigation"
    return ""


def _extract_candidate_phrases(text: str) -> list[str]:
    """
    Pull a few meaningful phrases from free-text corrections.
    """

    candidates: list[str] = []
    normalized = text.replace("->", ",").replace("then", ",")
    for piece in normalized.split(","):
        candidate = piece.strip().strip(".")
        if not candidate:
            continue
        lowered = candidate.lower()
        for prefix in ("click ", "open ", "tap ", "choose ", "select "):
            if lowered.startswith(prefix):
                candidate = candidate[len(prefix) :].strip()
                break
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates[:6]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
