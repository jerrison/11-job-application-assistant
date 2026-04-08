# Performance-First macOS App Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a fast local macOS `.app` that launches without the repo, stores all mutable state outside the app bundle, onboards a brand-new user from uploads/paste/URLs for resume and context materials, exposes a shared Settings page in both the web app and mac app for editing those materials plus provider credentials, and then runs the existing draft-first job-application workflow from the desktop UI.

**Spec:** Conversation request in the 2026-04-08 session: package the repo minus current generated artifacts as a macOS application, make onboarding work for a new user starting from scratch, and optimize aggressively for performance.

**Existing code:** `bin/job-assets`, `scripts/job_web.py`, `scripts/static/index.html`, `scripts/static/app.js`, `scripts/static/style.css`, `scripts/job_db.py`, `scripts/output_layout.py`, `scripts/project_env.py`, `scripts/browser_runtime.py`, `scripts/job_worker.py`, `scripts/job_tui.py`, `scripts/job_assets_pipeline.py`, `scripts/pipeline_orchestrator.py`, `scripts/submit_application.py`, `scripts/llm_common.sh`, `docs/shared-inputs.md`, `README.md`

---

## Purpose / Big Picture

The current repo behaves like "source tree = runtime": `jobs.db`, `output/`, logs, browser state, and source materials all sit at repo root. That works for development but it is the wrong shape for a distributable desktop app, and it guarantees that packaging either includes stale artifacts or breaks as soon as the app is moved off the repo.

This plan turns the existing local web UI into the desktop product surface. The packaged app will contain only code, static assets, and build-time resources. All user-owned data will move into a writable app-home directory under macOS Application Support. On first launch, the app should either import the current repo-root materials for the existing user or walk a brand-new user through onboarding: upload a resume, paste text, or provide URLs for the canonical materials the pipeline already expects.

The onboarding surface and the long-lived Settings surface should be the same product concept. A brand-new user lands in onboarding first, and after onboarding the same settings area remains available for editing the master resume, work stories, candidate context, application defaults, and provider credentials such as OpenAI and Gemini API keys. The web app and mac app must share the same backend settings APIs and storage model; only the launcher differs.

The performance target is not "good enough desktop packaging." The packaged app should feel immediate:

- First interactive onboarding shell visible in under 1.5s cold and under 750ms warm on the target development machine.
- Existing-user dashboard usable in under 5s cold and under 2s warm.
- No `uv` bootstrapping, dependency resolution, Playwright browser download, or worker startup on the critical path to first paint.
- Worker startup and heavy imports remain off the first-render path and become lazy/background work.
- Bundle contents are whitelisted so generated artifacts, logs, browser state, and local databases can never leak into the distributed app.

---

## Context and Orientation

- **Docs to read:** `AGENTS.md`, `ARCHITECTURE.md`, `docs/shared-inputs.md`, `docs/output-structure.md`, `docs/provider-setup.md`, `docs/launch-modes.md`, `docs/exec-plans/README.md`
- **Primary files:** `bin/job-assets`, `scripts/job_web.py`, `scripts/job_db.py`, `scripts/output_layout.py`, `scripts/project_env.py`, `scripts/browser_runtime.py`, `scripts/job_worker.py`, `scripts/static/index.html`, `scripts/static/app.js`, `scripts/static/style.css`
- **Helpful existing patterns to preserve:**
  - `scripts/job_web.py` already exposes paginated queue APIs and should remain the primary product surface.
  - `scripts/static/index.html` and `scripts/static/app.js` already include a Settings screen that can grow into onboarding/profile management.
  - `scripts/browser_runtime.py` already puts browser profile state under a user-scoped home path instead of the repo root.
  - `bin/job-assets` already has a `sys.executable` fallback when `uv` is absent; packaged mode should extend that pattern everywhere.
- **Constraints:**
  - Keep `--draft` as the default; packaging must not weaken submit guardrails.
  - Preserve the canonical material contract (`master_resume.md`, `work_stories.md`, `candidate_context.md`, `application_profile.md`) even though the files move out of the repo root.
  - Bundle resources must be explicit allowlists; never rely on blacklisting `output/`, `jobs.db`, or local caches after the fact.
  - Packaged mode must not assume a git checkout, writable bundle, or externally installed `uv`.
  - A brand-new user must be able to complete onboarding without editing files in Finder or Terminal.
  - Pure UI settings like theme/font size can remain client-local; user application materials and runtime defaults must be persisted server-side in app-owned storage.
  - Provider credentials are secrets. They must never be stored in browser `localStorage`, committed to the bundle, echoed into logs, or returned verbatim by read APIs.
  - The web app and the packaged mac app must expose the same Settings page and the same editable inputs for materials, runtime defaults, and provider credentials.

---

## Milestones

1. **Milestone 1:** Runtime portability exists. The app can resolve code resources separately from mutable user data, and CLI/web/worker/TUI surfaces all stop assuming repo-root `jobs.db`, `output/`, and material files. Verification: targeted path-resolution tests plus repo-mode regression tests stay green.
2. **Milestone 2:** First-run onboarding exists. A new user can provide a resume and supporting context via file upload, pasted text, or URL, and the app materializes canonical source files in user-owned storage. Verification: API tests, web onboarding flow tests, and a manual first-run smoke test.
3. **Milestone 3:** A packaged macOS app launches quickly and runs from the bundled resources without `uv`, repo-root artifacts, or first-launch worker churn. Verification: packaged smoke script, startup timing capture, and bundle-manifest checks.
4. **Milestone 4:** Documentation and verification are complete. Another engineer can build, run, and onboard from scratch using the repo alone. Verification: README/docs updates and standard repo verification commands all pass.

---

## Progress

| Step | Status | Updated |
|------|--------|---------|
| Research current runtime/package constraints | Completed | 2026-04-08 |
| Define path portability + packaged runtime architecture | Completed | 2026-04-08 |
| Write performance-first execution plan | Completed | 2026-04-08 |
| Implement runtime portability | Not started | |
| Implement onboarding + material ingestion | Not started | |
| Implement packaged launcher + build pipeline | Not started | |
| Run verification and package smoke tests | Not started | |

---

## File Structure

### New files:
- `docs/exec-plans/active/2026-04-08-performance-first-macos-app.md` — living implementation plan for the desktop productization work
- `scripts/app_paths.py` — single source of truth for bundle resources, user app-home paths, packaged-mode detection, and runtime directories
- `scripts/settings_store.py` — persistence for non-secret user settings and material metadata
- `scripts/credential_store.py` — secure provider credential storage and retrieval with redacted reads
- `scripts/material_ingest.py` — converts uploaded files, pasted text, or URLs into canonical local source materials
- `scripts/user_materials.py` — read/write helpers for `master_resume.md`, `work_stories.md`, `candidate_context.md`, and `application_profile.md`
- `scripts/desktop_launcher.py` — packaged app entrypoint that starts the local server and opens the desktop shell
- `packaging/pyinstaller/job_assets_web.spec` — PyInstaller build spec with explicit bundle contents and macOS bundle metadata
- `packaging/README.md` — local build, signing, and packaging instructions
- `tests/test_app_paths.py` — runtime path and packaged-mode coverage
- `tests/test_settings_store.py` — persisted settings and redacted settings API coverage
- `tests/test_credential_store.py` — provider credential storage coverage
- `tests/test_material_ingest.py` — upload/paste/URL ingestion coverage
- `tests/test_packaging_manifest.py` — bundle allowlist/exclusion coverage

### Modified files:
- `pyproject.toml` — add a `desktop` packaging dependency group and any launcher/runtime requirements
- `bin/job-assets` — shared path resolution, packaged-safe subprocess spawning, optional import/migration commands
- `scripts/job_web.py` — onboarding APIs, bootstrap endpoint, packaged-safe paths, lazy workers, startup perf instrumentation
- `scripts/static/index.html` — onboarding/settings/profile-management UI
- `scripts/static/app.js` — onboarding flow, bootstrap fetch, lazy dashboard hydration, client perf markers
- `scripts/static/style.css` — onboarding/profile UI styling
- `scripts/job_db.py` — database location from `scripts/app_paths.py`
- `scripts/output_layout.py` — output root from `scripts/app_paths.py`
- `scripts/project_env.py` — load env files from user app-home in packaged mode
- `scripts/llm_provider.py`, `scripts/openai_provider.py` — provider resolution through the credential/settings layer instead of raw repo-root env assumptions
- `scripts/browser_runtime.py` — browser cache/profile/runtime temp paths from `scripts/app_paths.py`
- `scripts/job_worker.py` — worker PID/log/state files from `scripts/app_paths.py`
- `scripts/job_tui.py` — path portability and packaged-safe worker spawning
- `scripts/job_assets_pipeline.py`, `scripts/pipeline_orchestrator.py`, `scripts/submit_application.py`, `scripts/run_pipeline.py` — packaged-safe script/process launch helpers
- `scripts/llm_common.sh` — source-material path resolution through app-home helpers instead of repo-root assumptions
- `scripts/answer_generation_support.py`, `scripts/application_models.py`, `scripts/application_submit_common.py`, `scripts/autofill_*.py`, `scripts/generate_interview_prep.py`, `scripts/greenhouse_preference_research.py`, `scripts/job_discovery.py`, `scripts/notion_job_applications.py`, `scripts/question_classifier.py`, `scripts/rank_bullets.py`, `scripts/render_resume_pdf.py`, `scripts/sync_master_resume.py`, `scripts/update_master_resume_outputs.py` — replace direct repo-root material lookups with helper-based resolution
- `tests/test_job_assets_cli.py`, `tests/test_job_web.py`, `tests/test_job_tui.py`, `tests/test_job_db.py`, `tests/test_output_layout.py`, `tests/test_project_env.py`, `tests/test_browser_runtime.py`, `tests/test_llm_provider.py`, `tests/test_openai_provider.py` — regression coverage for portability, onboarding, settings, and credentials
- `README.md`, `docs/shared-inputs.md`, `docs/launch-modes.md`, `docs/provider-setup.md`, `docs/output-structure.md` — user-facing desktop/onboarding/runtime docs

---

## Chunks & Tasks

### Chunk 1: Make Runtime State Portable and Fast

#### Task 1: Introduce a single runtime path contract

**Files:**
- Create: `scripts/app_paths.py`
- Create: `tests/test_app_paths.py`
- Modify: `scripts/job_db.py`
- Modify: `scripts/output_layout.py`
- Modify: `scripts/project_env.py`
- Modify: `scripts/browser_runtime.py`
- Modify: `bin/job-assets`

- [ ] **Step 1:** Add failing tests for repo mode, packaged mode, and explicit env overrides:
  - repo mode keeps current development behavior when `JOB_ASSETS_APP_HOME` is unset
  - packaged mode resolves `jobs.db`, `output/`, logs, browser state, and env files under the writable app-home
  - helper functions return stable paths for `jobs_db_path()`, `output_root()`, `materials_root()`, `logs_root()`, `tmp_root()`, and `browser_root()`
- [ ] **Step 2:** Implement `scripts/app_paths.py` with a small public surface:
  - `code_root()`
  - `app_home()`
  - `jobs_db_path()`
  - `output_root()`
  - `materials_root()`
  - `env_file_paths()`
  - `runtime_log_dir()`
  - `browser_state_dir()`
  - `is_packaged_runtime()`
- [ ] **Step 3:** Rewire `scripts/job_db.py`, `scripts/output_layout.py`, `scripts/project_env.py`, `scripts/browser_runtime.py`, and `bin/job-assets` to consume `scripts/app_paths.py` instead of `PROJECT_ROOT / "jobs.db"` or `PROJECT_ROOT / "output"` style globals.
- [ ] **Step 4:** Run:
  - `uv run python -m pytest tests/test_app_paths.py tests/test_project_env.py tests/test_output_layout.py tests/test_browser_runtime.py -v`
  - Expected: new path tests pass and existing repo-mode behavior remains intact.

#### Task 2: Remove package-hostile subprocess assumptions

**Files:**
- Modify: `bin/job-assets`
- Modify: `scripts/job_web.py`
- Modify: `scripts/job_tui.py`
- Modify: `scripts/job_worker.py`
- Modify: `scripts/job_assets_pipeline.py`
- Modify: `scripts/pipeline_orchestrator.py`
- Modify: `scripts/submit_application.py`
- Modify: `tests/test_job_assets_cli.py`
- Modify: `tests/test_job_web.py`
- Modify: `tests/test_job_tui.py`

- [ ] **Step 1:** Add failing tests that cover packaged-mode subprocess spawning:
  - web worker startup does not shell out through `uv`
  - TUI worker startup does not shell out through `uv`
  - CLI subprocess helpers use the embedded Python/runtime script path in packaged mode
- [ ] **Step 2:** Centralize subprocess command construction so packaged mode always uses `sys.executable` plus bundled scripts/resources, while repo mode can continue to use `uv` where useful for development.
- [ ] **Step 3:** Replace hardcoded `["uv", "run", ...]` launch paths in `scripts/job_web.py`, `scripts/job_tui.py`, and related pipeline helpers with the shared command builder.
- [ ] **Step 4:** Run:
  - `uv run python -m pytest tests/test_job_assets_cli.py tests/test_job_web.py tests/test_job_tui.py -v`
  - Expected: packaged-mode subprocess tests pass without regressing repo-mode commands.

#### Task 3: Make first paint cheap

**Files:**
- Modify: `scripts/job_web.py`
- Modify: `scripts/static/app.js`
- Modify: `scripts/static/index.html`
- Modify: `tests/test_job_web.py`

- [ ] **Step 1:** Add failing tests around bootstrap behavior:
  - initial app load can complete without queue hydration
  - onboarding state is returned without requiring worker startup
  - worker status and queue refresh are skipped until the user can actually use them
- [ ] **Step 2:** Add a lightweight `/api/bootstrap` response that returns:
  - whether onboarding is complete
  - current material presence summary
  - current provider/settings summary
  - worker-running flag without forcing heavy queue/detail work
- [ ] **Step 3:** Change the frontend startup path so:
  - onboarding/settings render first
  - queue/dashboard hydration is lazy
  - 5-second passive refresh starts only after onboarding is complete and the active view needs it
  - worker pool startup is explicit or deferred until a queue/submit action actually needs it
- [ ] **Step 4:** Run:
  - `uv run python -m pytest tests/test_job_web.py -v`
  - Expected: bootstrap tests pass and no endpoint regression appears in the existing web test suite.

### Chunk 2: Build New-User Onboarding Without Changing the Core Material Contract

#### Task 4: Create persisted settings and secure credential storage

**Files:**
- Create: `scripts/settings_store.py`
- Create: `scripts/credential_store.py`
- Create: `tests/test_settings_store.py`
- Create: `tests/test_credential_store.py`
- Modify: `scripts/project_env.py`
- Modify: `scripts/llm_provider.py`
- Modify: `scripts/openai_provider.py`
- Modify: `scripts/job_web.py`
- Modify: `tests/test_llm_provider.py`
- Modify: `tests/test_openai_provider.py`

- [ ] **Step 1:** Add failing tests for:
  - saving and loading non-secret settings such as material metadata and runtime defaults
  - redacted reads for secret-backed settings
  - storing provider credentials for OpenAI, Gemini, Codex, Claude, and Steel without exposing raw values in normal API responses
  - injecting stored credentials into provider execution paths at runtime
- [ ] **Step 2:** Implement a split settings model:
  - `scripts/settings_store.py` for non-secret persisted settings
  - `scripts/credential_store.py` for secret material
  - packaged macOS mode uses a Keychain-backed secret store when available
  - repo/local-web fallback can use an app-home env file only when secure OS storage is unavailable
- [ ] **Step 3:** Update provider resolution so API keys entered through Settings become the authoritative runtime source for OpenAI/Gemini and related providers, without requiring manual `.env.local` edits.
- [ ] **Step 4:** Run:
  - `uv run python -m pytest tests/test_settings_store.py tests/test_credential_store.py tests/test_llm_provider.py tests/test_openai_provider.py -v`
  - Expected: settings persist, credentials are redacted in normal reads, and provider execution receives the right secrets.

#### Task 5: Create canonical material ingestion

**Files:**
- Create: `scripts/material_ingest.py`
- Create: `scripts/user_materials.py`
- Create: `tests/test_material_ingest.py`
- Modify: `scripts/project_env.py`
- Modify: `scripts/sync_master_resume.py`
- Modify: `scripts/update_master_resume_outputs.py`

- [ ] **Step 1:** Add failing ingestion tests for:
  - raw markdown/text upload
  - `.docx` upload
  - `.pdf` upload
  - pasted text
  - public URL fetch
  - Google-Doc-style URL normalization where the content is publicly fetchable
- [ ] **Step 2:** Implement ingestion helpers that normalize those inputs into the existing canonical filenames:
  - `master_resume.md`
  - `work_stories.md`
  - `candidate_context.md`
  - `application_profile.md`
- [ ] **Step 3:** Keep the scope tight:
  - resume is required
  - work stories and candidate context are optional but importable
  - application profile can start from defaults plus web-form input
  - private Google auth flows and full cloud sync stay out of v1 unless already supported by existing repo primitives
- [ ] **Step 4:** Run:
  - `uv run python -m pytest tests/test_material_ingest.py tests/test_project_env.py -v`
  - Expected: every supported import path results in canonical material files under the user app-home.

#### Task 6: Replace direct repo-root material readers with helper-based resolution

**Files:**
- Modify: `bin/job-assets`
- Modify: `scripts/answer_generation_support.py`
- Modify: `scripts/application_models.py`
- Modify: `scripts/application_submit_common.py`
- Modify: `scripts/autofill_*.py`
- Modify: `scripts/generate_interview_prep.py`
- Modify: `scripts/greenhouse_preference_research.py`
- Modify: `scripts/job_assets_pipeline.py`
- Modify: `scripts/job_discovery.py`
- Modify: `scripts/llm_common.sh`
- Modify: `scripts/notion_job_applications.py`
- Modify: `scripts/prompts/interview_prep_system.md`
- Modify: `scripts/question_classifier.py`
- Modify: `scripts/rank_bullets.py`
- Modify: `scripts/render_resume_pdf.py`
- Modify: `scripts/run_pipeline.py`
- Modify: `tests/test_job_assets_pipeline.py`
- Modify: `tests/test_application_submit_common.py`
- Modify: `tests/test_job_discovery.py`

- [ ] **Step 1:** Sweep every direct `master_resume.md` / `work_stories.md` / `candidate_context.md` / `application_profile.md` read and convert it to a helper call from `scripts/user_materials.py`.
- [ ] **Step 2:** Update shell prompt assembly in `scripts/llm_common.sh` so generated prompts refer to resolved material paths instead of assuming repo-root file locations.
- [ ] **Step 3:** Add representative regression tests for at least:
  - submit-time answer generation
  - one autofill adapter path
  - interview prep
  - job discovery
  - pipeline content generation
- [ ] **Step 4:** Run:
  - `uv run python -m pytest tests/test_application_submit_common.py tests/test_job_assets_pipeline.py tests/test_job_discovery.py -v`
  - Expected: representative flows continue to use the canonical materials from the user app-home.

#### Task 7: Add a shared onboarding + settings page to the web UI and mac app shell

**Files:**
- Modify: `scripts/job_web.py`
- Modify: `scripts/static/index.html`
- Modify: `scripts/static/app.js`
- Modify: `scripts/static/style.css`
- Modify: `tests/test_job_web.py`

- [ ] **Step 1:** Add failing API tests for onboarding endpoints:
  - fetch onboarding status
  - upload/paste/import each canonical material
  - save application profile defaults
  - save and load provider settings
  - read redacted provider credential status
  - update provider credentials from the Settings page
  - import existing repo-root materials into app-home for the current user
- [ ] **Step 2:** Add backend endpoints that expose:
  - onboarding status
  - material metadata and last-updated source
  - upload/paste/url import actions
  - application-profile save/load
  - provider settings save/load
  - redacted credential status for OpenAI, Gemini, and other supported providers
  - first-run "import current repo materials" shortcut
- [ ] **Step 3:** Extend the current Settings view into a product-grade onboarding flow:
  - Welcome / choose import path
  - Resume import
  - Optional work stories import
  - Optional candidate context import
  - Application profile form
  - Provider credentials and provider defaults
  - Browser setup/help text
  - Post-onboarding editability for all of the above from the same Settings page
- [ ] **Step 4:** Manual verification:
  - launch the local web UI
  - simulate a brand-new app-home
  - complete onboarding without touching Terminal or raw files
  - revisit Settings after onboarding and edit materials plus provider credentials
  - verify canonical material files land in the app-home and the dashboard unlocks afterward

### Chunk 3: Package It as a Real macOS App Without Shipping Artifacts

#### Task 8: Add a desktop launcher and packaged-mode entrypoint

**Files:**
- Create: `scripts/desktop_launcher.py`
- Modify: `pyproject.toml`
- Modify: `bin/job-assets`
- Modify: `tests/test_job_assets_cli.py`

- [ ] **Step 1:** Add a launcher entrypoint that:
  - creates/validates the writable app-home
  - starts the local FastAPI server against packaged resources
  - opens the desktop shell
  - shuts down cleanly without corrupting SQLite WAL state
- [ ] **Step 2:** Decide and keep scope firm:
  - v1 ships a packaged launcher app plus the current local web UI surface
  - do not re-platform the app around a second frontend stack
  - if a native webview wrapper is added, keep the HTTP backend and static app unchanged behind it
- [ ] **Step 3:** Ensure the packaged entrypoint never depends on a git checkout, repo-relative static files, or repo-root writable paths.
- [ ] **Step 4:** Run:
  - `uv run python -m pytest tests/test_job_assets_cli.py tests/test_job_web.py -v`
  - Expected: launcher path selection and packaged entrypoint logic are covered.

#### Task 9: Build a whitelist-only bundle manifest

**Files:**
- Create: `packaging/pyinstaller/job_assets_web.spec`
- Create: `tests/test_packaging_manifest.py`
- Modify: `packaging/README.md`

- [ ] **Step 1:** Add a failing packaging-manifest test that asserts the bundle spec includes only approved resources and excludes:
  - `output/`
  - `submit/`
  - `jobs.db*`
  - `logs/`
  - `tmp/`
  - `.playwright-*`
  - `.uv-cache`
  - `.venv`
  - coverage artifacts
  - local env files
- [ ] **Step 2:** Write the PyInstaller spec as an allowlist:
  - code modules
  - static frontend assets
  - docs/templates actually needed at runtime
  - icons/plist metadata
  - no generated artifacts copied from the current working tree
- [ ] **Step 3:** Add a reproducible local build command and output location docs to `packaging/README.md`.
- [ ] **Step 4:** Run:
  - `uv run python -m pytest tests/test_packaging_manifest.py -v`
  - Expected: manifest exclusions are enforced in test coverage before any build is shipped.

#### Task 10: Tune packaged startup for speed

**Files:**
- Modify: `scripts/job_web.py`
- Modify: `scripts/desktop_launcher.py`
- Modify: `scripts/browser_runtime.py`
- Modify: `scripts/static/app.js`
- Modify: `tests/test_job_web.py`
- Modify: `tests/test_browser_runtime.py`

- [ ] **Step 1:** Move non-essential imports behind endpoint or action boundaries where startup currently pulls in heavy pipeline modules too early.
- [ ] **Step 2:** Keep browser installation/bootstrap out of the first-run critical path:
  - prefer a locally installed Chrome when present
  - defer Playwright browser installation to explicit browser setup or first browser-required action
  - cache any installed browser state under the user app-home
- [ ] **Step 3:** Keep worker startup lazy and non-blocking:
  - packaged app opens the UI immediately
  - workers start only when the user enters a workflow that needs them or explicitly starts them
  - worker status fetches stay best-effort and never block render
- [ ] **Step 4:** Capture and record cold-start/warm-start timings in a lightweight benchmark or startup log and compare them against the performance budgets in this plan.

### Chunk 4: Verify, Document, and Hand Off

#### Task 11: Finish regression coverage and repo verification

**Files:**
- Modify: `README.md`
- Modify: `docs/shared-inputs.md`
- Modify: `docs/launch-modes.md`
- Modify: `docs/provider-setup.md`
- Modify: `docs/output-structure.md`
- Modify: `docs/exec-plans/active/2026-04-08-performance-first-macos-app.md`

- [ ] **Step 1:** Update docs to describe:
  - packaged runtime vs repo runtime
  - user app-home layout
  - onboarding paths
  - provider setup for a new user
  - browser setup expectations
  - packaging/build workflow
- [ ] **Step 2:** Run the standard verification suite:
  - `uv run python -m pytest tests/ -v`
  - `uv run ruff check scripts/ tests/`
  - `uv run python scripts/check_architecture.py`
  - `uv run python scripts/sync_agent_files.py --check`
  - `uv run python scripts/check_agent_docs.py`
- [ ] **Step 3:** Run a packaged smoke test from a clean app-home and record:
  - first launch time
  - onboarding completion time
  - subsequent warm launch time
  - proof that writes land in app-home instead of the app bundle
- [ ] **Step 4:** Update this plan's `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` sections with actual results and remaining follow-ups.

---

## Surprises & Discoveries

- The web UI already has a Settings surface and a paginated queue API, so onboarding can extend the existing product shell instead of introducing a second frontend.
- Browser profile state is already user-scoped in `scripts/browser_runtime.py`; that is the right precedent for moving the rest of runtime state out of the repo root.
- The broad portability problem is larger than just `jobs.db` and `output/`. A repo-wide sweep found many direct reads of `master_resume.md`, `work_stories.md`, `candidate_context.md`, and `application_profile.md`, plus several hardcoded `jobs.db` references across CLI, web, worker, and repair flows.
- Startup performance will regress badly if packaged mode keeps the current "import everything and maybe start workers immediately" posture. Lazy bootstrap and lazy workers are required, not optional polish.
- API-key entry needs a real secret-storage plan. A Settings page is the right UX, but credentials cannot live in browser storage or plain response payloads.

---

## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-04-08 | Use the existing FastAPI + static web UI as the desktop product surface. | It already covers queue, review, settings, and job detail workflows, so re-platforming to a new frontend stack would add risk without helping the packaged product ship faster. |
| 2026-04-08 | Keep the canonical material filenames but move them into a user-owned app-home. | This minimizes changes to the generation/submission pipeline while making onboarding and packaging possible. |
| 2026-04-08 | Treat bundle contents as an allowlist, not a blacklist. | The requirement is "package the repo minus artifacts"; explicit allowlisting is the only reliable way to prevent shipping current databases, logs, screenshots, and output trees. |
| 2026-04-08 | Optimize for startup by deferring workers, browser bootstrap, and heavy imports until the user actually needs them. | The user explicitly asked for extreme performance, and desktop-app perception is dominated by first paint and readiness latency. |
| 2026-04-08 | Keep repo mode working while adding packaged mode. | The existing development workflow remains valuable and should not be broken just to support the desktop bundle. |
| 2026-04-08 | Use one shared Settings surface for onboarding, material editing, and provider credentials in both the web app and packaged app. | The user explicitly wants materials and API keys editable from Settings; a single surface avoids duplicated flows and keeps onboarding as a first-run state of the same product area. |

---

## Outcomes & Retrospective

- **Achieved:** Plan written for a performance-first macOS packaging/onboarding effort covering runtime portability, onboarding, bundle manifesting, packaged startup, and repo verification.
- **Remaining:** Implement the plan in order, starting with `scripts/app_paths.py` and packaged-safe subprocess spawning before any onboarding UI work.
- **Lessons:** The hard part is not generating a `.app`; it is separating code resources from user-owned runtime state and doing it without regressing existing repo workflows or startup performance.
