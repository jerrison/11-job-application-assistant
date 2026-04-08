---
title: fix: Correct Greenhouse current-state residency selection
type: fix
status: active
date: 2026-03-26
deepened: 2026-03-26
---

# fix: Correct Greenhouse current-state residency selection

## Overview

Greenhouse is currently answering some current-residence prompts from the role location instead of the candidate profile. On the ezCater draft, `Which state do you currently reside in?` was filled as `Massachusetts` even though `application_profile.md` still says `San Francisco, CA`. The fix must preserve role-location-first behavior for work-location availability prompts while sourcing actual residence/state prompts from the candidate.

If left unfixed, the pipeline can carry a visually confirmed but incorrect residence answer all the way through draft review, creating a real risk of submitting false current-location information on an application even though the automation appears to have succeeded.

## Problem Frame

This is a deterministic autofill bug in the Greenhouse submitter, not stale profile data. `application_profile.md` and `candidate_context.md` still point to San Francisco, and the saved draft artifact under `output/ezcater-inc/principal-pm-finance-platform-remote/draft_summary.md` records the wrong answer as `Massachusetts`. The current Greenhouse `_question_step()` broad `reside in / based in / located in` branch falls back to `_location_option_label(...)`, and that helper intentionally prefers `role_location` from `jd_parsed.json` before the candidate location. That behavior is correct for prompts like `Where do you intend to work?` but wrong for `Which state do you currently reside in?`.

Because the planned step itself is wrong, the browser runtime confirms the wrong value and the screenshot becomes the source of truth across CLI, TUI, worker, and web. The fix has to happen at step construction time rather than in downstream confirmation logic.

## Requirements Trace

- R1. Greenhouse prompts asking for the candidate's current state or current residence must answer from `application_profile.md` / candidate location, not `jd_parsed.json` role location.
- R2. Existing Greenhouse role-location-first behavior for availability / intended-work-location prompts must remain intact.
- R3. The planned answer in draft artifacts and the live browser-confirmed combobox value must stay consistent and reflect the candidate's actual state.
- R4. Regression coverage must include the exact real-world label `Which state do you currently reside in?` plus a role whose JD location is `Boston, MA`.
- R5. The fix must be audited against existing board patterns so Greenhouse does not diverge from current-state handling already present elsewhere.

## Scope Boundaries

- No resume/header or job-discovery location changes.
- No user-profile data edits; `application_profile.md` is already correct.
- No broad classifier refactor unless the Greenhouse fix reveals a second board with the same composition bug.

## Context & Research

### Relevant Code and Patterns

- `scripts/autofill_greenhouse.py`: `_question_step()`, `_location_option_label()`, `_match_option_label()`, `_location_state_variants()`, `_state_membership_answer()`
- `tests/test_greenhouse_autofill.py`: existing tests for state-only location option matching and explicit state-list residency yes/no prompts
- `output/ezcater-inc/principal-pm-finance-platform-remote/draft_summary.md`: real artifact showing `Which state do you currently reside in?` answered as `Massachusetts`
- `output/ezcater-inc/principal-pm-finance-platform-remote/content/jd_raw.md`: Greenhouse JD metadata carries `location: Boston, MA`
- `scripts/autofill_ashby.py`: interrogative `what/which/where` location guard before broad `live in` yes/no handling
- `scripts/autofill_lever.py`: `_is_currently_in_state_question()` / `_answer_currently_in_state()` pattern for current-state prompts
- `scripts/autofill_linkedin.py`: direct `which state` / `current state` mapping to `CA`
- `docs/autofill-patterns.md` and `README.md`: both already promise Greenhouse state handling should come from the actual profile state

### Institutional Learnings

- `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`
  - Prefer specific-before-broad routing for overlapping location/residency prompts.
  - Add regression cases from real labels instead of relying only on generic keyword coverage.
- `docs/autofill-patterns.md`
  - Greenhouse state-only selectors are supposed to resolve from the profile state.
  - Screenshots and confirmed live fields are the source of truth, so wrong planned values can slip through unless detection is fixed upstream.

### External References

- None. Local code paths and saved artifacts are sufficient.

## Key Technical Decisions

- Add a Greenhouse-specific current-residence/state path ahead of the broad `do you live in / reside in / based in` branch so interrogative prompts do not inherit role-location logic.
  Rationale: the failing ezCater prompt is not a generic yes/no residency gate and not a work-location preference prompt. The current bug is caused by Greenhouse step construction choosing the wrong desired option, so the most direct fix is at that board-local seam.
- Use an explicit precedence rule for prompt routing.
  Rationale: route by meaning in this order: (1) explicit current-residence semantics, defined as an interrogative label asking where the candidate currently resides or lives and explicitly naming `state` or `province`; (2) explicit intent/availability semantics such as `intend to work`, `available to work`, `work out of`, or `open to work`; (3) field type and option semantics only as tie-breakers when label semantics are still ambiguous. If label wording and options disagree, explicit intent/availability language wins over interrogative form.
- Use candidate state variants derived from `application_profile.location` for state-only selects; do not pass `role_location` into this path.
  Rationale: for `Which state do you currently reside in?`, the JD location is irrelevant. If the profile location cannot yield a parseable state, the runtime should fail closed rather than silently picking the role state or first non-placeholder option.
- For required state-only current-residence prompts, fail closed through the existing unconfirmed-field pipeline rather than via silent fallback or uncaught exception.
  Rationale: if candidate-state extraction or option matching fails, the submitter should leave the field unresolved, let report generation mark it unconfirmed, write `submit/pending_user_input.json`, and block submit. That preserves correctness without inventing a new fatal error contract for draft runs.
- Keep `_location_option_label(..., role_location=...)` unchanged for candidate-availability / intended-work-location prompts, since that behavior is already documented and tested.
  Rationale: the repo explicitly wants role-location-first behavior for prompts like `Where do you intend to work out of?`, and weakening that rule would risk regressions on already-covered Greenhouse location selectors.
- Keep `question_classifier.py` unchanged unless implementation uncovers a second board with the same failure shape.
  Rationale: the current classifier corpus intentionally leaves interrogative current-state prompts unclassified, and neighboring boards already solve this at board-local fill time. Promoting the prompt into a shared category now would widen blast radius without evidence that the shared boundary itself is wrong. Revisit shared-helper or classifier work only if the same failure shape is confirmed on at least two boards or repeated Greenhouse cases show the board-local rule is insufficient.
- Keep the cross-board audit bounded and read-only unless another board reproduces the same failure shape.
  Rationale: the minimum audit deliverable is one explicit conclusion each for Ashby, Lever, and LinkedIn: `already handled`, `not applicable`, or `follow-up needed`. That is enough to support R5 without turning this fix into a cross-board rewrite.

## Open Questions

### Resolved During Planning

- Should the older completed Ashby plan be reopened? No. That plan fixes a different bug (`city/state` free-text questions returning `Yes`). This issue is a Greenhouse select regression driven by `role_location`.
- Is this a stale profile-data issue? No. Source files still say `San Francisco, CA`; the wrong answer comes from Greenhouse option selection.
- Is external research needed? No. The repo already contains the failing artifact, the current code path, and neighboring board patterns.

### Deferred to Implementation

- Whether the Greenhouse-local helper should later be promoted into `application_submit_common.py` after the immediate bug is fixed and audited across more boards.
- Whether any docs need wording changes beyond sync-level updates once the final implementation shape is clear.

## Alternative Approaches Considered

- Widen the shared classifier so `which state` / `current state` prompts become a cross-board deterministic category.
  Not chosen for the initial fix because the current corpus intentionally treats these Greenhouse labels as unclassified, and the failing behavior appears after classification inside Greenhouse option selection. A shared-category change would alter cross-board semantics before proving that other boards need it.
- Remove role-location preference from Greenhouse location option matching entirely.
  Rejected because the repo already documents and tests role-location-first behavior for work-location availability prompts, and that behavior is still correct for questions about where the candidate intends to work.
- Patch only the browser/runtime confirmation layer.
  Rejected because the planned value is already wrong in `draft_summary.md` and `greenhouse_autofill_report.json`; runtime confirmation would only validate the wrong choice instead of correcting it.

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```text
question label + field type
  -> explicit current-residence semantics?
       yes -> derive candidate state from application_profile.location
              -> match against state-only option labels
                 -> success: select candidate state
                 -> failure on required field: leave unresolved
                    -> existing unconfirmed/pending-user-input path
       no  -> explicit intent/availability semantics?
              -> yes: keep existing role_location-first path
              -> no: existing Greenhouse branches continue
```

## Implementation Units

- [ ] **Unit 1: Separate Greenhouse current-residence prompts from role-location prompts**

**Goal:** Stop Greenhouse from using the JD/role location for prompts that ask where the candidate currently resides.

**Requirements:** R1, R2, R5

**Dependencies:** None

**Files:**
- Modify: `scripts/autofill_greenhouse.py`
- Test: `tests/test_greenhouse_autofill.py`

**Approach:**
- Add a narrow detector/helper for interrogative current-residence prompts such as `Which state do you currently reside in?`, `Which state or province do you currently live in?`, and similar `what/which/where` variants that ask for actual candidate residence rather than role alignment.
- Apply the routing precedence explicitly: current-residence semantics first, intent/availability semantics second, field/option shape only as a tie-breaker.
- Route that path before the broad Greenhouse `do you live in / reside in / based in / located in` branch.
- For state-only selects, match against candidate state variants only; for free-text actual-residence prompts, continue to use the full `application_profile.location`.
- Treat labels like `Which state do you currently reside in?` and `Which state or province do you currently live in?` as positive examples for the new path.
- Treat labels/fragments such as `Where do you intend to work out of?`, `What cities are you available to work in?`, `available to work`, `intend to work`, `open to work`, and `work out of` as explicit exclusions that stay on the role-location path even when interrogative.
- Keep `role_location`-first behavior limited to prompts like `Where do you intend to work?` and other availability/location-preference questions.
- If candidate-state extraction or option matching fails for a required state-only prompt, do not choose a fallback option and do not raise an uncaught exception; instead, leave the field unresolved so the existing unconfirmed-field flow can emit `submit/pending_user_input.json` and block submit.

**Execution note:** Start with a failing Greenhouse unit test using the exact ezCater label and a `Boston, MA` `role_location`.

**Patterns to follow:**
- `scripts/autofill_ashby.py`
- `scripts/autofill_lever.py`
- `scripts/autofill_linkedin.py`

**Test scenarios:**
- `Which state do you currently reside in?` with options `California` / `Massachusetts` and `role_location="Boston, MA"` selects `California`.
- `Which state or province do you currently live in?` with abbreviation options `CA` / `MA` selects `CA`.
- `Where do you intend to work out of?` with state-only options still preserves existing role-location-first behavior.
- `What cities are you available to work in?` stays on the availability/location-preference path even though it is interrogative.
- `Do you currently reside in the location specified for this role?` remains on the yes/no location-residency path.
- A malformed or non-parseable profile location for a required state-only prompt does not silently choose the role state or first option; it surfaces through the existing unconfirmed/pending-user-input flow.

**Verification:**
- `_question_step()` produces a planned combobox step for `California` (or `CA`) instead of `Massachusetts`.
- Existing intended-work-location tests continue to pass without behavior changes.
- Required state-only prompts with no parseable candidate state fail closed through the existing unresolved-field path rather than a silent substitution.

- [ ] **Unit 2: Add regression coverage around overlapping Greenhouse location branches**

**Goal:** Lock in the exact bug shape so future location/residency changes cannot silently reintroduce role-location leakage.

**Requirements:** R3, R4, R5

**Dependencies:** Unit 1

**Files:**
- Modify: `tests/test_greenhouse_autofill.py`
- Test: `tests/test_greenhouse_autofill.py`

**Approach:**
- Add exact-label Greenhouse tests for the ezCater question and the Boston-vs-California mismatch.
- Add boundary tests covering interrogative current-state prompts versus yes/no location-residency prompts so the overlap stays explicit.
- Add control tests for explicit exclusion prompts such as `Where do you intend to work out of?` and `What cities are you available to work in?` so interrogative wording alone cannot steal intended-work-location flows.
- Define the minimum cross-board audit deliverable during implementation: inspect Ashby, Lever, and LinkedIn current-state/current-residence handling and record one conclusion per board (`already handled`, `not applicable`, or `follow-up needed`). Only expand scope if the same failure shape is reproduced elsewhere.

**Patterns to follow:**
- `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`
- Existing Greenhouse tests around state-only options and explicit state-list residency gates in `tests/test_greenhouse_autofill.py`

**Test scenarios:**
- Exact ezCater label remains stable under a role whose Greenhouse JD location is `Boston, MA`.
- Broad yes/no residency prompts still classify/route as location-residency checks.
- Interrogative current-state prompts do not fall back to the same branch as role-location availability prompts.
- Required unresolved state-only prompts produce the existing unresolved/pending-user-input outcome rather than a silent fallback selection.

**Verification:**
- Regression tests fail on the pre-fix code path and pass once the new Greenhouse routing is in place.
- The intended boundary between interrogative current-state prompts, yes/no residency prompts, and intended-work-location prompts is encoded in tests.
- The bounded cross-board audit yields one explicit conclusion per inspected board without silently widening implementation scope.

- [ ] **Unit 3: Preserve runtime/report parity across draft and live Greenhouse fills**

**Goal:** Ensure the corrected planned answer is what the browser runtime actually selects and what downstream artifacts report.

**Requirements:** R1, R3

**Dependencies:** Unit 1

**Files:**
- Modify: `scripts/autofill_greenhouse.py`
- Test: `tests/test_greenhouse_autofill.py`

**Approach:**
- Ensure the corrected state selection sets the planned `option`/`search` values the live combobox fill path expects, rather than planning one value and confirming another.
- Reuse existing option-matching helpers where safe, but keep the desired value narrowed to the candidate state for actual-residence prompts.
- Preserve existing report generation and `planned_but_unconfirmed_fields` behavior so semantic mismatches are corrected at step construction time instead of being treated as confirmation gaps.
- Validate the downstream artifact chain that consumes the planned step: Greenhouse report JSON/markdown, draft summary generation, and pending-user-input handling for any unconfirmed required location field.

**Patterns to follow:**
- Existing Greenhouse report and confirmation handling in `scripts/autofill_greenhouse.py`
- `scripts/autofill_pipeline.py`
- `scripts/draft_manager.py`
- `docs/autofill-patterns.md` rules for confirmed location fields and fail-closed behavior

**Test scenarios:**
- Planned combobox answer for the current-state prompt matches the value the live fill code would search/select.
- The corrected state prompt does not end up in `planned_but_unconfirmed_fields`.
- Draft/report output records `California` / `CA` for the prompt instead of `Massachusetts`.
- When candidate-state extraction or matching fails for a required state-only prompt, the field remains unresolved and flows into the existing `planned_but_unconfirmed_fields` / `pending_user_input` path.

**Verification:**
- A regenerated draft/report for the ezCater role would show the current-state answer as California/CA.
- No new unconfirmed-field blockers appear for the corrected prompt.
- The regenerated `submit/greenhouse_autofill_report.json`, root `draft_summary.md`, and pre-submit screenshot for the ezCater role all agree on the corrected state.

## System-Wide Impact

- **Interaction graph:** `jd_parsed.json` role location feeds `scripts/autofill_greenhouse.py::_question_step()`, which builds planned steps consumed by Greenhouse browser fill, `scripts/autofill_pipeline.py` report writing, `scripts/draft_manager.py` summary generation, and screenshot review flows.
- **Error propagation:** A wrong planned option is treated as a successful fill, so the error propagates through draft artifacts and live screenshots without triggering fail-closed unknown-question handling.
- **State lifecycle risks:** Cached draft artifacts and reruns can preserve the wrong answer until the planning step is corrected; remote roles with HQ-based Greenhouse metadata are the highest-risk shape.
- **API surface parity:** CLI, TUI, worker, and web all share the same Greenhouse submitter, so one Greenhouse fix covers every surface. Cross-board audit should confirm Ashby, Lever, and LinkedIn do not need parallel changes.
- **Audit scope:** this fix includes a bounded read-only audit of Ashby, Lever, and LinkedIn current-residence handling. Any reproduced parallel bug becomes explicit follow-on work instead of being silently folded into this implementation.
- **Integration coverage:** Unit tests are necessary but not sufficient; the post-fix draft/report for the ezCater role should be re-generated during execution to confirm the real artifact and screenshot now show California/CA.

## Risks & Dependencies

- Over-broad interrogative matching could steal prompts that are actually about preferred work location or office selection.
  Mitigation: use the explicit precedence rule, add exclusion tests for `intend to work` / `available to work` prompts, and treat label semantics as primary with options only as tie-breakers.
- A future maintainer could over-interpret this Greenhouse-local fix as a permanent argument against shared-helper or classifier consolidation.
  Mitigation: document the promotion threshold explicitly: revisit shared work only when the same failure shape is confirmed on at least two boards or repeated Greenhouse incidents show the board-local rule is insufficient.
- If `application_profile.location` loses a parseable state component, the new path could regress back to role-location leakage or first-option selection.
  Mitigation: for required state-only prompts, leave the field unresolved and rely on the existing `planned_but_unconfirmed_fields` / `pending_user_input` contract instead of any fallback option or uncaught exception.
- Cached draft artifacts may obscure whether the real browser flow is fixed.
  Mitigation: regenerate the ezCater draft/report artifacts during execution and compare the updated draft summary, report JSON, and screenshot rather than relying on old output.

## Documentation / Operational Notes

- The current docs already promise actual-state behavior for Greenhouse residency gates. If implementation adds a new prompt class (explicit interrogative current-state prompts), sync `README.md` and `docs/autofill-patterns.md` only as needed to describe that narrower rule without duplicating existing promises.
- If those docs change, keep generated agent-instruction mirrors aligned through the existing AGENTS-derived sync flow rather than documenting the behavior in only one surface.
- During execution, validate the fix using the ezCater draft flow because screenshots are the source of truth for autofill results.

## Sources & References

- Related code: `scripts/autofill_greenhouse.py`
- Related code: `scripts/autofill_ashby.py`
- Related code: `scripts/autofill_lever.py`
- Related code: `scripts/autofill_linkedin.py`
- Related tests: `tests/test_greenhouse_autofill.py`
- Related artifact: `output/ezcater-inc/principal-pm-finance-platform-remote/draft_summary.md`
- Related artifact: `output/ezcater-inc/principal-pm-finance-platform-remote/content/jd_raw.md`
- Related docs: `docs/autofill-patterns.md`
- Related docs: `docs/board-architecture.md`
- Related learning: `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`
