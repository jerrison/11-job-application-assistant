---
title: "Redrafted queue rows showed stale completion age"
category: ui-bugs
module: Job Queue UI
date: 2026-03-26
tags:
  - queue-ui
  - redraft
  - queue-timestamp
  - completed-at
  - updated-at
  - job-tui
  - job-web
component: tooling
components:
  - scripts/job_db.py
  - scripts/job_web.py
  - scripts/static/app.js
  - scripts/job_tui.py
  - tests/test_job_db.py
  - tests/test_job_web.py
problem_type: ui_bug
symptoms:
  - "Queue rows could still show `8d ago` after a redraft."
  - "Draft rows with fresh `updated_at` values looked stale because queue recency preferred historical `completed_at`."
  - "The web queue and TUI queue were deriving recency from different raw columns."
root_cause: logic_error
resolution_type: code_fix
severity: medium
---

# Redrafted Queue Rows Showed Stale Completion Age

## Problem

A redrafted job could still look stale in the queue because the UI was showing historical completion time instead of current queue freshness. For job `#29`, the row stayed at `8d ago` even though the job had been redrafted and `updated_at` had moved to `2026-03-26 22:01:07`; the stale value came from `completed_at = 2026-03-18 05:10:46`.

## Symptoms

- User requested a redraft, but row `#29` still showed `8d ago`.
- Database inspection showed the job was actually fresh:

```text
id=29
status=draft
updated_at=2026-03-26 22:01:07
completed_at=2026-03-18 05:10:46
```

- The web queue comment described the right intent, but the implementation did not follow it:

```js
// Show contextual timestamp: completed_at for terminal states, updated_at for active, created_at for queued
const _when = job.completed_at || job.updated_at || job.created_at;
```

- The queue surfaces had already drifted apart: the web queue preferred `completed_at`, while the TUI was still rendering `created_at`.

## What Didn't Work

- Checking SQLite alone did not fix anything. The stored data was already correct; the bug was in queue presentation logic.
- Restarting the server without changing timestamp selection would not help, because the renderer would still prefer stale `completed_at`.
- Letting each surface compute its own `when` field caused inconsistent behavior. The web queue and TUI were already using different raw columns.

## Solution

Move queue-facing timestamp selection into the shared data layer and have every queue surface consume that value.

Before, the web queue always preferred `completed_at`:

```js
const _when = job.completed_at || job.updated_at || job.created_at;
```

After, `scripts/job_db.py` attaches a shared queue-facing timestamp:

```python
def _with_queue_timestamp(job: dict) -> dict:
    """Attach the queue-facing timestamp without losing historical completion data."""
    if job.get("status") == "submitted" and job.get("completed_at"):
        job["queue_timestamp"] = job["completed_at"]
        job["queue_timestamp_source"] = "completed_at"
        return job
    if job.get("updated_at"):
        job["queue_timestamp"] = job["updated_at"]
        job["queue_timestamp_source"] = "updated_at"
        return job
    if job.get("created_at"):
        job["queue_timestamp"] = job["created_at"]
        job["queue_timestamp_source"] = "created_at"
        return job
    job["queue_timestamp"] = job.get("completed_at")
    job["queue_timestamp_source"] = "completed_at" if job.get("completed_at") else None
    return job
```

`get_job()` and `query_jobs()` now both attach `queue_timestamp` and `queue_timestamp_source`, so the API ships one consistent answer.

The web queue now uses the shared field first:

```js
function queueTimestamp(job) {
  if (job.queue_timestamp) return job.queue_timestamp;
  if (job.status === 'submitted' && job.completed_at) return job.completed_at;
  return job.updated_at || job.created_at || job.completed_at;
}
```

The TUI was also updated to stop hard-coding `created_at` and to label the column `When` instead of `Created`:

```python
# before
_format_ts(job.get("created_at"))

# after
_format_ts(job.get("queue_timestamp") or job.get("updated_at") or job.get("created_at"))
```

## Why This Works

`completed_at` is the right timestamp for a truly terminal submitted row, but it is the wrong freshness signal once a job re-enters the queue after a redraft. Centralizing the rule in one shared helper makes the selection status-aware, keeps historical completion data intact, and prevents the web UI, API, and TUI from drifting into different interpretations. `queue_timestamp_source` also makes the decision observable during debugging.

## Verification

- `uv run python -m pytest tests/test_job_db.py tests/test_job_web.py -k "queue_timestamp or redraft or prefers_updated_at or prefers_completed_at" -v`
- `uv run ruff check scripts/job_db.py scripts/job_tui.py tests/test_job_db.py tests/test_job_web.py`
- `node --check scripts/static/app.js`
- Live API verification after restarting the web server on `http://127.0.0.1:8420`:

```json
{
  "id": 29,
  "status": "draft",
  "updated_at": "2026-03-26 22:01:07",
  "completed_at": "2026-03-18 05:10:46",
  "queue_timestamp": "2026-03-26 22:01:07",
  "queue_timestamp_source": "updated_at"
}
```

- Browser verification on `http://127.0.0.1:8420/#queue` confirmed row `#29` rendered from the fresh timestamp, showing `9m ago` at validation time.
- Repeated reload verification confirmed the queue row stayed fresh across five reloads:

```json
["9m ago", "9m ago", "9m ago", "9m ago", "9m ago"]
```

- Browser console after the fix only showed an unrelated missing `favicon.ico`.

## Prevention

- Keep queue timestamp selection centralized. Queue surfaces should render `queue_timestamp`, not recompute `completed_at || updated_at || created_at`.
- Keep regression tests for both branches of the rule:

```python
assert result["queue_timestamp"] == "2026-03-26 22:01:07"
assert result["queue_timestamp_source"] == "updated_at"
assert result["queue_timestamp_source"] == "completed_at"
```

- Keep API coverage so the client cannot regress silently:

```python
assert resp.json()[0]["queue_timestamp"] == "2026-03-26 22:01:07"
```

- Any queue-surface change should be checked in all three places: shared job serialization, web queue, and TUI queue.
- Finish queue timestamp fixes with the same checklist used here: targeted pytest, `ruff`, `node --check`, live API inspection, and repeated browser reloads. Use the rendered UI state as the final source of truth.

## Investigation Steps

1. Started from the user report and screenshot showing a draft row still rendering `8d ago` after a redraft request.
2. Queried `jobs.db` and confirmed the job had a fresh `updated_at` but a stale historical `completed_at`.
3. Traced the queue renderer in `scripts/static/app.js` and found it always preferred `completed_at`, despite the comment describing a status-aware rule.
4. Checked adjacent queue surfaces and found the TUI still rendered `created_at`, which meant queue recency logic had already drifted across surfaces.
5. Moved timestamp selection into `scripts/job_db.py`, wired both queue surfaces to the shared value, and added regression coverage before re-verifying the live UI.

## Cross-References

- Related: `docs/solutions/workflow-issues/explicit-answer-regeneration-requires-durable-fresh-proof-2026-03-26.md`
- Related: `docs/solutions/logic-errors/visible-self-id-draft-blockers-2026-03-26.md`
- Related: `docs/solutions/ui-bugs/job-detail-proof-card-obscured-review-content-2026-03-26.md`
- GitHub issues: no related issues found via `gh issue list --search "queue timestamp redraft updated_at completed_at stale when column job_web app.js" --state all --limit 5`
