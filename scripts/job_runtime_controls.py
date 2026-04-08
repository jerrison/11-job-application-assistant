"""Runtime flag and repair queue control helpers for the job DB."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

REPAIR_QUEUE_PAUSE_FLAG = "repair_pause_new_queued_work"
_PAUSEABLE_QUEUE_STATUSES = ("queued", "queued_submit")
_RETRY_AFTER_SENTINEL = "1970-01-01 00:00:00"


def set_runtime_flag(conn: sqlite3.Connection, key: str, value: dict | list | str) -> None:
    serialized = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    conn.execute(
        """
        INSERT INTO runtime_flags (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = CURRENT_TIMESTAMP
        """,
        (str(key).strip(), serialized),
    )
    conn.commit()


def get_runtime_flag(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM runtime_flags WHERE key = ?", (str(key).strip(),)).fetchone()
    return str(row["value"]) if row else None


def get_runtime_flag_json(conn: sqlite3.Connection, key: str) -> dict | list | None:
    raw = get_runtime_flag(conn, key)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def delete_runtime_flag(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM runtime_flags WHERE key = ?", (str(key).strip(),))
    conn.commit()


def set_repair_queue_pause(
    conn: sqlite3.Connection,
    *,
    rollout_id: int,
    commit_sha: str,
    cluster_id: int,
    fingerprint: str,
    reason: str,
) -> None:
    set_runtime_flag(
        conn,
        REPAIR_QUEUE_PAUSE_FLAG,
        {
            "rollout_id": int(rollout_id),
            "commit_sha": str(commit_sha),
            "cluster_id": int(cluster_id),
            "fingerprint": str(fingerprint),
            "reason": str(reason),
            "paused_at": datetime.now(UTC).isoformat(),
        },
    )


def get_repair_queue_pause(conn: sqlite3.Connection) -> dict | None:
    payload = get_runtime_flag_json(conn, REPAIR_QUEUE_PAUSE_FLAG)
    return payload if isinstance(payload, dict) else None


def repair_queue_pause_active(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM runtime_flags WHERE key = ? LIMIT 1", (REPAIR_QUEUE_PAUSE_FLAG,)).fetchone()
    return row is not None


def clear_repair_queue_pause(conn: sqlite3.Connection) -> None:
    delete_runtime_flag(conn, REPAIR_QUEUE_PAUSE_FLAG)


def claim_pending_job(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    expected_status: str,
    expected_progress: str,
    claim_status: str,
    claim_progress: str,
) -> bool:
    params: list[int | str] = [
        claim_status,
        claim_progress,
        _RETRY_AFTER_SENTINEL,
        job_id,
        expected_status,
        expected_progress,
    ]
    sql = (
        "UPDATE jobs SET status = ?, progress = ?, retry_after = ? "
        "WHERE id = ? AND status = ? AND COALESCE(progress, '') = ?"
    )
    if expected_status in _PAUSEABLE_QUEUE_STATUSES:
        sql += " AND NOT EXISTS (SELECT 1 FROM runtime_flags WHERE key = ?)"
        params.append(REPAIR_QUEUE_PAUSE_FLAG)
    cur = conn.execute(sql, tuple(params))
    conn.commit()
    return cur.rowcount > 0
