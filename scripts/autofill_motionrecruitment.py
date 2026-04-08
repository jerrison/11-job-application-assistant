#!/usr/bin/env python3
"""Motion Recruitment application autofill.

Modal form on motionrecruitment.com -- no auth, reCAPTCHA.
Uses run_browser_pipeline().

URL patterns:
  - motionrecruitment.com/tech-jobs/{location}/{type}/{slug}/{id}
"""

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
    find_resume_file,
    load_meta,
    parse_application_profile,
    parse_master_resume,
)
from autofill_common import (
    board_file_constants,
    capture_full_page,
    click_submit_button,
    page_snapshot,
)
from output_layout import migrate_role_output_layout, role_submit_path
from project_env import load_project_env

_BOARD = "motionrecruitment"
_BOARD_CONSTANTS = board_file_constants(_BOARD)
AUTOFILL_PAYLOAD_JSON = _BOARD_CONSTANTS["payload_json"]
APPLICATION_PAGE_HTML = _BOARD_CONSTANTS["application_page_html"]

SUBMIT_BUTTON_NAMES = (
    "Submit",
    "Submit application",
    "Apply",
)

SUBMIT_CONFIRM_PATTERNS = (
    re.compile(r"\bthank(?:s| you)\s+for\s+(?:applying|your application)\b", re.I),
    re.compile(r"\bapplication (?:has been )?(?:received|submitted)\b", re.I),
    re.compile(r"\bwe(?:'|')ve received your application\b", re.I),
    re.compile(r"\bsuccessfully submitted\b", re.I),
    re.compile(r"\bsubmitted successfully\b", re.I),
)

VALIDATION_ERROR_PATTERNS = (
    re.compile(r"\bthis field is required\b", re.I),
    re.compile(r"\bplease (?:enter|select|complete|fill)\b", re.I),
    re.compile(r"\binvalid\b", re.I),
)

load_project_env()


# --- Payload builder ---


def _build_payload(out_dir: Path, provider: str) -> dict:
    """Build the autofill payload for a Motion Recruitment application."""
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)
    job_url = str(meta.get("jd_source_resolved") or meta.get("jd_source") or "")
    profile = parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    resume_path = find_resume_file(out_dir)

    steps: list[dict] = [
        {
            "field_name": "first_name",
            "label": "First Name",
            "kind": "text",
            "required": True,
            "value": profile.first_name,
            "source": "master_resume.md",
        },
        {
            "field_name": "last_name",
            "label": "Last Name",
            "kind": "text",
            "required": True,
            "value": profile.last_name,
            "source": "master_resume.md",
        },
        {
            "field_name": "email",
            "label": "Email",
            "kind": "text",
            "required": True,
            "value": profile.email,
            "source": "master_resume.md",
        },
        {
            "field_name": "phone",
            "label": "Phone Number",
            "kind": "text",
            "required": True,
            "value": profile.phone or "",
            "source": "master_resume.md",
        },
        {
            "field_name": "resume",
            "label": "Upload Your Resume",
            "kind": "file",
            "required": True,
            "file_path": str(resume_path),
            "source": "existing_resume_asset",
        },
    ]

    payload = {
        "job_url": job_url,
        "application_url": job_url,
        "out_dir": str(out_dir),
        "job_title": str(meta.get("jd_title") or ""),
        "company": str(meta.get("company_proper") or meta.get("company") or ""),
        "candidate_email": profile.email,
        "verification_code_email": application_profile.verification_code_email or profile.email,
        "mode": "review-before-submit",
        "notes": [
            "Motion Recruitment modal form. reCAPTCHA present.",
            "Click 'Apply' on listing page to open the modal.",
        ],
        "artifacts": {
            "payload_path": str(role_submit_path(out_dir, AUTOFILL_PAYLOAD_JSON)),
            "report_markdown": str(role_submit_path(out_dir, _BOARD_CONSTANTS["report_md"])),
            "report_json": str(role_submit_path(out_dir, _BOARD_CONSTANTS["report_json"])),
            "pre_submit_screenshot": str(role_submit_path(out_dir, _BOARD_CONSTANTS["pre_submit_screenshot"])),
            "page_screenshots_dir": str(role_submit_path(out_dir, _BOARD_CONSTANTS["page_screenshots_dir"])),
            "unknown_questions_json": str(role_submit_path(out_dir, _BOARD_CONSTANTS["unknown_questions_json"])),
            "submit_debug_html": str(role_submit_path(out_dir, _BOARD_CONSTANTS["submit_debug_html"])),
            "submit_debug_screenshot": str(role_submit_path(out_dir, _BOARD_CONSTANTS["submit_debug_screenshot"])),
            "application_page_html": str(role_submit_path(out_dir, APPLICATION_PAGE_HTML)),
        },
        "steps": steps,
        "unknown_questions": [],
    }
    return payload


# --- Browser pipeline callbacks ---


def _dismiss_cookie_banner(page) -> None:
    """Dismiss cookie consent banner if present."""
    try:
        for name in ("ALLOW ALL", "Allow All", "Accept all", "Accept", "Got it", "DENY"):
            btn = page.get_by_role("button", name=name)
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                page.wait_for_timeout(500)
                return
    except Exception:
        pass


def _open_apply_modal(page) -> None:
    """Click the 'Apply' button on the listing page to open the modal form."""
    try:
        apply_btn = page.get_by_role("button", name="Apply").first
        if apply_btn.count() and apply_btn.is_visible():
            apply_btn.click()
            page.wait_for_timeout(1500)
    except Exception:
        pass


def _fill_text_field(page, label: str, value: str) -> bool:
    """Fill a text input by label."""
    if not value:
        return False
    try:
        locator = page.get_by_role("textbox", name=re.compile(re.escape(label), re.I)).first
        if locator.count():
            locator.scroll_into_view_if_needed()
            locator.fill(str(value))
            return True
    except Exception:
        pass
    try:
        locator = page.get_by_label(re.compile(re.escape(label), re.I)).first
        if locator.count():
            locator.scroll_into_view_if_needed()
            locator.fill(str(value))
            return True
    except Exception:
        pass
    return False


def _fill_file_field(page, label: str, file_path: str) -> bool:
    """Upload a file."""
    if not file_path or not Path(file_path).exists():
        return False
    try:
        file_inputs = page.locator("input[type='file']")
        if file_inputs.count() > 0:
            file_inputs.first.set_input_files(file_path)
            return True
    except Exception:
        pass
    return False


def _fill_step(page, step: dict) -> None:
    """Fill a single form field."""
    kind = step.get("kind", "")
    label = step.get("label", "")
    value = step.get("value", "")

    if kind == "file":
        file_path = step.get("file_path", "")
        if _fill_file_field(page, label, file_path):
            step["filled"] = True
        return

    if kind in ("text", "textarea"):
        if _fill_text_field(page, label, value):
            step["filled"] = True
        return


def _classify_submit_state(snapshot: dict) -> dict[str, object]:
    """Classify page state after submit click."""
    page_text = str(snapshot.get("page_text") or "")

    if any(pattern.search(page_text) for pattern in SUBMIT_CONFIRM_PATTERNS):
        return {"status": "confirmed", "reason": "text"}

    # reCAPTCHA detection
    if snapshot.get("recaptcha_visible") or snapshot.get("hcaptcha_visible"):
        return {"status": "captcha_required", "reason": "recaptcha"}

    errors = list(snapshot.get("errors") or [])
    page_level_errors = [
        pattern.search(page_text).group(0) for pattern in VALIDATION_ERROR_PATTERNS if pattern.search(page_text)
    ]
    combined_errors = list(dict.fromkeys(errors + page_level_errors))
    if combined_errors:
        return {"status": "validation_error", "errors": combined_errors}

    return {"status": "pending"}


def _wait_for_motion_form(page) -> None:
    """Wait for page to load, dismiss cookies, open the Apply modal."""
    page.wait_for_selector(
        'button:has-text("Apply")',
        timeout=25000,
    )
    page.wait_for_timeout(2000)
    _dismiss_cookie_banner(page)
    _open_apply_modal(page)
    # Wait for modal to render
    page.wait_for_selector(
        'heading:has-text("Submit Your Application"), button:has-text("Submit")',
        timeout=15000,
    )
    page.wait_for_timeout(1000)


# --- Entry point ---


def _run_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    """Wrap run_browser_pipeline with Motion Recruitment-specific callbacks."""
    from autofill_pipeline import run_browser_pipeline

    return run_browser_pipeline(
        payload_path,
        headless=headless,
        submit=submit,
        board_name=_BOARD,
        form_ready_fn=_wait_for_motion_form,
        fill_step_fn=_fill_step,
        page_snapshot_fn=lambda page: page_snapshot(page, form_selector="form", captcha_type="recaptcha"),
        classify_state_fn=_classify_submit_state,
        click_submit_fn=lambda page: click_submit_button(page, button_names=SUBMIT_BUTTON_NAMES),
        capture_fn=lambda page, path: capture_full_page(page, path),
    )


def main() -> int:
    from autofill_pipeline import autofill_main

    return autofill_main(
        board_name=_BOARD,
        build_payload_fn=_build_payload,
        has_browser=True,
        run_browser_fn=_run_browser,
    )


if __name__ == "__main__":
    raise SystemExit(main())
