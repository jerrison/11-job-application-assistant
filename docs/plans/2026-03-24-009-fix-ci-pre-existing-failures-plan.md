---
title: "fix: CI pre-existing failures (formatting, missing deps in tests)"
type: fix
status: active
date: 2026-03-24
---

# fix: CI pre-existing failures (formatting, missing deps in tests)

Three pre-existing CI failures unrelated to PR #30:

## Problem 1: Formatting (CI/lint)

4 files fail `ruff format --check`: `autofill_greenhouse.py`, `check_architecture.py`, `test_greenhouse_autofill.py`, `test_sync_agent_files.py`.

**Fix:** Run `uv run ruff format` on them.

## Problem 2: test_job_web.py (CI/unit-tests)

Tests import `fastapi` directly without guarding for its absence. CI doesn't install fastapi.

**Fix:** Add `pytest.importorskip("fastapi")` at module level.

## Problem 3: test_autofill_pipeline.py (CI/unit-tests)

Two tests call `autofill_main` which calls `default_answer_provider()` → raises `RuntimeError` when no LLM provider CLI is installed. The tests mock `write_report` and `find_output_dir` but not the provider.

**Fix:** Mock `default_answer_provider` to return `"claude"` in both tests.

## Acceptance Criteria

- [ ] `ruff format --check scripts/ tests/` passes
- [ ] `pytest tests/` passes in CI (no fastapi, no LLM provider)
