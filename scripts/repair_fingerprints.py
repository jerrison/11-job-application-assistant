"""Helpers for clustering repairable failures into stable, human-readable fingerprints."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path


def _normalize_text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _slug(value: object, *, max_len: int = 96) -> str:
    normalized = _normalize_text(value)
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    if not slug:
        return "unknown"
    return slug[:max_len].strip("-") or "unknown"


def _coerce_job_ids(raw_value: object) -> list[int]:
    if raw_value is None:
        return []
    if isinstance(raw_value, list):
        values = raw_value
    else:
        try:
            values = json.loads(str(raw_value))
        except (TypeError, json.JSONDecodeError):
            values = []
    job_ids: list[int] = []
    for value in values:
        try:
            job_ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return sorted(set(job_ids))


def _markdown_relpath(base_file: Path, target: Path) -> str:
    try:
        return Path(os.path.relpath(target, start=base_file.parent)).as_posix()
    except ValueError:
        return target.as_posix()


def _infer_output_root(output_dir: str | Path | None) -> Path | None:
    if output_dir is None:
        return None
    path = Path(output_dir).resolve()
    for candidate in (path, *path.parents):
        if candidate.name == "output":
            return candidate
    return None


def build_repair_fingerprint(
    *,
    board: str,
    phase: str,
    failure_type: str,
    message: str,
    field_labels: list[str] | None = None,
) -> str:
    normalized_labels = sorted(_normalize_text(label) for label in (field_labels or []) if _normalize_text(label))
    tail_source = " | ".join(normalized_labels) if normalized_labels else _normalize_text(message)[:160]
    return ":".join(
        [
            _slug(board, max_len=32),
            _slug(phase, max_len=32),
            _slug(failure_type, max_len=48),
            _slug(tail_source, max_len=96),
        ]
    )


class _RetryRepairClusterUpsert(Exception):
    pass


class _Savepoint:
    def __init__(self, conn: sqlite3.Connection, name: str) -> None:
        self._conn = conn
        self._name = name

    def __enter__(self) -> None:
        self._conn.execute(f"SAVEPOINT {self._name}")

    def __exit__(self, exc_type, _exc, _tb) -> bool:
        if exc_type is None:
            self._conn.execute(f"RELEASE SAVEPOINT {self._name}")
            return False
        self._conn.execute(f"ROLLBACK TO SAVEPOINT {self._name}")
        self._conn.execute(f"RELEASE SAVEPOINT {self._name}")
        return False


def upsert_repair_cluster(
    conn: sqlite3.Connection,
    *,
    fingerprint: str,
    summary: str,
    job_id: int,
    status: str = "open",
    eligibility: str = "unknown",
) -> dict:
    normalized_summary = str(summary or "").strip()
    for attempt in range(6):
        try:
            with _Savepoint(conn, f"repair_cluster_upsert_{attempt}"):
                row = conn.execute("SELECT * FROM repair_clusters WHERE fingerprint = ?", (fingerprint,)).fetchone()
                if row is None:
                    try:
                        conn.execute(
                            "INSERT INTO repair_clusters "
                            "(fingerprint, status, eligibility, attempt_count, representative_job_ids, latest_summary) "
                            "VALUES (?, ?, ?, 1, ?, ?)",
                            (fingerprint, status, eligibility, json.dumps([int(job_id)]), normalized_summary),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise _RetryRepairClusterUpsert from exc
                else:
                    previous_ids = str(row["representative_job_ids"] or "[]")
                    previous_attempt_count = int(row["attempt_count"] or 0)
                    job_ids = _coerce_job_ids(previous_ids)
                    job_ids.append(int(job_id))
                    updated = conn.execute(
                        "UPDATE repair_clusters SET status = ?, eligibility = ?, attempt_count = ?, representative_job_ids = ?, "
                        "latest_summary = ?, updated_at = CURRENT_TIMESTAMP "
                        "WHERE id = ? AND attempt_count = ? AND representative_job_ids = ?",
                        (
                            status,
                            eligibility,
                            previous_attempt_count + 1,
                            json.dumps(sorted(set(job_ids))),
                            normalized_summary or str(row["latest_summary"] or ""),
                            row["id"],
                            previous_attempt_count,
                            previous_ids,
                        ),
                    )
                    if updated.rowcount == 0:
                        raise _RetryRepairClusterUpsert
            break
        except _RetryRepairClusterUpsert:
            continue
    else:
        raise RuntimeError(f"Could not upsert repair cluster for fingerprint {fingerprint!r}")
    current = conn.execute("SELECT * FROM repair_clusters WHERE fingerprint = ?", (fingerprint,)).fetchone()
    return dict(current) if current is not None else {}


def write_repair_cluster_report(
    output_root: str | Path,
    *,
    cluster_row: dict | sqlite3.Row,
    suggestions: list[str],
    artifacts: dict[str, str] | None = None,
) -> Path:
    root = Path(output_root)
    report_dir = root / "_audit" / "repair_clusters"
    report_dir.mkdir(parents=True, exist_ok=True)
    row = dict(cluster_row)
    report_path = report_dir / f"{row['fingerprint']}.md"
    metadata = {
        "fingerprint": row["fingerprint"],
        "status": row.get("status") or "open",
        "eligibility": row.get("eligibility") or "unknown",
        "summary": row.get("latest_summary") or "",
        "note_path": str(report_path.relative_to(root)),
    }
    lines = [f"<!-- repair_cluster_index: {json.dumps(metadata, sort_keys=True)} -->", "# Repair Cluster", ""]
    lines.extend(
        [
            f"- Fingerprint: `{row['fingerprint']}`",
            f"- Status: `{row.get('status') or 'open'}`",
            f"- Eligibility: `{row.get('eligibility') or 'unknown'}`",
            f"- Attempt Count: `{int(row.get('attempt_count') or 0)}`",
            f"- Representative Jobs: `{json.dumps(_coerce_job_ids(row.get('representative_job_ids')))} `",
            "",
            "## Latest Summary",
            "",
            str(row.get("latest_summary") or "No summary recorded."),
            "",
            "## Suggestions",
            "",
        ]
    )
    for suggestion in suggestions or ["Inspect the artifacts for the representative jobs before attempting another automated repair."]:
        lines.append(f"- {suggestion}")
    lines.extend(["", "## Evidence", ""])
    artifact_items = artifacts or {}
    if artifact_items:
        for key, raw_path in sorted(artifact_items.items()):
            path = Path(raw_path)
            artifact_link = _markdown_relpath(report_path, path)
            lines.append(f"- `{key}`: [{path.name}]({artifact_link})")
    else:
        lines.append("- No artifact paths were recorded for this cluster.")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def refresh_active_repair_failure_index(output_root: str | Path) -> Path:
    root = Path(output_root)
    audit_dir = root / "_audit"
    cluster_dir = audit_dir / "repair_clusters"
    cluster_dir.mkdir(parents=True, exist_ok=True)
    index_path = audit_dir / "active_repair_failures.md"
    lines = ["# Active Repair Failures", ""]
    entries: list[tuple[str, str]] = []
    for note_path in sorted(cluster_dir.glob("*.md")):
        try:
            first_line = note_path.read_text(encoding="utf-8").splitlines()[0]
        except (IndexError, OSError):
            continue
        prefix = "<!-- repair_cluster_index: "
        suffix = " -->"
        if not (first_line.startswith(prefix) and first_line.endswith(suffix)):
            continue
        try:
            payload = json.loads(first_line[len(prefix) : -len(suffix)])
        except json.JSONDecodeError:
            continue
        fingerprint = str(payload.get("fingerprint") or note_path.stem).strip() or note_path.stem
        if str(payload.get("status") or "open").strip().casefold() != "open":
            continue
        summary = str(payload.get("summary") or "").strip()
        entries.append((fingerprint, summary))
        rel = _markdown_relpath(index_path, note_path)
        lines.append(f"- [{fingerprint}]({rel}): {summary}")
    if not entries:
        lines.append("No active repair failures.")
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index_path


def record_repairable_failure_cluster(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    board: str,
    phase: str,
    failure_type: str,
    summary: str,
    field_labels: list[str] | None = None,
    output_dir: str | Path | None = None,
    output_root: str | Path | None = None,
    suggestions: list[str] | None = None,
    artifacts: dict[str, str] | None = None,
) -> dict:
    fingerprint = build_repair_fingerprint(
        board=board,
        phase=phase,
        failure_type=failure_type,
        message=summary,
        field_labels=field_labels,
    )
    cluster = upsert_repair_cluster(
        conn,
        fingerprint=fingerprint,
        summary=summary,
        job_id=job_id,
        status="open",
        eligibility="auto_repair_candidate",
    )
    resolved_output_root = Path(output_root) if output_root is not None else _infer_output_root(output_dir)
    if resolved_output_root is not None:
        write_repair_cluster_report(
            resolved_output_root,
            cluster_row=cluster,
            suggestions=suggestions or [],
            artifacts=artifacts,
        )
        refresh_active_repair_failure_index(resolved_output_root)
    return cluster
