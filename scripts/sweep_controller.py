#!/usr/bin/env python3
"""Shared state machine for recording backlog sweep snapshot transitions."""

from __future__ import annotations

import csv
import importlib.util
import json
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST_PATH = PROJECT_ROOT / ".context" / "compound-engineering" / "todos" / "current_backlog_sweep.json"
TRACE_ROOT_DIRNAME = "backlog-sweep-proof"
BROWSER_HANDLED_VIA = frozenset(("draft_web_browser", "job_web_browser", "playwright_browser"))
LINEAR_SYNC_STATUSES = frozenset(("pending", "synced", "drifted"))

SNAPSHOT_FIELDS = ("id", "company", "role_title", "board", "output_dir")

PHASE_ALLOWED_OUTCOMES: dict[str, tuple[str, ...]] = {
    "phase1": (
        "fixed_verified",
        "blocked_user",
        "blocked_external",
        "duplicate_closed",
        "not_a_bug_closed",
    ),
    "phase2": (
        "fixed_redrafted",
        "parked_requires_user_input",
        "nad_created",
        "duplicate_archived",
        "unsupported_parked",
        "terminal_external_confirmed",
    ),
    "phase3": (
        "reviewed_ready",
        "fixed_redrafted",
        "parked_requires_user_input",
        "nad_created",
        "duplicate_archived",
    ),
}

PHASE_ROW_ID_FIELD: dict[str, str] = {"phase1": "linear_issue_id", "phase2": "id", "phase3": "id"}

PHASE_RESULT_FIELDS: dict[str, tuple[str, ...]] = {
    "phase1": (
        "handled_at_utc",
        "linear_issue_id",
        "title",
        "labels",
        "status",
        "related_job_id",
        "related_output_dir",
        "outcome",
        "handled_via",
        "review_trace_path",
        "artifact_manifest_path",
        "proof_generated_at_utc",
        "repair_wave_fingerprint",
        "linear_sync_status",
        "linear_sync_payload_path",
        "notes",
    ),
    "phase2": (
        "handled_at_utc",
        "id",
        "company",
        "role_title",
        "board",
        "output_dir",
        "outcome",
        "issue_id",
        "evidence_paths",
        "handled_via",
        "review_trace_path",
        "artifact_manifest_path",
        "proof_generated_at_utc",
        "repair_wave_fingerprint",
        "linear_sync_status",
        "linear_sync_payload_path",
        "notes",
    ),
    "phase3": (
        "handled_at_utc",
        "id",
        "company",
        "role_title",
        "board",
        "output_dir",
        "outcome",
        "issue_id",
        "evidence_paths",
        "handled_via",
        "review_trace_path",
        "artifact_manifest_path",
        "proof_generated_at_utc",
        "repair_wave_fingerprint",
        "linear_sync_status",
        "linear_sync_payload_path",
        "notes",
    ),
}


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _current_repair_wave_fingerprint() -> str:
    """Load the fingerprint helper by path.

    `scripts/` is not a Python package, and tests load modules via explicit file
    paths, so we avoid relying on import-time `sys.path` behavior here.
    """

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


def _linear_sync_backend():
    path = PROJECT_ROOT / "scripts" / "sweep_linear_sync.py"
    spec = importlib.util.spec_from_file_location("_sweep_linear_sync", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load linear sync module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    queue = getattr(module, "queue_sync_payload", None)
    if queue is None:
        raise RuntimeError(f"Missing queue_sync_payload in {path}")
    return module, queue


def _pending_sync_dir(manifest_path: Path) -> Path:
    base = manifest_path.parent
    if base.name == "todos":
        base = base.parent
    return base / "linear-sync"


def _queue_linear_sync_if_needed(
    *,
    manifest_path: Path,
    phase_key: str,
    row_id: str,
    snapshot_row: dict[str, str],
    outcome: str,
    notes: str,
    linear_sync_status: str,
    linear_sync_payload_path: str,
    issue_id: str,
) -> tuple[str, str]:
    if linear_sync_payload_path:
        return linear_sync_payload_path, linear_sync_status
    if phase_key != "phase1":
        return linear_sync_payload_path, linear_sync_status

    _module, queue_sync_payload = _linear_sync_backend()
    pending_dir = _pending_sync_dir(manifest_path)
    payload_path = queue_sync_payload(
        pending_dir,
        item_id=f"{phase_key}:{row_id}",
        action="sync_phase1_issue_state",
        body={
            "linear_issue_id": snapshot_row.get("linear_issue_id", ""),
            "title": snapshot_row.get("title", ""),
            "outcome": outcome,
            "notes": notes,
        },
    )
    return str(payload_path), "pending"


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _read_tsv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames is None:
            raise ValueError(f"{path} is missing a TSV header row")
        rows = [
            {str(key).strip(): str(value or "").strip() for key, value in row.items() if key is not None}
            for row in reader
            if any(str(value or "").strip() for value in row.values())
        ]
    return [str(name).strip() for name in reader.fieldnames], rows


def _resolve_manifest_path(manifest_path: Path, raw: str) -> Path:
    candidate = Path(str(raw).strip())
    if not candidate.is_absolute():
        return (manifest_path.parent / candidate).resolve()
    return candidate


def _phase_paths(manifest_path: Path, manifest: dict[str, Any], phase_key: str) -> tuple[Path, Path]:
    snapshot_raw = str(manifest.get(f"{phase_key}_snapshot") or "").strip()
    results_raw = str(manifest.get(f"{phase_key}_results") or "").strip()
    if not snapshot_raw or not results_raw:
        raise ValueError(f"{manifest_path} is missing {phase_key} snapshot/results paths")
    return _resolve_manifest_path(manifest_path, snapshot_raw), _resolve_manifest_path(manifest_path, results_raw)


def _normalize_paths(values: Sequence[str | Path]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        cleaned = str(raw).strip()
        if not cleaned or cleaned in seen:
            continue
        normalized.append(cleaned)
        seen.add(cleaned)
    return normalized


def _artifact_entry(kind: str, path: Path) -> dict[str, str]:
    return {"kind": kind, "path": str(path)}


def _gather_current_artifacts(output_dir: Path, board_name: str | None) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    seen_paths: set[Path] = set()

    def add(kind: str, path: Path | None) -> None:
        if path is None:
            return
        candidate = Path(path)
        if not candidate.exists() or candidate in seen_paths:
            return
        seen_paths.add(candidate)
        artifacts.append(_artifact_entry(kind, candidate))

    add("draft_summary_screenshot", output_dir / "draft_summary.png")
    add("draft_summary_markdown", output_dir / "draft_summary.md")

    try:
        from submit_review_common import resolve_current_submit_artifacts
    except ImportError:
        resolve_current_submit_artifacts = None

    if resolve_current_submit_artifacts is not None:
        resolved = resolve_current_submit_artifacts(output_dir, board_name=board_name)
        for key in (
            "report_json",
            "report_md",
            "pre_submit_screenshot",
            "review_screenshot",
            "post_submit_screenshot",
            "submit_debug_screenshot",
            "payload_json",
        ):
            raw = resolved.get(key)
            if isinstance(raw, Path):
                add(key, raw)

    submit_dir = output_dir / "submit"
    if submit_dir.exists():
        for pattern in ("*.png", "*.jpg", "*.jpeg", "*.json", "*.md"):
            for path in sorted(submit_dir.glob(pattern)):
                add(f"submit:{path.name}", path)

    return artifacts


def _default_evidence_paths(artifacts: list[dict[str, str]]) -> list[str]:
    by_kind = {artifact["kind"]: artifact["path"] for artifact in artifacts}
    ordered_keys = (
        "pre_submit_screenshot",
        "review_screenshot",
        "post_submit_screenshot",
        "draft_summary_screenshot",
        "submit_debug_screenshot",
    )
    selected = [by_kind[key] for key in ordered_keys if key in by_kind]
    if selected:
        return _normalize_paths(selected)
    fallback = [artifact["path"] for artifact in artifacts if artifact["path"].lower().endswith((".png", ".jpg", ".jpeg"))]
    return _normalize_paths(fallback)


def _validate_evidence_paths(values: Sequence[str | Path], *, output_dir: Path) -> list[str]:
    normalized = _normalize_paths(values)
    output_root = output_dir.resolve()
    allowed_suffixes = {".png", ".jpg", ".jpeg"}
    validated: list[str] = []
    for raw in normalized:
        candidate = Path(raw).resolve()
        if not candidate.is_file():
            raise ValueError(f"Evidence path does not exist or is not a file: {candidate}")
        if output_root not in (candidate, *candidate.parents):
            raise ValueError(f"Evidence path must live under the job output directory: {candidate}")
        if candidate.suffix.lower() not in allowed_suffixes:
            raise ValueError(f"Evidence path must be a screenshot file under the job output directory: {candidate}")
        validated.append(str(candidate))
    return validated


def _safe_path_component(value: str) -> str:
    return value.replace("/", "_").replace("\\", "_").replace("..", "__")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _snapshot_row(snapshot_path: Path, row_id: str, *, id_field: str = "id") -> dict[str, str]:
    _, rows = _read_tsv_rows(snapshot_path)
    for row in rows:
        if row.get(id_field, "").strip() == row_id:
            return row
    raise ValueError(f"{snapshot_path} does not contain job id {row_id}")


def _append_results_row(results_path: Path, fieldnames: tuple[str, ...], row: dict[str, str]) -> None:
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with results_path.open("a+", encoding="utf-8", newline="") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        header_line = handle.readline().strip()
        expected_header = list(fieldnames)
        if header_line:
            existing_header = [part.strip() for part in header_line.split("\t")]
            if existing_header != expected_header:
                raise ValueError(
                    f"{results_path} header does not match expected controller fields for this phase"
                )
        handle.seek(0, 2)
        file_is_empty = handle.tell() == 0
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), delimiter="\t")
        if file_is_empty:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fieldnames})
        handle.flush()
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def record_transition(
    *,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    phase_key: str,
    row_id: str,
    outcome: str,
    handled_via: str,
    issue_id: str = "",
    notes: str = "",
    evidence_paths: Sequence[str | Path] | None = None,
    detail_json: Mapping[str, Any] | None = None,
    proof_generated_at_utc: str | None = None,
    repair_wave_fingerprint: str | None = None,
    linear_sync_status: str = "pending",
    linear_sync_payload_path: str = "",
) -> dict[str, str]:
    manifest_path = Path(manifest_path)
    manifest = _load_manifest(manifest_path)
    phase_key = str(phase_key).strip()
    row_id = str(row_id).strip()
    outcome = str(outcome).strip()
    handled_via = str(handled_via).strip()
    issue_id = str(issue_id).strip()
    notes = str(notes).strip()
    linear_sync_status = str(linear_sync_status).strip() or "pending"
    linear_sync_payload_path = str(linear_sync_payload_path).strip()

    if phase_key not in PHASE_ALLOWED_OUTCOMES:
        allowed = ", ".join(sorted(PHASE_ALLOWED_OUTCOMES))
        raise ValueError(f"Unsupported phase_key '{phase_key}'. Allowed values: {allowed}")
    if outcome not in PHASE_ALLOWED_OUTCOMES[phase_key]:
        allowed = ", ".join(PHASE_ALLOWED_OUTCOMES[phase_key])
        raise ValueError(f"Unsupported outcome '{outcome}' for {phase_key}. Allowed values: {allowed}")
    if not handled_via:
        raise ValueError("handled_via is required")
    if linear_sync_status not in LINEAR_SYNC_STATUSES:
        allowed = ", ".join(sorted(LINEAR_SYNC_STATUSES))
        raise ValueError(f"Unsupported linear_sync_status '{linear_sync_status}'. Allowed values: {allowed}")
    if phase_key == "phase3" and outcome == "reviewed_ready" and handled_via not in BROWSER_HANDLED_VIA:
        allowed = ", ".join(sorted(BROWSER_HANDLED_VIA))
        raise ValueError(f"Phase 3 reviewed_ready requires browser handled_via. Allowed values: {allowed}")

    snapshot_path, results_path = _phase_paths(manifest_path, manifest, phase_key)
    id_field = PHASE_ROW_ID_FIELD.get(phase_key, "id")
    snapshot_row = _snapshot_row(snapshot_path, row_id, id_field=id_field)

    artifacts: list[dict[str, str]] = []
    chosen_evidence: list[str] = []
    if phase_key != "phase1":
        output_dir = Path(str(snapshot_row.get("output_dir") or "").strip())
        if not output_dir.is_dir():
            raise ValueError(f"Output directory is missing or not a directory for job {row_id}: {output_dir}")
        artifacts = _gather_current_artifacts(output_dir, board_name=snapshot_row.get("board") or None)
        chosen_evidence = _validate_evidence_paths(
            evidence_paths or _default_evidence_paths(artifacts),
            output_dir=output_dir,
        )
        if not chosen_evidence:
            raise ValueError(f"No current screenshot evidence found for job {row_id}. Provide evidence_paths explicitly.")

    repair_wave = str(repair_wave_fingerprint or _current_repair_wave_fingerprint()).strip()
    proof_generated_at = str(proof_generated_at_utc or utc_now_iso()).strip()
    handled_at_utc = utc_now_iso()

    run_id = str(manifest.get("run_id") or "adhoc-run").strip() or "adhoc-run"
    review_root = (
        manifest_path.parent
        / TRACE_ROOT_DIRNAME
        / _safe_path_component(run_id)
        / _safe_path_component(phase_key)
        / _safe_path_component(row_id)
    )
    artifact_manifest_path = review_root / "artifact-manifest.json"
    review_trace_path = review_root / "review-trace.json"

    if phase_key == "phase1":
        artifact_manifest: dict[str, Any] = {
            "phase": phase_key,
            "linear_issue_id": snapshot_row.get("linear_issue_id", ""),
            "related_job_id": snapshot_row.get("related_job_id", ""),
            "related_output_dir": snapshot_row.get("related_output_dir", ""),
            "generated_at_utc": proof_generated_at,
            "artifacts": [],
        }
    else:
        artifact_manifest = {
            "phase": phase_key,
            "job_id": row_id,
            "output_dir": str(snapshot_row.get("output_dir", "")),
            "generated_at_utc": proof_generated_at,
            "evidence_paths": chosen_evidence,
            "artifacts": artifacts,
        }
    review_trace = {
        "phase": phase_key,
        "job_id": row_id,
        "outcome": outcome,
        "handled_via": handled_via,
        "review_kind": "manual_browser_review" if handled_via in BROWSER_HANDLED_VIA else "manual_row_review",
        "proof_generated_at_utc": proof_generated_at,
        "handled_at_utc": handled_at_utc,
        "issue_id": issue_id,
        "notes": notes,
        "artifacts_reviewed": chosen_evidence,
        "artifacts_reviewed_count": len(chosen_evidence),
        "detail": dict(detail_json or {}),
    }
    _write_json(artifact_manifest_path, artifact_manifest)
    _write_json(review_trace_path, review_trace)

    queued_payload_path, effective_sync_status = _queue_linear_sync_if_needed(
        manifest_path=manifest_path,
        phase_key=phase_key,
        row_id=row_id,
        snapshot_row=snapshot_row,
        outcome=outcome,
        notes=notes,
        linear_sync_status=linear_sync_status,
        linear_sync_payload_path=linear_sync_payload_path,
        issue_id=issue_id,
    )
    if phase_key == "phase1":
        row: dict[str, str] = {
            "handled_at_utc": handled_at_utc,
            "linear_issue_id": str(snapshot_row.get("linear_issue_id", "")),
            "title": str(snapshot_row.get("title", "")),
            "labels": str(snapshot_row.get("labels", "")),
            "status": str(snapshot_row.get("status", "")),
            "related_job_id": str(snapshot_row.get("related_job_id", "")),
            "related_output_dir": str(snapshot_row.get("related_output_dir", "")),
            "outcome": outcome,
            "handled_via": handled_via,
            "review_trace_path": str(review_trace_path),
            "artifact_manifest_path": str(artifact_manifest_path),
            "proof_generated_at_utc": proof_generated_at,
            "repair_wave_fingerprint": repair_wave,
            "linear_sync_status": effective_sync_status,
            "linear_sync_payload_path": queued_payload_path,
            "notes": notes,
        }
    else:
        row = {
            "handled_at_utc": handled_at_utc,
            "id": row_id,
            "company": str(snapshot_row.get("company", "")),
            "role_title": str(snapshot_row.get("role_title", "")),
            "board": str(snapshot_row.get("board", "")),
            "output_dir": str(snapshot_row.get("output_dir", "")),
            "outcome": outcome,
            "issue_id": issue_id,
            "evidence_paths": "|".join(chosen_evidence),
            "handled_via": handled_via,
            "review_trace_path": str(review_trace_path),
            "artifact_manifest_path": str(artifact_manifest_path),
            "proof_generated_at_utc": proof_generated_at,
            "repair_wave_fingerprint": repair_wave,
            "linear_sync_status": effective_sync_status,
            "linear_sync_payload_path": queued_payload_path,
            "notes": notes,
        }
    try:
        _append_results_row(results_path, PHASE_RESULT_FIELDS[phase_key], row)
    except Exception:
        for path in (artifact_manifest_path, review_trace_path):
            with suppress(FileNotFoundError, OSError):
                path.unlink()
        raise
    return row
