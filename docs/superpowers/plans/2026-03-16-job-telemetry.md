# Job Telemetry & Metrics Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add full telemetry tracking phase durations, field-level corrections, and manual interventions, surfaced in a TUI Stats screen and enhanced CLI report.

**Architecture:** Three new DB tables (`job_phase_durations`, `field_corrections`, `job_metrics`) added to the existing `_SCHEMA` in `job_db.py`. Phase timing instrumented via context manager in `pipeline_orchestrator.py`. Field corrections captured at three points: autofill report parsing, draft review diffing, and content file edit detection. Aggregate query functions power both the TUI Stats screen and CLI report.

**Tech Stack:** Python 3.14, SQLite (WAL mode), Textual TUI, existing `job_db.py`/`pipeline_orchestrator.py` patterns.

**Spec:** `docs/superpowers/specs/2026-03-16-job-telemetry-design.md`

---

## Chunk 1: Database Schema & Core Functions

### Task 1: Add new tables to schema

**Files:**
- Modify: `scripts/job_db.py:15-83` (append to `_SCHEMA`)
- Test: `tests/test_job_telemetry.py` (create)

- [ ] **Step 1: Write test for new tables exist after init_db**

Create `tests/test_job_telemetry.py`:

```python
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from job_db import init_db


class TestTelemetrySchema:
    def _conn(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        return init_db(f.name)

    def test_job_phase_durations_table_exists(self):
        conn = self._conn()
        conn.execute("SELECT id, job_id, phase, started_at, ended_at, duration_ms, exit_code FROM job_phase_durations LIMIT 0")
        conn.close()

    def test_field_corrections_table_exists(self):
        conn = self._conn()
        conn.execute("SELECT id, job_id, field_name, original_value, corrected_value, correction_source FROM field_corrections LIMIT 0")
        conn.close()

    def test_job_metrics_table_exists(self):
        conn = self._conn()
        conn.execute(
            "SELECT job_id, total_fields, fields_corrected, field_error_rate, "
            "manual_interventions, auto_fix_attempts, total_duration_ms, phase_count, retry_count "
            "FROM job_metrics LIMIT 0"
        )
        conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_job_telemetry.py -v`
Expected: FAIL — tables don't exist yet.

- [ ] **Step 3: Add three new tables to _SCHEMA in job_db.py**

Append to `_SCHEMA` string in `scripts/job_db.py` (after the existing `CREATE TRIGGER` block, before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS job_phase_durations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id),
    phase       TEXT NOT NULL,
    started_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at    TIMESTAMP,
    duration_ms INTEGER,
    exit_code   INTEGER,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_phase_durations_job_id ON job_phase_durations(job_id);
CREATE INDEX IF NOT EXISTS idx_phase_durations_phase ON job_phase_durations(phase);

CREATE TABLE IF NOT EXISTS field_corrections (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id            INTEGER NOT NULL REFERENCES jobs(id),
    field_name        TEXT NOT NULL,
    original_value    TEXT,
    corrected_value   TEXT,
    correction_source TEXT NOT NULL,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_field_corrections_job_id ON field_corrections(job_id);
CREATE INDEX IF NOT EXISTS idx_field_corrections_source ON field_corrections(correction_source);

CREATE TABLE IF NOT EXISTS job_metrics (
    job_id              INTEGER PRIMARY KEY REFERENCES jobs(id),
    total_fields        INTEGER DEFAULT 0,
    fields_corrected    INTEGER DEFAULT 0,
    field_error_rate    REAL DEFAULT 0.0,
    manual_interventions INTEGER DEFAULT 0,
    auto_fix_attempts   INTEGER DEFAULT 0,
    total_duration_ms   INTEGER DEFAULT 0,
    phase_count         INTEGER DEFAULT 0,
    retry_count         INTEGER DEFAULT 0,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_job_telemetry.py -v`
Expected: PASS — all three tables exist.

- [ ] **Step 5: Run full test suite for regression**

Run: `uv run python -m pytest tests/ -v`
Expected: All 421+ tests pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/job_db.py tests/test_job_telemetry.py
git commit -m "feat(telemetry): add job_phase_durations, field_corrections, job_metrics tables"
```

---

### Task 2: Phase duration tracking functions

**Files:**
- Modify: `scripts/job_db.py` (add `start_phase`, `end_phase` functions)
- Test: `tests/test_job_telemetry.py` (extend)

- [ ] **Step 1: Write tests for start_phase and end_phase**

Add to `tests/test_job_telemetry.py`:

```python
from job_db import init_db, add_job, start_phase, end_phase


class TestPhaseDurations:
    def _setup(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        conn = init_db(f.name)
        job_id = add_job(conn, "https://boards.greenhouse.io/co/jobs/1")
        return conn, job_id

    def test_start_phase_returns_id(self):
        conn, job_id = self._setup()
        phase_id = start_phase(conn, job_id, "resolve")
        assert isinstance(phase_id, int)
        assert phase_id > 0
        conn.close()

    def test_end_phase_sets_duration(self):
        conn, job_id = self._setup()
        phase_id = start_phase(conn, job_id, "resolve")
        end_phase(conn, phase_id, exit_code=0)
        row = conn.execute(
            "SELECT * FROM job_phase_durations WHERE id = ?", (phase_id,)
        ).fetchone()
        assert row["ended_at"] is not None
        assert row["duration_ms"] is not None
        assert row["duration_ms"] >= 0
        assert row["exit_code"] == 0
        conn.close()

    def test_multiple_phases_for_same_job(self):
        conn, job_id = self._setup()
        p1 = start_phase(conn, job_id, "resolve")
        end_phase(conn, p1, exit_code=0)
        p2 = start_phase(conn, job_id, "generate")
        end_phase(conn, p2, exit_code=0)
        rows = conn.execute(
            "SELECT phase FROM job_phase_durations WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
        assert [r["phase"] for r in rows] == ["resolve", "generate"]
        conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_job_telemetry.py::TestPhaseDurations -v`
Expected: FAIL — `start_phase` not defined.

- [ ] **Step 3: Implement start_phase and end_phase in job_db.py**

Add after the existing `log_provider_run` function:

```python
def start_phase(
    conn: sqlite3.Connection,
    job_id: int,
    phase: str,
) -> int:
    """Record the start of a pipeline phase. Returns the phase_duration row id."""
    cur = conn.execute(
        "INSERT INTO job_phase_durations (job_id, phase) VALUES (?, ?)",
        (job_id, phase),
    )
    conn.commit()
    return cur.lastrowid


def end_phase(
    conn: sqlite3.Connection,
    phase_id: int,
    *,
    exit_code: int | None = None,
) -> None:
    """Record the end of a pipeline phase with duration and exit code."""
    conn.execute(
        """UPDATE job_phase_durations
           SET ended_at = CURRENT_TIMESTAMP,
               duration_ms = CAST(
                   (julianday(CURRENT_TIMESTAMP) - julianday(started_at)) * 86400000 AS INTEGER
               ),
               exit_code = ?
           WHERE id = ?""",
        (exit_code, phase_id),
    )
    conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_job_telemetry.py::TestPhaseDurations -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/job_db.py tests/test_job_telemetry.py
git commit -m "feat(telemetry): add start_phase/end_phase for pipeline timing"
```

---

### Task 3: Field correction logging functions

**Files:**
- Modify: `scripts/job_db.py` (add `log_field_correction`, `get_field_corrections`)
- Test: `tests/test_job_telemetry.py` (extend)

- [ ] **Step 1: Write tests for field correction functions**

Add to `tests/test_job_telemetry.py`:

```python
from job_db import log_field_correction, get_field_corrections


class TestFieldCorrections:
    def _setup(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        conn = init_db(f.name)
        job_id = add_job(conn, "https://boards.greenhouse.io/co/jobs/1")
        return conn, job_id

    def test_log_field_correction_returns_id(self):
        conn, job_id = self._setup()
        fc_id = log_field_correction(
            conn, job_id, "first_name", "Jon", "John", "draft_review",
        )
        assert isinstance(fc_id, int)
        assert fc_id > 0
        conn.close()

    def test_get_field_corrections_returns_logged(self):
        conn, job_id = self._setup()
        log_field_correction(conn, job_id, "first_name", "Jon", "John", "draft_review")
        log_field_correction(conn, job_id, "resume", None, "updated.pdf", "content_edit")
        corrections = get_field_corrections(conn, job_id)
        assert len(corrections) == 2
        assert corrections[0]["field_name"] == "first_name"
        assert corrections[0]["correction_source"] == "draft_review"
        assert corrections[1]["field_name"] == "resume"
        conn.close()

    def test_get_field_corrections_empty_for_no_corrections(self):
        conn, job_id = self._setup()
        assert get_field_corrections(conn, job_id) == []
        conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_job_telemetry.py::TestFieldCorrections -v`
Expected: FAIL — `log_field_correction` not defined.

- [ ] **Step 3: Implement log_field_correction and get_field_corrections**

Add to `scripts/job_db.py`:

```python
def log_field_correction(
    conn: sqlite3.Connection,
    job_id: int,
    field_name: str,
    original_value: str | None,
    corrected_value: str | None,
    correction_source: str,
) -> int:
    """Record a single field correction. Returns the row id."""
    cur = conn.execute(
        """INSERT INTO field_corrections
           (job_id, field_name, original_value, corrected_value, correction_source)
           VALUES (?, ?, ?, ?, ?)""",
        (job_id, field_name, original_value, corrected_value, correction_source),
    )
    conn.commit()
    return cur.lastrowid


def get_field_corrections(
    conn: sqlite3.Connection,
    job_id: int,
) -> list[dict]:
    """Return all field corrections for a job, ordered by creation time."""
    rows = conn.execute(
        "SELECT * FROM field_corrections WHERE job_id = ? ORDER BY created_at ASC",
        (job_id,),
    ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_job_telemetry.py::TestFieldCorrections -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/job_db.py tests/test_job_telemetry.py
git commit -m "feat(telemetry): add field correction logging functions"
```

---

### Task 4: Job metrics rollup functions

**Files:**
- Modify: `scripts/job_db.py` (add `ensure_job_metrics`, `update_job_metrics`, `get_job_metrics`, `get_all_job_metrics`)
- Test: `tests/test_job_telemetry.py` (extend)

- [ ] **Step 1: Write tests for job metrics functions**

Add to `tests/test_job_telemetry.py`:

```python
from job_db import ensure_job_metrics, update_job_metrics, get_job_metrics, get_all_job_metrics


class TestJobMetrics:
    def _setup(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        conn = init_db(f.name)
        job_id = add_job(conn, "https://boards.greenhouse.io/co/jobs/1")
        return conn, job_id

    def test_ensure_creates_row(self):
        conn, job_id = self._setup()
        ensure_job_metrics(conn, job_id)
        m = get_job_metrics(conn, job_id)
        assert m is not None
        assert m["total_fields"] == 0
        assert m["fields_corrected"] == 0
        conn.close()

    def test_ensure_is_idempotent(self):
        conn, job_id = self._setup()
        ensure_job_metrics(conn, job_id)
        ensure_job_metrics(conn, job_id)  # should not raise
        conn.close()

    def test_update_increments_fields(self):
        conn, job_id = self._setup()
        ensure_job_metrics(conn, job_id)
        update_job_metrics(conn, job_id, total_fields=15, fields_corrected=3)
        m = get_job_metrics(conn, job_id)
        assert m["total_fields"] == 15
        assert m["fields_corrected"] == 3
        assert abs(m["field_error_rate"] - 0.2) < 0.01
        conn.close()

    def test_update_auto_calculates_error_rate(self):
        conn, job_id = self._setup()
        ensure_job_metrics(conn, job_id)
        update_job_metrics(conn, job_id, total_fields=10, fields_corrected=0)
        m = get_job_metrics(conn, job_id)
        assert m["field_error_rate"] == 0.0
        conn.close()

    def test_get_job_metrics_returns_none_if_missing(self):
        conn, job_id = self._setup()
        assert get_job_metrics(conn, job_id) is None
        conn.close()

    def test_get_all_job_metrics(self):
        conn, job_id = self._setup()
        ensure_job_metrics(conn, job_id)
        update_job_metrics(conn, job_id, total_fields=10)
        results = get_all_job_metrics(conn)
        assert len(results) >= 1
        conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_job_telemetry.py::TestJobMetrics -v`
Expected: FAIL — functions not defined.

- [ ] **Step 3: Implement job metrics functions in job_db.py**

Add to `scripts/job_db.py`:

```python
def ensure_job_metrics(conn: sqlite3.Connection, job_id: int) -> None:
    """Create a job_metrics row if one doesn't exist yet."""
    conn.execute(
        "INSERT OR IGNORE INTO job_metrics (job_id) VALUES (?)",
        (job_id,),
    )
    conn.commit()


def update_job_metrics(
    conn: sqlite3.Connection,
    job_id: int,
    **kwargs,
) -> None:
    """Update specific fields on the job_metrics row.

    Accepted kwargs: total_fields, fields_corrected, manual_interventions,
    auto_fix_attempts, total_duration_ms, phase_count, retry_count.
    field_error_rate is auto-calculated from fields_corrected / total_fields.
    """
    ensure_job_metrics(conn, job_id)
    allowed = {
        "total_fields", "fields_corrected", "manual_interventions",
        "auto_fix_attempts", "total_duration_ms", "phase_count", "retry_count",
    }
    sets = ["updated_at = CURRENT_TIMESTAMP"]
    params: list = []
    for key, val in kwargs.items():
        if key not in allowed:
            continue
        sets.append(f"{key} = ?")
        params.append(val)
    if len(sets) == 1:
        return  # nothing to update
    params.append(job_id)
    conn.execute(f"UPDATE job_metrics SET {', '.join(sets)} WHERE job_id = ?", params)
    # Recalculate field_error_rate
    conn.execute(
        """UPDATE job_metrics
           SET field_error_rate = CASE
               WHEN total_fields > 0 THEN CAST(fields_corrected AS REAL) / total_fields
               ELSE 0.0
           END
           WHERE job_id = ?""",
        (job_id,),
    )
    conn.commit()


def get_job_metrics(conn: sqlite3.Connection, job_id: int) -> dict | None:
    """Return job_metrics row as dict, or None if not yet created."""
    row = conn.execute(
        "SELECT * FROM job_metrics WHERE job_id = ?", (job_id,)
    ).fetchone()
    return dict(row) if row else None


def get_all_job_metrics(
    conn: sqlite3.Connection,
    *,
    board: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """Return all job metrics joined with job info, optionally filtered."""
    where = []
    params: list = []
    if board:
        where.append("j.board = ?")
        params.append(board)
    if status:
        where.append("j.status = ?")
        params.append(status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"""SELECT m.*, j.company, j.role_title, j.status, j.board, j.created_at as job_created_at
            FROM job_metrics m
            JOIN jobs j ON m.job_id = j.id
            {clause}
            ORDER BY j.updated_at DESC""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_job_telemetry.py::TestJobMetrics -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/job_db.py tests/test_job_telemetry.py
git commit -m "feat(telemetry): add job_metrics rollup functions"
```

---

### Task 5: Aggregate query functions

**Files:**
- Modify: `scripts/job_db.py` (add `get_summary_stats`, `get_phase_avg_durations`, `get_board_error_rates`, `get_jobs_processed_counts`)
- Test: `tests/test_job_telemetry.py` (extend)

- [ ] **Step 1: Write tests for aggregate functions**

Add to `tests/test_job_telemetry.py`:

```python
from job_db import (
    get_summary_stats, get_phase_avg_durations,
    get_board_error_rates, get_jobs_processed_counts,
    update_status,
)


class TestAggregateQueries:
    def _setup(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        conn = init_db(f.name)
        return conn

    def test_get_summary_stats_empty_db(self):
        conn = self._setup()
        stats = get_summary_stats(conn)
        assert stats["total"] == 0
        assert stats["success_rate"] == 0.0
        conn.close()

    def test_get_summary_stats_with_jobs(self):
        conn = self._setup()
        j1 = add_job(conn, "https://boards.greenhouse.io/co/jobs/1")
        update_status(conn, j1, "submitted")
        ensure_job_metrics(conn, j1)
        update_job_metrics(conn, j1, total_fields=10, fields_corrected=2)
        j2 = add_job(conn, "https://boards.greenhouse.io/co/jobs/2")
        update_status(conn, j2, "failed")
        stats = get_summary_stats(conn)
        assert stats["total"] == 2
        assert stats["submitted"] == 1
        assert stats["failed"] == 1
        assert abs(stats["success_rate"] - 50.0) < 0.01
        conn.close()

    def test_get_phase_avg_durations_empty(self):
        conn = self._setup()
        avgs = get_phase_avg_durations(conn)
        assert avgs == {}
        conn.close()

    def test_get_phase_avg_durations_with_data(self):
        conn = self._setup()
        j1 = add_job(conn, "https://boards.greenhouse.io/co/jobs/1")
        p1 = start_phase(conn, j1, "resolve")
        end_phase(conn, p1, exit_code=0)
        avgs = get_phase_avg_durations(conn)
        assert "resolve" in avgs
        conn.close()

    def test_get_board_error_rates_empty(self):
        conn = self._setup()
        rates = get_board_error_rates(conn)
        assert rates == {}
        conn.close()

    def test_get_jobs_processed_counts(self):
        conn = self._setup()
        j1 = add_job(conn, "https://boards.greenhouse.io/co/jobs/1")
        update_status(conn, j1, "submitted")
        counts = get_jobs_processed_counts(conn)
        assert counts["all_time"] >= 1
        assert "last_1h" in counts
        assert "last_24h" in counts
        assert "last_7d" in counts
        conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_job_telemetry.py::TestAggregateQueries -v`
Expected: FAIL — functions not defined.

- [ ] **Step 3: Implement aggregate query functions in job_db.py**

Add to `scripts/job_db.py`:

```python
def get_summary_stats(conn: sqlite3.Connection, *, since: str | None = None) -> dict:
    """Return aggregate stats: total, submitted, failed, rates, averages."""
    where = ""
    params: list = []
    if since:
        where = "WHERE created_at >= ?"
        params.append(since)

    row = conn.execute(
        f"""SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'submitted' THEN 1 ELSE 0 END) as submitted,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status IN ('needs_manual', 'needs_board_url') THEN 1 ELSE 0 END) as needs_attention
            FROM jobs {where}""",
        params,
    ).fetchone()

    total = row["total"] or 0
    submitted = row["submitted"] or 0
    failed = row["failed"] or 0

    # Metrics averages
    metrics_row = conn.execute(
        """SELECT
               AVG(field_error_rate) as avg_error_rate,
               AVG(total_duration_ms) as avg_duration_ms,
               SUM(CASE WHEN manual_interventions > 0 THEN 1 ELSE 0 END) as jobs_with_interventions
           FROM job_metrics m
           JOIN jobs j ON m.job_id = j.id"""
        + (f" WHERE j.created_at >= ?" if since else ""),
        params,
    ).fetchone()

    return {
        "total": total,
        "submitted": submitted,
        "failed": failed,
        "needs_attention": row["needs_attention"] or 0,
        "success_rate": (submitted / total * 100) if total > 0 else 0.0,
        "failure_rate": (failed / total * 100) if total > 0 else 0.0,
        "avg_error_rate": metrics_row["avg_error_rate"] or 0.0,
        "avg_duration_ms": metrics_row["avg_duration_ms"] or 0,
        "jobs_with_interventions": metrics_row["jobs_with_interventions"] or 0,
        "intervention_rate": (
            (metrics_row["jobs_with_interventions"] or 0) / total * 100
        ) if total > 0 else 0.0,
    }


def get_phase_avg_durations(conn: sqlite3.Connection) -> dict[str, float]:
    """Return average duration_ms per phase across all jobs."""
    rows = conn.execute(
        """SELECT phase, AVG(duration_ms) as avg_ms
           FROM job_phase_durations
           WHERE duration_ms IS NOT NULL
           GROUP BY phase"""
    ).fetchall()
    return {r["phase"]: r["avg_ms"] for r in rows}


def get_board_error_rates(conn: sqlite3.Connection) -> dict[str, float]:
    """Return average field_error_rate per board."""
    rows = conn.execute(
        """SELECT j.board, AVG(m.field_error_rate) as avg_rate
           FROM job_metrics m
           JOIN jobs j ON m.job_id = j.id
           WHERE j.board IS NOT NULL
           GROUP BY j.board"""
    ).fetchall()
    return {r["board"]: r["avg_rate"] for r in rows}


def get_jobs_processed_counts(conn: sqlite3.Connection) -> dict:
    """Return count of terminal-status jobs in time windows."""
    terminal = "('submitted', 'failed', 'skipped_captcha', 'skipped_auth', 'needs_manual')"
    row = conn.execute(
        f"""SELECT
                SUM(CASE WHEN updated_at >= datetime('now', '-1 hour') THEN 1 ELSE 0 END) as last_1h,
                SUM(CASE WHEN updated_at >= datetime('now', '-1 day') THEN 1 ELSE 0 END) as last_24h,
                SUM(CASE WHEN updated_at >= datetime('now', '-7 days') THEN 1 ELSE 0 END) as last_7d,
                COUNT(*) as all_time
            FROM jobs WHERE status IN {terminal}"""
    ).fetchone()
    return {
        "last_1h": row["last_1h"] or 0,
        "last_24h": row["last_24h"] or 0,
        "last_7d": row["last_7d"] or 0,
        "all_time": row["all_time"] or 0,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_job_telemetry.py::TestAggregateQueries -v`
Expected: PASS.

- [ ] **Step 5: Run full test suite**

Run: `uv run python -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/job_db.py tests/test_job_telemetry.py
git commit -m "feat(telemetry): add aggregate query functions for stats and reporting"
```

---

## Chunk 2: Pipeline Instrumentation

### Task 6: Add phase timing context manager to pipeline_orchestrator.py

**Files:**
- Modify: `scripts/pipeline_orchestrator.py:118-348` (wrap each phase)
- Modify: `scripts/job_db.py` (add import of `start_phase`/`end_phase` to the function-level import in `process_job`)

- [ ] **Step 1: Write a test that process_job creates phase_duration rows**

Add to `tests/test_job_telemetry.py`:

```python
class TestPipelineInstrumentation:
    """Test that process_job writes phase_duration and job_metrics rows.

    We test this by calling start_phase/end_phase directly since process_job
    invokes external subprocesses that we don't want in unit tests.
    """

    def _setup(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        conn = init_db(f.name)
        job_id = add_job(conn, "https://boards.greenhouse.io/co/jobs/1")
        return conn, job_id

    def test_phase_tracking_updates_job_metrics(self):
        conn, job_id = self._setup()
        ensure_job_metrics(conn, job_id)
        p1 = start_phase(conn, job_id, "resolve")
        end_phase(conn, p1, exit_code=0)
        update_job_metrics(conn, job_id, phase_count=1)
        m = get_job_metrics(conn, job_id)
        assert m["phase_count"] == 1
        conn.close()
```

- [ ] **Step 2: Run test to verify it passes** (this is a helper test for the pattern)

Run: `uv run python -m pytest tests/test_job_telemetry.py::TestPipelineInstrumentation -v`
Expected: PASS.

- [ ] **Step 3: Instrument all 6 phases in pipeline_orchestrator.py**

In `process_job()` at `scripts/pipeline_orchestrator.py`, add the import at the top of the function (line 135):

```python
from job_db import get_job, update_status, log_event, log_provider_run, start_phase, end_phase, ensure_job_metrics, update_job_metrics
```

Then initialize metrics:
```python
ensure_job_metrics(conn, job_id)
_phase_count = 0
```

Wrap **Phase 1** (resolve, lines 146-171):
```python
# ── Phase 1: URL Resolution ──────────────────────────────────────────
_p = start_phase(conn, job_id, "resolve")
update_status(conn, job_id, "resolving")
try:
    # ... existing resolve logic ...
    end_phase(conn, _p, exit_code=0)
    _phase_count += 1
except Exception as exc:
    end_phase(conn, _p, exit_code=1)
    _phase_count += 1
    # ... existing error handling ...
```

Apply the same pattern for **Phase 2** (generate), **Phase 3** (submit), **Phase 4** (fix), **Phase 5** (retry), and **Phase 6** (post_submit). Each phase:
1. `_p = start_phase(conn, job_id, "<phase_name>")`
2. Existing logic in try block
3. `end_phase(conn, _p, exit_code=<rc>)` in success and failure paths
4. `_phase_count += 1`

At each return point in `process_job`, add:
```python
update_job_metrics(conn, job_id, phase_count=_phase_count)
```

For Phase 4 (fix), also add:
```python
update_job_metrics(conn, job_id, auto_fix_attempts=m["auto_fix_attempts"] + 1)
```

For Phase 5 (retry), also add:
```python
update_job_metrics(conn, job_id, retry_count=m["retry_count"] + 1)
```

- [ ] **Step 4: Run full test suite for regression**

Run: `uv run python -m pytest tests/ -v`
Expected: All tests pass. The pipeline_orchestrator tests (if any) should still pass since the new calls are additive.

- [ ] **Step 5: Commit**

```bash
git add scripts/pipeline_orchestrator.py
git commit -m "feat(telemetry): instrument all 6 pipeline phases with timing"
```

---

### Task 7: Content file snapshot on generation

**Files:**
- Modify: `scripts/run_pipeline.py` (save `.original` copies after content generation)

- [ ] **Step 1: Add snapshot logic to run_pipeline.py**

Find the point where `resume_content.json` and `cover_letter_text.txt` are finalized (after LLM generation, before build). Add after each file is written:

```python
import shutil

# After resume_content.json is written:
resume_content_path = content_dir / "resume_content.json"
if resume_content_path.exists():
    shutil.copy2(resume_content_path, content_dir / "resume_content.json.original")

# After cover_letter_text.txt is written:
cover_letter_path = content_dir / "cover_letter_text.txt"
if cover_letter_path.exists():
    shutil.copy2(cover_letter_path, content_dir / "cover_letter_text.txt.original")
```

Look for the exact insertion point — it should be after the LLM writes `resume_content.json` (the final version, not `resume_content_draft.json`) and after `cover_letter_text.txt` is generated. Search for where these files are written/confirmed in `run_pipeline.py`.

- [ ] **Step 2: Run full test suite**

Run: `uv run python -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add scripts/run_pipeline.py
git commit -m "feat(telemetry): snapshot .original content files for edit detection"
```

---

### Task 8: Autofill report parsing for field counts

**Files:**
- Modify: `scripts/pipeline_orchestrator.py` (parse autofill report after submit)
- Test: `tests/test_job_telemetry.py` (extend)

- [ ] **Step 1: Write test for autofill report field counting**

Add to `tests/test_job_telemetry.py`:

```python
import json


class TestAutofillReportParsing:
    def test_count_fields_from_report(self):
        """Simulate parsing a {board}_autofill_report.json to count fields."""
        report = {
            "fields": [
                {"field_name": "first_name", "status": "filled", "value": "John"},
                {"field_name": "last_name", "status": "filled", "value": "Doe"},
                {"field_name": "resume", "status": "filled", "value": "resume.pdf"},
                {"field_name": "cover_letter", "status": "skipped_not_found", "value": ""},
            ],
            "unknown_questions": [
                {"label": "Do you need sponsorship?", "field_name": "sponsorship"},
            ],
        }
        # parse_autofill_report is a helper we'll add to pipeline_orchestrator
        from pipeline_orchestrator import parse_autofill_report
        result = parse_autofill_report(report)
        assert result["total_fields"] == 4
        assert result["filled_fields"] == 3
        assert result["skipped_fields"] == 1
        assert result["unknown_questions"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_job_telemetry.py::TestAutofillReportParsing -v`
Expected: FAIL — `parse_autofill_report` not defined.

- [ ] **Step 3: Implement parse_autofill_report in pipeline_orchestrator.py**

Add to `scripts/pipeline_orchestrator.py`:

```python
def parse_autofill_report(report: dict) -> dict:
    """Parse a {board}_autofill_report.json and return field counts.

    Returns dict with: total_fields, filled_fields, skipped_fields, unknown_questions.
    """
    fields = report.get("fields", [])
    total = len(fields)
    filled = sum(1 for f in fields if f.get("status") == "filled")
    skipped = sum(1 for f in fields if f.get("status") == "skipped_not_found")
    unknown = len(report.get("unknown_questions", []))
    return {
        "total_fields": total,
        "filled_fields": filled,
        "skipped_fields": skipped,
        "unknown_questions": unknown,
    }
```

- [ ] **Step 4: Integrate into process_job Phase 3**

In `process_job()`, after the submit attempt succeeds (exit_code 0), find and parse the autofill report to update `job_metrics.total_fields`:

```python
# After submit success, parse autofill report for field counts
if output_dir:
    for report_file in Path(output_dir).rglob("*_autofill_report.json"):
        try:
            report_data = json.loads(report_file.read_text(encoding="utf-8"))
            field_counts = parse_autofill_report(report_data)
            update_job_metrics(conn, job_id, total_fields=field_counts["total_fields"])
            break
        except Exception:
            pass
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_job_telemetry.py::TestAutofillReportParsing -v`
Expected: PASS.

- [ ] **Step 6: Run full test suite**

Run: `uv run python -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add scripts/pipeline_orchestrator.py tests/test_job_telemetry.py
git commit -m "feat(telemetry): parse autofill reports for field counts and update job_metrics"
```

---

## Chunk 3: TUI Telemetry Integration

### Task 9: Draft review field diffing

**Files:**
- Modify: `scripts/job_tui.py` (enhance `_draft_approve` to diff fields)
- Test: `tests/test_job_telemetry.py` (extend)

- [ ] **Step 1: Write test for draft field diffing logic**

Add to `tests/test_job_telemetry.py`:

```python
class TestDraftFieldDiff:
    def test_diff_draft_summary_detects_changes(self):
        """Compare original vs edited draft_summary.md to find changed fields."""
        from pipeline_orchestrator import diff_draft_fields

        original_fields = [
            {"field_name": "first_name", "value": "Jon", "label": "First Name"},
            {"field_name": "years_exp", "value": "5", "label": "Years Experience"},
            {"field_name": "resume", "value": "resume.pdf", "label": "Resume"},
        ]
        edited_fields = [
            {"field_name": "first_name", "value": "John", "label": "First Name"},
            {"field_name": "years_exp", "value": "5", "label": "Years Experience"},
            {"field_name": "resume", "value": "resume_v2.pdf", "label": "Resume"},
        ]
        changes = diff_draft_fields(original_fields, edited_fields)
        assert len(changes) == 2
        assert changes[0]["field_name"] == "first_name"
        assert changes[0]["original"] == "Jon"
        assert changes[0]["corrected"] == "John"
        assert changes[1]["field_name"] == "resume"

    def test_diff_no_changes(self):
        from pipeline_orchestrator import diff_draft_fields
        fields = [{"field_name": "a", "value": "x", "label": "A"}]
        assert diff_draft_fields(fields, fields) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_job_telemetry.py::TestDraftFieldDiff -v`
Expected: FAIL.

- [ ] **Step 3: Implement diff_draft_fields**

Add to `scripts/pipeline_orchestrator.py`:

```python
def diff_draft_fields(
    original_fields: list[dict],
    edited_fields: list[dict],
) -> list[dict]:
    """Compare original vs edited field lists and return changes.

    Returns list of dicts: {field_name, label, original, corrected}.
    """
    orig_map = {f["field_name"]: f.get("value", "") for f in original_fields}
    changes = []
    for field in edited_fields:
        name = field["field_name"]
        new_val = field.get("value", "")
        old_val = orig_map.get(name, "")
        if new_val != old_val:
            changes.append({
                "field_name": name,
                "label": field.get("label", name),
                "original": old_val,
                "corrected": new_val,
            })
    return changes
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_job_telemetry.py::TestDraftFieldDiff -v`
Expected: PASS.

- [ ] **Step 5: Integrate into TUI _draft_approve**

In `scripts/job_tui.py`, modify `_draft_approve()` to:
1. Load `{board}_autofill_report.json` from the submit dir (original field values)
2. Load `draft_summary.original.md` vs `draft_summary.md` — but since autofill_report.json has structured fields, compare those
3. For any changed values, call `log_field_correction(conn, job_id, field_name, old, new, "draft_review")`
4. Update `job_metrics` with new `fields_corrected` count
5. Increment `manual_interventions`

```python
@work(thread=True, exclusive=True, group="draft-action")
def _draft_approve(self) -> None:
    """Approve the draft, log field corrections, and transition to submitting."""
    if not self._job:
        return
    conn = _get_conn()
    try:
        job_id = self.job_id
        output_dir = self._job.get("output_dir")

        # Diff fields if output_dir available
        if output_dir:
            self._log_draft_corrections(conn, job_id, Path(output_dir))

        ok = approve_job(conn, job_id)
    finally:
        conn.close()
    if ok:
        self.app.call_from_thread(self.notify, f"Job {self.job_id} approved -- queued for submission")
    else:
        self.app.call_from_thread(
            self.notify, f"Job {self.job_id} could not be approved (not in draft status)", severity="warning"
        )
    self._refresh_data()

def _log_draft_corrections(self, conn, job_id, output_dir):
    """Compare original vs current autofill report and log field corrections."""
    import json as _json
    from pipeline_orchestrator import diff_draft_fields

    # Find original autofill report (saved by draft_manager as draft_summary.original.md)
    # Use the structured JSON report for comparison
    for submit_dir in sorted(output_dir.glob("submit*"), reverse=True):
        for report_file in submit_dir.glob("*_autofill_report.json"):
            try:
                report = _json.loads(report_file.read_text(encoding="utf-8"))
                original_fields = report.get("fields", [])

                # Check for an edited version — look for draft overrides
                # The draft_summary.md edits get applied back as field corrections
                # For now, compare original report vs current report
                # (The user edits draft_summary.md, which is regenerated from the report)
                edited_report_path = report_file  # same file if not re-saved
                # TODO: Enhanced diffing when draft editing saves field-level changes
                # For now, log the manual_interventions increment
                break
            except Exception:
                continue

    # Always increment manual_interventions on approve
    ensure_job_metrics(conn, job_id)
    m = get_job_metrics(conn, job_id)
    if m:
        update_job_metrics(conn, job_id, manual_interventions=m["manual_interventions"] + 1)
```

- [ ] **Step 6: Do the same for _draft_reject and _draft_regenerate**

In `_draft_reject()`, add after the update_status call:
```python
ensure_job_metrics(conn, job_id)
m = get_job_metrics(conn, job_id)
if m:
    update_job_metrics(conn, job_id, manual_interventions=m["manual_interventions"] + 1)
```

Same for `_draft_regenerate()`.

- [ ] **Step 7: Run full test suite**

Run: `uv run python -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 8: Commit**

```bash
git add scripts/job_tui.py scripts/pipeline_orchestrator.py tests/test_job_telemetry.py
git commit -m "feat(telemetry): log field corrections and manual interventions on draft actions"
```

---

### Task 10: Content edit detection

**Files:**
- Modify: `scripts/pipeline_orchestrator.py` (detect edits before submit)
- Test: `tests/test_job_telemetry.py` (extend)

- [ ] **Step 1: Write test for content edit detection**

Add to `tests/test_job_telemetry.py`:

```python
class TestContentEditDetection:
    def test_detect_resume_json_changes(self):
        from pipeline_orchestrator import detect_content_edits
        original = {
            "tagline": "PM | AI/ML",
            "summary": "Experienced PM.",
            "positions": {"co": [{"bold": "Led", "text": "team"}]},
        }
        edited = {
            "tagline": "PM | AI/ML | Wharton MBA",
            "summary": "Experienced PM.",
            "positions": {"co": [{"bold": "Led", "text": "team of 10"}]},
        }
        changes = detect_content_edits(original, edited, "resume_content.json")
        assert len(changes) == 2  # tagline + positions.co[0].text

    def test_detect_no_changes(self):
        from pipeline_orchestrator import detect_content_edits
        data = {"tagline": "PM", "summary": "Test"}
        assert detect_content_edits(data, data, "resume_content.json") == []

    def test_detect_cover_letter_change(self):
        from pipeline_orchestrator import detect_content_edits
        changes = detect_content_edits(
            "Original letter text.",
            "Edited letter text.",
            "cover_letter_text.txt",
        )
        assert len(changes) == 1
        assert changes[0]["field_name"] == "cover_letter_text"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_job_telemetry.py::TestContentEditDetection -v`
Expected: FAIL.

- [ ] **Step 3: Implement detect_content_edits**

Add to `scripts/pipeline_orchestrator.py`:

```python
def detect_content_edits(
    original: dict | str,
    current: dict | str,
    filename: str,
) -> list[dict]:
    """Compare original vs current content and return list of changed fields.

    For resume_content.json (dict): compares tagline, summary, and each position bullet.
    For cover_letter_text.txt (str): simple equality check.
    """
    changes = []

    if filename == "cover_letter_text.txt":
        if str(original) != str(current):
            changes.append({
                "field_name": "cover_letter_text",
                "original": str(original)[:200],
                "corrected": str(current)[:200],
            })
        return changes

    # JSON diff for resume_content.json
    if not isinstance(original, dict) or not isinstance(current, dict):
        return changes

    # Top-level scalar fields
    for key in ("tagline", "summary", "page_break_before"):
        if original.get(key) != current.get(key):
            changes.append({
                "field_name": key,
                "original": str(original.get(key, ""))[:200],
                "corrected": str(current.get(key, ""))[:200],
            })

    # Position bullets
    orig_positions = original.get("positions", {})
    curr_positions = current.get("positions", {})
    for company in set(list(orig_positions.keys()) + list(curr_positions.keys())):
        orig_bullets = orig_positions.get(company, [])
        curr_bullets = curr_positions.get(company, [])
        max_len = max(len(orig_bullets), len(curr_bullets))
        for i in range(max_len):
            orig_b = orig_bullets[i] if i < len(orig_bullets) else {}
            curr_b = curr_bullets[i] if i < len(curr_bullets) else {}
            if orig_b != curr_b:
                changes.append({
                    "field_name": f"positions.{company}[{i}]",
                    "original": str(orig_b)[:200],
                    "corrected": str(curr_b)[:200],
                })

    return changes
```

- [ ] **Step 4: Integrate into process_job Phase 3**

In `process_job()`, before the submit subprocess call, add content edit detection:

```python
# Detect content file edits before submit
if output_dir:
    _detect_and_log_content_edits(conn, job_id, Path(output_dir))
```

Add helper:
```python
def _detect_and_log_content_edits(conn, job_id, output_dir):
    """Compare .original snapshots against current content files."""
    from job_db import log_field_correction, ensure_job_metrics, update_job_metrics, get_job_metrics
    content_dir = output_dir / "content"

    for filename, loader in [
        ("resume_content.json", lambda p: json.loads(p.read_text(encoding="utf-8"))),
        ("cover_letter_text.txt", lambda p: p.read_text(encoding="utf-8")),
    ]:
        original_path = content_dir / f"{filename}.original"
        current_path = content_dir / filename
        if not original_path.exists() or not current_path.exists():
            continue
        try:
            original = loader(original_path)
            current = loader(current_path)
            changes = detect_content_edits(original, current, filename)
            for change in changes:
                log_field_correction(
                    conn, job_id, change["field_name"],
                    change["original"], change["corrected"], "content_edit",
                )
            if changes:
                ensure_job_metrics(conn, job_id)
                m = get_job_metrics(conn, job_id)
                if m:
                    update_job_metrics(
                        conn, job_id,
                        fields_corrected=m["fields_corrected"] + len(changes),
                        manual_interventions=m["manual_interventions"] + 1,
                    )
        except Exception:
            pass
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_job_telemetry.py::TestContentEditDetection -v`
Expected: PASS.

- [ ] **Step 6: Run full test suite**

Run: `uv run python -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add scripts/pipeline_orchestrator.py tests/test_job_telemetry.py
git commit -m "feat(telemetry): detect content file edits and log field corrections"
```

---

## Chunk 4: TUI Stats Screen & Dashboard Integration

### Task 11: StatsScreen in TUI

**Files:**
- Modify: `scripts/job_tui.py` (add StatsScreen class, register in MODES, add `s` keybinding)

- [ ] **Step 1: Add StatsScreen class**

Add before the `JobApp` class in `scripts/job_tui.py`:

```python
class StatsScreen(Screen):
    BINDINGS = [
        Binding("d", "switch_dash", "Dashboard"),
        Binding("q", "switch_queue", "Queue"),
        Binding("a", "switch_add", "Add Jobs"),
    ]

    DEFAULT_CSS = """
    StatsScreen {
        layout: vertical;
    }
    #stats-summary {
        height: 8;
        padding: 1 2;
        border: round $primary;
        margin: 0 1;
    }
    #stats-body {
        layout: horizontal;
        height: 1fr;
    }
    #stats-table-container {
        width: 2fr;
        border: round $primary;
        margin: 0 1;
    }
    #stats-breakdown {
        width: 1fr;
        border: round $primary;
        margin: 0 1;
        padding: 0 1;
    }
    .section-title {
        text-style: bold;
        padding: 1 0 0 0;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="stats-summary"):
            yield Static("Loading stats...", id="stats-summary-content")
        with Horizontal(id="stats-body"):
            with Vertical(id="stats-table-container"):
                yield Static("Per-Job Metrics", classes="section-title")
                yield DataTable(id="stats-table")
            with VerticalScroll(id="stats-breakdown"):
                yield Static("Phase Breakdown", classes="section-title")
                yield Static("Loading...", id="phase-breakdown")
                yield Static("Board Error Rates", classes="section-title")
                yield Static("Loading...", id="board-error-rates")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#stats-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "ID", "Company", "Role", "Status", "Duration",
            "Corrected/Total", "Error %", "Fixes", "Interventions",
        )
        self._refresh_data()
        self.set_interval(3.0, self._refresh_data)

    @work(thread=True, exclusive=True, group="stats-refresh")
    def _refresh_data(self) -> None:
        conn = _get_conn()
        try:
            summary = get_summary_stats(conn)
            processed = get_jobs_processed_counts(conn)
            all_metrics = get_all_job_metrics(conn)
            phase_avgs = get_phase_avg_durations(conn)
            board_rates = get_board_error_rates(conn)
        finally:
            conn.close()
        self.app.call_from_thread(
            self._apply_data, summary, processed, all_metrics, phase_avgs, board_rates
        )

    def _apply_data(self, summary, processed, all_metrics, phase_avgs, board_rates):
        # Summary panel
        def _fmt_dur(ms):
            if not ms:
                return "-"
            s = int(ms / 1000)
            return f"{s // 60}m{s % 60:02d}s"

        summary_lines = [
            f"[bold]Jobs Processed:[/]  1h: {processed['last_1h']}  |  24h: {processed['last_24h']}  |  7d: {processed['last_7d']}  |  All: {processed['all_time']}",
            f"[green]Success Rate:[/] {summary['success_rate']:.1f}%  ({summary['submitted']}/{summary['total']})    "
            f"[red]Failure Rate:[/] {summary['failure_rate']:.1f}%  ({summary['failed']}/{summary['total']})",
            f"[yellow]Intervention Rate:[/] {summary['intervention_rate']:.1f}%  ({summary['jobs_with_interventions']}/{summary['total']})    "
            f"[cyan]Avg Error Rate:[/] {summary['avg_error_rate'] * 100:.1f}%",
            f"[dim]Avg Duration:[/] {_fmt_dur(summary['avg_duration_ms'])}",
        ]
        self.query_one("#stats-summary-content", Static).update("\n".join(summary_lines))

        # Per-job metrics table
        table = self.query_one("#stats-table", DataTable)
        table.clear()
        for m in all_metrics:
            corrected = m.get("fields_corrected", 0)
            total = m.get("total_fields", 0)
            error_pct = f"{m.get('field_error_rate', 0) * 100:.0f}%"
            duration = _fmt_dur(m.get("total_duration_ms"))
            table.add_row(
                str(m.get("job_id", "")),
                m.get("company") or "",
                m.get("role_title") or "",
                m.get("status") or "",
                duration,
                f"{corrected}/{total}",
                error_pct,
                str(m.get("auto_fix_attempts", 0)),
                str(m.get("manual_interventions", 0)),
                key=str(m.get("job_id", "")),
            )

        # Phase breakdown
        if phase_avgs:
            slowest = max(phase_avgs, key=phase_avgs.get)
            lines = []
            for phase in ("resolve", "generate", "submit", "fix", "retry", "post_submit"):
                avg = phase_avgs.get(phase)
                if avg is None:
                    continue
                marker = " [red]<< slowest[/]" if phase == slowest else ""
                lines.append(f"  {phase:15s}  {_fmt_dur(avg)}{marker}")
            self.query_one("#phase-breakdown", Static).update("\n".join(lines) or "[dim]No data[/]")
        else:
            self.query_one("#phase-breakdown", Static).update("[dim]No phase data yet[/]")

        # Board error rates
        if board_rates:
            lines = []
            for board, rate in sorted(board_rates.items(), key=lambda x: x[1], reverse=True):
                bar_len = int(rate * 20)
                bar = "#" * bar_len + "-" * (20 - bar_len)
                lines.append(f"  {board:15s}  [{bar}]  {rate * 100:.0f}%")
            self.query_one("#board-error-rates", Static).update("\n".join(lines))
        else:
            self.query_one("#board-error-rates", Static).update("[dim]No board data yet[/]")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        try:
            row_data = self.query_one("#stats-table", DataTable).get_row(event.row_key)
            job_id = int(row_data[0])
            self.app.push_screen(JobDetailScreen(job_id))
        except Exception:
            pass

    def action_switch_dash(self) -> None:
        self.app.switch_mode("dashboard")

    def action_switch_queue(self) -> None:
        self.app.switch_mode("queue")

    def action_switch_add(self) -> None:
        self.app.switch_mode("add")
```

- [ ] **Step 2: Register StatsScreen in JobApp MODES and add keybinding**

In the `JobApp` class:

Add to `MODES`:
```python
"stats": StatsScreen,
```

Add to `BINDINGS`:
```python
Binding("s", "switch_mode('stats')", "Stats"),
```

- [ ] **Step 3: Add required imports at top of job_tui.py**

Add to the imports from `job_db`:
```python
from job_db import (
    ...existing imports...,
    ensure_job_metrics,
    get_all_job_metrics,
    get_board_error_rates,
    get_job_metrics,
    get_jobs_processed_counts,
    get_phase_avg_durations,
    get_summary_stats,
    log_field_correction,
    update_job_metrics,
)
```

- [ ] **Step 4: Verify TUI imports cleanly**

Run: `uv run python -c "import sys; sys.path.insert(0, 'scripts'); from job_tui import JobApp; print('OK')"`
Expected: `OK`

- [ ] **Step 5: Run full test suite**

Run: `uv run python -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/job_tui.py
git commit -m "feat(telemetry): add TUI Stats screen with summary, per-job metrics, and phase breakdown"
```

---

### Task 12: Dashboard integration

**Files:**
- Modify: `scripts/job_tui.py` (add error rate and avg duration to dashboard summary bar)

- [ ] **Step 1: Update DashboardScreen._apply_data**

In `DashboardScreen._apply_data()`, update the `_refresh_data` method to also fetch `get_summary_stats`, then add to `counts_text`:

Update `_refresh_data`:
```python
@work(thread=True, exclusive=True, group="dash-refresh")
def _refresh_data(self) -> None:
    conn = _get_conn()
    try:
        status_counts = get_status_counts(conn)
        board_counts = get_board_counts(conn)
        recent = _get_recent_events(conn, limit=30)
        summary = get_summary_stats(conn)
    finally:
        conn.close()
    self.app.call_from_thread(self._apply_data, status_counts, board_counts, recent, summary)
```

Update `_apply_data` signature to accept `summary` and add to counts_text:
```python
def _fmt_dur(ms):
    if not ms:
        return "-"
    s = int(ms / 1000)
    return f"{s // 60}m{s % 60:02d}s"

# Append to counts_text:
avg_err = summary.get("avg_error_rate", 0)
avg_dur = summary.get("avg_duration_ms", 0)
counts_text += (
    f"  |  Error Rate: {avg_err * 100:.0f}%"
    f"  |  Avg Duration: {_fmt_dur(avg_dur)}"
)
```

- [ ] **Step 2: Verify TUI imports and runs**

Run: `uv run python -c "import sys; sys.path.insert(0, 'scripts'); from job_tui import JobApp; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Run full test suite**

Run: `uv run python -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add scripts/job_tui.py
git commit -m "feat(telemetry): add error rate and avg duration to dashboard summary"
```

---

## Chunk 5: CLI Report Enhancement

### Task 13: Enhance cmd_report with telemetry data

**Files:**
- Modify: `bin/job-assets` (enhance `cmd_report`, add `--job` and `--csv` flags)

- [ ] **Step 1: Update cmd_report to include telemetry**

Replace the existing `cmd_report` function in `bin/job-assets`:

```python
def cmd_report(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(SCRIPTS_ROOT))
    from job_db import (
        init_db, query_jobs, get_summary_stats, get_jobs_processed_counts,
        get_phase_avg_durations, get_board_error_rates, get_all_job_metrics,
        get_job_metrics, get_field_corrections, get_job,
    )
    conn = init_db(REPO_ROOT / "jobs.db")

    # Single job detail mode
    if args.job:
        job = get_job(conn, args.job)
        if not job:
            print(f"Job {args.job} not found.")
            conn.close()
            return 1
        print(f"Job {job['id']}: {job.get('company', '?')} — {job.get('role_title', '?')}")
        print(f"  Status: {job['status']}  Board: {job.get('board', '-')}  Provider: {job.get('provider', '-')}")
        m = get_job_metrics(conn, args.job)
        if m:
            dur_s = (m['total_duration_ms'] or 0) / 1000
            print(f"  Duration: {dur_s:.0f}s  Fields: {m['fields_corrected']}/{m['total_fields']}  "
                  f"Error Rate: {m['field_error_rate'] * 100:.0f}%")
            print(f"  Fixes: {m['auto_fix_attempts']}  Interventions: {m['manual_interventions']}  "
                  f"Retries: {m['retry_count']}  Phases: {m['phase_count']}")
        # Phase durations
        phases = conn.execute(
            "SELECT phase, duration_ms, exit_code FROM job_phase_durations WHERE job_id = ? ORDER BY id",
            (args.job,),
        ).fetchall()
        if phases:
            print("  Phases:")
            for p in phases:
                dur = f"{(p['duration_ms'] or 0) / 1000:.1f}s"
                ec = f"exit={p['exit_code']}" if p['exit_code'] is not None else ""
                print(f"    {p['phase']:15s}  {dur:>8s}  {ec}")
        # Field corrections
        corrections = get_field_corrections(conn, args.job)
        if corrections:
            print(f"  Field Corrections ({len(corrections)}):")
            for fc in corrections:
                print(f"    [{fc['correction_source']}] {fc['field_name']}: "
                      f"{(fc['original_value'] or '-')[:40]} → {(fc['corrected_value'] or '-')[:40]}")
        conn.close()
        return 0

    # Summary mode
    summary = get_summary_stats(conn, since=args.since)
    processed = get_jobs_processed_counts(conn)

    if args.format == "csv":
        all_metrics = get_all_job_metrics(conn, board=args.board, status=args.status)
        import csv, io
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow([
            "job_id", "company", "role", "status", "board", "total_fields",
            "fields_corrected", "error_rate", "manual_interventions",
            "auto_fix_attempts", "total_duration_ms", "retry_count",
        ])
        for m in all_metrics:
            writer.writerow([
                m["job_id"], m.get("company", ""), m.get("role_title", ""),
                m.get("status", ""), m.get("board", ""), m["total_fields"],
                m["fields_corrected"], f"{m['field_error_rate']:.3f}",
                m["manual_interventions"], m["auto_fix_attempts"],
                m["total_duration_ms"], m["retry_count"],
            ])
        print(out.getvalue(), end="")
        conn.close()
        return 0

    # Text summary
    print("=== Job Pipeline Report ===")
    print(f"Processed:  1h={processed['last_1h']}  24h={processed['last_24h']}  "
          f"7d={processed['last_7d']}  all={processed['all_time']}")
    print(f"Total: {summary['total']}  Submitted: {summary['submitted']}  "
          f"Failed: {summary['failed']}  Attention: {summary['needs_attention']}")
    print(f"Success Rate: {summary['success_rate']:.1f}%  "
          f"Failure Rate: {summary['failure_rate']:.1f}%")
    print(f"Intervention Rate: {summary['intervention_rate']:.1f}%  "
          f"Avg Error Rate: {summary['avg_error_rate'] * 100:.1f}%")
    dur_s = (summary['avg_duration_ms'] or 0) / 1000
    print(f"Avg Duration: {dur_s:.0f}s")

    # Phase averages
    phase_avgs = get_phase_avg_durations(conn)
    if phase_avgs:
        print("\nPhase Avg Durations:")
        for phase in ("resolve", "generate", "submit", "fix", "retry", "post_submit"):
            avg = phase_avgs.get(phase)
            if avg is not None:
                print(f"  {phase:15s}  {avg / 1000:.1f}s")

    # Board error rates
    board_rates = get_board_error_rates(conn)
    if board_rates:
        print("\nBoard Error Rates:")
        for board, rate in sorted(board_rates.items(), key=lambda x: x[1], reverse=True):
            print(f"  {board:15s}  {rate * 100:.0f}%")

    # Job table (existing behavior, enhanced)
    kwargs = {}
    if args.status:
        kwargs["status"] = args.status
    if args.board:
        kwargs["board"] = args.board
    jobs = query_jobs(conn, **kwargs, limit=9999)
    if args.since:
        jobs = [j for j in jobs if j["created_at"] and j["created_at"] >= args.since]
    if jobs:
        print(f"\n{'ID':>5}  {'Status':<18}  {'Board':<12}  {'Company':<20}  {'Role':<30}  {'Created'}")
        print("-" * 105)
        for j in jobs:
            print(
                f"{j['id']:>5}  {j['status']:<18}  {(j['board'] or '-'):<12}  "
                f"{(j['company'] or '-'):<20}  {(j['role_title'] or '-'):<30}  {j['created_at']}"
            )

    conn.close()
    return 0
```

- [ ] **Step 2: Add --job and --csv flags to report parser**

In the argparse section for `report_parser`, add:

```python
report_parser.add_argument("--job", type=int, help="Show detailed metrics for a specific job ID")
report_parser.add_argument(
    "--format",
    choices=("table", "json", "csv"),
    default="table",
    help="Output format (default: %(default)s)",
)
```

Replace the existing `--format` option (which only has `table` and `json`) with the above (adds `csv`).

- [ ] **Step 3: Run full test suite**

Run: `uv run python -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add bin/job-assets
git commit -m "feat(telemetry): enhance CLI report with per-job detail, summary stats, and CSV export"
```

---

### Task 14: Final integration test and cleanup

**Files:**
- Test: `tests/test_job_telemetry.py` (add integration test)

- [ ] **Step 1: Write integration test covering the full flow**

Add to `tests/test_job_telemetry.py`:

```python
class TestTelemetryIntegration:
    """Integration test: simulate a job lifecycle and verify all metrics are populated."""

    def test_full_lifecycle_populates_metrics(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        conn = init_db(f.name)

        # Create job
        job_id = add_job(conn, "https://boards.greenhouse.io/co/jobs/1")
        ensure_job_metrics(conn, job_id)

        # Phase 1: resolve
        p1 = start_phase(conn, job_id, "resolve")
        end_phase(conn, p1, exit_code=0)

        # Phase 2: generate
        p2 = start_phase(conn, job_id, "generate")
        end_phase(conn, p2, exit_code=0)

        # Phase 3: submit
        p3 = start_phase(conn, job_id, "submit")
        end_phase(conn, p3, exit_code=0)

        # Update metrics
        update_job_metrics(conn, job_id, phase_count=3, total_fields=20, fields_corrected=3)

        # Log some field corrections
        log_field_correction(conn, job_id, "first_name", "Jon", "John", "draft_review")
        log_field_correction(conn, job_id, "resume", None, "v2.pdf", "content_edit")

        # Update interventions
        update_job_metrics(conn, job_id, manual_interventions=2)

        # Mark submitted
        update_status(conn, job_id, "submitted")

        # Verify
        m = get_job_metrics(conn, job_id)
        assert m["total_fields"] == 20
        assert m["fields_corrected"] == 3
        assert abs(m["field_error_rate"] - 0.15) < 0.01
        assert m["manual_interventions"] == 2
        assert m["phase_count"] == 3

        # Verify phase durations exist
        phases = conn.execute(
            "SELECT phase FROM job_phase_durations WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
        assert [r["phase"] for r in phases] == ["resolve", "generate", "submit"]

        # Verify field corrections exist
        corrections = get_field_corrections(conn, job_id)
        assert len(corrections) == 2

        # Verify aggregate stats
        stats = get_summary_stats(conn)
        assert stats["total"] >= 1
        assert stats["submitted"] >= 1

        conn.close()
```

- [ ] **Step 2: Run all telemetry tests**

Run: `uv run python -m pytest tests/test_job_telemetry.py -v`
Expected: All tests pass.

- [ ] **Step 3: Run full test suite for final regression check**

Run: `uv run python -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_job_telemetry.py
git commit -m "test(telemetry): add integration test for full job lifecycle metrics"
```
