---
date: 2026-03-26
topic: answer-regeneration-proof
---

# Explicit Answer Regeneration Must Produce Fresh Artifacts and Visible Proof

## Problem Frame

The product currently exposes multiple user actions that imply application answers will be regenerated, including answer-only regeneration and full draft regeneration. In practice, those flows can return the job to `draft` while still showing an older `application_answers.json` payload and older raw provider output.

That creates a trust failure:
- The user believes they asked for fresh answers, but the product can silently reuse stale answer caches.
- Prompt or policy changes, such as updated prose rules, may never reach the visible draft even after the user explicitly regenerates.
- The UI provides no durable proof that a fresh answer-generation run actually happened.
- A proof surface that hides the draft itself also fails the trust goal, because the user cannot review answers, resumes, or screenshots while keeping the proof in view.

For this workflow, "regenerate answers" must mean a real fresh answer-generation attempt, not a best-effort redraw of the existing draft.

## Requirements

- R1. Define a shared product contract for **answer-affecting regeneration**. This includes any user-triggered action across web, CLI, TUI, worker-assisted draft flows, or direct LLM-assisted draft flows whose semantics imply application answers may change, including answer-only regeneration and full draft regeneration.
- R2. Every answer-affecting regeneration shall bypass reusable generated-answer caches for that run. This includes the active `submit/application_answers.json` cache and any older matching answer cache that would otherwise be copied forward from a previous submit attempt.
- R3. A successful answer-affecting regeneration shall require a fresh answer-generation execution for generated application-answer fields. Reusing existing answer artifacts without a fresh generation attempt does not satisfy the regenerate contract.
- R4. Full draft regeneration shall invalidate generated-answer caches even when the question list is unchanged, because prompt text, answer policy, source content, or other answer inputs may have changed.
- R5. Successful fresh answer generation shall rewrite the answer artifacts for that run, including the structured answer cache and raw provider output artifact, with fresh metadata proving recency. This must hold even if the newly generated prose happens to be textually identical to the previous answer.
- R6. The draft review UI shall expose visible freshness proof for generated answers. At minimum, the user must be able to see that the current draft answers came from a fresh generation run, including when that run happened and which provider produced the answers.
- R7. If an answer-affecting regeneration does not produce fresh answer proof, the product shall not present the outcome as a successful regenerated draft. It must fail visibly rather than silently returning to `draft` with stale answer artifacts.
- R8. If the current application has no generated answer fields to regenerate, the product shall say so explicitly instead of reporting a successful answer regeneration.
- R9. Freshness proof must be backed by durable system state rather than transient UI assumptions. The proof shown in the UI must derive from persisted answer artifacts, persisted job metadata, persisted event/provider-run state, or an equivalent durable source of truth.
- R10. This regeneration contract applies globally across supported boards and runtime surfaces. Greenhouse's parallel answer-generation path and the shared `generate_application_answers()` path must both satisfy the same product behavior.
- R11. In the web job-detail view, answer freshness proof shall render as a shared helper card below the sticky dock in normal flow and remain visible from every job-detail tab.
- R12. When board URL, error, progress, and answer freshness proof are all present in the web job-detail view, the answer freshness proof card shall appear after board URL, error, and progress.
- R13. The shared helper card below the dock shall be the canonical web proof surface. The web UI shall not depend on sticky-dock placement or an Answers-tab-only rendering to satisfy the visible-proof requirement.
- R14. Visible freshness proof shall confirm recency without obscuring the active tab's primary content.

## Success Criteria

- Triggering `Regenerate Answers` for a draft with generated answer fields produces newly written answer artifacts for that run instead of reusing an older March 22-style cache with unchanged freshness metadata.
- Triggering a full draft regeneration after an answer-policy or prompt change produces fresh answer artifacts and visible proof that the answers were regenerated, even when the question list is unchanged.
- If a regeneration attempt fails before fresh answer proof exists, the user sees a failure state rather than a silently refreshed draft backed by stale answer artifacts.
- If a draft has no generated answer fields, the product reports that there was nothing to regenerate instead of implying a fresh answer run occurred.
- The same regeneration expectations hold across web, CLI, TUI, worker-driven flows, and direct LLM-driven draft review flows.
- In the web job-detail view, the user can see freshness proof while reviewing Resume, Cover Letter, Screenshot, or Answers without switching to a dedicated proof tab or losing the start of the active tab's content.
- The proof surface never expands into a sticky or viewport-dominating panel that hides the draft materials it is supposed to contextualize.

## Scope Boundaries

- NOT disabling answer-cache reuse for ordinary non-regenerate retries where the user did not explicitly request fresh answers.
- NOT requiring regenerated answers to differ textually from the prior version. Fresh execution and fresh proof are required; textual differences are not.
- NOT automatically rewriting legacy answer artifacts in place without a regeneration action.
- NOT changing the source-of-truth rule that live draft screenshots and confirmed live form state determine whether autofill results are acceptable.
- NOT redefining resume or cover-letter regeneration behavior beyond the requirement that full draft regeneration must also refresh generated answers.
- NOT making answer freshness proof part of the sticky dock.
- NOT limiting visible proof to the Answers tab in the web UI.

## Key Decisions

- Explicit answer regeneration is a hard product contract, not a best-effort hint.
- Full regenerate and answer-only regenerate both count as answer-affecting flows and must force fresh answer generation.
- Freshness proof requires both backend durability and UI visibility; backend-only proof is insufficient for user trust.
- Matching old answer text is acceptable only if it was produced by a fresh run with new proof.
- Silent fallback to stale answer artifacts is worse than a visible failure because it misleads the user about what changed.
- In the web job-detail view, answer freshness proof belongs in a shared non-sticky helper card below the dock on every tab.
- The web proof surface should be singular and canonical rather than split between sticky chrome and tab-local duplicates.
- The proof surface must support review work, not interrupt it.

## Dependencies / Assumptions

- Generated answer artifacts already have durable storage locations and can carry freshness metadata.
- The product already records enough job-level or provider-level state that planning can choose a durable proof source without inventing a separate ephemeral cache.
- Prompt and policy changes, including prose rules such as em-dash avoidance, are legitimate reasons a full regenerate should refresh answers even when the question specs are unchanged.

## Alternatives Considered

- Strengthen prompts only: rejected because the main failure is stale cache reuse, not only weak prose guidance.
- Fix only the web `Regenerate Answers` button: rejected because full regenerate and other runtime surfaces also imply fresh answers.
- Backend proof only: rejected because the user needs visible confirmation that a fresh answer run occurred.
- Best-effort regeneration with silent cache reuse when outputs look similar: rejected because it preserves the trust failure that triggered this brainstorm.
- Keeping proof inside the sticky dock: rejected because it risks turning proof into a blocking layout surface rather than lightweight confirmation.
- Showing proof only in the Answers tab: rejected because the user may need freshness context while reviewing other draft materials.
- Split proof surfaces with a shared summary plus a separate full Answers-tab card: rejected because it adds duplicate states without improving trust enough to justify the extra complexity.

## Outstanding Questions

### Resolve Before Planning

- None.

### Deferred to Planning

- What exact persisted fields or events should be the authoritative freshness proof shown in the UI?
- Where should answer-cache invalidation be enforced so all answer-affecting regeneration entrypoints inherit it without drift?
- How should the product distinguish "fresh run succeeded with identical text" from "no fresh run happened" in both backend state and UI presentation?
- What is the cleanest no-generated-answers UX across web, CLI, and TUI surfaces?

## Next Steps

-> `/prompts:ce-plan` for a global implementation plan covering cache invalidation, durable freshness proof, and UI surfacing across all answer-affecting regeneration paths
