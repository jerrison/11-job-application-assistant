# OpenAI Provider Parity and Gemini Flash Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route auto-fix and interview prep through OpenAI when `ASSET_LLM_PROVIDER=openai`, and make `gemini` default to `gemini-3.0-flash` instead of Pro.

**Architecture:** Keep provider selection centralized in `scripts/llm_provider.py`, keep repo-controlled orchestration for git branches/tests/document rendering, and remove the remaining Claude-only exceptions from auto-fix and interview prep. Implement each behavior change test-first, then align current docs and config comments with the new runtime behavior.

**Tech Stack:** Python, pytest, unittest mocks, OpenAI provider shim, existing provider command abstraction, markdown docs

---

## File Map

| File | Responsibility |
| --- | --- |
| `scripts/llm_provider.py` | Canonical provider defaults and command construction |
| `tests/test_llm_provider.py` | Unit coverage for provider defaults and generated argv |
| `scripts/pipeline_orchestrator.py` | Auto-fix orchestration, provider selection, fix prompt wording |
| `tests/test_pipeline_orchestrator.py` | Regression coverage for auto-fix provider behavior |
| `scripts/generate_interview_prep.py` | Interview prep provider subprocess, output persistence, progress messages |
| `tests/test_interview_prep.py` | Regression coverage for interview prep provider selection and error handling |
| `docs/provider-setup.md` | Current provider setup and limitations |
| `agent_preferences.md` | Current repo-wide provider guidance |
| `.env.local` | Local config comments shown to the user |
| `docs/worker-pipeline-patterns.md` | Current worker/pipeline operational behavior |
| `docs/board-architecture.md` | Current provider and worker architecture reference |
| `docs/cli-reference.md` | Current CLI-facing provider guidance |

Historical brainstorm/spec/plan documents that describe older decisions should stay unchanged. Only current operational docs should be updated.

---

### Task 1: Make `gemini` Default to Flash

**Files:**
- Modify: `tests/test_llm_provider.py`
- Modify: `scripts/llm_provider.py`
- Test: `tests/test_llm_provider.py`

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_llm_provider.py` near the existing Gemini coverage:

```python
    def test_effective_provider_settings_gemini_defaults_to_flash(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        gemini = provider.effective_provider_settings("gemini", environ={})

        self.assertEqual(gemini["model"], "gemini-3.0-flash")
        self.assertEqual(gemini["timeout_seconds"], "600")
        self.assertEqual(gemini["asset_timeout_seconds"], "1200")

    def test_provider_command_gemini_uses_flash_model_by_default(self):
        provider = load_module("llm_provider", "scripts/llm_provider.py")

        command = provider.provider_command(
            "gemini",
            "Draft the tailored resume.",
            environ={},
        )

        self.assertEqual(command[0], "gemini")
        self.assertIn("--model", command)
        self.assertIn("gemini-3.0-flash", command)
        self.assertEqual(command[-2:], ["-p", "Draft the tailored resume."])
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
uv run python -m pytest tests/test_llm_provider.py -k "gemini and flash" -v
```

Expected:

```text
FAIL ... expected 'gemini-3.0-flash'
got 'gemini-3.1-pro-preview'
```

- [ ] **Step 3: Write the minimal implementation**

Update `scripts/llm_provider.py`:

```python
DEFAULT_GEMINI_MODEL = "gemini-3.0-flash"
DEFAULT_GEMINI_FLASH_MODEL = "gemini-3.0-flash"
```

Do not rename providers or remove `gemini-flash`.

- [ ] **Step 4: Run the test to verify it passes**

Run:

```bash
uv run python -m pytest tests/test_llm_provider.py -k "gemini and flash" -v
```

Expected:

```text
PASSED tests/test_llm_provider.py::...
```

- [ ] **Step 5: Commit**

```bash
git add tests/test_llm_provider.py scripts/llm_provider.py
git commit -m "fix(provider): default gemini to flash"
```

---

### Task 2: Route Auto-Fix Through OpenAI Without Claude Fallback

**Files:**
- Modify: `tests/test_pipeline_orchestrator.py`
- Modify: `scripts/pipeline_orchestrator.py`
- Test: `tests/test_pipeline_orchestrator.py`

- [ ] **Step 1: Write the failing tests**

Update the import list in `tests/test_pipeline_orchestrator.py` to include `auto_fix` and `_build_fix_prompt`, then add these tests:

```python
def test_auto_fix_uses_openai_with_file_tools_without_claude_fallback(tmp_path):
    error_context = {
        "exit_code": 1,
        "url": "https://boards.greenhouse.io/example/jobs/1",
        "output_dir": str(tmp_path),
    }

    with patch.dict("os.environ", {"ASSET_LLM_PROVIDER": "openai"}, clear=False), \
        patch("pipeline_orchestrator._git_current_branch", return_value="main"), \
        patch("pipeline_orchestrator.shutil.which", return_value=sys.executable), \
        patch(
            "pipeline_orchestrator.provider_command",
            return_value=[sys.executable, "scripts/openai_provider.py", "--model", "gpt-5.4", "--file-tools", "prompt"],
        ) as provider_cmd, \
        patch(
            "pipeline_orchestrator.subprocess.run",
            side_effect=[
                MagicMock(returncode=0),  # git checkout -b
                MagicMock(returncode=0),  # provider run
                MagicMock(returncode=0),  # pytest
                MagicMock(returncode=0),  # git checkout original
                MagicMock(returncode=0),  # git merge
            ],
        ):
        assert auto_fix(error_context, "greenhouse", max_attempts=1) is True

    assert provider_cmd.call_args.args[0] == "openai"
    assert provider_cmd.call_args.kwargs["file_tools_enabled"] is True


def test_build_fix_prompt_keeps_test_execution_repo_control():
    prompt = _build_fix_prompt(
        {
            "exit_code": 1,
            "url": "https://boards.greenhouse.io/example/jobs/1",
            "output_dir": "/tmp/job-output",
        },
        "greenhouse",
    )

    assert "Do not run tests or git commands yourself" in prompt
    assert "Run the relevant tests after your fix." not in prompt
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
uv run python -m pytest tests/test_pipeline_orchestrator.py -k "auto_fix_uses_openai or build_fix_prompt_keeps_test_execution_repo_control" -v
```

Expected:

```text
FAIL ... provider_command called without file_tools_enabled
FAIL ... old prompt text still tells the provider to run tests
```

- [ ] **Step 3: Write the minimal implementation**

Update `scripts/pipeline_orchestrator.py` in three places.

1. Fix the module and function docstrings so they stop describing auto-fix as Claude-only:

```python
"""Shared pipeline orchestration logic for the job worker, CLI, and TUI.

This module contains the core job processing logic:
- provider_fallback: try LLM providers in order until one succeeds
- process_job: full job lifecycle (resolve -> generate -> submit -> fix -> retry -> post-submit)
- retry_with_recording: final submission attempt with Playwright trace
- auto_fix: invoke the configured provider to diagnose and fix autofill errors
"""
```

2. Remove the OpenAI-to-Claude fallback and enable file tools for OpenAI:

```python
    active_provider = os.environ.get("ASSET_LLM_PROVIDER", "claude")
    if not shutil.which(provider_binary(active_provider)):
        log.info("%s CLI not on PATH — skipping auto-fix", active_provider)
        return False
```

```python
                fix_command_kwargs = {"file_tools_enabled": active_provider == "openai"}
                fix_result = subprocess.run(
                    provider_command(active_provider, prompt, **fix_command_kwargs),
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
```

3. Keep test execution repo-controlled in the prompt text:

```python
def _build_fix_prompt(error_context: dict, board: str) -> str:
    """Build a prompt for the configured provider to fix a submission error."""
    return (
        f"The autofill script for {board} failed with exit code {error_context.get('exit_code')}. "
        f"The job URL is {error_context.get('url')}. "
        f"Output directory: {error_context.get('output_dir')}. "
        f"Please diagnose the error by reading the autofill script and recent logs, "
        f"then fix the issue and save your edits. Focus on scripts/autofill_{board}.py. "
        "Do not run tests or git commands yourself; the orchestrator will run tests and manage branches after you return."
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
uv run python -m pytest tests/test_pipeline_orchestrator.py -k "auto_fix_uses_openai or build_fix_prompt_keeps_test_execution_repo_control" -v
```

Expected:

```text
PASSED tests/test_pipeline_orchestrator.py::test_auto_fix_uses_openai_with_file_tools_without_claude_fallback
PASSED tests/test_pipeline_orchestrator.py::test_build_fix_prompt_keeps_test_execution_repo_control
```

- [ ] **Step 5: Commit**

```bash
git add tests/test_pipeline_orchestrator.py scripts/pipeline_orchestrator.py
git commit -m "fix(provider): use openai directly for auto-fix"
```

---

### Task 3: Route Interview Prep Through the Configured Provider

**Files:**
- Modify: `tests/test_interview_prep.py`
- Modify: `scripts/generate_interview_prep.py`
- Test: `tests/test_interview_prep.py`

- [ ] **Step 1: Write the failing tests**

Replace the old Claude-only import and fallback tests with provider-parity tests.

At the top of `tests/test_interview_prep.py`:

```python
import sys
from scripts.generate_interview_prep import _run_provider
```

Add these tests:

```python
@patch("scripts.generate_interview_prep.shutil.which", return_value=sys.executable)
@patch(
    "scripts.generate_interview_prep.provider_command",
    return_value=[sys.executable, "scripts/openai_provider.py", "--model", "gpt-5.4", "--search", "--file-tools", DUMMY_PROMPT],
)
@patch("scripts.generate_interview_prep.subprocess.run")
def test_openai_interview_prep_uses_shared_provider_command(
    mock_run: MagicMock,
    mock_provider_command: MagicMock,
    mock_which: MagicMock,
    prep_dir: Path,
) -> None:
    mock_run.return_value = MagicMock(returncode=0, stdout="# Prep", stderr="")

    with patch.dict("os.environ", {"ASSET_LLM_PROVIDER": "openai"}, clear=False):
        result = _run_provider(DUMMY_PROMPT, prep_dir)

    assert result == "# Prep"
    assert mock_provider_command.call_args.args[0] == "openai"
    assert mock_provider_command.call_args.kwargs["search_enabled"] is True
    assert mock_provider_command.call_args.kwargs["file_tools_enabled"] is True


@patch("scripts.generate_interview_prep.shutil.which", return_value="/usr/local/bin/codex")
@patch(
    "scripts.generate_interview_prep.provider_command",
    return_value=["codex", "exec", DUMMY_PROMPT],
)
@patch("scripts.generate_interview_prep.subprocess.run")
def test_interview_prep_uses_configured_provider_without_claude_fallback(
    mock_run: MagicMock,
    mock_provider_command: MagicMock,
    mock_which: MagicMock,
    prep_dir: Path,
) -> None:
    mock_run.return_value = MagicMock(returncode=0, stdout="# Prep", stderr="")

    with patch.dict("os.environ", {"ASSET_LLM_PROVIDER": "codex"}, clear=False):
        result = _run_provider(DUMMY_PROMPT, prep_dir)

    assert result == "# Prep"
    assert mock_provider_command.call_args.args[0] == "codex"


@patch("scripts.generate_interview_prep.subprocess.run")
def test_provider_error_message_is_provider_agnostic(mock_run: MagicMock, prep_dir: Path) -> None:
    mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="some error")

    with patch.dict("os.environ", {"ASSET_LLM_PROVIDER": "openai"}, clear=False), \
        patch("scripts.generate_interview_prep.shutil.which", return_value=sys.executable), \
        patch(
            "scripts.generate_interview_prep.provider_command",
            return_value=[sys.executable, "scripts/openai_provider.py", "--model", "gpt-5.4", DUMMY_PROMPT],
        ):
        with pytest.raises(RuntimeError, match=r"openai provider exited with code 1"):
            _run_provider(DUMMY_PROMPT, prep_dir)
```

Keep the existing file-write fallback test, but switch it from `_run_claude(...)` to `_run_provider(...)`.

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
uv run python -m pytest tests/test_interview_prep.py -v
```

Expected:

```text
FAIL ... cannot import _run_provider
FAIL ... old Claude fallback behavior still present
```

- [ ] **Step 3: Write the minimal implementation**

Update `scripts/generate_interview_prep.py`.

1. Replace the Claude-only imports/commentary with shared-provider imports:

```python
from llm_provider import provider_binary, provider_command
```

2. Replace `_run_claude(...)` with `_run_provider(...)`:

```python
_CLAUDE_INTERVIEW_PREP_ALLOWED_TOOLS = ",".join(
    [
        "WebSearch",
        "WebFetch",
        "Read",
        "Write",
        "Glob",
        "Grep",
        "mcp__plugin_playwright_playwright__browser_navigate",
        "mcp__plugin_playwright_playwright__browser_snapshot",
        "mcp__plugin_playwright_playwright__browser_click",
        "mcp__plugin_playwright_playwright__browser_take_screenshot",
    ]
)


def _run_provider(full_prompt: str, prep_dir: Path) -> str:
    """Run interview prep through the configured provider and return markdown content."""
    provider = os.environ.get("ASSET_LLM_PROVIDER", "claude")
    binary = provider_binary(provider)
    if shutil.which(binary) is None:
        log.warning("Interview prep provider %s is not available — skipping", provider)
        return ""

    output_path = prep_dir / "interview_prep.md"
    command = provider_command(
        provider,
        full_prompt,
        search_enabled=True,
        file_tools_enabled=provider == "openai",
        claude_allowed_tools=_CLAUDE_INTERVIEW_PREP_ALLOWED_TOOLS if provider == "claude" else None,
    )

    result = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=900,
    )

    if result.returncode != 0:
        stderr_snippet = (result.stderr or "")[:500]
        raise RuntimeError(f"{provider} provider exited with code {result.returncode}: {stderr_snippet}")

    if output_path.exists():
        return output_path.read_text(encoding="utf-8")

    content = result.stdout.strip()
    if not content:
        raise RuntimeError(f"{provider} provider produced no output and did not write the file.")

    prep_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return content
```

3. Update docstrings and progress text so they no longer claim Claude-only behavior:

```python
"""Generate a personalized interview preparation guide.

Gathers context from the job's output directory (JD, research, resume, work stories),
spawns the configured provider subprocess to generate a comprehensive prep guide in markdown,
then converts it to .docx and .pdf.
"""
```

```python
    current_provider = os.environ.get("ASSET_LLM_PROVIDER", "claude")
    _write_progress(prep_dir, "generating", f"{current_provider} generating prep guide for {company}")
    print(f"Generating interview prep guide for {ctx['jd_title']} at {company}...")
    print(f"This may take several minutes as {current_provider} researches and writes.")

    md_content = _run_provider(full_prompt, prep_dir)

    if not md_content:
        _write_progress(prep_dir, "skipped", f"Interview prep provider {current_provider} not available — skipped")
        print(f"Interview prep skipped ({current_provider} not available).")
        return
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
uv run python -m pytest tests/test_interview_prep.py -v
```

Expected:

```text
PASSED tests/test_interview_prep.py::...
```

- [ ] **Step 5: Commit**

```bash
git add tests/test_interview_prep.py scripts/generate_interview_prep.py
git commit -m "fix(provider): use configured provider for interview prep"
```

---

### Task 4: Update Current Docs and Config Comments

**Files:**
- Modify: `docs/provider-setup.md`
- Modify: `agent_preferences.md`
- Modify: `.env.local`
- Modify: `docs/worker-pipeline-patterns.md`
- Modify: `docs/board-architecture.md`
- Modify: `docs/cli-reference.md`

- [ ] **Step 1: Update the current docs/comments**

Apply these edits.

In `docs/provider-setup.md`:

```md
| `gemini` | `gemini` | `gemini-3.0-flash` | [ai.google.dev/gemini-api/docs/quickstart](https://ai.google.dev/gemini-api/docs/quickstart) |
| `gemini-flash` | `gemini` | `gemini-3.0-flash` | Same as gemini |
```

Replace the limitations bullets with current behavior:

```md
- **Interview prep uses the configured provider.** `generate_interview_prep.py` now routes through the shared provider layer. OpenAI uses web search plus file tools; Claude keeps its richer browser-enabled tool allowlist through the same provider command path.
- **Auto-fix uses the configured provider.** The orchestrator still owns branch creation, pytest, merge, and cleanup. When the active provider is `openai`, the provider call enables file tools so it can read and edit repo files before control returns to the orchestrator.
```

In `agent_preferences.md`:

```md
- Default LLM provider for pipeline: **OpenAI API** (`ASSET_LLM_PROVIDER=openai`), model `gpt-5.4`. Provider is configurable via `ASSET_LLM_PROVIDER` env var (supports `claude`, `codex`, `openai`, `gemini`, `gemini-flash`). Gemini defaults to `gemini-3.0-flash`. Auto-fix and interview prep use the configured provider too.
```

In `.env.local` comments:

```bash
# Auto-fix and interview prep use the active provider too.
# GEMINI_MODEL=gemini-3.0-flash               # optional: override model
```

In `docs/worker-pipeline-patterns.md`:

```md
`AUTO_FIX_CONCURRENCY` (default 3, was 1). Each auto-fix spawns the configured provider through `pipeline_orchestrator.auto_fix()`.
```

```md
On-demand guide generation via `scripts/generate_interview_prep.py` (CLI) or web UI "Interview Prep" tab. Uses the configured provider for research and guide generation. Output: `interview_prep/interview_prep.md` + `.docx` + `.pdf`.
```

In `docs/board-architecture.md`:

```md
- **`gemini`** — defaults to `gemini-3.0-flash`. Override via `GEMINI_MODEL`.
- **`gemini-flash`** — uses the same `gemini` CLI binary and also targets `gemini-3.0-flash` by default. Configured via `GEMINI_FLASH_MODEL`.
```

```md
- **`scripts/job_worker.py`** — Worker pool (default 40 concurrent). Provider fallback, auto-fix via the configured provider, retry with Playwright recording, post-submit Notion sync + email reply. Launch: `job-assets worker start`. Isolated browser profiles per worker.
```

In `docs/cli-reference.md`:

```md
- `gemini` targets `gemini-3.0-flash` by default; `gemini-flash` also targets `gemini-3.0-flash`. Both use the same `gemini` CLI binary. Override with `GEMINI_MODEL` or `GEMINI_FLASH_MODEL`.
```

```md
- Uses the configured provider subprocess for deep research and writing. When OpenAI is active, the command enables web search plus file tools before the repo converts the markdown into `.docx` and `.pdf`.
```

- [ ] **Step 2: Run the drift grep to verify the current docs are aligned**

Run:

```bash
rg -n "gemini-3.1-pro-preview|gemini-3.1-pro|requires Claude|falls back to Claude" \
  docs/provider-setup.md \
  docs/worker-pipeline-patterns.md \
  docs/board-architecture.md \
  docs/cli-reference.md \
  agent_preferences.md \
  .env.local \
  scripts/pipeline_orchestrator.py \
  scripts/generate_interview_prep.py
```

Expected:

```text
no matches
```

- [ ] **Step 3: Run the full verification commands**

Run:

```bash
uv run python -m pytest tests/test_llm_provider.py tests/test_pipeline_orchestrator.py tests/test_interview_prep.py -v
uv run ruff check scripts/ tests/
uv run python scripts/check_architecture.py
uv run python scripts/sync_agent_files.py --check
uv run python scripts/check_agent_docs.py
```

Expected:

```text
all targeted tests pass
ruff exits 0
architecture check exits 0
sync check exits 0
agent docs check exits 0
```

- [ ] **Step 4: Commit**

```bash
git add docs/provider-setup.md agent_preferences.md .env.local docs/worker-pipeline-patterns.md docs/board-architecture.md docs/cli-reference.md
git commit -m "docs(provider): align runtime guidance with openai parity"
```

---

## Self-Review

### Spec Coverage

- OpenAI auto-fix parity: covered in Task 2
- OpenAI interview prep parity: covered in Task 3
- Gemini default to Flash: covered in Task 1
- Current docs/config alignment: covered in Task 4

### Placeholder Scan

- No `TBD`, `TODO`, or deferred “write tests later” steps remain.
- Every task names exact files, commands, and code snippets.

### Type and Name Consistency

- `auto_fix`, `_build_fix_prompt`, and `_run_provider` are the function names used consistently throughout the plan.
- Provider flags use the existing `provider_command(..., search_enabled=..., file_tools_enabled=...)` API rather than inventing new names.
