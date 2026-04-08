#!/usr/bin/env python3
"""Orchestrator that chains all deterministic pipeline steps into a single command.

Usage:
    uv run scripts/run_pipeline.py <jd_source>
    uv run scripts/run_pipeline.py <jd_source> -c <company> -r <role-slug>
    uv run scripts/run_pipeline.py <jd_source> --build --skip-sync

When -c and -r are omitted, the script auto-detects them from the parsed JD
(company name and job title). You can also provide just one to override.

Where <jd_source> is either a file path or a URL (http:// or https://).

Pipeline steps:
    1. Sync work_stories.md and candidate_context.md from configured remote sources (unless --skip-sync)
    2. If jd_source is a URL, extract the JD from the website and require a usable result
    3. Run parse_jd.py → extract company/role slugs if needed
    4. Run rank_bullets.py
    5. Run draft_resume.py
    6. Print summary

With --build:
    7. Run build_resume.py and build_cover_letter.py
    8. Run validate_resume.py on the built PDF
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from app_paths import material_path, sync_state_path, tmp_root
from candidate_runtime import document_filename, load_candidate_runtime_profile
from entrypoint_guard import abort_if_recursive_entrypoints_forbidden
from job_board_urls import (
    canonical_greenhouse_job_url,
    looks_like_greenhouse_url,
    looks_like_non_html_asset_url,
    looks_like_unresolved_url_template,
    resolve_job_source_url,
)
from output_layout import (
    ensure_role_output_dirs,
    migrate_role_output_layout,
    role_content_path,
    role_documents_path,
    role_layout_metadata,
)
from parse_jd import company_name_looks_generic, company_name_looks_locationish, title_looks_generic
from url_resolver import detect_source, resolve_to_board_url
from worker_subprocess import run_worker_subprocess

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORK_STORIES_PATH = material_path("work_stories.md")
SYNC_STATE_PATH = sync_state_path(".work_stories_sync_state.json")
WORK_STORIES_SOURCE_URL_ENV = "JOB_ASSETS_WORK_STORIES_SOURCE_URL"
CANDIDATE_CONTEXT_PATH = material_path("candidate_context.md")
CANDIDATE_CONTEXT_SYNC_STATE_PATH = sync_state_path(".candidate_context_sync_state.json")
CANDIDATE_CONTEXT_SOURCE_URL_ENV = "JOB_ASSETS_CANDIDATE_CONTEXT_SOURCE_URL"

PYTHON = sys.executable
STEALTH_DOMAINS = (
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "workday.com",
    "myworkdayjobs.com",
    "myworkdaysite.com",
)
JD_EXTRACTION_BLOCKERS = (
    "javascript must be enabled",
    "please enable javascript to continue",
    "unsupported-browser",
    "access denied",
    "verify you are human",
    "captcha",
    "please enable cookies",
    "cloudflare ray id",
)

# ---------------------------------------------------------------------------
# User-Agent rotation pool for JD scraping
# ---------------------------------------------------------------------------
_UA_POOL: list[str] = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]
_ua_index: int = 0


def _next_user_agent() -> str:
    """Return the next User-Agent from the rotation pool."""
    global _ua_index
    ua = _UA_POOL[_ua_index % len(_UA_POOL)]
    _ua_index += 1
    return ua


def _create_pipeline_tmp_dir(base_dir: Path | None = None) -> Path:
    """Create a unique scratch directory for a single pipeline run.

    Concurrent worker runs must never share temp JD files or they can clobber
    each other's parsed content and write the wrong metadata/output paths.
    """
    root = Path(base_dir) if base_dir is not None else (tmp_root() / "pipeline")
    root.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="run-", dir=str(root)))
    atexit.register(shutil.rmtree, tmp_dir, ignore_errors=True)
    return tmp_dir


# ---------------------------------------------------------------------------
# Per-domain rate limiting for JD fetching
# ---------------------------------------------------------------------------
_domain_last_fetch: dict[str, float] = {}
_DOMAIN_MIN_GAP_SECONDS: float = 3.0


def _enforce_domain_rate_limit(url: str) -> None:
    """Sleep if needed to enforce a minimum gap between requests to the same domain."""
    parsed = urlparse(url)
    domain = (parsed.hostname or "").casefold()
    if not domain:
        return
    now = time.monotonic()
    last = _domain_last_fetch.get(domain)
    if last is not None:
        elapsed = now - last
        if elapsed < _DOMAIN_MIN_GAP_SECONDS:
            wait = _DOMAIN_MIN_GAP_SECONDS - elapsed
            _log(f"[rate-limit] Waiting {wait:.1f}s before next request to {domain}")
            time.sleep(wait)
    _domain_last_fetch[domain] = time.monotonic()


abort_if_recursive_entrypoints_forbidden("scripts/run_pipeline.py")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_cover_letter(path: Path) -> None:
    """Ensure cover letter text has greeting and signoff."""
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return
    import re

    changed = False
    if not re.match(r"(?i)^dear\s", text):
        text = "Dear Hiring Team,\n\n" + text
        changed = True
    if not re.search(r"\n(Best regards|Sincerely|Regards|Warm regards|Thank you),?\s*\n", text, re.IGNORECASE):
        candidate_profile = load_candidate_runtime_profile()
        text = text.rstrip() + f"\n\nBest regards,\n{candidate_profile.full_name}"
        changed = True
    if changed:
        path.write_text(text + "\n", encoding="utf-8")


def _log(msg: str) -> None:
    """Print a status message to stderr."""
    print(msg, file=sys.stderr, flush=True)


def _run_step(
    label: str,
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    """Run a subprocess step, print timing, and return the result."""
    _log(f"\n{'─' * 60}")
    _log(f"  {label}")
    _log(f"{'─' * 60}")

    t0 = time.monotonic()
    kwargs: dict = {"cwd": str(PROJECT_ROOT)}
    if capture:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    result = run_worker_subprocess(cmd, **kwargs)
    elapsed = time.monotonic() - t0

    status = "OK" if result.returncode == 0 else f"FAILED (exit {result.returncode})"
    _log(f"  [{status}] {label}  ({elapsed:.1f}s)")

    if check and result.returncode != 0:
        _log(f"\nPipeline aborted: {label} failed.")
        sys.exit(result.returncode)

    return result


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _slugify(text: str) -> str:
    """Convert text to a lowercase hyphenated slug."""
    text = text.lower().strip()
    # Replace common separators with hyphens
    text = re.sub(r"[_\s/|,]+", "-", text)
    # Remove non-alphanumeric (keep hyphens)
    text = re.sub(r"[^a-z0-9-]", "", text)
    # Collapse multiple hyphens
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")


def _role_slug_from_title(title: str) -> str:
    """Generate a concise role slug from a job title.

    Examples:
        'Principal Product Manager - Maintenance' → 'principal-pm-maintenance'
        'Senior Software Engineer, Platform' → 'senior-swe-platform'
        'Staff Product Manager' → 'staff-pm'
    """
    t = title.lower().strip()

    # Common abbreviations
    abbreviations = [
        (r"product manager", "pm"),
        (r"product management", "pm"),
        (r"software engineer", "swe"),
        (r"data scientist", "ds"),
        (r"data engineer", "de"),
        (r"machine learning", "ml"),
        (r"engineering manager", "em"),
        (r"program manager", "pgm"),
        (r"project manager", "pjm"),
        (r"technical program manager", "tpm"),
        (r"user experience", "ux"),
        (r"user interface", "ui"),
    ]
    for pattern, abbrev in abbreviations:
        t = re.sub(pattern, abbrev, t)

    return _slugify(t)


def _company_slug_from_url(url: str) -> str | None:
    """Try to extract company slug from a job posting URL."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    generic_host_parts = {
        "www",
        "jobs",
        "careers",
        "boards",
        "apply",
        "ats",
        "app",
        "job",
        "join",
        "hire",
        "hiring",
        "recruiting",
        "team",
    }
    tld_parts = {"com", "io", "ai", "co", "org", "net", "jobs"}

    # Greenhouse: boards.greenhouse.io/<company>/jobs/...
    # or boards-api.greenhouse.io/v1/boards/<company>/jobs/...
    if "greenhouse.io" in host:
        parts = parsed.path.strip("/").split("/")
        for _i, part in enumerate(parts):
            if part in ("boards", "v1"):
                continue
            if part == "jobs":
                break
            # Skip numeric parts
            if not part.isdigit():
                return _slugify(part)

    # Lever: jobs.lever.co/<company>/...
    if "lever.co" in host:
        parts = parsed.path.strip("/").split("/")
        if parts:
            return _slugify(parts[0])

    # Ashby: jobs.ashbyhq.com/<company>/...
    if "ashbyhq.com" in host:
        parts = parsed.path.strip("/").split("/")
        if parts and parts[0]:
            return _slugify(parts[0])

    # Gem: jobs.gem.com/<company>/...
    if "jobs.gem.com" in host:
        parts = parsed.path.strip("/").split("/")
        if parts and parts[0]:
            return _slugify(parts[0])

    # Dover: app.dover.com/apply/<company>/<job-or-search-id>
    if "dover.com" in host:
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "apply" and parts[1]:
            return _slugify(parts[1])

    # Workable: jobs.workable.com/view/... — company in URL suffix "at-<company>"
    if "workable.com" in host:
        # URL often has "at-companyname" at the end
        path_lower = parsed.path.lower()
        m = re.search(r"-at-([a-z0-9-]+?)(?:\?|$)", path_lower)
        if m:
            return _slugify(m.group(1))

    # iCIMS: uscareers-<company>.icims.com/...
    if "icims.com" in host:
        m = re.match(r"(?:uscareers-)?([a-z0-9-]+)\.icims", host)
        if m:
            return _slugify(m.group(1))

    # Workday: <company>.wd503.myworkdayjobs.com/...
    if "myworkdayjobs.com" in host:
        m = re.match(r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs", host)
        if m:
            return _slugify(m.group(1))

    # Workday (myworkdaysite.com): wd1.myworkdaysite.com/en-US/recruiting/<company>/...
    if "myworkdaysite.com" in host:
        parts = parsed.path.strip("/").split("/")
        # Path: en-US/recruiting/<company>/<company2>/job/...
        recruiting_idx = next((i for i, p in enumerate(parts) if p == "recruiting"), -1)
        if recruiting_idx >= 0 and recruiting_idx + 1 < len(parts):
            return _slugify(parts[recruiting_idx + 1])

    # Company career pages: <company>.com/careers/...
    # Extract from domain
    domain_parts = host.replace("www.", "").split(".")
    for part in domain_parts:
        if part in generic_host_parts or part in tld_parts:
            continue
        return _slugify(part)

    return None


def _company_name_from_text(text: str) -> str | None:
    """Try to extract a company name directly from JD text/content."""
    from parse_jd import _strip_generic_company_suffix, _try_parse_greenhouse_json

    normalized_greenhouse = _try_parse_greenhouse_json(text)
    if normalized_greenhouse is not None:
        text = normalized_greenhouse

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    top_lines = [
        line
        for line in lines[:20]
        if "page is loaded" not in line.casefold() and not title_looks_generic(line)
    ]
    generic_sentence_starts = {"this", "that", "these", "those", "we", "our", "the company"}

    for line in top_lines:
        match = re.match(r"^(?:\*\*)?Company(?:\*\*)?:\s*(.{2,60})$", line, re.I)
        if match:
            candidate = _strip_generic_company_suffix(match.group(1).strip().strip("*").strip())
            if (
                candidate
                and len(candidate.split()) <= 5
                and not company_name_looks_generic(candidate)
                and not company_name_looks_locationish(candidate)
            ):
                return candidate

    for line in top_lines:
        normalized = re.sub(r"\s+[|·-]\s*linkedin\s*$", "", line, flags=re.I)
        segments = [segment.strip() for segment in re.split(r"\s*[|·]\s*", normalized) if segment.strip()]
        if len(segments) < 2:
            continue
        candidate = segments[-1].strip(" -|,")
        if not company_name_looks_generic(candidate) and not company_name_looks_locationish(candidate):
            return candidate

    for line in top_lines:
        normalized = re.sub(r"\s+[|·-]\s*linkedin\s*$", "", line, flags=re.I)
        match = re.match(r"^(?:About|With)\s+([A-Z][A-Za-z0-9&.' -]{1,40})$", normalized)
        if match:
            candidate = match.group(1).strip()
            if not company_name_looks_generic(candidate):
                return candidate

    for line in top_lines:
        normalized = re.sub(r"\s+[|·-]\s*linkedin\s*$", "", line, flags=re.I)
        match = re.match(
            r"^([A-Z][A-Za-z0-9&.' -]{1,40})['’]s\b.+\bis hiring\b",
            normalized,
        )
        if match:
            candidate = match.group(1).strip()
            if not company_name_looks_generic(candidate) and not company_name_looks_locationish(candidate):
                return candidate

    for line in top_lines:
        normalized = re.sub(r"\s+[|·-]\s*linkedin\s*$", "", line, flags=re.I)
        match = re.match(
            r"^([A-Z][A-Za-z0-9&.' -]{1,40})\s+"
            r"(?:is|are|helps?|builds?|develops?|creates?|offers?|provides?|operates?|supports?|enables?|empowers?|welcomes?)\b",
            normalized,
        )
        if match:
            candidate = match.group(1).strip()
            if not company_name_looks_generic(candidate) and candidate.casefold() not in generic_sentence_starts:
                return candidate

    for line in top_lines:
        normalized = re.sub(r"\s+[|·-]\s*linkedin\s*$", "", line, flags=re.I)
        match = re.match(
            r"^(?i:at)\s+"
            r"([A-Z0-9][A-Za-z0-9&.'()\-/,]*(?:\s+[A-Z0-9][A-Za-z0-9&.'()\-/,]*){0,5})"
            r"(?=[:.,]|\s+(?:we|you|our|the|this|they|it)\b|$)",
            normalized,
        )
        if match:
            candidate = match.group(1).strip(" -|,")
            if not company_name_looks_generic(candidate) and not company_name_looks_locationish(candidate):
                return candidate

    for line in top_lines:
        normalized = re.sub(r"\s+[|·-]\s*linkedin\s*$", "", line, flags=re.I)
        match = re.match(r"^([A-Z][A-Za-z0-9&.' -]{1,40})\.com$", normalized, re.I)
        if match:
            candidate = match.group(1).strip()
            if not company_name_looks_generic(candidate):
                return candidate

    for line in top_lines:
        normalized = re.sub(r"\s+[|·-]\s*linkedin\s*$", "", line, flags=re.I)
        match = re.match(r"^([A-Z0-9][A-Za-z0-9&.'()\-/, ]{1,80}?)\s+(?:is\s+)?hiring\b", normalized)
        if match:
            candidate = match.group(1).strip(" -|,")
            if not company_name_looks_generic(candidate):
                return candidate
        match = re.match(r"^.+?\s+at\s+([A-Z0-9][A-Za-z0-9&.'()\-/, ]{1,80}?)(?:\s+[|·-]\s+.*)?$", normalized)
        if match:
            candidate = match.group(1).strip(" -|,")
            if not company_name_looks_generic(candidate):
                return candidate

    # If the same proper noun appears repeatedly near the top, treat it as a likely company.
    candidate_counts: dict[str, int] = {}
    for line in top_lines:
        for match in re.finditer(r"\b([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,2})\b", line):
            candidate = match.group(1).strip()
            if candidate.lower() in {"about", "with", "additional information", "workflow studio"}:
                continue
            if company_name_looks_generic(candidate) or company_name_looks_locationish(candidate):
                continue
            candidate_counts[candidate] = candidate_counts.get(candidate, 0) + 1

    if candidate_counts:
        candidate, count = max(candidate_counts.items(), key=lambda item: item[1])
        if count >= 2:
            return candidate

    return None


def _company_slug_from_text(text: str) -> str | None:
    """Try to extract a company slug directly from JD text/content."""
    company_name = _company_name_from_text(text)
    return _slugify(company_name) if company_name else None


def _resolve_company_proper(
    *,
    jd_company: str,
    company_slug: str,
    title_company_name: str | None,
    text_company_name: str | None,
    resolved_host: str,
    is_url: bool,
) -> str:
    """Resolve display-cased company name for metadata and filenames."""
    company_proper = jd_company.strip()
    if company_name_looks_generic(company_proper):
        company_proper = ""
    if title_company_name and ((is_url and "linkedin.com" in resolved_host) or not company_proper):
        company_proper = title_company_name
    if text_company_name and not company_proper:
        company_proper = text_company_name
    if not company_proper:
        company_proper = company_slug.replace("-", " ").title()
    return company_proper


def _url_prefers_stealth(url: str) -> bool:
    """Return True when the scraper will already prefer a stealth fetcher."""
    lowered = url.lower()
    return any(domain in lowered for domain in STEALTH_DOMAINS)


def _read_text(path: Path) -> str:
    """Read text robustly for scraped outputs."""
    return path.read_text(encoding="utf-8", errors="replace")


def _strip_html_for_validation(raw: str) -> str:
    """Remove HTML tags and decode entities for validation heuristics."""
    text = html.unescape(raw)
    text = html.unescape(text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.I)
    text = re.sub(r"</(?:p|div|h[1-6]|ul|ol|li|tr|td|th|table|section|header|footer|article)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _try_extract_greenhouse_text_for_validation(raw: str) -> str | None:
    """Normalize Greenhouse API JSON into human-readable text for validation."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None

    jobs = [data] if isinstance(data, dict) else data if isinstance(data, list) else None
    if jobs is None:
        return None

    parts: list[str] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if job.get("title"):
            parts.append(f"# {job['title']}")
        if job.get("company_name"):
            parts.append(f"Company: {job['company_name']}")
        location = job.get("location")
        if isinstance(location, dict) and location.get("name"):
            parts.append(f"Location: {location['name']}")
        elif isinstance(location, str) and location:
            parts.append(f"Location: {location}")
        if job.get("content"):
            parts.append(_strip_html_for_validation(str(job["content"])))

    return "\n\n".join(part for part in parts if part).strip() or None


def _normalize_extracted_jd_text(raw: str) -> str:
    """Normalize raw extracted content into plain text for validation heuristics."""
    greenhouse = _try_extract_greenhouse_text_for_validation(raw)
    if greenhouse is not None:
        return greenhouse

    lowered = raw.lower()
    if "<html" in lowered or "<body" in lowered or "<div" in lowered:
        return _strip_html_for_validation(raw)
    return raw


def _terminal_url_extraction_issue(raw: str, jd_data: dict, *, normalized: str) -> str | None:
    lowered_raw = raw.casefold()
    compact_raw = re.sub(r"\s+", "", lowered_raw)
    lowered_text = normalized.casefold()
    normalized_title = re.sub(r"[^a-z0-9]+", " ", str(jd_data.get("title") or "").casefold()).strip()
    strong_markers = (
        "paid advertisement",
        "find an advisor",
        "weekly market commentary",
        "advisor insights",
    )
    marketing_markers = strong_markers + (
        "industry insights",
        "investment essentials",
        "leadership, size, and strength",
        "individual investors",
        "institutions",
        "disclosures",
    )
    role_markers = (
        "about the role",
        "responsibilities",
        "required qualifications",
        "preferred qualifications",
        "minimum qualifications",
        "what you'll do",
        "what you will do",
        "what you'll bring",
        "what you bring",
        "job description",
        "years of experience",
    )
    marketing_marker_count = sum(marker in lowered_text for marker in marketing_markers)
    strong_marker_count = sum(marker in lowered_text for marker in strong_markers)
    role_marker_count = sum(marker in lowered_text for marker in role_markers)
    promotional_landing = strong_marker_count >= 1 and marketing_marker_count >= 4 and role_marker_count == 0

    if (
        "window.__appdata" in lowered_raw
        and '"organization":null' in compact_raw
        and '"posting":null' in compact_raw
    ):
        return "job_closed: Ashby returned an unavailable posting shell instead of a live job description."

    explicit_unavailable_markers = (
        "job posting is no longer available",
        "the job you are looking for is no longer available",
        "this job has been filled",
        "this position has been filled",
        "no longer accepting applications",
        "application is no longer available",
        "job not found",
        "page not found",
        "404 not found",
        "the page you requested could not be found",
    )
    if any(marker in lowered_text for marker in explicit_unavailable_markers):
        return "job_closed: The extracted page explicitly says the posting is unavailable."

    generic_title = (
        not normalized_title
        or normalized_title in {
            "career opportunities",
            "careers",
            "careers listing",
            "job application",
            "job applications",
            "job opening",
            "job openings",
            "jobs",
            "open positions",
            "open roles",
            "notion",
        }
        or normalized_title.startswith(("careers at ", "make your move at ", "learn about "))
        or "browse jobs" in normalized_title
        or "open roles" in normalized_title
        or "job openings" in normalized_title
    )
    landing_markers = (
        "browse jobs",
        "open roles",
        "search jobs",
        "job alerts",
        "join our talent community",
        "career site",
        "view all jobs",
    )
    meaningful_lines = [line.strip() for line in normalized.splitlines() if len(line.strip()) >= 25]
    meaningful_chars = sum(len(line) for line in meaningful_lines) or len(normalized)
    listing_markers = (
        "jobs shown",
        "search jobs",
        "open positions",
        "featured jobs",
        "job listing",
        "job location",
        "job type",
        "sign in or create an account",
    )
    listing_marker_count = sum(marker in lowered_text for marker in listing_markers)
    jobs_shown_pattern = re.search(r"\b\d+\s*-\s*\d+\s+of\s+\d+\s+jobs shown\b", lowered_text) is not None
    if (generic_title or promotional_landing) and (
        (meaningful_chars < 1200 and any(marker in lowered_text for marker in landing_markers))
        or promotional_landing
        or jobs_shown_pattern
        or listing_marker_count >= 2
    ):
        return "job_closed: The extracted page resolved to a generic careers landing page instead of a specific job posting."

    return None


def _terminal_extraction_issue_from_stderr(stderr_text: str) -> str | None:
    for raw_line in str(stderr_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.casefold()
        for prefix in ("job_closed:", "needs_board_url:", "skipped_captcha:", "unsupported:"):
            idx = lowered.find(prefix)
            if idx != -1:
                return line[idx:]
    return None


def _terminal_extraction_issue_from_issues(issues: list[str]) -> str | None:
    for issue in issues:
        terminal_issue = _terminal_extraction_issue_from_stderr(issue)
        if terminal_issue:
            return terminal_issue
    return None


def _validate_url_jd_extraction(raw: str, jd_data: dict) -> list[str]:
    """Return validation issues for a scraped URL JD. Empty means success."""
    normalized = _normalize_extracted_jd_text(raw).strip()
    lowered_raw = raw.lower()
    lowered_text = normalized.lower()

    issues: list[str] = []
    if not normalized:
        return ["scraper returned empty content"]

    terminal_issue = _terminal_url_extraction_issue(raw, jd_data, normalized=normalized)
    if terminal_issue:
        return [terminal_issue]

    if raw.lstrip().lower().startswith(("<!doctype html", "<html")) or any(
        marker in lowered_raw or marker in lowered_text for marker in JD_EXTRACTION_BLOCKERS
    ):
        issues.append("extracted page looks like an HTML/login/blocker shell instead of a job description")

    meaningful_lines = [line.strip() for line in normalized.splitlines() if len(line.strip()) >= 25]
    meaningful_chars = sum(len(line) for line in meaningful_lines) or len(normalized)
    section_items = sum(
        len(jd_data.get(key, [])) for key in ("responsibilities", "required_qualifications", "preferred_qualifications")
    )
    keyword_count = len(jd_data.get("keywords", []))
    title = (jd_data.get("title") or "").strip()
    normalized_title = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
    generic_titles = {
        "career opportunities",
        "careers",
        "careers listing",
        "job application",
        "job applications",
        "job opening",
        "job openings",
        "jobs",
        "open positions",
        "open roles",
        "notion",
    }

    if not title or normalized_title in generic_titles or normalized_title.startswith("careers at "):
        issues.append("parser could not identify a credible job title from the extracted page")

    if meaningful_chars < 400:
        issues.append("too little job-description text was extracted from the website")

    if section_items < 2 and keyword_count < 6 and meaningful_chars < 1000:
        issues.append("parsed JD did not contain enough structured detail to trust the extraction")

    return issues


def _log_captured_output(result: subprocess.CompletedProcess) -> None:
    """Emit captured stdout/stderr from a subprocess result."""
    if result.stdout and result.stdout.strip():
        _log(result.stdout.rstrip())
    if result.stderr and result.stderr.strip():
        _log(result.stderr.rstrip())


def _run_url_extraction_attempt(
    *,
    label: str,
    command: list[str] | None,
    fetcher: callable | None,
    url: str,
    tmp_raw: Path,
    tmp_parsed: Path,
) -> tuple[dict | None, list[str]]:
    """Run one extraction attempt and validate the resulting parsed JD."""
    if fetcher is not None:
        if not fetcher(url, tmp_raw):
            return None, [f"{label} did not produce a JD payload"]
    elif command is not None:
        result = _run_step(label, command, check=False, capture=True)
        _log_captured_output(result)
        if result.returncode != 0:
            terminal_issue = _terminal_extraction_issue_from_stderr(result.stderr or "")
            if terminal_issue:
                return None, [terminal_issue]
            issues = [f"{label} failed with exit code {result.returncode}"]
            # Surface HTTP 429 Retry-After info from subprocess stderr
            stderr_text = (result.stderr or "") if hasattr(result, "stderr") else ""
            if "429" in stderr_text or "Too Many Requests" in stderr_text:
                import re as _re

                m = _re.search(r"[Rr]etry-[Aa]fter:\s*(\d+)", stderr_text)
                if m:
                    issues.append(f"Retry-After: {m.group(1)}")
                else:
                    issues.append("HTTP 429 Too Many Requests (no Retry-After header)")
            return None, issues
    else:
        return None, [f"{label} was misconfigured"]

    if not tmp_raw.exists():
        return None, [f"{label} did not write {tmp_raw}"]

    raw_text = _read_text(tmp_raw)
    if not raw_text.strip():
        return None, [f"{label} returned empty content"]

    parse_result = _run_step(
        f"{label} → Parse JD",
        [PYTHON, "scripts/parse_jd.py", str(tmp_raw), "-o", str(tmp_parsed)],
        check=False,
        capture=True,
    )
    _log_captured_output(parse_result)
    if parse_result.returncode != 0:
        return None, [f"{label} produced content that parse_jd.py could not parse"]

    try:
        jd_data = json.loads(_read_text(tmp_parsed))
    except json.JSONDecodeError:
        return None, [f"{label} produced invalid parsed JD JSON"]

    issues = _validate_url_jd_extraction(raw_text, jd_data)
    if issues:
        return None, issues

    return jd_data, []


# ---------------------------------------------------------------------------
# Greenhouse API helper
# ---------------------------------------------------------------------------


def _try_fetch_greenhouse_api(url: str, output_path: Path) -> bool:
    """If URL is a Greenhouse job posting, fetch via the API for better parsing.

    Greenhouse URLs look like:
        https://job-boards.greenhouse.io/<company>/jobs/<id>
        https://boards.greenhouse.io/<company>/jobs/<id>
        https://boards-api.greenhouse.io/v1/boards/<company>/jobs/<id>
        https://<company>.com/careers/roles/<id>?gh_jid=<id>

    The API endpoint is:
        https://boards-api.greenhouse.io/v1/boards/<company>/jobs/<id>

    Returns True if successful (JSON saved to output_path), False otherwise.
    """
    url = canonical_greenhouse_job_url(url)
    parsed = urlparse(url)
    host = parsed.hostname or ""

    company_slug = None
    job_id = None

    # Direct Greenhouse URLs
    if "greenhouse.io" in host:
        parts = parsed.path.strip("/").split("/")
        # Find company and job ID from path
        # Patterns: <company>/jobs/<id> or v1/boards/<company>/jobs/<id>
        for i, part in enumerate(parts):
            if part == "jobs" and i + 1 < len(parts):
                job_id = parts[i + 1]
                # Company is the part before "jobs", skipping "v1" and "boards"
                for j in range(i - 1, -1, -1):
                    if parts[j] not in ("v1", "boards"):
                        company_slug = parts[j]
                        break
                break

    # Career page with gh_jid parameter
    if not job_id:
        from urllib.parse import parse_qs

        params = parse_qs(parsed.query)
        if "gh_jid" in params:
            job_id = params["gh_jid"][0]
            # Prefer explicit 'board' query param (e.g. ?board=weights_and_biases)
            if "board" in params:
                company_slug = params["board"][0]
            else:
                # Company from domain — skip generic subdomains
                _generic_subs = {
                    "careers",
                    "jobs",
                    "apply",
                    "hire",
                    "work",
                    "join",
                    "talent",
                    "recruiting",
                    "employment",
                    "opportunities",
                }
                _tld_parts = {"com", "org", "net", "io", "co", "ai", "dev", "app", "us", "uk"}
                domain_parts = host.replace("www.", "").split(".")
                candidates = [p for p in domain_parts if p not in _generic_subs and p not in _tld_parts]
                # Add subdomain as last-resort candidate
                if domain_parts and domain_parts[0] not in _tld_parts and domain_parts[0] not in candidates:
                    candidates.append(domain_parts[0])
                company_slug = candidates[0] if candidates else (domain_parts[0] if domain_parts else None)

    # Career page with numeric job ID in the path (e.g. /detail/7598877/)
    if not job_id:
        path_parts = parsed.path.strip("/").split("/")
        for part in path_parts:
            if part.isdigit() and len(part) >= 6:
                job_id = part
                break
        if job_id:
            _generic_subs = {
                "careers",
                "jobs",
                "apply",
                "hire",
                "work",
                "join",
                "talent",
                "recruiting",
                "employment",
                "opportunities",
            }
            _tld_parts = {"com", "org", "net", "io", "co", "ai", "dev", "app", "us", "uk"}
            domain_parts = host.replace("www.", "").split(".")
            candidates = [p for p in domain_parts if p not in _generic_subs and p not in _tld_parts]
            company_slug = candidates[0] if candidates else (domain_parts[0] if domain_parts else None)

    if not company_slug or not job_id:
        return False

    # Probe candidate slugs against the Greenhouse API
    candidates_to_try = [company_slug]
    if job_id:
        # Re-derive candidates if we have multiple to try
        domain_parts = host.replace("www.", "").split(".")
        _generic_subs = {
            "careers",
            "jobs",
            "apply",
            "hire",
            "work",
            "join",
            "talent",
            "recruiting",
            "employment",
            "opportunities",
        }
        _tld_parts = {"com", "org", "net", "io", "co", "ai", "dev", "app", "us", "uk"}
        extra = [p for p in domain_parts if p not in _generic_subs and p not in _tld_parts and p != company_slug]
        # Also try subdomain as last-resort
        if domain_parts and domain_parts[0] not in _tld_parts and domain_parts[0] != company_slug:
            extra.append(domain_parts[0])
        candidates_to_try = [company_slug] + extra
    # Try variations with common suffixes stripped (e.g. datadoghq → datadog)
    _slug_suffixes = ("hq", "inc", "labs", "tech")
    stripped: list[str] = []
    for candidate in candidates_to_try:
        for suffix in _slug_suffixes:
            if candidate.endswith(suffix) and len(candidate) > len(suffix) + 2:
                stripped.append(candidate[: -len(suffix)])
    candidates_to_try.extend(s for s in stripped if s not in candidates_to_try)

    import os as _os

    for slug in candidates_to_try:
        api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"
        _log(f"\n[greenhouse] Fetching API: {api_url}")
        try:
            ua = _os.environ.get("JOB_ASSETS_USER_AGENT", "run_pipeline/1.0")
            req = Request(api_url, headers={"User-Agent": ua})
            with urlopen(req, timeout=30) as resp:
                data = resp.read()
            output_path.write_bytes(data)
            _log(f"[greenhouse] Saved API JSON to {output_path}")
            return True
        except (URLError, OSError) as exc:
            _log(f"[greenhouse] API fetch failed for slug '{slug}': {exc}")
            continue

    _log("[greenhouse] All candidate slugs failed — falling back to scraper")
    return False


# ---------------------------------------------------------------------------
# Remote source sync
# ---------------------------------------------------------------------------

_GOOGLE_DOC_RE = re.compile(r"^https?://docs\.google\.com/document/d/([A-Za-z0-9_-]+)")


def _normalize_remote_source_url(source_url: str) -> str:
    normalized = str(source_url or "").strip()
    if not normalized:
        raise ValueError("source_url is required")

    match = _GOOGLE_DOC_RE.match(normalized)
    if match:
        doc_id = match.group(1)
        return f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    return normalized


def _configured_sync_source_url(env_var_name: str) -> str | None:
    raw = os.environ.get(env_var_name, "").strip()
    if not raw:
        return None
    return _normalize_remote_source_url(raw)


def _sync_google_doc(
    *,
    label: str,
    output_path: Path,
    state_path: Path,
    export_url: str,
    opener=urlopen,
) -> None:
    """Fetch a remote text source, updating the local file only when content changes."""
    _log(f"\n[sync] Fetching {label} from configured source...")

    t0 = time.monotonic()

    try:
        req = Request(export_url, headers={"User-Agent": "run_pipeline/1.0"})
        with opener(req, timeout=30) as resp:
            remote_bytes = resp.read()
    except (URLError, OSError) as exc:
        _log(f"[sync] Warning: could not fetch {label}: {exc}")
        _log("[sync] Continuing with local copy.")
        return

    remote_hash = _sha256(remote_bytes)

    # Load previous sync state
    prev_hash = None
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
            prev_hash = state.get("sha256")
        except (json.JSONDecodeError, OSError):
            pass

    # Also hash the current local file for comparison
    local_hash = None
    if output_path.exists():
        local_hash = _sha256(output_path.read_bytes())

    elapsed = time.monotonic() - t0

    if remote_hash == prev_hash and remote_hash == local_hash:
        _log(f"[sync] {label} is up to date.  ({elapsed:.1f}s)")
        return

    # Write the updated file
    output_path.write_bytes(remote_bytes)

    # Update sync state
    state_path.write_text(json.dumps({"sha256": remote_hash}, indent=2) + "\n")

    _log(f"[sync] {label} updated (hash: {remote_hash[:12]}...).  ({elapsed:.1f}s)")


def sync_work_stories() -> None:
    """Fetch work_stories.md from a configured source, update only if changed."""
    source_url = _configured_sync_source_url(WORK_STORIES_SOURCE_URL_ENV)
    if not source_url:
        _log(f"[sync] Skipping work_stories.md; {WORK_STORIES_SOURCE_URL_ENV} is not set.")
        return
    _sync_google_doc(
        label="work_stories.md",
        output_path=WORK_STORIES_PATH,
        state_path=SYNC_STATE_PATH,
        export_url=source_url,
    )


def sync_candidate_context() -> None:
    """Fetch candidate_context.md from a configured source, update only if changed."""
    source_url = _configured_sync_source_url(CANDIDATE_CONTEXT_SOURCE_URL_ENV)
    if not source_url:
        _log(f"[sync] Skipping candidate_context.md; {CANDIDATE_CONTEXT_SOURCE_URL_ENV} is not set.")
        return
    _sync_google_doc(
        label="candidate_context.md",
        output_path=CANDIDATE_CONTEXT_PATH,
        state_path=CANDIDATE_CONTEXT_SYNC_STATE_PATH,
        export_url=source_url,
    )


def sync_supporting_docs() -> None:
    """Fetch configured supporting sources concurrently to shave fixed startup latency."""
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(sync_work_stories),
            executor.submit(sync_candidate_context),
        ]
        for future in futures:
            future.result()


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the full deterministic resume pipeline.",
    )
    parser.add_argument(
        "jd_source",
        help="Path to a JD text file, or a URL (http/https) to scrape.",
    )
    parser.add_argument(
        "-c",
        "--company",
        default=None,
        help="Company slug (e.g. 'samsara'). Auto-detected from JD if omitted.",
    )
    parser.add_argument(
        "-r",
        "--role",
        default=None,
        help="Role slug (e.g. 'principal-pm-maintenance'). Auto-detected from JD if omitted.",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="Also build .docx/.pdf and validate (requires resume_content.json and cover_letter_text.txt).",
    )
    parser.add_argument(
        "--skip-sync",
        action="store_true",
        help="Skip configured source syncs (work_stories.md and candidate_context.md).",
    )
    parser.add_argument(
        "--meta-path-file",
        default=None,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    pipeline_t0 = time.monotonic()
    is_url = args.jd_source.startswith("http://") or args.jd_source.startswith("https://")
    resolved_jd_source = args.jd_source
    extraction_source_url = args.jd_source

    if is_url:
        # Resolve aggregator URLs (LinkedIn, Indeed, etc.) to board URLs first
        source = detect_source(args.jd_source)
        if source not in ("direct", "unknown"):
            _log(f"\n[resolve] Detected aggregator source: {source}")
            board_url = resolve_to_board_url(args.jd_source)
            if board_url:
                extraction_source_url = board_url
                resolved_jd_source = board_url
                _log(f"[resolve] Resolved to board URL: {resolved_jd_source}")
            else:
                _log(f"[resolve] Could not resolve {source} URL to board URL, proceeding with original")
        else:
            extraction_source_url = args.jd_source

        try:
            resolved_jd_source = resolve_job_source_url(extraction_source_url)
        except ValueError as exc:
            _log(f"\nERROR: {exc}")
            sys.exit(1)
        if looks_like_non_html_asset_url(resolved_jd_source) and not looks_like_non_html_asset_url(
            extraction_source_url
        ):
            _log(
                f"\n[resolve] Resolved URL points to a static asset ({resolved_jd_source}); "
                "using the original job URL instead."
            )
            resolved_jd_source = extraction_source_url
        if looks_like_unresolved_url_template(resolved_jd_source) and not looks_like_unresolved_url_template(
            extraction_source_url
        ):
            _log(
                f"\n[resolve] Resolved URL still contains an unresolved template ({resolved_jd_source}); "
                "using the original job URL instead."
            )
            resolved_jd_source = extraction_source_url
        if resolved_jd_source != extraction_source_url:
            _log(f"\n[resolve] Canonical job URL: {resolved_jd_source}")

    # ── Step 1: Sync configured remote sources ─────────────────────────────

    if not args.skip_sync:
        sync_supporting_docs()
    else:
        _log("\n[sync] Skipped (--skip-sync).")

    # ── Step 2: Acquire and parse JD ────────────────────────────────────
    # We need the parsed JD before we know the output dir (if auto-detecting),
    # so scrape/parse to a temp location first.

    tmp_dir = _create_pipeline_tmp_dir()
    tmp_parsed = tmp_dir / "jd_parsed.json"

    jd_extraction_method = "local-file"

    # Check if input is already a parsed JD JSON
    is_already_parsed = False
    if not is_url and args.jd_source.endswith(".json"):
        try:
            with open(args.jd_source) as f:
                candidate = json.load(f)
            # A parsed JD has these keys from parse_jd.py
            if isinstance(candidate, dict) and "title" in candidate and "keywords" in candidate:
                is_already_parsed = True
                jd_data = candidate
                shutil.copy2(args.jd_source, str(tmp_parsed))
                jd_extraction_method = "preparsed-json"
                _log("\n[parse] Input is already parsed JD — skipping parse step.")
        except (json.JSONDecodeError, OSError):
            pass

    if not is_already_parsed:
        if is_url:
            tmp_raw = tmp_dir / "jd_raw.md"
            extraction_attempts: list[tuple[str, str, list[str] | None, callable | None]] = []

            if looks_like_greenhouse_url(extraction_source_url):
                extraction_attempts.append(
                    ("greenhouse-api", "Validate Greenhouse API JD", None, _try_fetch_greenhouse_api)
                )

            extraction_attempts.append(
                (
                    "scrape",
                    "Scrape JD from URL",
                    [PYTHON, "scripts/scrape_job.py", extraction_source_url, "-o", str(tmp_raw)],
                    None,
                )
            )
            extraction_attempts.append(
                (
                    "scrape-raw",
                    "Retry scrape JD from URL (raw text)",
                    [PYTHON, "scripts/scrape_job.py", extraction_source_url, "--raw", "-o", str(tmp_raw)],
                    None,
                )
            )
            if not _url_prefers_stealth(extraction_source_url):
                extraction_attempts.append(
                    (
                        "scrape-stealth-raw",
                        "Retry scrape JD from URL (stealth raw text)",
                        [
                            PYTHON,
                            "scripts/scrape_job.py",
                            extraction_source_url,
                            "--stealth",
                            "--raw",
                            "-o",
                            str(tmp_raw),
                        ],
                        None,
                    )
                )

            # Retry the full extraction sequence with backoff on failure.
            # Many job boards rate-limit; retrying after a pause often succeeds.
            import os as _os

            _JD_FETCH_MAX_RETRIES = int(_os.environ.get("JD_FETCH_MAX_RETRIES", "5"))
            attempt_failures: list[tuple[str, list[str]]] = []
            jd_data = None
            _retry_after_override: float | None = None
            terminal_extraction_issue: str | None = None

            for jd_retry in range(_JD_FETCH_MAX_RETRIES + 1):
                if jd_retry > 0:
                    import random as _rand

                    if _retry_after_override is not None:
                        backoff = _retry_after_override + _rand.uniform(0, 2)
                        _log(
                            f"[extract] JD fetch retry {jd_retry}/{_JD_FETCH_MAX_RETRIES}"
                            f" — honouring Retry-After: {backoff:.0f}s..."
                        )
                        _retry_after_override = None
                    else:
                        backoff = min(2**jd_retry * 10, 120) + _rand.uniform(0, 5)
                        _log(f"[extract] JD fetch retry {jd_retry}/{_JD_FETCH_MAX_RETRIES} — waiting {backoff:.0f}s...")
                    time.sleep(backoff)
                    attempt_failures.clear()

                # Rotate User-Agent for each retry round and propagate to subprocesses
                ua = _next_user_agent()
                _os.environ["JOB_ASSETS_USER_AGENT"] = ua

                for method, label, command, fetcher in extraction_attempts:
                    # Enforce per-domain rate limit before each request
                    _enforce_domain_rate_limit(extraction_source_url)

                    candidate, issues = _run_url_extraction_attempt(
                        label=label,
                        command=command,
                        fetcher=fetcher,
                        url=extraction_source_url,
                        tmp_raw=tmp_raw,
                        tmp_parsed=tmp_parsed,
                    )
                    if candidate is not None:
                        jd_data = candidate
                        jd_extraction_method = method
                        _log(f"[extract] Validated website JD extraction via {method}.")
                        break

                    terminal_issue = _terminal_extraction_issue_from_issues(issues)
                    if terminal_issue:
                        attempt_failures.append((label, [terminal_issue]))
                        terminal_extraction_issue = terminal_issue
                        _log(f"[extract] {label} did not yield a usable JD:")
                        _log(f"  - {terminal_issue}")
                        break

                    # Check for HTTP 429 Retry-After hint in issue messages
                    for issue_text in issues:
                        if "retry-after:" in issue_text.casefold():
                            import re as _re

                            m = _re.search(r"retry-after:\s*(\d+)", issue_text, _re.IGNORECASE)
                            if m:
                                _retry_after_override = float(m.group(1))

                    attempt_failures.append((label, issues))
                    _log(f"[extract] {label} did not yield a usable JD:")
                    for issue in issues:
                        _log(f"  - {issue}")

                if jd_data is not None or terminal_extraction_issue is not None:
                    break

            # If scraping failed and URL wasn't already identified as Greenhouse,
            # try the Greenhouse API as a last-resort fallback (many custom career
            # pages are Greenhouse-backed with numeric IDs in the path).
            if (
                jd_data is None
                and terminal_extraction_issue is None
                and not looks_like_greenhouse_url(extraction_source_url)
            ):
                candidate, issues = _run_url_extraction_attempt(
                    label="Greenhouse API probe (fallback)",
                    command=None,
                    fetcher=_try_fetch_greenhouse_api,
                    url=extraction_source_url,
                    tmp_raw=tmp_raw,
                    tmp_parsed=tmp_parsed,
                )
                if candidate is not None:
                    jd_data = candidate
                    jd_extraction_method = "greenhouse-api"
                    _log("[extract] Validated website JD extraction via greenhouse-api (fallback probe).")
                else:
                    attempt_failures.append(("Greenhouse API probe (fallback)", issues))

            if jd_data is None:
                _log("\nERROR: URL-based JD extraction did not produce a usable job description.")
                _log("The workflow is stopping instead of generating assets from weak content.")
                for label, issues in attempt_failures:
                    _log(f"\n  Attempt: {label}")
                    for issue in issues:
                        _log(f"    - {issue}")
                _log("\nPlease retry later or provide the JD text directly.")
                sys.exit(1)
        else:
            jd_input_path = args.jd_source
            _run_step(
                "Parse JD",
                [PYTHON, "scripts/parse_jd.py", jd_input_path, "-o", str(tmp_parsed)],
            )

            with open(tmp_parsed) as f:
                jd_data = json.load(f)

    # ── Auto-detect company and role from parsed JD ──────────────────────

    company = args.company
    role = args.role
    jd_title = jd_data.get("title", "").strip()
    _resolved_host = urlparse(resolved_jd_source).hostname or ""
    title_company_name = _company_name_from_text(jd_title) if jd_title else None
    title_company_slug = _slugify(title_company_name) if title_company_name else None
    text_company_name = None

    if company is None:
        jd_company = jd_data.get("company", "").strip()
        if company_name_looks_generic(jd_company):
            jd_company = ""
        url_company = _company_slug_from_url(resolved_jd_source) if is_url else None
        text_company = None
        if is_url:
            tmp_raw_path = tmp_dir / "jd_raw.md"
            if tmp_raw_path.exists():
                try:
                    text_company_name = _company_name_from_text(tmp_raw_path.read_text())
                    text_company = _slugify(text_company_name) if text_company_name else None
                except OSError:
                    text_company_name = None
                    text_company = None

        # Prefer URL-extracted company for boards where the page title is unreliable
        # (e.g., Workday pages titled "FactSet Careers" → "factset-careers" vs. URL "factset")
        jd_slug = _slugify(jd_company) if jd_company else ""
        jd_is_generic_aggregator = jd_slug in {"linkedin", "indeed", "glassdoor"}
        if url_company and is_url and ("myworkdayjobs.com" in _resolved_host or "myworkdaysite.com" in _resolved_host):
            company = url_company
            _log(f"[auto] Using URL-derived company for Workday: {company}")
        elif title_company_slug and is_url and "linkedin.com" in _resolved_host and jd_is_generic_aggregator:
            company = title_company_slug
            _log(f"[auto] Parsed company '{jd_company}' is a LinkedIn shell value — using title-derived: {company}")
        elif jd_slug and len(jd_slug) <= 30:
            company = jd_slug
        elif text_company:
            company = text_company
            _log(f"[auto] Parsed company '{jd_company}' missing/weak — using JD-text-derived: {company}")
        elif url_company:
            company = url_company
            _log(f"[auto] Parsed company '{jd_company}' looks wrong — using URL-derived: {company}")
        elif jd_slug:
            company = jd_slug  # last resort, even if long

        if not company:
            _log("\nERROR: Could not auto-detect company name.")
            _log("  The parsed JD has no company field and the URL didn't help.")
            _log("  Please provide -c <company-slug> explicitly.")
            sys.exit(1)

        _log(f"\n[auto] Company detected: {company}")

    if role is None:
        if jd_title:
            role = _role_slug_from_title(jd_title)
        if not role:
            role = "general"
            _log(f"\n[auto] Could not detect role title — using '{role}'")
        else:
            _log(f"[auto] Role detected: {role} (from: {jd_title})")

    # ── Set up output directory and move parsed files ────────────────────

    out_dir = Path("output") / company / role
    out_dir.mkdir(parents=True, exist_ok=True)
    migrate_role_output_layout(out_dir)
    layout = ensure_role_output_dirs(out_dir)
    content_dir = layout["content"]
    documents_dir = layout["documents"]

    # Preserve the JD's company casing when available for file naming and metadata.
    company_proper = _resolve_company_proper(
        jd_company=str(jd_data.get("company", "") or ""),
        company_slug=company,
        title_company_name=title_company_name,
        text_company_name=text_company_name,
        resolved_host=_resolved_host,
        is_url=is_url,
    )

    # Move parsed JD to output dir
    jd_parsed_path = role_content_path(out_dir, "jd_parsed.json")
    shutil.copy2(str(tmp_parsed), str(jd_parsed_path))
    if is_url:
        jd_raw_dest = role_content_path(out_dir, "jd_raw.md")
        tmp_raw_path = tmp_dir / "jd_raw.md"
        if tmp_raw_path.exists():
            shutil.copy2(str(tmp_raw_path), str(jd_raw_dest))

    # File paths within output dir
    ranked_path = role_content_path(out_dir, "ranked_bullets.json")
    draft_path = role_content_path(out_dir, "resume_content_draft.json")
    resume_content_path = role_content_path(out_dir, "resume_content.json")
    cover_letter_text_path = role_content_path(out_dir, "cover_letter_text.txt")
    resume_docx_path = role_documents_path(out_dir, document_filename("Resume", company_proper, ".docx"))
    cover_letter_docx_path = role_documents_path(out_dir, document_filename("Cover Letter", company_proper, ".docx"))
    resume_pdf_path = role_documents_path(out_dir, document_filename("Resume", company_proper, ".pdf"))

    _log("\n" + "=" * 60)
    _log("  RESUME PIPELINE")
    _log(f"  Company:  {company} ({company_proper})")
    _log(f"  Role:     {role}")
    _log(f"  JD src:   {args.jd_source}")
    if resolved_jd_source != args.jd_source:
        _log(f"  JD resolved: {resolved_jd_source}")
    _log(f"  JD mode:  {jd_extraction_method}")
    _log(f"  Output:   {out_dir}/")
    _log(f"  Content:  {content_dir}/")
    _log(f"  Docs:     {documents_dir}/")
    _log(f"  Build:    {'yes' if args.build else 'no'}")
    _log("=" * 60)

    # ── Step 4: Rank bullets ───────────────────────────────────────────────

    _run_step(
        "Rank bullets",
        [PYTHON, "scripts/rank_bullets.py", str(jd_parsed_path), "-o", str(ranked_path)],
    )

    # ── Step 5: Draft resume ───────────────────────────────────────────────

    _run_step(
        "Draft resume content",
        [PYTHON, "scripts/draft_resume.py", str(ranked_path), "-o", str(draft_path)],
    )

    # ── Summary ────────────────────────────────────────────────────────────

    _log(f"\n{'=' * 60}")
    _log("  PIPELINE COMPLETE — Deterministic steps finished")
    _log(f"{'=' * 60}")
    _log("  Produced:")
    _log(f"    {jd_parsed_path}")
    _log(f"    {ranked_path}")
    _log(f"    {draft_path}")

    if not args.build:
        _log(f"\n  Next: review {draft_path}, have the LLM produce")
        _log(f"    {resume_content_path}")
        _log(f"    {cover_letter_text_path}")
        _log("  Then re-run with --build to generate final documents.")
    _log("")

    # ── Write metadata for apply.sh to read ──────────────────────────────

    meta_path = out_dir / ".pipeline_meta.json"
    meta_payload = {
        "company": company,
        "company_proper": company_proper,
        "role": role,
        "jd_title": jd_data.get("title", ""),
        "jd_source": args.jd_source,
        "jd_source_resolved": resolved_jd_source,
        "jd_extraction_method": jd_extraction_method,
        "out_dir": str(out_dir),
    }
    meta_payload.update(role_layout_metadata(out_dir))
    meta_path.write_text(json.dumps(meta_payload, indent=2) + "\n")
    if args.meta_path_file:
        Path(args.meta_path_file).write_text(str(meta_path.resolve()) + "\n", encoding="utf-8")

    # ── Build phase (--build) ──────────────────────────────────────────────

    if args.build:
        # Verify LLM outputs exist
        missing = []
        if not resume_content_path.exists():
            missing.append(str(resume_content_path))
        if not cover_letter_text_path.exists():
            missing.append(str(cover_letter_text_path))

        if missing:
            _log("ERROR: --build requires these LLM-produced files, which are missing:")
            for m in missing:
                _log(f"  - {m}")
            _log("\nRun the LLM step first to produce these files, then re-run with --build.")
            sys.exit(1)

        # Snapshot original LLM outputs before any build-phase mutations
        if resume_content_path.exists():
            shutil.copy2(resume_content_path, content_dir / "resume_content.json.original")
        if cover_letter_text_path.exists():
            shutil.copy2(cover_letter_text_path, content_dir / "cover_letter_text.txt.original")

        # ── Step 7: Build resume + cover letter ─────────────────────────────
        # Run sequentially — LibreOffice PDF conversion can't run concurrently

        _run_step(
            "Enforce resume policy",
            [PYTHON, "scripts/enforce_resume_policy.py", str(resume_content_path)],
        )

        _run_step(
            "Optimize page break",
            [PYTHON, "scripts/optimize_page_break.py", str(resume_content_path)],
        )

        _run_step(
            "Build resume",
            [PYTHON, "scripts/build_resume.py", str(resume_content_path), "-o", str(resume_docx_path)],
        )

        # Ensure cover letter has greeting + signoff before building
        _normalize_cover_letter(cover_letter_text_path)

        _run_step(
            "Build cover letter",
            [PYTHON, "scripts/build_cover_letter.py", str(cover_letter_text_path), "-o", str(cover_letter_docx_path)],
        )

        # ── Step 8: Validate resume PDF ────────────────────────────────────

        if resume_pdf_path.exists():
            _run_step(
                "Validate resume PDF",
                [PYTHON, "scripts/validate_resume.py", str(resume_pdf_path)],
            )
        else:
            _log(f"\n[validate] Skipped: PDF not found at {resume_pdf_path}")
            _log("  (LibreOffice may not be installed for .docx -> .pdf conversion)")

        # ── Final summary ──────────────────────────────────────────────────

        _log(f"\n{'=' * 60}")
        _log("  BUILD COMPLETE")
        _log(f"{'=' * 60}")
        _log("  Produced:")
        _log(f"    {resume_docx_path}")
        _log(f"    {cover_letter_docx_path}")
        if resume_pdf_path.exists():
            _log(f"    {resume_pdf_path}")
        _log("")

    total_elapsed = time.monotonic() - pipeline_t0
    _log(f"Total pipeline time: {total_elapsed:.1f}s\n")


if __name__ == "__main__":
    main()
