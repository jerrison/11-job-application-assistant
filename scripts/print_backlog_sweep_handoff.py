#!/usr/bin/env python3
"""Print a ready-to-paste prompt for resuming the active backlog sweep."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST_PATH = PROJECT_ROOT / ".context" / "compound-engineering" / "todos" / "current_backlog_sweep.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH, help="Sweep manifest to summarize.")
    parser.add_argument(
        "--active",
        action="store_true",
        help=f"Use the active manifest at {DEFAULT_MANIFEST_PATH.relative_to(PROJECT_ROOT)}.",
    )
    return parser


def _load_manifest(path: Path) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(f"Missing manifest: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _read_tsv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing TSV: {path}")
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames is None:
            raise ValueError(f"{path} is missing a TSV header row")
        return [
            {str(key).strip(): str(value or "").strip() for key, value in row.items() if key is not None}
            for row in reader
            if any(str(value or "").strip() for value in row.values())
        ]


def _relpath(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return str(path)


def _phase_progress(manifest: dict[str, object], phase_key: str) -> tuple[int, int, str]:
    snapshot_raw = str(manifest.get(f"{phase_key}_snapshot") or "").strip()
    results_raw = str(manifest.get(f"{phase_key}_results") or "").strip()
    if not snapshot_raw or not results_raw:
        return 0, 0, "-"

    snapshot_rows = _read_tsv_rows(Path(snapshot_raw))
    result_rows = _read_tsv_rows(Path(results_raw))

    latest_by_id: dict[str, dict[str, str]] = {}
    for row in result_rows:
        job_id = row.get("id", "").strip()
        if job_id:
            latest_by_id[job_id] = row

    outcomes = Counter(row.get("outcome", "").strip() for row in latest_by_id.values() if row.get("outcome", "").strip())
    outcome_summary = ", ".join(f"{name}={outcomes[name]}" for name in sorted(outcomes)) or "-"
    return len(snapshot_rows), len(latest_by_id), outcome_summary


def _active_exec_plan_paths() -> list[str]:
    plan_dir = PROJECT_ROOT / "docs" / "exec-plans" / "active"
    if not plan_dir.exists():
        return []
    return [
        _relpath(path)
        for path in sorted(plan_dir.glob("*.md"))
        if path.name != ".gitkeep"
    ]


def build_handoff_prompt(manifest_path: Path) -> str:
    manifest = _load_manifest(manifest_path)
    phase2_total, phase2_covered, phase2_outcomes = _phase_progress(manifest, "phase2")
    phase3_total, phase3_covered, phase3_outcomes = _phase_progress(manifest, "phase3")
    run_id = str(manifest.get("run_id") or "").strip() or "unknown"

    repo_state = manifest.get("repo_state")
    dirty_paths_count = 0
    if isinstance(repo_state, dict):
        dirty_paths_count = int(repo_state.get("dirty_paths_count") or 0)

    manifest_rel = _relpath(manifest_path)
    phase2_snapshot = _relpath(Path(str(manifest.get("phase2_snapshot") or manifest_path)))
    phase2_results = _relpath(Path(str(manifest.get("phase2_results") or manifest_path)))
    phase3_snapshot = _relpath(Path(str(manifest.get("phase3_snapshot") or manifest_path)))
    phase3_results = _relpath(Path(str(manifest.get("phase3_results") or manifest_path)))
    plan_paths = _active_exec_plan_paths()

    plan_lines = "\n".join(f"- `{path}`" for path in plan_paths) if plan_paths else "- `docs/exec-plans/active/` (no active plan files found)"

    return "\n".join(
        [
            "$using-superpowers",
            "",
            "Resume the active backlog sweep already in progress in this repo. Do not start a new sweep run unless I explicitly tell you to replace the active manifest.",
            "",
            "Follow, in order:",
            "- `AGENTS.md`",
            "- `docs/operational-rules.md`",
            "- `docs/backlog-sweep.md`",
            "- `docs/runbooks/repeatable-backlog-sweep.md`",
            "- active execution plan(s):",
            plan_lines,
            f"- active sweep manifest: `{manifest_rel}`",
            "",
            "Treat the repo-local manifest, snapshots, ledgers, and proof artifacts as the source of truth.",
            f"Current sweep run id: `{run_id}`.",
            f"Current repo dirtiness from the manifest bootstrap: `{dirty_paths_count}` dirty paths.",
            "",
            "Active sweep progress:",
            f"- Phase 2 snapshot: `{phase2_snapshot}` ({phase2_total} rows)",
            f"- Phase 2 results ledger: `{phase2_results}` ({phase2_covered} latest covered rows; outcomes: {phase2_outcomes})",
            f"- Phase 3 snapshot: `{phase3_snapshot}` ({phase3_total} rows)",
            f"- Phase 3 results ledger: `{phase3_results}` ({phase3_covered} latest covered rows; outcomes: {phase3_outcomes})",
            "",
            "Execution rules:",
            "- Always use `--draft`. Never auto-submit. Fail closed at the final review boundary.",
            "- Screenshots are the source of truth.",
            "- Use repo-native commands and `uv run python`, never bare `python`.",
            "- Use the repo-native sweep recorder for every handled snapshot row. Do not hand-edit or bulk-backfill ledgers.",
            "- Default to repair, not description. If a Linear issue, stopped row, or drafted row exposes a likely repo-side bug, reproduce it, attempt a concrete generalized fix, rerun/redraft, and only then record the row or close the issue.",
            "- Creating or updating a Linear issue does not count as a repair attempt or as handling a row by itself.",
            "- Only park or classify a row as terminal after you have evidence that it is external, user-blocked, or still unresolved after the allowed fix budget.",
            "- Continue from uncovered snapshot rows and the latest failing verification state. Do not restart Phase 2 or Phase 3 unless explicitly instructed.",
            "- Use sub-agents and parallel work only for disjoint batches that keep the ledgers correct.",
            "",
            "Work to finish:",
            "- Complete every Linear issue currently in `Todo`. If an issue has `requires-user-input`, read the comments and use that input. If human action is still required, keep it parked and move on.",
            "- Exhaust the active Phase 2 stopped-job snapshot row by row.",
            "- Exhaust the active Phase 3 draft-review snapshot row by row through the browser review surface.",
            "- Fix any lint, test, architecture, doc-sync, or CI fallout before claiming completion.",
            "- Commit, push, merge, including data, only after the verifier passes or the remaining blockers are explicitly reported.",
            "",
            "Coverage and completion gates:",
            "- Use `uv run python scripts/check_backlog_sweep.py --active` as the fast coverage gate.",
            "- Before claiming completion, run `uv run python scripts/verify_active_sweep.py --active`.",
            "- If anything remains uncovered or blocked, report `incomplete` and list every remaining snapshot row and Linear blocker explicitly.",
            "- In the final report, include how many concrete repair attempts were made and which rows were left open without a code change, with proof for each.",
            "",
            "Before your final message, generate the next handoff with:",
            "- `uv run python scripts/print_backlog_sweep_handoff.py --active`",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    manifest_path = DEFAULT_MANIFEST_PATH if args.active else args.manifest

    try:
        prompt = build_handoff_prompt(manifest_path)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 1

    print(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
