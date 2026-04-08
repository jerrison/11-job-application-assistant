"""Shared candidate/application models and profile parsing helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True)
class CandidateProfile:
    full_name: str
    first_name: str
    last_name: str
    email: str
    phone: str
    location: str
    linkedin: str | None
    website: str | None
    work_authorized: bool
    employers: set[str]


@dataclass(slots=True)
class ApplicationProfile:
    country: str
    location: str
    work_authorization_statement: str
    authorized_to_work_unconditionally: bool
    require_sponsorship_now: bool
    require_sponsorship_future: bool
    minimum_years_experience: bool
    sponsorship_answer: str
    lives_in_job_location: bool
    willing_to_relocate: bool
    comfortable_working_on_site: bool
    comfortable_with_posted_salary: bool
    text_message_consent: bool
    available_cities: list[str] | None = None
    street_address: str | None = None
    zip_code: str | None = None
    age_range: str | None = None
    gender: str | None = None
    gender_identity: str | None = None
    transgender_status: str | None = None
    race_or_ethnicity: str | None = None
    veteran_status: str | None = None
    disability_status: str | None = None
    sexual_orientation: str | None = None
    pronouns: str | None = None
    verification_code_email: str | None = None
    how_did_you_hear: str | None = None
    linkedin: str | None = None
    github: str | None = None
    website: str | None = None
    communities: str | None = None
    education_entries: list[str] | None = None
    education_graduation_month_years: list[str | None] | None = None
    compensation_expectations: str | None = None
    compensation_numeric_fallback: str | None = None
    undergraduate_gpa: str | None = None
    citizen_of_cuba_iran_north_korea_or_syria: bool | None = None
    maximum_travel_percentage: str | None = None
    notice_period: str | None = None
    earliest_start_timing: str | None = None
    interview_recording_consent: bool | None = None
    default_skill_years: str | None = None
    pm_people_management_years: str | None = None
    skill_years: dict[str, str] | None = None


@dataclass(slots=True)
class CandidateContactDetails:
    street_address: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None


@dataclass(slots=True)
class SharedQuestionPolicy:
    category: str
    source: str
    boolean_value: bool | None
    text_value: str | None
    is_positive_fit: bool
    credential_supported: bool = False


def parse_bool(value: str, *, field_name: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"yes", "y", "true"}:
        return True
    if normalized in {"no", "n", "false"}:
        return False
    raise ValueError(f"Expected Yes/No for {field_name}, got {value!r}")


def normalize_url(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    return f"https://{value}"


def _normalize_compensation_expectations(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(
        r"\s*if the field requires a numeric-only amount,\s*enter\s+\d+\.?\s*$",
        "",
        value.strip(),
        flags=re.I,
    ).strip()
    return normalized or None


def _extract_compensation_numeric_fallback(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(
        r"if the field requires a numeric-only amount,\s*enter\s+(\d+(?:\.\d+)?)(?:[.!?])?(?:\s|$)",
        value,
        flags=re.I,
    )
    if not match:
        return None
    return match.group(1)


def _normalize_percentage_text(value: str | None, *, field_name: str) -> str | None:
    if not value:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    match = re.fullmatch(r"(?:up to\s+)?(\d{1,3})(?:\s*%)?", stripped, flags=re.I)
    if not match:
        return stripped
    percentage = int(match.group(1))
    if not 0 <= percentage <= 100:
        raise ValueError(f"Expected 0-100% for {field_name}, got {value!r}")
    return f"{percentage}%"


def _normalize_month_year(value: str | None, *, field_name: str) -> str | None:
    if not value:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    match = re.fullmatch(r"(\d{1,2})\s*/\s*(\d{4})", stripped)
    if not match:
        raise ValueError(f"Expected MM/YYYY for {field_name}, got {value!r}")
    month = int(match.group(1))
    year = int(match.group(2))
    if not 1 <= month <= 12:
        raise ValueError(f"Expected valid month for {field_name}, got {value!r}")
    return f"{month:02d}/{year:04d}"


def _normalize_skill_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _normalize_skill_years_value(value: str | None) -> str | None:
    if not value:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    digit_match = re.search(r"\b(\d{1,2})\b", stripped)
    if digit_match:
        return digit_match.group(1)
    return stripped


def parse_candidate_contact_details(text: str) -> CandidateContactDetails:
    address_patterns = (
        re.compile(
            r"\bLives in\s+(?P<street>[^,\n]+),\s*(?P<city>[A-Za-z .'-]+?)\s*,?\s+(?P<state>[A-Z]{2})\s*,?\s*(?P<zip>\d{5}(?:-\d{4})?)\b",
            re.I,
        ),
        re.compile(
            r"\bAddress:\s*(?P<street>[^,\n]+),\s*(?P<city>[A-Za-z .'-]+?)\s*,?\s+(?P<state>[A-Z]{2})\s*,?\s*(?P<zip>\d{5}(?:-\d{4})?)\b",
            re.I,
        ),
    )
    for raw_line in text.splitlines():
        line = raw_line.replace("\ufeff", "").strip()
        if line.startswith("*"):
            line = line[1:].strip()
        if not line:
            continue
        for pattern in address_patterns:
            match = pattern.search(line)
            if not match:
                continue
            groups = match.groupdict()
            return CandidateContactDetails(
                street_address=groups["street"].strip(),
                city=groups["city"].strip(),
                state=groups["state"].strip().upper(),
                zip_code=groups["zip"].strip(),
            )
    return CandidateContactDetails()


def parse_application_profile(text: str) -> ApplicationProfile:
    aliases = {
        "country": "country",
        "location": "location",
        "city": "location",
        "current location": "location",
        "street address": "street_address",
        "address line 1": "street_address",
        "mailing address": "street_address",
        "zip": "zip_code",
        "zip code": "zip_code",
        "postal code": "zip_code",
        "postal / zip code": "zip_code",
        "work authorization statement": "work_authorization_statement",
        "authorized to work unconditionally": "authorized_to_work_unconditionally",
        "require sponsorship now": "require_sponsorship_now",
        "need sponsorship now": "require_sponsorship_now",
        "require sponsorship in future": "require_sponsorship_future",
        "need sponsorship in future": "require_sponsorship_future",
        "minimum years of experience": "minimum_years_experience",
        "minimum years experience": "minimum_years_experience",
        "meets minimum years of experience": "minimum_years_experience",
        "meets minimum years experience": "minimum_years_experience",
        "meets minimum years of experience requirement": "minimum_years_experience",
        "meets minimum years experience requirement": "minimum_years_experience",
        "sponsorship answer": "sponsorship_answer",
        "available cities": "available_cities",
        "live in job location": "lives_in_job_location",
        "live where the job is": "lives_in_job_location",
        "currently live in job location": "lives_in_job_location",
        "willing to relocate": "willing_to_relocate",
        "open to relocate": "willing_to_relocate",
        "open to relocation": "willing_to_relocate",
        "comfortable working on site": "comfortable_working_on_site",
        "comfortable working on-site": "comfortable_working_on_site",
        "comfortable working onsite": "comfortable_working_on_site",
        "comfortable working in person": "comfortable_working_on_site",
        "comfortable working in-person": "comfortable_working_on_site",
        "comfortable with posted salary": "comfortable_with_posted_salary",
        "comfortable with the posted salary": "comfortable_with_posted_salary",
        "comfortable interviewing for the salary outlined in the job description": "comfortable_with_posted_salary",
        "comfortable interviewing for the salary outlined": "comfortable_with_posted_salary",
        "comfortable with the salary outlined in the job description": "comfortable_with_posted_salary",
        "comfortable with the salary range": "comfortable_with_posted_salary",
        "comfortable with the compensation range": "comfortable_with_posted_salary",
        "maximum travel percentage": "maximum_travel_percentage",
        "max travel percentage": "maximum_travel_percentage",
        "travel percentage": "maximum_travel_percentage",
        "travel percentage limit": "maximum_travel_percentage",
        "notice period": "notice_period",
        "current notice period": "notice_period",
        "notice period to your current employer": "notice_period",
        "earliest start timing": "earliest_start_timing",
        "earliest start": "earliest_start_timing",
        "earliest available to start": "earliest_start_timing",
        "interview recording consent": "interview_recording_consent",
        "consent to interview recording": "interview_recording_consent",
        "default years of experience": "default_skill_years",
        "pm people management years": "pm_people_management_years",
        "years managing product managers": "pm_people_management_years",
        "years of product manager people management experience": "pm_people_management_years",
        "compensation expectations": "compensation_expectations",
        "undergraduate gpa": "undergraduate_gpa",
        "undergraduate (bachelor's) gpa": "undergraduate_gpa",
        "bachelor's gpa": "undergraduate_gpa",
        "bachelors gpa": "undergraduate_gpa",
        "citizen of cuba, iran, north korea, or syria": "citizen_of_cuba_iran_north_korea_or_syria",
        "citizen of cuba, iran, north korea or syria": "citizen_of_cuba_iran_north_korea_or_syria",
        "text message consent": "text_message_consent",
        "sms consent": "text_message_consent",
        "receive text message updates": "text_message_consent",
        "consent to text messages": "text_message_consent",
        "age range": "age_range",
        "age group": "age_range",
        "gender": "gender",
        "gender identity": "gender_identity",
        "transgender": "transgender_status",
        "transgender status": "transgender_status",
        "transgender identity": "transgender_status",
        "race or ethnicity": "race_or_ethnicity",
        "race": "race_or_ethnicity",
        "ethnicity": "race_or_ethnicity",
        "veteran status": "veteran_status",
        "disability status": "disability_status",
        "sexual orientation": "sexual_orientation",
        "communities": "communities",
        "pronouns": "pronouns",
        "verification code email": "verification_code_email",
        "security code email": "verification_code_email",
        "how did you hear about us": "how_did_you_hear",
        "heard about us": "how_did_you_hear",
        "referral source": "how_did_you_hear",
        "linkedin": "linkedin",
        "linkedin profile": "linkedin",
        "github": "github",
        "github profile": "github",
        "github url": "github",
        "website": "website",
        "personal website": "website",
        "portfolio": "website",
    }

    parsed: dict[str, str] = {}
    parsed_skill_years: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        if ":" not in line:
            continue
        label, value = line.split(":", 1)
        normalized_label = re.sub(r"\s+", " ", label.strip().casefold())
        key = aliases.get(normalized_label)
        if key:
            parsed[key] = value.strip()
            continue
        skill_match = re.fullmatch(r"(?P<skill>.+?)\s+years?\s+of\s+(?:work\s+)?experience", normalized_label)
        if not skill_match:
            skill_match = re.fullmatch(r"years?\s+of\s+(?:work\s+)?experience\s+with\s+(?P<skill>.+)", normalized_label)
        if skill_match:
            normalized_value = _normalize_skill_years_value(value)
            normalized_skill = _normalize_skill_key(skill_match.group("skill"))
            if normalized_skill and normalized_value:
                parsed_skill_years[normalized_skill] = normalized_value

    required = [
        "country",
        "location",
        "work_authorization_statement",
        "authorized_to_work_unconditionally",
        "require_sponsorship_now",
        "require_sponsorship_future",
        "sponsorship_answer",
        "gender",
        "race_or_ethnicity",
        "veteran_status",
        "disability_status",
        "sexual_orientation",
    ]
    missing = [key for key in required if key not in parsed]
    if missing:
        raise ValueError(f"application_profile.md is missing required fields: {', '.join(missing)}")

    pronouns = parsed.get("pronouns") or None
    verification_code_email = parsed.get("verification_code_email") or None
    sponsorship_answer = parsed["sponsorship_answer"]
    if not sponsorship_answer:
        requires_sponsorship = parse_bool(
            parsed["require_sponsorship_now"],
            field_name="require sponsorship now",
        ) or parse_bool(
            parsed["require_sponsorship_future"],
            field_name="require sponsorship in future",
        )
        sponsorship_answer = "Yes" if requires_sponsorship else "No"

    education_entries: list[str] = []
    education_graduation_month_years: list[str | None] = []
    in_education = False
    education_graduation_pattern = re.compile(
        r"(?:\s*[|;]\s*graduation(?:\s+month\s+and\s+year|\s+date)?\s*:\s*(?P<value>\d{1,2}/\d{4}))\s*$",
        re.I,
    )
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.casefold().startswith("## education"):
            in_education = True
            continue
        if in_education:
            if stripped.startswith("##"):
                break
            if stripped.startswith("- "):
                entry_text = stripped[2:].strip()
                graduation_month_year: str | None = None
                match = education_graduation_pattern.search(entry_text)
                if match:
                    graduation_month_year = _normalize_month_year(
                        match.group("value"),
                        field_name="education graduation month and year",
                    )
                    entry_text = entry_text[: match.start()].rstrip(" |;")
                education_entries.append(entry_text)
                education_graduation_month_years.append(graduation_month_year)

    return ApplicationProfile(
        country=parsed["country"],
        location=parsed["location"],
        work_authorization_statement=parsed["work_authorization_statement"],
        authorized_to_work_unconditionally=parse_bool(
            parsed["authorized_to_work_unconditionally"],
            field_name="authorized to work unconditionally",
        ),
        require_sponsorship_now=parse_bool(
            parsed["require_sponsorship_now"],
            field_name="require sponsorship now",
        ),
        require_sponsorship_future=parse_bool(
            parsed["require_sponsorship_future"],
            field_name="require sponsorship in future",
        ),
        minimum_years_experience=parse_bool(
            parsed.get("minimum_years_experience", "Yes"),
            field_name="minimum years of experience",
        ),
        sponsorship_answer=sponsorship_answer,
        lives_in_job_location=parse_bool(
            parsed.get("lives_in_job_location", "Yes"),
            field_name="live in job location",
        ),
        willing_to_relocate=parse_bool(
            parsed.get("willing_to_relocate", "Yes"),
            field_name="willing to relocate",
        ),
        comfortable_working_on_site=parse_bool(
            parsed.get("comfortable_working_on_site", "Yes"),
            field_name="comfortable working on site",
        ),
        comfortable_with_posted_salary=parse_bool(
            parsed.get("comfortable_with_posted_salary", "Yes"),
            field_name="comfortable with posted salary",
        ),
        text_message_consent=parse_bool(
            parsed.get("text_message_consent", "No"),
            field_name="text message consent",
        ),
        available_cities=[city.strip() for city in parsed.get("available_cities", "").split(",") if city.strip()] or None,
        street_address=parsed.get("street_address") or None,
        zip_code=parsed.get("zip_code") or None,
        age_range=parsed.get("age_range") or None,
        gender=parsed["gender"] or None,
        gender_identity=parsed.get("gender_identity") or None,
        transgender_status=parsed.get("transgender_status", "No") or None,
        race_or_ethnicity=parsed["race_or_ethnicity"] or None,
        veteran_status=parsed["veteran_status"] or None,
        disability_status=parsed["disability_status"] or None,
        sexual_orientation=parsed["sexual_orientation"] or None,
        communities=parsed.get("communities") or None,
        pronouns=pronouns,
        verification_code_email=verification_code_email,
        how_did_you_hear=parsed.get("how_did_you_hear") or None,
        linkedin=normalize_url(parsed.get("linkedin")),
        github=normalize_url(parsed.get("github")),
        website=normalize_url(parsed.get("website")),
        education_entries=education_entries or None,
        education_graduation_month_years=(
            education_graduation_month_years
            if education_graduation_month_years and any(value is not None for value in education_graduation_month_years)
            else None
        ),
        compensation_expectations=_normalize_compensation_expectations(parsed.get("compensation_expectations")),
        compensation_numeric_fallback=_extract_compensation_numeric_fallback(parsed.get("compensation_expectations")),
        undergraduate_gpa=parsed.get("undergraduate_gpa") or None,
        citizen_of_cuba_iran_north_korea_or_syria=(
            parse_bool(
                parsed["citizen_of_cuba_iran_north_korea_or_syria"],
                field_name="citizen of cuba, iran, north korea, or syria",
            )
            if "citizen_of_cuba_iran_north_korea_or_syria" in parsed
            else None
        ),
        maximum_travel_percentage=_normalize_percentage_text(
            parsed.get("maximum_travel_percentage"),
            field_name="maximum travel percentage",
        ),
        notice_period=parsed.get("notice_period") or None,
        earliest_start_timing=parsed.get("earliest_start_timing") or None,
        interview_recording_consent=(
            parse_bool(
                parsed["interview_recording_consent"],
                field_name="interview recording consent",
            )
            if "interview_recording_consent" in parsed
            else None
        ),
        default_skill_years=_normalize_skill_years_value(parsed.get("default_skill_years")),
        pm_people_management_years=_normalize_skill_years_value(parsed.get("pm_people_management_years")),
        skill_years=parsed_skill_years or None,
    )


def parse_master_resume(text: str) -> CandidateProfile:
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
        for match in [re.match(r"^\s*(?:##\s+)?(.+?)\s+—", line)]
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
        linkedin=normalize_url(linkedin),
        website=normalize_url(website),
        work_authorized=work_authorized,
        employers=employers,
    )


def _bool_to_text(value: bool | None) -> str | None:
    if value is None:
        return None
    return "Yes" if value else "No"


def _normalized_education_entries(application_profile: ApplicationProfile) -> list[str]:
    return [
        re.sub(r"[^a-z0-9]+", " ", entry.casefold()).strip()
        for entry in application_profile.education_entries or []
        if entry
    ]
