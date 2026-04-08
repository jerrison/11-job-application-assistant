#!/usr/bin/env python3
"""Rippling ATS application autofill.

Single-page form at ats.rippling.com -- no auth, no wizard.
Uses run_browser_pipeline().

URL patterns:
  - ats.rippling.com/{company}/jobs/{job_id}         (listing)
  - ats.rippling.com/{company}/jobs/{job_id}/apply    (application form)
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_submit_common import (
    APPLICATION_PROFILE_PATH,
    PROJECT_ROOT,
    GeneratedAnswerBlockersError,
    append_url_path_suffix,
    clear_pending_user_input,
    find_cover_letter_file,
    find_resume_file,
    generate_application_answers,
    json_dumps_pretty,
    load_meta,
    parse_application_profile,
    parse_master_resume,
    pending_user_input_reason_for_spec,
    primary_employer_name,
    question_is_culture_careers_optin,
    question_is_current_company_field,
    resolve_shared_question_policy,
    shared_text_answer_for_question,
    write_pending_user_input,
    write_pending_user_input_for_unconfirmed_fields,
)
from autofill_common import (
    board_file_constants,
    capture_full_page,
    click_submit_button,
    fill_basic_step,
    select_option,
    select_shared_policy_option,
)
from generated_answer_validation import _is_conditional_followup
from output_layout import migrate_role_output_layout, role_submit_path
from project_env import load_project_env
from text_normalization_helpers import normalize_text, slugify_label

_BOARD = "rippling"
_BOARD_CONSTANTS = board_file_constants(_BOARD)
AUTOFILL_PAYLOAD_JSON = _BOARD_CONSTANTS["payload_json"]
APPLICATION_PAGE_HTML = _BOARD_CONSTANTS["application_page_html"]
AUTOFILL_UNKNOWN_QUESTIONS_JSON = _BOARD_CONSTANTS["unknown_questions_json"]

SUBMIT_BUTTON_NAMES = (
    "Apply",
    "Submit",
    "Submit application",
    "Submit Application",
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
_NEXT_DATA_RE = re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(?P<payload>.*?)</script>', re.S)
_US_PERSON_WORK_AUTH_TOKENS = (
    "citizen",
    "permanent resident",
    "green card",
    "green-card",
    "refugee",
    "asylee",
    "asylum",
)
_NOT_APPLICABLE_CHOICES = ("N/A", "Not applicable")

load_project_env()


# --- Application inspection ---


def _fetch_application_html(application_url: str, cache_path: Path | None = None) -> str:
    """Fetch the Rippling application page HTML and optionally cache it."""
    request = Request(
        application_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0 Safari/537.36"
            )
        },
    )
    with urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", errors="replace")
    if cache_path is not None:
        cache_path.write_text(html, encoding="utf-8")
    return html


def _extract_next_data(html: str) -> dict:
    match = _NEXT_DATA_RE.search(html)
    if not match:
        raise ValueError("Could not find __NEXT_DATA__ in Rippling application page")
    return json.loads(match.group("payload"))


def _normalize_field_type(
    raw_type: str,
    *,
    is_multi_select_enabled: bool = False,
) -> str:
    normalized = str(raw_type or "").strip().casefold()
    if normalized in {"file", "upload"}:
        return "file"
    if normalized in {"long_answer", "long_text", "textarea"}:
        return "textarea"
    if normalized in {"single_select_dropdown", "single_select"}:
        return "multi_value_single_select"
    if normalized in {"multi_select_dropdown", "multi_select"} or is_multi_select_enabled:
        return "multi_value_multi_select"
    return "string"


def _field_kind(field_type: str) -> str:
    if field_type == "file":
        return "file"
    if field_type == "multi_value_single_select":
        return "select"
    if field_type == "multi_value_multi_select":
        return "checkbox"
    if field_type == "textarea":
        return "textarea"
    return "text"


def _rippling_fields(next_data: dict) -> list[dict]:
    page_props = next_data.get("props", {}).get("pageProps", {})
    api_data = page_props.get("apiData") or {}
    job_post = api_data.get("jobPost") or {}
    application = page_props.get("activeJobApplication") or job_post.get("activeJobApplication") or {}
    custom_questions = application.get("customQuestions") or {}
    additional_questions = application.get("additionalQuestions") or []

    fields: list[dict] = []
    seen_field_names: set[str] = set()

    def _unique_field_name(*parts: object) -> str:
        for part in parts:
            text = str(part or "").strip()
            if not text:
                continue
            base = slugify_label(text)
            if not base:
                continue
            candidate = base
            suffix = 2
            while candidate in seen_field_names:
                candidate = f"{base}_{suffix}"
                suffix += 1
            seen_field_names.add(candidate)
            return candidate
        candidate = "field"
        suffix = 2
        while candidate in seen_field_names:
            candidate = f"field_{suffix}"
            suffix += 1
        seen_field_names.add(candidate)
        return candidate

    for raw_field in custom_questions.get("fields") or []:
        label = str(raw_field.get("title") or raw_field.get("oid") or "").strip()
        if not label:
            continue
        field_type = _normalize_field_type(str(raw_field.get("fieldType") or ""))
        fields.append(
            {
                "field_name": _unique_field_name(raw_field.get("oid"), label),
                "label": label,
                "description": "",
                "required": bool(raw_field.get("required")),
                "type": field_type,
                "kind": _field_kind(field_type),
                "options": [],
                "path": str(raw_field.get("oid") or "").strip(),
                "source_group": "custom_question",
            }
        )

    for additional_group in additional_questions:
        form = additional_group.get("form") or {}
        section_name = str(additional_group.get("name") or "").strip()
        for raw_question in form.get("questions") or []:
            label = str(raw_question.get("title") or raw_question.get("uniqueKey") or "").strip()
            if not label:
                continue
            options = [str(option).strip() for option in raw_question.get("strChoices") or [] if str(option).strip()]
            field_type = _normalize_field_type(
                str(raw_question.get("questionType") or raw_question.get("dataType") or ""),
                is_multi_select_enabled=bool(raw_question.get("isMultiSelectEnabled")),
            )
            fields.append(
                {
                    "field_name": _unique_field_name(raw_question.get("uniqueKey"), label),
                    "label": label,
                    "description": str(raw_question.get("description") or "").strip(),
                    "required": bool(raw_question.get("isRequired")),
                    "type": field_type,
                    "kind": _field_kind(field_type),
                    "options": options,
                    "path": str(raw_question.get("uniqueKey") or "").strip(),
                    "source_group": section_name or "additional_question",
                }
            )
    return fields


def _profile_city(location: str | None) -> str:
    city = str(location or "").split(",", 1)[0].strip()
    return city or str(location or "").strip()


def _candidate_us_person(master_resume_text: str) -> bool | None:
    match = re.search(r"^\s*Work Authorization:\s*(.+)$", master_resume_text, re.M)
    if not match:
        return None
    normalized = normalize_text(match.group(1))
    if any(token in normalized for token in _US_PERSON_WORK_AUTH_TOKENS):
        return True
    return None


def _question_asks_us_person(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if "u s person" in normalized or "us person" in normalized:
        return True
    return (
        "protected individual" in normalized
        and "permanent resident" in normalized
        and "citizen" in normalized
    )


def _question_supports_not_applicable(field: dict) -> str | None:
    options = list(field.get("options") or [])
    for choice in _NOT_APPLICABLE_CHOICES:
        matched = select_option(options, choice)
        if matched:
            return matched
    return None


def _looks_like_conditional_followup(label: str) -> bool:
    normalized = normalize_text(label)
    if _is_conditional_followup({"label": label}):
        return True
    return normalized.startswith(("if yes", "if no", "if you answered", "if selected", "if applicable"))


def _write_unknown_questions(out_dir: Path, unknown_questions: list[dict]) -> None:
    path = role_submit_path(out_dir, AUTOFILL_UNKNOWN_QUESTIONS_JSON)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "questions": unknown_questions,
    }
    path.write_text(json_dumps_pretty(payload) + "\n", encoding="utf-8")


def _pending_user_input_fields(fields: list[dict], *, application_profile) -> list[dict]:
    pending: list[dict] = []
    for field in fields:
        reason = pending_user_input_reason_for_spec(field, application_profile)
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


def _field_supports_generated_answer(field: dict) -> bool:
    return field["type"] in {"string", "textarea", "multi_value_single_select", "multi_value_multi_select"}


def _question_specs(fields: list[dict], generated_field_names: set[str]) -> list[dict]:
    specs: list[dict] = []
    for field in fields:
        if field["field_name"] not in generated_field_names:
            continue
        spec: dict[str, object] = {
            "field_name": field["field_name"],
            "label": field["label"],
            "description": field.get("description", ""),
            "required": field["required"],
            "type": field["type"],
        }
        options = [str(option).strip() for option in field.get("options") or [] if str(option).strip()]
        if options:
            spec["options"] = options
        specs.append(spec)
    return specs


# --- Deterministic overrides ---


def _infer_deterministic(
    field: dict,
    *,
    application_profile,
    master_resume_text: str,
    company_name: str,
    job_url: str,
) -> tuple[str, str] | None:
    """Return a deterministic answer or None to defer to LLM."""
    label = str(field.get("label") or "")
    options = [str(option).strip() for option in field.get("options") or [] if str(option).strip()]
    field_type = str(field.get("type") or "")
    ll = label.casefold()
    policy = resolve_shared_question_policy(
        label,
        application_profile,
        master_resume_text=master_resume_text,
        company_name=company_name,
        job_url=job_url,
        source_url=job_url,
    )
    if _looks_like_conditional_followup(label):
        not_applicable = _question_supports_not_applicable(field)
        if not_applicable:
            return not_applicable, "deterministic"

    if _question_asks_us_person(label):
        is_us_person = _candidate_us_person(master_resume_text)
        if is_us_person is not None:
            answer = "Yes" if is_us_person else "No"
            return (select_option(options, answer) or answer), "master_resume.md"

    if policy is not None and policy.text_value is not None:
        if options:
            return (
                select_shared_policy_option(options, policy, application_profile=application_profile)
                or select_option(options, policy.text_value)
                or policy.text_value,
                policy.source,
            )
        return policy.text_value, policy.source

    shared_text = shared_text_answer_for_question(
        label,
        application_profile,
        master_resume_text=master_resume_text,
    )
    if shared_text is not None and field_type in {"string", "textarea"}:
        if "linkedin" in ll:
            return shared_text, "application_profile.md"
        if any(fragment in ll for fragment in ("website", "portfolio")):
            return shared_text, "application_profile.md"
        if "pronoun" in ll:
            return shared_text, "application_profile.md"
        if question_is_current_company_field(field_name=field.get("field_name"), label=label):
            return shared_text, "master_resume.md"
        if any(fragment in ll for fragment in ("salary", "compensation", "total rewards", "pay")):
            return shared_text, "application_profile.md"
        if any(fragment in ll for fragment in ("authorized", "authorised", "sponsorship", "visa", "work permit")):
            return shared_text, "application_profile.md"

    # Previous employee -> No
    if "previous employee" in ll or "former employee" in ll:
        answer = select_option(options, "No") or "No"
        return answer, "deterministic"

    # Work authorisation -> Yes
    if (
        "authorized to work" in ll
        or "authorised to work" in ll
        or "legally authorized" in ll
        or "eligible to work" in ll
        or "legally eligible" in ll
    ):
        answer = select_option(options, "Yes") or "Yes"
        return answer, "application_profile.md"

    # Sponsorship -> No
    if "require sponsorship" in ll or "need sponsorship" in ll or "commence or sponsor" in ll:
        answer = select_option(options, "No") or "No"
        return answer, "application_profile.md"

    # Age verification -> Yes
    if "18 years" in ll or "legal age" in ll:
        answer = select_option(options, "Yes") or "Yes"
        return answer, "deterministic"

    # Culture/careers opt-in -> No
    if question_is_culture_careers_optin(label):
        answer = select_option(options, "No") or "No"
        return answer, "deterministic"

    # SMS consent follows application_profile.md.
    if any(fragment in ll for fragment in ("text message", "text messages", "sms")):
        answer = "Yes" if application_profile.text_message_consent else "No"
        return (select_option(options, answer) or answer), "application_profile.md"

    return None


# --- Payload builder ---


def _standard_step(
    field: dict,
    *,
    out_dir: Path,
    profile,
    application_profile,
    master_resume_text: str,
) -> dict | None:
    label = str(field.get("label") or "")
    field_name = str(field.get("field_name") or "")
    field_type = str(field.get("type") or "")
    path = str(field.get("path") or "").strip()

    if field_type == "file":
        if "cover" in normalize_text(label):
            cover_letter_file = find_cover_letter_file(out_dir)
            if cover_letter_file is None:
                return None
            return {
                "field_name": field_name,
                "label": label,
                "kind": "file",
                "required": field["required"],
                "path": path,
                "field_type": field_type,
                "file_path": str(cover_letter_file),
                "source": "existing_cover_letter_asset",
            }
        resume_path = find_resume_file(out_dir)
        return {
            "field_name": field_name,
            "label": label,
            "kind": "file",
            "required": field["required"],
            "path": path,
            "field_type": field_type,
            "file_path": str(resume_path),
            "source": "existing_resume_asset",
        }

    standard_values: tuple[tuple[bool, str, str], ...] = (
        ("first name" in normalize_text(label) or field_name == "first_name", profile.first_name, "master_resume.md"),
        ("last name" in normalize_text(label) or field_name == "last_name", profile.last_name, "master_resume.md"),
        ("email" in normalize_text(label) or field_name == "email", profile.email, "master_resume.md"),
        (
            "phone" in normalize_text(label) or field_name in {"phone", "phone_number"},
            profile.phone or "",
            "master_resume.md",
        ),
        (
            "linkedin" in normalize_text(label),
            getattr(application_profile, "linkedin", None) or getattr(profile, "linkedin", None) or "",
            "application_profile.md" if getattr(application_profile, "linkedin", None) else "master_resume.md",
        ),
        (
            any(fragment in normalize_text(label) for fragment in ("website", "portfolio")),
            getattr(application_profile, "website", None) or getattr(profile, "website", None) or "",
            "application_profile.md" if getattr(application_profile, "website", None) else "master_resume.md",
        ),
        (
            question_is_current_company_field(field_name=field_name, label=label),
            primary_employer_name(master_resume_text),
            "master_resume.md",
        ),
        (
            "pronoun" in normalize_text(label),
            getattr(application_profile, "pronouns", None) or "",
            "application_profile.md",
        ),
    )
    for matched, value, source in standard_values:
        if not matched or not value:
            continue
        return {
            "field_name": field_name,
            "label": label,
            "kind": field["kind"],
            "required": field["required"],
            "path": path,
            "field_type": field_type,
            "value": value,
            "source": source,
        }

    if "location" in normalize_text(label):
        location_value = (
            _profile_city(application_profile.location)
            if "city only" in normalize_text(label)
            else application_profile.location
        )
        if location_value:
            return {
                "field_name": field_name,
                "label": label,
                "kind": field["kind"],
                "required": field["required"],
                "path": path,
                "field_type": field_type,
                "value": location_value,
                "source": "application_profile.md",
            }

    return None


def _infer_step(
    field: dict,
    *,
    out_dir: Path,
    profile,
    application_profile,
    generated_answers: dict[str, object],
    master_resume_text: str,
    company_name: str,
    job_url: str,
) -> dict | None:
    standard_step = _standard_step(
        field,
        out_dir=out_dir,
        profile=profile,
        application_profile=application_profile,
        master_resume_text=master_resume_text,
    )
    if standard_step is not None:
        return standard_step

    direct_answer = _infer_deterministic(
        field,
        application_profile=application_profile,
        master_resume_text=master_resume_text,
        company_name=company_name,
        job_url=job_url,
    )
    if direct_answer is not None:
        value, source = direct_answer
        return {
            "field_name": field["field_name"],
            "label": field["label"],
            "kind": field["kind"],
            "required": field["required"],
            "path": str(field.get("path") or "").strip(),
            "field_type": str(field.get("type") or "").strip(),
            "value": value,
            "source": source,
        }

    if field["field_name"] in generated_answers:
        value = generated_answers[field["field_name"]]
        if field["kind"] == "checkbox":
            joined = ", ".join(str(item).strip() for item in value if str(item).strip()) if isinstance(value, list) else str(value)
            if not joined.strip():
                return None
            return {
                "field_name": field["field_name"],
                "label": field["label"],
                "kind": field["kind"],
                "required": field["required"],
                "path": str(field.get("path") or "").strip(),
                "field_type": str(field.get("type") or "").strip(),
                "value": joined,
                "report_value": joined,
                "source": "generated_application_answer",
            }
        text_value = str(value or "").strip()
        if not text_value:
            return None
        return {
            "field_name": field["field_name"],
            "label": field["label"],
            "kind": field["kind"],
            "required": field["required"],
            "path": str(field.get("path") or "").strip(),
            "field_type": str(field.get("type") or "").strip(),
            "value": text_value,
            "source": "generated_application_answer",
        }

    return None


def _build_payload(out_dir: Path, provider: str) -> dict:
    """Build the autofill payload for a Rippling application."""
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)
    job_url = str(meta.get("jd_source_resolved") or meta.get("jd_source") or "")
    master_resume_text = (PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8")
    profile = parse_master_resume(master_resume_text)
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))

    # Derive application URL (add /apply if needed)
    application_url = job_url
    if job_url and not job_url.rstrip("/").endswith("/apply"):
        application_url = append_url_path_suffix(job_url, "/apply")
    page_html_path = role_submit_path(out_dir, APPLICATION_PAGE_HTML)
    html = _fetch_application_html(application_url, cache_path=page_html_path)
    next_data = _extract_next_data(html)
    fields = _rippling_fields(next_data)

    pending_user_input = _pending_user_input_fields(fields, application_profile=application_profile)
    if pending_user_input:
        pending_path = write_pending_user_input(
            out_dir,
            board=_BOARD,
            questions=pending_user_input,
        )
        labels = ", ".join(question["label"] for question in pending_user_input)
        raise ValueError(f"Rippling submit requires explicit user input before submission for: {labels}. See {pending_path}")
    clear_pending_user_input(out_dir)

    generated_candidates = [
        field
        for field in fields
        if _field_supports_generated_answer(field)
        and _infer_step(
            field,
            out_dir=out_dir,
            profile=profile,
            application_profile=application_profile,
            generated_answers={},
            master_resume_text=master_resume_text,
            company_name=str(meta.get("company_proper") or meta.get("company") or ""),
            job_url=job_url,
        )
        is None
    ]
    generated_answers: dict[str, object] = {}
    if generated_candidates:
        try:
            generated_answers = generate_application_answers(
                out_dir=out_dir,
                meta=meta,
                question_specs=_question_specs(fields, {field["field_name"] for field in generated_candidates}),
                provider=provider,
            )
        except GeneratedAnswerBlockersError as exc:
            pending_path = write_pending_user_input_for_unconfirmed_fields(
                out_dir,
                board=_BOARD,
                fields=exc.blockers,
            )
            labels = ", ".join(
                str(blocker.get("label") or blocker.get("field_name") or "").strip() for blocker in exc.blockers
            )
            raise ValueError(
                f"Rippling submit requires review for generated-answer regressions: {labels}. See {pending_path}"
            ) from exc

    steps: list[dict] = []
    unknown_questions: list[dict] = []
    for field in fields:
        step = _infer_step(
            field,
            out_dir=out_dir,
            profile=profile,
            application_profile=application_profile,
            generated_answers=generated_answers,
            master_resume_text=master_resume_text,
            company_name=str(meta.get("company_proper") or meta.get("company") or ""),
            job_url=job_url,
        )
        if step is not None:
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
            }
        )

    _write_unknown_questions(out_dir, unknown_questions)
    missing_required = [question for question in unknown_questions if question["required"]]
    if missing_required:
        labels = ", ".join(question["label"] for question in missing_required)
        raise ValueError(
            f"Autofill payload is missing answers for required Rippling fields: {labels}. "
            f"See {role_submit_path(out_dir, AUTOFILL_UNKNOWN_QUESTIONS_JSON)}"
        )

    payload = {
        "job_url": job_url,
        "application_url": application_url,
        "out_dir": str(out_dir),
        "job_title": str(meta.get("jd_title") or ""),
        "company": str(meta.get("company_proper") or meta.get("company") or ""),
        "candidate_email": profile.email,
        "verification_code_email": application_profile.verification_code_email or profile.email,
        "mode": "review-before-submit",
        "notes": [
            "Rippling single-page form. The payload builder inspects the live __NEXT_DATA__ application schema.",
            "Deterministic profile-backed answers come from application_profile.md and master_resume.md.",
            "Any remaining open-ended or choice questions route through generated_application_answers.",
            "EEO fields (gender, race, veteran, disability) are optional and omitted when the live schema does not require them.",
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
        "fields": fields,
        "steps": steps,
        "unknown_questions": unknown_questions,
    }
    return payload


# --- Browser pipeline callbacks ---


def _dismiss_cookie_banner(page) -> None:
    """Dismiss cookie consent banner if present."""
    try:
        for name in ("Accept", "Accept all", "Accept All", "Accept cookies", "Got it"):
            btn = page.get_by_role("button", name=name)
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                page.wait_for_timeout(500)
                return
    except Exception:
        pass


def _navigate_to_apply_form(page, payload: dict) -> None:
    """If on a job listing page, click 'Apply now' to go to the form."""
    from urllib.parse import urlparse

    current_url = page.url
    parsed = urlparse(current_url)
    path = parsed.path.rstrip("/")

    # Already on the apply form
    if path.endswith("/apply"):
        return

    # Click "Apply now" button
    try:
        apply_btn = page.get_by_role("button", name=re.compile(r"apply", re.I)).first
        if apply_btn.count() and apply_btn.is_visible():
            apply_btn.click()
            page.wait_for_timeout(3000)
            payload["application_url"] = page.url
            return
    except Exception:
        pass

    # Try link variant
    try:
        apply_link = page.get_by_role("link", name=re.compile(r"apply", re.I)).first
        if apply_link.count() and apply_link.is_visible():
            apply_link.click()
            page.wait_for_timeout(3000)
            payload["application_url"] = page.url
            return
    except Exception:
        pass


def _fill_text_field(page, label: str, value: str) -> bool:
    """Fill a text input or textarea by label. Returns True if successful."""
    if not value:
        return False
    try:
        locator = page.get_by_role("textbox", name=re.compile(re.escape(label), re.I)).first
        if locator.count():
            locator.scroll_into_view_if_needed()
            locator.fill(str(value))
            return True
    except Exception:
        pass

    try:
        locator = page.get_by_label(re.compile(re.escape(label), re.I)).first
        if locator.count():
            locator.scroll_into_view_if_needed()
            locator.fill(str(value))
            return True
    except Exception:
        pass

    return False


def _fill_file_field(page, label: str, file_path: str) -> bool:
    """Upload a file to an input[type=file]. Returns True if successful."""
    if not file_path or not Path(file_path).exists():
        return False

    try:
        file_input = page.get_by_label(re.compile(re.escape(label), re.I)).first
        if file_input.count():
            file_input.set_input_files(file_path)
            return True
    except Exception:
        pass

    try:
        # Rippling uses a drop zone with a hidden file input
        file_inputs = page.locator("input[type='file']")
        count = file_inputs.count()
        if count > 0:
            label_lower = label.casefold()
            if "resum" in label_lower or "résumé" in label_lower or "cv" in label_lower:
                file_inputs.first.set_input_files(file_path)
                return True
    except Exception:
        pass

    return False


def _fill_select_field(page, label: str, value: str) -> bool:
    """Fill a select/dropdown field. Returns True if successful."""
    if not value:
        return False
    from playwright.sync_api import Error as PlaywrightError

    try:
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
            return False
    except PlaywrightError:
        pass

    try:
        sel_by_label = page.get_by_label(re.compile(re.escape(label), re.I)).first
        if sel_by_label.count():
            sel_by_label.select_option(label=value)
            return True
    except Exception:
        pass

    return False


def _fill_radio_field(page, label: str, value: str) -> bool:
    """Select a radio button by label and value. Returns True if successful."""
    try:
        radio = page.get_by_role("radio", name=re.compile(re.escape(value), re.I)).first
        if radio.count():
            radio.scroll_into_view_if_needed()
            radio.click()
            page.wait_for_timeout(200)
            return True
    except Exception:
        pass
    return False


def _css_attr_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _rippling_step_path(step: dict) -> str:
    return str(step.get("path") or "").strip()


def _rippling_locator_candidates(step: dict) -> list[str]:
    path = _rippling_step_path(step)
    if not path:
        return []
    escaped_path = _css_attr_value(path)
    kind = str(step.get("kind") or "").strip().casefold()
    if kind in {"text", "textarea"}:
        return [
            f'[data-testid="{escaped_path}"] textarea',
            f'[data-testid="{escaped_path}"] input',
            f'[data-input*="{escaped_path}"]',
            f'[data-testid*="{escaped_path}"] textarea',
            f'[data-testid*="{escaped_path}"] input',
        ]
    if kind == "select":
        return [
            f'[data-testid="{escaped_path}"] [role="combobox"]',
            f'[data-testid*="{escaped_path}"] [role="combobox"]',
        ]
    return []


def _find_rippling_locator(page, step: dict):
    for selector in _rippling_locator_candidates(step):
        try:
            locator = page.locator(selector).first
            if locator.count():
                return locator
        except Exception:
            continue
    return None


def _normalized_visible_value(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalized_location_value(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _is_rippling_location_step(step: dict) -> bool:
    path = _rippling_step_path(step)
    if path == "location":
        return True
    return "location" in normalize_text(str(step.get("label") or ""))


def _rippling_locator_value(locator) -> str:
    for method_name in ("input_value", "inner_text"):
        try:
            value = getattr(locator, method_name)()
        except Exception:
            continue
        normalized = _normalized_visible_value(value)
        if normalized:
            return normalized
    return ""


def _location_value_matches(actual: str, *expected_candidates: str) -> bool:
    actual_key = _normalized_location_value(actual)
    if not actual_key:
        return False
    for candidate in expected_candidates:
        candidate_key = _normalized_location_value(candidate)
        if not candidate_key:
            continue
        if actual_key == candidate_key or actual_key.startswith(f"{candidate_key} "):
            return True
    return False


def _location_option_score(option_text: str, *, expected_value: str, query_value: str) -> int:
    option_key = _normalized_location_value(option_text)
    if not option_key:
        return 0
    expected_key = _normalized_location_value(expected_value)
    query_key = _normalized_location_value(query_value)
    if expected_key and (option_key == expected_key or option_key.startswith(f"{expected_key} ")):
        return 4
    if query_key and (option_key == query_key or option_key.startswith(f"{query_key} ")):
        return 3
    if expected_key and expected_key in option_key:
        return 2
    if query_key and query_key in option_key:
        return 1
    return 0


def _select_rippling_location_option(page, *, expected_value: str, query_value: str) -> str | None:
    options = page.get_by_role("option")
    try:
        option_count = options.count()
    except Exception:
        return None
    best_option = None
    best_text = ""
    best_score = 0
    for index in range(option_count):
        option = options.nth(index)
        try:
            option_text = _normalized_visible_value(option.inner_text())
        except Exception:
            continue
        score = _location_option_score(
            option_text,
            expected_value=expected_value,
            query_value=query_value,
        )
        if score > best_score:
            best_score = score
            best_option = option
            best_text = option_text
    if best_option is None:
        return None
    best_option.click()
    page.wait_for_timeout(150)
    return best_text


def _fill_rippling_text_step(page, step: dict) -> bool:
    locator = _find_rippling_locator(page, step)
    if locator is None:
        return False
    value = str(step.get("value") or "")
    if not value:
        return False
    try:
        locator.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        locator.click()
    except Exception:
        pass
    locator.fill(value)
    page.wait_for_timeout(150)
    actual_value = _rippling_locator_value(locator)
    if _is_rippling_location_step(step):
        selected_value = _select_rippling_location_option(
            page,
            expected_value=value,
            query_value=value,
        )
        actual_value = selected_value or _rippling_locator_value(locator)
        if not _location_value_matches(actual_value, value):
            return False
        if actual_value and actual_value != value:
            step["report_value"] = actual_value
        step["filled"] = True
        step.pop("status", None)
        return True
    if _normalized_visible_value(actual_value) != _normalized_visible_value(value):
        return False
    step["filled"] = True
    step.pop("status", None)
    return True


def _fill_rippling_select_step(page, step: dict) -> bool:
    locator = _find_rippling_locator(page, step)
    if locator is None:
        return False
    value = str(step.get("value") or "")
    if not value:
        return False
    try:
        locator.scroll_into_view_if_needed()
    except Exception:
        pass
    locator.click()
    page.wait_for_timeout(250)
    options = page.get_by_role("option")
    option_values: list[str] = []
    option_locators = []
    try:
        option_count = options.count()
    except Exception:
        option_count = 0
    for index in range(option_count):
        option = options.nth(index)
        try:
            option_text = _normalized_visible_value(option.inner_text())
        except Exception:
            continue
        if not option_text:
            continue
        option_values.append(option_text)
        option_locators.append(option)
    choice = select_option(option_values, value) or value
    for option, option_text in zip(option_locators, option_values, strict=False):
        if option_text != choice:
            continue
        option.click()
        page.wait_for_timeout(150)
        actual_value = _normalized_visible_value(_rippling_locator_value(locator))
        if actual_value != _normalized_visible_value(choice):
            return False
        step["filled"] = True
        step.pop("status", None)
        return True
    return False


def _fill_step(page, step: dict) -> None:
    """Fill a single form field on the Rippling application page."""
    kind = str(step.get("kind") or "").strip().casefold()
    if kind in {"text", "textarea"} and _fill_rippling_text_step(page, step):
        return
    if kind == "select" and _fill_rippling_select_step(page, step):
        return
    fill_basic_step(page, step)


def _page_snapshot_rippling(page) -> dict:
    """Capture a snapshot of the Rippling page state."""
    js = """() => {
        const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
        const pageText = normalize(document.body ? document.body.innerText : '');

        const formSelector = 'form';
        const invalidFields = Array.from(document.querySelectorAll('[aria-invalid="true"]'))
            .map((node) => {
                const entry = node.closest('form, [class*="field"], [class*="Field"]');
                const label = entry ? entry.querySelector('label') : null;
                return normalize(label ? label.innerText : node.getAttribute('name') || node.getAttribute('id') || '');
            })
            .filter(Boolean);
        const explicitErrors = Array.from(document.querySelectorAll('[role="alert"], [class*="error"], [class*="Error"]'))
            .map((node) => normalize(node.innerText))
            .filter(Boolean);

        return {
            url: window.location.href,
            page_text: pageText,
            form_visible: !!document.querySelector(formSelector),
            invalid_fields: invalidFields,
            errors: explicitErrors,
        };
    }"""

    return page.evaluate(js)


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


def _wait_for_rippling_form(page) -> None:
    """Wait for the Rippling application form to render."""
    page.wait_for_selector(
        'button:has-text("Apply"), button:has-text("Submit"), [class*="application"]',
        timeout=25000,
    )
    page.wait_for_timeout(2000)  # Let React hydrate
    _dismiss_cookie_banner(page)


# --- Entry point ---


def _run_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    """Wrap run_browser_pipeline with Rippling-specific callbacks."""
    import json

    from autofill_pipeline import run_browser_pipeline

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    navigate_url = payload.get("application_url") or payload.get("job_url", "")

    def _post_navigate(page):
        """After initial navigation, navigate from listing to apply form if needed."""
        _navigate_to_apply_form(page, payload)
        if payload.get("application_url") and payload["application_url"] != navigate_url:
            payload_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

    return run_browser_pipeline(
        payload_path,
        headless=headless,
        submit=submit,
        board_name=_BOARD,
        form_ready_fn=_wait_for_rippling_form,
        fill_step_fn=_fill_step,
        page_snapshot_fn=lambda page: _page_snapshot_rippling(page),
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
