#!/usr/bin/env python3
"""Compatibility wrapper around the shared sweep controller.

Historically, this module directly appended Phase 2/3 results and wrote trace
artifacts. To enforce a single state-machine transition path, Phase 2/3
recording now delegates to `scripts/sweep_controller.py`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sweep_controller import DEFAULT_MANIFEST_PATH, record_transition


def record_backlog_sweep_result(
    *,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    phase_key: str,
    job_id: str | int,
    outcome: str,
    handled_via: str,
    issue_id: str = "",
    notes: str = "",
    evidence_paths: Sequence[str | Path] | None = None,
    detail_json: Mapping[str, Any] | None = None,
    proof_generated_at_utc: str | None = None,
) -> dict[str, str]:
    return record_transition(
        manifest_path=Path(manifest_path),
        phase_key=phase_key,
        row_id=str(job_id),
        outcome=outcome,
        handled_via=handled_via,
        issue_id=issue_id,
        notes=notes,
        evidence_paths=evidence_paths,
        detail_json=dict(detail_json or {}),
        proof_generated_at_utc=proof_generated_at_utc,
    )

