---
title: "fix: SQLite corruption crash blocks server startup + focus-browser 404 semantics"
type: fix
status: completed
date: 2026-03-24
---

# fix: SQLite corruption crash blocks server startup + focus-browser 404 semantics

## Overview

Two bugs observed during normal `job-assets web` usage:

1. **Server-blocking crash**: `sqlite3.DatabaseError: database disk image is malformed` in `_migrate_archived_status` during `init_db`, preventing the web server from accepting any connections
2. **Misleading 404**: `POST /api/jobs/{id}/focus-browser` returns 404 when the job isn't in `awaiting_captcha` status — semantically wrong (route exists) and the UI allows the action regardless of status

## Problem Statement

### Bug 1: DB corruption crashes server on startup

`init_db()` runs every time a new web thread requests a DB connection via `get_conn()` → `_get_conn()` → `init_db()`. The function runs schema DDL, column migrations, then `_migrate_archived_status()` — the first query that actually scans data rows. On a corrupted DB, this throws `DatabaseError` with zero error handling, crashing the server.

**Root causes of corruption:**
- `os._exit(0)` in `kill()` (job_web.py:476) and `main()` (job_web.py:1507) — bypasses Python cleanup, leaves WAL uncheckpointed
- `os.execv` in `restart()` (job_web.py:491) — replaces process image without closing connections
- `_kill_port()` in `main()` (job_web.py:1495) — sends SIGKILL to previous server, no cleanup
- `stop_workers()` fallback (job_web.py:170) — SIGKILL on orphaned processes

**Evidence of prior corruption:** `jobs.db.corrupt.bak` (9.3MB, March 23) exists alongside current `jobs.db` (4.1MB). A `lost_and_found` table with 198 rows from a prior `.recover` operation exists in the production DB.

**Architectural issue:** The web server and TUI both call `init_db` on every new thread connection. The worker pool already does this correctly — `init_db` once at startup, `open_db` per-thread.

### Bug 2: focus-browser uses wrong HTTP status

The endpoint at `job_web.py:607` conflates two conditions: "job not found" (genuine 404) and "job exists but wrong status" (should be 409 Conflict). Currently both return 404.

Additionally, two independent JS code paths trigger this:
- `focusCaptchaBrowser()` (app.js:1538) — button click, uses `apiCall`, shows error toast
- `_focusBrowser()` (app.js:2939) — F key shortcut, uses raw `fetch`, silently swallows errors

Neither has a client-side status guard. The F key fires regardless of job status. The silent error swallowing in `_focusBrowser` is a bug — users get no feedback when the action fails.

### Research Insight: Broader Race Condition Pattern

`window.jobs` is an eventually-consistent local cache with up to 2-second staleness (WebSocket poll interval). **Every keyboard shortcut that gates on `job.status` has the same race window** — not just focus-browser but also approve (A key), archive (E key), etc. The client-side check is a courtesy fast-path; the server is the authority; the client must handle rejection gracefully.

## Proposed Solution

### Phase 1: DB resilience (high priority)

**1a. Startup integrity check**
- Add `PRAGMA quick_check` in `init_db` before running any migrations
- Use `signal.alarm(5)` timeout on macOS to prevent hanging on severely corrupted DBs
- On corruption: log clear error to stderr with manual recovery command, then raise `RuntimeError` (aborts lifespan startup — uvicorn exits with code 3)
- Recovery instructions: `sqlite3 jobs.db .recover | sqlite3 jobs_recovered.db`
- Do NOT auto-recover — `.recover` loses constraints/indexes/triggers and risks producing a subtly wrong database

```python
# In init_db(), before schema DDL:
result = conn.execute("PRAGMA quick_check").fetchone()
if result[0] != "ok":
    log.error("DATABASE CORRUPTION DETECTED: %s", result[0])
    log.error("Recovery: sqlite3 %s .recover | sqlite3 %s_recovered", db_path, db_path)
    raise RuntimeError(f"SQLite corruption: {result[0]}")
```

**1b. Web server + TUI: `init_db` once, `open_db` per-thread** *(highest value change)*
- Match the worker pool pattern: call `init_db` once during `lifespan()` startup
- Change `_get_conn()` to call `open_db()` instead of `init_db()`
- Apply same fix to `job_tui.py:68` — call `init_db` once in `on_mount()`, use `open_db` per-call
- If `init_db` raises in `lifespan()`, do NOT yield — let the exception abort startup (uvicorn exits with code 3)
- This eliminates redundant migration runs and the concurrent-migration race condition

**1c. Connection tracking + graceful shutdown**
- Maintain a plain `set` (not WeakSet) of open connections in `job_web.py`, protected by `threading.Lock`
- Register connections in a `_open_db_tracked()` wrapper in `job_web.py` (not in `job_db.py` — the registry is web-server-specific, not a shared library concern)
- `close_all_connections()`: acquire lock, snapshot set, clear, close each connection outside lock
- Run `PRAGMA wal_checkpoint(PASSIVE)` on a fresh connection before exit (PASSIVE not TRUNCATE — avoids blocking on shared DB)
- Register `atexit.register(close_all_connections)` as safety net for double-SIGINT (which skips lifespan shutdown)
- Replace `os._exit(0)` in `main()` finally block with `sys.exit(0)` — test that uvicorn actually exits
- In `kill()`: close all connections → `os.kill(os.getpid(), signal.SIGTERM)` (let uvicorn handle graceful shutdown)
- In `restart()`: close all connections → WAL checkpoint → then `os.execv`

```python
_connections: set[sqlite3.Connection] = set()
_conn_lock = threading.Lock()

def _register_conn(conn):
    with _conn_lock:
        _connections.add(conn)

def close_all_connections():
    with _conn_lock:
        snapshot = list(_connections)
        _connections.clear()
    for conn in snapshot:
        try:
            conn.close()
        except Exception:
            pass
```

**1d. Pre-migration backup**
- Before `init_db` in `lifespan()`, use `sqlite3.Connection.backup()` to create `jobs.db.backup`
- `conn.backup()` is the only safe way to backup a WAL-mode database — `shutil.copy2` can produce corrupt backups if WAL has uncommitted frames
- No file lock needed — `conn.backup()` handles concurrent access internally
- Set permissions to `0o600` on backup file (personal data)

```python
def _backup_db(source_path: Path, backup_path: Path):
    src = sqlite3.connect(str(source_path))
    dst = sqlite3.connect(str(backup_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    os.chmod(str(backup_path), 0o600)
```

**1e. Raw connection cleanup + leak fixes**
- Four background threads in `job_web.py` (lines 973, 1361, 1431, 1455) create raw `sqlite3.connect()` calls — missing `PRAGMA foreign_keys=ON`
- Convert to use `open_db()` from `job_db.py` for consistency
- Fix connection leaks in `_score_background` and `_score_bg` — add `finally: bg_conn.close()`
- Use `contextlib.closing(open_db(...))` pattern for short-lived connections

**1f. Fix `_kill_port` to SIGTERM-first**
- Currently sends SIGKILL (job_web.py:1476) — another corruption vector
- Change to: send SIGTERM, wait 3 seconds, then escalate to SIGKILL only if process still alive
- Verify the process is a previous instance of this application before killing (check command line via `ps -p {pid} -o command=`)

**1g. Fix `_SUBMISSION_STATUS_MAP`**
- Missing `"needs_manual": "stopped"` — causes silent data corruption if migration re-runs on a restored backup
- Add the mapping to match the legacy status migration at line 329
- Change fallback default from `"submitted"` to `"stopped"` (safer — better to under-count success)

### Phase 2: focus-browser + status-gated endpoint pattern (low priority)

**2a. Fix HTTP status — split 404 vs 409**
- `not row` → genuine 404: `HTTPException(404, f"Job {job_id} not found")`
- `row["status"] != "awaiting_captcha"` → 409: `HTTPException(409, detail={"error": "wrong_status", "current_status": row["status"]})`
- Return `current_status` as dict (not string) so JS can parse programmatically

**2b. Consolidate JS paths**
- Make `_focusBrowser()` delegate to `focusCaptchaBrowser()` — single implementation
- Add in-flight guard (boolean flag) to prevent double-tap on F key firing two concurrent requests

```javascript
let _focusBrowserInFlight = false;
async function focusCaptchaBrowser(jobId) {
  if (_focusBrowserInFlight) return;
  _focusBrowserInFlight = true;
  try {
    await apiCall('POST', `/api/jobs/${jobId}/focus-browser`);
    showToast('Browser focused', 'success');
  } catch (e) {
    if (e.current_status) {
      window.jobs[jobId].status = e.current_status;
      // Re-render to remove stale button
    }
    showToast('Focus failed: ' + e.message, 'error');
  } finally {
    _focusBrowserInFlight = false;
  }
}
```

**2c. Client-side status guard + optimistic update**
- Pre-check `currentJob.status === 'awaiting_captcha'` as fast-path (not a guarantee — server is authority)
- On 409 response: update `window.jobs[jobId].status` with returned `current_status` and re-render
- Show human-readable toast: `"Job is now stopped"` not `"Conflict"`
- Gray out F key in shortcut reference when not applicable

**2d. Pessimistic local update on `stopAllWorkers`**
- After POST success, walk `window.jobs` and transition `awaiting_captcha` → `stopped` immediately
- WebSocket will confirm with authoritative status within 2 seconds — no harm if slightly off

**2e. Fix `_pendingGTimer` leak**
- At app.js:2794, `clearTimeout(_pendingGTimer)` before setting new timeout

## Technical Considerations

- **`PRAGMA quick_check` performance**: Measured 7.79ms on the 4.1MB production DB. Startup-only cost, negligible.
- **`conn.backup()` performance**: Measured ~3ms for 4.1MB DB. Consistent regardless of WAL state.
- **WAL checkpoint**: Use `PASSIVE` not `TRUNCATE` — avoids blocking if worker process has the DB open. `TRUNCATE` requires exclusive access.
- **`signal.alarm(5)` for quick_check timeout**: Simpler than threading-based timeout on macOS. No GIL concerns.
- **`sys.exit(0)` vs `os._exit(0)` in uvicorn**: `sys.exit` raises `SystemExit` which uvicorn catches and handles. Test that the process actually exits with active WebSocket connections. The `atexit` handler is the fallback.
- **Double-SIGINT skips lifespan shutdown**: Uvicorn sets `force_exit=True` on second SIGINT, skipping the lifespan `yield` cleanup. The `atexit` handler catches this path.
- **SQLite 3.50.4 WAL-reset bug**: A 15-year-old data race in WAL checkpoint was fixed in SQLite 3.51.3 (2026-03-03). Current Python 3.14 bundles 3.50.4, which is affected. Risk is low but nonzero. Upgrade when Python bundles 3.51.3+.
- **ALTER TABLE race**: Two threads probing the same missing column simultaneously → both try ALTER → second gets `OperationalError: duplicate column`. The current code does NOT catch this. Phase 1b (init_db once) shrinks the window but doesn't eliminate it for web+worker startup overlap. Add `try/except OperationalError` around each ALTER.
- **Migration idempotency**: `_migrate_archived_status` is already effectively transactional via Python sqlite3's implicit transactions (commit at line 1169, implicit rollback on exception). Add explicit `BEGIN`/`COMMIT` for clarity.

## System-Wide Impact

- **Interaction graph**: `init_db` is called by web (`get_conn`), workers (`WorkerPool.start`), TUI (`_get_conn`), and CLI commands. Changes affect web server AND TUI (both have the per-call bug). Workers already correct.
- **Error propagation**: Currently, `DatabaseError` propagates to WebSocket handler → ASGI exception → connection drops. After fix, corruption is caught at startup before any requests are served. If `init_db` raises in `lifespan()` before `yield`, uvicorn aborts startup and exits with code 3.
- **State lifecycle risks**: Partial migration in `_migrate_archived_status` rolls back via implicit transaction. Explicit `BEGIN`/`COMMIT` makes this clearer.
- **API surface parity**: The 409 pattern applies only to focus-browser in this plan. Generalizing to other status-gated endpoints (approve, reject, etc.) is a follow-up item.

## Acceptance Criteria

### Phase 1: DB resilience
- [ ] `PRAGMA quick_check` runs once at startup; corrupted DB detected before migrations
- [ ] On corruption: clear stderr message with recovery command, RuntimeError aborts startup
- [ ] `init_db` called once at web startup (in `lifespan()`); per-thread connections use `open_db`
- [ ] `init_db` called once at TUI startup; per-call connections use `open_db`
- [ ] Plain `set` + `Lock` connection registry; `close_all_connections()` called at shutdown
- [ ] `atexit.register(close_all_connections)` as safety net for double-SIGINT
- [ ] `os._exit(0)` replaced: `kill()` sends SIGTERM to self, `main()` uses `sys.exit(0)`
- [ ] `os.execv` in `restart()` preceded by connection close + WAL checkpoint (PASSIVE)
- [ ] Pre-migration backup via `conn.backup()` creates `jobs.db.backup` with `0o600` permissions
- [ ] Four raw `sqlite3.connect()` sites converted to `open_db()` with `finally: conn.close()`
- [ ] `_kill_port` sends SIGTERM first, escalates to SIGKILL after 3s
- [ ] `_SUBMISSION_STATUS_MAP` includes `"needs_manual": "stopped"`; default changed to `"stopped"`
- [ ] ALTER TABLE migrations wrapped in `try/except OperationalError` (concurrent race guard)
- [ ] Test: `init_db` on deliberately corrupted DB file logs error and exits cleanly
- [ ] Test: graceful shutdown closes all tracked connections

### Phase 2: focus-browser + UI
- [ ] `focus_browser` returns 404 for missing job, 409 for wrong status with `current_status` in body
- [ ] `_focusBrowser` (F key) delegates to `focusCaptchaBrowser` (single implementation)
- [ ] In-flight guard prevents concurrent focus-browser requests
- [ ] Client-side pre-check for `awaiting_captcha` status (fast-path, not guarantee)
- [ ] On 409: update local `window.jobs` with `current_status`, re-render, show human-readable toast
- [ ] `stopAllWorkers` pessimistically transitions `awaiting_captcha` → `stopped` on success
- [ ] `_pendingGTimer` cleared before setting new timeout (app.js:2794)
- [ ] F key shortcut reference annotated "(awaiting_captcha only)"

## Verification Queries (post-deployment)

```sql
-- Verify no legacy statuses survived
SELECT COUNT(*) FROM jobs
WHERE status IN ('failed', 'skipped_captcha', 'skipped_auth', 'needs_manual', 'archived');
-- Expected: 0

-- Verify archived flag consistency
SELECT status, archived, COUNT(*) FROM jobs GROUP BY status, archived;
-- Expected: no rows with archived=1 AND status='archived'

-- Verify all migration columns exist
SELECT progress, confirmation_method, confirmed_at, email_confirmed,
       notion_sync_status, notion_page_id, total_form_fields,
       fields_filled, fields_skipped, fields_errored, archived,
       jd_fingerprint, failure_type
FROM jobs LIMIT 0;
-- Expected: no error

-- Verify DB integrity
PRAGMA quick_check;
-- Expected: ok
```

**Rollback:** Revert the commit. No data migration rollback needed — no data transformations are introduced by this plan.

## Success Metrics

- Server survives corrupted DB without crashing — exits gracefully with recovery instructions
- No redundant `init_db` calls from web threads (verify via log/counter)
- Zero `DatabaseError` crashes after fix
- focus-browser 404s drop to zero in server logs
- All SIGKILL sites replaced with SIGTERM-first

## Dependencies & Risks

- **Risk**: `sys.exit(0)` in `main()` finally block may cause uvicorn to hang if threads are alive → test with active WebSocket connections; `atexit` handler is the fallback
- **Risk**: `PRAGMA quick_check` could hang on severely corrupted DBs → mitigate with `signal.alarm(5)`
- **Risk**: `_kill_port` SIGTERM may not kill the old process fast enough → 3-second grace period before SIGKILL
- **Known issue**: SQLite 3.50.4 has WAL-reset bug (fixed in 3.51.3) — low risk, upgrade when available
- **Dependency**: Phase 2 JS changes require browser testing — Playwright or manual verification

## Implementation Order

1. **1b** (init_db once, open_db per-thread — web + TUI) — simplest change, biggest impact
2. **1g** (`_SUBMISSION_STATUS_MAP` fix) — one-line data integrity fix
3. **1c** (connection tracking + graceful shutdown) — eliminates corruption root cause
4. **1f** (`_kill_port` SIGTERM-first) — eliminates another corruption vector
5. **1a** (integrity check) — defense-in-depth for already-corrupted DBs
6. **1d** (pre-migration backup via `conn.backup()`) — safety net
7. **1e** (raw connection cleanup + leak fixes) — consistency
8. **2a** (404 vs 409 split) — correct HTTP semantics
9. **2b-2e** (JS consolidation + guards + pessimistic update + timer fix)

## Follow-up Items (out of scope)

- **Migration version tracking**: Adopt `PRAGMA user_version` when migration count exceeds ~50 or first non-idempotent migration arrives
- **Generalize 409 pattern**: Apply to approve, reject, regenerate, and all status-gated endpoints
- **`lost_and_found` table**: Document or clean up the 198 orphaned rows from prior recovery
- **`jobs.db` gitignore**: Add `jobs.db` and `jobs.db.*` to `.gitignore`, remove from tracking
- **Architecture checker**: Add rule that no file outside `job_db.py` may call `sqlite3.connect()` directly
- **`draft_web.py` alignment**: Remove duplicate `_open_db()` wrapper, import from `job_db`
- **Security headers**: Add `X-Frame-Options`, `X-Content-Type-Options`, `Content-Security-Policy`
- **CSRF protection**: Add `Origin` header validation on state-mutating endpoints
- **SQLite upgrade**: Upgrade to 3.51.3+ when Python 3.14 bundles it (WAL-reset bug fix)

## Sources & References

### Internal
- `scripts/job_db.py:295` — `init_db()` function
- `scripts/job_db.py:282` — `open_db()` function
- `scripts/job_db.py:1132` — `_SUBMISSION_STATUS_MAP` (missing `needs_manual`)
- `scripts/job_db.py:1141` — `_migrate_archived_status()` crash site
- `scripts/job_web.py:61` — `_get_conn()` calling `init_db` per-thread
- `scripts/job_web.py:469` — `kill()` with `os._exit(0)`
- `scripts/job_web.py:481` — `restart()` with `os.execv`
- `scripts/job_web.py:607` — `focus_browser()` endpoint
- `scripts/job_web.py:973,1361,1431,1455` — raw `sqlite3.connect()` sites with connection leaks
- `scripts/job_web.py:1476` — `_kill_port` SIGKILL
- `scripts/job_worker.py:491-493` — worker pattern: `init_db` once, `open_db` per-thread
- `scripts/job_tui.py:68` — TUI `_get_conn()` calling `init_db` per-call
- `scripts/static/app.js:1538` — `focusCaptchaBrowser()` button handler
- `scripts/static/app.js:2939` — `_focusBrowser()` keyboard handler (silent error swallowing)
- `scripts/static/app.js:2794` — `_pendingGTimer` leak
- `docs/solutions/integration-issues/adding-new-llm-provider.md` — institutional learning: audit all DB write paths
- Evidence: `jobs.db.corrupt.bak`, `lost_and_found` table (198 rows from prior recovery)

### External
- [SQLite WAL Mode](https://sqlite.org/wal.html) — WAL corruption causes and prevention
- [How To Corrupt An SQLite Database](https://sqlite.org/howtocorrupt.html) — official corruption vectors
- [SQLite Recovery](https://sqlite.org/recovery.html) — `.recover` limitations and expectations
- [SQLite Backup API](https://sqlite.org/backup.html) — safe backup during concurrent access
- [FastAPI Lifespan Events](https://fastapi.tiangolo.com/advanced/events/) — startup/shutdown pattern
- [Python sqlite3 docs](https://docs.python.org/3/library/sqlite3.html) — `conn.backup()`, threading modes
- [CPython #123089](https://github.com/python/cpython/issues/123089) — WeakSet thread safety (fixed in 3.14)
- [SQLite 3.51.3 WAL Fix](https://sqlite.org/releaselog/3_51_3.html) — WAL-reset data race fix
