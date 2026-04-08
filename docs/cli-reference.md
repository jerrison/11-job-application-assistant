# CLI Reference

Prefer the installed CLI commands over raw `./script.sh` invocations when working locally. The shell-script entrypoints remain valid, but they are implementation details now.

Primary commands:

```bash
# Single job end-to-end, review by default
job-assets <jd_source>
job-assets --submit <jd_source>

# Explicit subcommands
job-assets apply <jd_source> [company] [role-slug]
job-assets pipeline <jd_source> [company] [role-slug] [--submit]
job-assets submit output/<company>/<role-slug> [--submit]
job-assets batch [--dry-run] [--max-parallel N] [--provider gemini|gemini-flash|claude|codex|openai]
job-assets parallel [--dry-run] [--build-only] [--max-parallel N] [--provider gemini|gemini-flash|claude|codex|openai]
job-assets profile [show|path]
job-assets settings show
job-assets settings import <master_resume|work_stories|candidate_context|application_profile> [--file PATH|--url URL|--text TEXT]
job-assets settings set [--default-provider ...] [--openai-api-key ...] [--gemini-api-key ...]
job-assets notion-sync <target> [--wait-for-email N]

# Provider-specific wrappers
job-assets-codex <jd_source>
job-assets-claude <jd_source>
job-assets-codex --submit <jd_source>
job-assets-codex parallel --max-parallel 8
job-assets-claude batch --dry-run
```

Command discovery:
- Use `job-assets --help` for the top-level command list
- Use `job-assets apply --help`, `job-assets pipeline --help`, `job-assets submit --help`, `job-assets batch --help`, `job-assets parallel --help`, or `job-assets notion-sync --help` for subcommand details
- Use `man job-assets`, `man job-assets-codex`, or `man job-assets-claude` for manual pages after installation

## Provider Guidance

- Five providers are supported: `gemini`, `gemini-flash`, `claude`, `codex`, and `openai`. The canonical list is `VALID_PROVIDERS` in `scripts/llm_provider.py` — all CLI entrypoints import from it so provider choices stay in sync.
- `gemini` targets `gemini-3-flash-preview` by default; `gemini-flash` targets `gemini-3-flash-preview`. Both use the same `gemini` CLI binary. Override with `GEMINI_MODEL` or `GEMINI_FLASH_MODEL`.
- Use `--provider gemini|gemini-flash|claude|codex|openai` when the provider should be explicit
- **Provider defaults & timeouts:** Claude defaults to `claude-sonnet-4-6` / `--effort max`; Codex to `gpt-5.4` / `xhigh`. Override via `CLAUDE_MODEL`, `CLAUDE_EFFORT`, `CLAUDE_PERMISSION_MODE`, `CLAUDE_EXTRA_ARGS`, `CODEX_MODEL`, `CODEX_REASONING_EFFORT`, `CODEX_APPROVAL_POLICY`, `CODEX_SANDBOX_MODE`, `CODEX_PROFILE`, `CODEX_EXTRA_ARGS`. When `CODEX_PROFILE` is set without explicit `CODEX_*` overrides, the CLI defers to that profile. Timeouts: `JOB_ASSETS_PROVIDER_TIMEOUT_SECONDS` (default 600s, submit-time answers), `JOB_ASSETS_ASSET_PROVIDER_TIMEOUT_SECONDS` (default 1200s, asset generation). Set to `0` to disable.
- **Asset generation flow:** Two-step: company research → resume/cover-letter drafting from cached `research_cache.json`. If Claude times out or fails, retries once with Codex. Override with `JOB_ASSETS_CLAUDE_PRIMARY_ASSET_TIMEOUT_SECONDS` and `JOB_ASSETS_CLAUDE_ASSET_FALLBACK_PROVIDER` (`0`/empty to disable). Submit-time answers also retry once with Codex on failure. Raw output written to `content/llm_research_raw.txt`, `content/llm_drafting_raw.txt`, `content/llm_fix_attempt_<n>_raw.txt`; fallback retries write `*_fallback_raw.txt`. If provider exits non-zero but wrote files, pipeline finalizes and continues.
- **Provider isolation:** CLI mirrors the TUI: Claude uses `--permission-mode auto` with `--print` before the prompt; Codex forces approval/sandbox only when env overrides profile defaults. Non-interactive Codex runs use a temporary `CODEX_HOME` that mirrors global `AGENTS.md`, prompts, skills, and `mcp_servers`; the wrapper still strips the `features.apps` flag. Non-interactive Claude subprocesses disable session persistence, slash commands, and MCP plugins. Shared provider argv and tool policy live in `scripts/llm_provider.py`.
- **Env defaults:** `UV_CACHE_DIR` defaults to `./.uv-cache` when unset. Repo entrypoints auto-load `.env` and `.env.local`.
- When changing the Codex non-interactive invocation, validate the generated command against the installed CLI surface from both `codex --help` and `codex exec --help`. Codex has moved approval/sandbox/search flags between the top-level command and `exec` across versions, and provider tests alone are not enough if the live CLI no longer accepts the built argv.
- When a provider subtask is asked to produce `resume_content.json` or `cover_letter_text.txt`, it must read the prepared inputs and write those files directly. Do not recurse back into `job-assets`, `apply.sh`, `scripts/run_pipeline.py`, `scripts/job_assets_pipeline.py`, or other repo entrypoints from inside the provider call.
- Use bare `job-assets <jd_source>` for end-to-end single-job (assets + submit); `job-assets apply ...` for assets only.
- `job-assets settings ...` is the CLI view of the same onboarding/settings backend used by the web UI, TUI, and packaged app runtime.

## Batch & Parallel

- `job-assets batch` and `job-assets parallel` share the same worker pool. Use `batch` by default, `parallel` for extra flags like `--build-only`. Worker count auto-sizes to `max(4, min(cpu_count * 2, 16))` unless overridden by `--max-parallel` / `JOB_ASSETS_MAX_PARALLEL`. Same-company workers coordinate via a lock on `research_cache.json`.

## Submit

- **Supported boards:** Greenhouse (including `gh_jid=...` wrappers), Gem, Lever, Ashby (including `ashby_jid=...` wrappers and direct `jobs.ashbyhq.com` URLs), Dover (`app.dover.com/apply/...`), Workday. On captcha (hCaptcha on Gem/Lever, reCAPTCHA on Ashby), skip gracefully — record `skipped_captcha` in `submit/application_submission_result.json`, exit 0 for batch continuity. Board-specific notes: Lever uses static `/apply` form with word-boundary label matching, pastes long answers, fills inputs directly when hCaptcha intercepts label clicks. Dover is API-based (multipart payload mirroring frontend).
- **Submit idempotency:** If `application_submission_result.json` already shows website-confirmed, reruns skip the live submit and resume only email/Notion reconciliation. Cached `application_answers.json` payloads are reused. Single-job reruns reuse unchanged `content/` and `documents/` assets. Gmail polling is time-bounded and company/timestamp-scoped.
- **Explicit answer refresh contract:** User-requested answer-affecting reruns (`job-assets draft regenerate`, web/TUI reanswer, draft overrides, full regenerate, restart-pipeline) bypass reusable answer caches, rewrite `submit/application_answers.json` plus raw answer artifacts, and update output-root `answer_refresh_status.json`. Successful runs record provider/time proof, drafts with zero generated answer fields resolve to `not_applicable`, and missing proof is surfaced as a visible failure instead of a silent return to `draft`.
- **Browser provider:** `--browser-provider local|steel` or `JOB_ASSETS_BROWSER_PROVIDER`. Steel Cloud requires `STEEL_API_KEY`; self-hosted: `STEEL_LOCAL=true` + optional `STEEL_BASE_URL`.

## Interview Prep (On-Demand)

Generate interview preparation guides for any job:
- **CLI**: `uv run python scripts/generate_interview_prep.py <output_dir> [--stage Onsite] [--interviewer "Name, Title"] [--notes "focus areas"]`
- **Web UI**: Job detail -> "Interview Prep" tab -> "Generate Interview Prep" button
- **Output**: `interview_prep/interview_prep.md` + `.docx` + `.pdf` in the job's output directory
- Uses the configured provider via the shared provider layer. Interview prep enables web research for all providers, keeps Claude's richer tool allowlist when Claude is selected, and enables file tools for OpenAI.
