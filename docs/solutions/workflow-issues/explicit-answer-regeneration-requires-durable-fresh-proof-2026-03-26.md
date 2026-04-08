---
title: "Explicit answer regeneration requires durable fresh proof"
category: workflow-issues
date: 2026-03-26
tags:
  - answer-refresh
  - draft-review
  - cache-bypass
  - submit-generation
  - user-trust
component: development_workflow
components:
  - scripts/answer_refresh_state.py
  - scripts/application_submit_common.py
  - scripts/autofill_greenhouse.py
  - scripts/pipeline_orchestrator.py
  - scripts/job_db.py
  - scripts/job_web.py
  - scripts/draft_manager.py
  - scripts/build_draft_summary.py
  - scripts/static/app.js
  - scripts/static/style.css
problem_type: workflow_issue
root_cause: missing_workflow_step
resolution_type: workflow_improvement
severity: high
---

# Explicit Answer Regeneration Requires Durable Fresh Proof

## Problem

Explicit answer regeneration was behaving like a cache hint instead of a product contract. A user could click `Regenerate Answers` or trigger a full regenerate, return to `draft`, and still be looking at older `application_answers.json` and raw answer artifacts, so prompt or policy changes never actually reached the visible draft.

## Symptoms

- `Regenerate Answers` could finish with stale answers still visible in the draft.
- Full regenerate and restart flows could refresh other assets while silently reusing old answer caches.
- `submit/application_answers.json` and raw answer logs could remain unchanged after an explicit refresh request.
- The web UI had no durable proof showing when answers were regenerated or which provider produced them.
- Interrupted `reanswering` jobs could reset back to `draft` without any visible non-success answer-refresh state.
- Drafts with zero generated answer fields were indistinguishable from successful refreshes.
- Prompt-only changes, such as new punctuation rules, could appear to "not work" because cached answers masked the updated prompt.

## What Didn't Work

- Prompt guidance alone in `scripts/application_submit_common.py` was insufficient because explicit regenerate flows could still reuse cached answers.
- Clearing only `.asset_pipeline_state.json` during full regenerate did not help because answer generation could still short-circuit on the active `submit/application_answers.json` or copy a matching cache from older `submit*` directories.
- Treating a return to `draft` as proof of success was a bad assumption. Status alone did not prove a fresh answer-generation run happened.
- Using generic job/provider metadata as UI proof did not work because asset-generation provider metadata can differ from the provider that actually generated application answers.

## Solution

The fix turned explicit answer refresh into a request-scoped lifecycle backed by durable artifacts.

1. Add a new output-root sidecar, `answer_refresh_status.json`, managed by `scripts/answer_refresh_state.py`.
2. Mark every answer-affecting entrypoint as `pending` before queueing work:
   - web `reanswer`
   - answers-only regenerate
   - `restart-pipeline`
   - draft overrides
   - full `regenerate_job()` for draft/stopped jobs
3. Carry a unique `request_id` through the refresh lifecycle so older completions cannot satisfy newer refresh requests.
4. Bypass answer-cache reuse at the actual generation seams in both shared and Greenhouse paths.
5. Rewrite structured and raw answer artifacts with request-scoped metadata.
6. Finalize refresh state from durable artifacts plus the autofill report as `fresh`, `not_applicable`, or `failed`.
7. Surface the same proof in the web UI and draft summary artifacts.

Shared cache-bypass is now enforced where answers are generated, not only where the user clicks regenerate:

```python
refresh_request_id = current_answer_refresh_request_id(out_dir)
force_fresh_generation = refresh_request_id is not None
if force_fresh_generation:
    clear_answer_generation_artifacts(out_dir)
```

Structured answer artifacts now record the active refresh request:

```json
{
  "generated_at_utc": "2026-03-26T18:30:00+00:00",
  "provider": "claude",
  "refresh_request_id": "..."
}
```

The orchestrator finalizes state from persisted evidence instead of inferring freshness from status transitions:

- `fresh` when the current `request_id` matches rewritten answer artifacts and generated-answer fields exist
- `not_applicable` when the current draft has zero `generated_application_answer` fields
- `failed` when proof was requested but never materialized because of interruption, stale reset, missing artifacts, or provider failure

The visible proof surfaces now come from the same persisted source:

- `job_web.py` returns `job["answer_refresh"]`
- `scripts/static/app.js` renders one shared non-sticky proof card below the sticky dock across all job-detail tabs
- `scripts/draft_manager.py` writes an `## Answer Refresh` section into `draft_summary.md`
- `scripts/build_draft_summary.py` renders that section into `draft_summary.png`

## Why This Works

The old behavior failed because "regenerate answers" had no durable workflow contract. Once refresh became a request-scoped state machine backed by `answer_refresh_status.json`, rewritten answer artifacts, and request-id correlation, the product could distinguish "fresh run with identical text" from "no fresh run happened" and could fail visibly instead of silently returning stale drafts.

Because cache bypass now happens inside `generate_application_answers()` and Greenhouse's parallel `_generate_application_answers()` path, every runtime surface inherits the same contract. Because finalization validates the current `request_id` against persisted artifacts, stale completions cannot masquerade as success.

## Verification

- `uv run python -m pytest tests/test_answer_refresh_state.py tests/test_draft_manager.py tests/test_job_web.py tests/test_pipeline_orchestrator.py -v`
- `uv run python -m pytest tests/test_submit_application.py tests/test_greenhouse_autofill.py tests/test_job_db.py tests/test_job_worker.py tests/test_job_web.py tests/test_draft_manager.py tests/test_pipeline_orchestrator.py -k 'refresh_pending or reanswering or answer_refresh or regenerate_job_marks or restart_pipeline_marks or finalize_pending_answer_refresh' -v`
- `uv run ruff check scripts/answer_refresh_state.py scripts/application_submit_common.py scripts/autofill_greenhouse.py scripts/build_draft_summary.py scripts/draft_manager.py scripts/job_db.py scripts/job_web.py scripts/output_layout.py scripts/pipeline_orchestrator.py tests/test_answer_refresh_state.py tests/test_draft_manager.py tests/test_greenhouse_autofill.py tests/test_job_db.py tests/test_job_web.py tests/test_job_worker.py tests/test_output_layout.py tests/test_pipeline_orchestrator.py tests/test_submit_application.py`
- `node --check scripts/static/app.js`
- `uv run python scripts/check_agent_docs.py`
- Browser verification on `http://127.0.0.1:8420/#job/159` confirmed:
  - one shared proof card rendered below the sticky dock and above the active tab content
  - the same proof remained visible on Answers, Resume, Cover Letter, and Screenshot tabs
  - after 10 tab toggles, the page still had exactly 1 proof region total and 0 proof regions inside `#tab-answers`

## Prevention

- Keep `tests/test_answer_refresh_state.py`; it guards legacy `unknown`, superseded requests, stale finalization, and artifact proof loading.
- Keep the shared and Greenhouse cache-bypass regression tests in `tests/test_submit_application.py` and `tests/test_greenhouse_autofill.py`.
- Keep `_finalize_pending_answer_refresh()` characterization in `tests/test_pipeline_orchestrator.py` so `fresh` and `not_applicable` stay artifact-derived.
- Keep stale-reset coverage in `tests/test_job_db.py` and `tests/test_job_worker.py` so interrupted `reanswering` work cannot silently fall back to success semantics.
- Keep UI/draft artifact coverage in `tests/test_job_web.py` and `tests/test_draft_manager.py`.
- Preserve this rule: any user action whose semantics imply answers may change must call `mark_answer_refresh_pending(...)` before queueing work.
- Do not treat `draft` status or `jobs.provider` as answer freshness proof.
- For future answer-refresh changes, verify the shared helper proof card below the dock and stress the tab switching path so proof blocks do not duplicate, disappear, or obscure the first meaningful tab content.

## Cross-References

- Brainstorm: `docs/brainstorms/2026-03-26-answer-regeneration-proof-requirements.md`
- Plan: `docs/plans/2026-03-26-007-fix-answer-regeneration-proof-plan.md`
- Related: `docs/solutions/integration-issues/strict-submit-answer-schema-requires-nullable-optionals.md`
- Related: `docs/solutions/integration-issues/adding-new-llm-provider.md`
- Related pattern: `docs/solutions/logic-errors/submit-attempt-scoped-confirmation-email-replies.md`
- GitHub issues: no related issues found via `gh issue list --search "answer refresh application_answers regenerate draft_summary cache reuse" --state all --limit 5`
