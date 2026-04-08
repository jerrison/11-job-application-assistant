# Job TUI, Worker, and CLI Parity — Design Spec

**Date:** 2026-03-15
**Status:** Draft

## Problem

The current system requires an LLM agent (Claude Code / Gemini CLI) to orchestrate the full job application lifecycle: provider fallback, error recovery, post-submit actions, and batch resilience. The CLI alone can't do this — it lacks autonomous orchestration. There is also no interactive UI for monitoring, adding jobs, or reviewing results during long-running sessions or overnight runs.

## Goals

1. **TUI** — Interactive Textual terminal app with dashboard, job queue, add-jobs wizard, and job detail views. Snappy navigation with zero perceived latency.
2. **Worker** — Long-running background process that autonomously processes jobs: provider fallback, retry with recording, auto-fix via LLM, post-submit lifecycle. Runs overnight unattended.
3. **CLI parity** — The `bin/job-assets` CLI gets new commands so it can do everything the TUI does.
4. **Optimizations** — Fix known bugs and reduce code duplication as part of this work.

## Non-Goals

- Splitting `autofill_greenhouse.py` (stable, high risk)
- Adding test coverage for Phenom/iCIMS/Workday (separate effort)
- Centralizing environment variables
- Building a web UI
- Changing which asset formats are generated

---

## Architecture

Three components sharing one SQLite database:

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  job-tui    │     │  job-worker  │     │ bin/job-assets│
│  (Textual)  │     │  (long-run)  │     │    (CLI)     │
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │
       └───────────┬───────┴───────────────────┘
                   │
            ┌──────▼──────┐
            │   jobs.db   │
            │  (SQLite)   │
            └─────────────┘
```

- **`jobs.db`** — Single SQLite file in the project root. All state lives here. WAL mode for concurrent reads.
- **`job-worker`** — Long-running Python process (runs in tmux/screen). Polls the DB for pending jobs, processes them with full autonomy. Writes results back to DB. The only process that mutates job state beyond `queued`.
- **`job-tui`** — Textual app. Reads DB to display views. Writes to DB to enqueue jobs, set priority, mark skipped. Polls DB every 1-2s for updates via background async worker. Closing the TUI does not stop the worker.
- **`bin/job-assets`** — Existing CLI enhanced with new commands. Reads/writes the same DB. Full feature parity with TUI.

**Key principle:** The worker is the only process that *processes* jobs. The TUI and CLI only *enqueue* and *read*. This eliminates race conditions on job processing.

---

## Database Schema

```sql
CREATE TABLE jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL,
    source          TEXT,
    source_url      TEXT,
    board_url       TEXT,
    canonical_url   TEXT UNIQUE,
    company         TEXT,
    role_title      TEXT,
    board           TEXT,
    status          TEXT NOT NULL DEFAULT 'queued',
    priority        INTEGER DEFAULT 0,
    provider        TEXT,
    output_dir      TEXT,
    notion_url      TEXT,
    error_message   TEXT,
    fix_attempts    INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at    TIMESTAMP
);

-- Trigger to auto-update updated_at on every row change.
-- SQLite has no ON UPDATE CURRENT_TIMESTAMP — this trigger provides it.
CREATE TRIGGER trg_jobs_updated_at AFTER UPDATE ON jobs
BEGIN
    UPDATE jobs SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TABLE events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs(id),
    event_type      TEXT NOT NULL,
    detail          TEXT,                    -- human-readable summary
    detail_json     TEXT,                    -- structured JSON for programmatic access
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE fix_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs(id),
    error_type      TEXT,
    error_context   TEXT,
    fix_diff        TEXT,
    fix_branch      TEXT,
    tests_passed    BOOLEAN,
    applied         BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE provider_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs(id),
    provider        TEXT NOT NULL,
    phase           TEXT,
    exit_code       INTEGER,
    duration_ms     INTEGER,
    error_message   TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_jobs_board ON jobs(board);
CREATE INDEX idx_jobs_source ON jobs(source);
CREATE INDEX idx_jobs_created ON jobs(created_at);
CREATE INDEX idx_events_job_id ON events(job_id);
CREATE INDEX idx_events_type ON events(event_type);
CREATE INDEX idx_fix_attempts_job_id ON fix_attempts(job_id);
CREATE INDEX idx_provider_runs_job_id ON provider_runs(job_id);
```

### Status Lifecycle

```
queued → resolving → generating → submitting → submitted
            │             │            │
            │             │            ├→ fix_in_progress → (retry submitting)
            │             │            ├→ retrying (recorded final attempt)
            │             │            ├→ skipped_captcha
            │             │            ├→ skipped_auth
            │             │            └→ needs_manual
            │             │
            │             └→ failed (all providers exhausted)
            │
            ├→ needs_board_url (unresolvable aggregator URL)
            └→ failed (resolution error)
```

Direct board URLs skip `resolving` and go straight from `queued` to `generating`.

### Supported Boards

The worker must handle all boards supported by `_board_for_url()` in `submit_application.py`: Greenhouse, Ashby, Lever, Gem, Dover, Workday, Phenom, iCIMS. The `board` column stores these values.

### URL Input Handling

| User pastes | `source` | `source_url` | `board_url` | What happens |
|-------------|----------|-------------|-------------|-------------|
| Board URL (Greenhouse, Phenom, etc.) | `direct` | `null` | Set immediately | Process right away |
| LinkedIn URL | `linkedin` | Set | Auto-resolved via redirect | Process after resolution |
| Indeed URL | `indeed` | Set | Auto-resolved via redirect | Process after resolution |
| Aggregator that can't resolve | Detected | Set | `null` | Status = `needs_board_url` |

### Event Types

`status_change`, `provider_fallback`, `provider_success`, `provider_failure`, `fix_attempt`, `fix_applied`, `fix_failed`, `retry`, `retry_recorded`, `notion_synced`, `email_replied`, `url_resolved`, `url_resolution_failed`

---

## Worker Design

### Processing Loop

```
job-worker (runs forever in tmux)
│
├─ Worker Pool (configurable, default 3 concurrent)
│   ├─ Worker 1: processing job A (own browser profile: .profiles/worker-1/)
│   ├─ Worker 2: processing job B (own browser profile: .profiles/worker-2/)
│   └─ Worker 3: processing job C (own browser profile: .profiles/worker-3/)
│
├─ Coordinator thread
│   ├─ Polls DB every 5s for pending jobs
│   ├─ Assigns to available workers
│   ├─ Rate limits: configurable per-board concurrency (default 1, Greenhouse can be higher
│   │   since each company's career page is independent infrastructure)
│   └─ Prioritizes: highest priority first, then oldest
│
└─ Each worker runs the full lifecycle:
```

**Browser profile isolation:** Each worker gets its own persistent browser profile directory (`{project_root}/.profiles/worker-{N}/`). This prevents Chromium profile locking conflicts when multiple workers run simultaneously. The existing `submit_browser_profile_dir()` in `browser_runtime.py` is extended to accept a worker ID.

### Job Lifecycle (per worker)

```
Phase 1: URL Resolution
├─ Detect source from URL domain
├─ If aggregator → follow redirects, scrape for board URL
├─ If unresolvable → status = 'needs_board_url', return
└─ Canonicalize board URL via job_board_urls.py

Phase 2: Asset Generation
├─ Run pipeline: scrape JD → parse → rank bullets → build resume + cover letter
├─ Provider fallback chain: gemini → gemini-flash → claude → codex
├─ If all providers fail → status = 'failed', log error, return
└─ Write assets to output/{company}/{role}/

Phase 3: Submit
├─ Detect board, run autofill_{board}.py
├─ Handle known issues:
│   ├─ Captcha → status = 'skipped_captcha'
│   ├─ Auth failure → status = 'skipped_auth'
│   └─ Already applied → status = 'submitted'
└─ If novel error → Phase 4

Phase 4: Auto-Fix (max 3 attempts per error)
├─ Capture: error message, page HTML, screenshot, stack trace
├─ Invoke LLM CLI for fix (requires `claude` on PATH; skip Phase 4 if unavailable)
├─ Apply fix to git branch, run tests
├─ If tests pass → merge to main, go to Phase 3 retry
├─ If tests fail → discard branch, log attempt
└─ After 3 failures → Phase 5

Phase 5: Retry with Recording (final attempt before giving up)
├─ Enable Playwright trace recording
├─ Step-by-step screenshots at every action
├─ Human-readable action_log.md
├─ Page HTML snapshots at each step
├─ Console errors captured
├─ Save all to output/{company}/{role}/submit/debug_recording/
│   ├─ trace.zip
│   ├─ action_log.md
│   ├─ step_01_navigate.png
│   ├─ step_02_fill_email.png
│   ├─ ...
│   ├─ step_XX_error.png
│   └─ page_snapshots/
└─ status = 'needs_manual'

Phase 6: Post-Submit (on success)
├─ Sync to Notion
├─ Reply to confirmation email with screenshot + report
└─ status = 'submitted'
```

### Action Log Format

```markdown
# Submission Recording — Circle TradeFi
- URL: https://careers.circle.com/...
- Board: Phenom
- Recorded: 2026-03-15 03:42:17 UTC
- Duration: 47.3s

## Step 1: Navigate (0.0s)
Loaded application page. Title: "Apply — Principal PM, TradeFi"
Screenshot: step_01_navigate.png

## Step 2: Fill Email (1.2s)
Field: input#emailAddress → "jerrisonli@gmail.com" ✓
Screenshot: step_02_fill_email.png

...

## Step 14: Fill Role Description (12.4s)
Field: textarea#experienceData[0].description → "• Led product strategy..."
⚠ WARNING: Field shows validation error after fill
Screenshot: step_14_fill_description.png

## Step 15: Click Next (14.1s)
Button: "Next" clicked
❌ FAILED: Page did not advance. Validation errors present.
Screenshot: step_15_error.png
Page HTML: page_snapshots/step_15.html
Console errors: ["Uncaught TypeError: Cannot read property..."]
```

### Auto-Fix Prompt Template

When invoking `claude` CLI for auto-fix:

```
Fix this autofill error in {board} board script.

Error: {error_message}
Traceback: {traceback}
Page HTML (relevant section): {html_snippet}
Screenshot: {screenshot_path}

The autofill script is at: scripts/autofill_{board}.py
Shared utilities: scripts/autofill_common.py

Requirements:
1. Fix the specific error — do not refactor unrelated code
2. Run tests: uv run python -m pytest tests/ -v
3. If tests pass, commit the fix
4. If tests fail, revert and explain what went wrong
```

### Auto-Fix Provider

Phase 4 requires `claude` CLI on PATH. If unavailable, Phase 4 is skipped entirely and the job proceeds directly to Phase 5 (retry with recording). The auto-fix provider is separate from the asset generation provider chain — auto-fix always uses `claude` because it needs the ability to read files, run tests, and commit code in a single agentic session.

### Crash Recovery

- If the worker crashes mid-job, jobs remain in `generating`/`submitting`/`fix_in_progress` status
- On restart, the coordinator detects stale in-progress jobs (updated_at older than `WORKER_STALE_THRESHOLD_SECONDS`, default 1800 / 30 minutes) and resets them to `queued` for reprocessing. This threshold exceeds the maximum LLM provider timeout (1200s in `llm_provider.py`) to avoid resetting legitimate long-running jobs.
- SQLite WAL mode ensures DB consistency even on crash

---

## TUI Design

### Performance Requirements

- **No DB queries on keypress** — job data cached in memory, refreshed by background async worker every 1-2s
- **Virtualized lists** — only render visible rows (Textual DataTable handles this)
- **Lazy tab content** — Resume/Screenshot/Recording tabs load content only when selected
- **Debounced search** — filter input waits 200ms after last keystroke before querying
- **Async file loading** — images, PDFs, large text files loaded off main thread

### View 1: Dashboard (`D` key)

- **Summary bar** — counts by status: Submitted, Processing, Queued, Failed, Needs Attention, Skipped
- **Worker status** — indicator showing worker running/stopped, active worker count
- **Recent activity** — scrollable list of latest events from events table, auto-refreshes
- **By-board breakdown** — progress bars per board showing completion percentage

### View 2: Job Queue (`Q` key)

- **Sortable table** — columns: Status, Company, Role, Board, Source, Provider, Created
- **Filters** — dropdown for status, board, source; free-text search for company/role
- **Actions on selected job:**
  - `Enter` — open Job Detail
  - `R` — retry
  - `S` — skip
  - `P` — bump priority
  - `Delete` — remove from queue

### View 3: Add Jobs (`A` key)

- **Multi-line text input** — paste one or many URLs (one per line or comma-separated)
- **Provider selector** — Auto (use fallback chain) or force specific provider
- **Priority selector** — Normal, High, Urgent
- **Instant feedback** — after adding, shows "N jobs added — breakdown by board"
- Auto-detects source from URL domain

### View 4: Job Detail (`Enter` on any job in Queue)

- **Header** — company, role, status, board, source, provider
- **URLs** — source URL, board URL, canonical URL (shown if different)
- **Links** — output directory, Notion page
- **Timeline** — scrollable chronological list from events table
- **Tabbed content area** (navigate with `←` `→` or `Tab`):

| Tab | Content | When available |
|-----|---------|---------------|
| **Report** | Autofill report — filled fields table with field name, value, source | After submission attempt |
| **Screenshot** | Pre-submit PNG — rendered inline via `textual-image` widget (supports Kitty/iTerm2/Sixel protocols), degrades to "open externally" on `Enter` for unsupported terminals | After submission attempt |
| **Recording** | Action log markdown (scrollable) + `Enter` to open Playwright trace viewer | Failed jobs with recorded retry |
| **Resume** | Format picker: `[MD] [PDF] [DOCX]` — MD rendered inline, PDF/DOCX opened externally | After asset generation |
| **Cover Letter** | Format picker: `[MD] [PDF] [DOCX] [TXT]` — MD/TXT rendered inline, PDF/DOCX opened externally | After asset generation |

The format picker scans `output/{company}/{role}/documents/` and only shows buttons for files that exist.

### Keybindings

| Key | Action |
|-----|--------|
| `D` | Dashboard view |
| `Q` | Queue view |
| `A` | Add jobs |
| `Enter` | Job detail (from queue) |
| `Esc` | Back |
| `R` | Retry selected job |
| `S` | Skip selected job |
| `P` | Prioritize selected job |
| `W` | Toggle worker start/stop |
| `/` | Search/filter |
| `?` | Help |
| `←` `→` | Switch tabs in Job Detail |
| `Ctrl+C` | Quit TUI (worker keeps running) |

---

## CLI Parity

New commands added to `bin/job-assets`:

| Command | Description | TUI equivalent |
|---------|-------------|----------------|
| `job-assets add <url> [url...]` | Queue one or more jobs | Add Jobs view |
| `job-assets queue [--status X] [--board Y] [--source Z]` | List jobs with filters | Queue view |
| `job-assets status <job_id>` | Show job detail + event timeline | Job Detail view |
| `job-assets retry <job_id>` | Retry a failed job | `R` key |
| `job-assets skip <job_id>` | Mark as skipped | `S` key |
| `job-assets prioritize <job_id>` | Bump priority | `P` key |
| `job-assets worker start [--workers N]` | Start worker in background | `W` key |
| `job-assets worker stop` | Stop worker gracefully | `W` key |
| `job-assets worker status` | Show worker health + active jobs | Dashboard indicator |
| `job-assets report [--board X] [--status Y] [--since DATE]` | Query and export job data | Queue filters |
| `job-assets tui` | Launch TUI | — |

Existing commands (`pipeline`, `submit`, `batch`, `parallel`, `doctor`, `profile`, `notion-sync`, `install`, `man`) remain unchanged.

---

## New File Structure

```
scripts/
├─ pipeline_orchestrator.py    (NEW — shared processing brain)
│   ├─ resolve_url()           — source detection + redirect following
│   ├─ process_job()           — full lifecycle: resolve → generate → submit → post-submit
│   ├─ provider_fallback()     — try provider chain, return first success
│   ├─ retry_with_recording()  — final attempt with Playwright trace + action log
│   └─ auto_fix()              — invoke claude CLI, branch, test, merge/discard
│
├─ job_db.py                   (NEW — SQLite interface)
│   ├─ init_db()               — create tables if not exist
│   ├─ add_job()               — insert with source detection
│   ├─ get_pending_jobs()      — priority-ordered, respecting rate limits
│   ├─ update_status()         — transition + auto-update updated_at
│   ├─ log_event()             — append to events table
│   ├─ log_fix_attempt()       — append to fix_attempts table
│   ├─ log_provider_run()      — append to provider_runs table
│   ├─ query_jobs()            — filtered queries for TUI/CLI
│   └─ get_job_timeline()      — events for a specific job
│
├─ job_worker.py               (NEW — worker pool)
│   ├─ WorkerPool              — manages N concurrent workers
│   ├─ Coordinator             — polls DB, assigns jobs, rate limits by board
│   ├─ Worker                  — runs process_job() in own thread
│   └─ main()                  — entry point for `job-assets worker start`
│
├─ job_tui.py                  (NEW — Textual app)
│   ├─ JobApp(App)             — main Textual application
│   ├─ DashboardView(Screen)   — summary, activity feed, board breakdown
│   ├─ QueueView(Screen)       — sortable/filterable job table
│   ├─ AddJobsView(Screen)     — URL input + options
│   ├─ JobDetailView(Screen)   — timeline + tabbed content
│   └─ main()                  — entry point for `job-assets tui`
│
├─ url_resolver.py             (NEW — aggregator URL resolution)
│   ├─ detect_source()         — classify URL as linkedin/indeed/direct/unknown
│   ├─ resolve_to_board_url()  — follow redirects, scrape apply links
│   └─ SOURCE_PATTERNS         — regex patterns for known aggregators
│
│   Note: url_resolver.py handles aggregator-to-board resolution (LinkedIn → Greenhouse).
│   Board-specific URL detection remains in job_board_urls.py (looks_like_greenhouse_url, etc.)
│   and submit_application.py (_board_for_url). url_resolver wraps these existing functions.
```

---

## Optimizations (bundled)

| Issue | Fix |
|-------|-----|
| `job_assets_pipeline.py` hardcodes provider list, rejects `gemini-flash` | Replace with `VALID_PROVIDERS` import |
| Greenhouse reimplements `parse_application_profile()` | Import from `application_submit_common.py` |
| Provider fallback is manual (`--provider claude`) | Built into `provider_fallback()` in orchestrator |
| Post-submit lifecycle (Notion + email) inconsistent | Standard step in `process_job()` |
| Batch resilience — failures can stall processing | Worker always moves on, logs everything |

Note: Each board's `_build_payload()` is intentionally board-specific (different signatures, different fields). The shared preamble (loading meta, profile, JD) is already extracted to `autofill_pipeline.py`'s `autofill_main()`. No further extraction needed.

---

## Migration

### Importing Existing Jobs

A `job-assets import` command scans existing `output/` directories and populates the database with historical jobs. It reads `application_submission_result.json` files to determine status, board, provider, and timestamps. This is a one-time migration — after import, all new jobs go through the queue.

```
job-assets import [--output-dir PATH]   # default: output/
```

---

## Dependencies

New Python packages:

- `textual` — TUI framework
- `textual-image` — inline image rendering for Screenshot tab (degrades gracefully)
- `aiofiles` — async file reading for TUI (optional, can use Textual workers)

No other new dependencies. SQLite is built into Python.
