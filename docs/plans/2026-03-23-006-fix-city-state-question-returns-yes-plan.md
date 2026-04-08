---
title: "fix: 'What city and state do you live in?' returns 'Yes' instead of location"
type: fix
status: completed
date: 2026-03-23
---

# fix: "What city and state do you live in?" returns "Yes" instead of location

## Problem

On Ashby forms, the question "What city and state do you currently live in?" gets answered with "Yes" instead of "San Francisco, CA". Seen on Mudflap — director-of-product-growth (job #308).

## Root Cause

`autofill_ashby.py:712` matches "currently live in" in the label and enters the yes/no path. `_yes_no_step()` → `_ashby_option_matcher()` returns the literal string "Yes" for non-select field types (line 338: `return candidates[0]`). The question is a free-text location question, not a yes/no question.

The pattern "live in" / "currently live in" is valid for yes/no questions like "Do you currently live in the Bay Area?" but too greedy for "What city... do you currently live in?".

## Proposed Solution

Before the `_label_matches(field, "live in", ...)` check at line 712, add a guard: if the label starts with an interrogative word ("what", "which", "where") and matches location patterns, return the candidate's location instead of entering the yes/no path.

### `scripts/autofill_ashby.py:712`

```python
# Before the yes/no "live in" check, catch free-text location questions
# e.g. "What city and state do you currently live in?" → "San Francisco, CA"
# vs.  "Do you currently live in the Bay Area?" → "Yes"
label_lower = (field.get("label") or "").strip().lower()
if any(label_lower.startswith(w) for w in ("what ", "which ", "where ")) and \
   _label_matches(field, "live in", "currently live", "reside", "based"):
    return {
        **base,  # base = {"path": field["path"], "field_type": field["field_type"]}
        "kind": "text",
        "value": application_profile.location,  # "San Francisco, CA"
        "source": "application_profile.md",
    }
```

This must come **before** the existing `_label_matches(field, "live in", ...)` block so the more specific pattern wins.

**Note:** The guard uses `_label_matches(field, "live in", "currently live", "reside", "based")` without requiring "city"/"state" keywords — this also covers variants like "Where do you currently live?" and "Where are you currently based?".

## Generalization

Per user preference, check all board autofill scripts for the same pattern — any "live in" yes/no matcher that could match free-text location questions:
- `autofill_greenhouse.py`
- `autofill_lever.py`
- `autofill_workday.py`
- Other board scripts

## Acceptance Criteria

- [ ] "What city and state do you currently live in?" returns "San Francisco, CA"
- [ ] "Do you currently live in the Bay Area?" still returns "Yes"
- [ ] Other "live in" yes/no questions still work correctly
- [ ] Check all board scripts for the same greedy pattern
- [ ] Existing tests pass
- [ ] New test covers the free-text city/state question variant

## Sources

- Yes/no matcher: `scripts/autofill_ashby.py:712-719`
- Option matcher for non-select: `scripts/autofill_ashby.py:336-339`
- City location detection: `scripts/application_submit_common.py:1309` (`_question_is_city_location`)
- Location default: `application_profile.md:7` (Location: San Francisco, CA)
- User preference: Bay Area = "San Francisco, CA" (auto memory)
