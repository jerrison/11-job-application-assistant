---
title: "Provider-capacity fallback must inspect logs, not just exit codes"
category: integration-issues
date: 2026-03-28
last_updated: 2026-03-28
tags:
  - llm-provider
  - provider-capacity
  - fallback
  - reliability
  - openai
  - gemini
components:
  - scripts/llm_common.sh
  - tests/test_llm_common.py
  - docs/brainstorms/2026-03-25-pipeline-resilience-requirements.md
problem_type: integration_issue
root_cause: exit_status_only_detection
resolution_type: code_fix
severity: high
---

# Provider-Capacity Fallback Must Inspect Logs, Not Just Exit Codes

## Problem

Explicit provider quota and capacity failures were still leaving stopped jobs behind even after the fallback chain existed. The underlying bug was subtle: a provider could return exit code `0` while writing only a quota banner like `You're out of extra usage` or `You've hit your usage limit` and never produce the requested output file. The fallback helper treated that as success, so the chain never advanced.

## Symptoms

- Live stopped rows still preserved raw LLM artifacts with explicit quota text.
- A shell reproduction of `job_assets_run_prompt_with_fallback chain` only called `openai` before this fix, even when the provider wrote no output file.
- The same trust gap existed in the single-provider legacy fallback path, so pinned providers could also stall before reaching their configured fallback.

## What Didn't Work

- Trusting provider exit status alone was not sufficient because wrapper scripts and CLIs can report account-state failures in stdout/stderr while still exiting successfully.
- The fallback helpers did not have a shared concept of "provider-capacity failure"; they only reacted to non-zero exit status.
- Missing output files were not enough to explain the failure mode because the quota text was sitting in the raw log, but no code path promoted that into a fallback decision.

## Solution

`scripts/llm_common.sh` now centralizes quota and capacity detection in `job_assets_log_contains_provider_capacity_error()`. The helper scans the saved provider log for explicit signatures such as:

- `You're out of extra usage`
- `You've hit your usage limit`
- `exhausted your capacity on this model`
- `TerminalQuotaError`
- `purchase more credits`
- `quota will reset after`

Both fallback paths now use that helper:

- `job_assets_run_prompt_with_fallback chain` forces a fallthrough to the next configured provider when the current provider log contains a capacity signal, even if the provider exited `0`.
- `_job_assets_run_single_provider_with_legacy_fallback` applies the same rule to the primary provider and the configured legacy fallback.

`tests/test_llm_common.py` now includes one regression for chain mode and one for legacy fallback. In both cases, the first provider returns `0`, writes only quota text, and the fallback provider is required to generate the real output artifact.

## Why This Works

The saved raw provider log is the best source of truth for account-state failures. By converting those explicit quota banners into provider failures before the outer pipeline retries kick in, the runtime moves immediately to the next provider instead of burning retries on a provider that is already known to be unavailable for that call.

## Verification

- `uv run python -m pytest tests/test_llm_common.py -v`
- `uv run python -m pytest tests/test_job_assets_pipeline.py -v`
- `uv run ruff check tests/test_llm_common.py`
- Direct shell reproduction of chain mode now records `CALLS=openai,gemini` and writes fallback output from `gemini`

## Prevention

- When a provider can surface account-state banners in logs, do not rely on exit codes alone to decide whether fallback should advance.
- Keep provider-capacity signatures centralized so new providers or wording changes only require one update.
- Add a regression any time a provider integration can return a "successful" process status without producing the requested output artifact.

## Cross-References

- Active brainstorm: `docs/brainstorms/2026-03-25-pipeline-resilience-requirements.md`
- Repo-local audit: `docs/solutions/workflow-issues/stopped-job-audits-must-be-repo-local-and-artifact-backed-2026-03-27.md`
- Related provider setup learning: `docs/solutions/integration-issues/adding-new-llm-provider.md`
