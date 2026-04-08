import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from queue_review_summary import derive_queue_review_summary, visible_action_ids


def _job(status: str, **overrides):
    job = {
        "id": 7,
        "status": status,
        "board": "greenhouse",
        "output_dir": "/tmp/job-output",
        "archived": False,
        "previously_submitted": False,
        "submission_lock_state": None,
        "llm_generated_answers": 0,
    }
    job.update(overrides)
    return job


def test_ready_draft_is_high_confidence():
    summary = derive_queue_review_summary(
        _job("draft"),
        draft_review={"state": "ready", "reason": "ok"},
        answer_verification={"status": "verified"},
        pending_user_input=None,
    )

    assert summary["overall_confidence"] == "high"
    assert summary["confidence_label"] == "Ready to submit"
    assert [chip["label"] for chip in summary["reason_chips"]] == [
        "Proof current",
        "Answers verified",
        "No blockers",
    ]
    assert summary["visible_actions"] == [
        "approve_submit",
        "reset_to_new",
        "restart_draft",
        "restart_submit",
        "stop",
        "archive",
        "delete",
    ]


def test_stale_draft_with_verified_answers_is_medium_confidence():
    summary = derive_queue_review_summary(
        _job("draft", llm_generated_answers=3),
        draft_review={"state": "stale", "reason": "Historical proof exists"},
        answer_verification={"status": "verified"},
        pending_user_input=None,
    )

    assert summary["overall_confidence"] == "medium"
    assert summary["confidence_label"] == "Usable, but review recommended"
    assert [chip["label"] for chip in summary["reason_chips"]] == [
        "Proof stale",
        "Answers verified",
        "3 AI answers",
    ]


def test_pending_user_input_forces_low_confidence():
    summary = derive_queue_review_summary(
        _job("draft"),
        draft_review={"state": "blocked", "reason": "Missing proof"},
        answer_verification={"status": "blocked"},
        pending_user_input={"questions": [{"label": "Portfolio URL"}]},
    )

    assert summary["overall_confidence"] == "low"
    assert summary["confidence_label"] == "Needs review before submit"
    assert [chip["label"] for chip in summary["reason_chips"]] == [
        "Proof blocked",
        "Verification blocked",
        "Pending input",
    ]


def test_generating_job_reports_pending_confidence():
    summary = derive_queue_review_summary(
        _job("reanswering"),
        draft_review=None,
        answer_verification={"status": "pending"},
        pending_user_input=None,
    )

    assert summary["overall_confidence"] == "pending"
    assert summary["confidence_label"] == "Draft in progress"
    assert summary["visible_actions"] == ["stop"]


def test_locked_resubmission_exposes_unlock_before_rerun_actions():
    summary = derive_queue_review_summary(
        _job(
            "submitted",
            previously_submitted=True,
            submission_lock_state="locked",
        ),
        draft_review=None,
        answer_verification=None,
        pending_user_input=None,
    )

    assert summary["visible_actions"] == ["unlock_resubmit", "archive", "delete"]


def test_queued_submit_uses_queued_semantics_not_processing_semantics():
    summary = derive_queue_review_summary(
        _job("queued_submit", output_dir=None),
        draft_review=None,
        answer_verification=None,
        pending_user_input=None,
    )

    assert summary["overall_confidence"] == "medium"
    assert summary["confidence_label"] == "Usable, but review recommended"
    assert summary["visible_actions"] == [
        "restart_draft",
        "restart_submit",
        "archive",
        "delete",
    ]


@pytest.mark.parametrize(
    ("job", "expected_actions"),
    [
        (_job("queued"), ["stop", "delete"]),
        (_job("approved"), ["stop"]),
        (
            _job("needs_board_url"),
            ["approve_submit", "reset_to_new", "restart_draft", "restart_submit", "archive", "delete"],
        ),
        (
            _job("awaiting_captcha"),
            ["approve_submit", "reset_to_new", "focus_browser", "stop", "delete"],
        ),
        (
            _job("draft", archived=True, submission_lock_state="unlocked_for_resubmit"),
            ["lock_resubmission", "unarchive", "delete"],
        ),
        (
            _job("draft", archived=True, submission_lock_state="locked"),
            ["unlock_resubmit", "unarchive", "delete"],
        ),
    ],
)
def test_visible_action_ids_match_current_queue_action_semantics(job, expected_actions):
    assert visible_action_ids(job) == expected_actions


@pytest.mark.parametrize(
    ("status", "expected_actions"),
    [
        ("queued", ["stop", "delete"]),
        ("approved", ["stop"]),
    ],
)
def test_pending_confidence_can_share_non_processing_action_sets(status, expected_actions):
    summary = derive_queue_review_summary(
        _job(status, output_dir=None),
        draft_review=None,
        answer_verification=None,
        pending_user_input=None,
    )

    assert summary["overall_confidence"] == "pending"
    assert summary["confidence_label"] == "Draft in progress"
    assert summary["visible_actions"] == expected_actions
