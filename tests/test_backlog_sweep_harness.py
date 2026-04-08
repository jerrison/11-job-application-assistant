from __future__ import annotations

import contextlib
import csv
import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

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


def run_main(module, args: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = module.main(args)
    return code, stdout.getvalue(), stderr.getvalue()


def test_init_backlog_sweep_bootstraps_manifest_without_phase_files(tmp_path: Path):
    module = load_module("init_backlog_sweep", "scripts/init_backlog_sweep.py")

    todos_dir = tmp_path / ".context" / "compound-engineering" / "todos"
    manifest_path = todos_dir / "current_backlog_sweep.json"

    with (
        patch.object(module, "TODOS_DIR", todos_dir),
        patch.object(module, "DEFAULT_MANIFEST_PATH", manifest_path),
        patch.object(module, "load_repo_state", return_value={"head": "abc", "branch": "main", "dirty_paths_count": 0}),
        patch.object(module, "load_job_status_counts", return_value={"draft": 5, "stopped": 2}),
    ):
        code, stdout, stderr = run_main(module, ["--date", "2026-04-08"])

    assert code == 0, stderr or stdout
    assert manifest_path.exists()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["date_tag"] == "2026-04-08"
    assert "--manifest" in " ".join(manifest["checker_command"])
    assert "phase2_snapshot" not in manifest
    assert "phase3_snapshot" not in manifest
    assert not list(todos_dir.glob("*.tsv"))


def test_init_backlog_sweep_new_run_archives_existing_manifest_and_records_current_state(tmp_path: Path):
    module = load_module("init_backlog_sweep", "scripts/init_backlog_sweep.py")

    todos_dir = tmp_path / ".context" / "compound-engineering" / "todos"
    history_dir = todos_dir / "history"
    manifest_path = todos_dir / "current_backlog_sweep.json"
    todos_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"date_tag": "2026-04-01", "phase2_snapshot_count": 9}, indent=2) + "\n",
        encoding="utf-8",
    )

    repo_state = {
        "head": "abc123def456",
        "branch": "main",
        "dirty_paths_count": 2,
        "dirty_paths_preview": ["AGENTS.md", "scripts/job_db.py"],
    }
    job_counts = {"draft": 41, "stopped": 17, "queued": 9}

    with (
        patch.object(module, "TODOS_DIR", todos_dir),
        patch.object(module, "DEFAULT_MANIFEST_PATH", manifest_path),
        patch.object(module, "HISTORY_DIR", history_dir),
        patch.object(module, "load_repo_state", return_value=repo_state),
        patch.object(module, "load_job_status_counts", return_value=job_counts),
    ):
        code, stdout, stderr = run_main(module, ["--new-run", "--date", "2026-04-08"])

    assert code == 0, stderr or stdout
    archived = sorted(history_dir.glob("current_backlog_sweep-*.json"))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text(encoding="utf-8"))["date_tag"] == "2026-04-01"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["date_tag"] == "2026-04-08"
    assert manifest["repo_state"] == repo_state
    assert manifest["job_status_counts"] == job_counts
    assert manifest["run_id"]
    assert manifest["active_manifest_version"] == 1


def test_init_backlog_sweep_new_run_defaults_to_unique_run_tag(tmp_path: Path):
    module = load_module("init_backlog_sweep", "scripts/init_backlog_sweep.py")

    todos_dir = tmp_path / ".context" / "compound-engineering" / "todos"
    manifest_path = todos_dir / "current_backlog_sweep.json"

    with (
        patch.object(module, "TODOS_DIR", todos_dir),
        patch.object(module, "DEFAULT_MANIFEST_PATH", manifest_path),
        patch.object(module, "utc_run_tag", return_value="2026-04-08T09-10-11Z"),
        patch.object(module, "load_repo_state", return_value={"head": "abc", "branch": "main", "dirty_paths_count": 0}),
        patch.object(module, "load_job_status_counts", return_value={"draft": 1}),
    ):
        code, stdout, stderr = run_main(module, ["--new-run"])

    assert code == 0, stderr or stdout
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["date_tag"] == "2026-04-08T09-10-11Z"


def test_init_backlog_sweep_starts_each_phase_from_current_queue_state(tmp_path: Path):
    module = load_module("init_backlog_sweep", "scripts/init_backlog_sweep.py")
    controller = load_module("sweep_controller", "scripts/sweep_controller.py")

    todos_dir = tmp_path / ".context" / "compound-engineering" / "todos"
    manifest_path = todos_dir / "current_backlog_sweep.json"

    stopped_rows = [
        {"id": "11", "company": "Acme", "role_title": "pm", "board": "greenhouse", "output_dir": "/tmp/acme"},
        {"id": "12", "company": "Beta", "role_title": "staff-pm", "board": "ashby", "output_dir": "/tmp/beta"},
    ]
    draft_rows = [
        {"id": "21", "company": "Gamma", "role_title": "principal-pm", "board": "lever", "output_dir": "/tmp/gamma"}
    ]

    def fake_load_rows(status: str):
        return stopped_rows if status == "stopped" else draft_rows

    with (
        patch.object(module, "TODOS_DIR", todos_dir),
        patch.object(module, "DEFAULT_MANIFEST_PATH", manifest_path),
        patch.object(module, "load_snapshot_rows", side_effect=fake_load_rows),
        patch.object(module, "load_repo_state", return_value={"head": "abc", "branch": "main", "dirty_paths_count": 0}),
        patch.object(module, "load_job_status_counts", return_value={"draft": 4, "stopped": 2}),
    ):
        code, stdout, stderr = run_main(module, ["--date", "2026-04-08"])
        assert code == 0, stderr or stdout

        code, stdout, stderr = run_main(module, ["--start-phase", "phase2", "--manifest", str(manifest_path)])
        assert code == 0, stderr or stdout
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["phase2_snapshot_count"] == 2
        assert "phase3_snapshot" not in manifest
        assert Path(manifest["phase2_snapshot"]).exists()
        assert Path(manifest["phase2_results"]).read_text(encoding="utf-8").splitlines()[0] == "\t".join(
            controller.PHASE_RESULT_FIELDS["phase2"]
        )

        code, stdout, stderr = run_main(module, ["--start-phase", "phase3", "--manifest", str(manifest_path)])
        assert code == 0, stderr or stdout
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["phase3_snapshot_count"] == 1
        assert Path(manifest["phase3_snapshot"]).exists()
        assert Path(manifest["phase3_results"]).read_text(encoding="utf-8").splitlines()[0] == "\t".join(
            controller.PHASE_RESULT_FIELDS["phase3"]
        )

    phase2_snapshot = Path(manifest["phase2_snapshot"]).read_text(encoding="utf-8").splitlines()
    phase3_snapshot = Path(manifest["phase3_snapshot"]).read_text(encoding="utf-8").splitlines()
    assert phase2_snapshot[0] == "id\tcompany\trole_title\tboard\toutput_dir"
    assert phase2_snapshot[1].startswith("11\tAcme\tpm\tgreenhouse\t")
    assert phase2_snapshot[2].startswith("12\tBeta\tstaff-pm\tashby\t")
    assert phase3_snapshot[0] == "id\tcompany\trole_title\tboard\toutput_dir"
    assert phase3_snapshot[1].startswith("21\tGamma\tprincipal-pm\tlever\t")


def test_init_backlog_sweep_refuses_to_overwrite_existing_manifest_without_force(tmp_path: Path):
    module = load_module("init_backlog_sweep", "scripts/init_backlog_sweep.py")

    todos_dir = tmp_path / ".context" / "compound-engineering" / "todos"
    todos_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = todos_dir / "current_backlog_sweep.json"
    manifest_path.write_text('{"existing": true}\n', encoding="utf-8")

    with patch.object(module, "TODOS_DIR", todos_dir), patch.object(module, "DEFAULT_MANIFEST_PATH", manifest_path):
        code, stdout, stderr = run_main(module, ["--date", "2026-04-08"])

    assert code == 1
    assert "already exist" in (stdout + stderr).lower()


def test_init_backlog_sweep_refuses_to_restart_phase_without_force(tmp_path: Path):
    module = load_module("init_backlog_sweep", "scripts/init_backlog_sweep.py")

    todos_dir = tmp_path / ".context" / "compound-engineering" / "todos"
    manifest_path = todos_dir / "current_backlog_sweep.json"
    rows = [{"id": "11", "company": "Acme", "role_title": "pm", "board": "greenhouse", "output_dir": "/tmp/acme"}]

    with (
        patch.object(module, "TODOS_DIR", todos_dir),
        patch.object(module, "DEFAULT_MANIFEST_PATH", manifest_path),
        patch.object(module, "load_snapshot_rows", return_value=rows),
        patch.object(module, "load_repo_state", return_value={"head": "abc", "branch": "main", "dirty_paths_count": 0}),
        patch.object(module, "load_job_status_counts", return_value={"draft": 4, "stopped": 1}),
    ):
        code, stdout, stderr = run_main(module, ["--date", "2026-04-08"])
        assert code == 0, stderr or stdout
        code, stdout, stderr = run_main(module, ["--start-phase", "phase2", "--manifest", str(manifest_path)])
        assert code == 0, stderr or stdout
        code, stdout, stderr = run_main(module, ["--start-phase", "phase2", "--manifest", str(manifest_path)])

    assert code == 1
    assert "already exist" in (stdout + stderr).lower()


def test_init_backlog_sweep_phase2_ledger_accepts_controller_record_transition(tmp_path: Path):
    init_module = load_module("init_backlog_sweep", "scripts/init_backlog_sweep.py")
    controller = load_module("sweep_controller", "scripts/sweep_controller.py")

    todos_dir = tmp_path / ".context" / "compound-engineering" / "todos"
    manifest_path = todos_dir / "current_backlog_sweep.json"
    output_dir = tmp_path / "output" / "acme" / "pm"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "draft_summary.png").write_bytes(b"proof")
    stopped_rows = [
        {
            "id": "11",
            "company": "Acme",
            "role_title": "pm",
            "board": "greenhouse",
            "output_dir": str(output_dir),
        }
    ]

    with (
        patch.object(init_module, "TODOS_DIR", todos_dir),
        patch.object(init_module, "DEFAULT_MANIFEST_PATH", manifest_path),
        patch.object(init_module, "load_snapshot_rows", return_value=stopped_rows),
        patch.object(init_module, "load_repo_state", return_value={"head": "abc", "branch": "main", "dirty_paths_count": 0}),
        patch.object(init_module, "load_job_status_counts", return_value={"draft": 4, "stopped": 1}),
    ):
        code, stdout, stderr = run_main(init_module, ["--date", "2026-04-08"])
        assert code == 0, stderr or stdout
        code, stdout, stderr = run_main(init_module, ["--start-phase", "phase2", "--manifest", str(manifest_path)])
        assert code == 0, stderr or stdout

    recorded = controller.record_transition(
        manifest_path=manifest_path,
        phase_key="phase2",
        row_id="11",
        outcome="fixed_redrafted",
        handled_via="cli_manual",
        repair_wave_fingerprint="wave-a",
        linear_sync_status="pending",
    )

    assert recorded["id"] == "11"
    with Path(json.loads(manifest_path.read_text(encoding="utf-8"))["phase2_results"]).open(
        "r", encoding="utf-8", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    assert len(rows) == 1
    assert rows[0]["repair_wave_fingerprint"] == "wave-a"
    assert rows[0]["linear_sync_status"] == "pending"


def test_record_backlog_sweep_result_cli_accepts_linear_sync_metadata(tmp_path: Path):
    controller = load_module("sweep_controller", "scripts/sweep_controller.py")
    module = load_module("record_backlog_sweep_result", "scripts/record_backlog_sweep_result.py")

    output_dir = tmp_path / "output" / "relativity" / "pm"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "draft_summary.png").write_bytes(b"proof")

    snapshot_path = tmp_path / "phase2-snapshot.tsv"
    snapshot_path.write_text(
        "id\tcompany\trole_title\tboard\toutput_dir\n"
        f"19\tLE0001 Relativity ODA LLC\tGroup Manager, PM, Data Platform Extensibility\tworkday\t{output_dir}\n",
        encoding="utf-8",
    )
    results_path = tmp_path / "phase2-results.tsv"
    results_path.write_text("\t".join(controller.PHASE_RESULT_FIELDS["phase2"]) + "\n", encoding="utf-8")
    manifest_path = tmp_path / "current_backlog_sweep.json"
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "2026-04-08T16-39-46Z",
                "phase2_snapshot": str(snapshot_path),
                "phase2_results": str(results_path),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    sync_payload_path = tmp_path / "linear-sync" / "phase2-19.json"
    sync_payload_path.parent.mkdir(parents=True, exist_ok=True)
    sync_payload_path.write_text('{"synced": true}\n', encoding="utf-8")

    code, stdout, stderr = run_main(
        module,
        [
            "--manifest",
            str(manifest_path),
            "--phase",
            "phase2",
            "--id",
            "19",
            "--outcome",
            "parked_requires_user_input",
            "--handled-via",
            "cli_manual",
            "--issue-id",
            "NAD-199",
            "--notes",
            "Only numeric salary-range selection still needs explicit user input.",
            "--linear-sync-status",
            "synced",
            "--linear-sync-payload-path",
            str(sync_payload_path),
        ],
    )

    assert code == 0, stderr or stdout
    with results_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    assert len(rows) == 1
    assert rows[0]["id"] == "19"
    assert rows[0]["linear_sync_status"] == "synced"
    assert rows[0]["linear_sync_payload_path"] == str(sync_payload_path)
    assert rows[0]["issue_id"] == "NAD-199"


def test_init_backlog_sweep_starts_phase1_from_linear_snapshot_source(tmp_path: Path):
    module = load_module("init_backlog_sweep", "scripts/init_backlog_sweep.py")
    controller = load_module("sweep_controller", "scripts/sweep_controller.py")
    todos_dir = tmp_path / ".context" / "compound-engineering" / "todos"
    manifest_path = todos_dir / "current_backlog_sweep.json"
    phase1_template = tmp_path / "phase1-template.tsv"
    phase1_template.write_text(
        "handled_at_utc\tlinear_issue_id\ttitle\tlabels\tstatus\trelated_job_id\trelated_output_dir\toutcome\thandled_via\treview_trace_path\tartifact_manifest_path\tproof_generated_at_utc\trepair_wave_fingerprint\tlinear_sync_status\tlinear_sync_payload_path\tnotes\n",
        encoding="utf-8",
    )

    fake_phase1_rows = [
        {
            "linear_issue_id": "NAD-101",
            "title": "Fix draft proof drift",
            "labels": "bug",
            "status": "Todo",
            "related_job_id": "42",
            "related_output_dir": "output/acme/pm",
            "requires_user_input": "false",
            "captured_at_utc": "2026-04-08T10:00:00Z",
        }
    ]

    with (
        patch.object(module, "TODOS_DIR", todos_dir),
        patch.object(module, "DEFAULT_MANIFEST_PATH", manifest_path),
        patch.object(module, "PHASE1_TEMPLATE", phase1_template),
        patch.object(module, "load_phase1_linear_rows", return_value=fake_phase1_rows),
        patch.object(module, "load_repo_state", return_value={"head": "abc", "branch": "main", "dirty_paths_count": 0}),
        patch.object(module, "load_job_status_counts", return_value={"draft": 4, "stopped": 2}),
    ):
        code, stdout, stderr = run_main(module, ["--date", "2026-04-08"])
        assert code == 0, stderr or stdout
        code, stdout, stderr = run_main(module, ["--start-phase", "phase1", "--manifest", str(manifest_path)])

    assert code == 0, stderr or stdout
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["phase1_snapshot_count"] == 1
    assert Path(manifest["phase1_results"]).read_text(encoding="utf-8").splitlines()[0] == "\t".join(
        controller.PHASE_RESULT_FIELDS["phase1"]
    )


def test_init_backlog_sweep_write_snapshot_tolerates_missing_fields_and_sanitizes_cells(tmp_path: Path):
    module = load_module("init_backlog_sweep", "scripts/init_backlog_sweep.py")
    snapshot_path = tmp_path / "snapshot.tsv"

    module.write_snapshot(
        snapshot_path,
        [
            {
                "id": "11",
                "company": "Acme\tInc",
                "role_title": "Staff\nPM",
                "board": "greenhouse",
            }
        ],
    )

    assert snapshot_path.read_text(encoding="utf-8") == (
        "id\tcompany\trole_title\tboard\toutput_dir\n"
        "11\tAcme Inc\tStaff PM\tgreenhouse\t\n"
    )
    assert module._sanitize_tsv_cell(0) == "0"


def test_resume_or_start_reuses_valid_active_manifest(tmp_path: Path):
    module = load_module("resume_or_start_backlog_sweep", "scripts/resume_or_start_backlog_sweep.py")
    manifest_path = tmp_path / ".context" / "compound-engineering" / "todos" / "current_backlog_sweep.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    phase2_snapshot = manifest_path.parent / "phase2.tsv"
    phase2_results = manifest_path.parent / "phase2-results.tsv"
    phase2_snapshot.write_text("id\tcompany\trole_title\tboard\toutput_dir\n", encoding="utf-8")
    phase2_results.write_text("handled_at_utc\tid\tcompany\trole_title\tboard\toutput_dir\toutcome\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps({"phase2_snapshot": "phase2.tsv", "phase2_results": "phase2-results.tsv"}) + "\n",
        encoding="utf-8",
    )

    with (
        patch.object(module, "DEFAULT_MANIFEST_PATH", manifest_path),
        patch.object(module, "bootstrap_manifest") as bootstrap_manifest,
    ):
        code, stdout, stderr = run_main(module, ["--active"])

    assert code == 0
    assert stderr == ""
    assert stdout.strip() == f"Resume active sweep: {manifest_path}"
    bootstrap_manifest.assert_not_called()


def test_resume_or_start_active_uses_default_manifest_path(tmp_path: Path):
    module = load_module("resume_or_start_backlog_sweep", "scripts/resume_or_start_backlog_sweep.py")
    default_manifest = tmp_path / ".context" / "compound-engineering" / "todos" / "current_backlog_sweep.json"
    default_manifest.parent.mkdir(parents=True, exist_ok=True)
    phase2_snapshot = default_manifest.parent / "phase2.tsv"
    phase2_results = default_manifest.parent / "phase2-results.tsv"
    phase2_snapshot.write_text("id\tcompany\trole_title\tboard\toutput_dir\n", encoding="utf-8")
    phase2_results.write_text("handled_at_utc\tid\tcompany\trole_title\tboard\toutput_dir\toutcome\n", encoding="utf-8")
    default_manifest.write_text(
        json.dumps({"phase2_snapshot": "phase2.tsv", "phase2_results": "phase2-results.tsv"}) + "\n",
        encoding="utf-8",
    )

    explicit_manifest = tmp_path / "custom-manifest.json"

    with (
        patch.object(module, "DEFAULT_MANIFEST_PATH", default_manifest),
        patch.object(module, "bootstrap_manifest") as bootstrap_manifest,
    ):
        code, stdout, stderr = run_main(module, ["--active", "--manifest", str(explicit_manifest)])

    assert code == 0
    assert stderr == ""
    assert stdout.strip() == f"Resume active sweep: {default_manifest}"
    bootstrap_manifest.assert_not_called()


def test_resume_or_start_bootstraps_when_active_manifest_has_no_phase_artifacts(tmp_path: Path):
    module = load_module("resume_or_start_backlog_sweep", "scripts/resume_or_start_backlog_sweep.py")
    manifest_path = tmp_path / ".context" / "compound-engineering" / "todos" / "current_backlog_sweep.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({"date_tag": "2026-04-08"}) + "\n", encoding="utf-8")

    with (
        patch.object(module, "DEFAULT_MANIFEST_PATH", manifest_path),
        patch.object(module, "utc_run_tag", return_value="2026-04-08T09-10-11Z"),
        patch.object(module, "bootstrap_manifest", return_value={"run_id": "2026-04-08T09-10-11Z"}) as bootstrap_manifest,
    ):
        code, stdout, stderr = run_main(module, ["--active"])

    assert code == 0
    assert stderr == ""
    assert stdout.strip() == f"Could not resume active sweep; started new active sweep: {manifest_path}"
    bootstrap_manifest.assert_called_once_with(
        manifest_path,
        date_tag="2026-04-08T09-10-11Z",
        force=False,
        new_run=True,
    )


def test_resume_or_start_requires_snapshot_and_results_to_be_files(tmp_path: Path):
    module = load_module("resume_or_start_backlog_sweep", "scripts/resume_or_start_backlog_sweep.py")
    manifest_path = tmp_path / ".context" / "compound-engineering" / "todos" / "current_backlog_sweep.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    (manifest_path.parent / "phase2.tsv").mkdir()
    (manifest_path.parent / "phase2-results.tsv").mkdir()
    manifest_path.write_text(
        json.dumps({"phase2_snapshot": "phase2.tsv", "phase2_results": "phase2-results.tsv"}) + "\n",
        encoding="utf-8",
    )

    assert module.manifest_is_resumable(manifest_path) is False


def test_verify_active_sweep_runs_expected_command_bundle_in_order(tmp_path: Path):
    module = load_module("verify_active_sweep", "scripts/verify_active_sweep.py")

    manifest_path = tmp_path / "current_backlog_sweep.json"
    manifest_path.write_text(
        json.dumps(
            {
                "phase1_snapshot": "/tmp/phase1-snapshot.tsv",
                "phase1_results": "/tmp/phase1-results.tsv",
                "phase2_snapshot": "/tmp/phase2-snapshot.tsv",
                "phase2_results": "/tmp/phase2-results.tsv",
                "phase3_snapshot": "/tmp/phase3-snapshot.tsv",
                "phase3_results": "/tmp/phase3-results.tsv",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> int:
        calls.append(command)
        return 0

    with patch.object(module, "run_command", side_effect=fake_run):
        code, stdout, stderr = run_main(module, ["--manifest", str(manifest_path)])

    assert code == 0, stderr or stdout
    assert calls == [
        ["uv", "run", "python", "scripts/check_backlog_sweep.py", "--manifest", str(manifest_path)],
        ["uv", "run", "python", "-m", "pytest", "tests/", "-v"],
        ["uv", "run", "ruff", "check", "scripts/", "tests/"],
        ["uv", "run", "python", "scripts/check_architecture.py"],
        ["uv", "run", "python", "scripts/sync_agent_files.py", "--check"],
        ["uv", "run", "python", "scripts/check_agent_docs.py"],
    ]


def test_verify_active_sweep_stops_on_first_failed_command(tmp_path: Path):
    module = load_module("verify_active_sweep", "scripts/verify_active_sweep.py")

    manifest_path = tmp_path / "current_backlog_sweep.json"
    manifest_path.write_text(
        json.dumps(
            {
                "phase1_snapshot": "/tmp/phase1-snapshot.tsv",
                "phase1_results": "/tmp/phase1-results.tsv",
                "phase2_snapshot": "/tmp/phase2-snapshot.tsv",
                "phase2_results": "/tmp/phase2-results.tsv",
                "phase3_snapshot": "/tmp/phase3-snapshot.tsv",
                "phase3_results": "/tmp/phase3-results.tsv",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> int:
        calls.append(command)
        return 2 if len(calls) == 2 else 0

    with patch.object(module, "run_command", side_effect=fake_run):
        code, stdout, stderr = run_main(module, ["--manifest", str(manifest_path)])

    assert code == 2
    assert len(calls) == 2
    assert "FAILED" in (stdout + stderr)
