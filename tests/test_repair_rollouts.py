import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from job_db import init_db
from repair_rollouts import evaluate_rollout, record_active_rollout, refresh_active_repair_rollouts_index


def test_record_active_rollout_persists_metadata_and_writes_markdown_index(tmp_path):
    db_path = tmp_path / "jobs.db"
    output_root = tmp_path / "output"
    with init_db(db_path) as conn:
        conn.execute(
            "INSERT INTO repair_clusters (id, fingerprint, status, eligibility, representative_job_ids, latest_summary) "
            "VALUES (1, 'greenhouse:draft_audit:rendered_audit_mismatch:work-auth', 'open', "
            "'auto_repair_candidate', '[42, 43]', 'Work authorization mismatch')"
        )
        conn.commit()

        rollout_id = record_active_rollout(
            conn,
            cluster_id=1,
            commit_sha="abc1234",
            fingerprint="greenhouse:draft_audit:rendered_audit_mismatch:work-auth",
            touched_files=["scripts/autofill_greenhouse.py", "scripts/repair_supervisor.py"],
            monitored_job_ids=[42, 43],
            output_root=output_root,
        )

        rollout_row = conn.execute("SELECT * FROM repair_rollouts WHERE id = ?", (rollout_id,)).fetchone()
        assert rollout_row is not None
        assert rollout_row["status"] == "active"

        baseline_metrics = json.loads(rollout_row["baseline_metrics_json"])
        assert baseline_metrics["fingerprint"] == "greenhouse:draft_audit:rendered_audit_mismatch:work-auth"
        assert baseline_metrics["board"] == "greenhouse"
        assert baseline_metrics["phase"] == "draft_audit"
        assert baseline_metrics["monitored_job_ids"] == [42, 43]

        markdown_path = output_root / "_audit" / "active_repair_rollouts.md"
        assert markdown_path.exists()
        markdown = markdown_path.read_text(encoding="utf-8")
        assert "abc1234" in markdown
        assert "greenhouse:draft_audit:rendered_audit_mismatch:work-auth" in markdown


def test_evaluate_rollout_requests_pause_when_fingerprint_reappears(tmp_path):
    db_path = tmp_path / "jobs.db"
    output_root = tmp_path / "output"
    with init_db(db_path) as conn:
        conn.execute(
            "INSERT INTO repair_clusters (id, fingerprint, status, eligibility, representative_job_ids, latest_summary) "
            "VALUES (1, 'greenhouse:draft_audit:rendered_audit_mismatch:work-auth', 'open', "
            "'auto_repair_candidate', '[42, 43]', 'Work authorization mismatch')"
        )
        conn.commit()

        rollout_id = record_active_rollout(
            conn,
            cluster_id=1,
            commit_sha="abc1234",
            fingerprint="greenhouse:draft_audit:rendered_audit_mismatch:work-auth",
            touched_files=["scripts/autofill_greenhouse.py", "scripts/repair_supervisor.py"],
            monitored_job_ids=[42, 43],
            output_root=output_root,
        )
        rollout = dict(conn.execute("SELECT * FROM repair_rollouts WHERE id = ?", (rollout_id,)).fetchone())

        conn.execute("UPDATE repair_clusters SET updated_at = '2099-01-01 00:00:00' WHERE id = 1")
        conn.commit()

        evaluation = evaluate_rollout(conn, rollout, confirmation=False)
        assert evaluation.action == "pause"
        assert evaluation.reason == "fingerprint_recurred"
        assert evaluation.post_fix_metrics["fingerprint_recurrences"] == 1


def test_refresh_active_repair_rollouts_index_reports_empty_state(tmp_path):
    output_root = tmp_path / "output"
    markdown_path = refresh_active_repair_rollouts_index(output_root=output_root, rollouts=[])

    assert markdown_path.exists()
    markdown = markdown_path.read_text(encoding="utf-8")
    assert markdown.rstrip().endswith("No active repair rollouts.")
