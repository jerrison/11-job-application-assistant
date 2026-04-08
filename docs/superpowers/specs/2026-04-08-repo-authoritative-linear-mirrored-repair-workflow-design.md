# Repo-Authoritative, Linear-Mirrored Repair Workflow Design

Date: 2026-04-08
Status: Draft for review
Related:

- [2026-03-31-draft-audit-and-stopped-job-repair-loop-design.md](./2026-03-31-draft-audit-and-stopped-job-repair-loop-design.md)
- [2026-04-01-draft-answer-verifier-design.md](./2026-04-01-draft-answer-verifier-design.md)
- [../../backlog-sweep.md](../../backlog-sweep.md)
- [../../runbooks/repeatable-backlog-sweep.md](../../runbooks/repeatable-backlog-sweep.md)
- [../../operational-rules.md](../../operational-rules.md)
- [../../worker-pipeline-patterns.md](../../worker-pipeline-patterns.md)

## Problem

The repo already enforces a lot of the right mechanics:

- `--draft` fails closed at submit boundaries
- screenshots are the source of truth
- stopped and draft sweeps use immutable snapshots and append-only ledgers
- current proof selection, stale-artifact cleanup, and rendered-state verification are increasingly repo-native
- verifier commands already block completion when snapshot coverage or proof is missing

The remaining failure mode is behavioral rather than purely technical:

- a Linear `Todo` issue can be "handled" by description, comments, or parking without a real repair attempt
- a stopped row can be classified and recorded without a current, concrete repair loop
- a drafted row can be reviewed and recorded without forcing the repair path for discovered defects
- once an item has been looked at once, later repo code changes do not mechanically force the same item back through another repair attempt
- Linear is useful as a planning and context surface, but it is not today the enforcement layer for proof, reruns, or current-wave attempt history

The user wants a stronger contract:

1. every Linear issue, stopped job, and drafted-job defect should keep being retried until it is proven external or user-blocked
2. later repo code changes must force another pass over unresolved blockers
3. Linear should stay truthful and useful, but durable enforcement should live in code instead of prompt wording

## Goals

- Make "handled" a repo-native state transition instead of free-form agent behavior.
- Keep the repo as the source of truth for machine-checkable state: attempts, proof, verifier status, repair waves, and job status.
- Keep Linear truthful and useful as the mirrored product-management and context surface.
- Extend snapshot-and-ledger discipline to Phase 1 Linear `Todo` issues, not just Phase 2 stopped jobs and Phase 3 draft reviews.
- Require at least one current-wave repair attempt for every unresolved repairable item unless the item is proven external or user-blocked.
- Force unresolved items back into scope after relevant repo code changes.
- Preserve existing draft safety, proof contracts, and rerun machinery instead of replacing them with a second ad-hoc system.

## Non-Goals

- Making Linear the sole source of truth for proof, reruns, or verifier state.
- Replacing the current draft/stopped audit helpers, retry logic, or proof-artifact selection from scratch.
- Automating genuinely external blockers such as captchas, sign-in walls, or explicit human-input requirements beyond the repo's current truthful capabilities.
- Building a new general-purpose project-management system outside the scope of backlog repair workflows.

## Principles

1. **Repo truth first.** If state can be validated from local files, the database, or deterministic code paths, it belongs in the repo-controlled workflow.
2. **Linear mirrors repo truth.** Linear should reflect the latest repo-known status, but repo-native transitions remain authoritative.
3. **Repair-first, not triage-first.** Description, commentary, and parking do not count as a fix attempt.
4. **Current-wave accountability.** A prior attempt only counts if it was made against the current repair-relevant code state.
5. **Unified control surface.** Phase 1, Phase 2, and Phase 3 should all flow through the same core state-machine concepts.

## Approaches Considered

### 1. Prompt Hardening Only

Keep the current repo behavior and rely on stronger prompt language.

Pros:

- lowest implementation cost
- no schema or workflow migration

Cons:

- still depends on agent discipline
- no machine-checkable revisit-after-code-change rule
- allows drift between repo state, proof artifacts, and Linear state

Rejected.

### 2. Verifier Hardening Without Wrapper Actions

Add current-wave checks and stricter verifier rules, but still allow operators or agents to perform free-form work and manually report progress.

Pros:

- better completion gate
- smaller change than a full wrapper model

Cons:

- "handled" still occurs outside the state machine
- hard to prove which action actually changed state
- direct Linear edits remain ambiguous

Better than prompt-only, but not strong enough.

### 3. Repo-Authoritative Workflow With Linear Mirroring And Wrapper Actions

Recommended.

Pros:

- makes handling a code-enforced transition
- preserves Linear as a truthful, visible control plane
- supports revisit-after-code-change mechanically
- fits the repo's existing emphasis on durable artifacts, proof, and verifier gates

Cons:

- broader than verifier-only hardening
- requires Phase 1 snapshots/ledgers and new controller actions
- needs a migration path from the current sweep contract

## Selected Design

### A. Control Model

Use a repo-authoritative workflow with Linear mirroring.

Authoritative local state owns:

- sweep snapshots
- sweep ledgers
- repair attempts
- blocker proof
- verifier status
- repair-wave fingerprint
- current job/draft proof state

Mirrored Linear state owns:

- human-readable titles and summaries
- labels such as `requires-user-input`
- user comments and product-management context
- current mirrored status derived from repo truth

Direct edits in Linear may still be useful for context, but they do not count as authoritative progress unless a repo-native action also records the corresponding local transition.

### B. Unified Repair Item Model

Every tracked item becomes the same conceptual entity regardless of source.

Core fields:

- `item_type`: `linear_issue`, `stopped_job`, `draft_review`
- `item_id`: stable local identifier
- `source_ref`: Linear issue ID or job ID
- `status`: `open`, `in_repair`, `blocked_user`, `blocked_external`, `fixed_pending_verification`, `verified`, `closed`
- `closure_reason`: `duplicate`, `not_a_bug`, or empty
- `repair_wave_fingerprint`
- `last_attempted_at_utc`
- `last_attempt_commit`
- `proof_status`: `missing`, `stale`, `current`
- `linear_sync_status`: `pending`, `synced`, `drifted`

Completion-relevant terminal states are:

- `verified`
- `blocked_user`
- `blocked_external`
- `closed` when `closure_reason` is explicitly `duplicate` or `not_a_bug`

Everything else is incomplete.

### C. Phase Snapshots And Ledgers

All three phases should use immutable snapshots and append-only result ledgers.

#### Phase 1

Add:

- `phase1-linear-snapshot-<run>.tsv`
- `phase1-linear-results-<run>.tsv`

Snapshot fields:

- `linear_issue_id`
- `title`
- `labels`
- `status`
- `related_job_id`
- `related_output_dir`
- `requires_user_input`
- `captured_at_utc`

Allowed outcomes:

- `fixed_verified`
- `blocked_user`
- `blocked_external`
- `duplicate_closed`
- `not_a_bug_closed`

#### Phase 2

Keep the current stopped-job snapshot and results ledger, but route all handling through the unified wrapper actions and current-wave verifier rules.

#### Phase 3

Keep the current draft snapshot and browser-backed review recording, but require defects found during review to enter the repair path before a row can reach `verified`.

### D. Wrapper Actions

Handling must go through repo-native commands or web actions.

First-class actions:

- `resume_or_start`
- `start_phase`
- `next_items`
- `attempt_repair`
- `record_user_blocker`
- `record_external_blocker`
- `verify_item`
- `sync_linear`
- `close_item`
- `verify_run`

Each wrapper action must:

1. load the authoritative local item state
2. validate allowed transition preconditions
3. write the transition, proof metadata, and attempt history locally
4. update or queue the matching Linear sync payload
5. return machine-readable output suitable for the verifier and web surfaces

Free-form actions that bypass this path do not count as state transitions.

### E. Repair-First Transition Rules

For any `linear_issue`, `stopped_job`, or `draft_review` item that is repo-side or ambiguous:

1. reproduce from current artifacts or a fresh rerun
2. identify the code or runtime path that must change
3. implement the smallest generalized fix
4. run targeted verification
5. rerun/redraft the canonical affected job
6. inspect fresh proof artifacts
7. only then transition toward `verified`

Creating or updating a Linear issue, leaving notes, or explaining the root cause does not satisfy this loop.

### F. Repair-Wave Fingerprint

The design must force unresolved items back through another attempt after later repo code changes.

Add a `repair_wave_fingerprint` derived from repair-relevant repo state only. The first version should hash:

- `scripts/**/*.py`
- `scripts/static/**` only where review/control surfaces depend on it
- `AGENTS.md`
- `docs/operational-rules.md`
- `docs/backlog-sweep.md`
- `docs/runbooks/repeatable-backlog-sweep.md`

Do not include generated artifacts, ledgers, screenshots, `output/**`, or `jobs.db`.

Each repair attempt stores the fingerprint it ran against.

Verifier rule:

- if an item is unresolved and its latest attempt fingerprint does not match the current fingerprint, the item is stale and must be retried in the current wave

This is how later code changes automatically pull old blockers back into scope.

### G. Verifier Rules

The sweep verifier should fail unless every snapshot row satisfies one of these:

1. **Verified**
   - has a current-wave repair attempt
   - has current proof
   - has successful verification status
   - has `linear_sync_status = synced`

2. **Blocked user**
   - has explicit blocker proof
   - has `linear_sync_status = synced`

3. **Blocked external**
   - has explicit blocker proof
   - has `linear_sync_status = synced`

4. **Closed non-actionable**
   - `status = closed`
   - `closure_reason` is `duplicate` or `not_a_bug`
   - has explicit supporting proof
   - has `linear_sync_status = synced`

The verifier must fail for rows that are only:

- described
- triaged
- commented on in Linear
- attempted in an older wave
- missing current proof
- missing successful Linear sync

### H. Phase-Specific Workflow Rules

#### Phase 1

Phase 1 should stop being a loose "check Linear and do work" pass.

Instead:

- snapshot current `Todo` issues
- handle each issue via wrapper actions
- require repair attempts for repo-side or ambiguous issues
- require blocker proof for `blocked_user` or `blocked_external`
- sync the resulting authoritative state back to Linear

#### Phase 2

For stopped rows:

- the current stopped snapshot remains the denominator
- `attempt_repair`, `record_user_blocker`, or `record_external_blocker` are the only valid handling paths
- an old handled row becomes stale when the repair-wave fingerprint changes

#### Phase 3

For draft reviews:

- browser review remains the proof capture surface
- if the review finds no defect, `verify_item` may move the row to `verified`
- if the review finds a defect, the row must enter `attempt_repair`
- after repair, redraft and re-review are mandatory before `verified`

### I. Linear Sync Contract

Linear should remain truthful, but not authoritative.

Recommended contract:

- local state changes create a deterministic Linear sync payload
- a sync backend applies that payload
- the local item records whether Linear is `pending`, `synced`, or `drifted`

Support two backends:

1. **Preferred backend: local Linear API client**
   - repo-owned script updates Linear directly when credentials are available

2. **Transitional backend: repo-owned sync queue plus agent bridge**
   - repo writes pending sync operations locally
   - the agent executes them through the available Linear integration
   - the same bridge also supports Phase 1 snapshot reads until a local Linear API client exists
   - final verification still requires the local state to show `synced`

Even in the transitional backend, repo state stays authoritative and Linear mirrors it.

### J. Initial File / Module Plan

The first implementation slice should add:

- Phase 1 snapshot and ledger templates
- a shared sweep controller module for item loading and state transitions
- repair-wave fingerprinting helpers
- per-item attempt logging for all three phases
- verifier extensions for current-wave and Linear-sync enforcement
- a minimal Linear sync abstraction

Likely files:

- `scripts/sweep_controller.py`
- `scripts/sweep_repair_wave.py`
- `scripts/sweep_linear_sync.py`
- `scripts/resume_or_start_backlog_sweep.py`
- `scripts/init_backlog_sweep.py` updates for Phase 1
- `scripts/check_backlog_sweep.py` and `scripts/verify_active_sweep.py` updates
- `docs/templates/phase1-linear-results-template.tsv`

The current `draft_web` browser review action should be kept, but routed through the shared controller instead of writing only Phase 3 ledger rows directly.

## Migration Plan

### Step 1

Add Phase 1 snapshots and ledgers plus the unified item model.

### Step 2

Add repair-wave fingerprinting and make stale unresolved items fail verification.

### Step 3

Wrap Phase 2 and Phase 3 handling behind the shared controller actions while preserving the current proof contract.

### Step 4

Add Linear sync status and require successful sync for final completion.

### Step 5

Move from the transitional sync backend to a fully local Linear API backend if desired.

## Risks And Tradeoffs

- The workflow becomes more explicit and therefore more demanding up front, but that cost buys reliability.
- A strict current-wave rule can increase repeat work if the fingerprint is too broad. The first fingerprint should stay narrow and repair-focused.
- Linear sync should not be allowed to silently fail; otherwise the system regresses into drift between repo truth and the product-management surface.
- The migration should preserve today's working proof and rerun logic. Replacing too much at once would risk losing good safety guarantees already present in the repo.

## Decision

Adopt a repo-authoritative, Linear-mirrored repair workflow where every Phase 1, Phase 2, and Phase 3 item is handled through repo-native wrapper actions, every unresolved item must have a current-wave repair attempt or blocker proof, and later repo code changes automatically force stale unresolved items back through another repair wave until they are proven external or user-blocked.
