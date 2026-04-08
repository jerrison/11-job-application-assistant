---
date: 2026-03-26
topic: shared-compensation-and-gpa-defaults
---

# Shared Compensation and GPA Defaults

## Problem Frame

Some explicit application questions that should receive deterministic profile answers are not being filled consistently across supported draft flows. In the Airwallex Ashby draft, a salary-expectation field was left blank even though the product already has a truthful compensation default, and an undergraduate GPA prompt was filled with honors text instead of the candidate's GPA. This creates avoidable cleanup work and inconsistent autofill behavior.

## Requirements

- R1. Explicit salary or compensation expectation prompts must be filled automatically with the existing truthful compensation-default answer whenever the field accepts free text.
- R2. If a salary or compensation field is truly numeric-only, the system must keep the existing numeric-only fallback behavior instead of leaving the field blank or inventing a new policy.
- R3. Explicit undergraduate or Bachelor's GPA prompts must deterministically answer `3.8/4.0`.
- R4. Latin honors, class rank, and other education distinctions must not be used as substitutes for GPA prompts.
- R5. These behaviors must be treated as shared defaults across supported boards and surfaces, not as a one-off Airwallex or Ashby exception.

## Success Criteria

- Free-text salary expectation prompts no longer appear blank in draft screenshots when the question is recognized.
- Undergraduate GPA prompts resolve to `3.8/4.0` instead of unrelated education text.
- The same compensation and GPA behavior appears consistently across future draft runs, regardless of board or surface.

## Scope Boundaries

- Not changing the standing policy to avoid negotiating with numeric salary amounts in normal text fields.
- Not changing work-authorization, sponsorship, or self-identification defaults.
- Not introducing company-specific overrides for Airwallex only.

## Key Decisions

- **Reuse the existing compensation default**: Salary-expectation prompts should use the current truthful profile answer rather than introducing a new compensation script.
- **Keep numeric-only handling on its existing path**: The problem is the missing fill, not a request to redefine numeric-only compensation policy.
- **Treat GPA as a deterministic credential answer**: Undergraduate GPA should come from a stable candidate fact, not inferred from other education fields.

## Dependencies / Assumptions

- `3.8/4.0` is the authoritative undergraduate GPA for future autofill.
- Supported boards can distinguish GPA prompts from other education-related prompts closely enough to avoid false matches.

## Outstanding Questions

### Resolve Before Planning

- None.

### Deferred to Planning

- [Affects R1][Technical] Where the shared compensation-answer path should live so boards that already classify compensation prompts inherit it automatically.
- [Affects R3][Technical] How to ensure GPA prompts resolve before any broader education-answer fallback that might substitute honors text.

## Next Steps

→ /prompts:ce-plan for structured implementation planning
