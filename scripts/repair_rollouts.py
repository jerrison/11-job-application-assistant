"""Helpers for persisting and evaluating active repair rollouts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from job_db import clear_repair_queue_pause, record_repair_rollout, set_repair_queue_pause

_ACTIVE_ROLLOUT_STATUSES = frozenset({"active", "paused_pending_confirmation", "monitoring_resumed"})
_NON_REGRESSION_FAILURE_TYPES = frozenset(
    {
        "already_applied",
        "auth_failed",
        "auth_guarded",
        "auth_unknown",
        "external_apply",
        "job_closed",
        "pending_user_input",
        "skipped_captcha",
        "unsupported",
        "user_rejected",
        "user_stopped",
    }
)


@dataclass(frozen=True)
class RolloutEvaluation:
    action: str
    reason: str
    post_fix_metrics: dict


def _scope_from_fingerprint(fingerprint: str) -> tuple[str, str]:
    parts = [part.strip() for part in str(fingerprint or "").split(":")]
    board = parts[0] if parts and parts[0] else "unknown"
    phase = parts[1] if len(parts) > 1 and parts[1] else "unknown"
    return board, phase


def _parse_json_object(raw_value: object) -> dict:
    if isinstance(raw_value, dict):
        return raw_value
    try:
        parsed = json.loads(str(raw_value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _coerce_int_list(values: list[object]) -> list[int]:
    deduped: list[int] = []
    seen: set[int] = set()
    for value in values:
        try:
            as_int = int(value)
        except (TypeError, ValueError):
            continue
        if as_int in seen:
            continue
        seen.add(as_int)
        deduped.append(as_int)
    return deduped


def record_active_rollout(
    conn: sqlite3.Connection,
    *,
    cluster_id: int,
    commit_sha: str,
    fingerprint: str,
    touched_files: list[str],
    monitored_job_ids: list[int],
    output_root: str | Path,
) -> int:
    board, phase = _scope_from_fingerprint(fingerprint)
    baseline_metrics = {
        "fingerprint": str(fingerprint),
        "board": board,
        "phase": phase,
        "touched_files": list(dict.fromkeys(str(path) for path in touched_files if str(path).strip())),
        "monitored_job_ids": _coerce_int_list(list(monitored_job_ids)),
        "fingerprint_recurrences": 0,
        "unexpected_hard_failures": 0,
    }
    rollout_id = record_repair_rollout(
        conn,
        int(cluster_id),
        commit_sha=str(commit_sha),
        status="active",
        baseline_metrics_json=baseline_metrics,
    )
    refresh_active_repair_rollouts_index(output_root=output_root, rollouts=list_active_rollouts(conn))
    return rollout_id


def list_active_rollouts(conn: sqlite3.Connection) -> list[dict]:
    placeholders = ", ".join("?" for _ in _ACTIVE_ROLLOUT_STATUSES)
    rows = conn.execute(
        f"""
        SELECT *
        FROM repair_rollouts
        WHERE status IN ({placeholders})
        ORDER BY created_at ASC, id ASC
        """,
        tuple(_ACTIVE_ROLLOUT_STATUSES),
    ).fetchall()
    return [dict(row) for row in rows]


def update_rollout_status(
    conn: sqlite3.Connection,
    rollout_id: int,
    *,
    status: str,
    post_fix_metrics: dict,
    output_root: str | Path,
    revert_sha: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE repair_rollouts
        SET status = ?,
            post_fix_metrics_json = ?,
            revert_sha = COALESCE(?, revert_sha),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            str(status),
            json.dumps(post_fix_metrics or {}, sort_keys=True),
            revert_sha,
            int(rollout_id),
        ),
    )
    conn.commit()
    refresh_active_repair_rollouts_index(output_root=output_root, rollouts=list_active_rollouts(conn))

    row = conn.execute("SELECT * FROM repair_rollouts WHERE id = ?", (int(rollout_id),)).fetchone()
    if row is None:
        return
    rollout = dict(row)
    baseline = _parse_json_object(rollout.get("baseline_metrics_json"))
    fingerprint = str(baseline.get("fingerprint") or "")
    if status == "paused_pending_confirmation":
        set_repair_queue_pause(
            conn,
            rollout_id=int(rollout_id),
            commit_sha=str(rollout.get("commit_sha") or ""),
            cluster_id=int(rollout.get("cluster_id") or 0),
            fingerprint=fingerprint,
            reason=str((post_fix_metrics or {}).get("reason") or "rollout_pause_requested"),
        )
    elif status in {"reverted", "cleared"}:
        clear_repair_queue_pause(conn)


def evaluate_rollout(conn: sqlite3.Connection, rollout: dict, *, confirmation: bool) -> RolloutEvaluation:
    baseline = _parse_json_object(rollout.get("baseline_metrics_json"))
    fingerprint = str(baseline.get("fingerprint") or rollout.get("fingerprint") or "").strip()
    board = str(baseline.get("board") or _scope_from_fingerprint(fingerprint)[0]).strip()
    created_at = str(rollout.get("created_at") or "").strip()

    recurrence_row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM repair_clusters
        WHERE fingerprint = ?
          AND COALESCE(updated_at, created_at) > ?
        """,
        (fingerprint, created_at),
    ).fetchone()
    fingerprint_recurrences = int((recurrence_row["count"] if recurrence_row else 0) or 0)

    hard_failure_row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM jobs
        WHERE board = ?
          AND status = 'stopped'
          AND updated_at > ?
          AND (failure_type IS NULL OR failure_type NOT IN ({", ".join("?" for _ in _NON_REGRESSION_FAILURE_TYPES)}))
        """,
        (board, created_at, *_NON_REGRESSION_FAILURE_TYPES),
    ).fetchone()
    unexpected_hard_failures = int((hard_failure_row["count"] if hard_failure_row else 0) or 0)

    metrics = {
        "fingerprint_recurrences": fingerprint_recurrences,
        "unexpected_hard_failures": unexpected_hard_failures,
    }
    if fingerprint_recurrences > 0:
        return RolloutEvaluation(
            action="revert" if confirmation else "pause",
            reason="fingerprint_recurred",
            post_fix_metrics=metrics,
        )
    if unexpected_hard_failures > 0:
        return RolloutEvaluation(
            action="revert" if confirmation else "pause",
            reason="unexpected_hard_failures",
            post_fix_metrics=metrics,
        )
    return RolloutEvaluation(
        action="clear" if confirmation else "monitor",
        reason="no_regression_signal",
        post_fix_metrics=metrics,
    )


def refresh_active_repair_rollouts_index(*, output_root: str | Path, rollouts: list[dict]) -> Path:
    root = Path(output_root)
    audit_dir = root / "_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / "active_repair_rollouts.md"

    lines = ["# Active Repair Rollouts", ""]
    if not rollouts:
        lines.append("No active repair rollouts.")
    else:
        for rollout in rollouts:
            baseline = _parse_json_object(rollout.get("baseline_metrics_json"))
            fingerprint = str(baseline.get("fingerprint") or "").strip() or "unknown"
            board = str(baseline.get("board") or "").strip() or "unknown"
            phase = str(baseline.get("phase") or "").strip() or "unknown"
            lines.append(
                f"- rollout `{int(rollout.get('id') or 0)}` | commit `{rollout.get('commit_sha') or ''}` | "
                f"status `{rollout.get('status') or ''}` | scope `{board}:{phase}` | fingerprint `{fingerprint}`"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
