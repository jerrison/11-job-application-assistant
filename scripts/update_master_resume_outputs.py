#!/usr/bin/env python3
"""Sync source resume and regenerate markdown/PDF artifacts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = PROJECT_ROOT / "scripts" / "sync_master_resume.py"
RENDER_SCRIPT = PROJECT_ROOT / "scripts" / "render_resume_pdf.py"
OUTPUT_MD = PROJECT_ROOT / "master_resume.md"
OUTPUT_PDF = PROJECT_ROOT / "output" / "pdf" / "master_resume.pdf"


def select_python() -> Path:
    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        return venv_python
    return Path(sys.executable)


def run_step(name: str, command: list[str]) -> None:
    print(f"[{name}] {' '.join(command)}", flush=True)
    completed = subprocess.run(command, cwd=PROJECT_ROOT, text=True)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main() -> int:
    python_for_sync = Path(sys.executable)
    python_for_pdf = select_python()

    run_step("sync", [str(python_for_sync), str(SYNC_SCRIPT)])
    run_step("pdf", [str(python_for_pdf), str(RENDER_SCRIPT)])

    if not OUTPUT_MD.exists():
        print(f"Error: missing {OUTPUT_MD}")
        return 1
    if not OUTPUT_PDF.exists():
        print(f"Error: missing {OUTPUT_PDF}")
        return 1

    print(f"Ready: {OUTPUT_MD}")
    print(f"Ready: {OUTPUT_PDF}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
