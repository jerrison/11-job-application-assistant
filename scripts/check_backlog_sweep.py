#!/usr/bin/env python3
"""Validate backlog sweep coverage against immutable snapshot TSVs.

Usage:
    uv run python scripts/check_backlog_sweep.py \
        --phase2-snapshot path/to/phase2-snapshot.tsv \
        --phase2-results path/to/phase2-results.tsv \
        --phase3-snapshot path/to/phase3-snapshot.tsv \
        --phase3-results path/to/phase3-results.tsv

The script exits non-zero unless every snapshot row has a valid latest result row.
Results ledgers are append-friendly: if the same job id appears multiple times,
the last row wins and the script reports the duplicate count.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST_PATH = PROJECT_ROOT / ".context" / "compound-engineering" / "todos" / "current_backlog_sweep.json"
BROWSER_HANDLED_VIA = frozenset(("draft_web_browser", "job_web_browser", "playwright_browser"))
PHASE_ROW_ID_FIELD = {"phase1": "linear_issue_id", "phase2": "id", "phase3": "id"}
PHASE_REQUIRED_SNAPSHOT_COLUMNS = {
    "phase1": ("linear_issue_id",),
    "phase2": ("id",),
    "phase3": ("id",),
}
PHASE_REQUIRED_RESULTS_COLUMNS = {
    "phase1": (
        "linear_issue_id",
        "outcome",
        "handled_via",
        "review_trace_path",
        "artifact_manifest_path",
        "proof_generated_at_utc",
        "repair_wave_fingerprint",
        "linear_sync_status",
        "linear_sync_payload_path",
    ),
    "phase2": (
        "id",
        "outcome",
        "evidence_paths",
        "handled_via",
        "review_trace_path",
        "artifact_manifest_path",
        "proof_generated_at_utc",
        "repair_wave_fingerprint",
        "linear_sync_status",
        "linear_sync_payload_path",
    ),
    "phase3": (
        "id",
        "outcome",
        "evidence_paths",
        "handled_via",
        "review_trace_path",
        "artifact_manifest_path",
        "proof_generated_at_utc",
        "repair_wave_fingerprint",
        "linear_sync_status",
        "linear_sync_payload_path",
    ),
}
PHASE_CURRENT_WAVE_EXEMPT = {
    "phase1": {"blocked_user", "blocked_external", "duplicate_closed", "not_a_bug_closed"},
    "phase2": {"parked_requires_user_input", "terminal_external_confirmed", "duplicate_archived", "unsupported_parked"},
    "phase3": {"parked_requires_user_input", "duplicate_archived"},
}

PHASE_CONFIG: dict[str, dict[str, object]] = {
    "phase1": {
        "title": "Phase 1",
        "allowed_outcomes": (
            "fixed_verified",
            "blocked_user",
            "blocked_external",
            "duplicate_closed",
            "not_a_bug_closed",
        ),
    },
    "phase2": {
        "title": "Phase 2",
        "allowed_outcomes": (
            "fixed_redrafted",
            "parked_requires_user_input",
            "nad_created",
            "duplicate_archived",
            "unsupported_parked",
            "terminal_external_confirmed",
        ),
    },
    "phase3": {
        "title": "Phase 3",
        "allowed_outcomes": (
            "reviewed_ready",
            "fixed_redrafted",
            "parked_requires_user_input",
            "nad_created",
            "duplicate_archived",
        ),
    },
}


@dataclass
class PhaseContext:
    started_at_utc: str | None = None


@dataclass
class PhaseSummary:
    phase_key: str
    title: str
    snapshot_total: int
    latest_total: int
    missing_ids: list[str]
    latest_outcomes: Counter[str]
    duplicate_rows: int
    errors: list[str]

    @property
    def complete(self) -> bool:
        return not self.errors and not self.missing_ids and self.snapshot_total == self.latest_total


def current_repair_wave_fingerprint() -> str:
    path = PROJECT_ROOT / "scripts" / "sweep_repair_wave.py"
    spec = importlib.util.spec_from_file_location("_sweep_repair_wave", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load repair wave module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn = getattr(module, "current_repair_wave_fingerprint", None)
    if fn is None:
        raise RuntimeError(f"Missing current_repair_wave_fingerprint in {path}")
    return str(fn(PROJECT_ROOT))


def _read_tsv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        raise ValueError(f"{path} does not exist")

    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames is None:
            raise ValueError(f"{path} is missing a TSV header row")
        fieldnames = [str(name).strip() for name in reader.fieldnames]
        rows: list[dict[str, str]] = []
        for row in reader:
            normalized = {str(key).strip(): str(value or "").strip() for key, value in row.items() if key is not None}
            if not any(normalized.values()):
                continue
            rows.append(normalized)
    return fieldnames, rows


def _missing_columns(fieldnames: list[str], required: tuple[str, ...]) -> list[str]:
    present = {name.strip() for name in fieldnames}
    return [column for column in required if column not in present]


def _split_evidence_paths(raw_value: str) -> list[str]:
    normalized = raw_value.replace("\r", "\n").replace(";", "|").replace("\n", "|")
    return [part.strip() for part in normalized.split("|") if part.strip()]


def _normalize_local_path(raw_value: str) -> Path:
    path = Path(raw_value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _parse_iso8601(raw_value: str) -> datetime | None:
    cleaned = str(raw_value or "").strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _validate_evidence_paths(
    results_path: Path,
    row_number: int,
    job_id: str,
    raw_value: str,
    *,
    output_dir_raw: str,
    review_dir: Path,
) -> tuple[list[str], list[Path]]:
    evidence_paths = _split_evidence_paths(raw_value)
    if not evidence_paths:
        return [f"{results_path}:{row_number}: missing evidence_paths for id {job_id}"], []

    output_dir = _normalize_local_path(output_dir_raw)
    if not output_dir.exists():
        return [f"{results_path}:{row_number}: output_dir does not exist for id {job_id}: {output_dir_raw}"], []

    errors: list[str] = []
    resolved_paths: list[Path] = []
    for evidence_path in evidence_paths:
        if "://" in evidence_path:
            errors.append(
                f"{results_path}:{row_number}: remote evidence URLs are not allowed for id {job_id}: {evidence_path}"
            )
            continue
        path = _normalize_local_path(evidence_path)
        if not path.exists():
            errors.append(f"{results_path}:{row_number}: evidence path does not exist for id {job_id}: {evidence_path}")
            continue
        if not (_path_is_within(path, output_dir) or _path_is_within(path, review_dir)):
            errors.append(
                f"{results_path}:{row_number}: evidence path for id {job_id} must live under output_dir "
                f"or the review directory: {evidence_path}"
            )
            continue
        resolved_paths.append(path)
    return errors, resolved_paths


def _load_json_file(path: Path, *, label: str, results_path: Path, row_number: int, job_id: str) -> tuple[dict[str, Any] | None, list[str]]:
    if not path.exists():
        return None, [f"{results_path}:{row_number}: {label} does not exist for id {job_id}: {path}"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, [f"{results_path}:{row_number}: {label} is not valid JSON for id {job_id}: {exc}"]
    if not isinstance(payload, dict):
        return None, [f"{results_path}:{row_number}: {label} must contain a JSON object for id {job_id}"]
    return payload, []


def _artifact_paths_from_manifest(payload: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    evidence_paths = payload.get("evidence_paths")
    if isinstance(evidence_paths, list):
        for item in evidence_paths:
            cleaned = str(item or "").strip()
            if cleaned:
                paths.append(cleaned)
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            cleaned = str(artifact.get("path") or "").strip()
            if cleaned:
                paths.append(cleaned)
    return _split_evidence_paths("|".join(paths))


def _validate_trace_payloads(
    phase_key: str,
    results_path: Path,
    row_number: int,
    row: dict[str, str],
    *,
    context: PhaseContext | None,
) -> list[str]:
    row_id = row.get(PHASE_ROW_ID_FIELD[phase_key], "").strip()
    errors: list[str] = []

    review_trace_raw = row.get("review_trace_path", "").strip()
    artifact_manifest_raw = row.get("artifact_manifest_path", "").strip()
    if not review_trace_raw:
        errors.append(f"{results_path}:{row_number}: missing review_trace_path for id {row_id}")
        return errors
    if not artifact_manifest_raw:
        errors.append(f"{results_path}:{row_number}: missing artifact_manifest_path for id {row_id}")
        return errors
    if "://" in review_trace_raw or "://" in artifact_manifest_raw:
        errors.append(f"{results_path}:{row_number}: review trace paths must be local files for id {row_id}")
        return errors

    review_trace_path = _normalize_local_path(review_trace_raw)
    artifact_manifest_path = _normalize_local_path(artifact_manifest_raw)
    review_trace, trace_errors = _load_json_file(
        review_trace_path,
        label="review_trace_path",
        results_path=results_path,
        row_number=row_number,
        job_id=row_id,
    )
    errors.extend(trace_errors)
    artifact_manifest, manifest_errors = _load_json_file(
        artifact_manifest_path,
        label="artifact_manifest_path",
        results_path=results_path,
        row_number=row_number,
        job_id=row_id,
    )
    errors.extend(manifest_errors)
    if review_trace is None or artifact_manifest is None:
        return errors

    if str(review_trace.get("phase") or "").strip() != phase_key:
        errors.append(f"{results_path}:{row_number}: review trace phase mismatch for id {row_id}")
    if str(review_trace.get("job_id") or "").strip() != row_id:
        errors.append(f"{results_path}:{row_number}: review trace job_id mismatch for id {row_id}")
    if str(review_trace.get("outcome") or "").strip() != row.get("outcome", "").strip():
        errors.append(f"{results_path}:{row_number}: review trace outcome mismatch for id {row_id}")
    if str(review_trace.get("handled_via") or "").strip() != row.get("handled_via", "").strip():
        errors.append(f"{results_path}:{row_number}: review trace handled_via mismatch for id {row_id}")

    if str(artifact_manifest.get("phase") or "").strip() != phase_key:
        errors.append(f"{results_path}:{row_number}: artifact manifest phase mismatch for id {row_id}")

    if phase_key == "phase1":
        if str(artifact_manifest.get("linear_issue_id") or "").strip() != row_id:
            errors.append(f"{results_path}:{row_number}: artifact manifest linear_issue_id mismatch for id {row_id}")
        if str(artifact_manifest.get("related_job_id") or "").strip() != row.get("related_job_id", "").strip():
            errors.append(f"{results_path}:{row_number}: artifact manifest related_job_id mismatch for id {row_id}")
        if str(artifact_manifest.get("related_output_dir") or "").strip() != row.get("related_output_dir", "").strip():
            errors.append(f"{results_path}:{row_number}: artifact manifest related_output_dir mismatch for id {row_id}")
    else:
        review_dir = review_trace_path.parent
        evidence_errors, resolved_evidence = _validate_evidence_paths(
            results_path,
            row_number,
            row_id,
            row.get("evidence_paths", ""),
            output_dir_raw=row.get("output_dir", ""),
            review_dir=review_dir,
        )
        errors.extend(evidence_errors)
        if int(review_trace.get("artifacts_reviewed_count") or 0) < 1:
            errors.append(f"{results_path}:{row_number}: review trace must record reviewed artifacts for id {row_id}")
        if str(artifact_manifest.get("job_id") or "").strip() != row_id:
            errors.append(f"{results_path}:{row_number}: artifact manifest job_id mismatch for id {row_id}")
        if str(artifact_manifest.get("output_dir") or "").strip() != row.get("output_dir", "").strip():
            errors.append(f"{results_path}:{row_number}: artifact manifest output_dir mismatch for id {row_id}")

        manifest_paths = set(_artifact_paths_from_manifest(artifact_manifest))
        row_evidence_paths = set(_split_evidence_paths(row.get("evidence_paths", "")))
        if row_evidence_paths and not row_evidence_paths.issubset(manifest_paths):
            errors.append(f"{results_path}:{row_number}: artifact manifest must include all evidence_paths for id {row_id}")

    proof_generated_at_utc = row.get("proof_generated_at_utc", "").strip()
    proof_generated_at = _parse_iso8601(proof_generated_at_utc)
    if proof_generated_at is None:
        errors.append(f"{results_path}:{row_number}: invalid proof_generated_at_utc for id {row_id}: {proof_generated_at_utc}")
    trace_proof_generated = _parse_iso8601(str(review_trace.get("proof_generated_at_utc") or ""))
    if trace_proof_generated is None:
        errors.append(f"{results_path}:{row_number}: review trace missing proof_generated_at_utc for id {row_id}")
    elif proof_generated_at is not None and trace_proof_generated != proof_generated_at:
        errors.append(f"{results_path}:{row_number}: proof_generated_at_utc mismatch between row and trace for id {row_id}")
    if context and context.started_at_utc:
        phase_started_at = _parse_iso8601(context.started_at_utc)
        if phase_started_at is None:
            errors.append(f"{results_path}:{row_number}: invalid phase start timestamp in manifest for {phase_key}")
        elif proof_generated_at is not None and proof_generated_at < phase_started_at:
            errors.append(
                f"{results_path}:{row_number}: proof_generated_at_utc predates the phase start for id {row_id}"
            )

    handled_via = row.get("handled_via", "").strip()
    if phase_key == "phase3" and row.get("outcome", "").strip() == "reviewed_ready":
        if handled_via not in BROWSER_HANDLED_VIA:
            allowed = ", ".join(sorted(BROWSER_HANDLED_VIA))
            errors.append(
                f"{results_path}:{row_number}: Phase 3 reviewed_ready requires browser handled_via for id {row_id}. "
                f"Allowed values: {allowed}"
            )
        if str(review_trace.get("review_kind") or "").strip() != "manual_browser_review":
            errors.append(
                f"{results_path}:{row_number}: Phase 3 reviewed_ready requires a manual_browser_review trace for id {row_id}"
            )

    if phase_key != "phase1":
        trace_reviewed = review_trace.get("artifacts_reviewed")
        if isinstance(trace_reviewed, list):
            trace_paths = {str(item or "").strip() for item in trace_reviewed if str(item or "").strip()}
            if row_evidence_paths and not row_evidence_paths.issubset(trace_paths):
                errors.append(f"{results_path}:{row_number}: review trace must include all evidence_paths for id {row_id}")
        if not resolved_evidence:
            errors.append(f"{results_path}:{row_number}: no valid local evidence paths remain for id {row_id}")

    return errors


def _load_snapshot_ids(
    path: Path,
    *,
    id_field: str,
    required_columns: tuple[str, ...],
) -> tuple[list[dict[str, str]], list[str]]:
    fieldnames, rows = _read_tsv(path)
    missing = _missing_columns(fieldnames, required_columns)
    if missing:
        raise ValueError(f"{path} is missing required snapshot columns: {', '.join(missing)}")

    errors: list[str] = []
    seen: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        row_id = row.get(id_field, "").strip()
        if not row_id:
            errors.append(f"{path}:{row_number}: missing snapshot row id in column {id_field}")
            continue
        if row_id in seen:
            errors.append(f"{path}:{row_number}: duplicate snapshot id {row_id}")
            continue
        seen.add(row_id)
    if errors:
        raise ValueError("\n".join(errors))
    return rows, sorted(seen, key=lambda value: int(value) if value.isdigit() else value)


def _check_phase(
    phase_key: str,
    snapshot_path: Path,
    results_path: Path,
    *,
    context: PhaseContext | None = None,
) -> PhaseSummary:
    config = PHASE_CONFIG[phase_key]
    title = str(config["title"])
    allowed_outcomes = set(config["allowed_outcomes"])
    current_wave = current_repair_wave_fingerprint()
    id_field = PHASE_ROW_ID_FIELD[phase_key]

    snapshot_rows, snapshot_ids = _load_snapshot_ids(
        snapshot_path,
        id_field=id_field,
        required_columns=PHASE_REQUIRED_SNAPSHOT_COLUMNS[phase_key],
    )
    snapshot_id_set = set(snapshot_ids)

    result_fieldnames, result_rows = _read_tsv(results_path)
    missing_result_columns = _missing_columns(result_fieldnames, PHASE_REQUIRED_RESULTS_COLUMNS[phase_key])
    if missing_result_columns:
        return PhaseSummary(
            phase_key=phase_key,
            title=title,
            snapshot_total=len(snapshot_rows),
            latest_total=0,
            missing_ids=list(snapshot_ids),
            latest_outcomes=Counter(),
            duplicate_rows=0,
            errors=[f"{results_path} is missing required result columns: {', '.join(missing_result_columns)}"],
        )

    errors: list[str] = []
    latest_by_id: dict[str, tuple[int, dict[str, str]]] = {}
    duplicate_rows = 0

    for row_number, row in enumerate(result_rows, start=2):
        row_id = row.get(id_field, "").strip()
        if not row_id:
            errors.append(f"{results_path}:{row_number}: missing result row id in column {id_field}")
            continue
        if row_id not in snapshot_id_set:
            errors.append(f"{results_path}:{row_number}: result id {row_id} is not present in snapshot")
            continue
        if row_id in latest_by_id:
            duplicate_rows += 1
        latest_by_id[row_id] = (row_number, row)

    missing_ids = [row_id for row_id in snapshot_ids if row_id not in latest_by_id]
    latest_outcomes: Counter[str] = Counter()
    for row_id, (row_number, row) in latest_by_id.items():
        outcome = row.get("outcome", "").strip()
        if not outcome:
            errors.append(f"{results_path}:{row_number}: missing outcome for id {row_id}")
            continue
        if outcome not in allowed_outcomes:
            allowed = ", ".join(sorted(allowed_outcomes))
            errors.append(
                f"{results_path}:{row_number}: invalid outcome '{outcome}' for {title} id {row_id}. "
                f"Allowed outcomes: {allowed}"
            )
            continue
        if row.get("repair_wave_fingerprint", "").strip() != current_wave and outcome not in PHASE_CURRENT_WAVE_EXEMPT[phase_key]:
            errors.append(f"{results_path}:{row_number}: stale repair_wave_fingerprint for id {row_id}")
        if row.get("linear_sync_status", "").strip() != "synced":
            errors.append(f"{results_path}:{row_number}: latest row for id {row_id} is not synced to Linear")
        errors.extend(_validate_trace_payloads(phase_key, results_path, row_number, row, context=context))
        latest_outcomes[outcome] += 1

    return PhaseSummary(
        phase_key=phase_key,
        title=title,
        snapshot_total=len(snapshot_rows),
        latest_total=len(latest_by_id),
        missing_ids=missing_ids,
        latest_outcomes=latest_outcomes,
        duplicate_rows=duplicate_rows,
        errors=errors,
    )


def _format_outcomes(counter: Counter[str]) -> str:
    if not counter:
        return "-"
    return ", ".join(f"{name}={counter[name]}" for name in sorted(counter))


def _print_summary(summary: PhaseSummary) -> None:
    print(f"{summary.title} ({summary.phase_key})")
    print(f"  snapshot rows: {summary.snapshot_total}")
    print(f"  latest covered rows: {summary.latest_total}")
    print(f"  duplicate result rows: {summary.duplicate_rows}")
    print(f"  latest outcomes: {_format_outcomes(summary.latest_outcomes)}")

    if summary.errors:
        for error in summary.errors:
            print(f"  error: {error}")

    if summary.missing_ids:
        joined = ", ".join(summary.missing_ids)
        print(f"  missing snapshot ids: {joined}")

    status = "complete" if summary.complete else "incomplete"
    print(f"  status: {status}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, help="JSON manifest containing phase snapshot/results paths.")
    parser.add_argument(
        "--active",
        action="store_true",
        help=f"Load the active sweep manifest from {DEFAULT_MANIFEST_PATH.relative_to(PROJECT_ROOT)}.",
    )
    parser.add_argument("--phase1-snapshot", type=Path, help="Immutable Phase 1 Linear Todo snapshot TSV.")
    parser.add_argument("--phase1-results", type=Path, help="Phase 1 Linear Todo results ledger TSV.")
    parser.add_argument("--phase2-snapshot", type=Path, help="Immutable Phase 2 stopped-job snapshot TSV.")
    parser.add_argument("--phase2-results", type=Path, help="Phase 2 stopped-job results ledger TSV.")
    parser.add_argument("--phase3-snapshot", type=Path, help="Immutable Phase 3 draft snapshot TSV.")
    parser.add_argument("--phase3-results", type=Path, help="Phase 3 draft results ledger TSV.")
    return parser


def _load_manifest(path: Path) -> dict[str, object]:
    if not path.exists():
        raise ValueError(f"{path} does not exist")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _validate_phase_args(args: argparse.Namespace) -> list[tuple[str, Path, Path, PhaseContext | None]]:
    if args.active and args.manifest:
        raise ValueError("Use only one of --active or --manifest")

    manifest_path: Path | None = None
    if args.active:
        manifest_path = DEFAULT_MANIFEST_PATH
    elif args.manifest:
        manifest_path = args.manifest

    if manifest_path is not None:
        payload = _load_manifest(manifest_path)
        requested: list[tuple[str, Path, Path, PhaseContext | None]] = []
        for phase_key in ("phase1", "phase2", "phase3"):
            snapshot_raw = payload.get(f"{phase_key}_snapshot")
            results_raw = payload.get(f"{phase_key}_results")
            if bool(snapshot_raw) != bool(results_raw):
                raise ValueError(
                    f"{manifest_path} must define both {phase_key}_snapshot and {phase_key}_results together"
                )
            if snapshot_raw and results_raw:
                requested.append(
                    (
                        phase_key,
                        Path(str(snapshot_raw)),
                        Path(str(results_raw)),
                        PhaseContext(started_at_utc=str(payload.get(f"{phase_key}_started_at_utc") or "").strip() or None),
                    )
                )
        if not requested:
            raise ValueError(f"{manifest_path} does not define any phase snapshot/results pairs")
        return requested

    requested: list[tuple[str, Path, Path, PhaseContext | None]] = []
    for phase_key in ("phase1", "phase2", "phase3"):
        snapshot = getattr(args, f"{phase_key}_snapshot")
        results = getattr(args, f"{phase_key}_results")
        if bool(snapshot) != bool(results):
            raise ValueError(f"{phase_key} requires both --{phase_key}-snapshot and --{phase_key}-results")
        if snapshot and results:
            requested.append((phase_key, snapshot, results, None))
    if not requested:
        raise ValueError("Provide at least one snapshot/results pair, --manifest, or --active")
    return requested


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        requested = _validate_phase_args(args)
    except ValueError as exc:
        parser.error(str(exc))

    summaries: list[PhaseSummary] = []
    failed_to_load = False

    for phase_key, snapshot_path, results_path, context in requested:
        try:
            summary = _check_phase(phase_key, snapshot_path, results_path, context=context)
        except ValueError as exc:
            failed_to_load = True
            config = PHASE_CONFIG[phase_key]
            summary = PhaseSummary(
                phase_key=phase_key,
                title=str(config["title"]),
                snapshot_total=0,
                latest_total=0,
                missing_ids=[],
                latest_outcomes=Counter(),
                duplicate_rows=0,
                errors=[str(exc)],
            )
        summaries.append(summary)

    overall_complete = not failed_to_load and all(summary.complete for summary in summaries)

    for index, summary in enumerate(summaries):
        if index:
            print()
        _print_summary(summary)

    print()
    if overall_complete:
        print("Backlog sweep check passed: every requested snapshot row has a valid latest result.")
        return 0

    print("Backlog sweep check FAILED: completion is forbidden until every snapshot row is covered.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
