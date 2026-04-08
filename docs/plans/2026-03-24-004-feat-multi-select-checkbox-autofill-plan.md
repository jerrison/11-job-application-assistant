---
title: "feat: multi-select checkbox autofill (select top N)"
type: feat
status: completed
date: 2026-03-24
---

# feat: multi-select checkbox autofill (select top N)

## Overview

Greenhouse `multi_value_multi_select` fields that require selecting multiple checkboxes (e.g., "Select your top 3 tangible factors") only check 1 box. The pipeline creates one step per question, but multi-select needs N steps — one per selected option.

## Problem Statement

The current pipeline has a 1-question → 1-step assumption baked into three layers:

1. **LLM spec** (`_application_question_specs`, line 2138): `multi_value_multi_select` fields don't include available `options` in the prompt — the LLM can't see what to choose from
2. **LLM answer validation** (`_validate_generated_answers`, line 2555): expects one string per field, not a list
3. **Step builder** (`_build_steps`, line 3399): calls `_question_step` once per question, appends one step
4. **Checkbox executor** (line 6506): clicks one checkbox per step, then `break`

## Proposed Solution

### 1. Include options in LLM spec for multi_value_multi_select

`_application_question_specs` (line 2138) currently only includes `options` for `multi_value_single_select`. Add the same for `multi_value_multi_select`, plus an instruction like: "Return a JSON array of selected option labels."

### 2. LLM returns a list for multi-select fields

Update the prompt (`_build_application_answers_prompt`, line 2432) to instruct:
- For `multi_value_multi_select`: return a JSON array of selected labels
- Include the full question label (with "Select your top 3" etc.) so the LLM can determine count from context — no need to parse N ourselves
- Use stored preferences for common question types (e.g., tangible factors → Culture, Career Growth, Company Outlook)
- If the LLM returns fewer or more items than requested, accept what it returns (no truncation/padding) — the form itself may not enforce the count

### 3. Validation accepts lists

`_validate_generated_answers` (line 2555): when the field type is `multi_value_multi_select`, accept a `list[str]` and normalize each element against available options. Fallback: if the LLM returns a comma-separated string instead of a JSON array, split on commas and normalize each.

### 4. _question_step returns multiple steps

`_question_step` (line 3208-3217): when the answer is a list, return a list of checkbox steps — one per selected option. `_build_steps` (line 3399) must handle both dict and list returns from `_question_step`.

### 5. Executor already works

The checkbox executor (line 6475) already handles one checkbox per step. Multiple steps with different `option` values will check multiple boxes — no executor changes needed.

## Acceptance Criteria

- [ ] "Select your top 3" checkbox questions produce 3 checkbox steps in the autofill report
- [ ] Each step checks the correct checkbox at runtime
- [ ] LLM receives available options for multi_value_multi_select fields
- [ ] LLM answer validation accepts list[str] for multi-select fields
- [ ] Stored preferences (agent_preferences.md) guide default answers for common questions
- [ ] Single-select checkbox behavior is unchanged (regression test)
- [ ] Test: multi_value_multi_select with 3 options produces 3 checkbox steps

## Scope

Greenhouse only for this plan. Other boards (Ashby, Lever, Phenom) may have similar multi-select patterns — generalize as a follow-up once the Greenhouse implementation is validated.

## Key Files

- `scripts/autofill_greenhouse.py:2138` — `_application_question_specs` (add options)
- `scripts/autofill_greenhouse.py:2432` — `_build_application_answers_prompt` (list instruction)
- `scripts/autofill_greenhouse.py:2543` — `_validate_generated_answers` (accept lists)
- `scripts/autofill_greenhouse.py:3208` — `_question_step` (return list of steps)
- `scripts/autofill_greenhouse.py:3399` — `_build_steps` (flatten list returns)
- `tests/test_greenhouse_autofill.py:775` — existing test to extend

## Implementation Order

1. Include options in spec for multi_value_multi_select
2. Update prompt to request JSON array for multi-select
3. Update validation to accept list[str]
4. _question_step returns list of steps for multi-select
5. _build_steps flattens list returns
6. Add/update tests
7. Add default preferences for common multi-select questions to agent_preferences.md
