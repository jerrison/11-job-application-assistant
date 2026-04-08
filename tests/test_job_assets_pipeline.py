import argparse
import importlib.util
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class JobAssetsPipelineTests(unittest.TestCase):
    def test_default_provider_defaults_to_openai_without_env(self):
        pipeline = load_module("job_assets_pipeline", "scripts/job_assets_pipeline.py")

        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(pipeline.default_provider(), "openai")

    def test_default_provider_prefers_chain_when_provider_chain_is_configured(self):
        pipeline = load_module("job_assets_pipeline", "scripts/job_assets_pipeline.py")

        with mock.patch.dict(
            "os.environ",
            {"ASSET_LLM_PROVIDER": "openai", "ASSET_LLM_PROVIDER_CHAIN": "openai,gemini,claude"},
            clear=False,
        ):
            self.assertEqual(pipeline.default_provider(), "chain")

    def test_default_provider_falls_back_to_active_provider_without_chain(self):
        pipeline = load_module("job_assets_pipeline", "scripts/job_assets_pipeline.py")

        with mock.patch.dict(
            "os.environ",
            {"ASSET_LLM_PROVIDER": "openai", "ASSET_LLM_PROVIDER_CHAIN": ""},
            clear=False,
        ):
            self.assertEqual(pipeline.default_provider(), "openai")

    def test_build_apply_command_includes_meta_capture_and_skip_sync(self):
        pipeline = load_module("job_assets_pipeline", "scripts/job_assets_pipeline.py")
        args = argparse.Namespace(
            provider="codex",
            skip_sync=True,
            jd_source="tmp/jd.md",
            company="acme",
            role="senior-pm",
        )

        command = pipeline.build_apply_command(args, "/tmp/meta-path.txt")

        self.assertEqual(
            command,
            [
                "bash",
                str(PROJECT_ROOT / "apply.sh"),
                "--provider",
                "codex",
                "--skip-sync",
                "--meta-path-file",
                "/tmp/meta-path.txt",
                "tmp/jd.md",
                "acme",
                "senior-pm",
            ],
        )

    def test_build_submit_command_passes_provider_and_browser_flags(self):
        pipeline = load_module("job_assets_pipeline", "scripts/job_assets_pipeline.py")
        args = argparse.Namespace(
            payload_only=False,
            headless=True,
            submit=True,
            reapply=True,
            browser_provider="steel",
            provider="claude",
        )

        with mock.patch.object(
            pipeline,
            "python_script_command",
            return_value=["python", str(PROJECT_ROOT / "scripts" / "submit_application.py")],
        ):
            command = pipeline.build_submit_command(args, "output/acme/senior-pm")

        self.assertEqual(
            command,
            [
                "python",
                str(PROJECT_ROOT / "scripts" / "submit_application.py"),
                "output/acme/senior-pm",
                "--headless",
                "--submit",
                "--reapply",
                "--browser-provider",
                "steel",
                "--provider",
                "claude",
            ],
        )

    def test_require_provider_accepts_gemini_flash(self):
        """gemini-flash should be accepted as a valid provider."""
        pipeline = load_module("job_assets_pipeline", "scripts/job_assets_pipeline.py")
        with mock.patch("shutil.which", return_value="/usr/bin/gemini"):
            pipeline.require_provider("gemini-flash")  # Should NOT raise

    def test_require_provider_accepts_chain(self):
        pipeline = load_module("job_assets_pipeline", "scripts/job_assets_pipeline.py")
        pipeline.require_provider("chain")

    def test_require_provider_rejects_unknown_provider(self):
        """Unknown providers should raise ValueError."""
        pipeline = load_module("job_assets_pipeline", "scripts/job_assets_pipeline.py")
        with self.assertRaises(ValueError):
            pipeline.require_provider("unknown-provider")

    def test_require_provider_uses_provider_binary_mapping(self):
        """gemini-flash should check for 'gemini' binary, not 'gemini-flash'."""
        pipeline = load_module("job_assets_pipeline", "scripts/job_assets_pipeline.py")
        which_calls = []

        def fake_which(name):
            which_calls.append(name)
            return "/usr/bin/gemini"

        with mock.patch("shutil.which", side_effect=fake_which):
            pipeline.require_provider("gemini-flash")

        self.assertEqual(which_calls, ["gemini"])

    def test_read_meta_capture_loads_reported_metadata_file(self):
        pipeline = load_module("job_assets_pipeline", "scripts/job_assets_pipeline.py")

        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            meta_path = tmp_path / ".pipeline_meta.json"
            capture_path = tmp_path / "meta-path.txt"
            payload = {"company": "acme", "role": "senior-pm", "out_dir": "output/acme/senior-pm"}
            meta_path.write_text(json.dumps(payload), encoding="utf-8")
            capture_path.write_text(str(meta_path), encoding="utf-8")

            result = pipeline.read_meta_capture(str(capture_path))

        self.assertEqual(result, payload)

    def test_run_command_uses_devnull_stdin(self):
        pipeline = load_module("job_assets_pipeline", "scripts/job_assets_pipeline.py")
        completed = mock.Mock(returncode=0)

        with mock.patch.object(pipeline.subprocess, "run", return_value=completed) as run:
            result = pipeline.run_command(["uv", "run", "python", "scripts/submit_application.py"])

        self.assertEqual(result, 0)
        _, kwargs = run.call_args
        self.assertIs(kwargs["stdin"], pipeline.subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
