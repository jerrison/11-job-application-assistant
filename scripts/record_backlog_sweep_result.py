#!/usr/bin/env python3
"""Record a trace-backed backlog sweep result row for the active manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sweep_controller import DEFAULT_MANIFEST_PATH, record_transition


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH, help="Sweep manifest to update.")
    parser.add_argument("--active", action="store_true", help=f"Use the active manifest at {DEFAULT_MANIFEST_PATH}.")
    parser.add_argument("--phase", required=True, choices=("phase2", "phase3"), help="Sweep phase to record.")
    parser.add_argument("--id", required=True, help="Snapshot job id to record.")
    parser.add_argument("--outcome", required=True, help="Result outcome for this snapshot job.")
    parser.add_argument("--handled-via", required=True, help="How the row was handled, for example draft_web_browser.")
    parser.add_argument("--issue-id", default="", help="Related Linear issue id, if any.")
    parser.add_argument("--notes", default="", help="Free-form notes to append to the row.")
    parser.add_argument(
        "--linear-sync-status",
        default="pending",
        help="Linear sync state for the recorded row: pending, synced, or drifted.",
    )
    parser.add_argument(
        "--linear-sync-payload-path",
        default="",
        help="Optional repo-local sync payload path associated with the recorded row.",
    )
    parser.add_argument(
        "--evidence-path",
        action="append",
        dest="evidence_paths",
        default=None,
        help="Optional explicit evidence path. Repeat for multiple paths.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    manifest_path = DEFAULT_MANIFEST_PATH if args.active else args.manifest

    try:
        recorded = record_transition(
            manifest_path=manifest_path,
            phase_key=args.phase,
            row_id=args.id,
            outcome=args.outcome,
            handled_via=args.handled_via,
            issue_id=args.issue_id,
            notes=args.notes,
            evidence_paths=args.evidence_paths,
            linear_sync_status=args.linear_sync_status,
            linear_sync_payload_path=args.linear_sync_payload_path,
        )
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 1

    print(json.dumps(recorded, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
