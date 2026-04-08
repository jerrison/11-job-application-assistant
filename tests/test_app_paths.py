import importlib.util
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


class AppPathsTests(unittest.TestCase):
    def test_repo_mode_keeps_runtime_files_in_repo_root(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            app_paths = load_module("app_paths", "scripts/app_paths.py")

        self.assertFalse(app_paths.is_packaged_runtime())
        self.assertEqual(app_paths.code_root(), PROJECT_ROOT)
        self.assertEqual(app_paths.app_home(), PROJECT_ROOT)
        self.assertEqual(app_paths.jobs_db_path(), PROJECT_ROOT / "jobs.db")
        self.assertEqual(app_paths.output_root(), PROJECT_ROOT / "output")
        self.assertEqual(app_paths.materials_root(), PROJECT_ROOT)
        self.assertEqual(
            app_paths.env_file_paths(),
            [PROJECT_ROOT / ".env", PROJECT_ROOT / ".env.local"],
        )
        self.assertEqual(app_paths.browser_root(), Path.home() / ".job-assets")

    def test_app_home_override_moves_runtime_files_under_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "runtime-home"
            with mock.patch.dict(os.environ, {"JOB_ASSETS_APP_HOME": str(runtime_root)}, clear=True):
                app_paths = load_module("app_paths", "scripts/app_paths.py")

                self.assertFalse(app_paths.is_packaged_runtime())
                self.assertEqual(app_paths.app_home(), runtime_root)
                self.assertEqual(app_paths.jobs_db_path(), runtime_root / "jobs.db")
                self.assertEqual(app_paths.output_root(), runtime_root / "output")
                self.assertEqual(app_paths.materials_root(), runtime_root)
                self.assertEqual(
                    app_paths.env_file_paths(),
                    [runtime_root / ".env", runtime_root / ".env.local"],
                )
                self.assertEqual(app_paths.browser_root(), runtime_root / ".job-assets")

    def test_runtime_home_override_updates_material_and_state_helpers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "runtime-home"
            with mock.patch.dict(os.environ, {"JOB_ASSETS_APP_HOME": str(runtime_root)}, clear=True):
                app_paths = load_module("app_paths_runtime_helpers", "scripts/app_paths.py")

                self.assertEqual(app_paths.material_path("master_resume.md"), runtime_root / "master_resume.md")
                self.assertEqual(
                    app_paths.sync_state_path(".master_resume_sync_state.json"),
                    runtime_root / ".master_resume_sync_state.json",
                )
                self.assertEqual(app_paths.tmp_root(), runtime_root / "tmp")

    def test_packaged_runtime_defaults_to_application_support(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            with mock.patch.dict(
                os.environ,
                {
                    "HOME": str(home),
                    "JOB_ASSETS_PACKAGED": "1",
                    "JOB_ASSETS_CODE_ROOT": str(PROJECT_ROOT),
                },
                clear=True,
            ):
                app_paths = load_module("app_paths", "scripts/app_paths.py")

                self.assertTrue(app_paths.is_packaged_runtime())
                self.assertEqual(app_paths.code_root(), PROJECT_ROOT)
                self.assertEqual(
                    app_paths.app_home(),
                    home / "Library" / "Application Support" / "Job Assets",
                )


if __name__ == "__main__":
    unittest.main()
