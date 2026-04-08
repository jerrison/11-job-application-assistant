---
title: "fix: LinkedIn qualification dropdown questions always answer Yes"
type: fix
status: active
date: 2026-03-25
origin: docs/brainstorms/2026-03-25-linkedin-qualification-dropdown-yes-requirements.md
---

# fix: LinkedIn qualification dropdown questions always answer Yes

## Overview

LinkedIn Easy Apply presents "Do you have..." qualification screening questions as dropdowns (Yes/No). The autofill leaves them unanswered because: (1) the question classifier misses "do you have experience/background" phrasing, (2) the minimum-experience detector misses range syntax ("4 to 10 years"), (3) LinkedIn's `_answer_for_select()` short-circuits all labels containing "experience" or "years" to `None` before the classifier runs, and (4) no category-to-answer mapping exists for `experience_confirmation` or `minimum_experience` on LinkedIn. (see origin: requirements doc)

## Problem Statement

Three dropdown questions on a Zest Search application (job 4384345202) went unanswered, blocking submission:

1. "Do you have experience working closely with engineers on technical products (APIs, data platforms, ML/AI systems)?*"
2. "Do you have a background in software engineering? (Nice to have)*"
3. "Do you have 4 to 10 years of experience in Product Management with over 2 years of delivering SaaS/AI products?*"

## Proposed Solution

Four coordinated changes across classifier, LinkedIn answer provider, and test corpus.

## Implementation Steps

### Step 1: Broaden `question_is_experience_confirmation()` (R1)

**File:** `scripts/application_submit_common.py` (lines 1735-1752)

Add a second regex branch for "do you have" + noun phrasing alongside the existing "have you" + verb pattern:

```python
# Existing pattern — keep as-is
r"\bhave you\b.*\b(?:shipped|built|launched|developed|managed|led|created|designed|implemented)\b"

# New pattern — "do you have experience/background [in/with/as]"
r"\bdo you have\b\s+(?:a\s+)?(?:experience|background)\b"
```

**Guards (critical):**
- Reuse the existing exclusion list: reject labels containing "share more", "describe", "tell us more", "elaborate", "explain", "please provide details", "if none"
- Add education keyword guard: reject if label also contains "degree", "university", "college", "academic" (prevents stealing from the `education` detector at P13)
- Do NOT add a year-count guard — labels like "Do you have 4 to 10 years of experience" should be caught by `minimum_experience` at P6 first (priority ordering handles this)

**Out of scope for this PR:** Expanding the "have you [verb]" verb list (`owned`, `worked`, `contributed`, etc.) is a separate enhancement. The existing verb list already covers the common cases and none of the three Zest Search questions use that pattern.

### Step 2: Broaden `question_is_minimum_experience_check()` (R2)

**File:** `scripts/application_submit_common.py` (lines 1612-1630)

Add a third regex for range syntax:

```python
# Existing regexes — keep as-is
r"\bat least\b.*\byears?\b.*\bexperience\b"
r"\b\d+\+?\s+years?\s+of\s+(?:\w+\s+)*experience\b"

# New regex — range syntax "4 to 10 years of experience"
r"\b\d+\s+to\s+\d+\s+years?\s+of\s+.*\bexperience\b"
```

### Step 3: Wire LinkedIn `_answer_for_select()` to the classifier (R3)

**File:** `scripts/autofill_linkedin.py` (lines 854-919)

Insert classifier-based routing **above** the existing line 868 catch-all:

```python
# scripts/autofill_linkedin.py:_answer_for_select — add before line 868
from question_classifier import classify_question as _classify_question

category = _classify_question(label)
if category == "experience_confirmation":
    return "Yes"
if category == "minimum_experience":
    from application_profile import get_application_profile
    profile = get_application_profile()
    return "Yes" if profile.get("minimum_years_experience", True) else "No"

# Existing catch-all remains as fallback for unclassified experience/years labels
if label_matches(label, "experience", "years"):
    return None
```

This follows option (a) from SpecFlow analysis — minimal diff, classifier checks above the catch-all. The catch-all remains as fallback for open-ended "How many years of experience?" questions that should NOT get "Yes".

**Also update `_linkedin_answer_from_category()`** (lines 657-679) for textarea completeness — use the same logic as the dropdown path above:

```python
if category == "experience_confirmation":
    return "Yes"
if category == "minimum_experience":
    profile = get_application_profile()
    return "Yes" if profile.get("minimum_years_experience", True) else "No"
```

### Step 4: Update test corpus and add test cases

**File:** `tests/fixtures/question_label_corpus.json`

Add 10+ LinkedIn-specific entries:

```json
{"label": "Do you have experience working closely with engineers on technical products (APIs, data platforms, ML/AI systems)?", "expected_category": "experience_confirmation", "board": "linkedin"},
{"label": "Do you have a background in software engineering? (Nice to have)", "expected_category": "experience_confirmation", "board": "linkedin"},
{"label": "Do you have 4 to 10 years of experience in Product Management with over 2 years of delivering SaaS/AI products?", "expected_category": "minimum_experience", "board": "linkedin"},
{"label": "Do you have experience building data pipelines?", "expected_category": "experience_confirmation", "board": "linkedin"},
{"label": "Do you have a background in machine learning?", "expected_category": "experience_confirmation", "board": "linkedin"},
{"label": "Do you have 3 to 5 years of software engineering experience?", "expected_category": "minimum_experience", "board": "linkedin"}
```

Update existing null entries that should now classify:

- `"Do you have experience working as a Product Manager at a PLG..."` (ashby line 518) -> `experience_confirmation`
- `"Do you have experience building and scaling internal platform products..."` (greenhouse line 3533) -> `experience_confirmation`
- `"Do you have experience with Big Data technologies?"` (greenhouse line 3548) -> `experience_confirmation`
- `"Do you have experience as a Product Manager for a b2b SaaS solution?"` (lever line 6088) -> `experience_confirmation`

**Do NOT reclassify** entries with elaboration suffixes ("Please provide details", "If none, put N/A") — these should remain null so the LLM handles them on text-field boards.

Add overlap edge cases to `OverlapEdgeCaseTests` in `tests/test_question_classifier.py`:

```python
# "do you have" + years -> minimum_experience (P6 wins over P7)
("Do you have 5+ years of product management experience?", "minimum_experience"),
# "do you have" + background + degree -> education (guard prevents experience_confirmation)
("Do you have an engineering background (either degree or professional experience)?", "education"),
# "do you have" + experience + "please provide details" -> None (exclusion fires)
("Do you have experience with CRM systems? Please provide details.", None),
```

### Step 5: Verification

1. `uv run python -m pytest tests/test_question_classifier.py -v` — all corpus entries pass
2. `uv run python -m pytest tests/test_submit_application.py -v -k experience` — detector unit tests
3. `uv run python -m pytest tests/ -v` — full suite, no regressions
4. `uv run ruff check scripts/ tests/` — lint clean
5. `uv run python scripts/check_architecture.py` — architecture valid

## Acceptance Criteria

- [ ] "Do you have experience working closely with engineers on technical products (APIs, data platforms, ML/AI systems)?" classifies as `experience_confirmation` (R1)
- [ ] "Do you have a background in software engineering? (Nice to have)" classifies as `experience_confirmation` (R1)
- [ ] "Do you have 4 to 10 years of experience in Product Management..." classifies as `minimum_experience` (R2)
- [ ] LinkedIn `_answer_for_select()` returns "Yes" for both categories (R3)
- [ ] LinkedIn `_linkedin_answer_from_category()` maps both categories (R3)
- [ ] Existing `minimum_experience` and `experience_confirmation` corpus entries unchanged (no regressions)
- [ ] Labels with "please provide details" / "describe" remain unclassified (exclusion guard works)
- [ ] "Do you have an engineering background (either degree or professional experience)?" stays `education` (education guard works)
- [ ] 10+ new corpus entries added with LinkedIn board tag
- [ ] All existing tests pass, lint and architecture checks pass

## Dependencies & Risks

- **Low risk:** Broadened `experience_confirmation` detector could over-match. Mitigated by exclusion guards (elaboration prompts, education keywords) and the priority ordering (`minimum_experience` at P6 catches year-count questions before `experience_confirmation` at P7).
- **Corpus drift:** ~4-6 existing null entries will be reclassified to `experience_confirmation`. Each must be reviewed. The `test_null_entries_remain_unclassified` test will catch any unexpected reclassifications.
- **R4 already satisfied:** Audit confirms LinkedIn is the only board with this gap. Lever, Greenhouse, Ashby, Gem, iCIMS all already handle both categories via their board-specific mappers.
- **Documented learning:** Per `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`, detector changes require corpus entries, overlap edge cases, and priority placement verification. All addressed in Step 4.

## Sources & References

- **Origin document:** [docs/brainstorms/2026-03-25-linkedin-qualification-dropdown-yes-requirements.md](docs/brainstorms/2026-03-25-linkedin-qualification-dropdown-yes-requirements.md) — key decisions: always answer Yes, fix at classifier + answer mapping level, not via LLM fallback
- Question classifier dispatch: `scripts/question_classifier.py:103-176`
- Experience confirmation detector: `scripts/application_submit_common.py:1735-1752`
- Minimum experience detector: `scripts/application_submit_common.py:1612-1630`
- LinkedIn `_answer_for_select`: `scripts/autofill_linkedin.py:854-919` (line 868 catch-all)
- LinkedIn `_linkedin_answer_from_category`: `scripts/autofill_linkedin.py:657-679`
- Lever reference (working pattern): `scripts/autofill_lever.py:427-430`
- Documented learning: `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`
- Test corpus: `tests/fixtures/question_label_corpus.json`
