from __future__ import annotations

from pathlib import Path

from answer_verification_state import load_answer_verification_state
from application_submit_common import load_pending_user_input_for_submit_attempt
from pipeline_draft_proof import draft_review_state

ACTION_PROCESSING_STATUSES = {
    "generating",
    "resolving",
    "autofilling",
    "retrying",
    "fix_in_progress",
    "regenerating",
    "reanswering",
    "submitting",
}
PENDING_CONFIDENCE_STATUSES = ACTION_PROCESSING_STATUSES | {"queued", "approved"}
ATTENTION_STATUSES = {"needs_board_url", "awaiting_captcha"}


def _chip(kind: str, label: str, tone: str) -> dict[str, str]:
    return {"kind": kind, "label": label, "tone": tone}


def _optional_review_chip(count: int) -> dict[str, str]:
    suffix = "field" if count == 1 else "fields"
    return _chip("friction", f"{count} unresolved optional {suffix}", "warn")


def visible_action_ids(job: dict[str, object]) -> list[str]:
    status = str(job.get("status") or "").strip()
    is_processing = status in ACTION_PROCESSING_STATUSES
    is_stopped = status == "stopped" or status in ATTENTION_STATUSES
    archived = bool(job.get("archived"))
    output_dir = job.get("output_dir")
    lock_state = str(job.get("submission_lock_state") or "").strip()

    can_restart = not is_processing and status not in {"approved", "queued", "awaiting_captcha"}
    can_archive = not is_processing and status not in {"approved", "queued", "awaiting_captcha"}
    can_delete = (
        status in {"queued", "draft", "submitted"}
        or is_stopped
        or (not is_processing and status not in {"approved", "awaiting_captcha"})
    )

    if lock_state == "locked":
        actions = ["unlock_resubmit"]
        if can_archive:
            actions.append("unarchive" if archived else "archive")
        if can_delete:
            actions.append("delete")
        return actions

    if archived and not is_processing:
        actions: list[str] = []
        if lock_state == "unlocked_for_resubmit":
            actions.append("lock_resubmission")
        actions.extend(["unarchive", "delete"])
        return actions

    actions: list[str] = []
    if lock_state == "unlocked_for_resubmit":
        actions.append("lock_resubmission")
    if not archived and (status == "draft" or (is_stopped and output_dir)):
        actions.extend(["approve_submit", "reset_to_new"])
    if status == "awaiting_captcha":
        actions.append("focus_browser")
    if status == "submitted":
        actions.append("resubmit")
    if can_restart:
        actions.append("restart_draft")
    if can_restart and status != "submitted":
        actions.append("restart_submit")
    if is_processing or status in {"approved", "draft", "awaiting_captcha", "queued"}:
        actions.append("stop")
    if can_archive:
        actions.append("archive")
    if can_delete:
        actions.append("delete")
    return actions


def derive_queue_review_summary(
    job: dict[str, object],
    *,
    draft_review: dict[str, object] | None,
    answer_verification: dict[str, object] | None,
    pending_user_input: dict[str, object] | None,
) -> dict[str, object]:
    status = str(job.get("status") or "").strip()
    review_state = str((draft_review or {}).get("state") or "missing").strip()
    verification_state = str((answer_verification or {}).get("status") or "unknown").strip()
    pending_questions = list((pending_user_input or {}).get("questions") or [])
    llm_answers = int(job.get("llm_generated_answers") or 0)
    optional_review_note_count = int((draft_review or {}).get("optional_review_note_count") or 0)

    if status in PENDING_CONFIDENCE_STATUSES:
        return {
            "overall_confidence": "pending",
            "confidence_label": "Draft in progress",
            "reason_chips": [_chip("state", "Waiting on pipeline", "info")],
            "proof_state": review_state,
            "verification_state": verification_state,
            "visible_actions": visible_action_ids(job),
        }

    proof_chip = {
        "ready": _chip("proof", "Proof current", "good"),
        "stale": _chip("proof", "Proof stale", "warn"),
        "legacy": _chip("proof", "Proof legacy", "warn"),
        "blocked": _chip("proof", "Proof blocked", "bad"),
        "unavailable": _chip("proof", "Posting unavailable", "bad"),
    }.get(review_state, _chip("proof", "Proof unavailable", "muted"))

    verification_chip = {
        "verified": _chip("answers", "Answers verified", "good"),
        "pending": _chip("answers", "Verification pending", "warn"),
        "blocked": _chip("answers", "Verification blocked", "bad"),
        "failed": _chip("answers", "Verification failed", "bad"),
        "not_applicable": _chip("answers", "No AI answers", "muted"),
    }.get(verification_state)

    if pending_questions or status in ATTENTION_STATUSES:
        friction_chip = (
            _chip("friction", "Pending input", "bad")
            if pending_questions
            else _chip(
                "friction",
                "Needs board URL" if status == "needs_board_url" else "Manual review",
                "bad",
            )
        )
        return {
            "overall_confidence": "low",
            "confidence_label": "Needs review before submit",
            "reason_chips": [
                proof_chip,
                verification_chip or _chip("answers", "Verification unavailable", "muted"),
                friction_chip,
            ],
            "proof_state": review_state,
            "verification_state": verification_state,
            "visible_actions": visible_action_ids(job),
        }

    if review_state in {"blocked", "unavailable"} or verification_state in {"blocked", "failed"}:
        return {
            "overall_confidence": "low",
            "confidence_label": "Needs review before submit",
            "reason_chips": [
                proof_chip,
                verification_chip or _chip("answers", "Verification unavailable", "muted"),
                _chip("friction", "1 blocker", "bad"),
            ],
            "proof_state": review_state,
            "verification_state": verification_state,
            "visible_actions": visible_action_ids(job),
        }

    if review_state == "ready" and verification_state in {"verified", "not_applicable"} and optional_review_note_count == 0:
        third_chip = _chip("friction", "No blockers", "good")
        if llm_answers > 0 and verification_state == "not_applicable":
            third_chip = _chip("answers", f"{llm_answers} AI answers", "muted")
        return {
            "overall_confidence": "high",
            "confidence_label": "Ready to submit",
            "reason_chips": [
                proof_chip,
                verification_chip or _chip("answers", "No AI answers", "muted"),
                third_chip,
            ],
            "proof_state": review_state,
            "verification_state": verification_state,
            "visible_actions": visible_action_ids(job),
        }

    chips: list[dict[str, str]] = [proof_chip]
    if verification_chip is not None:
        chips.append(verification_chip)
    if optional_review_note_count > 0:
        chips.append(_optional_review_chip(optional_review_note_count))
    elif llm_answers > 0:
        chips.append(_chip("answers", f"{llm_answers} AI answers", "muted"))
    else:
        chips.append(_chip("friction", "No blockers", "muted"))
    return {
        "overall_confidence": "medium",
        "confidence_label": "Usable, but review recommended",
        "reason_chips": chips[:3],
        "proof_state": review_state,
        "verification_state": verification_state,
        "visible_actions": visible_action_ids(job),
    }


def attach_queue_review_summary(jobs: list[dict[str, object]]) -> list[dict[str, object]]:
    for job in jobs:
        out_dir = job.get("output_dir")
        if out_dir:
            path = Path(out_dir)
            draft = draft_review_state(path, board_name=job.get("board"))
            verification = load_answer_verification_state(path)
            pending = load_pending_user_input_for_submit_attempt(path)
            pending_payload = pending[1] if pending is not None else None
        else:
            draft = None
            verification = None
            pending_payload = None
        job["queue_review_summary"] = derive_queue_review_summary(
            job,
            draft_review=draft,
            answer_verification=verification,
            pending_user_input=pending_payload,
        )
    return jobs
