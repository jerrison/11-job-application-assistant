---
title: "Visible self-ID fields require live confirmation before draft approval"
category: logic-errors
date: 2026-03-26
tags:
  - visible-self-id
  - draft-review
  - pending-user-input
  - active-submit
  - screenshot-confirmation
module: System
component: development_workflow
components:
  - scripts/autofill_common.py
  - scripts/autofill_pipeline.py
  - scripts/application_submit_common.py
  - scripts/pipeline_orchestrator.py
  - scripts/draft_manager.py
  - scripts/job_web.py
  - scripts/draft_web.py
  - scripts/job_tui.py
  - scripts/autofill_ashby.py
  - scripts/autofill_lever.py
  - scripts/autofill_gem.py
  - scripts/autofill_eightfold.py
  - scripts/autofill_greenhouse.py
problem_type: logic_error
symptoms:
  - "Visible self-ID fields like race or ethnicity remained blank in draft screenshots while the system still treated the draft as complete."
  - "pending_user_input.json and review surfaces missed profile-backed self-ID blockers."
  - "approve_job() could advance a draft even when live confirmation for a self-ID answer never happened."
root_cause: missing_validation
resolution_type: code_fix
severity: high
---

# Visible Self-ID Fields Require Live Confirmation Before Draft Approval

## Problem

A draft screenshot showed a configured ethnicity answer still blank on the live application while the system believed the draft was ready. The autofill pipeline was treating visible self-ID fields as complete after the click or select action, instead of carrying them forward as blockers until the UI visibly confirmed the planned value.

## Symptoms

- Draft screenshots could still show blank race, ethnicity, pronoun, veteran, disability, gender, or similar self-ID fields even though the job was back in `draft`.
- `pending_user_input.json`, `draft_summary.md`, the web Answers tab, and TUI attention views could miss the blocker entirely because the runtime had already marked the step as filled.
- Approval paths only checked job status, so `approve_job()` could move an incomplete draft forward when the live form never reflected the planned self-ID answer.
- Greenhouse's custom unconfirmed-field path excluded demographic blockers, so that board family could silently lose the same review signal.

## What Didn't Work

- Happy-path `_fill_step()` handlers on Ashby, Lever, Gem, and Eightfold assumed that a successful interaction meant the answer was visible on the page.
- The shared unconfirmed-field flow treated every unresolved field the same, so optional non-self-ID fields and genuinely blocking self-ID gaps were not distinguished.
- Greenhouse filtered demographic gaps out before writing pending-user-input artifacts, which broke parity with the shared blocker flow.
- Approval, CLI, TUI, and web review surfaces were not consistently anchored to the current active submit attempt, so even correctly written blocker artifacts could be missed.

## Solution

The fix introduced an explicit blocker contract for visible self-ID fields and carried it from the board runtime all the way to approval gating.

`scripts/autofill_common.py` now tags these steps with blocker metadata:

```python
def mark_visible_self_id_step(step: dict | None, *, profile_field: str | None = None) -> dict | None:
    if step is None:
        return None
    step["blocks_draft_completion"] = True
    step["blocker_kind"] = VISIBLE_SELF_ID_BLOCKER_KIND
    if profile_field:
        step["profile_field"] = profile_field
    return step
```

`scripts/autofill_pipeline.py` retries only visible self-ID blockers once, then writes only blocking unconfirmed fields into `pending_user_input.json`:

```python
if retry_unconfirmed_visible_self_id_once:
    retry_candidates = [
        step
        for step in steps
        if is_visible_self_id_blocker(step)
        and not step.get("filled")
        and str(step.get("status") or "").strip().casefold() != "skipped_not_found"
    ]
```

Board adapters on Ashby, Lever, Gem, and Eightfold now re-read the rendered control before marking the step as filled. If the live UI still does not reflect the planned answer, they leave the step as planned, add an explanatory note, and let the shared blocker flow surface it for review. Greenhouse now tags runtime demographic steps with the same blocker metadata and no longer filters them out before writing pending-user-input artifacts.

`scripts/application_submit_common.py` preserves blocker metadata in `pending_user_input.json` and adds `load_pending_user_input_for_submit_attempt(...)` so every consumer reads the active submit attempt instead of defaulting to a stale bucket. `scripts/draft_manager.py`, `scripts/job_web.py`, `scripts/draft_web.py`, `scripts/job_tui.py`, and `scripts/static/app.js` now render a blocker-first `Needs Review` section from that payload.

Approval now fails closed on current-attempt blockers:

```python
def approve_job(conn: sqlite3.Connection, job_id: int) -> bool:
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row or str(row["status"] or "") not in _APPROVABLE_DRAFT_STATUSES:
        return False
    if current_draft_pending_user_input(conn, job_id) is not None:
        return False
```

`approve_job_failure_message(...)` gives CLI, TUI, and web callers a specific incomplete-draft message, including the first unresolved field label when available.

## Why This Works

The fix changes the contract from "the board script tried to answer the field" to "the system has durable proof that the live UI reflects the answer." That proof now flows through one shared artifact chain:

1. Board runtime tags and validates a visible self-ID step.
2. The autofill report preserves blocker metadata.
3. `pending_user_input.json` stores only blockers that still matter.
4. Draft summary, web, TUI, and CLI review surfaces render the same blocker payload.
5. Approval checks the current submit attempt and stops if blockers remain.

This also keeps scope tight. Optional non-self-ID gaps no longer fail the draft just because they were visible, while configured self-ID answers fail closed until the screenshot-backed confirmation exists.

## Verification

- `uv run python -m pytest tests/test_autofill_common.py tests/test_autofill_pipeline.py tests/test_submit_application.py tests/test_draft_manager.py tests/test_job_web.py tests/test_ashby_autofill.py tests/test_lever_autofill.py tests/test_gem_autofill.py tests/test_eightfold_autofill.py tests/test_greenhouse_autofill.py -q`
- Result: `388 passed`
- `uv run python -m py_compile` on the modified Python files
- `uv run ruff check scripts/autofill_common.py scripts/application_submit_common.py scripts/autofill_pipeline.py scripts/submit_application.py scripts/pipeline_orchestrator.py scripts/draft_manager.py scripts/job_web.py scripts/draft_web.py scripts/job_tui.py scripts/autofill_ashby.py scripts/autofill_lever.py scripts/autofill_gem.py scripts/autofill_eightfold.py scripts/autofill_greenhouse.py tests/test_autofill_common.py tests/test_autofill_pipeline.py tests/test_submit_application.py tests/test_draft_manager.py tests/test_job_web.py tests/test_ashby_autofill.py tests/test_lever_autofill.py tests/test_gem_autofill.py tests/test_eightfold_autofill.py tests/test_greenhouse_autofill.py`

## Prevention

- Any truthful profile-backed field that must be visibly present on the form should carry explicit blocker metadata until live confirmation happens.
- Only blocker-class unconfirmed fields should write `pending_user_input.json`; do not regress back to treating every optional visible field as draft-blocking noise.
- Approval and review surfaces must resolve artifacts from the current submit attempt, not a hardcoded `submit/` path or a historical bucket chosen by accident.
- Keep board-specific confirmation characterization tests so UI-library regressions on Ashby, Lever, Gem, and Eightfold fail loudly.
- Keep Greenhouse in parity with the shared blocker contract whenever runtime demographic handling changes.

## Investigation Steps

1. Started from a live draft screenshot where ethnicity remained unanswered even though the draft looked otherwise complete.
2. Traced how planned-but-unconfirmed fields moved from board runtimes into the shared autofill report and `pending_user_input.json`.
3. Found that self-ID steps were being marked as filled optimistically and that Greenhouse's custom path dropped demographic blockers entirely.
4. Tightened the shared blocker metadata, added one retry for visible self-ID fields, and made review plus approval consume current-attempt blocker artifacts.

## Cross-References

- Brainstorm: `docs/brainstorms/2026-03-26-visible-self-id-confirmation-requirements.md`
- Plan: `docs/plans/2026-03-26-008-fix-visible-self-id-confirmation-plan.md`
- Related: `docs/solutions/workflow-issues/explicit-answer-regeneration-requires-durable-fresh-proof-2026-03-26.md`
- Related: `docs/solutions/logic-errors/submit-attempt-scoped-confirmation-email-replies.md`
- Related: `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`
- GitHub issues: no related issues found via `gh issue list --search "visible self-id pending_user_input draft approval ethnicity race pronouns" --state all --limit 5`
