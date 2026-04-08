---
title: "Centralize fragile question detection into unified classifier"
category: logic-errors
date: 2026-03-24
tags:
  - question-detection
  - autofill
  - classifier
  - regression-prevention
  - cross-board
  - priority-dispatch
components:
  - scripts/question_classifier.py
  - scripts/application_submit_common.py
  - scripts/autofill_lever.py
  - scripts/autofill_ashby.py
  - scripts/autofill_gem.py
  - scripts/autofill_greenhouse.py
  - scripts/autofill_dover.py
  - scripts/autofill_phenom.py
  - scripts/autofill_workday.py
  - scripts/autofill_icims.py
  - scripts/autofill_linkedin.py
  - scripts/autofill_bamboohr.py
problem_type: fragmentation-and-fragility
severity: high
---

# Fragile Question Classifier — Regression Cascade

## Problem

Every autofill bug fix was breaking something else. 13 question detectors were scattered across 10+ board scripts with order-dependent if/elif chains. 5 detectors had zero test coverage. Keyword overlap between detectors caused false positives:

- **Education → cover letter:** Lever textarea asking "Provide all post-secondary education attained" received cover letter text because Lever had no education detection — fell through to the comments catch-all
- **"Live in" yes/no swallowed free-text:** "What city and state do you currently live in?" got "Yes" instead of "San Francisco, CA" because the "live in" yes/no detector fired first
- **Salary comfort vs. expectation:** "What is your desired salary range?" risked matching the salary comfort detector ("comfortable with salary range") instead of the open-ended compensation handler

Adding a new detector required editing 9+ files. The education fix alone touched 9 board scripts in one commit. (auto memory [claude]: `feedback_generalize_all_boards` — every fix must be generalized across all boards, all surfaces)

## Root Cause

1. **No centralized classification** — each board reimplemented its own detection chain in `_infer_step()` or equivalent
2. **No priority ordering** — keyword overlap resolved by per-detector exclusion lists (O(n^2) maintenance as detectors grow)
3. **No regression testing** — no corpus of real labels to catch when a classification changes
4. **Post-generation overrides wasted LLM tokens** — `apply_generated_answer_overrides()` generated an LLM answer for questions it then deterministically replaced

## Solution

Three layered PRs: immediate fix, systemic fix, cleanup.

### PR #26 — Immediate fix (education detection)

Added `question_is_education()` and `format_education_from_profile()` to `application_submit_common.py`. Wired into 9 boards before cover letter catch-alls. 17 new tests.

### PR #27 — Systemic fix (unified classifier)

Created `scripts/question_classifier.py` with `classify_question()` as single entry point. Priority-ordered dispatch (first match wins, 13 detectors ranked specific-to-broad):

```python
# scripts/question_classifier.py
def classify_question(label, field_type=None, application_profile=None, out_dir=None):
    """Priority-ordered dispatch. First match wins."""
    normalized = normalize_text(label)
    # P1:  current_company  (most specific — field name match)
    # P2:  nda_noncompete
    # P3:  reasonable_accommodation
    # P4:  salary_comfort   (two-step gate, before broader compensation)
    # P5:  compensation
    # P6:  minimum_experience
    # P7:  experience_confirmation
    # P8:  product_usage
    # P9:  city_location
    # P10: office_attendance
    # P11: company_engagement
    # P12: culture_careers_optin
    # P13: education         (broadest keywords — last)
    for detector in _DETECTOR_CHAIN:
        result = detector(normalized, ...)
        if result is not None:
            return result
    return None  # fall through to LLM
```

Harvested **1,323 real labels** from 814 autofill report JSONs as regression corpus. Hand-wrote overlap edge cases. Added unit tests for 5 previously untested detectors. Migrated all 10 boards.

### PR #28 — Cleanup

Deleted `apply_generated_answer_overrides()` and its 6 call sites (3 in `application_submit_common.py`, 3 in Greenhouse's parallel `_generate_answers_for_questions()`). Extracted `apply_draft_overrides()` for user edit handling. Added warning log for classified questions reaching LLM generation.

## Key Patterns

### Specific-to-broad ordering eliminates exclusion lists

Instead of each detector maintaining a deny-list of other detectors' keywords (O(n^2)), the chain ordering means `nda_noncompete` (P2) runs before `education` (P13), `salary_comfort` (P4) runs before `compensation` (P5). Overlap is resolved by position, not per-detector exclusion logic.

### Classify-then-generate eliminates wasted LLM calls

Questions with known static answers (sponsorship, education, NDA) are classified and answered before reaching the LLM. Saves ~250-500 prompt tokens and ~100-250 response tokens per application.

### Real-label regression corpus

The 1,323-label corpus (`tests/fixtures/question_label_corpus.json`) is ground truth harvested from production autofill reports. Any new detector that changes an existing label's classification fails the test. This catches regressions that hand-written keyword tests miss.

## Prevention

### Adding a new detector — checklist

1. Add detector function to `scripts/question_classifier.py` in the priority chain
2. Choose priority placement: specific patterns before broad ones. If keywords overlap with existing detectors, place above the broader one
3. Add 10+ test entries to `tests/fixtures/question_label_corpus.json` (positive matches, near-miss non-matches, overlap cases)
4. Run `uv run python -m pytest tests/test_question_classifier.py -v` — verify no existing classifications change
5. Add hand-written edge cases to `OverlapEdgeCaseTests` for ambiguous labels
6. Update the priority table comment in `question_classifier.py`

### Keyword overlap vigilance

Known overlap pairs requiring ongoing attention:
- `salary_comfort` vs `compensation` (both match "salary")
- `city_location` vs `office_attendance` (both match location text)
- `current_company` vs `nda_noncompete` (both match employer text)
- `education` vs everything (broadest keywords, placed last)

### Preventing detector sprawl

- **One file for classification:** All question-type routing lives in `scripts/question_classifier.py`. No board script should contain inline keyword matching for question classification.
- **Flag threshold:** After 3+ similar implementations, suggest a generic approach (auto memory [claude]: `feedback_flag_consolidation`).
- **Grep audit:** `grep -r "question_is_\|_question_is_" scripts/autofill_*.py` should show only imports from `question_classifier`, never inline detector functions.

### Known gap — bidirectional corpus regression

The corpus test currently only validates entries with `expected_category is not None`. A new detector that starts classifying previously-null labels passes silently. Fix: add a `test_null_entries_remain_unclassified` test that catches null-to-classified drift.

## Cross-References

- **Origin brainstorm:** `docs/brainstorms/2026-03-24-unified-question-classifier-requirements.md`
- **Implementation plan:** `docs/plans/2026-03-24-001-refactor-unified-question-classifier-plan.md`
- **Predecessor bug fixes:** `docs/plans/2026-03-23-010-fix-education-question-detection-across-boards-plan.md`, `docs/plans/2026-03-23-006-fix-city-state-question-returns-yes-plan.md`
- **Existing learning (multi-layer validation):** `docs/solutions/integration-issues/adding-new-llm-provider.md`
- **Architecture context:** `docs/board-architecture.md`, `docs/autofill-patterns.md`
- **Core beliefs enforced:** #7 (deterministic overrides beat LLM answers), #10 (cached answers run through overrides), #16 (bug in one board likely exists in others)

### Refresh candidates (stale references to `apply_generated_answer_overrides`)

| Priority | Doc | What to update |
|----------|-----|----------------|
| High | `docs/autofill-patterns.md` | 3 bullets referencing deleted function |
| High | `docs/board-architecture.md` | Add `question_classifier.py` to architecture description |
| Medium | `docs/INDEX.md` | Add classifier to architecture table; fix plan counts |
