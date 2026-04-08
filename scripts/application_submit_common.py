#!/usr/bin/env python3
"""Shared helpers for browser-based application submitters."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from base64 import urlsafe_b64encode
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse, urlunparse
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import json_lenient
from answer_generation_support import (
    _augment_answer_generation_prompt_with_verifier_feedback,
    _build_answer_verification_source_bundle,
    _linked_resource_answer_payload,
    _linked_resource_deterministic_answers,
    _linked_resource_failure_blockers,
    _verification_retry_feedback_by_field,
    _verifier_retry_feedback_blockers,
    _write_pending_linked_resource_blockers,
    default_answer_provider,
)
from answer_refresh_state import current_answer_refresh_request_id
from answer_verifier import verify_generated_answers
from application_models import (
    ApplicationProfile,
    CandidateProfile,
    SharedQuestionPolicy,
    _bool_to_text,
    _normalize_compensation_expectations,
    _normalized_education_entries,
    normalize_url,
    parse_application_profile,
    parse_bool,
    parse_master_resume,
)
from generated_answer_validation import (
    _generated_answer_blocker_step,
    _is_conditional_followup,
    validate_generated_answers,
    validate_generated_answers_with_blockers,
)
from job_board_urls import looks_like_non_html_asset_url, looks_like_unresolved_url_template
from linked_resource_context import clear_linked_resource_artifacts, prepare_linked_resource_context
from llm_provider import (
    automation_provider_chain,
    provider_command_for_mode,
    provider_timeout_seconds,
)
from output_layout import (
    ANSWER_VERIFICATION_JSON as OUTPUT_ANSWER_VERIFICATION_JSON,
)
from output_layout import (
    ANSWER_VERIFICATION_RAW as OUTPUT_ANSWER_VERIFICATION_RAW,
)
from output_layout import (
    APPLICATION_ANSWER_CACHE as OUTPUT_APPLICATION_ANSWER_CACHE,
)
from output_layout import (
    APPLICATION_ANSWER_FALLBACK_RAW as OUTPUT_APPLICATION_ANSWER_FALLBACK_RAW,
)
from output_layout import (
    APPLICATION_ANSWER_RAW as OUTPUT_APPLICATION_ANSWER_RAW,
)
from output_layout import (
    CONFIRMATION_EMAIL_REPLY_JSON,
    existing_submit_dirs,
    find_role_file,
    glob_role_files,
    migrate_role_output_layout,
    preferred_submit_dir_name_for_post_submit,
    role_content_path,
    role_submit_dir,
    role_submit_path,
)
from pipeline_meta_common import load_pipeline_meta
from submit_review_common import (  # noqa: F401
    PENDING_USER_INPUT_JSON,
    clear_pending_user_input,
    load_pending_user_input_for_submit_attempt,
    pending_user_input_questions_for_unconfirmed_fields,
    resolve_current_submit_artifacts,
    resolve_submit_artifact_path,
    write_pending_user_input,
    write_pending_user_input_for_unconfirmed_fields,
)
from text_normalization_helpers import (
    normalize_multi_select_generated_answers,
    normalize_text,
    slugify_label,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
JOBS_DB_PATH = PROJECT_ROOT / "jobs.db"
MASTER_RESUME_PATH = PROJECT_ROOT / "master_resume.md"
WORK_STORIES_PATH = PROJECT_ROOT / "work_stories.md"
CANDIDATE_CONTEXT_PATH = PROJECT_ROOT / "candidate_context.md"
APPLICATION_PROFILE_PATH = PROJECT_ROOT / "application_profile.md"
APPLICATION_ANSWER_CACHE = OUTPUT_APPLICATION_ANSWER_CACHE
APPLICATION_ANSWER_RAW = OUTPUT_APPLICATION_ANSWER_RAW
APPLICATION_ANSWER_FALLBACK_RAW = OUTPUT_APPLICATION_ANSWER_FALLBACK_RAW
ANSWER_VERIFICATION_JSON = OUTPUT_ANSWER_VERIFICATION_JSON
ANSWER_VERIFICATION_RAW = OUTPUT_ANSWER_VERIFICATION_RAW
__all__ = [
    "ApplicationProfile",
    "CandidateProfile",
    "SharedQuestionPolicy",
    "_generated_answer_blocker_step",
    "_normalize_compensation_expectations",
    "build_how_did_you_hear_candidates",
    "build_how_did_you_hear_option_candidates",
    "current_professional_license_inventory_answer",
    "append_url_path_suffix",
    "question_requests_current_professional_license_inventory",
    "resolve_how_did_you_hear_candidates",
    "resolve_how_did_you_hear_option_candidates",
    "normalize_url",
    "parse_application_profile",
    "parse_bool",
    "parse_master_resume",
    "select_truthful_age_option",
]
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
SIGNOFF_PREFIXES = ("best regards", "regards", "sincerely", "thank you")
HTML_TAG_RE = re.compile(r"<[^>]+>")
LINKED_RESOURCE_URL_RE = re.compile(r"https?://[^\s<>\")]+", re.I)
APPLICATION_ANSWER_EM_DASH_GUIDANCE = (
    "For application answers, avoid the Unicode em dash character when possible. "
    "Prefer commas, periods, parentheses, or hyphens instead. "
    "Keep em dashes only in direct quotes or fixed text that must stay verbatim."
)
GMAIL_INLINE_SEND_MAX_JSON_BYTES = 900_000
DEFAULT_SUBMIT_EMAIL_POLL_INTERVAL_SECONDS = 15
CURRENT_COMPANY_FIELD_NAMES = {
    "current_company",
    "current_company_name",
    "current_employer",
    "current_employer_name",
    "org",
}
CURRENT_COMPANY_LABEL_FRAGMENTS = (
    "current company",
    "current company name",
    "current employer",
    "current employer name",
)
SALARY_COMFORT_TEXT_FRAGMENTS = (
    "comfortable with posted salary",
    "comfortable with the posted salary",
    "comfortable interviewing for the salary outlined in the job description",
    "comfortable interviewing for the salary outlined",
    "comfortable with the salary outlined in the job description",
    "comfortable with the salary range",
    "comfortable with the compensation range",
    "comfortable with the compensation band",
    "comfortable with the salary band",
)
SALARY_REQUIREMENT_CONFIRMATION_TEXT_FRAGMENTS = (
    "meet your compensation requirements",
    "meets your compensation requirements",
    "meet your salary requirements",
    "meets your salary requirements",
)
# Short salary fragments used ONLY as a first-pass filter — the question must
# ALSO contain a comfort/interview keyword to qualify as a comfort check.
# Without this two-step gate, open-ended questions like "What is your desired
# salary range?" falsely match and get answered "Yes" instead of the real salary.
_SALARY_KEYWORD_FRAGMENTS = (
    "salary outlined",
    "salary range",
    "compensation range",
    "compensation band",
    "pay range",
    "compensation outlined",
)
PENDING_USER_INPUT_TEXT_FRAGMENTS = (
    "homeowners",
    "property line of business",
    "admitted vs",
    "admitted vs e s",
    "market context",
    "decision scope",
    "carrier constraints",
    "carrier",
    "regulatory",
    "actuarial",
    "peril",
)
_OUTSIDE_COMMITMENT_PENDING_INPUT_FRAGMENTS = (
    "side business",
    "side businesses",
    "board position",
    "board positions",
    "nonprofit role",
    "nonprofit roles",
    "academic commitment",
    "academic commitments",
    "obligations that you anticipate continuing",
)
_FINANCIAL_PRODUCT_DISCLOSURE_PENDING_INPUT_FRAGMENTS = (
    "lending financing or credit products",
    "lending or credit products",
    "credit products",
)


class GeneratedAnswerBlockersError(ValueError):
    """Raised when required generated-answer questions resolved to explicit blockers."""

    def __init__(self, blockers: list[dict], *, valid_answers: dict[str, object] | None = None):
        self.blockers = blockers
        self.valid_answers = dict(valid_answers or {})
        labels = ", ".join(str(blocker.get("label") or blocker.get("field_name") or "").strip() for blocker in blockers)
        super().__init__(f"Generated answers require review for: {labels}")


POSITIVE_FIT_CATEGORIES = frozenset(
    {
        "minimum_experience",
        "experience_confirmation",
        "product_usage",
        "office_attendance",
        "role_commitment",
        "relocation_willingness",
        "travel_willingness",
        "location_residency",
    }
)


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


def primary_employer_name(master_resume_text: str | None = None) -> str:
    text = master_resume_text
    if text is None:
        text = MASTER_RESUME_PATH.read_text(encoding="utf-8")
    match = re.search(r"^\s*(?:##\s+)?(.+?)\s+—", text, re.M)
    if not match:
        raise ValueError("Could not determine the primary employer from master_resume.md")
    employer_name = match.group(1).strip()
    if employer_name.isupper():
        return _title_case_phrase(employer_name)
    return employer_name


def append_url_path_suffix(url: str, suffix: str) -> str:
    """Append a path suffix while preserving any existing query or fragment."""
    if not url:
        return url
    parsed = urlparse(url)
    normalized_suffix = suffix if suffix.startswith("/") else f"/{suffix}"
    target = normalized_suffix.rstrip("/") or "/"
    path = parsed.path.rstrip("/")
    if not path.endswith(target):
        path = f"{path}{normalized_suffix}"
    elif normalized_suffix.endswith("/") and not path.endswith("/"):
        path = f"{path}/"
    return urlunparse(parsed._replace(path=path))


def _education_supports_credential_claim(text: str, application_profile: ApplicationProfile) -> bool:
    normalized = normalize_text(text)
    education_entries = _normalized_education_entries(application_profile)
    if not education_entries:
        return False

    def has_entry(*fragments: str) -> bool:
        return any(any(fragment in entry for fragment in fragments) for entry in education_entries)

    if any(fragment in normalized for fragment in ("bachelor", "undergraduate")):
        return has_entry("bachelor", "b s", "b a", "b sc", "bachelor of")
    if any(fragment in normalized for fragment in ("master", "mba", "graduate degree")):
        return has_entry("master", "m b a", "mba", "m s", "m a", "master of")
    if any(fragment in normalized for fragment in ("phd", "doctorate", "doctoral")):
        return has_entry("phd", "doctorate", "doctoral", "doctor of")
    if any(fragment in normalized for fragment in ("graduate from", "graduated from")) and any(
        fragment in normalized for fragment in ("university", "college", "4 year", "four year")
    ):
        return bool(education_entries)
    return any(fragment in normalized for fragment in ("degree", "diploma", "education"))


def _master_resume_credential_lines(master_resume_text: str | None = None) -> list[str]:
    text = master_resume_text
    if text is None:
        if not MASTER_RESUME_PATH.exists():
            return []
        text = MASTER_RESUME_PATH.read_text(encoding="utf-8")

    credentials: list[str] = []
    in_credential_section = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        inline_match = re.match(r"^(certifications?|licenses?)\s*:\s*(.+)$", stripped, re.I)
        if inline_match:
            inline_values = [part.strip() for part in re.split(r"\s*;\s*", inline_match.group(2)) if part.strip()]
            credentials.extend(inline_values or [inline_match.group(2).strip()])
            continue
        if stripped.startswith("##"):
            heading = normalize_text(stripped[2:].strip())
            in_credential_section = any(token in heading for token in ("certification", "license"))
            continue
        if in_credential_section:
            if stripped.startswith("- "):
                credentials.append(stripped[2:].strip())
            else:
                credentials.append(stripped)
    return credentials


def _resume_supports_credential_claim(text: str, master_resume_text: str | None = None) -> bool:
    normalized = normalize_text(text)
    credentials = [normalize_text(line) for line in _master_resume_credential_lines(master_resume_text)]
    if not credentials:
        return False

    if any(fragment in normalized for fragment in ("certification", "certifications", "certificate", "certified")):
        return True

    series_matches = re.findall(r"series\s+\d+", normalized)
    if series_matches:
        return any(series in credential for credential in credentials for series in series_matches)

    if "finra" in normalized:
        return any("finra" in credential or "series " in credential for credential in credentials)

    if "license" in normalized or "licence" in normalized or "licensed" in normalized:
        return any(
            token in credential for credential in credentials for token in ("license", "licence", "licensed", "series ")
        )

    return False


def _credential_support_source(
    text: str,
    application_profile: ApplicationProfile,
    master_resume_text: str | None = None,
) -> str | None:
    if _education_supports_credential_claim(text, application_profile):
        return "application_profile.md"
    if _resume_supports_credential_claim(text, master_resume_text):
        return "master_resume.md"
    return None


def question_requests_current_professional_license_inventory(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if any(fragment in normalized for fragment in _CREDENTIAL_CLAIM_EXCLUDE_FRAGMENTS):
        return False
    if "intend to hold" in normalized:
        return False
    if not any(
        token in normalized
        for token in (
            "license",
            "licenses",
            "licence",
            "licences",
            "certification",
            "certifications",
            "certificate",
            "certified",
            "finra",
            "series ",
        )
    ):
        return False
    asks_inventory = normalized.startswith(("what ", "which ", "list ", "please list", "provide "))
    return asks_inventory and (
        "currently hold" in normalized or "current license" in normalized or "current certification" in normalized
    )


def current_professional_license_inventory_answer(
    text: str | None,
    *,
    master_resume_text: str | None = None,
) -> str | None:
    if not question_requests_current_professional_license_inventory(text):
        return None
    if _resume_supports_credential_claim(text or "", master_resume_text):
        credentials = [line.strip() for line in _master_resume_credential_lines(master_resume_text) if line.strip()]
        if credentials:
            return "; ".join(credentials)
    return "None"


def question_requests_product_analysis_tools_inventory(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    return "product analysis" in normalized and any(
        fragment in normalized
        for fragment in (
            "analytical tools",
            "tools or languages",
            "tools or languages have you used",
        )
    )


def _english_list(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def product_analysis_tools_inventory_answer(
    text: str | None,
    *,
    master_resume_text: str | None = None,
) -> str | None:
    if not question_requests_product_analysis_tools_inventory(text):
        return None
    resume_text = master_resume_text
    if resume_text is None:
        if not MASTER_RESUME_PATH.exists():
            return None
        resume_text = MASTER_RESUME_PATH.read_text(encoding="utf-8")
    normalized_resume = normalize_text(resume_text)
    items: list[str] = []
    if "python" in normalized_resume:
        items.append("Python")
    if re.search(r"\bsql\b", resume_text, re.I):
        items.append("SQL")
    if "a/b testing platform" in normalized_resume or "a b testing platform" in normalized_resume:
        items.append("an in-house A/B testing platform")
    if "analytics dashboard" in normalized_resume or "analytics dashboards" in normalized_resume:
        items.append("analytics dashboards")
    if "session recordings" in normalized_resume:
        items.append("session recordings")
    if "support ticket analysis" in normalized_resume:
        items.append("support ticket analysis")
    if "user interviews" in normalized_resume:
        items.append("user interviews")
    if not items:
        return None
    return f"I have used {_english_list(items)} for product analysis."


def question_requests_customer_experience_helpdesk_tools(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    return "helpdesk tools" in normalized and any(
        fragment in normalized
        for fragment in (
            "customer experience",
            "customer support",
            "implemented or integrated",
        )
    )


def customer_experience_helpdesk_tools_answer(
    text: str | None,
    *,
    master_resume_text: str | None = None,
) -> str | None:
    if not question_requests_customer_experience_helpdesk_tools(text):
        return None
    resume_text = master_resume_text
    if resume_text is None:
        if not MASTER_RESUME_PATH.exists():
            return None
        resume_text = MASTER_RESUME_PATH.read_text(encoding="utf-8")
    normalized_resume = normalize_text(resume_text)
    examples: list[str] = []
    if "chatbot" in normalized_resume:
        examples.append("improving an AI chatbot at Moody's")
    if "automated chat workflows" in normalized_resume:
        examples.append("implementing automated chat workflows at Kyte")
    if "partner onboarding portal" in normalized_resume:
        examples.append("building a self-service onboarding portal at T-Mobile")
    if not examples:
        return None
    return (
        "I have not implemented a named enterprise helpdesk platform. "
        f"My closest relevant work has been {_english_list(examples)}."
    )


def question_requests_global_teams_experience(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if not any(
        fragment in normalized
        for fragment in (
            "global teams",
            "global team",
            "international teams",
            "international team",
            "distributed teams",
            "distributed team",
            "cross cultural teams",
            "cross cultural team",
            "multicultural teams",
            "multicultural team",
        )
    ):
        return False
    return any(
        fragment in normalized
        for fragment in (
            "experience",
            "worked with",
            "working with",
            "worked across",
            "working across",
            "collaborat",
            "elaborate",
        )
    )


def global_teams_experience_answer(
    text: str | None,
    *,
    master_resume_text: str | None = None,
    candidate_context_text: str | None = None,
) -> str | None:
    if not question_requests_global_teams_experience(text):
        return None

    resume_text = _load_master_resume_text(master_resume_text)
    context_text = candidate_context_text
    if context_text is None:
        try:
            context_text = CANDIDATE_CONTEXT_PATH.read_text(encoding="utf-8")
        except OSError:
            context_text = ""

    normalized_resume = normalize_text(resume_text)
    normalized_context = normalize_text(context_text)
    language_sequence = _candidate_context_language_sequence(context_text)
    if not language_sequence:
        language_sequence = list(_resume_language_proficiencies(resume_text))

    supported_fragments: list[str] = [
        "I would describe my experience as strong cross-cultural collaboration rather than formal ownership of a globally distributed internal team.",
    ]
    if "panama" in normalized_context and "different cultures" in normalized_context:
        supported_fragments.append("I was born and raised in Panama and have lived in different cultures.")
    elif "different cultures" in normalized_context:
        supported_fragments.append("I have lived in different cultures.")
    if language_sequence:
        language_names = [_display_language_name(language) for language in language_sequence]
        supported_fragments.append(f"I speak {_english_list(language_names)}.")
    if all(employer in normalized_resume for employer in ("moody s", "kyte", "t mobile")):
        supported_fragments.append(
            "Across Moody's, Kyte, and T-Mobile, I have regularly partnered with engineering, analytics, sales, operations, and customer-facing teams."
        )
    supported_fragments.append(
        "That background helps me adapt communication clearly across different stakeholders and cultures."
    )
    return " ".join(fragment for fragment in supported_fragments if fragment)


def source_backed_domain_experience_answer(
    text: str | None,
    *,
    master_resume_text: str | None = None,
) -> str | None:
    normalized = normalize_text(text)
    if not normalized or "where have you worked on" not in normalized:
        return None

    resume_text = _load_master_resume_text(master_resume_text)
    normalized_resume = normalize_text(resume_text)

    if (
        "fraud" in normalized
        and "risk" in normalized
        and "compliance" in normalized
        and "case management" in normalized
        and "ml risk engine" in normalized_resume
        and "risk management function" in normalized_resume
    ):
        return (
            "My closest experience is at Kyte and Lyft. At Kyte, I built the company's first ML risk engine "
            "to reduce fraud, dispute, and accident losses in a consumer marketplace. At Lyft, I built driver "
            "risk models and helped establish the company's risk management function across cyber, tech E&O, "
            "and business risks. I also owned security and compliance requirements for T-Mobile's IoT "
            "connectivity platform."
        )

    if (
        "behavioral analytics" in normalized
        and "session recordings" in normalized_resume
        and "user interviews" in normalized_resume
        and "driver risk model" in normalized_resume
    ):
        return (
            "My closest experience is at Kyte and Lyft. At Kyte, I used session recordings, support ticket "
            "analysis, user interviews, and performance signals to shape the ML risk engine and matching "
            "roadmap. At Lyft, I built predictive risk and accident-prevention models from behavior and usage "
            "data. I would frame that as adjacent behavioral analytics experience rather than a standalone "
            "product literally named Behavioral Analytics."
        )

    return None


def relocation_willingness_text_answer(
    text: str | None,
    application_profile: ApplicationProfile | None,
) -> str | None:
    if application_profile is None or not question_is_relocation_willingness(text):
        return None
    location = str(getattr(application_profile, "location", "") or "").strip()
    if location:
        return f"Yes. I live in {location} and am open to relocation as needed."
    return "Yes. I am open to relocation as needed."


def question_requests_professional_license_intent(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if any(fragment in normalized for fragment in _CREDENTIAL_CLAIM_EXCLUDE_FRAGMENTS):
        return False
    if question_requests_current_professional_license_inventory(normalized):
        return False
    if not any(token in normalized for token in ("license", "licenses", "licence", "licences", "finra", "series ")):
        return False
    if "intend to hold" not in normalized:
        return False
    return normalized.startswith(("do you", "would you", "will you", "are you"))


def _resume_employer_names(master_resume_text: str | None = None) -> list[str]:
    text = master_resume_text
    if text is None:
        if not MASTER_RESUME_PATH.exists():
            return []
        text = MASTER_RESUME_PATH.read_text(encoding="utf-8")

    employers: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        match = re.match(r"^\s*(?:##\s+)?(.+?)\s+—", raw_line)
        if not match:
            continue
        employer_name = match.group(1).strip()
        key = employer_name.casefold()
        if key in seen:
            continue
        seen.add(key)
        employers.append(_title_case_phrase(employer_name) if employer_name.isupper() else employer_name)
    return employers


def format_resume_employer_history(master_resume_text: str | None = None) -> str | None:
    employers = _resume_employer_names(master_resume_text)
    if not employers:
        return None
    return ", ".join(employers)


def _strip_leading_article(value: str) -> str:
    text = value.strip()
    for prefix in ("a ", "an ", "the "):
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return text


def _clean_employment_subject(value: str) -> str:
    text = _strip_leading_article(normalize_text(value))
    if not text:
        return ""
    for fragment in (
        " in the past",
        " before",
        " previously",
        " or contracted to provide services to ",
        " as a consultant or independent contractor",
        " through an employment agency or placement firm of any kind",
        " through an employment agency or placement firm",
        " through an employment agency",
        " through a placement firm",
        " is our external financial auditor",
        " is our external auditor",
        " is our independent auditor",
        " is our independent registered public accounting firm",
        " who is our independent auditor",
        " who is our independent registered public accounting firm",
        " who is our auditor",
        " who serves as our independent auditor",
        " full time",
        " part time",
        " contractor",
        " intern capacity",
        " capacity",
    ):
        if fragment in text:
            text = text.split(fragment, 1)[0].strip()
    return text.strip()


def _employment_subject_candidates(text: str | None) -> list[str]:
    normalized = normalize_text(text)
    if not normalized:
        return []

    subjects: list[str] = []
    seen: set[str] = set()
    patterns = (
        r"are you currently a (?P<company>.+?) employee(?:\?|$)",
        r"are you a current (?P<company>.+?) employee(?:\?|$)",
        r"are you currently or have you ever been employed by (?P<company>.+?)$",
        r"are you now or have you ever been employed by (?P<company>.+?)$",
        r"are you currently or have you ever provided services to (?P<company>.+?)$",
        r"are you currently employed with or have been employed by (?P<company>.+?)$",
        r"are you currently employed by or have (?:you )?previously worked for (?P<company>.+?)$",
        r"are you currently employed by or have (?:you )?worked for (?P<company>.+?)$",
        r"currently working at (?P<company>.+?)(?: who is|$)",
        r"currently work for (?P<company>.+?)(?: or have you|$)",
        r"currently work at (?P<company>.+?)(?: or have you|$)",
        r"currently employed by (?P<company>.+?)$",
        r"have you previously been employed at (?P<company>.+?)(?: for any length of time| in the past| before|$)",
        r"have you previously been employed by (?P<company>.+?)(?: for any length of time| in the past| before|$)",
        r"have you been employed with (?P<company>.+?)(?: in the past| before|$)",
        r"have you ever been employed with (?P<company>.+?)(?: in the past| before|$)",
        r"have you (?:been )?employed by (?P<company>.+?)(?: in the past| before|$)",
        r"have you ever been employed by (?P<company>.+?)(?: in the past| before|$)",
        r"have you ever provided services to (?P<company>.+?)(?: in the past| before|$)",
        r"have you worked at or been a consultant for (?P<company>.+?)(?: or any company subsequently acquired by|$)",
        r"have you worked for or been a consultant for (?P<company>.+?)(?: or any company subsequently acquired by|$)",
        r"have you been a consultant for (?P<company>.+?)(?: in the past| before|$)",
        r"have you consulted for (?P<company>.+?)(?: in the past| before|$)",
        r"have worked in the past, as a contractor or consultant for (?P<company>.+?)(?:,|$)",
        r"have you (?:ever )?worked as\b.*\b(?:employee|contractor|consultant)\b.*\b(?:for|at) (?P<company>.+?)(?:\?|$)",
        r"have you worked at (?P<company>.+?)(?: in the past| before|$)",
        r"have you worked for (?P<company>.+?)(?: in the past| before|$)",
        r"have you ever worked at (?P<company>.+?)(?: in the past| before|$)",
        r"previously worked at (?P<company>.+?)(?: or if currently working at| who is|$)",
        r"previously worked for (?P<company>.+?)(?: or if currently working at| who is|$)",
        r"are you currently working for a (?P<company>.+?)(?: or is the dealership you are working for in process to implement .+|$)",
        r"working at (?P<company>.+?)(?: who is|$)",
        r"employed by (?P<company>.+?)(?: in the past| before|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if not match:
            continue
        raw_company = match.group("company")
        raw_parts = re.split(r"\s*(?:,|/|;|\band/or\b|\bor\b)\s*", raw_company, flags=re.I)
        for raw_part in raw_parts:
            cleaned = _clean_employment_subject(raw_part)
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            subjects.append(cleaned)
    return subjects


def _employment_subject_matches(employer_name: str, subject: str) -> bool:
    employer = normalize_text(employer_name)
    normalized_subject = _clean_employment_subject(subject)
    if not employer or not normalized_subject:
        return False
    if any(fragment in normalized_subject for fragment in (" and/or ", " or ", ",")):
        pieces = [
            piece.strip() for piece in re.split(r"\s*(?:,|and/or|or)\s*", normalized_subject) if piece and piece.strip()
        ]
        if pieces:
            return any(_employment_subject_matches(employer_name, piece) for piece in pieces)
    if normalized_subject in employer or employer in normalized_subject:
        return True
    employer_tokens = set(employer.split())
    subject_tokens = set(normalized_subject.split())
    return bool(subject_tokens) and subject_tokens.issubset(employer_tokens)


def _question_is_prior_employment_check(text: str | None) -> bool:
    if question_is_current_company_field(label=text):
        return False
    if question_requests_startup_experience(text):
        return False
    if _is_conditional_followup({"label": text or ""}):
        return False
    normalized = normalize_text(text)
    if not normalized:
        return False
    return bool(
        "previously worked at" in normalized
        or "previously worked for" in normalized
        or re.search(r"\bare you (?:a )?(?:previous|former)\b.*\bemployee\b", normalized)
        or re.search(r"\b(?:previous|former)\b.*\bemployee\b", normalized)
        or re.search(r"\bhave you previously been employed at\b", normalized)
        or re.search(r"\bhave you previously been employed by\b", normalized)
        or re.search(r"\bhave you been employed with\b", normalized)
        or re.search(r"\bhave you ever been employed with\b", normalized)
        or re.search(r"\bhave you (?:been )?employed by\b", normalized)
        or re.search(r"\bhave you ever been employed by\b", normalized)
        or re.search(r"\bhave you ever provided services to\b", normalized)
        or re.search(r"\bhave you (?:ever )?worked (?:at|for)\b", normalized)
        or re.search(
            r"\bhave you (?:ever )?worked as\b.*\b(?:employee|contractor|consultant)\b.*\b(?:at|for)\b", normalized
        )
        or re.search(r"\bhave you (?:ever )?(?:been a consultant for|consulted for)\b", normalized)
        or re.search(r"\bhave worked in the past\b.*\bas a contractor or consultant for\b", normalized)
        or re.search(r"\b(?:are|do) you currently\b.*\bor have (?:you )?worked (?:at|for)\b", normalized)
        or re.search(r"\b(?:are|do) you currently\b.*\bor have you previously worked (?:at|for)\b", normalized)
        or re.search(r"\b(?:are|do) you currently\b.*\bor have (?:you )?previously worked (?:at|for)\b", normalized)
        or re.search(r"\b(?:are|do) you currently\b.*\bor have you been employed by\b", normalized)
        or re.search(r"\b(?:are|do) you currently\b.*\bor have (?:you )?been employed by\b", normalized)
        or re.search(r"\b(?:are|do) you currently\b.*\bor have you ever been employed by\b", normalized)
        or re.search(r"\b(?:are|do) you currently\b.*\bor have you ever provided services to\b", normalized)
        or re.search(r"\b(?:are|do) you now\b.*\bor have you ever been employed by\b", normalized)
    )


def _question_is_current_employer_affiliation(text: str | None) -> bool:
    if question_is_current_company_field(label=text):
        return False
    normalized = normalize_text(text)
    if not normalized:
        return False
    if any(
        fragment in normalized
        for fragment in ("previously worked", "in the past", "have you been employed", "have you worked")
    ):
        return False
    if any(fragment in normalized for fragment in ("government official", "government agency")):
        return False
    return bool(
        re.search(r"\bare you currently employed by\b", normalized)
        or re.search(r"\bare you currently working for\b", normalized)
        or re.search(r"\bare you currently\b.*\bemployee\b", normalized)
        or re.search(r"\bare you a current\b.*\bemployee\b", normalized)
        or re.search(r"\bare you an employee of\b", normalized)
        or re.search(r"\bdo you currently work for\b", normalized)
        or re.search(r"\bdo you currently work at\b", normalized)
    )


def _question_is_employee_referral_confirmation(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if re.search(
        r"^(?:were you|have you been)\s+referred\s+(?:for|to)\s+(?:this|the)\s+(?:position|role|opportunity|job)\b",
        normalized,
    ):
        return True
    return bool(
        (
            re.search(r"^(?:were you|have you been)\s+referred by\b", normalized)
            or re.search(r"^(?:was|were)\s+this role referred by\b", normalized)
            or re.search(r"^(?:did|has)\b.*\bemployee\b.*\brefer you\b", normalized)
            or re.search(r"\bemployee\b.*\breferred you to the role\b", normalized)
        )
        and "employee" in normalized
    )


def _question_is_prior_application(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    return bool(
        re.search(r"\bhave you (?:ever )?previously applied to\b", normalized)
        or re.search(r"\bhave you ever applied to\b", normalized)
        or re.search(r"\bhave you applied to\b.*\bbefore\b", normalized)
        or re.search(r"\bhave you (?:ever )?applied for\b.*\bbefore\b", normalized)
        or re.search(r"\bhave you (?:ever )?applied for a role at\b", normalized)
    )


def _question_is_truthfulness_attestation(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if any(
        fragment in normalized
        for fragment in (
            "all statements made in this application are true and complete",
            "information provided is true and complete",
            "information i have provided is true and complete",
            "i hereby certify",
            "under penalty of perjury",
        )
    ):
        return True
    return any(token in normalized for token in ("certify", "attest", "confirm")) and any(
        fragment in normalized
        for fragment in ("true and complete", "accurate and complete", "true correct and complete")
    )


def _question_is_company_interview_history(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized or "interview" not in normalized:
        return False
    return bool(
        re.search(r"\bhave you (?:ever )?interviewed at\b", normalized)
        or re.search(r"\bhave you (?:ever )?interviewed with\b", normalized)
        or re.search(r"\bhave you interviewed\b.*\bbefore\b", normalized)
        or re.search(r"\bhave you previously interviewed\b", normalized)
    )


def _question_is_candidate_ai_guidance_attestation(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if "ai policy for application" in normalized:
        return True
    if not any(
        fragment in normalized
        for fragment in (
            "candidate ai guidance",
            "guidance on candidates ai usage",
            "ai partnership guidelines",
            "guidelines for candidates",
        )
    ):
        return False
    return any(
        fragment in normalized
        for fragment in (
            "confirm your understanding",
            "have read and agree",
            "have read and understood",
            "please indicate yes",
            "by selecting yes",
        )
    )


def _question_is_company_relationship_disclosure(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    return any(
        fragment in normalized
        for fragment in (
            "relatives or personal relationships working at",
            "family or personal connection with current or former employees",
            "family or personal connection with",
            "related to or in a relationship with anyone that works for",
            "related to, or in a relationship with, anyone that works for",
            "personal relationships working at",
        )
    )


def _question_is_referral_detail_disclosure(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if not any(
        fragment in normalized
        for fragment in (
            "referred by a current or former",
            "referred by a current employee",
            "referred by a former employee",
            "employee referral",
        )
    ):
        return False
    return any(
        fragment in normalized
        for fragment in (
            "if yes",
            "list their name",
            "list the name",
            "provide their name",
            "name below",
        )
    )


def _question_is_work_restriction_or_conflict_disclosure(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if "non-compete" in normalized or "non compete" in normalized or "noncompete" in normalized:
        return True
    if "conflicts of interest" in normalized or "conflict of interest" in normalized:
        return True
    return bool(
        "restriction" in normalized
        and "prevent or limit your ability to work" in normalized
    )


def _question_is_professional_certification_inventory(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if "certification" not in normalized and "license" not in normalized:
        return False
    return any(
        fragment in normalized
        for fragment in (
            "currently hold",
            "you hold",
            "please list",
            "list any relevant professional",
        )
    )


def _resume_history_contains_subject(text: str | None, master_resume_text: str | None = None) -> bool | None:
    subjects = _employment_subject_candidates(text)
    employers = _resume_employer_names(master_resume_text)
    if not subjects or not employers:
        return None
    return any(
        _employment_subject_matches(employer_name, subject) for employer_name in employers for subject in subjects
    )


def _current_employer_matches_subject(text: str | None, master_resume_text: str | None = None) -> bool | None:
    subjects = _employment_subject_candidates(text)
    if not subjects:
        return None
    try:
        current_employer = primary_employer_name(master_resume_text)
    except (FileNotFoundError, OSError, ValueError):
        return None
    return any(_employment_subject_matches(current_employer, subject) for subject in subjects)


def _prior_application_subject_candidates(text: str | None, *, company_name: str | None = None) -> list[str]:
    normalized = normalize_text(text)
    if not normalized:
        return []

    subjects: list[str] = []
    seen: set[str] = set()
    patterns = (
        r"have you (?:ever )?previously applied to (?P<company>.+?)(?:\?|$)",
        r"have you ever applied to (?P<company>.+?)(?:\?|$)",
        r"have you applied to (?P<company>.+?) before(?:\?|$)",
        r"have you (?:ever )?applied for a role at (?P<company>.+?)(?:\?|$)",
        r"have you (?:ever )?applied for (?P<company>.+?) before(?:\?|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if not match:
            continue
        raw_company = match.group("company")
        raw_company = re.sub(r"\s+or\s+any\s+.+?\bsubsidiar(?:y|ies)\b.*$", "", raw_company).strip()
        raw_company = re.sub(r"\s+or\s+any\s+.+?\baffiliate(?:s)?\b.*$", "", raw_company).strip()
        cleaned = _clean_employment_subject(raw_company)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            subjects.append(cleaned)

    if company_name and re.search(r"\bor any\b.*\bsubsidiar(?:y|ies)\b", normalized):
        cleaned_company = _clean_employment_subject(company_name)
        if cleaned_company and cleaned_company not in seen:
            seen.add(cleaned_company)
            subjects.append(cleaned_company)
    return subjects


def _prior_application_history_boolean(
    text: str | None,
    *,
    company_name: str | None = None,
    jobs_db_path: Path | None = None,
) -> bool | None:
    subjects = _prior_application_subject_candidates(text, company_name=company_name)
    if not subjects:
        return None

    db_path = Path(jobs_db_path or JOBS_DB_PATH)
    if not db_path.exists():
        return None

    import sqlite3

    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return None

    try:
        rows = conn.execute(
            "SELECT company FROM jobs WHERE status = 'submitted' OR confirmed_at IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    prior_companies = [str(row[0] or "").strip() for row in rows if str(row[0] or "").strip()]
    if not prior_companies:
        return False
    return any(_employment_subject_matches(company, subject) for company in prior_companies for subject in subjects)


def _employee_referral_boolean(application_profile: ApplicationProfile) -> tuple[bool, str]:
    referral_source = normalize_text(getattr(application_profile, "how_did_you_hear", None))
    if not referral_source:
        return False, "deterministic"
    is_referral = any(
        fragment in referral_source
        for fragment in (
            "employee referral",
            "referred by employee",
            "internal referral",
            "employee referred",
        )
    )
    return is_referral, "application_profile.md"


def question_requests_company_history(text: str | None) -> bool:
    if question_is_current_company_field(label=text):
        return False
    normalized = normalize_text(text)
    if not normalized:
        return False
    return any(
        fragment in normalized
        for fragment in (
            "companies you ve worked at",
            "companies you have worked at",
            "list of companies that you ve worked at",
            "list the companies you ve worked at",
            "company history",
            "employment history",
            "companies you worked at",
        )
    )


def question_requests_startup_experience(text: str | None) -> bool:
    normalized = normalize_text(text)
    if "startup" not in normalized:
        return False
    return any(
        fragment in normalized
        for fragment in (
            "startup experience",
            "worked at a startup",
            "worked for a startup",
            "experience in an early stage startup",
            "experience at a startup",
        )
    )


def question_is_startup_experience_confirmation(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not question_requests_startup_experience(normalized):
        return False
    return not any(
        fragment in normalized
        for fragment in (
            "describe",
            "detail",
            "details",
            "tell us",
            "share",
            "summarize",
            "explain",
            "what startup experience",
        )
    )


def question_requests_proud_product_story(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized or "proud" not in normalized:
        return False
    if any(
        fragment in normalized
        for fragment in (
            "share something you built",
            "share a project",
            "project you d like to share",
            "link to something you built",
            "demo link",
        )
    ):
        return False
    if not any(
        fragment in normalized
        for fragment in (
            "consumer facing",
            "customer facing",
            "product",
            "feature",
            "project",
            "shipped",
            "built",
            "launched",
            "worked on",
        )
    ):
        return False
    return any(
        fragment in normalized
        for fragment in (
            "most proud",
            "proud of",
            "were proud of",
            "you re proud of",
        )
    )


def question_requests_crypto_product_experience(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if "crypto" not in normalized and "blockchain" not in normalized:
        return False
    if not any(fragment in normalized for fragment in ("experience", "worked on", "working on", "product")):
        return False
    return any(
        fragment in normalized
        for fragment in (
            "zero to one",
            "zero-to-one",
            "0 to 1",
            "0-to-1",
            "crypto facing",
            "crypto-facing",
        )
    )


def question_requests_sponsorship_requirement(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if normalized.startswith(
        (
            "if yes",
            'if "yes"',
            "if you said yes",
            "if you answered yes",
            "if you answered ‘yes’",
            "if you answered 'yes'",
        )
    ):
        return False
    if any(
        fragment in normalized
        for fragment in (
            "if sponsorship is required",
            "provide additional details",
            "feel free to provide additional details",
            "current visa status",
            "visa type",
            "type of sponsorship required",
            "amount of time left on current visa",
            "expiration date",
            "current employment authorization expire",
            "basis of your current employment authorization",
        )
    ):
        return False
    if any(
        fragment in normalized
        for fragment in (
            "require sponsorship",
            "require any sponsorship",
            "require any form of sponsorship",
            "require visa",
            "visa support",
            "need sponsorship",
            "need a visa",
            "visa sponsorship",
            "visa transfer",
            "employer sponsorship",
            "work sponsorship",
            "immigration sponsorship",
            "immigration-related support or sponsorship",
            "employment sponsorship",
            "employment visa status",
            "immigration case",
            "work permit",
            "sponsor an immigration",
            "sponsor you for a work",
            "h 1b status",
            "h1b status",
        )
    ):
        return True
    if ("petition approved on your behalf" in normalized or "approved i 140" in normalized) and any(
        fragment in normalized for fragment in ("h 1b", "h1b", "immigration", "cap exempt")
    ):
        return True
    if "sponsorship" in normalized and re.search(
        r"\b(require|required|need|needs|petition|application|file|proceed)\b",
        normalized,
    ):
        return True
    return _question_requests_employment_based_status_case(normalized)


def _question_requests_employment_based_status_case(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    return ("employment based immigration status" in normalized or "immigration status" in normalized) and any(
        fragment in normalized
        for fragment in (
            "require",
            "petition",
            "application",
            "on your behalf",
            "file",
            "proceed with",
        )
    )


_WORK_AUTHORIZATION_TEXT_FRAGMENTS = (
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
_WORK_AUTHORIZATION_BOOLEAN_PROMPT_PREFIXES = (
    "are ",
    "do ",
    "does ",
    "did ",
    "can ",
    "could ",
    "will ",
    "would ",
    "have ",
    "has ",
    "is ",
    "should ",
)
_US_PERSON_PROMPT_FRAGMENTS = (
    "u s person",
    "us person",
)
_RESTRICTED_COUNTRY_CITIZENSHIP_PROMPT_FRAGMENTS = (
    "cuba iran north korea or syria",
    "cuba iran north korea and syria",
)
_RESTRICTED_COUNTRY_CITIZENSHIP_COUNTRY_TOKENS = (
    "cuba",
    "iran",
    "north korea",
    "syria",
)
_US_PERSON_EXPLICIT_STATUS_TOKENS = (
    "united states citizen",
    "u s citizen",
    "us citizen",
    "u s national",
    "us national",
    "american citizen",
    "lawful permanent resident",
    "permanent resident",
    "green card",
    "refugee",
    "asylee",
)
_WORK_AUTHORIZATION_COUNTRY_PATTERNS = {
    "united_states": (
        re.compile(r"\bunited states(?: of america)?\b", re.I),
        re.compile(r"\bu\.?s\.?a?\b", re.I),
    ),
    "canada": (re.compile(r"\bcanada\b", re.I),),
    "united_kingdom": (
        re.compile(r"\bunited kingdom\b", re.I),
        re.compile(r"\bu\.?k\.\b", re.I),
    ),
}


def _explicit_work_authorization_country_codes(text: str | None) -> set[str]:
    raw = str(text or "").strip()
    if not raw:
        return set()
    matched: set[str] = set()
    for code, patterns in _WORK_AUTHORIZATION_COUNTRY_PATTERNS.items():
        if any(pattern.search(raw) for pattern in patterns):
            matched.add(code)
    return matched


def _authorized_country_codes(application_profile: ApplicationProfile) -> set[str]:
    if not application_profile.authorized_to_work_unconditionally:
        return set()
    matched = _explicit_work_authorization_country_codes(getattr(application_profile, "country", None))
    matched.update(
        _explicit_work_authorization_country_codes(getattr(application_profile, "work_authorization_statement", None))
    )
    if not matched:
        matched.add("united_states")
    return matched


def _question_expects_boolean_work_authorization_answer(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if any(token in normalized for token in ("yes no", "yes or no")):
        return True
    if any(
        phrase in normalized
        for phrase in ("if yes", "please provide details", "provide details", "please explain", "please describe")
    ):
        return False
    if normalized.startswith(("have you held", "have you had")) and any(
        fragment in normalized for fragment in ("h 1b", "h1b", "petition approved on your behalf", "visa")
    ):
        return True
    if not normalized.startswith(_WORK_AUTHORIZATION_BOOLEAN_PROMPT_PREFIXES):
        return False
    if question_requests_sponsorship_requirement(normalized):
        return True
    return len(normalized.split()) <= 14


def question_is_current_country_work_authorization(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if (
        "country of residence" in normalized
        and ("without restrictions" in normalized or "without restriction" in normalized)
        and any(fragment in normalized for fragment in ("able to work", "can work", "work in"))
    ):
        return True
    explicit_countries = _explicit_work_authorization_country_codes(text)
    if not explicit_countries:
        return False
    if not any(fragment in normalized for fragment in _WORK_AUTHORIZATION_TEXT_FRAGMENTS):
        return False
    return any(
        fragment in normalized
        for fragment in (
            "without the need for employer sponsorship",
            "without employer sponsorship",
            "without the need for sponsorship",
            "without need for sponsorship",
            "without sponsorship",
            "without the need for visa sponsorship",
            "without needing sponsorship",
            "without requiring sponsorship",
        )
    )


def question_is_u_s_person_status(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if any(fragment in normalized for fragment in _US_PERSON_PROMPT_FRAGMENTS):
        return True
    return "protected individual" in normalized and any(
        fragment in normalized for fragment in ("citizen", "permanent resident", "green card", "refugee", "asylee")
    )


def question_is_restricted_country_citizenship_status(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    country_hits = sum(1 for token in _RESTRICTED_COUNTRY_CITIZENSHIP_COUNTRY_TOKENS if token in normalized)
    if country_hits < 3 and not any(
        fragment in normalized for fragment in _RESTRICTED_COUNTRY_CITIZENSHIP_PROMPT_FRAGMENTS
    ):
        return False
    return any(
        token in normalized
        for token in (
            "citizen",
            "citizenship",
            "passport",
            "dual citizenship",
            "reside in",
            "residency",
            "residence",
            "permanent residence",
            "permanent resident",
            "permanent residency",
            "refugee",
            "asylum",
            "asylee",
        )
    )


def _question_expects_boolean_u_s_person_answer(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if normalized.startswith("please indicate whether"):
        return True
    if any(token in normalized for token in ("yes no", "yes or no")):
        return True
    return normalized.startswith(_WORK_AUTHORIZATION_BOOLEAN_PROMPT_PREFIXES)


def _explicit_u_s_person_support_source(
    application_profile: ApplicationProfile,
    *,
    master_resume_text: str | None = None,
) -> str | None:
    source_texts: list[tuple[str, str]] = []

    try:
        resume_text = master_resume_text or MASTER_RESUME_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        resume_text = None
    if resume_text:
        match = re.search(r"^\s*Work Authorization:\s*(.+)$", resume_text, re.M)
        if match:
            source_texts.append(("master_resume.md", match.group(1).strip()))

    work_auth_statement = str(getattr(application_profile, "work_authorization_statement", "") or "").strip()
    if work_auth_statement:
        source_texts.append(("application_profile.md", work_auth_statement))

    country_is_united_states = "united states" in normalize_text(getattr(application_profile, "country", None))
    for source_name, raw_text in source_texts:
        normalized = normalize_text(raw_text)
        if not normalized:
            continue
        if any(
            fragment in normalized
            for fragment in (
                "united states citizen",
                "u s citizen",
                "us citizen",
                "u s national",
                "us national",
                "american citizen",
            )
        ):
            return source_name
        has_u_s_context = country_is_united_states or any(
            fragment in normalized for fragment in ("united states", "u s", "us")
        )
        if has_u_s_context and any(fragment in normalized for fragment in _US_PERSON_EXPLICIT_STATUS_TOKENS):
            return source_name
    return None


def _u_s_person_answer(
    text: str | None,
    application_profile: ApplicationProfile,
    *,
    master_resume_text: str | None = None,
) -> tuple[str | None, str | None]:
    if not question_is_u_s_person_status(text):
        return None, None
    source = _explicit_u_s_person_support_source(
        application_profile,
        master_resume_text=master_resume_text,
    )
    if source is None:
        return None, None
    if _question_expects_boolean_u_s_person_answer(text):
        return "Yes", source
    return "I am a U.S. person", source


def build_truthful_work_authorization_answer(
    text: str | None,
    application_profile: ApplicationProfile,
    *,
    master_resume_text: str | None = None,
) -> str | None:
    normalized = normalize_text(text)
    if not normalized:
        return None
    us_person_answer, _ = _u_s_person_answer(
        text,
        application_profile,
        master_resume_text=master_resume_text,
    )
    if us_person_answer is not None:
        return us_person_answer
    if question_is_restricted_country_citizenship_status(text):
        return _bool_to_text(getattr(application_profile, "citizen_of_cuba_iran_north_korea_or_syria", None))
    boolean_value = _work_authorization_boolean(str(text), application_profile)
    boolean_prompt = _question_expects_boolean_work_authorization_answer(text)
    asks_sponsorship = question_requests_sponsorship_requirement(normalized)
    asks_current_country_authorization = question_is_current_country_work_authorization(normalized)
    asks_employment_based_status_case = _question_requests_employment_based_status_case(normalized)
    sponsorship_answer = str(getattr(application_profile, "sponsorship_answer", "") or "").strip() or None
    work_authorization_statement = (
        str(getattr(application_profile, "work_authorization_statement", "") or "").strip() or None
    )
    asks_authorization = any(fragment in normalized for fragment in _WORK_AUTHORIZATION_TEXT_FRAGMENTS)
    if asks_sponsorship and asks_authorization:
        if boolean_value is not None and boolean_prompt:
            return _bool_to_text(boolean_value)
        return sponsorship_answer or work_authorization_statement
    if asks_sponsorship:
        if asks_employment_based_status_case:
            return sponsorship_answer or work_authorization_statement
        if boolean_value is not None and boolean_prompt:
            return _bool_to_text(boolean_value)
        return sponsorship_answer
    if asks_authorization or asks_current_country_authorization:
        if boolean_value is not None and (boolean_prompt or boolean_value is False):
            return _bool_to_text(boolean_value)
        return work_authorization_statement
    return None


def question_requests_language_proficiencies(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized or "language" not in normalized:
        return False
    if "programming language" in normalized:
        return False
    return any(
        fragment in normalized
        for fragment in (
            "language proficiencies",
            "language proficiency",
            "language fluency",
            "level of fluency",
            "level of fluency for each",
            "languages spoken",
            "spoken languages",
            "human languages",
        )
    )


def _normalize_language_name(value: str | None) -> str:
    normalized = normalize_text(value)
    normalized = re.sub(r"^(languages spoken|speaks and writes)\s+", "", normalized).strip()
    normalized = normalized.removeprefix("and ").strip()
    aliases = {
        "mandarin chinese": "mandarin",
        "cantonese chinese": "cantonese",
        "english language": "english",
        "spanish language": "spanish",
    }
    return aliases.get(normalized, normalized)


def _display_language_name(language_key: str) -> str:
    special_names = {
        "asl": "ASL",
        "american sign language": "American Sign Language",
        "mandarin": "Mandarin",
        "cantonese": "Cantonese",
        "english": "English",
        "spanish": "Spanish",
    }
    if language_key in special_names:
        return special_names[language_key]
    return " ".join(part.upper() if len(part) <= 3 else part.capitalize() for part in language_key.split())


def _split_language_entries(value: str | None) -> list[str]:
    if not value:
        return []
    compact = value.replace("\ufeff", "").replace("; ", ", ").replace(" and ", ", ")
    entries = [part.strip(" -*\t\r\n") for part in compact.split(",")]
    return [entry for entry in entries if entry]


def _resume_language_proficiencies(master_resume_text: str | None) -> dict[str, str]:
    if master_resume_text is None:
        try:
            master_resume_text = MASTER_RESUME_PATH.read_text(encoding="utf-8")
        except OSError:
            return {}
    match = re.search(r"^Languages:\s*(.+)$", master_resume_text, re.MULTILINE)
    if not match:
        return {}
    proficiencies: dict[str, str] = {}
    for language, level in re.findall(r"([^,]+?)\s*\(([^)]+)\)", match.group(1)):
        normalized_language = _normalize_language_name(language)
        normalized_level = re.sub(r"\s+", " ", level).strip().lower()
        if normalized_language and normalized_level:
            proficiencies[normalized_language] = normalized_level
    return proficiencies


def _candidate_context_language_sequence(candidate_context_text: str | None = None) -> list[str]:
    if candidate_context_text is None:
        try:
            candidate_context_text = CANDIDATE_CONTEXT_PATH.read_text(encoding="utf-8")
        except OSError:
            return []
    match = re.search(r"speaks and writes\s+([^\n\r]+)", candidate_context_text.replace("\ufeff", ""), re.I)
    if not match:
        return []
    sequence: list[str] = []
    seen: set[str] = set()
    for entry in _split_language_entries(match.group(1)):
        normalized = _normalize_language_name(entry)
        if normalized and normalized not in seen:
            seen.add(normalized)
            sequence.append(normalized)
    return sequence


def _application_profile_language_sequence(application_profile_text: str | None = None) -> list[str]:
    if application_profile_text is None:
        try:
            application_profile_text = APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        except OSError:
            return []
    match = re.search(r"Languages Spoken:\s*([^\n\r]+)", application_profile_text, re.I)
    if not match:
        return []
    sequence: list[str] = []
    seen: set[str] = set()
    for entry in _split_language_entries(match.group(1)):
        normalized = _normalize_language_name(entry)
        if normalized and normalized not in seen:
            seen.add(normalized)
            sequence.append(normalized)
    return sequence


def build_source_backed_language_proficiency_answer(
    *,
    master_resume_text: str | None = None,
    candidate_context_text: str | None = None,
    application_profile_text: str | None = None,
) -> str | None:
    resume_proficiencies = _resume_language_proficiencies(master_resume_text)
    candidate_sequence = _candidate_context_language_sequence(candidate_context_text)
    profile_sequence = _application_profile_language_sequence(application_profile_text)
    ordered_languages: list[str] = []
    seen: set[str] = set()
    for sequence in (candidate_sequence, profile_sequence, list(resume_proficiencies)):
        for language in sequence:
            if language and language not in seen:
                seen.add(language)
                ordered_languages.append(language)
    if not ordered_languages:
        return None
    candidate_languages = set(candidate_sequence)
    formatted_languages: list[str] = []
    for language in ordered_languages:
        level = resume_proficiencies.get(language)
        if level is None and language in candidate_languages:
            level = "fluent"
        if level is None:
            continue
        formatted_languages.append(f"{_display_language_name(language)} ({level})")
    if not formatted_languages:
        return None
    return ", ".join(formatted_languages)


def build_source_backed_biography_answer(
    text: str | None,
    *,
    master_resume_text: str | None = None,
    work_stories_text: str | None = None,
) -> str | None:
    if question_requests_crypto_product_experience(text):
        return (
            "I have not yet shipped a crypto product in-market, but I do have adjacent zero-to-one product experience "
            "that maps well. I built 0-to-1 products including Moody's SlipStream, Kyte's first ML risk engine, and "
            "Kyte's in-house A/B testing platform, and during my M.S. in Computer Science I was a Ripple Research "
            "Fellow focused on blockchain applications in insurance. That combination gave me experience building "
            "technical products from first principles in regulated environments while developing a strong foundation "
            "in blockchain."
        )
    if question_requests_proud_product_story(text):
        resume_text = master_resume_text
        if resume_text is None:
            if MASTER_RESUME_PATH.exists():
                resume_text = MASTER_RESUME_PATH.read_text(encoding="utf-8")
            else:
                resume_text = ""
        stories_text = work_stories_text
        if stories_text is None:
            if WORK_STORIES_PATH.exists():
                stories_text = WORK_STORIES_PATH.read_text(encoding="utf-8")
            else:
                stories_text = ""
        normalized_resume = normalize_text(resume_text)
        normalized_stories = normalize_text(stories_text)
        if (
            "ml risk engine" in normalized_resume
            and "23" in normalized_resume
            and "7" in normalized_resume
            and "post booking" in normalized_stories
            and "completed bookings" in normalized_stories
        ):
            return (
                "At Kyte, I'm most proud of shipping the post-booking verification flow powered by a new "
                "ML risk engine. We replaced a rules-based system that was blocking about 12% of completed "
                "bookings, and I partnered with a data scientist and engineer to get the model into "
                "production. After launch, losses fell 23% and revenue increased 7% from previously blocked "
                "good customers."
            )
    if question_requests_company_history(text):
        return format_resume_employer_history(master_resume_text)
    if not question_requests_startup_experience(text):
        return None

    stories_text = work_stories_text
    if stories_text is None:
        if WORK_STORIES_PATH.exists():
            stories_text = WORK_STORIES_PATH.read_text(encoding="utf-8")
        else:
            stories_text = ""
    startup_match = re.search(r"\bAt\s+(.+?)\s+—\s+a\s+(.+?startup)\b", stories_text, re.I)
    if startup_match:
        company_name = startup_match.group(1).strip()
        startup_descriptor = startup_match.group(2).strip()
        return f"Yes - I worked at {company_name}, a {startup_descriptor}, as Staff Product Manager."

    employer_history = format_resume_employer_history(master_resume_text)
    if employer_history and "kyte" in employer_history.casefold():
        return "Yes - I worked at Kyte in a startup environment as Staff Product Manager."
    return None


def _source_backed_special_answer_for_question(
    text: str | None,
    *,
    master_resume_text: str | None = None,
    work_stories_text: str | None = None,
) -> str | None:
    normalized = normalize_text(text)
    if not normalized:
        return None

    resume_text = _load_master_resume_text(master_resume_text)
    stories_text = work_stories_text
    if stories_text is None:
        try:
            stories_text = WORK_STORIES_PATH.read_text(encoding="utf-8")
        except OSError:
            stories_text = ""
    normalized_resume = normalize_text(resume_text)
    normalized_stories = normalize_text(stories_text)

    if any(
        fragment in normalized
        for fragment in (
            "ai llm impact",
            "examples where ai improved your work output",
            "examples where llm improved your work output",
        )
    ):
        has_slipstream = "slipstream" in normalized_resume and "200b" in normalized_resume
        has_irp_navigator = "irp navigator" in normalized_resume and "31%" in resume_text
        has_prototype = (
            "prototype in 3 days" in normalized_resume
            or ("claude code" in normalized_stories and "figma" in normalized_stories)
        )
        if has_prototype and has_irp_navigator:
            return (
                "1) I was trying to break a product-engineering stalemate on a workflow change for a $15M "
                "at-risk enterprise account. I asked Claude Code to help turn the proposed workflow into a "
                "working prototype I could put in front of customers quickly, then used Figma to refine the "
                "flow based on what we learned. That let me build and deploy a functional prototype in 3 days, "
                "move the conversation from opinion to evidence, and narrow scope in time to retain the account.\n\n"
                "2) I was trying to improve answer quality and reduce support volume in IRP Navigator. We used "
                "the highest-volume customer questions as test prompts, then tightened retrieval and response "
                "quality with RAG changes. That improved answer usefulness and reduced support ticket volume 31%."
            )
        if has_slipstream and has_irp_navigator:
            return (
                "1) At Moody's, I was trying to remove the manual extraction work that happened before "
                "underwriters could run a risk model. I used SlipStream, a multi-agent LLM pipeline, to turn "
                "unstructured policy documents into structured data. That cut processing time from 60 minutes "
                "to 5 and unlocked automation across $200B+ in policy premiums.\n\n"
                "2) I also worked on IRP Navigator, our AI chatbot, to improve support quality and reduce ticket "
                "volume. I partnered with engineering to tune retrieval accuracy and response quality using RAG "
                "architecture optimizations. That reduced support ticket volume 31% and improved the usefulness "
                "of the answers users received."
            )
    if "recent cybersecurity product" in normalized and "bring to market" in normalized:
        if "slipstream" in normalized_resume and "200b" in normalized_resume:
            return (
                "At Moody's Analytics, I helped bring SlipStream to market, an agentic AI workflow for cyber "
                "and catastrophe underwriting teams that transforms unstructured policy documents into structured "
                "risk data. The opportunity came from repeated customer pain and sales notes showing that "
                "underwriters were spending meaningful time manually extracting data before they could even run "
                "a risk model. I helped define the product direction and work cross-functionally with engineering "
                "to turn that bottleneck into a production workflow. The result cut processing time from 60 "
                "minutes to 5 and unlocked automation across $200B+ in policy premiums."
            )
    if "builder executive" in normalized:
        return (
            "This kind of role appeals to me because it combines executive-facing delivery with the systems-"
            "building work I enjoy most. Some of my best work has been turning messy enterprise implementation "
            "problems into reusable onboarding frameworks, API standards, integration playbooks, and operating "
            "rhythms that make future deployments faster and lower risk. I like roles where you stay close to "
            "senior stakeholders, but the end goal is not just getting one customer live; it is building the "
            "repeatable methods and automation that help every subsequent implementation go better."
        )
    if (
        "reason you are looking for a change" in normalized
        or "why are you leaving or left your previous company" in normalized
    ):
        return (
            "I am not looking for a change because of a problem at Moody's Analytics. I am looking for a role "
            "that is closer to financial infrastructure and payments, where I can apply my background in "
            "high-stakes, risk-aware product work to money movement more directly. What is most compelling to "
            "me is the combination of technical complexity, trust, compliance, and user experience, because "
            "that is where I have done some of my strongest product work."
        )
    return None


def shared_text_answer_for_question(
    text: str | None,
    application_profile: ApplicationProfile | None,
    *,
    master_resume_text: str | None = None,
    work_stories_text: str | None = None,
) -> str | None:
    if not text:
        return None
    normalized = normalize_text(text)
    if application_profile is not None:
        if question_requests_language_proficiencies(text):
            language_answer = build_source_backed_language_proficiency_answer(
                master_resume_text=master_resume_text,
            )
            if language_answer:
                return language_answer
        if question_requests_ai_agent_and_rag_experience(text):
            return (
                "Yes. I regularly use AI coding agents such as Claude Code and Codex to prototype, "
                "automate workflows, and accelerate product and engineering work. I also work in "
                "codebases that use markdown instruction files to guide agent behavior, workflow "
                "constraints, and quality checks. Professionally, I launched SlipStream at Moody's, "
                "a multi-agent LLM system that turns unstructured insurance documents into structured "
                "underwriting data, and I have also improved RAG-based support experiences and built "
                "smaller AI workflow automations personally. So yes across all three areas, with "
                "hands-on experience in both production systems and day-to-day development workflows."
            )
        if question_requests_ai_workflow_usage(text):
            workflow_answer = build_source_backed_ai_workflow_usage_answer(
                question_text=text,
                master_resume_text=master_resume_text,
                work_stories_text=work_stories_text,
            )
            if workflow_answer:
                return workflow_answer
        if question_requests_writing_samples(text):
            writing_sample_links = _candidate_writing_sample_links()
            if writing_sample_links:
                return "\n".join(writing_sample_links)
        if any(
            fragment in normalized
            for fragment in (
                "preferred first name and last name",
                "preferred first and last name",
                "preferred full name",
                "legal first and last name",
                "legal full name",
            )
        ):
            preferred_full_name = _candidate_full_name_text(master_resume_text)
            if preferred_full_name:
                return preferred_full_name
        if "preferred name" in normalized:
            preferred_name = _candidate_first_name_text(master_resume_text)
            if preferred_name:
                return preferred_name
        current_application_date = _current_application_date_answer(normalized)
        if current_application_date:
            return current_application_date
        current_license_inventory = current_professional_license_inventory_answer(
            text,
            master_resume_text=master_resume_text,
        )
        if current_license_inventory:
            return current_license_inventory
        product_analysis_tools_inventory = product_analysis_tools_inventory_answer(
            text,
            master_resume_text=master_resume_text,
        )
        if product_analysis_tools_inventory:
            return product_analysis_tools_inventory
        customer_experience_helpdesk_tools = customer_experience_helpdesk_tools_answer(
            text,
            master_resume_text=master_resume_text,
        )
        if customer_experience_helpdesk_tools:
            return customer_experience_helpdesk_tools
        global_teams_answer = global_teams_experience_answer(
            text,
            master_resume_text=master_resume_text,
        )
        if global_teams_answer:
            return global_teams_answer
        domain_experience_answer = source_backed_domain_experience_answer(
            text,
            master_resume_text=master_resume_text,
        )
        if domain_experience_answer:
            return domain_experience_answer
        relocation_text_answer = relocation_willingness_text_answer(text, application_profile)
        if relocation_text_answer:
            return relocation_text_answer
        if re.search(r"\bpronouns?\b", normalized):
            pronouns = getattr(application_profile, "pronouns", None)
            if pronouns:
                return pronouns
        if any(
            fragment in normalized
            for fragment in (
                "confirm your email",
                "confirm email address",
                "confirm email",
            )
        ):
            candidate_email = _candidate_email_text(application_profile, master_resume_text)
            if candidate_email:
                return candidate_email
        if (
            "linkedin" in normalized
            and getattr(application_profile, "linkedin", None)
            and not question_is_profile_included_confirmation(text)
        ):
            return application_profile.linkedin
        if (
            "github" in normalized
            and getattr(application_profile, "github", None)
            and not question_is_profile_included_confirmation(text)
        ):
            return application_profile.github
        if (
            any(fragment in normalized for fragment in ("website", "portfolio"))
            and getattr(application_profile, "website", None)
            and not question_is_profile_included_confirmation(text)
        ):
            return application_profile.website
        if any(fragment in normalized for fragment in ("profile url", "profile link")):
            if normalized in {"profile url", "profile link", "your profile url", "your profile link"}:
                profile_url = (
                    getattr(application_profile, "website", None)
                    or getattr(application_profile, "github", None)
                    or getattr(application_profile, "linkedin", None)
                )
                if profile_url:
                    return profile_url
            return None
        if any(
            fragment in normalized
            for fragment in (
                "share something you built",
                "share a project",
                "project you'd like to share",
                "sample of your work",
                "demo link",
                "link to something you built",
            )
        ):
            if re.search(r"\bbuilt\s+(?:with|on|using)\b", normalized):
                return None
            project_links = []
            website = getattr(application_profile, "website", None)
            github = getattr(application_profile, "github", None)
            if website:
                project_links.append(website)
            if github and github not in project_links:
                project_links.append(github)
            if project_links:
                return "You can review examples of my work here: " + " and ".join(project_links)
    if application_profile is not None and question_is_education(text):
        return format_education_from_profile(application_profile)
    if question_is_current_company_field(label=text):
        return primary_employer_name(master_resume_text)
    if application_profile is not None:
        work_authorization_answer = build_truthful_work_authorization_answer(text, application_profile)
        if work_authorization_answer is not None:
            return work_authorization_answer
        special_answer = _source_backed_special_answer_for_question(
            text,
            master_resume_text=master_resume_text,
            work_stories_text=work_stories_text,
        )
        if special_answer:
            return special_answer
        policy = resolve_shared_question_policy(text, application_profile, master_resume_text=master_resume_text)
        if policy is not None:
            return policy.text_value
    return build_source_backed_biography_answer(
        text,
        master_resume_text=master_resume_text,
        work_stories_text=work_stories_text,
    )


def should_skip_optional_generated_answer(spec: dict) -> bool:
    if bool(spec.get("required")):
        return False
    normalized = normalize_text(
        "\n".join(
            part.strip()
            for part in (str(spec.get("label") or ""), str(spec.get("description") or ""))
            if part and str(part).strip()
        )
    )
    if not normalized:
        return False
    if any(
        fragment in normalized
        for fragment in (
            "how do you pronounce your name",
            "phonetic spelling of your name",
            "pronunciation of your name",
        )
    ):
        return True
    if any(fragment in normalized for fragment in ("profile url", "profile link")) and not any(
        fragment in normalized for fragment in ("linkedin", "github", "website", "portfolio")
    ):
        return True
    if re.search(r"\bbuilt\s+(?:with|on|using)\b", normalized) and any(
        fragment in normalized
        for fragment in (
            "share something you built",
            "share a project",
            "project you d like to share",
            "sample of your work",
            "demo link",
            "link to something you built",
        )
    ):
        return True
    if any(
        fragment in normalized
        for fragment in (
            "when is the earliest you would want to start",
            "when can you start",
            "date available",
            "available date",
            "earliest start",
        )
    ):
        return True
    if any(
        fragment in normalized
        for fragment in (
            "deadline",
            "deadlines",
            "timeline consideration",
            "timeline considerations",
        )
    ):
        return True
    return normalized in {
        "date",
        "today s date",
        "today s date of application",
        "todays date",
        "todays date of application",
    }


def question_requests_writing_samples(text: str | None) -> bool:
    if not text:
        return False
    normalized = normalize_text(text)
    return any(
        fragment in normalized
        for fragment in (
            "writing sample",
            "writing samples",
            "sample of your writing",
            "samples of your writing",
            "published writing",
            "examples of your writing",
        )
    )


def _candidate_writing_sample_links(candidate_context_text: str | None = None) -> list[str]:
    try:
        context_text = candidate_context_text or CANDIDATE_CONTEXT_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []

    lines = context_text.replace("\ufeff", "").splitlines()
    collecting = False
    section_links: list[str] = []
    seen: set[str] = set()
    non_url_lines = 0

    for raw_line in lines:
        line = raw_line.strip()
        if not collecting:
            if normalize_text(line).startswith("writing samples"):
                collecting = True
            continue

        if not line:
            if section_links:
                break
            continue

        if re.fullmatch(r"[A-Z][A-Za-z0-9'&,/(). -]{2,}", line) and section_links:
            break

        matches = [match.rstrip(").,") for match in LINKED_RESOURCE_URL_RE.findall(line)]
        if matches:
            non_url_lines = 0
            for match in matches:
                normalized_url = normalize_url(match)
                if normalized_url and normalized_url not in seen:
                    seen.add(normalized_url)
                    section_links.append(normalized_url)
            continue

        if section_links:
            non_url_lines += 1
            if non_url_lines >= 2:
                break

    return section_links[:5]


def question_requests_ai_agent_and_rag_experience(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    mentions_agent_coding = any(fragment in normalized for fragment in ("ai agents", "cursor", "claude code"))
    mentions_markdown_guidance = "markdown files" in normalized and "codebase" in normalized
    mentions_llm_or_rag = any(fragment in normalized for fragment in ("large language models", "rag"))
    return mentions_agent_coding and mentions_markdown_guidance and mentions_llm_or_rag


def question_requests_ai_workflow_usage(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if question_requests_ai_agent_and_rag_experience(text) or question_requests_proud_product_story(text):
        return False
    if any(
        fragment in normalized
        for fragment in (
            "product that incorporated ai",
            "core user experience",
            "consumer facing product",
            "consumer facing feature",
            "customer facing product",
            "customer facing feature",
            "built a system that uses large language models",
            "uses large language models and or rag",
            "uses large language models and rag",
            "ai powered feature",
            "product or feature have you shipped",
        )
    ):
        return False
    mentions_ai_tools = any(
        fragment in normalized
        for fragment in (
            "gen ai tools",
            "ai tools",
            "ai tool",
            "ai llm agent tools",
            "ai llm tools",
            "llm agent tools",
            "llm tools",
            "agent tools",
            "using ai",
            "use of ai",
            "incorporated ai tools",
            "incorporated ai tools or technologies",
            "use ai to build product",
        )
    )
    if not mentions_ai_tools:
        return False
    asks_about_workflow = any(
        fragment in normalized
        for fragment in (
            "your product work",
            "your work",
            "daily work",
            "approach your work",
            "projects to improve efficiency",
            "improve efficiency or outcomes",
            "problem you were solving",
            "tools used",
            "specific example",
            "give us an understanding",
            "used in the last 6 months",
            "last 6 months",
            "what you use it for",
            "note frequency",
            "daily weekly occasional",
        )
    )
    return asks_about_workflow


def build_source_backed_ai_workflow_usage_answer(
    *,
    question_text: str | None = None,
    master_resume_text: str | None = None,
    work_stories_text: str | None = None,
    candidate_context_text: str | None = None,
) -> str | None:
    resume_text = master_resume_text
    if resume_text is None:
        try:
            resume_text = MASTER_RESUME_PATH.read_text(encoding="utf-8")
        except OSError:
            resume_text = ""

    stories_text = work_stories_text
    if stories_text is None:
        try:
            stories_text = WORK_STORIES_PATH.read_text(encoding="utf-8")
        except OSError:
            stories_text = ""

    context_text = candidate_context_text
    if context_text is None:
        try:
            context_text = CANDIDATE_CONTEXT_PATH.read_text(encoding="utf-8")
        except OSError:
            context_text = ""

    normalized_resume = normalize_text(resume_text)
    normalized_stories = normalize_text(stories_text)
    normalized_context = normalize_text(context_text)
    normalized_question = normalize_text(question_text)
    has_prototype_story = "$15m" in resume_text.casefold() and "prototype" in normalized_resume
    has_claude_figma_story = "figma and claude code" in normalized_stories
    has_ai_tooling_context = "claude code" in normalized_context and "prototype ui designs" in normalized_context
    has_codex_context = "codex" in normalized_context and "prototype ui designs" in normalized_context
    has_llm_product_work = "slipstream" in normalized_resume and "irp navigator" in normalized_resume
    if not (has_prototype_story and has_claude_figma_story and has_ai_tooling_context):
        return None

    if normalized_question and any(
        fragment in normalized_question
        for fragment in (
            "which ai llm agent tools",
            "which ai llm tools",
            "last 6 months",
            "what you use it for",
            "note frequency",
        )
    ):
        lines = [
            "Claude Code: regular current use for rapid workflow and UI prototyping, especially when I want "
            "something concrete to put in front of design partners or customers quickly. I used it with Figma "
            "on the prototype that helped resolve a $15M at-risk enterprise account."
        ]
        if has_codex_context:
            lines.append(
                "Codex: regular current use for quick implementation exploration and UI/workflow iteration "
                "before I ask engineering for a full build."
            )
        if has_llm_product_work:
            lines.append(
                "Production LLM/RAG workflows: ongoing use in product work through systems like SlipStream and "
                "IRP Navigator, where I have worked on multi-agent extraction, retrieval quality, and "
                "response-quality improvements."
            )
        return "\n\n".join(lines)

    return (
        "At Moody's, I used Claude Code with Figma to prototype a workflow solution for a "
        "$15M at-risk enterprise account. I built the prototype end to end with AI-assisted "
        "tooling, then hosted it in AWS so customers could interact with it directly and give "
        "feedback. The problem was that we needed fast evidence to resolve a product and "
        "engineering disagreement and keep the customer from churning. That prototype let us "
        "validate the direction quickly, align stakeholders on a scoped path forward, and help "
        "retain the account."
    )


def _extract_linked_resource_urls(question_specs: list[dict]) -> list[tuple[str, str]]:
    resource_urls: list[tuple[str, str]] = []
    seen: set[str] = set()
    for spec in question_specs:
        question_text = "\n".join(
            part.strip() for part in (str(spec.get("label") or ""), str(spec.get("description") or "")) if part.strip()
        )
        for match in LINKED_RESOURCE_URL_RE.findall(question_text):
            url = match.rstrip(").,")
            if url in seen:
                continue
            seen.add(url)
            resource_urls.append((str(spec.get("label") or spec.get("field_name") or "").strip(), url))
    return resource_urls[:2]


def _fetch_linked_resource_excerpt(url: str) -> str | None:
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=10) as response:
            content_type = str(getattr(response, "headers", {}).get("Content-Type", "")).casefold()
            raw_bytes = response.read(24_000)
    except (HTTPError, URLError, OSError, ValueError):
        return None

    if not raw_bytes or b"\x00" in raw_bytes[:512]:
        return None
    if content_type and not any(token in content_type for token in ("text", "json", "html", "xml")):
        return None

    text = raw_bytes.decode("utf-8", errors="replace")
    text = HTML_TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    if len(text) > 2_000:
        text = text[:2_000].rstrip() + "..."
    return text


def build_linked_resource_context(question_specs: list[dict]) -> str | None:
    resources: list[str] = []
    for label, url in _extract_linked_resource_urls(question_specs):
        excerpt = _fetch_linked_resource_excerpt(url)
        if excerpt is None:
            continue
        resources.append(f"Question: {label}\nURL: {url}\nExcerpt: {excerpt}")
    if not resources:
        return None
    return "\n\n".join(resources)


def find_output_dir(target: str) -> Path:
    candidate = Path(target).expanduser()
    if candidate.exists():
        if candidate.is_dir():
            meta_path = candidate / ".pipeline_meta.json"
            if not meta_path.exists():
                raise FileNotFoundError(f"{candidate} does not contain .pipeline_meta.json")
            resolved = candidate.resolve()
            migrate_role_output_layout(resolved)
            return resolved
        raise FileNotFoundError(f"{candidate} is not a directory")

    if re.match(r"^https?://", target, re.I):
        for meta_path in PROJECT_ROOT.glob("output/*/*/.pipeline_meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if target in (
                meta.get("jd_source"),
                meta.get("jd_source_resolved"),
                meta.get("board_url"),
            ):
                resolved = meta_path.parent.resolve()
                migrate_role_output_layout(resolved)
                return resolved

        # Fallback: check jobs.db for URL → output_dir mapping
        import sqlite3

        db_path = PROJECT_ROOT / "jobs.db"
        if db_path.exists():
            try:
                conn = sqlite3.connect(str(db_path))
                row = conn.execute(
                    "SELECT output_dir FROM jobs WHERE url = ? OR board_url = ? LIMIT 1",
                    (target, target),
                ).fetchone()
                conn.close()
                if row and row[0]:
                    candidate_dir = Path(row[0])
                    if not candidate_dir.is_absolute():
                        candidate_dir = PROJECT_ROOT / candidate_dir
                    if candidate_dir.is_dir():
                        resolved = candidate_dir.resolve()
                        migrate_role_output_layout(resolved)
                        return resolved
            except sqlite3.Error:
                pass

        raise FileNotFoundError(f"No output directory found for job URL: {target}")

    raise FileNotFoundError(f"Could not resolve target: {target}")


def _repair_meta_source_tracking_from_jobs_db(out_dir: Path, meta: dict) -> dict:
    db_path = Path(JOBS_DB_PATH)
    if not db_path.exists():
        return meta

    import sqlite3

    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return meta

    try:
        rows = conn.execute(
            """
            SELECT source, source_url, board_url, canonical_url, id
            FROM jobs
            WHERE output_dir = ? AND archived = FALSE
            ORDER BY id DESC
            """,
            (str(out_dir.resolve()),),
        ).fetchall()
    except sqlite3.Error:
        conn.close()
        return meta
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    if not rows:
        return meta

    def _row_score(row: tuple[object, ...]) -> tuple[int, int]:
        source = str(row[0] or "").strip().casefold()
        source_url = str(row[1] or "").strip()
        board_url = str(row[2] or "").strip()
        canonical_url = str(row[3] or "").strip()
        row_id = int(row[4] or 0)
        score = 0
        if source and source != "direct":
            score += 4
        if source_url:
            score += 3
        if board_url and canonical_url and board_url != canonical_url:
            score += 3
        if board_url:
            score += 1
        return score, row_id

    best_row = max(rows, key=_row_score)
    saved_source = str(best_row[0] or "").strip()
    saved_source_url = str(best_row[1] or "").strip()
    saved_board_url = str(best_row[2] or "").strip()

    current_board_url = str(meta.get("board_url") or "").strip()
    resolved_url = str(meta.get("jd_source_resolved") or "").strip()
    changed = False

    if saved_board_url and (not current_board_url or current_board_url == resolved_url):
        if saved_board_url != current_board_url:
            meta["board_url"] = saved_board_url
            changed = True

    if saved_source and not str(meta.get("source") or "").strip():
        meta["source"] = saved_source
        changed = True

    if saved_source_url and not str(meta.get("source_url") or "").strip():
        meta["source_url"] = saved_source_url
        changed = True

    if changed:
        meta_path = out_dir / ".pipeline_meta.json"
        try:
            meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass

    return meta


def load_meta(out_dir: Path) -> dict:
    meta = load_pipeline_meta(out_dir)
    if not isinstance(meta, dict):
        return meta
    return _repair_meta_source_tracking_from_jobs_db(out_dir, meta)


def load_optional_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return None


def json_dumps_pretty(data: object) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _document_company_name_variants(out_dir: Path) -> list[str]:
    try:
        meta = load_pipeline_meta(out_dir)
    except Exception:
        return []

    raw_company = str(meta.get("company_proper") or meta.get("company") or "").strip()
    if not raw_company:
        return []

    stripped_company = raw_company.rstrip(" .,:;!?")
    variants: list[str] = []
    for candidate in (stripped_company, raw_company):
        normalized = candidate.strip()
        if normalized and normalized not in variants:
            variants.append(normalized)
    return variants


def _preferred_document_file(out_dir: Path, label: str, extensions: tuple[str, ...]) -> Path | None:
    for company_name in _document_company_name_variants(out_dir):
        for extension in extensions:
            preferred = find_role_file(
                out_dir,
                f"Jerrison Li {label} - {company_name}{extension}",
                bucket="documents",
            )
            if preferred is not None and preferred.exists():
                return preferred
    return None


def find_resume_file(out_dir: Path) -> Path:
    preferred = _preferred_document_file(out_dir, "Resume", (".pdf", ".docx"))
    if preferred is not None:
        return preferred

    pdfs = sorted(
        glob_role_files(out_dir, "*Resume*.pdf", bucket="documents"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if pdfs:
        return pdfs[0]
    docxs = sorted(
        glob_role_files(out_dir, "*Resume*.docx", bucket="documents"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if docxs:
        return docxs[0]
    raise FileNotFoundError(f"Could not find a resume file in {out_dir}")


def find_cover_letter_text(out_dir: Path) -> str:
    preferred = find_role_file(out_dir, "cover_letter_text.txt", bucket="content")
    if preferred is not None and preferred.exists():
        return preferred.read_text(encoding="utf-8")

    preferred_document = _preferred_document_file(out_dir, "Cover Letter", (".txt",))
    if preferred_document is not None:
        return preferred_document.read_text(encoding="utf-8")

    fallbacks = sorted(
        glob_role_files(out_dir, "*Cover Letter*.txt", bucket="documents"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if fallbacks:
        return fallbacks[0].read_text(encoding="utf-8")

    raise FileNotFoundError(f"Could not find a cover letter text file in {out_dir}")


def find_cover_letter_file(out_dir: Path) -> Path:
    preferred = _preferred_document_file(out_dir, "Cover Letter", (".pdf", ".docx", ".txt"))
    if preferred is not None:
        return preferred

    for pattern in ("*Cover Letter*.pdf", "*Cover Letter*.docx", "*Cover Letter*.txt"):
        matches = sorted(
            glob_role_files(out_dir, pattern, bucket="documents"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if matches:
            return matches[0]
    preferred = find_role_file(out_dir, "cover_letter_text.txt", bucket="content")
    if preferred is not None and preferred.exists():
        return preferred
    raise FileNotFoundError(f"Could not find a cover letter file in {out_dir}")


def build_simple_payload(
    *,
    board_name: str,
    out_dir: Path,
    provider: str,
    meta: dict,
    profile,
    application_profile,
    resume_path: Path | None,
    cover_letter_path: Path | None,
    notes: list[str] | None = None,
    extra_steps: list[dict] | None = None,
    application_url: str | None = None,
) -> dict:
    job_url = preferred_meta_job_url(meta, keys=("jd_source_resolved", "jd_source"))
    company = str(meta.get("company_proper") or meta.get("company") or "")
    company_slug = str(meta.get("company") or "")
    job_title = str(meta.get("jd_title") or "")

    first_name = str(getattr(profile, "first_name", "") or "")
    last_name = str(getattr(profile, "last_name", "") or "")
    full_name = str(getattr(profile, "full_name", "") or "").strip() or f"{first_name} {last_name}".strip()
    email = str(getattr(profile, "email", "") or getattr(application_profile, "verification_code_email", "") or "")
    phone = str(getattr(profile, "phone", "") or "")
    location = str(getattr(application_profile, "location", "") or getattr(profile, "location", "") or "")
    linkedin = str(getattr(application_profile, "linkedin", "") or getattr(profile, "linkedin", "") or "")
    website = str(getattr(application_profile, "website", "") or getattr(profile, "website", "") or "")
    verification_email = str(getattr(application_profile, "verification_code_email", "") or email)

    resume_path_str = str(resume_path) if resume_path is not None else ""
    cover_letter_path_str = str(cover_letter_path) if cover_letter_path is not None else ""

    steps: list[dict] = [
        {
            "field_name": "resume",
            "label": "Resume",
            "kind": "file",
            "required": True,
            "file_path": resume_path_str,
            "source": "existing_resume_asset",
        },
        {
            "field_name": "full_name",
            "label": "Full name",
            "kind": "text",
            "required": True,
            "value": full_name,
            "source": "master_resume.md",
        },
        {
            "field_name": "first_name",
            "label": "First name",
            "kind": "text",
            "required": True,
            "value": first_name,
            "source": "master_resume.md",
        },
        {
            "field_name": "last_name",
            "label": "Last name",
            "kind": "text",
            "required": True,
            "value": last_name,
            "source": "master_resume.md",
        },
        {
            "field_name": "email",
            "label": "Email",
            "kind": "text",
            "required": True,
            "value": email,
            "source": "master_resume.md",
        },
        {
            "field_name": "phone",
            "label": "Phone",
            "kind": "text",
            "required": False,
            "value": phone,
            "source": "master_resume.md",
        },
        {
            "field_name": "location",
            "label": "Location",
            "kind": "text",
            "required": False,
            "value": location,
            "source": "application_profile.md",
        },
        {
            "field_name": "city",
            "label": "City",
            "kind": "text",
            "required": False,
            "value": location,
            "source": "application_profile.md",
        },
        {
            "field_name": "current_location",
            "label": "Current location",
            "kind": "text",
            "required": False,
            "value": location,
            "source": "application_profile.md",
        },
        {
            "field_name": "linkedin",
            "label": "LinkedIn",
            "kind": "text",
            "required": False,
            "value": linkedin,
            "source": "application_profile.md",
        },
        {
            "field_name": "website",
            "label": "Website",
            "kind": "text",
            "required": False,
            "value": website,
            "source": "application_profile.md",
        },
        {
            "field_name": "work_authorization",
            "label": "Work authorization",
            "kind": "select",
            "required": False,
            "value": "Yes",
            "source": "application_profile.md",
        },
        {
            "field_name": "sponsorship",
            "label": "Require sponsorship",
            "kind": "select",
            "required": False,
            "value": "No",
            "source": "application_profile.md",
        },
    ]
    if cover_letter_path_str:
        steps.insert(
            1,
            {
                "field_name": "cover_letter",
                "label": "Cover letter",
                "kind": "file",
                "required": False,
                "file_path": cover_letter_path_str,
                "source": "existing_cover_letter_asset",
            },
        )
    if extra_steps:
        steps.extend(extra_steps)

    return {
        "board": board_name,
        "job_url": job_url,
        "application_url": str(application_url or job_url),
        "out_dir": str(out_dir),
        "company": company,
        "company_slug": company_slug,
        "candidate_name": full_name,
        "candidate_email": email,
        "verification_code_email": verification_email,
        "job_title": job_title,
        "provider": provider,
        "resume_path": resume_path_str,
        "cover_letter_path": cover_letter_path_str,
        "mode": "review-before-submit",
        "notes": list(notes or []),
        "artifacts": {
            "payload_json": str(role_submit_path(out_dir, f"{board_name}_autofill_payload.json")),
            "report_json": str(role_submit_path(out_dir, f"{board_name}_autofill_report.json")),
            "report_markdown": str(role_submit_path(out_dir, f"{board_name}_autofill_report.md")),
            "pre_submit_screenshot": str(role_submit_path(out_dir, f"{board_name}_autofill_pre_submit.png")),
            "post_submit_screenshot": str(role_submit_path(out_dir, f"{board_name}_autofill_post_submit.png")),
            "page_screenshots_dir": str(role_submit_path(out_dir, f"{board_name}_autofill_pages")),
            "unknown_questions_json": str(role_submit_path(out_dir, f"{board_name}_unknown_questions.json")),
            "submit_debug_html": str(role_submit_path(out_dir, f"{board_name}_submit_debug.html")),
            "submit_debug_screenshot": str(role_submit_path(out_dir, f"{board_name}_submit_debug.png")),
            "application_page_html": str(role_submit_path(out_dir, f"{board_name}_application_page.html")),
        },
        "steps": steps,
        "unknown_questions": [],
    }


def preferred_meta_job_url(
    meta: dict,
    *,
    keys: tuple[str, ...] = ("board_url", "jd_source_resolved", "jd_source"),
) -> str:
    """Return the most trustworthy job/apply URL stored in pipeline metadata."""
    candidates = [str(meta.get(key) or "").strip() for key in keys]
    for candidate in candidates:
        if candidate and not looks_like_non_html_asset_url(candidate) and not looks_like_unresolved_url_template(candidate):
            return candidate
    return next((candidate for candidate in candidates if candidate), "")


def write_submission_result(
    *,
    out_dir: Path,
    status: str,
    job_url: str,
    message: str,
    failure_type: str | None = None,
    auth_state: str | None = None,
    auth_scope: str | None = None,
    board: str | None = None,
    provider: str | None = None,
    artifacts: dict[str, str] | None = None,
) -> None:
    result = {
        "status": status,
        "job_url": job_url,
        "message": message,
    }
    if board:
        result["board"] = board
    if provider:
        result["provider"] = provider
    if failure_type:
        result["failure_type"] = failure_type
    if auth_state:
        result["auth_state"] = auth_state
    if auth_scope:
        result["auth_scope"] = auth_scope
    if artifacts:
        result["artifacts"] = dict(artifacts)

    result_path = role_submit_path(out_dir, "application_submission_result.json")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def load_notion_sync_module():
    script_path = PROJECT_ROOT / "scripts" / "notion_sync.py"
    spec = importlib.util.spec_from_file_location("job_assets_notion_sync", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load Notion sync helper from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EmailConfirmationWatcher:
    def __init__(
        self,
        out_dir: Path,
        *,
        min_received_at_utc: str | datetime | None = None,
        poll_interval_seconds: int | None = None,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.min_received_at_utc = min_received_at_utc
        self.poll_interval_seconds = max(
            int(
                poll_interval_seconds
                if poll_interval_seconds is not None
                else os.environ.get(
                    "JOB_ASSETS_SUBMIT_EMAIL_POLL_INTERVAL_SECONDS",
                    str(DEFAULT_SUBMIT_EMAIL_POLL_INTERVAL_SECONDS),
                )
            ),
            1,
        )
        self._module = None
        self._last_checked_at = 0.0
        self._cached_confirmation: dict | None = None
        self.last_error: str | None = None

    def poll(self, *, force: bool = False) -> dict | None:
        if self._cached_confirmation is not None:
            return self._cached_confirmation
        now = time.monotonic()
        if not force and self._last_checked_at and now - self._last_checked_at < self.poll_interval_seconds:
            return None
        self._last_checked_at = now
        try:
            module = self._module or load_notion_sync_module()
            self._module = module
            confirmation = module.find_email_confirmation(
                self.out_dir,
                min_received_at_utc=self.min_received_at_utc,
                write_artifact=True,
            )
        except Exception as exc:
            self.last_error = str(exc)
            return None
        self.last_error = None
        if confirmation:
            self._cached_confirmation = confirmation
        return confirmation


def build_email_confirmation_watcher(
    payload: dict,
    *,
    min_received_at_utc: str | datetime | None = None,
    poll_interval_seconds: int | None = None,
) -> EmailConfirmationWatcher:
    return EmailConfirmationWatcher(
        Path(payload["out_dir"]),
        min_received_at_utc=min_received_at_utc,
        poll_interval_seconds=poll_interval_seconds,
    )


def sync_notion_after_submit(
    payload: dict,
    outcome: dict,
    *,
    provider: str = "generic",
    email_confirmation: dict | None = None,
    min_received_at_utc: datetime | str | None = None,
) -> dict:
    notion_sync = load_notion_sync_module()
    out_dir = Path(payload["out_dir"])
    notion_sync.record_website_confirmation(out_dir, outcome, provider=provider)
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
    return notion_sync.sync_application(
        out_dir,
        **kwargs,
    )


def _search_confirmation_email(payload: dict) -> str | None:
    """Search Gmail for a recent confirmation email matching the company name.

    Uses a two-attempt strategy: full (post-suffix-strip) name first, then
    first word only if the name is multi-word.  This handles cases like
    "ZoomInfo Technologies LLC" where emails only say "ZoomInfo".
    """
    company = payload.get("company_proper") or payload.get("company") or ""
    if not company:
        return None

    company_search = re.sub(
        r",?\s*\b(Inc\.?|LLC|Corp\.?|Ltd\.?|Co\.?|PBC|L\.?P\.?)\b\.?",
        "",
        company,
        flags=re.IGNORECASE,
    ).strip()
    company_search = company_search.replace("-", " ")

    # Two-attempt strategy: full name, then first word (if multi-word)
    words = company_search.split()
    candidates = [company_search]
    if len(words) > 1:
        candidates.append(words[0])

    for candidate in candidates:
        thread_id = _gmail_search_confirmation(candidate)
        if thread_id:
            return thread_id
    return None


def _gmail_search_confirmation(company_search: str) -> str | None:
    """Run a single Gmail search for confirmation emails matching *company_search*."""
    # Quote the company name to prevent Gmail search-operator injection
    safe_company = company_search.replace('"', "")
    try:
        result = subprocess.run(
            [
                "gws",
                "gmail",
                "users",
                "messages",
                "list",
                "--params",
                json.dumps(
                    {
                        "userId": "me",
                        "q": (
                            f'newer_than:1d "{safe_company}" '
                            f"(application OR applying OR received OR submission "
                            f'OR submitted OR "thank you" OR interest)'
                        ),
                        "maxResults": 3,
                    }
                ),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            logger.warning("Gmail search failed (rc=%d): %s", result.returncode, result.stderr[:200])
            return None
        messages = json.loads(result.stdout).get("messages", [])
        return messages[0].get("threadId") if messages else None
    except subprocess.TimeoutExpired:
        logger.warning("Gmail search timed out for: %s", safe_company)
        return None
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("Gmail search parse error: %s", exc)
        return None


def _reply_state_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _confirmation_email_reply_submit_dir(out_dir: Path) -> Path:
    preferred_name = preferred_submit_dir_name_for_post_submit(out_dir)
    if preferred_name:
        preferred_dir = out_dir / preferred_name
        if preferred_dir.is_dir():
            return preferred_dir
    return role_submit_dir(out_dir)


def _confirmation_email_reply_state_path(submit_dir: Path) -> Path:
    submit_dir.mkdir(parents=True, exist_ok=True)
    return submit_dir / CONFIRMATION_EMAIL_REPLY_JSON


def _load_confirmation_email_reply_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_confirmation_email_reply_state(path: Path, state: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return state


def _persist_confirmation_email_reply_outcome(
    state_path: Path,
    previous_state: dict,
    *,
    status: str,
    caller: str,
    board_name: str,
    reason: str | None = None,
    sent: bool = False,
    thread_id: str | None = None,
    report_path: Path | None = None,
    screenshot_path: Path | None = None,
    gmail_message_id: str | None = None,
    subject: str | None = None,
    error: str | None = None,
) -> dict:
    timestamp = _reply_state_timestamp()
    state = dict(previous_state)
    state.setdefault("sent", False)
    state["board_name"] = board_name
    state["last_status"] = status
    state["last_attempted_at_utc"] = timestamp
    state["last_caller"] = caller
    if reason is not None:
        state["last_reason"] = reason
    else:
        state.pop("last_reason", None)
    if thread_id:
        state["thread_id"] = thread_id
    if report_path is not None:
        state["report_path"] = str(report_path)
    if screenshot_path is not None:
        state["screenshot_path"] = str(screenshot_path)
    if gmail_message_id:
        state["gmail_message_id"] = gmail_message_id
    if subject:
        state["subject"] = subject
    if error:
        state["last_error"] = error
    else:
        state.pop("last_error", None)
    if sent:
        state["sent"] = True
        state["sent_at_utc"] = timestamp
        state["sent_by"] = caller
    return _write_confirmation_email_reply_state(state_path, state)


def _reply_result(
    status: str,
    *,
    reason: str | None,
    submit_dir: Path,
    state_path: Path,
    state: dict,
) -> dict:
    return {
        "status": status,
        "reason": reason,
        "submit_dir": str(submit_dir),
        "state_path": str(state_path),
        "thread_id": state.get("thread_id"),
        "sent": bool(state.get("sent")),
        "sent_at_utc": state.get("sent_at_utc"),
    }


def _resolve_confirmation_email_thread_id(payload: dict, email_confirmation: dict | None) -> str | None:
    thread_id = None
    if email_confirmation:
        thread_id = email_confirmation.get("thread_id") or email_confirmation.get("threadId")
        if not thread_id:
            msg_id = email_confirmation.get("message_id") or email_confirmation.get("gmail_message_id")
            if msg_id:
                try:
                    result = subprocess.run(
                        [
                            "gws",
                            "gmail",
                            "users",
                            "messages",
                            "get",
                            "--params",
                            json.dumps({"userId": "me", "id": msg_id, "format": "metadata"}),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    if result.returncode == 0:
                        thread_id = json.loads(result.stdout).get("threadId")
                except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as exc:
                    logger.warning("Failed to fetch thread for message %s: %s", msg_id, exc)

    if not thread_id:
        thread_id = _search_confirmation_email(payload)

    if not thread_id:
        logger.info("Waiting 30s for confirmation email to arrive...")
        time.sleep(30)
        thread_id = _search_confirmation_email(payload)

    return thread_id


def _send_confirmation_email_message(
    msg: Message,
    *,
    thread_id: str,
    submit_dir: Path,
) -> subprocess.CompletedProcess[str]:
    msg_bytes = msg.as_bytes()
    body_json = json.dumps(
        {
            "raw": urlsafe_b64encode(msg_bytes).decode("ascii"),
            "threadId": thread_id,
        }
    )
    if len(body_json) > GMAIL_INLINE_SEND_MAX_JSON_BYTES:
        logger.info("Email reply payload too large (%d bytes); retrying via upload.", len(body_json))
        return _send_confirmation_email_message_upload(msg_bytes, thread_id=thread_id, submit_dir=submit_dir)
    return subprocess.run(
        [
            "gws",
            "gmail",
            "users",
            "messages",
            "send",
            "--params",
            json.dumps({"userId": "me"}),
            "--json",
            body_json,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _send_confirmation_email_message_upload(
    msg_bytes: bytes,
    *,
    thread_id: str,
    submit_dir: Path,
) -> subprocess.CompletedProcess[str]:
    upload_path = submit_dir / f".confirmation_email_reply_{os.getpid()}_{time.time_ns()}.eml"
    upload_path.write_bytes(msg_bytes)
    try:
        return subprocess.run(
            [
                "gws",
                "gmail",
                "users",
                "messages",
                "send",
                "--params",
                json.dumps({"userId": "me"}),
                "--json",
                json.dumps({"threadId": thread_id}),
                "--upload",
                upload_path.name,
                "--upload-content-type",
                "message/rfc822",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=submit_dir,
        )
    finally:
        upload_path.unlink(missing_ok=True)


def send_confirmation_email_reply(
    payload: dict,
    *,
    board_name: str,
    email_confirmation: dict | None = None,
    caller: str = "automatic",
) -> dict:
    """Reply to the confirmation email with the pre-submit screenshot and autofill report.

    Returns a structured result describing whether the reply was sent, skipped as a
    duplicate, or left unsent but retryable.
    """
    out_dir = Path(payload["out_dir"])
    submit_dir = _confirmation_email_reply_submit_dir(out_dir)
    state_path = _confirmation_email_reply_state_path(submit_dir)
    prior_state = _load_confirmation_email_reply_state(state_path)

    if prior_state.get("sent") is True:
        state = _persist_confirmation_email_reply_outcome(
            state_path,
            prior_state,
            status="skipped_duplicate",
            caller=caller,
            board_name=board_name,
            reason="reply_already_sent",
            thread_id=str(prior_state.get("thread_id") or "") or None,
        )
        return _reply_result(
            "skipped_duplicate",
            reason="reply_already_sent",
            submit_dir=submit_dir,
            state_path=state_path,
            state=state,
        )

    if not shutil.which("gws"):
        logger.warning("gws CLI not found — skipping confirmation email reply.")
        state = _persist_confirmation_email_reply_outcome(
            state_path,
            prior_state,
            status="not_sent",
            caller=caller,
            board_name=board_name,
            reason="gws_not_found",
        )
        return _reply_result(
            "not_sent", reason="gws_not_found", submit_dir=submit_dir, state_path=state_path, state=state
        )

    artifacts = payload.get("artifacts") or {}

    # --- Derive company from .pipeline_meta.json when payload lacks it ---
    company = payload.get("company_proper") or payload.get("company") or ""
    if not company:
        try:
            meta_path = out_dir / ".pipeline_meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                company = meta.get("company_proper") or meta.get("company") or ""
        except Exception as exc:
            logger.warning("Failed to read pipeline meta for company: %s", exc)
    # Enrich payload in-place so _search_confirmation_email sees the derived company
    if company and not payload.get("company"):
        payload["company"] = company

    # --- Resolve report path (fix key: "report_md" not "report_markdown") ---
    report_path = resolve_submit_artifact_path(
        out_dir,
        board_name=board_name,
        artifact_key="report_markdown",
        artifacts=artifacts,
        submit_dirname=submit_dir.name,
        fallback_key="report_md",
    )
    if not report_path:
        logger.warning("No autofill report found for email reply.")
        state = _persist_confirmation_email_reply_outcome(
            state_path,
            prior_state,
            status="not_sent",
            caller=caller,
            board_name=board_name,
            reason="missing_autofill_report",
        )
        return _reply_result(
            "not_sent",
            reason="missing_autofill_report",
            submit_dir=submit_dir,
            state_path=state_path,
            state=state,
        )

    # --- Resolve screenshot path (with board_file_constants fallback) ---
    screenshot_path = resolve_submit_artifact_path(
        out_dir,
        board_name=board_name,
        artifact_key="pre_submit_screenshot",
        artifacts=artifacts,
        submit_dirname=submit_dir.name,
        fallback_key="pre_submit_screenshot",
    )
    if not screenshot_path:
        logger.warning("No pre-submit screenshot found for email reply.")
        state = _persist_confirmation_email_reply_outcome(
            state_path,
            prior_state,
            status="not_sent",
            caller=caller,
            board_name=board_name,
            reason="missing_pre_submit_screenshot",
            report_path=report_path,
        )
        return _reply_result(
            "not_sent",
            reason="missing_pre_submit_screenshot",
            submit_dir=submit_dir,
            state_path=state_path,
            state=state,
        )

    # Find confirmation email thread
    thread_id = _resolve_confirmation_email_thread_id(payload, email_confirmation)

    if not thread_id:
        logger.warning("Could not find confirmation email thread — skipping reply.")
        state = _persist_confirmation_email_reply_outcome(
            state_path,
            prior_state,
            status="not_sent",
            caller=caller,
            board_name=board_name,
            reason="confirmation_email_thread_not_found",
            report_path=report_path,
            screenshot_path=screenshot_path,
        )
        return _reply_result(
            "not_sent",
            reason="confirmation_email_thread_not_found",
            submit_dir=submit_dir,
            state_path=state_path,
            state=state,
        )

    # Build and send MIME reply
    try:
        from email.mime.image import MIMEImage
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        report_text = report_path.read_text(encoding="utf-8")

        # Compress screenshot to JPEG to fit within gws CLI arg limits
        try:
            from io import BytesIO

            from PIL import Image

            img = Image.open(screenshot_path)
            w, h = img.size
            small = img.resize((w // 2, h // 2), Image.LANCZOS).convert("RGB")
            buf = BytesIO()
            small.save(buf, "JPEG", quality=55, optimize=True)
            img_data = buf.getvalue()
            img_subtype = "jpeg"
            img_filename = screenshot_path.stem + ".jpg"
        except ImportError:
            # Pillow not available — use raw PNG if small enough
            img_data = screenshot_path.read_bytes()
            img_subtype = "png"
            img_filename = screenshot_path.name

        # Get In-Reply-To header
        in_reply_to = None
        subject = "Re: Application Confirmation"
        try:
            result = subprocess.run(
                [
                    "gws",
                    "gmail",
                    "users",
                    "messages",
                    "get",
                    "--params",
                    json.dumps(
                        {
                            "userId": "me",
                            "id": thread_id,
                            "format": "metadata",
                            "metadataHeaders": ["Message-Id", "Subject"],
                        }
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                for h in json.loads(result.stdout).get("payload", {}).get("headers", []):
                    if h["name"].lower() == "message-id":
                        in_reply_to = h["value"]
                    if h["name"] == "Subject":
                        fetched_subject = h["value"]
                        subject = fetched_subject if fetched_subject.startswith("Re:") else f"Re: {fetched_subject}"
        except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to fetch In-Reply-To header: %s", exc)

        msg = MIMEMultipart("mixed")
        msg["From"] = "jerrisonli@gmail.com"
        msg["To"] = "jerrisonli@gmail.com"
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to

        # Build HTML body with report text and inline image
        import html as html_mod
        from email.mime.application import MIMEApplication

        report_html_lines = "".join(
            f"<p>{html_mod.escape(line)}</p>" if line.strip() else "<br>" for line in report_text.splitlines()
        )
        html_body = (
            f'<div style="font-family:monospace;font-size:13px;">'
            f"{report_html_lines}"
            f'<br><img src="cid:pre_submit_screenshot" style="max-width:100%;"><br>'
            f"</div>"
        )

        related_part = MIMEMultipart("related")
        related_part.attach(MIMEText(html_body, "html", "utf-8"))

        img_part = MIMEImage(img_data, _subtype=img_subtype)
        img_part.add_header("Content-ID", "<pre_submit_screenshot>")
        img_part.add_header("Content-Disposition", "inline", filename=img_filename)
        related_part.attach(img_part)
        msg.attach(related_part)

        # Attach resume and cover letter PDFs if they exist
        docs_dir = out_dir / "documents"
        if docs_dir.is_dir():
            for pdf_file in sorted(docs_dir.glob("*.pdf")):
                try:
                    pdf_data = pdf_file.read_bytes()
                    pdf_part = MIMEApplication(pdf_data, _subtype="pdf")
                    pdf_part.add_header("Content-Disposition", "attachment", filename=pdf_file.name)
                    msg.attach(pdf_part)
                except OSError as exc:
                    logger.warning("Failed to attach PDF %s: %s", pdf_file.name, exc)

        result = _send_confirmation_email_message(msg, thread_id=thread_id, submit_dir=submit_dir)
        if result.returncode == 0:
            logger.info("Replied to confirmation email with report + screenshot.")
            gmail_message_id = None
            try:
                send_payload = json.loads(result.stdout or "{}")
                gmail_message_id = send_payload.get("id")
                thread_id = send_payload.get("threadId") or thread_id
            except json.JSONDecodeError:
                pass
            state = _persist_confirmation_email_reply_outcome(
                state_path,
                prior_state,
                status="sent",
                caller=caller,
                board_name=board_name,
                reason=None,
                sent=True,
                thread_id=thread_id,
                report_path=report_path,
                screenshot_path=screenshot_path,
                gmail_message_id=gmail_message_id,
                subject=subject,
            )
            return _reply_result("sent", reason=None, submit_dir=submit_dir, state_path=state_path, state=state)
        logger.warning("Failed to send confirmation reply: %s", result.stdout[:200])
        state = _persist_confirmation_email_reply_outcome(
            state_path,
            prior_state,
            status="not_sent",
            caller=caller,
            board_name=board_name,
            reason="gmail_send_failed",
            thread_id=thread_id,
            report_path=report_path,
            screenshot_path=screenshot_path,
            subject=subject,
        )
        return _reply_result(
            "not_sent", reason="gmail_send_failed", submit_dir=submit_dir, state_path=state_path, state=state
        )
    except Exception as exc:
        logger.warning("Error sending confirmation reply: %s", exc)
        state = _persist_confirmation_email_reply_outcome(
            state_path,
            prior_state,
            status="not_sent",
            caller=caller,
            board_name=board_name,
            reason="gmail_send_exception",
            thread_id=thread_id,
            report_path=report_path,
            screenshot_path=screenshot_path,
            error=str(exc),
        )
        return _reply_result(
            "not_sent",
            reason="gmail_send_exception",
            submit_dir=submit_dir,
            state_path=state_path,
            state=state,
        )


def reply_to_confirmation_email(
    payload: dict,
    *,
    board_name: str,
    email_confirmation: dict | None = None,
    caller: str = "automatic",
) -> bool:
    result = send_confirmation_email_reply(
        payload,
        board_name=board_name,
        email_confirmation=email_confirmation,
        caller=caller,
    )
    return result.get("status") == "sent"


def _load_json_object_candidate(text: str, *, provider: str | None = None) -> dict:
    data, repair_step = json_lenient.loads_with_diagnostics(text)
    if not isinstance(data, dict):
        raise ValueError("Provider output did not contain a JSON object")
    if repair_step != "clean":
        logger.warning("Repaired answer JSON for provider=%s using step=%s", provider or "unknown", repair_step)
    return data


def extract_json_object(text: str, *, provider: str | None = None) -> dict:
    text = text.strip()
    if not text:
        raise ValueError("Provider returned empty output")

    try:
        return _load_json_object_candidate(text, provider=provider)
    except (json.JSONDecodeError, ValueError):
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        try:
            return _load_json_object_candidate(fenced.group(1), provider=provider)
        except (json.JSONDecodeError, ValueError):
            pass

    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : index + 1]
                    try:
                        return _load_json_object_candidate(candidate, provider=provider)
                    except (json.JSONDecodeError, ValueError):
                        pass
                    break
        start = text.find("{", start + 1)

    raise ValueError("Could not extract a JSON object from provider output")


def _string_answer_schema(*, label: str, nullable: bool, required: bool) -> dict:
    schema: dict[str, object] = {"type": ["string", "null"] if nullable else "string"}
    if label:
        schema["description"] = label
    if required:
        schema["minLength"] = 1
    return schema


def _multi_select_answer_schema(*, label: str, nullable: bool) -> dict:
    schema: dict[str, object] = {
        "type": ["array", "null"] if nullable else "array",
        "items": {"type": "string", "minLength": 1},
        "minItems": 1,
    }
    if label:
        schema["description"] = label
    return schema


def build_application_answers_json_schema(question_specs: list[dict]) -> dict:
    properties: dict[str, dict] = {}
    required: list[str] = []

    for spec in question_specs:
        field_name = str(spec["field_name"])
        label = str(spec.get("label") or "").strip()
        is_required = bool(spec.get("required"))
        nullable = not is_required or _is_conditional_followup(spec)
        if spec.get("type") == "multi_value_multi_select":
            schema = _multi_select_answer_schema(label=label, nullable=nullable)
        else:
            schema = _string_answer_schema(label=label, nullable=nullable, required=is_required)

        properties[field_name] = schema
        required.append(field_name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def canonicalize_provider_question_specs(question_specs: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Normalize provider-facing field names to schema-safe unique slugs."""
    seen: set[str] = set()
    canonical_specs: list[dict] = []
    canonical_to_original: dict[str, str] = {}
    for index, spec in enumerate(question_specs):
        original_field_name = str(spec.get("field_name") or "")
        label = str(spec.get("label") or "")
        base_name = slugify_label(original_field_name or label or f"field_{index + 1}")
        if not base_name or base_name == "field":
            base_name = f"field_{index + 1}"
        canonical_field_name = base_name
        suffix = 2
        while canonical_field_name in seen:
            canonical_field_name = f"{base_name}_{suffix}"
            suffix += 1
        seen.add(canonical_field_name)
        canonical_spec = dict(spec)
        canonical_spec["field_name"] = canonical_field_name
        canonical_specs.append(canonical_spec)
        canonical_to_original[canonical_field_name] = original_field_name
    return canonical_specs, canonical_to_original


def restore_provider_answer_field_names(
    answers: dict[str, object] | None, canonical_to_original: dict[str, str]
) -> dict[str, object] | None:
    if answers is None:
        return None
    restored: dict[str, object] = {}
    for field_name, value in answers.items():
        original_field_name = canonical_to_original.get(str(field_name), str(field_name))
        restored[original_field_name] = value
    return restored


def _question_is_undergraduate_gpa(label: str) -> bool:
    normalized = normalize_text(label)
    mentions_undergraduate = "undergraduate" in normalized or "bachelor" in normalized
    mentions_gpa = " gpa" in f" {normalized}" or "grade point average" in normalized
    return mentions_undergraduate and mentions_gpa


def build_application_answers_prompt(
    *,
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
    employer_name = primary_employer_name(master_resume_text)
    instructions = [
        "You are writing answers for a job application form.",
        "Use the cover letter as source material, but do NOT simply reuse the first few sentences of the cover letter.",
        "Generate fresh answers for each field using all available context: JD, company research, tailored resume content, cover letter, master resume, work stories, and candidate context.",
        "Use application_profile.md for factual defaults such as work authorization and other application-profile details when relevant.",
        f"For explicit current-company or current-employer fields, use only the current employer: {employer_name}.",
        "Answer optional fields whenever a truthful answer can be derived from the available context.",
        "Only return JSON null for optional fields when no truthful answer exists.",
        "For conditional follow-up fields such as 'If other...' or 'If yes...' where the condition does not clearly apply, return JSON null.",
        "If a field says something like '3-4 sentences', obey that exactly.",
        APPLICATION_ANSWER_EM_DASH_GUIDANCE,
        "For salary, compensation, or total rewards expectation questions, NEVER give a specific number or range. Always use negotiable language like: 'I'm open and flexible on compensation and would prefer to discuss after learning more about the role's scope and total rewards package.'",
        "When a question asks about a specific domain, product category, or industry, only claim direct experience if the sources explicitly support it. If the experience is adjacent, say that explicitly instead of implying direct ownership.",
        "When a question asks about tools, platforms, programming languages, certifications, or licenses, mention only items explicitly named in the sources. Do not infer common tools or credentials from the work.",
        "Do not claim global, international, or distributed-team experience unless the sources explicitly support it. If the sources only show broad cross-functional work, say that instead.",
        "For prompts that say 'If none, put N/A,' answer with 'N/A' when the specific experience or tool is not explicitly supported by the sources.",
        "Avoid adding implementation details, architectures, internal tool names, or validation mechanics unless those specifics appear in the allowed sources.",
        "For multi-select questions about how you've engaged with or been exposed to the company (e.g. 'Have you engaged with [Company] through any of the following?'), always select options for product/service usage (e.g. taken a course, used the platform) and blog/content consumption. For single-select, prioritize product usage.",
        "For multi_value_single_select fields, return one allowed option label exactly as written in the options array.",
        "For multi_value_multi_select fields, return a JSON array of selected option labels and obey any count instructions in the prompt.",
        "For company-history, prior-employer, or startup-experience questions, use only facts directly supported by master_resume.md or work_stories.md.",
        "If a question label or description includes a directly accessible URL, use the fetched linked resource context below before answering that question.",
        "Answers must be punchy, concise, specific, and company-specific.",
        "Mirror the company's language where useful, but do not sound templated.",
        "Never fabricate accomplishments, metrics, or experience.",
        "Do not mention private or sensitive facts from candidate_context.md unless they are clearly relevant to the field.",
        "When the role or question is explicitly about LATAM, Latin America, or Spanish-speaking markets, and candidate_context.md supports a Panama/Panamanian background, that regional context is materially relevant and may be mentioned briefly.",
        "Do not volunteer protected characteristics from application_profile.md unless the field explicitly asks for them.",
        "For open-ended application questions, answer the question directly. Do not write a salutation or signoff unless the field explicitly asks for a cover letter.",
        "For AI-related questions: if the question explicitly asks how you use AI tools in your work, daily workflow, or product work, use the Moody's prototyping story: Claude Code + Figma to build a prototype for a $15M at-risk account, hosted in AWS for direct customer feedback, used to turn a product/engineering disagreement into evidence and help retain the account. Reserve SlipStream and similar examples for prompts explicitly about an AI product, feature, system, or workflow you built or launched. Personal projects like meal planning are off-limits when the question scopes to work. If the question asks about AI in general or in your life broadly, personal projects are fine to include.",
        "Return valid JSON only. The output must be a single JSON object keyed by field_name. Include every field_name exactly once. Use strings for answered fields and JSON null for blank optional or non-applicable conditional follow-up fields.",
    ]

    return (
        "\n".join(instructions)
        + "\n\nFields to answer:\n"
        + json_dumps_pretty(question_specs)
        + ("\n\nContext: linked screening resources\n" + linked_resource_context if linked_resource_context else "")
        + "\n\nContext: pipeline metadata\n"
        + json_dumps_pretty(meta)
        + "\n\nContext: jd_parsed.json\n"
        + json_dumps_pretty(jd_parsed)
        + "\n\nContext: resume_content.json\n"
        + json_dumps_pretty(resume_content)
        + "\n\nContext: research_cache.json\n"
        + json_dumps_pretty(research_cache)
        + "\n\nContext: cover_letter_text.txt\n"
        + cover_letter_text
        + "\n\nContext: master_resume.md\n"
        + master_resume_text
        + "\n\nContext: work_stories.md\n"
        + work_stories_text
        + "\n\nContext: candidate_context.md\n"
        + candidate_context_text
        + "\n\nContext: application_profile.md\n"
        + application_profile_text
    )


def load_cached_application_answers(
    path: Path,
    question_specs: list[dict],
    *,
    linked_resource_cache_key: str | None = None,
    preference_research_cache_key: str | None = None,
) -> dict[str, object] | None:
    cached = load_optional_json(path)
    if not cached:
        return None
    if cached.get("questions") != question_specs:
        return None
    linked_resources = cached.get("linked_resources") if isinstance(cached, dict) else None
    cached_linked_resource_cache_key = None
    if isinstance(linked_resources, dict):
        cached_linked_resource_cache_key = str(linked_resources.get("cache_key") or "").strip() or None
    if linked_resource_cache_key:
        if cached_linked_resource_cache_key != linked_resource_cache_key:
            return None
    preference_research = cached.get("preference_research") if isinstance(cached, dict) else None
    cached_preference_research_cache_key = None
    if isinstance(preference_research, dict):
        cached_preference_research_cache_key = str(preference_research.get("cache_key") or "").strip() or None
    if preference_research_cache_key:
        if cached_preference_research_cache_key != preference_research_cache_key:
            return None
    answers = cached.get("answers")
    if not isinstance(answers, dict):
        return None
    return validate_generated_answers(question_specs, answers)


def find_matching_cached_application_answers_path(
    out_dir: Path,
    *,
    current_answers_path: Path,
    question_specs: list[dict],
    linked_resource_cache_key: str | None = None,
    preference_research_cache_key: str | None = None,
) -> Path | None:
    for submit_dir in existing_submit_dirs(out_dir):
        candidate = submit_dir / APPLICATION_ANSWER_CACHE
        if candidate == current_answers_path or not candidate.exists():
            continue
        if (
            load_cached_application_answers(
                candidate,
                question_specs,
                linked_resource_cache_key=linked_resource_cache_key,
                preference_research_cache_key=preference_research_cache_key,
            )
            is not None
        ):
            return candidate
    return None


def _provider_binary(provider: str) -> str:
    """Return the CLI binary name for a provider (handles variants like gemini-flash).

    .. deprecated:: Use ``llm_provider.provider_binary()`` instead.
    """
    from llm_provider import provider_binary

    return provider_binary(provider)


def _answer_generation_fallback_provider(provider: str) -> str | None:
    chain = list(automation_provider_chain())
    try:
        idx = chain.index(provider)
    except ValueError:
        return None
    for candidate in chain[idx + 1 :]:
        if shutil.which(_provider_binary(candidate)):
            return candidate
    return None


def _run_answer_generation_provider(
    *,
    provider: str,
    prompt: str,
    raw_output_path: Path,
    timeout_seconds: int,
    question_specs: list[dict],
    request_id: str | None = None,
    command_builder=None,
) -> tuple[dict[str, str] | None, Exception | None]:
    command_builder = command_builder or provider_command_for_mode
    json_schema = build_application_answers_json_schema(question_specs) if provider == "openai" else None
    cmd = command_builder(
        provider,
        prompt,
        mode="submit",
        json_schema=json_schema,
        json_schema_name="application_answers" if json_schema else None,
    )
    attempt_logs: list[str] = []

    for attempt in range(2):
        header = f"INFO: provider={provider} mode=submit attempt={attempt + 1}"
        if request_id:
            header += f" request_id={request_id}"
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(PROJECT_ROOT),
                text=True,
                capture_output=True,
                timeout=timeout_seconds or None,
            )
        except subprocess.TimeoutExpired as exc:
            parts = [header]
            if exc.stdout:
                parts.append(exc.stdout.rstrip())
            if exc.stderr:
                parts.append(exc.stderr.rstrip())
            parts.append(f"ERROR: Answer generation via {provider} timed out after {timeout_seconds}s.")
            attempt_logs.append("\n".join(part for part in parts if part))
            raw_output_path.write_text("\n\n".join(part for part in attempt_logs if part) + "\n", encoding="utf-8")
            return None, RuntimeError(
                f"Answer generation via {provider} timed out after {timeout_seconds}s. See {raw_output_path} for details."
            )

        parts = [header]
        if completed.stdout:
            parts.append(completed.stdout.rstrip())
        if completed.stderr:
            parts.append(completed.stderr.rstrip())
        attempt_logs.append("\n".join(part for part in parts if part))
        raw_output_path.write_text("\n\n".join(part for part in attempt_logs if part) + "\n", encoding="utf-8")

        if completed.returncode != 0:
            return None, RuntimeError(f"Answer generation via {provider} failed. See {raw_output_path} for details.")

        try:
            answers = extract_json_object((completed.stdout or "").strip(), provider=provider)
        except (json.JSONDecodeError, ValueError) as exc:
            if attempt == 0:
                logger.warning("Answer generation via %s returned invalid JSON; retrying once: %s", provider, exc)
                retry_note = f"ERROR: Invalid JSON from {provider}: {exc}. Retrying once."
                attempt_logs[-1] = "\n".join(part for part in (attempt_logs[-1], retry_note) if part)
                raw_output_path.write_text("\n\n".join(part for part in attempt_logs if part) + "\n", encoding="utf-8")
                continue
            return None, exc
        return answers, None

    return None, RuntimeError(f"Answer generation via {provider} failed without producing valid JSON.")


def clear_answer_generation_artifacts(out_dir: Path) -> None:
    migrate_role_output_layout(out_dir)
    for name in (
        APPLICATION_ANSWER_CACHE,
        APPLICATION_ANSWER_RAW,
        APPLICATION_ANSWER_FALLBACK_RAW,
    ):
        path = role_submit_path(out_dir, name)
        try:
            path.unlink()
        except FileNotFoundError:
            continue
    clear_linked_resource_artifacts(out_dir)


def _write_application_answers_payload(
    path: Path,
    *,
    question_specs: list[dict],
    answers: dict[str, object],
    provider: str,
    refresh_request_id: str | None,
    linked_resource_payload: dict,
    existing_payload: dict | None = None,
) -> None:
    payload = dict(existing_payload or {})
    payload["generated_at_utc"] = datetime.now(UTC).replace(microsecond=0).isoformat()
    payload["provider"] = provider
    payload["refresh_request_id"] = refresh_request_id
    payload["questions"] = question_specs
    payload["answers"] = answers
    linked_resources = _linked_resource_answer_payload(linked_resource_payload)
    if linked_resources is not None:
        payload["linked_resources"] = linked_resources
    else:
        payload.pop("linked_resources", None)
    path.write_text(json_dumps_pretty(payload) + "\n", encoding="utf-8")


def _normalized_known_board_name(value: object) -> str | None:
    normalized = str(value or "").strip().casefold()
    if not normalized or normalized == "unknown":
        return None
    return normalized


def _resolved_submit_board_name(out_dir: Path, meta: dict | None) -> str:
    known_meta_board = _normalized_known_board_name((meta or {}).get("board"))
    if known_meta_board:
        return known_meta_board
    resolved = resolve_current_submit_artifacts(out_dir, board_name=None)
    resolved_board = _normalized_known_board_name(resolved.get("board_name"))
    if resolved_board:
        return resolved_board
    return "unknown"


def _meta_with_resolved_submit_board(out_dir: Path, meta: dict) -> dict:
    resolved_board = _resolved_submit_board_name(out_dir, meta)
    if resolved_board == "unknown":
        return dict(meta)
    enriched_meta = dict(meta)
    enriched_meta["board"] = resolved_board
    return enriched_meta


def _verify_generated_answers_or_raise(
    *,
    out_dir: Path,
    meta: dict,
    question_specs: list[dict],
    answers: dict[str, object],
    application_profile: ApplicationProfile,
    deterministic_field_names: set[str],
    user_provided_field_names: set[str] | None = None,
    answer_provider: str | None = None,
    source_bundle: dict | None = None,
) -> dict:
    verification = verify_generated_answers(
        out_dir=out_dir,
        meta=meta,
        question_specs=question_specs,
        answers=answers,
        application_profile=application_profile,
        deterministic_field_names=deterministic_field_names,
        user_provided_field_names=user_provided_field_names,
        answer_provider=answer_provider,
        source_bundle=source_bundle,
    )
    blockers = list(verification.get("blockers") or [])
    if blockers:
        pending_path = role_submit_dir(out_dir) / PENDING_USER_INPUT_JSON
        if not pending_path.exists():
            artifacts: dict[str, object] = {}
            answer_verification_json_path = role_submit_path(out_dir, ANSWER_VERIFICATION_JSON)
            if answer_verification_json_path.exists():
                artifacts["answer_verification_json"] = str(answer_verification_json_path)
            answer_verification_raw_path = role_submit_path(out_dir, ANSWER_VERIFICATION_RAW)
            if answer_verification_raw_path.exists():
                artifacts["answer_verification_raw"] = str(answer_verification_raw_path)
            write_pending_user_input_for_unconfirmed_fields(
                out_dir,
                board=_resolved_submit_board_name(out_dir, meta),
                fields=blockers,
                artifacts=artifacts or None,
            )
        raise GeneratedAnswerBlockersError(blockers, valid_answers=answers)
    return verification


def generate_application_answers(
    *, out_dir: Path, meta: dict, question_specs: list[dict], provider: str | None = None
) -> dict[str, object]:
    migrate_role_output_layout(out_dir)
    meta = _meta_with_resolved_submit_board(out_dir, meta)
    if not question_specs:
        clear_answer_generation_artifacts(out_dir)
        return {}

    def _persist_generated_answer_blockers(blockers: list[dict]) -> None:
        pending_path = role_submit_dir(out_dir) / PENDING_USER_INPUT_JSON
        if pending_path.exists():
            return
        artifacts: dict[str, object] = {}
        answer_verification_json_path = role_submit_path(out_dir, ANSWER_VERIFICATION_JSON)
        if answer_verification_json_path.exists():
            artifacts["answer_verification_json"] = str(answer_verification_json_path)
        answer_verification_raw_path = role_submit_path(out_dir, ANSWER_VERIFICATION_RAW)
        if answer_verification_raw_path.exists():
            artifacts["answer_verification_raw"] = str(answer_verification_raw_path)
        write_pending_user_input_for_unconfirmed_fields(
            out_dir,
            board=_resolved_submit_board_name(out_dir, meta),
            fields=blockers,
            artifacts=artifacts or None,
        )

    def _raise_generated_answer_blockers(
        blockers: list[dict], *, valid_answers: dict[str, object] | None = None
    ) -> None:
        _persist_generated_answer_blockers(blockers)
        raise GeneratedAnswerBlockersError(blockers, valid_answers=valid_answers)

    chosen_provider = provider or default_answer_provider()
    raw_output_path = role_submit_path(out_dir, APPLICATION_ANSWER_RAW)
    answers_path = role_submit_path(out_dir, APPLICATION_ANSWER_CACHE)
    raw_output_path.parent.mkdir(parents=True, exist_ok=True)
    refresh_request_id = current_answer_refresh_request_id(out_dir)
    force_fresh_generation = refresh_request_id is not None
    if force_fresh_generation:
        clear_answer_generation_artifacts(out_dir)
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
        _raise_generated_answer_blockers(linked_resource_blockers, valid_answers={})
    master_resume_text = MASTER_RESUME_PATH.read_text(encoding="utf-8")
    work_stories_text = WORK_STORIES_PATH.read_text(encoding="utf-8")
    candidate_context_text = CANDIDATE_CONTEXT_PATH.read_text(encoding="utf-8")
    application_profile_text = APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
    application_profile = parse_application_profile(application_profile_text)
    classified_shared_answers = _classified_shared_answers(
        question_specs,
        application_profile,
        master_resume_text=master_resume_text,
        work_stories_text=work_stories_text,
        candidate_context_text=candidate_context_text,
        company_name=meta.get("company"),
    )
    optional_skipped_field_names = {
        str(spec.get("field_name") or "").strip()
        for spec in question_specs
        if should_skip_optional_generated_answer(spec)
    }
    reviewable_question_specs = [
        spec
        for spec in question_specs
        if str(spec.get("field_name") or "").strip() not in linked_resource_deterministic
        and str(spec.get("field_name") or "").strip() not in classified_shared_answers
        and str(spec.get("field_name") or "").strip() not in optional_skipped_field_names
    ]
    provider_question_specs, provider_field_name_map = canonicalize_provider_question_specs(reviewable_question_specs)
    jd_parsed = (
        load_optional_json(role_content_path(out_dir, "jd_parsed.json"))
        or load_optional_json(out_dir / "jd_parsed.json")
        or {}
    )
    resume_content = load_optional_json(role_content_path(out_dir, "resume_content.json")) or load_optional_json(
        out_dir / "resume_content.json"
    )
    research_cache = load_optional_json(PROJECT_ROOT / "output" / meta["company"] / "research_cache.json") or {}
    role_research = load_optional_json(role_content_path(out_dir, "role_research_cache.json")) or load_optional_json(
        out_dir / "role_research_cache.json"
    )
    if role_research and "role_context" in role_research:
        research_cache = {**research_cache, "role_context": role_research["role_context"]}
    verifier_source_bundle = _build_answer_verification_source_bundle(
        out_dir=out_dir,
        application_profile_text=application_profile_text,
        master_resume_text=master_resume_text,
        work_stories_text=work_stories_text,
        candidate_context_text=candidate_context_text,
        jd_parsed=jd_parsed,
        resume_content=resume_content,
        research_cache=research_cache,
        linked_resource_payload=linked_resource_payload,
    )
    deterministic_field_names = (
        set(linked_resource_deterministic) | set(classified_shared_answers) | optional_skipped_field_names
    )
    draft_override_field_names = set(load_draft_overrides(out_dir))

    def _validated_answers_from_raw(
        raw_answer_payload: dict[str, object] | None,
    ) -> tuple[dict[str, object], list[dict]]:
        merged_answers = {**(raw_answer_payload or {}), **linked_resource_deterministic, **classified_shared_answers}
        merged_answers = normalize_multi_select_generated_answers(question_specs, merged_answers)
        validated_answers, validation_blockers = validate_generated_answers_with_blockers(
            question_specs,
            merged_answers,
            application_profile=application_profile,
        )
        validated_answers = apply_draft_overrides(question_specs, validated_answers, out_dir=out_dir)
        validated_answers = normalize_multi_select_generated_answers(question_specs, validated_answers)
        return validate_generated_answers_with_blockers(
            question_specs,
            validated_answers,
            application_profile=application_profile,
        )

    if not provider_question_specs:
        answers, blockers = _validated_answers_from_raw({})
        if blockers:
            _raise_generated_answer_blockers(blockers, valid_answers=answers)
        deterministic_provider = _deterministic_answer_provider_name(
            linked_resource_answers=linked_resource_deterministic,
            classified_answers=classified_shared_answers,
        )
        verification = _verify_generated_answers_or_raise(
            out_dir=out_dir,
            meta=meta,
            question_specs=question_specs,
            answers=answers,
            application_profile=application_profile,
            deterministic_field_names=deterministic_field_names,
            user_provided_field_names=draft_override_field_names,
            answer_provider=deterministic_provider,
            source_bundle=verifier_source_bundle,
        )
        if _verification_retry_feedback_by_field(verification):
            _raise_generated_answer_blockers(
                _verifier_retry_feedback_blockers(
                    question_specs=question_specs,
                    answers=answers,
                    verification=verification,
                ),
                valid_answers=answers,
            )
        _write_application_answers_payload(
            answers_path,
            question_specs=question_specs,
            answers=answers,
            provider=deterministic_provider,
            refresh_request_id=refresh_request_id,
            linked_resource_payload=linked_resource_payload,
        )
        return answers

    verifier_retry_feedback_by_field: dict[str, list[str]] = {}
    if not force_fresh_generation:
        current_answers_payload = load_optional_json(answers_path)
        cached_answers = load_cached_application_answers(
            answers_path,
            question_specs,
            linked_resource_cache_key=linked_resource_cache_key,
        )
        if cached_answers is not None:
            original_cached_answers = dict(cached_answers)
            cached_answers, blockers = _validated_answers_from_raw(cached_answers)
            if blockers:
                cached_answers = None
            else:
                if cached_answers != original_cached_answers:
                    _write_application_answers_payload(
                        answers_path,
                        question_specs=question_specs,
                        answers=cached_answers,
                        provider=str((current_answers_payload or {}).get("provider") or chosen_provider),
                        refresh_request_id=refresh_request_id,
                        linked_resource_payload=linked_resource_payload,
                        existing_payload=current_answers_payload,
                    )
        if cached_answers is not None:
            verification = _verify_generated_answers_or_raise(
                out_dir=out_dir,
                meta=meta,
                question_specs=question_specs,
                answers=cached_answers,
                application_profile=application_profile,
                deterministic_field_names=deterministic_field_names,
                user_provided_field_names=draft_override_field_names,
                answer_provider=chosen_provider,
                source_bundle=verifier_source_bundle,
            )
            verifier_retry_feedback_by_field = _verification_retry_feedback_by_field(verification)
            if not verifier_retry_feedback_by_field:
                return cached_answers
        if not verifier_retry_feedback_by_field:
            cached_answers_path = find_matching_cached_application_answers_path(
                out_dir,
                current_answers_path=answers_path,
                question_specs=question_specs,
                linked_resource_cache_key=linked_resource_cache_key,
            )
            if cached_answers_path is not None:
                cached_payload = load_optional_json(cached_answers_path)
                if cached_payload:
                    answers_path.write_text(json_dumps_pretty(cached_payload) + "\n", encoding="utf-8")
                cached_answers = load_cached_application_answers(
                    cached_answers_path,
                    question_specs,
                    linked_resource_cache_key=linked_resource_cache_key,
                )
                if cached_answers is not None:
                    original_cached_answers = dict(cached_answers)
                    cached_answers, blockers = _validated_answers_from_raw(cached_answers)
                    if blockers:
                        cached_answers = None
                    else:
                        if cached_answers != original_cached_answers:
                            _write_application_answers_payload(
                                answers_path,
                                question_specs=question_specs,
                                answers=cached_answers,
                                provider=str((cached_payload or {}).get("provider") or chosen_provider),
                                refresh_request_id=refresh_request_id,
                                linked_resource_payload=linked_resource_payload,
                                existing_payload=cached_payload,
                            )
                if cached_answers is not None:
                    verification = _verify_generated_answers_or_raise(
                        out_dir=out_dir,
                        meta=meta,
                        question_specs=question_specs,
                        answers=cached_answers,
                        application_profile=application_profile,
                        deterministic_field_names=deterministic_field_names,
                        user_provided_field_names=draft_override_field_names,
                        answer_provider=chosen_provider,
                        source_bundle=verifier_source_bundle,
                    )
                    verifier_retry_feedback_by_field = _verification_retry_feedback_by_field(verification)
                    if not verifier_retry_feedback_by_field:
                        return cached_answers
    cover_letter_text = find_cover_letter_text(out_dir)
    # Observability: warn if any question could have been classified deterministically
    from question_classifier import classify_question

    for spec in question_specs:
        if classify_question(spec.get("label", "")) is not None:
            logger.warning("Classified question reached LLM generation: %s", spec.get("label"))
    base_prompt = build_application_answers_prompt(
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
    prompt = _augment_answer_generation_prompt_with_verifier_feedback(
        base_prompt,
        question_specs=reviewable_question_specs,
        retry_feedback_by_field=verifier_retry_feedback_by_field,
    )
    timeout_seconds = provider_timeout_seconds()
    raw_answers, error = _run_answer_generation_provider(
        provider=chosen_provider,
        prompt=prompt,
        raw_output_path=raw_output_path,
        timeout_seconds=timeout_seconds,
        question_specs=provider_question_specs,
        request_id=refresh_request_id,
    )
    raw_answers = restore_provider_answer_field_names(raw_answers, provider_field_name_map)
    final_provider = chosen_provider
    if error is not None:
        fallback_provider = _answer_generation_fallback_provider(chosen_provider)
        if fallback_provider:
            fallback_raw_path = role_submit_path(out_dir, APPLICATION_ANSWER_FALLBACK_RAW)
            raw_answers, fallback_error = _run_answer_generation_provider(
                provider=fallback_provider,
                prompt=prompt,
                raw_output_path=fallback_raw_path,
                timeout_seconds=timeout_seconds,
                question_specs=provider_question_specs,
                request_id=refresh_request_id,
            )
            raw_answers = restore_provider_answer_field_names(raw_answers, provider_field_name_map)
            if fallback_error is None:
                final_provider = fallback_provider
                error = None
            else:
                raise RuntimeError(
                    f"{error} Fallback answer generation via {fallback_provider} also failed. "
                    f"See {fallback_raw_path} for details."
                ) from fallback_error
        else:
            raise error

    answers, blockers = _validated_answers_from_raw(raw_answers)
    if blockers:
        retry_raw_answers, retry_error = _run_answer_generation_provider(
            provider=final_provider,
            prompt=prompt,
            raw_output_path=raw_output_path,
            timeout_seconds=timeout_seconds,
            question_specs=provider_question_specs,
            request_id=refresh_request_id,
        )
        retry_raw_answers = restore_provider_answer_field_names(retry_raw_answers, provider_field_name_map)
        if retry_error is None:
            answers, blockers = _validated_answers_from_raw(retry_raw_answers)
        if blockers:
            _raise_generated_answer_blockers(blockers, valid_answers=answers)
    verification = _verify_generated_answers_or_raise(
        out_dir=out_dir,
        meta=meta,
        question_specs=question_specs,
        answers=answers,
        application_profile=application_profile,
        deterministic_field_names=deterministic_field_names,
        user_provided_field_names=draft_override_field_names,
        answer_provider=final_provider,
        source_bundle=verifier_source_bundle,
    )
    verifier_retry_feedback_by_field = _verification_retry_feedback_by_field(verification)
    if verifier_retry_feedback_by_field:
        retry_prompt = _augment_answer_generation_prompt_with_verifier_feedback(
            base_prompt,
            question_specs=reviewable_question_specs,
            retry_feedback_by_field=verifier_retry_feedback_by_field,
        )
        retry_raw_answers, retry_error = _run_answer_generation_provider(
            provider=final_provider,
            prompt=retry_prompt,
            raw_output_path=raw_output_path,
            timeout_seconds=timeout_seconds,
            question_specs=provider_question_specs,
            request_id=refresh_request_id,
        )
        if retry_error is not None:
            raise retry_error
        retry_raw_answers = restore_provider_answer_field_names(retry_raw_answers, provider_field_name_map)
        answers, blockers = _validated_answers_from_raw(retry_raw_answers)
        if blockers:
            _raise_generated_answer_blockers(blockers, valid_answers=answers)
        verification = _verify_generated_answers_or_raise(
            out_dir=out_dir,
            meta=meta,
            question_specs=question_specs,
            answers=answers,
            application_profile=application_profile,
            deterministic_field_names=deterministic_field_names,
            user_provided_field_names=draft_override_field_names,
            answer_provider=final_provider,
            source_bundle=verifier_source_bundle,
        )
        final_retry_feedback_by_field = _verification_retry_feedback_by_field(verification)
        if final_retry_feedback_by_field:
            fallback_overrides = build_verifier_retry_fallback_answers(
                question_specs=question_specs,
                answers=answers,
                retry_feedback_by_field=final_retry_feedback_by_field,
                jd_parsed=jd_parsed,
                research_cache=research_cache,
                master_resume_text=master_resume_text,
            )
            optional_blank_overrides = build_optional_retry_blank_fallback_answers(
                question_specs=question_specs,
                answers=answers,
                retry_feedback_by_field=final_retry_feedback_by_field,
            )
            combined_fallback_overrides: dict[str, object] = {
                **optional_blank_overrides,
                **fallback_overrides,
            }
            if combined_fallback_overrides:
                answers, blockers = _validated_answers_from_raw({**answers, **combined_fallback_overrides})
                if blockers:
                    _raise_generated_answer_blockers(blockers, valid_answers=answers)
                verification = _verify_generated_answers_or_raise(
                    out_dir=out_dir,
                    meta=meta,
                    question_specs=question_specs,
                    answers=answers,
                    application_profile=application_profile,
                    deterministic_field_names=deterministic_field_names,
                    user_provided_field_names=draft_override_field_names,
                    answer_provider=final_provider,
                    source_bundle=verifier_source_bundle,
                )
                final_retry_feedback_by_field = _verification_retry_feedback_by_field(verification)
                if not final_retry_feedback_by_field:
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
                    answers_path.write_text(json_dumps_pretty(answers_payload) + "\n", encoding="utf-8")
                    return answers
            _raise_generated_answer_blockers(
                _verifier_retry_feedback_blockers(
                    question_specs=question_specs,
                    answers=answers,
                    verification=verification,
                ),
                valid_answers=answers,
            )
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
    answers_path.write_text(json_dumps_pretty(answers_payload) + "\n", encoding="utf-8")
    return answers


_CURRENT_COMPANY_EXCLUSION_FRAGMENTS = (
    "background check",
    "as part of our hiring process",
    "pre-employment",
    "verification from your",
    "acknowledge",
    "i acknowledge",
    "non-compete",
    "non compete",
    "noncompete",
    "non-disclosure",
    "nondisclosure",
    "non-solicitation",
    "restrictive covenant",
    "notice period",
    "employment agreement",
    "post-employment",
)


def question_is_current_company_field(
    *,
    field_name: str | None = None,
    label: str | None = None,
) -> bool:
    normalized_field_name = slugify_label(field_name or "")
    if normalized_field_name in CURRENT_COMPANY_FIELD_NAMES:
        return True
    if label:
        norm_label = normalize_text(label)
        # Exclude acknowledgment/consent labels that happen to mention
        # "current employer" incidentally (e.g. background-check consent).
        if any(normalize_text(exc) in norm_label for exc in _CURRENT_COMPANY_EXCLUSION_FRAGMENTS):
            return False
    return any(
        normalize_text(fragment) in normalize_text(label)
        for fragment in CURRENT_COMPANY_LABEL_FRAGMENTS
        if fragment and label
    )


_NDA_NONCOMPETE_FRAGMENTS = (
    "non-compete",
    "non compete",
    "noncompete",
    "non-competition",
    "non competition",
    "non-disclosure",
    "non disclosure",
    "nondisclosure",
    "non-solicitation",
    "non solicitation",
    "nonsolicitation",
    "restrictive covenant",
    "restrictions on competition",
    "restrictions that may affect",
)

_CONFLICT_OF_INTEREST_PRIMARY_FRAGMENTS = (
    "significant financial interest",
    "close personal relationship",
    "personal familial relationship",
    "personal familial relationships",
    "outside employment with or receiving compensation",
    "outside business activities",
    "intellectual property ownership",
    "position as a government official",
    "referred or recommended for this position by a government official",
    "served or are serving in a government or public body",
    "referred to this position by a senior leader",
    "referred to this position by a decision maker",
)
_CONFLICT_OF_INTEREST_CONTEXT_FRAGMENTS = (
    "connected to coinbase",
    "institutional client",
    "business partner",
    "supplier",
    "vendor",
    "competitor",
    "government official",
    "government officials",
    "government owned entity",
    "government or public body",
    "regulatory authority",
)

_COMPENSATION_QUESTION_FRAGMENTS = (
    "salary expectation",
    "salary requirement",
    "desired salary",
    "desired compensation",
    "compensation expectation",
    "pay expectation",
    "total rewards",
    "total compensation",
    "expected salary",
    "expected compensation",
    "what are your salary",
    "what is your desired",
    "what are your compensation",
    "salary range",
)

_COMPENSATION_NEGOTIABLE_ANSWER = (
    "I'm open and flexible on compensation. I'd prefer to learn more about "
    "the role's scope and total rewards package before discussing specific numbers. "
    "I'm confident we can find a mutually agreeable arrangement."
)


def _question_is_nda_noncompete(label: str) -> bool:
    """Detect NDA/non-compete/non-solicitation questions."""
    label_lower = label.lower()
    return any(frag in label_lower for frag in _NDA_NONCOMPETE_FRAGMENTS)


def _question_is_conflict_of_interest(label: str) -> bool:
    """Detect conflict-of-interest disclosure questions."""
    normalized = normalize_text(label)
    if not normalized:
        return False
    if any(fragment in normalized for fragment in _OUTSIDE_COMMITMENT_PENDING_INPUT_FRAGMENTS):
        return True
    if "directly employed by" in normalized and any(
        fragment in normalized
        for fragment in (
            "government or military entity",
            "state owned enterprise",
            "publicly funded institution",
            "government contractor",
            "government procurement",
        )
    ):
        return True
    if any(
        fragment in normalized
        for fragment in (
            "relatives or personal relationships working at",
            "relative or personal relationship working at",
            "relatives working at",
            "relatives currently working for",
            "relatives currently working at",
            "personal relationships working at",
            "related to anyone currently employed with",
            "related to anyone who currently works at",
            "related to or in a relationship with anyone that works for",
            "relationship with anyone that works for",
            "family or personal connection with current or former employees at",
            "family or personal connection with current employees at",
            "family or personal connection with former employees at",
            "professional or personal connections to individuals currently employed by",
            "personal connections to individuals currently employed by",
            "professional connections to individuals currently employed by",
        )
    ):
        return True
    if (
        ("family or household" in normalized or "household member" in normalized)
        and any(
            fragment in normalized
            for fragment in (
                "current",
                "currently",
                "works for",
                "working for",
                "employed by",
                "employed with",
            )
        )
        and any(
            fragment in normalized
            for fragment in (
                "employee",
                "employees",
                "contractor",
                "contractors",
                "board member",
                "board members",
            )
        )
    ):
        return True
    if "family or close friend" in normalized and "employed by" in normalized:
        return True
    if "government" in normalized and "public institution" in normalized and "employment experience" in normalized:
        return True
    has_context = any(fragment in normalized for fragment in _CONFLICT_OF_INTEREST_CONTEXT_FRAGMENTS)
    has_primary = any(fragment in normalized for fragment in _CONFLICT_OF_INTEREST_PRIMARY_FRAGMENTS)
    return has_context and (has_primary or re.search(r"\bconflicts?\s+of\s+interest\b", normalized) is not None)


def _question_is_compensation(label: str) -> bool:
    """Detect open-ended compensation/salary expectation questions."""
    normalized = normalize_text(label)
    if any(
        fragment in normalized
        for fragment in (
            "desired start date",
            "when can you start",
            "date available",
            "available date",
            "earliest start",
        )
    ):
        return False
    return any(frag in normalized for frag in _COMPENSATION_QUESTION_FRAGMENTS)


def _question_is_product_usage(label: str) -> bool:
    """Return True for yes/no product usage questions, NOT open-ended ones.

    Matches: "Have you used X before", "Do you use X", "Are you a X user", etc.
    Excludes: "Describe your experience with...", "Tell us about...", "What is your experience..."
    """
    normalized = normalize_text(label)
    if question_requests_ai_agent_and_rag_experience(label) or question_requests_ai_workflow_usage(label):
        return False
    if "pronoun" in normalized or normalized.startswith(("what ", "which ", "where ")):
        return False
    # Exclude open-ended questions first
    if any(
        frag in normalized for frag in ("describe", "tell us about", "what is your experience", "how do you", "explain")
    ):
        return False
    return bool(
        re.search(
            r"(?:have you (?:ever )?used|have you tried|are you a\b.*\buser|do you use|have you ever used)",
            normalized,
        )
    )


_FREE_TEXT_GENERATION_FIELD_TYPES = frozenset({"text", "textarea", "input_text", "string", "longtext"})
_FREE_TEXT_BOOLEAN_GENERATION_CATEGORIES = frozenset(
    {
        "product_usage",
        "experience_confirmation",
        "skill_confirmation",
        "company_engagement",
        "prior_employment",
        "current_employer_affiliation",
        "employee_referral",
        "office_attendance",
        "interview_accommodation",
        "reasonable_accommodation",
        "nda_noncompete",
        "conflict_of_interest",
        "relocation_assistance_requirement",
    }
)
_DETERMINISTIC_FREE_TEXT_CATEGORY_EXCLUSIONS = frozenset(
    {
        "education",
        "work_authorization",
        "compensation",
        "travel_percentage",
        "availability_timing",
        "undergraduate_gpa",
        "how_did_you_hear",
        "ai_captcha",
    }
)
_FREE_TEXT_DETAIL_PROMPT_FRAGMENTS = (
    "if yes",
    "if no",
    "if none",
    "if you selected yes",
    "if selected yes",
    "if this does not apply",
    "please provide details",
    "provide details",
    "please explain",
    "please describe",
    "please list",
    "please share",
    "please specify",
    "what is the address from which you plan on working",
    'type "relocating"',
    "type 'relocating'",
)


def _shared_text_answer_overrides_generation_gate(answer: str | None) -> bool:
    normalized = normalize_text(answer)
    if not normalized:
        return False
    if normalized in {"yes", "no", "none", "n a", "na", "n/a"}:
        return False
    if "http" in normalized or "www " in normalized:
        return True
    if "\n" in str(answer or ""):
        return True
    return len(normalized) >= 20


def question_prefers_generated_free_text_answer(
    text: str | None,
    *,
    field_type: str | None,
    category: str | None = None,
    policy=None,
) -> bool:
    """Return True when a free-text field should not collapse to bare Yes/No."""

    normalized_field_type = str(field_type or "").strip().casefold()
    if normalized_field_type not in _FREE_TEXT_GENERATION_FIELD_TYPES:
        return False

    normalized = normalize_text(text)
    if not normalized:
        return False

    if category is None:
        from question_classifier import classify_question

        category = classify_question(text, field_type=field_type)

    if category in _DETERMINISTIC_FREE_TEXT_CATEGORY_EXCLUSIONS:
        return False

    if (
        policy is not None
        and bool(getattr(policy, "is_positive_fit", False))
        and getattr(policy, "text_value", None) in {"Yes", "No"}
    ):
        return True

    if category in _FREE_TEXT_BOOLEAN_GENERATION_CATEGORIES:
        return True

    return any(fragment in normalized for fragment in _FREE_TEXT_DETAIL_PROMPT_FRAGMENTS)


def _question_is_pm_people_management(label: str) -> bool:
    normalized = normalize_text(label)
    if not normalized:
        return False
    if "product manager" not in normalized:
        return False
    return any(fragment in normalized for fragment in ("managing a team", "manage a team", "managed a team"))


def _question_is_location_cost_tier(label: str) -> bool:
    normalized = normalize_text(label)
    if not normalized:
        return False
    return "cost tier" in normalized and "location" in normalized


def _question_is_city_location(label: str) -> bool:
    """Return True if the question asks which city/location the candidate is available to work in."""
    normalized = normalize_text(label)
    if "commuting distance" in normalized or "reasonable commute" in normalized:
        return False
    if "relocat" in normalized and any(
        fragment in normalized for fragment in ("office location", "job location", "specified office location")
    ):
        return False
    if normalized.startswith(("do you", "are you", "would you", "will you")) and any(
        fragment in normalized
        for fragment in (
            "commuting distance",
            "reasonable commute",
            "currently reside",
            "currently live",
            "live within",
            "located within",
            "based within",
            "relocat",
        )
    ):
        return False
    if re.search(r"\bwhich\b.*\b(?:office|location)\b.*\bclosest to\b", normalized):
        return True
    if re.search(r"\bwhich\b.*\blocation\b.*\bapplying for\b", normalized):
        return True
    return any(
        fragment in normalized
        for fragment in (
            "what cities",
            "in what cities",
            "which city",
            "which office",
            "available to work",
            "preferred office",
            "preferred location",
            "office location",
            "work location",
        )
    )


_US_STATE_ABBREVIATIONS = {
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
_US_STATE_NAMES = {normalize_text(name): abbrev for abbrev, name in _US_STATE_ABBREVIATIONS.items()}


def _location_state_abbreviation(location: str | None) -> str:
    if not location:
        return ""
    parts = [part.strip() for part in str(location).split(",") if part.strip()]
    candidates = [*parts[1:], parts[0] if len(parts) == 1 else ""]
    for candidate in candidates:
        token = candidate.strip()
        if not token:
            continue
        upper = token.upper()
        if upper in _US_STATE_ABBREVIATIONS:
            return upper
        normalized = normalize_text(token)
        abbrev = _US_STATE_NAMES.get(normalized)
        if abbrev:
            return abbrev
    return ""


def _parse_age_bounds(value: str | None) -> tuple[int | None, int | None] | None:
    text = str(value or "").strip()
    if not text:
        return None
    if match := re.search(r"(\d+)\s*[-–]\s*(\d+)", text):
        return int(match.group(1)), int(match.group(2))
    if match := re.search(r"(\d+)\s*(?:and|or)\s+(?:over|older)", text, flags=re.I):
        return int(match.group(1)), None
    if match := re.search(r"(\d+)\s*(?:and|or)\s+(?:under|younger)", text, flags=re.I):
        return None, int(match.group(1))
    if match := re.search(r"\bunder\s+(\d+)\b", text, flags=re.I):
        return None, int(match.group(1)) - 1
    if match := re.search(r"\b(\d+)\+\b", text):
        return int(match.group(1)), None
    if match := re.search(r"\b(\d+)\b", text):
        age = int(match.group(1))
        return age, age
    return None


def _age_bounds_contain(bounds: tuple[int | None, int | None] | None, age: int) -> bool:
    if bounds is None:
        return False
    lower, upper = bounds
    if lower is not None and age < lower:
        return False
    return not (upper is not None and age > upper)


def select_truthful_age_option(option_labels: list[str], age_range: str | None) -> str | None:
    if not age_range:
        return None

    normalized_options = [(label, normalize_text(label)) for label in option_labels if str(label or "").strip()]
    normalized_age_range = normalize_text(age_range)
    if normalized_age_range:
        for label, normalized_label in normalized_options:
            if normalized_label == normalized_age_range:
                return label

    desired_bounds = _parse_age_bounds(age_range)
    if desired_bounds is None:
        return None

    desired_ages = [age for age in desired_bounds if age is not None]
    for desired_age in desired_ages:
        for label, _ in normalized_options:
            option_bounds = _parse_age_bounds(label)
            if _age_bounds_contain(option_bounds, desired_age):
                return label
    return None


def _best_city_option(options: list[str], role_location: str | None, candidate_location: str) -> str | None:
    """Pick the best city option, preferring the candidate's San Francisco choice when offered."""
    if not options:
        return None
    normalized_options = [(o, normalize_text(o)) for o in options]

    candidate_city = candidate_location.split(",")[0].strip().lower()
    if candidate_city == "san francisco":
        for original, norm in normalized_options:
            if "san francisco" in norm:
                return original

    # Prefer the candidate's city over the role city for office-preference prompts.
    if candidate_city:
        for original, norm in normalized_options:
            if candidate_city in norm:
                return original

    candidate_state = _location_state_abbreviation(candidate_location)
    if candidate_state:
        for original, _ in normalized_options:
            if _location_state_abbreviation(original) == candidate_state:
                return original

    if role_location:
        role_city = role_location.split(",")[0].strip().lower()
        if role_city:
            for original, norm in normalized_options:
                if role_city in norm:
                    return original

    return None


def load_draft_overrides(out_dir: Path | None = None) -> dict[str, object]:
    if not out_dir:
        return {}

    def _read_draft_overrides(path: Path) -> dict[str, object]:
        if not path.exists():
            return {}
        try:
            draft_overrides = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(field_name): value for field_name, value in draft_overrides.items()}

    def _serializable_override_key(value: object) -> str | None:
        try:
            return json.dumps(value, sort_keys=True)
        except TypeError:
            return None

    inherited_overrides: dict[str, object] = {}
    company_dir = out_dir.parent
    current_path = out_dir / "draft_overrides.json"
    if company_dir.exists():
        inherited_candidates: dict[str, dict[str, object]] = {}
        for candidate_path in sorted(company_dir.glob("*/draft_overrides.json")):
            if candidate_path == current_path:
                continue
            for field_name, value in _read_draft_overrides(candidate_path).items():
                serialized_value = _serializable_override_key(value)
                if serialized_value is None:
                    continue
                inherited_candidates.setdefault(field_name, {})[serialized_value] = value
        inherited_overrides = {
            field_name: next(iter(candidates.values()))
            for field_name, candidates in inherited_candidates.items()
            if len(candidates) == 1
        }

    local_overrides = _read_draft_overrides(current_path)
    try:
        inherited_overrides.update(local_overrides)
    except Exception:
        return local_overrides
    return inherited_overrides


def apply_draft_overrides(
    question_specs: list[dict],
    answers: dict[str, object],
    *,
    out_dir: Path | None = None,
) -> dict[str, object]:
    """Apply user draft overrides from ``draft_overrides.json``.

    This is the last-mile override: user edits take highest precedence and are
    applied after all deterministic classification and LLM generation.
    """
    overridden = dict(answers)
    for field_name, value in load_draft_overrides(out_dir).items():
        overridden[field_name] = value  # Add even if not already present
    return overridden


def _question_is_company_engagement(label: str) -> bool:
    """Return True if the question asks about engagement/interaction with the company."""
    normalized = normalize_text(label)
    if normalized.startswith("are you familiar with "):
        subject = normalized.removeprefix("are you familiar with ")
        subject = re.split(r"\b(?:we|let|please|if|whether)\b", subject, maxsplit=1)[0].strip()
        subject = re.sub(r"\bplease provide details\b.*$", "", subject).strip()
        subject_words = [word for word in re.findall(r"[a-z0-9]+", subject) if word not in {"a", "an", "the"}]
        generic_subject_words = {
            "ai",
            "automation",
            "cloud",
            "code",
            "data",
            "deployment",
            "design",
            "different",
            "engineering",
            "feature",
            "features",
            "gitops",
            "infrastructure",
            "learning",
            "machine",
            "ml",
            "modern",
            "operations",
            "pipelines",
            "platform",
            "platforms",
            "process",
            "product",
            "products",
            "requirements",
            "software",
            "strategies",
            "strategy",
            "system",
            "systems",
            "technical",
            "technologies",
            "technology",
            "tool",
            "tools",
            "types",
            "workflow",
            "workflows",
            "workload",
            "workloads",
        }
        if any(
            fragment in subject
            for fragment in ("as a company", "the company", "our company", "our product", "our products")
        ):
            return True
        return 0 < len(subject_words) <= 3 and not any(word in generic_subject_words for word in subject_words)
    if "how familiar were you with " in normalized:
        return True
    return any(
        fragment in normalized
        for fragment in ("engaged with", "interacted with", "been exposed to", "how have you interacted")
    )


def _question_is_how_did_you_hear(label: str | None) -> bool:
    normalized = normalize_text(label)
    if not normalized:
        return False
    return any(
        fragment in normalized
        for fragment in (
            "how did you hear about",
            "how did you first hear about",
            "how did you first learn about",
            "how did you hear of",
            "how did you learn about",
            "how did you initially hear about",
            "how did you find out about",
            "how did you find this position",
            "how did you find this role",
            "how did you find this opportunity",
            "how did you find this job",
            "where did you hear about",
        )
    )


_COMPANY_VARIANT_STOPWORDS = frozenset(
    {
        "and",
        "co",
        "company",
        "corp",
        "corporation",
        "group",
        "holdings",
        "inc",
        "incorporated",
        "limited",
        "llc",
        "ltd",
        "plc",
        "software",
        "systems",
        "technologies",
    }
)
_HOW_DID_YOU_HEAR_QUERY_KEYS = ("utm_source", "source", "ref", "referrer")
_GENERIC_JOB_BOARD_SOURCE_CANDIDATES = (
    "Other Job Board",
    "Other - Job Site",
    "Other Job Site",
    "Job Site",
    "Job Search",
    "Other - Job Search",
    "Job Board",
    "Job Boards",
)
_JOB_SOURCE_DISPLAY_CANDIDATES: dict[str, tuple[str, ...]] = {
    "linkedin": ("LinkedIn", "Linkedin", "Social Networking", "Social Media", "Social"),
    "indeed": ("Indeed", *_GENERIC_JOB_BOARD_SOURCE_CANDIDATES),
    "glassdoor": ("Glassdoor", *_GENERIC_JOB_BOARD_SOURCE_CANDIDATES),
    "ziprecruiter": ("ZipRecruiter", *_GENERIC_JOB_BOARD_SOURCE_CANDIDATES),
    "dice": ("Dice", *_GENERIC_JOB_BOARD_SOURCE_CANDIDATES),
    "trueup": ("TrueUp", "True Up", *_GENERIC_JOB_BOARD_SOURCE_CANDIDATES),
    "jackandjill": ("Jack & Jill", "Jack and Jill", *_GENERIC_JOB_BOARD_SOURCE_CANDIDATES),
    "wellfound": ("Wellfound", "WellFound", "AngelList", *_GENERIC_JOB_BOARD_SOURCE_CANDIDATES),
    "builtin": ("Built In", "BuiltIn", "Builtin", *_GENERIC_JOB_BOARD_SOURCE_CANDIDATES),
    "purposejobs": ("Purpose Jobs", "PurposeJobs", *_GENERIC_JOB_BOARD_SOURCE_CANDIDATES),
    "google_search": ("Google Search", "Internet Search", "Internet", "Search Engine"),
}


def _company_source_name_variants(company_name: str | None) -> list[str]:
    text = str(company_name or "").strip()
    if not text:
        return []
    tokens = [token for token in re.split(r"[^A-Za-z0-9]+", text) if token]
    filtered = [token for token in tokens if token.casefold() not in _COMPANY_VARIANT_STOPWORDS]
    if len(filtered) >= 2 and filtered[0].isupper() and len(filtered[0]) <= 4:
        filtered = filtered[1:]
    variants: list[str] = []
    if filtered:
        variants.append(" ".join(filtered))
        if len(filtered) > 1:
            variants.append(filtered[0])
    elif tokens:
        variants.append(" ".join(tokens))
    deduped: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        normalized = normalize_text(variant)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(variant)
    return deduped


def _normalize_job_source_identifier(value: str | None) -> str:
    normalized = normalize_text(value)
    if not normalized:
        return ""
    if normalized == "direct":
        return "direct"
    if any(
        fragment in normalized
        for fragment in (
            "corporate website",
            "company website",
            "career site",
            "careers site",
            "careers page",
            "company source",
        )
    ):
        return "direct"
    if "linkedin" in normalized:
        return "linkedin"
    if "indeed" in normalized:
        return "indeed"
    if "glassdoor" in normalized:
        return "glassdoor"
    if "ziprecruiter" in normalized:
        return "ziprecruiter"
    if re.search(r"(?<![a-z0-9])dice(?![a-z0-9])", normalized):
        return "dice"
    if "trueup" in normalized:
        return "trueup"
    if "jackandjill" in normalized or ("jack" in normalized and "jill" in normalized):
        return "jackandjill"
    if "wellfound" in normalized or "angelist" in normalized or "angel list" in normalized:
        return "wellfound"
    if "built in" in normalized or "builtin" in normalized:
        return "builtin"
    if "purpose jobs" in normalized or "purposejobs" in normalized:
        return "purposejobs"
    if "google search" in normalized:
        return "google_search"
    return ""


def _job_source_pairs_from_value(value: str | None, source_label: str) -> list[tuple[str, str]]:
    text = str(value or "").strip()
    if not text:
        return []

    if "://" not in text:
        identifier = _normalize_job_source_identifier(text)
        return [(identifier, source_label)] if identifier else []

    try:
        parsed = urlparse(text)
    except ValueError:
        return []

    if not parsed.scheme or not parsed.netloc:
        return []

    pairs: list[tuple[str, str]] = []
    for query_key in _HOW_DID_YOU_HEAR_QUERY_KEYS:
        for raw_value in parse_qs(parsed.query).get(query_key, []):
            identifier = _normalize_job_source_identifier(raw_value)
            if identifier:
                pairs.append((identifier, f"{source_label}.{query_key}"))

    try:
        from url_resolver import detect_source

        detected_source = detect_source(text)
    except Exception:
        detected_source = "unknown"

    if detected_source not in {"unknown", "direct"}:
        pairs.append((detected_source, source_label))

    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for identifier, evidence_source in pairs:
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        deduped.append((identifier, evidence_source))
    return deduped


def _metadata_how_did_you_hear_candidates(
    *,
    company_name: str | None = None,
    job_url: str | None = None,
    source_url: str | None = None,
    source_hint: str | None = None,
) -> tuple[list[str], str | None]:
    ordered_pairs: list[tuple[str, str]] = []
    ordered_pairs.extend(_job_source_pairs_from_value(source_hint, "job.source"))
    ordered_pairs.extend(_job_source_pairs_from_value(source_url, "job.source_url"))
    ordered_pairs.extend(_job_source_pairs_from_value(job_url, "job_url"))

    chosen_identifier = ""
    chosen_source: str | None = None
    for identifier, evidence_source in ordered_pairs:
        if identifier:
            chosen_identifier = identifier
            chosen_source = evidence_source
            break

    if not chosen_identifier:
        return [], None

    if chosen_identifier == "direct":
        return _company_website_how_did_you_hear_candidates(company_name), chosen_source

    return list(_JOB_SOURCE_DISPLAY_CANDIDATES.get(chosen_identifier, ())), chosen_source


def _company_website_how_did_you_hear_candidates(company_name: str | None = None) -> list[str]:
    company_variants = _company_source_name_variants(company_name)
    domain_candidates: list[str] = []
    for variant in company_variants:
        compact = re.sub(r"[^A-Za-z0-9]+", "", str(variant or ""))
        for candidate in (f"{variant}.com", f"{compact}.com", f"{variant}.careers", f"{compact}.careers"):
            text = str(candidate or "").strip()
            if text:
                domain_candidates.append(text)
    candidates = [
        *domain_candidates,
        *(f"{variant} Website" for variant in company_variants),
        *(f"{variant}'s Website" for variant in company_variants),
        *(f"{variant} Careers" for variant in company_variants),
        *(f"{variant}'s Careers" for variant in company_variants),
        *(f"{variant} Career Site" for variant in company_variants),
        *(f"{variant}'s Career Site" for variant in company_variants),
        *(f"{variant} Careers Site" for variant in company_variants),
        *(f"{variant}'s Careers Site" for variant in company_variants),
        *(f"{variant} Source" for variant in company_variants),
        "Corporate website",
        "Company website",
        "Company Website / Careers Page",
        "Company Website / Career Site",
        "Company Website Careers Page",
        "Career Site",
        "Careers Site",
        "Website",
        "Company Source",
    ]
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        text = str(candidate or "").strip()
        normalized_candidate = normalize_text(text)
        if not normalized_candidate or normalized_candidate in seen:
            continue
        seen.add(normalized_candidate)
        deduped.append(text)
    return deduped


def _how_did_you_hear_option_matches_company_website(
    options: Iterable[str] | None,
    *,
    company_name: str | None = None,
) -> bool:
    normalized_options = [normalize_text(option) for option in (options or []) if normalize_text(option)]
    if not normalized_options:
        return False
    for candidate in _company_website_how_did_you_hear_candidates(company_name):
        normalized_candidate = normalize_text(candidate)
        if not normalized_candidate:
            continue
        for normalized_option in normalized_options:
            if (
                normalized_option == normalized_candidate
                or normalized_candidate in normalized_option
                or normalized_option in normalized_candidate
            ):
                return True
    return False


def resolve_how_did_you_hear_candidates(
    application_profile: ApplicationProfile,
    *,
    company_name: str | None = None,
    job_url: str | None = None,
    source_url: str | None = None,
    source_hint: str | None = None,
) -> tuple[list[str], str]:
    metadata_candidates, metadata_source = _metadata_how_did_you_hear_candidates(
        company_name=company_name,
        job_url=job_url,
        source_url=source_url,
        source_hint=source_hint,
    )
    profile_value = (getattr(application_profile, "how_did_you_hear", None) or "Corporate website").strip()
    normalized = normalize_text(profile_value)
    candidates: list[str] = []
    if metadata_candidates:
        candidates.extend(metadata_candidates)
    if profile_value:
        candidates.append(profile_value)
    if any(
        fragment in normalized for fragment in ("corporate website", "company website", "career site", "careers page")
    ):
        candidates.extend(
            [
                "Corporate website",
                "Company website",
                "Company Website / Careers Page",
                "Company Website / Career Site",
                "Company Website Careers Page",
                "Career Site",
                "Careers Site",
                "Website",
                "Company Source",
            ]
        )
        for variant in _company_source_name_variants(company_name):
            candidates.extend(
                [
                    f"{variant} Website",
                    f"{variant} Careers",
                    f"{variant} Career Site",
                    f"{variant} Careers Site",
                    f"{variant} Source",
                ]
            )
    elif "linkedin" in normalized:
        candidates.extend(["LinkedIn", "Linkedin", "Social Networking", "Social Media", "Social"])
    elif "job board" in normalized:
        candidates.extend(["Job Board", "Job Boards", "Indeed", "Built In"])

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        text = str(candidate or "").strip()
        normalized_candidate = normalize_text(text)
        if not normalized_candidate or normalized_candidate in seen:
            continue
        seen.add(normalized_candidate)
        deduped.append(text)
    if metadata_candidates:
        return deduped, metadata_source or "application_profile.md"
    if profile_value:
        return deduped, "application_profile.md"
    return deduped, metadata_source or "application_profile.md"


def build_how_did_you_hear_candidates(
    application_profile: ApplicationProfile,
    company_name: str | None = None,
    *,
    job_url: str | None = None,
    source_url: str | None = None,
    source_hint: str | None = None,
) -> list[str]:
    candidates, _ = resolve_how_did_you_hear_candidates(
        application_profile,
        company_name=company_name,
        job_url=job_url,
        source_url=source_url,
        source_hint=source_hint,
    )
    return candidates


def resolve_how_did_you_hear_option_candidates(
    application_profile: ApplicationProfile,
    options: Iterable[str] | None,
    *,
    company_name: str | None = None,
    job_url: str | None = None,
    source_url: str | None = None,
    source_hint: str | None = None,
    prefer_metadata_job_board_matches: bool = False,
) -> tuple[list[str], str]:
    candidates, source = resolve_how_did_you_hear_candidates(
        application_profile,
        company_name=company_name,
        job_url=job_url,
        source_url=source_url,
        source_hint=source_hint,
    )
    if not candidates or not _how_did_you_hear_option_matches_company_website(options, company_name=company_name):
        return candidates, source
    metadata_candidates, _ = _metadata_how_did_you_hear_candidates(
        company_name=company_name,
        job_url=job_url,
        source_url=source_url,
        source_hint=source_hint,
    )
    if prefer_metadata_job_board_matches and metadata_candidates:
        normalized_options = {normalize_text(option) for option in (options or []) if normalize_text(option)}
        normalized_metadata_candidates = {
            normalize_text(candidate) for candidate in metadata_candidates if normalize_text(candidate)
        }
        if normalized_options & normalized_metadata_candidates:
            return candidates, source
    profile_value = str(getattr(application_profile, "how_did_you_hear", "") or "").strip()
    normalized_profile_value = normalize_text(profile_value)
    company_website_candidates = _company_website_how_did_you_hear_candidates(company_name)
    company_website_candidate_set = {
        normalize_text(candidate) for candidate in company_website_candidates if normalize_text(candidate)
    }
    if normalize_text(candidates[0]) not in company_website_candidate_set and not any(
        fragment in normalized_profile_value
        for fragment in ("corporate website", "company website", "career site", "careers page")
    ):
        return candidates, source
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in [*company_website_candidates, *candidates]:
        text = str(candidate or "").strip()
        normalized_candidate = normalize_text(text)
        if not normalized_candidate or normalized_candidate in seen:
            continue
        seen.add(normalized_candidate)
        deduped.append(text)
    return deduped, source


def build_how_did_you_hear_option_candidates(
    application_profile: ApplicationProfile,
    options: Iterable[str] | None,
    company_name: str | None = None,
    *,
    job_url: str | None = None,
    source_url: str | None = None,
    source_hint: str | None = None,
) -> list[str]:
    candidates, _ = resolve_how_did_you_hear_option_candidates(
        application_profile,
        options,
        company_name=company_name,
        job_url=job_url,
        source_url=source_url,
        source_hint=source_hint,
    )
    return candidates


def _best_engagement_option(options: list[str]) -> str | None:
    """Pick the best engagement option — prefer product/course/platform, then blog/content."""
    normalized_options = [(o, normalize_text(o)) for o in options]
    for original, norm in normalized_options:
        if "heard of it" in norm and any(
            fragment in norm for fragment in ("knew little", "know little", "little about it")
        ):
            return original
    for original, norm in normalized_options:
        if "heard of" in norm and (
            any(fragment in norm for fragment in ("knew little", "know little", "little about"))
            or ("but" in norm and "know much" in norm)
        ):
            return original
    for original, norm in normalized_options:
        if "somewhat familiar" in norm:
            return original
    for original, norm in normalized_options:
        if "familiar" in norm and "not a user" in norm:
            return original
    for original, norm in normalized_options:
        if norm.startswith("yes") and "familiar" in norm:
            return original
    # Priority: product/course/platform usage first, then blog/content
    for keyword in ("course", "product", "platform", "service", "app"):
        for original, norm in normalized_options:
            if keyword in norm:
                return original
    for keyword in ("blog", "content", "article", "newsletter"):
        for original, norm in normalized_options:
            if keyword in norm:
                return original
    return None


def _pm_people_management_support_source(
    *,
    master_resume_text: str | None = None,
    work_stories_text: str | None = None,
    candidate_context_text: str | None = None,
) -> str | None:
    sources = (
        ("master_resume.md", master_resume_text, MASTER_RESUME_PATH),
        ("work_stories.md", work_stories_text, WORK_STORIES_PATH),
        ("candidate_context.md", candidate_context_text, CANDIDATE_CONTEXT_PATH),
    )
    patterns = (
        r"\bmanage(?:d|s|ing)?\b.{0,80}\bproduct managers?\b",
        r"\bproduct managers?\b.{0,80}\bmanage(?:d|s|ing)?\b",
        r"\bpeople manager\b.{0,80}\bproduct managers?\b",
    )
    for source_name, provided_text, default_path in sources:
        try:
            text = provided_text if provided_text is not None else default_path.read_text(encoding="utf-8")
        except OSError:
            continue
        normalized = normalize_text(text)
        if normalized and any(re.search(pattern, normalized) for pattern in patterns):
            return source_name
    return None


_YEAR_COUNT_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}


def _coerce_year_count(value: str | None) -> int | None:
    normalized = normalize_text(value)
    if not normalized:
        return None
    match = re.search(r"\b(\d{1,2})\b", normalized)
    if match:
        return int(match.group(1))
    for word, number in _YEAR_COUNT_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", normalized):
            return number
    return None


def _pm_people_management_year_requirement(text: str | None) -> int | None:
    normalized = normalize_text(text)
    if not normalized or "year" not in normalized:
        return None

    year_value_pattern = (
        r"(?P<value>\d{1,2}|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    )
    patterns = (
        rf"\bat least\s+{year_value_pattern}\s+years?\b",
        rf"\bminimum of\s+{year_value_pattern}\s+years?\b",
        rf"\b{year_value_pattern}\s*\+\s*years?\b",
        rf"\b{year_value_pattern}\s+or\s+more\s+years?\b",
        rf"\b{year_value_pattern}\s*(?:-|to)\s*(?:\d{{1,2}}|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+years?\b",
        rf"\b{year_value_pattern}\s+years?\b",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            return _coerce_year_count(match.group("value"))
    return None


def _location_cost_tier_answer(application_profile: ApplicationProfile) -> str | None:
    normalized_location = normalize_text(application_profile.location)
    if not normalized_location:
        return None

    if any(marker in normalized_location for marker in ("san francisco", "new york", "london")):
        return "High Cost"
    if any(marker in normalized_location for marker in ("los angeles", "seattle", "miami", "boston")):
        return "Mid Cost"
    if any(marker in normalized_location for marker in ("austin", "chicago", "atlanta", "denver")):
        return "Low Cost"
    return "Unknown"


def text_matches_any_fragment(text: str | None, fragments: tuple[str, ...]) -> bool:
    normalized_text = normalize_text(text)
    if not normalized_text:
        return False
    return any(normalize_text(fragment) in normalized_text for fragment in fragments if fragment)


def question_is_salary_comfort_check(text: str | None) -> bool:
    """Return True only for yes/no salary comfort questions, not open-ended salary asks.

    Two-step gate (mirrors Greenhouse's ``_question_is_salary_comfort_check``):
    1. Exact long-form fragments (e.g. "comfortable with the salary range") → immediate match.
    2. Short keyword fragments (e.g. "salary range") require a secondary
       "comfortable" or "interview" keyword so open-ended questions like
       "What is your desired salary range?" are not falsely matched.
    """
    if text_matches_any_fragment(text, SALARY_COMFORT_TEXT_FRAGMENTS):
        return True
    normalized = normalize_text(text)
    if not normalized:
        return False
    if text_matches_any_fragment(text, SALARY_REQUIREMENT_CONFIRMATION_TEXT_FRAGMENTS):
        return any(token in normalized for token in ("salary", "compensation", "pay"))
    # Short-fragment path: keyword + comfort/interview gate
    if not any(frag in normalized for frag in _SALARY_KEYWORD_FRAGMENTS):
        return False
    return "comfortable" in normalized or "interview" in normalized or "accept" in normalized


def question_is_minimum_experience_check(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    number_fragment = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)"
    if any(
        fragment in normalized
        for fragment in (
            "minimum years of experience",
            "minimum years experience",
            "meets minimum years of experience",
            "meets minimum years experience",
            "experience requirement",
        )
    ):
        return True
    return bool(
        re.search(r"\bat least\b.*\byears?\b.*\bexperience\b", normalized)
        or re.search(r"\b\d+\+?\s+years?\s+of\s+(?:\w+\s+)*experience\b", normalized)
        or re.search(rf"\b{number_fragment}\s+(?:or\s+more\s+)?years?\s+of\s+(?:\w+\s+)*experience\b", normalized)
        # Range syntax: "4 to 10 years of experience"
        or re.search(r"\b\d+\s+to\s+\d+\s+years?\s+of\s+.*\bexperience\b", normalized)
    )


def question_requires_pending_user_input(
    text: str | None,
    application_profile: ApplicationProfile | None = None,
) -> str | None:
    normalized = normalize_text(text)
    if not normalized:
        return None
    policy = None
    if application_profile is not None:
        policy = resolve_shared_question_policy(text, application_profile)
        if (
            policy is not None
            and policy.text_value is not None
            and policy.category in {"nda_noncompete", "conflict_of_interest"}
        ):
            if (
                policy.category != "conflict_of_interest"
                or not _question_is_company_relationship_disclosure(text)
                or "department" not in normalized
            ):
                return None
    if _question_is_candidate_ai_guidance_attestation(text):
        return (
            "This question asks the candidate to attest that they reviewed and understand external AI-usage "
            "guidance for the application process, which requires explicit user confirmation."
        )
    if _question_is_company_interview_history(text):
        return (
            "This question asks about the candidate's prior interview history with the company, which is not "
            "tracked in the repo sources and requires explicit user input."
        )
    if _question_is_company_relationship_disclosure(text):
        return (
            "This question asks about personal or family relationships with company employees, which is not "
            "defined in the repo sources and requires explicit user input."
        )
    if _question_is_referral_detail_disclosure(text):
        return (
            "This question asks for employee-referral detail that is not defined in the repo sources and "
            "requires explicit user input."
        )
    if _question_is_work_restriction_or_conflict_disclosure(text):
        return (
            "This question asks about legal work restrictions or conflicts of interest that are not defined in "
            "the repo sources and require explicit user input."
        )
    if _question_is_professional_certification_inventory(text):
        return (
            "This question asks for the candidate's current professional certifications or licenses, which "
            "must be explicitly confirmed from repo sources or by the user before autofill can continue."
        )
    if any(
        fragment in normalized
        for fragment in (
            "how do you pronounce your name",
            "phonetic spelling of your name",
            "pronunciation of your name",
        )
    ):
        return (
            "This question asks for the candidate's personal phonetic name pronunciation, "
            "which is not defined in the repo sources and requires explicit user input."
        )
    if any(fragment in normalized for fragment in _OUTSIDE_COMMITMENT_PENDING_INPUT_FRAGMENTS):
        return (
            "This question asks about ongoing outside commitments or obligations that are not defined in the repo "
            "sources and require explicit user input."
        )
    if any(fragment in normalized for fragment in _FINANCIAL_PRODUCT_DISCLOSURE_PENDING_INPUT_FRAGMENTS):
        return (
            "This question asks for candidate-specific lending, financing, or credit-product experience detail "
            "that is not defined in the repo sources and requires explicit user input."
        )
    asks_for_description = any(
        phrase in normalized
        for phrase in (
            "describe",
            "include market context",
            "what alternative approach",
            "please describe",
        )
    )
    if asks_for_description and any(fragment in normalized for fragment in PENDING_USER_INPUT_TEXT_FRAGMENTS):
        return (
            "This question requests specialized domain detail that should be answered with explicit user input "
            "instead of being inferred automatically."
        )
    if application_profile is not None:
        if policy is not None and policy.text_value is None:
            if policy.category == "work_authorization" and question_is_u_s_person_status(text):
                return (
                    "This question requires an explicit U.S. person legal-status confirmation unless the repo "
                    "sources explicitly confirm citizenship, lawful permanent residency, refugee, or asylee status."
                )
            if policy.category == "work_authorization" and question_is_restricted_country_citizenship_status(text):
                return (
                    "This question requires an explicit confirmation of whether the candidate is a citizen of "
                    "Cuba, Iran, North Korea, or Syria before autofill can continue."
                )
            if policy.category == "compensation":
                return (
                    "This question needs the shared Compensation Expectations value from application_profile.md "
                    "before autofill can continue."
                )
            if policy.category == "travel_percentage":
                return (
                    "This question needs the shared Maximum Travel Percentage value from application_profile.md "
                    "before autofill can continue."
                )
            if policy.category == "interview_recording_consent":
                return (
                    "This question needs the shared Interview Recording Consent value from application_profile.md "
                    "before autofill can continue."
                )
            if policy.category == "availability_timing":
                return (
                    "This question needs the shared Notice Period / Earliest Start Timing values from "
                    "application_profile.md before autofill can continue."
                )
            if policy.category == "skill_years_experience":
                skill_subject = _skill_years_experience_subject(text) or "this skill"
                return (
                    "This question needs the shared years-of-experience value for "
                    f"{skill_subject} from application_profile.md before autofill can continue."
                )
            if policy.category == "undergraduate_gpa":
                return (
                    "This question needs the shared Undergraduate GPA value from application_profile.md "
                    "before autofill can continue."
                )
    return None


def pending_user_input_reason_for_spec(
    spec: dict | None,
    application_profile: ApplicationProfile | None = None,
) -> str | None:
    if not isinstance(spec, dict):
        return None
    if should_skip_optional_generated_answer(spec):
        return None
    combined_text = "\n".join(
        str(part).strip()
        for part in (
            spec.get("label"),
            spec.get("description"),
            spec.get("question"),
        )
        if str(part or "").strip()
    )
    return question_requires_pending_user_input(combined_text, application_profile)


def _profile_city(application_profile: ApplicationProfile) -> str:
    city = (application_profile.location or "").split(",")[0].strip()
    return city or application_profile.location or ""


_TIMING_WORD_TO_INT: dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_AVAILABILITY_DATE_FORMAT_HINTS = (
    "mm/dd/yyyy",
    "mm dd yyyy",
    "mm/dd/yy",
    "mm dd yy",
    "yyyy-mm-dd",
    "yyyy mm dd",
    "dd/mm/yyyy",
    "dd mm yyyy",
    "dd/mm/yy",
    "dd mm yy",
)


def _local_application_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now().astimezone()
    if now.tzinfo is None:
        return now.astimezone()
    return now.astimezone()


def _timing_token_to_int(value: str) -> int | None:
    normalized = normalize_text(value)
    if not normalized:
        return None
    if normalized.isdigit():
        return int(normalized)
    return _TIMING_WORD_TO_INT.get(normalized)


def _timing_offset_days_from_text(value: str | None) -> int | None:
    normalized = normalize_text(value)
    if not normalized:
        return None
    if any(fragment in normalized for fragment in ("immediately", "asap", "right away")):
        return 0
    week_match = re.search(
        r"\b(?P<count>\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+weeks?\b",
        normalized,
    )
    if week_match:
        count = _timing_token_to_int(str(week_match.group("count") or ""))
        if count is not None:
            return count * 7
    day_match = re.search(
        r"\b(?P<count>\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+(?:business\s+)?days?\b",
        normalized,
    )
    if day_match:
        return _timing_token_to_int(str(day_match.group("count") or ""))
    return None


def date_in_two_weeks(now: datetime | None = None) -> date:
    return _local_application_now(now).date() + timedelta(days=14)


def profile_available_start_date(
    application_profile: ApplicationProfile,
    *,
    now: datetime | None = None,
) -> date:
    offset_days = _timing_offset_days_from_text(getattr(application_profile, "earliest_start_timing", None))
    if offset_days is None:
        offset_days = _timing_offset_days_from_text(getattr(application_profile, "notice_period", None))
    if offset_days is None:
        return date_in_two_weeks(now)
    return _local_application_now(now).date() + timedelta(days=offset_days)


def monday_in_two_weeks(now: datetime | None = None) -> date:
    return date_in_two_weeks(now)


def format_long_date(value: date) -> str:
    return f"{value.strftime('%B')} {value.day}, {value.year}"


def build_onsite_start_location_answer(
    text: str | None,
    application_profile: ApplicationProfile,
    *,
    now: datetime | None = None,
) -> str | None:
    normalized = normalize_text(text)
    if not normalized:
        return None
    asks_to_proceed = "want to proceed" in normalized or "taken this into consideration" in normalized
    asks_for_start = "soonest you could start" in normalized or "soonest you can start" in normalized
    asks_for_location = "which location" in normalized or "at which location" in normalized
    asks_onsite_context = any(token in normalized for token in ("onsite", "on site", "in person", "hybrid", "wfh"))
    if not (asks_onsite_context and asks_to_proceed and asks_for_start and asks_for_location):
        return None

    start_date = format_long_date(profile_available_start_date(application_profile, now=now))
    location = _profile_city(application_profile) or "San Francisco"
    return f"Yes. The soonest I could start is {start_date}, and I would plan to work from {location}."


def question_is_office_attendance_prompt(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if "hybrid" in normalized:
        return True
    if "office hub" in normalized or "office hubs" in normalized:
        return True
    if "office" not in normalized and "onsite" not in normalized and "on site" not in normalized:
        return False
    if re.search(r"\d+x\s*/?\s*week", normalized) or re.search(r"\d+x\s+per\s+week", normalized):
        return True
    return any(
        fragment in normalized
        for fragment in (
            "come into the office",
            "come into our",
            "come into",
            "commute into the office",
            "days per week",
            "days a week",
            "days week",
            "hybrid",
            "in office",
            "in office availability",
            "office attendance",
        )
    )


def question_is_role_commitment_prompt(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    commitment_terms = (
        "comfortable committing",
        "comfortable with these details",
        "comfortable committing to these details",
        "comfortable proceeding with these details",
        "still comfortable with these details",
    )
    role_detail_terms = (
        "hours per week",
        "expected schedule",
        "temp role",
        "temporary role",
        "contract role",
        "contract/temp",
        "fixed term",
        "fixed-term",
        "month contract",
        "months",
    )
    return any(term in normalized for term in commitment_terms) and any(
        term in normalized for term in role_detail_terms
    )


def question_is_relocation_willingness(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized or "relocat" not in normalized:
        return False
    if any(
        fragment in normalized
        for fragment in (
            "when are you willing to relocate",
            "where would you be open to relocating",
            "where are you open to relocating",
            "if you would need to relocate",
            "please type relocating",
            "relocation support",
        )
    ):
        return False
    return bool(
        re.search(r"\b(?:are|would|will)\s+you\b.*\b(?:willing|able|open|planning)\b.*\brelocat", normalized)
        or re.search(r"\b(?:are|would|will)\s+you\b.*\brelocat", normalized)
        or re.search(r"\bif (?:not|you are not)\b.*\brelocat", normalized)
        or "willing to relocate" in normalized
        or "open to relocate" in normalized
        or "open to relocation" in normalized
    )


def question_is_relocation_assistance_requirement(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized or "relocation assistance" not in normalized:
        return False
    return bool(
        re.search(r"\b(?:do|would|will)\s+you\b.*\b(?:require|need)\b.*\brelocation assistance\b", normalized)
        or "require relocation assistance" in normalized
        or "need relocation assistance" in normalized
    )


def question_is_travel_willingness(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized or "travel" not in normalized:
        return False
    if any(fragment in normalized for fragment in ("travel experience", "travel history", "travel policy")):
        return False
    return bool(
        re.search(r"\b(?:are|would|will|do)\s+you\b.*\b(?:able|willing|open)\b.*\btravel\b", normalized)
        or "travel up to" in normalized
        or "willing to travel" in normalized
        or "able to travel" in normalized
        or "open to travel" in normalized
    )


def question_is_travel_percentage_prompt(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized or "travel" not in normalized:
        return False
    if question_is_travel_willingness(text):
        return False
    if any(fragment in normalized for fragment in ("travel experience", "travel history", "travel policy")):
        return False
    if any(
        fragment in normalized
        for fragment in (
            "what percentage",
            "what percent",
            "travel percentage",
            "maximum travel",
            "max travel",
            "please mention how much",
            "please specify how much",
            "how much can travel",
            "how much travel can",
        )
    ):
        return True
    return bool(
        re.search(r"\b(?:what|which)\s+(?:percentage|percent)\b.*\btravel\b", normalized)
        or re.search(r"\bhow much\b.*\btravel\b", normalized)
    )


def question_is_location_residency_check(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized or "relocat" in normalized:
        return False
    if normalized.startswith(("what ", "which ", "where ")):
        return False
    if "united states" in normalized and "location specified" not in normalized and "job" not in normalized:
        return False
    return bool(
        re.search(r"\bdo you\b.*\b(?:reside|live|based|located)\b", normalized)
        and any(
            fragment in normalized
            for fragment in (
                "location specified",
                "job location",
                "for this role",
                "for this position",
                "bay area",
                "metro area",
                "commuting distance",
                "reasonable commute",
            )
        )
    ) or any(
        fragment in normalized
        for fragment in (
            "currently reside in the location specified",
            "currently based within commuting distance",
            "currently located within",
            "based within commuting distance",
            "currently live in the bay area",
            "currently based in the bay area",
        )
    )


_LOCATION_RESIDENCY_GENERIC_FRAGMENTS = (
    "location specified",
    "job location",
    "for this role",
    "for this position",
    "commuting distance",
    "reasonable commute",
)
_LOCATION_RESIDENCY_METRO_MEMBERS: dict[str, tuple[str, ...]] = {
    "bay area": (
        "bay area",
        "san francisco",
        "oakland",
        "san jose",
        "berkeley",
        "palo alto",
        "mountain view",
        "redwood city",
    ),
    "washington d c metro area": (
        "washington d c",
        "washington dc",
        "district of columbia",
        "dc",
        "mclean",
        "arlington",
        "alexandria",
        "bethesda",
        "silver spring",
        "rockville",
        "reston",
        "fairfax",
        "falls church",
        "tysons",
    ),
}


def _candidate_location_residency_aliases(application_profile: ApplicationProfile) -> set[str]:
    location = str(getattr(application_profile, "location", "") or "").strip()
    country = str(getattr(application_profile, "country", "") or "").strip()
    normalized_location = normalize_text(location)
    if not normalized_location:
        return set()

    aliases = {normalized_location}
    parts = [part.strip() for part in location.split(",") if part.strip()]
    city = normalize_text(parts[0]) if parts else ""
    state_abbrev = _location_state_abbreviation(location)
    state_name = normalize_text(_US_STATE_ABBREVIATIONS.get(state_abbrev, "")) if state_abbrev else ""
    normalized_country = normalize_text(country)

    for candidate in (
        city,
        state_abbrev,
        state_name,
        normalized_country,
        f"{city} {state_abbrev}".strip(),
        f"{city} {state_name}".strip(),
        f"{city} {normalized_country}".strip(),
        f"{state_name} {normalized_country}".strip(),
    ):
        normalized_candidate = normalize_text(candidate)
        if normalized_candidate:
            aliases.add(normalized_candidate)

    for metro_name, members in _LOCATION_RESIDENCY_METRO_MEMBERS.items():
        if any(member in normalized_location for member in members):
            aliases.add(metro_name)

    return aliases


def _extract_explicit_location_residency_target(text: str | None) -> str | None:
    normalized = normalize_text(text)
    if not normalized or any(fragment in normalized for fragment in _LOCATION_RESIDENCY_GENERIC_FRAGMENTS):
        return None
    if "bay area" in normalized:
        return "bay area"
    if "washington d c metro area" in normalized or "washington dc metro area" in normalized:
        return "washington d c metro area"

    patterns = (
        re.compile(r"\b(?:reside|live|based|located)\b(?:\s+\w+){0,3}?\s+\bin\s+(?:the\s+)?(?P<target>.+?)(?:\?|$)"),
        re.compile(
            r"\b(?:reside|live|based|located)\b(?:\s+\w+){0,3}?\s+\bwithin\s+(?:the\s+)?(?P<target>.+?)(?:\?|$)"
        ),
    )
    for pattern in patterns:
        match = pattern.search(normalized)
        if not match:
            continue
        target = str(match.group("target") or "").strip(" .,:;!?")
        if not target:
            continue
        target = re.sub(r"\b(?:metro area|area|region)\b", "", target).strip(" .,:;!?")
        if not target or any(fragment in target for fragment in _LOCATION_RESIDENCY_GENERIC_FRAGMENTS):
            return None
        return target
    return None


def _location_residency_boolean(text: str | None, application_profile: ApplicationProfile) -> bool | None:
    if not question_is_location_residency_check(text):
        return None
    candidate_aliases = _candidate_location_residency_aliases(application_profile)
    if not candidate_aliases:
        return None

    normalized = normalize_text(text)
    if "bay area" in normalized:
        return "bay area" in candidate_aliases

    if "washington d c metro area" in normalized or "washington dc metro area" in normalized:
        metro_members = _LOCATION_RESIDENCY_METRO_MEMBERS["washington d c metro area"]
        candidate_location = normalize_text(getattr(application_profile, "location", "") or "")
        return any(member in candidate_location for member in metro_members)

    target = _extract_explicit_location_residency_target(text)
    if target is None:
        return None

    normalized_target = normalize_text(target)
    if not normalized_target:
        return None
    if normalized_target in candidate_aliases:
        return True
    return any(normalized_target in alias for alias in candidate_aliases if alias)


_ROLE_DISCIPLINE_INTEREST_PATTERNS = (
    re.compile(r"what interests you about working in (?P<subject>.+?) for this role", re.I),
    re.compile(r"what interests you about (?P<subject>.+?) for this role", re.I),
    re.compile(r"why are you interested in (?P<subject>.+?) for this role", re.I),
)


def _role_discipline_interest_subject(text: str | None) -> str | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    for pattern in _ROLE_DISCIPLINE_INTEREST_PATTERNS:
        match = pattern.search(raw)
        if match:
            subject = str(match.group("subject") or "").strip(" .?:!\"'")
            if subject:
                return subject
    return None


def _source_bundle_mentions_llm_discovery(*, jd_parsed: dict | None = None, research_cache: dict | None = None) -> bool:
    bundle_text = normalize_text(
        json.dumps(
            {
                "jd_parsed": jd_parsed or {},
                "research_cache": research_cache or {},
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if not bundle_text:
        return False
    if "answer engine" in bundle_text or "ai answer" in bundle_text:
        return True
    return "llm" in bundle_text and "discovery" in bundle_text


def _verifier_retry_fallback_answer_for_question(
    text: str | None,
    *,
    jd_parsed: dict | None = None,
    research_cache: dict | None = None,
    master_resume_text: str | None = None,
) -> str | None:
    special_answer = _source_backed_special_answer_for_question(
        text,
        master_resume_text=master_resume_text,
    )
    if special_answer is not None:
        return special_answer
    domain_years_answer = _resume_domain_years_answer(text, master_resume_text=master_resume_text)
    if domain_years_answer is not None:
        return domain_years_answer
    subject = _role_discipline_interest_subject(text)
    if not subject:
        return None
    normalized_subject = normalize_text(subject)
    if "seo" in normalized_subject or "search engine optimization" in normalized_subject:
        extra_sentence = ""
        if _source_bundle_mentions_llm_discovery(jd_parsed=jd_parsed, research_cache=research_cache):
            extra_sentence = (
                " The role's focus on LLM-driven discovery is especially compelling because more people now "
                "start product discovery in search and answer engines."
            )
        return (
            "What interests me most is the chance to work on SEO as a product and discoverability problem. "
            "My background is strongest in experimentation, acquisition, conversion, and helping users discover "
            "the right experience, so Robinhood's focus on organic growth and relevant consumer entry points "
            "feels like a strong fit."
            f"{extra_sentence} I am excited by the chance to bring that product lens, analytics rigor, and "
            "discoverability mindset to a role centered on trusted entry points."
        )
    subject_display = subject.upper() if len(subject) <= 5 and subject.isalpha() else subject
    return (
        f"What interests me most is the chance to work on {subject_display} as a real customer and business "
        "problem with clear outcomes. I like roles that combine experimentation, strong product judgment, "
        "and close cross-functional partnership, so this focus feels like a strong fit for how I like to build."
    )


def build_verifier_retry_fallback_answers(
    *,
    question_specs: list[dict],
    answers: dict[str, object],
    retry_feedback_by_field: dict[str, list[str]],
    jd_parsed: dict | None = None,
    research_cache: dict | None = None,
    master_resume_text: str | None = None,
) -> dict[str, str]:
    overrides: dict[str, str] = {}
    if not retry_feedback_by_field:
        return overrides
    for spec in question_specs:
        field_name = str(spec.get("field_name") or "").strip()
        if not field_name or field_name not in retry_feedback_by_field:
            continue
        question_text = "\n".join(
            part.strip()
            for part in (str(spec.get("label") or ""), str(spec.get("description") or ""))
            if part and str(part).strip()
        )
        fallback_answer = _verifier_retry_fallback_answer_for_question(
            question_text,
            jd_parsed=jd_parsed,
            research_cache=research_cache,
            master_resume_text=master_resume_text,
        )
        if fallback_answer and question_is_skill_years_experience(question_text):
            option_labels = [str(option).strip() for option in (spec.get("options") or []) if str(option or "").strip()]
            matched_option = _select_years_experience_option_label(option_labels, fallback_answer)
            if matched_option:
                fallback_answer = matched_option
        if fallback_answer and fallback_answer != str(answers.get(field_name) or "").strip():
            overrides[field_name] = fallback_answer
    return overrides


_OPTIONAL_RETRY_BLANK_FIELD_TYPES = frozenset({"textarea", "input_text", "text", "string", "longtext"})


def build_optional_retry_blank_fallback_answers(
    *,
    question_specs: list[dict],
    answers: dict[str, object],
    retry_feedback_by_field: dict[str, list[str]],
) -> dict[str, None]:
    overrides: dict[str, None] = {}
    if not retry_feedback_by_field:
        return overrides
    for spec in question_specs:
        field_name = str(spec.get("field_name") or "").strip()
        if not field_name or field_name not in retry_feedback_by_field:
            continue
        if bool(spec.get("required")):
            continue
        field_type = normalize_text(str(spec.get("type") or ""))
        if field_type not in _OPTIONAL_RETRY_BLANK_FIELD_TYPES:
            continue
        current_answer = answers.get(field_name)
        if not isinstance(current_answer, str) or not current_answer.strip():
            continue
        overrides[field_name] = None
    return overrides


def question_is_interview_accommodation_request(text: str | None) -> bool:
    """Return True for interview-process accommodation prompts that should default to No."""
    normalized = normalize_text(text)
    if not normalized:
        return False
    if not any(
        fragment in normalized
        for fragment in (
            "reasonable accommodation",
            "reasonable accommodations",
            " accommodation ",
            " accommodations ",
            " assistance ",
            " support ",
        )
    ):
        return False
    return any(
        fragment in normalized
        for fragment in (
            "complete the hiring process",
            "complete our hiring process",
            "during the hiring process",
            "application process",
            "participate fully in our application process",
            "interview process",
            "interviewing process",
            "recruiting process",
            "recruitment process",
            "technical testing",
            "virtual and in person",
            "in person style interviews",
            "interviews",
        )
    )


def question_is_legal_age_check(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if any(fragment in normalized for fragment in ("legal age to work", "of legal age to work")):
        return True
    if not any(term in normalized for term in ("work", "employment", "position", "role")):
        return False
    return bool(
        re.search(r"\b(?:18|eighteen)\s+years?\s+(?:of age\s+)?or older\b", normalized)
        or re.search(r"\bat least\s+(?:18|eighteen)\s+years?\s+old\b", normalized)
        or re.search(r"\bat least\s+(?:18|eighteen)\s+years?\s+of\s+age\b", normalized)
    )


def question_is_background_check_consent(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if not any(
        fragment in normalized
        for fragment in ("background check", "background screening", "background screen", "background investigation")
    ):
        return False
    if any(
        fragment in normalized
        for fragment in (
            "are you willing",
            "would you be willing",
            "will you be willing",
            "willing to submit",
            "willing to undergo",
            "do you consent",
            "do you agree",
            "agree to",
            "consent to this verification",
            "acknowledge and consent",
            "by selecting yes",
            "agree to a background check",
            "authorize a background check",
            "background and reference check disclosure",
        )
    ):
        return True
    return bool(re.search(r"\b(?:are|would|will)\s+you\b.*\bwilling\b.*\b(?:undergo|submit)\b", normalized))


def question_is_interview_ai_policy_consent(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if "ai policy for interviewers" in normalized:
        return True
    if "interview" not in normalized:
        return False
    if not any(
        fragment in normalized
        for fragment in (
            "non ai assisted skills",
            "do not use any ai tools",
            "use any ai tools during any part of the interview process",
        )
    ):
        return False
    return any(
        fragment in normalized
        for fragment in (
            "please indicate yes if you have read and agree",
            "have read and agree",
            "read and agree",
            "please indicate yes",
        )
    )


def question_is_interview_recording_consent(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized or "interview" not in normalized:
        return False
    if not any(
        fragment in normalized
        for fragment in (
            "recorded in various formats",
            "record and transcribe interviews",
            "record and transcribe interview",
            "recorded",
            "recording",
            "transcript",
            "transcribe",
            "notetaker",
        )
    ):
        return False
    return any(
        fragment in normalized
        for fragment in (
            "consent",
            "agree",
            "acknowledge",
            "prefer not to be recorded",
            "may be recorded",
            "reviewed by",
        )
    )


def question_is_notice_period_prompt(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    return any(
        fragment in normalized
        for fragment in (
            "notice period",
            "current notice period",
            "notice period to your current employer",
        )
    )


def question_is_availability_timing_prompt(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if (
        any(fragment in normalized for fragment in ("which location", "at which location"))
        and "want to proceed" in normalized
        and any(token in normalized for token in ("onsite", "on site", "in person", "hybrid", "wfh"))
    ):
        return False
    if any(
        fragment in normalized
        for fragment in (
            "start and end dates",
            "start date month",
            "start date year",
            "employment or engagement",
        )
    ):
        return False
    if question_is_notice_period_prompt(text):
        return True
    if "date available" in normalized or "available date" in normalized:
        return True
    if "desired start date" in normalized:
        return True
    if any(fragment in normalized for fragment in _AVAILABILITY_DATE_FORMAT_HINTS) and any(
        token in normalized for token in ("start", "available")
    ):
        return True
    if re.search(r"\bwhen\b.*\b(?:can|could|would)\b.*\bstart\b", normalized):
        return True
    if re.search(r"\b(?:soonest|earliest)\b.*\bstart\b", normalized):
        return True
    return bool(re.search(r"\bavailable\b.*\bstart\b", normalized))


def question_is_reasonable_accommodation_check(text: str | None) -> bool:
    """Return True if the question asks about ability to perform duties with or without reasonable accommodation."""
    normalized = normalize_text(text)
    if not normalized:
        return False
    if question_is_interview_accommodation_request(text):
        return False
    if any(
        fragment in normalized
        for fragment in (
            "with or without reasonable accommodation",
            "with or without reasonable accommodations",
            "ability to perform the requisite duties",
            "perform the essential functions",
            "perform essential job functions",
        )
    ):
        return True
    return any(
        fragment in normalized for fragment in ("reasonable accommodation", "reasonable accommodations")
    ) and bool(re.search(r"\b(?:able|ability|can|perform)\b", normalized))


def question_is_experience_confirmation(text: str | None) -> bool:
    """Return True if the question is a yes/no confirmation about having specific experience.

    Matches questions like:
    - "Have you shipped an AI-powered feature or product?"
    - "Do you have experience working closely with engineers on technical products?"
    - "Do you have a background in software engineering?"

    Excludes open-ended follow-ups like "share more about the AI-powered feature"
    and education-focused questions containing "degree"/"university"/"college"/"academic".
    """
    normalized = normalize_text(text)
    if not normalized:
        return False
    if question_requests_ai_agent_and_rag_experience(text):
        return False
    if re.search(
        r"^(?:what|which|where|how)\b.*\bhave you\b.*\b(?:shipped|built|launched|developed|managed|led|created|designed|implemented|worked on)\b",
        normalized,
    ):
        return False
    # Exclude open-ended prompts that ask for elaboration
    if any(
        phrase in normalized
        for phrase in (
            "share more",
            "describe",
            "tell us more",
            "elaborate",
            "explain",
            "please provide details",
            "what problem were you solving",
            "what impact did it have",
        )
    ):
        return False
    # Pattern 1: "Have you [verb]..." (existing)
    if re.search(
        r"\bhave you\b.*\b(?:shipped|built|launched|developed|managed|led|created|designed|implemented)\b",
        normalized,
    ):
        return True
    if re.search(r"\bhave you worked on\b.*\b(product|feature|platform|workflow|system|experience)\b", normalized):
        return True
    # Pattern 2: "Do you have experience/background..." (new)
    # Anchored: require "do you have" at the start to avoid matching "What do you have experience with"
    # Education guard: reject if label contains degree/university/college/academic keywords
    if re.search(
        r"^do you have\s+(?:a\s+)?(?:extensive\s+|direct\s+|prior\s+|hands on\s+)?(?:experience|background)\b",
        normalized,
    ):
        if not any(kw in normalized for kw in ("degree", "university", "college", "academic")):
            return True
    if re.search(
        r"^do you have\s+(?:a\s+)?(?:extensive\s+|direct\s+|prior\s+|hands on\s+)?(?:[\w/-]+\s+){1,4}(?:experience|background)\b",
        normalized,
    ):
        if not any(kw in normalized for kw in ("degree", "university", "college", "academic")):
            return True
    return False


_SKILL_CONFIRMATION_GENERIC_SUBJECT_TERMS = frozenset(
    {
        "background",
        "business",
        "customer",
        "customers",
        "data science",
        "design",
        "engineering",
        "experience",
        "leadership",
        "management",
        "operations",
        "platform",
        "platforms",
        "process",
        "industry",
        "product",
        "products",
        "program",
        "program management",
        "project",
        "project management",
        "research",
        "relevant",
        "sales",
        "significant",
        "solid",
        "strategy",
        "strong",
        "support",
        "systems",
        "workflow",
        "workflows",
        "extensive",
        "direct",
        "prior",
    }
)
_SKILL_CONFIRMATION_ALIAS_MAP: dict[str, tuple[str, ...]] = {
    "sql": ("sql", "structured query language"),
    "structured query language": ("sql", "structured query language"),
}
_SKILL_YEARS_EXPERIENCE_PATTERNS = (
    re.compile(r"^how many years of\s+(?:work\s+)?experience do you have with (?P<subject>.+?)(?:\?|$)", re.I),
    re.compile(r"^how many years of\s+(?:work\s+)?experience do you have in (?P<subject>.+?)(?:\?|$)", re.I),
    re.compile(
        r"^how many years of\s+(?:proven\s+)?experience do you (?:currently\s+)?have(?:\s+of|\s+with|\s+in|\s+managing)\s+(?P<subject>.+?)(?:\?|$)",
        re.I,
    ),
    re.compile(r"^how many years of\s+(?P<subject>.+?)\s+experience do you (?:currently\s+)?have(?:\?|$)", re.I),
    re.compile(r"^years?\s+of\s+(?:work\s+)?experience with (?P<subject>.+?)(?:\?|$)", re.I),
)
_SKILL_YEARS_ALIAS_MAP: dict[str, tuple[str, ...]] = {
    "eng": ("eng", "engineering"),
    "engineering": ("engineering", "eng"),
    "python": ("python", "python programming language"),
    "python programming language": ("python", "python programming language"),
}
_SKILL_YEARS_GENERIC_SUBJECT_TOKENS = frozenset(
    {
        "management",
        "product",
        "professional",
        "program",
        "project",
        "relevant",
    }
)
_RESUME_MONTH_NAMES = (
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
)
_RESUME_MONTH_ALIASES: dict[str, str] = {}
for _month in _RESUME_MONTH_NAMES:
    _RESUME_MONTH_ALIASES[_month.casefold()] = _month
    _RESUME_MONTH_ALIASES[_month[:3].casefold()] = _month
_MASTER_RESUME_ROLE_HEADER_RE = re.compile(r"^[A-Z0-9&'().,/ -]+ — .+$")
_MASTER_RESUME_DATE_RANGE_RE = re.compile(
    r"(?P<start_month>[A-Za-z]{3,9})\s+(?P<start_year>\d{4})\s*[–-]\s*"
    r"(?:(?P<end_month>[A-Za-z]{3,9})\s+(?P<end_year>\d{4})|(?P<present>Present))"
)
_PRODUCT_MANAGEMENT_TITLE_MARKERS = ("product manager", "product management")
_RESUME_DOMAIN_YEARS_SIGNAL_GROUPS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("activation", "onboarding", "growth"),
        (
            "activation",
            "onboarding",
            "growth",
            "conversion",
            "funnel",
            "experiment",
            "a b testing",
            "pipeline",
            "client growth",
        ),
    ),
    (
        ("customer support", "tooling"),
        (
            "support ticket",
            "support tickets",
            "support escalation",
            "support escalations",
            "chat workflow",
            "chat workflows",
            "chatbot",
            "support quality",
            "support volume",
            "business support systems",
            "self serve diagnostic",
        ),
    ),
    (
        ("ai support tools",),
        ("slipstream", "irp navigator", "ai chatbot", "llm", "rag"),
    ),
    (
        ("llm based platforms",),
        ("slipstream", "irp navigator", "ai chatbot", "llm", "rag"),
    ),
)


def _load_master_resume_text(master_resume_text: str | None = None) -> str:
    if master_resume_text is not None:
        return master_resume_text
    try:
        return MASTER_RESUME_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def _normalized_text_has_phrase(normalized_text: str, phrase: str | None) -> bool:
    normalized_phrase = normalize_text(phrase)
    if not normalized_text or not normalized_phrase:
        return False
    pattern = r"\b" + r"\s+".join(re.escape(token) for token in normalized_phrase.split()) + r"\b"
    return re.search(pattern, normalized_text) is not None


def _resume_month_year(month_text: str, year_text: str) -> tuple[int, int] | None:
    full_month = _RESUME_MONTH_ALIASES.get(str(month_text).strip().casefold())
    if not full_month:
        return None
    try:
        parsed = datetime.strptime(f"{full_month} {year_text}", "%B %Y")
    except ValueError:
        return None
    return parsed.year, parsed.month


def _inclusive_months_between(start: tuple[int, int], end: tuple[int, int]) -> int:
    start_year, start_month = start
    end_year, end_month = end
    return max(0, (end_year - start_year) * 12 + (end_month - start_month) + 1)


def _resume_experience_blocks(
    master_resume_text: str | None = None,
    *,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    resume_text = _load_master_resume_text(master_resume_text)
    if not resume_text:
        return []

    today = now.astimezone() if now is not None and now.tzinfo else datetime.now().astimezone()
    lines = resume_text.splitlines()
    blocks: list[dict[str, object]] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or not _MASTER_RESUME_ROLE_HEADER_RE.match(line):
            index += 1
            continue
        normalized_header = normalize_text(line)

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
            start = _resume_month_year(date_match.group("start_month"), date_match.group("start_year"))
            if date_match.group("present"):
                end = (today.year, today.month)
            else:
                end = _resume_month_year(date_match.group("end_month"), date_match.group("end_year"))
            if start is not None and end is not None:
                months = _inclusive_months_between(start, end)
                if months > 0:
                    blocks.append(
                        {
                            "is_product_role": any(marker in normalized_header for marker in _PRODUCT_MANAGEMENT_TITLE_MARKERS),
                            "months": months,
                            "text": normalize_text(" ".join(block_lines)),
                        }
                    )

        index = block_index

    return blocks


def _rounded_resume_years_text(total_months: int) -> str | None:
    if total_months <= 0:
        return None
    if total_months < 12:
        return "0"
    return str(max(1, int((total_months / 12) + 0.5)))


def _resume_domain_years_answer(
    text: str | None,
    *,
    master_resume_text: str | None = None,
    now: datetime | None = None,
) -> str | None:
    normalized_question = normalize_text(text)
    if not normalized_question:
        return None
    blocks = _resume_experience_blocks(master_resume_text, now=now)
    if not blocks:
        return None

    for question_fragments, resume_fragments in _RESUME_DOMAIN_YEARS_SIGNAL_GROUPS:
        if not all(_normalized_text_has_phrase(normalized_question, fragment) for fragment in question_fragments):
            continue
        total_months = sum(
            int(block["months"])
            for block in blocks
            if bool(block.get("is_product_role"))
            and any(_normalized_text_has_phrase(str(block["text"]), fragment) for fragment in resume_fragments)
        )
        answer = _rounded_resume_years_text(total_months)
        if answer is not None:
            return answer
    return None


def _select_years_experience_option_label(options: list[str] | None, years_text: str | None) -> str | None:
    normalized_answer = normalize_text(years_text)
    if not normalized_answer:
        return None
    normalized_options = [(option, normalize_text(option)) for option in options or [] if normalize_text(option)]
    if not normalized_options:
        return None

    for option, normalized_option in normalized_options:
        if normalized_option == normalized_answer:
            return option

    years_match = re.search(r"\b(\d{1,2})\b", str(years_text))
    if not years_match:
        return None
    years_value = int(years_match.group(1))

    for option, normalized_option in normalized_options:
        if normalized_option == str(years_value) or normalized_option == f"{years_value} years":
            return option

    range_pattern = re.compile(r"\b(?P<low>\d{1,2})\s*(?:-|–|to|\s+)\s*(?P<high>\d{1,2})(?:\s+years?)?\b")
    plus_pattern = re.compile(r"\b(?P<low>\d{1,2})\s*\+(?:\s+years?)?\b")
    exact_pattern = re.compile(r"\b(?P<exact>\d{1,2})(?:\s+years?)?\b")

    for option, normalized_option in normalized_options:
        if match := range_pattern.search(normalized_option):
            low = int(match.group("low"))
            high = int(match.group("high"))
            if low <= years_value <= high:
                return option
    for option, normalized_option in normalized_options:
        if match := plus_pattern.search(normalized_option):
            low = int(match.group("low"))
            if years_value >= low:
                return option
    for option, normalized_option in normalized_options:
        if match := exact_pattern.search(normalized_option):
            if years_value == int(match.group("exact")):
                return option
    return None


def _skill_confirmation_subject(text: str | None) -> str | None:
    normalized = normalize_text(text)
    if not normalized:
        return None
    if any(phrase in normalized for phrase in ("please provide details", "share more", "tell us more", "describe")):
        return None
    patterns = (
        re.compile(
            r"^are you\s+(?:currently\s+)?(?:proficient|comfortable|experienced|fluent|skilled)\s+in\s+(?P<subject>.+?)(?:\?|$)"
        ),
        re.compile(
            r"^do you have\s+(?:moderate\s+|strong\s+|solid\s+|significant\s+|extensive\s+|hands on\s+|hands-on\s+|direct\s+|prior\s+)?(?P<subject>(?:[\w+#./-]+\s+){0,3}[\w+#./-]+)\s+(?:experience|expertise|proficiency)\b"
        ),
    )
    for pattern in patterns:
        match = pattern.search(normalized)
        if not match:
            continue
        subject = str(match.group("subject") or "").strip(" .?:!,'\"")
        subject = re.sub(r"^(?:a|an)\s+", "", subject, flags=re.I)
        if not subject:
            continue
        if len(subject.split()) > 4:
            continue
        if subject in _SKILL_CONFIRMATION_GENERIC_SUBJECT_TERMS:
            continue
        if any(
            re.search(r"\b" + r"\s+".join(re.escape(token) for token in term.split()) + r"\b", subject)
            for term in _SKILL_CONFIRMATION_GENERIC_SUBJECT_TERMS
        ):
            continue
        return subject
    return None


def question_is_skill_confirmation(text: str | None) -> bool:
    return _skill_confirmation_subject(text) is not None


def _explicit_resume_skill_inventory_text(master_resume_text: str | None = None) -> str:
    try:
        resume_text = master_resume_text or MASTER_RESUME_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""
    skill_lines = [
        line.strip() for line in resume_text.splitlines() if line.strip().startswith(("Technical:", "ML/AI:", "Data:"))
    ]
    return normalize_text(" ".join(skill_lines))


def _resume_explicit_skill_support(
    text: str | None,
    *,
    master_resume_text: str | None = None,
) -> bool | None:
    subject = _skill_confirmation_subject(text)
    if subject is None:
        return None
    inventory_text = _explicit_resume_skill_inventory_text(master_resume_text)
    if not inventory_text:
        return False
    candidates = _SKILL_CONFIRMATION_ALIAS_MAP.get(subject, (subject,))
    for candidate in candidates:
        pattern = r"\b" + r"\s+".join(re.escape(token) for token in candidate.split()) + r"\b"
        if re.search(pattern, inventory_text):
            return True
    return False


def _normalize_skill_years_subject(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _subject_is_generic_skill_years_prompt(subject: str | None) -> bool:
    normalized_subject = _normalize_skill_years_subject(subject)
    if not normalized_subject:
        return True
    subject_tokens = tuple(normalized_subject.split())
    return all(token in _SKILL_YEARS_GENERIC_SUBJECT_TOKENS for token in subject_tokens)


def _skill_years_experience_subject(text: str | None) -> str | None:
    raw_text = str(text or "").strip()
    if not raw_text:
        return None
    abbreviation_match = re.search(
        r"^How many years of (?P<subject>[A-Z][A-Z0-9+/& .-]{1,15}) experience do you (?:currently\s+)?have(?:\?|$)",
        raw_text,
    )
    if abbreviation_match:
        normalized_subject = _normalize_skill_years_subject(abbreviation_match.group("subject"))
        if normalized_subject and len(normalized_subject.split()) <= 6:
            return normalized_subject
    for pattern in _SKILL_YEARS_EXPERIENCE_PATTERNS:
        match = pattern.search(raw_text)
        if not match:
            continue
        subject = re.sub(r"\([^)]*\)", "", str(match.group("subject") or ""))
        normalized_subject = _normalize_skill_years_subject(subject)
        if (
            normalized_subject
            and len(normalized_subject.split()) <= 12
            and not _subject_is_generic_skill_years_prompt(normalized_subject)
        ):
            return normalized_subject
    return None


def question_is_skill_years_experience(text: str | None) -> bool:
    return _skill_years_experience_subject(text) is not None


def _skill_years_subject_candidates(text: str | None) -> tuple[str, ...]:
    subject = _skill_years_experience_subject(text)
    if subject is None:
        return ()
    candidates = [subject]
    candidates.extend(_SKILL_YEARS_ALIAS_MAP.get(subject, ()))
    if subject.endswith(" programming language"):
        candidates.append(subject.removesuffix(" programming language").strip())
    unique_candidates: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized_candidate = _normalize_skill_years_subject(candidate)
        if not normalized_candidate or normalized_candidate in seen:
            continue
        seen.add(normalized_candidate)
        unique_candidates.append(normalized_candidate)
    return tuple(unique_candidates)


def _profile_skill_years_answer(
    text: str | None,
    application_profile: ApplicationProfile,
    *,
    master_resume_text: str | None = None,
) -> str | None:
    resume_answer = _resume_domain_years_answer(text, master_resume_text=master_resume_text)
    if resume_answer is not None:
        return resume_answer
    default_skill_years = getattr(application_profile, "default_skill_years", None)
    if default_skill_years:
        return default_skill_years
    skill_years = getattr(application_profile, "skill_years", None) or {}
    if not skill_years:
        return None
    for candidate in _skill_years_subject_candidates(text):
        value = skill_years.get(candidate)
        if value:
            return value
    return None


def question_is_application_status_sms_optin(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if not any(
        fragment in normalized
        for fragment in (
            "text message",
            "text messages",
            "sms message",
            "sms messages",
            "communication via text",
            "communication via sms",
        )
    ):
        return False
    if not any(
        fragment in normalized
        for fragment in (
            "application status",
            "job application",
            "recruitment process",
            "application process",
            "follow up communication",
            "follow-up communication",
            "next steps",
        )
    ):
        return False
    if any(
        fragment in normalized
        for fragment in (
            "future opportunities",
            "future job opportunities",
            "future openings",
            "future job openings",
            "careers content",
            "career newsletter",
            "talent community",
            "marketing communications about careers",
        )
    ):
        return False
    return any(
        fragment in normalized
        for fragment in (
            "consent",
            "agree",
            "opt in",
            "opt-in",
            "receive",
            "receiving",
        )
    )


def question_is_culture_careers_optin(text: str | None) -> bool:
    """Return True if the question is asking about opting in to culture/careers content, alerts, or newsletters."""
    normalized = normalize_text(text)
    if not normalized:
        return False
    if (
        any(fragment in normalized for fragment in ("sms from", "text messages from", "communications via sms"))
        and any(
            fragment in normalized
            for fragment in (
                "job application",
                "recruitment process",
                "next steps in the recruitment process",
            )
        )
        and any(
            fragment in normalized
            for fragment in (
                "acknowledge",
                "by providing my phone number",
                "message frequency varies",
                "reply stop",
                "reply help",
            )
        )
    ):
        return False
    return any(
        fragment in normalized
        for fragment in (
            "stay up to date",
            "culture and careers",
            "receive alerts for similar",
            "alerts for similar jobs",
            "career newsletter",
            "careers content",
            "talent community",
            "keep me informed",
            "notify me of",
            "future opportunities",
            "future job opportunities",
            "future openings",
            "future job openings",
            "email me about future job openings",
            "marketing communications about careers",
            "communications about careers",
            "receive communications via sms",
            "communications via text",
            "sms from",
            "text messages from",
        )
    )


def question_is_profile_included_confirmation(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if not any(fragment in normalized for fragment in ("linkedin", "github", "portfolio", "website")):
        return False
    if not any(
        fragment in normalized
        for fragment in (
            "did you include",
            "have you included",
            "included your",
            "as part of your application",
            "part of your application",
            "in your application",
            "with your application",
            "provided your",
        )
    ):
        return False
    return any(fragment in normalized for fragment in ("did you", "have you", "included", "provided"))


_EDUCATION_KEYWORDS = (
    "education",
    "degree",
    "college",
    "university",
    "post-secondary",
    "school attended",
    "institution",
    "academic",
    "diploma",
)
_EDUCATION_EXCLUDE_KEYWORDS = ("background check", "discrimination", "equal opportunity")
_EDUCATION_IMMIGRATION_EXCLUDE_KEYWORDS = (
    "optional practical training",
    "24-month opt",
    "24 month opt",
    "opt extension",
    "stem opt",
    "h-1b",
    "h1b",
    "i-140",
    "cap exempt",
)
_EDUCATION_EMPLOYMENT_EXCLUDE_KEYWORDS = (
    "employment experience",
    "employment history",
    "employed by",
    "government contractor",
)
_EDUCATION_DISCIPLINE_HINTS = (
    "major",
    "discipline",
    "field of study",
    "area of study",
    "subject of study",
    "concentration",
    "specialization",
    "specialisation",
)
_EDUCATION_LEVEL_HINTS = (
    "highest level of completed education",
    "highest level of education",
    "highest completed education",
    "highest degree",
    "highest completed degree",
    "most recent degree",
    "degree obtained",
    "degree completed",
    "education level",
)
_EDUCATION_DISCIPLINE_OPTION_CANDIDATES = (
    (("computer science",), "Computer Science"),
    (("business administration", "m b a", "mba"), "Business Administration"),
    (("finance",), "Finance"),
    (("artificial intelligence",), "Artificial Intelligence"),
    (("machine learning",), "Machine Learning"),
)
_EDUCATION_LEVEL_OPTION_CANDIDATES = (
    (("doctor", "doctorate", "doctoral", "ph d"), ("PHD", "PhD", "Doctorate", "Doctoral Degree")),
    (
        ("master", "m b a", "mba", "m s", "m a"),
        (
            "Masters Degree",
            "Master's Degree",
            "Master Degree",
            "Master of Business Administration (M.B.A.)",
        ),
    ),
    (("bachelor", "undergraduate", "b s", "b a"), ("Bachelors Degree", "Bachelor's Degree", "Bachelor Degree")),
    (("associate",), ("Associates", "Associate's Degree", "Associate Degree")),
    (("high school", "ged"), ("High school diploma", "High School Diploma", "High School", "GED")),
)


def question_is_education(label: str) -> bool:
    """Detect questions asking about education/degrees."""
    lower = label.lower()
    if any(kw in lower for kw in _EDUCATION_EXCLUDE_KEYWORDS):
        return False
    if any(kw in lower for kw in _EDUCATION_IMMIGRATION_EXCLUDE_KEYWORDS):
        return False
    if "institution" in lower and any(kw in lower for kw in _EDUCATION_EMPLOYMENT_EXCLUDE_KEYWORDS):
        return False
    return any(kw in lower for kw in _EDUCATION_KEYWORDS)


_CREDENTIAL_CLAIM_EXCLUDE_FRAGMENTS = (
    "deemed export license",
    "ear controlled technology",
    "export control",
    "export license",
)
_CREDENTIAL_TERMS = (
    "bachelor",
    "master",
    "mba",
    "phd",
    "doctorate",
    "degree",
    "diploma",
    "certification",
    "certifications",
    "certificate",
    "certified",
    "license",
    "licenses",
    "licence",
    "licensed",
)


def question_is_credential_claim(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if any(fragment in normalized for fragment in _CREDENTIAL_CLAIM_EXCLUDE_FRAGMENTS):
        return False
    if normalized.startswith(("what ", "which ", "where ", "provide ", "list ", "please list")):
        return False
    if "background" in normalized and "professional experience" in normalized:
        return False
    has_claim_verb = any(
        phrase in normalized
        for phrase in (
            "do you have",
            "do you hold",
            "do you currently hold",
            "intend to hold",
            "are you certified",
            "are you licensed",
        )
    )
    return has_claim_verb and any(term in normalized for term in _CREDENTIAL_TERMS)


def format_education_from_profile(profile: ApplicationProfile) -> str | None:
    """Format education entries from the application profile as newline-separated text."""
    if not profile.education_entries:
        return None
    return "\n".join(profile.education_entries)


def question_is_education_discipline_field(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    has_discipline_hint = any(fragment in normalized for fragment in _EDUCATION_DISCIPLINE_HINTS)
    if not has_discipline_hint:
        return False
    return question_is_education(text) or "major" in normalized


def question_is_education_level_field(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if not question_is_education(text):
        return False
    return any(fragment in normalized for fragment in _EDUCATION_LEVEL_HINTS)


def question_is_binary_education_completion(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized or not question_is_education(text):
        return False
    if question_is_education_level_field(text) or question_is_education_discipline_field(text):
        return False
    return any(
        fragment in normalized
        for fragment in (
            "have you completed",
            "completed the following level of education",
            "did you complete",
            "have you obtained",
            "did you obtain",
            "have you earned",
            "did you earn",
            "have you graduated",
            "did you graduate",
            "graduated from",
        )
    )


def _match_option_text(options: list[str], candidate: str) -> str | None:
    normalized_candidate = normalize_text(candidate)
    if not normalized_candidate:
        return None
    for option in options:
        normalized_option = normalize_text(option)
        if normalized_option == normalized_candidate:
            return option
    for option in options:
        normalized_option = normalize_text(option)
        if normalized_candidate in normalized_option or normalized_option in normalized_candidate:
            return option
    return None


def education_discipline_option_matches(
    text: str | None,
    options: list[str] | None,
    application_profile: ApplicationProfile | None,
) -> list[str]:
    if application_profile is None or not options or not question_is_education_discipline_field(text):
        return []

    matches: list[str] = []
    seen_options: set[str] = set()
    for entry in _normalized_education_entries(application_profile):
        for fragments, candidate in _EDUCATION_DISCIPLINE_OPTION_CANDIDATES:
            if not any(fragment in entry for fragment in fragments):
                continue
            matched = _match_option_text(options, candidate)
            if matched is None or matched in seen_options:
                continue
            matches.append(matched)
            seen_options.add(matched)
    return matches


def education_level_option_matches(
    text: str | None,
    options: list[str] | None,
    application_profile: ApplicationProfile | None,
) -> list[str]:
    if application_profile is None or not options or not question_is_education_level_field(text):
        return []

    education_entries = _normalized_education_entries(application_profile)
    if not education_entries:
        return []

    for fragments, candidates in _EDUCATION_LEVEL_OPTION_CANDIDATES:
        if not any(any(fragment in entry for fragment in fragments) for entry in education_entries):
            continue
        for candidate in candidates:
            matched = _match_option_text(options, candidate)
            if matched is not None:
                return [matched]
    return []


def _work_authorization_boolean(text: str, application_profile: ApplicationProfile) -> bool | None:
    normalized = normalize_text(text)
    explicit_countries = _explicit_work_authorization_country_codes(text)
    authorized_countries = _authorized_country_codes(application_profile)
    requires_sponsorship = application_profile.require_sponsorship_now or application_profile.require_sponsorship_future
    has_explicit_authorized_country = bool(explicit_countries & authorized_countries)
    if question_is_current_country_work_authorization(normalized):
        return application_profile.authorized_to_work_unconditionally
    if normalized.startswith(("do you require", "will you require", "would you require")) and any(
        fragment in normalized
        for fragment in (
            "sponsorship",
            "visa",
            "work authorization",
            "authorization to work",
            "work permit",
        )
    ):
        if explicit_countries and not has_explicit_authorized_country:
            return True
        return requires_sponsorship
    if question_requests_sponsorship_requirement(normalized):
        if explicit_countries and not has_explicit_authorized_country:
            return True
        return requires_sponsorship
    if any(fragment in normalized for fragment in _WORK_AUTHORIZATION_TEXT_FRAGMENTS):
        if explicit_countries:
            return has_explicit_authorized_country
        return application_profile.authorized_to_work_unconditionally
    return None


def _candidate_first_name_answer(master_resume_text: str | None = None) -> str | None:
    first_name = _candidate_first_name_text(master_resume_text)
    return first_name.upper() if first_name else None


def _candidate_full_name_text(master_resume_text: str | None = None) -> str | None:
    try:
        resume_text = master_resume_text or MASTER_RESUME_PATH.read_text(encoding="utf-8")
        profile = parse_master_resume(resume_text)
    except (FileNotFoundError, OSError, ValueError):
        return None
    full_name = (profile.full_name or "").strip()
    return full_name or None


def _candidate_email_text(
    application_profile: ApplicationProfile | None = None,
    master_resume_text: str | None = None,
) -> str | None:
    try:
        resume_text = master_resume_text or MASTER_RESUME_PATH.read_text(encoding="utf-8")
        profile = parse_master_resume(resume_text)
        email = (profile.email or "").strip()
        if email:
            return email
    except (FileNotFoundError, OSError, ValueError):
        pass
    if application_profile is not None:
        email = str(getattr(application_profile, "verification_code_email", "") or "").strip()
        if email:
            return email
    return None


def _candidate_first_name_text(master_resume_text: str | None = None) -> str | None:
    try:
        resume_text = master_resume_text or MASTER_RESUME_PATH.read_text(encoding="utf-8")
        profile = parse_master_resume(resume_text)
    except (FileNotFoundError, OSError, ValueError):
        return None
    first_name = (profile.first_name or profile.full_name.partition(" ")[0]).strip()
    return first_name or None


_SPECIALIST_DOMAIN_EXPERIENCE_SIGNAL_GROUPS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("clinical", "mental health", "behavioral health", "behavioural health"),
        ("clinical", "mental health", "behavioral health", "behavioural health", "patient", "care delivery"),
    ),
    (
        ("actuarial",),
        ("actuarial",),
    ),
)


def _specialist_domain_experience_boolean(
    text: str | None,
    *,
    master_resume_text: str | None = None,
) -> bool | None:
    normalized_question = normalize_text(text)
    if not normalized_question:
        return None
    resume_text = normalize_text(master_resume_text or "")
    for question_terms, resume_terms in _SPECIALIST_DOMAIN_EXPERIENCE_SIGNAL_GROUPS:
        if any(term in normalized_question for term in question_terms):
            return any(term in resume_text for term in resume_terms)
    return None


def _current_application_date_answer(normalized_question: str | None, *, now: datetime | None = None) -> str | None:
    normalized = normalize_text(normalized_question)
    if not normalized:
        return None
    if not ("today s date" in normalized or "todays date" in normalized or "date of application" in normalized):
        return None
    local_now = now.astimezone() if now is not None and now.tzinfo else datetime.now().astimezone()
    if "mm/dd/yyyy" in normalized or "mm dd yyyy" in normalized:
        return local_now.strftime("%m/%d/%Y")
    if "mm/dd/yy" in normalized or "mm dd yy" in normalized:
        return local_now.strftime("%m/%d/%y")
    if "yyyy-mm-dd" in normalized or "yyyy mm dd" in normalized:
        return local_now.strftime("%Y-%m-%d")
    return local_now.strftime("%m/%d/%Y")


def _availability_question_expects_date(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if any(fragment in normalized for fragment in _AVAILABILITY_DATE_FORMAT_HINTS):
        return True
    if "date available" in normalized or "available date" in normalized:
        return True
    return "date" in normalized and any(token in normalized for token in ("start", "available"))


def _availability_start_date_answer(
    text: str | None,
    application_profile: ApplicationProfile,
    *,
    now: datetime | None = None,
) -> str:
    normalized = normalize_text(text)
    start_date = profile_available_start_date(application_profile, now=now)
    if "yyyy-mm-dd" in normalized or "yyyy mm dd" in normalized:
        return start_date.strftime("%Y-%m-%d")
    if "dd/mm/yyyy" in normalized or "dd mm yyyy" in normalized:
        return start_date.strftime("%d/%m/%Y")
    if "dd/mm/yy" in normalized or "dd mm yy" in normalized:
        return start_date.strftime("%d/%m/%y")
    if "mm/dd/yy" in normalized or "mm dd yy" in normalized:
        return start_date.strftime("%m/%d/%y")
    return start_date.strftime("%m/%d/%Y")


def _profile_availability_timing_answer(
    text: str | None,
    application_profile: ApplicationProfile,
    *,
    now: datetime | None = None,
) -> str | None:
    if not question_is_availability_timing_prompt(text):
        return None
    if question_is_notice_period_prompt(text):
        return getattr(application_profile, "notice_period", None)
    if _availability_question_expects_date(text):
        return _availability_start_date_answer(text, application_profile, now=now)
    return getattr(application_profile, "earliest_start_timing", None) or getattr(
        application_profile, "notice_period", None
    )


def _is_textual_question_spec(spec: dict) -> bool:
    field_type = str(spec.get("type") or "").strip().casefold()
    return field_type in {"textarea", "string"} or field_type.endswith("text")


def _candidate_affiliation_inventory(candidate_context_text: str | None = None) -> list[str]:
    if candidate_context_text is None:
        try:
            candidate_context_text = CANDIDATE_CONTEXT_PATH.read_text(encoding="utf-8")
        except OSError:
            return []

    context_text = str(candidate_context_text or "").replace("\ufeff", "")
    if not context_text.strip():
        return []

    candidates: list[str] = []
    patterns = (
        r"\b(?:board member|member|vice president|president|advisor|volunteer|fellow)[^.\n]*?\bat\s+the\s+([A-Z][^.\n:]+)",
        r"\bmember of\s+([A-Z][^.\n:]+)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, context_text, flags=re.I):
            affiliation = str(match.group(1) or "").strip(" .,:;\"'")
            if affiliation:
                candidates.append(affiliation)
    unique_candidates: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized_candidate = normalize_text(candidate)
        if not normalized_candidate or normalized_candidate in seen:
            continue
        seen.add(normalized_candidate)
        unique_candidates.append(candidate)
    return unique_candidates


def _question_requests_affiliations(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if "talent community" in normalized:
        return False
    return any(
        fragment in normalized
        for fragment in ("affiliation", "affiliations", "afflil", "membership", "memberships")
    )


def _truthful_none_affiliation_option(option_labels: list[str]) -> str | None:
    normalized_options = [(option, normalize_text(option)) for option in option_labels if normalize_text(option)]
    for fragment in (
        "i am not affiliated with any of these groups",
        "i am not affiliated with any of the groups",
        "not affiliated with any of these groups",
        "none of the above",
        "none",
        "no affiliations",
        "not a member of any",
    ):
        for option, normalized_option in normalized_options:
            if normalized_option == fragment or fragment in normalized_option:
                return option
    return None


def _source_backed_affiliation_answer(
    spec: dict,
    *,
    candidate_context_text: str | None = None,
) -> object | None:
    question_text = "\n".join(
        part.strip()
        for part in (str(spec.get("label") or ""), str(spec.get("description") or ""))
        if part and str(part).strip()
    )
    if not _question_requests_affiliations(question_text):
        return None

    affiliations = _candidate_affiliation_inventory(candidate_context_text)
    if not affiliations:
        return None

    option_labels = [str(option).strip() for option in (spec.get("options") or []) if str(option or "").strip()]
    matched_options: list[str] = []
    for affiliation in affiliations:
        normalized_affiliation = normalize_text(affiliation)
        if not normalized_affiliation:
            continue
        for option in option_labels:
            normalized_option = normalize_text(option)
            if not normalized_option:
                continue
            if (
                normalized_affiliation == normalized_option
                or normalized_affiliation in normalized_option
                or normalized_option in normalized_affiliation
            ):
                if option not in matched_options:
                    matched_options.append(option)
                break

    field_type = str(spec.get("type") or "").strip().casefold()
    if matched_options:
        return matched_options if "multi_select" in field_type else matched_options[0]
    none_option = _truthful_none_affiliation_option(option_labels)
    if none_option is not None:
        return [none_option] if "multi_select" in field_type else none_option
    if not option_labels and _is_textual_question_spec(spec):
        return ", ".join(affiliations)
    return None


def _classified_text_answers(
    question_specs: list[dict],
    application_profile: ApplicationProfile | None,
    *,
    master_resume_text: str | None = None,
    company_name: str | None = None,
) -> dict[str, str]:
    shared_answers = _classified_shared_answers(
        question_specs,
        application_profile,
        master_resume_text=master_resume_text,
        company_name=company_name,
    )
    if not shared_answers:
        return {}

    answers: dict[str, str] = {}
    for spec in question_specs:
        field_name = str(spec.get("field_name") or "").strip()
        if not field_name or field_name not in shared_answers or not _is_textual_question_spec(spec):
            continue
        answers[field_name] = shared_answers[field_name]
    return answers


def _required_single_option_select_answer(spec: dict) -> str | None:
    if not bool(spec.get("required")):
        return None
    if _is_textual_question_spec(spec):
        return None
    field_type = str(spec.get("type") or "").strip().casefold()
    if "multi_select" in field_type:
        return None
    options = [
        str(option).strip() for option in (spec.get("options") or []) if option is not None and str(option).strip()
    ]
    if len(options) != 1:
        return None
    return options[0]


def _profile_included_confirmation_availability(
    text: str | None,
    application_profile: ApplicationProfile | None,
    master_resume_text: str | None = None,
) -> tuple[bool | None, str | None]:
    normalized = normalize_text(text)
    if not normalized or application_profile is None:
        return None, None

    resource_field: str | None = None
    resource_value = None
    if "linkedin" in normalized:
        resource_field = "linkedin"
        resource_value = getattr(application_profile, "linkedin", None)
    elif "github" in normalized:
        resource_field = "github"
        resource_value = getattr(application_profile, "github", None)
    elif any(fragment in normalized for fragment in ("website", "portfolio")):
        resource_field = "website"
        resource_value = getattr(application_profile, "website", None)

    if resource_field is None:
        return None, None
    if resource_value:
        return True, "application_profile.md"
    if master_resume_text:
        try:
            profile = parse_master_resume(master_resume_text)
        except (OSError, ValueError):
            profile = None
        if profile is not None and getattr(profile, resource_field, None):
            return True, "master_resume.md"
    return False, "application_profile.md"


def _classified_shared_answers(
    question_specs: list[dict],
    application_profile: ApplicationProfile | None,
    *,
    master_resume_text: str | None = None,
    work_stories_text: str | None = None,
    candidate_context_text: str | None = None,
    company_name: str | None = None,
) -> dict[str, object]:
    if application_profile is None:
        return {}

    answers: dict[str, object] = {}
    for spec in question_specs:
        field_name = str(spec.get("field_name") or "").strip()
        if not field_name:
            continue
        single_option_answer = _required_single_option_select_answer(spec)
        if single_option_answer is not None:
            answers[field_name] = single_option_answer
            continue
        affiliation_answer = _source_backed_affiliation_answer(
            spec,
            candidate_context_text=candidate_context_text,
        )
        if affiliation_answer is not None:
            answers[field_name] = affiliation_answer
            continue
        question_text = "\n".join(
            part.strip()
            for part in (str(spec.get("label") or ""), str(spec.get("description") or ""))
            if part and str(part).strip()
        )
        shared_text_answer = shared_text_answer_for_question(
            question_text,
            application_profile,
            master_resume_text=master_resume_text,
            work_stories_text=work_stories_text,
        )
        policy = resolve_shared_question_policy(
            question_text,
            application_profile,
            master_resume_text=master_resume_text,
            work_stories_text=work_stories_text,
            candidate_context_text=candidate_context_text,
            company_name=company_name,
        )
        raw_field_type = str(spec.get("type") or "").strip().casefold()
        if (
            policy is not None
            and policy.category == "conflict_of_interest"
            and bool(spec.get("required"))
            and raw_field_type in _FREE_TEXT_GENERATION_FIELD_TYPES
            and normalize_text(policy.text_value) in {"yes", "no"}
        ):
            answers[field_name] = policy.text_value
            continue
        if question_prefers_generated_free_text_answer(
            question_text,
            field_type=raw_field_type,
            policy=policy,
            category=policy.category if policy is not None else None,
        ) and not _shared_text_answer_overrides_generation_gate(shared_text_answer):
            continue
        if (
            _is_textual_question_spec(spec)
            and shared_text_answer
            and policy is not None
            and normalize_text(policy.text_value) in {"yes", "no", "n a", "na", "n/a"}
            and normalize_text(shared_text_answer) != normalize_text(policy.text_value)
        ):
            answers[field_name] = shared_text_answer
            continue
        if policy is not None and policy.text_value is not None:
            option_labels = [str(option).strip() for option in (spec.get("options") or []) if str(option or "").strip()]
            if policy.category == "company_engagement" and option_labels:
                option = _best_engagement_option(option_labels)
                if option:
                    answers[field_name] = option
                    continue
            if option_labels:
                from autofill_common import select_shared_policy_option

                matched_option = select_shared_policy_option(
                    option_labels,
                    policy,
                    application_profile=application_profile,
                )
                if matched_option:
                    answers[field_name] = [matched_option] if "multi_select" in raw_field_type else matched_option
                    continue
            answers[field_name] = policy.text_value
            continue
        if shared_text_answer:
            answers[field_name] = shared_text_answer
    return answers


def _deterministic_answer_provider_name(
    *,
    linked_resource_answers: dict[str, str],
    classified_answers: dict[str, str],
) -> str:
    if linked_resource_answers and classified_answers:
        return "deterministic"
    if linked_resource_answers:
        return "deterministic_linked_resource"
    if classified_answers:
        return "deterministic_classification"
    return "deterministic"


def resolve_shared_question_policy(
    text: str | None,
    application_profile: ApplicationProfile,
    *,
    master_resume_text: str | None = None,
    work_stories_text: str | None = None,
    candidate_context_text: str | None = None,
    company_name: str | None = None,
    job_url: str | None = None,
    source_url: str | None = None,
    source_hint: str | None = None,
) -> SharedQuestionPolicy | None:
    if not text:
        return None

    from question_classifier import classify_question

    category = classify_question(text)
    if category is None:
        return None

    if category == "experience_confirmation":
        specialist_domain_boolean = _specialist_domain_experience_boolean(
            text,
            master_resume_text=master_resume_text,
        )
        if specialist_domain_boolean is not None:
            return SharedQuestionPolicy(
                category=category,
                source="master_resume.md" if specialist_domain_boolean else "unsupported_specialist_domain_experience",
                boolean_value=specialist_domain_boolean,
                text_value="Yes" if specialist_domain_boolean else "No",
                is_positive_fit=specialist_domain_boolean,
            )

    if category == "skill_confirmation":
        explicit_skill_support = _resume_explicit_skill_support(
            text,
            master_resume_text=master_resume_text,
        )
        if explicit_skill_support:
            return SharedQuestionPolicy(
                category=category,
                source="master_resume.md",
                boolean_value=True,
                text_value="Yes",
                is_positive_fit=True,
            )
        return SharedQuestionPolicy(
            category=category,
            source="shared_positive_fit_policy",
            boolean_value=True,
            text_value="Yes",
            is_positive_fit=True,
        )

    if category == "skill_years_experience":
        resume_answer = _resume_domain_years_answer(text, master_resume_text=master_resume_text)
        answer = _profile_skill_years_answer(
            text,
            application_profile,
            master_resume_text=master_resume_text,
        )
        return SharedQuestionPolicy(
            category=category,
            source=(
                "master_resume.md"
                if answer is not None and resume_answer is not None
                else "application_profile.md" if answer is not None else "unsupported_skill_years_experience"
            ),
            boolean_value=None,
            text_value=answer,
            is_positive_fit=False,
        )

    if category == "location_residency":
        explicit_location_match = _location_residency_boolean(text, application_profile)
        if explicit_location_match is not None:
            return SharedQuestionPolicy(
                category=category,
                source="application_profile.md",
                boolean_value=explicit_location_match,
                text_value="Yes" if explicit_location_match else "No",
                is_positive_fit=explicit_location_match,
            )
        return SharedQuestionPolicy(
            category=category,
            source="shared_positive_fit_policy",
            boolean_value=True,
            text_value="Yes",
            is_positive_fit=True,
        )

    if category == "city_location":
        location = str(getattr(application_profile, "location", "") or "").strip()
        if not location:
            return None
        return SharedQuestionPolicy(
            category=category,
            source="application_profile.md",
            boolean_value=None,
            text_value=location,
            is_positive_fit=False,
        )

    if category in POSITIVE_FIT_CATEGORIES:
        return SharedQuestionPolicy(
            category=category,
            source="shared_positive_fit_policy",
            boolean_value=True,
            text_value="Yes",
            is_positive_fit=True,
        )

    if category == "startup_experience":
        if not question_is_startup_experience_confirmation(text):
            return None
        return SharedQuestionPolicy(
            category=category,
            source="shared_positive_fit_policy",
            boolean_value=True,
            text_value="Yes",
            is_positive_fit=True,
        )

    if category == "pm_people_management":
        supported_years = _coerce_year_count(application_profile.pm_people_management_years)
        required_years = _pm_people_management_year_requirement(text)
        if supported_years is not None:
            source = "application_profile.md"
            supported = required_years is None or supported_years >= required_years
        else:
            source = _pm_people_management_support_source(
                master_resume_text=master_resume_text,
                work_stories_text=work_stories_text,
                candidate_context_text=candidate_context_text,
            )
            supported = source is not None
        return SharedQuestionPolicy(
            category=category,
            source=source or "deterministic_no_pm_people_management_support",
            boolean_value=supported,
            text_value="Yes" if supported else "No",
            is_positive_fit=supported,
        )

    if category == "location_cost_tier":
        answer = _location_cost_tier_answer(application_profile)
        if answer is None:
            return None
        return SharedQuestionPolicy(
            category=category,
            source="application_profile.md",
            boolean_value=None,
            text_value=answer,
            is_positive_fit=False,
        )

    if category == "credential_claim":
        if question_requests_professional_license_intent(text):
            source = _credential_support_source(text, application_profile, master_resume_text)
            supported = source is not None
            return SharedQuestionPolicy(
                category=category,
                source=source or "deterministic_no_professional_credentials",
                boolean_value=supported,
                text_value="Yes" if supported else "No",
                is_positive_fit=supported,
                credential_supported=supported,
            )
        source = _credential_support_source(text, application_profile, master_resume_text)
        supported = source is not None
        return SharedQuestionPolicy(
            category=category,
            source=source or "unsupported_credential_claim",
            boolean_value=True if supported else None,
            text_value="Yes" if supported else None,
            is_positive_fit=True,
            credential_supported=supported,
        )

    if category == "education" and question_is_binary_education_completion(text):
        completed = _education_supports_credential_claim(text, application_profile)
        return SharedQuestionPolicy(
            category=category,
            source="application_profile.md",
            boolean_value=completed,
            text_value=_bool_to_text(completed),
            is_positive_fit=completed,
        )

    if category == "work_authorization":
        if question_is_restricted_country_citizenship_status(text):
            restricted_country_citizenship = getattr(
                application_profile,
                "citizen_of_cuba_iran_north_korea_or_syria",
                None,
            )
            return SharedQuestionPolicy(
                category=category,
                source=(
                    "application_profile.md"
                    if restricted_country_citizenship is not None
                    else "unsupported_restricted_country_citizenship_status"
                ),
                boolean_value=restricted_country_citizenship,
                text_value=_bool_to_text(restricted_country_citizenship),
                is_positive_fit=False,
            )
        us_person_answer, us_person_source = _u_s_person_answer(
            text,
            application_profile,
            master_resume_text=master_resume_text,
        )
        if question_is_u_s_person_status(text):
            return SharedQuestionPolicy(
                category=category,
                source=us_person_source or "unsupported_u_s_person_status",
                boolean_value=True if us_person_answer is not None else None,
                text_value=us_person_answer,
                is_positive_fit=False,
            )
        boolean_value = _work_authorization_boolean(text, application_profile)
        return SharedQuestionPolicy(
            category=category,
            source="application_profile.md",
            boolean_value=boolean_value,
            text_value=build_truthful_work_authorization_answer(
                text,
                application_profile,
                master_resume_text=master_resume_text,
            ),
            is_positive_fit=False,
        )

    if category == "salary_comfort":
        boolean_value = application_profile.comfortable_with_posted_salary
        return SharedQuestionPolicy(
            category=category,
            source="application_profile.md",
            boolean_value=boolean_value,
            text_value=_bool_to_text(boolean_value),
            is_positive_fit=False,
        )

    if category == "compensation":
        return SharedQuestionPolicy(
            category=category,
            source="application_profile.md",
            boolean_value=None,
            text_value=getattr(application_profile, "compensation_expectations", None),
            is_positive_fit=False,
        )

    if category == "travel_percentage":
        return SharedQuestionPolicy(
            category=category,
            source="application_profile.md",
            boolean_value=None,
            text_value=getattr(application_profile, "maximum_travel_percentage", None),
            is_positive_fit=False,
        )

    if category == "availability_timing":
        answer = _profile_availability_timing_answer(text, application_profile)
        return SharedQuestionPolicy(
            category=category,
            source="application_profile.md" if answer is not None else "unsupported_availability_timing",
            boolean_value=None,
            text_value=answer,
            is_positive_fit=False,
        )

    if category == "interview_recording_consent":
        consent = getattr(application_profile, "interview_recording_consent", None)
        return SharedQuestionPolicy(
            category=category,
            source="application_profile.md" if consent is not None else "unsupported_interview_recording_consent",
            boolean_value=consent,
            text_value=_bool_to_text(consent),
            is_positive_fit=False,
        )

    if category == "ai_captcha":
        return SharedQuestionPolicy(
            category=category,
            source="master_resume.md",
            boolean_value=None,
            text_value=_candidate_first_name_answer(master_resume_text),
            is_positive_fit=False,
        )

    if category == "undergraduate_gpa":
        return SharedQuestionPolicy(
            category=category,
            source="application_profile.md",
            boolean_value=None,
            text_value=getattr(application_profile, "undergraduate_gpa", None),
            is_positive_fit=False,
        )

    if category == "application_status_sms_optin":
        return SharedQuestionPolicy(
            category=category,
            source="deterministic",
            boolean_value=True,
            text_value="Yes",
            is_positive_fit=False,
        )

    if category == "profile_included_confirmation":
        included_boolean, source = _profile_included_confirmation_availability(
            text,
            application_profile,
            master_resume_text=master_resume_text,
        )
        if included_boolean is None or source is None:
            return None
        return SharedQuestionPolicy(
            category=category,
            source=source,
            boolean_value=included_boolean,
            text_value=_bool_to_text(included_boolean),
            is_positive_fit=False,
        )

    if category == "culture_careers_optin":
        return SharedQuestionPolicy(
            category=category,
            source="deterministic",
            boolean_value=False,
            text_value="No",
            is_positive_fit=False,
        )

    if category == "prior_employment":
        boolean_value = _resume_history_contains_subject(text, master_resume_text)
        if boolean_value is None:
            return None
        return SharedQuestionPolicy(
            category=category,
            source="master_resume.md",
            boolean_value=boolean_value,
            text_value=_bool_to_text(boolean_value),
            is_positive_fit=False,
        )

    if category == "current_employer_affiliation":
        boolean_value = _current_employer_matches_subject(text, master_resume_text)
        if boolean_value is None:
            return None
        return SharedQuestionPolicy(
            category=category,
            source="master_resume.md",
            boolean_value=boolean_value,
            text_value=_bool_to_text(boolean_value),
            is_positive_fit=False,
        )

    if category == "prior_application":
        boolean_value = _prior_application_history_boolean(text, company_name=company_name)
        if boolean_value is None:
            return None
        return SharedQuestionPolicy(
            category=category,
            source="jobs.db",
            boolean_value=boolean_value,
            text_value=_bool_to_text(boolean_value),
            is_positive_fit=False,
        )

    if category == "employee_referral":
        boolean_value, source = _employee_referral_boolean(application_profile)
        return SharedQuestionPolicy(
            category=category,
            source=source,
            boolean_value=boolean_value,
            text_value=_bool_to_text(boolean_value),
            is_positive_fit=False,
        )

    if category == "truthfulness_attestation":
        return SharedQuestionPolicy(
            category=category,
            source="deterministic",
            boolean_value=True,
            text_value="Yes",
            is_positive_fit=False,
        )

    if category == "legal_age":
        return SharedQuestionPolicy(
            category=category,
            source="deterministic",
            boolean_value=True,
            text_value="Yes",
            is_positive_fit=False,
        )

    if category == "background_check_consent":
        return SharedQuestionPolicy(
            category=category,
            source="deterministic",
            boolean_value=True,
            text_value="Yes",
            is_positive_fit=False,
        )

    if category == "interview_ai_policy_consent":
        return SharedQuestionPolicy(
            category=category,
            source="deterministic",
            boolean_value=True,
            text_value="Yes",
            is_positive_fit=False,
        )

    if category == "company_engagement":
        return SharedQuestionPolicy(
            category=category,
            source="shared_positive_fit_policy",
            boolean_value=True,
            text_value="Yes",
            is_positive_fit=True,
        )

    if category == "relocation_assistance_requirement":
        return SharedQuestionPolicy(
            category=category,
            source="deterministic",
            boolean_value=False,
            text_value="No",
            is_positive_fit=False,
        )

    if category == "how_did_you_hear":
        candidates, source = resolve_how_did_you_hear_candidates(
            application_profile,
            company_name=company_name,
            job_url=job_url,
            source_url=source_url,
            source_hint=source_hint,
        )
        return SharedQuestionPolicy(
            category=category,
            source=source,
            boolean_value=None,
            text_value=candidates[0] if candidates else None,
            is_positive_fit=False,
        )

    if category == "nda_noncompete":
        return SharedQuestionPolicy(
            category=category,
            source="deterministic",
            boolean_value=False,
            text_value="No",
            is_positive_fit=False,
        )

    if category == "conflict_of_interest":
        return SharedQuestionPolicy(
            category=category,
            source="deterministic",
            boolean_value=False,
            text_value="No",
            is_positive_fit=False,
        )

    if category == "interview_accommodation":
        return SharedQuestionPolicy(
            category=category,
            source="deterministic",
            boolean_value=False,
            text_value="No",
            is_positive_fit=False,
        )

    if category == "reasonable_accommodation":
        return SharedQuestionPolicy(
            category=category,
            source="deterministic",
            boolean_value=True,
            text_value="Yes",
            is_positive_fit=False,
        )

    return None


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in SENTENCE_SPLIT_RE.split(text.strip()) if part.strip()]


def extract_cover_letter_paragraphs(text: str) -> list[str]:
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


def build_company_specific_answer(company: str, paragraphs: list[str], *, max_sentences: int = 4) -> str:
    company_lower = company.lower()
    first_sentence_matches = [
        paragraph
        for paragraph in paragraphs
        if split_sentences(paragraph) and company_lower in split_sentences(paragraph)[0].lower()
    ]
    prioritized = [paragraph for paragraph in paragraphs if company_lower in paragraph.lower()]
    source = first_sentence_matches or prioritized or paragraphs

    sentences: list[str] = []
    seen: set[str] = set()
    for paragraph in source:
        for sentence in split_sentences(paragraph):
            key = sentence.casefold()
            if key in seen:
                continue
            seen.add(key)
            sentences.append(sentence)
            if len(sentences) >= max_sentences:
                return " ".join(sentences)
    return " ".join(sentences)
