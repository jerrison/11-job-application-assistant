---
title: "fix: SQLite resilience follow-ups from PR #34"
type: fix
status: active
date: 2026-03-25
origin: docs/brainstorms/2026-03-25-sqlite-resilience-follow-ups-requirements.md
---

# fix: SQLite resilience follow-ups from PR #34

Four independent hardening items deferred from PR #34 to keep that PR focused on the critical recovery + web UI resilience.

## R1: TUI timeline error handling

`job_tui.py:865` calls `get_job_timeline()` without error handling inside `_refresh_data()`. If the events table is corrupted, the TUI crashes with unhandled `DatabaseError`.

```python
# scripts/job_tui.py:860-868 — current code
@work(thread=True, exclusive=True, group="detail-refresh")
def _refresh_data(self) -> None:
    conn = _get_conn()
    try:
        job = get_job(conn, self.job_id)
        timeline = get_job_timeline(conn, self.job_id)
    finally:
        conn.close()
    self.app.call_from_thread(self._apply_data, job, timeline)
```

**Fix:** Wrap `get_job_timeline` in `try/except sqlite3.DatabaseError`, return empty list on failure, log the warning. Mirrors the pattern in `job_web.py:get_job_detail` (see origin: requirements doc R1).

```python
@work(thread=True, exclusive=True, group="detail-refresh")
def _refresh_data(self) -> None:
    conn = _get_conn()
    try:
        job = get_job(conn, self.job_id)
        try:
            timeline = get_job_timeline(conn, self.job_id)
        except sqlite3.DatabaseError:
            log.warning("Timeline query failed for job %d", self.job_id, exc_info=True)
            timeline = []
    finally:
        conn.close()
    self.app.call_from_thread(self._apply_data, job, timeline)
```

## R2: draft_web.py connection tracking

PR #34 replaced `_open_db()` with `job_db.open_db()`, but `draft_web.py` connections are still invisible to `close_all_connections()`. Its write endpoints (`approve_draft`, `reject_draft`) create untracked connections.

**Deferred question resolved:** Extract connection tracking to `job_db.py` so both apps can share it. Currently `_connections`, `_conn_lock`, `_open_db_tracked`, and `close_all_connections` live in `job_web.py:61-94`. Moving them to `job_db.py` avoids circular imports and follows the pattern that `job_db.py` is the shared data layer.

**Fix:**

1. Move to `job_db.py`:
   - `_connections: set[sqlite3.Connection]`
   - `_conn_lock = threading.Lock()`
   - `open_db_tracked(db_path, **kwargs) -> Connection` (new function, wraps `open_db` + registers)
   - `close_all_connections() -> None`

2. Update `job_web.py`:
   - Remove local `_connections`, `_conn_lock`, `_open_db_tracked`, `close_all_connections`
   - Import from `job_db` instead
   - `get_conn()` calls `job_db.open_db_tracked(DB_PATH, check_same_thread=False)`

3. Update `draft_web.py`:
   - Replace `_open_db()` with `job_db.open_db_tracked(...)`
   - Add `atexit.register(job_db.close_all_connections)` or a lifespan handler

4. Add `@app.exception_handler(sqlite3.DatabaseError)` to `draft_web.py`'s FastAPI app (same pattern as `job_web.py`).

## R3: SQLite WAL-reset mitigation

Confirmed: `sqlite3.sqlite_version` = **3.50.4** (affected by WAL-reset data race, fixed in 3.50.7/3.51.3).

**Deferred question resolved:** `pysqlite3` requires `import pysqlite3 as sqlite3` across ~15 files — too invasive for a mitigation. Use `wal_autocheckpoint` instead.

**Fix:** Add `PRAGMA wal_autocheckpoint=4000` to `open_db()` in `job_db.py:290`. This reduces WAL reset frequency by 4x (16MB threshold vs default 4MB), shrinking the window for the race condition. Costs nothing — WAL file grows slightly larger between checkpoints.

```python
# scripts/job_db.py:open_db() — add after existing PRAGMAs
conn.execute("PRAGMA wal_autocheckpoint=4000")  # mitigate SQLite 3.50.4 WAL-reset bug
```

Also add a startup warning in `init_db()` if the SQLite version is < 3.50.7:

```python
import sqlite3 as _sqlite3
if tuple(int(x) for x in _sqlite3.sqlite_version.split(".")) < (3, 50, 7):
    log.warning(
        "SQLite %s has a known WAL-reset data race (fixed in 3.50.7). "
        "Using wal_autocheckpoint=4000 as mitigation.",
        _sqlite3.sqlite_version,
    )
```

## R4: Per-table startup probe

`integrity_check` validates B-tree structure but can miss scenarios where a specific table's root page passes the check but fails on queries. This would have caught the third corruption incident immediately.

**Deferred question resolved:** Probe all tables in `_SCHEMA` — it's 7 tables at ~1ms each. The cost of missing one is another undetected corruption.

**Fix:** Add after `integrity_check` passes in `init_db()` (after line 316, before `conn.executescript(_SCHEMA)`):

```python
# Probe every table to catch per-table corruption that integrity_check misses
_PROBE_TABLES = ("jobs", "events", "fix_attempts", "provider_runs",
                 "job_phase_durations", "field_corrections", "job_metrics",
                 "candidate_jobs")
for table in _PROBE_TABLES:
    try:
        conn.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
    except sqlite3.DatabaseError as exc:
        conn.close()
        log.error("TABLE %s UNREADABLE: %s", table, exc)
        raise RuntimeError(f"Table {table} unreadable: {exc}") from exc
```

Note: This runs AFTER `_SCHEMA` creates the tables (via `CREATE TABLE IF NOT EXISTS`), so it works on first run too. Actually, the probe should go after `conn.executescript(_SCHEMA)` at line 318 to ensure tables exist.

## Acceptance Criteria

- [ ] TUI does not crash when events table is corrupted (R1)
- [ ] `draft_web.py` connections tracked by shared `close_all_connections()` (R2)
- [ ] `draft_web.py` has `DatabaseError` exception handler returning 503 (R2)
- [ ] Connection tracking functions live in `job_db.py`, not `job_web.py` (R2)
- [ ] `PRAGMA wal_autocheckpoint=4000` set in `open_db()` (R3)
- [ ] Startup warning logged when SQLite < 3.50.7 (R3)
- [ ] Per-table probe runs at startup after schema creation (R4)
- [ ] Probe covers all 8 tables in `_SCHEMA` (R4)
- [ ] All existing tests pass
- [ ] Lint and architecture checks pass

## Dependencies & Risks

- **R2 is the largest change** — extracting connection tracking from `job_web.py` to `job_db.py` touches the most code. Other items are 1-5 line changes.
- **R2 risk:** Moving `_connections` to `job_db.py` means it's a module-level singleton. This is fine because `job_db.py` is already used that way (one DB per process). Worker processes that import `job_db` but don't use tracked connections are unaffected.
- **R3 risk (low):** `wal_autocheckpoint=4000` means WAL can grow to ~16MB before auto-checkpoint. Negligible for a 5MB DB.
- **R4 risk (low):** `SELECT COUNT(*)` on a large table could be slow. At current scale (max ~12K rows in `job_phase_durations`), this is <5ms per table.

## Sources & References

- **Origin document:** [docs/brainstorms/2026-03-25-sqlite-resilience-follow-ups-requirements.md](docs/brainstorms/2026-03-25-sqlite-resilience-follow-ups-requirements.md)
- Prior PR: #34 (fix: harden web UI against DB corruption)
- TUI timeline call: `scripts/job_tui.py:865`
- Connection registry: `scripts/job_web.py:61-94`
- `open_db()`: `scripts/job_db.py:282-292`
- `init_db()`: `scripts/job_db.py:296-364`
- `draft_web.py`: `scripts/draft_web.py:23-31`
- SQLite WAL-reset bug: fixed in SQLite 3.50.7 / 3.51.3
