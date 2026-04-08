---
title: "feat: Delete jobs from Duplicate Jobs modal"
type: feat
status: completed
date: 2026-03-24
---

# feat: Delete jobs from Duplicate Jobs modal

The Duplicate Jobs modal currently only offers "View" links per row. Users need to delete duplicates directly from this modal without navigating away. Add per-row delete buttons and per-group "Keep Oldest" bulk actions.

## Enhancement Summary

**Deepened on:** 2026-03-24 (3 rounds)
**Review agents used:** Frontend Races, Performance, Security, Simplicity, Pattern Recognition, Architecture, Best Practices Research, Codebase Explorer (race conditions + status map + apiCall errors + runDedup template + test patterns)

### Key Improvements
1. **Bug fix:** DELETE endpoint silently succeeds for non-existent jobs (returns 200, not 404) — the "not found" catch is dead code; simplify to just catch network/server errors
2. **Bug fix:** Reuse existing `PROCESSING_STATUSES` constant instead of defining a new inline set (which drifts — missing `regenerating`)
3. **Race condition fix:** Disable group buttons during Keep Oldest to prevent concurrent delete corruption
4. **Race condition fix:** `isConnected` guard in cleanup helper to handle concurrent rapid deletes
5. **Race condition fix:** Re-check active status at click time (WebSocket may update between render and click)
6. **Bug fix:** `dedupCleanupGroup` used `header.textContent` which would destroy the Keep Oldest button — use `<span class="dedup-count">` instead

### New Considerations Discovered
- Cascade delete misses `candidate_jobs.promoted_job_id` — dangling reference after delete
- Dedup status badges use raw `job.status` instead of `statusClass()`/`statusLabel()` — fix while touching the code
- Guard `removeQueueRow` with `if (!window.jobs[id]) return` to prevent redundant re-renders from WebSocket echo during bulk deletes
- Simplicity reviewer argues Keep Oldest is YAGNI — kept because 15+ groups makes per-row-only tedious, but could be deferred

## Acceptance Criteria

- [ ] Each job row in the dedup modal has a "Delete" button
- [ ] Delete button disabled for active-status jobs (reuse `PROCESSING_STATUSES`, which now includes `reanswering`) with tooltip
- [ ] Active status re-checked at click time (WebSocket may have updated since render)
- [ ] Clicking Delete shows `window.confirm()` with job ID, company, and status
- [ ] After deletion: row removed from DOM, `window.jobs` updated, group cleaned up via `dedupCleanupGroup()`
- [ ] When a group is reduced to 1 job, the group is removed from the modal
- [ ] Each group header has a "Keep Oldest" button that deletes all but the oldest job
- [ ] "Keep Oldest" disables all buttons in the group during operation (prevents race with per-row Delete)
- [ ] "Keep Oldest" confirmation lists the jobs that will be deleted with their statuses
- [ ] "Keep Oldest" skips active-status jobs with a toast warning
- [ ] When all groups are resolved, modal body shows "All duplicates resolved."
- [ ] Deleting from the modal does NOT navigate to `#/queue` — user stays in the modal
- [ ] Toast shown per successful delete
- [ ] Dedup status badges use `statusClass()`/`statusLabel()` (fix pre-existing inconsistency)

## Context

**No backend changes needed** for the core feature. The existing `DELETE /api/jobs/{job_id}` endpoint (`scripts/job_web.py:929`) handles full cascade deletion across 6 child tables. WebSocket broadcasts `job_deleted` automatically.

The modal is treated as a snapshot — no WebSocket-driven DOM updates inside it.

**Important:** The DELETE endpoint silently succeeds for non-existent jobs (SQLite DELETE on missing row is a no-op, endpoint returns `{"status": "deleted"}`). It does NOT return 404. This means error handling only needs to cover network failures and server errors, not "already deleted" races.

The queue view behind the modal stays in sync automatically: the WebSocket `job_deleted` broadcast triggers `removeQueueRow()`, which updates both `window.jobs` and the queue table DOM.

### Performance Notes

Sequential deletes are correct here. SQLite uses a single writer lock, so parallel requests would serialize anyway. Per-delete latency on loopback is ~3-8ms. A group of 10 (9 deletes) completes in ~70ms — imperceptible. No batch endpoint needed.

## Design Decisions

1. **New `dedupDeleteJob()` function** rather than reusing `deleteJob()` — the existing function navigates to `#/queue` after delete, which would close the modal
2. **`window.confirm()` for confirmation** — matches existing destructive action pattern (e.g., `killSingleWorker`)
3. **Reuse `PROCESSING_STATUSES` constant** at `app.js:19` — do NOT define a new inline set. Add `reanswering` to the constant (currently appended ad-hoc with `|| s === 'reanswering'` in 8 call sites)
4. **"Keep Oldest" uses `created_at` ordering** — matches `find_jd_duplicates` query which already orders `ASC`. Confirmation dialog names the kept job and lists the deletions so the user can cancel if a newer job has more progress
5. **Disable group buttons during Keep Oldest** — prevents race condition where user clicks per-row Delete while bulk operation is in-flight
6. **`isConnected` guard in `dedupCleanupGroup`** — prevents operating on detached DOM from concurrent rapid deletes

## MVP

### `scripts/static/app.js` — update `PROCESSING_STATUSES` (line 19)

Add `reanswering` to the existing constant so all callers share one definition:

```javascript
const PROCESSING_STATUSES = new Set([
  'generating', 'resolving', 'submitting', 'autofilling',
  'retrying', 'fix_in_progress', 'regenerating', 'reanswering'
]);
```

Then remove the `|| s === 'reanswering'` ad-hoc checks scattered across the file (lines 39, 473, 516, 946, 2514).

### `scripts/static/app.js` — new function `dedupCleanupGroup(groupEl)`

Shared helper used by both `dedupDeleteJob` and `dedupKeepOldest`:

```javascript
function dedupCleanupGroup(groupEl) {
  if (!groupEl.isConnected) return; // already removed by concurrent delete
  const remaining = groupEl.querySelectorAll('tbody tr');
  if (remaining.length <= 1) {
    groupEl.remove();
  } else {
    // Update count via the <span class="dedup-count"> — NOT header.textContent
    // (textContent would destroy the Keep Oldest button inside the h3)
    const countEl = groupEl.querySelector('.dedup-count');
    if (countEl) countEl.textContent = remaining.length;
  }
  const body = document.getElementById('dedup-body');
  if (body && !body.querySelector('.dedup-group')) {
    body.innerHTML = '<p>All duplicates resolved.</p>';
  }
}
```

### `scripts/static/app.js` — new function `dedupDeleteJob(jobId, btn)`

```javascript
async function dedupDeleteJob(jobId, btn) {
  // Re-check active status at click time (WebSocket may have updated since render)
  const job = window.jobs[jobId];
  if (job && PROCESSING_STATUSES.has(job.status)) {
    showToast(`Job #${jobId} is now ${job.status} — cannot delete`, 'warning');
    btn.disabled = true;
    btn.title = 'Job is actively processing';
    return;
  }
  // Check if group has a bulk operation in progress
  const group = btn.closest('.dedup-group');
  if (group._dedupBusy) {
    showToast('Bulk operation in progress — please wait', 'warning');
    return;
  }
  const label = job ? `#${jobId} (${job.company} — ${job.role_title}, ${job.status})` : `#${jobId}`;
  if (!confirm(`Delete ${label}? This cannot be undone.`)) return;
  try {
    await apiCall('DELETE', `/api/jobs/${jobId}`);
    // DELETE endpoint returns 200 even for non-existent jobs (SQLite DELETE is idempotent)
    showToast(`Deleted #${jobId}`, 'info');
    delete window.jobs[jobId];
    btn.closest('tr').remove();
    dedupCleanupGroup(group);
  } catch (e) {
    // Only fires on network error or 500 — DELETE never returns 404
    showToast('Delete failed: ' + e.message, 'error');
  }
}
```

### `scripts/static/app.js` — new function `dedupKeepOldest(groupEl)`

```javascript
async function dedupKeepOldest(groupEl) {
  const rows = [...groupEl.querySelectorAll('tbody tr')];
  const keepRow = rows[0]; // oldest (query orders by created_at ASC)
  const deleteRows = rows.slice(1);
  const toDelete = [];
  const skipped = [];
  for (const row of deleteRows) {
    const jobId = parseInt(row.dataset.jobId);
    const job = window.jobs[jobId];
    if (job && PROCESSING_STATUSES.has(job.status)) {
      skipped.push(`#${jobId} (${job.status})`);
    } else {
      toDelete.push({ id: jobId, row, label: job ? `#${jobId} ${job.company}` : `#${jobId}` });
    }
  }
  if (toDelete.length === 0) {
    showToast('No deletable jobs — all are actively processing', 'warning');
    return;
  }
  const keepId = parseInt(keepRow.dataset.jobId);
  const msg = `Keep #${keepId} and delete ${toDelete.length} job(s):\n${toDelete.map(j => j.label).join('\n')}` +
    (skipped.length ? `\n\nSkipping ${skipped.length} active: ${skipped.join(', ')}` : '') +
    '\n\nThis cannot be undone.';
  if (!confirm(msg)) return;

  // Disable all buttons in group to prevent race with per-row Delete
  const buttons = groupEl.querySelectorAll('button');
  buttons.forEach(b => b.disabled = true);
  groupEl._dedupBusy = true;

  let ok = 0, fail = 0;
  try {
    for (const { id, row } of toDelete) {
      try {
        await apiCall('DELETE', `/api/jobs/${id}`);
        delete window.jobs[id];
        row.remove();
        ok++;
      } catch (_) { fail++; }
    }
    showToast(ok + ' jobs deleted' + (fail ? ', ' + fail + ' failed' : ''), ok ? 'info' : 'error');
  } finally {
    groupEl._dedupBusy = false;
    dedupCleanupGroup(groupEl);
  }
}
```

### `scripts/static/app.js` — modify `runDedup()` rendering (~line 3496)

Add `data-job-id` to each `<tr>`, add Delete button column, add "Keep Oldest" button to group header, disable Delete for active statuses, and fix status badges:

```javascript
// In the group rendering loop, add to table header:
// <th>Actions</th>

// Fix status badges to use proper helpers (pre-existing inconsistency):
// <td><span class="status-badge ${statusClass(job.status)}">${statusLabel(job.status)}</span></td>

// In each row, add after the View link cell:
const isActive = PROCESSING_STATUSES.has(job.status);
// <td>
//   <button class="btn btn-sm btn-outline btn-delete"
//     onclick="dedupDeleteJob(${job.id}, this)"
//     ${isActive ? 'disabled title="Job is actively processing"' : ''}>
//     Delete
//   </button>
// </td>

// Each <tr> gets: data-job-id="${job.id}"

// Group header gets "Keep Oldest" button:
// <button class="btn btn-sm btn-outline" onclick="dedupKeepOldest(this.closest('.dedup-group'))">Keep Oldest</button>
```

### `scripts/static/app.js` — guard `removeQueueRow` (~line 610)

Prevent redundant re-renders from WebSocket echo during bulk dedup deletes:

```javascript
function removeQueueRow(id) {
  if (!window.jobs[id]) return; // already handled (dedup modal or prior WS)
  // ... existing logic
}
```

## Deep Dive: Race Conditions

The codebase has **no AbortController, no busy flags, no request deduplication** anywhere in `app.js`. All `window.jobs` mutations are direct and synchronous. The dedup modal `closeDedupModal()` only does `display:none` — no DOM destruction, no cleanup. This means:

### Confirmed Safe
- **Keep Oldest loop iteration** — pre-snapshots rows into array, `row.remove()` on detached node is no-op, sequential `await` prevents concurrent iterations
- **Double `delete window.jobs[id]`** — benign (JS delete on missing key is no-op). WebSocket echo will try to re-delete after `dedupDeleteJob` already removed it

### Mitigated by Plan
- **Delete during Keep Oldest** — `_dedupBusy` flag + button disabling prevents concurrent operations on same group
- **Rapid parallel deletes** — `isConnected` guard in `dedupCleanupGroup` prevents operating on detached DOM
- **Stale active status** — re-check at click time catches WebSocket-updated status
- **Redundant re-renders** — `removeQueueRow` early return when `!window.jobs[id]` prevents 2N renders during bulk delete (N from direct path + N from WebSocket echo)

### Accepted Risks (low severity)
- **Modal close during flight** — in-flight async callbacks will update detached (hidden) DOM and fire orphan toasts. Generation counter would fix this but adds complexity for a rare scenario. The `isConnected` guard catches the DOM case; orphan toasts are cosmetic.
- **apiCall has no timeout** — relies on browser fetch defaults. A network hang during Keep Oldest blocks the loop. No AbortController exists anywhere in the codebase, so adding one for just this feature would be inconsistent.

### How to Test Race Conditions
1. **Delete-during-Keep-Oldest:** DevTools → Network → Slow 3G. Click Keep Oldest, then try clicking Delete on a row in the same group. Should see "Bulk operation in progress" toast.
2. **WebSocket echo:** Add `console.count('renderQueue')` temporarily. Delete one job from modal. Should see exactly 1 renderQueue call (not 2) thanks to the `removeQueueRow` guard.
3. **Stale status:** Start a worker on a queued job in a dedup group. While it transitions to `generating`, click Delete in the modal. Should see "Job is now generating — cannot delete" toast.

## Deep Dive: Keep Oldest UX

### Why Keep Oldest (not just per-row Delete)
The screenshot shows 15+ duplicate groups. Per-row-only delete would require 15+ individual confirm dialogs to clean up one duplicate per group. Keep Oldest handles the common case (keep the original, delete the re-crawls) in one click per group.

### UX Pattern Choice
Research shows three tiers of destructive confirmation (Cloudscape/AWS pattern):
1. **One-click** — for easily recreatable resources (not applicable)
2. **Simple confirmation** — shows what will be deleted + count (our choice)
3. **Type-to-confirm** — for irreversible high-stakes actions (overkill for dev tool)

`window.confirm()` is appropriate here because:
- The dedup modal is already a "review and decide" workflow — user is pausing to inspect
- confirm() shows job IDs + companies + statuses so user can verify
- Matches the existing `killSingleWorker` pattern at `app.js:451`
- The alternative (inline confirmation modal) would require building a sub-modal inside a modal

### Visual Feedback During Keep Oldest
During the sequential delete loop, add row-level feedback:
- On success: `row.style.opacity = '0.3'` before `row.remove()` (instant visual confirmation)
- On failure: leave row at full opacity with a "Failed" indicator
- All buttons disabled during the operation (already in plan)

This matches the Airflow/Databricks pattern for sequential async operations in admin UIs.

### Future Enhancement (not in scope)
If users frequently want to keep the most-progressed job instead of oldest, add a "Keep Best" button that prefers `submitted` > `draft` > `queued` > `stopped`. But `created_at` ordering covers the common case and matches the DB query.

## Deep Dive: Active Status Consolidation

### Current State — 9+ Inline Definitions Across 5 Files

| Location | Statuses | Missing vs Full Set |
|----------|----------|-------------------|
| `app.js:19` `PROCESSING_STATUSES` | generating, resolving, submitting, autofilling, retrying, fix_in_progress, regenerating | Missing `reanswering` |
| `app.js:1524` polling active | generating, resolving, submitting, autofilling, retrying, fix_in_progress | Missing `regenerating`, `reanswering` |
| `job_web.py:409` WS broadcast | generating, resolving, submitting, autofilling, retrying, fix_in_progress, reanswering | Missing `regenerating` |
| `job_web.py:727` regenerate guard | generating, resolving, approved, submitting, autofilling, retrying, fix_in_progress, reanswering, regenerating | Includes `approved` |
| `job_web.py:1095` workers/status | generating, resolving, submitting, retrying, fix_in_progress, reanswering | **Missing `autofilling`** (bug?) |
| `job_tui.py:123` _PROCESSING | resolving, generating, submitting, autofilling, fix_in_progress, retrying, approved | Missing `reanswering`, `regenerating` |
| `job_db.py:773` submit_phase | approved, submitting, reanswering, awaiting_captcha | Subset (submit only) |
| `job_db.py:795` in_progress | resolving, generating, fix_in_progress, retrying, regenerating | Subset (gen only) |
| `job_worker.py:413` kill submit | approved, submitting, reanswering, awaiting_captcha | Matches db:773 |

### `reanswering` — The Invisible Coupling
In `app.js`, `reanswering` is checked ad-hoc with `|| status === 'reanswering'` at **5 locations** (lines 39, 473, 516, 946, 2514). It was never added to `PROCESSING_STATUSES`. This creates an invisible coupling where adding a new status requires finding all the ad-hoc checks.

### Plan for This Feature (minimum viable fix)
1. Add `reanswering` to `PROCESSING_STATUSES` at `app.js:19`
2. Remove the 5 ad-hoc `|| s === 'reanswering'` checks
3. Use `PROCESSING_STATUSES` in dedup modal code — no new inline set

### Broader Consolidation (separate ticket)
Define canonical sets in `job_db.py` alongside `JOB_STATUSES`:

```python
# job_db.py
ACTIVE_STATUSES = frozenset({
    "generating", "resolving", "submitting", "autofilling",
    "retrying", "fix_in_progress", "regenerating", "reanswering",
})
SUBMIT_PHASE_STATUSES = frozenset({
    "approved", "submitting", "reanswering", "awaiting_captcha",
})
```

Then have `job_web.py`, `job_worker.py`, `job_tui.py` import these instead of defining inline. Serve the JS set via `/api/config` endpoint so `app.js` doesn't maintain its own copy. This eliminates the drift risk permanently but is out of scope for this feature.

## Deep Dive: apiCall Error Handling

### Error Message Map for DELETE /api/jobs/{id}

| Scenario | HTTP Status | apiCall Throws | e.message |
|----------|-------------|----------------|-----------|
| Job exists, deleted | 200 | Nothing (returns `{"status": "deleted"}`) | N/A |
| Job doesn't exist | **200** | Nothing (SQLite DELETE is idempotent) | N/A |
| Server error | 500 | `Error("Internal Server Error")` | `"Internal Server Error"` |
| Network down | N/A | `TypeError("Failed to fetch")` | `"Failed to fetch"` |
| DB locked/corrupt | 500 | `Error("database is locked")` or similar | SQLite error text |

**Key insight:** The DELETE endpoint (`job_web.py:929`) does NOT check if the job exists before deleting. It runs 7 DELETE statements and commits — all no-ops if the job is gone. Returns `{"status": "deleted"}` regardless. **No 404 is ever returned.**

This means the `dedupDeleteJob` catch block only needs to handle network failures and server errors. The "already deleted" race condition resolves silently as a success.

### apiCall internals (`app.js:93-109`)
- Throws `Error(res.statusText)` or `Error(response.json().detail)` on non-2xx
- Network errors propagate as `TypeError` (not caught internally)
- Returns `null` on 2xx with empty/malformed JSON body (line 108)
- No timeout — relies on browser defaults

## Deep Dive: Exact runDedup() Template

### Current HTML structure (`app.js:3489-3510`)

```javascript
// Group container
`<div class="dedup-group">
  <h3>Group ${i + 1} (${group.length} jobs)</h3>
  <table class="dedup-table">
    <thead><tr>
      <th>ID</th><th>Company</th><th>Role</th><th>Status</th><th>Created</th><th></th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>
</div>`

// Each row (lines 3497-3507)
`<tr>
  <td>#${job.id}</td>
  <td>${escapeHtml(job.company || '')}</td>
  <td>${escapeHtml(job.role_title || '')}</td>
  <td><span class="status-badge status-${job.status}">${job.status}</span></td>
  <td>${created}</td>
  <td><a href="#job/${job.id}" onclick="closeDedupModal()" class="btn btn-sm btn-outline">View</a></td>
</tr>`
```

### Modified template (exact implementation)

```javascript
// Group container — add "Keep Oldest" button to header
`<div class="dedup-group">
  <h3>Group ${i + 1} (<span class="dedup-count">${group.length}</span> jobs)
    <button class="btn btn-sm btn-outline" style="margin-left:12px;font-size:12px"
      onclick="dedupKeepOldest(this.closest('.dedup-group'))">Keep Oldest</button>
  </h3>
  <table class="dedup-table">
    <thead><tr>
      <th>ID</th><th>Company</th><th>Role</th><th>Status</th><th>Created</th><th></th><th></th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>
</div>`

// Each row — add data-job-id, fix status badge, add Delete button
const isActive = PROCESSING_STATUSES.has(job.status);
`<tr data-job-id="${job.id}">
  <td>#${job.id}</td>
  <td>${escapeHtml(job.company || '')}</td>
  <td>${escapeHtml(job.role_title || '')}</td>
  <td><span class="status-badge ${statusClass(job.status)}">${statusLabel(job.status)}</span></td>
  <td>${created}</td>
  <td><a href="#job/${job.id}" onclick="closeDedupModal()" class="btn btn-sm btn-outline">View</a></td>
  <td><button class="btn btn-sm btn-outline btn-delete"
    onclick="dedupDeleteJob(${job.id}, this)"
    ${isActive ? 'disabled title="Job is actively processing"' : ''}>Delete</button></td>
</tr>`
```

### CSS additions needed (`style.css`)

```css
/* No new CSS needed — existing classes cover it:
   .btn-delete already styled (red on hover)
   .dedup-table td already has padding
   .status-badge + status-* classes already exist
   button:disabled already has opacity:0.5 + cursor:not-allowed */
```

### Helper functions used (already exist)
- `statusClass(status)` — `app.js:36-43` — maps status to CSS class (e.g., `reanswering` → `status-generating`)
- `statusLabel(status)` — `app.js:45-67` — maps status to display text (e.g., `reanswering` → `"Processing"`)
- `escapeHtml(str)` — `app.js:26` — prevents XSS in company/role text
- `closeDedupModal()` — `app.js:3522-3525` — hides modal via `display:none`

## Deep Dive: Testing Strategy

### Backend Tests (`tests/test_job_web.py`)

Existing `test_delete_job` at line 97 only tests happy path. Add:

```python
def test_delete_job_cascade(client):
    """Verify cascade deletes all child table rows."""
    resp = client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/1"]})
    job_id = 1
    # Insert child records in events, fix_attempts, provider_runs, etc.
    # DELETE the job
    resp = client.delete(f"/api/jobs/{job_id}")
    assert resp.status_code == 200
    # Verify child records are gone

def test_delete_nonexistent_job(client):
    """DELETE on non-existent ID returns 200 (current behavior, documents it)."""
    resp = client.delete("/api/jobs/99999")
    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted"}

def test_delete_job_removes_from_dedup(client):
    """After deleting a duplicate, find_jd_duplicates returns fewer groups."""
    # Create 2 jobs with same fingerprint
    # Verify they appear as duplicates
    # Delete one
    # Verify group is gone (only 1 job left)
```

### Frontend Manual Test Script

Write to `/tmp/test_dedup_delete.js` and paste into browser console:

```javascript
// Test 1: Per-row delete
// - Open dedup modal
// - Click Delete on any row
// - Verify confirm dialog shows job info
// - Confirm → verify row removed, group count updated
// - Verify window.jobs no longer has that ID

// Test 2: Keep Oldest
// - Open dedup modal
// - Click Keep Oldest on a group with 3+ jobs
// - Verify confirm lists jobs to delete
// - Confirm → verify only oldest row remains
// - Verify group removed if ≤1 row left

// Test 3: Active status guard
// - Start a worker so a job transitions to 'generating'
// - Open dedup modal
// - Verify Delete button is disabled on that row
// - Click it anyway → should be no-op (disabled)

// Test 4: Buttons disabled during Keep Oldest
// - Throttle network to Slow 3G
// - Click Keep Oldest
// - Try clicking Delete on a row in same group → should see toast warning

// Test 5: All groups resolved
// - Delete all duplicates across all groups
// - Verify modal shows "All duplicates resolved."
```

### Race Condition Tests (manual, DevTools)

See "How to Test Race Conditions" section in the Race Conditions deep dive above.

## Optional Improvements (not blocking)

These were identified by reviewers but are out of scope for this feature:

1. **Extract `delete_job()` to `job_db.py`** — duplicated cascade between `job_web.py:929` and `job_tui.py:540`. Separate prerequisite refactor commit.
2. **Add `candidate_jobs.promoted_job_id` cleanup** to the delete cascade — currently leaves dangling reference.
3. **Wrap cascade DELETE in explicit transaction** — `with conn:` for atomicity on crash.
4. **Add backend status guard to DELETE endpoint** — currently unconditional, unlike regenerate/restart endpoints which check status.
5. **Generation counter for modal close** — prevents ghost DOM updates if modal closed during in-flight deletes. Low severity since modal hides rather than destroys DOM.

## Sources

- Existing delete endpoint: `scripts/job_web.py:929-940`
- Existing JS delete: `scripts/static/app.js:1640-1649` (`deleteJob`), `scripts/static/app.js:764-772` (`bulkDelete`)
- Existing PROCESSING_STATUSES: `scripts/static/app.js:19`
- Dedup modal rendering: `scripts/static/app.js:3480-3525` (`runDedup`)
- Dedup DB query: `scripts/job_db.py:516-536` (`find_jd_duplicates`)
- Modal HTML: `scripts/static/index.html:637-648`
- Active statuses (backend): `scripts/job_web.py:409`, `scripts/job_web.py:727`, `scripts/job_web.py:867`
- Status badge helpers: `scripts/static/app.js` (`statusClass`, `statusLabel`)
- WebSocket delete handler: `scripts/static/app.js:171-174` (`removeQueueRow`)
