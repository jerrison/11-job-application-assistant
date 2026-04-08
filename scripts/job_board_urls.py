#!/usr/bin/env python3
"""Shared job-board URL detection and wrapper-resolution helpers."""

from __future__ import annotations

import html
import json
import re
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from browser_runtime import launch_chromium_browser

GREENHOUSE_HOST_PATTERNS = ("greenhouse.io",)
GEM_HOST_PATTERNS = ("jobs.gem.com",)
ASHBY_HOST_PATTERNS = ("ashbyhq.com",)
LEVER_HOST_PATTERNS = ("lever.co",)
WORKDAY_HOST_PATTERNS = ("myworkdayjobs.com", "myworkdaysite.com")
DOVER_HOST_PATTERNS = ("app.dover.com", "dover.com")
ICIMS_HOST_PATTERNS = ("icims.com",)
ORACLE_HCM_HOST_PATTERNS = ("oraclecloud.com",)
PHENOM_HOST_PATTERNS = ("phenom.com",)
EIGHTFOLD_HOST_PATTERNS = ("eightfold.ai",)
BAMBOOHR_HOST_PATTERNS = ("bamboohr.com",)
AVATURE_HOST_PATTERNS = ("avature.net",)
SUCCESSFACTORS_HOST_PATTERNS = ("successfactors.com",)
BREEZY_HOST_PATTERNS = ("breezy.hr",)
RECRUITEE_HOST_PATTERNS = ("recruitee.com",)
JOBVITE_HOST_PATTERNS = ("jobvite.com",)
JAZZHR_HOST_PATTERNS = ("applytojob.com", "jazzhr.com")
PAYCOR_HOST_PATTERNS = ("recruitingbypaycor.com", "paycor.com")
BYTEDANCE_HOST_PATTERNS = ("jobs.bytedance.com", "joinbytedance.com")
JOB_URL_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0 Safari/537.36"
)
NON_HTML_ASSET_EXTENSIONS = frozenset(
    {
        ".avif",
        ".bmp",
        ".css",
        ".gif",
        ".ico",
        ".jpeg",
        ".jpg",
        ".js",
        ".json",
        ".pdf",
        ".png",
        ".svg",
        ".txt",
        ".webp",
        ".xml",
    }
)
UNRESOLVED_URL_TEMPLATE_RE = re.compile(r"(\{\{[^{}]+\}\}|\$\{[^{}]+\}|<%=?[^%]+%>)")
GENERIC_HOST_LABELS = {"www", "jobs", "careers", "apply", "job", "join", "team", "app"}
ASHBY_NON_JOB_SEGMENTS = {"application", "embed"}
ASHBY_APP_DATA_TOKEN = "window.__appData ="
GREENHOUSE_BOARD_HOSTS = (
    "boards.greenhouse.io",
    "boards.eu.greenhouse.io",
    "job-boards.greenhouse.io",
    "job-boards.eu.greenhouse.io",
    "boards-api.greenhouse.io",
    "boards-api.eu.greenhouse.io",
)
GREENHOUSE_GENERIC_SUBDOMAINS = frozenset(
    {
        "apply",
        "boards",
        "careers",
        "employment",
        "hire",
        "job-boards",
        "jobs",
        "join",
        "opportunities",
        "recruiting",
        "talent",
        "work",
        "www",
    }
)
GREENHOUSE_TLD_PARTS = frozenset({"ai", "app", "co", "com", "dev", "io", "net", "org", "uk", "us"})
GREENHOUSE_SLUG_SUFFIXES = ("hq", "inc", "labs", "tech")
GREENHOUSE_CANDIDATE_NOISE_TOKENS = frozenset(
    {
        "badges",
        "careers",
        "cdn",
        "cloudinary",
        "css",
        "footer",
        "html",
        "http",
        "https",
        "image",
        "images",
        "job",
        "jobs",
        "js",
        "json",
        "next",
        "openings",
        "png",
        "res",
        "site",
        "static",
        "svg",
        "upload",
        "video",
        "webp",
        "www",
    }
)


def looks_like_non_html_asset_url(url: str) -> bool:
    """Return whether *url* points at a static asset instead of a job page."""
    path = urlparse(url).path.casefold().rstrip("/")
    if not path:
        return False
    return any(path.endswith(extension) for extension in NON_HTML_ASSET_EXTENSIONS)


def looks_like_unresolved_url_template(url: str) -> bool:
    """Return whether *url* still contains an unresolved template placeholder."""
    decoded = html.unescape(unquote(str(url or "")))
    return bool(UNRESOLVED_URL_TEMPLATE_RE.search(decoded))
_WORKDAY_LOCALE_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$", re.I)
_AVATURE_BRANDED_PATH_RE = re.compile(
    r"/(?:[a-z]{2}_[a-z]{2}/)?careers/(?:jobdetail|applicationmethods|registrationmethods|register|login)\b",
    re.I,
)
_WORKDAY_RESERVED_SCOPE_SEGMENTS = frozenset(
    {
        "apply",
        "applymanually",
        "candidate-home",
        "candidatehome",
        "job",
        "jobs",
        "login",
        "logout",
        "passwordreset",
        "userhome",
    }
)


def _canonicalize_url_path(url: str) -> str:
    parsed = urlparse(url)
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    return urlunparse(parsed._replace(path=path))


def greenhouse_job_id_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    query = parsed.query.replace("?", "&")
    params = parse_qs(query)
    job_id = (params.get("gh_jid") or [None])[0]
    if job_id:
        return str(job_id).strip()

    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    for i, part in enumerate(parts):
        if part == "jobs" and i + 1 < len(parts):
            return parts[i + 1]

    for part in parts:
        if part.isdigit() and len(part) >= 6:
            return part

    return None


def _greenhouse_slug_from_direct_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if not any(board_host == host for board_host in GREENHOUSE_BOARD_HOSTS):
        return None

    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    if host == "boards-api.greenhouse.io" and len(parts) >= 5 and parts[0] == "v1" and parts[1] == "boards":
        return parts[2] or None

    for i, part in enumerate(parts):
        if part == "jobs" and i >= 1:
            for j in range(i - 1, -1, -1):
                if parts[j] not in {"v1", "boards"}:
                    return parts[j] or None

    query = parse_qs(parsed.query.replace("?", "&"))
    board = (query.get("for") or [None])[0]
    if board:
        return str(board).strip() or None
    return None


def _canonical_greenhouse_direct_job_url(url: str) -> str | None:
    normalized_url = _canonicalize_url_path(url)
    slug = _greenhouse_slug_from_direct_url(normalized_url)
    job_id = greenhouse_job_id_from_url(normalized_url)
    if not slug or not job_id:
        return None
    return f"https://job-boards.greenhouse.io/{slug}/jobs/{job_id}"


def greenhouse_board_slug_from_url(url: str) -> str | None:
    return _greenhouse_slug_from_direct_url(url)


def _extract_greenhouse_slug_from_text(body: str) -> str | None:
    normalized = html.unescape(str(body or "")).replace("\\/", "/")
    patterns = (
        r"(?:[?&])job_board=([a-zA-Z0-9_-]+)(?:[&#\"'<>]|$)",
        r"boards-api\.greenhouse\.io/v1/boards/([a-zA-Z0-9_-]+)/jobs(?:[/?&]|\\b)",
        r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/embed/job_(?:board|app)(?:/js)?\?for=([a-zA-Z0-9_-]+)",
        r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/([a-zA-Z0-9_-]+)/jobs(?:[/?&]|\\b)",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.I)
        if match:
            return match.group(1)
    return None


def _extract_greenhouse_direct_job_url_from_text(body: str) -> str | None:
    normalized = html.unescape(str(body or "")).replace("\\/", "/")
    pattern = r"https://(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/[A-Za-z0-9_-]+/jobs/\d+(?:#[^\s\"'<>]+)?"
    for match in re.finditer(pattern, normalized, flags=re.I):
        candidate = _canonical_greenhouse_direct_job_url(match.group(0))
        if candidate:
            return candidate
    return None


def _greenhouse_slug_from_aux_url(url: str) -> str | None:
    parsed = urlparse(url)
    params = parse_qs(parsed.query.replace("?", "&"))
    for key in ("job_board", "board", "for"):
        value = (params.get(key) or [None])[0]
        if value:
            return str(value).strip() or None
    return None


def _fetch_url_text(url: str, *, opener=urlopen) -> tuple[str, str]:
    req = Request(_canonicalize_url_path(url), headers={"User-Agent": JOB_URL_USER_AGENT})
    with opener(req, timeout=30) as resp:
        final_url = getattr(resp, "url", url)
        body = resp.read().decode("utf-8", errors="ignore")
    return str(final_url), body


def _greenhouse_script_urls(html_doc: str, base_url: str) -> list[str]:
    greenhouse_urls: list[str] = []
    first_party_urls: list[str] = []
    base_host = (urlparse(base_url).hostname or "").casefold()
    for match in re.finditer(r'<script[^>]+src=["\']([^"\']+)["\']', html_doc, flags=re.I):
        raw_src = match.group(1).strip()
        if not raw_src:
            continue
        candidate = urljoin(base_url, raw_src)
        lowered = candidate.casefold()
        host = (urlparse(candidate).hostname or "").casefold()
        if "greenhouse" in lowered:
            if candidate not in greenhouse_urls:
                greenhouse_urls.append(candidate)
            continue
        if host == base_host and (
            "/_next/static/chunks/pages/" in lowered
            or "/careers" in lowered
            or "/jobs" in lowered
            or "/openings" in lowered
        ):
            if candidate not in first_party_urls:
                first_party_urls.append(candidate)
    return greenhouse_urls + first_party_urls


def _discover_greenhouse_board_slug(url: str, *, opener=urlopen) -> str | None:
    try:
        final_url, body = _fetch_url_text(url, opener=opener)
    except Exception:
        return None

    direct_slug = _greenhouse_slug_from_direct_url(final_url)
    if direct_slug:
        return direct_slug

    body_slug = _extract_greenhouse_slug_from_text(body)
    if body_slug:
        return body_slug

    for script_url in _greenhouse_script_urls(body, final_url)[:4]:
        try:
            _, script_body = _fetch_url_text(script_url, opener=opener)
        except Exception:
            continue
        script_slug = _extract_greenhouse_slug_from_text(script_body)
        if script_slug:
            return script_slug

    return None


def _discover_greenhouse_direct_job_url(url: str, *, opener=urlopen) -> str | None:
    try:
        final_url, body = _fetch_url_text(url, opener=opener)
    except Exception:
        return None

    direct_url = _canonical_greenhouse_direct_job_url(final_url)
    if direct_url:
        return direct_url

    direct_url = _extract_greenhouse_direct_job_url_from_text(body)
    if direct_url:
        return direct_url

    for script_url in _greenhouse_script_urls(body, final_url)[:4]:
        try:
            _, script_body = _fetch_url_text(script_url, opener=opener)
        except Exception:
            continue
        direct_url = _extract_greenhouse_direct_job_url_from_text(script_body)
        if direct_url:
            return direct_url

    return None


def _greenhouse_slug_candidates(url: str, *, company_hint: str | None = None) -> list[str]:
    host = (urlparse(url).hostname or "").removeprefix("www.").casefold()
    host_parts = [part for part in host.split(".") if part]
    candidates: list[str] = []

    if company_hint:
        normalized = str(company_hint).strip().casefold()
        if normalized:
            candidates.append(normalized)

    for part in host_parts:
        if part in GREENHOUSE_GENERIC_SUBDOMAINS or part in GREENHOUSE_TLD_PARTS:
            continue
        if part not in candidates:
            candidates.append(part)

    if host_parts:
        subdomain = host_parts[0]
        if subdomain not in GREENHOUSE_TLD_PARTS and subdomain not in candidates:
            candidates.append(subdomain)

    stripped: list[str] = []
    for candidate in candidates:
        for suffix in GREENHOUSE_SLUG_SUFFIXES:
            if candidate.endswith(suffix) and len(candidate) > len(suffix) + 2:
                base = candidate[: -len(suffix)]
                if base not in candidates and base not in stripped:
                    stripped.append(base)
    candidates.extend(stripped)
    return candidates


def _greenhouse_wrapper_base_urls(url: str) -> list[str]:
    parsed = urlparse(_canonicalize_url_path(url))
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {"gh_jid", "gh_src"}
    ]
    candidates: list[str] = []

    query_variants = [""]
    sanitized_query = urlencode(query_pairs, doseq=True)
    if sanitized_query:
        query_variants.append(sanitized_query)

    for query in query_variants:
        base_url = urlunparse(parsed._replace(query=query))
        if base_url != url and base_url not in candidates:
            candidates.append(base_url)

    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    if parts and parts[-1].isdigit():
        parent_path = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
        for query in query_variants:
            parent_url = urlunparse(parsed._replace(path=parent_path, query=query))
            if parent_url not in candidates:
                candidates.append(parent_url)

    return candidates


def _greenhouse_slug_candidates_from_text(body: str) -> list[str]:
    normalized = html.unescape(str(body or "")).replace("\\/", "/")
    counts: Counter[str] = Counter()
    for candidate_url in re.findall(r"https?://[^\s\"'<>]+", normalized, flags=re.I):
        for token in re.split(r"[^a-z0-9_-]+", candidate_url.casefold()):
            if (
                len(token) < 4
                or token.isdigit()
                or token in GREENHOUSE_GENERIC_SUBDOMAINS
                or token in GREENHOUSE_TLD_PARTS
                or token in GREENHOUSE_CANDIDATE_NOISE_TOKENS
            ):
                continue
            counts[token] += 1
    return [token for token, _count in counts.most_common(20)]


def _recover_greenhouse_slug_from_wrapper_base(url: str, job_id: str, *, opener=urlopen) -> str | None:
    for base_url in _greenhouse_wrapper_base_urls(url):
        direct_url = _discover_greenhouse_direct_job_url(base_url, opener=opener)
        if direct_url:
            direct_slug = _greenhouse_slug_from_direct_url(direct_url)
            if direct_slug:
                return direct_slug

        discovered_slug = _discover_greenhouse_board_slug(base_url, opener=opener)
        if discovered_slug:
            return discovered_slug

        try:
            _final_url, body = _fetch_url_text(base_url, opener=opener)
        except Exception:
            continue

        candidates = _greenhouse_slug_candidates_from_text(body)
        if not candidates:
            continue
        discovered_slug = _probe_greenhouse_board_slug(job_id, candidates, opener=opener)
        if discovered_slug:
            return discovered_slug
    return None


def _probe_greenhouse_board_slug(job_id: str, candidates: list[str], *, opener=urlopen) -> str | None:
    for slug in candidates:
        api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"
        req = Request(api_url, method="HEAD", headers={"User-Agent": JOB_URL_USER_AGENT})
        try:
            with opener(req, timeout=10) as resp:
                status = getattr(resp, "status", 200)
            if status == 200:
                return slug
        except Exception:
            continue
    return None


def canonical_greenhouse_job_url(
    url: str,
    *,
    opener=urlopen,
    embed_url_resolver: Callable[[str], str | None] | None = None,
) -> str:
    if not looks_like_greenhouse_url(url):
        return url

    normalized_url = _canonicalize_url_path(url)
    direct_url = _canonical_greenhouse_direct_job_url(normalized_url)
    if direct_url:
        return direct_url
    parsed = urlparse(normalized_url)
    query = parse_qs(parsed.query.replace("?", "&"))
    job_id = greenhouse_job_id_from_url(normalized_url)
    slug = _greenhouse_slug_from_direct_url(normalized_url)

    if not slug:
        board_hint = (query.get("board") or [None])[0]
        if board_hint:
            slug = str(board_hint).strip() or None

    if not slug:
        direct_url = _discover_greenhouse_direct_job_url(normalized_url, opener=opener)
        if direct_url:
            return direct_url
        slug = _discover_greenhouse_board_slug(normalized_url, opener=opener)

    if not slug and job_id:
        slug = _probe_greenhouse_board_slug(
            job_id,
            _greenhouse_slug_candidates(normalized_url),
            opener=opener,
        )

    if not slug and job_id:
        slug = _recover_greenhouse_slug_from_wrapper_base(normalized_url, job_id, opener=opener)

    if not slug and job_id:
        browser_url_resolver = embed_url_resolver or _resolve_greenhouse_embed_url_in_browser
        try:
            candidate_url = browser_url_resolver(normalized_url)
        except Exception:
            candidate_url = None
        if candidate_url:
            direct_url = _canonical_greenhouse_direct_job_url(candidate_url)
            if direct_url:
                return direct_url
            slug = (
                _greenhouse_slug_from_direct_url(candidate_url)
                or _greenhouse_slug_from_aux_url(candidate_url)
                or _extract_greenhouse_slug_from_text(candidate_url)
            )

    if slug and job_id:
        return f"https://job-boards.greenhouse.io/{slug}/jobs/{job_id}"

    return normalized_url


def looks_like_greenhouse_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if any(pattern in host for pattern in GREENHOUSE_HOST_PATTERNS):
        return True
    params = parse_qs(parsed.query)
    return "gh_jid" in params or "gh_src" in params


def looks_like_dover_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    return (
        any(pattern in host for pattern in DOVER_HOST_PATTERNS) and len(parts) >= 2 and parts[0].casefold() == "apply"
    )


def canonical_dover_job_url(url: str) -> str:
    if not looks_like_dover_url(url):
        return url
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return _canonicalize_url_path(urlunparse(parsed._replace(path=path)))


def dover_job_or_search_id_from_url(url: str) -> str | None:
    if not looks_like_dover_url(url):
        return None
    parts = [part.strip() for part in urlparse(url).path.split("/") if part.strip()]
    if len(parts) >= 3 and parts[2]:
        return parts[2]
    if len(parts) >= 2 and parts[1]:
        return parts[1]
    return None


def dover_company_slug_from_url(url: str) -> str | None:
    if not looks_like_dover_url(url):
        return None
    parts = [part.strip() for part in urlparse(url).path.split("/") if part.strip()]
    if len(parts) >= 2 and parts[1]:
        company_slug = re.sub(r"[^a-z0-9-]+", "", parts[1].casefold())
        return company_slug or None
    return None


def looks_like_lever_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return any(pattern in host for pattern in LEVER_HOST_PATTERNS)


def canonical_lever_job_url(url: str) -> str:
    """Strip ``/apply`` suffix and query params so the scraper fetches the JD page."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if not any(pattern in host for pattern in LEVER_HOST_PATTERNS):
        return url
    path = parsed.path.rstrip("/")
    if path.endswith("/apply"):
        path = path[: -len("/apply")]
        path = path or "/"
    return _canonicalize_url_path(urlunparse(parsed._replace(path=path, query="")))


def looks_like_workday_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return any(pattern in host for pattern in WORKDAY_HOST_PATTERNS)


def looks_like_avature_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path or ""
    if any(pattern in host for pattern in AVATURE_HOST_PATTERNS):
        return True
    return bool(_AVATURE_BRANDED_PATH_RE.search(path))


def canonical_workday_job_url(url: str) -> str:
    """Strip tracking query params (e.g. ``?source=LinkedIn``) from Workday URLs."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if not any(pattern in host for pattern in WORKDAY_HOST_PATTERNS):
        return url
    return _canonicalize_url_path(urlunparse(parsed._replace(query="")))


def _normalize_workday_scope_token(value: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "-", value.strip().casefold()).strip("-")
    return token


def _workday_candidate_paths(parsed) -> list[str]:
    paths = [parsed.path]
    params = parse_qs(parsed.query or "")
    for key in ("redirect", "redirecturl"):
        for value in params.get(key, []):
            if not value:
                continue
            redirect_path = urlparse(value).path if "://" in value else value
            if redirect_path:
                paths.append(redirect_path)
    return paths


def workday_auth_scope(url: str) -> str | None:
    """Return a stable tenant/site key for Workday auth guard accounting."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if not any(pattern in host for pattern in WORKDAY_HOST_PATTERNS):
        return None

    if "myworkdayjobs.com" in host:
        host_match = re.match(r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs", host)
        tenant = _normalize_workday_scope_token(host_match.group(1)) if host_match else ""
        site = ""
        for candidate_path in _workday_candidate_paths(parsed):
            parts = [part for part in candidate_path.strip("/").split("/") if part]
            for part in parts:
                if _WORKDAY_LOCALE_RE.fullmatch(part):
                    continue
                candidate = _normalize_workday_scope_token(part)
                if candidate and candidate not in _WORKDAY_RESERVED_SCOPE_SEGMENTS:
                    site = candidate
                    break
            if site:
                break
        if tenant and site:
            return f"workday:{tenant}/{site}"
        if tenant:
            return f"workday:{tenant}"
        return None

    if "myworkdaysite.com" in host:
        for candidate_path in _workday_candidate_paths(parsed):
            parts = [part for part in candidate_path.strip("/").split("/") if part]
            recruiting_idx = next((i for i, part in enumerate(parts) if part.casefold() == "recruiting"), -1)
            if recruiting_idx >= 0 and recruiting_idx + 2 < len(parts):
                tenant = _normalize_workday_scope_token(parts[recruiting_idx + 1])
                site = _normalize_workday_scope_token(parts[recruiting_idx + 2])
                if tenant and site:
                    return f"workday:{tenant}/{site}"
        return None

    return None


def icims_auth_scope(url: str) -> str | None:
    """Return a stable tenant key for iCIMS auth guard accounting.

    Some branded iCIMS tenants stay on wrapper hosts like ``www.amazon.jobs``
    or ``careers.docusign.com`` until the browser discovers the embedded
    application URL, so scope by host whenever the board context is already
    known to be iCIMS.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold().strip(".")
    if not host:
        return None
    return f"icims:{host}"


def looks_like_icims_url(url: str) -> bool:
    """Detect iCIMS ATS URLs.

    iCIMS application URLs contain ``icims.com`` in the hostname, e.g.:
      - ``https://uscareers-docusign.icims.com/jobs/28629/login``
      - ``https://careers-docusign.icims.com/jobs/28629/job``

    The subdomain pattern is typically ``{prefix}-{company}.icims.com``
    or ``{company}.icims.com``.
    """
    host = (urlparse(url).hostname or "").casefold()
    return any(pattern in host for pattern in ICIMS_HOST_PATTERNS)


def looks_like_oracle_hcm_url(url: str) -> bool:
    """Detect Oracle Cloud HCM (Fusion) candidate experience URLs.

    Oracle HCM URLs use patterns like:
      - ``https://fa-ewgu-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/job/28981/``
      - ``https://*.oraclecloud.com/hcmUI/CandidateExperience/...``

    The key identifiers are the ``oraclecloud.com`` host combined with
    ``/hcmUI/CandidateExperience/`` in the path.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    return any(pattern in host for pattern in ORACLE_HCM_HOST_PATTERNS) and "/hcmui/candidateexperience/" in path


def canonical_icims_job_url(url: str) -> str:
    """Normalize an iCIMS URL to its canonical JD form.

    Strips ``/login``, ``/apply``, and ``/job`` suffixes and tracking query
    params so the scraper fetches the JD page at ``/jobs/{id}``.
    """
    if not looks_like_icims_url(url):
        return url
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    # Strip trailing /login, /job, /apply segments
    path = re.sub(r"/(login|job|apply)$", "", path)
    return _canonicalize_url_path(urlunparse(parsed._replace(path=path, query="")))


def looks_like_ashby_wrapper_url(url: str) -> bool:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    return "ashby_jid" in params


def looks_like_ashby_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return any(pattern in host for pattern in ASHBY_HOST_PATTERNS) or looks_like_ashby_wrapper_url(url)


def canonical_ashby_job_url(url: str) -> str:
    if looks_like_ashby_wrapper_url(url):
        return url
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if not any(pattern in host for pattern in ASHBY_HOST_PATTERNS):
        return url
    path = parsed.path.rstrip("/")
    if path.endswith("/application"):
        path = path[: -len("/application")]
        path = path or "/"
        return _canonicalize_url_path(urlunparse(parsed._replace(path=path)))
    return _canonicalize_url_path(url)


def ashby_job_id_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    job_id = (params.get("ashby_jid") or [None])[0]
    if job_id:
        return str(job_id).strip()

    host = (parsed.hostname or "").casefold()
    if not any(pattern in host for pattern in ASHBY_HOST_PATTERNS):
        return None

    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    if len(parts) >= 2 and parts[1] not in ASHBY_NON_JOB_SEGMENTS:
        return parts[1]
    if len(parts) >= 3 and parts[2] not in ASHBY_NON_JOB_SEGMENTS:
        return parts[2]
    return None


def ashby_company_slug_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if not any(pattern in host for pattern in ASHBY_HOST_PATTERNS):
        return None
    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    if not parts:
        return None
    company_slug = re.sub(r"[^a-z0-9-]+", "", parts[0].casefold())
    return company_slug or None


def looks_like_phenom_url(url: str) -> bool:
    """Detect Phenom ATS URLs.

    Phenom career pages use patterns like:
      - ``https://careers.{company}.com/us/en/job/{id}/{slug}``
      - ``https://careers.{company}.com/global/en/job/{id}/{slug}``
      - ``https://careers.{company}.com/us/en/apply?jobSeqNo={id}``

    The key identifier is the ``jobSeqNo`` query parameter or the
    ``/{region}/en/job/`` path pattern typical of Phenom Apply Studio.
    The region segment is typically a 2-char country code (``us``, ``uk``)
    but can also be ``global`` or other longer strings.
    Phenom hosts include ``phenom.com`` subdomains but many companies
    host on their own domains (e.g. ``careers.hpe.com``).
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    params = parse_qs(parsed.query)

    # Direct phenom.com host
    if any(pattern in host for pattern in PHENOM_HOST_PATTERNS):
        return True

    # jobSeqNo query parameter is Phenom-specific
    if "jobseqno" in params:
        return True

    # utm_medium=phenom-feeds is a strong signal from LinkedIn referrals
    utm_medium = params.get("utm_medium", [""])[0].casefold()
    if "phenom" in utm_medium:
        return True

    # Path pattern: /us/en/job/ or /global/en/job/ with job ID
    # Region segment can be 2-char country code or longer (e.g. "global")
    if re.search(r"/[a-z]{2,}/[a-z]{2}/job/[A-Za-z0-9]+", path):
        return True

    # Path pattern: /us/en/apply or /global/en/apply (Phenom apply page)
    return bool(re.search(r"/[a-z]{2,}/[a-z]{2}/apply\b", path))


def canonical_phenom_job_url(url: str) -> str:
    """Normalize a Phenom URL to its canonical JD form.

    Strips the ``/apply`` suffix and tracking query params, keeping the
    ``/job/{id}/{slug}`` path for scraping.
    """
    if not looks_like_phenom_url(url):
        return url
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")

    # If this is an apply URL with jobSeqNo, convert to job URL
    # (We can't reconstruct the slug, but keep the ID for reference)
    if "/apply" in path:
        params = parse_qs(parsed.query)
        seq_no = (params.get("jobSeqNo") or params.get("jobseqno") or [""])[0]
        if seq_no:
            # Strip /apply and append /job/{id}
            base = re.sub(r"/apply$", "", path)
            path = f"{base}/job/{seq_no}"
            return _canonicalize_url_path(urlunparse(parsed._replace(path=path, query="")))

    # Strip tracking query params
    return _canonicalize_url_path(urlunparse(parsed._replace(query="")))


def looks_like_eightfold_url(url: str) -> bool:
    """Detect Eightfold AI ATS URLs (e.g. paypal.eightfold.ai)."""
    host = (urlparse(url).hostname or "").casefold()
    return any(pattern in host for pattern in EIGHTFOLD_HOST_PATTERNS)


_EIGHTFOLD_KEEP_PARAMS = {"pid", "domain", "query"}


def canonical_eightfold_job_url(url: str) -> str:
    """Strip tracking/filter params, keep pid + domain + query."""
    if not looks_like_eightfold_url(url):
        return url
    parsed = urlparse(url)
    kept = [(k, v) for k, v in parse_qsl(parsed.query) if k in _EIGHTFOLD_KEEP_PARAMS]
    return urlunparse(parsed._replace(query=urlencode(kept)))


def looks_like_bamboohr_url(url: str) -> bool:
    """Detect BambooHR ATS URLs (e.g. coherent.bamboohr.com/careers/229)."""
    host = (urlparse(url).hostname or "").casefold()
    return any(pattern in host for pattern in BAMBOOHR_HOST_PATTERNS)


def canonical_bamboohr_job_url(url: str) -> str:
    """BambooHR URLs are already canonical — return as-is."""
    return _canonicalize_url_path(url)


def _first_path_segment(url: str) -> str:
    parsed = urlparse(url)
    for segment in parsed.path.split("/"):
        if segment:
            return segment.casefold()
    return ""


def looks_like_successfactors_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    if not any(pattern in host for pattern in SUCCESSFACTORS_HOST_PATTERNS):
        return False
    return _first_path_segment(url) in {"career", "careers"}


def canonical_successfactors_job_url(url: str) -> str:
    if not looks_like_successfactors_url(url):
        return url
    return _canonicalize_url_path(url)


def html_looks_like_successfactors(html: str) -> bool:
    lowered = html.casefold()
    markers = (
        "successfactors.com",
        "jobs2web",
        "j2w.apply",
        "j2w.init",
        "rmkcdn.successfactors.com",
        "performancemanager4.successfactors.com",
    )
    return any(marker in lowered for marker in markers)


def looks_like_breezy_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    if not any(pattern in host for pattern in BREEZY_HOST_PATTERNS):
        return False
    return _first_path_segment(url) == "p"


def canonical_breezy_job_url(url: str) -> str:
    if not looks_like_breezy_url(url):
        return url
    return _canonicalize_url_path(url)


def html_looks_like_breezy(html: str) -> bool:
    return "breezy.hr" in html.casefold()


def looks_like_recruitee_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    if not any(pattern in host for pattern in RECRUITEE_HOST_PATTERNS):
        return False
    return _first_path_segment(url) == "o"


def looks_like_recruitee_wrapper_url(url: str) -> bool:
    return _first_path_segment(url) == "o"


def html_looks_like_recruitee(html: str) -> bool:
    lowered = html.casefold()
    markers = (
        "cdn.recruitee.com/assets/app.js",
        "static.recruitee.com/assets/app.js",
        "cdn.recruitee.com/assets/vendor.js",
        "cdn.recruitee.com/assets/main.js",
    )
    return any(marker in lowered for marker in markers)


def looks_like_jobvite_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    if "jobvite.com" not in host:
        return False
    return "/job/" in urlparse(url).path.casefold()


def canonical_jobvite_job_url(url: str) -> str:
    if not looks_like_jobvite_url(url):
        return url

    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(parts):
        if part.casefold() == "job" and index + 1 < len(parts):
            path = "/" + "/".join(parts[: index + 2])
            return _canonicalize_url_path(urlunparse(parsed._replace(path=path, query="", fragment="")))
    return _canonicalize_url_path(urlunparse(parsed._replace(query="", fragment="")))


def looks_like_jazzhr_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    if "applytojob.com" not in host:
        return False
    return _first_path_segment(url) == "apply"


def looks_like_paycor_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    if "recruitingbypaycor.com" not in host:
        return False
    path = urlparse(url).path.casefold()
    return "/recruiting/jobs/" in path or "/career/jobintroduction.action" in path


def looks_like_bytedance_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if not any(host.endswith(pattern) for pattern in BYTEDANCE_HOST_PATTERNS):
        return False
    parts = [part for part in parsed.path.split("/") if part]
    offset = 0
    if parts and re.fullmatch(r"[a-z]{2}(?:_[a-z]{2})?", parts[0], flags=re.I):
        offset = 1
    remaining = parts[offset:]
    if len(remaining) >= 3 and remaining[0] == "position" and remaining[2] == "detail":
        return True
    if len(remaining) >= 2 and remaining[0] == "search":
        return True
    return len(remaining) >= 3 and remaining[0] == "resume" and remaining[2] == "apply"


def canonical_bytedance_job_url(url: str) -> str:
    if not looks_like_bytedance_url(url):
        return url
    parsed = urlparse(url)
    return _canonicalize_url_path(urlunparse(parsed._replace(query="", fragment="")))



def looks_like_linkedin_easy_apply_url(url: str) -> bool:
    """True if *url* is a LinkedIn job view page (potential Easy Apply)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return "linkedin.com" in host and "/jobs/view/" in parsed.path


def canonical_linkedin_job_url(url: str) -> str:
    """Normalize a LinkedIn job URL: strip query params, ensure trailing slash."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") + "/"
    return _canonicalize_url_path(urlunparse((parsed.scheme, parsed.netloc, path, "", "", "")))


def resolve_job_source_url(
    url: str,
    *,
    opener=urlopen,
    embed_url_resolver: Callable[[str], str | None] | None = None,
) -> str:
    if looks_like_linkedin_easy_apply_url(url):
        return canonical_linkedin_job_url(url)
    if looks_like_greenhouse_url(url):
        return canonical_greenhouse_job_url(url, opener=opener, embed_url_resolver=embed_url_resolver)
    if looks_like_lever_url(url):
        return canonical_lever_job_url(url)
    if looks_like_workday_url(url):
        return canonical_workday_job_url(url)
    if looks_like_avature_url(url):
        return _canonicalize_url_path(url)
    if looks_like_dover_url(url):
        return canonical_dover_job_url(url)
    if looks_like_icims_url(url):
        return canonical_icims_job_url(url)
    if looks_like_phenom_url(url):
        return canonical_phenom_job_url(url)
    if looks_like_eightfold_url(url):
        return canonical_eightfold_job_url(url)
    if looks_like_bamboohr_url(url):
        return canonical_bamboohr_job_url(url)
    if looks_like_bytedance_url(url):
        return canonical_bytedance_job_url(url)
    if looks_like_jobvite_url(url):
        return canonical_jobvite_job_url(url)
    if looks_like_ashby_wrapper_url(url):
        return resolve_ashby_wrapper_url(url, opener=opener, embed_url_resolver=embed_url_resolver)
    if looks_like_ashby_url(url):
        return canonical_ashby_job_url(url)
    return url


def resolve_ashby_wrapper_url(
    url: str,
    *,
    opener=urlopen,
    embed_url_resolver: Callable[[str], str | None] | None = None,
) -> str:
    if not looks_like_ashby_wrapper_url(url):
        return url

    job_id = ashby_job_id_from_url(url)
    if not job_id:
        raise ValueError(f"Ashby wrapper URL is missing ashby_jid: {url}")

    query_pairs = [
        (key, value) for key, value in parse_qsl(urlparse(url).query, keep_blank_values=True) if key != "ashby_jid"
    ]

    for company_slug in _candidate_company_slugs_from_host(url):
        candidate = _build_ashby_job_url(company_slug, job_id, query_pairs)
        if _is_valid_ashby_job_url(candidate, opener=opener):
            return candidate

    resolver = embed_url_resolver or _resolve_ashby_embed_url_in_browser
    embed_url = resolver(url)
    company_slug = ashby_company_slug_from_url(embed_url or "")
    if company_slug:
        candidate = _build_ashby_job_url(company_slug, job_id, query_pairs)
        if _is_valid_ashby_job_url(candidate, opener=opener):
            return candidate

    return _canonicalize_url_path(url)


def _candidate_company_slugs_from_host(url: str) -> list[str]:
    host = (urlparse(url).hostname or "").casefold().removeprefix("www.")
    labels = [label for label in host.split(".") if label and label not in GENERIC_HOST_LABELS]
    if not labels:
        return []
    candidates: list[str] = []
    first = re.sub(r"[^a-z0-9-]+", "", labels[0])
    if first:
        candidates.append(first)
    # Also try concatenating all labels (handles e.g. li.me -> lime)
    if len(labels) > 1:
        joined = re.sub(r"[^a-z0-9-]+", "", "".join(labels))
        if joined and joined != first:
            candidates.append(joined)
    return candidates


def _build_ashby_job_url(company_slug: str, job_id: str, query_pairs: list[tuple[str, str]]) -> str:
    query = urlencode(query_pairs, doseq=True)
    return urlunparse(
        (
            "https",
            "jobs.ashbyhq.com",
            f"/{company_slug}/{job_id}",
            "",
            query,
            "",
        )
    )


def _fetch_text(url: str, *, opener=urlopen) -> str:
    request = Request(url, headers={"User-Agent": JOB_URL_USER_AGENT})
    with opener(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def _extract_json_object_after(text: str, token: str) -> str | None:
    start = text.find(token)
    if start == -1:
        return None
    brace_start = text.find("{", start)
    if brace_start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for index in range(brace_start, len(text)):
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
                return text[brace_start : index + 1]
    return None


def _is_valid_ashby_job_url(url: str, *, opener=urlopen) -> bool:
    try:
        html = _fetch_text(url, opener=opener)
    except Exception:
        return False
    payload_raw = _extract_json_object_after(html, ASHBY_APP_DATA_TOKEN)
    if not payload_raw:
        return False
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        return False
    posting = payload.get("posting")
    if not isinstance(posting, dict):
        return False
    return bool(str(posting.get("title") or "").strip())


def _load_playwright_sync():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    return sync_playwright


def _greenhouse_browser_candidate_urls(page, *, extra_urls: list[str] | None = None) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def _append(value: str | None) -> None:
        candidate = str(value or "").strip()
        if not candidate:
            return
        lowered = candidate.casefold()
        if "greenhouse" not in lowered and "job_board=" not in lowered:
            return
        if candidate in seen:
            return
        seen.add(candidate)
        candidates.append(candidate)

    _append(page.url)
    for frame in page.frames:
        _append(getattr(frame, "url", ""))
    for candidate in extra_urls or []:
        _append(candidate)

    try:
        discovered = page.evaluate(
            """
            () => {
              const urls = [];
              const seen = new Set();
              const push = (value) => {
                const candidate = String(value || '').trim();
                if (!candidate) {
                  return;
                }
                const lowered = candidate.toLowerCase();
                if (!lowered.includes('greenhouse') && !lowered.includes('job_board=')) {
                  return;
                }
                if (seen.has(candidate)) {
                  return;
                }
                seen.add(candidate);
                urls.push(candidate);
              };
              for (const iframe of document.querySelectorAll('iframe[src]')) {
                push(iframe.src);
              }
              for (const link of document.querySelectorAll('a[href]')) {
                push(link.href);
              }
              return urls;
            }
            """
        )
    except Exception:
        discovered = []

    for candidate in discovered or []:
        _append(candidate)
    return candidates


def _resolve_greenhouse_embed_url_in_browser(url: str) -> str | None:
    sync_playwright = _load_playwright_sync()
    if sync_playwright is None:
        return None

    request_urls: list[str] = []

    def _record_request(request) -> None:
        request_urls.append(getattr(request, "url", ""))

    def _read_candidate_url(page) -> str | None:
        for candidate in _greenhouse_browser_candidate_urls(page, extra_urls=request_urls):
            if _canonical_greenhouse_direct_job_url(candidate):
                return candidate
            if (
                _greenhouse_slug_from_direct_url(candidate)
                or _greenhouse_slug_from_aux_url(candidate)
                or _extract_greenhouse_slug_from_text(candidate)
            ):
                return candidate
        return None

    with sync_playwright() as playwright:
        browser = launch_chromium_browser(
            playwright,
            headless=True,
            slow_mo=0,
            channel_env_var="SCRAPE_JOB_BROWSER_CHANNEL",
            executable_env_var="SCRAPE_JOB_BROWSER_EXECUTABLE",
            purpose="resolve company-hosted Greenhouse wrapper",
        )
        try:
            candidate_urls = [url, *_greenhouse_wrapper_base_urls(url)]
            seen_candidates: set[str] = set()
            for candidate_page_url in candidate_urls:
                if candidate_page_url in seen_candidates:
                    continue
                seen_candidates.add(candidate_page_url)
                page = browser.new_page(viewport={"width": 1440, "height": 1600})
                try:
                    request_urls.clear()
                    page.on("request", _record_request)
                    page.goto(candidate_page_url, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    try:
                        page.close()
                    except Exception:
                        pass
                    continue
                page.wait_for_timeout(1500)
                candidate_url = _read_candidate_url(page)
                if candidate_url:
                    try:
                        page.close()
                    except Exception:
                        pass
                    return candidate_url

                try:
                    page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    pass
                candidate_url = _read_candidate_url(page)
                if candidate_url:
                    try:
                        page.close()
                    except Exception:
                        pass
                    return candidate_url
                try:
                    page.close()
                except Exception:
                    pass
            return None
        finally:
            browser.close()


def _resolve_ashby_embed_url_in_browser(url: str) -> str | None:
    sync_playwright = _load_playwright_sync()
    if sync_playwright is None:
        return None

    with sync_playwright() as playwright:
        browser = launch_chromium_browser(
            playwright,
            headless=True,
            slow_mo=0,
            channel_env_var="SCRAPE_JOB_BROWSER_CHANNEL",
            executable_env_var="SCRAPE_JOB_BROWSER_EXECUTABLE",
            purpose="resolve company-hosted Ashby wrapper",
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1600})
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            if "jobs.ashbyhq.com" in page.url.casefold():
                return canonical_ashby_job_url(page.url)

            def _read_candidate_url() -> str | None:
                candidate = page.evaluate(
                    """
                    () => {
                      const iframe = Array.from(document.querySelectorAll('iframe[src*="jobs.ashbyhq.com"]'))[0];
                      if (iframe && iframe.src) {
                        return iframe.src;
                      }
                      const link = Array.from(document.querySelectorAll('a[href*="jobs.ashbyhq.com"]'))[0];
                      return link ? (link.href || '') : '';
                    }
                    """
                )
                return str(candidate).strip() or None

            candidate_url = _read_candidate_url()
            if candidate_url:
                return candidate_url

            try:
                page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            return _read_candidate_url()
        finally:
            browser.close()
