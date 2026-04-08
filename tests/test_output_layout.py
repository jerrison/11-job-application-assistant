import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class OutputLayoutTests(unittest.TestCase):
    def test_migrate_role_output_layout_moves_known_files_into_subfolders(self):
        layout = load_module("output_layout", "scripts/output_layout.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "jd_parsed.json").write_text("{}", encoding="utf-8")
            (out_dir / "resume_content.json").write_text("{}", encoding="utf-8")
            (out_dir / "cover_letter_text.txt").write_text("Dear Hiring Team,", encoding="utf-8")
            (out_dir / "Candidate Name Resume - Acme.pdf").write_bytes(b"%PDF-1.4")
            (out_dir / "application_answers.json").write_text("{}", encoding="utf-8")
            (out_dir / "application_answers_fallback_raw.txt").write_text("fallback", encoding="utf-8")
            (out_dir / "pending_user_input.json").write_text("{}", encoding="utf-8")
            (out_dir / "ashby_autofill_pages").mkdir()
            (out_dir / "ashby_autofill_pages" / "page_01.png").write_bytes(b"png")

            moved = layout.migrate_role_output_layout(out_dir)

            self.assertTrue(moved)
            self.assertTrue((out_dir / "content" / "jd_parsed.json").exists())
            self.assertTrue((out_dir / "content" / "resume_content.json").exists())
            self.assertTrue((out_dir / "content" / "cover_letter_text.txt").exists())
            self.assertTrue((out_dir / "documents" / "Candidate Name Resume - Acme.pdf").exists())
            self.assertTrue((out_dir / "submit" / "application_answers.json").exists())
            self.assertTrue((out_dir / "submit" / "application_answers_fallback_raw.txt").exists())
            self.assertTrue((out_dir / "submit" / "pending_user_input.json").exists())
            self.assertTrue((out_dir / "submit" / "ashby_autofill_pages" / "page_01.png").exists())

    def test_find_role_file_prefers_bucketed_paths_with_root_fallback(self):
        layout = load_module("output_layout", "scripts/output_layout.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            layout.ensure_role_output_dirs(out_dir)
            (out_dir / "content" / "resume_content.json").write_text("{}", encoding="utf-8")
            (out_dir / "Candidate Name Resume - Acme.pdf").write_bytes(b"%PDF-1.4")

            resume_content = layout.find_role_file(out_dir, "resume_content.json", bucket="content")
            legacy_resume = layout.glob_role_files(out_dir, "*Resume*.pdf", bucket="documents")

            self.assertEqual(resume_content, out_dir / "content" / "resume_content.json")
            self.assertEqual(legacy_resume, [out_dir / "Candidate Name Resume - Acme.pdf"])

    def test_create_reapply_submit_dir_sets_new_active_submit_directory(self):
        layout = load_module("output_layout", "scripts/output_layout.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            layout.ensure_role_output_dirs(out_dir)

            reapply_dir = layout.create_reapply_submit_dir(out_dir)

            self.assertTrue(reapply_dir.exists())
            self.assertNotEqual(reapply_dir.name, "submit")
            self.assertEqual(layout.role_submit_dir(out_dir), reapply_dir)
            self.assertEqual(
                (out_dir / layout.ACTIVE_SUBMIT_DIR_POINTER).read_text(encoding="utf-8").strip(),
                reapply_dir.name,
            )
            self.assertEqual(
                layout.role_submit_path(out_dir, "application_submission_result.json"),
                reapply_dir / "application_submission_result.json",
            )

    def test_ensure_reapply_submit_dir_reuses_existing_active_reapply_directory(self):
        layout = load_module("output_layout", "scripts/output_layout.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            layout.ensure_role_output_dirs(out_dir)
            existing = layout.create_reapply_submit_dir(out_dir)

            reused = layout.ensure_reapply_submit_dir(out_dir)

            self.assertEqual(reused, existing)
            self.assertEqual(
                (out_dir / layout.ACTIVE_SUBMIT_DIR_POINTER).read_text(encoding="utf-8").strip(),
                existing.name,
            )

    def test_existing_submit_dirs_prefers_active_then_default_then_other_attempts(self):
        layout = load_module("output_layout", "scripts/output_layout.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            layout.ensure_role_output_dirs(out_dir)
            older_reapply = out_dir / "submit-20260313T170000Z"
            older_reapply.mkdir()
            newer_reapply = out_dir / "submit-20260313T180000Z"
            newer_reapply.mkdir()
            layout.set_active_submit_dir(out_dir, newer_reapply.name)

            ordered = layout.existing_submit_dirs(out_dir)

            self.assertEqual(
                ordered,
                [
                    newer_reapply,
                    out_dir / "submit",
                    older_reapply,
                ],
            )

    def test_current_submit_dir_name_for_reads_prefers_newer_default_submit_over_stale_active_pointer(self):
        layout = load_module("output_layout", "scripts/output_layout.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            layout.ensure_role_output_dirs(out_dir)
            stale_active = out_dir / "submit-20260313T180000Z"
            stale_active.mkdir()
            layout.set_active_submit_dir(out_dir, stale_active.name)

            stale_result = stale_active / layout.SUBMISSION_RESULT_JSON
            stale_result.write_text(json.dumps({"status": "unknown"}), encoding="utf-8")

            current_pending = out_dir / "submit" / "pending_user_input.json"
            current_pending.write_text(
                json.dumps({"status": "pending_user_input", "questions": [{"label": "Resume"}]}),
                encoding="utf-8",
            )

            os.utime(stale_result, (1_000_000_000, 1_000_000_000))
            os.utime(current_pending, (1_000_000_100, 1_000_000_100))

            self.assertEqual(layout.current_submit_dir_name_for_reads(out_dir), "submit")
            self.assertEqual(layout.existing_submit_dirs(out_dir)[0], out_dir / "submit")

    def test_active_submit_dir_name_prefers_env_override(self):
        layout = load_module("output_layout", "scripts/output_layout.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            layout.ensure_role_output_dirs(out_dir)
            override_dir = out_dir / "submit-override"
            override_dir.mkdir()

            with mock.patch.dict(os.environ, {layout.ACTIVE_SUBMIT_DIR_ENV: "submit-override"}, clear=False):
                self.assertEqual(layout.role_submit_dir(out_dir), override_dir)
                self.assertEqual(
                    layout.role_submit_path(out_dir, "application_submission_result.json"),
                    override_dir / "application_submission_result.json",
                )

    def test_latest_confirmed_submit_dir_prefers_latest_confirmed_attempt(self):
        layout = load_module("output_layout", "scripts/output_layout.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            layout.ensure_role_output_dirs(out_dir)
            default_submit = out_dir / "submit"
            (default_submit / layout.SUBMISSION_RESULT_JSON).write_text(
                json.dumps({"website_confirmed": False}),
                encoding="utf-8",
            )
            confirmed_submit = out_dir / "submit-20260326T160000Z"
            confirmed_submit.mkdir()
            (confirmed_submit / layout.WEBSITE_CONFIRMATION_JSON).write_text(
                json.dumps({"website_confirmed": True}),
                encoding="utf-8",
            )
            os.utime(default_submit, (1, 1))
            os.utime(confirmed_submit, None)

            self.assertEqual(layout.latest_confirmed_submit_dir(out_dir), confirmed_submit)

    def test_preferred_submit_dir_name_for_post_submit_prefers_confirmed_attempt_without_env_override(self):
        layout = load_module("output_layout", "scripts/output_layout.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            layout.ensure_role_output_dirs(out_dir)
            confirmed_submit = layout.create_reapply_submit_dir(out_dir)
            (confirmed_submit / layout.SUBMISSION_RESULT_JSON).write_text(
                json.dumps({"website_confirmed": True}),
                encoding="utf-8",
            )
            layout.set_active_submit_dir(out_dir, layout.SUBMIT_DIRNAME)

            self.assertEqual(layout.preferred_submit_dir_name_for_post_submit(out_dir), confirmed_submit.name)

    def test_preferred_submit_dir_name_for_post_submit_yields_to_explicit_env_override(self):
        layout = load_module("output_layout", "scripts/output_layout.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            layout.ensure_role_output_dirs(out_dir)
            confirmed_submit = layout.create_reapply_submit_dir(out_dir)
            (confirmed_submit / layout.SUBMISSION_RESULT_JSON).write_text(
                json.dumps({"website_confirmed": True}),
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {layout.ACTIVE_SUBMIT_DIR_ENV: confirmed_submit.name}, clear=False):
                self.assertIsNone(layout.preferred_submit_dir_name_for_post_submit(out_dir))


if __name__ == "__main__":
    unittest.main()
