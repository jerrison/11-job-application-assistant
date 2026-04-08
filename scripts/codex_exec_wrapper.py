#!/usr/bin/env python3
"""Run Codex with an isolated CODEX_HOME that mirrors global assets and preserves user MCP servers."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

COPIED_FILES: tuple[str, ...] = ("auth.json", "AGENTS.md")
COPIED_DIRECTORIES: tuple[str, ...] = ("prompts", "skills")


def _default_source_codex_home() -> Path:
    raw = os.environ.get("JOB_ASSETS_SOURCE_CODEX_HOME") or os.environ.get("CODEX_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".codex"


def _sanitize_config(config_text: str) -> str:
    kept_lines: list[str] = []
    current_header = ""
    for line in config_text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("[") and stripped.rstrip().endswith("]"):
            current_header = stripped.strip()
        if current_header == "[features]" and stripped.split("=", 1)[0].strip() == "apps":
            continue
        kept_lines.append(line)
    return "".join(kept_lines)


def _copy_file_if_exists(source: Path, target: Path) -> None:
    if source.exists():
        shutil.copy2(source, target)


def _copy_dir_if_exists(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)


def _copy_codex_home_artifacts(source_home: Path, target_home: Path) -> None:
    target_home.mkdir(parents=True, exist_ok=True)

    for relative_name in COPIED_FILES:
        _copy_file_if_exists(source_home / relative_name, target_home / relative_name)

    for relative_name in COPIED_DIRECTORIES:
        _copy_dir_if_exists(source_home / relative_name, target_home / relative_name)

    config_path = source_home / "config.toml"
    if config_path.exists():
        sanitized = _sanitize_config(config_path.read_text(encoding="utf-8"))
        (target_home / "config.toml").write_text(sanitized, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Codex with an isolated CODEX_HOME.")
    parser.add_argument(
        "--source-codex-home",
        default=str(_default_source_codex_home()),
        help="Source CODEX_HOME to sanitize before running codex.",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to run after --")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a codex command is required")

    source_home = Path(args.source_codex_home).expanduser()
    with tempfile.TemporaryDirectory(prefix="job-assets-codex-home-") as tmp_dir:
        isolated_home = Path(tmp_dir)
        _copy_codex_home_artifacts(source_home, isolated_home)
        env = os.environ.copy()
        env["JOB_ASSETS_SOURCE_CODEX_HOME"] = str(source_home)
        env["CODEX_HOME"] = str(isolated_home)
        completed = subprocess.run(command, env=env)
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
