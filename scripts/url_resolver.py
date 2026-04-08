"""URL source detection and aggregator-to-board resolution.

Wraps existing board detection from job_board_urls.py.
Adds aggregator detection (LinkedIn, Indeed, Glassdoor) and
redirect-following resolution to find the underlying board URL.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

SOURCE_PATTERNS: dict[str, tuple[str, ...]] = {
    "linkedin": ("linkedin.com",),
    "indeed": ("indeed.com",),
    "glassdoor": ("glassdoor.com",),
    "ziprecruiter": ("ziprecruiter.com",),
    "dice": ("dice.com",),
    "easyapply": ("easyapply.jobs",),
    "trueup": ("trueup.io",),
    "theladders": ("theladders.com",),
    "vonq": ("vonq.io",),
    "jackandjill": ("jackandjill.ai",),
    "wellfound": ("wellfound.com",),
    "builtin": ("builtin.com",),
}
_LINKEDIN_ZOOM_HOSTS = ("linkedin.com", "www.linkedin.com")

_STATIC_APPLY_URL_PATTERNS = (
    re.compile(
        r"""<meta[^>]+name=["']search-job(?:-mobile)?-apply-url["'][^>]+content=["']([^"']+)["']""",
        re.IGNORECASE,
    ),
    re.compile(r"""\bdata-apply-url=["']([^"']+)["']""", re.IGNORECASE),
    re.compile(r"""["']applyUrl["']\s*:\s*["']([^"']+)["']""", re.IGNORECASE),
    re.compile(r"""\bhref=["']([^"']+)["']""", re.IGNORECASE),
)


def _is_known_board_url(url: str) -> bool:
    """Check if URL is a recognized job board using existing detectors."""
    try:
        from job_board_urls import (
            GEM_HOST_PATTERNS,
            looks_like_ashby_url,
            looks_like_bamboohr_url,
            looks_like_breezy_url,
            looks_like_bytedance_url,
            looks_like_dover_url,
            looks_like_eightfold_url,
            looks_like_greenhouse_url,
            looks_like_icims_url,
            looks_like_jazzhr_url,
            looks_like_jobvite_url,
            looks_like_lever_url,
            looks_like_linkedin_easy_apply_url,
            looks_like_paycor_url,
            looks_like_phenom_url,
            looks_like_recruitee_url,
            looks_like_successfactors_url,
            looks_like_workday_url,
        )

        host = (urlparse(url).hostname or "").lower()
        is_gem = any(p in host for p in GEM_HOST_PATTERNS)
        if looks_like_linkedin_easy_apply_url(url):
            return True
        return any(
            [
                looks_like_greenhouse_url(url),
                looks_like_dover_url(url),
                looks_like_lever_url(url),
                looks_like_workday_url(url),
                looks_like_icims_url(url),
                looks_like_ashby_url(url),
                looks_like_phenom_url(url),
                looks_like_eightfold_url(url),
                looks_like_bamboohr_url(url),
                looks_like_bytedance_url(url),
                looks_like_successfactors_url(url),
                looks_like_breezy_url(url),
                looks_like_recruitee_url(url),
                looks_like_jobvite_url(url),
                looks_like_jazzhr_url(url),
                looks_like_paycor_url(url),
                is_gem,
            ]
        )
    except ImportError:
        return False


def detect_source(url: str) -> str:
    """Classify a URL as an aggregator name, 'direct' (board URL), or 'unknown'."""
    host = (urlparse(url).hostname or "").lower()
    for source, patterns in SOURCE_PATTERNS.items():
        if any(p in host for p in patterns):
            return source
    if _is_known_board_url(url):
        return "direct"
    return "unknown"


def _looks_like_jobish_url(url: str) -> bool:
    path = urlparse(url).path.casefold()
    return any(
        token in path
        for token in (
            "/job",
            "/jobs",
            "/career",
            "/careers",
            "/apply",
        )
    )


def _canonical_board_url(url: str) -> str:
    try:
        from job_board_urls import resolve_job_source_url

        return resolve_job_source_url(url)
    except Exception:
        return url


def resolve_to_board_url(url: str) -> str | None:
    """Attempt to resolve an aggregator URL to the underlying board URL.

    Tries HTTP HEAD redirect first (fast), then falls back to browser-based
    resolution for sites like LinkedIn that require JavaScript rendering.
    Returns None if the board URL cannot be determined.
    """
    source = detect_source(url)
    if source == "direct":
        return url
    # Fast path: follow HTTP redirects via urllib
    try:
        from urllib.request import Request, urlopen

        req = Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=15) as resp:
            final_url = resp.url
            if _is_known_board_url(final_url):
                return _canonical_board_url(final_url)
            if final_url != url and detect_source(final_url) == "unknown" and _looks_like_jobish_url(final_url):
                return final_url
    except Exception:
        pass
    # Slow path: browser-based resolution (LinkedIn, etc.)
    return _resolve_via_browser(url, source)


_LINKEDIN_PROFILE_DIR = SCRIPT_DIR.parent / ".playwright-linkedin"
_LINKEDIN_LOCK_FILE = SCRIPT_DIR.parent / ".playwright-linkedin.lock"


def _resolve_company_url_to_board(url: str) -> str | None:
    """Resolve a company career page URL to its underlying ATS board URL.

    Handles cases like: https://www.bill.com/job?gh_jid=5755728004
    → https://job-boards.greenhouse.io/billcom/jobs/5755728004

    Strategies:
    1. Detect Greenhouse gh_jid param and resolve via the company's Greenhouse slug.
    2. Follow the URL and check for redirects to a known board.
    """
    from job_board_urls import resolve_job_source_url

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    # If it's already on a board domain (not a company website), return as-is
    _board_hosts = (
        "greenhouse.io",
        "lever.co",
        "ashbyhq.com",
        "myworkdayjobs.com",
        "icims.com",
        "phenom.com",
        "eightfold.ai",
        "bamboohr.com",
        "smartrecruiters.com",
        "dover.com",
        "gem.com",
    )
    if any(h in host for h in _board_hosts):
        return url

    # Strategy 1: use the shared canonical resolver for wrapper URLs.
    try:
        resolved = resolve_job_source_url(url)
        if resolved != url and _is_known_board_url(resolved):
            return resolved
    except Exception:
        pass

    # Strategy 2: Follow the URL and check for redirect to a board
    try:
        import urllib.request
        from html import unescape

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            final_url = resp.url
            if final_url != url and _is_known_board_url(final_url):
                return _canonical_board_url(final_url)
            html = resp.read(500_000).decode("utf-8", errors="replace")
        seen: set[str] = set()
        for pattern in _STATIC_APPLY_URL_PATTERNS:
            for match in pattern.finditer(html):
                raw = unescape(match.group(1)).replace("\\/", "/").strip()
                if not raw or raw.startswith(("#", "javascript:", "mailto:", "tel:")):
                    continue
                candidate = urljoin(url, raw)
                if candidate in seen:
                    continue
                seen.add(candidate)
                if _is_known_board_url(candidate):
                    return _canonical_board_url(candidate)
    except Exception:
        pass

    return url  # Return original — pipeline will try to scrape it as-is


def _resolve_via_browser(url: str, source: str) -> str | None:
    """Use Playwright to resolve aggregator URLs to board URLs.

    For LinkedIn, uses a persistent Playwright profile at .playwright-linkedin/
    so login session persists across runs. A file lock prevents multiple workers
    from opening the same profile simultaneously (Chromium locks the profile dir).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    board_url = None
    if source == "linkedin":
        board_url = _resolve_linkedin_with_lock(url)
        # LinkedIn often resolves to a company career page, not the actual board
        if board_url and not _is_known_board_url(board_url):
            board_url = _resolve_company_url_to_board(board_url)
    else:
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                board_url = _extract_any_board_link(page)
                browser.close()
        except Exception:
            pass
    return board_url


def _ensure_linkedin_logged_in(page) -> bool:
    """Check if the page hit an auth wall and auto-login if credentials are available."""
    if "authwall" not in page.url and "/login" not in page.url and "checkpoint" not in page.url:
        return True  # Not on auth wall
    email = os.environ.get("LINKEDIN_EMAIL", "").strip()
    password = os.environ.get("LINKEDIN_PASSWORD", "").strip()
    if not email or not password:
        return False
    try:
        # Navigate to login page if on authwall
        if "authwall" in page.url:
            page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(2000)
        email_input = page.locator('input#username, input[name="session_key"]').first
        pw_input = page.locator('input#password, input[name="session_password"]').first
        if email_input.count() and pw_input.count():
            email_input.fill(email)
            pw_input.fill(password)
            page.locator('button[type="submit"], button:has-text("Sign in")').first.click()
            page.wait_for_timeout(5000)
            return "feed" in page.url or "linkedin.com/jobs" in page.url or "linkedin.com/in" in page.url
    except Exception:
        pass
    return False


def _resolve_linkedin_with_lock(url: str, retries: int = 3) -> str | None:
    """Resolve a LinkedIn URL with a file lock to prevent profile contention."""
    import fcntl
    import time

    from browser_runtime import normalize_chromium_profile_zoom
    from playwright.sync_api import sync_playwright

    for attempt in range(retries):
        try:
            lock_fd = open(_LINKEDIN_LOCK_FILE, "w")  # noqa: SIM115
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                profile_dir = _LINKEDIN_PROFILE_DIR
                profile_dir.mkdir(parents=True, exist_ok=True)
                normalize_chromium_profile_zoom(
                    profile_dir,
                    hosts=_LINKEDIN_ZOOM_HOSTS,
                    reset_default_zoom=True,
                )
                with sync_playwright() as pw:
                    context = pw.chromium.launch_persistent_context(
                        str(profile_dir),
                        headless=True,
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                    page = context.pages[0] if context.pages else context.new_page()
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        # Auto-login if we hit the auth wall
                        if "authwall" in page.url or "/login" in page.url:
                            if _ensure_linkedin_logged_in(page):
                                # Re-navigate to the original URL after login
                                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        # Wait for Apply button to render (up to 10s)
                        try:
                            page.wait_for_selector(
                                'a[aria-label*="Apply"], button:has-text("Apply"), a:has-text("Apply")',
                                timeout=10000,
                            )
                        except Exception:
                            pass
                        page.wait_for_timeout(1000)
                        result = _extract_linkedin_apply_url(page)
                    finally:
                        context.close()
                    return result
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
            continue
    return None


def linkedin_login_interactive() -> None:
    """Launch a visible browser with the persistent LinkedIn profile for manual login.

    After logging in, close the browser — the session is saved to the profile dir.
    Subsequent resolve_to_board_url calls for LinkedIn URLs will use this session.
    """
    from browser_runtime import normalize_chromium_profile_zoom
    from playwright.sync_api import sync_playwright

    profile_dir = _LINKEDIN_PROFILE_DIR
    profile_dir.mkdir(parents=True, exist_ok=True)
    normalize_chromium_profile_zoom(
        profile_dir,
        hosts=_LINKEDIN_ZOOM_HOSTS,
        reset_default_zoom=True,
    )
    print(f"Opening LinkedIn login... (profile: {profile_dir})")
    print("Log in to LinkedIn, then close the browser window.")
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        # Block until user closes the browser
        try:
            page.wait_for_event("close", timeout=600000)  # 10 min max
        except Exception:
            pass
        context.close()
    print("LinkedIn session saved. URL resolution will now use your login.")


def _mark_applied_on_page(page) -> bool:
    """Mark a job as applied on an already-navigated LinkedIn job page."""
    page.wait_for_timeout(3000)

    # Strategy 1: Check if already marked as applied
    try:
        applied_badge = page.query_selector('span:has-text("Applied"), li-icon[type="success"]')
        if applied_badge and applied_badge.is_visible():
            return True
    except Exception:
        pass

    # Strategy 2: Look for "Did you finish applying?" prompt and click Yes
    # LinkedIn's new UI shows this directly on the job page after clicking Apply
    for yes_selector in [
        # New LinkedIn UI: "Did you finish applying?" Yes/No buttons
        'button:has-text("Yes")',
        'span:has-text("Yes")',
        '[data-test-modal-close-btn]:has-text("Yes")',
        'button.artdeco-button--primary:has-text("Yes")',
        'button:has-text("Done")',
    ]:
        try:
            els = page.query_selector_all(yes_selector)
            for el in els:
                if el.is_visible():
                    el.click()
                    page.wait_for_timeout(2000)
                    return True
        except Exception:
            continue

    # Strategy 3: Click Apply button to trigger the "Did you finish applying?" prompt
    apply_clicked = False
    for selector in [
        'button:has-text("Apply")',
        'a:has-text("Apply")',
        'button[aria-label*="Apply"]',
        'a[aria-label*="Apply"]',
    ]:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.click()
                apply_clicked = True
                break
        except Exception:
            continue

    if apply_clicked:
        page.wait_for_timeout(3000)
        # Now look for the "Did you finish applying?" Yes button again
        for yes_selector in [
            'button:has-text("Yes")',
            'span:has-text("Yes")',
            '[data-test-modal-close-btn]:has-text("Yes")',
        ]:
            try:
                els = page.query_selector_all(yes_selector)
                for el in els:
                    if el.is_visible():
                        el.click()
                        page.wait_for_timeout(2000)
                        return True
            except Exception:
                continue

    return apply_clicked


def _extract_linkedin_job_id(linkedin_url: str) -> str | None:
    parsed = urlparse(linkedin_url)
    match = re.search(r"/jobs/view/(\d+)", parsed.path)
    if match:
        return match.group(1)
    current_job_id = parse_qs(parsed.query).get("currentJobId", [None])[0]
    if current_job_id and current_job_id.isdigit():
        return current_job_id
    return None


def _dismiss_linkedin_recommendation_on_page(page, job_id: str) -> bool:
    return bool(
        page.evaluate(
            """
            async ({ jobId }) => {
                const jsession = document.cookie
                    .split("; ")
                    .find((row) => row.startsWith("JSESSIONID="))
                    ?.split("=")[1];
                if (!jsession) {
                    return false;
                }
                const csrfToken = decodeURIComponent(jsession).replace(/^"|"$/g, "");
                const response = await fetch(
                    "/voyager/api/voyagerJobsDashJobPostingRelevanceFeedback?action=dismiss",
                    {
                        method: "POST",
                        credentials: "include",
                        headers: {
                            "accept": "application/vnd.linkedin.normalized+json+2.1",
                            "content-type": "application/json; charset=UTF-8",
                            "csrf-token": csrfToken,
                            "x-restli-protocol-version": "2.0.0",
                        },
                        body: JSON.stringify({
                            jobPostingRelevanceFeedbackUrn:
                                `urn:li:fsd_jobPostingRelevanceFeedback:urn:li:fsd_jobPosting:${jobId}`,
                            channel: "JOB_SEARCH",
                        }),
                    },
                );
                return response.ok;
            }
            """,
            {"jobId": job_id},
        )
    )


def mark_linkedin_job_applied(linkedin_url: str) -> bool:
    """Navigate to a LinkedIn job page and mark it as "Applied".

    After submitting an application via the company's board, this function
    navigates to the original LinkedIn job posting and clicks through the
    "Did you apply?" dialog to mark the job as applied on LinkedIn.

    Auto-logs in via LINKEDIN_EMAIL/LINKEDIN_PASSWORD env vars if needed.
    Returns True if successfully marked, False otherwise.
    """
    import fcntl
    import time

    try:
        from browser_runtime import normalize_chromium_profile_zoom
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False

    for attempt in range(2):
        try:
            lock_fd = open(_LINKEDIN_LOCK_FILE, "w")  # noqa: SIM115
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                profile_dir = _LINKEDIN_PROFILE_DIR
                profile_dir.mkdir(parents=True, exist_ok=True)
                normalize_chromium_profile_zoom(
                    profile_dir,
                    hosts=_LINKEDIN_ZOOM_HOSTS,
                    reset_default_zoom=True,
                )
                with sync_playwright() as pw:
                    context = pw.chromium.launch_persistent_context(
                        str(profile_dir),
                        headless=True,
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                    page = context.pages[0] if context.pages else context.new_page()
                    try:
                        page.goto(linkedin_url, wait_until="domcontentloaded", timeout=45000)
                        if "authwall" in page.url or "/login" in page.url:
                            if _ensure_linkedin_logged_in(page):
                                page.goto(linkedin_url, wait_until="domcontentloaded", timeout=45000)
                        return _mark_applied_on_page(page)
                    finally:
                        context.close()
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
        except Exception:
            if attempt == 0:
                time.sleep(3)
            continue
    return False


def dismiss_linkedin_job_recommendation(linkedin_url: str) -> bool:
    """Hide a LinkedIn job from recommendations via the jobs search feedback API."""
    import fcntl
    import time

    try:
        from browser_runtime import normalize_chromium_profile_zoom
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False

    job_id = _extract_linkedin_job_id(linkedin_url)
    if not job_id:
        return False

    search_url = f"https://www.linkedin.com/jobs/search/?currentJobId={job_id}"
    for attempt in range(2):
        try:
            lock_fd = open(_LINKEDIN_LOCK_FILE, "w")  # noqa: SIM115
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                profile_dir = _LINKEDIN_PROFILE_DIR
                profile_dir.mkdir(parents=True, exist_ok=True)
                normalize_chromium_profile_zoom(
                    profile_dir,
                    hosts=_LINKEDIN_ZOOM_HOSTS,
                    reset_default_zoom=True,
                )
                with sync_playwright() as pw:
                    context = pw.chromium.launch_persistent_context(
                        str(profile_dir),
                        headless=True,
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                    page = context.pages[0] if context.pages else context.new_page()
                    try:
                        page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                        if "authwall" in page.url or "/login" in page.url:
                            if _ensure_linkedin_logged_in(page):
                                page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(2000)
                        return _dismiss_linkedin_recommendation_on_page(page, job_id)
                    finally:
                        context.close()
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
        except Exception:
            if attempt == 0:
                time.sleep(3)
            continue
    return False


if __name__ == "__main__":
    import argparse as _argparse

    _parser = _argparse.ArgumentParser()
    _parser.add_argument("--linkedin-login", action="store_true")
    _parser.add_argument("--mark-applied", help="LinkedIn URL to mark as applied")
    _parser.add_argument("--dismiss", help="LinkedIn URL to hide from recommendations")
    _args = _parser.parse_args()
    if _args.linkedin_login:
        linkedin_login_interactive()
    elif _args.mark_applied:
        ok = mark_linkedin_job_applied(_args.mark_applied)
        print(f"Mark applied: {'success' if ok else 'failed'}")
    elif _args.dismiss:
        ok = dismiss_linkedin_job_recommendation(_args.dismiss)
        print(f"Dismiss: {'success' if ok else 'failed'}")


def _extract_linkedin_apply_url(page) -> str | None:
    """Extract external apply URL from a LinkedIn job page."""

    # Strategy 1: Find <a> or <button> with "Apply" text and extract href
    # LinkedIn uses aria-label="Apply on company website" for external apply links
    for selector in [
        'a[aria-label*="Apply on company"]',
        'a:has-text("Apply")',
        'button:has-text("Apply")',
    ]:
        try:
            els = page.query_selector_all(selector)
            for el in els:
                href = el.get_attribute("href") or ""
                resolved = _resolve_linkedin_href(href)
                if resolved:
                    return resolved
        except Exception:
            continue

    # Strategy 2: Check all links for LinkedIn redirect wrappers pointing to boards
    try:
        links = page.query_selector_all('a[href*="linkedin.com/redir"]')
        for link in links:
            href = link.get_attribute("href") or ""
            resolved = _resolve_linkedin_href(href)
            if resolved:
                return resolved
    except Exception:
        pass

    # Strategy 3: Scan all links for direct board URLs
    found = _extract_any_board_link(page)
    if found:
        return found

    # Strategy 4: Check if this is an Easy Apply job (has Easy Apply button, no external link)
    easy_apply_btn = page.query_selector(
        'button.jobs-apply-button[aria-label*="Easy Apply"], button[aria-label*="Easy Apply"]'
    )
    if easy_apply_btn:
        import logging as _logging

        log = _logging.getLogger(__name__)
        log.info("LinkedIn Easy Apply detected — returning LinkedIn URL as board URL")
        from job_board_urls import canonical_linkedin_job_url

        return canonical_linkedin_job_url(page.url)
    # No apply mechanism found
    return None


def _resolve_linkedin_href(href: str) -> str | None:
    """Resolve a LinkedIn href to a board URL, handling redirect wrappers.

    Returns any external (non-LinkedIn) URL — even unsupported boards —
    so the pipeline can at least scrape the JD and generate materials.
    """
    from urllib.parse import parse_qs, unquote

    if not href:
        return None

    # Direct external link (known board or not)
    if href and "linkedin.com" not in href and href.startswith("http"):
        return href

    # LinkedIn redirect wrappers:
    #   /redir/redirect/?url=<encoded_url>
    #   /safety/go/?url=<encoded_url>
    for pattern in ("linkedin.com/redir", "linkedin.com/safety/go"):
        if pattern in href:
            params = parse_qs(urlparse(href).query)
            if "url" in params:
                decoded = unquote(params["url"][0])
                if decoded.startswith("http") and "linkedin.com" not in decoded:
                    return decoded

    return None


def _extract_any_board_link(page) -> str | None:
    """Scan all links on a page for known board URLs."""
    try:
        links = page.query_selector_all("a[href]")
        for link in links:
            href = link.get_attribute("href")
            if href and _is_known_board_url(href):
                return href
    except Exception:
        pass
    return None
