import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from job_db import add_job, get_job, get_job_timeline, init_db
from submission_result_outcomes import handle_draft_mode_submission_result


def test_handle_draft_mode_submission_result_persists_auth_state(tmp_path):
    conn = init_db(tmp_path / "jobs.db")
    try:
        job_id = add_job(conn, url="https://example.myworkdayjobs.com/job/123")

        status = handle_draft_mode_submission_result(
            conn,
            job_id,
            {
                "status": "auth_unknown",
                "board": "workday",
                "auth_state": "account_verification_gate",
                "auth_scope": "workday:example/careers",
                "message": (
                    "Workday never reached the application form after trying sign in, "
                    "password reset, and create account. Saved evidence for diagnosis."
                ),
            },
        )

        job = get_job(conn, job_id)

        assert status == "stopped"
        assert job["status"] == "stopped"
        assert job["failure_type"] == "auth_unknown"
        assert job["auth_state"] == "account_verification_gate"
        assert job["auth_scope"] == "workday:example/careers"
    finally:
        conn.close()


def test_handle_draft_mode_submission_result_records_confirmation_lock_and_timestamp(tmp_path):
    conn = init_db(tmp_path / "jobs.db")
    try:
        job_id = add_job(conn, url="https://boards.greenhouse.io/example/jobs/123")

        status = handle_draft_mode_submission_result(
            conn,
            job_id,
            {
                "status": "confirmed",
                "board": "greenhouse",
                "website_confirmed": True,
                "confirmed_at_utc": "2026-03-30T02:20:50+00:00",
                "message": "Application submitted successfully.",
            },
        )

        job = get_job(conn, job_id)
        event_types = [event["event_type"] for event in get_job_timeline(conn, job_id)]

        assert status == "submitted"
        assert job["status"] == "submitted"
        assert job["submission_lock_state"] == "locked"
        assert job["confirmed_at"] == "2026-03-30T02:20:50+00:00"
        assert "submission_locked" in event_types
        assert "submission_result_submitted" in event_types
    finally:
        conn.close()
