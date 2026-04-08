---
title: "Current draft proof must backfill stale root answer state from active submit artifacts"
category: workflow-issues
date: 2026-04-05
tags:
  - draft-proof
  - answer-refresh
  - answer-verification
  - disk-sync
  - queue-trust
component: development_workflow
components:
  - scripts/answer_state_sync.py
  - scripts/submit_application.py
  - scripts/job_db.py
  - scripts/draft_manager.py
  - tests/test_answer_state_sync.py
  - tests/test_submit_application.py
  - tests/test_job_db.py
  - tests/test_draft_manager.py
problem_type: workflow_issue
root_cause: missing_workflow_step
resolution_type: workflow_improvement
severity: high
---

# Current Draft Proof Must Backfill Stale Root Answer State From Active Submit Artifacts

## Problem

The repo could already contain correct current submit proof on disk while the root draft sidecars still looked stale or missing. That split the truth across two layers:

- the active `submit/` artifacts were current
- the root `answer_refresh_status.json` / `answer_verification_status.json` were stale, absent, or legacy

Queue confidence, draft summaries, and job-detail views then read the stale root layer instead of the current proof.

## Symptoms

- Ready drafts with `0` generated answers still surfaced root verification as `unknown`.
- Ready drafts with generated answers and a current `submit/application_answers.json` still surfaced root answer refresh as `unknown`.
- Draft summaries could render `unknown` answer state even though the active submit attempt already proved `fresh` or `not_applicable`.
- Repo-local `bin/job-assets sync` could move rows back to `draft` without also restoring the root answer-state files that web/draft surfaces depend on.

Representative live rows before the fix:

- Turo `#143` (`output/turo/lead-pm-host`)
  - active `submit/workday_autofill_report.json` had `0` `generated_application_answer` fields
  - root `answer_refresh_status.json` already said `not_applicable`
  - root `answer_verification_status.json` was missing
- Abridge `#727` (`output/abridge/product-lead-life-sciences-new-products`)
  - active `submit/ashby_autofill_report.json` had `1` generated answer
  - active `submit/application_answers.json` and `submit/answer_verification.json` were current
  - root `answer_refresh_status.json` was missing

## What Didn't Work

- Submit-time reconciliation only in `scripts/submit_application.py` was too narrow. Board-local reruns and repo-local disk sync did not inherit it.
- Reading only the root sidecars was insufficient because older drafts could have correct current proof in `submit/` while the root state remained legacy.
- Re-running `generate_draft_summary()` without a reconciliation step preserved stale `unknown` state in the rendered artifact.

## Solution

Add one shared reconciler, `scripts/answer_state_sync.py`, and use the current submit attempt as the source of truth for root answer-state sidecars.

Rules:

1. Read the active submit attempt's autofill report.
2. If the current report has `0` generated answers:
   - sync root answer refresh to `not_applicable`
   - sync root answer verification to `not_applicable`
   - point both at the active submit dir
3. If the current report has generated answers:
   - backfill root answer refresh from `submit/application_answers.json`
   - backfill root answer verification from `submit/answer_verification.json`
4. Do not stomp a genuinely `pending` state during an in-flight request unless the caller is the submit path that just finished the run.
5. Do not rewrite matching state on every read.

The helper is now called from:

- `scripts/submit_application.py`
- `scripts/job_db.py::sync_job_from_disk()`
- `scripts/draft_manager.py::generate_draft_summary()`

## Why This Works

The old bug was not board logic; it was a missing shared workflow step. Once the same reconciliation logic runs in submit flow, repo-local disk sync, and draft-summary generation, every surface reads the same current-proof contract.

Because the helper keys off the active submit attempt's report first, stale older verification artifacts cannot override a current `0`-generated-answer draft. Because non-submit callers preserve `pending`, the fix does not mask an in-flight reanswer or reverification request.

## Live Evidence

Real repo-local sync after the fix:

- `uv run bin/job-assets sync`

Representative rows after that sync:

- Turo `#143`
  - root `answer_refresh_status.json` now has `status = not_applicable` and `proof_submit_dir = submit`
  - root `answer_verification_status.json` now exists with `status = not_applicable`, `blocked_answer_count = 0`, `proof_submit_dir = submit`
  - live job-detail API now returns `queue_review_summary.overall_confidence = high`, `confidence_label = Ready to submit`, `verification_state = not_applicable`
- Abridge `#727`
  - root `answer_refresh_status.json` now exists with `status = fresh`, `answer_provider = openai`, `generated_answer_count = 1`, `proof_submit_dir = submit`
  - root `answer_verification_status.json` remains `verified`
  - live job-detail API now returns `answer_refresh.status = fresh` and `draft_review_state.state = ready`

Ready-draft audit delta from the real sync:

- Before sync, representative stale classes included:
  - `11` ready drafts with `0` generated answers and `verification = unknown`, `refresh = not_applicable`
  - `4` ready drafts with `0` generated answers and `verification = unknown`, `refresh = unknown`
  - `14` ready drafts with generated answers and `verification = verified`, `refresh = unknown`
- After sync on the fixed code:
  - `zero_llm::not_applicable::not_applicable = 54`
  - `generated::verified::fresh = 25`
  - the stale targeted classes above dropped to `0`

Saved proof artifacts:

- `output/playwright/nad-32-turo-lead-pm-host-pre.png`
- `output/playwright/nad-32-turo-lead-pm-host-post.png`
- `output/playwright/nad-32-abridge-product-lead-life-sciences-new-products-pre.png`
- `output/playwright/nad-32-abridge-product-lead-life-sciences-new-products-post.png`
- `output/playwright/nad-32-job-143-web-ui.png`
- `output/playwright/nad-32-job-727-web-ui.png`

## Verification

- `uv run python -m pytest tests/test_answer_state_sync.py tests/test_submit_application.py tests/test_job_db.py tests/test_draft_manager.py -v`
- `uv run ruff check scripts/answer_state_sync.py scripts/submit_application.py scripts/job_db.py scripts/draft_manager.py tests/test_answer_state_sync.py tests/test_job_db.py tests/test_draft_manager.py tests/test_submit_application.py`
- `uv run bin/job-assets sync`
- `curl -s http://127.0.0.1:8420/api/jobs/143 | jq '.queue_review_summary, .answer_refresh, .draft_review_state'`
- `curl -s http://127.0.0.1:8420/api/jobs/727 | jq '.queue_review_summary, .answer_refresh, .draft_review_state'`

## Prevention

- Keep `tests/test_answer_state_sync.py`; it guards the shared current-proof contract directly.
- Keep the `tests/test_job_db.py` regression so repo-local sync keeps repairing zero-generated-answer drafts.
- Keep the `tests/test_draft_manager.py` regression so rendered draft artifacts cannot drift back to stale `unknown`.
- Preserve this rule: root answer sidecars are derived from the active submit proof, not inferred only from job status.
- Treat the remaining `generated::unknown::fresh` draft cohort as a separate problem. Those rows are missing current verification proof, not stale root-state sync.

## Cross-References

- Plan: `docs/superpowers/plans/2026-04-04-stopped-draft-backlog-repair.md`
- Related: `docs/solutions/workflow-issues/explicit-answer-regeneration-requires-durable-fresh-proof-2026-03-26.md`
- Related: `docs/solutions/workflow-issues/stopped-job-audits-must-be-repo-local-and-artifact-backed-2026-03-27.md`
- Linear: `NAD-32`
