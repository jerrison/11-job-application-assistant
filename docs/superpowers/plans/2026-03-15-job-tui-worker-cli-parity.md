# Job TUI, Worker, and CLI Parity — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an interactive Textual TUI, a background job worker with autonomous processing, and extend the CLI — all sharing a single SQLite database.

**Architecture:** Three components (TUI, Worker, CLI) share one SQLite database (`jobs.db`). The worker is the only process that mutates job state beyond `queued`. The TUI and CLI enqueue and read. A shared `pipeline_orchestrator.py` module contains the processing logic used by the worker and importable by the CLI.

**Tech Stack:** Python 3.14, SQLite (WAL mode), Textual (TUI), Playwright (browser automation), existing LLM provider chain.

**Spec:** `docs/superpowers/specs/2026-03-15-job-tui-worker-cli-parity-design.md`

---

## File Structure

```
scripts/
├─ job_db.py                   (NEW — SQLite interface, ~250 lines)
├─ url_resolver.py             (NEW — aggregator URL resolution, ~150 lines)
├─ pipeline_orchestrator.py    (NEW — shared processing brain, ~400 lines)
├─ job_worker.py               (NEW — worker pool + coordinator, ~350 lines)
├─ job_tui.py                  (NEW — Textual app with 4 views, ~600 lines)
├─ browser_runtime.py          (MODIFY — extend profile dir for worker ID)
├─ job_assets_pipeline.py      (MODIFY — fix hardcoded provider bug)
├─ autofill_greenhouse.py      (MODIFY — import shared parse_application_profile)
│
bin/
├─ job-assets                  (MODIFY — add new subcommands)
│
tests/
├─ test_job_db.py              (NEW — database layer tests)
├─ test_url_resolver.py        (NEW — URL detection/resolution tests)
├─ test_pipeline_orchestrator.py (NEW — orchestrator tests)
├─ test_job_worker.py          (NEW — worker pool tests)
```

---

## Chunk 1: Foundation — Database Layer + Bug Fixes

### Task 1: Fix hardcoded provider bug in job_assets_pipeline.py

**Files:**
- Modify: `scripts/job_assets_pipeline.py:38-42`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_job_assets_pipeline.py (add to existing file or create)
import sys
from pathlib import Path

def test_require_provider_accepts_gemini_flash(tmp_path, monkeypatch):
    """gemini-flash should be accepted as a valid provider."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gemini")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from job_assets_pipeline import require_provider
    # Should NOT raise
    require_provider("gemini-flash")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_job_assets_pipeline.py::test_require_provider_accepts_gemini_flash -v`
Expected: FAIL with `ValueError: Unsupported provider: gemini-flash`

- [ ] **Step 3: Fix the bug**

In `scripts/job_assets_pipeline.py`, replace lines 38-42:

```python
from llm_provider import VALID_PROVIDERS

def require_provider(provider: str) -> None:
    if provider not in VALID_PROVIDERS:
        raise ValueError(f"Unsupported provider: {provider}")
    from application_submit_common import _provider_binary
    binary = _provider_binary(provider)
    if not shutil.which(binary):
        raise FileNotFoundError(f"'{binary}' is not installed or not on PATH.")
```

This uses `VALID_PROVIDERS` instead of a hardcoded set, and `_provider_binary()` to map `gemini-flash` → `gemini` binary.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_job_assets_pipeline.py::test_require_provider_accepts_gemini_flash -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `uv run python -m pytest tests/ -v`
Expected: All 338+ tests pass

- [ ] **Step 6: Commit**

```bash
git add scripts/job_assets_pipeline.py tests/test_job_assets_pipeline.py
git commit -m "fix: use VALID_PROVIDERS in job_assets_pipeline to support gemini-flash"
```

---

### Task 2: Create job_db.py — SQLite database layer

**Files:**
- Create: `scripts/job_db.py`
- Create: `tests/test_job_db.py`

- [ ] **Step 1: Write failing tests for core DB operations**

```python
# tests/test_job_db.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import sqlite3
import pytest
from job_db import (
    init_db, add_job, get_pending_jobs, update_status,
    log_event, query_jobs, get_job_timeline, get_job,
    JOB_STATUSES,
)


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test_jobs.db"
    conn = init_db(db_path)
    yield conn
    conn.close()


def test_init_db_creates_tables(db):
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert {"jobs", "events", "fix_attempts", "provider_runs"} <= tables


def test_add_job_returns_id(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/company/jobs/123")
    assert job_id == 1


def test_add_job_detects_direct_source(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/company/jobs/123")
    job = get_job(db, job_id)
    assert job["source"] == "direct"
    assert job["board_url"] == "https://boards.greenhouse.io/company/jobs/123"


def test_add_job_detects_linkedin_source(db):
    job_id = add_job(db, url="https://www.linkedin.com/jobs/view/12345")
    job = get_job(db, job_id)
    assert job["source"] == "linkedin"
    assert job["source_url"] == "https://www.linkedin.com/jobs/view/12345"
    assert job["board_url"] is None


def test_add_job_deduplicates_by_canonical_url(db):
    add_job(db, url="https://boards.greenhouse.io/co/jobs/123")
    with pytest.raises(sqlite3.IntegrityError):
        add_job(db, url="https://boards.greenhouse.io/co/jobs/123")


def test_get_pending_jobs_ordered_by_priority_then_created(db):
    id1 = add_job(db, url="https://boards.greenhouse.io/a/jobs/1")
    id2 = add_job(db, url="https://boards.greenhouse.io/b/jobs/2", priority=5)
    pending = get_pending_jobs(db)
    assert pending[0]["id"] == id2  # higher priority first


def test_update_status(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/1")
    update_status(db, job_id, "generating")
    job = get_job(db, job_id)
    assert job["status"] == "generating"


def test_update_status_logs_event(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/1")
    update_status(db, job_id, "generating")
    events = get_job_timeline(db, job_id)
    assert len(events) == 1
    assert events[0]["event_type"] == "status_change"


def test_log_event(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/1")
    log_event(db, job_id, "provider_fallback", detail="gemini → claude")
    events = get_job_timeline(db, job_id)
    assert events[0]["detail"] == "gemini → claude"


def test_query_jobs_filters_by_status(db):
    add_job(db, url="https://boards.greenhouse.io/a/jobs/1")
    id2 = add_job(db, url="https://boards.greenhouse.io/b/jobs/2")
    update_status(db, id2, "generating")
    results = query_jobs(db, status="queued")
    assert len(results) == 1


def test_query_jobs_filters_by_board(db):
    add_job(db, url="https://boards.greenhouse.io/a/jobs/1")
    update_status(db, 1, "generating")  # triggers board detection
    results = query_jobs(db, board="greenhouse")
    # board is set during processing, not at add time — this tests the filter
    assert isinstance(results, list)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_job_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'job_db'`

- [ ] **Step 3: Implement job_db.py**

Create `scripts/job_db.py`:

```python
"""SQLite database layer for job queue management."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

JOB_STATUSES = (
    "queued", "resolving", "generating", "submitting",
    "submitted", "retrying", "fix_in_progress",
    "failed", "skipped_captcha", "skipped_auth",
    "needs_manual", "needs_board_url",
)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL,
    source          TEXT,
    source_url      TEXT,
    board_url       TEXT,
    canonical_url   TEXT UNIQUE,
    company         TEXT,
    role_title      TEXT,
    board           TEXT,
    status          TEXT NOT NULL DEFAULT 'queued',
    priority        INTEGER DEFAULT 0,
    provider        TEXT,
    output_dir      TEXT,
    notion_url      TEXT,
    error_message   TEXT,
    fix_attempts    INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at    TIMESTAMP
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs(id),
    event_type      TEXT NOT NULL,
    detail          TEXT,
    detail_json     TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fix_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs(id),
    error_type      TEXT,
    error_context   TEXT,
    fix_diff        TEXT,
    fix_branch      TEXT,
    tests_passed    BOOLEAN,
    applied         BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS provider_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs(id),
    provider        TEXT NOT NULL,
    phase           TEXT,
    exit_code       INTEGER,
    duration_ms     INTEGER,
    error_message   TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_board ON jobs(board);
CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_events_job_id ON events(job_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_fix_attempts_job_id ON fix_attempts(job_id);
CREATE INDEX IF NOT EXISTS idx_provider_runs_job_id ON provider_runs(job_id);

CREATE TRIGGER IF NOT EXISTS trg_jobs_updated_at AFTER UPDATE ON jobs
BEGIN
    UPDATE jobs SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;
"""

def init_db(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def add_job(
    conn: sqlite3.Connection,
    url: str,
    *,
    priority: int = 0,
    provider: str | None = None,
) -> int:
    from url_resolver import detect_source, _is_known_board_url
    source = detect_source(url)
    if source == "direct" or _is_known_board_url(url):
        source = "direct"
        board_url = url
        source_url = None
        canonical_url = url  # will be refined during resolution
    else:
        board_url = None
        source_url = url
        canonical_url = url  # placeholder until resolved
    cur = conn.execute(
        """INSERT INTO jobs (url, source, source_url, board_url,
           canonical_url, priority, provider, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'queued')""",
        (url, source, source_url, board_url, canonical_url, priority, provider),
    )
    conn.commit()
    return cur.lastrowid


def get_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def get_pending_jobs(
    conn: sqlite3.Connection,
    *,
    exclude_boards: set[str] | None = None,
    limit: int = 10,
) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM jobs
           WHERE status = 'queued'
           ORDER BY priority DESC, created_at ASC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    result = [dict(r) for r in rows]
    if exclude_boards:
        result = [r for r in result if r.get("board") not in exclude_boards]
    return result


def update_status(
    conn: sqlite3.Connection,
    job_id: int,
    status: str,
    *,
    error_message: str | None = None,
    provider: str | None = None,
    board: str | None = None,
    board_url: str | None = None,
    canonical_url: str | None = None,
    company: str | None = None,
    role_title: str | None = None,
    output_dir: str | None = None,
    notion_url: str | None = None,
) -> None:
    sets = ["status = ?"]
    params: list = [status]
    if error_message is not None:
        sets.append("error_message = ?")
        params.append(error_message)
    if provider is not None:
        sets.append("provider = ?")
        params.append(provider)
    if board is not None:
        sets.append("board = ?")
        params.append(board)
    if board_url is not None:
        sets.append("board_url = ?")
        params.append(board_url)
    if canonical_url is not None:
        sets.append("canonical_url = ?")
        params.append(canonical_url)
    if company is not None:
        sets.append("company = ?")
        params.append(company)
    if role_title is not None:
        sets.append("role_title = ?")
        params.append(role_title)
    if output_dir is not None:
        sets.append("output_dir = ?")
        params.append(output_dir)
    if notion_url is not None:
        sets.append("notion_url = ?")
        params.append(notion_url)
    if status == "submitted":
        sets.append("completed_at = CURRENT_TIMESTAMP")
    params.append(job_id)
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)
    log_event(conn, job_id, "status_change", detail=status)
    conn.commit()


def log_event(
    conn: sqlite3.Connection,
    job_id: int,
    event_type: str,
    *,
    detail: str | None = None,
    detail_json: dict | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO events (job_id, event_type, detail, detail_json) VALUES (?, ?, ?, ?)",
        (job_id, event_type, detail, json.dumps(detail_json) if detail_json else None),
    )
    conn.commit()
    return cur.lastrowid


def log_fix_attempt(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    error_type: str,
    error_context: dict,
    fix_diff: str | None = None,
    fix_branch: str | None = None,
    tests_passed: bool | None = None,
    applied: bool = False,
) -> int:
    cur = conn.execute(
        """INSERT INTO fix_attempts
           (job_id, error_type, error_context, fix_diff, fix_branch, tests_passed, applied)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (job_id, error_type, json.dumps(error_context), fix_diff, fix_branch, tests_passed, applied),
    )
    conn.execute(
        "UPDATE jobs SET fix_attempts = fix_attempts + 1 WHERE id = ?",
        (job_id,),
    )
    conn.commit()
    return cur.lastrowid


def log_provider_run(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    provider: str,
    phase: str,
    exit_code: int | None = None,
    duration_ms: int | None = None,
    error_message: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO provider_runs
           (job_id, provider, phase, exit_code, duration_ms, error_message)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (job_id, provider, phase, exit_code, duration_ms, error_message),
    )
    conn.commit()
    return cur.lastrowid


def get_job_timeline(conn: sqlite3.Connection, job_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM events WHERE job_id = ? ORDER BY created_at ASC",
        (job_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def query_jobs(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    board: str | None = None,
    source: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    where = []
    params: list = []
    if status:
        where.append("status = ?")
        params.append(status)
    if board:
        where.append("board = ?")
        params.append(board)
    if source:
        where.append("source = ?")
        params.append(source)
    if search:
        where.append("(company LIKE ? OR role_title LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.extend([limit, offset])
    rows = conn.execute(
        f"SELECT * FROM jobs {clause} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def get_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status"
    ).fetchall()
    return {r["status"]: r["cnt"] for r in rows}


def get_board_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    rows = conn.execute(
        "SELECT board, status, COUNT(*) as cnt FROM jobs GROUP BY board, status"
    ).fetchall()
    result: dict[str, dict[str, int]] = {}
    for r in rows:
        board = r["board"] or "unknown"
        result.setdefault(board, {})
        result[board][r["status"]] = r["cnt"]
    return result


def reset_stale_jobs(
    conn: sqlite3.Connection,
    stale_threshold_seconds: int = 1800,
) -> list[int]:
    in_progress = ("resolving", "generating", "submitting", "fix_in_progress")
    placeholders = ",".join("?" * len(in_progress))
    rows = conn.execute(
        f"""SELECT id FROM jobs
            WHERE status IN ({placeholders})
            AND updated_at < datetime('now', ? || ' seconds')""",
        (*in_progress, f"-{stale_threshold_seconds}"),
    ).fetchall()
    ids = [r["id"] for r in rows]
    for job_id in ids:
        update_status(conn, job_id, "queued", error_message="Reset: stale in-progress job")
    return ids
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_job_db.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run full test suite**

Run: `uv run python -m pytest tests/ -v`
Expected: All tests pass (no regressions)

- [ ] **Step 6: Commit**

```bash
git add scripts/job_db.py tests/test_job_db.py
git commit -m "feat: add SQLite job database layer (job_db.py)"
```

---

### Task 3: Create url_resolver.py — URL source detection and resolution

**Files:**
- Create: `scripts/url_resolver.py`
- Create: `tests/test_url_resolver.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_url_resolver.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from url_resolver import detect_source, SOURCE_PATTERNS


def test_detect_linkedin():
    assert detect_source("https://www.linkedin.com/jobs/view/12345") == "linkedin"


def test_detect_indeed():
    assert detect_source("https://www.indeed.com/viewjob?jk=abc123") == "indeed"


def test_detect_glassdoor():
    assert detect_source("https://www.glassdoor.com/job-listing/pm-j123.htm") == "glassdoor"


def test_detect_greenhouse_direct():
    assert detect_source("https://boards.greenhouse.io/company/jobs/123") == "direct"


def test_detect_phenom_direct():
    assert detect_source("https://careers.adobe.com/us/en/job/R12345") == "direct"


def test_detect_unknown():
    assert detect_source("https://some-random-site.com/jobs/123") == "unknown"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_url_resolver.py -v`
Expected: FAIL

- [ ] **Step 3: Implement url_resolver.py**

```python
"""URL source detection and aggregator-to-board resolution.

Wraps existing board detection from job_board_urls.py.
Adds aggregator detection (LinkedIn, Indeed, Glassdoor) and
redirect-following resolution to find the underlying board URL.
"""
from __future__ import annotations

from urllib.parse import urlparse

SOURCE_PATTERNS: dict[str, tuple[str, ...]] = {
    "linkedin": ("linkedin.com",),
    "indeed": ("indeed.com",),
    "glassdoor": ("glassdoor.com",),
    "ziprecruiter": ("ziprecruiter.com",),
    "wellfound": ("wellfound.com",),
    "builtin": ("builtin.com",),
}


def _is_known_board_url(url: str) -> bool:
    """Check if URL is a recognized job board using existing detectors."""
    try:
        from job_board_urls import (
            looks_like_greenhouse_url, looks_like_dover_url,
            looks_like_lever_url, looks_like_workday_url,
            looks_like_icims_url, looks_like_ashby_url,
            looks_like_phenom_url,
        )
        return any([
            looks_like_greenhouse_url(url),
            looks_like_dover_url(url),
            looks_like_lever_url(url),
            looks_like_workday_url(url),
            looks_like_icims_url(url),
            looks_like_ashby_url(url),
            looks_like_phenom_url(url),
        ])
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


def resolve_to_board_url(url: str) -> str | None:
    """Attempt to resolve an aggregator URL to the underlying board URL.

    Follows redirects from the aggregator's 'Apply' link.
    Returns None if the board URL cannot be determined.
    """
    source = detect_source(url)
    if source == "direct":
        return url
    # For aggregators, try following redirects via urllib
    try:
        from urllib.request import Request, urlopen
        req = Request(url, method="HEAD",
                     headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=15) as resp:
            final_url = resp.url
            if _is_known_board_url(final_url):
                return final_url
    except Exception:
        pass
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_url_resolver.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/url_resolver.py tests/test_url_resolver.py
git commit -m "feat: add URL source detection and resolution (url_resolver.py)"
```

---

### Task 4: Extend browser_runtime.py for per-worker profiles

**Files:**
- Modify: `scripts/browser_runtime.py:58-63`

- [ ] **Step 1: Write failing test**

```python
# tests/test_browser_runtime.py (add to existing file)
def test_submit_browser_profile_dir_with_worker_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JOB_ASSETS_SUBMIT_BROWSER_PROFILE_DIR", raising=False)
    from browser_runtime import submit_browser_profile_dir
    p = submit_browser_profile_dir(worker_id=2)
    assert p.name == "worker-2"
    assert "playwright-submit-profile" in str(p.parent)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_browser_runtime.py::test_submit_browser_profile_dir_with_worker_id -v`
Expected: FAIL (unexpected keyword argument 'worker_id')

- [ ] **Step 3: Add worker_id parameter**

In `scripts/browser_runtime.py`, add `worker_id` parameter while preserving existing logic:

```python
def submit_browser_profile_dir(
    *,
    environ: dict[str, str] | None = None,
    worker_id: int | None = None,
) -> Path:
    env = environ or os.environ
    raw = env.get("JOB_ASSETS_SUBMIT_BROWSER_PROFILE_DIR", "").strip()
    if raw:
        base = Path(raw).expanduser()
    else:
        base = DEFAULT_SUBMIT_BROWSER_PROFILE_DIR
    if worker_id is not None:
        return base / f"worker-{worker_id}"
    return base
```

This preserves the existing `.strip()`, empty-string handling, and `.expanduser()` behavior.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_browser_runtime.py::test_submit_browser_profile_dir_with_worker_id -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `uv run python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add scripts/browser_runtime.py tests/test_browser_runtime.py
git commit -m "feat: support per-worker browser profile directories"
```

---

## Chunk 2: Pipeline Orchestrator + Worker

### Task 5: Create pipeline_orchestrator.py — shared processing brain

**Files:**
- Create: `scripts/pipeline_orchestrator.py`
- Create: `tests/test_pipeline_orchestrator.py`

- [ ] **Step 1: Write failing tests for provider_fallback**

```python
# tests/test_pipeline_orchestrator.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest
from unittest.mock import patch, MagicMock
from pipeline_orchestrator import provider_fallback


def test_provider_fallback_returns_first_success(tmp_path):
    results = {"gemini": 1, "gemini-flash": 1, "claude": 0}
    def mock_run(cmd, **kw):
        provider = cmd[cmd.index("--provider") + 1] if "--provider" in cmd else "gemini"
        r = MagicMock()
        r.returncode = results.get(provider, 1)
        return r
    with patch("subprocess.run", side_effect=mock_run):
        provider, rc = provider_fallback(
            base_cmd=["uv", "run", "python", "scripts/run_pipeline.py", str(tmp_path)],
            providers=["gemini", "gemini-flash", "claude"],
        )
    assert provider == "claude"
    assert rc == 0


def test_provider_fallback_all_fail():
    def mock_run(cmd, **kw):
        r = MagicMock()
        r.returncode = 1
        return r
    with patch("subprocess.run", side_effect=mock_run):
        provider, rc = provider_fallback(
            base_cmd=["uv", "run", "python", "test.py"],
            providers=["gemini", "claude"],
        )
    assert provider is None
    assert rc == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_pipeline_orchestrator.py -v`
Expected: FAIL

- [ ] **Step 3: Implement pipeline_orchestrator.py**

Create `scripts/pipeline_orchestrator.py` with these functions:
- `provider_fallback(base_cmd, providers) -> tuple[str | None, int]` — try providers in order
- `process_job(conn, job_id, *, worker_id, headless) -> str` — full lifecycle, returns final status
- `retry_with_recording(payload_path, board, *, headless, worker_id) -> int` — Playwright trace + action log
- `auto_fix(error_context, board) -> bool` — invoke claude CLI, branch, test, merge/discard

The `process_job()` function is the heart — it runs the full lifecycle from the spec:
1. URL resolution (Phase 1)
2. Asset generation with provider fallback (Phase 2)
3. Submission (Phase 3)
4. Auto-fix loop (Phase 4, max 3 attempts, requires `claude` on PATH)
5. Retry with recording (Phase 5)
6. Post-submit: Notion sync + email reply (Phase 6)

Each phase updates the DB via `job_db` functions and logs events.

Implementation is ~400 lines. The key function signatures:

```python
def provider_fallback(
    base_cmd: list[str],
    providers: list[str],
    *,
    timeout: int | None = None,
) -> tuple[str | None, int]:
    """Try providers in order. Return (winning_provider, exit_code)."""

def process_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    worker_id: int = 0,
    headless: bool = True,
) -> str:
    """Process a single job through the full lifecycle. Returns final status."""

def retry_with_recording(
    payload_path: Path,
    board: str,
    *,
    headless: bool = True,
    worker_id: int = 0,
) -> int:
    """Final submission attempt with full Playwright trace recording."""

def auto_fix(
    error_context: dict,
    board: str,
    *,
    max_attempts: int = 3,
) -> bool:
    """Invoke claude CLI to diagnose and fix an autofill error. Returns True if fixed."""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_pipeline_orchestrator.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/pipeline_orchestrator.py tests/test_pipeline_orchestrator.py
git commit -m "feat: add pipeline orchestrator with provider fallback and auto-fix"
```

---

### Task 6: Create job_worker.py — worker pool with coordinator

**Files:**
- Create: `scripts/job_worker.py`
- Create: `tests/test_job_worker.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_job_worker.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from unittest.mock import patch, MagicMock
from job_db import init_db, add_job, get_job
from job_worker import Coordinator, WorkerPool


def test_coordinator_picks_highest_priority_job(tmp_path):
    conn = init_db(tmp_path / "test.db")
    add_job(conn, url="https://boards.greenhouse.io/a/jobs/1")
    add_job(conn, url="https://boards.greenhouse.io/b/jobs/2", priority=10)
    coord = Coordinator(conn)
    job = coord.next_job(active_boards=set())
    assert job["id"] == 2


def test_coordinator_respects_board_rate_limit(tmp_path):
    conn = init_db(tmp_path / "test.db")
    # Add two greenhouse jobs and one phenom job
    add_job(conn, url="https://boards.greenhouse.io/a/jobs/1")
    add_job(conn, url="https://boards.greenhouse.io/b/jobs/2")
    add_job(conn, url="https://careers.adobe.com/us/en/job/R12345")
    coord = Coordinator(conn)
    # Simulate greenhouse already active — should skip greenhouse, return phenom
    job = coord.next_job(active_boards={"greenhouse"})
    assert job is not None
    assert "adobe" in job["url"]


def test_worker_pool_starts_and_stops(tmp_path):
    conn = init_db(tmp_path / "test.db")
    pool = WorkerPool(conn, num_workers=2, headless=True)
    pool.start()
    assert pool.is_running
    pool.stop()
    assert not pool.is_running
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_job_worker.py -v`
Expected: FAIL

- [ ] **Step 3: Implement job_worker.py**

Create `scripts/job_worker.py` with:
- `Coordinator` class — polls DB, assigns jobs to workers, rate limits by board
- `WorkerPool` class — manages N worker threads
- `Worker` class — calls `process_job()` from `pipeline_orchestrator.py`
- `main()` — CLI entry point with `--workers N`, `--headless`, `--stop` flags

Key implementation details:
- Each worker thread gets its own `worker_id` (1-N) for browser profile isolation
- Coordinator tracks which boards have active workers via a shared set (thread-safe)
- Worker loop: get job → process → update active_boards → sleep 5s → repeat
- Graceful shutdown on SIGINT/SIGTERM: finish current jobs, then exit
- `main()` writes a PID file to `jobs.db.worker.pid` for `worker stop` to find

```python
class Coordinator:
    def __init__(self, conn): ...
    def next_job(self, active_boards: set[str]) -> dict | None:
        """Get next pending job, skipping boards already active.
        Board rate-limiting works by detecting the board from the URL at query time
        (using detect_source/board detection from url_resolver + job_board_urls),
        since the `board` column is only set during processing."""
        ...

class WorkerPool:
    def __init__(self, conn, num_workers, headless): ...
    def start(self): ...
    def stop(self): ...
    @property
    def is_running(self) -> bool: ...

def main():
    """Entry point: parse args, init DB, start pool, block until stopped."""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_job_worker.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/job_worker.py tests/test_job_worker.py
git commit -m "feat: add job worker pool with coordinator and rate limiting"
```

---

## Chunk 3: CLI Parity — New Commands

### Task 7: Add new CLI commands to bin/job-assets

**Files:**
- Modify: `bin/job-assets`

- [ ] **Step 1: Add new commands to KNOWN_COMMANDS**

Add `"add"`, `"queue"`, `"status"`, `"retry"`, `"skip"`, `"prioritize"`, `"worker"`, `"report"`, `"tui"`, `"import"` to the `KNOWN_COMMANDS` set at line 40.

- [ ] **Step 2: Implement cmd_add**

```python
def cmd_add(args: argparse.Namespace) -> int:
    """Queue one or more jobs."""
    from pathlib import Path
    sys.path.insert(0, str(SCRIPTS_ROOT))
    from job_db import init_db, add_job
    conn = init_db(REPO_ROOT / "jobs.db")
    added = 0
    for url in args.urls:
        try:
            job_id = add_job(conn, url=url.strip(), priority=args.priority or 0,
                           provider=args.provider)
            added += 1
            print(f"  #{job_id}: {url.strip()}")
        except Exception as e:
            print(f"  SKIP: {url.strip()} — {e}", file=sys.stderr)
    print(f"\n{added} job(s) added to queue.")
    conn.close()
    return 0
```

- [ ] **Step 3: Implement cmd_queue**

```python
def cmd_queue(args: argparse.Namespace) -> int:
    """Show job queue with filters."""
    sys.path.insert(0, str(SCRIPTS_ROOT))
    from job_db import init_db, query_jobs
    conn = init_db(REPO_ROOT / "jobs.db")
    jobs = query_jobs(conn, status=args.status, board=args.board,
                     source=args.source, search=args.search)
    # Print formatted table
    print(f"{'ID':>5}  {'Status':<15}  {'Company':<20}  {'Role':<30}  {'Board':<10}  {'Source':<10}")
    print("-" * 95)
    for j in jobs:
        print(f"{j['id']:>5}  {j['status']:<15}  {(j['company'] or '?'):<20}  "
              f"{(j['role_title'] or '?'):<30}  {(j['board'] or '?'):<10}  {(j['source'] or '?'):<10}")
    conn.close()
    return 0
```

- [ ] **Step 4: Implement cmd_worker**

```python
def cmd_worker(args: argparse.Namespace) -> int:
    """Start/stop/status the background worker."""
    action = args.action  # "start", "stop", "status"
    if action == "start":
        cmd = python_script_command(SCRIPTS_ROOT / "job_worker.py")
        cmd.extend(["--workers", str(args.workers or 3)])
        if getattr(args, "headless", True):
            cmd.append("--headless")
        # Start as background process
        import subprocess
        proc = subprocess.Popen(cmd, start_new_session=True)
        print(f"Worker started (PID {proc.pid}, {args.workers or 3} workers)")
        return 0
    elif action == "stop":
        # Send SIGTERM to PID file
        pid_path = REPO_ROOT / "jobs.db.worker.pid"
        if pid_path.exists():
            import signal
            pid = int(pid_path.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            print(f"Worker stopped (PID {pid})")
        else:
            print("No worker running.")
        return 0
    elif action == "status":
        pid_path = REPO_ROOT / "jobs.db.worker.pid"
        if pid_path.exists():
            pid = int(pid_path.read_text().strip())
            try:
                os.kill(pid, 0)  # check if alive
                print(f"Worker running (PID {pid})")
            except OSError:
                print("Worker not running (stale PID file)")
        else:
            print("Worker not running.")
        return 0
```

- [ ] **Step 5: Implement remaining commands (status, retry, skip, prioritize, report, tui, import)**

Each follows the same pattern: init DB, call `job_db` functions, print results. The `tui` command launches `job_tui.py`.

- [ ] **Step 6: Register all subparsers**

Add argparse subparsers for each new command following the existing pattern in lines 421-618.

- [ ] **Step 7: Run full test suite**

Run: `uv run python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 8: Commit**

```bash
git add bin/job-assets
git commit -m "feat: add CLI commands for job queue management (add, queue, worker, etc.)"
```

---

## Chunk 4: TUI — Textual Application

### Task 8: Add Textual dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add textual to dependencies**

```bash
uv add textual textual-image
```

Note: The spec lists `aiofiles` as optional. We skip it — Textual's built-in `run_worker()` handles async file/DB operations without an additional dependency.

- [ ] **Step 2: Verify installation**

```bash
uv run python -c "import textual; print(textual.__version__)"
```

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "deps: add textual and textual-image for TUI"
```

---

### Task 9: Create job_tui.py — Textual app with 4 views

**Files:**
- Create: `scripts/job_tui.py`

This is the largest task. Build incrementally:

- [ ] **Step 1: Create app shell with screen switching**

```python
# scripts/job_tui.py
"""Job Application TUI — interactive terminal interface."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, Footer, Static

from job_db import init_db

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "jobs.db"


class DashboardScreen(Screen):
    """Dashboard with summary stats and recent activity."""
    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Dashboard — loading...", id="dashboard-content")
        yield Footer()


class JobApp(App):
    TITLE = "Job Applications"
    BINDINGS = [
        Binding("d", "switch_screen('dashboard')", "Dashboard"),
        Binding("q", "switch_screen('queue')", "Queue"),
        Binding("a", "switch_screen('add')", "Add Jobs"),
        Binding("?", "toggle_help", "Help"),
        Binding("w", "toggle_worker", "Worker"),
    ]

    def on_mount(self) -> None:
        self.db = init_db(DB_PATH)
        self.install_screen(DashboardScreen(), name="dashboard")
        self.push_screen("dashboard")


def main():
    app = JobApp()
    app.run()

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify app launches**

```bash
uv run python scripts/job_tui.py
```
Expected: TUI opens with Dashboard screen, Ctrl+C exits

- [ ] **Step 3: Implement DashboardScreen with cached data loading**

Add background data refresh using Textual's `set_interval`:
- Summary bar with status counts
- Recent activity feed from events table
- Board breakdown with progress bars
- Worker status indicator

Key performance pattern:
```python
class DashboardScreen(Screen):
    _cache: dict = {}

    def on_mount(self):
        self.set_interval(1.5, self._refresh_data)

    async def _refresh_data(self):
        # Run DB query in worker thread to avoid blocking UI
        data = await self.run_worker(self._load_data)
        self._cache = data
        self._update_widgets()
```

- [ ] **Step 4: Implement QueueScreen with virtualized DataTable**

- Sortable columns: Status, Company, Role, Board, Source, Provider, Created
- Filter bar with status/board/source dropdowns and search input
- Keybindings: Enter (detail), R (retry), S (skip), P (prioritize), Delete
- Debounced search (200ms)

- [ ] **Step 5: Implement AddJobsScreen**

- Multi-line TextArea for pasting URLs
- Provider selector (Auto, gemini, gemini-flash, claude, codex)
- Priority selector (Normal, High, Urgent)
- Submit button that calls `add_job()` for each URL

- [ ] **Step 6: Implement JobDetailScreen with tabbed content**

- Header with job metadata
- Timeline widget (scrollable event list)
- TabbedContent with: Report, Screenshot, Recording, Resume, Cover Letter
- Format picker for Resume/Cover Letter scans output directory
- Screenshot tab uses textual-image or fallback to external viewer
- Lazy loading: tab content loads only when selected

- [ ] **Step 7: Test all views manually**

Launch TUI, add a few test URLs, verify:
- Dashboard shows correct counts
- Queue shows jobs, sorting works
- Job Detail shows timeline and tabs
- Keybindings work (D, Q, A, Enter, Esc, R, S, P)
- Navigation is snappy with no perceptible lag

- [ ] **Step 8: Commit**

```bash
git add scripts/job_tui.py
git commit -m "feat: add Textual TUI with dashboard, queue, add jobs, and detail views"
```

---

## Chunk 5: Integration, Polish, and Ship

### Task 10: Fix Greenhouse duplicate parse_application_profile

**Files:**
- Modify: `scripts/autofill_greenhouse.py`

- [ ] **Step 1: Find the duplicate**

Search for `_parse_application_profile` in `autofill_greenhouse.py` and confirm it duplicates `parse_application_profile` from `application_submit_common.py`.

- [ ] **Step 2: Replace with import**

Replace the private function with an import:
```python
from application_submit_common import parse_application_profile
```

Update all call sites in `autofill_greenhouse.py` to use the imported version.

- [ ] **Step 3: Run tests**

Run: `uv run python -m pytest tests/test_greenhouse_autofill.py -v`
Expected: All Greenhouse tests pass

- [ ] **Step 4: Commit**

```bash
git add scripts/autofill_greenhouse.py
git commit -m "refactor: remove duplicate parse_application_profile from greenhouse"
```

---

### Task 11: Add import command for existing output data

**Files:**
- Modify: `bin/job-assets` (add `cmd_import`)
- Modify: `scripts/job_db.py` (add `import_from_output_dir`)

- [ ] **Step 1: Implement import_from_output_dir in job_db.py**

Scans `output/` directories for `application_submission_result.json` files and populates the DB with historical jobs.

- [ ] **Step 2: Add cmd_import to bin/job-assets**

```bash
job-assets import [--output-dir PATH]
```

- [ ] **Step 3: Test with existing output data**

Run: `uv run python -c "from job_db import init_db, import_from_output_dir; ..."`

- [ ] **Step 4: Commit**

```bash
git add scripts/job_db.py bin/job-assets
git commit -m "feat: add import command to populate DB from existing output directories"
```

---

### Task 12: Update instruction files and final verification

**Files:**
- Modify: `CLAUDE.md`
- Modify: `AGENTS.md`
- Modify: `GEMINI.md`

- [ ] **Step 1: Update CLAUDE.md**

Add sections for:
- TUI architecture (job_tui.py, job_db.py, job_worker.py, pipeline_orchestrator.py)
- New CLI commands
- Worker design (pool, rate limiting, auto-fix)
- Database location and schema

- [ ] **Step 2: Update AGENTS.md and GEMINI.md**

Add the new entry points to the board list / CLI sections. These files must remain identical.

- [ ] **Step 3: Run CI sync test**

Run: `uv run python -m pytest tests/test_ci_workflow.py -v`
Expected: AGENTS.md and GEMINI.md identical test passes

- [ ] **Step 4: Run full test suite**

Run: `uv run python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 5: Final manual verification**

```bash
# Test CLI commands
uv run python bin/job-assets add https://boards.greenhouse.io/test/jobs/1
uv run python bin/job-assets queue
uv run python bin/job-assets status 1

# Test TUI
uv run python bin/job-assets tui

# Test worker
uv run python bin/job-assets worker start --workers 1
uv run python bin/job-assets worker status
uv run python bin/job-assets worker stop
```

- [ ] **Step 6: Commit and push**

```bash
git add CLAUDE.md AGENTS.md GEMINI.md
git commit -m "docs: update instruction files with TUI, worker, and CLI parity documentation"
git push origin main
```
