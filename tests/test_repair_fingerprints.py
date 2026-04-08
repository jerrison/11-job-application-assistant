import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from job_db import init_db
from repair_fingerprints import (
    build_repair_fingerprint,
    record_repairable_failure_cluster,
    refresh_active_repair_failure_index,
    upsert_repair_cluster,
    write_repair_cluster_report,
)


def test_build_repair_fingerprint_groups_equivalent_greenhouse_mismatch():
    left = build_repair_fingerprint(
        board="Greenhouse",
        phase="draft_audit",
        failure_type="rendered_audit_mismatch",
        message="Work authorization expected Yes observed No",
        field_labels=["Are you legally authorized to work in the US?"],
    )
    right = build_repair_fingerprint(
        board="greenhouse",
        phase="DRAFT_AUDIT",
        failure_type="rendered_audit_mismatch",
        message="work authorization expected YES observed NO",
        field_labels=["  Are you legally authorized to work in the US?  "],
    )

    assert left == right
    assert left.startswith("greenhouse:draft-audit:rendered-audit-mismatch:")


def test_upsert_repair_cluster_reuses_existing_fingerprint(tmp_path):
    conn = init_db(tmp_path / "jobs.db", check_same_thread=False)
    fingerprint = "greenhouse:draft_audit:rendered_audit_mismatch:work_auth"

    first = upsert_repair_cluster(conn, fingerprint=fingerprint, summary="first", job_id=10)
    second = upsert_repair_cluster(conn, fingerprint=fingerprint, summary="second", job_id=11)

    assert first["id"] == second["id"]
    assert json.loads(second["representative_job_ids"]) == [10, 11]
    assert second["attempt_count"] == 2
    assert second["latest_summary"] == "second"


def test_record_repairable_failure_cluster_writes_report_and_index(tmp_path):
    output_root = tmp_path / "output"
    conn = init_db(tmp_path / "jobs.db", check_same_thread=False)

    cluster = record_repairable_failure_cluster(
        conn,
        job_id=22,
        board="greenhouse",
        phase="draft_audit",
        failure_type="rendered_audit_mismatch",
        summary="Work authorization expected Yes observed No",
        output_root=output_root,
        suggestions=["Re-run the rendered-state audit after applying the board-wide fix."],
    )

    report_path = output_root / "_audit" / "repair_clusters" / f"{cluster['fingerprint']}.md"
    index_path = output_root / "_audit" / "active_repair_failures.md"
    assert report_path.exists()
    assert index_path.exists()
    assert cluster["eligibility"] == "auto_repair_candidate"
    assert cluster["status"] == "open"
    assert cluster["attempt_count"] == 1
    assert cluster["latest_summary"] == "Work authorization expected Yes observed No"
    assert cluster["fingerprint"] in report_path.read_text(encoding="utf-8")
    assert cluster["fingerprint"] in index_path.read_text(encoding="utf-8")


def test_record_repairable_failure_cluster_skips_audit_output_when_output_root_cannot_be_inferred(tmp_path):
    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path, check_same_thread=False)
    outside_output = tmp_path / "outside" / "company" / "role"
    outside_output.mkdir(parents=True)

    cluster = record_repairable_failure_cluster(
        conn,
        job_id=22,
        board="greenhouse",
        phase="draft_audit",
        failure_type="rendered_audit_mismatch",
        summary="Work authorization expected Yes observed No",
        output_dir=outside_output,
    )

    assert cluster["fingerprint"]
    assert not (tmp_path / "_audit").exists()


def test_refresh_active_repair_failure_index_excludes_non_open_clusters(tmp_path):
    output_root = tmp_path / "output"
    write_repair_cluster_report(
        output_root,
        cluster_row={
            "fingerprint": "open-cluster",
            "status": "open",
            "eligibility": "auto_repair_candidate",
            "attempt_count": 1,
            "representative_job_ids": "[1]",
            "latest_summary": "Open cluster summary",
        },
        suggestions=[],
    )
    write_repair_cluster_report(
        output_root,
        cluster_row={
            "fingerprint": "resolved-cluster",
            "status": "resolved",
            "eligibility": "auto_repair_candidate",
            "attempt_count": 1,
            "representative_job_ids": "[2]",
            "latest_summary": "Resolved cluster summary",
        },
        suggestions=[],
    )

    index_path = refresh_active_repair_failure_index(output_root)
    index_text = index_path.read_text(encoding="utf-8")

    assert "open-cluster" in index_text
    assert "resolved-cluster" not in index_text


def test_init_db_migrates_last_repair_cluster_id_to_integer(tmp_path):
    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path, check_same_thread=False)
    conn.execute("INSERT INTO jobs (id, url, status) VALUES (1, 'http://x', 'queued')")
    conn.execute("INSERT INTO job_metrics (job_id, last_repair_cluster_id) VALUES (1, 7)")
    conn.commit()
    conn.close()

    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        PRAGMA foreign_keys=OFF;
        ALTER TABLE job_metrics RENAME TO job_metrics_old;
        CREATE TABLE job_metrics (
            job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
            total_fields INTEGER DEFAULT 0,
            fields_corrected INTEGER DEFAULT 0,
            field_error_rate REAL DEFAULT 0.0,
            manual_interventions INTEGER DEFAULT 0,
            auto_fix_attempts INTEGER DEFAULT 0,
            total_duration_ms INTEGER DEFAULT 0,
            phase_count INTEGER DEFAULT 0,
            retry_count INTEGER DEFAULT 0,
            audit_attempts INTEGER DEFAULT 0,
            audit_failure_count INTEGER DEFAULT 0,
            rendered_audit_failures INTEGER DEFAULT 0,
            last_repair_cluster_id TEXT,
            last_rollout_sha TEXT,
            llm_generated_answers INTEGER DEFAULT 0,
            llm_generated_labels TEXT,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
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
            CAST(last_repair_cluster_id AS TEXT),
            last_rollout_sha,
            llm_generated_answers,
            llm_generated_labels,
            updated_at
        FROM job_metrics_old;
        DROP TABLE job_metrics_old;
        PRAGMA foreign_keys=ON;
        """
    )
    raw.commit()
    raw.close()

    migrated = init_db(db_path, check_same_thread=False)
    type_rows = migrated.execute("PRAGMA table_info(job_metrics)").fetchall()
    repair_cluster_type = next(row[2] for row in type_rows if row[1] == "last_repair_cluster_id")
    metrics = migrated.execute("SELECT last_repair_cluster_id FROM job_metrics WHERE job_id = 1").fetchone()

    assert repair_cluster_type == "INTEGER"
    assert metrics["last_repair_cluster_id"] == 7
