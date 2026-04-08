---
title: "fix: Recover corrupted events table and harden web UI against DB errors"
type: fix
status: completed
date: 2026-03-24
---

# fix: Recover corrupted events table and harden web UI against DB errors

## Deepening Summary (Round 2)

**Deepened on:** 2026-03-25
**Agents used:** kieran-python-reviewer, data-integrity-guardian, architecture-strategist, codebase-explorer

### Critical Bugs Found in Recovery Script
1. **`trg_jobs_updated_at` trigger** fires during upsert and overwrites `updated_at` with `CURRENT_TIMESTAMP` — must DROP before merge
2. **Column metadata fetched from corrupted source** — use `rows[0].keys()` or query `dst` instead
3. **`INSERT OR IGNORE` silently swallows failures** — orphaned child rows pass undetected
4. **`JSONResponse` not imported** in `job_web.py` — runtime `NameError` on first DB error
5. **`sqlite_sequence` UPDATE misses entries** that don't exist yet — use `INSERT OR REPLACE`

### Architecture Corrections
6. **Backup rotation** should backup both before AND after `init_db()` with different filenames
7. **`draft_web.py`** minimal fix should ship in same PR (replace `_open_db()` with import from `job_db.py`)
8. **Recovery script** should be permanent tooling committed to git, not a one-off
9. **`integrity_check(1)` → full `integrity_check`** — costs <50ms on 5MB DB, catches multi-table corruption

---

## Key Context

1. **Third corruption incident.** Prior root causes: shared SQLite connections across 30+ threads (Mar 23 PM), committing WAL/SHM files via git (Mar 23 late). Both fixed in prior commits.
2. **`job_metrics` uses `job_id` as PK**, not auto-increment `id` — backup is the superset (354 vs 351 rows).
3. **Error handling belongs in web middleware**, not per-function in `job_db.py`.
4. **Backup rotation bug:** `_backup_db()` runs BEFORE `init_db()` — corruption restart overwrites good backup.
5. **`draft_web.py` bypasses all protections** — inline `_open_db()` skips integrity checks. Minimal fix in this PR: replace `_open_db()` with import from `job_db.py`.

---

## Overview

The `events` table in `jobs.db` is severely corrupted (hundreds of malformed B-tree pages), causing 500 Internal Server Errors whenever the web UI loads job details. The `get_job_timeline()` function in `job_db.py:705` queries this table without error handling, and the crash propagates unhandled through the FastAPI middleware stack.

A healthy backup exists at `jobs.db.backup` (integrity: ok, 9,785 events, 404 jobs). The current DB has 405 jobs (1 new since backup). Recovery is straightforward.

## Problem Statement

**Immediate:** The web UI (`job_web.py`) returns 500 on `GET /api/jobs/{id}` for any job, because `get_job_timeline()` hits the corrupted `events` table. This blocks all job review and approval workflows.

**Root cause (corrected):** The web server's `lifespan()` calls `init_db()` which runs `PRAGMA quick_check` — but `quick_check` only validates B-tree structure for pages it touches. It did NOT detect the `events` table corruption because the corrupted pages were not traversed during the check. The corruption itself likely stems from the same class of issues that caused two prior incidents on Mar 23: unclean process termination leaving WAL uncheckpointed.

**Systemic:** No error handling wraps DB queries in the web endpoint handlers. Every read-only function in `job_db.py` (`get_job_timeline`, `query_jobs`, `get_status_counts`, `get_board_counts`, `get_summary_stats`, etc.) propagates `DatabaseError` unhandled. There is no global exception handler middleware in the FastAPI app.

### Research Insight: Corruption History

| Date | Incident | Root Cause | Fix |
|------|----------|-----------|-----|
| Mar 23 PM | First corruption (9.3MB DB) | Shared SQLite connection across 30+ threads | `dc497160`: per-thread connections |
| Mar 23 late | Second corruption | Committing WAL/SHM files via git while DB in use | `f7b43b47`: added WAL/SHM to .gitignore |
| Mar 24 (now) | Third corruption (events table) | Likely residual from prior incidents or unclean restart | This plan |

Prior fixes (Mar 24 commits `7d518d50`, `be2dcbc7`, `c7069d80`): connection registry, graceful shutdown, pre-migration backup via `sqlite3.backup()`, SIGTERM instead of `os._exit(0)`.

## Corruption Assessment

| Table | Root Page | Status | Current Rows | Backup Rows | Delta |
|---|---|---|---|---|---|
| `jobs` | 3 | OK | 405 (max id=445) | 404 (max id=444) | +1 new job |
| `events` | 5 | **CORRUPTED** | unreadable | 9,785 | events for job 445 lost |
| `fix_attempts` | 6 | OK | 0 | 0 | — |
| `provider_runs` | 7 | minor corruption | 1,633 (max id=2145) | 1,626 (max id=2016) | +7 new |
| `job_phase_durations` | 8 | minor corruption | 11,229 (max id=13686) | 11,044 (max id=13026) | +185 new |
| `field_corrections` | 9 | OK | 0 | 0 | — |
| `job_metrics` | 10 | OK | 351 | **354** | backup has 3 MORE |
| `candidate_jobs` | 11 | OK | 105 | 105 | — |
| `lost_and_found` | 819 | OK | 198 | 198 | — |

**Backup:** `jobs.db.backup` — fully healthy, created automatically by `_backup_db()` at last web server startup (uses WAL-safe `sqlite3.Connection.backup()` API).

## Proposed Solution

Two-phase fix: (1) recover the database, then (2) add web-layer resilience so corruption can't crash the UI.

### Phase 1: Database Recovery

#### Recommended Approach: Copy Backup + Merge Delta

The delta is small (1 new job + a few hundred rows in non-critical tables), so recovery is straightforward: copy backup, merge newer rows, verify.

```bash
# 1. Stop all processes
kill $(cat jobs.db.worker.pid) 2>/dev/null
# Stop web server (Ctrl-C or kill PID)

# 2. Verify no locks
lsof jobs.db jobs.db-wal jobs.db-shm 2>/dev/null

# 3. Preserve corrupted DB + WAL artifacts
cp jobs.db jobs.db.corrupt-2026-03-24
cp jobs.db-shm jobs.db.corrupt-2026-03-24-shm 2>/dev/null
cp jobs.db-wal jobs.db.corrupt-2026-03-24-wal 2>/dev/null

# 4. Start from healthy backup
cp jobs.db.backup jobs.db.recovered
```

Then merge the delta with a focused script:

```python
"""Merge newer rows from corrupted DB into recovered backup copy.

Committed as permanent tooling — 3 corruption incidents in 2 days means
this is a known failure mode with a known recovery path.

Usage: uv run python scripts/recover_db.py [--dry-run] [--verify-only]
"""
import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

CORRUPTED = Path("jobs.db")
RECOVERED = Path("jobs.db.recovered")


def recover(dry_run: bool = False) -> None:
    # Open corrupted DB read-only to prevent accidental writes
    with closing(sqlite3.connect(f"file:{CORRUPTED}?mode=ro", uri=True)) as src, \
         closing(sqlite3.connect(RECOVERED)) as dst:

        src.row_factory = sqlite3.Row
        dst.execute("PRAGMA journal_mode=WAL")
        dst.execute("PRAGMA foreign_keys=OFF")  # disable during merge

        # CRITICAL: Drop the updated_at trigger — it fires on UPDATE and
        # overwrites the historical updated_at we're trying to preserve
        dst.execute("DROP TRIGGER IF EXISTS trg_jobs_updated_at")

        # --- Merge new rows for AUTOINCREMENT tables ---
        # Table names are hardcoded literals — not user input, f-string is safe.
        for table in ("jobs", "provider_runs", "job_phase_durations"):
            max_id = dst.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0] or 0
            try:
                rows = src.execute(f"SELECT * FROM {table} WHERE id > ?", (max_id,)).fetchall()
            except sqlite3.DatabaseError as e:
                print(f"WARNING: {table} unreadable from corrupted source: {e}")
                continue
            if rows:
                # Get column names from the rows themselves (safe) — NOT from
                # a second query to the corrupted source
                cols = rows[0].keys()
                placeholders = ",".join(["?"] * len(cols))
                if dry_run:
                    print(f"{table}: would merge {len(rows)} newer rows (id > {max_id})")
                else:
                    dst.executemany(
                        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
                        rows,
                    )
                    print(f"{table}: merged {len(rows)} newer rows")

        # --- Upsert jobs with newer updated_at (data integrity fix) ---
        # MAX(id) merge only captures new rows. Jobs 1-404 may have had
        # status/updated_at changes since backup. Update those too.
        try:
            # Get column names from dst (healthy, guaranteed not to fail)
            cols = [d[0] for d in dst.execute("SELECT * FROM jobs LIMIT 0").description]
            backup_max_id = dst.execute("SELECT MAX(id) FROM jobs").fetchone()[0] or 0
            updated = 0
            for row in src.execute("SELECT * FROM jobs WHERE id <= ?", (backup_max_id,)):
                row_dict = dict(row)
                backup_updated = dst.execute(
                    "SELECT updated_at FROM jobs WHERE id = ?", (row_dict["id"],)
                ).fetchone()
                if backup_updated and row_dict["updated_at"] > backup_updated[0]:
                    set_clause = ", ".join(f"{c} = ?" for c in cols if c != "id")
                    vals = [row_dict[c] for c in cols if c != "id"] + [row_dict["id"]]
                    if not dry_run:
                        dst.execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", vals)
                    updated += 1
            if updated:
                print(f"jobs: {'would update' if dry_run else 'updated'} {updated} existing rows with newer data")
        except sqlite3.DatabaseError as e:
            print(f"WARNING: could not upsert existing jobs: {e}")

        # job_metrics uses job_id as PK (not auto-increment) — backup has MORE rows
        # Keep backup's version (354 rows); it's the superset

        if dry_run:
            print("\nDry run complete. No changes written.")
            return

        # sqlite_sequence: use INSERT OR REPLACE to handle missing entries
        for table in ("jobs", "provider_runs", "job_phase_durations"):
            max_id = dst.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0]
            dst.execute(
                "INSERT OR REPLACE INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                (table, max_id),
            )

        # Recreate the trigger before committing
        dst.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_jobs_updated_at AFTER UPDATE ON jobs
            BEGIN
                UPDATE jobs SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
            END
        """)

        dst.execute("PRAGMA foreign_keys=ON")
        dst.commit()

        # Verify
        verify(dst)


def verify(conn: sqlite3.Connection | None = None) -> None:
    """Run all post-recovery verification checks."""
    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(RECOVERED)

    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"Recovery failed integrity check: {result}")
    print(f"integrity_check: {result}")

    fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk_errors:
        raise RuntimeError(f"Foreign key violations found: {fk_errors[:5]}")
    print("foreign_key_check: ok")

    # Orphan checks for child tables
    for child, fk_col, parent in [
        ("provider_runs", "job_id", "jobs"),
        ("job_phase_durations", "job_id", "jobs"),
        ("job_metrics", "job_id", "jobs"),
        ("events", "job_id", "jobs"),
    ]:
        orphans = conn.execute(
            f"SELECT COUNT(*) FROM {child} WHERE {fk_col} NOT IN (SELECT id FROM {parent})"
        ).fetchone()[0]
        if orphans:
            print(f"WARNING: {orphans} orphaned rows in {child}")

    # Row counts report
    for table in ("jobs", "events", "provider_runs", "job_phase_durations", "job_metrics"):
        count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count} rows")
    print("Recovery successful.")

    if own_conn:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Recover corrupted jobs.db from backup")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be merged without writing")
    parser.add_argument("--verify-only", action="store_true", help="Run verification checks on existing recovered DB")
    args = parser.parse_args()

    if args.verify_only:
        verify()
    else:
        recover(dry_run=args.dry_run)

# Final step (manual): mv jobs.db.recovered jobs.db
```

#### Research Insights: Recovery Script Improvements

From the Python reviewer, data integrity guardian, and architecture strategist (rounds 1+2):

| Issue | Fix Applied |
|---|---|
| No context managers for DB connections | `closing()` context managers |
| `assert` stripped by `-O` flag | `raise RuntimeError` |
| Corrupted DB opened read-write | `file:...?mode=ro` URI |
| `job_metrics` uses `job_id` PK, not `id` | Kept backup's superset |
| sqlite_sequence not updated after merge | `INSERT OR REPLACE INTO sqlite_sequence` |
| Reads from corrupted tables not wrapped | `try/except DatabaseError` per table |
| **`trg_jobs_updated_at` overwrites `updated_at` during upsert** | **DROP TRIGGER before merge, recreate after** |
| **Column metadata fetched from corrupted `src`** | **Use `rows[0].keys()` or query `dst` instead** |
| **`INSERT OR IGNORE` silently swallows constraint violations** | **Changed to plain `INSERT` — failures are now visible** |
| **Hardcoded magic number `444`** | **Computed from `dst` via `SELECT MAX(id)`** |
| **`foreign_key_check` missing from script verification** | **Added to `verify()` function** |
| **Orphan rows in child tables not checked** | **Added orphan checks for all FK relationships** |
| **One-off script with no CLI interface** | **Permanent tooling with `--dry-run` and `--verify-only`** |

#### Deployment Verification Queries

Run BEFORE recovery (against current `jobs.db`) to establish baseline:

```sql
-- Row counts baseline
SELECT 'jobs' AS tbl, COUNT(*) AS cnt FROM jobs
UNION ALL SELECT 'provider_runs', COUNT(*) FROM provider_runs
UNION ALL SELECT 'job_phase_durations', COUNT(*) FROM job_phase_durations
UNION ALL SELECT 'job_metrics', COUNT(*) FROM job_metrics;

-- Verify the new job that must survive
SELECT id, url, company, role_title, status FROM jobs WHERE id = 445;

-- Max IDs for merge verification
SELECT 'jobs' AS tbl, MAX(id) FROM jobs
UNION ALL SELECT 'provider_runs', MAX(id) FROM provider_runs
UNION ALL SELECT 'job_phase_durations', MAX(id) FROM job_phase_durations;
```

Run AFTER recovery (against `jobs.db.recovered`) to verify:

```sql
-- All counts must meet or exceed current counts
-- events must have 9785+ rows (from backup)
-- job_metrics must have 354 rows (backup superset)
-- jobs must have 405 rows
-- PRAGMA integrity_check must return "ok"
-- PRAGMA foreign_key_check must return no rows
```

### Phase 2: Code Resilience

#### Research Insight: Middleware, Not Per-Function

The architecture strategist identified that per-function `try/except` in `job_db.py` is the wrong pattern:

- `job_db.py` is a **shared library** used by web server, TUI, workers, and CLI
- Only the web server needs graceful degradation (HTTP 503 vs 500 traceback)
- Workers should crash and retry. The TUI should show an error widget.
- Per-function catch creates inconsistent contracts: "is empty list normal or broken?"

**2a. Add global exception handler in `job_web.py`:**

**Prerequisite:** Add `JSONResponse` to the import at `job_web.py:18` — it's not currently imported and would cause a `NameError`:

```python
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
```

```python
# scripts/job_web.py — add near the app definition
@app.exception_handler(sqlite3.DatabaseError)
async def db_error_handler(request: Request, exc: sqlite3.DatabaseError) -> JSONResponse:
    log.error("Database error on %s: %s", request.url.path, exc, exc_info=True)
    return JSONResponse(
        status_code=503,
        content={"error": "database_error", "detail": str(exc)},
    )
```

This gives: one place for alerting, consistent error shape for the frontend, no changes to `job_db.py`. The `async def` handler works correctly with sync route handlers — Starlette's `ExceptionMiddleware` catches exceptions from threadpool-executed sync routes and dispatches to registered handlers.

> **Design principle:** Targeted try/except at the route level (Phase 2b) is reserved for fields where (a) the table has known corruption risk AND (b) the field is non-critical to the page's primary function. All other DatabaseErrors propagate to this global handler.

**2b. Additionally handle timeline gracefully in the route handler** (timeline is non-critical — the job detail page should still load):

```python
# scripts/job_web.py — in get_job_detail()
@app.get("/api/jobs/{job_id}")
def get_job_detail(job_id: int):
    conn = get_conn()
    job = get_job(conn, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    try:
        timeline = get_job_timeline(conn, job_id)
    except sqlite3.DatabaseError:
        log.warning("Timeline query failed for job %d, returning empty", job_id, exc_info=True)
        timeline = []
    if not timeline and job.get("output_dir"):
        timeline = _synthetic_timeline(job)
    job["timeline"] = timeline
    job["metrics"] = get_job_metrics(conn, job_id)
    job["field_corrections"] = get_field_corrections(conn, job_id)
    job["previously_submitted"] = job.get("confirmed_at") is not None
    return job
```

**2c. Upgrade `quick_check` to full `integrity_check` in `init_db()`** (`scripts/job_db.py:305`)

~~Add a new startup handler~~ — `init_db()` already runs `PRAGMA quick_check` at line 305. Upgrade it:

```python
# scripts/job_db.py line 305 — change:
result = conn.execute("PRAGMA quick_check").fetchone()[0]
# to:
result = conn.execute("PRAGMA integrity_check").fetchone()[0]
```

Full `integrity_check` (no limit) costs <50ms on a 5MB database — negligible at startup. It catches multi-table corruption that `integrity_check(1)` would miss: in this incident, both `events` (root page 5) and `provider_runs` (root page 7) were corrupted, but `integrity_check(1)` would have only reported the first table encountered.

#### Research Insight: Performance Is a Non-Issue

| Operation | Cost on 5MB DB |
|---|---|
| `PRAGMA quick_check` | <5ms |
| `PRAGMA integrity_check` (full, no limit) | <50ms |
| `try/except` (no exception thrown) | 0 bytecode cost (Python 3.11+ zero-cost exceptions) |
| Timeline query (~24 events/job with existing index) | <1ms |

**2d. Add composite index for timeline queries** (`scripts/job_db.py`, in `_SCHEMA` near line 97 — schema defined at lines 34-100):

```sql
CREATE INDEX IF NOT EXISTS idx_events_job_created ON events(job_id, created_at DESC);
```

The current single-column `idx_events_job_id` satisfies `WHERE job_id = ?` but forces a sort for `ORDER BY created_at DESC`. The composite index eliminates the sort. At current scale (~24 events/job) the difference is invisible, but it costs nothing and scales linearly.

**2e. Fix backup rotation bug** (`scripts/job_web.py:439-452`):

The current `_backup_db()` runs BEFORE `init_db()` in the lifespan. If corruption causes a restart, the corrupted DB overwrites the good backup before the integrity check fires.

**Architecture insight:** Simply moving backup after `init_db()` introduces a new risk — `init_db()` runs migrations (`ALTER TABLE`, status updates) before backup, so you'd lose the pre-migration state. The correct fix is to backup **both** before and after:

```python
# In lifespan() — change to dual backup:
async def lifespan(app: FastAPI):
    if DB_PATH.exists():
        # Pre-migration backup — preserves last-known state (even if corrupted)
        _backup_db(DB_PATH, DB_PATH.with_suffix(".db.pre-migration"))

    conn = init_db(DB_PATH, check_same_thread=False)
    conn.close()

    if DB_PATH.exists():
        # Post-migration backup — known-good, schema up to date
        _backup_db(DB_PATH, DB_PATH.with_suffix(".db.backup"))
    ...
```

Cost: one extra `sqlite3.backup()` call — <10ms on 5MB DB. The `.pre-migration` file is a safety net for broken migrations; `.backup` is the recovery target for runtime corruption.

**2f. Minimal fix for `draft_web.py`** (`scripts/draft_web.py:23-31`):

`draft_web.py` has its own `_open_db()` that bypasses integrity checks, connection tracking, and timeout configuration. Its write endpoints (`approve_draft`, `reject_draft`) create untracked connections that are a live vector for the same corruption class (untracked connections were the Mar 23 root cause).

Minimal fix for this PR — replace `_open_db()` with the shared `open_db`:

```python
# scripts/draft_web.py — replace inline _open_db() with:
from job_db import open_db
# Then use: conn = open_db(PROJECT_ROOT / "jobs.db", check_same_thread=False)
```

Connection tracking and the global exception handler for `draft_web.py` are legitimate deferrals for a follow-up.

**Note:** 12+ query functions in `job_db.py` lack error handling (`get_job`, `query_jobs`, `get_status_counts`, `get_summary_stats`, etc.). The global `@app.exception_handler(sqlite3.DatabaseError)` in Phase 2a covers all of them — no per-function changes needed.

## Deployment Checklist (Go/No-Go)

### Pre-Deploy Blockers (any = STOP)

- [ ] Backup file `jobs.db.backup` exists and passes `PRAGMA integrity_check` = "ok"
- [ ] No processes holding locks: `lsof jobs.db* 2>/dev/null`
- [ ] Sufficient disk space (~15 MB needed for 3 copies)
- [ ] Tests pass: `uv run python -m pytest tests/test_job_db.py tests/test_job_web.py -v`
- [ ] Lint passes: `uv run ruff check scripts/job_db.py scripts/job_web.py`
- [ ] Baseline queries saved (row counts, max IDs, status distribution)

### Deploy Steps

| Step | Command | Rollback |
|---|---|---|
| 1. Stop worker | `kill $(cat jobs.db.worker.pid)` | Restart worker |
| 2. Stop web server | Ctrl-C or kill PID | Restart web |
| 3. Verify no locks | `lsof jobs.db*` | — |
| 4. Preserve corrupted DB | `cp jobs.db jobs.db.corrupt-2026-03-25` | — |
| 5. Preserve WAL/SHM | `cp jobs.db-shm jobs.db.corrupt-2026-03-25-shm` | — |
| 6. Dry-run recovery | `uv run python scripts/recover_db.py --dry-run` | — |
| 7. Run recovery | `uv run python scripts/recover_db.py` | Use corrupt backup |
| 8. Verify recovered DB | Run SQL queries below | — |
| 9. Replace live DB | `mv jobs.db.recovered jobs.db` | `cp jobs.db.corrupt-2026-03-25 jobs.db` |
| 10. Remove stale WAL/SHM | `rm -f jobs.db-wal jobs.db-shm` | — |
| 11. Deploy code | `git commit` the code changes | `git revert` |
| 12. Restart web | `job-assets web` | Kill and check logs |
| 13. Verify no corruption warning | Check startup log | STOP if warning |

**CRITICAL: Step 10 is required.** Old WAL/SHM files belong to the corrupted DB. SQLite will try to replay them on the new file.

### Post-Deploy SQL Verification

```sql
-- 1. INTEGRITY (most important)
PRAGMA integrity_check;
-- EXPECTED: "ok"

-- 2. Row counts
SELECT 'jobs' AS tbl, COUNT(*) AS cnt FROM jobs
UNION ALL SELECT 'events', COUNT(*) FROM events
UNION ALL SELECT 'provider_runs', COUNT(*) FROM provider_runs
UNION ALL SELECT 'job_phase_durations', COUNT(*) FROM job_phase_durations
UNION ALL SELECT 'job_metrics', COUNT(*) FROM job_metrics;
-- EXPECTED: jobs>=405, events>=9785, provider_runs>=1633, job_phase_durations>=11229, job_metrics>=354

-- 3. New job survived
SELECT id, url, company, role_title, status FROM jobs WHERE id = 445;

-- 4. Events actually work now
SELECT job_id, COUNT(*) FROM events GROUP BY job_id ORDER BY COUNT(*) DESC LIMIT 5;

-- 5. Foreign key integrity
PRAGMA foreign_key_check;
-- EXPECTED: no rows

-- 6. No orphaned rows in any child table
SELECT 'events' AS tbl, COUNT(*) AS orphans FROM events WHERE job_id NOT IN (SELECT id FROM jobs)
UNION ALL SELECT 'provider_runs', COUNT(*) FROM provider_runs WHERE job_id NOT IN (SELECT id FROM jobs)
UNION ALL SELECT 'job_phase_durations', COUNT(*) FROM job_phase_durations WHERE job_id NOT IN (SELECT id FROM jobs)
UNION ALL SELECT 'job_metrics', COUNT(*) FROM job_metrics WHERE job_id NOT IN (SELECT id FROM jobs);
-- EXPECTED: all 0

-- 7. candidate_jobs FK integrity
SELECT COUNT(*) FROM candidate_jobs WHERE promoted_job_id IS NOT NULL AND promoted_job_id NOT IN (SELECT id FROM jobs);
-- EXPECTED: 0
```

## Monitoring (First 24 Hours)

### Immediate (0-5 min)
- [ ] Web server starts without corruption warning in log
- [ ] `curl -s http://127.0.0.1:8420/api/jobs/1` returns 200 with timeline
- [ ] No `DatabaseError` tracebacks in `jobs.db.worker.log`

### Hourly (first 4 hours)
```bash
uv run python -c "
import sqlite3, os
conn = sqlite3.connect('jobs.db', timeout=5)
print('integrity:', conn.execute('PRAGMA integrity_check(1)').fetchone()[0])
print('jobs:', conn.execute('SELECT count(*) FROM jobs').fetchone()[0])
print('events:', conn.execute('SELECT count(*) FROM events').fetchone()[0])
wal = 'jobs.db-wal'
print(f'WAL size: {os.path.getsize(wal) if os.path.exists(wal) else 0} bytes')
conn.close()
"
```

Watch for: integrity != "ok", events count dropping, WAL > 10 MB.

### Daily (first 3 days)
- [ ] Full `PRAGMA integrity_check` (no limit)
- [ ] New jobs added since recovery have events
- [ ] Confirm `jobs.db.backup` updated with fresh backup

## Acceptance Criteria

- [ ] `jobs.db` passes `PRAGMA integrity_check` with result "ok"
- [ ] All 405 jobs accessible via web UI (including job 445)
- [ ] Events timeline loads for all jobs (9,785+ events recovered from backup)
- [ ] `GET /api/jobs/{id}` returns 503 (not 500) on `DatabaseError`
- [ ] Timeline-specific failures degrade gracefully (empty timeline, page still loads)
- [ ] `init_db()` uses full `integrity_check` (not `quick_check` or `integrity_check(1)`)
- [ ] Composite index `idx_events_job_created` on `events(job_id, created_at DESC)` exists
- [ ] Dual backup: `lifespan()` backs up before `init_db()` (`.pre-migration`) and after (`.backup`)
- [ ] `PRAGMA foreign_key_check` returns no orphan references
- [ ] No orphaned rows in `events`, `provider_runs`, `job_phase_durations`, or `job_metrics`
- [ ] `job_metrics` has 354 rows (backup superset preserved)
- [ ] `JSONResponse` imported in `job_web.py`
- [ ] `draft_web.py` uses `open_db` from `job_db.py` instead of inline `_open_db()`
- [ ] Recovery script committed to `scripts/recover_db.py` with `--dry-run` and `--verify-only`

## Dependencies & Risks

- **Risk (low):** Events for job 445 are permanently lost — they existed only in the corrupted table and `.recover` found 0 salvageable rows. The job itself and its `provider_runs`/`job_phase_durations` will survive the merge.
- **Risk (low):** `provider_runs` and `job_phase_durations` have minor B-tree corruption. The merge only reads rows with `id > max_backup_id` from the corrupted DB, wrapped in `try/except`. The bulk of the data comes from the healthy backup.
- **Risk (medium):** Any jobs that had `status`, `updated_at`, or other field updates after the backup was taken will revert to backup-era values. Mitigation: after recovery, manually verify the status of recently active jobs.
- **Dependency:** Web server and workers must be stopped during recovery.
- **Note:** `draft_web.py` minimal fix (replace `_open_db()`) included in this PR. Full connection tracking is a follow-up.
- **Follow-up:** `job_tui.py` also calls `get_job_timeline` (imported at line 52). If events are corrupted, the TUI will crash with unhandled `DatabaseError`. Needs similar targeted handling in the TUI's timeline widget.

## Rollback Procedure

If recovery produces a worse result:

```bash
# Recovery artifacts preserved:
# - jobs.db.corrupt-2026-03-24  (corrupted original)
# - jobs.db.backup              (untouched healthy backup)
# - jobs.db.recovered           (recovery attempt)

# To rollback:
mv jobs.db jobs.db.failed-recovery
cp jobs.db.backup jobs.db
# You lose: job 445 + its provider_runs + job_phase_durations
# You get: fully working DB with 404 jobs, 9785 events, integrity OK
```

## Sources & References

- Crash site: `scripts/job_db.py:705` (`get_job_timeline`)
- Web endpoint: `scripts/job_web.py:599` (`get_job_detail`)
- Lifespan with `init_db`: `scripts/job_web.py:465`
- Existing `quick_check`: `scripts/job_db.py:305`
- Schema definition: `scripts/job_db.py:34-100` (`_SCHEMA`)
- `updated_at` trigger: `scripts/job_db.py:102-105` (`trg_jobs_updated_at`)
- Pre-migration backup: `scripts/job_web.py:439-452` (`_backup_db`)
- Connection registry: `scripts/job_web.py:61-94`
- `draft_web.py` bypass: `scripts/draft_web.py:23-31` (`_open_db`)
- TUI timeline import: `scripts/job_tui.py:52`
- Prior corruption fix plan: `docs/plans/2026-03-24-001-fix-sqlite-corruption-crash-and-focus-browser-plan.md`
- Healthy backup: `jobs.db.backup` (404 jobs, 9,785 events, integrity OK)
- Corrupted backup: `jobs.db.corrupt.bak` (from Mar 23 incident 1)
