# TrueUp My Jobs Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add TrueUp My Jobs import support across the web UI, TUI, and CLI so saved TrueUp jobs are resolved to external job-board URLs, deduplicated through the existing queue path, and added with TrueUp provenance preserved.

**Architecture:** Introduce a shared saved-portal import runner that owns result accounting, dedup-aware insertion, and downstream fingerprint backfill. Refactor the existing LinkedIn saved-job importer onto that runner, then add a TrueUp adapter that uses a dedicated persistent browser profile to scrape `https://www.trueup.io/myjobs`, follow external-apply links, and queue only resolved external destinations. Surface the shared importer through `job-assets add --saved-portal ...`, synchronous web endpoints, and a second TUI import button on the Add Jobs screen.

**Tech Stack:** Python 3.14 via `uv`, SQLite (`scripts/job_db.py`), Playwright persistent Chromium profiles, FastAPI, vanilla JS frontend, Textual TUI, existing pytest/ruff validation

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `scripts/saved_portal_import.py` | Shared runner for saved-portal imports, common result contract, auth-required short-circuit |
| `scripts/import_trueup_saved.py` | TrueUp adapter, persistent profile handling, `/myjobs` scraping, external-board resolution |
| `tests/test_saved_portal_import.py` | Unit tests for shared runner accounting and auth behavior |
| `tests/test_import_trueup_saved.py` | Unit tests for TrueUp import wrapper behavior and provenance |
| `tests/test_job_tui.py` | First TUI tests covering Add Jobs saved-portal controls and feedback formatting |

### Modified Files
| File | Changes |
|------|---------|
| `scripts/import_linkedin_saved.py` | Refactor onto shared runner while preserving duplicate-sync behavior |
| `scripts/job_db.py` | Add source metadata overrides to `add_job()` |
| `scripts/url_resolver.py` | Teach `detect_source()` about `trueup.io` |
| `scripts/job_web.py` | Add synchronous saved-portal import API surface |
| `scripts/static/index.html` | Add TrueUp import buttons in queue and Add Jobs views |
| `scripts/static/app.js` | Add generic saved-portal import helper and result formatting |
| `scripts/job_tui.py` | Add TrueUp import button, shared feedback formatting, provider/priority-aware portal imports |
| `bin/job-assets` | Extend `add` command with `--saved-portal`, dispatch importers, print shared summaries |
| `tests/test_import_linkedin_saved.py` | Cover shared-result fields after LinkedIn refactor |
| `tests/test_job_db.py` | Verify `add_job()` provenance overrides |
| `tests/test_url_resolver.py` | Add `trueup` source detection coverage |
| `tests/test_job_web.py` | Cover TrueUp import API and saved-portal buttons in HTML |
| `tests/test_job_assets_cli.py` | Cover parser/dispatch for `job-assets add --saved-portal` |
| `docs/cli-reference.md` | Document saved-portal import mode and login prerequisite |

## Execution Order Note

Complete **Task 2 before Task 1**.

Reason: the shared runner in Task 1 passes `source_override` and `source_url_override` into `add_job()`. Those parameters do not exist until Task 2 lands. After Task 2 is complete, execute Task 1, then continue with Task 3 onward.

---

### Task 1: Introduce The Shared Saved-Portal Import Runner

**Files:**
- Create: `scripts/saved_portal_import.py`
- Test: `tests/test_saved_portal_import.py`

- [ ] **Step 1: Write the failing runner tests**

```python
# tests/test_saved_portal_import.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from job_db import add_job, init_db
from saved_portal_import import AuthRequiredError, import_saved_portal_jobs


def _conn(tmp_path: Path):
    conn = init_db(tmp_path / "saved_portal.db")
    try:
        yield conn
    finally:
        conn.close()


def test_import_saved_portal_jobs_counts_added_duplicates_unresolved_and_errors(tmp_path):
    for conn in _conn(tmp_path):
        add_job(
            conn,
            url="https://boards.greenhouse.io/beta/jobs/2",
            company="Beta",
            role_title="Platform PM",
        )

        candidates = [
            {"source_url": "https://trueup.io/jobs/acme-1", "company": "Acme", "role_title": "AI PM"},
            {"source_url": "https://trueup.io/jobs/beta-2", "company": "Beta", "role_title": "Platform PM"},
            {"source_url": "https://trueup.io/jobs/gamma-3", "company": "Gamma", "role_title": "Infra PM"},
            {"source_url": "https://trueup.io/jobs/delta-4", "company": "Delta", "role_title": "Growth PM"},
        ]

        def scrape_jobs():
            return candidates

        def resolve_job(candidate):
            source_url = candidate["source_url"]
            if source_url.endswith("acme-1"):
                return {
                    "status": "resolved",
                    "url": "https://boards.greenhouse.io/acme/jobs/1",
                    "source_url": source_url,
                    "company": "Acme",
                    "role_title": "AI PM",
                }
            if source_url.endswith("beta-2"):
                return {
                    "status": "resolved",
                    "url": "https://boards.greenhouse.io/beta/jobs/2",
                    "source_url": source_url,
                    "company": "Beta",
                    "role_title": "Platform PM",
                }
            if source_url.endswith("gamma-3"):
                return {"status": "unresolved", "source_url": source_url, "reason": "no external apply link"}
            raise RuntimeError("detail page timeout")

        result = import_saved_portal_jobs(
            conn,
            portal_name="trueup",
            scrape_jobs=scrape_jobs,
            resolve_job=resolve_job,
            priority=5,
            provider="codex",
        )

        assert result["status"] == "ok"
        assert result["scraped"] == 4
        assert result["resolved"] == 2
        assert result["added"] == 1
        assert result["duplicates"] == 1
        assert result["skipped_unresolved"] == 1
        assert result["errors"] == 1
        assert result["samples"]["unresolved"][0]["source_url"] == "https://trueup.io/jobs/gamma-3"
        assert result["samples"]["errors"][0]["source_url"] == "https://trueup.io/jobs/delta-4"


def test_import_saved_portal_jobs_returns_auth_required_when_scrape_fails(tmp_path):
    for conn in _conn(tmp_path):
        def scrape_jobs():
            raise AuthRequiredError("TrueUp session expired")

        result = import_saved_portal_jobs(
            conn,
            portal_name="trueup",
            scrape_jobs=scrape_jobs,
            resolve_job=lambda candidate: candidate,
        )

        assert result["status"] == "auth_required"
        assert result["message"] == "TrueUp session expired"
        assert result["scraped"] == 0
        assert result["added"] == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/test_saved_portal_import.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'saved_portal_import'`

- [ ] **Step 3: Implement the shared runner**

```python
# scripts/saved_portal_import.py
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


class AuthRequiredError(RuntimeError):
    """Raised when a saved-portal session is unauthenticated or unusable."""


def _empty_result(*, status: str = "ok", message: str = "") -> dict[str, Any]:
    return {
        "status": status,
        "message": message,
        "scraped": 0,
        "resolved": 0,
        "added": 0,
        "duplicates": 0,
        "skipped_unresolved": 0,
        "errors": 0,
        "fingerprints_added": 0,
        "duplicate_groups": [],
        "samples": {"unresolved": [], "errors": []},
    }


def _append_sample(bucket: list[dict[str, str]], item: dict[str, str], *, limit: int = 5) -> None:
    if len(bucket) < limit:
        bucket.append(item)


def import_saved_portal_jobs(
    conn: sqlite3.Connection,
    *,
    portal_name: str,
    scrape_jobs: Callable[[], list[dict[str, Any]]],
    resolve_job: Callable[[dict[str, Any]], dict[str, Any]],
    priority: int = 0,
    provider: str | None = None,
    on_duplicate: Callable[[sqlite3.Connection, dict[str, Any], int | None], None] | None = None,
) -> dict[str, Any]:
    from job_db import add_job, backfill_jd_fingerprints, find_jd_duplicates

    result = _empty_result()
    try:
        candidates = list(scrape_jobs())
    except AuthRequiredError as exc:
        return _empty_result(status="auth_required", message=str(exc))

    result["scraped"] = len(candidates)

    for candidate in candidates:
        source_url = str(candidate.get("source_url") or candidate.get("url") or "").strip()
        try:
            resolved = resolve_job(candidate)
        except AuthRequiredError as exc:
            result["status"] = "auth_required"
            result["message"] = str(exc)
            break
        except Exception as exc:
            log.exception("%s import failed for %s", portal_name, source_url)
            result["errors"] += 1
            _append_sample(result["samples"]["errors"], {"source_url": source_url, "reason": str(exc)})
            continue

        if resolved.get("status") == "unresolved":
            result["skipped_unresolved"] += 1
            _append_sample(
                result["samples"]["unresolved"],
                {
                    "source_url": str(resolved.get("source_url") or source_url),
                    "reason": str(resolved.get("reason") or "unresolved"),
                },
            )
            continue

        resolved_url = str(resolved["url"]).strip()
        result["resolved"] += 1

        try:
            job_id = add_job(
                conn,
                resolved_url,
                priority=priority,
                provider=provider,
                company=resolved.get("company"),
                role_title=resolved.get("role_title"),
                source_override=portal_name,
                source_url_override=str(resolved.get("source_url") or source_url or resolved_url),
            )
            if job_id < 0:
                result["duplicates"] += 1
                if on_duplicate is not None:
                    on_duplicate(conn, resolved, -job_id)
            else:
                result["added"] += 1
        except sqlite3.IntegrityError:
            result["duplicates"] += 1
            if on_duplicate is not None:
                on_duplicate(conn, resolved, None)

    if result["added"] > 0:
        fingerprints_added, _ = backfill_jd_fingerprints(conn)
        result["fingerprints_added"] = fingerprints_added
        result["duplicate_groups"] = find_jd_duplicates(conn)

    return result
```

- [ ] **Step 4: Run the runner tests to verify they pass**

Run: `uv run python -m pytest tests/test_saved_portal_import.py -v`
Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/saved_portal_import.py tests/test_saved_portal_import.py
git commit -m "feat: add shared saved-portal import runner"
```

---

### Task 2: Extend `add_job()` For Portal Provenance And Add `trueup` Source Detection

**Files:**
- Modify: `scripts/job_db.py`
- Modify: `scripts/url_resolver.py`
- Test: `tests/test_job_db.py`
- Test: `tests/test_url_resolver.py`

- [ ] **Step 1: Add failing tests for source override behavior and TrueUp source detection**

```python
# tests/test_job_db.py
def test_add_job_source_override_preserves_external_url_and_trueup_source(db):
    job_id = add_job(
        db,
        url="https://boards.greenhouse.io/acme/jobs/123",
        company="Acme",
        role_title="Staff Product Manager",
        source_override="trueup",
        source_url_override="https://www.trueup.io/jobs/acme-staff-pm",
    )

    row = db.execute("SELECT url, source, source_url, board_url, canonical_url FROM jobs WHERE id = ?", (job_id,)).fetchone()

    assert row["url"] == "https://boards.greenhouse.io/acme/jobs/123"
    assert row["source"] == "trueup"
    assert row["source_url"] == "https://www.trueup.io/jobs/acme-staff-pm"
    assert row["board_url"] == "https://boards.greenhouse.io/acme/jobs/123"
    assert row["canonical_url"] == "https://boards.greenhouse.io/acme/jobs/123"
```

```python
# tests/test_url_resolver.py
def test_detect_trueup():
    assert detect_source("https://www.trueup.io/jobs/acme-staff-pm") == "trueup"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/test_job_db.py -k source_override tests/test_url_resolver.py -k trueup -v`
Expected: FAIL with `TypeError: add_job() got an unexpected keyword argument 'source_override'` and `AssertionError` for unknown source

- [ ] **Step 3: Implement the source override path**

```python
# scripts/url_resolver.py
SOURCE_PATTERNS: dict[str, tuple[str, ...]] = {
    "linkedin": ("linkedin.com",),
    "indeed": ("indeed.com",),
    "glassdoor": ("glassdoor.com",),
    "ziprecruiter": ("ziprecruiter.com",),
    "dice": ("dice.com",),
    "wellfound": ("wellfound.com",),
    "builtin": ("builtin.com",),
    "trueup": ("trueup.io",),
}
```

```python
# scripts/job_db.py
def add_job(
    conn: sqlite3.Connection,
    url: str,
    *,
    priority: int = 0,
    provider: str | None = None,
    company: str | None = None,
    role_title: str | None = None,
    source_override: str | None = None,
    source_url_override: str | None = None,
) -> int:
    try:
        from url_resolver import _is_known_board_url, detect_source
    except ImportError:
        detect_source = _fallback_detect_source
        _is_known_board_url = _fallback_is_known_board_url

    source = detect_source(url)
    if source == "direct":
        board_url = url
        source_url = None
        canonical_url = url
    elif source != "unknown":
        board_url = None
        source_url = url
        canonical_url = url
    elif _is_known_board_url(url):
        source = "direct"
        board_url = url
        source_url = None
        canonical_url = url
    else:
        board_url = None
        source_url = url
        canonical_url = url

    if source_override is not None:
        source = source_override
    if source_url_override is not None:
        source_url = source_url_override

    existing_id = _is_duplicate_by_company_role(conn, company, role_title)
    if existing_id is not None:
        log.info("Duplicate of job #%d (same company+role), skipping", existing_id)
        return -existing_id

    for col in ("url", "board_url", "canonical_url"):
        check_val = board_url if col == "board_url" else url
        if not check_val:
            continue
        existing = conn.execute(
            f"SELECT id FROM jobs WHERE {col} = ? AND (archived IS NULL OR archived = FALSE) LIMIT 1",
            (check_val,),
        ).fetchone()
        if existing:
            raise sqlite3.IntegrityError(f"Duplicate job: existing #{existing['id']} has same {col}={check_val!r}")

    cur = conn.execute(
        """INSERT INTO jobs (url, source, source_url, board_url,
           canonical_url, priority, provider, company, role_title, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')""",
        (url, source, source_url, board_url, canonical_url, priority, provider, company, role_title),
    )
    conn.commit()
    return cur.lastrowid
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/test_job_db.py -k source_override tests/test_url_resolver.py -k trueup -v`
Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/job_db.py scripts/url_resolver.py tests/test_job_db.py tests/test_url_resolver.py
git commit -m "feat: preserve portal provenance for imported jobs"
```

---

### Task 3: Refactor The LinkedIn Importer Onto The Shared Runner

**Files:**
- Modify: `scripts/import_linkedin_saved.py`
- Test: `tests/test_import_linkedin_saved.py`

- [ ] **Step 1: Add a failing regression test for the shared result contract**

```python
# tests/test_import_linkedin_saved.py
def test_import_saved_jobs_returns_shared_result_fields(tmp_path):
    for conn in _open_db(tmp_path):
        with patch(
            "import_linkedin_saved._scrape_saved_jobs",
            return_value=[
                {
                    "url": "https://www.linkedin.com/jobs/view/45678",
                    "company": "Acme",
                    "role_title": "Principal Product Manager",
                }
            ],
        ):
            result = import_saved_jobs(conn)

        assert result["status"] == "ok"
        assert result["resolved"] == 1
        assert result["skipped_unresolved"] == 0
        assert result["added"] == 1
```

- [ ] **Step 2: Run the LinkedIn importer tests to verify the new assertion fails**

Run: `uv run python -m pytest tests/test_import_linkedin_saved.py -v`
Expected: FAIL because `resolved` and `status` are missing from the existing result

- [ ] **Step 3: Move LinkedIn import onto `import_saved_portal_jobs()`**

```python
# scripts/import_linkedin_saved.py
from saved_portal_import import import_saved_portal_jobs


def _resolve_saved_job(job: SavedLinkedInJob) -> dict:
    return {
        "status": "resolved",
        "url": job["url"],
        "source_url": job["url"],
        "company": job.get("company"),
        "role_title": job.get("role_title"),
    }


def import_saved_jobs(
    conn: sqlite3.Connection,
    *,
    priority: int = 0,
    provider: str | None = None,
) -> dict:
    from job_db import get_job

    linkedin_marked = 0
    linkedin_hidden = 0

    def _handle_duplicate(conn: sqlite3.Connection, resolved: dict, existing_id: int | None) -> None:
        nonlocal linkedin_marked, linkedin_hidden
        existing_job = get_job(conn, existing_id) if existing_id is not None else _get_active_job_by_url(conn, resolved["url"])
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
        on_duplicate=_handle_duplicate,
    )
    result["linkedin_marked"] = linkedin_marked
    result["linkedin_hidden"] = linkedin_hidden
    return result
```

- [ ] **Step 4: Run the LinkedIn importer tests to verify they pass**

Run: `uv run python -m pytest tests/test_import_linkedin_saved.py -v`
Expected: all LinkedIn importer tests PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/import_linkedin_saved.py tests/test_import_linkedin_saved.py
git commit -m "refactor: move LinkedIn saved import onto shared runner"
```

---

### Task 4: Implement The TrueUp Adapter

**Files:**
- Create: `scripts/import_trueup_saved.py`
- Test: `tests/test_import_trueup_saved.py`

- [ ] **Step 1: Write failing TrueUp adapter tests**

```python
# tests/test_import_trueup_saved.py
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_trueup_saved import AuthRequiredError, import_saved_jobs
from job_db import init_db


def _open_db(tmp_path: Path):
    conn = init_db(tmp_path / "trueup_jobs.db")
    try:
        yield conn
    finally:
        conn.close()


def test_import_saved_jobs_queues_resolved_external_urls_with_trueup_provenance(tmp_path):
    for conn in _open_db(tmp_path):
        fake_context = object()
        with (
            patch("import_trueup_saved._trueup_context", return_value=nullcontext(fake_context)),
            patch(
                "import_trueup_saved._scrape_saved_jobs",
                return_value=[
                    {
                        "source_url": "https://www.trueup.io/jobs/acme-ai-pm",
                        "company": "Acme",
                        "role_title": "AI PM",
                    },
                    {
                        "source_url": "https://www.trueup.io/jobs/beta-platform-pm",
                        "company": "Beta",
                        "role_title": "Platform PM",
                    },
                ],
            ),
            patch(
                "import_trueup_saved._resolve_saved_job",
                side_effect=[
                    {
                        "status": "resolved",
                        "url": "https://boards.greenhouse.io/acme/jobs/1",
                        "source_url": "https://www.trueup.io/jobs/acme-ai-pm",
                        "company": "Acme",
                        "role_title": "AI PM",
                    },
                    {
                        "status": "unresolved",
                        "source_url": "https://www.trueup.io/jobs/beta-platform-pm",
                        "reason": "no external apply link",
                    },
                ],
            ),
        ):
            result = import_saved_jobs(conn, priority=5, provider="codex")

        assert result["status"] == "ok"
        assert result["added"] == 1
        assert result["skipped_unresolved"] == 1
        row = conn.execute("SELECT url, source, source_url, priority, provider FROM jobs").fetchone()
        assert row["url"] == "https://boards.greenhouse.io/acme/jobs/1"
        assert row["source"] == "trueup"
        assert row["source_url"] == "https://www.trueup.io/jobs/acme-ai-pm"
        assert row["priority"] == 5
        assert row["provider"] == "codex"


def test_import_saved_jobs_returns_auth_required_when_trueup_session_is_invalid(tmp_path):
    for conn in _open_db(tmp_path):
        fake_context = object()
        with (
            patch("import_trueup_saved._trueup_context", return_value=nullcontext(fake_context)),
            patch("import_trueup_saved._scrape_saved_jobs", side_effect=AuthRequiredError("TrueUp session expired")),
        ):
            result = import_saved_jobs(conn)

        assert result["status"] == "auth_required"
        assert result["message"] == "TrueUp session expired"
        assert result["added"] == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/test_import_trueup_saved.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'import_trueup_saved'`

- [ ] **Step 3: Implement the TrueUp importer**

```python
# scripts/import_trueup_saved.py
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict

from saved_portal_import import AuthRequiredError, import_saved_portal_jobs

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
_TRUEUP_PROFILE_DIR = PROJECT_ROOT / ".playwright-trueup"
_TRUEUP_LOCK_FILE = PROJECT_ROOT / ".playwright-trueup.lock"
MY_JOBS_URL = "https://www.trueup.io/myjobs"


class SavedTrueUpJob(TypedDict):
    source_url: str
    company: str | None
    role_title: str | None


def _ensure_trueup_logged_in(page) -> None:
    current_url = page.url
    body_text = (page.locator("body").inner_text(timeout=3000) or "").strip()
    if "/login" in current_url or "Log in" in body_text and "Join now" in body_text:
        raise AuthRequiredError("TrueUp session expired")


def _extract_page_jobs(page) -> list[SavedTrueUpJob]:
    return page.evaluate(
        """
        () => {
            const seen = new Set();
            const jobs = [];
            for (const link of document.querySelectorAll('a[href*="/jobs/"]')) {
                const href = (link.href || "").split("?")[0];
                if (!href || seen.has(href)) continue;
                seen.add(href);
                const lines = (link.innerText || "")
                    .split("\\n")
                    .map((line) => line.trim())
                    .filter(Boolean);
                jobs.push({
                    source_url: href,
                    role_title: lines[0] || null,
                    company: lines[1] || null,
                });
            }
            return jobs;
        }
        """
    )


@contextmanager
def _trueup_context():
    from playwright.sync_api import sync_playwright

    _TRUEUP_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = open(_TRUEUP_LOCK_FILE, "w")  # noqa: SIM115
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                str(_TRUEUP_PROFILE_DIR),
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                yield context
            finally:
                context.close()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _scrape_saved_jobs(context) -> list[SavedTrueUpJob]:
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(MY_JOBS_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    _ensure_trueup_logged_in(page)
    return _extract_page_jobs(page)


def _resolve_saved_job(context, job: SavedTrueUpJob) -> dict:
    page = context.new_page()
    try:
        page.goto(job["source_url"], wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        _ensure_trueup_logged_in(page)

        direct_link = page.evaluate(
            """
            () => {
                const link = Array.from(document.querySelectorAll('a[href^="http"]')).find((node) => {
                    const href = node.href || "";
                    const label = (node.innerText || "").toLowerCase();
                    return !href.includes("trueup.io") && (label.includes("apply") || label.includes("view job"));
                });
                return link ? link.href : null;
            }
            """
        )
        if direct_link and "trueup.io" not in direct_link:
            return {
                "status": "resolved",
                "url": direct_link.split("?")[0],
                "source_url": job["source_url"],
                "company": job.get("company"),
                "role_title": job.get("role_title"),
            }

        return {
            "status": "unresolved",
            "source_url": job["source_url"],
            "reason": "no external apply link",
        }
    finally:
        page.close()


def import_saved_jobs(
    conn: sqlite3.Connection,
    *,
    priority: int = 0,
    provider: str | None = None,
) -> dict:
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
```

- [ ] **Step 4: Run the TrueUp importer tests to verify they pass**

Run: `uv run python -m pytest tests/test_import_trueup_saved.py -v`
Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/import_trueup_saved.py tests/test_import_trueup_saved.py
git commit -m "feat: add TrueUp saved-job importer"
```

---

### Task 5: Extend `job-assets add` With `--saved-portal`

**Files:**
- Modify: `bin/job-assets`
- Test: `tests/test_job_assets_cli.py`

- [ ] **Step 1: Add failing CLI parser and dispatch tests**

```python
# tests/test_job_assets_cli.py
    def test_build_parser_accepts_add_saved_portal_mode(self):
        cli = load_cli_module()
        parser = cli.build_parser()

        args = parser.parse_args(["add", "--saved-portal", "trueup", "--priority", "5", "--provider", "codex"])

        self.assertEqual(args.command, "add")
        self.assertEqual(args.saved_portal, "trueup")
        self.assertEqual(args.urls, [])
        self.assertEqual(args.priority, 5)
        self.assertEqual(args.provider, "codex")

    def test_cmd_add_dispatches_saved_portal_import(self):
        cli = load_cli_module()
        args = argparse.Namespace(urls=[], saved_portal="trueup", priority=5, provider="codex")
        stdout = io.StringIO()

        with (
            mock.patch("sys.stdout", stdout),
            mock.patch.object(
                cli,
                "_import_saved_portal",
                return_value={"status": "ok", "added": 2, "duplicates": 1, "skipped_unresolved": 3, "errors": 0, "scraped": 6, "resolved": 4},
            ) as run_import,
        ):
            result = cli.cmd_add(args)

        self.assertEqual(result, 0)
        run_import.assert_called_once_with("trueup", priority=5, provider="codex")
        self.assertIn("TrueUp import", stdout.getvalue())
        self.assertIn("2 added", stdout.getvalue())
```

- [ ] **Step 2: Run the CLI tests to verify they fail**

Run: `uv run python -m pytest tests/test_job_assets_cli.py -k "saved_portal or cmd_add_dispatches_saved_portal_import" -v`
Expected: FAIL because `--saved-portal` and `_import_saved_portal` do not exist

- [ ] **Step 3: Implement CLI saved-portal mode**

```python
# bin/job-assets
OPTIONS_WITH_VALUES = {
    "--bin-dir",
    "--board",
    "--browser-provider",
    "--format",
    "--limit",
    "--man-dir",
    "--max-parallel",
    "--output-dir",
    "--priority",
    "--provider",
    "--saved-portal",
    "--search",
    "--since",
    "--source",
    "--status",
    "--wait-for-email",
    "--workers",
}


def _import_saved_portal(saved_portal: str, *, priority: int, provider: str | None) -> dict:
    sys.path.insert(0, str(SCRIPTS_ROOT))
    module_name = "import_linkedin_saved" if saved_portal == "linkedin" else "import_trueup_saved"
    module = __import__(module_name, fromlist=["import_saved_jobs"])
    from job_db import init_db

    conn = init_db(REPO_ROOT / "jobs.db")
    try:
        return module.import_saved_jobs(conn, priority=priority, provider=provider)
    finally:
        conn.close()


def _print_saved_portal_summary(saved_portal: str, result: dict) -> None:
    label = "LinkedIn" if saved_portal == "linkedin" else "TrueUp"
    print(f"{label} import: status={result['status']} scraped={result['scraped']} resolved={result['resolved']} "
          f"added={result['added']} duplicates={result['duplicates']} unresolved={result['skipped_unresolved']} "
          f"errors={result['errors']}")
    if result.get("message"):
        print(f"Message: {result['message']}")


def cmd_add(args: argparse.Namespace) -> int:
    if args.saved_portal:
        result = _import_saved_portal(args.saved_portal, priority=args.priority, provider=args.provider)
        _print_saved_portal_summary(args.saved_portal, result)
        return 0 if result["status"] == "ok" else 1

    if not args.urls:
        print("Provide one or more URLs, or use --saved-portal linkedin|trueup.", file=sys.stderr)
        return 2

    sys.path.insert(0, str(SCRIPTS_ROOT))
    from job_db import add_job, init_db

    conn = init_db(REPO_ROOT / "jobs.db")
    try:
        for url in args.urls:
            job_id = add_job(conn, url, priority=args.priority, provider=args.provider)
            print(f"Queued job {job_id}: {url}")
    finally:
        conn.close()
    return 0
```

```python
# bin/job-assets build_parser()
    add_parser = subparsers.add_parser(
        "add",
        help="Queue one or more job URLs for processing",
    )
    add_parser.add_argument("urls", nargs="*", help="Job posting URLs to queue")
    add_parser.add_argument(
        "--saved-portal",
        choices=("linkedin", "trueup"),
        help="Import jobs from a saved-job portal instead of positional URLs",
    )
```

- [ ] **Step 4: Run the CLI tests to verify they pass**

Run: `uv run python -m pytest tests/test_job_assets_cli.py -k "saved_portal or cmd_add_dispatches_saved_portal_import" -v`
Expected: targeted CLI tests PASS

- [ ] **Step 5: Commit**

```bash
git add bin/job-assets tests/test_job_assets_cli.py
git commit -m "feat: add saved-portal mode to job-assets add"
```

---

### Task 6: Wire The Saved-Portal Import Endpoints And Web UI

**Files:**
- Modify: `scripts/job_web.py`
- Modify: `scripts/static/index.html`
- Modify: `scripts/static/app.js`
- Test: `tests/test_job_web.py`

- [ ] **Step 1: Add failing web endpoint and HTML tests**

```python
# tests/test_job_web.py
def test_root_includes_trueup_saved_portal_buttons(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="trueup-import-btn"' in resp.text
    assert 'id="add-trueup-import-btn"' in resp.text


def test_import_trueup_saved_endpoint_returns_import_result(client):
    with mock.patch(
        "import_trueup_saved.import_saved_jobs",
        return_value={
            "status": "ok",
            "message": "",
            "scraped": 6,
            "resolved": 4,
            "added": 2,
            "duplicates": 1,
            "skipped_unresolved": 1,
            "errors": 0,
            "fingerprints_added": 0,
            "duplicate_groups": [],
            "samples": {"unresolved": [], "errors": []},
        },
    ):
        resp = client.post("/api/jobs/import/trueup", json={"provider": "codex", "priority": 5})

    assert resp.status_code == 200
    assert resp.json()["added"] == 2
    assert resp.json()["skipped_unresolved"] == 1
```

- [ ] **Step 2: Run the web tests to verify they fail**

Run: `uv run python -m pytest tests/test_job_web.py -k "trueup_saved_portal_buttons or import_trueup_saved_endpoint_returns_import_result" -v`
Expected: FAIL because the button IDs and `/api/jobs/import/trueup` route do not exist

- [ ] **Step 3: Add the saved-portal web API and frontend controls**

```python
# scripts/job_web.py
class SavedPortalImportRequest(BaseModel):
    provider: str | None = None
    priority: int = 0


def _run_saved_portal_import(portal: str, *, conn: sqlite3.Connection, provider: str | None, priority: int) -> dict:
    module_name = "import_linkedin_saved" if portal == "linkedin" else "import_trueup_saved"
    importer = __import__(module_name, fromlist=["import_saved_jobs"])
    return importer.import_saved_jobs(conn, priority=priority, provider=provider)


@app.post("/api/jobs/import/{portal}")
def import_saved_portal(portal: str, req: SavedPortalImportRequest):
    if portal not in {"linkedin", "trueup"}:
        raise HTTPException(404, "Unknown saved portal")
    conn = get_conn()
    return _run_saved_portal_import(portal, conn=conn, provider=req.provider, priority=req.priority)
```

```html
<!-- scripts/static/index.html -->
<button class="btn btn-sm btn-outline" onclick="importSavedPortalFromQueue('linkedin')" id="linkedin-import-btn" title="Import saved jobs from LinkedIn">Import LinkedIn Saved</button>
<button class="btn btn-sm btn-outline" onclick="importSavedPortalFromQueue('trueup')" id="trueup-import-btn" title="Import saved jobs from TrueUp">Import TrueUp My Jobs</button>
```

```html
<!-- scripts/static/index.html Add Jobs view -->
<div class="form-group">
  <label class="form-label">Saved Portals</label>
  <div class="form-row">
    <button class="btn btn-outline" id="add-linkedin-import-btn" onclick="importSavedPortalFromAddView('linkedin')">Import LinkedIn Saved</button>
    <button class="btn btn-outline" id="add-trueup-import-btn" onclick="importSavedPortalFromAddView('trueup')">Import TrueUp My Jobs</button>
  </div>
</div>
```

```javascript
// scripts/static/app.js
function _selectedAddImportSettings() {
  const provider = document.getElementById('provider-select')?.value || null;
  const priority = parseInt(document.getElementById('priority-select')?.value || '0', 10) || 0;
  return { provider, priority };
}

function formatSavedPortalImportSummary(label, result) {
  if (result.status === 'auth_required') {
    return `${label} import needs login: ${result.message || 'session expired'}`;
  }
  const bits = [`${result.added} added`];
  if (result.duplicates) bits.push(`${result.duplicates} duplicate${result.duplicates > 1 ? 's' : ''}`);
  if (result.skipped_unresolved) bits.push(`${result.skipped_unresolved} unresolved`);
  if (result.errors) bits.push(`${result.errors} error${result.errors > 1 ? 's' : ''}`);
  bits.push(`(${result.scraped} scraped)`);
  return `${label}: ${bits.join(', ')}`;
}

async function importSavedPortal(portal, { buttonId, feedbackId = null, provider = null, priority = 0 } = {}) {
  const btn = document.getElementById(buttonId);
  const label = portal === 'trueup' ? 'TrueUp' : 'LinkedIn';
  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Importing...';
  try {
    const result = await apiCall('POST', `/api/jobs/import/${portal}`, { provider, priority });
    const summary = formatSavedPortalImportSummary(label, result);
    if (feedbackId) {
      const feedback = document.getElementById(feedbackId);
      feedback.className = result.status === 'auth_required' ? 'add-feedback error' : 'add-feedback success';
      feedback.textContent = summary;
      feedback.style.display = 'block';
    }
    showToast(summary, result.status === 'auth_required' ? 'warning' : 'success');
  } catch (err) {
    showToast(`${label} import failed: ${err.message}`, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

function importSavedPortalFromQueue(portal) {
  return importSavedPortal(portal, {
    buttonId: portal === 'trueup' ? 'trueup-import-btn' : 'linkedin-import-btn',
    provider: null,
    priority: 0,
  });
}

function importSavedPortalFromAddView(portal) {
  const { provider, priority } = _selectedAddImportSettings();
  return importSavedPortal(portal, {
    buttonId: portal === 'trueup' ? 'add-trueup-import-btn' : 'add-linkedin-import-btn',
    feedbackId: 'add-feedback',
    provider,
    priority,
  });
}
```

- [ ] **Step 4: Run the web tests to verify they pass**

Run: `uv run python -m pytest tests/test_job_web.py -k "trueup_saved_portal_buttons or import_trueup_saved_endpoint_returns_import_result" -v`
Expected: targeted web tests PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/job_web.py scripts/static/index.html scripts/static/app.js tests/test_job_web.py
git commit -m "feat(web): add saved-portal import for LinkedIn and TrueUp"
```

---

### Task 7: Add The TrueUp Control To The TUI And Cover It With Tests

**Files:**
- Modify: `scripts/job_tui.py`
- Create: `tests/test_job_tui.py`

- [ ] **Step 1: Write failing TUI tests**

```python
# tests/test_job_tui.py
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from job_tui import AddJobsScreen, _format_saved_portal_feedback
from textual.app import App


class AddJobsHarness(App[None]):
    MODES = {"add": AddJobsScreen}

    def on_mount(self) -> None:
        self.switch_mode("add")


def test_format_saved_portal_feedback_includes_unresolved_counts():
    msg = _format_saved_portal_feedback(
        "TrueUp",
        {
            "status": "ok",
            "message": "",
            "scraped": 6,
            "resolved": 4,
            "added": 2,
            "duplicates": 1,
            "skipped_unresolved": 3,
            "errors": 0,
        },
    )

    assert "2 added" in msg
    assert "1 duplicate" in msg
    assert "3 unresolved" in msg


def test_add_jobs_screen_shows_trueup_import_button():
    async def _run():
        app = AddJobsHarness()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.query_one("#btn-import-linkedin")
            assert app.screen.query_one("#btn-import-trueup")

    asyncio.run(_run())
```

- [ ] **Step 2: Run the TUI tests to verify they fail**

Run: `uv run python -m pytest tests/test_job_tui.py -v`
Expected: FAIL because `_format_saved_portal_feedback` and `#btn-import-trueup` do not exist

- [ ] **Step 3: Implement the TUI saved-portal controls**

```python
# scripts/job_tui.py
def _format_saved_portal_feedback(label: str, result: dict) -> str:
    if result.get("status") == "auth_required":
        return f"[red]{label} login required[/] {result.get('message', '')}"
    parts = [f"[green]{result['added']} added[/]"]
    if result.get("duplicates"):
        suffix = "duplicate" if result["duplicates"] == 1 else "duplicates"
        parts.append(f"[yellow]{result['duplicates']} {suffix}[/]")
    if result.get("skipped_unresolved"):
        parts.append(f"[yellow]{result['skipped_unresolved']} unresolved[/]")
    if result.get("errors"):
        parts.append(f"[red]{result['errors']} errors[/]")
    parts.append(f"({result['scraped']} scraped from {label})")
    return "  ".join(parts)
```

```python
# scripts/job_tui.py AddJobsScreen.compose()
            with Horizontal(id="add-actions"):
                yield Button("Add Jobs", variant="primary", id="btn-add")
                yield Button("Import LinkedIn Saved", variant="default", id="btn-import-linkedin")
                yield Button("Import TrueUp My Jobs", variant="default", id="btn-import-trueup")
                yield Button("Clear", variant="default", id="btn-clear")
```

```python
# scripts/job_tui.py AddJobsScreen
    def _selected_provider_priority(self) -> tuple[str | None, int]:
        provider_val = self.query_one("#provider-select", Select).value
        provider = provider_val if isinstance(provider_val, str) and provider_val else None
        priority_val = self.query_one("#priority-select", Select).value
        priority = int(priority_val) if isinstance(priority_val, str) and priority_val else 0
        return provider, priority

    @work(thread=True, exclusive=True, group="saved-portal-import")
    def _import_saved_portal(self, portal: str, label: str, button_id: str) -> None:
        feedback = self.query_one("#add-feedback", Static)
        btn = self.query_one(f"#{button_id}", Button)
        provider, priority = self._selected_provider_priority()
        self.app.call_from_thread(feedback.update, f"[yellow]Importing {label} saved jobs…[/]")
        self.app.call_from_thread(setattr, btn, "disabled", True)
        try:
            module_name = "import_linkedin_saved" if portal == "linkedin" else "import_trueup_saved"
            importer = __import__(module_name, fromlist=["import_saved_jobs"])
            conn = _get_conn()
            try:
                result = importer.import_saved_jobs(conn, priority=priority, provider=provider)
            finally:
                conn.close()
            self.app.call_from_thread(feedback.update, _format_saved_portal_feedback(label, result))
        except Exception as exc:
            self.app.call_from_thread(feedback.update, f"[red]{label} import failed: {exc}[/]")
        finally:
            self.app.call_from_thread(setattr, btn, "disabled", False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-add":
            self._add_jobs()
        elif event.button.id == "btn-import-linkedin":
            self._import_saved_portal("linkedin", "LinkedIn", "btn-import-linkedin")
        elif event.button.id == "btn-import-trueup":
            self._import_saved_portal("trueup", "TrueUp", "btn-import-trueup")
        elif event.button.id == "btn-clear":
            self.query_one("#url-input", TextArea).clear()
            self.query_one("#add-feedback", Static).update("")
```

- [ ] **Step 4: Run the TUI tests to verify they pass**

Run: `uv run python -m pytest tests/test_job_tui.py -v`
Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/job_tui.py tests/test_job_tui.py
git commit -m "feat(tui): add TrueUp saved import action"
```

---

### Task 8: Update CLI Docs And Run Full Verification

**Files:**
- Modify: `docs/cli-reference.md`

- [ ] **Step 1: Update the CLI reference**

```markdown
<!-- docs/cli-reference.md -->
# Queue jobs directly
job-assets add https://boards.greenhouse.io/acme/jobs/123

# Import saved-portal jobs
job-assets add --saved-portal linkedin
job-assets add --saved-portal trueup
job-assets add --saved-portal trueup --priority 5 --provider codex
```

Add one short note under the queue-management section:

```markdown
- `job-assets add --saved-portal trueup` requires an already logged-in persistent TrueUp browser profile. The importer resolves each saved job to an external application URL and skips unresolved TrueUp listings.
```

- [ ] **Step 2: Run the targeted implementation test suite**

Run:

```bash
uv run python -m pytest \
  tests/test_saved_portal_import.py \
  tests/test_import_linkedin_saved.py \
  tests/test_import_trueup_saved.py \
  tests/test_job_db.py \
  tests/test_url_resolver.py \
  tests/test_job_web.py \
  tests/test_job_assets_cli.py \
  tests/test_job_tui.py -v
```

Expected: all targeted tests PASS

- [ ] **Step 3: Run repo-wide validation**

Run:

```bash
uv run ruff check scripts/ tests/
uv run python scripts/check_architecture.py
uv run python scripts/sync_agent_files.py --check
uv run python scripts/check_agent_docs.py
```

Expected: all commands exit 0

- [ ] **Step 4: Run a manual TrueUp smoke check with a logged-in profile**

Run:

```bash
uv run python scripts/import_trueup_saved.py --priority 0 --provider codex
job-assets queue --source trueup --limit 5
```

Expected:
- the importer prints `Status: ok` with non-zero `Scraped` when the session is valid
- `Added`, `Duplicates`, `Unresolved`, and `Errors` are all reported explicitly
- `job-assets queue --source trueup --limit 5` shows external board URLs filtered to TrueUp-sourced jobs

- [ ] **Step 5: Commit**

```bash
git add docs/cli-reference.md
git commit -m "docs: document saved-portal import mode"
```
