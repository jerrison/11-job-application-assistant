import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import unittest


class DraftWebTests(unittest.TestCase):
    """Tests for the FastAPI draft review web interface."""

    def test_list_drafts_endpoint(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed — install with: uv pip install -e '.[web]'")

        from draft_web import create_app

        client = TestClient(create_app())
        resp = client.get("/api/drafts")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), list)

    def test_dashboard_html(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")

        from draft_web import create_app

        client = TestClient(create_app())
        resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("content-type", ""))
        self.assertIn("Application Drafts", resp.text)

    def test_get_draft_not_found(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")

        from draft_web import create_app

        client = TestClient(create_app())
        resp = client.get("/api/drafts/99999")
        self.assertEqual(resp.status_code, 404)

    def test_get_image_invalid_type(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")

        from draft_web import create_app

        client = TestClient(create_app())
        # Even if job existed, an invalid image type returns 400 or 404
        resp = client.get("/api/drafts/99999/images/invalid")
        self.assertIn(resp.status_code, (400, 404))

    def test_reset_route_and_control_are_exposed(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")

        import draft_web
        from job_db import add_job, init_db

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            db_path = project_root / "jobs.db"
            out_dir = project_root / "output" / "example"
            out_dir.mkdir(parents=True)

            conn = init_db(db_path)
            job_id = add_job(
                conn,
                "https://boards.greenhouse.io/example/jobs/1",
                company="Example",
                role_title="Principal PM",
            )
            conn.execute(
                "UPDATE jobs SET status = 'draft', board = 'greenhouse', output_dir = ? WHERE id = ?",
                (str(out_dir), job_id),
            )
            conn.commit()
            conn.close()

            original_root = draft_web.PROJECT_ROOT
            draft_web.PROJECT_ROOT = project_root
            try:
                with mock.patch("pipeline_orchestrator.reset_job_to_new", return_value=True) as reset_job_to_new:
                    client = TestClient(draft_web.create_app())

                    page = client.get(f"/drafts/{job_id}")
                    resp = client.post(f"/api/drafts/{job_id}/reset")

                self.assertEqual(page.status_code, 200)
                self.assertIn("Reset to New", page.text)
                self.assertIn(f"/api/drafts/{job_id}/reset", page.text)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json(), {"status": "queued", "job_id": job_id})
                reset_job_to_new.assert_called_once()
            finally:
                draft_web.PROJECT_ROOT = original_root

    def test_mark_reviewed_route_is_exposed_and_calls_recorder(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")

        import draft_web
        from job_db import add_job, init_db

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            db_path = project_root / "jobs.db"
            todos_dir = project_root / ".context" / "compound-engineering" / "todos"
            todos_dir.mkdir(parents=True, exist_ok=True)
            out_dir = project_root / "output" / "example"
            out_dir.mkdir(parents=True)
            (out_dir / "draft_summary.png").write_bytes(b"proof")

            results_path = todos_dir / "phase3-results.tsv"
            results_path.write_text(
                "handled_at_utc\tid\tcompany\trole_title\tboard\toutput_dir\toutcome\tissue_id\tevidence_paths\thandled_via\treview_trace_path\tartifact_manifest_path\tproof_generated_at_utc\trepair_wave_fingerprint\tlinear_sync_status\tlinear_sync_payload_path\tnotes\n",
                encoding="utf-8",
            )
            snapshot_path = todos_dir / "phase3-snapshot.tsv"
            snapshot_path.write_text(
                "id\tcompany\trole_title\tboard\toutput_dir\n"
                f"1\tExample\tPrincipal PM\tgreenhouse\t{out_dir}\n",
                encoding="utf-8",
            )
            (todos_dir / "current_backlog_sweep.json").write_text(
                json.dumps(
                    {
                        "run_id": "2026-04-08T10-30-00Z",
                        "phase3_snapshot": str(snapshot_path),
                        "phase3_results": str(results_path),
                        "phase3_started_at_utc": "2026-04-08T10:29:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            conn = init_db(db_path)
            job_id = add_job(
                conn,
                "https://boards.greenhouse.io/example/jobs/1",
                company="Example",
                role_title="Principal PM",
            )
            conn.execute(
                "UPDATE jobs SET status = 'draft', board = 'greenhouse', output_dir = ? WHERE id = ?",
                (str(out_dir), job_id),
            )
            conn.commit()
            conn.close()

            original_root = draft_web.PROJECT_ROOT
            draft_web.PROJECT_ROOT = project_root
            try:
                with mock.patch(
                    "sweep_controller.record_transition",
                    return_value={
                        "id": str(job_id),
                        "outcome": "reviewed_ready",
                        "review_trace_path": str(todos_dir / "trace.json"),
                        "artifact_manifest_path": str(todos_dir / "artifact-manifest.json"),
                        "linear_sync_status": "pending",
                    },
                ) as record_result:
                    client = TestClient(draft_web.create_app())

                    page = client.get(f"/drafts/{job_id}")
                    resp = client.post(f"/api/drafts/{job_id}/mark-reviewed")

                self.assertEqual(page.status_code, 200)
                self.assertIn("Mark Reviewed", page.text)
                self.assertIn(f"/api/drafts/{job_id}/mark-reviewed", page.text)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()["status"], "recorded")
                self.assertEqual(resp.json()["outcome"], "reviewed_ready")
                self.assertEqual(resp.json()["linear_sync_status"], "pending")
                record_result.assert_called_once()
            finally:
                draft_web.PROJECT_ROOT = original_root
