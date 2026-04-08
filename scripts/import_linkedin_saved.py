#!/usr/bin/env python3
"""Import saved jobs from LinkedIn into the jobs pipeline.

Uses the persistent Playwright LinkedIn profile (.playwright-linkedin/) to
scrape the user's saved jobs list and feed each URL into add_job(), which
handles URL dedup via canonical_url UNIQUE constraint.  After import,
backfills JD fingerprints and returns duplicate groups for downstream dedup.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import TypedDict

from saved_portal_browser import saved_portal_browser_session
from saved_portal_import import import_saved_portal_jobs

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
_LINKEDIN_PROFILE_DIR = PROJECT_ROOT / ".playwright-linkedin"
_LINKEDIN_LOCK_FILE = PROJECT_ROOT / ".playwright-linkedin.lock"

SAVED_JOBS_URL = "https://www.linkedin.com/my-items/saved-jobs/"


class SavedLinkedInJob(TypedDict):
    url: str
    company: str | None
    role_title: str | None


def _sanitize_saved_company(company: str | None) -> str | None:
    from parse_jd import company_name_looks_generic, company_name_looks_locationish

    cleaned = str(company or "").strip() or None
    if not cleaned:
        return None
    if company_name_looks_generic(cleaned) or company_name_looks_locationish(cleaned):
        return None
    return cleaned


def _extract_page_jobs(page) -> list[SavedLinkedInJob]:
    """Extract unique saved jobs with best-effort title/company metadata."""
    return page.evaluate("""
        () => {
            const links = document.querySelectorAll('a[href*="/jobs/view/"]');
            const seen = new Set();
            const results = [];

            function cleanLine(value) {
                return (value || "").replace(/\\s+/g, " ").trim();
            }

            function isMetadataLine(value) {
                const line = cleanLine(value);
                if (!line) return true;
                const lower = line.toLowerCase();
                if (line.startsWith(",")) return true;
                if (lower.includes("verified")) return true;
                if (lower === "easy apply") return true;
                if (lower === "promoted") return true;
                if (lower === "applied") return true;
                if (lower === "in progress") return true;
                if (lower.includes("reviewing applicants")) return true;
                if (lower.startsWith("posted ")) return true;
                if (lower.startsWith("reposted ")) return true;
                if (/(remote|hybrid|on-site|onsite)/i.test(line)) return true;
                if (/,[ ]?[A-Z]{2}\\b/.test(line)) return true;
                return false;
            }

            function extractLines(a) {
                let node = a;
                for (let i = 0; i < 8 && node; i += 1, node = node.parentElement) {
                    const lines = (node.innerText || "")
                        .split("\\n")
                        .map(cleanLine)
                        .filter(Boolean);
                    if (lines.length >= 3) {
                        return lines;
                    }
                }
                return [];
            }

            for (const a of links) {
                const href = a.href.split('?')[0];
                if (!href || seen.has(href)) {
                    continue;
                }
                seen.add(href);

                const lines = extractLines(a);
                let roleTitle = cleanLine(a.innerText) || cleanLine(lines[0]);
                if (!roleTitle && lines.length) {
                    roleTitle = cleanLine(lines[0]);
                }

                let company = null;
                for (const line of lines.slice(1)) {
                    if (!isMetadataLine(line)) {
                        company = cleanLine(line);
                        break;
                    }
                }

                results.push({
                    url: href,
                    company,
                    role_title: roleTitle || null,
                });
            }
            return results;
        }
    """)


def _scrape_saved_jobs(max_pages: int = 200) -> list[SavedLinkedInJob]:
    """Open LinkedIn saved jobs and paginate through all pages.

    LinkedIn uses button-based pagination (10 jobs/page) with a sliding
    window of page buttons.  We click through sequentially: Page 1 → 2 → 3…
    until no next page button exists.

    Returns unique LinkedIn saved jobs with best-effort title/company metadata.
    """
    with saved_portal_browser_session(
        profile_dir=_LINKEDIN_PROFILE_DIR,
        lock_file=_LINKEDIN_LOCK_FILE,
        headless=True,
        purpose="LinkedIn saved jobs import",
        normalize_zoom_hosts=("linkedin.com", "www.linkedin.com"),
        reset_default_zoom=True,
    ) as browser:
        page = browser.new_page()
        all_jobs: list[SavedLinkedInJob] = []
        seen: set[str] = set()

        page.goto(SAVED_JOBS_URL, wait_until="domcontentloaded", timeout=30000)

        # Handle auth wall
        if "authwall" in page.url or "/login" in page.url:
            from url_resolver import _ensure_linkedin_logged_in

            if not _ensure_linkedin_logged_in(page):
                log.error("LinkedIn login failed — run linkedin_login_interactive() first")
                return []
            page.goto(SAVED_JOBS_URL, wait_until="domcontentloaded", timeout=30000)

        # Wait for job cards to render
        page.wait_for_timeout(3000)

        # Paginate through all pages
        current_page = 1
        while current_page <= max_pages:
            page_jobs = _extract_page_jobs(page)
            new_count = 0
            for job in page_jobs:
                url = job["url"]
                if url not in seen:
                    seen.add(url)
                    all_jobs.append(job)
                    new_count += 1
            log.info("page %d: found %d jobs (%d new)", current_page, len(page_jobs), new_count)

            # Try to navigate to next page
            next_page = current_page + 1
            next_btn = page.query_selector(f'button[aria-label="Page {next_page}"]')
            if not next_btn:
                log.info("no Page %d button — reached last page", next_page)
                break

            next_btn.click()
            # Wait for page transition: new cards to load
            page.wait_for_timeout(2000)
            current_page = next_page

    log.info("scraped %d total saved jobs across %d pages", len(all_jobs), current_page)
    return all_jobs


def _get_active_job_by_url(conn: sqlite3.Connection, url: str) -> dict | None:
    for col in ("url", "source_url", "board_url", "canonical_url"):
        row = conn.execute(
            f"SELECT * FROM jobs WHERE {col} = ? AND (archived IS NULL OR archived = FALSE) LIMIT 1",
            (url,),
        ).fetchone()
        if row:
            return dict(row)
    return None


def _mark_and_hide_linkedin_job(linkedin_url: str) -> tuple[bool, bool]:
    from url_resolver import dismiss_linkedin_job_recommendation, mark_linkedin_job_applied

    try:
        marked = mark_linkedin_job_applied(linkedin_url)
    except Exception:
        log.exception("failed to mark LinkedIn job as applied: %s", linkedin_url)
        return False, False

    if not marked:
        return False, False

    try:
        hidden = dismiss_linkedin_job_recommendation(linkedin_url)
    except Exception:
        log.exception("failed to hide LinkedIn recommendation: %s", linkedin_url)
        hidden = False

    return True, hidden


def _resolve_saved_job(job: SavedLinkedInJob) -> dict[str, str | None]:
    linkedin_url = str(job["url"])
    return {
        "status": "resolved",
        "url": linkedin_url,
        "source_url": linkedin_url,
        "company": _sanitize_saved_company(job.get("company")),
        "role_title": job.get("role_title"),
    }


def import_saved_jobs(
    conn: sqlite3.Connection,
    *,
    priority: int = 0,
    provider: str | None = None,
) -> dict:
    """Scrape LinkedIn saved jobs and import into the jobs table.

    Returns shared saved-portal import result fields plus LinkedIn sync counters.
    """
    from job_db import get_job

    linkedin_marked = 0
    linkedin_hidden = 0

    def _on_duplicate(
        callback_conn: sqlite3.Connection,
        resolved: dict[str, str | None],
        existing_id: int | None,
    ) -> None:
        nonlocal linkedin_marked, linkedin_hidden

        if existing_id is not None:
            existing_job = get_job(callback_conn, existing_id)
        else:
            existing_job = _get_active_job_by_url(callback_conn, str(resolved["url"]))

        if existing_job and existing_job.get("status") == "submitted":
            marked, hidden = _mark_and_hide_linkedin_job(str(resolved["source_url"]))
            linkedin_marked += int(marked)
            linkedin_hidden += int(hidden)

    result = import_saved_portal_jobs(
        conn,
        portal_name="linkedin",
        scrape_jobs=_scrape_saved_jobs,
        resolve_job=_resolve_saved_job,
        priority=priority,
        provider=provider,
        on_duplicate=_on_duplicate,
    )

    log.info(
        "LinkedIn saved import: status=%s scraped=%d resolved=%d added=%d duplicates=%d skipped_unresolved=%d errors=%d linkedin_marked=%d linkedin_hidden=%d dup_groups=%d",
        result["status"],
        result["scraped"],
        result["resolved"],
        result["added"],
        result["duplicates"],
        result["skipped_unresolved"],
        result["errors"],
        linkedin_marked,
        linkedin_hidden,
        len(result["duplicate_groups"]),
    )
    return {
        **result,
        "linkedin_marked": linkedin_marked,
        "linkedin_hidden": linkedin_hidden,
    }


def main() -> int:
    """CLI entry point."""
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Import LinkedIn saved jobs")
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

    print(f"Scraped:    {result['scraped']}")
    print(f"Added:      {result['added']}")
    print(f"Duplicates: {result['duplicates']}")
    print(f"Errors:     {result['errors']}")
    if result["linkedin_marked"]:
        print(f"Marked:     {result['linkedin_marked']}")
    if result["linkedin_hidden"]:
        print(f"Hidden:     {result['linkedin_hidden']}")
    if result["duplicate_groups"]:
        print(f"\nDuplicate groups found ({len(result['duplicate_groups'])}):")
        for group in result["duplicate_groups"]:
            print(f"  Fingerprint {group['fingerprint']}:")
            for job in group["jobs"]:
                print(f"    id={job['id']} {job['company']} — {job['role_title']} ({job['status']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
