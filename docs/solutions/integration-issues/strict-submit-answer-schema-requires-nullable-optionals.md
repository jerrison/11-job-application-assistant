---
title: "Strict submit-answer schemas require nullable optionals and full required coverage"
category: integration-issues
date: 2026-03-26
tags:
  - openai
  - structured-output
  - submit-generation
  - greenhouse
  - provider-observability
components:
  - scripts/application_submit_common.py
  - scripts/autofill_greenhouse.py
  - tests/test_submit_application.py
  - tests/test_greenhouse_autofill.py
  - tests/test_llm_provider.py
problem_type: external-contract-drift
severity: high
---

# Strict Submit-Answer Schemas Require Nullable Optionals

## Problem

Draft-safe submit answer generation started failing on the OpenAI structured-output path with:

`Invalid schema for response_format 'application_answers' ... 'required' is required to be supplied and to be an array including every key in properties.`

The failure surfaced while reproducing a Duolingo Greenhouse submit issue from a `codex`-configured run, which made the provider path look ambiguous from the raw artifacts alone.

## Root Cause

The shared submit schema builder in `scripts/application_submit_common.py` only placed truly required questions in the root `required` array. That matched older JSON Schema intuition, but it no longer matched the strict structured-output contract enforced by the OpenAI shim.

Two extra details made the bug easy to mis-handle:

1. Optional submit questions need to remain skippable without filler text, so they cannot simply become required non-empty strings.
2. Required conditional follow-up questions such as OPT or recent-graduate GPA prompts already rely on validator-side normalization to `N/A` / `NA` when the condition does not apply.

Without a nullable schema branch, the strict schema rejected the request before answer generation even ran.

## Solution

Updated the shared submit schema contract so it matches the strict provider path instead of relying on omitted properties:

- Every declared `field_name` now appears in the root `required` array.
- Ordinary optional fields are modeled as nullable string schemas instead of omitted keys.
- Required conditional follow-up fields are also nullable at the schema layer, which preserves the existing validator behavior that normalizes blank/non-applicable responses to `N/A`.
- Optional Greenhouse-generated fields use the same nullable contract through the imported shared runner path.

Prompt semantics were aligned with the schema:

- Shared and Greenhouse prompts now instruct the model to return JSON `null` for blank optional fields.
- Conditional follow-up prompts that do not clearly apply also use JSON `null` rather than empty-string placeholders.
- Prompts now tell the model to include every `field_name` exactly once.

Provider-path observability was tightened at the raw-artifact layer:

- `application_answers_raw.txt` and `application_answers_fallback_raw.txt` now prepend `provider=<name>` headers per attempt.
- This makes it obvious whether a failing submit run used the primary provider or a fallback provider before `application_answers.json` is even inspected.

## Verification

- `uv run python -m pytest tests/test_submit_application.py tests/test_question_classifier.py tests/test_greenhouse_autofill.py tests/test_llm_provider.py tests/test_openai_provider.py -v`
- `uv run ruff check scripts/application_submit_common.py scripts/autofill_greenhouse.py tests/test_submit_application.py tests/test_greenhouse_autofill.py tests/test_llm_provider.py`
- Reproduced the fix against a fresh temp copy of `output/duolingo/senior-pm-in-app-purchases` with `provider='openai'` after clearing cached answer artifacts. The run completed successfully, generated 7 answers, restored:
  - `question_35137919002 = JEH-rih-son Lee`
  - `question_35137924002 = NA`
  - `question_35137925002 = NA`
  - `question_35137926002 = N/A`
- The regenerated raw artifact started with `INFO: provider=openai mode=submit attempt=1`, confirming the actual executing provider path.
- Reproduced the same temp-copy flow with `provider='codex'`. The run completed without the historical schema 400, wrote a raw artifact headed by `INFO: provider=codex mode=submit attempt=1`, and preserved the required OPT / GPA follow-up answers (`NA`, `NA`, `N/A`).

## Prevention

- When using strict structured outputs, treat “optional” object fields as nullable required properties, not omitted properties.
- If validator behavior already normalizes conditional blanks downstream, make the schema permissive enough to let that normalization run.
- Characterize provider routing at both the command-builder seam and the raw-artifact seam so fallback or misrouted runs are obvious.
- For submit-answer repros, clear cached answer artifacts before verification; otherwise matching `application_answers.json` files can short-circuit provider execution and mask the real contract.

## Cross-References

- Plan: `docs/plans/2026-03-26-003-fix-codex-answer-schema-plan.md`
- Related: `docs/solutions/integration-issues/adding-new-llm-provider.md`
- Related: `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`
