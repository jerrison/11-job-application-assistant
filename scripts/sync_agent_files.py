#!/usr/bin/env python3
"""Generate provider-specific agent files from AGENTS.md (canonical source).

Usage:
    uv run python scripts/sync_agent_files.py            # Generate all files
    uv run python scripts/sync_agent_files.py --check     # Exit 1 if any file is stale
    uv run python scripts/sync_agent_files.py --target claude   # Generate one file
    uv run python scripts/sync_agent_files.py --target gemini
    uv run python scripts/sync_agent_files.py --target codex
    uv run python scripts/sync_agent_files.py --target gpt
    uv run python scripts/sync_agent_files.py --target copilot
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

AGENTS_MD = PROJECT_ROOT / "AGENTS.md"

# ---------------------------------------------------------------------------
# Target definitions
# ---------------------------------------------------------------------------

TARGETS: dict[str, dict] = {
    "claude": {
        "path": PROJECT_ROOT / "CLAUDE.md",
        "header": (
            "<!-- GENERATED — do not edit directly. Source: AGENTS.md -->\n"
            "<!-- Regenerate: uv run python scripts/sync_agent_files.py -->\n"
            "<!-- Provider: Claude Code -->\n"
        ),
        "builder": None,
    },
    "gemini": {
        "path": PROJECT_ROOT / "GEMINI.md",
        "header": (
            "<!-- GENERATED — do not edit directly. Source: AGENTS.md -->\n"
            "<!-- Regenerate: uv run python scripts/sync_agent_files.py -->\n"
            "<!-- Provider: Google Gemini CLI -->\n"
        ),
        "builder": None,  # uses default (header + full AGENTS.md)
    },
    "codex": {
        "path": PROJECT_ROOT / "CODEX.md",
        "header": (
            "<!-- GENERATED — do not edit directly. Source: AGENTS.md -->\n"
            "<!-- Regenerate: uv run python scripts/sync_agent_files.py -->\n"
            "<!-- Provider: OpenAI Codex CLI -->\n"
        ),
        "builder": None,
    },
    "gpt": {
        "path": PROJECT_ROOT / "GPT.md",
        "header": (
            "<!-- GENERATED — do not edit directly. Source: AGENTS.md -->\n"
            "<!-- Regenerate: uv run python scripts/sync_agent_files.py -->\n"
            "<!-- Provider: OpenAI GPT / Codex-compatible runtimes -->\n"
        ),
        "builder": None,
    },
    "copilot": {
        "path": PROJECT_ROOT / ".github" / "copilot-instructions.md",
        "header": (
            "<!-- GENERATED — do not edit directly. Source: AGENTS.md -->\n"
            "<!-- Regenerate: uv run python scripts/sync_agent_files.py -->\n"
            "<!-- Provider: GitHub Copilot -->\n"
        ),
        "builder": None,
    },
}


def _build_content(target_name: str) -> str:
    """Return the expected file content for *target_name*."""
    cfg = TARGETS[target_name]
    builder_name = cfg["builder"]
    if builder_name is not None:
        builder_fn = globals()[builder_name]
        return builder_fn()

    agents_text = AGENTS_MD.read_text(encoding="utf-8")
    return cfg["header"] + "\n" + agents_text


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def generate(target_name: str) -> bool:
    """Write the target file.  Returns True if the file changed."""
    cfg = TARGETS[target_name]
    dest: Path = cfg["path"]
    expected = _build_content(target_name)

    if dest.exists() and dest.read_text(encoding="utf-8") == expected:
        print(f"  \u2713 {dest.relative_to(PROJECT_ROOT)} is up to date")
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(expected, encoding="utf-8")
    print(f"  \u2713 {dest.relative_to(PROJECT_ROOT)} generated")
    return True


def check(target_name: str) -> bool:
    """Return True if the target file matches the expected content."""
    cfg = TARGETS[target_name]
    dest: Path = cfg["path"]
    expected = _build_content(target_name)
    rel = dest.relative_to(PROJECT_ROOT)

    if not dest.exists():
        print(f"  \u2717 {rel} is missing \u2014 regenerate with: uv run python scripts/sync_agent_files.py")
        return False

    if dest.read_text(encoding="utf-8") != expected:
        print(f"  \u2717 {rel} is stale \u2014 regenerate with: uv run python scripts/sync_agent_files.py")
        return False

    print(f"  \u2713 {rel} is up to date")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync provider agent files from AGENTS.md",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify files are up to date (exit 1 if stale)",
    )
    parser.add_argument(
        "--target",
        choices=list(TARGETS),
        help="Generate a single target instead of all",
    )
    args = parser.parse_args(argv)

    if not AGENTS_MD.exists():
        print(f"Error: {AGENTS_MD} not found", file=sys.stderr)
        return 1

    names = [args.target] if args.target else list(TARGETS)

    if args.check:
        results = [check(name) for name in names]
        all_ok = all(results)
        return 0 if all_ok else 1

    for name in names:
        generate(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
