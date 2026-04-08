#!/usr/bin/env python3
"""Uber Careers application autofill.

Custom ATS at uber.com/careers -- requires account creation/sign-in.
Uses run_browser_pipeline().

URL patterns:
  - uber.com/global/en/careers/list/{job_id}/       (listing)
  - uber.com/careers/apply/form/{job_id}             (application form, needs auth)
  - uber.com/careers/apply/interstitial/{job_id}     (interstitial -> form)
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_submit_common import (
    APPLICATION_PROFILE_PATH,
    PROJECT_ROOT,
    find_cover_letter_file,
    find_resume_file,
    load_meta,
    normalize_text,
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

_BOARD = "uber"
_BOARD_CONSTANTS = board_file_constants(_BOARD)
AUTOFILL_PAYLOAD_JSON = _BOARD_CONSTANTS["payload_json"]
APPLICATION_PAGE_HTML = _BOARD_CONSTANTS["application_page_html"]

UBER_EMAIL_ENV = "UBER_EMAIL"
UBER_PASSWORD_ENV = "UBER_PASSWORD"

SUBMIT_BUTTON_NAMES = (
    "Submit application",
    "Submit Application",
    "Submit",
    "Apply",
    "Next",
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
    re.compile(r"\berror\b", re.I),
)

load_project_env()


# --- Auth handling ---


def _uber_credentials() -> tuple[str, str]:
    """Return (email, password) for Uber Careers login."""
    profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    email = os.environ.get(UBER_EMAIL_ENV) or getattr(profile, "verification_code_email", "") or ""
    password = os.environ.get(UBER_PASSWORD_ENV, "")
    if not password:
        raise RuntimeError(
            "Uber Careers password not configured. Set UBER_PASSWORD in .env.local or as an environment variable."
        )
    if not email:
        raise RuntimeError(
            "Uber Careers email not configured. Set UBER_EMAIL env var or "
            "Verification Code Email in application_profile.md."
        )
    return email, password


def _handle_auth(page, email: str, password: str) -> bool:
    """Handle Uber Careers authentication.

    Flow:
    1. Page shows "Sign in" / "Create account" options
    2. Try sign in first; if no account, create one
    3. After auth, should land on the application form
    """
    page.wait_for_timeout(2000)

    # Check if already on the application form (no auth needed)
    if _is_application_form(page):
        return True

    # Try "Create account" first (for new applicants)
    create_link = page.get_by_role("link", name="Create account")
    if create_link.count() and create_link.first.is_visible():
        create_link.first.click()
        page.wait_for_timeout(2000)

        # Fill create account dialog
        email_input = page.get_by_role("textbox", name=re.compile(r"email", re.I)).first
        if email_input.count():
            email_input.fill(email)

        password_input = page.get_by_role("textbox", name=re.compile(r"password", re.I)).first
        if password_input.count():
            password_input.fill(password)

        create_btn = page.get_by_role("button", name="Create account")
        if create_btn.count():
            create_btn.first.click()
            page.wait_for_timeout(5000)

        # Check if landed on form
        if _is_application_form(page):
            return True

        # If account already exists, dialog may show error — try sign in
        close_btn = page.get_by_role("button", name="Close")
        if close_btn.count():
            close_btn.first.click()
            page.wait_for_timeout(1000)

    # Try "Sign in"
    sign_in_btn = page.get_by_role("button", name="Sign in")
    if sign_in_btn.count() and sign_in_btn.first.is_visible():
        sign_in_btn.first.click()
        page.wait_for_timeout(2000)

        email_input = page.get_by_role("textbox", name=re.compile(r"email", re.I)).first
        if email_input.count():
            email_input.fill(email)

        password_input = page.get_by_role("textbox", name=re.compile(r"password", re.I)).first
        if password_input.count():
            password_input.fill(password)

        submit_btn = page.get_by_role("button", name=re.compile(r"sign in|log in|submit", re.I))
        if submit_btn.count():
            submit_btn.first.click()
            page.wait_for_timeout(5000)

    return _is_application_form(page)


def _is_application_form(page) -> bool:
    """Check if the current page is the application form (not the auth page)."""
    # Auth page has "Uber Careers account" heading
    auth_heading = page.locator("h4:has-text('Uber Careers account')")
    if auth_heading.count() > 0:
        return False

    # Form page typically has input fields, file uploads, etc.
    has_inputs = page.locator("input[type='text'], input[type='email'], textarea").count() > 2
    has_file = page.locator("input[type='file']").count() > 0
    return has_inputs or has_file


# --- Payload builder ---


def _build_payload(out_dir: Path, provider: str) -> dict:
    """Build the autofill payload for an Uber application."""
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)
    job_url = str(meta.get("jd_source_resolved") or meta.get("jd_source") or "")
    profile = parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    resume_path = find_resume_file(out_dir)
    cover_letter_path = find_cover_letter_file(out_dir)

    # Derive application form URL from listing URL
    application_url = job_url
    # Convert listing URL to form URL
    # uber.com/global/en/careers/list/{id}/ -> uber.com/careers/apply/form/{id}
    import re as _re

    m = _re.search(r"/careers/list/(\d+)", job_url)
    if m:
        application_url = f"https://www.uber.com/careers/apply/form/{m.group(1)}"

    steps: list[dict] = [
        {
            "field_name": "resume",
            "label": "Resume",
            "kind": "file",
            "required": True,
            "file_path": str(resume_path),
            "source": "existing_resume_asset",
        },
        {
            "field_name": "first_name",
            "label": "First name",
            "kind": "text",
            "required": True,
            "value": profile.first_name,
            "source": "master_resume.md",
        },
        {
            "field_name": "last_name",
            "label": "Last name",
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
            "label": "Phone",
            "kind": "text",
            "required": False,
            "value": profile.phone or "",
            "source": "master_resume.md",
        },
        {
            "field_name": "linkedin",
            "label": "LinkedIn",
            "kind": "text",
            "required": False,
            "value": profile.linkedin or "",
            "source": "master_resume.md",
        },
        {
            "field_name": "location",
            "label": "Location",
            "kind": "text",
            "required": False,
            "value": application_profile.location or "San Francisco, CA",
            "source": "application_profile.md",
        },
    ]

    if cover_letter_path and cover_letter_path.exists():
        steps.append(
            {
                "field_name": "cover_letter",
                "label": "Cover letter",
                "kind": "file",
                "required": False,
                "file_path": str(cover_letter_path),
                "source": "existing_cover_letter_asset",
            }
        )

    payload = {
        "job_url": job_url,
        "application_url": application_url,
        "out_dir": str(out_dir),
        "job_title": str(meta.get("jd_title") or ""),
        "company": str(meta.get("company_proper") or meta.get("company") or ""),
        "candidate_email": profile.email,
        "verification_code_email": application_profile.verification_code_email or profile.email,
        "mode": "review-before-submit",
        "notes": [
            "Uber Careers custom ATS. Requires account creation/sign-in.",
            "Form structure discovered at runtime after authentication.",
            "Unknown fields handled via LLM answering.",
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
        file_input = page.get_by_label(re.compile(re.escape(label), re.I)).first
        if file_input.count():
            file_input.set_input_files(file_path)
            return True
    except Exception:
        pass
    try:
        file_inputs = page.locator("input[type='file']")
        label_lower = label.casefold()
        if "resume" in label_lower or "cv" in label_lower:
            if file_inputs.count() > 0:
                file_inputs.first.set_input_files(file_path)
                return True
        if "cover" in label_lower and file_inputs.count() > 1:
            file_inputs.nth(1).set_input_files(file_path)
            return True
    except Exception:
        pass
    return False


def _fill_select_field(page, label: str, value: str) -> bool:
    """Fill a select/dropdown field."""
    if not value:
        return False
    from playwright.sync_api import Error as PlaywrightError

    try:
        combobox = page.get_by_role("combobox", name=re.compile(re.escape(label), re.I)).first
        if combobox.count():
            combobox.scroll_into_view_if_needed()
            combobox.click()
            page.wait_for_timeout(400)

            options = page.get_by_role("option")
            for i in range(options.count()):
                opt = options.nth(i)
                try:
                    text = opt.inner_text().strip()
                except PlaywrightError:
                    continue
                if value.casefold() in text.casefold() or text.casefold() in value.casefold():
                    opt.click()
                    page.wait_for_timeout(300)
                    return True
            combobox.press("Escape")
            return False
    except PlaywrightError:
        pass
    return False


def _fill_radio_field(page, label: str, value: str) -> bool:
    """Select a radio button."""
    try:
        radio = page.get_by_role("radio", name=re.compile(re.escape(value), re.I)).first
        if radio.count():
            radio.scroll_into_view_if_needed()
            radio.click()
            page.wait_for_timeout(200)
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

    if kind in ("select", "combobox"):
        if _fill_select_field(page, label, value):
            step["filled"] = True
        return

    if kind == "radio":
        if _fill_radio_field(page, label, value):
            step["filled"] = True
        return


def _classify_submit_state(snapshot: dict) -> dict[str, object]:
    """Classify page state after submit click."""
    page_text = str(snapshot.get("page_text") or "")

    if any(pattern.search(page_text) for pattern in SUBMIT_CONFIRM_PATTERNS):
        return {"status": "confirmed", "reason": "text"}

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


def _detect_uber_auth_result(page, payload: dict[str, object]) -> dict[str, object] | None:
    try:
        body_text = normalize_text(page.locator("body").inner_text(timeout=5000))
    except Exception:
        return None
    if not body_text:
        return None
    if all(fragment in body_text for fragment in ("uber careers account", "create account", "sign in")):
        job_url = str(payload.get("job_url") or page.url or "").strip()
        host = urlparse(job_url).netloc or "unknown"
        return {
            "status": "skipped_auth",
            "board": _BOARD,
            "provider": _BOARD,
            "website_confirmed": False,
            "updated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "job_url": job_url,
            "company": str(payload.get("company") or "").strip(),
            "job_title": str(payload.get("job_title") or "").strip(),
            "failure_type": "auth_guarded",
            "auth_state": "account_gate",
            "auth_scope": f"{_BOARD}:{host}",
            "message": "Uber requires sign in or account creation before the application form is available.",
        }
    return None


def _post_navigate(page, payload_path: Path) -> None:
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    result = _detect_uber_auth_result(page, payload)
    if result is None:
        return
    submit_dir = role_submit_path(Path(str(payload["out_dir"])), "")
    submit_dir.mkdir(parents=True, exist_ok=True)
    result_path = submit_dir / "application_submission_result.json"
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("Uber auth gate detected before the application form became available.", file=sys.stderr)


def _wait_for_uber_form(page) -> None:
    """Navigate from listing page to application form and handle auth."""
    page.wait_for_timeout(2000)

    # The pipeline navigates to the listing page (careers/list/{id}/).
    # We need to navigate to the application form URL.
    current_url = page.url
    m = re.search(r"/careers/list/(\d+)", current_url)
    if m:
        form_url = f"https://www.uber.com/careers/apply/form/{m.group(1)}"
        page.goto(form_url, wait_until="domcontentloaded", timeout=30000)

    page.wait_for_timeout(3000)

    # Skip auth if already on the application form (persistent browser profile)
    if _is_application_form(page):
        return

    try:
        email, password = _uber_credentials()
    except RuntimeError as exc:
        print(f"Uber: {exc}  — continuing without auth", file=sys.stderr)
        return

    auth_ok = _handle_auth(page, email, password)
    if not auth_ok:
        print("Uber: auth may have failed, continuing with form filling attempt", file=sys.stderr)

    page.wait_for_timeout(3000)


# --- Entry point ---


def _run_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    """Wrap run_browser_pipeline with Uber-specific callbacks."""
    from autofill_pipeline import run_browser_pipeline

    return run_browser_pipeline(
        payload_path,
        headless=headless,
        submit=submit,
        board_name=_BOARD,
        form_ready_fn=_wait_for_uber_form,
        post_navigate_hook=lambda page: _post_navigate(page, payload_path),
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
