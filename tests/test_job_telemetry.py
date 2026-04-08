# tests/test_job_telemetry.py
"""Tests for telemetry tables and functions: phase durations, field corrections, job metrics, aggregates."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest
from job_db import (
    add_job,
    end_phase,
    ensure_job_metrics,
    get_all_job_metrics,
    get_board_error_rates,
    get_field_corrections,
    get_job_metrics,
    get_jobs_processed_counts,
    get_phase_avg_durations,
    get_summary_stats,
    init_db,
    log_field_correction,
    start_phase,
    update_job_metrics,
    update_status,
)


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test_telemetry.db"
    conn = init_db(db_path)
    yield conn
    conn.close()


@pytest.fixture
def job_id(db):
    """Create a single job and return its id."""
    return add_job(db, url="https://boards.greenhouse.io/testco/jobs/100")


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestSchema:
    def test_job_phase_durations_table_exists(self, db):
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "job_phase_durations" in tables

    def test_field_corrections_table_exists(self, db):
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "field_corrections" in tables

    def test_job_metrics_table_exists(self, db):
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "job_metrics" in tables

    def test_job_phase_durations_columns(self, db):
        cols = {r[1] for r in db.execute("PRAGMA table_info(job_phase_durations)").fetchall()}
        expected = {
            "id",
            "job_id",
            "phase",
            "started_at",
            "ended_at",
            "duration_ms",
            "exit_code",
            "created_at",
        }
        assert expected <= cols

    def test_field_corrections_columns(self, db):
        cols = {r[1] for r in db.execute("PRAGMA table_info(field_corrections)").fetchall()}
        expected = {
            "id",
            "job_id",
            "field_name",
            "original_value",
            "corrected_value",
            "correction_source",
            "created_at",
        }
        assert expected <= cols

    def test_job_metrics_columns(self, db):
        cols = {r[1] for r in db.execute("PRAGMA table_info(job_metrics)").fetchall()}
        expected = {
            "job_id",
            "total_fields",
            "fields_corrected",
            "field_error_rate",
            "manual_interventions",
            "auto_fix_attempts",
            "total_duration_ms",
            "phase_count",
            "retry_count",
            "updated_at",
        }
        assert expected <= cols


# ---------------------------------------------------------------------------
# Phase duration tests
# ---------------------------------------------------------------------------


class TestPhaseDurations:
    def test_start_phase_returns_id(self, db, job_id):
        phase_id = start_phase(db, job_id, "resolving")
        assert isinstance(phase_id, int)
        assert phase_id > 0

    def test_end_phase_sets_duration_and_exit_code(self, db, job_id):
        phase_id = start_phase(db, job_id, "resolving")
        end_phase(db, phase_id, exit_code=0)
        row = db.execute("SELECT * FROM job_phase_durations WHERE id = ?", (phase_id,)).fetchone()
        assert row["ended_at"] is not None
        assert row["duration_ms"] is not None
        assert row["duration_ms"] >= 0
        assert row["exit_code"] == 0

    def test_end_phase_without_exit_code(self, db, job_id):
        phase_id = start_phase(db, job_id, "generating")
        end_phase(db, phase_id)
        row = db.execute("SELECT * FROM job_phase_durations WHERE id = ?", (phase_id,)).fetchone()
        assert row["ended_at"] is not None
        assert row["exit_code"] is None

    def test_multiple_phases_per_job(self, db, job_id):
        p1 = start_phase(db, job_id, "resolving")
        end_phase(db, p1, exit_code=0)
        p2 = start_phase(db, job_id, "generating")
        end_phase(db, p2, exit_code=0)
        p3 = start_phase(db, job_id, "submitting")
        end_phase(db, p3, exit_code=1)

        rows = db.execute(
            "SELECT * FROM job_phase_durations WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
        assert len(rows) == 3
        phases = [r["phase"] for r in rows]
        assert phases == ["resolving", "generating", "submitting"]
        assert rows[2]["exit_code"] == 1


# ---------------------------------------------------------------------------
# Field correction tests
# ---------------------------------------------------------------------------


class TestFieldCorrections:
    def test_log_field_correction_returns_id(self, db, job_id):
        corr_id = log_field_correction(db, job_id, "first_name", "Jon", "John", "llm")
        assert isinstance(corr_id, int)
        assert corr_id > 0

    def test_get_field_corrections_retrieves_logged(self, db, job_id):
        log_field_correction(db, job_id, "first_name", "Jon", "John", "llm")
        log_field_correction(db, job_id, "email", "bad@", "good@example.com", "validation")

        corrections = get_field_corrections(db, job_id)
        assert len(corrections) == 2
        assert corrections[0]["field_name"] == "first_name"
        assert corrections[0]["original_value"] == "Jon"
        assert corrections[0]["corrected_value"] == "John"
        assert corrections[0]["correction_source"] == "llm"
        assert corrections[1]["field_name"] == "email"

    def test_get_field_corrections_empty_for_no_corrections(self, db, job_id):
        corrections = get_field_corrections(db, job_id)
        assert corrections == []

    def test_get_field_corrections_ordered_by_created_at(self, db, job_id):
        log_field_correction(db, job_id, "a_field", "x", "y", "llm")
        log_field_correction(db, job_id, "b_field", "x", "y", "validation")
        log_field_correction(db, job_id, "c_field", "x", "y", "manual")

        corrections = get_field_corrections(db, job_id)
        assert [c["field_name"] for c in corrections] == [
            "a_field",
            "b_field",
            "c_field",
        ]


# ---------------------------------------------------------------------------
# Job metrics tests
# ---------------------------------------------------------------------------


class TestJobMetrics:
    def test_ensure_job_metrics_creates_row(self, db, job_id):
        ensure_job_metrics(db, job_id)
        row = db.execute("SELECT * FROM job_metrics WHERE job_id = ?", (job_id,)).fetchone()
        assert row is not None
        assert row["total_fields"] == 0
        assert row["field_error_rate"] == 0.0

    def test_ensure_job_metrics_is_idempotent(self, db, job_id):
        ensure_job_metrics(db, job_id)
        ensure_job_metrics(db, job_id)  # should not raise
        rows = db.execute("SELECT COUNT(*) as cnt FROM job_metrics WHERE job_id = ?", (job_id,)).fetchone()
        assert rows["cnt"] == 1

    def test_update_job_metrics_sets_values(self, db, job_id):
        ensure_job_metrics(db, job_id)
        update_job_metrics(db, job_id, total_fields=20, fields_corrected=4)
        m = get_job_metrics(db, job_id)
        assert m["total_fields"] == 20
        assert m["fields_corrected"] == 4

    def test_update_job_metrics_auto_calculates_error_rate(self, db, job_id):
        ensure_job_metrics(db, job_id)
        update_job_metrics(db, job_id, total_fields=10, fields_corrected=3)
        m = get_job_metrics(db, job_id)
        assert abs(m["field_error_rate"] - 0.3) < 1e-6

    def test_update_job_metrics_error_rate_zero_when_no_fields(self, db, job_id):
        ensure_job_metrics(db, job_id)
        update_job_metrics(db, job_id, total_fields=0, fields_corrected=0)
        m = get_job_metrics(db, job_id)
        assert m["field_error_rate"] == 0.0

    def test_get_job_metrics_returns_none_if_missing(self, db, job_id):
        result = get_job_metrics(db, job_id)
        assert result is None

    def test_get_job_metrics_returns_dict_if_exists(self, db, job_id):
        ensure_job_metrics(db, job_id)
        result = get_job_metrics(db, job_id)
        assert isinstance(result, dict)
        assert "job_id" in result

    def test_get_all_job_metrics_returns_joined_data(self, db):
        j1 = add_job(db, url="https://boards.greenhouse.io/co1/jobs/1")
        j2 = add_job(db, url="https://boards.greenhouse.io/co2/jobs/2")
        update_status(db, j1, "submitted", board="greenhouse", company="Co1")
        update_status(db, j2, "stopped", board="lever", company="Co2")
        ensure_job_metrics(db, j1)
        ensure_job_metrics(db, j2)
        update_job_metrics(db, j1, total_fields=10, fields_corrected=1)
        update_job_metrics(db, j2, total_fields=5, fields_corrected=2)

        all_metrics = get_all_job_metrics(db)
        assert len(all_metrics) == 2
        # Should have job-level fields from the join
        assert all("company" in m for m in all_metrics)

    def test_get_all_job_metrics_filter_by_board(self, db):
        j1 = add_job(db, url="https://boards.greenhouse.io/co1/jobs/1")
        j2 = add_job(db, url="https://jobs.lever.co/co2/abc")
        update_status(db, j1, "submitted", board="greenhouse")
        update_status(db, j2, "submitted", board="lever")
        ensure_job_metrics(db, j1)
        ensure_job_metrics(db, j2)

        gh_only = get_all_job_metrics(db, board="greenhouse")
        assert len(gh_only) == 1
        assert gh_only[0]["board"] == "greenhouse"

    def test_get_all_job_metrics_filter_by_status(self, db):
        j1 = add_job(db, url="https://boards.greenhouse.io/co1/jobs/1")
        j2 = add_job(db, url="https://boards.greenhouse.io/co2/jobs/2")
        update_status(db, j1, "submitted", board="greenhouse")
        update_status(db, j2, "stopped", board="greenhouse")
        ensure_job_metrics(db, j1)
        ensure_job_metrics(db, j2)

        submitted = get_all_job_metrics(db, status="submitted")
        assert len(submitted) == 1


# ---------------------------------------------------------------------------
# Aggregate query tests
# ---------------------------------------------------------------------------


class TestAggregates:
    def test_get_summary_stats_empty_db(self, db):
        stats = get_summary_stats(db)
        assert stats["total"] == 0
        assert stats["submitted"] == 0
        assert stats["stopped"] == 0
        assert stats["needs_attention"] == 0
        assert stats["success_rate"] == 0.0
        assert stats["failure_rate"] == 0.0
        assert stats["avg_error_rate"] == 0.0
        assert stats["avg_duration_ms"] == 0.0
        assert stats["jobs_with_interventions"] == 0
        assert stats["intervention_rate"] == 0.0

    def test_get_summary_stats_with_jobs(self, db):
        j1 = add_job(db, url="https://boards.greenhouse.io/co1/jobs/1")
        j2 = add_job(db, url="https://boards.greenhouse.io/co2/jobs/2")
        j3 = add_job(db, url="https://boards.greenhouse.io/co3/jobs/3")
        update_status(db, j1, "submitted")
        update_status(db, j2, "submitted")
        update_status(db, j3, "stopped")

        ensure_job_metrics(db, j1)
        ensure_job_metrics(db, j2)
        ensure_job_metrics(db, j3)
        update_job_metrics(db, j1, total_fields=10, fields_corrected=2, total_duration_ms=1000)
        update_job_metrics(db, j2, total_fields=10, fields_corrected=0, total_duration_ms=2000)
        update_job_metrics(db, j3, total_fields=10, fields_corrected=5, total_duration_ms=500, manual_interventions=1)

        stats = get_summary_stats(db)
        assert stats["total"] == 3
        assert stats["submitted"] == 2
        assert stats["stopped"] == 1
        # success_rate = 2/3
        assert abs(stats["success_rate"] - 2 / 3) < 0.01
        # failure_rate = 1/3
        assert abs(stats["failure_rate"] - 1 / 3) < 0.01
        assert stats["jobs_with_interventions"] == 1

    def test_get_phase_avg_durations_empty(self, db):
        result = get_phase_avg_durations(db)
        assert result == {}

    def test_get_phase_avg_durations_with_data(self, db, job_id):
        # Insert two "resolving" phases with known durations
        db.execute(
            "INSERT INTO job_phase_durations (job_id, phase, duration_ms) VALUES (?, ?, ?)",
            (job_id, "resolving", 100),
        )
        db.execute(
            "INSERT INTO job_phase_durations (job_id, phase, duration_ms) VALUES (?, ?, ?)",
            (job_id, "resolving", 200),
        )
        db.execute(
            "INSERT INTO job_phase_durations (job_id, phase, duration_ms) VALUES (?, ?, ?)",
            (job_id, "generating", 500),
        )
        db.commit()

        avgs = get_phase_avg_durations(db)
        assert abs(avgs["resolving"] - 150.0) < 1e-6
        assert abs(avgs["generating"] - 500.0) < 1e-6

    def test_get_board_error_rates_empty(self, db):
        result = get_board_error_rates(db)
        assert result == {}

    def test_get_board_error_rates_with_data(self, db):
        j1 = add_job(db, url="https://boards.greenhouse.io/co1/jobs/1")
        j2 = add_job(db, url="https://boards.greenhouse.io/co2/jobs/2")
        update_status(db, j1, "submitted", board="greenhouse")
        update_status(db, j2, "submitted", board="greenhouse")
        ensure_job_metrics(db, j1)
        ensure_job_metrics(db, j2)
        update_job_metrics(db, j1, total_fields=10, fields_corrected=2)  # 0.2
        update_job_metrics(db, j2, total_fields=10, fields_corrected=4)  # 0.4

        rates = get_board_error_rates(db)
        assert "greenhouse" in rates
        assert abs(rates["greenhouse"] - 0.3) < 1e-6

    def test_get_jobs_processed_counts_returns_time_windows(self, db):
        counts = get_jobs_processed_counts(db)
        assert "last_1h" in counts
        assert "last_24h" in counts
        assert "last_7d" in counts
        assert "all_time" in counts
        # Empty db
        assert counts["all_time"] == 0

    def test_get_jobs_processed_counts_with_data(self, db):
        j1 = add_job(db, url="https://boards.greenhouse.io/co1/jobs/1")
        j2 = add_job(db, url="https://boards.greenhouse.io/co2/jobs/2")
        update_status(db, j1, "submitted")
        update_status(db, j2, "stopped")

        counts = get_jobs_processed_counts(db)
        # Both are terminal
        assert counts["all_time"] == 2
        # Recently created, so should appear in all windows
        assert counts["last_1h"] == 2
        assert counts["last_24h"] == 2
        assert counts["last_7d"] == 2


# ---------------------------------------------------------------------------
# parse_autofill_report tests
# ---------------------------------------------------------------------------


class TestParseAutofillReport:
    def test_basic_field_counts(self):
        from pipeline_orchestrator import parse_autofill_report

        report = {
            "fields": [
                {"name": "first_name", "status": "filled"},
                {"name": "last_name", "status": "filled"},
                {"name": "cover_letter", "status": "skipped_not_found"},
                {"name": "phone", "status": "filled"},
            ],
            "unknown_questions": [
                {"question": "What is your favorite color?"},
            ],
        }
        result = parse_autofill_report(report)
        assert result["total_fields"] == 4
        assert result["filled_fields"] == 3
        assert result["skipped_fields"] == 1
        assert result["unknown_questions"] == 1

    def test_empty_report(self):
        from pipeline_orchestrator import parse_autofill_report

        report = {}
        result = parse_autofill_report(report)
        assert result["total_fields"] == 0
        assert result["filled_fields"] == 0
        assert result["skipped_fields"] == 0
        assert result["unknown_questions"] == 0

    def test_no_unknown_questions(self):
        from pipeline_orchestrator import parse_autofill_report

        report = {
            "fields": [
                {"name": "email", "status": "filled"},
            ],
        }
        result = parse_autofill_report(report)
        assert result["total_fields"] == 1
        assert result["filled_fields"] == 1
        assert result["skipped_fields"] == 0
        assert result["unknown_questions"] == 0

    def test_mixed_statuses(self):
        from pipeline_orchestrator import parse_autofill_report

        report = {
            "fields": [
                {"name": "a", "status": "filled"},
                {"name": "b", "status": "skipped_not_found"},
                {"name": "c", "status": "error"},
                {"name": "d"},  # no status key
            ],
            "unknown_questions": [],
        }
        result = parse_autofill_report(report)
        assert result["total_fields"] == 4
        assert result["filled_fields"] == 1
        assert result["skipped_fields"] == 1
        assert result["unknown_questions"] == 0


# ---------------------------------------------------------------------------
# diff_draft_fields tests (Task 9b)
# ---------------------------------------------------------------------------


class TestDiffDraftFields:
    def test_detects_changed_fields(self):
        from pipeline_orchestrator import diff_draft_fields

        original = [
            {"field_name": "first_name", "label": "First Name", "value": "Jon"},
            {"field_name": "last_name", "label": "Last Name", "value": "Doe"},
            {"field_name": "email", "label": "Email", "value": "old@example.com"},
        ]
        edited = [
            {"field_name": "first_name", "label": "First Name", "value": "John"},
            {"field_name": "last_name", "label": "Last Name", "value": "Doe"},
            {"field_name": "email", "label": "Email", "value": "new@example.com"},
        ]
        changes = diff_draft_fields(original, edited)
        assert len(changes) == 2
        names = {c["field_name"] for c in changes}
        assert names == {"first_name", "email"}
        # Check structure
        fn_change = [c for c in changes if c["field_name"] == "first_name"][0]
        assert fn_change["original"] == "Jon"
        assert fn_change["corrected"] == "John"
        assert fn_change["label"] == "First Name"

    def test_no_changes_returns_empty(self):
        from pipeline_orchestrator import diff_draft_fields

        fields = [
            {"field_name": "first_name", "label": "First Name", "value": "John"},
            {"field_name": "email", "label": "Email", "value": "john@example.com"},
        ]
        changes = diff_draft_fields(fields, fields)
        assert changes == []


# ---------------------------------------------------------------------------
# detect_content_edits tests (Task 10d)
# ---------------------------------------------------------------------------


class TestDetectContentEdits:
    def test_resume_json_changes_detected(self):
        from pipeline_orchestrator import detect_content_edits

        original = {
            "tagline": "Old tagline",
            "summary": "Same summary",
            "page_break_before": False,
            "positions": {
                "Acme Corp": [
                    {"text": "Led team of 5", "selected": True},
                    {"text": "Shipped v2", "selected": False},
                ],
            },
        }
        current = {
            "tagline": "New tagline",
            "summary": "Same summary",
            "page_break_before": False,
            "positions": {
                "Acme Corp": [
                    {"text": "Led team of 5", "selected": True},
                    {"text": "Shipped v3", "selected": True},
                ],
            },
        }
        changes = detect_content_edits(original, current, "resume_content.json")
        field_names = {c["field_name"] for c in changes}
        assert "tagline" in field_names
        assert "positions.Acme Corp[1]" in field_names
        # summary and page_break_before unchanged
        assert "summary" not in field_names
        assert "page_break_before" not in field_names

    def test_no_changes_returns_empty(self):
        from pipeline_orchestrator import detect_content_edits

        data = {
            "tagline": "Same",
            "summary": "Same",
            "page_break_before": False,
            "positions": {
                "Acme": [{"text": "bullet", "selected": True}],
            },
        }
        changes = detect_content_edits(data, data, "resume_content.json")
        assert changes == []

    def test_cover_letter_change_detected(self):
        from pipeline_orchestrator import detect_content_edits

        original = "Dear Hiring Manager, I am excited..."
        current = "Dear Hiring Manager, I am thrilled..."
        changes = detect_content_edits(original, current, "cover_letter_text.txt")
        assert len(changes) == 1
        assert changes[0]["field_name"] == "cover_letter_text"
        assert "excited" in changes[0]["original"]
        assert "thrilled" in changes[0]["corrected"]


# ---------------------------------------------------------------------------
# Integration test – full job lifecycle (Task 14)
# ---------------------------------------------------------------------------


class TestTelemetryIntegration:
    """Simulates a complete job lifecycle and verifies all telemetry metrics."""

    def test_full_job_lifecycle(self, tmp_path):
        # 1. Create a temp DB and initialize it
        db_path = tmp_path / "integration_telemetry.db"
        conn = init_db(db_path)

        try:
            # 2. Add a job
            jid = add_job(conn, url="https://boards.greenhouse.io/acme/jobs/42")

            # 3. Ensure job_metrics exists
            ensure_job_metrics(conn, jid)
            assert get_job_metrics(conn, jid) is not None

            # 4. Phase 1 – resolve
            p1 = start_phase(conn, jid, "resolve")
            end_phase(conn, p1, exit_code=0)

            # 5. Phase 2 – generate
            p2 = start_phase(conn, jid, "generate")
            end_phase(conn, p2, exit_code=0)

            # 6. Phase 3 – submit
            p3 = start_phase(conn, jid, "submit")
            end_phase(conn, p3, exit_code=0)

            # 7. Update job_metrics with phase_count, total_fields, fields_corrected
            update_job_metrics(
                conn,
                jid,
                phase_count=3,
                total_fields=20,
                fields_corrected=3,
            )

            # 8. Log field corrections: 2 corrections (draft_review + content_edit)
            log_field_correction(
                conn,
                jid,
                field_name="first_name",
                original_value="Jon",
                corrected_value="John",
                correction_source="draft_review",
            )
            log_field_correction(
                conn,
                jid,
                field_name="tagline",
                original_value="Old tagline",
                corrected_value="New tagline",
                correction_source="content_edit",
            )

            # 9. Update manual_interventions
            update_job_metrics(conn, jid, manual_interventions=2)

            # 10. Mark job as submitted
            update_status(conn, jid, "submitted", board="greenhouse", company="Acme")

            # ------------------------------------------------------------------
            # 11. Verify everything
            # ------------------------------------------------------------------

            # 11a. get_job_metrics returns correct values
            m = get_job_metrics(conn, jid)
            assert m["total_fields"] == 20
            assert m["fields_corrected"] == 3
            assert abs(m["field_error_rate"] - 0.15) < 1e-6
            assert m["manual_interventions"] == 2
            assert m["phase_count"] == 3

            # 11b. Phase durations: 3 rows with correct phase names
            phase_rows = conn.execute(
                "SELECT * FROM job_phase_durations WHERE job_id = ? ORDER BY id",
                (jid,),
            ).fetchall()
            assert len(phase_rows) == 3
            assert [r["phase"] for r in phase_rows] == ["resolve", "generate", "submit"]
            # All durations should be non-negative
            for row in phase_rows:
                assert row["duration_ms"] is not None
                assert row["duration_ms"] >= 0

            # 11c. Field corrections: 2 rows
            corrections = get_field_corrections(conn, jid)
            assert len(corrections) == 2

            # 11d. get_summary_stats: total >= 1, submitted >= 1
            stats = get_summary_stats(conn)
            assert stats["total"] >= 1
            assert stats["submitted"] >= 1

            # 11e. get_jobs_processed_counts: all_time >= 1
            counts = get_jobs_processed_counts(conn)
            assert counts["all_time"] >= 1

            # 11f. get_phase_avg_durations: resolve key exists
            avgs = get_phase_avg_durations(conn)
            assert "resolve" in avgs

        finally:
            conn.close()
