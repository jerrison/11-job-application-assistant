# Submitted Job Lock And Resubmission Design

## Overview

Submitted jobs should be locked by default. A job that has already been confirmed submitted must not drift back into `queued`, `generating`, `autofilling`, or `draft` unless the user explicitly unlocks it for resubmission. Unlocking does not erase history. It temporarily permits the job to be redrafted, edited, and submitted again, and the job relocks automatically after the next confirmed submission.

This design fixes the current mismatch where `confirmed_at` is treated as an informational badge only. Today, rows like job `#17` can show `Submitted before` and still be rerun because the web restart route, worker pickup, and pipeline retry paths do not enforce a shared submission lock.

## Current Problem

- The web route `POST /api/jobs/{job_id}/restart-pipeline` can move a previously submitted job back to `queued`.
- `reanswer` and answer regeneration can also re-enter the pipeline for submitted jobs.
- `get_pending_jobs()` and worker reset paths do not refuse previously submitted jobs that were accidentally requeued.
- The queue and job detail UI show `Submitted before`, but the badge has no enforcement meaning.
- Notion reuses the same page for later syncs and currently overwrites `Application Date` with the most recent confirmation timestamp.

The result is inconsistent state:

- the repo currently contains confirmed-submitted rows still marked `draft`, `queued`, or `stopped`
- the worker can redraft those rows without an explicit user unlock
- Notion would overwrite the original application date if a resubmission were synced with no extra handling

## Goals

- Lock every confirmed-submitted job by default.
- Make unlocking explicit, deliberate, and one-cycle-at-a-time.
- Preserve submission history in repo data, UI, timeline/events, and Notion.
- Auto-refuse background requeues for locked jobs.
- Repair currently inconsistent rows during rollout.
- Keep the design understandable through one explicit lock state instead of scattered `confirmed_at` checks.

## Non-Goals

- Creating separate Notion pages for each resubmission.
- Erasing old submission evidence when a job is unlocked.
- Supporting silent or automatic unlocks.
- Allowing locked jobs to re-enter draft mode through any code path.

## Options Considered

### 1. UI And Route Guard Only

Hide rerun buttons and reject a few web endpoints for submitted jobs.

Rejected because the worker, reset logic, and direct queue transitions could still redraft a submitted job in the background.

### 2. `confirmed_at` Plus A Small Override Flag

Infer lock from `confirmed_at` and add a single `resubmit_unlocked_once` flag.

Rejected because the semantics remain implicit and easy to regress. This problem is a workflow invariant, not a view-layer convenience.

### 3. Explicit Submitted-Lock Model

Add a dedicated lock state and route all rerun entry points through one shared enforcement rule.

Chosen because it is the clearest, safest, and easiest to audit.

## Chosen Design

### Data Model

Add the following job-level fields:

- `submission_lock_state TEXT NOT NULL DEFAULT 'open'`
  - allowed values: `open`, `locked`, `unlocked_for_resubmit`
- `resubmit_count INTEGER NOT NULL DEFAULT 0`
- `last_resubmit_unlocked_at TIMESTAMP NULL`
- `last_resubmit_unlock_initiator TEXT NULL`
- `last_resubmit_confirmed_at TIMESTAMP NULL`

Keep existing fields with these semantics:

- `confirmed_at` remains the timestamp of the first confirmed submission for the job.
- `email_confirmed` remains a confirmation signal, not a lock signal.
- `confirmation_method` remains the latest confirmation mechanism recorded by the existing pipeline.

### Lifecycle

The lifecycle is:

1. New or imported job starts as `submission_lock_state = 'open'`.
2. First confirmed submission sets `confirmed_at` if empty and changes the lock to `locked`.
3. User explicitly chooses `Unlock to Resubmit`, which changes the lock to `unlocked_for_resubmit` and records unlock metadata.
4. While unlocked, the job may be restarted to draft, reanswered, or resubmitted.
5. If the unlocked rerun fails or stops, the job stays `unlocked_for_resubmit`.
6. On the next confirmed submission, the job relocks to `locked`, increments `resubmit_count`, and records `last_resubmit_confirmed_at`.

Unlocking does not mean "never submitted." It means "previously submitted, but intentionally reopened for one more submission cycle."

### Rerunnable Statuses

Treat these statuses as blocked when the job is `locked`:

- `queued`
- `queued_submit`
- `approved`
- `reanswering`
- `resolving`
- `generating`
- `autofilling`
- `draft`
- `submitting`
- `regenerating`

The system may still read artifacts, show history, archive the row, or delete it if the surrounding workflow already allows those operations. The lock only forbids re-entry into the submission pipeline.

## Enforcement Architecture

### Shared Guard

Create one shared submission-lock helper in the data/status layer. It should answer:

- whether a job is currently submission-locked
- whether a target status is a rerunnable status
- whether a requested transition is allowed
- whether an invalid locked-state row should be repaired back to `submitted`

The rule should live close to `update_status()` and job selection, not only in the web layer.

### Explicit User Actions

Guard these endpoints:

- `POST /api/jobs/{job_id}/restart-pipeline`
- `POST /api/jobs/{job_id}/reanswer`
- `POST /api/jobs/{job_id}/regenerate-asset` when target is `answers`
- `POST /api/jobs/{job_id}/retry` if it would re-enter the pipeline

Add:

- `POST /api/jobs/{job_id}/unlock-resubmit`

Behavior:

- locked jobs return a refusal with no status change
- unlocked jobs follow existing rerun behavior
- `unlock-resubmit` only works for `locked` jobs and logs an explicit event

### Background And Worker Paths

Guard these non-UI paths:

- `get_pending_jobs()` must exclude locked jobs even if their `status` was incorrectly left as `queued`
- worker startup reset must not requeue locked jobs
- auto-retry paths in `pipeline_orchestrator.py` must not move locked jobs back to `queued`
- any code path that writes `draft` or `queued` after a retry, reset, or draft-mode result must refuse that when the lock is `locked`

This ensures the policy survives future UI changes and direct status updates.

## Repair During Rollout

Backfill:

- any row with `confirmed_at IS NOT NULL` becomes `submission_lock_state = 'locked'` unless it was explicitly unlocked after the new feature exists

Repair existing inconsistent rows:

- if `confirmed_at IS NOT NULL`
- and `submission_lock_state = 'locked'`
- and status is one of the rerunnable statuses
- then force status back to `submitted`
- clear transient progress/provider fields as needed
- log `submission_lock_repaired`

Based on the current database snapshot, this rollout must repair active confirmed-submitted rows presently stuck in `draft` and `queued`. Job `#17` is the concrete repro that motivated the design, but the repair should be generic rather than one-off.

## Web UI Behavior

### Locked Jobs

For a locked submitted job:

- keep `Submitted before` visible
- add an explicit locked indicator, such as `Locked`
- hide `Restart -> Draft`
- hide `Restart -> Submit`
- hide `Reanswer` or any equivalent answer-only rerun affordance
- show `Unlock to Resubmit`

### Unlocked Jobs

For an unlocked previously submitted job:

- keep `Submitted before` visible
- show an explicit unlocked indicator, such as `Unlocked for resubmit`
- allow `Restart -> Draft`
- allow `Restart -> Submit`
- allow `Reanswer`

### Corrupted Pre-Repair Rows

If a locked job is still rendered with an inconsistent status before repair runs, the UI should still suppress rerun actions and surface the lock reason instead of trusting the raw status alone.

## Notion Behavior

The repo already reuses the same Notion page for later syncs. That behavior should stay.

However, the sync logic must stop treating resubmission like a first submission:

- keep the same page
- do not overwrite the original `Application Date`
- continue setting page `Status` to the applied/submitted status used today
- append resubmission history to `Notes`
- append or maintain a dedicated body section summarizing:
  - first submitted at
  - unlocked for resubmit at
  - latest resubmitted at
  - resubmit count

If the Notion schema contains a compatible property for latest resubmission date or count, populate it. If not, preserve that information in `Notes` and body blocks only.

This preserves the original application date while still showing that the job was intentionally reopened and resubmitted.

## Events And Observability

Add explicit timeline events:

- `submission_locked`
- `submission_unlocked_for_resubmit`
- `submission_lock_repaired`
- `resubmitted`
- `submission_lock_refused`

These events should make it obvious why a job did or did not re-enter the pipeline.

## Testing

Add targeted coverage for:

- migration/backfill of `submission_lock_state`
- repair of legacy corrupted rows
- locked-job refusal on `restart-pipeline`
- locked-job refusal on `reanswer`
- locked-job exclusion from `get_pending_jobs()`
- auto-retry and worker-reset refusal for locked jobs
- successful unlock then rerun then relock on confirmed submission
- UI action visibility for locked and unlocked previously submitted jobs
- Notion sync preserving original `Application Date` on existing pages while appending resubmission history

## Implementation Notes

Expected touchpoints:

- `scripts/job_db.py`
- `scripts/job_web.py`
- `scripts/job_worker.py`
- `scripts/pipeline_orchestrator.py`
- `scripts/static/app.js`
- `scripts/notion_job_applications.py`
- web/API, DB, and pipeline tests covering the new lock behavior

The implementation should favor a small shared guard over repeated ad hoc `confirmed_at` checks. The bug happened because submission history was displayed but not enforced. The fix should make the lock a first-class workflow rule.
