# LLM Provider Setup

The pipeline supports multiple LLM providers via CLI tools. Switch providers by setting one environment variable — no code changes needed.

## Supported Providers

| Provider | CLI Binary | Default Model | Install |
|----------|-----------|---------------|---------|
| `claude` (default) | `claude` | `claude-sonnet-4-6` | [claude.ai/download](https://claude.ai/download) |
| `codex` | `codex` | `gpt-5.4` | `brew install codex` or `npm install -g @openai/codex` |
| `openai` | `python openai_provider.py` | `gpt-5.4` | `uv add openai` + API key |
| `gemini` | `gemini` | `gemini-3-flash-preview` | [ai.google.dev/gemini-api/docs/quickstart](https://ai.google.dev/gemini-api/docs/quickstart) |
| `gemini-flash` | `gemini` | `gemini-3-flash-preview` | Same as gemini |

## Quick Start: Switch to Codex (GPT)

1. **Install:** `brew install codex`
2. **Authenticate** (pick one):
   - **OAuth:** `codex login` (opens browser)
   - **API key:** Add `CODEX_API_KEY=sk-proj-...` to `.env.local`
3. **Configure `.env.local`:**
   ```bash
   ASSET_LLM_PROVIDER=codex
   ```
4. Run the pipeline as normal — all LLM calls now use GPT via Codex CLI.

## Quick Start: OpenAI API

1. **Get an API key** from [platform.openai.com](https://platform.openai.com)
2. **Configure `.env.local`:**
   ```bash
   ASSET_LLM_PROVIDER=openai
   OPENAI_API_KEY=sk-proj-...
   ```
3. Run the pipeline as normal — all LLM calls now use OpenAI's Responses API directly.

### OpenAI Key Pool

If you want to spread OpenAI traffic across multiple keys, put them in `.env.local` as a comma-separated pool:

```bash
ASSET_LLM_PROVIDER=openai
OPENAI_API_KEYS=sk-proj-key-1,sk-proj-key-2,sk-proj-key-3,sk-proj-key-4,sk-proj-key-5
```

When `OPENAI_API_KEYS` is set, it takes precedence over `OPENAI_API_KEY`. The provider shim selects one key from the pool for each OpenAI request so traffic is spread across the configured keys.

## Configuration

All provider settings are in `.env.local`:

```bash
# Primary provider (required)
ASSET_LLM_PROVIDER=openai

# Automated fallback chain — only OpenAI and Gemini are used
ASSET_LLM_PROVIDER_CHAIN=openai,gemini

# Per-provider concurrency limit
LLM_PROVIDER_CONCURRENCY=15
```

### Codex-Specific Env Vars

| Variable | Default | Description |
|----------|---------|-------------|
| `CODEX_MODEL` | `gpt-5.4` | Model name |
| `CODEX_REASONING_EFFORT` | `xhigh` | `minimal`, `low`, `medium`, `high`, `xhigh` |
| `CODEX_SANDBOX_MODE` | `danger-full-access` | `read-only`, `workspace-write`, `danger-full-access` |
| `CODEX_PROFILE` | (none) | Named profile from `~/.codex/config.toml` |
| `CODEX_EXTRA_ARGS` | (none) | Additional CLI flags |
| `CODEX_API_KEY` | (none) | OpenAI API key (alternative to `codex login` OAuth) |
| `JOB_ASSETS_CODEX_CONFIG_PATH` | `~/.codex` | Override Codex home directory |

### OpenAI-Specific Env Vars

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | (required) | Your OpenAI API key |
| `OPENAI_API_KEYS` | (none) | Optional comma-separated OpenAI API key pool. When set, it overrides `OPENAI_API_KEY` and spreads requests across the pool. |
| `OPENAI_MODEL` | `gpt-5.4` | Model name |
| `OPENAI_EXTRA_ARGS` | (none) | Additional CLI flags for openai_provider.py |

### Claude-Specific Env Vars

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | Model name |
| `CLAUDE_EFFORT` | `max` | Reasoning effort |
| `CLAUDE_PERMISSION_MODE` | `auto` | Permission mode |
| `CLAUDE_EXTRA_ARGS` | (none) | Additional CLI flags |

## Known Limitations

- **Interview prep uses the configured provider.** `generate_interview_prep.py` now routes through the shared provider layer. OpenAI uses web search plus file tools; Claude keeps its richer browser-enabled tool allowlist through the same provider command path.
- **Auto-fix uses the configured provider.** The orchestrator still owns branch creation, pytest, merge, and cleanup. When the active provider is `openai`, the provider call uses fix-mode settings so file tools stay enabled before control returns to the orchestrator.
- **Submit-mode linked resources are fetched by the repo, not the provider.** `prompt_mode_settings("submit")` still keeps provider-side search and file tools disabled. When a required application question includes a directly linked public resource (for example db-fiddle, JSON/CSV, HTML tables, or PDFs), the runtime fetches that resource in repo-controlled Python first, saves the proof under `submit/linked_resource_context.json` and `submit/linked_resource_evidence/`, and then sends the extracted context to the provider.
- **OpenAI API costs money per call.** Unlike Claude CLI (which uses a subscription) or Codex CLI, every OpenAI API call is billed per-token. Monitor usage at [platform.openai.com/usage](https://platform.openai.com/usage).
- **No per-mode tool restriction for Codex.** Claude uses `--allowedTools` to restrict tools per mode (research/draft/fix/submit). Codex has no equivalent — it always has full tool access. The sandbox mode (`read-only`/`workspace-write`/`danger-full-access`) is a coarser substitute.
- **Codex output format.** Codex `exec` sends progress to stderr, final response to stdout as plain text. The `extract_json_object()` parser handles this, but if JSON extraction fails, try adding `--output-schema` support.

## How It Works

All LLM invocations go through `scripts/llm_provider.py`:
- `provider_command(provider, prompt)` builds the CLI argv for any provider
- `provider_command_for_mode(provider, prompt, mode=...)` adds mode-specific settings (tool restrictions, web search)
- `default_provider_chain()` resolves the fallback chain from env vars
- `automation_provider_chain()` resolves the OpenAI/Gemini-only automation chain used by drafting, worker fallback, and submit-time answer generation
- `provider_binary(provider)` maps provider names to binary names (e.g., `gemini-flash` → `gemini`)

The shell layer (`scripts/llm_common.sh`) delegates command building to `llm_provider.py --command` and reads the resolved automation chain from `llm_provider.py --automation-chain`.
