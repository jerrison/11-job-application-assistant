---
date: 2026-03-26
topic: confirmation-email-reply-idempotency
---

# Confirmation Email Reply Idempotency

## Problem Frame

After a successful application submit, the system replies to the employer's confirmation thread with the autofill report, screenshot, and generated documents. For recent Harvey and Alchemy applications, Gmail shows one real confirmation email plus two self-replies from `jerrisonli@gmail.com` for the same application. The repo and logs indicate this is not a duplicate employer email problem. It is a duplicate internal reply problem caused by more than one runtime path performing the same post-submit reply.

The highest-leverage fix is to make confirmation-email replies idempotent across all runtime surfaces without removing the feature entirely.

## Requirements

- R1. Automatic confirmation-email replies must send at most once per submit attempt.
- R2. The idempotency boundary must be the active submit attempt for a role, not the Gmail thread alone and not the `jobs.db` row alone.
- R3. A distinct explicit reapply attempt must be allowed to send its own reply even when it reuses the same role `output_dir`.
- R4. CLI, TUI, web, worker, and board-specific submit paths must honor the same dedupe state.
- R5. When a reply is skipped because the current submit attempt already sent one, that skip must be recorded as an intentional dedupe outcome rather than a silent no-op.
- R6. The dedupe record must preserve enough metadata to explain later what was sent and what was skipped.
- R7. Reruns that resume post-submit reconciliation must continue checking email and Notion state without generating a second confirmation-thread reply for the same submit attempt.

## Success Criteria

- A single application confirmation thread contains at most one self-reply with the autofill report for that submit attempt.
- Worker post-submit retries and resumptions do not add extra self-replies after the board submit flow already sent one.
- Explicit reapply flows that create a fresh `submit-*` attempt directory can still send a new reply for the new attempt.
- Operators can tell from on-disk artifacts or logs whether the reply was sent, skipped as duplicate, or never attempted.

## Scope Boundaries

- Keep the confirmation-email reply feature; do not remove it entirely.
- Do not make `jobs.db` the only source of truth for reply dedupe.
- Do not treat separate explicit reapply attempts as duplicates of each other.
- Do not require Gmail thread discovery to happen before dedupe can work.
- Do not design a manual resend UI as part of this fix.

## Key Decisions

- **Submit attempt is the source of truth**: The repo already models reapply as a fresh active `submit-*` directory under a shared role `output_dir`, so the durable identity is the submit attempt, not the role folder in the abstract.
- **`output_dir` is the right cross-surface anchor, refined to the active submit attempt**: Every runtime path already has `out_dir`, while `job_id` is worker-specific and `threadId` may only be discovered inside the reply helper.
- **Gmail thread metadata is supporting evidence, not the primary key**: Store and reuse it when available, but do not base correctness solely on whether the thread was already discovered.
- **DB event logs are observability, not authority**: Worker logs are useful for diagnosing duplicates, but reply suppression must still work in non-worker and board-local submit flows.

## Dependencies / Assumptions

- Each submit run has one active submit artifact bucket (`submit/` or `submit-*`) that all participating runtime paths can read.
- Existing rerun behavior already resumes post-submit work from on-disk artifacts rather than requiring a fresh live submission.
- The current duplicate cases are caused by two automatic runtime paths operating on the same submit attempt artifacts.

## Outstanding Questions

### Resolve Before Planning

- None.

### Deferred to Planning

- [Affects R1][Technical] Should the reply state live in a dedicated submit-attempt JSON artifact or inside an existing post-submit status artifact?
- [Affects R5][Technical] What exact dedupe outcome vocabulary should be used in logs and artifacts (`sent`, `skipped_duplicate`, `failed`, etc.)?
- [Affects R6][Technical] Which metadata should be persisted for diagnostics: `sent_at_utc`, `thread_id`, `message_id`, caller, board, and artifact paths?
- [Affects R7][Technical] Whether an explicit force-resend escape hatch should exist now or be deferred.

## Next Steps

→ /prompts:ce-plan for structured implementation planning
