---
title: "feat: Codex CLI provider parity — plug-and-play GPT support"
type: feat
status: completed
date: 2026-03-23
origin: docs/brainstorms/2026-03-23-gpt-provider-support-requirements.md
deepened: 2026-03-23
---

# feat: Codex CLI provider parity — plug-and-play GPT support

## Enhancement Summary

**Deepened on:** 2026-03-23
**Research agents used:** Python reviewer, pattern recognition, architecture strategist, performance oracle, security sentinel, code simplicity, agent-native reviewer, Codex CLI best-practices researcher

### Key Improvements from Research
1. **Codex CLI has fundamentally different tools** — shell + `apply_patch`, not Read/Write/Edit. Core prompts use generic "READ"/"SAVE" language and are already compatible.
2. **Interview prep has an unbridgeable Playwright MCP gap** — scope it as Claude-only; don't pretend it works with Codex.
3. **Auto-fix needs unrestricted tool access** — mode `"fix"` is too restrictive; use `provider_command()` directly.
4. **`--ask-for-approval` is NOT available on `codex exec`** — current `llm_provider.py` passes an invalid flag. Must use `--full-auto` instead.
5. **Phase 5 (auth pre-flight) cut as YAGNI** — Codex CLI already produces clear auth errors.
6. **`_provider_binary()` must be consolidated** — lives in `application_submit_common.py`, needs to move to `llm_provider.py`.
7. **Additional hardcoded defaults found** — `job_assets_pipeline.py:34` defaults to `"gemini"`, inconsistent with other files.

### New Risks Discovered
- Process table leakage: prompts with PII visible via `ps aux` (pre-existing, not caused by this plan)
- `codex_exec_wrapper.py` copies `auth.json` without explicit 0o600 permissions (pre-existing)
- Codex `danger-full-access` sandbox has no per-mode restrictions unlike Claude's `--allowedTools` (accepted limitation)

---

## Overview

Make `ASSET_LLM_PROVIDER=codex` a true plug-and-play alternative to Claude. The provider abstraction in `llm_provider.py` already builds Codex commands correctly, but three callsites bypass it entirely, default strings hardcode `"claude"`, and auto-fix is gated on the `claude` binary. After this work, changing one env var switches every LLM call to GPT via Codex CLI with zero code changes.

**Scoped exception:** Interview prep (`generate_interview_prep.py`) stays Claude-only because it depends on Playwright MCP tools that Codex does not have. This is documented, not hidden.

## Problem Statement / Motivation

The user wants provider flexibility — the ability to use GPT models for cost, speed, or quality comparison without touching code. The abstraction layer exists but was never fully closed: hardcoded `claude` references, Claude-only defaults, and an untested Codex end-to-end path prevent a clean switch. (see origin: `docs/brainstorms/2026-03-23-gpt-provider-support-requirements.md`)

## Proposed Solution

Fix every gap between the existing multi-provider abstraction and actual provider-agnostic behavior. No new abstraction — extend the existing pattern established by the Gemini provider implementation.

---

### Phase 1: Empirical verification

**Goal:** Resolve deferred questions from brainstorm. Answer before writing code.
**Gate:** If 1a reveals Codex output is incompatible with `extract_json_object()`, add `--output-schema` support to `provider_command()` before proceeding to Phase 2.

- [ ] **1a.** Verify Codex CLI output format
  - Run: `codex exec "Respond with ONLY valid JSON: {\"test\": true}"` and inspect raw stdout
  - Confirm `extract_json_object()` in `application_submit_common.py:854` can parse the output
  - **File:** N/A (manual)

  > **Research insight:** Codex `exec` sends progress to stderr and only the final agent message to stdout as plain text. This should be compatible with `extract_json_object()` which already handles raw text, markdown fencing, and embedded JSON. Use `--json` flag or `--output-schema` if plain text proves unreliable.

- [ ] **1b.** Verify `--ask-for-approval` flag behavior
  - **CRITICAL FINDING:** `--ask-for-approval` is NOT available on `codex exec` — only on interactive `codex`. Current `llm_provider.py:323` passes this flag and it will fail or be ignored.
  - Verify by running: `codex exec --help` and checking available flags
  - For non-interactive `codex exec`, use `--full-auto` (= `--ask-for-approval on-request` + `--sandbox workspace-write`) or `--dangerously-bypass-approvals-and-sandbox`
  - **File:** `scripts/llm_provider.py:309-337`

- [ ] **1c.** Verify Codex tool architecture
  - **CRITICAL FINDING:** Codex does NOT use Read/Write/Edit/Glob/Grep tools. It uses **shell commands** (cat, rg, sed) + **`apply_patch`** (custom diff format).
  - The core pipeline prompts use generic language ("READ", "SAVE") which works across both tool architectures.
  - Prompts that reference specific Claude tool names (e.g., "Use the Write tool") will be interpreted by Codex as instructions to write via shell commands — likely still works but verify.
  - **File:** N/A (manual)

### Phase 2: Fix all hardcoded references

**Goal:** Route hardcoded `claude` subprocess calls through `llm_provider.py` and fix defaults. This is the core work — one phase, one pass.

#### Hardcoded subprocess calls

- [ ] **2a.** Migrate `job_discovery.py:_call_llm_score()` (line ~220)
  - **Current:** `subprocess.run(["claude", "--print", "-p", prompt], timeout=120)`
  - **Target:** Use `provider_command(provider, prompt)` — scoring is text-in/JSON-out, needs NO tools and NO mode settings
  - Read `provider` from `os.environ.get("ASSET_LLM_PROVIDER", "claude")`
  - **File:** `scripts/job_discovery.py`
  - **Test:** `tests/test_job_discovery.py` — mock subprocess, verify codex command argv

  > **Research insight:** Do NOT use `provider_command_for_mode(mode="submit")` — the "submit" mode adds `--allowedTools Read,Write,Edit,Glob,Grep` which scoring doesn't need. Use bare `provider_command()` or add a lightweight "score" mode with no tools. The timeout of 120s may be tight for Codex (CLI startup overhead); consider 180s.

- [ ] **2b.** Scope `generate_interview_prep.py` as Claude-only (line ~177)
  - **Current:** Hardcoded `["claude", "--print", "-p", ..., "--allowedTools", ...]` with Playwright MCP tools
  - **Decision:** Keep Claude-only. Codex has no Playwright MCP equivalent. Attempting to force parity would silently degrade quality.
  - **Change:** Add a guard: if configured provider is not `claude`, attempt to fall back to `claude` for interview prep. If `claude` is also unavailable, skip interview prep entirely with a clear warning ("Interview prep requires Claude CLI").
  - Fix error messages at lines ~192, ~201 to name the provider actually used
  - **File:** `scripts/generate_interview_prep.py`

  > **Research insight:** The system prompt at `scripts/prompts/interview_prep_system.md:78` references "WebFetch" and "Playwright MCP tools" by name — these are Claude-specific. Rewriting prompts is out of scope (per origin doc). The right call is to keep this Claude-only and document it.

- [ ] **2c.** Migrate `pipeline_orchestrator.py:auto_fix()` (lines ~1237-1262)
  - **Current:** `shutil.which("claude")` gate + `subprocess.run(["claude", "--print", "-p", prompt], timeout=300)`
  - **Target:** Use `provider_command(provider, prompt)` with NO mode restrictions — auto-fix creates git branches, edits files, runs tests, and needs unrestricted tool access
  - Read `provider` from `os.environ.get("ASSET_LLM_PROVIDER", "claude")` (same pattern as 2a)
  - Rename `claude_result` → `fix_result`, update docstring and log messages to be provider-agnostic
  - Note: the `shutil.which("claude")` guards at lines ~1050 and ~1237 are fixed in **2f** (depends on `provider_binary()` existing first)
  - **File:** `scripts/pipeline_orchestrator.py`
  - **Test:** `tests/test_pipeline_orchestrator.py` — add codex auto-fix variant

  > **Research insight:** Do NOT use `provider_command_for_mode(mode="fix")` — the "fix" mode only allows `Read,Write,Edit` which is too restrictive for auto-fix. Use bare `provider_command()`. Claude's `--permission-mode auto` grants full access; Codex's `--sandbox danger-full-access` is the equivalent (already the default).

#### Hardcoded default strings

- [ ] **2d.** Fix default chain derivation (3+1 locations)
  - Add `default_provider_chain()` helper to `llm_provider.py`:
    ```python
    def default_provider_chain() -> str:
        return os.environ.get("ASSET_LLM_PROVIDER_CHAIN",
                              os.environ.get("ASSET_LLM_PROVIDER", "claude"))
    ```
  - Replace hardcoded `"claude"` defaults:
    - `pipeline_orchestrator.py:73` → `DEFAULT_PROVIDER_CHAIN = "claude"` → use helper
    - `application_submit_common.py:1058` → `os.environ.get(..., "claude")` → use helper
    - `llm_common.sh:29` → `"${ASSET_LLM_PROVIDER_CHAIN:-claude}"` → `"${ASSET_LLM_PROVIDER_CHAIN:-${ASSET_LLM_PROVIDER:-claude}}"`
    - `_answer_generation_fallback_provider()` in `application_submit_common.py:1057` — also reads chain with `"claude"` default (4th location missed in original plan)
    - `llm_common.sh:481,504` — Claude-specific timeout branching in `_job_assets_run_single_provider_with_legacy_fallback()`. This function gives Claude a special primary timeout before falling back to codex. Make the branching provider-agnostic: apply the primary/fallback timeout pattern to whichever provider is configured, not just Claude. If only one provider is in the chain, skip the fallback timeout entirely.
  - **Files:** `scripts/llm_provider.py`, `scripts/pipeline_orchestrator.py`, `scripts/application_submit_common.py`, `scripts/llm_common.sh`
  - **Test:** `tests/test_llm_provider.py` — test chain derivation: `{chain=codex → codex}`, `{no chain, provider=codex → codex}`, `{no chain, no provider → claude}`

  > **Research insight:** Pattern recognition found `job_assets_pipeline.py:34` defaults to `"gemini"` while everything else defaults to `"claude"`. This is an existing inconsistency — note it but don't fix in this plan (different scope). Also, when only `ASSET_LLM_PROVIDER=codex` is set and chain defaults to single-element `"codex"`, there is no fallback if Codex fails. This is acceptable — the user explicitly chose codex. If they want resilience, they set `ASSET_LLM_PROVIDER_CHAIN=codex,claude`.

- [ ] **2e.** Fix hardcoded `"claude"` default in `autofill_bamboohr.py:643`
  - **Current:** `os.environ.get("ASSET_LLM_PROVIDER", "claude")`
  - **Target:** Same pattern: `os.environ.get("ASSET_LLM_PROVIDER", "claude")` is actually fine — it reads the env var correctly, and `"claude"` as ultimate fallback is acceptable. No change needed unless we want to call `default_provider_chain()`.
  - **Decision:** Leave as-is. The env var read is correct; `"claude"` as the no-env-var fallback matches the system default.

#### Provider-aware guards

- [ ] **2f.** Move `_provider_binary()` to `llm_provider.py` and fix guards
  - `_provider_binary()` currently lives in `application_submit_common.py:1050`. It maps provider names to binary names (e.g., `"gemini-flash"` → `"gemini"`).
  - Move to `llm_provider.py` as `provider_binary(provider: str) -> str` (public)
  - Update imports in `application_submit_common.py` and `job_assets_pipeline.py`
  - Replace `shutil.which("claude")` guards in `pipeline_orchestrator.py` with `shutil.which(provider_binary(active_provider))`
  - **Files:** `scripts/llm_provider.py`, `scripts/application_submit_common.py`, `scripts/pipeline_orchestrator.py`, `scripts/job_assets_pipeline.py`

- [ ] **2g.** Fix `--ask-for-approval` flag in `llm_provider.py` Codex command builder
  - **Current:** Line ~323 passes `--ask-for-approval` to `codex exec` — this flag doesn't exist on `codex exec`
  - **Target:** Replace `--ask-for-approval` AND `--sandbox` with a single convenience flag:
    - `danger-full-access` (the default) → `--dangerously-bypass-approvals-and-sandbox` (implies both sandbox=danger-full-access and no approvals)
    - `workspace-write` → `--full-auto` (implies both sandbox=workspace-write and approval=on-request)
    - `read-only` → `--sandbox read-only` only (no approval flag needed)
  - This simplifies the command builder: one flag instead of two
  - **File:** `scripts/llm_provider.py:309-337`
  - **Test:** `tests/test_llm_provider.py` — verify codex command argv does not contain `--ask-for-approval` or redundant `--sandbox` flags

### Phase 3: Documentation & configuration

- [ ] **3a.** Add Codex CLI setup instructions
  - Install: `brew install codex` (macOS) or `npm install -g @openai/codex`
  - Authenticate: `codex login` (browser-based OAuth) or `printenv OPENAI_API_KEY | codex login --with-api-key`
  - Configure `.env.local`:
    ```bash
    ASSET_LLM_PROVIDER=codex
    # Optional fallback chain:
    # ASSET_LLM_PROVIDER_CHAIN=codex,claude
    ```
  - Document all Codex-specific env vars: `CODEX_MODEL`, `CODEX_REASONING_EFFORT`, `CODEX_SANDBOX_MODE`, `CODEX_PROFILE`, `CODEX_EXTRA_ARGS`, `JOB_ASSETS_CODEX_CONFIG_PATH`
  - Document the interview prep exception (Claude-only)
  - **File:** `docs/provider-setup.md` (new) or extend `docs/cli-reference.md`

- [ ] **3b.** Update `agent_preferences.md`
  - **Current:** "Uses Claude Sonnet 4.6 as sole LLM provider... Do NOT use Gemini, GPT, or other providers"
  - **Target:** Document that the provider is configurable via `ASSET_LLM_PROVIDER`, with Claude as the default
  - **File:** `agent_preferences.md`

- [ ] **3c.** Update `.env.local` with commented-out Codex examples
  - **File:** `.env.local`

### Phase 4: Test & verify

- [ ] **4a.** Unit tests with mocked subprocesses
  - Parameterize `test_llm_provider.py` to verify codex command argv (especially: no `--ask-for-approval`, correct `--full-auto`/`--dangerously-bypass-approvals-and-sandbox`)
  - Add codex variants to `test_pipeline_orchestrator.py` (auto-fix), `test_job_discovery.py` (scoring)
  - Test `default_provider_chain()` with all env var combos
  - Test `provider_binary()` mapping

- [ ] **4b.** Integration smoke test
  - Run a simple prompt through `codex exec` → verify stdout captured correctly
  - Run a JSON prompt → verify `extract_json_object()` parses the result
  - Run `codex_exec_wrapper.py` → verify auth isolation works

- [ ] **4c.** End-to-end verification
  - Set `ASSET_LLM_PROVIDER=codex` in `.env.local`
  - Run full pipeline for one job: research → draft → build → submit (draft mode)
  - Verify: resume PDF generated, cover letter generated, form answers produced

---

## Technical Considerations

- **Codex tool architecture is different**: Codex uses shell commands + `apply_patch` instead of discrete Read/Write/Edit/Glob/Grep tools. This does NOT break the pipeline because core prompts use generic language ("READ", "SAVE"). The `provider_command()` function already handles the difference: Claude gets `--allowedTools`, Codex gets `--search` for web access and nothing else (all tools available by default).

- **Codex stdout format**: `codex exec` sends progress to stderr, final message to stdout as plain text. For structured output, use `--output-schema` or `--json`. The default plain-text output should be compatible with `extract_json_object()`.

- **No `--print` equivalent needed**: `codex exec "prompt"` IS the non-interactive equivalent of `claude --print -p "prompt"`. No flag needed.

- **`--ask-for-approval` invalid on `codex exec`**: Must be replaced with `--full-auto` or `--dangerously-bypass-approvals-and-sandbox`. This is a bug in the current `llm_provider.py` Codex command builder.

- **Interview prep is Claude-only**: Depends on Playwright MCP tools. Codex strips MCP servers via `codex_exec_wrapper.py`. No clean workaround without rewriting prompts.

- **Timeout considerations**: Codex CLI has additional startup overhead (Python wrapper + CLI init). The 120s scoring timeout may be tight — consider 180s. The 300s auto-fix timeout should be adequate. The 600s/1200s pipeline timeouts are generous enough.

- **Per-mode tool restriction gap**: Claude enforces `--allowedTools` per mode; Codex has no equivalent. Codex's sandbox modes (`read-only`, `workspace-write`, `danger-full-access`) are a partial substitute but coarser-grained. Accepted as a known limitation.

## System-Wide Impact

- **Interaction graph**: Changing `ASSET_LLM_PROVIDER` affects: `run_pipeline.py` → `apply.sh` → `llm_common.sh` → `llm_provider.py` → subprocess. Also: `pipeline_orchestrator.py` → `provider_fallback()` → subprocess. Also: `application_submit_common.py` → `_run_answer_generation_provider()` → subprocess. Also: `job_discovery.py` → `_call_llm_score()` → subprocess (currently broken). Exception: `generate_interview_prep.py` stays Claude-only.
- **Error propagation**: Provider failures bubble up as non-zero exit codes. `provider_fallback()` retries rate-limited errors and falls back. Works identically for Codex.
- **State lifecycle risks**: None — provider switching doesn't affect persisted state.
- **API surface parity**: CLI, TUI, worker, web app all consume the same `ASSET_LLM_PROVIDER` env var.

## Acceptance Criteria

- [ ] `ASSET_LLM_PROVIDER=codex` routes all pipeline LLM calls (research, draft, fix, submit, scoring) through Codex CLI
- [ ] Interview prep documented as Claude-only with graceful fallback
- [ ] No hardcoded `"claude"` subprocess calls in Python (only as ultimate fallback in env var defaults)
- [ ] `ASSET_LLM_PROVIDER_CHAIN=codex,claude` falls back correctly
- [ ] Setting only `ASSET_LLM_PROVIDER=codex` defaults the chain to `codex`
- [ ] Codex command builder uses correct flags (`--full-auto` not `--ask-for-approval`)
- [ ] `provider_binary()` consolidated in `llm_provider.py`
- [ ] All existing tests pass with both `claude` and `codex` as configured provider
- [ ] Setup docs cover install, auth, configuration, and known limitations
- [ ] End-to-end pipeline run with Codex produces usable output

## Dependencies & Risks

- **Codex CLI must be installed and authenticated** — `codex` binary on PATH with valid auth via `codex login`
- **Output format risk** — Codex stdout format may not be compatible with `extract_json_object()`. Mitigated by Phase 1a empirical verification. Fallback: use `--output-schema` for structured JSON.
- **Quality risk** — GPT models may produce different quality with same prompts. Out of scope per origin doc.
- **Flag compatibility risk** — `--ask-for-approval` bug in current `llm_provider.py` must be fixed. Phase 2g addresses this.
- **Interview prep gap** — Cannot run with Codex. Documented and handled with fallback.

## Deferred Work (not in scope)

- Deduplicating `default_answer_provider()` from `application_submit_common.py` and `autofill_greenhouse.py` — separate refactoring concern
- Deduplicating `extract_json_object()`, `validate_generated_answers()`, and related functions from the same files — same
- Renaming `claude_allowed_tools` to `allowed_tools` in the provider abstraction — mechanical rename, separate PR
- Reconciling `job_assets_pipeline.py:34` defaulting to `"gemini"` vs everywhere else defaulting to `"claude"` — existing inconsistency, different scope
- Process table PII leakage via CLI arguments (security finding) — pre-existing issue, needs its own plan
- Per-mode sandbox restrictions for Codex (equivalent to Claude's `--allowedTools`) — accepted limitation
- Caching `_runtime_codex_defaults()` TOML reads — performance optimization, separate PR

## Sources & References

### Origin

- **Origin document:** [docs/brainstorms/2026-03-23-gpt-provider-support-requirements.md](docs/brainstorms/2026-03-23-gpt-provider-support-requirements.md) — Key decisions: Codex CLI over direct API (agent parity), global toggle over per-task config (simpler surface).

### Internal References

- Provider abstraction: `scripts/llm_provider.py` (central registry, 475 lines)
- Codex wrapper: `scripts/codex_exec_wrapper.py` (MCP isolation)
- Provider fallback: `scripts/pipeline_orchestrator.py:84-163`
- Shell provider layer: `scripts/llm_common.sh`
- Gemini provider precedent: `docs/superpowers/specs/2026-03-13-gemini-provider-fallback-chain-design.md`
- Provider tests: `tests/test_llm_provider.py`
- Codex config: `~/.codex/config.toml` (model: gpt-5.4, reasoning: xhigh)

### External References

- [Codex CLI reference](https://developers.openai.com/codex/cli/reference)
- [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive)
- [Codex config reference](https://developers.openai.com/codex/config-reference)
- [Codex models](https://developers.openai.com/codex/models)

### Hardcoded `claude` references (complete audit)

| File | Line | Type | Fix |
|------|------|------|-----|
| `scripts/job_discovery.py` | ~220 | Subprocess call | Phase 2a |
| `scripts/generate_interview_prep.py` | ~177 | Subprocess call + Claude MCP tools | Phase 2b (Claude-only exception) |
| `scripts/pipeline_orchestrator.py` | ~1050 | `shutil.which("claude")` guard | Phase 2f |
| `scripts/pipeline_orchestrator.py` | ~1237 | `shutil.which("claude")` guard | Phase 2f |
| `scripts/pipeline_orchestrator.py` | ~1262 | Subprocess call | Phase 2c |
| `scripts/pipeline_orchestrator.py` | 73 | `DEFAULT_PROVIDER_CHAIN = "claude"` | Phase 2d |
| `scripts/application_submit_common.py` | ~1050 | `_provider_binary()` (to consolidate) | Phase 2f |
| `scripts/application_submit_common.py` | ~1058 | Default chain string | Phase 2d |
| `scripts/autofill_bamboohr.py` | ~643 | Default provider string | Phase 2e (leave as-is) |
| `scripts/llm_common.sh` | ~29 | Default chain string | Phase 2d |
| `scripts/llm_common.sh` | ~481,504 | Claude-specific timeout branching in legacy fallback | Phase 2d |
| `scripts/llm_provider.py` | ~323 | `--ask-for-approval` (invalid for exec) | Phase 2g |
| `scripts/generate_interview_prep.py` | ~192,201 | Error message text | Phase 2b |
| `scripts/pipeline_orchestrator.py` | ~1229,1261,1269 | Docstring + variable name + log | Phase 2c |
| `scripts/job_db.py` | ~1307 | Log content scan (cosmetic) | Skip (read-only) |
