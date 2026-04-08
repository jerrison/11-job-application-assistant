"""Unified question classifier — single entry point for deterministic question routing.

Maps question labels to categories using priority-ordered detector dispatch.
Board scripts call ``classify_question()`` and then let shared policy code decide
whether a category resolves to affirmative positive-fit, profile-driven, or
non-deterministic behavior.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_submit_common import (
    ApplicationProfile,
    _question_is_city_location,
    _question_is_company_engagement,
    _question_is_compensation,
    _question_is_conflict_of_interest,
    _question_is_current_employer_affiliation,
    _question_is_employee_referral_confirmation,
    _question_is_how_did_you_hear,
    _question_is_location_cost_tier,
    _question_is_nda_noncompete,
    _question_is_pm_people_management,
    _question_is_prior_application,
    _question_is_prior_employment_check,
    _question_is_product_usage,
    _question_is_truthfulness_attestation,
    _question_is_undergraduate_gpa,
    normalize_text,
    question_is_application_status_sms_optin,
    question_is_availability_timing_prompt,
    question_is_background_check_consent,
    question_is_credential_claim,
    question_is_culture_careers_optin,
    question_is_current_company_field,
    question_is_current_country_work_authorization,
    question_is_education,
    question_is_experience_confirmation,
    question_is_interview_accommodation_request,
    question_is_interview_ai_policy_consent,
    question_is_interview_recording_consent,
    question_is_legal_age_check,
    question_is_location_residency_check,
    question_is_minimum_experience_check,
    question_is_office_attendance_prompt,
    question_is_profile_included_confirmation,
    question_is_reasonable_accommodation_check,
    question_is_relocation_assistance_requirement,
    question_is_relocation_willingness,
    question_is_restricted_country_citizenship_status,
    question_is_role_commitment_prompt,
    question_is_salary_comfort_check,
    question_is_skill_confirmation,
    question_is_skill_years_experience,
    question_is_travel_percentage_prompt,
    question_is_travel_willingness,
    question_is_u_s_person_status,
    question_requests_sponsorship_requirement,
    question_requests_startup_experience,
)


@dataclass(slots=True)
class QuestionClassification:
    """Result of classifying a question label."""

    category: str  # e.g. "education", "salary_comfort"
    value: str | None  # deterministic answer, or None when board must decide
    source: str  # e.g. "application_profile.md", "deterministic"


# ---------------------------------------------------------------------------
# Supplemental detectors (logic not yet in application_submit_common)
# ---------------------------------------------------------------------------

_WORK_AUTH_FRAGMENTS = (
    "authorized to work",
    "authorization to work",
    "work authorization",
    "employment visa",
    "employment visa status",
    "work visa",
    "work permit",
    "legally authorized",
)

_CITY_LOCATION_EXTRA_FRAGMENTS = (
    "what city",
    "in which city",
)
_PREFERENCE_RANKING_CHOICE_TYPES = frozenset({"multi_value_single_select", "multi_value_multi_select"})
_PREFERENCE_RANKING_HINTS = (
    "most interested in",
    "most interest",
    "select up to",
    "select your top",
    "top 2",
    "top 3",
    "top 5",
    "rank your",
    "ranking",
    "order of preference",
    "most important to you",
    "priority order",
    "prioritize",
    "prioritise",
)
_PREFERENCE_RANKING_PREFERENCE_TERMS = (
    "prefer",
    "preference",
    "priority",
    "priorities",
    "interested",
    "important to you",
)
_PREFERENCE_RANKING_SUBJECT_TERMS = (
    "role",
    "roles",
    "team",
    "teams",
    "function",
    "functions",
    "product area",
    "product areas",
    "group",
    "groups",
    "domain",
    "domains",
    "focus area",
    "focus areas",
    "factor",
    "factors",
)
_AI_CAPTCHA_MACHINE_FRAGMENTS = (
    "if you are an ai",
    "large language model",
    "llm",
)
_AI_CAPTCHA_HUMAN_FRAGMENTS = (
    "if you are a human",
    "typing your first name",
    "type your first name",
    "first name in capital letters",
)


def _question_is_work_authorization(label: str) -> bool:
    """Detect work-authorization / sponsorship questions."""
    normalized = normalize_text(label)
    if not normalized:
        return False
    if question_is_restricted_country_citizenship_status(normalized):
        return True
    if question_is_u_s_person_status(normalized):
        return True
    if question_is_current_country_work_authorization(normalized):
        return True
    return question_requests_sponsorship_requirement(normalized) or any(
        frag in normalized for frag in _WORK_AUTH_FRAGMENTS
    )


def _question_is_startup_experience(label: str) -> bool:
    normalized = normalize_text(label)
    return bool(normalized and question_requests_startup_experience(normalized))


def _question_is_city_location_extended(label: str) -> bool:
    """Extended city/location check: original detector + supplemental fragments.

    Does NOT modify the upstream detector — just adds a second pass.
    """
    if _question_is_city_location(label):
        return True
    normalized = normalize_text(label)
    # Exclude yes/no questions that START with "do you ... live" (e.g. "Do you
    # currently live in the Bay Area?") — those are not asking for a city name.
    # But allow "What city ... do you ... live in?" where the question starts
    # with a "what" interrogative asking for a specific city.
    if re.search(r"^do you\b.*\blive\b", normalized):
        return False
    return any(frag in normalized for frag in _CITY_LOCATION_EXTRA_FRAGMENTS)


def _question_is_preference_ranking(label: str, field_type: str | None = None) -> bool:
    """Detect choice prompts asking the candidate to rank or choose preferences."""
    if field_type not in _PREFERENCE_RANKING_CHOICE_TYPES:
        return False
    normalized = normalize_text(label)
    if not normalized:
        return False
    if any(fragment in normalized for fragment in _PREFERENCE_RANKING_HINTS):
        return True
    return any(term in normalized for term in _PREFERENCE_RANKING_PREFERENCE_TERMS) and any(
        subject in normalized for subject in _PREFERENCE_RANKING_SUBJECT_TERMS
    )


def _question_is_ai_captcha(label: str) -> bool:
    """Detect anti-AI prompts that ask a human to type their name."""
    normalized = normalize_text(label)
    if not normalized:
        return False
    return any(fragment in normalized for fragment in _AI_CAPTCHA_MACHINE_FRAGMENTS) and any(
        fragment in normalized for fragment in _AI_CAPTCHA_HUMAN_FRAGMENTS
    )


# ---------------------------------------------------------------------------
# Main classifier
# ---------------------------------------------------------------------------


def _classify_question_impl(
    label: str,
    field_type: str | None = None,
    application_profile: ApplicationProfile | None = None,
    out_dir: Path | None = None,
) -> str | None:
    """Classify a question label and return its category string, or ``None``.

    Detectors are checked in priority order; the first match wins.
    """
    if not label:
        return None

    # 1. current_company
    if question_is_current_company_field(field_name=None, label=label):
        return "current_company"

    # 2. nda_noncompete
    if _question_is_nda_noncompete(label):
        return "nda_noncompete"

    # 3. conflict_of_interest
    if _question_is_conflict_of_interest(label):
        return "conflict_of_interest"

    # 4. prior_employment
    if _question_is_prior_employment_check(label):
        return "prior_employment"

    # 5. current_employer_affiliation
    if _question_is_current_employer_affiliation(label):
        return "current_employer_affiliation"

    # 6. prior_application
    if _question_is_prior_application(label):
        return "prior_application"

    # 7. employee_referral
    if _question_is_employee_referral_confirmation(label):
        return "employee_referral"

    # 8. truthfulness_attestation
    if _question_is_truthfulness_attestation(label):
        return "truthfulness_attestation"

    # 9. legal_age
    if question_is_legal_age_check(label):
        return "legal_age"

    # 10. background_check_consent
    if question_is_background_check_consent(label):
        return "background_check_consent"

    # 11. interview_recording_consent
    if question_is_interview_recording_consent(label):
        return "interview_recording_consent"

    # 12. interview_ai_policy_consent
    if question_is_interview_ai_policy_consent(label):
        return "interview_ai_policy_consent"

    # 13. application_status_sms_optin
    if question_is_application_status_sms_optin(label):
        return "application_status_sms_optin"

    # 13. interview_accommodation
    #    Guard: if the label is primarily about office attendance or location and
    #    just *mentions* "reasonable accommodation" incidentally (e.g. as a legal
    #    caveat), let the office/city detector handle it instead.
    if question_is_interview_accommodation_request(label):
        if not (question_is_office_attendance_prompt(label) or _question_is_city_location(label)):
            return "interview_accommodation"

    # 14. reasonable_accommodation
    #    Guard: if the label is primarily about office attendance or location and
    #    just *mentions* "reasonable accommodation" incidentally (e.g. as a legal
    #    caveat), let the office/city detector handle it instead.
    if question_is_reasonable_accommodation_check(label):
        if not (question_is_office_attendance_prompt(label) or _question_is_city_location(label)):
            return "reasonable_accommodation"

    # 15. ai_captcha
    if _question_is_ai_captcha(label):
        return "ai_captcha"

    # 16. salary_comfort  (must precede compensation — both can match salary text)
    if question_is_salary_comfort_check(label):
        return "salary_comfort"

    # 17. compensation
    if _question_is_compensation(label):
        return "compensation"

    # 18. minimum_experience
    if question_is_minimum_experience_check(label):
        return "minimum_experience"

    # 19. skill_years_experience
    if question_is_skill_years_experience(label):
        return "skill_years_experience"

    # 20. startup_experience
    if _question_is_startup_experience(label):
        return "startup_experience"

    # 21. skill_confirmation
    if question_is_skill_confirmation(label):
        return "skill_confirmation"

    # 22. experience_confirmation
    if question_is_experience_confirmation(label):
        return "experience_confirmation"

    # 23. pm_people_management
    if _question_is_pm_people_management(label):
        return "pm_people_management"

    # 24. product_usage
    if _question_is_product_usage(label):
        return "product_usage"

    # 25. city_location  (extended version catches "what city ..." patterns too)
    if _question_is_city_location_extended(label):
        return "city_location"

    # 26. office_attendance
    if question_is_office_attendance_prompt(label):
        return "office_attendance"

    # 27. availability_timing
    if question_is_availability_timing_prompt(label):
        return "availability_timing"

    # 28. role_commitment
    if question_is_role_commitment_prompt(label):
        return "role_commitment"

    # 29. relocation_assistance_requirement
    if question_is_relocation_assistance_requirement(label):
        return "relocation_assistance_requirement"

    # 30. relocation_willingness
    if question_is_relocation_willingness(label):
        return "relocation_willingness"

    # 30. travel_percentage
    if question_is_travel_percentage_prompt(label):
        return "travel_percentage"

    # 31. travel_willingness
    if question_is_travel_willingness(label):
        return "travel_willingness"

    # 32. location_residency
    if question_is_location_residency_check(label):
        return "location_residency"

    # 33. credential_claim
    if question_is_credential_claim(label):
        return "credential_claim"

    # 34. location_cost_tier
    if _question_is_location_cost_tier(label):
        return "location_cost_tier"

    # 35. company_engagement
    if _question_is_company_engagement(label):
        return "company_engagement"

    # 36. how_did_you_hear
    if _question_is_how_did_you_hear(label):
        return "how_did_you_hear"

    # 37. profile_included_confirmation
    if question_is_profile_included_confirmation(label):
        return "profile_included_confirmation"

    # 38. culture_careers_optin
    if question_is_culture_careers_optin(label):
        return "culture_careers_optin"

    # 39. undergraduate_gpa
    if _question_is_undergraduate_gpa(label):
        return "undergraduate_gpa"

    # 40. education
    if question_is_education(label):
        return "education"

    # 41. work_authorization (supplemental — kept after education so mixed
    # degree/authorization wording does not bypass credential handling)
    if _question_is_work_authorization(label):
        return "work_authorization"

    # 42. preference_ranking (choice prompts only; Greenhouse applies a second
    # board-local eligibility gate before routing these into live research)
    if _question_is_preference_ranking(label, field_type):
        return "preference_ranking"

    return None


class _Classifier:
    """Non-descriptor callable so ``cls.classify_question = classify_question``
    in test classes doesn't bind ``self`` as the first argument.

    Regular functions implement ``__get__`` (the descriptor protocol), which
    causes ``self.classify_question(label)`` to inject the test instance as the
    first positional arg.  Wrapping in a plain callable avoids that.
    """

    __slots__ = ()

    @staticmethod
    def __call__(
        label: str,
        field_type: str | None = None,
        application_profile: ApplicationProfile | None = None,
        out_dir: Path | None = None,
    ) -> str | None:
        return _classify_question_impl(label, field_type, application_profile, out_dir)


classify_question = _Classifier()
