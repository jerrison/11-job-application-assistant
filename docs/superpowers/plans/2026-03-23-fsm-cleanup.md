# Job Lifecycle FSM Cleanup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 9 identified issues in the job lifecycle state machine — ambiguous statuses, missing states, overloaded semantics, redundant representations, and missing quality gates.

**Architecture:** Additive changes to the status enum, new DB column for failure taxonomy, split of dual-meaning statuses, and consistent representation across DB/web/TUI/CLI layers.

**Tech Stack:** Python (SQLite, FastAPI), JavaScript (vanilla app.js), Textual TUI

---

## File Map

| File | Changes |
|------|---------|
| `scripts/job_db.py` | Add `regenerating`+`approved` to JOB_STATUSES, add `failure_type` column, update `get_pending_jobs`, update `reset_stale_jobs`, update `update_status`, simplify archive logic |
| `scripts/pipeline_orchestrator.py` | `approve_job` → set `approved` not `submitting`, pass `failure_type` on all stopped transitions, fix `_post_submit` double-write, add captcha-resolved transition |
| `scripts/job_worker.py` | CAS claim `approved` → `submitting`, handle `approved` in safety resets, consolidate duplicate resets |
| `scripts/job_web.py` | Pass `failure_type` on user actions, update archive/unarchive, add `approved` to endpoint checks |
| `scripts/static/app.js` | Add `approved` to statusLabel/statusClass/buttons, show `failure_type` in stopped jobs, update archive display |
| `scripts/job_tui.py` | Add `approved` to _STATUS_COLORS/_STATUS_EMOJI/_PROCESSING_STATUSES, update archive display |
| `tests/test_job_db.py` | Tests for new statuses, failure_type, archive changes |
| `tests/test_job_worker.py` | Tests for approved→submitting CAS, consolidated resets |
| `tests/test_draft_manager.py` | Update approve_job tests for new status |

---

### Task 1: Add `regenerating` to JOB_STATUSES + fix _post_submit double-write

**Files:**
- Modify: `scripts/job_db.py:10-15`
- Modify: `scripts/pipeline_orchestrator.py:1506`

- [ ] **Step 1: Add `regenerating` to JOB_STATUSES**

In `scripts/job_db.py`, change the tuple:
```python
JOB_STATUSES = (
    "queued", "queued_submit", "resolving", "generating", "autofilling",
    "draft", "submitting", "reanswering", "awaiting_captcha",
    "submitted", "retrying", "fix_in_progress", "regenerating",
    "stopped", "needs_board_url", "archived",
)
```

- [ ] **Step 2: Fix _post_submit double-write**

In `scripts/pipeline_orchestrator.py` around line 1506, change:
```python
# BEFORE:
update_status(conn, job_id, "submitted", notion_url=notion_url)

# AFTER — only update notion_url, don't re-set status:
conn.execute("UPDATE jobs SET notion_url = ? WHERE id = ?", (notion_url, job_id))
conn.commit()
```

- [ ] **Step 3: Run tests**

Run: `uv run python -m pytest tests/test_job_db.py tests/test_draft_manager.py -v`

- [ ] **Step 4: Commit**

```bash
git add scripts/job_db.py scripts/pipeline_orchestrator.py
git commit -m "fix: add regenerating to JOB_STATUSES, fix _post_submit double-write"
```

---

### Task 2: Add `failure_type` column to jobs table

**Files:**
- Modify: `scripts/job_db.py` (schema, update_status, migration)
- Modify: `scripts/pipeline_orchestrator.py` (all stopped transitions)
- Modify: `scripts/job_web.py` (user-initiated stops)
- Modify: `scripts/job_tui.py` (user-initiated stops)
- Modify: `scripts/static/app.js` (display failure_type)
- Test: `tests/test_job_db.py`

**Failure type taxonomy:**
```
user_rejected    — User clicked reject/skip
user_stopped     — User clicked stop on a running job
auth_failed      — Login/auth wall encountered
captcha          — CAPTCHA encountered
timeout          — Operation timed out
unsupported      — Unsupported job board
duplicate        — Duplicate JD detected
incomplete       — Draft missing required artifacts
crash            — Unhandled exception
retries_exhausted — All auto-retries used up
generation_failed — LLM asset generation failed
resolution_failed — URL resolution failed
submit_failed    — Form submission failed, no confirmation
```

- [ ] **Step 1: Add column + migration to job_db.py**

Add `failure_type TEXT` to `_SCHEMA` after `error_message TEXT`. Add migration in `init_db`:
```python
# Migration: add failure_type column
try:
    conn.execute("ALTER TABLE jobs ADD COLUMN failure_type TEXT")
    conn.commit()
except sqlite3.OperationalError:
    pass  # Column already exists
```

Add `failure_type` param to `update_status`:
```python
if failure_type is not None:
    sets.append("failure_type = ?")
    params.append(failure_type)
# Clear failure_type when leaving stopped status
if status != "stopped":
    sets.append("failure_type = NULL")
```

- [ ] **Step 2: Pass failure_type on all stopped transitions in pipeline_orchestrator.py**

Each `update_status(conn, job_id, "stopped", ...)` call gets a `failure_type=` kwarg. Map by line:
- Line 409 (URL resolution error) → `failure_type="resolution_failed"`
- Line 454 (Asset generation failed) → `failure_type="generation_failed"`
- Line 500 (Asset generation exception) → `failure_type="generation_failed"`
- Line 806 (Unsupported board) → `failure_type="unsupported"`
- Line 823 (Auth failure) → `failure_type="auth_failed"`
- Line 859 (Incomplete draft) → `failure_type="incomplete"`
- Line 942 (No confirmation) → `failure_type="submit_failed"`
- Line 952 (Captcha) → `failure_type="captcha"`
- Line 965 (Timeout) → `failure_type="timeout"`
- Line 973 (Submit exception) → `failure_type="crash"`
- Line 983 (Re-answer failed) → `failure_type="submit_failed"`
- Line 1001 (Fix validation error) → `failure_type="submit_failed"`
- Line 1010 (Fix slot busy) → `failure_type="submit_failed"`
- Line 1115 (All attempts failed) → `failure_type="retries_exhausted"`
- In `_auto_retry_if_transient` line 562 → `failure_type="retries_exhausted"`
- In `regenerate_job` for duplicate detection line 488 → `failure_type="duplicate"`

- [ ] **Step 3: Pass failure_type on user actions in job_web.py and job_tui.py**

All `update_status(... "stopped", error_message="Rejected/Skipped/Stopped ...")` calls:
- `job_web.py` reject → `failure_type="user_rejected"`
- `job_web.py` skip → `failure_type="user_rejected"`
- `job_web.py` stop → `failure_type="user_stopped"`
- `job_tui.py` skip → `failure_type="user_rejected"`
- `job_tui.py` reject → `failure_type="user_rejected"`

- [ ] **Step 4: Display failure_type in app.js**

In the answers tab or job header, show failure_type badge for stopped jobs:
```javascript
// In renderJobHeaderFull, after status badge:
if (job.status === 'stopped' && job.failure_type) {
  const ft = document.createElement('span');
  ft.className = 'failure-type-badge';
  ft.textContent = job.failure_type.replace(/_/g, ' ');
  badge.parentElement.appendChild(ft);
}
```

Add CSS for `.failure-type-badge` (small muted pill after the status badge).

- [ ] **Step 5: Write test**

```python
def test_failure_type_set_on_stopped(self):
    conn = init_db(":memory:")
    job_id = add_job(conn, "https://example.com/job")
    update_status(conn, job_id, "stopped", error_message="auth", failure_type="auth_failed")
    job = get_job(conn, job_id)
    assert job["failure_type"] == "auth_failed"

def test_failure_type_cleared_on_non_stopped(self):
    conn = init_db(":memory:")
    job_id = add_job(conn, "https://example.com/job")
    update_status(conn, job_id, "stopped", failure_type="auth_failed")
    update_status(conn, job_id, "queued")
    job = get_job(conn, job_id)
    assert job["failure_type"] is None
```

- [ ] **Step 6: Run tests and commit**

```bash
uv run python -m pytest tests/test_job_db.py -v
git add scripts/job_db.py scripts/pipeline_orchestrator.py scripts/job_web.py scripts/job_tui.py scripts/static/app.js tests/test_job_db.py
git commit -m "feat: add failure_type column for structured error taxonomy"
```

---

### Task 3: Add `approved` status (split from `submitting`)

**Files:**
- Modify: `scripts/job_db.py:10-15` (JOB_STATUSES), `get_pending_jobs`, `reset_stale_jobs`
- Modify: `scripts/pipeline_orchestrator.py:169-184` (approve_job)
- Modify: `scripts/job_worker.py:82-129` (next_job CAS), `WorkerPool.start`
- Modify: `scripts/job_web.py` (reanswer, stop, restart-pipeline, draft-overrides endpoint status checks)
- Modify: `scripts/static/app.js` (statusLabel, statusClass, action buttons, PROCESSING_STATUSES, badge counts)
- Modify: `scripts/job_tui.py` (_STATUS_COLORS, _STATUS_EMOJI, _PROCESSING_STATUSES)
- Test: `tests/test_job_db.py`, `tests/test_job_worker.py`, `tests/test_draft_manager.py`

- [ ] **Step 1: Add `approved` to JOB_STATUSES**

```python
JOB_STATUSES = (
    "queued", "queued_submit", "resolving", "generating", "autofilling",
    "draft", "approved", "submitting", "reanswering", "awaiting_captcha",
    "submitted", "retrying", "fix_in_progress", "regenerating",
    "stopped", "needs_board_url", "archived",
)
```

- [ ] **Step 2: Change approve_job to set `approved`**

In `scripts/pipeline_orchestrator.py`:
```python
def approve_job(conn: sqlite3.Connection, job_id: int) -> bool:
    """CAS transition: draft/stopped/submitted -> approved. Returns True if successful."""
    from job_db import log_event
    cur = conn.execute(
        "UPDATE jobs SET status = 'approved', error_message = '', progress = '' "
        "WHERE id = ? AND status IN ('draft', 'stopped', 'submitted')",
        (job_id,),
    )
    conn.commit()
    if cur.rowcount > 0:
        try:
            log_event(conn, job_id, "approved_for_submit", initiator="web")
        except Exception:
            pass
        log.info("approve_job: job %d transitioned to approved", job_id)
    return cur.rowcount > 0
```

- [ ] **Step 3: Update get_pending_jobs to include `approved`**

In `scripts/job_db.py`, change the query:
```sql
WHERE status IN ('queued', 'queued_submit', 'approved', 'submitting', 'reanswering', 'regenerating')
```

Update ordering to prioritize `approved` same as `submitting`:
```sql
ORDER BY
  CASE status WHEN 'approved' THEN 0 WHEN 'submitting' THEN 0 WHEN 'reanswering' THEN 0 WHEN 'regenerating' THEN 0 ELSE 1 END,
  ...
  CASE WHEN status IN ('approved', 'submitting', 'reanswering', 'regenerating') THEN updated_at ELSE created_at END ASC
```

- [ ] **Step 4: Update worker CAS claim to transition approved → submitting**

In `scripts/job_worker.py` `Coordinator.next_job()`, change line 114:
```python
claim_status = "submitting" if job["status"] in ("approved", "submitting", "reanswering") else "resolving"
```

This means: when a worker picks up an `approved` job, it atomically CAS-transitions it to `submitting`.

- [ ] **Step 5: Update reset_stale_jobs to handle `approved`**

In `scripts/job_db.py` `reset_stale_jobs`, add `approved` to the submit_phase tuple:
```python
submit_phase = ("approved", "submitting", "reanswering", "awaiting_captcha")
```

- [ ] **Step 6: Update WorkerPool.start safety resets**

In `scripts/job_worker.py` `WorkerPool.start()`, add `approved` to the safety reset:
```python
cur = self._conn.execute(
    "UPDATE jobs SET status = 'draft', "
    "progress = 'Submit interrupted — needs re-approval' "
    "WHERE status IN ('approved', 'submitting', 'reanswering', 'awaiting_captcha')"
)
```

Also in `_kill_single_worker`, add `approved` to submit-phase check.

- [ ] **Step 7: Update job_web.py endpoints**

- `reanswer` (line 648): Add `approved` to allowed source statuses
- `stop` (line 709): Add `approved` to stoppable statuses
- `restart-pipeline` (line 726): Add `approved` to active set
- `draft-overrides` (line 765): Add `approved` to allowed source statuses

- [ ] **Step 8: Update app.js**

Add to `PROCESSING_STATUSES`:
```javascript
const PROCESSING_STATUSES = new Set([
  'generating', 'resolving', 'submitting', 'autofilling', 'retrying', 'fix_in_progress', 'regenerating'
]);
```
Note: `approved` should NOT be in PROCESSING_STATUSES. It's a waiting state like `draft`.

Add to `statusLabel`:
```javascript
approved: 'Approved',
```

Add to `statusClass`: It should get its own class:
```javascript
if (status === 'approved') return 'status-approved';
```

Add to badge counts in `updateStatusBadges`:
```javascript
else if (s === 'approved') counts.processing++;  // count with processing for simplicity
```

Add button logic in `renderJobActionRow` — show same buttons as `submitting` currently shows (Stop):
```javascript
} else if (s === 'approved') {
  row.appendChild(makeBtn('Stop', 'btn-danger', () => stopJob(job.id)));
}
```

Add to `_phaseToPercent`:
```javascript
approved: 65,
```

- [ ] **Step 9: Update job_tui.py**

Add to dictionaries:
```python
_STATUS_COLORS["approved"] = "yellow"
_STATUS_EMOJI["approved"] = "[>]"  # queued for submit
```

Add to `_PROCESSING_STATUSES` or create a new `_SUBMIT_QUEUE_STATUSES` set? Best to add to processing:
```python
_PROCESSING_STATUSES = {"resolving", "generating", "submitting", "autofilling", "fix_in_progress", "retrying", "approved"}
```

- [ ] **Step 10: Update tests**

In `tests/test_job_worker.py`:
- Update `test_submitting_jobs_respect_board_limits` to use `approved`
- Update `test_submitting_job_resets_to_draft_on_startup` to test `approved` too
- Add `test_approved_job_claimed_as_submitting` — approved → CAS → submitting

In `tests/test_draft_manager.py`:
- Update `test_approve_job_cas_accepts_draft` — now transitions to `approved` not `submitting`

In `tests/test_job_db.py`:
- Update `test_submitting_jobs_ordered_before_queued` — add `approved` ordering test
- Update `test_reset_stale_submitting_to_draft` — add `approved` case

- [ ] **Step 11: Run all tests and commit**

```bash
uv run python -m pytest tests/ -v --no-header 2>&1 | tail -30
git add scripts/job_db.py scripts/pipeline_orchestrator.py scripts/job_worker.py scripts/job_web.py scripts/static/app.js scripts/job_tui.py tests/
git commit -m "feat: add approved status to disambiguate waiting vs active submission"
```

---

### Task 4: Fix archived redundancy

**Files:**
- Modify: `scripts/job_web.py:667-699` (archive/unarchive endpoints)
- Modify: `scripts/static/app.js` (status display, filtering)
- Modify: `scripts/job_tui.py` (archive display)
- Test: `tests/test_job_web.py`

- [ ] **Step 1: Change archive to preserve real status**

In `scripts/job_web.py`, replace the archive endpoint:
```python
@app.post("/api/jobs/{job_id}/archive")
def archive_job(job_id: int):
    conn = get_conn()
    job = get_job(conn, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    conn.execute(
        "UPDATE jobs SET archived = TRUE, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (job_id,),
    )
    conn.commit()
    log_event(conn, job_id, "archived", initiator="web")
    return {"status": "archived"}
```

- [ ] **Step 2: Change unarchive to just clear the boolean**

```python
@app.post("/api/jobs/{job_id}/unarchive")
def unarchive_job(job_id: int):
    conn = get_conn()
    job = get_job(conn, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.get("archived"):
        raise HTTPException(400, "Job is not archived")
    conn.execute(
        "UPDATE jobs SET archived = FALSE, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (job_id,),
    )
    conn.commit()
    log_event(conn, job_id, "unarchived", initiator="web")
    return {"status": job["status"]}  # return the real preserved status
```

- [ ] **Step 3: Update app.js to show archived as overlay badge**

In `statusLabel`, remove the `archived` entry — it's no longer a status.

In `getFilteredJobs`, change archived filter to use the boolean:
```javascript
if (currentFilter === 'archived') {
  jobs = jobs.filter(j => j.archived);
} else {
  jobs = jobs.filter(j => !j.archived);
  // ... existing filter logic
}
```

In `renderJobActionRow`, change archived check:
```javascript
} else if (job.archived) {
  row.appendChild(makeBtn('Unarchive', 'btn-outline', () => unarchiveJob(job.id)));
  row.appendChild(makeBtn('Delete', 'btn-outline btn-delete', () => deleteJob(job.id)));
}
```

In status badge rendering, show "(archived)" suffix when `job.archived`:
```javascript
badge.textContent = statusLabel(job.status) + (job.archived ? ' (archived)' : '');
```

- [ ] **Step 4: Update badge counts**

```javascript
if (j.archived) { counts.archived++; continue; }  // use boolean, not status
```

- [ ] **Step 5: Update job_tui.py**

In queue view and detail view, show archived suffix:
```python
if job.get("archived"):
    status_display += " [dim](archived)[/]"
```

- [ ] **Step 6: Run tests and commit**

```bash
uv run python -m pytest tests/ -v -k "archive" --no-header
git add scripts/job_web.py scripts/static/app.js scripts/job_tui.py
git commit -m "fix: archive preserves real status, uses boolean-only representation"
```

---

### Task 5: Fix awaiting_captcha exit + deduplicate resets + handle stuck autofilling

**Files:**
- Modify: `scripts/pipeline_orchestrator.py:592-610` (_poll_captcha_signal)
- Modify: `scripts/job_db.py:644-723` (reset_stale_jobs)
- Modify: `scripts/job_worker.py` (WorkerPool.start — remove duplicate resets)

- [ ] **Step 1: Add captcha-resolved transition in _poll_captcha_signal**

In `scripts/pipeline_orchestrator.py`, after the `break` on captcha resolved:
```python
elif not signal_path.exists() and notified:
    # Captcha resolved — restore previous status
    update_status(conn, job_id, "submitting", progress="Captcha resolved, continuing...")
    log_event(conn, job_id, "captcha_resolved", initiator="worker")
    break
```

- [ ] **Step 2: Remove duplicate safety resets from WorkerPool.start**

In `scripts/job_worker.py` `WorkerPool.start()`, remove the redundant safety resets (lines ~453-480). Keep only the call to `reset_stale_jobs(self._conn)`. The consolidated logic lives in `reset_stale_jobs` in `job_db.py`.

Ensure `reset_stale_jobs` already handles all cases:
- approved/submitting/reanswering/awaiting_captcha → draft
- autofilling → queued
- queued_submit → queued
- stale resolving/generating/fix_in_progress/retrying → queued or draft

- [ ] **Step 3: Add `retrying` and `regenerating` to stale-job detection in reset_stale_jobs**

In `scripts/job_db.py` `reset_stale_jobs`, ensure the `in_progress` tuple includes all processing statuses:
```python
in_progress = ("resolving", "generating", "fix_in_progress", "retrying", "regenerating")
```

- [ ] **Step 4: Run tests and commit**

```bash
uv run python -m pytest tests/test_job_worker.py tests/test_job_db.py -v
git add scripts/pipeline_orchestrator.py scripts/job_db.py scripts/job_worker.py
git commit -m "fix: captcha-resolved transition, consolidated safety resets, handle stuck jobs"
```

---

### Task 6: Add CSS + final polish

**Files:**
- Modify: `scripts/static/app.js` or `scripts/static/index.html`

- [ ] **Step 1: Add CSS for new status classes**

```css
.status-approved { background: #ffc107; color: #000; }
.failure-type-badge {
  display: inline-block;
  font-size: 0.75rem;
  padding: 2px 6px;
  margin-left: 6px;
  border-radius: 4px;
  background: #f5f5f5;
  color: #666;
}
```

- [ ] **Step 2: Lint, test full suite, commit**

```bash
uv run ruff check scripts/ tests/
uv run python -m pytest tests/ -v --no-header 2>&1 | tail -20
git add -A && git commit -m "style: CSS for approved status and failure type badge"
```
