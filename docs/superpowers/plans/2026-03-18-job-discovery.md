# Job Discovery & Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Discover" page to the existing web UI where users can search for jobs across LinkedIn/Indeed/Glassdoor, see AI fit scores, and send selected jobs to the existing auto-draft pipeline.

**Architecture:** New `candidate_jobs` SQLite table stores discovered jobs separately from the active pipeline. A Python wrapper around the `jobspy` library handles multi-source search. Claude scores each job (0-100) against `master_resume.md`. The frontend adds a new hash-routed page (`#discover`) with a search form, scored results table, and "Draft Selected" bulk action that promotes candidates into the existing `jobs` queue.

**Tech Stack:** Python 3.14 (uv), SQLite (existing DB), jobspy library (pip), Claude API (existing provider), vanilla JS frontend (existing pattern), FastAPI (existing web server)

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `scripts/job_discovery.py` | Search via jobspy, score via Claude, CRUD for candidate_jobs table |
| `tests/test_job_discovery.py` | Unit tests for discovery, scoring, and promotion logic |

### Modified Files
| File | Changes |
|------|---------|
| `scripts/job_db.py` | Add `candidate_jobs` table schema + migration + query functions |
| `scripts/job_web.py` | Add `/api/discover/*` routes (search, list, score, promote, delete) |
| `scripts/static/index.html` | Add nav link + `#discover` view section |
| `scripts/static/app.js` | Add discover page rendering, search form, results table, bulk actions |
| `scripts/static/style.css` | Score badge colors, source badges, discover-specific styles |
| `pyproject.toml` | Add `python-jobspy` dependency |

---

### Task 1: Add `python-jobspy` dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add jobspy to dependencies**

In `pyproject.toml`, add `"python-jobspy>=1.1"` to the `[project.dependencies]` list.

- [ ] **Step 2: Install and verify**

Run: `uv sync && uv run python -c "from jobspy import scrape_jobs; print('jobspy OK')"`
Expected: `jobspy OK`

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "deps: add python-jobspy for job discovery"
```

---

### Task 2: Add `candidate_jobs` table to database

**Files:**
- Modify: `scripts/job_db.py`
- Test: `tests/test_job_discovery.py`

- [ ] **Step 1: Write failing test for candidate_jobs table**

```python
# tests/test_job_discovery.py
import sqlite3
import pytest

@pytest.fixture
def db():
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
    from job_db import init_db
    conn = init_db(":memory:")
    return conn

class TestCandidateJobsSchema:
    def test_candidate_jobs_table_exists(self, db):
        tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "candidate_jobs" in tables

    def test_insert_candidate_job(self, db):
        db.execute(
            """INSERT INTO candidate_jobs
               (source, title, company, job_url, location, salary, job_description)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("linkedin", "Senior PM", "Acme Corp", "https://linkedin.com/jobs/123",
             "San Francisco, CA", "$180k-$220k", "We are looking for..."),
        )
        db.commit()
        row = db.execute("SELECT * FROM candidate_jobs WHERE id = 1").fetchone()
        assert row is not None
        assert row["title"] == "Senior PM"
        assert row["source"] == "linkedin"
        assert row["score"] is None  # Not yet scored

    def test_unique_job_url_constraint(self, db):
        db.execute(
            "INSERT INTO candidate_jobs (source, title, company, job_url) VALUES (?, ?, ?, ?)",
            ("linkedin", "PM", "Acme", "https://example.com/job/1"),
        )
        db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO candidate_jobs (source, title, company, job_url) VALUES (?, ?, ?, ?)",
                ("indeed", "PM", "Acme", "https://example.com/job/1"),
            )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_job_discovery.py::TestCandidateJobsSchema -v`
Expected: FAIL — `candidate_jobs` table doesn't exist

- [ ] **Step 3: Add candidate_jobs schema to job_db.py**

Add to `_SCHEMA` string in `job_db.py`:

```sql
CREATE TABLE IF NOT EXISTS candidate_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    job_url TEXT NOT NULL UNIQUE,
    application_url TEXT,
    location TEXT,
    salary TEXT,
    job_type TEXT,
    job_level TEXT,
    is_remote INTEGER,
    date_posted TEXT,
    job_description TEXT,
    company_industry TEXT,
    company_rating REAL,
    score INTEGER,
    score_reason TEXT,
    status TEXT DEFAULT 'new',
    promoted_job_id INTEGER,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    scored_at TIMESTAMP,
    promoted_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_candidate_status ON candidate_jobs(status);
CREATE INDEX IF NOT EXISTS idx_candidate_score ON candidate_jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_candidate_source ON candidate_jobs(source);
```

Status values: `new` (just discovered), `scored` (has AI score), `promoted` (sent to draft queue), `skipped` (user dismissed).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_job_discovery.py::TestCandidateJobsSchema -v`
Expected: All 3 PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/job_db.py tests/test_job_discovery.py
git commit -m "feat: add candidate_jobs table for job discovery"
```

---

### Task 3: Implement job search via jobspy

**Files:**
- Create: `scripts/job_discovery.py`
- Test: `tests/test_job_discovery.py`

- [ ] **Step 1: Write failing test for search**

```python
# Add to tests/test_job_discovery.py
from unittest.mock import patch, MagicMock
import pandas as pd

class TestJobSearch:
    def test_search_jobs_returns_candidates(self, db):
        from job_discovery import search_jobs
        # Mock jobspy.scrape_jobs to return a DataFrame
        mock_df = pd.DataFrame([{
            "site": "linkedin",
            "title": "Senior PM",
            "company": "TestCo",
            "job_url": "https://linkedin.com/jobs/111",
            "location": "San Francisco, CA",
            "min_amount": 180000,
            "max_amount": 220000,
            "currency": "USD",
            "interval": "YEARLY",
            "date_posted": "2026-03-18",
            "description": "Looking for a PM...",
            "is_remote": False,
            "job_level": "senior",
            "job_type": "Full-time",
            "company_industry": "Technology",
        }])
        with patch("job_discovery.scrape_jobs", return_value=mock_df):
            results = search_jobs(
                conn=db,
                search_term="product manager",
                location="San Francisco, CA",
                results_wanted=10,
            )
        assert len(results) == 1
        assert results[0]["title"] == "Senior PM"
        assert results[0]["company"] == "TestCo"
        # Should be persisted in DB
        row = db.execute("SELECT * FROM candidate_jobs WHERE job_url = ?",
                         ("https://linkedin.com/jobs/111",)).fetchone()
        assert row is not None

    def test_search_deduplicates(self, db):
        from job_discovery import search_jobs
        mock_df = pd.DataFrame([{
            "site": "linkedin", "title": "PM", "company": "Co",
            "job_url": "https://example.com/1", "location": "SF",
            "description": "desc",
        }])
        with patch("job_discovery.scrape_jobs", return_value=mock_df):
            r1 = search_jobs(db, "pm", "SF", 10)
            r2 = search_jobs(db, "pm", "SF", 10)
        assert len(r1) == 1
        assert len(r2) == 0  # duplicate skipped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_job_discovery.py::TestJobSearch -v`
Expected: FAIL — `job_discovery` module not found

- [ ] **Step 3: Implement job_discovery.py search function**

```python
# scripts/job_discovery.py
"""Job discovery: search, score, and promote candidate jobs."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime

log = logging.getLogger(__name__)

try:
    from jobspy import scrape_jobs
except ImportError:
    scrape_jobs = None  # Graceful fallback for tests


def _format_salary(row: dict) -> str | None:
    """Format salary from jobspy row fields."""
    min_amt = row.get("min_amount")
    max_amt = row.get("max_amount")
    currency = row.get("currency", "USD")
    interval = row.get("interval", "YEARLY")
    if not min_amt and not max_amt:
        return None
    symbol = {"USD": "$", "GBP": "\u00a3", "EUR": "\u20ac"}.get(currency, currency + " ")
    parts = []
    if min_amt:
        parts.append(f"{symbol}{int(min_amt):,}")
    if max_amt:
        parts.append(f"{symbol}{int(max_amt):,}")
    salary = " - ".join(parts)
    if interval:
        salary += f" / {interval.lower()}"
    return salary


def search_jobs(
    conn: sqlite3.Connection,
    search_term: str,
    location: str,
    results_wanted: int = 50,
    *,
    sources: list[str] | None = None,
    hours_old: int = 72,
) -> list[dict]:
    """Search for jobs via jobspy and insert new candidates into DB.

    Returns list of newly inserted candidate dicts (skips duplicates).
    """
    if scrape_jobs is None:
        raise RuntimeError("python-jobspy is not installed")

    site_names = sources or ["linkedin", "indeed", "glassdoor"]
    df = scrape_jobs(
        site_name=site_names,
        search_term=search_term,
        location=location,
        results_wanted=results_wanted,
        hours_old=hours_old,
        linkedin_fetch_description=True,
    )

    new_candidates = []
    now = datetime.now(UTC).isoformat()
    for _, row in df.iterrows():
        row_dict = row.to_dict()
        # Replace NaN with None
        row_dict = {k: (None if isinstance(v, float) and v != v else v)
                    for k, v in row_dict.items()}
        job_url = row_dict.get("job_url") or ""
        if not job_url:
            continue
        try:
            conn.execute(
                """INSERT INTO candidate_jobs
                   (source, title, company, job_url, application_url, location,
                    salary, job_type, job_level, is_remote, date_posted,
                    job_description, company_industry, company_rating, discovered_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(row_dict.get("site") or "unknown"),
                    str(row_dict.get("title") or "Unknown"),
                    str(row_dict.get("company") or "Unknown"),
                    job_url,
                    row_dict.get("job_url_direct"),
                    row_dict.get("location"),
                    _format_salary(row_dict),
                    row_dict.get("job_type"),
                    row_dict.get("job_level"),
                    1 if row_dict.get("is_remote") else 0,
                    row_dict.get("date_posted"),
                    row_dict.get("description"),
                    row_dict.get("company_industry"),
                    row_dict.get("company_rating"),
                    now,
                ),
            )
            conn.commit()
            candidate = dict(conn.execute(
                "SELECT * FROM candidate_jobs WHERE job_url = ?", (job_url,)
            ).fetchone())
            new_candidates.append(candidate)
        except sqlite3.IntegrityError:
            pass  # Duplicate URL, skip
    log.info("Discovered %d new candidates (search=%r, location=%r)",
             len(new_candidates), search_term, location)
    return new_candidates
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_job_discovery.py::TestJobSearch -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/job_discovery.py tests/test_job_discovery.py
git commit -m "feat: job search via jobspy with deduplication"
```

---

### Task 4: Implement AI scoring

**Files:**
- Modify: `scripts/job_discovery.py`
- Test: `tests/test_job_discovery.py`

- [ ] **Step 1: Write failing test for scoring**

```python
class TestJobScoring:
    def test_score_candidate(self, db):
        from job_discovery import score_candidate
        # Insert a candidate
        db.execute(
            "INSERT INTO candidate_jobs (source, title, company, job_url, job_description) "
            "VALUES (?, ?, ?, ?, ?)",
            ("linkedin", "Senior PM", "TestCo", "https://example.com/1",
             "Looking for a senior product manager with ML experience"),
        )
        db.commit()

        # Mock the LLM call
        mock_response = '{"score": 85, "reason": "Strong PM match with ML background"}'
        with patch("job_discovery._call_llm_score", return_value=mock_response):
            result = score_candidate(db, 1)

        assert result["score"] == 85
        assert "PM match" in result["reason"]
        # Should be persisted
        row = db.execute("SELECT score, score_reason, status FROM candidate_jobs WHERE id = 1").fetchone()
        assert row["score"] == 85
        assert row["status"] == "scored"

    def test_score_all_unscored(self, db):
        from job_discovery import score_unscored_candidates
        for i in range(3):
            db.execute(
                "INSERT INTO candidate_jobs (source, title, company, job_url, job_description) "
                "VALUES (?, ?, ?, ?, ?)",
                ("linkedin", f"PM {i}", "Co", f"https://example.com/{i}", "desc"),
            )
        db.commit()

        mock_response = '{"score": 70, "reason": "decent match"}'
        with patch("job_discovery._call_llm_score", return_value=mock_response):
            scored = score_unscored_candidates(db)
        assert scored == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_job_discovery.py::TestJobScoring -v`
Expected: FAIL — functions not defined

- [ ] **Step 3: Implement scoring in job_discovery.py**

Add to `scripts/job_discovery.py`:

```python
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

_SCORING_PROMPT = """\
Score how suitable this job is for the candidate on a scale of 0-100.

SCORING CRITERIA:
- Skills & experience match: 0-35 points
- Seniority level alignment: 0-25 points
- Industry/domain fit: 0-20 points
- Location/remote alignment: 0-10 points
- Career growth signal: 0-10 points

CANDIDATE RESUME:
{resume}

JOB LISTING:
Title: {title}
Company: {company}
Location: {location}
Salary: {salary}
Description:
{description}

Respond with ONLY valid JSON: {{"score": <0-100>, "reason": "<1-2 sentences>"}}
"""


def _load_resume_text() -> str:
    """Load master_resume.md as scoring context."""
    resume_path = PROJECT_ROOT / "master_resume.md"
    if resume_path.exists():
        return resume_path.read_text(encoding="utf-8")[:4000]  # Truncate for token efficiency
    return "(no resume available)"


def _call_llm_score(prompt: str) -> str:
    """Call Claude CLI to score a job. Returns raw JSON string."""
    import subprocess as _sp
    result = _sp.run(
        ["claude", "--print", "-p", prompt],
        capture_output=True, text=True, timeout=120, cwd=str(PROJECT_ROOT),
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {result.stderr[:200]}")
    return result.stdout.strip()


def score_candidate(conn: sqlite3.Connection, candidate_id: int) -> dict:
    """Score a single candidate job. Returns {score, reason}."""
    row = conn.execute("SELECT * FROM candidate_jobs WHERE id = ?", (candidate_id,)).fetchone()
    if row is None:
        raise ValueError(f"Candidate {candidate_id} not found")

    resume = _load_resume_text()
    prompt = _SCORING_PROMPT.format(
        resume=resume,
        title=row["title"] or "Unknown",
        company=row["company"] or "Unknown",
        location=row["location"] or "Not specified",
        salary=row["salary"] or "Not specified",
        description=(row["job_description"] or "No description")[:3000],
    )

    raw = _call_llm_score(prompt)
    try:
        parsed = json.loads(raw)
        score = max(0, min(100, int(parsed.get("score", 0))))
        reason = str(parsed.get("reason", ""))
    except (json.JSONDecodeError, ValueError):
        score = 0
        reason = f"Failed to parse LLM response: {raw[:200]}"

    now = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE candidate_jobs SET score = ?, score_reason = ?, status = 'scored', scored_at = ? "
        "WHERE id = ?",
        (score, reason, now, candidate_id),
    )
    conn.commit()
    return {"score": score, "reason": reason}


def score_unscored_candidates(conn: sqlite3.Connection) -> int:
    """Score all unscored candidates. Returns count scored."""
    rows = conn.execute(
        "SELECT id FROM candidate_jobs WHERE status = 'new' AND score IS NULL"
    ).fetchall()
    count = 0
    for row in rows:
        try:
            score_candidate(conn, row["id"])
            count += 1
        except Exception as exc:
            log.warning("Failed to score candidate %d: %s", row["id"], exc)
    return count
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_job_discovery.py::TestJobScoring -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/job_discovery.py tests/test_job_discovery.py
git commit -m "feat: AI scoring for candidate jobs via Claude"
```

---

### Task 5: Implement promotion (candidate → draft queue)

**Files:**
- Modify: `scripts/job_discovery.py`
- Test: `tests/test_job_discovery.py`

- [ ] **Step 1: Write failing test for promotion**

```python
class TestPromotion:
    def test_promote_candidate_to_queue(self, db):
        from job_discovery import promote_candidate
        db.execute(
            "INSERT INTO candidate_jobs (source, title, company, job_url, score, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("linkedin", "PM", "Acme", "https://linkedin.com/jobs/999", 85, "scored"),
        )
        db.commit()

        job_id = promote_candidate(db, 1)
        assert job_id is not None
        assert job_id > 0

        # Candidate should be marked as promoted
        candidate = db.execute("SELECT status, promoted_job_id FROM candidate_jobs WHERE id = 1").fetchone()
        assert candidate["status"] == "promoted"
        assert candidate["promoted_job_id"] == job_id

        # Job should exist in the main queue
        job = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert job is not None
        assert job["status"] == "queued"
        assert "linkedin.com" in job["url"]

    def test_promote_skips_already_promoted(self, db):
        from job_discovery import promote_candidate
        db.execute(
            "INSERT INTO candidate_jobs (source, title, company, job_url, score, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("linkedin", "PM", "Acme", "https://example.com/1", 85, "promoted"),
        )
        db.commit()
        job_id = promote_candidate(db, 1)
        assert job_id is None  # Already promoted
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_job_discovery.py::TestPromotion -v`
Expected: FAIL

- [ ] **Step 3: Implement promotion**

Add to `scripts/job_discovery.py`:

```python
def promote_candidate(conn: sqlite3.Connection, candidate_id: int) -> int | None:
    """Promote a candidate job to the main draft queue.

    Inserts into the jobs table and marks the candidate as promoted.
    Returns the new job_id, or None if already promoted.
    """
    from job_db import add_job

    row = conn.execute("SELECT * FROM candidate_jobs WHERE id = ?", (candidate_id,)).fetchone()
    if row is None:
        raise ValueError(f"Candidate {candidate_id} not found")
    if row["status"] == "promoted":
        return None

    # Use the application URL if available, otherwise the listing URL
    url = row["application_url"] or row["job_url"]
    try:
        job_id = add_job(conn, url)
    except sqlite3.IntegrityError:
        # Duplicate URL — find existing job
        existing = conn.execute(
            "SELECT id FROM jobs WHERE url = ? OR canonical_url = ?", (url, url)
        ).fetchone()
        if existing:
            job_id = existing["id"]
        else:
            raise

    now = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE candidate_jobs SET status = 'promoted', promoted_job_id = ?, promoted_at = ? "
        "WHERE id = ?",
        (job_id, now, candidate_id),
    )
    conn.commit()
    log.info("Promoted candidate %d → job %d (%s at %s)",
             candidate_id, job_id, row["title"], row["company"])
    return job_id
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_job_discovery.py::TestPromotion -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/job_discovery.py tests/test_job_discovery.py
git commit -m "feat: promote candidate jobs to the draft queue"
```

---

### Task 6: Add API routes

**Files:**
- Modify: `scripts/job_web.py`
- Test: `tests/test_job_discovery.py`

- [ ] **Step 1: Write failing test for API routes**

```python
class TestDiscoverAPI:
    def test_list_candidates_empty(self, db):
        """Verify the query function returns empty list when no candidates."""
        from job_discovery import list_candidates
        result = list_candidates(db)
        assert result == []

    def test_list_candidates_with_filters(self, db):
        from job_discovery import list_candidates
        for i, source in enumerate(["linkedin", "indeed", "linkedin"]):
            db.execute(
                "INSERT INTO candidate_jobs (source, title, company, job_url, score, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (source, f"PM {i}", "Co", f"https://example.com/{i}", 80 - i * 10, "scored"),
            )
        db.commit()
        # Filter by source
        result = list_candidates(db, source="linkedin")
        assert len(result) == 2
        # Filter by status
        result = list_candidates(db, status="scored")
        assert len(result) == 3
        # Sorted by score desc
        assert result[0]["score"] >= result[1]["score"]

    def test_skip_candidate(self, db):
        from job_discovery import skip_candidate
        db.execute(
            "INSERT INTO candidate_jobs (source, title, company, job_url) VALUES (?, ?, ?, ?)",
            ("linkedin", "PM", "Co", "https://example.com/1"),
        )
        db.commit()
        skip_candidate(db, 1)
        row = db.execute("SELECT status FROM candidate_jobs WHERE id = 1").fetchone()
        assert row["status"] == "skipped"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_job_discovery.py::TestDiscoverAPI -v`
Expected: FAIL

- [ ] **Step 3: Implement list/skip/delete functions**

Add to `scripts/job_discovery.py`:

```python
def list_candidates(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    source: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List candidate jobs with optional filters, sorted by score desc."""
    clauses = []
    params: list = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if source:
        clauses.append("source = ?")
        params.append(source)
    if search:
        clauses.append("(title LIKE ? OR company LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    where = (" AND ".join(clauses)) if clauses else "1=1"
    rows = conn.execute(
        f"""SELECT * FROM candidate_jobs
            WHERE {where}
            ORDER BY COALESCE(score, -1) DESC, discovered_at DESC
            LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    ).fetchall()
    return [dict(r) for r in rows]


def skip_candidate(conn: sqlite3.Connection, candidate_id: int) -> None:
    """Mark a candidate as skipped (user dismissed)."""
    conn.execute(
        "UPDATE candidate_jobs SET status = 'skipped' WHERE id = ?",
        (candidate_id,),
    )
    conn.commit()


def delete_candidate(conn: sqlite3.Connection, candidate_id: int) -> None:
    """Delete a candidate job."""
    conn.execute("DELETE FROM candidate_jobs WHERE id = ?", (candidate_id,))
    conn.commit()


def get_candidate_stats(conn: sqlite3.Connection) -> dict:
    """Get counts by status."""
    rows = conn.execute(
        "SELECT status, COUNT(*) as count FROM candidate_jobs GROUP BY status"
    ).fetchall()
    return {r["status"]: r["count"] for r in rows}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_job_discovery.py::TestDiscoverAPI -v`
Expected: PASS

- [ ] **Step 5: Add FastAPI routes to job_web.py**

Add these routes **inside `create_app()`** in `scripts/job_web.py` (after existing `/api/` routes), following the existing pattern. Also add Pydantic models before `create_app()`:

```python
# Add with other BaseModel classes at top of file:
class DiscoverSearchRequest(BaseModel):
    search_term: str
    location: str = "San Francisco, CA"
    sources: list[str] | None = None
    results_wanted: int = 50

class PromoteBulkRequest(BaseModel):
    ids: list[int]
```

```python
# Inside create_app(), after existing routes:

# --- Discovery API ---

@app.get("/api/discover/candidates")
def api_list_candidates(
    status: str | None = None,
    source: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    from job_discovery import list_candidates, get_candidate_stats
    conn = get_conn()
    candidates = list_candidates(conn, status=status, source=source,
                                  search=search, limit=limit, offset=offset)
    stats = get_candidate_stats(conn)
    return {"candidates": candidates, "stats": stats}

@app.post("/api/discover/search")
def api_discover_search(req: DiscoverSearchRequest):
    from job_discovery import search_jobs, score_unscored_candidates
    conn = get_conn()
    if not req.search_term:
        raise HTTPException(400, "search_term is required")
    candidates = search_jobs(
        conn, req.search_term, req.location,
        results_wanted=req.results_wanted, sources=req.sources,
    )
    # Score in background — must open own DB connection in new thread
    import threading
    from job_db import init_db
    db_path = PROJECT_ROOT / "jobs.db"
    threading.Thread(
        target=lambda: score_unscored_candidates(
            init_db(db_path, check_same_thread=False)),
        daemon=True,
    ).start()
    return {"added": len(candidates), "search_term": req.search_term}

@app.post("/api/discover/candidates/{candidate_id}/promote")
def api_promote_candidate(candidate_id: int):
    from job_discovery import promote_candidate
    conn = get_conn()
    job_id = promote_candidate(conn, candidate_id)
    if job_id is None:
        return {"status": "already_promoted"}
    return {"status": "promoted", "job_id": job_id}

@app.post("/api/discover/candidates/{candidate_id}/skip")
def api_skip_candidate(candidate_id: int):
    from job_discovery import skip_candidate
    conn = get_conn()
    skip_candidate(conn, candidate_id)
    return {"status": "skipped"}

@app.post("/api/discover/candidates/{candidate_id}/unskip")
def api_unskip_candidate(candidate_id: int):
    """Restore a skipped candidate back to scored/new status."""
    conn = get_conn()
    row = conn.execute("SELECT score FROM candidate_jobs WHERE id = ?", (candidate_id,)).fetchone()
    new_status = "scored" if row and row["score"] is not None else "new"
    conn.execute("UPDATE candidate_jobs SET status = ? WHERE id = ?", (new_status, candidate_id))
    conn.commit()
    return {"status": new_status}

@app.delete("/api/discover/candidates/{candidate_id}")
def api_delete_candidate(candidate_id: int):
    from job_discovery import delete_candidate
    conn = get_conn()
    delete_candidate(conn, candidate_id)
    return {"status": "deleted"}

@app.post("/api/discover/candidates/promote-bulk")
def api_promote_bulk(req: PromoteBulkRequest):
    from job_discovery import promote_candidate
    conn = get_conn()
    promoted = []
    for cid in req.ids:
        job_id = promote_candidate(conn, cid)
        if job_id is not None:
            promoted.append({"candidate_id": cid, "job_id": job_id})
    return {"promoted": promoted, "count": len(promoted)}

@app.post("/api/discover/candidates/{candidate_id}/score")
def api_score_candidate(candidate_id: int):
    from job_discovery import score_candidate
    conn = get_conn()
    result = score_candidate(conn, candidate_id)
    return result
```

- [ ] **Step 6: Lint**

Run: `uv run ruff check scripts/job_web.py scripts/job_discovery.py`
Expected: All checks passed

- [ ] **Step 7: Commit**

```bash
git add scripts/job_web.py scripts/job_discovery.py tests/test_job_discovery.py
git commit -m "feat: add discovery API routes (search, list, score, promote, skip)"
```

---

### Task 7: Add Discover page to frontend

**Files:**
- Modify: `scripts/static/index.html`
- Modify: `scripts/static/app.js`
- Modify: `scripts/static/style.css`

- [ ] **Step 1: Add nav link and view section to index.html**

Add nav link after existing nav items:
```html
<a href="#discover" class="nav-link">Discover</a>
```

Add view section (after `view-settings`):
```html
<section id="view-discover" style="display:none">
  <div class="section-header">
    <h2>Discover Jobs</h2>
  </div>

  <!-- Search Form -->
  <div class="discover-search-form">
    <input type="text" id="discover-search-term" placeholder="Job title, keywords..."
           class="form-input" style="flex:2">
    <input type="text" id="discover-location" placeholder="Location (e.g. San Francisco, CA)"
           class="form-input" value="San Francisco, CA" style="flex:1">
    <select id="discover-sources" class="form-select" multiple>
      <option value="linkedin" selected>LinkedIn</option>
      <option value="indeed" selected>Indeed</option>
      <option value="glassdoor">Glassdoor</option>
    </select>
    <button id="discover-search-btn" class="btn btn-primary">Search</button>
  </div>

  <!-- Filter bar -->
  <div class="discover-filter-bar">
    <span class="filter-badge active" data-filter="all">All <span id="discover-count-all">0</span></span>
    <span class="filter-badge" data-filter="new">New <span id="discover-count-new">0</span></span>
    <span class="filter-badge" data-filter="scored">Scored <span id="discover-count-scored">0</span></span>
    <span class="filter-badge" data-filter="promoted">Drafted <span id="discover-count-promoted">0</span></span>
    <span class="filter-badge" data-filter="skipped">Skipped <span id="discover-count-skipped">0</span></span>
    <input type="text" id="discover-search-filter" placeholder="Filter results..." class="form-input" style="width:200px">
  </div>

  <!-- Bulk actions -->
  <div id="discover-bulk-bar" class="bulk-bar" style="display:none">
    <span id="discover-selected-count">0</span> selected
    <button id="discover-draft-selected" class="btn btn-primary">Draft Selected</button>
    <button id="discover-skip-selected" class="btn btn-secondary">Skip Selected</button>
  </div>

  <!-- Results table -->
  <div class="table-wrapper">
    <table id="discover-table" class="data-table">
      <thead>
        <tr>
          <th><input type="checkbox" id="discover-select-all"></th>
          <th>Score</th>
          <th>Company</th>
          <th>Role</th>
          <th>Location</th>
          <th>Salary</th>
          <th>Source</th>
          <th>Posted</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="discover-tbody"></tbody>
    </table>
  </div>
  <div id="discover-empty" style="display:none; text-align:center; padding:3rem; color:var(--base01)">
    Search for jobs to get started.
  </div>
</section>
```

- [ ] **Step 2: Add discover rendering and event handling to app.js**

Add to `app.js` (append at end, before the closing of DOMContentLoaded):

```javascript
// --- Discover Page ---
let discoverFilter = 'all';
let discoverCandidates = [];

let _discoverBound = false;
async function renderDiscover() {
  await fetchCandidates();
  renderCandidateTable();
  if (!_discoverBound) { bindDiscoverEvents(); _discoverBound = true; }
}

async function fetchCandidates() {
  const params = new URLSearchParams();
  if (discoverFilter !== 'all') params.set('status', discoverFilter);
  const searchFilter = document.getElementById('discover-search-filter')?.value || '';
  if (searchFilter) params.set('search', searchFilter);
  try {
    const res = await fetch(`/api/discover/candidates?${params}`);
    const data = await res.json();
    discoverCandidates = data.candidates || [];
    // Update counts
    const stats = data.stats || {};
    const total = Object.values(stats).reduce((a, b) => a + b, 0);
    setText('discover-count-all', total);
    setText('discover-count-new', stats.new || 0);
    setText('discover-count-scored', stats.scored || 0);
    setText('discover-count-promoted', stats.promoted || 0);
    setText('discover-count-skipped', stats.skipped || 0);
  } catch (e) {
    console.error('Failed to fetch candidates:', e);
  }
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function renderCandidateTable() {
  const tbody = document.getElementById('discover-tbody');
  const empty = document.getElementById('discover-empty');
  if (!discoverCandidates.length) {
    tbody.innerHTML = '';
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';
  tbody.innerHTML = discoverCandidates.map(c => {
    const scoreClass = c.score >= 75 ? 'score-high' : c.score >= 50 ? 'score-mid' : c.score !== null ? 'score-low' : 'score-none';
    const scoreText = c.score !== null ? c.score : '-';
    const sourceClass = `source-${c.source}`;
    const posted = c.date_posted ? new Date(c.date_posted).toLocaleDateString() : '-';
    const isPromoted = c.status === 'promoted';
    const isSkipped = c.status === 'skipped';
    return `<tr class="${isSkipped ? 'row-dimmed' : ''}" data-id="${c.id}">
      <td><input type="checkbox" class="discover-cb" data-id="${c.id}" ${isPromoted ? 'disabled' : ''}></td>
      <td><span class="score-badge ${scoreClass}" title="${escapeHtml(c.score_reason || '')}">${scoreText}</span></td>
      <td>${escapeHtml(c.company)}</td>
      <td><a href="${escapeHtml(c.job_url)}" target="_blank" rel="noopener">${escapeHtml(c.title)}</a></td>
      <td>${escapeHtml(c.location || '-')}</td>
      <td>${escapeHtml(c.salary || '-')}</td>
      <td><span class="badge-pill ${sourceClass}">${escapeHtml(c.source)}</span></td>
      <td>${posted}</td>
      <td>
        ${isPromoted ? '<span class="badge-pill status-submitted">Drafted</span>' :
          isSkipped ? '<button class="btn btn-xs" onclick="unskipCandidate(' + c.id + ')">Restore</button>' :
          '<button class="btn btn-xs btn-primary" onclick="draftCandidate(' + c.id + ')">Draft</button> ' +
          '<button class="btn btn-xs" onclick="skipCandidate(' + c.id + ')">Skip</button>'}
      </td>
    </tr>`;
  }).join('');
}

function bindDiscoverEvents() {
  // Search button
  document.getElementById('discover-search-btn')?.addEventListener('click', async () => {
    const term = document.getElementById('discover-search-term')?.value;
    const location = document.getElementById('discover-location')?.value || 'San Francisco, CA';
    if (!term) { showToast('Enter a search term', 'warning'); return; }
    const btn = document.getElementById('discover-search-btn');
    btn.disabled = true; btn.textContent = 'Searching...';
    try {
      const res = await fetch('/api/discover/search', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        const sources = Array.from(document.querySelectorAll('#discover-sources option:checked')).map(o => o.value);
        body: JSON.stringify({ search_term: term, location, sources }),
      });
      const data = await res.json();
      showToast(`Found ${data.added} new jobs. Scoring in background...`, 'success');
      // Poll for scores
      setTimeout(() => { fetchCandidates(); renderCandidateTable(); }, 2000);
      setTimeout(() => { fetchCandidates(); renderCandidateTable(); }, 8000);
      setTimeout(() => { fetchCandidates(); renderCandidateTable(); }, 20000);
    } catch (e) {
      showToast('Search failed: ' + e.message, 'error');
    } finally {
      btn.disabled = false; btn.textContent = 'Search';
    }
    await fetchCandidates();
    renderCandidateTable();
  });

  // Filter badges
  document.querySelectorAll('.discover-filter-bar .filter-badge').forEach(badge => {
    badge.addEventListener('click', async () => {
      document.querySelectorAll('.discover-filter-bar .filter-badge').forEach(b => b.classList.remove('active'));
      badge.classList.add('active');
      discoverFilter = badge.dataset.filter;
      await fetchCandidates();
      renderCandidateTable();
    });
  });

  // Search filter
  let filterTimeout;
  document.getElementById('discover-search-filter')?.addEventListener('input', () => {
    clearTimeout(filterTimeout);
    filterTimeout = setTimeout(async () => {
      await fetchCandidates();
      renderCandidateTable();
    }, 300);
  });

  // Select all checkbox
  document.getElementById('discover-select-all')?.addEventListener('change', (e) => {
    document.querySelectorAll('.discover-cb:not(:disabled)').forEach(cb => cb.checked = e.target.checked);
    updateDiscoverBulkBar();
  });

  // Individual checkboxes (delegated)
  document.getElementById('discover-tbody')?.addEventListener('change', updateDiscoverBulkBar);

  // Bulk draft
  document.getElementById('discover-draft-selected')?.addEventListener('click', async () => {
    const ids = getSelectedCandidateIds();
    if (!ids.length) return;
    const res = await fetch('/api/discover/candidates/promote-bulk', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ids }),
    });
    const data = await res.json();
    showToast(`Drafted ${data.count} jobs. Workers will process them.`, 'success');
    await fetchCandidates();
    renderCandidateTable();
  });

  // Bulk skip
  document.getElementById('discover-skip-selected')?.addEventListener('click', async () => {
    const ids = getSelectedCandidateIds();
    for (const id of ids) await fetch(`/api/discover/candidates/${id}/skip`, { method: 'POST' });
    showToast(`Skipped ${ids.length} jobs.`, 'success');
    await fetchCandidates();
    renderCandidateTable();
  });
}

function getSelectedCandidateIds() {
  return Array.from(document.querySelectorAll('.discover-cb:checked')).map(cb => parseInt(cb.dataset.id));
}

function updateDiscoverBulkBar() {
  const count = getSelectedCandidateIds().length;
  const bar = document.getElementById('discover-bulk-bar');
  setText('discover-selected-count', count);
  bar.style.display = count > 0 ? 'flex' : 'none';
}

async function draftCandidate(id) {
  const res = await fetch(`/api/discover/candidates/${id}/promote`, { method: 'POST' });
  const data = await res.json();
  if (data.status === 'promoted') {
    showToast(`Job #${data.job_id} added to draft queue.`, 'success');
  } else {
    showToast('Already drafted.', 'warning');
  }
  await fetchCandidates();
  renderCandidateTable();
}

async function skipCandidate(id) {
  await fetch(`/api/discover/candidates/${id}/skip`, { method: 'POST' });
  await fetchCandidates();
  renderCandidateTable();
}

async function unskipCandidate(id) {
  await fetch(`/api/discover/candidates/${id}/unskip`, { method: 'POST' });
  await fetchCandidates();
  renderCandidateTable();
}
```

Also add the `discover` case to the existing `navigate()` function:

```javascript
// In navigate() add alongside other if statements:
if (hash === 'discover') renderDiscover();
```

- [ ] **Step 3: Add CSS styles**

Add to `scripts/static/style.css`:

```css
/* Discover page */
.discover-search-form {
  display: flex; gap: 0.5rem; margin-bottom: 1rem; align-items: center; flex-wrap: wrap;
}
.discover-filter-bar {
  display: flex; gap: 0.5rem; margin-bottom: 1rem; align-items: center; flex-wrap: wrap;
}
.score-badge {
  display: inline-block; min-width: 2rem; text-align: center; padding: 0.2rem 0.5rem;
  border-radius: 0.75rem; font-weight: 600; font-size: 0.85rem;
}
.score-high { background: var(--green); color: var(--base3); }
.score-mid { background: var(--yellow); color: var(--base03); }
.score-low { background: var(--red); color: var(--base3); }
.score-none { background: var(--base2); color: var(--base01); }
.source-linkedin { background: #0077b5; color: white; }
.source-indeed { background: #003366; color: white; }
.source-glassdoor { background: #0caa41; color: white; }
.row-dimmed { opacity: 0.5; }
.btn-xs { font-size: 0.75rem; padding: 0.15rem 0.5rem; }
```

- [ ] **Step 4: Verify the page renders**

Start the web server and navigate to `http://127.0.0.1:8420/#discover`. Verify:
- Search form appears with text input, location, source selector, and Search button
- Filter badges show (All, New, Scored, Drafted, Skipped)
- Empty state message shows "Search for jobs to get started."

- [ ] **Step 5: Commit**

```bash
git add scripts/static/index.html scripts/static/app.js scripts/static/style.css
git commit -m "feat: add Discover page with search, score, and draft UI"
```

---

### Task 8: Integration test — end-to-end flow

**Files:**
- Test: `tests/test_job_discovery.py`

- [ ] **Step 1: Write end-to-end test**

```python
class TestEndToEnd:
    def test_full_flow_search_score_promote(self, db):
        """Search → Score → Promote → Verify job in queue."""
        from job_discovery import search_jobs, score_candidate, promote_candidate, list_candidates
        import pandas as pd

        # 1. Search (mocked)
        mock_df = pd.DataFrame([{
            "site": "linkedin", "title": "Staff PM", "company": "CoolStartup",
            "job_url": "https://linkedin.com/jobs/e2e-test",
            "location": "San Francisco", "description": "AI product manager",
        }])
        with patch("job_discovery.scrape_jobs", return_value=mock_df):
            results = search_jobs(db, "product manager", "San Francisco", 10)
        assert len(results) == 1
        cid = results[0]["id"]

        # 2. Score
        mock_score = '{"score": 92, "reason": "Excellent PM+AI fit"}'
        with patch("job_discovery._call_llm_score", return_value=mock_score):
            score = score_candidate(db, cid)
        assert score["score"] == 92

        # 3. List (should show scored)
        candidates = list_candidates(db, status="scored")
        assert len(candidates) == 1
        assert candidates[0]["score"] == 92

        # 4. Promote
        job_id = promote_candidate(db, cid)
        assert job_id is not None

        # 5. Verify in queue
        job = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert job["status"] == "queued"

        # 6. Candidate marked as promoted
        candidate = db.execute("SELECT * FROM candidate_jobs WHERE id = ?", (cid,)).fetchone()
        assert candidate["status"] == "promoted"
        assert candidate["promoted_job_id"] == job_id
```

- [ ] **Step 2: Run full test suite**

Run: `uv run python -m pytest tests/test_job_discovery.py -v`
Expected: All tests PASS

- [ ] **Step 3: Run lint + architecture check**

Run: `uv run ruff check scripts/job_discovery.py scripts/job_web.py && uv run python scripts/check_architecture.py`
Expected: All checks passed

- [ ] **Step 4: Final commit**

```bash
git add tests/test_job_discovery.py
git commit -m "test: end-to-end discovery flow integration test"
```

---

## Summary

| Task | What it builds | Estimated effort |
|------|---------------|-----------------|
| 1 | jobspy dependency | 2 min |
| 2 | candidate_jobs table | 5 min |
| 3 | Job search via jobspy | 10 min |
| 4 | AI scoring via Claude | 10 min |
| 5 | Promotion to draft queue | 5 min |
| 6 | API routes | 10 min |
| 7 | Frontend Discover page | 15 min |
| 8 | Integration test | 5 min |

**Total: ~8 tasks, ~60 min of implementation**
