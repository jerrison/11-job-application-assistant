---
title: "fix: Persist LinkedIn Easy Apply failure evidence"
type: fix
status: completed
date: 2026-03-27
origin: docs/brainstorms/2026-03-25-pipeline-resilience-requirements.md
---

# fix: Persist LinkedIn Easy Apply failure evidence

## Overview

Implement the first focused slice of pipeline resilience by making LinkedIn Easy Apply failures durable, current-attempt-scoped, and actionable. The live audit found LinkedIn is the largest active stopped-job cluster (`30` active stopped rows) and also the weakest evidence path: `30/30` stopped LinkedIn reports had `fields = []`, `28/30` referenced missing pre-submit screenshots, and the queue mostly collapsed those runs into generic timeout or retries-exhausted states (see origin: `docs/brainstorms/2026-03-25-pipeline-resilience-requirements.md`).

This plan keeps scope narrow:

- persist classified LinkedIn failure evidence in-repo
- allow one targeted retry for retryable LinkedIn failure classes
- stop collapsing LinkedIn answer-refresh failures into generic messages

It does **not** broaden into Workday, Greenhouse, provider-chain, or general stopped-job UI work.

## Problem Frame

LinkedIn's runtime currently has an asymmetric failure contract:

- successful or review-ready flows produce the expected current-attempt payload/report artifacts
- two benign non-submit outcomes (`already_applied`, `not_easy_apply`) write `application_submission_result.json`
- most real Easy Apply failures only log to stderr and return exit code `1`

That leaves the rest of the pipeline guessing. `scripts/pipeline_orchestrator.py` only special-cases unsupported-board and auth artifacts before falling into generic retry/timeout handling, so classified LinkedIn failures become `Submit timed out`, `All submission attempts failed`, or `retries_exhausted`. The repo then loses the last observed step, the concrete failure class, and the screenshot path needed to debug the dominant stopped cluster.

The requirements already lock the intended product behavior:

- LinkedIn is the first implementation slice
- LinkedIn failures must emit durable board-local evidence
- once LinkedIn emits a concrete retryable failure class, the queue gets one targeted retry, then stops with the saved evidence

## Requirements Trace

- **R7:** A failed LinkedIn run writes a structured failure artifact with last observed step, concrete failure class, and a screenshot path that exists.
- **R7:** Classified retryable LinkedIn failures get one targeted retry, then stop with durable evidence.
- **R7:** Review surfaces and `answer_refresh_status.json` stop collapsing these cases into generic `retries_exhausted` / `submit_timeout`.
- **Success criteria:** New LinkedIn stopped runs no longer end with `linkedin_autofill_report.json` containing `fields = []` plus a missing referenced pre-submit screenshot.

## Scope Boundaries

- **In scope:** LinkedIn runtime failure classification, current-attempt artifact cleanup, durable failure result persistence, orchestrator targeted retry behavior, DB sync of classified failures, and repo docs describing the new artifact contract.
- **In scope:** preserving current-attempt proof semantics so failed LinkedIn reruns do not leave stale review artifacts behind.
- **Out of scope:** Workday and Greenhouse fixes, provider fallback/quota handling, new board support, and general stopped-job dashboard redesign.
- **Out of scope:** changing the already-completed LinkedIn fresh-resume-upload contract except where its existing `upload_verification_failed` outcome must participate in the new failure-artifact model.

## Context & Research

### Relevant Code and Patterns

- `scripts/autofill_linkedin.py`
  - `_build_payload()` already publishes the standard report/debug artifact paths, including `submit_debug_screenshot`.
  - `_wizard_flow()` currently returns `1` on key failures such as missing modal, missing navigation buttons, or runaway validation loops without writing a canonical failure artifact.
  - `_ResumeUploadVerificationError` is the one existing LinkedIn-specific failure path that already writes a report before returning `1`.
  - Existing resume outcome helpers (`_resume_outcomes`) prove LinkedIn already uses explicit board-local outcome modeling when needed.
- `scripts/autofill_common.py::write_report()`
  - the shared report writer expects a `pre_submit_screenshot` artifact path and is best suited for review-stage artifacts, not early LinkedIn failures that never reached review.
- `scripts/pipeline_orchestrator.py`
  - only special-cases unsupported boards, Workday auth artifacts, and generic auth artifacts before moving into generic submit failure / timeout / auto-retry handling.
  - `_retry_classification()` currently supports only permanent, transient, and unknown paths.
  - answer-refresh failure messages are currently synthesized from generic submit outcomes, so LinkedIn loses its concrete board-local cause.
- `scripts/job_db.py`
  - `_SUBMISSION_STATUS_MAP` already treats generic `application_submission_result.json` statuses as the authoritative disk-to-DB state source.
  - `sync_job_from_disk()` already reads `application_submission_result.json`, but it does not currently preserve a generic `failure_type` from that file outside the existing auth-specific artifacts.
- `scripts/submit_review_common.py`
  - `resolve_current_submit_artifacts()` already resolves `submit_debug_screenshot` without requiring a report or pre-submit screenshot.
  - This means we can reuse existing proof-artifact plumbing rather than inventing a new review-surface API.
- `docs/plans/2026-03-25-005-fix-linkedin-fresh-resume-upload-plan.md`
  - already established the LinkedIn pattern of keeping LinkedIn-specific state transitions explicit and artifact-backed rather than scattering DOM heuristics.

### Institutional Learnings

- `docs/solutions/workflow-issues/stopped-job-audits-must-be-repo-local-and-artifact-backed-2026-03-27.md`
  - the stopped-job investigation must remain repo-local and artifact-backed; external notes are mirrors only.
- `docs/solutions/workflow-issues/explicit-answer-regeneration-requires-durable-fresh-proof-2026-03-26.md`
  - request-scoped workflows must fail from durable artifacts, not from inferred status transitions.
- `docs/solutions/integration-issues/adding-new-llm-provider.md`
  - when multiple layers consume status/config, keep a clear single source of truth and make every layer explicitly honor it.
- `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`
  - centralize brittle branching decisions behind one explicit, test-covered path instead of scattering heuristics.

### External Research

- None. The repo already has the relevant Playwright, proof-artifact, retry, and current-attempt patterns, and this work is about aligning internal seams rather than adopting external framework guidance.

## Key Technical Decisions

### D1. Use `application_submission_result.json` as the canonical LinkedIn failure envelope

Do **not** invent a separate `linkedin_failure.json`.

Rationale:

- `application_submission_result.json` is already the authoritative cross-board outcome artifact.
- `job_db.py` already uses it during disk sync.
- LinkedIn already writes it for `already_applied` and `not_easy_apply`.
- Extending the existing result envelope is less surprising than adding a second LinkedIn-only result file.

Planned LinkedIn failure payload fields:

- `status: "failed"`
- `board: "linkedin"`
- `failure_type`: one explicit LinkedIn failure taxonomy value
- `message`: human-readable board-local summary
- `retry_class`: `targeted_retry` or `none`
- `step_num`: last observed wizard step when known
- `artifacts.submit_debug_screenshot`
- `artifacts.step_screenshot` when a step-scoped image exists
- `updated_at_utc`

### D2. Keep `linkedin_autofill_report.*` as review-stage artifacts, not early-failure truth

If LinkedIn fails before review, do **not** leave behind a fresh report that points to a missing `pre_submit_screenshot`.

Instead:

- clear stale LinkedIn review artifacts at the start of the current attempt
- write a canonical `application_submission_result.json` plus `submit_debug_screenshot` on early failure
- only write `linkedin_autofill_report.*` once the runtime reaches review or the existing resume-upload verification path that already produces a meaningful report

Rationale:

- this is the cleanest way to satisfy the audit success criterion without weakening the semantics of `pre_submit_screenshot`
- it keeps the shared `write_report()` contract review-focused

### D3. Reuse the existing `submit_debug_screenshot` artifact key as the canonical failure screenshot

Do not overload `pre_submit_screenshot` for failures that never reached review.

Rationale:

- `submit_review_common.py`, `job_web.py`, `job_tui.py`, and `scripts/static/app.js` already know how to resolve and surface `submit_debug_screenshot`
- this avoids new UI/plumbing work for the first slice

### D4. Introduce an explicit LinkedIn failure taxonomy

Use DB-facing `failure_type` values that are specific enough to drive targeted retry and human diagnosis:

- `linkedin_modal_missing`
- `linkedin_validation_loop`
- `linkedin_navigation_missing`
- `linkedin_timeout_after_partial_fill`
- `linkedin_resume_upload_verification_failed`

`already_applied` and `not_easy_apply` remain current result statuses, not stopped-failure taxonomy entries.

### D5. Only some LinkedIn failure classes get the one targeted retry

Targeted-retry set:

- `linkedin_modal_missing`
- `linkedin_validation_loop`
- `linkedin_navigation_missing`
- `linkedin_timeout_after_partial_fill`

No targeted retry for:

- `linkedin_resume_upload_verification_failed`

Rationale:

- navigation/modal/loop failures are plausibly flaky UI states
- resume-upload verification failure is a visible truth-contract miss and should fail closed rather than blindly churn

### D6. Preserve current-attempt truth by clearing stale LinkedIn review artifacts at run start

Clear only current-attempt LinkedIn-generated artifacts that can lie after a failed rerun:

- `linkedin_autofill_report.json`
- `linkedin_autofill_report.md`
- `linkedin_autofill_pre_submit.png`
- `linkedin_autofill_post_submit.png`
- `linkedin_submit_debug.png`
- existing `page_screenshots_dir` contents

Do not clear:

- payload JSON needed for the active attempt
- prior `submit-*` directories from earlier explicit reapply attempts

### D7. Stop generic auto-fix / retries once a classified LinkedIn failure artifact exists

Once the orchestrator sees a classified LinkedIn failure result:

- skip the generic auto-fix phase for that path
- route retry behavior through the explicit targeted-retry classification
- set `error_message`, `failure_type`, and answer-refresh failure state from the classified artifact rather than from a generic exit-code message

## High-Level Technical Design

> This is directional guidance for review, not implementation code.

```text
LinkedIn runtime start
  -> clear current-attempt LinkedIn review/debug artifacts
  -> proceed through Easy Apply wizard

on retryable LinkedIn failure before review
  -> capture canonical submit_debug_screenshot
  -> capture step-scoped screenshot when available
  -> write application_submission_result.json(status=failed, failure_type=linkedin_*)
  -> return non-zero

on review reached
  -> capture pre_submit_screenshot
  -> write linkedin_autofill_report.*
  -> continue draft/submit flow as today

orchestrator on LinkedIn failure result
  -> load application_submission_result.json from current submit attempt
  -> if failure_type in targeted-retry set and fix_attempts < 1:
       requeue once with targeted retry messaging
     else:
       stop with classified failure_type + error_message
       fail answer_refresh_status.json with the classified reason/message

orchestrator on LinkedIn subprocess timeout
  -> if current-attempt page/debug screenshots exist:
       synthesize linkedin_timeout_after_partial_fill result
       route through targeted retry path
     else:
       keep generic submit timeout behavior

disk sync
  -> map application_submission_result.status=failed to stopped
  -> persist failure_type/message from result file when present
```

## Implementation Units

- [x] **Unit 1: Write current-attempt LinkedIn failure results instead of silently returning `1`**

**Goal:** Every non-review LinkedIn failure leaves behind a current-attempt `application_submission_result.json` plus a canonical debug screenshot path that exists.

**Requirements:** R7 failure artifact, repo-local evidence success criterion

**Execution posture:** characterization-first

**Files:**
- Modify: `scripts/autofill_linkedin.py`
- Test: `tests/test_autofill_linkedin.py`

**Approach:**
- Add a LinkedIn helper that:
  - clears current-attempt LinkedIn review/debug artifacts at run start
  - writes classified failure results to `submit/application_submission_result.json`
  - captures `artifacts["submit_debug_screenshot"]`
  - optionally records the last step screenshot path under `artifacts.step_screenshot`
- Replace early failure returns in `_wizard_flow()` with classified result writes for:
  - modal not visible
  - missing next/submit navigation
  - repeated validation-loop exhaustion
  - resume upload verification failure
- Keep `already_applied` and `not_easy_apply` on the existing result-writing path.

**Patterns to follow:**
- existing LinkedIn result-writing helpers in `scripts/autofill_linkedin.py`
- current-attempt artifact semantics in `scripts/submit_review_common.py`
- explicit outcome modeling from the completed LinkedIn resume-upload plan

**Test scenarios:**
- early modal failure writes `application_submission_result.json` with `status = failed`, `failure_type = linkedin_modal_missing`, and an existing `submit_debug_screenshot`
- repeated validation errors stop with `linkedin_validation_loop` and include visible validation text
- missing Next/Submit CTA writes `linkedin_navigation_missing`
- resume-upload verification failure continues to write its review data but also writes a classified failure result
- stale report/pre-submit artifacts from a previous run are removed before a new failed run starts

**Verification outcome:**
- a LinkedIn failure can be diagnosed from disk without reopening worker logs
- failed runs no longer leave a fresh report that references a missing pre-submit screenshot

- [x] **Unit 2: Route classified LinkedIn failures through one targeted retry, then stop cleanly**

**Goal:** The orchestrator consumes LinkedIn's classified failure result, allows one targeted retry for retryable classes, and then stops with the concrete failure instead of generic retry exhaustion.

**Requirements:** R7 targeted retry, R7 non-generic answer-refresh failure messaging

**Execution posture:** characterization-first

**Files:**
- Modify: `scripts/pipeline_orchestrator.py`
- Test: `tests/test_pipeline_orchestrator.py`

**Approach:**
- Add a helper to load the current-attempt `application_submission_result.json` before the generic auto-fix/retry path.
- Introduce a targeted-retry classification tier for the retryable LinkedIn failure taxonomy.
- Skip generic auto-fix when a classified LinkedIn failure artifact exists.
- On `TimeoutExpired`, inspect the current submit dir for LinkedIn page/debug screenshots and synthesize `linkedin_timeout_after_partial_fill` when the timeout happened after partial progress.
- Feed the classified failure message into:
  - job `error_message`
  - job `failure_type`
  - `answer_refresh_status.json` failure reason/message

**Patterns to follow:**
- existing special-case artifact loading for Workday auth in `scripts/pipeline_orchestrator.py`
- current answer-refresh failure semantics from `docs/solutions/workflow-issues/explicit-answer-regeneration-requires-durable-fresh-proof-2026-03-26.md`

**Test scenarios:**
- first `linkedin_modal_missing` result requeues once with targeted-retry messaging
- second `linkedin_modal_missing` stop sets classified `failure_type` instead of `retries_exhausted`
- `linkedin_resume_upload_verification_failed` stops immediately without targeted retry
- LinkedIn subprocess timeout with existing page/debug screenshots synthesizes `linkedin_timeout_after_partial_fill`
- answer-refresh failures caused by LinkedIn classification preserve the specific failure reason instead of `submit_timeout` or generic retries exhaustion

**Verification outcome:**
- LinkedIn stopped rows no longer collapse into generic timeout/retries-exhausted messages when a classified artifact exists

- [x] **Unit 3: Preserve LinkedIn classification in disk sync and repo documentation**

**Goal:** Repo-local consumers and future sessions can recover the same LinkedIn failure classification from disk without depending on worker-local state.

**Requirements:** repo self-containment, R7 durable evidence

**Files:**
- Modify: `scripts/job_db.py`
- Modify: `tests/test_job_db.py`
- Modify: `tests/test_submit_application.py`
- Modify: `docs/output-structure.md`
- Modify: `docs/autofill-patterns.md`

**Approach:**
- Extend `sync_job_from_disk()` to preserve generic `failure_type` and `message` from `application_submission_result.json` when `status = failed`.
- Add one characterization test showing `resolve_current_submit_artifacts()` still surfaces `submit_debug_screenshot` correctly even when no report/pre-submit screenshot exists for the current attempt.
- Document the LinkedIn failure-artifact contract:
  - review artifacts are optional on early failure
  - `application_submission_result.json` plus `submit_debug_screenshot` become the canonical failure pair
  - `pre_submit_screenshot` remains review-only

**Patterns to follow:**
- `_SUBMISSION_STATUS_MAP` and `sync_job_from_disk()` in `scripts/job_db.py`
- artifact-resolution tests already present in `tests/test_submit_application.py`
- repo-local audit/documentation rule from `docs/solutions/workflow-issues/stopped-job-audits-must-be-repo-local-and-artifact-backed-2026-03-27.md`

**Test scenarios:**
- disk sync pulls `failure_type = linkedin_modal_missing` and the human-readable message from `application_submission_result.json`
- current-attempt artifact resolution succeeds with only `submit_debug_screenshot` present
- docs explicitly describe why a failed LinkedIn attempt may have debug artifacts but no review report

**Verification outcome:**
- repo surfaces remain self-contained even after worker restarts or external note drift

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Clearing LinkedIn artifacts too aggressively removes useful current-attempt evidence | Medium | Medium | Only clear LinkedIn-generated review/debug outputs at attempt start; do not touch payload JSON or older `submit-*` dirs |
| Classified failure types accidentally fall back into generic outer retries | Medium | High | Add an explicit targeted-retry tier and characterization tests for first retry vs final stop |
| Overloading `application_submission_result.json` breaks existing sync assumptions | Low | Medium | Keep `status` backward-compatible (`failed`) and add only optional fields (`failure_type`, `retry_class`, `artifacts`) with sync tests |
| Resume-upload verification gets wrongly retried and hides a deterministic mismatch | Medium | High | Keep `linkedin_resume_upload_verification_failed` out of the targeted-retry set |
| LinkedIn subprocess timeouts still lose the last step | Medium | Medium | Synthesize `linkedin_timeout_after_partial_fill` from current-attempt page/debug screenshots when available |

## Verification Strategy

Primary test files:

- `tests/test_autofill_linkedin.py`
- `tests/test_pipeline_orchestrator.py`
- `tests/test_job_db.py`
- `tests/test_submit_application.py`

Expected verification outcomes:

- LinkedIn failure result JSON always contains an explicit failure type and an existing screenshot path.
- Retryable LinkedIn failures requeue once and then stop with the same classified cause if repeated.
- Non-retryable LinkedIn failures stop immediately with their classified cause.
- Current-attempt artifact resolution and disk sync remain correct when a LinkedIn run produced only failure/debug artifacts and never reached review.

Repo-level validation:

- `uv run python -m pytest tests/test_autofill_linkedin.py tests/test_pipeline_orchestrator.py tests/test_job_db.py tests/test_submit_application.py -v`
- `uv run ruff check scripts/autofill_linkedin.py scripts/pipeline_orchestrator.py scripts/job_db.py tests/test_autofill_linkedin.py tests/test_pipeline_orchestrator.py tests/test_job_db.py tests/test_submit_application.py`
- `uv run python scripts/check_agent_docs.py`

## Implementation Order & Dependencies

```text
Unit 1 (LinkedIn runtime classification + cleanup)
    ->
Unit 2 (orchestrator targeted retry + timeout synthesis)
    ->
Unit 3 (disk-sync parity + repo docs)
```

Unit 1 should land first so the orchestrator work has a concrete artifact contract to consume. Unit 3 finishes the self-contained repo requirement after the runtime and orchestrator semantics are stable.

## Key Files

| File | Why it matters |
|------|----------------|
| `scripts/autofill_linkedin.py` | LinkedIn runtime, artifact emission, failure classification, and current-attempt cleanup |
| `scripts/pipeline_orchestrator.py` | Targeted retry policy, answer-refresh failure messages, and timeout handling |
| `scripts/job_db.py` | Disk sync of generic failure results into DB `failure_type` / `error_message` |
| `scripts/submit_review_common.py` | Existing current-attempt artifact-resolution seam that already supports `submit_debug_screenshot` |
| `tests/test_autofill_linkedin.py` | Primary behavior characterization for LinkedIn runtime changes |
| `tests/test_pipeline_orchestrator.py` | Retry classification and failure-state propagation coverage |
| `tests/test_job_db.py` | Disk sync coverage for generic failed submission results |
| `tests/test_submit_application.py` | Current-attempt artifact resolution coverage |
| `docs/output-structure.md` | Canonical artifact contract for repo-local consumers |
| `docs/autofill-patterns.md` | Board-specific guidance for future LinkedIn debugging |
