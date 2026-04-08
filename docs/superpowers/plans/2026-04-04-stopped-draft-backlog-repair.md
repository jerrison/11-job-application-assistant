# Stopped And Draft Backlog Sweep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Audit the live stopped/draft backlog, implement generalized fixes for repairable bugs across boards and surfaces, rerun the affected jobs in `--draft` with fresh readable proof, and leave repo-local, Obsidian, Linear, and git state fully updated.

**Architecture:** Treat repo-local artifacts and the shared audit helpers as the source of truth. Split the backlog into truthful/manual inventory vs repairable engineering work, fix only the repairable bugs, then rerun the affected cohorts so queue state, screenshot proof, docs, and Linear all converge on the same current evidence.

**Tech Stack:** Python 3.14, SQLite, Playwright, pytest, Ruff, `uv run python`, Linear MCP, local Obsidian mirror

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `.context/compound-engineering/todos/004-ready-p1-sweep-live-stopped-and-draft-backlog.md` | Durable cross-session tracker for the full sweep |
| Modify | `docs/superpowers/plans/2026-04-04-stopped-draft-backlog-repair.md` | Current implementation plan for the sweep |
| Modify | `docs/solutions/workflow-issues/stopped-job-audits-must-be-repo-local-and-artifact-backed-2026-03-27.md` | Canonical repo-local audit write-up with the fresh queue snapshot and proof |
| Modify | `docs/solutions/workflow-issues/current-draft-proof-must-backfill-stale-root-answer-state-from-active-submit-artifacts-2026-04-05.md` | Canonical repo-local learning for stale root answer-state reconciliation |
| Modify | `/Users/jerrison/My Drive (jerrisonli@gmail.com)/00. Top Folder/04-obsidian-vaults/jerrison-personal-gdrive/Jobs Application Automation.md` | Convenience mirror of the repo-local findings |
| Modify | `scripts/pipeline_audit_loop.py` | Shared repairable-vs-terminal audit helpers if new backlog rules are needed |
| Modify | `scripts/job_db.py` | Disk-sync truthfulness and stopped/draft state reconciliation fixes |
| Modify | `scripts/pipeline_orchestrator.py` | Worker/runtime behavior when reruns or stop-state reconciliation need generalization |
| Modify | `scripts/application_submit_common.py` | Shared answer, proof, and pending-user-input behavior across boards |
| Modify | `scripts/autofill_*.py` | Board-specific generalized fixes discovered from the repairable cohorts |
| Modify | `bin/job-assets` | Shared retry/sync/redraft workflow if the CLI needs backlog-sweep support |
| Modify | `tests/test_job_db.py` | Queue truthfulness regressions |
| Modify | `tests/test_pipeline_orchestrator.py` | Retry and worker-path regressions |
| Modify | `tests/test_submit_application.py` | Shared draft/proof regressions |
| Modify | board-specific tests under `tests/` | Regression coverage for new board-local bugs |

### Task 1: Materialize the live backlog manifests

**Files:**
- Modify: `.context/compound-engineering/todos/004-ready-p1-sweep-live-stopped-and-draft-backlog.md`

- [ ] **Step 1: Capture the live queue counts**

Run:

```bash
sqlite3 jobs.db "select status, count(*) from jobs where archived = 0 group by status order by count(*) desc;"
sqlite3 jobs.db "select coalesce(board,'<null>') as board, failure_type, count(*) from jobs where archived = 0 and status = 'stopped' group by coalesce(board,'<null>'), failure_type order by count(*) desc limit 40;"
```

Expected: `stopped`, `draft`, `submitted`, and `queued` counts plus the largest stopped clusters.

- [ ] **Step 2: Split stopped/draft rows with the shared audit helpers**

Run:

```bash
uv run python - <<'PY'
import sqlite3, sys
from collections import Counter
sys.path.insert(0, "scripts")
from pipeline_audit_loop import audit_draft_outcome, audit_stopped_outcome

conn = sqlite3.connect("jobs.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "select id, status, board, failure_type, error_message, output_dir "
    "from jobs where archived = 0 and status in ('stopped','draft')"
).fetchall()

stopped = Counter()
draft = Counter()
for row in rows:
    if row["status"] == "stopped":
        decision = audit_stopped_outcome(
            failure_type=row["failure_type"],
            error_message=row["error_message"] or "",
        )
        stopped[(decision.kind, decision.failure_type or "")] += 1
    else:
        decision = audit_draft_outcome(row["output_dir"], board_name=row["board"])
        draft[(decision.kind, decision.failure_type or "")] += 1

print("STOPPED")
for key, value in stopped.most_common():
    print(key, value)
print("DRAFT")
for key, value in draft.most_common():
    print(key, value)
PY
```

Expected: a clean split between repairable generic stopped rows and truthful/manual stopped rows, plus the current failing draft-audit count.

- [ ] **Step 3: Record the counts in the todo work log**

Add the exact counts, the current date, and the dominant repairable clusters to:

```text
.context/compound-engineering/todos/004-ready-p1-sweep-live-stopped-and-draft-backlog.md
```

### Task 2: Sample the repairable clusters and identify generalized bugs

**Files:**
- Modify: `.context/compound-engineering/todos/004-ready-p1-sweep-live-stopped-and-draft-backlog.md`
- Modify: `docs/solutions/workflow-issues/stopped-job-audits-must-be-repo-local-and-artifact-backed-2026-03-27.md`

- [ ] **Step 1: Pull representative rows for the largest repairable clusters**

Run:

```bash
uv run python - <<'PY'
import sqlite3
conn = sqlite3.connect("jobs.db")
conn.row_factory = sqlite3.Row
clusters = [
    ("greenhouse", "retries_exhausted"),
    ("ashby", "retries_exhausted"),
    ("<null>", "retries_exhausted"),
    ("workday", "retries_exhausted"),
    ("smartrecruiters", "retries_exhausted"),
    ("lever", "retries_exhausted"),
    ("rippling", "retries_exhausted"),
]
for board, failure_type in clusters:
    clause = "board is null" if board == "<null>" else "board = ?"
    params = [failure_type] if board == "<null>" else [board, failure_type]
    sql = (
        "select id, company, role, coalesce(output_dir, '') as output_dir, "
        "substr(replace(coalesce(error_message,''), char(10), ' '), 1, 200) as error_message "
        f"from jobs where archived = 0 and status = 'stopped' and {clause} and failure_type = ? "
        "order by id desc limit 5"
    )
    if board == "<null>":
        rows = conn.execute(sql, params).fetchall()
    else:
        rows = conn.execute(sql, params).fetchall()
    print(f\"\\n## {board} / {failure_type}\")
    for row in rows:
        print(row["id"], row["company"], row["role"], row["output_dir"], row["error_message"])
PY
```

Expected: five current canaries per repairable cluster with output dirs and truncated error text.

- [ ] **Step 2: Inspect the active artifacts for each cluster**

For each sampled canary, inspect:

```bash
ls -1 output/<company>/<role>/submit*
sed -n '1,200p' output/<company>/<role>/submit/application_submission_result.json
sed -n '1,200p' output/<company>/<role>/submit/*_autofill_report.json
```

Expected: one or more repeatable root-cause signatures instead of a single generic retry label.

- [ ] **Step 3: Create NAD Linear issues per generalized bug**

For each distinct generalized bug found, create a Linear issue with:
- the cluster counts
- representative job IDs and output dirs
- the pre-fix artifact evidence
- the suspected generalized root cause
- the planned regression tests and rerun canaries

Expected: one issue per bug class, not one issue per job.

### Task 3: Repair the failing draft audits first

**Files:**
- Modify: shared/board files implicated by the 13 failing draft audits
- Modify: `tests/test_job_db.py`
- Modify: `tests/test_submit_application.py`
- Modify: board-specific tests as needed

- [ ] **Step 1: Materialize the failing draft rows**

Run:

```bash
uv run python - <<'PY'
import sqlite3, sys
sys.path.insert(0, "scripts")
from pipeline_audit_loop import audit_draft_outcome

conn = sqlite3.connect("jobs.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "select id, board, company, role, output_dir from jobs "
    "where archived = 0 and status = 'draft' order by id"
).fetchall()
for row in rows:
    decision = audit_draft_outcome(row["output_dir"], board_name=row["board"])
    if decision.kind != "ready":
        print(row["id"], row["board"], row["company"], row["role"], decision.failure_type, decision.reason, row["output_dir"])
PY
```

Expected: exactly the current failing `draft_audit_incomplete` and `rendered_audit_mismatch` rows.

- [ ] **Step 2: Reproduce each failing draft audit on disk before changing code**

For each failing row:

```bash
uv run python - <<'PY'
import sys
sys.path.insert(0, "scripts")
from pipeline_audit_loop import audit_draft_outcome
print(audit_draft_outcome("output/<company>/<role>", board_name="<board>"))
PY
```

Expected: the exact same failure reason as the manifest.

- [ ] **Step 3: Add or update the failing regression tests before implementation**

Use the smallest targeted test slices that cover the failing proof contract or rendered-value mismatch.

- [ ] **Step 4: Implement the shared or board-local fix and re-run the targeted tests**

Run:

```bash
uv run python -m pytest tests/test_job_db.py tests/test_submit_application.py -k "draft_audit or rendered_audit or pending_user_input" -v
```

Expected: PASS for the newly added failing regressions and the surrounding draft-proof coverage.

### Task 4: Repair one stopped cluster at a time with generalized fixes

**Files:**
- Modify: the runtime files implicated by each cluster
- Modify: corresponding tests under `tests/`

- [ ] **Step 1: Start with the largest repairable cluster**

Use the sampled Greenhouse `retries_exhausted` cohort first unless the artifact evidence clearly shows another cluster has a smaller, higher-confidence fix.

- [ ] **Step 2: Lock in a failing regression**

Add the smallest failing regression that reproduces the generalized bug in code rather than only in saved artifacts.

- [ ] **Step 3: Implement the minimal generalized fix**

Prefer shared helpers and queue-truthfulness paths over board-local one-offs when the same contract should hold across multiple boards or surfaces.

- [ ] **Step 4: Run focused verification**

Run only the targeted tests for the touched surfaces first, for example:

```bash
uv run python -m pytest tests/test_autofill_greenhouse.py tests/test_job_db.py -k "<new-keyword>" -v
uv run python -m pytest tests/test_autofill_workday.py tests/test_pipeline_orchestrator.py -k "<new-keyword>" -v
```

Expected: PASS

- [ ] **Step 5: Repeat for the next repairable cluster**

Move to the next cluster only after:
- the regression exists
- the fix is landed
- the canary rerun plan is known
- the Linear issue has been updated

### Task 5: Rerun canaries and then the affected cohorts in `--draft`

**Files:**
- Modify: repo docs and issue trackers only; rerun artifacts live under `output/`

- [ ] **Step 1: Redraft one canary per fixed bug before any bulk retry**

Run:

```bash
uv run python scripts/submit_application.py output/<company>/<role> --draft --provider openai
```

Expected: the canary no longer stops for the old repairable reason, and it writes fresh current-attempt screenshots plus report artifacts.

- [ ] **Step 2: Sync the DB from disk**

Run:

```bash
uv run bin/job-assets sync
```

Expected: the canary row reflects the new truth (`draft` or a newer truthful/manual terminal state).

- [ ] **Step 3: Retry the full affected cohort only after the canary passes**

Use the repo’s retry path for the explicit job IDs in the affected cluster. Keep truthful/manual terminals out of the retry set.

- [ ] **Step 4: Capture fresh screenshot proof**

For each representative fixed job, preserve:
- the pre-submit screenshot
- the review screenshot
- the current answers/report artifacts
- a web UI ready-to-submit view when the result is `draft`

### Task 6: Audit the draft backlog for deterministic correctness and quality

**Files:**
- Modify: shared/board files implicated by any draft-review mismatch
- Modify: repo docs and Obsidian mirror

- [ ] **Step 1: Re-run the draft audit manifest after each batch of fixes**

Use the Task 3 manifest command again until all repairable draft rows audit as `ready`.

- [ ] **Step 2: Visually inspect ready drafts for deterministic correctness**

For representative drafts across current boards, verify that:
- work authorization answers remain truthful
- compensation answers remain "open and flexible"
- degree/license/certification answers only auto-affirm when supported by `application_profile.md` or `master_resume.md`
- affirmative positive-fit answers still match the configured default
- the screenshots are full, legible, and show a ready-to-submit state

- [ ] **Step 3: Fix any deterministic-answer drift with shared logic first**

If a drift is found, patch the shared answer-selection layer before applying any board-local fallback.

### Task 7: Refresh durable docs, Linear, Obsidian, git, and final verification

**Files:**
- Modify: `docs/solutions/workflow-issues/stopped-job-audits-must-be-repo-local-and-artifact-backed-2026-03-27.md`
- Modify: `/Users/jerrison/My Drive (jerrisonli@gmail.com)/00. Top Folder/04-obsidian-vaults/jerrison-personal-gdrive/Jobs Application Automation.md`
- Modify: `.context/compound-engineering/todos/004-ready-p1-sweep-live-stopped-and-draft-backlog.md`

- [ ] **Step 1: Refresh the repo-local audit doc**

Document:
- current queue counts
- the repairable-vs-terminal split
- each generalized bug and its NAD issue
- representative rerun outcomes
- screenshot evidence paths
- remaining truthful/manual inventory

- [ ] **Step 2: Mirror the findings into Obsidian**

Update:

```text
/Users/jerrison/My Drive (jerrisonli@gmail.com)/00. Top Folder/04-obsidian-vaults/jerrison-personal-gdrive/Jobs Application Automation.md
```

Do not reintroduce superseded requests such as the old "space 5" AeroSpace note.

- [ ] **Step 3: Run the required verification commands**

Run:

```bash
uv run python -m pytest tests/ -v
uv run ruff check scripts/ tests/
uv run python scripts/check_architecture.py
uv run python scripts/sync_agent_files.py --check
uv run python scripts/check_agent_docs.py
```

Expected: PASS, or explicit documentation of any unrelated pre-existing failures.

- [ ] **Step 4: Commit, push, and merge the code plus data updates**

Use non-destructive git commands only. Include:
- the code changes
- the updated docs
- any intentional queue/data/artifact updates required for the fresh proof

- [ ] **Step 5: Mark the todo complete**

Rename:

```text
.context/compound-engineering/todos/004-ready-p1-sweep-live-stopped-and-draft-backlog.md
```

from `ready` to `complete` only after the acceptance criteria are actually satisfied.

## 2026-04-05 Progress Update

- Rippling canary fix completed for Malwarebytes `#1439`:
  - real pre-fix reproduction forcing the old shared-only fill path: `output/playwright/nad-31-malwarebytes-pre.png`
  - canonical post-fix rerun: `output/malwarebytes/sr-technical-pm-core-tech/submit/rippling_autofill_pre_submit.png`
  - `uv run bin/job-assets sync` now moves `#1439` back to `draft`
- Linear tracking for that bug is complete:
  - `NAD-31` created, proof comment added, pre/post screenshots attached, state moved to `Done`
- Repairable stopped backlog handling has moved from diagnosis to rerun:
  - current queue snapshot after the bulk repairable requeue:
    - `stopped = 561`
    - `queued = 424`
    - `draft = 145`
    - `submitted = 262`
    - `generating = 1`
  - the live stopped audit now shows `0` repairable stopped rows because all `418` repairable rows were requeued via `requeue_jobs_for_repair_redraft(...)`
- New follow-up discovered while validating Rippling generalization:
  - Rippling `#99` (`output/rippling/product-lead-automation-platform`) now returns HTTP `404` for both the listing and `/apply` paths
  - treat this as a separate stale-job truthfulness issue, not part of the Malwarebytes selector fix
- Root answer-state reconciliation landed and was verified through `NAD-32`:
  - added `scripts/answer_state_sync.py` and wired it into submit flow, repo-local disk sync, and draft-summary generation
  - targeted verification passed:
    - `uv run python -m pytest tests/test_answer_state_sync.py tests/test_submit_application.py tests/test_job_db.py tests/test_draft_manager.py -v`
    - `315 passed`
    - Ruff passed on all touched files
  - live repo-local `uv run bin/job-assets sync` applied the fix to saved outputs
  - representative evidence:
    - Turo `#143` now writes root verification as `not_applicable` and the live job detail reports `Ready to submit`
    - Abridge `#727` now writes root answer refresh as `fresh`
  - issue artifacts:
    - `output/playwright/nad-32-turo-lead-pm-host-pre.png`
    - `output/playwright/nad-32-turo-lead-pm-host-post.png`
    - `output/playwright/nad-32-abridge-product-lead-life-sciences-new-products-pre.png`
    - `output/playwright/nad-32-abridge-product-lead-life-sciences-new-products-post.png`
    - `output/playwright/nad-32-job-143-web-ui.png`
    - `output/playwright/nad-32-job-727-web-ui.png`
  - remaining draft backlog exposed by that pass:
    - `generated::unknown::fresh = 65`
    - these drafts now have current refresh proof but still lack current verification proof
