# Smarter Research Cache Invalidation Design

## Problem

The research cache uses a hardcoded 7-day TTL based on file mtime. This has two issues:

1. **No content awareness** — applying to a different role at the same company reuses the same cached research, even though `role_context` should differ per JD.
2. **Hardcoded TTL** — 7 days is too short for stable company info (mission, culture, leadership) and not configurable.

## Goals

- **Content-hash-based invalidation** for role-specific research — cache busts when the JD changes
- **Configurable TTL** for all research caches — default 30 days, overridable via env var
- **Split company vs role research** — company info shared across roles, role context per-JD
- **Backward compatible** — old cache files still work, no manual migration

## Architecture: Two-Tier Cache

### Company Cache — `output/{company}/research_cache.json`

Shared across all roles at a company. Contains company-level research only.

- **Fields:** `company`, `researched_at`, `mission`, `vision`, `culture`, `leadership`, `product`, `growth`, `recent_news`, `tech_stack`
- **No `role_context`** — that moves to the role cache
- **Cache key:** company name (directory path)
- **Invalidation:** configurable TTL via `JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS` env var, default 30 days. Based on file mtime (same mechanism as today, just different default).

### Role Cache — `output/{company}/{role}/role_research_cache.json` (new file)

Per-role, per-JD. Contains role-specific research informed by the JD and company cache.

- **Fields:** `role_context`, `researched_at`, `jd_hash`
- **Cache key:** SHA-256 hash of `jd_parsed.json` content, stored as `jd_hash` field
- **Invalidation:** JD hash mismatch OR TTL exceeded (same configurable TTL, default 30 days)

### Cache Hit Logic

Both caches must be fresh to skip research entirely:
- **Company cache:** file exists AND `mtime < TTL`
- **Role cache:** file exists AND `jd_hash` matches current `jd_parsed.json` AND `mtime < TTL`

Partial hit scenarios:
- Company fresh + role stale → only run role research prompt (1 small LLM call)
- Company stale + role stale → run company research, then role research (2 LLM calls)
- Both fresh → 0 LLM calls

## Changes to Existing Code

### `scripts/llm_common.sh`

1. **`job_assets_cache_is_fresh()`** (lines 125-144) — change `max_age_days` default from `7` to read from `${JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS:-30}`. No signature change.

2. **New function: `job_assets_role_cache_is_fresh(role_cache_path, jd_parsed_path, max_age_days)`** — checks:
   - File exists
   - `mtime < max_age_days * 86400`
   - `jd_hash` field in JSON matches `job_assets_file_sha256 "$jd_parsed_path"` (reuses the existing `job_assets_file_sha256` function at line 94, no new hash function needed)
   - If `jd_parsed.json` doesn't exist, treats role cache as stale (returns 1) — this triggers research which will fail fast with a clear error if the JD truly isn't available
   - Returns exit code 0 (fresh) or 1 (stale)

4. **Research prompt split** (lines 146-180): split into two prompts:
   - **Company research prompt** — input: company name, JD (for context). Output: writes company-level fields to `research_cache.json`. No `role_context`.
   - **Role research prompt** — input: `jd_parsed.json`, company research cache. Output: writes `role_context`, `researched_at`, `jd_hash` to `role_research_cache.json`.

5. **Drafting prompt** (lines 182-230): reads from both `research_cache.json` and `role_research_cache.json`, merging into the same context. Falls back to `role_context` from company cache if `role_research_cache.json` doesn't exist (backward compatibility).

### `scripts/llm_worker.sh`

- Line 45: replace hardcoded `7` with `${JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS:-30}`
- Add role cache path variable: `role_research_cache="${content_dir}/role_research_cache.json"`
- After company cache check, add role cache freshness check using `job_assets_role_cache_is_fresh`
- If company fresh but role stale: skip company research, run only role research prompt
- Lock/poll mechanism unchanged (optimization #4 is separate)

### `apply.sh`

- Line 211: replace hardcoded `7` with `${JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS:-30}`
- Add role cache freshness check after company cache check
- Same partial-hit logic: if company fresh but role stale, run only role research

### `scripts/application_submit_common.py`

- `generate_application_answers()` reads `research_cache.json` for LLM context during submit-time answer generation. Update to also read `role_research_cache.json` and merge `role_context` into the prompt context. Falls back to `role_context` from company cache if role cache doesn't exist.

### `AGENTS.md` and `GEMINI.md`

- Update references to the 7-day TTL and `research_cache.json` as the single research output. Describe the two-tier cache, the new `role_research_cache.json` file, and the configurable TTL. These files are the LLM agent's prompt instructions — they must match the implementation.

### `CLAUDE.md` and `.github/copilot-instructions.md`

- Add note about two-tier research cache architecture if relevant sections exist.

### `tests/test_llm_common.py`

- Update existing tests that assert the research prompt writes to `research_cache.json` (line 30) and the drafting prompt reads from it (line 50). Tests must reflect the prompt split and two-file read.

### `scripts/autofill_greenhouse.py`

- Line 2216: loads `research_cache.json` via `_load_optional_json` and passes it into `_build_application_answers_prompt`. Update to also read `role_research_cache.json` and merge `role_context` into the prompt context. Same treatment as `application_submit_common.py`. (This is a pre-existing duplication between Greenhouse's inline answer generation and the shared `application_submit_common.py` — deduplication is tracked separately under the Greenhouse consolidation effort.)

### No Changes To

- `parse_jd.py`, `scrape_job.py`, `run_pipeline.py` — JD pipeline unchanged
- `asset_pipeline_state.py` — content/build state tracking is separate
- `autofill_ashby.py`, `autofill_lever.py`, `autofill_gem.py`, `autofill_dover.py` — these board scripts don't read the research cache directly (they use `application_submit_common.generate_application_answers()` which is updated above)
- `autofill_common.py`, `autofill_pipeline.py` — unrelated

## JD Hash Computation

Reuses the existing `job_assets_file_sha256()` function (line 94 of `llm_common.sh`) which computes SHA-256 via `shasum -a 256`. No new function needed — `job_assets_role_cache_is_fresh` calls `job_assets_file_sha256 "$jd_parsed_path"` and compares against the stored `jd_hash` field.

## Role Cache File Format

```json
{
  "role_context": "...",
  "researched_at": "2026-03-13T10:30:00+00:00",
  "jd_hash": "a1b2c3d4e5f6..."
}
```

## Backward Compatibility

- Old `research_cache.json` files with `role_context` still work — the drafting prompt falls back to reading `role_context` from the company cache when `role_research_cache.json` doesn't exist.
- First run after migration: company cache is treated as fresh if within TTL (mtime check). Role cache is missing, so role research runs once per active role. One-time cost.
- No manual migration step.

## Configuration

| Env Var | Default | Description |
|---------|---------|-------------|
| `JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS` | `30` | TTL in days for both company and role caches. Set to `0` to force re-research every run. |

## Testing

- Unit test `job_assets_role_cache_is_fresh()` — fresh when hash matches + within TTL, stale when hash differs, stale when over TTL, stale when `jd_parsed.json` missing
- Integration test: verify drafting prompt reads from both cache files
- Update existing tests in `tests/test_llm_common.py` that reference the research prompt writing to `research_cache.json` and the drafting prompt reading from it
- Existing tests must keep passing (`job_assets_cache_is_fresh` signature unchanged)

## Cost Impact

| Scenario | Before | After |
|----------|--------|-------|
| Same role re-run within TTL | 0 LLM calls | 0 LLM calls |
| New role at known company (within TTL) | 1 full research call | 1 small role-only call |
| Same role after TTL | 1 full research call | 2 calls (company + role, but individually smaller/cheaper than the monolithic call) |
| Different role after TTL | 1 full research call | 2 calls (company + role) |

The main win is the second row — applying to multiple roles at the same company no longer re-researches the company each time. For row 3, the two split calls are individually smaller than the original monolithic call (company research doesn't need JD context, role research doesn't need web search), so total token cost is similar.
