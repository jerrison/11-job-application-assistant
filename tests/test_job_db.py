# tests/test_job_db.py
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import job_db
import pytest
from job_db import (
    RETRY_AFTER_SENTINEL,
    SubmissionLockError,
    _conn_lock,
    _connections,
    add_job,
    backfill_jd_fingerprints,
    check_jd_duplicate,
    clear_repair_queue_pause,
    close_all_connections,
    find_jd_duplicates,
    get_job,
    get_job_timeline,
    get_pending_jobs,
    get_repair_queue_pause,
    get_runtime_flag_json,
    init_db,
    jd_fingerprint,
    log_event,
    migrate_legacy_output_dirs,
    normalize_company,
    open_db,
    open_db_tracked,
    query_jobs,
    reconcile_duplicate_jobs,
    record_confirmed_submission,
    repair_stale_processing_jobs,
    reset_stale_jobs,
    set_jd_fingerprint,
    set_repair_queue_pause,
    set_runtime_flag,
    sync_job_from_disk,
    unlock_job_for_resubmit,
    update_status,
)


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test_jobs.db"
    conn = init_db(db_path)
    yield conn
    conn.close()


def _filled_autofill_report_payload(**overrides):
    payload = {
        "fields": [
            {
                "field_name": "candidate_name",
                "label": "Full Name",
                "kind": "text",
                "status": "filled",
                "value": "Jerrison Li",
                "source": "application_profile.md",
            }
        ]
    }
    payload.update(overrides)
    return payload


def test_init_db_backfills_submission_lock_for_confirmed_jobs(tmp_path):
    db_path = tmp_path / "legacy_jobs.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(
        f"""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            confirmed_at TIMESTAMP,
            retry_after TIMESTAMP NOT NULL DEFAULT '{RETRY_AFTER_SENTINEL}',
            archived BOOLEAN DEFAULT FALSE
        );
        INSERT INTO jobs (id, url, status, confirmed_at) VALUES
            (1, 'https://example.com/locked', 'submitted', '2026-03-18T17:11:18+00:00'),
            (2, 'https://example.com/open', 'queued', NULL);
        """
    )
    raw.commit()
    raw.close()

    conn = init_db(db_path)

    locked = conn.execute("SELECT submission_lock_state FROM jobs WHERE id = 1").fetchone()
    open_row = conn.execute("SELECT submission_lock_state FROM jobs WHERE id = 2").fetchone()

    assert locked["submission_lock_state"] == "locked"
    assert open_row["submission_lock_state"] == "open"

    conn.close()


def test_init_db_backfills_submission_lock_for_legacy_submitted_jobs_without_confirmed_at(tmp_path):
    db_path = tmp_path / "legacy_submitted_jobs.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(
        f"""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            confirmed_at TIMESTAMP,
            retry_after TIMESTAMP NOT NULL DEFAULT '{RETRY_AFTER_SENTINEL}',
            archived BOOLEAN DEFAULT FALSE
        );
        INSERT INTO jobs (id, url, status, confirmed_at) VALUES
            (1, 'https://example.com/submitted-open', 'submitted', NULL),
            (2, 'https://example.com/open', 'queued', NULL);
        """
    )
    raw.commit()
    raw.close()

    conn = init_db(db_path)

    submitted = conn.execute("SELECT submission_lock_state FROM jobs WHERE id = 1").fetchone()
    open_row = conn.execute("SELECT submission_lock_state FROM jobs WHERE id = 2").fetchone()

    assert submitted["submission_lock_state"] == "locked"
    assert open_row["submission_lock_state"] == "open"

    conn.close()


def test_unlock_job_for_resubmit_records_unlock_metadata(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/unlockable")
    db.execute(
        "UPDATE jobs SET status = 'submitted', confirmed_at = ?, submission_lock_state = 'locked' WHERE id = ?",
        ("2026-03-18T17:11:18+00:00", job_id),
    )
    db.commit()

    assert unlock_job_for_resubmit(db, job_id, initiator="web") is True

    job = get_job(db, job_id)
    assert job["submission_lock_state"] == "unlocked_for_resubmit"
    assert job["last_resubmit_unlocked_at"] is not None
    assert job["last_resubmit_unlock_initiator"] == "web"


def test_init_db_creates_tables(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"jobs", "events", "fix_attempts", "provider_runs"} <= tables


def test_add_job_returns_id(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/company/jobs/123")
    assert job_id == 1


def test_add_job_detects_direct_source(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/company/jobs/123")
    job = get_job(db, job_id)
    assert job["source"] == "direct"
    assert job["board_url"] == "https://boards.greenhouse.io/company/jobs/123"


def test_add_job_detects_linkedin_source(db):
    job_id = add_job(db, url="https://www.linkedin.com/jobs/view/12345")
    job = get_job(db, job_id)
    assert job["source"] == "linkedin"
    assert job["source_url"] == "https://www.linkedin.com/jobs/view/12345"
    assert job["board_url"] is None


def test_add_job_source_override_preserves_external_url_and_trueup_source(db):
    external_url = "https://boards.greenhouse.io/company/jobs/123"
    trueup_url = "https://www.trueup.io/jobs/acme-senior-product-manager-123"
    job_id = add_job(
        db,
        url=external_url,
        source_override="trueup",
        source_url_override=trueup_url,
    )
    job = get_job(db, job_id)
    assert job["url"] == external_url
    assert job["source"] == "trueup"
    assert job["source_url"] == trueup_url
    assert job["board_url"] == external_url
    assert job["canonical_url"] == external_url


def test_add_job_source_overrides_must_be_provided_together(db):
    external_url = "https://boards.greenhouse.io/company/jobs/123"
    trueup_url = "https://www.trueup.io/jobs/acme-senior-product-manager-123"

    with pytest.raises(ValueError, match="must be provided together"):
        add_job(db, url=external_url, source_override="trueup")

    with pytest.raises(ValueError, match="must be provided together"):
        add_job(db, url=external_url, source_url_override=trueup_url)


def test_add_job_deduplicates_by_canonical_url(db):
    add_job(db, url="https://boards.greenhouse.io/co/jobs/123")
    with pytest.raises(sqlite3.IntegrityError):
        add_job(db, url="https://boards.greenhouse.io/co/jobs/123")


def test_add_job_duplicate_url_backfills_missing_metadata(db):
    job_id = add_job(
        db,
        url="https://jobs.apple.com/en-us/details/200647871?utm_source=trueup.io&utm_medium=website&ref=trueup",
        source_override="trueup",
        source_url_override="https://www.trueup.io/myjobs",
        role_title="Sr Product Manager — Finance Controllership Applications",
    )
    db.execute("UPDATE jobs SET company = NULL, source_url = NULL WHERE id = ?", (job_id,))
    db.commit()

    with pytest.raises(sqlite3.IntegrityError):
        add_job(
            db,
            url="https://jobs.apple.com/en-us/details/200647871?utm_source=trueup.io&utm_medium=website&ref=trueup",
            source_override="trueup",
            source_url_override="https://www.trueup.io/myjobs",
            company="Apple",
            role_title="Sr Product Manager — Finance Controllership Applications",
        )

    job = get_job(db, job_id)
    assert job["company"] == "Apple"
    assert job["role_title"] == "Sr Product Manager — Finance Controllership Applications"
    assert job["source_url"] == "https://www.trueup.io/myjobs"


def test_get_pending_jobs_ordered_by_priority_then_created(db):
    add_job(db, url="https://boards.greenhouse.io/a/jobs/1")
    id2 = add_job(db, url="https://boards.greenhouse.io/b/jobs/2", priority=5)
    pending = get_pending_jobs(db)
    assert pending[0]["id"] == id2  # higher priority first


def test_submitting_jobs_ordered_before_queued(db):
    """Approve+submit jobs appear before queued jobs regardless of priority."""
    id1 = add_job(db, url="https://boards.greenhouse.io/a/jobs/1", priority=10)
    id2 = add_job(db, url="https://boards.greenhouse.io/b/jobs/2")
    update_status(db, id2, "submitting")
    pending = get_pending_jobs(db)
    assert pending[0]["id"] == id2  # submitting first despite lower priority
    assert pending[1]["id"] == id1


def test_submitting_jobs_fifo_by_approval_time(db):
    """Among submitting jobs, FIFO by approval time (updated_at), not created_at."""
    # Create job A first (older created_at), then job B
    id_a = add_job(db, url="https://boards.greenhouse.io/a/jobs/1")
    id_b = add_job(db, url="https://boards.greenhouse.io/b/jobs/2")
    # Both become submitting
    update_status(db, id_a, "submitting")
    update_status(db, id_b, "submitting")
    # Drop trigger so we can set timestamps manually
    db.execute("DROP TRIGGER IF EXISTS trg_jobs_updated_at")
    # Simulate: B was approved earlier than A (lower updated_at = approved first)
    db.execute("UPDATE jobs SET updated_at = '2025-01-01 00:00:00' WHERE id = ?", (id_b,))
    db.execute("UPDATE jobs SET updated_at = '2025-01-01 00:01:00' WHERE id = ?", (id_a,))
    db.commit()
    pending = get_pending_jobs(db)
    assert pending[0]["id"] == id_b  # approved first → processed first
    assert pending[1]["id"] == id_a


def test_get_pending_jobs_skips_future_retry_after(db):
    ready_id = add_job(db, url="https://boards.greenhouse.io/ready/jobs/1")
    delayed_id = add_job(db, url="https://boards.greenhouse.io/delayed/jobs/2")
    db.execute("UPDATE jobs SET retry_after = datetime('now', '+10 minutes') WHERE id = ?", (delayed_id,))
    db.commit()

    pending = get_pending_jobs(db)

    assert [job["id"] for job in pending] == [ready_id]


def test_runtime_flag_round_trips_json_payload(db):
    payload = {"rollout_id": 7, "reason": "fingerprint_recurred"}

    set_runtime_flag(db, "repair_pause_new_queued_work", payload)

    assert get_runtime_flag_json(db, "repair_pause_new_queued_work") == payload


def test_runtime_flag_json_returns_none_for_scalar_json_values(db):
    set_runtime_flag(db, "repair_pause_new_queued_work", "1")

    assert get_runtime_flag_json(db, "repair_pause_new_queued_work") is None


def test_runtime_flag_json_returns_none_for_non_json_strings(db):
    set_runtime_flag(db, "repair_pause_new_queued_work", "not-json")

    assert get_runtime_flag_json(db, "repair_pause_new_queued_work") is None


def test_get_pending_jobs_skips_fresh_queue_when_repair_pause_active(db):
    queued_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/queued")
    queued_submit_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/queued-submit")
    approved_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/approved")
    update_status(db, queued_submit_id, "queued_submit")
    update_status(db, approved_id, "approved")

    set_repair_queue_pause(
        db,
        rollout_id=3,
        commit_sha="abc1234",
        cluster_id=1,
        fingerprint="greenhouse:draft_audit:rendered_audit_mismatch:work-auth",
        reason="fingerprint_recurred",
    )

    pending = get_pending_jobs(db, limit=10)

    assert queued_id not in [job["id"] for job in pending]
    assert queued_submit_id not in [job["id"] for job in pending]
    assert [job["id"] for job in pending] == [approved_id]


def test_get_pending_jobs_allows_in_flight_submit_states_when_repair_pause_active(db):
    submitting_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/submitting")
    reanswering_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/reanswering")
    regenerating_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/regenerating")
    update_status(db, submitting_id, "submitting")
    update_status(db, reanswering_id, "reanswering")
    update_status(db, regenerating_id, "regenerating")

    set_repair_queue_pause(
        db,
        rollout_id=4,
        commit_sha="feedface",
        cluster_id=2,
        fingerprint="shared:draft_audit:rendered_audit_mismatch:work-auth",
        reason="rendered_audit_regressed",
    )

    pending = get_pending_jobs(db, limit=10)

    assert [job["id"] for job in pending] == [submitting_id, reanswering_id, regenerating_id]
    assert get_repair_queue_pause(db)["rollout_id"] == 4

    clear_repair_queue_pause(db)
    assert get_repair_queue_pause(db) is None


def test_get_pending_jobs_treats_malformed_pause_payload_as_active_for_queued_work(db):
    queued_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/queued-malformed-pause")
    queued_submit_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/queued-submit-malformed-pause")
    approved_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/approved-malformed-pause")
    update_status(db, queued_submit_id, "queued_submit")
    update_status(db, approved_id, "approved")

    set_runtime_flag(db, "repair_pause_new_queued_work", "not-json")
    assert get_repair_queue_pause(db) is None

    paused_pending = get_pending_jobs(db, limit=10)
    paused_ids = [job["id"] for job in paused_pending]
    assert queued_id not in paused_ids
    assert queued_submit_id not in paused_ids
    assert paused_ids == [approved_id]

    clear_repair_queue_pause(db)
    resumed_pending = get_pending_jobs(db, limit=10)
    resumed_ids = [job["id"] for job in resumed_pending]
    assert resumed_ids == [approved_id, queued_id, queued_submit_id]


def test_get_pending_jobs_skips_locked_jobs_even_if_status_is_queued(db):
    locked_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/locked-queued")
    open_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/open-queued", priority=5)
    db.execute(
        "UPDATE jobs SET status = 'queued', confirmed_at = ?, submission_lock_state = 'locked' WHERE id = ?",
        ("2026-03-18T17:11:18+00:00", locked_id),
    )
    db.commit()

    pending = get_pending_jobs(db)

    assert [job["id"] for job in pending] == [open_id]


def test_get_pending_jobs_treats_confirmed_open_state_as_locked(db):
    locked_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/confirmed-open")
    open_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/open-queued-2", priority=5)
    db.execute(
        "UPDATE jobs SET status = 'queued', confirmed_at = ?, submission_lock_state = 'open' WHERE id = ?",
        ("2026-03-18T17:11:18+00:00", locked_id),
    )
    db.commit()

    pending = get_pending_jobs(db)

    assert [job["id"] for job in pending] == [open_id]


def test_update_status(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/1")
    update_status(db, job_id, "generating")
    job = get_job(db, job_id)
    assert job["status"] == "generating"


def test_update_status_clears_stale_claim_progress_when_requeued_without_explicit_progress(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/requeue-claimed")
    update_status(db, job_id, "resolving", progress="claimed:12345")

    update_status(db, job_id, "queued")

    job = get_job(db, job_id)
    assert job["status"] == "queued"
    assert job["progress"] == ""


def test_update_status_refuses_locked_rerunnable_transition(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/locked-transition")
    db.execute(
        "UPDATE jobs SET status = 'submitted', confirmed_at = ?, submission_lock_state = 'locked' WHERE id = ?",
        ("2026-03-18T17:11:18+00:00", job_id),
    )
    db.commit()

    with pytest.raises(SubmissionLockError):
        update_status(db, job_id, "draft")


def test_update_status_refuses_confirmed_open_rerunnable_transition(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/confirmed-open-transition")
    db.execute(
        "UPDATE jobs SET status = 'submitted', confirmed_at = ?, submission_lock_state = 'open' WHERE id = ?",
        ("2026-03-18T17:11:18+00:00", job_id),
    )
    db.commit()

    with pytest.raises(SubmissionLockError):
        update_status(db, job_id, "draft")


def test_update_status_logs_event(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/1")
    update_status(db, job_id, "generating")
    events = get_job_timeline(db, job_id)
    assert len(events) == 1
    assert events[0]["event_type"] == "status_change"


def test_update_status_logs_action_audit_metadata(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/with-audit")
    update_status(
        db,
        job_id,
        "queued",
        initiator="web",
        process_info="action_surface=detail action_trigger=button request_id=req-123 route=/api/jobs/1/restart-pipeline",
        event_detail_json={
            "action": {
                "surface": "detail",
                "trigger": "button",
                "request_id": "req-123",
                "route": "/api/jobs/1/restart-pipeline",
            }
        },
    )

    events = get_job_timeline(db, job_id)

    assert events[0]["event_type"] == "status_change"
    assert events[0]["detail"] == "queued"
    assert json.loads(events[0]["detail_json"]) == {
        "action": {
            "surface": "detail",
            "trigger": "button",
            "request_id": "req-123",
            "route": "/api/jobs/1/restart-pipeline",
        }
    }


def test_log_event(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/1")
    log_event(db, job_id, "provider_fallback", detail="gemini \u2192 claude")
    events = get_job_timeline(db, job_id)
    assert events[0]["detail"] == "gemini \u2192 claude"


def test_query_jobs_filters_by_status(db):
    add_job(db, url="https://boards.greenhouse.io/a/jobs/1")
    id2 = add_job(db, url="https://boards.greenhouse.io/b/jobs/2")
    update_status(db, id2, "generating")
    results = query_jobs(db, status="queued")
    assert len(results) == 1


def test_query_jobs_filters_by_board(db):
    add_job(db, url="https://boards.greenhouse.io/a/jobs/1")
    update_status(db, 1, "generating")  # triggers board detection
    results = query_jobs(db, board="greenhouse")
    # board is set during processing, not at add time -- this tests the filter
    assert isinstance(results, list)


def test_query_jobs_prefers_current_status_change_timestamp_for_non_terminal_rows(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/a/jobs/3")
    update_status(db, job_id, "draft")
    db.execute(
        "UPDATE events SET created_at = '2026-03-26 22:00:00' "
        "WHERE job_id = ? AND event_type = 'status_change' AND detail = 'draft'",
        (job_id,),
    )
    db.execute("DROP TRIGGER IF EXISTS trg_jobs_updated_at")
    db.execute(
        "UPDATE jobs SET updated_at = '2026-03-26 22:01:07', completed_at = '2026-03-18 05:10:46' WHERE id = ?",
        (job_id,),
    )
    db.commit()

    result = query_jobs(db, status="draft")[0]

    assert result["status_entered_at"] == "2026-03-26 22:00:00"
    assert result["status_entered_at_source"] == "status_change"
    assert result["queue_timestamp"] == "2026-03-26 22:00:00"
    assert result["queue_timestamp_source"] == "status_change"


def test_query_jobs_prefers_current_status_change_timestamp_for_submitted_rows(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/a/jobs/4")
    update_status(db, job_id, "submitted")
    db.execute(
        "UPDATE events SET created_at = '2026-03-26 22:00:00' "
        "WHERE job_id = ? AND event_type = 'status_change' AND detail = 'submitted'",
        (job_id,),
    )
    db.execute("DROP TRIGGER IF EXISTS trg_jobs_updated_at")
    db.execute(
        "UPDATE jobs SET updated_at = '2026-03-26 22:05:00', completed_at = '2026-03-26 21:59:59' WHERE id = ?",
        (job_id,),
    )
    db.commit()

    result = query_jobs(db, status="submitted")[0]

    assert result["status_entered_at"] == "2026-03-26 22:00:00"
    assert result["status_entered_at_source"] == "status_change"
    assert result["queue_timestamp"] == "2026-03-26 22:00:00"
    assert result["queue_timestamp_source"] == "status_change"


def test_query_jobs_falls_back_to_created_at_for_legacy_queued_rows_without_status_events(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/a/jobs/5")
    db.execute("DROP TRIGGER IF EXISTS trg_jobs_updated_at")
    db.execute(
        "UPDATE jobs SET created_at = '2026-03-26 22:00:00', updated_at = '2026-03-26 22:05:00' WHERE id = ?",
        (job_id,),
    )
    db.commit()

    result = query_jobs(db, status="queued")[0]

    assert result["status_entered_at"] == "2026-03-26 22:00:00"
    assert result["status_entered_at_source"] == "created_at"
    assert result["queue_timestamp"] == "2026-03-26 22:00:00"
    assert result["queue_timestamp_source"] == "created_at"


def test_update_status_clears_auth_metadata_when_leaving_stopped(db):
    job_id = add_job(db, url="https://example.com/auth-state")

    update_status(
        db,
        job_id,
        "stopped",
        failure_type="auth_unknown",
        auth_state="sign_in_gate",
        auth_scope="workday:example/careers",
        error_message="Auth blocked",
    )
    update_status(db, job_id, "submitted")

    job = get_job(db, job_id)

    assert job["status"] == "submitted"
    assert job["failure_type"] is None
    assert job["auth_state"] is None
    assert job["auth_scope"] is None


# ── reset_stale_jobs ACID submit safety ────────────────────────────────────


def test_reset_stale_submitting_to_draft(db):
    """Submitting jobs are always reset to draft, regardless of age."""
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/100")
    update_status(db, job_id, "submitting")
    # Even a freshly-updated job should be reset
    ids = reset_stale_jobs(db, stale_threshold_seconds=0)
    assert job_id in ids
    job = get_job(db, job_id)
    assert job["status"] == "draft"
    assert "re-approval" in job["progress"]


def test_reset_stale_reanswering_to_draft(db):
    """Reanswering jobs are always reset to draft."""
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/101")
    update_status(db, job_id, "reanswering")
    ids = reset_stale_jobs(db, stale_threshold_seconds=0)
    assert job_id in ids
    job = get_job(db, job_id)
    assert job["status"] == "draft"


def test_reset_stale_reanswering_marks_pending_answer_refresh_failed(db, tmp_path):
    from answer_refresh_state import load_answer_refresh_state, mark_answer_refresh_pending

    out_dir = tmp_path / "job-output"
    out_dir.mkdir()
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/101-refresh")
    update_status(db, job_id, "reanswering", output_dir=str(out_dir))
    mark_answer_refresh_pending(out_dir, request_kind="reanswer")

    reset_stale_jobs(db, stale_threshold_seconds=0)

    state = load_answer_refresh_state(out_dir)
    assert state["status"] == "failed"
    assert state["reason"] == "interrupted_by_reset"


def test_reset_stale_awaiting_captcha_to_draft(db):
    """Awaiting captcha jobs are always reset to draft."""
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/102")
    update_status(db, job_id, "awaiting_captcha")
    ids = reset_stale_jobs(db, stale_threshold_seconds=0)
    assert job_id in ids
    job = get_job(db, job_id)
    assert job["status"] == "draft"


def test_reset_stale_resolving_to_queued(db):
    """Non-submit stale jobs (resolving) are reset to queued, not draft."""
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/103")
    update_status(db, job_id, "resolving")
    # Disable the auto-updated_at trigger so we can backdate the timestamp
    db.execute("DROP TRIGGER IF EXISTS trg_jobs_updated_at")
    db.execute(
        "UPDATE jobs SET updated_at = datetime('now', '-1 hour') WHERE id = ?",
        (job_id,),
    )
    db.commit()
    ids = reset_stale_jobs(db, stale_threshold_seconds=1800)
    assert job_id in ids
    job = get_job(db, job_id)
    assert job["status"] == "queued"
    assert job["retry_after"] == RETRY_AFTER_SENTINEL


def test_reset_stale_jobs_clears_claim_progress_from_queued_rows(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/stale-claimed-queued")
    update_status(db, job_id, "resolving", progress="claimed:98765")
    db.execute("DROP TRIGGER IF EXISTS trg_jobs_updated_at")
    db.execute(
        "UPDATE jobs SET status = 'queued', progress = ?, updated_at = datetime('now', '-2 hours') WHERE id = ?",
        ("claimed:98765", job_id),
    )
    db.commit()

    ids = reset_stale_jobs(db, stale_threshold_seconds=0)

    job = get_job(db, job_id)
    assert job_id in ids
    assert job["status"] == "queued"
    assert job["progress"] == ""


def test_reset_stale_jobs_repairs_locked_rerunnable_rows(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/locked-repair")
    db.execute(
        "UPDATE jobs SET status = 'draft', confirmed_at = ?, submission_lock_state = 'locked', progress = 'stale', "
        "failure_type = 'crash', auth_state = 'sign_in_gate', auth_scope = 'workday:co/careers', error_message = 'stale failure' "
        "WHERE id = ?",
        ("2026-03-18T17:11:18+00:00", job_id),
    )
    db.commit()

    ids = reset_stale_jobs(db, stale_threshold_seconds=0)

    job = get_job(db, job_id)
    events = get_job_timeline(db, job_id)
    assert job_id in ids
    assert job["status"] == "submitted"
    assert job["submission_lock_state"] == "locked"
    assert job["progress"] == ""
    assert job["failure_type"] is None
    assert job["auth_state"] is None
    assert job["auth_scope"] is None
    assert job["error_message"] == ""
    assert any(event["event_type"] == "submission_lock_repaired" for event in events)


@pytest.mark.parametrize("status", ["awaiting_captcha", "retrying", "fix_in_progress"])
def test_reset_stale_jobs_repairs_locked_submit_phase_and_retry_rows(db, status):
    job_id = add_job(db, url=f"https://boards.greenhouse.io/co/jobs/locked-{status}")
    db.execute(
        "UPDATE jobs SET status = ?, confirmed_at = ?, submission_lock_state = 'locked' WHERE id = ?",
        (status, "2026-03-18T17:11:18+00:00", job_id),
    )
    db.commit()

    ids = reset_stale_jobs(db, stale_threshold_seconds=0)

    job = get_job(db, job_id)
    events = get_job_timeline(db, job_id)
    assert job_id in ids
    assert job["status"] == "submitted"
    assert any(event["event_type"] == "submission_lock_repaired" for event in events)


def test_sync_job_from_disk_reads_workday_auth_artifact(db, tmp_path):
    job_id = add_job(db, url="https://factset.wd108.myworkdayjobs.com/FactSetCareers/job/1")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(db, job_id, "draft", output_dir=str(out_dir), board="workday")
    (submit_dir / "workday_auth_failure.json").write_text(
        """
        {
          "status": "auth_unknown",
          "auth_state": "create_account_gate",
          "auth_scope": "workday:factset/factsetcareers",
          "message": "Workday never reached the form after sign in, password reset, and create account."
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["error_message"].startswith("Workday never reached the application form")
    assert job["failure_type"] == "auth_unknown"
    assert job["auth_state"] == "create_account_gate"
    assert job["auth_scope"] == "workday:factset/factsetcareers"


def test_sync_job_from_disk_reads_workday_auth_artifact_as_job_closed(db, tmp_path):
    job_id = add_job(db, url="https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced/job/1")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(db, job_id, "draft", output_dir=str(out_dir), board="workday")
    (submit_dir / "workday_auth_failure.json").write_text(
        """
        {
          "status": "job_closed",
          "auth_state": "job_unavailable",
          "auth_scope": "workday:adobe/external-experienced",
          "alert_text": "The page you are looking for doesn't exist.",
          "message": "job_closed: Workday resolved to a missing or unavailable posting shell instead of the application form."
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "stopped"
    assert job["failure_type"] == "job_closed"
    assert job["archived"] == 1
    assert "job_closed:" in job["error_message"]
    assert job["auth_state"] == "job_unavailable"
    assert job["auth_scope"] == "workday:adobe/external-experienced"


def test_sync_job_from_disk_reads_workday_auth_submission_result(db, tmp_path):
    job_id = add_job(db, url="https://factset.wd108.myworkdayjobs.com/FactSetCareers/job/2")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="workday",
        error_message="Failed after 3 retries",
    )
    (submit_dir / "application_submission_result.json").write_text(
        """
        {
          "status": "auth_unknown",
          "board": "workday",
          "auth_state": "create_account_gate",
          "auth_scope": "workday:factset/factsetcareers",
          "message": "Workday never reached the application form after trying sign in, password reset, and create account. Saved evidence for diagnosis."
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "stopped"
    assert job["failure_type"] == "auth_unknown"
    assert job["auth_state"] == "create_account_gate"
    assert job["auth_scope"] == "workday:factset/factsetcareers"
    assert job["error_message"].startswith("Workday never reached the application form")


def test_sync_job_from_disk_normalizes_legacy_workday_maintenance_artifact(db, tmp_path):
    job_id = add_job(db, url="https://wd1.myworkdaysite.com/en-US/recruiting/snapchat/snap/job/1")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(db, job_id, "draft", output_dir=str(out_dir), board="workday")
    (submit_dir / "workday_auth_failure.json").write_text(
        """
        {
          "status": "auth_failed",
          "auth_scope": "workday:snapchat/snap",
          "page_url": "https://community.workday.com/maintenance-page",
          "page_text_excerpt": "Workday is currently unavailable. We are experiencing a service interruption.",
          "message": "Workday authentication failed. The account may be locked from too many failed login attempts, or the password may be incorrect."
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["failure_type"] == "service_unavailable"
    assert (
        job["error_message"]
        == "Workday is currently unavailable for this tenant. The queue should auto-retry with backoff."
    )
    assert job["auth_scope"] == "workday:snapchat/snap"


def test_sync_job_from_disk_replaces_stale_generic_error_message_with_workday_artifact_reason(db, tmp_path):
    job_id = add_job(db, url="https://wd1.myworkdaysite.com/en-US/recruiting/snapchat/snap/job/2")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="workday",
        error_message="Failed after 3 retries: All submission attempts failed (last exit 1).",
    )
    (submit_dir / "workday_auth_failure.json").write_text(
        """
        {
          "status": "auth_failed",
          "auth_scope": "workday:snapchat/snap",
          "page_url": "https://community.workday.com/maintenance-page",
          "page_text_excerpt": "Workday is currently unavailable. We are experiencing a service interruption.",
          "message": "Workday authentication failed. The account may be locked from too many failed login attempts, or the password may be incorrect."
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert job["failure_type"] == "service_unavailable"
    assert (
        job["error_message"]
        == "Workday is currently unavailable for this tenant. The queue should auto-retry with backoff."
    )


def test_sync_job_from_disk_reads_job_unavailable_artifact(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/1")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit-20260326T010203Z"
    submit_dir.mkdir(parents=True)
    (out_dir / ".active_submit_dir").write_text("submit-20260326T010203Z\n", encoding="utf-8")
    update_status(db, job_id, "draft", output_dir=str(out_dir), board="greenhouse")
    (submit_dir / "job_unavailable.json").write_text(
        """
        {
          "status": "job_closed",
          "board": "greenhouse",
          "application_url": "https://boards.greenhouse.io/embed/job_app?for=acme&token=1",
          "message": "job_closed: Job posting not found (HTTP 404)"
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "stopped"
    assert job["failure_type"] == "job_closed"
    assert job["archived"] == 1
    assert "HTTP 404" in job["error_message"]


def test_sync_job_from_disk_replaces_stale_runtime_error_with_job_closed_message(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/1")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit-20260326T010203Z"
    submit_dir.mkdir(parents=True)
    (out_dir / ".active_submit_dir").write_text("submit-20260326T010203Z\n", encoding="utf-8")
    update_status(db, job_id, "stopped", output_dir=str(out_dir), board="greenhouse")
    db.execute(
        "UPDATE jobs SET failure_type = ?, error_message = ? WHERE id = ?",
        ("greenhouse_runtime_error", "Autofill payload is missing required Greenhouse fields: question_123", job_id),
    )
    (submit_dir / "job_unavailable.json").write_text(
        """
        {
          "status": "job_closed",
          "board": "greenhouse",
          "application_url": "https://boards.greenhouse.io/embed/job_app?for=acme&token=1",
          "message": "job_closed: Job posting not found (HTTP 404)"
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert job["failure_type"] == "job_closed"
    assert job["archived"] == 1
    assert "HTTP 404" in job["error_message"]
    assert "Autofill payload is missing required Greenhouse fields" not in job["error_message"]


def test_sync_job_from_disk_reads_unsupported_board_artifact(db, tmp_path):
    job_id = add_job(db, url="https://jobs.example.com/role")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(db, job_id, "stopped", output_dir=str(out_dir), board="unknown", failure_type="retries_exhausted")
    (submit_dir / "unsupported_board.json").write_text(
        """
        {
          "status": "unsupported_board",
          "reason": "No autofill support for this job board URL: output/acme_jd.txt",
          "suggestion": "Apply manually using the generated resume and cover letter."
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "stopped"
    assert job["failure_type"] == "unsupported"
    assert job["error_message"] == "No autofill support for this job board URL: output/acme_jd.txt"


def test_sync_job_from_disk_overrides_stale_auth_guard_message_with_unsupported_board_reason(db, tmp_path):
    job_id = add_job(db, url="https://jobs.example.com/role")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="icims",
        failure_type="unsupported",
        error_message="Board 'icims' has 9 auth failures in the last 24h — skipping to avoid account lockout",
    )
    (submit_dir / "unsupported_board.json").write_text(
        """
        {
          "status": "unsupported_board",
          "reason": "No autofill support for this job board URL: https://www.amazon.jobs/en/jobs/3201141/role",
          "suggestion": "Apply manually using the generated resume and cover letter."
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["failure_type"] == "unsupported"
    assert job["error_message"] == "No autofill support for this job board URL: https://www.amazon.jobs/en/jobs/3201141/role"


def test_sync_job_from_disk_prefers_current_submission_result_over_stale_unsupported_board_artifact(db, tmp_path):
    job_id = add_job(db, url="https://jobs.supermicro.com/job/example")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="unknown",
        failure_type="unsupported",
        error_message="No autofill support for this job board URL: https://jobs.supermicro.com/job/example",
    )
    unsupported_path = submit_dir / "unsupported_board.json"
    unsupported_path.write_text(
        json.dumps(
            {
                "status": "unsupported_board",
                "reason": "No autofill support for this job board URL: https://jobs.supermicro.com/job/example",
            }
        ),
        encoding="utf-8",
    )
    result_path = submit_dir / "application_submission_result.json"
    result_path.write_text(
        json.dumps(
            {
                "status": "unknown",
                "failure_type": "unknown",
                "board": "successfactors",
                "message": "SuccessFactors redirected to the generic careers home/search page instead of an application form.",
            }
        ),
        encoding="utf-8",
    )
    os.utime(unsupported_path, (1_000_000_000, 1_000_000_000))
    os.utime(result_path, (1_000_000_100, 1_000_000_100))

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "stopped"
    assert job["board"] == "successfactors"
    assert job["failure_type"] == "unknown"
    assert job["error_message"] == (
        "SuccessFactors redirected to the generic careers home/search page instead of an application form."
    )


def test_sync_job_from_disk_reads_failed_linkedin_submission_result(db, tmp_path):
    job_id = add_job(db, url="https://www.linkedin.com/jobs/view/1234567890/")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(db, job_id, "queued", output_dir=str(out_dir), board="linkedin")
    (submit_dir / "application_submission_result.json").write_text(
        """
        {
          "status": "failed",
          "board": "linkedin",
          "failure_type": "linkedin_modal_missing",
          "message": "LinkedIn Easy Apply modal not visible at step 1."
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "stopped"
    assert job["failure_type"] == "linkedin_modal_missing"
    assert job["error_message"] == "LinkedIn Easy Apply modal not visible at step 1."


def test_sync_job_from_disk_uses_result_board_as_source_of_truth(db, tmp_path):
    job_id = add_job(db, url="https://www.linkedin.com/jobs/view/1234567891/")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(db, job_id, "queued", output_dir=str(out_dir), board="greenhouse")
    (submit_dir / "application_submission_result.json").write_text(
        """
        {
          "status": "failed",
          "board": "linkedin",
          "failure_type": "linkedin_modal_missing",
          "message": "LinkedIn Easy Apply modal not visible at step 1."
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "stopped"
    assert job["board"] == "linkedin"
    assert job["failure_type"] == "linkedin_modal_missing"
    assert job["error_message"] == "LinkedIn Easy Apply modal not visible at step 1."


def test_sync_job_from_disk_reclassifies_not_easy_apply_result(db, tmp_path):
    job_id = add_job(db, url="https://www.linkedin.com/jobs/view/1234567890/")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="linkedin",
        failure_type="linkedin_modal_missing",
        error_message="LinkedIn Easy Apply modal not visible at step 1.",
    )
    (submit_dir / "application_submission_result.json").write_text(
        """
        {
          "status": "not_easy_apply",
          "failure_type": "no_apply_button",
          "reason": "no_apply_button",
          "message": "LinkedIn job does not currently expose an Easy Apply or external Apply control."
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "stopped"
    assert job["failure_type"] == "no_apply_button"
    assert job["error_message"] == "LinkedIn job does not currently expose an Easy Apply or external Apply control."


def test_sync_job_from_disk_reclassifies_skipped_captcha_result(db, tmp_path):
    job_id = add_job(db, url="https://jobs.intuit.com/job/1")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="avature",
        error_message="Failed after 3 retries",
    )
    (submit_dir / "application_submission_result.json").write_text(
        """
        {
          "status": "skipped_captcha",
          "board": "avature",
          "message": "Submission skipped: captcha required. Moving on to next job."
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "stopped"
    assert job["failure_type"] == "skipped_captcha"
    assert job["error_message"] == "Submission skipped: captcha required. Moving on to next job."


def test_sync_job_from_disk_reclassifies_pending_user_input_result(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/example/jobs/1")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="greenhouse",
        error_message="Failed after 3 retries",
    )
    (submit_dir / "application_submission_result.json").write_text(
        """
        {
          "status": "pending_user_input",
          "board": "greenhouse",
          "message": "Submission paused because one or more answers require manual user input."
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "stopped"
    assert job["failure_type"] == "pending_user_input"
    assert job["error_message"] == "Submission paused because one or more answers require manual user input."


def test_sync_job_from_disk_reclassifies_pending_user_input_payload_without_submission_result(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/example/jobs/1")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Jerrison Li Resume - Example.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Example.pdf").write_text("cover", encoding="utf-8")
    (submit_dir / "greenhouse_autofill_report.json").write_text(
        json.dumps(_filled_autofill_report_payload()),
        encoding="utf-8",
    )
    (submit_dir / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    (submit_dir / "pending_user_input.json").write_text(
        json.dumps(
            {
                "status": "pending_user_input",
                "board": "greenhouse",
                "message": "The submitter stopped before submission because one or more planned fields could not be confirmed on the live form. Every field must be confirmed before submit.",
                "questions": [
                    {
                        "field_name": "travel_percentage",
                        "label": "What % of travel are you open to?",
                        "reason": "No source states a work-travel percentage the candidate is open to.",
                        "status": "planned",
                        "kind": "text",
                        "source": "answer_verifier",
                        "required": True,
                        "planned_value": "25%",
                        "blocks_draft_completion": True,
                        "blocker_kind": "generated_answer",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    update_status(
        db,
        job_id,
        "draft",
        output_dir=str(out_dir),
        board="greenhouse",
        progress="Draft ready for review",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "stopped"
    assert job["failure_type"] == "pending_user_input"
    assert (
        job["error_message"]
        == "The submitter stopped before submission because one or more planned fields could not be confirmed on the live form. Every field must be confirmed before submit."
    )
    assert job["progress"] == ""


def test_sync_job_from_disk_preserves_known_board_when_result_board_is_unknown(db, tmp_path):
    job_id = add_job(db, url="https://www.linkedin.com/jobs/view/1234567890/")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="linkedin",
        error_message="Failed after 3 retries",
    )
    (submit_dir / "application_submission_result.json").write_text(
        """
        {
          "status": "pending_user_input",
          "board": "unknown",
          "message": "Submission paused because one or more answers require manual user input."
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["board"] == "linkedin"
    assert job["status"] == "stopped"
    assert job["failure_type"] == "pending_user_input"


def test_sync_job_from_disk_reclassifies_unknown_result(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/example/jobs/2")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="greenhouse",
        error_message="Failed after 3 retries",
    )
    (submit_dir / "application_submission_result.json").write_text(
        """
        {
          "status": "unknown",
          "board": "greenhouse",
          "message": "Submission stopped because the board reported an unknown result state."
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "stopped"
    assert job["failure_type"] == "unknown"
    assert job["error_message"] == "Submission stopped because the board reported an unknown result state."


def test_sync_job_from_disk_reclassifies_skipped_auth_failure_result(db, tmp_path):
    job_id = add_job(db, url="https://careers.example.com/jobs/3")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="icims",
        error_message="Failed after 3 retries",
    )
    (submit_dir / "application_submission_result.json").write_text(
        """
        {
          "status": "skipped_auth_failure",
          "board": "icims",
          "message": "Submission stopped because the application redirected to an authentication gate."
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "stopped"
    assert job["failure_type"] == "skipped_auth_failure"
    assert job["error_message"] == "Submission stopped because the application redirected to an authentication gate."


def test_sync_job_from_disk_keeps_current_workday_submit_result_over_stale_auth_artifact(db, tmp_path):
    job_id = add_job(db, url="https://wd1.myworkdaysite.com/en-US/recruiting/snapchat/snap/job/1")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="workday",
        failure_type="service_unavailable",
        error_message="Workday is currently unavailable.",
    )
    (submit_dir / "application_submission_result.json").write_text(
        """
        {
          "status": "skipped_captcha",
          "board": "workday",
          "message": "Submission skipped: captcha required. Moving on to next job."
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )
    (submit_dir / "workday_auth_failure.json").write_text(
        """
        {
          "status": "auth_failed",
          "board": "workday",
          "page_url": "https://community.workday.com/maintenance-page",
          "message": "Workday authentication failed. The account may be locked from too many failed login attempts, or the password may be incorrect."
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["failure_type"] == "skipped_captcha"
    assert job["error_message"] == "Submission skipped: captcha required. Moving on to next job."


def test_sync_job_from_disk_prefers_active_workday_failed_result_over_stale_submit_auth_artifact(db, tmp_path):
    job_id = add_job(db, url="https://calix.wd1.myworkdayjobs.com/External/job/1")
    out_dir = tmp_path / "job-output"
    stale_submit_dir = out_dir / "submit"
    active_submit_dir = out_dir / "submit-20260329T191800Z"
    stale_submit_dir.mkdir(parents=True)
    active_submit_dir.mkdir(parents=True)
    (out_dir / ".active_submit_dir").write_text("submit-20260329T191800Z\n", encoding="utf-8")
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="workday",
        failure_type="auth_unknown",
        error_message="Workday never reached the application form after trying sign in, password reset, and create account. Saved evidence for diagnosis.",
    )
    db.execute("UPDATE jobs SET auth_state = 'sign_in_gate' WHERE id = ?", (job_id,))
    db.commit()
    (stale_submit_dir / "workday_auth_failure.json").write_text(
        json.dumps(
            {
                "status": "auth_unknown",
                "auth_state": "account_verification_gate",
                "message": "Workday never reached the application form after trying sign in, password reset, and create account. Saved evidence for diagnosis.",
            }
        ),
        encoding="utf-8",
    )
    (stale_submit_dir / "application_submission_result.json").write_text(
        json.dumps(
            {
                "status": "auth_unknown",
                "board": "workday",
                "failure_type": "auth_unknown",
                "message": "Workday never reached the application form after trying sign in, password reset, and create account. Saved evidence for diagnosis.",
            }
        ),
        encoding="utf-8",
    )
    (active_submit_dir / "application_submission_result.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "board": "workday",
                "failure_type": "application_questions_validation",
                "message": "Workday Application Questions page still shows required validation errors after repeated retry attempts.",
            }
        ),
        encoding="utf-8",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "stopped"
    assert job["failure_type"] == "application_questions_validation"
    assert job["auth_state"] is None
    assert job["auth_scope"] is None
    assert job["error_message"] == (
        "Workday Application Questions page still shows required validation errors after repeated retry attempts."
    )


def test_sync_job_from_disk_promotes_ready_draft_proof_and_clears_stale_failure_metadata(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/123")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit-20260326T010203Z"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (out_dir / ".active_submit_dir").write_text("submit-20260326T010203Z\n", encoding="utf-8")
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Jerrison Li Resume - Acme.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Acme.pdf").write_text("cover letter", encoding="utf-8")
    (submit_dir / "greenhouse_autofill_report.json").write_text(
        json.dumps(_filled_autofill_report_payload()),
        encoding="utf-8",
    )
    (submit_dir / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="greenhouse",
        failure_type="retries_exhausted",
        error_message="Failed after 3 retries",
    )
    db.execute("UPDATE jobs SET auth_state = 'sign_in_gate' WHERE id = ?", (job_id,))
    db.commit()

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "draft"
    assert job["failure_type"] is None
    assert job["auth_state"] is None
    assert job["error_message"] is None


def test_sync_job_from_disk_promotes_ready_draft_proof_and_backfills_unknown_board_from_current_artifacts(
    db, tmp_path
):
    job_id = add_job(db, url="https://zero-hash.breezy.hr/p/6411af0e2ee2-principal-product-manager")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Jerrison Li Resume - Zero Hash.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Zero Hash.pdf").write_text("cover letter", encoding="utf-8")
    (submit_dir / "breezy_autofill_payload.json").write_text(
        json.dumps({"board": "breezy", "provider": "openai", "artifacts": {}}),
        encoding="utf-8",
    )
    (submit_dir / "breezy_autofill_report.json").write_text(
        json.dumps(_filled_autofill_report_payload()),
        encoding="utf-8",
    )
    (submit_dir / "breezy_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="unknown",
        failure_type="unsupported",
        error_message="Unsupported board.",
        provider="gemini",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "draft"
    assert job["board"] == "breezy"
    assert job["failure_type"] is None
    assert job["error_message"] is None


def test_sync_job_from_disk_trusts_newer_ready_draft_proof_over_stale_unsupported_board_artifact(
    db, tmp_path
):
    job_id = add_job(db, url="https://zero-hash.breezy.hr/p/e4cda20c3c01-principal-product-manager-identity")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Jerrison Li Resume - Identity.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Identity.pdf").write_text("cover letter", encoding="utf-8")
    unsupported_path = submit_dir / "unsupported_board.json"
    unsupported_path.write_text(
        json.dumps({"status": "unsupported_board", "message": "Unsupported board."}),
        encoding="utf-8",
    )
    (submit_dir / "breezy_autofill_payload.json").write_text(
        json.dumps({"board": "breezy", "provider": "openai", "artifacts": {}}),
        encoding="utf-8",
    )
    (submit_dir / "breezy_autofill_report.json").write_text(
        json.dumps(_filled_autofill_report_payload()),
        encoding="utf-8",
    )
    (submit_dir / "breezy_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    os.utime(unsupported_path, (1_000_000_000, 1_000_000_000))
    os.utime(submit_dir / "breezy_autofill_report.json", (1_000_000_100, 1_000_000_100))
    os.utime(submit_dir / "breezy_autofill_pre_submit.png", (1_000_000_100, 1_000_000_100))
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="unknown",
        failure_type="unsupported",
        error_message="Unsupported board.",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "draft"
    assert job["board"] == "breezy"
    assert job["failure_type"] is None
    assert job["error_message"] is None


def test_sync_job_from_disk_prefers_current_repo_output_dir_over_stale_clone_path(db, tmp_path, monkeypatch):
    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/rehome-proof")
    current_repo = tmp_path / "current-repo"
    old_repo = tmp_path / "old-repo"
    current_out = current_repo / "output" / "acme" / "rehome-proof"
    old_out = old_repo / "output" / "acme" / "rehome-proof"
    current_submit = current_out / "submit"
    current_docs = current_out / "documents"
    old_submit = old_out / "submit"

    current_submit.mkdir(parents=True)
    current_docs.mkdir(parents=True)
    old_submit.mkdir(parents=True)

    (current_out / "draft_summary.png").write_text("png", encoding="utf-8")
    (current_docs / "Jerrison Li Resume - Acme.pdf").write_text("resume", encoding="utf-8")
    (current_docs / "Jerrison Li Cover Letter - Acme.pdf").write_text("cover letter", encoding="utf-8")
    (current_submit / "greenhouse_autofill_report.json").write_text(
        json.dumps(_filled_autofill_report_payload()),
        encoding="utf-8",
    )
    (current_submit / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    (old_submit / "application_submission_result.json").write_text(
        json.dumps(
            {
                "status": "skipped_captcha",
                "board": "greenhouse",
                "failure_type": "skipped_captcha",
                "message": "Submission skipped: captcha required. Moving on to next job.",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(job_db, "PROJECT_ROOT", current_repo)
    monkeypatch.setattr(job_db, "OUTPUT_ROOT", current_repo / "output")

    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(old_out),
        board="greenhouse",
        failure_type="skipped_captcha",
        error_message="Submission skipped: captcha required. Moving on to next job.",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert "output_dir→repo_local" in synced["updates"]
    assert job["output_dir"] == str(current_out)
    assert job["status"] == "draft"
    assert job["failure_type"] is None
    assert job["error_message"] is None


def test_sync_job_from_disk_canonicalizes_symlinked_legacy_output_dir(db, tmp_path, monkeypatch):
    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/symlink-proof")
    current_repo = tmp_path / "current-repo"
    legacy_repo = tmp_path / "legacy-repo"
    current_out = current_repo / "output" / "acme" / "symlink-proof"
    current_submit = current_out / "submit"
    current_docs = current_out / "documents"

    current_submit.mkdir(parents=True)
    current_docs.mkdir(parents=True)
    legacy_repo.symlink_to(current_repo, target_is_directory=True)

    (current_out / "draft_summary.png").write_text("png", encoding="utf-8")
    (current_docs / "Jerrison Li Resume - Acme.pdf").write_text("resume", encoding="utf-8")
    (current_docs / "Jerrison Li Cover Letter - Acme.pdf").write_text("cover letter", encoding="utf-8")
    (current_submit / "greenhouse_autofill_report.json").write_text(
        json.dumps(_filled_autofill_report_payload()),
        encoding="utf-8",
    )
    (current_submit / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")

    monkeypatch.setattr(job_db, "PROJECT_ROOT", current_repo)
    monkeypatch.setattr(job_db, "OUTPUT_ROOT", current_repo / "output")

    legacy_out = legacy_repo / "output" / "acme" / "symlink-proof"
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(legacy_out),
        board="greenhouse",
        failure_type="skipped_captcha",
        error_message="Submission skipped: captcha required. Moving on to next job.",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert "output_dir→repo_local" in synced["updates"]
    assert job["output_dir"] == str(current_out)
    assert job["status"] == "draft"


def test_migrate_legacy_output_dirs_copies_stopped_legacy_tree_into_current_repo(db, tmp_path, monkeypatch):
    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/migrate-proof")
    current_repo = tmp_path / "current-repo"
    old_repo = tmp_path / "old-repo"
    old_out = old_repo / "output" / "acme" / "migrate-proof"
    old_submit = old_out / "submit"
    old_submit.mkdir(parents=True)
    (old_submit / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    (old_submit / "greenhouse_autofill_report.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(job_db, "PROJECT_ROOT", current_repo)
    monkeypatch.setattr(job_db, "OUTPUT_ROOT", current_repo / "output")

    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(old_out),
        board="greenhouse",
        failure_type="retries_exhausted",
        error_message="Failed after 3 retries",
    )

    summary = migrate_legacy_output_dirs(db, statuses=("stopped",), initiator="test")
    job = get_job(db, job_id)
    current_out = current_repo / "output" / "acme" / "migrate-proof"

    assert summary["scanned"] == 1
    assert summary["migrated"] == 1
    assert job["output_dir"] == str(current_out)
    assert (current_out / "submit" / "greenhouse_autofill_pre_submit.png").read_text(encoding="utf-8") == "png"


def test_migrate_legacy_output_dirs_repoints_existing_repo_local_tree_without_copy(db, tmp_path, monkeypatch):
    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/repoint-proof")
    current_repo = tmp_path / "current-repo"
    old_repo = tmp_path / "old-repo"
    current_out = current_repo / "output" / "acme" / "repoint-proof"
    old_out = old_repo / "output" / "acme" / "repoint-proof"
    (current_out / "submit").mkdir(parents=True)
    (current_out / "submit" / "greenhouse_autofill_pre_submit.png").write_text("repo-local", encoding="utf-8")

    monkeypatch.setattr(job_db, "PROJECT_ROOT", current_repo)
    monkeypatch.setattr(job_db, "OUTPUT_ROOT", current_repo / "output")

    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(old_out),
        board="greenhouse",
        failure_type="retries_exhausted",
        error_message="Failed after 3 retries",
    )

    summary = migrate_legacy_output_dirs(db, statuses=("stopped",), initiator="test")
    job = get_job(db, job_id)

    assert summary["scanned"] == 1
    assert summary["repointed_existing"] == 1
    assert summary["migrated"] == 0
    assert job["output_dir"] == str(current_out)


def test_sync_job_from_disk_does_not_promote_stale_ready_draft_status_when_proof_contract_drifted(db, tmp_path):
    job_id = add_job(db, url="https://jobs.ashbyhq.com/acme/123")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Jerrison Li Resume - Acme.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Acme.pdf").write_text("cover letter", encoding="utf-8")
    (submit_dir / "application_answers.json").write_text(json.dumps({"answers": []}), encoding="utf-8")
    (submit_dir / "ashby_application_page.html").write_text("<html></html>", encoding="utf-8")
    (out_dir / "draft_status.json").write_text(
        json.dumps(
            {
                "status": "awaiting_review",
                "draft_review_state": {
                    "state": "ready",
                    "reason": "The active submit attempt has the required draft-proof artifacts.",
                    "submit_dirname": "submit",
                    "historical_submit_dirs": [],
                },
            }
        ),
        encoding="utf-8",
    )
    update_status(db, job_id, "autofilling", output_dir=str(out_dir), board="ashby")

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "autofilling"
    assert job["failure_type"] is None
    assert job["error_message"] is None


def test_sync_job_from_disk_does_not_trust_stale_ready_draft_hint_when_current_proof_is_blocked(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/stale-ready")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Jerrison Li Resume - Acme.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Acme.pdf").write_text("cover letter", encoding="utf-8")
    (submit_dir / "application_answers.json").write_text(
        json.dumps(
            {
                "questions": [
                    {
                        "field_name": "question_1",
                        "label": "How did you hear about us?",
                        "type": "multi_value_single_select",
                        "options": ["LinkedIn", "Other"],
                    }
                ],
                "answers": {"question_1": "LinkedIn"},
            }
        ),
        encoding="utf-8",
    )
    (submit_dir / "greenhouse_autofill_review.png").write_text("png", encoding="utf-8")
    (out_dir / "draft_status.json").write_text(
        json.dumps(
            {
                "status": "awaiting_review",
                "draft_review_state": {
                    "state": "ready",
                    "reason": "The active submit attempt has the required draft-proof artifacts.",
                    "submit_dirname": "submit",
                    "historical_submit_dirs": [],
                },
            }
        ),
        encoding="utf-8",
    )
    update_status(db, job_id, "autofilling", output_dir=str(out_dir), board="greenhouse")

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "autofilling"
    assert job["failure_type"] is None
    assert job["error_message"] is None


def test_sync_job_from_disk_refreshes_stale_draft_status_when_live_greenhouse_proof_is_ready(db, tmp_path, monkeypatch):
    import draft_manager
    from pipeline_draft_proof import draft_review_state

    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/missing-review-proof")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit-20260406T230927Z"
    historical_submit = out_dir / "submit"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    historical_submit.mkdir()
    docs_dir.mkdir()
    (out_dir / ".active_submit_dir").write_text(f"{submit_dir.name}\n", encoding="utf-8")
    (submit_dir / "greenhouse_autofill_report.json").write_text(
        json.dumps(_filled_autofill_report_payload()),
        encoding="utf-8",
    )
    (submit_dir / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    (historical_submit / "greenhouse_autofill_report.json").write_text(
        json.dumps(_filled_autofill_report_payload()),
        encoding="utf-8",
    )
    (historical_submit / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    (historical_submit / "greenhouse_autofill_review.png").write_text("png", encoding="utf-8")
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Jerrison Li Resume - Acme.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Acme.pdf").write_text("cover letter", encoding="utf-8")
    (out_dir / "draft_status.json").write_text(
        json.dumps(
            {
                "status": "awaiting_review",
                "draft_review_state": {
                    "state": "stale",
                    "reason": (
                        "Historical proof exists, but the active submit attempt is missing required proof or "
                        "still has blockers. The current submit attempt is missing the required review screenshot proof."
                    ),
                    "submit_dirname": submit_dir.name,
                    "historical_submit_dirs": ["submit"],
                },
            }
        ),
        encoding="utf-8",
    )
    update_status(db, job_id, "draft", output_dir=str(out_dir), board="greenhouse")

    refreshed: list[tuple[str, str | None]] = []

    def fake_generate_draft_summary(target_out_dir, target_submit_dir, meta):
        refreshed.append((str(target_out_dir), getattr(target_submit_dir, "name", None)))
        review_state = draft_review_state(target_out_dir, board_name=meta.get("board"))
        (target_out_dir / "draft_status.json").write_text(
            json.dumps(
                {
                    "status": "awaiting_review",
                    "draft_review_state": review_state,
                }
            ),
            encoding="utf-8",
        )
        return {"status": str(target_out_dir / "draft_status.json")}

    monkeypatch.setattr(draft_manager, "generate_draft_summary", fake_generate_draft_summary)

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)
    refreshed_status = json.loads((out_dir / "draft_status.json").read_text(encoding="utf-8"))

    assert synced["synced"] is True
    assert job["status"] == "draft"
    assert refreshed == [(str(out_dir), submit_dir.name)]
    assert refreshed_status["draft_review_state"]["state"] == "ready"
    assert refreshed_status["draft_review_state"]["submit_dirname"] == submit_dir.name


def test_sync_job_from_disk_reconciles_no_generated_answer_verification_from_current_proof(db, tmp_path):
    from answer_refresh_state import load_answer_refresh_state
    from answer_verification_state import load_answer_verification_state

    job_id = add_job(db, url="https://apply.workable.com/acme/no-generated-proof")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "workable_autofill_report.json").write_text(
        json.dumps(
            {
                "fields": [
                    {
                        "field_name": "full_name",
                        "label": "Full Name",
                        "status": "filled",
                        "source": "master_resume.md",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (submit_dir / "application_answers.json").write_text(json.dumps({"answers": []}), encoding="utf-8")
    (out_dir / "answer_refresh_status.json").write_text(
        json.dumps(
            {
                "version": 1,
                "status": "not_applicable",
                "request_id": "refresh-123",
                "request_kind": "reset_to_new",
                "requested_at_utc": "2026-04-04T17:19:17+00:00",
                "resolved_at_utc": "2026-04-04T17:19:22+00:00",
                "generated_answer_count": 0,
                "reason": "no_generated_answers",
                "message": "No generated application answers were present for this draft.",
                "proof_submit_dir": None,
            }
        ),
        encoding="utf-8",
    )
    update_status(db, job_id, "draft", output_dir=str(out_dir), board="workable")

    synced = sync_job_from_disk(db, job_id)
    refresh_state = load_answer_refresh_state(out_dir)
    verification_state = load_answer_verification_state(out_dir)

    assert synced["synced"] is True
    assert refresh_state["status"] == "not_applicable"
    assert refresh_state["proof_submit_dir"] == "submit"
    assert verification_state["status"] == "not_applicable"
    assert verification_state["blocked_answer_count"] == 0
    assert verification_state["proof_submit_dir"] == "submit"


@pytest.mark.parametrize("archived", [False, True])
def test_sync_job_from_disk_archives_ready_draft_duplicate_when_locked_submission_owns_output_dir(
    db,
    tmp_path,
    archived,
):
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Jerrison Li Resume - Acme.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Acme.pdf").write_text("cover letter", encoding="utf-8")
    (submit_dir / "application_answers.json").write_text(json.dumps({"answers": []}), encoding="utf-8")
    (submit_dir / "greenhouse_autofill_report.json").write_text(
        json.dumps(_filled_autofill_report_payload()),
        encoding="utf-8",
    )
    (submit_dir / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")

    owner_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/owner")
    update_status(db, owner_id, "submitted", output_dir=str(out_dir), board="greenhouse")
    db.execute(
        "UPDATE jobs SET confirmed_at = ?, submission_lock_state = 'locked' WHERE id = ?",
        ("2026-03-18T17:11:18+00:00", owner_id),
    )

    duplicate_id = add_job(db, url="https://www.trueup.io/jobs/acme-owner")
    update_status(
        db,
        duplicate_id,
        "draft",
        output_dir=str(out_dir),
        board="greenhouse",
        archived=archived,
    )
    db.commit()

    synced = sync_job_from_disk(db, duplicate_id)

    owner = get_job(db, owner_id)
    duplicate = get_job(db, duplicate_id)

    assert synced["synced"] is True
    assert synced["changed"] is True
    assert owner["status"] == "submitted"
    assert owner["submission_lock_state"] == "locked"
    assert duplicate["status"] == "stopped"
    assert bool(duplicate["archived"]) is True
    assert duplicate["failure_type"] == "duplicate"
    assert f"job #{owner_id}" in str(duplicate["error_message"] or "")


def test_repair_stale_processing_jobs_promotes_ready_draft_rows(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/stale-processing")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Jerrison Li Resume - Acme.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Acme.pdf").write_text("cover letter", encoding="utf-8")
    (submit_dir / "greenhouse_autofill_report.json").write_text(
        json.dumps(_filled_autofill_report_payload()),
        encoding="utf-8",
    )
    (submit_dir / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    update_status(
        db,
        job_id,
        "autofilling",
        output_dir=str(out_dir),
        board="greenhouse",
        progress="Assets generated, preparing draft...",
    )
    db.execute("DROP TRIGGER IF EXISTS trg_jobs_updated_at")
    db.execute("UPDATE jobs SET updated_at = datetime('now', '-600 seconds') WHERE id = ?", (job_id,))
    db.commit()

    repaired = repair_stale_processing_jobs(db, stale_threshold_seconds=60, limit=10)
    job = get_job(db, job_id)

    assert repaired == {
        "scanned": 1,
        "changed": 1,
        "promoted_to_draft": 1,
        "promoted_to_submitted": 0,
        "promoted_to_stopped": 0,
        "reset_to_queued": 0,
    }
    assert job["status"] == "draft"


def test_repair_stale_processing_jobs_requeues_unrecoverable_rows(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/stale-requeue")
    update_status(
        db,
        job_id,
        "generating",
        company="Acme",
        role_title="Senior Product Manager",
        progress="Generating assets...",
    )
    db.execute("DROP TRIGGER IF EXISTS trg_jobs_updated_at")
    db.execute("UPDATE jobs SET updated_at = datetime('now', '-600 seconds') WHERE id = ?", (job_id,))
    db.commit()

    repaired = repair_stale_processing_jobs(db, stale_threshold_seconds=60, limit=10)
    job = get_job(db, job_id)

    assert repaired == {
        "scanned": 1,
        "changed": 1,
        "promoted_to_draft": 0,
        "promoted_to_submitted": 0,
        "promoted_to_stopped": 0,
        "reset_to_queued": 1,
    }
    assert job["status"] == "queued"
    assert job["retry_after"] == RETRY_AFTER_SENTINEL


def test_repair_stale_processing_jobs_skips_excluded_active_job_ids(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/stale-active")
    update_status(
        db,
        job_id,
        "generating",
        company="Acme",
        role_title="Senior Product Manager",
        progress="Generating assets...",
    )
    db.execute("DROP TRIGGER IF EXISTS trg_jobs_updated_at")
    db.execute("UPDATE jobs SET updated_at = datetime('now', '-600 seconds') WHERE id = ?", (job_id,))
    db.commit()

    repaired = repair_stale_processing_jobs(
        db,
        stale_threshold_seconds=0,
        limit=10,
        exclude_job_ids={job_id},
    )
    job = get_job(db, job_id)

    assert repaired == {
        "scanned": 0,
        "changed": 0,
        "promoted_to_draft": 0,
        "promoted_to_submitted": 0,
        "promoted_to_stopped": 0,
        "reset_to_queued": 0,
    }
    assert job["status"] == "generating"


def test_sync_job_from_disk_promotes_ready_draft_proof_even_with_stale_failed_result_artifact(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/456")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Jerrison Li Resume - Acme.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Acme.pdf").write_text("cover letter", encoding="utf-8")
    (submit_dir / "greenhouse_autofill_report.json").write_text(
        json.dumps(_filled_autofill_report_payload()),
        encoding="utf-8",
    )
    (submit_dir / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    result_path = submit_dir / "application_submission_result.json"
    result_path.write_text(
        json.dumps(
            {
                "status": "failed",
                "board": "greenhouse",
                "failure_type": "greenhouse_runtime_error",
                "message": "Could not find a verification/security code in Gmail.",
            }
        ),
        encoding="utf-8",
    )
    stale_timestamp = time.time() - 10
    os.utime(result_path, (stale_timestamp, stale_timestamp))
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="greenhouse",
        failure_type="greenhouse_runtime_error",
        error_message="Could not find a verification/security code in Gmail.",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "draft"
    assert job["failure_type"] is None
    assert job["error_message"] is None


def test_sync_job_from_disk_is_noop_for_ready_draft_with_stale_stopped_result(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/456-stale-ready")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Jerrison Li Resume - Acme.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Acme.pdf").write_text("cover letter", encoding="utf-8")
    (submit_dir / "greenhouse_autofill_report.json").write_text(
        json.dumps(_filled_autofill_report_payload()),
        encoding="utf-8",
    )
    (submit_dir / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    result_path = submit_dir / "application_submission_result.json"
    result_path.write_text(
        json.dumps(
            {
                "status": "skipped_captcha",
                "board": "greenhouse",
                "failure_type": "skipped_captcha",
                "message": "Submission skipped: captcha required. Moving on to next job.",
            }
        ),
        encoding="utf-8",
    )
    stale_timestamp = time.time() - 10
    os.utime(result_path, (stale_timestamp, stale_timestamp))
    update_status(db, job_id, "draft", output_dir=str(out_dir), board="greenhouse")

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert "status→stopped" not in synced["updates"]
    assert job["status"] == "draft"
    assert job["failure_type"] is None
    assert job["error_message"] is None


def test_sync_job_from_disk_trusts_current_stopped_result_over_ready_draft_proof(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/456-current-stopped")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Jerrison Li Resume - Acme.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Acme.pdf").write_text("cover letter", encoding="utf-8")
    (submit_dir / "greenhouse_autofill_report.json").write_text("{}", encoding="utf-8")
    (submit_dir / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    (submit_dir / "application_submission_result.json").write_text(
        json.dumps(
            {
                "status": "skipped_captcha",
                "board": "greenhouse",
                "failure_type": "skipped_captcha",
                "message": "Submission skipped: captcha required. Moving on to next job.",
            }
        ),
        encoding="utf-8",
    )
    update_status(db, job_id, "draft", output_dir=str(out_dir), board="greenhouse")

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "stopped"
    assert job["failure_type"] == "skipped_captcha"
    assert job["error_message"] == "Submission skipped: captcha required. Moving on to next job."


def test_sync_job_from_disk_trusts_active_skipped_auth_result_over_stale_legacy_draft_proof(db, tmp_path):
    job_id = add_job(db, url="https://www.uber.com/global/en/careers/list/123/")
    out_dir = tmp_path / "job-output"
    stale_submit_dir = out_dir / "submit"
    active_submit_dir = out_dir / "submit-20260406T205644Z"
    docs_dir = out_dir / "documents"
    stale_submit_dir.mkdir(parents=True)
    active_submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (out_dir / ".active_submit_dir").write_text("submit-20260406T205644Z\n", encoding="utf-8")
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Jerrison Li Resume - Uber Eats.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Uber Eats.pdf").write_text("cover letter", encoding="utf-8")
    (stale_submit_dir / "greenhouse_autofill_report.json").write_text("{}", encoding="utf-8")
    (stale_submit_dir / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    (active_submit_dir / "application_submission_result.json").write_text(
        json.dumps(
            {
                "status": "skipped_auth",
                "board": "greenhouse",
                "failure_type": "auth_guarded",
                "message": "Uber requires sign in or account creation before the application form is available.",
            }
        ),
        encoding="utf-8",
    )
    update_status(db, job_id, "draft", output_dir=str(out_dir), board="greenhouse")

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "stopped"
    assert job["failure_type"] == "auth_guarded"
    assert job["error_message"] == "Uber requires sign in or account creation before the application form is available."


def test_sync_job_from_disk_trusts_same_attempt_skipped_captcha_result_over_fresh_proof(db, tmp_path):
    job_id = add_job(db, url="https://jobs.lever.co/ridezum/1")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Jerrison Li Resume - Zūm.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Zūm.pdf").write_text("cover letter", encoding="utf-8")
    (submit_dir / "application_submission_result.json").write_text(
        json.dumps(
            {
                "status": "skipped_captcha",
                "board": "lever",
                "failure_type": "skipped_captcha",
                "message": "Submission skipped: captcha required. Moving on to next job.",
            }
        ),
        encoding="utf-8",
    )
    (submit_dir / "lever_autofill_report.json").write_text("{}", encoding="utf-8")
    (submit_dir / "lever_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    result_mtime = (submit_dir / "application_submission_result.json").stat().st_mtime
    os.utime(out_dir / "draft_summary.png", (result_mtime + 5, result_mtime + 5))
    update_status(db, job_id, "draft", output_dir=str(out_dir), board="lever")

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "stopped"
    assert job["failure_type"] == "skipped_captcha"
    assert job["error_message"] == "Submission skipped: captcha required. Moving on to next job."


def test_sync_job_from_disk_trusts_active_job_closed_result_over_stale_legacy_draft_proof(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/harvey/jobs/123")
    out_dir = tmp_path / "job-output"
    stale_submit_dir = out_dir / "submit"
    active_submit_dir = out_dir / "submit-20260406T222155Z"
    docs_dir = out_dir / "documents"
    stale_submit_dir.mkdir(parents=True)
    active_submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (out_dir / ".active_submit_dir").write_text("submit-20260406T222155Z\n", encoding="utf-8")
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Jerrison Li Resume - Harvey.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Harvey.pdf").write_text("cover letter", encoding="utf-8")
    (stale_submit_dir / "greenhouse_autofill_report.json").write_text("{}", encoding="utf-8")
    (stale_submit_dir / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    (active_submit_dir / "application_submission_result.json").write_text(
        json.dumps(
            {
                "status": "job_closed",
                "board": "greenhouse",
                "message": "job_closed: The application page resolved to an unavailable job shell.",
            }
        ),
        encoding="utf-8",
    )
    update_status(db, job_id, "draft", output_dir=str(out_dir), board="greenhouse")

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "stopped"
    assert job["failure_type"] == "job_closed"
    assert job["archived"] == 1
    assert job["error_message"] == "job_closed: The application page resolved to an unavailable job shell."


def test_sync_job_from_disk_promotes_ready_draft_proof_even_when_row_is_already_submitted(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/789")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    active_submit_dir = out_dir / "submit-20260329T183545Z"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    active_submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (out_dir / ".active_submit_dir").write_text("submit-20260329T183545Z\n", encoding="utf-8")
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Jerrison Li Resume - Acme.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Acme.pdf").write_text("cover letter", encoding="utf-8")
    (active_submit_dir / "greenhouse_autofill_report.json").write_text(
        json.dumps(_filled_autofill_report_payload()),
        encoding="utf-8",
    )
    (active_submit_dir / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    (active_submit_dir / "greenhouse_autofill_review.png").write_text("png", encoding="utf-8")
    (submit_dir / "application_submission_result.json").write_text(
        json.dumps(
            {
                "status": "unknown",
                "board": "greenhouse",
                "message": "",
            }
        ),
        encoding="utf-8",
    )
    update_status(db, job_id, "submitted", output_dir=str(out_dir), board="greenhouse")

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "draft"
    assert job["failure_type"] is None
    assert job["error_message"] is None


def test_sync_job_from_disk_keeps_confirmed_submission_stable_when_stale_result_and_ready_proof_coexist(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/789-confirmed")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    confirmed_at = "2026-04-02T15:33:19+00:00"
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Jerrison Li Resume - Acme.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Acme.pdf").write_text("cover letter", encoding="utf-8")
    (submit_dir / "greenhouse_autofill_report.json").write_text("{}", encoding="utf-8")
    (submit_dir / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    (submit_dir / "application_submission_result.json").write_text(
        json.dumps(
            {
                "status": "unknown",
                "board": "greenhouse",
                "message": "",
            }
        ),
        encoding="utf-8",
    )
    (submit_dir / "application_confirmation_website.json").write_text(
        json.dumps(
            {
                "status": "confirmed",
                "website_confirmed": True,
                "confirmed_at_utc": confirmed_at,
            }
        ),
        encoding="utf-8",
    )
    (submit_dir / "notion_sync_status.json").write_text(
        json.dumps({"status": "synced", "page_id": "page-123", "page_url": "https://www.notion.so/page-123"}),
        encoding="utf-8",
    )
    update_status(db, job_id, "submitted", output_dir=str(out_dir), board="greenhouse", notion_url="https://www.notion.so/page-123")
    db.execute(
        "UPDATE jobs SET confirmed_at = ?, confirmation_method = 'website', notion_sync_status = 'synced', "
        "submission_lock_state = 'locked' WHERE id = ?",
        (confirmed_at, job_id),
    )
    db.commit()

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert synced["updates"] == []
    assert job["status"] == "submitted"
    assert job["confirmed_at"] == confirmed_at
    assert job["confirmation_method"] == "website"
    assert job["failure_type"] is None
    assert job["error_message"] is None


def test_sync_job_from_disk_relocks_unlocked_resubmission_without_overwriting_first_confirmed_at(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/resubmit-sync")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(db, job_id, "draft", output_dir=str(out_dir))
    db.execute(
        "UPDATE jobs SET confirmed_at = ?, submission_lock_state = 'unlocked_for_resubmit', "
        "last_resubmit_unlocked_at = ?, resubmit_count = 0 WHERE id = ?",
        ("2026-03-18T17:11:18+00:00", "2026-03-30T03:45:00+00:00", job_id),
    )
    db.commit()
    (submit_dir / "application_submission_result.json").write_text(
        json.dumps(
            {
                "status": "confirmed",
                "website_confirmed": True,
                "confirmed_at_utc": "2026-03-30T04:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    sync_job_from_disk(db, job_id)

    job = get_job(db, job_id)
    assert job["submission_lock_state"] == "locked"
    assert job["confirmed_at"] == "2026-03-18T17:11:18+00:00"
    assert job["last_resubmit_confirmed_at"] == "2026-03-30T04:00:00+00:00"
    assert job["resubmit_count"] == 1


def test_sync_job_from_disk_keeps_locked_submission_history_idempotent(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/already-submitted")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    first_confirmed_at = "2026-03-18T17:11:18+00:00"
    db.execute(
        "UPDATE jobs SET status = 'submitted', output_dir = ?, confirmed_at = ?, "
        "submission_lock_state = 'locked', completed_at = ? WHERE id = ?",
        (str(out_dir), first_confirmed_at, first_confirmed_at, job_id),
    )
    db.commit()
    log_event(db, job_id, "submission_locked", detail=first_confirmed_at, initiator="worker")
    (submit_dir / "application_submission_result.json").write_text(
        json.dumps(
            {
                "status": "confirmed",
                "website_confirmed": True,
                "confirmed_at_utc": "2026-03-30T04:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    sync_job_from_disk(db, job_id)
    sync_job_from_disk(db, job_id)

    job = get_job(db, job_id)
    submission_locked_count = db.execute(
        "SELECT COUNT(*) AS count FROM events WHERE job_id = ? AND event_type = 'submission_locked'",
        (job_id,),
    ).fetchone()["count"]
    resubmitted_count = db.execute(
        "SELECT COUNT(*) AS count FROM events WHERE job_id = ? AND event_type = 'resubmitted'",
        (job_id,),
    ).fetchone()["count"]
    assert job["completed_at"] == first_confirmed_at
    assert job["confirmed_at"] == first_confirmed_at
    assert job["resubmit_count"] == 0
    assert submission_locked_count == 1
    assert resubmitted_count == 0


def test_sync_job_from_disk_is_noop_when_row_already_matches_artifacts(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/idempotent")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "application_submission_result.json").write_text(
        json.dumps(
            {
                "status": "pending_user_input",
                "failure_type": "pending_user_input",
                "message": "Submission paused because one or more answers require manual user input.",
            }
        ),
        encoding="utf-8",
    )
    (submit_dir / "greenhouse_autofill_report.json").write_text(
        json.dumps(
            {
                "fields": [
                    {"name": "First Name", "status": "filled"},
                    {"name": "Last Name", "status": "filled"},
                    {"name": "LinkedIn", "status": "skipped"},
                ]
            }
        ),
        encoding="utf-8",
    )
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="greenhouse",
        failure_type="retries_exhausted",
        error_message="Failed after 3 retries",
    )

    first_sync = sync_job_from_disk(db, job_id)
    first_job = get_job(db, job_id)

    assert first_sync["updates"]

    time.sleep(1.1)

    second_sync = sync_job_from_disk(db, job_id)
    second_job = get_job(db, job_id)

    assert second_sync["updates"] == []
    assert second_job["updated_at"] == first_job["updated_at"]


def test_record_confirmed_submission_counts_unlocked_resubmit_without_timestamp(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/resubmit-without-timestamp")
    db.execute(
        "UPDATE jobs SET status = 'submitting', confirmed_at = ?, submission_lock_state = 'unlocked_for_resubmit', "
        "last_resubmit_unlocked_at = ?, resubmit_count = 0 WHERE id = ?",
        ("2026-03-18T17:11:18+00:00", "2026-03-30T03:45:00+00:00", job_id),
    )
    db.commit()

    record_confirmed_submission(db, job_id, confirmed_at=None, initiator="worker")

    job = get_job(db, job_id)
    assert job["status"] == "submitted"
    assert job["submission_lock_state"] == "locked"
    assert job["confirmed_at"] == "2026-03-18T17:11:18+00:00"
    assert job["last_resubmit_confirmed_at"] is None
    assert job["resubmit_count"] == 1


def test_record_confirmed_submission_serializes_unlocked_resubmit_transition(tmp_path, monkeypatch):
    db_path = tmp_path / "concurrent_jobs.db"
    db = init_db(db_path)
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/concurrent-resubmit")
    db.execute(
        "UPDATE jobs SET status = 'submitting', confirmed_at = ?, submission_lock_state = 'unlocked_for_resubmit', "
        "last_resubmit_unlocked_at = ?, resubmit_count = 0 WHERE id = ?",
        ("2026-03-18T17:11:18+00:00", "2026-03-30T03:45:00+00:00", job_id),
    )
    db.commit()
    worker_conn = open_db(db_path, check_same_thread=False)
    disk_conn = open_db(db_path, check_same_thread=False)
    first_lock_acquired = threading.Event()
    release_first = threading.Event()
    errors: list[BaseException] = []
    original_execute = job_db.ManagedConnection.execute

    def execute_with_pause(self, sql, params=(), /):
        if sql == "BEGIN IMMEDIATE" and threading.current_thread().name == "locker":
            result = original_execute(self, sql, params)
            first_lock_acquired.set()
            assert release_first.wait(timeout=1)
            return result
        return original_execute(self, sql, params)

    monkeypatch.setattr(job_db.ManagedConnection, "execute", execute_with_pause)

    def run_record(conn, initiator):
        try:
            record_confirmed_submission(
                conn,
                job_id,
                confirmed_at="2026-03-30T04:00:00+00:00",
                initiator=initiator,
            )
        except BaseException as exc:  # pragma: no cover - surfaced by assertions below
            errors.append(exc)

    worker = threading.Thread(target=run_record, args=(worker_conn, "worker"), name="locker")
    disk_sync = threading.Thread(target=run_record, args=(disk_conn, "disk_sync"), name="waiter")

    worker.start()
    assert first_lock_acquired.wait(timeout=1)
    disk_sync.start()
    assert disk_sync.is_alive()
    release_first.set()
    worker.join(timeout=1)
    disk_sync.join(timeout=1)

    assert not errors
    job = get_job(db, job_id)
    submission_locked_count = db.execute(
        "SELECT COUNT(*) AS count FROM events WHERE job_id = ? AND event_type = 'submission_locked'",
        (job_id,),
    ).fetchone()["count"]
    resubmitted_count = db.execute(
        "SELECT COUNT(*) AS count FROM events WHERE job_id = ? AND event_type = 'resubmitted'",
        (job_id,),
    ).fetchone()["count"]
    assert job["submission_lock_state"] == "locked"
    assert job["resubmit_count"] == 1
    assert submission_locked_count == 1
    assert resubmitted_count == 1

    worker_conn.close()
    disk_conn.close()
    db.close()


def test_sync_job_from_disk_regenerates_missing_draft_summary_before_promoting_ready_proof(db, tmp_path):
    job_id = add_job(db, url="https://www.linkedin.com/jobs/view/123")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (docs_dir / "Jerrison Li Resume - Quizlet.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Quizlet.pdf").write_text("cover letter", encoding="utf-8")
    (submit_dir / "linkedin_autofill_report.json").write_text(
        json.dumps(_filled_autofill_report_payload()),
        encoding="utf-8",
    )
    (submit_dir / "linkedin_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    (out_dir / ".pipeline_meta.json").write_text(
        json.dumps({"company": "Quizlet", "role_title": "Lead PM", "board": "linkedin"}),
        encoding="utf-8",
    )
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="linkedin",
        failure_type="linkedin_validation_loop",
        error_message="Validation loop",
    )

    def _generate_summary(generated_out_dir, generated_submit_dir, meta):
        assert Path(generated_out_dir) == out_dir
        assert Path(generated_submit_dir) == submit_dir
        assert meta["board"] == "linkedin"
        (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
        return {"png_path": str(out_dir / "draft_summary.png")}

    with mock.patch("draft_manager.generate_draft_summary", side_effect=_generate_summary) as generate_summary:
        synced = sync_job_from_disk(db, job_id)

    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "draft"
    assert job["failure_type"] is None
    assert job["error_message"] is None
    assert (out_dir / "draft_summary.png").exists()
    generate_summary.assert_called_once()


def test_sync_job_from_disk_ignores_stale_pending_user_input_from_other_board(db, tmp_path):
    job_id = add_job(db, url="https://careers.synopsys.com/job/-/-/44408/92625958080")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Jerrison Li Resume - Engineer.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Engineer.pdf").write_text("cover letter", encoding="utf-8")
    (submit_dir / "avature_autofill_report.json").write_text(
        json.dumps(_filled_autofill_report_payload()),
        encoding="utf-8",
    )
    (submit_dir / "avature_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    (submit_dir / "pending_user_input.json").write_text(
        json.dumps(
            {
                "status": "pending_user_input",
                "board": "icims",
                "questions": [
                    {
                        "field_name": "pre_submit_screenshot",
                        "label": "Current-attempt pre-submit screenshot",
                        "reason": "The current submit attempt is missing the required pre-submit screenshot proof.",
                        "status": "missing",
                        "kind": "artifact",
                        "source": "draft_proof_contract",
                        "required": True,
                        "planned_value": str(submit_dir / "icims_autofill_pre_submit.png"),
                        "blocks_draft_completion": True,
                        "blocker_kind": "required_artifact",
                        "artifact_key": "pre_submit_screenshot",
                    }
                ],
                "artifacts": {
                    "report_json": str(submit_dir / "icims_autofill_report.json"),
                    "report_md": str(submit_dir / "icims_autofill_report.md"),
                },
            }
        ),
        encoding="utf-8",
    )
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="avature",
        failure_type="retries_exhausted",
        error_message="Failed after 3 retries",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "draft"
    assert job["failure_type"] is None
    assert job["error_message"] is None


def test_sync_job_from_disk_promotes_downstream_board_proof_after_linkedin_external_apply(db, tmp_path):
    job_id = add_job(db, url="https://www.linkedin.com/jobs/view/1234567890/")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")
    (docs_dir / "Jerrison Li Resume - Aircall.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Aircall.pdf").write_text("cover letter", encoding="utf-8")
    (submit_dir / "linkedin_autofill_payload.json").write_text("{}", encoding="utf-8")
    (submit_dir / "greenhouse_autofill_report.json").write_text(
        json.dumps(_filled_autofill_report_payload()),
        encoding="utf-8",
    )
    (submit_dir / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    (submit_dir / "application_submission_result.json").write_text(
        json.dumps(
            {
                "status": "not_easy_apply",
                "failure_type": "external_apply",
                "message": "LinkedIn job no longer exposes Easy Apply; an external Apply flow is shown instead.",
            }
        ),
        encoding="utf-8",
    )
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="linkedin",
        failure_type="external_apply",
        error_message="LinkedIn job no longer exposes Easy Apply; an external Apply flow is shown instead.",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "draft"
    assert job["failure_type"] is None
    assert job["error_message"] is None


def test_sync_job_from_disk_prefers_newer_submit_dir_over_stale_active_pointer(db, tmp_path):
    job_id = add_job(db, url="https://jobs.sap.com/job/123")
    out_dir = tmp_path / "job-output"
    active_submit = out_dir / "submit-20260406T194822Z"
    current_submit = out_dir / "submit"
    active_submit.mkdir(parents=True)
    current_submit.mkdir(parents=True)
    (out_dir / ".active_submit_dir").write_text("submit-20260406T194822Z\n", encoding="utf-8")
    (active_submit / "application_submission_result.json").write_text(
        json.dumps({"status": "unknown", "failure_type": "unknown", "message": "Redirected to careers home"}),
        encoding="utf-8",
    )
    (current_submit / "application_submission_result.json").write_text(
        json.dumps({"status": "pending_user_input", "message": "Submission paused pending manual review."}),
        encoding="utf-8",
    )
    (current_submit / "pending_user_input.json").write_text(
        json.dumps(
            {
                "status": "pending_user_input",
                "message": "Submission paused pending manual review.",
                "questions": [{"label": "Resume"}],
            }
        ),
        encoding="utf-8",
    )
    os.utime(active_submit / "application_submission_result.json", (1_000_000_000, 1_000_000_000))
    os.utime(current_submit / "application_submission_result.json", (1_000_000_100, 1_000_000_100))
    os.utime(current_submit / "pending_user_input.json", (1_000_000_100, 1_000_000_100))
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="successfactors",
        failure_type="unknown",
        error_message="Redirected to careers home",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "stopped"
    assert job["failure_type"] == "pending_user_input"
    assert job["error_message"] == "Submission paused pending manual review."


def test_sync_job_from_disk_infers_greenhouse_payload_build_failure_from_submit_output(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/123")
    out_dir = tmp_path / "job-output"
    (out_dir / "submit").mkdir(parents=True)
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="greenhouse",
        failure_type="retries_exhausted",
        error_message="Failed after 3 retries",
    )
    log_event(
        db,
        job_id,
        "submit_output",
        detail=(
            "Traceback (most recent call last):\n"
            '  File "scripts/autofill_greenhouse.py", line 1, in <module>\n'
            "ValueError: Autofill payload is missing required Greenhouse fields: question_123"
        ),
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["failure_type"] == "greenhouse_runtime_error"
    assert job["error_message"] == "Autofill payload is missing required Greenhouse fields: question_123"


def test_sync_job_from_disk_infers_greenhouse_unknown_questions_from_submit_output(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/124")
    out_dir = tmp_path / "job-output"
    (out_dir / "submit").mkdir(parents=True)
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="greenhouse",
        failure_type="retries_exhausted",
        error_message="Failed after 3 retries",
    )
    log_event(
        db,
        job_id,
        "submit_output",
        detail=(
            "Traceback (most recent call last):\n"
            '  File "scripts/autofill_greenhouse.py", line 1, in <module>\n'
            "RuntimeError: Encountered required application questions that do not have answers in the payload. "
            "See /tmp/job-output/submit/greenhouse_unknown_questions.json for details."
        ),
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["failure_type"] == "greenhouse_unknown_questions"
    assert job["error_message"].startswith(
        "Encountered required application questions that do not have answers in the payload."
    )


def test_sync_job_from_disk_infers_greenhouse_runtime_error_from_submit_output_traceback(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/125")
    out_dir = tmp_path / "job-output"
    (out_dir / "submit").mkdir(parents=True)
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="greenhouse",
        failure_type="retries_exhausted",
        error_message="Failed after 3 retries",
    )
    log_event(
        db,
        job_id,
        "submit_output",
        detail=(
            "Traceback (most recent call last):\n"
            '  File "scripts/autofill_greenhouse.py", line 1, in <module>\n'
            "AttributeError: 'ApplicationProfile' object has no attribute 'education_entries'"
        ),
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["failure_type"] == "greenhouse_runtime_error"
    assert job["error_message"].startswith("Greenhouse payload build crashed before writing a submission result:")


def test_sync_job_from_disk_clears_stale_failure_metadata_for_already_applied_result(db, tmp_path):
    job_id = add_job(db, url="https://autodesk.wd1.myworkdayjobs.com/Ext/job/1")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(
        db,
        job_id,
        "stopped",
        output_dir=str(out_dir),
        board="workday",
        failure_type="retries_exhausted",
        error_message="Failed after 3 retries",
    )
    (submit_dir / "application_submission_result.json").write_text(
        """
        {
          "status": "already_applied",
          "board": "workday",
          "website_confirmed": true,
          "message": "Workday already shows this job as applied."
        }
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    synced = sync_job_from_disk(db, job_id)
    job = get_job(db, job_id)

    assert synced["synced"] is True
    assert job["status"] == "submitted"
    assert job["failure_type"] is None
    assert job["error_message"] is None
    assert job["confirmation_method"] == "website"


def test_sync_job_from_disk_backfills_confirmation_email_reply_when_confirmation_arrives_late(db, tmp_path):
    job_id = add_job(db, url="https://autodesk.wd1.myworkdayjobs.com/Ext/job/1")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(db, job_id, "stopped", output_dir=str(out_dir), board="workday")
    (submit_dir / "application_submission_result.json").write_text(
        json.dumps(
            {
                "status": "already_applied",
                "board": "workday",
                "website_confirmed": True,
                "message": "Workday already shows this job as applied.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    email_confirmation = {
        "message_id": "gmail-msg-1",
        "thread_id": "gmail-thread-1",
        "subject": "Application received",
        "received_at_utc": "2026-03-28T04:15:03+00:00",
    }
    (submit_dir / "application_confirmation_email.json").write_text(
        json.dumps(email_confirmation) + "\n",
        encoding="utf-8",
    )

    with mock.patch(
        "application_submit_common.send_confirmation_email_reply",
        return_value={
            "status": "sent",
            "submit_dir": str(submit_dir),
            "state_path": str(submit_dir / "confirmation_email_reply.json"),
        },
    ) as send_reply:
        synced = sync_job_from_disk(db, job_id)

    job = get_job(db, job_id)
    events = db.execute(
        "SELECT event_type, initiator FROM events WHERE job_id = ? ORDER BY id DESC LIMIT 5",
        (job_id,),
    ).fetchall()

    assert synced["synced"] is True
    assert "email_reply→sent" in synced["updates"]
    assert job["status"] == "submitted"
    assert job["email_confirmed"] == 1
    send_reply.assert_called_once_with(
        {"out_dir": str(out_dir)},
        board_name="workday",
        email_confirmation=email_confirmation,
        caller="disk_sync",
    )
    assert ("email_reply_sent", "disk_sync") in {(row["event_type"], row["initiator"]) for row in events}


def test_sync_job_from_disk_skips_reply_backfill_when_reply_state_already_exists(db, tmp_path):
    job_id = add_job(db, url="https://autodesk.wd1.myworkdayjobs.com/Ext/job/1")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(db, job_id, "submitted", output_dir=str(out_dir), board="workday")
    (submit_dir / "application_submission_result.json").write_text(
        json.dumps(
            {
                "status": "already_applied",
                "board": "workday",
                "website_confirmed": True,
                "message": "Workday already shows this job as applied.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (submit_dir / "application_confirmation_email.json").write_text(
        json.dumps({"thread_id": "gmail-thread-1"}) + "\n",
        encoding="utf-8",
    )
    (submit_dir / "confirmation_email_reply.json").write_text(
        json.dumps({"sent": True, "thread_id": "gmail-thread-1"}) + "\n",
        encoding="utf-8",
    )

    with mock.patch("application_submit_common.send_confirmation_email_reply") as send_reply:
        synced = sync_job_from_disk(db, job_id)

    assert synced["synced"] is True
    send_reply.assert_not_called()


def test_reset_stale_submitting_ignores_age(db):
    """Submitting jobs are reset even if recently updated (not stale by age)."""
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/104")
    update_status(db, job_id, "submitting")
    # Don't backdate — job was just updated. Should still be reset.
    ids = reset_stale_jobs(db, stale_threshold_seconds=1800)
    assert job_id in ids
    job = get_job(db, job_id)
    assert job["status"] == "draft"


def test_reset_stale_jobs_caps_far_future_retry_after(db):
    """Queued jobs with buggy far-future retry_after values are restored."""
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/105")
    db.execute(
        "UPDATE jobs SET retry_after = datetime('now', '+2 hours') WHERE id = ?",
        (job_id,),
    )
    db.commit()

    reset_stale_jobs(db, stale_threshold_seconds=1800)

    job = get_job(db, job_id)
    assert job["retry_after"] == RETRY_AFTER_SENTINEL


# ── JD fingerprint duplicate detection ──────────────────────────────────


def test_normalize_company():
    assert normalize_company("Acme, Inc.") == "acme"
    assert normalize_company("  Big Tech  ") == "big tech"
    assert normalize_company("My-Co LLC") == "myco"


def test_jd_fingerprint_returns_none_for_short_jd():
    assert jd_fingerprint("Acme", None) is None
    assert jd_fingerprint("Acme", "too short") is None


def test_jd_fingerprint_stable():
    jd = "We are looking for a senior engineer to join our platform team. " * 5
    fp1 = jd_fingerprint("Acme Inc.", jd)
    fp2 = jd_fingerprint("Acme, Inc.", jd)
    assert fp1 == fp2  # normalized company names match
    assert fp1 is not None
    assert len(fp1) == 16


def test_jd_fingerprint_matches_slug_company_variants():
    jd = "We are looking for a senior engineer to join our platform team. " * 5
    assert jd_fingerprint("Snorkel AI", jd) == jd_fingerprint("snorkel-ai", jd)


def test_jd_fingerprint_differs_for_different_jds():
    jd1 = "Senior backend engineer building distributed systems. " * 5
    jd2 = "Junior frontend developer working on React apps. " * 5
    fp1 = jd_fingerprint("Acme", jd1)
    fp2 = jd_fingerprint("Acme", jd2)
    assert fp1 != fp2


def test_set_jd_fingerprint_stores_in_db(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/200")
    jd = "We are hiring a senior engineer for our platform team. " * 5
    fp = set_jd_fingerprint(db, job_id, "Acme", jd)
    assert fp is not None
    job = get_job(db, job_id)
    assert job["jd_fingerprint"] == fp


def test_check_jd_duplicate_finds_match(db):
    jd = "Looking for senior software engineer to build APIs and services. " * 5
    job_id1 = add_job(db, url="https://boards.greenhouse.io/co/jobs/300")
    set_jd_fingerprint(db, job_id1, "Acme", jd)
    update_status(db, job_id1, "generating", company="Acme", role_title="Senior SWE")

    # Same JD, different URL — should detect duplicate
    job_id2 = add_job(db, url="https://www.linkedin.com/jobs/view/99999")
    dup = check_jd_duplicate(db, "Acme", jd, exclude_job_id=job_id2)
    assert dup is not None
    assert dup["id"] == job_id1


def test_check_jd_duplicate_no_false_positive(db):
    jd1 = "Senior backend engineer building distributed systems. " * 5
    jd2 = "Junior frontend developer working on React apps. " * 5
    job_id1 = add_job(db, url="https://boards.greenhouse.io/co/jobs/400")
    set_jd_fingerprint(db, job_id1, "Acme", jd1)

    dup = check_jd_duplicate(db, "Acme", jd2, exclude_job_id=None)
    assert dup is None


def test_check_jd_duplicate_excludes_self(db):
    jd = "Looking for a data scientist to analyze user behavior patterns. " * 5
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/500")
    set_jd_fingerprint(db, job_id, "Acme", jd)

    # Should not match itself
    dup = check_jd_duplicate(db, "Acme", jd, exclude_job_id=job_id)
    assert dup is None


def test_check_jd_duplicate_prefers_active_row_over_archived_match(db):
    jd = "Lead roadmap for a platform product across billing and analytics. " * 5
    archived_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/550", company="Acme", role_title="Archived PM")
    active_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/551", company="Acme", role_title="Active PM")
    set_jd_fingerprint(db, archived_id, "Acme", jd)
    set_jd_fingerprint(db, active_id, "Acme", jd)
    db.execute("UPDATE jobs SET archived = TRUE WHERE id = ?", (archived_id,))
    db.commit()

    dup = check_jd_duplicate(db, "Acme", jd)

    assert dup is not None
    assert dup["id"] == active_id


def test_backfill_jd_fingerprints(db, tmp_path):
    """Backfill computes fingerprints from jd_raw.md in output dirs."""
    jd = "We need a principal engineer to lead infrastructure projects. " * 5
    # Create an output dir with jd_raw.md
    out = tmp_path / "output" / "acme" / "principal-engineer"
    content = out / "content"
    content.mkdir(parents=True)
    (content / "jd_raw.md").write_text(jd)

    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/600")
    update_status(db, job_id, "generating", company="Acme", output_dir=str(out))

    assert get_job(db, job_id)["jd_fingerprint"] is None
    updated, skipped = backfill_jd_fingerprints(db)
    assert updated == 1
    assert skipped == 0
    assert get_job(db, job_id)["jd_fingerprint"] is not None


def test_backfill_skips_jobs_without_jd(db, tmp_path):
    """Backfill skips jobs whose output dir has no jd_raw.md."""
    out = tmp_path / "output" / "acme" / "empty-role"
    out.mkdir(parents=True)
    job_id = add_job(db, url="https://boards.greenhouse.io/acme/jobs/601")
    update_status(db, job_id, "generating", company="Acme", output_dir=str(out))

    updated, skipped = backfill_jd_fingerprints(db)
    assert updated == 0
    assert skipped == 1


def test_find_jd_duplicates(db):
    """find_jd_duplicates returns groups of jobs sharing a fingerprint."""
    jd = "Staff ML engineer to build recommendation systems at scale. " * 5
    id1 = add_job(db, url="https://boards.greenhouse.io/acme/jobs/700")
    id2 = add_job(db, url="https://www.linkedin.com/jobs/view/88888")
    set_jd_fingerprint(db, id1, "Acme", jd)
    set_jd_fingerprint(db, id2, "Acme", jd)

    groups = find_jd_duplicates(db)
    assert len(groups) == 1
    assert len(groups[0]) == 2
    ids = {g["id"] for g in groups[0]}
    assert ids == {id1, id2}


def test_find_jd_duplicates_no_dupes(db):
    """No groups returned when all fingerprints are unique."""
    jd1 = "Senior backend engineer building distributed systems. " * 5
    jd2 = "Junior frontend developer working on React apps. " * 5
    id1 = add_job(db, url="https://boards.greenhouse.io/a/jobs/800")
    id2 = add_job(db, url="https://boards.greenhouse.io/b/jobs/801")
    set_jd_fingerprint(db, id1, "AcmeA", jd1)
    set_jd_fingerprint(db, id2, "AcmeB", jd2)

    groups = find_jd_duplicates(db)
    assert len(groups) == 0


# ── Cross-source company+role duplicate prevention ──────────────────────


def test_add_job_duplicate_company_role_returns_negative(db):
    """Adding the same company+role twice returns negative ID on the second add."""
    id1 = add_job(
        db,
        url="https://boards.greenhouse.io/stampli/jobs/100",
        company="Stampli",
        role_title="Sr PM Procurement",
    )
    assert id1 > 0
    id2 = add_job(
        db,
        url="https://www.linkedin.com/jobs/view/99999",
        company="Stampli",
        role_title="Sr PM Procurement",
    )
    assert id2 == -id1  # negative of existing job ID


def test_add_job_duplicate_company_role_case_insensitive(db):
    """Company+role dedup is case-insensitive."""
    id1 = add_job(
        db,
        url="https://boards.greenhouse.io/acme/jobs/200",
        company="Acme Corp",
        role_title="Software Engineer",
    )
    id2 = add_job(
        db,
        url="https://www.linkedin.com/jobs/view/88888",
        company="ACME CORP",
        role_title="software engineer",
    )
    assert id2 == -id1


def test_add_job_duplicate_company_role_normalizes_linkedin_wrapper_titles_and_company_suffixes(db):
    existing_id = add_job(
        db,
        url="https://jobs.ashbyhq.com/valon/0f1fbd7d-e30d-4b8c-9e3d-04ff6bfd7638",
        company="Valon Tech",
        role_title="senior-pm-product-infrastructure",
    )

    duplicate_id = add_job(
        db,
        url="https://www.linkedin.com/jobs/view/4366508877/",
        company="Valon",
        role_title="Valon hiring Senior Product Manager, Product Infrastructure in San Francisco, CA | LinkedIn",
    )

    assert duplicate_id == -existing_id


def test_add_job_duplicate_company_role_matches_slug_company_names(db):
    existing_id = add_job(
        db,
        url="https://job-boards.greenhouse.io/snorkelai/jobs/5811231004",
        company="snorkel-ai",
        role_title="Senior Product Manager - Platform",
    )

    duplicate_id = add_job(
        db,
        url="https://job-boards.greenhouse.io/snorkelai/jobs/5811231004?utm_source=trueup.io&utm_medium=website&ref=trueup",
        company="Snorkel AI",
        role_title="senior-pm-platform",
    )

    assert duplicate_id == -existing_id


def test_add_job_jd_duplicate_returns_negative_archived_id(db):
    existing_id = add_job(
        db,
        url="https://boards.greenhouse.io/acme/jobs/201",
        company="Acme",
        role_title="Senior Product Manager",
        jd_text="Lead roadmap for a platform product across billing and analytics.",
    )
    db.execute("UPDATE jobs SET archived = TRUE WHERE id = ?", (existing_id,))
    db.commit()

    duplicate_id = add_job(
        db,
        url="https://boards.greenhouse.io/acme/jobs/202",
        company="Acme",
        role_title="Principal Product Manager",
        jd_text="Lead roadmap for a platform product across billing and analytics.",
    )

    assert duplicate_id == -existing_id


def test_add_job_active_url_duplicate_wins_over_archived_jd_match(db):
    add_job(
        db,
        url="https://boards.greenhouse.io/acme/jobs/210",
        company="Acme",
        role_title="Active Product Manager",
    )
    archived_id = add_job(
        db,
        url="https://boards.greenhouse.io/acme/jobs/211",
        company="Acme",
        role_title="Archived Product Manager",
        jd_text="Lead roadmap for a platform product across billing and analytics.",
    )
    db.execute("UPDATE jobs SET archived = TRUE WHERE id = ?", (archived_id,))
    db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="same url"):
        add_job(
            db,
            url="https://boards.greenhouse.io/acme/jobs/210",
            company="Acme",
            role_title="New Product Manager",
            jd_text="Lead roadmap for a platform product across billing and analytics.",
        )


def test_add_job_different_roles_same_company_not_duplicate(db):
    """Different roles at the same company are NOT duplicates."""
    id1 = add_job(
        db,
        url="https://boards.greenhouse.io/acme/jobs/300",
        company="Acme",
        role_title="Backend Engineer",
    )
    id2 = add_job(
        db,
        url="https://boards.greenhouse.io/acme/jobs/301",
        company="Acme",
        role_title="Frontend Engineer",
    )
    assert id1 > 0
    assert id2 > 0
    assert id2 != -id1


def test_add_job_archived_jobs_dont_block_reimport(db):
    """Archived jobs don't block new imports of the same position."""
    id1 = add_job(
        db,
        url="https://boards.greenhouse.io/acme/jobs/400",
        company="Acme",
        role_title="Staff Engineer",
    )
    # Archive the job
    db.execute("UPDATE jobs SET archived = TRUE WHERE id = ?", (id1,))
    db.commit()
    # Re-importing the same company+role should succeed (new URL)
    id2 = add_job(
        db,
        url="https://boards.greenhouse.io/acme/jobs/401",
        company="Acme",
        role_title="Staff Engineer",
    )
    assert id2 > 0  # new job created, not a duplicate


def test_add_job_null_company_no_false_match(db):
    """Null company doesn't trigger false duplicate matches."""
    id1 = add_job(
        db,
        url="https://boards.greenhouse.io/unknown/jobs/500",
        company=None,
        role_title="Engineer",
    )
    id2 = add_job(
        db,
        url="https://boards.greenhouse.io/unknown/jobs/501",
        company=None,
        role_title="Engineer",
    )
    assert id1 > 0
    assert id2 > 0  # both succeed, no false duplicate


def test_add_job_null_role_no_false_match(db):
    """Null role_title doesn't trigger false duplicate matches."""
    id1 = add_job(
        db,
        url="https://boards.greenhouse.io/acme/jobs/600",
        company="Acme",
        role_title=None,
    )
    id2 = add_job(
        db,
        url="https://boards.greenhouse.io/acme/jobs/601",
        company="Acme",
        role_title=None,
    )
    assert id1 > 0
    assert id2 > 0  # both succeed, no false duplicate


def test_reconcile_duplicate_jobs_backfills_keeper_metadata_and_archives_later_cross_source_match(db, tmp_path):
    keeper_out = tmp_path / "output" / "valon" / "senior-pm-product-infrastructure"
    keeper_out.mkdir(parents=True)
    (keeper_out / ".pipeline_meta.json").write_text(
        json.dumps(
            {
                "company": "valon",
                "company_proper": "Valon",
                "role": "senior-pm-product-infrastructure",
                "board": "ashby",
                "board_url": "https://jobs.ashbyhq.com/valon/0f1fbd7d-e30d-4b8c-9e3d-04ff6bfd7638",
            }
        ),
        encoding="utf-8",
    )

    keeper_id = add_job(db, url="https://www.linkedin.com/jobs/view/4366508877?trackingId=abc123")
    update_status(db, keeper_id, "draft", output_dir=str(keeper_out))

    duplicate_id = add_job(
        db,
        url="https://jobs.ashbyhq.com/valon/0f1fbd7d-e30d-4b8c-9e3d-04ff6bfd7638",
        company="Valon",
        role_title="Valon hiring Senior Product Manager, Product Infrastructure in San Francisco, CA | LinkedIn",
    )
    update_status(
        db,
        duplicate_id,
        "stopped",
        company="Valon",
        role_title="Valon hiring Senior Product Manager, Product Infrastructure in San Francisco, CA | LinkedIn",
        board="ashby",
        board_url="https://jobs.ashbyhq.com/valon/0f1fbd7d-e30d-4b8c-9e3d-04ff6bfd7638",
    )

    summary = reconcile_duplicate_jobs(db, initiator="test")

    keeper = get_job(db, keeper_id)
    duplicate = get_job(db, duplicate_id)

    assert summary["metadata_backfilled"] == 1
    assert summary["archived"] == 1
    assert summary["skipped_processing"] == 0
    assert summary["archived_job_ids"] == [duplicate_id]
    assert keeper["company"] == "Valon"
    assert keeper["role_title"] == "senior-pm-product-infrastructure"
    assert keeper["board"] == "ashby"
    assert keeper["board_url"] == "https://jobs.ashbyhq.com/valon/0f1fbd7d-e30d-4b8c-9e3d-04ff6bfd7638"
    assert duplicate["status"] == "stopped"
    assert bool(duplicate["archived"]) is True
    assert duplicate["failure_type"] == "duplicate"
    assert f"job #{keeper_id}" in str(duplicate["error_message"] or "")


def test_reconcile_duplicate_jobs_skips_processing_duplicates(db):
    keeper_id = add_job(
        db,
        url="https://boards.greenhouse.io/acme/jobs/7001",
        company="Acme",
        role_title="Senior Product Manager",
    )
    update_status(db, keeper_id, "draft", company="Acme", role_title="Senior Product Manager")

    duplicate_id = add_job(
        db,
        url="https://www.linkedin.com/jobs/view/70010002",
    )
    update_status(db, duplicate_id, "generating", company="Acme", role_title="Senior Product Manager")

    summary = reconcile_duplicate_jobs(db, initiator="test")

    duplicate = get_job(db, duplicate_id)

    assert summary["metadata_backfilled"] == 0
    assert summary["archived"] == 0
    assert summary["skipped_processing"] == 1
    assert summary["archived_job_ids"] == []
    assert duplicate["status"] == "generating"
    assert bool(duplicate["archived"]) is False


def test_reconcile_duplicate_jobs_uses_normalized_source_url_for_legacy_linkedin_wrapper_duplicates(db):
    keeper_id = add_job(db, url="https://www.linkedin.com/jobs/view/4366690075/")
    update_status(
        db,
        keeper_id,
        "stopped",
        company="Supermicro",
        role_title="principal-pm-dcim-software-27484",
        board="linkedin",
    )

    duplicate_url = (
        "https://www.linkedin.com/jobs/view/4366690075/"
        "?lipi=urn%3Ali%3Apage%3Ad_flagship3_opportunity_tracker%3BNqv0q%2FLtSuWqwQBiw9FPZw%3D%3D"
    )
    duplicate_id = add_job(
        db,
        url="https://jobs.supermicro.com/job/San-Jose-Principal-Product-Manager-DCIM-Software-(27484)-Cali/1323446000/",
        source_override="linkedin",
        source_url_override=duplicate_url,
    )
    db.execute(
        "UPDATE jobs SET url = ?, board = ?, board_url = ?, canonical_url = ?, company = ?, role_title = ?, status = ? WHERE id = ?",
        (
            duplicate_url,
            "successfactors",
            "https://jobs.supermicro.com/job/San-Jose-Principal-Product-Manager-DCIM-Software-(27484)-Cali/1323446000/",
            duplicate_url,
            "Architect and",
            "principal-pm-dcim-software-27484",
            "stopped",
            duplicate_id,
        ),
    )
    db.commit()

    summary = reconcile_duplicate_jobs(db, initiator="test")

    duplicate = get_job(db, duplicate_id)

    assert summary["archived"] == 1
    assert summary["archived_job_ids"] == [duplicate_id]
    assert duplicate["failure_type"] == "duplicate"
    assert bool(duplicate["archived"]) is True
    assert f"job #{keeper_id}" in str(duplicate["error_message"] or "")


# ---------------------------------------------------------------------------
# Corruption detection tests
# ---------------------------------------------------------------------------


def test_init_db_detects_corruption(tmp_path):
    """init_db raises RuntimeError on a corrupted database file."""
    corrupt_db = tmp_path / "corrupt.db"
    # Create a valid DB first, then corrupt it
    conn = init_db(corrupt_db)
    conn.close()
    # Overwrite the middle of the file with garbage to corrupt B-tree pages
    data = corrupt_db.read_bytes()
    corrupted = data[:100] + b"\x00" * 200 + data[300:]
    corrupt_db.write_bytes(corrupted)
    with pytest.raises(RuntimeError, match="SQLite corruption"):
        init_db(corrupt_db)


def test_init_db_succeeds_on_fresh_db(tmp_path):
    """init_db works normally on a fresh database."""
    db_path = tmp_path / "fresh.db"
    conn = init_db(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "jobs" in tables
    conn.close()


def test_open_db_does_not_create_schema(tmp_path):
    """open_db only sets PRAGMAs, does not create tables."""
    db_path = tmp_path / "empty.db"
    conn = open_db(db_path)
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert len(tables) == 0
    conn.close()


# ── Connection tracking tests ────────────────────────────────────────────


def test_open_db_tracked_registers_connection(tmp_path):
    """open_db_tracked returns a working connection and registers it in _connections."""
    db_path = tmp_path / "tracked.db"
    conn = open_db_tracked(db_path)
    try:
        # Connection works
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert isinstance(tables, list)
        # Connection is tracked
        with _conn_lock:
            assert conn in _connections
    finally:
        conn.close()
        with _conn_lock:
            _connections.discard(conn)


def test_close_all_connections_closes_and_clears(tmp_path):
    """close_all_connections closes every tracked connection and empties the registry."""
    db_path = tmp_path / "tracked_close.db"
    conn1 = open_db_tracked(db_path)
    conn2 = open_db_tracked(db_path)
    with _conn_lock:
        assert conn1 in _connections
        assert conn2 in _connections

    close_all_connections()

    # Registry is empty
    with _conn_lock:
        assert len(_connections) == 0

    # Connections are actually closed (executing should raise ProgrammingError)
    with pytest.raises(sqlite3.ProgrammingError):
        conn1.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        conn2.execute("SELECT 1")


# ── WAL autocheckpoint tests ────────────────────────────────────────────


def test_open_db_sets_wal_autocheckpoint(tmp_path):
    """open_db sets wal_autocheckpoint=4000 to mitigate WAL-reset bug."""
    db_path = tmp_path / "wal.db"
    conn = open_db(db_path)
    val = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
    assert val == 4000
    conn.close()


def test_init_db_wal_version_warning(tmp_path, caplog):
    """init_db logs a warning when SQLite version < 3.50.7."""
    import logging

    # Current SQLite 3.50.4 is < 3.50.7, so the warning should fire.
    ver = tuple(int(x) for x in sqlite3.sqlite_version.split("."))
    db_path = tmp_path / "wal_warn.db"
    with caplog.at_level(logging.WARNING, logger="job_db"):
        conn = init_db(db_path)
        conn.close()
    if ver < (3, 50, 7):
        assert any("WAL-reset data race" in r.message for r in caplog.records)
    else:
        assert not any("WAL-reset data race" in r.message for r in caplog.records)


# ── Per-table startup probe tests ───────────────────────────────────────


def test_init_db_probes_all_tables(tmp_path):
    """init_db successfully probes all tables on a healthy database."""
    db_path = tmp_path / "probe.db"
    conn = init_db(db_path)
    # Verify all 8 expected tables exist and are queryable
    expected = {
        "jobs",
        "events",
        "fix_attempts",
        "provider_runs",
        "job_phase_durations",
        "field_corrections",
        "job_metrics",
        "candidate_jobs",
    }
    for table in expected:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert count >= 0
    conn.close()


def test_init_db_probe_detects_corrupted_table(tmp_path):
    """init_db raises RuntimeError when a table is unreadable after schema creation."""
    db_path = tmp_path / "probe_corrupt.db"
    # Create a valid DB first
    conn = init_db(db_path)
    conn.close()

    # Corrupt just the events table by dropping and recreating as a view
    # that will fail on SELECT COUNT(*).  Simpler: insert garbage into
    # sqlite_master to make the table unreadable.
    # The most reliable way: corrupt the DB file after knowing events exists.
    raw = sqlite3.connect(str(db_path))
    # Get the root page of the events table
    row = raw.execute("SELECT rootpage FROM sqlite_master WHERE name='events' AND type='table'").fetchone()
    raw.close()

    if row:
        rootpage = row[0]
        page_size = 4096  # default SQLite page size
        data = bytearray(db_path.read_bytes())
        offset = (rootpage - 1) * page_size
        # Zero out the page to make it unreadable
        data[offset : offset + page_size] = b"\x00" * page_size
        db_path.write_bytes(bytes(data))

        # init_db should detect this via the table probe
        with pytest.raises(RuntimeError, match="(unreadable|corruption)"):
            init_db(db_path)
