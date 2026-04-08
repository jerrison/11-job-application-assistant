---
title: "fix: Make CI pipeline green — fix 21 pre-existing test failures"
type: fix
status: active
date: 2026-03-24
---

# fix: Make CI pipeline green

## Problem

CI has 21 test failures across 6 root causes. All pre-existing — none introduced by recent work.

## Fixes

### 1. `test_job_web` (12 failures) — 403 auth errors
Tests hit the web server without auth. Fix: mock or disable auth in test setup.

### 2. `test_draft_manager` (3 failures) — `no such column: priority`
DB schema drift. Code references `priority` column not in test DB. Fix: add column to test schema setup or mock the query.

### 3. `test_ci_workflow` (1 failure) — AGENTS.md != GEMINI.md
Agent file sync diverged. Fix: run sync script and commit.

### 4. `test_greenhouse_autofill` (1 failure) — missing profile fields
Test fixture missing disability_status, gender, race_or_ethnicity, sexual_orientation, veteran_status. Fix: add missing fields to mock profile.

### 5. `test_job_db` (1 failure) — LinkedIn source detection
`assert 'direct' == 'linkedin'`. Fix: trace the detection logic and fix.

### 6. `test_browser_runtime` (1 failure) — profile dir naming
`assert 'playwright-submit-profile' == 'worker-2'`. Fix: update assertion or fix naming.

## Acceptance Criteria

- [ ] `uv run python -m pytest tests/ -v` — 0 failures
- [ ] `uv run ruff check scripts/ tests/` — 0 errors
- [ ] `uv run ruff format --check scripts/ tests/` — 0 reformats needed
