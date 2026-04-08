#!/usr/bin/env python3
"""Resume an active backlog sweep when possible, otherwise start a new run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from init_backlog_sweep import DEFAULT_MANIFEST_PATH, bootstrap_manifest, utc_run_tag


def _artifact_exists(manifest_path: Path, artifact_path: str) -> bool:
    candidate = Path(artifact_path)
    if not candidate.is_absolute():
        candidate = manifest_path.parent / candidate
    return candidate.is_file()


def manifest_is_resumable(path: Path) -> bool:
    if not path.exists():
        return False

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False

    if not isinstance(payload, dict):
        return False

    phase_pairs = (
        ("phase1_snapshot", "phase1_results"),
        ("phase2_snapshot", "phase2_results"),
        ("phase3_snapshot", "phase3_results"),
    )
    for snapshot_key, results_key in phase_pairs:
        snapshot_raw = str(payload.get(snapshot_key) or "").strip()
        results_raw = str(payload.get(results_key) or "").strip()
        if not snapshot_raw or not results_raw:
            continue
        if _artifact_exists(path, snapshot_raw) and _artifact_exists(path, results_raw):
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--active", action="store_true", help="Use the active sweep manifest flow.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    args = parser.parse_args(argv)

    manifest_path = DEFAULT_MANIFEST_PATH if args.active else args.manifest
    if manifest_is_resumable(manifest_path):
        print(f"Resume active sweep: {manifest_path}")
        return 0

    bootstrap_manifest(manifest_path, date_tag=utc_run_tag(), force=False, new_run=True)
    print(f"Could not resume active sweep; started new active sweep: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
