#!/usr/bin/env python3
"""Scrape a job posting URL and extract structured job description content.

Usage:
    python scripts/scrape_job.py <url> [--output FILE] [--stealth] [--raw]

Options:
    --output FILE   Write output to file instead of stdout
    --stealth       Use StealthyFetcher for sites with anti-bot protection
    --raw           Output raw text without cleaning

Examples:
    python scripts/scrape_job.py "https://boards.greenhouse.io/company/jobs/123"
    python scripts/scrape_job.py "https://lever.co/company/abc" --stealth
    python scripts/scrape_job.py "https://linkedin.com/jobs/view/123" --stealth --output jd.md
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - optional dependency in some environments
    BeautifulSoup = None

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from browser_runtime import launch_chromium_browser
from job_board_urls import (
    ashby_job_id_from_url,
    canonical_icims_job_url,
    dover_job_or_search_id_from_url,
    greenhouse_board_slug_from_url,
    greenhouse_job_id_from_url,
    looks_like_ashby_url,
    looks_like_dover_url,
    looks_like_greenhouse_url,
    looks_like_icims_url,
    looks_like_workday_url,
    resolve_job_source_url,
)

STEALTH_DOMAINS = (
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "workday.com",
    "myworkdayjobs.com",
    "myworkdaysite.com",
)
RENDERED_FALLBACK_DOMAINS = (
    "jobs.gem.com",
    "ats.rippling.com",
    "jobs.smartrecruiters.com",
    "bamboohr.com",
    "uber.com",
)
BLOCKER_PATTERNS = (
    "javascript must be enabled",
    "you need to enable javascript to run this app",
    "please enable javascript to continue",
    "unsupported browser",
    "access denied",
    "verify you are human",
    "captcha",
)
MANUAL_CHALLENGE_MARKERS = (
    "access denied",
    "you don't have permission to access",
    "access is temporarily restricted",
    "unusual activity from your device or network",
    "verify you are human",
    "captcha-delivery.com",
    "geo.captcha-delivery.com",
    "datadome device check",
    "errors.edgesuite.net",
)
TERMINAL_SCRAPE_ERROR_PREFIXES = ("job_closed:", "needs_board_url:", "skipped_captcha:", "unsupported:")
CLOUDFLARE_ACCOUNT_ENV_VARS = (
    "CLOUDFLARE_ACCOUNT_ID",
    "CF_ACCOUNT_ID",
)
CLOUDFLARE_TOKEN_ENV_VARS = (
    "CLOUDFLARE_BROWSER_RENDERING_API_TOKEN",
    "CLOUDFLARE_API_TOKEN",
    "CF_API_TOKEN",
)

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)


def _get_user_agent() -> str:
    """Return the User-Agent string, preferring the rotated value from the pipeline."""
    return os.environ.get("JOB_ASSETS_USER_AGENT", _DEFAULT_USER_AGENT)


def maybe_reexec_with_uv() -> None:
    """Re-exec under uv when optional scraper deps are missing."""
    if os.environ.get("JOB_ASSETS_SCRAPE_BOOTSTRAPPED") == "1":
        return
    if not shutil.which("uv"):
        return

    env = os.environ.copy()
    env["JOB_ASSETS_SCRAPE_BOOTSTRAPPED"] = "1"
    project_root = Path(__file__).resolve().parent.parent
    cmd = ["uv", "run", "--project", str(project_root), "--extra", "fetchers", "python", __file__, *sys.argv[1:]]
    print("[scrape_job] Re-running under uv to provide scraper dependencies...", file=sys.stderr)
    raise SystemExit(subprocess.call(cmd, env=env))


def load_scrapling_fetchers():
    """Load scrapling fetchers if available, otherwise retry under uv."""
    try:
        from scrapling.fetchers import Fetcher, StealthyFetcher
    except ImportError:
        maybe_reexec_with_uv()
        return None, None
    return Fetcher, StealthyFetcher


def needs_stealth(url: str) -> bool:
    """Check if a URL likely needs stealth fetching."""
    lowered = url.lower()
    return any(domain in lowered for domain in STEALTH_DOMAINS)


def strip_html(raw: str) -> str:
    """Strip HTML tags and decode entities into readable text."""
    text = html.unescape(raw)
    text = html.unescape(text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.I)
    text = re.sub(r"</(?:p|div|h[1-6]|ul|ol|li|tr|td|th|table|section|header|footer|article)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_text(text: str) -> str:
    """Clean extracted text into readable markdown-like format."""
    lines = text.split("\n")
    cleaned = []
    prev_blank = False

    for line in lines:
        line = line.strip()

        if not line:
            if not prev_blank:
                cleaned.append("")
                prev_blank = True
            continue

        prev_blank = False

        noise = [
            "cookie",
            "accept all",
            "privacy policy",
            "sign in",
            "log in",
            "create account",
            "subscribe",
            "newsletter",
            "follow us",
            "share this",
            "apply now",
            "back to",
            "similar jobs",
        ]
        if any(n in line.lower() for n in noise) and len(line) < 60:
            continue

        cleaned.append(line)

    result = "\n".join(cleaned)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def _env_first(*names: str) -> str:
    """Return the first non-empty environment value from the provided names."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def extract_job_content(page) -> dict:
    """Extract structured job posting content from a scrapling page."""
    result = {
        "title": "",
        "company": "",
        "location": "",
        "full_text": "",
    }

    title_selectors = [
        "h1.posting-headline",
        "h1.job-title",
        "h1[class*='title']",
        "h1[class*='job']",
        ".job-title h1",
        ".posting-headline h1",
        "h1",
    ]
    for sel in title_selectors:
        el = page.css(sel)
        if el:
            result["title"] = el[0].text.strip()
            break

    location_selectors = [
        ".location",
        "[class*='location']",
        "[class*='Location']",
        ".job-location",
    ]
    for sel in location_selectors:
        el = page.css(sel)
        if el:
            result["location"] = el[0].text.strip()
            break

    company_selectors = [
        ".company-name",
        "[class*='company']",
        "[class*='Company']",
        "[class*='employer']",
    ]
    for sel in company_selectors:
        el = page.css(sel)
        if el:
            result["company"] = el[0].text.strip()
            break

    jd_selectors = [
        ".job-description",
        ".posting-page",
        "[class*='description']",
        "[class*='job-details']",
        "[class*='jobDescription']",
        "[class*='job_description']",
        "#job-details",
        "#job-description",
        "article",
        "main",
    ]

    full_text = ""
    for sel in jd_selectors:
        el = page.css(sel)
        if el:
            full_text = el[0].get_all_text(separator="\n", strip=True)
            if len(full_text) > 100:
                break

    if len(full_text) < 100:
        full_text = page.get_all_text(separator="\n", strip=True)

    result["full_text"] = full_text
    return result


def fetch_page(url: str, use_stealth: bool = False):
    """Fetch a page with scrapling."""
    Fetcher, StealthyFetcher = load_scrapling_fetchers()
    if Fetcher is None or StealthyFetcher is None:
        raise RuntimeError("scrapling is unavailable")

    if use_stealth:
        print("[scrape_job] Using StealthyFetcher...", file=sys.stderr)
        return StealthyFetcher.fetch(url, headless=True, network_idle=True)

    print("[scrape_job] Using Fetcher...", file=sys.stderr)
    return Fetcher.get(url)


def _normalize_request_url(url: str) -> str:
    parsed = urlparse(url)
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    return urlunparse(parsed._replace(path=path))


def fetch_raw_html(url: str) -> str:
    """Fetch raw HTML using stdlib urllib as a structured fallback path."""
    normalized_url = _normalize_request_url(url)
    req = Request(
        normalized_url,
        headers={"User-Agent": _get_user_agent()},
    )
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(f"job_closed: URL returned HTTP 404 at {normalized_url}") from exc
        if exc.code == 403 and str(exc.headers.get("cf-mitigated") or "").casefold() == "challenge":
            raise RuntimeError(
                "needs_board_url: wrapper URL is protected by an anti-bot challenge and did not resolve to a job board URL "
                f"({normalized_url})"
            ) from exc
        body = exc.read().decode("utf-8", errors="replace")
        blocked_reason = _manual_challenge_reason_from_text(
            body,
            normalized_url,
            response_status=exc.code,
            server_header=str(exc.headers.get("server") or ""),
        )
        if blocked_reason:
            raise RuntimeError(blocked_reason) from exc
        if exc.code == 429:
            retry_after = exc.headers.get("Retry-After", "")
            msg = "HTTP 429 Too Many Requests"
            if retry_after:
                msg += f" (Retry-After: {retry_after})"
            print(f"[scrape_job] {msg}", file=sys.stderr)
        raise


def _greenhouse_location(payload: dict) -> str:
    location = payload.get("location")
    if isinstance(location, dict):
        return str(location.get("name") or "").strip()
    return str(location or "").strip()


def extract_greenhouse_job_data(payload: dict, url: str) -> dict | None:
    if not isinstance(payload, dict):
        return None

    title = str(payload.get("title") or "").strip()
    content = strip_html(str(payload.get("content") or "")).strip()
    if not title or not content:
        return None

    company = str(payload.get("company_name") or _company_from_url(url) or "").strip()
    location = _greenhouse_location(payload)

    extra_lines: list[str] = []
    for key, label in (("departments", "Department"), ("offices", "Office")):
        for item in payload.get(key) or []:
            if isinstance(item, dict) and item.get("name"):
                extra_lines.append(f"{label}: {item['name']}")

    full_text = content
    if extra_lines:
        full_text = f"{full_text}\n\n" + "\n".join(extra_lines)

    return {
        "title": title,
        "company": company,
        "location": location,
        "full_text": full_text,
    }


def fetch_greenhouse_job_data(url: str) -> dict | None:
    direct_url = resolve_job_source_url(url)
    if not looks_like_greenhouse_url(direct_url):
        return None

    board_slug = greenhouse_board_slug_from_url(direct_url)
    job_id = greenhouse_job_id_from_url(direct_url)
    if not board_slug or not job_id:
        return None

    api_url = f"https://boards-api.greenhouse.io/v1/boards/{board_slug}/jobs/{job_id}"
    req = Request(api_url, headers={"User-Agent": _get_user_agent()})
    try:
        with urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(f"job_closed: Greenhouse posting is no longer available at {direct_url}") from exc
        raise
    return extract_greenhouse_job_data(payload, direct_url)


def _dover_location(payload: dict) -> str:
    explicit = str(payload.get("location") or "").strip()
    if explicit:
        return explicit

    values: list[str] = []
    for item in payload.get("locations") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            location_option = item.get("location_option")
            if isinstance(location_option, dict):
                name = str(location_option.get("display_name") or "").strip()
        if name and name not in values:
            values.append(name)
    return ", ".join(values)


def _dover_compensation_text(payload: dict) -> str:
    compensation = payload.get("compensation")
    if not isinstance(compensation, dict):
        return ""
    if not compensation.get("open_to_sharing_comp"):
        return ""

    lower = compensation.get("lower_bound")
    upper = compensation.get("upper_bound")
    currency = str(compensation.get("currency_code") or "").strip()
    salary_range_type = str(compensation.get("salary_range_type") or "").replace("_", " ").strip().casefold()
    employment_type = str(compensation.get("employment_type") or "").replace("_", " ").strip().casefold()

    amount_text = ""
    if isinstance(lower, (int, float)) and isinstance(upper, (int, float)):
        amount_text = f"{int(lower):,}-{int(upper):,}"
    elif isinstance(lower, (int, float)):
        amount_text = f"{int(lower):,}+"
    elif isinstance(upper, (int, float)):
        amount_text = f"Up to {int(upper):,}"

    suffix_parts = [part for part in (salary_range_type, employment_type) if part]
    suffix = f" {' '.join(suffix_parts)}".strip()
    if amount_text and currency:
        amount_text = f"{currency} {amount_text}"
    elif amount_text and not currency:
        amount_text = f"${amount_text}"
    return f"{amount_text} {suffix}".strip()


def extract_dover_job_data(payload: dict, url: str) -> dict | None:
    if not isinstance(payload, dict):
        return None

    description_html = str(payload.get("user_provided_description") or "").strip()
    full_text = strip_html(description_html)
    if not full_text:
        return None

    location = _dover_location(payload)
    compensation_text = _dover_compensation_text(payload)
    supplements = []
    if location:
        supplements.append(f"Location: {location}")
    if compensation_text:
        supplements.append(f"Compensation: {compensation_text}")
    if supplements:
        full_text = "\n\n".join([full_text, *supplements])

    data = {
        "title": str(payload.get("title") or "").strip(),
        "company": str(payload.get("client_name") or "").strip() or _company_from_url(url),
        "location": location,
        "full_text": full_text,
    }
    return data if data["title"] and data["full_text"] else None


def fetch_dover_job_data(url: str) -> dict | None:
    job_or_search_id = dover_job_or_search_id_from_url(url)
    if not job_or_search_id:
        return None

    api_url = f"https://app.dover.com/api/v1/inbound/application-portal-job/{quote(job_or_search_id, safe='')}"
    req = Request(
        api_url,
        headers={"User-Agent": _get_user_agent()},
    )
    with urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    return extract_dover_job_data(payload, url)


def fetch_icims_job_data(url: str) -> dict | None:
    """Fetch iCIMS job content from the board-served iframe surfaces.

    Some tenants canonicalize the public job URL to a branded wrapper page and
    only expose the actual JD on the ``/job?in_iframe=1`` surface. Try those
    candidates first so asset generation does not get stuck on marketing or
    intro shells.
    """
    if not looks_like_icims_url(url):
        return None

    canonical_url = canonical_icims_job_url(url).rstrip("/")
    candidate_urls = (
        f"{canonical_url}/job?in_iframe=1",
        f"{canonical_url}/job",
        f"{canonical_url}?in_iframe=1",
    )

    seen: set[str] = set()
    for candidate_url in candidate_urls:
        normalized = candidate_url.rstrip("/")
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            html_doc = fetch_raw_html(candidate_url)
        except Exception as exc:
            print(f"[scrape_job] iCIMS iframe fallback fetch failed for {candidate_url}: {exc}", file=sys.stderr)
            if _is_terminal_scrape_error(exc):
                raise RuntimeError(str(exc)) from exc
            continue

        data = extract_structured_html_fallback(html_doc, candidate_url)
        if data and is_usable_job_content(data):
            return data

    return None


def _workday_cxs_api_url(url: str) -> str | None:
    """Build the Workday CXS JSON API URL from a Workday job page URL.

    myworkdayjobs.com pattern:
        Page: {company}.wd{N}.myworkdayjobs.com/{site}/job/{loc}/{title}_{id}
        API:  {company}.wd{N}.myworkdayjobs.com/wday/cxs/{company}/{site}/job/{loc}/{title}_{id}

    myworkdaysite.com pattern:
        Page: wd{N}.myworkdaysite.com/en-US/recruiting/{co1}/{co2}/job/{loc}/{title}_{id}
        API:  wd{N}.myworkdaysite.com/wday/cxs/{co1}/{co2}/job/{loc}/{title}_{id}
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path

    if "myworkdayjobs.com" in host:
        m = re.match(r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs", host)
        if not m:
            return None
        company = m.group(1)
        parts = [part for part in path.strip("/").split("/") if part]
        if parts and re.fullmatch(r"[A-Za-z]{2}(?:[-_][A-Za-z]{2,8}){1,2}", parts[0]):
            parts = parts[1:]
        if not parts:
            return None
        return urlunparse(parsed._replace(path="/wday/cxs/" + company + "/" + "/".join(parts), query=""))

    if "myworkdaysite.com" in host:
        # Path: /en-US/recruiting/{co1}/{co2}/job/{loc}/{title}_{id}
        parts = path.strip("/").split("/")
        # Find 'recruiting' and extract from the segment after it
        recruiting_idx = next((i for i, p in enumerate(parts) if p == "recruiting"), -1)
        if recruiting_idx < 0 or recruiting_idx + 2 >= len(parts):
            return None
        # Build CXS path: /wday/cxs/{co1}/{co2}/job/...
        cxs_parts = parts[recruiting_idx + 1 :]
        return urlunparse(parsed._replace(path="/wday/cxs/" + "/".join(cxs_parts), query=""))

    return None


def fetch_workday_job_data(url: str) -> dict | None:
    """Fetch job data from the Workday CXS API."""
    api_url = _workday_cxs_api_url(url)
    if not api_url:
        return None

    req = Request(
        api_url,
        headers={
            "Accept": "application/json",
            "User-Agent": _get_user_agent(),
        },
    )
    try:
        with urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None

    info = payload.get("jobPostingInfo")
    if not info or not info.get("title"):
        return None

    title = info["title"]
    raw_desc = info.get("jobDescription", "")
    description = strip_html(raw_desc)
    location = info.get("location", "")
    additional_locations = info.get("additionalLocations", [])
    if additional_locations:
        location = f"{location}; {'; '.join(additional_locations)}"
    company = (payload.get("hiringOrganization") or {}).get("name", "")

    return {
        "title": title,
        "company": company,
        "location": location,
        "full_text": description,
        "url": url,
    }


def load_playwright_sync():
    """Load Playwright's sync API if available, otherwise retry under uv."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        maybe_reexec_with_uv()
        return None
    return sync_playwright


def _should_try_rendered_browser(url: str, html_doc: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    lowered = (html_doc or "").casefold()
    if any(domain in host for domain in RENDERED_FALLBACK_DOMAINS):
        return True
    if any(pattern in lowered for pattern in BLOCKER_PATTERNS):
        return True

    # Some custom-hosted Greenhouse wrappers return an empty or near-empty shell
    # to raw fetchers even though the rendered page contains the JD.
    if looks_like_greenhouse_url(url):
        visible_text = clean_text(strip_html(html_doc))
        if len(visible_text) < 80 and greenhouse_job_id_from_url(url):
            return True

    return False


def _trim_rendered_text_before_markers(text: str, markers: tuple[str, ...]) -> str:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if lines and lines[-1]:
                lines.append("")
            continue
        if line in markers:
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _infer_gem_location(lines: list[str], title: str) -> str:
    generic_values = {
        "product",
        "engineering",
        "design",
        "sales",
        "marketing",
        "operations",
        "hybrid",
        "remote",
        "on-site",
        "onsite",
        "full-time",
        "part-time",
        "contract",
        "internship",
    }
    normalized_title = title.casefold().strip()
    for line in lines[:8]:
        lowered = line.casefold().strip()
        if (
            not lowered
            or lowered == "view all jobs"
            or lowered == normalized_title
            or lowered in generic_values
            or lowered.startswith("about ")
        ):
            continue
        if len(line) <= 80:
            return line
    return ""


def extract_rendered_browser_data(url: str, title: str, body_text: str) -> dict | None:
    """Normalize text captured from a rendered browser fallback."""
    cleaned = clean_text(body_text or "")
    if not cleaned:
        return None

    host = (urlparse(url).hostname or "").casefold()
    linkedin_company = _company_from_linkedin_title(title) if "linkedin.com" in host else ""
    if "jobs.gem.com" in host:
        cleaned = _trim_rendered_text_before_markers(
            cleaned,
            ("Ready to apply?", "Apply and save", "Apply without saving", "First name"),
        )
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        if lines and lines[0].casefold() == "view all jobs":
            lines = lines[1:]
        normalized_title = title.strip() or (lines[0] if lines else "")
        company = _company_from_url(url)
        for line in lines:
            if line.lower().startswith("about ") and len(line.split()) <= 4:
                company = line[6:].strip() or company
                break
        cleaned = "\n".join(lines).strip()
        data = {
            "title": normalized_title,
            "company": company,
            "location": _infer_gem_location(lines[1:], normalized_title),
            "full_text": cleaned,
        }
        return data if is_usable_job_content(data) else None

    if "jobs.smartrecruiters.com" in host:
        company = _company_from_url(url)
        normalized_title = re.sub(r"\s+\|\s+SmartRecruiters\s*$", "", title.strip(), flags=re.I)
        if company and normalized_title.casefold().startswith(company.casefold() + " "):
            normalized_title = normalized_title[len(company) :].strip(" -|")
        data = {
            "title": normalized_title or title.strip() or _company_from_url(url),
            "company": company,
            "location": "",
            "full_text": cleaned,
        }
        return data if is_usable_job_content(data) else None

    if "bamboohr.com" in host:
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        while lines and lines[0].casefold() in {"privacy policy", "job openings"}:
            lines.pop(0)
        normalized_title = title.strip()
        if not normalized_title or normalized_title.casefold() == "bamboohr":
            normalized_title = lines[0] if lines else _company_from_url(url)
        location = ""
        for index, line in enumerate(lines):
            if line.casefold() == "location" and index + 1 < len(lines):
                location = lines[index + 1]
                break
        if not location and len(lines) >= 2 and " - " in lines[1]:
            location = lines[1].split(" - ", 1)[1].strip()
        cleaned = _trim_rendered_text_before_markers(
            "\n".join(lines),
            (
                "Apply for This Job",
                "Link to This Job",
                "Location",
                "Department",
                "Employment Type",
                "Minimum Experience",
                "Privacy Policy",
                "Terms of Service",
                "© BambooHR All rights reserved.",
            ),
        )
        data = {
            "title": normalized_title,
            "company": _company_from_url(url),
            "location": location,
            "full_text": cleaned,
        }
        return data if is_usable_job_content(data) else None

    data = {
        "title": title.strip() or _company_from_url(url),
        "company": linkedin_company or _company_from_url(url),
        "location": "",
        "full_text": cleaned,
    }
    return data if is_usable_job_content(data) else None


def extract_rendered_browser_fallback(url: str) -> dict | None:
    """Use Playwright-rendered DOM text when static HTML is only a JS shell."""
    sync_playwright = load_playwright_sync()
    if sync_playwright is None:
        return None

    with sync_playwright() as playwright:
        browser = launch_chromium_browser(
            playwright,
            headless=True,
            slow_mo=0,
            channel_env_var="SCRAPE_JOB_BROWSER_CHANNEL",
            executable_env_var="SCRAPE_JOB_BROWSER_EXECUTABLE",
            purpose="scrape_job rendered fallback",
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1600})
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_function(
                "() => document.body && (document.body.innerText || '').trim().length >= 500",
                timeout=20000,
            )
            page.wait_for_timeout(1500)
            title = page.title().strip()
            body_text = page.evaluate("() => (document.body && document.body.innerText) || ''")
            blocked_reason = _manual_challenge_reason_from_text("\n".join((title, body_text)), url)
            if blocked_reason:
                raise RuntimeError(blocked_reason)
            unavailable_reason = _job_unavailable_reason_from_extracted_data(
                url,
                {
                    "title": title,
                    "full_text": body_text,
                },
            )
            if unavailable_reason:
                raise RuntimeError(unavailable_reason)
            return extract_rendered_browser_data(url, title, body_text)
        finally:
            browser.close()


def _find_jobposting(obj):
    if isinstance(obj, dict):
        job_type = str(obj.get("@type", "")).lower()
        if job_type == "jobposting":
            return obj
        for value in obj.values():
            found = _find_jobposting(value)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_jobposting(item)
            if found:
                return found
    return None


def _first_meta_content(html_doc: str, *names: str) -> str:
    for name in names:
        pattern = rf'<meta[^>]+(?:name|property)=["\']{re.escape(name)}["\'][^>]+content=["\'](.*?)["\']'
        match = re.search(pattern, html_doc, flags=re.I | re.S)
        if match:
            return html.unescape(match.group(1)).strip()
    return ""


def _extract_title_from_html(html_doc: str) -> str:
    title = _first_meta_content(html_doc, "title", "og:title", "twitter:title")
    if title:
        title = re.sub(r"\s+@\s+.*$", "", title).strip()
        return title

    match = re.search(r"<title>(.*?)</title>", html_doc, flags=re.I | re.S)
    if not match:
        return ""
    title = html.unescape(match.group(1)).strip()
    return re.sub(r"\s+@\s+.*$", "", title).strip()


def _company_from_linkedin_title(title: str) -> str:
    """Extract the employer name from common LinkedIn job-title patterns."""
    normalized = re.sub(r"\s+[|·-]\s*linkedin\s*$", "", title.strip(), flags=re.I)
    if not normalized:
        return ""

    segments = [segment.strip() for segment in re.split(r"\s*[|·]\s*", normalized) if segment.strip()]
    if len(segments) >= 2:
        company = segments[-1].strip(" -|,")
        if company and company.casefold() != "linkedin":
            return company

    patterns = (
        r"^(?P<company>[A-Z0-9][A-Za-z0-9&.'()\-/, ]{1,80}?)\s+(?:is\s+)?hiring\b",
        r"^.+?\s+at\s+(?P<company>[A-Z0-9][A-Za-z0-9&.'()\-/, ]{1,80}?)(?:\s+[|·-]\s+.*)?$",
    )
    for pattern in patterns:
        match = re.match(pattern, normalized)
        if not match:
            continue
        company = match.group("company").strip(" -|,")
        if company and company.casefold() != "linkedin":
            return company
    return ""


def _extract_company_from_html(html_doc: str, url: str) -> str:
    hiring_org_match = re.search(
        r'<meta[^>]+itemprop=["\']hiringOrganization["\'][^>]+content=["\'](.*?)["\']',
        html_doc,
        flags=re.I | re.S,
    )
    if hiring_org_match:
        company = html.unescape(hiring_org_match.group(1)).strip()
        if company:
            return company

    parsed = urlparse(url)
    if "linkedin.com" in (parsed.hostname or ""):
        return _company_from_linkedin_title(_extract_title_from_html(html_doc))
    if "ashbyhq.com" in (parsed.hostname or ""):
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if parts:
            return parts[0].replace("-", " ").title()
    if any(host in (parsed.hostname or "") for host in ("joinbytedance.com", "jobs.bytedance.com")):
        return "ByteDance"
    return ""


def _company_from_url(url: str) -> str:
    """Best-effort company extraction from a job URL."""
    parsed = urlparse(url)
    host = parsed.hostname or ""

    if "linkedin.com" in host:
        return ""

    if "jobs.gem.com" in host:
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if parts:
            return parts[0].replace("-", " ").title()

    if "ats.rippling.com" in host:
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if parts:
            return parts[0].replace("-", " ").title()

    if "jobs.jobvite.com" in host:
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if parts:
            return parts[0].replace("-", " ").title()

    if "jobs.smartrecruiters.com" in host:
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if parts:
            return parts[0].replace("-", " ").title()

    if "ashbyhq.com" in host:
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if parts:
            return parts[0].replace("-", " ").title()

    if "joinbytedance.com" in host or "jobs.bytedance.com" in host:
        return "ByteDance"

    if looks_like_dover_url(url):
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) >= 2:
            return parts[1].replace("-", " ").title()

    domain_parts = [part for part in host.replace("www.", "").split(".") if part]
    generic = {"jobs", "careers", "boards", "apply", "ats", "app", "com", "io", "ai", "co", "org", "net"}
    for part in domain_parts:
        if part not in generic:
            return part.replace("-", " ").title()

    return ""


def _extract_json_object_after(html_doc: str, token: str) -> str | None:
    """Extract the first balanced JSON object that appears after a token."""
    start = html_doc.find(token)
    if start == -1:
        return None

    brace_start = html_doc.find("{", start)
    if brace_start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for idx in range(brace_start, len(html_doc)):
        ch = html_doc[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html_doc[brace_start : idx + 1]

    return None


def _extract_location_from_jobposting(jobposting: dict) -> str:
    if str(jobposting.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        return "Remote"

    location = jobposting.get("jobLocation")
    if isinstance(location, list) and location:
        location = location[0]

    if isinstance(location, dict):
        address = location.get("address", {})
        if isinstance(address, dict):
            raw_parts = [
                address.get("addressLocality"),
                address.get("addressRegion"),
                address.get("addressCountry"),
            ]
            parts: list[str] = []
            for part in raw_parts:
                if isinstance(part, dict):
                    name = str(part.get("name", "")).strip()
                    if name:
                        parts.append(name)
                elif isinstance(part, str) and part.strip():
                    parts.append(part.strip())
            return ", ".join(parts)

    return ""


def extract_jobposting_ld_json(html_doc: str, url: str) -> dict | None:
    """Extract JobPosting data from JSON-LD."""
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_doc,
        flags=re.I | re.S,
    ):
        try:
            payload = json.loads(html.unescape(match.group(1)))
        except json.JSONDecodeError:
            continue

        jobposting = _find_jobposting(payload)
        if not isinstance(jobposting, dict):
            continue

        company = ""
        hiring_org = jobposting.get("hiringOrganization")
        if isinstance(hiring_org, dict):
            company = str(hiring_org.get("name", "")).strip()

        title = str(jobposting.get("title", "")).strip()
        description = strip_html(str(jobposting.get("description", "")).strip())
        location = _extract_location_from_jobposting(jobposting)

        data = {
            "title": title or _extract_title_from_html(html_doc),
            "company": company or _extract_company_from_html(html_doc, url),
            "location": location,
            "full_text": description,
        }
        if data["full_text"]:
            return data

    return None


def extract_ashby_app_data(html_doc: str, url: str) -> dict | None:
    """Extract full posting details from Ashby's embedded window.__appData JSON."""
    payload_raw = _extract_json_object_after(html_doc, "window.__appData =")
    if not payload_raw:
        return None

    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        return None

    posting = payload.get("posting")
    if not isinstance(posting, dict):
        return None

    organization = payload.get("organization")
    company = organization.get("name", "") if isinstance(organization, dict) else ""
    description_html = str(posting.get("descriptionHtml", "")).strip()
    location_hints = [
        str(posting.get("locationName", "")).strip(),
        *[str(value).strip() for value in (posting.get("secondaryLocationNames") or []) if str(value).strip()],
    ]
    location = ", ".join(dict.fromkeys(value for value in location_hints if value))

    data = {
        "title": str(posting.get("title", "")).strip() or _extract_title_from_html(html_doc),
        "company": str(company).strip() or _extract_company_from_html(html_doc, url),
        "location": location,
        "full_text": strip_html(description_html),
    }
    if data["full_text"]:
        return data

    return None


def _is_bytedance_search_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return host.endswith(("joinbytedance.com", "jobs.bytedance.com"))


def _iter_bytedance_react_flight_chunks(html_doc: str) -> list[str]:
    chunks: list[str] = []
    pattern = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
    for match in pattern.finditer(html_doc):
        payload = match.group(1)
        try:
            decoded = json.loads(f'"{payload}"')
        except json.JSONDecodeError:
            continue
        decoded = html.unescape(decoded).strip()
        if decoded:
            chunks.append(decoded)
    return chunks


def _extract_bytedance_location(html_doc: str, title: str) -> str:
    if not title:
        return ""

    cleaned = clean_text(strip_html(html_doc))
    if not cleaned:
        return ""

    for match in re.finditer(re.escape(title), cleaned):
        window = cleaned[match.start() : match.start() + 1200]
        if "Location:" not in window:
            continue
        if not any(marker in window for marker in ("Responsibilities", "Qualifications", "Job Information")):
            continue
        location_match = re.search(r"Location:\s*\n+([^\n]+)", window)
        if location_match:
            return location_match.group(1).strip()
    return ""


def extract_bytedance_search_data(html_doc: str, url: str) -> dict | None:
    if not _is_bytedance_search_host(url):
        return None

    title = _extract_title_from_html(html_doc)
    if not title:
        return None

    markers = (
        "About ByteDance",
        "Why Join Us",
        "About the Team",
        "About the team",
        "Responsibilities",
        "Qualifications",
        "Job Information",
        "Minimum Qualification",
        "Preferred Qualification",
    )
    section_texts: list[str] = []
    seen_sections: set[str] = set()
    for chunk in _iter_bytedance_react_flight_chunks(html_doc):
        start = min((idx for idx in (chunk.find(marker) for marker in markers) if idx >= 0), default=-1)
        if start < 0:
            continue
        text = clean_text(chunk[start:])
        if not text or len(text) < 120:
            continue
        if text in seen_sections:
            continue
        seen_sections.add(text)
        section_texts.append(text)

    full_text = "\n\n".join(section_texts).strip()
    if not full_text:
        return None

    data = {
        "title": title,
        "company": _extract_company_from_html(html_doc, url) or _company_from_url(url),
        "location": _extract_bytedance_location(html_doc, title),
        "full_text": full_text,
    }
    return data if is_usable_job_content(data) else None


def _soup_block_text(node) -> str:
    if node is None:
        return ""
    return clean_text(node.get_text("\n", strip=True))


def _soup_inline_text(node) -> str:
    if node is None:
        return ""
    text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
    return text.strip(" ,")


def extract_jobvite_html_data(html_doc: str, url: str) -> dict | None:
    if "jv-job-detail-description" not in html_doc or BeautifulSoup is None:
        return None

    soup = BeautifulSoup(html_doc, "html.parser")
    description_node = soup.select_one(".jv-job-detail-description")
    if description_node is None:
        return None

    title = _soup_inline_text(soup.select_one("h2.jv-header"))
    if not title:
        title = re.sub(r"^\s*.+?\s+Careers\s*-\s*", "", _extract_title_from_html(html_doc), flags=re.I)
    location = ""
    meta_node = soup.select_one("p.jv-job-detail-meta")
    if meta_node is not None:
        meta_parts = [part.strip() for part in meta_node.stripped_strings if part.strip()]
        for index, part in enumerate(meta_parts):
            if "," not in part or part.lower().startswith("req.num"):
                continue
            location = part
            if index + 1 < len(meta_parts):
                next_part = meta_parts[index + 1]
                if next_part and "," not in next_part and not next_part.lower().startswith("req.num"):
                    location = f"{location} {next_part}".strip()
            break
    if location:
        location = re.sub(r"\s*,\s*", ", ", location)
        location = re.sub(r"\s+", " ", location).strip()

    data = {
        "title": title,
        "company": _extract_company_from_html(html_doc, url) or _company_from_url(url),
        "location": location,
        "full_text": _soup_block_text(description_node),
    }
    return data if is_usable_job_content(data) else None


def _looks_like_successfactors_html(html_doc: str) -> bool:
    lowered = html_doc.casefold()
    return any(
        marker in lowered
        for marker in (
            'data-careersite-propertyid="description"',
            'class="jobdescription"',
            "rmkcdn.successfactors.com",
            "performancemanager",
        )
    )


def extract_successfactors_html_data(html_doc: str, url: str) -> dict | None:
    if not _looks_like_successfactors_html(html_doc) or BeautifulSoup is None:
        return None

    soup = BeautifulSoup(html_doc, "html.parser")
    description_node = (
        soup.select_one('[data-careersite-propertyid="description"] .jobdescription')
        or soup.select_one('span[itemprop="description"] .jobdescription')
        or soup.select_one(".jobdescription")
    )
    if description_node is None:
        return None

    title = _soup_inline_text(soup.select_one('[data-careersite-propertyid="title"]'))
    if not title:
        title = _soup_inline_text(soup.select_one('[itemprop="title"]')) or _extract_title_from_html(html_doc)

    location_parts = []
    for prop in ("addressLocality", "addressRegion", "addressCountry"):
        node = soup.select_one(f'[itemprop="jobLocation"] [itemprop="{prop}"]')
        if node is not None:
            value = re.sub(r"\s+", " ", str(node.get("content") or "").strip())
            if value:
                location_parts.append(value)
    location = ", ".join(location_parts)
    if not location:
        location = _soup_inline_text(soup.select_one("#job-location"))
    if not location:
        location = _soup_inline_text(soup.select_one('[data-careersite-propertyid="location"]'))

    data = {
        "title": title,
        "company": _extract_company_from_html(html_doc, url) or _company_from_url(url),
        "location": location,
        "full_text": _soup_block_text(description_node),
    }
    return data if is_usable_job_content(data) else None


def extract_structured_html_fallback(html_doc: str, url: str) -> dict | None:
    """Extract JD data from structured HTML when generic scraping misses the content."""
    parsed = urlparse(url)
    host = parsed.hostname or ""

    if "ashbyhq.com" in host:
        ashby = extract_ashby_app_data(html_doc, url)
        if ashby:
            return ashby

    ld_json = extract_jobposting_ld_json(html_doc, url)
    if ld_json:
        return ld_json

    bytedance = extract_bytedance_search_data(html_doc, url)
    if bytedance:
        return bytedance

    jobvite = extract_jobvite_html_data(html_doc, url)
    if jobvite:
        return jobvite

    successfactors = extract_successfactors_html_data(html_doc, url)
    if successfactors:
        return successfactors

    description = _first_meta_content(html_doc, "description", "og:description", "twitter:description")
    if description:
        return {
            "title": _extract_title_from_html(html_doc),
            "company": _extract_company_from_html(html_doc, url),
            "location": "",
            "full_text": description,
        }

    return None


def _normalize_url_for_match(url: str) -> str:
    """Normalize URLs for record matching."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    normalized = f"{parsed.scheme}://{parsed.netloc}{path}"
    if parsed.query:
        normalized += f"?{parsed.query}"
    return normalized


def _cloudflare_credentials() -> tuple[str, str] | tuple[None, None]:
    """Return Cloudflare Browser Rendering credentials if configured."""
    account_id = _env_first(*CLOUDFLARE_ACCOUNT_ENV_VARS)
    api_token = _env_first(*CLOUDFLARE_TOKEN_ENV_VARS)
    if not account_id or not api_token:
        return None, None
    return account_id, api_token


def _cloudflare_request(account_id: str, api_token: str, method: str, path: str, body: dict | None = None) -> dict:
    """Call the Cloudflare Browser Rendering REST API."""
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/browser-rendering{path}"
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
        "User-Agent": "job-assets-scrape-job/1.0",
    }
    request = Request(url, data=payload, headers=headers, method=method)
    try:
        with urlopen(request, timeout=90) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Cloudflare Browser Rendering API returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cloudflare Browser Rendering API request failed: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Cloudflare Browser Rendering API returned invalid JSON") from exc

    if not data.get("success", False):
        errors = data.get("errors") or []
        if errors:
            detail = "; ".join(err.get("message", "unknown error") for err in errors if isinstance(err, dict))
        else:
            detail = data.get("message", "unknown error")
        raise RuntimeError(f"Cloudflare Browser Rendering API error: {detail}")

    return data


def _cloudflare_pick_record(records: list[dict], target_url: str) -> dict | None:
    """Pick the most relevant completed crawl record for the target URL."""
    normalized_target = _normalize_url_for_match(target_url)
    fallback = None

    for record in records:
        if not isinstance(record, dict) or record.get("status") != "completed":
            continue
        markdown = str(record.get("markdown", "")).strip()
        if not markdown:
            continue

        record_url = str(record.get("url", "")).strip()
        metadata = record.get("metadata")
        metadata_url = str(metadata.get("url", "")).strip() if isinstance(metadata, dict) else ""
        if any(
            candidate and _normalize_url_for_match(candidate) == normalized_target
            for candidate in (record_url, metadata_url)
        ):
            return record

        if fallback is None:
            fallback = record

    return fallback


def extract_cloudflare_crawl_record(crawl_result: dict, target_url: str) -> dict | None:
    """Extract the best crawl record from a Cloudflare crawl job response."""
    if not isinstance(crawl_result, dict):
        return None
    result = crawl_result.get("result")
    if not isinstance(result, dict):
        return None
    records = result.get("records")
    if not isinstance(records, list):
        return None
    return _cloudflare_pick_record(records, target_url)


def extract_cloudflare_crawl_fallback(url: str) -> dict | None:
    """Use Cloudflare Browser Rendering crawl as a last-resort extraction path."""
    account_id, api_token = _cloudflare_credentials()
    if not account_id or not api_token:
        return None

    body = {
        "url": url,
        "limit": 1,
        "depth": 0,
        "source": "links",
        "formats": ["markdown"],
        "render": True,
        "maxAge": 0,
        "options": {
            "includeExternalLinks": False,
            "includeSubdomains": False,
        },
        "gotoOptions": {
            "waitUntil": "networkidle2",
            "timeout": 60000,
        },
        "rejectResourceTypes": ["image", "media", "font"],
    }

    print("[scrape_job] Falling back to Cloudflare Browser Rendering crawl endpoint...", file=sys.stderr)
    create_response = _cloudflare_request(account_id, api_token, "POST", "/crawl", body)
    job_id = create_response.get("result")
    if not isinstance(job_id, str) or not job_id:
        raise RuntimeError("Cloudflare Browser Rendering crawl did not return a job id")

    max_attempts = int(os.environ.get("CLOUDFLARE_BROWSER_RENDERING_CRAWL_MAX_ATTEMPTS", "18"))
    delay_seconds = float(os.environ.get("CLOUDFLARE_BROWSER_RENDERING_CRAWL_DELAY_SECONDS", "3"))
    terminal_result = None

    for _ in range(max_attempts):
        status_response = _cloudflare_request(account_id, api_token, "GET", f"/crawl/{job_id}?limit=1")
        result = status_response.get("result", {})
        status = result.get("status")
        if status != "running":
            terminal_result = result
            break
        time.sleep(delay_seconds)

    if terminal_result is None:
        raise RuntimeError("Cloudflare Browser Rendering crawl did not complete within timeout")

    status = terminal_result.get("status")
    if status != "completed":
        raise RuntimeError(f"Cloudflare Browser Rendering crawl ended with status '{status}'")

    result_response = _cloudflare_request(account_id, api_token, "GET", f"/crawl/{job_id}?status=completed&limit=10")
    record = extract_cloudflare_crawl_record(result_response, url)
    if record is None:
        raise RuntimeError("Cloudflare Browser Rendering crawl completed but returned no usable records")

    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    title = str(metadata.get("title", "")).strip()
    title = re.sub(r"\s+[·|-]\s+.*$", "", title).strip() if title else title
    data = {
        "title": title,
        "company": _company_from_linkedin_title(title) or _company_from_url(url),
        "location": "",
        "full_text": str(record.get("markdown", "")).strip(),
    }
    return data if is_usable_job_content(data) else None


def is_usable_job_content(data: dict) -> bool:
    """Check whether extracted content looks like a real job description."""
    title = str(data.get("title", "")).strip()
    full_text = str(data.get("full_text", "")).strip()
    if not title or not full_text:
        return False

    cleaned = clean_text(full_text)
    lowered = cleaned.lower()
    if any(pattern in lowered for pattern in BLOCKER_PATTERNS):
        return False

    meaningful_lines = [line.strip() for line in cleaned.splitlines() if len(line.strip()) >= 20]
    meaningful_chars = sum(len(line) for line in meaningful_lines) or len(cleaned)
    return meaningful_chars >= 350


def _looks_like_generic_careers_title(normalized_title: str) -> bool:
    return (
        not normalized_title
        or normalized_title
        in {
            "career opportunities",
            "careers",
            "careers listing",
            "job opening",
            "job openings",
            "jobs",
            "open positions",
            "open roles",
        }
        or normalized_title.startswith(("careers at ", "make your move at ", "learn about "))
        or "browse jobs" in normalized_title
        or "open roles" in normalized_title
        or "job openings" in normalized_title
    )


def _looks_like_promotional_careers_landing(lowered_text: str) -> bool:
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
    return strong_marker_count >= 1 and marketing_marker_count >= 4 and role_marker_count == 0


def _job_unavailable_reason_from_extracted_data(url: str, data: dict) -> str | None:
    title = str(data.get("title", "")).strip()
    body_text = clean_text(str(data.get("full_text", "") or ""))
    combined_text = "\n".join(part for part in (title, body_text) if part).casefold()
    normalized_title = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()

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
    if any(marker in combined_text for marker in explicit_unavailable_markers):
        return "job_closed: The extracted page explicitly says the posting is unavailable."

    lowered_text = body_text.casefold()
    landing_markers = (
        "browse jobs",
        "open roles",
        "search jobs",
        "job alerts",
        "join our talent community",
        "career site",
        "view all jobs",
    )
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
    if (
        _looks_like_promotional_careers_landing(lowered_text)
        or _looks_like_generic_careers_title(normalized_title)
    ) and (
        (len(lowered_text) < 1500 and any(marker in lowered_text for marker in landing_markers))
        or _looks_like_promotional_careers_landing(lowered_text)
        or jobs_shown_pattern
        or listing_marker_count >= 2
    ):
        return "job_closed: The extracted page resolved to a generic careers landing page instead of a specific job posting."

    return None


def _job_unavailable_reason_from_html(html_doc: str, url: str) -> str | None:
    lowered = str(html_doc or "").casefold()
    compact = re.sub(r"\s+", "", lowered)
    host = (urlparse(url).hostname or "").casefold()
    if "window.__appdata" in lowered and '"organization":null' in compact and '"posting":null' in compact:
        return "job_closed: Ashby returned an unavailable posting shell instead of a live job description."

    if (
        "hibob.com" in host
        and "<careers-app-root" in lowered
        and "front.hibob.com" in lowered
        and "<title>careers</title>" in lowered
    ):
        return "unsupported: HiBob-hosted careers pages require dedicated board support and did not expose a static job description."

    if looks_like_workday_url(url) and ("postingavailable:false" in compact or '"postingavailable":false' in compact):
        return "job_closed: Workday shell reported postingAvailable=false for this job URL."

    return _job_unavailable_reason_from_extracted_data(
        url,
        {
            "title": _extract_title_from_html(html_doc),
            "full_text": strip_html(html_doc),
        },
    )


def _manual_challenge_reason_from_text(
    text: str,
    url: str,
    *,
    response_status: int | None = None,
    server_header: str = "",
) -> str | None:
    lowered = html.unescape(str(text or "")).casefold()
    collapsed = re.sub(r"\s+", " ", lowered).strip()
    if not collapsed:
        return None

    if any(marker in collapsed for marker in MANUAL_CHALLENGE_MARKERS):
        return (
            "skipped_captcha: The job board blocked access to the job description behind an anti-bot challenge "
            f"at {url}"
        )

    if response_status == 403 and "akamai" in server_header.casefold() and "access denied" in collapsed:
        return (
            "skipped_captcha: The job board blocked access to the job description behind an anti-bot challenge "
            f"at {url}"
        )
    return None


def _is_terminal_scrape_error(exc: object) -> bool:
    text = str(exc or "").strip().casefold()
    return text.startswith(TERMINAL_SCRAPE_ERROR_PREFIXES)


def _site_key(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold().removeprefix("www.")
    parts = [part for part in host.split(".") if part]
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def _same_site(url: str, seed_url: str) -> bool:
    return bool(_site_key(url) and _site_key(url) == _site_key(seed_url))


def _job_related_candidate_url(candidate: str, *, seed_url: str, job_id: str | None) -> bool:
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        return False

    lower = candidate.casefold()
    if job_id and job_id.casefold() in lower:
        return True
    if "ashbyhq.com" in lower:
        return True
    if not _same_site(candidate, seed_url):
        return False
    return any(
        token in lower
        for token in (
            "/career",
            "/careers",
            "/job",
            "/jobs",
            "/opening",
            "/openings",
            "/position",
            "/positions",
        )
    )


def _extract_candidate_urls_from_html(html_doc: str, base_url: str, *, job_id: str | None = None) -> list[str]:
    candidates: list[str] = []
    patterns = (
        r'<link[^>]+rel=["\'][^"\']*canonical[^"\']*["\'][^>]+href=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']',
        r'<iframe[^>]+src=["\']([^"\']+)["\']',
        r'<a[^>]+href=["\']([^"\']+)["\']',
    )

    for pattern in patterns:
        for match in re.finditer(pattern, html_doc, flags=re.I):
            raw_candidate = html.unescape(match.group(1).strip())
            if not raw_candidate:
                continue
            candidate = urljoin(base_url, raw_candidate)
            if _job_related_candidate_url(candidate, seed_url=base_url, job_id=job_id):
                candidates.append(candidate)

    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = candidate.rstrip("/")
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(candidate)
    return ordered


def _search_same_site_job_content(
    *,
    original_url: str,
    resolved_url: str,
    resolved_html_doc: str = "",
) -> tuple[dict, str] | None:
    job_id = ashby_job_id_from_url(original_url) or ashby_job_id_from_url(resolved_url)
    seed_urls = [original_url]
    if resolved_url != original_url:
        seed_urls.append(resolved_url)

    html_by_url: dict[str, str] = {}
    if resolved_html_doc:
        html_by_url[resolved_url] = resolved_html_doc

    candidate_urls: list[str] = []
    seen_candidates: set[str] = {resolved_url.rstrip("/")}

    for seed_url in seed_urls:
        html_doc = html_by_url.get(seed_url, "")
        if not html_doc:
            try:
                html_doc = fetch_raw_html(seed_url)
            except Exception as exc:
                print(f"[scrape_job] Same-site JD search could not fetch {seed_url}: {exc}", file=sys.stderr)
                continue
            html_by_url[seed_url] = html_doc

        if seed_url != resolved_url:
            seed_data = extract_structured_html_fallback(html_doc, seed_url)
            if seed_data and is_usable_job_content(seed_data):
                return seed_data, seed_url

        for candidate in _extract_candidate_urls_from_html(html_doc, seed_url, job_id=job_id):
            normalized = candidate.rstrip("/")
            if normalized in seen_candidates:
                continue
            seen_candidates.add(normalized)
            candidate_urls.append(candidate)

    for candidate_url in candidate_urls[:8]:
        try:
            candidate_html = fetch_raw_html(candidate_url)
        except Exception as exc:
            print(f"[scrape_job] Same-site JD search could not fetch {candidate_url}: {exc}", file=sys.stderr)
            continue

        candidate_data = extract_structured_html_fallback(candidate_html, candidate_url)
        if candidate_data and is_usable_job_content(candidate_data):
            return candidate_data, candidate_url

        try:
            page = fetch_page(candidate_url, use_stealth=needs_stealth(candidate_url))
            candidate_data = extract_job_content(page)
        except Exception:
            candidate_data = None
        if candidate_data and is_usable_job_content(candidate_data):
            return candidate_data, candidate_url

    return None


def _should_search_same_site_job_content(
    *,
    original_url: str,
    resolved_url: str,
    resolved_html_doc: str = "",
) -> bool:
    """Decide whether same-site JD discovery is worth attempting.

    This keeps the broad Ashby/wrapper recovery path intact while also covering
    canonicalized URLs whose HTML still exposes a same-site job iframe/link.
    """
    if looks_like_ashby_url(resolved_url) or original_url != resolved_url:
        return True

    if not resolved_html_doc:
        return False

    job_id = ashby_job_id_from_url(original_url) or ashby_job_id_from_url(resolved_url)
    return bool(_extract_candidate_urls_from_html(resolved_html_doc, resolved_url, job_id=job_id))


def format_output(data: dict, raw: bool = False) -> str:
    """Format extracted data as markdown."""
    parts = []

    if data["title"]:
        parts.append(f"# {data['title']}")
    if data["company"]:
        parts.append(f"**Company:** {data['company']}")
    if data["location"]:
        parts.append(f"**Location:** {data['location']}")

    if parts:
        parts.append("")
        parts.append("---")
        parts.append("")

    text = data["full_text"] if raw else clean_text(data["full_text"])
    parts.append(text)
    return "\n".join(parts)


def scrape_job(url: str, use_stealth: bool = False) -> tuple[dict, str]:
    """Extract a job description using layered strategies."""
    original_url = url
    resolved_url = resolve_job_source_url(url)
    if resolved_url != url:
        print(
            f"[scrape_job] Resolved job source URL to {resolved_url}",
            file=sys.stderr,
        )
    url = resolved_url

    if looks_like_dover_url(url):
        try:
            dover_data = fetch_dover_job_data(url)
        except Exception as exc:
            print(f"[scrape_job] Dover API extraction failed: {exc}", file=sys.stderr)
            if _is_terminal_scrape_error(exc):
                raise RuntimeError(str(exc)) from exc
            dover_data = None
        if dover_data and is_usable_job_content(dover_data):
            print("[scrape_job] Using Dover application API.", file=sys.stderr)
            return dover_data, "dover-api"

    if looks_like_workday_url(url):
        try:
            workday_data = fetch_workday_job_data(url)
        except Exception as exc:
            print(f"[scrape_job] Workday CXS API extraction failed: {exc}", file=sys.stderr)
            if _is_terminal_scrape_error(exc):
                raise RuntimeError(str(exc)) from exc
            workday_data = None
        if workday_data and is_usable_job_content(workday_data):
            print("[scrape_job] Using Workday CXS API.", file=sys.stderr)
            return workday_data, "workday-api"

    if looks_like_greenhouse_url(url):
        try:
            greenhouse_data = fetch_greenhouse_job_data(url)
        except Exception as exc:
            print(f"[scrape_job] Greenhouse API extraction failed: {exc}", file=sys.stderr)
            if _is_terminal_scrape_error(exc):
                raise RuntimeError(str(exc)) from exc
            greenhouse_data = None
        if greenhouse_data and is_usable_job_content(greenhouse_data):
            print("[scrape_job] Using Greenhouse API.", file=sys.stderr)
            return greenhouse_data, "greenhouse-api"

    if looks_like_icims_url(url):
        try:
            icims_data = fetch_icims_job_data(url)
        except Exception as exc:
            print(f"[scrape_job] iCIMS iframe fallback failed: {exc}", file=sys.stderr)
            if _is_terminal_scrape_error(exc):
                raise RuntimeError(str(exc)) from exc
            icims_data = None
        if icims_data and is_usable_job_content(icims_data):
            print("[scrape_job] Using iCIMS iframe fallback.", file=sys.stderr)
            return icims_data, "icims-html"

    scrapling_error = None

    try:
        page = fetch_page(url, use_stealth=use_stealth)
        data = extract_job_content(page)
        unavailable_reason = _job_unavailable_reason_from_extracted_data(url, data)
        if unavailable_reason:
            raise RuntimeError(unavailable_reason)
        if is_usable_job_content(data):
            return data, "scrapling"
        print(
            "[scrape_job] Scrapling fetched the page but the extracted text looked incomplete; "
            "trying structured HTML fallback...",
            file=sys.stderr,
        )
    except Exception as exc:
        scrapling_error = exc
        print(f"[scrape_job] Scrapling path failed: {exc}", file=sys.stderr)
        if _is_terminal_scrape_error(exc):
            raise RuntimeError(str(exc)) from exc

    html_error = None
    html_doc = ""
    try:
        html_doc = fetch_raw_html(url)
    except Exception as exc:
        html_error = exc
        print(f"[scrape_job] Raw HTML fallback fetch failed: {exc}", file=sys.stderr)
        if _is_terminal_scrape_error(exc):
            raise RuntimeError(str(exc)) from exc

    if html_doc:
        data = extract_structured_html_fallback(html_doc, url)
        if data and is_usable_job_content(data):
            print("[scrape_job] Using structured HTML fallback.", file=sys.stderr)
            return data, "structured-html"
        blocked_reason = _manual_challenge_reason_from_text(html_doc, url)
        if blocked_reason:
            raise RuntimeError(blocked_reason)
        unavailable_reason = _job_unavailable_reason_from_html(html_doc, url)
        if unavailable_reason:
            raise RuntimeError(unavailable_reason)

    if _should_search_same_site_job_content(
        original_url=original_url,
        resolved_url=url,
        resolved_html_doc=html_doc,
    ):
        same_site_result = _search_same_site_job_content(
            original_url=original_url,
            resolved_url=url,
            resolved_html_doc=html_doc,
        )
        if same_site_result is not None:
            data, candidate_url = same_site_result
            print(
                f"[scrape_job] Using same-site JD discovery fallback from {candidate_url}.",
                file=sys.stderr,
            )
            return data, "same-site-search"

    if _should_try_rendered_browser(url, html_doc):
        try:
            rendered_data = extract_rendered_browser_fallback(url)
        except Exception as exc:
            print(f"[scrape_job] Rendered browser fallback failed: {exc}", file=sys.stderr)
            if _is_terminal_scrape_error(exc):
                raise RuntimeError(str(exc)) from exc
            rendered_data = None
        if rendered_data and is_usable_job_content(rendered_data):
            print("[scrape_job] Using rendered browser fallback.", file=sys.stderr)
            return rendered_data, "rendered-browser"

    try:
        cloudflare_data = extract_cloudflare_crawl_fallback(url)
    except Exception as exc:
        print(f"[scrape_job] Cloudflare Browser Rendering fallback failed: {exc}", file=sys.stderr)
        cloudflare_data = None

    if cloudflare_data and is_usable_job_content(cloudflare_data):
        print("[scrape_job] Using Cloudflare Browser Rendering crawl fallback.", file=sys.stderr)
        return cloudflare_data, "cloudflare-crawl"

    for exc in (scrapling_error, html_error):
        if _is_terminal_scrape_error(exc):
            raise RuntimeError(str(exc))

    if scrapling_error is not None:
        raise RuntimeError(
            "Could not extract a usable JD from the website via scrapling, structured HTML fallback, or Cloudflare crawl fallback."
        ) from scrapling_error

    if html_error is not None:
        raise RuntimeError(
            "Could not extract a usable JD from the website via raw HTML fallback or Cloudflare crawl fallback."
        ) from html_error

    raise RuntimeError("Could not extract a usable JD from the website.")


def main():
    parser = argparse.ArgumentParser(description="Scrape a job posting URL")
    parser.add_argument("url", help="URL of the job posting")
    parser.add_argument("--output", "-o", help="Output file path")
    parser.add_argument(
        "--stealth",
        "-s",
        action="store_true",
        help="Use StealthyFetcher for anti-bot bypass",
    )
    parser.add_argument(
        "--raw",
        "-r",
        action="store_true",
        help="Output raw text without cleaning",
    )
    args = parser.parse_args()

    use_stealth = args.stealth or needs_stealth(args.url)

    try:
        data, source = scrape_job(args.url, use_stealth=use_stealth)
    except Exception as exc:
        print(f"[scrape_job] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    output = format_output(data, raw=args.raw)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"[scrape_job] Written to {args.output} via {source}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
