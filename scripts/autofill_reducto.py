#!/usr/bin/env python3
"""Reducto custom career page autofill.

Inline single-page form on reducto.ai/careers/{job_id} -- no auth.
Uses run_browser_pipeline().

URL patterns:
  - reducto.ai/careers/{uuid}  (job listing + inline application form)
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
)
from output_layout import migrate_role_output_layout, role_submit_path
from project_env import load_project_env

_BOARD = "reducto"
_BOARD_CONSTANTS = board_file_constants(_BOARD)
AUTOFILL_PAYLOAD_JSON = _BOARD_CONSTANTS["payload_json"]
APPLICATION_PAGE_HTML = _BOARD_CONSTANTS["application_page_html"]

SUBMIT_BUTTON_NAMES = (
    "Submit Application",
    "Submit application",
    "Submit",
    "Apply",
)

SUBMIT_CONFIRM_PATTERNS = (
    re.compile(r"\bthank(?:s| you)\s+for\s+(?:applying|your application)\b", re.I),
    re.compile(r"\bapplication (?:has been )?(?:received|submitted)\b", re.I),
    re.compile(r"\bwe(?:'|')ve received your application\b", re.I),
    re.compile(r"\bsuccessfully submitted\b", re.I),
    re.compile(r"\bsubmitted successfully\b", re.I),
    re.compile(r"\balready applied\b", re.I),
)

VALIDATION_ERROR_PATTERNS = (
    re.compile(r"\bthis field is required\b", re.I),
    re.compile(r"\bplease (?:enter|select|complete|fill)\b", re.I),
    re.compile(r"\binvalid\b", re.I),
)

load_project_env()


# --- Payload builder ---


def _us_phone_with_country_code(phone: str | None) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if not digits:
        return ""
    if len(digits) == 10:
        return f"1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return digits
    return digits


def _build_payload(out_dir: Path, provider: str) -> dict:
    """Build the autofill payload for a Reducto application."""
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)
    job_url = str(meta.get("jd_source_resolved") or meta.get("jd_source") or "")
    profile = parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    resume_path = find_resume_file(out_dir)

    steps: list[dict] = []

    # Personal info
    steps.extend(
        [
            {
                "field_name": "name",
                "label": "Name",
                "kind": "text",
                "required": True,
                "value": f"{profile.first_name} {profile.last_name}",
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
                "value": _us_phone_with_country_code(profile.phone),
                "source": "master_resume.md",
            },
            {
                "field_name": "pronouns",
                "label": "Pronouns",
                "kind": "text",
                "required": False,
                "value": application_profile.pronouns or "",
                "source": "application_profile.md",
            },
            {
                "field_name": "linkedin",
                "label": "LinkedIn",
                "kind": "text",
                "required": True,
                "value": application_profile.linkedin or profile.linkedin or "",
                "source": "application_profile.md" if application_profile.linkedin else "master_resume.md",
            },
        ]
    )

    # Radio questions
    steps.extend(
        [
            {
                "field_name": "open_to_onsite",
                "label": "Are you open to working 5 days a week onsite",
                "kind": "radio",
                "required": True,
                "value": "Yes",
                "source": "deterministic",
            },
            {
                "field_name": "college_degree",
                "label": "If you have a college degree",
                "kind": "text",
                "required": True,
                "value": "The Wharton School, University of Pennsylvania; Penn Engineering, University of Pennsylvania; Florida State University",
                "source": "master_resume.md",
            },
            {
                "field_name": "worked_at_startup",
                "label": "Have you worked at a startup",
                "kind": "radio",
                "required": True,
                "value": "Yes",
                "source": "deterministic",
            },
            {
                "field_name": "require_sponsorship",
                "label": "Will you now or in the future require",
                "kind": "radio",
                "required": True,
                "value": "No",
                "source": "deterministic",
            },
        ]
    )

    # Resume upload
    steps.append(
        {
            "field_name": "resume",
            "label": "Resume",
            "kind": "file",
            "required": True,
            "file_path": str(resume_path),
            "source": "existing_resume_asset",
        }
    )

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
            "Reducto inline career page form. No CAPTCHA observed.",
            "Form is at the bottom of the job listing page.",
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
        for name in ("Accept all", "Accept", "Accept All", "Got it"):
            btn = page.get_by_role("button", name=name)
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                page.wait_for_timeout(500)
                return
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
        # Reducto uses a custom upload zone; find the hidden file input
        file_inputs = page.locator("input[type='file']")
        if file_inputs.count() > 0:
            file_inputs.first.set_input_files(file_path)
            return True
    except Exception:
        pass
    try:
        file_input = page.get_by_label(re.compile(re.escape(label), re.I)).first
        if file_input.count():
            file_input.set_input_files(file_path)
            return True
    except Exception:
        pass
    return False


def _fill_radio_field(page, label: str, value: str) -> bool:
    """Select a radio button by group label and value."""
    label_re = re.compile(re.escape(label), re.I)
    value_re = re.compile(re.escape(value), re.I)

    def click_radio(scope) -> bool:
        try:
            radio = scope.get_by_role("radio", name=value_re).first
            if not radio.count():
                return False
            radio.scroll_into_view_if_needed()
            radio.click()
            page.wait_for_timeout(200)
            return True
        except Exception:
            return False

    try:
        containers = page.locator("fieldset, div, section, [role='group'], [role='radiogroup']")
        labeled_containers: list[tuple[int, object]] = []
        for i in range(containers.count()):
            container = containers.nth(i)
            try:
                container_text = container.inner_text()
            except Exception:
                continue
            if label_re.search(container_text):
                labeled_containers.append((len(container_text), container))

        for _, container in sorted(labeled_containers, key=lambda entry: entry[0]):
            if click_radio(container):
                return True
            try:
                nested_groups = container.locator("[role='group'], [role='radiogroup'], fieldset")
            except Exception:
                continue
            for index in range(nested_groups.count()):
                if click_radio(nested_groups.nth(index)):
                    return True
    except Exception:
        pass

    try:
        radio = page.get_by_role("radio", name=value_re).first
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

    if kind == "radio":
        if _fill_radio_field(page, label, value):
            step["filled"] = True
        return


def _page_snapshot_reducto(page) -> dict:
    """Capture a snapshot of the Reducto page state."""
    js = """() => {
        const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
        const pageText = normalize(document.body ? document.body.innerText : '');

        const invalidFields = Array.from(document.querySelectorAll('[aria-invalid="true"]'))
            .map((node) => {
                const entry = node.closest('[class*="field"], [class*="Field"]');
                const label = entry ? entry.querySelector('label') : null;
                return normalize(label ? label.innerText : node.getAttribute('name') || '');
            })
            .filter(Boolean);
        const explicitErrors = Array.from(document.querySelectorAll('[role="alert"], [class*="error"], [class*="Error"]'))
            .map((node) => normalize(node.innerText))
            .filter(Boolean);

        return {
            url: window.location.href,
            page_text: pageText,
            form_visible: !!document.querySelector('form, button[type="submit"], button:has(text)'),
            invalid_fields: invalidFields,
            errors: explicitErrors,
        };
    }"""
    return page.evaluate(js)


def _classify_submit_state(snapshot: dict) -> dict[str, object]:
    """Classify page state after submit click."""
    page_text = str(snapshot.get("page_text") or "")

    if any(pattern.search(page_text) for pattern in SUBMIT_CONFIRM_PATTERNS):
        return {"status": "confirmed", "reason": "text"}

    errors = list(snapshot.get("errors") or [])
    page_level_errors = [
        pattern.search(page_text).group(0) for pattern in VALIDATION_ERROR_PATTERNS if pattern.search(page_text)
    ]
    combined_errors = list(dict.fromkeys(errors + page_level_errors))
    if combined_errors:
        return {"status": "validation_error", "errors": combined_errors}

    return {"status": "pending"}


def _wait_for_reducto_form(page) -> None:
    """Wait for the Reducto application form to render."""
    page.wait_for_selector(
        'button:has-text("Submit Application"), button:has-text("Submit"), input[type="file"]',
        timeout=25000,
    )
    page.wait_for_timeout(2000)
    _dismiss_cookie_banner(page)
    # Scroll to the form at the bottom of the page
    try:
        submit_btn = page.get_by_role("button", name=re.compile(r"submit", re.I)).first
        if submit_btn.count():
            submit_btn.scroll_into_view_if_needed()
            page.wait_for_timeout(500)
    except Exception:
        pass


# --- Entry point ---


def _run_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    """Wrap run_browser_pipeline with Reducto-specific callbacks."""
    from autofill_pipeline import run_browser_pipeline

    return run_browser_pipeline(
        payload_path,
        headless=headless,
        submit=submit,
        board_name=_BOARD,
        form_ready_fn=_wait_for_reducto_form,
        fill_step_fn=_fill_step,
        page_snapshot_fn=lambda page: _page_snapshot_reducto(page),
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
