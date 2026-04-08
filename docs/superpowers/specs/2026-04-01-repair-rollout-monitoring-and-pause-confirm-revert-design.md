# Repair Rollout Monitoring And Pause-Confirm-Revert

Date: 2026-04-01
Status: Approved for spec review
Related:

- [2026-03-31-self-repair-supervisor-and-rendered-state-audit-design.md](./2026-03-31-self-repair-supervisor-and-rendered-state-audit-design.md)
- [worker-pipeline-patterns.md](../../worker-pipeline-patterns.md)
- [operational-rules.md](../../operational-rules.md)

## Problem

The repo now has the first half of the self-repair flow:

- repairable failures can be fingerprinted into repair clusters
- the repair supervisor can require a failing regression test, run targeted verification, canary a candidate fix, push the promoted commit to `origin/main`, and redraft affected jobs
- repair failures already write repo-local cluster notes and an active repair-failure index

The remaining Task 7/8 gap is what happens after a repair is promoted.

Right now the runtime does not yet:

- persist a human-meaningful active rollout record beyond the minimal `repair_rollouts` row
- compare post-fix outcomes against a comparable cohort on the touched board/phase
- pause fresh queued work when a rollout appears to regress the queue
- run a one-pass confirmation check before deciding whether the signal was real
- auto-revert a promoted repair on confirmed regression
- surface active rollout state in a repo-local rollout index or worker-status payload

That leaves the system with an unsafe middle state: it can auto-push a repair to `origin/main`, but it cannot yet monitor that repair or contain blast radius if the promoted fix regresses neighboring jobs.

## Goals

- Keep rollout monitoring generalized across boards and runtime surfaces.
- Reuse the existing repair supervisor instead of introducing a second orchestration daemon.
- Compare comparable jobs, not raw global queue totals.
- Pause only fresh queued work while allowing already-running or already-approved work to drain.
- Require one confirmation pass before any automatic revert.
- Keep the rollout state human-readable in repo-local markdown, not only in SQLite rows.
- Preserve draft-mode fail-closed behavior: if a promoted fix appears to regress the queue, prefer pausing and reverting over continued autonomous expansion.

## Non-Goals

- Replacing the existing repair-cluster or canary flow.
- Counting truthful terminal states such as `unsupported`, `external_apply`, `pending_user_input`, `skipped_captcha`, `job_closed`, or auth/manual-control outcomes as regressions by default.
- Adding a second long-running rollout-monitor daemon.
- Performing a broad schema expansion for rollout metadata when the current JSON columns can hold the needed context.
- Reverting unrelated commits or rewinding repo history with destructive git operations.

## Current Gap

The current repair supervisor records only a minimal rollout attempt:

- `cluster_id`
- `commit_sha`
- `status`
- optional baseline/post-fix JSON
- optional `revert_sha`

That is enough to note that a promotion happened, but not enough to drive the full Task 7/8 behavior on its own.

The current repo also has a `runtime_flags` table, but no helpers or queue-claim integration that let repair rollout monitoring pause fresh queued work. Worker status currently exposes whether workers and the repair supervisor are running, but not whether rollout monitoring has paused new claims.

## Options Considered

### 1. Keep All Task 7/8 Logic Inside `repair_supervisor.py`

Pros:

- smallest file count
- fastest path to an initial implementation

Cons:

- pushes repair generation, rollout persistence, cohort analytics, queue pausing, report writing, confirmation, and revert orchestration into one file
- makes later debugging harder because rollout semantics and repair semantics would be tightly coupled

Rejected.

### 2. Add A Small Rollout-Monitoring Helper And Keep `repair_supervisor.py` As The Orchestrator

Recommended.

Pros:

- preserves the current split where `repair_fingerprints.py` owns cluster reporting and `repair_git.py` owns git operations
- keeps rollout logic small, testable, and focused
- lets the supervisor own sequence and safety while another helper owns cohort analysis and markdown reporting

Cons:

- one more module to maintain
- requires careful boundaries so rollout metadata does not drift from supervisor behavior

### 3. Create A Separate Rollout Monitor Process

Pros:

- strongest runtime separation

Cons:

- introduces another singleton process, another lifecycle surface, and another coordination channel
- weakens the guarantee that monitoring runs immediately after promotion
- adds failure modes that are not required for the current repo

Rejected.

## Selected Design

### A. New `repair_rollouts.py` Helper Owns Rollout State

Add a new `scripts/repair_rollouts.py` module responsible for:

- recording rich rollout metadata into `repair_rollouts`
- computing comparable-cohort baseline and post-fix metrics
- selecting active rollouts that still need monitoring
- evaluating regression signals
- refreshing `output/_audit/active_repair_rollouts.md`
- reading and writing rollout pause state through `runtime_flags`

This helper does not run the supervisor loop itself. `repair_supervisor.py` remains the orchestrator and calls the helper before attempting any new repair.

### B. Rollout Metadata Lives In Existing JSON Fields

Do not widen the rollout schema for this pass.

The existing table already gives enough durable fields:

- `cluster_id`
- `commit_sha`
- `status`
- `baseline_metrics_json`
- `post_fix_metrics_json`
- `revert_sha`
- timestamps

Use the two JSON columns to hold the richer rollout context:

- board and phase derived from the cluster fingerprint
- rollout fingerprint
- monitored job ids
- touched files
- baseline counts and ratios
- post-fix counts and ratios
- threshold decisions
- confirmation attempt details
- pause reason and resume details

This keeps the implementation repo-native and durable without another migration.

### C. Queue Pause Uses `runtime_flags`

Introduce small `job_db` helpers for runtime flags and a dedicated pause flag such as `repair_pause_new_queued_work`.

The stored value should include:

- rollout id
- promoted commit sha
- cluster id and fingerprint
- reason the rollout was paused
- when the pause started

`get_pending_jobs()` should honor the pause flag by excluding only fresh queue work:

- block `queued`
- block `queued_submit`

Do not block:

- `approved`
- `submitting`
- `reanswering`
- `regenerating`

This matches the design requirement to pause new work while allowing already-running or already-approved flows to drain.

### D. Supervisor Poll Order

Each repair supervisor poll should run in this order:

1. monitor existing active rollouts
2. if a rollout is paused pending confirmation, run the confirmation pass before starting anything new
3. if the pause remains active, do not claim or attempt another repair
4. only when no rollout pause is active, continue into the normal open-cluster repair loop

This keeps rollout containment on the critical path. The runtime should never continue promoting fresh fixes while a prior rollout is paused on a suspected regression.

### E. Promotion Captures An Active Rollout Record

When a repair candidate passes canary, is pushed to `origin/main`, and the runtime repo is synced:

- compute baseline comparable-cohort metrics for the touched board/phase and fingerprint family
- requeue the non-canary affected jobs
- write an active rollout row with status `active`
- update `last_rollout_sha` on the monitored jobs
- refresh `output/_audit/active_repair_rollouts.md`

The rollout record should include enough context to monitor the rollout later without recomputing the original promotion intent from scratch.

### F. Comparable-Cohort Monitoring

The monitor should compare comparable jobs, not raw global queue totals.

The first-pass cohort should be scoped by:

- board
- phase implied by the repair fingerprint
- rows updated after the rollout timestamp

The primary regression signals are:

1. **Fingerprint recurrence**
   - the original repair fingerprint reappears on the same board/phase after promotion
2. **Rendered-state regression**
   - exact `rendered_audit_mismatch` or related audit-failure counts rise materially on the same surface
3. **Unexpected hard-failure rise**
   - new hard failures materially rise on the same board/phase after excluding truthful terminal/manual outcomes

Truthful terminal outcomes should not count as regressions by default, including:

- `unsupported`
- `external_apply`
- `pending_user_input`
- `skipped_captcha`
- `job_closed`
- `already_applied`
- `auth_failed`
- `auth_unknown`
- `auth_guarded`
- `user_stopped`
- `user_rejected`

### G. Pause-And-Confirm Flow

If the first monitoring pass trips a regression threshold:

1. set the runtime pause flag
2. update the rollout status to `paused_pending_confirmation`
3. write the paused state into `active_repair_rollouts.md`
4. stop fresh queued work from being claimed

The confirmation pass should not invent another rerun subsystem. It should re-check persisted comparable-cohort evidence after at least one more comparable sample appears.

If the confirmation pass clears the signal:

- clear the runtime pause flag
- mark the rollout `monitoring_resumed`
- update the rollout JSON with the cleared reason
- refresh the active rollout index

If the confirmation pass confirms regression:

- proceed to revert

### H. Revert Flow

Add a narrow `repair_git.revert_main()` helper that:

- creates a normal git revert commit for the promoted repair commit
- pushes the revert commit to `origin/main`
- returns the revert sha

On confirmed regression, the supervisor should:

1. create and push the revert commit
2. sync the runtime repo to the revert sha
3. requeue/redraft only the rollout's monitored cohort jobs
4. store the revert sha on the rollout row
5. clear the pause flag
6. mark the rollout `reverted`
7. refresh `active_repair_rollouts.md`

This must remain non-destructive:

- no `reset --hard`
- no branch rewrites
- no reverting unrelated history

### I. Repo-Local Rollout Reporting

Add `output/_audit/active_repair_rollouts.md`.

Each active rollout entry should include:

- rollout id
- promoted sha
- cluster id and fingerprint
- rollout status
- board/phase scope
- baseline summary
- latest post-fix summary
- paused or reverted reason, if any

If no active rollouts remain, the file should explicitly say so, mirroring the existing repair-failure index style.

### J. Worker Status Surfaces

Extend worker-status payloads so the repo exposes rollout pause state alongside `repair_supervisor_running`.

At minimum, add:

- whether fresh queued work is paused by rollout monitoring
- the paused rollout id or reason when available

This keeps CLI/web/TUI state truthful when the system has intentionally stopped claiming fresh queued jobs.

## File Boundaries

### New file

- `scripts/repair_rollouts.py`
  - rollout metadata persistence
  - comparable-cohort metric helpers
  - pause-flag helpers
  - active rollout markdown refresh

### Modified files

- `scripts/repair_supervisor.py`
  - monitor rollouts before new repair attempts
  - record rich rollout metadata on promotion
  - orchestrate pause, confirmation, revert, and resume
- `scripts/repair_git.py`
  - add revert helper for promoted repair commits
- `scripts/job_db.py`
  - runtime-flag helpers
  - queue pause integration in `get_pending_jobs()`
- `scripts/job_web.py`
  - extend worker-status payload with rollout pause state
- `tests/test_repair_supervisor.py`
  - rollout monitoring, confirmation, and revert coverage
- `tests/test_job_db.py`
  - pause flag helpers and queue gating coverage
- `tests/test_job_web.py`
  - worker-status and websocket rollout pause coverage

## Testing Strategy

Add failing tests first for:

- runtime flag helpers can set, read, and clear the rollout pause state
- `get_pending_jobs()` excludes `queued` and `queued_submit` while a rollout pause is active
- `get_pending_jobs()` still returns `approved`, `submitting`, `reanswering`, and `regenerating` jobs during that pause
- repair promotion records an active rollout with baseline metadata and refreshes the active rollout index
- a first regression signal pauses fresh queued work without reverting immediately
- a cleared confirmation removes the pause and resumes monitoring
- a confirmed regression creates a revert commit, syncs the runtime repo, requeues the monitored cohort, records `revert_sha`, and clears the pause
- worker-status and websocket payloads expose rollout pause state truthfully

Verification after implementation:

- focused pytest runs for the touched test modules during TDD
- `uv run python -m pytest tests/ -v`
- `uv run ruff check scripts/ tests/`
- `uv run python scripts/check_architecture.py`
- `uv run python scripts/sync_agent_files.py --check`
- `uv run python scripts/check_agent_docs.py`

## Implementation Notes

- Keep the rollout table shape unchanged for this pass unless implementation proves the JSON fields insufficient.
- Prefer helper functions in `job_db.py` for runtime flags instead of raw SQL scattered across modules.
- Keep rollback eligibility narrow and deterministic; noisy signals should pause first, not revert immediately.
- Treat the active rollout markdown as a first-class operational artifact, not a best-effort debug extra.
- Do not broaden the worker pause to all job states; the pause is specifically for fresh queued work.
