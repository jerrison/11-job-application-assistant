#!/usr/bin/env python3
"""Run a subprocess with an optional timeout and optional combined log capture."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _coerce_text(chunk: str | bytes | None) -> str:
    if chunk is None:
        return ""
    if isinstance(chunk, bytes):
        return chunk.decode("utf-8", errors="replace")
    return chunk


def _combined_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    text = _coerce_text(stdout)
    stderr_text = _coerce_text(stderr)
    if stderr_text:
        if text and not text.endswith("\n"):
            text += "\n"
        text += stderr_text
    return text


def _write_log(log_file: str | None, text: str) -> None:
    if not log_file:
        return
    Path(log_file).write_text(text, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a subprocess with timeout and optional logging.")
    parser.add_argument("--cwd", default=".", help="Working directory for the subprocess.")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=0,
        help="Timeout in seconds. Use 0 to disable the timeout.",
    )
    parser.add_argument(
        "--log-file",
        default="",
        help="Optional file path to receive combined stdout/stderr.",
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
        parser.error("a command is required")

    timeout_seconds = args.timeout_seconds if args.timeout_seconds > 0 else None
    try:
        completed = subprocess.run(
            command,
            cwd=args.cwd,
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        output = _combined_output(exc.stdout, exc.stderr)
        if output and not output.endswith("\n"):
            output += "\n"
        output += f"ERROR: Command timed out after {args.timeout_seconds}s.\n"
        _write_log(args.log_file, output)
        if not args.log_file and output:
            sys.stderr.write(output)
        return 124

    output = _combined_output(completed.stdout, completed.stderr)
    _write_log(args.log_file, output)
    if not args.log_file:
        if completed.stdout:
            sys.stdout.write(completed.stdout)
        if completed.stderr:
            sys.stderr.write(completed.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
