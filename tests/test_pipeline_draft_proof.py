import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_draft_review_state_blocks_empty_report_fields(tmp_path):
    proof = load_module("pipeline_draft_proof", "scripts/pipeline_draft_proof.py")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "workday_autofill_report.json").write_text(
        json.dumps({"fields": [], "unknown_questions": []}),
        encoding="utf-8",
    )
    (submit_dir / "workday_autofill_pre_submit.png").write_text("png", encoding="utf-8")

    state = proof.draft_review_state(out_dir, board_name="workday")

    assert state["state"] == "blocked"
    assert "empty autofill report" in state["reason"].casefold()


def test_draft_review_state_marks_legacy_when_only_legacy_artifacts_exist(tmp_path, monkeypatch):
    proof = load_module("pipeline_draft_proof_legacy", "scripts/pipeline_draft_proof.py")
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()

    monkeypatch.setitem(
        sys.modules,
        "application_submit_common",
        type(
            "SubmitCommon",
            (),
            {
                "load_pending_user_input_for_submit_attempt": staticmethod(lambda _output_dir: None),
            },
        )(),
    )
    monkeypatch.setattr(
        proof,
        "_draft_proof_blocker_entries",
        lambda *_args, **_kwargs: (
            {
                "submit_dirname": "submit",
                "submit_dir": out_dir / "submit",
                "board_name": "greenhouse",
                "artifact_sources": {"report_json": "legacy_default"},
                "report_json": out_dir / "submit" / "greenhouse_autofill_report.json",
            },
            [],
        ),
    )
    monkeypatch.setattr(proof, "_optional_review_note_count", lambda _path: 0)
    monkeypatch.setattr(proof, "_historical_proof_dirs", lambda *_args, **_kwargs: [])

    state = proof.draft_review_state(out_dir, board_name="greenhouse")

    assert state["state"] == "legacy"


def test_draft_review_state_marks_stale_when_blocked_current_attempt_has_historical_proof(tmp_path, monkeypatch):
    proof = load_module("pipeline_draft_proof_stale", "scripts/pipeline_draft_proof.py")
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()

    monkeypatch.setitem(
        sys.modules,
        "application_submit_common",
        type(
            "SubmitCommon",
            (),
            {
                "load_pending_user_input_for_submit_attempt": staticmethod(lambda _output_dir: None),
            },
        )(),
    )
    monkeypatch.setattr(
        proof,
        "_draft_proof_blocker_entries",
        lambda *_args, **_kwargs: (
            {
                "submit_dirname": "submit",
                "submit_dir": out_dir / "submit",
                "board_name": "greenhouse",
                "artifact_sources": {"report_json": "active_submit"},
            },
            [{"field_name": "pre_submit_screenshot", "reason": "Missing current screenshot proof."}],
        ),
    )
    monkeypatch.setattr(proof, "_optional_review_note_count", lambda _path: 0)
    monkeypatch.setattr(proof, "_historical_proof_dirs", lambda *_args, **_kwargs: ["submit-20260401T010101Z"])

    state = proof.draft_review_state(out_dir, board_name="greenhouse")

    assert state["state"] == "stale"
    assert "historical proof exists" in state["reason"].casefold()


def test_draft_review_state_marks_ready_when_required_proof_exists(tmp_path, monkeypatch):
    proof = load_module("pipeline_draft_proof_ready", "scripts/pipeline_draft_proof.py")
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()

    monkeypatch.setitem(
        sys.modules,
        "application_submit_common",
        type(
            "SubmitCommon",
            (),
            {
                "load_pending_user_input_for_submit_attempt": staticmethod(lambda _output_dir: None),
            },
        )(),
    )
    monkeypatch.setattr(
        proof,
        "_draft_proof_blocker_entries",
        lambda *_args, **_kwargs: (
            {
                "submit_dirname": "submit",
                "submit_dir": out_dir / "submit",
                "board_name": "greenhouse",
                "artifact_sources": {"report_json": "active_submit", "pre_submit_screenshot": "active_submit"},
                "report_json": out_dir / "submit" / "greenhouse_autofill_report.json",
            },
            [],
        ),
    )
    monkeypatch.setattr(proof, "_optional_review_note_count", lambda _path: 0)
    monkeypatch.setattr(proof, "_historical_proof_dirs", lambda *_args, **_kwargs: [])

    state = proof.draft_review_state(out_dir, board_name="greenhouse")

    assert state["state"] == "ready"
