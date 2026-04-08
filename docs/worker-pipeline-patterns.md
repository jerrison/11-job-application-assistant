# Worker & Pipeline Patterns

## Status Flow

`queued → resolving → generating → autofilling → draft → submitting → submitted`

Phase 3 form filling (draft mode) uses `autofilling`; `submitting` is only used for explicit approved submissions.

## Auto-Retry

Transient failures (rate limits, timeouts, auto-fix busy) are auto-retried up to `MAX_AUTO_RETRIES` (default 3). Workday `service_unavailable` outcomes also use this transient retry path. Provider-wide OpenAI/Gemini capacity failures requeue separately as `llm_rate_limited` without consuming the normal fix-attempt budget. After exhaustion of the normal transient budget, jobs go to `stopped` with a suggestion.

## Rate Limiting

- Per-provider concurrency semaphores (`LLM_PROVIDER_CONCURRENCY`, default 15) + in-place rate-limit retry (up to `RATE_LIMIT_RETRIES`, default 5, with exponential backoff 5s→120s) before falling back to the next automation provider in the OpenAI/Gemini chain + staggered worker job starts.
- When both automation providers are exhausted by rate limits, the worker requeues the affected job with `retry_after`, records `llm_rate_limit_retry`, and applies a short global LLM cooldown so queued jobs do not immediately burn the same exhausted providers.
- JD fetch retry: up to `JD_FETCH_MAX_RETRIES` (default 3) with exponential backoff (20s→40s→80s) + 2s delay between individual extraction attempts.

## Auto-Fix Concurrency

`AUTO_FIX_CONCURRENCY` (default 3, was 1). Each auto-fix invokes the configured provider through the shared fix-mode command path.

## Draft Completeness

Drafts require resume PDF + cover letter PDF + current-attempt autofill report + current-attempt screenshot, and every extracted field must be explicitly accounted for, including optionals. Repairable audit failures requeue up to three times using a separate audit-attempt counter. After the third failed repair cycle, the job lands in `stopped` with `failure_type = audit_failure`.

Exhausted audit failures also write human-readable markdown:

- per-job note: `output/<company>/<role>/submit/audit_failure.md`
- rolling index: `output/_audit/active_audit_failures.md`

## Submitted Job Lock

Jobs with `confirmed_at` are locked by default. Workers and retry logic must not move a locked job back into `queued`, `reanswering`, `approved`, `autofilling`, or `draft`. A user must explicitly unlock the row for one resubmission cycle, and the next confirmed submit relocks it automatically.

## LLM Answer Tracking

`generated_application_answer` fields are counted and surfaced in the queue table as "AI answers" badge.

## Worker Controls

Web UI panel at `/` shows per-worker status, phase, elapsed time. Per-worker stop/kill via API. State published to `jobs.db.worker_state.json`.

## Archived Filtering

Workers never pick up archived jobs (`get_pending_jobs` filters by `archived` flag).

## Browser Profile

All workers share a single Playwright persistent profile at `~/.job-assets/playwright-submit-profile` (no per-worker isolation). Google session set up via `uv run python scripts/setup_browser_profile.py` persists across all workers. Improves reCAPTCHA v3 scores.

LinkedIn Easy Apply uses a separate profile at `.playwright-linkedin/` with file lock serialization.

## Google Session Check

`ensure_google_session()` in `browser_runtime.py` runs before each autofill. Hits `ListAccounts` endpoint; if expired (headed mode), navigates to Google sign-in and waits up to 5 min for user to re-auth. macOS notification sent. Headless mode logs warning and continues. Skipped for LinkedIn Easy Apply (separate profile).

## Browser Minimization

Headed browsers start minimized. On macOS, new Playwright Chromium windows default to AeroSpace workspace `Q` (the user's `alt-q` space; override with `JOB_ASSETS_AEROSPACE_WORKSPACE`) so background automation stays out of the active space. The browser stays backgrounded unless manual action is required. Captcha/manual challenges send the notification, unminimize/raise the `[Captcha]` window once, and the web UI "Focus Browser" button can re-raise it later if needed.

## Captcha Wait (Headed Browsers)

Submit/approve runs default to headed browsers. If captcha is detected, the browser stays open for manual solving (up to `JOB_ASSETS_CAPTCHA_TIMEOUT` seconds, default 3600). macOS notification sent and the `[Captcha]` window is unminimized/raised once so the user can act immediately. Job status transitions to `awaiting_captcha`. Signal file `submit/awaiting_captcha.json` bridges subprocess ↔ orchestrator. Auth artifacts (Workday, iCIMS, Uber) are not captchas. For Workday specifically, explicit credential rejection becomes `auth_failed`, tenant-scoped guard skips become `auth_guarded`, maintenance/service interruption becomes retryable `service_unavailable`, and exhausted gateway states become `auth_unknown` with saved evidence.

Runtime note for draft attempts: they still start headless by default. If the current attempt hits captcha, the orchestrator reruns that submit phase once in headed mode, transitions the job to `awaiting_captcha`, and leaves the browser open for manual solve. If that headed retry still cannot clear captcha, the job stops with the existing captcha failure result.

## Web Server Security

`LocalOnlyMiddleware` rejects all non-localhost requests with 403. Blocks external scanners/bots that probe via tunnels.

## Post-Submit Screenshots

`{board}_autofill_post_submit.png` captured on confirmation. Shown in dedicated "Confirmation" tab (separate from pre-submit "Screenshot" tab). Only visible for `submitted` jobs. Per-page screenshots from multi-step boards (Workday, LinkedIn) shown in "Form Pages" section of Screenshot tab.

## Interview Prep

On-demand guide generation via `scripts/generate_interview_prep.py` (CLI) or web UI "Interview Prep" tab. Routes through the configured provider with shared interview-prep settings (web research enabled; OpenAI also gets file tools; Claude keeps its richer tool allowlist). Output: `interview_prep/interview_prep.md` + `.docx` + `.pdf`.

## LinkedIn Saved Import

`uv run python scripts/import_linkedin_saved.py` scrapes all saved jobs from LinkedIn (button-based pagination, 10 jobs/page) using persistent profile at `.playwright-linkedin/`. Feeds URLs into `add_job()` with canonical_url dedup, then runs JD fingerprint backfill + duplicate group detection. Available from CLI, TUI, and web UI. API: `POST /api/jobs/import-linkedin-saved`.

## Resume Auto-Sync

`sync_if_stale()` in `sync_master_resume.py` checks `.master_resume_sync_state.json` timestamp. If >24h since last sync, it fetches the latest configured remote source before asset generation. Called automatically at the start of `process_job()` phase 1-2.
