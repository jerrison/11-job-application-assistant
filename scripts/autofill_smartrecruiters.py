#!/usr/bin/env python3
"""SmartRecruiters application autofill.

Single-page or multi-step form -- no auth required. Uses run_browser_pipeline().
SmartRecruiters URLs: jobs.smartrecruiters.com/{company}/{job_id}
Custom career sites (e.g. careers.intuitive.com) may redirect here.
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_submit_common import (
    APPLICATION_PROFILE_PATH,
    PROJECT_ROOT,
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
    label_matches,
    mark_visible_self_id_step,
    page_snapshot,
    select_option,
    select_profile_option,
    select_shared_policy_option,
)
from output_layout import migrate_role_output_layout, role_submit_path
from project_env import load_project_env

_BOARD = "smartrecruiters"
_BOARD_CONSTANTS = board_file_constants(_BOARD)
AUTOFILL_PAYLOAD_JSON = _BOARD_CONSTANTS["payload_json"]
APPLICATION_PAGE_HTML = _BOARD_CONSTANTS["application_page_html"]

SUBMIT_BUTTON_NAMES = (
    "Submit Application",
    "Submit application",
    "Submit",
    "Apply",
    "Apply Now",
    "Apply now",
)

SUBMIT_CONFIRM_PATTERNS = (
    re.compile(r"\bthank(?:s| you)\s+for\s+(?:applying|your application)\b", re.I),
    re.compile(r"\bapplication (?:has been )?(?:received|submitted)\b", re.I),
    re.compile(r"\bwe(?:'|')ve received your application\b", re.I),
    re.compile(r"\bsuccessfully submitted\b", re.I),
    re.compile(r"\bsubmitted successfully\b", re.I),
    re.compile(r"\balready applied\b", re.I),
    re.compile(r"\bapplication complete\b", re.I),
)

VALIDATION_ERROR_PATTERNS = (
    re.compile(r"\bthis field is required\b", re.I),
    re.compile(r"\bplease (?:enter|select|complete|fill)\b", re.I),
    re.compile(r"\binvalid\b", re.I),
    re.compile(r"\berror\b", re.I),
)

JOB_CLOSED_PATTERNS = (
    re.compile(r"\bsorry,\s*this job has expired\b", re.I),
    re.compile(r"\bthis job has expired\b", re.I),
    re.compile(r"\bjob is no longer available\b", re.I),
    re.compile(r"\bno longer accepting applications\b", re.I),
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
    if (
        "authorized to work" in ll
        or "legally authorized" in ll
        or "work authorization" in ll
        or "eligible to work" in ll
        or "legally eligible" in ll
    ):
        return select_option(options, "Yes") or "Yes"

    # Sponsorship -> No
    if "sponsorship" in ll or "require visa" in ll:
        return select_option(options, "No") or "No"

    # GDPR / privacy consent -> check
    if "privacy" in ll and ("consent" in ll or "agree" in ll or "acknowledge" in ll):
        return "checked"
    if "gdpr" in ll:
        return "checked"

    # Terms & conditions / data processing consent
    if ("terms" in ll or "data processing" in ll) and ("agree" in ll or "consent" in ll or "accept" in ll):
        return "checked"

    # Culture/careers opt-in -> No
    if question_is_culture_careers_optin(label):
        return select_option(options, "No") or "No"

    return None


# --- Payload builder ---


def _build_payload(out_dir: Path, provider: str) -> dict:
    """Build the autofill payload for a SmartRecruiters application."""
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)
    job_url = str(meta.get("jd_source_resolved") or meta.get("jd_source") or "")
    profile = parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
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
            {
                "field_name": "location",
                "label": "Location",
                "kind": "text",
                "required": False,
                "value": application_profile.location or profile.location or "",
                "source": "application_profile.md",
            },
            {
                "field_name": "current_location",
                "label": "Current location",
                "kind": "text",
                "required": False,
                "value": application_profile.location or profile.location or "",
                "source": "application_profile.md",
            },
            {
                "field_name": "city",
                "label": "City",
                "kind": "text",
                "required": False,
                "value": application_profile.location or profile.location or "",
                "source": "application_profile.md",
            },
        ]
    )

    # LinkedIn and website
    steps.extend(
        [
            {
                "field_name": "linkedin",
                "label": "LinkedIn",
                "kind": "text",
                "required": False,
                "value": application_profile.linkedin or profile.linkedin or "",
                "source": "application_profile.md",
            },
            {
                "field_name": "website",
                "label": "Website",
                "kind": "text",
                "required": False,
                "value": application_profile.website or profile.website or "",
                "source": "application_profile.md",
            },
        ]
    )

    # Common application questions
    steps.extend(
        [
            {
                "field_name": "work_authorization",
                "label": "legally authorized to work",
                "kind": "select",
                "required": True,
                "value": "Yes",
                "source": "application_profile.md",
            },
            {
                "field_name": "sponsorship",
                "label": "require sponsorship",
                "kind": "select",
                "required": True,
                "value": "No",
                "source": "application_profile.md",
            },
        ]
    )

    # Consent checkboxes
    steps.append(
        {
            "field_name": "privacy_consent",
            "label": "privacy",
            "kind": "checkbox",
            "required": True,
            "value": "checked",
            "source": "deterministic_override",
        }
    )

    # Demographics (optional, may not be present)
    steps.extend(
        [
            mark_visible_self_id_step(
                {
                    "field_name": "gender",
                    "label": "Gender",
                    "kind": "select",
                    "required": False,
                    "value": getattr(application_profile, "gender_identity", None) or application_profile.gender or "Male",
                    "source": "application_profile.md",
                },
                profile_field="gender_identity" if getattr(application_profile, "gender_identity", None) else "gender",
            ),
            mark_visible_self_id_step(
                {
                    "field_name": "veteran_status",
                    "label": "Veteran",
                    "kind": "select",
                    "required": False,
                    "value": application_profile.veteran_status or "I am not a protected veteran",
                    "source": "application_profile.md",
                },
                profile_field="veteran_status",
            ),
            mark_visible_self_id_step(
                {
                    "field_name": "race_ethnicity",
                    "label": "Race",
                    "kind": "select",
                    "required": False,
                    "value": application_profile.race_or_ethnicity or "Hispanic or Latino",
                    "source": "application_profile.md",
                },
                profile_field="race_or_ethnicity",
            ),
            mark_visible_self_id_step(
                {
                    "field_name": "disability",
                    "label": "Disability",
                    "kind": "select",
                    "required": False,
                    "value": application_profile.disability_status or "No, I do not have a disability",
                    "source": "application_profile.md",
                },
                profile_field="disability_status",
            ),
        ]
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
            "SmartRecruiters form. May be single-page or multi-step.",
            "Deterministic overrides handle work authorization, sponsorship, privacy consent.",
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
    for label in ("Accept", "Accept All", "Accept all", "Accept Cookies", "I agree", "OK"):
        try:
            btn = page.get_by_role("button", name=label)
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def _navigate_to_apply_form(page, payload: dict) -> None:
    """Navigate from a SmartRecruiters listing page to the one-click apply flow."""
    current_url = str(page.url or "")
    if "/oneclick-ui/" in current_url:
        payload["application_url"] = current_url
        return

    apply_pattern = re.compile(r"i'?m interested|apply(?: now)?", re.I)

    try:
        apply_link = page.get_by_role("link", name=apply_pattern).first
        if apply_link.count() and apply_link.is_visible():
            href = apply_link.get_attribute("href") or ""
            if href:
                page.goto(href, wait_until="domcontentloaded", timeout=30000)
            else:
                apply_link.click()
            page.wait_for_timeout(1500)
            payload["application_url"] = str(page.url or "")
            return
    except Exception:
        pass

    try:
        apply_button = page.get_by_role("button", name=apply_pattern).first
        if apply_button.count() and apply_button.is_visible():
            apply_button.click()
            page.wait_for_timeout(1500)
            payload["application_url"] = str(page.url or "")
    except Exception:
        pass


def _smartrecruiters_profile_field_for_label(label: str) -> str | None:
    if label_matches(label, "transgender"):
        return "transgender_status"
    if label_matches(label, "gender") and not label_matches(label, "transgender"):
        return "gender_identity"
    if label_matches(label, "race", "ethnicity", "hispanic", "latino"):
        return "race_or_ethnicity"
    if label_matches(label, "veteran", "military"):
        return "veteran_status"
    if label_matches(label, "disability"):
        return "disability_status"
    if label_matches(label, "sexual orientation", "orientation"):
        return "sexual_orientation"
    if label_matches(label, "pronoun", "pronouns"):
        return "pronouns"
    return None


def _resolve_smartrecruiters_select_label(
    label: str,
    value: str,
    option_labels: list[str],
    *,
    profile_field: str | None = None,
) -> str | None:
    resolved_profile_field = profile_field or _smartrecruiters_profile_field_for_label(label)
    if resolved_profile_field:
        matched = select_profile_option(option_labels, value, profile_field=resolved_profile_field)
        if matched is not None:
            return matched
    return select_option(option_labels, value)


def _fill_smartrecruiters_select(page, label: str, value: str, *, profile_field: str | None = None) -> bool:
    """Fill a SmartRecruiters select/dropdown by label. Returns True if successful.

    SmartRecruiters uses standard <select> elements and sometimes custom
    dropdowns. Try native select first, then fall back to combobox/listbox.
    """
    from playwright.sync_api import Error as PlaywrightError

    # Try native <select> via label association
    try:
        label_lower = label.casefold()
        select = (
            page.locator("select")
            .filter(
                has=page.locator(
                    f"xpath=preceding::label[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{label_lower}')]"
                )
            )
            .first
        )
        if select.count():
            select.scroll_into_view_if_needed()
            option_locator = select.locator("option")
            option_labels = [option_locator.nth(i).inner_text().strip() for i in range(option_locator.count())]
            matched = _resolve_smartrecruiters_select_label(
                label,
                value,
                option_labels,
                profile_field=profile_field,
            )
            select.select_option(label=matched or value)
            page.wait_for_timeout(300)
            return True
    except PlaywrightError:
        pass

    # Try get_by_label for <select> elements
    try:
        select = page.get_by_label(re.compile(re.escape(label), re.I)).first
        if select.count():
            tag = select.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                select.scroll_into_view_if_needed()
                option_locator = select.locator("option")
                option_labels = [option_locator.nth(i).inner_text().strip() for i in range(option_locator.count())]
                matched = _resolve_smartrecruiters_select_label(
                    label,
                    value,
                    option_labels,
                    profile_field=profile_field,
                )
                select.select_option(label=matched or value)
                page.wait_for_timeout(300)
                return True
    except PlaywrightError:
        pass

    # Try combobox role (custom dropdown)
    try:
        combobox = page.get_by_role("combobox", name=re.compile(re.escape(label), re.I)).first
        if combobox.count():
            combobox.scroll_into_view_if_needed()
            combobox.click()
            page.wait_for_timeout(400)

            # Find and click the matching option
            options = page.get_by_role("option")
            option_labels: list[str] = []
            options_by_text: dict[str, object] = {}
            for i in range(options.count()):
                opt = options.nth(i)
                try:
                    text = opt.inner_text().strip()
                except PlaywrightError:
                    continue
                if not text:
                    continue
                option_labels.append(text)
                options_by_text.setdefault(text, opt)
            matched = _resolve_smartrecruiters_select_label(
                label,
                value,
                option_labels,
                profile_field=profile_field,
            )
            if matched is not None:
                matched_option = options_by_text.get(matched)
                if matched_option is not None:
                    matched_option.click()
                    page.wait_for_timeout(300)
                    return True

            # Close dropdown if no match found
            combobox.press("Escape")
            page.wait_for_timeout(200)
    except PlaywrightError:
        pass

    return False


def _fill_step(page, step: dict) -> None:
    """Fill a single form field on the SmartRecruiters application page."""
    kind = step.get("kind", "")
    label = step.get("label", "")
    value = step.get("value", "")

    if kind == "file":
        file_path = step.get("file_path", "")
        if not file_path:
            return
        # SmartRecruiters may use a visible file input or a hidden one behind a drop zone
        try:
            locator = page.locator("input[type='file']")
            if locator.count():
                # Use the first file input for resume, second for cover letter
                field_name = step.get("field_name", "")
                if field_name == "cover_letter" and locator.count() > 1:
                    locator.nth(1).set_input_files(file_path)
                else:
                    locator.first.set_input_files(file_path)
                step["filled"] = True
                page.wait_for_timeout(1000)  # Wait for file processing
        except Exception:
            pass
        return

    if kind == "text":
        try:
            # Try by role with label
            locator = page.get_by_role("textbox", name=re.compile(re.escape(label), re.I)).first
            if locator.count():
                locator.scroll_into_view_if_needed()
                locator.fill(str(value))
                step["filled"] = True
                return
        except Exception:
            pass
        # Fallback: try get_by_label
        try:
            locator = page.get_by_label(re.compile(re.escape(label), re.I)).first
            if locator.count():
                tag = locator.evaluate("el => el.tagName.toLowerCase()")
                if tag in ("input", "textarea"):
                    locator.scroll_into_view_if_needed()
                    locator.fill(str(value))
                    step["filled"] = True
                    return
        except Exception:
            pass
        return

    if kind == "select":
        if _fill_smartrecruiters_select(page, label, value, profile_field=str(step.get("profile_field") or "").strip() or None):
            step["filled"] = True
        return

    if kind == "checkbox":
        try:
            locator = page.get_by_role("checkbox", name=re.compile(re.escape(label), re.I)).first
            if locator.count() and not locator.is_checked():
                locator.scroll_into_view_if_needed()
                locator.click()
                page.wait_for_timeout(200)
            step["filled"] = True
        except Exception:
            pass
        # Fallback: try label-based checkbox (SmartRecruiters consent checkboxes)
        if not step.get("filled"):
            try:
                locator = page.get_by_label(re.compile(re.escape(label), re.I)).first
                if locator.count():
                    tag = locator.evaluate("el => el.type || el.tagName.toLowerCase()")
                    if tag == "checkbox" and not locator.is_checked():
                        locator.scroll_into_view_if_needed()
                        locator.click()
                        page.wait_for_timeout(200)
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


def _smartrecruiters_page_text(page) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=2000) or "")
    except Exception:
        return ""


def _smartrecruiters_job_closed_reason(page) -> str | None:
    page_text = _smartrecruiters_page_text(page)
    if any(pattern.search(page_text) for pattern in JOB_CLOSED_PATTERNS):
        return f"SmartRecruiters showed an expired or unavailable job page at {page.url}"
    return None


def _detect_smartrecruiters_job_closed_result(page, payload: dict[str, object]) -> dict[str, object] | None:
    reason = _smartrecruiters_job_closed_reason(page)
    if reason is None:
        return None
    return {
        "status": "job_closed",
        "board": _BOARD,
        "provider": _BOARD,
        "website_confirmed": True,
        "updated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "job_url": str(payload.get("job_url") or page.url or "").strip(),
        "company": str(payload.get("company") or "").strip(),
        "job_title": str(payload.get("job_title") or "").strip(),
        "failure_type": "job_closed",
        "message": f"job_closed: {reason}",
    }


def _wait_for_smartrecruiters_form(page) -> None:
    """Wait for the SmartRecruiters application form to render."""
    # SmartRecruiters forms appear under various selectors
    try:
        page.wait_for_selector(
            'button:has-text("Submit"), button:has-text("Apply"), '
            "form, .application-form, #application-form, "
            'input[type="file"], [data-test="resume-upload"]',
            timeout=25000,
        )
    except Exception as exc:
        reason = _smartrecruiters_job_closed_reason(page)
        if reason is not None:
            raise RuntimeError(f"job_closed: {reason}") from exc
        raise
    page.wait_for_timeout(2000)  # Let React/JS hydrate
    _dismiss_cookie_banner(page)


# --- Entry point ---


def _run_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    """Wrap run_browser_pipeline with SmartRecruiters-specific callbacks."""
    import json

    from autofill_pipeline import run_browser_pipeline

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    navigate_url = payload.get("application_url") or payload.get("job_url", "")

    def _post_navigate(page):
        _navigate_to_apply_form(page, payload)
        if payload.get("application_url") and payload["application_url"] != navigate_url:
            payload_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        result = _detect_smartrecruiters_job_closed_result(page, payload)
        if result is not None:
            submit_dir = role_submit_path(Path(str(payload["out_dir"])), "")
            submit_dir.mkdir(parents=True, exist_ok=True)
            result_path = submit_dir / "application_submission_result.json"
            result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print(str(result.get("message") or "SmartRecruiters pre-form terminal state detected."), file=sys.stderr)
            return
        _wait_for_smartrecruiters_form(page)

    return run_browser_pipeline(
        payload_path,
        headless=headless,
        submit=submit,
        board_name=_BOARD,
        fill_step_fn=_fill_step,
        page_snapshot_fn=lambda page: page_snapshot(page, form_selector="form", captcha_type="recaptcha"),
        classify_state_fn=_classify_submit_state,
        click_submit_fn=lambda page: click_submit_button(page, button_names=SUBMIT_BUTTON_NAMES),
        capture_fn=lambda page, path: capture_full_page(page, path),
        post_navigate_hook=_post_navigate,
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
