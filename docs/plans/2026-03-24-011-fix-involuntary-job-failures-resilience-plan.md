---
title: "fix: harden pipeline against involuntary job failures"
type: fix
status: completed
date: 2026-03-24
---

# fix: harden pipeline against involuntary job failures

## Overview

49 jobs were stopped involuntarily (excluding `user_stopped` and `duplicate`). This plan addresses each failure category with targeted fixes to reduce failures and make retries smarter.

## Problem Statement / Motivation

Analysis of all `stopped` jobs with `failure_type NOT IN ('user_stopped', 'duplicate')` reveals **6 root-cause categories**:

| # | Category | Count | Root Cause | Boards Affected |
|---|----------|-------|------------|-----------------|
| 1 | **JD scrape failures** | 25 | Job board rate-limits or blocks scraper; "Please retry later or provide the JD text directly" | greenhouse(6), icims(5), unknown(6), workday(2), smartrecruiters, rippling, ashby, reducto, uber |
| 2 | **LLM process timeout (SIGTERM)** | 6 | `run_command_with_timeout.py` kills LLM subprocess; `llm_common.sh: line 337: ... Terminated: 15` | greenhouse(2), linkedin, phenom, lever, workday |
| 3 | **JSON parse errors** | 5 | LLM returned JSON that `json_lenient.py` couldn't repair (trailing-comma regex isn't enough) | greenhouse(2), workday, icims, linkedin |
| 4 | **Auth failures** | 5 | Workday (4) + iCIMS (1) login locked or wrong credentials | workday(4), icims(1) |
| 5 | **Truncated/empty errors + fast pipeline** | 5 | Error messages stored with `────` separators (truncated), or pipeline completed in ~5s with no output | icims(2), greenhouse, ashby, linkedin(2) |
| 6 | **Auto-fix slot busy** | 2 | All 3 auto-fix semaphore slots occupied; job stopped instead of requeued | linkedin(2) |
| — | **Submission all attempts failed** | 1 | LinkedIn submission exit 1, no specific error | linkedin |

**Impact:** ~31 of these 49 (63%) are likely recoverable with better retry logic, error handling, and scraper resilience.

## Proposed Solution

### Fix 1: JD scrape — add request rotation and smarter backoff (25 jobs)

**Problem:** Single user-agent and IP hits rate limits after a few fetches in the same batch.

**Changes in `scripts/run_pipeline.py` (JD fetch loop, lines 875-944):**
- Rotate `User-Agent` headers between fetch attempts (pool of 5-10 realistic browser UAs)
- Add per-domain rate limiting: track last-fetch time per domain, enforce minimum 3s gap
- On HTTP 429: parse `Retry-After` header and respect it instead of fixed backoff
- On HTTP 403: try with Playwright browser context (not just headless fetch) as fallback before giving up
- Increase `JD_FETCH_MAX_RETRIES` default from 3 → 5 for batch mode

**Changes in `scripts/pipeline_orchestrator.py`:**
- Add "retry later" scrape failures to `_TRANSIENT_PATTERNS` if not already matched (they are — "retry later" matches)
- Verify these get auto-retried (they do via `_auto_retry_if_transient`)

### Fix 2: LLM timeout — increase timeout and treat SIGTERM as transient (6 jobs)

**Problem:** LLM subprocess killed by `run_command_with_timeout.py` returning exit 124. The timeout may be too aggressive for large JDs or complex roles.

**Changes in `scripts/llm_common.sh`:**
- Increase `JOB_ASSETS_PROVIDER_TIMEOUT_SECONDS` default from 600 → 900 for primary asset generation
- Log the actual elapsed time before timeout so we can see how close successful runs are to the limit

**Changes in `scripts/pipeline_orchestrator.py`:**
- Ensure exit code 124 (timeout) triggers provider fallback and then auto-retry
- Verify "timed out" / "timeout" patterns in `_TRANSIENT_PATTERNS` catch the `run_command_with_timeout.py` error message

### Fix 3: JSON parse — extend `json_lenient.py` with better repair (5 jobs)

**Problem:** LLM sometimes produces JSON with issues beyond trailing commas: unescaped newlines in strings, missing closing braces, single-quoted strings.

**Changes in `scripts/json_lenient.py`:**
- Add repair steps before second parse attempt:
  1. Strip trailing commas (existing)
  2. Fix unescaped control characters in string values (`\n` → `\\n` inside quoted strings)
  3. Balance braces/brackets (detect unclosed and attempt to close)
  4. Try `ast.literal_eval` → JSON roundtrip as last resort (handles single quotes)
- Add a `loads_with_diagnostics()` variant that returns the repair step that worked (for logging)

**Changes in `scripts/pipeline_orchestrator.py`:**
- `_suggest_resolution()` already says "retry usually fixes this" for JSON errors — verify the retry logic actually triggers (JSON errors should be treated as transient)

### Fix 4: Auth failures — skip known-broken boards and surface credentials (5 jobs)

**Problem:** Workday and iCIMS auth fails due to locked accounts or wrong credentials. Retrying makes it worse (locks accounts further).

**Changes in `scripts/pipeline_orchestrator.py`:**
- Auth failures should NOT be treated as transient (they already aren't — `auth_failed` doesn't match `_TRANSIENT_PATTERNS`)
- Add a board-level "auth broken" flag: after N auth failures on the same board domain, skip new jobs for that board with a warning
- Surface the actual board domain in the error message (e.g., "workday.autodesk.com" not just "Workday")

**Changes in `scripts/job_db.py`:**
- Add helper: `get_recent_auth_failures(board, hours=24)` → count of auth failures in the last 24h
- Used by orchestrator to skip boards with repeated auth failures

### Fix 5: Truncated error messages — capture full stderr (5 jobs)

**Problem:** Some error messages stored in the DB contain only `────` separator lines, losing the actual error. The pipeline captures stderr but truncates it.

**Changes in `scripts/pipeline_orchestrator.py`:**
- In `_run_generation()` and `_run_submission()`: when capturing subprocess stderr, take the **last 500 chars** instead of relying on the last line
- Strip ANSI escape codes and box-drawing characters before storing in `error_message`
- For "fast pipeline" failures (completed in <10s): explicitly log "Pipeline completed too quickly — likely no JD content extracted" instead of storing a truncated empty message

### Fix 6: Auto-fix slot busy — treat as transient immediately (2 jobs)

**Problem:** When all 3 auto-fix slots are busy, the job is stopped with `failure_type="submit_failed"`. It DOES match `_TRANSIENT_PATTERNS` ("auto-fix slot busy"), so it gets retried eventually — but the `failure_type` is misleading.

**Changes in `scripts/pipeline_orchestrator.py` (lines 1149-1159):**
- When auto-fix slot is unavailable: set `failure_type="auto_fix_busy"` (new type) instead of `submit_failed`
- Requeue immediately to `queued` with incremented `fix_attempts` instead of going through stopped → auto-retry cycle
- This avoids one wasted cycle through the stopped state

## Technical Considerations

- **Rate limiting coordination:** Per-domain rate limiting state should be shared across workers. Use a simple `threading.Lock` + dict since the orchestrator already runs in-process with threads.
- **JSON repair safety:** Aggressive repair (balancing braces) can produce valid-but-wrong JSON. Validate repaired JSON against expected schema before accepting.
- **Auth skip propagation:** The board-auth-broken flag should be in-memory only (not persisted), so it resets on restart — allowing manual credential fixes.
- **Backwards compatibility:** All changes are additive to existing retry/error infrastructure. No status flow changes.

## Acceptance Criteria

### Fix 1: JD Scrape Resilience
- [ ] User-Agent rotation implemented in `run_pipeline.py` fetch loop
- [ ] Per-domain rate limiting enforced (3s minimum gap)
- [ ] HTTP 429 `Retry-After` header respected
- [ ] Playwright browser fallback attempted on 403
- [ ] Test: mock 429 with Retry-After header → respects delay
- [ ] Test: mock 403 → falls back to Playwright extraction

### Fix 2: LLM Timeout Handling
- [ ] Default timeout increased for asset generation
- [ ] Elapsed time logged before timeout kills process
- [ ] Exit code 124 verified to trigger auto-retry
- [ ] Test: subprocess returning 124 → triggers provider fallback

### Fix 3: JSON Parse Repair
- [ ] `json_lenient.py` handles unescaped newlines in strings
- [ ] `json_lenient.py` attempts brace balancing
- [ ] `json_lenient.py` handles single-quoted strings
- [ ] Repair logging added (which step fixed it)
- [ ] Test: each repair case with sample malformed JSON from actual failures

### Fix 4: Auth Failure Handling
- [ ] Board domain included in auth error messages
- [ ] `get_recent_auth_failures()` helper added to `job_db.py`
- [ ] Orchestrator skips boards with 3+ auth failures in 24h
- [ ] Test: 3 auth failures → 4th job on same board skipped with warning

### Fix 5: Error Message Capture
- [ ] Stderr captured with last 500 chars, ANSI stripped
- [ ] Fast-pipeline detection (<10s) logs explicit message
- [ ] Test: verify truncated error is replaced with meaningful content

### Fix 6: Auto-Fix Slot Busy
- [ ] New `failure_type="auto_fix_busy"` used
- [ ] Job requeued immediately instead of stopped-then-retried
- [ ] Test: mock semaphore full → job requeued, not stopped

### Integration
- [ ] All 6 fixes pass `uv run python -m pytest tests/ -v`
- [ ] `uv run ruff check scripts/ tests/` clean
- [ ] Batch of 10 jobs run through pipeline without regressions

## Dependencies & Risks

- **Fix 1 (scrape)** is the highest-impact change (50% of failures) but also the most complex — UA rotation and per-domain throttling need careful coordination with the existing worker pool.
- **Fix 3 (JSON)** carries risk of accepting wrong-but-valid JSON. Mitigated by schema validation.
- **Fix 4 (auth)** could skip valid jobs if credentials are fixed mid-batch. Mitigated by in-memory-only flag that resets on restart.
- Fixes 2, 5, 6 are low-risk, isolated changes.

## Suggested Implementation Order

1. Fix 6 (auto-fix busy) — smallest, most isolated
2. Fix 5 (error capture) — improves debugging for everything else
3. Fix 2 (LLM timeout) — config change + small code tweak
4. Fix 4 (auth skip) — new helper + orchestrator guard
5. Fix 3 (JSON repair) — needs careful testing
6. Fix 1 (scrape resilience) — largest, most impactful

## Sources & References

- Pipeline orchestrator: `scripts/pipeline_orchestrator.py` (retry logic: lines 601-651, transient patterns: lines 50-60)
- JD fetch loop: `scripts/run_pipeline.py` (lines 875-944)
- JSON lenient parser: `scripts/json_lenient.py` (lines 15-26)
- Timeout wrapper: `scripts/run_command_with_timeout.py`
- LLM shell utilities: `scripts/llm_common.sh` (timeout at line 381)
- Worker pipeline docs: `docs/worker-pipeline-patterns.md`
- Database layer: `scripts/job_db.py`
