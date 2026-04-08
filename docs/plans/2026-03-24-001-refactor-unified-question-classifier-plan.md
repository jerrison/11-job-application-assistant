---
title: "refactor: Unified question classifier to prevent autofill regressions"
type: refactor
status: completed
date: 2026-03-24
origin: docs/brainstorms/2026-03-24-unified-question-classifier-requirements.md
---

# refactor: Unified question classifier to prevent autofill regressions

## Enhancement Summary

**Deepened on:** 2026-03-24
**Review agents used:** Architecture Strategist, Code Simplicity Reviewer, Pattern Recognition Specialist, Performance Oracle

### Key Improvements
1. Extract classifier to new `scripts/question_classifier.py` — keeps `application_submit_common.py` from growing beyond its current ~1,700 lines
2. Merged Phases 3+4 into single Board Migration phase — eliminated duplicated plan text
3. Simplified Phase 4 (was 5) to direct deletion — no safety-net pre-filter or deprecation wrapper
4. Added performance fix: pre-normalize constant fragments in `question_is_current_company_field()` (25 redundant `normalize_text()` calls per invocation)
5. Added normalize-once pattern for the classifier chain — avoids 13 redundant normalizations per field

### New Considerations Discovered
- Greenhouse has a **parallel copy** of `generate_application_answers` (`_generate_answers_for_questions` at `autofill_greenhouse.py:2618-2699`) with 3 additional `apply_generated_answer_overrides` call sites — Phase 4 must cover these
- `apply_generated_answer_overrides()` also handles `draft_overrides.json` (user edits) — this is NOT classification and must be preserved as a separate concern
- Tests must use `load_module()` Variant B (with `sys.modules` registration) per existing convention in 21 test files
- Dataclass must use `@dataclass(slots=True)` to match `CandidateProfile` and `ApplicationProfile`

## Overview

Consolidate 13 scattered question detectors and per-board detection chains into a single `classify_question()` entry point with priority-ordered dispatch, backed by a 1,152-label regression test corpus harvested from real autofill runs. (see origin: docs/brainstorms/2026-03-24-unified-question-classifier-requirements.md)

## Problem Statement

Every autofill bug fix risks breaking existing behavior. The education fix touched 9 files. The city/state "live in" bug, salary comfort vs. expectation overlap, and education-falls-to-cover-letter bug all stem from the same root cause: fragile, order-dependent, per-board detection chains with no overlap testing. 5 of 13 detectors have zero test coverage. `apply_generated_answer_overrides()` wastes LLM tokens by generating answers for questions it then deterministically replaces.

## Proposed Solution

### `classify_question()` — Single Entry Point

```python
# scripts/question_classifier.py (NEW FILE)

from dataclasses import dataclass

@dataclass(slots=True)
class QuestionClassification:
    category: str          # e.g., "education", "salary_comfort", "nda_noncompete"
    value: str | None      # deterministic answer, or None if board should compute
    source: str            # e.g., "application_profile.md", "deterministic"

def classify_question(
    label: str,
    field_type: str | None = None,
    application_profile: ApplicationProfile | None = None,
    out_dir: Path | None = None,
) -> QuestionClassification | None:
    """Classify a question label and return its deterministic answer.

    Returns None if no detector matches (field should fall through to LLM).
    Detectors run in priority order — first match wins.

    Most fields are handled by field-name routing (name, email, phone, LinkedIn,
    demographics) in the board's _infer_step() before reaching this function.
    This classifier is only called for unresolved question-type fields.
    """
    normalized = normalize_text(label)
    for detector in _DETECTOR_CHAIN:
        result = detector(normalized, field_type, application_profile, out_dir)
        if result is not None:
            return result
    return None
```

### Research Insights — API Design

- **Normalize once, pass to all detectors.** Each detector currently calls `normalize_text(label)` independently. The classifier normalizes once and passes the pre-normalized string, avoiding 13 redundant normalizations per field. Each detector gains a `_pre_normalized` parameter or is restructured to accept pre-normalized input.
- **Performance is not a concern.** Running all 13 detectors sequentially takes ~49μs per field. For 50 fields at 2 call sites, that's ~5ms total — four orders of magnitude below Playwright DOM operations (10-100ms each).
- **Per-form classification cache** (optional but recommended). A `dict[tuple[str, str | None], QuestionClassification | None]` initialized at the start of each form-filling loop guarantees consistency between `_infer_step()` and the LLM candidate filter. Not for speed — for correctness.
- **Boards can override the value.** The classifier returns a default `value`, but boards like Greenhouse that need option-matching can use the `category` to decide on their own answer. This preserves the origin decision (category + value) while accommodating board-specific needs.

### Priority-Ordered Detector Chain

Detectors run in this order. First match wins. More specific patterns before broader ones.

| Priority | Category | Detector | Keywords/Pattern | Answer |
|----------|----------|----------|-----------------|--------|
| 1 | `current_company` | `question_is_current_company_field()` | field name match + label fragments; excludes background check, NDA | primary employer name |
| 2 | `nda_noncompete` | `_question_is_nda_noncompete()` | non-compete, non-disclosure, restrictive covenant | "No" |
| 3 | `reasonable_accommodation` | `question_is_reasonable_accommodation_check()` | reasonable accommodation, essential functions | "Yes" |
| 4 | `salary_comfort` | `question_is_salary_comfort_check()` | two-step gate: exact fragments OR short + "comfortable"/"interview" | "Yes" |
| 5 | `compensation` | `_question_is_compensation()` | salary expectation, desired salary, expected compensation | deflection text |
| 6 | `minimum_experience` | `question_is_minimum_experience_check()` | minimum years, regex for "at least N years" | "Yes" |
| 7 | `experience_confirmation` | `question_is_experience_confirmation()` | "have you shipped/built/launched"; excludes "describe", "tell us" | "Yes" |
| 8 | `product_usage` | `_question_is_product_usage()` | "have you used/tried"; excludes elaboration | "Yes" |
| 9 | `city_location` | `_question_is_city_location()` | what cities, which office, preferred location | profile city |
| 10 | `office_attendance` | `question_is_office_attendance_prompt()` | days per week, hybrid, regex for "Nx/week" | "Yes" |
| 11 | `company_engagement` | `_question_is_company_engagement()` | engaged with, interacted with | product > blog |
| 12 | `culture_careers_optin` | `question_is_culture_careers_optin()` | stay up to date, talent community, future opportunities | "Yes" |
| 13 | `education` | `question_is_education()` | education, degree, college; excludes background check | formatted degrees |

**Key ordering rationale:**
- `salary_comfort` (P4) before `compensation` (P5): the two-step gate prevents comfort checks from being consumed by the broader compensation detector
- `current_company` (P1) before everything: uses field_name as primary match, most specific
- `education` (P13) last: broadest keywords, most overlap risk

### Research Insights — Performance Fix

**`question_is_current_company_field()` has 25 redundant `normalize_text()` calls per invocation** (`application_submit_common.py:1251-1269`). Since it runs at Priority 1 for every field, this is the highest-frequency normalization waste. Fix by pre-normalizing constant fragment tuples at module load time:

```python
_CURRENT_COMPANY_EXCLUSION_NORMALIZED = tuple(
    normalize_text(exc) for exc in _CURRENT_COMPANY_EXCLUSION_FRAGMENTS
)
_CURRENT_COMPANY_LABEL_NORMALIZED = tuple(
    normalize_text(f) for f in CURRENT_COMPANY_LABEL_FRAGMENTS if f
)
```

This drops from 25 `normalize_text` calls to 1 per invocation. Across 50 fields, that eliminates ~1,200 unnecessary calls per form.

## Technical Approach

### Implementation Phases

#### Phase 1: Regression Test Corpus + Missing Tests

**Goal:** Build the safety net before changing any detection logic.

**Files:**
- `tests/fixtures/question_label_corpus.json` (new) — harvested labels with expected classifications
- `tests/test_question_classifier.py` (new) — corpus-driven + hand-written overlap tests

**Steps:**
1. Run a one-time harvest script (not committed — write as `/tmp/harvest_labels.py` or inline) to scan all `*_autofill_report.json`, `*_autofill_payload.json`, and `*_unknown_questions.json` in `output/`. Extract unique `(label, board)` pairs.
2. Run each label through all 13 existing detectors to establish current classification as ground truth. Save as `tests/fixtures/question_label_corpus.json`.
3. Hand-write overlap edge cases (mandatory regardless of corpus size): "education background check", "comfortable with salary range" vs "desired salary range", "currently live in [city]" vs "do you currently live in the Bay Area", "sponsorship" + "authorized to work" in same label.
4. Write `test_question_classifier.py` using `load_module()` Variant B (with `sys.modules` registration, matching `tests/test_submit_application.py:14-21`). Tests run the full corpus against `classify_question()` and assert each label gets its expected classification. Also flags multi-match labels.
5. Add unit tests for the 5 untested detectors: `_question_is_product_usage`, `_question_is_city_location`, `_question_is_company_engagement`, `question_is_reasonable_accommodation_check`, `question_is_experience_confirmation`.

**Execution note:** Test-first. Write the corpus test targeting `classify_question()` before the function exists — it will fail. Then Phase 2 makes it pass.

**Verification:** All 5 previously-untested detectors have tests. Corpus test exists and establishes baseline. `uv run python -m pytest tests/test_question_classifier.py -v` shows the expected failure for `classify_question` not existing yet, plus passing detector unit tests. **Gate:** 100% exact match on corpus for deterministic categories before proceeding to Phase 3.

---

#### Phase 2: Unified Classifier

**Goal:** Create `classify_question()` with priority-ordered dispatch. Make the corpus test pass.

**Files:**
- `scripts/question_classifier.py` (new) — `QuestionClassification` dataclass + `classify_question()` function
- `scripts/application_submit_common.py` — re-export `classify_question` for backward compatibility
- `scripts/check_architecture.py` — add `question_classifier.py` to dependency enforcement rules

**Steps:**
1. Create `scripts/question_classifier.py` with `QuestionClassification` dataclass (using `@dataclass(slots=True)`) and `classify_question()` function.
2. Implement `classify_question()` as a linear chain calling existing detector functions in priority order. Normalize the label once at the top; pass pre-normalized string to detectors.
3. Each detector match returns `QuestionClassification(category=..., value=..., source=...)`.
4. No match returns `None` (fallthrough to LLM).
5. Fix `question_is_current_company_field()` to pre-normalize constant fragments at module load time.
6. Keep per-detector exclusion lists as defense-in-depth (e.g., education's "background check" exclusion). Priority ordering is the primary conflict resolution; exclusions are backup.
7. Update `check_architecture.py` to enforce dependency rules for the new module.

**Execution note:** Characterization-first. Run the corpus test to verify `classify_question()` produces identical results to the current per-board detection chains. Any deviation is a bug in the new code, not an intentional change.

**Patterns to follow:**
- `@dataclass(slots=True)` matching `CandidateProfile` and `ApplicationProfile` (`application_submit_common.py:96,110`)
- Two-step gate pattern in `question_is_salary_comfort_check()` (`application_submit_common.py:1470-1487`)

**Verification:** `uv run python -m pytest tests/test_question_classifier.py -v` all green. Every corpus label gets the same classification as before.

---

#### Phase 3: Board Migration

**Goal:** All boards call `classify_question()` instead of inline detection chains.

**Files:**
- Tier 1 (payload-based): `autofill_lever.py`, `autofill_ashby.py`, `autofill_gem.py`, `autofill_dover.py`, `autofill_greenhouse.py`
- Tier 2 (browser-pipeline): `autofill_phenom.py`, `autofill_workday.py`, `autofill_icims.py`, `autofill_linkedin.py`, `autofill_bamboohr.py`

**Steps:**

For each board (one at a time, commit after each):
1. In `_infer_step()` / `_try_deterministic_answer()` / equivalent, find the block of question-type checks.
2. Replace with a single call:
   ```python
   classification = classify_question(label, field_type, application_profile, out_dir)
   if classification:
       return {**base, "kind": field_kind, "value": classification.value, "source": classification.source}
   ```
3. Keep non-question field routing (name, email, phone, LinkedIn, demographics, location, file uploads) untouched — these are field-name matches, not question classification.
4. In the LLM candidate filter, replace individual `not question_is_*()` checks with `classify_question(label, ...) is not None`.
5. Remove now-unused individual detector imports.

**Board-specific notes:**
- **Dover first** — simplest board, good canary for the migration pattern.
- **Greenhouse last** — has its own parallel detection system (`_question_is_deterministic()` at line 2082, `_question_requires_generated_answer()` at line 2094, `_question_matches()` wrapper). These composite functions must be replaced, not just the individual detector calls. Greenhouse also wraps shared detectors with `as shared_question_is_*` aliases (lines 49-53) — remove those.
- **Tier 2 boards** — the `classify_question()` call stays inside the browser loop. Only the classification logic changes, not the call site structure. Phenom's inline salary detection (`_try_deterministic_answer` line 1500 checks `"comfortable with" in label_lower` directly) must be replaced with the classifier call.
- **Boards not listed** (SmartRecruiters, Workable, Comeet, Rippling, Uber, Motion Recruitment, Reducto, Eightfold) — these have no LLM fallback and 0-2 detector call sites each. Migrate opportunistically if they import individual detectors; skip if they don't.

**Execution note:** One board at a time. Run the full test suite after each migration. Commit after each board passes.

**Verification:** `uv run python -m pytest tests/ -v` — no regressions. Corpus test still passes. Board-specific tests still pass.

---

#### Phase 4: Delete Post-Generation Overrides

**Goal:** Remove `apply_generated_answer_overrides()` now that boards pre-filter deterministic questions. (see origin R6)

**Files:**
- `scripts/application_submit_common.py` — delete `apply_generated_answer_overrides()` and its 3 call sites (lines 1140, 1154, 1206)
- `scripts/autofill_greenhouse.py` — delete `apply_generated_answer_overrides` call sites in `_generate_answers_for_questions()` (lines 2618, 2634, 2699)

**Steps:**
1. Phase 3 already excludes classified questions from reaching `generate_application_answers()`. Verify by adding a warning log inside `generate_application_answers()` that fires if any incoming question_spec matches `classify_question()`. Run the test suite — if no warnings fire, the board migration is complete.
2. **Preserve draft override logic.** `apply_generated_answer_overrides()` handles `draft_overrides.json` at lines 1426-1434. This is user edit application, NOT question classification. Extract this into a standalone `apply_draft_overrides(question_specs, generated_answers, out_dir)` function before deleting the parent function.
3. Delete `apply_generated_answer_overrides()` and update all 6 call sites (3 in `application_submit_common.py`, 3 in `autofill_greenhouse.py`) to call only `apply_draft_overrides()`.
4. Remove the now-dead detector code that was only used inside `apply_generated_answer_overrides()`.

**Execution note:** The warning log from step 1 is an observability check, not a silent filter. If it fires, that means a board migration in Phase 3 missed a code path — fix the board, don't add redundant filtering.

**Verification:** Warning log produces zero warnings during test suite. `apply_generated_answer_overrides()` is deleted. Draft overrides still work (test with a `draft_overrides.json` fixture). Full test suite passes.

## Acceptance Criteria

- [ ] `classify_question()` exists in `scripts/question_classifier.py` and returns `QuestionClassification | None`
- [ ] `QuestionClassification` uses `@dataclass(slots=True)` matching existing conventions
- [ ] Priority order is documented in code (docstring or constant) and matches the plan
- [ ] Regression test corpus contains 1,000+ real labels with expected classifications
- [ ] Hand-written overlap tests cover: education + background check, salary comfort + salary expectation, live-in yes/no + live-in free-text, sponsorship + work authorization
- [ ] All 13 detectors have unit tests (5 previously untested now covered)
- [ ] Tests use `load_module()` Variant B with `sys.modules` registration
- [ ] All boards with LLM fallback call `classify_question()` for question-type routing
- [ ] `apply_generated_answer_overrides()` is deleted; draft overrides preserved separately
- [ ] Adding a new detector requires changes to ONE file (`question_classifier.py`) + a test entry
- [ ] No existing label changes classification without an explicit test update
- [ ] `check_architecture.py` enforces dependency rules for `question_classifier.py`
- [ ] All existing tests pass with no regressions

## Success Metrics

- New detector addition: 1 file changed (was 9+)
- Regression detection: corpus test catches any classification change automatically
- LLM token savings: ~250-500 prompt tokens and ~100-250 response tokens saved per application (5 deterministic questions no longer sent to LLM)
- Performance: `question_is_current_company_field()` drops from 25 to 1 `normalize_text()` calls per invocation

## Dependencies & Risks

- **Risk: Large refactor scope.** Mitigated by phased approach — each phase is independently valuable and committable. Phase 1 (tests) provides value even if later phases are deferred.
- **Risk: Corpus labels may have incorrect ground-truth classifications.** Mitigated by the characterization-first approach — we capture current behavior as ground truth, not ideal behavior. Known incorrect classifications are fixed separately.
- **Risk: Greenhouse has the most complex migration** due to parallel detection system (`_question_is_deterministic`, `_question_requires_generated_answer`, `_question_matches` wrappers, its own `_generate_answers_for_questions`). Mitigated by migrating it last within Phase 3.
- **Risk: `draft_overrides.json` logic entangled with `apply_generated_answer_overrides()`.** Mitigated by extracting draft overrides into a standalone function before deletion.

## Scope Boundaries

- Not redesigning how boards fill fields (DOM interaction, API payloads) — only centralizing classification + answer decision (see origin)
- Not changing the LLM generation pipeline itself — only what gets sent to it (see origin)
- Not adding new question detectors — only consolidating existing ones (see origin)
- Greenhouse structured education fields (school/degree/discipline dropdowns) remain board-specific (see origin)
- Boards without LLM fallback (SmartRecruiters, Workable, Comeet, Rippling, Uber, Motion Recruitment, Reducto, Eightfold) — migrate opportunistically, not required

## Sources & References

### Origin

- **Origin document:** [docs/brainstorms/2026-03-24-unified-question-classifier-requirements.md](docs/brainstorms/2026-03-24-unified-question-classifier-requirements.md) — Key decisions: unified classifier returns category + answer; explicit priority order over exclusion lists; real-label test corpus

### Internal References

- Question detectors: `scripts/application_submit_common.py:1251-1670`
- Post-generation overrides: `scripts/application_submit_common.py:1362-1436` (`apply_generated_answer_overrides`)
- Draft overrides logic: `scripts/application_submit_common.py:1426-1434` (must be preserved)
- Greenhouse parallel answer generation: `scripts/autofill_greenhouse.py:2618-2699` (`_generate_answers_for_questions`)
- Greenhouse composite detectors: `scripts/autofill_greenhouse.py:2082-2105` (`_question_is_deterministic`, `_question_requires_generated_answer`)
- Salary comfort two-step gate (pattern to follow): `scripts/application_submit_common.py:1470-1487`
- Dataclass convention (`slots=True`): `scripts/application_submit_common.py:96,110`
- Test `load_module()` Variant B: `tests/test_submit_application.py:14-21`
- Architecture enforcement: `scripts/check_architecture.py`
- Lever detection chain: `scripts/autofill_lever.py:354-570`
- Ashby detection chain: `scripts/autofill_ashby.py:358-832`
- Gem detection chain: `scripts/autofill_gem.py:162-280`
- Phenom deterministic answers: `scripts/autofill_phenom.py:1465-1519`
- Label corpus source: `output/` (814 JSON files, 1,152 unique labels)
