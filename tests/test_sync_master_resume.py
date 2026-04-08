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


class SyncMasterResumeTests(unittest.TestCase):
    def test_module_paths_follow_runtime_home_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "runtime-home"
            with mock.patch.dict(os.environ, {"JOB_ASSETS_APP_HOME": str(runtime_root)}, clear=True):
                sync_master_resume = load_module("sync_master_resume_runtime_home", "scripts/sync_master_resume.py")

            self.assertEqual(sync_master_resume.OUTPUT_MD, runtime_root / "master_resume.md")
            self.assertEqual(sync_master_resume.STATE_JSON, runtime_root / ".master_resume_sync_state.json")

    def test_main_requires_configured_source_url(self):
        sync_master_resume = load_module("sync_master_resume", "scripts/sync_master_resume.py")
        previous = os.environ.pop(sync_master_resume.MASTER_RESUME_SOURCE_URL_ENV, None)
        try:
            with mock.patch.object(sync_master_resume, "fetch_resume_text", side_effect=AssertionError("should not fetch")):
                with self.assertRaises(RuntimeError):
                    sync_master_resume.main()
        finally:
            if previous is not None:
                os.environ[sync_master_resume.MASTER_RESUME_SOURCE_URL_ENV] = previous

    def test_sync_if_stale_skips_without_configured_source_url(self):
        sync_master_resume = load_module("sync_master_resume", "scripts/sync_master_resume.py")
        previous = os.environ.pop(sync_master_resume.MASTER_RESUME_SOURCE_URL_ENV, None)
        try:
            with mock.patch.object(sync_master_resume, "main", side_effect=AssertionError("should not sync")):
                assert sync_master_resume.sync_if_stale(max_age_seconds=0) is False
        finally:
            if previous is not None:
                os.environ[sync_master_resume.MASTER_RESUME_SOURCE_URL_ENV] = previous
