---
title: "fix: Shell validation rejects openai provider — llm_common.sh gates missing openai"
type: fix
status: completed
date: 2026-03-23
---

# fix: Shell validation rejects openai provider

The OpenAI provider was added to `llm_provider.py` (Python) but not to the shell-level validation in `llm_common.sh`. When the pipeline runs, `job_assets_require_provider()` rejects `openai` before the Python abstraction ever gets called.

Error: `"ERROR: Unsupported provider 'openai'. Use 'gemini', 'claude', or 'codex'."`

## Changes

### 1. `scripts/llm_common.sh:65` — Add `openai` to case statement

```diff
-        claude|codex|gemini) ;;
+        claude|codex|gemini|openai) ;;
```

### 2. `scripts/llm_common.sh:76` — Skip `command -v` for openai

The `openai` provider has no binary — it uses `scripts/openai_provider.py` invoked through `llm_provider.py --command`. The `command -v` check must be skipped for `openai`.

```diff
+    # openai uses a Python subprocess shim, not a binary
+    [[ "$provider" = "openai" ]] && return 0
+
     if ! command -v "$provider" >/dev/null 2>&1; then
```

### 3. `scripts/llm_common.sh:424` — Skip `command -v` in chain mode for openai

Same issue in `job_assets_run_prompt_with_fallback()` chain iteration.

```diff
-        if ! command -v "$current_provider" >/dev/null 2>&1; then
+        if [[ "$current_provider" != "openai" ]] && ! command -v "$current_provider" >/dev/null 2>&1; then
```

### 4. `scripts/llm_common.sh:515` — Skip `command -v` in legacy fallback for openai

Same pattern in `_job_assets_run_single_provider_with_legacy_fallback()`.

```diff
-    if ! command -v "$fallback_provider" >/dev/null 2>&1; then
+    if [[ "$fallback_provider" != "openai" ]] && ! command -v "$fallback_provider" >/dev/null 2>&1; then
```

### 5. Error message — update provider list

```diff
-            echo "ERROR: Unsupported provider '${provider}'. Use 'gemini', 'claude', or 'codex'." >&2
+            echo "ERROR: Unsupported provider '${provider}'. Use 'gemini', 'claude', 'codex', or 'openai'." >&2
```

## Acceptance Criteria

- [ ] `ASSET_LLM_PROVIDER=openai` passes shell validation in `job_assets_require_provider()`
- [ ] Chain mode with `openai` doesn't skip it as "not installed"
- [ ] Pipeline successfully invokes `openai_provider.py` for asset generation
- [ ] Error message lists all 4 valid providers
- [ ] Existing providers (claude, codex, gemini) still validate correctly

## Sources

- Shell validation: `scripts/llm_common.sh:61-80`
- Chain mode binary check: `scripts/llm_common.sh:424`
- Python provider command: `scripts/llm_provider.py` (`provider_command("openai", ...)`)
- OpenAI shim: `scripts/openai_provider.py`
