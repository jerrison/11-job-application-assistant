---
date: 2026-03-28
topic: greenhouse-preference-ranking-live-research
---

# Greenhouse Preference / Ranking Live Research

## Problem Frame

The 2026-03-28 Greenhouse fixes restored support for skipped `multi_value_multi_select` and ranking-style prompts, but those answers still rely on normal generated-answer behavior. That is good enough for many prompts, yet it does not satisfy the stronger product ask: when Greenhouse asks the candidate to choose or rank teams, product areas, functions, or similar preference buckets, the runtime should research the current options before choosing instead of treating them like generic multi-select labels.

This needs to stay narrow. The goal is not open-ended web browsing for every Greenhouse select field. The enhancement should only apply to preference/ranking prompts where informed option selection is the whole point of the question, and it must preserve the repo's existing truthful deterministic paths for work authorization, compliance, self-ID, compensation, location, and other factual questions.

## Confirmed Scope

- Greenhouse only.
- Preference / ranking prompts only.
- Current deterministic categories keep priority over this new path.
- Current live option labels are the source of truth; stale saved artifacts are not enough.

## Requirements

- R1. The enhancement shall run only for Greenhouse-generated choice questions that are clearly asking for preferences, priorities, interest areas, or rankings.
- R2. The enhancement shall support `multi_value_multi_select` prompts and may support `multi_value_single_select` prompts when the label is clearly preference-based rather than factual.
- R3. Existing deterministic classification paths such as office attendance, city/location, work authorization, compensation, product usage, prior-employer, and self-ID shall win before this feature can trigger.
- R4. The runtime shall use the current live Greenhouse option list as the answer boundary and may not invent, paraphrase, or fuzzily broaden option labels beyond what the live form exposes.
- R5. Preference/ranking research shall use existing repo context first: `jd_parsed.json`, `research_cache.json`, role research, resume context, cover letter context, and candidate context.
- R6. The runtime may use provider-side web research for these prompts, but only in a pre-answer research stage that is separate from submit-mode answer generation.
- R7. The research stage shall persist current-attempt proof artifacts showing the eligible field, the live options considered, the selected labels, and the supporting evidence used to justify them.
- R8. If a required preference/ranking field remains ambiguous after research or fails validation against the current live options, the draft shall fail closed instead of guessing.
- R9. If live options drift after research, runtime fill shall confirm the researched labels still exist on the current DOM before checking boxes or selecting options.
- R10. Normal Greenhouse generated-answer behavior for non-preference prompts shall remain unchanged.

## Success Criteria

- Greenhouse prompts like `Which of these roles are you most interested in? Select up to 3.` can select exact live options using grounded research rather than generic prompting alone.
- Preference prompts do not leak into location, compliance, or self-ID handling.
- A rerun leaves behind durable current-attempt artifacts showing which options were researched and why they were chosen.
- If the runtime cannot justify or validate a required preference answer, the draft stops with explicit proof instead of silently picking weak options.

## Scope Boundaries

- In scope: Greenhouse preference/ranking prompts about teams, functions, product areas, role families, or similar interest-based choices.
- In scope: using existing company/role research context plus targeted provider-side web research before normal submit answers are generated.
- In scope: durable current-attempt artifacts for researched option selection.
- Out of scope: general web research for every Greenhouse select field.
- Out of scope: compliance, legal, self-ID, work authorization, compensation, location, or prior-employer prompts.
- Out of scope: replacing Greenhouse's normal generated-answer flow for open-ended text answers.

## Current Design Direction

### Approved

- Add a narrow `preference_ranking` classification lane, but only use it as the first half of the gate.
- Require a second Greenhouse-only eligibility check so prompts that merely contain words like `preference` but are actually office/location questions stay on their existing deterministic path.
- Treat live current-form options as the hard boundary for valid outputs.

### Proposed Next Implementation Shape

- Run a Greenhouse-only pre-answer research stage after `_application_question_specs(job_post)` and before the normal submit answer provider call.
- Feed that stage the exact live option labels plus the existing repo research context.
- Use provider `research` mode for this stage so provider-side web search is enabled there without widening submit-mode provider permissions.
- Persist submit-time artifacts such as:
  - `submit/preference_research_context.json`
  - `submit/preference_research_raw.txt`
  - `submit/preference_research_failures.json`
- Merge validated researched selections into the normal answer flow as deterministic answers ahead of generic generated answers.
- Revalidate the researched labels against the current live DOM at fill time and stop if the options have drifted.

## Dependencies / Assumptions

- `scripts/question_classifier.py` remains the central classification seam.
- Greenhouse's existing `_generate_application_answers()` path remains the correct orchestration seam for this board-local enhancement.
- `provider_command_for_mode(..., mode="research")` is the intended way to enable provider-side web research without widening submit-mode permissions.
- Existing linked-resource evidence handling is the closest current proof pattern for current-attempt artifacts and fail-closed behavior.

## Outstanding Questions

### Resolve Before Planning

- Confirm the exact artifact filenames and whether they should also surface through the existing proof-artifact serialization path.
- Confirm whether the first slice should support only `multi_value_multi_select` or both multi-select and single-select preference prompts.

### Deferred to Planning

- Decide the exact heuristic that distinguishes `preference_ranking` from office/location wording that also includes words like `preference`.
- Decide whether researched evidence notes should be stored only in the new preference-research artifact or also mirrored into `application_answers.json`.
- Decide the cache boundary for reusing preference research across retries without hiding live-form drift.

## Next Steps

- Resume the brainstorming flow from the `Research Flow + Proof` section and get explicit approval on the proposed artifact and merge design.
- After design approval, write the formal spec and implementation plan before touching code.
