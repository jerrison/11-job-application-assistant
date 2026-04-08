#!/usr/bin/env python3
"""Generate and optionally run a Gem application autofill flow."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_submit_common import (
    APPLICATION_PROFILE_PATH,
    PROJECT_ROOT,
    _authorized_country_codes,
    build_company_specific_answer,
    build_onsite_start_location_answer,
    clear_pending_user_input,
    extract_cover_letter_paragraphs,
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
    resolve_how_did_you_hear_candidates,
    resolve_how_did_you_hear_option_candidates,
    resolve_shared_question_policy,
    slugify_label,
    write_pending_user_input,
)
from autofill_common import (
    board_file_constants,
    capture_full_page,
    click_submit_button,
    infer_unknown_question_blocker_metadata,
    is_visible_self_id_blocker,
    label_matches,
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
from output_layout import migrate_role_output_layout, role_submit_path
from project_env import load_project_env
from question_classifier import classify_question

_BOARD_CONSTANTS = board_file_constants("gem")
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
UNKNOWN_STATUSES = {"unknown_required", "unknown_optional"}
SUBMIT_BUTTON_NAMES = ("Apply without saving", "Apply and save", "Submit application", "Submit", "Apply")
PREFERRED_CAPTURE_SELECTORS = (".formContainer-21",)
SUBMIT_CONFIRM_PATTERNS = (
    re.compile(r"\bthank(?:s| you)\s+for\s+applying\b", re.I),
    re.compile(r"\bapplication (?:has been )?(?:received|submitted)\b", re.I),
    re.compile(r"\bwe(?:'|’)ve received your application\b", re.I),
    re.compile(r"\bwe have received your application\b", re.I),
    re.compile(r"\bwe will be in touch\b", re.I),
)
VALIDATION_ERROR_PATTERNS = (
    re.compile(r"\bthis field is required\b", re.I),
    re.compile(r"\bplease (?:enter|select|complete)\b", re.I),
    re.compile(r"\binvalid\b", re.I),
    re.compile(r"\berror\b", re.I),
)

_US_STATES = {
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

_AUTHORIZED_COUNTRY_OPTION_CANDIDATES = {
    "united_states": ("United States", "United States of America", "US", "U.S.", "USA"),
    "canada": ("Canada",),
    "united_kingdom": ("United Kingdom", "UK", "U.K.", "Great Britain", "Britain"),
}
DEFAULT_CAPTCHA_WAIT_SECONDS = 300


load_project_env()


def _maybe_reexec_with_uv() -> None:
    if os.environ.get("JOB_ASSETS_GEM_BOOTSTRAPPED") == "1":
        return
    if not shutil.which("uv"):
        return
    env = os.environ.copy()
    env["JOB_ASSETS_GEM_BOOTSTRAPPED"] = "1"
    cmd = ["uv", "run", "--project", str(PROJECT_ROOT), "python", __file__, *sys.argv[1:]]
    raise SystemExit(subprocess.call(cmd, cwd=PROJECT_ROOT, env=env))


def _normalize_spacing(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _utm_source_label(job_url: str) -> str | None:
    parsed = urllib.parse.urlparse(job_url)
    source = urllib.parse.parse_qs(parsed.query).get("utm_source", [""])[0].strip()
    if not source:
        return None
    return source.replace("-", " ").replace("_", " ").title()


def _is_location_field(field: dict) -> bool:
    label = normalize_text(field.get("label"))
    if label in {
        "location",
        "current location",
        "city",
        "city of residence",
        "residence city",
    }:
        return True
    return label in {
        "where are you located",
        "where are you currently located",
    }


def _is_website_password_field(field: dict) -> bool:
    return label_matches(field, "portfolio", "website") and label_matches(field, "password")


def _is_website_field(field: dict) -> bool:
    return label_matches(field, "portfolio", "website") and not label_matches(field, "password", "cover letter")


def _pick_hear_about_option(options: list[str], company: str) -> str | None:
    """Pick the best 'how did you hear' radio option using priority order.

    Priority: company careers/website > blog > LinkedIn > job site > other.
    """
    lower_options = [o.lower() for o in options]
    company_lower = company.lower()
    # Priority 1: company-specific website / careers page
    for i, lo in enumerate(lower_options):
        if company_lower and company_lower in lo and any(w in lo for w in ("career", "website", "site")):
            return options[i]
    # Priority 2: generic website / careers page
    for i, lo in enumerate(lower_options):
        if any(w in lo for w in ("career", "corporate website", "company website")):
            return options[i]
    # Priority 3: blog
    for i, lo in enumerate(lower_options):
        if "blog" in lo:
            return options[i]
    # Priority 4: LinkedIn / social
    for i, lo in enumerate(lower_options):
        if "linkedin" in lo or "social" in lo:
            return options[i]
    # Priority 5: job site / job board
    for i, lo in enumerate(lower_options):
        if "job site" in lo or "job board" in lo:
            return options[i]
    # Priority 6: "Other" as last resort
    for i, lo in enumerate(lower_options):
        if lo == "other":
            return options[i]
    return options[0] if options else None


def _choice_kind(kind: str) -> str:
    if kind in {"radio", "select"}:
        return kind
    return "radio"


def _choice_step(base: dict, *, kind: str, option: str, source: str) -> dict:
    return {
        **base,
        "kind": _choice_kind(kind),
        "option": option,
        "source": source,
    }


def _candidate_state_variants(application_profile) -> list[str]:
    location = str(getattr(application_profile, "location", "") or "")
    parts = [part.strip() for part in location.split(",") if part.strip()]
    candidates: list[str] = []
    if len(parts) >= 2:
        state = parts[-1].upper()
        candidates.append(state)
        full_name = _US_STATES.get(state)
        if full_name:
            candidates.append(full_name)
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = normalize_text(candidate)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(candidate)
    return deduped


def _candidate_location_variants(application_profile) -> list[str]:
    location = str(getattr(application_profile, "location", "") or "")
    parts = [part.strip() for part in location.split(",") if part.strip()]
    candidates: list[str] = []
    if location:
        candidates.append(location)
    if parts:
        candidates.extend(parts)
    candidates.extend(_candidate_state_variants(application_profile))
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = normalize_text(candidate)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(candidate)
    return deduped


def _match_candidate_state_option(field: dict, application_profile) -> str | None:
    for candidate in _candidate_state_variants(application_profile):
        matched = select_option(field.get("options"), candidate)
        if matched:
            return matched
    return None


def _match_candidate_location_option(field: dict, application_profile) -> str | None:
    for candidate in _candidate_location_variants(application_profile):
        matched = select_option(field.get("options"), candidate)
        if matched:
            return matched
    return None


def _match_authorized_country_option(field: dict, application_profile) -> str | None:
    for code in _authorized_country_codes(application_profile):
        for candidate in _AUTHORIZED_COUNTRY_OPTION_CANDIDATES.get(code, ()):
            matched = select_option(field.get("options"), candidate)
            if matched:
                return matched
    return None


def _supports_boolean_choice_option(options: list[str] | None) -> bool:
    values = list(options or [])
    if not values:
        return True
    return any(select_option(values, candidate) is not None for candidate in ("Yes", "No"))


def _step_from_classifier(
    category: str | None,
    field: dict,
    *,
    base: dict,
    kind: str,
    meta: dict,
    application_profile,
    out_dir: Path,
) -> dict | None:
    """Map a classifier category to a step dict, or return None to fall through."""
    if category is None:
        return None

    if category == "education" and kind in {"text", "textarea"}:
        education_text = format_education_from_profile(application_profile)
        if education_text:
            return {**base, "kind": kind, "value": education_text, "source": "application_profile.md"}
        return None

    policy = resolve_shared_question_policy(field.get("label", ""), application_profile)
    if policy is not None and policy.boolean_value is not None:
        if kind in {"radio", "select", "checkbox", "unknown"}:
            if policy.category == "work_authorization" and not _supports_boolean_choice_option(field.get("options")):
                country_option = _match_authorized_country_option(field, application_profile)
                if country_option is not None:
                    return _choice_step(
                        base,
                        kind=kind,
                        option=country_option,
                        source=policy.source,
                    )
                return None
            desired_option = "Yes" if policy.boolean_value else "No"
            return _choice_step(
                base,
                kind=kind,
                option=select_option(field.get("options"), desired_option) or desired_option,
                source=policy.source,
            )
        if kind in {"text", "textarea"} and policy.text_value is not None:
            return {
                **base,
                "kind": kind,
                "value": policy.text_value,
                "source": policy.source,
            }

    if category == "culture_careers_optin":
        if kind in {"radio", "select", "checkbox"}:
            option = select_option(field.get("options"), "No")
            if option is None:
                option = "No"
            return _choice_step(base, kind=kind, option=option, source="deterministic_override")
        return {**base, "kind": kind, "value": "No", "source": "deterministic_override"}

    if category == "salary_comfort" and kind in {"radio", "select", "checkbox", "unknown"}:
        return _choice_step(
            base,
            kind=kind,
            option="Yes" if getattr(application_profile, "comfortable_with_posted_salary", True) else "No",
            source="application_profile.md",
        )

    if category == "office_attendance" and kind in {"radio", "select", "checkbox", "unknown"}:
        matched = select_location_positive_fit_option(
            field.get("options"),
            application_profile=application_profile,
        )
        if matched is not None:
            return _choice_step(
                base,
                kind=kind,
                option=matched,
                source="shared_positive_fit_policy",
            )
        can_attend_office = (
            application_profile.lives_in_job_location and application_profile.comfortable_working_on_site
        )
        return _choice_step(
            base,
            kind=kind,
            option="Yes" if can_attend_office else "No",
            source="application_profile.md",
        )

    if category == "minimum_experience" and kind in {"radio", "select", "checkbox", "unknown"}:
        return _choice_step(base, kind=kind, option="Yes", source="application_profile.md")

    if category == "product_usage" and kind in {"radio", "select", "checkbox", "unknown"}:
        return _choice_step(base, kind=kind, option="Yes", source="deterministic_override")

    if category == "experience_confirmation" and kind in {"radio", "select", "checkbox", "unknown"}:
        return _choice_step(base, kind=kind, option="Yes", source="deterministic_override")

    return None


def _infer_step(
    field: dict, *, meta: dict, profile, application_profile, out_dir: Path, generated_answers: dict[str, str]
) -> dict | None:
    label = field["label"]
    clean_label = label.replace(" *", "").strip()
    field_name = field["field_name"]
    kind = field["kind"]
    base = {
        "field_name": field_name,
        "label": clean_label,
        "required": field["required"],
        "form_index": field["index"],
        "group_scope": field.get("group_scope", "main"),
    }

    if kind == "file":
        if label_matches(field, "resume"):
            return {
                **base,
                "kind": "file",
                "file_path": str(find_resume_file(out_dir)),
                "source": "existing_resume_asset",
            }
        if label_matches(field, "cover letter"):
            return {
                **base,
                "kind": "file",
                "file_path": str(find_cover_letter_file(out_dir)),
                "source": "existing_cover_letter_asset",
            }
        return None

    if label_matches(field, "first name"):
        return {**base, "kind": "text", "value": profile.first_name, "source": "master_resume.md"}
    if label_matches(field, "last name"):
        return {**base, "kind": "text", "value": profile.last_name, "source": "master_resume.md"}
    if label_matches(field, "email"):
        return {**base, "kind": "text", "value": profile.email, "source": "master_resume.md"}
    if label_matches(field, "linkedin"):
        return {
            **base,
            "kind": "text",
            "value": application_profile.linkedin or profile.linkedin or "",
            "source": "application_profile.md" if application_profile.linkedin else "master_resume.md",
        }
    if label_matches(field, "github"):
        return {
            **base,
            "kind": "text",
            "value": application_profile.github or "",
            "source": "application_profile.md" if application_profile.github else "not_provided",
        }
    if _is_website_field(field):
        return {
            **base,
            "kind": "text",
            "value": application_profile.website or profile.website or "",
            "source": "application_profile.md" if application_profile.website else "master_resume.md",
        }
    onsite_answer = build_onsite_start_location_answer(field.get("label"), application_profile)
    if kind in {"text", "textarea"} and onsite_answer:
        return {
            **base,
            "kind": "textarea" if kind == "textarea" else "text",
            "value": onsite_answer,
            "source": "application_profile.md",
        }
    if _is_location_field(field) and kind in {"text", "textarea"}:
        return {**base, "kind": "text", "value": application_profile.location, "source": "application_profile.md"}
    if label_matches(field, "cover letter"):
        return {
            **base,
            "kind": "textarea" if kind == "textarea" else "text",
            "value": find_cover_letter_text(out_dir),
            "source": "cover_letter_text.txt",
        }

    # --- Unified question classifier ---
    category = classify_question(field.get("label", ""))
    step_from_classifier = _step_from_classifier(
        category,
        field,
        base=base,
        kind=kind,
        meta=meta,
        application_profile=application_profile,
        out_dir=out_dir,
    )
    if step_from_classifier is not None:
        return step_from_classifier

    if kind in {"radio", "select", "checkbox"} and label_matches(
        field,
        "how did you hear",
        "where did you hear",
        "how did you learn",
        "how did you first learn",
        "hear about this role",
    ):
        preferred_candidates, heard_source = resolve_how_did_you_hear_option_candidates(
            application_profile,
            field.get("options", []),
            company_name=(meta or {}).get("company_proper") or (meta or {}).get("company"),
            job_url=(meta or {}).get("jd_source"),
            source_url=(meta or {}).get("source_url"),
            source_hint=(meta or {}).get("source"),
        )
        preferred = select_option(field.get("options"), preferred_candidates[0] if preferred_candidates else None)
        if preferred is None:
            for candidate in preferred_candidates:
                preferred = select_option(field.get("options"), candidate)
                if preferred is not None:
                    break
        if preferred:
            return _choice_step(base, kind=kind, option=preferred, source=heard_source)

    if kind in {"radio", "select", "checkbox"} and label_matches(
        field,
        "which state would you be working from",
        "which state are you working from",
        "what state would you be working from",
        "what state are you working from",
    ):
        state_option = _match_candidate_state_option(field, application_profile)
        if state_option is not None:
            return _choice_step(base, kind=kind, option=state_option, source="application_profile.md")

    if kind in {"radio", "select", "checkbox"} and label_matches(
        field,
        "where are you located",
        "where are you currently located",
    ):
        location_option = _match_candidate_location_option(field, application_profile)
        if location_option is not None:
            return _choice_step(base, kind=kind, option=location_option, source="application_profile.md")

    if kind in {"radio", "select", "checkbox"}:
        if label_matches(field, "transgender"):
            option = select_profile_option(
                field.get("options"),
                str(getattr(application_profile, "transgender_status", "No") or "No").strip().title(),
                profile_field="transgender_status",
            ) or str(getattr(application_profile, "transgender_status", "No") or "No").strip().title()
            return mark_visible_self_id_step(
                _choice_step(
                    base,
                    kind=kind,
                    option=option,
                    source="application_profile.md",
                ),
                profile_field="transgender_status",
            )
        if label_matches(field, "gender"):
            desired = application_profile.gender_identity or application_profile.gender or ""
            if desired:
                profile_field = "gender_identity" if getattr(application_profile, "gender_identity", None) else "gender"
                option = select_profile_option(field.get("options"), desired, profile_field=profile_field) or desired
                return mark_visible_self_id_step(
                    _choice_step(base, kind=kind, option=option, source="application_profile.md"),
                    profile_field=profile_field,
                )
        if label_matches(field, "race or ethnicity", "race", "racial", "ethnicity", "ethnic"):
            desired = application_profile.race_or_ethnicity or ""
            if desired:
                option = select_profile_option(field.get("options"), desired, profile_field="race_or_ethnicity") or desired
                return mark_visible_self_id_step(
                    _choice_step(base, kind=kind, option=option, source="application_profile.md"),
                    profile_field="race_or_ethnicity",
                )
        if label_matches(field, "veteran"):
            desired = application_profile.veteran_status or ""
            if desired:
                option = select_profile_option(field.get("options"), desired, profile_field="veteran_status") or desired
                return mark_visible_self_id_step(
                    _choice_step(base, kind=kind, option=option, source="application_profile.md"),
                    profile_field="veteran_status",
                )
        if label_matches(field, "disability"):
            desired = application_profile.disability_status or ""
            if desired:
                option = (
                    select_profile_option(field.get("options"), desired, profile_field="disability_status") or desired
                )
                return mark_visible_self_id_step(
                    _choice_step(base, kind=kind, option=option, source="application_profile.md"),
                    profile_field="disability_status",
                )
        if label_matches(field, "sexual orientation"):
            desired = application_profile.sexual_orientation or ""
            if desired:
                option = (
                    select_profile_option(field.get("options"), desired, profile_field="sexual_orientation") or desired
                )
                return mark_visible_self_id_step(
                    _choice_step(base, kind=kind, option=option, source="application_profile.md"),
                    profile_field="sexual_orientation",
                )
        if label_matches(field, "sponsorship", "visa"):
            return _choice_step(base, kind=kind, option="No", source="application_profile.md")
        if label_matches(
            field,
            "based in",
            "commuting",
            "commute",
            "bay area",
            "relocate",
            "on site",
            "on-site",
            "onsite",
            "in person",
            "in-person",
        ):
            return _choice_step(base, kind=kind, option="Yes", source="application_profile.md")
        if label_matches(
            field,
            "authorized to work",
            "work authorization",
            "right to work",
            "legally authorized",
            "eligible to work",
            "legally eligible",
        ):
            return _choice_step(base, kind=kind, option="Yes", source="application_profile.md")
        if label_matches(field, "pronoun", "pronouns"):
            raw_pronouns = application_profile.pronouns or ""
            lp = raw_pronouns.lower().replace("/", " ").replace(",", " ")
            if "he" in lp:
                option = "He/Him"
            elif "she" in lp:
                option = "She/Her"
            elif "they" in lp:
                option = "They/Them"
            else:
                option = raw_pronouns
            return mark_visible_self_id_step(
                _choice_step(base, kind=kind, option=option, source="application_profile.md"),
                profile_field="pronouns",
            )
        if label_matches(field, "privacy", "acknowledge", "consent", "by continuing"):
            # Privacy/consent acknowledgments — select the affirmative option
            options = field.get("options", [])
            option = options[0] if options else "Continue"
            return _choice_step(base, kind=kind, option=option, source="deterministic_override")
        return None

    if kind == "unknown":
        if label_matches(
            field,
            "based in",
            "commuting",
            "commute",
            "bay area",
            "relocate",
            "on site",
            "on-site",
            "onsite",
            "in person",
            "in-person",
        ):
            return _choice_step(base, kind=kind, option="Yes", source="application_profile.md")

    if kind in {"text", "textarea"} and label_matches(
        field,
        "how did you hear",
        "how did you learn",
        "how did you first learn",
        "hear about this role",
        "share how you heard about us",
        "selected other",
        "share more details below",
    ):
        source_candidates, heard_source = resolve_how_did_you_hear_candidates(
            application_profile,
            company_name=(meta or {}).get("company_proper") or (meta or {}).get("company"),
            job_url=(meta or {}).get("jd_source"),
            source_url=(meta or {}).get("source_url"),
            source_hint=(meta or {}).get("source"),
        )
        source_label = source_candidates[0] if source_candidates else (
            application_profile.how_did_you_hear or _utm_source_label(meta["jd_source"]) or "Corporate website"
        )
        if (
            not getattr(application_profile, "how_did_you_hear", None)
            and source_label == "Corporate website"
            and (utm_source_label := _utm_source_label(str((meta or {}).get("jd_source") or "")))
        ):
            source_label = utm_source_label
            heard_source = "job_url.utm_source"
        return {
            **base,
            "kind": kind,
            "value": source_label,
            "source": heard_source if source_candidates else (
                "application_profile.md" if application_profile.how_did_you_hear else "job_url.utm_source"
            ),
        }

    if field_name in generated_answers:
        return {**base, "kind": kind, "value": generated_answers[field_name], "source": "generated_application_answer"}
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
                "reason": reason,
            }
        )
    return pending


# Gem uses CSS-module class names like "form-33", "form-42", etc.
# Match dynamically instead of hardcoding a single class.
_GEM_FORM_JS = """
(() => {
    const candidates = document.querySelectorAll('[class*="form-"]');
    for (const el of candidates) {
        if (/\\bform-\\d+\\b/.test(el.className) && Array.from(el.children).some(c => c.innerText && c.innerText.includes('First name'))) {
            return el;
        }
    }
    return null;
})()
""".strip()

_GEM_FORM_CSS_JS = """
(() => {
    const candidates = document.querySelectorAll('[class*="form-"]');
    for (const el of candidates) {
        const match = el.className.match(/\\b(form-\\d+)\\b/);
        if (match && Array.from(el.children).some(c => c.innerText && c.innerText.includes('First name'))) {
            return '.' + match[1];
        }
    }
    return null;
})()
""".strip()


def _gem_job_closed_reason(url: str, page_text: str) -> str | None:
    normalized = normalize_text(page_text).casefold()
    if "job not found" in normalized and (
        "out of date" in normalized or "removed" in normalized or "view all open jobs" in normalized
    ):
        return f"job_closed: Gem showed a removed or missing posting shell at {url}"
    if "no longer accepting applications" in normalized:
        return f"job_closed: Gem reported that the posting is no longer accepting applications at {url}"
    if "this job is no longer available" in normalized:
        return f"job_closed: Gem reported that the posting is no longer available at {url}"
    return None


def _write_gem_job_unavailable_artifact(
    out_dir: Path,
    *,
    application_url: str,
    source_url: str | None,
    message: str,
) -> Path:
    path = role_submit_path(out_dir, "job_unavailable.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "job_closed",
        "board": "gem",
        "application_url": application_url,
        "source_url": source_url,
        "message": message,
        "recorded_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }
    path.write_text(json_dumps_pretty(payload) + "\n", encoding="utf-8")
    return path


def _inspect_gem_form(job_url: str, *, headless: bool, cache_path: Path | None = None) -> dict:
    try:
        from playwright.sync_api import (
            TimeoutError as PlaywrightTimeoutError,
        )
        from playwright.sync_api import (
            sync_playwright,
        )
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
            purpose="Gem autofill",
        )
        page = browser.new_page(viewport={"width": 1600, "height": 1200}, device_scale_factor=2)
        try:
            page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_function(
                    """() => {
                        const normalizedBody = (document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (
                            /job not found/i.test(normalizedBody)
                            && /(out of date|removed|view all open jobs)/i.test(normalizedBody)
                        ) {
                            return true;
                        }
                        if (/no longer accepting applications/i.test(normalizedBody)) {
                            return true;
                        }
                        if (/this job is no longer available/i.test(normalizedBody)) {
                            return true;
                        }
                        const candidates = document.querySelectorAll('[class*="form-"]');
                        for (const el of candidates) {
                            if (/\\bform-\\d+\\b/.test(el.className) && Array.from(el.children).some(c => c.innerText && c.innerText.includes('First name'))) {
                                return true;
                            }
                        }
                        return false;
                    }""",
                    timeout=25000,
                )
            except PlaywrightTimeoutError as exc:
                body_text = page.locator("body").inner_text(timeout=3000) if page.locator("body").count() else ""
                reason = _gem_job_closed_reason(page.url, body_text)
                if reason:
                    if cache_path is not None:
                        cache_path.write_text(page.content(), encoding="utf-8")
                    raise RuntimeError(reason) from exc
                raise
            page.wait_for_timeout(1000)
            body_text = page.locator("body").inner_text(timeout=3000) if page.locator("body").count() else ""
            reason = _gem_job_closed_reason(page.url, body_text)
            if reason:
                if cache_path is not None:
                    cache_path.write_text(page.content(), encoding="utf-8")
                raise RuntimeError(reason)
            if cache_path is not None:
                cache_path.write_text(page.content(), encoding="utf-8")
            data = page.evaluate(
                """() => {
                    const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                    const form = """
                + _GEM_FORM_JS
                + """;
                    const selfIdRoot = Array.from(document.querySelectorAll("div[class*='formLayout-']")).find(
                        (node) => normalize(node.innerText || '').includes('Voluntary Self-Identification')
                    ) || null;
                    const groups = [];
                    if (form) {
                        Array.from(form.children).forEach((group, index) => groups.push({ group, index, group_scope: 'main' }));
                    }
                    if (selfIdRoot) {
                        Array.from(selfIdRoot.querySelectorAll("div[class*='input-']")).forEach(
                            (group, index) => groups.push({ group, index, group_scope: 'self_id' })
                        );
                    }
                    const fields = groups.map(({ group, index, group_scope }) => {
                        const labelElement = group.querySelector('span[class*="bodyImportant"], div[class*="bodyImportant"]');
                        const rawLabel = normalize(labelElement ? labelElement.innerText : '');
                        const required = /\\*/.test(rawLabel);
                        const cleanLabel = normalize(rawLabel.replace(/\\s*\\*$/, ''));
                        let kind = 'unknown';
                        if (group.querySelector('input[type="file"]')) {
                            kind = 'file';
                        } else if (group.querySelector('textarea')) {
                            kind = 'textarea';
                        } else if (group.querySelector('input[type="radio"]')) {
                            kind = 'radio';
                        } else if (group.querySelector('input[type="checkbox"]')) {
                            kind = 'checkbox';
                        } else if (group.querySelector('select')) {
                            kind = 'select';
                        } else if (group.querySelector('button')) {
                            kind = 'select';
                        } else if (group.querySelector('input:not([type="file"]):not([type="radio"]):not([type="checkbox"])')) {
                            kind = 'text';
                        }
                        return {
                            index,
                            group_scope,
                            label: cleanLabel,
                            raw_label: rawLabel,
                            required,
                            kind,
                            options: Array.from(group.querySelectorAll('label')).map(label => normalize(label.innerText)).filter(Boolean),
                        };
                    }).filter(field => field.label);
                    return {
                        title: document.title,
                        page_text: normalize(document.body ? document.body.innerText : ''),
                        fields,
                    };
                }"""
            )
            return data
        finally:
            browser.close()


def _question_specs(fields: list[dict], generated_field_names: set[str]) -> list[dict]:
    specs = []
    for field in fields:
        if field["field_name"] not in generated_field_names:
            continue
        specs.append(
            {
                "field_name": field["field_name"],
                "label": field["label"],
                "description": "",
                "required": field["required"],
                "type": field["kind"],
            }
        )
    return specs


def _write_unknown_questions(out_dir: Path, unknown_questions: list[dict]) -> None:
    path = role_submit_path(out_dir, AUTOFILL_UNKNOWN_QUESTIONS_JSON)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "questions": unknown_questions,
    }
    path.write_text(json_dumps_pretty(payload) + "\n", encoding="utf-8")


def _build_payload(out_dir: Path, provider: str | None = None) -> dict:
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)
    profile = parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    try:
        inspection = _inspect_gem_form(
            meta["jd_source"],
            headless=True,
            cache_path=role_submit_path(out_dir, APPLICATION_PAGE_HTML),
        )
    except RuntimeError as exc:
        if "job_closed" in str(exc).lower():
            unavailable_path = _write_gem_job_unavailable_artifact(
                out_dir,
                application_url=str(meta.get("jd_source_resolved") or meta.get("board_url") or meta.get("jd_source") or ""),
                source_url=meta.get("jd_source"),
                message=str(exc),
            )
            raise RuntimeError(f"{exc} Evidence: {unavailable_path}") from exc
        raise

    raw_fields = inspection["fields"]
    counts: dict[str, int] = {}
    fields: list[dict] = []
    for raw_field in raw_fields:
        slug = slugify_label(raw_field["label"])
        counts[slug] = counts.get(slug, 0) + 1
        field_name = slug if counts[slug] == 1 else f"{slug}_{counts[slug]}"
        fields.append({**raw_field, "field_name": field_name})
    pending_user_input = _pending_user_input_fields(fields)
    if pending_user_input:
        pending_path = write_pending_user_input(
            out_dir,
            board="gem",
            questions=pending_user_input,
        )
        labels = ", ".join(question["label"] for question in pending_user_input)
        raise ValueError(f"Gem submit requires explicit user input before submission for: {labels}. See {pending_path}")
    clear_pending_user_input(out_dir)

    deterministic_fields: list[dict] = []
    generated_candidates: list[dict] = []
    unknown_questions: list[dict] = []

    for field in fields:
        if label_matches(field, "why are you interested in backops"):
            generated_candidates.append(field)
            continue
        if field["kind"] in {"text", "textarea"} and _is_website_password_field(field):
            deterministic_fields.append(field)
            continue
        if (
            field["kind"] in {"text", "textarea"}
            and not label_matches(
                field,
                "first name",
                "last name",
                "email",
                "linkedin",
                "location",
                "how did you hear",
                "where did you hear",
                "how did you learn",
                "cover letter",
            )
            and not _is_website_field(field)
            and classify_question(field.get("label", "")) is None
            and not build_onsite_start_location_answer(field.get("label"), application_profile)
        ):
            generated_candidates.append(field)
            continue
        deterministic_fields.append(field)

    generated_answers = generate_application_answers(
        out_dir=out_dir,
        meta=meta,
        question_specs=_question_specs(fields, {field["field_name"] for field in generated_candidates}),
        provider=provider,
    )

    cover_letter = find_cover_letter_text(out_dir)
    cover_letter_paragraphs = extract_cover_letter_paragraphs(cover_letter)
    if cover_letter_paragraphs:
        for field in generated_candidates:
            if label_matches(field, "why are you interested in backops"):
                generated_answers.setdefault(
                    field["field_name"],
                    build_company_specific_answer(meta["company_proper"], cover_letter_paragraphs, max_sentences=4),
                )

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
            f"Autofill payload is missing answers for required Gem fields: {labels}. "
            f"See {role_submit_path(out_dir, AUTOFILL_UNKNOWN_QUESTIONS_JSON)}"
        )

    return {
        "job_url": meta["jd_source"],
        "out_dir": str(out_dir),
        "job_title": inspection["title"] or meta.get("jd_title") or "",
        "company": meta["company_proper"],
        "candidate_email": profile.email,
        "verification_code_email": application_profile.verification_code_email or profile.email,
        "mode": "review-before-submit",
        "notes": [
            "Application-profile defaults come from application_profile.md.",
            "Open-ended answers are generated from the tailored assets, research cache, and candidate context.",
            "The Gem runtime discovers fields from the rendered form instead of assuming a fixed field order beyond direct form grouping.",
            "Gem submit attempts use Playwright's bundled Chromium by default instead of the installed Chrome app on macOS terminals.",
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


def _gem_form_selector(page) -> str:
    """Return the CSS selector for the Gem application form on the current page."""
    sel = page.evaluate(_GEM_FORM_CSS_JS)
    return sel or ".form-33"  # fallback


def _field_group(page, step: dict):
    if step.get("group_scope") == "self_id":
        self_id_root = page.locator("div[class*='formLayout-']").filter(has_text="Voluntary Self-Identification").first
        return self_id_root.locator("div[class*='input-']").nth(int(step["form_index"]))
    sel = _gem_form_selector(page)
    return page.locator(f"{sel} > div").nth(int(step["form_index"]))


def _is_cover_letter_step(step: dict) -> bool:
    if step.get("source") in {"cover_letter_text.txt", "existing_cover_letter_asset"}:
        return True
    if "cover_letter" in str(step.get("field_name") or ""):
        return True
    return "cover letter" in normalize_text(step.get("label"))


def _fill_text_value(locator, value: str) -> None:
    if len(value) >= 400:
        locator.fill(value)
        return
    human_fill(locator, value)


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
    return normalize_text(_read_control_value(locator)) == normalize_text(expected)


def _confirm_file_attached(locator) -> bool:
    try:
        return bool(locator.evaluate("(element) => !!(element.files && element.files.length > 0)"))
    except Exception:
        return False


def _mark_visible_self_id_unconfirmed(step: dict) -> None:
    step["filled"] = False
    step["status"] = "planned"
    step["note"] = "Selected the planned self-ID answer but could not confirm it remained visible on the live form."


def _mark_choice_step_unconfirmed(step: dict) -> None:
    step["filled"] = False
    step["status"] = "planned"
    note = str(step.get("note") or "").strip()
    confirmation_note = "Selected the planned answer but could not confirm it remained visible on the live form."
    if confirmation_note not in note:
        step["note"] = f"{note} {confirmation_note}".strip()


def _confirm_choice_step(group, step: dict, *, locator=None) -> bool:
    expected = str(step.get("option") or step.get("value") or "")
    if not expected:
        return False
    if step["kind"] in {"text", "textarea"} and locator is not None:
        return normalize_text(_read_control_value(locator)) == normalize_text(expected)
    normalized_expected = normalize_text(expected)
    try:
        return bool(
            group.evaluate(
                """(node, expected) => {
                    const normalize = (value) => (value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
                    const matches = (value) => {
                        const normalized = normalize(value);
                        return Boolean(normalized) && (normalized.includes(expected) || expected.includes(normalized));
                    };
                    const selectedTexts = [];
                    const push = (value) => { if (value) selectedTexts.push(value); };
                    for (const input of node.querySelectorAll("input[type='radio']:checked")) {
                        const labels = Array.from(input.labels || []).map(label => label.textContent || "");
                        if (labels.length) {
                            labels.forEach(push);
                        } else if (input.closest("label")) {
                            push(input.closest("label").textContent || "");
                        }
                    }
                    for (const input of node.querySelectorAll("input[type='checkbox']:checked")) {
                        const labels = Array.from(input.labels || []).map(label => label.textContent || "");
                        if (labels.length) {
                            labels.forEach(push);
                        } else if (input.closest("label")) {
                            push(input.closest("label").textContent || "");
                        }
                    }
                    for (const button of node.querySelectorAll("button")) {
                        push(button.textContent || "");
                    }
                    for (const active of node.querySelectorAll(
                        "[role='radio'][aria-checked='true'], [role='checkbox'][aria-checked='true'], [role='menuitemcheckbox'][aria-checked='true'], [role='option'][aria-selected='true'], [role='menuitem'][aria-selected='true']"
                    )) {
                        push(active.textContent || "");
                    }
                    return selectedTexts.some(matches);
                }""",
                normalized_expected,
            )
        )
    except Exception:
        return False


def _confirm_visible_self_id_step(group, step: dict, *, locator=None) -> bool:
    return _confirm_choice_step(group, step, locator=locator)


def _visible_choice_options(page) -> list[tuple[object, str]]:
    if not hasattr(page, "locator"):
        return []
    options = []
    locators = page.locator("[role='option'], [role='menuitem']")
    for index in range(locators.count()):
        locator = locators.nth(index)
        try:
            if not locator.is_visible():
                continue
            text = str(locator.inner_text() or "").strip()
        except Exception:
            continue
        if not normalize_text(text):
            continue
        options.append((locator, text))
    return options


def _select_visible_choice_option(page, desired: str) -> str | None:
    visible_options = _visible_choice_options(page)
    labels = [label for _, label in visible_options]
    matched = select_option(labels, desired, filter_select_prefix=True)
    if matched is None:
        return None
    for locator, label in reversed(visible_options):
        if normalize_text(label) == normalize_text(matched):
            locator.click()
            return label
    return None


def _fill_choice_step(page, group, step: dict) -> None:
    label_loc = group.locator("label", has_text=step["option"])
    if step["kind"] == "radio" and label_loc.count():
        label_loc.first.click()
    else:
        native_select = group.locator("select")
        if native_select.count():
            locator = native_select.first
            options = [
                text
                for text in locator.locator("option").evaluate_all(
                    """(els) => els.map((el) => (el.textContent || '').replace(/\\s+/g, ' ').trim())"""
                )
                if normalize_text(text)
            ]
            matched = select_option(options, str(step.get("option") or ""), filter_select_prefix=True)
            if matched is None:
                raise RuntimeError(f"Could not match select option for Gem field: {step['label']}")
            locator.select_option(label=matched)
            step["option"] = matched
        else:
            button_loc = group.locator("button")
            if not button_loc.count():
                raise RuntimeError(f"Could not find radio label or dropdown button for Gem field: {step['label']}")
            button_loc.first.click()
            page.wait_for_timeout(300)
            matched = _select_visible_choice_option(page, str(step.get("option") or ""))
            if matched is None:
                fallback = page.get_by_text(step["option"], exact=True)
                getattr(fallback, "last", fallback).click()
            else:
                step["option"] = matched
    if is_visible_self_id_blocker(step) and not _confirm_visible_self_id_step(group, step):
        _mark_visible_self_id_unconfirmed(step)
        return
    if not _confirm_choice_step(group, step):
        _mark_choice_step_unconfirmed(step)
        return
    step["filled"] = True


def _fill_step(page, step: dict) -> None:
    group = _field_group(page, step)
    if step["kind"] == "text":
        locator = group.locator("input:not([type='file']):not([type='radio']):not([type='checkbox']), textarea").first
        _fill_text_value(locator, str(step["value"] or ""))
        if _is_cover_letter_step(step) and not _confirm_cover_letter_text(locator, str(step["value"] or "")):
            raise RuntimeError(f"Could not confirm Gem cover letter text for {step['label']}")
        if is_visible_self_id_blocker(step) and not _confirm_visible_self_id_step(group, step, locator=locator):
            _mark_visible_self_id_unconfirmed(step)
            return
        step["filled"] = True
        return
    if step["kind"] == "textarea":
        locator = group.locator("textarea").first
        _fill_text_value(locator, str(step["value"] or ""))
        if _is_cover_letter_step(step) and not _confirm_cover_letter_text(locator, str(step["value"] or "")):
            raise RuntimeError(f"Could not confirm Gem cover letter text for {step['label']}")
        if is_visible_self_id_blocker(step) and not _confirm_visible_self_id_step(group, step, locator=locator):
            _mark_visible_self_id_unconfirmed(step)
            return
        step["filled"] = True
        return
    if step["kind"] == "file":
        locator = group.locator("input[type='file']").first
        locator.set_input_files(step["file_path"])
        if _is_cover_letter_step(step) and not _confirm_file_attached(locator):
            raise RuntimeError(f"Could not confirm Gem cover letter upload for {step['label']}")
        step["filled"] = True
        return
    if step["kind"] in {"radio", "select"}:
        _fill_choice_step(page, group, step)
        return

    raise RuntimeError(f"Unsupported Gem step kind: {step['kind']}")


def _gem_page_snapshot(page) -> dict:
    return page_snapshot(page, form_selector=".form-33", captcha_type="hcaptcha")


def _classify_submit_state(snapshot: dict) -> dict[str, object]:
    page_text = snapshot.get("page_text", "")
    if any(pattern.search(page_text) for pattern in SUBMIT_CONFIRM_PATTERNS):
        return {"status": "confirmed", "reason": "text"}
    if snapshot.get("hcaptcha_visible") and snapshot.get("form_visible"):
        return {"status": "captcha_required"}
    errors = list(snapshot.get("errors") or [])
    page_level_errors = [
        pattern.search(page_text).group(0) for pattern in VALIDATION_ERROR_PATTERNS if pattern.search(page_text)
    ]
    combined_errors = list(dict.fromkeys(errors + page_level_errors))
    if combined_errors:
        return {"status": "validation_error", "errors": combined_errors}
    return {"status": "pending"}


def _confirmed_outcome_from_email(snapshot: dict | None, email_confirmation: dict) -> dict[str, object]:
    snapshot = dict(snapshot or {})
    snapshot.setdefault("page_text", "(matched application confirmation email while browser confirmation was pending)")
    return {
        "status": "confirmed",
        "reason": "email_confirmation",
        "snapshot": snapshot,
        "email_confirmation": email_confirmation,
    }


def _wait_for_gem_form(page) -> None:
    """Wait for the Gem application form to be fully rendered."""
    page.wait_for_function(
        """() => {
            const candidates = document.querySelectorAll('[class*="form-"]');
            for (const el of candidates) {
                if (/\\bform-\\d+\\b/.test(el.className) && Array.from(el.children).some(c => c.innerText && c.innerText.includes('First name'))) {
                    return true;
                }
            }
            return false;
        }""",
        timeout=25000,
    )


def main() -> int:
    from autofill_pipeline import autofill_main, run_browser_pipeline

    return autofill_main(
        board_name="gem",
        build_payload_fn=_build_payload,
        run_browser_fn=lambda pp, headless, submit: run_browser_pipeline(
            pp,
            headless=headless,
            submit=submit,
            board_name="gem",
            retry_unconfirmed_visible_self_id_once=True,
            form_ready_fn=_wait_for_gem_form,
            fill_step_fn=_fill_step,
            page_snapshot_fn=lambda page: page_snapshot(
                page, form_selector=_gem_form_selector(page), captcha_type="hcaptcha"
            ),
            classify_state_fn=_classify_submit_state,
            click_submit_fn=lambda page: click_submit_button(page, button_names=SUBMIT_BUTTON_NAMES),
            capture_fn=lambda page, path: capture_full_page(
                page, path, preferred_selectors=PREFERRED_CAPTURE_SELECTORS
            ),
            confirmed_outcome_from_email_fn=_confirmed_outcome_from_email,
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
