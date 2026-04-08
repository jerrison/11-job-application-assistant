# Submitted Job Lock And Resubmission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent previously submitted jobs from drifting back into draft/queue unless explicitly unlocked, preserve submission history everywhere, relock automatically after a confirmed resubmission, and keep the existing Notion page anchored to the first application date.

**Architecture:** Add explicit submission-lock columns and a small lock helper in `scripts/job_db.py`, then route every rerun/requeue path through that helper or a repair helper. Web/API and UI expose one explicit `Unlock to Resubmit` action, while worker resets, retry logic, disk sync, and Notion sync preserve prior submission history and repair legacy corrupted rows back to `submitted`.

**Tech Stack:** Python (SQLite, FastAPI), JavaScript (vanilla `app.js`), Notion API sync utilities, Playwright/manual browser verification

---

## File Map

| File | Responsibility |
| --- | --- |
| `scripts/job_db.py` | Add lock-state columns/migrations, lock helpers, transition enforcement, stale-row repair, and confirmed-submission persistence |
| `scripts/pipeline_orchestrator.py` | Guard direct rerun/requeue helpers and centralize relock-after-confirmation behavior |
| `scripts/job_worker.py` | Ensure startup reset and killed-worker requeue logic repair locked rows instead of requeueing them |
| `scripts/job_web.py` | Add `unlock-resubmit` endpoint and HTTP 409 guards for rerun actions on locked jobs |
| `scripts/static/index.html` | Reserve a detail-header slot for explicit lock badges |
| `scripts/static/app.js` | Show lock badges, surface unlock action, and suppress rerun buttons while locked |
| `scripts/notion_job_applications.py` | Preserve original `Application Date`, merge resubmission history into `Notes`, and append body history blocks to the same page |
| `tests/test_job_db.py` | Cover migrations, backfill, lock helpers, transition refusal, repair, and disk-sync relock behavior |
| `tests/test_job_worker.py` | Cover worker claim/kill/startup behavior for locked submitted jobs |
| `tests/test_job_web.py` | Cover locked HTTP refusal, unlock flow, and returned lock metadata |
| `tests/test_pipeline_orchestrator.py` | Cover transient retry refusal for locked jobs and confirmed resubmission relock helper |
| `tests/test_notion_sync.py` | Cover preserved application date and appended resubmission notes/body history |
| `docs/worker-pipeline-patterns.md` | Document the submitted-lock lifecycle in worker/reset behavior |
| `docs/launch-modes.md` | Document that Notion `Application Date` stays on the first confirmed submission |

---

### Task 1: Add submission-lock schema and helper primitives

**Files:**
- Modify: `scripts/job_db.py`
- Test: `tests/test_job_db.py`

- [ ] **Step 1: Write failing schema/backfill and unlock-helper tests**

Extend the import list in `tests/test_job_db.py` to include `unlock_job_for_resubmit`, then add:

```python
def test_init_db_backfills_submission_lock_for_confirmed_jobs(tmp_path):
    db_path = tmp_path / "legacy_jobs.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(
        f"""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            confirmed_at TIMESTAMP,
            retry_after TIMESTAMP NOT NULL DEFAULT '{RETRY_AFTER_SENTINEL}',
            archived BOOLEAN DEFAULT FALSE
        );
        INSERT INTO jobs (id, url, status, confirmed_at) VALUES
            (1, 'https://example.com/locked', 'submitted', '2026-03-18T17:11:18+00:00'),
            (2, 'https://example.com/open', 'queued', NULL);
        """
    )
    raw.commit()
    raw.close()

    conn = init_db(db_path)

    locked = conn.execute("SELECT submission_lock_state FROM jobs WHERE id = 1").fetchone()
    open_row = conn.execute("SELECT submission_lock_state FROM jobs WHERE id = 2").fetchone()

    assert locked["submission_lock_state"] == "locked"
    assert open_row["submission_lock_state"] == "open"


def test_unlock_job_for_resubmit_records_unlock_metadata(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/unlockable")
    db.execute(
        "UPDATE jobs SET status = 'submitted', confirmed_at = ?, submission_lock_state = 'locked' WHERE id = ?",
        ("2026-03-18T17:11:18+00:00", job_id),
    )
    db.commit()

    assert unlock_job_for_resubmit(db, job_id, initiator="web") is True

    job = get_job(db, job_id)
    assert job["submission_lock_state"] == "unlocked_for_resubmit"
    assert job["last_resubmit_unlocked_at"] is not None
    assert job["last_resubmit_unlock_initiator"] == "web"
```

- [ ] **Step 2: Run the targeted DB tests to verify they fail first**

Run:

```bash
uv run python -m pytest tests/test_job_db.py -k "submission_lock or unlock_job_for_resubmit" -v
```

Expected:
- `FAIL` because `submission_lock_state` does not exist yet
- `FAIL` or `ImportError` because `unlock_job_for_resubmit` is not implemented yet

- [ ] **Step 3: Add schema columns, migrations, backfill, and unlock helper**

In `scripts/job_db.py`, extend `_SCHEMA` and `_MIGRATIONS`, then add the helper constants/functions:

```python
SUBMISSION_LOCK_STATES = ("open", "locked", "unlocked_for_resubmit")
RERUNNABLE_PIPELINE_STATUSES = frozenset(
    {
        "queued",
        "queued_submit",
        "approved",
        "reanswering",
        "resolving",
        "generating",
        "autofilling",
        "draft",
        "submitting",
        "regenerating",
    }
)


def _submission_lock_state(row: sqlite3.Row | dict | None) -> str:
    if not row:
        return "open"
    state = str((row["submission_lock_state"] if "submission_lock_state" in row.keys() else None) or "").strip()
    if state in SUBMISSION_LOCK_STATES:
        return state
    confirmed_at = row["confirmed_at"] if "confirmed_at" in row.keys() else None
    return "locked" if confirmed_at else "open"


def backfill_submission_locks(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE jobs SET submission_lock_state = 'locked' "
        "WHERE confirmed_at IS NOT NULL AND COALESCE(submission_lock_state, 'open') = 'open'"
    )
    conn.execute(
        "UPDATE jobs SET submission_lock_state = 'open' "
        "WHERE confirmed_at IS NULL AND submission_lock_state IS NULL"
    )
    conn.commit()


def unlock_job_for_resubmit(conn: sqlite3.Connection, job_id: int, *, initiator: str) -> bool:
    cur = conn.execute(
        "UPDATE jobs SET submission_lock_state = 'unlocked_for_resubmit', "
        "last_resubmit_unlocked_at = CURRENT_TIMESTAMP, "
        "last_resubmit_unlock_initiator = ? "
        "WHERE id = ? AND COALESCE(submission_lock_state, 'open') = 'locked'",
        (initiator, job_id),
    )
    conn.commit()
    if cur.rowcount > 0:
        log_event(conn, job_id, "submission_unlocked_for_resubmit", initiator=initiator)
    return cur.rowcount > 0
```

Add the new columns in order near the existing confirmation fields:

```python
("submission_lock_state", "ALTER TABLE jobs ADD COLUMN submission_lock_state TEXT NOT NULL DEFAULT 'open'"),
("resubmit_count", "ALTER TABLE jobs ADD COLUMN resubmit_count INTEGER NOT NULL DEFAULT 0"),
("last_resubmit_unlocked_at", "ALTER TABLE jobs ADD COLUMN last_resubmit_unlocked_at TIMESTAMP"),
("last_resubmit_unlock_initiator", "ALTER TABLE jobs ADD COLUMN last_resubmit_unlock_initiator TEXT"),
("last_resubmit_confirmed_at", "ALTER TABLE jobs ADD COLUMN last_resubmit_confirmed_at TIMESTAMP"),
```

Call `backfill_submission_locks(conn)` once in `init_db()` after the column migrations complete.

- [ ] **Step 4: Re-run the targeted DB tests**

Run:

```bash
uv run python -m pytest tests/test_job_db.py -k "submission_lock or unlock_job_for_resubmit" -v
```

Expected:
- Both new tests `PASS`

- [ ] **Step 5: Commit the schema/helper slice**

```bash
git add scripts/job_db.py tests/test_job_db.py
git commit -m "feat: add submission lock schema and unlock metadata"
```

---

### Task 2: Enforce the lock in status transitions and repair stale locked rows

**Files:**
- Modify: `scripts/job_db.py`
- Test: `tests/test_job_db.py`

- [ ] **Step 1: Write failing transition and repair tests**

Add imports for `SubmissionLockError` and `get_job_timeline`, then add:

```python
def test_get_pending_jobs_skips_locked_jobs_even_if_status_is_queued(db):
    locked_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/locked-queued")
    open_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/open-queued", priority=5)
    db.execute(
        "UPDATE jobs SET status = 'queued', confirmed_at = ?, submission_lock_state = 'locked' WHERE id = ?",
        ("2026-03-18T17:11:18+00:00", locked_id),
    )
    db.commit()

    pending = get_pending_jobs(db)

    assert [job["id"] for job in pending] == [open_id]


def test_update_status_refuses_locked_rerunnable_transition(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/locked-transition")
    db.execute(
        "UPDATE jobs SET status = 'submitted', confirmed_at = ?, submission_lock_state = 'locked' WHERE id = ?",
        ("2026-03-18T17:11:18+00:00", job_id),
    )
    db.commit()

    with pytest.raises(SubmissionLockError):
        update_status(db, job_id, "draft")


def test_reset_stale_jobs_repairs_locked_rerunnable_rows(db):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/locked-repair")
    db.execute(
        "UPDATE jobs SET status = 'draft', confirmed_at = ?, submission_lock_state = 'locked', progress = 'stale' "
        "WHERE id = ?",
        ("2026-03-18T17:11:18+00:00", job_id),
    )
    db.commit()

    ids = reset_stale_jobs(db, stale_threshold_seconds=0)

    job = get_job(db, job_id)
    events = get_job_timeline(db, job_id)
    assert job_id in ids
    assert job["status"] == "submitted"
    assert job["submission_lock_state"] == "locked"
    assert job["progress"] == ""
    assert any(event["event_type"] == "submission_lock_repaired" for event in events)
```

- [ ] **Step 2: Run the transition/repair tests and confirm failure**

Run:

```bash
uv run python -m pytest tests/test_job_db.py -k "locked_jobs or locked_rerunnable or locked_repair" -v
```

Expected:
- `FAIL` because `get_pending_jobs()` still returns locked rows
- `FAIL` because `update_status()` currently allows `submitted -> draft`
- `FAIL` because `reset_stale_jobs()` currently leaves locked rows in rerunnable states

- [ ] **Step 3: Add `SubmissionLockError`, transition enforcement, and repair helpers**

In `scripts/job_db.py`, add:

```python
class SubmissionLockError(RuntimeError):
    """Raised when a submitted-and-locked job is asked to re-enter the pipeline."""


def _status_requires_submission_unlock(status: str) -> bool:
    return status in RERUNNABLE_PIPELINE_STATUSES


def repair_submission_locked_job(conn: sqlite3.Connection, job_id: int, *, initiator: str = "system") -> bool:
    placeholders = ",".join("?" * len(RERUNNABLE_PIPELINE_STATUSES))
    cur = conn.execute(
        "UPDATE jobs SET status = 'submitted', provider = NULL, progress = '', retry_after = ?, "
        "submission_lock_state = 'locked', completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP) "
        f"WHERE id = ? AND COALESCE(submission_lock_state, CASE WHEN confirmed_at IS NOT NULL THEN 'locked' ELSE 'open' END) = 'locked' "
        f"AND status IN ({placeholders})",
        (RETRY_AFTER_SENTINEL, job_id, *sorted(RERUNNABLE_PIPELINE_STATUSES)),
    )
    conn.commit()
    if cur.rowcount > 0:
        log_event(conn, job_id, "submission_lock_repaired", initiator=initiator)
    return cur.rowcount > 0


def enforce_submission_lock(conn: sqlite3.Connection, job_id: int, *, target_status: str) -> None:
    if not _status_requires_submission_unlock(target_status):
        return
    row = conn.execute(
        "SELECT id, status, confirmed_at, submission_lock_state FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if not row or _submission_lock_state(row) != "locked":
        return
    if row["status"] != "submitted":
        repair_submission_locked_job(conn, job_id, initiator="system")
    raise SubmissionLockError(
        f"Job #{job_id} was already submitted and is locked. Unlock it before redrafting or resubmitting."
    )
```

Hook it into the status layer:

```python
def update_status(
    conn: sqlite3.Connection,
    job_id: int,
    status: str,
    *,
    error_message: str | None = None,
    progress: str | None = None,
    provider: str | None = None,
    clear_provider: bool = False,
    board: str | None = None,
    board_url: str | None = None,
    canonical_url: str | None = None,
    company: str | None = None,
    role_title: str | None = None,
    output_dir: str | None = None,
    notion_url: str | None = None,
    archived: bool | None = None,
    failure_type: str | None = None,
    auth_state: str | None = None,
    auth_scope: str | None = None,
    retry_after: str | None = None,
    initiator: str | None = None,
    process_info: str | None = None,
) -> None:
    enforce_submission_lock(conn, job_id, target_status=status)
    sets = ["status = ?"]
```

Filter locked rows out of the worker queue:

```python
AND COALESCE(submission_lock_state, CASE WHEN confirmed_at IS NOT NULL THEN 'locked' ELSE 'open' END) != 'locked'
```

Repair stale corrupted rows before the existing reset logic runs:

```python
def reset_stale_jobs(
    conn: sqlite3.Connection,
    stale_threshold_seconds: int = 1800,
) -> list[int]:
    repaired_rows = conn.execute(
        "SELECT id FROM jobs WHERE "
        "COALESCE(submission_lock_state, CASE WHEN confirmed_at IS NOT NULL THEN 'locked' ELSE 'open' END) = 'locked' "
        "AND status IN ('queued', 'queued_submit', 'approved', 'reanswering', 'resolving', 'generating', "
        "'autofilling', 'draft', 'submitting', 'regenerating')"
    ).fetchall()
    ids = []
    for row in repaired_rows:
        if repair_submission_locked_job(conn, row["id"], initiator="system"):
            ids.append(row["id"])
```

- [ ] **Step 4: Re-run the DB lock-enforcement tests**

Run:

```bash
uv run python -m pytest tests/test_job_db.py -k "locked_jobs or locked_rerunnable or locked_repair" -v
```

Expected:
- All three tests `PASS`

- [ ] **Step 5: Commit the transition-enforcement slice**

```bash
git add scripts/job_db.py tests/test_job_db.py
git commit -m "fix: enforce submission lock in status transitions"
```

---

### Task 3: Guard worker kill/startup and orchestrator retry paths from requeueing locked jobs

**Files:**
- Modify: `scripts/job_worker.py`
- Modify: `scripts/pipeline_orchestrator.py`
- Test: `tests/test_job_worker.py`
- Test: `tests/test_pipeline_orchestrator.py`

- [ ] **Step 1: Write failing worker and retry tests**

In `tests/test_job_worker.py`, add:

```python
@patch("job_worker.process_job")
@patch("job_worker.reset_stale_jobs", return_value=[])
def test_killed_worker_repairs_locked_job_instead_of_requeueing(self, mock_reset, mock_process, db, db_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/locked-kill")
    db.execute(
        "UPDATE jobs SET status = 'queued', confirmed_at = ?, submission_lock_state = 'locked' WHERE id = ?",
        ("2026-03-18T17:11:18+00:00", job_id),
    )
    db.commit()

    pool = WorkerPool(db_path, num_workers=1, headless=True)
    try:
        pool._worker_stop_events[1] = threading.Event()
        pool._worker_registry[1] = {
            "worker_id": 1,
            "status": "busy",
            "job_id": job_id,
            "company": "",
            "role_title": "",
            "board": "greenhouse",
            "phase": "generating",
            "start_time": None,
            "progress": "",
        }

        pool._kill_single_worker(1)

        row = db.execute("SELECT status, submission_lock_state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["status"] == "submitted"
        assert row["submission_lock_state"] == "locked"
    finally:
        pool.stop()
```

In `tests/test_pipeline_orchestrator.py`, update `_make_in_memory_db()` with the new columns and add:

```python
def test_auto_retry_refuses_locked_job_requeue():
    conn = _make_in_memory_db()
    conn.execute(
        "INSERT INTO jobs (id, url, status, fix_attempts, confirmed_at, submission_lock_state) "
        "VALUES (1, 'http://x', 'stopped', 0, '2026-03-18T17:11:18+00:00', 'locked')"
    )
    conn.commit()

    result = _auto_retry_if_transient(conn, 1, "auto-fix slot busy")

    row = conn.execute("SELECT status, submission_lock_state FROM jobs WHERE id = 1").fetchone()
    assert result == "submitted"
    assert row["status"] == "submitted"
    assert row["submission_lock_state"] == "locked"
```

- [ ] **Step 2: Run the worker/orchestrator tests and verify failure**

Run:

```bash
uv run python -m pytest tests/test_job_worker.py tests/test_pipeline_orchestrator.py -k "locked_job or locked_requeue" -v
```

Expected:
- `FAIL` because `_kill_single_worker()` still requeues the row
- `FAIL` because `_auto_retry_if_transient()` still writes `queued`

- [ ] **Step 3: Reuse the lock helper in worker kill and retry code**

In `scripts/job_worker.py`, import `repair_submission_locked_job` and short-circuit `_kill_single_worker()` before any `queued` or `draft` fallback:

```python
row = conn.execute(
    "SELECT submission_lock_state, confirmed_at FROM jobs WHERE id = ?",
    (current_job_id,),
).fetchone()
state = str((row["submission_lock_state"] if row else None) or ("locked" if row and row["confirmed_at"] else "open"))
if state == "locked":
    repair_submission_locked_job(conn, current_job_id, initiator="system")
    log.info("Repaired locked submitted job %d from killed worker %d", current_job_id, worker_id)
    return
```

In `scripts/pipeline_orchestrator.py`, import `SubmissionLockError`, `enforce_submission_lock`, and `repair_submission_locked_job`, then guard the direct `queued` CAS writes:

```python
try:
    enforce_submission_lock(conn, job_id, target_status="queued")
except SubmissionLockError:
    repair_submission_locked_job(conn, job_id, initiator="worker")
    log_event(conn, job_id, "submission_lock_refused", detail="queued", initiator="worker")
    return "submitted"
```

Apply that pattern in:
- `_auto_retry_if_transient()`
- `_handle_linkedin_failure_result()` targeted retry branch
- `regenerate_job()` before its direct `queued` update
- `approve_job()` before its direct `approved` update

Also extend `_make_in_memory_db()` in `tests/test_pipeline_orchestrator.py`:

```python
confirmed_at TIMESTAMP,
submission_lock_state TEXT NOT NULL DEFAULT 'open',
resubmit_count INTEGER NOT NULL DEFAULT 0,
last_resubmit_unlocked_at TIMESTAMP,
last_resubmit_unlock_initiator TEXT,
last_resubmit_confirmed_at TIMESTAMP,
completed_at TIMESTAMP,
```

- [ ] **Step 4: Re-run the worker/orchestrator lock tests**

Run:

```bash
uv run python -m pytest tests/test_job_worker.py tests/test_pipeline_orchestrator.py -k "locked_job or locked_requeue" -v
```

Expected:
- Both new tests `PASS`

- [ ] **Step 5: Commit the worker/orchestrator safety slice**

```bash
git add scripts/job_worker.py scripts/pipeline_orchestrator.py tests/test_job_worker.py tests/test_pipeline_orchestrator.py
git commit -m "fix: block worker and retry requeues for locked jobs"
```

---

### Task 4: Add the unlock-resubmit API and reject locked rerun actions with HTTP 409

**Files:**
- Modify: `scripts/job_web.py`
- Test: `tests/test_job_web.py`

- [ ] **Step 1: Write failing API tests for lock refusal and unlock flow**

Add:

```python
def test_locked_restart_pipeline_returns_409(client, tmp_path):
    from job_db import get_job

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/locked-restart"]})
    import job_web

    conn = job_web.get_conn()
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()
    conn.execute(
        "UPDATE jobs SET status = 'submitted', output_dir = ?, confirmed_at = ?, submission_lock_state = 'locked' "
        "WHERE id = 1",
        (str(out_dir), "2026-03-18T17:11:18+00:00"),
    )
    conn.commit()

    resp = client.post("/api/jobs/1/restart-pipeline", json={"auto_submit": False})

    assert resp.status_code == 409
    assert "Unlock it before redrafting or resubmitting" in resp.text
    assert get_job(conn, 1)["status"] == "submitted"


def test_locked_reanswer_returns_409(client, tmp_path):
    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/locked-reanswer"]})
    import job_web

    conn = job_web.get_conn()
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()
    conn.execute(
        "UPDATE jobs SET status = 'submitted', output_dir = ?, confirmed_at = ?, submission_lock_state = 'locked' "
        "WHERE id = 1",
        (str(out_dir), "2026-03-18T17:11:18+00:00"),
    )
    conn.commit()

    resp = client.post("/api/jobs/1/reanswer")

    assert resp.status_code == 409


def test_unlock_resubmit_endpoint_allows_restart_after_unlock(client, tmp_path):
    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/unlock-flow"]})
    import job_web

    conn = job_web.get_conn()
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()
    conn.execute(
        "UPDATE jobs SET status = 'submitted', output_dir = ?, confirmed_at = ?, submission_lock_state = 'locked' "
        "WHERE id = 1",
        (str(out_dir), "2026-03-18T17:11:18+00:00"),
    )
    conn.commit()

    unlock_resp = client.post("/api/jobs/1/unlock-resubmit")
    restart_resp = client.post("/api/jobs/1/restart-pipeline", json={"auto_submit": False})

    row = conn.execute("SELECT status, submission_lock_state FROM jobs WHERE id = 1").fetchone()
    assert unlock_resp.status_code == 200
    assert unlock_resp.json()["status"] == "unlocked_for_resubmit"
    assert restart_resp.status_code == 200
    assert row["status"] == "queued"
    assert row["submission_lock_state"] == "unlocked_for_resubmit"
```

- [ ] **Step 2: Run the API tests and confirm failure**

Run:

```bash
uv run python -m pytest tests/test_job_web.py -k "locked_restart or locked_reanswer or unlock_flow" -v
```

Expected:
- `FAIL` because the routes still return `200`
- `FAIL` because `/api/jobs/{job_id}/unlock-resubmit` does not exist yet

- [ ] **Step 3: Add a route helper that maps `SubmissionLockError` to HTTP 409**

In `scripts/job_web.py`, import `SubmissionLockError`, `enforce_submission_lock`, and `unlock_job_for_resubmit`, then add:

```python
def _enforce_submission_unlock(conn, job_id: int, *, target_status: str, initiator: str = "web") -> None:
    try:
        enforce_submission_lock(conn, job_id, target_status=target_status)
    except SubmissionLockError as exc:
        log_event(conn, job_id, "submission_lock_refused", detail=target_status, initiator=initiator)
        raise HTTPException(409, str(exc)) from exc
```

Call it before routes that re-enter the pipeline:

```python
@app.post("/api/jobs/{job_id}/unlock-resubmit")
def unlock_resubmit(job_id: int):
    conn = get_conn()
    job = get_job(conn, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not unlock_job_for_resubmit(conn, job_id, initiator="web"):
        raise HTTPException(409, "Job is not submission-locked")
    return {"status": "unlocked_for_resubmit"}
```

Use `_enforce_submission_unlock()` in:
- `approve()` with `target_status="approved"`
- `regenerate()` with `target_status="queued"`
- `regenerate_asset()` with `target_status="regenerating"` for `resume`/`cover_letter` and `target_status="reanswering"` for `answers`
- `reanswer()` with `target_status="reanswering"`
- `retry()` with `target_status="queued"`
- `restart_pipeline()` with `target_status="queued"` or `queued_submit`
- `save_draft_overrides()` before writing `reanswering`

- [ ] **Step 4: Re-run the API tests**

Run:

```bash
uv run python -m pytest tests/test_job_web.py -k "locked_restart or locked_reanswer or unlock_flow" -v
```

Expected:
- All new route tests `PASS`

- [ ] **Step 5: Commit the API slice**

```bash
git add scripts/job_web.py tests/test_job_web.py
git commit -m "feat: add unlock-resubmit API guards"
```

---

### Task 5: Relock automatically after confirmed resubmission and preserve first confirmed timestamp

**Files:**
- Modify: `scripts/job_db.py`
- Modify: `scripts/pipeline_orchestrator.py`
- Test: `tests/test_job_db.py`
- Test: `tests/test_pipeline_orchestrator.py`

- [ ] **Step 1: Write failing relock tests**

In `tests/test_job_db.py`, add:

```python
def test_sync_job_from_disk_relocks_unlocked_resubmission_without_overwriting_first_confirmed_at(db, tmp_path):
    job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/resubmit-sync")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    update_status(db, job_id, "draft", output_dir=str(out_dir))
    db.execute(
        "UPDATE jobs SET confirmed_at = ?, submission_lock_state = 'unlocked_for_resubmit', "
        "last_resubmit_unlocked_at = ?, resubmit_count = 0 WHERE id = ?",
        ("2026-03-18T17:11:18+00:00", "2026-03-30T03:45:00+00:00", job_id),
    )
    db.commit()
    (submit_dir / "application_submission_result.json").write_text(
        json.dumps(
            {
                "status": "confirmed",
                "website_confirmed": True,
                "confirmed_at_utc": "2026-03-30T04:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    sync_job_from_disk(db, job_id)

    job = get_job(db, job_id)
    assert job["submission_lock_state"] == "locked"
    assert job["confirmed_at"] == "2026-03-18T17:11:18+00:00"
    assert job["last_resubmit_confirmed_at"] == "2026-03-30T04:00:00+00:00"
    assert job["resubmit_count"] == 1
```

In `tests/test_pipeline_orchestrator.py`, add:

```python
def test_finalize_successful_submission_relocks_unlocked_job(tmp_path):
    import pipeline_orchestrator

    conn = _make_in_memory_db()
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "application_confirmation_website.json").write_text(
        json.dumps({"website_confirmed": True, "confirmed_at_utc": "2026-03-30T04:00:00+00:00"}),
        encoding="utf-8",
    )
    conn.execute(
        "INSERT INTO jobs (id, url, status, output_dir, confirmed_at, submission_lock_state, resubmit_count) "
        "VALUES (1, 'http://x', 'submitting', ?, '2026-03-18T17:11:18+00:00', 'unlocked_for_resubmit', 0)",
        (str(out_dir),),
    )
    conn.commit()

    with patch.object(pipeline_orchestrator, "_post_submit", return_value=None):
        result = pipeline_orchestrator._finalize_successful_submission(
            conn,
            1,
            out_dir,
            "https://boards.example/jobs/1",
        )

    row = conn.execute(
        "SELECT status, submission_lock_state, confirmed_at, last_resubmit_confirmed_at, resubmit_count "
        "FROM jobs WHERE id = 1"
    ).fetchone()
    assert result == "submitted"
    assert row["status"] == "submitted"
    assert row["submission_lock_state"] == "locked"
    assert row["confirmed_at"] == "2026-03-18T17:11:18+00:00"
    assert row["last_resubmit_confirmed_at"] == "2026-03-30T04:00:00+00:00"
    assert row["resubmit_count"] == 1
```

- [ ] **Step 2: Run the relock tests and verify failure**

Run:

```bash
uv run python -m pytest tests/test_job_db.py tests/test_pipeline_orchestrator.py -k "relocks_unlocked or finalize_successful_submission" -v
```

Expected:
- `FAIL` because `sync_job_from_disk()` overwrites `confirmed_at` directly
- `FAIL` because `_finalize_successful_submission()` does not exist yet

- [ ] **Step 3: Add a single helper that records confirmed submission history correctly**

In `scripts/job_db.py`, add:

```python
def record_confirmed_submission(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    confirmed_at: str | None,
    initiator: str | None = None,
) -> None:
    row = conn.execute(
        "SELECT confirmed_at, submission_lock_state, resubmit_count FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if not row:
        return

    sets = [
        "status = 'submitted'",
        "completed_at = CURRENT_TIMESTAMP",
        "submission_lock_state = 'locked'",
        "error_message = NULL",
        "failure_type = NULL",
        "auth_state = NULL",
        "auth_scope = NULL",
        "progress = ''",
        "retry_after = ?",
    ]
    params: list[object] = [RETRY_AFTER_SENTINEL]

    first_confirmed = row["confirmed_at"]
    if first_confirmed is None and confirmed_at:
        sets.append("confirmed_at = ?")
        params.append(confirmed_at)
    elif (
        first_confirmed is not None
        and row["submission_lock_state"] == "unlocked_for_resubmit"
        and confirmed_at
    ):
        sets.append("last_resubmit_confirmed_at = ?")
        params.append(confirmed_at)
        sets.append("resubmit_count = COALESCE(resubmit_count, 0) + 1")

    params.append(job_id)
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()
    log_event(conn, job_id, "submission_locked", detail=confirmed_at, initiator=initiator)
    if first_confirmed is not None and confirmed_at and row["submission_lock_state"] == "unlocked_for_resubmit":
        log_event(conn, job_id, "resubmitted", detail=confirmed_at, initiator=initiator)
```

Update `sync_job_from_disk()` so confirmation artifacts call `record_confirmed_submission()` instead of directly overwriting `confirmed_at` or `status`.

- [ ] **Step 4: Add `_finalize_successful_submission()` and use it for all successful submit paths**

In `scripts/pipeline_orchestrator.py`, add:

```python
def _confirmed_submission_timestamp(output_dir: Path | str | None) -> str | None:
    from output_layout import preferred_submit_dir_name_for_post_submit

    if not output_dir:
        return None
    submit_name = preferred_submit_dir_name_for_post_submit(output_dir) or "submit"
    submit_dir = Path(output_dir) / submit_name
    for name in ("application_confirmation_website.json", "application_submission_result.json", "notion_sync_status.json"):
        path = submit_dir / name
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        confirmed_at = payload.get("confirmed_at_utc") or payload.get("confirmed_at")
        if payload.get("website_confirmed") or payload.get("status") == "synced":
            return confirmed_at
    return None


def _finalize_successful_submission(conn, job_id: int, output_dir: Path | str | None, board_url: str) -> str:
    from job_db import record_confirmed_submission, update_status

    _post_submit(conn, job_id, output_dir, board_url)
    confirmed_at = _confirmed_submission_timestamp(output_dir)
    if not confirmed_at:
        update_status(
            conn,
            job_id,
            "stopped",
            error_message="Submit clicked but no confirmation detected — verify manually",
            failure_type="submit_failed",
        )
        log_event(conn, job_id, "submit_unconfirmed", initiator="worker")
        return "stopped"

    record_confirmed_submission(conn, job_id, confirmed_at=confirmed_at, initiator="worker")
    log_event(conn, job_id, "submitted", initiator="worker")
    return "submitted"
```

Replace the three successful submit branches that currently call `update_status(conn, job_id, "submitted")` directly:
- main confirmation path
- auto-fix success with `auto_submit`
- `retry_with_recording()` success with `auto_submit`

- [ ] **Step 5: Re-run the relock tests**

Run:

```bash
uv run python -m pytest tests/test_job_db.py tests/test_pipeline_orchestrator.py -k "relocks_unlocked or finalize_successful_submission" -v
```

Expected:
- Both new tests `PASS`

- [ ] **Step 6: Commit the confirmed-resubmission slice**

```bash
git add scripts/job_db.py scripts/pipeline_orchestrator.py tests/test_job_db.py tests/test_pipeline_orchestrator.py
git commit -m "fix: relock confirmed resubmissions without losing first submit history"
```

---

### Task 6: Preserve the original Notion application date and append resubmission history to the same page

**Files:**
- Modify: `scripts/notion_job_applications.py`
- Test: `tests/test_notion_sync.py`

- [ ] **Step 1: Write failing Notion tests for preserved date and history notes**

Add:

```python
def test_page_properties_preserve_existing_application_date_for_existing_page(self):
    notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
    schema = {
        "properties": {
            "Name": {"type": "title"},
            "Status": {"type": "status"},
            "Application Date": {"type": "date"},
            "Notes": {"type": "rich_text"},
            "Position": {"type": "rich_text"},
        }
    }
    existing_page = {
        "properties": {
            "Application Date": {"type": "date", "date": {"start": "2026-03-18T17:11:18+00:00"}},
            "Notes": {"type": "rich_text", "rich_text": [{"plain_text": "Applied via automation."}]},
        }
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        out_dir = Path(tmpdir)
        (out_dir / "content").mkdir()
        (out_dir / "content" / "jd_parsed.json").write_text(json.dumps({}), encoding="utf-8")
        properties = notion_sync._page_properties(
            schema,
            meta={"jd_title": "Senior Product Manager", "company_proper": "Valon Tech"},
            out_dir=out_dir,
            website_confirmation={"confirmed_at_utc": "2026-03-30T04:00:00+00:00", "url": "https://example.com/confirmation"},
            email_confirmation=None,
            existing_page=existing_page,
            submission_history={
                "confirmed_at": "2026-03-18T17:11:18+00:00",
                "last_resubmit_unlocked_at": "2026-03-30T03:45:00+00:00",
                "last_resubmit_confirmed_at": "2026-03-30T04:00:00+00:00",
                "resubmit_count": 1,
            },
        )

    self.assertEqual(properties["Application Date"]["date"]["start"], "2026-03-18T17:11:18+00:00")


def test_notes_text_includes_resubmission_history_lines(self):
    notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
    notes = notion_sync._notes_text(
        Path("/tmp/valon"),
        {"confirmed_at_utc": "2026-03-30T04:00:00+00:00", "url": "https://example.com/confirmation"},
        {"subject": "Thanks for applying", "date": "Mon, 30 Mar 2026 04:01:00 +0000"},
        submission_history={
            "confirmed_at": "2026-03-18T17:11:18+00:00",
            "last_resubmit_unlocked_at": "2026-03-30T03:45:00+00:00",
            "last_resubmit_confirmed_at": "2026-03-30T04:00:00+00:00",
            "resubmit_count": 1,
        },
        existing_notes="Applied via automation.",
    )

    self.assertIn("Originally applied: 2026-03-18T17:11:18+00:00", notes)
    self.assertIn("Unlocked for resubmit: 2026-03-30T03:45:00+00:00", notes)
    self.assertIn("Resubmit count: 1", notes)
```

- [ ] **Step 2: Run the Notion tests and confirm failure**

Run:

```bash
uv run python -m pytest tests/test_notion_sync.py -k "preserve_existing_application_date or resubmission_history_lines" -v
```

Expected:
- `FAIL` because `_page_properties()` does not accept `existing_page` or `submission_history`
- `FAIL` because `_notes_text()` does not merge existing notes or resubmission lines yet

- [ ] **Step 3: Add submission-history lookup and merge helpers**

In `scripts/notion_job_applications.py`, add:

```python
def _load_submission_history_for_output(out_dir: Path) -> dict:
    from job_db import open_db

    db_path = PROJECT_ROOT / "jobs.db"
    if not db_path.exists():
        return {}
    conn = open_db(db_path)
    try:
        row = conn.execute(
            "SELECT confirmed_at, last_resubmit_unlocked_at, last_resubmit_confirmed_at, resubmit_count "
            "FROM jobs WHERE output_dir = ? ORDER BY id DESC LIMIT 1",
            (str(out_dir),),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _existing_date_value(existing_page: dict | None, schema: dict, aliases: tuple[str, ...]) -> str | None:
    if not existing_page:
        return None
    name = _find_schema_property_name(schema, aliases, types=("date",))
    if not name:
        return None
    value = (((existing_page.get("properties") or {}).get(name) or {}).get("date") or {}).get("start")
    return str(value) if value else None


def _existing_notes_value(existing_page: dict | None, schema: dict) -> str:
    if not existing_page:
        return ""
    name = _find_schema_property_name(schema, ("notes",), types=("rich_text",))
    if not name:
        return ""
    rich_text = ((existing_page.get("properties") or {}).get(name) or {}).get("rich_text") or []
    return "\n".join(part.get("plain_text", "") for part in rich_text if part.get("plain_text"))
```

Update `_notes_text()` and `_page_properties()` signatures:

```python
def _notes_text(
    out_dir: Path,
    website_confirmation: dict,
    email_confirmation: dict | None,
    *,
    submission_history: dict | None = None,
    existing_notes: str = "",
) -> str:
    lines = [line for line in existing_notes.splitlines() if line.strip()]
    lines.extend(
        [
            f"Applied via automation with {confirmation_type}.",
            f"Output dir: {local_dir}",
            f"Website confirmed at: {website_confirmation['confirmed_at_utc']}",
        ]
    )
    if submission_history and submission_history.get("resubmit_count"):
        lines.extend(
            [
                f"Originally applied: {submission_history['confirmed_at']}",
                f"Unlocked for resubmit: {submission_history['last_resubmit_unlocked_at']}",
                f"Latest resubmitted at: {submission_history['last_resubmit_confirmed_at']}",
                f"Resubmit count: {submission_history['resubmit_count']}",
            ]
        )
    return "\n".join(dict.fromkeys(line for line in lines if line))
```

```python
def _page_properties(
    schema: dict,
    *,
    meta: dict,
    out_dir: Path,
    website_confirmation: dict,
    email_confirmation: dict | None,
    existing_page: dict | None = None,
    submission_history: dict | None = None,
) -> dict:
    existing_application_date = _existing_date_value(existing_page, schema, ("application date", "applied date"))
    existing_notes = _existing_notes_value(existing_page, schema)
    mapped_values = [
        (("application date", "applied date"), ("date",), existing_application_date or website_confirmation["confirmed_at_utc"]),
        (("notes",), ("rich_text",), _notes_text(out_dir, website_confirmation, email_confirmation, submission_history=submission_history, existing_notes=existing_notes)),
    ]
```

- [ ] **Step 4: Append resubmission history blocks when updating an existing page**

Add:

```python
def _resubmission_history_blocks(history: dict) -> list[dict]:
    if not history or not history.get("resubmit_count"):
        return []
    items = [
        ("heading_2", "Resubmission History"),
        ("bulleted_list_item", f"Originally applied: {history['confirmed_at']}"),
        ("bulleted_list_item", f"Unlocked for resubmit: {history['last_resubmit_unlocked_at']}"),
        ("bulleted_list_item", f"Latest resubmitted at: {history['last_resubmit_confirmed_at']}"),
        ("bulleted_list_item", f"Resubmit count: {history['resubmit_count']}"),
    ]
    blocks: list[dict] = []
    for block_type, content in items:
        block = _make_block(block_type, content)
        if block:
            blocks.append(block)
    return blocks
```

Update `_sync_to_notion()`:

```python
submission_history = _load_submission_history_for_output(out_dir)
properties = _page_properties(
    schema,
    meta=meta,
    out_dir=out_dir,
    website_confirmation=website_confirmation,
    email_confirmation=email_confirmation,
    existing_page=existing_page,
    submission_history=submission_history,
)
```

When `existing_page` exists:

```python
existing_blocks = client.list_block_children(page_id)
history_blocks = _resubmission_history_blocks(submission_history)
history_marker = str(submission_history.get("last_resubmit_confirmed_at") or "")
if history_blocks and history_marker and history_marker not in json.dumps(existing_blocks):
    client.append_block_children(page_id, history_blocks)
```

- [ ] **Step 5: Re-run the Notion tests**

Run:

```bash
uv run python -m pytest tests/test_notion_sync.py -k "preserve_existing_application_date or resubmission_history_lines" -v
```

Expected:
- Both new tests `PASS`

- [ ] **Step 6: Commit the Notion slice**

```bash
git add scripts/notion_job_applications.py tests/test_notion_sync.py
git commit -m "fix: preserve notion application date on resubmit"
```

---

### Task 7: Surface lock state in the web UI, update docs, and run the full verification sweep

**Files:**
- Modify: `scripts/static/index.html`
- Modify: `scripts/static/app.js`
- Modify: `docs/worker-pipeline-patterns.md`
- Modify: `docs/launch-modes.md`
- Test: `tests/test_job_web.py`

- [ ] **Step 1: Add a minimal HTML shell assertion for the new badge slot**

In `tests/test_job_web.py`, add:

```python
def test_root_includes_submission_lock_indicator_shell(client):
    resp = client.get("/")

    assert resp.status_code == 200
    assert 'id="submission-lock-indicator"' in resp.text
```

- [ ] **Step 2: Run the shell test and confirm failure**

Run:

```bash
uv run python -m pytest tests/test_job_web.py -k "submission_lock_indicator_shell" -v
```

Expected:
- `FAIL` because the new indicator element is not in `index.html` yet

- [ ] **Step 3: Add explicit lock badges and unlock action wiring**

In `scripts/static/index.html`, add a second badge next to the existing `Submitted before` badge:

```html
<span class="prev-submitted-badge" id="prev-submitted-indicator" style="display:none"></span>
<span class="prev-submitted-badge" id="submission-lock-indicator" style="display:none"></span>
```

In `scripts/static/app.js`, add a small helper and use it in both queue rows and the detail header:

```javascript
function submissionLockLabel(job) {
  if (!job.previously_submitted) return '';
  if (job.submission_lock_state === 'locked') return 'Locked';
  if (job.submission_lock_state === 'unlocked_for_resubmit') return 'Unlocked for resubmit';
  return '';
}
```

Use it in `buildQueueRow()`:

```javascript
const lockLabel = submissionLockLabel(job);
const lockBadge = lockLabel
  ? `<span class="prev-submitted-badge" title="${escapeHtml(lockLabel)}">${escapeHtml(lockLabel)}</span>`
  : '';
<td class="col-status"><span class="status-badge ${sClass}">${sLabel}</span>${prevSub}${lockBadge}${llmBadge}${progress}${errMsg}</td>
```

Update `renderJobHeaderFull()`:

```javascript
const lockBadge = document.getElementById('submission-lock-indicator');
const lockLabel = submissionLockLabel(job);
if (lockLabel) {
  lockBadge.className = 'prev-submitted-badge';
  lockBadge.textContent = lockLabel;
  lockBadge.title = lockLabel;
  lockBadge.style.display = '';
} else {
  lockBadge.style.display = 'none';
}
```

Add the new action:

```javascript
async function unlockForResubmit(jobId) {
  try {
    await apiCall('POST', `/api/jobs/${jobId}/unlock-resubmit`);
    showToast('Job unlocked for resubmission', 'success');
    const updated = await apiCall('GET', `/api/jobs/${jobId}`);
    if (updated) { window.jobs[updated.id] = updated; updateJobDetailHeader(updated); }
  } catch (e) {
    showToast('Unlock failed: ' + e.message, 'error');
  }
}
```

Gate `getJobActionModels(job)`:

```javascript
const isLockedSubmission = job.submission_lock_state === 'locked';
if (isLockedSubmission) {
  actions.push({ label: 'Unlock to Resubmit', className: 'btn-primary', handler: () => unlockForResubmit(job.id) });
  if (canArchive) actions.push({ label: 'Archive', className: 'btn-outline', handler: () => archiveJob(job.id) });
  if (canDelete) actions.push({ label: 'Delete', className: 'btn-outline btn-delete', handler: () => deleteJob(job.id) });
  return actions;
}
```

- [ ] **Step 4: Update the workflow docs to match the new invariant**

In `docs/worker-pipeline-patterns.md`, add:

```markdown
## Submitted Job Lock

Jobs with `confirmed_at` are locked by default. Workers and retry logic must not move a locked job back into `queued`, `reanswering`, `approved`, `autofilling`, or `draft`. A user must explicitly unlock the row for one resubmission cycle, and the next confirmed submit relocks it automatically.
```

In `docs/launch-modes.md`, update the Notion sync note to say:

```markdown
For an existing Notion page, keep `Application Date` pinned to the first confirmed submission. Later resubmissions append history to `Notes` and the page body instead of overwriting the original application date.
```

- [ ] **Step 5: Re-run targeted UI shell coverage**

Run:

```bash
uv run python -m pytest tests/test_job_web.py -k "submission_lock_indicator_shell or unlock_flow" -v
```

Expected:
- Both tests `PASS`

- [ ] **Step 6: Run the full verification sweep**

Run:

```bash
uv run python -m pytest tests/ -v
uv run ruff check scripts/ tests/
uv run python scripts/check_architecture.py
uv run python scripts/sync_agent_files.py --check
uv run python scripts/check_agent_docs.py
```

Expected:
- All targeted tests pass
- `ruff` returns clean
- architecture and doc checks pass

- [ ] **Step 7: Run the browser smoke test for the actual workflow**

Run:

```bash
uv run python scripts/job_web.py
```

Expected:
- the local web app starts on `http://127.0.0.1:8420`

Then verify in the browser:
- a locked previously submitted job shows `Submitted before` and `Locked`
- the detail view shows `Unlock to Resubmit` and suppresses rerun buttons
- unlocking flips the badge to `Unlocked for resubmit`
- `Restart → Draft` works only after unlock
- after a confirmed resubmission, the badge returns to `Locked`

- [ ] **Step 8: Commit the UI/docs/final verification slice**

```bash
git add scripts/static/index.html scripts/static/app.js docs/worker-pipeline-patterns.md docs/launch-modes.md tests/test_job_web.py
git commit -m "feat: surface submitted job lock state in web UI"
```
