#!/usr/bin/env python3
"""Recruitee application autofill."""

from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_submit_common import (
    APPLICATION_PROFILE_PATH,
    PROJECT_ROOT,
    build_simple_payload,
    find_cover_letter_file,
    find_resume_file,
    load_meta,
    parse_application_profile,
    parse_master_resume,
    resolve_shared_question_policy,
)
from autofill_common import (
    classify_submit_state,
    fill_basic_step,
    select_shared_policy_option,
)
from autofill_pipeline import autofill_main, run_simple_board_pipeline
from output_layout import migrate_role_output_layout
from project_env import load_project_env

_BOARD = "recruitee"
_FORM_SELECTOR = "form, input[type='file'], button[type='submit']"
SUBMIT_BUTTON_NAMES = ("Submit application", "Submit Application", "Apply", "Apply now")
_CONFIRM_PATTERNS = (
    re.compile(r"\bthank(?:s| you)\s+for\s+applying\b", re.I),
    re.compile(r"\bapplication submitted\b", re.I),
)
_REVIEW_PATTERNS = (re.compile(r"\bsubmit application\b", re.I),)

load_project_env()


def _infer_deterministic(label: str, options: list[str]) -> str | None:
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    policy = resolve_shared_question_policy(label, application_profile)
    if policy is not None and policy.text_value is not None:
        return select_shared_policy_option(options, policy, application_profile=application_profile) or policy.text_value
    return None


def _build_payload(out_dir: Path, provider: str) -> dict:
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)
    profile = parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))

    try:
        resume_path = find_resume_file(out_dir)
    except FileNotFoundError:
        resume_path = None
    try:
        cover_letter_path = find_cover_letter_file(out_dir)
    except FileNotFoundError:
        cover_letter_path = None

    extra_steps = [
        {
            "field_name": "privacy_consent",
            "label": "Privacy",
            "kind": "checkbox",
            "required": False,
            "value": "checked",
            "source": "deterministic_override",
        }
    ]
    return build_simple_payload(
        board_name=_BOARD,
        out_dir=out_dir,
        provider=provider,
        meta=meta,
        profile=profile,
        application_profile=application_profile,
        resume_path=resume_path,
        cover_letter_path=cover_letter_path,
        notes=["Recruitee form. Stop at review in draft mode."],
        extra_steps=extra_steps,
    )


def _classify_submit_state(snapshot: dict) -> dict[str, object]:
    return classify_submit_state(
        snapshot,
        confirm_patterns=_CONFIRM_PATTERNS,
        review_patterns=_REVIEW_PATTERNS,
    )


def _run_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    return run_simple_board_pipeline(
        payload_path,
        headless=headless,
        submit=submit,
        board_name=_BOARD,
        form_selector=_FORM_SELECTOR,
        submit_button_names=SUBMIT_BUTTON_NAMES,
        classify_state_fn=_classify_submit_state,
        fill_step_fn=fill_basic_step,
        preferred_capture_selectors=("form", "main"),
    )


def main() -> int:
    return autofill_main(_BOARD, _build_payload, run_browser_fn=_run_browser)


if __name__ == "__main__":
    raise SystemExit(main())
