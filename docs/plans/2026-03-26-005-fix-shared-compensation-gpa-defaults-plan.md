---
title: fix: Share compensation and undergraduate GPA defaults across autofill
type: fix
status: active
date: 2026-03-26
origin: docs/brainstorms/2026-03-26-shared-compensation-and-gpa-defaults-requirements.md
deepened: 2026-03-26
---

# fix: Share compensation and undergraduate GPA defaults across autofill

## Overview

Bring explicit salary-expectation and undergraduate-GPA prompts onto the same shared deterministic-answer path used for other profile-backed autofill defaults. The immediate user-visible failures are an Ashby salary field left blank and an undergraduate GPA prompt answered with honors text, but the fix should land in shared profile/classifier/policy seams so CLI, TUI, web, worker, and future board runs inherit the same behavior. (see origin: docs/brainstorms/2026-03-26-shared-compensation-and-gpa-defaults-requirements.md)

## Problem Frame

The repo already has the right product intent but not a complete implementation path. `application_profile.md` already contains a compensation-expectations answer, yet the shared `ApplicationProfile` parser in `scripts/application_submit_common.py` does not expose it as structured data, so boards still rely on duplicated compensation strings or miss the field entirely. `scripts/question_classifier.py` already classifies open-ended compensation prompts, but Ashby does not convert that category into a text step, and Greenhouse excludes deterministic questions from LLM generation without a corresponding compensation/GPA text-fill branch in `_question_step()`.

Undergraduate GPA has a second gap: the real Ashby label `Please list your undergraduate (Bachelor's) GPA:` is currently a `null` corpus entry, so it can fall through to the LLM and drift into adjacent education facts such as honors text. Greenhouse has duplicate conditional-follow-up validation that currently treats recent-grad GPA prompts as `N/A`, which conflicts with the new requirement to answer explicit undergraduate GPA prompts as `3.8/4.0`.

## Requirements Trace

- R1. Explicit salary or compensation expectation prompts must be filled automatically with the existing truthful compensation-default answer whenever the field accepts free text.
- R2. If a salary or compensation field is truly numeric-only, the system must keep the existing numeric-only fallback behavior instead of leaving the field blank or inventing a new policy.
- R3. Explicit undergraduate or Bachelor's GPA prompts must deterministically answer `3.8/4.0`.
- R4. Latin honors, class rank, and other education distinctions must not be used as substitutes for GPA prompts.
- R5. These behaviors must be treated as shared defaults across supported boards and surfaces, not as a one-off Airwallex or Ashby exception.

## Scope Boundaries

- Do not change the standing non-numeric compensation policy for normal free-text fields.
- Do not widen this work into a general education-profile redesign beyond the explicit undergraduate-GPA fact needed here.
- Do not rewrite every board's deterministic question flow if the local scan already shows the board has a working compensation text path.
- Do not weaken screenshot-based confirmation or the existing fail-closed `pending_user_input` behavior for unresolved fields.

## Context & Research

### Relevant Code and Patterns

- `application_profile.md` already contains `Compensation Expectations`, but `scripts/application_submit_common.py` does not parse that line into `ApplicationProfile`.
- `scripts/application_submit_common.py` contains the shared `ApplicationProfile` dataclass, `parse_application_profile()`, `resolve_shared_question_policy()`, `_COMPENSATION_NEGOTIABLE_ANSWER`, and the shared generated-answer validation path that currently treats recent-grad GPA prompts as generic conditional follow-ups.
- `scripts/question_classifier.py` already classifies `compensation`, `salary_comfort`, and `education`, but real Ashby GPA labels are still unclassified in `tests/fixtures/question_label_corpus.json`.
- `scripts/autofill_ashby.py` already uses `classify_question()` and `resolve_shared_question_policy()`, but only consumes boolean policy answers; it has no shared text-answer branch for compensation or GPA.
- `scripts/autofill_greenhouse.py` has its own `ApplicationProfile` parser, deterministic-question filter, duplicate conditional-follow-up validation, and `_question_step()` logic. It handles `salary_comfort` but has no explicit branch for `compensation` or undergraduate GPA text answers.
- `scripts/autofill_lever.py`, `scripts/autofill_bamboohr.py`, `scripts/autofill_gem.py`, `scripts/autofill_workday.py`, `scripts/autofill_icims.py`, `scripts/autofill_phenom.py`, `scripts/autofill_dover.py`, and `scripts/autofill_linkedin.py` already show either a compensation text branch or a `policy.text_value` pattern worth following.
- `scripts/submit_application.py` and `scripts/autofill_pipeline.py` are the shared draft/submit entry path that CLI, worker, and browser-backed board runs all traverse before control reaches a board-local `_infer_step()` or `_question_step()`.
- `scripts/autofill_common.py`, `scripts/draft_manager.py`, and the `planned_but_unconfirmed_fields` / `pending_user_input` artifact flow are the shared confirmation/reporting seam that TUI and web surfaces consume rather than re-implementing board logic.
- `scripts/job_worker.py`, `scripts/job_tui.py`, and `scripts/job_web.py` consume the same draft artifacts and submit pipeline outputs; they do not maintain an independent compensation/GPA answer path.
- `tests/test_lever_autofill.py` already locks in a compensation-textarea pattern that can serve as the baseline behavior.
- `tests/test_greenhouse_autofill.py` and `tests/test_submit_application.py` currently assert `N/A` for recent-grad GPA prompts, which will need to be narrowed so only true conditional follow-ups keep that fallback.
- `tests/test_positive_fit_screening_policy.py`, `tests/test_autofill_pipeline.py`, `tests/test_job_worker.py`, and `tests/test_job_web.py` are the existing shared-surface verification seams that can catch regressions in common policy routing, unresolved-field handling, and artifact consumption.

### Institutional Learnings

- `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`
  - New deterministic question handling belongs in `scripts/question_classifier.py`, not in new per-board label matchers.
  - Specific-before-broad ordering matters whenever a new detector overlaps with `education`.
  - Real-label corpus entries should be updated alongside detector changes so new classifications do not silently drift.
  - If one board exposes a shared-classifier gap, audit the other supported boards before declaring the fix board-specific.

### External References

- None. The codebase already contains the relevant shared-policy, classifier, and board-level patterns.

## Key Technical Decisions

- **Make compensation and GPA profile-backed shared text answers**: parse both values from shared profile inputs and let `resolve_shared_question_policy()` surface them, rather than hardcoding new board-local literals.
  Rationale: this matches the repo's existing source-of-truth model and keeps CLI/TUI/web/worker behavior aligned.
- **Justify the common-layer change through the actual entry-point chain**: the plan relies on `application_profile.md` -> `scripts/application_submit_common.py` -> `scripts/question_classifier.py` / `resolve_shared_question_policy()` -> `scripts/autofill_pipeline.py` -> board step builders, which is the same path used by CLI, worker, and web-backed draft runs.
  Rationale: this makes the shared approach an architectural necessity rather than a preference. A board-only patch would leave the shared draft/submit path drifting whenever another board or surface encountered the same prompt shape.
- **Add a narrow undergraduate-GPA classifier category before broad education detection**.
  Rationale: GPA prompts are a specific candidate fact, not a request for the full education history. Putting the category ahead of `education` prevents honors/degree text from stealing the label.
- **Make the GPA source explicit in shared profile data**: the implementation should add and parse an explicit `Undergraduate GPA` entry in `application_profile.md`, with `3.8/4.0` as the authoritative value for this workflow.
  Rationale: the shared policy cannot return a deterministic text answer if the source field is only implied in planning prose.
- **Treat Ashby and Greenhouse as the implementation outliers unless the bounded audit finds another broken text path**.
  Rationale: the local scan already shows most other supported boards either honor `policy.text_value` or implement compensation text explicitly.
- **Keep numeric-only compensation policy stable in this change**.
  Rationale: the user-visible bug is the missing free-text fill. This fix should not reopen the broader numeric-only compensation policy debate unless implementation uncovers a concrete regression.
- **Fail closed when a newly shared text value is missing**: if compensation expectations or undergraduate GPA are absent/empty in the shared profile, the deterministic path must route the field into the existing unresolved-field / `pending_user_input` flow instead of silently dropping the prompt or falling back to an invented LLM answer.
  Rationale: the classifier may now filter these prompts out before generation. Missing source data must therefore become an explicit review artifact, not an invisible blank.
- **Preserve fail-closed runtime confirmation**.
  Rationale: deterministic planning is only useful if the screenshot-confirmed field value matches it. Unconfirmed salary/GPA fields must still surface through the existing unresolved-field path.
- **Keep the audit bounded but explicit**: implementation should verify the shared text-policy consumers and deterministic filters in `scripts/autofill_lever.py`, `scripts/autofill_gem.py`, `scripts/autofill_bamboohr.py`, `scripts/autofill_workday.py`, `scripts/autofill_icims.py`, `scripts/autofill_phenom.py`, `scripts/autofill_dover.py`, `scripts/autofill_linkedin.py`, `scripts/autofill_workable.py`, and `scripts/autofill_eightfold.py`, widening code changes only on confirmed repro.
  Rationale: this keeps scope under control while making the parity claim reviewable.

## Open Questions

### Resolved During Planning

- **Should this be fixed only in Ashby?** No. The origin requirements and the classifier learning both point to a shared policy fix with bounded board outlier cleanup, not a one-off Ashby patch.
- **Where should `3.8/4.0` live?** In an explicit `Undergraduate GPA` field in `application_profile.md`, parsed into shared profile data rather than hardcoded in a board constant.
- **Is external research needed?** No. The repo already shows the relevant shared and board-local patterns.
- **Which boards clearly need direct code changes?** Ashby and Greenhouse. The other supported boards already show either a compensation text branch or a shared text-policy hook in the current code scan.
- **What should happen if the shared compensation or GPA value is missing?** Fail closed through the existing unresolved-field / `pending_user_input` path so deterministic classification never silently removes a required question from generation without leaving a review artifact.

### Deferred to Implementation

- **Should every remaining per-board compensation literal be deleted in the same change?** Prefer yes where the shared policy path can replace it cleanly, but defer the exact cleanup scope until regression coverage is green.
- **Should generic GPA prompts without explicit `undergraduate` / `Bachelor's` wording be classified now?** Start with explicit undergraduate/Bachelor's and other clearly unambiguous GPA/grade-point-average corpus labels only; broaden only if harvested labels show a safe, repeatable pattern that does not overlap with honors or general education prompts.
- **How should numeric-only salary widgets behave if they do not accept the shared text answer?** Keep the current board behavior unchanged for this plan unless a concrete regression appears during implementation.

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```text
application_profile.md
  -> parse shared profile fields
     -> compensation_expectations
     -> undergraduate_gpa

submit_application.py / bin/job-assets / job_worker.py
  -> autofill_pipeline.py
     -> board step builder
        -> classify_question()
             compensation       -> shared text policy
             undergraduate_gpa  -> shared text policy
             everything else    -> existing category flow

board step builder
  -> if text-capable field and policy.text_value exists
       fill deterministic text
     else if shared source value missing
       fail closed into pending_user_input / planned_but_unconfirmed_fields
     else
       continue existing board-local routing

generated-answer fallback
  -> deterministic compensation/GPA prompts should be filtered out before LLM
  -> if a GPA prompt still leaks through validation, prefer profile GPA over generic N/A

runtime verification
  -> existing screenshot + planned_but_unconfirmed_fields flow remains unchanged
  -> draft artifacts feed TUI/web review surfaces without UI-specific answer logic
```

## Implementation Units

- [ ] **Unit 1: Extend shared profile, classifier, and policy for compensation and GPA**

**Goal:** Expose compensation expectations and undergraduate GPA as shared deterministic text answers rather than board-local literals or LLM guesses.

**Requirements:** R1, R2, R3, R4

**Dependencies:** None

**Files:**
- Modify: `application_profile.md`
- Modify: `scripts/application_submit_common.py`
- Modify: `scripts/question_classifier.py`
- Modify: `tests/fixtures/question_label_corpus.json`
- Modify: `tests/test_question_classifier.py`
- Test: `tests/test_positive_fit_screening_policy.py`
- Test: `tests/test_submit_application.py`

**Approach:**
- Add structured shared-profile fields for `compensation_expectations` and `undergraduate_gpa`, parsing them from `application_profile.md` instead of relying on implicit text in the markdown file. `Compensation Expectations` already exists; the plan should add an explicit `Undergraduate GPA: 3.8/4.0` entry as the authoritative shared source.
- Keep the existing compensation-answer wording as the default shared text answer source, but move the decision into `resolve_shared_question_policy()` so boards can consume it uniformly.
- Introduce a narrow classifier category for explicit undergraduate/Bachelor's GPA labels and other clearly unambiguous GPA/grade-point-average corpus labels ahead of the broad `education` category, then update the corpus entries that currently treat real GPA labels as `null`.
- Update the shared generated-answer validation path so explicit GPA prompts prefer the profile-backed GPA value when they appear, while generic conditional follow-ups still retain the `N/A` fallback.
- Define the missing-data branch explicitly: if a newly shared text value is blank or absent, the shared layer must fail closed through the existing unresolved-field / `pending_user_input` flow rather than silently filtering the question out before generation.
- Avoid widening the detector to generic education or honors prompts; this unit should distinguish GPA from education-history text, not replace education handling wholesale.

**Execution note:** Start with failing classifier-corpus and shared-validator tests so the new category and shared-policy behavior are locked in before board scripts change.

**Patterns to follow:**
- `comfortable_with_posted_salary` parsing and shared-policy handling in `scripts/application_submit_common.py`
- Corpus-backed detector changes in `tests/fixtures/question_label_corpus.json`
- Specific-before-broad classifier ordering from `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`

**Test scenarios:**
- `Compensation Expectations` is parsed into the shared application profile and surfaced as the shared text answer for compensation prompts.
- `Please list your undergraduate (Bachelor's) GPA:` classifies as the new GPA category, not `education` and not `None`.
- Explicit GPA variants harvested from the corpus match the new GPA category only when they unambiguously ask for the candidate's GPA; honors/achievement prompts remain outside the GPA category.
- `What is your desired salary range?` remains `compensation`, not `salary_comfort`.
- Explicit GPA prompts no longer validate to `N/A` when shared profile data exists.
- Missing shared compensation/GPA values fail closed into unresolved-field or `pending_user_input` behavior instead of silently removing the question from generation.
- Generic conditional prompts such as `If yes... please specify` still keep the `N/A` fallback when appropriate.

**Verification:**
- Shared policy callers can obtain deterministic text answers for compensation and GPA from the common layer.
- The classifier corpus passes with the GPA label moved from `null` to a stable explicit category.
- Shared validation still differentiates between true conditional follow-ups and explicit GPA questions.
- Existing shared positive-fit / policy tests still pass, confirming the new text categories do not regress the common policy surface.

- [ ] **Unit 2: Align Greenhouse deterministic filtering with Greenhouse text filling**

**Goal:** Ensure Greenhouse both excludes compensation/GPA prompts from LLM generation and fills them deterministically on the live form.

**Requirements:** R1, R2, R3, R4, R5

**Dependencies:** Unit 1

**Files:**
- Modify: `scripts/autofill_greenhouse.py`
- Test: `tests/test_greenhouse_autofill.py`

**Approach:**
- Extend the Greenhouse-local `ApplicationProfile` parser to carry the same shared compensation/GPA facts as the common profile parser.
- Keep Greenhouse's deterministic-question filter and `_question_requires_generated_answer()` in sync with the new shared classifier category so explicit compensation/GPA prompts do not reach provider generation.
- Add a Greenhouse text-answer branch that consumes `resolve_shared_question_policy(...).text_value` for `input_text` / `textarea` style questions before any generated-answer fallback.
- Keep the existing `salary_comfort` select/yes-no handling intact; this unit is about open-ended salary expectation text and GPA text prompts, not compensation-comfort gates.
- Update Greenhouse's duplicate validation fallback so leaked GPA prompts resolve to the shared profile GPA instead of `N/A`.
- If Greenhouse classifies a compensation/GPA prompt as deterministic but the shared source value is missing, require the same fail-closed unresolved-field / `pending_user_input` behavior the rest of the draft pipeline already uses.

**Patterns to follow:**
- Existing Greenhouse `salary_comfort` deterministic branch
- Existing Greenhouse deterministic-question exclusion before provider generation
- Shared fail-closed `planned_but_unconfirmed_fields` behavior already used for unresolved Greenhouse fields

**Test scenarios:**
- A Greenhouse free-text salary-expectation question produces the shared compensation answer in `_question_step()`.
- `If you're less than 3 years out of school, what is your undergraduate GPA?` is treated as deterministic and plans `3.8/4.0`.
- Explicit GPA prompts no longer round-trip through blank-provider-output to `N/A`.
- Unrelated conditional follow-ups remain `N/A` when their parent question makes them inapplicable.
- Existing salary-comfort yes/no prompts continue to use `comfortable_with_posted_salary`.

**Verification:**
- Greenhouse deterministic text prompts are both filtered out of generated-answer specs and filled at step construction time.
- Greenhouse tests demonstrate `3.8/4.0` for GPA prompts and the shared compensation text for salary prompts.
- Existing unresolved-field / pending-user-input behavior still catches any live confirmation mismatch.
- The Greenhouse path remains aligned with the shared draft artifact flow consumed by worker/TUI/web review surfaces, rather than introducing a board-local exception.

- [ ] **Unit 3: Teach Ashby to honor shared text policies for salary and GPA prompts**

**Goal:** Close the Ashby-specific live gap by routing shared text answers into `String` and `LongText` fields before LLM fallbacks.

**Requirements:** R1, R3, R4, R5

**Dependencies:** Unit 1

**Files:**
- Modify: `scripts/autofill_ashby.py`
- Test: `tests/test_ashby_autofill.py`

**Approach:**
- Extend Ashby's `_infer_step()` so shared `policy.text_value` answers can produce deterministic text steps for `String` and `LongText` fields.
- Keep the existing boolean policy path, `salary_comfort` handling, education-history fill, resume upload, and cover-letter branches in their current precedence order unless they directly conflict with the new shared text policy.
- Make sure the GPA text path only handles explicit GPA prompts; broad education prompts should still use the full education-history formatter.
- During implementation, run the bounded audit explicitly against `scripts/autofill_lever.py`, `scripts/autofill_gem.py`, `scripts/autofill_bamboohr.py`, `scripts/autofill_workday.py`, `scripts/autofill_icims.py`, `scripts/autofill_phenom.py`, `scripts/autofill_dover.py`, `scripts/autofill_linkedin.py`, `scripts/autofill_workable.py`, and `scripts/autofill_eightfold.py` to confirm each deterministic filter either consumes `policy.text_value` or safely leaves compensation/GPA prompts in the generated-answer path. Only widen code changes on confirmed repro.

**Patterns to follow:**
- Compensation textarea handling in `scripts/autofill_lever.py`
- Shared text-policy consumption in `scripts/autofill_gem.py` and `scripts/autofill_bamboohr.py`
- The classifier-regression learning's instruction to generalize across boards before declaring a bug fixed

**Test scenarios:**
- An Ashby salary-expectation text field receives the shared compensation answer instead of remaining blank.
- An Ashby undergraduate GPA field receives `3.8/4.0` instead of honors text or an LLM-generated guess.
- A broad education textarea still returns formatted education history, not GPA.
- Cover-letter, sponsorship, and salary-comfort handlers continue behaving as they do today.

**Verification:**
- Ashby deterministic text planning returns shared salary/GPA answers for the explicit labels covered by the origin requirements.
- The board no longer falls through to LLM-generated drift for those prompts.
- The bounded cross-board audit confirms either `already handled`, `not applicable`, or `follow-up needed` for other supported board text paths.
- Shared draft artifacts remain the operator-facing verification source for CLI, worker, TUI, and web review flows after the Ashby fix lands.

- [ ] **Unit 4: Update operator docs for the new shared defaults**

**Goal:** Keep user-facing and operator-facing docs aligned with the new shared deterministic behavior.

**Requirements:** R5

**Dependencies:** Units 1, 2, 3

**Files:**
- Modify: `README.md`
- Modify: `docs/autofill-patterns.md`

**Approach:**
- Document that open-ended salary-expectation prompts now use the shared profile-backed compensation answer when the field accepts text.
- Document that explicit undergraduate/Bachelor's GPA prompts now use the shared profile-backed GPA value instead of LLM inference or education-history text.
- Preserve current wording around salary-comfort yes/no gates and numeric-only compensation behavior unless the implementation materially changes those paths.

**Patterns to follow:**
- Existing README/autofill-patterns bullets for salary-comfort and deterministic question routing

**Test scenarios:**
- None. This unit is documentation-only.

**Verification:**
- The docs describe the same shared compensation/GPA behavior the implementation now enforces.

## System-Wide Impact

- **Interaction graph:** `application_profile.md` -> shared/common profile parser in `scripts/application_submit_common.py` -> `scripts/question_classifier.py` / `resolve_shared_question_policy()` -> `scripts/submit_application.py` / `scripts/autofill_pipeline.py` -> board step builders (`scripts/autofill_ashby.py`, `scripts/autofill_greenhouse.py`, and existing text-policy consumers) -> draft artifacts via `scripts/autofill_common.py` / `scripts/draft_manager.py` -> TUI/web/worker review surfaces.
- **Error propagation:** If the profile lacks a needed compensation or GPA fact, or a deterministic text field cannot be confirmed live, the board should continue using the existing unresolved-field / `pending_user_input` flow rather than inventing a fallback answer or silently dropping the prompt before generation.
- **State lifecycle risks:** Greenhouse currently has separate deterministic-filter, validation, and question-step layers. Missing any one of those seams would recreate the exact blank-or-`N/A` mismatch this fix is meant to remove.
- **API surface parity:** CLI, TUI, web, and worker all consume the same shared submit/autofill modules and draft artifacts, so the fix must land in common policy/classifier seams and the board outliers, not in UI-only review tooling.
- **Integration coverage:** Regression coverage should span shared parser/policy tests, classifier corpus tests, board-local step-planning tests, and the shared artifact/reporting seams used by `tests/test_autofill_pipeline.py`, `tests/test_job_worker.py`, and `tests/test_job_web.py`. Screenshots remain the source of truth for end-to-end confirmation beyond unit coverage.

## Risks & Dependencies

- Adding a new GPA classifier category could accidentally steal broad education labels if the detector is too wide. Mitigation: keep the pattern narrow and update corpus entries with real labels before broadening.
- Greenhouse duplicates shared profile/validation logic locally. Mitigation: treat Greenhouse as its own implementation unit and keep parser/filter/validation/step-builder changes together.
- Newly deterministic compensation/GPA prompts could be filtered out before LLM generation while the shared source value is missing. Mitigation: define and test the fail-closed unresolved-field / `pending_user_input` branch in the shared layer and Greenhouse-local duplicate path.
- Numeric-only compensation behavior is documented inconsistently in the repo. Mitigation: keep that policy stable for this plan and avoid using this change to redefine numeric-only answers.
- If additional boards fail the bounded audit, the work could expand. Mitigation: keep the audit explicit, list the concrete scripts to inspect up front, and widen scope only on confirmed repro, not speculation.

## Documentation / Operational Notes

- The post-implementation docs update should mention the new explicit undergraduate-GPA default and the fact that compensation text now comes from shared profile data rather than ad hoc board literals where possible.
- No rollout flag is needed; this is deterministic autofill behavior behind existing draft-mode verification and screenshot review.

## Sources & References

- **Origin document:** [docs/brainstorms/2026-03-26-shared-compensation-and-gpa-defaults-requirements.md](../brainstorms/2026-03-26-shared-compensation-and-gpa-defaults-requirements.md)
- Related code: `scripts/application_submit_common.py`
- Related code: `scripts/question_classifier.py`
- Related code: `scripts/autofill_ashby.py`
- Related code: `scripts/autofill_greenhouse.py`
- Related tests: `tests/test_question_classifier.py`
- Related tests: `tests/test_submit_application.py`
- Related tests: `tests/test_ashby_autofill.py`
- Related tests: `tests/test_greenhouse_autofill.py`
- Institutional learning: `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`
