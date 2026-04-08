---
title: "fix: Requeued jobs should use the current default provider, not the stale one"
type: fix
status: completed
date: 2026-03-23
---

# fix: Requeued jobs should use the current default provider, not the stale one

When a job is requeued (killed, restarted, auto-retried, or stale-reset), the `provider` column in the database is preserved from the original run. Since `pipeline_orchestrator.py:442` overrides the fallback chain when `job["provider"]` is set, requeued jobs always use the old provider — even if the user changed `ASSET_LLM_PROVIDER` in `.env.local`.

## Proposed Solution

Clear the `provider` column (set to `NULL`) on all requeue paths. When `provider` is `NULL`, `_run_phases_1_2()` falls through to `_get_provider_chain()`, which reads the current env default. The provider is only cleared on **requeue**, not on initial queue.

Note: This intentionally clears even user-pinned providers (chosen via the "Add Jobs" dropdown). The user's global env setting is the source of truth for requeued jobs. If a specific provider is needed, the user can re-add the job with that provider selected.

**Bonus:** Clearing provider on auto-retry also enables the fallback chain. Currently, a pinned provider is a single-element list with no fallback — if it fails, it just fails. After this fix, retried jobs get the full chain (e.g., `openai,claude`), improving resilience.

**Implementation approach:** Add `provider = NULL` directly to each SQL UPDATE statement. Don't modify the `update_status()` helper — it's called from many places and adding a kwarg risks unintended side effects.

**Completeness check:** After making the 4 changes below, grep for any other `UPDATE jobs SET status = 'queued'` or `status = 'draft'` statements that don't include `provider = NULL`. `stop_workers()` in `job_web.py` also resets job statuses during shutdown — verify it's covered.

## Changes

### 1. Kill-and-requeue in `scripts/job_worker.py`

Three UPDATE statements in `_force_kill_worker()` (lines 396-426) reset status but don't touch provider. Add `provider = NULL` to each.

- `scripts/job_worker.py:396` — submit-phase jobs → draft
- `scripts/job_worker.py:408` — autofilling jobs → queued
- `scripts/job_worker.py:418` — other in-progress jobs → queued

### 2. Auto-retry in `scripts/pipeline_orchestrator.py`

`_auto_retry_if_transient()` (line 565) requeues with `status = 'queued'` but preserves provider. Add `provider = NULL` to the UPDATE.

- `scripts/pipeline_orchestrator.py:565`

### 3. Manual restart in `scripts/job_web.py`

The restart endpoint (line 755) calls `update_status()` which doesn't clear provider. Clear provider alongside the status reset.

- `scripts/job_web.py:755`

### 4. Stale job reset in `scripts/job_db.py`

`reset_stale_jobs()` or similar (line 743) resets status without clearing provider. Add `provider = NULL`.

- `scripts/job_db.py:743`

## Acceptance Criteria

- [ ] Killing and requeuing a job clears its provider — next run uses env default
- [ ] Manual restart clears provider
- [ ] Auto-retry on transient failure clears provider
- [ ] Stale job reset clears provider
- [ ] Explicitly choosing a provider in the "Add Jobs" form still pins that provider on first run
- [ ] Existing tests pass

## Sources

- Provider override logic: `scripts/pipeline_orchestrator.py:441-443`
- Provider chain: `scripts/llm_provider.py:57-60`
- Kill-requeue: `scripts/job_worker.py:388-428`
- Auto-retry: `scripts/pipeline_orchestrator.py:543-576`
- Manual restart: `scripts/job_web.py:755`
- Stale reset: `scripts/job_db.py:743`
