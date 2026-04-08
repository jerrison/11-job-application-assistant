# Draft Audit And Stopped-Job Repair Loop Design

## Overview

The repo already knows how to generate drafts, classify many board failures, auto-retry transient issues, and run provider-backed `auto_fix()` for some submit failures. The gap is the post-run enforcement loop:

- fresh `draft` rows can still hide unaccounted-for optional fields or incomplete proof
- fresh `stopped` rows can land without a durable, repo-native repair policy
- the current retry counters mix transport/runtime retries with higher-level "this draft still is not good enough" failures

The new behavior should be repo-native and surface-agnostic. Whether a draft is launched from CLI, TUI, web, worker, or a local REPL entrypoint, the same runtime should:

1. audit the outcome using current repo-local artifacts
2. attempt a generalized repair/requeue cycle when the failure is repairable
3. stop after three audit repair cycles with an explicit audit failure
4. avoid retry churn for truthful terminal outcomes

## Approaches Considered

### 1. External Watcher Process

Run a separate daemon that polls `jobs.db`, inspects output artifacts, and requeues failed rows.

Pros:

- works across surfaces without touching the core pipeline

Cons:

- duplicates the state machine outside the orchestrator
- races with worker status transitions
- harder to test because the logic is split across two runtimes
- encourages artifact drift and shell-script behavior instead of one canonical policy

Rejected.

### 2. Worker-Only Audit Loop

Add a background audit thread to `job_worker.py` that continuously scans fresh `draft` and `stopped` rows.

Pros:

- repo-native
- catches rows that appear through worker activity

Cons:

- misses single-job CLI/TUI/web flows that do not depend on the worker loop
- still leaves outcome policy outside `process_job()`
- makes unit testing harder because enforcement is asynchronous

Useful as a secondary sweep, but not strong enough as the primary design.

### 3. Shared Audit Module + Orchestrator Enforcement + Worker Sweep

Create one shared post-run audit module. Call it directly from `process_job()` whenever a run would otherwise end as `draft` or `stopped`, and add a small worker sweep for rows that reach those states through sync/manual/external paths.

Pros:

- surface-agnostic because every primary entrypoint converges on `process_job()`
- repo-native and artifact-backed
- testable as pure functions plus a few integration hooks
- lets the worker recover stragglers without moving policy out of the orchestrator

Recommended.

## Design

### A. Shared Audit Module

Add a new shared module, tentatively `scripts/pipeline_audit_loop.py`, responsible for post-run classification and repair decisions.

Core responsibilities:

- inspect current-attempt proof, `pending_user_input`, `unknown_questions`, `application_submission_result.json`, and draft assets
- compute whether a row is:
  - `ready`
  - `terminal`
  - `repairable`
  - `audit_failed`
- return a normalized result with:
  - `kind`
  - `reason`
  - `failure_type`
  - `repair_actions`
  - `artifacts`
  - `attempts_used`

This module should stay board-agnostic by default and only delegate board-specific normalization where necessary.

### B. Field Accounting Contract

The draft audit should stop treating optional fields as "fine if blank." A field must be accounted for in exactly one explicit bucket:

- confirmed filled in the current attempt
- intentionally blocked in `pending_user_input`
- explicitly recorded in `unknown_questions`
- truthful terminal outcome from `application_submission_result.json`
- explicit review-only blocker backed by artifact evidence

If a field appears in the current payload/report surface and is in none of those buckets, the draft is incomplete even if the field was optional.

The audit should prefer shared artifact surfaces instead of board-specific DOM knowledge:

- current autofill report
- current payload
- current `unknown_questions`
- current `pending_user_input`
- current draft proof artifacts

Board-specific adapters should only exist where one board emits a structurally different field inventory.

### C. Repair Policy

The runtime should distinguish three categories:

1. **Truthful terminal**
   Examples: `external_apply`, `auth_failed`, `unsupported`, `pending_user_input`, `already_applied`, `job_closed`, explicit `no_apply_button`.

   Behavior:

   - do not retry
   - persist truthful state

2. **Transient/runtime retry**
   Existing behavior already handled by `_auto_retry_if_transient()` and LinkedIn targeted retry.

   Behavior:

   - keep the existing retry logic
   - do not count these against the new audit repair budget

3. **Audit-repairable**
   Examples:

   - missing current-attempt proof after a run that should have produced it
   - unresolved field accounting, including missed optionals
   - stale answer/proof artifacts causing a draft to fail the current contract
   - classified stopped outcomes where a generalized rerun is justified

   Behavior:

   - perform generalized repair prep
   - requeue
   - increment audit attempt counter
   - after three failed audit repair cycles, mark `stopped` with `failure_type = 'audit_failure'`

Generalized repair prep should be safe and cross-board:

- clear stale current-attempt proof/debug/result artifacts
- regenerate or invalidate draft summary / answer-refresh artifacts when the audit says the current run is incomplete
- preserve truthful payload/unknown-question artifacts that still describe the current job
- requeue through the normal worker/orchestrator path instead of inventing a custom rerun path

The audit loop should not blindly invoke provider code rewriting for every failure. Existing `auto_fix()` remains available for the submit failure path where it already applies. The new repair loop owns classification, generalized cleanup, and bounded requeue semantics.

### D. Human-Readable Exhaustion Reports

When a row exhausts all three audit repair cycles, the runtime should emit human-readable markdown, not just DB metadata.

Artifacts:

- per-job note in the active submit directory: `output/<company>/<role>/submit/audit_failure.md`
- repo-local rolling index for current exhausted audit failures: `output/_audit/active_audit_failures.md`

Each per-job note should include:

- final failure summary
- the three repair attempts that were tried
- concrete suggestions for the next code/manual action
- paths to current screenshot evidence and other key artifacts

The rolling index should link to the per-job notes and the main screenshot/report artifacts so the failure cluster is easy to review from one file.

### E. Attempt Accounting

Do not reuse `jobs.fix_attempts` for the audit loop.

`fix_attempts` already tracks transient/targeted retry behavior. Mixing that counter with audit repair cycles would make "network hiccup" and "draft still missing optional answers" look like the same class of failure.

Add separate audit attempt tracking under `job_metrics`, tentatively:

- `audit_attempts INTEGER DEFAULT 0`
- `audit_failure_count INTEGER DEFAULT 0`

Reset `audit_attempts` when a row reaches a truthful good terminal state:

- `draft`
- `submitted`
- archived terminal states such as `job_closed`

Keep detailed per-attempt evidence in `events`, including:

- `draft_audit_failed`
- `stopped_audit_classified`
- `audit_retry_scheduled`
- `audit_retry_exhausted`

### F. Orchestrator Hooks

Primary enforcement should live in `scripts/pipeline_orchestrator.py`.

Hook points:

1. Right before a row would be marked `draft`
2. Right before a row would be left `stopped` for incomplete draft proof
3. After submit failure classification when the result is not already a truthful terminal outcome
4. After successful reruns that currently bypass the main draft proof path via auto-fix recovery

At each hook, call the shared audit module and let it decide whether to:

- accept the outcome
- convert the outcome into an audit-driven requeue
- persist `audit_failure`

### G. Worker Sweep

Add a lightweight sweep in `scripts/job_worker.py` for rows that became fresh `draft` or `stopped` outside the primary orchestrator path, such as:

- disk sync updates
- manual status changes
- older rows promoted back to `draft` from proof reconciliation

The sweep should be narrow:

- recent rows only
- skip rows already carrying a final audit decision
- reuse the same shared audit module

This is a repair net, not a second state machine.

## Data Flow

### Draft Flow

`process_job()` draft success candidate
-> shared draft-proof sync
-> shared audit loop
-> either:

- `draft` accepted
- `queued` with `audit_retry_scheduled`
- `stopped` with `audit_failure`

### Stopped Flow

classified stopped result
-> shared audit loop
-> either:

- truthful terminal `stopped`
- `queued` with audit repair attempt
- `stopped` with `audit_failure`

## Testing

Add focused tests for:

- optional field left unaccounted-for causes audit retry instead of silent `draft`
- three audit repair cycles exhaust into `failure_type = 'audit_failure'`
- transient retry counters and audit counters remain separate
- truthful terminal outcomes bypass audit retry churn
- worker sweep only touches rows without a final audit decision
- board-agnostic repair prep clears stale proof while preserving truthful payload/unknown artifacts
- exhausted audit failures write per-job markdown plus a rolling markdown index with screenshot/report paths

## Rollout

1. Land the audit module, metrics updates, and orchestrator hooks.
2. Add the worker sweep for non-orchestrated rows.
3. Requeue active non-submitted, non-archived draft candidates and let the new loop enforce the contract.
4. Monitor fresh `audit_failure` rows as the new canonical cluster for issues that still need real code fixes.
