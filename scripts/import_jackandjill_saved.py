#!/usr/bin/env python3
"""Import saved jobs from Jack & Jill into the jobs pipeline."""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import parse_qs, urlparse, urlunparse

from saved_portal_browser import saved_portal_browser_session
from saved_portal_import import AuthRequiredError, import_saved_portal_jobs

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
_JACKANDJILL_PROFILE_DIR = PROJECT_ROOT / ".playwright-jackandjill"
_JACKANDJILL_LOCK_FILE = PROJECT_ROOT / ".playwright-jackandjill.lock"
_JACKANDJILL_EXTERNAL_TRIGGER_ATTR = "data-jackandjill-external-trigger"

OPPORTUNITIES_URL = "https://app.jackandjill.ai/jack/dashboard/jobs/opportunities"


class SavedJackAndJillJob(TypedDict, total=False):
    source_url: str
    company: str | None
    role_title: str | None


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


def _ensure_jackandjill_logged_in(page) -> None:
    current_url = str(page.url or "")
    lower_url = current_url.lower()
    if "sign-in" in lower_url or "/login" in lower_url:
        raise AuthRequiredError("Jack & Jill session expired (redirected to sign-in)")

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
    if "jack & jill" in combined and "sign in" in combined:
        raise AuthRequiredError("Jack & Jill authentication required")


def _extract_page_jobs(page) -> list[SavedJackAndJillJob]:
    raw_jobs = page.evaluate(
        """
        () => {
            const seen = new Set();
            const results = [];

            function clean(value) {
                return (value || "").replace(/\\s+/g, " ").trim();
            }

            function pushCandidate(candidate, dedupeKey) {
                if (!dedupeKey || seen.has(dedupeKey)) return;
                seen.add(dedupeKey);
                results.push(candidate);
            }

            const anchors = Array.from(document.querySelectorAll("a[href]"));
            for (const anchor of anchors) {
                const href = clean(anchor.href);
                if (!href) continue;
                if (!href.includes("/jobs/") && !href.includes("/opportunities/")) continue;

                const roleTitle = clean(anchor.innerText);
                if (!roleTitle) continue;

                pushCandidate({ source_url: href, role_title: roleTitle }, href);
            }

            return results;
        }
        """
    )
    jobs: list[SavedJackAndJillJob] = []
    seen: set[str] = set()
    for raw in raw_jobs:
        if not isinstance(raw, Mapping):
            continue
        source_url = _normalize_http_url(raw.get("source_url"))
        if not source_url:
            continue
        if source_url in seen:
            continue
        seen.add(source_url)
        role_title = _clean_text(raw.get("role_title")) or None
        company = _clean_text(raw.get("company")) or None
        job: SavedJackAndJillJob = {"source_url": source_url}
        if role_title is not None:
            job["role_title"] = role_title
        if company is not None:
            job["company"] = company
        jobs.append(job)
    return jobs


def _wait_for_page_settle(page) -> None:
    for state in ("load", "networkidle"):
        try:
            page.wait_for_load_state(state, timeout=3000)
        except Exception:  # noqa: BLE001
            continue


@contextmanager
def _jackandjill_context():
    with saved_portal_browser_session(
        profile_dir=_JACKANDJILL_PROFILE_DIR,
        lock_file=_JACKANDJILL_LOCK_FILE,
        headless=os.getenv("JACKANDJILL_IMPORT_HEADLESS", "").strip().lower() in {"1", "true", "yes", "on"},
        purpose="Jack & Jill saved jobs import",
    ) as browser:
        yield browser


def _scrape_saved_jobs(context) -> list[SavedJackAndJillJob]:
    page = context.new_page()
    page.goto(OPPORTUNITIES_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1000)
    _ensure_jackandjill_logged_in(page)
    return _extract_page_jobs(page)


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
                    return host !== "app.jackandjill.ai" && !host.endsWith(".jackandjill.ai");
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


def _is_jackandjill_url(url: str | None) -> bool:
    normalized = _normalize_http_url(url)
    if not normalized:
        return False
    hostname = (urlparse(normalized).hostname or "").lower()
    return hostname == "jackandjill.ai" or hostname.endswith(".jackandjill.ai")


def _is_external_url(url: str | None) -> bool:
    normalized = _normalize_http_url(url)
    return bool(normalized and not _is_jackandjill_url(normalized))


def _looks_like_job_url(url: str | None) -> bool:
    normalized = _normalize_http_url(url)
    if not normalized:
        return False
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if "/job/" in path or "/jobs/" in path:
        return True
    if "greenhouse.io" in host or "lever.co" in host or "ashbyhq.com" in host:
        return True
    if "myworkdayjobs.com" in host or "icims.com" in host or "smartrecruiters.com" in host:
        return True
    if "bamboohr.com" in host or "eightfold.ai" in host or "phenom.com" in host:
        return True
    if "dover.com" in host or "gem.com" in host:
        return True
    return bool(parse_qs(parsed.query).get("gh_jid"))


def _score_label(label: str) -> int:
    lower = _clean_text(label).lower()
    if not lower:
        return -1
    if "apply" in lower:
        return 100
    if "view job" in lower:
        return 90
    if "company website" in lower or "company site" in lower:
        return 80
    if "open job" in lower:
        return 70
    return -1


def _pick_external_destination(candidates: list[Mapping[str, Any]], *, include_fallback: bool = True) -> str | None:
    fallback = None
    for candidate in candidates:
        url = _normalize_http_url(candidate.get("url"))
        if not url or not _is_external_url(url):
            continue
        label = candidate.get("label") or ""
        score = _score_label(label)
        if score >= 0:
            return url
        if include_fallback and fallback is None and _looks_like_job_url(url):
            fallback = url
    return fallback


def _find_external_destination(page) -> str | None:
    candidates = _collect_external_link_candidates(page)
    destination = _pick_external_destination(candidates, include_fallback=False)
    if destination:
        return destination

    destination = _click_external_destination_control(page)
    if destination:
        return destination

    return _pick_external_destination(candidates)


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
                    return host !== "app.jackandjill.ai" && !host.endsWith(".jackandjill.ai");
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
        _JACKANDJILL_EXTERNAL_TRIGGER_ATTR,
    )
    return bool(marked)


def _click_external_destination_control(page) -> str | None:
    if not _mark_external_destination_control(page):
        return None

    trigger = page.locator(f'[{_JACKANDJILL_EXTERNAL_TRIGGER_ATTR}="true"]').first
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


def _resolve_saved_job(context, job: Mapping[str, Any]) -> dict[str, Any]:
    source_url = _normalize_http_url(job.get("source_url"))
    if not source_url:
        return {
            "status": "unresolved",
            "reason": "missing opportunity URL",
            "source_url": job.get("source_url") or OPPORTUNITIES_URL,
        }

    page = context.new_page()
    try:
        page.goto(source_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(500)
        _ensure_jackandjill_logged_in(page)
        external_url = _find_external_destination(page)
    finally:
        page.close()

    if not external_url:
        return {
            "status": "unresolved",
            "reason": "no external application URL found",
            "source_url": source_url,
        }

    return {
        "status": "resolved",
        "url": external_url,
        "source_url": source_url,
        "company": job.get("company"),
        "role_title": job.get("role_title"),
    }


def import_saved_jobs(
    conn: sqlite3.Connection,
    *,
    priority: int = 0,
    provider: str | None = None,
) -> dict:
    with _jackandjill_context() as context:
        result = import_saved_portal_jobs(
            conn,
            portal_name="jackandjill",
            scrape_jobs=lambda: _scrape_saved_jobs(context),
            resolve_job=lambda job: _resolve_saved_job(context, job),
            priority=priority,
            provider=provider,
        )

    log.info(
        "Jack & Jill saved import: status=%s scraped=%d resolved=%d added=%d duplicates=%d skipped_unresolved=%d errors=%d dup_groups=%d",
        result["status"],
        result["scraped"],
        result["resolved"],
        result["added"],
        result["duplicates"],
        result["skipped_unresolved"],
        result["errors"],
        len(result["duplicate_groups"]),
    )
    return result


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Import Jack & Jill saved jobs")
    parser.add_argument("--priority", type=int, default=0, help="Priority for imported jobs")
    parser.add_argument("--provider", type=str, default=None, help="LLM provider override")
    parser.add_argument("--db", type=str, default="jobs.db", help="Path to jobs database")
    args = parser.parse_args()

    import job_db

    conn = job_db.init_db(args.db)
    try:
        result = import_saved_jobs(conn, priority=args.priority, provider=args.provider)
    finally:
        conn.close()

    print(f"Status:     {result['status']}")
    if result["message"]:
        print(f"Message:    {result['message']}")
    print(f"Scraped:    {result['scraped']}")
    print(f"Resolved:   {result['resolved']}")
    print(f"Added:      {result['added']}")
    print(f"Duplicates: {result['duplicates']}")
    print(f"Unresolved: {result['skipped_unresolved']}")
    print(f"Errors:     {result['errors']}")
    if result["duplicate_groups"]:
        print(f"\nDuplicate groups found ({len(result['duplicate_groups'])}):")
        for group in result["duplicate_groups"]:
            print(f"  Fingerprint {group['fingerprint']}:")
            for job in group["jobs"]:
                print(f"    id={job['id']} {job['company']} — {job['role_title']} ({job['status']})")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
