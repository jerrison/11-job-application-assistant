# Queue Confidence And Inline Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface draft confidence and inline per-job actions directly in the web queue so drafts can be judged and acted on without opening job detail first.

**Architecture:** Add a server-side `queue_review_summary` aggregator that composes existing draft-proof, answer-verification, pending-user-input, and lock-state signals into one normalized queue payload. Feed that summary into `/api/queue`, `/api/jobs/{id}`, and WebSocket job payloads, then refactor the queue renderer to use a dense three-cell layout (`Job`, `Draft Confidence`, `Actions`) with queue-specific micro-actions while the detail dock reuses the same action ids.

**Tech Stack:** Python, FastAPI, sqlite3, vanilla JS, CSS, pytest, Ruff

**Spec:** `docs/superpowers/specs/2026-04-01-queue-confidence-and-inline-actions-design.md`

---

## File Structure

### New files

- `scripts/queue_review_summary.py` — shared queue-facing derivation for overall confidence, reason chips, and normalized visible action ids
- `tests/test_queue_review_summary.py` — unit tests for the pure queue summary logic

### Modified files

- `scripts/job_web.py` — queue/detail/WebSocket payload enrichment with `queue_review_summary`
- `scripts/static/index.html` — queue header structure changes for `Job`, `Draft Confidence`, and `Actions`
- `scripts/static/app.js` — queue row renderer, queue action dispatcher, shared action descriptor mapping for queue + detail
- `scripts/static/style.css` — queue micro-button styling, confidence chip styling, responsive row layout, compact-mode selector fix
- `tests/test_job_web.py` — API, WebSocket, and static-asset regression coverage for the new queue experience

---

### Task 1: Add Shared Queue Review Summary Primitives

**Files:**
- Create: `scripts/queue_review_summary.py`
- Create: `tests/test_queue_review_summary.py`

- [ ] **Step 1: Write the failing derivation tests**

Create `tests/test_queue_review_summary.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from queue_review_summary import derive_queue_review_summary


def _job(status: str, **overrides):
    job = {
        "id": 7,
        "status": status,
        "board": "greenhouse",
        "output_dir": "/tmp/job-output",
        "archived": False,
        "previously_submitted": False,
        "submission_lock_state": None,
        "llm_generated_answers": 0,
    }
    job.update(overrides)
    return job


def test_ready_draft_is_high_confidence():
    summary = derive_queue_review_summary(
        _job("draft"),
        draft_review={"state": "ready", "reason": "ok"},
        answer_verification={"status": "verified"},
        pending_user_input=None,
    )

    assert summary["overall_confidence"] == "high"
    assert summary["confidence_label"] == "Ready to submit"
    assert [chip["label"] for chip in summary["reason_chips"]] == [
        "Proof current",
        "Answers verified",
        "No blockers",
    ]
    assert summary["visible_actions"] == [
        "approve_submit",
        "reset_to_new",
        "restart_draft",
        "restart_submit",
        "stop",
        "archive",
        "delete",
    ]


def test_stale_draft_with_verified_answers_is_medium_confidence():
    summary = derive_queue_review_summary(
        _job("draft", llm_generated_answers=3),
        draft_review={"state": "stale", "reason": "Historical proof exists"},
        answer_verification={"status": "verified"},
        pending_user_input=None,
    )

    assert summary["overall_confidence"] == "medium"
    assert summary["confidence_label"] == "Usable, but review recommended"
    assert [chip["label"] for chip in summary["reason_chips"]] == [
        "Proof stale",
        "Answers verified",
        "3 AI answers",
    ]


def test_pending_user_input_forces_low_confidence():
    summary = derive_queue_review_summary(
        _job("draft"),
        draft_review={"state": "blocked", "reason": "Missing proof"},
        answer_verification={"status": "blocked"},
        pending_user_input={"questions": [{"label": "Portfolio URL"}]},
    )

    assert summary["overall_confidence"] == "low"
    assert summary["confidence_label"] == "Needs review before submit"
    assert [chip["label"] for chip in summary["reason_chips"]] == [
        "Proof blocked",
        "Verification blocked",
        "Pending input",
    ]


def test_generating_job_reports_pending_confidence():
    summary = derive_queue_review_summary(
        _job("reanswering"),
        draft_review=None,
        answer_verification={"status": "pending"},
        pending_user_input=None,
    )

    assert summary["overall_confidence"] == "pending"
    assert summary["confidence_label"] == "Draft in progress"
    assert summary["visible_actions"] == ["stop"]


def test_locked_resubmission_exposes_unlock_before_rerun_actions():
    summary = derive_queue_review_summary(
        _job(
            "submitted",
            previously_submitted=True,
            submission_lock_state="locked",
        ),
        draft_review=None,
        answer_verification=None,
        pending_user_input=None,
    )

    assert summary["visible_actions"] == ["unlock_resubmit", "archive", "delete"]
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run python -m pytest tests/test_queue_review_summary.py -v`
Expected: FAIL because `scripts/queue_review_summary.py` does not exist yet.

- [ ] **Step 3: Implement the shared derivation module**

Create `scripts/queue_review_summary.py`:

```python
from __future__ import annotations

from pathlib import Path

from answer_verification_state import load_answer_verification_state
from application_submit_common import load_pending_user_input_for_submit_attempt
from pipeline_draft_proof import draft_review_state

PROCESSING_STATUSES = {
    "queued",
    "queued_submit",
    "approved",
    "generating",
    "resolving",
    "autofilling",
    "retrying",
    "fix_in_progress",
    "regenerating",
    "reanswering",
    "submitting",
}
ATTENTION_STATUSES = {"needs_board_url", "awaiting_captcha"}


def _chip(kind: str, label: str, tone: str) -> dict[str, str]:
    return {"kind": kind, "label": label, "tone": tone}


def visible_action_ids(job: dict) -> list[str]:
    status = str(job.get("status") or "")
    is_processing = status in {
        "generating",
        "resolving",
        "autofilling",
        "retrying",
        "fix_in_progress",
        "regenerating",
        "reanswering",
        "submitting",
    }
    is_stopped = status == "stopped" or status in ATTENTION_STATUSES
    archived = bool(job.get("archived"))
    output_dir = job.get("output_dir")
    lock_state = str(job.get("submission_lock_state") or "")

    can_restart = not is_processing and status not in {"approved", "queued", "awaiting_captcha"}
    can_archive = not is_processing and status not in {"approved", "queued", "awaiting_captcha"}
    can_delete = status in {"queued", "draft", "submitted"} or is_stopped or (
        not is_processing and status not in {"approved", "awaiting_captcha"}
    )

    if lock_state == "locked":
        actions = ["unlock_resubmit"]
        if can_archive:
            actions.append("unarchive" if archived else "archive")
        if can_delete:
            actions.append("delete")
        return actions

    if archived and not is_processing:
        actions = []
        if lock_state == "unlocked_for_resubmit":
            actions.append("lock_resubmission")
        actions.extend(["unarchive", "delete"])
        return actions

    actions: list[str] = []
    if lock_state == "unlocked_for_resubmit":
        actions.append("lock_resubmission")
    if not archived and (status == "draft" or (is_stopped and output_dir)):
        actions.extend(["approve_submit", "reset_to_new"])
    if status == "awaiting_captcha":
        actions.append("focus_browser")
    if status == "submitted":
        actions.append("resubmit")
    if can_restart:
        actions.append("restart_draft")
    if can_restart and status != "submitted":
        actions.append("restart_submit")
    if is_processing or status in {"approved", "draft", "awaiting_captcha", "queued"}:
        actions.append("stop")
    if can_archive:
        actions.append("archive")
    if can_delete:
        actions.append("delete")
    return actions


def derive_queue_review_summary(
    job: dict,
    *,
    draft_review: dict | None,
    answer_verification: dict | None,
    pending_user_input: dict | None,
) -> dict:
    status = str(job.get("status") or "")
    review_state = str((draft_review or {}).get("state") or "missing")
    verification_state = str((answer_verification or {}).get("status") or "unknown")
    pending_questions = list((pending_user_input or {}).get("questions") or [])
    llm_answers = int(job.get("llm_generated_answers") or 0)

    if status in PROCESSING_STATUSES:
        return {
            "overall_confidence": "pending",
            "confidence_label": "Draft in progress",
            "reason_chips": [_chip("state", "Waiting on pipeline", "info")],
            "proof_state": review_state,
            "verification_state": verification_state,
            "visible_actions": visible_action_ids(job),
        }

    proof_chip = {
        "ready": _chip("proof", "Proof current", "good"),
        "stale": _chip("proof", "Proof stale", "warn"),
        "legacy": _chip("proof", "Proof legacy", "warn"),
        "blocked": _chip("proof", "Proof blocked", "bad"),
        "unavailable": _chip("proof", "Posting unavailable", "bad"),
    }.get(review_state, _chip("proof", "Proof unavailable", "muted"))

    verification_chip = {
        "verified": _chip("answers", "Answers verified", "good"),
        "pending": _chip("answers", "Verification pending", "warn"),
        "blocked": _chip("answers", "Verification blocked", "bad"),
        "failed": _chip("answers", "Verification failed", "bad"),
        "not_applicable": _chip("answers", "No AI answers", "muted"),
    }.get(verification_state, None)

    if pending_questions or status in ATTENTION_STATUSES:
        friction_chip = _chip("friction", "Pending input", "bad") if pending_questions else _chip(
            "friction",
            "Needs board URL" if status == "needs_board_url" else "Manual review",
            "bad",
        )
        return {
            "overall_confidence": "low",
            "confidence_label": "Needs review before submit",
            "reason_chips": [proof_chip, verification_chip or _chip("answers", "Verification unavailable", "muted"), friction_chip],
            "proof_state": review_state,
            "verification_state": verification_state,
            "visible_actions": visible_action_ids(job),
        }

    if review_state in {"blocked", "unavailable"} or verification_state in {"blocked", "failed"}:
        return {
            "overall_confidence": "low",
            "confidence_label": "Needs review before submit",
            "reason_chips": [
                proof_chip,
                verification_chip or _chip("answers", "Verification unavailable", "muted"),
                _chip("friction", "1 blocker", "bad"),
            ],
            "proof_state": review_state,
            "verification_state": verification_state,
            "visible_actions": visible_action_ids(job),
        }

    if review_state == "ready" and verification_state in {"verified", "not_applicable"}:
        third_chip = _chip("friction", "No blockers", "good")
        if llm_answers > 0 and verification_state == "not_applicable":
            third_chip = _chip("answers", f"{llm_answers} AI answers", "muted")
        return {
            "overall_confidence": "high",
            "confidence_label": "Ready to submit",
            "reason_chips": [proof_chip, verification_chip or _chip("answers", "No AI answers", "muted"), third_chip],
            "proof_state": review_state,
            "verification_state": verification_state,
            "visible_actions": visible_action_ids(job),
        }

    chips = [proof_chip]
    if verification_chip is not None:
        chips.append(verification_chip)
    if llm_answers > 0:
        chips.append(_chip("answers", f"{llm_answers} AI answers", "muted"))
    else:
        chips.append(_chip("friction", "No blockers", "muted"))
    return {
        "overall_confidence": "medium",
        "confidence_label": "Usable, but review recommended",
        "reason_chips": chips[:3],
        "proof_state": review_state,
        "verification_state": verification_state,
        "visible_actions": visible_action_ids(job),
    }


def attach_queue_review_summary(jobs: list[dict]) -> list[dict]:
    for job in jobs:
        out_dir = job.get("output_dir")
        if out_dir:
            review = draft_review_state(Path(out_dir), board_name=job.get("board"))
            verification = load_answer_verification_state(Path(out_dir))
            pending = load_pending_user_input_for_submit_attempt(Path(out_dir))
            pending_payload = pending[1] if pending is not None else None
        else:
            review = None
            verification = None
            pending_payload = None
        job["queue_review_summary"] = derive_queue_review_summary(
            job,
            draft_review=review,
            answer_verification=verification,
            pending_user_input=pending_payload,
        )
    return jobs
```

- [ ] **Step 4: Run the new tests again**

Run: `uv run python -m pytest tests/test_queue_review_summary.py -v`
Expected: PASS

- [ ] **Step 5: Commit the shared summary primitives**

```bash
command git add scripts/queue_review_summary.py tests/test_queue_review_summary.py
command git commit -m "feat(web): add queue review summary derivation"
```

---

### Task 2: Expose Queue Review Summary In Queue, Detail, And WebSocket Payloads

**Files:**
- Modify: `scripts/job_web.py`
- Modify: `tests/test_job_web.py`

- [ ] **Step 1: Write the failing API and WebSocket tests**

Append to `tests/test_job_web.py`:

```python
def test_queue_endpoint_includes_queue_review_summary(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/review-summary"]})
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "pending_user_input.json").write_text(
        json.dumps({"questions": [{"label": "Portfolio URL"}]}),
        encoding="utf-8",
    )

    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir))

    resp = client.get("/api/queue")

    assert resp.status_code == 200
    summary = resp.json()["jobs"][0]["queue_review_summary"]
    assert summary["overall_confidence"] == "low"
    assert summary["confidence_label"] == "Needs review before submit"
    assert "approve_submit" in summary["visible_actions"]


def test_job_detail_includes_queue_review_summary(client):
    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/detail-summary"]})

    resp = client.get("/api/jobs/1")

    assert resp.status_code == 200
    assert "queue_review_summary" in resp.json()
    assert "visible_actions" in resp.json()["queue_review_summary"]


def test_websocket_initial_bulk_includes_queue_review_summary(client):
    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/ws-summary"]})

    with client.websocket_connect("/ws") as ws:
        bulk = ws.receive_json()

    assert bulk["type"] == "job_bulk"
    assert "queue_review_summary" in bulk["jobs"][0]
    assert "visible_actions" in bulk["jobs"][0]["queue_review_summary"]
```

- [ ] **Step 2: Run the focused Web UI tests to verify they fail**

Run: `uv run python -m pytest tests/test_job_web.py -k "queue_review_summary or websocket_initial_bulk_includes_queue_review_summary" -v`
Expected: FAIL because the current payloads do not include `queue_review_summary`.

- [ ] **Step 3: Add one queue-row enrichment helper and use it everywhere the queue consumes job rows**

Modify `scripts/job_web.py`:

```python
from queue_review_summary import attach_queue_review_summary


def _enrich_queue_rows(conn: sqlite3.Connection, jobs: list[dict]) -> list[dict]:
    if not jobs:
        return jobs
    _add_submitted_flags(conn, jobs)
    return attach_queue_review_summary(jobs)
```

Replace the queue/detail/WebSocket call sites:

```python
@app.get("/api/queue")
def list_queue(
    status: str | None = None,
    board: str | None = None,
    search: str | None = None,
    sort_field: str = "updated_at",
    sort_dir: str = "desc",
    limit: int = 200,
    offset: int = 0,
):
    if limit < 1 or limit > 500:
        raise HTTPException(400, "limit must be between 1 and 500")
    if offset < 0:
        raise HTTPException(400, "offset must be non-negative")
    conn = get_conn()
    jobs = _enrich_queue_rows(
        conn,
        query_queue_jobs(
            conn,
            status=status,
            board=board,
            search=search,
            sort_field=sort_field,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        ),
    )
    return {
        "jobs": jobs,
        "counts": get_queue_counts(conn),
        "total": count_queue_jobs(conn, status=status, board=board, search=search),
        "limit": limit,
        "offset": offset,
    }
```

```python
@app.get("/api/jobs/{job_id}")
def get_job_detail(job_id: int):
    conn = get_conn()
    job = get_job(conn, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    _enrich_queue_rows(conn, [job])
    timeline = get_job_timeline(conn, job_id)
    job["timeline"] = timeline
    return job
```

```python
def get_changed_jobs(self, conn: sqlite3.Connection) -> list[dict]:
    jobs = _enrich_queue_rows(conn, query_jobs(conn, limit=500))
    changed = []
    new_state = {}
    for job in jobs:
        job_id = job["id"]
        updated_at = job.get("updated_at", "")
        new_state[job_id] = updated_at
        if self._last_state.get(job_id) != updated_at:
            changed.append(job)
    for job_id in set(self._last_state) - set(new_state):
        changed.append({"id": job_id, "_deleted": True})
    self._last_state = new_state
    return changed
```

```python
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    conn = get_conn()
    jobs = _enrich_queue_rows(conn, query_queue_jobs(conn, limit=200))
    await ws.send_text(json.dumps({"type": "job_bulk", "jobs": jobs}, default=str))
    while True:
        await ws.receive_text()
```

- [ ] **Step 4: Run the focused Web UI tests again**

Run: `uv run python -m pytest tests/test_job_web.py -k "queue_review_summary or websocket_initial_bulk_includes_queue_review_summary" -v`
Expected: PASS

- [ ] **Step 5: Commit the API and WebSocket integration**

```bash
command git add scripts/job_web.py tests/test_job_web.py
command git commit -m "feat(web): expose queue review summary in api payloads"
```

---

### Task 3: Refactor Queue Rendering Around Job, Confidence, And Actions

**Files:**
- Modify: `scripts/static/index.html`
- Modify: `scripts/static/app.js`
- Modify: `tests/test_job_web.py`

- [ ] **Step 1: Write the failing static-asset tests for the new queue structure**

Append to `tests/test_job_web.py`:

```python
def test_root_queue_headers_use_job_review_and_actions_columns(client):
    resp = client.get("/")

    assert resp.status_code == 200
    assert '>Job <span class="sort-icon"></span></th>' in resp.text
    assert ">Draft Confidence</th>" in resp.text
    assert ">Actions</th>" in resp.text
    assert 'colspan="4"' in resp.text


def test_app_js_builds_queue_review_and_queue_actions_from_summary(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "function buildQueueJobCell(job)" in resp.text
    assert "function buildQueueConfidenceCell(job)" in resp.text
    assert "function buildQueueActionsCell(job)" in resp.text
    assert "function runQueueAction(event, jobId, actionId)" in resp.text
    assert "event.stopPropagation();" in resp.text
    assert "job.queue_review_summary" in resp.text


def test_app_js_removes_row_click_navigation_from_queue_rows(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert 'onclick="rowClick(event, ${job.id})"' not in resp.text


def test_app_js_uses_shared_action_ids_for_queue_and_detail(client):
    resp = client.get("/static/app.js")

    assert resp.status_code == 200
    assert "function actionDescriptorForId(job, actionId, surface = 'detail')" in resp.text
    assert "function getJobActionModels(job, surface = 'detail')" in resp.text
    assert "visible_actions" in resp.text
```

- [ ] **Step 2: Run the focused static-asset tests to verify they fail**

Run: `uv run python -m pytest tests/test_job_web.py -k "queue_headers_use_job_review_and_actions_columns or queue_review_and_queue_actions_from_summary or removes_row_click_navigation or shared_action_ids" -v`
Expected: FAIL because the queue still renders the old status/company/role/board layout.

- [ ] **Step 3: Replace the queue row builder with explicit job, confidence, and actions cells**

Modify `scripts/static/index.html`:

```html
<table class="job-table" id="job-table">
  <thead>
    <tr>
      <th class="col-check"><input type="checkbox" id="select-all" onchange="toggleSelectAll()"></th>
      <th class="col-job sortable" onclick="sortTable('company')">Job <span class="sort-icon"></span></th>
      <th class="col-review">Draft Confidence</th>
      <th class="col-actions">Actions</th>
    </tr>
  </thead>
  <tbody id="job-tbody">
    <tr class="empty-row"><td colspan="4">Loading</td></tr>
  </tbody>
</table>
```

Modify the loading and empty states in `scripts/static/app.js`:

```javascript
if (showLoading && tbody && !window.queueRows.length) {
  tbody.innerHTML = '<tr class="empty-row"><td colspan="4">Loading</td></tr>';
}
if (jobs.length === 0) {
  tbody.innerHTML = '<tr class="empty-row"><td colspan="4">No jobs found.</td></tr>';
  updateBulkActionsBar();
  return;
}
```

Add shared queue/detail action mapping and the new queue builders:

```javascript
function actionDescriptorForId(job, actionId, surface = 'detail') {
  const labels = surface === 'queue'
    ? {
        approve_submit: 'Submit',
        reset_to_new: 'Reset',
        restart_draft: 'Redraw',
        restart_submit: 'Redraw + Submit',
        resubmit: 'Resubmit',
        focus_browser: 'Focus Browser',
        unlock_resubmit: 'Unlock',
        lock_resubmission: 'Relock',
        stop: 'Stop',
        archive: 'Archive',
        unarchive: 'Unarchive',
        delete: 'Delete',
      }
    : {
        approve_submit: 'Approve + Submit',
        reset_to_new: 'Reset to New',
        restart_draft: 'Restart → Draft',
        restart_submit: 'Restart → Submit',
        resubmit: 'Resubmit',
        focus_browser: 'Focus Browser',
        unlock_resubmit: 'Unlock to Resubmit',
        lock_resubmission: 'Lock Resubmission',
        stop: 'Stop',
        archive: 'Archive',
        unarchive: 'Unarchive',
        delete: 'Delete',
      };
  const handlers = {
    approve_submit: () => approveJob(job.id),
    reset_to_new: () => resetJobToNew(job.id),
    restart_draft: () => restartPipeline(job.id, false),
    restart_submit: () => restartPipeline(job.id, true),
    resubmit: () => restartPipeline(job.id, true),
    focus_browser: () => focusCaptchaBrowser(job.id),
    unlock_resubmit: () => unlockForResubmit(job.id),
    lock_resubmission: () => lockResubmission(job.id),
    stop: () => stopJob(job.id),
    archive: () => archiveJob(job.id),
    unarchive: () => unarchiveJob(job.id),
    delete: () => deleteJob(job.id),
  };
  const classNames = {
    approve_submit: surface === 'queue' ? 'btn-success queue-action-btn' : 'btn-success',
    reset_to_new: surface === 'queue' ? 'btn-outline queue-action-btn' : 'btn-warning',
    restart_draft: surface === 'queue' ? 'btn-outline queue-action-btn' : 'btn-outline',
    restart_submit: surface === 'queue' ? 'btn-outline queue-action-btn' : 'btn-outline',
    resubmit: surface === 'queue' ? 'btn-success queue-action-btn' : 'btn-success',
    focus_browser: surface === 'queue' ? 'btn-primary queue-action-btn' : 'btn-primary',
    unlock_resubmit: surface === 'queue' ? 'btn-primary queue-action-btn' : 'btn-primary',
    lock_resubmission: surface === 'queue' ? 'btn-outline queue-action-btn' : 'btn-outline',
    stop: surface === 'queue' ? 'btn-danger queue-action-btn' : 'btn-danger',
    archive: surface === 'queue' ? 'btn-outline queue-action-btn' : 'btn-outline',
    unarchive: surface === 'queue' ? 'btn-outline queue-action-btn' : 'btn-outline',
    delete: surface === 'queue' ? 'btn-outline btn-delete queue-action-btn' : 'btn-outline btn-delete',
  };
  if (!labels[actionId] || !handlers[actionId]) return null;
  return { id: actionId, label: labels[actionId], className: classNames[actionId], handler: handlers[actionId] };
}

function getJobActionModels(job, surface = 'detail') {
  const visibleActions = Array.isArray(job.queue_review_summary?.visible_actions)
    ? job.queue_review_summary.visible_actions
    : [];
  return visibleActions
    .map(actionId => actionDescriptorForId(job, actionId, surface))
    .filter(Boolean);
}

async function runQueueAction(event, jobId, actionId) {
  event.preventDefault();
  event.stopPropagation();
  const job = window.jobs[jobId];
  const action = actionDescriptorForId(job, actionId, 'queue');
  if (!action) return;
  await action.handler();
}

function buildQueueJobCell(job) {
  const company = escapeHtml(job.company || '—');
  const role = escapeHtml(job.role_title || '—');
  const board = escapeHtml(job.board || '—');
  const when = escapeHtml(timeAgo(queueTimestamp(job)));
  const status = escapeHtml(statusLabel(job.status));
  const lockLabel = submissionLockLabel(job);
  const prevSub = job.previously_submitted ? '<span class="queue-meta-pill">Submitted before</span>' : '';
  const lock = lockLabel ? `<span class="queue-meta-pill">${escapeHtml(lockLabel)}</span>` : '';
  return `<div class="queue-job-cell">
    <div class="queue-job-topline">
      <a class="job-id-link" data-open-job href="#job/${job.id}">#${job.id}</a>
      <span class="status-badge ${statusClass(job.status)}">${status}</span>
      <span class="queue-board-label">${board}</span>
    </div>
    <a class="queue-job-title" data-open-job href="#job/${job.id}">${role}</a>
    <div class="queue-company-line">${company}</div>
    <div class="queue-job-meta">
      <span class="queue-meta-pill">${when}</span>
      ${prevSub}
      ${lock}
      ${_jobUrlIcons(job)}
    </div>
  </div>`;
}

function buildQueueConfidenceCell(job) {
  const review = job.queue_review_summary || {};
  const confidence = escapeHtml((review.overall_confidence || 'na').toUpperCase());
  const label = escapeHtml(review.confidence_label || 'Unavailable');
  const chips = Array.isArray(review.reason_chips) ? review.reason_chips : [];
  return `<div class="queue-review-summary">
    <div class="queue-review-heading">
      <span class="queue-confidence-badge" data-confidence="${escapeHtml(review.overall_confidence || 'na')}">${confidence}</span>
      <span class="queue-confidence-label">${label}</span>
    </div>
    <div class="queue-review-chips">
      ${chips.map(chip => `<span class="queue-review-chip" data-tone="${escapeHtml(chip.tone || 'muted')}">${escapeHtml(chip.label || '')}</span>`).join('')}
    </div>
  </div>`;
}

function buildQueueActionsCell(job) {
  return `<div class="queue-actions">
    ${getJobActionModels(job, 'queue').map(action => `<button class="btn ${action.className}" onclick="runQueueAction(event, ${job.id}, '${action.id}')">${escapeHtml(action.label)}</button>`).join('')}
  </div>`;
}

function buildQueueRow(job) {
  return `<tr data-job-id="${job.id}">
    <td class="col-check"><input type="checkbox" data-id="${job.id}" onclick="toggleRowCheck(event, ${job.id})"></td>
    <td class="col-job">${buildQueueJobCell(job)}</td>
    <td class="col-review">${buildQueueConfidenceCell(job)}</td>
    <td class="col-actions">${buildQueueActionsCell(job)}</td>
  </tr>`;
}
```

Update the detail dock to reuse the same action ids:

```javascript
function renderJobActionRow(job) {
  const row = document.getElementById('action-row');
  row.innerHTML = '';
  getJobActionModels(job, 'detail').forEach(action => {
    row.appendChild(makeBtn(action.label, action.className, action.handler));
  });
  row.scrollLeft = 0;
  refreshScrollableDockRails();
}
```

- [ ] **Step 4: Run the focused static-asset tests again**

Run: `uv run python -m pytest tests/test_job_web.py -k "queue_headers_use_job_review_and_actions_columns or queue_review_and_queue_actions_from_summary or removes_row_click_navigation or shared_action_ids" -v`
Expected: PASS

- [ ] **Step 5: Commit the queue renderer refactor**

```bash
command git add scripts/static/index.html scripts/static/app.js tests/test_job_web.py
command git commit -m "feat(web): render queue confidence and inline actions"
```

---

### Task 4: Add Queue-Specific Micro-Action And Confidence Styling

**Files:**
- Modify: `scripts/static/style.css`
- Modify: `tests/test_job_web.py`

- [ ] **Step 1: Write the failing CSS regression tests**

Append to `tests/test_job_web.py`:

```python
def test_style_css_adds_queue_review_and_micro_action_selectors(client):
    resp = client.get("/static/style.css")

    assert resp.status_code == 200
    assert ".queue-job-cell" in resp.text
    assert ".queue-review-summary" in resp.text
    assert ".queue-actions" in resp.text
    assert ".queue-action-btn" in resp.text
    assert ".queue-confidence-badge" in resp.text


def test_style_css_compact_mode_targets_job_table_not_queue_table(client):
    resp = client.get("/static/style.css")

    assert resp.status_code == 200
    assert '[data-compact="true"] .job-table td' in resp.text
    assert '[data-compact="true"] .queue-table td' not in resp.text
```

- [ ] **Step 2: Run the focused CSS tests to verify they fail**

Run: `uv run python -m pytest tests/test_job_web.py -k "queue_review_and_micro_action_selectors or compact_mode_targets_job_table_not_queue_table" -v`
Expected: FAIL because the queue-specific selectors do not exist yet and compact mode still targets `.queue-table`.

- [ ] **Step 3: Add the new queue layout and compact styles**

Modify `scripts/static/style.css`:

```css
/* Queue columns */
.col-job { min-width: 340px; }
.col-review { width: 280px; }
.col-actions { width: 360px; }

.queue-job-cell {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.queue-job-topline,
.queue-job-meta,
.queue-review-heading,
.queue-review-chips,
.queue-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.queue-job-title {
  color: var(--base01);
  font-size: 14px;
  font-weight: 700;
  line-height: 1.3;
  text-decoration: none;
}

.queue-job-title:hover,
.queue-job-title:focus,
.queue-job-title:focus-visible {
  color: var(--blue);
  text-decoration: underline;
}

.queue-company-line {
  color: var(--base0);
  font-size: 12px;
}

.queue-board-label,
.queue-meta-pill,
.queue-review-chip {
  display: inline-flex;
  align-items: center;
  padding: 3px 7px;
  border-radius: 999px;
  font-size: 11px;
  line-height: 1.2;
}

.queue-meta-pill,
.queue-board-label {
  background: var(--base2);
  color: var(--base01);
}

.queue-confidence-badge {
  display: inline-flex;
  align-items: center;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.queue-confidence-badge[data-confidence="high"] { background: rgba(133,153,0,0.15); color: var(--green); }
.queue-confidence-badge[data-confidence="medium"] { background: rgba(181,137,0,0.15); color: var(--yellow); }
.queue-confidence-badge[data-confidence="low"] { background: rgba(220,50,47,0.15); color: var(--red); }
.queue-confidence-badge[data-confidence="pending"] { background: rgba(42,161,152,0.15); color: var(--cyan); }
.queue-confidence-badge[data-confidence="na"] { background: rgba(131,148,150,0.15); color: var(--base0); }

.queue-confidence-label {
  color: var(--base01);
  font-size: 12px;
  font-weight: 700;
}

.queue-review-chip[data-tone="good"] { background: rgba(133,153,0,0.12); color: var(--green); }
.queue-review-chip[data-tone="warn"] { background: rgba(181,137,0,0.12); color: var(--yellow); }
.queue-review-chip[data-tone="bad"] { background: rgba(220,50,47,0.12); color: var(--red); }
.queue-review-chip[data-tone="info"] { background: rgba(42,161,152,0.12); color: var(--cyan); }
.queue-review-chip[data-tone="muted"] { background: var(--base2); color: var(--base0); }

.queue-actions {
  justify-content: flex-start;
  align-items: flex-start;
}

.queue-action-btn {
  min-height: 30px;
  padding: 4px 8px;
  font-size: 11px;
  line-height: 1.2;
}

[data-compact="true"] .job-table td { padding: 6px 10px; }
[data-compact="true"] .job-table th { padding: 6px 10px; }
[data-compact="true"] .queue-action-btn { min-height: 28px; padding: 3px 7px; }

@media (max-width: 768px) {
  .col-job,
  .col-review,
  .col-actions { width: auto; }
  .queue-job-topline,
  .queue-job-meta,
  .queue-review-heading,
  .queue-review-chips,
  .queue-actions { justify-content: flex-start; }
  .queue-actions { margin-top: 6px; }
}
```

- [ ] **Step 4: Run the focused CSS tests again**

Run: `uv run python -m pytest tests/test_job_web.py -k "queue_review_and_micro_action_selectors or compact_mode_targets_job_table_not_queue_table" -v`
Expected: PASS

- [ ] **Step 5: Commit the queue styling changes**

```bash
command git add scripts/static/style.css tests/test_job_web.py
command git commit -m "fix(web): compress queue actions and confidence styling"
```

---

### Task 5: Verify End-To-End And Lock In The Queue Experience

**Files:**
- Modify: `tests/test_job_web.py` if browser verification exposes a gap that is not already covered

- [ ] **Step 1: Run the focused automated checks**

Run: `uv run python -m pytest tests/test_queue_review_summary.py tests/test_job_web.py -v`
Expected: PASS

- [ ] **Step 2: Launch the web UI and verify the queue manually**

Run: `uv run python scripts/job_web.py --host 127.0.0.1 --port 8420`
Expected: FastAPI starts locally and serves the queue UI at `http://127.0.0.1:8420`

Manual verification:

1. Load the queue at a normal desktop width and confirm rows render as `Job | Draft Confidence | Actions`.
2. Narrow the viewport until the action lane wraps and confirm no buttons clip horizontally.
3. On safe draft rows, click inline `Submit`, `Redraw`, and `Archive` controls repeatedly for at least 10 total interactions and confirm the queue stays in queue view and the row updates in place.
4. Resize to `768px` width or narrower and confirm the row stacks with all actions still visible.
5. Open one job from the title link and confirm the detail dock still shows the same valid action set as the queue.

- [ ] **Step 3: Run the full repo verification suite**

Run: `uv run python -m pytest tests/ -v`
Expected: PASS

Run: `uv run ruff check scripts/ tests/`
Expected: PASS

Run: `uv run python scripts/check_architecture.py`
Expected: PASS

Run: `uv run python scripts/sync_agent_files.py --check`
Expected: PASS

Run: `uv run python scripts/check_agent_docs.py`
Expected: PASS

- [ ] **Step 4: Commit any final verification-driven polish**

```bash
command git add scripts/queue_review_summary.py scripts/job_web.py scripts/static/index.html scripts/static/app.js scripts/static/style.css tests/test_queue_review_summary.py tests/test_job_web.py
command git commit -m "feat(web): ship queue confidence and inline actions"
```

---

## Self-Review

### Spec coverage

- Queue-level confidence before opening detail: covered by Tasks 1, 2, and 3.
- All valid queue actions visible inline: covered by Tasks 1 and 3.
- Smaller buttons / clipping fix: covered by Task 4.
- Shared queue/detail action semantics: covered by Tasks 2 and 3.
- Desktop and mobile behavior: covered by Tasks 4 and 5.
- Browser verification with repeated interactions: covered by Task 5.

### Placeholder scan

- No placeholder markers remain in executable steps.
- Every task names exact files, concrete test names, commands, and commit messages.

### Type consistency

- Server payload field name is consistently `queue_review_summary`.
- Normalized action ids are consistently:
  - `approve_submit`
  - `reset_to_new`
  - `restart_draft`
  - `restart_submit`
  - `resubmit`
  - `focus_browser`
  - `unlock_resubmit`
  - `lock_resubmission`
  - `stop`
  - `archive`
  - `unarchive`
  - `delete`
- Frontend helper names are consistently:
  - `actionDescriptorForId`
  - `getJobActionModels`
  - `runQueueAction`
  - `buildQueueJobCell`
  - `buildQueueConfidenceCell`
  - `buildQueueActionsCell`
