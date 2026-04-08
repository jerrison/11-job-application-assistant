---
date: 2026-03-23
topic: openai-api-provider
---

# Direct OpenAI API Provider

## Problem Frame

The Codex CLI path (built earlier today) requires ChatGPT OAuth login — standard OpenAI API keys (`sk-proj-`) fail because Codex CLI's agent tools use a `"custom"` tool type unsupported by the standard Responses API. The user needs a "paste API key, everything works" path. Adding a direct OpenAI API provider via the `openai` Python SDK solves this without needing any CLI tool installed.

## Requirements

- R1. Setting `OPENAI_API_KEY` in `.env.local` and `ASSET_LLM_PROVIDER=openai` enables GPT models for the pipeline with no CLI tool installation required.
- R2. Scoring, answer generation, research, and resume/cover letter drafting all work via the OpenAI API.
- R3. Research mode uses OpenAI's built-in `web_search` tool for company research.
- R4. Auto-fix and interview prep automatically fall back to Claude CLI (these need agentic capabilities the API alone can't provide).
- R5. The provider integrates into the existing `llm_provider.py` abstraction — same `provider_command_for_mode()` / `provider_command()` contract, but returns a callable or uses a different invocation path instead of building a subprocess argv.
- R6. Output from the OpenAI API is compatible with `extract_json_object()` and the existing stdout-capture pattern used by callers.

## Success Criteria

- Setting `OPENAI_API_KEY` + `ASSET_LLM_PROVIDER=openai` in `.env.local` runs the full pipeline (minus auto-fix/interview prep) with no other setup.
- Existing tests pass. New tests verify the OpenAI API path with mocked responses.
- A real end-to-end run produces usable resume, cover letter, and form answers.

## Scope Boundaries

- No agent loop / tool-use loop for the OpenAI API path — callers handle file I/O, the API just returns text/JSON.
- Auto-fix stays Claude CLI only (needs file editing, git, test running).
- Interview prep stays Claude CLI only (needs Playwright MCP tools).
- No changes to prompt content — same prompts, different provider.
- The Codex CLI path (built today) remains as a separate provider option for ChatGPT OAuth users.

## Key Decisions

- **Direct API over agent framework**: The pipeline already handles file I/O in Python/shell — prompts include file contents inline, callers write output to files. The API just needs to return text. No tool-use loop needed.
- **New provider name `openai`**: Distinct from `codex` (CLI-based). `VALID_PROVIDERS` expands to include `openai`.
- **Auto-fix/interview prep fall back to Claude**: These are the only tasks requiring agentic capabilities. They check for Claude CLI and use it regardless of the configured provider.

## Outstanding Questions

### Deferred to Planning
- [Affects R5][Technical] The current `provider_command()` returns a subprocess argv list. The OpenAI API path needs a different invocation mechanism (Python function call, not subprocess). How should this be integrated — new function `provider_call()`, or modify callers to check provider type?
- [Affects R3][Needs research] Does the OpenAI Responses API `web_search` tool work with standard API keys, or is it restricted like Codex's custom tools?
- [Affects R6][Technical] The existing callers use `subprocess.run(cmd, capture_output=True)` and read `result.stdout`. The API path returns a string directly. How to bridge this — wrapper that mimics subprocess result, or refactor callers?
- [Affects R1][Needs research] Which OpenAI models are available via API key? (gpt-4o, gpt-4.1, o3, etc.) What should the default be?

## Next Steps

→ `/ce:plan` for structured implementation planning
