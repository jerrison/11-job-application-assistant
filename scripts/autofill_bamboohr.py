#!/usr/bin/env python3
"""BambooHR ATS application autofill.

BambooHR forms are single-page forms at URLs like
``https://coherent.bamboohr.com/careers/229``.
The form appears after clicking "Apply for This Job" on the JD page.
Uses run_browser_pipeline().
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
    build_truthful_work_authorization_answer,
    find_cover_letter_file,
    find_resume_file,
    format_education_from_profile,
    load_meta,
    parse_application_profile,
    parse_master_resume,
    profile_available_start_date,
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
from llm_provider import default_active_provider
from output_layout import migrate_role_output_layout, role_submit_path
from project_env import load_project_env
from question_classifier import classify_question

_BOARD = "bamboohr"
_BOARD_CONSTANTS = board_file_constants(_BOARD)
AUTOFILL_PAYLOAD_JSON = _BOARD_CONSTANTS["payload_json"]
APPLICATION_PAGE_HTML = _BOARD_CONSTANTS["application_page_html"]

SUBMIT_BUTTON_NAMES = (
    "Submit Application",
    "Submit",
    "Apply",
    "Send Application",
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

HONEYPOT_LABELS = ("please leave this field blank",)

load_project_env()


# --- Deterministic overrides ---


def _infer_deterministic(label: str, options: list[str]) -> str | None:
    """Return a deterministic answer or None to defer to LLM.

    Uses the unified question classifier as first pass, then falls back to
    BambooHR-specific inline checks.
    """
    app_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    policy = resolve_shared_question_policy(label, app_profile)
    if policy is not None and policy.text_value is not None:
        return select_shared_policy_option(options, policy, application_profile=app_profile) or policy.text_value

    # --- Unified classifier dispatch ---
    category = classify_question(label)
    if category is not None:
        _CATEGORY_TO_ANSWER = {
            "work_authorization": "Yes",
            "culture_careers_optin": "No",
            "interview_accommodation": "No",
            "reasonable_accommodation": "Yes",
            "product_usage": "Yes",
            "nda_noncompete": "No",
            "salary_comfort": "Yes",
            "minimum_experience": "Yes",
            "experience_confirmation": "Yes",
            "office_attendance": "Yes",
            "company_engagement": "Yes",
        }
        if category in _CATEGORY_TO_ANSWER:
            return select_option(options, _CATEGORY_TO_ANSWER[category]) or _CATEGORY_TO_ANSWER[category]

    # --- BambooHR-specific fallbacks ---
    ll = label.casefold()

    # Sponsorship -> No
    if "sponsorship" in ll or "require sponsorship" in ll:
        return select_option(options, "No") or "No"

    return None


_COMPENSATION_DEFLECT = (
    "I'm open and flexible on compensation. I'd prefer to learn more about "
    "the role's scope and total rewards package before discussing specific numbers. "
    "I'm confident we can find a mutually agreeable arrangement."
)


def _answer_from_classifier_text(label: str, app_profile) -> str | None:
    """Use the unified classifier to produce a deterministic text answer.

    For textarea/text fields in BambooHR custom questions.
    Returns an answer string or None.
    """
    category = classify_question(label)
    if category is None:
        return None

    policy = resolve_shared_question_policy(label, app_profile)
    if policy is not None and policy.category != "work_authorization" and policy.text_value is not None:
        return policy.text_value

    if category == "education":
        return format_education_from_profile(app_profile) or None
    if category == "compensation":
        return _COMPENSATION_DEFLECT
    if category == "nda_noncompete":
        return "No"
    if category == "work_authorization":
        return build_truthful_work_authorization_answer(label, app_profile) or (
            getattr(app_profile, "work_authorization_statement", None) or None
        )
    if category == "city_location":
        return getattr(app_profile, "location", None) or None
    if category == "current_company":
        return getattr(app_profile, "current_company", None) or None
    if category == "culture_careers_optin":
        return "No"
    if category == "product_usage":
        return "Yes"
    if category == "interview_accommodation":
        return "No"
    if category == "reasonable_accommodation":
        return "Yes"
    return None


def _available_start_date(app_profile) -> str:
    """Return dd/mm/yyyy for the profile-backed earliest available start date."""
    return profile_available_start_date(app_profile).strftime("%d/%m/%Y")


def _parse_city_state(location: str) -> tuple[str, str]:
    """Split 'City, ST' into (city, state) tuple."""
    parts = [p.strip() for p in location.split(",", 1)]
    if len(parts) == 2:
        return parts[0], parts[1]
    return location, ""


# --- Payload builder ---


def _build_payload(out_dir: Path, provider: str) -> dict:
    """Build the autofill payload for a BambooHR application."""
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)
    job_url = str(meta.get("jd_source_resolved") or meta.get("jd_source") or "")
    profile = parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))

    resume_path = find_resume_file(out_dir)
    cover_letter_path = find_cover_letter_file(out_dir)

    city, state = _parse_city_state(application_profile.location)

    # Build deterministic steps for standard fields
    steps: list[dict] = []

    # Personal info
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

    # Address fields
    steps.extend(
        [
            {
                "field_name": "address",
                "label": "Address",
                "kind": "text",
                "required": True,
                "value": city,
                "source": "application_profile.md",
            },
            {
                "field_name": "city",
                "label": "City",
                "kind": "text",
                "required": True,
                "value": city,
                "source": "application_profile.md",
            },
            {
                "field_name": "state",
                "label": "State",
                "kind": "select",
                "required": True,
                "value": state,
                "source": "application_profile.md",
            },
            {
                "field_name": "zip",
                "label": "ZIP",
                "kind": "text",
                "required": True,
                "value": "",
                "source": "application_profile.md",
            },
            {
                "field_name": "country",
                "label": "Country",
                "kind": "select",
                "required": False,
                "value": application_profile.country or "United States",
                "source": "application_profile.md",
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

    # Date Available
    steps.append(
        {
            "field_name": "date_available",
            "label": "Date Available",
            "kind": "text",
            "required": False,
            "value": _available_start_date(application_profile),
            "source": "calculated",
        }
    )

    # Desired Pay — never a specific number
    steps.append(
        {
            "field_name": "desired_pay",
            "label": "Desired Pay",
            "kind": "text",
            "required": True,
            "value": "Negotiable based on total compensation package",
            "source": "application_profile.md",
        }
    )

    # LinkedIn URL
    steps.append(
        {
            "field_name": "linkedin_url",
            "label": "LinkedIn URL",
            "kind": "text",
            "required": False,
            "value": application_profile.linkedin or "",
            "source": "application_profile.md",
        }
    )

    # Website
    steps.append(
        {
            "field_name": "website",
            "label": "Website",
            "kind": "text",
            "required": False,
            "value": application_profile.website or "",
            "source": "application_profile.md",
        }
    )

    # Education
    steps.extend(
        [
            {
                "field_name": "highest_education",
                "label": "Highest Education Obtained",
                "kind": "select",
                "required": True,
                "value": "Master's Degree",
                "source": "master_resume.md",
            },
            {
                "field_name": "college_university",
                "label": "College/University",
                "kind": "text",
                "required": False,
                "value": "University of Pennsylvania (Wharton MBA, M.S. Computer Science)",
                "source": "master_resume.md",
            },
        ]
    )

    # Referral / References — leave empty
    steps.extend(
        [
            {
                "field_name": "who_referred",
                "label": "Who referred you",
                "kind": "text",
                "required": False,
                "value": "",
                "source": "deterministic",
            },
            {
                "field_name": "references",
                "label": "References",
                "kind": "textarea",
                "required": False,
                "value": "",
                "source": "deterministic",
            },
        ]
    )

    # Work authorization radio — Yes
    steps.append(
        {
            "field_name": "work_authorization",
            "label": "Are you legally authorized to work in the United States?",
            "kind": "radio",
            "required": True,
            "value": "Yes",
            "source": "application_profile.md",
        }
    )

    # How did you hear about us radio
    steps.append(
        {
            "field_name": "how_did_you_hear",
            "label": "How did you hear about us?",
            "kind": "radio",
            "required": False,
            "value": "Job Boards",
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
            "BambooHR single-page form. Click 'Apply for This Job' to reveal form.",
            "Has honeypot field ('Please leave this field blank') — skip it.",
            "Has reCAPTCHA checkbox.",
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


def _is_honeypot_field(label: str) -> bool:
    """Check if a field label matches a known honeypot pattern."""
    return any(hp in label.casefold() for hp in HONEYPOT_LABELS)


def _fill_step(page, step: dict) -> None:
    """Fill a single form field on the BambooHR application page."""
    kind = step.get("kind", "")
    label = step.get("label", "")
    value = step.get("value", "")

    # Skip honeypot fields
    if _is_honeypot_field(label):
        step["filled"] = True
        return

    # Skip empty optional fields
    if not value and not step.get("file_path") and not step.get("required"):
        step["filled"] = True
        return

    if kind == "file":
        file_path = step.get("file_path", "")
        if not file_path:
            return
        try:
            locator = page.locator("input[type='file']").first
            if locator.count():
                locator.set_input_files(file_path)
                step["filled"] = True
        except Exception:
            pass
        return

    if kind == "text":
        try:
            # Try by role (accessible name) first
            locator = page.get_by_role("textbox", name=re.compile(re.escape(label), re.I)).first
            if locator.count():
                locator.scroll_into_view_if_needed()
                locator.fill(str(value))
                step["filled"] = True
                return
            # Fallback: by placeholder
            locator = page.locator(f"input[placeholder*='{label}' i]").first
            if locator.count():
                locator.scroll_into_view_if_needed()
                locator.fill(str(value))
                step["filled"] = True
                return
            # Fallback: by aria-label
            locator = page.locator(f"input[aria-label*='{label}' i]").first
            if locator.count():
                locator.scroll_into_view_if_needed()
                locator.fill(str(value))
                step["filled"] = True
                return
        except Exception:
            pass
        return

    if kind == "textarea":
        try:
            # Best: target by form name attribute (e.g. customQuestionAnswers.long_457)
            form_name = step.get("form_name", "")
            if form_name:
                locator = page.locator(f'textarea[name="{form_name}"]').first
                if locator.count():
                    locator.scroll_into_view_if_needed()
                    locator.fill(str(value))
                    step["filled"] = True
                    return
            # Fallback: by accessible name
            locator = page.get_by_role("textbox", name=re.compile(re.escape(label[:50]), re.I)).first
            if locator.count():
                locator.scroll_into_view_if_needed()
                locator.fill(str(value))
                step["filled"] = True
                return
        except Exception:
            pass
        return

    if kind == "select":
        try:
            # BambooHR uses custom dropdown buttons like: button "State –Select–"
            # Click the button to expand, then click the matching option
            drop_btn = page.get_by_role("button", name=re.compile(re.escape(label), re.I)).first
            if drop_btn.count() and drop_btn.is_visible():
                drop_btn.scroll_into_view_if_needed()
                drop_btn.click()
                page.wait_for_timeout(500)

                # Look for options in the dropdown list
                options = page.get_by_role("option")
                if not options.count():
                    options = page.get_by_role("listitem")
                if not options.count():
                    # BambooHR may render options as generic divs inside a dropdown
                    options = page.locator("[class*='option'], [class*='item'], li")

                for i in range(options.count()):
                    opt = options.nth(i)
                    try:
                        text = opt.inner_text().strip()
                    except Exception:
                        continue
                    if not text or text == "–Select–":
                        continue
                    if value.casefold() in text.casefold() or text.casefold() in value.casefold():
                        opt.click()
                        page.wait_for_timeout(300)
                        step["filled"] = True
                        return

                # Try pressing Escape to close if no match
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)

            # Fallback: native <select>
            sel = page.locator("select").first
            if sel.count():
                sel.select_option(label=value)
                step["filled"] = True
                return
        except Exception:
            pass
        return

    if kind == "radio":
        try:
            # Try clicking the radio with matching value text
            radio = page.get_by_role("radio", name=re.compile(re.escape(value), re.I)).first
            if radio.count():
                radio.scroll_into_view_if_needed()
                radio.click()
                page.wait_for_timeout(200)
                step["filled"] = True
                return
            # Fallback: find radio by label text near the question label
            radio_label = page.locator(f"label:has-text('{value}')").first
            if radio_label.count():
                radio_label.scroll_into_view_if_needed()
                radio_label.click()
                page.wait_for_timeout(200)
                step["filled"] = True
                return
        except Exception:
            pass
        return


def _classify_submit_state(snapshot: dict) -> dict[str, object]:
    """Classify page state after submit click."""
    page_text = str(snapshot.get("page_text") or "")

    # Check for confirmation
    if any(pattern.search(page_text) for pattern in SUBMIT_CONFIRM_PATTERNS):
        return {"status": "confirmed", "reason": "text"}

    # Check for reCAPTCHA
    if snapshot.get("recaptcha_visible") and snapshot.get("recaptcha_challenge_active"):
        return {"status": "captcha_required", "captcha_type": "recaptcha"}

    # Check for validation errors
    errors = list(snapshot.get("errors") or [])
    page_level_errors = [
        pattern.search(page_text).group(0) for pattern in VALIDATION_ERROR_PATTERNS if pattern.search(page_text)
    ]
    combined_errors = list(dict.fromkeys(errors + page_level_errors))
    if combined_errors:
        return {"status": "validation_error", "errors": combined_errors}

    return {"status": "pending"}


def _wait_for_bamboohr_form(page) -> None:
    """Click 'Apply for This Job' if visible, then wait for form fields."""
    # Wait for the JD page to load first
    page.wait_for_timeout(3000)

    # Click the "Apply for This Job" button to reveal the application form
    clicked = False
    for selector in [
        'button:has-text("Apply for This Job")',
        'a:has-text("Apply for This Job")',
        'text="Apply for This Job"',
    ]:
        try:
            el = page.locator(selector).first
            if el.count() and el.is_visible():
                el.click()
                clicked = True
                break
        except Exception:
            continue

    if clicked:
        page.wait_for_timeout(3000)

    # Wait for the form to render — look for labeled text inputs
    try:
        page.wait_for_selector(
            'input[aria-label="First Name"], input[aria-label="Email"], button:has-text("Submit Application")',
            timeout=25000,
        )
    except Exception:
        # Fallback: wait for any text input
        page.wait_for_selector('input[type="text"], input[type="email"]', timeout=10000)
    page.wait_for_timeout(2000)  # Let the form hydrate


# --- Entry point ---


def _discover_and_inject_custom_questions(page, payload_path: Path) -> None:
    """Discover custom questions from the live BambooHR form and inject LLM answers into the payload."""
    import json as _json

    from application_submit_common import generate_application_answers

    payload = _json.loads(payload_path.read_text(encoding="utf-8"))
    out_dir = Path(payload["out_dir"])
    meta = load_meta(out_dir)
    existing_labels = {s["label"].lower() for s in payload["steps"]}

    # Discover custom question fields from the live BambooHR form.
    # BambooHR uses MUI FormControl components where the label is in a
    # sibling div with class containing "label", not aria-label.
    # Custom questions have name="customQuestionAnswers.*".
    discovered = []
    try:
        fields_info = page.evaluate("""() => {
            const results = [];
            for (const el of document.querySelectorAll('textarea[name^="customQuestionAnswers"]')) {
                const parent = el.closest('.MuiFormControl-root');
                let label = '';
                if (parent) {
                    const labelDiv = parent.querySelector('[class*="label"]');
                    if (labelDiv) label = labelDiv.textContent.replace(/\\*/g, '').trim();
                }
                if (label) results.push({name: el.name, label: label, formName: el.name});
            }
            return results;
        }""")
    except Exception:
        fields_info = []

    for info in fields_info:
        name = info.get("label", "").strip()
        if not name:
            continue
        if name.lower() in existing_labels:
            continue
        if _is_honeypot_field(name):
            continue
        # Custom questions: long labels or contain a question mark
        if len(name) > 40 or "?" in name:
            discovered.append(
                {"field_name": re.sub(r"\W+", "_", name.lower())[:60], "label": name, "field_type": "LongText"}
            )

    if not discovered:
        return

    # Handle classifiable questions deterministically via unified classifier
    app_profile_text = APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
    app_profile = parse_application_profile(app_profile_text)
    new_steps = []
    llm_discovered = []
    for q in discovered:
        deterministic_value = _answer_from_classifier_text(q["label"], app_profile)
        if deterministic_value is not None:
            new_steps.append(
                {
                    "field_name": q["field_name"],
                    "label": q["label"],
                    "kind": "textarea",
                    "required": True,
                    "value": deterministic_value,
                    "source": "application_profile.md",
                    "form_name": q.get("form_name", ""),
                }
            )
        else:
            llm_discovered.append(q)
    discovered = llm_discovered

    if not discovered:
        if new_steps:
            payload["steps"].extend(new_steps)
            payload_path.write_text(_json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return

    # Generate LLM answers for the custom questions
    question_specs = [
        {"field_name": q["field_name"], "label": q["label"], "field_type": q["field_type"], "required": True}
        for q in discovered
    ]
    provider = default_active_provider()
    answers = generate_application_answers(
        out_dir=out_dir,
        meta=meta,
        question_specs=question_specs,
        provider=provider,
    )

    # Inject steps into payload (append to education steps already collected)
    for q in discovered:
        answer = answers.get(q["field_name"], "")
        if answer:
            new_steps.append(
                {
                    "field_name": q["field_name"],
                    "label": q["label"],
                    "kind": "textarea",
                    "required": True,
                    "value": answer,
                    "source": "generated_application_answer",
                    "form_name": q.get("form_name", ""),
                }
            )

    if new_steps:
        payload["steps"].extend(new_steps)
        payload_path.write_text(_json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _run_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    """Wrap run_browser_pipeline with BambooHR-specific callbacks."""
    from autofill_pipeline import run_browser_pipeline

    def _post_navigate(page):
        _discover_and_inject_custom_questions(page, payload_path)

    return run_browser_pipeline(
        payload_path,
        headless=headless,
        submit=submit,
        board_name=_BOARD,
        form_ready_fn=_wait_for_bamboohr_form,
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
