# Architecture

This document describes the high-level architecture of the job application
automation system. Read this to orient before making changes.

## Bird's-Eye View

Job application automation: scrape job descriptions, generate tailored resumes
and cover letters, autofill application forms across 19 job boards.

## Entry Points

- `scripts/run_pipeline.py` -- CLI: JD -> resume + cover letter + submit
- `scripts/job_tui.py` -- TUI (Textual)
- `scripts/job_web.py` -- Web UI (FastAPI, local-only)
- `scripts/job_worker.py` -- Background workers
- `scripts/mac_app_launcher.py` -- Packaged macOS app launcher around the local web UI
- `scripts/draft_web.py` -- Draft review FastAPI app (local server + optional tunnel)

## Module Map

### Data Layer
- `job_db.py` -- SQLite, raw SQL, no ORM
- `project_env.py` -- Load local, gitignored environment files
- `output_layout.py` -- Per-role output directory organization

### Runtime Settings & User Context
- `app_paths.py` -- Resolve repo-root vs packaged runtime-home paths
- `settings_store.py` -- Shared materials, provider, and credential settings backend
- `web_settings_api.py` -- Shared FastAPI onboarding/settings routes for the web and packaged app surfaces
- `material_ingest.py` -- Import and normalize text, document, PDF, HTML, and public URL source materials
- `candidate_runtime.py` -- Derive runtime-facing display defaults from the active candidate materials
- `runtime_policy.py` -- Policy-as-code gate for shared runtime actions
- `runtime_trace.py` -- Redacted JSONL trace processors plus startup-time processor registration for runtime audit events
- `runtime_entrypoints.py` -- Dispatch internal Python entrypoints safely in packaged mode

**Architecture Invariant:** the packaged app, web app, CLI, and TUI must all
resolve user materials and provider credentials through the same runtime-home
and settings backend. No surface-specific shadow copies.

**Architecture Invariant:** packaged mode must not rely on writable repo-root
state. Worker PID files, logs, traces, browser state, credentials, and
canonical user materials belong under the runtime home from `app_paths.py`.

### Pipeline Orchestration
- `pipeline_orchestrator.py` -- Core job processing: resolve -> generate -> submit -> fix -> retry -> post-submit
- `job_assets_pipeline.py` -- Single-job asset pipeline (generate + submit automation)
- `asset_pipeline_state.py` -- Reusable content/build state for pipeline reruns
- `draft_manager.py` -- Draft mode: summary generation, diff detection, override management

### Scraping & Parsing
- `scrape_job.py` -- Scrape job posting URLs, extract structured content
- `parse_jd.py` -- Parse raw JD text into structured JSON

### Resume Generation
- `rank_bullets.py` -- TF-IDF cosine similarity ranking of master resume bullets (no LLM)
- `draft_resume.py` -- Draft resume_content.json from ranked bullets
- `build_resume.py` -- Deterministic .docx builder (pixel-matches Google Doc template)
- `render_resume_pdf.py` -- Render master_resume.md into two-page PDF
- `resume_layout.py` -- Page-break heuristics
- `optimize_page_break.py` -- Choose page_break_before after content is finalized
- `enforce_resume_policy.py` -- Deterministic content policies (e.g., minimum bullet counts)
- `validate_resume.py` -- Validate built resume PDF for 2-page constraint
- `sync_master_resume.py` -- Sync master resume Google Doc export to local markdown
- `build_draft_summary.py` -- Render draft_summary.png from markdown via Pillow

### Cover Letter Generation
- `build_cover_letter.py` -- Deterministic .docx + .txt builder

### Interview Prep
- `generate_interview_prep.py` -- Interview preparation content generation

### LLM Layer
- `llm_provider.py` -- LLM abstraction (OpenAI, Claude, Gemini)
- `openai_provider.py` -- Subprocess shim for OpenAI Responses API

### Autofill Composition
- `autofill_common.py` -- Shared browser utilities (label matching, screenshots)
- `autofill_pipeline.py` -- Shared orchestration (autofill_main)
- `autofill_{board}.py` -- 19 board-specific handlers: ashby, bamboohr, comeet, dover, eightfold, email, gem, greenhouse, icims, lever, linkedin, motionrecruitment, phenom, reducto, rippling, smartrecruiters, uber, workable, workday

**Architecture Invariant:** autofill_common must NOT import from pipeline
or any board. Pipeline must NOT import from any board. Board scripts must
NOT import from other boards. Each board handler is independent -- shared
logic lives in autofill_common.py. Enforced by check_architecture.py (CI).

### Submission
- `application_submit_common.py` -- Shared submit logic (profile, LLM answers)
- `question_classifier.py` -- Unified question classification
- `submit_application.py` -- Submission orchestration

**Architecture Invariant:** application_submit_common must NOT import from
any board module. Question classification is centralized -- board scripts
must not inline their own classifiers.

### URL & Board Detection
- `job_board_urls.py` -- Board URL detection and wrapper-resolution helpers
- `url_resolver.py` -- Aggregator-to-board resolution (LinkedIn, Indeed, Glassdoor redirect following)

### Browser Infrastructure
- `browser_runtime.py` -- Shared Playwright browser-launch helpers
- `setup_browser_profile.py` -- Open persistent Playwright profile for manual sign-in

### Job Discovery & Import
- `job_discovery.py` -- Search, score, and promote candidate jobs
- `import_linkedin_saved.py` -- Import saved jobs from LinkedIn into pipeline

### Sync
- `notion_sync.py` -- Notion <-> SQLite sync
- `notion_job_applications.py` -- Upsert applied roles into Notion Job Applications database

### Utilities
- `json_lenient.py` -- Lenient JSON parsing (handles LLM trailing commas)
- `entrypoint_guard.py` -- Shared guardrails for repo entrypoints launched from provider subtasks
- `run_command_with_timeout.py` -- Subprocess timeout wrapper

### CI & Tooling
- `check_architecture.py` -- Validate import direction constraints
- `check_agent_docs.py` -- Validate agent instruction files
- `sync_agent_files.py` -- Generate CLAUDE.md, GEMINI.md, CODEX.md, GPT.md, and Copilot instructions from AGENTS.md
- `build_mac_app.py` -- Build the macOS `.app` bundle with PyInstaller

## Agent Instruction System

- `AGENTS.md` -- canonical agent prompt (source of truth, ~80-100 lines)
- `CLAUDE.md`, `GEMINI.md`, `CODEX.md`, `GPT.md` -- generated copies (sync_agent_files.py)
- `.github/copilot-instructions.md` -- generated copy for GitHub Copilot
- `agent_preferences.md` -- behavioral defaults and learned corrections (all providers)
- `docs/` -- progressive disclosure (detailed reference loaded on-demand)
- `docs/exec-plans/active/` and `docs/exec-plans/completed/` -- living and archived execution plans for multi-step work
- `docs/harness-governance.md` -- repo-level approval, durable-memory, and release-gate contract
- `docs/registries/` -- agent, tool, and prompt registries for authority and rollback visibility

**Architecture Invariant:** AGENTS.md is the only instruction file edited directly.
All provider files are generated. agent_preferences.md is the only file for
behavioral defaults -- never duplicate preferences into AGENTS.md or doc files.
Enforced by sync_agent_files.py, check_agent_docs.py, and CI.

## Harness & Governance

- `docs/core-beliefs.md` captures the repo's golden principles for agent legibility.
- `docs/harness-governance.md` defines durable-memory requirements, risk tiers,
  and push/merge boundaries.
- `docs/registries/*.md` document the owned surfaces, tool authority, and
  prompt/control-plane lineage.
- `docs/exec-plans/*` are the resumable state for complex in-flight work.
- `governance/runtime-policy.json` is the runtime policy control plane for
  action tiers, required action metadata, and explicit-approval requirements.

**Architecture Invariant:** long-running work must remain resumable from repo
artifacts alone, and `L3` actions (live submit, push, merge, publication,
destructive operations) require explicit operator approval.

## Deliberate Absences

- No ORM -- raw SQL via job_db.py (deliberate: schema is simple, ORM overhead not justified)
- No package structure -- flat scripts/ directory (deliberate: all files are entry points or utilities)
- FastAPI is NOT a web service -- it is local-only, serving the TUI companion UI and draft review
