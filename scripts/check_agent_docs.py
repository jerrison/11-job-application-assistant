#!/usr/bin/env python3
"""Doc-gardening validator for agent instruction files.

Checks:
  1. Agent file sync — shells out to sync_agent_files.py --check (if it exists)
  2. Broken links — scans .md files in repo root and docs/ for broken internal links
  3. AGENTS.md size — warns if AGENTS.md exceeds 110 lines
  4. INDEX.md references — verifies all files referenced in docs/INDEX.md exist
  5. Execution-plan scaffolding — validates docs/exec-plans/ structure and template sections

Run:
  uv run python scripts/check_agent_docs.py                    # All checks
  uv run python scripts/check_agent_docs.py --check links      # Specific check
  uv run python scripts/check_agent_docs.py --check sync
  uv run python scripts/check_agent_docs.py --check size
  uv run python scripts/check_agent_docs.py --check index
  uv run python scripts/check_agent_docs.py --check plans
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

AGENTS_MD = PROJECT_ROOT / "AGENTS.md"
INDEX_MD = PROJECT_ROOT / "docs" / "INDEX.md"
SYNC_SCRIPT = PROJECT_ROOT / "scripts" / "sync_agent_files.py"
EXEC_PLANS_ROOT = PROJECT_ROOT / "docs" / "exec-plans"
EXEC_PLANS_ACTIVE = EXEC_PLANS_ROOT / "active"
EXEC_PLANS_COMPLETED = EXEC_PLANS_ROOT / "completed"
EXEC_PLANS_README = EXEC_PLANS_ROOT / "README.md"
PLAN_TEMPLATE = PROJECT_ROOT / "docs" / "PLAN_TEMPLATE.md"

AGENTS_LINE_LIMIT = 110
PLAN_TEMPLATE_SECTIONS = (
    "## Purpose / Big Picture",
    "## Context and Orientation",
    "## Milestones",
    "## Progress",
    "## Surprises & Discoveries",
    "## Decision Log",
    "## Outcomes & Retrospective",
)

# Matches markdown links: [text](path) — captures the text and path
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

# Matches fenced code blocks (``` ... ``` or ~~~ ... ~~~) to exclude from link scanning
FENCED_CODE_RE = re.compile(
    r"^(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^(?P=fence)\s*$",
    re.MULTILINE | re.DOTALL,
)

# Matches inline code spans that wrap an entire markdown link — e.g. `[text](path)`
# These are code examples, not actual links
INLINE_CODE_LINK_RE = re.compile(r"`[^`]*\[[^\]]+\]\([^)]+\)[^`]*`")


def _collect_md_files() -> list[Path]:
    """Collect .md files from the repo root and docs/ directory (non-recursive for root)."""
    files: list[Path] = []

    # Root-level .md files (not recursive — excludes output/, .venv/, etc.)
    for f in PROJECT_ROOT.iterdir():
        if f.is_file() and f.suffix == ".md":
            files.append(f)

    # docs/ directory — recursive to catch superpowers/ subdirs
    docs_dir = PROJECT_ROOT / "docs"
    if docs_dir.is_dir():
        files.extend(docs_dir.rglob("*.md"))

    return sorted(files)


def check_sync() -> tuple[list[str], list[str]]:
    """Check agent file sync by running sync_agent_files.py --check."""
    passes: list[str] = []
    failures: list[str] = []

    if not SYNC_SCRIPT.exists():
        failures.append(f"Sync script not found: {SYNC_SCRIPT.relative_to(PROJECT_ROOT)}")
        return passes, failures

    result = subprocess.run(
        [sys.executable, str(SYNC_SCRIPT), "--check"],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )

    if result.returncode == 0:
        passes.append("Agent files in sync")
    else:
        # Include stderr/stdout details in the failure message
        detail = result.stdout.strip() or result.stderr.strip() or "exit code 1"
        failures.append(f"Agent files out of sync: {detail}")

    return passes, failures


def check_links() -> tuple[list[str], list[str]]:
    """Scan .md files for broken internal links."""
    passes: list[str] = []
    failures: list[str] = []

    md_files = _collect_md_files()
    checked = 0

    for md_file in md_files:
        text = md_file.read_text(encoding="utf-8", errors="replace")

        # Strip fenced code blocks and inline-code-wrapped links to avoid
        # false positives from examples like `[text](path)`
        text_no_code = FENCED_CODE_RE.sub("", text)
        text_no_code = INLINE_CODE_LINK_RE.sub("", text_no_code)

        for match in MD_LINK_RE.finditer(text_no_code):
            link_text = match.group(1)
            link_path = match.group(2)

            # Skip external URLs
            if link_path.startswith(("http://", "https://", "mailto:")):
                continue

            # Skip anchor-only links
            if link_path.startswith("#"):
                continue

            # Strip fragment identifiers (e.g., path.md#section)
            link_path_clean = link_path.split("#")[0]
            if not link_path_clean:
                continue

            checked += 1

            # Resolve relative to the file's directory
            target = (md_file.parent / link_path_clean).resolve()
            if not target.exists():
                rel_file = md_file.relative_to(PROJECT_ROOT)
                failures.append(f"Broken link in {rel_file}: [{link_text}]({link_path}) \u2014 target does not exist")

    if not failures:
        passes.append(f"All {checked} internal doc links resolve")

    return passes, failures


def check_size() -> tuple[list[str], list[str]]:
    """Check AGENTS.md line count against limit."""
    passes: list[str] = []
    failures: list[str] = []

    if not AGENTS_MD.exists():
        failures.append("AGENTS.md not found")
        return passes, failures

    line_count = len(AGENTS_MD.read_text(encoding="utf-8").splitlines())

    if line_count <= AGENTS_LINE_LIMIT:
        passes.append(f"AGENTS.md is {line_count} lines (under {AGENTS_LINE_LIMIT} limit)")
    else:
        failures.append(f"AGENTS.md is {line_count} lines (exceeds {AGENTS_LINE_LIMIT} limit)")

    return passes, failures


def check_index() -> tuple[list[str], list[str]]:
    """Verify all file references in docs/INDEX.md exist."""
    passes: list[str] = []
    failures: list[str] = []

    if not INDEX_MD.exists():
        failures.append("docs/INDEX.md not found")
        return passes, failures

    text = INDEX_MD.read_text(encoding="utf-8")
    checked = 0

    for match in MD_LINK_RE.finditer(text):
        link_text = match.group(1)
        link_path = match.group(2)

        # Skip external URLs
        if link_path.startswith(("http://", "https://", "mailto:")):
            continue

        # Skip anchor-only links
        if link_path.startswith("#"):
            continue

        # Strip fragments
        link_path_clean = link_path.split("#")[0]
        if not link_path_clean:
            continue

        checked += 1

        # Resolve relative to docs/ directory
        target = (INDEX_MD.parent / link_path_clean).resolve()
        if not target.exists():
            failures.append(f"INDEX.md reference missing: [{link_text}]({link_path}) \u2014 target does not exist")

    if not failures:
        passes.append(f"All {checked} INDEX.md references exist")

    return passes, failures


def check_plans() -> tuple[list[str], list[str]]:
    """Verify execution-plan directories and template sections exist."""
    passes: list[str] = []
    failures: list[str] = []

    required_paths = {
        "docs/exec-plans/README.md": EXEC_PLANS_README,
        "docs/exec-plans/active/": EXEC_PLANS_ACTIVE,
        "docs/exec-plans/completed/": EXEC_PLANS_COMPLETED,
        "docs/PLAN_TEMPLATE.md": PLAN_TEMPLATE,
    }

    for label, path in required_paths.items():
        if not path.exists():
            failures.append(f"Missing execution-plan path: {label}")

    for label, path in (
        ("docs/exec-plans/active/", EXEC_PLANS_ACTIVE),
        ("docs/exec-plans/completed/", EXEC_PLANS_COMPLETED),
    ):
        if path.exists() and not path.is_dir():
            failures.append(f"Execution-plan path is not a directory: {label}")

    if PLAN_TEMPLATE.exists():
        template_text = PLAN_TEMPLATE.read_text(encoding="utf-8")
        for section in PLAN_TEMPLATE_SECTIONS:
            if section not in template_text:
                failures.append(f"PLAN_TEMPLATE.md missing required section: {section}")

    if not failures:
        passes.append("Execution-plan scaffolding is present")

    return passes, failures


# Map of check names to functions
CHECKS: dict[str, callable] = {
    "sync": check_sync,
    "links": check_links,
    "size": check_size,
    "index": check_index,
    "plans": check_plans,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Doc-gardening validator for agent instruction files.")
    parser.add_argument(
        "--check",
        choices=list(CHECKS.keys()),
        help="Run a specific check (default: all)",
    )
    args = parser.parse_args()

    checks_to_run = [args.check] if args.check else list(CHECKS.keys())

    all_passes: list[str] = []
    all_failures: list[str] = []

    for name in checks_to_run:
        passes, failures = CHECKS[name]()
        all_passes.extend(passes)
        all_failures.extend(failures)

    # Print passes
    for msg in all_passes:
        print(f"\u2713 {msg}")

    # Print failures
    if all_failures:
        print(f"\n{len(all_failures)} issue(s) found:")
        for msg in all_failures:
            print(f"\u2717 {msg}")

    return 1 if all_failures else 0


if __name__ == "__main__":
    sys.exit(main())
