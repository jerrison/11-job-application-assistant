---
title: "Make confirmation email self-replies idempotent per submit attempt"
category: logic-errors
date: 2026-03-26
last_updated: 2026-03-28
tags:
  - confirmation-email
  - idempotency
  - reapply
  - submit-attempt
  - gmail
  - post-submit
component: email_processing
components:
  - scripts/application_submit_common.py
  - scripts/output_layout.py
  - scripts/pipeline_orchestrator.py
  - scripts/notion_job_applications.py
  - scripts/submit_application.py
problem_type: logic_error
root_cause: logic_error
resolution_type: code_fix
severity: medium
---

# Submit-Attempt-Scoped Confirmation Email Replies

## Problem

Successful applications were generating duplicate self-replies in the employer confirmation thread. Harvey and Alchemy both showed one real employer confirmation plus two self-replies because the board submit flow sent the reply first and worker post-submit later sent it again for the same role output.

## Symptoms

- Gmail threads showed one real confirmation email and two sent messages from `jerrisonli@gmail.com`
- Worker logs showed `email_reply_sent` even when the board-local submit path had already replied
- Reapply attempts were at risk of reading artifacts from the wrong submit bucket after the active submit pointer reset back to `submit/`

## What Didn't Work

- Treating the Gmail thread as the dedupe boundary would have coupled send suppression to a value discovered late in the flow and would not map cleanly to reruns before a thread id was resolved
- Treating `jobs.db` as the dedupe boundary would have made the worker the source of truth even though board-local submit flows also send replies
- Reusing the active submit pointer alone was not sufficient because `record_website_confirmation(...)` resets the active pointer after a confirmed reapply submit
- The previous artifact fallback in `application_submit_common.py` used `role_submit_path(...)`, which followed the current active pointer instead of the confirmed submit attempt that actually owned the submission

## Solution

Two changes were required:

1. Extract a shared notion of the latest confirmed submit attempt into `scripts/output_layout.py`
2. Store confirmation-email reply state in the resolved submit attempt and suppress only after a recorded successful send

The shared resolver now prefers the latest confirmed `submit-*` directory when post-submit code is running after the active pointer has already been reset:

```python
def latest_confirmed_submit_dir(out_dir: str | Path) -> Path | None:
    for submit_dir in submit_dirs_by_mtime(out_dir):
        for name in (SUBMISSION_RESULT_JSON, WEBSITE_CONFIRMATION_JSON):
            payload = _read_submit_json(submit_dir / name)
            if isinstance(payload, dict) and payload.get("website_confirmed"):
                return submit_dir
    return None
```

`application_submit_common.py` now resolves the reply bucket from that shared helper, writes `confirmation_email_reply.json` into that submit attempt, and returns a structured result:

```python
def send_confirmation_email_reply(..., caller: str = "automatic") -> dict:
    submit_dir = _confirmation_email_reply_submit_dir(out_dir)
    state_path = _confirmation_email_reply_state_path(submit_dir)
    prior_state = _load_confirmation_email_reply_state(state_path)

    if prior_state.get("sent") is True:
        state = _persist_confirmation_email_reply_outcome(
            state_path,
            prior_state,
            status="skipped_duplicate",
            caller=caller,
            board_name=board_name,
            reason="reply_already_sent",
        )
        return _reply_result("skipped_duplicate", ...)
```

Important contract details:

- `sent: true` is the only suppression bit
- `not_sent` outcomes remain retryable
- reply-state metadata records the last caller, last status, reason, thread id, and sent timestamp
- artifact fallback resolution uses the same submit attempt as the reply-state file, so reapply attempts do not accidentally read the default `submit/` bucket
- worker post-submit now distinguishes `sent` from `skipped_duplicate` and logs `email_reply_skipped_duplicate` explicitly

Worker post-submit now uses the structured result and logs `email_reply_skipped_duplicate` distinctly instead of silently doing nothing.

Late reconciliation now backfills the same reply once when a real confirmation is only discovered afterward from disk. If `sync_job_from_disk()` finds `application_confirmation_email.json` for the preferred confirmed submit bucket and that bucket still has no `confirmation_email_reply.json`, it calls the same `send_confirmation_email_reply(...)` helper with `caller="disk_sync"` instead of leaving the confirmation unreplied forever.

## Why This Works

The system had two valid runtime paths reaching the same side effect. The missing piece was durable state anchored to the real object identity of that side effect: the confirmed submit attempt. Once the reply state moved to the submit attempt itself, both the board-local sender and the worker fallback could make the same decision without depending on fragile ambient state like the current active pointer, a Gmail thread id, or a jobs row.

This also preserves reapply behavior. A new confirmed `submit-*` attempt gets its own `confirmation_email_reply.json`, so a previous successful reply under `submit/` does not suppress a later explicit reapply.

## Prevention

- Any future post-submit side effect that can run from both the submitter and the worker should persist state in the submit attempt that owns the work, not in `jobs.db` or only in process memory
- When reapply flows reset the active submit pointer after confirmation, post-submit helpers must resolve the latest confirmed submit attempt rather than defaulting to `role_submit_dir(...)`
- Worker reconciliation should log duplicate skips explicitly so repeated sends are visible as intentional suppression, not silent behavior
- Keep regression tests for:
  - first send writes reply-state metadata
  - second call for the same attempt returns `skipped_duplicate`
  - `not_sent` remains retryable
  - a prior send in `submit/` does not suppress a new confirmed `submit-*` reapply attempt
  - late `application_confirmation_email.json` discovery backfills the missing reply once when reply-state is absent

## Investigation Steps

1. Checked Gmail via `gws` and confirmed Harvey and Alchemy each had one employer confirmation and two self-replies
2. Traced the first send to board submit flows and the second send to `pipeline_orchestrator._post_submit(...)`
3. Confirmed `record_website_confirmation(...)` resets the active submit pointer after a confirmed reapply submit
4. Extracted the shared confirmed-submit resolution before adding the reply-state artifact so Notion sync, resume post-submit, and email reply all used the same boundary
5. Extended disk reconciliation so a submit discovered later from `application_confirmation_email.json` can still send the missing self-reply once instead of relying on the original worker post-submit hook to have run

## Cross-References

- Brainstorm: `docs/brainstorms/2026-03-26-confirmation-email-reply-idempotency-requirements.md`
- Plan: `docs/plans/2026-03-26-006-fix-confirmation-email-reply-idempotency-plan.md`
- Related dedupe boundary learning: `docs/solutions/database-issues/cross-source-duplicate-jobs.md`
- Related single-source-of-truth learning: `docs/solutions/integration-issues/adding-new-llm-provider.md`
- Related shared-helper extraction learning: `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`
- GitHub issues: no related issues found via `gh issue list --search "confirmation email duplicate reply submit attempt gmail" --state all --limit 5`
