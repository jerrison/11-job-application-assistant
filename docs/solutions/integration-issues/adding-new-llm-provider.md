---
title: "OpenAI provider blocked by stale DB state, shell validation gaps, and worker env inheritance"
category: integration-issues
date: 2026-03-23
tags:
  - llm-provider
  - openai
  - env-config
  - worker-lifecycle
  - requeue
  - shell-validation
components:
  - scripts/llm_common.sh
  - scripts/job_worker.py
  - scripts/pipeline_orchestrator.py
  - scripts/job_web.py
  - scripts/job_db.py
  - scripts/job_tui.py
  - scripts/project_env.py
problem_type: cascading-failure
severity: critical
---

# Adding a New LLM Provider — Three Cascading Blockers

## Problem

Switching the default LLM provider from `claude` to `openai` (via `.env.local`) caused all jobs to fail with: `"ERROR: Unsupported provider 'openai'. Use 'gemini', 'claude', or 'codex'."`

Even after fixing the error, requeued jobs still used the old provider, and restarting workers didn't pick up config changes. Three independent failure modes had to be resolved in sequence.

## Root Cause

The system validates and stores providers at multiple independent layers (Python, shell, database) with no shared source of truth. Adding `openai` to `llm_provider.py` (Python) did not propagate to `llm_common.sh` (shell) or the requeue SQL paths (database). Additionally, worker processes inherit a frozen env snapshot from the web server.

---

## Issue 1: Shell Validation Rejects `openai`

`llm_common.sh::job_assets_require_provider()` has a hardcoded case statement (`claude|codex|gemini`) and three `command -v "$provider"` binary checks. OpenAI has no CLI binary — it uses `scripts/openai_provider.py` as a subprocess shim invoked through `llm_provider.py --command`.

**Locations:**
- Case statement: `scripts/llm_common.sh:65`
- Binary checks: `scripts/llm_common.sh:76, 424, 515`

**Fix:**
```bash
# Case statement — add openai
claude|codex|gemini|openai) ;;

# Binary check — early return for shim-based providers
[[ "$provider" = "openai" ]] && return 0

# Chain/fallback binary checks — guard condition
if [[ "$current_provider" != "openai" ]] && ! command -v "$current_provider" >/dev/null 2>&1; then
```

## Issue 2: Requeued Jobs Keep Stale Provider

`pipeline_orchestrator.py:442` overrides the env fallback chain when `job["provider"]` is set. All 7 requeue paths across 5 files preserved the `provider` column, locking jobs to the original provider even after config changes.

**Affected paths:**

| File | Paths |
|------|-------|
| `job_worker.py` | 3 in `_force_kill_worker()`: submit->draft, autofilling->queued, other->queued |
| `pipeline_orchestrator.py` | auto-retry (`_auto_retry_if_transient`), `regenerate_job()` |
| `job_web.py` | `stop_workers()` (2 paths), manual restart endpoint |
| `job_db.py` | stale job reset (2 paths) |
| `job_tui.py` | TUI worker stop |

**Fix:** Add `provider = NULL` to every requeue SQL UPDATE statement:
```python
# Before
"UPDATE jobs SET status = 'queued', progress = '' WHERE id = ?"
# After
"UPDATE jobs SET status = 'queued', provider = NULL, progress = '' WHERE id = ?"
```

**Bonus:** Clearing provider on retry re-enables the full fallback chain. Previously, pinned providers had no fallback — if the provider failed, the job just failed.

## Issue 3: Worker Env Inheritance (No Code Fix)

Workers are spawned as subprocesses inheriting `os.environ` from the web server. `load_project_env()` in `project_env.py` skips keys already in `os.environ` (line 59: `if key in locked_keys: continue`). Restarting workers doesn't help because they inherit the same frozen env.

**Resolution:** Restart the web server itself (not just workers) after changing `.env.local`.

---

## Verification

Job #220 (Moloco — group-pm-supply-quality) successfully processed with `provider: openai` after all three fixes.

## Investigation Steps

1. Changed `.env.local` to `ASSET_LLM_PROVIDER=openai` — jobs still used `claude` (worker env inheritance)
2. Restarted web server — jobs still used `claude` (stale provider in DB)
3. Manually cleared `provider = NULL` on job — error: "Unsupported provider 'openai'" (shell validation)
4. Fixed shell validation — job processed successfully with OpenAI

## Prevention: New Provider Checklist

When adding any new LLM provider, complete ALL of the following:

- [ ] Add to `VALID_PROVIDERS` in `scripts/llm_provider.py`
- [ ] Add branch in `effective_provider_settings()` and `provider_command()`
- [ ] Add to case statement in `scripts/llm_common.sh:job_assets_require_provider()`
- [ ] Add binary-check exemption if provider uses a Python shim (no binary)
- [ ] Update error message listing valid providers
- [ ] Update `command -v` guards in chain mode and legacy fallback
- [ ] Verify all requeue paths include `provider = NULL`
- [ ] Update `.env.local` example and `docs/provider-setup.md`
- [ ] Run full test suite: `uv run python -m pytest tests/ -v`

## Architectural Improvement (Future)

The shell `case` statement in `llm_common.sh` is a redundant copy of `VALID_PROVIDERS` in Python. The shell validation should delegate to Python (`llm_provider.py --shell "$provider"` already validates) rather than maintaining a parallel list. This would reduce the sync surface to a single source of truth and prevent this class of bug entirely.

## Cross-References

- Plans: `docs/plans/2026-03-23-004-fix-requeue-clears-stale-provider-plan.md`, `docs/plans/2026-03-23-005-fix-shell-validation-rejects-openai-provider-plan.md`
- Stale docs: `docs/cli-reference.md` — provider list missing `openai` in 3 places
- Provider setup: `docs/provider-setup.md` (current, updated same day)
