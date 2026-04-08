#!/usr/bin/env python3
"""Run the backlog sweep checker plus the standard repo verification bundle."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST_PATH = PROJECT_ROOT / ".context" / "compound-engineering" / "todos" / "current_backlog_sweep.json"
REQUIRED_MANIFEST_KEYS = (
    "phase1_snapshot",
    "phase1_results",
    "phase2_snapshot",
    "phase2_results",
    "phase3_snapshot",
    "phase3_results",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH, help="Path to the active sweep manifest.")
    parser.add_argument(
        "--active",
        action="store_true",
        help=f"Use the default active sweep manifest at {DEFAULT_MANIFEST_PATH.relative_to(PROJECT_ROOT)}.",
    )
    return parser


def load_manifest(path: Path) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(f"Missing manifest: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    missing = [key for key in REQUIRED_MANIFEST_KEYS if not payload.get(key)]
    if missing:
        raise ValueError(f"{path} is missing required keys: {', '.join(missing)}")
    return payload


def verification_commands(manifest_path: Path) -> list[list[str]]:
    return [
        ["uv", "run", "python", "scripts/check_backlog_sweep.py", "--manifest", str(manifest_path)],
        ["uv", "run", "python", "-m", "pytest", "tests/", "-v"],
        ["uv", "run", "ruff", "check", "scripts/", "tests/"],
        ["uv", "run", "python", "scripts/check_architecture.py"],
        ["uv", "run", "python", "scripts/sync_agent_files.py", "--check"],
        ["uv", "run", "python", "scripts/check_agent_docs.py"],
    ]


def run_command(command: list[str]) -> int:
    completed = subprocess.run(command, cwd=PROJECT_ROOT)
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    manifest_path = DEFAULT_MANIFEST_PATH if args.active else args.manifest

    try:
        load_manifest(manifest_path)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1

    for command in verification_commands(manifest_path):
        print(f"Running: {' '.join(command)}")
        exit_code = run_command(command)
        if exit_code != 0:
            print(f"FAILED: {' '.join(command)}")
            return exit_code

    print("Active sweep verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
