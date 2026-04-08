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
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "check_backlog_sweep.py"
SNAPSHOT_FIELDS = ["id", "company", "role_title", "board", "output_dir"]
PHASE1_SNAPSHOT_FIELDS = [
    "linear_issue_id",
    "title",
    "labels",
    "status",
    "related_job_id",
    "related_output_dir",
    "requires_user_input",
    "captured_at_utc",
]
RESULT_FIELDS = [
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
]
PHASE1_RESULT_FIELDS = [
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
]


def load_module():
    assert SCRIPT_PATH.exists(), (
        "Missing scripts/check_backlog_sweep.py. "
        "Add the backlog sweep checker before running these tests."
    )
    spec = importlib.util.spec_from_file_location("check_backlog_sweep", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop("check_backlog_sweep", None)
    sys.modules["check_backlog_sweep"] = module
    spec.loader.exec_module(module)
    return module


def write_tsv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    return path


def run_checker(module, args: list[str], *, current_wave: str = "current-wave") -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
        patch.object(module, "current_repair_wave_fingerprint", return_value=current_wave),
    ):
        code = module.main(args)
    return code, stdout.getvalue(), stderr.getvalue()


def write_manifest(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def write_evidence(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"proof")
    return str(path)


def make_job_row(tmp_path: Path, job_id: str, company: str, role_title: str, board: str) -> dict[str, str]:
    output_dir = tmp_path / "output" / company.lower() / role_title.replace(" ", "-")
    output_dir.mkdir(parents=True, exist_ok=True)
    return {
        "id": job_id,
        "company": company,
        "role_title": role_title,
        "board": board,
        "output_dir": str(output_dir),
    }


def build_trace_bundle(
    tmp_path: Path,
    *,
    phase: str,
    job: dict[str, str],
    outcome: str,
    handled_via: str,
    proof_generated_at_utc: str,
    evidence_paths: list[str] | None = None,
    review_kind: str | None = None,
) -> dict[str, str]:
    output_dir = Path(job["output_dir"])
    if evidence_paths is None:
        evidence_paths = [write_evidence(output_dir / "draft_summary.png")]
    review_dir = tmp_path / "review-proof" / phase / job["id"]
    review_dir.mkdir(parents=True, exist_ok=True)
    artifact_manifest_path = review_dir / "artifact-manifest.json"
    review_trace_path = review_dir / "review-trace.json"
    review_kind = review_kind or (
        "manual_browser_review" if handled_via.endswith("_browser") else "manual_row_review"
    )
    artifact_manifest_path.write_text(
        json.dumps(
            {
                "phase": phase,
                "job_id": job["id"],
                "output_dir": job["output_dir"],
                "artifacts": [{"path": path, "kind": "evidence"} for path in evidence_paths],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    review_trace_path.write_text(
        json.dumps(
            {
                "phase": phase,
                "job_id": job["id"],
                "outcome": outcome,
                "handled_via": handled_via,
                "review_kind": review_kind,
                "proof_generated_at_utc": proof_generated_at_utc,
                "artifacts_reviewed": evidence_paths,
                "artifacts_reviewed_count": len(evidence_paths),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "evidence_paths": "|".join(evidence_paths),
        "handled_via": handled_via,
        "review_trace_path": str(review_trace_path),
        "artifact_manifest_path": str(artifact_manifest_path),
        "proof_generated_at_utc": proof_generated_at_utc,
    }


def make_phase1_snapshot_row(
    *,
    linear_issue_id: str,
    title: str,
    labels: str = "bug",
    status: str = "Todo",
    related_job_id: str = "42",
    related_output_dir: str = "output/acme/pm",
    requires_user_input: str = "false",
    captured_at_utc: str = "2026-04-08T10:00:00Z",
) -> dict[str, str]:
    return {
        "linear_issue_id": linear_issue_id,
        "title": title,
        "labels": labels,
        "status": status,
        "related_job_id": related_job_id,
        "related_output_dir": related_output_dir,
        "requires_user_input": requires_user_input,
        "captured_at_utc": captured_at_utc,
    }


def build_phase1_trace_bundle(
    tmp_path: Path,
    *,
    issue: dict[str, str],
    outcome: str,
    handled_via: str,
    proof_generated_at_utc: str,
) -> dict[str, str]:
    review_dir = tmp_path / "review-proof" / "phase1" / issue["linear_issue_id"]
    review_dir.mkdir(parents=True, exist_ok=True)
    artifact_manifest_path = review_dir / "artifact-manifest.json"
    review_trace_path = review_dir / "review-trace.json"
    artifact_manifest_path.write_text(
        json.dumps(
            {
                "phase": "phase1",
                "linear_issue_id": issue["linear_issue_id"],
                "related_job_id": issue["related_job_id"],
                "related_output_dir": issue["related_output_dir"],
                "generated_at_utc": proof_generated_at_utc,
                "artifacts": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    review_trace_path.write_text(
        json.dumps(
            {
                "phase": "phase1",
                "job_id": issue["linear_issue_id"],
                "outcome": outcome,
                "handled_via": handled_via,
                "review_kind": "manual_row_review",
                "proof_generated_at_utc": proof_generated_at_utc,
                "handled_at_utc": proof_generated_at_utc,
                "artifacts_reviewed": [],
                "artifacts_reviewed_count": 0,
                "detail": {},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "handled_via": handled_via,
        "review_trace_path": str(review_trace_path),
        "artifact_manifest_path": str(artifact_manifest_path),
        "proof_generated_at_utc": proof_generated_at_utc,
    }


def make_result_row(
    job: dict[str, str],
    *,
    handled_at_utc: str,
    outcome: str,
    issue_id: str = "",
    notes: str = "",
    trace_bundle: dict[str, str],
    repair_wave_fingerprint: str = "current-wave",
    linear_sync_status: str = "synced",
    linear_sync_payload_path: str = "",
) -> dict[str, str]:
    return {
        "handled_at_utc": handled_at_utc,
        "id": job["id"],
        "company": job["company"],
        "role_title": job["role_title"],
        "board": job["board"],
        "output_dir": job["output_dir"],
        "outcome": outcome,
        "issue_id": issue_id,
        "evidence_paths": trace_bundle["evidence_paths"],
        "handled_via": trace_bundle["handled_via"],
        "review_trace_path": trace_bundle["review_trace_path"],
        "artifact_manifest_path": trace_bundle["artifact_manifest_path"],
        "proof_generated_at_utc": trace_bundle["proof_generated_at_utc"],
        "repair_wave_fingerprint": repair_wave_fingerprint,
        "linear_sync_status": linear_sync_status,
        "linear_sync_payload_path": linear_sync_payload_path,
        "notes": notes,
    }


def make_phase1_result_row(
    issue: dict[str, str],
    *,
    handled_at_utc: str,
    outcome: str,
    notes: str = "",
    trace_bundle: dict[str, str],
    repair_wave_fingerprint: str = "current-wave",
    linear_sync_status: str = "synced",
    linear_sync_payload_path: str = "",
) -> dict[str, str]:
    return {
        "handled_at_utc": handled_at_utc,
        "linear_issue_id": issue["linear_issue_id"],
        "title": issue["title"],
        "labels": issue["labels"],
        "status": issue["status"],
        "related_job_id": issue["related_job_id"],
        "related_output_dir": issue["related_output_dir"],
        "outcome": outcome,
        "handled_via": trace_bundle["handled_via"],
        "review_trace_path": trace_bundle["review_trace_path"],
        "artifact_manifest_path": trace_bundle["artifact_manifest_path"],
        "proof_generated_at_utc": trace_bundle["proof_generated_at_utc"],
        "repair_wave_fingerprint": repair_wave_fingerprint,
        "linear_sync_status": linear_sync_status,
        "linear_sync_payload_path": linear_sync_payload_path,
        "notes": notes,
    }


def test_checker_requires_provenance_columns_for_results(tmp_path: Path):
    module = load_module()

    job = make_job_row(tmp_path, "100", "Acme", "pm", "greenhouse")
    snapshot = write_tsv(tmp_path / "phase2-snapshot.tsv", SNAPSHOT_FIELDS, [job])
    results = write_tsv(
        tmp_path / "phase2-results.tsv",
        ["handled_at_utc", "id", "company", "role_title", "board", "output_dir", "outcome", "issue_id", "evidence_paths", "notes"],
        [
            {
                "handled_at_utc": "2026-04-08T01:00:00Z",
                "id": "100",
                "company": "Acme",
                "role_title": "pm",
                "board": "greenhouse",
                "output_dir": job["output_dir"],
                "outcome": "fixed_redrafted",
                "issue_id": "NAD-100",
                "evidence_paths": write_evidence(Path(job["output_dir"]) / "draft_summary.png"),
                "notes": "",
            }
        ],
    )

    code, stdout, stderr = run_checker(
        module,
        ["--phase2-snapshot", str(snapshot), "--phase2-results", str(results)],
    )

    assert code == 1
    assert "handled_via" in (stdout + stderr)
    assert "review_trace_path" in (stdout + stderr)


def test_checker_passes_when_phase2_and_phase3_have_full_trace_backed_latest_coverage(tmp_path: Path):
    module = load_module()

    phase2_job_a = make_job_row(tmp_path, "101", "Acme", "pm", "greenhouse")
    phase2_job_b = make_job_row(tmp_path, "102", "Beta", "staff-pm", "ashby")
    phase3_job = make_job_row(tmp_path, "201", "Gamma", "principal-pm", "lever")

    phase2_snapshot = write_tsv(tmp_path / "phase2-snapshot.tsv", SNAPSHOT_FIELDS, [phase2_job_a, phase2_job_b])
    phase3_snapshot = write_tsv(tmp_path / "phase3-snapshot.tsv", SNAPSHOT_FIELDS, [phase3_job])

    phase2_results = write_tsv(
        tmp_path / "phase2-results.tsv",
        RESULT_FIELDS,
        [
            make_result_row(
                phase2_job_a,
                handled_at_utc="2026-04-08T01:00:00Z",
                outcome="fixed_redrafted",
                issue_id="NAD-200",
                notes="reran canonical job",
                trace_bundle=build_trace_bundle(
                    tmp_path,
                    phase="phase2",
                    job=phase2_job_a,
                    outcome="fixed_redrafted",
                    handled_via="cli_manual",
                    proof_generated_at_utc="2026-04-08T01:00:00Z",
                ),
            ),
            make_result_row(
                phase2_job_b,
                handled_at_utc="2026-04-08T01:05:00Z",
                outcome="parked_requires_user_input",
                issue_id="NAD-201",
                notes="login wall",
                trace_bundle=build_trace_bundle(
                    tmp_path,
                    phase="phase2",
                    job=phase2_job_b,
                    outcome="parked_requires_user_input",
                    handled_via="cli_manual",
                    proof_generated_at_utc="2026-04-08T01:05:00Z",
                ),
            ),
        ],
    )
    phase3_results = write_tsv(
        tmp_path / "phase3-results.tsv",
        RESULT_FIELDS,
        [
            make_result_row(
                phase3_job,
                handled_at_utc="2026-04-08T02:00:00Z",
                outcome="reviewed_ready",
                notes="ready to submit",
                trace_bundle=build_trace_bundle(
                    tmp_path,
                    phase="phase3",
                    job=phase3_job,
                    outcome="reviewed_ready",
                    handled_via="draft_web_browser",
                    proof_generated_at_utc="2026-04-08T02:00:00Z",
                ),
            )
        ],
    )

    code, stdout, stderr = run_checker(
        module,
        [
            "--phase2-snapshot",
            str(phase2_snapshot),
            "--phase2-results",
            str(phase2_results),
            "--phase3-snapshot",
            str(phase3_snapshot),
            "--phase3-results",
            str(phase3_results),
        ],
    )

    assert code == 0, stderr or stdout
    assert "phase2" in stdout.lower()
    assert "phase3" in stdout.lower()
    assert "complete" in stdout.lower()


def test_checker_fails_when_snapshot_ids_are_missing_from_results(tmp_path: Path):
    module = load_module()

    job_a = make_job_row(tmp_path, "301", "Acme", "pm", "greenhouse")
    job_b = make_job_row(tmp_path, "302", "Beta", "staff-pm", "ashby")
    snapshot = write_tsv(tmp_path / "phase2-snapshot.tsv", SNAPSHOT_FIELDS, [job_a, job_b])
    results = write_tsv(
        tmp_path / "phase2-results.tsv",
        RESULT_FIELDS,
        [
            make_result_row(
                job_a,
                handled_at_utc="2026-04-08T03:00:00Z",
                outcome="fixed_redrafted",
                issue_id="NAD-300",
                trace_bundle=build_trace_bundle(
                    tmp_path,
                    phase="phase2",
                    job=job_a,
                    outcome="fixed_redrafted",
                    handled_via="cli_manual",
                    proof_generated_at_utc="2026-04-08T03:00:00Z",
                ),
            )
        ],
    )

    code, stdout, stderr = run_checker(
        module,
        ["--phase2-snapshot", str(snapshot), "--phase2-results", str(results)],
    )

    assert code == 1
    assert "302" in stdout or "302" in stderr
    assert "missing" in (stdout + stderr).lower()


def test_checker_rejects_results_for_jobs_outside_the_snapshot(tmp_path: Path):
    module = load_module()

    snapshot_job = make_job_row(tmp_path, "401", "Gamma", "principal-pm", "lever")
    ghost_job = make_job_row(tmp_path, "999", "Ghost", "pm", "greenhouse")
    snapshot = write_tsv(tmp_path / "phase3-snapshot.tsv", SNAPSHOT_FIELDS, [snapshot_job])
    results = write_tsv(
        tmp_path / "phase3-results.tsv",
        RESULT_FIELDS,
        [
            make_result_row(
                ghost_job,
                handled_at_utc="2026-04-08T04:00:00Z",
                outcome="reviewed_ready",
                trace_bundle=build_trace_bundle(
                    tmp_path,
                    phase="phase3",
                    job=ghost_job,
                    outcome="reviewed_ready",
                    handled_via="draft_web_browser",
                    proof_generated_at_utc="2026-04-08T04:00:00Z",
                ),
            )
        ],
    )

    code, stdout, stderr = run_checker(
        module,
        ["--phase3-snapshot", str(snapshot), "--phase3-results", str(results)],
    )

    assert code == 1
    assert "999" in stdout or "999" in stderr
    assert "not present in snapshot" in (stdout + stderr).lower()


def test_checker_uses_latest_row_per_job_and_validates_phase_specific_outcomes(tmp_path: Path):
    module = load_module()

    job = make_job_row(tmp_path, "501", "Gamma", "principal-pm", "lever")
    snapshot = write_tsv(tmp_path / "phase3-snapshot.tsv", SNAPSHOT_FIELDS, [job])
    results = write_tsv(
        tmp_path / "phase3-results.tsv",
        RESULT_FIELDS,
        [
            make_result_row(
                job,
                handled_at_utc="2026-04-08T05:00:00Z",
                outcome="fixed_redrafted",
                issue_id="NAD-500",
                notes="first pass found a defect",
                trace_bundle=build_trace_bundle(
                    tmp_path,
                    phase="phase3",
                    job=job,
                    outcome="fixed_redrafted",
                    handled_via="draft_web_browser",
                    proof_generated_at_utc="2026-04-08T05:00:00Z",
                ),
            ),
            make_result_row(
                job,
                handled_at_utc="2026-04-08T05:10:00Z",
                outcome="reviewed_ready",
                notes="second pass verified ready",
                trace_bundle=build_trace_bundle(
                    tmp_path,
                    phase="phase3",
                    job=job,
                    outcome="reviewed_ready",
                    handled_via="draft_web_browser",
                    proof_generated_at_utc="2026-04-08T05:10:00Z",
                ),
            ),
        ],
    )

    code, stdout, stderr = run_checker(
        module,
        ["--phase3-snapshot", str(snapshot), "--phase3-results", str(results)],
    )

    assert code == 0, stderr or stdout
    assert "duplicate" in stdout.lower()
    assert "reviewed_ready" in stdout


def test_checker_rejects_phase2_only_outcomes_in_phase3_ledgers(tmp_path: Path):
    module = load_module()

    job = make_job_row(tmp_path, "601", "Gamma", "principal-pm", "lever")
    snapshot = write_tsv(tmp_path / "phase3-snapshot.tsv", SNAPSHOT_FIELDS, [job])
    results = write_tsv(
        tmp_path / "phase3-results.tsv",
        RESULT_FIELDS,
        [
            make_result_row(
                job,
                handled_at_utc="2026-04-08T06:00:00Z",
                outcome="unsupported_parked",
                trace_bundle=build_trace_bundle(
                    tmp_path,
                    phase="phase3",
                    job=job,
                    outcome="unsupported_parked",
                    handled_via="cli_manual",
                    proof_generated_at_utc="2026-04-08T06:00:00Z",
                    review_kind="manual_row_review",
                ),
            )
        ],
    )

    code, stdout, stderr = run_checker(
        module,
        ["--phase3-snapshot", str(snapshot), "--phase3-results", str(results)],
    )

    assert code == 1
    assert "unsupported_parked" in stdout or "unsupported_parked" in stderr
    assert "invalid outcome" in (stdout + stderr).lower()


def test_checker_supports_manifest_input_and_fresh_proof_timestamps(tmp_path: Path):
    module = load_module()

    phase1_issue = make_phase1_snapshot_row(linear_issue_id="NAD-700", title="Fix drifted ticket state")
    phase2_job = make_job_row(tmp_path, "701", "Acme", "pm", "greenhouse")
    phase3_job = make_job_row(tmp_path, "702", "Beta", "staff-pm", "ashby")
    phase1_snapshot = write_tsv(tmp_path / "phase1-snapshot.tsv", PHASE1_SNAPSHOT_FIELDS, [phase1_issue])
    phase2_snapshot = write_tsv(tmp_path / "phase2-snapshot.tsv", SNAPSHOT_FIELDS, [phase2_job])
    phase3_snapshot = write_tsv(tmp_path / "phase3-snapshot.tsv", SNAPSHOT_FIELDS, [phase3_job])
    phase1_results = write_tsv(
        tmp_path / "phase1-results.tsv",
        PHASE1_RESULT_FIELDS,
        [
            make_phase1_result_row(
                phase1_issue,
                handled_at_utc="2026-04-08T06:55:00Z",
                outcome="fixed_verified",
                notes="mirrored repo truth back to Linear",
                trace_bundle=build_phase1_trace_bundle(
                    tmp_path,
                    issue=phase1_issue,
                    outcome="fixed_verified",
                    handled_via="cli_manual",
                    proof_generated_at_utc="2026-04-08T06:55:00Z",
                ),
            )
        ],
    )
    phase2_results = write_tsv(
        tmp_path / "phase2-results.tsv",
        RESULT_FIELDS,
        [
            make_result_row(
                phase2_job,
                handled_at_utc="2026-04-08T07:00:00Z",
                outcome="fixed_redrafted",
                issue_id="NAD-701",
                trace_bundle=build_trace_bundle(
                    tmp_path,
                    phase="phase2",
                    job=phase2_job,
                    outcome="fixed_redrafted",
                    handled_via="cli_manual",
                    proof_generated_at_utc="2026-04-08T07:00:00Z",
                ),
            )
        ],
    )
    phase3_results = write_tsv(
        tmp_path / "phase3-results.tsv",
        RESULT_FIELDS,
        [
            make_result_row(
                phase3_job,
                handled_at_utc="2026-04-08T07:10:00Z",
                outcome="reviewed_ready",
                trace_bundle=build_trace_bundle(
                    tmp_path,
                    phase="phase3",
                    job=phase3_job,
                    outcome="reviewed_ready",
                    handled_via="draft_web_browser",
                    proof_generated_at_utc="2026-04-08T07:10:00Z",
                ),
            )
        ],
    )
    manifest = write_manifest(
        tmp_path / "current_backlog_sweep.json",
        {
            "run_id": "2026-04-08T07-00-00Z",
            "phase1_snapshot": str(phase1_snapshot),
            "phase1_results": str(phase1_results),
            "phase1_started_at_utc": "2026-04-08T06:50:00Z",
            "phase2_snapshot": str(phase2_snapshot),
            "phase2_results": str(phase2_results),
            "phase2_started_at_utc": "2026-04-08T06:59:00Z",
            "phase3_snapshot": str(phase3_snapshot),
            "phase3_results": str(phase3_results),
            "phase3_started_at_utc": "2026-04-08T07:09:00Z",
        },
    )

    code, stdout, stderr = run_checker(module, ["--manifest", str(manifest)])

    assert code == 0, stderr or stdout
    assert "passed" in stdout.lower()


def test_checker_rejects_latest_row_from_old_repair_wave(tmp_path: Path):
    module = load_module()

    job = make_job_row(tmp_path, "101", "Acme", "pm", "greenhouse")
    snapshot = write_tsv(tmp_path / "phase2-snapshot.tsv", SNAPSHOT_FIELDS, [job])
    results = write_tsv(
        tmp_path / "phase2-results.tsv",
        RESULT_FIELDS,
        [
            make_result_row(
                job,
                handled_at_utc="2026-04-08T01:00:00Z",
                outcome="fixed_redrafted",
                trace_bundle=build_trace_bundle(
                    tmp_path,
                    phase="phase2",
                    job=job,
                    outcome="fixed_redrafted",
                    handled_via="cli_manual",
                    proof_generated_at_utc="2026-04-08T01:00:00Z",
                ),
                repair_wave_fingerprint="old-wave",
            )
        ],
    )
    manifest = write_manifest(
        tmp_path / "current_backlog_sweep.json",
        {
            "phase2_snapshot": str(snapshot),
            "phase2_results": str(results),
            "phase2_started_at_utc": "2026-04-08T00:30:00Z",
        },
    )

    code, stdout, stderr = run_checker(module, ["--manifest", str(manifest)])

    assert code == 1
    assert "repair_wave_fingerprint" in (stdout + stderr)


def test_checker_rejects_unsynced_phase1_terminal_row(tmp_path: Path):
    module = load_module()

    issue = make_phase1_snapshot_row(linear_issue_id="NAD-101", title="Fix proof drift")
    phase1_snapshot = write_tsv(tmp_path / "phase1-snapshot.tsv", PHASE1_SNAPSHOT_FIELDS, [issue])
    phase1_results = write_tsv(
        tmp_path / "phase1-results.tsv",
        PHASE1_RESULT_FIELDS,
        [
            make_phase1_result_row(
                issue,
                handled_at_utc="2026-04-08T10:05:00Z",
                outcome="blocked_external",
                notes="requires vendor escalation",
                trace_bundle=build_phase1_trace_bundle(
                    tmp_path,
                    issue=issue,
                    outcome="blocked_external",
                    handled_via="cli_manual",
                    proof_generated_at_utc="2026-04-08T10:05:00Z",
                ),
                linear_sync_status="pending",
                linear_sync_payload_path=str(tmp_path / "linear-sync" / "phase1.json"),
            )
        ],
    )
    manifest = write_manifest(
        tmp_path / "current_backlog_sweep.json",
        {
            "phase1_snapshot": str(phase1_snapshot),
            "phase1_results": str(phase1_results),
            "phase1_started_at_utc": "2026-04-08T10:00:00Z",
        },
    )

    code, stdout, stderr = run_checker(module, ["--manifest", str(manifest)])

    assert code == 1
    assert "not synced to Linear" in (stdout + stderr)


def test_checker_requires_existing_evidence_for_latest_rows(tmp_path: Path):
    module = load_module()

    job = make_job_row(tmp_path, "801", "Gamma", "principal-pm", "lever")
    missing_path = str(tmp_path / "missing-proof.png")
    trace_bundle = build_trace_bundle(
        tmp_path,
        phase="phase3",
        job=job,
        outcome="reviewed_ready",
        handled_via="draft_web_browser",
        proof_generated_at_utc="2026-04-08T08:00:00Z",
        evidence_paths=[missing_path],
    )
    results = write_tsv(
        tmp_path / "phase3-results.tsv",
        RESULT_FIELDS,
        [
            make_result_row(
                job,
                handled_at_utc="2026-04-08T08:00:00Z",
                outcome="reviewed_ready",
                trace_bundle=trace_bundle,
            )
        ],
    )
    snapshot = write_tsv(tmp_path / "phase3-snapshot.tsv", SNAPSHOT_FIELDS, [job])

    code, stdout, stderr = run_checker(
        module,
        ["--phase3-snapshot", str(snapshot), "--phase3-results", str(results)],
    )

    assert code == 1
    assert "evidence" in (stdout + stderr).lower()
    assert "missing-proof.png" in (stdout + stderr)


def test_checker_rejects_phase3_reviewed_ready_without_browser_trace(tmp_path: Path):
    module = load_module()

    job = make_job_row(tmp_path, "901", "Gamma", "principal-pm", "lever")
    snapshot = write_tsv(tmp_path / "phase3-snapshot.tsv", SNAPSHOT_FIELDS, [job])
    results = write_tsv(
        tmp_path / "phase3-results.tsv",
        RESULT_FIELDS,
        [
            make_result_row(
                job,
                handled_at_utc="2026-04-08T09:00:00Z",
                outcome="reviewed_ready",
                trace_bundle=build_trace_bundle(
                    tmp_path,
                    phase="phase3",
                    job=job,
                    outcome="reviewed_ready",
                    handled_via="cli_manual",
                    proof_generated_at_utc="2026-04-08T09:00:00Z",
                    review_kind="manual_row_review",
                ),
            )
        ],
    )

    code, stdout, stderr = run_checker(
        module,
        ["--phase3-snapshot", str(snapshot), "--phase3-results", str(results)],
    )

    assert code == 1
    assert "browser" in (stdout + stderr).lower()
    assert "handled_via" in (stdout + stderr).lower()


def test_checker_rejects_shared_evidence_outside_output_dir_and_review_dir(tmp_path: Path):
    module = load_module()

    job = make_job_row(tmp_path, "902", "Gamma", "principal-pm", "lever")
    shared_path = write_evidence(tmp_path / "shared" / "batch-proof.png")
    trace_bundle = build_trace_bundle(
        tmp_path,
        phase="phase3",
        job=job,
        outcome="reviewed_ready",
        handled_via="draft_web_browser",
        proof_generated_at_utc="2026-04-08T09:10:00Z",
        evidence_paths=[shared_path],
    )
    snapshot = write_tsv(tmp_path / "phase3-snapshot.tsv", SNAPSHOT_FIELDS, [job])
    results = write_tsv(
        tmp_path / "phase3-results.tsv",
        RESULT_FIELDS,
        [
            make_result_row(
                job,
                handled_at_utc="2026-04-08T09:10:00Z",
                outcome="reviewed_ready",
                trace_bundle=trace_bundle,
            )
        ],
    )

    code, stdout, stderr = run_checker(
        module,
        ["--phase3-snapshot", str(snapshot), "--phase3-results", str(results)],
    )

    assert code == 1
    assert "output_dir" in (stdout + stderr)
    assert "review directory" in (stdout + stderr).lower()
