#!/usr/bin/env python3
"""Generate and optionally submit a Dover application payload."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_submit_common import (  # noqa: E402
    APPLICATION_PROFILE_PATH,
    MASTER_RESUME_PATH,
    build_onsite_start_location_answer,
    build_truthful_work_authorization_answer,
    clear_pending_user_input,
    find_cover_letter_file,
    find_cover_letter_text,
    find_resume_file,
    format_education_from_profile,
    generate_application_answers,
    json_dumps_pretty,
    load_meta,
    parse_application_profile,
    parse_master_resume,
    pending_user_input_reason_for_spec,
    question_is_minimum_experience_check,
    question_is_salary_comfort_check,
    reply_to_confirmation_email,
    resolve_shared_question_policy,
    slugify_label,
    sync_notion_after_submit,
    write_pending_user_input,
)
from autofill_common import label_matches, select_option, select_shared_policy_option  # noqa: E402
from job_board_urls import dover_job_or_search_id_from_url, looks_like_dover_url  # noqa: E402
from output_layout import migrate_role_output_layout, role_submit_path  # noqa: E402
from project_env import load_project_env  # noqa: E402
from question_classifier import classify_question  # noqa: E402

AUTOFILL_PAYLOAD_JSON = "dover_autofill_payload.json"
APPLICATION_JOB_JSON = "dover_application_job.json"
SUBMISSION_RESPONSE_JSON = "dover_submission_response.json"
UNKNOWN_QUESTIONS_JSON = "dover_unknown_questions.json"
WEBSITE_CONFIRMATION_JSON = "application_confirmation_website.json"
EMAIL_CONFIRMATION_JSON = "application_confirmation_email.json"
NOTION_SYNC_STATUS_JSON = "notion_sync_status.json"
SUBMISSION_URL = "https://app.dover.com/api/v1/inbound/application-portal-inbound-application"


load_project_env()


def _dover_referrer_source(job_url: str) -> str | None:
    query = parse_qs(urlparse(job_url).query)
    for key in ("referrerSource", "rs"):
        value = (query.get(key) or [""])[0].strip()
        if value:
            return value
    return None


def _dover_job_api_url(job_url: str) -> str:
    job_or_search_id = dover_job_or_search_id_from_url(job_url)
    if not job_or_search_id:
        raise ValueError(f"Unsupported Dover application URL: {job_url}")
    return f"https://app.dover.com/api/v1/inbound/application-portal-job/{quote(job_or_search_id, safe='')}"


def _fetch_dover_job(job_url: str) -> dict:
    req = Request(
        _dover_job_api_url(job_url),
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
            )
        },
    )
    with urlopen(req, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    if not isinstance(payload, dict) or not payload.get("id"):
        raise RuntimeError("Dover job API returned an unexpected payload")
    return payload


def _question_field_name(question: dict) -> str:
    question_id = str(question.get("id") or "").strip()
    question_slug = slugify_label(str(question.get("question") or "question"))[:48]
    suffix = slugify_label(question_id)[:8] or "field"
    return f"dover_{question_slug}_{suffix}"


def _is_cover_letter_prompt(text: str) -> bool:
    return label_matches(text, "cover letter")


def _is_cover_letter_upload_question(question: dict) -> bool:
    input_type = str(question.get("input_type") or "").strip().upper()
    question_type = str(question.get("question_type") or "").strip().upper()
    prompt = str(question.get("question") or "")
    return input_type == "FILE_UPLOAD" and ("COVER_LETTER" in question_type or _is_cover_letter_prompt(prompt))


def _infer_yes_no_answer(question_text: str, application_profile) -> bool | None:
    requires_sponsorship = application_profile.require_sponsorship_now or application_profile.require_sponsorship_future
    if label_matches(question_text, "transgender"):
        return str(getattr(application_profile, "transgender_status", "No") or "No").strip().casefold() in {
            "yes",
            "y",
            "true",
        }
    policy = resolve_shared_question_policy(question_text, application_profile)
    if policy is not None and policy.boolean_value is not None:
        return policy.boolean_value
    if label_matches(question_text, "sponsorship", "sponsor", "visa"):
        return requires_sponsorship
    if question_is_salary_comfort_check(question_text):
        return getattr(application_profile, "comfortable_with_posted_salary", True)
    if label_matches(
        question_text,
        "authorized to work",
        "work authorization",
        "right to work",
        "legally authorized",
        "eligible to work",
        "legally eligible",
    ):
        return application_profile.authorized_to_work_unconditionally
    if question_is_minimum_experience_check(question_text):
        return application_profile.minimum_years_experience
    if label_matches(question_text, "relocate", "relocation"):
        return application_profile.willing_to_relocate
    if label_matches(question_text, "on site", "on-site", "onsite", "in person", "in-person", "commute", "commuting"):
        return application_profile.comfortable_working_on_site
    # Free-text location: "What city do you live in?" → return location, not boolean
    qt = question_text.strip().lower()
    if any(qt.startswith(w) for w in ("what ", "which ", "where ")) and label_matches(
        question_text, "live in", "currently located", "based", "reside"
    ):
        return application_profile.location
    if label_matches(question_text, "live in", "currently located", "based in", "reside"):
        return application_profile.lives_in_job_location
    if label_matches(question_text, "text message", "sms"):
        return application_profile.text_message_consent
    return None


_COMPENSATION_DEFLECT = (
    "I'm open and flexible on compensation. I'd prefer to learn more about "
    "the role's scope and total rewards package before discussing specific numbers. "
    "I'm confident we can find a mutually agreeable arrangement."
)


def _answer_from_classifier(prompt: str, application_profile, *, out_dir: Path) -> str | None:
    """Use the unified question classifier to produce a deterministic answer.

    Returns an answer string if the classifier identifies the question category
    and a deterministic answer can be produced, otherwise None.
    """
    category = classify_question(prompt)
    if category is None:
        return None

    policy = resolve_shared_question_policy(prompt, application_profile)
    if policy is not None and policy.category != "work_authorization" and policy.text_value is not None:
        return policy.text_value

    if category == "education":
        return format_education_from_profile(application_profile)
    if category == "compensation":
        return _COMPENSATION_DEFLECT
    if category == "nda_noncompete":
        return "No"
    if category == "work_authorization":
        return build_truthful_work_authorization_answer(prompt, application_profile) or (
            application_profile.work_authorization_statement or None
        )
    if category == "city_location":
        return application_profile.location or None
    if category == "current_company":
        return getattr(application_profile, "current_company", None) or None
    if category == "culture_careers_optin":
        return "No"
    if category == "product_usage":
        return "Yes"
    if category == "interview_accommodation":
        return "No"
    if category == "reasonable_accommodation":
        return "Yes"
    # Categories that are primarily yes/no (salary_comfort, minimum_experience,
    # experience_confirmation, office_attendance, company_engagement) are handled
    # by _infer_yes_no_answer for MULTIPLE_CHOICE or build_onsite_start_location_answer
    # for composite text fields — return None to let the existing handlers decide.
    return None


def _infer_custom_answer(question: dict, application_profile, *, out_dir: Path) -> str | None:
    input_type = str(question.get("input_type") or "").strip().upper()
    options = question.get("multiple_choice_options")
    prompt = str(question.get("question") or "")
    policy = resolve_shared_question_policy(prompt, application_profile)

    if input_type == "MULTIPLE_CHOICE":
        if policy is not None and policy.text_value is not None:
            matched = select_shared_policy_option(options, policy, application_profile=application_profile)
            if matched is not None:
                return matched
        inferred = _infer_yes_no_answer(prompt, application_profile)
        if inferred is not None:
            return select_option(options, "Yes" if inferred else "No")
    if input_type in {"SHORT_ANSWER", "LONG_ANSWER", "STRING"}:
        onsite_answer = build_onsite_start_location_answer(prompt, application_profile)
        if onsite_answer:
            return onsite_answer
    if input_type in {"SHORT_ANSWER", "LONG_ANSWER", "STRING"}:
        classified_answer = _answer_from_classifier(prompt, application_profile, out_dir=out_dir)
        if classified_answer:
            return classified_answer
    if input_type in {"SHORT_ANSWER", "LONG_ANSWER", "STRING"} and _is_cover_letter_prompt(prompt):
        return find_cover_letter_text(out_dir)
    if input_type in {"SHORT_ANSWER", "LONG_ANSWER", "STRING"} and label_matches(prompt, "github"):
        return application_profile.github or None
    return None


def _pending_user_input_questions(questions: list[dict]) -> list[dict]:
    pending: list[dict] = []
    for question in questions:
        prompt = str(question.get("question") or "").strip()
        reason = pending_user_input_reason_for_spec(question)
        if not reason:
            continue
        pending.append(
            {
                "id": str(question.get("id") or "").strip(),
                "field_name": _question_field_name(question),
                "question": prompt,
                "input_type": str(question.get("input_type") or "").strip(),
                "required": bool(question.get("required")),
                "reason": reason,
            }
        )
    return pending


def _question_specs(
    custom_questions: list[dict], application_profile, *, out_dir: Path
) -> tuple[list[dict], dict[str, str]]:
    specs: list[dict] = []
    inferred_answers: dict[str, str] = {}

    for question in custom_questions:
        field_name = _question_field_name(question)
        inferred = _infer_custom_answer(question, application_profile, out_dir=out_dir)
        if inferred:
            inferred_answers[field_name] = inferred
            continue
        # Fallback: if the classifier recognizes the question but
        # _infer_custom_answer didn't produce a concrete value (e.g. profile
        # data was missing), still try to produce an answer so we avoid
        # sending a deterministic question to the LLM.
        prompt = str(question.get("question") or "").strip()
        classified_answer = _answer_from_classifier(prompt, application_profile, out_dir=out_dir)
        if classified_answer:
            inferred_answers[field_name] = classified_answer
            continue
        specs.append(
            {
                "field_name": field_name,
                "label": prompt,
                "description": "",
                "required": bool(question.get("required")),
                "type": str(question.get("input_type") or "").strip() or "String",
                "options": list(question.get("multiple_choice_options") or []),
            }
        )

    return specs, inferred_answers


def _unknown_question_payload(question: dict, *, field_name: str) -> dict:
    return {
        "id": str(question.get("id") or "").strip(),
        "field_name": field_name,
        "question": str(question.get("question") or "").strip(),
        "input_type": str(question.get("input_type") or "").strip(),
        "required": bool(question.get("required")),
        "options": list(question.get("multiple_choice_options") or []),
    }


def _build_payload(out_dir: Path, provider: str) -> dict:
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)
    job_url = str(meta.get("jd_source_resolved") or meta.get("jd_source") or "")
    if not looks_like_dover_url(job_url):
        raise ValueError(f"Output directory does not point at a Dover apply URL: {job_url}")

    job_payload = _fetch_dover_job(job_url)
    role_submit_path(out_dir, APPLICATION_JOB_JSON).write_text(
        json_dumps_pretty(job_payload) + "\n",
        encoding="utf-8",
    )

    questions = [
        question
        for question in (job_payload.get("application_questions") or [])
        if isinstance(question, dict) and not question.get("hidden")
    ]
    custom_questions = [
        question
        for question in questions
        if str(question.get("question_type") or "").strip().upper() == "CUSTOM"
        and not _is_cover_letter_upload_question(question)
    ]
    pending_user_input = _pending_user_input_questions(custom_questions)
    if pending_user_input:
        pending_path = write_pending_user_input(
            out_dir,
            board="dover",
            questions=pending_user_input,
        )
        labels = ", ".join(question["question"] for question in pending_user_input)
        raise ValueError(
            f"Dover submit requires explicit user input before submission for: {labels}. See {pending_path}"
        )
    clear_pending_user_input(out_dir)
    profile = parse_master_resume(MASTER_RESUME_PATH.read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    resume_path = find_resume_file(out_dir)
    cover_letter_question = next(
        (question for question in questions if _is_cover_letter_upload_question(question)), None
    )
    linkedin_question = next(
        (
            question
            for question in questions
            if str(question.get("question_type") or "").strip().upper() == "LINKEDIN_URL"
        ),
        None,
    )
    phone_question = next(
        (
            question
            for question in questions
            if str(question.get("question_type") or "").strip().upper() == "PHONE_NUMBER"
        ),
        None,
    )
    resume_question = next(
        (question for question in questions if str(question.get("question_type") or "").strip().upper() == "RESUME"),
        None,
    )
    cover_letter_path = find_cover_letter_file(out_dir) if cover_letter_question else None

    question_specs, inferred_answers = _question_specs(custom_questions, application_profile, out_dir=out_dir)
    generated_answers = generate_application_answers(
        out_dir=out_dir,
        meta=meta,
        question_specs=question_specs,
        provider=provider,
    )

    custom_answers: list[dict] = []
    answer_records: list[dict] = []
    unknown_required: list[dict] = []
    unknown_optional: list[dict] = []

    for question in custom_questions:
        field_name = _question_field_name(question)
        answer = inferred_answers.get(field_name)
        if answer is None:
            answer = generated_answers.get(field_name)
        input_type = str(question.get("input_type") or "").strip().upper()
        if input_type == "MULTIPLE_CHOICE":
            answer = select_option(question.get("multiple_choice_options"), answer)
        if isinstance(answer, str):
            answer = answer.strip()

        record = {
            "id": str(question.get("id") or "").strip(),
            "field_name": field_name,
            "question": str(question.get("question") or "").strip(),
            "input_type": input_type,
            "required": bool(question.get("required")),
            "answer": answer or "",
            "source": (
                "inferred_application_profile" if field_name in inferred_answers else "generated_application_answer"
            ),
        }
        answer_records.append(record)

        if answer:
            custom_answers.append(
                {
                    "id": record["id"],
                    "question": record["question"],
                    "answer": answer,
                }
            )
        elif record["required"]:
            unknown_required.append(_unknown_question_payload(question, field_name=field_name))
        else:
            unknown_optional.append(_unknown_question_payload(question, field_name=field_name))

    unknown_payload = {
        "required": unknown_required,
        "optional": unknown_optional,
    }
    role_submit_path(out_dir, UNKNOWN_QUESTIONS_JSON).write_text(
        json_dumps_pretty(unknown_payload) + "\n",
        encoding="utf-8",
    )
    if unknown_required:
        raise ValueError(
            "Could not confidently answer required Dover custom questions. "
            f"See {role_submit_path(out_dir, UNKNOWN_QUESTIONS_JSON)}"
        )

    linkedin_url = application_profile.linkedin or profile.linkedin or ""
    linkedin_required = bool(
        linkedin_question
        and (bool(linkedin_question.get("required")) or bool(job_payload.get("require_linkedin_profile_url")))
    )
    if linkedin_required and not linkedin_url:
        raise ValueError("Dover application requires a LinkedIn URL, but none is available.")

    phone_number = profile.phone if phone_question and profile.phone else ""
    if phone_question and bool(phone_question.get("required")) and not phone_number:
        raise ValueError("Dover application requires a phone number, but none is available.")

    if resume_question and bool(resume_question.get("required")) and not resume_path.exists():
        raise FileNotFoundError(f"Dover application requires a resume, but {resume_path} was not found.")
    if (
        cover_letter_question
        and bool(cover_letter_question.get("required"))
        and (cover_letter_path is None or not cover_letter_path.exists())
    ):
        raise FileNotFoundError(f"Dover application requires a cover letter, but {cover_letter_path} was not found.")

    request_payload = {
        "job_id": str(job_payload.get("id") or "").strip(),
        "first_name": profile.first_name,
        "last_name": profile.last_name,
        "email": profile.email,
        "linkedin_url": linkedin_url or None,
        "phone_number": phone_number or None,
        "resume_path": str(resume_path),
        "cover_letter_path": str(cover_letter_path) if cover_letter_path is not None else None,
        "application_questions": custom_answers,
        "referrer_source": _dover_referrer_source(job_url),
    }

    payload = {
        "board": "dover",
        "provider": provider,
        "out_dir": str(out_dir),
        "job_url": job_url,
        "submit_url": SUBMISSION_URL,
        "job_id": request_payload["job_id"],
        "company": str(job_payload.get("client_name") or meta.get("company_proper") or meta.get("company") or ""),
        "role_title": str(job_payload.get("title") or meta.get("jd_title") or ""),
        "candidate_email": profile.email,
        "verification_code_email": application_profile.verification_code_email or profile.email,
        "request": request_payload,
        "question_answers": answer_records,
        "unknown_questions": unknown_payload,
    }
    return payload


def _multipart_body(fields: dict[str, object], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = f"----jobassets{uuid.uuid4().hex}"
    body = bytearray()

    def append_line(line: bytes) -> None:
        body.extend(line)
        body.extend(b"\r\n")

    for name, value in fields.items():
        if value is None:
            continue
        append_line(f"--{boundary}".encode())
        append_line(f'Content-Disposition: form-data; name="{name}"'.encode())
        append_line(b"")
        append_line(str(value).encode("utf-8"))

    for name, (filename, content, content_type) in files.items():
        append_line(f"--{boundary}".encode())
        append_line(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode())
        append_line(f"Content-Type: {content_type}".encode())
        append_line(b"")
        body.extend(content)
        body.extend(b"\r\n")

    append_line(f"--{boundary}--".encode())
    return bytes(body), boundary


def _parse_error_message(raw_text: str) -> str:
    text = (raw_text or "").strip()
    if not text:
        return "Dover submit failed with no error details."
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, str) and item.strip():
                return item.strip()
    if isinstance(payload, dict):
        for key in ("message", "detail", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        errors = payload.get("errors")
        if isinstance(errors, list):
            for item in errors:
                if isinstance(item, str) and item.strip():
                    return item.strip()
    lower = text.casefold()
    if "cloudflare" in lower or "security verification" in lower:
        return "Dover submit request was blocked by Cloudflare security verification."
    if "server error" in lower:
        return "Dover returned a server error while processing the application."
    return text[:500]


def _response_artifact(status_code: int, raw_text: str) -> dict:
    artifact: dict[str, object] = {
        "status_code": status_code,
        "raw_excerpt": (raw_text or "")[:2000],
    }
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        artifact["json"] = parsed
    return artifact


def _submit_payload(payload: dict) -> dict:
    request_payload = dict(payload["request"])
    resume_path = Path(str(request_payload.pop("resume_path")))
    cover_letter_path_value = request_payload.pop("cover_letter_path", None)
    cover_letter_path = Path(str(cover_letter_path_value)) if cover_letter_path_value else None
    if not resume_path.exists():
        raise FileNotFoundError(f"Resume file not found: {resume_path}")
    if cover_letter_path is not None and not cover_letter_path.exists():
        raise FileNotFoundError(f"Cover letter file not found: {cover_letter_path}")

    fields = {
        "job_id": request_payload["job_id"],
        "first_name": request_payload["first_name"],
        "last_name": request_payload["last_name"],
        "email": request_payload["email"],
        "referrer_source": request_payload.get("referrer_source"),
        "linkedin_url": request_payload.get("linkedin_url"),
        "phone_number": request_payload.get("phone_number"),
        "application_questions": json.dumps(request_payload.get("application_questions") or []),
    }
    file_bytes = resume_path.read_bytes()
    files = {
        "resume": (
            resume_path.name,
            file_bytes,
            "application/pdf" if resume_path.suffix.casefold() == ".pdf" else "application/octet-stream",
        )
    }
    if cover_letter_path is not None:
        cover_letter_bytes = cover_letter_path.read_bytes()
        files["cover_letter"] = (
            cover_letter_path.name,
            cover_letter_bytes,
            "application/pdf" if cover_letter_path.suffix.casefold() == ".pdf" else "application/octet-stream",
        )
    body, boundary = _multipart_body(fields, files)
    request = Request(
        payload["submit_url"],
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://app.dover.com",
            "Referer": payload["job_url"],
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
            ),
        },
    )

    try:
        with urlopen(request, timeout=60) as response:
            raw_text = response.read().decode("utf-8", errors="replace")
            return {
                "status_code": int(getattr(response, "status", 200)),
                "raw_text": raw_text,
                "error": None,
            }
    except HTTPError as exc:
        raw_text = exc.read().decode("utf-8", errors="replace")
        return {
            "status_code": exc.code,
            "raw_text": raw_text,
            "error": _parse_error_message(raw_text),
        }
    except URLError as exc:
        raise RuntimeError(f"Dover submit request failed: {exc}") from exc


def _run_submit(payload_path: Path, headless: bool, submit: bool) -> int:
    """API-based submission — headless/browser flags are ignored."""
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    out_dir = Path(payload["out_dir"])

    if not submit:
        return 0

    submitted_at_utc = datetime.now(UTC).replace(microsecond=0).isoformat()
    response = _submit_payload(payload)
    response_artifact = _response_artifact(response["status_code"], response["raw_text"])
    role_submit_path(out_dir, SUBMISSION_RESPONSE_JSON).write_text(
        json_dumps_pretty(response_artifact) + "\n",
        encoding="utf-8",
    )

    error = response.get("error")
    status_code = int(response["status_code"])
    if error or status_code < 200 or status_code >= 300:
        raise RuntimeError(
            f"Dover submit failed (HTTP {status_code}). {error or 'No error details provided.'} "
            f"See {role_submit_path(out_dir, SUBMISSION_RESPONSE_JSON)}"
        )

    outcome = {
        "status": "confirmed",
        "reason": "api",
        "snapshot": {"url": payload["job_url"], "page_text": "Dover API accepted the application payload."},
        "errors": [],
        "invalid_fields": [],
    }
    sync_result = sync_notion_after_submit(payload, outcome, provider="dover", min_received_at_utc=submitted_at_utc)
    reply_to_confirmation_email(payload, board_name="dover")
    print("Dover application submitted successfully.")
    status = str(sync_result.get("status") or "")
    if status:
        print(f"Notion sync status: {status}")
    if status == "pending_email_confirmation":
        print(f"Waiting for the confirmation email or rerun:\n  job-assets notion-sync {out_dir}")
    return 0


def main() -> int:
    from autofill_pipeline import autofill_main

    return autofill_main(
        board_name="dover",
        build_payload_fn=_build_payload,
        has_browser=True,
        run_browser_fn=_run_submit,
    )


if __name__ == "__main__":
    raise SystemExit(main())
