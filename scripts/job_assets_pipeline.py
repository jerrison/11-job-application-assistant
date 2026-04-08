#!/usr/bin/env python3
"""Run the single-job asset pipeline and then continue into submit automation."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from entrypoint_guard import abort_if_recursive_entrypoints_forbidden  # noqa: E402, I001
from llm_provider import (  # noqa: E402, I001
    VALID_PROVIDERS,
    default_active_provider,
    provider_binary as _provider_binary,
)
from project_env import load_project_env  # noqa: E402, I001
from worker_subprocess import run_worker_subprocess  # noqa: E402, I001

load_project_env()
abort_if_recursive_entrypoints_forbidden("scripts/job_assets_pipeline.py")


def repo_command(path: str) -> str:
    return str(PROJECT_ROOT / path)


def default_provider() -> str:
    if os.environ.get("ASSET_LLM_PROVIDER_CHAIN"):
        return "chain"
    return default_active_provider()


def require_provider(provider: str) -> None:
    if provider not in VALID_PROVIDERS:
        if provider == "chain":
            return
        raise ValueError(f"Unsupported provider: {provider}")
    binary = _provider_binary(provider)
    if not shutil.which(binary):
        raise FileNotFoundError(f"'{binary}' is not installed or not on PATH.")


def python_script_command(relative_path: str) -> list[str]:
    script = repo_command(relative_path)
    if shutil.which("uv"):
        return ["uv", "run", "--project", str(PROJECT_ROOT), "python", script]
    return [sys.executable, script]


def run_command(cmd: list[str], *, env: dict[str, str] | None = None) -> int:
    completed: subprocess.CompletedProcess = run_worker_subprocess(cmd, cwd=PROJECT_ROOT, env=env)
    return completed.returncode


def build_apply_command(args: argparse.Namespace, meta_path_file: str) -> list[str]:
    cmd = ["bash", repo_command("apply.sh"), "--provider", args.provider]
    if args.skip_sync:
        cmd.append("--skip-sync")
    cmd.extend(["--meta-path-file", meta_path_file, args.jd_source])
    if args.company:
        cmd.append(args.company)
    if args.role:
        cmd.append(args.role)
    return cmd


def build_submit_command(args: argparse.Namespace, target: str) -> list[str]:
    cmd = [*python_script_command("scripts/submit_application.py"), target]
    if args.payload_only:
        cmd.append("--payload-only")
    if args.headless:
        cmd.append("--headless")
    if args.submit:
        cmd.append("--submit")
    if args.reapply:
        cmd.append("--reapply")
    if args.browser_provider:
        cmd.extend(["--browser-provider", args.browser_provider])
    cmd.extend(["--provider", args.provider])
    return cmd


def read_meta_capture(meta_path_file: str) -> dict:
    capture_path = Path(meta_path_file)
    captured = capture_path.read_text(encoding="utf-8").strip()
    if not captured:
        raise RuntimeError("The apply step did not report a resolved .pipeline_meta.json path.")
    meta_path = Path(captured)
    if not meta_path.is_absolute():
        meta_path = (PROJECT_ROOT / meta_path).resolve()
    if not meta_path.exists():
        raise FileNotFoundError(f"Resolved metadata file does not exist: {meta_path}")
    return json.loads(meta_path.read_text(encoding="utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the single-job asset pipeline end-to-end: generate tailored assets, "
            "then continue into supported board autofill/submission."
        ),
    )
    parser.add_argument(
        "--provider",
        choices=(*VALID_PROVIDERS, "chain"),
        default=default_provider(),
        help="LLM provider to use for asset generation and submit-time answers.",
    )
    parser.add_argument(
        "--skip-sync",
        action="store_true",
        help="Skip syncing work_stories.md and candidate_context.md before generation.",
    )
    parser.add_argument(
        "--browser-provider",
        choices=("local", "steel"),
        default=None,
        help="Browser runtime to use for submit automation (default: env or local).",
    )
    parser.add_argument(
        "--payload-only",
        action="store_true",
        help="Generate the board-specific autofill payload after assets are built, but do not launch the browser.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Launch the Playwright runtime in headless mode during the submit stage.",
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Submit the application after autofill. Without this flag, the browser stops at a review state.",
    )
    parser.add_argument(
        "--reapply",
        action="store_true",
        help="Start or continue one fresh submit-* reapply artifact directory for this role.",
    )
    parser.add_argument("jd_source", help="Job description source: URL or local file")
    parser.add_argument("company", nargs="?", help="Optional company slug override")
    parser.add_argument("role", nargs="?", help="Optional role slug override")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.payload_only and args.submit:
        parser.error("--payload-only and --submit cannot be used together.")

    try:
        require_provider(args.provider)
    except (ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1

    with tempfile.NamedTemporaryFile(prefix="job-assets-meta-", suffix=".txt", delete=False) as capture_file:
        meta_path_file = capture_file.name

    try:
        apply_cmd = build_apply_command(args, meta_path_file)
        apply_status = run_command(apply_cmd)
        if apply_status != 0:
            return apply_status

        meta = read_meta_capture(meta_path_file)
        out_dir = str(meta["out_dir"])
        company = meta["company"]
        role = meta["role"]

        print("", flush=True)
        print("── Step 4: Submit automation ──", flush=True)
        print(f"── Target: {company}/{role} ({out_dir}) ──", flush=True)

        submit_cmd = build_submit_command(args, out_dir)
        return run_command(submit_cmd)
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        try:
            Path(meta_path_file).unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
