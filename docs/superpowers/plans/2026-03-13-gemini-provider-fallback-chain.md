# Gemini Provider + Configurable Fallback Chain Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Gemini as a first-class LLM provider and replace the hardcoded Claude→Codex fallback with a configurable provider chain (default: gemini,claude,codex).

**Architecture:** Extend `llm_provider.py` with a `gemini` provider branch (settings, command builder, shell exports). Refactor `llm_common.sh`'s `job_assets_run_prompt_with_fallback()` to iterate through `ASSET_LLM_PROVIDER_CHAIN` instead of hardcoding Claude→Codex. Update all provider validation call sites across Python and bash entry points.

**Tech Stack:** Python 3.12+, bash, Gemini CLI (v0.33.1, OAuth auth), unittest

**Spec:** `docs/superpowers/specs/2026-03-13-gemini-provider-fallback-chain-design.md`

---

## Chunk 1: Gemini Provider in `llm_provider.py`

### Task 1: Add Gemini provider settings and constants

**Files:**
- Modify: `scripts/llm_provider.py:21-37` (constants section)
- Modify: `scripts/llm_provider.py:94-135` (`effective_provider_settings`)
- Test: `tests/test_llm_provider.py`

- [ ] **Step 1: Write the failing test for Gemini default settings**

Add to `tests/test_llm_provider.py` inside `LlmProviderTests`:

```python
def test_effective_provider_settings_gemini_defaults(self):
    provider = load_module("llm_provider", "scripts/llm_provider.py")

    gemini = provider.effective_provider_settings("gemini", environ={})

    self.assertEqual(gemini["model"], "gemini-3.1-pro-preview")
    self.assertEqual(gemini["effort"], "")
    self.assertEqual(gemini["profile"], "")
    self.assertEqual(gemini["reasoning_effort"], "")
    self.assertEqual(gemini["extra_args"], "")
    self.assertEqual(gemini["timeout_seconds"], "600")
    self.assertEqual(gemini["asset_timeout_seconds"], "1200")
    self.assertEqual(gemini["asset_primary_timeout_seconds"], "")
    self.assertEqual(gemini["asset_fallback_provider"], "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/jerrison/00-projects/00-career/01-prep/11-job-application-material-creation && uv run python -m pytest tests/test_llm_provider.py::LlmProviderTests::test_effective_provider_settings_gemini_defaults -v`
Expected: FAIL with `ValueError: Unsupported provider: gemini`

- [ ] **Step 3: Add Gemini constant and settings branch**

In `scripts/llm_provider.py`, add constant after line 31 (after `DEFAULT_CODEX_SANDBOX_MODE`):

```python
DEFAULT_GEMINI_MODEL = "gemini-3.1-pro-preview"
```

In `effective_provider_settings()`, add a new branch before the final `raise ValueError` (after the codex block ending at line 133):

```python
    if provider == "gemini":
        return {
            "model": _clean(env.get("GEMINI_MODEL")) or DEFAULT_GEMINI_MODEL,
            "effort": "",
            "permission_mode": "",
            "setting_sources": "",
            "no_session_persistence": "",
            "disable_slash_commands": "",
            "strict_mcp_config": "",
            "mcp_config": "",
            "asset_primary_timeout_seconds": "",
            "asset_fallback_provider": "",
            "profile": "",
            "reasoning_effort": "",
            "approval_policy": "",
            "sandbox_mode": "",
            "extra_args": _clean(env.get("GEMINI_EXTRA_ARGS")) or "",
            "timeout_seconds": str(provider_timeout_seconds(environ=env)),
            "asset_timeout_seconds": str(asset_provider_timeout_seconds(environ=env)),
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/jerrison/00-projects/00-career/01-prep/11-job-application-material-creation && uv run python -m pytest tests/test_llm_provider.py::LlmProviderTests::test_effective_provider_settings_gemini_defaults -v`
Expected: PASS

- [ ] **Step 5: Write test for Gemini env var overrides**

Add to `tests/test_llm_provider.py`:

```python
def test_effective_provider_settings_gemini_respects_overrides(self):
    provider = load_module("llm_provider", "scripts/llm_provider.py")
    environ = {
        "GEMINI_MODEL": "gemini-3-flash-preview",
        "GEMINI_EXTRA_ARGS": "--sandbox",
        "JOB_ASSETS_PROVIDER_TIMEOUT_SECONDS": "42",
    }

    gemini = provider.effective_provider_settings("gemini", environ=environ)

    self.assertEqual(gemini["model"], "gemini-3-flash-preview")
    self.assertEqual(gemini["extra_args"], "--sandbox")
    self.assertEqual(gemini["timeout_seconds"], "42")
    self.assertEqual(gemini["asset_timeout_seconds"], "42")
```

- [ ] **Step 6: Run test to verify it passes (implementation already covers this)**

Run: `cd /Users/jerrison/00-projects/00-career/01-prep/11-job-application-material-creation && uv run python -m pytest tests/test_llm_provider.py::LlmProviderTests::test_effective_provider_settings_gemini_respects_overrides -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add scripts/llm_provider.py tests/test_llm_provider.py
git commit -m "feat: add Gemini provider settings to llm_provider.py"
```

### Task 2: Add Gemini command builder

**Files:**
- Modify: `scripts/llm_provider.py:212-293` (`provider_command`)
- Test: `tests/test_llm_provider.py`

- [ ] **Step 1: Write the failing test for Gemini command building**

Add to `tests/test_llm_provider.py`:

```python
def test_provider_command_builds_gemini_with_yolo_and_prompt(self):
    provider = load_module("llm_provider", "scripts/llm_provider.py")

    command = provider.provider_command(
        "gemini",
        "Draft the tailored resume.",
        environ={},
    )

    self.assertEqual(
        command,
        [
            "gemini",
            "--yolo",
            "--model",
            "gemini-3.1-pro-preview",
            "-p",
            "Draft the tailored resume.",
        ],
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/jerrison/00-projects/00-career/01-prep/11-job-application-material-creation && uv run python -m pytest tests/test_llm_provider.py::LlmProviderTests::test_provider_command_builds_gemini_with_yolo_and_prompt -v`
Expected: FAIL with `ValueError: Unsupported provider: gemini`

- [ ] **Step 3: Add Gemini branch to `provider_command()`**

In `scripts/llm_provider.py`, add before the final `raise ValueError` in `provider_command()` (after the codex block ending at line 291):

```python
    if provider == "gemini":
        cmd = [
            "gemini",
            "--yolo",
            "--model",
            settings["model"],
        ]
        cmd.extend(_split_extra_args(settings.get("extra_args")))
        cmd.append("-p")
        cmd.append(prompt)
        return cmd
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/jerrison/00-projects/00-career/01-prep/11-job-application-material-creation && uv run python -m pytest tests/test_llm_provider.py::LlmProviderTests::test_provider_command_builds_gemini_with_yolo_and_prompt -v`
Expected: PASS

- [ ] **Step 5: Write test for Gemini ignoring claude_allowed_tools and search_enabled**

Add to `tests/test_llm_provider.py`:

```python
def test_provider_command_gemini_ignores_claude_specific_flags(self):
    provider = load_module("llm_provider", "scripts/llm_provider.py")

    command = provider.provider_command(
        "gemini",
        "Research the company.",
        search_enabled=True,
        claude_allowed_tools=provider.CLAUDE_RESEARCH_ALLOWED_TOOLS,
        environ={},
    )

    # Gemini should not contain any Claude-specific flags
    self.assertNotIn("--allowedTools", command)
    self.assertNotIn("--search", command)
    self.assertEqual(command[0], "gemini")
    self.assertEqual(command[-2:], ["-p", "Research the company."])
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd /Users/jerrison/00-projects/00-career/01-prep/11-job-application-material-creation && uv run python -m pytest tests/test_llm_provider.py::LlmProviderTests::test_provider_command_gemini_ignores_claude_specific_flags -v`
Expected: PASS

- [ ] **Step 7: Write test for Gemini extra args**

Add to `tests/test_llm_provider.py`:

```python
def test_provider_command_gemini_allows_extra_args(self):
    provider = load_module("llm_provider", "scripts/llm_provider.py")

    command = provider.provider_command(
        "gemini",
        "Draft the resume.",
        environ={"GEMINI_EXTRA_ARGS": "--sandbox --output-format json"},
    )

    self.assertEqual(
        command,
        [
            "gemini",
            "--yolo",
            "--model",
            "gemini-3.1-pro-preview",
            "--sandbox",
            "--output-format",
            "json",
            "-p",
            "Draft the resume.",
        ],
    )
```

- [ ] **Step 8: Run test to verify it passes**

Run: `cd /Users/jerrison/00-projects/00-career/01-prep/11-job-application-material-creation && uv run python -m pytest tests/test_llm_provider.py::LlmProviderTests::test_provider_command_gemini_allows_extra_args -v`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add scripts/llm_provider.py tests/test_llm_provider.py
git commit -m "feat: add Gemini command builder to llm_provider.py"
```

### Task 3: Add Gemini shell exports and update argparse

**Files:**
- Modify: `scripts/llm_provider.py:339-341` (argparse choices)
- Test: `tests/test_llm_provider.py`

- [ ] **Step 1: Write the failing test for Gemini shell exports**

Add to `tests/test_llm_provider.py`:

```python
def test_shell_exports_gemini_includes_model_and_empty_irrelevant_keys(self):
    provider = load_module("llm_provider", "scripts/llm_provider.py")

    exports = provider.shell_exports("gemini", environ={})

    self.assertIn("export JOB_ASSETS_PROVIDER_MODEL=gemini-3.1-pro-preview", exports)
    self.assertIn("export JOB_ASSETS_PROVIDER_EFFORT=''", exports)
    self.assertIn("export JOB_ASSETS_PROVIDER_PROFILE=''", exports)
    self.assertIn("export JOB_ASSETS_PROVIDER_REASONING_EFFORT=''", exports)
    self.assertIn("export JOB_ASSETS_PROVIDER_EXTRA_ARGS=''", exports)
    self.assertIn("export JOB_ASSETS_PROVIDER_TIMEOUT_SECONDS=600", exports)
    self.assertIn("export JOB_ASSETS_ASSET_PROVIDER_TIMEOUT_SECONDS=1200", exports)
```

- [ ] **Step 2: Run test to verify it passes (settings already return all needed keys)**

Run: `cd /Users/jerrison/00-projects/00-career/01-prep/11-job-application-material-creation && uv run python -m pytest tests/test_llm_provider.py::LlmProviderTests::test_shell_exports_gemini_includes_model_and_empty_irrelevant_keys -v`
Expected: PASS (effective_provider_settings already returns all keys shell_exports needs)

- [ ] **Step 3: Write the failing test for CLI accepting gemini**

Add to `tests/test_llm_provider.py`:

```python
def test_command_cli_accepts_gemini_provider(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
        prompt_file = Path(tmp_dir) / "prompt.txt"
        prompt_file.write_text("Research the company.", encoding="utf-8")

        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "llm_provider.py"),
                "gemini",
                "--command",
                "--mode",
                "research",
                "--prompt-file",
                str(prompt_file),
                "--project-root",
                str(PROJECT_ROOT),
            ],
            capture_output=True,
        )

    self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
    argv = [part.decode("utf-8") for part in completed.stdout.split(b"\0") if part]
    self.assertEqual(argv[0], "gemini")
    self.assertIn("--yolo", argv)
    self.assertIn("gemini-3.1-pro-preview", argv)
```

- [ ] **Step 4: Run test to verify it fails**

Run: `cd /Users/jerrison/00-projects/00-career/01-prep/11-job-application-material-creation && uv run python -m pytest tests/test_llm_provider.py::LlmProviderTests::test_command_cli_accepts_gemini_provider -v`
Expected: FAIL with exit code 2 (argparse rejects "gemini")

- [ ] **Step 5: Update argparse choices**

In `scripts/llm_provider.py`, change line 341:

```python
# Before:
    parser.add_argument("provider", choices=("claude", "codex"))
# After:
    parser.add_argument("provider", choices=("claude", "codex", "gemini"))
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd /Users/jerrison/00-projects/00-career/01-prep/11-job-application-material-creation && uv run python -m pytest tests/test_llm_provider.py::LlmProviderTests::test_command_cli_accepts_gemini_provider -v`
Expected: PASS

- [ ] **Step 7: Run all llm_provider tests to verify no regressions**

Run: `cd /Users/jerrison/00-projects/00-career/01-prep/11-job-application-material-creation && uv run python -m pytest tests/test_llm_provider.py -v`
Expected: All tests PASS

- [ ] **Step 8: Commit**

```bash
git add scripts/llm_provider.py tests/test_llm_provider.py
git commit -m "feat: add Gemini shell exports and update argparse choices"
```

---

## Chunk 2: Configurable Fallback Chain in `llm_common.sh`

### Task 4: Add chain resolution and update `require_provider` and default provider

**Files:**
- Modify: `scripts/llm_common.sh:24-63`

- [ ] **Step 1: Update `job_assets_default_provider()` to return `"chain"` by default**

In `scripts/llm_common.sh`, replace `job_assets_default_provider()` (lines 24-26):

```bash
job_assets_default_provider() {
    printf '%s\n' "${ASSET_LLM_PROVIDER:-chain}"
}
```

When `ASSET_LLM_PROVIDER` is unset, this returns `"chain"`, which triggers chain mode in `job_assets_run_prompt_with_fallback()`. When explicitly set (e.g., `ASSET_LLM_PROVIDER=claude`), it returns the single provider for legacy single-provider mode.

- [ ] **Step 2: Add `job_assets_resolve_provider_chain()` and `job_assets_display_provider()`**

Add after `job_assets_default_provider()`:

```bash
job_assets_resolve_provider_chain() {
    printf '%s\n' "${ASSET_LLM_PROVIDER_CHAIN:-gemini,claude,codex}"
}

job_assets_display_provider() {
    local provider="$1"
    if [[ "$provider" == "chain" ]]; then
        printf '%s\n' "chain($(job_assets_resolve_provider_chain))"
    else
        printf '%s\n' "$provider"
    fi
}
```

- [ ] **Step 3: Update `job_assets_require_provider()` to accept gemini and chain**

Replace `job_assets_require_provider()` (lines 48-63):

```bash
job_assets_require_provider() {
    local provider="$1"

    case "$provider" in
        claude|codex|gemini) ;;
        chain)
            # Chain mode — individual providers validated at runtime
            return 0
            ;;
        *)
            echo "ERROR: Unsupported provider '${provider}'. Use 'gemini', 'claude', or 'codex'." >&2
            return 1
            ;;
    esac

    if ! command -v "$provider" >/dev/null 2>&1; then
        echo "ERROR: '${provider}' is not installed or not on PATH." >&2
        return 1
    fi
}
```

- [ ] **Step 4: Verify by sourcing the file**

Run: `cd /Users/jerrison/00-projects/00-career/01-prep/11-job-application-material-creation && bash -c 'source scripts/llm_common.sh && job_assets_require_provider gemini && echo OK'`
Expected: "OK" (assuming gemini is installed) or appropriate error

- [ ] **Step 5: Commit**

```bash
git add scripts/llm_common.sh
git commit -m "feat: add gemini to require_provider and chain resolution in llm_common.sh"
```

### Task 5: Rewrite fallback function to use provider chain

**Files:**
- Modify: `scripts/llm_common.sh:94-351` (fallback_log_file + run_prompt_with_fallback)

- [ ] **Step 1: Replace `job_assets_fallback_log_file()` with `job_assets_chain_log_file()`**

Replace `job_assets_fallback_log_file()` (lines 94-101):

```bash
job_assets_chain_log_file() {
    local log_file="$1"
    local chain_index="$2"
    if [[ "$chain_index" -eq 0 ]]; then
        printf '%s\n' "$log_file"
        return
    fi
    case "$log_file" in
        *_raw.txt) printf '%s\n' "${log_file%_raw.txt}_fallback_${chain_index}_raw.txt" ;;
        *.txt) printf '%s\n' "${log_file%.txt}_fallback_${chain_index}.txt" ;;
        *) printf '%s\n' "${log_file}_fallback_${chain_index}" ;;
    esac
}
```

- [ ] **Step 2: Rewrite `job_assets_run_prompt_with_fallback()` to iterate through chain**

Replace `job_assets_run_prompt_with_fallback()` (lines 290-351):

```bash
job_assets_run_prompt_with_fallback() {
    local provider="$1"
    local prompt_file="$2"
    local mode="$3"
    local log_file="$4"
    shift 4
    local changed_paths=("$@")

    # Single-provider mode: provider is explicit (not "chain")
    if [[ "$provider" != "chain" ]]; then
        _job_assets_run_single_provider_with_legacy_fallback \
            "$provider" "$prompt_file" "$mode" "$log_file" "${changed_paths[@]}"
        return $?
    fi

    # Chain mode: iterate through ASSET_LLM_PROVIDER_CHAIN
    local chain_str
    chain_str="$(job_assets_resolve_provider_chain)"
    IFS=',' read -ra chain <<< "$chain_str"

    local -a pre_shas=()
    local path
    for path in "${changed_paths[@]}"; do
        pre_shas+=("$(job_assets_file_sha256 "$path")")
    done

    local chain_index=0
    local last_status=1
    for current_provider in "${chain[@]}"; do
        current_provider="$(echo "$current_provider" | tr -d '[:space:]')"
        if [[ -z "$current_provider" ]]; then
            continue
        fi
        if ! command -v "$current_provider" >/dev/null 2>&1; then
            echo "WARNING: Provider '${current_provider}' is not installed; skipping." >&2
            chain_index=$((chain_index + 1))
            continue
        fi

        local current_log_file
        current_log_file="$(job_assets_chain_log_file "$log_file" "$chain_index")"

        local provider_timeout="${JOB_ASSETS_PROVIDER_TIMEOUT_SECONDS:-600}"
        echo "INFO: Trying provider '${current_provider}' (chain position ${chain_index})." >&2

        last_status=0
        job_assets_run_prompt "$current_provider" "$prompt_file" "$mode" "$current_log_file" "$provider_timeout" || last_status=$?

        if [[ $last_status -eq 0 ]]; then
            return 0
        fi

        # Check if the provider wrote output files despite non-zero exit
        local wrote_files=false
        local index
        for index in "${!changed_paths[@]}"; do
            if job_assets_file_changed "${changed_paths[$index]}" "${pre_shas[$index]}"; then
                wrote_files=true
                break
            fi
        done
        if $wrote_files; then
            return "$last_status"
        fi

        echo "WARNING: ${current_provider} ${mode} generation failed before writing output files." >&2
        chain_index=$((chain_index + 1))
    done

    return "$last_status"
}
```

- [ ] **Step 3: Add the legacy single-provider fallback as a private function**

Add after `job_assets_run_prompt_with_fallback()`:

```bash
_job_assets_run_single_provider_with_legacy_fallback() {
    local provider="$1"
    local prompt_file="$2"
    local mode="$3"
    local log_file="$4"
    shift 4
    local changed_paths=("$@")
    local -a pre_shas=()
    local path

    job_assets_load_provider_defaults "$provider"

    for path in "${changed_paths[@]}"; do
        pre_shas+=("$(job_assets_file_sha256 "$path")")
    done

    local primary_status=0
    local primary_asset_timeout="${JOB_ASSETS_CLAUDE_PRIMARY_ASSET_TIMEOUT_SECONDS:-600}"
    if [[ "$provider" == "claude" && ( "$mode" == "content" || "$mode" == "research" || "$mode" == "draft" || "$mode" == "fix" ) && "$primary_asset_timeout" =~ ^[0-9]+$ && "$primary_asset_timeout" -gt 0 ]]; then
        job_assets_run_prompt "$provider" "$prompt_file" "$mode" "$log_file" "$primary_asset_timeout" || primary_status=$?
    else
        job_assets_run_prompt "$provider" "$prompt_file" "$mode" "$log_file" || primary_status=$?
    fi
    if [[ $primary_status -eq 0 ]]; then
        return 0
    fi

    local wrote_files=false
    local index
    for index in "${!changed_paths[@]}"; do
        if job_assets_file_changed "${changed_paths[$index]}" "${pre_shas[$index]}"; then
            wrote_files=true
            break
        fi
    done
    if $wrote_files; then
        return "$primary_status"
    fi

    local fallback_provider=""
    if [[ "$provider" == "claude" && ( "$mode" == "content" || "$mode" == "research" || "$mode" == "draft" || "$mode" == "fix" ) ]]; then
        fallback_provider="${JOB_ASSETS_CLAUDE_ASSET_FALLBACK_PROVIDER:-codex}"
        case "$fallback_provider" in
            ""|0|false|False|FALSE|none|None|NONE)
                fallback_provider=""
                ;;
        esac
    fi

    if [[ -z "$fallback_provider" || "$fallback_provider" == "$provider" ]]; then
        return "$primary_status"
    fi
    if ! command -v "$fallback_provider" >/dev/null 2>&1; then
        echo "WARNING: ${provider} ${mode} generation failed and fallback provider '${fallback_provider}' is not available." >&2
        return "$primary_status"
    fi

    local fallback_log_file
    fallback_log_file="$(job_assets_chain_log_file "$log_file" 1)"
    echo "WARNING: ${provider} ${mode} generation failed before writing output files; retrying with ${fallback_provider}." >&2
    job_assets_run_prompt "$fallback_provider" "$prompt_file" "$mode" "$fallback_log_file"
}
```

- [ ] **Step 4: Verify bash syntax**

Run: `cd /Users/jerrison/00-projects/00-career/01-prep/11-job-application-material-creation && bash -n scripts/llm_common.sh && echo "Syntax OK"`
Expected: "Syntax OK"

- [ ] **Step 5: Commit**

```bash
git add scripts/llm_common.sh
git commit -m "feat: rewrite fallback function to use configurable provider chain"
```

---

## Chunk 3: Update All Call Sites

### Task 6: Update `apply.sh` and `parallel_apply.sh`

**Note on `scripts/llm_worker.sh`:** No code changes needed. It receives `$provider` as `$2` from `parallel_apply.sh` and passes it through to `job_assets_run_prompt_with_fallback()`. Since `parallel_apply.sh` uses `job_assets_default_provider()` which now returns `"chain"`, `llm_worker.sh` automatically gets chain mode. The `job_assets_run_prompt_with_fallback()` chain loop calls `job_assets_run_prompt()` with specific provider names (not `"chain"`), so `job_assets_load_provider_defaults()` gets the correct provider.

**Files:**
- Modify: `apply.sh:37,40`
- Modify: `parallel_apply.sh:59`

- [ ] **Step 1: Update `apply.sh` help text**

In `apply.sh`, change line 37 (usage line):

```bash
# Before:
    echo "Usage: $0 [--provider claude|codex] [--skip-sync] <url_or_file> [company] [role-slug]"
# After:
    echo "Usage: $0 [--provider gemini|claude|codex] [--skip-sync] <url_or_file> [company] [role-slug]"
```

And change line 40 (provider description):

```bash
# Before:
    echo "  --provider   LLM CLI to use for asset generation (default: ${ASSET_LLM_PROVIDER:-claude})"
# After:
    echo "  --provider   LLM CLI to use for asset generation (default: $(job_assets_default_provider))"
```

- [ ] **Step 2: Update `parallel_apply.sh` help text**

In `parallel_apply.sh`, change line 59:

```bash
# Before:
            echo "Usage: $0 [--dry-run] [--build-only] [--provider claude|codex] [--max-parallel N]"
# After:
            echo "Usage: $0 [--dry-run] [--build-only] [--provider gemini|claude|codex] [--max-parallel N]"
```

- [ ] **Step 3: Verify bash syntax on both files**

Run: `cd /Users/jerrison/00-projects/00-career/01-prep/11-job-application-material-creation && bash -n apply.sh && bash -n parallel_apply.sh && echo "Syntax OK"`
Expected: "Syntax OK"

- [ ] **Step 4: Commit**

```bash
git add apply.sh parallel_apply.sh
git commit -m "feat: update help text to include gemini provider option"
```

### Task 7: Update `scripts/job_assets_pipeline.py`

**Files:**
- Modify: `scripts/job_assets_pipeline.py:33-41`

- [ ] **Step 1: Update `default_provider()` and `require_provider()`**

In `scripts/job_assets_pipeline.py`, replace lines 33-41:

```python
def default_provider() -> str:
    return os.environ.get("ASSET_LLM_PROVIDER", "gemini")


def require_provider(provider: str) -> None:
    if provider not in {"gemini", "claude", "codex"}:
        raise ValueError(f"Unsupported provider: {provider}")
    if not shutil.which(provider):
        raise FileNotFoundError(f"'{provider}' is not installed or not on PATH.")
```

- [ ] **Step 2: Commit**

```bash
git add scripts/job_assets_pipeline.py
git commit -m "feat: add gemini to job_assets_pipeline.py provider validation"
```

### Task 8: Update `bin/job-assets`

**Files:**
- Modify: `bin/job-assets:66-67` (default_provider)
- Modify: `bin/job-assets:153-159` (add_provider_option)
- Modify: `bin/job-assets:255-317` (cmd_doctor)

- [ ] **Step 1: Update `default_provider()`**

In `bin/job-assets`, change line 67:

```python
# Before:
    return PROVIDER_ALIASES.get(COMMAND_NAME, os.environ.get("ASSET_LLM_PROVIDER", "claude"))
# After:
    return PROVIDER_ALIASES.get(COMMAND_NAME, os.environ.get("ASSET_LLM_PROVIDER", "gemini"))
```

- [ ] **Step 2: Update `add_provider_option()`**

In `bin/job-assets`, change line 156:

```python
# Before:
        choices=("claude", "codex"),
# After:
        choices=("gemini", "claude", "codex"),
```

- [ ] **Step 3: Add `PROVIDER_ALIASES` entry for gemini**

In `bin/job-assets`, add to `PROVIDER_ALIASES` dict (line 52-55):

```python
PROVIDER_ALIASES = {
    "job-assets-codex": "codex",
    "job-assets-claude": "claude",
    "job-assets-gemini": "gemini",
}
```

- [ ] **Step 4: Update `cmd_doctor()` to include Gemini diagnostics**

In `bin/job-assets`, replace lines 255-317 of `cmd_doctor()`:

```python
def cmd_doctor(args: argparse.Namespace) -> int:
    providers = {}
    for provider in ("gemini", "codex", "claude"):
        providers[provider] = shutil.which(provider)
    claude_settings = effective_provider_settings("claude")
    codex_settings = effective_provider_settings("codex")
    gemini_settings = effective_provider_settings("gemini")

    print(f"command:        {COMMAND_NAME}")
    print(f"repo:           {REPO_ROOT}")
    print(f"default provider: {default_provider()}")
    print(f"provider chain: {os.environ.get('ASSET_LLM_PROVIDER_CHAIN', 'gemini,claude,codex')}")
    print(f"gemini defaults: model={gemini_settings['model']}")
    print(f"claude defaults: model={claude_settings['model']} effort={claude_settings['effort']}")
    print(
        "claude exec mode: "
        f"permission={claude_settings['permission_mode']} "
        f"settings={claude_settings['setting_sources']} "
        f"session_persistence={'off' if claude_settings['no_session_persistence'] not in {'0', 'false', 'False'} else 'on'} "
        f"slash_commands={'off' if claude_settings['disable_slash_commands'] not in {'0', 'false', 'False'} else 'on'} "
        f"strict_mcp={'on' if claude_settings['strict_mcp_config'] not in {'0', 'false', 'False'} else 'off'}"
    )
    print(
        f"codex defaults: model={codex_settings['model']} reasoning={codex_settings['reasoning_effort']}"
    )
    print(
        f"codex exec mode: approval={codex_settings['approval_policy']} sandbox={codex_settings['sandbox_mode']}"
    )
    print("codex exec isolation: minimal CODEX_HOME with auth + sanitized config (user MCP servers disabled)")
    print(f"provider timeout (submit answers, s): {claude_settings['timeout_seconds']}")
    print(f"provider timeout (asset generation, s): {claude_settings['asset_timeout_seconds']}")
    print(f"default max parallel jobs: {default_max_parallel()}")
    primary_timeout = claude_settings["asset_primary_timeout_seconds"]
    fallback_provider = claude_settings["asset_fallback_provider"] or "disabled"
    if primary_timeout in {"", "0"}:
        print("claude primary asset timeout before fallback (s): disabled")
    else:
        print(f"claude primary asset timeout before fallback (s): {primary_timeout}")
    print(f"claude asset fallback provider: {fallback_provider}")
    if codex_settings["profile"]:
        print(f"codex profile override: {codex_settings['profile']}")
        print("codex profile-backed runs defer core execution settings to that profile unless explicit CODEX_* env overrides are set")
    if claude_settings["extra_args"]:
        print(f"claude extra args: {claude_settings['extra_args']}")
    if codex_settings["extra_args"]:
        print(f"codex extra args: {codex_settings['extra_args']}")
    if gemini_settings["extra_args"]:
        print(f"gemini extra args: {gemini_settings['extra_args']}")
    print(f"job-assets on PATH: {shutil.which('job-assets') or 'not found'}")
    print(f"job-assets-codex on PATH: {shutil.which('job-assets-codex') or 'not found'}")
    print(f"job-assets-claude on PATH: {shutil.which('job-assets-claude') or 'not found'}")
    print(f"job-assets-gemini on PATH: {shutil.which('job-assets-gemini') or 'not found'}")
    print(f"man page dir:   {MAN1_ROOT}")
    print(f"application profile: {application_profile_path()} ({'present' if application_profile_path().exists() else 'missing'})")
    env_files = [str(path) for path in PROJECT_ENV_FILES if path.exists()]
    print(f"local env files: {', '.join(env_files) if env_files else 'none'}")
    print(f"submit browser provider: {submit_browser_provider()}")
    print(f"submit browser profile: {submit_browser_profile_dir()}")
    print(f"submit slow mo (headed ms): {submit_slow_mo_ms(False)}")
    print(f"submit type delay (ms): {submit_type_delay_ms()}")
    print(f"submit viewport: {submit_viewport()}")
    print(f"steel base url: {steel_base_url()}")
    print(f"steel local mode: {steel_local_mode()}")
    print(f"steel use proxy: {steel_use_proxy()}")
    print(f"steel solve captcha: {steel_solve_captcha()}")
    print(f"steel api key: {'configured' if steel_api_key() else 'missing'}")
    for provider, location in providers.items():
        print(f"{provider}:         {location or 'not found'}")
    return 0
```

- [ ] **Step 5: Commit**

```bash
git add bin/job-assets
git commit -m "feat: add gemini to bin/job-assets provider options and doctor diagnostics"
```

### Task 9: Update `application_submit_common.py` provider auto-detection

**Files:**
- Modify: `scripts/application_submit_common.py:639-647` (default_answer_provider)
- Modify: `scripts/application_submit_common.py:746-749` (_answer_generation_fallback_provider)

- [ ] **Step 1: Update `default_answer_provider()` to check gemini first**

In `scripts/application_submit_common.py`, replace lines 639-647:

```python
def default_answer_provider() -> str:
    provider = os.getenv("ASSET_LLM_PROVIDER")
    if provider:
        return provider
    for candidate in ("gemini", "claude", "codex"):
        if shutil.which(candidate):
            return candidate
    raise RuntimeError("No answer-generation provider found. Install `gemini`, `claude`, or `codex`, or set ASSET_LLM_PROVIDER.")
```

- [ ] **Step 2: Update `_answer_generation_fallback_provider()` to use chain**

In `scripts/application_submit_common.py`, replace lines 746-749:

```python
def _answer_generation_fallback_provider(provider: str) -> str | None:
    chain_str = os.environ.get("ASSET_LLM_PROVIDER_CHAIN", "gemini,claude,codex")
    chain = [p.strip() for p in chain_str.split(",") if p.strip()]
    try:
        idx = chain.index(provider)
    except ValueError:
        return None
    for candidate in chain[idx + 1:]:
        if shutil.which(candidate):
            return candidate
    return None
```

- [ ] **Step 3: Commit**

```bash
git add scripts/application_submit_common.py
git commit -m "feat: add gemini to answer provider auto-detection and fallback chain"
```

### Task 10: Update `autofill_greenhouse.py` duplicate provider function

**Files:**
- Modify: `scripts/autofill_greenhouse.py:2167-2175`

- [ ] **Step 1: Update `_default_answer_provider()` to include gemini**

In `scripts/autofill_greenhouse.py`, replace lines 2167-2175:

```python
def _default_answer_provider() -> str:
    provider = os.getenv("ASSET_LLM_PROVIDER")
    if provider:
        return provider
    for candidate in ("gemini", "claude", "codex"):
        if shutil.which(candidate):
            return candidate
    raise RuntimeError("No answer-generation provider found. Install `gemini`, `claude`, or `codex`, or set ASSET_LLM_PROVIDER.")
```

- [ ] **Step 2: Commit**

```bash
git add scripts/autofill_greenhouse.py
git commit -m "feat: add gemini to autofill_greenhouse.py provider detection"
```

---

## Chunk 4: Verification

### Task 11: Run full test suite and verify

**Files:** None (verification only)

- [ ] **Step 1: Run all llm_provider tests**

Run: `cd /Users/jerrison/00-projects/00-career/01-prep/11-job-application-material-creation && uv run python -m pytest tests/test_llm_provider.py -v`
Expected: All tests PASS (including new Gemini tests)

- [ ] **Step 2: Run full test suite**

Run: `cd /Users/jerrison/00-projects/00-career/01-prep/11-job-application-material-creation && uv run python -m pytest tests/ -v --timeout=60`
Expected: All tests PASS

- [ ] **Step 3: Verify bash syntax on all modified shell scripts**

Run: `cd /Users/jerrison/00-projects/00-career/01-prep/11-job-application-material-creation && bash -n scripts/llm_common.sh && bash -n scripts/llm_worker.sh && bash -n apply.sh && bash -n parallel_apply.sh && echo "All syntax OK"`
Expected: "All syntax OK"

- [ ] **Step 4: Verify `job-assets doctor` runs cleanly**

Run: `cd /Users/jerrison/00-projects/00-career/01-prep/11-job-application-material-creation && python bin/job-assets doctor`
Expected: Output includes `gemini` provider info, no errors

- [ ] **Step 5: Verify Gemini CLI command construction end-to-end**

Run: `cd /Users/jerrison/00-projects/00-career/01-prep/11-job-application-material-creation && python scripts/llm_provider.py gemini --shell`
Expected: Prints shell exports with `gemini-3.1-pro-preview` model
