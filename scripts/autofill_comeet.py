#!/usr/bin/env python3
"""Comeet ATS application autofill.

Comeet forms are embedded in company career pages (e.g. Stampli).
Single-page form -- no auth, no wizard. Uses run_browser_pipeline().
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
    MASTER_RESUME_PATH,
    find_cover_letter_file,
    find_resume_file,
    load_meta,
    parse_application_profile,
    parse_master_resume,
    question_is_culture_careers_optin,
    resolve_shared_question_policy,
)
from autofill_common import (
    board_file_constants,
    capture_full_page,
    click_submit_button,
    page_snapshot,
    select_option,
    select_shared_policy_option,
)
from output_layout import migrate_role_output_layout, role_submit_path
from project_env import load_project_env

_BOARD = "comeet"
_BOARD_CONSTANTS = board_file_constants(_BOARD)
AUTOFILL_PAYLOAD_JSON = _BOARD_CONSTANTS["payload_json"]
APPLICATION_PAGE_HTML = _BOARD_CONSTANTS["application_page_html"]

SUBMIT_BUTTON_NAMES = (
    "Submit",
    "Submit Application",
    "Send Application",
    "Apply",
    "Send",
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
    re.compile(r"\berror\b", re.I),
)


load_project_env()


# --- Deterministic overrides ---


def _infer_deterministic(label: str, options: list[str]) -> str | None:
    """Return a deterministic answer or None to defer to LLM."""
    ll = label.casefold()
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    policy = resolve_shared_question_policy(label, application_profile)
    if policy is not None and policy.text_value is not None:
        return select_shared_policy_option(options, policy, application_profile=application_profile) or policy.text_value

    # Previous employee -> No
    if "previous employee" in ll or "former employee" in ll:
        return select_option(options, "No") or "No"

    # Work authorization -> Yes
    if "authorized to work" in ll or "legally authorized" in ll or "eligible to work" in ll or "legally eligible" in ll:
        return select_option(options, "Yes") or "Yes"

    # Sponsorship -> No
    if "sponsorship" in ll or "require sponsorship" in ll:
        return select_option(options, "No") or "No"

    # Culture/careers opt-in -> No
    if question_is_culture_careers_optin(label):
        return select_option(options, "No") or "No"

    return None


# --- Payload builder ---


def _build_payload(out_dir: Path, provider: str) -> dict:
    """Build the autofill payload for a Comeet application."""
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)
    job_url = str(meta.get("jd_source_resolved") or meta.get("jd_source") or "")
    profile = parse_master_resume(MASTER_RESUME_PATH.read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))

    resume_path = find_resume_file(out_dir)
    cover_letter_path = find_cover_letter_file(out_dir)

    # Build deterministic steps for standard fields
    steps: list[dict] = []

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

    # Cover letter upload (if file exists)
    if cover_letter_path and cover_letter_path.exists():
        steps.append(
            {
                "field_name": "cover_letter",
                "label": "Cover Letter",
                "kind": "file",
                "required": False,
                "file_path": str(cover_letter_path),
                "source": "existing_cover_letter_asset",
            }
        )

    # Personal info fields
    steps.extend(
        [
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
                "label": "Phone",
                "kind": "text",
                "required": False,
                "value": profile.phone or "",
                "source": "master_resume.md",
            },
        ]
    )

    # LinkedIn URL
    steps.append(
        {
            "field_name": "linkedin",
            "label": "LinkedIn",
            "kind": "text",
            "required": False,
            "value": application_profile.linkedin or profile.linkedin or "",
            "source": "application_profile.md",
        }
    )

    return {
        "job_url": job_url,
        "out_dir": str(out_dir),
        "job_title": str(meta.get("jd_title") or ""),
        "company": str(meta.get("company_proper") or meta.get("company") or ""),
        "candidate_email": profile.email,
        "verification_code_email": application_profile.verification_code_email or profile.email,
        "mode": "review-before-submit",
        "notes": [
            "Comeet embedded form. Browser selectors are provisional.",
            "Form may be inside an iframe -- fill_step switches into it if needed.",
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


# --- Browser pipeline callbacks ---


def _dismiss_cookie_banner(page) -> None:
    """Dismiss cookie consent banner if present."""
    for name in ("Accept", "Accept All", "Accept all cookies", "OK", "Got it"):
        try:
            btn = page.get_by_role("button", name=re.compile(rf"^{re.escape(name)}$", re.I)).first
            if btn.count() and btn.is_visible():
                btn.click()
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def _switch_to_comeet_iframe(page):
    """If the Comeet form is inside an iframe, return the frame; otherwise return page.

    Comeet widgets are often embedded via an iframe with src containing
    'comeet.com' or 'comeet.co', or with a class/id containing 'comeet'.
    """
    try:
        for frame in page.frames:
            url = frame.url or ""
            name = frame.name or ""
            if "comeet" in url.lower() or "comeet" in name.lower():
                return frame
    except Exception:
        pass
    return page


def _fill_comeet_combobox(page, label: str, value: str) -> bool:
    """Fill a Comeet combobox/select by label. Returns True if successful.

    Comeet forms may use standard <select> elements or custom dropdowns.
    Click to expand, then pick the best matching option.
    """
    from playwright.sync_api import Error as PlaywrightError

    try:
        # Try native <select> by associated label
        label_el = page.locator(f"label:has-text('{label}')").first
        if label_el.count():
            for_attr = label_el.get_attribute("for")
            if for_attr:
                sel = page.locator(f"select#{for_attr}")
                if sel.count():
                    sel.select_option(label=value)
                    return True
                # Try by name
                sel = page.locator(f"select[name='{for_attr}']")
                if sel.count():
                    sel.select_option(label=value)
                    return True

        # Fallback: ARIA combobox
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
            page.wait_for_timeout(200)
    except PlaywrightError:
        pass
    return False


def _fill_step(page, step: dict) -> None:
    """Fill a single form field on the Comeet application page."""
    kind = step.get("kind", "")
    label = step.get("label", "")
    value = step.get("value", "")

    # Switch into Comeet iframe if present
    ctx = _switch_to_comeet_iframe(page)

    if kind == "file":
        file_path = step.get("file_path", "")
        if not file_path:
            return
        try:
            file_inputs = ctx.locator("input[type='file']")
            count = file_inputs.count()
            if not count:
                return
            field_name = step.get("field_name", "")
            if field_name == "cover_letter" and count > 1:
                file_inputs.nth(1).set_input_files(file_path)
            else:
                file_inputs.first.set_input_files(file_path)
            step["filled"] = True
            ctx.wait_for_timeout(1000)
        except Exception:
            pass
        return

    if kind == "text":
        try:
            # Try by role first
            locator = ctx.get_by_role("textbox", name=re.compile(re.escape(label), re.I)).first
            if locator.count():
                locator.scroll_into_view_if_needed()
                locator.fill(str(value))
                step["filled"] = True
                return
            # Fallback: by label association
            locator = ctx.get_by_label(re.compile(re.escape(label), re.I)).first
            if locator.count():
                tag = (locator.evaluate("e => e.tagName") or "").lower()
                if tag in ("input", "textarea"):
                    locator.scroll_into_view_if_needed()
                    locator.fill(str(value))
                    step["filled"] = True
                    return
            # Fallback: by placeholder
            locator = ctx.locator(f"input[placeholder*='{label}' i]").first
            if locator.count():
                locator.scroll_into_view_if_needed()
                locator.fill(str(value))
                step["filled"] = True
                return
        except Exception:
            pass
        return

    if kind in ("combobox", "select"):
        if _fill_comeet_combobox(ctx, label, value):
            step["filled"] = True
        return

    if kind == "checkbox":
        try:
            locator = ctx.get_by_role("checkbox", name=re.compile(re.escape(label), re.I)).first
            if locator.count() and not locator.is_checked():
                locator.scroll_into_view_if_needed()
                locator.click()
                ctx.wait_for_timeout(200)
            step["filled"] = True
        except Exception:
            pass
        return

    if kind == "textarea":
        try:
            locator = ctx.get_by_role("textbox", name=re.compile(re.escape(label), re.I)).first
            if not locator.count():
                locator = ctx.get_by_label(re.compile(re.escape(label), re.I)).first
            if locator.count():
                locator.scroll_into_view_if_needed()
                locator.fill(str(value))
                step["filled"] = True
        except Exception:
            pass
        return


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


def _click_apply_if_needed(page) -> None:
    """Click the 'Apply' button on Comeet listing pages to reveal the form.

    Some Comeet career pages show the job description first and require
    clicking an Apply/Apply Now button to reveal the application form.
    """
    # Check if an application form is already visible (file inputs or
    # Comeet-specific form container).
    ctx = _switch_to_comeet_iframe(page)
    try:
        if ctx.locator("input[type='file']").count():
            return  # Form already visible
    except Exception:
        pass

    # Look for an Apply button on the main page
    for btn_text in ("Apply Now", "Apply for this job", "Apply"):
        try:
            btn = page.get_by_role("button", name=re.compile(rf"^{re.escape(btn_text)}$", re.I)).first
            if btn.count() and btn.is_visible():
                btn.scroll_into_view_if_needed()
                btn.click()
                page.wait_for_timeout(2000)
                return
            # Also check links styled as buttons
            link = page.get_by_role("link", name=re.compile(rf"^{re.escape(btn_text)}$", re.I)).first
            if link.count() and link.is_visible():
                link.scroll_into_view_if_needed()
                link.click()
                page.wait_for_timeout(2000)
                return
        except Exception:
            continue


def _wait_for_comeet_form(page) -> None:
    """Wait for the Comeet application form to render."""
    # Comeet forms may be in iframes or directly embedded
    _dismiss_cookie_banner(page)
    _click_apply_if_needed(page)
    try:
        page.wait_for_selector(
            ".comeet-apply-form:visible, .comeet-position-apply:visible, "
            "iframe[src*='comeet']:visible, iframe[name*='comeet']:visible, "
            "form:visible input:not([type='hidden']), "
            "form:visible textarea, "
            "form:visible select, "
            "button[type='submit']:visible, "
            'button:has-text("Submit"):visible, '
            'button:has-text("Apply"):visible, '
            'button:has-text("Send Application"):visible',
            timeout=25000,
        )
    except Exception:
        page.wait_for_selector(
            "input:not([type='hidden']):visible, textarea:visible, select:visible, button[type='submit']:visible",
            timeout=10000,
        )
    page.wait_for_timeout(2000)  # Let widgets hydrate
    _dismiss_cookie_banner(page)


# --- Entry point ---


def _run_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    """Wrap run_browser_pipeline with Comeet-specific callbacks."""
    from autofill_pipeline import run_browser_pipeline

    return run_browser_pipeline(
        payload_path,
        headless=headless,
        submit=submit,
        board_name=_BOARD,
        form_ready_fn=_wait_for_comeet_form,
        fill_step_fn=_fill_step,
        page_snapshot_fn=lambda page: page_snapshot(
            page, form_selector=".comeet-apply-form, form", captcha_type="recaptcha"
        ),
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
