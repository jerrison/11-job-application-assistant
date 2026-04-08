---
title: "Workday draft reruns must fail closed before submit"
category: workflow-issues
date: 2026-03-28
last_updated: 2026-03-28
tags:
  - workday
  - draft-mode
  - submit-guard
  - safety
  - autodesk
  - review-shell
module: System
component: development_workflow
components:
  - AGENTS.md
  - scripts/autofill_workday.py
  - tests/test_autofill_workday.py
  - docs/autofill-patterns.md
problem_type: workflow_issue
root_cause: missing_workflow_step
resolution_type: code_fix
severity: critical
---

# Workday Draft Reruns Must Fail Closed Before Submit

## Problem

A supposed Workday draft rerun for Autodesk crossed the last review boundary and resulted in a real submitted application. The repo later reconciled the job truthfully as `already_applied` plus email-confirmed, but draft mode had already failed its core safety promise.

## Symptoms

- The Autodesk output bucket preserved review-stage artifacts like `submit/workday_autofill_pre_submit.png` and `submit/workday_autofill_pages/page_09_review.png`, which proved the flow reached Review during a draft rerun.
- The same output bucket later held `submit/application_confirmation_email.json` with Gmail message id `19d32a7151ce1fc9` received at `2026-03-28T04:15:03+00:00`, confirming a real Workday application submission.
- A subsequent rerun hit Autodesk's public `You applied for this job` / `View Application` state, and `submit/application_submission_result.json` was written as `status: "already_applied"` at `2026-03-28T04:20:38+00:00`.
- `jobs.db` job `#328` ended up reconciled as `submitted` with `confirmation_method = website` and `email_confirmed = true`, which was correct for data truth but exposed that draft mode had not failed closed.

## What Didn't Work

- `_click_next_button()` treated `Submit` as just another forward-navigation label, so a review shell could be advanced through the generic next-step helper instead of an explicit submit-only path.
- Workday review detection depended too heavily on heading/body heuristics. If the review shell appeared with unexpected heading text, the runtime could misclassify the page and keep navigating.
- The later `already_applied` reconciliation and Gmail confirmation matching corrected the final job state, but those are after-the-fact truth repairs, not preventive safety controls.

## Solution

The fix tightened both the runtime guardrail and the agent-facing rule.

`scripts/autofill_workday.py` now has an explicit review-shell detector that fails closed on either a stable Workday review root or a live visible submit control paired with review text:

```python
def _is_workday_review_shell(page) -> bool:
    review_root_selectors = (
        "[data-automation-id='applyFlowReviewPage']",
        "[data-automation-id='reviewPage']",
    )
    ...
    return any(marker in body_text for marker in ("review and submit", "review application", "submit application"))
```

`_detect_current_page()` now defers to that helper before continuing through normal wizard-step detection:

```python
heading_text = _workday_heading_text(page)

if _is_workday_review_shell(page):
    return PAGE_REVIEW
```

Most importantly, `_click_next_button()` no longer treats `Submit` as a generic next action:

```python
for name in ("Save and Continue", "Next", "Continue"):
    ...
```

The repo-level agent rule was updated too. `AGENTS.md` now states that draft mode must fail closed at the final review boundary whenever a submit control is visible or page detection is ambiguous.

## Why This Works

The failure mode required two things at once: a review shell that was not classified strongly enough, and a fallback navigation helper willing to click the live submit control. The guard removes both conditions. Review shells are now recognized earlier and more explicitly, and the generic next-step helper is no longer allowed to submit even if state detection drifts.

This is deliberately fail-closed. A false positive now stops a draft early with proof instead of silently submitting a real application. That tradeoff is correct for draft mode.

## Verification

- `uv run python -m pytest tests/test_autofill_workday.py -q`
- Result: `26 passed`
- `uv run ruff check scripts/autofill_workday.py tests/test_autofill_workday.py`
- Browser-level regression via `uv run python` + Playwright local fixture:
  - loaded a minimal DOM with `[data-automation-id='applyFlowReviewPage']` and a live `Submit Application` button
  - verified `_is_workday_review_shell(page) is True`
  - verified `_detect_current_page(page) == PAGE_REVIEW`
  - verified `_click_next_button(page) is False`
  - verified the submit button's click counter stayed at `0`

## Prevention

- Draft-mode runtimes must treat final submit controls as a separate trust boundary, not as another variant of "next."
- When a board has a stable review-shell selector, prefer that explicit signal over heading-only heuristics.
- Reconciliation artifacts like `already_applied` and confirmation-email matching should remain truthful, but they must not be treated as substitutes for pre-submit safety guards.
- Keep at least one regression that proves a live review-shell submit button cannot be activated by the generic navigation helper.
- Preserve repo-local evidence for any draft-mode safety incident so future sessions can see both the accidental outcome and the preventive fix in one place.

## Investigation Steps

1. Confirmed that Autodesk job `#328` had been reconciled as `submitted` and `email_confirmed = true` in `jobs.db`.
2. Read the repo-local evidence under `output/autodesk/senior-principal-pm-advanced-solutions/submit/`, including the pre-submit screenshot, review-page screenshot, `application_submission_result.json`, and `application_confirmation_email.json`.
3. Identified the unsafe navigation path in `scripts/autofill_workday.py`: review detection could drift, and `_click_next_button()` still allowed `"Submit"`.
4. Added a review-shell detector, removed `Submit` from generic forward navigation, and verified the fix with both unit tests and a real Playwright page fixture.

## Cross-References

- Audit baseline: `docs/solutions/workflow-issues/stopped-job-audits-must-be-repo-local-and-artifact-backed-2026-03-27.md`
- Active brainstorm: `docs/brainstorms/2026-03-25-pipeline-resilience-requirements.md`
- Related blocker contract: `docs/solutions/logic-errors/visible-self-id-draft-blockers-2026-03-26.md`
- Related reply/idempotency learning: `docs/solutions/logic-errors/submit-attempt-scoped-confirmation-email-replies.md`
- GitHub issues: no related issues found via `gh issue list --search "Workday draft submit guard Autodesk already_applied draft mode" --state all --limit 5`
