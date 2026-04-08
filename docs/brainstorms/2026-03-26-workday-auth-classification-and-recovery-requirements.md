---
date: 2026-03-26
topic: workday-auth-classification-and-recovery
---

# Workday Auth Must Distinguish Credential Failures from Recoverable Gateway States

## Problem Frame

The current Workday flow collapses too many different gateway states into a single `auth_failed` outcome. In practice, a normal `Create Account / Sign In` screen, a password-reset screen, a maintenance page, and a real credential rejection can all stop the job with the same generic message.

That creates two product failures:
- The user cannot tell whether a stopped Workday draft was caused by a bad password, a recoverable site state, or a Workday outage.
- The existing protective auth guard can block later Workday jobs for the wrong reason, because it treats all of those states as the same failure and keys them across the whole Workday board.

Recent Workday stops showed a more specific pattern: several jobs landed on the normal `Create Account / Sign In` gateway and were recorded as `auth_failed`, then later Workday jobs were skipped by the 24-hour auth guard. That is not good enough for diagnosis or recovery.

For Workday, the product should optimize for the most likely recovery path first: sign in with known credentials, then reset password if needed, then create an account as a final fallback. Only explicit credential rejection should count as a true auth failure for guard purposes.

## Requirements

- R1. Every Workday auth stop, skip, or retry decision shall be classified into a more specific auth state rather than flattened into a single generic `auth_failed` bucket.
- R2. The Workday auth state model shall distinguish at least these product-relevant categories: explicit credential rejection, maintenance or service interruption, password-reset flow, create-account flow, authenticated success, and unknown auth state.
- R3. Only explicit credential rejection shall be treated as a true Workday auth failure for protective lockout behavior and durable `auth_failed` accounting.
- R4. Landing on a normal Workday auth gateway such as `Create Account / Sign In`, `Forgot Password`, or a reset-password screen without explicit credential rejection shall not be recorded as a true `auth_failed` result.
- R5. The canonical Workday recovery order shall be: sign in with configured credentials, then password reset via `gws`, then create account as the final fallback, then email verification if the create-account flow requires it.
- R6. If the product completes the full Workday recovery order and still does not reach the application flow, and no explicit credential rejection was observed, it shall stop as an unknown auth state rather than a true auth failure.
- R7. Unknown Workday auth states shall preserve rich evidence for later diagnosis, including the last attempted recovery step, page URL, visible heading, visible auth calls to action, visible alert or error text, and debug artifacts sufficient to reconstruct what the browser saw.
- R8. Explicit Workday maintenance or service-interruption states shall be treated as transient. They shall auto-retry with backoff and shall not count toward the Workday auth-failure guard.
- R9. The protective Workday auth-failure guard shall be scoped to the Workday tenant or site, not to the entire `workday` board across unrelated employers.
- R10. The protective Workday auth-failure guard shall trigger only after repeated explicit credential rejections on the same Workday tenant within the rolling guard window. The current product threshold shall remain three failures in twenty-four hours unless a later brainstorm changes that contract.
- R11. A Workday job stopped by a tenant-scoped auth guard shall say so explicitly, including which tenant was guarded and that the skip was triggered by repeated credential rejections on that tenant.
- R12. User-visible status and error surfaces across worker, web, TUI, CLI, and saved submit artifacts shall expose the more specific Workday auth reason instead of a generic “authentication failed” message whenever the system has better evidence.
- R13. If the Workday flow reaches an authenticated non-form destination such as a candidate home or user-home state, the product shall treat that as a recoverable intermediate state rather than immediately labeling the run as auth failed.
- R14. The Workday auth-recovery contract shall apply consistently to draft generation, answer refreshes that re-enter Workday submission, and any other product flow that executes the Workday browser path.

## Success Criteria

- A Workday run that stops on the plain `Create Account / Sign In` gateway without explicit rejection is recorded as a gateway or unknown auth state, not as a true credential failure.
- A Workday maintenance page is retried automatically and does not poison later jobs on other Workday tenants.
- Repeated credential rejection on one Workday tenant can pause that tenant safely without blocking unrelated Workday employers.
- A stopped Workday row in the queue and job detail makes it clear whether the issue was credential rejection, maintenance, unknown auth state, or a tenant guard skip.
- The saved Workday auth artifact contains enough evidence to answer “what happened?” without having to infer the answer from a generic message or rerun the job blindly.
- The Workday auth flow tries sign in first, then password reset, then create account, matching the intended product policy.

## Scope Boundaries

- NOT redesigning the full auth taxonomy for every job board in this brainstorm. This work is specifically about Workday, though planning may extract shared infrastructure if it is low-cost.
- NOT removing the protective auth guard entirely.
- NOT requiring manual review as the first response to routine Workday maintenance or service interruption.
- NOT changing the broader product rule that draft mode stops before final submission.
- NOT introducing a new per-employer credential-management product outside the existing configured email and password plus `gws`-driven recovery flows.
- NOT treating every ambiguous Workday auth landing as transient; ambiguous states should stop with evidence after the defined recovery order is exhausted.

## Key Decisions

- Workday needs a specific auth-state model, not one generic auth-failure label.
- The preferred recovery order is sign in, then password reset, then create account.
- Unknown auth states should fail with evidence rather than masquerading as true credential failures.
- Explicit maintenance and service interruption states are transient and should auto-retry.
- Protective auth guards should be tenant-scoped, not board-wide across all Workday jobs.
- Only explicit credential rejection should contribute to Workday auth guard counts.

## Dependencies / Assumptions

- The product can derive a stable Workday tenant or site identity from Workday URLs and redirect targets.
- `gws` remains the supported mechanism for password-reset and verification-email retrieval.
- Existing debug artifacts and event logging can be extended to preserve richer auth-state evidence without inventing a separate manual investigation workflow.

## Alternatives Considered

- Keep one generic `auth_failed` label for all Workday gateway problems: rejected because it obscures root cause and causes false guard triggers.
- Keep the guard board-wide across all Workday jobs: rejected because Workday auth scope is tenant-specific and unrelated employers should not block each other.
- Prefer create account before sign in: rejected because it increases avoidable friction when an account already exists and conflicts with the desired recovery order.
- Stop maintenance pages for manual review: rejected because they are transient and should not consume user attention first.
- Treat unknown auth states as true credential failures: rejected because it pollutes the guard signal and prevents accurate diagnosis.

## Outstanding Questions

### Resolve Before Planning

- None.

### Deferred to Planning

- What exact persisted failure-type and auth-state taxonomy should the system use so Workday can distinguish explicit credential failure, maintenance, and unknown auth state cleanly across DB rows, events, and saved submit artifacts?
- What is the cleanest tenant-key derivation for both `myworkdayjobs.com` and `myworkdaysite.com` URLs, including login and redirect URLs?
- Where should the richer Workday auth evidence be captured so queue surfaces, job detail, and future diagnostics all read from the same durable source of truth?
- How should authenticated-but-not-on-form states such as candidate home be resumed back into the application flow without creating brittle loops?
- Is there a low-cost shared abstraction worth extracting so Workday and future auth-gated boards do not reintroduce the same generic-failure problem?

## Next Steps

-> `/prompts:ce-plan` for a structured implementation plan covering Workday auth-state classification, tenant-scoped guarding, recovery-order cleanup, and richer evidence capture
