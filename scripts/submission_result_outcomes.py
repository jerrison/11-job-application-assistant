"""Helpers for current-attempt submission-result reconciliation."""

from __future__ import annotations

import sqlite3

_DRAFT_MODE_SUBMISSION_RESULT_STATUS_MAP = {
    "confirmed": "submitted",
    "submitted": "submitted",
    "already_applied": "submitted",
    "not_easy_apply": "stopped",
    "skipped_captcha": "stopped",
    "skipped_auth": "stopped",
    "skipped_auth_failure": "stopped",
    "auth_failed": "stopped",
    "auth_unknown": "stopped",
    "auth_guarded": "stopped",
    "service_unavailable": "stopped",
    "job_closed": "stopped",
    "needs_manual": "stopped",
    "pending_user_input": "stopped",
    "unknown": "stopped",
    "failed": "stopped",
}


def _submission_result_failure_type(result: dict) -> str | None:
    status = str(result.get("status") or "").strip().casefold()
    failure_type = str(result.get("failure_type") or result.get("reason") or "").strip()
    if failure_type:
        return failure_type
    return status or None


def _submission_result_message(result: dict) -> str:
    message = str(result.get("message") or "").strip()
    if message:
        return message

    status = str(result.get("status") or "").strip().casefold()
    failure_type = _submission_result_failure_type(result)
    if status == "not_easy_apply":
        if failure_type == "external_apply":
            return "The current application flow switched to an external apply surface."
        if failure_type == "no_apply_button":
            return "The current application surface does not expose an Easy Apply or external Apply control."
        return "The current application flow no longer exposes an in-flow draftable apply surface."
    if status == "skipped_captcha":
        return "Submission skipped: captcha required. Moving on to next job."
    if status in {"pending_user_input", "needs_manual"}:
        return "Submission paused because one or more answers require manual user input."
    if status == "unknown":
        return "Submission stopped because the board reported an unknown result state."
    if status == "job_closed":
        return "The application is no longer available."
    if failure_type:
        return f"Submission stopped with failure type: {failure_type}."
    if status:
        return f"Submission stopped with result status: {status}."
    return "Submission stopped after the board returned a non-draft outcome."


def handle_draft_mode_submission_result(
    conn: sqlite3.Connection,
    job_id: int,
    result: dict | None,
    *,
    initiator: str = "worker",
) -> str | None:
    """Honor terminal submission-result artifacts before draft-proof validation."""
    if not isinstance(result, dict):
        return None

    result_status = str(result.get("status") or "").strip().casefold()
    mapped_status = _DRAFT_MODE_SUBMISSION_RESULT_STATUS_MAP.get(result_status)
    if mapped_status is None:
        return None

    from job_db import log_event, record_confirmed_submission, update_status

    if mapped_status == "submitted":
        confirmed_at = str(result.get("confirmed_at_utc") or result.get("confirmed_at") or "").strip() or None
        record_confirmed_submission(conn, job_id, confirmed_at=confirmed_at, initiator=initiator)
        log_event(
            conn,
            job_id,
            "submission_result_submitted",
            detail=_submission_result_message(result),
            detail_json=result,
            initiator=initiator,
        )
        return "submitted"

    update_kwargs: dict[str, object] = {
        "error_message": _submission_result_message(result),
        "failure_type": _submission_result_failure_type(result),
    }
    auth_state = str(result.get("auth_state") or "").strip()
    if auth_state:
        update_kwargs["auth_state"] = auth_state
    auth_scope = str(result.get("auth_scope") or "").strip()
    if auth_scope:
        update_kwargs["auth_scope"] = auth_scope
    if result_status == "job_closed":
        update_kwargs["archived"] = True
    update_status(conn, job_id, "stopped", **update_kwargs)
    log_event(
        conn,
        job_id,
        "submission_result_stopped",
        detail=str(update_kwargs["error_message"]),
        detail_json=result,
        initiator=initiator,
    )
    return "stopped"
