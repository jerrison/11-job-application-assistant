# Worker Controls & Pipeline Improvements Design

## Overview

Six workstreams improving the job processing pipeline, error visibility, rate limiting, and web UI worker controls.

## A: Filter Archived Jobs from Worker Pickup

**Problem**: `get_pending_jobs()` in `job_db.py` does not check the `archived` flag, so workers can pick up archived jobs.

**Fix**: Add `AND (archived IS NULL OR archived = FALSE)` to the SQL WHERE clause in `get_pending_jobs()`.

**Files**: `scripts/job_db.py` (line ~253)

## B: Rate Limiting Mitigation for LLM API Calls

**Problem**: No backoff between provider retries, no concurrency limits on LLM calls, all workers can hit APIs simultaneously.

**Changes**:
1. **Exponential backoff with jitter** in `provider_fallback()` — after a provider fails, wait `min(2^attempt * 2, 30) + random(0, 2)` seconds before trying next
2. **Per-provider concurrency semaphore** — limit concurrent LLM subprocess calls per provider (default: 5 for Claude, 5 for others)
3. **Stagger worker job starts** — add `random(0.5, 3.0)` second delay when a worker picks up a job, spreading out initial API hits

**Files**: `scripts/pipeline_orchestrator.py`, `scripts/job_worker.py`

## C: Descriptive Errors in Job Queue Table

**Problem**: Queue table shows "Stopped" badge but not WHY the job stopped. Users must click into detail view.

**Changes**:
1. Show `error_message` as a subtitle line under the status badge in `buildQueueRow()`
2. Truncate to ~80 chars with ellipsis
3. Style as muted, smaller text

**Files**: `scripts/static/app.js` (buildQueueRow function)

## D: Draft Completeness Validation

**Problem**: A draft can be marked complete even if resume, cover letter, answers, or screenshot are missing.

**Changes**:
1. After submit_application runs in draft mode, check for:
   - Resume PDF in `documents/`
   - Cover letter PDF in `documents/`
   - Autofill report in `submit/`
   - Draft summary screenshot (`draft_summary.png`)
2. If any missing, set status to `failed` with descriptive error listing what's missing
3. Add validation function `validate_draft_completeness()` in `pipeline_orchestrator.py`

**Files**: `scripts/pipeline_orchestrator.py`

## E: Detailed Worker Controls Panel

**Problem**: No per-worker visibility or control. Users can only see active jobs, not which worker is doing what.

### Backend Changes

1. **Worker Registry** in `WorkerPool`:
   - Dict mapping `worker_id → {job_id, job, phase, start_time, status}`
   - Updated when worker picks up/completes a job
   - Thread-safe via existing lock

2. **Per-worker stop signaling**:
   - Each worker gets its own `threading.Event` for stop signaling
   - `POST /api/workers/{worker_id}/stop` — graceful stop after current job
   - `POST /api/workers/{worker_id}/kill` — force-stop, requeue current job

3. **WebSocket enhancements**:
   - Broadcast `worker_detail` message with per-worker state every 2 seconds
   - Include: worker_id, status (idle/busy/stopping), current job info, phase, elapsed_time

4. **New API endpoints**:
   - `GET /api/workers/detail` — full worker registry state
   - `POST /api/workers/{worker_id}/stop` — stop single worker
   - `POST /api/workers/{worker_id}/kill` — kill single worker + requeue job
   - `POST /api/workers/scale` — adjust worker count up/down

### Frontend Changes

1. **Worker panel** (collapsible, below nav):
   - Bulk controls: Start All, Stop All, Restart All, Scale slider
   - Per-worker table:
     - Worker #, Status badge, Current Job (company + role + ID), Phase indicator, Progress bar, Elapsed time, Board, Stop/Kill buttons
   - Real-time updates via WebSocket

2. **Phase progression indicator**:
   - Visual pipeline: Resolve → Generate → Submit
   - Highlight current phase

**Files**: `scripts/job_worker.py`, `scripts/job_web.py`, `scripts/static/app.js`, `scripts/static/index.html`, `scripts/static/style.css`

## F: Redo All Drafts

After A-E are implemented:
1. Query all jobs where status NOT IN submitted terminal states AND archived = FALSE
2. Set status to `queued` for full pipeline re-run in draft mode
3. Monitor for issues, fix as encountered

## Implementation Order

A → B → C → D can be done in parallel (independent changes)
E requires backend + frontend coordination
F depends on A-E being deployed
