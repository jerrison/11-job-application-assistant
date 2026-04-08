#!/usr/bin/env python3
"""Generate and optionally run a Lever application autofill flow."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_submit_common import (
    _COMPENSATION_NEGOTIABLE_ANSWER,
    APPLICATION_PROFILE_PATH,
    PROJECT_ROOT,
    GeneratedAnswerBlockersError,
    _authorized_country_codes,
    _best_city_option,
    _shared_text_answer_overrides_generation_gate,
    build_onsite_start_location_answer,
    clear_pending_user_input,
    find_cover_letter_file,
    find_cover_letter_text,
    find_resume_file,
    format_education_from_profile,
    generate_application_answers,
    json_dumps_pretty,
    load_meta,
    normalize_text,
    parse_application_profile,
    parse_master_resume,
    pending_user_input_reason_for_spec,
    question_prefers_generated_free_text_answer,
    resolve_how_did_you_hear_candidates,
    resolve_how_did_you_hear_option_candidates,
    resolve_shared_question_policy,
    select_truthful_age_option,
    shared_text_answer_for_question,
    slugify_label,
    write_pending_user_input,
    write_pending_user_input_for_unconfirmed_fields,
)
from autofill_common import (
    board_file_constants,
    capture_full_page,
    click_submit_button,
    infer_unknown_question_blocker_metadata,
    is_visible_confirmation_blocker,
    label_matches,
    mark_visible_profile_field_step,
    mark_visible_self_id_step,
    page_snapshot,
    select_location_positive_fit_option,
    select_option,
    select_profile_option,
)
from browser_runtime import (
    human_fill,
    launch_chromium_browser,
)
from output_layout import JOB_UNAVAILABLE_JSON, migrate_role_output_layout, role_submit_path
from project_env import load_project_env
from question_classifier import classify_question

_BOARD_CONSTANTS = board_file_constants("lever")
AUTOFILL_REPORT_MD = _BOARD_CONSTANTS["report_md"]
AUTOFILL_REPORT_JSON = _BOARD_CONSTANTS["report_json"]
AUTOFILL_PRE_SUBMIT_SCREENSHOT = _BOARD_CONSTANTS["pre_submit_screenshot"]
AUTOFILL_PAGE_SCREENSHOTS_DIR = _BOARD_CONSTANTS["page_screenshots_dir"]
AUTOFILL_UNKNOWN_QUESTIONS_JSON = _BOARD_CONSTANTS["unknown_questions_json"]
AUTOFILL_SUBMIT_DEBUG_HTML = _BOARD_CONSTANTS["submit_debug_html"]
AUTOFILL_SUBMIT_DEBUG_SCREENSHOT = _BOARD_CONSTANTS["submit_debug_screenshot"]
AUTOFILL_PAYLOAD_JSON = _BOARD_CONSTANTS["payload_json"]
APPLICATION_PAGE_HTML = _BOARD_CONSTANTS["application_page_html"]
WEBSITE_CONFIRMATION_JSON = "application_confirmation_website.json"
EMAIL_CONFIRMATION_JSON = "application_confirmation_email.json"
NOTION_SYNC_STATUS_JSON = "notion_sync_status.json"
DEFAULT_CAPTCHA_WAIT_SECONDS = 300
SUBMIT_BUTTON_NAMES = ("Submit application", "Submit", "Apply")
PREFERRED_CAPTURE_SELECTORS = ("#application-form", ".content-wrapper", "main")
FORM_READY_SELECTOR = "#application-form"
SUBMIT_CONFIRM_PATTERNS = (
    re.compile(r"\bthank(?:s| you)\s+for\s+applying\b", re.I),
    re.compile(r"\bapplication (?:has been )?(?:received|submitted)\b", re.I),
    re.compile(r"\byour application has been submitted\b", re.I),
    re.compile(r"\bsubmitted successfully\b", re.I),
    re.compile(r"\bwe(?:'|’)ll be in touch\b", re.I),
)
VALIDATION_ERROR_PATTERNS = (
    re.compile(r"\bthis field is required\b", re.I),
    re.compile(r"\bplease (?:enter|select|complete)\b", re.I),
    re.compile(r"\binvalid\b", re.I),
    re.compile(r"\berror\b", re.I),
)

_AUTHORIZED_COUNTRY_OPTION_CANDIDATES = {
    "united_states": ("United States", "United States of America", "US", "U.S.", "USA"),
    "canada": ("Canada",),
    "united_kingdom": ("United Kingdom", "UK", "U.K.", "Great Britain", "Britain"),
}


load_project_env()


def _lever_job_closed_reason(*, response_status: int | None, url: str, page_text: str) -> str | None:
    normalized_text = re.sub(r"\s+", " ", str(page_text or "").casefold()).strip()
    if response_status == 404:
        return f"job_closed: Lever returned HTTP 404 at {url}"
    if any(
        fragment in normalized_text
        for fragment in (
            "job posting is no longer available",
            "the job you are looking for is no longer available",
            "this job has been filled",
            "page not found",
            "job not found",
        )
    ):
        return f"job_closed: Lever showed an unavailable job page at {url}"
    return None


def _write_lever_job_unavailable_artifact(
    out_dir: Path,
    *,
    application_url: str,
    source_url: str | None,
    message: str,
) -> Path:
    path = role_submit_path(out_dir, JOB_UNAVAILABLE_JSON)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "job_closed",
        "board": "lever",
        "application_url": application_url,
        "source_url": source_url,
        "message": message,
        "recorded_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _maybe_reexec_with_uv() -> None:
    if os.environ.get("JOB_ASSETS_LEVER_BOOTSTRAPPED") == "1":
        return
    if not shutil.which("uv"):
        return
    env = os.environ.copy()
    env["JOB_ASSETS_LEVER_BOOTSTRAPPED"] = "1"
    cmd = ["uv", "run", "--project", str(PROJECT_ROOT), "python", __file__, *sys.argv[1:]]
    raise SystemExit(subprocess.call(cmd, cwd=PROJECT_ROOT, env=env))


def _lever_application_url(job_url: str) -> str:
    parsed = urllib.parse.urlparse(job_url)
    path = parsed.path.rstrip("/")
    if not path.endswith("/apply"):
        path = f"{path}/apply"
    return urllib.parse.urlunparse(parsed._replace(path=path))


def _label_matches(field: dict, *fragments: str) -> bool:
    return label_matches(field, *fragments, word_boundary=True)


def _is_current_company_field(field: dict) -> bool:
    field_name = str(field.get("name") or "").casefold()
    if field_name == "org":
        return True
    label = normalize_text(field.get("label"))
    return label in {
        "company",
        "company name",
        "current company",
        "current company name",
        "organization",
        "current employer",
        "employer",
    }


def _is_standard_name_field(field: dict) -> bool:
    field_name = str(field.get("name") or "").casefold()
    label = normalize_text(field.get("label"))
    if field_name == "name":
        return True
    return label in {"name", "full name", "legal name", "your name"}


def _is_location_field(field: dict) -> bool:
    field_name = str(field.get("name") or "").casefold()
    if field_name == "location":
        return True
    label = normalize_text(field.get("label"))
    if label in {
        "location",
        "current location",
        "city",
        "city of residence",
        "residence city",
    }:
        return True
    return "city" in label and "state" in label and any(
        fragment in label for fragment in ("current", "currently", "reside", "live")
    )


_US_STATES: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "DC": "District of Columbia",
}
_STATE_NAME_TO_ABBREV = {v.casefold(): k for k, v in _US_STATES.items()}


def _candidate_state(application_profile) -> tuple[str, str]:
    """Return (state_abbreviation, state_full_name) from the candidate's location."""
    location = application_profile.location or ""
    parts = [p.strip() for p in location.split(",")]
    if len(parts) >= 2:
        abbrev = parts[-1].strip().upper()
        if abbrev in _US_STATES:
            return abbrev, _US_STATES[abbrev]
    for name, abbrev in _STATE_NAME_TO_ABBREV.items():
        if name in location.casefold():
            return abbrev, _US_STATES[abbrev]
    return "", ""


def _is_currently_in_state_question(field: dict, application_profile) -> bool:
    """Return True if the label asks 'Are you currently in <state>?'."""
    label = normalize_text(field.get("label"))
    if not label:
        return False
    if (
        "currently in" not in label
        and "located in" not in label
        and "based in" not in label
        and "reside in" not in label
    ):
        return False
    _, state_name = _candidate_state(application_profile)
    if not state_name:
        return False
    return any(full_name.casefold() in label for full_name in _US_STATES.values())


def _answer_currently_in_state(field: dict, application_profile) -> dict | None:
    """Answer 'Are you currently in <state>?' using the candidate's location."""
    label = normalize_text(field.get("label"))
    abbrev, state_name = _candidate_state(application_profile)
    if not state_name:
        return None
    in_that_state = state_name.casefold() in label
    if field["kind"] in {"select", "radio", "checkbox"}:
        already_option = _select_option(field, [f"I'm already in {state_name}", f"I am already in {state_name}"])
        if already_option:
            return _choice_step(field, already_option, source="application_profile.md")
    return _yes_no_step(field, value=in_that_state, source="application_profile.md")


def _match_candidate_state_option(field: dict, application_profile) -> str | None:
    """Pick the option matching the candidate's US state from a list of state options."""
    abbrev, state_name = _candidate_state(application_profile)
    if not abbrev:
        return None
    for opt in field.get("options") or []:
        opt_lower = opt.casefold()
        if state_name.casefold() in opt_lower or f"({abbrev.lower()})" in opt_lower:
            return opt
    return None


def _match_authorized_country_option(field: dict, application_profile) -> str | None:
    for code in _authorized_country_codes(application_profile):
        for candidate in _AUTHORIZED_COUNTRY_OPTION_CANDIDATES.get(code, ()):
            option = _select_option(field, [candidate])
            if option is not None:
                return option
    return None


def _is_location_application_selector(field: dict) -> bool:
    return _label_matches(field, "which location", "applying for") and field["kind"] == "select"


def _acknowledgment_checkbox_option(field: dict) -> str | None:
    if field["kind"] != "checkbox":
        return None
    for option in field.get("options") or []:
        option_text = option.casefold()
        if any(keyword in option_text for keyword in ("acknowledge", "i agree", "consent", "accept")):
            return option
    return None


_METRO_TO_STATE: dict[str, str] = {
    "nyc": "NY",
    "new york city": "NY",
    "new york metro": "NY",
    "bay area": "CA",
    "sf bay area": "CA",
    "san francisco": "CA",
    "los angeles": "CA",
    "la metro": "CA",
    "silicon valley": "CA",
    "chicago": "IL",
    "chicagoland": "IL",
    "seattle": "WA",
    "seattle metro": "WA",
    "boston": "MA",
    "boston metro": "MA",
    "dc area": "DC",
    "washington dc": "DC",
    "dmv": "DC",
    "dallas": "TX",
    "dfw": "TX",
    "austin": "TX",
    "houston": "TX",
    "denver": "CO",
    "denver metro": "CO",
    "atlanta": "GA",
    "atlanta metro": "GA",
    "miami": "FL",
    "south florida": "FL",
    "portland": "OR",
    "philadelphia": "PA",
    "philly": "PA",
    "detroit": "MI",
    "minneapolis": "MN",
    "twin cities": "MN",
    "phoenix": "AZ",
    "san diego": "CA",
    "raleigh": "NC",
    "rtp": "NC",
    "research triangle": "NC",
    "pittsburgh": "PA",
    "salt lake": "UT",
}


def _candidate_in_label_area(field: dict, application_profile) -> bool | None:
    """Check if candidate is in the geographic area mentioned in a 'located in' label.

    Returns True/False if an area is detected, None if no area recognised.
    """
    label = normalize_text(field.get("label"))
    user_state, _ = _candidate_state(application_profile)
    if not user_state:
        return None
    for area, state in _METRO_TO_STATE.items():
        if area in label:
            return user_state == state
    for abbrev, state_name in _US_STATES.items():
        if state_name.casefold() in label:
            return user_state == abbrev
    return None


def _normalize_spacing(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _location_confirmation_variants(value: str | None) -> set[str]:
    normalized = normalize_text(value)
    if not normalized:
        return set()
    variants = {normalized}
    parts = [normalize_text(part) for part in str(value or "").split(",") if normalize_text(part)]
    if len(parts) < 2:
        return variants

    country_tokens = {"united states", "united states of america", "us", "usa"}
    city = parts[0]
    state = parts[1]
    extras = [part for part in parts[2:] if part not in country_tokens]

    variants.add(" ".join([city, state]))
    if extras:
        variants.add(" ".join([city, state, *extras]))

    state_abbrev = _STATE_NAME_TO_ABBREV.get(state.casefold())
    if state_abbrev:
        state_abbrev_normalized = normalize_text(state_abbrev)
        variants.add(" ".join([city, state_abbrev_normalized]))
        if extras:
            variants.add(" ".join([city, state_abbrev_normalized, *extras]))

    state_name = _US_STATES.get(state.upper())
    if state_name:
        state_name_normalized = normalize_text(state_name)
        variants.add(" ".join([city, state_name_normalized]))
        if extras:
            variants.add(" ".join([city, state_name_normalized, *extras]))

    return {variant.strip() for variant in variants if variant.strip()}


def _location_matches_expected(actual: str | None, expected: str | None) -> bool:
    actual_variants = _location_confirmation_variants(actual)
    expected_variants = _location_confirmation_variants(expected)
    if not actual_variants or not expected_variants:
        return False
    return not actual_variants.isdisjoint(expected_variants)


def _primary_employer_name() -> str:
    text = (PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8")
    # Try markdown header format (## Company —) then plain text (COMPANY — Title)
    match = re.search(r"^\s*##\s+(.+?)\s+—", text, re.M)
    if not match:
        # Plain text: look for "EXPERIENCE" section, then first "COMPANY — Title" line
        exp_match = re.search(r"^EXPERIENCE\s*$", text, re.M)
        if exp_match:
            after = text[exp_match.end() :]
            match = re.search(r"^([A-Z][A-Z\s'&.,]+?)\s+—\s+", after, re.M)
    return match.group(1).strip() if match else ""


def _select_option(field: dict, candidates: list[str], *, profile_field: str | None = None) -> str | None:
    for candidate in candidates:
        result = (
            select_profile_option(
                field.get("options"),
                candidate,
                profile_field=profile_field,
                filter_select_prefix=True,
            )
            if profile_field
            else select_option(field.get("options"), candidate, filter_select_prefix=True)
        )
        if result is not None:
            return result
    return None


def _generated_answer_candidates(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def _select_age_option(field: dict, age_range: str | None) -> str | None:
    return select_truthful_age_option(field.get("options") or [], age_range)


def _choice_step(field: dict, option: str | None, *, source: str) -> dict | None:
    if not option:
        return None
    step = {
        "field_name": field["field_name"],
        "label": field["label"],
        "required": field["required"],
        "form_index": field["index"],
        "kind": field["kind"],
        "value": option,
        "source": source,
    }
    if field["kind"] == "checkbox":
        step["checked"] = True
    return step


def _yes_no_step(field: dict, *, value: bool, source: str) -> dict | None:
    option = None
    if field["kind"] in {"select", "radio", "checkbox"}:
        option = _select_option(field, ["Yes" if value else "No"])
        if option is None:
            return None
    if field["kind"] in {"select", "radio", "checkbox"}:
        return _choice_step(field, option, source=source)
    return {
        "field_name": field["field_name"],
        "label": field["label"],
        "required": field["required"],
        "form_index": field["index"],
        "kind": field["kind"],
        "value": option or ("Yes" if value else "No"),
        "source": source,
    }


def _shared_policy_step(field: dict, policy) -> dict | None:
    if policy.boolean_value is None:
        return None
    if field["kind"] == "checkbox":
        options = list(field.get("options") or [])
        return {
            "field_name": field["field_name"],
            "label": field["label"],
            "required": field["required"],
            "form_index": field["index"],
            "kind": "checkbox",
            "name": field.get("name"),
            "value": options[0] if options else None,
            "checked": policy.boolean_value,
            "source": policy.source,
        }
    return _yes_no_step(field, value=policy.boolean_value, source=policy.source)


def _is_textual_field(field: dict) -> bool:
    return str(field.get("kind") or "").casefold() in {"text", "textarea"}


def _should_defer_positive_fit_textarea(
    field: dict,
    *,
    category: str | None,
    policy,
) -> bool:
    return question_prefers_generated_free_text_answer(
        field.get("label"),
        field_type=field.get("kind"),
        category=category,
        policy=policy,
    )


def _step_from_classifier(
    field: dict,
    application_profile,
    *,
    out_dir: Path,
    meta: dict | None = None,
    company_name: str | None = None,
) -> dict | None:
    """Use the unified question classifier to produce a deterministic step.

    Returns a step dict if the classifier identifies the question category
    and a deterministic answer can be produced, otherwise None.
    """
    label = field.get("text", "") or field.get("label", "")
    category = classify_question(label)
    if category is None:
        return None

    base = {
        "field_name": field["field_name"],
        "label": field["label"],
        "required": field["required"],
        "form_index": field["index"],
        "kind": field["kind"],
        "name": field.get("name"),
    }

    # --- Text/textarea answers ---
    if category == "education" and field["kind"] in {"text", "textarea"}:
        education_text = format_education_from_profile(application_profile)
        if education_text:
            return {**base, "kind": field["kind"], "value": education_text, "source": "application_profile.md"}
        return None

    if category == "how_did_you_hear":
        if field["kind"] in {"text", "textarea"}:
            source_candidates, source = resolve_how_did_you_hear_candidates(
                application_profile,
                company_name=(meta or {}).get("company_proper") or company_name,
                job_url=(meta or {}).get("jd_source"),
                source_url=(meta or {}).get("source_url"),
                source_hint=(meta or {}).get("source"),
            )
            if not source_candidates:
                return None
            return {
                **base,
                "kind": field["kind"],
                "value": source_candidates[0],
                "source": source,
            }
        if field["kind"] in {"select", "radio", "checkbox"}:
            source_candidates, source = resolve_how_did_you_hear_option_candidates(
                application_profile,
                field.get("options", []),
                company_name=(meta or {}).get("company_proper") or company_name,
                job_url=(meta or {}).get("jd_source"),
                source_url=(meta or {}).get("source_url"),
                source_hint=(meta or {}).get("source"),
            )
            option = _select_option(field, source_candidates)
            if option is not None:
                return _choice_step(field, option, source=source)
            return None

    policy = resolve_shared_question_policy(
        label,
        application_profile,
        company_name=(meta or {}).get("company_proper") or (meta or {}).get("company"),
        job_url=(meta or {}).get("jd_source"),
        source_url=(meta or {}).get("source_url"),
        source_hint=(meta or {}).get("source"),
    )
    shared_text_answer = shared_text_answer_for_question(label, application_profile)
    if _should_defer_positive_fit_textarea(field, category=category, policy=policy) and not (
        _is_textual_field(field) and _shared_text_answer_overrides_generation_gate(shared_text_answer)
    ):
        return None
    if (
        _is_textual_field(field)
        and shared_text_answer
        and policy is not None
        and policy.text_value is not None
        and normalize_text(policy.text_value) in {"yes", "no", "n a", "na", "n/a"}
        and normalize_text(shared_text_answer) != normalize_text(policy.text_value)
    ):
        return {
            **base,
            "kind": field["kind"],
            "value": shared_text_answer,
            "source": "application_profile.md",
        }
    if (
        policy is not None
        and policy.category not in {"work_authorization", "relocation_willingness", "location_residency"}
        and policy.boolean_value is not None
    ):
        step = _shared_policy_step(field, policy)
        if step is not None:
            return step

    # --- Yes/No answers ---
    if category == "salary_comfort":
        return _yes_no_step(
            field,
            value=getattr(application_profile, "comfortable_with_posted_salary", True),
            source="application_profile.md",
        )
    if category == "product_usage":
        return _yes_no_step(field, value=True, source="deterministic_override")
    if category == "minimum_experience":
        return _yes_no_step(field, value=application_profile.minimum_years_experience, source="application_profile.md")
    if category == "experience_confirmation":
        return _yes_no_step(field, value=True, source="deterministic")
    if category == "office_attendance":
        matched = select_location_positive_fit_option(
            field.get("options"),
            application_profile=application_profile,
            filter_select_prefix=True,
        )
        if matched is not None:
            return _choice_step(field, matched, source="shared_positive_fit_policy")
        can_attend_office = (
            application_profile.lives_in_job_location and application_profile.comfortable_working_on_site
        )
        return _yes_no_step(field, value=can_attend_office, source="application_profile.md")
    if category == "interview_accommodation":
        return _yes_no_step(field, value=False, source="deterministic")
    if category == "reasonable_accommodation":
        return _yes_no_step(field, value=True, source="deterministic")
    if category == "company_engagement":
        return _yes_no_step(field, value=True, source="deterministic")

    if category == "compensation" and field["kind"] in {"text", "textarea"}:
        return {
            **base,
            "kind": field["kind"],
            "value": _COMPENSATION_NEGOTIABLE_ANSWER,
            "source": "application_profile.md",
        }
    if category == "nda_noncompete":
        return _yes_no_step(field, value=False, source="deterministic")

    # Categories handled elsewhere (city_location, current_company,
    # work_authorization) or that need board-specific routing
    # are left to the existing field-name / label-match logic.
    return None


def _infer_step(
    field: dict,
    *,
    meta: dict,
    profile,
    application_profile,
    out_dir: Path,
    generated_answers: dict[str, object],
) -> dict | None:
    base = {
        "field_name": field["field_name"],
        "label": field["label"],
        "required": field["required"],
        "form_index": field["index"],
        "kind": field["kind"],
        "name": field.get("name"),
    }
    field_name = str(field.get("name") or "")
    requires_sponsorship = application_profile.require_sponsorship_now or application_profile.require_sponsorship_future

    if field["kind"] == "file" and field_name == "resume":
        return {**base, "kind": "file", "file_path": str(find_resume_file(out_dir)), "source": "existing_resume_asset"}
    if field["kind"] == "file" and _label_matches(field, "cover letter"):
        return {
            **base,
            "kind": "file",
            "file_path": str(find_cover_letter_file(out_dir)),
            "source": "existing_cover_letter_asset",
        }
    if _is_current_company_field(field):
        return {**base, "kind": "text", "value": _primary_employer_name(), "source": "master_resume.md"}
    if _is_standard_name_field(field):
        return {**base, "kind": "text", "value": profile.full_name, "source": "master_resume.md"}
    if field_name == "email" or (field.get("name") == "email" and _label_matches(field, "email")):
        return {**base, "kind": "text", "value": profile.email, "source": "master_resume.md"}
    if field_name == "phone" or (field.get("name") == "phone" and _label_matches(field, "phone")):
        return mark_visible_profile_field_step(
            {**base, "kind": "text", "value": profile.phone, "source": "master_resume.md"},
            profile_field="phone",
        )
    onsite_answer = build_onsite_start_location_answer(field.get("label"), application_profile)
    if field["kind"] in {"text", "textarea"} and onsite_answer:
        return {
            **base,
            "kind": field["kind"],
            "value": onsite_answer,
            "source": "application_profile.md",
        }
    if _is_location_field(field):
        return mark_visible_profile_field_step(
            {**base, "kind": "text", "value": application_profile.location, "source": "application_profile.md"},
            profile_field="location",
        )
    # --- Acknowledgment / consent checkboxes ---
    acknowledgment_option = _acknowledgment_checkbox_option(field)
    if acknowledgment_option is not None:
        return _choice_step(field, acknowledgment_option, source="deterministic")
    # --- URL fields (only match text/textarea inputs) ---
    if field["kind"] in {"text", "textarea"} and (
        "linkedin" in field_name.casefold() or _label_matches(field, "linkedin")
    ):
        return mark_visible_profile_field_step(
            {
                **base,
                "kind": "text",
                "value": application_profile.linkedin or profile.linkedin or "",
                "source": "application_profile.md" if application_profile.linkedin else "master_resume.md",
            },
            profile_field="linkedin",
        )
    if field["kind"] in {"text", "textarea"} and ("github" in field_name.casefold() or _label_matches(field, "github")):
        return {
            **base,
            "kind": "text",
            "value": application_profile.github or "",
            "source": "application_profile.md" if application_profile.github else "not_provided",
        }
    if field["kind"] in {"text", "textarea"} and (
        any(token in field_name.casefold() for token in ("portfolio", "website"))
        or _label_matches(field, "portfolio", "website")
    ):
        return {
            **base,
            "kind": "text",
            "value": application_profile.website or profile.website or "",
            "source": "application_profile.md" if application_profile.website else "master_resume.md",
        }
    if field["kind"] in {"text", "textarea"} and "twitter" in field_name.casefold():
        return {**base, "kind": "text", "value": "", "source": "not_provided"}
    if field["kind"] in {"text", "textarea"} and _label_matches(
        field,
        "currently require sponsorship",
        "require sponsorship in the future",
        "type of visa",
        "additional information that aircall may need to know",
    ):
        if not requires_sponsorship:
            return {**base, "kind": field["kind"], "value": "N/A", "source": "application_profile.md"}
        return {
            **base,
            "kind": field["kind"],
            "value": application_profile.sponsorship_answer,
            "source": "application_profile.md",
        }
    # --- Unified classifier: education, salary_comfort, product_usage, etc. ---
    classifier_step = _step_from_classifier(
        field,
        application_profile,
        out_dir=out_dir,
        meta=meta,
        company_name=(meta or {}).get("company"),
    )
    if classifier_step is not None:
        return classifier_step
    if field_name == "comments" or _label_matches(field, "cover letter", "anything else", "additional information"):
        return {
            **base,
            "kind": "textarea",
            "value": find_cover_letter_text(out_dir),
            "source": "cover_letter_text.txt",
        }
    if field_name == "eeo[gender]" or _label_matches(field, "gender"):
        profile_field = "gender_identity" if getattr(application_profile, "gender_identity", None) else "gender"
        option = _select_option(
            field,
            [getattr(application_profile, "gender_identity", None) or application_profile.gender or ""],
            profile_field=profile_field,
        )
        return mark_visible_self_id_step(
            _choice_step(field, option, source="application_profile.md"),
            profile_field=profile_field,
        )
    if _label_matches(field, "transgender"):
        option = _select_option(
            field,
            [getattr(application_profile, "transgender_status", None) or "No"],
            profile_field="transgender_status",
        )
        return mark_visible_self_id_step(
            _choice_step(field, option, source="application_profile.md"),
            profile_field="transgender_status",
        )
    if field_name == "eeo[race]" or _label_matches(field, "race or ethnicity", "race", "racial", "ethnicity", "ethnic"):
        raw_race = application_profile.race_or_ethnicity or ""
        race_candidates = [raw_race]
        rl = raw_race.lower()
        if "hispanic" in rl or "latino" in rl or "latina" in rl:
            race_candidates.extend(
                [
                    "Hispanic or Latinx",
                    "Hispanic or Latino",
                    "Hispanic / Latino",
                    "Hispanic/Latinx",
                    "Latino/a",
                    "Latinx",
                ]
            )
        option = _select_option(field, race_candidates, profile_field="race_or_ethnicity")
        return mark_visible_self_id_step(
            _choice_step(field, option, source="application_profile.md"),
            profile_field="race_or_ethnicity",
        )
    if field_name == "eeo[veteran]" or _label_matches(field, "veteran"):
        veteran_value = application_profile.veteran_status or ""
        candidates = [veteran_value]
        if "not" in veteran_value.casefold() and "veteran" in veteran_value.casefold():
            candidates.insert(0, "I am not a veteran")
        option = _select_option(field, candidates, profile_field="veteran_status")
        return mark_visible_self_id_step(
            _choice_step(field, option, source="application_profile.md"),
            profile_field="veteran_status",
        )
    if _label_matches(field, "sexual orientation"):
        raw_so = application_profile.sexual_orientation or ""
        so_candidates = [raw_so]
        sl = raw_so.lower()
        if "straight" in sl or "heterosexual" in sl:
            so_candidates.extend(
                [
                    "Heterosexual / straight",
                    "Heterosexual/straight",
                    "Heterosexual",
                    "Straight",
                    "Straight / Heterosexual",
                ]
            )
        option = _select_option(field, so_candidates, profile_field="sexual_orientation")
        return mark_visible_self_id_step(
            _choice_step(field, option, source="application_profile.md"),
            profile_field="sexual_orientation",
        )
    if _label_matches(field, "communities", "community"):
        raw_comm = getattr(application_profile, "communities", None) or "None of the above"
        comm_candidates = [raw_comm, "None of the above", "None", "Not applicable"]
        option = _select_option(field, comm_candidates, profile_field="communities")
        return mark_visible_self_id_step(
            _choice_step(field, option, source="application_profile.md"),
            profile_field="communities",
        )
    if _label_matches(field, "pronoun", "pronouns"):
        if field["kind"] in {"text", "textarea"}:
            return mark_visible_self_id_step(
                {
                    **base,
                    "kind": field["kind"],
                    "value": application_profile.pronouns or "",
                    "source": "application_profile.md",
                },
                profile_field="pronouns",
            )
        raw_pronouns = application_profile.pronouns or ""
        candidates = [raw_pronouns]
        lp = raw_pronouns.lower().replace("/", " ").replace(",", " ")
        if "he" in lp:
            candidates.extend(["He/Him", "He/Him/His", "he him", "he him his"])
        elif "she" in lp:
            candidates.extend(["She/Her", "She/Her/Hers", "she her", "she her hers"])
        elif "they" in lp:
            candidates.extend(["They/Them", "They/Them/Theirs", "they them", "they them theirs"])
        option = _select_option(field, candidates, profile_field="pronouns")
        return mark_visible_self_id_step(
            _choice_step(field, option, source="application_profile.md"),
            profile_field="pronouns",
        )
    if _label_matches(field, "disability"):
        option = _select_option(
            field,
            [application_profile.disability_status or ""],
            profile_field="disability_status",
        )
        return mark_visible_self_id_step(
            _choice_step(field, option, source="application_profile.md"),
            profile_field="disability_status",
        )
    if _label_matches(field, "age group", "age range", "your age", "age:"):
        truthful_option = _select_age_option(field, getattr(application_profile, "age_range", None))
        if truthful_option:
            return mark_visible_profile_field_step(
                _choice_step(field, truthful_option, source="application_profile.md"),
                profile_field="age_range",
            )
        option = _select_option(
            field,
            ["Prefer not to say", "I prefer not to say", "I don't wish to answer", "I do not wish to answer"],
        )
        return mark_visible_profile_field_step(
            _choice_step(field, option, source="deterministic"),
            profile_field="age_range",
        )
    if field_name == "consent[marketing]" or _label_matches(field, "future job opportunities", "marketing"):
        return {**base, "kind": "checkbox", "checked": False, "source": "default_no"}

    if field["kind"] in {"radio", "checkbox"} and _label_matches(field, "do you have experience"):
        return _yes_no_step(field, value=True, source="deterministic")
    if _label_matches(field, "sponsorship", "sponsor", "visa", "immigration"):
        return _yes_no_step(field, value=requires_sponsorship, source="application_profile.md")
    if _label_matches(
        field,
        "authorized to work",
        "authorised to work",
        "work authorization",
        "work authorisation",
        "right to work",
        "legally authorized",
        "legally authorised",
        "eligible to work",
    ):
        return _yes_no_step(
            field, value=application_profile.authorized_to_work_unconditionally, source="application_profile.md"
        )
    if _label_matches(field, "located outside of the us", "located outside the us", "outside of the united states"):
        is_in_us = (application_profile.country or "").casefold() in {"united states", "us", "usa"}
        return _yes_no_step(field, value=not is_in_us, source="application_profile.md")
    if _is_currently_in_state_question(field, application_profile):
        return _answer_currently_in_state(field, application_profile)
    if field["kind"] in {"radio", "select"} and _label_matches(field, "live in", "reside in", "currently live"):
        state_option = _match_candidate_state_option(field, application_profile)
        if state_option is not None:
            return _choice_step(field, state_option, source="application_profile.md")
    if (
        field["kind"] in {"radio", "select"}
        and _label_matches(field, "located in")
        and not _label_matches(field, "outside")
    ):
        in_area = _candidate_in_label_area(field, application_profile)
        if in_area is not None:
            return _yes_no_step(field, value=in_area, source="application_profile.md")
    if _label_matches(
        field, "relocate", "relocating", "based in", "commute", "on site", "on-site", "onsite", "in person", "in-person"
    ):
        if field["kind"] in {"select", "radio", "checkbox"}:
            _, state_name = _candidate_state(application_profile)
            if state_name:
                already_option = _select_option(
                    field, [f"I'm already in {state_name}", f"I am already in {state_name}"]
                )
                if already_option:
                    return _choice_step(field, already_option, source="application_profile.md")
        return _yes_no_step(field, value=True, source="application_profile.md")
    if _label_matches(
        field, "previously employed", "previously been employed", "worked for", "worked at", "former", "employed by"
    ):
        has_worked = (
            any(employer in (field.get("label") or "").casefold() for employer in profile.employers)
            if hasattr(profile, "employers")
            else False
        )
        return _yes_no_step(field, value=has_worked, source="master_resume.md")
    if _label_matches(field, "lgbtq", "lgbt"):
        straight = application_profile.sexual_orientation and any(
            t in (application_profile.sexual_orientation or "").casefold() for t in ("straight", "heterosexual")
        )
        candidates = (
            ["I do not identify as LGBTQ+", "No", "I don't wish to answer"] if straight else ["Prefer not to say"]
        )
        option = _select_option(field, candidates)
        return mark_visible_self_id_step(
            _choice_step(field, option, source="application_profile.md"),
            profile_field="sexual_orientation",
        )
    if _label_matches(field, "race", "racial", "ethnicity", "ethnic") and not _label_matches(
        field, "authorized", "work"
    ):
        option = _select_option(field, [application_profile.race_or_ethnicity or "Prefer not to say"])
        return mark_visible_self_id_step(
            _choice_step(field, option, source="application_profile.md"),
            profile_field="race_or_ethnicity",
        )
    if _is_location_application_selector(field):
        country_option = _match_authorized_country_option(field, application_profile)
        if country_option is not None:
            return _choice_step(field, country_option, source="application_profile.md")
        generated_value = generated_answers.get(field["field_name"])
        generated_candidates = _generated_answer_candidates(generated_value)
        if generated_candidates:
            option = _select_option(field, generated_candidates)
            if option is not None:
                return _choice_step(field, option, source="generated_application_answer")
        option = _best_city_option(
            field.get("options") or [],
            None,
            application_profile.location or "",
        )
        if option is None:
            _, state_name = _candidate_state(application_profile)
            if state_name:
                for opt in field.get("options") or []:
                    if state_name.casefold() in opt.casefold():
                        option = opt
                        break
        return _choice_step(field, option, source="application_profile.md")

    generated_value = generated_answers.get(field["field_name"])
    if field["kind"] in {"radio", "select"}:
        generated_candidates = _generated_answer_candidates(generated_value)
        if generated_candidates:
            option = _select_option(field, generated_candidates)
            return _choice_step(field, option, source="generated_application_answer")
    if field["kind"] == "checkbox":
        generated_candidates = _generated_answer_candidates(generated_value)
        matched_options: list[str] = []
        for candidate in generated_candidates:
            option = _select_option(field, [candidate])
            if option and option not in matched_options:
                matched_options.append(option)
        if matched_options:
            return {
                **base,
                "kind": "checkbox",
                "value": matched_options if len(matched_options) > 1 else matched_options[0],
                "source": "generated_application_answer",
                "checked": True,
            }
    if isinstance(generated_value, str):
        if field["kind"] in {"text", "textarea"}:
            return {
                **base,
                "kind": field["kind"],
                "value": generated_value.strip(),
                "source": "generated_application_answer",
            }
    return None


def _pending_user_input_fields(fields: list[dict]) -> list[dict]:
    pending: list[dict] = []
    for field in fields:
        reason = pending_user_input_reason_for_spec(field)
        if not reason:
            continue
        pending.append(
            {
                "field_name": field["field_name"],
                "label": field["label"],
                "kind": field["kind"],
                "required": field["required"],
                "name": field.get("name"),
                "reason": reason,
            }
        )
    return pending


def _inspect_lever_form(
    application_url: str,
    *,
    headless: bool,
    cache_path: Path | None = None,
    out_dir: Path | None = None,
) -> dict:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        _maybe_reexec_with_uv()
        print(
            "ERROR: Playwright is not installed in the project environment. Run `uv add playwright` first.",
            file=sys.stderr,
        )
        raise

    with sync_playwright() as playwright:
        browser = launch_chromium_browser(
            playwright,
            headless=headless,
            slow_mo=0,
            channel_env_var="JOB_ASSETS_SUBMIT_BROWSER_CHANNEL",
            executable_env_var="JOB_ASSETS_SUBMIT_BROWSER_EXECUTABLE",
            purpose="Lever autofill",
        )
        page = browser.new_page(viewport={"width": 1600, "height": 1200}, device_scale_factor=2)
        try:
            response = page.goto(application_url, wait_until="domcontentloaded", timeout=30000)
            response_status = getattr(response, "status", None)
            page_text = page.locator("body").inner_text(timeout=2000)
            closed_reason = _lever_job_closed_reason(
                response_status=response_status,
                url=str(page.url or application_url),
                page_text=page_text,
            )
            if closed_reason is not None:
                if cache_path is not None:
                    cache_path.write_text(page.content(), encoding="utf-8")
                if out_dir is not None:
                    unavailable_path = _write_lever_job_unavailable_artifact(
                        out_dir,
                        application_url=str(page.url or application_url),
                        source_url=application_url,
                        message=closed_reason,
                    )
                    raise RuntimeError(f"{closed_reason} Evidence: {unavailable_path}")
                raise RuntimeError(closed_reason)
            try:
                page.wait_for_selector("#application-form", timeout=25000)
            except PlaywrightTimeoutError as timeout_err:
                closed_reason = _lever_job_closed_reason(
                    response_status=response_status,
                    url=str(page.url or application_url),
                    page_text=page.locator("body").inner_text(timeout=2000),
                )
                if closed_reason is not None:
                    if cache_path is not None:
                        cache_path.write_text(page.content(), encoding="utf-8")
                    if out_dir is not None:
                        unavailable_path = _write_lever_job_unavailable_artifact(
                            out_dir,
                            application_url=str(page.url or application_url),
                            source_url=application_url,
                            message=closed_reason,
                        )
                        raise RuntimeError(f"{closed_reason} Evidence: {unavailable_path}") from timeout_err
                    raise RuntimeError(closed_reason) from timeout_err
                raise
            page.wait_for_timeout(1000)
            if cache_path is not None:
                cache_path.write_text(page.content(), encoding="utf-8")
            return page.evaluate(
                """() => {
                    const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                    const groups = Array.from(document.querySelectorAll('#application-form .application-question, #application-form .application-additional'));
                    const fieldData = groups.map((group, index) => {
                        const labelElement = group.querySelector('.application-label') || group.querySelector('label');
                        const rawLabel = normalize(labelElement ? labelElement.innerText : '');
                        const cleanLabel = normalize(rawLabel.replace(/[✱*]+$/g, ''));
                        const awliWidget =
                            group.classList.contains('awli-application-row') ||
                            !!group.querySelector('.awli-button-container, .awli-button, .IN-widget');
                        const fileInput = group.querySelector('input[type="file"]');
                        const textarea = group.querySelector('textarea');
                        const select = group.querySelector('select');
                        const radio = group.querySelector('input[type="radio"]');
                        const checkbox = group.querySelector('input[type="checkbox"]');
                        const textInput = group.querySelector("input:not([type='hidden']):not([type='file']):not([type='radio']):not([type='checkbox'])");
                        let kind = 'unknown';
                        let control = null;
                        if (awliWidget) {
                            kind = 'awli';
                        } else if (fileInput) {
                            kind = 'file';
                            control = fileInput;
                        } else if (textarea) {
                            kind = 'textarea';
                            control = textarea;
                        } else if (select) {
                            kind = 'select';
                            control = select;
                        } else if (radio) {
                            kind = 'radio';
                            control = radio;
                        } else if (checkbox) {
                            kind = 'checkbox';
                            control = checkbox;
                        } else if (textInput) {
                            kind = 'text';
                            control = textInput;
                        }
                        const name = control ? (control.getAttribute('name') || '') : '';
                        const inputType = control ? (control.getAttribute('type') || '') : '';
                        const required = !!group.querySelector('.required') || !!(control && control.required);
                        const options = select
                            ? Array.from(select.options).map((option) => normalize(option.textContent)).filter(Boolean)
                            : Array.from(group.querySelectorAll('label')).map((label) => normalize(label.innerText)).filter(Boolean);
                        return {
                            index,
                            label: cleanLabel || name,
                            raw_label: rawLabel,
                            required,
                            kind,
                            name,
                            input_type: inputType,
                            options,
                        };
                    }).filter((field) => field.label);
                    return {
                        title: document.title,
                        page_text: normalize(document.body ? document.body.innerText : ''),
                        fields: fieldData,
                    };
                }"""
            )
        finally:
            browser.close()


def _question_specs(fields: list[dict], generated_field_names: set[str]) -> list[dict]:
    specs = []
    for field in fields:
        if field["field_name"] not in generated_field_names:
            continue
        field_type = field["kind"]
        if field["kind"] == "checkbox":
            field_type = "multi_value_multi_select"
        elif field["kind"] in {"radio", "select"}:
            field_type = "multi_value_single_select"
        spec = {
            "field_name": field["field_name"],
            "label": field["label"],
            "description": "",
            "required": field["required"],
            "type": field_type,
        }
        if field["kind"] in {"checkbox", "radio", "select"}:
            options = [str(option).strip() for option in field.get("options") or [] if str(option).strip()]
            if options:
                spec["options"] = options
        specs.append(spec)
    return specs


def _write_unknown_questions(out_dir: Path, unknown_questions: list[dict]) -> None:
    path = role_submit_path(out_dir, AUTOFILL_UNKNOWN_QUESTIONS_JSON)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "questions": unknown_questions,
    }
    path.write_text(json_dumps_pretty(payload) + "\n", encoding="utf-8")


def _find_lever_url(meta: dict, out_dir: Path) -> str:
    """Return the Lever job URL from meta fields or the jobs database.

    The pipeline meta may store a non-Lever discovery source (e.g. LinkedIn)
    when the job was discovered via an aggregator.  Fall back to the database
    which tracks the resolved board URL separately.
    """
    for key in ("jd_source_resolved", "jd_source", "board_url"):
        url = str(meta.get(key) or "")
        if url and "lever.co" in url:
            return url.split("?")[0]

    import sqlite3

    db_path = PROJECT_ROOT / "jobs.db"
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            for col in ("url", "board_url"):
                row = conn.execute(
                    f"SELECT {col} FROM jobs WHERE output_dir = ? AND {col} LIKE '%lever.co%' LIMIT 1",
                    (str(out_dir),),
                ).fetchone()
                if row and row[0]:
                    conn.close()
                    return row[0].split("?")[0]
            conn.close()
        except sqlite3.Error:
            pass

    raise ValueError(
        f"Cannot find a Lever URL for {out_dir}. The pipeline meta and jobs database do not contain a lever.co URL."
    )


def _build_payload(out_dir: Path, provider: str | None = None) -> dict:
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)
    profile = parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    application_url = _lever_application_url(_find_lever_url(meta, out_dir))
    inspection = _inspect_lever_form(
        application_url,
        headless=True,
        cache_path=role_submit_path(out_dir, APPLICATION_PAGE_HTML),
        out_dir=out_dir,
    )

    raw_fields = inspection["fields"]
    counts: dict[str, int] = {}
    fields: list[dict] = []
    for raw_field in raw_fields:
        if raw_field.get("kind") == "awli":
            continue
        slug = slugify_label(raw_field["label"])
        counts[slug] = counts.get(slug, 0) + 1
        field_name = slug if counts[slug] == 1 else f"{slug}_{counts[slug]}"
        fields.append({**raw_field, "field_name": field_name})
    pending_user_input = _pending_user_input_fields(fields)
    if pending_user_input:
        pending_path = write_pending_user_input(
            out_dir,
            board="lever",
            questions=pending_user_input,
        )
        labels = ", ".join(question["label"] for question in pending_user_input)
        raise ValueError(
            f"Lever submit requires explicit user input before submission for: {labels}. See {pending_path}"
        )
    clear_pending_user_input(out_dir)

    answer_generation_candidates: list[dict] = []
    unknown_questions: list[dict] = []

    for field in fields:
        question_category = classify_question(field.get("label", ""))
        shared_policy = resolve_shared_question_policy(
            field.get("label"),
            application_profile,
            company_name=(meta or {}).get("company_proper") or (meta or {}).get("company"),
            job_url=(meta or {}).get("jd_source"),
            source_url=(meta or {}).get("source_url"),
            source_hint=(meta or {}).get("source"),
        )
        defer_positive_fit_textarea = _should_defer_positive_fit_textarea(
            field,
            category=question_category,
            policy=shared_policy,
        )
        if (
            field["kind"] in {"text", "textarea", "radio", "checkbox", "select"}
            and field.get("name")
            not in {
                "name",
                "email",
                "phone",
                "location",
                "org",
                "comments",
                "urls[LinkedIn]",
                "urls[Twitter]",
                "urls[GitHub]",
                "urls[Portfolio]",
                "urls[Other]",
            }
            and not _label_matches(
                field,
                "gender",
                "race",
                "veteran",
                "cover letter",
                "anything else",
                "additional information",
                "pronoun",
                "pronouns",
                "sexual orientation",
                "disability",
                "age group",
                "age range",
                "age:",
            )
            and not _is_location_application_selector(field)
            and _acknowledgment_checkbox_option(field) is None
            and (question_category is None or question_category == "ai_captcha" or defer_positive_fit_textarea)
            and not build_onsite_start_location_answer(field.get("label"), application_profile)
        ):
            answer_generation_candidates.append(field)

    try:
        generated_answers = generate_application_answers(
            out_dir=out_dir,
            meta=meta,
            question_specs=_question_specs(fields, {field["field_name"] for field in answer_generation_candidates}),
            provider=provider,
        )
    except GeneratedAnswerBlockersError as exc:
        pending_path = write_pending_user_input_for_unconfirmed_fields(
            out_dir,
            board="lever",
            fields=exc.blockers,
        )
        labels = ", ".join(
            str(blocker.get("label") or blocker.get("field_name") or "").strip() for blocker in exc.blockers
        )
        raise ValueError(
            f"Lever submit requires review for generated-answer regressions: {labels}. See {pending_path}"
        ) from exc

    steps: list[dict] = []
    for field in fields:
        step = _infer_step(
            field,
            meta=meta,
            profile=profile,
            application_profile=application_profile,
            out_dir=out_dir,
            generated_answers=generated_answers,
        )
        if step:
            steps.append(step)
            continue
        status = "unknown_required" if field["required"] else "unknown_optional"
        unknown_questions.append(
            {
                "field_name": field["field_name"],
                "label": field["label"],
                "kind": field["kind"],
                "required": field["required"],
                "name": field.get("name", ""),
                "options": field.get("options", []),
                "status": status,
                **infer_unknown_question_blocker_metadata(
                    field_name=field["field_name"],
                    label=field["label"],
                    application_profile=application_profile,
                    profile=profile,
                ),
            }
        )

    _write_unknown_questions(out_dir, unknown_questions)
    missing_required = [question for question in unknown_questions if question["required"]]
    if missing_required:
        labels = ", ".join(question["label"] for question in missing_required)
        raise ValueError(
            f"Autofill payload is missing answers for required Lever fields: {labels}. "
            f"See {role_submit_path(out_dir, AUTOFILL_UNKNOWN_QUESTIONS_JSON)}"
        )

    return {
        "job_url": meta["jd_source"],
        "application_url": application_url,
        "out_dir": str(out_dir),
        "job_title": inspection["title"] or meta.get("jd_title") or "",
        "company": meta["company_proper"],
        "candidate_email": profile.email,
        "verification_code_email": application_profile.verification_code_email or profile.email,
        "mode": "review-before-submit",
        "notes": [
            "Application-profile defaults come from application_profile.md.",
            "Open-ended answers are generated from the tailored assets, research cache, and candidate context.",
            "The Lever runtime discovers fields from the rendered form instead of assuming a fixed custom-question schema.",
            "Lever submit attempts use the persistent Chrome-backed Playwright submit profile when available.",
        ],
        "artifacts": {
            "payload_path": str(role_submit_path(out_dir, AUTOFILL_PAYLOAD_JSON)),
            "report_markdown": str(role_submit_path(out_dir, AUTOFILL_REPORT_MD)),
            "report_json": str(role_submit_path(out_dir, AUTOFILL_REPORT_JSON)),
            "pre_submit_screenshot": str(role_submit_path(out_dir, AUTOFILL_PRE_SUBMIT_SCREENSHOT)),
            "page_screenshots_dir": str(role_submit_path(out_dir, AUTOFILL_PAGE_SCREENSHOTS_DIR)),
            "unknown_questions_json": str(role_submit_path(out_dir, AUTOFILL_UNKNOWN_QUESTIONS_JSON)),
            "submit_debug_html": str(role_submit_path(out_dir, AUTOFILL_SUBMIT_DEBUG_HTML)),
            "submit_debug_screenshot": str(role_submit_path(out_dir, AUTOFILL_SUBMIT_DEBUG_SCREENSHOT)),
            "application_page_html": str(role_submit_path(out_dir, APPLICATION_PAGE_HTML)),
            "application_profile": str(APPLICATION_PROFILE_PATH),
            "website_confirmation_json": str(role_submit_path(out_dir, WEBSITE_CONFIRMATION_JSON)),
            "email_confirmation_json": str(role_submit_path(out_dir, EMAIL_CONFIRMATION_JSON)),
            "notion_sync_status_json": str(role_submit_path(out_dir, NOTION_SYNC_STATUS_JSON)),
        },
        "fields": fields,
        "steps": steps,
        "unknown_questions": unknown_questions,
    }


def _write_report(payload: dict, runtime: dict | None = None) -> dict:
    from autofill_common import write_report

    return write_report(payload, board_name="lever", runtime=runtime)


def _field_group(page, step: dict):
    return page.locator("#application-form .application-question, #application-form .application-additional").nth(
        int(step["form_index"])
    )


def _set_choice_checked(locator, *, checked: bool) -> None:
    try:
        if checked:
            locator.check(force=True)
        else:
            locator.uncheck(force=True)
        return
    except Exception:
        locator.evaluate(
            """(element, shouldCheck) => {
                element.checked = shouldCheck;
                element.dispatchEvent(new Event('input', { bubbles: true }));
                element.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            checked,
        )


def _is_cover_letter_step(step: dict) -> bool:
    if step.get("source") in {"cover_letter_text.txt", "existing_cover_letter_asset"}:
        return True
    if "cover_letter" in str(step.get("field_name") or ""):
        return True
    return "cover letter" in normalize_text(step.get("label"))


def _read_control_value(locator) -> str:
    try:
        return locator.input_value()
    except Exception:
        return str(
            locator.evaluate(
                """(element) => {
                    if (typeof element.value === 'string') {
                        return element.value;
                    }
                    return element.textContent || '';
                }"""
            )
            or ""
        )


def _confirm_cover_letter_text(locator, expected: str) -> bool:
    actual = _normalize_spacing(_read_control_value(locator))
    return actual == _normalize_spacing(expected)


def _confirm_file_attached(locator) -> bool:
    try:
        return bool(locator.evaluate("(element) => !!(element.files && element.files.length > 0)"))
    except Exception:
        return False


def _mark_visible_self_id_unconfirmed(step: dict) -> None:
    step["filled"] = False
    step["status"] = "planned"
    step["note"] = "Filled the planned value but could not confirm it remained visible on the live form."


def _step_requires_live_confirmation(step: dict, *, is_location_autocomplete: bool = False) -> bool:
    if is_visible_confirmation_blocker(step):
        return True
    if is_location_autocomplete:
        return True
    return step.get("kind") in {"select", "radio", "checkbox"}


def _confirm_visible_self_id_step(group, step: dict, *, locator=None) -> bool:
    raw_expected = step.get("value")
    if isinstance(raw_expected, list):
        expected_values = [normalize_text(str(value)) for value in raw_expected if str(value).strip()]
        if not expected_values:
            return False
    else:
        expected = str(raw_expected or "")
        if not expected:
            return False
        expected_values = [normalize_text(expected)]
    if step["kind"] in {"text", "textarea"} and locator is not None:
        expected = str(raw_expected or "")
        if step.get("profile_field") == "location":
            return _location_matches_expected(_read_control_value(locator), expected)
        return _normalize_spacing(_read_control_value(locator)) == _normalize_spacing(expected)
    if step["kind"] == "select":
        expected = str(raw_expected or "")
        if not expected:
            return False
        select = group.locator("select").first
        if not select.count():
            return False
        try:
            selected = select.locator("option:checked").first.inner_text().strip()
        except Exception:
            selected = _read_control_value(select)
        return _normalize_spacing(selected) == _normalize_spacing(expected)
    try:
        return bool(
            group.evaluate(
                """(node, expectedValues) => {
                    const normalize = (value) => (value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
                    const selectedTexts = [];
                    const push = (value) => { if (value) selectedTexts.push(value); };
                    for (const input of node.querySelectorAll("input[type='radio']:checked, input[type='checkbox']:checked")) {
                        const labels = Array.from(input.labels || []).map(label => label.textContent || "");
                        if (labels.length) {
                            labels.forEach(push);
                        } else if (input.closest("label")) {
                            push(input.closest("label").textContent || "");
                        }
                    }
                    for (const active of node.querySelectorAll("[role='radio'][aria-checked='true'], [role='checkbox'][aria-checked='true']")) {
                        push(active.textContent || "");
                    }
                    const normalizedSelections = selectedTexts.map(normalize).filter(Boolean);
                    return expectedValues.every((expected) =>
                        normalizedSelections.some((selection) => selection.includes(expected) || expected.includes(selection))
                    );
                }""",
                expected_values,
            )
        )
    except Exception:
        return False


def _confirm_visible_self_id_step_with_wait(
    page,
    group,
    step: dict,
    *,
    locator=None,
    attempts: int = 1,
    delay_ms: int = 0,
) -> bool:
    attempts = max(1, attempts)
    for attempt in range(attempts):
        if _confirm_visible_self_id_step(group, step, locator=locator):
            return True
        if attempt + 1 >= attempts or page is None or delay_ms <= 0:
            continue
        try:
            page.wait_for_timeout(delay_ms)
        except Exception:
            pass
    return False


def _location_search_results(page, query: str) -> list[dict]:
    normalized_query = _normalize_spacing(query)
    if page is None or not normalized_query:
        return []
    try:
        results = page.evaluate(
            """async (query) => {
                const hcaptchaResponse = document.querySelector('#hcaptchaResponseInput')?.value || '';
                const response = await fetch(
                    '/searchLocations?text=' +
                    encodeURIComponent(query) +
                    '&hcaptchaResponse=' +
                    encodeURIComponent(hcaptchaResponse)
                );
                if (!response.ok) {
                    return [];
                }
                return await response.json();
            }""",
            normalized_query,
        )
    except Exception:
        return []
    if not isinstance(results, list):
        return []
    return [result for result in results if isinstance(result, dict) and str(result.get("name") or "").strip()]


def _best_location_search_result(results: list[dict], expected: str) -> dict | None:
    for result in results:
        if _location_matches_expected(str(result.get("name") or ""), expected):
            return result
    return None


def _apply_location_search_result(page, result: dict) -> bool:
    name = _normalize_spacing(result.get("name"))
    if page is None or not name:
        return False
    try:
        return bool(
            page.evaluate(
                """(result) => {
                    const input = document.querySelector('input.location-input, input[data-qa="location-input"]');
                    const hidden = document.querySelector('#selected-location');
                    if (!input || !hidden || !result?.name) {
                        return false;
                    }
                    input.value = result.name;
                    hidden.value = JSON.stringify(result);
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    hidden.dispatchEvent(new Event('input', { bubbles: true }));
                    hidden.dispatchEvent(new Event('change', { bubbles: true }));
                    if (window.$) {
                        window.$('.dropdown-results').empty();
                        window.$('.dropdown-no-results, .dropdown-loading-results, .dropdown-container').css({
                            display: 'none',
                        });
                    } else {
                        const dropdownResults = document.querySelector('.dropdown-results');
                        if (dropdownResults) {
                            dropdownResults.innerHTML = '';
                        }
                        for (const selector of ['.dropdown-no-results', '.dropdown-loading-results', '.dropdown-container']) {
                            const element = document.querySelector(selector);
                            if (element) {
                                element.style.display = 'none';
                            }
                        }
                    }
                    return true;
                }""",
                result,
            )
        )
    except Exception:
        return False


def _select_location_autocomplete_option(group, expected: str) -> bool:
    options = group.locator(".dropdown-location, .dropdown-results div, .dropdown-results li")
    try:
        option_count = options.count()
    except Exception:
        return False
    for index in range(option_count):
        option = options.nth(index)
        try:
            option_text = option.inner_text().strip()
        except Exception:
            continue
        if not _location_matches_expected(option_text, expected):
            continue
        try:
            option.dispatch_event("mousedown")
        except Exception:
            option.click()
        return True
    return False


def _fill_step(page, step: dict) -> None:
    group = _field_group(page, step)
    if step["kind"] == "text":
        # Location autocomplete: type value then select from dropdown
        location_input = group.locator("input.location-input, input[data-qa='location-input']")
        if location_input.count():
            loc = location_input.first
            human_fill(loc, step["value"])
            try:
                dropdown = group.locator(".dropdown-location, .dropdown-results div, .dropdown-results li").first
                dropdown.wait_for(state="visible", timeout=3000)
                _select_location_autocomplete_option(group, str(step["value"] or ""))
            except Exception:
                pass  # dropdown didn't appear; typed value may still be accepted
            confirmed = _confirm_visible_self_id_step_with_wait(
                page,
                group,
                step,
                locator=loc,
                attempts=3,
                delay_ms=250,
            )
            if not confirmed and page is not None:
                fallback_result = _best_location_search_result(
                    _location_search_results(page, str(step["value"] or "")),
                    str(step["value"] or ""),
                )
                if fallback_result and _apply_location_search_result(page, fallback_result):
                    confirmed = _confirm_visible_self_id_step_with_wait(
                        page,
                        group,
                        step,
                        locator=loc,
                        attempts=3,
                        delay_ms=250,
                    )
            if _step_requires_live_confirmation(step, is_location_autocomplete=True) and not confirmed:
                _mark_visible_self_id_unconfirmed(step)
                return
            step["filled"] = True
            return
        locator = group.locator(
            "input:not([type='file']):not([type='radio']):not([type='checkbox']):not([type='hidden']), textarea"
        ).first
        human_fill(locator, step["value"])
        if _is_cover_letter_step(step) and not _confirm_cover_letter_text(locator, str(step["value"] or "")):
            raise RuntimeError(f"Could not confirm Lever cover letter text for {step['label']}")
        if _step_requires_live_confirmation(step) and not _confirm_visible_self_id_step(group, step, locator=locator):
            _mark_visible_self_id_unconfirmed(step)
            return
        step["filled"] = True
        return
    if step["kind"] == "textarea":
        locator = group.locator("textarea").first
        value = str(step["value"] or "")
        if len(value) > 400:
            locator.fill(value)
        else:
            human_fill(locator, value)
        if _is_cover_letter_step(step) and not _confirm_cover_letter_text(locator, value):
            raise RuntimeError(f"Could not confirm Lever cover letter text for {step['label']}")
        if _step_requires_live_confirmation(step) and not _confirm_visible_self_id_step(group, step, locator=locator):
            _mark_visible_self_id_unconfirmed(step)
            return
        step["filled"] = True
        return
    if step["kind"] == "file":
        locator = group.locator("input[type='file']").first
        locator.set_input_files(step["file_path"])
        if _is_cover_letter_step(step) and not _confirm_file_attached(locator):
            raise RuntimeError(f"Could not confirm Lever cover letter upload for {step['label']}")
        step["filled"] = True
        return
    if step["kind"] == "select":
        group.locator("select").first.select_option(label=step["value"])
        if _step_requires_live_confirmation(step) and not _confirm_visible_self_id_step(group, step):
            _mark_visible_self_id_unconfirmed(step)
            return
        step["filled"] = True
        return
    if step["kind"] == "radio":
        radio_selector = f"input[type='radio'][value={json.dumps(step['value'])}]"
        radios = group.locator(radio_selector)
        if radios.count():
            _set_choice_checked(radios.first, checked=True)
        else:
            group.locator("label", has_text=step["value"]).first.click(force=True)
        if _step_requires_live_confirmation(step) and not _confirm_visible_self_id_step(group, step):
            _mark_visible_self_id_unconfirmed(step)
            return
        step["filled"] = True
        return
    if step["kind"] == "checkbox":
        desired_checked = bool(step.get("checked", True))
        if step.get("value"):
            values = step["value"] if isinstance(step["value"], list) else [step["value"]]
            for value in values:
                checkbox_selector = f"input[type='checkbox'][value={json.dumps(value)}]"
                checkboxes = group.locator(checkbox_selector)
                if checkboxes.count():
                    _set_choice_checked(checkboxes.first, checked=desired_checked)
                elif desired_checked:
                    group.locator("label", has_text=value).first.click(force=True)
        else:
            locator = group.locator("input[type='checkbox']").first
            _set_choice_checked(locator, checked=desired_checked)
        if _step_requires_live_confirmation(step) and not _confirm_visible_self_id_step(group, step):
            _mark_visible_self_id_unconfirmed(step)
            return
        step["filled"] = True
        return
    raise RuntimeError(f"Unsupported Lever step kind: {step['kind']}")


def _page_snapshot(page) -> dict:
    return page_snapshot(page, form_selector=FORM_READY_SELECTOR, captcha_type="hcaptcha")


def _classify_submit_state(snapshot: dict) -> dict[str, object]:
    page_text = str(snapshot.get("page_text") or "")
    if any(pattern.search(page_text) for pattern in SUBMIT_CONFIRM_PATTERNS):
        return {"status": "confirmed", "reason": "text"}
    errors = list(snapshot.get("errors") or [])
    page_level_errors = [
        pattern.search(page_text).group(0) for pattern in VALIDATION_ERROR_PATTERNS if pattern.search(page_text)
    ]
    combined_errors = list(dict.fromkeys(errors + page_level_errors))
    invalid_fields = list(snapshot.get("invalid_fields") or [])
    if snapshot.get("hcaptcha_challenge_active") and snapshot.get("form_visible") and not invalid_fields:
        return {"status": "captcha_required"}
    if combined_errors:
        return {
            "status": "validation_error",
            "errors": combined_errors,
            "invalid_fields": invalid_fields,
        }
    if snapshot.get("hcaptcha_challenge_active") and snapshot.get("form_visible"):
        return {"status": "captcha_required"}
    return {"status": "pending"}


def _capture_full_page(page, path: Path, *, preferred_selectors: tuple[str, ...] = ()) -> None:
    capture_full_page(page, path, preferred_selectors=preferred_selectors)


def _click_submit_button(page) -> bool:
    # Lever has a specific #btn-submit ID; try it first
    btn = page.locator("#btn-submit")
    if btn.count():
        try:
            if btn.first.is_visible() and btn.first.is_enabled():
                btn.first.click()
                return True
        except Exception:
            pass
    return click_submit_button(page, button_names=SUBMIT_BUTTON_NAMES)


def _wait_for_pre_submit_manual_challenge(page, *, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _click_submit_button(page):
            return True
        page.wait_for_timeout(1000)
    return False


def _confirmed_outcome_from_email(snapshot: dict | None, email_confirmation: dict) -> dict[str, object]:
    snapshot = dict(snapshot or {})
    snapshot.setdefault("page_text", "(matched application confirmation email while browser confirmation was pending)")
    return {
        "status": "confirmed",
        "reason": "email_confirmation",
        "snapshot": snapshot,
        "email_confirmation": email_confirmation,
    }


def _wait_for_lever_form(page, payload) -> None:
    """Navigate to Lever's application URL and wait for the form."""
    application_url = payload.get("application_url", payload["job_url"])
    current = page.url.rstrip("/")
    target = application_url.rstrip("/")
    if current != target:
        page.goto(application_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_selector(FORM_READY_SELECTOR, timeout=25000)


def main() -> int:
    from autofill_pipeline import autofill_main, run_browser_pipeline

    return autofill_main(
        board_name="lever",
        build_payload_fn=_build_payload,
        run_browser_fn=lambda pp, headless, submit: run_browser_pipeline(
            pp,
            headless=headless,
            submit=submit,
            board_name="lever",
            retry_unconfirmed_visible_self_id_once=True,
            form_ready_fn=lambda page: _wait_for_lever_form(page, json.loads(pp.read_text(encoding="utf-8"))),
            fill_step_fn=_fill_step,
            page_snapshot_fn=_page_snapshot,
            classify_state_fn=_classify_submit_state,
            click_submit_fn=_click_submit_button,
            capture_fn=lambda page, path: _capture_full_page(page, path),
            pre_submit_hook=lambda page: _wait_for_pre_submit_manual_challenge(
                page,
                timeout_seconds=int(
                    os.environ.get("JOB_ASSETS_CAPTCHA_WAIT_SECONDS", str(DEFAULT_CAPTCHA_WAIT_SECONDS))
                ),
            ),
            confirmed_outcome_from_email_fn=_confirmed_outcome_from_email,
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
