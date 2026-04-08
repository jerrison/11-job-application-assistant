import argparse
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_cli_module(command_name: str = "job-assets"):
    path = PROJECT_ROOT / "bin" / "job-assets"
    loader = SourceFileLoader(f"job_assets_cli_{command_name.replace('-', '_')}", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    with mock.patch("sys.argv", [command_name]):
        spec.loader.exec_module(module)
    module.COMMAND_NAME = command_name
    return module


class JobAssetsCliTests(unittest.TestCase):
    def test_focused_aerospace_workspace_defaults_to_q_on_macos(self):
        cli = load_cli_module()

        with (
            mock.patch.object(cli.sys, "platform", "darwin"),
            mock.patch.object(cli.shutil, "which", return_value=None),
        ):
            self.assertEqual(cli._focused_aerospace_workspace(environ={}), "Q")

    def test_default_provider_defaults_to_openai_when_env_unset(self):
        cli = load_cli_module()

        with mock.patch.dict(os.environ, {}, clear=True):
            cli.COMMAND_NAME = "job-assets"
            self.assertEqual(cli.default_provider(), "openai")

    def test_application_profile_path_uses_app_home_override(self):
        cli = load_cli_module()

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(
            os.environ, {"JOB_ASSETS_APP_HOME": tmpdir}, clear=True
        ):
            self.assertEqual(cli.application_profile_path(), Path(tmpdir) / "application_profile.md")

    def test_build_parser_accepts_pipeline_command(self):
        cli = load_cli_module()
        parser = cli.build_parser()

        args = parser.parse_args(["pipeline", "--submit", "tmp/jd.md"])

        self.assertEqual(args.command, "pipeline")
        self.assertEqual(args.jd_source, "tmp/jd.md")
        self.assertTrue(args.submit)
        self.assertFalse(args.skip_sync)

    def test_build_parser_accepts_submit_reapply_flag(self):
        cli = load_cli_module()
        parser = cli.build_parser()

        args = parser.parse_args(["submit", "output/acme/senior-pm", "--submit", "--reapply"])

        self.assertEqual(args.command, "submit")
        self.assertEqual(args.target, "output/acme/senior-pm")
        self.assertTrue(args.submit)
        self.assertTrue(args.reapply)

    def test_preprocess_defaults_bare_jd_source_to_pipeline(self):
        cli = load_cli_module()

        argv = cli.preprocess_argv(["https://example.com/jobs/123"])

        self.assertEqual(argv, ["pipeline", "https://example.com/jobs/123"])

    def test_preprocess_defaults_pipeline_when_options_precede_jd_source(self):
        cli = load_cli_module()
        parser = cli.build_parser()

        argv = cli.preprocess_argv(["--provider", "codex", "--submit", "https://example.com/jobs/123"])
        args = parser.parse_args(argv)

        self.assertEqual(
            argv,
            ["pipeline", "--provider", "codex", "--submit", "https://example.com/jobs/123"],
        )
        self.assertEqual(args.command, "pipeline")
        self.assertEqual(args.provider, "codex")
        self.assertTrue(args.submit)
        self.assertEqual(args.jd_source, "https://example.com/jobs/123")

    def test_preprocess_reorders_explicit_command_after_options(self):
        cli = load_cli_module()

        argv = cli.preprocess_argv(["--provider", "codex", "pipeline", "--submit", "tmp/jd.md"])

        self.assertEqual(argv, ["pipeline", "--provider", "codex", "--submit", "tmp/jd.md"])

    def test_build_parser_accepts_batch_max_parallel(self):
        cli = load_cli_module()
        parser = cli.build_parser()

        args = parser.parse_args(["batch", "--provider", "codex", "--max-parallel", "12"])

        self.assertEqual(args.command, "batch")
        self.assertEqual(args.provider, "codex")
        self.assertEqual(args.max_parallel, 12)

    def test_cmd_apply_passes_skip_sync_to_apply_script(self):
        cli = load_cli_module()
        args = argparse.Namespace(
            provider="codex",
            skip_sync=True,
            jd_source="tmp/jd.md",
            company="acme",
            role="senior-pm",
        )

        with mock.patch.object(cli, "run_command", return_value=0) as run_command:
            result = cli.cmd_apply(args)

        self.assertEqual(result, 0)
        run_command.assert_called_once_with(
            [
                "bash",
                str(PROJECT_ROOT / "apply.sh"),
                "--provider",
                "codex",
                "--skip-sync",
                "tmp/jd.md",
                "acme",
                "senior-pm",
            ]
        )

    def test_cmd_pipeline_invokes_pipeline_runner_with_submit_flags(self):
        cli = load_cli_module()
        args = argparse.Namespace(
            provider="claude",
            skip_sync=True,
            browser_provider="steel",
            payload_only=False,
            headless=True,
            submit=True,
            reapply=True,
            jd_source="https://example.com/jobs/123",
            company=None,
            role=None,
        )

        with (
            mock.patch.object(
                cli,
                "python_script_command",
                return_value=["python", str(PROJECT_ROOT / "scripts" / "job_assets_pipeline.py")],
            ),
            mock.patch.object(cli, "run_command", return_value=0) as run_command,
        ):
            result = cli.cmd_pipeline(args)

        self.assertEqual(result, 0)
        run_command.assert_called_once_with(
            [
                "python",
                str(PROJECT_ROOT / "scripts" / "job_assets_pipeline.py"),
                "--provider",
                "claude",
                "--skip-sync",
                "--browser-provider",
                "steel",
                "--headless",
                "--submit",
                "--reapply",
                "https://example.com/jobs/123",
            ]
        )

    def test_cmd_submit_invokes_submit_runner_with_reapply_flag(self):
        cli = load_cli_module()
        args = argparse.Namespace(
            target="output/acme/senior-pm",
            payload_only=False,
            headless=False,
            submit=True,
            reapply=True,
            browser_provider="steel",
            provider="claude",
        )

        with (
            mock.patch.object(
                cli,
                "python_script_command",
                return_value=["python", str(PROJECT_ROOT / "scripts" / "submit_application.py")],
            ),
            mock.patch.object(cli, "run_command", return_value=0) as run_command,
        ):
            result = cli.cmd_submit(args)

        self.assertEqual(result, 0)
        run_command.assert_called_once_with(
            [
                "python",
                str(PROJECT_ROOT / "scripts" / "submit_application.py"),
                "output/acme/senior-pm",
                "--submit",
                "--reapply",
                "--provider",
                "claude",
            ],
            env=mock.ANY,
        )
        _, kwargs = run_command.call_args
        self.assertEqual(kwargs["env"]["JOB_ASSETS_BROWSER_PROVIDER"], "steel")

    def test_cmd_batch_invokes_parallelized_batch_runner(self):
        cli = load_cli_module()
        args = argparse.Namespace(
            provider="claude",
            dry_run=True,
            max_parallel=8,
        )

        with mock.patch.object(cli, "run_command", return_value=0) as run_command:
            result = cli.cmd_batch(args)

        self.assertEqual(result, 0)
        run_command.assert_called_once_with(
            [
                "bash",
                str(PROJECT_ROOT / "batch_apply.sh"),
                "--provider",
                "claude",
                "--max-parallel",
                "8",
                "--dry-run",
            ],
            env=mock.ANY,
        )
        _, kwargs = run_command.call_args
        self.assertEqual(kwargs["env"]["JOB_ASSETS_MAX_PARALLEL"], "8")

    def test_cmd_worker_start_captures_aerospace_workspace_for_background_worker(self):
        cli = load_cli_module()
        tmp_root = PROJECT_ROOT / "tmp-test-worker-root"
        args = argparse.Namespace(action="start", workers=4, headless=True, auto_submit=False)

        class FakeProc:
            pid = 43210

        with (
            mock.patch.object(cli, "REPO_ROOT", tmp_root),
            mock.patch.object(cli, "SCRIPTS_ROOT", tmp_root / "scripts"),
            mock.patch.object(cli, "python_script_command", return_value=["python", "job_worker.py"]),
            mock.patch.object(cli, "_focused_aerospace_workspace", return_value="Q"),
            mock.patch.object(cli.subprocess, "Popen", return_value=FakeProc()) as popen,
            mock.patch.object(Path, "write_text", return_value=None),
        ):
            result = cli.cmd_worker(args)

        self.assertEqual(result, 0)
        _, kwargs = popen.call_args
        self.assertEqual(kwargs["env"]["JOB_ASSETS_AEROSPACE_WORKSPACE"], "Q")
        self.assertEqual(kwargs["cwd"], tmp_root)

    def test_cmd_doctor_reports_effective_provider_defaults(self):
        cli = load_cli_module()
        args = argparse.Namespace()
        stdout = io.StringIO()

        with mock.patch("sys.stdout", stdout), mock.patch("shutil.which", return_value="/usr/local/bin/tool"):
            result = cli.cmd_doctor(args)

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("claude defaults: model=claude-sonnet-4-6 effort=max", output)
        self.assertIn(
            "claude exec mode: permission=auto settings=project,local session_persistence=off slash_commands=off strict_mcp=on",
            output,
        )
        self.assertIn("codex defaults: model=gpt-5.4 reasoning=xhigh", output)
        self.assertIn("codex exec mode: approval=never sandbox=danger-full-access", output)
        self.assertIn(
            "codex exec isolation: temp CODEX_HOME with auth, AGENTS, prompts, skills + config (global MCP servers preserved)",
            output,
        )
        self.assertIn("provider timeout (submit answers, s): 600", output)
        self.assertIn("provider timeout (asset generation, s): 1200", output)
        self.assertIn("default max parallel jobs:", output)
        self.assertIn("claude primary asset timeout before fallback (s): 600", output)
        self.assertIn("claude asset fallback provider: ", output)

    def test_build_parser_accepts_gemini_flash_provider(self):
        cli = load_cli_module()
        parser = cli.build_parser()

        for cmd_args in [
            ["pipeline", "--provider", "gemini-flash", "tmp/jd.md"],
            ["submit", "--provider", "gemini-flash", "output/acme/sr-pm"],
            ["apply", "--provider", "gemini-flash", "tmp/jd.md"],
        ]:
            args = parser.parse_args(cmd_args)
            self.assertEqual(args.provider, "gemini-flash")

    def test_build_parser_accepts_add_saved_portal_mode(self):
        cli = load_cli_module()
        parser = cli.build_parser()

        args = parser.parse_args(["add", "--saved-portal", "trueup", "--priority", "5", "--provider", "codex"])

        self.assertEqual(args.command, "add")
        self.assertEqual(args.saved_portal, "trueup")
        self.assertEqual(args.urls, [])
        self.assertEqual(args.priority, 5)
        self.assertEqual(args.provider, "codex")

    def test_build_parser_accepts_jackandjill_saved_portal_mode(self):
        cli = load_cli_module()
        parser = cli.build_parser()

        args = parser.parse_args(["add", "--saved-portal", "jackandjill"])

        self.assertEqual(args.command, "add")
        self.assertEqual(args.saved_portal, "jackandjill")

    def test_saved_portal_choices_match_registry(self):
        cli = load_cli_module()

        self.assertEqual(
            cli.saved_portal_choices(),
            tuple(spec.key for spec in cli.saved_portal_import.list_saved_portals()),
        )

    def test_cmd_add_dispatches_saved_portal_import(self):
        cli = load_cli_module()
        args = argparse.Namespace(urls=[], saved_portal="trueup", priority=5, provider="codex")
        stdout = io.StringIO()

        with (
            mock.patch("sys.stdout", stdout),
            mock.patch.object(
                cli,
                "_import_saved_portal",
                return_value={
                    "status": "ok",
                    "added": 2,
                    "duplicates": 1,
                    "skipped_unresolved": 3,
                    "errors": 0,
                    "scraped": 6,
                    "resolved": 4,
                    "message": "",
                },
            ) as run_import,
        ):
            result = cli.cmd_add(args)

        self.assertEqual(result, 0)
        run_import.assert_called_once_with("trueup", priority=5, provider="codex")
        output = stdout.getvalue()
        self.assertIn("TrueUp import", output)
        self.assertIn("added=2", output)

    def test_cmd_add_rejects_mixing_saved_portal_and_urls(self):
        cli = load_cli_module()
        args = argparse.Namespace(
            urls=["https://example.com/jobs/123"],
            saved_portal="trueup",
            priority=5,
            provider="codex",
        )
        stderr = io.StringIO()

        with mock.patch("sys.stderr", stderr), mock.patch.object(cli, "_import_saved_portal") as run_import:
            result = cli.cmd_add(args)

        self.assertEqual(result, 2)
        run_import.assert_not_called()
        self.assertIn("linkedin|trueup|jackandjill", stderr.getvalue())

    def test_cmd_add_returns_nonzero_and_prints_message_for_non_ok_saved_portal_import(self):
        cli = load_cli_module()
        args = argparse.Namespace(urls=[], saved_portal="trueup", priority=5, provider="codex")
        stdout = io.StringIO()

        with (
            mock.patch("sys.stdout", stdout),
            mock.patch.object(
                cli,
                "_import_saved_portal",
                return_value={
                    "status": "auth_required",
                    "added": 0,
                    "duplicates": 0,
                    "skipped_unresolved": 0,
                    "errors": 0,
                    "scraped": 0,
                    "resolved": 0,
                    "message": "TrueUp session expired",
                },
            ),
        ):
            result = cli.cmd_add(args)

        self.assertEqual(result, 1)
        output = stdout.getvalue()
        self.assertIn("status=auth_required", output)
        self.assertIn("Message: TrueUp session expired", output)

    def test_import_saved_portal_uses_shared_registry_module_loader(self):
        cli = load_cli_module()
        fake_module = mock.Mock()
        fake_module.import_saved_jobs.return_value = {"status": "ok"}

        with (
            mock.patch.object(cli, "_maybe_reexec_saved_portal_with_uv"),
            mock.patch.object(cli, "_open_job_db") as open_job_db,
            mock.patch("saved_portal_import.load_saved_portal_module", return_value=fake_module) as load_module,
        ):
            result = cli._import_saved_portal("jackandjill", priority=5, provider="codex")

        self.assertEqual(result, {"status": "ok"})
        load_module.assert_called_once_with("jackandjill")
        fake_module.import_saved_jobs.assert_called_once_with(
            open_job_db.return_value,
            priority=5,
            provider="codex",
        )

    def test_print_saved_portal_summary_uses_registry_label(self):
        cli = load_cli_module()
        stdout = io.StringIO()

        with (
            mock.patch("sys.stdout", stdout),
            mock.patch("saved_portal_import.get_saved_portal") as get_saved_portal,
        ):
            get_saved_portal.return_value.label = "Jack & Jill"
            cli._print_saved_portal_summary(
                "jackandjill",
                {
                    "status": "ok",
                    "scraped": 4,
                    "resolved": 3,
                    "added": 2,
                    "duplicates": 1,
                    "skipped_unresolved": 1,
                    "errors": 0,
                },
            )

        self.assertIn(
            "Jack & Jill import: status=ok scraped=4 resolved=3 added=2 duplicates=1 unresolved=1 errors=0",
            stdout.getvalue(),
        )

    def test_saved_portal_import_reexecs_under_uv_when_playwright_is_missing(self):
        cli = load_cli_module()

        with (
            mock.patch.object(cli.importlib.util, "find_spec", return_value=None),
            mock.patch.object(cli.shutil, "which", return_value="/usr/local/bin/uv"),
            mock.patch.object(cli.subprocess, "call", return_value=0) as subprocess_call,
            mock.patch.object(cli.os, "environ", {"PATH": "/usr/local/bin"}),
            mock.patch.object(cli.sys, "argv", ["job-assets", "add", "--saved-portal", "trueup"]),
        ):
            with self.assertRaises(SystemExit) as exc:
                cli._maybe_reexec_saved_portal_with_uv()

        self.assertEqual(exc.exception.code, 0)
        args, kwargs = subprocess_call.call_args
        self.assertEqual(
            args[0],
            [
                "uv",
                "run",
                "--project",
                str(PROJECT_ROOT),
                "python",
                str(PROJECT_ROOT / "bin" / "job-assets"),
                "add",
                "--saved-portal",
                "trueup",
            ],
        )
        self.assertEqual(kwargs["cwd"], PROJECT_ROOT)
        self.assertEqual(kwargs["env"]["JOB_ASSETS_SAVED_PORTAL_BOOTSTRAPPED"], "1")

    def test_cli_reexecs_under_uv_when_pdfplumber_is_missing(self):
        cli = load_cli_module()

        with (
            mock.patch.object(cli.importlib.util, "find_spec", return_value=None),
            mock.patch.object(cli.shutil, "which", return_value="/usr/local/bin/uv"),
            mock.patch.object(cli.subprocess, "call", return_value=0) as subprocess_call,
            mock.patch.object(cli.os, "environ", {"PATH": "/usr/local/bin"}),
            mock.patch.object(cli.sys, "argv", ["job-assets", "sync"]),
        ):
            with self.assertRaises(SystemExit) as exc:
                cli._maybe_reexec_cli_with_uv()

        self.assertEqual(exc.exception.code, 0)
        args, kwargs = subprocess_call.call_args
        self.assertEqual(
            args[0],
            [
                "uv",
                "run",
                "--project",
                str(PROJECT_ROOT),
                "python",
                str(PROJECT_ROOT / "bin" / "job-assets"),
                "sync",
            ],
        )
        self.assertEqual(kwargs["cwd"], PROJECT_ROOT)
        self.assertEqual(kwargs["env"]["JOB_ASSETS_CLI_BOOTSTRAPPED"], "1")

    def test_cli_reexec_skips_when_pdfplumber_is_available(self):
        cli = load_cli_module()

        with (
            mock.patch.object(cli.importlib.util, "find_spec", return_value=object()),
            mock.patch.object(cli.subprocess, "call") as subprocess_call,
        ):
            cli._maybe_reexec_cli_with_uv()

        subprocess_call.assert_not_called()

    def test_cmd_doctor_reports_gemini_flash_model(self):
        cli = load_cli_module()
        args = argparse.Namespace()
        stdout = io.StringIO()

        with mock.patch("sys.stdout", stdout), mock.patch("shutil.which", return_value="/usr/local/bin/tool"):
            result = cli.cmd_doctor(args)

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("gemini-flash defaults: model=gemini-3-flash-preview", output)

    def test_main_blocks_recursive_provider_entrypoints(self):
        cli = load_cli_module()
        stderr = io.StringIO()

        with mock.patch.dict(os.environ, {"JOB_ASSETS_FORBID_RECURSIVE_ENTRYPOINTS": "1"}, clear=False):
            with mock.patch("sys.argv", ["job-assets", "doctor"]), mock.patch("sys.stderr", stderr):
                with self.assertRaises(SystemExit) as exc:
                    cli.main()

        self.assertIn("cannot be invoked from inside a non-interactive provider subtask", str(exc.exception))

    def test_web_help_shows_reduced_worker_default(self):
        completed = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "bin" / "job-assets"), "web", "--help"],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("Number of job workers (default: 16)", completed.stdout)


if __name__ == "__main__":
    unittest.main()
