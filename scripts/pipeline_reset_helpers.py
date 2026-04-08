"""Utility helpers shared by pipeline reset workflows."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from job_db import RETRY_AFTER_SENTINEL


def _clear_root_draft_review_artifacts(output_dir: Path, *, remove_answer_refresh_status: bool) -> None:
    paths = [
        output_dir / ".asset_pipeline_state.json",
        output_dir / "draft_status.json",
        output_dir / "draft_summary.md",
        output_dir / "draft_summary.original.md",
        output_dir / "draft_summary.png",
    ]
    if remove_answer_refresh_status:
        paths.append(output_dir / "answer_refresh_status.json")
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _current_attempt_cleanup_payload(output_dir: Path, submit_dir: Path, board_name: str | None) -> dict:
    board_name = str(board_name or "").strip()
    if not board_name:
        return {
            "out_dir": str(output_dir),
            "artifacts": {
                "payload_json": str(submit_dir / "_autofill_payload.json"),
            },
        }

    from autofill_common import board_file_constants

    constants = board_file_constants(board_name)
    return {
        "out_dir": str(output_dir),
        "artifacts": {
            "report_markdown": str(submit_dir / constants["report_md"]),
            "report_json": str(submit_dir / constants["report_json"]),
            "pre_submit_screenshot": str(submit_dir / constants["pre_submit_screenshot"]),
            "review_screenshot": str(submit_dir / constants["review_screenshot"]),
            "post_submit_screenshot": str(submit_dir / constants["post_submit_screenshot"]),
            "submit_debug_html": str(submit_dir / constants["submit_debug_html"]),
            "submit_debug_screenshot": str(submit_dir / constants["submit_debug_screenshot"]),
            "page_screenshots_dir": str(submit_dir / constants["page_screenshots_dir"]),
            "payload_json": str(submit_dir / constants["payload_json"]),
        },
    }


def _clear_submission_completion_artifacts(submit_dir: Path) -> None:
    for path in (
        submit_dir / "application_submission_result.json",
        submit_dir / "application_confirmation_website.json",
        submit_dir / "application_confirmation_email.json",
        submit_dir / "confirmation_email_reply.json",
        submit_dir / "notion_sync_status.json",
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def clear_restart_pipeline_artifacts(output_dir: Path, board_name: str | None) -> None:
    """Remove stale current-attempt proof before a fresh draft rerun."""
    from autofill_common import clear_current_attempt_artifacts
    from output_layout import role_submit_dir

    _clear_root_draft_review_artifacts(output_dir, remove_answer_refresh_status=True)

    submit_dir = role_submit_dir(output_dir)
    clear_current_attempt_artifacts(_current_attempt_cleanup_payload(output_dir, submit_dir, board_name))
    _clear_submission_completion_artifacts(submit_dir)


def clear_reset_to_new_artifacts(output_dir: Path, board_name: str | None) -> None:
    """Remove transient artifacts a reset-to-new attempt must flush."""
    from autofill_common import board_file_constants, clear_current_attempt_artifacts
    from output_layout import default_role_submit_dir, role_submit_dir

    board_name = str(board_name or "").strip()

    submit_dirs: list[Path] = []
    for candidate in (role_submit_dir(output_dir), default_role_submit_dir(output_dir)):
        if candidate not in submit_dirs:
            submit_dirs.append(candidate)

    def _clear_submit_dir(submit_dir: Path) -> None:
        clear_current_attempt_artifacts(_current_attempt_cleanup_payload(output_dir, submit_dir, board_name))
        _clear_submission_completion_artifacts(submit_dir)
        if board_name:
            constants = board_file_constants(board_name)
            for key in ("payload_json", "unknown_questions_json", "application_page_html"):
                try:
                    (submit_dir / constants[key]).unlink(missing_ok=True)
                except OSError:
                    pass

        for path in (
            submit_dir / "pending_user_input.json",
            submit_dir / "unsupported_board.json",
            submit_dir / "job_unavailable.json",
            submit_dir / "workday_auth_failure.json",
            submit_dir / "icims_auth_failure.json",
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    _clear_root_draft_review_artifacts(output_dir, remove_answer_refresh_status=True)

    for submit_dir in submit_dirs:
        _clear_submit_dir(submit_dir)
        if submit_dir.exists():
            for pattern in (
                "*_autofill_payload.json",
                "*_unknown_questions.json",
                "*_application_page.html",
            ):
                for candidate in submit_dir.glob(pattern):
                    try:
                        candidate.unlink(missing_ok=True)
                    except OSError:
                        continue


def _finalize_reset_job_to_new(
    conn: sqlite3.Connection,
    job_id: int,
    output_dir: Path | None,
    board_name: str | None,
    *,
    initiator: str = "web",
    event_detail_json: dict | None = None,
    process_info: str | None = None,
) -> bool:
    """Complete the RESET job to new workflow after the submission lock clears."""
    from answer_refresh_state import mark_answer_refresh_pending
    from job_db import log_event
    from output_layout import set_active_submit_dir

    has_output_dir = output_dir is not None and output_dir.exists()
    if has_output_dir:
        clear_reset_to_new_artifacts(output_dir, board_name)
        set_active_submit_dir(output_dir, "submit")

    cur = conn.execute(
        "UPDATE jobs SET status = 'queued', provider = NULL, retry_after = ?, error_message = '', progress = '', "
        "failure_type = NULL, auth_state = NULL, auth_scope = NULL WHERE id = ? AND archived = FALSE",
        (RETRY_AFTER_SENTINEL, job_id),
    )
    conn.commit()
    if cur.rowcount <= 0:
        return False
    if has_output_dir and output_dir is not None:
        mark_answer_refresh_pending(output_dir, request_kind="reset_to_new")
    log_event(
        conn,
        job_id,
        "status_change",
        detail="queued",
        detail_json=event_detail_json,
        initiator=initiator,
        process_info=process_info,
    )
    log_event(
        conn,
        job_id,
        "reset_to_new_requested",
        detail_json=event_detail_json,
        initiator=initiator,
        process_info=process_info,
    )
    return True
