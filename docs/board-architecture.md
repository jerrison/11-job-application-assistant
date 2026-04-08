# Board Architecture

## Autofill Architecture

Board-specific autofill scripts follow a composition-based architecture:

- **`scripts/autofill_common.py`** — shared utilities (label matching, option selection, screenshot capture, page snapshotting, report writing, submit button clicking, captcha helpers)
- **`scripts/autofill_pipeline.py`** — shared orchestration (`autofill_main` for CLI entry + payload building, `run_browser_pipeline` for browser-based navigate/fill/submit/confirm flow)
- **`scripts/autofill_{board}.py`** — board-specific logic only (CSS selectors, form parsing, field inference, form filling)
- **`scripts/question_classifier.py`** — unified question classifier. Single entry point (`classify_question()`) that maps question labels to categories using priority-ordered detector dispatch. Board scripts use the category result, then let shared policy code decide whether the prompt is affirmative positive-fit, profile-driven, or still non-deterministic.
- **`scripts/application_submit_common.py`** — lower-level shared functions (profile parsing, `resolve_shared_question_policy()`, LLM answer generation, Gmail polling, Notion sync, individual question detector functions, credential-backed claim lookup)

New boards import from `autofill_common`, `autofill_pipeline`, and `application_submit_common` only. Simple single-page ATS families (Breezy, Recruitee, Jobvite, JazzHR, Paycor) reuse shared payload helpers from `application_submit_common.py`, shared fill/classification helpers from `autofill_common.py`, and the thin runtime wrapper in `autofill_pipeline.py` while still keeping board naming, selectors, and submit-state rules explicit in each `autofill_{board}.py`. API-based boards (Dover) and multi-page wizard or auth-gated boards (Workday, Phenom, SuccessFactors) use `autofill_main` with a custom `run_browser_fn` callback instead of raw `run_browser_pipeline`.

**Supported boards (25):** Greenhouse, Ashby, Lever, Gem, Dover, Workday, Phenom, iCIMS, Eightfold, BambooHR, SmartRecruiters, Workable, Comeet, Rippling, Uber, Motion Recruitment, Reducto, Email, LinkedIn Easy Apply, SuccessFactors, Breezy, Recruitee, Jobvite, JazzHR, Paycor.

**Auth-required boards:** Workday (`WORKDAY_PASSWORD`), iCIMS (session-based), Uber (`UBER_PASSWORD`). Credentials via env vars in `.env.local`.

**Conventions:** Board scripts import shared utilities from `autofill_common.py` (no duplication). Each board keeps `_classify_submit_state`, `_fill_step`, and `_infer_step` inline (selectors/logic differ per board). Greenhouse is not yet consolidated. **AGENTS.md is the only editable instruction file**; all generated provider copies must be regenerated from it via `scripts/sync_agent_files.py`, and CI enforces parity.

## Captcha Wait and Headed Browsers

Submit/approve runs default to **headed (visible) browsers**. Draft runs default to headless. Explicit `--headless` / `--no-headless` flags override.

When a captcha is detected during a headed submit:

1. `wait_for_captcha_resolution()` in `autofill_common.py` is called (default implementation of the `wait_for_captcha_fn` hook in `run_browser_pipeline`)
2. Signal file `submit/awaiting_captcha.json` written — orchestrator polls every 5s and sets job status to `awaiting_captcha`
3. macOS notification sent; browser page title set to `[Captcha] {Company} — {Role}`; the runtime unminimizes and raises that window once so the user can act immediately
4. Pipeline polls page state + email confirmation every 3s for up to `JOB_ASSETS_CAPTCHA_TIMEOUT` seconds (default 3600)
5. On confirmation → downstream actions fire (Notion sync, email reply) → return 0
6. On timeout → debug artifacts saved → return `CAPTCHA_SKIP_EXIT_CODE` (75) → job status → `stopped`
7. Signal file always cleaned up in `finally` block

When headless, captcha detection returns `CAPTCHA_SKIP_EXIT_CODE` immediately (no waiting — nobody can interact with an invisible browser). Job status → `stopped` with error message.

**Auth failures vs. captchas:** Workday/iCIMS/Uber auth artifacts are not captchas. For Workday, only explicit credential rejection is a true `auth_failed` outcome. Maintenance/service-interruption pages write a retryable `service_unavailable` result, and ambiguous exhausted gateway states write `auth_unknown` with rich evidence (`auth_state`, `auth_scope`, last attempted step, heading, alert text, visible CTAs, screenshot/page text). These states should not be flattened into “wrong password” or captcha handling.

**Unsupported boards:** When a URL falls outside the supported set after direct-host checks plus wrapper HTML probes, `submit_application.py` logs to `submit/unsupported_board.json` and returns exit code 0. Materials are still generated for manual application.

**Browser minimization:** Headed browsers start minimized. On macOS, new Playwright Chromium windows default to AeroSpace workspace `Q` (the user's `alt-q` space; override with `JOB_ASSETS_AEROSPACE_WORKSPACE`) so background automation stays out of the active space. The browser stays backgrounded unless manual action is required. Captcha/manual challenges send the notification, unminimize/raise the `[Captcha]` window once, and still support the web UI "Focus Browser" button or Dock icon for refocus later.

**Web UI:** `awaiting_captcha` jobs show a pulsing orange badge and a "Focus Browser" button that re-raises the `[Captcha]`-titled Chromium window to foreground via the shared browser-runtime helper.

**Subprocess timeout:** Headed submit runs get `JOB_ASSETS_CAPTCHA_TIMEOUT + 300` seconds subprocess timeout (vs 900s for headless) to accommodate manual captcha solving.

## Browser Profile and Google Session

All workers share a single Playwright persistent profile at `~/.job-assets/playwright-submit-profile`. No per-worker subdirectories — a single `setup_browser_profile.py` sign-in benefits every worker.

**Setup:** `uv run python scripts/setup_browser_profile.py` opens a headed browser for Google sign-in. Session persists across all future autofill runs. Re-run if session expires (reCAPTCHA scores degrade).

**Google session check:** `ensure_google_session()` in `browser_runtime.py` runs before each autofill. Hits Google `ListAccounts` endpoint; if no signed-in accounts:
- **Headed:** navigates to `accounts.google.com`, sends macOS notification, waits up to 5 min for user to sign in, then continues.
- **Headless:** logs warning and continues (no interactive auth possible).

**Web server security:** `LocalOnlyMiddleware` in `job_web.py` rejects all non-localhost requests with HTTP 403. Blocks external scanners/bots probing via tunnels or port exposure.

## Provider Architecture

- **`VALID_PROVIDERS`** — canonical tuple in `scripts/llm_provider.py` listing all accepted provider names (`gemini`, `gemini-flash`, `claude`, `codex`, `openai`). All CLI entrypoints (`bin/job-assets`, `autofill_pipeline.py`, `submit_application.py`, `job_assets_pipeline.py`, `autofill_greenhouse.py`) import this constant instead of hardcoding provider choice lists.
- **`gemini-flash`** — uses the same `gemini` CLI binary but targets `gemini-3-flash-preview` model by default. Configured via `GEMINI_FLASH_MODEL` env var.
- **Automation fallback chain** — drafting, worker fallback, Greenhouse preference research, and submit-time answer generation resolve through `automation_provider_chain()` in `scripts/llm_provider.py`, which keeps automated retries within OpenAI and Gemini even if older env values still mention Claude or Codex. `_provider_binary()` in `application_submit_common.py` maps `gemini-flash` to the `gemini` binary for `shutil.which()` checks.

## TUI, Worker, and Job Queue Architecture

Three components share a single SQLite database (`jobs.db`, WAL mode):

- **`scripts/job_tui.py`** — Textual TUI (Dashboard, Queue, Job Detail). Polls DB every 1.5s. Launch: `job-assets tui`.
- **`scripts/job_worker.py`** — Worker pool (default 40 concurrent). Provider fallback, auto-fix via the configured provider, retry with Playwright recording, post-submit Notion sync + email reply. Launch: `job-assets worker start`. Isolated browser profiles per worker.
- **`bin/job-assets`** — CLI commands (`add`, `queue`, `status`, `retry`, `skip`, `prioritize`, `worker`, `report`, `tui`, `import`).

**Key modules:** `job_db.py` (SQLite interface, tables: `jobs`, `events`, `fix_attempts`, `provider_runs`), `pipeline_orchestrator.py` (provider fallback, auto-fix, recording retry), `url_resolver.py` (URL classification/resolution). Rate limit: 1 concurrent job per board. Crash recovery: stale jobs >30min reset to `queued`.

## Draft Mode

**Status flow:** `queued → resolving → generating → autofilling → draft → submitting → submitted`

- `autofilling` — Phase 3: browser is open, form is being filled (draft mode). This is the active form-filling phase.
- `draft` — form filled and screenshots taken; pipeline paused for user review before any submit click.
- `submitting` — explicit approved submission in progress (only entered after user approves the draft).
- `submitted` — submission confirmed (website + email).

Pipeline stops at `draft` status after autofilling (form filled, screenshots taken) but before clicking submit. User reviews via CLI (`job-assets draft list|review|approve|reject|regenerate`), TUI (Drafts filter tab + detail view), web (`job-assets draft serve`), or LLM conversation.

**Key files:** `draft_manager.py` (summary generation, diff classification, override management), `build_draft_summary.py` (PNG rendering), `draft_web.py` (FastAPI server).

**Draft artifacts** (in role output root): `draft_summary.md` (editable), `draft_summary.original.md` (immutable for diffing), `draft_summary.png`, `draft_status.json`, `draft_overrides.json`, `draft_fix_report.md`, `answer_refresh_status.json`.

**Answer refresh proof:** Explicit answer-affecting actions (`reanswer`, draft overrides, full regenerate, restart pipeline) write a durable request to `answer_refresh_status.json`, force fresh answer generation at the submit-artifact seam, and resolve to `fresh`, `not_applicable`, or `failed`. In the web draft UI, the proof appears once as a shared non-sticky helper card below the sticky job-detail dock across all tabs, and it also appears in `draft_summary.md`/`.png`. Legacy drafts with no sidecar remain `unknown` until a new explicit regenerate action occurs.

**Edit-to-fix loop:** User edits `draft_summary.md` → `draft_manager.py` auto-classifies changes (missing_handler, wrong_answer) → generates fix report → `auto_fix()` applies generalized code fixes → pipeline regenerates with research cache preserved and `draft_overrides.json` applied.

**Flags:** `--draft` on `submit_application.py` (autofill form + stop at draft, no submit click). `--auto-submit` on worker/orchestrator (bypass draft, go straight to `submitting`). Stale drafts (>7 days, configurable via `JOB_ASSETS_DRAFT_TTL_DAYS`) auto-expire to `stopped`.

**Full regenerate contract:** Full regenerate is now answer-affecting work, not just an asset-cache reset. When the user asks for a full draft regenerate, answer caches in the active and prior `submit*` directories are bypassed so prompt or policy changes can produce fresh answer proof even if the question list is unchanged.

## URL Canonicalization

`scripts/job_board_urls.py` normalizes board-specific application URLs to canonical JD URLs before scraping:

- **Ashby** — strips `/application` suffix; multi-label hosts concatenated (e.g. `li.me` → `lime`)
- **Lever** — strips `/apply` suffix and query params (e.g. `?source=LinkedIn`)
- **Workday** — strips tracking query params (e.g. `?source=LinkedIn`). Supports both `myworkdayjobs.com` (company in subdomain) and `myworkdaysite.com` (company in `/recruiting/{company}/` path) URL patterns
- **Dover** — normalizes trailing slashes

Board autofill scripts reconstruct the application URL from the canonical JD URL as needed.

## Greenhouse Board Slug Discovery

For custom Greenhouse career pages (e.g. `careers.toasttab.com/jobs/...?gh_jid=123`), use `?board=` parameter if present; otherwise probe the Greenhouse API with hostname-derived candidates (skipping generic subdomains like "careers", "jobs", "apply") and optional company hint from pipeline metadata.

## Research Cache

Two-tier cache for company/role research:

- **Company cache** — `output/{company}/research_cache.json` — shared across roles, 30-day configurable TTL
- **Role cache** — `output/{company}/{role}/content/role_research_cache.json` — per-JD (keyed by SHA-256 hash of `jd_parsed.json`), same TTL
- **Config:** `JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS` env var (default 30, set to 0 to force re-research)
