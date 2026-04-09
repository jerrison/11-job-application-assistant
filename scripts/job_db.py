"""SQLite database layer for job queue management."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import job_runtime_controls as _runtime_controls
from app_paths import code_root, output_root
from confirmation_email_reply_backfill import maybe_backfill_confirmation_email_reply
from job_normalization import (
    company_match_variants,
    jd_fingerprint,
    normalize_role_title,
)
from job_normalization import (
    normalize_company as _normalize_company,
)

normalize_company = _normalize_company
claim_pending_job = _runtime_controls.claim_pending_job
clear_repair_queue_pause = _runtime_controls.clear_repair_queue_pause
delete_runtime_flag = _runtime_controls.delete_runtime_flag
get_repair_queue_pause = _runtime_controls.get_repair_queue_pause
get_runtime_flag = _runtime_controls.get_runtime_flag
get_runtime_flag_json = _runtime_controls.get_runtime_flag_json
repair_queue_pause_active = _runtime_controls.repair_queue_pause_active
set_repair_queue_pause = _runtime_controls.set_repair_queue_pause
set_runtime_flag = _runtime_controls.set_runtime_flag

log = logging.getLogger(__name__)
PROJECT_ROOT = code_root()
OUTPUT_ROOT = output_root()

RETRY_AFTER_SENTINEL = "1970-01-01 00:00:00"

JOB_STATUSES = (
    "queued",
    "queued_submit",
    "resolving",
    "generating",
    "autofilling",
    "draft",
    "approved",
    "submitting",
    "reanswering",
    "awaiting_captcha",
    "submitted",
    "retrying",
    "fix_in_progress",
    "regenerating",
    "stopped",
    "needs_board_url",
    "archived",
)

QUEUE_QUEUED_STATUSES = ("queued", "queued_submit")
QUEUE_PROCESSING_STATUSES = (
    "approved",
    "generating",
    "resolving",
    "submitting",
    "autofilling",
    "retrying",
    "fix_in_progress",
    "regenerating",
    "reanswering",
)
QUEUE_STOPPED_STATUSES = ("stopped", "needs_board_url", "awaiting_captcha")
STALE_PROCESSING_REPAIR_STATUSES = ("resolving", "generating", "autofilling")
QUEUE_STATUS_GROUPS: dict[str, tuple[str, ...]] = {
    "queued": QUEUE_QUEUED_STATUSES,
    "processing": QUEUE_PROCESSING_STATUSES,
    "draft": ("draft",),
    "submitted": ("submitted",),
    "stopped": QUEUE_STOPPED_STATUSES,
}
DUPLICATE_RECONCILE_ELIGIBLE_STATUSES = frozenset((*QUEUE_QUEUED_STATUSES, "draft", "stopped", "needs_board_url"))

SUBMISSION_LOCK_STATES = ("open", "locked", "unlocked_for_resubmit")
RERUNNABLE_PIPELINE_STATUSES = frozenset(
    {
        "queued",
        "queued_submit",
        "approved",
        "reanswering",
        "resolving",
        "generating",
        "autofilling",
        "draft",
        "submitting",
        "regenerating",
    }
)
LOCK_REPAIR_RESETTABLE_STATUSES = frozenset(
    set(RERUNNABLE_PIPELINE_STATUSES) | {"awaiting_captcha", "retrying", "fix_in_progress"}
)
LEGACY_REVIEW_ONLY_DRAFT_RESCUE_STATUSES = frozenset({"draft", "stopped", "submitted", "awaiting_captcha"})


class SubmissionLockError(RuntimeError):
    """Raised when a submitted-and-locked job is asked to re-enter the pipeline."""


def _effective_submission_lock_state_sql() -> str:
    return (
        "CASE "
        "WHEN submission_lock_state = 'unlocked_for_resubmit' THEN 'unlocked_for_resubmit' "
        "WHEN confirmed_at IS NOT NULL THEN 'locked' "
        "ELSE COALESCE(submission_lock_state, 'open') "
        "END"
    )


_SCHEMA = """\
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL,
    source          TEXT,
    source_url      TEXT,
    board_url       TEXT,
    canonical_url   TEXT UNIQUE,
    company         TEXT,
    role_title      TEXT,
    board           TEXT,
    status          TEXT NOT NULL DEFAULT 'queued',
    submission_lock_state   TEXT NOT NULL DEFAULT 'open',
    resubmit_count          INTEGER NOT NULL DEFAULT 0,
    last_resubmit_unlocked_at TIMESTAMP,
    last_resubmit_unlock_initiator TEXT,
    last_resubmit_confirmed_at TIMESTAMP,
    priority        INTEGER DEFAULT 0,
    provider        TEXT,
    output_dir      TEXT,
    notion_url      TEXT,
    error_message   TEXT,
    failure_type    TEXT,
    auth_state      TEXT,
    auth_scope      TEXT,
    progress        TEXT,
    fix_attempts    INTEGER DEFAULT 0,
    retry_after     TIMESTAMP NOT NULL DEFAULT '1970-01-01 00:00:00',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at    TIMESTAMP
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs(id),
    event_type      TEXT NOT NULL,
    detail          TEXT,
    detail_json     TEXT,
    initiator       TEXT,
    process_info    TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fix_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs(id),
    error_type      TEXT,
    error_context   TEXT,
    fix_diff        TEXT,
    fix_branch      TEXT,
    tests_passed    BOOLEAN,
    applied         BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS provider_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs(id),
    provider        TEXT NOT NULL,
    phase           TEXT,
    exit_code       INTEGER,
    duration_ms     INTEGER,
    error_message   TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TRIGGER IF NOT EXISTS trg_jobs_updated_at AFTER UPDATE ON jobs
BEGIN
    UPDATE jobs SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TABLE IF NOT EXISTS job_phase_durations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id),
    phase       TEXT NOT NULL,
    started_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at    TIMESTAMP,
    duration_ms INTEGER,
    exit_code   INTEGER,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS field_corrections (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id            INTEGER NOT NULL REFERENCES jobs(id),
    field_name        TEXT NOT NULL,
    original_value    TEXT,
    corrected_value   TEXT,
    correction_source TEXT NOT NULL,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS job_metrics (
    job_id              INTEGER PRIMARY KEY REFERENCES jobs(id),
    total_fields        INTEGER DEFAULT 0,
    fields_corrected    INTEGER DEFAULT 0,
    field_error_rate    REAL DEFAULT 0.0,
    manual_interventions INTEGER DEFAULT 0,
    auto_fix_attempts   INTEGER DEFAULT 0,
    total_duration_ms   INTEGER DEFAULT 0,
    phase_count         INTEGER DEFAULT 0,
    retry_count         INTEGER DEFAULT 0,
    audit_attempts      INTEGER DEFAULT 0,
    audit_failure_count INTEGER DEFAULT 0,
    rendered_audit_failures INTEGER DEFAULT 0,
    last_repair_cluster_id INTEGER,
    last_rollout_sha    TEXT,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS repair_clusters (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint         TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL DEFAULT 'open',
    eligibility         TEXT NOT NULL DEFAULT 'unknown',
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    representative_job_ids TEXT NOT NULL DEFAULT '[]',
    latest_summary      TEXT NOT NULL DEFAULT '',
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS repair_rollouts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id          INTEGER NOT NULL REFERENCES repair_clusters(id),
    commit_sha          TEXT NOT NULL,
    status              TEXT NOT NULL,
    baseline_metrics_json TEXT NOT NULL DEFAULT '{}',
    post_fix_metrics_json TEXT NOT NULL DEFAULT '{}',
    revert_sha          TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runtime_flags (
    key                 TEXT PRIMARY KEY,
    value               TEXT NOT NULL,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS candidate_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    job_url TEXT NOT NULL UNIQUE,
    application_url TEXT,
    location TEXT,
    salary TEXT,
    job_type TEXT,
    job_level TEXT,
    is_remote INTEGER,
    date_posted TEXT,
    job_description TEXT,
    company_industry TEXT,
    company_rating REAL,
    score INTEGER,
    score_reason TEXT,
    status TEXT DEFAULT 'new',
    promoted_job_id INTEGER,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    scored_at TIMESTAMP,
    promoted_at TIMESTAMP,
    jd_fingerprint TEXT
);
"""
# Known board URL patterns for fallback when url_resolver is not yet available
_KNOWN_BOARD_DOMAINS = (
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.ashbyhq.com",
    "jobs.lever.co",
    "app.dover.com",
    "myworkdayjobs.com",
    "myworkdaysite.com",
    "jobs.bytedance.com",
    "joinbytedance.com",
)

_AGGREGATOR_DOMAINS = (
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "dice.com",
)


def _fallback_detect_source(url: str) -> str:
    """Detect source from URL when url_resolver is not available."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or ""
    for domain in _AGGREGATOR_DOMAINS:
        if host.endswith(domain):
            return domain.split(".")[0]  # e.g. "linkedin"
    return "direct"


def _fallback_is_known_board_url(url: str) -> bool:
    """Check if URL is a known board URL when url_resolver is not available."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or ""
    return any(host.endswith(domain) for domain in _KNOWN_BOARD_DOMAINS)


def _submission_lock_state(row: sqlite3.Row | dict | None) -> str:
    if not row:
        return "open"
    row_keys = row.keys()
    state = str((row["submission_lock_state"] if "submission_lock_state" in row_keys else None) or "").strip()
    if state == "unlocked_for_resubmit":
        return state
    confirmed_at = row["confirmed_at"] if "confirmed_at" in row_keys else None
    if confirmed_at:
        return "locked"
    if state in SUBMISSION_LOCK_STATES:
        return state
    return "open"


def backfill_submission_locks(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE jobs SET submission_lock_state = CASE "
        "WHEN confirmed_at IS NOT NULL THEN 'locked' ELSE 'open' END "
        "WHERE TRIM(COALESCE(submission_lock_state, '')) NOT IN ('open','locked','unlocked_for_resubmit')"
    )
    conn.execute(
        "UPDATE jobs SET submission_lock_state = 'locked' "
        "WHERE confirmed_at IS NOT NULL AND COALESCE(submission_lock_state, 'open') = 'open'"
    )
    conn.execute(
        "UPDATE jobs SET submission_lock_state = 'locked' "
        "WHERE status = 'submitted' AND COALESCE(submission_lock_state, 'open') = 'open'"
    )
    conn.execute(
        "UPDATE jobs SET submission_lock_state = 'open' WHERE confirmed_at IS NULL AND submission_lock_state IS NULL"
    )
    conn.commit()


def _status_requires_submission_unlock(status: str) -> bool:
    return status in RERUNNABLE_PIPELINE_STATUSES


def repair_submission_locked_job(conn: sqlite3.Connection, job_id: int, *, initiator: str = "system") -> bool:
    resettable_statuses = tuple(sorted(LOCK_REPAIR_RESETTABLE_STATUSES))
    placeholders = ",".join("?" * len(resettable_statuses))
    effective_lock_state = _effective_submission_lock_state_sql()
    cur = conn.execute(
        "UPDATE jobs SET status = 'submitted', provider = NULL, progress = '', retry_after = ?, "
        "submission_lock_state = 'locked', completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP), "
        "failure_type = NULL, auth_state = NULL, auth_scope = NULL, error_message = '' "
        f"WHERE id = ? AND {effective_lock_state} = 'locked' "
        f"AND status IN ({placeholders})",
        (RETRY_AFTER_SENTINEL, job_id, *resettable_statuses),
    )
    conn.commit()
    if cur.rowcount > 0:
        log_event(conn, job_id, "submission_lock_repaired", initiator=initiator)
    return cur.rowcount > 0


def enforce_submission_lock(conn: sqlite3.Connection, job_id: int, *, target_status: str) -> None:
    if not _status_requires_submission_unlock(target_status):
        return
    job_columns = _table_columns(conn, "jobs")
    select_columns = ["id", "status"]
    if "confirmed_at" in job_columns:
        select_columns.append("confirmed_at")
    if "submission_lock_state" in job_columns:
        select_columns.append("submission_lock_state")
    row = conn.execute(
        f"SELECT {', '.join(select_columns)} FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if not row or _submission_lock_state(row) != "locked":
        return
    if row["status"] != "submitted":
        repair_submission_locked_job(conn, job_id, initiator="system")
    raise SubmissionLockError(
        f"Job #{job_id} was already submitted and is locked. Unlock it before redrafting or resubmitting."
    )


def unlock_job_for_resubmit(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    initiator: str,
    event_detail_json: dict | None = None,
    process_info: str | None = None,
) -> bool:
    effective_lock_state = _effective_submission_lock_state_sql()
    cur = conn.execute(
        "UPDATE jobs SET submission_lock_state = 'unlocked_for_resubmit', "
        "last_resubmit_unlocked_at = CURRENT_TIMESTAMP, "
        "last_resubmit_unlock_initiator = ? "
        f"WHERE id = ? AND {effective_lock_state} = 'locked'",
        (initiator, job_id),
    )
    conn.commit()
    if cur.rowcount > 0:
        log_event(
            conn,
            job_id,
            "submission_unlocked_for_resubmit",
            detail_json=event_detail_json,
            initiator=initiator,
            process_info=process_info,
        )
    return cur.rowcount > 0


def lock_job_for_resubmit(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    initiator: str,
    event_detail_json: dict | None = None,
    process_info: str | None = None,
) -> bool:
    effective_lock_state = _effective_submission_lock_state_sql()
    cur = conn.execute(
        "UPDATE jobs SET submission_lock_state = 'locked' "
        f"WHERE id = ? AND {effective_lock_state} = 'unlocked_for_resubmit'",
        (job_id,),
    )
    conn.commit()
    if cur.rowcount > 0:
        log_event(
            conn,
            job_id,
            "submission_relocked",
            detail_json=event_detail_json,
            initiator=initiator,
            process_info=process_info,
        )
    return cur.rowcount > 0


def record_confirmed_submission(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    confirmed_at: str | None,
    initiator: str | None = None,
) -> bool:
    started_immediate = False
    if not conn.in_transaction:
        # Serialize confirmation transitions across connections so only one caller
        # can consume an unlocked-for-resubmit window.
        conn.execute("BEGIN IMMEDIATE")
        started_immediate = True
    try:
        row = conn.execute(
            "SELECT status, confirmed_at, submission_lock_state, last_resubmit_confirmed_at, "
            "completed_at, error_message, failure_type, auth_state, auth_scope, progress, retry_after "
            "FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if not row:
            if started_immediate and conn.in_transaction:
                conn.rollback()
            return False

        first_confirmed = row["confirmed_at"]
        submission_lock_state = str(row["submission_lock_state"] or "").strip()
        is_resubmission = submission_lock_state == "unlocked_for_resubmit"
        needs_status = row["status"] != "submitted"
        needs_completed_at = row["completed_at"] is None or needs_status or is_resubmission
        needs_lock = submission_lock_state != "locked"
        needs_error_clear = bool(str(row["error_message"] or "").strip())
        needs_failure_clear = row["failure_type"] is not None
        needs_auth_state_clear = row["auth_state"] is not None
        needs_auth_scope_clear = row["auth_scope"] is not None
        needs_progress_clear = bool(str(row["progress"] or "").strip())
        needs_retry_reset = str(row["retry_after"] or RETRY_AFTER_SENTINEL) != RETRY_AFTER_SENTINEL
        needs_first_confirmed = first_confirmed is None and confirmed_at is not None
        needs_last_resubmit_confirmed = (
            is_resubmission
            and confirmed_at is not None
            and str(row["last_resubmit_confirmed_at"] or "").strip() != confirmed_at
        )
        needs_resubmit_count = is_resubmission

        if not any(
            (
                needs_status,
                needs_completed_at,
                needs_lock,
                needs_error_clear,
                needs_failure_clear,
                needs_auth_state_clear,
                needs_auth_scope_clear,
                needs_progress_clear,
                needs_retry_reset,
                needs_first_confirmed,
                needs_last_resubmit_confirmed,
                needs_resubmit_count,
            )
        ):
            if started_immediate and conn.in_transaction:
                conn.rollback()
            return False

        sets: list[str] = []
        params: list[object] = []
        if needs_status:
            sets.append("status = 'submitted'")
        if needs_completed_at:
            sets.append("completed_at = CURRENT_TIMESTAMP")
        if needs_lock:
            sets.append("submission_lock_state = 'locked'")
        if needs_error_clear:
            sets.append("error_message = NULL")
        if needs_failure_clear:
            sets.append("failure_type = NULL")
        if needs_auth_state_clear:
            sets.append("auth_state = NULL")
        if needs_auth_scope_clear:
            sets.append("auth_scope = NULL")
        if needs_progress_clear:
            sets.append("progress = ''")
        if needs_retry_reset:
            sets.append("retry_after = ?")
            params.append(RETRY_AFTER_SENTINEL)
        if first_confirmed is None and confirmed_at:
            sets.append("confirmed_at = ?")
            params.append(confirmed_at)
        if needs_last_resubmit_confirmed:
            sets.append("last_resubmit_confirmed_at = ?")
            params.append(confirmed_at)
        if needs_resubmit_count:
            sets.append("resubmit_count = COALESCE(resubmit_count, 0) + 1")

        params.append(job_id)
        cur = conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
    except Exception:
        if started_immediate and conn.in_transaction:
            conn.rollback()
        raise
    if cur.rowcount <= 0:
        return False
    if needs_status or needs_lock or is_resubmission:
        log_event(conn, job_id, "submission_locked", detail=confirmed_at, initiator=initiator)
    if needs_resubmit_count:
        log_event(conn, job_id, "resubmitted", detail=confirmed_at, initiator=initiator)
    return True


def _create_index_if_possible(conn: sqlite3.Connection, sql: str) -> None:
    deadline = time.monotonic() + 1.0
    while True:
        try:
            conn.execute(sql)
            return
        except sqlite3.OperationalError as exc:
            normalized = str(exc).lower()
            if "already exists" in normalized or "duplicate" in normalized:
                return
            if "database is locked" in normalized and time.monotonic() < deadline:
                time.sleep(0.05)
                continue
            raise


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {row["name"] for row in rows}


def _table_has_columns(conn: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> bool:
    if not columns:
        return True
    existing = _table_columns(conn, table)
    return all(column in existing for column in columns)


_MIGRATIONS = [
    ("progress", "ALTER TABLE jobs ADD COLUMN progress TEXT"),
    ("confirmation_method", "ALTER TABLE jobs ADD COLUMN confirmation_method TEXT"),
    ("confirmed_at", "ALTER TABLE jobs ADD COLUMN confirmed_at TIMESTAMP"),
    (
        "submission_lock_state",
        "ALTER TABLE jobs ADD COLUMN submission_lock_state TEXT NOT NULL DEFAULT 'open'",
    ),
    ("resubmit_count", "ALTER TABLE jobs ADD COLUMN resubmit_count INTEGER NOT NULL DEFAULT 0"),
    ("last_resubmit_unlocked_at", "ALTER TABLE jobs ADD COLUMN last_resubmit_unlocked_at TIMESTAMP"),
    ("last_resubmit_unlock_initiator", "ALTER TABLE jobs ADD COLUMN last_resubmit_unlock_initiator TEXT"),
    ("last_resubmit_confirmed_at", "ALTER TABLE jobs ADD COLUMN last_resubmit_confirmed_at TIMESTAMP"),
    ("email_confirmed", "ALTER TABLE jobs ADD COLUMN email_confirmed BOOLEAN DEFAULT FALSE"),
    ("notion_sync_status", "ALTER TABLE jobs ADD COLUMN notion_sync_status TEXT"),
    ("notion_page_id", "ALTER TABLE jobs ADD COLUMN notion_page_id TEXT"),
    ("total_form_fields", "ALTER TABLE jobs ADD COLUMN total_form_fields INTEGER"),
    ("fields_filled", "ALTER TABLE jobs ADD COLUMN fields_filled INTEGER"),
    ("fields_skipped", "ALTER TABLE jobs ADD COLUMN fields_skipped INTEGER"),
    ("fields_errored", "ALTER TABLE jobs ADD COLUMN fields_errored INTEGER"),
    ("archived", "ALTER TABLE jobs ADD COLUMN archived BOOLEAN DEFAULT FALSE"),
    ("jd_fingerprint", "ALTER TABLE jobs ADD COLUMN jd_fingerprint TEXT"),
    ("failure_type", "ALTER TABLE jobs ADD COLUMN failure_type TEXT"),
    ("auth_state", "ALTER TABLE jobs ADD COLUMN auth_state TEXT"),
    ("auth_scope", "ALTER TABLE jobs ADD COLUMN auth_scope TEXT"),
    ("retry_after", f"ALTER TABLE jobs ADD COLUMN retry_after TIMESTAMP NOT NULL DEFAULT '{RETRY_AFTER_SENTINEL}'"),
]

_EVENT_MIGRATIONS = [
    ("initiator", "ALTER TABLE events ADD COLUMN initiator TEXT"),
    ("process_info", "ALTER TABLE events ADD COLUMN process_info TEXT"),
]

_METRICS_MIGRATIONS = [
    ("audit_attempts", "ALTER TABLE job_metrics ADD COLUMN audit_attempts INTEGER DEFAULT 0"),
    ("audit_failure_count", "ALTER TABLE job_metrics ADD COLUMN audit_failure_count INTEGER DEFAULT 0"),
    ("rendered_audit_failures", "ALTER TABLE job_metrics ADD COLUMN rendered_audit_failures INTEGER DEFAULT 0"),
    ("last_repair_cluster_id", "ALTER TABLE job_metrics ADD COLUMN last_repair_cluster_id INTEGER"),
    ("last_rollout_sha", "ALTER TABLE job_metrics ADD COLUMN last_rollout_sha TEXT"),
    ("llm_generated_answers", "ALTER TABLE job_metrics ADD COLUMN llm_generated_answers INTEGER DEFAULT 0"),
    ("llm_generated_labels", "ALTER TABLE job_metrics ADD COLUMN llm_generated_labels TEXT"),
]


def _normalize_job_metrics_schema(conn: sqlite3.Connection) -> None:
    columns = {
        str(row["name"]): str(row["type"] or "").strip().upper()
        for row in conn.execute("PRAGMA table_info(job_metrics)").fetchall()
    }
    if columns.get("last_repair_cluster_id") == "INTEGER":
        return
    conn.executescript(
        """
        ALTER TABLE job_metrics RENAME TO job_metrics_old;
        CREATE TABLE job_metrics (
            job_id              INTEGER PRIMARY KEY REFERENCES jobs(id),
            total_fields        INTEGER DEFAULT 0,
            fields_corrected    INTEGER DEFAULT 0,
            field_error_rate    REAL DEFAULT 0.0,
            manual_interventions INTEGER DEFAULT 0,
            auto_fix_attempts   INTEGER DEFAULT 0,
            total_duration_ms   INTEGER DEFAULT 0,
            phase_count         INTEGER DEFAULT 0,
            retry_count         INTEGER DEFAULT 0,
            audit_attempts      INTEGER DEFAULT 0,
            audit_failure_count INTEGER DEFAULT 0,
            rendered_audit_failures INTEGER DEFAULT 0,
            last_repair_cluster_id INTEGER,
            last_rollout_sha    TEXT,
            llm_generated_answers INTEGER DEFAULT 0,
            llm_generated_labels TEXT,
            updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO job_metrics (
            job_id,
            total_fields,
            fields_corrected,
            field_error_rate,
            manual_interventions,
            auto_fix_attempts,
            total_duration_ms,
            phase_count,
            retry_count,
            audit_attempts,
            audit_failure_count,
            rendered_audit_failures,
            last_repair_cluster_id,
            last_rollout_sha,
            llm_generated_answers,
            llm_generated_labels,
            updated_at
        )
        SELECT
            job_id,
            total_fields,
            fields_corrected,
            field_error_rate,
            manual_interventions,
            auto_fix_attempts,
            total_duration_ms,
            phase_count,
            retry_count,
            audit_attempts,
            audit_failure_count,
            rendered_audit_failures,
            CASE
                WHEN TRIM(COALESCE(last_repair_cluster_id, '')) = '' THEN NULL
                ELSE CAST(last_repair_cluster_id AS INTEGER)
            END,
            last_rollout_sha,
            llm_generated_answers,
            llm_generated_labels,
            updated_at
        FROM job_metrics_old;
        DROP TABLE job_metrics_old;
        """
    )


_JOB_INDEXES = [
    ("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)", "jobs", ("status",)),
    ("CREATE INDEX IF NOT EXISTS idx_jobs_board ON jobs(board)", "jobs", ("board",)),
    ("CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source)", "jobs", ("source",)),
    ("CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at)", "jobs", ("created_at",)),
    (
        "CREATE INDEX IF NOT EXISTS idx_jobs_archived_updated ON jobs(archived, updated_at DESC)",
        "jobs",
        ("archived", "updated_at"),
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_jobs_archived_status_updated ON jobs(archived, status, updated_at DESC)",
        "jobs",
        ("archived", "status", "updated_at"),
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_jobs_archived_board_updated ON jobs(archived, board, updated_at DESC)",
        "jobs",
        ("archived", "board", "updated_at"),
    ),
    ("CREATE INDEX IF NOT EXISTS idx_events_job_id ON events(job_id)", "events", ("job_id",)),
    (
        "CREATE INDEX IF NOT EXISTS idx_events_job_created ON events(job_id, created_at DESC)",
        "events",
        ("job_id", "created_at"),
    ),
    ("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)", "events", ("event_type",)),
    (
        "CREATE INDEX IF NOT EXISTS idx_events_job_type_detail_id ON events(job_id, event_type, detail, id DESC)",
        "events",
        ("job_id", "event_type", "detail", "id"),
    ),
    ("CREATE INDEX IF NOT EXISTS idx_fix_attempts_job_id ON fix_attempts(job_id)", "fix_attempts", ("job_id",)),
    ("CREATE INDEX IF NOT EXISTS idx_provider_runs_job_id ON provider_runs(job_id)", "provider_runs", ("job_id",)),
    (
        "CREATE INDEX IF NOT EXISTS idx_phase_durations_job_id ON job_phase_durations(job_id)",
        "job_phase_durations",
        ("job_id",),
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_phase_durations_phase ON job_phase_durations(phase)",
        "job_phase_durations",
        ("phase",),
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_field_corrections_job_id ON field_corrections(job_id)",
        "field_corrections",
        ("job_id",),
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_field_corrections_source ON field_corrections(correction_source)",
        "field_corrections",
        ("correction_source",),
    ),
    ("CREATE INDEX IF NOT EXISTS idx_candidate_status ON candidate_jobs(status)", "candidate_jobs", ("status",)),
    ("CREATE INDEX IF NOT EXISTS idx_candidate_score ON candidate_jobs(score DESC)", "candidate_jobs", ("score",)),
    ("CREATE INDEX IF NOT EXISTS idx_candidate_source ON candidate_jobs(source)", "candidate_jobs", ("source",)),
]


# ── Connection tracking ──────────────────────────────────────────────────
# Shared registry so any process (web, draft, worker) can track and close
# all open connections during graceful shutdown.
_connections: set[sqlite3.Connection] = set()
_conn_lock = threading.Lock()


class ManagedConnection(sqlite3.Connection):
    """SQLite connection with idempotent close and defensive GC cleanup."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._managed_closed = False

    def close(self) -> None:
        if self._managed_closed:
            return
        self._managed_closed = True
        with _conn_lock:
            _connections.discard(self)
        super().close()

    def __del__(self) -> None:
        if not getattr(self, "_managed_closed", True):
            try:
                self.close()
            except Exception:
                pass


_RAW_SQLITE_CONNECT = sqlite3.connect


def managed_sqlite_connect(*args, **kwargs) -> sqlite3.Connection:
    """Default sqlite3.connect wrapper that uses ManagedConnection."""
    kwargs.setdefault("factory", ManagedConnection)
    return _RAW_SQLITE_CONNECT(*args, **kwargs)


sqlite3.connect = managed_sqlite_connect


def _connectable_db_path(db_path: Path | str) -> str:
    raw_path = str(db_path)
    if raw_path == ":memory:" or raw_path.startswith("file:"):
        return raw_path
    path = Path(raw_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def open_db(db_path: Path | str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a lightweight DB connection (PRAGMAs only, no schema/migrations).

    Use this for per-thread connections after ``init_db`` has already been
    called once to set up the schema.
    """
    conn = sqlite3.connect(
        _connectable_db_path(db_path),
        check_same_thread=check_same_thread,
        timeout=30,
        factory=ManagedConnection,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA wal_autocheckpoint=4000")  # mitigate SQLite 3.50.4 WAL-reset bug
    return conn


def open_db_tracked(db_path: Path | str, **kwargs) -> sqlite3.Connection:
    """Open a DB connection via open_db() and register it for shutdown tracking."""
    conn = open_db(db_path, **kwargs)
    with _conn_lock:
        _connections.add(conn)
    return conn


def close_all_connections() -> None:
    """Close all tracked connections. Called during shutdown."""
    with _conn_lock:
        snapshot = list(_connections)
        _connections.clear()
    for conn in snapshot:
        try:
            conn.close()
        except Exception:
            pass


def init_db(db_path: Path | str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(
        _connectable_db_path(db_path),
        check_same_thread=check_same_thread,
        timeout=30,
        factory=ManagedConnection,
    )
    conn.row_factory = sqlite3.Row

    # Warn about known SQLite WAL-reset data race (fixed in 3.50.7).
    _ver = tuple(int(x) for x in sqlite3.sqlite_version.split("."))
    if _ver < (3, 50, 7):
        log.warning(
            "SQLite %s has a known WAL-reset data race (fixed in 3.50.7). Using wal_autocheckpoint=4000 as mitigation.",
            sqlite3.sqlite_version,
        )

    # Integrity check — detect corruption before running migrations.
    # Full integrity_check (no limit) costs <50ms on a 5MB DB and catches
    # multi-table corruption that quick_check misses.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        result = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as exc:
        conn.close()
        log.error("DATABASE CORRUPTION DETECTED: %s", exc)
        log.error("Recovery: sqlite3 %s .recover | sqlite3 %s_recovered.db", db_path, db_path)
        raise RuntimeError(f"SQLite corruption: {exc}") from exc
    if result[0] != "ok":
        conn.close()
        log.error("DATABASE CORRUPTION DETECTED: %s", result[0])
        log.error("Recovery: sqlite3 %s .recover | sqlite3 %s_recovered.db", db_path, db_path)
        raise RuntimeError(f"SQLite corruption: {result[0]}")

    conn.executescript(_SCHEMA)
    # Run column migrations — each is idempotent (skips if column exists).
    # The inner try/except around ALTER guards against concurrent init_db
    # calls (e.g. web + worker startup overlap) where both see the column
    # missing and both try to ALTER — the second gets "duplicate column".
    for col_name, alter_sql in _MIGRATIONS:
        try:
            conn.execute(f"SELECT {col_name} FROM jobs LIMIT 0")
        except sqlite3.OperationalError:
            try:
                conn.execute(alter_sql)
            except sqlite3.OperationalError:
                pass
    for col_name, alter_sql in _EVENT_MIGRATIONS:
        try:
            conn.execute(f"SELECT {col_name} FROM events LIMIT 0")
        except sqlite3.OperationalError:
            try:
                conn.execute(alter_sql)
            except sqlite3.OperationalError:
                pass
    for col_name, alter_sql in _METRICS_MIGRATIONS:
        try:
            conn.execute(f"SELECT {col_name} FROM job_metrics LIMIT 0")
        except sqlite3.OperationalError:
            try:
                conn.execute(alter_sql)
            except sqlite3.OperationalError:
                pass
    _normalize_job_metrics_schema(conn)
    updated_at_added = False
    try:
        conn.execute("SELECT updated_at FROM jobs LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE jobs ADD COLUMN updated_at TIMESTAMP")
        updated_at_added = True
    needs_updated_at_repair = updated_at_added
    if not needs_updated_at_repair:
        updated_at_state = conn.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN updated_at IS NULL THEN 1 ELSE 0 END) AS null_count FROM jobs"
        ).fetchone()
        total_rows = int(updated_at_state["total"] or 0)
        null_rows = int(updated_at_state["null_count"] or 0)
        needs_updated_at_repair = total_rows > 0 and null_rows == total_rows
    if needs_updated_at_repair:
        try:
            conn.execute(
                "UPDATE jobs SET updated_at = COALESCE(created_at, CURRENT_TIMESTAMP) WHERE updated_at IS NULL"
            )
        except sqlite3.OperationalError as exc:
            normalized = str(exc).lower()
            if "no such column" in normalized and "created_at" in normalized:
                conn.execute("UPDATE jobs SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL")
            else:
                raise
    backfill_submission_locks(conn)
    for sql, table, columns in _JOB_INDEXES:
        if _table_has_columns(conn, table, columns):
            _create_index_if_possible(conn, sql)
    conn.execute("UPDATE jobs SET retry_after = ? WHERE retry_after IS NULL", (RETRY_AFTER_SENTINEL,))
    if _table_has_columns(conn, "jobs", ("status", "retry_after")):
        _create_index_if_possible(
            conn, "CREATE INDEX IF NOT EXISTS idx_jobs_status_retry_after ON jobs(status, retry_after)"
        )
    if _table_has_columns(conn, "jobs", ("board", "failure_type", "auth_scope", "updated_at")):
        _create_index_if_possible(
            conn,
            "CREATE INDEX IF NOT EXISTS idx_jobs_board_failure_scope ON jobs(board, failure_type, auth_scope, updated_at)",
        )
    if _table_has_columns(conn, "jobs", ("board", "failure_type", "auth_scope", "auth_state", "updated_at")):
        _create_index_if_possible(
            conn,
            "CREATE INDEX IF NOT EXISTS idx_jobs_board_failure_scope_state "
            "ON jobs(board, failure_type, auth_scope, auth_state, updated_at)",
        )
    conn.commit()
    # Candidate_jobs column migrations
    try:
        conn.execute("SELECT jd_fingerprint FROM candidate_jobs LIMIT 0")
    except sqlite3.OperationalError:
        try:
            conn.execute("ALTER TABLE candidate_jobs ADD COLUMN jd_fingerprint TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    if _table_has_columns(conn, "candidate_jobs", ("jd_fingerprint",)):
        _create_index_if_possible(
            conn,
            "CREATE INDEX IF NOT EXISTS idx_candidate_jd_fp ON candidate_jobs(jd_fingerprint)",
        )
    # Migrate legacy statuses to 'stopped'
    conn.execute(
        "UPDATE jobs SET status = 'stopped' WHERE status IN "
        "('failed', 'skipped_captcha', 'skipped_auth', 'needs_manual')"
    )
    conn.commit()
    # Migrate status='archived' to archived=TRUE flag + real status
    _migrate_archived_status(conn)

    # Probe every table to catch per-table corruption that integrity_check misses.
    _PROBE_TABLES = (
        "jobs",
        "events",
        "fix_attempts",
        "provider_runs",
        "job_phase_durations",
        "field_corrections",
        "job_metrics",
        "repair_clusters",
        "repair_rollouts",
        "runtime_flags",
        "candidate_jobs",
    )
    for table in _PROBE_TABLES:
        try:
            conn.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
        except sqlite3.DatabaseError as exc:
            conn.close()
            log.error("TABLE %s UNREADABLE: %s", table, exc)
            raise RuntimeError(f"Table {table} unreadable: {exc}") from exc

    return conn


def _find_duplicate_by_company_role(
    conn: sqlite3.Connection,
    company: str,
    role_title: str,
    *,
    exclude_job_id: int | None = None,
    older_than_job_id: int | None = None,
) -> dict | None:
    """Return an existing active job matching company+role normalization, or None."""
    if not company or not role_title:
        return None
    normalized_companies = company_match_variants(company)
    normalized_role_title = normalize_role_title(role_title)
    if not normalized_companies or not normalized_role_title:
        return None
    query = "SELECT * FROM jobs WHERE (archived IS NULL OR archived = FALSE)"
    params: list[object] = []
    if exclude_job_id is not None:
        query += " AND id != ?"
        params.append(exclude_job_id)
    if older_than_job_id is not None:
        query += " AND id < ?"
        params.append(older_than_job_id)
    query += " ORDER BY id ASC"
    rows = conn.execute(query, params).fetchall()
    for row in rows:
        row_company = str(row["company"] or "")
        row_role_title = str(row["role_title"] or "")
        if (
            company_match_variants(row_company) & normalized_companies
            and normalize_role_title(row_role_title) == normalized_role_title
        ):
            return dict(row)
    return None


def _find_locked_output_dir_owner(
    conn: sqlite3.Connection,
    output_dir: str | Path,
    *,
    exclude_job_id: int | None = None,
    older_than_job_id: int | None = None,
) -> dict | None:
    """Return the oldest submitted/locked row that owns the same repo-local output dir."""
    repo_local = _repo_local_output_dir(output_dir)
    query = "SELECT * FROM jobs WHERE output_dir IS NOT NULL"
    params: list[object] = []
    try:
        relative = repo_local.relative_to(OUTPUT_ROOT).as_posix()
    except ValueError:
        relative = None
    if relative:
        query += " AND output_dir LIKE ?"
        params.append(f"%/output/{relative}")
    if exclude_job_id is not None:
        query += " AND id != ?"
        params.append(exclude_job_id)
    if older_than_job_id is not None:
        query += " AND id < ?"
        params.append(older_than_job_id)
    query += " ORDER BY id ASC"
    rows = conn.execute(query, params).fetchall()
    for row in rows:
        candidate_output_dir = str(row["output_dir"] or "").strip()
        if not candidate_output_dir:
            continue
        if _repo_local_output_dir(candidate_output_dir) != repo_local:
            continue
        if _submission_lock_state(row) == "locked" or str(row["status"] or "").strip() == "submitted":
            return dict(row)
    return None


def _is_duplicate_by_company_role(conn: sqlite3.Connection, company: str, role_title: str) -> int | None:
    """Return the ID of an existing non-archived job matching company+role, or None."""
    duplicate = _find_duplicate_by_company_role(conn, company, role_title)
    return int(duplicate["id"]) if duplicate is not None else None


def _backfill_duplicate_url_metadata(
    conn: sqlite3.Connection,
    existing_id: int,
    *,
    company: str | None = None,
    role_title: str | None = None,
    source_url: str | None = None,
) -> None:
    row = conn.execute(
        "SELECT company, role_title, source_url FROM jobs WHERE id = ? LIMIT 1",
        (existing_id,),
    ).fetchone()
    if row is None:
        return

    updates: list[str] = []
    params: list[object] = []

    if company and not str(row["company"] or "").strip():
        updates.append("company = ?")
        params.append(company)
    if role_title and not str(row["role_title"] or "").strip():
        updates.append("role_title = ?")
        params.append(role_title)
    if source_url and not str(row["source_url"] or "").strip():
        updates.append("source_url = ?")
        params.append(source_url)

    if not updates:
        return

    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(existing_id)
    conn.execute(f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()


def _initial_canonical_job_url(url: str) -> str:
    """Return a best-effort canonical URL for duplicate checks without network fetches."""
    try:
        from job_board_urls import resolve_job_source_url
    except ImportError:
        return url

    def _no_network(*_args, **_kwargs):
        raise RuntimeError("network disabled for add-time canonicalization")

    try:
        canonical = resolve_job_source_url(
            url,
            opener=_no_network,
            embed_url_resolver=lambda _url: None,
        )
    except Exception:
        return url
    return str(canonical or url)


def _duplicate_url_identities(*identities: str | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in identities:
        value = str(raw or "").strip()
        if not value:
            continue
        for candidate in (value, _initial_canonical_job_url(value)):
            cleaned = str(candidate or "").strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            normalized.append(cleaned)
    return normalized


def _find_active_duplicate_by_url_identity(
    conn: sqlite3.Connection,
    *,
    identities: list[str],
    exclude_job_id: int | None = None,
    older_than_job_id: int | None = None,
) -> dict | None:
    """Return an active job matching any provided URL identity across url/board/canonical."""
    cleaned = _duplicate_url_identities(*identities)
    if not cleaned:
        return None
    # Preserve order while dropping duplicates.
    unique_identities = list(dict.fromkeys(cleaned))
    placeholders = ",".join("?" for _ in unique_identities)
    query = (
        "SELECT * FROM jobs WHERE (archived IS NULL OR archived = FALSE) "
        f"AND (url IN ({placeholders}) OR board_url IN ({placeholders}) OR canonical_url IN ({placeholders}))"
    )
    params: list[object] = [*unique_identities, *unique_identities, *unique_identities]
    if exclude_job_id is not None:
        query += " AND id != ?"
        params.append(exclude_job_id)
    if older_than_job_id is not None:
        query += " AND id < ?"
        params.append(older_than_job_id)
    query += " ORDER BY id ASC LIMIT 1"
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def find_duplicate_job_match(
    conn: sqlite3.Connection,
    *,
    url: str | None = None,
    source_url: str | None = None,
    board_url: str | None = None,
    canonical_url: str | None = None,
    company: str | None = None,
    role_title: str | None = None,
    jd_text: str | None = None,
    exclude_job_id: int | None = None,
    older_than_job_id: int | None = None,
) -> tuple[str, dict] | None:
    """Return the first matching duplicate job plus the match type.

    Match order mirrors the queue's duplicate policy:
    1. Active exact URL identity matches across url / board_url / canonical_url
    2. Active company+role matches with wrapper normalization
    3. JD fingerprint matches (active preferred, archived fallback)
    """
    url_duplicate = _find_active_duplicate_by_url_identity(
        conn,
        identities=[url or "", source_url or "", board_url or "", canonical_url or ""],
        exclude_job_id=exclude_job_id,
        older_than_job_id=older_than_job_id,
    )
    if url_duplicate is not None:
        return "url", url_duplicate

    company_role_duplicate = _find_duplicate_by_company_role(
        conn,
        company or "",
        role_title or "",
        exclude_job_id=exclude_job_id,
        older_than_job_id=older_than_job_id,
    )
    if company_role_duplicate is not None:
        return "company_role", company_role_duplicate

    jd_duplicate = check_jd_duplicate(
        conn,
        company or "",
        jd_text,
        exclude_job_id=exclude_job_id,
        older_than_job_id=older_than_job_id,
    )
    if jd_duplicate is not None:
        return "jd", jd_duplicate
    return None


def add_job(
    conn: sqlite3.Connection,
    url: str,
    *,
    priority: int = 0,
    provider: str | None = None,
    company: str | None = None,
    role_title: str | None = None,
    jd_text: str | None = None,
    source_override: str | None = None,
    source_url_override: str | None = None,
) -> int:
    try:
        from url_resolver import _is_known_board_url, detect_source
    except ImportError:
        detect_source = _fallback_detect_source
        _is_known_board_url = _fallback_is_known_board_url

    if (source_override is None) != (source_url_override is None):
        raise ValueError("source_override and source_url_override must be provided together")

    source = detect_source(url)
    canonical_url = _initial_canonical_job_url(url)
    if source == "direct":
        board_url = url
        source_url = None
    elif source != "unknown":
        # Aggregator URL (linkedin, indeed, etc.) — not yet resolved to a board
        board_url = None
        source_url = url
    elif _is_known_board_url(url):
        source = "direct"
        board_url = url
        source_url = None
    else:
        board_url = None
        source_url = url

    if source_override is not None:
        source = source_override
        # Caller-supplied source overrides preserve the explicit external URL contract.
        canonical_url = url
    if source_url_override is not None:
        source_url = source_url_override

    # ── Dedup: reject if an active job with the same company+role exists ──
    existing_id = _is_duplicate_by_company_role(conn, company, role_title)
    if existing_id is not None:
        log.info("Duplicate of job #%d (same company+role), skipping", existing_id)
        return -existing_id  # negative ID signals duplicate to callers

    # ── Dedup: reject if an active job with the same URL already exists ──
    # Check url, board_url, and canonical_url against existing non-archived jobs.
    for col, check_val in {
        "url": url,
        "board_url": board_url,
        "canonical_url": canonical_url,
    }.items():
        if not check_val:
            continue
        existing = conn.execute(
            f"SELECT id FROM jobs WHERE {col} = ? AND (archived IS NULL OR archived = FALSE) LIMIT 1",
            (check_val,),
        ).fetchone()
        if existing:
            _backfill_duplicate_url_metadata(
                conn,
                existing["id"],
                company=company,
                role_title=role_title,
                source_url=source_url,
            )
            raise sqlite3.IntegrityError(f"Duplicate job: existing #{existing['id']} has same {col}={check_val!r}")

    jd_fp = jd_fingerprint(company, jd_text) if company and jd_text else None
    if jd_fp is not None:
        existing = check_jd_duplicate(conn, company, jd_text)
        if existing is not None:
            log.info("Duplicate of job #%d (same JD fingerprint), skipping", existing["id"])
            return -int(existing["id"])

    cur = conn.execute(
        """INSERT INTO jobs (url, source, source_url, board_url,
           canonical_url, priority, provider, company, role_title, jd_fingerprint, status, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', CURRENT_TIMESTAMP)""",
        (url, source, source_url, board_url, canonical_url, priority, provider, company, role_title, jd_fp),
    )
    conn.commit()
    return cur.lastrowid


def check_jd_duplicate(
    conn: sqlite3.Connection,
    company: str,
    jd_text: str | None,
    *,
    exclude_job_id: int | None = None,
    older_than_job_id: int | None = None,
) -> dict | None:
    """Check if a job with the same company+JD fingerprint already exists.

    Returns the existing job dict if a duplicate is found, None otherwise.
    The exclude_job_id parameter allows skipping the job being checked itself.
    """
    fp = jd_fingerprint(company, jd_text)
    if not fp:
        return None
    query = "SELECT * FROM jobs WHERE jd_fingerprint = ?"
    params: list = [fp]
    if exclude_job_id is not None:
        query += " AND id != ?"
        params.append(exclude_job_id)
    if older_than_job_id is not None:
        query += " AND id < ?"
        params.append(older_than_job_id)
    query += " ORDER BY CASE WHEN archived IS NULL OR archived = FALSE THEN 0 ELSE 1 END, id ASC LIMIT 1"
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def set_jd_fingerprint(
    conn: sqlite3.Connection,
    job_id: int,
    company: str,
    jd_text: str | None,
) -> str | None:
    """Compute and store the JD fingerprint for a job. Returns the fingerprint."""
    fp = jd_fingerprint(company, jd_text)
    if fp:
        conn.execute("UPDATE jobs SET jd_fingerprint = ? WHERE id = ?", (fp, job_id))
        conn.commit()
    return fp


def _read_jd_from_output_dir(output_dir: str) -> str | None:
    """Read jd_raw.md from a job's output directory."""
    od = _repo_local_output_dir(output_dir)
    for candidate in (od / "content" / "jd_raw.md", od / "jd_raw.md"):
        if candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8")
            except OSError:
                pass
    return None


def _read_pipeline_meta_from_output_dir(output_dir: str | None) -> dict:
    if not output_dir:
        return {}
    od = _repo_local_output_dir(output_dir)
    for candidate in (od / "content" / ".pipeline_meta.json", od / ".pipeline_meta.json"):
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _repo_local_output_candidate(path: str | Path) -> Path | None:
    out = Path(path)
    resolved_root: Path | None = None
    try:
        resolved_out = out.resolve()
        resolved_root = OUTPUT_ROOT.resolve()
        if resolved_out.is_relative_to(resolved_root):
            return OUTPUT_ROOT / resolved_out.relative_to(resolved_root)
    except OSError:
        resolved_out = None

    parts = out.parts
    try:
        output_index = parts.index("output")
    except ValueError:
        return None

    relative_output = Path(*parts[output_index + 1 :])
    if not relative_output.parts:
        return None

    return OUTPUT_ROOT / relative_output


def _repo_local_output_dir(path: str | Path) -> Path:
    """Prefer the current repo-local output subtree over stale older-clone paths."""

    out = Path(path)
    repo_local = _repo_local_output_candidate(out)
    if repo_local is None:
        return out
    if repo_local == out:
        return out
    if repo_local.exists():
        return repo_local
    return out


def migrate_legacy_output_dirs(
    conn: sqlite3.Connection,
    *,
    statuses: tuple[str, ...] | None = None,
    limit: int | None = None,
    initiator: str = "system",
) -> dict[str, int]:
    """Copy legacy output trees into the current repo and rehome matching rows.

    Some long-lived queue rows still point at older clone paths such as
    ``/Users/.../00-projects/11-job-application-material-creation/output/...``.
    Those rows cannot participate in current-repo reruns or proof sync until their
    output tree exists under this repo's ``output/`` directory.
    """

    where_clauses = ["output_dir IS NOT NULL", "(archived IS NULL OR archived = FALSE)"]
    params: list[object] = []
    if statuses:
        placeholders = ",".join("?" * len(statuses))
        where_clauses.append(f"status IN ({placeholders})")
        params.extend(statuses)
    query = f"SELECT id, output_dir FROM jobs WHERE {' AND '.join(where_clauses)} ORDER BY id ASC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(query, params).fetchall()

    summary = {
        "scanned": 0,
        "migrated": 0,
        "repointed_existing": 0,
        "missing_legacy_dir": 0,
        "non_legacy": 0,
    }

    for row in rows:
        output_dir = str(row["output_dir"] or "").strip()
        if not output_dir:
            continue
        summary["scanned"] += 1
        legacy_out = Path(output_dir)
        repo_local = _repo_local_output_candidate(legacy_out)
        if repo_local is None or repo_local == legacy_out:
            summary["non_legacy"] += 1
            continue
        if repo_local.exists():
            conn.execute("UPDATE jobs SET output_dir = ? WHERE id = ?", (str(repo_local), int(row["id"])))
            log_event(
                conn,
                int(row["id"]),
                "output_dir_migrated",
                detail="repo_local_existing",
                detail_json={"from": str(legacy_out), "to": str(repo_local)},
                initiator=initiator,
            )
            summary["repointed_existing"] += 1
            continue
        if not legacy_out.exists():
            summary["missing_legacy_dir"] += 1
            continue
        repo_local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(legacy_out, repo_local, dirs_exist_ok=True)
        conn.execute("UPDATE jobs SET output_dir = ? WHERE id = ?", (str(repo_local), int(row["id"])))
        log_event(
            conn,
            int(row["id"]),
            "output_dir_migrated",
            detail="repo_local_copied",
            detail_json={"from": str(legacy_out), "to": str(repo_local)},
            initiator=initiator,
        )
        summary["migrated"] += 1

    if summary["migrated"] or summary["repointed_existing"]:
        conn.commit()
    return summary


def backfill_duplicate_match_metadata(conn: sqlite3.Connection) -> int:
    """Fill missing duplicate-match metadata from output artifacts for existing jobs."""
    rows = conn.execute(
        "SELECT id, board_url, company, role_title, board, output_dir, jd_fingerprint "
        "FROM jobs WHERE (archived IS NULL OR archived = FALSE) AND output_dir IS NOT NULL"
    ).fetchall()
    updated_rows = 0
    for row in rows:
        job = dict(row)
        meta = _read_pipeline_meta_from_output_dir(job.get("output_dir"))
        company = str(job.get("company") or meta.get("company_proper") or meta.get("company") or "").strip() or None
        role_title = str(job.get("role_title") or meta.get("role") or meta.get("job_title") or "").strip() or None
        board = str(job.get("board") or meta.get("board") or "").strip() or None
        board_url = str(job.get("board_url") or meta.get("board_url") or "").strip() or None
        jd_fp = str(job.get("jd_fingerprint") or "").strip() or None
        if jd_fp is None and company and job.get("output_dir"):
            jd_text = _read_jd_from_output_dir(str(job["output_dir"]))
            jd_fp = jd_fingerprint(company, jd_text)

        sets: list[str] = []
        params: list[object] = []
        if not str(job.get("company") or "").strip() and company:
            sets.append("company = ?")
            params.append(company)
        if not str(job.get("role_title") or "").strip() and role_title:
            sets.append("role_title = ?")
            params.append(role_title)
        if not str(job.get("board") or "").strip() and board:
            sets.append("board = ?")
            params.append(board)
        if not str(job.get("board_url") or "").strip() and board_url:
            sets.append("board_url = ?")
            params.append(board_url)
        if not str(job.get("jd_fingerprint") or "").strip() and jd_fp:
            sets.append("jd_fingerprint = ?")
            params.append(jd_fp)
        if not sets:
            continue
        params.append(int(job["id"]))
        conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)
        updated_rows += 1
    if updated_rows:
        conn.commit()
    return updated_rows


def _duplicate_label(match_type: str, duplicate_job: dict) -> tuple[str, str]:
    duplicate_label = (
        f"{duplicate_job.get('company') or '?'} — {duplicate_job.get('role_title') or '?'} (job #{duplicate_job['id']})"
    )
    if match_type == "url":
        prefix = "Duplicate job URL"
    elif match_type == "jd":
        prefix = "Duplicate job description"
    else:
        prefix = "Duplicate job"
    return prefix, duplicate_label


def reconcile_duplicate_jobs(
    conn: sqlite3.Connection,
    *,
    initiator: str = "system",
) -> dict:
    """Retroactively archive inactive duplicate rows after metadata becomes authoritative."""
    metadata_backfilled = backfill_duplicate_match_metadata(conn)
    rows = conn.execute(
        "SELECT * FROM jobs WHERE (archived IS NULL OR archived = FALSE) ORDER BY created_at ASC, id ASC"
    ).fetchall()
    archived_job_ids: list[int] = []
    skipped_processing = 0
    for row in rows:
        job = dict(row)
        duplicate_match = find_duplicate_job_match(
            conn,
            url=job.get("url"),
            source_url=job.get("source_url"),
            board_url=job.get("board_url"),
            canonical_url=job.get("canonical_url")
            or _initial_canonical_job_url(str(job.get("board_url") or job.get("url") or "")),
            company=job.get("company"),
            role_title=job.get("role_title"),
            jd_text=_read_jd_from_output_dir(str(job["output_dir"])) if job.get("output_dir") else None,
            exclude_job_id=int(job["id"]),
            older_than_job_id=int(job["id"]),
        )
        if duplicate_match is None:
            continue
        if str(job.get("status") or "") not in DUPLICATE_RECONCILE_ELIGIBLE_STATUSES:
            skipped_processing += 1
            continue

        match_type, duplicate_job = duplicate_match
        prefix, duplicate_label = _duplicate_label(match_type, duplicate_job)
        detail_json = {
            "match_type": match_type,
            "duplicate_job_id": int(duplicate_job["id"]),
            "duplicate_label": duplicate_label,
            "duplicate_status": duplicate_job.get("status"),
            "duplicate_archived": bool(duplicate_job.get("archived")),
            "mode": "retroactive_reconcile",
        }
        event_name = "jd_duplicate_detected" if match_type == "jd" else "duplicate_detected"
        current = conn.execute(
            "SELECT status, archived, board, board_url, company, role_title, output_dir FROM jobs WHERE id = ?",
            (int(job["id"]),),
        ).fetchone()
        if current is None:
            continue
        current_status = str(current["status"] or "")
        if bool(current["archived"]) or current_status != str(job.get("status") or ""):
            continue
        error_message = f"{prefix} — matches job #{duplicate_job['id']} ({duplicate_label})"
        cur = conn.execute(
            "UPDATE jobs SET status = 'stopped', archived = TRUE, failure_type = 'duplicate', "
            "error_message = ?, auth_state = NULL, auth_scope = NULL, progress = '' "
            "WHERE id = ? AND (archived IS NULL OR archived = FALSE) AND status = ?",
            (error_message, int(job["id"]), current_status),
        )
        conn.commit()
        if cur.rowcount <= 0:
            continue
        log_event(conn, int(job["id"]), event_name, detail_json=detail_json, initiator=initiator)
        log_event(conn, int(job["id"]), "status_change", detail="stopped", initiator=initiator)
        archived_job_ids.append(int(job["id"]))
    return {
        "metadata_backfilled": metadata_backfilled,
        "archived": len(archived_job_ids),
        "archived_job_ids": archived_job_ids,
        "skipped_processing": skipped_processing,
    }


def backfill_jd_fingerprints(conn: sqlite3.Connection) -> tuple[int, int]:
    """Compute jd_fingerprint for all jobs that have an output_dir but no fingerprint.

    Returns (updated_count, skipped_count).
    """
    rows = conn.execute(
        "SELECT id, company, output_dir FROM jobs "
        "WHERE jd_fingerprint IS NULL AND output_dir IS NOT NULL AND company IS NOT NULL"
    ).fetchall()
    updated = 0
    skipped = 0
    for row in rows:
        jd_text = _read_jd_from_output_dir(row["output_dir"])
        fp = jd_fingerprint(row["company"], jd_text)
        if fp:
            conn.execute("UPDATE jobs SET jd_fingerprint = ? WHERE id = ?", (fp, row["id"]))
            updated += 1
        else:
            skipped += 1
    conn.commit()
    return updated, skipped


def find_jd_duplicates(conn: sqlite3.Connection) -> list[list[dict]]:
    """Find groups of jobs that share the same jd_fingerprint.

    Returns a list of groups, where each group is a list of job dicts
    sharing the same fingerprint. Only groups with 2+ jobs are returned.
    """
    rows = conn.execute(
        "SELECT jd_fingerprint, COUNT(*) AS cnt FROM jobs "
        "WHERE jd_fingerprint IS NOT NULL "
        "GROUP BY jd_fingerprint HAVING cnt > 1 "
        "ORDER BY cnt DESC"
    ).fetchall()
    groups = []
    for row in rows:
        jobs = conn.execute(
            "SELECT id, company, role_title, url, status, canonical_url, created_at "
            "FROM jobs WHERE jd_fingerprint = ? ORDER BY created_at ASC",
            (row["jd_fingerprint"],),
        ).fetchall()
        groups.append([dict(j) for j in jobs])
    return groups


def get_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    return _attach_status_timestamps(conn, [dict(row)])[0]


def _fallback_status_entered_at(job: dict) -> tuple[str | None, str | None]:
    """Best-effort fallback for legacy rows without matching status-change events."""
    if job.get("status") == "submitted" and job.get("completed_at"):
        return job["completed_at"], "completed_at"
    if job.get("status") == "queued" and job.get("created_at"):
        return job["created_at"], "created_at"
    if job.get("updated_at"):
        return job["updated_at"], "updated_at"
    if job.get("created_at"):
        return job["created_at"], "created_at"
    if job.get("completed_at"):
        return job["completed_at"], "completed_at"
    return None, None


def _latest_status_change_timestamps(conn: sqlite3.Connection, jobs: list[dict]) -> dict[tuple[int, str], str]:
    job_ids = [int(job["id"]) for job in jobs if job.get("id") is not None]
    if not job_ids:
        return {}
    placeholders = ",".join("?" for _ in job_ids)
    rows = conn.execute(
        f"""
        SELECT e.job_id, e.detail AS status, e.created_at
        FROM events e
        JOIN (
            SELECT job_id, detail, MAX(id) AS latest_id
            FROM events
            WHERE event_type = 'status_change'
              AND job_id IN ({placeholders})
            GROUP BY job_id, detail
        ) latest ON latest.latest_id = e.id
        """,
        job_ids,
    ).fetchall()
    return {
        (int(row["job_id"]), str(row["status"])): row["created_at"]
        for row in rows
        if row["status"] and row["created_at"]
    }


def _attach_status_timestamps(conn: sqlite3.Connection, jobs: list[dict]) -> list[dict]:
    """Attach the exact current-status entry time for queue and detail surfaces."""
    status_change_timestamps = _latest_status_change_timestamps(conn, jobs)
    for job in jobs:
        timestamp = None
        source = None
        status = job.get("status")
        job_id = job.get("id")
        if job_id is not None and status:
            timestamp = status_change_timestamps.get((int(job_id), str(status)))
            if timestamp:
                source = "status_change"
        if not timestamp:
            timestamp, source = _fallback_status_entered_at(job)
        job["status_entered_at"] = timestamp
        job["status_entered_at_source"] = source
        job["queue_timestamp"] = timestamp
        job["queue_timestamp_source"] = source
    return jobs


def get_pending_jobs(
    conn: sqlite3.Connection,
    *,
    exclude_boards: set[str] | None = None,
    limit: int = 10,
) -> list[dict]:
    effective_lock_state = _effective_submission_lock_state_sql()
    rows = conn.execute(
        """SELECT * FROM jobs
           WHERE status IN ('queued', 'queued_submit', 'approved', 'submitting', 'reanswering', 'regenerating')
           AND (archived IS NULL OR archived = FALSE)
           AND """
        + effective_lock_state
        + """ != 'locked'
           AND retry_after <= datetime('now')
           ORDER BY
             CASE status WHEN 'approved' THEN 0 WHEN 'submitting' THEN 0 WHEN 'reanswering' THEN 0 WHEN 'regenerating' THEN 0 ELSE 1 END,
             priority DESC,
             CASE WHEN status IN ('approved', 'submitting', 'reanswering', 'regenerating') THEN updated_at ELSE created_at END ASC,
             id ASC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    result = [dict(r) for r in rows]
    if repair_queue_pause_active(conn):
        result = [row for row in result if row.get("status") not in QUEUE_QUEUED_STATUSES]
    if exclude_boards:
        result = [r for r in result if r.get("board") not in exclude_boards]
    return result


def update_progress(conn: sqlite3.Connection, job_id: int, progress: str) -> None:
    """Update the progress text for a job without changing status."""
    conn.execute("UPDATE jobs SET progress = ? WHERE id = ?", (progress, job_id))
    conn.commit()


def update_status(
    conn: sqlite3.Connection,
    job_id: int,
    status: str,
    *,
    error_message: str | None = None,
    progress: str | None = None,
    provider: str | None = None,
    clear_provider: bool = False,
    board: str | None = None,
    board_url: str | None = None,
    canonical_url: str | None = None,
    company: str | None = None,
    role_title: str | None = None,
    output_dir: str | None = None,
    notion_url: str | None = None,
    archived: bool | None = None,
    failure_type: str | None = None,
    auth_state: str | None = None,
    auth_scope: str | None = None,
    retry_after: str | None = None,
    initiator: str | None = None,
    process_info: str | None = None,
    event_detail_json: dict | None = None,
) -> None:
    enforce_submission_lock(conn, job_id, target_status=status)
    sets = ["status = ?"]
    params: list = [status]
    if error_message is not None:
        sets.append("error_message = ?")
        params.append(error_message)
    if progress is not None:
        sets.append("progress = ?")
        params.append(progress)
    elif status in {"queued", "queued_submit"}:
        sets.append("progress = ''")
    if provider is not None:
        sets.append("provider = ?")
        params.append(provider)
    elif clear_provider:
        sets.append("provider = NULL")
    if board is not None:
        sets.append("board = ?")
        params.append(board)
    if board_url is not None:
        sets.append("board_url = ?")
        params.append(board_url)
    if canonical_url is not None:
        sets.append("canonical_url = ?")
        params.append(canonical_url)
    if company is not None:
        sets.append("company = ?")
        params.append(company)
    if role_title is not None:
        sets.append("role_title = ?")
        params.append(role_title)
    if output_dir is not None:
        sets.append("output_dir = ?")
        params.append(output_dir)
    if notion_url is not None:
        sets.append("notion_url = ?")
        params.append(notion_url)
    if archived is not None:
        sets.append("archived = ?")
        params.append(archived)
    if failure_type is not None:
        sets.append("failure_type = ?")
        params.append(failure_type)
    if auth_state is not None:
        sets.append("auth_state = ?")
        params.append(auth_state)
    if auth_scope is not None:
        sets.append("auth_scope = ?")
        params.append(auth_scope)
    if retry_after is not None:
        sets.append("retry_after = ?")
        params.append(retry_after)
    # Auto-clear failure_type when leaving stopped
    if status != "stopped" and failure_type is None:
        sets.append("failure_type = NULL")
    if status != "stopped" and auth_state is None:
        sets.append("auth_state = NULL")
    if status != "stopped" and auth_scope is None:
        sets.append("auth_scope = NULL")
    if status == "submitted":
        sets.append("completed_at = CURRENT_TIMESTAMP")
    params.append(job_id)
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)
    log_event(
        conn,
        job_id,
        "status_change",
        detail=status,
        detail_json=event_detail_json,
        initiator=initiator,
        process_info=process_info,
    )
    conn.commit()


def log_event(
    conn: sqlite3.Connection,
    job_id: int,
    event_type: str,
    *,
    detail: str | None = None,
    detail_json: dict | None = None,
    initiator: str | None = None,
    process_info: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO events (job_id, event_type, detail, detail_json, initiator, process_info) VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, event_type, detail, json.dumps(detail_json) if detail_json else None, initiator, process_info),
    )
    conn.commit()
    return cur.lastrowid


def log_fix_attempt(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    error_type: str,
    error_context: dict,
    fix_diff: str | None = None,
    fix_branch: str | None = None,
    tests_passed: bool | None = None,
    applied: bool = False,
) -> int:
    cur = conn.execute(
        """INSERT INTO fix_attempts
           (job_id, error_type, error_context, fix_diff, fix_branch, tests_passed, applied)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (job_id, error_type, json.dumps(error_context), fix_diff, fix_branch, tests_passed, applied),
    )
    conn.execute(
        "UPDATE jobs SET fix_attempts = fix_attempts + 1 WHERE id = ?",
        (job_id,),
    )
    conn.commit()
    return cur.lastrowid


def log_provider_run(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    provider: str,
    phase: str,
    exit_code: int | None = None,
    duration_ms: int | None = None,
    error_message: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO provider_runs
           (job_id, provider, phase, exit_code, duration_ms, error_message)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (job_id, provider, phase, exit_code, duration_ms, error_message),
    )
    conn.commit()
    return cur.lastrowid


def get_job_timeline(conn: sqlite3.Connection, job_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM events WHERE job_id = ? ORDER BY created_at DESC",
        (job_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def query_jobs(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    board: str | None = None,
    source: str | None = None,
    search: str | None = None,
    include_archived: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    where = []
    params: list = []
    if status:
        where.append("status = ?")
        params.append(status)
    if board:
        where.append("board = ?")
        params.append(board)
    if source:
        where.append("source = ?")
        params.append(source)
    if search:
        where.append("(company LIKE ? OR role_title LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    if not include_archived:
        where.append("(archived IS NULL OR archived = FALSE)")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.extend([limit, offset])
    rows = conn.execute(
        f"SELECT * FROM jobs {clause} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        params,
    ).fetchall()
    return _attach_status_timestamps(conn, [dict(r) for r in rows])


def _queue_where_clause(
    *,
    status: str | None = None,
    board: str | None = None,
    search: str | None = None,
) -> tuple[str, list[object]]:
    where: list[str] = []
    params: list[object] = []

    normalized_status = (status or "").strip()
    if normalized_status == "archived":
        where.append("COALESCE(archived, FALSE) = TRUE")
    else:
        where.append("COALESCE(archived, FALSE) = FALSE")
        if normalized_status:
            statuses = QUEUE_STATUS_GROUPS.get(normalized_status, (normalized_status,))
            placeholders = ",".join("?" * len(statuses))
            where.append(f"status IN ({placeholders})")
            params.extend(statuses)

    normalized_board = (board or "").strip().lower()
    if normalized_board:
        where.append("LOWER(COALESCE(board, '')) = ?")
        params.append(normalized_board)

    normalized_search = (search or "").strip()
    if normalized_search:
        needle = f"%{normalized_search}%"
        where.append(
            "("
            "COALESCE(company, '') LIKE ? OR "
            "COALESCE(role_title, '') LIKE ? OR "
            "COALESCE(canonical_url, '') LIKE ? OR "
            "COALESCE(board, '') LIKE ? OR "
            "COALESCE(provider, '') LIKE ?"
            ")"
        )
        params.extend([needle, needle, needle, needle, needle])

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return clause, params


def query_queue_jobs(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    board: str | None = None,
    search: str | None = None,
    sort_field: str = "updated_at",
    sort_dir: str = "desc",
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    order_columns = {
        "id": "id",
        "status": "status COLLATE NOCASE",
        "company": "CASE WHEN TRIM(COALESCE(company, '')) = '' THEN 1 ELSE 0 END, COALESCE(company, '') COLLATE NOCASE",
        "role_title": "CASE WHEN TRIM(COALESCE(role_title, '')) = '' THEN 1 ELSE 0 END, COALESCE(role_title, '') COLLATE NOCASE",
        "board": "CASE WHEN TRIM(COALESCE(board, '')) = '' THEN 1 ELSE 0 END, COALESCE(board, '') COLLATE NOCASE",
        "progress": "CASE WHEN TRIM(COALESCE(progress, '')) = '' THEN 1 ELSE 0 END, COALESCE(progress, '') COLLATE NOCASE",
        "updated_at": "updated_at",
    }
    order_by = order_columns.get(sort_field, order_columns["updated_at"])
    direction = "ASC" if str(sort_dir).lower() == "asc" else "DESC"
    clause, params = _queue_where_clause(status=status, board=board, search=search)
    params.extend([limit, offset])
    rows = conn.execute(
        f"SELECT * FROM jobs {clause} ORDER BY {order_by} {direction}, id DESC LIMIT ? OFFSET ?",
        params,
    ).fetchall()
    return _attach_status_timestamps(conn, [dict(r) for r in rows])


def count_queue_jobs(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    board: str | None = None,
    search: str | None = None,
) -> int:
    clause, params = _queue_where_clause(status=status, board=board, search=search)
    row = conn.execute(f"SELECT COUNT(*) AS total FROM jobs {clause}", params).fetchone()
    return int(row["total"] or 0)


def get_queue_counts(conn: sqlite3.Connection) -> dict[str, int]:
    queued_expr = ", ".join(f"'{status}'" for status in QUEUE_QUEUED_STATUSES)
    processing_expr = ", ".join(f"'{status}'" for status in QUEUE_PROCESSING_STATUSES)
    stopped_expr = ", ".join(f"'{status}'" for status in QUEUE_STOPPED_STATUSES)
    row = conn.execute(
        f"""
        SELECT
            SUM(CASE WHEN COALESCE(archived, FALSE) = FALSE THEN 1 ELSE 0 END) AS all_count,
            SUM(CASE WHEN COALESCE(archived, FALSE) = FALSE AND status IN ({queued_expr}) THEN 1 ELSE 0 END)
                AS queued_count,
            SUM(CASE WHEN COALESCE(archived, FALSE) = FALSE AND status IN ({processing_expr}) THEN 1 ELSE 0 END)
                AS processing_count,
            SUM(CASE WHEN COALESCE(archived, FALSE) = FALSE AND status = 'draft' THEN 1 ELSE 0 END)
                AS draft_count,
            SUM(CASE WHEN COALESCE(archived, FALSE) = FALSE AND status = 'submitted' THEN 1 ELSE 0 END)
                AS submitted_count,
            SUM(CASE WHEN COALESCE(archived, FALSE) = FALSE AND status IN ({stopped_expr}) THEN 1 ELSE 0 END)
                AS stopped_count,
            SUM(CASE WHEN COALESCE(archived, FALSE) = TRUE THEN 1 ELSE 0 END) AS archived_count
        FROM jobs
        """
    ).fetchone()
    return {
        "all": int(row["all_count"] or 0),
        "queued": int(row["queued_count"] or 0),
        "processing": int(row["processing_count"] or 0),
        "draft": int(row["draft_count"] or 0),
        "submitted": int(row["submitted_count"] or 0),
        "stopped": int(row["stopped_count"] or 0),
        "archived": int(row["archived_count"] or 0),
    }


def get_status_counts(conn: sqlite3.Connection, *, include_archived: bool = True) -> dict[str, int]:
    where = "" if include_archived else "WHERE archived IS NULL OR archived = FALSE"
    rows = conn.execute(f"SELECT status, COUNT(*) as cnt FROM jobs {where} GROUP BY status").fetchall()
    return {r["status"]: r["cnt"] for r in rows}


def get_board_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    rows = conn.execute("SELECT board, status, COUNT(*) as cnt FROM jobs GROUP BY board, status").fetchall()
    result: dict[str, dict[str, int]] = {}
    for r in rows:
        board = r["board"] or "unknown"
        result.setdefault(board, {})
        result[board][r["status"]] = r["cnt"]
    return result


def reset_stale_jobs(
    conn: sqlite3.Connection,
    stale_threshold_seconds: int = 1800,
) -> list[int]:
    from answer_refresh_state import fail_pending_answer_refresh

    conn.execute(
        "UPDATE jobs SET retry_after = ? WHERE status = 'queued' AND retry_after > datetime('now', '+1 hour')",
        (RETRY_AFTER_SENTINEL,),
    )
    conn.commit()
    stale_claim_rows = conn.execute(
        "SELECT id FROM jobs WHERE status IN ('queued', 'queued_submit') AND COALESCE(progress, '') LIKE 'claimed:%'"
    ).fetchall()
    stale_claim_ids = [int(row["id"]) for row in stale_claim_rows]
    if stale_claim_ids:
        placeholders = ",".join("?" * len(stale_claim_ids))
        conn.execute(f"UPDATE jobs SET progress = '' WHERE id IN ({placeholders})", stale_claim_ids)
        conn.commit()
    lock_repair_statuses = tuple(sorted(LOCK_REPAIR_RESETTABLE_STATUSES))
    lock_repair_ph = ",".join("?" * len(lock_repair_statuses))
    effective_lock_state = _effective_submission_lock_state_sql()
    ids: list[int] = list(stale_claim_ids)
    repaired_rows = conn.execute(
        f"SELECT id FROM jobs WHERE {effective_lock_state} = 'locked' AND status IN ({lock_repair_ph})",
        lock_repair_statuses,
    ).fetchall()
    for row in repaired_rows:
        if repair_submission_locked_job(conn, row["id"], initiator="system"):
            ids.append(row["id"])

    # SAFETY: Submit-phase jobs (submitting, reanswering, awaiting_captcha) are
    # ALWAYS reset to draft on startup, regardless of age.  The only way a job
    # gets submitted is through an explicit Approve + Submit that completes in
    # the current session.  No auto-resume of interrupted submissions.
    submit_phase = ("approved", "submitting", "reanswering", "awaiting_captcha")
    submit_ph = ",".join("?" * len(submit_phase))
    submit_rows = conn.execute(
        f"SELECT id FROM jobs WHERE status IN ({submit_ph})",
        submit_phase,
    ).fetchall()
    submit_ids = [r["id"] for r in submit_rows]
    ids.extend(submit_ids)
    for job_id in submit_ids:
        job = get_job(conn, job_id)
        if job and job.get("output_dir"):
            refresh_state = fail_pending_answer_refresh(
                Path(job["output_dir"]),
                reason="interrupted_by_reset",
                message="Answer regeneration was interrupted before fresh proof was recorded.",
            )
            if refresh_state.get("status") == "failed":
                log_event(
                    conn,
                    job_id,
                    "answer_refresh_failed",
                    detail="Answer regeneration was interrupted before fresh proof was recorded.",
                    initiator="system",
                )
        update_status(
            conn,
            job_id,
            "draft",
            error_message="",
            progress="Submit interrupted — needs re-approval",
            clear_provider=True,
            retry_after=RETRY_AFTER_SENTINEL,
        )

    # autofilling → queued (form-fill was interrupted before draft was created)
    autofill_rows = conn.execute("SELECT id FROM jobs WHERE status = 'autofilling'").fetchall()
    for r in autofill_rows:
        update_status(
            conn,
            r["id"],
            "queued",
            error_message="Reset: autofill interrupted",
            clear_provider=True,
            retry_after=RETRY_AFTER_SENTINEL,
        )
        ids.append(r["id"])

    # queued_submit → queued (strip auto-submit intent on restart — ACID safety)
    qs_cur = conn.execute(
        "UPDATE jobs SET status = 'queued', provider = NULL, retry_after = ? WHERE status = 'queued_submit'",
        (RETRY_AFTER_SENTINEL,),
    )
    if qs_cur.rowcount > 0:
        conn.commit()

    # Non-submit in-progress statuses: safe to retry automatically
    in_progress = ("resolving", "generating", "fix_in_progress", "retrying", "regenerating")
    placeholders = ",".join("?" * len(in_progress))
    rows = conn.execute(
        f"""SELECT id FROM jobs
            WHERE status IN ({placeholders})
            AND updated_at < datetime('now', ? || ' seconds')
            AND id NOT IN (
                SELECT DISTINCT job_id FROM events WHERE event_type = 'submitted'
            )""",
        (*in_progress, f"-{stale_threshold_seconds}"),
    ).fetchall()
    stale_ids = [r["id"] for r in rows]
    for job_id in stale_ids:
        # If job has a complete draft (output with autofill report), go to draft not queued
        job = get_job(conn, job_id)
        if job and job.get("output_dir"):
            out = Path(job["output_dir"])
            has_draft = any(out.glob("submit/*_autofill_report.json"))
            if has_draft:
                update_status(
                    conn,
                    job_id,
                    "draft",
                    error_message="",
                    progress="Draft ready for review",
                    clear_provider=True,
                    retry_after=RETRY_AFTER_SENTINEL,
                )
                ids.append(job_id)
                continue
        update_status(
            conn,
            job_id,
            "queued",
            error_message="Reset: stale in-progress job",
            clear_provider=True,
            retry_after=RETRY_AFTER_SENTINEL,
        )
        ids.append(job_id)

    # Previously submitted stale jobs → stopped (not queued) to prevent accidental resubmit
    prev_submitted_rows = conn.execute(
        f"""SELECT id FROM jobs
            WHERE status IN ({placeholders})
            AND updated_at < datetime('now', ? || ' seconds')
            AND id IN (
                SELECT DISTINCT job_id FROM events WHERE event_type = 'submitted'
            )""",
        (*in_progress, f"-{stale_threshold_seconds}"),
    ).fetchall()
    for r in prev_submitted_rows:
        update_status(
            conn,
            r["id"],
            "stopped",
            error_message="Reset: stale job (previously submitted)",
            failure_type="crash",
            retry_after=RETRY_AFTER_SENTINEL,
        )
        ids.append(r["id"])

    return ids


def import_from_output_dir(
    conn: sqlite3.Connection,
    output_root: Path,
) -> int:
    """Import existing jobs from output/ directories into the database.

    Scans for application_submission_result.json files to determine
    status, board, provider, and timestamps. Returns count of imported jobs.
    """
    imported = 0
    for result_file in output_root.rglob("application_submission_result.json"):
        try:
            data = json.loads(result_file.read_text(encoding="utf-8"))
            submit_dir = result_file.parent
            role_dir = submit_dir.parent

            # Read meta for URL and company info
            meta_path = role_dir / "content" / ".pipeline_meta.json"
            if not meta_path.exists():
                meta_path = role_dir / ".pipeline_meta.json"
            meta = {}
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))

            url = meta.get("jd_url") or meta.get("url") or ""
            if not url:
                continue

            company = meta.get("company") or role_dir.parent.name
            role_title = meta.get("job_title") or role_dir.name

            # Determine status from result
            result_status = data.get("result", "")
            if result_status == "confirmed":
                status = "submitted"
            else:
                status = "stopped"

            board = data.get("board") or meta.get("board") or ""
            provider = data.get("provider") or ""

            try:
                add_job(conn, url=url)
                job_id = conn.execute("SELECT id FROM jobs WHERE url = ?", (url,)).fetchone()["id"]
                update_status(
                    conn,
                    job_id,
                    status,
                    company=company,
                    role_title=role_title,
                    board=board,
                    provider=provider,
                    output_dir=str(role_dir),
                )
                imported += 1
            except sqlite3.IntegrityError:
                # Already imported (duplicate canonical_url)
                continue
        except Exception:
            continue
    return imported


# ---------------------------------------------------------------------------
# Phase duration tracking
# ---------------------------------------------------------------------------


def start_phase(conn: sqlite3.Connection, job_id: int, phase: str) -> int:
    """Insert a new phase row and return its id."""
    cur = conn.execute(
        "INSERT INTO job_phase_durations (job_id, phase) VALUES (?, ?)",
        (job_id, phase),
    )
    conn.commit()
    return cur.lastrowid


def end_phase(conn: sqlite3.Connection, phase_id: int, *, exit_code: int | None = None) -> None:
    """Mark a phase as ended, calculating duration via julianday diff."""
    conn.execute(
        """UPDATE job_phase_durations
           SET ended_at = CURRENT_TIMESTAMP,
               duration_ms = CAST((julianday(CURRENT_TIMESTAMP) - julianday(started_at)) * 86400000 AS INTEGER),
               exit_code = ?
           WHERE id = ?""",
        (exit_code, phase_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Field corrections
# ---------------------------------------------------------------------------


def log_field_correction(
    conn: sqlite3.Connection,
    job_id: int,
    field_name: str,
    original_value: str | None,
    corrected_value: str | None,
    correction_source: str,
) -> int:
    """Log a field correction and return the row id."""
    cur = conn.execute(
        """INSERT INTO field_corrections
           (job_id, field_name, original_value, corrected_value, correction_source)
           VALUES (?, ?, ?, ?, ?)""",
        (job_id, field_name, original_value, corrected_value, correction_source),
    )
    conn.commit()
    return cur.lastrowid


def get_field_corrections(conn: sqlite3.Connection, job_id: int) -> list[dict]:
    """Return all corrections for a job, ordered by created_at ASC."""
    rows = conn.execute(
        "SELECT * FROM field_corrections WHERE job_id = ? ORDER BY created_at ASC",
        (job_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Job metrics
# ---------------------------------------------------------------------------


def ensure_job_metrics(conn: sqlite3.Connection, job_id: int) -> None:
    """Create a metrics row if it doesn't already exist (idempotent)."""
    conn.execute(
        "INSERT OR IGNORE INTO job_metrics (job_id) VALUES (?)",
        (job_id,),
    )
    conn.commit()


def update_job_metrics(conn: sqlite3.Connection, job_id: int, **kwargs) -> None:
    """Update specific metric fields and auto-recalculate field_error_rate."""
    allowed = {
        "total_fields",
        "fields_corrected",
        "manual_interventions",
        "auto_fix_attempts",
        "total_duration_ms",
        "phase_count",
        "retry_count",
        "audit_attempts",
        "audit_failure_count",
        "rendered_audit_failures",
        "last_repair_cluster_id",
        "last_rollout_sha",
        "llm_generated_answers",
        "llm_generated_labels",
    }
    sets = []
    params: list = []
    for key, value in kwargs.items():
        if key not in allowed:
            raise ValueError(f"Unknown metric field: {key}")
        sets.append(f"{key} = ?")
        params.append(value)
    if not sets:
        return
    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.append(job_id)
    conn.execute(
        f"UPDATE job_metrics SET {', '.join(sets)} WHERE job_id = ?",
        params,
    )
    # Auto-recalculate field_error_rate
    conn.execute(
        """UPDATE job_metrics
           SET field_error_rate = CASE
               WHEN total_fields > 0 THEN CAST(fields_corrected AS REAL) / total_fields
               ELSE 0.0
           END
           WHERE job_id = ?""",
        (job_id,),
    )
    conn.commit()


def get_job_metrics(conn: sqlite3.Connection, job_id: int) -> dict | None:
    """Return metrics dict for a job, or None if no row exists."""
    row = conn.execute("SELECT * FROM job_metrics WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def get_all_job_metrics(
    conn: sqlite3.Connection,
    *,
    board: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """Return metrics joined with jobs, optionally filtered by board/status."""
    where = []
    params: list = []
    if board is not None:
        where.append("j.board = ?")
        params.append(board)
    if status is not None:
        where.append("j.status = ?")
        params.append(status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"""SELECT j.*, m.total_fields, m.fields_corrected, m.field_error_rate,
                   m.manual_interventions, m.auto_fix_attempts, m.total_duration_ms,
                   m.phase_count, m.retry_count, m.audit_attempts,
                   m.audit_failure_count, m.rendered_audit_failures,
                   m.last_repair_cluster_id, m.last_rollout_sha,
                   m.updated_at AS metrics_updated_at
            FROM job_metrics m
            JOIN jobs j ON j.id = m.job_id
            {clause}""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def get_repair_cluster(conn: sqlite3.Connection, cluster_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM repair_clusters WHERE id = ?", (cluster_id,)).fetchone()
    return dict(row) if row else None


def list_open_repair_clusters(
    conn: sqlite3.Connection,
    *,
    limit: int = 10,
    max_rollouts: int = 3,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT c.*,
               COALESCE(r.rollout_count, 0) AS rollout_count
        FROM repair_clusters c
        LEFT JOIN (
            SELECT cluster_id, COUNT(*) AS rollout_count
            FROM repair_rollouts
            GROUP BY cluster_id
        ) r ON r.cluster_id = c.id
        WHERE c.status = 'open'
          AND c.eligibility = 'auto_repair_candidate'
        ORDER BY c.updated_at ASC, c.id ASC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    return [dict(row) for row in rows]


def list_exhausted_repair_clusters(
    conn: sqlite3.Connection,
    *,
    limit: int = 10,
    max_rollouts: int = 3,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT c.*,
               COALESCE(r.rollout_count, 0) AS rollout_count
        FROM repair_clusters c
        LEFT JOIN (
            SELECT cluster_id, COUNT(*) AS rollout_count
            FROM repair_rollouts
            GROUP BY cluster_id
        ) r ON r.cluster_id = c.id
        WHERE c.status = 'open'
          AND c.eligibility = 'auto_repair_candidate'
          AND COALESCE(r.rollout_count, 0) >= ?
        ORDER BY c.updated_at ASC, c.id ASC
        LIMIT ?
        """,
        (int(max_rollouts), int(limit)),
    ).fetchall()
    return [dict(row) for row in rows]


def update_repair_cluster(
    conn: sqlite3.Connection,
    cluster_id: int,
    *,
    status: str | None = None,
    eligibility: str | None = None,
    latest_summary: str | None = None,
) -> None:
    sets: list[str] = []
    params: list[object] = []
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if eligibility is not None:
        sets.append("eligibility = ?")
        params.append(eligibility)
    if latest_summary is not None:
        sets.append("latest_summary = ?")
        params.append(latest_summary)
    if not sets:
        return
    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.append(int(cluster_id))
    conn.execute(f"UPDATE repair_clusters SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()


def count_repair_rollouts(conn: sqlite3.Connection, cluster_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM repair_rollouts WHERE cluster_id = ?", (int(cluster_id),)
    ).fetchone()
    return int((row["count"] if row else 0) or 0)


def record_repair_rollout(
    conn: sqlite3.Connection,
    cluster_id: int,
    *,
    commit_sha: str,
    status: str,
    baseline_metrics_json: dict | None = None,
    post_fix_metrics_json: dict | None = None,
    revert_sha: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO repair_rollouts (
            cluster_id,
            commit_sha,
            status,
            baseline_metrics_json,
            post_fix_metrics_json,
            revert_sha
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            int(cluster_id),
            str(commit_sha or "").strip(),
            str(status or "").strip(),
            json.dumps(baseline_metrics_json or {}, sort_keys=True),
            json.dumps(post_fix_metrics_json or {}, sort_keys=True),
            revert_sha,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Aggregate queries
# ---------------------------------------------------------------------------


def get_summary_stats(conn: sqlite3.Connection, *, since: str | None = None) -> dict:
    """Return summary statistics across all jobs (optionally since a timestamp)."""
    where = ""
    params: list = []
    if since:
        where = "WHERE j.created_at >= ?"
        params = [since]

    row = conn.execute(
        f"""SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN j.status = 'submitted' THEN 1 ELSE 0 END) AS submitted,
                SUM(CASE WHEN j.status = 'stopped' THEN 1 ELSE 0 END) AS stopped,
                SUM(CASE WHEN j.status = 'needs_board_url' THEN 1 ELSE 0 END) AS needs_attention
            FROM jobs j {where}""",
        params,
    ).fetchone()

    total = row["total"] or 0
    submitted = row["submitted"] or 0
    stopped = row["stopped"] or 0
    needs_attention = row["needs_attention"] or 0

    success_rate = submitted / total if total > 0 else 0.0
    failure_rate = stopped / total if total > 0 else 0.0

    # Metrics-level stats
    mrow = conn.execute(
        """SELECT
               COALESCE(AVG(m.field_error_rate), 0.0) AS avg_error_rate,
               COALESCE(AVG(m.total_duration_ms), 0.0) AS avg_duration_ms,
               SUM(CASE WHEN m.manual_interventions > 0 THEN 1 ELSE 0 END) AS jobs_with_interventions
           FROM job_metrics m"""
    ).fetchone()

    avg_error_rate = mrow["avg_error_rate"] or 0.0
    avg_duration_ms = mrow["avg_duration_ms"] or 0.0
    jobs_with_interventions = mrow["jobs_with_interventions"] or 0
    intervention_rate = jobs_with_interventions / total if total > 0 else 0.0

    return {
        "total": total,
        "submitted": submitted,
        "stopped": stopped,
        "needs_attention": needs_attention,
        "success_rate": success_rate,
        "failure_rate": failure_rate,
        "avg_error_rate": avg_error_rate,
        "avg_duration_ms": avg_duration_ms,
        "jobs_with_interventions": jobs_with_interventions,
        "intervention_rate": intervention_rate,
    }


def get_phase_avg_durations(conn: sqlite3.Connection) -> dict[str, float]:
    """Return average duration_ms grouped by phase name."""
    rows = conn.execute(
        """SELECT phase, AVG(duration_ms) AS avg_ms
           FROM job_phase_durations
           WHERE duration_ms IS NOT NULL
           GROUP BY phase"""
    ).fetchall()
    return {r["phase"]: r["avg_ms"] for r in rows}


def get_board_error_rates(conn: sqlite3.Connection) -> dict[str, float]:
    """Return average field_error_rate grouped by board."""
    rows = conn.execute(
        """SELECT j.board, AVG(m.field_error_rate) AS avg_rate
           FROM job_metrics m
           JOIN jobs j ON j.id = m.job_id
           WHERE j.board IS NOT NULL
           GROUP BY j.board"""
    ).fetchall()
    return {r["board"]: r["avg_rate"] for r in rows}


def get_jobs_processed_counts(conn: sqlite3.Connection) -> dict:
    """Count terminal-status jobs in various time windows."""
    terminal = ("submitted", "stopped")
    placeholders = ",".join("?" * len(terminal))
    base = f"SELECT COUNT(*) AS cnt FROM jobs WHERE status IN ({placeholders})"

    all_time = conn.execute(base, terminal).fetchone()["cnt"]
    last_7d = conn.execute(f"{base} AND updated_at >= datetime('now', '-7 days')", terminal).fetchone()["cnt"]
    last_24h = conn.execute(f"{base} AND updated_at >= datetime('now', '-1 day')", terminal).fetchone()["cnt"]
    last_1h = conn.execute(f"{base} AND updated_at >= datetime('now', '-1 hour')", terminal).fetchone()["cnt"]

    return {
        "last_1h": last_1h,
        "last_24h": last_24h,
        "last_7d": last_7d,
        "all_time": all_time,
    }


def get_recent_auth_failures(
    conn: sqlite3.Connection,
    board: str,
    hours: int = 24,
    *,
    auth_scope: str | None = None,
) -> int:
    """Count auth failures for *board* in the last *hours* hours.

    Looks at the ``jobs`` table for rows with ``failure_type='auth_failed'``
    and a matching ``board`` value whose ``updated_at`` falls within the
    specified window.  Returns the count (0 if none).
    """
    if not board:
        return 0
    query = [
        "SELECT COUNT(*) AS cnt FROM jobs",
        "WHERE board = ?",
        "  AND failure_type = 'auth_failed'",
        "  AND updated_at >= datetime('now', ?)",
    ]
    params: list[object] = [board, f"-{hours} hours"]
    if auth_scope:
        query.append("  AND auth_scope = ?")
        params.append(auth_scope)
    row = conn.execute("\n".join(query), params).fetchone()
    return row["cnt"] if row else 0


# ---------------------------------------------------------------------------
# Disk-to-DB sync — reads on-disk artifacts and populates DB fields/events
# ---------------------------------------------------------------------------

_SUBMISSION_STATUS_MAP = {
    "confirmed": "submitted",
    "submitted": "submitted",
    "already_applied": "submitted",
    "not_easy_apply": "stopped",
    "pending_user_input": "stopped",
    "skipped_captcha": "stopped",
    "skipped_auth": "stopped",
    "skipped_auth_failure": "stopped",
    "auth_failed": "stopped",
    "auth_unknown": "stopped",
    "auth_guarded": "stopped",
    "service_unavailable": "stopped",
    "job_closed": "stopped",
    "needs_manual": "stopped",
    "unknown": "stopped",
    "failed": "stopped",
}

_WORKDAY_MAINTENANCE_PATTERNS = (
    "workday is currently unavailable",
    "service interruption",
    "maintenance-page",
)
_WORKDAY_CREDENTIAL_REJECTION_PATTERNS = (
    "invalid email or password",
    "invalid username or password",
    "incorrect password",
    "password is incorrect",
    "email or password is incorrect",
)
_GENERIC_STOPPED_ERROR_PREFIXES = (
    "Failed after ",
    "All submission attempts failed",
    "Submit timed out",
)


def _latest_submit_output_detail(conn: sqlite3.Connection, job_id: int) -> str | None:
    row = conn.execute(
        "SELECT detail FROM events WHERE job_id = ? AND event_type = 'submit_output' ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if not row:
        return None
    detail = str(row["detail"] or "").strip()
    return detail or None


def _infer_greenhouse_failure_from_submit_output(conn: sqlite3.Connection, job_id: int) -> tuple[str, str] | None:
    detail = _latest_submit_output_detail(conn, job_id)
    if not detail:
        return None

    lines = [line.strip() for line in detail.splitlines() if line.strip()]
    if not lines:
        return None
    last_line = lines[-1]

    missing_fields_prefix = "ValueError: Autofill payload is missing required Greenhouse fields:"
    if last_line.startswith(missing_fields_prefix):
        return "greenhouse_runtime_error", last_line.removeprefix("ValueError: ").strip()

    unknown_questions_prefix = (
        "RuntimeError: Encountered required application questions that do not have answers in the payload."
    )
    if last_line.startswith(unknown_questions_prefix):
        return "greenhouse_unknown_questions", last_line.removeprefix("RuntimeError: ").strip()

    if "AttributeError: 'ApplicationProfile' object has no attribute 'education_entries'" in last_line:
        return (
            "greenhouse_runtime_error",
            f"Greenhouse payload build crashed before writing a submission result: {last_line}",
        )

    return None


def _normalize_workday_auth_artifact(data: dict) -> tuple[str | None, str | None]:
    raw_status = str(data.get("status") or "").strip()
    auth_state = str(data.get("auth_state") or "").strip()
    page_url = str(data.get("page_url") or "").strip().casefold()
    page_text = str(data.get("page_text_excerpt") or "").strip().casefold()
    heading_text = str(data.get("heading_text") or "").strip().casefold()
    alert_text = str(data.get("alert_text") or "").strip().casefold()
    visible_actions = " ".join(str(item or "") for item in (data.get("visible_actions") or [])).casefold()
    combined = " ".join(part for part in (heading_text, alert_text, page_text, visible_actions) if part)

    if any(pattern in combined or pattern in page_url for pattern in _WORKDAY_MAINTENANCE_PATTERNS):
        return (
            "service_unavailable",
            "Workday is currently unavailable for this tenant. The queue should auto-retry with backoff.",
        )

    if raw_status == "job_closed" or auth_state == "job_unavailable":
        return (
            "job_closed",
            str(data.get("message") or "").strip()
            or "job_closed: Workday resolved to a missing or unavailable posting shell instead of the application form.",
        )

    if auth_state == "credential_rejected" or any(
        pattern in alert_text for pattern in _WORKDAY_CREDENTIAL_REJECTION_PATTERNS
    ):
        return (
            "auth_failed",
            "Workday explicitly rejected the configured credentials after the approved recovery steps.",
        )

    if raw_status in {"auth_unknown", "auth_guarded"} or auth_state in {
        "sign_in_gate",
        "create_account_gate",
        "password_reset_gate",
        "unknown",
        "authenticated_non_form",
    }:
        return (
            "auth_unknown",
            "Workday never reached the application form after trying sign in, password reset, and create account. Saved evidence for diagnosis.",
        )

    if raw_status in {"auth_failed", "service_unavailable"}:
        return raw_status, str(data.get("message") or "").strip() or None

    return None, str(data.get("message") or "").strip() or None


def _should_replace_stale_error_message(existing_message: str | None) -> bool:
    normalized = str(existing_message or "").strip()
    if not normalized:
        return True
    return any(normalized.startswith(prefix) for prefix in _GENERIC_STOPPED_ERROR_PREFIXES)


def _load_ready_draft_status_hint(output_dir: Path) -> dict[str, str] | None:
    status_path = output_dir / "draft_status.json"
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("status") or "").strip() != "awaiting_review":
        return None
    review_state = payload.get("draft_review_state")
    if not isinstance(review_state, dict):
        return None
    if str(review_state.get("state") or "").strip() != "ready":
        return None
    if not (output_dir / "draft_summary.png").exists():
        return None

    docs_dir = output_dir / "documents"
    has_resume = any(docs_dir.glob("*Resume*")) if docs_dir.is_dir() else False
    if not has_resume:
        has_resume = any(output_dir.glob("*Resume*"))
    if not has_resume:
        return None

    has_cover_letter = any(docs_dir.glob("*Cover Letter*")) if docs_dir.is_dir() else False
    if not has_cover_letter:
        has_cover_letter = any(output_dir.glob("*Cover Letter*"))
    if not has_cover_letter:
        return None

    submit_dirname = str(review_state.get("submit_dirname") or "submit").strip() or "submit"
    submit_dir = output_dir / submit_dirname
    try:
        has_submit_artifacts = submit_dir.is_dir() and any(path.is_file() for path in submit_dir.iterdir())
    except OSError:
        return None
    if not has_submit_artifacts:
        return None

    return {
        "submit_dirname": submit_dirname,
        "reason": str(review_state.get("reason") or "").strip(),
    }


def _saved_draft_review_state_requires_refresh(output_dir: Path, review_state: dict | None) -> bool:
    if not isinstance(review_state, dict) or not review_state:
        return False

    status_path = output_dir / "draft_status.json"
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False

    saved_review_state = payload.get("draft_review_state")
    if not isinstance(saved_review_state, dict):
        return False

    current_state = str(review_state.get("state") or "").strip()
    saved_state = str(saved_review_state.get("state") or "").strip()
    if current_state != saved_state:
        return True

    current_submit_dirname = str(review_state.get("submit_dirname") or "submit").strip() or "submit"
    saved_submit_dirname = str(saved_review_state.get("submit_dirname") or "submit").strip() or "submit"
    return current_submit_dirname != saved_submit_dirname


def _stopped_result_artifact_is_stale_vs_current_draft_proof(
    output_dir: Path | None,
    *,
    board_name: str | None = None,
    result_submit_dir: Path | None = None,
) -> bool:
    if output_dir is None or result_submit_dir is None:
        return False

    from output_layout import current_submit_dir_name_for_reads
    from submit_review_common import resolve_current_submit_artifacts

    out_dir = Path(output_dir)
    current_submit_dirname = current_submit_dir_name_for_reads(out_dir)
    if result_submit_dir.name != current_submit_dirname:
        return True

    result_path = result_submit_dir / "application_submission_result.json"
    try:
        result_mtime = result_path.stat().st_mtime
    except OSError:
        return False

    resolved = resolve_current_submit_artifacts(out_dir, board_name=board_name, submit_dirname=current_submit_dirname)
    for artifact_key in ("report_json", "pre_submit_screenshot", "review_screenshot"):
        raw_path = resolved.get(artifact_key)
        if not raw_path:
            continue
        artifact_path = Path(raw_path)
        try:
            if artifact_path.parent.resolve() != result_submit_dir.resolve():
                continue
        except OSError:
            if artifact_path.parent != result_submit_dir:
                continue
        try:
            if artifact_path.stat().st_mtime > result_mtime:
                return True
        except OSError:
            continue

    # A terminal result in the active submit dir is the source of truth for the
    # current attempt unless newer proof artifacts show a later redraft refreshed
    # the same submit boundary after that result was written.
    return False


def _current_proof_is_legacy_ready_except_review_screenshot(
    output_dir: Path | None,
    *,
    board_name: str | None = None,
) -> bool:
    if output_dir is None:
        return False

    from application_submit_common import load_pending_user_input_for_submit_attempt
    from pipeline_draft_proof import _validate_draft_completeness, draft_review_state

    out_dir = Path(output_dir)
    if load_pending_user_input_for_submit_attempt(out_dir) is not None:
        return False

    review_state = draft_review_state(out_dir, board_name=board_name)
    if str(review_state.get("state") or "").strip() != "blocked":
        return False

    missing = _validate_draft_completeness(out_dir, board_name=board_name)
    return missing == ["current-attempt review screenshot"]


def _migrate_archived_status(conn: sqlite3.Connection) -> None:
    """One-time migration: convert status='archived' to archived=TRUE + real status."""
    try:
        rows = conn.execute("SELECT id, output_dir FROM jobs WHERE status = 'archived'").fetchall()
    except sqlite3.OperationalError as exc:
        normalized = str(exc).lower()
        if "no such table" in normalized and "jobs" in normalized:
            log.debug("Skipping archived status migration: jobs table missing")
            return
        if "no such column" in normalized and "output_dir" in normalized:
            log.debug("Skipping archived status migration: output_dir column missing")
            return
        raise
    for row in rows:
        real_status = "stopped"  # safer default — under-count success rather than over-count
        if row["output_dir"]:
            # Detect actual status from disk
            from pathlib import Path as _P

            out = _P(row["output_dir"])
            for d in [out / "submit"] + sorted(out.glob("submit-*")):
                if (d / "application_submission_result.json").exists():
                    try:
                        data = json.loads((d / "application_submission_result.json").read_text())
                        mapped = _SUBMISSION_STATUS_MAP.get(data.get("status", ""))
                        if mapped:
                            real_status = mapped
                    except (json.JSONDecodeError, OSError):
                        pass
                    break
                if (d / "application_confirmation_website.json").exists():
                    real_status = "submitted"
                    break
        conn.execute(
            "UPDATE jobs SET status = ?, archived = TRUE WHERE id = ?",
            (real_status, row["id"]),
        )
    if rows:
        conn.commit()


def sync_job_from_disk(conn: sqlite3.Connection, job_id: int) -> dict:
    """Read on-disk artifacts for a job and update DB fields and events.

    Never overwrites archived status — that's an explicit user/worker action.
    Returns a dict of what was synced (for logging/display).
    """
    job = get_job(conn, job_id)
    if not job or not job.get("output_dir"):
        return {"synced": False, "reason": "no output_dir"}

    out = _repo_local_output_dir(str(job["output_dir"]))
    if not out.exists():
        return {"synced": False, "reason": "output_dir missing"}

    job_board = str(job["board"] or "").strip().casefold() or None
    if job_board == "unknown":
        job_board = None
    synced = {"synced": True, "updates": [], "changed": False}
    updates: list[str] = synced["updates"]
    generic_updates: list[str] = []
    _clear = object()
    pending_updates: dict[str, object] = {}
    pending_update_notes: dict[str, str] = {}
    stale_failure_columns: set[str] = set()

    if str(out) != str(job["output_dir"]):
        pending_updates["output_dir"] = str(out)
        pending_update_notes["output_dir"] = "output_dir→repo_local"

    def _current_value(column: str) -> object:
        value = pending_updates.get(column, job.get(column))
        return None if value is _clear else value

    def _set_pending_note(column: str, note: str | None) -> None:
        if note:
            pending_update_notes[column] = note

    def _stage_value(column: str, value: object, note: str | None = None) -> bool:
        if _current_value(column) == value:
            return False
        pending_updates[column] = value
        _set_pending_note(column, note)
        return True

    def _stage_null(column: str, note: str | None = None) -> bool:
        if _current_value(column) is None:
            return False
        pending_updates[column] = _clear
        _set_pending_note(column, note)
        return True

    def _discard_pending_update(column: str) -> None:
        pending_updates.pop(column, None)
        pending_update_notes.pop(column, None)

    def _append_update(note: str) -> None:
        generic_updates.append(note)

    def _stage_ready_draft_status() -> None:
        _stage_value("status", "draft", note="status→draft")
        had_failure_metadata = _current_value("failure_type") is not None
        if had_failure_metadata:
            _stage_null("failure_type")
            stale_failure_columns.add("failure_type")
        had_auth_state = _current_value("auth_state") is not None
        if had_auth_state:
            _stage_null("auth_state")
            stale_failure_columns.add("auth_state")
        had_error_message = _current_value("error_message") is not None
        if had_error_message:
            _stage_null("error_message")
            stale_failure_columns.add("error_message")
        if str(_current_value("progress") or "").strip():
            _stage_value("progress", "", note="progress→cleared")

    def _stage_pending_user_input_state(message: str | None) -> None:
        pending_message = str(message or "").strip() or (
            "Submission paused because one or more answers require manual user input."
        )
        _stage_value("status", "stopped", note="status→stopped")
        _stage_value("failure_type", "pending_user_input", note="failure_type→pending_user_input")
        _stage_value("error_message", pending_message, note=f"error: {pending_message[:60]}")
        if _current_value("auth_state") is not None:
            _stage_null("auth_state", note="cleared stale auth state")
            stale_failure_columns.add("auth_state")
        if _current_value("auth_scope") is not None:
            _stage_null("auth_scope", note="cleared stale auth scope")
            stale_failure_columns.add("auth_scope")
        if str(_current_value("progress") or "").strip():
            _stage_value("progress", "", note="progress→cleared")

    def _stage_duplicate_output_dir_owner_state(owner_job: dict) -> None:
        duplicate_label = (
            f"{owner_job.get('company') or '?'} — {owner_job.get('role_title') or '?'} (job #{owner_job['id']})"
        )
        message = f"Duplicate proof directory — matches job #{owner_job['id']} ({duplicate_label})"
        _stage_value("status", "stopped", note="status→stopped")
        _stage_value("archived", True, note="duplicate→archived")
        _stage_value("failure_type", "duplicate", note="failure_type→duplicate")
        _stage_value("error_message", message, note=f"error: {message[:60]}")
        if _current_value("auth_state") is not None:
            _stage_null("auth_state", note="cleared stale auth state")
            stale_failure_columns.add("auth_state")
        if _current_value("auth_scope") is not None:
            _stage_null("auth_scope", note="cleared stale auth scope")
            stale_failure_columns.add("auth_scope")
        if str(_current_value("progress") or "").strip():
            _stage_value("progress", "", note="progress→cleared")
        _append_update(f"proof_owner→job#{owner_job['id']}")

    def _finalize_sync(
        *,
        should_record_confirmed_submission: bool,
        confirmed_submission_at: str | None,
        email_confirmation: dict | None,
    ) -> dict:
        nonlocal pending_updates

        if should_record_confirmed_submission:
            for column in ("status", "failure_type", "auth_state", "auth_scope", "error_message", "archived"):
                _discard_pending_update(column)

        retained_pending_updates: dict[str, object] = {}
        for column, value in pending_updates.items():
            current_value = job.get(column)
            normalized_value = None if value is _clear else value
            if current_value == normalized_value:
                pending_update_notes.pop(column, None)
                continue
            retained_pending_updates[column] = value
        pending_updates = retained_pending_updates

        for column in pending_updates:
            note = pending_update_notes.get(column)
            if note:
                updates.append(note)
        if stale_failure_columns & pending_updates.keys():
            updates.append("cleared stale failure metadata")
        updates.extend(generic_updates)

        if pending_updates:
            sets: list[str] = []
            params: list[object] = []
            for column, value in pending_updates.items():
                if value is _clear:
                    sets.append(f"{column} = NULL")
                else:
                    sets.append(f"{column} = ?")
                    params.append(value)
            params.append(job_id)
            conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)
            conn.commit()
            synced["changed"] = True

        if should_record_confirmed_submission and record_confirmed_submission(
            conn,
            job_id,
            confirmed_at=confirmed_submission_at,
            initiator="disk_sync",
        ):
            updates.append("submission→submitted")
            synced["changed"] = True

        maybe_backfill_confirmation_email_reply(
            conn,
            job_id,
            job=job,
            out_dir=out,
            email_confirmation=email_confirmation,
            synced=synced,
            log_event_fn=log_event,
        )

        synced["changed"] = synced["changed"] or bool(updates)
        return synced

    from output_layout import existing_submit_dirs

    submit_dir = out / "submit"
    content_dir = out / "content"
    email_confirmation: dict | None = None
    result_artifact_found = False
    result_status: str | None = None
    result_mapped_status: str | None = None
    result_submit_dir: Path | None = None
    clear_auth_state_from_result = False
    clear_auth_scope_from_result = False
    should_record_confirmed_submission = False
    confirmed_submission_at: str | None = None

    proof_owner_conflict = _find_locked_output_dir_owner(
        conn,
        out,
        exclude_job_id=job_id,
        older_than_job_id=job_id,
    )
    if proof_owner_conflict is not None and not job.get("confirmed_at") and _submission_lock_state(job) != "locked":
        _stage_duplicate_output_dir_owner_state(proof_owner_conflict)
        return _finalize_sync(
            should_record_confirmed_submission=False,
            confirmed_submission_at=None,
            email_confirmation=email_confirmation,
        )

    # --- 1. Submission result (most authoritative status source) ---
    for candidate in existing_submit_dirs(out):
        result_path = candidate / "application_submission_result.json"
        if result_path.exists():
            result_artifact_found = True
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
                result_submit_dir = candidate
                result_status = str(data.get("status") or "").strip()
                mapped = _SUBMISSION_STATUS_MAP.get(result_status)
                result_mapped_status = mapped
                if mapped == "submitted":
                    should_record_confirmed_submission = True
                    confirmed_submission_at = str(
                        data.get("confirmed_at_utc") or data.get("confirmed_at") or ""
                    ).strip()
                    if not confirmed_submission_at:
                        confirmed_submission_at = None
                if mapped and mapped != "submitted":
                    _stage_value("status", mapped, note=f"status→{mapped}")
                result_board = str(data.get("board") or "").strip().casefold()
                if result_board == "unknown":
                    result_board = ""
                if result_board and result_board != job_board:
                    if _stage_value("board", result_board, note=f"board→{result_board}"):
                        job_board = result_board
                result_auth_scope = str(data.get("auth_scope") or "").strip()
                if result_auth_scope:
                    _stage_value("auth_scope", result_auth_scope, note=f"auth_scope→{result_auth_scope}")
                elif _current_value("auth_scope") is not None:
                    clear_auth_scope_from_result = True
                result_auth_state = str(data.get("auth_state") or "").strip()
                if result_auth_state:
                    _stage_value("auth_state", result_auth_state, note=f"auth_state→{result_auth_state}")
                elif _current_value("auth_state") is not None:
                    clear_auth_state_from_result = True
                result_failure_type = str(data.get("failure_type") or "").strip() or None
                result_reason = str(data.get("reason") or "").strip() or None
                classified_failure: str | None = None
                if result_status == "failed":
                    classified_failure = result_failure_type
                elif result_status == "not_easy_apply":
                    classified_failure = result_failure_type or result_reason or "not_easy_apply"
                elif mapped == "stopped":
                    classified_failure = result_failure_type or result_reason or result_status or None
                if classified_failure:
                    _stage_value("failure_type", classified_failure, note=f"failure_type→{classified_failure}")
                result_message = data.get("message")
                if mapped == "stopped" and result_message:
                    message = str(result_message)[:500]
                    _stage_value("error_message", message, note=f"error: {str(result_message)[:60]}")
                if result_status == "job_closed":
                    _stage_value("archived", True, note="job_closed→archived")
                # Confirmation method
                if data.get("website_confirmed"):
                    _stage_value("confirmation_method", "website", note="confirmation_method→website")
            except (json.JSONDecodeError, OSError):
                pass
            break

    # --- 2. Website confirmation fallback ---
    if "confirmation_method" not in pending_updates:
        for candidate in [submit_dir] + sorted(out.glob("submit-*")):
            conf_path = candidate / "application_confirmation_website.json"
            if conf_path.exists():
                try:
                    data = json.loads(conf_path.read_text(encoding="utf-8"))
                    changed = _stage_value("confirmation_method", "website")
                    confirmed_at = data.get("confirmed_at") or data.get("confirmed_at_utc")
                    should_record_confirmed_submission = True
                    if confirmed_submission_at is None and confirmed_at:
                        confirmed_submission_at = str(confirmed_at).strip() or None
                    if changed:
                        _append_update("website_confirmation synced")
                except (json.JSONDecodeError, OSError):
                    pass
                break

    # --- 3. Email confirmation ---
    for candidate in [submit_dir] + sorted(out.glob("submit-*")):
        email_path = candidate / "application_confirmation_email.json"
        if email_path.exists():
            try:
                data = json.loads(email_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    email_confirmation = data
                changed = _stage_value("email_confirmed", True, note="email_confirmed")
                if "confirmation_method" not in pending_updates:
                    _stage_value("confirmation_method", "email")
                elif not changed and _current_value("confirmation_method") == "email":
                    pass
            except (json.JSONDecodeError, OSError):
                pass
            break

    # --- 4. Notion sync status ---
    for candidate in [submit_dir] + sorted(out.glob("submit-*")):
        notion_path = candidate / "notion_sync_status.json"
        if notion_path.exists():
            try:
                data = json.loads(notion_path.read_text(encoding="utf-8"))
                ns = data.get("status")
                if ns:
                    _stage_value("notion_sync_status", ns, note=f"notion_sync→{ns}")
                page_id = data.get("page_id")
                if page_id:
                    _stage_value("notion_page_id", page_id)
                page_url = data.get("page_url")
                if page_url and not _current_value("notion_url"):
                    _stage_value("notion_url", page_url, note="notion_url set")
            except (json.JSONDecodeError, OSError):
                pass
            break

    # --- 5. Autofill report → field counts ---
    for report_path in list(submit_dir.glob("*_autofill_report.json")) if submit_dir.exists() else []:
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
            fields = data.get("fields", [])
            if fields:
                total = len(fields)
                filled = sum(1 for f in fields if f.get("status") == "filled")
                skipped = sum(1 for f in fields if f.get("status") == "skipped")
                errored = sum(1 for f in fields if f.get("status") == "error")
                field_counts_changed = False
                field_counts_changed |= _stage_value("total_form_fields", total)
                field_counts_changed |= _stage_value("fields_filled", filled)
                field_counts_changed |= _stage_value("fields_skipped", skipped)
                field_counts_changed |= _stage_value("fields_errored", errored)
                if field_counts_changed:
                    _append_update(f"fields: {filled}/{total} filled, {skipped} skipped, {errored} errors")
        except (json.JSONDecodeError, OSError):
            pass
        break

    # --- 6. Phase durations → events (if not already logged) ---
    if submit_dir.exists():
        for dur_file in sorted(submit_dir.glob("*_phase_durations.json")):
            try:
                phases = json.loads(dur_file.read_text(encoding="utf-8"))
                if isinstance(phases, list):
                    for phase in phases:
                        phase_name = phase.get("phase", "unknown")
                        duration_ms = phase.get("duration_ms", 0)
                        exit_code = phase.get("exit_code")
                        # Log as event if not already present
                        existing = conn.execute(
                            "SELECT id FROM events WHERE job_id = ? AND event_type = ? AND detail = ?",
                            (job_id, "phase_completed", phase_name),
                        ).fetchone()
                        if not existing:
                            detail_j = {"duration_ms": duration_ms, "exit_code": exit_code}
                            log_event(conn, job_id, "phase_completed", detail=phase_name, detail_json=detail_j)
                            _append_update(f"phase:{phase_name} {duration_ms}ms")
            except (json.JSONDecodeError, OSError):
                pass
            break

    # --- 7. Provider used (check answers, then LLM log filenames) ---
    if not job.get("provider"):
        provider_found = None
        for candidate in [submit_dir] + sorted(out.glob("submit-*")):
            answers_path = candidate / "application_answers.json"
            if answers_path.exists():
                try:
                    data = json.loads(answers_path.read_text(encoding="utf-8"))
                    provider_found = data.get("provider")
                except (json.JSONDecodeError, OSError):
                    pass
                break
        # Fallback: check LLM log filenames for provider hints
        if not provider_found:
            for log_dir in (content_dir, out):
                if not log_dir.exists():
                    continue
                for log in log_dir.glob("llm_*_raw.txt"):
                    # Read first few lines to find provider info
                    try:
                        head = log.read_text(encoding="utf-8", errors="replace")[:500]
                        if "gemini" in head.lower():
                            provider_found = "gemini"
                        elif "claude" in head.lower():
                            provider_found = "claude"
                        elif "codex" in head.lower():
                            provider_found = "codex"
                        if provider_found:
                            break
                    except OSError:
                        pass
                if provider_found:
                    break
        if provider_found:
            _stage_value("provider", provider_found, note=f"provider→{provider_found}")

    # --- 7. Error details from unsupported_board / auth failures / unavailable postings ---
    for err_file in (
        "job_unavailable.json",
        "unsupported_board.json",
        "workday_auth_failure.json",
        "manual_review.json",
    ):
        err_path = None
        if err_file == "workday_auth_failure.json" and result_submit_dir is not None:
            candidate = result_submit_dir / err_file
            if candidate.exists():
                err_path = candidate
        else:
            for candidate_submit_dir in existing_submit_dirs(out):
                candidate = candidate_submit_dir / err_file
                if candidate.exists():
                    err_path = candidate
                    break
        if result_artifact_found and err_file in {"job_unavailable.json", "unsupported_board.json"}:
            continue
        if err_path is not None and err_path.exists():
            try:
                data = json.loads(err_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    data = data[0] if data else {}
            except (json.JSONDecodeError, OSError):
                continue

            if err_file == "job_unavailable.json":
                status_changed = _stage_value("status", "stopped")
                failure_changed = _stage_value("failure_type", "job_closed")
                archived_changed = _stage_value("archived", True)
                if status_changed or failure_changed or archived_changed:
                    updates.append("job_unavailable→archived")
            elif err_file == "unsupported_board.json":
                status_changed = _stage_value("status", "stopped")
                failure_changed = _stage_value("failure_type", "unsupported")
                if status_changed or failure_changed:
                    updates.append("unsupported_board→unsupported")

            if err_file == "workday_auth_failure.json":
                auth_state = str(data.get("auth_state") or "").strip()
                if auth_state and "auth_state" not in pending_updates:
                    _stage_value("auth_state", auth_state, note=f"auth_state→{auth_state}")
                auth_scope = data.get("auth_scope")
                if auth_scope:
                    _stage_value("auth_scope", str(auth_scope), note=f"auth_scope→{auth_scope}")
                has_current_result_classification = result_artifact_found and any(
                    column == "failure_type" for column in pending_updates
                )
                if has_current_result_classification:
                    reason = None
                    _append_update("workday_auth_artifact_present")
                else:
                    failure_type, reason = _normalize_workday_auth_artifact(data)
                    if failure_type == "job_closed":
                        status_changed = _stage_value("status", "stopped")
                        failure_changed = _stage_value("failure_type", "job_closed")
                        archived_changed = _stage_value("archived", True)
                        if status_changed or failure_changed or archived_changed:
                            updates.append("workday_auth→job_closed")
                    elif failure_type in {"auth_failed", "auth_unknown", "auth_guarded", "service_unavailable"}:
                        _stage_value("failure_type", failure_type, note=f"failure_type→{failure_type}")
                    elif reason:
                        _append_update("workday_auth_artifact_present")
            else:
                reason = None

            reason = reason or data.get("message") or data.get("reason") or data.get("issue") or data.get("status", "")
            if (
                reason
                and "error_message" not in pending_updates
                and (
                    err_file in {"job_unavailable.json", "unsupported_board.json"}
                    or _should_replace_stale_error_message(job.get("error_message"))
                )
            ):
                message = str(reason)[:500]
                _stage_value("error_message", message, note=f"error: {str(reason)[:60]}")
            break

    if clear_auth_state_from_result and "auth_state" not in pending_updates:
        _stage_null("auth_state", note="cleared stale auth state")
    if clear_auth_scope_from_result and "auth_scope" not in pending_updates:
        _stage_null("auth_scope", note="cleared stale auth scope")

    # --- 8. Greenhouse traceback backfill for historical pre-result failures ---
    if (
        not result_artifact_found
        and str(job["board"] or "").strip().casefold() == "greenhouse"
        and "failure_type" not in pending_updates
    ):
        inferred = _infer_greenhouse_failure_from_submit_output(conn, job_id)
        if inferred is not None:
            inferred_failure_type, inferred_message = inferred
            _stage_value("failure_type", inferred_failure_type, note=f"failure_type→{inferred_failure_type}")
            _stage_value("error_message", inferred_message[:500], note=f"error: {inferred_message[:60]}")

    # --- 9. Board detection (if missing or stale unknown) ---
    if not job_board:
        meta_path = out / ".pipeline_meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                board = meta.get("board")
                if board:
                    normalized_board = str(board).strip().casefold() or None
                    if normalized_board == "unknown":
                        normalized_board = None
                    if normalized_board and _stage_value("board", normalized_board, note=f"board→{normalized_board}"):
                        job_board = normalized_board
            except (json.JSONDecodeError, OSError):
                pass
        if not job_board:
            try:
                from submit_review_common import resolve_current_submit_artifacts

                resolved = resolve_current_submit_artifacts(out, board_name=None)
                resolved_board = str(resolved.get("board_name") or "").strip().casefold() or None
                if resolved_board == "unknown":
                    resolved_board = None
                if resolved_board and _stage_value("board", resolved_board, note=f"board→{resolved_board}"):
                    job_board = resolved_board
            except Exception:
                pass

    # --- 9b. Current answer-state reconciliation from active proof ---
    try:
        from answer_state_sync import sync_current_attempt_answer_states_from_proof

        sync_current_attempt_answer_states_from_proof(out)
    except Exception:
        pass

    # --- 10. Ready draft-proof reconciliation for historical stopped rows ---
    allow_ready_draft_override = (
        result_artifact_found
        and result_mapped_status == "stopped"
        and _stopped_result_artifact_is_stale_vs_current_draft_proof(
            out,
            board_name=job_board,
            result_submit_dir=result_submit_dir,
        )
    )
    if (
        job["status"] not in {"approved", "submitting", "awaiting_captcha", "archived"}
        and ("status" not in pending_updates or allow_ready_draft_override)
        and result_mapped_status != "submitted"
        and result_status != "job_closed"
        and not should_record_confirmed_submission
    ):
        from application_submit_common import load_pending_user_input_for_submit_attempt
        from pipeline_draft_proof import _validate_draft_completeness, draft_review_state

        review_state = draft_review_state(out, board_name=job_board)
        missing = _validate_draft_completeness(out, board_name=job_board)
        ready_draft_status_hint = _load_ready_draft_status_hint(out)
        pending = load_pending_user_input_for_submit_attempt(out)
        pending_payload = pending[1] if pending is not None else None
        if _saved_draft_review_state_requires_refresh(out, review_state):
            try:
                from draft_manager import generate_draft_summary

                submit_dirname = str(review_state.get("submit_dirname") or "submit").strip() or "submit"
                submit_dir = out / submit_dirname
                draft_meta_path = out / ".pipeline_meta.json"
                draft_meta = (
                    json.loads(draft_meta_path.read_text(encoding="utf-8"))
                    if draft_meta_path.exists()
                    else {
                        "board": job_board,
                        "company": job.get("company"),
                        "role_title": job.get("role_title"),
                    }
                )
                if submit_dir.is_dir():
                    generate_draft_summary(out, submit_dir, draft_meta)
                    review_state = draft_review_state(out, board_name=job_board)
                    missing = _validate_draft_completeness(out, board_name=job_board)
                    ready_draft_status_hint = _load_ready_draft_status_hint(out)
                    _append_update("draft_summary→refreshed")
            except Exception:
                pass
        if review_state.get("state") == "ready" and missing == ["draft summary screenshot"]:
            try:
                from draft_manager import generate_draft_summary

                submit_dirname = str(review_state.get("submit_dirname") or "submit").strip() or "submit"
                submit_dir = out / submit_dirname
                draft_meta_path = out / ".pipeline_meta.json"
                draft_meta = (
                    json.loads(draft_meta_path.read_text(encoding="utf-8"))
                    if draft_meta_path.exists()
                    else {
                        "board": job_board,
                        "company": job.get("company"),
                        "role_title": job.get("role_title"),
                    }
                )
                if submit_dir.is_dir():
                    generate_draft_summary(out, submit_dir, draft_meta)
                    missing = _validate_draft_completeness(out, board_name=job_board)
                    if "draft summary screenshot" not in missing:
                        _append_update("draft_summary→regenerated")
            except Exception:
                pass
        if pending_payload is not None:
            _stage_pending_user_input_state(pending_payload.get("message"))
        elif review_state.get("state") == "ready" and not missing:
            _stage_ready_draft_status()
        elif (
            pending_payload is None
            and missing == ["current-attempt review screenshot"]
            and str(_current_value("status") or "").strip() in LEGACY_REVIEW_ONLY_DRAFT_RESCUE_STATUSES
        ):
            had_existing_ready_draft_gap = (
                str(job.get("status") or "").strip() != "draft"
                or any(job.get(column) is not None for column in ("failure_type", "auth_state", "error_message"))
                or bool(str(job.get("progress") or "").strip())
            )
            _stage_ready_draft_status()
            if had_existing_ready_draft_gap:
                _append_update("draft_proof→legacy_review_only")
        elif (
            ready_draft_status_hint is not None
            and str(review_state.get("state") or "").strip() == "legacy"
            and pending_payload is None
            and not result_artifact_found
        ):
            # Disk sync must trust the current repo-local proof over stale
            # draft_status.json metadata. Only let the saved ready hint rescue
            # explicit legacy rows; a currently blocked proof state wins.
            _stage_ready_draft_status()
            _append_update("draft_status→ready")

    return _finalize_sync(
        should_record_confirmed_submission=should_record_confirmed_submission,
        confirmed_submission_at=confirmed_submission_at,
        email_confirmation=email_confirmation,
    )


def repair_stale_processing_jobs(
    conn: sqlite3.Connection,
    *,
    stale_threshold_seconds: int = 300,
    limit: int = 200,
    exclude_job_ids: set[int] | None = None,
) -> dict[str, int]:
    """Repair stale in-progress rows that no active worker is still advancing.

    The repair flow is intentionally conservative:
    1. Reconcile from repo-local proof first via ``sync_job_from_disk``.
    2. If the row is still stuck in a non-claimable processing status, reset it out
       of the processing bucket using the same safety posture as startup recovery.
    """

    placeholders = ",".join("?" * len(STALE_PROCESSING_REPAIR_STATUSES))
    where_clauses = [
        f"status IN ({placeholders})",
        "(archived IS NULL OR archived = FALSE)",
    ]
    params: list[object] = [*STALE_PROCESSING_REPAIR_STATUSES]

    normalized_exclusions = sorted({int(job_id) for job_id in (exclude_job_ids or set())})
    if normalized_exclusions:
        exclusion_placeholders = ",".join("?" * len(normalized_exclusions))
        where_clauses.append(f"id NOT IN ({exclusion_placeholders})")
        params.extend(normalized_exclusions)

    if stale_threshold_seconds > 0:
        where_clauses.append("updated_at < datetime('now', ? || ' seconds')")
        params.append(f"-{stale_threshold_seconds}")

    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT id
        FROM jobs
        WHERE {" AND ".join(where_clauses)}
        ORDER BY updated_at ASC, id ASC
        LIMIT ?
        """,
        params,
    ).fetchall()

    summary = {
        "scanned": 0,
        "changed": 0,
        "promoted_to_draft": 0,
        "promoted_to_submitted": 0,
        "promoted_to_stopped": 0,
        "reset_to_queued": 0,
    }

    for row in rows:
        job_id = int(row["id"])
        before = get_job(conn, job_id)
        if before is None:
            continue
        summary["scanned"] += 1
        before_status = str(before.get("status") or "")

        sync_job_from_disk(conn, job_id)
        after = get_job(conn, job_id)
        if after is None:
            continue

        after_status = str(after.get("status") or "")
        if after_status in STALE_PROCESSING_REPAIR_STATUSES:
            repair_out_dir = _repo_local_output_dir(str(after.get("output_dir") or ""))
            if _current_proof_is_legacy_ready_except_review_screenshot(
                repair_out_dir,
                board_name=str(after.get("board") or "").strip() or None,
            ):
                conn.execute(
                    "UPDATE jobs "
                    "SET status = 'draft', failure_type = NULL, auth_state = NULL, auth_scope = NULL, "
                    "error_message = NULL, progress = '' "
                    "WHERE id = ?",
                    (job_id,),
                )
                log_event(conn, job_id, "status_change", detail="draft", initiator="system")
                conn.commit()
                after = get_job(conn, job_id)
                if after is None:
                    continue
                after_status = str(after.get("status") or "")
            if after_status not in STALE_PROCESSING_REPAIR_STATUSES:
                pass
            else:
                was_previously_submitted = bool(
                    conn.execute(
                        "SELECT 1 FROM events WHERE job_id = ? AND event_type = 'submitted' LIMIT 1",
                        (job_id,),
                    ).fetchone()
                )
                if was_previously_submitted:
                    update_status(
                        conn,
                        job_id,
                        "stopped",
                        error_message="Reset: stale job (previously submitted)",
                        failure_type="crash",
                        retry_after=RETRY_AFTER_SENTINEL,
                        initiator="system",
                    )
                else:
                    update_status(
                        conn,
                        job_id,
                        "queued",
                        error_message="Reset: stale in-progress job",
                        clear_provider=True,
                        retry_after=RETRY_AFTER_SENTINEL,
                        initiator="system",
                    )
                after = get_job(conn, job_id)
                if after is None:
                    continue
                after_status = str(after.get("status") or "")

        if after_status == before_status:
            continue

        summary["changed"] += 1
        if after_status == "draft":
            summary["promoted_to_draft"] += 1
        elif after_status == "submitted":
            summary["promoted_to_submitted"] += 1
        elif after_status == "stopped":
            summary["promoted_to_stopped"] += 1
        elif after_status == "queued":
            summary["reset_to_queued"] += 1

    return summary


def sync_all_jobs_from_disk(conn: sqlite3.Connection) -> dict:
    """Sync all jobs that have output_dir from on-disk artifacts.

    Returns summary stats.
    """
    rows = conn.execute("SELECT id FROM jobs WHERE output_dir IS NOT NULL ORDER BY id").fetchall()
    total = len(rows)
    synced = 0
    for row in rows:
        result = sync_job_from_disk(conn, row["id"])
        if result.get("updates"):
            synced += 1
    return {"total": total, "synced": synced}
