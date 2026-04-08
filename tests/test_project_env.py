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


class ProjectEnvTests(unittest.TestCase):
    def test_load_project_env_prefers_env_local_over_env(self):
        project_env = load_module("project_env", "scripts/project_env.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_file = root / ".env"
            env_local = root / ".env.local"
            env_file.write_text("NOTION_API_TOKEN=base-token\nASSET_LLM_PROVIDER=claude\n", encoding="utf-8")
            env_local.write_text("NOTION_API_TOKEN=local-token\n", encoding="utf-8")

            loaded = project_env.load_project_env(files=[env_file, env_local], environ={})

        self.assertEqual(loaded["NOTION_API_TOKEN"], "local-token")
        self.assertEqual(loaded["ASSET_LLM_PROVIDER"], "claude")

    def test_load_project_env_does_not_override_existing_environment(self):
        project_env = load_module("project_env", "scripts/project_env.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_local = root / ".env.local"
            env_local.write_text("NOTION_API_TOKEN=file-token\n", encoding="utf-8")

            environ = {"NOTION_API_TOKEN": "existing-token"}
            loaded = project_env.load_project_env(files=[env_local], environ=environ)

        self.assertEqual(environ["NOTION_API_TOKEN"], "existing-token")
        self.assertNotIn("NOTION_API_TOKEN", loaded)
        self.assertEqual(loaded["UV_CACHE_DIR"], str(project_env.PROJECT_ROOT / ".uv-cache"))

    def test_parse_env_file_supports_export_prefix(self):
        project_env = load_module("project_env", "scripts/project_env.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            env_local = Path(tmpdir) / ".env.local"
            env_local.write_text("export NOTION_API_TOKEN='quoted-token'\n", encoding="utf-8")

            parsed = project_env.parse_env_file(env_local)

        self.assertEqual(parsed["NOTION_API_TOKEN"], "quoted-token")

    def test_load_project_env_defaults_uv_cache_dir_to_repo_local_path(self):
        project_env = load_module("project_env", "scripts/project_env.py")

        environ: dict[str, str] = {}
        loaded = project_env.load_project_env(files=[], environ=environ)

        expected = str(project_env.PROJECT_ROOT / ".uv-cache")
        self.assertEqual(environ["UV_CACHE_DIR"], expected)
        self.assertEqual(loaded["UV_CACHE_DIR"], expected)

    def test_load_project_env_uses_app_home_override_for_default_files_and_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            (runtime_root / ".env.local").write_text("NOTION_API_TOKEN=runtime-token\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"JOB_ASSETS_APP_HOME": str(runtime_root)}, clear=True):
                project_env = load_module("project_env", "scripts/project_env.py")

                environ: dict[str, str] = {}
                loaded = project_env.load_project_env(environ=environ)

        self.assertEqual(environ["NOTION_API_TOKEN"], "runtime-token")
        self.assertEqual(loaded["NOTION_API_TOKEN"], "runtime-token")
        self.assertEqual(environ["UV_CACHE_DIR"], str(runtime_root / ".uv-cache"))
        self.assertEqual(loaded["UV_CACHE_DIR"], str(runtime_root / ".uv-cache"))


if __name__ == "__main__":
    unittest.main()
