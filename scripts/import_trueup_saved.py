#!/usr/bin/env python3
"""Import saved jobs from TrueUp into the jobs pipeline."""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import urlparse, urlunparse

from saved_portal_browser import saved_portal_browser_session
from saved_portal_import import AuthRequiredError, import_saved_portal_jobs

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
_TRUEUP_PROFILE_DIR = PROJECT_ROOT / ".playwright-trueup"
_TRUEUP_LOCK_FILE = PROJECT_ROOT / ".playwright-trueup.lock"
_TRUEUP_EXTERNAL_TRIGGER_ATTR = "data-trueup-external-trigger"

MY_JOBS_URL = "https://www.trueup.io/myjobs"
_MAX_SHOW_MORE_CLICKS = 200


class SavedTrueUpJob(TypedDict, total=False):
    source_url: str
    external_url: str
    company: str | None
    role_title: str | None
    card_index: int


def _clean_text(value: str | None) -> str:
    return " ".join((value or "").split()).strip()


def _normalize_http_url(url: str | None) -> str | None:
    if not url:
        return None
    candidate = str(url).strip()
    if not candidate:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunparse(parsed._replace(fragment=""))


def _is_trueup_url(url: str | None) -> bool:
    normalized = _normalize_http_url(url)
    if not normalized:
        return False
    hostname = (urlparse(normalized).hostname or "").lower()
    return hostname == "trueup.io" or hostname.endswith(".trueup.io")


def _is_external_url(url: str | None) -> bool:
    normalized = _normalize_http_url(url)
    return bool(normalized and not _is_trueup_url(normalized))


def _ensure_trueup_logged_in(page) -> None:
    current_url = str(page.url or "")
    lower_url = current_url.lower()
    if "/sign-in" in lower_url:
        raise AuthRequiredError("TrueUp session expired (redirected to sign-in)")

    title_text = ""
    try:
        title_text = _clean_text(page.title())
    except Exception:  # noqa: BLE001
        title_text = ""

    body_text = ""
    try:
        body_text = _clean_text(page.locator("body").inner_text(timeout=4000))
    except Exception:  # noqa: BLE001
        body_text = ""

    combined = f"{title_text}\n{body_text}".lower()
    if (
        "sign in to trueup" in combined
        or "unlock the complete trueup platform" in combined
        or ("create free account" in combined and "trueup" in combined)
    ):
        raise AuthRequiredError("TrueUp authentication required")

    cloudflare_markers = (
        "performing security verification",
        "just a moment...",
    )
    if any(marker in combined for marker in cloudflare_markers):
        raise AuthRequiredError("TrueUp security verification is blocking automation")


def _wait_for_page_settle(page) -> None:
    for state in ("load", "networkidle"):
        try:
            page.wait_for_load_state(state, timeout=3000)
        except Exception:  # noqa: BLE001
            continue


def _sanitize_saved_jobs(raw_jobs: list[Any]) -> list[SavedTrueUpJob]:
    jobs: list[SavedTrueUpJob] = []
    seen: set[str] = set()

    for raw_job in raw_jobs:
        if not isinstance(raw_job, Mapping):
            continue

        source_url = _normalize_http_url(raw_job.get("source_url"))
        if source_url and not _is_trueup_url(source_url):
            source_url = None

        external_url = _normalize_http_url(raw_job.get("external_url"))
        if external_url and not _is_external_url(external_url):
            external_url = None

        role_title = _clean_text(raw_job.get("role_title")) or None
        company = _clean_text(raw_job.get("company")) or None

        raw_card_index = raw_job.get("card_index")
        card_index = (
            raw_card_index if isinstance(raw_card_index, int) and not isinstance(raw_card_index, bool) else None
        )
        if card_index is not None and card_index < 0:
            card_index = None

        if not source_url and card_index is None and not external_url:
            continue

        key = external_url or source_url or f"card:{card_index}:{role_title or ''}:{company or ''}"
        if key in seen:
            continue
        seen.add(key)

        sanitized_job: SavedTrueUpJob = {}
        if source_url:
            sanitized_job["source_url"] = source_url
        if external_url:
            sanitized_job["external_url"] = external_url
        if role_title is not None:
            sanitized_job["role_title"] = role_title
        if company is not None:
            sanitized_job["company"] = company
        if card_index is not None:
            sanitized_job["card_index"] = card_index
        jobs.append(sanitized_job)

    return jobs


def _extract_page_jobs(page) -> list[SavedTrueUpJob]:
    """Extract saved jobs from My Jobs, supporting direct-link, anchor, and button-card layouts."""
    raw_jobs = page.evaluate(
        """
        () => {
            const seen = new Set();
            const results = [];

            function clean(value) {
                return (value || "").replace(/\\s+/g, " ").trim();
            }

            function isMetadataLine(line) {
                const normalized = clean(line);
                if (!normalized) return true;
                const lower = normalized.toLowerCase();
                if (lower === "saved") return true;
                if (lower === "new") return true;
                if (lower === "remote" || lower === "hybrid" || lower === "on-site" || lower === "onsite") return true;
                if (lower.startsWith("posted ")) return true;
                if (/,[ ]?[A-Z]{2}\\b/.test(normalized)) return true;
                return false;
            }

            function extractRoleCompany(text) {
                const lines = String(text || "")
                    .split("\\n")
                    .map(clean)
                    .filter(Boolean);
                const role = lines.length ? lines[0] : null;
                let company = null;
                for (const line of lines.slice(1)) {
                    if (!isMetadataLine(line)) {
                        company = line;
                        break;
                    }
                }
                return { role_title: role, company };
            }

            function isTrueUpUrl(href) {
                try {
                    const parsed = new URL(href, window.location.href);
                    const host = parsed.hostname.toLowerCase();
                    return parsed.protocol === "http:" || parsed.protocol === "https:"
                        ? host === "trueup.io" || host.endsWith(".trueup.io")
                        : false;
                } catch (_err) {
                    return false;
                }
            }

            function isExternalUrl(href) {
                try {
                    const parsed = new URL(href, window.location.href);
                    const host = parsed.hostname.toLowerCase();
                    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
                    return host !== "trueup.io" && !host.endsWith(".trueup.io");
                } catch (_err) {
                    return false;
                }
            }

            function findJobCard(node) {
                let current = node.parentElement;
                while (current && current !== document.body) {
                    const companyLink = current.querySelector('a[href^="/co/"]');
                    if (companyLink && current.contains(node)) return current;
                    current = current.parentElement;
                }
                return node.closest('article, li, [data-testid*="job"], [class*="job"], [class*="card"]');
            }

            function pushCandidate(candidate, dedupeKey) {
                if (!dedupeKey || seen.has(dedupeKey)) return;
                seen.add(dedupeKey);
                results.push(candidate);
            }

            const directExternalAnchors = Array.from(document.querySelectorAll("a[href]"));
            for (const anchor of directExternalAnchors) {
                const href = clean(anchor.href);
                if (!href || !isExternalUrl(href)) continue;

                const rel = clean(anchor.getAttribute("rel") || "").toLowerCase();
                const target = clean(anchor.getAttribute("target") || "").toLowerCase();
                if (target !== "_blank" || !rel.includes("nofollow")) continue;

                const roleTitle = clean(anchor.innerText);
                if (!roleTitle) continue;

                const card = findJobCard(anchor);
                if (!card) continue;

                const companyLink = Array.from(card.querySelectorAll('a[href^="/co/"]'))
                    .map((link) => clean(link.innerText))
                    .find(Boolean);
                if (!companyLink) continue;

                pushCandidate(
                    {
                        source_url: window.location.href,
                        external_url: href,
                        role_title: roleTitle,
                        company: companyLink,
                    },
                    `external:${href}`
                );
            }

            const anchors = Array.from(document.querySelectorAll('a[href*="/jobs/"]'));
            for (const anchor of anchors) {
                let href = clean(anchor.href);
                if (!href) continue;
                href = href.split("?")[0];
                if (!/\\/jobs\\//.test(href) || !isTrueUpUrl(href)) continue;
                const card = anchor.closest("article, li, [data-testid*='job'], [class*='job']");
                const text = card ? card.innerText : anchor.innerText;
                const parsed = extractRoleCompany(text);
                pushCandidate(
                    {
                        source_url: href,
                        role_title: parsed.role_title,
                        company: parsed.company,
                    },
                    `url:${href}`
                );
            }

            const buttonNodes = Array.from(document.querySelectorAll("button, [role='button']"));
            const cardButtons = buttonNodes.filter((node) => {
                const text = clean(node.innerText);
                if (!text) return false;
                if (text.length < 12 || text.length > 500) return false;
                const lower = text.toLowerCase();
                if (lower === "save" || lower === "unsave" || lower === "next" || lower === "previous") return false;
                return text.split("\\n").filter((line) => clean(line).length > 0).length >= 2;
            });

            cardButtons.forEach((button, index) => {
                const parsed = extractRoleCompany(button.innerText);
                const card = button.closest("article, li, [data-testid*='job'], [class*='job']");
                const nestedLink = card ? card.querySelector('a[href*="/jobs/"]') : null;
                const nestedHref = nestedLink ? clean(nestedLink.href).split("?")[0] : null;
                const keyTitle = clean(parsed.role_title || "");
                const keyCompany = clean(parsed.company || "");
                const dedupeKey = nestedHref ? `url:${nestedHref}` : `card:${index}:${keyTitle}:${keyCompany}`;
                pushCandidate(
                    {
                        source_url: nestedHref || undefined,
                        role_title: parsed.role_title,
                        company: parsed.company,
                        card_index: index,
                    },
                    dedupeKey
                );
            });

            return results;
        }
        """
    )
    return _sanitize_saved_jobs(raw_jobs)


def _expand_all_saved_jobs(page, *, max_clicks: int = _MAX_SHOW_MORE_CLICKS) -> int:
    current_count = len(_extract_page_jobs(page))

    for _ in range(max_clicks):
        button = page.get_by_role("button", name="Show more")
        if button.count() == 0 or not button.is_visible() or button.is_disabled():
            break

        try:
            button.scroll_into_view_if_needed(timeout=5000)
        except Exception:  # noqa: BLE001
            pass

        try:
            button.click(timeout=5000)
        except Exception:  # noqa: BLE001
            break

        next_count = current_count
        for _attempt in range(10):
            page.wait_for_timeout(500)
            _wait_for_page_settle(page)
            next_count = len(_extract_page_jobs(page))
            if next_count > current_count:
                break

        if next_count <= current_count:
            break

        current_count = next_count

    return current_count


@contextmanager
def _trueup_context():
    with saved_portal_browser_session(
        profile_dir=_TRUEUP_PROFILE_DIR,
        lock_file=_TRUEUP_LOCK_FILE,
        headless=os.getenv("TRUEUP_IMPORT_HEADLESS", "").strip().lower() in {"1", "true", "yes", "on"},
        purpose="TrueUp saved jobs import",
    ) as browser:
        yield browser


def _scrape_saved_jobs(context) -> list[SavedTrueUpJob]:
    page = context.new_page()
    page.goto(MY_JOBS_URL, wait_until="domcontentloaded", timeout=45000)
    _wait_for_page_settle(page)
    page.wait_for_timeout(500)
    _ensure_trueup_logged_in(page)
    _expand_all_saved_jobs(page)
    return _extract_page_jobs(page)


def _open_saved_job_from_my_jobs(page, job: Mapping[str, Any]) -> str | None:
    page.goto(MY_JOBS_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2000)
    _ensure_trueup_logged_in(page)

    result = page.evaluate(
        """
        (candidate) => {
            function clean(value) {
                return (value || "").replace(/\\s+/g, " ").trim();
            }

            function containsIgnoreCase(haystack, needle) {
                if (!needle) return true;
                return clean(haystack).toLowerCase().includes(clean(needle).toLowerCase());
            }

            const nodes = Array.from(document.querySelectorAll("button, [role='button']"));
            const cards = nodes.filter((node) => {
                const text = clean(node.innerText);
                if (!text) return false;
                if (text.length < 12 || text.length > 500) return false;
                const lower = text.toLowerCase();
                if (lower === "save" || lower === "unsave" || lower === "next" || lower === "previous") return false;
                return text.split("\\n").filter((line) => clean(line).length > 0).length >= 2;
            });

            let target = null;
            if (Number.isInteger(candidate.card_index) && candidate.card_index >= 0 && candidate.card_index < cards.length) {
                target = cards[candidate.card_index];
            }

            if (!target) {
                const expectedRole = clean(candidate.role_title || "");
                const expectedCompany = clean(candidate.company || "");
                target = cards.find((node) => {
                    const text = clean(node.innerText);
                    return containsIgnoreCase(text, expectedRole) && containsIgnoreCase(text, expectedCompany);
                }) || null;
            }

            if (!target) {
                return { clicked: false, source_url: null };
            }

            const card = target.closest("article, li, [data-testid*='job'], [class*='job']");
            const nestedLink = card ? card.querySelector('a[href*="/jobs/"]') : null;
            const nestedHref = nestedLink ? clean(nestedLink.href).split("?")[0] : null;
            target.click();
            return { clicked: true, source_url: nestedHref || null };
        }
        """,
        dict(job),
    )

    _wait_for_page_settle(page)
    page.wait_for_timeout(500)
    if not result.get("clicked"):
        return None
    current_url = _normalize_http_url(str(page.url or ""))
    if current_url and _is_trueup_url(current_url) and "/jobs/" in urlparse(current_url).path:
        return current_url
    return _normalize_http_url(result.get("source_url")) or None


def _collect_external_link_candidates(page) -> list[dict[str, str]]:
    raw_candidates = page.evaluate(
        """
        () => {
            function clean(value) {
                return (value || "").replace(/\\s+/g, " ").trim();
            }

            function isVisible(el) {
                if (!el || !el.getBoundingClientRect) return false;
                const style = window.getComputedStyle(el);
                if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity || "1") === 0) return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            }

            function isExternal(href) {
                try {
                    const parsed = new URL(href, window.location.href);
                    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
                    const host = parsed.hostname.toLowerCase();
                    return host !== "trueup.io" && !host.endsWith(".trueup.io");
                } catch (_err) {
                    return false;
                }
            }

            return Array.from(document.querySelectorAll("a[href]"))
                .filter(isVisible)
                .map((link) => ({
                    url: clean(link.href),
                    label: clean(link.innerText || link.getAttribute("aria-label") || link.getAttribute("title") || ""),
                }))
                .filter((candidate) => isExternal(candidate.url));
        }
        """
    )
    return [candidate for candidate in raw_candidates if isinstance(candidate, dict)]


def _pick_external_destination(
    candidates: list[Mapping[str, Any]],
    *,
    include_fallback: bool = True,
) -> str | None:
    fallback = None
    preferred_keywords = ("apply", "view job", "company website", "company site", "open job")

    for candidate in candidates:
        url = _normalize_http_url(candidate.get("url"))
        if not url or not _is_external_url(url):
            continue

        label = _clean_text(candidate.get("label")).lower()
        if fallback is None:
            fallback = url
        if any(keyword in label for keyword in preferred_keywords):
            return url

    if include_fallback:
        return fallback
    return None


def _mark_external_destination_control(page) -> bool:
    marked = page.evaluate(
        """
        (attrName) => {
            function clean(value) {
                return (value || "").replace(/\\s+/g, " ").trim();
            }

            function isVisible(el) {
                if (!el || !el.getBoundingClientRect) return false;
                const style = window.getComputedStyle(el);
                if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity || "1") === 0) return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            }

            function isExternal(href) {
                try {
                    const parsed = new URL(href, window.location.href);
                    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
                    const host = parsed.hostname.toLowerCase();
                    return host !== "trueup.io" && !host.endsWith(".trueup.io");
                } catch (_err) {
                    return false;
                }
            }

            function scoreLabel(label) {
                const lower = clean(label).toLowerCase();
                if (!lower) return -1;
                if (lower.includes("apply")) return 100;
                if (lower.includes("view job")) return 90;
                if (lower.includes("company website") || lower.includes("company site")) return 80;
                if (lower.includes("open job")) return 70;
                return -1;
            }

            document.querySelectorAll(`[${attrName}]`).forEach((node) => node.removeAttribute(attrName));

            let best = null;
            for (const node of Array.from(document.querySelectorAll("a[href], button, [role='button']"))) {
                if (!isVisible(node)) continue;
                const href = clean(node.href || node.getAttribute("href") || "");
                if (href && isExternal(href)) continue;
                const label = clean(node.innerText || node.getAttribute("aria-label") || node.getAttribute("title") || "");
                const score = scoreLabel(label);
                if (score < 0) continue;
                if (!best || score > best.score) {
                    best = { node, score };
                }
            }

            if (!best) return false;
            best.node.setAttribute(attrName, "true");
            return true;
        }
        """,
        _TRUEUP_EXTERNAL_TRIGGER_ATTR,
    )
    return bool(marked)


def _click_external_destination_control(page) -> str | None:
    if not _mark_external_destination_control(page):
        return None

    trigger = page.locator(f'[{_TRUEUP_EXTERNAL_TRIGGER_ATTR}="true"]').first
    if trigger.count() == 0:
        return None

    existing_page_ids = {id(existing_page) for existing_page in page.context.pages}
    try:
        trigger.click(timeout=5000)
    except Exception:  # noqa: BLE001
        return None

    for attempt in range(5):
        candidate_pages = [page]
        candidate_pages.extend(
            candidate_page for candidate_page in page.context.pages if id(candidate_page) not in existing_page_ids
        )

        for candidate_page in candidate_pages:
            _wait_for_page_settle(candidate_page)
            current_url = _normalize_http_url(str(candidate_page.url or ""))
            if current_url and _is_external_url(current_url):
                return current_url

            destination = _pick_external_destination(_collect_external_link_candidates(candidate_page))
            if destination:
                return destination

        if attempt < 4:
            page.wait_for_timeout(500)

    return None


def _find_external_destination(page) -> str | None:
    candidates = _collect_external_link_candidates(page)
    destination = _pick_external_destination(candidates, include_fallback=False)
    if destination:
        return destination

    destination = _click_external_destination_control(page)
    if destination:
        return destination

    return _pick_external_destination(candidates)


def _load_trueup_source_page(page, source_url: str) -> None:
    normalized_source_url = _normalize_http_url(source_url)
    if not normalized_source_url or not _is_trueup_url(normalized_source_url):
        return

    current_url = _normalize_http_url(str(page.url or ""))
    if current_url != normalized_source_url:
        page.goto(normalized_source_url, wait_until="domcontentloaded", timeout=45000)
    _wait_for_page_settle(page)
    page.wait_for_timeout(500)
    _ensure_trueup_logged_in(page)


def _is_trueup_job_detail_url(url: str | None) -> bool:
    normalized = _normalize_http_url(url)
    if not normalized or not _is_trueup_url(normalized):
        return False
    path = (urlparse(normalized).path or "").strip("/")
    return path.startswith("jobs/")


def _extract_page_jd_text(page, source_url: str | None) -> str | None:
    from scrape_job import clean_text, extract_rendered_browser_data, extract_structured_html_fallback

    current_url = _normalize_http_url(source_url) or _normalize_http_url(str(page.url or ""))
    if not current_url:
        return None

    html_doc = ""
    try:
        html_doc = str(page.content() or "")
    except Exception:  # noqa: BLE001
        html_doc = ""
    if html_doc:
        try:
            structured = extract_structured_html_fallback(html_doc, current_url)
        except Exception:  # noqa: BLE001
            structured = None
        full_text = str((structured or {}).get("full_text") or "").strip()
        if len(full_text) >= 200:
            return full_text

    title_text = ""
    try:
        title_text = _clean_text(page.title())
    except Exception:  # noqa: BLE001
        title_text = ""

    body_text = ""
    try:
        body_text = _clean_text(page.locator("body").inner_text(timeout=4000))
    except Exception:  # noqa: BLE001
        body_text = ""
    if not body_text:
        return None

    try:
        rendered = extract_rendered_browser_data(current_url, title_text, body_text)
    except Exception:  # noqa: BLE001
        rendered = None
    full_text = str((rendered or {}).get("full_text") or "").strip()
    if len(full_text) >= 200:
        return full_text

    cleaned = clean_text(body_text)
    return cleaned if len(cleaned) >= 400 else None


def _resolve_saved_job(context, job: SavedTrueUpJob) -> dict[str, Any]:
    page = context.new_page()
    source_url = _normalize_http_url(job.get("source_url"))
    external_url = _normalize_http_url(job.get("external_url"))
    discovered_source_url = source_url
    jd_text: str | None = None

    try:
        if external_url and _is_external_url(external_url):
            if _is_trueup_job_detail_url(source_url):
                try:
                    _load_trueup_source_page(page, source_url)
                    jd_text = _extract_page_jd_text(page, source_url)
                except Exception:  # noqa: BLE001
                    jd_text = None
            else:
                try:
                    page.goto(external_url, wait_until="domcontentloaded", timeout=45000)
                    _wait_for_page_settle(page)
                    jd_text = _extract_page_jd_text(page, external_url)
                except Exception:  # noqa: BLE001
                    jd_text = None
            return {
                "status": "resolved",
                "url": external_url,
                "source_url": source_url if source_url and _is_trueup_url(source_url) else MY_JOBS_URL,
                "company": job.get("company"),
                "role_title": job.get("role_title"),
                "jd_text": jd_text,
            }

        if source_url and not _is_trueup_url(source_url):
            return {
                "status": "unresolved",
                "source_url": MY_JOBS_URL,
                "reason": "saved TrueUp candidate did not contain a TrueUp job URL",
            }

        if source_url:
            _load_trueup_source_page(page, source_url)
            jd_text = _extract_page_jd_text(page, source_url)
        else:
            discovered_source_url = _open_saved_job_from_my_jobs(page, job)
            current_url = _normalize_http_url(str(page.url or ""))
            if current_url and _is_external_url(current_url):
                return {
                    "status": "resolved",
                    "url": current_url,
                    "source_url": discovered_source_url or MY_JOBS_URL,
                    "company": job.get("company"),
                    "role_title": job.get("role_title"),
                    "jd_text": _extract_page_jd_text(page, current_url),
                }
            if not discovered_source_url:
                _ensure_trueup_logged_in(page)
            if discovered_source_url and _is_trueup_url(discovered_source_url):
                _load_trueup_source_page(page, discovered_source_url)
                jd_text = _extract_page_jd_text(page, discovered_source_url)

        external_url = _find_external_destination(page)
        if not external_url and source_url:
            reopened_source_url = _open_saved_job_from_my_jobs(page, job)
            if reopened_source_url and _is_trueup_url(reopened_source_url):
                discovered_source_url = reopened_source_url
                _load_trueup_source_page(page, reopened_source_url)
                jd_text = _extract_page_jd_text(page, reopened_source_url)
            else:
                _ensure_trueup_logged_in(page)
            external_url = _find_external_destination(page)
        if not external_url:
            return {
                "status": "unresolved",
                "source_url": discovered_source_url or source_url or MY_JOBS_URL,
                "reason": "no external apply/view-job link found",
            }

        return {
            "status": "resolved",
            "url": external_url,
            "source_url": discovered_source_url or source_url or MY_JOBS_URL,
            "company": job.get("company"),
            "role_title": job.get("role_title"),
            "jd_text": jd_text,
        }
    finally:
        page.close()


def import_saved_jobs(
    conn: sqlite3.Connection,
    *,
    priority: int = 0,
    provider: str | None = None,
) -> dict[str, Any]:
    with _trueup_context() as context:
        return import_saved_portal_jobs(
            conn,
            portal_name="trueup",
            scrape_jobs=lambda: _scrape_saved_jobs(context),
            resolve_job=lambda candidate: _resolve_saved_job(context, candidate),
            priority=priority,
            provider=provider,
        )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Import TrueUp saved jobs")
    parser.add_argument("--priority", type=int, default=0, help="Priority for imported jobs")
    parser.add_argument("--provider", type=str, default=None, help="LLM provider override")
    parser.add_argument("--db", type=str, default="jobs.db", help="Path to jobs database")
    args = parser.parse_args()

    from job_db import init_db

    conn = init_db(args.db)
    try:
        result = import_saved_jobs(conn, priority=args.priority, provider=args.provider)
    finally:
        conn.close()

    print(f"Status:      {result['status']}")
    if result["message"]:
        print(f"Message:     {result['message']}")
    print(f"Scraped:     {result['scraped']}")
    print(f"Resolved:    {result['resolved']}")
    print(f"Added:       {result['added']}")
    print(f"Duplicates:  {result['duplicates']}")
    print(f"Unresolved:  {result['skipped_unresolved']}")
    print(f"Errors:      {result['errors']}")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
