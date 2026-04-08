# tests/test_pipeline_audit_loop.py
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from pipeline_audit_loop import (  # type: ignore[import-not-found]
    audit_draft_outcome,
    audit_stopped_outcome,
    write_audit_failure_report,
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_audit_draft_outcome_flags_unaccounted_optional_field(tmp_path):
    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "ashby_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    _write_json(
        submit_dir / "ashby_autofill_report.json",
        {
            "fields": [
                {
                    "field_name": "application_full_name",
                    "label": "Full Name",
                    "status": "filled",
                    "required": True,
                    "optional": False,
                },
                {
                    "field_name": "portfolio_link",
                    "label": "Portfolio Link",
                    "status": "planned",
                    "required": False,
                    "optional": True,
                },
            ],
            "unknown_questions": [],
        },
    )
    _write_json(submit_dir / "ashby_unknown_questions.json", {"generated_at_utc": "2026-03-31T00:00:00+00:00", "questions": []})

    decision = audit_draft_outcome(out_dir, board_name="ashby", missing_items=[])

    assert decision.kind == "repairable"
    assert decision.failure_type == "draft_audit_incomplete"
    assert "Portfolio Link" in decision.reason


def test_audit_draft_outcome_allows_pending_user_input_blockers(tmp_path):
    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "ashby_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    _write_json(
        submit_dir / "ashby_autofill_report.json",
        {
            "fields": [
                {
                    "field_name": "portfolio_link",
                    "label": "Portfolio Link",
                    "status": "planned",
                    "required": False,
                    "optional": True,
                }
            ],
            "unknown_questions": [],
        },
    )
    _write_json(
        submit_dir / "pending_user_input.json",
        {
            "status": "pending_user_input",
            "board": "ashby",
            "questions": [
                {
                    "field_name": "portfolio_link",
                    "label": "Portfolio Link",
                    "required": False,
                }
            ],
        },
    )

    decision = audit_draft_outcome(out_dir, board_name="ashby", missing_items=[])

    assert decision.kind == "terminal"
    assert decision.failure_type == "pending_user_input"


def test_audit_draft_outcome_fails_closed_when_current_proof_artifacts_are_missing(tmp_path):
    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit"
    docs_dir = out_dir / "documents"
    submit_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    (submit_dir / "greenhouse_autofill_payload.json").write_text("{}", encoding="utf-8")
    (docs_dir / "Jerrison Li Resume - Example.pdf").write_text("resume", encoding="utf-8")
    (docs_dir / "Jerrison Li Cover Letter - Example.pdf").write_text("cover", encoding="utf-8")
    (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")

    decision = audit_draft_outcome(out_dir, board_name="greenhouse")

    assert decision.kind == "repairable"
    assert decision.failure_type == "draft_audit_incomplete"
    assert "missing the required autofill report proof" in decision.reason
    assert decision.artifacts == {"submit_dir": str(submit_dir)}


def test_audit_draft_outcome_allows_greenhouse_draft_without_review_screenshot(tmp_path):
    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "greenhouse_autofill_report.json").write_text("{}", encoding="utf-8")
    (submit_dir / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")

    decision = audit_draft_outcome(out_dir, board_name="greenhouse")

    assert decision.kind == "ready"
    assert decision.failure_type is None


def test_audit_draft_outcome_allows_active_greenhouse_draft_without_review_screenshot(tmp_path):
    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit-20260406T230927Z"
    submit_dir.mkdir(parents=True)
    (out_dir / ".active_submit_dir").write_text("submit-20260406T230927Z\n", encoding="utf-8")
    (submit_dir / "greenhouse_autofill_report.json").write_text("{}", encoding="utf-8")
    (submit_dir / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")

    decision = audit_draft_outcome(out_dir, board_name="greenhouse")

    assert decision.kind == "ready"
    assert decision.failure_type is None


def test_audit_draft_outcome_blocks_content_duplicate_review_when_board_requires_distinct_proof(tmp_path):
    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "custom_autofill_report.json").write_text("{}", encoding="utf-8")
    (submit_dir / "custom_autofill_pre_submit.png").write_bytes(b"same-proof")
    (submit_dir / "custom_autofill_review.png").write_bytes(b"same-proof")

    with mock.patch("autofill_common.board_requires_distinct_review_screenshot", return_value=True):
        decision = audit_draft_outcome(out_dir, board_name="custom")

    assert decision.kind == "repairable"
    assert decision.failure_type == "draft_audit_incomplete"
    assert "multiple required proof checkpoints" in decision.reason.casefold()
    assert decision.artifacts == {"submit_dir": str(submit_dir)}


def test_audit_draft_outcome_marks_rendered_audit_mismatch_as_repairable(tmp_path):
    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    screenshot_path = submit_dir / "ashby_autofill_pre_submit.png"
    screenshot_path.write_text("png", encoding="utf-8")
    _write_json(
        submit_dir / "ashby_autofill_report.json",
        {
            "fields": [
                {
                    "field_name": "work_auth",
                    "label": "Work authorization",
                    "field_type": "radio",
                    "selected_labels": ["No"],
                    "status": "filled",
                    "required": True,
                    "optional": False,
                }
            ],
            "unknown_questions": [],
        },
    )
    _write_json(
        submit_dir / "application_answers.json",
        {
            "questions": [
                {
                    "field_name": "work_auth",
                    "label": "Work authorization",
                    "type": "radio",
                }
            ],
            "answers": {"work_auth": "Yes"},
        },
    )

    decision = audit_draft_outcome(out_dir, board_name="ashby", missing_items=[])

    assert decision.kind == "repairable"
    assert decision.failure_type == "rendered_audit_mismatch"
    assert "Work authorization" in decision.reason
    assert decision.artifacts == {"screenshot": str(screenshot_path)}


def test_audit_draft_outcome_accepts_report_kind_and_value_for_rendered_audit(tmp_path):
    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    screenshot_path = submit_dir / "ashby_autofill_pre_submit.png"
    screenshot_path.write_text("png", encoding="utf-8")
    _write_json(
        submit_dir / "ashby_autofill_report.json",
        {
            "fields": [
                {
                    "field_name": "employment_history",
                    "label": "Have you ever been employed here?",
                    "kind": "choice",
                    "value": "No",
                    "status": "filled",
                    "required": True,
                    "optional": False,
                }
            ],
            "unknown_questions": [],
        },
    )
    _write_json(
        submit_dir / "application_answers.json",
        {
            "questions": [
                {
                    "field_name": "employment_history",
                    "label": "Have you ever been employed here?",
                    "type": "Boolean",
                }
            ],
            "answers": {"employment_history": "No"},
        },
    )

    decision = audit_draft_outcome(out_dir, board_name="ashby", missing_items=[])

    assert decision.kind == "ready"
    assert decision.failure_type is None


def test_audit_draft_outcome_merges_checkbox_group_report_rows_for_rendered_audit(tmp_path):
    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    screenshot_path = submit_dir / "linkedin_autofill_pre_submit.png"
    screenshot_path.write_text("png", encoding="utf-8")
    _write_json(
        submit_dir / "linkedin_autofill_report.json",
        {
            "fields": [
                {
                    "field_name": "work_env",
                    "label": "What types of work environments are you open to?",
                    "kind": "checkbox_group",
                    "value": "Hybrid",
                    "status": "filled",
                    "required": True,
                    "optional": False,
                },
                {
                    "field_name": "work_env",
                    "label": "What types of work environments are you open to?",
                    "kind": "checkbox_group",
                    "value": "Remote",
                    "status": "filled",
                    "required": True,
                    "optional": False,
                },
            ],
            "unknown_questions": [],
        },
    )
    _write_json(
        submit_dir / "application_answers.json",
        {
            "questions": [
                {
                    "field_name": "work_env",
                    "label": "What types of work environments are you open to?",
                    "type": "multi_value_multi_select",
                }
            ],
            "answers": {"work_env": ["Hybrid", "Remote"]},
        },
    )

    decision = audit_draft_outcome(out_dir, board_name="linkedin", missing_items=[])

    assert decision.kind == "ready"
    assert decision.failure_type is None


def test_audit_draft_outcome_uses_matching_expected_field_when_report_kind_is_blank(tmp_path):
    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    screenshot_path = submit_dir / "phenom_autofill_pre_submit.png"
    screenshot_path.write_text("png", encoding="utf-8")
    _write_json(
        submit_dir / "phenom_autofill_report.json",
        {
            "fields": [
                {
                    "field_name": "relocation_assistance",
                    "label": "Do you require relocation assistance?",
                    "kind": "",
                    "value": "No",
                    "status": "filled",
                    "required": True,
                    "optional": False,
                }
            ],
            "unknown_questions": [],
        },
    )
    _write_json(
        submit_dir / "application_answers.json",
        {
            "questions": [
                {
                    "field_name": "relocation_assistance",
                    "label": "Do you require relocation assistance?",
                    "type": "select",
                }
            ],
            "answers": {"relocation_assistance": "No"},
        },
    )

    decision = audit_draft_outcome(out_dir, board_name="phenom", missing_items=[])

    assert decision.kind == "ready"
    assert decision.failure_type is None


def test_audit_draft_outcome_ignores_generic_checkbox_affirmative_when_specific_value_exists(tmp_path):
    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    screenshot_path = submit_dir / "avature_autofill_pre_submit.png"
    screenshot_path.write_text("png", encoding="utf-8")
    _write_json(
        submit_dir / "avature_autofill_report.json",
        {
            "fields": [
                {
                    "field_name": "privacy_ack",
                    "label": "Please confirm the privacy policy acknowledgement.",
                    "kind": "checkbox",
                    "value": "Yes",
                    "status": "filled",
                    "required": True,
                    "optional": False,
                },
                {
                    "field_name": "privacy_ack",
                    "label": "Please confirm the privacy policy acknowledgement.",
                    "kind": "checkbox",
                    "value": "I acknowledge",
                    "status": "filled",
                    "required": True,
                    "optional": False,
                },
            ],
            "unknown_questions": [],
        },
    )
    _write_json(
        submit_dir / "application_answers.json",
        {
            "questions": [
                {
                    "field_name": "privacy_ack",
                    "label": "Please confirm the privacy policy acknowledgement.",
                    "type": "checkbox",
                }
            ],
            "answers": {"privacy_ack": "I acknowledge"},
        },
    )

    decision = audit_draft_outcome(out_dir, board_name="avature", missing_items=[])

    assert decision.kind == "ready"
    assert decision.failure_type is None


def test_audit_draft_outcome_marks_multi_value_option_mismatch_as_repairable(tmp_path):
    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    screenshot_path = submit_dir / "ashby_autofill_pre_submit.png"
    screenshot_path.write_text("png", encoding="utf-8")
    _write_json(
        submit_dir / "ashby_autofill_report.json",
        {
            "fields": [
                {
                    "field_name": "regions",
                    "label": "Preferred regions",
                    "field_type": "MultiValueSelect",
                    "selected_labels": ["United States", "Canada"],
                    "status": "filled",
                    "required": True,
                    "optional": False,
                }
            ],
            "unknown_questions": [],
        },
    )
    _write_json(
        submit_dir / "application_answers.json",
        {
            "questions": [
                {
                    "field_name": "regions",
                    "label": "Preferred regions",
                    "type": "multi_value_multi_select",
                }
            ],
            "answers": {"regions": ["United States", "Mexico"]},
        },
    )

    decision = audit_draft_outcome(out_dir, board_name="ashby", missing_items=[])

    assert decision.kind == "repairable"
    assert decision.failure_type == "rendered_audit_mismatch"
    assert "Preferred regions" in decision.reason
    assert decision.artifacts == {"screenshot": str(screenshot_path)}


def test_audit_draft_outcome_keeps_screenshot_when_rendered_field_is_missing(tmp_path):
    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    screenshot_path = submit_dir / "ashby_autofill_pre_submit.png"
    screenshot_path.write_text("png", encoding="utf-8")
    _write_json(
        submit_dir / "ashby_autofill_report.json",
        {
            "fields": [],
            "unknown_questions": [],
        },
    )
    _write_json(
        submit_dir / "application_answers.json",
        {
            "questions": [
                {
                    "field_name": "work_auth",
                    "label": "Work authorization",
                    "type": "radio",
                }
            ],
            "answers": {"work_auth": "Yes"},
        },
    )

    decision = audit_draft_outcome(out_dir, board_name="ashby", missing_items=[])

    assert decision.kind == "repairable"
    assert decision.failure_type == "rendered_audit_mismatch"
    assert decision.artifacts == {"screenshot": str(screenshot_path)}


def test_audit_draft_outcome_matches_fields_by_label_when_report_field_name_differs(tmp_path):
    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    screenshot_path = submit_dir / "greenhouse_autofill_review.png"
    screenshot_path.write_text("png", encoding="utf-8")
    _write_json(
        submit_dir / "greenhouse_autofill_report.json",
        {
            "fields": [
                {
                    "label": "Are you legally authorized to work in the US?",
                    "field_type": "select",
                    "selected_labels": ["Yes"],
                    "status": "filled",
                    "required": True,
                    "optional": False,
                }
            ],
            "unknown_questions": [],
        },
    )
    _write_json(
        submit_dir / "application_answers.json",
        {
            "questions": [
                {
                    "field_name": "work_auth",
                    "label": "Are you legally authorized to work in the US?",
                    "type": "select",
                }
            ],
            "answers": {"work_auth": "Yes"},
        },
    )

    decision = audit_draft_outcome(out_dir, board_name="greenhouse", missing_items=[])

    assert decision.kind == "ready"
    assert decision.failure_type is None


def test_audit_draft_outcome_accepts_stringified_multi_select_report_values(tmp_path):
    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    screenshot_path = submit_dir / "workday_autofill_pre_submit.png"
    screenshot_path.write_text("png", encoding="utf-8")
    _write_json(
        submit_dir / "workday_autofill_report.json",
        {
            "fields": [
                {
                    "field_name": "former_employment",
                    "label": "Have you ever worked here before?",
                    "kind": "",
                    "value": "['I have not worked here before.']",
                    "status": "filled",
                    "required": True,
                    "optional": False,
                }
            ],
            "unknown_questions": [],
        },
    )
    _write_json(
        submit_dir / "application_answers.json",
        {
            "questions": [
                {
                    "field_name": "former_employment",
                    "label": "Have you ever worked here before?",
                    "type": "multi_value_multi_select",
                }
            ],
            "answers": {"former_employment": ["I have not worked here before."]},
        },
    )

    decision = audit_draft_outcome(out_dir, board_name="workday", missing_items=[])

    assert decision.kind == "ready"
    assert decision.failure_type is None


def test_audit_draft_outcome_fails_closed_when_deterministic_answers_are_missing(tmp_path):
    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    screenshot_path = submit_dir / "greenhouse_autofill_review.png"
    screenshot_path.write_text("png", encoding="utf-8")
    _write_json(
        submit_dir / "greenhouse_autofill_report.json",
        {
            "fields": [
                {
                    "field_name": "work_auth",
                    "label": "Are you legally authorized to work in the US?",
                    "field_type": "select",
                    "source": "generated_application_answer",
                    "selected_labels": ["Yes"],
                    "status": "filled",
                    "required": True,
                    "optional": False,
                }
            ],
            "unknown_questions": [],
        },
    )

    decision = audit_draft_outcome(out_dir, board_name="greenhouse", missing_items=[])

    assert decision.kind == "repairable"
    assert decision.failure_type == "draft_audit_incomplete"
    assert "application answers" in decision.reason.casefold()
    assert decision.artifacts == {"submit_dir": str(submit_dir)}


def test_audit_draft_outcome_allows_profile_backed_deterministic_answers_without_payload(tmp_path):
    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    screenshot_path = submit_dir / "greenhouse_autofill_review.png"
    screenshot_path.write_text("png", encoding="utf-8")
    _write_json(
        submit_dir / "greenhouse_autofill_report.json",
        {
            "fields": [
                {
                    "field_name": "work_auth",
                    "label": "Are you legally authorized to work in the US?",
                    "field_type": "select",
                    "source": "application_profile.md",
                    "selected_labels": ["Yes"],
                    "status": "filled",
                    "required": True,
                    "optional": False,
                }
            ],
            "unknown_questions": [],
        },
    )

    decision = audit_draft_outcome(out_dir, board_name="greenhouse", missing_items=[])

    assert decision.kind == "ready"
    assert decision.failure_type is None


def test_audit_draft_outcome_fails_closed_when_some_deterministic_answers_are_missing(tmp_path):
    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    screenshot_path = submit_dir / "greenhouse_autofill_review.png"
    screenshot_path.write_text("png", encoding="utf-8")
    _write_json(
        submit_dir / "greenhouse_autofill_report.json",
        {
            "fields": [
                {
                    "field_name": "work_auth",
                    "label": "Are you legally authorized to work in the US?",
                    "field_type": "select",
                    "selected_labels": ["Yes"],
                    "status": "filled",
                    "required": True,
                    "optional": False,
                }
            ],
            "unknown_questions": [],
        },
    )
    _write_json(
        submit_dir / "application_answers.json",
        {
            "questions": [
                {
                    "field_name": "work_auth",
                    "label": "Are you legally authorized to work in the US?",
                    "type": "select",
                },
                {
                    "field_name": "sponsorship",
                    "label": "Will you require sponsorship?",
                    "type": "select",
                },
            ],
            "answers": {"work_auth": "Yes"},
        },
    )

    decision = audit_draft_outcome(out_dir, board_name="greenhouse", missing_items=[])

    assert decision.kind == "repairable"
    assert decision.failure_type == "draft_audit_incomplete"
    assert "application answers" in decision.reason.casefold()
    assert decision.artifacts == {"submit_dir": str(submit_dir)}


def test_audit_draft_outcome_allows_report_only_deterministic_field_when_payload_has_none(tmp_path):
    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    screenshot_path = submit_dir / "greenhouse_autofill_review.png"
    screenshot_path.write_text("png", encoding="utf-8")
    _write_json(
        submit_dir / "greenhouse_autofill_report.json",
        {
            "fields": [
                {
                    "field_name": "profile_region",
                    "label": "Profile region",
                    "field_type": "select",
                    "selected_labels": ["United States"],
                    "status": "filled",
                    "required": False,
                    "optional": True,
                }
            ],
            "unknown_questions": [],
        },
    )
    _write_json(
        submit_dir / "application_answers.json",
        {
            "questions": [
                {
                    "field_name": "why_here",
                    "label": "Why this role?",
                    "type": "textarea",
                }
            ],
            "answers": {"why_here": "Because it fits my background."},
        },
    )

    decision = audit_draft_outcome(out_dir, board_name="greenhouse", missing_items=[])

    assert decision.kind == "ready"
    assert decision.failure_type is None


def test_audit_stopped_outcome_marks_external_apply_as_terminal():
    decision = audit_stopped_outcome(failure_type="external_apply", error_message="LinkedIn redirected externally.")

    assert decision.kind == "terminal"
    assert decision.failure_type == "external_apply"


def test_audit_stopped_outcome_treats_rate_limited_retries_exhausted_as_terminal_service_unavailable():
    decision = audit_stopped_outcome(
        failure_type="retries_exhausted",
        error_message=(
            "Failed after 3 retries: Asset generation failed: extracted page looks like an HTML/login/blocker shell "
            "instead of a job description. Suggestion: Job board is rate-limiting. Try again in a few hours or "
            "provide JD text directly."
        ),
    )

    assert decision.kind == "terminal"
    assert decision.failure_type == "service_unavailable"


def test_audit_stopped_outcome_marks_currently_supported_unsupported_rows_as_repairable():
    decision = audit_stopped_outcome(
        failure_type="unsupported",
        error_message="Unsupported application board for URL: https://jobs.supermicro.com/job/pm",
        job_url="https://jobs.supermicro.com/job/pm",
    )

    assert decision.kind == "repairable"
    assert decision.failure_type == "stopped_audit_repairable"
    assert "currently recognizes this URL as successfactors" in decision.reason


def test_audit_stopped_outcome_keeps_truly_unsupported_rows_terminal():
    decision = audit_stopped_outcome(
        failure_type="unsupported",
        error_message="Unsupported application board for URL: https://jobs.example.com/company/role",
        job_url="https://jobs.example.com/company/role",
    )

    assert decision.kind == "terminal"
    assert decision.failure_type == "unsupported"


def test_write_audit_failure_report_creates_job_note_and_index(tmp_path):
    output_root = tmp_path / "output"
    out_dir = output_root / "acme" / "principal-pm"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "ashby_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    _write_json(submit_dir / "ashby_autofill_report.json", {"fields": [], "unknown_questions": []})

    note_path, index_path = write_audit_failure_report(
        output_dir=out_dir,
        job_id=42,
        summary="Draft audit found missing proof after three repair cycles.",
        suggestions=["Inspect the board-specific selector and rerun the draft."],
        attempts=[
            "Audit retry 1/3: missing proof",
            "Audit retry 2/3: missing proof",
            "Audit retry 3/3: missing proof",
        ],
        output_root=output_root,
    )

    note_text = note_path.read_text(encoding="utf-8")
    index_text = index_path.read_text(encoding="utf-8")

    assert note_path.exists()
    assert "# Audit Failure" in note_text
    assert "## What Failed" in note_text
    assert "Draft audit found missing proof after three repair cycles." in note_text
    assert "Inspect the board-specific selector" in note_text
    assert "ashby_autofill_pre_submit.png" in note_text
    assert "## Screenshot Evidence" in note_text
    assert "![pre_submit_screenshot](ashby_autofill_pre_submit.png)" in note_text

    assert index_path.exists()
    assert "# Active Audit Failures" in index_text
    assert "acme/principal-pm/submit/audit_failure.md" in index_text
    assert "](../acme/principal-pm/submit/audit_failure.md)" in index_text
