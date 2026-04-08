---
title: "fix: Lever autofill leaves compensation, pronouns, age, and NDA fields empty"
type: fix
status: completed
date: 2026-03-24
deepened: 2026-03-24
---

# fix: Lever autofill leaves compensation, pronouns, age, and NDA fields empty

## Overview

Several Lever form fields are left empty during autofill on the Aircall (senior-pm-growth-for-small-businesses) application: compensation, pronouns, and age. Additionally, `nda_noncompete` has the same routing gap as compensation. Each has a distinct root cause in the field routing and step inference pipeline.

## Problem Statement

The autofill system uses a two-tier routing architecture:
- **Tier 1 (`_build_payload`, line 941-965):** splits fields into `generated_candidates` (sent to LLM) vs `deterministic_fields`. Uses `classify_question()` as a gate — if the classifier returns non-None, the field is routed to the deterministic path.
- **Tier 2 (`_infer_step`):** runs board-specific label matching plus `_step_from_classifier`. A field classified non-None in Tier 1 **must** produce a step in Tier 2, otherwise it becomes `unknown_required` and raises `ValueError`.

**The architecture invariant is: classification at Tier 1 and handling at Tier 2 must be symmetric.** Four fields currently violate this:

### 1. Compensation (CRITICAL — blocks submission)

`classify_question()` returns `"compensation"` (P5), which excludes the field from LLM generation at line 961. But `_step_from_classifier` line 442-444 says compensation is "handled elsewhere" and returns None. There is no "elsewhere" — `_infer_step` has zero handling for compensation text fields. The field becomes `unknown_required`, raising `ValueError` and blocking submission on Ahead and FloQast forms.

### 2. NDA/Non-Compete (same routing gap)

`classify_question()` returns `"nda_noncompete"` (P2). `_step_from_classifier` line 442 says it's "handled elsewhere" and returns None. Searching the entire `_infer_step` function: **no NDA/non-compete handling exists**. Same gap as compensation.

### 3. Pronouns (text/textarea variant)

The pronoun handler at `autofill_lever.py:611` calls `_select_option()` → `select_option(field.get("options"), ...)`. For text/textarea fields, `field.get("options")` is `[]`, so `_select_option` always returns None, and `_choice_step` returns None. On Lever, pronouns appear as text/textarea inputs (confirmed in Aircall and Match Group unknown questions).

### 4. Age (radio/checkbox — no handler)

No age handler exists in `_infer_step`. No age detector in `question_classifier.py`. No age field in `application_profile.md`. Age confirmation questions become unknown. Note: radio/checkbox/select fields bypass the LLM routing gate entirely (line 943 checks `field["kind"] in {"text", "textarea"}`), so these can never reach the LLM either.

---

## Proposed Solution

### Fix 1: Wire up compensation handling in `_step_from_classifier` (CRITICAL)

**File:** `scripts/autofill_lever.py` — `_step_from_classifier` function (line 390-445)

Add a `"compensation"` case for text/textarea fields that returns a step with the deflection text. Import `_COMPENSATION_NEGOTIABLE_ANSWER` from `application_submit_common.py:1455` — do **not** create a new constant (there are already 7 duplicated copies across board scripts).

```python
# In _step_from_classifier, after existing handlers:
if category == "compensation":
    if field["kind"] in {"text", "textarea"}:
        return {**base, "kind": field["kind"],
                "value": _COMPENSATION_NEGOTIABLE_ANSWER,
                "source": "application_profile.md"}
    return None  # select/radio compensation not seen on Lever; handle when encountered
```

**Why this works:** The classifier already correctly identifies compensation. The only gap is `_step_from_classifier` doesn't act on it. Adding the handler closes the Tier 1/Tier 2 routing loop.

#### Research Insights

**Cross-board pattern (verified in 6 boards):**

| Board | Function | Constant | Source tracking |
|-------|----------|----------|----------------|
| Dover | `_answer_from_classifier` (line 172) | `_COMPENSATION_DEFLECT` (local) | No |
| LinkedIn | `_linkedin_answer_from_category` (line 664) | `_COMPENSATION_DEFLECT` (local) | No |
| Workday | `_answer_from_classifier` (line 1376) | `_COMPENSATION_DEFLECT` (local) | No |
| iCIMS | `_answer_from_classifier` (line 1084) | `_COMPENSATION_DEFLECT` (local) | Yes (tuple) |
| BambooHR | `_answer_from_classifier_text` (line 133) | `_COMPENSATION_DEFLECT` (local) | No |
| Phenom | `_try_deterministic_answer` (line 1612) | Inline text | Yes (dict) |

All use identical deflection text. All handle text/textarea only. Lever's fix follows this established pattern using `_step_from_classifier` (Lever's native dispatch mechanism, which returns step dicts rather than plain strings).

**Numeric compensation note:** `application_profile.md` says "If the field requires a numeric-only amount, enter 1000" but no board implements this. If a Lever form validates for numeric-only, the deflection text would fail silently. Accept as known limitation.

### Fix 1b: Wire up NDA/non-compete handling (same function, same change)

**File:** `scripts/autofill_lever.py` — `_step_from_classifier` function

Add an `"nda_noncompete"` case. All other boards that handle this return "No" for yes/no fields.

```python
if category == "nda_noncompete":
    return _yes_no_step(field, value=False, source="deterministic")
```

**Why this works:** Same routing gap pattern as compensation. Since we're already modifying `_step_from_classifier`, fix both gaps in one change.

### Fix 2: Add text/textarea branch to pronoun handler

**File:** `scripts/autofill_lever.py` — `_infer_step` pronoun section (line 611-622)

Add a text/textarea branch before the existing choice-type handler:

```python
if _label_matches(field, "pronoun", "pronouns"):
    if field["kind"] in {"text", "textarea"}:
        return {**base, "kind": field["kind"],
                "value": application_profile.pronouns or "",
                "source": "application_profile.md"}
    # existing choice-type handler follows...
    raw_pronouns = application_profile.pronouns or ""
    candidates = [raw_pronouns]
    # ...
```

**Why this works:** "What are your pronouns?" has exactly one deterministic answer from the profile. No LLM needed.

#### Research Insights

**Cross-board text pronoun handling (only 2 boards):**
- **Greenhouse** (line 3130-3135): Conditional — `multi_value_single_select` → option matching; else → `value = application_profile.pronouns` directly
- **Reducto** (line 109-115): Always uses raw value pass-through for text kind

**Boards with NO text/textarea pronoun support:**
- **Lever** (line 611): Choice-type only — this fix
- **Ashby** (line 657): `ValueSelect`/`MultiValueSelect` only
- **Gem** (line 340): Radio only, no fallback

**Generalization follow-up:** Ashby and Gem should also get text/textarea support. File as separate issue per simplicity — ship the Lever fix first.

### Fix 3: Add age handler in `_infer_step` (board-local)

**File:** `scripts/autofill_lever.py` — `_infer_step` function

Add a `_label_matches` block for age questions, following the existing pattern at lines 611-625. Use `_select_option` with "prefer not to say" candidates. **Do NOT add to the unified classifier** — all 4 existing boards handle age board-locally, and two semantically different question types exist.

```python
if _label_matches(field, "age group", "age range", "your age", "age:"):
    option = _select_option(field, [
        "Prefer not to say",
        "I prefer not to say",
        "I don't wish to answer",
        "I do not wish to answer",
    ])
    return _choice_step(field, option, source="deterministic")
```

If `_select_option` returns None (e.g., VGS options have only numeric ranges with no "prefer not to say"), `_choice_step` returns None, and the field surfaces as unknown for manual review. This is the correct behavior — selecting an arbitrary age range would disclose information the user wants to decline.

#### Research Insights

**Why board-local, not unified classifier:**
1. Two semantically different question types exist across boards:
   - **Age verification** ("Are you at least 18?"): Answer is "Yes" (Workable, Rippling, Phenom)
   - **Age demographic** ("What age group?"): Answer is "Prefer not to say" (Greenhouse, Lever)
2. Lumping both into one classifier category would produce wrong answers for half the boards
3. All 4 existing implementations are board-local — adding to classifier is premature generalization
4. No regression corpus entries needed, no false-positive risk analysis needed

**Cross-board age patterns (verified):**

| Board | Pattern | Answer | Handler |
|-------|---------|--------|---------|
| Greenhouse (line 5527) | "age group", "which age group" | "I don't wish to answer" | Inline demographic spec |
| Workable (line 98) | "18 years", "legal age" | "Yes" | `_infer_deterministic` |
| Rippling (line 98) | "18 years", "legal age" | "Yes" | `_infer_deterministic` |
| Phenom (line 1663) | "legal age", "legally eligible", "18 years" | "Yes" (dict) | `_try_deterministic_answer` |

Lever's questions (from Aircall unknown_questions) are the demographic variant ("Age:" with range options), so "Prefer not to say" is the correct answer — consistent with Greenhouse.

---

## Technical Considerations

- **Tier 1/Tier 2 symmetry:** Both compensation and nda_noncompete fixes close routing gaps where the classifier claims a field but no handler produces a step. After this fix, audit remaining "handled elsewhere" categories (`city_location`, `work_authorization`) for similar gaps.
- **React re-render risk:** Lever uses React forms. Choice/button fills after textarea fills can trigger re-renders that clear textarea values. Phase 2b re-verification handles this, but be aware.
- **Constant import:** Import `_COMPENSATION_NEGOTIABLE_ANSWER` from `application_submit_common`; do not duplicate.
- **No classifier changes:** Age is handled board-locally. No regression corpus entries needed. No null-to-classified drift risk.

## System-Wide Impact

- **Interaction graph:** `_step_from_classifier` runs inside `_infer_step` which runs inside `_build_payload`. Changes are additive (new `if` branches) — no existing branches affected.
- **Error propagation:** Compensation/NDA fields marked `required: true` currently raise `ValueError` in `_build_payload`. The fix converts hard failures to successful fills.
- **API surface parity:** Compensation now handled in 7/9+ boards (Greenhouse still gaps — separate issue). NDA handling added for Lever. Age handled on Lever following Greenhouse pattern.
- **State lifecycle:** No persistent state changes. All fixes are in the per-run payload generation path.
- **Integration test scenarios:**
  1. Lever form with required compensation textarea → fill with deflection text
  2. Lever form with NDA yes/no question → fill with "No"
  3. Lever form with text pronoun field → fill with profile pronouns
  4. Lever form with age radio including "Prefer not to say" → select it
  5. Lever form with age radio WITHOUT "Prefer not to say" → return None (unknown)
  6. Checkbox/radio/select pronoun fields → no regression

## Acceptance Criteria

- [ ] Compensation text/textarea fields filled with deflection text
- [ ] Required compensation fields no longer raise `ValueError`
- [ ] NDA/non-compete yes/no fields filled with "No"
- [ ] Text/textarea pronoun fields filled with `application_profile.pronouns`
- [ ] Checkbox/radio/select pronoun fields still work (no regression)
- [ ] Age fields with "Prefer not to say" option are handled
- [ ] Age fields without safe option surface as unknown (not errored)
- [ ] `_COMPENSATION_NEGOTIABLE_ANSWER` imported from `application_submit_common`, not duplicated
- [ ] All existing Lever autofill tests pass
- [ ] All existing classifier regression corpus passes (no classifier changes)

## Success Metrics

- Aircall Lever autofill: 0 unknown fields for compensation, pronouns, NDA (currently 3+)
- Age fields handled when "Prefer not to say" available
- No regressions in existing tests

## Dependencies & Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Compensation select/radio variant exists on Lever | Low | Not seen in any real Lever form. Return None for non-text kinds; handle when encountered. |
| Pronoun text fill wrong for broader questions ("How would you like to be addressed?") | Low | Profile value is the standard answer. LLM fallback still works for unusual questions. |
| Age "prefer not to say" not available | Medium | `_choice_step` returns None → field surfaces as unknown. Correct behavior. |
| NDA answer should be "Yes" on some forms | Low | All 5 boards that handle NDA return "No". If a form asks "Do you agree to NDA?" (positive framing), the label would need distinct detection. |

**Dependency:** `application_profile.md` must have `Pronouns: He / Him / His` (currently present).

## Implementation Order

1. **Fix 1 + 1b: Compensation + NDA** in `_step_from_classifier` — highest priority, same function, one change
2. **Fix 2: Pronouns text/textarea** in `_infer_step` — quick win, ~3 lines
3. **Fix 3: Age handler** in `_infer_step` — board-local `_label_matches`, ~5 lines

## Follow-up Issues (out of scope)

- [ ] Extract `_COMPENSATION_DEFLECT` across all 7 board scripts to import from `application_submit_common`
- [ ] Add text/textarea pronoun support to Ashby and Gem
- [ ] Audit `city_location` and `work_authorization` partial coverage in Lever
- [ ] Add Tier 1/Tier 2 symmetry CI test (prevent this bug class permanently)
- [ ] Fix same compensation + NDA gaps in Gem's `_step_from_classifier`
- [ ] Migrate inline age handlers (Greenhouse, Workable, Rippling, Phenom) to a consistent pattern

## Sources & References

### Internal References
- Lever autofill: `scripts/autofill_lever.py` — `_build_payload` (line 902), `_infer_step` (line 448), `_step_from_classifier` (line 390), `_fill_step` (line 1113)
- Shared constant: `scripts/application_submit_common.py:1455` — `_COMPENSATION_NEGOTIABLE_ANSWER`
- Unified classifier: `scripts/question_classifier.py` — 14 priority-ordered detectors (P1-P14)
- Compensation in other boards: Dover:172, LinkedIn:664, Workday:1376, iCIMS:1084, BambooHR:133, Phenom:1612
- Age in other boards: Greenhouse:5527, Workable:98, Rippling:98, Phenom:1663
- Pronouns text/textarea precedent: Greenhouse:3130, Reducto:109

### Learnings Applied
- `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md` — classifier priority ordering, null-to-classified drift prevention
- `docs/autofill-patterns.md` — pronoun candidate expansion, React re-render clearing, salary comfort vs compensation ordering

---

<details>
<summary>Enhancement Summary (deepening audit trail)</summary>

**Deepened on:** 2026-03-24
**Research agents used:** repo-research-analyst, learnings-researcher, spec-flow-analyzer, architecture-strategist, code-simplicity-reviewer, pattern-recognition-specialist, 4x Explore agents (compensation/age/pronoun cross-board patterns, classifier architecture)

**Key changes from research:**
1. Discovered `nda_noncompete` has the identical routing gap — added as Fix 1b
2. Simplified age handling to board-local `_label_matches` (all 4 existing boards handle age locally; two semantically different question types make classifier unification premature)
3. Dropped Fix 4 (top textarea) — unconfirmed, speculative
4. Corrected pronoun diagnosis — failing fields are text/textarea, not checkbox
5. Compensation constant location corrected — it's in `application_submit_common.py:1455`, not Lever

**New considerations:** Tier 1/Tier 2 symmetry has no CI enforcement; Gem has same gaps; `city_location` has partial coverage risk.

</details>
