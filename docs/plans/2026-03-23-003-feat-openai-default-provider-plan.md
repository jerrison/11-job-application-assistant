---
title: "feat: Switch default LLM provider to OpenAI"
type: feat
status: completed
date: 2026-03-23
origin: docs/plans/2026-03-23-002-feat-openai-api-direct-provider-plan.md
---

# feat: Switch default LLM provider to OpenAI

## Overview

Flip the active LLM provider from Claude to OpenAI in `.env.local`. The OpenAI API provider was already implemented (see origin plan). This is a config-only change — no code modifications.

## Problem Statement / Motivation

User wants OpenAI with their API key as the default provider. The `openai` provider is already built and wired into `llm_provider.py`. Currently `.env.local` has `ASSET_LLM_PROVIDER=claude` active and the OpenAI lines commented out.

## Proposed Solution

Edit `.env.local` to:
1. Comment out `ASSET_LLM_PROVIDER=claude` and `ASSET_LLM_PROVIDER_CHAIN=claude`
2. Uncomment `ASSET_LLM_PROVIDER=openai`

The `OPENAI_API_KEY` is already set (line 45). The code-level fallback in `llm_provider.py:60` remains `"claude"` — only the `.env.local` config changes.

## Changes

### `.env.local` (lines 37-47)

```diff
 # Option 1: Claude (default — subscription-based, no per-call cost)
-ASSET_LLM_PROVIDER=claude
-ASSET_LLM_PROVIDER_CHAIN=claude
+# ASSET_LLM_PROVIDER=claude
+# ASSET_LLM_PROVIDER_CHAIN=claude
 JOB_ASSETS_CLAUDE_ASSET_FALLBACK_PROVIDER=none

 # Option 2: OpenAI API (paste API key, everything works)
-# ASSET_LLM_PROVIDER=openai
+ASSET_LLM_PROVIDER=openai
 OPENAI_API_KEY=sk-proj-...
```

## Acceptance Criteria

- [ ] `ASSET_LLM_PROVIDER=openai` is the active (uncommented) provider in `.env.local`
- [ ] Claude provider lines are commented out
- [ ] Pipeline runs use OpenAI API (verify with a test job)

## Sources

- **Origin plan:** [docs/plans/2026-03-23-002-feat-openai-api-direct-provider-plan.md](docs/plans/2026-03-23-002-feat-openai-api-direct-provider-plan.md) — OpenAI API provider implementation (completed)
- Provider config: `.env.local:37-47`
- Code fallback: `scripts/llm_provider.py:60`
