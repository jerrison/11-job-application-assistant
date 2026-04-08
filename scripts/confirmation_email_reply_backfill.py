"""Helpers for backfilling confirmation-email self-reply state during disk sync."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path


def maybe_backfill_confirmation_email_reply(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    job: sqlite3.Row,
    out_dir: Path,
    email_confirmation: dict | None,
    synced: dict,
    log_event_fn: Callable[..., int],
) -> None:
    """Backfill the self-reply when confirmation is discovered after submit-time hooks."""
    if not isinstance(email_confirmation, dict):
        return

    board_name = str(job["board"] or "").strip()
    if not board_name:
        return

    from output_layout import CONFIRMATION_EMAIL_REPLY_JSON, preferred_submit_dir_name_for_post_submit

    submit_dir_name = preferred_submit_dir_name_for_post_submit(out_dir) or "submit"
    state_path = out_dir / submit_dir_name / CONFIRMATION_EMAIL_REPLY_JSON
    if state_path.exists():
        return

    try:
        from application_submit_common import send_confirmation_email_reply

        reply_result = send_confirmation_email_reply(
            {"out_dir": str(out_dir)},
            board_name=board_name,
            email_confirmation=email_confirmation,
            caller="disk_sync",
        )
        _record_reply_backfill_result(conn, job_id, reply_result, synced, log_event_fn)
    except Exception as exc:
        log_event_fn(conn, job_id, "email_reply_failed", detail=str(exc), initiator="disk_sync")


def _record_reply_backfill_result(
    conn: sqlite3.Connection,
    job_id: int,
    reply_result: dict,
    synced: dict,
    log_event_fn: Callable[..., int],
) -> None:
    reply_status = str(reply_result.get("status") or "")
    if reply_status == "sent":
        log_event_fn(conn, job_id, "email_reply_sent", initiator="disk_sync")
        synced["updates"].append("email_reply→sent")
        return

    if reply_status == "skipped_duplicate":
        log_event_fn(
            conn,
            job_id,
            "email_reply_skipped_duplicate",
            detail_json=_reply_detail_json(reply_result),
            initiator="disk_sync",
        )
        synced["updates"].append("email_reply→skipped_duplicate")
        return

    if reply_status == "not_sent":
        log_event_fn(
            conn,
            job_id,
            "email_reply_not_sent",
            detail_json=_reply_detail_json(reply_result),
            initiator="disk_sync",
        )
        synced["updates"].append(f"email_reply→{reply_result.get('reason') or 'not_sent'}")


def _reply_detail_json(reply_result: dict) -> dict:
    return {
        "reason": reply_result.get("reason"),
        "submit_dir": reply_result.get("submit_dir"),
        "state_path": reply_result.get("state_path"),
    }
