#!/usr/bin/env python3
"""Generate and optionally run a Greenhouse application autofill flow."""

from __future__ import annotations

import argparse
import base64
import html as html_lib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen

try:
    from playwright.sync_api import Error as PlaywrightError
except Exception:  # pragma: no cover - import fallback for environments without Playwright

    class PlaywrightError(Exception):
        pass


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from answer_generation_support import (
    _augment_answer_generation_prompt_with_verifier_feedback,
    _build_answer_verification_source_bundle,
    _verification_retry_feedback_by_field,
    _verifier_retry_feedback_blockers,
)
from answer_refresh_state import current_answer_refresh_request_id
from answer_verifier import verify_generated_answers
from application_models import ApplicationProfile
from application_models import parse_application_profile as parse_shared_application_profile
from application_submit_common import (
    APPLICATION_ANSWER_EM_DASH_GUIDANCE as SHARED_APPLICATION_ANSWER_EM_DASH_GUIDANCE,
)
from application_submit_common import (
    APPLICATION_ANSWER_FALLBACK_RAW,
    GeneratedAnswerBlockersError,
    _best_engagement_option,
    _classified_shared_answers,
    _generated_answer_blocker_step,
    _linked_resource_answer_payload,
    _linked_resource_deterministic_answers,
    _linked_resource_failure_blockers,
    _run_answer_generation_provider,
    _write_pending_linked_resource_blockers,
    apply_draft_overrides,
    build_email_confirmation_watcher,
    build_onsite_start_location_answer,
    build_optional_retry_blank_fallback_answers,
    build_truthful_work_authorization_answer,
    build_verifier_retry_fallback_answers,
    clear_answer_generation_artifacts,
    clear_pending_user_input,
    current_professional_license_inventory_answer,
    find_matching_cached_application_answers_path,
    load_cached_application_answers,
    normalize_multi_select_generated_answers,
    pending_user_input_reason_for_spec,
    preferred_meta_job_url,
    primary_employer_name,
    provider_command_for_mode,
    question_is_current_company_field,
    question_is_relocation_willingness,
    question_prefers_generated_free_text_answer,
    question_requests_current_professional_license_inventory,
    question_requests_sponsorship_requirement,
    reply_to_confirmation_email,
    resolve_how_did_you_hear_candidates,
    resolve_how_did_you_hear_option_candidates,
    resolve_shared_question_policy,
    should_skip_optional_generated_answer,
    validate_generated_answers_with_blockers,
    write_pending_user_input,
    write_pending_user_input_for_unconfirmed_fields,
)
from application_submit_common import (
    build_application_answers_prompt as build_shared_application_answers_prompt,
)
from autofill_common import (
    blocking_unconfirmed_report_entries,
    dedupe_page_screenshot_artifacts,
    mark_visible_profile_field_step,
    mark_visible_self_id_step,
    match_prior_employer_option,
    select_location_positive_fit_option,
    select_shared_policy_option,
)
from browser_runtime import (
    human_fill,
    launch_chromium_browser,
    submit_browser_profile_dir,
    submit_slow_mo_ms,
    submit_viewport,
)
from greenhouse_capture import (
    _capture_root_selector,
    _capture_scroll_metrics,
    _choose_capture_root,
    _set_capture_scroll_position,
)
from greenhouse_common import (
    GENERIC_SUBDOMAINS,
    greenhouse_browser_job_closed_reason,
    is_greenhouse_error_page,
    probe_greenhouse_board_slug,
    write_job_unavailable_artifact,
)
from greenhouse_failure_artifacts import (
    GREENHOUSE_REVIEW_PROOF_GAP_FAILURE,
    GREENHOUSE_RUNTIME_FAILURE,
    GREENHOUSE_SECURITY_CODE_UNRESOLVED_FAILURE,
    GREENHOUSE_SUBMIT_NAVIGATION_MISSING_FAILURE,
    GREENHOUSE_SUBMIT_NOT_CONFIRMED_FAILURE,
    GREENHOUSE_SUBMIT_VALIDATION_FAILURE,
    GREENHOUSE_UNKNOWN_QUESTIONS_FAILURE,
    clear_greenhouse_failure_artifacts,
    greenhouse_submission_result_path,
    write_greenhouse_failed_result,
)
from greenhouse_preference_research import clear_preference_research_artifacts, prepare_preference_research_context
from job_board_urls import canonical_greenhouse_job_url
from linked_resource_context import prepare_linked_resource_context
from llm_provider import VALID_PROVIDERS, provider_timeout_seconds
from output_layout import (
    JOB_UNAVAILABLE_JSON,
    PREFERENCE_RESEARCH_FAILURES_JSON,
    SUBMISSION_RESULT_JSON,
    migrate_role_output_layout,
    role_content_path,
    role_submit_path,
)
from pipeline_meta_common import load_pipeline_meta
from project_env import load_project_env
from question_classifier import classify_question

_greenhouse_browser_job_closed_reason = greenhouse_browser_job_closed_reason
APPLICATION_ANSWER_EM_DASH_GUIDANCE = SHARED_APPLICATION_ANSWER_EM_DASH_GUIDANCE

PROJECT_ROOT = SCRIPT_DIR.parent
MASTER_RESUME_PATH = PROJECT_ROOT / "master_resume.md"
WORK_STORIES_PATH = PROJECT_ROOT / "work_stories.md"
CANDIDATE_CONTEXT_PATH = PROJECT_ROOT / "candidate_context.md"
APPLICATION_PROFILE_PATH = PROJECT_ROOT / "application_profile.md"
REMIX_CONTEXT_RE = re.compile(r"window\.__remixContext\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S)
URL_RE = re.compile(r"^https?://", re.I)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
SIGNOFF_PREFIXES = ("best regards", "regards", "sincerely", "thank you")
HTML_TAG_RE = re.compile(r"<[^>]+>")
QUESTION_SENTENCE_HINT_RE = re.compile(r"(\d+\s*-\s*\d+|\d+\s+to\s+\d+)\s+sentences?", re.I)
DETERMINISTIC_QUESTION_LABEL_FRAGMENTS = (
    "first name",
    "last name",
    "email",
    "phone",
    "candidate location",
    "location (city)",
    "linkedin profile",
    "github",
    "other website",
    "portfolio",
    "personal website",
    "website",
    "pronouns",
    "legal name",
    "preferred first name",
    "preferred last name",
    "from where do you intend to work",
    "where do you intend to work",
    "current location",
    "state of residence",
    "province of residence",
    "current state of residence",
    "current province of residence",
    "where are you currently based",
    "where are you based",
    "do you live in",
    "currently live in",
    "reside in",
    "based in",
    "located in",
    "willing to relocate",
    "open to relocation",
    "open to relocate",
    "relocate",
    "authorized to work",
    "work authorization",
    "right to work",
    "legally authorized",
    "sponsorship",
    "sponsor",
    "visa",
    "worked for",
    "previously employed",
    "previously been employed",
    "how did you learn",
    "how did you hear",
    "gender",
    "sex",
    "transgender",
    "race",
    "ethnicity",
    "sexual orientation",
    "veteran",
    "protected veteran",
    "military status",
    "military",
    "disability",
    "what is your age",
    "age range",
    "age group",
    "resume/cv",
    "country",
)


load_project_env()
APPLICATION_ANSWER_CACHE = "application_answers.json"
APPLICATION_ANSWER_RAW = "application_answers_raw.txt"
AUTOFILL_REPORT_MD = "greenhouse_autofill_report.md"
AUTOFILL_REPORT_JSON = "greenhouse_autofill_report.json"
AUTOFILL_PRE_SUBMIT_SCREENSHOT = "greenhouse_autofill_pre_submit.png"
AUTOFILL_REVIEW_SCREENSHOT = "greenhouse_autofill_review.png"
AUTOFILL_PAGE_SCREENSHOTS_DIR = "greenhouse_autofill_pages"
AUTOFILL_UNKNOWN_QUESTIONS_JSON = "greenhouse_unknown_questions.json"
AUTOFILL_SUBMIT_DEBUG_HTML = "greenhouse_submit_debug.html"
AUTOFILL_SUBMIT_DEBUG_SCREENSHOT = "greenhouse_submit_debug.png"
WEBSITE_CONFIRMATION_JSON = "application_confirmation_website.json"
EMAIL_CONFIRMATION_JSON = "application_confirmation_email.json"
NOTION_SYNC_STATUS_JSON = "notion_sync_status.json"
DEFAULT_SECURITY_CODE_WAIT_SECONDS = 20
NORMALIZED_TEXT_RE = re.compile(r"[^a-z0-9]+")
SECURITY_CODE_RE = re.compile(r"(?:verification|security)\s+code(?:\s+is|:)?\s*([A-Z0-9]{6,10})", re.I)
GENERIC_CODE_TOKEN_RE = re.compile(r"\b([A-Za-z0-9]{8})\b")
NEXT_BUTTON_RE = re.compile(r"(next|continue|review|save and continue|next step|continue application|proceed)", re.I)
SUBMIT_BUTTON_RE = re.compile(r"submit(?:\s+application)?", re.I)
SUBMISSION_CONFIRM_URL_RE = re.compile(r"(confirmation|thank|submitted|complete|success)", re.I)
SUBMISSION_CONFIRM_TEXT_PATTERNS = (
    re.compile(r"\bthank you for applying\b", re.I),
    re.compile(r"\bthank you for (?:your )?applying\b", re.I),
    re.compile(r"\bthanks for applying\b", re.I),
    re.compile(r"\bthanks for submitting\b", re.I),
    re.compile(r"\bapplication (?:has been )?submitted\b", re.I),
    re.compile(r"\bapplication (?:has been )?received\b", re.I),
    re.compile(r"\bsubmission received\b", re.I),
    re.compile(r"\bwe(?:'|’)ve received your application\b", re.I),
    re.compile(r"\bwe have received your application\b", re.I),
    re.compile(r"\bwe(?:'|’)ll be in touch\b", re.I),
    re.compile(r"\bwe will be in touch\b", re.I),
    re.compile(r"\byour application is under review\b", re.I),
    re.compile(r"\bunder review\b", re.I),
)
SUBMISSION_ERROR_TEXT_PATTERNS = (
    re.compile(r"\benter a valid security code\b", re.I),
    re.compile(r"\bthis field is required\b", re.I),
    re.compile(r"\bplease (?:enter|select|choose)\b", re.I),
    re.compile(r"\bis required\b", re.I),
    re.compile(r"\binvalid\b", re.I),
    re.compile(r"\berror\b", re.I),
)
STATE_NAME_MAP = {
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


def _maybe_reexec_with_uv() -> None:
    if os.environ.get("JOB_ASSETS_GREENHOUSE_BOOTSTRAPPED") == "1":
        return
    if not shutil.which("uv"):
        return
    env = os.environ.copy()
    env["JOB_ASSETS_GREENHOUSE_BOOTSTRAPPED"] = "1"
    cmd = ["uv", "run", "--project", str(PROJECT_ROOT), "python", __file__, *sys.argv[1:]]
    raise SystemExit(subprocess.call(cmd, cwd=PROJECT_ROOT, env=env))


class CandidateProfile:
    def __init__(
        self,
        *,
        full_name: str,
        first_name: str,
        last_name: str,
        email: str,
        phone: str,
        location: str,
        linkedin: str | None,
        website: str | None,
        work_authorized: bool,
        employers: set[str],
        education_entries: list[EducationEntry],
        employment_entries: list[EmploymentEntry] | None = None,
    ) -> None:
        self.full_name = full_name
        self.first_name = first_name
        self.last_name = last_name
        self.email = email
        self.phone = phone
        self.location = location
        self.linkedin = linkedin
        self.website = website
        self.work_authorized = work_authorized
        self.employers = employers
        self.education_entries = education_entries
        self.employment_entries = employment_entries or []


class EducationEntry:
    def __init__(
        self,
        *,
        school: str,
        degree_option: str,
        discipline_option: str,
        end_year: str,
        start_year: str = "",
    ) -> None:
        self.school = school
        self.degree_option = degree_option
        self.discipline_option = discipline_option
        self.end_year = end_year
        self.start_year = start_year


class EmploymentEntry:
    def __init__(
        self,
        *,
        company: str,
        title: str,
        start_month: str,
        start_year: str,
    ) -> None:
        self.company = company
        self.title = title
        self.start_month = start_month
        self.start_year = start_year


def _parse_application_profile(text: str) -> ApplicationProfile:
    return parse_shared_application_profile(text)


def _normalize_url(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    return f"https://{value}"


def _title_case_phrase(value: str) -> str:
    return re.sub(
        r"\b([A-Za-z]+(?:'[A-Za-z]+)?)\b",
        lambda match: (
            match.group(1).capitalize()
            if match.group(1).casefold() not in {"of", "and", "the", "for", "in"}
            else match.group(1).casefold()
        ),
        value.strip(),
    )


def _canonical_school_name(raw_school: str) -> str:
    normalized = " ".join(raw_school.strip().split())
    if "," in normalized:
        parts = [part.strip() for part in normalized.split(",") if part.strip()]
        preferred = next(
            (
                part
                for part in reversed(parts)
                if any(token in part.casefold() for token in ("university", "college", "institute"))
            ),
            parts[-1],
        )
        normalized = preferred
    return _title_case_phrase(normalized)


def _education_degree_option(degree_text: str) -> str:
    normalized = degree_text.casefold()
    if "m.b.a" in normalized or re.search(r"\bmba\b", normalized):
        return "Master of Business Administration (M.B.A.)"
    if "m.s" in normalized or "master" in normalized:
        return "Master's Degree"
    if "b.s" in normalized or "bachelor" in normalized:
        return "Bachelor's Degree"
    if "associate" in normalized:
        return "Associate's Degree"
    if "ph.d" in normalized or "doctor of philosophy" in normalized:
        return "Doctor of Philosophy (Ph.D.)"
    if "high school" in normalized:
        return "High School"
    return "Other"


def _education_discipline_option(degree_text: str) -> str:
    normalized = degree_text.casefold()
    if "finance" in normalized:
        return "Finance"
    if "computer science" in normalized:
        return "Computer Science"
    if "business administration" in normalized or "m.b.a" in normalized or re.search(r"\bmba\b", normalized):
        return "Business Administration"
    if "artificial intelligence" in normalized:
        return "Artificial Intelligence"
    if "machine learning" in normalized:
        return "Machine Learning"
    if "actuarial" in normalized or "statistics" in normalized:
        return "Statistics & Decision Theory"
    return "Other"


def _parse_education_entries(lines: list[str]) -> list[EducationEntry]:
    try:
        start_index = next(index for index, line in enumerate(lines) if line.strip() == "EDUCATION")
    except StopIteration:
        return []

    entries: list[EducationEntry] = []
    index = start_index + 1
    while index < len(lines):
        school_line = lines[index].strip()
        if not school_line:
            index += 1
            continue
        if school_line.startswith("#") or school_line == "SKILLS & ADDITIONAL":
            break
        degree_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if "|" not in degree_line:
            index += 1
            continue
        degree_text = degree_line.split("|", 1)[0].strip()
        year_match = re.search(r"(\d{4})\s*[–-]\s*(\d{4})", degree_line)
        start_year = year_match.group(1) if year_match else ""
        end_year = year_match.group(2) if year_match else ""
        if school_line and degree_text and end_year:
            entries.append(
                EducationEntry(
                    school=_canonical_school_name(school_line),
                    degree_option=_education_degree_option(degree_text),
                    discipline_option=_education_discipline_option(degree_text),
                    end_year=end_year,
                    start_year=start_year,
                )
            )
        index += 1

    return entries


_MONTH_NAMES = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
_MONTH_ABBREV_TO_FULL: dict[str, str] = {}
for _m in _MONTH_NAMES:
    _MONTH_ABBREV_TO_FULL[_m.casefold()] = _m
    _MONTH_ABBREV_TO_FULL[_m[:3].casefold()] = _m


def _parse_employment_entries(lines: list[str]) -> list[EmploymentEntry]:
    """Parse employment entries from the EXPERIENCE section of master_resume.md.

    Each entry has:
      COMPANY — Title
      Location | Month Year–...
    """
    try:
        start_index = next(
            index
            for index, line in enumerate(lines)
            if line.strip() in ("EXPERIENCE", "WORK EXPERIENCE", "PROFESSIONAL EXPERIENCE")
        )
    except StopIteration:
        return []

    entries: list[EmploymentEntry] = []
    index = start_index + 1
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if line in ("EDUCATION", "SKILLS & ADDITIONAL", "SKILLS"):
            break
        # Match "COMPANY — Title" pattern
        company_match = re.match(r"^(?:##\s+)?(.+?)\s+[—–-]+\s+(.+)$", line)
        if not company_match:
            index += 1
            continue
        company_raw = company_match.group(1).strip()
        title = company_match.group(2).strip()
        # Next line should have date info: "Location | Month Year–..."
        date_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        date_match = re.search(r"(\w+)\s+(\d{4})\s*[–\-—]", date_line)
        start_month = ""
        start_year = ""
        if date_match:
            month_raw = date_match.group(1)
            full_month = _MONTH_ABBREV_TO_FULL.get(month_raw.casefold(), "")
            if full_month:
                start_month = full_month
                start_year = date_match.group(2)
        if company_raw and title and start_year:
            company_name = company_raw
            if company_name.isupper():
                company_name = " ".join(word.capitalize() if word.isalpha() else word for word in company_name.split())
            entries.append(
                EmploymentEntry(
                    company=company_name,
                    title=title,
                    start_month=start_month,
                    start_year=start_year,
                )
            )
        index += 1

    return entries


def _location_search_text(location: str) -> str:
    """Return search text for Greenhouse location combobox.

    Use the full location (e.g. "San Francisco, CA") instead of just the city
    name to avoid ambiguous results like "San Francisco, Cebu, Philippines".
    """
    return location.strip()


def _location_match_candidates(location: str, country: str | None = None) -> list[str]:
    parts = [part.strip() for part in location.split(",") if part.strip()]
    if not parts:
        return [location]

    city = parts[0]
    candidates = [location, city]
    if _normalize_free_text(city) == "new york city":
        candidates.append("NYC")
    if _normalize_free_text(city) == "san francisco":
        candidates.append("Bay Area")

    if len(parts) >= 2:
        state = parts[1]
        candidates.append(state)
        expanded_state = STATE_NAME_MAP.get(state.upper())
        if expanded_state:
            candidates.append(expanded_state)
            candidates.append(", ".join([city, expanded_state]))
            if country:
                candidates.append(", ".join([city, expanded_state, country]))
    if country:
        candidates.append(country)
        candidates.append(f"{city}, {country}")
    return [
        candidate for candidate in dict.fromkeys(candidate.strip() for candidate in candidates if candidate.strip())
    ]


def _normalized_option_match_candidates(field_name: str, desired: str) -> list[str]:
    candidates = [desired]
    if "," in desired:
        candidates.extend(_location_match_candidates(desired))
    normalized_candidates = [
        _normalize_free_text(candidate) for candidate in candidates if _normalize_free_text(candidate)
    ]
    desired_normalized = _normalize_free_text(desired)
    if desired_normalized in {"na", "n a", "not applicable", "none", "none of the above"}:
        normalized_candidates.extend(["na", "n a", "not applicable", "none", "none of the above"])
    if field_name in ("gender", "gender_identity"):
        if ("male" in desired_normalized or desired_normalized.endswith(" man") or desired_normalized == "man") and (
            "woman" not in desired_normalized
        ):
            normalized_candidates.extend(["man", "male", "masculine", "man male or masculine"])
        if (
            "female" in desired_normalized or desired_normalized.endswith(" woman") or desired_normalized == "woman"
        ) and ("man" not in desired_normalized):
            normalized_candidates.extend(["woman", "female", "feminine", "woman female or feminine"])
        if desired_normalized == "male":
            normalized_candidates.append("man")
        if desired_normalized == "female":
            normalized_candidates.append("woman")
        if re.search(r"\bmale\b", desired_normalized):
            normalized_candidates.append(
                re.sub(r"\bman\b(?:\s+\bman\b)+", "man", re.sub(r"\bmale\b", "man", desired_normalized)).strip()
            )
        if re.search(r"\bfemale\b", desired_normalized):
            normalized_candidates.append(
                re.sub(
                    r"\bwoman\b(?:\s+\bwoman\b)+", "woman", re.sub(r"\bfemale\b", "woman", desired_normalized)
                ).strip()
            )
    if field_name == "transgender_status" or "transgender" in field_name:
        if desired_normalized == "no":
            normalized_candidates.extend(
                [
                    "cisgender",
                    "cis gender",
                    "not transgender",
                    "do not identify as transgender",
                    "i do not identify as transgender",
                ]
            )
        if desired_normalized == "yes":
            normalized_candidates.extend(
                [
                    "transgender",
                    "identify as transgender",
                    "i identify as transgender",
                ]
            )
    if field_name == "race" and "hispanic" in desired_normalized:
        normalized_candidates.extend(
            [
                "hispanic",
                "latino",
                "latinx",
                "hispanic latinx or of spanish origin",
            ]
        )
    if field_name == "sexual_orientation" and (
        "straight" in desired_normalized or "heterosexual" in desired_normalized
    ):
        normalized_candidates.extend(["straight", "heterosexual", "heterosexual straight", "heterosexual / straight"])
    if field_name == "pronouns":
        if desired_normalized in {"he him his", "he him"}:
            normalized_candidates.extend(
                [
                    "he",
                    "he him",
                    "he him his",
                    "he him his pronouns",
                    "he him his other variations",
                ]
            )
        if desired_normalized in {"she her hers", "she her"}:
            normalized_candidates.extend(
                [
                    "she",
                    "she her",
                    "she her hers",
                    "she her hers pronouns",
                ]
            )
    if "i don t wish to answer" in desired_normalized:
        normalized_candidates.extend(
            [
                "i do not want to answer",
                "decline to self identify",
                "prefer not to say",
                "prefer not to answer",
            ]
        )
    if field_name == "disability_status" and "do not have a disability" in _normalize_free_text(desired):
        normalized_candidates.extend(
            [
                "do not have a disability",
                "i do not identify as having a disability",
                "do not identify as having a disability",
                "i do not identify as someone with a disability",
                "do not identify as someone with a disability",
                "not disabled",
                "no",
                "none of these apply",
                "none of the above",
            ]
        )
    if field_name == "veteran_status" and "not a protected veteran" in _normalize_free_text(desired):
        normalized_candidates.extend(
            [
                "not a protected veteran",
                "i have never served in the military",
                "i identify as a non protected veteran",
                "no",
                "no i am not a veteran or active member",
                "no i am not a veteran active member or reservist",
            ]
        )
    if field_name == "education_level":
        if "graduate" in desired_normalized or "master" in desired_normalized or "doctoral" in desired_normalized:
            normalized_candidates.extend(
                [
                    "graduate degree",
                    "master s degree",
                    "master",
                    "doctoral degree",
                    "master s degree e g ma ms meng med msw mba",
                ]
            )
    if field_name == "employment_status":
        if "full time" in desired_normalized or "employed" in desired_normalized:
            normalized_candidates.extend(
                [
                    "full time employment",
                    "employed working 40 or more hours per week",
                    "employed working full time",
                    "employed full time",
                    "employed",
                ]
            )
    if field_name == "how_did_you_hear":
        if any(term in desired_normalized for term in ("website", "career site", "careers page", "corporate website")):
            normalized_candidates.extend(
                [
                    "company website",
                    "company website careers page",
                    "company website career site",
                    "company careers page",
                    "careers page",
                    "career site",
                    "corporate website",
                    "website",
                ]
            )
    # Country abbreviation mappings
    if desired_normalized in {"united states", "united states of america", "usa"}:
        normalized_candidates.extend(["us", "usa", "united states", "united states of america"])
    elif desired_normalized in {"united kingdom", "great britain"}:
        normalized_candidates.extend(["uk", "united kingdom", "great britain"])
    elif desired_normalized in {"united arab emirates"}:
        normalized_candidates.extend(["uae", "united arab emirates"])
    return list(dict.fromkeys(normalized_candidates))


def _split_sentences(text: str) -> list[str]:
    return [part.strip() for part in SENTENCE_SPLIT_RE.split(text.strip()) if part.strip()]


def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    return html_lib.unescape(HTML_TAG_RE.sub(" ", text)).replace("\xa0", " ").strip()


def _extract_cover_letter_paragraphs(text: str) -> list[str]:
    paragraphs = [part.strip() for part in text.strip().split("\n\n") if part.strip()]
    cleaned: list[str] = []
    for paragraph in paragraphs:
        lower = paragraph.lower()
        if lower.startswith("dear "):
            continue
        if any(lower.startswith(prefix) for prefix in SIGNOFF_PREFIXES):
            continue
        cleaned.append(paragraph)
    return cleaned


def _build_company_specific_answer(company: str, paragraphs: list[str], *, max_sentences: int = 4) -> str:
    company_lower = company.lower()
    first_sentence_matches = [
        paragraph
        for paragraph in paragraphs
        if _split_sentences(paragraph) and company_lower in _split_sentences(paragraph)[0].lower()
    ]
    prioritized = [p for p in paragraphs if company_lower in p.lower()]
    source = first_sentence_matches or prioritized or paragraphs

    sentences: list[str] = []
    seen: set[str] = set()
    for paragraph in source:
        for sentence in _split_sentences(paragraph):
            key = sentence.casefold()
            if key in seen:
                continue
            seen.add(key)
            sentences.append(sentence)
            if len(sentences) >= max_sentences:
                return " ".join(sentences)
    return " ".join(sentences)


def _preferred_education_entry(profile: CandidateProfile) -> EducationEntry | None:
    if not profile.education_entries:
        return None

    def score(entry: EducationEntry) -> tuple[int, int]:
        score_value = 0
        normalized = f"{entry.school} {entry.degree_option} {entry.discipline_option}".casefold()
        if "master of business administration" in normalized:
            score_value += 200
        if "finance" in normalized:
            score_value += 150
        if "computer science" in normalized:
            score_value += 100
        if "machine learning" in normalized or "artificial intelligence" in normalized:
            score_value += 80
        if "engineering" in normalized:
            score_value += 60
        if "business administration" in normalized:
            score_value += 40
        try:
            year_value = int(entry.end_year)
        except ValueError:
            year_value = 0
        return score_value, year_value

    return max(profile.education_entries, key=score)


def _education_degree_fallback_options(entry: EducationEntry) -> list[str]:
    if entry.degree_option == "Master of Business Administration (M.B.A.)":
        return ["Master Degree", "Master's Degree"]
    return []


def _education_discipline_fallback_options(entry: EducationEntry) -> list[str]:
    if entry.degree_option == "Master of Business Administration (M.B.A.)" and entry.discipline_option == "Finance":
        return ["Computer Science"]
    return []


def _parse_master_resume(text: str) -> CandidateProfile:
    lines = [line.rstrip() for line in text.splitlines()]
    stripped = [line.strip() for line in lines if line.strip()]

    full_name = next((line.title() for line in stripped if line.isupper() and " " in line), "")
    if not full_name:
        raise ValueError("Could not find candidate name in master_resume.md")

    try:
        location_line = next(line for line in stripped if "|" in line and "@" in line)
    except StopIteration as exc:
        raise ValueError("Could not find contact line in master_resume.md") from exc

    parts = [part.strip() for part in location_line.split("|")]

    derived_location = ""
    for line in lines:
        location_match = re.match(r"^\s*([A-Za-z .'-]+,\s*[A-Z]{2})\s*\|", line)
        if location_match:
            derived_location = location_match.group(1).strip()
            break

    if len(parts) >= 5:
        location, email, phone, linkedin, website = parts[:5]
    elif len(parts) >= 4:
        # A four-part contact line may be `location | email | phone | website`.
        first_is_location = bool(re.match(r"^[A-Za-z .'-]+,\s*[A-Z]{2}$", parts[0]))
        if first_is_location:
            location, email, phone, website = parts[:4]
            linkedin = ""
        else:
            location = derived_location
            email, phone, linkedin, website = parts[:4]
    else:
        raise ValueError("Unexpected contact line format in master_resume.md")

    employers = {
        match.group(1).strip().casefold()
        for line in lines
        for match in [re.match(r"^\s*##\s+(.+?)\s+—", line)]
        if match
    }

    work_auth_match = re.search(r"^\s*Work Authorization:\s*(.+)$", text, re.M)
    work_auth_value = work_auth_match.group(1).strip().lower() if work_auth_match else ""
    work_authorized = any(
        token in work_auth_value for token in ("citizen", "permanent resident", "authorized", "green card")
    )

    first_name, _, last_name = full_name.partition(" ")
    return CandidateProfile(
        full_name=full_name,
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=phone,
        location=location,
        linkedin=_normalize_url(linkedin),
        website=_normalize_url(website),
        work_authorized=work_authorized,
        employers=employers,
        education_entries=_parse_education_entries(lines),
        employment_entries=_parse_employment_entries(lines),
    )


def _fetch_greenhouse_html(
    url: str,
    *,
    cache_path: Path | None = None,
    fallback_cache_paths: list[Path] | None = None,
) -> str:
    def read_cached_html(path: Path) -> str:
        html = path.read_text(encoding="utf-8")
        if cache_path is not None and path != cache_path and not cache_path.exists():
            cache_path.write_text(html, encoding="utf-8")
        return html

    def _try_cached_fallbacks() -> str | None:
        """Return a valid cached page if one exists, skipping error pages."""
        candidates: list[Path] = []
        if cache_path and cache_path.exists():
            candidates.append(cache_path)
        candidates.extend(p for p in (fallback_cache_paths or []) if p.exists())
        for path in candidates:
            html = path.read_text(encoding="utf-8")
            if not is_greenhouse_error_page(html):
                return read_cached_html(path)
        return None

    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=60) as response:
            payload = response.read()
    except HTTPError as exc:
        try:
            cached = _try_cached_fallbacks()
            if cached is not None:
                return cached
            if exc.code == 404:
                raise RuntimeError(
                    f"job_closed: Job posting not found (HTTP 404) at {url} — the position may have been filled or removed"
                ) from exc
            raise RuntimeError(f"HTTP error while downloading application page: {exc.code}") from exc
        finally:
            exc.close()
    except URLError as exc:
        cached = _try_cached_fallbacks()
        if cached is not None:
            return cached
        raise RuntimeError(f"Network error while downloading application page: {exc.reason}") from exc

    html = payload.decode("utf-8", errors="replace")

    if is_greenhouse_error_page(html):
        # Retry once after a short delay — Greenhouse occasionally returns
        # transient error pages for valid jobs.
        time.sleep(3)
        try:
            with urlopen(req, timeout=60) as response:
                payload = response.read()
            retry_html = payload.decode("utf-8", errors="replace")
            if not is_greenhouse_error_page(retry_html):
                html = retry_html
        except (HTTPError, URLError, OSError):
            pass

    if is_greenhouse_error_page(html):
        # Don't cache the error page — try cached fallbacks instead.
        cached = _try_cached_fallbacks()
        if cached is not None:
            return cached
        raise RuntimeError(f"job_closed: Greenhouse returned an unavailable application shell (error=true) at {url}")

    if cache_path is not None:
        cache_path.write_bytes(html.encode("utf-8"))

    return html


def _greenhouse_application_url(url: str, *, company_hint: str | None = None) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").removeprefix("www.").casefold()
    query = parsed.query.replace("?", "&")
    params = parse_qs(query)
    if (
        host
        not in (
            "job-boards.greenhouse.io",
            "job-boards.eu.greenhouse.io",
            "boards.greenhouse.io",
            "boards.eu.greenhouse.io",
        )
        and not (params.get("board") or [None])[0]
    ):
        try:
            canonical_url = canonical_greenhouse_job_url(url)
        except Exception:
            canonical_url = url
        canonical_parsed = urlparse(canonical_url)
        canonical_host = (canonical_parsed.hostname or "").removeprefix("www.").casefold()
        if canonical_host in ("job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"):
            canonical_parts = canonical_parsed.path.strip("/").split("/")
            if len(canonical_parts) >= 3 and canonical_parts[1] == "jobs":
                slug = canonical_parts[0]
                job_id_part = canonical_parts[2].split("?")[0]
                return f"https://boards.greenhouse.io/embed/job_app?for={slug}&token={job_id_part}"
            return canonical_url
        url = canonical_url
        parsed = canonical_parsed
        host = canonical_host

    if host in ("job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"):
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) >= 3 and path_parts[1] == "jobs":
            slug = path_parts[0]
            job_id_part = path_parts[2].split("?")[0]
            return f"https://boards.greenhouse.io/embed/job_app?for={slug}&token={job_id_part}"
        # Fall back to the original URL for non-job application shells such as error pages.
        return url

    # boards.greenhouse.io/{slug}/jobs/{id} — the slug is in the path.
    if host in ("boards.greenhouse.io", "boards.eu.greenhouse.io"):
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) >= 3 and path_parts[1] == "jobs":
            slug = path_parts[0]
            job_id_part = path_parts[2].split("?")[0]
            return f"https://boards.greenhouse.io/embed/job_app?for={slug}&token={job_id_part}"

    # Fix malformed query strings where ? appears instead of & (e.g. ?gh_jid=123?gh_src=foo)
    job_id = (params.get("gh_jid") or [None])[0]
    # Fall back to numeric IDs in the path (e.g. /detail/7598877/)
    if not job_id:
        path_parts = parsed.path.strip("/").split("/")
        for part in path_parts:
            if part.isdigit() and len(part) >= 6:
                job_id = part
                break
    if not job_id:
        return url

    board = (params.get("board") or [None])[0]
    if board:
        return f"https://boards.greenhouse.io/embed/job_app?for={board}&token={job_id}"

    # Build candidate slugs from the hostname, skipping generic subdomains.
    host_parts = host.split(".")
    tld_parts = {"com", "org", "net", "io", "co", "ai", "dev", "app", "us", "uk"}
    candidates: list[str] = []
    # Add company_hint first (most likely to match)
    if company_hint and company_hint not in candidates:
        candidates.append(company_hint)
    for part in host_parts:
        if part in GENERIC_SUBDOMAINS or part in tld_parts:
            continue
        if part not in candidates:
            candidates.append(part)
    # Also add the subdomain as a last-resort candidate
    if host_parts and host_parts[0] not in tld_parts:
        subdomain = host_parts[0]
        if subdomain not in candidates:
            candidates.append(subdomain)

    # Also try common suffix-stripped variants (e.g. datadoghq → datadog)
    _slug_suffixes = ("hq", "inc", "labs", "tech")
    stripped: list[str] = []
    for candidate in candidates:
        for suffix in _slug_suffixes:
            if candidate.endswith(suffix) and len(candidate) > len(suffix) + 2:
                s = candidate[: -len(suffix)]
                if s not in candidates:
                    stripped.append(s)
    candidates.extend(stripped)

    if not candidates:
        return url

    # Probe the Greenhouse API to find the correct board slug
    found = probe_greenhouse_board_slug(job_id, candidates)
    if found:
        return f"https://boards.greenhouse.io/embed/job_app?for={found}&token={job_id}"

    # Fallback: use the first candidate
    return f"https://boards.greenhouse.io/embed/job_app?for={candidates[0]}&token={job_id}"


class _GreenhouseEmbeddedApplicationParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.application_src: str | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if self.application_src is not None or tag.casefold() != "iframe":
            return
        attrs_dict = {str(key): str(value or "") for key, value in attrs}
        src = attrs_dict.get("src", "").strip()
        if not src:
            return
        iframe_id = attrs_dict.get("id", "").strip().casefold()
        if iframe_id == "grnhse_iframe" or "embed/job_app" in src or "job_app" in src:
            self.application_src = src


def _greenhouse_embedded_application_url(page_url: str, html: str) -> str | None:
    parser = _GreenhouseEmbeddedApplicationParser()
    parser.feed(str(html or ""))
    if not parser.application_src:
        return None
    return urljoin(str(page_url or "").strip(), parser.application_src)


def _resolved_greenhouse_source_url(meta: dict) -> str:
    """Choose the most trustworthy Greenhouse target URL from saved metadata."""
    candidate_keys = ("source_url", "jd_source", "board_url", "jd_source_resolved")
    greenhouse_hosts = {
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
        "boards.greenhouse.io",
        "boards.eu.greenhouse.io",
    }
    for key in candidate_keys:
        candidate = str(meta.get(key) or "").strip()
        if not candidate:
            continue
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").removeprefix("www.").casefold()
        if host in greenhouse_hosts:
            return candidate
        query = parsed.query.replace("?", "&")
        params = parse_qs(query)
        if (params.get("gh_jid") or [None])[0] or (params.get("board") or [None])[0]:
            return candidate
        try:
            canonical_candidate = canonical_greenhouse_job_url(candidate)
        except Exception:
            canonical_candidate = ""
        canonical_host = (urlparse(canonical_candidate).hostname or "").removeprefix("www.").casefold()
        if canonical_host in greenhouse_hosts:
            return candidate
    return preferred_meta_job_url(meta, keys=candidate_keys)


def _normalize_classic_form_text(value: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(value or "")).strip()


def _field_name_from_classic_attrs(field_id: str, field_name: str) -> str:
    if field_id:
        if field_id in {
            "first_name",
            "last_name",
            "email",
            "phone",
            "country",
            "resume_text",
            "cover_letter_text",
        }:
            return field_id
        if field_id.startswith("job_application_") and "answers_attributes" not in field_id:
            return field_id[len("job_application_") :]
        return field_id

    if field_name.startswith("job_application[") and field_name.endswith("]"):
        inner = field_name[len("job_application[") : -1]
        if "][" not in inner:
            return inner
    return field_name


class _GreenhouseClassicFormParser(HTMLParser):
    _CONTROL_TAGS = {"input", "select", "textarea", "option", "button"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.questions: list[dict] = []
        self._in_form = False
        self._form_depth = 0
        self._current_field: dict | None = None
        self._current_field_depth = 0
        self._in_label = False
        self._label_ignore_depth = 0
        self._current_option: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}

        if tag == "form":
            if not self._in_form and attrs_dict.get("id") in {"application_form", "application-form"}:
                self._in_form = True
                self._form_depth = 1
                return
            if self._in_form:
                self._form_depth += 1

        if not self._in_form:
            return

        if self._current_field is None and tag == "div":
            classes = set((attrs_dict.get("class") or "").split())
            if "field" in classes:
                self._current_field = {
                    "label_parts": [],
                    "required": False,
                    "control": None,
                }
                self._current_field_depth = 1
                return
        elif self._current_field is not None and tag == "div":
            self._current_field_depth += 1
            classes = set((attrs_dict.get("class") or "").split())
            data_field = attrs_dict.get("data-field", "")
            if "attach-or-paste" in classes and data_field in {"resume", "cover_letter"}:
                self._set_control(field_type="input_file", attrs={"name": data_field, "id": data_field})

        if self._current_field is None:
            return

        if tag == "label":
            self._in_label = True
            return

        if self._in_label and tag in self._CONTROL_TAGS:
            self._label_ignore_depth += 1

        if tag == "span" and "asterisk" in set((attrs_dict.get("class") or "").split()):
            self._current_field["required"] = True

        if tag == "input":
            input_type = (attrs_dict.get("type") or "text").casefold()
            if input_type == "hidden":
                return
            if input_type in {"submit", "button", "image"}:
                return
            field_type = "input_file" if input_type == "file" else "input_text"
            self._set_control(field_type=field_type, attrs=attrs_dict)
            return

        if tag == "textarea":
            self._set_control(field_type="textarea", attrs=attrs_dict)
            return

        if tag == "select":
            self._set_control(field_type="multi_value_single_select", attrs=attrs_dict)
            return

        if tag == "option" and self._current_field.get("control", {}).get("type") == "multi_value_single_select":
            self._current_option = {
                "value": attrs_dict.get("value", ""),
                "label_parts": [],
            }

    def handle_endtag(self, tag: str) -> None:
        if not self._in_form:
            return

        if self._current_field is not None:
            if tag == "option" and self._current_option is not None:
                label = _normalize_classic_form_text("".join(self._current_option["label_parts"]))
                value = str(self._current_option.get("value") or "").strip()
                if label and value:
                    self._current_field["control"]["values"].append({"label": label, "value": value})
                self._current_option = None

            if self._in_label and tag in self._CONTROL_TAGS and self._label_ignore_depth > 0:
                self._label_ignore_depth -= 1

            if tag == "label":
                self._in_label = False
                self._label_ignore_depth = 0

            if tag == "div":
                self._current_field_depth -= 1
                if self._current_field_depth == 0:
                    self._finalize_field()
                    self._current_field = None

        if tag == "form":
            self._form_depth -= 1
            if self._form_depth == 0:
                self._in_form = False

    def handle_data(self, data: str) -> None:
        if self._current_field is None:
            return
        if self._current_option is not None:
            self._current_option["label_parts"].append(data)
            return
        if self._in_label and self._label_ignore_depth == 0:
            self._current_field["label_parts"].append(data)

    def _set_control(self, *, field_type: str, attrs: dict[str, str]) -> None:
        assert self._current_field is not None
        if self._current_field.get("control") is not None:
            return
        if attrs.get("aria-required") == "true" or "required" in attrs:
            self._current_field["required"] = True
        self._current_field["control"] = {
            "name": attrs.get("name", ""),
            "id": attrs.get("id", ""),
            "type": field_type,
            "values": [],
        }

    def _finalize_field(self) -> None:
        assert self._current_field is not None
        control = self._current_field.get("control")
        if not control:
            return

        label = _normalize_classic_form_text("".join(self._current_field["label_parts"]))
        label = re.sub(r"\s*\*\s*$", "", label).strip()
        if not label:
            return

        field_name = _field_name_from_classic_attrs(
            str(control.get("id") or ""),
            str(control.get("name") or ""),
        )
        if not field_name:
            return

        field = {
            "name": field_name,
            "type": control["type"],
        }
        if control["type"] == "multi_value_single_select":
            field["values"] = control["values"]

        self.questions.append(
            {
                "label": label,
                "description": "",
                "required": bool(self._current_field.get("required")),
                "fields": [field],
            }
        )


def _extract_classic_form_job_post(html: str) -> dict:
    parser = _GreenhouseClassicFormParser()
    parser.feed(html)
    if not parser.questions:
        raise ValueError("Could not parse Greenhouse classic application form fields")
    return {
        "questions": parser.questions,
        "eeoc_sections": [],
    }


def _extract_job_post(html: str) -> dict:
    match = REMIX_CONTEXT_RE.search(html)
    if not match:
        return _extract_classic_form_job_post(html)

    context = json.loads(match.group(1))
    loader_data = context["state"]["loaderData"]
    # Try the standard route key first, then search for any route containing jobPost
    for key in (
        "routes/$url_token_.jobs_.$job_post_id",
        "routes/jobs.$job_post_id",
    ):
        if key in loader_data and "jobPost" in (loader_data[key] or {}):
            return loader_data[key]["jobPost"]
    # Fallback: search all routes for one containing jobPost
    for _key, value in loader_data.items():
        if isinstance(value, dict) and "jobPost" in value:
            return value["jobPost"]
    # No jobPost found in loaderData — try classic extraction
    return _extract_classic_form_job_post(html)


def _find_output_dir(target: str) -> Path:
    candidate = Path(target)
    if candidate.exists():
        if candidate.is_dir():
            meta_path = candidate / ".pipeline_meta.json"
            if not meta_path.exists():
                raise FileNotFoundError(f"{candidate} does not contain .pipeline_meta.json")
            resolved = candidate.resolve()
            migrate_role_output_layout(resolved)
            return resolved
        raise FileNotFoundError(f"{candidate} is not a directory")

    if URL_RE.match(target):
        for meta_path in PROJECT_ROOT.glob("output/*/*/.pipeline_meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if meta.get("jd_source") == target:
                resolved = meta_path.parent.resolve()
                migrate_role_output_layout(resolved)
                return resolved
        raise FileNotFoundError(f"No output directory found for job URL: {target}")

    raise FileNotFoundError(f"Could not resolve target: {target}")


def _load_meta(out_dir: Path) -> dict:
    return load_pipeline_meta(out_dir)


def _load_notion_sync_module():
    script_path = PROJECT_ROOT / "scripts" / "notion_sync.py"
    spec = importlib.util.spec_from_file_location("job_assets_notion_sync", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load Notion sync helper from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sync_notion_after_submit(
    payload: dict,
    outcome: dict,
    *,
    email_confirmation: dict | None = None,
    min_received_at_utc: datetime | str | None = None,
) -> dict:
    notion_sync = _load_notion_sync_module()
    out_dir = Path(payload["out_dir"])
    notion_sync.record_website_confirmation(out_dir, outcome, provider="greenhouse")
    wait_for_email_seconds = int(os.environ.get("NOTION_SYNC_EMAIL_WAIT_SECONDS", "90"))
    kwargs = {
        "wait_for_email_seconds": wait_for_email_seconds,
        "allow_pending_email": True,
        "fail_on_missing_token": False,
    }
    if email_confirmation is not None:
        kwargs["email_confirmation"] = email_confirmation
    if min_received_at_utc is not None:
        kwargs["min_received_at_utc"] = min_received_at_utc
    result = notion_sync.sync_application(
        out_dir,
        **kwargs,
    )
    reply_to_confirmation_email(payload, board_name="greenhouse", email_confirmation=email_confirmation)
    return result


def _find_resume_file(out_dir: Path) -> Path | None:
    from application_submit_common import find_resume_file

    try:
        return find_resume_file(out_dir)
    except FileNotFoundError:
        return None


def _find_cover_letter_text(out_dir: Path) -> str:
    from application_submit_common import find_cover_letter_text

    try:
        return find_cover_letter_text(out_dir)
    except FileNotFoundError:
        return ""


def _find_cover_letter_file(out_dir: Path) -> Path:
    from application_submit_common import find_cover_letter_file

    return find_cover_letter_file(out_dir)


def _find_question(questions: list[dict], label_fragment: str) -> dict:
    fragment = label_fragment.casefold()
    for question in questions:
        if fragment in question["label"].casefold():
            return question
    raise KeyError(f"Could not find Greenhouse question containing {label_fragment!r}")


def _field_name(question: dict) -> str:
    return question["fields"][0]["name"]


def _question_label(question: dict) -> str:
    return question["label"].strip()


def _question_description(question: dict) -> str:
    return _strip_html(question.get("description"))


def _question_type(question: dict) -> str:
    return question["fields"][0]["type"]


def _question_text(question: dict) -> str:
    return f"{_question_label(question)} {_question_description(question)}".strip().casefold()


def _question_field_name(question: dict) -> str:
    return str(question["fields"][0]["name"]).strip()


def _question_matches(question: dict, *fragments: str) -> bool:
    text = _normalize_free_text(_question_text(question))
    for fragment in fragments:
        normalized_fragment = _normalize_free_text(fragment)
        if not normalized_fragment:
            continue
        pattern = rf"(?<![a-z0-9]){re.escape(normalized_fragment)}(?![a-z0-9])"
        if re.search(pattern, text):
            return True
    return False


def _normalize_free_text(value: str) -> str:
    return NORMALIZED_TEXT_RE.sub(" ", value.casefold()).strip()


_GREENHOUSE_DISCOVERED_DEMOGRAPHIC_FIELD_FRAGMENTS = (
    ("gender identity", "gender_identity"),
    ("pronoun", "pronouns"),
    ("do you identify as transgender", "transgender_status"),
    ("identify as transgender", "transgender_status"),
    ("transgender", "transgender_status"),
    ("i identify as", "transgender_status"),
    ("what gender do you identify as", "gender"),
    ("gender", "gender"),
    ("race ethnicity", "race"),
    ("racial ethnic background", "race"),
    ("identify your race", "race"),
    ("race and", "race"),
    ("race ethnic", "race"),
    ("race select", "race"),
    ("race", "race"),
    ("ethnicity", "race"),
    ("ethnicities", "race"),
    ("ethnic group", "race"),
    ("categories describe you", "race"),
    ("hispanic", "hispanic_ethnicity"),
    ("highest level of school", "education_level"),
    ("highest degree", "education_level"),
    ("employment status", "employment_status"),
    ("sexual orientation", "sexual_orientation"),
    ("lgbt", "sexual_orientation"),
    ("communities", "communities"),
    ("community", "communities"),
    ("protected veteran", "veteran_status"),
    ("veteran status", "veteran_status"),
    ("veteran or active member", "veteran_status"),
    ("veteran active member", "veteran_status"),
    ("military status", "veteran_status"),
    ("armed forces", "veteran_status"),
    ("military", "veteran_status"),
    ("disability", "disability_status"),
    ("cc 305", "disability_status"),
    ("how did you hear", "how_did_you_hear"),
    ("how did you first hear about", "how_did_you_hear"),
    ("how did you learn about", "how_did_you_hear"),
    ("how did you find out", "how_did_you_hear"),
    ("how did you find this position", "how_did_you_hear"),
    ("how did you find this role", "how_did_you_hear"),
    ("how did you find this opportunity", "how_did_you_hear"),
    ("how did you find this job", "how_did_you_hear"),
    ("where did you hear about", "how_did_you_hear"),
    ("first generation", "first_generation_professional"),
    ("worked for", "worked_for_company"),
    ("previously employed", "worked_for_company"),
    ("previously been employed", "worked_for_company"),
    ("former", "worked_for_company"),
    ("employed by", "worked_for_company"),
    ("languages you speak", "languages_spoken"),
    ("languages do you speak", "languages_spoken"),
    ("what is your age", "age_group"),
    ("age range", "age_group"),
    ("age group", "age_group"),
    ("which age group", "age_group"),
)


def _greenhouse_discovered_demographic_field_name(heading: str) -> str | None:
    normalized_heading = _normalize_free_text(heading)
    return next(
        (
            field_name
            for fragment, field_name in _GREENHOUSE_DISCOVERED_DEMOGRAPHIC_FIELD_FRAGMENTS
            if fragment in normalized_heading
        ),
        None,
    )


def _greenhouse_discovered_demographic_desired_value(field_name: str, heading: str, application_profile) -> str | None:
    normalized_heading = _normalize_free_text(heading)
    if field_name == "transgender_status":
        return application_profile.transgender_status or "Not transgender"
    if field_name == "gender_identity":
        return (
            getattr(application_profile, "gender_identity", None)
            or application_profile.gender
            or "I don't wish to answer"
        )
    if field_name == "pronouns":
        return application_profile.pronouns or "I don't wish to answer"
    if field_name == "gender":
        return (
            application_profile.gender
            or getattr(application_profile, "gender_identity", None)
            or "I don't wish to answer"
        )
    if field_name == "race":
        return application_profile.race_or_ethnicity or "I don't wish to answer"
    if field_name == "hispanic_ethnicity":
        return (
            "Yes"
            if application_profile.race_or_ethnicity
            and any(
                term in _normalize_free_text(application_profile.race_or_ethnicity)
                for term in ("hispanic", "latino", "latinx")
            )
            else "No"
        )
    if field_name == "education_level":
        return "Graduate degree"
    if field_name == "employment_status":
        return "Full-time employment"
    if field_name == "sexual_orientation":
        if "lgbt" in normalized_heading:
            return (
                "No"
                if application_profile.sexual_orientation
                and any(
                    term in _normalize_free_text(application_profile.sexual_orientation)
                    for term in ("straight", "heterosexual")
                )
                else "I don't wish to answer"
            )
        return application_profile.sexual_orientation or "I don't wish to answer"
    if field_name == "communities":
        return getattr(application_profile, "communities", None) or "None of the above"
    if field_name == "veteran_status":
        return application_profile.veteran_status or "I don't wish to answer"
    if field_name == "disability_status":
        return application_profile.disability_status or "I do not want to answer"
    if field_name == "how_did_you_hear":
        return application_profile.how_did_you_hear or "Corporate website"
    if field_name == "first_generation_professional":
        return "No"
    if field_name == "worked_for_company":
        return "No"
    if field_name == "languages_spoken":
        return "English,Spanish,Mandarin,Cantonese,Chinese"
    if field_name == "age_group":
        return application_profile.age_range
    return None


def _match_greenhouse_discovered_demographic_question(heading: str, application_profile) -> tuple[str, str] | None:
    normalized_heading = _normalize_free_text(heading)
    asks_sponsorship = question_requests_sponsorship_requirement(heading)
    asks_authorization = any(
        fragment in normalized_heading
        for fragment in (
            "authorized to work",
            "authorised to work",
            "authorization to work",
            "authorisation to work",
            "work authorization",
            "work authorisation",
            "legally authorized",
            "legally authorised",
            "right to work",
            "eligible to work",
        )
    )
    if asks_sponsorship:
        return (
            "sponsorship",
            "Yes"
            if application_profile.require_sponsorship_now or application_profile.require_sponsorship_future
            else "No",
        )
    if asks_authorization:
        return ("work_authorization", "Yes" if application_profile.authorized_to_work_unconditionally else "No")

    field_name = _greenhouse_discovered_demographic_field_name(heading)
    if not field_name:
        return None
    desired = _greenhouse_discovered_demographic_desired_value(field_name, heading, application_profile)
    if not desired:
        return None
    return (field_name, desired)


_GREENHOUSE_DISCOVERED_MULTI_VALUE_FIELD_NAMES = frozenset({"communities", "languages_spoken"})


def _greenhouse_discovered_combobox_desired_values(field_name: str, desired: str) -> list[str]:
    if field_name not in _GREENHOUSE_DISCOVERED_MULTI_VALUE_FIELD_NAMES:
        return [desired.strip()] if desired.strip() else []
    return [value.strip() for value in desired.split(",") if value.strip()]


def _greenhouse_discovered_group_key(
    field_name: str,
    *,
    control_id: str | None = None,
    label: str | None = None,
) -> str:
    normalized_control_id = _normalize_free_text(control_id or "")
    if normalized_control_id:
        return f"{field_name}:{normalized_control_id}"
    normalized_label = _normalize_free_text(label or "")
    if normalized_label:
        return f"{field_name}:{normalized_label}"
    return field_name


_VISIBLE_DISCOVERED_PROFILE_FIELDS = {
    "work_authorization": "work_authorization",
    "sponsorship": "sponsorship",
}


def _mark_visible_discovered_greenhouse_step(step: dict | None) -> dict | None:
    step = _mark_visible_self_id_greenhouse_step(step)
    if step is None:
        return None
    blocker_kind = str(step.get("blocker_kind") or "").strip()
    if blocker_kind:
        return step
    profile_field = str(step.get("profile_field") or "").strip() or _VISIBLE_DISCOVERED_PROFILE_FIELDS.get(
        str(step.get("field_name") or "")
    )
    if not profile_field:
        return step
    return mark_visible_profile_field_step(step, profile_field=profile_field)


def _greenhouse_runtime_confirmation_step_from_discovered_group(
    *,
    answer: dict[str, object],
    application_profile: ApplicationProfile,
    page_index: int,
    existing_step: dict[str, object] | None = None,
) -> dict[str, object] | None:
    if answer.get("visible") is False:
        return None
    heading = str(answer.get("heading") or "").strip()
    if not heading:
        return None

    matched = _match_greenhouse_discovered_demographic_question(heading, application_profile)
    if matched is not None:
        field_name, desired = matched
    else:
        field_name = _greenhouse_discovered_demographic_field_name(heading)
        if not field_name:
            return None
        desired = _greenhouse_discovered_demographic_desired_value(field_name, heading, application_profile)
        if not desired:
            return None

    observed_value = str(answer.get("value") or "").strip()
    kind = str((existing_step or {}).get("kind") or answer.get("kind") or "combobox").strip() or "combobox"
    step: dict[str, object] = {
        "kind": kind,
        "field_name": field_name,
        "label": heading,
        "optional": False,
        "source": str((existing_step or {}).get("source") or "application_profile.md"),
        "page_index": page_index,
    }
    if observed_value:
        step["value"] = observed_value
        step["filled"] = True
        step["status"] = "filled"
    else:
        step["value"] = desired
        step["observed_value"] = ""
        step["status"] = "planned"
        step["reason"] = (
            "Autofill planned a value for this field but could not confirm that the value was present on the "
            "live application form."
        )
        step["note"] = f"Live form showed no selected value instead of expected {desired!r}."
    return _mark_visible_discovered_greenhouse_step(step)


SCHOOL_QUALIFIER_TOKENS = {
    "arts",
    "business",
    "campus",
    "college",
    "engineering",
    "graduate",
    "institute",
    "main",
    "of",
    "penn",
    "school",
    "sciences",
    "the",
    "university",
    "wharton",
}


def _is_school_field_name(field_name: str) -> bool:
    normalized = _normalize_free_text(field_name)
    return "school name" in normalized or "school_name" in field_name.casefold()


def _school_option_score(desired: str, option_text: str) -> tuple[int, int] | None:
    desired_normalized = _normalize_free_text(desired)
    option_normalized = _normalize_free_text(option_text)
    if not desired_normalized or not option_normalized:
        return None
    if option_normalized == desired_normalized:
        return (3, 0)
    if option_normalized.endswith(desired_normalized):
        prefix = option_normalized[: -len(desired_normalized)].strip()
        prefix_tokens = [token for token in prefix.split() if token]
        if prefix_tokens and all(token in SCHOOL_QUALIFIER_TOKENS for token in prefix_tokens):
            return (2, -len(prefix_tokens))
    return None


def _location_state_variants(location: str) -> set[str]:
    parts = [part.strip() for part in location.split(",") if part.strip()]
    if len(parts) < 2:
        return set()

    state = parts[1]
    normalized_state = _normalize_free_text(state)
    variants = {normalized_state}
    if len(state) == 2:
        expanded_state = STATE_NAME_MAP.get(state.upper())
        if expanded_state:
            variants.add(_normalize_free_text(expanded_state))
    else:
        for abbreviation, name in STATE_NAME_MAP.items():
            if _normalize_free_text(name) == normalized_state:
                variants.add(_normalize_free_text(abbreviation))
                break
    return {variant for variant in variants if variant}


def _question_targets_explicit_state_list(question: dict) -> bool:
    combined = " ".join(str(question.get(key) or "").strip() for key in ("label", "description"))
    normalized = _normalize_free_text(combined)
    return any(
        phrase in normalized
        for phrase in (
            "one of the following states",
            "one of these states",
            "any of these states",
            "any of the following states",
        )
    )


def _listed_state_names(question: dict) -> set[str]:
    combined = " ".join(str(question.get(key) or "").strip() for key in ("label", "description"))
    normalized = _normalize_free_text(combined)
    matches = {
        _normalize_free_text(state_name)
        for state_name in STATE_NAME_MAP.values()
        if re.search(rf"\b{re.escape(_normalize_free_text(state_name))}\b", normalized)
    }
    return {match for match in matches if match}


def _state_membership_answer(question: dict, application_profile: ApplicationProfile) -> bool | None:
    if not _question_targets_explicit_state_list(question):
        return None

    listed_states = _listed_state_names(question)
    if not listed_states:
        return None

    candidate_state_variants = _location_state_variants(application_profile.location)
    if not candidate_state_variants:
        return None

    return any(candidate_state in listed_states for candidate_state in candidate_state_variants)


def _candidate_state_text(application_profile: ApplicationProfile) -> str | None:
    parts = [part.strip() for part in application_profile.location.split(",") if part.strip()]
    if len(parts) < 2:
        return None
    state = parts[1].strip()
    return state or None


def _candidate_state_option_label(field: dict, application_profile: ApplicationProfile) -> str:
    candidate_state_variants = _location_state_variants(application_profile.location)
    if not candidate_state_variants:
        raise ValueError(
            f"Could not derive a parseable state from application_profile.md location {application_profile.location!r}."
        )

    labels = [
        str(option.get("label") or "").strip()
        for option in field.get("values", [])
        if str(option.get("label") or "").strip()
    ]
    for label in labels:
        if _normalize_free_text(label) in candidate_state_variants:
            return label
    for label in labels:
        normalized_label = _normalize_free_text(label)
        if any(re.search(rf"\b{re.escape(variant)}\b", normalized_label) for variant in candidate_state_variants):
            return label

    raise ValueError(
        "Could not match candidate state from application_profile.md "
        f"location {application_profile.location!r} against {[label for label in labels]!r}."
    )


def _candidate_location_option_label(field: dict, application_profile: ApplicationProfile) -> str:
    for desired in _location_match_candidates(application_profile.location, application_profile.country):
        try:
            return _match_option_label(field, desired)
        except ValueError:
            continue
    raise ValueError(
        "Could not match candidate location from application_profile.md "
        f"location {application_profile.location!r} against "
        f"{[str(option.get('label') or '').strip() for option in field.get('values', [])]!r}."
    )


def _salary_comfort_option_label(field: dict, comfortable: bool) -> str:
    try:
        return _yes_no_option_label(field, comfortable)
    except ValueError:
        pass

    labels = [str(option["label"]).strip() for option in field.get("values", [])]
    normalized_labels = [(label, _normalize_free_text(label)) for label in labels]

    if comfortable:
        for label, normalized in normalized_labels:
            if "not comfortable" in normalized:
                continue
            if "comfortable" in normalized or ("within" in normalized and "range" in normalized):
                return label
    else:
        for label, normalized in normalized_labels:
            if "not comfortable" in normalized or ("outside" in normalized and "range" in normalized):
                return label

    if labels:
        return labels[0]
    raise ValueError(f"Could not determine a salary-comfort option for field {field.get('name')}")


def discovered_field_step(field: dict) -> dict[str, str]:
    field_name = str(field.get("field_name") or "").strip()
    label = str(field.get("label") or "").strip()
    selector = ""
    if field_name:
        escaped = field_name.replace("\\", "\\\\").replace('"', '\\"')
        selector = f'[id="{escaped}"]'
    field_role = _normalize_free_text(str(field.get("role") or ""))
    field_type = _normalize_free_text(str(field.get("type") or ""))
    kind = "combobox" if field_role == "combobox" or field_type == "select" else "text"
    return {
        "kind": kind,
        "field_name": field_name,
        "label": label,
        "selector": selector,
    }


def _classify_submission_snapshot(snapshot: dict) -> dict[str, object]:
    url = str(snapshot.get("url") or "")
    page_text = str(snapshot.get("page_text") or "")
    page_title = str(snapshot.get("page_title") or "")
    normalized_text = _normalize_free_text(page_text)
    normalized_title = _normalize_free_text(page_title)
    errors = [error.strip() for error in snapshot.get("errors", []) or [] if isinstance(error, str) and error.strip()]
    invalid_fields = [
        field.strip() for field in snapshot.get("invalid_fields", []) or [] if isinstance(field, str) and field.strip()
    ]
    form_visible = bool(snapshot.get("form_visible"))
    security_code_visible = bool(snapshot.get("security_code_visible"))
    confirmation_visible = bool(snapshot.get("confirmation_visible"))
    page_level_errors = [
        match.group(0) for pattern in SUBMISSION_ERROR_TEXT_PATTERNS for match in [pattern.search(page_text)] if match
    ]
    security_code_error_texts = [
        _normalize_free_text(value)
        for value in [*errors, *page_level_errors]
        if isinstance(value, str) and value.strip()
    ]
    has_invalid_security_code_error = any(
        "security code" in value and any(token in value for token in ("valid", "invalid", "incorrect", "expired"))
        for value in security_code_error_texts
    )

    if confirmation_visible:
        return {"status": "confirmed", "reason": "selector"}
    if security_code_visible and not has_invalid_security_code_error:
        return {"status": "security_code_required"}
    if form_visible and (errors or invalid_fields or page_level_errors):
        combined_errors = list(dict.fromkeys([*errors, *page_level_errors]))
        return {
            "status": "validation_error",
            "errors": combined_errors,
            "invalid_fields": invalid_fields,
        }
    if SUBMISSION_CONFIRM_URL_RE.search(url):
        return {"status": "confirmed", "reason": "url"}
    if any(pattern.search(page_text) for pattern in SUBMISSION_CONFIRM_TEXT_PATTERNS):
        if (
            not form_visible
            or "thank you for applying" in normalized_text
            or "thank you for your applying" in normalized_text
            or "application has been submitted" in normalized_text
            or "under review" in normalized_text
        ):
            return {"status": "confirmed", "reason": "text"}
    if any(pattern.search(page_title) for pattern in SUBMISSION_CONFIRM_TEXT_PATTERNS):
        if not form_visible or "thank you for applying" in normalized_title:
            return {"status": "confirmed", "reason": "text"}
    if not form_visible and any(pattern.search(page_text) for pattern in SUBMISSION_CONFIRM_TEXT_PATTERNS):
        return {"status": "confirmed", "reason": "form_hidden"}
    return {"status": "pending", "errors": errors, "invalid_fields": invalid_fields}


def _greenhouse_file_upload_key(label: str | None) -> str | None:
    label_text = str(label or "").casefold()
    if "resume" in label_text:
        return "resume"
    if "cover letter" in label_text:
        return "cover_letter"
    return None


def _greenhouse_file_upload_confirmed(snapshot: dict[str, object], expected_name: str) -> bool:
    expected = expected_name.strip()
    if not expected:
        return False

    widget_text = str(snapshot.get("widget_text") or "")
    chosen_text = str(snapshot.get("chosen_text") or "")
    body_text = str(snapshot.get("body_text") or "")
    matching_file_input = bool(snapshot.get("matching_file_input"))
    widget_present = bool(snapshot.get("widget_present"))

    if expected in widget_text or expected in chosen_text or expected in body_text:
        return True
    if widget_present:
        return False
    return matching_file_input


def _greenhouse_file_upload_confirmation_args(step: dict) -> dict[str, str | None]:
    return {"uploadKey": _greenhouse_file_upload_key(step.get("label")), "expectedName": Path(step["file_path"]).name}


def _wait_for_greenhouse_file_upload_confirmation(page, step: dict, *, timeout_ms: int = 3000) -> bool:
    try:
        page.wait_for_function(
            """({ uploadKey, expectedName }) => {
                const text = node => (node?.textContent || "").replace(/\\s+/g, " ").trim();
                const matchingFileInput = Array.from(document.querySelectorAll('input[type="file"]')).find(input =>
                  Array.from(input.files || []).some(file => (file?.name || "") === expectedName));
                const widget = (uploadKey && (document.querySelector(`[aria-labelledby="upload-label-${uploadKey}"]`)
                  || document.querySelector(`#upload-label-${uploadKey}`)?.closest('.file-upload')))
                  || matchingFileInput?.closest?.('.file-upload') || null;
                const chosenText = `${text(uploadKey ? document.querySelector(`#${uploadKey}_filename`) : null)} ${text(uploadKey ? document.querySelector(`#${uploadKey}_chosen`) : null)}`.trim();
                return !!(expectedName && (text(widget).includes(expectedName) || chosenText.includes(expectedName)
                  || text(document.body).includes(expectedName) || (!widget && matchingFileInput)));
            }""",
            arg=_greenhouse_file_upload_confirmation_args(step),
            timeout=timeout_ms,
        )
        return True
    except Exception as exc:
        if exc.__class__.__name__ == "TimeoutError":
            return False
        raise


def _greenhouse_combobox_display_text(
    raw_input_value: str | None,
    single_value: str | None = None,
    multi_values: list[str] | None = None,
    placeholder_text: str | None = None,
    menu_expanded: bool = False,
) -> str:
    chips = [str(value or "").strip() for value in (multi_values or []) if str(value or "").strip()]
    if chips:
        return ", ".join(chips)

    visible_single_value = str(single_value or "").strip()
    if visible_single_value:
        return visible_single_value

    input_value = str(raw_input_value or "").strip()
    if not input_value or _normalize_free_text(input_value).startswith("select"):
        return ""

    if menu_expanded:
        return ""

    placeholder = _normalize_free_text(placeholder_text or "")
    if placeholder.startswith("select"):
        return ""

    return input_value


def _greenhouse_combobox_candidate_listbox_ids(
    control_id: str | None,
    aria_controls: str | None,
    aria_owns: str | None,
) -> list[str]:
    listbox_ids: list[str] = []
    for raw_value in (aria_controls, aria_owns):
        for candidate in str(raw_value or "").split():
            trimmed = candidate.strip()
            if trimmed and trimmed not in listbox_ids:
                listbox_ids.append(trimmed)
    normalized_control_id = str(control_id or "").strip()
    if normalized_control_id:
        inferred_id = f"react-select-{normalized_control_id}-listbox"
        if inferred_id not in listbox_ids:
            listbox_ids.append(inferred_id)
    return listbox_ids


def _greenhouse_option_text_matches(field_name: str, desired: str, option_text: str) -> bool:
    normalized = _normalize_free_text(option_text)
    if not normalized:
        return False
    if _is_school_field_name(field_name):
        return _school_option_score(desired, option_text) is not None
    desired_candidates = _normalized_option_match_candidates(field_name, desired)
    if any(candidate == normalized for candidate in desired_candidates):
        return True
    if any(re.search(rf"\b{re.escape(candidate)}\b", normalized) for candidate in desired_candidates):
        return True
    return bool(any(normalized in candidate for candidate in desired_candidates))


def _greenhouse_combobox_value_matches_expected(field_name: str, desired: str, observed: str | None) -> bool:
    normalized = _normalize_free_text(observed or "")
    if not normalized or normalized.startswith("select"):
        return False
    return _greenhouse_option_text_matches(field_name, desired, str(observed or ""))


def _confirmed_combobox_selection_value(field_name: str, desired: str, observed: str | None) -> str | None:
    observed_value = str(observed or "").strip()
    if _greenhouse_combobox_value_matches_expected(field_name, desired, observed_value):
        return observed_value
    return None


def _greenhouse_discovered_combobox_can_skip_open(
    field_name: str,
    desired_values: list[str],
    current_option_text: str | None,
) -> bool:
    return len(desired_values) == 1 and _greenhouse_combobox_value_matches_expected(
        field_name,
        desired_values[0],
        current_option_text,
    )


def _match_option_label(field: dict, desired: str) -> str:
    field_name = str(field.get("name") or "")
    desired_candidates = _normalized_option_match_candidates(field_name, desired)
    labels = [str(option["label"]).strip() for option in field.get("values", [])]

    for label in labels:
        if _normalize_free_text(label) in desired_candidates:
            return label

    # Fuzzy match — prefer the shortest matching label to avoid
    # e.g. "United States Minor Outlying Islands" winning over "United States".
    # Use the same boundary-safe semantics as live combobox confirmation so
    # profile values like "Male" do not get swallowed by labels such as "Female".
    fuzzy_matches: list[tuple[str, int]] = []
    for label in labels:
        normalized = _normalize_free_text(label)
        if _greenhouse_option_text_matches(field_name, desired, label):
            fuzzy_matches.append((label, len(normalized)))
    if fuzzy_matches:
        fuzzy_matches.sort(key=lambda item: item[1])
        return fuzzy_matches[0][0]

    raise ValueError(f"Could not match option {desired!r} for field {field.get('name')} against {labels!r}")


def _single_option_label(field: dict) -> str:
    labels = [
        str(option["label"]).strip() for option in field.get("values", []) if str(option.get("label") or "").strip()
    ]
    if len(labels) == 1:
        return labels[0]
    # For confirmation/acknowledgment questions with Yes/No options, select Yes.
    yes_label = next((label for label in labels if _normalize_free_text(label) == "yes"), None)
    if yes_label:
        return yes_label
    if labels:
        return labels[0]
    raise ValueError(f"Expected at least one option for field {field.get('name')}, got {labels!r}")


def _non_numeric_compensation_option_label(field: dict) -> str | None:
    labels = [
        str(option["label"]).strip() for option in field.get("values", []) if str(option.get("label") or "").strip()
    ]
    preferred_fragments = (
        "open",
        "flexible",
        "negotiable",
        "discuss",
        "competitive",
        "depends",
    )
    for label in labels:
        normalized = _normalize_free_text(label)
        if any(fragment in normalized for fragment in preferred_fragments) and not re.search(r"\d", normalized):
            return label
    return None


def _how_did_you_hear_option_label(
    field: dict,
    application_profile: ApplicationProfile,
    company_name: str,
    *,
    job_url: str | None = None,
    source_url: str | None = None,
    source_hint: str | None = None,
) -> str:
    option_labels = [
        str(option["label"]).strip() for option in field.get("values", []) if str(option.get("label") or "").strip()
    ]
    desired_values, _ = resolve_how_did_you_hear_option_candidates(
        application_profile,
        option_labels,
        company_name=company_name,
        job_url=job_url,
        source_url=source_url,
        source_hint=source_hint,
        prefer_metadata_job_board_matches=True,
    )
    for desired in desired_values:
        if not desired:
            continue
        try:
            return _match_option_label(field, desired)
        except ValueError:
            continue
    raise ValueError(f"Could not determine a 'How did you hear about us?' option for {field.get('name')}")


def _no_current_professional_license_option_label(field: dict) -> str:
    for desired in ("None", "None of the above", "Not Applicable", "N/A", "NA"):
        try:
            return _match_option_label(field, desired)
        except ValueError:
            continue
    raise ValueError(f"Could not determine a no-license option for field {field.get('name')}")


def _location_option_label(
    field: dict, application_profile: ApplicationProfile, *, role_location: str | None = None
) -> str:
    # Prefer the candidate's current city first. This preserves the
    # San Francisco-first office preference when the candidate is SF-based.
    values = list(field.get("values", []))
    for desired in _location_match_candidates(application_profile.location, application_profile.country):
        try:
            return _match_option_label(field, desired)
        except ValueError:
            continue

    candidate_markers = [
        _normalize_free_text(candidate)
        for candidate in _location_match_candidates(application_profile.location, application_profile.country)
        if _normalize_free_text(candidate)
    ]
    for option in values:
        label = str(option.get("label", "")).strip()
        normalized = _normalize_free_text(label)
        if normalized and any(marker in normalized for marker in candidate_markers):
            return label

    if role_location:
        for desired in _location_match_candidates(role_location):
            try:
                return _match_option_label(field, desired)
            except ValueError:
                continue

    # Last resort: first non-Remote, non-placeholder option
    for option in values:
        label = str(option.get("label", "")).strip()
        norm = _normalize_free_text(label)
        if norm and norm not in ("remote", "select", "please select", "other"):
            return label
    raise ValueError(
        f"Could not determine a location option for {field.get('name')} from {application_profile.location!r}"
    )


_MASTER_RESUME_ROLE_HEADER_RE = re.compile(r"^[A-Z0-9&'().,/ -]+ — .+$")
_MASTER_RESUME_DATE_RANGE_RE = re.compile(
    r"(?P<start_month>[A-Za-z]{3,9})\s+(?P<start_year>\d{4})\s*[–-]\s*"
    r"(?:(?P<end_month>[A-Za-z]{3,9})\s+(?P<end_year>\d{4})|(?P<present>Present))"
)
_PRODUCT_MANAGEMENT_TITLE_MARKERS = ("product manager", "product management")
_TECHNICAL_PM_MARKERS = (
    "api",
    "architecture",
    "automation",
    "cloud",
    "complex integration",
    "complex integrations",
    "developer",
    "infrastructure",
    "integration",
    "integrations",
    "platform",
    "platforms",
    "system",
    "systems",
    "technical",
    "workflow",
    "workflows",
)


def _resume_month_year(month_text: str, year_text: str) -> tuple[int, int] | None:
    try:
        parsed = datetime.strptime(f"{month_text} {year_text}", "%B %Y")
    except ValueError:
        try:
            parsed = datetime.strptime(f"{month_text} {year_text}", "%b %Y")
        except ValueError:
            return None
    return parsed.year, parsed.month


def _inclusive_months_between(
    start: tuple[int, int],
    end: tuple[int, int],
) -> int:
    start_year, start_month = start
    end_year, end_month = end
    return max(0, (end_year - start_year) * 12 + (end_month - start_month) + 1)


def _candidate_product_management_experience_years(
    *,
    technical_focus_only: bool,
    now: datetime | None = None,
) -> float | None:
    try:
        resume_text = MASTER_RESUME_PATH.read_text(encoding="utf-8")
    except OSError:
        return None

    today = now.astimezone() if now is not None and now.tzinfo else datetime.now().astimezone()
    lines = resume_text.splitlines()
    total_months = 0
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        normalized_line = _normalize_free_text(line)
        if not line or not _MASTER_RESUME_ROLE_HEADER_RE.match(line):
            index += 1
            continue
        if not any(marker in normalized_line for marker in _PRODUCT_MANAGEMENT_TITLE_MARKERS):
            index += 1
            continue

        block_lines: list[str] = []
        block_index = index + 1
        date_match = None
        while block_index < len(lines):
            candidate_line = lines[block_index].strip()
            if candidate_line and _MASTER_RESUME_ROLE_HEADER_RE.match(candidate_line):
                break
            if date_match is None:
                date_match = _MASTER_RESUME_DATE_RANGE_RE.search(candidate_line)
            block_lines.append(candidate_line)
            block_index += 1

        if date_match is not None:
            block_text = _normalize_free_text(" ".join(block_lines))
            if not technical_focus_only or any(marker in block_text for marker in _TECHNICAL_PM_MARKERS):
                start = _resume_month_year(date_match.group("start_month"), date_match.group("start_year"))
                if date_match.group("present"):
                    end = (today.year, today.month)
                else:
                    end = _resume_month_year(date_match.group("end_month"), date_match.group("end_year"))
                if start is not None and end is not None:
                    total_months += _inclusive_months_between(start, end)

        index = block_index

    if total_months <= 0:
        return None
    return total_months / 12


def _question_requests_product_management_experience_range(question: dict) -> bool:
    normalized = _normalize_free_text(_question_text(question))
    return bool(
        normalized.startswith("how many years of experience do you have in product management")
        and "product management" in normalized
        and "years of experience" in normalized
    )


def _question_requests_technical_pm_experience(question: dict) -> bool:
    normalized = _normalize_free_text(_question_text(question))
    return any(
        fragment in normalized
        for fragment in (
            "technical platform",
            "technical platforms",
            "complex integration",
            "complex integrations",
        )
    )


def _experience_years_option_label(field: dict, years: float) -> str:
    for option in field.get("values", []):
        label = str(option.get("label", "")).strip()
        normalized = _normalize_free_text(label)
        if not normalized:
            continue
        if "no experience" in normalized and years <= 0:
            return label
        if "less than 1 year" in normalized and years < 1:
            return label
        plus_match = re.search(r"(?P<low>\d+)\s*\+\s*years?", normalized)
        if plus_match and years >= int(plus_match.group("low")):
            return label
        range_match = re.search(r"(?P<low>\d+)\s*(?:-|–|to|\s+)\s*(?P<high>\d+)\s*years?", normalized)
        if range_match:
            low = int(range_match.group("low"))
            high = int(range_match.group("high"))
            if low <= years < high:
                return label
    raise ValueError(f"Could not match {years:.2f} years to an experience option for field {field.get('name')}")


def _question_starts_with_interrogative(question: dict) -> bool:
    normalized_label = _normalize_free_text(_question_label(question))
    return any(normalized_label.startswith(prefix) for prefix in ("what ", "which ", "where "))


def _question_targets_work_location(question: dict) -> bool:
    if _question_matches(
        question,
        "sponsorship",
        "sponsor",
        "visa",
        "work authorization",
        "authorized to work",
        "authorised to work",
        "legally authorized",
        "legally authorised",
        "right to work",
    ):
        return False
    return _question_matches(
        question,
        "candidate location",
        "location (city)",
        "location are you closest to",
        "office are you closest to",
        "office location",
        "preferred office",
        "office do you prefer",
        "location do you prefer",
        "where do you intend to work",
        "intend to work",
        "current location",
        "what cities are you available",
        "in what cities",
        "available to work",
        "open to work",
        "work out of",
    )


def _question_targets_current_residence_state(question: dict) -> bool:
    return _question_matches(
        question,
        "state of residence",
        "province of residence",
        "current state of residence",
        "current province of residence",
        "state or province",
        "which state",
        "which province",
        "what state",
        "what province",
    )


def _question_targets_current_residence(question: dict, *, category: str | None = None) -> bool:
    if category == "location_residency":
        return False
    if _question_targets_work_location(question):
        return False
    if _question_targets_explicit_state_list(question):
        return False
    if _question_matches(question, "location specified for this role", "location for this role", "job location"):
        return False
    if _question_targets_current_residence_state(question):
        return True
    if _question_matches(question, "city of residence", "residence city"):
        return True
    if _question_matches(
        question, "where are you currently based", "where are you based", "where do you currently live"
    ):
        return True
    if not _question_starts_with_interrogative(question):
        return False
    return _question_matches(
        question,
        "currently live in",
        "currently reside in",
        "currently based",
        "live in",
        "reside in",
        "based in",
    )


def _question_targets_post_citizenship_permanent_residency(question: dict) -> bool:
    return _question_matches(
        question,
        "most recent citizenship",
        "afterwards become a permanent resident",
        "permanent resident in any other country",
        "permanent resident in any other country/region",
    )


def _planned_unconfirmed_question_step(
    *,
    field: dict,
    selector: str,
    label: str,
    optional: bool,
    source: str,
    report_value: str,
) -> dict[str, object]:
    field_type = str(field.get("type") or "")
    kind = (
        "combobox" if field_type == "multi_value_single_select" else "textarea" if field_type == "textarea" else "text"
    )
    return {
        "kind": kind,
        "field_name": str(field.get("name") or ""),
        "selector": selector,
        "label": label,
        "value": "[manual review required]",
        "report_value": report_value,
        "optional": optional,
        "source": source,
        "status": "planned",
        "skip_runtime_fill": True,
        "note": report_value,
    }


def _yes_no_option_label(field: dict, value: bool) -> str:
    target = "yes" if value else "no"
    for option in field.get("values", []):
        label = str(option["label"]).strip()
        normalized = _normalize_free_text(label)
        if normalized == target or normalized.startswith(f"{target} "):
            return label
    raise ValueError(f"Could not find a {target!r} option for field {field.get('name')}")


def _field_has_yes_no_options(field: dict) -> bool:
    labels = [_normalize_free_text(str(option.get("label") or "").strip()) for option in field.get("values", [])]
    labels = [label for label in labels if label]
    if not labels:
        return False
    yes_options = [label for label in labels if label == "yes" or label.startswith("yes ")]
    no_options = [label for label in labels if label == "no" or label.startswith("no ")]
    return len(yes_options) == 1 and len(no_options) == 1 and len(yes_options) + len(no_options) == len(labels)


def _first_prefixed_option_label(field: dict, prefix: str) -> str | None:
    normalized_prefix = _normalize_free_text(prefix)
    for option in field.get("values", []):
        label = str(option["label"]).strip()
        normalized = _normalize_free_text(label)
        if normalized == normalized_prefix or normalized.startswith(f"{normalized_prefix} "):
            return label
    return None


def _location_aware_option_label(field: dict, application_profile: ApplicationProfile) -> str:
    matched = select_location_positive_fit_option(
        [str(option.get("label") or "").strip() for option in field.get("values", [])],
        application_profile=application_profile,
    )

    candidate_markers = [
        _normalize_free_text(candidate)
        for candidate in _location_match_candidates(application_profile.location or "", application_profile.country)
    ]
    candidate_markers = [candidate for candidate in candidate_markers if candidate]
    values = list(field.get("values", []))

    for option in values:
        label = str(option["label"]).strip()
        normalized = _normalize_free_text(label)
        if any(marker in normalized for marker in candidate_markers) and (
            normalized == "yes" or normalized.startswith("yes ")
        ):
            return label
    if matched is not None:
        return matched
    try:
        return _yes_no_option_label(field, True)
    except ValueError:
        prefixed_yes = _first_prefixed_option_label(field, "yes")
        if prefixed_yes:
            return prefixed_yes

    for option in values:
        label = str(option["label"]).strip()
        normalized = _normalize_free_text(label)
        if "relocat" in normalized:
            return label

    for option in values:
        label = str(option["label"]).strip()
        normalized = _normalize_free_text(label)
        if any(marker in normalized for marker in candidate_markers):
            return label

    if values:
        return str(values[0]["label"]).strip()
    raise ValueError(f"Could not find a location-aware option for field {field.get('name')}")


def _work_authorization_option_label(field: dict, application_profile: ApplicationProfile) -> str:
    labels = [str(option["label"]).strip() for option in field.get("values", [])]
    normalized_labels = [(label, _normalize_free_text(label)) for label in labels]

    if not application_profile.authorized_to_work_unconditionally:
        for label, normalized in normalized_labels:
            if "not authorized" in normalized or "need visa support" in normalized:
                return label

    requires_sponsorship = application_profile.require_sponsorship_now or application_profile.require_sponsorship_future
    if requires_sponsorship:
        for label, normalized in normalized_labels:
            if ("need" in normalized and "sponsor" in normalized) or "sponsor my visa" in normalized:
                return label
            if "sponsored by the company" in normalized:
                return label
        for label, normalized in normalized_labels:
            if "not authorized" in normalized or "need visa support" in normalized:
                return label
    else:
        for label, normalized in normalized_labels:
            if "do not need" in normalized and "sponsor" in normalized:
                return label
            if "authorized to work" in normalized and "do not need" in normalized:
                return label
        for label, normalized in normalized_labels:
            if "authorized to work" in normalized and "need" not in normalized:
                return label

    if labels:
        return labels[0]
    raise ValueError(f"Could not find a work-authorization option for field {field.get('name')}")


def _should_refill_visible_deterministic_step(step: dict[str, object]) -> bool:
    if step.get("kind") not in {"text", "textarea", "combobox"}:
        return False
    if str(step.get("source") or "").strip() not in {"application_profile.md", "master_resume.md"}:
        return False
    return not bool(step.get("skip_runtime_fill"))


def _work_authorization_followup_option_label(field: dict, application_profile: ApplicationProfile) -> str:
    requires_sponsorship = application_profile.require_sponsorship_now or application_profile.require_sponsorship_future

    if not requires_sponsorship:
        for desired in ("Not Applicable", "N/A", "NA", "None of the above"):
            try:
                return _match_option_label(field, desired)
            except ValueError:
                continue

    answer_text = " ".join(
        part
        for part in (
            getattr(application_profile, "work_authorization_statement", ""),
            getattr(application_profile, "sponsorship_answer", ""),
        )
        if part
    )
    normalized_answer = _normalize_free_text(answer_text)
    visa_keywords = (
        ("h 1b", "H-1B"),
        ("h1b", "H-1B"),
        ("e 3", "E-3/H-1B1"),
        ("h 1b1", "E-3/H-1B1"),
        ("tn", "TN"),
        ("j 1", "J-1"),
        ("opt", "OPT"),
        ("cpt", "CPT"),
    )
    for needle, desired in visa_keywords:
        if needle in normalized_answer:
            try:
                return _match_option_label(field, desired)
            except ValueError:
                continue

    if requires_sponsorship:
        for desired in ("Other", "I don't know"):
            try:
                return _match_option_label(field, desired)
            except ValueError:
                continue

    return _work_authorization_option_label(field, application_profile)


def _resolve_discovered_demographic_option_text(
    *,
    field_name: str,
    desired: str,
    option_texts: list[str],
    application_profile: ApplicationProfile,
    company_name: str | None = None,
    job_url: str | None = None,
    source_url: str | None = None,
    source_hint: str | None = None,
) -> str:
    labels = [str(text or "").strip() for text in option_texts if str(text or "").strip()]
    if not labels:
        return desired
    if field_name == "how_did_you_hear":
        desired_values, _ = resolve_how_did_you_hear_option_candidates(
            application_profile,
            labels,
            company_name=company_name,
            job_url=job_url,
            source_url=source_url,
            source_hint=source_hint,
            prefer_metadata_job_board_matches=True,
        )
        pseudo_field = {"name": field_name, "values": [{"label": label} for label in labels]}
        for desired_value in desired_values:
            try:
                return _match_option_label(pseudo_field, desired_value)
            except ValueError:
                continue
        return desired

    pseudo_field = {"name": field_name, "values": [{"label": label} for label in labels]}
    try:
        return _match_option_label(pseudo_field, desired)
    except ValueError:
        pass

    if field_name == "work_authorization":
        try:
            return _work_authorization_option_label(pseudo_field, application_profile)
        except ValueError:
            return desired

    return desired


def _greenhouse_expected_step_value(step: dict[str, object]) -> str:
    return str(step.get("option") or step.get("search") or step.get("value") or "").strip()


_GREENHOUSE_LOCATION_TEXT_FIELD_NAMES = frozenset(
    {"candidate_location", "candidate-location", "job_application[location]"}
)


def _greenhouse_text_value_matches_expected(field_name: str, desired: str, observed: str) -> bool:
    normalized_field_name = str(field_name or "").strip()
    if normalized_field_name in _GREENHOUSE_LOCATION_TEXT_FIELD_NAMES:
        return _greenhouse_option_text_matches(normalized_field_name, desired, observed)
    return _normalize_free_text(desired) == _normalize_free_text(observed)


def _greenhouse_live_value_matches_step(step: dict[str, object], live_value: str) -> bool:
    expected_value = _greenhouse_expected_step_value(step)
    observed_value = str(live_value or "").strip()
    if not expected_value or not observed_value:
        return False
    if str(step.get("kind") or "").strip() == "combobox":
        return _greenhouse_combobox_value_matches_expected(
            str(step.get("field_name") or "").strip(),
            expected_value,
            observed_value,
        )
    return _greenhouse_text_value_matches_expected(
        str(step.get("field_name") or "").strip(),
        expected_value,
        observed_value,
    )


def _all_questions(job_post: dict) -> list[dict]:
    questions = list(job_post.get("questions", []))
    for section in job_post.get("eeoc_sections", []):
        questions.extend(section.get("questions", []))
    return questions


def _question_is_cover_letter_slot(question: dict) -> bool:
    return _question_matches(question, "cover letter", "anything else you'd like to share")


def _question_is_current_company(question: dict) -> bool:
    return question_is_current_company_field(
        field_name=_question_field_name(question),
        label=_question_label(question),
    )


_ACKNOWLEDGMENT_OPTION_LABELS = frozenset(
    {
        "i acknowledge",
        "i agree",
        "i accept",
        "i confirm",
        "i consent",
        "acknowledge",
        "confirm",
        "agree",
        "accept",
        "consent",
    }
)
_PREFERENCE_RESEARCH_LIMIT_RE = re.compile(
    r"(?:select(?:\s+your)?\s+top|select\s+up\s+to|choose(?:\s+your)?\s+top|top)\s+(\d+)",
    re.I,
)
_PREFERENCE_RESEARCH_EXCLUSION_FRAGMENTS = (
    "location",
    "office",
    "hybrid",
    "remote",
    "onsite",
    "on site",
    "commute",
    "relocat",
    "visa",
    "sponsor",
    "work authorization",
    "authorised to work",
    "authorized to work",
    "pronouns",
    "gender",
    "race",
    "ethnicity",
    "veteran",
    "disability",
    "sexual orientation",
    "salary",
    "compensation",
    "how did you hear",
    "country",
    "state of residence",
    "what state",
    "what country",
)
_PREFERENCE_RESEARCH_OPTION_EXCLUSION_FRAGMENTS = (
    "remote",
    "hybrid",
    "onsite",
    "on site",
    "in office",
    "san francisco",
    "new york",
)


def _is_acknowledgment_field(field: dict) -> bool:
    """Return True when a field's only options are acknowledgment-type labels.

    This prevents false-positive matches where a question label incidentally
    contains phrases like "current employer" (e.g. background-check
    acknowledgments) but the field itself is only asking for confirmation,
    not for a company name.
    """
    values = field.get("values") or []
    if not values:
        return False
    labels = [_normalize_free_text(str(v.get("label", ""))) for v in values]
    return all(label in _ACKNOWLEDGMENT_OPTION_LABELS for label in labels if label)


def _question_is_acknowledgment_confirmation(question: dict) -> bool:
    field = (question.get("fields") or [{}])[0]
    if _is_acknowledgment_field(field):
        return True
    if not (_field_has_yes_no_options(field) or len(field.get("values", []) or []) == 1):
        return False
    return _question_matches(
        question,
        "privacy acknowledgement",
        "privacy acknowledgment",
        "candidate privacy",
        "privacy notice",
        "privacy policy",
        "reviewed and confirmed",
        "accurate and complete",
        "double-check all the information",
    )


def _question_is_deterministic(question: dict) -> bool:
    text = _question_text(question)
    if _question_matches(question, "pronouns"):
        return True
    category = classify_question(text, field_type=_question_type(question))
    if question_prefers_generated_free_text_answer(text, field_type=_question_type(question), category=category):
        return False
    if category is not None and category != "preference_ranking":
        return True
    if _question_requests_product_management_experience_range(question):
        return True
    # Field-name-based current_company detection not covered by label-only classifier
    if _question_is_current_company(question):
        return True
    return _question_matches(question, *DETERMINISTIC_QUESTION_LABEL_FRAGMENTS)


def _is_classic_demographic_field_name(field_name: str) -> bool:
    return field_name.startswith("job_application[demographic_answers]")


def _question_requires_generated_answer(question: dict) -> bool:
    field_name = _question_field_name(question)
    if field_name == "security_code":
        return False
    if _is_classic_demographic_field_name(field_name):
        return False
    if _question_is_acknowledgment_confirmation(question):
        return False
    field_type = _question_type(question)
    if field_type not in {"textarea", "input_text", "multi_value_single_select", "multi_value_multi_select"}:
        return False
    if _question_is_cover_letter_slot(question):
        return False
    return not _question_is_deterministic(question)


def _question_is_voluntary_demographic_decline_opt_out(question: dict) -> bool:
    fields = list(question.get("fields") or [])
    if len(fields) != 1:
        return False
    field = fields[0]
    if field.get("type") != "multi_value_multi_select" or question.get("required"):
        return False
    values = list(field.get("values") or [])
    if len(values) != 1:
        return False
    option_label = _normalize_free_text(str(values[0].get("label", "")))
    if not option_label:
        return False
    if not any(
        fragment in option_label
        for fragment in (
            "decline to answer",
            "do not wish to answer",
            "prefer not to answer",
            "choose not to answer",
        )
    ):
        return False
    prompt_text = _normalize_free_text(
        " ".join(
            part
            for part in (
                _question_label(question),
                str(question.get("description", "") or ""),
            )
            if str(part or "").strip()
        )
    )
    return any(
        fragment in prompt_text
        for fragment in (
            "equal opportunity employment",
            "self identification",
            "self identify",
            "completion is voluntary",
            "voluntary self identification",
            "demographic information",
        )
    )


def _selector_for_field(field_name: str) -> str:
    if field_name in {"candidate_location", "candidate-location"}:
        return "#candidate-location"
    if "[" in field_name or "]" in field_name:
        return f'[id="{field_name}"], [name="{field_name}"]'
    return f"#{field_name}"


def _load_optional_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return None


def _preference_research_selection_limit(question: dict) -> int | None:
    if _question_type(question) == "multi_value_single_select":
        return 1
    match = _PREFERENCE_RESEARCH_LIMIT_RE.search(_question_text(question))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _question_is_preference_research_eligible(question: dict, *, category: str | None = None) -> bool:
    field = (question.get("fields") or [None])[0]
    if not isinstance(field, dict):
        return False
    if field.get("type") not in {"multi_value_single_select", "multi_value_multi_select"}:
        return False
    if category != "preference_ranking":
        return False
    normalized_text = _normalize_free_text(_question_text(question))
    if any(fragment in normalized_text for fragment in _PREFERENCE_RESEARCH_EXCLUSION_FRAGMENTS):
        return False
    option_labels = [str(value.get("label", "")).strip() for value in field.get("values", [])]
    if len(option_labels) < 2:
        return False
    normalized_options = [_normalize_free_text(label) for label in option_labels if label]
    if normalized_options and all(
        option in {"yes", "no", "na", "n a", "not applicable"} for option in normalized_options
    ):
        return False
    return not (
        normalized_options
        and any(
            fragment in option
            for option in normalized_options
            for fragment in _PREFERENCE_RESEARCH_OPTION_EXCLUSION_FRAGMENTS
        )
    )


def _preference_research_answers(payload: dict) -> dict[str, str | list[str]]:
    answers = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(answers, dict):
        return {}
    return {
        str(field_name).strip(): value
        for field_name, value in answers.items()
        if str(field_name).strip() and isinstance(value, (str, list))
    }


def _preference_research_answer_payload(payload: dict) -> dict | None:
    if not isinstance(payload, dict):
        return None
    questions = payload.get("questions")
    failures = payload.get("failures")
    if not questions and not failures:
        return None
    return {
        "cache_key": payload.get("cache_key"),
        "provider": payload.get("provider"),
        "artifacts": payload.get("artifacts") or {},
        "questions": [dict(question) for question in (questions or []) if isinstance(question, dict)],
        "failures": [dict(failure) for failure in (failures or []) if isinstance(failure, dict)],
    }


def _preference_research_failure_blockers(payload: dict) -> list[dict]:
    blockers: list[dict] = []
    failures = payload.get("failures") if isinstance(payload, dict) else None
    if not isinstance(failures, list):
        return blockers
    for failure in failures:
        if not isinstance(failure, dict) or not failure.get("required"):
            continue
        spec = {
            "field_name": failure.get("field_name"),
            "label": failure.get("label"),
            "required": True,
            "type": failure.get("type"),
        }
        reason = str(failure.get("failure_reason") or "Preference research could not resolve this field.").strip()
        step = _generated_answer_blocker_step(spec, raw_value=failure.get("raw_value"), reason=reason)
        step["source"] = "preference_research"
        step["artifact_key"] = "preference_research_failures_json"
        step["note"] = reason
        blockers.append(step)
    return blockers


def _preference_research_drift_blockers(job_post: dict, *, out_dir: Path) -> list[dict]:
    answers_payload = _load_optional_json(role_submit_path(out_dir, APPLICATION_ANSWER_CACHE)) or {}
    preference_payload = answers_payload.get("preference_research") if isinstance(answers_payload, dict) else None
    if not isinstance(preference_payload, dict):
        return []
    researched_questions = preference_payload.get("questions")
    if not isinstance(researched_questions, list):
        return []

    failures: list[dict] = []
    blockers: list[dict] = []
    live_fields = {
        str((question.get("fields") or [{}])[0].get("name") or "").strip(): (question.get("fields") or [{}])[0]
        for question in _all_questions(job_post)
        if question.get("fields")
    }

    for researched in researched_questions:
        if not isinstance(researched, dict):
            continue
        field_name = str(researched.get("field_name") or "").strip()
        selected_options = [
            str(option).strip() for option in (researched.get("selected_options") or []) if str(option).strip()
        ]
        if not field_name or not selected_options:
            continue
        live_field = live_fields.get(field_name)
        if not isinstance(live_field, dict):
            reason = "Preference research selected a field that is no longer present on the current live form."
            failure = {
                "field_name": field_name,
                "label": researched.get("label"),
                "required": bool(researched.get("required")),
                "type": researched.get("type"),
                "selected_options": selected_options,
                "failure_reason": reason,
            }
            failures.append(failure)
        else:
            live_options = [str(option.get("label", "")).strip() for option in live_field.get("values", [])]
            missing = [option for option in selected_options if option not in live_options]
            if not missing:
                continue
            reason = (
                "Preference research selected option labels that are no longer present on the current live form: "
                + ", ".join(missing)
            )
            failure = {
                "field_name": field_name,
                "label": researched.get("label"),
                "required": bool(researched.get("required")),
                "type": researched.get("type"),
                "selected_options": selected_options,
                "failure_reason": reason,
            }
            failures.append(failure)

    if not failures:
        return []

    role_submit_path(out_dir, PREFERENCE_RESEARCH_FAILURES_JSON).write_text(
        _json_dumps_pretty(failures) + "\n",
        encoding="utf-8",
    )
    for failure in failures:
        if not failure.get("required"):
            continue
        spec = {
            "field_name": failure.get("field_name"),
            "label": failure.get("label"),
            "required": True,
            "type": failure.get("type"),
        }
        reason = str(failure.get("failure_reason") or "").strip()
        step = _generated_answer_blocker_step(spec, raw_value=failure.get("selected_options"), reason=reason)
        step["source"] = "preference_research"
        step["artifact_key"] = "preference_research_failures_json"
        step["note"] = reason
        blockers.append(step)
    return blockers


def _generated_answer_blocker_artifacts(out_dir: Path, blockers: list[dict]) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    for blocker in blockers:
        artifact_key = str(blocker.get("artifact_key") or "").strip()
        if artifact_key == "linked_resource_failures_json":
            candidate = role_submit_path(out_dir, "linked_resource_failures.json")
        elif artifact_key == "preference_research_failures_json":
            candidate = role_submit_path(out_dir, PREFERENCE_RESEARCH_FAILURES_JSON)
        else:
            continue
        if candidate.exists():
            artifacts[artifact_key] = str(candidate)
    return artifacts


def _application_question_specs(job_post: dict) -> list[dict]:
    specs: list[dict] = []
    for question in _all_questions(job_post):
        if not _question_requires_generated_answer(question):
            continue
        field = question["fields"][0]
        spec: dict = {
            "field_name": field["name"],
            "label": _question_label(question),
            "description": _question_description(question),
            "required": question["required"],
            "type": field["type"],
        }
        category = classify_question(_question_text(question), field_type=field["type"])
        if category == "preference_ranking" and _question_is_preference_research_eligible(question, category=category):
            spec["research_mode"] = "preference_ranking"
            selection_limit = _preference_research_selection_limit(question)
            if selection_limit is not None:
                spec["selection_limit"] = selection_limit
        if field["type"] in ("multi_value_single_select", "multi_value_multi_select"):
            spec["options"] = [str(v["label"]).strip() for v in field.get("values", [])]
        specs.append(spec)
    return specs


def _pending_user_input_questions(job_post: dict, application_profile: ApplicationProfile) -> list[dict]:
    pending: list[dict] = []
    for question in _all_questions(job_post):
        reason = pending_user_input_reason_for_spec(question, application_profile)
        if not reason:
            continue
        pending.append(
            {
                "field_name": _question_field_name(question),
                "label": _question_label(question),
                "description": _question_description(question),
                "type": _question_type(question),
                "required": bool(question.get("required")),
                "reason": reason,
            }
        )
    return pending


def _json_dumps_pretty(data: object) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _write_or_clear_json_artifact(path: Path, payload: dict | None) -> None:
    if payload is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.write_text(_json_dumps_pretty(payload) + "\n", encoding="utf-8")


def _page_screenshot_path(out_dir: Path, page_index: int) -> Path:
    return role_submit_path(out_dir, AUTOFILL_PAGE_SCREENSHOTS_DIR) / f"page_{page_index:02d}.png"


def capture_stitched_screenshot(page, path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Try to screenshot just the application form container to avoid
    # capturing the duplicate JD content that Greenhouse pages render.
    for selector in (
        "#application",
        "#application-form",
        "#application_form",
        "main",
    ):
        try:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                locator.screenshot(path=str(output_path), type="png")
                return
        except Exception:
            continue

    # Fallback: hide fixed/sticky elements, then full_page screenshot.
    page.evaluate("""() => {
        const state = { hidden: [] };
        for (const el of document.querySelectorAll('*')) {
            const style = window.getComputedStyle(el);
            if ((style.position === 'fixed' || style.position === 'sticky') && el.offsetHeight < window.innerHeight * 0.5) {
                state.hidden.push({ el, prev: el.style.display });
                el.style.display = 'none';
            }
        }
        window.__screenshotHiddenState = state;
    }""")
    try:
        page.screenshot(path=str(output_path), type="png", full_page=True)
    finally:
        page.evaluate("""() => {
            const state = window.__screenshotHiddenState;
            if (!state) return;
            for (const item of state.hidden) {
                item.el.style.display = item.prev;
            }
            delete window.__screenshotHiddenState;
        }""")


def _capture_review_checkpoint_artifacts(page, artifacts: dict[str, str]) -> None:
    pre_submit_screenshot = artifacts.get("pre_submit_screenshot")
    if pre_submit_screenshot:
        pre_submit_path = Path(pre_submit_screenshot)
        pre_submit_path.parent.mkdir(parents=True, exist_ok=True)
        capture_stitched_screenshot(page, str(pre_submit_path))

    review_screenshot = artifacts.get("review_screenshot")
    if review_screenshot:
        capture_stitched_screenshot(page, str(review_screenshot))


def _redact_security_code(code: str) -> str:
    return f"[redacted {len(code)}-character code]"


def _decode_gmail_body(data: str | None) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, OSError):
        return ""


def _iter_gmail_parts(payload: dict | None) -> list[dict]:
    if not payload:
        return []
    parts = [payload]
    for part in payload.get("parts", []) or []:
        parts.extend(_iter_gmail_parts(part))
    return parts


def _gmail_header_value(message: dict, header_name: str) -> str:
    payload = message.get("payload") or {}
    headers = payload.get("headers", []) or []
    target = header_name.casefold()
    for header in headers:
        if str(header.get("name", "")).casefold() == target:
            return str(header.get("value", "")).strip()
    return ""


def _coerce_utc_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _gmail_message_received_at_utc(message: dict) -> datetime | None:
    raw_internal_date = str(message.get("internalDate") or "").strip()
    if raw_internal_date.isdigit():
        try:
            return datetime.fromtimestamp(int(raw_internal_date) / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError):
            pass

    header_date = _gmail_header_value(message, "Date")
    if not header_date:
        return None
    try:
        parsed = parsedate_to_datetime(header_date)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _gmail_after_query_term(min_received_at_utc: datetime | str | None) -> str | None:
    min_received_at = _coerce_utc_datetime(min_received_at_utc)
    if min_received_at is None:
        return None
    return f"after:{min_received_at.strftime('%Y/%m/%d')}"


def _extract_security_code_from_text(text: str) -> str | None:
    if not text:
        return None

    for match in SECURITY_CODE_RE.finditer(text):
        return match.group(1)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        lowered = line.casefold()
        if (
            "verification code" in lowered
            or "security code" in lowered
            or "copy and paste this code" in lowered
            or "confirm you're a human" in lowered
            or "confirm you are not a robot" in lowered
        ):
            window = " ".join(lines[index : index + 3])
            for token_match in GENERIC_CODE_TOKEN_RE.finditer(window):
                token = token_match.group(1)
                if any(char.isalpha() for char in token) and any(char.isdigit() for char in token):
                    return token
    return None


def _extract_security_code_from_gmail_message(message: dict) -> str | None:
    candidates = [
        _gmail_header_value(message, "Subject"),
        str(message.get("snippet", "")).strip(),
    ]
    payload = message.get("payload") or {}
    for part in _iter_gmail_parts(payload):
        body = part.get("body") or {}
        decoded = _decode_gmail_body(body.get("data"))
        if decoded:
            candidates.append(decoded)

    for candidate in candidates:
        code = _extract_security_code_from_text(candidate)
        if code:
            return code
    return None


def _run_gws_json(args: list[str]) -> dict:
    completed = subprocess.run(
        ["gws", *args],
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(
            "googleworkspace/cli failed while fetching Gmail verification messages. "
            "Make sure `gws auth login` is complete and the Gmail API is reachable. "
            f"Command: {' '.join(['gws', *args])}. Details: {detail}"
        )

    output = (completed.stdout or "").strip()
    if not output:
        raise RuntimeError(f"googleworkspace/cli returned empty output for {' '.join(['gws', *args])}")

    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"googleworkspace/cli returned non-JSON output for {' '.join(['gws', *args])}: {output[:400]}"
        ) from exc


def _fetch_security_code_from_gmail(
    email: str | None,
    *,
    min_received_at_utc: datetime | str | None = None,
    wait_seconds: int = DEFAULT_SECURITY_CODE_WAIT_SECONDS,
) -> str:
    del email  # The authenticated Gmail account is the source of truth for `gws`.
    min_received_at = _coerce_utc_datetime(min_received_at_utc)
    after_term = _gmail_after_query_term(min_received_at)
    query_terms = [
        after_term or "newer_than:1d",
        '"security code"',
        '"copy and paste this code"',
    ]
    deadline = time.monotonic() + max(wait_seconds, 0)
    last_error = "Could not find a verification/security code in the latest Gmail messages."

    while True:
        list_response = _run_gws_json(
            [
                "gmail",
                "users",
                "messages",
                "list",
                "--params",
                json.dumps(
                    {
                        "userId": "me",
                        "maxResults": 20,
                        "q": " ".join(term for term in query_terms if term),
                    }
                ),
            ]
        )
        message_ids = [message.get("id") for message in list_response.get("messages", []) if message.get("id")]
        if not message_ids:
            last_error = "No recent Gmail messages were returned while searching for a verification code."
        else:
            for message_id in message_ids:
                message = _run_gws_json(
                    [
                        "gmail",
                        "users",
                        "messages",
                        "get",
                        "--params",
                        json.dumps({"userId": "me", "id": message_id, "format": "full"}),
                    ]
                )
                received_at = _gmail_message_received_at_utc(message)
                if min_received_at and received_at and received_at < min_received_at:
                    continue
                if min_received_at and received_at is None:
                    continue
                code = _extract_security_code_from_gmail_message(message)
                if code:
                    return code
            threshold = min_received_at.isoformat() if min_received_at else None
            if threshold:
                last_error = (
                    f"Could not find a verification/security code in Gmail that was received after {threshold}."
                )
            else:
                last_error = "Could not find a verification/security code in the latest Gmail messages."

        if time.monotonic() >= deadline:
            break
        time.sleep(2)

    raise RuntimeError(last_error)


def _build_application_answers_prompt(
    *,
    provider: str,
    meta: dict,
    question_specs: list[dict],
    jd_parsed: dict,
    resume_content: dict | None,
    research_cache: dict | None,
    cover_letter_text: str,
    master_resume_text: str,
    work_stories_text: str,
    candidate_context_text: str,
    application_profile_text: str,
    linked_resource_context: str | None = None,
) -> str:
    del provider  # Included for future provider-specific prompt tuning.
    return build_shared_application_answers_prompt(
        meta=meta,
        question_specs=question_specs,
        jd_parsed=jd_parsed,
        resume_content=resume_content,
        research_cache=research_cache,
        cover_letter_text=cover_letter_text,
        master_resume_text=master_resume_text,
        work_stories_text=work_stories_text,
        candidate_context_text=candidate_context_text,
        application_profile_text=application_profile_text,
        linked_resource_context=linked_resource_context,
    )


def _default_answer_provider() -> str:
    from application_submit_common import default_answer_provider

    return default_answer_provider()


_CONDITIONAL_FOLLOWUP_RE = re.compile(
    r"^if\s+(yes|you answered|other|so|applicable|selected|true)"
    r"|^if\s+\S+.*(?:required|needed|applicable|necessary).*(?:please|provide|confirm|specify)"
    r"|mentioned above|the above question|the previous question"
    r"|if\s+yes.*(?:provide|detail|explain|describe)"
    r"|if you answered.*(?:yes|other)"
    r"|please\s+(?:confirm|specify|explain|describe).*(?:above|mentioned)",
    re.I,
)
_RECENT_GRAD_GPA_FOLLOWUP_RE = re.compile(
    r"^if\s+you(?:'re| are)\s+less than\s+\d+\s+years?\s+out of school,\s*"
    r"(?:what\s+is|please\s+(?:provide|share|enter|list)|(?:provide|share|enter|list))\s+your\s+"
    r"(?:undergraduate\s+)?(?:gpa|grade point average)\b",
    re.I,
)


def _is_conditional_followup(spec: dict) -> bool:
    """Return True if the question looks like a conditional follow-up (e.g. 'If yes...')."""
    label = (spec.get("label") or "").strip()
    return bool(_CONDITIONAL_FOLLOWUP_RE.search(label) or _RECENT_GRAD_GPA_FOLLOWUP_RE.search(label))


def _validate_generated_answers(
    question_specs: list[dict],
    answers: dict,
    *,
    application_profile: ApplicationProfile | None = None,
) -> dict[str, str | list[str]]:
    validated, blockers = validate_generated_answers_with_blockers(
        question_specs,
        normalize_multi_select_generated_answers(question_specs, answers),
        application_profile=application_profile,
    )
    if blockers:
        first_blocker = blockers[0]
        raise ValueError(
            f"Generated answer regression for {first_blocker.get('field_name')}: {first_blocker.get('reason')}"
        )
    return validated


def _generate_application_answers(
    *,
    out_dir: Path,
    meta: dict,
    job_post: dict,
    provider: str,
) -> dict[str, str | list[str]]:
    question_specs = _application_question_specs(job_post)
    migrate_role_output_layout(out_dir)
    if not question_specs:
        clear_answer_generation_artifacts(out_dir)
        clear_preference_research_artifacts(out_dir)
        return {}
    application_profile_text = APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
    application_profile = _parse_application_profile(application_profile_text)
    master_resume_text = MASTER_RESUME_PATH.read_text(encoding="utf-8")
    work_stories_text = WORK_STORIES_PATH.read_text(encoding="utf-8")
    candidate_context_text = CANDIDATE_CONTEXT_PATH.read_text(encoding="utf-8")
    jd_parsed = (
        _load_optional_json(role_content_path(out_dir, "jd_parsed.json"))
        or _load_optional_json(out_dir / "jd_parsed.json")
        or {}
    )
    resume_content = _load_optional_json(role_content_path(out_dir, "resume_content.json")) or _load_optional_json(
        out_dir / "resume_content.json"
    )
    research_cache = _load_optional_json(PROJECT_ROOT / "output" / meta["company"] / "research_cache.json") or {}
    _role_research = _load_optional_json(role_content_path(out_dir, "role_research_cache.json")) or _load_optional_json(
        out_dir / "role_research_cache.json"
    )
    if _role_research and "role_context" in _role_research:
        research_cache = {**research_cache, "role_context": _role_research["role_context"]}
    cover_letter_text = _find_cover_letter_text(out_dir)
    raw_output_path = role_submit_path(out_dir, APPLICATION_ANSWER_RAW)
    answers_path = role_submit_path(out_dir, APPLICATION_ANSWER_CACHE)
    raw_output_path.parent.mkdir(parents=True, exist_ok=True)
    refresh_request_id = current_answer_refresh_request_id(out_dir)
    force_fresh_generation = refresh_request_id is not None
    if force_fresh_generation:
        clear_answer_generation_artifacts(out_dir)
        clear_preference_research_artifacts(out_dir)
    preference_research_payload = prepare_preference_research_context(
        out_dir,
        meta=meta,
        question_specs=question_specs,
        provider=provider,
        jd_parsed=jd_parsed,
        resume_content=resume_content,
        research_cache=research_cache,
        cover_letter_text=cover_letter_text,
        master_resume_text=master_resume_text,
        work_stories_text=work_stories_text,
        candidate_context_text=candidate_context_text,
        application_profile_text=application_profile_text,
        force_refresh=force_fresh_generation,
        command_builder=provider_command_for_mode,
        timeout_seconds=provider_timeout_seconds(),
    )
    preference_research_cache_key = str(preference_research_payload.get("cache_key") or "").strip() or None
    preference_research_deterministic = _preference_research_answers(preference_research_payload)
    preference_research_blockers = _preference_research_failure_blockers(preference_research_payload)
    if preference_research_blockers:
        raise GeneratedAnswerBlockersError(
            preference_research_blockers,
            valid_answers=preference_research_deterministic,
        )
    linked_resource_payload = prepare_linked_resource_context(
        out_dir,
        question_specs,
        force_refresh=force_fresh_generation,
    )
    linked_resource_cache_key = str(linked_resource_payload.get("cache_key") or "").strip() or None
    linked_resource_deterministic = _linked_resource_deterministic_answers(linked_resource_payload)
    linked_resource_blockers = _linked_resource_failure_blockers(linked_resource_payload)
    if linked_resource_blockers:
        _write_pending_linked_resource_blockers(
            out_dir,
            meta=meta,
            blockers=linked_resource_blockers,
            payload=linked_resource_payload,
        )
        raise GeneratedAnswerBlockersError(linked_resource_blockers, valid_answers={})
    classified_shared_answers = _classified_shared_answers(
        question_specs,
        application_profile,
        master_resume_text=master_resume_text,
        work_stories_text=work_stories_text,
    )
    preference_research_fields = {
        str(spec.get("field_name") or "").strip()
        for spec in question_specs
        if str(spec.get("research_mode") or "").strip() == "preference_ranking"
    }
    optional_skipped_field_names = {
        str(spec.get("field_name") or "").strip()
        for spec in question_specs
        if should_skip_optional_generated_answer(spec)
    }
    provider_question_specs = [
        spec
        for spec in question_specs
        if str(spec.get("field_name") or "").strip() not in linked_resource_deterministic
        and str(spec.get("field_name") or "").strip() not in classified_shared_answers
        and str(spec.get("field_name") or "").strip() not in preference_research_fields
        and str(spec.get("field_name") or "").strip() not in optional_skipped_field_names
    ]

    def _write_answers_payload(
        *,
        answers: dict[str, str | list[str]],
        provider_name: str,
        existing_payload: dict | None = None,
    ) -> None:
        answers_payload = dict(existing_payload or {})
        answers_payload["generated_at_utc"] = datetime.now(UTC).replace(microsecond=0).isoformat()
        answers_payload["provider"] = provider_name
        answers_payload["refresh_request_id"] = refresh_request_id
        answers_payload["questions"] = question_specs
        answers_payload["answers"] = answers
        linked_resources = _linked_resource_answer_payload(linked_resource_payload)
        if linked_resources is not None:
            answers_payload["linked_resources"] = linked_resources
        else:
            answers_payload.pop("linked_resources", None)
        preference_research = _preference_research_answer_payload(preference_research_payload)
        if preference_research is not None:
            answers_payload["preference_research"] = preference_research
        else:
            answers_payload.pop("preference_research", None)
        answers_path.write_text(_json_dumps_pretty(answers_payload) + "\n", encoding="utf-8")

    def _merge_deterministic_answers(raw_answers: dict[str, str | list[str]] | None) -> dict[str, str | list[str]]:
        merged_answers = {
            **(raw_answers or {}),
            **linked_resource_deterministic,
            **preference_research_deterministic,
            **classified_shared_answers,
        }
        answers = apply_draft_overrides(question_specs, merged_answers, out_dir=out_dir)
        return _validate_generated_answers(
            question_specs,
            answers,
            application_profile=application_profile,
        )

    if not force_fresh_generation:
        current_answers_payload = _load_optional_json(answers_path)
        cached_answers = load_cached_application_answers(
            answers_path,
            question_specs,
            linked_resource_cache_key=linked_resource_cache_key,
            preference_research_cache_key=preference_research_cache_key,
        )
        if cached_answers is not None:
            original_cached_answers = dict(cached_answers)
            cached_answers = _merge_deterministic_answers(cached_answers)
            if cached_answers != original_cached_answers:
                _write_answers_payload(
                    answers=cached_answers,
                    provider_name=str((current_answers_payload or {}).get("provider") or provider),
                    existing_payload=current_answers_payload,
                )
            return cached_answers
        cached_answers_path = find_matching_cached_application_answers_path(
            out_dir,
            current_answers_path=answers_path,
            question_specs=question_specs,
            linked_resource_cache_key=linked_resource_cache_key,
            preference_research_cache_key=preference_research_cache_key,
        )
        if cached_answers_path is not None:
            cached_payload = _load_optional_json(cached_answers_path)
            if cached_payload:
                answers_path.write_text(_json_dumps_pretty(cached_payload) + "\n", encoding="utf-8")
            cached_answers = load_cached_application_answers(
                cached_answers_path,
                question_specs,
                linked_resource_cache_key=linked_resource_cache_key,
                preference_research_cache_key=preference_research_cache_key,
            )
            if cached_answers is not None:
                original_cached_answers = dict(cached_answers)
                cached_answers = _merge_deterministic_answers(cached_answers)
                if cached_answers != original_cached_answers:
                    _write_answers_payload(
                        answers=cached_answers,
                        provider_name=str((cached_payload or {}).get("provider") or provider),
                        existing_payload=cached_payload,
                    )
                return cached_answers
    if not provider_question_specs:
        answers = _merge_deterministic_answers({})
        deterministic_provider = (
            "deterministic_submit_context"
            if (linked_resource_deterministic or classified_shared_answers) and preference_research_deterministic
            else "deterministic_preference_research"
            if preference_research_deterministic
            else "deterministic_classification"
            if classified_shared_answers
            else "deterministic_linked_resource"
        )
        _write_answers_payload(answers=answers, provider_name=deterministic_provider)
        return answers

    prompt = _build_application_answers_prompt(
        provider=provider,
        meta=meta,
        question_specs=provider_question_specs,
        jd_parsed=jd_parsed,
        resume_content=resume_content,
        research_cache=research_cache,
        cover_letter_text=cover_letter_text,
        master_resume_text=master_resume_text,
        work_stories_text=work_stories_text,
        candidate_context_text=candidate_context_text,
        application_profile_text=application_profile_text,
        linked_resource_context=str(linked_resource_payload.get("prompt_context") or "").strip() or None,
    )
    timeout_seconds = provider_timeout_seconds()

    def run_provider(
        current_provider: str, current_raw_output_path: Path
    ) -> tuple[dict[str, str] | None, Exception | None]:
        return _run_answer_generation_provider(
            provider=current_provider,
            prompt=prompt,
            raw_output_path=current_raw_output_path,
            timeout_seconds=timeout_seconds,
            question_specs=provider_question_specs,
            request_id=refresh_request_id,
            command_builder=provider_command_for_mode,
        )

    raw_answers, error = run_provider(provider, raw_output_path)
    final_provider = provider
    # Use provider chain from env for fallback (respects user's sole-provider preference)
    if error is not None:
        from application_submit_common import _answer_generation_fallback_provider

        fallback_provider = _answer_generation_fallback_provider(provider)
        if fallback_provider:
            fallback_raw_path = role_submit_path(out_dir, APPLICATION_ANSWER_FALLBACK_RAW)
            raw_answers, fallback_error = run_provider(fallback_provider, fallback_raw_path)
            if fallback_error is None:
                final_provider = fallback_provider
                error = None
            else:
                raise RuntimeError(
                    f"{error} Fallback answer generation via {fallback_provider} also failed. "
                    f"See {fallback_raw_path} for details."
                ) from fallback_error
    if error is not None:
        raise error

    merged_answers = {
        **(raw_answers or {}),
        **linked_resource_deterministic,
        **preference_research_deterministic,
        **classified_shared_answers,
    }
    answers, blockers = validate_generated_answers_with_blockers(
        question_specs,
        merged_answers,
        application_profile=application_profile,
    )
    answers = apply_draft_overrides(question_specs, answers, out_dir=out_dir)
    answers, blockers = validate_generated_answers_with_blockers(
        question_specs,
        answers,
        application_profile=application_profile,
    )
    if blockers:
        retry_raw_answers, retry_error = run_provider(final_provider, raw_output_path)
        if retry_error is None:
            retry_merged_answers = {
                **(retry_raw_answers or {}),
                **linked_resource_deterministic,
                **preference_research_deterministic,
                **classified_shared_answers,
            }
            answers, blockers = validate_generated_answers_with_blockers(
                question_specs,
                retry_merged_answers,
                application_profile=application_profile,
            )
            answers = apply_draft_overrides(question_specs, answers, out_dir=out_dir)
            answers, blockers = validate_generated_answers_with_blockers(
                question_specs,
                answers,
                application_profile=application_profile,
            )
        if blockers:
            raise GeneratedAnswerBlockersError(blockers, valid_answers=answers)
    answers_payload = {
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "provider": final_provider,
        "refresh_request_id": refresh_request_id,
        "questions": question_specs,
        "answers": answers,
    }
    linked_resources = _linked_resource_answer_payload(linked_resource_payload)
    if linked_resources is not None:
        answers_payload["linked_resources"] = linked_resources
    preference_research = _preference_research_answer_payload(preference_research_payload)
    if preference_research is not None:
        answers_payload["preference_research"] = preference_research
    answers_path.write_text(_json_dumps_pretty(answers_payload) + "\n", encoding="utf-8")
    return answers


def _question_step(
    *,
    question: dict,
    profile: CandidateProfile,
    application_profile: ApplicationProfile,
    company_name: str,
    cover_letter: str,
    cover_letter_file: Path | None,
    generated_answers: dict[str, str | list[str]],
    role_location: str | None = None,
    job_url: str | None = None,
    source_url: str | None = None,
    source_hint: str | None = None,
) -> dict | list[dict] | None:
    fields = list(question.get("fields") or [])
    if not fields:
        return None
    if _question_matches(question, "cover letter") and cover_letter_file:
        field = next((candidate for candidate in fields if candidate.get("type") == "input_file"), fields[0])
    else:
        field = fields[0]
    field_name = field["name"]
    field_type = field["type"]
    optional = not question["required"]
    label = _question_label(question)
    selector = _selector_for_field(field_name)

    if field_name in {"first_name", "last_name", "email", "phone", "resume", "resume_text"}:
        return None
    if _is_classic_demographic_field_name(field_name):
        return None
    if _question_is_voluntary_demographic_decline_opt_out(question):
        return None

    has_worked_for_company = company_name.casefold() in profile.employers
    value: str | None = None
    option: str | None = None
    search: str | None = None
    source: str | None = None
    visible_self_id_profile_field: str | None = None
    visible_profile_field: str | None = None
    category = classify_question(_question_text(question), field_type=field_type)
    policy = resolve_shared_question_policy(
        _question_text(question),
        application_profile,
        company_name=company_name,
        job_url=job_url,
        source_url=source_url,
        source_hint=source_hint,
    )
    prefer_generated_free_text = question_prefers_generated_free_text_answer(
        _question_text(question),
        field_type=field_type,
        category=category,
        policy=policy,
    )

    def _apply_visible_blocker(step: dict | None) -> dict | None:
        if step is None:
            return None
        if visible_self_id_profile_field:
            return mark_visible_self_id_step(step, profile_field=visible_self_id_profile_field)
        if visible_profile_field:
            return mark_visible_profile_field_step(step, profile_field=visible_profile_field)
        return step

    if prefer_generated_free_text and field_name in generated_answers:
        if field_type == "multi_value_single_select":
            try:
                option = _match_option_label(field, generated_answers[field_name])
            except ValueError:
                return None
            source = "generated_application_answer"
        elif field_type == "multi_value_multi_select" and isinstance(generated_answers[field_name], list):
            steps = []
            for item in generated_answers[field_name]:
                try:
                    matched = _match_option_label(field, item)
                except ValueError:
                    continue
                if matched:
                    steps.append(
                        {
                            "kind": "checkbox",
                            "field_name": field_name,
                            "selector": selector,
                            "label": label,
                            "option": matched,
                            "optional": optional,
                            "source": "generated_application_answer",
                        }
                    )
            return steps if steps else None
        else:
            value = generated_answers[field_name]
            source = "generated_application_answer"

    elif field_type == "multi_value_single_select" and len(field.get("values", [])) == 1 and category != "skill_years_experience":
        option = _single_option_label(field)
        source = "deterministic"
    elif category == "application_status_sms_optin":
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, True)
        else:
            value = "Yes"
        source = policy.source if policy is not None else "deterministic"
    elif category == "profile_included_confirmation":
        included_boolean = policy.boolean_value if policy is not None and policy.boolean_value is not None else False
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, included_boolean)
        else:
            value = "Yes" if included_boolean else "No"
        source = policy.source if policy is not None else "application_profile.md"
    elif _question_matches(
        question,
        "how did you hear about us",
        "how did you hear",
        "how did you first hear about",
        "how did you learn about us",
        "how did you learn about",
        "how did you first learn",
        "how did you find this position",
        "how did you find this role",
        "how did you find this opportunity",
        "how did you find this job",
        "where did you hear about",
    ):
        heard_values, heard_source = resolve_how_did_you_hear_candidates(
            application_profile,
            company_name=company_name,
            job_url=job_url,
            source_url=source_url,
            source_hint=source_hint,
        )
        if field_type == "multi_value_single_select":
            option = _how_did_you_hear_option_label(
                field,
                application_profile,
                company_name,
                job_url=job_url,
                source_url=source_url,
                source_hint=source_hint,
            )
        else:
            value = (
                heard_values[0] if heard_values else application_profile.how_did_you_hear or f"{company_name} Website"
            )
        source = heard_source
    elif _question_matches(question, "linkedin") and field_type in {"textarea", "input_text"}:
        value = application_profile.linkedin or profile.linkedin
        visible_profile_field = "linkedin"
        source = "application_profile.md" if application_profile.linkedin else "master_resume.md"
    elif _question_matches(question, "github") and field_type in {"textarea", "input_text"}:
        value = application_profile.github
        source = "application_profile.md" if application_profile.github else "not_provided"
    elif _question_matches(question, "cover letter") and field_type == "input_file":
        if not cover_letter_file:
            return None
        return {
            "kind": "file",
            "field_name": field_name,
            "selector": selector,
            "label": label,
            "file_path": str(cover_letter_file),
            "optional": False,
            "source": "existing_cover_letter_asset",
        }
    elif _question_matches(question, "website", "portfolio") and field_type in {"textarea", "input_text"}:
        value = application_profile.website or profile.website
        source = "application_profile.md" if application_profile.website else "master_resume.md"
    elif question_requests_current_professional_license_inventory(_question_text(question)):
        credential_inventory_answer = current_professional_license_inventory_answer(_question_text(question))
        if credential_inventory_answer is None:
            return None
        if field_type in {"multi_value_single_select", "multi_value_multi_select"}:
            if credential_inventory_answer == "None":
                option = _no_current_professional_license_option_label(field)
            else:
                option = _match_option_label(field, credential_inventory_answer)
                if option is None:
                    return None
        else:
            value = credential_inventory_answer
        source = (
            "deterministic_no_professional_credentials" if credential_inventory_answer == "None" else "master_resume.md"
        )
    elif _question_is_cover_letter_slot(question):
        value = cover_letter.strip()
        source = "cover_letter_text.txt"
        optional = False
    elif _question_matches(question, "legal first and last name", "legal full name"):
        value = profile.full_name
        source = "master_resume.md"
    elif _question_matches(question, "legal first name"):
        value = profile.first_name
        source = "master_resume.md"
    elif _question_matches(question, "legal last name"):
        value = profile.last_name
        source = "master_resume.md"
    elif _question_matches(question, "legal name"):
        # For "Legal Name (if different than above)", fill the full name and let review correct true differences.
        value = f"{profile.first_name} {profile.last_name}"
        source = "master_resume.md"
    elif _question_matches(question, "confirm your email", "confirm email address", "confirm email"):
        value = profile.email or application_profile.verification_code_email
        source = "master_resume.md" if profile.email else "application_profile.md"
    elif _question_matches(question, "preferred first name", "preferred name"):
        value = profile.first_name
        source = "master_resume.md"
    elif _question_matches(question, "preferred last name"):
        value = profile.last_name
        source = "master_resume.md"
    elif _question_matches(question, "review the nda", "typing your full name below", "type your full name below"):
        value = profile.full_name
        source = "master_resume.md"
    elif (category == "current_company" or _question_is_current_company(question)) and not _is_acknowledgment_field(
        field
    ):
        employer_name = primary_employer_name()
        if field_type == "multi_value_single_select":
            option = _match_option_label(field, employer_name)
        else:
            value = employer_name
        source = "master_resume.md"
    elif (
        _question_matches(
            question,
            "based in any of these countries",
            "only countries where we are accepting applications",
            "which of these countries are you based in",
        )
        and field_type == "multi_value_single_select"
        or _question_matches(
            question,
            "countries you anticipate working",
            "country or countries you anticipate",
            "select the country or countries",
        )
        and field_type == "multi_value_multi_select"
    ):
        option = _match_option_label(field, application_profile.country)
        source = "application_profile.md"
    elif (
        _question_matches(question, "country")
        and not _question_matches(
            question,
            "sponsorship",
            "sponsor",
            "visa",
            "countries",
            "authorization",
            "authorisation",
            "authorized",
            "authorised",
            "eligible to work",
            "eligible for employment",
            "right to work",
            "intend to work",
            "candidate location",
            "location (city)",
            "current location",
            "what cities",
            "in what cities",
        )
        and not _field_has_yes_no_options(field)
    ):
        if field_type == "multi_value_single_select":
            option = _match_option_label(field, application_profile.country)
        else:
            value = application_profile.country
        source = "application_profile.md"
    elif _question_targets_current_residence(question, category=category):
        current_residence_asks_for_state = _question_targets_current_residence_state(question)
        try:
            if field_type in {"multi_value_single_select", "multi_value_multi_select"}:
                option = (
                    _candidate_state_option_label(field, application_profile)
                    if current_residence_asks_for_state
                    else _candidate_location_option_label(field, application_profile)
                )
                search = option
            else:
                if current_residence_asks_for_state:
                    value = _candidate_state_text(application_profile)
                    if not value:
                        raise ValueError(
                            "Could not derive a parseable state from application_profile.md "
                            f"location {application_profile.location!r}."
                        )
                else:
                    value = application_profile.location
                    if not value:
                        raise ValueError("application_profile.md does not include a candidate location.")
            source = "application_profile.md"
            visible_profile_field = "location"
        except ValueError as exc:
            if not optional:
                return _planned_unconfirmed_question_step(
                    field=field,
                    selector=selector,
                    label=label,
                    optional=optional,
                    source="application_profile.md",
                    report_value=(
                        "Greenhouse left this current-residence field unresolved because "
                        f"{exc} Runtime did not fall back to the role location."
                    ),
                )
            return None
    elif _question_targets_work_location(question):
        if field_type in ("textarea", "input_text") and _question_matches(question, "country"):
            value = f"{application_profile.location}, {application_profile.country}"
        else:
            value = application_profile.location
        option = (
            _location_option_label(field, application_profile, role_location=role_location)
            if field_type in ("multi_value_single_select", "multi_value_multi_select")
            else None
        )
        search = _location_search_text(option or application_profile.location) if option else None
        visible_profile_field = "location"
        source = "application_profile.md"
    elif category == "city_location":
        if field_type == "multi_value_single_select":
            try:
                option = _candidate_location_option_label(field, application_profile)
            except ValueError:
                try:
                    option = _candidate_state_option_label(field, application_profile)
                except ValueError:
                    option = _location_option_label(field, application_profile, role_location=role_location)
            search = _location_search_text(option or application_profile.location or "")
        elif field_type == "multi_value_multi_select":
            try:
                option = _candidate_location_option_label(field, application_profile)
            except ValueError:
                option = _location_option_label(field, application_profile, role_location=role_location)
            search = _location_search_text(option or application_profile.location or "")
        else:
            value = application_profile.location
        visible_profile_field = "location"
        source = "application_profile.md"
    elif category == "location_cost_tier" and policy is not None and policy.text_value:
        if field_type == "multi_value_single_select":
            option = _match_option_label(field, policy.text_value)
        else:
            value = policy.text_value
        visible_profile_field = "location"
        source = policy.source
    elif category == "location_residency" and policy is not None and policy.boolean_value is not None:
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, bool(policy.boolean_value))
        else:
            value = "Yes" if policy.boolean_value else "No"
        visible_profile_field = "location"
        source = policy.source
    elif field_type in {"textarea", "input_text"} and (
        onsite_answer := build_onsite_start_location_answer(_question_text(question), application_profile)
    ):
        value = onsite_answer
        source = "application_profile.md"
    elif (state_membership := _state_membership_answer(question, application_profile)) is not None:
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, state_membership)
        else:
            value = "Yes" if state_membership else "No"
        source = "application_profile.md"
    elif _question_matches(
        question, "do you live in", "currently live in", "reside in", "based in", "located in"
    ) and category != "work_authorization" and not _question_matches(
        question,
        "immigration",
        "visa",
        "sponsorship",
        "if you answered",
    ):
        if field_type == "multi_value_single_select":
            try:
                option = _yes_no_option_label(field, True)
            except ValueError:
                # Options are not yes/no — likely a state/province/location dropdown.
                option = _location_option_label(field, application_profile, role_location=role_location)
        elif _question_matches(question, "what state", "what country", "what city", "located in"):
            # Free-text asking for the actual location, not a yes/no.
            value = application_profile.location or "California"
        else:
            value = "Yes"
        visible_profile_field = "location"
        source = policy.source if policy is not None and policy.boolean_value is not None else "application_profile.md"
    elif _question_matches(
        question,
        "commuting distance",
        "commute",
        "office proximity",
        "within commuting distance",
    ):
        if field_type == "multi_value_single_select":
            option = _location_aware_option_label(field, application_profile)
        else:
            value = "Yes"
        source = policy.source if policy is not None and policy.boolean_value is not None else "application_profile.md"
    elif question_is_relocation_willingness(_question_text(question)):
        if field_type in {"multi_value_single_select", "multi_value_multi_select"}:
            option = _location_aware_option_label(field, application_profile)
        else:
            value = "Yes"
        source = (
            policy.source if policy is not None and policy.boolean_value is not None else "shared_positive_fit_policy"
        )
    elif _question_matches(question, "willing to work from the required location", "willing to work from"):
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, True)
        else:
            value = "Yes"
        source = "shared_positive_fit_policy"
    elif category == "role_commitment":
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, True)
        else:
            value = "Yes"
        source = (
            policy.source if policy is not None and policy.boolean_value is not None else "shared_positive_fit_policy"
        )
    elif category == "office_attendance":
        if field_type == "multi_value_single_select":
            try:
                option = _location_aware_option_label(field, application_profile)
            except ValueError:
                option = _yes_no_option_label(field, True)
        else:
            value = "Yes"
        source = (
            policy.source if policy is not None and policy.boolean_value is not None else "shared_positive_fit_policy"
        )
    elif _question_matches(
        question,
        "working in-person",
        "in-person at our",
        "in office",
        "in-office",
        "on-site",
        "on site",
        "onsite",
        "days per week",
        "days/week",
        "office hub",
        "office hubs",
        "comfortable working on site",
        "comfortable working onsite",
        "comfortable working in person",
    ):
        if field_type == "multi_value_single_select":
            try:
                option = _location_aware_option_label(field, application_profile)
            except ValueError:
                option = None
            candidate_city = (application_profile.location or "").split(",")[0].strip().casefold()
            located_option = None
            relocate_option = None
            for opt in field.get("values", []):
                opt_label = str(opt["label"]).casefold()
                if "located" in opt_label and candidate_city and candidate_city in opt_label:
                    located_option = str(opt["label"]).strip()
                if "relocate" in opt_label:
                    relocate_option = str(opt["label"]).strip()

            if option:
                pass
            elif located_option:
                option = located_option
            elif relocate_option:
                option = relocate_option
            else:
                option = _yes_no_option_label(field, True)
        else:
            value = "Yes"
        source = (
            policy.source if policy is not None and policy.boolean_value is not None else "shared_positive_fit_policy"
        )
    elif category == "salary_comfort":
        if field_type == "multi_value_single_select":
            option = _salary_comfort_option_label(field, application_profile.comfortable_with_posted_salary)
        else:
            value = "Yes" if application_profile.comfortable_with_posted_salary else "No"
        source = "application_profile.md"
    elif policy is not None and policy.category == "compensation" and field_type == "multi_value_single_select":
        option = _non_numeric_compensation_option_label(field)
        if option is not None:
            source = policy.source
        elif not optional:
            return _planned_unconfirmed_question_step(
                field=field,
                selector=selector,
                label=label,
                optional=optional,
                source="application_profile.md",
                report_value=(
                    "Greenhouse left this compensation field unresolved because the form only offers numeric "
                    "salary ranges and project policy requires non-numeric compensation answers."
                ),
            )
        else:
            return None
    elif (
        policy is not None
        and policy.category in {"compensation", "undergraduate_gpa"}
        and field_type in {"textarea", "input_text"}
    ):
        if policy.text_value is None:
            missing_field = "Compensation Expectations" if policy.category == "compensation" else "Undergraduate GPA"
            return _planned_unconfirmed_question_step(
                field=field,
                selector=selector,
                label=label,
                optional=optional,
                source="application_profile.md",
                report_value=(
                    f"Greenhouse left this field unresolved because application_profile.md is missing {missing_field}."
                ),
            )
        value = policy.text_value
        source = policy.source
    elif category in {
        "nda_noncompete",
        "conflict_of_interest",
        "prior_employment",
        "prior_application",
        "current_employer_affiliation",
        "pm_people_management",
        "employee_referral",
    }:
        negative_boolean = policy.boolean_value if policy is not None and policy.boolean_value is not None else False
        negative_answer = (
            policy.text_value
            if policy is not None and policy.text_value is not None
            else "Yes"
            if negative_boolean
            else "No"
        )
        if field_type == "multi_value_single_select":
            try:
                option = _yes_no_option_label(field, negative_boolean)
            except ValueError:
                if category in {"prior_employment", "prior_application", "current_employer_affiliation"}:
                    option = match_prior_employer_option(
                        [str(candidate.get("label", "")).strip() for candidate in field.get("values", [])],
                        negative_boolean,
                    )
                else:
                    raise
        else:
            value = negative_answer
        source = policy.source if policy is not None else "deterministic"
    elif category == "truthfulness_attestation":
        affirmative_boolean = policy.boolean_value if policy is not None and policy.boolean_value is not None else True
        affirmative_answer = (
            policy.text_value
            if policy is not None and policy.text_value is not None
            else "Yes"
            if affirmative_boolean
            else "No"
        )
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, affirmative_boolean)
        else:
            value = affirmative_answer
        source = policy.source if policy is not None else "deterministic"
    elif category == "education":
        preferred_education = _preferred_education_entry(profile)
        education_text = (
            "\n".join(
                f"{entry.school}; {entry.degree_option}"
                + (f" in {entry.discipline_option}" if entry.discipline_option else "")
                for entry in profile.education_entries
            )
            if profile.education_entries
            else None
        )
        normalized_question = _normalize_free_text(_question_text(question))
        if (
            field_type == "multi_value_single_select"
            and _field_has_yes_no_options(field)
            and preferred_education
            and not any(
                fragment in normalized_question
                for fragment in (
                    "currently enrolled",
                    "current student",
                    "pursuing",
                    "in school",
                    "still in school",
                )
            )
        ):
            option = _yes_no_option_label(field, True)
            source = "master_resume.md"
        elif (
            _question_matches(question, "field of study", "discipline", "major", "concentration")
            and preferred_education
        ):
            if field_type == "multi_value_single_select":
                desired_values = [
                    preferred_education.discipline_option,
                    *_education_discipline_fallback_options(preferred_education),
                ]
                for desired in desired_values:
                    try:
                        option = _match_option_label(field, desired)
                        break
                    except ValueError:
                        continue
            else:
                value = preferred_education.discipline_option
            source = "master_resume.md"
        elif _question_matches(
            question,
            "most recent degree",
            "highest degree",
            "degree you obtained",
            "degree obtained",
            "highest level of education",
            "education level",
        ):
            if field_type == "multi_value_single_select" and preferred_education:
                desired_values = [
                    preferred_education.degree_option,
                    *_education_degree_fallback_options(preferred_education),
                ]
                for desired in desired_values:
                    try:
                        option = _match_option_label(field, desired)
                        break
                    except ValueError:
                        continue
                source = "master_resume.md"
            elif preferred_education:
                value = preferred_education.degree_option
                source = "master_resume.md"
            elif education_text:
                value = education_text
                source = "master_resume.md"
        elif _question_matches(question, "school", "university", "college") and preferred_education:
            if field_type == "multi_value_single_select":
                option = _match_option_label(field, preferred_education.school)
            else:
                value = preferred_education.school
            source = "master_resume.md"
        elif field_type == "multi_value_single_select" and preferred_education:
            desired_values = [
                preferred_education.degree_option,
                *_education_degree_fallback_options(preferred_education),
            ]
            for desired in desired_values:
                try:
                    option = _match_option_label(field, desired)
                    break
                except ValueError:
                    continue
            source = "master_resume.md"
        elif education_text:
            value = education_text
            source = "master_resume.md"
    elif category == "startup_experience":
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, True)
        else:
            value = policy.text_value if policy is not None and policy.text_value is not None else "Yes"
        source = (
            policy.source if policy is not None and policy.boolean_value is not None else "shared_positive_fit_policy"
        )
    elif category == "travel_willingness":
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, True)
        else:
            value = "Yes"
        source = (
            policy.source if policy is not None and policy.boolean_value is not None else "shared_positive_fit_policy"
        )
    elif _question_matches(question, "sponsorship", "sponsor", "visa") and not _question_matches(
        question, "authorized to work", "authorised to work", "eligible to work", "right to work"
    ):
        # Pure sponsorship questions get the sponsorship answer; mixed authorization prompts fall through below.
        requires_sponsorship = (
            application_profile.require_sponsorship_now or application_profile.require_sponsorship_future
        )
        if field_type == "multi_value_single_select":
            try:
                option = _yes_no_option_label(field, requires_sponsorship)
            except ValueError:
                option = _work_authorization_followup_option_label(field, application_profile)
        elif not requires_sponsorship and _is_conditional_followup({"label": _question_text(question)}):
            # Conditional follow-up asking for visa type/details — answer N/A
            value = "N/A"
        else:
            value = application_profile.sponsorship_answer
        source = "application_profile.md"
    elif _question_targets_post_citizenship_permanent_residency(question):
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, False)
        else:
            value = "No"
        source = "application_profile.md"
    elif (
        _question_matches(
            question,
            "authorized to work",
            "authorised to work",
            "authorized to lawfully work",
            "authorised to lawfully work",
            "work authorization",
            "work authorisation",
            "right to work",
            "legally authorized",
            "legally authorised",
            "eligible to work",
        )
        or category == "work_authorization"
    ):
        work_authorization_boolean = (
            policy.boolean_value
            if policy is not None and policy.boolean_value is not None
            else application_profile.authorized_to_work_unconditionally
        )
        if field_type in ("multi_value_single_select", "multi_value_multi_select"):
            try:
                option = _yes_no_option_label(field, work_authorization_boolean)
            except ValueError:
                option = _work_authorization_option_label(field, application_profile)
        else:
            value = (
                build_truthful_work_authorization_answer(_question_text(question), application_profile)
                or application_profile.work_authorization_statement
            )
        source = "application_profile.md"
    elif _question_matches(question, "u.s. person", "us person"):
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, application_profile.authorized_to_work_unconditionally)
        else:
            value = "Yes" if application_profile.authorized_to_work_unconditionally else "No"
        source = "application_profile.md"
    elif _question_matches(question, "sanctions", "export control") and field_type == "multi_value_multi_select":
        # Sanctions/export-control residency questions should select "None of the above."
        try:
            option = _match_option_label(field, "None of the above")
        except ValueError:
            option = None  # skip if no "None of the above" option
        source = "application_profile.md"
    elif (
        _question_matches(question, "prior question")
        and _question_matches(
            question,
            "none of the above",
        )
        and field_type == "multi_value_multi_select"
    ):
        option_labels = [str(v.get("label", "")).strip() for v in field.get("values", [])]
        not_applicable_label = next(
            (ol for ol in option_labels if "not applicable" in ol.lower()),
            None,
        )
        if not_applicable_label:
            option = not_applicable_label
        else:
            option = _match_option_label(field, "U.S. citizen")
        source = "application_profile.md"
    elif category == "minimum_experience":
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, application_profile.minimum_years_experience)
        else:
            value = "Yes" if application_profile.minimum_years_experience else "No"
        source = "application_profile.md"
    elif _question_requests_product_management_experience_range(question):
        years = _candidate_product_management_experience_years(
            technical_focus_only=_question_requests_technical_pm_experience(question)
        )
        if years is None:
            return None
        if field_type == "multi_value_single_select":
            option = _experience_years_option_label(field, years)
        else:
            value = str(max(1, round(years)))
        source = "master_resume.md"
    elif category == "credential_claim":
        if (
            policy is None
            or policy.text_value is None
            or (not policy.credential_supported and policy.boolean_value is None)
        ):
            if not optional:
                return _planned_unconfirmed_question_step(
                    field=field,
                    selector=selector,
                    label=label,
                    optional=optional,
                    source="application_profile.md",
                    report_value=(
                        "Greenhouse left this credential claim unresolved because the claimed degree or "
                        "credential is not explicitly supported by application_profile.md or master_resume.md."
                    ),
                )
            return None
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, bool(policy.boolean_value))
        else:
            value = policy.text_value
        source = policy.source
    elif category == "skill_confirmation":
        if policy is None or policy.boolean_value is None:
            if not optional:
                return _planned_unconfirmed_question_step(
                    field=field,
                    selector=selector,
                    label=label,
                    optional=optional,
                    source="master_resume.md",
                    report_value=(
                        "Greenhouse left this skill confirmation unresolved because the claimed skill is "
                        "not explicitly supported by master_resume.md."
                    ),
                )
            return None
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, bool(policy.boolean_value))
        else:
            value = policy.text_value if policy.text_value is not None else ("Yes" if policy.boolean_value else "No")
        source = policy.source
    elif category == "skill_years_experience":
        if policy is None or policy.text_value is None:
            if not optional:
                return _planned_unconfirmed_question_step(
                    field=field,
                    selector=selector,
                    label=label,
                    optional=optional,
                    source="application_profile.md",
                    report_value=(
                        "Greenhouse left this years-of-experience field unresolved because no shared "
                        "resume-backed or profile-backed answer was available."
                    ),
                )
            return None
        if field_type in {"multi_value_single_select", "multi_value_multi_select"}:
            option = select_shared_policy_option(
                [str(candidate.get("label", "")).strip() for candidate in field.get("values", [])],
                policy,
                application_profile=application_profile,
            )
            if option is None:
                if not optional:
                    return _planned_unconfirmed_question_step(
                        field=field,
                        selector=selector,
                        label=label,
                        optional=optional,
                        source=policy.source,
                        report_value=(
                            "Greenhouse left this years-of-experience field unresolved because none of the "
                            f"visible options matched the shared answer {policy.text_value!r}."
                        ),
                    )
                return None
        else:
            value = policy.text_value
        source = policy.source
    elif category == "background_check_consent" or category == "interview_ai_policy_consent":
        affirmative_boolean = policy.boolean_value if policy is not None and policy.boolean_value is not None else True
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, affirmative_boolean)
        else:
            value = (
                policy.text_value
                if policy is not None and policy.text_value is not None
                else ("Yes" if affirmative_boolean else "No")
            )
        source = policy.source if policy is not None else "deterministic"
    elif category == "relocation_assistance_requirement":
        requires_assistance = policy.boolean_value if policy is not None and policy.boolean_value is not None else False
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, requires_assistance)
        else:
            value = (
                policy.text_value
                if policy is not None and policy.text_value is not None
                else ("Yes" if requires_assistance else "No")
            )
        source = policy.source if policy is not None else "deterministic"
    elif category == "experience_confirmation":
        affirmative_boolean = policy.boolean_value if policy is not None and policy.boolean_value is not None else True
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, affirmative_boolean)
        else:
            value = (
                policy.text_value
                if policy is not None and policy.text_value is not None
                else ("Yes" if affirmative_boolean else "No")
            )
        source = (
            policy.source if policy is not None and policy.boolean_value is not None else "shared_positive_fit_policy"
        )
    elif category == "product_usage":
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, True)
        else:
            value = "Yes"
        source = (
            policy.source if policy is not None and policy.boolean_value is not None else "shared_positive_fit_policy"
        )
    elif category == "company_engagement":
        if field_type == "multi_value_single_select":
            try:
                if not _field_has_yes_no_options(field):
                    raise ValueError("engagement field uses richer options")
                option = _yes_no_option_label(field, True)
            except ValueError:
                option = _best_engagement_option(
                    [str(candidate.get("label", "")).strip() for candidate in field.get("values", [])]
                )
        else:
            value = "Yes"
        source = "deterministic"
    elif category == "culture_careers_optin":
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, False)
        else:
            value = "No"
        source = "deterministic_override"
    elif category == "interview_accommodation":
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, False)
        else:
            value = "No"
        source = "deterministic"
    elif _question_matches(
        question,
        "worked for",
        "previously employed",
        "previously been employed",
        "contractor/consultant",
        "former",
        "employed by",
    ):
        if field_type == "multi_value_single_select":
            try:
                option = _yes_no_option_label(field, has_worked_for_company)
            except ValueError:
                option = match_prior_employer_option(
                    [str(candidate.get("label", "")).strip() for candidate in field.get("values", [])],
                    has_worked_for_company,
                )
        else:
            value = "Yes" if has_worked_for_company else "No"
        source = "master_resume.md"
    elif _question_matches(question, "pronouns"):
        if field_type == "multi_value_single_select" and application_profile.pronouns:
            option = _greenhouse_profile_option_label(field, application_profile.pronouns, profile_field="pronouns")
        else:
            value = application_profile.pronouns
        visible_self_id_profile_field = "pronouns"
        source = "application_profile.md"
    elif field_type == "multi_value_single_select" and (
        len(field.get("values", [])) == 1
        or _question_matches(
            question,
            "acknowledge",
            "confirm",
            "reviewed",
            "accurate and complete",
            "double check",
            "double-check",
            "accuracy",
            "privacy policy",
            "candidate privacy",
        )
    ):
        option = _single_option_label(field)
        source = "application_profile.md"
    elif _question_matches(
        question, "big data", "data warehousing", "data lakes", "data lakehouse", "trino", "iceberg"
    ):
        if field_type == "multi_value_single_select":
            option = _yes_no_option_label(field, True)
        else:
            value = "Yes"
        source = "master_resume.md"
    elif _question_matches(question, "transgender") and application_profile.transgender_status:
        if field_type == "multi_value_single_select":
            option = _greenhouse_profile_option_label(
                field,
                application_profile.transgender_status,
                profile_field="transgender_status",
            )
        else:
            value = application_profile.transgender_status
        visible_self_id_profile_field = "transgender_status"
        source = "application_profile.md"
    elif (_question_matches(question, "gender") or re.search(r"\bsex\b", _question_text(question))) and (
        getattr(application_profile, "gender_identity", None) or application_profile.gender
    ):
        desired_gender = getattr(application_profile, "gender_identity", None) or application_profile.gender
        gender_profile_field = "gender_identity" if getattr(application_profile, "gender_identity", None) else "gender"
        if field_type == "multi_value_single_select":
            option = _greenhouse_profile_option_label(field, desired_gender, profile_field=gender_profile_field)
        else:
            value = desired_gender
        visible_self_id_profile_field = gender_profile_field
        source = "application_profile.md"
    elif _question_matches(question, "race", "ethnicity") and application_profile.race_or_ethnicity:
        if field_type == "multi_value_single_select":
            option = _greenhouse_profile_option_label(
                field,
                application_profile.race_or_ethnicity,
                profile_field="race_or_ethnicity",
            )
        else:
            value = application_profile.race_or_ethnicity
        visible_self_id_profile_field = "race_or_ethnicity"
        source = "application_profile.md"
    elif _question_matches(question, "veteran", "military") and application_profile.veteran_status:
        if field_type == "multi_value_single_select":
            option = _greenhouse_profile_option_label(
                field,
                application_profile.veteran_status,
                profile_field="veteran_status",
            )
        else:
            value = application_profile.veteran_status
        visible_self_id_profile_field = "veteran_status"
        source = "application_profile.md"
    elif _question_matches(question, "disability") and application_profile.disability_status:
        if field_type == "multi_value_single_select":
            option = _greenhouse_profile_option_label(
                field,
                application_profile.disability_status,
                profile_field="disability_status",
            )
        else:
            value = application_profile.disability_status
        visible_self_id_profile_field = "disability_status"
        source = "application_profile.md"
    elif _question_matches(question, "sexual orientation") and application_profile.sexual_orientation:
        if field_type == "multi_value_single_select":
            option = _greenhouse_profile_option_label(
                field,
                application_profile.sexual_orientation,
                profile_field="sexual_orientation",
            )
        else:
            value = application_profile.sexual_orientation
        visible_self_id_profile_field = "sexual_orientation"
        source = "application_profile.md"
    elif _question_matches(question, "what is your age", "age range", "age group", "which age group"):
        if application_profile.age_range:
            if field_type == "multi_value_single_select":
                option = _match_option_label(field, application_profile.age_range)
            else:
                value = application_profile.age_range
            visible_profile_field = "age_range"
            source = "application_profile.md"
        elif not optional:
            return _planned_unconfirmed_question_step(
                field=field,
                selector=selector,
                label=label,
                optional=optional,
                source="application_profile.md",
                report_value=(
                    "Greenhouse left this age question unresolved because application_profile.md "
                    "does not include a truthful Age Range / Age Group value."
                ),
            )
        else:
            return None
    elif field_name in generated_answers:
        if field_type == "multi_value_single_select":
            try:
                option = _match_option_label(field, generated_answers[field_name])
            except ValueError:
                return None
        elif field_type == "multi_value_multi_select" and isinstance(generated_answers[field_name], list):
            # Multi-select: return a list of checkbox steps (one per selected option)
            steps = []
            for item in generated_answers[field_name]:
                try:
                    matched = _match_option_label(field, item)
                except ValueError:
                    continue
                if matched:
                    steps.append(
                        {
                            "kind": "checkbox",
                            "field_name": field_name,
                            "selector": selector,
                            "label": label,
                            "option": matched,
                            "optional": optional,
                            "source": "generated_application_answer",
                        }
                    )
            return steps if steps else None
        else:
            value = generated_answers[field_name]
        source = "generated_application_answer"
    else:
        return None

    if field_type == "multi_value_single_select":
        if not option:
            return None
        return _apply_visible_blocker(
            {
                "kind": "combobox",
                "field_name": field_name,
                "selector": selector,
                "label": label,
                "search": search or option,
                "option": option,
                "optional": optional,
                "source": source,
                "profile_field": visible_self_id_profile_field,
            }
        )

    if field_type == "multi_value_multi_select" and option:
        return _apply_visible_blocker(
            {
                "kind": "checkbox",
                "field_name": field_name,
                "selector": selector,
                "label": label,
                "option": option,
                "optional": optional,
                "source": source,
                "profile_field": visible_self_id_profile_field,
            }
        )

    # Only textarea and input_text can be filled as text fields.
    # Other types (e.g. multi_value_multi_select / checkbox fieldsets) are
    # handled at runtime by fill_discovered_profile_questions or
    # sync_runtime_confirmations, so skip them here.
    if field_type not in {"textarea", "input_text"}:
        return None

    kind = "textarea" if field_type == "textarea" else "text"
    return _apply_visible_blocker(
        {
            "kind": kind,
            "field_name": field_name,
            "selector": selector,
            "label": label,
            "value": value,
            "optional": optional,
            "source": source,
            "profile_field": visible_self_id_profile_field,
        }
    )


def _education_fields_visible(job_post: dict) -> bool:
    # Check the parsed HTML for actual education field presence.
    html = job_post.get("_raw_html", "")
    if html:
        has_education_inputs = (
            "education_school_name" in html
            or "education_degree" in html
            or "education_discipline" in html
            or "educations[]" in html
            # Remix UI education fields
            or "education--container" in html
            or 'id="school--0"' in html
        )
        if not has_education_inputs:
            return False

    config = job_post.get("education_config")
    if not isinstance(config, dict) or not config:
        return True

    tracked_fields = (
        "school_name",
        "degree",
        "discipline",
        "start_month",
        "start_year",
        "end_month",
        "end_year",
    )
    states = [str(config.get(field, "")).strip().lower() for field in tracked_fields if field in config]
    if not states:
        return True
    return any(state and state != "hidden" for state in states)


def _build_steps(
    job_post: dict,
    meta: dict,
    profile: CandidateProfile,
    application_profile: ApplicationProfile,
    out_dir: Path,
    *,
    generated_answers: dict[str, str | list[str]] | None = None,
) -> list[dict]:
    cover_letter = _find_cover_letter_text(out_dir)
    try:
        cover_letter_file = _find_cover_letter_file(out_dir)
    except FileNotFoundError:
        cover_letter_file = None
    resume_path = _find_resume_file(out_dir)
    generated_answers_was_computed = generated_answers is None
    if generated_answers is None:
        provider = _default_answer_provider()
        generated_answers = _generate_application_answers(
            out_dir=out_dir,
            meta=meta,
            job_post=job_post,
            provider=provider,
        )
    preference_research_blockers = _preference_research_drift_blockers(job_post, out_dir=out_dir)
    if preference_research_blockers:
        raise GeneratedAnswerBlockersError(preference_research_blockers, valid_answers=generated_answers)
    if generated_answers_was_computed:
        _verify_generated_answers_for_current_draft(
            out_dir=out_dir,
            meta=meta,
            job_post=job_post,
            generated_answers=generated_answers,
            application_profile=application_profile,
        )

    steps: list[dict] = []
    if resume_path is not None:
        steps.append(
            {
                "kind": "file",
                "field_name": "resume",
                "selector": "input[type='file']",
                "label": "Resume/CV",
                "file_path": str(resume_path),
                "source": "existing_resume_asset",
            }
        )
    steps += [
        {
            "kind": "text",
            "field_name": "first_name",
            "selector": "#first_name",
            "label": "First Name",
            "value": profile.first_name,
            "source": "master_resume.md",
        },
        {
            "kind": "text",
            "field_name": "last_name",
            "selector": "#last_name",
            "label": "Last Name",
            "value": profile.last_name,
            "source": "master_resume.md",
        },
        {
            "kind": "text",
            "field_name": "email",
            "selector": "#email",
            "label": "Email",
            "value": profile.email,
            "source": "master_resume.md",
        },
        {
            "kind": "text",
            "field_name": "phone",
            "selector": "#phone",
            "label": "Phone",
            "value": profile.phone,
            "source": "master_resume.md",
        },
    ]
    steps[-1] = mark_visible_profile_field_step(steps[-1], profile_field="phone")

    education_entry = _preferred_education_entry(profile)
    if education_entry and _education_fields_visible(job_post):
        steps.extend(
            [
                {
                    "kind": "combobox",
                    "field_name": "job_application[educations][][school_name_id]",
                    "selector": '#application_form [id^="s2id_education_school_name_"] a.select2-choice, #application-form [id^="s2id_education_school_name_"] a.select2-choice',
                    "label": "School",
                    "search": education_entry.school,
                    "option": education_entry.school,
                    "optional": True,
                    "source": "master_resume.md",
                },
                {
                    "kind": "combobox",
                    "field_name": "job_application[educations][][degree_id]",
                    "selector": '#application_form select[id^="education_degree_"], #application-form select[id^="education_degree_"]',
                    "label": "Degree",
                    "search": education_entry.degree_option,
                    "option": education_entry.degree_option,
                    "fallback_options": _education_degree_fallback_options(education_entry),
                    "optional": True,
                    "source": "master_resume.md",
                },
                {
                    "kind": "combobox",
                    "field_name": "job_application[educations][][discipline_id]",
                    "selector": '#application_form select[id^="education_discipline_"], #application-form select[id^="education_discipline_"]',
                    "label": "Discipline",
                    "search": education_entry.discipline_option,
                    "option": education_entry.discipline_option,
                    "fallback_options": _education_discipline_fallback_options(education_entry),
                    "optional": True,
                    "source": "master_resume.md",
                },
                {
                    "kind": "text",
                    "field_name": "job_application[educations][][end_date][year]",
                    "selector": '#application_form [name="job_application[educations][][end_date][year]"], #application-form [name="job_application[educations][][end_date][year]"]',
                    "label": "Education End Year",
                    "value": education_entry.end_year,
                    "optional": True,
                    "source": "master_resume.md",
                },
            ]
        )

    jd_parsed = (
        _load_optional_json(role_content_path(out_dir, "jd_parsed.json"))
        or _load_optional_json(out_dir / "jd_parsed.json")
        or {}
    )
    role_location = str(jd_parsed.get("location") or "").strip() or None

    for question in _all_questions(job_post):
        step = _question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name=meta["company_proper"],
            cover_letter=cover_letter,
            cover_letter_file=cover_letter_file,
            generated_answers=generated_answers,
            role_location=role_location,
            job_url=str(meta.get("jd_source") or ""),
            source_url=str(meta.get("source_url") or ""),
            source_hint=str(meta.get("source") or ""),
        )
        if step:
            # _question_step returns a list for multi-select, dict for single
            if isinstance(step, list):
                steps.extend(step)
            else:
                steps.append(step)

    return steps


def _verify_generated_answers_for_current_draft(
    *,
    out_dir: Path,
    meta: dict,
    job_post: dict,
    generated_answers: dict[str, str | list[str]],
    application_profile: ApplicationProfile,
) -> dict:
    answers_payload = _load_optional_json(role_submit_path(out_dir, APPLICATION_ANSWER_CACHE)) or {}
    raw_question_specs = answers_payload.get("questions")
    if isinstance(raw_question_specs, list) and raw_question_specs:
        question_specs = [dict(spec) for spec in raw_question_specs if isinstance(spec, dict)]
    else:
        question_specs = _application_question_specs(job_post)

    application_profile_text = APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
    master_resume_text = MASTER_RESUME_PATH.read_text(encoding="utf-8")
    work_stories_text = WORK_STORIES_PATH.read_text(encoding="utf-8")
    candidate_context_text = CANDIDATE_CONTEXT_PATH.read_text(encoding="utf-8")
    jd_parsed = (
        _load_optional_json(role_content_path(out_dir, "jd_parsed.json"))
        or _load_optional_json(out_dir / "jd_parsed.json")
        or {}
    )
    resume_content = _load_optional_json(role_content_path(out_dir, "resume_content.json")) or _load_optional_json(
        out_dir / "resume_content.json"
    )
    research_cache = _load_optional_json(PROJECT_ROOT / "output" / meta["company"] / "research_cache.json") or {}
    role_research = _load_optional_json(role_content_path(out_dir, "role_research_cache.json")) or _load_optional_json(
        out_dir / "role_research_cache.json"
    )
    if role_research and "role_context" in role_research:
        research_cache = {**research_cache, "role_context": role_research["role_context"]}

    linked_resource_payload = answers_payload.get("linked_resources") if isinstance(answers_payload, dict) else None
    preference_research_payload = (
        answers_payload.get("preference_research") if isinstance(answers_payload, dict) else None
    )
    verifier_source_bundle = _build_answer_verification_source_bundle(
        out_dir=out_dir,
        application_profile_text=application_profile_text,
        master_resume_text=master_resume_text,
        work_stories_text=work_stories_text,
        candidate_context_text=candidate_context_text,
        jd_parsed=jd_parsed,
        resume_content=resume_content,
        research_cache=research_cache,
        linked_resource_payload=linked_resource_payload or {},
        preference_research_payload=preference_research_payload
        if isinstance(preference_research_payload, dict)
        else None,
    )
    classified_shared_answers = _classified_shared_answers(
        question_specs,
        application_profile,
        master_resume_text=master_resume_text,
        work_stories_text=work_stories_text,
    )
    linked_resource_deterministic = _linked_resource_deterministic_answers(linked_resource_payload or {})
    preference_research_deterministic = _preference_research_answers(preference_research_payload or {})
    preference_research_fields = {
        str(spec.get("field_name") or "").strip()
        for spec in question_specs
        if str(spec.get("research_mode") or "").strip() == "preference_ranking"
    }
    optional_skipped_field_names = {
        str(spec.get("field_name") or "").strip()
        for spec in question_specs
        if should_skip_optional_generated_answer(spec)
    }
    deterministic_field_names = set(linked_resource_deterministic)
    deterministic_field_names |= set(preference_research_deterministic)
    deterministic_field_names |= {
        str(spec.get("field_name") or "").strip()
        for spec in question_specs
        if should_skip_optional_generated_answer(spec)
    }
    deterministic_field_names |= set(classified_shared_answers)
    current_answer_provider = str((answers_payload or {}).get("provider") or "").strip() or None

    def _run_verification(current_answers: dict[str, str | list[str]]) -> dict:
        return verify_generated_answers(
            out_dir=out_dir,
            meta=meta,
            question_specs=question_specs,
            answers=current_answers,
            application_profile=application_profile,
            deterministic_field_names=deterministic_field_names,
            answer_provider=current_answer_provider,
            source_bundle=verifier_source_bundle,
        )

    verification = _run_verification(generated_answers)
    blockers = list(verification.get("blockers") or [])
    if blockers:
        raise GeneratedAnswerBlockersError(blockers, valid_answers=generated_answers)
    retry_feedback_by_field = _verification_retry_feedback_by_field(verification)
    if not retry_feedback_by_field:
        return verification

    provider_question_specs = [
        spec
        for spec in question_specs
        if str(spec.get("field_name") or "").strip() not in linked_resource_deterministic
        and str(spec.get("field_name") or "").strip() not in classified_shared_answers
        and str(spec.get("field_name") or "").strip() not in preference_research_fields
        and str(spec.get("field_name") or "").strip() not in optional_skipped_field_names
    ]
    if not provider_question_specs:
        raise GeneratedAnswerBlockersError(
            _verifier_retry_feedback_blockers(
                question_specs=question_specs,
                answers=generated_answers,
                verification=verification,
            ),
            valid_answers=generated_answers,
        )

    base_prompt = _build_application_answers_prompt(
        provider=current_answer_provider or _default_answer_provider(),
        meta=meta,
        question_specs=provider_question_specs,
        jd_parsed=jd_parsed,
        resume_content=resume_content,
        research_cache=research_cache,
        cover_letter_text=_find_cover_letter_text(out_dir),
        master_resume_text=master_resume_text,
        work_stories_text=work_stories_text,
        candidate_context_text=candidate_context_text,
        application_profile_text=application_profile_text,
        linked_resource_context=str((linked_resource_payload or {}).get("prompt_context") or "").strip() or None,
    )
    retry_prompt = _augment_answer_generation_prompt_with_verifier_feedback(
        base_prompt,
        question_specs=provider_question_specs,
        retry_feedback_by_field=retry_feedback_by_field,
    )
    raw_output_path = role_submit_path(out_dir, APPLICATION_ANSWER_RAW)
    timeout_seconds = provider_timeout_seconds()
    request_id = current_answer_refresh_request_id(out_dir)
    retry_provider = current_answer_provider or _default_answer_provider()

    def _run_retry_provider(
        provider_name: str,
        output_path: Path,
    ) -> tuple[dict[str, str] | None, Exception | None]:
        return _run_answer_generation_provider(
            provider=provider_name,
            prompt=retry_prompt,
            raw_output_path=output_path,
            timeout_seconds=timeout_seconds,
            question_specs=provider_question_specs,
            request_id=request_id,
            command_builder=provider_command_for_mode,
        )

    retry_raw_answers, error = _run_retry_provider(retry_provider, raw_output_path)
    final_provider = retry_provider
    if error is not None:
        from application_submit_common import _answer_generation_fallback_provider

        fallback_provider = _answer_generation_fallback_provider(retry_provider)
        if fallback_provider:
            fallback_raw_path = role_submit_path(out_dir, APPLICATION_ANSWER_FALLBACK_RAW)
            retry_raw_answers, fallback_error = _run_retry_provider(fallback_provider, fallback_raw_path)
            if fallback_error is None:
                final_provider = fallback_provider
                error = None
            else:
                raise RuntimeError(
                    f"{error} Fallback answer generation via {fallback_provider} also failed. "
                    f"See {fallback_raw_path} for details."
                ) from fallback_error
    if error is not None:
        raise error

    retry_merged_answers = {
        **(retry_raw_answers or {}),
        **linked_resource_deterministic,
        **preference_research_deterministic,
        **classified_shared_answers,
    }
    retry_answers, blockers = validate_generated_answers_with_blockers(
        question_specs,
        retry_merged_answers,
        application_profile=application_profile,
    )
    retry_answers = apply_draft_overrides(question_specs, retry_answers, out_dir=out_dir)
    retry_answers, blockers = validate_generated_answers_with_blockers(
        question_specs,
        retry_answers,
        application_profile=application_profile,
    )
    if blockers:
        raise GeneratedAnswerBlockersError(blockers, valid_answers=retry_answers)

    generated_answers.clear()
    generated_answers.update(retry_answers)
    current_answer_provider = final_provider

    answers_payload["generated_at_utc"] = datetime.now(UTC).replace(microsecond=0).isoformat()
    answers_payload["provider"] = final_provider
    answers_payload["questions"] = question_specs
    answers_payload["answers"] = retry_answers
    linked_resources = _linked_resource_answer_payload(linked_resource_payload)
    if linked_resources is not None:
        answers_payload["linked_resources"] = linked_resources
    else:
        answers_payload.pop("linked_resources", None)
    preference_research = _preference_research_answer_payload(preference_research_payload)
    if preference_research is not None:
        answers_payload["preference_research"] = preference_research
    else:
        answers_payload.pop("preference_research", None)
    role_submit_path(out_dir, APPLICATION_ANSWER_CACHE).write_text(
        _json_dumps_pretty(answers_payload) + "\n",
        encoding="utf-8",
    )

    verification = _run_verification(generated_answers)
    blockers = list(verification.get("blockers") or [])
    if blockers:
        raise GeneratedAnswerBlockersError(blockers, valid_answers=generated_answers)
    final_retry_feedback_by_field = _verification_retry_feedback_by_field(verification)
    if final_retry_feedback_by_field:
        fallback_overrides = build_verifier_retry_fallback_answers(
            question_specs=question_specs,
            answers=generated_answers,
            retry_feedback_by_field=final_retry_feedback_by_field,
            jd_parsed=jd_parsed,
            research_cache=research_cache,
            master_resume_text=master_resume_text,
        )
        optional_blank_overrides = build_optional_retry_blank_fallback_answers(
            question_specs=question_specs,
            answers=generated_answers,
            retry_feedback_by_field=final_retry_feedback_by_field,
        )
        combined_fallback_overrides: dict[str, object] = {
            **optional_blank_overrides,
            **fallback_overrides,
        }
        if combined_fallback_overrides:
            retry_answers, blockers = validate_generated_answers_with_blockers(
                question_specs,
                {**generated_answers, **combined_fallback_overrides},
                application_profile=application_profile,
            )
            retry_answers = apply_draft_overrides(question_specs, retry_answers, out_dir=out_dir)
            retry_answers, blockers = validate_generated_answers_with_blockers(
                question_specs,
                retry_answers,
                application_profile=application_profile,
            )
            if blockers:
                raise GeneratedAnswerBlockersError(blockers, valid_answers=retry_answers)
            generated_answers.clear()
            generated_answers.update(retry_answers)
            verification = _run_verification(generated_answers)
            blockers = list(verification.get("blockers") or [])
            if blockers:
                raise GeneratedAnswerBlockersError(blockers, valid_answers=generated_answers)
            final_retry_feedback_by_field = _verification_retry_feedback_by_field(verification)
            if not final_retry_feedback_by_field:
                answers_payload["generated_at_utc"] = datetime.now(UTC).replace(microsecond=0).isoformat()
                answers_payload["provider"] = current_answer_provider
                answers_payload["questions"] = question_specs
                answers_payload["answers"] = generated_answers
                linked_resources = _linked_resource_answer_payload(linked_resource_payload)
                if linked_resources is not None:
                    answers_payload["linked_resources"] = linked_resources
                else:
                    answers_payload.pop("linked_resources", None)
                preference_research = _preference_research_answer_payload(preference_research_payload)
                if preference_research is not None:
                    answers_payload["preference_research"] = preference_research
                else:
                    answers_payload.pop("preference_research", None)
                role_submit_path(out_dir, APPLICATION_ANSWER_CACHE).write_text(
                    _json_dumps_pretty(answers_payload) + "\n",
                    encoding="utf-8",
                )
                return verification
        raise GeneratedAnswerBlockersError(
            _verifier_retry_feedback_blockers(
                question_specs=question_specs,
                answers=generated_answers,
                verification=verification,
            ),
            valid_answers=generated_answers,
        )
    return verification


def _validate_required_questions(job_post: dict, steps: list[dict]) -> None:
    required_field_names = set()
    for question in _all_questions(job_post):
        if not question["required"]:
            continue
        for field in question["fields"]:
            if field["name"] == "security_code":
                continue
            if _is_classic_demographic_field_name(str(field["name"])):
                continue
            if field["type"] == "textarea" and field["name"] in {"resume_text", "cover_letter_text"}:
                continue
            # multi_value_multi_select / checkbox fields are handled at runtime
            # by fill_discovered_profile_questions / sync_runtime_confirmations,
            # not via pre-built payload steps.
            if field["type"] == "multi_value_multi_select":
                continue
            # File upload fields (resume, cover_letter) are optional in the
            # payload — when the generated assets don't exist yet the step is
            # intentionally omitted and the file is uploaded at Playwright
            # runtime instead.
            if field["name"] in {"resume", "cover_letter"}:
                continue
            required_field_names.add(field["name"])

    provided = {step["field_name"] for step in steps}

    missing = sorted(name for name in required_field_names if name not in provided)
    if missing:
        raise ValueError(f"Autofill payload is missing required Greenhouse fields: {', '.join(missing)}")


def _missing_required_greenhouse_fields(exc: Exception) -> list[str]:
    prefix = "Autofill payload is missing required Greenhouse fields:"
    message = str(exc).strip()
    if not message.startswith(prefix):
        return []
    return [field.strip() for field in message.split(":", 1)[1].split(",") if field.strip()]


def _write_payload_build_failure_result(out_dir: Path, exc: Exception) -> Path:
    try:
        meta = _load_meta(out_dir)
    except Exception:
        meta = {}

    job_source_url = str(meta.get("jd_source") or "").strip()
    resolved_job_url = _resolved_greenhouse_source_url(meta)
    job_url = job_source_url
    if resolved_job_url:
        try:
            job_url = _greenhouse_application_url(resolved_job_url, company_hint=str(meta.get("company") or ""))
        except Exception:
            job_url = resolved_job_url

    payload = {
        "job_url": job_url,
        "job_source_url": job_source_url,
        "out_dir": str(out_dir),
        "job_title": str(meta.get("jd_title") or ""),
        "company": str(meta.get("company_proper") or meta.get("company") or ""),
        "artifacts": {
            "report_markdown": str(role_submit_path(out_dir, AUTOFILL_REPORT_MD)),
            "report_json": str(role_submit_path(out_dir, AUTOFILL_REPORT_JSON)),
            "pre_submit_screenshot": str(role_submit_path(out_dir, AUTOFILL_PRE_SUBMIT_SCREENSHOT)),
            "page_screenshots_dir": str(role_submit_path(out_dir, AUTOFILL_PAGE_SCREENSHOTS_DIR)),
            "unknown_questions_json": str(role_submit_path(out_dir, AUTOFILL_UNKNOWN_QUESTIONS_JSON)),
            "submit_debug_html": str(role_submit_path(out_dir, AUTOFILL_SUBMIT_DEBUG_HTML)),
            "submit_debug_screenshot": str(role_submit_path(out_dir, AUTOFILL_SUBMIT_DEBUG_SCREENSHOT)),
            "submission_result_json": str(role_submit_path(out_dir, SUBMISSION_RESULT_JSON)),
        },
    }
    missing_fields = _missing_required_greenhouse_fields(exc)
    return write_greenhouse_failed_result(
        out_dir,
        payload,
        failure_type=GREENHOUSE_RUNTIME_FAILURE,
        message=str(exc).strip() or exc.__class__.__name__,
        current_page="build_payload",
        validation_errors=missing_fields or None,
    )


def _build_payload(out_dir: Path) -> dict:
    migrate_role_output_layout(out_dir)
    meta = _load_meta(out_dir)
    profile = _parse_master_resume(MASTER_RESUME_PATH.read_text(encoding="utf-8"))
    application_profile = _parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    application_source_url = _resolved_greenhouse_source_url(meta)
    if not application_source_url:
        raise ValueError("Greenhouse submit metadata is missing jd_source / jd_source_resolved / board_url.")
    application_url = _greenhouse_application_url(application_source_url, company_hint=meta.get("company"))
    html_cache_path = role_submit_path(out_dir, "greenhouse_application_page.html")
    fallback_html_cache_paths = sorted(
        (path for path in out_dir.glob("submit*/greenhouse_application_page.html") if path != html_cache_path),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    try:
        html = _fetch_greenhouse_html(
            application_url,
            cache_path=html_cache_path,
            fallback_cache_paths=fallback_html_cache_paths,
        )
    except RuntimeError as exc:
        if "job_closed" in str(exc).lower():
            unavailable_path = write_job_unavailable_artifact(
                out_dir,
                job_unavailable_filename=JOB_UNAVAILABLE_JSON,
                application_url=application_url,
                source_url=meta.get("jd_source"),
                message=str(exc),
            )
            raise RuntimeError(f"{exc} Evidence: {unavailable_path}") from exc
        raise
    job_post = _extract_job_post(html)
    job_post["_raw_html"] = html
    pending_user_input = _pending_user_input_questions(job_post, application_profile)
    if pending_user_input:
        pending_path = write_pending_user_input(
            out_dir,
            board="greenhouse",
            questions=pending_user_input,
        )
        labels = ", ".join(question["label"] for question in pending_user_input)
        raise ValueError(
            f"Greenhouse submit requires explicit user input before submission for: {labels}. See {pending_path}"
        )
    clear_pending_user_input(out_dir)
    try:
        steps = _build_steps(job_post, meta, profile, application_profile, out_dir)
    except GeneratedAnswerBlockersError as exc:
        pending_artifacts = _generated_answer_blocker_artifacts(out_dir, exc.blockers)
        pending_path = write_pending_user_input_for_unconfirmed_fields(
            out_dir,
            board="greenhouse",
            fields=exc.blockers,
            artifacts=pending_artifacts or None,
        )
        labels = ", ".join(
            str(blocker.get("label") or blocker.get("field_name") or "").strip() for blocker in exc.blockers
        )
        raise ValueError(
            f"Greenhouse submit requires review for generated-answer regressions: {labels}. See {pending_path}"
        ) from exc
    try:
        _validate_required_questions(job_post, steps)
    except ValueError as exc:
        if _missing_required_greenhouse_fields(exc):
            _write_payload_build_failure_result(out_dir, exc)
        raise

    return {
        "job_url": application_url,
        "job_source_url": meta["jd_source"],
        "out_dir": str(out_dir),
        "job_title": meta["jd_title"],
        "company": meta["company_proper"],
        "candidate_email": profile.email,
        "verification_code_email": application_profile.verification_code_email or profile.email,
        "mode": "review-before-submit",
        "notes": [
            "Application-profile defaults come from application_profile.md.",
            "Open-ended answers are generated from the tailored assets, research cache, and candidate context.",
            "Pronouns stay blank unless application_profile.md explicitly sets them.",
            "Runtime navigation supports multi-page applications and will save one screenshot per page before moving forward.",
            "If a Greenhouse email verification code appears, the runtime fetches it via googleworkspace/cli (`gws`) instead of relying on browser-only flows.",
        ],
        "artifacts": {
            "payload_path": str(role_submit_path(out_dir, "greenhouse_autofill_payload.json")),
            "report_markdown": str(role_submit_path(out_dir, AUTOFILL_REPORT_MD)),
            "report_json": str(role_submit_path(out_dir, AUTOFILL_REPORT_JSON)),
            "pre_submit_screenshot": str(role_submit_path(out_dir, AUTOFILL_PRE_SUBMIT_SCREENSHOT)),
            "page_screenshots_dir": str(role_submit_path(out_dir, AUTOFILL_PAGE_SCREENSHOTS_DIR)),
            "unknown_questions_json": str(role_submit_path(out_dir, AUTOFILL_UNKNOWN_QUESTIONS_JSON)),
            "submit_debug_html": str(role_submit_path(out_dir, AUTOFILL_SUBMIT_DEBUG_HTML)),
            "submit_debug_screenshot": str(role_submit_path(out_dir, AUTOFILL_SUBMIT_DEBUG_SCREENSHOT)),
            "submission_result_json": str(role_submit_path(out_dir, SUBMISSION_RESULT_JSON)),
            "application_page_html": str(html_cache_path),
            "application_profile": str(APPLICATION_PROFILE_PATH),
            "website_confirmation_json": str(role_submit_path(out_dir, WEBSITE_CONFIRMATION_JSON)),
            "email_confirmation_json": str(role_submit_path(out_dir, EMAIL_CONFIRMATION_JSON)),
            "notion_sync_status_json": str(role_submit_path(out_dir, NOTION_SYNC_STATUS_JSON)),
        },
        "steps": [
            step
            for step in steps
            if step.get("value") or step.get("search") or step.get("option") or step["kind"] == "file"
        ],
    }


def _step_report_value(step: dict) -> str:
    if step.get("report_value"):
        return str(step["report_value"])
    if step.get("field_name") == "security_code" and step.get("value"):
        return _redact_security_code(str(step["value"]))
    if step["kind"] == "file":
        return step["file_path"]
    return str(step.get("value") or step.get("option") or step.get("search") or "")


def _step_report_note(step: dict) -> str | None:
    if step.get("field_name") == "race":
        return (
            "If Greenhouse renders race as 'Are you Hispanic/Latino?', the browser flow selects 'Yes' for this profile."
        )
    if step.get("field_name") == "security_code":
        return "Fetched from Gmail via googleworkspace/cli (`gws`) and redacted in the report."
    if step["kind"] == "file":
        if "cover letter" in str(step.get("label", "")).casefold():
            return "Uploads the existing cover letter asset from the output directory."
        return "Uploads the existing resume asset from the output directory."
    if step.get("note"):
        return str(step["note"])
    return None


_VISIBLE_SELF_ID_PROFILE_FIELDS = {
    "gender": "gender",
    "gender_identity": "gender_identity",
    "transgender_status": "transgender_status",
    "race": "race_or_ethnicity",
    "hispanic_ethnicity": "race_or_ethnicity",
    "age_group": "age_range",
    "pronouns": "pronouns",
    "sexual_orientation": "sexual_orientation",
    "veteran_status": "veteran_status",
    "disability_status": "disability_status",
    "communities": "communities",
}


def _mark_visible_self_id_greenhouse_step(step: dict | None) -> dict | None:
    if step is None:
        return None
    profile_field = str(step.get("profile_field") or "").strip() or _VISIBLE_SELF_ID_PROFILE_FIELDS.get(
        str(step.get("field_name") or "")
    )
    if profile_field is None:
        return step
    return mark_visible_self_id_step(step, profile_field=profile_field)


def _greenhouse_profile_option_label(field: dict, desired: str, *, profile_field: str) -> str:
    labels = [
        str(option.get("label") or "").strip()
        for option in field.get("values", [])
        if str(option.get("label") or "").strip()
    ]
    fuzzy_matches: list[tuple[str, int]] = []
    for label in labels:
        normalized = _normalize_free_text(label)
        if _greenhouse_option_text_matches(profile_field, desired, label):
            fuzzy_matches.append((label, len(normalized)))
    if fuzzy_matches:
        fuzzy_matches.sort(key=lambda item: item[1])
        return fuzzy_matches[0][0]
    return _match_option_label(field, desired)


def _report_entry(step: dict) -> dict[str, object]:
    entry = {
        "field_name": step["field_name"],
        "label": step["label"],
        "kind": step["kind"],
        "source": step.get("source"),
        "optional": step.get("optional", False),
        "value": _step_report_value(step),
        "note": _step_report_note(step),
        "status": step.get("status") or ("filled" if step.get("filled") or step.get("page_index") else "planned"),
    }
    if step.get("page_index"):
        entry["page_index"] = step["page_index"]
    if step.get("blocks_draft_completion"):
        entry["blocks_draft_completion"] = True
    blocker_kind = str(step.get("blocker_kind") or "").strip()
    if blocker_kind:
        entry["blocker_kind"] = blocker_kind
    profile_field = str(step.get("profile_field") or "").strip()
    if profile_field:
        entry["profile_field"] = profile_field
    if "observed_value" in step:
        entry["observed_value"] = str(step.get("observed_value") or "").strip()
    return entry


def _entry_observed_value(entry: dict[str, object]) -> str:
    if "observed_value" in entry:
        return str(entry.get("observed_value") or "").strip()
    return str(entry.get("value") or "").strip()


def _entry_confirmation_blocker_kind(expected_entry: dict[str, object], observed_entry: dict[str, object]) -> str:
    return str(observed_entry.get("blocker_kind") or expected_entry.get("blocker_kind") or "").strip().casefold()


def _apply_confirmation_blocker_metadata(
    blocker: dict[str, object],
    expected_entry: dict[str, object],
    observed_entry: dict[str, object],
) -> dict[str, object]:
    if expected_entry.get("blocks_draft_completion") or observed_entry.get("blocks_draft_completion"):
        blocker["blocks_draft_completion"] = True
    blocker_kind = str(observed_entry.get("blocker_kind") or expected_entry.get("blocker_kind") or "").strip()
    if blocker_kind:
        blocker["blocker_kind"] = blocker_kind
    profile_field = str(observed_entry.get("profile_field") or expected_entry.get("profile_field") or "").strip()
    if profile_field:
        blocker["profile_field"] = profile_field
    return blocker


def _validation_blocker_note(message: str | None) -> str:
    normalized = str(message or "").strip() or "This field is still invalid on the live page."
    return f"Visible validation error on page: {normalized}"


def _ensure_checkbox_checked(page, checkbox, *, settle_ms: int = 120) -> bool:
    def _checked() -> bool:
        try:
            return bool(checkbox.is_checked())
        except PlaywrightError:
            return False

    if _checked():
        return True

    try:
        checkbox.scroll_into_view_if_needed()
    except PlaywrightError:
        pass

    for action in (
        lambda: checkbox.check(force=True),
        lambda: checkbox.click(force=True),
    ):
        try:
            action()
        except PlaywrightError:
            continue
        page.wait_for_timeout(settle_ms)
        if _checked():
            return True

    try:
        checkbox_id = str(checkbox.get_attribute("id") or "").strip()
    except PlaywrightError:
        checkbox_id = ""
    if checkbox_id:
        try:
            label = page.locator(f'label[for="{checkbox_id}"]')
            if label.count() > 0:
                label.first.scroll_into_view_if_needed()
                label.first.click(force=True)
                page.wait_for_timeout(settle_ms)
                if _checked():
                    return True
        except PlaywrightError:
            pass

    return _checked()


def _report_entry_matches_validation_label(entry: dict[str, object], label: str | None) -> bool:
    normalized_label = _normalize_free_text(label or "")
    if not normalized_label:
        return False
    entry_labels = [
        _normalize_free_text(str(entry.get("label") or "")),
        _normalize_free_text(str(entry.get("field_name") or "")),
    ]
    return any(
        candidate and (candidate == normalized_label or candidate in normalized_label or normalized_label in candidate)
        for candidate in entry_labels
    )


def _merge_review_validation_blockers(
    report_entries: list[dict[str, object]],
    blockers: list[dict[str, object]] | None,
) -> list[dict[str, object]]:
    if not blockers:
        return report_entries

    merged_entries = [dict(entry) for entry in report_entries]
    for blocker in blockers:
        label = str(blocker.get("label") or "").strip()
        message = str(blocker.get("message") or "").strip()
        matching_entries = [entry for entry in merged_entries if _report_entry_matches_validation_label(entry, label)]
        if matching_entries:
            for entry in matching_entries:
                entry["status"] = "planned"
                entry["note"] = _validation_blocker_note(message)
            continue
        merged_entries.append(
            {
                "field_name": str(blocker.get("field_name") or label or "validation_error"),
                "label": label or "Visible validation error",
                "kind": "validation",
                "source": "greenhouse_review_validation",
                "optional": False,
                "value": "",
                "note": _validation_blocker_note(message),
                "status": "planned",
                "blocks_draft_completion": True,
            }
        )
    return merged_entries


def _report_entry_matches_expected_value(
    expected_entry: dict[str, object],
    observed_entry: dict[str, object],
) -> bool:
    field_name = str(observed_entry.get("field_name") or expected_entry.get("field_name") or "").strip()
    expected_value = str(expected_entry.get("value") or "").strip()
    observed_value = _entry_observed_value(observed_entry)
    if not expected_value or not observed_value:
        return False

    blocker_kind = _entry_confirmation_blocker_kind(expected_entry, observed_entry)
    if blocker_kind == "visible_self_id":
        return _greenhouse_combobox_value_matches_expected(field_name, expected_value, observed_value)
    return _greenhouse_text_value_matches_expected(field_name, expected_value, observed_value)


def _live_value_mismatch_blocker(
    expected_entry: dict[str, object],
    observed_entry: dict[str, object],
) -> dict[str, object]:
    expected_value = str(expected_entry.get("value") or "").strip()
    observed_value = _entry_observed_value(observed_entry)
    blocker = dict(expected_entry)
    blocker["status"] = "planned"
    blocker["value"] = expected_value
    blocker["reason"] = (
        "Autofill could not confirm the planned profile-backed value on the live application form because the "
        "visible value did not match the expected answer."
    )
    blocker["note"] = f"Live form showed {observed_value!r} instead of expected {expected_value!r}."
    if observed_entry.get("page_index") is not None:
        blocker["page_index"] = observed_entry["page_index"]
    return _apply_confirmation_blocker_metadata(blocker, expected_entry, observed_entry)


def _live_value_missing_blocker(
    expected_entry: dict[str, object],
    observed_entry: dict[str, object],
) -> dict[str, object]:
    expected_value = str(expected_entry.get("value") or "").strip()
    blocker = dict(expected_entry)
    blocker["status"] = "planned"
    blocker["value"] = expected_value
    blocker["reason"] = (
        "Autofill could not confirm the planned profile-backed value on the live application form because the "
        "visible field still showed no selected value."
    )
    blocker["note"] = f"Live form showed no selected value instead of expected {expected_value!r}."
    if observed_entry.get("page_index") is not None:
        blocker["page_index"] = observed_entry["page_index"]
    return _apply_confirmation_blocker_metadata(blocker, expected_entry, observed_entry)


def _reconcile_runtime_confirmation_entries(report_entries: list[dict[str, object]]) -> list[dict[str, object]]:
    def _group_key(entry: dict[str, object]) -> str:
        field_key = str(entry.get("field_name") or entry.get("label") or "").strip()
        if not field_key:
            return ""
        if str(entry.get("kind") or "").strip().casefold() == "checkbox":
            value_key = str(entry.get("value") or "").strip()
            if value_key:
                return f"{field_key}::{value_key}"
        return field_key

    grouped_entries: dict[str, list[dict[str, object]]] = {}
    ordered_keys: list[str] = []
    for entry in report_entries:
        field_key = _group_key(entry)
        if not field_key:
            field_key = f"__anonymous__:{len(ordered_keys)}"
        if field_key not in grouped_entries:
            ordered_keys.append(field_key)
        grouped_entries.setdefault(field_key, []).append(entry)

    reconciled: list[dict[str, object]] = []
    for field_key in ordered_keys:
        group = grouped_entries[field_key]
        if len(group) == 1:
            reconciled.append(group[0])
            continue

        expected_entry = group[0]
        missing_entry = next(
            (
                entry
                for entry in group[1:]
                if _entry_confirmation_blocker_kind(expected_entry, entry)
                in {"visible_self_id", "visible_profile_field"}
                and not _entry_observed_value(entry)
            ),
            None,
        )
        if missing_entry is not None:
            reconciled.append(_live_value_missing_blocker(expected_entry, missing_entry))
            continue

        mismatched_entry = next(
            (
                entry
                for entry in group[1:]
                if str(entry.get("status") or "").strip().casefold() == "filled"
                and not _report_entry_matches_expected_value(expected_entry, entry)
            ),
            None,
        )
        if mismatched_entry is not None:
            reconciled.append(_live_value_mismatch_blocker(expected_entry, mismatched_entry))
            continue

        preferred_entry = next(
            (entry for entry in reversed(group) if str(entry.get("status") or "").strip().casefold() == "filled"),
            group[-1],
        )
        reconciled.append(preferred_entry)
    return reconciled


def _review_validation_blockers_from_snapshot(snapshot: dict[str, object]) -> list[dict[str, str]]:
    checked_fields = [
        _normalize_free_text(str(field))
        for field in snapshot.get("checked_fields", []) or []
        if isinstance(field, str) and field.strip()
    ]
    checked_checkbox_groups = {
        _normalize_free_text(str(group))
        for group in snapshot.get("checked_checkbox_groups", []) or []
        if isinstance(group, str) and str(group).strip()
    }
    invalid_field_groups: dict[str, str] = {}
    invalid_group_fields: dict[str, set[str]] = {}
    invalid_group_labels: dict[str, str] = {}
    for item in snapshot.get("invalid_field_groups", []) or []:
        if not isinstance(item, dict):
            continue
        field = _normalize_free_text(str(item.get("field") or ""))
        group_label = str(item.get("group") or "").strip()
        group = _normalize_free_text(group_label)
        if field and group:
            invalid_field_groups[field] = group
            invalid_group_fields.setdefault(group, set()).add(field)
            invalid_group_labels.setdefault(group, group_label)

    def _matches_checked_field(label: str) -> bool:
        normalized_label = _normalize_free_text(label)
        if not normalized_label:
            return False
        return any(
            candidate
            and (candidate == normalized_label or candidate in normalized_label or normalized_label in candidate)
            for candidate in checked_fields
        )

    satisfied_checkbox_groups = set(checked_checkbox_groups)
    for group, fields in invalid_group_fields.items():
        if any(_matches_checked_field(field) for field in fields):
            satisfied_checkbox_groups.add(group)

    invalid_fields: list[str] = []
    seen_invalid_labels: set[str] = set()
    emitted_blockers: set[str] = set()
    for field in snapshot.get("invalid_fields", []) or []:
        if not isinstance(field, str) or not field.strip():
            continue
        label = str(field).strip()
        normalized_label = _normalize_free_text(label)
        if not normalized_label or normalized_label in seen_invalid_labels:
            continue
        seen_invalid_labels.add(normalized_label)
        if _matches_checked_field(label):
            continue
        group = invalid_field_groups.get(normalized_label, "")
        if group:
            if group in satisfied_checkbox_groups:
                continue
            label = invalid_group_labels.get(group, label)
            normalized_label = _normalize_free_text(label)
            if not normalized_label:
                continue
        if normalized_label in emitted_blockers:
            continue
        emitted_blockers.add(normalized_label)
        invalid_fields.append(label)
    error_messages = [
        str(error).strip() for error in snapshot.get("errors", []) or [] if isinstance(error, str) and error.strip()
    ]
    message = next((value for value in error_messages if value), "This field is required.")
    return [{"label": label, "message": message} for label in invalid_fields]


def _write_autofill_report(payload: dict, runtime: dict | None = None) -> dict:
    steps = list(payload["steps"])
    page_screenshots: list[str] = []
    if runtime:
        steps.extend(runtime.get("extra_report_steps", []))
        page_screenshots = [page["screenshot"] for page in runtime.get("pages", []) if page.get("screenshot")]
        page_screenshots = list(dict.fromkeys(page_screenshots))
    page_screenshots = dedupe_page_screenshot_artifacts(
        page_screenshots,
        pre_submit_screenshot=str(payload.get("artifacts", {}).get("pre_submit_screenshot") or "").strip() or None,
        review_screenshot=str(payload.get("artifacts", {}).get("review_screenshot") or "").strip() or None,
    )
    report_entries = [_report_entry(step) for step in steps]
    if runtime:
        report_entries = _merge_review_validation_blockers(
            report_entries,
            list(runtime.get("review_validation_blockers") or []),
        )
    report_entries = _reconcile_runtime_confirmation_entries(report_entries)
    filled_entries = [entry for entry in report_entries if entry["status"] == "filled"]
    planned_entries = [entry for entry in report_entries if entry["status"] != "filled"]
    artifacts = payload.get("artifacts", {})
    markdown_path = Path(artifacts["report_markdown"])
    json_path = Path(artifacts["report_json"])

    report_payload = {
        "job_title": payload["job_title"],
        "company": payload["company"],
        "job_url": payload["job_url"],
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "pre_submit_screenshot": artifacts.get("pre_submit_screenshot"),
        "page_screenshots": page_screenshots,
        "unknown_questions_path": artifacts.get("unknown_questions_json"),
        "fields": filled_entries,
    }
    if planned_entries:
        report_payload["planned_but_unconfirmed_fields"] = planned_entries
    if runtime and runtime.get("unknown_questions"):
        report_payload["unknown_questions"] = runtime["unknown_questions"]
    json_path.write_text(_json_dumps_pretty(report_payload) + "\n", encoding="utf-8")

    lines = [
        "# Greenhouse Autofill Report",
        "",
        f"- Company: {payload['company']}",
        f"- Job Title: {payload['job_title']}",
        f"- Job URL: {payload['job_url']}",
        f"- Generated At (UTC): {report_payload['generated_at_utc']}",
        f"- Pre-Submit Screenshot: {artifacts.get('pre_submit_screenshot')}",
        f"- Page Screenshots Directory: {artifacts.get('page_screenshots_dir')}",
        "",
        "## Filled Fields",
        "",
    ]

    if runtime and runtime.get("unknown_questions"):
        lines.extend(
            [
                "## Unresolved Questions",
                "",
            ]
        )
        for question in runtime["unknown_questions"]:
            lines.append(f"- {question['label']} (`{question['field_name']}`)")
        lines.append("")

    for index, entry in enumerate(filled_entries, start=1):
        lines.append(f"### {index}. {entry['label']} (`{entry['field_name']}`)")
        lines.append(f"Source: `{entry['source']}`")
        lines.append(f"Kind: `{entry['kind']}`")
        lines.append(f"Optional: `{'yes' if entry['optional'] else 'no'}`")
        if entry.get("page_index"):
            lines.append(f"Page: `{entry['page_index']}`")
        if entry.get("note"):
            lines.append(f"Note: {entry['note']}")
        lines.append("Filled With:")
        lines.append("```text")
        lines.append(str(entry["value"]))
        lines.append("```")
        lines.append("")

    if planned_entries:
        lines.extend(
            [
                "## Planned But Unconfirmed",
                "",
                "These fields were in the autofill payload, but the runtime did not confirm they were filled on the live page.",
                "",
            ]
        )
        for entry in planned_entries:
            lines.append(f"- {entry['label']} (`{entry['field_name']}`) from `{entry['source']}`")
        lines.append("")

    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return report_payload


def _run_playwright(payload_path: Path, *, headless: bool, submit: bool) -> int:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        _maybe_reexec_with_uv()
        print(
            "ERROR: Playwright is not installed in the project environment. Run `uv add playwright` first.",
            file=sys.stderr,
        )
        return 1

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    application_profile = _parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    profile = _parse_master_resume(MASTER_RESUME_PATH.read_text(encoding="utf-8"))
    out_dir = Path(payload["out_dir"])
    clear_greenhouse_failure_artifacts(out_dir, payload)
    artifacts = payload.get("artifacts", {})
    page_screenshots_dir = Path(artifacts["page_screenshots_dir"])
    page_screenshots_dir.mkdir(parents=True, exist_ok=True)
    form_selector = "#application-form, #application_form"
    runtime: dict[str, object] = {
        "pages": [],
        "extra_report_steps": [],
        "unknown_questions": [],
    }
    cdp_session = None

    def reveal_application_form(page) -> None:
        def _raise_if_job_closed() -> None:
            try:
                current_url = page.url
            except PlaywrightError:
                current_url = payload["job_url"]
            try:
                page_text = page.locator("body").inner_text(timeout=1000)
            except PlaywrightError:
                page_text = ""
            reason = greenhouse_browser_job_closed_reason(current_url, page_text)
            if not reason:
                return
            unavailable_path = write_job_unavailable_artifact(
                out_dir,
                job_unavailable_filename=JOB_UNAVAILABLE_JSON,
                application_url=current_url,
                source_url=payload.get("job_source_url"),
                message=reason,
            )
            raise RuntimeError(f"{reason} Evidence: {unavailable_path}")

        _raise_if_job_closed()
        try:
            page.wait_for_selector(form_selector, timeout=1500)
            return
        except PlaywrightError:
            pass
        _raise_if_job_closed()

        try:
            embedded_application_url = _greenhouse_embedded_application_url(page.url, page.content())
        except PlaywrightError:
            embedded_application_url = None
        if embedded_application_url:
            try:
                page.goto(embedded_application_url, wait_until="domcontentloaded")
                page.wait_for_selector(form_selector, timeout=10000)
                return
            except PlaywrightError:
                _raise_if_job_closed()
                raise

        apply_button = page.get_by_role("button", name=re.compile(r"^\s*Apply\s*$", re.I)).first
        try:
            if apply_button.is_visible(timeout=1000):
                apply_button.click()
                try:
                    page.wait_for_selector(form_selector, timeout=10000)
                except PlaywrightError:
                    _raise_if_job_closed()
                    raise
                return
        except PlaywrightError:
            pass

        _raise_if_job_closed()
        try:
            page.wait_for_selector(form_selector)
        except PlaywrightError:
            _raise_if_job_closed()
            raise

    def capture_stitched_screenshot(page, path: str | Path) -> None:
        globals()["capture_stitched_screenshot"](page, path)

    def _capture_stitched_screenshot_legacy(page, path: str | Path) -> None:
        """Legacy DOM-expansion based screenshot capture (kept for reference)."""
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        capture_metadata = page.evaluate(
            """formSelector => {
                const docScroller = document.scrollingElement || document.documentElement || document.body;
                const form = document.querySelector(formSelector);
                const viewportWidth = window.innerWidth || 0;
                const viewportHeight = window.innerHeight || 0;
                window.__jobAssetsCaptureRootSeq = window.__jobAssetsCaptureRootSeq || 0;
                const candidates = [];
                const seen = new Set();

                const addCandidate = (element, kind) => {
                  if (!element || seen.has(element)) return;
                  seen.add(element);
                  const rect = element === docScroller
                    ? { width: viewportWidth, height: viewportHeight }
                    : element.getBoundingClientRect();
                  const style = element === docScroller ? null : window.getComputedStyle(element);
                  let key = "__document__";
                  if (kind !== "document") {
                    key = element.getAttribute("data-job-assets-capture-root-key");
                    if (!key) {
                      window.__jobAssetsCaptureRootSeq += 1;
                      key = `job-assets-capture-root-${window.__jobAssetsCaptureRootSeq}`;
                      element.setAttribute("data-job-assets-capture-root-key", key);
                    }
                  }
                  candidates.push({
                    key,
                    kind,
                    contains_form: Boolean(form && (element === form || element.contains(form))),
                    scroll_height: Math.max(element.scrollHeight || 0, element.offsetHeight || 0),
                    client_height: element === docScroller
                      ? viewportHeight
                      : Math.max(element.clientHeight || 0, rect.height || 0),
                    width: element === docScroller ? viewportWidth : Math.max(rect.width || 0, 0),
                    height: element === docScroller ? viewportHeight : Math.max(rect.height || 0, 0),
                    overflow_y: style ? (style.overflowY || "") : "document",
                  });
                };

                addCandidate(docScroller, "document");
                let node = form;
                while (node && node instanceof Element) {
                  const rect = node.getBoundingClientRect();
                  const style = window.getComputedStyle(node);
                  const scrollable = (node.scrollHeight - node.clientHeight > 20)
                    || /(auto|scroll|overlay)/i.test(style.overflowY || "");
                  if (scrollable && rect.width > 0 && rect.height > 0) {
                    addCandidate(node, "ancestor");
                  }
                  node = node.parentElement;
                }

                return {
                  viewport_width: viewportWidth,
                  viewport_height: viewportHeight,
                  candidates,
                };
            }""",
            form_selector,
        )
        capture_root = _choose_capture_root(capture_metadata)
        capture_root_key = str(capture_root.get("key") or "__document__")
        page.evaluate(
            """payload => {
                const { formSelector, rootKey } = payload;
                const docScroller = document.scrollingElement || document.documentElement || document.body;
                const form = document.querySelector(formSelector);
                const root = rootKey === "__document__"
                  ? docScroller
                  : document.querySelector(`[data-job-assets-capture-root-key="${rootKey}"]`) || docScroller;

                const state = {
                  elements: [],
                  scrollY: window.scrollY || 0,
                  rootKey,
                  rootScrollTop: root && root !== docScroller && typeof root.scrollTop === "number" ? root.scrollTop : 0,
                };
                const remembered = new Map();
                let restoreSeq = 0;

                const remember = (element, styleNames) => {
                  if (!element || !(element instanceof HTMLElement)) return;
                  let restoreKey = element.getAttribute("data-job-assets-capture-restore-key");
                  let createdRestoreKey = false;
                  if (!restoreKey) {
                    restoreSeq += 1;
                    restoreKey = `job-assets-capture-restore-${Date.now()}-${restoreSeq}`;
                    element.setAttribute("data-job-assets-capture-restore-key", restoreKey);
                    createdRestoreKey = true;
                  }
                  let entry = remembered.get(restoreKey);
                  if (!entry) {
                    entry = {
                      key: restoreKey,
                      created_restore_key: createdRestoreKey,
                      styles: {},
                      scrollTop: typeof element.scrollTop === "number" ? element.scrollTop : null,
                    };
                    remembered.set(restoreKey, entry);
                    state.elements.push(entry);
                  } else if (createdRestoreKey) {
                    entry.created_restore_key = true;
                  }
                  for (const styleName of styleNames) {
                    if (!(styleName in entry.styles)) {
                      entry.styles[styleName] = element.style[styleName] || "";
                    }
                  }
                };

                const expandScrollable = element => {
                  if (!element || !(element instanceof HTMLElement)) return;
                  const style = window.getComputedStyle(element);
                  const overflowText = `${style.overflow || ""} ${style.overflowY || ""} ${style.overflowX || ""}`;
                  const scrollable = (element.scrollHeight - element.clientHeight > 20)
                    || /(auto|scroll|overlay|hidden)/i.test(overflowText);
                  if (!scrollable) return;
                  remember(element, ["overflow", "overflowY", "overflowX", "height", "maxHeight", "minHeight"]);
                  element.style.overflow = "visible";
                  element.style.overflowY = "visible";
                  element.style.overflowX = "visible";
                  element.style.height = `${Math.max(element.scrollHeight, element.clientHeight)}px`;
                  element.style.maxHeight = "none";
                  element.style.minHeight = "0";
                };

                const expanded = new Set();
                const expandChain = start => {
                  let node = start;
                  while (node && node instanceof Element) {
                    if (node === document.body || node === document.documentElement) break;
                    if (!expanded.has(node)) {
                      expanded.add(node);
                      expandScrollable(node);
                    }
                    node = node.parentElement;
                  }
                };

                if (root && root instanceof Element && root !== docScroller) {
                  expandChain(root);
                }
                expandChain(form);

                if (document.documentElement instanceof HTMLElement) {
                  remember(document.documentElement, ["overflow", "overflowY", "height", "maxHeight", "minHeight"]);
                  document.documentElement.style.overflow = "visible";
                  document.documentElement.style.overflowY = "visible";
                  document.documentElement.style.height = "auto";
                  document.documentElement.style.maxHeight = "none";
                  document.documentElement.style.minHeight = "0";
                }
                if (document.body instanceof HTMLElement) {
                  remember(document.body, ["overflow", "overflowY", "height", "maxHeight", "minHeight"]);
                  document.body.style.overflow = "visible";
                  document.body.style.overflowY = "visible";
                  document.body.style.height = "auto";
                  document.body.style.maxHeight = "none";
                  document.body.style.minHeight = "0";
                }

                const viewportWidth = window.innerWidth || 0;
                const viewportHeight = window.innerHeight || 0;
                const allElements = document.body ? Array.from(document.body.querySelectorAll("*")) : [];
                for (const element of allElements) {
                  if (!(element instanceof HTMLElement)) continue;
                  const style = window.getComputedStyle(element);
                  if (style.position !== "fixed" && style.position !== "sticky") continue;
                  const rect = element.getBoundingClientRect();
                  if (rect.top > 24) continue;
                  if (rect.bottom <= 0) continue;
                  if (rect.width < viewportWidth * 0.25) continue;
                  if (rect.height <= 0 || rect.height > viewportHeight * 0.35) continue;
                  remember(element, ["visibility", "opacity", "pointerEvents"]);
                  element.style.visibility = "hidden";
                  element.style.opacity = "0";
                  element.style.pointerEvents = "none";
                }

                window.__jobAssetsCaptureState = state;
                window.scrollTo(0, 0);
                if (root && root !== docScroller && root instanceof HTMLElement) {
                  root.scrollTop = 0;
                }
            }""",
            {"formSelector": form_selector, "rootKey": capture_root_key},
        )
        try:
            # After DOM expansion, full_page captures all visible content reliably.
            try:
                page.screenshot(path=str(output_path), type="png", full_page=True)
                return
            except Exception:
                pass

            root_selector = _capture_root_selector(capture_root_key)
            if root_selector:
                try:
                    page.locator(root_selector).first.screenshot(path=str(output_path), type="png")
                    return
                except Exception:
                    pass

            if cdp_session is not None:
                try:
                    metrics = cdp_session.send("Page.getLayoutMetrics")
                    content_size = metrics.get("contentSize") or {}
                    width = max(int(content_size.get("width") or 0), 1)
                    height = max(int(content_size.get("height") or 0), 1)
                    screenshot = cdp_session.send(
                        "Page.captureScreenshot",
                        {
                            "format": "png",
                            "fromSurface": True,
                            "captureBeyondViewport": True,
                            "clip": {
                                "x": 0,
                                "y": 0,
                                "width": width,
                                "height": height,
                                "scale": 1,
                            },
                        },
                    )
                    output_path.write_bytes(base64.b64decode(screenshot["data"]))
                    return
                except Exception:
                    pass

            from PIL import Image

            metrics = _capture_scroll_metrics(page, root_key=capture_root_key)
            total_height_css = max(int(metrics.get("scrollHeight") or 0), 1)
            viewport_height_css = max(int(metrics.get("viewportHeight") or 0), 1)
            device_pixel_ratio = max(float(metrics.get("devicePixelRatio") or 1), 1.0)
            final_height_px = max(int(round(total_height_css * device_pixel_ratio)), 1)

            stitched: Image.Image | None = None
            start_css = 0
            while start_css < total_height_css:
                end_css = min(start_css + viewport_height_css, total_height_css)
                scroll_target = max(0, end_css - viewport_height_css)
                actual_scroll_css = _set_capture_scroll_position(
                    page,
                    root_key=capture_root_key,
                    target_css=scroll_target,
                )
                page.wait_for_timeout(120)
                screenshot_bytes = page.screenshot(type="png")
                with Image.open(io.BytesIO(screenshot_bytes)) as captured:
                    image = captured.convert("RGB")
                if stitched is None:
                    stitched = Image.new("RGB", (image.width, final_height_px), "white")
                crop_top_px = max(int(round((start_css - actual_scroll_css) * device_pixel_ratio)), 0)
                crop_bottom_px = min(
                    int(round((end_css - actual_scroll_css) * device_pixel_ratio)),
                    image.height,
                )
                if crop_bottom_px > crop_top_px:
                    segment = image.crop((0, crop_top_px, image.width, crop_bottom_px))
                    paste_y_px = max(int(round(start_css * device_pixel_ratio)), 0)
                    overflow_px = (paste_y_px + segment.height) - final_height_px
                    if overflow_px > 0:
                        segment = segment.crop((0, 0, segment.width, segment.height - overflow_px))
                    if segment.height > 0:
                        stitched.paste(segment, (0, paste_y_px))
                start_css = end_css

            if stitched is None:
                raise RuntimeError(f"Could not capture screenshot for {output_path}")
            stitched.save(output_path)
        finally:
            page.evaluate(
                """() => {
                    const state = window.__jobAssetsCaptureState;
                    if (!state) return;
                    for (let index = state.elements.length - 1; index >= 0; index -= 1) {
                      const entry = state.elements[index];
                      const element = document.querySelector(
                        `[data-job-assets-capture-restore-key="${entry.key}"]`
                      );
                      if (!(element instanceof HTMLElement)) continue;
                      for (const [styleName, value] of Object.entries(entry.styles || {})) {
                        element.style[styleName] = value || "";
                      }
                      if (typeof entry.scrollTop === "number") {
                        element.scrollTop = entry.scrollTop;
                      }
                      if (entry.created_restore_key) {
                        element.removeAttribute("data-job-assets-capture-restore-key");
                      }
                    }
                    window.scrollTo(0, state.scrollY || 0);
                    for (const element of Array.from(document.querySelectorAll("[data-job-assets-capture-root-key]"))) {
                      element.removeAttribute("data-job-assets-capture-root-key");
                    }
                    delete window.__jobAssetsCaptureState;
                }"""
            )

    def resolve_locator(page, step, *, require_visible: bool = True, raise_if_missing: bool = True):
        def file_upload_key() -> str | None:
            label_text = str(step.get("label", "")).casefold()
            if "resume" in label_text or step.get("field_name") == "resume":
                return "resume"
            if "cover letter" in label_text or str(step.get("field_name") or "") in {
                "cover_letter",
                "cover_letter_text",
            }:
                return "cover_letter"
            return None

        def maybe_reveal_manual_textarea(locator) -> bool:
            if step.get("kind") not in {"text", "textarea"}:
                return False
            if step.get("field_name") not in {"cover_letter_text", "resume_text"}:
                return False
            try:
                tag_name = (locator.evaluate("node => node.tagName") or "").upper()
            except PlaywrightError:
                return False
            if tag_name != "TEXTAREA":
                return False
            try:
                if locator.is_visible():
                    return True
            except PlaywrightError:
                pass

            try:
                revealed = bool(
                    locator.evaluate(
                        """element => {
                            const isVisible = node => {
                                if (!(node instanceof HTMLElement)) return false;
                                for (let current = node; current; current = current.parentElement) {
                                    if (current.hidden) return false;
                                    if (current.getAttribute?.("aria-hidden") === "true") return false;
                                    const style = window.getComputedStyle(current);
                                    if (style.display === "none" || style.visibility === "hidden") return false;
                                }
                                const rect = node.getBoundingClientRect();
                                return rect.width > 0 || rect.height > 0;
                            };
                            if (isVisible(element)) return true;
                            const roots = [
                                element.closest(".field"),
                                element.closest("fieldset"),
                                element.parentElement,
                            ].filter(Boolean);
                            for (const root of roots) {
                                const button = root.querySelector('button[data-source="paste"], a[data-source="paste"]');
                                if (button instanceof HTMLElement) {
                                    button.click();
                                    return true;
                                }
                            }
                            return false;
                        }"""
                    )
                )
            except PlaywrightError:
                return False

            if revealed:
                page.wait_for_timeout(180)
                try:
                    return locator.is_visible()
                except PlaywrightError:
                    return False
            return False

        def attach_button_locator():
            upload_key = file_upload_key()
            if not upload_key:
                return None
            root_selectors = [
                f'[data-field="{upload_key}"]',
                f"#{upload_key}_fieldset",
                f"#{upload_key}",
            ]
            for selector in root_selectors:
                root = page.locator(selector).first
                if root.count() == 0:
                    continue
                button = root.locator('button[data-source="attach"], a[data-source="attach"]').first
                if button.count() == 0:
                    continue
                return button
            return None

        selectors: list[str] = []
        if step.get("selector"):
            selectors.append(step["selector"])
        if step.get("field_name") == "race":
            selectors.append("select#job_application_hispanic_ethnicity")
            selectors.append("#hispanic_ethnicity select")

        for selector in selectors:
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
            if step["kind"] == "file":
                try:
                    tag_name = (locator.evaluate("node => node.tagName") or "").upper()
                    input_type = (locator.get_attribute("type") or "").casefold()
                except PlaywrightError:
                    tag_name = ""
                    input_type = ""
                if tag_name == "INPUT" and input_type == "file":
                    return locator
                continue
            if step["kind"] == "combobox":
                try:
                    tag_name = (locator.evaluate("node => node.tagName") or "").upper()
                except PlaywrightError:
                    tag_name = ""
                if tag_name == "SELECT":
                    return locator
            if not require_visible:
                return locator
            try:
                if locator.is_visible():
                    return locator
            except PlaywrightError:
                pass
            if maybe_reveal_manual_textarea(locator):
                return locator
            continue

        if step["kind"] == "file" and page.locator("#application_form").count() > 0:
            file_inputs = page.locator(f"{form_selector} input[type='file']")
            label_text = str(step.get("label", "")).casefold()
            if "resume" in label_text and file_inputs.count() > 0:
                return file_inputs.nth(0)
            if "cover letter" in label_text and file_inputs.count() > 1:
                return file_inputs.nth(1)
            attach_button = attach_button_locator()
            if attach_button is not None:
                return attach_button

        labels: list[str] = []
        if step.get("label"):
            labels.append(step["label"])
        if step.get("field_name") == "race":
            labels.append("Are you Hispanic/Latino?")

        for label in labels:
            normalized_label = _normalize_free_text(label)
            exact_modes = [True]
            if len(normalized_label.split()) > 1:
                exact_modes.append(False)
            for exact in exact_modes:
                locator = page.get_by_label(label, exact=exact).first
                if locator.count() == 0:
                    continue
                if step["kind"] == "combobox":
                    try:
                        tag_name = (locator.evaluate("node => node.tagName") or "").upper()
                    except PlaywrightError:
                        tag_name = ""
                    if tag_name == "SELECT":
                        return locator
                if not require_visible:
                    return locator
                try:
                    if locator.is_visible():
                        return locator
                except PlaywrightError:
                    pass
                if maybe_reveal_manual_textarea(locator):
                    return locator
                continue

        if raise_if_missing:
            raise RuntimeError(f"Could not resolve locator for step {step}")
        return None

    def ordered_desired_values(desired: str, fallback_values: list[str] | None = None) -> list[str]:
        values: list[str] = []
        for candidate in [desired, *(fallback_values or [])]:
            normalized = str(candidate or "").strip()
            if normalized and normalized not in values:
                values.append(normalized)
        return values

    def combobox_choice(locator, step):
        search_value = step.get("search") or step.get("option") or ""
        option_text = step.get("option") or step.get("search") or ""

        try:
            field_id = locator.get_attribute("id") or ""
        except PlaywrightError:
            field_id = ""

        if field_id == "hispanic_ethnicity":
            normalized = _normalize_free_text(option_text or search_value)
            if "hispanic" in normalized or "latino" in normalized:
                return "Yes", "Yes"
            if normalized.startswith("decline"):
                return "I don't wish to answer", "I don't wish to answer"
            return "No", "No"

        return search_value, option_text

    def option_text_matches(field_name: str, desired: str, option_text: str) -> bool:
        return _greenhouse_option_text_matches(field_name, desired, option_text)

    def matching_option_text(
        field_name: str,
        desired: str,
        option_texts: list[str],
        *,
        fallback_values: list[str] | None = None,
    ) -> str | None:
        for candidate in ordered_desired_values(desired, fallback_values):
            desired_candidates = _normalized_option_match_candidates(field_name, candidate)
            if desired_candidates:
                exact_match = next(
                    (text for text in option_texts if _normalize_free_text(text) in desired_candidates),
                    None,
                )
                if exact_match:
                    return exact_match
            if _is_school_field_name(field_name):
                scored_options = [(text, _school_option_score(candidate, text)) for text in option_texts]
                ranked = [(text, score) for text, score in scored_options if score is not None]
                if ranked:
                    ranked.sort(key=lambda item: item[1], reverse=True)
                    return ranked[0][0]
            # Prefer shortest fuzzy match to avoid e.g. "United States Minor
            # Outlying Islands" beating "United States".
            fallback_matches = [text for text in option_texts if option_text_matches(field_name, candidate, text)]
            if fallback_matches:
                fallback_matches.sort(key=lambda t: len(t))
                return fallback_matches[0]
        return None

    def select_native_option(
        locator, *, field_name: str, desired: str, fallback_values: list[str] | None = None
    ) -> str:
        option_locator = locator.locator("option")
        option_texts = [option_locator.nth(i).inner_text().strip() for i in range(option_locator.count())]
        selected_option = matching_option_text(
            field_name,
            desired,
            option_texts,
            fallback_values=fallback_values,
        )
        if not selected_option:
            raise RuntimeError(f"Could not find answer {desired!r} for field {field_name!r}.")

        try:
            if locator.is_visible():
                locator.select_option(label=selected_option)
                return selected_option
        except PlaywrightError:
            pass

        selected = locator.evaluate(
            """(node, desiredLabel) => {
                const normalize = value => (value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
                const desired = normalize(desiredLabel);
                const options = Array.from(node.options || []);
                const match = options.find(option => {
                  const text = (option.textContent || "").trim();
                  return normalize(text) === desired;
                });
                if (!match) return "";
                node.value = match.value;
                for (const option of options) {
                  option.selected = option === match;
                }
                node.dispatchEvent(new Event("input", { bubbles: true }));
                node.dispatchEvent(new Event("change", { bubbles: true }));
                const select2Chosen = node.id
                  ? document.querySelector(`#s2id_${CSS.escape(node.id)} .select2-chosen`)
                  : null;
                if (select2Chosen) {
                  select2Chosen.textContent = (match.textContent || "").trim();
                }
                return (match.textContent || "").trim();
            }""",
            selected_option,
        )
        if not selected:
            raise RuntimeError(f"Could not select answer {desired!r} for field {field_name!r}.")
        return str(selected).strip() or selected_option

    def current_combobox_options(page, locator) -> list[tuple[object, str]]:
        try:
            control_id = (locator.get_attribute("id") or "").strip()
        except PlaywrightError:
            control_id = ""
        try:
            aria_controls = locator.get_attribute("aria-controls")
        except PlaywrightError:
            aria_controls = None
        try:
            aria_owns = locator.get_attribute("aria-owns")
        except PlaywrightError:
            aria_owns = None
        listbox_ids = _greenhouse_combobox_candidate_listbox_ids(control_id, aria_controls, aria_owns)

        option_selectors: list[str] = []
        if listbox_ids:
            for listbox_id in listbox_ids:
                option_selectors.extend(
                    [
                        f'[id="{listbox_id}"] .select2-result-selectable',
                        f'[id="{listbox_id}"] li[role="option"]',
                        f'[id="{listbox_id}"] .select2-result-label',
                        f'[id="{listbox_id}"] [role="option"]',
                        f'[id="{listbox_id}"] [class*="option"]',
                    ]
                )
        else:
            option_selectors = [
                ".select2-result-selectable",
                'li[role="option"]',
                ".select2-result-label",
                '[role="option"]',
                '[class*="option"]',
            ]

        options = page.locator(option_selectors[0])
        for selector in option_selectors[1:]:
            if options.count() > 0:
                break
            options = page.locator(selector)

        current_options: list[tuple[object, str]] = []
        for index in range(options.count()):
            candidate = options.nth(index)
            try:
                text = candidate.inner_text().strip()
            except PlaywrightError:
                continue
            if text and _normalize_free_text(text) != "no options":
                current_options.append((candidate, text))
        return current_options

    def wait_for_combobox_match(
        page,
        locator,
        *,
        field_name: str,
        desired: str,
        fallback_values: list[str] | None = None,
        timeout_ms: int = 2500,
        poll_ms: int = 150,
    ) -> tuple[list[tuple[object, str]], str | None]:
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        best_options: list[tuple[object, str]] = []
        best_match: str | None = None

        while True:
            option_rows = current_combobox_options(page, locator)
            option_texts = [text for _, text in option_rows]
            matched_option = matching_option_text(
                field_name,
                desired,
                option_texts,
                fallback_values=fallback_values,
            )
            if option_rows:
                best_options = option_rows
            if matched_option:
                return option_rows, matched_option
            if time.monotonic() >= deadline:
                return best_options, best_match
            page.wait_for_timeout(poll_ms)

    def read_combobox_display_value(locator) -> str:
        try:
            value = (locator.input_value() or "").strip()
        except PlaywrightError:
            value = ""
        try:
            selected_snapshot = locator.evaluate(
                """element => {
                    const text = node => (node?.textContent || "").replace(/\\s+/g, " ").trim();
                    const wrapper =
                      element.closest(".select")
                      || element.closest(".select__container")
                      || element.parentElement;
                    return {
                      placeholder:
                        text(wrapper?.querySelector(".select__placeholder, [class*='placeholder']")),
                      singleValue:
                        text(wrapper?.querySelector(".select__single-value, [class*='single-value']"))
                        || text(wrapper?.querySelector(".select2-chosen")),
                      expanded:
                        element.getAttribute?.("aria-expanded") === "true"
                        || element.closest?.("[aria-expanded='true']") !== null,
                      multiValues: Array.from(
                        wrapper?.querySelectorAll(".select__multi-value__label, [class*='multi-value__label']")
                        || []
                      ).map(text).filter(Boolean),
                    };
                }"""
            )
        except PlaywrightError:
            selected_snapshot = {}
        return _greenhouse_combobox_display_text(
            value,
            str((selected_snapshot or {}).get("singleValue") or ""),
            list((selected_snapshot or {}).get("multiValues") or []),
            str((selected_snapshot or {}).get("placeholder") or ""),
            bool((selected_snapshot or {}).get("expanded")),
        )

    def combobox_interaction_locator(page, locator):
        try:
            tag_name = (locator.evaluate("node => node.tagName") or "").upper()
            disabled = bool(locator.evaluate("node => !!node.disabled"))
            read_only = bool(locator.evaluate("node => !!node.readOnly"))
        except PlaywrightError:
            tag_name = ""
            disabled = False
            read_only = False

        if tag_name in {"INPUT", "TEXTAREA"} and not disabled and not read_only:
            return locator

        candidate_selectors = [
            ".select2-drop-active input.select2-input",
            ".select2-drop-active .select2-search input",
            "[role='combobox'][aria-expanded='true']",
        ]
        for selector in candidate_selectors:
            candidate = page.locator(selector).first
            if candidate.count() == 0:
                continue
            try:
                if candidate.is_visible():
                    return candidate
            except PlaywrightError:
                continue
        return locator

    def fill_combobox_locator(
        page,
        locator,
        *,
        field_name: str,
        search_value: str,
        option_text: str,
        fallback_values: list[str] | None = None,
    ) -> str:
        is_location_field = field_name in {"candidate_location", "job_application[location]", "location"}
        is_school_field = _is_school_field_name(field_name)

        locator.scroll_into_view_if_needed()
        try:
            locator.click(force=True, timeout=1500)
        except PlaywrightError:
            locator.evaluate(
                """(element) => {
                    element.focus();
                    if (typeof element.select === "function") {
                        element.select();
                    }
                }"""
            )
        page.wait_for_timeout(120)
        interaction_locator = combobox_interaction_locator(page, locator)

        visible_options = current_combobox_options(page, interaction_locator)
        if not visible_options:
            try:
                interaction_locator.press("ArrowDown")
                page.wait_for_timeout(120)
                visible_options = current_combobox_options(page, interaction_locator)
            except PlaywrightError:
                pass
        desired_option = (
            matching_option_text(
                field_name,
                option_text or search_value,
                [text for _, text in visible_options],
                fallback_values=fallback_values,
            )
            or option_text
        )

        if desired_option:
            # Prefer exact match over fuzzy — prevents e.g. "United States
            # Minor Outlying Islands" from winning over "United States".
            desired_norm = _normalize_free_text(desired_option)
            exact_hit = next(((c, t) for c, t in visible_options if _normalize_free_text(t) == desired_norm), None)
            ordered = (
                ([exact_hit] + [(c, t) for c, t in visible_options if (c, t) != exact_hit])
                if exact_hit
                else list(visible_options)
            )
            for candidate, text in ordered:
                if option_text_matches(field_name, desired_option, text):
                    candidate.click(force=True)
                    page.wait_for_timeout(160)
                    selected_value = read_combobox_display_value(locator)
                    confirmed_selection = _confirmed_combobox_selection_value(
                        field_name,
                        desired_option,
                        selected_value,
                    )
                    if confirmed_selection:
                        return confirmed_selection
                    if is_location_field:
                        try:
                            interaction_locator.press("ArrowDown")
                            interaction_locator.press("Enter")
                            page.wait_for_timeout(200)
                        except PlaywrightError:
                            pass
                        selected_value = read_combobox_display_value(locator)
                        confirmed_selection = _confirmed_combobox_selection_value(
                            field_name,
                            desired_option,
                            selected_value,
                        )
                        if confirmed_selection:
                            return confirmed_selection

        try:
            interaction_locator.fill("")
        except PlaywrightError:
            pass

        if search_value:
            human_fill(interaction_locator, search_value, delay_ms=35)
            visible_options, desired_option = wait_for_combobox_match(
                page,
                interaction_locator,
                field_name=field_name,
                desired=option_text or search_value,
                fallback_values=fallback_values,
            )
            if desired_option is None:
                desired_option = (
                    matching_option_text(
                        field_name,
                        option_text or search_value,
                        [text for _, text in visible_options],
                        fallback_values=fallback_values,
                    )
                    or desired_option
                )

        if desired_option:
            # Prefer exact match over fuzzy — prevents e.g. "United States
            # Minor Outlying Islands" from winning over "United States".
            desired_norm = _normalize_free_text(desired_option)
            exact_hit = next(((c, t) for c, t in visible_options if _normalize_free_text(t) == desired_norm), None)
            ordered = (
                ([exact_hit] + [(c, t) for c, t in visible_options if (c, t) != exact_hit])
                if exact_hit
                else list(visible_options)
            )
            for candidate, text in ordered:
                if option_text_matches(field_name, desired_option, text):
                    candidate.click(force=True)
                    page.wait_for_timeout(160)
                    selected_value = read_combobox_display_value(locator)
                    confirmed_selection = _confirmed_combobox_selection_value(
                        field_name,
                        desired_option,
                        selected_value,
                    )
                    if confirmed_selection:
                        return confirmed_selection
                    if is_location_field:
                        try:
                            interaction_locator.press("ArrowDown")
                            interaction_locator.press("Enter")
                            page.wait_for_timeout(200)
                        except PlaywrightError:
                            pass
                        selected_value = read_combobox_display_value(locator)
                        confirmed_selection = _confirmed_combobox_selection_value(
                            field_name,
                            desired_option,
                            selected_value,
                        )
                        if confirmed_selection:
                            return confirmed_selection

        if search_value and desired_option is None:
            visible_options = current_combobox_options(page, interaction_locator)
            desired_option = (
                matching_option_text(
                    field_name,
                    option_text or search_value,
                    [text for _, text in visible_options],
                    fallback_values=fallback_values,
                )
                or desired_option
            )

        if is_school_field:
            raise RuntimeError(
                f"Could not confirm an exact school selection for field {field_name!r}. "
                f"Desired={desired_option or search_value!r}"
            )

        interaction_locator.press("ArrowDown")
        interaction_locator.press("Enter")
        page.wait_for_timeout(160)
        selected_value = read_combobox_display_value(locator)
        confirmed_selection = _confirmed_combobox_selection_value(
            field_name,
            desired_option or search_value,
            selected_value,
        )
        if confirmed_selection:
            return confirmed_selection
        if is_location_field:
            try:
                input_value = (interaction_locator.input_value() or "").strip()
            except PlaywrightError:
                input_value = ""
            if input_value and option_text_matches(field_name, desired_option or search_value, input_value):
                return input_value
        raise RuntimeError(
            f"Could not confirm combobox selection for field {field_name!r}. "
            f"Desired={desired_option or search_value!r}; observed={selected_value!r}"
        )

    def fill_text(page, step):
        locator = resolve_locator(page, step)
        try:
            role = (locator.get_attribute("role") or "").strip().casefold()
        except PlaywrightError:
            role = ""
        try:
            aria_autocomplete = (locator.get_attribute("aria-autocomplete") or "").strip().casefold()
        except PlaywrightError:
            aria_autocomplete = ""
        if role == "combobox" or aria_autocomplete == "list":
            search_value = step["value"]
            option_text = step["value"]
            if step.get("field_name") == "candidate_location":
                search_value = _location_search_text(step["value"])
            fill_combobox_locator(
                page,
                locator,
                field_name=str(step.get("field_name") or ""),
                search_value=search_value,
                option_text=option_text,
                fallback_values=list(step.get("fallback_options") or []),
            )
            return
        # Detect checkbox/radio inputs that were misclassified as text steps
        try:
            input_type = locator.evaluate("node => (node.type || '').toLowerCase()") or ""
        except PlaywrightError:
            input_type = ""
        if input_type in ("checkbox", "radio"):
            locator.scroll_into_view_if_needed()
            if not locator.is_checked():
                locator.check()
            return
        locator.scroll_into_view_if_needed()
        human_fill(locator, step["value"])

    def manual_text_step_is_confirmed(page, step) -> bool:
        if step.get("field_name") not in {"cover_letter_text", "resume_text"}:
            return True
        locator = resolve_locator(page, step, require_visible=False, raise_if_missing=False)
        if locator is None:
            return False
        try:
            observed = (locator.input_value() or "").strip()
        except PlaywrightError:
            try:
                observed = str(locator.evaluate("node => (node.value || '').trim()")).strip()
            except PlaywrightError:
                return False
        return observed == str(step.get("value") or "").strip()

    def fill_combobox(page, step):
        locator = resolve_locator(page, step)
        try:
            tag_name = (locator.evaluate("node => node.tagName") or "").upper()
        except PlaywrightError:
            tag_name = ""
        if tag_name == "SELECT":
            select_native_option(
                locator,
                field_name=str(step.get("field_name") or ""),
                desired=str(step.get("option") or step.get("search") or step.get("value") or ""),
                fallback_values=list(step.get("fallback_options") or []),
            )
            return
        search_value, option_text = combobox_choice(locator, step)
        fill_combobox_locator(
            page,
            locator,
            field_name=str(step.get("field_name") or ""),
            search_value=search_value,
            option_text=option_text,
            fallback_values=list(step.get("fallback_options") or []),
        )

    def read_live_step_value(page, step, locator=None) -> str:
        locator = locator or resolve_locator(page, step, require_visible=False, raise_if_missing=False)
        if locator is None:
            return ""
        kind = str(step.get("kind") or "").strip()
        if kind == "combobox":
            try:
                tag_name = (locator.evaluate("node => node.tagName") or "").upper()
            except PlaywrightError:
                tag_name = ""
            if tag_name == "SELECT":
                selected_option_locator = locator.locator("option:checked").first
                if selected_option_locator.count() > 0:
                    try:
                        return selected_option_locator.inner_text().strip()
                    except PlaywrightError:
                        return ""
                return ""
            return read_combobox_display_value(locator)
        if kind in {"text", "textarea"}:
            try:
                role = (locator.get_attribute("role") or "").strip().casefold()
            except PlaywrightError:
                role = ""
            try:
                aria_autocomplete = (locator.get_attribute("aria-autocomplete") or "").strip().casefold()
            except PlaywrightError:
                aria_autocomplete = ""
            if role == "combobox" or aria_autocomplete == "list":
                return read_combobox_display_value(locator)
            try:
                return (locator.input_value() or "").strip()
            except PlaywrightError:
                try:
                    return str(locator.evaluate("node => (node.value || '').trim()")).strip()
                except PlaywrightError:
                    return ""
        return ""

    def refill_visible_deterministic_steps(page, *, page_index: int) -> None:
        for step in payload["steps"]:
            if not _should_refill_visible_deterministic_step(step):
                continue
            locator = resolve_locator(page, step, require_visible=False, raise_if_missing=False)
            if locator is None:
                continue
            try:
                if not locator.is_visible():
                    continue
            except PlaywrightError:
                continue
            live_value = read_live_step_value(page, step, locator)
            if _greenhouse_live_value_matches_step(step, live_value):
                continue
            if step.get("kind") in {"text", "textarea"}:
                fill_text(page, step)
                try:
                    locator.evaluate("element => element.blur()")
                except PlaywrightError:
                    pass
            else:
                fill_combobox(page, step)
            page.wait_for_timeout(120)
            live_value = read_live_step_value(page, step, locator)
            if _greenhouse_live_value_matches_step(step, live_value):
                step["filled"] = True
                step["page_index"] = page_index

    def upload_file(page, step):
        upload_key = _greenhouse_file_upload_key(step.get("label"))
        if upload_key:
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightError:
                pass
            attach_button = page.locator(
                f'[aria-labelledby="upload-label-{upload_key}"] button', has_text="Attach"
            ).first
            if attach_button.count():
                try:
                    with page.expect_file_chooser(timeout=2_500) as chooser_info:
                        attach_button.click(force=True)
                    chooser_info.value.set_files(step["file_path"])
                    if _wait_for_greenhouse_file_upload_confirmation(page, step):
                        return
                except PlaywrightTimeoutError:
                    pass

        locator = resolve_locator(page, step, require_visible=False, raise_if_missing=False)
        if locator is not None:
            try:
                tag_name = (locator.evaluate("node => node.tagName") or "").upper()
            except PlaywrightError:
                tag_name = ""
            if tag_name == "INPUT":
                locator.set_input_files(step["file_path"])
                _wait_for_greenhouse_file_upload_confirmation(page, step)
                return
            try:
                with page.expect_file_chooser(timeout=1500) as chooser_info:
                    locator.click(force=True)
                chooser_info.value.set_files(step["file_path"])
                if _wait_for_greenhouse_file_upload_confirmation(page, step):
                    return
            except PlaywrightTimeoutError:
                page.wait_for_timeout(180)
                locator = resolve_locator(page, step, require_visible=False, raise_if_missing=False)
                if locator is not None:
                    try:
                        tag_name = (locator.evaluate("node => node.tagName") or "").upper()
                    except PlaywrightError:
                        tag_name = ""
                    if tag_name == "INPUT":
                        locator.set_input_files(step["file_path"])
                        _wait_for_greenhouse_file_upload_confirmation(page, step)
                        return
        raise RuntimeError(f"Could not resolve a file upload control for {step.get('label')!r}.")

    def file_upload_is_confirmed(page, step: dict) -> bool:
        upload_confirm_expression = """({ uploadKey, expectedName }) => {
            const text = node => (node?.textContent || "").replace(/\\s+/g, " ").trim();
            const matchingFileInput = Array.from(document.querySelectorAll('input[type="file"]')).find(input => {
                const files = input.files ? Array.from(input.files) : [];
                return files.some(file => (file?.name || "") === expectedName);
            });
            const widget =
              (uploadKey && (
                document.querySelector(`[aria-labelledby="upload-label-${uploadKey}"]`)
                || document.querySelector(`#upload-label-${uploadKey}`)?.closest('.file-upload')
              ))
              || matchingFileInput?.closest?.('.file-upload')
              || null;
            const filename = uploadKey ? document.querySelector(`#${uploadKey}_filename`) : null;
            const chosen = uploadKey ? document.querySelector(`#${uploadKey}_chosen`) : null;
            return {
              matching_file_input: Boolean(matchingFileInput),
              widget_present: Boolean(widget),
              widget_text: text(widget),
              chosen_text: `${text(filename)} ${text(chosen)}`.trim(),
              body_text: text(document.body),
            };
        }"""
        snapshot = page.evaluate(upload_confirm_expression, _greenhouse_file_upload_confirmation_args(step))
        return _greenhouse_file_upload_confirmed(snapshot, Path(step["file_path"]).name)

    def confirm_file_upload_steps(page, *, page_index: int) -> list[str]:
        confirmed_fields: list[str] = []
        for step in payload["steps"]:
            if step.get("kind") != "file" or step.get("filled"):
                continue
            if not file_upload_is_confirmed(page, step):
                continue
            step["filled"] = True
            step["page_index"] = page_index
            confirmed_fields.append(step["field_name"])
        return confirmed_fields

    def step_is_available(page, step):
        if step.get("skip_runtime_fill"):
            return False
        locator = resolve_locator(
            page,
            step,
            require_visible=step["kind"] != "file",
            raise_if_missing=False,
        )
        return locator is not None

    def control_text(locator) -> str:
        try:
            tag_name = (locator.evaluate("node => node.tagName") or "").upper()
        except PlaywrightError:
            return ""
        if tag_name == "INPUT":
            return (locator.get_attribute("value") or locator.get_attribute("aria-label") or "").strip()
        return (locator.inner_text() or locator.get_attribute("aria-label") or "").strip()

    def visible_form_fields(page):
        return page.locator(form_selector).first.evaluate(
            """form => {
                const results = [];
                const seen = new Set();
                const text = node => (node?.textContent || "").replace(/\\s+/g, " ").trim();
                const isVisible = node => {
                  if (!node) return false;
                  if (node instanceof HTMLInputElement && node.type === "file") return true;
                  for (let current = node; current; current = current.parentElement) {
                    if (current.hidden) return false;
                    if (current.getAttribute?.("aria-hidden") === "true") return false;
                    const style = window.getComputedStyle(current);
                    if (style.display === "none" || style.visibility === "hidden") return false;
                  }
                  const rect = node.getBoundingClientRect();
                  return rect.width > 0 || rect.height > 0;
                };
                const readAriaLabelledBy = control => {
                  const ids = (control.getAttribute("aria-labelledby") || "").split(/\\s+/).filter(Boolean);
                  return ids.map(id => text(document.getElementById(id))).filter(Boolean).join(" ");
                };
                for (const control of form.querySelectorAll("input, textarea, select")) {
                  if (control.disabled) continue;
                  if (control instanceof HTMLInputElement && control.type === "hidden") continue;
                  if (!isVisible(control)) continue;
                  let fieldName = control.id || control.name || "";
                  let label = control.getAttribute("aria-label") || "";
                  if (!label && fieldName && window.CSS?.escape) {
                    label = text(form.querySelector(`label[for="${CSS.escape(fieldName)}"]`));
                  }
                  if (!label) label = readAriaLabelledBy(control);
                  if (!label) label = text(control.closest("fieldset")?.querySelector("legend"));
                  if (fieldName.startsWith("security-input-")) {
                    fieldName = "security_code";
                    label = label || "Security code";
                  }
                  const key = `${fieldName}|${label}`;
                  if (!fieldName && !label) continue;
                  if (seen.has(key)) continue;
                  seen.add(key);
                results.push({
                    field_name: fieldName,
                    label,
                    required: Boolean(control.required || control.getAttribute("aria-required") === "true"),
                    type: control instanceof HTMLInputElement ? (control.type || "input") : control.tagName.toLowerCase(),
                    role: control.getAttribute("role") || "",
                });
                }
                return results;
            }"""
        )

    def current_page_signature(page):
        return page.locator(form_selector).first.evaluate(
            """form => {
                const text = node => (node?.textContent || "").replace(/\\s+/g, " ").trim();
                const buttonText = node => {
                  if (node instanceof HTMLInputElement) {
                    return (node.value || node.getAttribute("aria-label") || "").trim();
                  }
                  return text(node);
                };
                const isVisible = node => {
                  if (!node) return false;
                  if (node instanceof HTMLInputElement && node.type === "file") return true;
                  for (let current = node; current; current = current.parentElement) {
                    if (current.hidden) return false;
                    if (current.getAttribute?.("aria-hidden") === "true") return false;
                    const style = window.getComputedStyle(current);
                    if (style.display === "none" || style.visibility === "hidden") return false;
                  }
                  const rect = node.getBoundingClientRect();
                  return rect.width > 0 || rect.height > 0;
                };
                const fields = Array.from(form.querySelectorAll("input, textarea, select"))
                  .filter(control => {
                    if (control.disabled) return false;
                    if (control instanceof HTMLInputElement && control.type === "hidden") return false;
                    return isVisible(control);
                  })
                  .map(control => {
                    let fieldName = control.id || control.name || "";
                    if (fieldName.startsWith("security-input-")) fieldName = "security_code";
                    return fieldName || control.getAttribute("aria-label") || "";
                  });
                const buttons = Array.from(
                  form.querySelectorAll("button, input[type='button'], input[type='submit']")
                )
                  .map(buttonText)
                  .filter(Boolean);
                return JSON.stringify({fields, buttons}, Object.keys({fields: [], buttons: []}).sort());
            }""",
        )

    def visible_unknown_questions(page):
        known_fields = {step["field_name"] for step in payload["steps"]}
        known_fields.add("security_code")
        known_labels = {_normalize_free_text(step["label"]) for step in payload["steps"]}
        known_fields.update(
            str(step.get("field_name") or "")
            for step in runtime["extra_report_steps"]
            if str(step.get("field_name") or "")
        )
        known_labels.update(
            _normalize_free_text(str(step.get("label") or ""))
            for step in runtime["extra_report_steps"]
            if str(step.get("label") or "")
        )
        separately_handled_labels = tuple(
            fragment for fragment, _ in _GREENHOUSE_DISCOVERED_DEMOGRAPHIC_FIELD_FRAGMENTS
        ) + (
            "highest level of school",
            "highest degree",
            "employment status",
            "candidate location",
            "location city",
            "what cities",
            "intend to work",
            "current location",
            "pronoun",
            "country",
            "how did you hear",
            "how did you learn",
            "how did you find out",
            "work authorization",
            "require sponsorship",
            "authorized to work",
            "right to work",
            "legally authorized",
            "require work authorization",
            "sponsorship",
            "sponsor",
            "visa",
            "acknowledge",
            "background check",
            "privacy consent",
            "data privacy",
            "first generation",
            "worked for",
            "previously employed",
            "previously been employed",
            "former",
            "employed by",
            "languages you speak",
            "languages do you speak",
        )
        unknown: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for field in visible_form_fields(page):
            if not field["required"]:
                continue
            if _is_classic_demographic_field_name(str(field["field_name"])):
                continue
            if str(field["field_name"]).startswith("gdpr_"):
                continue
            # Skip individual checkboxes that belong to multi-value checkbox
            # groups (e.g. "how did you hear" with options like LinkedIn,
            # Indeed).  These are handled at group level by
            # fill_discovered_profile_questions, not as individual fields.
            if field["type"] == "checkbox" and re.match(r"question_\d+\[\]_\d+", str(field["field_name"])):
                continue
            normalized_label = _normalize_free_text(field["label"] or "")
            key = (field["field_name"], normalized_label)
            if key in seen:
                continue
            seen.add(key)
            if any(fragment in normalized_label for fragment in separately_handled_labels):
                continue
            if field["field_name"] in known_fields or normalized_label in known_labels:
                continue
            unknown.append(field)
        return unknown

    def write_unknown_questions(page_index: int, questions: list[dict]) -> None:
        path = Path(artifacts["unknown_questions_json"])
        payload_data = {
            "job_url": payload["job_url"],
            "company": payload["company"],
            "job_title": payload["job_title"],
            "page_index": page_index,
            "questions": questions,
        }
        _write_or_clear_json_artifact(path, payload_data if questions else None)

    def next_button_locator(page):
        locator = page.locator("button, input[type='button'], input[type='submit']")
        for index in range(locator.count()):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible() and candidate.is_enabled() and NEXT_BUTTON_RE.search(control_text(candidate)):
                    return candidate
            except PlaywrightError:
                continue
        return None

    def submit_button_locator(page):
        classic = page.locator("#submit_app")
        if classic.count() > 0:
            return classic.first
        locator = page.locator("button, input[type='submit'], input[type='button']")
        for index in range(locator.count()):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible() and SUBMIT_BUTTON_RE.search(control_text(candidate)):
                    return candidate
            except PlaywrightError:
                continue
        fallback = page.locator("button[type='submit'], input[type='submit']")
        for index in range(fallback.count()):
            candidate = fallback.nth(index)
            try:
                if candidate.is_visible():
                    return candidate
            except PlaywrightError:
                continue
        return None

    def advance_to_next_page(page, *, page_index: int):
        button = next_button_locator(page)
        if button is None:
            raise RuntimeError(
                f"Application still has more steps to fill, but no Next/Continue button was found on page {page_index}."
            )
        signature = current_page_signature(page)
        button.scroll_into_view_if_needed()
        button.click()
        try:
            page.wait_for_function(
                """previous => {
                    const form = document.querySelector("#application-form") || document.querySelector("#application_form");
                    if (!form) return false;
                    const text = node => (node?.textContent || "").replace(/\\s+/g, " ").trim();
                    const fields = Array.from(form.querySelectorAll("input, textarea, select"))
                      .filter(control => {
                        if (control.disabled) return false;
                        if (control instanceof HTMLInputElement && control.type === "hidden") return false;
                        if (control instanceof HTMLInputElement && control.type === "file") return true;
                        const style = window.getComputedStyle(control);
                        if (style.display === "none" || style.visibility === "hidden") return false;
                        const rect = control.getBoundingClientRect();
                        return rect.width > 0 || rect.height > 0;
                      })
                      .map(control => {
                        let fieldName = control.id || control.name || "";
                        if (fieldName.startsWith("security-input-")) fieldName = "security_code";
                        return fieldName || control.getAttribute("aria-label") || "";
                      });
                    const buttons = Array.from(form.querySelectorAll("button"))
                      .concat(Array.from(form.querySelectorAll("input[type='button'], input[type='submit']")))
                      .map(button => button instanceof HTMLInputElement ? (button.value || button.getAttribute("aria-label") || "").trim() : text(button))
                      .filter(Boolean);
                    return JSON.stringify({fields, buttons}) !== previous;
                }""",
                signature,
                timeout=8000,
            )
        except PlaywrightError:
            page.wait_for_timeout(800)

    def capture_page_screenshot(page, *, page_index: int, field_names: list[str]) -> None:
        screenshot_path = _page_screenshot_path(out_dir, page_index)
        capture_stitched_screenshot(page, screenshot_path)
        page_record = {
            "index": page_index,
            "screenshot": str(screenshot_path),
            "fields": field_names,
        }
        pages = runtime["pages"]
        for index, existing in enumerate(pages):
            if existing.get("index") == page_index:
                pages[index] = page_record
                break
        else:
            pages.append(page_record)

    def capture_submit_debug_artifacts(page) -> None:
        html_path = artifacts.get("submit_debug_html")
        screenshot_path = artifacts.get("submit_debug_screenshot")
        if html_path:
            Path(html_path).write_text(page.content(), encoding="utf-8")
        if screenshot_path:
            capture_stitched_screenshot(page, screenshot_path)

    def fill_security_code_if_present(page, *, page_index: int, min_received_at_utc: datetime | str | None = None):
        security_inputs = page.locator("input[id^='security-input-'], #security_code")
        if security_inputs.count() == 0:
            return
        try:
            if not security_inputs.first.is_visible():
                return
        except PlaywrightError:
            return
        try:
            if security_inputs.first.is_disabled():
                return
        except PlaywrightError:
            pass

        current_values = []
        for index in range(security_inputs.count()):
            try:
                current_values.append((security_inputs.nth(index).input_value() or "").strip())
            except PlaywrightError:
                current_values.append("")
        security_error_text = ""
        for selector in (
            "#email-verification-error",
            ".email-verification .helper-text--error",
            ".email-verification [role='alert']",
        ):
            candidate = page.locator(selector).first
            if candidate.count() == 0:
                continue
            try:
                if candidate.is_visible():
                    security_error_text = candidate.inner_text().strip()
                    break
            except PlaywrightError:
                continue
        if all(current_values) and "security code" not in _normalize_free_text(security_error_text):
            return

        code = _fetch_security_code_from_gmail(
            payload.get("verification_code_email") or payload.get("candidate_email") or None,
            min_received_at_utc=min_received_at_utc,
        )
        if security_inputs.count() > 1:
            if len(code) < security_inputs.count():
                raise RuntimeError(
                    f"Gmail returned verification code {code!r}, but the form expects {security_inputs.count()} characters."
                )
            for index in range(security_inputs.count()):
                locator = security_inputs.nth(index)
                locator.fill("")
                locator.fill(code[index])
        else:
            security_inputs.first.fill(code)
        page.wait_for_timeout(200)
        runtime["extra_report_steps"].append(
            {
                "kind": "text",
                "field_name": "security_code",
                "label": "Security code",
                "value": code,
                "report_value": _redact_security_code(code),
                "optional": False,
                "source": "googleworkspace/cli:gmail",
                "page_index": page_index,
            }
        )

    def fill_discovered_profile_questions(page, *, page_index: int):
        location_question_specs = [
            {
                "field_name": "available_cities",
                "desired": ",".join(application_profile.available_cities)
                if application_profile.available_cities
                else application_profile.location,
                "label_fragments": ("what cities", "in what cities"),
                "field_names": set(),
                "exact_labels": set(),
            },
            {
                "field_name": "candidate_location",
                "desired": application_profile.location,
                "label_fragments": ("candidate location", "location city", "intend to work", "current location"),
                "field_names": {"candidate_location", "candidate-location", "job_application[location]"},
                "exact_labels": set(),
            },
            {
                "field_name": "country",
                "desired": application_profile.country,
                "label_fragments": tuple(),
                "field_names": {"country"},
                "exact_labels": {"country"},
            },
        ]

        def match_location_profile_spec(
            normalized_label: str,
            normalized_field_name: str,
        ) -> tuple[str, str] | None:
            if any(
                fragment in normalized_label
                for fragment in (
                    "sponsorship",
                    "sponsor",
                    "visa",
                    "work authorization",
                    "authorized to work",
                    "authorised to work",
                    "right to work",
                    "legally authorized",
                    "legally authorised",
                )
            ):
                return None
            for spec in location_question_specs:
                desired = str(spec["desired"] or "").strip()
                if not desired:
                    continue
                normalized_field_names = {_normalize_free_text(candidate) for candidate in spec["field_names"]}
                normalized_exact_labels = {_normalize_free_text(candidate) for candidate in spec["exact_labels"]}
                if (
                    normalized_field_name in normalized_field_names
                    or normalized_label in normalized_exact_labels
                    or any(fragment in normalized_label for fragment in spec["label_fragments"])
                ):
                    return str(spec["field_name"]), desired
            return None

        handled_field_names: set[str] = set()
        known_question_field_names = {
            str(step.get("field_name") or "").strip()
            for step in payload["steps"]
            if str(step.get("field_name") or "").strip()
        }
        handled_discovered_group_keys: set[str] = set()
        for field in visible_form_fields(page):
            normalized_label = _normalize_free_text(field.get("label") or "")
            normalized_field_name = _normalize_free_text(str(field.get("field_name") or ""))
            matched = match_location_profile_spec(normalized_label, normalized_field_name)
            if not matched:
                continue
            field_name, desired = matched
            if field_name in handled_field_names:
                continue
            locator = resolve_locator(page, discovered_field_step(field), raise_if_missing=False)
            if locator is None:
                continue
            current_option_text = ""
            field_role = _normalize_free_text(str(field.get("role") or ""))
            if field.get("type") == "select":
                selected_option_locator = locator.locator("option:checked").first
                if selected_option_locator.count() > 0:
                    try:
                        current_option_text = selected_option_locator.inner_text().strip()
                    except PlaywrightError:
                        current_option_text = ""
                if _greenhouse_combobox_value_matches_expected(field_name, desired, current_option_text):
                    handled_field_names.add(field_name)
                    continue
                option_texts = [
                    locator.locator("option").nth(i).inner_text().strip()
                    for i in range(locator.locator("option").count())
                ]
                selected_option = matching_option_text(field_name, desired, option_texts)
                if not selected_option:
                    continue
                try:
                    select_native_option(locator, field_name=field_name, desired=desired)
                except (PlaywrightError, RuntimeError):
                    if field.get("required"):
                        raise
                    continue
                runtime["extra_report_steps"].append(
                    {
                        "kind": "combobox",
                        "field_name": field_name,
                        "label": field["label"],
                        "value": selected_option,
                        "optional": False,
                        "source": "application_profile.md",
                        "page_index": page_index,
                        "filled": True,
                        "status": "filled",
                    }
                )
                handled_field_names.add(field_name)
                continue

            if field_role == "combobox":
                try:
                    current_option_text = read_combobox_display_value(locator)
                except PlaywrightError:
                    current_option_text = ""
                if option_text_matches(field_name, desired, current_option_text):
                    handled_field_names.add(field_name)
                    continue
                try:
                    selected_value = fill_combobox_locator(
                        page,
                        locator,
                        field_name=field_name,
                        search_value=_location_search_text(desired) if field_name == "candidate_location" else desired,
                        option_text="",
                    )
                except (PlaywrightError, RuntimeError):
                    if field.get("required"):
                        raise
                    continue
                runtime["extra_report_steps"].append(
                    {
                        "kind": "combobox",
                        "field_name": field_name,
                        "label": field["label"],
                        "value": selected_value or desired,
                        "optional": False,
                        "source": "application_profile.md",
                        "page_index": page_index,
                        "filled": True,
                        "status": "filled",
                    }
                )
                handled_field_names.add(field_name)

        # --- Remix education fields ---
        # Greenhouse's Remix UI renders education as React Select comboboxes
        # with ids like school--0, degree--0, discipline--0 and text inputs
        # start-year--0, end-year--0.  Fill them from the preferred education entry.
        education_entry = _preferred_education_entry(profile)
        education_container = page.locator(".education--container").first
        if education_entry and education_container.count() > 0:
            _remix_edu_specs = [
                ("school--0", education_entry.school, "combobox"),
                ("degree--0", education_entry.degree_option, "combobox"),
                ("discipline--0", education_entry.discipline_option, "combobox"),
                ("start-year--0", education_entry.start_year, "text"),
                ("end-year--0", education_entry.end_year, "text"),
            ]
            for edu_field_id, edu_desired, edu_kind in _remix_edu_specs:
                if not edu_desired:
                    continue
                edu_loc = page.locator(f"#{edu_field_id}")
                if edu_loc.count() == 0:
                    continue
                try:
                    if edu_kind == "text":
                        current_val = (edu_loc.input_value() or "").strip()
                        if current_val:
                            continue
                        edu_loc.scroll_into_view_if_needed()
                        human_fill(edu_loc, edu_desired, delay_ms=35)
                    else:
                        # React Select combobox: click to expand, read all
                        # options, pick the best match.
                        edu_loc.scroll_into_view_if_needed()
                        page.wait_for_timeout(300)
                        edu_loc.click(timeout=2000)
                        page.wait_for_timeout(400)
                        # For school (server-side search), type to populate;
                        # for degree/discipline (client-side list), just read.
                        is_school = "school" in edu_field_id
                        if is_school:
                            edu_loc.fill("")
                            page.wait_for_timeout(100)
                            search_term = edu_desired.split("(")[0].strip() if "(" in edu_desired else edu_desired
                            edu_loc.press_sequentially(search_term[:30], delay=35)
                            page.wait_for_timeout(800)
                        # Read available options from the listbox
                        listbox_id = edu_loc.get_attribute("aria-controls") or ""
                        if listbox_id:
                            opt_loc = page.locator(f'[id="{listbox_id}"] [role="option"]')
                            opt_count = opt_loc.count()
                            if opt_count > 0:
                                opt_texts = []
                                for oi in range(opt_count):
                                    try:
                                        opt_texts.append(opt_loc.nth(oi).inner_text().strip())
                                    except PlaywrightError:
                                        opt_texts.append("")
                                matched_opt = matching_option_text(
                                    edu_field_id,
                                    edu_desired,
                                    opt_texts,
                                    fallback_values=_education_degree_fallback_options(education_entry)
                                    if "degree" in edu_field_id
                                    else _education_discipline_fallback_options(education_entry)
                                    if "discipline" in edu_field_id
                                    else None,
                                )
                                if matched_opt:
                                    for oi, ot in enumerate(opt_texts):
                                        if ot == matched_opt:
                                            opt_loc.nth(oi).click(timeout=2000)
                                            break
                                else:
                                    # Select first option as fallback
                                    opt_loc.first.click(timeout=2000)
                            else:
                                edu_loc.press("ArrowDown")
                                edu_loc.press("Enter")
                        else:
                            edu_loc.press("ArrowDown")
                            edu_loc.press("Enter")
                        page.wait_for_timeout(300)
                    selected_val = (
                        read_combobox_display_value(edu_loc)
                        if edu_kind == "combobox"
                        else (edu_loc.input_value() or "").strip()
                    )
                    if selected_val and not _normalize_free_text(selected_val).startswith("select"):
                        runtime["extra_report_steps"].append(
                            {
                                "kind": edu_kind,
                                "field_name": edu_field_id,
                                "label": edu_field_id.replace("--0", "").replace("-", " ").title(),
                                "value": selected_val,
                                "optional": True,
                                "source": "master_resume.md",
                                "page_index": page_index,
                                "filled": True,
                                "status": "filled",
                            }
                        )
                except (PlaywrightError, RuntimeError):
                    continue

        # --- Remix employment fields ---
        # Greenhouse's Remix UI renders employment as text inputs and React
        # Select comboboxes inside an .employment--container with ids like
        # company-name-0, title-0, start-date-month-0, start-date-year-0.
        # Fill from the most recent employment entry (first in the list).
        employment_container = page.locator(".employment--container").first
        if profile.employment_entries and employment_container.count() > 0:
            emp_entry = profile.employment_entries[0]
            _remix_emp_specs: list[tuple[str, str, str]] = [
                ("company-name-0", emp_entry.company, "text"),
                ("title-0", emp_entry.title, "text"),
                ("start-date-month-0", emp_entry.start_month, "combobox"),
                ("start-date-year-0", emp_entry.start_year, "text"),
            ]
            for emp_field_id, emp_desired, emp_kind in _remix_emp_specs:
                if not emp_desired:
                    continue
                emp_loc = page.locator(f"#{emp_field_id}")
                if emp_loc.count() == 0:
                    continue
                try:
                    if emp_kind == "text":
                        current_val = (emp_loc.input_value() or "").strip()
                        if current_val:
                            continue
                        emp_loc.scroll_into_view_if_needed()
                        human_fill(emp_loc, emp_desired, delay_ms=35)
                    else:
                        # React Select combobox (month picker): click, read
                        # options, pick the best match.
                        emp_loc.scroll_into_view_if_needed()
                        page.wait_for_timeout(300)
                        emp_loc.click(timeout=2000)
                        page.wait_for_timeout(400)
                        listbox_id = emp_loc.get_attribute("aria-controls") or ""
                        if listbox_id:
                            opt_loc = page.locator(f'[id="{listbox_id}"] [role="option"]')
                            opt_count = opt_loc.count()
                            if opt_count > 0:
                                opt_texts = []
                                for oi in range(opt_count):
                                    try:
                                        opt_texts.append(opt_loc.nth(oi).inner_text().strip())
                                    except PlaywrightError:
                                        opt_texts.append("")
                                matched_opt = matching_option_text(
                                    emp_field_id,
                                    emp_desired,
                                    opt_texts,
                                )
                                if matched_opt:
                                    for oi, ot in enumerate(opt_texts):
                                        if ot == matched_opt:
                                            opt_loc.nth(oi).click(timeout=2000)
                                            break
                                else:
                                    opt_loc.first.click(timeout=2000)
                            else:
                                emp_loc.press("ArrowDown")
                                emp_loc.press("Enter")
                        else:
                            emp_loc.press("ArrowDown")
                            emp_loc.press("Enter")
                        page.wait_for_timeout(300)
                    selected_val = (
                        read_combobox_display_value(emp_loc)
                        if emp_kind == "combobox"
                        else (emp_loc.input_value() or "").strip()
                    )
                    if selected_val and not _normalize_free_text(selected_val).startswith("select"):
                        runtime["extra_report_steps"].append(
                            {
                                "kind": emp_kind,
                                "field_name": emp_field_id,
                                "label": emp_field_id.replace("-0", "").replace("-", " ").title(),
                                "value": selected_val,
                                "optional": False,
                                "source": "master_resume.md",
                                "page_index": page_index,
                                "filled": True,
                                "status": "filled",
                            }
                        )
                except (PlaywrightError, RuntimeError):
                    continue
            # Check "Current role" checkbox for the most recent entry to
            # indicate ongoing employment (hides end-date requirement).
            try:
                current_role_cb = page.locator("#current-role-0_1")
                if current_role_cb.count() > 0 and not current_role_cb.is_checked():
                    current_role_cb.scroll_into_view_if_needed()
                    current_role_cb.check(timeout=2000)
                    runtime["extra_report_steps"].append(
                        {
                            "kind": "checkbox",
                            "field_name": "current-role-0",
                            "label": "Current Role",
                            "value": "Checked",
                            "optional": True,
                            "source": "master_resume.md",
                            "page_index": page_index,
                            "filled": True,
                            "status": "filled",
                        }
                    )
            except (PlaywrightError, RuntimeError):
                pass

        groups = page.locator(form_selector).first.locator(
            ".demographic_question, [id$='_dropdown_container'][data-eeoc-question]"
        )
        if groups.count() == 0:
            groups = page.locator(form_selector).first.locator(".field-wrapper, fieldset, .field, .select")
        for index in range(groups.count()):
            group = groups.nth(index)
            checkboxes = group.locator("input[type='checkbox'], input[type='radio']")
            selects = group.locator("select")
            comboboxes = group.locator("[role='combobox']")
            if checkboxes.count() == 0 and selects.count() == 0 and comboboxes.count() == 0:
                continue
            try:
                group_required = bool(
                    group.evaluate(
                        """node => {
                            const control = node.querySelector("select, input, textarea, [role='combobox']");
                            if (!control) return false;
                            return Boolean(control.required || control.getAttribute?.("aria-required") === "true");
                        }"""
                    )
                )
            except PlaywrightError:
                group_required = False
            label_locator = group.locator("label").first
            label_text = ""
            if label_locator.count() > 0:
                try:
                    label_text = label_locator.inner_text().strip()
                except PlaywrightError:
                    label_text = ""
            text = group.inner_text().strip()
            heading_source = text.split("\n", 1)[0].strip() or label_text
            heading = _normalize_free_text(heading_source)
            matched = match_location_profile_spec(heading, "")
            if not matched:
                matched = _match_greenhouse_discovered_demographic_question(heading_source, application_profile)
            if not matched:
                continue
            field_name, desired = matched
            control_identifier = ""
            control = None
            if selects.count() > 0:
                control = selects.first
            elif comboboxes.count() > 0:
                control = comboboxes.first
            elif checkboxes.count() > 0:
                control = checkboxes.first
            if control is not None:
                try:
                    control_identifier = str(control.get_attribute("id") or "").strip()
                except PlaywrightError:
                    control_identifier = ""
                if not control_identifier:
                    try:
                        control_identifier = str(control.get_attribute("name") or "").strip()
                    except PlaywrightError:
                        control_identifier = ""
            if control_identifier and control_identifier in known_question_field_names:
                continue
            group_key = _greenhouse_discovered_group_key(
                field_name,
                control_id=control_identifier,
                label=heading_source,
            )
            if group_key in handled_discovered_group_keys:
                continue
            desired_normalized = _normalize_free_text(desired)
            planned_step = _mark_visible_discovered_greenhouse_step(
                {
                    "kind": "combobox"
                    if comboboxes.count() > 0 and selects.count() == 0 and checkboxes.count() == 0
                    else "checkbox",
                    "field_name": field_name,
                    "label": heading_source.strip(),
                    "value": desired,
                    "optional": False,
                    "source": "application_profile.md",
                    "page_index": page_index,
                    "status": "planned",
                }
            )
            runtime["extra_report_steps"].append(planned_step)

            def _mark_filled(_ps=planned_step):
                _ps["status"] = "filled"
                _ps["filled"] = True

            if selects.count() > 0:
                select = selects.first
                option_locator = select.locator("option")
                option_texts = [option_locator.nth(i).inner_text().strip() for i in range(option_locator.count())]
                resolved_desired = _resolve_discovered_demographic_option_text(
                    field_name=field_name,
                    desired=desired,
                    option_texts=option_texts,
                    application_profile=application_profile,
                    company_name=str(payload.get("company") or ""),
                    job_url=str(payload.get("job_url") or ""),
                    source_url=str(payload.get("job_source_url") or ""),
                    source_hint=str(payload.get("source") or ""),
                )
                current_option_text = ""
                selected_option_locator = select.locator("option:checked").first
                if selected_option_locator.count() > 0:
                    try:
                        current_option_text = selected_option_locator.inner_text().strip()
                    except PlaywrightError:
                        current_option_text = ""
                if _greenhouse_combobox_value_matches_expected(field_name, resolved_desired, current_option_text):
                    planned_step["value"] = current_option_text or resolved_desired
                    _mark_filled()
                    handled_discovered_group_keys.add(group_key)
                    continue
                try:
                    selected_option = select_native_option(select, field_name=field_name, desired=resolved_desired)
                    if not _greenhouse_combobox_value_matches_expected(field_name, resolved_desired, selected_option):
                        raise RuntimeError(
                            f"Could not confirm demographic select {field_name!r} after choosing {resolved_desired!r}."
                        )
                    planned_step["value"] = selected_option
                    _mark_filled()
                except (PlaywrightError, RuntimeError):
                    if group_required:
                        raise
                    continue
            elif comboboxes.count() > 0:
                combobox = comboboxes.first
                resolved_desired_values = _greenhouse_discovered_combobox_desired_values(field_name, desired)
                try:
                    current_option_text = read_combobox_display_value(combobox)
                except PlaywrightError:
                    current_option_text = ""
                if _greenhouse_discovered_combobox_can_skip_open(
                    field_name,
                    resolved_desired_values,
                    current_option_text,
                ):
                    planned_step["value"] = current_option_text or resolved_desired_values[0]
                    _mark_filled()
                    handled_discovered_group_keys.add(group_key)
                    continue

                try:
                    combobox.scroll_into_view_if_needed()
                    combobox.click(force=True, timeout=1500)
                    page.wait_for_timeout(120)
                    interaction_locator = combobox_interaction_locator(page, combobox)
                    live_option_texts = [text for _, text in current_combobox_options(page, interaction_locator)]
                except PlaywrightError:
                    interaction_locator = None
                    live_option_texts = []
                resolved_desired_values = [
                    _resolve_discovered_demographic_option_text(
                        field_name=field_name,
                        desired=value,
                        option_texts=live_option_texts,
                        application_profile=application_profile,
                        company_name=str(payload.get("company") or ""),
                        job_url=str(payload.get("job_url") or ""),
                        source_url=str(payload.get("job_source_url") or ""),
                        source_hint=str(payload.get("source") or ""),
                    )
                    for value in resolved_desired_values
                ]
                # Multi-value comboboxes are an explicit allowlist; many
                # single-select self-ID labels also contain commas.
                is_multi = len(resolved_desired_values) > 1
                desired_values = resolved_desired_values
                if _greenhouse_discovered_combobox_can_skip_open(
                    field_name,
                    desired_values,
                    current_option_text,
                ):
                    if interaction_locator is not None:
                        try:
                            interaction_locator.press("Escape")
                            page.wait_for_timeout(80)
                        except PlaywrightError:
                            pass
                    planned_step["value"] = current_option_text or desired_values[0]
                    _mark_filled()
                    handled_discovered_group_keys.add(group_key)
                    continue

                def _fill_combobox_value(target_desired, _cb=combobox, _grp=group, _fn=field_name):
                    """Fill a Greenhouse combobox value and return the confirmed display text."""
                    try:
                        filled_value = fill_combobox_locator(
                            page,
                            _cb,
                            field_name=_fn,
                            search_value=target_desired,
                            option_text=target_desired,
                        )
                        page.wait_for_timeout(120)
                        try:
                            observed_value = read_combobox_display_value(_cb)
                        except PlaywrightError:
                            observed_value = ""
                        confirmed_value = _confirmed_combobox_selection_value(_fn, target_desired, observed_value)
                        return confirmed_value or _confirmed_combobox_selection_value(_fn, target_desired, filled_value)
                    except (PlaywrightError, RuntimeError):
                        pass
                    return None

                filled_values: list[str] = []
                for dv in desired_values:
                    selected_value = _fill_combobox_value(dv)
                    if selected_value:
                        filled_values.append(selected_value)

                if filled_values:
                    planned_step["value"] = desired if is_multi else filled_values[0]

                    _mark_filled()
                else:
                    if group_required:
                        raise RuntimeError(
                            f"Could not fill demographic combobox {field_name!r} with any of {desired_values!r}"
                        )
                    continue
            else:
                selected = group.locator(
                    "label input:checked, input[type='checkbox']:checked, input[type='radio']:checked"
                )
                if selected.count() > 0:
                    _mark_filled()
                    handled_discovered_group_keys.add(group_key)
                    continue
                labels = group.locator("label")
                option_texts = [labels.nth(i).inner_text().strip() for i in range(labels.count())]
                resolved_checkbox_desired = _resolve_discovered_demographic_option_text(
                    field_name=field_name,
                    desired=desired,
                    option_texts=option_texts,
                    application_profile=application_profile,
                    company_name=str(payload.get("company") or ""),
                    job_url=str(payload.get("job_url") or ""),
                    source_url=str(payload.get("job_source_url") or ""),
                    source_hint=str(payload.get("source") or ""),
                )
                desired_normalized = _normalize_free_text(resolved_checkbox_desired)
                option_index = None
                exact_match_index = None
                fuzzy_match_index = None
                for label_index in range(labels.count()):
                    option_text = labels.nth(label_index).inner_text().strip()
                    if option_text_matches(field_name, resolved_checkbox_desired, option_text):
                        exact_match_index = label_index
                        break
                    normalized = _normalize_free_text(option_text)
                    if fuzzy_match_index is None and (
                        desired_normalized in normalized or normalized in desired_normalized
                    ):
                        fuzzy_match_index = label_index
                option_index = exact_match_index if exact_match_index is not None else fuzzy_match_index
                # Word-level fallback: match individual words from the desired
                # value against option labels (e.g. "Corporate website" →
                # "website" matches "Gusto Website").
                if option_index is None:
                    desired_words = [w for w in desired_normalized.split() if len(w) >= 4]
                    for word in reversed(desired_words):
                        for label_index in range(labels.count()):
                            opt_norm = _normalize_free_text(labels.nth(label_index).inner_text().strip())
                            if word in opt_norm:
                                option_index = label_index
                                break
                        if option_index is not None:
                            break
                if option_index is not None:
                    target_label = labels.nth(option_index)
                    target_input = target_label.locator("input").first
                    try:
                        # Click the label to trigger Greenhouse's JS
                        # single-select handler (force-checking the input
                        # bypasses the click event and leaves the form
                        # state inconsistent).
                        target_label.scroll_into_view_if_needed()
                        target_label.click()
                        page.wait_for_timeout(150)
                        _mark_filled()
                    except PlaywrightError:
                        try:
                            target_input.check(force=True)
                            _mark_filled()
                        except PlaywrightError:
                            if group_required:
                                raise
                            continue
                else:
                    # Fallback: match checkboxes/radios by adjacent text when not in labels
                    all_inputs = group.locator("input[type='checkbox'], input[type='radio']")
                    clicked = False
                    for input_index in range(all_inputs.count()):
                        cb = all_inputs.nth(input_index)
                        try:
                            parent_text = cb.evaluate("el => el.parentElement?.textContent?.trim() || ''")
                        except PlaywrightError:
                            continue
                        normalized_parent = _normalize_free_text(parent_text)
                        if option_text_matches(field_name, resolved_checkbox_desired, parent_text) or (
                            desired_normalized in normalized_parent or normalized_parent == desired_normalized
                        ):
                            try:
                                cb.check(force=True)
                                clicked = True
                                _mark_filled()
                                break
                            except PlaywrightError:
                                continue
                    if not clicked:
                        if not group_required:
                            continue
                        raise RuntimeError(
                            f"Could not find answer {resolved_checkbox_desired!r} for visible question {heading!r}."
                        )
            handled_discovered_group_keys.add(group_key)

    def sync_runtime_confirmations(page, *, page_index: int, settle_ms: int = 0) -> None:
        if settle_ms:
            page.wait_for_timeout(settle_ms)
        confirm_file_upload_steps(page, page_index=page_index)
        check_gdpr_consent_checkboxes(page)
        check_acknowledgment_checkboxes(page)
        demographic_answers = page.evaluate(
            """formSelector => {
                const normalize = value => (value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
                const text = node => (node?.textContent || "").replace(/\\s+/g, " ").trim();
                const isVisible = node => {
                  if (!node) return false;
                  for (let current = node; current; current = current.parentElement) {
                    if (current.hidden) return false;
                    if (current.getAttribute?.('aria-hidden') === 'true') return false;
                    const style = window.getComputedStyle(current);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                  }
                  const rect = node.getBoundingClientRect();
                  return rect.width > 0 || rect.height > 0;
                };
                const form =
                  document.querySelector("#application-form")
                  || document.querySelector("#application_form");
                if (!form) return [];
                const explicitGroups = Array.from(form.querySelectorAll(".demographic_question"));
                const groups = explicitGroups.length
                  ? explicitGroups
                  : Array.from(form.querySelectorAll(".field-wrapper, .field, .select"))
                      .filter(group => !group.querySelector(".demographic_question"));
                return groups.map(group => {
                    const directHeading = Array.from(group.childNodes || [])
                      .filter(node => node.nodeType === Node.TEXT_NODE)
                      .map(text)
                      .find(Boolean) || "";
                    const heading =
                      directHeading
                      || text(group.querySelector("legend"))
                      || text(group.querySelector("label"));
                    const checked = group.querySelector("input[type='checkbox']:checked, input[type='radio']:checked");
                    const selectedOption = group.querySelector("select option:checked");
                    const select = group.querySelector("select");
                    const combobox = group.querySelector('[role="combobox"]');
                    const textInput = group.querySelector(
                      "input:not([type='hidden']):not([type='checkbox']):not([type='radio']), textarea"
                    );
                    const checkedLabel =
                      text(checked?.closest("label"))
                      || text(
                        checked?.id
                          ? group.querySelector(`label[for="${CSS.escape(checked.id)}"]`)
                            || form.querySelector(`label[for="${CSS.escape(checked.id)}"]`)
                          : null
                      )
                      || text(checked?.parentElement?.nextElementSibling)
                      || text(checked?.closest(".checkbox__wrapper, .radio__wrapper, .field-wrapper"));
                    const comboboxMultiValues = Array.from(
                      group.querySelectorAll(".select__multi-value__label, [class*='multi-value__label']")
                    ).map(text).filter(Boolean);
                    const comboboxValue =
                      comboboxMultiValues.join(", ")
                      || text(group.querySelector(".select__single-value, [class*='single-value']"))
                      || (combobox?.value || "").trim();
                    return {
                        heading,
                        normalized_heading: normalize(heading),
                        kind: (select || combobox) ? "combobox" : "checkbox",
                        visible: isVisible(group),
                        value:
                          checkedLabel
                          || text(selectedOption)
                          || comboboxValue
                          || ((textInput?.value || "").trim())
                          || "",
                    };
                }).filter(item => item.heading);
            }""",
            form_selector,
        )
        extra_steps = runtime["extra_report_steps"]
        for answer in demographic_answers:
            existing = next(
                (
                    step
                    for step in extra_steps
                    if str(step.get("label") or "").strip() == str(answer.get("heading") or "").strip()
                    or str(step.get("field_name") or "").strip()
                    == str(
                        (
                            _match_greenhouse_discovered_demographic_question(
                                str(answer.get("heading") or ""),
                                application_profile,
                            )
                            or (None,)
                        )[0]
                        or _greenhouse_discovered_demographic_field_name(str(answer.get("heading") or ""))
                        or ""
                    ).strip()
                ),
                None,
            )
            step_payload = _greenhouse_runtime_confirmation_step_from_discovered_group(
                answer=answer,
                application_profile=application_profile,
                page_index=page_index,
                existing_step=existing,
            )
            if step_payload is None:
                continue
            if existing:
                existing.update(step_payload)
            else:
                extra_steps.append(step_payload)

    def check_gdpr_consent_checkboxes(page) -> None:
        """Auto-check all GDPR consent checkboxes (demographic data, processing, etc.)."""
        gdpr_prefixes = [
            "gdpr_demographic_data_consent",
            "gdpr_processing_consent",
        ]
        selectors = []
        for prefix in gdpr_prefixes:
            selectors.extend(
                [
                    f"{form_selector} input[name^='{prefix}']",
                    f"{form_selector} input[id^='{prefix}']",
                    f"input[name^='{prefix}']",
                    f"input[id^='{prefix}']",
                    # Greenhouse wraps the name as job_application[data_compliance][gdpr_...]
                    f"{form_selector} input[name*='{prefix}']",
                    f"{form_selector} input[id*='{prefix}']",
                    f"input[name*='{prefix}']",
                    f"input[id*='{prefix}']",
                ]
            )
        checked_any = False
        for selector in selectors:
            try:
                checkboxes = page.locator(selector)
                count = checkboxes.count()
                if count == 0:
                    continue
                for i in range(count):
                    cb = checkboxes.nth(i)
                    if not cb.is_visible():
                        continue
                    if _ensure_checkbox_checked(page, cb):
                        checked_any = True
                        cb_name = cb.get_attribute("name") or cb.get_attribute("id") or "gdpr_consent"
                        runtime["extra_report_steps"].append(
                            {
                                "kind": "checkbox",
                                "field_name": cb_name,
                                "label": "GDPR consent",
                                "value": "Checked",
                                "source": "auto_consent",
                                "filled": True,
                                "status": "filled",
                            }
                        )
            except PlaywrightError:
                continue
        if checked_any:
            return

    def check_acknowledgment_checkboxes(page) -> None:
        """Auto-check required acknowledgment/confirmation checkboxes.

        These are single-option checkbox groups with labels like
        "Acknowledge/Confirm" used for policy acknowledgments (e.g. AI use
        policies).  The checkbox field names follow the ``question_NNN[]_NNN``
        pattern so they are skipped by the payload builder and unknown-question
        detector, but they still need to be checked at runtime.
        """
        consent_fragments = ("acknowledge", "confirm", "i agree", "i accept")
        # Fragments that indicate multi-select questions, NOT simple acknowledgments
        multi_select_exclusions = ("select all that apply", "sanctions", "export control")
        groups = page.locator(f"{form_selector} fieldset.checkbox, {form_selector} .field-wrapper")
        for i in range(groups.count()):
            group = groups.nth(i)
            checkboxes = group.locator("input[type='checkbox']")
            if checkboxes.count() == 0:
                continue
            # Skip multi-option groups — real acknowledgments have 1-2 checkboxes.
            # Groups with 3+ checkboxes are multi-select questions (e.g. sanctions,
            # export controls) that need specific answers, not blanket checking.
            if checkboxes.count() > 2:
                continue
            # Check all text in the group (labels, legend, etc.) for consent keywords.
            try:
                group_text = _normalize_free_text(group.inner_text().strip())
            except PlaywrightError:
                continue
            if not any(fragment in group_text for fragment in consent_fragments):
                continue
            # Skip groups that look like multi-select questions
            if any(excl in group_text for excl in multi_select_exclusions):
                continue
            for ci in range(checkboxes.count()):
                cb = checkboxes.nth(ci)
                try:
                    if _ensure_checkbox_checked(page, cb):
                        runtime["extra_report_steps"].append(
                            {
                                "kind": "checkbox",
                                "field_name": "acknowledgment_consent",
                                "label": group_text[:80],
                                "value": "Checked",
                                "source": "auto_consent",
                                "filled": True,
                                "status": "filled",
                            }
                        )
                except PlaywrightError:
                    continue

    def refresh_review_artifacts(page) -> dict:
        current_page_index = runtime["pages"][-1]["index"] if runtime["pages"] else 1
        sync_runtime_confirmations(page, page_index=current_page_index, settle_ms=2000)
        snapshot = read_submission_snapshot(page)
        validation_state = _classify_submission_snapshot(snapshot)
        runtime["review_validation_blockers"] = (
            _review_validation_blockers_from_snapshot(snapshot)
            if validation_state.get("status") == "validation_error"
            else []
        )
        _capture_review_checkpoint_artifacts(page, artifacts)
        return _write_autofill_report(payload, runtime)

    def record_unconfirmed_fields(report_payload: dict) -> Path | None:
        blocking_fields = blocking_unconfirmed_report_entries(
            list(report_payload.get("planned_but_unconfirmed_fields") or [])
        )
        pending_path = write_pending_user_input_for_unconfirmed_fields(
            out_dir,
            board="greenhouse",
            fields=blocking_fields,
            report_json=artifacts.get("report_json"),
            report_markdown=artifacts.get("report_markdown"),
            pre_submit_screenshot=artifacts.get("pre_submit_screenshot"),
        )
        if pending_path is not None:
            print(
                "Greenhouse autofill left planned fields unconfirmed. "
                f"See {pending_path.relative_to(PROJECT_ROOT)} before submitting.",
                file=sys.stderr,
            )
        return pending_path

    def read_submission_snapshot(page) -> dict[str, object]:
        snapshot = page.evaluate(
            """formSelector => {
                const text = node => (node?.textContent || "").replace(/\\s+/g, " ").trim();
                const isVisible = node => {
                  if (!node) return false;
                  for (let current = node; current; current = current.parentElement) {
                    if (current.hidden) return false;
                    if (current.getAttribute?.("aria-hidden") === "true") return false;
                    const style = window.getComputedStyle(current);
                    if (style.display === "none" || style.visibility === "hidden") return false;
                  }
                  const rect = node.getBoundingClientRect();
                  return rect.width > 0 || rect.height > 0;
                };
                const form = document.querySelector(formSelector);
                const readLabel = control => {
                  let label = control.getAttribute("aria-label") || "";
                  if (!label && control.id && window.CSS?.escape) {
                    label = text(form?.querySelector(`label[for="${CSS.escape(control.id)}"]`));
                  }
                  if (!label) label = text(control.closest("fieldset")?.querySelector("legend"));
                  return label || control.name || control.id || control.tagName.toLowerCase();
                };
                const readCheckboxGroupLabel = control => {
                  return (
                    text(control.closest("fieldset")?.querySelector("legend"))
                    || text(control.closest(".field-wrapper, .field")?.querySelector("legend"))
                    || readLabel(control)
                  );
                };
                const checkboxGroupKey = control => {
                  const groupLabel = readCheckboxGroupLabel(control);
                  const groupNode =
                    control.closest("fieldset")
                    || control.closest(".field-wrapper")
                    || control.closest(".field")
                    || control.parentElement;
                  const groupIndex = groupNode && form ? Array.from(form.querySelectorAll("fieldset, .field-wrapper, .field")).indexOf(groupNode) : -1;
                  return `${control.name || control.id || "checkbox-group"}::${groupLabel}::${groupIndex}`;
                };
                const errorSelectors = [
                  ".error",
                  ".errors",
                  ".field_with_errors",
                  ".field-error",
                  ".validation-error",
                  "[role='alert']",
                  "[aria-live='assertive']",
                  ".required-error",
                  ".flash-error",
                  ".error-text",
                  ".invalid-feedback",
                  ".message.error"
                ];
                const errors = [];
                if (form) {
                  for (const selector of errorSelectors) {
                    for (const node of form.querySelectorAll(selector)) {
                      if (!isVisible(node)) continue;
                      const value = text(node);
                      if (value) errors.push(value);
                    }
                  }
                }
                const invalidFields = [];
                const invalidFieldGroups = [];
                const checkedCheckboxGroups = [];
                const checkboxGroupStates = new Map();
                if (form) {
                  for (const control of form.querySelectorAll("input, textarea, select")) {
                    if (!isVisible(control)) continue;
                    const isInvalid = control.matches("[aria-invalid='true']") || control.matches(":invalid");
                    if (control instanceof HTMLInputElement && (control.type === "checkbox" || control.type === "radio")) {
                      const groupKey = checkboxGroupKey(control);
                      const current = checkboxGroupStates.get(groupKey) || {
                        fieldLabels: [],
                        groupLabel: readCheckboxGroupLabel(control),
                        anyChecked: false,
                        anyInvalid: false,
                      };
                      const fieldLabel = readLabel(control);
                      if (fieldLabel && !current.fieldLabels.includes(fieldLabel)) {
                        current.fieldLabels.push(fieldLabel);
                      }
                      current.anyChecked = current.anyChecked || Boolean(control.checked);
                      current.anyInvalid = current.anyInvalid || Boolean(isInvalid);
                      checkboxGroupStates.set(groupKey, current);
                      continue;
                    }
                    if (!isInvalid) continue;
                    invalidFields.push(readLabel(control));
                  }
                  for (const state of checkboxGroupStates.values()) {
                    if (state.anyChecked && state.groupLabel) {
                      checkedCheckboxGroups.push(state.groupLabel);
                    }
                    if (!state.anyInvalid || state.anyChecked) continue;
                    if (state.groupLabel) {
                      invalidFields.push(state.groupLabel);
                    }
                    for (const fieldLabel of state.fieldLabels) {
                      invalidFieldGroups.push({field: fieldLabel, group: state.groupLabel});
                    }
                  }
                }
                const checkedFields = [];
                if (form) {
                  for (const control of form.querySelectorAll("input[type='checkbox']:checked, input[type='radio']:checked")) {
                    if (!isVisible(control)) continue;
                    checkedFields.push(readLabel(control));
                  }
                }
                const securityInput = document.querySelector("#security_code, input[id^='security-input-']");
                const confirmationNode = document.querySelector("#submission_received, #application_confirmation");
                return {
                  page_title: document.title || "",
                  page_text: text(document.body).slice(0, 16000),
                  form_visible: isVisible(form),
                  security_code_visible: Boolean(securityInput && isVisible(securityInput) && !securityInput.disabled),
                  confirmation_visible: Boolean(confirmationNode && isVisible(confirmationNode)),
                  errors: Array.from(new Set(errors)).slice(0, 12),
                  checked_fields: Array.from(new Set(checkedFields)).slice(0, 12),
                  checked_checkbox_groups: Array.from(new Set(checkedCheckboxGroups)).slice(0, 12),
                  invalid_fields: Array.from(new Set(invalidFields)).slice(0, 12),
                  invalid_field_groups: invalidFieldGroups.slice(0, 24),
                };
            }""",
            form_selector,
        )
        snapshot["url"] = page.url
        return snapshot

    def wait_for_submission_outcome(
        page,
        *,
        timeout_ms: int,
        email_watcher=None,
        return_on_security_code_required: bool = True,
    ) -> dict[str, object]:
        deadline = time.monotonic() + (timeout_ms / 1000)
        last_snapshot: dict[str, object] | None = None
        last_state: dict[str, object] = {"status": "pending"}
        while time.monotonic() < deadline:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=500)
            except PlaywrightError:
                pass
            try:
                snapshot = read_submission_snapshot(page)
            except PlaywrightError as exc:
                if "context was destroyed" in str(exc) or "navigation" in str(exc).lower():
                    return {
                        "status": "confirmed",
                        "snapshot": {
                            "url": page.url,
                            "page_text": "(page navigated after submit — likely success)",
                            "form_visible": False,
                            "security_code_visible": False,
                            "errors": [],
                            "invalid_fields": [],
                        },
                        "reason": "post_submit_navigation",
                    }
                raise
            state = _classify_submission_snapshot(snapshot)
            if email_watcher is not None:
                email_confirmation = email_watcher.poll()
                if email_confirmation:
                    return {
                        "status": "confirmed",
                        "snapshot": snapshot,
                        "reason": "email_confirmation",
                        "email_confirmation": email_confirmation,
                    }
            if state["status"] == "confirmed" or (
                return_on_security_code_required and state["status"] == "security_code_required"
            ):
                state["snapshot"] = snapshot
                return state
            last_snapshot = snapshot
            last_state = state
            page.wait_for_timeout(250)
        if email_watcher is not None:
            email_confirmation = email_watcher.poll(force=True)
            if email_confirmation:
                return {
                    "status": "confirmed",
                    "snapshot": last_snapshot or {},
                    "reason": "email_confirmation",
                    "email_confirmation": email_confirmation,
                }
        if last_state.get("status") in {"validation_error", "security_code_required"}:
            last_state["snapshot"] = last_snapshot or {}
            return last_state
        last_state["status"] = "timeout"
        last_state["snapshot"] = last_snapshot or {}
        return last_state

    def launch_browser(playwright):
        viewport = submit_viewport()
        return launch_chromium_browser(
            playwright,
            headless=headless,
            slow_mo=submit_slow_mo_ms(headless),
            channel_env_var="GREENHOUSE_AUTOFILL_BROWSER_CHANNEL",
            executable_env_var="GREENHOUSE_AUTOFILL_BROWSER_EXECUTABLE",
            persistent_profile_dir=submit_browser_profile_dir(),
            prefer_local_browser=True,
            viewport=viewport,
            device_scale_factor=2,
            purpose="Greenhouse autofill",
        )

    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        viewport = submit_viewport()
        page = browser.new_page(
            viewport=viewport,
            device_scale_factor=2,
        )
        try:
            cdp_session = page.context.new_cdp_session(page)
        except Exception:
            cdp_session = None
        try:
            page.goto(payload["job_url"], wait_until="domcontentloaded")
            reveal_application_form(page)
            pending_steps = list(payload["steps"])
            page_index = 1
            max_pages = 12

            while True:
                filled_on_page: list[str] = []
                for step in list(pending_steps):
                    if not step_is_available(page, step):
                        continue
                    # Skip GDPR consent checkbox steps — they are handled
                    # by check_gdpr_consent_checkboxes() after demographics
                    # are filled (checking consent before demographics can
                    # lock the demographic fields).
                    if str(step.get("field_name", "")).startswith("data_compliance_gdpr"):
                        step["filled"] = True
                        step["page_index"] = page_index
                        filled_on_page.append(step["field_name"])
                        pending_steps.remove(step)
                        continue
                    if step["kind"] in {"text", "textarea"}:
                        fill_text(page, step)
                    elif step["kind"] == "combobox":
                        fill_combobox(page, step)
                    elif step["kind"] == "file":
                        upload_file(page, step)
                    elif step["kind"] == "checkbox":
                        # Find and check the checkbox matching the desired option label
                        desired_option = _normalize_free_text(step.get("option", ""))
                        field_name = step["field_name"]
                        checkboxes = page.locator(
                            f'input[type="checkbox"][name="{field_name}[]"], input[type="checkbox"][name="{field_name}"]'
                        )
                        checked = False
                        for ci in range(checkboxes.count()):
                            cb = checkboxes.nth(ci)
                            cb_label = ""
                            try:
                                label_el = cb.locator("xpath=ancestor::label | following-sibling::label | ../label")
                                if label_el.count() > 0:
                                    cb_label = _normalize_free_text(label_el.first.inner_text())
                            except PlaywrightError:
                                pass
                            if not cb_label:
                                try:
                                    cb_id = cb.get_attribute("id") or ""
                                    if cb_id:
                                        page_label = page.locator(f'label[for="{cb_id}"]')
                                        if page_label.count() > 0:
                                            cb_label = _normalize_free_text(page_label.first.inner_text())
                                except PlaywrightError:
                                    pass
                            if desired_option and cb_label == desired_option:
                                cb.scroll_into_view_if_needed()
                                checked = _ensure_checkbox_checked(page, cb)
                                break
                        if not checked:
                            # Fallback: multi_value_multi_select fields may
                            # render as React Select comboboxes instead of
                            # checkbox inputs.
                            cb_locator = resolve_locator(page, step, raise_if_missing=False)
                            if cb_locator is not None:
                                try:
                                    role = (cb_locator.get_attribute("role") or "").strip().casefold()
                                    aria_auto = (cb_locator.get_attribute("aria-autocomplete") or "").strip().casefold()
                                except PlaywrightError:
                                    role = ""
                                    aria_auto = ""
                                if role == "combobox" or aria_auto == "list":
                                    fill_combobox(page, step)
                                    checked = True
                        if not checked:
                            raise RuntimeError(
                                f"Could not find checkbox option {step.get('option')!r} for {field_name}"
                            )
                    else:
                        raise RuntimeError(f"Unsupported step kind: {step['kind']}")
                    if step["kind"] != "file":
                        if not manual_text_step_is_confirmed(page, step):
                            raise RuntimeError(
                                f"Greenhouse field {step['field_name']} was present but could not be confirmed after fill."
                            )
                        step["filled"] = True
                        step["page_index"] = page_index
                        filled_on_page.append(step["field_name"])
                    pending_steps.remove(step)
                    page.wait_for_timeout(120)

                fill_discovered_profile_questions(page, page_index=page_index)
                fill_security_code_if_present(page, page_index=page_index)
                refill_visible_deterministic_steps(page, page_index=page_index)
                sync_runtime_confirmations(page, page_index=page_index, settle_ms=500)
                filled_on_page.extend(
                    [
                        step["field_name"]
                        for step in runtime["extra_report_steps"]
                        if step.get("page_index") == page_index and step.get("filled")
                    ]
                )
                filled_on_page.extend(confirm_file_upload_steps(page, page_index=page_index))
                filled_on_page = list(dict.fromkeys(filled_on_page))
                [step for step in pending_steps if step_is_available(page, step)]
                remaining_required_steps = [step for step in pending_steps if not step.get("optional")]
                visible_required_pending = [step for step in remaining_required_steps if step_is_available(page, step)]

                unknown_questions = visible_unknown_questions(page)
                if unknown_questions:
                    runtime["unknown_questions"] = unknown_questions
                    write_unknown_questions(page_index, unknown_questions)
                    _write_autofill_report(payload, runtime)
                    write_greenhouse_failed_result(
                        out_dir,
                        payload,
                        failure_type=GREENHOUSE_UNKNOWN_QUESTIONS_FAILURE,
                        message="Greenhouse encountered required questions that do not have answers in the payload.",
                        current_page="in_progress",
                        page_index=page_index,
                        unknown_questions=unknown_questions,
                    )
                    raise RuntimeError(
                        "Encountered required application questions that do not have answers in the payload. "
                        f"See {artifacts['unknown_questions_json']} for details."
                    )
                runtime["unknown_questions"] = []
                write_unknown_questions(page_index, [])

                capture_page_screenshot(page, page_index=page_index, field_names=filled_on_page)

                submit_locator = submit_button_locator(page)
                next_locator = next_button_locator(page)

                if submit_locator is not None and next_locator is None:
                    if remaining_required_steps:
                        for _ in range(4):
                            page.wait_for_timeout(250)
                            visible_required_pending = [
                                step for step in remaining_required_steps if step_is_available(page, step)
                            ]
                            if visible_required_pending:
                                break
                        if visible_required_pending:
                            continue
                        report_payload = refresh_review_artifacts(page)
                        record_unconfirmed_fields(report_payload)
                        pending = ", ".join(step["field_name"] for step in remaining_required_steps)
                        write_greenhouse_failed_result(
                            out_dir,
                            payload,
                            failure_type=GREENHOUSE_REVIEW_PROOF_GAP_FAILURE,
                            message="Greenhouse reached the final review state before all planned fields were confirmed in the DOM.",
                            current_page="review",
                            page_index=page_index,
                            validation_errors=[step["field_name"] for step in remaining_required_steps],
                        )
                        raise RuntimeError(
                            "The final application page exposed a submit button before all planned fields were "
                            f"confirmed in the DOM. Unconfirmed fields: {pending}"
                        )
                    break

                if not remaining_required_steps:
                    if next_locator is None:
                        break
                    if page_index >= max_pages:
                        raise RuntimeError("Reached the maximum supported page count while navigating the application.")
                    advance_to_next_page(page, page_index=page_index)
                    page_index += 1
                    continue

                if submit_locator is not None and next_locator is None and visible_required_pending:
                    continue

                if page_index >= max_pages:
                    pending = ", ".join(step["field_name"] for step in remaining_required_steps)
                    raise RuntimeError(f"Reached the maximum supported page count with unresolved fields: {pending}")
                advance_to_next_page(page, page_index=page_index)
                page_index += 1

            sync_runtime_confirmations(page, page_index=page_index, settle_ms=2000)
            report_payload = refresh_review_artifacts(page)
            pending_path = record_unconfirmed_fields(report_payload)

            if submit:
                if pending_path is not None:
                    write_greenhouse_failed_result(
                        out_dir,
                        payload,
                        failure_type=GREENHOUSE_REVIEW_PROOF_GAP_FAILURE,
                        message="Greenhouse autofill left one or more planned fields unconfirmed before submit.",
                        current_page="review",
                        page_index=page_index,
                        validation_errors=[
                            field.get("field_name") or field.get("label") or ""
                            for field in blocking_unconfirmed_report_entries(
                                list(report_payload.get("planned_but_unconfirmed_fields") or [])
                            )
                        ],
                    )
                    raise RuntimeError(
                        "Greenhouse autofill left one or more planned fields unconfirmed. "
                        f"Review {pending_path.relative_to(PROJECT_ROOT)} and rerun after fixing every field."
                    )
                submit_locator = submit_button_locator(page)
                if submit_locator is None:
                    write_greenhouse_failed_result(
                        out_dir,
                        payload,
                        failure_type=GREENHOUSE_SUBMIT_NAVIGATION_MISSING_FAILURE,
                        message="Could not find a visible submit button on the final Greenhouse application page.",
                        current_page="review",
                        page_index=page_index,
                    )
                    raise RuntimeError("Could not find a visible submit button on the final application page.")
                submit_started_at_utc = datetime.now(UTC).replace(microsecond=0).isoformat()
                email_watcher = build_email_confirmation_watcher(
                    payload,
                    min_received_at_utc=submit_started_at_utc,
                )
                submit_locator.click()
                outcome = wait_for_submission_outcome(page, timeout_ms=8000, email_watcher=email_watcher)
                if outcome["status"] in {"security_code_required", "validation_error"}:
                    before_extra_steps = len(runtime["extra_report_steps"])
                    fill_security_code_if_present(
                        page,
                        page_index=page_index,
                        min_received_at_utc=submit_started_at_utc,
                    )
                    if len(runtime["extra_report_steps"]) == before_extra_steps:
                        if outcome["status"] == "security_code_required":
                            capture_submit_debug_artifacts(page)
                            write_greenhouse_failed_result(
                                out_dir,
                                payload,
                                failure_type=GREENHOUSE_SECURITY_CODE_UNRESOLVED_FAILURE,
                                message="Greenhouse requested a security code, but autofill could not populate it.",
                                current_page="review",
                                page_index=page_index,
                            )
                            raise RuntimeError(
                                "Greenhouse requested a security code, but autofill could not populate it. "
                                f"Debug artifacts: {artifacts.get('submit_debug_html')}, {artifacts.get('submit_debug_screenshot')}"
                            )
                    else:
                        report_payload = refresh_review_artifacts(page)
                        pending_path = record_unconfirmed_fields(report_payload)
                        if pending_path is not None:
                            capture_submit_debug_artifacts(page)
                            write_greenhouse_failed_result(
                                out_dir,
                                payload,
                                failure_type=GREENHOUSE_REVIEW_PROOF_GAP_FAILURE,
                                message="Greenhouse still had planned-but-unconfirmed fields after the security code step.",
                                current_page="review",
                                page_index=page_index,
                                validation_errors=[
                                    field.get("field_name") or field.get("label") or ""
                                    for field in blocking_unconfirmed_report_entries(
                                        list(report_payload.get("planned_but_unconfirmed_fields") or [])
                                    )
                                ],
                            )
                            raise RuntimeError(
                                "Greenhouse autofill still had planned-but-unconfirmed fields after the security "
                                f"code step. Review {pending_path.relative_to(PROJECT_ROOT)} before retrying submit."
                            )
                        submit_locator = submit_button_locator(page)
                        if submit_locator is None:
                            capture_submit_debug_artifacts(page)
                            write_greenhouse_failed_result(
                                out_dir,
                                payload,
                                failure_type=GREENHOUSE_SUBMIT_NAVIGATION_MISSING_FAILURE,
                                message="Could not find a visible submit button after entering the Greenhouse security code.",
                                current_page="review",
                                page_index=page_index,
                            )
                            raise RuntimeError(
                                "Could not find a visible submit button after entering the security code. "
                                f"Debug artifacts: {artifacts.get('submit_debug_html')}, {artifacts.get('submit_debug_screenshot')}"
                            )
                        page.wait_for_timeout(300)
                        submit_locator.click()
                        outcome = wait_for_submission_outcome(
                            page,
                            timeout_ms=15000,
                            email_watcher=email_watcher,
                            return_on_security_code_required=False,
                        )

                if outcome["status"] != "confirmed":
                    page.wait_for_timeout(500)
                    try:
                        refreshed_snapshot = read_submission_snapshot(page)
                    except PlaywrightError:
                        refreshed_snapshot = None
                    if refreshed_snapshot:
                        refreshed_state = _classify_submission_snapshot(refreshed_snapshot)
                        if refreshed_state["status"] == "confirmed":
                            refreshed_state["snapshot"] = refreshed_snapshot
                            outcome = refreshed_state

                if outcome["status"] == "confirmed":
                    if outcome.get("reason") == "email_confirmation":
                        print(
                            "Detected a matching application confirmation email while waiting for Greenhouse browser confirmation.",
                            file=sys.stderr,
                        )
                    print("Application submitted.")
                    try:
                        notion_sync_result = _sync_notion_after_submit(
                            payload,
                            outcome,
                            email_confirmation=outcome.get("email_confirmation"),
                            min_received_at_utc=submit_started_at_utc,
                        )
                    except Exception as exc:
                        print(
                            "WARNING: The application was submitted, but post-apply Notion sync failed. "
                            f"Rerun `python3 scripts/notion_sync.py {out_dir}` after fixing the issue. "
                            f"Details: {exc}",
                            file=sys.stderr,
                        )
                    else:
                        sync_status = notion_sync_result.get("status")
                        if sync_status == "synced":
                            destination = notion_sync_result.get("page_url") or notion_sync_result.get("page_id")
                            print(f"Notion synced: {destination}")
                        elif sync_status == "pending_email_confirmation":
                            print(
                                "Notion sync is waiting for the confirmation email. "
                                f"Rerun `python3 scripts/notion_sync.py {out_dir}` once the email arrives."
                            )
                        elif sync_status == "missing_notion_token":
                            print(
                                "Notion sync is pending because no Notion API token is configured. "
                                f"Set NOTION_API_TOKEN and rerun `python3 scripts/notion_sync.py {out_dir}`.",
                                file=sys.stderr,
                            )
                        elif sync_status != "synced":
                            print(
                                "Notion sync did not complete automatically. "
                                f"Check {artifacts.get('notion_sync_status_json')} and rerun "
                                f"`python3 scripts/notion_sync.py {out_dir}` if needed.",
                                file=sys.stderr,
                            )
                else:
                    refresh_review_artifacts(page)
                    capture_submit_debug_artifacts(page)
                    errors = outcome.get("errors") or []
                    invalid_fields = outcome.get("invalid_fields") or []
                    snapshot = outcome.get("snapshot") or {}
                    excerpt = str(snapshot.get("page_text") or "").strip().replace("\n", " ")
                    if len(excerpt) > 280:
                        excerpt = excerpt[:277] + "..."
                    details: list[str] = []
                    if errors:
                        details.append("errors: " + "; ".join(str(error) for error in errors))
                    if invalid_fields:
                        details.append("invalid fields: " + ", ".join(str(field) for field in invalid_fields))
                    if snapshot.get("url"):
                        details.append(f"url: {snapshot['url']}")
                    if excerpt:
                        details.append(f"page excerpt: {excerpt}")
                    if artifacts.get("submit_debug_html"):
                        details.append(f"debug html: {artifacts['submit_debug_html']}")
                    if artifacts.get("submit_debug_screenshot"):
                        details.append(f"debug screenshot: {artifacts['submit_debug_screenshot']}")
                    status = outcome.get("status", "unknown")
                    failure_type = (
                        GREENHOUSE_SUBMIT_VALIDATION_FAILURE
                        if status == "validation_error"
                        else GREENHOUSE_SUBMIT_NOT_CONFIRMED_FAILURE
                    )
                    validation_errors = [str(error) for error in errors] + [str(field) for field in invalid_fields]
                    write_greenhouse_failed_result(
                        out_dir,
                        payload,
                        failure_type=failure_type,
                        message=f"Greenhouse submission did not reach a confirmed completion state ({status}).",
                        current_page="review",
                        page_index=page_index,
                        validation_errors=validation_errors,
                    )
                    suffix = f" Details: {' | '.join(details)}" if details else ""
                    raise RuntimeError(
                        f"Greenhouse submission did not reach a confirmed completion state ({status}).{suffix}"
                    )
            else:
                print("Application filled. Review-before-submit mode is active.")
                if browser.session_viewer_url:
                    print(f"Greenhouse Steel session viewer: {browser.session_viewer_url}")
                if not headless and sys.stdin.isatty():
                    input("Press Enter to close the browser. ")
        except Exception as exc:
            result_path = greenhouse_submission_result_path(out_dir, payload)
            if not result_path.exists() and not role_submit_path(out_dir, JOB_UNAVAILABLE_JSON).exists():
                try:
                    capture_submit_debug_artifacts(page)
                except Exception:
                    pass
                write_greenhouse_failed_result(
                    out_dir,
                    payload,
                    failure_type=GREENHOUSE_RUNTIME_FAILURE,
                    message=str(exc),
                    current_page="review" if submit_button_locator(page) is not None else "in_progress",
                    page_index=page_index if "page_index" in locals() else None,
                )
            raise
        finally:
            browser.close()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Autofill a Greenhouse application using existing job assets.")
    parser.add_argument(
        "target",
        help="Output directory (e.g. output/figma/pm-design-tools) or a job URL already present in .pipeline_meta.json.",
    )
    parser.add_argument(
        "--payload-only",
        action="store_true",
        help="Only generate greenhouse_autofill_payload.json. Do not launch the browser automation.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Launch the Playwright runtime in headless mode.",
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Submit the application after autofill. Default behavior stops for review.",
    )
    parser.add_argument(
        "--browser-provider",
        choices=("local", "steel"),
        default=None,
        help="Browser runtime to use for submit automation (default: env or local).",
    )
    parser.add_argument(
        "--provider",
        choices=VALID_PROVIDERS,
        default=None,
        help="Provider to use for generated application answers. Defaults to ASSET_LLM_PROVIDER; otherwise the active provider (openai when unset), with CLI fallback if unavailable.",
    )
    args = parser.parse_args()

    out_dir = _find_output_dir(args.target)
    if args.browser_provider:
        os.environ["JOB_ASSETS_BROWSER_PROVIDER"] = args.browser_provider
    if args.provider:
        os.environ["ASSET_LLM_PROVIDER"] = args.provider
    payload = _build_payload(out_dir)
    _write_autofill_report(payload)
    payload_path = role_submit_path(out_dir, "greenhouse_autofill_payload.json")
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {payload_path.relative_to(PROJECT_ROOT)}")

    if args.payload_only:
        return 0

    return _run_playwright(payload_path, headless=args.headless, submit=args.submit)


if __name__ == "__main__":
    raise SystemExit(main())
