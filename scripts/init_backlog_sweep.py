#!/usr/bin/env python3
"""Initialize the active backlog sweep manifest and phase snapshots."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TODOS_DIR = PROJECT_ROOT / ".context" / "compound-engineering" / "todos"
DEFAULT_MANIFEST_PATH = TODOS_DIR / "current_backlog_sweep.json"
HISTORY_DIR = TODOS_DIR / "history"
PHASE1_TEMPLATE = PROJECT_ROOT / "docs" / "templates" / "phase1-linear-results-template.tsv"
PHASE2_TEMPLATE = PROJECT_ROOT / "docs" / "templates" / "phase2-stopped-results-template.tsv"
PHASE3_TEMPLATE = PROJECT_ROOT / "docs" / "templates" / "phase3-draft-results-template.tsv"
SNAPSHOT_FIELDS = ("id", "company", "role_title", "board", "output_dir")
PHASE1_SNAPSHOT_FIELDS = (
    "linear_issue_id",
    "title",
    "labels",
    "status",
    "related_job_id",
    "related_output_dir",
    "requires_user_input",
    "captured_at_utc",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Date tag for generated files in YYYY-MM-DD format. Defaults to the current UTC date.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing active sweep manifest and generated files.")
    parser.add_argument(
        "--new-run",
        action="store_true",
        help="Archive any existing active manifest and bootstrap a fresh run using the current repo and queue state.",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH, help="Path to write the active sweep manifest.")
    parser.add_argument(
        "--start-phase",
        choices=("phase1", "phase2", "phase3"),
        help="Materialize the snapshot and results ledger for the requested phase using the active manifest date tag.",
    )
    return parser


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def utc_date_tag() -> str:
    return datetime.now(UTC).date().isoformat()


def utc_run_tag() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


def load_snapshot_rows(status: str) -> list[dict[str, str]]:
    from job_db import init_db, query_jobs

    conn = init_db(PROJECT_ROOT / "jobs.db")
    try:
        rows = query_jobs(conn, status=status, include_archived=False, limit=100000)
    finally:
        conn.close()

    ordered = sorted(rows, key=lambda row: int(row["id"]))
    return [
        {
            "id": str(row["id"]),
            "company": str(row.get("company") or "").strip(),
            "role_title": str(row.get("role_title") or "").strip(),
            "board": str(row.get("board") or "").strip(),
            "output_dir": str(row.get("output_dir") or "").strip(),
        }
        for row in ordered
    ]


def phase_artifact_paths(manifest_path: Path, date_tag: str) -> dict[str, Path]:
    todos_dir = manifest_path.parent
    return {
        "phase1_snapshot": todos_dir / f"phase1-linear-snapshot-{date_tag}.tsv",
        "phase1_results": todos_dir / f"phase1-linear-results-{date_tag}.tsv",
        "phase2_snapshot": todos_dir / f"phase2-stopped-snapshot-{date_tag}.tsv",
        "phase2_results": todos_dir / f"phase2-stopped-results-{date_tag}.tsv",
        "phase3_snapshot": todos_dir / f"phase3-draft-snapshot-{date_tag}.tsv",
        "phase3_results": todos_dir / f"phase3-draft-results-{date_tag}.tsv",
    }


def load_phase1_linear_rows() -> list[dict[str, str]]:
    from sweep_linear_sync import fetch_phase1_linear_todo_rows

    return fetch_phase1_linear_todo_rows()


def _sanitize_tsv_cell(value: object) -> str:
    return str("" if value is None else value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def write_snapshot(path: Path, rows: list[dict[str, str]], *, fields: tuple[str, ...] = SNAPSHOT_FIELDS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "\t".join(fields)
    lines = [header]
    for row in rows:
        lines.append("\t".join(_sanitize_tsv_cell(row.get(field, "")) for field in fields))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_sweep_controller():
    controller_path = PROJECT_ROOT / "scripts" / "sweep_controller.py"
    spec = importlib.util.spec_from_file_location("_sweep_controller", controller_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load sweep controller: {controller_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _phase_results_fields(phase_key: str) -> tuple[str, ...]:
    module = _load_sweep_controller()
    fields = getattr(module, "PHASE_RESULT_FIELDS", {}).get(phase_key)
    if not isinstance(fields, tuple):
        raise RuntimeError(f"Sweep controller missing PHASE_RESULT_FIELDS[{phase_key!r}]")
    return fields


def initialize_results_ledger(path: Path, *, phase_key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "\t".join(_phase_results_fields(phase_key))
    path.write_text(header + "\n", encoding="utf-8")


def checker_command(manifest_path: Path) -> list[str]:
    return ["uv", "run", "python", "scripts/check_backlog_sweep.py", "--manifest", str(manifest_path)]


def verifier_command(manifest_path: Path) -> list[str]:
    return ["uv", "run", "python", "scripts/verify_active_sweep.py", "--manifest", str(manifest_path)]


def ensure_destinations_clear(paths: list[Path], *, force: bool) -> None:
    existing = [path for path in paths if path.exists()]
    if existing and not force:
        joined = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"Backlog sweep files already exist: {joined}")


def load_manifest(path: Path) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(f"Missing manifest: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _run_git_command(args: list[str]) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def load_repo_state() -> dict[str, object]:
    try:
        head = _run_git_command(["rev-parse", "HEAD"])
        branch = _run_git_command(["branch", "--show-current"])
        dirty_output = _run_git_command(["status", "--short", "--untracked-files=all"])
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise ValueError(f"Unable to read git state: {exc}") from exc

    dirty_paths = [line.strip() for line in dirty_output.splitlines() if line.strip()]
    payload: dict[str, object] = {
        "head": head,
        "branch": branch,
        "dirty_paths_count": len(dirty_paths),
    }
    if dirty_paths:
        payload["dirty_paths_preview"] = dirty_paths[:20]
    return payload


def load_job_status_counts() -> dict[str, int]:
    db_path = PROJECT_ROOT / "jobs.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Missing jobs database: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "select status, count(*) from jobs where archived = 0 group by status order by status"
        ).fetchall()
    finally:
        conn.close()

    return {str(status): int(count) for status, count in rows}


def history_dir_for(manifest_path: Path) -> Path:
    if manifest_path == DEFAULT_MANIFEST_PATH:
        return HISTORY_DIR
    return manifest_path.parent / "history"


def archive_active_manifest(manifest_path: Path) -> Path | None:
    if not manifest_path.exists():
        return None
    archive_dir = history_dir_for(manifest_path)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{manifest_path.stem}-{utc_run_tag()}.json"
    shutil.move(str(manifest_path), archive_path)
    return archive_path


def bootstrap_manifest(manifest_path: Path, *, date_tag: str, force: bool, new_run: bool) -> dict[str, object]:
    archived_manifest: Path | None = None
    if new_run:
        archived_manifest = archive_active_manifest(manifest_path)
    else:
        ensure_destinations_clear([manifest_path], force=force)

    repo_state = load_repo_state()
    job_status_counts = load_job_status_counts()
    run_id = utc_run_tag()
    payload = {
        "active_manifest_version": 1,
        "run_id": run_id,
        "created_at_utc": utc_now_iso(),
        "date_tag": date_tag,
        "repo_state": repo_state,
        "job_status_counts": job_status_counts,
        "checker_command": checker_command(manifest_path),
        "verifier_command": verifier_command(manifest_path),
    }
    if archived_manifest is not None:
        payload["previous_manifest_archive"] = str(archived_manifest)
    write_manifest(manifest_path, payload)
    return payload


def start_phase(
    manifest_path: Path,
    *,
    phase_key: str,
    force: bool,
) -> tuple[dict[str, object], Path, Path, list[dict[str, str]]]:
    payload = load_manifest(manifest_path)
    date_tag = str(payload.get("date_tag") or "").strip()
    if not date_tag:
        raise ValueError(f"{manifest_path} is missing date_tag")

    paths = phase_artifact_paths(manifest_path, date_tag)
    snapshot_path = paths[f"{phase_key}_snapshot"]
    results_path = paths[f"{phase_key}_results"]
    ensure_destinations_clear([snapshot_path, results_path], force=force)

    if phase_key == "phase1":
        rows = load_phase1_linear_rows()
        snapshot_fields = PHASE1_SNAPSHOT_FIELDS
    elif phase_key == "phase2":
        rows = load_snapshot_rows("stopped")
        snapshot_fields = SNAPSHOT_FIELDS
    else:
        rows = load_snapshot_rows("draft")
        snapshot_fields = SNAPSHOT_FIELDS

    write_snapshot(snapshot_path, rows, fields=snapshot_fields)
    initialize_results_ledger(results_path, phase_key=phase_key)

    payload[f"{phase_key}_snapshot"] = str(snapshot_path)
    payload[f"{phase_key}_results"] = str(results_path)
    payload[f"{phase_key}_snapshot_count"] = len(rows)
    payload[f"{phase_key}_started_at_utc"] = utc_now_iso()
    payload[f"{phase_key}_repo_state"] = load_repo_state()
    payload[f"{phase_key}_job_status_counts"] = load_job_status_counts()
    payload["checker_command"] = checker_command(manifest_path)
    payload["verifier_command"] = verifier_command(manifest_path)
    write_manifest(manifest_path, payload)
    return payload, snapshot_path, results_path, rows


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    manifest_path = args.manifest

    try:
        if args.start_phase:
            payload, snapshot_path, results_path, rows = start_phase(
                manifest_path,
                phase_key=args.start_phase,
                force=args.force,
            )
        else:
            if args.new_run:
                date_tag = args.date or utc_run_tag()
            else:
                date_tag = args.date or utc_date_tag()
            payload = bootstrap_manifest(manifest_path, date_tag=date_tag, force=args.force, new_run=args.new_run)
    except (FileExistsError, FileNotFoundError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1

    if args.start_phase:
        print(f"Started {args.start_phase} backlog sweep phase from manifest: {manifest_path}")
        print(f"  Snapshot: {snapshot_path} ({len(rows)} rows)")
        print(f"  Results:  {results_path}")
    else:
        print(f"Initialized backlog sweep manifest: {manifest_path}")
        if payload.get("previous_manifest_archive"):
            print(f"  Archived previous active manifest: {payload['previous_manifest_archive']}")
        repo_state = payload.get("repo_state", {})
        if isinstance(repo_state, dict):
            print(
                "  Repo state: "
                f"head={repo_state.get('head', '')} "
                f"branch={repo_state.get('branch', '')} "
                f"dirty_paths={repo_state.get('dirty_paths_count', 0)}"
            )
        job_status_counts = payload.get("job_status_counts", {})
        if isinstance(job_status_counts, dict) and job_status_counts:
            summary = ", ".join(f"{key}={job_status_counts[key]}" for key in sorted(job_status_counts))
            print(f"  Job status counts: {summary}")
    print(f"  Checker: {' '.join(payload['checker_command'])}")
    print(f"  Verify:  {' '.join(payload['verifier_command'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
