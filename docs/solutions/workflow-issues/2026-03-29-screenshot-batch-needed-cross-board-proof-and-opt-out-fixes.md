---
title: "2026-03-29 screenshot batch needed cross-board proof and opt-out fixes"
category: workflow-issues
date: 2026-03-30
last_updated: 2026-03-30
tags:
  - screenshot-batch
  - linkedin
  - lever
  - greenhouse
  - workday
  - duplicates
  - draft-proof
components:
  - AGENTS.md
  - scripts/application_submit_common.py
  - scripts/autofill_greenhouse.py
  - scripts/autofill_lever.py
  - scripts/autofill_linkedin.py
  - scripts/autofill_workday.py
  - scripts/job_db.py
  - scripts/job_discovery.py
  - scripts/pipeline_draft_proof.py
  - scripts/question_classifier.py
  - tests/test_autofill_linkedin.py
  - tests/test_autofill_workday.py
  - tests/test_greenhouse_autofill.py
  - tests/test_job_db.py
  - tests/test_job_discovery.py
  - tests/test_lever_autofill.py
  - tests/test_pipeline_orchestrator.py
  - tests/test_question_classifier.py
  - tests/test_submit_application.py
problem_type: workflow_issue
root_cause: missing_workflow_step
resolution_type: code_fix
severity: high
---

# 2026-03-29 Screenshot Batch Needed Cross-Board Proof And Opt-Out Fixes

## Problem

The `2026-03-29 10:10 AM` screenshot batch exposed six regressions that were easy to miss in board-specific spot checks but violated the repo's standing rules:

- duplicate / already-applied jobs were still entering the queue
- Greenhouse draft proof could stop on an incomplete pre-submit screenshot
- LinkedIn Easy Apply still left the follow-company opt-in checked on review
- anti-AI "type your name" traps could fall through to generated answers
- Workday could accumulate duplicate uploaded resume cards across reruns
- closed jobs needed a stronger archive-and-dismiss path when the posting disappeared

The common failure pattern was not one bad board. It was missing cross-board generalization plus missing proof that the live UI still matched policy at the final review boundary.

## Symptoms

- Valon's LinkedIn wrapper row (`job #528`) was still present even though the same role already existed as submitted Ashby job `#371`.
- dbt Labs draft proof showed that Greenhouse pre-submit capture was still a viewport shot instead of a durable full-page proof artifact.
- FloQast showed the anti-AI prompt asking for the candidate's first name in all caps, but the deterministic answer path was not actually reaching Lever's custom text payload.
- Turo's Workday experience page accumulated multiple `Jerrison Li Resume - Turo Inc..pdf` upload cards on reruns.
- Greylock's LinkedIn review step still showed `Follow Greylock Partners...` checked even though the project rule is to opt out of these prompts.
- Closed-job handling had archive coverage, but LinkedIn-sourced rows still needed the matching "hide this recommendation" action wired into the same unavailable-job path.

## What Didn't Work

- Duplicate prevention only handled the simple company+role case. LinkedIn wrapper titles and suffix variants like `Valon` vs `Valon Tech` still slipped through.
- Lever only routed a narrow set of generated text fields through shared answer generation, so deterministic classifier answers never reached some custom text prompts.
- LinkedIn only ran `_uncheck_follow_company()` on intermediate wizard steps. If the opt-in appeared only on the review step, draft proof captured the checked state untouched.
- The LinkedIn helper also assumed the checkbox itself was the clickable surface. Greylock's review DOM used a hidden checkbox plus a separate `label[for="follow-company-checkbox"]`.
- Workday only checked whether a matching resume was present. It did not dedupe the extra upload cards already on the page before deciding whether to upload again.
- Greenhouse review proof already had a stitched screenshot path, but pre-submit proof still needed the same treatment.

## Solution

### 1. Deterministic classified answers now survive all board paths

`scripts/question_classifier.py` now classifies anti-AI name prompts as `ai_captcha`. `scripts/application_submit_common.py` answers those prompts deterministically with the candidate's uppercase first name and skips the provider call entirely for classified fields. `scripts/autofill_lever.py` now routes the full custom text / textarea set through shared answer generation so deterministic answers reach the payload instead of dying in board-local filtering.

Live proof:

- `output/floqast/director-pm-ecosystem/submit-20260330T002710Z/application_answers.json`
- `output/floqast/director-pm-ecosystem/submit-20260330T002710Z/lever_autofill_pre_submit.png`

That rerun recorded `provider = "deterministic_classification"` and the anti-AI answer `JERRISON`, and the screenshot OCR shows the same answer on the live Lever form.

### 2. Greenhouse draft proof now uses full-page screenshots at both checkpoints

`scripts/autofill_greenhouse.py` now uses the stitched screenshot path for both pre-submit and review checkpoints, not just the review screenshot.

Live proof:

- `output/dbt-labs/staff-pm-developer-experience/submit-20260330T003018Z/greenhouse_autofill_pre_submit.png`
- `output/dbt-labs/staff-pm-developer-experience/submit-20260330T003018Z/greenhouse_autofill_review.png`

Both files were regenerated in the same rerun and preserve the full form instead of a cropped viewport shell.

### 3. Duplicate prevention now normalizes wrapper titles and company variants

`scripts/job_db.py` and `scripts/job_discovery.py` now normalize company aliases and LinkedIn wrapper titles before duplicate comparison. Discovery promotion links duplicates back to the existing job instead of enqueuing a second row, and insertion-time duplicate checks now treat `Valon` plus wrapper-title slugs as the same role as submitted `Valon Tech / senior-pm-product-infrastructure`.

Data reconciliation performed in this pass:

- archived `jobs.db` row `#528` as a duplicate of submitted job `#371`
- preserved the row's repo-local LinkedIn `external_apply` artifact on disk, but removed the row from the active queue

Related evidence:

- original bug screenshot: Obsidian `Pasted image 20260329145704.png`
- existing duplicate learning: `docs/solutions/database-issues/cross-source-duplicate-jobs.md`

### 4. LinkedIn review now actively opts out of follow-company prompts

`scripts/autofill_linkedin.py` now:

- runs `_uncheck_follow_company()` on the review step before pre-submit proof is captured
- prefers the associated `label[for=...]` click path for hidden LinkedIn checkboxes
- records the opt-out as `follow_company_opt_in = False` in the draft report when it successfully unchecks the box

Live proof:

- `output/greylock/sr-pm-security-ai/submit-20260329T185755Z/linkedin_autofill_pre_submit.png`
- `output/greylock/sr-pm-security-ai/submit-20260329T185755Z/linkedin_autofill_report.json`

The fresh rerun shows the `Follow Greylock Partners...` checkbox visibly unchecked, and the report records the same opt-out as a filled step sourced from `linkedin_opt_out_policy`.

### 5. Workday now dedupes uploaded resume cards before continuing

`scripts/autofill_workday.py` now finds duplicate resume delete controls, removes extras, and only treats the upload state as valid when a single matching card remains.

Live proof:

- `output/turo/lead-pm-host/submit-20260329T185846Z/workday_resume_dedupe_verified.png`
- `output/turo/lead-pm-host/submit-20260329T185846Z/workday_resume_dedupe_inspection.json`

The saved inspection JSON shows `itemCount = 1` and `deleteCount = 1`, and the screenshot shows one remaining uploaded resume card on the Workday experience page.

### 6. Closed jobs now archive and attempt LinkedIn dismissal from the same path

`scripts/pipeline_draft_proof.py` now calls `url_resolver.dismiss_linkedin_job_recommendation()` when a LinkedIn-sourced job is auto-archived as unavailable, and `tests/test_pipeline_orchestrator.py` covers both the archive and dismiss events.

This pass did not find a surviving live LinkedIn `job_closed` repro in the active queue after the other reruns. The policy change was therefore validated through repo-local tests rather than a fresh live screenshot.

## Why This Works

The fixes all move proof and policy closer to the exact runtime boundary where the bug was happening:

- deterministic prompts are answered before provider generation
- review-step opt-outs happen before the screenshot that becomes draft proof
- duplicate normalization happens before queue insertion and promotion
- Workday upload verification now measures the real DOM state, not just whether some matching filename exists somewhere on the page
- unavailable-job handling keeps archive and LinkedIn dismissal in the same workflow step instead of leaving them as separate human cleanup tasks

That is the right pattern for this repo: encode the rule once in shared logic, then prove it with both regression tests and fresh submit-bucket artifacts.

## Verification

Targeted tests:

- `uv run python -m pytest tests/test_pipeline_orchestrator.py tests/test_job_discovery.py tests/test_job_db.py tests/test_autofill_linkedin.py tests/test_autofill_workday.py tests/test_greenhouse_autofill.py tests/test_question_classifier.py tests/test_submit_application.py -q`
- `uv run python -m pytest tests/test_lever_autofill.py tests/test_question_classifier.py tests/test_submit_application.py -q`

Fresh live reruns / inspections:

- FloQast Lever rerun regenerated deterministic anti-AI answer proof
- dbt Labs Greenhouse rerun regenerated stitched pre-submit and review screenshots
- Greylock LinkedIn rerun regenerated review-step follow opt-out proof
- Turo Workday inspection regenerated post-dedupe screenshot plus DOM snapshot JSON

Data sync / cleanup performed:

- synced rerun state from disk back into `jobs.db` for jobs `#143`, `#174`, `#279`, and `#528`
- archived duplicate row `#528` after the normalized duplicate check confirmed it was the same role as submitted job `#371`

## Prevention

- If a board-specific rule is actually a user policy (`don't follow companies`, `type the candidate's name`, `don't duplicate uploaded resumes`), keep it in shared logic and report it explicitly in draft proof.
- Queue dedupe must normalize wrapper-company and wrapper-title variants before insert. Exact raw strings are not stable enough for aggregator imports.
- Review-step proof must be captured after every opt-out and guardrail has already been applied, not before.
- When a fix depends on live DOM behavior, preserve a repo-local artifact that survives the session: screenshot, JSON inspection snapshot, or both.

## Cross-References

- Related duplicate learning: `docs/solutions/database-issues/cross-source-duplicate-jobs.md`
- Related classifier learning: `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`
- Related draft-proof learning: `docs/solutions/logic-errors/draft-proof-must-prefer-canonical-job-assets-and-exact-visible-field-values-2026-03-29.md`
- Related Workday draft safety learning: `docs/solutions/workflow-issues/workday-draft-reruns-must-fail-closed-before-submit-2026-03-28.md`
