---
date: 2026-03-23
topic: gpt-provider-support
---

# GPT Model Support via Codex CLI

## Problem Frame

The system currently runs all LLM tasks exclusively through Claude Sonnet 4.6 via the `claude` CLI. The user wants the ability to swap in GPT models (via the Codex CLI) as a plug-and-play alternative — same behavior, different provider. The `llm_provider.py` abstraction already supports `codex` as a provider, but gaps exist: at least one callsite hardcodes the `claude` binary, and the codex path has never been tested end-to-end.

## Requirements

- R1. Switching `ASSET_LLM_PROVIDER=codex` in `.env.local` routes all LLM calls through the Codex CLI with no code changes required.
- R2. Every callsite that invokes an LLM must go through the `llm_provider.py` abstraction — no hardcoded `claude` binary references.
- R3. The Codex CLI path produces functionally equivalent output for all modes: research, draft, fix, submit, and job scoring.
- R4. The provider fallback chain works with codex (e.g., `ASSET_LLM_PROVIDER_CHAIN=codex,claude` retries with Claude if Codex fails).
- R5. Setup instructions document how to install, authenticate, and configure the Codex CLI as the active provider.

## Success Criteria

- Changing one env var switches the entire system from Claude to GPT with no other changes.
- All existing tests pass with `codex` as the configured provider (mocked subprocess calls).
- A real end-to-end run with Codex CLI produces usable resume, cover letter, and form answers.

## Scope Boundaries

- No direct OpenAI API/SDK integration — all GPT access goes through the Codex CLI binary.
- No per-task provider selection — this is a global toggle only.
- No new LLM providers beyond what `llm_provider.py` already defines.
- No changes to prompt content or agent instructions — same prompts, different provider.

## Key Decisions

- **Codex CLI over direct API**: The system invokes LLM agents (not raw completions) that need file access, web search, and tool use. The Codex CLI provides these agent capabilities natively, matching Claude CLI's model. Direct API would require building an agent framework.
- **Global toggle over per-task config**: Simpler configuration surface. The fallback chain already provides resilience if the primary provider fails.

## Known Gaps (from codebase scan)

- `job_discovery.py:_call_llm_score()` hardcodes `["claude", "--print", "-p", prompt]`
- Possibly other hardcoded `claude` references in scripts not yet audited
- Codex CLI tool names/allowed-tools may differ from Claude CLI equivalents
- Output format differences between providers may need normalization

## Outstanding Questions

### Deferred to Planning
- [Affects R2][Needs research] Full audit of all hardcoded `claude` binary references across the codebase
- [Affects R3][Needs research] Do Codex CLI tool names (Read, Write, WebSearch, etc.) match Claude CLI, or do they need mapping?
- [Affects R3][Technical] Does the Codex CLI support all modes the system uses (`--print`, `--allowedTools`, `--permission-mode`, etc.)?
- [Affects R5][Technical] What is the exact Codex CLI install and auth flow to document?

## Next Steps

-> `/ce:plan` for structured implementation planning
