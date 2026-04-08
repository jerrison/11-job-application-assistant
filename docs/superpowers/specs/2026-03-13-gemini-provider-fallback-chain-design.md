# Gemini Provider + Configurable Fallback Chain

**Date:** 2026-03-13
**Status:** Approved

## Summary

Add Gemini as a first-class LLM provider and replace the hardcoded Claude→Codex fallback with a configurable provider chain. Default priority: Gemini 3.1 Pro → Claude Sonnet 4.6 → GPT 5.4.

## Motivation

The user has an existing Gemini subscription (OAuth-authenticated, `jerrisonli@gmail.com`) and wants it as the primary provider for research and artifact generation. The current 2-tier hardcoded fallback (Claude → Codex) doesn't support a third provider or reordering without code changes.

## Design

### 1. Gemini Provider Command Construction (`llm_provider.py`)

Add a `gemini` provider that builds non-interactive commands:

```
gemini --yolo --model gemini-3.1-pro-preview -p <prompt>
```

New constants:
- `DEFAULT_GEMINI_MODEL = "gemini-3.1-pro-preview"`
- No sandbox, no API key — uses existing OAuth credentials at `~/.gemini/oauth_creds.json`

Env var overrides (following existing pattern):
- `GEMINI_MODEL` — override model ID
- `GEMINI_EXTRA_ARGS` — additional CLI flags

`effective_provider_settings("gemini")` returns all keys that `shell_exports()` accesses via direct dict indexing. Gemini-irrelevant keys (`effort`, `profile`, `reasoning_effort`, `permission_mode`, `mcp_config`, etc.) are set to empty strings:
- `model`: from env or `DEFAULT_GEMINI_MODEL`
- `effort`: `""` (not applicable to Gemini)
- `profile`: `""` (not applicable)
- `reasoning_effort`: `""` (not applicable)
- `extra_args`: from `GEMINI_EXTRA_ARGS` env or `""`
- `timeout_seconds`: shared timeout
- `asset_timeout_seconds`: shared asset timeout
- `asset_primary_timeout_seconds`: `""` (chain handles per-provider timeout uniformly)
- `asset_fallback_provider`: `""` (chain replaces this)
- All Claude/Codex-specific keys (`permission_mode`, `setting_sources`, `no_session_persistence`, `disable_slash_commands`, `strict_mcp_config`, `mcp_config`, `approval_policy`, `sandbox_mode`): `""`

`provider_command("gemini", prompt)` builds the argv list. Unlike Claude/Codex, Gemini does not need:
- Permission mode flags (uses `--yolo`)
- MCP config isolation (Gemini manages its own config at `~/.gemini/`)
- Sandbox mode (disabled per user preference)
- Tool restriction flags (Gemini CLI manages tool access internally; `claude_allowed_tools` parameter is ignored for Gemini)

The `argparse` choices in `main()` are updated from `("claude", "codex")` to `("claude", "codex", "gemini")`.

### 2. Configurable Fallback Chain (`llm_common.sh`)

New env var `ASSET_LLM_PROVIDER_CHAIN` replaces hardcoded fallback logic.

**Default value:** `gemini,claude,codex`

**Behavior of `job_assets_run_prompt_with_fallback()`:**
1. Parse chain into an ordered array of providers.
2. For each provider in the chain:
   a. Check if the provider binary is on PATH. If not, warn and skip.
   b. Run the prompt with 600s timeout per provider attempt.
   c. If the provider exits 0, return success.
   d. If the provider fails but wrote the expected output files (SHA changed), return the exit code (partial success — don't retry).
   e. If the provider fails without writing output files, continue to the next provider.
3. If all providers in the chain fail, return the last exit code.

**Function signature — keep `provider` as optional override:**

Rather than removing the `provider` argument, make the chain the default behavior while allowing explicit override:

```bash
# Signature: job_assets_run_prompt_with_fallback provider prompt_file mode log_file [changed_paths...]
# If provider is "chain" or empty: use ASSET_LLM_PROVIDER_CHAIN
# If provider is a specific name (gemini/claude/codex): single-provider mode (current behavior)
```

This minimizes call-site changes: existing callers pass `"$PROVIDER"` which defaults to `"chain"` via `job_assets_default_provider()`. Callers with explicit `--provider claude` override continue to work as single-provider invocations.

**Log file naming for chain fallbacks:**

New function `job_assets_chain_log_file(base_log, chain_index)`:
- Index 0 (primary): uses the base log file path unchanged (e.g., `llm_research_raw.txt`)
- Index 1+: appends `_fallback_N` (e.g., `llm_research_fallback_1_raw.txt`, `llm_research_fallback_2_raw.txt`)

Replaces the existing `job_assets_fallback_log_file()` function.

**Backward compatibility:**
- If `ASSET_LLM_PROVIDER_CHAIN` is unset and `ASSET_LLM_PROVIDER` is set to a specific provider, single-provider behavior with the legacy `JOB_ASSETS_CLAUDE_ASSET_FALLBACK_PROVIDER` logic is preserved.
- If `ASSET_LLM_PROVIDER_CHAIN` is set, it takes precedence. `JOB_ASSETS_CLAUDE_ASSET_FALLBACK_PROVIDER` and `JOB_ASSETS_CLAUDE_PRIMARY_ASSET_TIMEOUT_SECONDS` are ignored (all providers get uniform 600s per-attempt timeout in chain mode).

### 3. Provider Validation (`llm_common.sh`)

`job_assets_require_provider()` accepts `gemini` as a valid provider name (alongside `claude` and `codex`).

New function `job_assets_resolve_provider_chain()`:
- If `ASSET_LLM_PROVIDER_CHAIN` is set, parses it into an array
- Validates each entry is a known provider (`gemini`, `claude`, `codex`)
- Warns but does not fail if a provider binary is missing — it will be skipped at runtime
- Returns the resolved chain for use by `job_assets_run_prompt_with_fallback()`

### 4. Default Provider Change

`job_assets_default_provider()` returns `"chain"` when `ASSET_LLM_PROVIDER_CHAIN` is set (or defaulting to `gemini,claude,codex`). When provider is `"chain"`, `job_assets_run_prompt_with_fallback()` uses the chain. For display/logging purposes, the first entry in the chain is shown.

For callers that pass `--provider` explicitly (e.g., `./apply.sh --provider claude`), the explicit provider overrides the chain — single-provider mode with legacy fallback behavior.

### 5. Shell Exports (`llm_provider.py`)

`shell_exports("gemini")` produces all keys that Claude and Codex produce, with Gemini-irrelevant values as empty strings:

```bash
export JOB_ASSETS_PROVIDER_MODEL='gemini-3.1-pro-preview'
export JOB_ASSETS_PROVIDER_EFFORT=''
export JOB_ASSETS_PROVIDER_PERMISSION_MODE=''
export JOB_ASSETS_CLAUDE_SETTING_SOURCES=''
export JOB_ASSETS_CLAUDE_NO_SESSION_PERSISTENCE=''
export JOB_ASSETS_CLAUDE_DISABLE_SLASH_COMMANDS=''
export JOB_ASSETS_CLAUDE_STRICT_MCP_CONFIG=''
export JOB_ASSETS_CLAUDE_MCP_CONFIG=''
export JOB_ASSETS_PROVIDER_EXTRA_ARGS=''
export JOB_ASSETS_CLAUDE_PRIMARY_ASSET_TIMEOUT_SECONDS=''
export JOB_ASSETS_CLAUDE_ASSET_FALLBACK_PROVIDER=''
export JOB_ASSETS_PROVIDER_PROFILE=''
export JOB_ASSETS_PROVIDER_REASONING_EFFORT=''
export JOB_ASSETS_PROVIDER_APPROVAL_POLICY=''
export JOB_ASSETS_PROVIDER_SANDBOX_MODE=''
export JOB_ASSETS_PROVIDER_TIMEOUT_SECONDS='600'
export JOB_ASSETS_ASSET_PROVIDER_TIMEOUT_SECONDS='1200'
```

### 6. Submit Provider Integration (`application_submit_common.py`)

Two functions need Gemini awareness beyond what `provider_command_for_mode()` provides:

- **`default_answer_provider()`** (line ~639): Add `gemini` to the auto-detection loop that checks for installed provider binaries. Detection order: `gemini` → `claude` → `codex` (matching the default chain priority).
- **`_answer_generation_fallback_provider()`** (line ~746): Currently hardcoded to fall back from `claude` to `codex`. Refactor to use the provider chain: if the primary answer provider fails, try the next provider in the chain.

### 7. Timeout Behavior

In chain mode, all providers share the same per-attempt timeout:
- Each provider in the chain gets `JOB_ASSETS_PROVIDER_TIMEOUT_SECONDS` (default 600s) per attempt.
- The legacy `JOB_ASSETS_CLAUDE_PRIMARY_ASSET_TIMEOUT_SECONDS` is only honored in single-provider mode (explicit `--provider claude`), not in chain mode.
- Worst case for 3-tier chain: ~1800s (3 × 600s).
- Submit mode: `JOB_ASSETS_PROVIDER_TIMEOUT_SECONDS` (default 600s), single provider (no chain).

### 8. No Gemini Exec Wrapper Needed

Unlike Codex (which requires `codex_exec_wrapper.py` to isolate MCP config), Gemini uses its own config directory (`~/.gemini/`) independent of the project. No wrapper is needed.

### 9. `prompt_mode_settings()` and Gemini

`prompt_mode_settings()` returns `claude_allowed_tools` values. When `provider_command()` is called for Gemini, this parameter is ignored — the Gemini CLI manages tool access internally via `--yolo` mode. The `search_enabled` field is also not applicable (Gemini's tools are always available in `--yolo` mode).

## Files Changed

| File | Change |
|------|--------|
| `scripts/llm_provider.py` | Add `gemini` provider: settings (with all required keys), command builder, shell exports, argparse choices |
| `scripts/llm_common.sh` | Chain-based fallback loop, `gemini` in `require_provider`, `job_assets_resolve_provider_chain()`, `job_assets_chain_log_file()`, default provider → `"chain"` |
| `scripts/llm_worker.sh` | Pass `"chain"` as provider to fallback function when using default |
| `apply.sh` | Pass `"chain"` as provider to fallback function when using default; update help text |
| `parallel_apply.sh` | Pass `"chain"` as provider to fallback function when using default; update help text |
| `scripts/job_assets_pipeline.py` | Add `gemini` to provider validation set (line ~38) |
| `bin/job-assets` | Update default provider from `claude` to chain-aware default |
| `scripts/application_submit_common.py` | Add `gemini` to `default_answer_provider()` detection loop and `_answer_generation_fallback_provider()` |
| `scripts/autofill_greenhouse.py` | Update local `_default_answer_provider()` duplicate to include `gemini` (or refactor to import from shared module) |
| `tests/test_llm_provider.py` | Add Gemini provider tests: settings, command building, shell exports |

## Not Changed

- `AGENTS.md` / `GEMINI.md` — instruction files, not provider infrastructure
- `codex_exec_wrapper.py` — Codex-specific, untouched
- Build scripts (`build_resume.py`, `build_cover_letter.py`) — provider-independent
