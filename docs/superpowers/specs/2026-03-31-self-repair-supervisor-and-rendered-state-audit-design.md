# Self-Repair Supervisor And Exact-Match Rendered-State Audit

Date: 2026-03-31
Status: Approved for spec review
Related:

- [2026-03-31-draft-audit-and-stopped-job-repair-loop-design.md](./2026-03-31-draft-audit-and-stopped-job-repair-loop-design.md)
- [worker-pipeline-patterns.md](../../worker-pipeline-patterns.md)

## Problem

The repo now has a repo-native draft/stopped audit loop, bounded requeue behavior, and human-readable audit failures. That closes the "silent bad draft" gap, but two major gaps remain:

1. a draft can still look successful while rendered option fields are wrong on the live page
2. when a new board/runtime bug appears, the repo can classify and retry it, but it cannot yet invent, verify, ship, and monitor a generalized code fix by itself

The user wants the runtime to go further:

- deterministic option fields must be double-checked against the rendered page state, not trusted from payload/report output alone
- multi-selects must satisfy exact selected-set and exact cardinality checks
- new repairable failure clusters should trigger bounded autonomous code repair
- successful autonomous fixes should be committed, pushed to `origin/main`, and followed immediately by redrafting the affected jobs
- if the rollout appears to regress the broader queue, the system should pause new work, verify once more, and only then auto-revert if the regression is confirmed

## Relationship To The Existing Audit-Loop Design

This design extends, not replaces, the earlier draft-audit / stopped-job repair-loop design.

That earlier design remains responsible for:

- post-run classification (`ready`, `repairable`, `terminal`, `audit_failure`)
- bounded audit retry counters
- per-job audit failure notes
- worker sweep for rows that reached `draft` / `stopped` outside the main orchestrator path

This design adds four new layers on top:

1. exact-match rendered-state verification for deterministic fields
2. failure fingerprinting and clustering
3. a bounded self-repair supervisor that can patch and ship generalized fixes
4. rollout monitoring with pause-and-confirm rollback policy

## Goals

- Keep all behavior surface-agnostic across CLI, TUI, web, worker, and local REPL-driven runs.
- Treat the rendered page state as the acceptance boundary for deterministic option fields.
- Enforce exact selected-option matching by default for deterministic option questions.
- Allow autonomous code repair only for bounded, evidence-backed, repairable failure clusters.
- Require test-first repair, targeted verification, and live canary reruns before shipping any autonomous fix.
- Auto-commit and auto-push successful autonomous fixes to `origin/main`.
- Immediately redraft affected jobs after a successful repair rollout.
- Detect rollout regressions using comparable cohorts rather than raw queue totals.
- Pause new work and confirm once more before any automatic revert.

## Non-Goals

- General unrestricted self-modification across the entire repo.
- Auto-solving captcha, auth, unsupported boards, or other truthful manual states.
- Replacing screenshot review with DOM-only auditing.
- Forcing exact literal text matching for every free-text field on every board.
- Shipping speculative fixes without failing tests, canaries, and rollout monitoring.

## Approaches Considered

### 1. Keep The Existing Audit Loop And Leave New Bugs To Humans

Pros:

- simplest operational model
- lowest automation risk

Cons:

- still requires a human to notice and patch every new board failure pattern
- does not satisfy the goal of autonomous generalized repair
- keeps the repo reactive rather than self-healing

Rejected.

### 2. Unrestricted Self-Modifying Agent With Full Repo Access

Pros:

- maximum autonomy

Cons:

- too dangerous for a live draft pipeline
- hard to reason about blast radius
- too easy to ship broad regressions to `origin/main`
- not aligned with the repo's safety-first draft boundary

Rejected.

### 3. Bounded Repair Supervisor + Exact-Match Draft Audit + Rollout Guard

Recommended.

Pros:

- preserves repo-native audit and retry semantics
- constrains autonomous repair to evidence-backed failure clusters
- keeps screenshots as the source of truth
- gives the system a path to self-heal without giving it unlimited freedom
- provides an explicit rollback story once fixes are auto-pushed to `origin/main`

Cons:

- adds new orchestration state, rollout records, and repair metadata
- requires careful verification policy to avoid slow repair loops

## Selected Design

### A. Exact-Match Rendered-State Audit

The post-draft audit will gain a new rendered-state phase. A job may only remain `draft` if both checks pass:

1. existing artifact / field-accounting audit
2. new rendered-state exact-match audit

The new audit should build two structures:

- **expected manifest**
  - derived from the current payload, autofill plan, and board-specific structured field output
- **observed manifest**
  - derived from current-attempt review artifacts, with screenshots as the source of truth and DOM/report extraction as supporting evidence

For deterministic option fields, the default rule is **exact match**:

- planned `A, B, C`
- observed must be exactly `A, B, C`
- missing one fails
- extra one also fails

This applies to:

- radio groups
- dropdowns
- checkbox groups
- multi-select chip/tag selectors
- boolean yes/no questions

For multi-selects, exact cardinality is mandatory:

- if the plan says choose 3, exactly 3 rendered selections must be present
- if only 2 are rendered, fail
- if 4 are rendered, fail

Label matching should normalize common deterministic equivalents such as:

- `US` <-> `United States`
- `N/A` <-> `Not Applicable`

But after normalization, the selected set must still match exactly.

For free-text questions, this design keeps a narrower requirement:

- deterministic profile-backed text should compare against normalized planned value when safe
- non-deterministic generated text continues to rely on the existing accounting + proof contract unless a board-specific adapter can safely extract exact rendered value

If the rendered-state audit fails, the job is not allowed to remain `draft`. It enters the bounded repair cycle just like other repairable audit failures.

### B. Screenshot-First Evidence Policy

Screenshots remain the source of truth.

Observed-state extraction should use this precedence:

1. current-attempt screenshot / review screenshot
2. current-attempt DOM snapshot or structured submit artifact
3. autofill report JSON / markdown as supporting context only

The audit must never pass a deterministic option field solely because a report says it was filled if the rendered evidence does not confirm the exact final selected set.

When possible, each mismatch should be logged with:

- field label
- expected selected values
- observed selected values
- cardinality mismatch, if any
- screenshot path

### C. Failure Fingerprinting And Clustering

Every repairable stop or rendered-state audit failure should emit a normalized fingerprint. The fingerprint should capture the failure family, not just the raw traceback.

Fingerprint inputs should include:

- board
- phase (`generating`, `autofilling`, `draft_audit`, `auth_recovery`, etc.)
- normalized failure type
- exception class or failure code
- stable message signature
- field labels / selector family / auth scope where relevant

The runtime should group jobs into repair clusters when multiple jobs share the same fingerprint or when one high-confidence deterministic bug presents a well-scoped repair target.

This turns:

- one-off noisy stop rows into isolated cases
- repeated board bugs into explicit repair candidates

### D. Repair Eligibility Gate

Not every failure may trigger autonomous code editing.

Eligible classes:

- stale/hidden/duplicate selector bugs
- deterministic option-mapping bugs
- deterministic work-authorization / compliance classification misses
- exact-match audit failures caused by board adapter or extraction issues
- stale artifact selection / current-attempt resolution bugs

Ineligible classes:

- `auth_failed`
- `auth_guarded`
- `pending_user_input`
- `unsupported`
- `external_apply`
- captcha/manual intervention
- rate limiting / service interruption
- unsupported board discovery

Ineligible failures continue to use the existing truthful terminal or transient retry paths.

### E. Self-Repair Supervisor

Add a new repo-native supervisor, tentatively `scripts/repair_supervisor.py`.

Responsibilities:

- watch new repair clusters
- decide whether a cluster is eligible for autonomous repair
- create an isolated git worktree / branch for the repair attempt
- assemble a repair packet:
  - representative job ids
  - fingerprint summary
  - relevant artifacts
  - likely touched files
  - allowed verification commands
- run a bounded repair agent loop

The repair supervisor should use a dedicated repair-model configuration instead
of inheriting the general asset-generation provider defaults.

Default repair-model policy:

- provider: `openai`
- model: `gpt-5.4`
- reasoning effort: `xhigh`

This should be explicit in the supervisor's runtime configuration so repair
behavior does not drift when answer-generation or draft-generation providers
change for other parts of the pipeline.

The repair loop must be strict:

1. reproduce or restate the failure from artifacts
2. add or extend a failing regression test first
3. patch the minimal generalized fix
4. run targeted verification
5. run live canary reruns on affected jobs
6. if successful, commit and push to `origin/main`
7. immediately requeue/redraft affected jobs

Each repair cluster gets at most three autonomous repair attempts. After the third failed repair cycle:

- keep affected jobs in `stopped` with `failure_type = audit_failure` or the truthful terminal state
- write human-readable markdown describing what failed, attempted fixes, suggestions, and screenshot evidence

### F. Verification Policy For Autonomous Repairs

Autonomous repair may push to `origin/main` only after all required checks pass.

Required checks:

- relevant failing regression test now passes
- existing nearby board/component tests pass
- `ruff` passes on touched files
- any required invariant checks for touched shared modules pass

Suggested verification tiers:

- board-local patch:
  - targeted board tests
  - `ruff`
- shared pipeline / orchestrator / audit patch:
  - targeted tests
  - `ruff`
  - architecture/doc health checks where affected

After code verification, live canaries are mandatory:

- rerun 1-3 representative jobs from the repair cluster
- the original fingerprint must not recur
- the jobs must either:
  - reach `draft`, or
  - stop in a truthful non-bug terminal state

If tests or canaries fail, the repair is not pushed.

### G. Auto-Commit, Auto-Push, And Auto-Redraft

When a repair passes verification:

1. commit in the isolated repair branch
2. push directly to `origin/main`
3. fast-forward local `main` to the pushed commit
4. requeue/redraft the affected jobs immediately

This keeps the repo's live queue and `origin/main` aligned instead of leaving local-only repairs stranded.

### H. Rollout Monitoring

Every successful autonomous repair push creates a rollout record.

Each rollout record should include:

- commit SHA
- timestamp
- repaired fingerprint / cluster id
- touched boards/phases
- touched files
- baseline cohort metrics

The rollout monitor should compare **comparable jobs**, not raw queue totals.

Primary regression signals:

- the original fingerprint reappears in comparable jobs
- unexpected hard-failure rate rises materially on the touched board/phase
- exact-match draft-audit failure rate rises materially
- a new fingerprint cluster appears immediately after the fix on the touched surface

Truthful terminal outcomes such as `unsupported`, `external_apply`, and user-driven captcha waits should not count as regression by default.

### I. Pause-And-Confirm Rollback Policy

If rollout thresholds trip:

1. pause new work
   - stop claiming fresh queued jobs
   - let already-running jobs finish
2. run one confirmation check on the suspected regression
   - re-check the failing cohort and the triggering fingerprint
3. if regression is confirmed:
   - auto-revert the repair commit on `origin/main`
   - sync local `main`
   - requeue affected jobs on the reverted code
4. if regression is not confirmed:
   - resume new work

This avoids flapping `main` on noisy samples while still limiting the blast radius of a bad autonomous repair.

### J. Reporting

Autonomous repair needs repo-local human-readable reporting, not just DB events.

Add:

- per-cluster repair note:
  - `output/_audit/repair_clusters/<fingerprint>.md`
- active repair failure index:
  - `output/_audit/active_repair_failures.md`
- active rollout index:
  - `output/_audit/active_repair_rollouts.md`

Each repair-cluster note should include:

- what failed
- affected jobs
- repair attempts taken
- tests added
- commit SHA, if any
- rollout state
- suggestions
- screenshot/report artifact links

### K. Data Model

Extend the existing `events` + `job_metrics` approach with explicit repair metadata.

At minimum:

- `job_metrics`
  - `rendered_audit_failures INTEGER DEFAULT 0`
  - `last_repair_cluster_id TEXT`
  - `last_rollout_sha TEXT`
- new repair cluster table
  - fingerprint
  - status
  - attempt_count
  - representative jobs
  - latest summary
- new rollout table
  - commit SHA
  - cluster id
  - status
  - baseline metrics
  - post-fix metrics
  - revert SHA, if any

If a lighter first pass is preferred, cluster and rollout state may begin in JSON files plus `events`, but the intended stable direction is durable DB-backed records.

## Testing Strategy

Add focused tests for:

- deterministic checkbox group audit fails when a selected option is missing
- deterministic checkbox group audit fails when an extra option is selected
- multi-select exact cardinality mismatches fail even when all intended options are present
- rendered-state audit prevents a would-be `draft` from staying `draft`
- repair-eligible clusters trigger bounded autonomous repair attempts
- repair supervisor requires failing test before patch acceptance
- successful repair pushes to `origin/main` and requeues affected jobs
- rollout monitor pauses new work when regression thresholds trip
- pause-and-confirm path only auto-reverts after confirmation
- truthful terminal states never enter autonomous code-edit repair

## Rollout

1. Extend the existing draft audit with exact-match rendered-state checks.
2. Add failure fingerprinting and cluster creation.
3. Land the repair supervisor behind a feature flag.
4. Enable autonomous repair only for one or two eligible bug classes first.
5. Enable auto-push + rollout monitoring.
6. Expand the eligible repair classes once the pause-and-confirm rollback path proves stable.
