from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    assert path.exists(), f"Missing {relative_path}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop(name, None)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write_tsv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_record_backlog_sweep_result_writes_trace_backed_row(tmp_path: Path):
    module = load_module("backlog_sweep_recorder", "scripts/backlog_sweep_recorder.py")
    controller = load_module("sweep_controller", "scripts/sweep_controller.py")

    output_dir = tmp_path / "output" / "example"
    output_dir.mkdir(parents=True)
    (output_dir / "draft_summary.png").write_bytes(b"proof")

    results_path = tmp_path / "phase3-results.tsv"
    results_path.write_text(
        "\t".join(controller.PHASE_RESULT_FIELDS["phase3"]) + "\n",
        encoding="utf-8",
    )
    snapshot_path = write_tsv(
        tmp_path / "phase3-snapshot.tsv",
        ["id", "company", "role_title", "board", "output_dir"],
        [
            {
                "id": "21",
                "company": "Example",
                "role_title": "Principal PM",
                "board": "greenhouse",
                "output_dir": str(output_dir),
            }
        ],
    )
    manifest_path = tmp_path / ".context" / "compound-engineering" / "todos" / "current_backlog_sweep.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "2026-04-08T10-00-00Z",
                "phase3_snapshot": str(snapshot_path),
                "phase3_results": str(results_path),
                "phase3_started_at_utc": "2026-04-08T09:59:00Z",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    recorded = module.record_backlog_sweep_result(
        manifest_path=manifest_path,
        phase_key="phase3",
        job_id="21",
        outcome="reviewed_ready",
        handled_via="draft_web_browser",
        notes="manual browser review",
    )

    assert Path(recorded["review_trace_path"]).exists()
    assert Path(recorded["artifact_manifest_path"]).exists()
    assert recorded["repair_wave_fingerprint"]
    assert recorded["linear_sync_status"] == "pending"
    with results_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    assert len(rows) == 1
    assert rows[0]["outcome"] == "reviewed_ready"
    assert rows[0]["handled_via"] == "draft_web_browser"
    assert rows[0]["linear_sync_status"] == "pending"
    assert rows[0]["linear_sync_payload_path"] == ""
