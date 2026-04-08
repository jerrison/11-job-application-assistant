#!/usr/bin/env python3
"""Eightfold AI application autofill.

Single-page form -- no auth, no wizard. Uses run_browser_pipeline().
Ref: docs/superpowers/specs/2026-03-16-eightfold-successfactors-design.md
"""

from __future__ import annotations

import json
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
    MASTER_RESUME_PATH,
    find_cover_letter_file,
    find_resume_file,
    load_meta,
    normalize_text,
    parse_application_profile,
    parse_master_resume,
    question_is_culture_careers_optin,
    resolve_shared_question_policy,
)
from autofill_common import (
    board_file_constants,
    capture_full_page,
    click_submit_button,
    is_visible_self_id_blocker,
    label_matches,
    mark_visible_self_id_step,
    page_snapshot,
    select_option,
    select_profile_option,
    select_shared_policy_option,
)
from output_layout import migrate_role_output_layout, role_submit_path
from project_env import load_project_env

_BOARD = "eightfold"
_BOARD_CONSTANTS = board_file_constants(_BOARD)
AUTOFILL_PAYLOAD_JSON = _BOARD_CONSTANTS["payload_json"]
APPLICATION_PAGE_HTML = _BOARD_CONSTANTS["application_page_html"]

SUBMIT_BUTTON_NAMES = (
    "Submit application",
    "Submit Application",
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
    re.compile(r"\berror\b", re.I),
)
_JOB_CLOSED_MARKERS = (
    "no longer accepting applications",
    "job is no longer open",
    "this job is closed",
    "job posting is no longer available",
    "this requisition is no longer available",
    "we didn't find any relevant jobs",
)


load_project_env()

_EIGHTFOLD_FIELD_SEARCH_ALIASES: dict[str, tuple[str, ...]] = {
    "how_did_you_hear": ("how did you hear", "how did you learn", "heard about", "source"),
    "previous_employee": ("previous employee", "former employee"),
    "work_authorization": ("authorized to work", "authorization to work", "legally work"),
    "sponsorship": ("sponsor", "sponsorship"),
    "pep_related": ("politically exposed person",),
    "pep_self": ("politically exposed person",),
    "employee_relationship": ("close relationship", "related to"),
    "acknowledgment_date": ("today's date", "today s date", "acknowledg", "declaration"),
    "privacy_consent": ("privacy statement", "candidate privacy"),
    "nda_consent": ("nondisclosure", "non disclosure"),
    "disability_language": ("language",),
    "veteran_status": ("veteran", "military"),
}

_EIGHTFOLD_PROFILE_FIELD_SEARCH_ALIASES: dict[str, tuple[str, ...]] = {
    "gender": ("gender",),
    "gender_identity": ("gender",),
    "race_or_ethnicity": ("race", "ethnicity"),
    "veteran_status": ("veteran", "military"),
}


# --- Deterministic overrides ---


def _infer_deterministic(label: str, options: list[str]) -> str | None:
    """Return a deterministic answer or None to defer to LLM."""
    ll = label.casefold()

    # Previous employee -> No
    if "previous employee" in ll or "former employee" in ll:
        return select_option(options, "No") or "No"

    # PEP questions -> No
    if "politically exposed person" in ll or "pep" in ll.split():
        return select_option(options, "No") or "No"

    # Relationship to company employee -> No
    if "related to" in ll and "employee" in ll and "working in" in ll:
        return select_option(options, "No") or "No"

    # NDA / privacy consent -> check (return truthy string)
    if "acknowledge and agree" in ll and "nondisclosure" in ll:
        return "checked"
    if "read and consent" in ll and "privacy" in ll:
        return "checked"

    # Acknowledgment date -> today
    if "acknowledgement" in ll or ("declaration" in ll and "date" in ll):
        from datetime import date

        return date.today().isoformat()

    # Culture/careers opt-in -> No
    if question_is_culture_careers_optin(label):
        return select_option(options, "No") or "No"

    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    policy = resolve_shared_question_policy(label, application_profile)
    if policy is not None and policy.text_value is not None:
        return (
            select_shared_policy_option(options, policy, application_profile=application_profile) or policy.text_value
        )

    return None


# --- Payload builder ---


def _build_payload(out_dir: Path, provider: str) -> dict:
    """Build the autofill payload for an Eightfold application."""
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

    # Application questions — deterministic answers
    steps.extend(
        [
            {
                "field_name": "how_did_you_hear",
                "label": "How did you hear about us?",
                "kind": "combobox",
                "required": False,
                "value": "Corporate website",
                "source": "application_profile.md",
            },
            {
                "field_name": "previous_employee",
                "label": "Are you a previous Employee",
                "kind": "combobox",
                "required": True,
                "value": "No",
                "source": "deterministic_override",
            },
            {
                "field_name": "country",
                "label": "Country",
                "kind": "combobox",
                "required": True,
                "value": "United States of America",
                "source": "application_profile.md",
            },
            {
                "field_name": "work_authorization",
                "label": "legally authorized to work",
                "kind": "combobox",
                "required": True,
                "value": "Yes",
                "source": "application_profile.md",
            },
            {
                "field_name": "sponsorship",
                "label": "sponsorship",
                "kind": "combobox",
                "required": True,
                "value": "No",
                "source": "application_profile.md",
            },
            {
                "field_name": "pep_related",
                "label": "related to or associated with a Politically Exposed Person",
                "kind": "combobox",
                "required": True,
                "value": "No",
                "source": "deterministic_override",
            },
            {
                "field_name": "pep_self",
                "label": "current or former Politically Exposed Person",
                "kind": "combobox",
                "required": True,
                "value": "No",
                "source": "deterministic_override",
            },
            {
                "field_name": "employee_relationship",
                "label": "related to or have a close relationship",
                "kind": "combobox",
                "required": True,
                "value": "No",
                "source": "deterministic_override",
            },
            {
                "field_name": "acknowledgment_date",
                "label": "acknowledgement",
                "kind": "date",
                "required": True,
                "value": __import__("datetime").date.today().isoformat(),
                "source": "deterministic_override",
            },
        ]
    )

    # Consent checkboxes
    steps.extend(
        [
            {
                "field_name": "privacy_consent",
                "label": "read and consent to this Privacy Statement",
                "kind": "checkbox",
                "required": True,
                "value": "checked",
                "source": "deterministic_override",
            },
            {
                "field_name": "nda_consent",
                "label": "acknowledge and agree to abide by the terms of this Nondisclosure",
                "kind": "checkbox",
                "required": True,
                "value": "checked",
                "source": "deterministic_override",
            },
        ]
    )

    # Demographics
    steps.extend(
        [
            mark_visible_self_id_step(
                {
                    "field_name": "veteran_status",
                    "label": "Veteran Status",
                    "kind": "combobox",
                    "required": False,
                    "value": application_profile.veteran_status or "I am not a protected veteran",
                    "source": "application_profile.md",
                },
                profile_field="veteran_status",
            ),
            mark_visible_self_id_step(
                {
                    "field_name": "gender",
                    "label": "Gender",
                    "kind": "combobox",
                    "required": False,
                    "value": getattr(application_profile, "gender_identity", None)
                    or application_profile.gender
                    or "Male",
                    "source": "application_profile.md",
                },
                profile_field="gender_identity" if getattr(application_profile, "gender_identity", None) else "gender",
            ),
            mark_visible_self_id_step(
                {
                    "field_name": "race_ethnicity",
                    "label": "Hispanic or Latino",
                    "kind": "checkbox",
                    "required": False,
                    "value": "checked",
                    "source": "application_profile.md",
                },
                profile_field="race_or_ethnicity",
            ),
            {
                "field_name": "disability_language",
                "label": "Language",
                "kind": "combobox",
                "required": False,
                "value": "English",
                "source": "deterministic_override",
            },
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
            "Eightfold single-page form. Browser selectors are provisional.",
            "Deterministic overrides handle PEP, NDA, privacy, and previous-employee questions.",
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
    try:
        accept = page.get_by_role("button", name="Accept")
        if accept.count() and accept.is_visible():
            accept.click()
            page.wait_for_timeout(500)
    except Exception:
        pass


def _dismiss_privacy_dialog(page) -> None:
    """Dismiss Eightfold 'Data Privacy Agreement' dialog if present.

    Eightfold sites show a modal with Cancel/Agree buttons before the
    job description page is interactive.  Must be dismissed before
    clicking Apply.
    """
    try:
        agree = page.get_by_role("button", name=re.compile(r"^(?:I\s+)?(?:Agree|Understand)$", re.I))
        if agree.count() and agree.is_visible():
            agree.click()
            page.wait_for_timeout(1000)
    except Exception:
        pass


def _page_body_text(page) -> str:
    try:
        return normalize_text(page.locator("body").inner_text(timeout=5000))
    except Exception:
        return ""


def _eightfold_job_closed_reason(body_text: str) -> str | None:
    normalized = normalize_text(body_text)
    if not normalized:
        return None
    for marker in _JOB_CLOSED_MARKERS:
        if normalize_text(marker) in normalized:
            return marker
    return None


def _normalize_referral_source_option(value: str) -> str:
    normalized = normalize_text(value)
    if any(
        term in normalized
        for term in (
            "company website",
            "corporate website",
            "company site",
            "corporate site",
            "career site",
            "careers site",
            "careers page",
            "career page",
            "careers website",
            "career website",
            "direct source",
            "direct source candidate",
            "direct source candidates",
            "employer website",
            "employer site",
        )
    ):
        return "company website"
    return normalized


def _eightfold_combobox_option_matches(label: str, desired_value: str, option_text: str) -> bool:
    desired_normalized = normalize_text(desired_value)
    option_normalized = normalize_text(option_text)
    if desired_normalized in option_normalized or option_normalized in desired_normalized:
        return True

    label_normalized = normalize_text(label)
    if any(
        phrase in label_normalized
        for phrase in ("how did you hear", "where did you hear", "heard about", "learn about")
    ):
        return _normalize_referral_source_option(desired_value) == _normalize_referral_source_option(option_text)
    return False


def _eightfold_profile_field_for_label(label: str) -> str | None:
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


def _resolve_eightfold_combobox_option(
    label: str,
    value: str,
    option_labels: list[str],
    *,
    profile_field: str | None = None,
) -> str | None:
    resolved_profile_field = profile_field or _eightfold_profile_field_for_label(label)
    if resolved_profile_field:
        matched = select_profile_option(option_labels, value, profile_field=resolved_profile_field)
        if matched is not None:
            return matched
    matched = select_option(option_labels, value)
    if matched is not None:
        return matched
    for option_text in option_labels:
        if _eightfold_combobox_option_matches(label, value, option_text):
            return option_text
    return None


def _fill_eightfold_combobox(page, label: str, value: str, *, profile_field: str | None = None) -> bool:
    """Fill an Eightfold combobox by label. Returns True if successful.

    Eightfold comboboxes have an adjacent expand button. Click the button
    to open the dropdown, then select the matching option.
    """
    from playwright.sync_api import Error as PlaywrightError

    pattern = re.compile(re.escape(label), re.I)

    def labeled_locator(role: str | None = None):
        if role is not None:
            try:
                locator = page.get_by_role(role, name=pattern).first
                if locator.count():
                    return locator
            except Exception:
                pass
        try:
            locator = page.get_by_label(pattern).first
            if locator.count():
                return locator
        except Exception:
            pass
        try:
            label_locator = page.locator("label").filter(has_text=pattern).first
            if label_locator.count():
                field_id = label_locator.get_attribute("for") or ""
                if field_id:
                    target = page.locator(f"#{field_id}").first
                    if target.count():
                        return target
                target = label_locator.locator("input, select, textarea").first
                if target.count():
                    return target
                target = label_locator.locator(
                    "xpath=following::input[1] | xpath=following::select[1] | xpath=following::textarea[1]"
                ).first
                if target.count():
                    return target
        except Exception:
            pass
        return None

    try:
        combobox = labeled_locator("combobox")
        if combobox is None or not combobox.count():
            return False
        combobox.scroll_into_view_if_needed()
        options = page.get_by_role("option")

        def dropdown_open() -> bool:
            try:
                if options.count():
                    return True
            except PlaywrightError:
                pass
            try:
                return (combobox.get_attribute("aria-expanded") or "").strip().lower() == "true"
            except Exception:
                return False

        def open_dropdown(action) -> bool:
            try:
                action()
                page.wait_for_timeout(250)
            except Exception:
                return False
            return dropdown_open()

        def click_with_short_timeout(locator) -> None:
            try:
                locator.click(timeout=1000)
            except TypeError:
                locator.click()

        # Prefer keyboard interactions because Eightfold sometimes layers privacy
        # dialogs over the input, which blocks pointer clicks while leaving the
        # semantic combobox keyboard-accessible.
        opened = False

        if not opened:
            try:
                combobox.focus()
            except Exception:
                pass
            if open_dropdown(lambda: combobox.press("ArrowDown")):
                opened = True

        if not opened:
            try:
                combobox.focus()
            except Exception:
                pass
            if open_dropdown(lambda: combobox.press("Enter")):
                opened = True

        # Eightfold tenants use different combobox wrappers. Re-check dismissible
        # dialogs, then try any actionable inline icon buttons.
        if not opened:
            _dismiss_cookie_banner(page)
            _dismiss_privacy_dialog(page)
            for selector in (
                "xpath=following-sibling::button | ../button",
                "xpath=following-sibling::button",
                "xpath=../button",
                "xpath=../div//button[1]",
                "xpath=ancestor::div[contains(@class, 'input-group')][1]//button[1]",
            ):
                try:
                    expand_btn = combobox.locator(selector).first
                    if not expand_btn.count() or not expand_btn.is_visible():
                        continue
                    if (expand_btn.get_attribute("aria-hidden") or "").strip().lower() == "true":
                        continue
                    if (expand_btn.get_attribute("role") or "").strip().lower() == "presentation":
                        continue
                except Exception:
                    continue
                if open_dropdown(lambda btn=expand_btn: click_with_short_timeout(btn)):
                    opened = True
                    break

        if not opened:
            _dismiss_cookie_banner(page)
            _dismiss_privacy_dialog(page)
            if open_dropdown(lambda: click_with_short_timeout(combobox)):
                opened = True

        # Find and click the matching option
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
        matched = _resolve_eightfold_combobox_option(
            label,
            value,
            option_labels,
            profile_field=profile_field,
        )
        if matched is not None:
            matched_option = options_by_text.get(matched)
            if matched_option is not None:
                try:
                    matched_option.evaluate("(el) => el.click()")
                except Exception:
                    matched_option.click()
                page.wait_for_timeout(300)
                return True

        # Close dropdown if no match found
        combobox.press("Escape")
        page.wait_for_timeout(200)
    except PlaywrightError:
        pass
    return False


def _mark_visible_self_id_unconfirmed(step: dict) -> None:
    step["filled"] = False
    step["status"] = "planned"
    step["note"] = "Selected the planned self-ID answer but could not confirm it remained visible on the live form."


def _eightfold_field_patterns(
    label: str,
    *,
    field_name: str | None = None,
    profile_field: str | None = None,
) -> tuple[re.Pattern[str], ...]:
    patterns: list[re.Pattern[str]] = []
    seen: set[str] = set()
    raw_terms: list[str] = []
    if label:
        raw_terms.append(label)
    raw_terms.extend(_EIGHTFOLD_FIELD_SEARCH_ALIASES.get(str(field_name or "").strip(), ()))
    raw_terms.extend(_EIGHTFOLD_PROFILE_FIELD_SEARCH_ALIASES.get(str(profile_field or "").strip(), ()))
    for term in raw_terms:
        normalized = normalize_text(term)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        patterns.append(re.compile(re.escape(term), re.I))
    return tuple(patterns)


def _labeled_eightfold_field(
    page,
    label: str,
    *,
    role: str | None = None,
    field_name: str | None = None,
    profile_field: str | None = None,
):
    for pattern in _eightfold_field_patterns(label, field_name=field_name, profile_field=profile_field):
        if role is not None:
            try:
                locator = page.get_by_role(role, name=pattern).first
                if locator.count():
                    return locator
            except Exception:
                pass
        try:
            locator = page.get_by_label(pattern).first
            if locator.count():
                return locator
        except Exception:
            pass
        try:
            label_locator = page.locator("label, legend").filter(has_text=pattern).first
            if label_locator.count():
                field_id = label_locator.get_attribute("for") or ""
                if field_id:
                    target = page.locator(f"#{field_id}").first
                    if target.count():
                        return target
                target = label_locator.locator("input, select, textarea").first
                if target.count():
                    return target
                target = label_locator.locator(
                    "xpath=following::input[1] | xpath=following::select[1] | xpath=following::textarea[1]"
                ).first
                if target.count():
                    return target
        except Exception:
            pass
    return None


def _eightfold_checkbox_group_pattern(profile_field: str | None) -> re.Pattern[str] | None:
    aliases = _EIGHTFOLD_PROFILE_FIELD_SEARCH_ALIASES.get(str(profile_field or "").strip(), ())
    if not aliases:
        return None
    return re.compile("|".join(re.escape(alias) for alias in aliases), re.I)


def _fill_eightfold_profile_checkbox_group(page, value: str, *, profile_field: str | None = None) -> bool:
    pattern = _eightfold_checkbox_group_pattern(profile_field)
    if pattern is None:
        return False
    try:
        group = page.get_by_role("group", name=pattern).first
        if not group.count():
            return False
        inputs = group.locator("input[type='checkbox']")
        option_labels: list[str] = []
        inputs_by_label: dict[str, object] = {}
        for i in range(inputs.count()):
            input_locator = inputs.nth(i)
            option_label = str(input_locator.get_attribute("value") or "").strip()
            if not option_label:
                input_id = str(input_locator.get_attribute("id") or "").strip()
                if input_id:
                    label_locator = group.locator(f'label[for="{input_id}"]').first
                    if label_locator.count():
                        option_label = label_locator.inner_text().strip()
            if not option_label:
                continue
            option_labels.append(option_label)
            inputs_by_label.setdefault(option_label, input_locator)
        matched = select_profile_option(option_labels, value, profile_field=profile_field)
        if matched is None:
            return False
        matched_input = inputs_by_label.get(matched)
        if matched_input is None:
            return False
        try:
            matched_input.evaluate("(el) => el.click()")
        except Exception:
            matched_input.click()
        page.wait_for_timeout(250)
        return bool(matched_input.is_checked())
    except Exception:
        return False


def _confirm_eightfold_combobox(
    page,
    label: str,
    expected: str,
    *,
    field_name: str | None = None,
    profile_field: str | None = None,
) -> bool:
    try:
        combobox = _labeled_eightfold_field(
            page,
            label,
            role="combobox",
            field_name=field_name,
            profile_field=profile_field,
        )
        if combobox is None or not combobox.count():
            return False
        actual_values: list[str] = []
        for reader in (
            lambda: combobox.input_value(),
            lambda: combobox.inner_text(),
            lambda: combobox.get_attribute("value"),
            lambda: combobox.get_attribute("title"),
        ):
            try:
                actual = (reader() or "").strip()
            except Exception:
                actual = ""
            if actual and actual not in actual_values:
                actual_values.append(actual)

        if not actual_values:
            return False

        expected_cf = expected.casefold()
        for actual in actual_values:
            actual_cf = actual.casefold()
            if expected_cf in actual_cf or actual_cf in expected_cf:
                return True

        return (
            _resolve_eightfold_combobox_option(
                label,
                expected,
                actual_values,
                profile_field=_eightfold_profile_field_for_label(label),
            )
            is not None
        )
    except Exception:
        return False


def _confirm_eightfold_checkbox(
    page, label: str, *, field_name: str | None = None, profile_field: str | None = None
) -> bool:
    try:
        locator = _labeled_eightfold_field(
            page,
            label,
            role="checkbox",
            field_name=field_name,
            profile_field=profile_field,
        )
        return bool(locator is not None and locator.count() and locator.is_checked())
    except Exception:
        return False


def _set_eightfold_readonly_input_value(locator, value: str) -> bool:
    try:
        locator.evaluate(
            """
            (el, nextValue) => {
              el.removeAttribute("readonly");
              el.value = nextValue;
              el.dispatchEvent(new Event("input", { bubbles: true }));
              el.dispatchEvent(new Event("change", { bubbles: true }));
              el.dispatchEvent(new Event("blur", { bubbles: true }));
            }
            """,
            value,
        )
        return True
    except Exception:
        return False


def _fill_step(page, step: dict) -> None:
    """Fill a single form field on the Eightfold application page."""
    kind = step.get("kind", "")
    label = step.get("label", "")
    value = step.get("value", "")
    field_name = str(step.get("field_name") or "").strip() or None
    profile_field = str(step.get("profile_field") or "").strip() or None

    _dismiss_cookie_banner(page)
    _dismiss_privacy_dialog(page)

    if kind == "file":
        file_path = step.get("file_path", "")
        if not file_path:
            return
        # Try label-associated file input first (label nearby or aria-label)
        try:
            label_loc = page.get_by_label(re.compile(re.escape(label), re.I))
            file_input = label_loc.locator("input[type='file']").first
            if not file_input.count():
                file_input = label_loc.first
            if file_input.count() and file_input.get_attribute("type") == "file":
                file_input.set_input_files(file_path)
                step["filled"] = True
                return
        except Exception:
            pass
        # Fallback: find file input near label text
        try:
            section = page.locator(f"text=/{re.escape(label)}/i").first
            if section.count():
                file_input = section.locator("xpath=ancestor::div[.//input[@type='file']]//input[@type='file']").first
                if file_input.count():
                    file_input.set_input_files(file_path)
                    step["filled"] = True
                    return
        except Exception:
            pass
        # Last resort: first available file input (resume goes first)
        locator = page.locator("input[type='file']").first
        if locator.count():
            locator.set_input_files(file_path)
            step["filled"] = True
        return

    if kind == "text":
        try:
            locator = _labeled_eightfold_field(
                page,
                label,
                role="textbox",
                field_name=field_name,
                profile_field=profile_field,
            )
            if locator is not None and locator.count():
                locator.scroll_into_view_if_needed()
                locator.fill(str(value))
                step["filled"] = True
                return
        except Exception:
            pass
        step.setdefault("status", "skipped_not_found")
        return

    if kind == "combobox":
        filled = _fill_eightfold_combobox(page, label, value, profile_field=profile_field)
        filled_with_checkbox_group = False
        if not filled and profile_field:
            filled = _fill_eightfold_profile_checkbox_group(page, str(value), profile_field=profile_field)
            filled_with_checkbox_group = filled
        if filled:
            if (
                is_visible_self_id_blocker(step)
                and not filled_with_checkbox_group
                and not _confirm_eightfold_combobox(
                    page,
                    label,
                    str(value),
                    field_name=field_name,
                    profile_field=profile_field,
                )
            ):
                _mark_visible_self_id_unconfirmed(step)
                return
            step["filled"] = True
        else:
            step.setdefault("status", "skipped_not_found")
        return

    if kind == "checkbox":
        filled_with_checkbox_group = False
        if profile_field:
            filled_with_checkbox_group = _fill_eightfold_profile_checkbox_group(
                page, str(value), profile_field=profile_field
            )
            if filled_with_checkbox_group:
                step["filled"] = True
                return
        try:
            locator = _labeled_eightfold_field(
                page,
                label,
                role="checkbox",
                field_name=field_name,
                profile_field=profile_field,
            )
            if locator is not None and locator.count() and not locator.is_checked():
                locator.scroll_into_view_if_needed()
                locator.click()
                page.wait_for_timeout(200)
            if is_visible_self_id_blocker(step) and not _confirm_eightfold_checkbox(
                page,
                label,
                field_name=field_name,
                profile_field=profile_field,
            ):
                _mark_visible_self_id_unconfirmed(step)
                return
            step["filled"] = True
        except Exception:
            step.setdefault("status", "skipped_not_found")
        return

    if kind == "date":
        # Eightfold date fields are comboboxes — type the date string
        try:
            combobox = _labeled_eightfold_field(
                page,
                label,
                role="combobox",
                field_name=field_name,
                profile_field=profile_field,
            )
            if combobox is None or not combobox.count():
                step.setdefault("status", "skipped_not_found")
                return
            combobox.scroll_into_view_if_needed()
            if combobox.get_attribute("readonly") is not None:
                if _set_eightfold_readonly_input_value(combobox, value):
                    step["filled"] = True
                    return
            combobox.fill(value)
            page.wait_for_timeout(300)
            # Select the first option if dropdown appears
            opt = page.get_by_role("option").first
            if opt.count():
                opt.click()
                page.wait_for_timeout(200)
            step["filled"] = True
        except Exception:
            step.setdefault("status", "skipped_not_found")
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
    """Click the 'Apply' button on job detail pages to navigate to the form.

    Eightfold job URLs (e.g. /careers/job/{id}?domain=...) show a job
    description page.  The actual application form is behind an
    "Apply" / "Apply Now" button or link.
    """
    from playwright.sync_api import Error as PlaywrightError

    # If form inputs (file upload, textbox with name-like label) are already
    # visible, the form is rendered — no need to click.
    try:
        file_input = page.locator("input[type='file']").first
        if file_input.count() and file_input.is_visible():
            return
        name_field = page.get_by_role("textbox", name=re.compile(r"first.?name", re.I)).first
        if name_field.count() and name_field.is_visible():
            return
    except PlaywrightError:
        pass

    # Look for an Apply button/link on the job details page
    for btn_text in ("Apply Now", "Apply for this job", "Apply"):
        try:
            # Try buttons first (exact and substring match)
            for role in ("button", "link"):
                for pattern in (
                    re.compile(rf"^{re.escape(btn_text)}$", re.I),
                    re.compile(rf"{re.escape(btn_text)}", re.I),
                ):
                    loc = page.get_by_role(role, name=pattern).first
                    if loc.count() and loc.is_visible():
                        loc.scroll_into_view_if_needed()
                        loc.click()
                        page.wait_for_timeout(3000)
                        return
        except PlaywrightError:
            continue

    # CSS fallback: click any visible element containing "Apply" text
    try:
        for sel in (
            'a:has-text("Apply")',
            'button:has-text("Apply")',
            '[role="button"]:has-text("Apply")',
        ):
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.scroll_into_view_if_needed()
                loc.click()
                page.wait_for_timeout(3000)
                return
    except PlaywrightError:
        pass


def _wait_for_eightfold_form(page) -> None:
    """Wait for the Eightfold application form to render."""

    def _job_is_closed() -> bool:
        return _eightfold_job_closed_reason(_page_body_text(page)) is not None

    try:
        page.wait_for_selector(
            'button:has-text("Submit"), button:has-text("Apply"), a:has-text("Apply"), form, [role="main"]',
            timeout=25000,
        )
    except Exception:
        pass  # Page may use non-standard markup; proceed to click Apply
    page.wait_for_timeout(2000)  # Let React hydrate
    _dismiss_cookie_banner(page)
    _dismiss_privacy_dialog(page)
    if _job_is_closed():
        return
    # Job detail pages require clicking Apply to reveal the form
    _click_apply_if_needed(page)
    _dismiss_cookie_banner(page)
    _dismiss_privacy_dialog(page)
    if _job_is_closed():
        return
    # Wait for the actual application form fields to appear
    try:
        page.wait_for_selector(
            'input[type="file"], input[type="text"], form input',
            timeout=15000,
        )
        page.wait_for_timeout(1000)  # Let form fully render
    except Exception:
        if _job_is_closed():
            return
        page.wait_for_timeout(0)  # Proceed anyway — fields may use non-standard markup


def _detect_eightfold_auth_result(page, payload: dict[str, object]) -> dict[str, object] | None:
    body_text = _page_body_text(page)
    if not body_text:
        return None
    if "sign in" in body_text and "create an account" in body_text and "sign in using google" in body_text:
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
            "auth_state": "sign_in_gate",
            "auth_scope": f"{_BOARD}:{host}",
            "message": "Eightfold requires sign in or account creation before the application form is available.",
        }
    return None


def _detect_eightfold_job_closed_result(page, payload: dict[str, object]) -> dict[str, object] | None:
    reason = _eightfold_job_closed_reason(_page_body_text(page))
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
        "message": f"Eightfold job closed: {reason}.",
    }


def _post_navigate(page, payload_path: Path) -> None:
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    result = _detect_eightfold_auth_result(page, payload)
    if result is None:
        result = _detect_eightfold_job_closed_result(page, payload)
    if result is None:
        return
    submit_dir = role_submit_path(Path(str(payload["out_dir"])), "")
    submit_dir.mkdir(parents=True, exist_ok=True)
    result_path = submit_dir / "application_submission_result.json"
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(str(result.get("message") or "Eightfold pre-form terminal state detected."), file=sys.stderr)


# --- Entry point ---


def _run_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    """Wrap run_browser_pipeline with Eightfold-specific callbacks."""
    from autofill_pipeline import run_browser_pipeline

    return run_browser_pipeline(
        payload_path,
        headless=headless,
        submit=submit,
        board_name=_BOARD,
        retry_unconfirmed_visible_self_id_once=True,
        form_ready_fn=_wait_for_eightfold_form,
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
