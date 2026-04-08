#!/usr/bin/env python3
"""Generate and optionally run a Workday application autofill flow.

Workday uses a multi-page authenticated wizard, so this board script uses
``autofill_main`` with a custom ``run_browser_fn`` instead of the shared
``run_browser_pipeline`` (which assumes a single-form model).
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_submit_common import (
    APPLICATION_ANSWER_CACHE,
    APPLICATION_PROFILE_PATH,
    PROJECT_ROOT,
    GeneratedAnswerBlockersError,
    build_email_confirmation_watcher,
    build_how_did_you_hear_candidates,
    build_source_backed_language_proficiency_answer,
    build_truthful_work_authorization_answer,
    find_cover_letter_file,
    find_resume_file,
    format_education_from_profile,
    generate_application_answers,
    load_meta,
    normalize_text,
    parse_application_profile,
    parse_master_resume,
    reply_to_confirmation_email,
    resolve_how_did_you_hear_option_candidates,
    resolve_shared_question_policy,
    slugify_label,
    sync_notion_after_submit,
    write_pending_user_input_for_unconfirmed_fields,
)
from autofill_common import (
    board_file_constants,
    capture_full_page,
    concatenate_images_vertically,
    dedupe_page_screenshot_artifacts,
    page_snapshot,
    select_profile_option,
    select_shared_policy_option,
    wait_for_captcha_resolution,
    write_report,
)
from autofill_pipeline import CAPTCHA_SKIP_EXIT_CODE, autofill_main
from browser_runtime import (
    human_fill,
    launch_chromium_browser,
    submit_browser_profile_dir,
    submit_slow_mo_ms,
    submit_viewport,
)
from job_board_urls import workday_auth_scope
from output_layout import migrate_role_output_layout, role_submit_path
from project_env import load_project_env
from question_classifier import classify_question
from workday_auth import (
    build_workday_auth_result as _build_workday_auth_result,
)
from workday_auth import (
    classify_workday_auth_state as _classify_workday_auth_state,
)
from workday_auth import (
    looks_like_workday_authenticated_non_form as _looks_like_workday_authenticated_non_form,
)
from workday_resume_uploads import (
    _dedupe_workday_uploaded_resume_items,
    _workday_resume_already_uploaded,
)

_BOARD_CONSTANTS = board_file_constants("workday")
AUTOFILL_PAYLOAD_JSON = _BOARD_CONSTANTS["payload_json"]
APPLICATION_PAGE_HTML = _BOARD_CONSTANTS["application_page_html"]

WORKDAY_EMAIL_ENV = "WORKDAY_EMAIL"
WORKDAY_PASSWORD_ENV = "WORKDAY_PASSWORD"

SUBMIT_CONFIRM_PATTERNS = (
    re.compile(r"\bthank(?:s| you)\s+for\s+applying\b", re.I),
    re.compile(r"\bapplication (?:has been )?(?:received|submitted)\b", re.I),
    re.compile(r"\byour application has been submitted\b", re.I),
    re.compile(r"\bsubmitted successfully\b", re.I),
    re.compile(r"\bsuccessfully submitted\b", re.I),
    re.compile(r"\bwe(?:'|')ll be in touch\b", re.I),
    re.compile(r"\bwe(?:'|')ve received your application\b", re.I),
)
VALIDATION_ERROR_PATTERNS = (
    re.compile(r"\bthis field is required\b", re.I),
    re.compile(r"\bthe field .* is required\b", re.I),
    re.compile(r"\bmust have a value\b", re.I),
    re.compile(r"\bplease (?:enter|select|complete)\b", re.I),
    re.compile(r"^(?:error:\s*)?(?:enter|select|complete)\b", re.I),
    re.compile(r"\binvalid\b", re.I),
)
PREFERRED_CAPTURE_SELECTORS = ("[data-automation-id='applicationContainer']", "main", ".mainContent")
WORKDAY_AUTH_RESULT_JSON = "workday_auth_failure.json"
WORKDAY_AUTH_RECOVERY_STEPS = ("sign_in", "password_reset", "create_account")

_WORKDAY_PRIMARY_SOURCE_PREFERENCES = (
    "Company Website",
    "Corporate Website",
    "Career Site",
    "Website",
    "Blog",
    "Company Blog",
    "LinkedIn",
    "Job Boards",
    "Job Board",
    "Internet",
    "Social Media",
    "Social",
    "Other",
)
_WORKDAY_SUBSOURCE_PREFERENCES = (
    "LinkedIn",
    "Job Boards",
    "Job Board",
    "Indeed",
    "Built In",
    "Social Media",
    "Other",
)
_WORKDAY_RESUME_SECTION_HEADINGS = ("EXPERIENCE", "WORK EXPERIENCE", "PROFESSIONAL EXPERIENCE")
_WORKDAY_RESUME_SECTION_END_HEADINGS = ("EDUCATION", "SKILLS & ADDITIONAL", "SKILLS")
_WORKDAY_WORK_EXPERIENCE_ADD_BUTTON_SELECTORS = (
    "div[role='group'][aria-labelledby='Work-Experience-section'] button[data-automation-id='add-button']",
    "div[role='group'][aria-labelledby='Work-Experience-section'] button:has-text('Add Another')",
    "div[role='group'][aria-labelledby='Work-Experience-section'] button:has-text('Add')",
)
_WORKDAY_EDUCATION_ADD_BUTTON_SELECTORS = (
    "div[role='group'][aria-labelledby='Education-section'] button[data-automation-id='add-button']",
    "div[role='group'][aria-labelledby='Education-section'] button:has-text('Add Another')",
    "div[role='group'][aria-labelledby='Education-section'] button:has-text('Add')",
)
_WORKDAY_SKILLS_LABEL_CANDIDATES = ("Type to Add Skills", "Skills")
_WORKDAY_SKILL_CANDIDATE_LIMIT = 12
_WORKDAY_PROMPT_OPTION_SELECTORS = (
    "[data-automation-id='menuItem']",
    "[role='option']",
    "li[role='option']",
    "[role='listbox'] li",
    ".css-option",
    "[data-automation-id='promptOption']",
)
_WORKDAY_MONTH_NAMES = (
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
_WORKDAY_MONTH_ABBREV_TO_NUMBER: dict[str, int] = {}
for _month_index, _month_name in enumerate(_WORKDAY_MONTH_NAMES, start=1):
    _WORKDAY_MONTH_ABBREV_TO_NUMBER[_month_name.casefold()] = _month_index
    _WORKDAY_MONTH_ABBREV_TO_NUMBER[_month_name[:3].casefold()] = _month_index


load_project_env()


# ─── Gmail / gws helpers ────────────────────────────────────────────────────


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
            f"gws CLI failed. Make sure `gws auth login` is complete. Command: gws {' '.join(args)}. Details: {detail}"
        )
    output = (completed.stdout or "").strip()
    if not output:
        raise RuntimeError(f"gws CLI returned empty output for: gws {' '.join(args)}")
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gws CLI returned non-JSON output: {output[:400]}") from exc


def _is_gws_auth_failure(exc: Exception) -> bool:
    detail = str(exc).casefold()
    return any(
        token in detail
        for token in (
            "autherror",
            "invalid_grant",
            "token has been expired or revoked",
            "gws auth login",
        )
    )


def _gmail_after_query_term(min_received_at: datetime | None) -> str:
    if not min_received_at:
        return "newer_than:1h"
    epoch = int(min_received_at.timestamp())
    return f"after:{epoch}"


def _iter_gmail_parts(part: dict):
    yield part
    for child in part.get("parts", []) or []:
        yield from _iter_gmail_parts(child)


def _decode_gmail_body_data(data: str | None) -> str:
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(f"{data}{padding}").decode("utf-8", errors="replace")
    except Exception:
        return ""


def _collapse_email_body_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _workday_verification_link_sort_key(link: str, *, preferred_job_url: str = "") -> tuple[int, int]:
    lowered = link.casefold()
    parsed = urlparse(link)
    path = parsed.path.casefold()
    host = parsed.netloc.casefold()
    redirect_target = ""
    redirect_values = parse_qs(parsed.query).get("redirect", [])
    if redirect_values:
        redirect_target = unquote(redirect_values[0]).casefold()

    preferred_host = ""
    preferred_path = ""
    if preferred_job_url:
        preferred = urlparse(preferred_job_url)
        preferred_host = preferred.netloc.casefold()
        preferred_path = preferred.path.casefold().rstrip("/")

    score = 0
    if any(token in lowered for token in ("verify", "verification", "confirm", "activate")):
        score += 4
    if "/activate/" in path:
        score += 6
    if any(token in lowered for token in ("myworkdayjobs.com", "myworkdaysite.com")):
        score += 2
    if parsed.query:
        score += 2
    if redirect_target:
        score += 3
    if "/apply/" in redirect_target:
        score += 4
    if preferred_host and host == preferred_host:
        score += 5
    if preferred_path and redirect_target:
        if redirect_target.rstrip("/") == preferred_path:
            score += 7
        elif redirect_target.rstrip("/").startswith(preferred_path):
            score += 6
        elif preferred_path in redirect_target:
            score += 5

    return score, len(link)


def _fetch_workday_email_link(
    query_fragments: list[str],
    link_pattern: re.Pattern,
    *,
    min_received_at_utc: datetime | None = None,
    wait_seconds: int = 120,
) -> str | None:
    """Poll Gmail via gws for a Workday email containing a matching link."""
    after_term = _gmail_after_query_term(min_received_at_utc)
    query = " ".join([after_term, *query_fragments])
    deadline = time.monotonic() + max(wait_seconds, 0)

    while True:
        try:
            list_response = _run_gws_json(
                [
                    "gmail",
                    "users",
                    "messages",
                    "list",
                    "--params",
                    json.dumps({"userId": "me", "maxResults": 10, "q": query}),
                ]
            )
        except RuntimeError as exc:
            if _is_gws_auth_failure(exc):
                raise
            list_response = {}

        for message_stub in list_response.get("messages", []):
            msg_id = message_stub.get("id")
            if not msg_id:
                continue
            try:
                message = _run_gws_json(
                    [
                        "gmail",
                        "users",
                        "messages",
                        "get",
                        "--params",
                        json.dumps({"userId": "me", "id": msg_id, "format": "full"}),
                    ]
                )
            except RuntimeError as exc:
                if _is_gws_auth_failure(exc):
                    raise
                continue
            body = _extract_email_body(message)
            match = link_pattern.search(body)
            if match:
                return match.group(0)

        if time.monotonic() >= deadline:
            return None
        time.sleep(10)


def _fetch_workday_verification_code(
    *,
    min_received_at_utc: datetime | None = None,
    wait_seconds: int = 120,
) -> str | None:
    """Poll Gmail for a Workday verification code."""
    after_term = _gmail_after_query_term(min_received_at_utc)
    query = f"{after_term} workday verification"
    deadline = time.monotonic() + max(wait_seconds, 0)

    while True:
        try:
            list_response = _run_gws_json(
                [
                    "gmail",
                    "users",
                    "messages",
                    "list",
                    "--params",
                    json.dumps({"userId": "me", "maxResults": 10, "q": query}),
                ]
            )
        except RuntimeError as exc:
            if _is_gws_auth_failure(exc):
                raise
            list_response = {}

        for message_stub in list_response.get("messages", []):
            msg_id = message_stub.get("id")
            if not msg_id:
                continue
            try:
                message = _run_gws_json(
                    [
                        "gmail",
                        "users",
                        "messages",
                        "get",
                        "--params",
                        json.dumps({"userId": "me", "id": msg_id, "format": "full"}),
                    ]
                )
            except RuntimeError as exc:
                if _is_gws_auth_failure(exc):
                    raise
                continue
            body = _extract_email_body(message)
            code_match = re.search(r"\b(\d{6})\b", body)
            if code_match:
                return code_match.group(1)

        if time.monotonic() >= deadline:
            return None
        time.sleep(10)


def _select_workday_account_verification_link(body: str, *, preferred_job_url: str = "") -> str | None:
    candidates: list[tuple[tuple[int, int], str]] = []
    for raw_link in re.findall(r"https://[^\s\"'<>]+", body, re.I):
        link = raw_link.rstrip(").,>\"'")
        lowered = link.casefold()
        if not any(host in lowered for host in ("myworkdayjobs.com", "myworkdaysite.com", "workday.com")):
            continue
        if any(
            blocked in lowered
            for blocked in (
                "passwordreset",
                "privacy",
                "candidateprivacy",
                "linkedin.com",
                "facebook.com",
                "instagram.com",
                "twitter.com",
                "youtube.com",
                "unsubscribe",
            )
        ):
            continue
        candidates.append((_workday_verification_link_sort_key(link, preferred_job_url=preferred_job_url), link))

    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _fetch_workday_account_verification_link(
    *,
    min_received_at_utc: datetime | None = None,
    wait_seconds: int = 120,
    preferred_job_url: str = "",
) -> str | None:
    """Poll Gmail for a Workday account-verification link."""
    after_term = _gmail_after_query_term(min_received_at_utc)
    queries = [f"{after_term} from:otp.workday.com"]
    if preferred_job_url:
        queries.append("newer_than:1d from:otp.workday.com")
    deadline = time.monotonic() + max(wait_seconds, 0)

    while True:
        best_link: str | None = None
        best_key: tuple[int, int] | None = None
        seen_message_ids: set[str] = set()
        for query in queries:
            try:
                list_response = _run_gws_json(
                    [
                        "gmail",
                        "users",
                        "messages",
                        "list",
                        "--params",
                        json.dumps({"userId": "me", "maxResults": 10, "q": query}),
                    ]
                )
            except RuntimeError as exc:
                if _is_gws_auth_failure(exc):
                    raise
                list_response = {}

            for message_stub in list_response.get("messages", []):
                msg_id = message_stub.get("id")
                if not msg_id or msg_id in seen_message_ids:
                    continue
                seen_message_ids.add(msg_id)
                try:
                    message = _run_gws_json(
                        [
                            "gmail",
                            "users",
                            "messages",
                            "get",
                            "--params",
                            json.dumps({"userId": "me", "id": msg_id, "format": "full"}),
                        ]
                    )
                except RuntimeError as exc:
                    if _is_gws_auth_failure(exc):
                        raise
                    continue
                verification_link = _select_workday_account_verification_link(
                    _extract_email_body(message),
                    preferred_job_url=preferred_job_url,
                )
                if not verification_link:
                    continue
                candidate_key = _workday_verification_link_sort_key(
                    verification_link,
                    preferred_job_url=preferred_job_url,
                )
                if best_key is None or candidate_key > best_key:
                    best_key = candidate_key
                    best_link = verification_link

        if best_link:
            return best_link

        if time.monotonic() >= deadline:
            return None
        time.sleep(10)


def _extract_email_body(message: dict) -> str:
    """Extract plain text body from a Gmail message."""
    payload = message.get("payload", {})
    snippets: list[str] = []
    snippet = _collapse_email_body_text(str(message.get("snippet", "") or ""))
    if snippet:
        snippets.append(snippet)

    for part in _iter_gmail_parts(payload):
        mime_type = str(part.get("mimeType", "") or "").casefold()
        if mime_type not in {"text/plain", "text/html"}:
            continue
        decoded = _collapse_email_body_text(_decode_gmail_body_data(part.get("body", {}).get("data")))
        if decoded:
            snippets.append(decoded)

    if not snippets:
        return ""
    return "\n".join(dict.fromkeys(snippets))


# ─── Auth flow ───────────────────────────────────────────────────────────────


def _workday_credentials() -> tuple[str, str]:
    """Return (email, password) for Workday login."""
    profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    email = os.environ.get(WORKDAY_EMAIL_ENV) or getattr(profile, "verification_code_email", "") or ""
    password = os.environ.get(WORKDAY_PASSWORD_ENV, "")
    if not password:
        raise RuntimeError(
            "Workday password not configured. Set WORKDAY_PASSWORD in .env.local or as an environment variable."
        )
    if not email:
        raise RuntimeError(
            "Workday email not configured. Set WORKDAY_EMAIL env var or "
            "Verification Code Email in application_profile.md."
        )
    return email, password


def _normalize_workday_text(text: str | None, limit: int = 1000) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _extract_workday_auth_markers(page) -> dict[str, object]:
    page_text = ""
    heading_text = ""
    alert_text = ""
    visible_actions: list[str] = []

    try:
        page_text = _normalize_workday_text(page.inner_text("body"), limit=4000)
    except Exception:
        page_text = ""

    for selector in (
        "dialog h1, dialog h2, dialog h3",
        "[role='dialog'] h1, [role='dialog'] h2, [role='dialog'] h3",
        "main h1, main h2, main h3",
        "h1, h2, h3",
    ):
        try:
            locator = page.locator(selector)
            count = min(locator.count(), 4)
        except Exception:
            continue
        for idx in range(count):
            try:
                text = _normalize_workday_text(locator.nth(idx).inner_text(), limit=200)
            except Exception:
                text = ""
            if text:
                heading_text = text
                break
        if heading_text:
            break

    try:
        alert_locator = page.locator("[role='alert'], .errorMessage, [data-automation-id='errorMessage']")
        count = min(alert_locator.count(), 3)
    except Exception:
        count = 0
    for idx in range(count):
        try:
            text = _normalize_workday_text(alert_locator.nth(idx).inner_text(), limit=500)
        except Exception:
            text = ""
        if text:
            alert_text = text
            break

    try:
        action_locator = page.locator("button, a, [role='button']")
        count = min(action_locator.count(), 25)
    except Exception:
        count = 0
    for idx in range(count):
        try:
            text = _normalize_workday_text(action_locator.nth(idx).inner_text(), limit=120)
        except Exception:
            continue
        if text and text not in visible_actions:
            visible_actions.append(text)
    visible_actions = visible_actions[:12]

    markers = {
        "page_url": getattr(page, "url", "") or "",
        "page_text_excerpt": page_text,
        "heading_text": heading_text,
        "alert_text": alert_text,
        "visible_actions": visible_actions,
    }
    markers.update(
        _classify_workday_auth_state(
            page_url=markers["page_url"],
            page_text=page_text,
            alert_text=alert_text,
            heading_text=heading_text,
            visible_actions=visible_actions,
        )
    )
    return markers


def _write_workday_auth_result(out_dir: Path, result: dict[str, object]) -> None:
    submit_dir = role_submit_path(out_dir, "")
    submit_dir.mkdir(parents=True, exist_ok=True)
    log_path = submit_dir / WORKDAY_AUTH_RESULT_JSON
    log_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Workday auth details: {log_path.relative_to(PROJECT_ROOT)}", file=sys.stderr)

    submission_result = {
        "status": result.get("status"),
        "website_confirmed": False,
        "provider": "workday",
        "board": "workday",
        "updated_at_utc": result.get("updated_at_utc"),
        "message": result.get("message"),
        "auth_state": result.get("auth_state"),
        "auth_scope": result.get("auth_scope"),
        "retryable": result.get("retryable"),
    }
    result_path = submit_dir / "application_submission_result.json"
    result_path.write_text(json.dumps(submission_result, indent=2) + "\n", encoding="utf-8")


def _write_workday_failed_result(
    out_dir: Path,
    payload: dict,
    *,
    failure_type: str,
    message: str,
    current_page: str,
    validation_errors: list[str] | None = None,
) -> None:
    submit_dir = role_submit_path(out_dir, "")
    submit_dir.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, str] = {}
    debug_screenshot = str(payload.get("artifacts", {}).get("submit_debug_screenshot") or "").strip()
    if debug_screenshot and Path(debug_screenshot).exists():
        artifacts["submit_debug_screenshot"] = debug_screenshot
    debug_html = str(payload.get("artifacts", {}).get("submit_debug_html") or "").strip()
    if debug_html and Path(debug_html).exists():
        artifacts["submit_debug_html"] = debug_html
    result = {
        "status": "failed",
        "board": "workday",
        "provider": "workday",
        "website_confirmed": False,
        "updated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "job_url": payload.get("job_url", ""),
        "company": payload.get("company", ""),
        "job_title": payload.get("job_title", ""),
        "failure_type": failure_type,
        "message": message,
        "current_page": current_page,
    }
    if validation_errors:
        result["validation_errors"] = list(validation_errors)
    if artifacts:
        result["artifacts"] = artifacts
    result_path = submit_dir / "application_submission_result.json"
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def _clear_workday_failure_artifacts(payload: dict) -> None:
    submit_dir = role_submit_path(Path(payload["out_dir"]), "")
    artifact_candidates = [
        submit_dir / "application_submission_result.json",
        submit_dir / WORKDAY_AUTH_RESULT_JSON,
        Path(str(payload.get("artifacts", {}).get("submit_debug_html") or "")),
        Path(str(payload.get("artifacts", {}).get("submit_debug_screenshot") or "")),
    ]
    for candidate in artifact_candidates:
        try:
            if candidate and candidate.exists():
                candidate.unlink()
        except Exception:
            continue


def _write_workday_already_applied_result(out_dir: Path, payload: dict) -> None:
    submit_dir = role_submit_path(out_dir, "")
    submit_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "status": "already_applied",
        "board": "workday",
        "provider": "workday",
        "website_confirmed": True,
        "updated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "job_url": payload.get("job_url", ""),
        "company": payload.get("company", ""),
        "job_title": payload.get("job_title", ""),
        "message": "Workday already shows this job as applied.",
    }
    result_path = submit_dir / "application_submission_result.json"
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


class _WorkdayEmploymentEntry:
    def __init__(
        self,
        *,
        company: str,
        title: str,
        location: str,
        start_month: str,
        start_year: str,
        end_month: str,
        end_year: str,
        is_current: bool,
        bullets: list[str],
    ) -> None:
        self.company = company
        self.title = title
        self.location = location
        self.start_month = start_month
        self.start_year = start_year
        self.end_month = end_month
        self.end_year = end_year
        self.is_current = is_current
        self.bullets = bullets


class _WorkdayEducationEntry:
    def __init__(
        self,
        *,
        school: str,
        degree_text: str,
        degree_candidates: list[str],
        discipline_candidates: list[str],
        start_year: str,
        end_year: str,
    ) -> None:
        self.school = school
        self.degree_text = degree_text
        self.degree_candidates = degree_candidates
        self.discipline_candidates = discipline_candidates
        self.start_year = start_year
        self.end_year = end_year


def _workday_validation_failure_for_page(current_page: str) -> tuple[str, str]:
    page_label = current_page.replace("_", " ").strip() or "application"
    return (
        f"{current_page}_validation",
        f"Workday {page_label.title()} page still shows required validation errors after repeated retry attempts.",
    )


def _is_workday_apply_url(page_url: str) -> bool:
    return bool(re.search(r"/apply(?:/|$|\\?)", page_url.casefold()))


def _is_workday_already_applied_job_page(page) -> bool:
    try:
        body_text = re.sub(r"\s+", " ", page.inner_text("body")).strip().casefold()
    except Exception:
        body_text = ""
    if "you applied for this job" in body_text:
        return True
    try:
        return page.locator("button:has-text('View Application'), a:has-text('View Application')").count() > 0
    except Exception:
        return False


def _normalize_workday_source_hint(source_hint: str | None) -> str:
    normalized = re.sub(r"\s+", " ", str(source_hint or "")).strip().casefold()
    if "linkedin" in normalized:
        return "linkedin"
    return normalized


def _workday_source_hint_from_payload(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("job_source", "source", "source_url", "board_url", "job_url"):
        value = str(payload.get(key) or "").strip()
        if value:
            return _normalize_workday_source_hint(value)
    return ""


def _load_workday_resume_lines(path: Path) -> list[str]:
    lines = [line.rstrip() for line in path.read_text(encoding="utf-8").splitlines()]
    if "---" in lines:
        lines = lines[lines.index("---") + 1 :]
    return [line for line in lines if line.strip()]


def _workday_month_number(month_text: str) -> str:
    month_number = _WORKDAY_MONTH_ABBREV_TO_NUMBER.get(month_text.strip().casefold())
    return str(month_number) if month_number else ""


def _parse_workday_employment_entries(lines: list[str]) -> list[_WorkdayEmploymentEntry]:
    try:
        start_index = next(
            index for index, line in enumerate(lines) if line.strip() in _WORKDAY_RESUME_SECTION_HEADINGS
        )
    except StopIteration:
        return []

    entries: list[_WorkdayEmploymentEntry] = []
    index = start_index + 1
    while index < len(lines):
        line = lines[index].strip()
        if not line or line.startswith("* "):
            index += 1
            continue
        if line in _WORKDAY_RESUME_SECTION_END_HEADINGS:
            break
        company_match = re.match(r"^(?:##\s+)?(.+?)\s+[—–-]+\s+(.+)$", line)
        if not company_match:
            index += 1
            continue

        meta_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        meta_parts = [part.strip() for part in meta_line.split("|") if part.strip()]
        location = meta_parts[0] if meta_parts else ""
        date_fragment = meta_parts[1] if len(meta_parts) > 1 else meta_line
        date_match = re.search(
            r"(?P<start_month>[A-Za-z]+)\s+(?P<start_year>\d{4})\s*[–\-—]\s*(?P<end>(?:Present|Current|[A-Za-z]+\s+\d{4}))?",
            date_fragment,
            re.I,
        )
        if not date_match:
            index += 1
            continue

        start_month = _workday_month_number(date_match.group("start_month") or "")
        start_year = str(date_match.group("start_year") or "").strip()
        end_token = str(date_match.group("end") or "").strip()
        is_current = end_token.casefold() in {"present", "current"}
        end_month = ""
        end_year = ""
        if end_token and not is_current:
            end_match = re.match(r"(?P<month>[A-Za-z]+)\s+(?P<year>\d{4})", end_token)
            if end_match:
                end_month = _workday_month_number(end_match.group("month") or "")
                end_year = str(end_match.group("year") or "").strip()

        bullets: list[str] = []
        next_index = index + 2
        while next_index < len(lines):
            candidate_line = lines[next_index].strip()
            if candidate_line in _WORKDAY_RESUME_SECTION_END_HEADINGS:
                break
            if re.match(r"^(?:##\s+)?(.+?)\s+[—–-]+\s+(.+)$", candidate_line):
                break
            if candidate_line.startswith("* "):
                bullet = candidate_line[2:].strip()
                if bullet:
                    bullets.append(bullet)
            next_index += 1

        if start_month and start_year:
            entries.append(
                _WorkdayEmploymentEntry(
                    company=company_match.group(1).strip(),
                    title=company_match.group(2).strip(),
                    location=location,
                    start_month=start_month,
                    start_year=start_year,
                    end_month=end_month,
                    end_year=end_year,
                    is_current=is_current,
                    bullets=bullets,
                )
            )
        index = next_index

    return entries


def _workday_profile_education_parts(entry_text: str) -> tuple[str, str]:
    school, separator, degree_text = entry_text.partition(";")
    school = school.strip()
    degree_text = degree_text.strip() if separator else ""
    return school, degree_text


def _parse_workday_resume_education_details(lines: list[str]) -> list[dict[str, str]]:
    try:
        start_index = next(index for index, line in enumerate(lines) if line.strip() == "EDUCATION")
    except StopIteration:
        return []

    entries: list[dict[str, str]] = []
    index = start_index + 1
    while index < len(lines):
        school_line = lines[index].strip()
        if not school_line:
            index += 1
            continue
        if school_line in _WORKDAY_RESUME_SECTION_END_HEADINGS:
            break
        detail_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        year_match = re.search(r"(?P<start_year>\d{4})\s*[–\-—]\s*(?P<end_year>\d{4})", detail_line)
        if school_line and detail_line and "|" in detail_line:
            entries.append(
                {
                    "school": school_line,
                    "degree_detail": detail_line.split("|", 1)[0].strip(),
                    "start_year": str(year_match.group("start_year") if year_match else "").strip(),
                    "end_year": str(year_match.group("end_year") if year_match else "").strip(),
                }
            )
        index += 1
    return entries


def _workday_education_degree_candidates(degree_text: str) -> list[str]:
    normalized = degree_text.casefold()
    candidates: list[str] = []
    if re.search(r"\bm\.?b\.?a\b", normalized) or "master of business administration" in normalized:
        candidates.extend(
            [
                "Master of Business Administration (M.B.A.)",
                "MBA",
                "Master's Degree",
                "Master Degree",
                "Masters",
            ]
        )
    elif "master of science" in normalized or re.search(r"\bm\.?s\b", normalized):
        candidates.extend(["MS", "Master's Degree", "Master Degree", "Masters"])
    elif "master of arts" in normalized or re.search(r"\bm\.?a\b", normalized):
        candidates.extend(["MA", "Master's Degree", "Master Degree", "Masters"])
    elif "master" in normalized or re.search(r"\bm\.?s\b", normalized):
        candidates.extend(["Master's Degree", "Master Degree", "Masters"])
    elif "bachelor of science" in normalized or re.search(r"\bb\.?s\b", normalized):
        candidates.extend(["BS", "Bachelor's Degree", "Bachelor Degree", "Bachelors"])
    elif "bachelor of arts" in normalized or re.search(r"\bb\.?a\b", normalized):
        candidates.extend(["BA", "Bachelor's Degree", "Bachelor Degree", "Bachelors"])
    elif "bachelor of engineering" in normalized:
        candidates.extend(["Bachelor of Engineering", "BE", "Bachelor's Degree", "Bachelor Degree", "Bachelors"])
    elif "bachelor" in normalized or re.search(r"\bb\.?s\b", normalized):
        candidates.extend(["Bachelor's Degree", "Bachelor Degree", "Bachelors"])
    elif "associate of science" in normalized or re.search(r"\ba\.?s\b", normalized):
        candidates.extend(["AS", "Associate's Degree", "Associate Degree", "Associates"])
    elif "associate of arts" in normalized or re.search(r"\ba\.?a\b", normalized):
        candidates.extend(["AA", "Associate's Degree", "Associate Degree", "Associates"])
    elif "associate" in normalized:
        candidates.extend(["Associate's Degree", "Associate Degree", "Associates"])
    elif "juris doctor" in normalized or re.search(r"\bj\.?d\b", normalized):
        candidates.extend(["JD", "Doctorate", "Doctoral Degree"])
    elif "doctor" in normalized or re.search(r"\bph\.?d\b", normalized):
        candidates.extend(["PhD", "Doctorate", "Doctoral Degree"])
    elif "high school" in normalized:
        candidates.extend(["High School", "High School Diploma"])
    if degree_text:
        candidates.append(degree_text)
    return [candidate for candidate in dict.fromkeys(value.strip() for value in candidates if value.strip())]


def _workday_education_subject_fragments(*texts: str) -> list[str]:
    candidates: list[str] = []
    for text in texts:
        if not text:
            continue
        parenthetical_matches = re.findall(r"\(([^)]+)\)", text)
        for fragment in parenthetical_matches:
            lowered = fragment.casefold()
            if "dual degree" in lowered or re.search(r"\bm\.?b\.?a\b|\bm\.?s\b|\bb\.?s\b|\bph\.?d\b", lowered):
                continue
            candidates.append(fragment.strip())
        without_parenthetical = re.sub(r"\([^)]*\)", "", text).strip()
        match = re.search(r"\bin\s+(.+)$", without_parenthetical, re.I)
        if match:
            candidates.append(match.group(1).strip())

    normalized_subjects: list[str] = []
    for candidate in candidates:
        for part in re.split(r"\s*(?:&|/|,| and )\s*", candidate):
            subject = re.sub(r"\s+", " ", part).strip(" .;")
            if subject:
                normalized_subjects.append(subject)
    return [subject for subject in dict.fromkeys(normalized_subjects)]


def _workday_education_discipline_candidates(*texts: str) -> list[str]:
    candidates: list[str] = []
    joined = " ".join(texts).casefold()
    for subject in _workday_education_subject_fragments(*texts):
        candidates.append(subject)
    if "finance" in joined:
        candidates.append("Finance")
    if "business administration" in joined or re.search(r"\bm\.?b\.?a\b", joined):
        candidates.append("Business Administration")
    if "computer science" in joined:
        candidates.append("Computer Science")
    if "actuarial science" in joined:
        candidates.append("Actuarial Science")
    if "computational science" in joined:
        candidates.append("Computational Science")
    if "artificial intelligence" in joined:
        candidates.append("Artificial Intelligence")
    if "machine learning" in joined:
        candidates.append("Machine Learning")
    return [candidate for candidate in dict.fromkeys(value.strip() for value in candidates if value.strip())]


def _normalized_workday_school_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold()).strip()


def _build_workday_education_entries(application_profile, resume_lines: list[str]) -> list[_WorkdayEducationEntry]:
    profile_entries = list(getattr(application_profile, "education_entries", None) or [])
    resume_details = _parse_workday_resume_education_details(resume_lines)
    if not profile_entries and not resume_details:
        return []

    combined: list[_WorkdayEducationEntry] = []
    for index, profile_entry in enumerate(profile_entries):
        school, degree_text = _workday_profile_education_parts(profile_entry)
        matched_detail: dict[str, str] | None = None
        school_key = _normalized_workday_school_key(school)
        for detail in resume_details:
            detail_key = _normalized_workday_school_key(detail.get("school", ""))
            if school_key and detail_key and (school_key in detail_key or detail_key in school_key):
                matched_detail = detail
                break
        if matched_detail is None and index < len(resume_details):
            matched_detail = resume_details[index]

        detail_degree = matched_detail.get("degree_detail", "") if matched_detail else ""
        combined.append(
            _WorkdayEducationEntry(
                school=school,
                degree_text=degree_text,
                degree_candidates=_workday_education_degree_candidates(degree_text or detail_degree),
                discipline_candidates=_workday_education_discipline_candidates(detail_degree, degree_text),
                start_year=matched_detail.get("start_year", "") if matched_detail else "",
                end_year=matched_detail.get("end_year", "") if matched_detail else "",
            )
        )

    if combined:
        return combined

    for detail in resume_details:
        school = detail.get("school", "").strip()
        degree_text = detail.get("degree_detail", "").strip()
        combined.append(
            _WorkdayEducationEntry(
                school=school,
                degree_text=degree_text,
                degree_candidates=_workday_education_degree_candidates(degree_text),
                discipline_candidates=_workday_education_discipline_candidates(degree_text),
                start_year=detail.get("start_year", ""),
                end_year=detail.get("end_year", ""),
            )
        )
    return combined


def _workday_locator_value(locator) -> str:
    try:
        return str(locator.input_value() or "")
    except Exception:
        return ""


def _workday_role_description_text(entry: _WorkdayEmploymentEntry) -> str:
    return "\n".join(f"- {bullet}" for bullet in entry.bullets if bullet.strip())


def _workday_primary_language_entries(resume_lines: Sequence[str]) -> list[tuple[str, str]]:
    master_resume_text = "\n".join(str(line) for line in resume_lines if str(line).strip()) or None
    try:
        application_profile_text = APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
    except OSError:
        application_profile_text = None
    language_answer = build_source_backed_language_proficiency_answer(
        master_resume_text=master_resume_text,
        application_profile_text=application_profile_text,
    )
    if not language_answer:
        return []
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for language, proficiency in re.findall(r"([^,]+?)\s*\(([^)]+)\)", language_answer):
        normalized_language = re.sub(r"\s+", " ", language).strip()
        normalized_proficiency = re.sub(r"\s+", " ", proficiency).strip().casefold()
        if not normalized_language or not normalized_proficiency:
            continue
        key = normalized_language.casefold()
        if key in seen:
            continue
        seen.add(key)
        entries.append((normalized_language, normalized_proficiency))
    return entries


def _workday_language_level_candidates(proficiency: str) -> list[str]:
    normalized = normalize_text(proficiency)
    if normalized in {"native", "bilingual", "native bilingual"}:
        return ["C2", "Proficient/Native Speaker", "Native", "Fluent", "Advanced"]
    if normalized in {"fluent", "full professional", "professional working"}:
        return ["C2", "Proficient/Native Speaker", "Fluent", "C1", "Advanced", "Native"]
    if normalized in {"advanced", "professional proficiency"}:
        return ["C1", "Advanced", "B2", "Upper Intermediate"]
    if normalized in {"intermediate", "conversational"}:
        return ["B1", "Intermediate", "B2", "Upper Intermediate", "Conversational"]
    if normalized in {"basic", "beginner", "elementary"}:
        return ["A1", "Beginner", "A2", "Elementary", "Basic"]
    return ["C1", "Advanced", "B2", "Upper Intermediate", "Intermediate"]


def _workday_resume_skill_candidates(resume_lines: Sequence[str]) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for line in resume_lines:
        stripped = str(line).strip()
        if not stripped.startswith(("Technical:", "ML/AI:", "Data:")):
            continue
        _, _, remainder = stripped.partition(":")
        for fragment in re.split(r"[|,]", remainder):
            candidate = re.sub(r"\s+", " ", fragment).strip(" -")
            if not candidate:
                continue
            key = candidate.casefold()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
    return candidates


def _ensure_workday_repeater_rows(
    page,
    *,
    row_selector: str,
    target_count: int,
    add_button_selectors: Sequence[str],
):
    rows = page.locator(row_selector)
    while rows.count() < target_count:
        previous_count = rows.count()
        added = False
        for selector in add_button_selectors:
            buttons = page.locator(selector)
            for button_index in range(buttons.count()):
                button = buttons.nth(button_index)
                try:
                    if hasattr(button, "is_visible") and not button.is_visible():
                        continue
                    if hasattr(button, "is_enabled") and not button.is_enabled():
                        continue
                except Exception:
                    continue
                try:
                    button.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    button.click()
                except Exception:
                    continue
                page.wait_for_timeout(400)
                rows = page.locator(row_selector)
                if rows.count() > previous_count:
                    added = True
                    break
            if added:
                break
        if not added:
            break
    return rows


def _workday_source_answer_candidates(
    source_answer: str = "",
    company_name: str = "",
    *,
    source_hint: str = "",
    options: list[str] | None = None,
) -> tuple[str, ...]:
    profile_stub = SimpleNamespace(how_did_you_hear=source_answer or "")
    if options is not None:
        candidates, _ = resolve_how_did_you_hear_option_candidates(
            profile_stub,
            options,
            company_name=company_name,
            job_url=source_hint if "://" in str(source_hint or "") else None,
            source_hint=source_hint,
            prefer_metadata_job_board_matches=True,
        )
        return tuple(candidates)
    return tuple(
        build_how_did_you_hear_candidates(
            profile_stub,
            company_name=company_name,
            job_url=source_hint if "://" in str(source_hint or "") else None,
            source_hint=source_hint,
        )
    )


def _workday_source_preferences(
    label: str,
    *,
    source_hint: str = "",
    source_answer: str = "",
    company_name: str = "",
    options: list[str] | None = None,
) -> tuple[str, ...]:
    normalized_label = label.casefold()
    normalized_source_hint = _normalize_workday_source_hint(source_hint)
    ordered: list[str] = []
    if "how did you hear" in normalized_label:
        if normalized_source_hint == "linkedin":
            ordered.extend(["LinkedIn", "Social Networking", "Social Media", "Social"])
        ordered.extend(
            _workday_source_answer_candidates(source_answer, company_name, source_hint=source_hint, options=options)
        )
        ordered.extend(_WORKDAY_PRIMARY_SOURCE_PREFERENCES)
    else:
        if normalized_source_hint == "linkedin":
            ordered.append("LinkedIn")
        ordered.extend(
            _workday_source_answer_candidates(source_answer, company_name, source_hint=source_hint, options=options)
        )
        ordered.extend(_WORKDAY_SUBSOURCE_PREFERENCES)
    deduped: list[str] = []
    seen: set[str] = set()
    for value in ordered:
        key = re.sub(r"\s+", " ", value).strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return tuple(deduped)


def _preferred_workday_source_option(
    label: str,
    options: list[str],
    *,
    source_hint: str = "",
    source_answer: str = "",
    company_name: str = "",
) -> str | None:
    preferences = _workday_source_preferences(
        label,
        source_hint=source_hint,
        source_answer=source_answer,
        company_name=company_name,
        options=options,
    )
    normalized_options = [
        (option, re.sub(r"\s+", " ", option).strip().casefold()) for option in options if option.strip()
    ]
    for preferred in preferences:
        preferred_norm = preferred.casefold()
        for option, normalized in normalized_options:
            if normalized == preferred_norm:
                return option
        for option, normalized in normalized_options:
            if preferred_norm in normalized or normalized in preferred_norm:
                return option
    return None


def _workday_source_search_candidates(
    label: str,
    options: list[str],
    *,
    source_hint: str = "",
    source_answer: str = "",
    company_name: str = "",
) -> list[str]:
    candidates: list[str] = []
    normalized_source_hint = _normalize_workday_source_hint(source_hint)
    if "how did you hear" in label.casefold() and normalized_source_hint == "linkedin":
        candidates.extend(["LinkedIn", "Social Networking", "Social Media", "Social"])
    preferred = _preferred_workday_source_option(
        label,
        options,
        source_hint=source_hint,
        source_answer=source_answer,
        company_name=company_name,
    )
    if preferred:
        candidates.append(preferred)
    if not options:
        for preferred_text in _workday_source_preferences(
            label,
            source_hint=source_hint,
            source_answer=source_answer,
            company_name=company_name,
            options=None,
        ):
            candidates.append(preferred_text)
    normalized_options = [re.sub(r"\s+", " ", option).strip() for option in options if option.strip()]
    company_tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", str(company_name or "").casefold())
        if token and token not in {"inc", "llc", "corp", "corporation", "company", "co", "adus"}
    ]
    for option in normalized_options:
        option_norm = option.casefold()
        if not company_tokens:
            continue
        if not any(token in option_norm for token in company_tokens):
            continue
        if option_norm.endswith(".com") or "career site" in option_norm or "careers site" in option_norm:
            candidates.append(option)
    ordered_preferences = _workday_source_preferences(
        label,
        source_hint=source_hint,
        source_answer=source_answer,
        company_name=company_name,
        options=options,
    )
    for preferred_text in ordered_preferences:
        for option in normalized_options:
            option_norm = option.casefold()
            preferred_norm = preferred_text.casefold()
            if option_norm == preferred_norm or preferred_norm in option_norm or option_norm in preferred_norm:
                candidates.append(option)
    if options:
        for preferred_text in _workday_source_preferences(
            label,
            source_hint=source_hint,
            source_answer=source_answer,
            company_name=company_name,
            options=None,
        ):
            candidates.append(preferred_text)
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized_candidate = re.sub(r"\s+", " ", candidate).strip()
        if not normalized_candidate:
            continue
        key = normalized_candidate.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized_candidate)
    return deduped


def _looks_like_workday_prior_employment_prompt(text: str | None) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    return bool(
        "former employee" in normalized
        or "previous employee" in normalized
        or ("employ" in normalized and any(fragment in normalized for fragment in ("past", "previous", "former")))
        or ("work" in normalized and any(fragment in normalized for fragment in ("past", "previous", "former")))
    )


def _click_visible_locator(locator, *, prefer_last: bool = False) -> bool:
    try:
        count = locator.count()
    except Exception:
        return False
    indices = range(count - 1, -1, -1) if prefer_last else range(count)
    for idx in indices:
        target = locator.nth(idx)
        try:
            target.click(timeout=5000)
            return True
        except Exception:
            try:
                target.click(force=True, timeout=5000)
                return True
            except Exception:
                continue
    return False


def _click_visible_workday_prompt_option(page, expected_text: str) -> str | None:
    normalized_expected = re.sub(r"\s+", " ", expected_text).strip().casefold()
    clicked = page.evaluate(
        """(expectedText) => {
            const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
            const isVisible = (node) => {
                if (!node) return false;
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
            };
            const nodes = Array.from(document.querySelectorAll('[data-automation-id="menuItem"], [role="option"], [data-automation-id="promptOption"]'));
            for (const node of nodes) {
                const target =
                    node.getAttribute('data-automation-id') === 'menuItem'
                        ? node
                        : node.closest('[data-automation-id="menuItem"]') || node.closest('[role="option"]') || node;
                if (!isVisible(target)) continue;
                const text = normalize(target.textContent);
                const lower = text.toLowerCase();
                if (lower === expectedText || lower.includes(expectedText) || expectedText.includes(lower)) {
                    target.click();
                    return text;
                }
            }
            return null;
        }""",
        normalized_expected,
    )
    return str(clicked).strip() if clicked else None


def _select_workday_prompt_option_for_label(page, label_text: str, expected_text: str) -> str | None:
    normalized_expected = re.sub(r"\s+", " ", expected_text).strip().casefold()
    clicked = page.evaluate(
        """({labelText, expectedText}) => {
            const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
            const isVisible = (node) => {
                if (!node) return false;
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
            };
            const labels = Array.from(document.querySelectorAll('label'));
            for (const label of labels) {
                if (!normalize(label.textContent).toLowerCase().includes(labelText.toLowerCase())) continue;
                const targetId = label.getAttribute('for');
                if (!targetId) continue;
                const input = document.getElementById(targetId);
                if (!input) continue;
                const multiId = input.getAttribute('data-uxi-multiselect-id');
                if (!multiId) continue;
                const nodes = Array.from(
                    document.querySelectorAll(`[data-uxi-multiselect-id="${multiId}"][data-automation-id="menuItem"], [data-uxi-multiselect-id="${multiId}"][data-automation-id="promptOption"]`)
                );
                for (const node of nodes) {
                    const target =
                        node.getAttribute('data-automation-id') === 'menuItem'
                            ? node
                            : node.closest('[data-automation-id="menuItem"]') || node.closest('[role="option"]') || node;
                    if (!isVisible(target)) continue;
                    const text = normalize(target.textContent);
                    const lower = text.toLowerCase();
                    if (lower === expectedText || lower.includes(expectedText) || expectedText.includes(lower)) {
                        target.click();
                        return text;
                    }
                }
            }
            return null;
        }""",
        {"labelText": label_text, "expectedText": normalized_expected},
    )
    return str(clicked).strip() if clicked else None


def _workday_multiselect_id_for_label(page, label_text: str) -> str | None:
    multi_id = page.evaluate(
        """(labelText) => {
            const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
            const labels = Array.from(document.querySelectorAll('label'));
            for (const label of labels) {
                if (!normalize(label.textContent).toLowerCase().includes(labelText.toLowerCase())) continue;
                const targetId = label.getAttribute('for');
                if (!targetId) continue;
                const input = document.getElementById(targetId);
                if (!input) continue;
                return input.getAttribute('data-uxi-multiselect-id') || null;
            }
            return null;
        }""",
        label_text,
    )
    return str(multi_id).strip() if multi_id else None


def _select_workday_prompt_option_via_locator(page, label_text: str, expected_text: str) -> str | None:
    multi_id = _workday_multiselect_id_for_label(page, label_text)
    if not multi_id:
        return None
    selector = f'[data-uxi-multiselect-id="{multi_id}"][data-automation-id="menuItem"]'
    options = page.locator(selector).filter(has_text=expected_text)
    if not options.count():
        return None
    for index in range(options.count() - 1, -1, -1):
        option = options.nth(index)
        try:
            option.click(force=True, timeout=2000)
            page.wait_for_timeout(500)
            try:
                return re.sub(r"\s+", " ", option.inner_text()).strip()
            except Exception:
                return expected_text
        except Exception:
            continue
    return None


def _workday_prompt_state_for_label(page, label_text: str) -> dict[str, object]:
    state = page.evaluate(
        """(labelText) => {
            const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
            const labels = Array.from(document.querySelectorAll('label'));
            const label = labels.find((candidate) =>
                normalize(candidate.textContent).toLowerCase().includes(labelText.toLowerCase())
            );
            if (!label) {
                return {
                    fieldText: "",
                    inputValue: "",
                    promptInstruction: "",
                    selectedItems: [],
                    visibleOptions: [],
                    highlightedOptions: [],
                };
            }
            const field =
                label.closest('[data-automation-id*="formField"]') ||
                label.parentElement ||
                document;
            const inputId = label.getAttribute('for') || '';
            const input = inputId ? document.getElementById(inputId) : null;
            const selectedItems = Array.from(field.querySelectorAll('[data-automation-id="selectedItem"]'))
                .map((selected) => {
                    const prompt = selected.querySelector('[data-automation-id="promptOption"]') || selected;
                    return normalize(prompt.textContent);
                })
                .filter(Boolean);
            const isVisible = (node) => {
                if (!node) return false;
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
            };
            const visibleOptions = [];
            const highlightedOptions = [];
            for (const node of document.querySelectorAll('[data-automation-id="menuItem"], [role="option"], [data-automation-id="promptOption"]')) {
                const target =
                    node.getAttribute('data-automation-id') === 'menuItem'
                        ? node
                        : node.closest('[data-automation-id="menuItem"]') || node.closest('[role="option"]') || node;
                if (!isVisible(target)) continue;
                const text = normalize(target.textContent);
                if (!text || text.includes('+1') || text.includes('current step') || text.includes('Error-')) continue;
                visibleOptions.push(text);
                if ((target.getAttribute('aria-selected') || '').toLowerCase() === 'true') {
                    highlightedOptions.push(text);
                }
            }
            const promptInstruction = normalize(
                field.querySelector('[data-automation-id="promptAriaInstruction"]')?.textContent || ''
            );
            return {
                fieldText: normalize(field.textContent),
                inputValue: input ? String(input.value || '').trim() : '',
                promptInstruction,
                selectedItems,
                visibleOptions,
                highlightedOptions,
            };
        }""",
        label_text,
    )
    if not isinstance(state, dict):
        return {
            "fieldText": "",
            "inputValue": "",
            "promptInstruction": "",
            "selectedItems": [],
            "visibleOptions": [],
            "highlightedOptions": [],
        }
    normalized_state: dict[str, object] = dict(state)
    for key in ("selectedItems", "visibleOptions", "highlightedOptions"):
        value = normalized_state.get(key)
        normalized_state[key] = list(value) if isinstance(value, list) else []
    for key in ("fieldText", "inputValue", "promptInstruction"):
        normalized_state[key] = str(normalized_state.get(key) or "")
    return normalized_state


def _advance_workday_prompt_highlighted_selection(page, label_text: str) -> dict[str, object]:
    state = _workday_prompt_state_for_label(page, label_text)
    if state.get("selectedItems") or not state.get("highlightedOptions"):
        return state
    before_signature = (
        tuple(str(option) for option in state.get("visibleOptions", [])),
        tuple(str(option) for option in state.get("highlightedOptions", [])),
        str(state.get("promptInstruction") or ""),
    )
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    advanced = _workday_prompt_state_for_label(page, label_text)
    after_signature = (
        tuple(str(option) for option in advanced.get("visibleOptions", [])),
        tuple(str(option) for option in advanced.get("highlightedOptions", [])),
        str(advanced.get("promptInstruction") or ""),
    )
    if advanced.get("selectedItems") or after_signature != before_signature:
        return advanced
    page.keyboard.press("Space")
    page.wait_for_timeout(500)
    return _workday_prompt_state_for_label(page, label_text)


def _workday_prompt_match_key(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().casefold()
    if not normalized:
        return ""
    tokenized = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
    if tokenized.endswith("s") and len(tokenized) > 3:
        tokenized = tokenized[:-1]
    return re.sub(r"\s+", " ", tokenized).strip()


def _workday_prompt_committed_value(state: dict[str, object]) -> str | None:
    selected_items = [re.sub(r"\s+", " ", str(item)).strip() for item in state.get("selectedItems", [])]
    selected_items = [item for item in selected_items if item]
    if selected_items:
        return selected_items[0]
    prompt_instruction = re.sub(r"\s+", " ", str(state.get("promptInstruction") or "")).strip()
    if not prompt_instruction:
        return None
    match = re.search(
        r"\b\d+\s+item(?:s)?\s+selected(?:\s*,\s*|\s*:\s*|\s+-\s*)(.+)$",
        prompt_instruction,
        flags=re.I,
    )
    if not match:
        return None
    committed_value = re.sub(r"\s+", " ", match.group(1)).strip(" ,;")
    return committed_value or None


def _workday_prompt_matching_value(
    page,
    label_text: str,
    expected_text: str,
    *,
    state: dict[str, object] | None = None,
) -> str | None:
    current_state = state if state is not None else _workday_prompt_state_for_label(page, label_text)
    committed_value = _workday_prompt_committed_value(current_state)
    if not committed_value:
        return None
    if _workday_prompt_match_key(committed_value) == _workday_prompt_match_key(expected_text):
        return committed_value
    normalized_label = normalize_text(label_text)
    if "how did you hear about us" in normalized_label and _workday_option_text_matches(expected_text, committed_value):
        return committed_value
    return None


def _workday_prompt_nested_options(
    state: dict[str, object],
    previous_signature: tuple[str, ...],
) -> list[str] | None:
    if _workday_prompt_committed_value(state):
        return None
    nested_options = [str(option) for option in state.get("visibleOptions", []) if str(option or "").strip()]
    nested_signature = tuple(_workday_prompt_match_key(option) for option in nested_options if _workday_prompt_match_key(option))
    if not nested_signature or nested_signature == previous_signature:
        return None
    return nested_options


def _clear_workday_prompt_selection(page, label_text: str) -> bool:
    cleared = page.evaluate(
        """(labelText) => {
            const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const labels = Array.from(document.querySelectorAll('label'));
            const label = labels.find((candidate) => normalize(candidate.textContent).includes(normalize(labelText)));
            if (!label) return false;
            const field =
                label.closest('[data-automation-id*="formField"]') ||
                label.parentElement ||
                document;
            const selectedItems = Array.from(field.querySelectorAll('[data-automation-id="selectedItem"]'));
            if (!selectedItems.length) return false;
            for (const item of selectedItems) {
                const deleteCharm =
                    item.querySelector('[data-automation-id="DELETE_charm"]') ||
                    item.querySelector('button') ||
                    item.querySelector('[role="button"]');
                if (deleteCharm) {
                    deleteCharm.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                    deleteCharm.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                    deleteCharm.click();
                    return true;
                }
            }
            return false;
        }""",
        label_text,
    )
    if not cleared:
        return False
    page.wait_for_timeout(250)
    latest_state = _workday_prompt_state_for_label(page, label_text)
    return _workday_prompt_committed_value(latest_state) is None


def _clear_mismatched_workday_prompt_selection(
    page,
    label_text: str,
    expected_text: str,
    *,
    state: dict[str, object] | None = None,
) -> bool:
    current_state = state if state is not None else _workday_prompt_state_for_label(page, label_text)
    committed_value = _workday_prompt_committed_value(current_state)
    if not committed_value:
        return False
    if _workday_prompt_match_key(committed_value) == _workday_prompt_match_key(expected_text):
        return False
    return _clear_workday_prompt_selection(page, label_text)


def _workday_prompt_selection_matches(page, label_text: str, expected_text: str) -> bool:
    return _workday_prompt_matching_value(page, label_text, expected_text) is not None


def _select_workday_prompt_option_via_keyboard(page, expected_text: str) -> None:
    try:
        page.keyboard.press("Meta+A")
    except Exception:
        pass
    try:
        page.keyboard.press("Control+A")
    except Exception:
        pass
    page.keyboard.press("Backspace")
    page.keyboard.type(expected_text, delay=40)
    page.wait_for_timeout(500)
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)


def _select_workday_prompt_option_via_input(
    page,
    input_selector: str,
    expected_text: str,
    *,
    label_text: str | None = None,
) -> bool:
    input_locator = page.locator(input_selector).first
    if not input_locator.count():
        return False
    try:
        input_locator.click(timeout=3000)
    except Exception:
        try:
            input_locator.click(force=True, timeout=3000)
        except Exception:
            input_locator.focus()
    try:
        input_locator.fill("")
    except Exception:
        for key in ("Meta+A", "Control+A", "Backspace"):
            try:
                input_locator.press(key)
            except Exception:
                continue
    input_locator.type(expected_text, delay=40)
    page.wait_for_timeout(800)

    option_entries = _workday_visible_prompt_options(page)
    for text, option in reversed(option_entries):
        if expected_text.casefold() not in text.casefold():
            continue
        try:
            option.click(force=True, timeout=2000)
            page.wait_for_timeout(500)
            if not label_text or _workday_prompt_selection_matches(page, label_text, expected_text):
                return True
            input_locator.click()
            page.wait_for_timeout(250)
        except Exception:
            continue

    input_locator.press("ArrowDown")
    input_locator.press("Enter")
    page.wait_for_timeout(500)
    return not label_text or _workday_prompt_selection_matches(page, label_text, expected_text)


def _visible_workday_validation_errors(page) -> list[str]:
    errors: list[str] = []
    try:
        locator = page.locator(
            "[role='alert'], .error, .errorMessage, .css-14d18rb, "
            "[data-automation-id='errorHeading'] button, "
            "p[data-automation-id='inputAlert'], "
            "[id^='hint']"
        )
        count = locator.count()
    except Exception:
        return errors
    for idx in range(count):
        candidate = locator.nth(idx)
        try:
            text = re.sub(r"\s+", " ", candidate.inner_text()).strip()
        except Exception:
            continue
        try:
            described_by = candidate.get_attribute("aria-describedby") or ""
        except Exception:
            described_by = ""
        if described_by:
            try:
                described = page.locator(_workday_dom_id_selector(described_by))
                if described.count():
                    described_text = re.sub(r"\s+", " ", described.first.inner_text()).strip()
                    if described_text:
                        text = described_text
            except Exception:
                pass
        text = re.sub(r"^\s*error:\s*", "", text, flags=re.I)
        if not text:
            continue
        if any(pattern.search(text) for pattern in VALIDATION_ERROR_PATTERNS):
            if text not in errors:
                errors.append(text)
    if not errors:
        try:
            body_text = re.sub(r"\s+", " ", page.inner_text("body")).strip()
        except Exception:
            body_text = ""
        lower_body = body_text.casefold()
        if "how did you hear about us?" in lower_body and "must have a value" in lower_body:
            errors.append("The field How Did You Hear About Us? is required and must have a value.")
    return errors


def _open_workday_sign_in(page) -> bool:
    if _has_visible_workday_sign_in_form(page):
        return True
    markers = _extract_workday_auth_markers(page)
    if markers.get("auth_state") == "sign_in_gate" and not _has_visible_workday_create_account_form(page):
        return True
    if _click_visible_locator(page.locator("button:has-text('Sign In'), a:has-text('Sign In')"), prefer_last=True):
        page.wait_for_timeout(3000)
    if _has_visible_workday_sign_in_form(page):
        return True
    markers = _extract_workday_auth_markers(page)
    return markers.get("auth_state") in {"sign_in_gate", "credential_rejected"}


def _open_workday_create_account(page) -> bool:
    if _has_visible_workday_create_account_form(page):
        return True
    markers = _extract_workday_auth_markers(page)
    if markers.get("auth_state") == "create_account_gate" and not _has_visible_workday_sign_in_form(page):
        return True
    create_account_link = page.locator("a:has-text('Create Account'), button:has-text('Create Account')")
    if not create_account_link.count() and _open_workday_email_sign_in_entrypoint(page):
        create_account_link = page.locator("a:has-text('Create Account'), button:has-text('Create Account')")
    if _click_visible_locator(create_account_link, prefer_last=True):
        page.wait_for_timeout(3000)
    if _has_visible_workday_create_account_form(page):
        return True
    return _extract_workday_auth_markers(page).get("auth_state") == "create_account_gate"


def _enter_workday_application_flow(page, *, out_dir: Path | None = None) -> None:
    continue_btn = page.locator("button:has-text('Continue Application'), a:has-text('Continue Application')")
    if _click_visible_locator(continue_btn):
        page.wait_for_timeout(5000)
        return
    view_btn = page.locator("button:has-text('View Application'), a:has-text('View Application')")
    if _click_visible_locator(view_btn):
        page.wait_for_timeout(5000)
        return

    apply_btn = page.get_by_role("button", name="Apply")
    if apply_btn.count():
        apply_btn.first.click()
        page.wait_for_timeout(3000)

    apply_manually = page.get_by_role("button", name="Apply Manually")
    autofill_btn = page.get_by_role("button", name="Autofill with Resume")
    if apply_manually.count():
        apply_manually.first.click()
        page.wait_for_timeout(5000)
    elif autofill_btn.count():
        autofill_btn.first.click()
        page.wait_for_timeout(3000)
        file_input = page.locator("input[type='file']").first
        if file_input.count() and out_dir:
            try:
                resume_path = find_resume_file(out_dir)
                if _workday_resume_already_uploaded(page, resume_path.name):
                    print(f"Workday: resume already uploaded via Autofill: {resume_path.name}", file=sys.stderr)
                else:
                    file_input.set_input_files(str(resume_path))
                    page.wait_for_timeout(5000)
                    _dedupe_workday_uploaded_resume_items(page, resume_path.name, keep=1)
                    print(f"Workday: uploaded resume via Autofill: {resume_path.name}", file=sys.stderr)
            except (FileNotFoundError, Exception) as exc:
                print(f"Workday: resume upload during autofill failed: {exc}", file=sys.stderr)
    else:
        use_last = page.get_by_role("button", name="Use My Last Application")
        if use_last.count():
            use_last.first.click()
            page.wait_for_timeout(3000)


def _resume_workday_application(page, job_url: str, *, out_dir: Path | None = None) -> bool:
    if not job_url:
        return False
    try:
        page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        _enter_workday_application_flow(page, out_dir=out_dir)
        page.wait_for_timeout(3000)
    except Exception:
        return False
    return _is_application_page(page)


def _ensure_workday_application_context(page, job_url: str, *, out_dir: Path | None = None) -> bool:
    if _is_application_page(page):
        return True
    if page.locator("button:has-text('Apply'), a:has-text('Apply')").count():
        return _resume_workday_application(page, job_url, out_dir=out_dir)
    if page.locator("button:has-text('Continue Application'), a:has-text('Continue Application')").count():
        return _resume_workday_application(page, job_url, out_dir=out_dir)
    if page.locator("button:has-text('View Application'), a:has-text('View Application')").count():
        return _resume_workday_application(page, job_url, out_dir=out_dir)
    markers = _extract_workday_auth_markers(page)
    if markers.get("auth_state") == "authenticated_non_form":
        return _resume_workday_application(page, job_url, out_dir=out_dir)
    return _is_application_page(page)


def _run_workday_auth_flow(
    page,
    email: str,
    password: str,
    *,
    payload: dict,
    job_url: str,
    out_dir: Path | None = None,
    enter_application_flow: bool,
) -> dict[str, object]:
    def _markers_with_deferred_auth_error(
        markers: dict[str, object], deferred_auth_error: str | None
    ) -> dict[str, object]:
        if not deferred_auth_error:
            return markers
        if str(markers.get("alert_text") or "").strip():
            return markers
        updated = dict(markers)
        updated["alert_text"] = deferred_auth_error
        return updated

    if enter_application_flow:
        _enter_workday_application_flow(page, out_dir=out_dir)
        page.wait_for_timeout(3000)
    if _ensure_workday_application_context(page, job_url, out_dir=out_dir):
        return {"ok": True}
    if _is_workday_already_applied_job_page(page):
        return {"ok": True, "already_applied": True}

    last_attempted_step: str | None = None
    credential_rejection_observed = False
    deferred_auth_error: str | None = None
    last_informative_auth_state: str | None = None

    def _remember_auth_state(markers: dict[str, object]) -> None:
        nonlocal last_informative_auth_state
        observed_auth_state = str(markers.get("auth_state") or "").strip()
        if observed_auth_state and observed_auth_state != "unknown":
            last_informative_auth_state = observed_auth_state

    for step in WORKDAY_AUTH_RECOVERY_STEPS:
        markers = _extract_workday_auth_markers(page)
        _remember_auth_state(markers)
        auth_state = str(markers.get("auth_state") or "unknown")
        if auth_state == "maintenance":
            markers = _markers_with_deferred_auth_error(markers, deferred_auth_error)
            return {
                "ok": False,
                "result": _build_workday_auth_result(
                    payload,
                    markers,
                    auth_scope=workday_auth_scope(job_url),
                    last_attempted_step=last_attempted_step,
                    credential_rejection_observed=credential_rejection_observed,
                    auth_state_hint=last_informative_auth_state,
                ),
            }

        last_attempted_step = step
        print(f"Workday: attempting auth step '{step}'...", file=sys.stderr)
        try:
            if step == "sign_in":
                _open_workday_sign_in(page)
                _do_sign_in(page, email, password)
            elif step == "password_reset":
                _do_password_reset(page, email, password)
            else:
                _open_workday_create_account(page)
                _do_create_account(page, email, password, job_url=job_url)
        except Exception as exc:
            print(f"Workday: auth step '{step}' raised {exc!r}", file=sys.stderr)
            if _is_gws_auth_failure(exc):
                deferred_auth_error = str(exc)
                continue
            error_markers = _extract_workday_auth_markers(page)
            _remember_auth_state(error_markers)
            if not str(error_markers.get("alert_text") or "").strip():
                error_markers["alert_text"] = str(exc)
            error_markers = _markers_with_deferred_auth_error(error_markers, deferred_auth_error)
            return {
                "ok": False,
                "result": _build_workday_auth_result(
                    payload,
                    error_markers,
                    auth_scope=workday_auth_scope(job_url),
                    last_attempted_step=last_attempted_step,
                    credential_rejection_observed=credential_rejection_observed,
                    auth_state_hint=last_informative_auth_state,
                ),
            }

        if _ensure_workday_application_context(page, job_url, out_dir=out_dir):
            return {"ok": True}
        if _is_workday_already_applied_job_page(page):
            return {"ok": True, "already_applied": True}

        markers = _extract_workday_auth_markers(page)
        _remember_auth_state(markers)
        if markers.get("auth_state") == "credential_rejected":
            credential_rejection_observed = True
        if markers.get("auth_state") == "maintenance":
            markers = _markers_with_deferred_auth_error(markers, deferred_auth_error)
            return {
                "ok": False,
                "result": _build_workday_auth_result(
                    payload,
                    markers,
                    auth_scope=workday_auth_scope(job_url),
                    last_attempted_step=last_attempted_step,
                    credential_rejection_observed=credential_rejection_observed,
                    auth_state_hint=last_informative_auth_state,
                ),
            }

    final_markers = _extract_workday_auth_markers(page)
    _remember_auth_state(final_markers)
    final_markers = _markers_with_deferred_auth_error(final_markers, deferred_auth_error)
    return {
        "ok": False,
        "result": _build_workday_auth_result(
            payload,
            final_markers,
            auth_scope=workday_auth_scope(job_url),
            last_attempted_step=last_attempted_step,
            credential_rejection_observed=credential_rejection_observed,
            auth_state_hint=last_informative_auth_state,
        ),
    }


def _handle_auth(
    page,
    email: str,
    password: str,
    *,
    payload: dict,
    job_url: str,
    out_dir: Path | None = None,
) -> dict[str, object]:
    """Run the canonical Workday auth sequence and return a result dict."""
    return _run_workday_auth_flow(
        page,
        email,
        password,
        payload=payload,
        job_url=job_url,
        out_dir=out_dir,
        enter_application_flow=True,
    )


def _do_sign_in(page, email: str, password: str) -> bool:
    """Fill sign-in form and submit.

    The Sign In form may appear as a modal dialog overlaying the Create Account
    page.  Scope interactions to the dialog when present so we don't
    accidentally fill the Create Account fields behind it.
    """
    # Scope to dialog if present, otherwise full page
    dialog = page.locator("dialog, [role='dialog']")
    scope = dialog.first if dialog.count() else page

    email_input = _workday_email_field(scope, "Email Address")
    # Use exact=True to avoid matching "Verify New Password"
    password_input = _workday_password_field(scope, "Password")

    if not (_workday_can_fill(email_input) and _workday_can_fill(password_input)):
        if _open_workday_email_sign_in_entrypoint(page):
            dialog = page.locator("dialog, [role='dialog']")
            scope = dialog.first if dialog.count() else page
            email_input = _workday_email_field(scope, "Email Address")
            password_input = _workday_password_field(scope, "Password")

    if not _workday_fill_if_visible(email_input, email):
        return False
    if not _workday_fill_if_visible(password_input, password):
        return False

    clicked_sign_in = _click_workday_button(
        scope,
        "[data-automation-id='signInSubmitButton'], button:has-text('Sign In')",
    )
    if not clicked_sign_in:
        sign_in_btn = scope.get_by_role("button", name="Sign In")
        if sign_in_btn.count():
            sign_in_btn.first.click()
            clicked_sign_in = True
    if clicked_sign_in:
        page.wait_for_timeout(5000)

    # Check if sign-in succeeded (dialog should close and form should appear)
    if _is_application_page(page):
        return True

    # Check for error messages
    error_text = scope.locator("[role='alert'], .errorMessage")
    if error_text.count():
        err = error_text.first.inner_text()
        print(f"Workday sign-in error: {err}", file=sys.stderr)
        # Don't call password reset here — let the caller handle it
        return False

    page.wait_for_timeout(3000)
    if _is_application_page(page):
        return True

    # Sign-in may have succeeded but Workday redirected to a non-form page
    # (e.g., /userHome). Detect this to avoid unnecessary password reset attempts.
    if "/userhome" in page.url.lower():
        print("Workday: sign-in succeeded (redirected to userHome).", file=sys.stderr)
        return True

    return False


def _click_workday_button(page_or_locator, selector: str) -> bool:
    """Click a Workday button, handling the click_filter overlay.

    Workday wraps many buttons with a ``data-automation-id="click_filter"``
    overlay that intercepts pointer events.  Try the visible role button
    first, then force-click through the interceptor.
    """
    btn = page_or_locator.locator(selector)
    if not btn.count():
        return False
    try:
        btn.first.click(timeout=5000)
        return True
    except Exception:
        pass
    # Force-click through the overlay
    try:
        btn.first.click(force=True, timeout=5000)
        return True
    except Exception:
        pass
    # Try the click_filter overlay sibling
    try:
        parent = btn.first.locator("..")
        overlay = parent.locator("[data-automation-id='click_filter']")
        if overlay.count():
            overlay.first.click(timeout=5000)
            return True
    except Exception:
        pass
    return False


def _workday_preferred_locator(locator, *, prefer_last_visible: bool = False):
    """Return a single visible locator when Workday renders duplicate fields."""
    count = locator.count()
    if count == 0:
        return locator
    if count == 1:
        return locator.first

    indices = range(count - 1, -1, -1) if prefer_last_visible else range(count)
    for idx in indices:
        candidate = locator.nth(idx)
        try:
            if candidate.is_visible():
                return candidate
        except Exception:
            continue

    fallback_index = count - 1 if prefer_last_visible else 0
    return locator.nth(fallback_index)


def _workday_preferred_visible_checkbox(locator, *, prefer_last_visible: bool = False):
    """Return a single visible checkbox locator when duplicate or hidden controls exist."""
    count = locator.count()
    if count == 0:
        return locator

    indices = range(count - 1, -1, -1) if prefer_last_visible else range(count)
    for idx in indices:
        candidate = locator.nth(idx)
        try:
            if candidate.is_visible():
                return candidate
        except Exception:
            continue

    fallback_index = count - 1 if prefer_last_visible else 0
    return locator.nth(fallback_index)


def _workday_can_fill(locator) -> bool:
    if not locator.count():
        return False
    try:
        if not locator.is_visible():
            return False
    except Exception:
        return False
    try:
        return locator.is_enabled()
    except Exception:
        return True


def _workday_fill_if_visible(locator, value: str) -> bool:
    if not _workday_can_fill(locator):
        return False
    for attempt in range(2):
        try:
            if attempt == 0:
                locator.fill(value)
            else:
                try:
                    locator.fill("")
                except Exception:
                    pass
                human_fill(locator, value, delay_ms=35)
        except Exception:
            continue
        retained_value = _workday_locator_value(locator)
        if retained_value is None or _workday_filled_value_matches(value, retained_value):
            return True
    return False


def _workday_locator_value(locator) -> str | None:
    try:
        return locator.input_value()
    except Exception:
        pass
    try:
        value = locator.get_attribute("value")
    except Exception:
        value = None
    if value is not None:
        return value
    try:
        return locator.inner_text()
    except Exception:
        return None


def _workday_filled_value_matches(expected: str, actual: str) -> bool:
    normalized_expected = re.sub(r"\s+", " ", expected).strip()
    normalized_actual = re.sub(r"\s+", " ", actual).strip()
    if normalized_expected == normalized_actual:
        return True
    percentage_pattern = re.compile(r"(?:up to\s+)?(\d{1,3})(?:\s*%)?$", re.I)
    expected_percentage = percentage_pattern.fullmatch(normalized_expected)
    actual_percentage = percentage_pattern.fullmatch(normalized_actual)
    return bool(
        expected_percentage and actual_percentage and expected_percentage.group(1) == actual_percentage.group(1)
    )


def _workday_textbox_field(
    scope,
    *accessible_names: str,
    selectors: tuple[str, ...],
    prefer_last_visible: bool = False,
    exact: bool | None = None,
):
    role_kwargs = {"exact": exact} if exact is not None else {}
    for name in accessible_names:
        field = _workday_preferred_locator(
            scope.get_by_role("textbox", name=name, **role_kwargs),
            prefer_last_visible=prefer_last_visible,
        )
        if field.count():
            return field

    for selector in selectors:
        field = _workday_preferred_locator(
            scope.locator(selector),
            prefer_last_visible=prefer_last_visible,
        )
        if field.count():
            return field

    return scope.locator(selectors[0])


def _workday_question_text_field(group):
    """Resolve the visible editable text control inside a Workday question group."""
    return _workday_preferred_locator(group.locator("input[type='text'], input:not([type])"))


def _workday_question_textarea_field(group):
    """Resolve the visible editable textarea control inside a Workday question group."""
    return _workday_preferred_locator(group.locator("textarea"))


def _workday_question_has_date_input(group) -> bool:
    return (
        group.locator(
            "[data-automation-id='dateInputWrapper'], "
            "[data-automation-id='dateSectionMonth-input'], "
            "[data-automation-id='dateSectionDay-input'], "
            "[data-automation-id='dateSectionYear-input']"
        ).count()
        > 0
    )


def _workday_question_date_field(group, segment: str):
    return _workday_preferred_locator(
        group.locator(f"[data-automation-id='dateSection{segment}-input'], input[aria-label='{segment}']")
    )


def _workday_parse_date_answer(value: str) -> tuple[str, str, str] | None:
    stripped = value.strip()
    if not stripped:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            parsed = datetime.strptime(stripped, fmt)
        except ValueError:
            continue
        return str(parsed.month), str(parsed.day), str(parsed.year)
    return None


def _workday_field_has_input_alert(scope) -> bool:
    try:
        alerts = scope.locator("p[data-automation-id='inputAlert'], [data-automation-id='inputAlert']")
        count = alerts.count()
    except Exception:
        return False
    for idx in range(count):
        try:
            text = re.sub(r"\s+", " ", alerts.nth(idx).inner_text()).strip()
        except Exception:
            continue
        text = re.sub(r"^\s*error:\s*", "", text, flags=re.I)
        if text and any(pattern.search(text) for pattern in VALIDATION_ERROR_PATTERNS):
            return True
    return False


def _focus_workday_date_segment(locator) -> bool:
    if not locator.count():
        return False
    try:
        locator.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        locator.focus()
        return True
    except Exception:
        pass
    try:
        locator.evaluate(
            """(input) => {
                if (!input) return false;
                input.scrollIntoView({ block: 'center', inline: 'nearest' });
                input.focus();
                return document.activeElement === input;
            }"""
        )
        return True
    except Exception:
        pass
    try:
        locator.click(force=True, timeout=3000)
        return True
    except Exception:
        return False


def _fill_workday_segmented_date_scope_with_navigation(
    scope,
    month_value: str,
    day_value: str,
    year_value: str,
) -> bool:
    # Workday segmented date controls behave like one keyboard-navigated widget,
    # so we commit each section in order instead of clearing the inputs independently.
    segments = (
        (_workday_question_date_field(scope, "Month"), month_value, "ArrowRight"),
        (_workday_question_date_field(scope, "Day"), day_value, "ArrowRight"),
        (_workday_question_date_field(scope, "Year"), year_value, "Tab"),
    )
    for locator, _expected_value, _advance_key in segments:
        if not locator.count():
            return False
    for locator, expected_value, advance_key in segments:
        if not _focus_workday_date_segment(locator):
            return False
        try:
            locator.type(expected_value, delay=35)
        except Exception:
            try:
                locator.press_sequentially(expected_value, delay=35)
            except Exception:
                pass
        retained_value = (_workday_locator_value(locator) or "").strip()
        if retained_value != expected_value:
            try:
                locator.evaluate(
                    """(input, nextValue) => {
                        if (!input) return null;
                        input.focus();
                        const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                        if (descriptor && descriptor.set) {
                            descriptor.set.call(input, nextValue);
                        } else {
                            input.value = nextValue;
                        }
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        return input.value;
                    }""",
                    expected_value,
                )
            except Exception:
                return False
        retained_value = (_workday_locator_value(locator) or "").strip()
        if retained_value != expected_value:
            return False
        try:
            locator.press(advance_key)
        except Exception:
            if advance_key != "Tab":
                return False
            try:
                locator.evaluate(
                    """(input) => {
                        if (!input) return null;
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        input.blur();
                        return input.value;
                    }"""
                )
            except Exception:
                return False
    return all(
        (_workday_locator_value(locator) or "").strip() == expected_value for locator, expected_value, _ in segments
    )


def _fill_workday_segmented_date_scope(scope, value: str) -> bool:
    parsed_value = _workday_parse_date_answer(value)
    if parsed_value is None:
        return False
    month_value, day_value, year_value = parsed_value
    segments = (
        (_workday_question_date_field(scope, "Month"), month_value),
        (_workday_question_date_field(scope, "Day"), day_value),
        (_workday_question_date_field(scope, "Year"), year_value),
    )
    needs_refresh = _workday_field_has_input_alert(scope)
    has_mismatch = False
    for locator, expected_value in segments:
        if not locator.count():
            return False
        current_value = (_workday_locator_value(locator) or "").strip()
        if current_value != expected_value:
            has_mismatch = True
    if not needs_refresh and not has_mismatch:
        return True
    return _fill_workday_segmented_date_scope_with_navigation(scope, month_value, day_value, year_value)


def _fill_workday_self_identify_date_scope(page, scope, value: str) -> bool:
    parsed_value = _workday_parse_date_answer(value)
    if parsed_value is None:
        return False
    month_value, day_value, year_value = parsed_value
    segments = (
        (_workday_question_date_field(scope, "Month"), month_value),
        (_workday_question_date_field(scope, "Day"), day_value),
        (_workday_question_date_field(scope, "Year"), year_value),
    )
    for locator, _expected_value in segments:
        if not locator.count():
            return False
    if not _focus_workday_date_segment(segments[0][0]):
        return False
    typed_value = f"{int(month_value):02d}{int(day_value):02d}{year_value}"
    try:
        page.keyboard.type(typed_value, delay=35)
    except Exception:
        return False
    try:
        page.wait_for_timeout(250)
    except Exception:
        pass
    try:
        page.keyboard.press("Tab")
    except Exception:
        try:
            segments[0][0].evaluate(
                """(input) => {
                    if (!input) return null;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    input.blur();
                    return input.value;
                }"""
            )
        except Exception:
            return False
    try:
        page.wait_for_timeout(250)
    except Exception:
        pass
    return all(
        (_workday_locator_value(locator) or "").strip() == expected_value for locator, expected_value in segments
    )


def _fill_workday_question_date(group, value: str) -> bool:
    return _fill_workday_segmented_date_scope(group, value)


def _workday_email_field(scope, *accessible_names: str, prefer_last_visible: bool = False):
    """Resolve the intended Workday email field when multiple inputs share email metadata."""
    return _workday_textbox_field(
        scope,
        *accessible_names,
        selectors=("[data-automation-id='email']", "input[type='email']"),
        prefer_last_visible=prefer_last_visible,
        exact=True,
    )


def _workday_password_field(scope, *accessible_names: str, prefer_last_visible: bool = False):
    """Resolve the intended Workday password field when multiple auth shells are present."""
    return _workday_textbox_field(
        scope,
        *accessible_names,
        selectors=("[data-automation-id='password']", "input[type='password']"),
        prefer_last_visible=prefer_last_visible,
        exact=True,
    )


def _workday_verify_password_field(scope, *accessible_names: str, prefer_last_visible: bool = False):
    return _workday_textbox_field(
        scope,
        *accessible_names,
        selectors=("[data-automation-id='verifyPassword']",),
        prefer_last_visible=prefer_last_visible,
    )


def _do_password_reset(page, email: str, new_password: str) -> bool:
    """Reset password via forgot-password flow + gws email."""
    # Navigate to the reset page if we're not already there.
    markers = _extract_workday_auth_markers(page)
    if markers.get("auth_state") != "password_reset_gate":
        forgot_btn = page.get_by_role("button", name="Forgot your password")
        if not forgot_btn.count():
            forgot_btn = page.locator("button:has-text('Forgot'), a:has-text('Forgot')")
        if not forgot_btn.count() and _open_workday_email_sign_in_entrypoint(page):
            forgot_btn = page.get_by_role("button", name="Forgot your password")
            if not forgot_btn.count():
                forgot_btn = page.locator("button:has-text('Forgot'), a:has-text('Forgot')")
        if not forgot_btn.count():
            print("Workday: cannot find forgot password link.", file=sys.stderr)
            return False

        forgot_btn.first.click(force=True)
        page.wait_for_timeout(3000)

    # Enter email for reset — find the visible Email Address field
    # On the Forgot Password page there may be multiple email inputs
    # (from the Sign In form behind it), so target the visible one.
    email_input = _workday_email_field(page, "Email Address", prefer_last_visible=True)

    if _workday_fill_if_visible(email_input, email):
        print(f"Workday: filled reset email field with {email}", file=sys.stderr)
    else:
        print("Workday: could not find email field on Forgot Password page.", file=sys.stderr)
        return False

    # Click Reset Password button (has click_filter overlay)
    reset_started_at = datetime.now(UTC)
    # Click Reset Password — try role-based first, then force-click
    reset_btn = page.get_by_role("button", name="Reset Password")
    if reset_btn.count():
        reset_btn.first.click(force=True)
    else:
        _click_workday_button(page, "[data-automation-id='resetPasswordButton'], button:has-text('Reset Password')")
    page.wait_for_timeout(3000)

    # Fetch reset link from Gmail
    # Workday reset links look like: https://<company>.wd<N>.myworkdayjobs.com/<site>/passwordreset/<token>/?...
    print("Workday: waiting for password reset email...", file=sys.stderr)
    reset_link = _fetch_workday_email_link(
        ["from:otp.workday.com reset password"],
        re.compile(r"https://[^\s\"'<>]+/passwordreset/[^\s\"'<>]+", re.I),
        min_received_at_utc=reset_started_at,
        wait_seconds=120,
    )
    if not reset_link:
        print("Workday: password reset email not found.", file=sys.stderr)
        return False

    # Navigate to reset link and set new password
    page.goto(reset_link, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(5000)

    new_pw_input = _workday_password_field(page, "Password", prefer_last_visible=True)
    verify_pw_input = _workday_verify_password_field(page, "Verify New Password", prefer_last_visible=True)

    if not _workday_fill_if_visible(new_pw_input, new_password):
        return False
    if not _workday_fill_if_visible(verify_pw_input, new_password):
        return False

    _click_workday_button(page, "button:has-text('Submit'), button:has-text('Change Password'), button[type='submit']")
    page.wait_for_timeout(5000)

    # After password change, Workday may show a confirmation page or redirect.
    # Check if there's a "Sign In" link/button to click, or navigate back.
    print("Workday: password reset submitted, attempting sign in...", file=sys.stderr)

    # If page shows "Password has been reset" or similar, look for Sign In link
    sign_in_link = page.locator("a:has-text('Sign In'), button:has-text('Sign In')")
    if sign_in_link.count():
        sign_in_link.first.click(force=True)
        page.wait_for_timeout(3000)

    # Try sign in on current page
    if _do_sign_in(page, email, new_password):
        return True

    # If sign-in failed, navigate back to the original login URL from browser history
    # The page URL may still contain the original redirect path
    page.go_back()
    page.wait_for_timeout(3000)
    return _do_sign_in(page, email, new_password)


def _complete_workday_account_verification(
    page,
    email: str,
    password: str,
    *,
    verification_started_at: datetime,
    preferred_job_url: str = "",
) -> bool:
    print("Workday: waiting for account verification email...", file=sys.stderr)
    verification_link = _fetch_workday_account_verification_link(
        min_received_at_utc=verification_started_at,
        wait_seconds=120,
        preferred_job_url=preferred_job_url,
    )
    if not verification_link:
        resend_verification = page.get_by_role("button", name="Resend Account Verification")
        if not resend_verification.count():
            resend_verification = page.locator(
                "button:has-text('Resend Account Verification'), a:has-text('Resend Account Verification')"
            )
        if resend_verification.count():
            resend_verification.first.click(force=True)
            page.wait_for_timeout(5000)
            print("Workday: resent account verification email, checking again...", file=sys.stderr)
            verification_link = _fetch_workday_account_verification_link(
                min_received_at_utc=datetime.now(UTC),
                wait_seconds=120,
                preferred_job_url=preferred_job_url,
            )
    if not verification_link:
        print("Workday: account verification email not found.", file=sys.stderr)
        return False
    page.goto(verification_link, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(5000)

    if _is_application_page(page):
        return True
    if "/userhome" in page.url.lower():
        print("Workday: account verified (redirected to userHome).", file=sys.stderr)
        return True

    post_verification_markers = _extract_workday_auth_markers(page)
    if post_verification_markers.get("auth_state") == "sign_in_gate":
        print("Workday: account verification returned to sign in, attempting fresh sign in...", file=sys.stderr)
        return _do_sign_in(page, email, password)
    return False


def _do_create_account(page, email: str, password: str, *, job_url: str = "") -> bool:
    """Create a new Workday account.

    The form has: Email Address*, Password*, Verify New Password*,
    terms checkbox, and Create Account button.  There is also a
    honeypot field ("Enter website") that must NOT be filled.
    """
    email_input = _workday_email_field(page, "Email Address", prefer_last_visible=True)
    password_input = _workday_password_field(page, "Password", prefer_last_visible=True)
    verify_password = _workday_verify_password_field(page, "Verify New Password", prefer_last_visible=True)

    if not _workday_fill_if_visible(email_input, email):
        return False
    if not _workday_fill_if_visible(password_input, password):
        return False
    if not _workday_fill_if_visible(verify_password, password):
        return False

    # Accept terms checkbox
    terms_checkbox = _workday_preferred_visible_checkbox(
        page.get_by_role("checkbox", name="terms and conditions"),
        prefer_last_visible=True,
    )
    if not terms_checkbox.count():
        terms_checkbox = _workday_preferred_visible_checkbox(
            page.locator("input[type='checkbox']"),
            prefer_last_visible=True,
        )
    if terms_checkbox.count():
        try:
            if terms_checkbox.is_visible() and not terms_checkbox.is_checked():
                terms_checkbox.check()
        except Exception:
            pass

    # Do NOT fill the honeypot field ("Enter website. This input is for robots only")

    create_started_at = datetime.now(UTC)
    create_btn = page.get_by_role("button", name="Create Account").first
    if create_btn.count():
        create_btn.click()
        page.wait_for_timeout(5000)

    # Check if account creation needs email verification
    verify_input = page.get_by_role("textbox", name="Verification Code")
    if not verify_input.count():
        verify_input = page.locator("input[placeholder*='erification']").first
    if verify_input.count():
        print("Workday: waiting for verification code email...", file=sys.stderr)
        code = _fetch_workday_verification_code(
            min_received_at_utc=create_started_at,
            wait_seconds=120,
        )
        if code:
            verify_input.fill(code)
            verify_btn = page.get_by_role("button", name="Verify")
            if not verify_btn.count():
                verify_btn = page.get_by_role("button", name="Submit").first
            if verify_btn.count():
                verify_btn.click()
                page.wait_for_timeout(5000)
        else:
            print("Workday: verification code not found in email.", file=sys.stderr)
            return False

    page.wait_for_timeout(3000)
    post_create_markers = _extract_workday_auth_markers(page)
    if post_create_markers.get("auth_state") == "account_verification_gate":
        return _complete_workday_account_verification(
            page,
            email,
            password,
            verification_started_at=create_started_at,
            preferred_job_url=job_url,
        )

    if _is_application_page(page):
        return True

    # Account creation may have succeeded but Workday redirected to userHome
    if "/userhome" in page.url.lower():
        print("Workday: account created (redirected to userHome).", file=sys.stderr)
        return True

    if post_create_markers.get("auth_state") == "sign_in_gate":
        print("Workday: create account returned to sign in, attempting fresh sign in...", file=sys.stderr)
        if _do_sign_in(page, email, password):
            return True
        post_sign_in_markers = _extract_workday_auth_markers(page)
        if post_sign_in_markers.get("auth_state") == "account_verification_gate":
            return _complete_workday_account_verification(
                page,
                email,
                password,
                verification_started_at=create_started_at,
                preferred_job_url=job_url,
            )
        return False

    return False


def _is_application_page(page) -> bool:
    """Check if we're on a Workday application form page (past Create Account/Sign In).

    Must not match the Create Account/Sign In page — those pages also show
    progress bar labels like "My Information" which can cause false positives.
    """
    # Negative checks: still on auth pages
    if page.locator("h2:has-text('Create Account')").count() > 0:
        return False
    if page.locator("h2:has-text('Sign In')").count() > 0:
        return False
    if page.locator("h2:has-text('Reset Password')").count() > 0:
        return False
    if page.locator("h2:has-text('Forgot Password')").count() > 0:
        return False
    if page.get_by_role("textbox", name="Verify New Password").count() > 0:
        return False
    if _has_visible_workday_auth_shell(page):
        return False
    try:
        body_text = page.inner_text("body")[:5000]
    except Exception:
        body_text = ""
    heading_text = _workday_heading_text(page)
    has_form_controls = (
        page.get_by_role("button", name="Save and Continue").count() > 0
        or page.get_by_role("button", name="Submit").count() > 0
        or page.get_by_role("button", name="Next").count() > 0
        or page.get_by_role("textbox", name="First Name").count() > 0
        or page.locator("input[type='file']").count() > 0
    )
    url = page.url.lower()
    if _looks_like_workday_authenticated_non_form(
        page_url=getattr(page, "url", "") or "",
        page_text=body_text,
        heading_text=heading_text,
    ) and not (has_form_controls and (_is_workday_apply_url(url) or "current step" in body_text.casefold())):
        return False

    # Apply flows usually live under /apply/, but some public Workday forms
    # use shells like /introduceYourself while still exposing the real form.
    if not _is_workday_apply_url(url) and not has_form_controls:
        return False

    # Positive check: the logged-in user is visible in the header
    if page.locator("button:has-text('jerrisonli@gmail.com'), button:has-text('Settings')").count() > 0:
        # And an active h2 heading for a form step exists
        main = page.locator("main")
        if main.count():
            h2s = main.locator("h2")
            if h2s.count() > 0:
                return True

    return has_form_controls


# ─── Page detection ──────────────────────────────────────────────────────────

PAGE_CREATE_ACCOUNT = "create_account"
PAGE_MY_INFO = "my_information"
PAGE_EXPERIENCE = "my_experience"
PAGE_APPLICATION_QUESTIONS = "application_questions"
PAGE_VOLUNTARY_DISCLOSURES = "voluntary_disclosures"
PAGE_SELF_IDENTIFY = "self_identify"
PAGE_REVIEW = "review"
PAGE_UNKNOWN = "unknown"

_WORKDAY_PUBLIC_PROFILE_HEADING_FRAGMENTS = (
    "apply for future opportunities",
    "future opportunities",
    "introduce yourself",
    "submit a resume for future consideration",
    "join our talent community",
)
_WORKDAY_PUBLIC_PROFILE_BODY_MARKERS = (
    "first name",
    "last name",
    "email",
    "phone number",
    "resume/cv upload",
    "upload either doc",
)


def _has_visible_workday_auth_shell(page) -> bool:
    auth_shell_selectors = (
        "[data-automation-id='signInContent']",
        "[data-automation-id='signInSubmitButton']",
        "[data-automation-id='createAccountLink']",
        "[data-automation-id='forgotPasswordLink']",
        "[data-automation-id='verifyPassword']",
        "button:has-text('Resend Account Verification')",
        "a:has-text('Resend Account Verification')",
        "button:has-text('Reset Password')",
        "a:has-text('Reset Password')",
        "h3:has-text('Sign In')",
        "h3:has-text('Create Account')",
        "h3:has-text('Reset Password')",
        "h3:has-text('Forgot Password')",
    )
    for selector in auth_shell_selectors:
        try:
            locator = page.locator(selector)
            if not locator.count():
                continue
        except Exception:
            continue
        try:
            if locator.first.is_visible():
                return True
        except Exception:
            return True
    return False


def _has_visible_workday_sign_in_form(page) -> bool:
    sign_in_selectors = (
        "[data-automation-id='signInContent']",
        "[data-automation-id='signInSubmitButton']",
        "h3:has-text('Sign In')",
    )
    for selector in sign_in_selectors:
        try:
            locator = page.locator(selector)
            if not locator.count():
                continue
        except Exception:
            continue
        try:
            if locator.first.is_visible():
                return True
        except Exception:
            return True
    return False


def _has_visible_workday_create_account_form(page) -> bool:
    try:
        verify_password = page.get_by_role("textbox", name="Verify New Password")
        if verify_password.count():
            return True
    except Exception:
        pass

    create_account_selectors = (
        "[data-automation-id='verifyPassword']",
        "h2:has-text('Create Account')",
        "h3:has-text('Create Account')",
    )
    for selector in create_account_selectors:
        try:
            locator = page.locator(selector)
            if not locator.count():
                continue
        except Exception:
            continue
        try:
            if locator.first.is_visible():
                return True
        except Exception:
            return True
    return False


def _open_workday_email_sign_in_entrypoint(page) -> bool:
    entrypoint = page.get_by_role("button", name="Sign in with email")
    if not entrypoint.count():
        entrypoint = page.locator("button:has-text('Sign in with email'), a:has-text('Sign in with email')")
    if not entrypoint.count():
        return False
    try:
        entrypoint.first.click(force=True)
    except Exception:
        try:
            entrypoint.first.click()
        except Exception:
            return False
    page.wait_for_timeout(3000)
    return True


def _workday_heading_text(page) -> str:
    main = page.locator("main")
    if main.count():
        headings_locator = main.locator("h2, h3")
    else:
        headings_locator = page.locator("h2, h3")
    headings: list[str] = []
    for idx in range(min(headings_locator.count(), 8)):
        try:
            target = headings_locator.nth(idx)
            if not target.is_visible():
                continue
            heading = re.sub(r"\s+", " ", target.inner_text()).strip().lower()
        except Exception:
            continue
        if heading:
            headings.append(heading)
    return " ".join(headings)


def _is_workday_review_shell(page) -> bool:
    """Fail closed when Workday exposes a review shell or live submit control."""
    review_root_selectors = (
        "[data-automation-id='applyFlowReviewPage']",
        "[data-automation-id='reviewPage']",
    )
    for selector in review_root_selectors:
        locator = page.locator(selector)
        if not locator.count():
            continue
        try:
            if locator.first.is_visible():
                return True
        except Exception:
            return True

    submit_locators = (
        page.locator("button[data-automation-id='submitButton']"),
        page.locator("[data-automation-id='bottom-navigation-next-button']"),
        page.get_by_role("button", name="Submit"),
        page.get_by_role("button", name="Submit Application"),
    )
    has_visible_submit = False
    for locator in submit_locators:
        for idx in range(locator.count()):
            target = locator.nth(idx)
            try:
                if not target.is_visible():
                    continue
            except Exception:
                continue
            try:
                button_text = re.sub(r"\s+", " ", target.inner_text()).strip().casefold()
            except Exception:
                button_text = ""
            try:
                aria_label = re.sub(r"\s+", " ", target.get_attribute("aria-label") or "").strip().casefold()
            except Exception:
                aria_label = ""
            if "submit" in " ".join(part for part in (button_text, aria_label) if part):
                has_visible_submit = True
                break
        if has_visible_submit:
            break
    if not has_visible_submit:
        return False

    heading_text = _workday_heading_text(page)
    if "review" in heading_text:
        return True
    try:
        body_text = page.inner_text("body")[:5000].lower()
    except Exception:
        body_text = ""
    return any(marker in body_text for marker in ("review and submit", "review application", "submit application"))


def _looks_like_workday_public_profile_form(*, heading_text: str, body_text: str) -> bool:
    combined = " ".join(part for part in (heading_text, body_text) if part)
    if not any(fragment in combined for fragment in _WORKDAY_PUBLIC_PROFILE_HEADING_FRAGMENTS):
        return False
    return any(marker in combined for marker in _WORKDAY_PUBLIC_PROFILE_BODY_MARKERS)


def _detect_current_page(page) -> str:
    """Detect which Workday wizard page is currently active."""
    # Check for auth pages first (Create Account, Sign In dialog)
    dialog = page.locator("dialog, [role='dialog']")
    if dialog.count():
        dialog_text = dialog.first.inner_text()[:500].lower()
        if "sign in" in dialog_text:
            return PAGE_CREATE_ACCOUNT  # Sign In modal on Create Account page
    if page.locator("h2:has-text('Create Account')").count() > 0:
        return PAGE_CREATE_ACCOUNT
    if page.get_by_role("textbox", name="Verify New Password").count() > 0:
        return PAGE_CREATE_ACCOUNT
    if _has_visible_workday_auth_shell(page):
        return PAGE_CREATE_ACCOUNT

    body_text = page.inner_text("body")[:5000].lower()

    # Check headings that are NOT inside the progress bar (progress bar uses
    # listitem elements, actual page headings use h2/h3 inside main content)
    heading_text = _workday_heading_text(page)

    if _is_workday_review_shell(page):
        return PAGE_REVIEW
    if _looks_like_workday_public_profile_form(heading_text=heading_text, body_text=body_text):
        return PAGE_MY_INFO

    if "my information" in heading_text or "source" in heading_text:
        return PAGE_MY_INFO
    if "my experience" in heading_text or "resume" in heading_text:
        return PAGE_EXPERIENCE
    if "application questions" in heading_text or "questionnaire" in heading_text:
        return PAGE_APPLICATION_QUESTIONS
    if "self identify" in heading_text or "self-identify" in heading_text:
        return PAGE_SELF_IDENTIFY
    if "voluntary" in heading_text or "disclosures" in heading_text:
        return PAGE_VOLUNTARY_DISCLOSURES
    if "review" in heading_text:
        return PAGE_REVIEW

    # Fall back to body text — but exclude progress bar labels
    # by checking for form-specific content
    if "how did you hear" in body_text or "country" in body_text and "phone" in body_text:
        return PAGE_MY_INFO
    if "upload" in body_text and "resume" in body_text:
        return PAGE_EXPERIENCE
    if "self identify" in body_text or "self-identify" in body_text:
        return PAGE_SELF_IDENTIFY
    if "voluntary" in body_text and "disclosures" in body_text:
        return PAGE_VOLUNTARY_DISCLOSURES
    if "review" in body_text and "submit" in body_text:
        return PAGE_REVIEW
    return PAGE_UNKNOWN


# ─── Form filling helpers ────────────────────────────────────────────────────


def _fill_text_field(page, selector: str, value: str) -> bool:
    """Fill a text field found by selector."""
    field = page.locator(selector).first
    if not field.count():
        return False
    try:
        field.click()
        field.fill("")
        human_fill(field, value)
        return True
    except Exception:
        return False


def _workday_attr_selector(attribute: str, value: str) -> str:
    return f"[{attribute}={json.dumps(str(value))}]"


def _workday_dom_id_selector(dom_id: str) -> str:
    return _workday_attr_selector("id", dom_id)


def _workday_label_for_selector(control_id: str) -> str:
    return f"label[for={json.dumps(str(control_id))}]"


_WORKDAY_VETERAN_STATUS_LABEL_CANDIDATES = (
    "Please identify your veteran status.",
    "Please select your veteran status.",
    "What is your veteran status?",
    "Veteran Status",
    "Veterans Status",
    "Are you a U.S. Veteran?",
    "Please select the veteran status which most accurately describes your status.",
)
_WORKDAY_GENDER_LABEL_CANDIDATES = (
    "Gender",
    "Please select your gender.",
    "What is your gender?",
)
_WORKDAY_HISPANIC_LABEL_CANDIDATES = (
    "Hispanic or Latino?",
    "Hispanic or Latino",
)
_WORKDAY_RACE_LABEL_CANDIDATES = (
    "Race/Ethnicity",
    "Ethnicity",
    "Ethnicity (select all that apply)",
    "Please select the appropriate value(s)",
    "Please select your ethnicity.",
    "Please select your race.",
    "Please select the ethnicity (or ethnicities) which most accurately describe(s) how you identify yourself.",
    "What is your race?",
)
_WORKDAY_RADIO_OR_CHECKBOX_PROFILE_FIELDS = {
    "gender": "gender",
    "race": "race_or_ethnicity",
    "veteran": "veteran_status",
    "disability": "disability_status",
    "pronouns": "pronouns",
}

_WORKDAY_ACKNOWLEDGMENT_CHECKBOX_LABEL_FRAGMENTS = (
    "confirm the statement above",
    "confirm the statements above",
    "read and acknowledge",
    "read and agree",
    "i acknowledge that i have read and understand",
    "accommodations information above",
    "i certify that my answers",
    "i willingly accept",
    "candidate privacy statement",
    "candidate privacy notice",
    "privacy statement",
    "privacy notice",
    "terms and conditions",
    "terms & conditions",
)


def _fill_workday_dropdown(page, selector: str, value: str, *, profile_field: str | None = None) -> bool:
    """Fill a Workday custom dropdown by clicking to open, then selecting an option."""
    dropdown = page.locator(selector).first
    return _fill_workday_dropdown_locator(page, dropdown, value, profile_field=profile_field)


def _fill_workday_labeled_dropdown_candidates(
    page,
    label_candidates: tuple[str, ...],
    value: str,
    *,
    profile_field: str | None = None,
) -> bool:
    for label_text in label_candidates:
        if _fill_workday_labeled_dropdown(page, label_text, value, profile_field=profile_field):
            return True
    return False


def _fill_workday_labeled_checkbox_group(
    page, label_text: str, value: str, *, profile_field: str | None = None
) -> str | None:
    try:
        group = page.locator("[data-automation-id^='formField-']").filter(has_text=label_text).first
    except Exception:
        return None
    if not group.count():
        return None

    option_labels = group.locator("label[for]")
    count = option_labels.count()
    option_entries: list[tuple[str, object, str]] = []
    visible_option_texts: list[str] = []
    for index in range(count):
        label = option_labels.nth(index)
        try:
            if not label.is_visible():
                continue
        except Exception:
            continue
        text = label.inner_text().strip()
        input_id = (label.get_attribute("for") or "").strip()
        if not text or not input_id:
            continue
        visible_option_texts.append(text)
        option_entries.append((text, label, input_id))

    desired_value = value
    if profile_field and visible_option_texts:
        matched_profile_option = select_profile_option(
            visible_option_texts,
            value,
            profile_field=profile_field,
        )
        if matched_profile_option:
            desired_value = matched_profile_option

    for text, label, input_id in option_entries:
        if not _workday_option_text_matches(desired_value, text):
            continue
        checkbox = group.locator(_workday_dom_id_selector(input_id)).first
        try:
            if checkbox.count() and checkbox.is_checked():
                return text
        except Exception:
            pass
        try:
            label.scroll_into_view_if_needed()
            label.click()
            page.wait_for_timeout(300)
        except Exception:
            continue
        try:
            if checkbox.count() and checkbox.is_checked():
                return text
        except Exception:
            pass
        try:
            if (checkbox.get_attribute("aria-checked") or "").strip().casefold() == "true":
                return text
        except Exception:
            pass
    return None


def _fill_workday_labeled_checkbox_group_candidates(
    page,
    label_candidates: tuple[str, ...],
    value: str,
    *,
    profile_field: str | None = None,
) -> str | None:
    for label_text in label_candidates:
        selected_value = _fill_workday_labeled_checkbox_group(
            page,
            label_text,
            value,
            profile_field=profile_field,
        )
        if selected_value:
            return selected_value
    return None


def _normalize_workday_option_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _workday_option_text_matches(desired: str, option_text: str) -> bool:
    desired_norm = _normalize_workday_option_text(desired)
    option_norm = _normalize_workday_option_text(option_text)
    if not desired_norm or not option_norm:
        return False
    if option_norm == desired_norm:
        return True
    if len(desired_norm.split()) == 1:
        return any(option_norm.startswith(desired_norm + suffix) for suffix in (" ",))
    return option_norm.startswith(desired_norm)


def _workday_dropdown_descriptor(dropdown) -> str:
    parts: list[str] = []
    for attribute in ("id", "aria-label"):
        try:
            value = re.sub(r"\s+", " ", str(dropdown.get_attribute(attribute) or "")).strip()
        except Exception:
            value = ""
        if value:
            parts.append(value)
    try:
        text = re.sub(r"\s+", " ", dropdown.inner_text()).strip()
    except Exception:
        text = ""
    if text:
        parts.append(text)
    return " ".join(parts)


def _looks_like_workday_degree_dropdown(dropdown) -> bool:
    descriptor = _workday_dropdown_descriptor(dropdown).casefold()
    return "degree" in descriptor


def _workday_dropdown_value_candidates(dropdown, value: str) -> list[str]:
    candidates = [re.sub(r"\s+", " ", str(value or "")).strip()]
    if _looks_like_workday_degree_dropdown(dropdown):
        candidates.extend(_workday_education_degree_candidates(str(value or "")))
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        key = candidate.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _workday_dropdown_selection_matches(dropdown, value: str) -> bool:
    try:
        button_text = re.sub(r"\s+", " ", dropdown.inner_text()).strip()
    except Exception:
        button_text = ""
    try:
        aria_label = re.sub(r"\s+", " ", dropdown.get_attribute("aria-label") or "").strip()
    except Exception:
        aria_label = ""
    combined = " ".join(part for part in (button_text, aria_label) if part)
    combined_norm = combined.casefold()
    return bool(combined and _workday_option_text_matches(value, combined) and "select one" not in combined_norm)


def _workday_dropdown_has_selected_value(dropdown) -> bool:
    try:
        button_text = re.sub(r"\s+", " ", dropdown.inner_text()).strip()
    except Exception:
        button_text = ""
    try:
        aria_label = re.sub(r"\s+", " ", dropdown.get_attribute("aria-label") or "").strip()
    except Exception:
        aria_label = ""
    combined = " ".join(part for part in (button_text, aria_label) if part).casefold()
    return bool(combined and "select one" not in combined)


def _workday_option_is_selected_value_chip(option) -> bool:
    try:
        data_automation_id = str(option.get_attribute("data-automation-id") or "").strip()
    except Exception:
        data_automation_id = ""
    if data_automation_id == "selectedItem":
        return True
    try:
        role = str(option.get_attribute("role") or "").strip()
    except Exception:
        role = ""
    if data_automation_id == "menuItem" and role == "presentation":
        return True
    try:
        return bool(
            option.evaluate(
                """(node) => Boolean(
                    node?.closest?.('[data-automation-id="selectedItemList"]') ||
                    node?.closest?.('[data-automation-id="selectedItem"]')
                )"""
            )
        )
    except Exception:
        return False


def _workday_visible_prompt_options(page) -> list[tuple[str, object]]:
    options = page.locator(", ".join(_WORKDAY_PROMPT_OPTION_SELECTORS))
    option_entries: list[tuple[str, object]] = []
    seen_texts: set[str] = set()
    for index in range(options.count()):
        option = options.nth(index)
        if _workday_option_is_selected_value_chip(option):
            continue
        try:
            is_visible = option.is_visible()
        except Exception:
            is_visible = True
        if not is_visible:
            continue
        try:
            text = re.sub(r"\s+", " ", option.inner_text()).strip()
        except Exception:
            continue
        if not text or text in seen_texts:
            continue
        seen_texts.add(text)
        option_entries.append((text, option))
    return option_entries


def _fill_workday_dropdown_locator(page, dropdown, value: str, *, profile_field: str | None = None) -> bool:
    """Fill a Workday custom dropdown from an existing locator."""
    if not dropdown.count():
        return False
    try:
        desired_values = _workday_dropdown_value_candidates(dropdown, value)
        visible_option_texts: list[str] = []
        for _attempt in range(3):
            if any(_workday_dropdown_selection_matches(dropdown, candidate) for candidate in desired_values):
                return True
            dropdown.scroll_into_view_if_needed()
            dropdown.click()
            page.wait_for_timeout(500)
            option_entries = _workday_visible_prompt_options(page)
            visible_option_texts = [text for text, _ in option_entries]

            if profile_field and visible_option_texts:
                matched_profile_option = select_profile_option(
                    visible_option_texts,
                    value,
                    profile_field=profile_field,
                )
                if matched_profile_option:
                    desired_values = [matched_profile_option, *[
                        candidate for candidate in desired_values if candidate.casefold() != matched_profile_option.casefold()
                    ]]

            matched_option = False
            for desired_value in desired_values:
                for text, opt in option_entries:
                    if not _workday_option_text_matches(desired_value, text):
                        continue
                    opt.click()
                    page.wait_for_timeout(300)
                    if any(_workday_dropdown_selection_matches(dropdown, candidate) for candidate in desired_values):
                        return True
                    matched_option = True
                    break
                if matched_option:
                    break

            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            try:
                page.wait_for_timeout(200)
            except Exception:
                pass
        try:
            dropdown_label = re.sub(r"\s+", " ", dropdown.get_attribute("aria-label") or "").strip()
        except Exception:
            dropdown_label = ""
        print(
            f"Workday dropdown: failed to select '{value}'. "
            f"Button='{dropdown_label or dropdown.inner_text()}' options={visible_option_texts}",
            file=sys.stderr,
        )
        return False
    except Exception:
        return False


def _fill_workday_radio_group(group, value: str) -> bool:
    labels = group.locator("label").filter(has_text=value)
    if not labels.count():
        return False
    try:
        labels.first.click()
        return True
    except Exception:
        return False


def _workday_radio_question_labels(page) -> list[str]:
    labels = page.evaluate(
        """() => {
            const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
            const texts = [];
            for (const fieldset of document.querySelectorAll('fieldset')) {
                const legend = fieldset.querySelector('legend');
                const text = normalize(legend?.textContent || '');
                if (text) texts.push(text);
            }
            return texts;
        }"""
    )
    if not isinstance(labels, list):
        return []
    return [str(label).strip() for label in labels if str(label).strip()]


def _workday_field_id_for_label(page, label_text: str) -> str | None:
    field_id = page.evaluate(
        """(labelText) => {
            const normalize = (value) => String(value || '')
                .toLowerCase()
                .replace(/[^a-z0-9]+/g, ' ')
                .trim();
            const target = normalize(labelText);
            const labels = Array.from(document.querySelectorAll('label'));
            for (const label of labels) {
                if (!normalize(label.textContent).includes(target)) continue;
                const fieldId = label.getAttribute('for');
                if (fieldId) return fieldId;
            }
            return null;
        }""",
        label_text,
    )
    return str(field_id).strip() if field_id else None


def _fill_workday_prompt_field(page, label_text: str, candidates: Sequence[str]) -> str | None:
    field_id = _workday_field_id_for_label(page, label_text)
    if not field_id:
        return None
    input_selector = _workday_dom_id_selector(field_id)
    seen: set[str] = set()
    normalized_candidates: list[str] = []
    for candidate in candidates:
        normalized_candidate = re.sub(r"\s+", " ", str(candidate or "")).strip()
        if not normalized_candidate:
            continue
        key = normalized_candidate.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized_candidates.append(normalized_candidate)
    existing_state = _workday_prompt_state_for_label(page, label_text)
    for candidate in normalized_candidates:
        selected_value = _workday_prompt_matching_value(
            page,
            label_text,
            candidate,
            state=existing_state,
        )
        if selected_value:
            return selected_value
    for candidate in normalized_candidates:
        if not _select_workday_prompt_option_via_input(page, input_selector, candidate, label_text=label_text):
            continue
        state = _workday_prompt_state_for_label(page, label_text)
        selected_value = _workday_prompt_matching_value(page, label_text, candidate, state=state)
        if selected_value:
            return selected_value
        _clear_mismatched_workday_prompt_selection(page, label_text, candidate, state=state)
    return None


def _fill_workday_prompt_field_candidates(
    page,
    label_candidates: Sequence[str],
    candidates: Sequence[str],
) -> str | None:
    for label_text in label_candidates:
        selected_value = _fill_workday_prompt_field(page, label_text, candidates)
        if selected_value:
            return selected_value
    return None


def _fill_workday_source_prompt(page, label_text: str, candidates: Sequence[str]) -> str | None:
    return _fill_workday_prompt_field(page, label_text, candidates)


def _fill_workday_labeled_radio(page, label_text: str, value: str) -> bool:
    target_label = normalize_text(label_text)
    target_value = normalize_text(value)
    if not target_label or not target_value:
        return False

    try:
        fieldsets = page.locator("fieldset")
        fieldset_count = fieldsets.count()
    except Exception:
        return False

    def _attribute_is_true(locator, name: str) -> bool:
        try:
            value = locator.get_attribute(name) or ""
        except Exception:
            return False
        return value.strip().casefold() == "true"

    def _radio_selected(input_locator) -> bool:
        if not input_locator.count():
            return False
        try:
            if input_locator.is_checked():
                return True
        except Exception:
            pass
        return _attribute_is_true(input_locator, "aria-checked")

    def _verify_selected(input_locator, fieldset_locator) -> bool:
        if not _radio_selected(input_locator):
            return False
        try:
            page.wait_for_timeout(250)
        except Exception:
            pass
        if _radio_selected(input_locator):
            return True
        if not fieldset_locator.count():
            return False
        return not _attribute_is_true(fieldset_locator, "aria-invalid")

    for index in range(fieldset_count):
        fieldset = fieldsets.nth(index)
        try:
            legend_text = normalize_text(fieldset.locator("legend").first.inner_text())
        except Exception:
            legend_text = ""
        if target_label not in legend_text:
            continue

        try:
            labels = fieldset.locator("label[for]")
            label_count = labels.count()
        except Exception:
            label_count = 0

        for label_index in range(label_count):
            option_label = labels.nth(label_index)
            try:
                option_text = normalize_text(option_label.inner_text())
            except Exception:
                option_text = ""
            if option_text != target_value:
                continue

            try:
                input_id = option_label.get_attribute("for") or ""
            except Exception:
                input_id = ""
            if not input_id:
                continue

            input_locator = fieldset.locator(_workday_dom_id_selector(input_id)).first
            if _verify_selected(input_locator, fieldset):
                return True

            click_targets = [option_label, input_locator]
            sibling_targets = (
                input_locator.locator("xpath=following-sibling::span[1]").first,
                input_locator.locator("xpath=following-sibling::div[1]").first,
                option_label.locator("xpath=ancestor::div[contains(@class,'css-1utp272')][1]").first,
            )
            click_targets.extend(sibling_targets)

            for target in click_targets:
                try:
                    if hasattr(target, "scroll_into_view_if_needed"):
                        target.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    target.click(timeout=3000)
                except Exception:
                    try:
                        target.click(force=True, timeout=3000)
                    except Exception:
                        continue
                if _verify_selected(input_locator, fieldset):
                    return True

            try:
                input_locator.check(force=True)
                if _verify_selected(input_locator, fieldset):
                    return True
            except Exception:
                pass

            for key in ("Space", "Enter"):
                try:
                    input_locator.focus()
                    page.keyboard.press(key)
                except Exception:
                    continue
                if _verify_selected(input_locator, fieldset):
                    return True

    return False


def _fill_workday_labeled_dropdown(page, label_text: str, value: str, *, profile_field: str | None = None) -> bool:
    field_id = _workday_field_id_for_label(page, label_text)
    if not field_id:
        return False
    return _fill_workday_dropdown(page, _workday_dom_id_selector(field_id), value, profile_field=profile_field)


def _check_workday_checkbox_for_label(page, label_text: str) -> bool:
    if _select_workday_checkbox_option(page, label_text):
        return True

    checked = page.evaluate(
        """(labelText) => {
            const normalize = (value) => String(value || '')
                .toLowerCase()
                .replace(/[^a-z0-9]+/g, ' ')
                .trim();
            const target = normalize(labelText);
            if (!target) return false;

            const isVisible = (node) => {
                if (!node || !(node instanceof Element)) return false;
                const style = window.getComputedStyle(node);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                const rect = node.getBoundingClientRect();
                return rect.width > 0 || rect.height > 0;
            };

            const seenScopes = new Set();
            const candidateScopes = [];
            const candidateInputs = [];
            const seenInputs = new Set();

            const addScope = (scope) => {
                if (!scope || !(scope instanceof Element) || seenScopes.has(scope)) return;
                seenScopes.add(scope);
                candidateScopes.push(scope);
            };

            const addInput = (input) => {
                if (
                    !(input instanceof HTMLInputElement) ||
                    input.type !== 'checkbox' ||
                    input.disabled ||
                    seenInputs.has(input)
                ) {
                    return;
                }
                if (!isVisible(input) && !isVisible(input.parentElement)) return;
                seenInputs.add(input);
                candidateInputs.push(input);
            };

            for (const element of document.querySelectorAll('label, span, div, p, li, legend')) {
                const text = normalize(element.textContent);
                if (!text || !text.includes(target)) continue;
                addScope(element);
                addScope(element.parentElement);
                addScope(element.closest('label'));
                addScope(element.closest('[data-automation-id^="formField-"]'));
                addScope(element.closest('[data-automation-id*="acknowledg"]'));
                addScope(element.closest('[role="row"]'));
                addScope(element.closest('fieldset'));

                let current = element.parentElement;
                for (let depth = 0; current && depth < 3; depth += 1) {
                    addScope(current);
                    current = current.parentElement;
                }
            }

            for (const scope of candidateScopes) {
                if (scope instanceof HTMLInputElement) addInput(scope);
                for (const input of scope.querySelectorAll('input[type="checkbox"]')) {
                    addInput(input);
                }
            }

            const toggle = (node) => {
                if (!node || !(node instanceof Element)) return false;
                try {
                    node.scrollIntoView({ block: 'center', inline: 'center' });
                } catch (_error) {
                    // no-op
                }
                for (const eventName of ['mousedown', 'mouseup', 'click']) {
                    try {
                        node.dispatchEvent(new MouseEvent(eventName, { bubbles: true, cancelable: true }));
                    } catch (_error) {
                        // no-op
                    }
                }
                return true;
            };

            for (const input of candidateInputs) {
                if (input.checked) return true;
                for (const node of [
                    input,
                    input.closest('label'),
                    input.parentElement,
                    input.previousElementSibling,
                    input.nextElementSibling,
                ]) {
                    toggle(node);
                    try {
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                    } catch (_error) {
                        // no-op
                    }
                    if (input.checked) return true;
                }
                try {
                    input.click();
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                } catch (_error) {
                    // no-op
                }
                if (input.checked) return true;
            }

            return false;
        }""",
        label_text,
    )
    if checked:
        try:
            page.wait_for_timeout(300)
        except Exception:
            pass
    return bool(checked)


def _check_workday_acknowledgment_checkbox(page) -> bool:
    for label_text in _WORKDAY_ACKNOWLEDGMENT_CHECKBOX_LABEL_FRAGMENTS:
        if _check_workday_checkbox_for_label(page, label_text):
            return True
    return False


def _select_workday_checkbox_option(page, label_text: str) -> bool:
    label = page.locator("label").filter(has_text=label_text).first
    if not label.count():
        return False
    input_id = ""
    try:
        input_id = label.get_attribute("for") or ""
    except Exception:
        input_id = ""
    if input_id:
        checkbox = page.locator(_workday_dom_id_selector(input_id)).first
        fieldset = checkbox.locator("xpath=ancestor::fieldset[1]").first
        faux_box = checkbox.locator("xpath=following-sibling::span[1]").first
        faux_indicator = checkbox.locator("xpath=following-sibling::div[1]").first
        row = label.locator("xpath=ancestor::div[@role='row'][1]").first
        cell = label.locator("xpath=ancestor::div[@role='cell'][1]").first
        grid = checkbox.locator("xpath=ancestor::div[@role='grid'][1]").first

        def _field_cleared() -> bool:
            try:
                if not checkbox.count():
                    return False
                checked = checkbox.is_checked()
            except Exception:
                return False
            try:
                indicator_svg_count = faux_indicator.locator("svg").count() if faux_indicator.count() else 0
            except Exception:
                indicator_svg_count = 0
            try:
                indicator_class = (
                    faux_indicator.locator("div").first.get_attribute("class") or "" if faux_indicator.count() else ""
                )
            except Exception:
                indicator_class = ""
            visibly_checked = indicator_svg_count > 0 or (
                "css-wwg2k6" not in indicator_class if indicator_class else checked
            )
            try:
                aria_invalid = (
                    (fieldset.get_attribute("aria-invalid") or "").strip().casefold() if fieldset.count() else ""
                )
            except Exception:
                aria_invalid = ""
            if aria_invalid == "true":
                return False
            return checked and visibly_checked

        try:
            label.scroll_into_view_if_needed()
            label.click(timeout=3000)
            page.wait_for_timeout(300)
            if _field_cleared():
                return True
        except Exception:
            pass
        try:
            if checkbox.count():
                checkbox.click(force=True, timeout=3000)
                page.wait_for_timeout(300)
                if _field_cleared():
                    return True
        except Exception:
            pass
        try:
            if checkbox.count():
                checkbox.check(force=True)
                page.wait_for_timeout(300)
                if _field_cleared():
                    return True
        except Exception:
            pass
        for key in ("Space", "Enter"):
            try:
                if checkbox.count():
                    checkbox.focus()
                    page.keyboard.press(key)
                    page.wait_for_timeout(300)
                    if _field_cleared():
                        return True
            except Exception:
                continue
        for locator in (cell, row, faux_box, faux_indicator):
            try:
                if locator.count():
                    locator.click(force=True, timeout=3000)
                    page.wait_for_timeout(300)
                    if _field_cleared():
                        return True
            except Exception:
                continue
        try:
            if grid.count():
                option_labels = fieldset.locator("label.css-1ew7hmu")
                target_index = 0
                option_count = option_labels.count()
                for idx in range(option_count):
                    try:
                        if option_labels.nth(idx).inner_text().strip() == label_text:
                            target_index = idx
                            break
                    except Exception:
                        continue
                grid.focus()
                page.keyboard.press("Home")
                for _ in range(target_index):
                    page.keyboard.press("ArrowDown")
                page.keyboard.press("Space")
                page.wait_for_timeout(300)
                if _field_cleared():
                    return True
        except Exception:
            pass
        selected = page.evaluate(
            """(targetId) => {
                const input = document.getElementById(targetId);
                if (!input) return false;
                input.click();
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
                return input.checked;
            }""",
            input_id,
        )
        page.wait_for_timeout(300)
        if bool(selected) and _field_cleared():
            return True
        triggered = page.evaluate(
            """(targetId) => {
                const input = document.getElementById(targetId);
                if (!input) return false;
                const label = document.querySelector(`label[for="${targetId}"]`);
                const events = ['mousedown', 'mouseup', 'click'];
                for (const node of [label, input]) {
                    if (!node) continue;
                    for (const eventName of events) {
                        node.dispatchEvent(new MouseEvent(eventName, { bubbles: true, cancelable: true }));
                    }
                }
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
                return input.checked;
            }""",
            input_id,
        )
        page.wait_for_timeout(300)
        return bool(triggered) and _field_cleared()
    return False


def _fill_workday_date_segment(locator, value: str) -> bool:
    if not locator.count():
        return False
    try:
        locator.scroll_into_view_if_needed()
        locator.click(timeout=3000)
    except Exception:
        try:
            locator.click(force=True, timeout=3000)
        except Exception:
            locator.focus()
    for key in ("Meta+A", "Control+A"):
        try:
            locator.press(key)
        except Exception:
            pass
    try:
        locator.press("Backspace")
    except Exception:
        pass
    try:
        locator.fill("")
    except Exception:
        pass
    try:
        locator.type(value, delay=35)
    except Exception:
        try:
            locator.press_sequentially(value, delay=35)
        except Exception:
            pass

    if (_workday_locator_value(locator) or "").strip() == value.strip():
        try:
            locator.press("Tab")
        except Exception:
            try:
                locator.evaluate(
                    """(input) => {
                        if (!input) return null;
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        input.blur();
                        return input.value;
                    }"""
                )
            except Exception:
                return False
        return (_workday_locator_value(locator) or "").strip() == value.strip()

    try:
        locator.evaluate(
            """(input, nextValue) => {
                if (!input) return null;
                input.focus();
                const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                if (descriptor && descriptor.set) {
                    descriptor.set.call(input, nextValue);
                } else {
                    input.value = nextValue;
                }
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
                input.blur();
                return input.value;
            }""",
            value,
        )
    except Exception:
        return False
    return (_workday_locator_value(locator) or "").strip() == value.strip()


def _upload_file(page, selector: str, file_path: str) -> bool:
    """Upload a file to a file input."""
    file_input = page.locator(selector).first
    if not file_input.count():
        return False
    try:
        file_input.set_input_files(file_path)
        page.wait_for_timeout(2000)
        return True
    except Exception:
        return False


def _click_next_button(page) -> bool:
    """Click the Save and Continue / Next button.

    Workday uses "Save and Continue" as the primary navigation button,
    but some instances may use "Next" or "Continue". Do not ever
    let the generic next-step helper activate the final submit action.
    """
    # Try role-based first (most reliable with Workday's click_filter overlays)
    for name in ("Save and Continue", "Next", "Continue"):
        btn = page.get_by_role("button", name=name)
        for idx in range(btn.count()):
            target = btn.nth(idx)
            try:
                if not target.is_visible():
                    continue
            except Exception:
                continue
            try:
                target.scroll_into_view_if_needed()
                target.click(timeout=3000)
                page.wait_for_timeout(5000)
                return True
            except Exception:
                try:
                    target.click(force=True, timeout=3000)
                    page.wait_for_timeout(5000)
                    return True
                except Exception:
                    continue
    return False


def _click_submit_button(page) -> bool:
    """Click the Submit button on the review page."""
    submit_selectors = [
        "[data-automation-id='bottom-navigation-next-button']",
        "button[data-automation-id='submitButton']",
        "button:has-text('Submit')",
        "button:has-text('Submit Application')",
    ]
    for sel in submit_selectors:
        btn = page.locator(sel)
        if btn.count() and btn.first.is_visible():
            try:
                btn.first.click()
                page.wait_for_timeout(3000)
                return True
            except Exception:
                continue
    return False


def _has_visible_workday_submit_button(page) -> bool:
    submit_selectors = (
        "[data-automation-id='bottom-navigation-next-button']",
        "button[data-automation-id='submitButton']",
        "button:has-text('Submit')",
        "button:has-text('Submit Application')",
    )
    for selector in submit_selectors:
        locator = page.locator(selector)
        count = locator.count()
        for idx in range(count):
            candidate = locator.nth(idx)
            try:
                if candidate.is_visible():
                    return True
            except Exception:
                continue
    return False


def _is_workday_public_profile_submit_boundary(page) -> bool:
    try:
        body_text = page.inner_text("body")[:5000].lower()
    except Exception:
        body_text = ""
    heading_text = _workday_heading_text(page)
    return _looks_like_workday_public_profile_form(heading_text=heading_text, body_text=body_text) and (
        _has_visible_workday_submit_button(page)
    )


def _handle_workday_review_boundary(
    page,
    *,
    page_path: Path,
    payload: dict,
    filled_steps: list[dict],
    page_screenshots_dir: Path,
    submit: bool,
    headless: bool,
) -> int:
    _capture(page, page_path)
    pre_submit_path = Path(payload["artifacts"]["pre_submit_screenshot"])
    _write_workday_review_artifacts(
        payload,
        filled_steps=filled_steps,
        page_screenshots_dir=page_screenshots_dir,
    )

    if not submit:
        print(
            f"Workday: filled application for review: {pre_submit_path.relative_to(PROJECT_ROOT)}",
            file=sys.stderr,
        )
        return 0

    if not _click_submit_button(page):
        print("Workday: submit button not found on review page.", file=sys.stderr)
        return 1

    submit_started_at_utc = datetime.now(UTC).replace(microsecond=0).isoformat()
    email_watcher = build_email_confirmation_watcher(payload, min_received_at_utc=submit_started_at_utc)

    for _ in range(30):
        page.wait_for_timeout(500)
        snapshot = _page_snapshot_fn(page)
        email_confirmation = email_watcher.poll()
        if email_confirmation:
            outcome = {
                "status": "confirmed",
                "reason": "email_confirmation",
                "snapshot": snapshot,
                "email_confirmation": email_confirmation,
            }
            sync_notion_after_submit(
                payload,
                outcome,
                provider="workday",
                email_confirmation=email_confirmation,
                min_received_at_utc=submit_started_at_utc,
            )
            reply_to_confirmation_email(payload, board_name="workday", email_confirmation=email_confirmation)
            return 0
        state = _classify_submit_state(snapshot)
        if state["status"] == "confirmed":
            outcome = {"status": "confirmed", "reason": state.get("reason"), "snapshot": snapshot}
            sync_notion_after_submit(payload, outcome, provider="workday", min_received_at_utc=submit_started_at_utc)
            reply_to_confirmation_email(payload, board_name="workday")
            return 0
        if state["status"] == "captcha_required":
            print("Workday submission: captcha detected, waiting for resolution...", file=sys.stderr)
            _wait_result = wait_for_captcha_resolution(
                page,
                headless=headless,
                payload=payload,
                board_title="Workday",
                classify_state_fn=_classify_submit_state,
                page_snapshot_fn=_page_snapshot_fn,
                email_watcher=email_watcher,
                confirmed_outcome_from_email_fn=None,
                capture_fn=_capture,
                submit_started_at_utc=submit_started_at_utc,
            )
            if _wait_result["status"] == "confirmed":
                _outcome = _wait_result.get("outcome", {})
                _email_conf = _wait_result.get("email_confirmation")
                sync_notion_after_submit(
                    payload,
                    _outcome,
                    provider="workday",
                    email_confirmation=_email_conf,
                    min_received_at_utc=submit_started_at_utc,
                )
                reply_to_confirmation_email(payload, board_name="workday", email_confirmation=_email_conf)
                return 0
            return CAPTCHA_SKIP_EXIT_CODE
        if state["status"] == "validation_error":
            break

    email_confirmation = email_watcher.poll(force=True)
    if email_confirmation:
        outcome = {
            "status": "confirmed",
            "reason": "email_confirmation",
            "snapshot": _page_snapshot_fn(page),
            "email_confirmation": email_confirmation,
        }
        sync_notion_after_submit(
            payload,
            outcome,
            provider="workday",
            email_confirmation=email_confirmation,
            min_received_at_utc=submit_started_at_utc,
        )
        reply_to_confirmation_email(payload, board_name="workday", email_confirmation=email_confirmation)
        return 0

    debug_html = Path(payload["artifacts"]["submit_debug_html"])
    debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
    debug_html.write_text(page.content(), encoding="utf-8")
    _capture(page, debug_png)
    print(
        f"Workday submit did not reach confirmed state. "
        f"See {debug_html.relative_to(PROJECT_ROOT)} and {debug_png.relative_to(PROJECT_ROOT)}.",
        file=sys.stderr,
    )
    return 1


# ─── Page-specific fill functions ────────────────────────────────────────────


def _fill_my_information(page, profile, application_profile, payload: dict | None = None) -> list[dict]:
    """Fill the My Information page.

    Observed Workday fields (FactSet):
    - How Did You Hear About Us? (dropdown)
    - Are you a current or former employee? (radio Yes/No)
    - Country (dropdown, usually pre-filled)
    - First Name / Last Name (textbox)
    - Address Line 1 / City / State / Postal Code
    - Email (pre-filled from account)
    - Phone Number
    """
    filled = []
    source_hint = _workday_source_hint_from_payload(payload)
    source_answer = getattr(application_profile, "how_did_you_hear", "") or ""
    company_name = str((payload or {}).get("company") or "")

    def _fill_native_source_select(label_text: str, field_name: str) -> bool:
        result = page.evaluate(
            """({labelText, fieldName}) => {
                const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                const labels = Array.from(document.querySelectorAll('label'));
                for (const label of labels) {
                    if (!normalize(label.textContent).toLowerCase().includes(labelText.toLowerCase())) continue;
                    const targetId = label.getAttribute('for');
                    let select = null;
                    if (targetId) {
                        select = document.getElementById(targetId);
                    }
                    if (!select) {
                        const field = label.closest('.field, .form-group, [data-automation-id*="formField"]');
                        if (field) select = field.querySelector('select');
                    }
                    if (!select || select.tagName !== 'SELECT') continue;
                    const options = Array.from(select.options)
                        .map((option) => ({
                            value: option.value,
                            label: normalize(option.textContent),
                        }))
                        .filter((option) => option.value && option.label && option.label.toLowerCase() !== 'please select');
                    return {fieldName, options};
                }
                return null;
            }""",
            {"labelText": label_text, "fieldName": field_name},
        )
        if not result:
            return False
        options = [option.get("label", "") for option in result.get("options", []) if option.get("label")]
        selected = _preferred_workday_source_option(
            label_text,
            options,
            source_hint=source_hint,
            source_answer=source_answer,
            company_name=company_name,
        )
        if not selected:
            return False
        page.evaluate(
            """({labelText, selectedLabel}) => {
                const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                const labels = Array.from(document.querySelectorAll('label'));
                for (const label of labels) {
                    if (!normalize(label.textContent).toLowerCase().includes(labelText.toLowerCase())) continue;
                    const targetId = label.getAttribute('for');
                    let select = null;
                    if (targetId) {
                        select = document.getElementById(targetId);
                    }
                    if (!select) {
                        const field = label.closest('.field, .form-group, [data-automation-id*="formField"]');
                        if (field) select = field.querySelector('select');
                    }
                    if (!select || select.tagName !== 'SELECT') continue;
                    const match = Array.from(select.options).find(
                        (option) => normalize(option.textContent) === selectedLabel
                    );
                    if (!match) continue;
                    select.value = match.value;
                    select.dispatchEvent(new Event('input', { bubbles: true }));
                    select.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
                return false;
            }""",
            {"labelText": label_text, "selectedLabel": selected},
        )
        page.wait_for_timeout(500)
        filled.append({"field_name": field_name, "value": selected, "source": "deterministic", "filled": True})
        print(f"Workday: filled '{label_text}' native select with: {selected}", file=sys.stderr)
        return True

    def _fill_workday_source_field(label_text: str, field_name: str) -> bool:
        if _fill_native_source_select(label_text, field_name):
            return True
        candidates = _workday_source_search_candidates(
            label_text,
            [],
            source_hint=source_hint,
            source_answer=source_answer,
            company_name=company_name,
        )
        selected = _fill_workday_source_prompt(page, label_text, candidates)
        if not selected:
            return False
        filled.append({"field_name": field_name, "value": selected, "source": "deterministic", "filled": True})
        print(f"Workday: filled '{label_text}' prompt with: {selected}", file=sys.stderr)
        return True

    # How Did You Hear About Us (Workday multi-select combobox)
    # Workday renders this as a button that opens a popup with a search input
    source_filled = _fill_workday_source_field("How Did You Hear About Us", "source")
    if not source_filled:
        source_btn = page.get_by_role("button", name="How Did You Hear About Us")
        if not source_btn.count():
            # Try data-automation-id fallback
            source_btn = page.locator("[data-automation-id='sourcePrompt'], [data-automation-id='formField-source']")
        if not source_btn.count():
            # Try broader locators — Workday uses various patterns
            source_btn = page.locator("button:has-text('How Did You Hear'), [aria-label*='How Did You Hear']")
        if not source_btn.count():
            # Debug: dump all buttons to find the right one
            all_btns = page.evaluate("""() => {
                return Array.from(document.querySelectorAll('button, [role="button"], [data-automation-id]'))
                    .filter(el => el.textContent.includes('Hear') || el.textContent.includes('Source') ||
                           (el.getAttribute('data-automation-id') || '').includes('source'))
                    .map(el => ({tag: el.tagName, text: el.textContent.trim().substring(0, 50),
                                automationId: el.getAttribute('data-automation-id'),
                                ariaLabel: el.getAttribute('aria-label')}));
            }""")
            print(f"Workday: 'How Did You Hear' button not found. Nearby elements: {all_btns}", file=sys.stderr)
        if source_btn.count():
            print("Workday: found 'How Did You Hear' button, clicking...", file=sys.stderr)

            # The Workday combobox is complex. Instead of clicking the button
            # (which may be a label), find the actual interactive input element
            # inside the "How Did You Hear" field container and interact with it.
            source_open_state = page.evaluate("""() => {
                // Find the form field container for "How Did You Hear"
                const allFields = document.querySelectorAll('[data-automation-id*="formField"], [data-automation-id*="selectInputContainer"]');
                let fieldContainer = null;
                for (const f of allFields) {
                    if (f.textContent.includes('How Did You Hear')) {
                        fieldContainer = f;
                        break;
                    }
                }
                if (!fieldContainer) {
                    // Try broader: find label and get closest container
                    const labels = document.querySelectorAll('label');
                    for (const l of labels) {
                        if (l.textContent.includes('How Did You Hear')) {
                            fieldContainer = l.closest('[data-automation-id*="formField"]')
                                          || l.closest('.css-1dbjc4n')
                                          || l.parentElement;
                            break;
                        }
                    }
                }
                if (!fieldContainer) return false;

                // 1. Standard single-select dropdown (check FIRST — Workday
                //    single-selects also contain an internal <input type="text">
                //    which would incorrectly match the multi-select check below)
                const selectWidget = fieldContainer.querySelector(
                    '[data-automation-id="selectWidget"], ' +
                    '[data-automation-id="selectInputContainer"] button, ' +
                    'select');
                if (selectWidget) {
                    selectWidget.click();
                    return 'select_clicked';
                }

                // 2. Multi-select combobox (searchable input)
                const comboBtn = fieldContainer.querySelector('[data-automation-id="multiselectInputContainer"], [role="combobox"]');
                const input = fieldContainer.querySelector('[data-automation-id="multiselectInputContainer"] input, input[role="combobox"]');
                if (input) {
                    input.click();
                    input.focus();
                    return 'input_found';
                }
                if (comboBtn) {
                    comboBtn.click();
                    return 'btn_found';
                }

                // 3. Fallback: click the first button in the container
                const fallbackBtn = fieldContainer.querySelector('button');
                if (fallbackBtn) {
                    fallbackBtn.click();
                    return 'select_clicked';
                }
                return false;
            }""")
            print(f"Workday: combobox input discovery: {source_open_state}", file=sys.stderr)

            if source_open_state in (True, "input_found", "btn_found"):
                # Multi-select combobox: clear and type a short generic term to get all options
                page.keyboard.type(" ", delay=50)
                page.wait_for_timeout(1000)
            elif source_open_state == "select_clicked":
                # Standard dropdown: already clicked open, wait for options
                page.wait_for_timeout(1000)
            else:
                source_btn.first.click(force=True)
                page.wait_for_timeout(1000)

            # After opening, look for dropdown options that appeared.
            # Workday renders the dropdown as a portal at document root.
            # The key identifier: options that are NOT phone codes or wizard steps.
            result = page.evaluate("""() => {
                const allOpts = document.querySelectorAll('[role="option"], [data-automation-id="promptOption"]');
                const sourceOpts = [];
                const otherOpts = [];
                for (const o of allOpts) {
                    const text = o.textContent.trim();
                    if (text.includes('+1') || text.includes('step ') || text.includes('Error-')
                        || text.includes('current step')) {
                        otherOpts.push(text.substring(0, 50));
                        continue;
                    }
                    sourceOpts.push(text.substring(0, 80));
                }
                return {sourceOpts, otherOpts: otherOpts.length, total: allOpts.length};
            }""")
            print(
                f"Workday: dropdown options — source: {result.get('sourceOpts', [])[:10]}, "
                f"filtered out: {result.get('otherOpts', 0)}, total: {result.get('total', 0)}",
                file=sys.stderr,
            )

            source_opts = [str(option) for option in result.get("sourceOpts", []) if str(option or "").strip()]
            current_options = source_opts
            seen_option_sets: set[tuple[str, ...]] = set()
            for _source_level in range(4):
                option_signature = tuple(normalize_text(option) for option in current_options if normalize_text(option))
                if option_signature in seen_option_sets:
                    break
                if option_signature:
                    seen_option_sets.add(option_signature)
                candidates = _workday_source_search_candidates(
                    "How Did You Hear About Us?",
                    current_options,
                    source_hint=source_hint,
                    source_answer=source_answer,
                    company_name=company_name,
                )
                print(f"Workday: source candidates for 'How Did You Hear': {candidates}", file=sys.stderr)
                advanced_to_nested_options = False
                for candidate in candidates:
                    if _select_workday_prompt_option_via_input(
                        page,
                        "#source--source",
                        candidate,
                        label_text="How Did You Hear About Us?",
                    ):
                        state = _workday_prompt_state_for_label(page, "How Did You Hear About Us?")
                        selected_value = _workday_prompt_matching_value(
                            page,
                            "How Did You Hear About Us?",
                            candidate,
                            state=state,
                        )
                        if selected_value:
                            filled.append(
                                {
                                    "field_name": "source",
                                    "value": selected_value,
                                    "source": "deterministic",
                                    "filled": True,
                                }
                            )
                            source_filled = True
                            print(f"Workday: filled 'How Did You Hear' via input: {selected_value}", file=sys.stderr)
                            break
                        _clear_mismatched_workday_prompt_selection(
                            page,
                            "How Did You Hear About Us?",
                            candidate,
                            state=state,
                        )
                    clicked = _select_workday_prompt_option_via_locator(page, "How Did You Hear About Us?", candidate)
                    if not clicked:
                        clicked = _select_workday_prompt_option_for_label(page, "How Did You Hear About Us?", candidate)
                    if not clicked:
                        clicked = _click_visible_workday_prompt_option(page, candidate)
                    if not clicked:
                        _select_workday_prompt_option_via_keyboard(page, candidate)
                    state = _workday_prompt_state_for_label(page, "How Did You Hear About Us?")
                    selected_value = _workday_prompt_matching_value(
                        page,
                        "How Did You Hear About Us?",
                        candidate,
                        state=state,
                    )
                    if selected_value:
                        filled.append(
                            {"field_name": "source", "value": selected_value, "source": "deterministic", "filled": True}
                        )
                        source_filled = True
                        print(f"Workday: filled 'How Did You Hear' with: {selected_value}", file=sys.stderr)
                        break
                    _clear_mismatched_workday_prompt_selection(
                        page,
                        "How Did You Hear About Us?",
                        candidate,
                        state=state,
                    )
                    nested_options = _workday_prompt_nested_options(state, option_signature)
                    if nested_options:
                        current_options = nested_options
                        advanced_to_nested_options = True
                        break
                    if state.get("highlightedOptions"):
                        advanced_state = _advance_workday_prompt_highlighted_selection(page, "How Did You Hear About Us?")
                        selected_value = _workday_prompt_matching_value(
                            page,
                            "How Did You Hear About Us?",
                            candidate,
                            state=advanced_state,
                        )
                        if selected_value:
                            filled.append(
                                {
                                    "field_name": "source",
                                    "value": selected_value,
                                    "source": "deterministic",
                                    "filled": True,
                                }
                            )
                            source_filled = True
                            print(
                                f"Workday: filled 'How Did You Hear' with staged selection: {selected_value}",
                                file=sys.stderr,
                            )
                            break
                        _clear_mismatched_workday_prompt_selection(
                            page,
                            "How Did You Hear About Us?",
                            candidate,
                            state=advanced_state,
                        )
                        nested_options = _workday_prompt_nested_options(advanced_state, option_signature)
                        if nested_options:
                            current_options = nested_options
                            advanced_to_nested_options = True
                            break
                if source_filled:
                    break
                if advanced_to_nested_options:
                    continue
                latest_state = _workday_prompt_state_for_label(page, "How Did You Hear About Us?")
                for candidate in candidates:
                    selected_value = _workday_prompt_matching_value(
                        page,
                        "How Did You Hear About Us?",
                        candidate,
                        state=latest_state,
                    )
                    if not selected_value:
                        continue
                    filled.append(
                        {"field_name": "source", "value": selected_value, "source": "deterministic", "filled": True}
                    )
                    source_filled = True
                    print(f"Workday: filled 'How Did You Hear' with final state: {selected_value}", file=sys.stderr)
                    break
                if not source_filled:
                    for candidate in candidates:
                        if _clear_mismatched_workday_prompt_selection(
                            page,
                            "How Did You Hear About Us?",
                            candidate,
                            state=latest_state,
                        ):
                            break
                if source_filled:
                    break
                nested_options = _workday_prompt_nested_options(latest_state, option_signature)
                if nested_options:
                    current_options = nested_options
                    continue

            if not source_filled:
                selected = _fill_workday_source_prompt(
                    page,
                    "How Did You Hear About Us?",
                    _workday_source_search_candidates(
                        "How Did You Hear About Us?",
                        current_options,
                        source_hint=source_hint,
                        source_answer=source_answer,
                        company_name=company_name,
                    ),
                )
                if selected:
                    filled.append({"field_name": "source", "value": selected, "source": "deterministic", "filled": True})
                    source_filled = True
                    print(f"Workday: filled 'How Did You Hear' with direct prompt fallback: {selected}", file=sys.stderr)

            if not source_filled:
                latest_state = _workday_prompt_state_for_label(page, "How Did You Hear About Us?")
                print(
                    "Workday: could not confirm 'How Did You Hear About Us' selection. "
                    f"Latest state: {latest_state}",
                    file=sys.stderr,
                )
                print("Workday: could not fill 'How Did You Hear About Us'", file=sys.stderr)

            # Dismiss any open popup/dropdown before continuing to other fields
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            # Force-close any popup overlays via JS
            page.evaluate("""() => {
                // Close Workday popper popups
                document.querySelectorAll('[data-behavior-click-outside-close]').forEach(el => {
                    el.style.display = 'none';
                });
                // Click the page heading to trigger outside-click handlers
                const heading = document.querySelector('h2, h1, [data-automation-id="pageHeaderTitle"]');
                if (heading) heading.click();
            }""")
            page.wait_for_timeout(500)

    _fill_workday_source_field("Source", "source_detail")

    prior_employment_prompt = next(
        (
            label_text
            for label_text in _workday_radio_question_labels(page)
            if _looks_like_workday_prior_employment_prompt(label_text)
        ),
        None,
    )
    if prior_employment_prompt:
        if _fill_workday_labeled_radio(page, prior_employment_prompt, "No"):
            filled.append({"field_name": "former_employee", "value": "No", "source": "deterministic", "filled": True})
        else:
            print(
                f"Workday: could not fill prior employment prompt '{prior_employment_prompt}'",
                file=sys.stderr,
            )

    # Name fields
    first_name = _workday_textbox_field(
        page,
        "First Name",
        selectors=("input[name='legalName--firstName']", "input[name='preferredName--firstName']"),
    )
    first_name_value = getattr(profile, "first_name", "") or ""
    if first_name_value and _workday_can_fill(first_name) and not (_workday_locator_value(first_name) or "").strip():
        _workday_fill_if_visible(first_name, first_name_value)
        filled.append(
            {"field_name": "first_name", "value": first_name_value, "source": "master_resume.md", "filled": True}
        )

    last_name = _workday_textbox_field(
        page,
        "Last Name",
        selectors=("input[name='legalName--lastName']", "input[name='preferredName--lastName']"),
    )
    last_name_value = getattr(profile, "last_name", "") or ""
    if last_name_value and _workday_can_fill(last_name) and not (_workday_locator_value(last_name) or "").strip():
        _workday_fill_if_visible(last_name, last_name_value)
        filled.append(
            {"field_name": "last_name", "value": last_name_value, "source": "master_resume.md", "filled": True}
        )

    # Address — ApplicationProfile has `location` (e.g. "San Francisco, CA")
    # but no street_address/city/zip_code fields. Extract from location.
    location = getattr(application_profile, "location", "") or ""
    city_part = location.split(",")[0].strip() if location else "San Francisco"

    address_line = page.get_by_role("textbox", name="Address Line 1")
    if address_line.count() and not address_line.input_value():
        address_line.fill(city_part)
        filled.append({"field_name": "address", "value": city_part, "source": "application_profile.md", "filled": True})

    city_field = page.get_by_role("textbox", name="City")
    if city_field.count() and not city_field.input_value():
        city_field.fill(city_part)
        filled.append({"field_name": "city", "value": city_part, "source": "application_profile.md", "filled": True})

    # State dropdown — same approach as "How Did You Hear": find the input
    # inside the field container, type to filter, click matching option via JS
    state = _state_from_location(location)
    if state:
        state_filled = page.evaluate(
            """(stateName) => {
            // Find the State field container
            const labels = document.querySelectorAll('label');
            let container = null;
            for (const l of labels) {
                if (l.textContent.trim() === 'State' || l.textContent.trim() === 'State*') {
                    container = l.closest('[data-automation-id*="formField"]') || l.parentElement?.parentElement;
                    break;
                }
            }
            if (!container) return false;

            // Find the dropdown button/input inside
            const btn = container.querySelector('button, [role="combobox"], input');
            if (btn) {
                btn.click();
                return 'clicked';
            }
            return false;
        }""",
            state,
        )
        if state_filled:
            page.wait_for_timeout(500)
            # Type to filter
            page.keyboard.type(state[:5], delay=50)
            page.wait_for_timeout(800)
            # Click matching option via JS (filter out non-state options)
            clicked = page.evaluate(
                """(stateName) => {
                const opts = document.querySelectorAll('[role="option"]');
                for (const o of opts) {
                    const t = o.textContent.trim();
                    if (t.includes(stateName) && !t.includes('+1')) {
                        o.click();
                        return t;
                    }
                }
                return null;
            }""",
                state,
            )
            if clicked:
                page.wait_for_timeout(300)
                filled.append(
                    {"field_name": "state", "value": clicked, "source": "application_profile.md", "filled": True}
                )
            else:
                page.keyboard.press("Escape")

    postal = page.get_by_role("textbox", name="Postal Code")
    if postal.count() and not postal.input_value():
        postal.fill("94105")
        filled.append({"field_name": "postal_code", "value": "94105", "source": "deterministic", "filled": True})

    # Email — not always pre-filled from account creation
    candidate_email = profile.email if "@" in (profile.email or "") else ""
    if not candidate_email:
        candidate_email = getattr(application_profile, "verification_code_email", "") or ""
    if candidate_email and "@" in candidate_email:
        email_field = _workday_email_field(page, "Email", "Email Address")
        if email_field.count():
            current_val = email_field.input_value()
            if not current_val or "@" not in current_val:
                email_field.fill(candidate_email)
                filled.append(
                    {"field_name": "email", "value": candidate_email, "source": "master_resume.md", "filled": True}
                )

    # Phone Device Type dropdown
    phone_device_btn = page.get_by_role("button", name="Phone Device Type")
    if phone_device_btn.count():
        phone_device_btn.first.click(force=True)
        page.wait_for_timeout(500)
        for candidate in ["Mobile", "Cell", "Personal Mobile", "Home"]:
            opt = page.locator(f"[role='option']:has-text('{candidate}')")
            if opt.count():
                opt.first.click()
                page.wait_for_timeout(300)
                filled.append(
                    {"field_name": "phone_device_type", "value": candidate, "source": "deterministic", "filled": True}
                )
                break
        else:
            # Pick first non-placeholder option
            options = page.locator("[role='option']")
            if options.count() > 1:
                options.nth(1).click()
                page.wait_for_timeout(300)
                filled.append(
                    {
                        "field_name": "phone_device_type",
                        "value": "first_option",
                        "source": "deterministic",
                        "filled": True,
                    }
                )
            else:
                page.keyboard.press("Escape")

    # Phone Number — also clear if Workday's resume parser put an email in it
    phone_field = page.get_by_role("textbox", name="Phone Number")
    if phone_field.count():
        current_phone = phone_field.input_value()
        needs_fill = not current_phone or "@" in current_phone
        if needs_fill and profile.phone:
            # Strip country code prefix if present
            phone = re.sub(r"^\+?1[\s-]?", "", profile.phone)
            phone_field.fill(phone)
            filled.append({"field_name": "phone", "value": phone, "source": "master_resume.md", "filled": True})

    if _check_workday_acknowledgment_checkbox(page):
        filled.append(
            {
                "field_name": "required_acknowledgment_checked",
                "value": "Yes",
                "source": "deterministic",
                "filled": True,
            }
        )

    return filled


def _fill_my_experience(page, out_dir: Path) -> list[dict]:
    """Fill the My Experience page — upload resume."""
    filled = []

    # Upload resume
    resume_path = find_resume_file(out_dir)
    if resume_path:
        _dedupe_workday_uploaded_resume_items(page, resume_path.name, keep=1)
        file_input = page.locator(
            "input[type='file'][data-automation-id='file-upload-input-ref'], input[type='file']"
        ).first
        if file_input.count():
            try:
                if not _workday_resume_already_uploaded(page, resume_path.name):
                    file_input.set_input_files(str(resume_path))
                    page.wait_for_timeout(5000)  # Wait for upload + parsing
                    _dedupe_workday_uploaded_resume_items(page, resume_path.name, keep=1)
                    filled.append(
                        {"field_name": "resume", "value": str(resume_path.name), "source": "documents/", "filled": True}
                    )
            except Exception as exc:
                print(f"Workday: resume upload failed: {exc}", file=sys.stderr)

    # Upload cover letter if there's a second file input
    try:
        cover_letter_path = find_cover_letter_file(out_dir)
    except FileNotFoundError:
        cover_letter_path = None
    if cover_letter_path:
        all_file_inputs = page.locator("input[type='file']")
        if all_file_inputs.count() > 1:
            try:
                all_file_inputs.nth(1).set_input_files(str(cover_letter_path))
                page.wait_for_timeout(3000)
                filled.append(
                    {
                        "field_name": "cover_letter",
                        "value": str(cover_letter_path.name),
                        "source": "documents/",
                        "filled": True,
                    }
                )
            except Exception:
                pass

    try:
        resume_lines = _load_workday_resume_lines(PROJECT_ROOT / "master_resume.md")
    except FileNotFoundError:
        resume_lines = []
    try:
        application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    except Exception:
        application_profile = None

    employment_entries = _parse_workday_employment_entries(resume_lines)
    if employment_entries:
        work_experience_rows = _ensure_workday_repeater_rows(
            page,
            row_selector="[data-fkit-id^='workExperience-'][data-fkit-id$='--null']",
            target_count=len(employment_entries),
            add_button_selectors=_WORKDAY_WORK_EXPERIENCE_ADD_BUTTON_SELECTORS,
        )
        row_count = min(work_experience_rows.count(), len(employment_entries))
        for row_index in range(row_count):
            row = work_experience_rows.nth(row_index)
            entry = employment_entries[row_index]
            try:
                job_title_field = row.locator("[data-fkit-id$='--jobTitle'] input").first
                company_field = row.locator("[data-fkit-id$='--companyName'] input").first
                if not job_title_field.count() or not company_field.count():
                    continue

                try:
                    job_title_field.scroll_into_view_if_needed()
                except Exception:
                    pass
                if entry.title and not _workday_locator_value(job_title_field).strip():
                    human_fill(job_title_field, entry.title, delay_ms=35)
                    filled.append(
                        {
                            "field_name": f"work_experience_{row_index + 1}_job_title",
                            "value": entry.title,
                            "source": "master_resume.md",
                            "filled": True,
                        }
                    )
                if entry.company and not _workday_locator_value(company_field).strip():
                    human_fill(company_field, entry.company, delay_ms=35)
                    filled.append(
                        {
                            "field_name": f"work_experience_{row_index + 1}_company",
                            "value": entry.company,
                            "source": "master_resume.md",
                            "filled": True,
                        }
                    )

                location_field = row.locator("[data-fkit-id$='--location'] input").first
                if entry.location and location_field.count() and not _workday_locator_value(location_field).strip():
                    human_fill(location_field, entry.location, delay_ms=35)
                    filled.append(
                        {
                            "field_name": f"work_experience_{row_index + 1}_location",
                            "value": entry.location,
                            "source": "master_resume.md",
                            "filled": True,
                        }
                    )

                current_checkbox = row.locator("[data-fkit-id$='--currentlyWorkHere'] input[type='checkbox']").first
                if entry.is_current and current_checkbox.count() and not current_checkbox.is_checked():
                    checkbox_id = current_checkbox.get_attribute("id") or ""
                    checkbox_label = (
                        row.locator(_workday_label_for_selector(checkbox_id)).first
                        if checkbox_id
                        else row.locator("label:has-text('I currently work here')").first
                    )
                    if checkbox_label.count():
                        checkbox_label.click()
                    else:
                        current_checkbox.check(force=True)
                    page.wait_for_timeout(500)
                    filled.append(
                        {
                            "field_name": f"work_experience_{row_index + 1}_current",
                            "value": "Yes",
                            "source": "master_resume.md",
                            "filled": True,
                        }
                    )

                start_month_field = row.locator("[data-fkit-id$='--startDate'] input[aria-label='Month']").first
                start_year_field = row.locator("[data-fkit-id$='--startDate'] input[aria-label='Year']").first
                if (
                    entry.start_month
                    and start_month_field.count()
                    and _workday_locator_value(start_month_field).strip() != entry.start_month
                ):
                    human_fill(start_month_field, entry.start_month, delay_ms=35)
                if (
                    entry.start_year
                    and start_year_field.count()
                    and _workday_locator_value(start_year_field).strip() != entry.start_year
                ):
                    human_fill(start_year_field, entry.start_year, delay_ms=35)
                if entry.start_month and entry.start_year:
                    filled.append(
                        {
                            "field_name": f"work_experience_{row_index + 1}_start_date",
                            "value": f"{entry.start_month}/{entry.start_year}",
                            "source": "master_resume.md",
                            "filled": True,
                        }
                    )

                if not entry.is_current and entry.end_month and entry.end_year:
                    end_month_field = row.locator("[data-fkit-id$='--endDate'] input[aria-label='Month']").first
                    end_year_field = row.locator("[data-fkit-id$='--endDate'] input[aria-label='Year']").first
                    if (
                        end_month_field.count()
                        and _workday_locator_value(end_month_field).strip() != entry.end_month
                    ):
                        human_fill(end_month_field, entry.end_month, delay_ms=35)
                    if (
                        end_year_field.count()
                        and _workday_locator_value(end_year_field).strip() != entry.end_year
                    ):
                        human_fill(end_year_field, entry.end_year, delay_ms=35)
                    filled.append(
                        {
                            "field_name": f"work_experience_{row_index + 1}_end_date",
                            "value": f"{entry.end_month}/{entry.end_year}",
                            "source": "master_resume.md",
                            "filled": True,
                        }
                    )

                role_description_field = row.locator("[data-fkit-id$='--roleDescription'] textarea").first
                expected_role_description = _workday_role_description_text(entry)
                current_role_description = re.sub(r"\r\n?", "\n", _workday_locator_value(role_description_field)).strip()
                if (
                    expected_role_description
                    and role_description_field.count()
                    and current_role_description != expected_role_description
                ):
                    human_fill(role_description_field, expected_role_description, delay_ms=10)
                    filled.append(
                        {
                            "field_name": f"work_experience_{row_index + 1}_role_description",
                            "value": f"{len(entry.bullets)} bullets",
                            "source": "master_resume.md",
                            "filled": True,
                        }
                    )
            except Exception as exc:
                print(f"Workday: manual work experience fill failed: {exc}", file=sys.stderr)

    education_entries = _build_workday_education_entries(application_profile, resume_lines)
    if education_entries:
        education_rows = _ensure_workday_repeater_rows(
            page,
            row_selector="[data-fkit-id^='education-'][data-fkit-id$='--null']",
            target_count=len(education_entries),
            add_button_selectors=_WORKDAY_EDUCATION_ADD_BUTTON_SELECTORS,
        )
        row_count = min(education_rows.count(), len(education_entries))
        for row_index in range(row_count):
            row = education_rows.nth(row_index)
            entry = education_entries[row_index]
            try:
                school_field = row.locator("[data-fkit-id$='--schoolName'] input").first
                degree_button = row.locator("[data-fkit-id$='--degree'] button").first
                if not school_field.count() and not degree_button.count():
                    continue

                if school_field.count() and not (school_field.input_value() or "").strip() and entry.school:
                    school_field.scroll_into_view_if_needed()
                    human_fill(school_field, entry.school, delay_ms=35)
                    filled.append(
                        {
                            "field_name": f"education_{row_index + 1}_school",
                            "value": entry.school,
                            "source": "application_profile.md",
                            "filled": True,
                        }
                    )

                if degree_button.count() and not _workday_dropdown_has_selected_value(degree_button):
                    for candidate in entry.degree_candidates:
                        if not _fill_workday_dropdown_locator(page, degree_button, candidate):
                            continue
                        filled.append(
                            {
                                "field_name": f"education_{row_index + 1}_degree",
                                "value": candidate,
                                "source": "application_profile.md",
                                "filled": True,
                            }
                        )
                        break

                field_of_study_input = row.locator("[data-fkit-id$='--fieldOfStudy'] input").first
                if field_of_study_input.count() and not (field_of_study_input.input_value() or "").strip():
                    field_of_study_id = field_of_study_input.get_attribute("id") or ""
                    if field_of_study_id:
                        for candidate in entry.discipline_candidates:
                            if not _select_workday_prompt_option_via_input(
                                page,
                                _workday_dom_id_selector(field_of_study_id),
                                candidate,
                            ):
                                continue
                            filled.append(
                                {
                                    "field_name": f"education_{row_index + 1}_field_of_study",
                                    "value": candidate,
                                    "source": "master_resume.md",
                                    "filled": True,
                                }
                            )
                            break

                first_year_field = row.locator("[data-fkit-id$='--firstYearAttended'] input[aria-label='Year']").first
                if entry.start_year and first_year_field.count() and not (first_year_field.input_value() or "").strip():
                    human_fill(first_year_field, entry.start_year, delay_ms=35)
                    filled.append(
                        {
                            "field_name": f"education_{row_index + 1}_start_year",
                            "value": entry.start_year,
                            "source": "master_resume.md",
                            "filled": True,
                        }
                    )

                last_year_field = row.locator("[data-fkit-id$='--lastYearAttended'] input[aria-label='Year']").first
                if entry.end_year and last_year_field.count() and not (last_year_field.input_value() or "").strip():
                    human_fill(last_year_field, entry.end_year, delay_ms=35)
                    filled.append(
                        {
                            "field_name": f"education_{row_index + 1}_end_year",
                            "value": entry.end_year,
                            "source": "master_resume.md",
                            "filled": True,
                        }
                    )
            except Exception as exc:
                print(f"Workday: manual education fill failed: {exc}", file=sys.stderr)

    if hasattr(page, "evaluate"):
        language_entries = _workday_primary_language_entries(resume_lines)
        if language_entries:
            primary_language, proficiency = language_entries[0]
            if _fill_workday_labeled_dropdown(page, "Language", primary_language):
                filled.append(
                    {
                        "field_name": "language_1",
                        "value": primary_language,
                        "source": "master_resume.md",
                        "filled": True,
                    }
                )
            if proficiency in {"native", "fluent"} and _check_workday_checkbox_for_label(
                page, "I am fluent in this language."
            ):
                filled.append(
                    {
                        "field_name": "language_1_fluent",
                        "value": "Yes",
                        "source": "master_resume.md",
                        "filled": True,
                    }
                )
            for level_candidate in _workday_language_level_candidates(proficiency):
                if not _fill_workday_labeled_dropdown(page, "Level", level_candidate):
                    continue
                filled.append(
                    {
                        "field_name": "language_1_level",
                        "value": level_candidate,
                        "source": "master_resume.md",
                        "filled": True,
                    }
                )
                break

        skill_candidates = _workday_resume_skill_candidates(resume_lines)
        if skill_candidates:
            selected_skill = _fill_workday_prompt_field_candidates(
                page,
                _WORKDAY_SKILLS_LABEL_CANDIDATES,
                skill_candidates[:_WORKDAY_SKILL_CANDIDATE_LIMIT],
            )
            if selected_skill:
                filled.append(
                    {
                        "field_name": "skills",
                        "value": selected_skill,
                        "source": "master_resume.md",
                        "filled": True,
                    }
                )

    return filled


def _fill_voluntary_disclosures(page, application_profile, *, profile=None) -> list[dict]:
    """Fill the Voluntary Self-Identification / Disclosures page."""
    filled = []

    race = getattr(application_profile, "race_or_ethnicity", "") or ""
    hispanic_value = "Yes" if any(fragment in race.casefold() for fragment in ("hispanic", "latino")) else "No"

    # Gender
    gender = getattr(application_profile, "gender", "")
    if gender and _fill_workday_labeled_dropdown_candidates(
        page,
        _WORKDAY_GENDER_LABEL_CANDIDATES,
        gender,
        profile_field="gender",
    ):
        filled.append({"field_name": "gender", "value": gender, "source": "application_profile.md", "filled": True})

    # Race / Ethnicity
    race_value_filled: str | None = None
    if race and _fill_workday_labeled_dropdown_candidates(
        page,
        _WORKDAY_RACE_LABEL_CANDIDATES,
        race,
        profile_field="race_or_ethnicity",
    ):
        race_value_filled = race
    elif race:
        race_value_filled = _fill_workday_labeled_checkbox_group_candidates(
            page,
            _WORKDAY_RACE_LABEL_CANDIDATES,
            race,
            profile_field="race_or_ethnicity",
        )

    if race_value_filled:
        filled.append(
            {
                "field_name": "race_ethnicity",
                "value": race_value_filled,
                "source": "application_profile.md",
                "filled": True,
            }
        )

    if _fill_workday_labeled_dropdown_candidates(
        page,
        _WORKDAY_HISPANIC_LABEL_CANDIDATES,
        hispanic_value,
    ):
        filled.append(
            {
                "field_name": "hispanic_or_latino",
                "value": hispanic_value,
                "source": "application_profile.md",
                "filled": True,
            }
        )

    # Veteran Status
    veteran = getattr(application_profile, "veteran_status", "")
    if veteran:
        veteran_candidates = [veteran, veteran.upper()]
        if "not a protected veteran" in veteran.casefold():
            veteran_candidates.extend(
                [
                    "I AM NOT A VETERAN",
                    "I am not a veteran",
                    "I IDENTIFY AS A VETERAN, JUST NOT A PROTECTED VETERAN",
                    "I DO NOT WISH TO SELF-IDENTIFY",
                ]
            )
        for veteran_candidate in veteran_candidates:
            if _fill_workday_labeled_dropdown_candidates(
                page,
                _WORKDAY_VETERAN_STATUS_LABEL_CANDIDATES,
                veteran_candidate,
                profile_field="veteran_status",
            ):
                filled.append(
                    {
                        "field_name": "veteran_status",
                        "value": veteran_candidate,
                        "source": "application_profile.md",
                        "filled": True,
                    }
                )
                break

    # Disability Status
    disability = getattr(application_profile, "disability_status", "")
    if disability:
        disability_sel = "[data-automation-id='disabilityStatusDropdown'], [data-automation-id='disabilityStatus']"
        if _fill_workday_dropdown(page, disability_sel, disability, profile_field="disability_status"):
            filled.append(
                {
                    "field_name": "disability_status",
                    "value": disability,
                    "source": "application_profile.md",
                    "filled": True,
                }
            )

    if _check_workday_acknowledgment_checkbox(page):
        filled.append(
            {
                "field_name": "required_acknowledgment_checked",
                "value": "Yes",
                "source": "deterministic",
                "filled": True,
            }
        )

    # Also try radio buttons / checkboxes for these fields
    _try_radio_or_checkbox(page, "gender", gender, filled)
    _try_radio_or_checkbox(page, "race", race, filled)
    _try_radio_or_checkbox(page, "veteran", veteran, filled)
    _try_radio_or_checkbox(page, "disability", disability, filled)

    full_name = getattr(profile, "full_name", "") if profile else ""
    if full_name:
        name_field = page.locator("[data-fkit-id$='--name'] input").first
        if name_field.count() and not (name_field.input_value() or "").strip():
            human_fill(name_field, full_name, delay_ms=35)
            filled.append(
                {
                    "field_name": "self_identify_name",
                    "value": full_name,
                    "source": "master_resume.md",
                    "filled": True,
                }
            )

    if disability:
        if _select_workday_checkbox_option(page, disability):
            filled.append(
                {
                    "field_name": "disability_status",
                    "value": disability,
                    "source": "application_profile.md",
                    "filled": True,
                }
            )

    current_date = datetime.now().date()
    date_scope = page.locator("[data-fkit-id$='--dateSignedOn'], [data-automation-id='formField-dateSignedOn']").first
    if date_scope.count() and _fill_workday_self_identify_date_scope(page, date_scope, current_date.isoformat()):
        filled.append(
            {
                "field_name": "self_identify_date",
                "value": current_date.isoformat(),
                "source": "deterministic",
                "filled": True,
            }
        )

    return filled


def _try_radio_or_checkbox(page, field_fragment: str, value: str, filled: list[dict]) -> None:
    """Try to select a radio/checkbox option matching the value."""
    if not value:
        return
    profile_field = _WORKDAY_RADIO_OR_CHECKBOX_PROFILE_FIELDS.get(field_fragment)
    option_labels = page.locator("label[for]")
    option_entries: list[tuple[str, object, str]] = []
    visible_option_texts: list[str] = []
    for index in range(option_labels.count()):
        label = option_labels.nth(index)
        try:
            if not label.is_visible():
                continue
        except Exception:
            continue
        text = re.sub(r"\s+", " ", label.inner_text()).strip()
        target_id = (label.get_attribute("for") or "").strip()
        if not text or not target_id:
            continue
        visible_option_texts.append(text)
        option_entries.append((text, label, target_id))

    desired_text = None
    if visible_option_texts and profile_field:
        desired_text = select_profile_option(
            visible_option_texts,
            value,
            profile_field=profile_field,
        )
    if desired_text is None:
        desired_norm = _normalize_workday_option_text(value)
        desired_text = next(
            (text for text in visible_option_texts if _normalize_workday_option_text(text) == desired_norm),
            None,
        )

    if desired_text is None and not option_entries:
        labels = page.locator(f"label:has-text('{value}')")
        if labels.count():
            try:
                label = labels.first
                target_id = label.get_attribute("for") or ""
                if target_id:
                    target_input = page.locator(_workday_dom_id_selector(target_id)).first
                    try:
                        if target_input.count() and target_input.is_checked():
                            return
                    except Exception:
                        pass
                label.click()
                filled.append(
                    {"field_name": field_fragment, "value": value, "source": "application_profile.md", "filled": True}
                )
            except Exception:
                pass
        return

    desired_norm = _normalize_workday_option_text(desired_text or "")
    if not desired_norm:
        return

    for text, label, target_id in option_entries:
        if _normalize_workday_option_text(text) != desired_norm:
            continue
        target_input = page.locator(_workday_dom_id_selector(target_id)).first
        try:
            if target_input.count() and target_input.is_checked():
                return
        except Exception:
            pass
        try:
            label.click()
        except Exception:
            continue
        filled.append(
            {
                "field_name": field_fragment,
                "value": text,
                "source": "application_profile.md",
                "filled": True,
            }
        )
        return


_COMPENSATION_DEFLECT = (
    "I'm open and flexible on compensation. I'd prefer to learn more about "
    "the role's scope and total rewards package before discussing specific numbers. "
    "I'm confident we can find a mutually agreeable arrangement."
)


def _looks_like_workday_remote_state_availability_prompt(label: str) -> bool:
    normalized = normalize_text(label)
    if not normalized:
        return False
    if "able to work in" not in normalized:
        return False
    if "remote us location" not in normalized and "remote u s location" not in normalized:
        return False
    return any(
        fragment in normalized
        for fragment in (
            "what state are you able to work in",
            "what state s are you able to work in",
            "which state are you able to work in",
            "which state s are you able to work in",
            "what states are you able to work in",
            "which states are you able to work in",
        )
    )


def _workday_numeric_only_compensation_options(options: list[str] | None) -> bool:
    actionable_options = [
        re.sub(r"\s+", " ", str(option or "")).strip()
        for option in options or []
        if re.sub(r"\s+", " ", str(option or "")).strip()
    ]
    actionable_options = [
        option for option in actionable_options if _normalize_workday_option_text(option) not in {"select one", "select"}
    ]
    if not actionable_options:
        return False

    for option in actionable_options:
        normalized = normalize_text(option)
        if not normalized:
            continue
        if any(fragment in normalized for fragment in ("open", "flexible", "negotiable", "discuss", "competitive")):
            return False
        if not re.search(r"\d", normalized):
            return False
    return True


def _workday_numeric_only_compensation_blocker(spec: dict, planned_value: str) -> dict[str, object]:
    return {
        "field_name": str(spec.get("field_name") or "").strip(),
        "label": str(spec.get("label") or "").strip(),
        "kind": str(spec.get("kind") or "").strip() or "select",
        "required": bool(spec.get("required", True)),
        "source": "application_profile.md",
        "status": "planned",
        "blocker_kind": "generated_answer",
        "blocks_draft_completion": True,
        "planned_value": planned_value,
        "reason": (
            "This Workday compensation question only offers numeric salary ranges, and project policy requires "
            "a non-numeric compensation answer. Explicit user input is required before autofill can continue."
        ),
        "note": (
            "Workday left this compensation field unresolved because the form only offers numeric salary ranges "
            "and project policy requires non-numeric compensation answers."
        ),
    }


def _answer_from_classifier(label: str, application_profile) -> str | None:
    """Use the unified question classifier to produce a deterministic answer.

    Returns an answer string if the classifier identifies the question category
    and a deterministic answer can be produced, otherwise None.
    """
    category = classify_question(label)
    if category is None:
        return None

    policy = resolve_shared_question_policy(label, application_profile)
    if policy is not None and policy.category != "work_authorization" and policy.text_value is not None:
        return policy.text_value

    if category == "education":
        return format_education_from_profile(application_profile) or None
    if category == "compensation":
        return _COMPENSATION_DEFLECT
    if category == "nda_noncompete":
        return "No"
    if category == "work_authorization":
        return build_truthful_work_authorization_answer(label, application_profile) or (
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
    # experience_confirmation, office_attendance, company_engagement) are left
    # to the LLM for Workday since they may require selecting from dropdown options.
    return None


def _deterministic_workday_question_value(
    label: str, kind: str, application_profile, options: list[str] | None = None
) -> str | None:
    normalized_label = normalize_text(label)
    if kind == "date":
        bare_date = _workday_bare_date_answer(label)
        if bare_date:
            return bare_date
    if kind in {"text", "textarea"} and _looks_like_workday_remote_state_availability_prompt(label):
        return _state_from_location(getattr(application_profile, "location", "") or "") or None
    if kind in {"select", "radio"}:
        policy = resolve_shared_question_policy(label, application_profile)
        if policy is not None and policy.text_value is not None:
            return (
                select_shared_policy_option(options, policy, application_profile=application_profile)
                or policy.text_value
            )
        if "pronoun" in normalized_label and getattr(application_profile, "pronouns", None):
            return select_profile_option(
                options,
                application_profile.pronouns,
                profile_field="pronouns",
            ) or application_profile.pronouns
        if any(
            fragment in normalized_label
            for fragment in (
                "eligible to work",
                "authorized to work",
                "authorization to work",
                "work authorization",
                "right to work",
                "legally authorized",
            )
        ):
            return "Yes" if application_profile.authorized_to_work_unconditionally else "No"
        if any(
            fragment in normalized_label
            for fragment in (
                "require sponsorship",
                "visa support",
                "visa sponsorship",
                "employment sponsorship",
                "immigration sponsorship",
            )
        ):
            return application_profile.sponsorship_answer or (
                "Yes"
                if application_profile.require_sponsorship_now or application_profile.require_sponsorship_future
                else "No"
            )
        if options and (
            "previous question" in normalized_label
            or "most accurately fits your situation" in normalized_label
            or "which most accurately fits your situation" in normalized_label
        ):
            matched_status_option = _select_workday_follow_up_work_authorization_status_option(
                options,
                application_profile,
            )
            if matched_status_option:
                return matched_status_option
        if "talent community" in normalized_label:
            return "Yes"
    if (
        kind == "text"
        and ("employment based immigration status" in normalized_label or "immigration status" in normalized_label)
        and any(
            fragment in normalized_label
            for fragment in ("petition", "application", "on your behalf", "file", "proceed with")
        )
    ):
        return (
            "Yes"
            if application_profile.require_sponsorship_now or application_profile.require_sponsorship_future
            else "No"
        )
    return _answer_from_classifier(label, application_profile)


def _workday_bare_date_answer(label: str, *, now: datetime | None = None) -> str | None:
    normalized_label = normalize_text(label)
    if not normalized_label:
        return None
    bare_label = re.sub(r"^\d+\s*", "", normalized_label).strip(" :.-")
    if bare_label != "date":
        return None
    local_now = now.astimezone() if now is not None and now.tzinfo else datetime.now().astimezone()
    return local_now.strftime("%m/%d/%Y")


def _select_workday_follow_up_work_authorization_status_option(
    options: list[str] | None,
    application_profile,
) -> str | None:
    normalized_options = [(option, normalize_text(option)) for option in options or [] if normalize_text(option)]
    if not normalized_options:
        return None

    direct_positive_fragments = (
        "authorized to work for any employer",
        "can work for any employer",
        "authorized to work in the country",
        "authorised to work in the country",
        "do not require sponsorship",
        "do not need sponsorship",
        "no sponsorship",
        "no visa support",
        "do not require visa sponsorship",
        "do not require work permit sponsorship",
        "unrestricted work authorization",
        "indefinite work authorization",
    )
    negative_fragments = (
        "require sponsorship",
        "need sponsorship",
        "need visa support",
        "not authorized to work",
        "not authorised to work",
    )
    unsupported_identity_fragments = (
        "citizen",
        "national",
        "permanent resident",
        "green card",
        "refugee",
        "asylee",
    )

    for option, normalized_option in normalized_options:
        if not any(fragment in normalized_option for fragment in direct_positive_fragments):
            continue
        if any(fragment in normalized_option for fragment in negative_fragments):
            continue
        if any(fragment in normalized_option for fragment in unsupported_identity_fragments):
            continue
        return option

    support_texts: list[str] = []
    try:
        support_texts.append((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
    except OSError:
        pass
    work_auth_statement = str(getattr(application_profile, "work_authorization_statement", "") or "").strip()
    if work_auth_statement:
        support_texts.append(work_auth_statement)
    normalized_support = " ".join(normalize_text(text) for text in support_texts if text)
    identity_groups = (
        (
            (
                "united states citizen",
                "u s citizen",
                "us citizen",
                "american citizen",
                "u s national",
                "us national",
            ),
            ("citizen", "national"),
        ),
        (
            ("lawful permanent resident", "permanent resident", "green card"),
            ("permanent resident", "green card"),
        ),
        (("refugee",), ("refugee",)),
        (("asylee",), ("asylee",)),
    )
    for support_fragments, option_fragments in identity_groups:
        if not any(fragment in normalized_support for fragment in support_fragments):
            continue
        for option, normalized_option in normalized_options:
            if not any(fragment in normalized_option for fragment in option_fragments):
                continue
            if any(fragment in normalized_option for fragment in negative_fragments):
                continue
            return option

    profile_work_auth = normalize_text(getattr(application_profile, "work_authorization_statement", None))
    if any(fragment in profile_work_auth for fragment in ("always authorized", "authorized to work unconditionally")):
        for option, normalized_option in normalized_options:
            if "authorized to work" not in normalized_option and "authorised to work" not in normalized_option:
                continue
            if any(fragment in normalized_option for fragment in negative_fragments):
                continue
            if any(fragment in normalized_option for fragment in unsupported_identity_fragments):
                continue
            return option

    return None


def _maybe_write_truthful_workday_stuck_result(page, out_dir: Path, payload: dict, *, current_page: str) -> bool:
    if _is_workday_already_applied_job_page(page):
        _write_workday_already_applied_result(out_dir, payload)
        return True
    if _is_application_page(page):
        return False

    markers = _extract_workday_auth_markers(page)
    auth_state = str(markers.get("auth_state") or "unknown")
    if auth_state not in {
        "account_verification_gate",
        "authenticated_non_form",
        "create_account_gate",
        "credential_rejected",
        "maintenance",
        "password_reset_gate",
        "sign_in_gate",
    }:
        return False

    debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
    _capture(page, debug_png)
    auth_result = _build_workday_auth_result(
        payload,
        markers,
        auth_scope=workday_auth_scope(str(markers.get("page_url") or ""))
        or workday_auth_scope(payload.get("job_url", "")),
        last_attempted_step=current_page,
        credential_rejection_observed=auth_state == "credential_rejected",
        auth_state_hint=auth_state,
    )
    _write_workday_auth_result(out_dir, auth_result)
    return True


def _workday_question_kind(
    *,
    has_date: bool,
    has_textarea: bool,
    has_select: bool,
    has_radio: bool,
    has_checkbox: bool,
) -> str:
    if has_date:
        return "date"
    if has_textarea:
        return "textarea"
    if has_select:
        return "select"
    if has_radio:
        return "radio"
    if has_checkbox:
        return "checkbox"
    return "text"


def _workday_checkbox_answer_values(label: str, options: list[str], application_profile) -> list[str] | None:
    from application_submit_common import (
        normalize_text,
        question_is_location_residency_check,
        question_is_office_attendance_prompt,
        question_is_relocation_willingness,
    )
    from autofill_common import select_option

    if not options:
        return None

    normalized_options = [(option, normalize_text(option)) for option in options]
    candidate_city = normalize_text((application_profile.location or "").split(",")[0].strip())
    is_location_commitment = (
        question_is_office_attendance_prompt(label)
        or question_is_location_residency_check(label)
        or question_is_relocation_willingness(label)
        or "commuting distance" in normalize_text(label)
    )
    if is_location_commitment:
        if application_profile.location:
            for option, normalized in normalized_options:
                if "commuting distance" in normalized and any(
                    token in normalized for token in ("already", "currently", "reside", "live", "located")
                ):
                    return [option]
            for option, normalized in normalized_options:
                if candidate_city and candidate_city in normalized:
                    return [option]

        if getattr(application_profile, "willing_to_relocate", False):
            for option, normalized in normalized_options:
                if "relocat" in normalized:
                    return [option]

    desired = _deterministic_workday_question_value(label, "select", application_profile, options)
    if desired is not None:
        matched = select_option(options, desired)
        if matched:
            return [matched]

    matched_yes = select_option(options, "Yes")
    if matched_yes:
        return [matched_yes]

    return None


def _persist_workday_question_answers(
    out_dir: Path,
    question_specs: list[dict],
    answers: dict[str, object],
    *,
    provider: str | None,
) -> None:
    if not question_specs:
        return

    answers_path = role_submit_path(out_dir, APPLICATION_ANSWER_CACHE)
    answers_path.parent.mkdir(parents=True, exist_ok=True)
    existing_payload: dict[str, object] = {}
    if answers_path.exists():
        try:
            loaded = json.loads(answers_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict):
            existing_payload = dict(loaded)

    merged_answers = dict(existing_payload.get("answers") or {})
    merged_answers.update(answers)
    if not merged_answers:
        return

    payload = dict(existing_payload)
    payload["generated_at_utc"] = datetime.now(UTC).replace(microsecond=0).isoformat()
    payload["provider"] = str(existing_payload.get("provider") or provider or "deterministic_classification")
    payload["refresh_request_id"] = existing_payload.get("refresh_request_id")
    payload["questions"] = question_specs
    payload["answers"] = merged_answers
    answers_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _coerce_workday_checkbox_answers(raw_answer: object, options: list[str]) -> list[str]:
    from autofill_common import select_option

    if isinstance(raw_answer, list):
        raw_values = [str(value).strip() for value in raw_answer if str(value or "").strip()]
    elif isinstance(raw_answer, str) and raw_answer.strip():
        raw_values = [raw_answer.strip()]
    else:
        return []

    matched_values: list[str] = []
    for raw_value in raw_values:
        matched = select_option(options, raw_value)
        if matched and matched not in matched_values:
            matched_values.append(matched)
    return matched_values


def _apply_workday_checkbox_answers(page, option_labels: list[str]) -> bool:
    if not option_labels:
        return False
    applied = False
    for option_label in option_labels:
        if not _select_workday_checkbox_option(page, option_label):
            return False
        applied = True
    return applied


def _workday_dropdown_option_texts(page, dropdown) -> list[str]:
    if not dropdown.count():
        return []
    try:
        dropdown.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        dropdown.click()
        page.wait_for_timeout(300)
    except Exception:
        try:
            dropdown.click(force=True)
            page.wait_for_timeout(300)
        except Exception:
            return []

    option_locator = page.locator(
        "[data-automation-id='promptOption'], li[role='option'], [role='listbox'] li, .css-option"
    )
    option_texts: list[str] = []
    count = option_locator.count()
    for index in range(count):
        option = option_locator.nth(index)
        try:
            if not option.is_visible():
                continue
        except Exception:
            continue
        try:
            text = option.inner_text().strip()
        except Exception:
            continue
        if text and text not in option_texts:
            option_texts.append(text)
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
    except Exception:
        pass
    return option_texts


def _fill_application_questions(page, out_dir: Path, meta: dict, provider: str | None) -> list[dict]:
    """Fill custom application questions using LLM-generated answers."""
    filled = []

    # Collect visible questions
    question_groups = page.locator("[data-automation-id^='formField-']")
    question_specs = []
    count = question_groups.count()

    for i in range(count):
        group = question_groups.nth(i)
        label_el = group.locator(
            "label, [data-automation-id='formLabel'], legend [data-automation-id='richText'], legend, [id^='rich-label']"
        ).first
        if not label_el.count():
            continue
        label_text = re.sub(r"\s+", " ", label_el.inner_text()).replace("*", "").strip()
        if not label_text:
            continue
        field_name = slugify_label(label_text)
        # Detect field type
        has_date = _workday_question_has_date_input(group)
        has_textarea = group.locator("textarea").count() > 0
        has_select = (
            group.locator("select, [data-automation-id='selectWidget'], button[aria-haspopup='listbox']").count() > 0
        )
        has_radio = group.locator("input[type='radio']").count() > 0
        has_checkbox = group.locator("input[type='checkbox']").count() > 0
        kind = _workday_question_kind(
            has_date=has_date,
            has_textarea=has_textarea,
            has_select=has_select,
            has_radio=has_radio,
            has_checkbox=has_checkbox,
        )

        spec = {
            "group_index": i,
            "field_name": field_name,
            "label": label_text,
            "kind": kind,
            "required": True,
        }
        if kind == "checkbox":
            option_labels: list[str] = []
            checkboxes = group.locator("input[type='checkbox']")
            for checkbox_index in range(checkboxes.count()):
                checkbox = checkboxes.nth(checkbox_index)
                checkbox_id = checkbox.get_attribute("id") or ""
                if not checkbox_id:
                    continue
                option_label = group.locator(_workday_label_for_selector(checkbox_id)).first
                if not option_label.count():
                    option_label = page.locator(_workday_label_for_selector(checkbox_id)).first
                if not option_label.count():
                    continue
                option_text = re.sub(r"\s+", " ", option_label.inner_text()).strip()
                if option_text and option_text not in option_labels:
                    option_labels.append(option_text)
            if option_labels:
                spec["options"] = option_labels
            spec["type"] = "multi_value_multi_select"

        question_specs.append(spec)

    if not question_specs:
        return filled
    all_question_specs = [dict(spec) for spec in question_specs]
    persisted_answers: dict[str, object] = {}

    # Handle classifiable questions deterministically before LLM
    app_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    llm_specs = []
    blockers: list[dict[str, object]] = []
    for spec in question_specs:
        deterministic_value = None
        checkbox_values: list[str] | None = None
        group = question_groups.nth(spec["group_index"])
        if spec["kind"] == "select" and not spec.get("options"):
            dropdown = group.locator("button[aria-haspopup='listbox'], [data-automation-id='selectWidget']").first
            option_texts = _workday_dropdown_option_texts(page, dropdown)
            if option_texts:
                spec["options"] = option_texts
        if spec["kind"] == "checkbox":
            checkbox_values = _workday_checkbox_answer_values(spec["label"], spec.get("options", []), app_profile)
        else:
            deterministic_value = _deterministic_workday_question_value(
                spec["label"],
                spec["kind"],
                app_profile,
                spec.get("options"),
            )
        label_category = classify_question(spec["label"])
        if (
            deterministic_value is not None
            and spec["kind"] == "select"
            and label_category == "compensation"
            and _workday_numeric_only_compensation_options(spec.get("options"))
        ):
            if spec.get("required", True):
                blockers.append(_workday_numeric_only_compensation_blocker(spec, str(deterministic_value)))
            continue
        if deterministic_value is not None:
            fill_succeeded = False
            filled_value = deterministic_value
            if spec["kind"] == "textarea":
                textarea = _workday_question_textarea_field(group)
                if _workday_fill_if_visible(textarea, deterministic_value):
                    fill_succeeded = True
            elif spec["kind"] == "date":
                fill_succeeded = _fill_workday_question_date(group, deterministic_value)
            elif spec["kind"] == "text":
                text_input = _workday_question_text_field(group)
                if _workday_fill_if_visible(text_input, deterministic_value):
                    fill_succeeded = True
                normalized_label = normalize_text(spec["label"])
                is_compensation_prompt = classify_question(spec["label"]) == "compensation" or any(
                    fragment in normalized_label for fragment in ("salary", "compensation", "total rewards")
                )
                if not fill_succeeded and is_compensation_prompt:
                    numeric_fallback = getattr(app_profile, "compensation_numeric_fallback", None)
                    if numeric_fallback and _workday_fill_if_visible(text_input, numeric_fallback):
                        fill_succeeded = True
                        filled_value = numeric_fallback
            elif spec["kind"] == "select":
                dropdown = group.locator("button[aria-haspopup='listbox'], [data-automation-id='selectWidget']").first
                fill_succeeded = _fill_workday_dropdown_locator(page, dropdown, deterministic_value)
            elif spec["kind"] == "radio":
                fill_succeeded = _fill_workday_radio_group(group, deterministic_value)
            if fill_succeeded:
                persisted_answers[spec["field_name"]] = filled_value
                filled.append(
                    {
                        "field_name": spec["field_name"],
                        "label": spec["label"],
                        "value": filled_value,
                        "source": "application_profile.md",
                        "filled": True,
                    }
                )
            else:
                llm_specs.append(spec)
        elif checkbox_values:
            fill_succeeded = _apply_workday_checkbox_answers(page, checkbox_values)
            if fill_succeeded:
                persisted_answers[spec["field_name"]] = checkbox_values
                filled.append(
                    {
                        "field_name": spec["field_name"],
                        "label": spec["label"],
                        "value": checkbox_values,
                        "source": "application_profile.md",
                        "filled": True,
                    }
                )
            else:
                llm_specs.append(spec)
        else:
            llm_specs.append(spec)
    question_specs = llm_specs

    if blockers:
        _persist_workday_question_answers(out_dir, all_question_specs, persisted_answers, provider=provider)
        raise GeneratedAnswerBlockersError(blockers, valid_answers=persisted_answers)

    if not question_specs:
        _persist_workday_question_answers(out_dir, all_question_specs, persisted_answers, provider=provider)
        return filled

    # Generate answers using LLM
    answers = generate_application_answers(
        out_dir=out_dir,
        meta=meta,
        question_specs=question_specs,
        provider=provider,
    )

    # Fill answers
    for spec in question_specs:
        answer = answers.get(spec["field_name"], "")
        if not answer:
            continue

        group = question_groups.nth(spec["group_index"])
        if spec["kind"] == "textarea":
            textarea = _workday_question_textarea_field(group)
            if _workday_fill_if_visible(textarea, answer):
                filled.append(
                    {
                        "field_name": spec["field_name"],
                        "label": spec["label"],
                        "value": answer[:100],
                        "source": "generated",
                        "filled": True,
                    }
                )
        elif spec["kind"] == "date":
            if _fill_workday_question_date(group, answer):
                filled.append(
                    {
                        "field_name": spec["field_name"],
                        "label": spec["label"],
                        "value": answer[:100],
                        "source": "generated",
                        "filled": True,
                    }
                )
        elif spec["kind"] == "text":
            text_input = _workday_question_text_field(group)
            if _workday_fill_if_visible(text_input, answer):
                filled.append(
                    {
                        "field_name": spec["field_name"],
                        "label": spec["label"],
                        "value": answer[:100],
                        "source": "generated",
                        "filled": True,
                    }
                )
        elif spec["kind"] == "select":
            dropdown = group.locator("button[aria-haspopup='listbox'], [data-automation-id='selectWidget']").first
            if _fill_workday_dropdown_locator(page, dropdown, answer):
                filled.append(
                    {
                        "field_name": spec["field_name"],
                        "label": spec["label"],
                        "value": answer[:100],
                        "source": "generated",
                        "filled": True,
                    }
                )
        elif spec["kind"] == "radio":
            if _fill_workday_radio_group(group, answer):
                filled.append(
                    {
                        "field_name": spec["field_name"],
                        "label": spec["label"],
                        "value": answer[:100],
                        "source": "generated",
                        "filled": True,
                    }
                )
        elif spec["kind"] == "checkbox":
            checkbox_answers = _coerce_workday_checkbox_answers(answer, spec.get("options", []))
            if checkbox_answers and _apply_workday_checkbox_answers(page, checkbox_answers):
                persisted_answers[spec["field_name"]] = checkbox_answers
                filled.append(
                    {
                        "field_name": spec["field_name"],
                        "label": spec["label"],
                        "value": checkbox_answers,
                        "source": "generated",
                        "filled": True,
                    }
                )

    _persist_workday_question_answers(out_dir, all_question_specs, persisted_answers, provider=provider)
    return filled


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _state_from_location(location: str) -> str:
    """Extract state name from a location like 'San Francisco, CA'."""
    state_abbrevs = {
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
    if not location:
        return ""
    parts = [p.strip() for p in location.split(",")]
    for part in reversed(parts):
        abbrev = part.strip().upper()
        if abbrev in state_abbrevs:
            return state_abbrevs[abbrev]
        # Check if it's already a full state name
        for full_name in state_abbrevs.values():
            if part.strip().lower() == full_name.lower():
                return full_name
    return ""


def _page_snapshot_fn(page) -> dict:
    return page_snapshot(page, form_selector="[data-automation-id='applicationContainer']", captcha_type=None)


def _classify_submit_state(snapshot: dict) -> dict[str, object]:
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


def _capture(page, path: Path, *, preferred_selectors: tuple[str, ...] = ()) -> None:
    selectors = (*preferred_selectors, *PREFERRED_CAPTURE_SELECTORS)
    capture_full_page(page, path, preferred_selectors=selectors)


def _compose_workday_pre_submit_screenshot(page_screenshots_dir: Path, output_path: Path) -> None:
    page_paths = sorted(page_screenshots_dir.glob("page_*.png"))
    if not page_paths:
        raise FileNotFoundError(f"No Workday page screenshots found in {page_screenshots_dir}")
    concatenate_images_vertically(page_paths, output_path)


def _write_workday_review_artifacts(payload: dict, *, filled_steps: list[dict], page_screenshots_dir: Path) -> dict:
    pre_submit_path = Path(payload["artifacts"]["pre_submit_screenshot"])
    dedupe_page_screenshot_artifacts(
        [str(path) for path in sorted(page_screenshots_dir.glob("page_*.png")) if path.is_file()],
    )
    _compose_workday_pre_submit_screenshot(page_screenshots_dir, pre_submit_path)
    return write_report(
        payload,
        board_name="workday",
        runtime={
            "steps": list(filled_steps),
            "pages": [
                {"page_index": index, "screenshot": str(path)}
                for index, path in enumerate(sorted(page_screenshots_dir.glob("page_*.png")), start=1)
            ],
        },
    )


# ─── Payload building ───────────────────────────────────────────────────────


def _build_payload(out_dir: Path, provider: str | None = None) -> dict:
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)
    profile = parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))

    job_url = str(meta.get("jd_source_resolved") or meta["jd_source"])
    company_proper = str(meta.get("company_proper") or meta.get("company") or "")
    jd_title = str(meta.get("jd_title") or "")

    # Find resume and cover letter files
    try:
        resume_path = str(find_resume_file(out_dir))
    except FileNotFoundError:
        resume_path = ""
    try:
        cover_letter_path = str(find_cover_letter_file(out_dir))
    except FileNotFoundError:
        cover_letter_path = ""

    constants = board_file_constants("workday")
    role_submit_path(out_dir, "")

    return {
        "board": "workday",
        "job_url": job_url,
        "job_source": str(meta.get("source") or ""),
        "source_url": str(meta.get("source_url") or ""),
        "board_url": str(meta.get("board_url") or meta.get("jd_source") or job_url),
        "out_dir": str(out_dir),
        "company": company_proper,
        "company_slug": str(meta.get("company") or ""),
        "candidate_name": profile.full_name,
        "candidate_email": profile.email
        if "@" in (profile.email or "")
        else (getattr(application_profile, "verification_code_email", "") or ""),
        "verification_code_email": getattr(application_profile, "verification_code_email", "") or profile.email,
        "job_title": jd_title,
        "provider": provider,
        "resume_path": resume_path,
        "cover_letter_path": cover_letter_path,
        "fields": [],
        "steps": [],
        "artifacts": {
            "payload_json": str(role_submit_path(out_dir, constants["payload_json"])),
            "report_json": str(role_submit_path(out_dir, constants["report_json"])),
            "report_markdown": str(role_submit_path(out_dir, constants["report_md"])),
            "pre_submit_screenshot": str(role_submit_path(out_dir, constants["pre_submit_screenshot"])),
            "page_screenshots_dir": str(role_submit_path(out_dir, constants["page_screenshots_dir"])),
            "submit_debug_html": str(role_submit_path(out_dir, constants["submit_debug_html"])),
            "submit_debug_screenshot": str(role_submit_path(out_dir, constants["submit_debug_screenshot"])),
        },
    }


def _write_workday_pending_user_input_for_generated_answer_blockers(
    out_dir: Path,
    payload: dict,
    blockers: list[dict],
) -> Path:
    pending_path = write_pending_user_input_for_unconfirmed_fields(
        out_dir,
        board="workday",
        fields=blockers,
        report_json=str(payload["artifacts"].get("report_json") or ""),
        report_markdown=str(payload["artifacts"].get("report_markdown") or ""),
        pre_submit_screenshot=str(payload["artifacts"].get("pre_submit_screenshot") or ""),
    )
    if pending_path is None:
        raise ValueError("Workday generated-answer blockers did not produce pending_user_input.json")
    return pending_path


# ─── Custom browser pipeline ────────────────────────────────────────────────


def _run_workday_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    """Custom browser pipeline for Workday multi-page wizard."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: Playwright is not installed.", file=sys.stderr)
        return 1

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    out_dir = Path(payload["out_dir"])
    meta = load_meta(out_dir)
    profile = parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    email, password = _workday_credentials()
    _clear_workday_failure_artifacts(payload)

    page_screenshots_dir = Path(payload["artifacts"]["page_screenshots_dir"])
    page_screenshots_dir.mkdir(parents=True, exist_ok=True)
    page_idx = 0

    all_filled: list[dict] = []

    with sync_playwright() as playwright:
        viewport = submit_viewport()
        browser = launch_chromium_browser(
            playwright,
            headless=headless,
            slow_mo=submit_slow_mo_ms(headless),
            channel_env_var="JOB_ASSETS_SUBMIT_BROWSER_CHANNEL",
            executable_env_var="JOB_ASSETS_SUBMIT_BROWSER_EXECUTABLE",
            persistent_profile_dir=submit_browser_profile_dir(),
            prefer_local_browser=True,
            viewport=viewport,
            device_scale_factor=2,
            purpose="Workday autofill",
        )
        page = browser.new_page(viewport=viewport, device_scale_factor=2)

        try:
            # --- Phase 1: Navigate to job page ---
            page.goto(payload["job_url"], wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            if _is_workday_already_applied_job_page(page):
                print("Workday: job already shows as applied — skipping draft rerun.", file=sys.stderr)
                _write_workday_already_applied_result(out_dir, payload)
                return 0

            # --- Phase 2: Authenticate ---
            print("Workday: authenticating...", file=sys.stderr)
            auth_result = _handle_auth(
                page, email, password, payload=payload, job_url=payload["job_url"], out_dir=out_dir
            )
            if not auth_result.get("ok"):
                print(
                    "Workday: authentication did not reach the application flow. Logging for user review.",
                    file=sys.stderr,
                )
                debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
                _capture(page, debug_png)
                _write_workday_auth_result(out_dir, auth_result["result"])
                return 0
            print("Workday: authentication succeeded.", file=sys.stderr)
            if auth_result.get("already_applied") or _is_workday_already_applied_job_page(page):
                print("Workday: job already shows as applied after authentication.", file=sys.stderr)
                _write_workday_already_applied_result(out_dir, payload)
                return 0

            # --- Phase 3: Page-by-page form filling ---
            max_pages = 15  # Safety limit
            prev_page = None
            stuck_count = 0
            for page_attempt in range(max_pages):
                page.wait_for_timeout(2000)
                if _is_workday_already_applied_job_page(page):
                    print("Workday: job already shows as applied during wizard recovery.", file=sys.stderr)
                    _write_workday_already_applied_result(out_dir, payload)
                    return 0
                if not _is_application_page(page):
                    current_auth = _extract_workday_auth_markers(page)
                    if current_auth.get("auth_state") in {
                        "authenticated_non_form",
                        "create_account_gate",
                        "credential_rejected",
                        "maintenance",
                        "password_reset_gate",
                        "sign_in_gate",
                    }:
                        print(
                            "Workday: encountered auth gate during wizard, retrying approved auth flow...",
                            file=sys.stderr,
                        )
                        auth_result = _run_workday_auth_flow(
                            page,
                            email,
                            password,
                            payload=payload,
                            job_url=payload["job_url"],
                            out_dir=out_dir,
                            enter_application_flow=False,
                        )
                        if auth_result.get("ok"):
                            continue
                        debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
                        _capture(page, debug_png)
                        _write_workday_auth_result(out_dir, auth_result["result"])
                        return 0
                current_page = _detect_current_page(page)
                print(f"Workday: on page '{current_page}' (step {page_attempt + 1})", file=sys.stderr)

                # Detect stuck loop: same page repeated after clicking Next
                if current_page == prev_page and current_page != PAGE_CREATE_ACCOUNT:
                    stuck_count += 1
                    if stuck_count >= 3:
                        print(
                            f"Workday: stuck on '{current_page}' for {stuck_count} iterations — validation errors likely unresolvable.",
                            file=sys.stderr,
                        )
                        if _maybe_write_truthful_workday_stuck_result(
                            page,
                            out_dir,
                            payload,
                            current_page=current_page,
                        ):
                            return 0
                        debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
                        debug_html = Path(payload["artifacts"]["submit_debug_html"])
                        _capture(page, debug_png)
                        debug_html.write_text(page.content(), encoding="utf-8")
                        validation_errors = _visible_workday_validation_errors(page)
                        failure_type, failure_message = _workday_validation_failure_for_page(current_page)
                        _write_workday_failed_result(
                            out_dir,
                            payload,
                            failure_type=failure_type,
                            message=failure_message,
                            current_page=current_page,
                            validation_errors=validation_errors or ["Unknown validation error"],
                        )
                        return 1
                else:
                    stuck_count = 0
                prev_page = current_page

                # Screenshot each page
                page_idx += 1
                page_path = page_screenshots_dir / f"page_{page_idx:02d}_{current_page}.png"

                if current_page == PAGE_MY_INFO:
                    filled = _fill_my_information(page, profile, application_profile, payload)
                    all_filled.extend(filled)
                    if _is_workday_public_profile_submit_boundary(page):
                        experience_filled = _fill_my_experience(page, out_dir)
                        all_filled.extend(experience_filled)
                        return _handle_workday_review_boundary(
                            page,
                            page_path=page_path,
                            payload=payload,
                            filled_steps=all_filled,
                            page_screenshots_dir=page_screenshots_dir,
                            submit=submit,
                            headless=headless,
                        )
                elif current_page == PAGE_EXPERIENCE:
                    filled = _fill_my_experience(page, out_dir)
                    all_filled.extend(filled)
                elif current_page == PAGE_APPLICATION_QUESTIONS:
                    try:
                        filled = _fill_application_questions(page, out_dir, meta, payload.get("provider"))
                    except GeneratedAnswerBlockersError as exc:
                        _capture(page, page_path)
                        pending_path = _write_workday_pending_user_input_for_generated_answer_blockers(
                            out_dir,
                            payload,
                            exc.blockers,
                        )
                        print(
                            f"Workday: generated answers require manual review. See {pending_path.relative_to(PROJECT_ROOT)}.",
                            file=sys.stderr,
                        )
                        return 0
                    all_filled.extend(filled)
                elif current_page in (PAGE_VOLUNTARY_DISCLOSURES, PAGE_SELF_IDENTIFY):
                    filled = _fill_voluntary_disclosures(page, application_profile, profile=profile)
                    all_filled.extend(filled)
                elif current_page == PAGE_REVIEW:
                    return _handle_workday_review_boundary(
                        page,
                        page_path=page_path,
                        payload=payload,
                        filled_steps=all_filled,
                        page_screenshots_dir=page_screenshots_dir,
                        submit=submit,
                        headless=headless,
                    )

                if current_page != PAGE_REVIEW:
                    _capture(page, page_path)

                # Click Next to proceed to next page
                if current_page != PAGE_REVIEW:
                    if not _click_next_button(page):
                        # Maybe we're already past the wizard
                        if _is_confirmation_page(page):
                            snapshot = _page_snapshot_fn(page)
                            state = _classify_submit_state(snapshot)
                            if state["status"] == "confirmed":
                                outcome = {"status": "confirmed", "reason": "text", "snapshot": snapshot}
                                sync_notion_after_submit(payload, outcome, provider="workday")
                                reply_to_confirmation_email(payload, board_name="workday")
                                return 0
                        print("Workday: could not find Next button.", file=sys.stderr)
                        # Take a debug screenshot and continue trying
                        debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
                        _capture(page, debug_png)

            # Exhausted max pages
            print("Workday: exceeded max page attempts.", file=sys.stderr)
            return 1

        finally:
            browser.close()


def _is_confirmation_page(page) -> bool:
    text = page.inner_text("body")[:3000].lower()
    return any(p.search(text) for p in SUBMIT_CONFIRM_PATTERNS)


# ─── Entry point ─────────────────────────────────────────────────────────────


def main() -> int:
    return autofill_main(
        board_name="workday",
        build_payload_fn=_build_payload,
        run_browser_fn=_run_workday_browser,
    )


if __name__ == "__main__":
    raise SystemExit(main())
