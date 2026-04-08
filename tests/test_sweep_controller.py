from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    assert path.exists(), f"Missing {relative_path}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop(name, None)
    sys.modules[name] = module
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_record_transition_writes_current_wave_to_latest_row(tmp_path: Path):
    module = load_module("sweep_controller", "scripts/sweep_controller.py")
    manifest_path = tmp_path / "current_backlog_sweep.json"
    snapshot_path = tmp_path / "phase2-snapshot.tsv"
    results_path = tmp_path / "phase2-results.tsv"
    output_dir = tmp_path / "output" / "acme"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "draft_summary.png").write_bytes(b"proof")

    snapshot_path.write_text(
        "id\tcompany\trole_title\tboard\toutput_dir\n"
        f"1\tAcme\tPM\tgreenhouse\t{output_dir}\n",
        encoding="utf-8",
    )
    results_path.write_text("\t".join(module.PHASE_RESULT_FIELDS["phase2"]) + "\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps({"run_id": "wave-1", "phase2_snapshot": str(snapshot_path), "phase2_results": str(results_path)}) + "\n",
        encoding="utf-8",
    )

    recorded = module.record_transition(
        manifest_path=manifest_path,
        phase_key="phase2",
        row_id="1",
        outcome="fixed_redrafted",
        handled_via="cli_manual",
        repair_wave_fingerprint="wave-a",
        linear_sync_status="pending",
    )

    assert recorded["repair_wave_fingerprint"] == "wave-a"
    assert recorded["linear_sync_status"] == "pending"
    assert Path(recorded["review_trace_path"]).exists()
    assert Path(recorded["artifact_manifest_path"]).exists()
    with results_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    assert len(rows) == 1
    assert rows[0]["repair_wave_fingerprint"] == "wave-a"
    assert rows[0]["linear_sync_status"] == "pending"
    assert rows[0]["linear_sync_payload_path"] == ""
    assert rows[0]["evidence_paths"] == str(output_dir / "draft_summary.png")


def test_record_transition_refuses_phase3_reviewed_ready_without_browser_handled_via(tmp_path: Path):
    module = load_module("sweep_controller", "scripts/sweep_controller.py")
    manifest_path = tmp_path / "current_backlog_sweep.json"
    snapshot_path = tmp_path / "phase3-snapshot.tsv"
    results_path = tmp_path / "phase3-results.tsv"
    output_dir = tmp_path / "output" / "example"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "draft_summary.png").write_bytes(b"proof")

    snapshot_path.write_text(
        "id\tcompany\trole_title\tboard\toutput_dir\n"
        f"21\tExample\tPrincipal PM\tgreenhouse\t{output_dir}\n",
        encoding="utf-8",
    )
    results_path.write_text("\t".join(module.PHASE_RESULT_FIELDS["phase3"]) + "\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps({"run_id": "wave-1", "phase3_snapshot": str(snapshot_path), "phase3_results": str(results_path)}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="reviewed_ready requires browser"):
        module.record_transition(
            manifest_path=manifest_path,
            phase_key="phase3",
            row_id="21",
            outcome="reviewed_ready",
            handled_via="cli_manual",
            repair_wave_fingerprint="wave-a",
            linear_sync_status="pending",
        )


def test_record_transition_rejects_missing_explicit_evidence_paths(tmp_path: Path):
    module = load_module("sweep_controller", "scripts/sweep_controller.py")
    manifest_path = tmp_path / "current_backlog_sweep.json"
    snapshot_path = tmp_path / "phase2-snapshot.tsv"
    results_path = tmp_path / "phase2-results.tsv"
    output_dir = tmp_path / "output" / "acme"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "draft_summary.png").write_bytes(b"proof")

    snapshot_path.write_text(
        "id\tcompany\trole_title\tboard\toutput_dir\n"
        f"1\tAcme\tPM\tgreenhouse\t{output_dir}\n",
        encoding="utf-8",
    )
    results_path.write_text("\t".join(module.PHASE_RESULT_FIELDS["phase2"]) + "\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps({"run_id": "wave-1", "phase2_snapshot": str(snapshot_path), "phase2_results": str(results_path)}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Evidence path does not exist"):
        module.record_transition(
            manifest_path=manifest_path,
            phase_key="phase2",
            row_id="1",
            outcome="fixed_redrafted",
            handled_via="cli_manual",
            evidence_paths=[output_dir / "missing-proof.png"],
            repair_wave_fingerprint="wave-a",
            linear_sync_status="pending",
        )


def test_record_transition_rejects_evidence_outside_output_dir(tmp_path: Path):
    module = load_module("sweep_controller", "scripts/sweep_controller.py")
    manifest_path = tmp_path / "current_backlog_sweep.json"
    snapshot_path = tmp_path / "phase2-snapshot.tsv"
    results_path = tmp_path / "phase2-results.tsv"
    output_dir = tmp_path / "output" / "acme"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "draft_summary.png").write_bytes(b"proof")
    outside = tmp_path / "outside-proof.png"
    outside.write_bytes(b"proof")

    snapshot_path.write_text(
        "id\tcompany\trole_title\tboard\toutput_dir\n"
        f"1\tAcme\tPM\tgreenhouse\t{output_dir}\n",
        encoding="utf-8",
    )
    results_path.write_text("\t".join(module.PHASE_RESULT_FIELDS["phase2"]) + "\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps({"run_id": "wave-1", "phase2_snapshot": str(snapshot_path), "phase2_results": str(results_path)}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="job output directory"):
        module.record_transition(
            manifest_path=manifest_path,
            phase_key="phase2",
            row_id="1",
            outcome="fixed_redrafted",
            handled_via="cli_manual",
            evidence_paths=[outside],
            repair_wave_fingerprint="wave-a",
            linear_sync_status="pending",
        )


def test_record_transition_rejects_unknown_linear_sync_status(tmp_path: Path):
    module = load_module("sweep_controller", "scripts/sweep_controller.py")
    manifest_path = tmp_path / "current_backlog_sweep.json"
    snapshot_path = tmp_path / "phase2-snapshot.tsv"
    results_path = tmp_path / "phase2-results.tsv"
    output_dir = tmp_path / "output" / "acme"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "draft_summary.png").write_bytes(b"proof")

    snapshot_path.write_text(
        "id\tcompany\trole_title\tboard\toutput_dir\n"
        f"1\tAcme\tPM\tgreenhouse\t{output_dir}\n",
        encoding="utf-8",
    )
    results_path.write_text("\t".join(module.PHASE_RESULT_FIELDS["phase2"]) + "\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps({"run_id": "wave-1", "phase2_snapshot": str(snapshot_path), "phase2_results": str(results_path)}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unsupported linear_sync_status"):
        module.record_transition(
            manifest_path=manifest_path,
            phase_key="phase2",
            row_id="1",
            outcome="fixed_redrafted",
            handled_via="cli_manual",
            repair_wave_fingerprint="wave-a",
            linear_sync_status="unknown",
        )


def test_record_transition_rejects_output_dir_that_is_not_directory(tmp_path: Path):
    module = load_module("sweep_controller", "scripts/sweep_controller.py")
    manifest_path = tmp_path / "current_backlog_sweep.json"
    snapshot_path = tmp_path / "phase2-snapshot.tsv"
    results_path = tmp_path / "phase2-results.tsv"
    output_file = tmp_path / "not-a-directory"
    output_file.write_text("proof", encoding="utf-8")

    snapshot_path.write_text(
        "id\tcompany\trole_title\tboard\toutput_dir\n"
        f"1\tAcme\tPM\tgreenhouse\t{output_file}\n",
        encoding="utf-8",
    )
    results_path.write_text("\t".join(module.PHASE_RESULT_FIELDS["phase2"]) + "\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps({"run_id": "wave-1", "phase2_snapshot": str(snapshot_path), "phase2_results": str(results_path)}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not a directory"):
        module.record_transition(
            manifest_path=manifest_path,
            phase_key="phase2",
            row_id="1",
            outcome="fixed_redrafted",
            handled_via="cli_manual",
            repair_wave_fingerprint="wave-a",
            linear_sync_status="pending",
        )


def test_record_transition_rejects_results_ledger_with_mismatched_header(tmp_path: Path):
    module = load_module("sweep_controller", "scripts/sweep_controller.py")
    manifest_path = tmp_path / "current_backlog_sweep.json"
    snapshot_path = tmp_path / "phase2-snapshot.tsv"
    results_path = tmp_path / "phase2-results.tsv"
    output_dir = tmp_path / "output" / "acme"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "draft_summary.png").write_bytes(b"proof")

    snapshot_path.write_text(
        "id\tcompany\trole_title\tboard\toutput_dir\n"
        f"1\tAcme\tPM\tgreenhouse\t{output_dir}\n",
        encoding="utf-8",
    )
    results_path.write_text("handled_at_utc\tid\tcompany\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps({"run_id": "wave-1", "phase2_snapshot": str(snapshot_path), "phase2_results": str(results_path)}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="header does not match expected controller fields"):
        module.record_transition(
            manifest_path=manifest_path,
            phase_key="phase2",
            row_id="1",
            outcome="fixed_redrafted",
            handled_via="cli_manual",
            repair_wave_fingerprint="wave-a",
            linear_sync_status="pending",
        )


def test_record_transition_sanitizes_review_root_components(tmp_path: Path):
    module = load_module("sweep_controller", "scripts/sweep_controller.py")
    manifest_path = tmp_path / "current_backlog_sweep.json"
    snapshot_path = tmp_path / "phase2-snapshot.tsv"
    results_path = tmp_path / "phase2-results.tsv"
    output_dir = tmp_path / "output" / "acme"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "draft_summary.png").write_bytes(b"proof")

    snapshot_path.write_text(
        "id\tcompany\trole_title\tboard\toutput_dir\n"
        f"job/1\tAcme\tPM\tgreenhouse\t{output_dir}\n",
        encoding="utf-8",
    )
    results_path.write_text("\t".join(module.PHASE_RESULT_FIELDS["phase2"]) + "\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps({"run_id": "wave/1", "phase2_snapshot": str(snapshot_path), "phase2_results": str(results_path)}) + "\n",
        encoding="utf-8",
    )

    recorded = module.record_transition(
        manifest_path=manifest_path,
        phase_key="phase2",
        row_id="job/1",
        outcome="fixed_redrafted",
        handled_via="cli_manual",
        repair_wave_fingerprint="wave-a",
        linear_sync_status="pending",
    )

    assert Path(recorded["review_trace_path"]).parent == tmp_path / "backlog-sweep-proof" / "wave_1" / "phase2" / "job_1"


def test_record_transition_phase1_queues_linear_sync_payload(tmp_path: Path):
    module = load_module("sweep_controller", "scripts/sweep_controller.py")

    sweep_root = tmp_path / ".context" / "compound-engineering"
    todos_dir = sweep_root / "todos"
    todos_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = todos_dir / "current_backlog_sweep.json"
    snapshot_path = todos_dir / "phase1-snapshot.tsv"
    results_path = todos_dir / "phase1-results.tsv"

    snapshot_path.write_text(
        "linear_issue_id\ttitle\tlabels\tstatus\trelated_job_id\trelated_output_dir\trequires_user_input\tcaptured_at_utc\n"
        "NAD-101\tFix proof drift\tbug\tTodo\t42\toutput/acme/pm\tfalse\t2026-04-08T10:00:00Z\n",
        encoding="utf-8",
    )
    results_path.write_text(
        "handled_at_utc\tlinear_issue_id\ttitle\tlabels\tstatus\trelated_job_id\trelated_output_dir\toutcome\thandled_via\t"
        "review_trace_path\tartifact_manifest_path\tproof_generated_at_utc\trepair_wave_fingerprint\t"
        "linear_sync_status\tlinear_sync_payload_path\tnotes\n",
        encoding="utf-8",
    )
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "wave-1",
                "phase1_snapshot": str(snapshot_path),
                "phase1_results": str(results_path),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    recorded = module.record_transition(
        manifest_path=manifest_path,
        phase_key="phase1",
        row_id="NAD-101",
        outcome="blocked_external",
        handled_via="cli_manual",
        linear_sync_status="pending",
        repair_wave_fingerprint="wave-a",
    )

    assert recorded["linear_sync_status"] == "pending"
    payload_path = Path(recorded["linear_sync_payload_path"])
    assert payload_path.exists()
    assert payload_path.parent == sweep_root / "linear-sync"
