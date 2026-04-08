---
title: "fix: Prevent duplicate confirmation email replies"
type: fix
status: active
date: 2026-03-26
origin: docs/brainstorms/2026-03-26-confirmation-email-reply-idempotency-requirements.md
---

# fix: Prevent duplicate confirmation email replies

## Overview

Make confirmation-email replies idempotent per submit attempt so one successful application produces at most one self-reply with the autofill report, even when both the board submit flow and the worker post-submit flow touch the same role. The fix should preserve the feature, preserve reapply behavior, and keep worker logs explicit about when a later call was intentionally skipped as a duplicate.

## Problem Frame

Recent Harvey and Alchemy runs produced one real confirmation email plus two self-replies in the same Gmail thread. Repo inspection and Gmail logs show that this is an internal duplicate-send bug, not a duplicate employer-email bug: submit flows already call `reply_to_confirmation_email(...)`, and `pipeline_orchestrator._post_submit(...)` later calls it again with the same `output_dir`. The origin requirements document defines the desired boundary clearly: send at most once per submit attempt, across CLI, TUI, web, worker, and board-local flows, while still allowing explicit reapply attempts to send their own reply (see origin: `docs/brainstorms/2026-03-26-confirmation-email-reply-idempotency-requirements.md`).

One extra repo-specific constraint materially shapes the design: after a confirmed reapply submit, `record_website_confirmation(...)` writes artifacts into the active `submit-*` directory and then resets the active pointer back to `submit`. The reply idempotency state therefore cannot blindly follow the current active pointer. It must resolve the same confirmed submit attempt that Notion sync and post-submit resume logic already prefer.

## Requirements Trace

- R1. Automatic confirmation-email replies must send at most once per submit attempt.
- R2. The idempotency boundary must be the active/confirmed submit attempt for a role, not the Gmail thread alone and not the `jobs.db` row alone.
- R3. Explicit reapply attempts that use a fresh `submit-*` directory must still be allowed to send their own reply.
- R4. CLI, TUI, web, worker, and board-specific submit paths must honor the same dedupe state.
- R5. Duplicate skips must be recorded as intentional outcomes, not silent no-ops.
- R6. Reply state must preserve diagnostic metadata explaining what was sent or skipped.
- R7. Post-submit reruns must continue email/Notion reconciliation without generating a second reply for the same submit attempt.

## Scope Boundaries

- Keep the reply-to-self confirmation-email feature.
- Do not add a new `jobs` table column or make `jobs.db` the source of truth for dedupe.
- Do not remove the worker post-submit fallback path; it remains useful when the first automatic send did not succeed.
- Do not design a manual resend UI or CLI flag in this fix.
- Do not broaden the change into a general post-submit state-machine rewrite.

## Context & Research

### Relevant Code and Patterns

- `scripts/application_submit_common.py`
  Owns `sync_notion_after_submit(...)`, Gmail confirmation search, artifact fallback resolution, and `reply_to_confirmation_email(...)`. This is the natural home for reply-idempotency state because every automatic send path already funnels through it.
- `scripts/pipeline_orchestrator.py`
  `_post_submit(...)` currently performs a second unconditional reply attempt after the submit phase succeeds and logs `email_reply_sent` when the helper returns truthy.
- `scripts/autofill_pipeline.py`
  Shared browser submit path already sends the reply after confirmation detection, which is the first half of the observed duplicate.
- `scripts/autofill_greenhouse.py`
  Greenhouse’s custom submit path also sends the reply directly inside `_sync_notion_after_submit(...)`, so the fix must cover both shared and board-local flows.
- `scripts/notion_job_applications.py`
  Already contains the repo’s preferred semantics for “latest confirmed submit attempt,” including `_latest_confirmed_submit_dir(...)`, `_preferred_submit_dir_name_for_sync(...)`, and the pointer reset after `record_website_confirmation(...)`.
- `scripts/submit_application.py`
  Duplicates the same “latest confirmed submit attempt” selection in `_latest_confirmed_submit_dir(...)` and `_resume_post_submit_sync(...)`. This confirms the pattern is real and should not be copied a third time.
- `scripts/output_layout.py`
  Already owns active submit directory selection (`submit` vs `submit-*`) and is the right shared home for submit-attempt resolution helpers and the new reply-status artifact constant/pattern registration.

### Institutional Learnings

- `docs/solutions/database-issues/cross-source-duplicate-jobs.md`
  Relevant pattern: use a domain-specific dedupe boundary that matches the real object identity, and keep DB logs/rows as observability rather than authority.
- `docs/solutions/integration-issues/adding-new-llm-provider.md`
  Relevant pattern: avoid multi-layer state drift by centralizing the source of truth instead of relying on parallel Python/DB/shell copies.
- `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`
  Relevant pattern: when the same logic is about to be implemented a third time, extract a shared helper instead of adding another copy.

### External References

- None. Local patterns are already strong enough for this fix.

## Key Technical Decisions

- **Use a dedicated submit-attempt reply-state artifact**: Store reply state in a new JSON artifact under the resolved submit attempt, not inside `notion_sync_status.json` or `application_confirmation_email.json`. Those files represent different concerns: Notion reconciliation and inbound employer email.
- **Resolve the submit attempt with the same confirmed-attempt semantics used by Notion sync**: The helper should prefer the latest confirmed submit attempt when the active pointer has already been reset, so reapply attempts keep their own reply state and artifacts.
- **Suppress only on `sent`, not on any prior attempt**: Failed, missing-thread, or missing-artifact outcomes should remain retryable. The dedupe bit should be “a reply was already sent for this submit attempt,” not “a reply was previously attempted.”
- **Add a structured reply result alongside the existing boolean wrapper**: Introduce a richer internal result shape so worker code can log `email_reply_skipped_duplicate` distinctly, while preserving a simple boolean wrapper for the many board call sites that do not need structured handling.
- **Keep DB events as secondary observability**: The artifact is authoritative; worker event logging is additive and should not determine dedupe behavior.
- **Avoid a broader concurrency primitive unless implementation proves it necessary**: The observed duplicate paths are sequential (`submit` then `post_submit`). A sent-state artifact is sufficient for the primary fix. If implementation reveals truly concurrent callers, a lightweight per-attempt lock file can be added as a follow-up refinement.

## Open Questions

### Resolved During Planning

- **Where should reply dedupe state live?**
  In a dedicated submit-attempt artifact, not in Notion sync status or the DB. This keeps concerns separated and works across all runtime surfaces.
- **What should the dedupe key be?**
  The resolved confirmed submit attempt (`submit/` or a specific `submit-*` directory), not the role-wide `output_dir` and not the Gmail thread alone.
- **What should suppress a later send?**
  Only a recorded successful send for that submit attempt. Non-success outcomes remain retryable.
- **How should worker logs distinguish a duplicate?**
  By switching worker post-submit to the structured helper result and logging a dedicated duplicate-skip event when appropriate.

### Deferred to Implementation

- **Exact artifact filename and field names**
  The plan assumes a dedicated JSON artifact such as `confirmation_email_reply.json`; the implementer can settle the final constant name while keeping the semantics intact.
- **How much send-attempt metadata to persist**
  The minimum should include `sent`, `sent_at_utc`, `thread_id`, `caller`, and the last outcome. Extra fields like `message_id`, `subject`, or artifact paths can be added if low-cost.
- **Whether a post-crash stale in-progress marker is needed**
  Not required for the primary sequential duplicate fix; only add if code-level review uncovers genuine overlapping callers.

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```text
automatic caller
  -> send_confirmation_email_reply(payload, board_name, caller=...)
       -> resolve_reply_submit_dir(out_dir)
          - prefer active submit dir when explicitly set
          - otherwise prefer latest confirmed submit attempt
       -> load reply-state artifact from that submit dir
       -> if sent == true:
            return {status: "skipped_duplicate", ...}
       -> resolve artifacts + thread id
       -> if send succeeds:
            write reply-state artifact {sent: true, sent_at_utc, thread_id, caller, ...}
            return {status: "sent", ...}
       -> if send does not happen:
            update last-attempt metadata without setting sent=true
            return {status: "not_sent", reason: ...}

worker post_submit
  -> uses structured result
  -> logs sent vs skipped_duplicate distinctly

board/local callers
  -> keep boolean wrapper
  -> still benefit from the same submit-attempt-scoped dedupe
```

## Implementation Units

- [ ] **Unit 1: Share confirmed submit-attempt resolution**

**Goal:** Create one shared way to resolve the submit attempt that owns post-submit artifacts after confirmation, including reapply cases where the active pointer has been reset.

**Requirements:** R2, R3, R4, R7

**Dependencies:** None

**Files:**
- Modify: `scripts/output_layout.py`
- Modify: `scripts/notion_job_applications.py`
- Modify: `scripts/submit_application.py`
- Test: `tests/test_notion_sync.py`
- Test: `tests/test_submit_application.py`

**Approach:**
- Extract the duplicated “latest confirmed submit attempt” logic from `notion_job_applications.py` and `submit_application.py` into a shared helper in `output_layout.py` or an adjacent shared utility module that already owns submit-dir selection.
- Preserve current semantics:
  - active env/pointer overrides still win when explicitly set for an in-progress attempt;
  - absent an explicit override, post-submit flows can still recover the latest confirmed `submit-*` directory after the pointer resets to default `submit`.
- Update existing Notion-sync and submit-application callers to use the shared helper so the reply fix does not introduce a third copy of this logic.

**Patterns to follow:**
- `scripts/notion_job_applications.py` `_latest_confirmed_submit_dir(...)` and `_preferred_submit_dir_name_for_sync(...)`
- `scripts/submit_application.py` `_latest_confirmed_submit_dir(...)` and `_resume_post_submit_sync(...)`

**Test scenarios:**
- Latest confirmed `submit-*` attempt is chosen over default `submit/` after pointer reset.
- Default/in-progress active submit attempt still wins when explicitly set for a reapply in progress.
- Existing Notion-sync and submit-application resume behavior remains unchanged for non-reapply roles.

**Verification:**
- Reapply-aware tests still show that post-submit resume logic selects the latest confirmed submit attempt.
- The repo no longer has separate “latest confirmed submit dir” implementations in both Notion sync and submit-application code paths.

- [ ] **Unit 2: Add submit-attempt-scoped reply idempotency state**

**Goal:** Make the reply helper authoritative for “send at most once per submit attempt” and record durable reply metadata in the submit attempt that actually owns the submission.

**Requirements:** R1, R2, R3, R4, R5, R6, R7

**Dependencies:** Unit 1

**Files:**
- Modify: `scripts/application_submit_common.py`
- Modify: `scripts/output_layout.py`
- Modify: `docs/output-structure.md`
- Test: `tests/test_submit_application.py`

**Approach:**
- Introduce a dedicated JSON artifact in the resolved submit attempt for reply state.
- Add small helper functions in `application_submit_common.py` to:
  - resolve the reply submit dir using the shared confirmed-attempt logic from Unit 1;
  - read/write reply-state metadata;
  - return a structured result for send attempts.
- Keep a boolean compatibility wrapper around the structured helper so existing board call sites do not all need to change at once.
- Suppress future automatic sends only when the artifact records a successful prior send for that submit attempt.
- Persist enough metadata to diagnose later behavior, with `sent` as the suppression bit and last-attempt metadata for non-sent outcomes.
- Resolve report/screenshot artifacts against the same submit attempt so reapply flows do not accidentally read from the default `submit/` bucket after pointer reset.

**Execution note:** Start with characterization-style tests around the helper’s current behavior for minimal payloads and reapply attempts before changing the contract.

**Patterns to follow:**
- JSON artifact write/read style in `scripts/notion_job_applications.py`
- Artifact registration and submit-dir helpers in `scripts/output_layout.py`
- Existing minimal-payload fallback behavior in `scripts/application_submit_common.py`

**Test scenarios:**
- First successful send writes reply-state metadata and returns `sent`.
- Second call for the same submit attempt returns `skipped_duplicate` without issuing the Gmail send call.
- A prior `not_sent`/missing-thread outcome remains retryable and does not suppress a later successful send.
- A previously sent reply in `submit/` does not suppress a new explicit reapply attempt in a distinct confirmed `submit-*` directory.
- Minimal payload invocation still works when company and artifact paths must be derived from disk.

**Verification:**
- One send call occurs per submit attempt in unit tests even when the helper is invoked twice.
- Reapply-specific tests prove that idempotency state follows the confirmed submit attempt, not whichever submit bucket is currently active by default.
- `docs/output-structure.md` documents the new reply-state artifact in the submit bucket.

- [ ] **Unit 3: Wire worker observability and preserve cross-surface behavior**

**Goal:** Keep all existing automatic callers using the same idempotent helper while making worker logs explicit about duplicate skips.

**Requirements:** R1, R4, R5, R6, R7

**Dependencies:** Unit 2

**Files:**
- Modify: `scripts/pipeline_orchestrator.py`
- Modify: `docs/autofill-patterns.md`
- Test: `tests/test_pipeline_orchestrator.py`

**Approach:**
- Switch `_post_submit(...)` to use the structured reply result instead of a bare boolean.
- Preserve best-effort semantics:
  - `sent` still logs `email_reply_sent`;
  - duplicate suppression logs a dedicated duplicate-skip event (for example `email_reply_skipped_duplicate`);
  - non-sent/non-failed outcomes remain non-fatal and do not regress the existing worker completion behavior.
- Leave board-local submitters on the compatibility wrapper so the cross-surface feature stays centralized in the helper rather than spawning more board-specific branches.
- Update `docs/autofill-patterns.md` to state the new contract explicitly: automatic confirmation-email replies happen at most once per submit attempt, even if more than one runtime path reaches post-submit reconciliation.

**Patterns to follow:**
- Event logging style in `scripts/pipeline_orchestrator.py`
- Existing best-effort post-submit handling in `_post_submit(...)`
- Existing docs language around submit idempotency and reapply attempts in `docs/autofill-patterns.md`

**Test scenarios:**
- Worker post-submit logs `email_reply_sent` when the structured helper reports `sent`.
- Worker post-submit logs a duplicate-skip event when the helper reports `skipped_duplicate`.
- Worker post-submit does not fail the job when the helper reports a retryable non-send outcome.

**Verification:**
- Pipeline-orchestrator tests show distinct event behavior for sent vs duplicate-skipped outcomes.
- The documented submit pattern now states the at-most-once-per-submit-attempt rule alongside the existing reapply/idempotency rules.

## System-Wide Impact

- **Interaction graph:** Board submitters, the shared browser pipeline, Greenhouse’s custom submit path, and worker `_post_submit(...)` all continue to flow through the same reply helper. The change centralizes behavior instead of creating new board-local branches.
- **Error propagation:** Reply sending remains best-effort. The new artifact and structured result improve observability without making send failures fatal to submission success.
- **State lifecycle risks:** The biggest lifecycle risk is writing reply state into the wrong submit bucket after `record_website_confirmation(...)` resets the active pointer. Unit 1 exists specifically to prevent that regression. A secondary risk is over-suppressing future sends after a failed attempt; the design avoids that by suppressing only on `sent`.
- **API surface parity:** All automatic surfaces keep using the same helper contract. Worker code gains structured handling; other callers can remain on the boolean wrapper until a later cleanup.
- **Integration coverage:** Unit tests must cover both shared-pipeline and worker-post-submit semantics indirectly through the helper and worker logging. Reapply flows need explicit regression coverage because they are the easiest place to mis-anchor the dedupe state.

## Risks & Dependencies

- If submit-attempt resolution is not shared, the fix will likely reintroduce drift between Notion sync, submit-application resume logic, and reply idempotency.
- If the helper stores state only under the default `submit/` directory, explicit reapply attempts will either suppress valid new replies or miss their artifacts.
- If non-success outcomes are treated as duplicates, later fallback sends from worker post-submit will stop working.
- If implementation reveals truly concurrent reply callers, a lightweight lock file may need to be added to the same submit attempt as a follow-up refinement.

## Documentation / Operational Notes

- Add the reply-state artifact to the documented submit output structure.
- Update submit-pattern guidance to make the at-most-once rule explicit next to the existing website/email/Notion reconciliation rules.
- No DB migration or worker-control UI change is required for this fix.

## Sources & References

- **Origin document:** `docs/brainstorms/2026-03-26-confirmation-email-reply-idempotency-requirements.md`
- Related plan: `docs/plans/2026-03-24-008-fix-confirmation-email-reply-not-sent-plan.md`
- Related code:
  - `scripts/application_submit_common.py`
  - `scripts/pipeline_orchestrator.py`
  - `scripts/notion_job_applications.py`
  - `scripts/submit_application.py`
  - `scripts/output_layout.py`
- Related tests:
  - `tests/test_submit_application.py`
  - `tests/test_notion_sync.py`
  - `tests/test_pipeline_orchestrator.py`
