---
title: "fix: Education question detection — textarea gets cover letter instead of degrees"
type: fix
status: completed
date: 2026-03-23
---

# fix: Education question detection across boards

## Problem

On Lever (AHEAD, job #202), a textarea asking "PROVIDE ALL POST-SECONDARY EDUCATION ATTAINED — FORMATTED AS: COLLEGE NAME; DEGREE OBTAINED" received the cover letter text instead of education data. Lever has no education question detection — it falls through to LLM-generated answers which produce the cover letter for unknown textareas.

## Root Cause

1. **No education detection in Lever** — Greenhouse has `_parse_education_entries()` but Lever has zero education handling
2. **No education data in application_profile.md** — profile has work auth, location, EEO, but no education section
3. **Cover letter catch-all is too broad** — line 423 catches "comments" and generic textareas, but education questions aren't excluded

## Proposed Solution

### 1. Add education data to `application_profile.md`

Add a hardcoded education section (user's degrees don't change):

```markdown
## Education
- The Wharton School, University of Pennsylvania; Master of Business Administration (M.B.A.)
- Penn Engineering, University of Pennsylvania; Master of Science in Computer Science
- Florida State University; Bachelor of Science in Actuarial Science & Computational Science (Dual Degree)
```

### 2. Add `_question_is_education()` to `application_submit_common.py`

Shared detector for all boards:

```python
def _question_is_education(label: str) -> bool:
    """Detect questions asking about education/degrees."""
    lower = label.lower()
    education_keywords = ("education", "degree", "college", "university",
                          "post-secondary", "school attended", "institution",
                          "academic", "diploma")
    exclude_keywords = ("background check", "discrimination", "equal opportunity")
    if any(kw in lower for kw in exclude_keywords):
        return False
    return any(kw in lower for kw in education_keywords)
```

### 3. Add education handling to `autofill_lever.py`

Before the "comments" catch-all (line 423), add:

```python
if _question_is_education(field.get("text", "")):
    education_text = _format_education_from_profile(application_profile)
    if education_text:
        return {"kind": "textarea", "value": education_text, "source": "application_profile.md"}
```

### 4. Generalize across all boards

Per user preference, check all board autofill scripts for the same gap:
- `autofill_ashby.py` — check if education questions are handled
- `autofill_dover.py` — check
- `autofill_gem.py` — check
- `autofill_greenhouse.py` — already has education handling (verify it covers textareas)
- Other boards

## Acceptance Criteria

- [ ] "Provide all post-secondary education attained" textarea returns formatted education, not cover letter
- [ ] Education data sourced from `application_profile.md`
- [ ] `_question_is_education()` shared across boards
- [ ] Greenhouse education handling still works (no regression)
- [ ] Cover letter catch-all excludes education questions
- [ ] New test for education question detection

## Sources

- Lever textarea logic: `scripts/autofill_lever.py:423`
- Greenhouse education parsing: `scripts/autofill_greenhouse.py:617-645`
- User education: Wharton MBA, Penn M.S. CS, FSU B.S. Actuarial/Computational Science
