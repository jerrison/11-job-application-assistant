---
date: 2026-03-25
topic: global-positive-fit-screening-yes-policy
---

# Global Positive-Fit Screening Questions Default to "Yes"

## Problem Frame

The current autofill behavior mixes shared classifier logic, profile-driven answers, and board-specific yes/no handling. As a result, positive-fit screening questions such as hybrid willingness, commute/alignment, relocation, travel, product usage, and claimed relevant experience can still be left blank, answered inconsistently, or handled differently by board.

This creates two problems:
- Required yes/no-style screening fields can block draft readiness entirely.
- Conservative or inconsistent handling reduces interview chances for questions the candidate explicitly wants optimized toward `Yes`.

The user preference for this workflow is now explicit: across all supported boards and all runtime surfaces, positive-fit screening questions should default to `Yes` to maximize interview chances, with a narrower exception for degree/license/certification questions that must be backed by explicit candidate data before they can be answered `Yes`.

## Requirements

- R1. Define a shared cross-board category for positive-fit screening questions. This category includes discrete yes/no-style questions about willingness, comfort, alignment, availability, or favorable self-claims that improve fit, including hybrid/on-site attendance, commute, relocation, travel, product usage, general experience/background, and minimum-years experience.
- R2. Across all supported job boards and all runtime surfaces (CLI, TUI, worker, web, direct LLM runs), discrete positive-fit screening questions shall default to `Yes`.
- R3. The policy shall apply regardless of control type, including radio groups, native selects, custom dropdowns, button groups, and equivalent discrete widgets. It shall not collapse open-ended narrative prompts or specialized long-form questions into `Yes`.
- R4. Strict factual qualification claims are included in the `Yes` default for positive-fit screening, not just willingness/comfort prompts.
- R5. Degree, license, and certification questions are an exception. They shall answer `Yes` only when the credential is explicitly supported by authoritative candidate data already available to the repo, such as `application_profile.md` or `master_resume.md`.
- R6. If a degree/license/certification question is not explicitly supported by candidate data, the system shall not force `Yes`. It shall remain on the existing truth-preserving path for that field type and board, including profile-backed answers or fail-closed handling where appropriate.
- R7. Existing profile-driven categories that are not positive-fit screens shall remain outside this policy. Work authorization, sponsorship/visa, compensation/salary, demographic/self-ID/EEO, and similar compliance-sensitive questions must continue using their existing profile-driven or fail-closed behavior rather than a blanket `Yes`.
- R8. Screenshot-confirmed live form state remains the source of truth. If the system plans a `Yes` answer under this policy but cannot confirm that answer on the actual form, the run shall remain incomplete rather than reporting a successful draft from planned values alone.
- R9. The specific LinkedIn/Asurion-style cases that triggered this brainstorm must be covered by the global policy: `Are you comfortable working in a hybrid setting?` shall answer `Yes`, and `Do you have extensive experience working with Data Science and AI?` shall answer `Yes`.

## Success Criteria

- The LinkedIn/Asurion screenshot scenario is handled without manual intervention: both the hybrid-setting prompt and the experience-with-Data-Science-and-AI prompt are answered `Yes`.
- Equivalent discrete positive-fit screening questions across supported boards no longer remain unanswered solely because the board uses a different control type or board-specific handler.
- Degree/license/certification questions answer `Yes` only when explicit candidate data supports the credential.
- Existing profile-driven answers for work authorization, sponsorship, compensation, and self-ID/compliance questions do not regress into blanket `Yes` handling.
- Any run that cannot visually confirm the intended answer on the live form still fails closed instead of claiming draft readiness from planned answers alone.

## Scope Boundaries

- NOT changing the `--draft` rule or auto-submitting applications.
- NOT reducing long free-text or specialized narrative questions to `Yes`.
- NOT inventing unsupported degree, license, or certification credentials.
- NOT changing the existing answer policy for work authorization, sponsorship, compensation, demographic/self-ID, or other compliance-sensitive questions.
- NOT requiring an LLM fallback for discrete positive-fit screening questions if deterministic/shared handling can cover them.

## Key Decisions

- Positive-fit screening questions should optimize for interview chance and default to `Yes`.
- This is a global policy across all boards and all runtime surfaces, not a LinkedIn-only special case.
- Strict factual qualification claims are included in the `Yes` default, except for degree/license/certification questions that require explicit candidate-data support.
- The preferred solution direction is shared deterministic handling, not a board-by-board pile of exceptions and not an LLM-first fallback.
- Screenshot-confirmed live UI state remains the source of truth for whether a draft is actually ready.

## Dependencies / Assumptions

- `application_profile.md` and `master_resume.md` remain the authoritative structured sources for credential-backed exceptions.
- Existing pending-user-input and fail-closed behavior remains available for unsupported open-ended questions or unconfirmed live-form states.
- New boards added later should inherit this policy by default rather than requiring a fresh product decision.

## Alternatives Considered

- LinkedIn-only policy: rejected because the repo's operating rules require generalizing fixes across boards and surfaces.
- Willingness/comfort-only `Yes` policy: rejected because the user explicitly wants strict factual qualification claims included when they increase interview chances.
- Blanket `Yes` for degree/license/certification questions too: rejected because those credentials are unusually easy to contradict and should only be asserted when explicitly supported by candidate data.

## Outstanding Questions

### Resolve Before Planning

- None.

### Deferred to Planning

- How should the shared positive-fit category be represented so every board inherits the same answer policy without duplicating classifier and mapping logic?
- What is the cleanest authoritative-source lookup order for degree/license/certification support when both `application_profile.md` and `master_resume.md` may contain relevant signals?

## Next Steps

-> `/prompts:ce-plan` to replace the earlier LinkedIn-only implementation plan with a board-agnostic plan for the global policy above
