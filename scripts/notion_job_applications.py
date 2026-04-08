#!/usr/bin/env python3
"""Upsert applied roles into the Job Applications Notion database."""

from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from app_paths import jobs_db_path, material_path, output_root
from job_board_urls import (
    ashby_job_id_from_url,
    dover_job_or_search_id_from_url,
    looks_like_ashby_wrapper_url,
    looks_like_dover_url,
)
from output_layout import (
    ACTIVE_SUBMIT_DIR_ENV,
    SUBMIT_DIRNAME,
    active_submit_dir_name,
    find_role_file,
    glob_role_files,
    migrate_role_output_layout,
    preferred_submit_dir_name_for_post_submit,
    role_submit_path,
    set_active_submit_dir,
    submit_dirs_by_mtime,
)
from project_env import load_project_env

load_project_env()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PROJECT_ROOT = PROJECT_ROOT
OUTPUT_ROOT = output_root()
APPLICATION_PROFILE_PATH = material_path("application_profile.md")


def _runtime_output_root() -> Path:
    if PROJECT_ROOT != _DEFAULT_PROJECT_ROOT:
        return PROJECT_ROOT / "output"
    return output_root()


def _runtime_application_profile_path() -> Path:
    if PROJECT_ROOT != _DEFAULT_PROJECT_ROOT:
        return PROJECT_ROOT / "application_profile.md"
    return material_path("application_profile.md")


def _runtime_jobs_db_path() -> Path:
    if PROJECT_ROOT != _DEFAULT_PROJECT_ROOT:
        return PROJECT_ROOT / "jobs.db"
    return jobs_db_path()

DEFAULT_NOTION_DATABASE_ID = "2e238885-a751-80cd-bd2c-da1a28dc3edb"
DEFAULT_NOTION_DATA_SOURCE_ID = "2e238885-a751-802d-8274-000bd78e05b4"
DEFAULT_NOTION_VERSION = "2026-03-11"
DEFAULT_GWS_TIMEOUT_SECONDS = 10
DEFAULT_GWS_SCAN_TIMEOUT_SECONDS = 15
DEFAULT_GWS_MESSAGE_FETCH_LIMIT = 6
DEFAULT_GWS_QUERY_MAX_RESULTS = 6

SUBMISSION_RESULT_JSON = "application_submission_result.json"
WEBSITE_CONFIRMATION_JSON = "application_confirmation_website.json"
EMAIL_CONFIRMATION_JSON = "application_confirmation_email.json"
NOTION_SYNC_STATUS_JSON = "notion_sync_status.json"
NOTION_SYNC_RESULT_JSON = NOTION_SYNC_STATUS_JSON

NORMALIZED_TEXT_RE = re.compile(r"[^a-z0-9]+")
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
URL_RE = re.compile(r"^https?://", re.I)
EMAIL_CONFIRMATION_PATTERNS = (
    re.compile(r"\bthank(?:s| you)(?:\s+so\s+much)?\s+for\s+applying\b", re.I),
    re.compile(r"\bapplication (?:has been )?(?:received|submitted)\b", re.I),
    re.compile(r"\bwe(?:'|’)ve received your application\b", re.I),
    re.compile(r"\bwe have received your application\b", re.I),
    re.compile(r"\bwe received your application\b", re.I),
    re.compile(r"\bapplication confirmation\b", re.I),
)
GENERIC_JOB_TITLE_TOKENS = {
    "agent",
    "agents",
    "ai",
    "analyst",
    "associate",
    "director",
    "engineer",
    "growth",
    "head",
    "here",
    "insert",
    "lead",
    "manager",
    "platform",
    "pm",
    "principal",
    "product",
    "senior",
    "software",
    "staff",
    "technical",
    "role",
    "your",
    "apply",
    "excel",
}
PROVIDER_EMAIL_MARKERS = {
    "greenhouse:": ("greenhouse", "mygreenhouse", "greenhouse-mail"),
    "ashby:": ("ashby", "ashbyhq"),
    "gem:": ("gem",),
    "lever:": ("lever",),
    "dover:": ("dover",),
}

EXIT_SUCCESS = 0
EXIT_SYNC_FAILED = 1
EXIT_PENDING_EMAIL = 2
EXIT_MISSING_NOTION_TOKEN = 3
EXIT_WEBSITE_NOT_CONFIRMED = 4


def _json_dumps_pretty(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=False)


def _read_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_result(path: Path, payload: dict) -> None:
    path.write_text(_json_dumps_pretty(payload) + "\n", encoding="utf-8")


@contextmanager
def _submit_dir_override(dirname: str | None):
    if not dirname:
        yield
        return
    previous = os.environ.get(ACTIVE_SUBMIT_DIR_ENV)
    os.environ[ACTIVE_SUBMIT_DIR_ENV] = dirname
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(ACTIVE_SUBMIT_DIR_ENV, None)
        else:
            os.environ[ACTIVE_SUBMIT_DIR_ENV] = previous


def record_website_confirmation(out_dir: Path, outcome: dict, *, provider: str = "generic") -> dict:
    out_dir = Path(out_dir)
    migrate_role_output_layout(out_dir)
    snapshot = outcome.get("snapshot") or {}
    excerpt = _collapse_whitespace(str(snapshot.get("page_text") or outcome.get("page_excerpt") or ""))[:500]
    confirmed_at_utc = str(outcome.get("confirmed_at_utc") or outcome.get("confirmed_at") or "").strip()
    if not confirmed_at_utc:
        confirmed_at_utc = _now_utc_iso()
    payload = {
        "status": "confirmed" if outcome.get("status") == "confirmed" else str(outcome.get("status") or "unknown"),
        "website_confirmed": outcome.get("status") == "confirmed",
        "provider": provider,
        "confirmed_at_utc": confirmed_at_utc,
        "reason": outcome.get("reason"),
        "url": str(snapshot.get("url") or ""),
        "errors": list(outcome.get("errors") or []),
        "invalid_fields": list(outcome.get("invalid_fields") or []),
        "page_excerpt": excerpt,
    }
    _write_result(role_submit_path(out_dir, WEBSITE_CONFIRMATION_JSON), payload)
    _write_result(role_submit_path(out_dir, SUBMISSION_RESULT_JSON), payload)
    if active_submit_dir_name(out_dir) != SUBMIT_DIRNAME:
        set_active_submit_dir(out_dir, SUBMIT_DIRNAME)
    return payload


def _normalized_text(value: str | None) -> str:
    return NORMALIZED_TEXT_RE.sub("", (value or "").casefold())


def _collapse_whitespace(value: str) -> str:
    return WHITESPACE_RE.sub(" ", value).strip()


def _strip_html(value: str) -> str:
    return _collapse_whitespace(html_lib.unescape(HTML_TAG_RE.sub(" ", value or "")))


def _now_utc_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _parse_profile_value(text: str, label: str) -> str | None:
    prefix = f"{label.casefold()}:"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        if line.casefold().startswith(prefix):
            value = line.split(":", 1)[1].strip()
            return value or None
    return None


def _verification_email() -> str | None:
    profile_path = _runtime_application_profile_path()
    if not profile_path.exists():
        return None
    text = profile_path.read_text(encoding="utf-8")
    return _parse_profile_value(text, "Verification Code Email") or _parse_profile_value(text, "Email")


def _find_output_dir(target: str) -> Path:
    candidate = Path(target).expanduser()
    if candidate.is_dir() and (candidate / ".pipeline_meta.json").exists():
        resolved = candidate.resolve()
        migrate_role_output_layout(resolved)
        return resolved
    if candidate.is_file() and candidate.name == ".pipeline_meta.json":
        resolved = candidate.resolve().parent
        migrate_role_output_layout(resolved)
        return resolved

    output_root_path = _runtime_output_root()
    if not output_root_path.exists():
        raise FileNotFoundError(f"Could not find output directory for {target!r}")

    for meta_path in output_root_path.glob("*/*/.pipeline_meta.json"):
        meta = _read_json(meta_path)
        if not isinstance(meta, dict):
            continue
        if target == meta.get("jd_source"):
            resolved = meta_path.parent.resolve()
            migrate_role_output_layout(resolved)
            return resolved
        if target == str(meta_path.parent):
            resolved = meta_path.parent.resolve()
            migrate_role_output_layout(resolved)
            return resolved

    raise FileNotFoundError(f"Could not resolve {target!r} to an output directory with .pipeline_meta.json")


def _job_url_identity(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    host = parsed.netloc.casefold()
    path = parsed.path.rstrip("/")
    query = parse_qs(parsed.query)

    if "greenhouse.io" in host:
        job_id = None
        if "token" in query and query["token"]:
            job_id = query["token"][0]
        else:
            match = re.search(r"/jobs/(\d+)$", path)
            if match:
                job_id = match.group(1)
        if job_id:
            return f"greenhouse:{job_id}"

    if "ashbyhq.com" in host or looks_like_ashby_wrapper_url(url):
        job_id = ashby_job_id_from_url(url)
        if job_id:
            return f"ashby:{job_id.casefold()}"

    if "jobs.gem.com" in host:
        tail = path.split("/")[-1]
        if tail:
            return f"gem:{tail.casefold()}"

    if "lever.co" in host:
        parts = [part for part in path.split("/") if part]
        if parts and parts[-1].casefold() == "apply":
            parts = parts[:-1]
        if len(parts) >= 2:
            return f"lever:{parts[-1].casefold()}"

    if looks_like_dover_url(url):
        job_id = dover_job_or_search_id_from_url(url)
        if job_id:
            return f"dover:{job_id.casefold()}"

    cleaned_query: list[tuple[str, str]] = []
    for key, values in sorted(query.items()):
        if key.casefold().startswith("utm_"):
            continue
        if key.casefold() in {"gh_src", "source"}:
            continue
        for value in values:
            cleaned_query.append((key, value))
    normalized_url = urlunparse(
        (
            parsed.scheme.casefold(),
            host,
            path,
            "",
            "&".join(f"{key}={value}" for key, value in cleaned_query),
            "",
        )
    )
    return normalized_url


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _normalize_page_title(title: str, company: str) -> str:
    cleaned = _collapse_whitespace(title).rstrip()
    if company:
        suffix = f" at {company}".casefold()
        if cleaned.casefold().endswith(suffix):
            return cleaned[: -len(suffix)].strip() + f" | {company}"
    return cleaned


def _candidate_page_titles(meta: dict, out_dir: Path) -> list[str]:
    titles: list[str] = []
    company = str(meta.get("company_proper") or meta.get("company") or "").strip()

    for html_path in sorted(glob_role_files(out_dir, "*application_page.html", bucket="submit")):
        match = TITLE_RE.search(html_path.read_text(encoding="utf-8", errors="ignore"))
        if not match:
            continue
        raw_title = html_lib.unescape(match.group(1))
        # Skip placeholder titles that Greenhouse JS would normally populate.
        if raw_title.strip().casefold() in ("page_title", ""):
            continue
        titles.append(_normalize_page_title(raw_title, company))
        titles.append(_collapse_whitespace(raw_title).rstrip())

    job_title = str(meta.get("jd_title") or "").strip()
    if job_title and company:
        titles.extend(
            [
                f"{job_title} | {company}",
                f"Application for {job_title} | {company}",
                f"Job Application for {job_title} | {company}",
            ]
        )
    elif job_title:
        titles.append(job_title)

    deduped: list[str] = []
    seen = set()
    for title in titles:
        key = _normalized_text(title)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(title)
    return deduped


def _infer_job_type(meta: dict, jd_parsed: dict | None) -> str | None:
    signals = " ".join(
        filter(
            None,
            [
                str(meta.get("jd_title") or ""),
                str(meta.get("company_proper") or ""),
                str((jd_parsed or {}).get("summary") or ""),
                " ".join((jd_parsed or {}).get("keywords") or []),
            ],
        )
    ).casefold()
    if "technical product manager" in signals or re.search(r"\btpm\b", signals):
        return "Technical PM"
    if "growth" in signals:
        return "Growth PM"
    if "enterprise" in signals:
        return "Enterprise PM"
    if "consumer" in signals:
        return "Consumer PM"
    if "data" in signals or "analytics" in signals:
        return "Data PM"
    return None


def _run_gws_json(args: list[str]) -> dict:
    import subprocess

    timeout_seconds = DEFAULT_GWS_TIMEOUT_SECONDS
    raw_timeout = os.environ.get("JOB_ASSETS_GWS_TIMEOUT_SECONDS", "").strip()
    if raw_timeout:
        try:
            timeout_seconds = max(int(raw_timeout), 1)
        except ValueError:
            timeout_seconds = DEFAULT_GWS_TIMEOUT_SECONDS
    try:
        completed = subprocess.run(
            ["gws", *args],
            cwd=str(PROJECT_ROOT),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "googleworkspace/cli timed out while fetching Gmail messages. "
            "Make sure `gws auth login` is complete and the Gmail API is reachable. "
            f"Command: {' '.join(['gws', *args])}. Timeout: {timeout_seconds}s."
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(
            "googleworkspace/cli failed while fetching Gmail messages. "
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


def _iter_gmail_parts(part: dict) -> list[dict]:
    parts = [part]
    for child in part.get("parts", []) or []:
        parts.extend(_iter_gmail_parts(child))
    return parts


def _decode_gmail_body(data: str | None) -> str:
    if not data:
        return ""
    import base64

    padding = "=" * (-len(data) % 4)
    raw = base64.urlsafe_b64decode(data + padding)
    return raw.decode("utf-8", errors="ignore")


def _gmail_header_value(message: dict, name: str) -> str:
    headers = ((message.get("payload") or {}).get("headers")) or []
    for header in headers:
        if str(header.get("name") or "").casefold() == name.casefold():
            return str(header.get("value") or "")
    return ""


def _gmail_message_text(message: dict) -> str:
    payload = message.get("payload") or {}
    texts: list[str] = [html_lib.unescape(str(message.get("snippet") or ""))]
    for part in _iter_gmail_parts(payload):
        mime_type = str(part.get("mimeType") or "")
        body_data = (part.get("body") or {}).get("data")
        if mime_type in {"text/plain", "text/html"} and body_data:
            text = _decode_gmail_body(body_data)
            if mime_type == "text/html":
                text = _strip_html(text)
            texts.append(text)
    return _collapse_whitespace(" ".join(texts))


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


def _provider_marker_present(url_identity: str, normalized_text: str) -> bool:
    for prefix, markers in PROVIDER_EMAIL_MARKERS.items():
        if not url_identity.startswith(prefix):
            continue
        return any(marker in normalized_text for marker in markers)
    return False


def _provider_query_terms(url_identity: str) -> tuple[str, ...]:
    for prefix, markers in PROVIDER_EMAIL_MARKERS.items():
        if url_identity.startswith(prefix):
            return markers
    return ()


def _job_title_query_terms(job_title: str, *, max_terms: int = 2) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[A-Za-z0-9]+", job_title):
        normalized = token.casefold()
        if len(normalized) < 4 or normalized in GENERIC_JOB_TITLE_TOKENS or normalized in seen:
            continue
        seen.add(normalized)
        terms.append(normalized)
        if len(terms) >= max_terms:
            break
    return terms


def _gmail_after_query_term(min_received_at_utc: datetime | str | None) -> str | None:
    min_received_at = _coerce_utc_datetime(min_received_at_utc)
    if min_received_at is None:
        return None
    return f"after:{min_received_at.strftime('%Y/%m/%d')}"


def _email_confirmation_query_specs(
    meta: dict,
    *,
    min_received_at_utc: datetime | str | None = None,
) -> list[dict[str, object]]:
    company = str(meta.get("company_proper") or meta.get("company") or "").strip()
    job_title = str(meta.get("jd_title") or "").strip()
    url_identity = _job_url_identity(str(meta.get("jd_source") or ""))
    after_term = _gmail_after_query_term(min_received_at_utc)
    provider_terms = _provider_query_terms(url_identity)
    title_terms = _job_title_query_terms(job_title)
    max_results = DEFAULT_GWS_QUERY_MAX_RESULTS
    seen_queries: set[str] = set()
    query_specs: list[dict[str, object]] = []
    sent_exclusion = "-in:sent"

    def add_query(*terms: str) -> None:
        query = " ".join(term for term in terms if term).strip()
        if not query or query in seen_queries:
            return
        seen_queries.add(query)
        query_specs.append({"q": query, "max_results": max_results})

    company_term = f'"{company}"' if company else ""
    title_fragment = " ".join(f'"{term}"' for term in title_terms)

    if company_term:
        add_query(after_term or "", sent_exclusion, company_term, '"thank you for applying"')
        add_query(after_term or "", sent_exclusion, company_term, "application")
    if company_term and title_fragment:
        add_query(after_term or "", sent_exclusion, company_term, title_fragment, "application")
    if company_term and provider_terms:
        add_query(after_term or "", sent_exclusion, company_term, provider_terms[0], "application")
    elif provider_terms:
        add_query(after_term or "", sent_exclusion, provider_terms[0], "application")
    add_query(
        after_term or "",
        sent_exclusion,
        "application",
        title_fragment,
    )
    if not query_specs:
        query_specs.append(
            {
                "q": (
                    'newer_than:14d -in:sent '
                    '(application OR applying OR "thank you for applying" OR "received your application")'
                ),
                "max_results": max_results,
            }
        )
    return query_specs


def _job_title_token_hits(job_title: str, text_words: set[str]) -> tuple[int, int]:
    meaningful_hits = 0
    generic_hits = 0
    for token in re.findall(r"[A-Za-z0-9]+", job_title):
        token = token.casefold()
        if len(token) < 4 or token not in text_words:
            continue
        if token in GENERIC_JOB_TITLE_TOKENS:
            generic_hits += 1
        else:
            meaningful_hits += 1
    return meaningful_hits, generic_hits


def _email_confirmation_score(message: dict, *, company: str, job_title: str, url_identity: str) -> int:
    subject = _gmail_header_value(message, "Subject")
    sender = _gmail_header_value(message, "From")
    text = " ".join([subject, sender, _gmail_message_text(message)])
    normalized_text = _normalized_text(text)
    text_words = {token.casefold() for token in re.findall(r"[A-Za-z0-9]+", text)}
    score = 0

    if any(pattern.search(text) for pattern in EMAIL_CONFIRMATION_PATTERNS):
        score += 50
    company_match = bool(company and _normalized_text(company) in normalized_text)
    provider_match = _provider_marker_present(url_identity, normalized_text)
    meaningful_title_hits, generic_title_hits = _job_title_token_hits(job_title, text_words)

    if not company_match and meaningful_title_hits < 2:
        return 0

    if company_match:
        score += 25
    score += min(meaningful_title_hits, 4) * 8
    score += min(generic_title_hits, 2) * 2

    if provider_match:
        score += 10
    if "applicant" in text.casefold():
        score += 4
    return score


def _find_email_confirmation(meta: dict, *, min_received_at_utc: datetime | str | None = None) -> dict | None:
    company = str(meta.get("company_proper") or meta.get("company") or "")
    job_title = str(meta.get("jd_title") or "")
    url_identity = _job_url_identity(str(meta.get("jd_source") or ""))
    best: dict | None = None
    min_received_at = _coerce_utc_datetime(min_received_at_utc)
    scan_timeout_seconds = DEFAULT_GWS_SCAN_TIMEOUT_SECONDS
    raw_scan_timeout = os.environ.get("JOB_ASSETS_GWS_SCAN_TIMEOUT_SECONDS", "").strip()
    if raw_scan_timeout:
        try:
            scan_timeout_seconds = max(int(raw_scan_timeout), 1)
        except ValueError:
            scan_timeout_seconds = DEFAULT_GWS_SCAN_TIMEOUT_SECONDS
    fetch_limit = DEFAULT_GWS_MESSAGE_FETCH_LIMIT
    raw_fetch_limit = os.environ.get("JOB_ASSETS_GWS_MESSAGE_FETCH_LIMIT", "").strip()
    if raw_fetch_limit:
        try:
            fetch_limit = max(int(raw_fetch_limit), 1)
        except ValueError:
            fetch_limit = DEFAULT_GWS_MESSAGE_FETCH_LIMIT

    scan_deadline = time.monotonic() + scan_timeout_seconds
    message_ids: list[str] = []
    seen_message_ids: set[str] = set()

    for query_spec in _email_confirmation_query_specs(meta, min_received_at_utc=min_received_at):
        if time.monotonic() >= scan_deadline or len(message_ids) >= fetch_limit:
            break
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
                        "maxResults": int(query_spec["max_results"]),
                        "q": str(query_spec["q"]),
                    }
                ),
            ]
        )
        for item in list_response.get("messages", []) or []:
            message_id = str(item.get("id") or "").strip()
            if not message_id or message_id in seen_message_ids:
                continue
            seen_message_ids.add(message_id)
            message_ids.append(message_id)
            if len(message_ids) >= fetch_limit:
                break

    if not message_ids:
        return None

    for message_id in message_ids:
        if time.monotonic() >= scan_deadline:
            break
        message = _run_gws_json(
            [
                "gmail",
                "users",
                "messages",
                "get",
                "--params",
                json.dumps(
                    {
                        "userId": "me",
                        "id": message_id,
                        "format": "metadata",
                        "metadataHeaders": ["Subject", "From", "Date"],
                    }
                ),
            ]
        )
        label_ids = {str(label).upper() for label in (message.get("labelIds") or [])}
        if "SENT" in label_ids:
            continue
        score = _email_confirmation_score(
            message,
            company=company,
            job_title=job_title,
            url_identity=url_identity,
        )
        if score < 60:
            continue
        received_at = _gmail_message_received_at_utc(message)
        if min_received_at is not None and received_at is not None and received_at < min_received_at:
            continue
        excerpt = _gmail_message_text(message)[:500]
        candidate = {
            "message_id": message_id,
            "thread_id": message.get("threadId"),
            "subject": _gmail_header_value(message, "Subject"),
            "from": _gmail_header_value(message, "From"),
            "date": _gmail_header_value(message, "Date"),
            "received_at_utc": received_at.isoformat() if received_at else None,
            "snippet": str(message.get("snippet") or ""),
            "excerpt": excerpt,
            "score": score,
        }
        if best is None or score > best["score"]:
            best = candidate

    return best


def find_email_confirmation(
    out_dir: Path,
    *,
    min_received_at_utc: datetime | str | None = None,
    write_artifact: bool = True,
) -> dict | None:
    out_dir = Path(out_dir)
    migrate_role_output_layout(out_dir)
    meta = _read_json(out_dir / ".pipeline_meta.json")
    if not isinstance(meta, dict):
        raise RuntimeError(f"Missing or invalid .pipeline_meta.json in {out_dir}")
    email_confirmation = _find_email_confirmation(meta, min_received_at_utc=min_received_at_utc)
    if email_confirmation and write_artifact:
        _write_result(role_submit_path(out_dir, EMAIL_CONFIRMATION_JSON), email_confirmation)
    elif write_artifact:
        try:
            role_submit_path(out_dir, EMAIL_CONFIRMATION_JSON).unlink()
        except FileNotFoundError:
            pass
    return email_confirmation


def wait_for_email_confirmation(
    out_dir: Path,
    *,
    timeout_seconds: int = 0,
    poll_interval_seconds: int = 5,
    min_received_at_utc: datetime | str | None = None,
) -> dict | None:
    deadline = time.monotonic() + max(timeout_seconds, 0)
    while True:
        email_confirmation = find_email_confirmation(
            out_dir,
            min_received_at_utc=min_received_at_utc,
            write_artifact=True,
        )
        if email_confirmation:
            return email_confirmation
        if time.monotonic() >= deadline:
            return None
        remaining = max(deadline - time.monotonic(), 0)
        time.sleep(min(max(poll_interval_seconds, 1), remaining))


class NotionApiError(RuntimeError):
    pass


class NotionClient:
    def __init__(self, *, token: str, data_source_id: str, database_id: str, notion_version: str) -> None:
        self.token = token
        self.data_source_id = data_source_id
        self.database_id = database_id
        self.notion_version = notion_version
        self._data_source_mode: str | None = None

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"https://api.notion.com/v1{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Notion-Version": self.notion_version,
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise NotionApiError(f"{method} {path} failed with {exc.code}: {detail}") from exc
        except URLError as exc:
            raise NotionApiError(f"{method} {path} failed: {exc}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise NotionApiError(f"{method} {path} returned non-JSON output: {raw[:400]}") from exc

    def retrieve_schema(self) -> dict:
        errors: list[str] = []
        for mode, path in (
            ("data_source", f"/data_sources/{self.data_source_id}"),
            ("database", f"/databases/{self.database_id}"),
        ):
            try:
                data = self._request("GET", path)
                self._data_source_mode = mode
                return data
            except NotionApiError as exc:
                errors.append(str(exc))
        raise NotionApiError("Could not retrieve Notion schema. " + " | ".join(errors))

    def query_pages(self) -> list[dict]:
        all_results: list[dict] = []
        start_cursor: str | None = None
        mode = self._data_source_mode or "data_source"
        path = (
            f"/data_sources/{self.data_source_id}/query"
            if mode == "data_source"
            else f"/databases/{self.database_id}/query"
        )

        while True:
            payload = {"page_size": 100}
            if start_cursor:
                payload["start_cursor"] = start_cursor
            data = self._request("POST", path, payload)
            all_results.extend(data.get("results", []) or [])
            if not data.get("has_more"):
                return all_results
            start_cursor = data.get("next_cursor")

    def update_page(self, page_id: str, properties: dict) -> dict:
        return self._request("PATCH", f"/pages/{page_id}", {"properties": properties})

    def create_page(self, properties: dict, children: list[dict]) -> dict:
        errors: list[str] = []
        payload = {"properties": properties, "children": children[:100]}
        for parent in (
            {"data_source_id": self.data_source_id},
            {"database_id": self.database_id},
        ):
            try:
                return self._request("POST", "/pages", {"parent": parent, **payload})
            except NotionApiError as exc:
                errors.append(str(exc))
        raise NotionApiError("Could not create Notion page. " + " | ".join(errors))

    def list_block_children(self, block_id: str) -> list[dict]:
        all_results: list[dict] = []
        start_cursor: str | None = None
        while True:
            params = {"page_size": 20}
            if start_cursor:
                params["start_cursor"] = start_cursor
            data = self._request("GET", f"/blocks/{block_id}/children?{urlencode(params)}")
            all_results.extend(data.get("results", []) or [])
            if not data.get("has_more"):
                return all_results
            start_cursor = data.get("next_cursor")

    def append_block_children(self, block_id: str, children: list[dict]) -> None:
        for start in range(0, len(children), 100):
            chunk = children[start : start + 100]
            self._request("PATCH", f"/blocks/{block_id}/children", {"children": chunk})

    def archive_block(self, block_id: str) -> None:
        self._request("PATCH", f"/blocks/{block_id}", {"archived": True})


def _schema_title_property_name(schema: dict) -> str:
    properties = schema.get("properties", {}) or {}
    for name, config in properties.items():
        if config.get("type") == "title":
            return name
    raise RuntimeError("Could not find a title property in the Notion data source schema.")


def _find_schema_property_name(
    schema: dict, aliases: tuple[str, ...], *, types: tuple[str, ...] | None = None
) -> str | None:
    properties = schema.get("properties", {}) or {}
    normalized_aliases = {_normalized_text(alias) for alias in aliases}
    for name, config in properties.items():
        if types and config.get("type") not in types:
            continue
        if _normalized_text(name) in normalized_aliases:
            return name
    return None


def _property_plain_text(value: dict | None) -> str:
    if not isinstance(value, dict):
        return ""
    value_type = value.get("type")
    if value_type in {"title", "rich_text"}:
        items = value.get(value_type, []) or []
        return "".join(str(item.get("plain_text") or "") for item in items)
    if value_type == "status":
        return str((value.get("status") or {}).get("name") or "")
    if value_type == "select":
        return str((value.get("select") or {}).get("name") or "")
    if value_type == "url":
        return str(value.get("url") or "")
    if value_type == "date":
        date_value = value.get("date") or {}
        return str(date_value.get("start") or "")
    if value_type == "checkbox":
        return "true" if value.get("checkbox") else "false"
    if value_type == "email":
        return str(value.get("email") or "")
    return ""


def _match_existing_page(pages: list[dict], *, schema: dict, meta: dict, out_dir: Path) -> dict | None:
    title_property = _schema_title_property_name(schema)
    url_property = _find_schema_property_name(schema, ("url", "userDefined:URL", "job url"), types=("url",))
    candidate_title_keys = {_normalized_text(title) for title in _candidate_page_titles(meta, out_dir)}
    candidate_url_identity = _job_url_identity(str(meta.get("jd_source") or ""))
    candidate_company = _normalized_text(str(meta.get("company_proper") or meta.get("company") or ""))
    candidate_job = _normalized_text(str(meta.get("jd_title") or ""))

    best: tuple[int, dict] | None = None
    for page in pages:
        properties = page.get("properties", {}) or {}
        title = _property_plain_text(properties.get(title_property))
        title_key = _normalized_text(title)
        url_value = _property_plain_text(properties.get(url_property)) if url_property else ""
        score = 0
        url_match = bool(candidate_url_identity and _job_url_identity(url_value) == candidate_url_identity)
        title_match = bool(title_key and title_key in candidate_title_keys)
        company_match = bool(candidate_company and candidate_company in title_key)
        job_match = bool(candidate_job and candidate_job in title_key)

        if url_match:
            score += 100
        if title_match:
            score += 80
        if company_match:
            score += 15
        if job_match:
            score += 20

        if not (url_match or title_match or (company_match and job_match)):
            continue
        if score and (best is None or score > best[0]):
            best = (score, page)

    return best[1] if best else None


def _default_min_received_at_utc(out_dir: Path) -> str | None:
    out_dir = Path(out_dir)
    for submit_dir in submit_dirs_by_mtime(out_dir):
        for name in (WEBSITE_CONFIRMATION_JSON, SUBMISSION_RESULT_JSON):
            payload = _read_json(submit_dir / name)
            if not isinstance(payload, dict):
                continue
            confirmed_at = str(payload.get("confirmed_at_utc") or "").strip()
            if confirmed_at:
                return confirmed_at
    return None


def _rich_text(content: str) -> list[dict]:
    text = content.strip()
    if not text:
        return []
    chunks = []
    for start in range(0, len(text), 1800):
        chunk = text[start : start + 1800]
        chunks.append({"type": "text", "text": {"content": chunk}})
    return chunks


def _make_block(block_type: str, content: str) -> dict | None:
    rich_text = _rich_text(content)
    if not rich_text:
        return None
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": rich_text},
    }


def _html_to_blocks(html_text: str) -> list[dict]:
    try:
        from lxml import html as lxml_html
    except ImportError:
        return _markdown_to_blocks(_strip_html(html_text))

    root = lxml_html.fragment_fromstring(html_text, create_parent=True)
    blocks: list[dict] = []

    def text_of(node) -> str:
        return _collapse_whitespace(node.text_content())

    def walk(node) -> None:
        tag = getattr(node, "tag", "")
        if not isinstance(tag, str):
            return
        tag = tag.casefold()
        if tag in {"div", "section", "article"}:
            for child in node:
                walk(child)
            return
        if tag in {"h1", "h2", "h3"}:
            block = _make_block({"h1": "heading_1", "h2": "heading_2", "h3": "heading_3"}[tag], text_of(node))
            if block:
                blocks.append(block)
            return
        if tag in {"ul", "ol"}:
            for child in node:
                walk(child)
            return
        if tag == "li":
            block = _make_block("bulleted_list_item", text_of(node))
            if block:
                blocks.append(block)
            return
        if tag == "p":
            block = _make_block("paragraph", text_of(node))
            if block:
                blocks.append(block)
            return
        if tag == "br":
            return
        if len(node):
            for child in node:
                walk(child)
            return
        block = _make_block("paragraph", text_of(node))
        if block:
            blocks.append(block)

    for child in root:
        walk(child)
    return blocks


def _markdown_to_blocks(text: str) -> list[dict]:
    blocks: list[dict] = []
    paragraph_parts: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph_parts:
            return
        block = _make_block("paragraph", " ".join(paragraph_parts))
        if block:
            blocks.append(block)
        paragraph_parts.clear()

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            continue
        if stripped == "---":
            flush_paragraph()
            continue
        if stripped.startswith("### "):
            flush_paragraph()
            block = _make_block("heading_3", stripped[4:].strip())
            if block:
                blocks.append(block)
            continue
        if stripped.startswith("## "):
            flush_paragraph()
            block = _make_block("heading_2", stripped[3:].strip())
            if block:
                blocks.append(block)
            continue
        if stripped.startswith("# "):
            flush_paragraph()
            block = _make_block("heading_1", stripped[2:].strip())
            if block:
                blocks.append(block)
            continue
        if stripped.startswith(("- ", "* ")):
            flush_paragraph()
            block = _make_block("bulleted_list_item", stripped[2:].strip())
            if block:
                blocks.append(block)
            continue
        paragraph_parts.append(stripped)

    flush_paragraph()
    return blocks


def _jd_blocks(out_dir: Path) -> list[dict]:
    raw_path = find_role_file(out_dir, "jd_raw.md", bucket="content")
    if raw_path is None or not raw_path.exists():
        return []
    raw_text = raw_path.read_text(encoding="utf-8")
    stripped = raw_text.strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return _markdown_to_blocks(raw_text)
        if isinstance(payload, dict) and payload.get("content"):
            return _html_to_blocks(html_lib.unescape(str(payload["content"])))
    return _markdown_to_blocks(raw_text)


def _load_submission_history_for_output(out_dir: Path) -> dict:
    db_path = _runtime_jobs_db_path()
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        columns = {
            str(row["name"]).strip() for row in conn.execute("PRAGMA table_info(jobs)").fetchall() if row["name"]
        }
        if not {"output_dir", "confirmed_at"} <= columns:
            return {}
        select_columns = ["confirmed_at"]
        for column in ("last_resubmit_unlocked_at", "last_resubmit_confirmed_at", "resubmit_count"):
            if column in columns:
                select_columns.append(column)
        row = conn.execute(
            f"SELECT {', '.join(select_columns)} FROM jobs WHERE output_dir = ? ORDER BY id DESC LIMIT 1",
            (str(out_dir),),
        ).fetchone()
        if not row:
            return {}
        history = dict(row)
        history.setdefault("last_resubmit_unlocked_at", None)
        history.setdefault("last_resubmit_confirmed_at", None)
        history.setdefault("resubmit_count", 0)
        return history
    except sqlite3.DatabaseError:
        return {}
    finally:
        conn.close()


def _existing_date_value(existing_page: dict | None, schema: dict, aliases: tuple[str, ...]) -> str | None:
    if not existing_page:
        return None
    name = _find_schema_property_name(schema, aliases, types=("date",))
    if not name:
        return None
    value = (((existing_page.get("properties") or {}).get(name) or {}).get("date") or {}).get("start")
    return str(value) if value else None


def _existing_notes_value(existing_page: dict | None, schema: dict) -> str:
    if not existing_page:
        return ""
    name = _find_schema_property_name(schema, ("notes",), types=("rich_text",))
    if not name:
        return ""
    rich_text = ((existing_page.get("properties") or {}).get(name) or {}).get("rich_text") or []
    return "\n".join(part.get("plain_text", "") for part in rich_text if part.get("plain_text"))


_GENERATED_NOTES_PREFIXES = (
    "Applied via automation with",
    "Output dir:",
    "Website confirmed at:",
    "Website confirmation URL:",
    "Email subject:",
    "Email date:",
    "Originally applied:",
    "Unlocked for resubmit:",
    "Latest resubmitted at:",
    "Resubmit count:",
)


def _manual_notes_lines(existing_notes: str) -> list[str]:
    lines: list[str] = []
    for raw_line in existing_notes.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(line.casefold().startswith(prefix.casefold()) for prefix in _GENERATED_NOTES_PREFIXES):
            continue
        lines.append(line)
    return lines


def _resubmission_history_lines(history: dict | None) -> list[str]:
    if not history or not history.get("resubmit_count"):
        return []
    lines: list[str] = []
    original = str(history.get("confirmed_at") or "").strip()
    if original:
        lines.append(f"Originally applied: {original}")
    unlocked = str(history.get("last_resubmit_unlocked_at") or "").strip()
    if unlocked:
        lines.append(f"Unlocked for resubmit: {unlocked}")
    latest = str(history.get("last_resubmit_confirmed_at") or "").strip()
    if latest:
        lines.append(f"Latest resubmitted at: {latest}")
    lines.append(f"Resubmit count: {history.get('resubmit_count')}")
    return lines


def _resubmission_history_marker(history: dict | None) -> str:
    if not history or not history.get("resubmit_count"):
        return ""
    for key in ("last_resubmit_confirmed_at", "last_resubmit_unlocked_at"):
        value = str(history.get(key) or "").strip()
        if value:
            return value
    return f"Resubmit count: {history.get('resubmit_count')}"


def _block_plain_text(block: dict) -> str:
    block_type = str(block.get("type") or "").strip()
    if not block_type:
        return ""
    rich_text = (block.get(block_type) or {}).get("rich_text") or []
    return " ".join(part.get("plain_text", "") for part in rich_text if part.get("plain_text")).strip()


def _resubmission_history_block_ids(existing_blocks: list[dict]) -> list[str]:
    block_ids: list[str] = []
    collecting = False
    for block in existing_blocks:
        block_type = str(block.get("type") or "").strip()
        if block_type == "heading_2" and _block_plain_text(block) == "Resubmission History":
            collecting = True
            if block.get("id"):
                block_ids.append(str(block["id"]))
            continue
        if not collecting:
            continue
        if block_type == "bulleted_list_item":
            if block.get("id"):
                block_ids.append(str(block["id"]))
            continue
        collecting = False
    return block_ids


def _sync_resubmission_history_blocks(
    client: NotionClient,
    page_id: str,
    existing_blocks: list[dict],
    history: dict | None,
) -> bool:
    history_blocks = _resubmission_history_blocks(history or {})
    history_marker = _resubmission_history_marker(history)
    if not history_blocks or not history_marker:
        return False
    if history_marker in json.dumps(existing_blocks):
        return False
    for block_id in _resubmission_history_block_ids(existing_blocks):
        client.archive_block(block_id)
    client.append_block_children(page_id, history_blocks)
    return True


def _notes_text(
    out_dir: Path,
    website_confirmation: dict,
    email_confirmation: dict | None,
    *,
    submission_history: dict | None = None,
    existing_notes: str = "",
) -> str:
    local_dir = _display_path(out_dir)
    confirmation_type = "website and email confirmation" if email_confirmation else "website confirmation"
    lines = _manual_notes_lines(existing_notes)
    lines.extend(
        [
            f"Applied via automation with {confirmation_type}.",
            f"Output dir: {local_dir}",
            f"Website confirmed at: {website_confirmation['confirmed_at_utc']}",
        ]
    )
    if website_confirmation.get("url"):
        lines.append(f"Website confirmation URL: {website_confirmation['url']}")
    if email_confirmation and email_confirmation.get("subject"):
        lines.append(f"Email subject: {email_confirmation['subject']}")
    if email_confirmation and email_confirmation.get("date"):
        lines.append(f"Email date: {email_confirmation['date']}")
    lines.extend(_resubmission_history_lines(submission_history))
    return "\n".join(dict.fromkeys(line for line in lines if line))


def _body_blocks(out_dir: Path, website_confirmation: dict, email_confirmation: dict | None) -> list[dict]:
    blocks: list[dict] = []
    items = [
        ("heading_2", "Application Metadata"),
        ("bulleted_list_item", f"Website confirmed: {website_confirmation['confirmed_at_utc']}"),
    ]
    if email_confirmation:
        items.append(
            (
                "bulleted_list_item",
                f"Email confirmed: {email_confirmation.get('subject') or 'confirmation email found'}",
            )
        )
    else:
        items.append(("bulleted_list_item", "Email confirmation: not received"))
    items.extend(
        [
            ("bulleted_list_item", f"Output directory: {_display_path(out_dir)}"),
            ("heading_2", "Job Description"),
        ]
    )
    for block_type, content in items:
        block = _make_block(block_type, content)
        if block:
            blocks.append(block)
    blocks.extend(_jd_blocks(out_dir))
    return blocks


def _resubmission_history_blocks(history: dict) -> list[dict]:
    history_lines = _resubmission_history_lines(history)
    if not history_lines:
        return []
    items = [("heading_2", "Resubmission History")]
    items.extend(("bulleted_list_item", line) for line in history_lines)
    blocks: list[dict] = []
    for block_type, content in items:
        block = _make_block(block_type, content)
        if block:
            blocks.append(block)
    return blocks


def _page_properties(
    schema: dict,
    *,
    meta: dict,
    out_dir: Path,
    website_confirmation: dict,
    email_confirmation: dict | None,
    existing_page: dict | None = None,
    submission_history: dict | None = None,
) -> dict:
    title_name = _schema_title_property_name(schema)
    properties = schema.get("properties", {}) or {}
    title_value = next(iter(_candidate_page_titles(meta, out_dir)), f"{meta['jd_title']} | {meta['company_proper']}")
    jd_parsed_path = find_role_file(out_dir, "jd_parsed.json", bucket="content") or (out_dir / "jd_parsed.json")
    existing_application_date = _existing_date_value(existing_page, schema, ("application date", "applied date"))
    existing_notes = _existing_notes_value(existing_page, schema)

    payload: dict[str, dict] = {
        title_name: {"title": _rich_text(title_value)},
    }

    mapped_values = [
        (("status",), ("status",), "Applied"),
        (("url", "userDefined:URL", "job url"), ("url",), str(meta.get("jd_source") or "")),
        (("url direct", "board url", "direct url"), ("url",), str(meta.get("board_url") or "")),
        (("url source", "source url"), ("url",), str(meta.get("source_url") or "")),
        (("position", "role"), ("rich_text", "title"), str(meta.get("jd_title") or "")),
        (
            ("application date", "applied date"),
            ("date",),
            existing_application_date or website_confirmation["confirmed_at_utc"],
        ),
        (
            ("notes",),
            ("rich_text",),
            _notes_text(
                out_dir,
                website_confirmation,
                email_confirmation,
                submission_history=submission_history,
                existing_notes=existing_notes,
            ),
        ),
        (("job type",), ("select",), _infer_job_type(meta, _read_json(jd_parsed_path))),
    ]

    for aliases, allowed_types, value in mapped_values:
        if not value:
            continue
        name = _find_schema_property_name(schema, aliases, types=allowed_types)
        if not name:
            continue
        property_type = properties[name]["type"]
        if property_type == "status":
            payload[name] = {"status": {"name": str(value)}}
        elif property_type == "url":
            payload[name] = {"url": str(value)}
        elif property_type == "date":
            payload[name] = {"date": {"start": str(value)}}
        elif property_type == "select":
            payload[name] = {"select": {"name": str(value)}}
        elif property_type == "title":
            payload[name] = {"title": _rich_text(str(value))}
        else:
            payload[name] = {"rich_text": _rich_text(str(value))}

    return payload


def _website_confirmation(out_dir: Path) -> dict:
    for submit_dir in submit_dirs_by_mtime(out_dir):
        for name in (SUBMISSION_RESULT_JSON, WEBSITE_CONFIRMATION_JSON):
            submission = _read_json(submit_dir / name)
            if isinstance(submission, dict) and submission.get("website_confirmed"):
                return submission
    expected = " or ".join((SUBMISSION_RESULT_JSON, WEBSITE_CONFIRMATION_JSON))
    raise RuntimeError(f"Website confirmation is required before syncing to Notion. Expected a confirmed {expected}.")


def _pending_email_result(base_result: dict) -> dict:
    return {
        **base_result,
        "status": "pending_email_confirmation",
        "message": "The website submission was confirmed, but no matching application confirmation email was found in Gmail yet.",
    }


def _missing_token_result(base_result: dict) -> dict:
    return {
        **base_result,
        "status": "missing_notion_token",
        "message": (
            "Set NOTION_API_TOKEN or NOTION_TOKEN and share the Job Applications data source with that integration "
            "to enable Notion sync."
        ),
    }


def _sync_to_notion(out_dir: Path, *, email_confirmation: dict | None, allow_website_only: bool = False) -> dict:
    meta = _read_json(out_dir / ".pipeline_meta.json")
    if not isinstance(meta, dict):
        raise RuntimeError(f"Missing or invalid .pipeline_meta.json in {out_dir}")

    website_confirmation = _website_confirmation(out_dir)
    result_path = role_submit_path(out_dir, NOTION_SYNC_STATUS_JSON)
    base_result = {
        "updated_at_utc": _now_utc_iso(),
        "output_dir": str(out_dir),
        "job_url": meta.get("jd_source"),
        "job_title": meta.get("jd_title"),
        "company": meta.get("company_proper"),
        "website_confirmation": website_confirmation,
        "email_confirmation": email_confirmation,
    }
    if not email_confirmation and not allow_website_only:
        payload = _pending_email_result(base_result)
        _write_result(result_path, payload)
        return payload

    token = os.getenv("NOTION_API_TOKEN") or os.getenv("NOTION_TOKEN")
    if not token:
        payload = _missing_token_result(base_result)
        _write_result(result_path, payload)
        return payload

    client = NotionClient(
        token=token,
        data_source_id=os.getenv("NOTION_DATA_SOURCE_ID")
        or os.getenv("NOTION_JOB_APPLICATIONS_DATA_SOURCE_ID", DEFAULT_NOTION_DATA_SOURCE_ID),
        database_id=os.getenv("NOTION_DATABASE_ID")
        or os.getenv("NOTION_JOB_APPLICATIONS_DATABASE_ID", DEFAULT_NOTION_DATABASE_ID),
        notion_version=os.getenv("NOTION_VERSION", DEFAULT_NOTION_VERSION),
    )
    schema = client.retrieve_schema()
    submission_history = _load_submission_history_for_output(out_dir)
    existing_page = _match_existing_page(client.query_pages(), schema=schema, meta=meta, out_dir=out_dir)
    properties = _page_properties(
        schema,
        meta=meta,
        out_dir=out_dir,
        website_confirmation=website_confirmation,
        email_confirmation=email_confirmation,
        existing_page=existing_page,
        submission_history=submission_history,
    )

    if existing_page:
        page_id = existing_page["id"]
        client.update_page(page_id, properties)
        existing_blocks = client.list_block_children(page_id)
        had_body = bool(existing_blocks)
        if not had_body:
            client.append_block_children(page_id, _body_blocks(out_dir, website_confirmation, email_confirmation))
        _sync_resubmission_history_blocks(client, page_id, existing_blocks, submission_history)
        payload = {
            **base_result,
            "status": "synced",
            "page_id": page_id,
            "page_url": existing_page.get("url"),
            "existing_page": True,
            "body_appended": not had_body,
        }
    else:
        children = _body_blocks(out_dir, website_confirmation, email_confirmation)
        created = client.create_page(properties, children)
        page_id = created["id"]
        if len(children) > 100:
            client.append_block_children(page_id, children[100:])
        payload = {
            **base_result,
            "status": "synced",
            "page_id": page_id,
            "page_url": created.get("url"),
            "existing_page": False,
            "body_appended": True,
        }

    _write_result(result_path, payload)
    return payload


def sync_application(
    out_dir: Path,
    *,
    wait_for_email_seconds: int = 0,
    email_confirmation: dict | None = None,
    min_received_at_utc: datetime | str | None = None,
    allow_pending_email: bool = False,
    allow_website_only: bool = True,
    fail_on_missing_token: bool = True,
) -> dict:
    out_dir = Path(out_dir)
    migrate_role_output_layout(out_dir)
    meta = _read_json(out_dir / ".pipeline_meta.json")
    if not isinstance(meta, dict):
        raise RuntimeError(f"Missing or invalid .pipeline_meta.json in {out_dir}")

    if min_received_at_utc is None:
        min_received_at_utc = _default_min_received_at_utc(out_dir)

    if email_confirmation:
        _write_result(role_submit_path(out_dir, EMAIL_CONFIRMATION_JSON), email_confirmation)
    else:
        email_confirmation = wait_for_email_confirmation(
            out_dir,
            timeout_seconds=wait_for_email_seconds,
            poll_interval_seconds=int(os.environ.get("NOTION_SYNC_EMAIL_POLL_INTERVAL_SECONDS", "10")),
            min_received_at_utc=min_received_at_utc,
        )

    result = _sync_to_notion(out_dir, email_confirmation=email_confirmation, allow_website_only=allow_website_only)
    if result["status"] == "pending_email_confirmation" and not allow_pending_email:
        raise RuntimeError(result["message"])
    if result["status"] == "missing_notion_token" and fail_on_missing_token:
        raise RuntimeError(result["message"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync a successfully submitted application into the Job Applications Notion database."
    )
    parser.add_argument(
        "target",
        help="Output directory (for example output/figma/pm-design-tools) or a JD URL already present in .pipeline_meta.json.",
    )
    parser.add_argument(
        "--wait-for-email",
        "--wait-for-email-seconds",
        type=int,
        dest="wait_for_email_seconds",
        default=0,
        help="How long to wait for an application confirmation email before returning a pending status.",
    )
    parser.add_argument(
        "--allow-pending-email",
        action="store_true",
        help="Return a pending status instead of failing when the confirmation email has not arrived yet.",
    )
    parser.add_argument(
        "--strict-token",
        action="store_true",
        help="Return a non-zero exit status when no Notion API token is configured.",
    )
    parser.add_argument(
        "--no-fail-on-missing-token",
        action="store_true",
        help="Return a pending status instead of failing when no Notion API token is configured.",
    )
    args = parser.parse_args()

    out_dir = _find_output_dir(args.target)
    preferred_submit_dir = preferred_submit_dir_name_for_post_submit(out_dir)
    with _submit_dir_override(preferred_submit_dir):
        try:
            result = sync_application(
                out_dir,
                wait_for_email_seconds=args.wait_for_email_seconds,
                allow_pending_email=args.allow_pending_email,
                fail_on_missing_token=args.strict_token and not args.no_fail_on_missing_token,
            )
        except Exception as exc:
            payload = {
                "status": "sync_failed",
                "updated_at_utc": _now_utc_iso(),
                "output_dir": str(out_dir),
                "message": str(exc),
            }
            _write_result(role_submit_path(out_dir, NOTION_SYNC_STATUS_JSON), payload)
            print(str(exc), file=sys.stderr)
            return EXIT_SYNC_FAILED

    print(_json_dumps_pretty(result))
    status = result["status"]
    if status == "synced":
        return EXIT_SUCCESS
    if status == "pending_email_confirmation":
        return EXIT_SUCCESS if args.allow_pending_email else EXIT_PENDING_EMAIL
    if status == "missing_notion_token":
        return EXIT_MISSING_NOTION_TOKEN if args.strict_token and not args.no_fail_on_missing_token else EXIT_SUCCESS
    return EXIT_WEBSITE_NOT_CONFIRMED


if __name__ == "__main__":
    raise SystemExit(main())
