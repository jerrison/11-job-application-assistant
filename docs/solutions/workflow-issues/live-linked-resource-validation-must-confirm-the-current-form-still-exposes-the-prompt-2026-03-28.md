---
title: "Live linked-resource validation must confirm the current form still exposes the prompt"
category: workflow-issues
date: 2026-03-28
tags:
  - linked-resources
  - live-validation
  - form-drift
  - review-ui
  - draft-proof
component: development_workflow
components:
  - jobs.db
  - scripts/application_submit_common.py
  - scripts/autofill_ashby.py
  - scripts/autofill_greenhouse.py
  - scripts/draft_manager.py
  - scripts/job_web.py
  - docs/plans/2026-03-28-001-feat-linked-resource-submit-answers-plan.md
problem_type: workflow_issue
root_cause: missing_workflow_step
resolution_type: workflow_improvement
severity: medium
---

# Live Linked-Resource Validation Must Confirm the Current Form Still Exposes the Prompt

## Problem

The linked-resource submit flow already had a live Ramp/Ashby proof case, but the follow-up todo asked for one more live board-consumer validation. Historical output buckets made Datadog and Motion Recruitment look like candidates, but those artifacts were stale or not on the shared generated-answer path, so they could not be trusted as current parity proof.

## Symptoms

- `output/datadog/senior-technical-pgm-knowledge-systems/submit/ashby_autofill_payload.json` still contained Ramp's db-fiddle question even though the current `jobs.db` row for job `#170` routes through Greenhouse.
- A fresh Datadog rerun on `2026-03-28` rewrote the Greenhouse artifacts but did **not** produce `submit/linked_resource_context.json`, `submit/linked_resource_evidence/`, or a `linked_resources` block in `submit/application_answers.json`.
- `output/motion-recruitment/senior-pm-ml/submit/ashby_application_page.html` still embedded the same Ramp prompt, but `scripts/autofill_motionrecruitment.py` never calls `generate_application_answers()`, so it cannot validate the shared review surfaces from the linked-resource feature.
- The current repo only had fresh `linked_resources` proof on the Ramp Ashby jobs, so "another board consumer" was no longer available as a live candidate.

## What Didn't Work

- Picking a validation target from historical submit artifacts alone was misleading. Old payloads and saved application HTML can outlive board changes and current form drift.
- Assuming that any saved page containing the prompt also exercises the shared generated-answer contract was wrong. Some board wrappers capture embedded HTML but never run `generate_application_answers()`.
- Treating "same prompt appears somewhere in the output tree" as equivalent to "current live board still exposes the prompt" created false confidence.

## Solution

Add a validation preflight step before claiming cross-board linked-resource parity:

1. Confirm the candidate board consumer actually routes through `generate_application_answers()` or its board-specific equivalent.
2. Confirm the **current** live form or freshly generated answer artifacts still expose the linked-resource prompt.
3. Only then use the rerun as parity proof for `draft_summary.md`, the Answers tab, and linked-resource artifacts.

For this round:

- Datadog was explicitly invalidated as the non-Ramp proof target after the live Greenhouse rerun showed the prompt no longer exists on the current form.
- Motion Recruitment was rejected as a proof target because its board-local automation fills only the wrapper modal fields and does not exercise the shared submit-answer pipeline.
- The still-live Ramp job `output/ramp/pm-financial-intelligence` was rerun to validate the persisted answer surfaces end to end:
  - `submit/application_answers.json` now carries `linked_resources`
  - `draft_summary.md` now shows a `Linked Resource:` provenance line
  - the web UI Answers tab shows `Linked resource: db_fiddle via ...`
  - browser evidence was captured in `output/playwright/ramp-pm-financial-intelligence-answers.png`

That does not create new board parity by itself, but it closes the actionable product work for the current repo state and documents why a non-Ramp parity proof could not be produced honestly.

## Why This Works

The failure mode here was not broken code. It was broken validation targeting. By verifying both the board consumer and the current live prompt before rerunning, future investigations can distinguish:

- stale historical evidence
- board wrappers that bypass shared generated answers
- real current candidates that can prove cross-board parity

That keeps follow-up todos grounded in live product reality instead of the accident of what old output directories still contain.

## Verification

- `job-assets submit output/datadog/senior-technical-pgm-knowledge-systems --provider openai --headless`
- Confirmed the fresh Datadog Greenhouse rerun produced no linked-resource artifacts in `submit/application_answers.json`, `submit/linked_resource_context.json`, or `submit/linked_resource_evidence/`
- `job-assets submit output/ramp/pm-financial-intelligence --provider openai --headless`
- Regenerated `output/ramp/pm-financial-intelligence/draft_summary.md` after finalizing the pending answer refresh
- Browser-checked `http://127.0.0.1:8420/#job/263` and captured `output/playwright/ramp-pm-financial-intelligence-answers.png`, confirming the Answers tab shows `Linked resource: db_fiddle via https://www.db-fiddle.com/f/sRqKozBHiTZ9rZ8W14D8wS/29`

## Prevention

- Before opening a live-validation todo, preflight the candidate with current artifacts rather than historical payloads alone.
- Require two checks for parity candidates:
  - the board automation must use shared generated answers
  - the current live form must still expose the linked-resource prompt
- If the only remaining live linked-resource cases are on one board, validate the saved review surfaces there and document the blocker instead of overstating cross-board parity.
- Treat stale embedded application HTML as a clue, not as current truth.

## Cross-References

- Plan: `docs/plans/2026-03-28-001-feat-linked-resource-submit-answers-plan.md`
- Related learning: `docs/solutions/workflow-issues/explicit-answer-regeneration-requires-durable-fresh-proof-2026-03-26.md`
- Related learning: `docs/solutions/workflow-issues/stopped-job-audits-must-be-repo-local-and-artifact-backed-2026-03-27.md`
