---
title: "feat: Direct OpenAI API provider — paste API key, everything works"
type: feat
status: completed
date: 2026-03-23
origin: docs/brainstorms/2026-03-23-openai-api-provider-requirements.md
---

# feat: Direct OpenAI API provider — paste API key, everything works

## Overview

Add an `openai` provider that calls the OpenAI Responses API directly via the `openai` Python SDK. Setting `OPENAI_API_KEY` + `ASSET_LLM_PROVIDER=openai` in `.env.local` runs the full pipeline (scoring, answers, research, drafting) with no CLI tool installation needed. Auto-fix and interview prep fall back to Claude automatically.

## Problem Statement / Motivation

The Codex CLI path (built earlier today) requires ChatGPT OAuth — standard `sk-proj-` API keys fail because Codex CLI's agent tools use a `"custom"` tool type unsupported by the standard Responses API. Users need a "paste API key, everything works" path. (see origin: `docs/brainstorms/2026-03-23-openai-api-provider-requirements.md`)

## Proposed Solution: Subprocess Shim

Create `scripts/openai_provider.py` — a standalone Python script that:
1. Reads the prompt from a CLI argument (or stdin for long prompts)
2. Reads `OPENAI_API_KEY` from environment (already loaded by `project_env.py`)
3. Calls the OpenAI Responses API with the appropriate settings
4. Prints the response text to stdout
5. Exits 0 on success, non-zero on failure

`provider_command("openai", prompt)` returns `[sys.executable, "scripts/openai_provider.py", ...]` — same pattern as `codex_exec_wrapper.py`. **Zero callsite changes needed.** All existing callers (`subprocess.run(cmd, capture_output=True)` → read `stdout`) work unchanged.

For research mode: adds `tools=[{"type": "web_search"}]` to the API call.
For JSON mode: adds `text={"format": {"type": "json_object"}}`.

---

### Phase 1: Add `openai` dependency and provider script

- [ ] **1a.** Add `openai` to `pyproject.toml` dependencies
  - Run: `uv add openai`
  - **File:** `pyproject.toml`

- [ ] **1b.** Create `scripts/openai_provider.py` subprocess shim
  - CLI interface: `python openai_provider.py [--model MODEL] [--search] [--json-mode] [--timeout SECS] PROMPT`
  - Also accept prompt from stdin when `PROMPT` is `-` (for long prompts)
  - Reads `OPENAI_API_KEY` from environment (loaded by `project_env.py` from `.env.local`)
  - Uses the **Responses API** (`client.responses.create()`) — recommended over Chat Completions
  - Default model: `gpt-5.4-mini` (best cost/performance: $0.75/$4.50 per MTok, 400K context)
  - When `--search`: adds `tools=[{"type": "web_search"}]`
  - When `--json-mode`: adds `text={"format": {"type": "json_object"}}`
  - Prints `response.output_text` to stdout
  - Prints errors/diagnostics to stderr
  - Exit code 0 on success, 1 on error
  - Built-in retries via SDK (`max_retries=3`)
  - Configurable timeout (default 180s)
  - **File:** `scripts/openai_provider.py` (new)
  - **Test:** `tests/test_openai_provider.py` — mock `openai.OpenAI` client, verify stdout output

### Phase 2: Wire into `llm_provider.py`

- [ ] **2a.** Add `"openai"` to `VALID_PROVIDERS`
  - **Current:** `("gemini", "gemini-flash", "claude", "codex")`
  - **Target:** `("gemini", "gemini-flash", "claude", "codex", "openai")`
  - **File:** `scripts/llm_provider.py`

- [ ] **2b.** Add `openai` branch to `effective_provider_settings()`
  - Env vars: `OPENAI_MODEL` (default `gpt-5.4-mini`), `OPENAI_EXTRA_ARGS`, `OPENAI_API_KEY`
  - Return dict with: `model`, `timeout_seconds`, `asset_timeout_seconds`, plus empty strings for Claude/Codex-specific keys (matching the Gemini pattern)
  - **File:** `scripts/llm_provider.py`

- [ ] **2c.** Add `openai` branch to `provider_command()`
  - Build: `[sys.executable, str(OPENAI_PROVIDER_SCRIPT), "--model", model]`
  - When `search_enabled`: add `--search`
  - Append prompt as last arg (or use stdin for prompts > 100K chars)
  - **File:** `scripts/llm_provider.py`

- [ ] **2d.** Handle `provider_binary("openai")` — no binary needed
  - `provider_binary()` currently maps provider names to CLI binary names
  - For `openai`: return `"python"` (or `sys.executable`) since the provider is a Python script, not an external binary
  - This makes `shutil.which(provider_binary("openai"))` return a valid path (Python is always available)
  - **File:** `scripts/llm_provider.py`

- [ ] **2e.** Add `openai` to `shell_exports()` and CLI `--shell` / `--command` modes
  - `shell_exports()` must return all expected keys for the shell layer
  - The CLI `--command` mode must produce NUL-delimited argv for shell consumption
  - **File:** `scripts/llm_provider.py`

### Phase 3: Auto-fix and interview prep fallback

- [ ] **3a.** Make `auto_fix()` always use Claude when provider is `openai`
  - In `pipeline_orchestrator.py:auto_fix()`, if `active_provider == "openai"`, override to `"claude"`
  - If Claude is unavailable, skip auto-fix with a warning (same as interview prep pattern)
  - **File:** `scripts/pipeline_orchestrator.py`

- [ ] **3b.** Update the `process_job()` auto-fix guard similarly
  - The guard at line ~1051 checks `shutil.which(provider_binary(_active_provider))`
  - For `openai`, this would pass (Python exists) but auto-fix can't work via API — add explicit check
  - **File:** `scripts/pipeline_orchestrator.py`

### Phase 4: Configuration and documentation

- [ ] **4a.** Update `.env.local` with OpenAI API configuration
  - Add commented example:
    ```bash
    # ASSET_LLM_PROVIDER=openai
    # OPENAI_API_KEY=sk-proj-...
    # OPENAI_MODEL=gpt-5.4-mini    # optional
    ```
  - **File:** `.env.local`

- [ ] **4b.** Update `docs/provider-setup.md`
  - Add OpenAI API section: install (nothing — just an API key), configure, limitations
  - Document that auto-fix and interview prep fall back to Claude
  - **File:** `docs/provider-setup.md`

- [ ] **4c.** Update `agent_preferences.md`
  - Add `openai` to the list of supported providers
  - **File:** `agent_preferences.md`

### Phase 5: Tests

- [ ] **5a.** Unit tests for `openai_provider.py`
  - Mock `openai.OpenAI` client
  - Test: basic prompt → stdout, `--search` flag, `--json-mode` flag, error handling, stdin prompt
  - **File:** `tests/test_openai_provider.py` (new)

- [ ] **5b.** Unit tests for `llm_provider.py` openai branch
  - Test `effective_provider_settings("openai")` returns correct defaults
  - Test `provider_command("openai", "test prompt")` builds correct argv
  - Test `provider_binary("openai")` returns valid path
  - Test `default_provider_chain()` works with `ASSET_LLM_PROVIDER=openai`
  - **File:** `tests/test_llm_provider.py`

- [ ] **5c.** Integration smoke test
  - Set `OPENAI_API_KEY` from `.env.local`, run a real API call
  - Verify `extract_json_object()` parses the response
  - Verify web search mode returns results
  - **File:** manual or `tests/test_openai_integration.py`

---

## Technical Considerations

- **Responses API over Chat Completions**: The Responses API is OpenAI's recommended API (2025+). It has built-in `web_search` tool, server-side state, and better caching. The `openai` SDK v2.x supports both.
- **Default model**: `gpt-5.4-mini` — $0.75/$4.50 per MTok, 400K context, best cost/performance. User can override with `OPENAI_MODEL`.
- **Web search pricing**: ~$0.01/call + token costs. Available on standard API keys via the Responses API.
- **Prompt size**: Most prompts fit in a CLI argument. For very large prompts (research mode with full file contents), the script reads from stdin via `-` argument.
- **No streaming needed**: The pipeline captures complete output — non-streaming is simpler and sufficient.
- **JSON mode**: The Responses API supports `text={"format": {"type": "json_object"}}` for reliable JSON output. The `--json-mode` flag enables this for scoring and answer generation calls.
- **SDK retries**: The `openai` SDK has built-in retry with exponential backoff (default 2, we set 3). No need to reimplement retry logic.

## Acceptance Criteria

- [ ] `ASSET_LLM_PROVIDER=openai` + `OPENAI_API_KEY=sk-...` in `.env.local` runs scoring, answer generation, research, and drafting via OpenAI API
- [ ] Auto-fix automatically falls back to Claude CLI when provider is `openai`
- [ ] Interview prep automatically falls back to Claude CLI (already done)
- [ ] Web search works in research mode via Responses API `web_search` tool
- [ ] `extract_json_object()` successfully parses OpenAI API responses
- [ ] All existing tests pass with `openai` as configured provider (mocked API calls)
- [ ] Real API call produces usable output for scoring and answer generation
- [ ] Documentation covers setup: API key, model selection, limitations

## Dependencies & Risks

- **`openai` Python SDK**: New dependency. v2.29.0, requires Python 3.9+ (we have 3.14).
- **API key cost**: Each API call costs money (unlike Claude CLI which uses subscription). `gpt-5.4-mini` is cheap but research mode with web search adds ~$0.01/call.
- **Rate limits**: OpenAI API has per-organization rate limits. SDK handles retries automatically.
- **Model deprecation**: OpenAI deprecates models frequently. The default should be updatable via `OPENAI_MODEL` env var.

## Sources & References

### Origin
- **Origin document:** [docs/brainstorms/2026-03-23-openai-api-provider-requirements.md](docs/brainstorms/2026-03-23-openai-api-provider-requirements.md) — Key decisions: direct API over agent framework, auto-fix/interview prep stay Claude-only.

### Internal References
- Provider abstraction: `scripts/llm_provider.py`
- Codex wrapper precedent: `scripts/codex_exec_wrapper.py` (subprocess shim pattern)
- Answer generation: `scripts/application_submit_common.py:1072` (`_run_answer_generation_provider`)
- JSON extraction: `scripts/application_submit_common.py:854` (`extract_json_object`)

### External References
- [OpenAI Responses API](https://platform.openai.com/docs/guides/responses-vs-chat-completions)
- [OpenAI Web Search Tool](https://platform.openai.com/docs/guides/tools-web-search)
- [OpenAI Python SDK](https://github.com/openai/openai-python)
- [OpenAI Models & Pricing](https://developers.openai.com/api/docs/pricing)
