# Job Application Web UI — Design Spec

**Date:** 2026-03-16
**Status:** Draft

## Overview

Replace the Textual TUI with a high-performance web UI for the job application pipeline. FastAPI backend with WebSocket for real-time updates, vanilla JS frontend with zero framework overhead. Accessible from local network (phone/tablet review).

## Motivation

The Textual TUI has inherent performance issues: the Python event loop, thread workers, and key input all share one process. DB refresh cycles cause dropped keystrokes and UI jank. A web UI separates concerns — the server handles data, the browser handles rendering — eliminating the event loop contention.

## Architecture

### Backend: FastAPI (async)

Two concerns:
- **REST API** — CRUD for jobs, actions (approve/reject/retry/skip), bulk URL add, worker control
- **WebSocket** — real-time push of job status changes, progress updates, worker events

### Frontend: Single HTML Page + Vanilla JS

No framework, no build step. Served as static files by FastAPI.
- `scripts/static/index.html` — single page with all views
- `scripts/static/app.js` — DOM updates, WebSocket, hash routing
- `scripts/static/style.css` — all styles

### Data Flow

1. Browser connects via WebSocket on page load
2. Server polls SQLite every 2 seconds (server-local, not browser round-trips)
3. Server keeps a hash of last-sent state per WebSocket client
4. Only sends jobs whose `updated_at` changed since last push (diff-based)
5. 0 jobs processing → 0 messages sent (silent)
6. User actions (approve, edit, add) go via REST POST
7. Server broadcasts the update to all connected WebSocket clients

### Worker Management

Same subprocess approach as TUI:
- FastAPI starts/stops the worker subprocess
- Worker writes to SQLite
- Server detects changes and pushes to browser

### Launch

`job-assets web` starts the FastAPI server, prints URL, optionally opens browser.
`job-assets web --host 0.0.0.0` makes it LAN-accessible for phone/tablet.

## WebSocket Protocol

Server sends JSON messages with a `type` field:

```json
{"type": "job_update", "job": {"id": 3, "status": "generating", "progress": "Researching company...", ...}}
{"type": "job_bulk", "jobs": [...]}
{"type": "stats_update", "stats": {"total": 47, "submitted": 35, ...}}
{"type": "worker_status", "running": true}
```

### Inline Edit Conflict Resolution

If the user is editing a field and a WebSocket update arrives for that job, the UI:
- Skips updating the field being edited (preserves in-progress edit)
- Shows a small indicator that the server version changed
- User can accept server version or keep their edit

## Views (Hash Routing)

All views in a single HTML page. Navigation swaps visible sections. URL hash routing: `#queue`, `#job/3`, `#add`, `#dashboard`, `#stats`.

### 1. Queue (`#queue`, default)

- **Status badges at top** — clickable count filters: Queued (N) | Processing (N) | Draft (N) | Submitted (N) | Failed (N). Click to filter. Click again to clear.
- **Job table below** — columns: ID, Status + Progress, Company, Role, Board, Provider, Created
- Click a row → navigates to `#job/:id`
- **Bulk actions** — checkbox column, select multiple → Retry All / Skip All / Delete buttons
- **Search** — text input filters by company/role
- **Board filter** — dropdown

### 2. Job Detail (`#job/:id`)

- **Sticky header** — Company — Role | Status badge | Board | Provider
- **Action buttons** — Approve + Submit | Reject | Regenerate | Retry (contextual based on status)
- **Board URL input** — shown only when status is `needs_board_url`
- **Progress bar** — shown when generating/submitting, reads from `progress` DB field and `.progress.json`
- **Tabbed content** (default: Answers tab):

  **Answers tab:**
  - Lists all application form fields with labels, values, sources
  - Each field is inline-editable: click or press Enter to edit
  - Short text → input, long text → textarea, yes/no → toggle, select → dropdown
  - Edited fields get yellow highlight, original value shown as strikethrough
  - Staged changes shown in sticky bottom bar: "3 changes — [Review Changes] [Discard All]"

  **Resume tab:**
  - Tagline, summary, bullets per company (structured from resume_content.json)
  - Inline-editable

  **Cover Letter tab:**
  - Full text from cover_letter_text.txt
  - Inline-editable

- **Timeline** — collapsible section showing event history
- **Error display** — red banner when error_message is set

### 3. Add Jobs (`#add`)

- Large textarea for pasting URLs (one per line or comma-separated)
- Provider selector (Auto | gemini | claude | codex)
- Priority selector (Normal | High | Urgent)
- Submit button (also Ctrl+Enter)
- Feedback: "5 jobs added, 1 duplicate skipped"

### 4. Dashboard (`#dashboard`)

- **Summary stats** — two rows:
  - Workers ON/OFF | Total | Submitted | Processing | Queued | Drafts
  - Failed | Attention | Skipped | Error Rate | Avg Duration
- **Activity feed** — recent events across all jobs
- **Board breakdown** — per-board submission progress bars

### 5. Stats (`#stats`)

- **Summary panel** — jobs processed in 1h/24h/7d/all, success/failure/intervention rates
- **Per-job metrics table** — sortable: ID, Company, Role, Duration, Error Rate, Fixes, Interventions
- **Phase breakdown** — avg duration per phase, slowest highlighted
- **Board error rates** — bar chart per board

## Inline Editing & Draft Review

### Edit Flow

1. Click any field value OR press Enter while focused → enters edit mode
2. Edit the value → yellow highlight, strikethrough of original above
3. Enter saves the field edit, Escape cancels
4. Changes staged in browser memory (not saved yet)
5. Sticky bottom bar: "3 changes — [Review Changes] [Discard All]"
6. "Review Changes" → modal with diff table: Field | Original | New
7. "Confirm" → POST `/api/jobs/:id/draft-overrides` → saves to `draft_overrides.json`
8. Changes applied on next submission

### Approve Flow

After confirming edits (or with no edits):
1. Click "Approve + Submit"
2. Confirmation dialog: "Submit application for [Company] — [Role]?"
3. POST `/api/jobs/:id/approve`
4. Worker picks up the job and submits

## Mobile Responsiveness

Optimized for iPhone 17 Pro (402pt width, 6.3" screen):
- Breakpoint: 768px
- Touch targets: 44pt minimum
- **Queue** → cards instead of table rows (Company, Role, Status badge, progress)
- **Job Detail** → tabs stack vertically, sticky header shrinks to one line
- **Add Jobs** → full-width textarea, dropdowns below
- **Navigation** → hamburger menu on mobile

## API Endpoints

### Jobs
- `GET /api/jobs` — list jobs with optional filters (status, board, search, limit, offset)
- `GET /api/jobs/:id` — single job with metrics, timeline, field corrections
- `POST /api/jobs` — add one or more jobs (body: `{urls: [...], provider?, priority?}`)
- `POST /api/jobs/:id/approve` — approve draft, transition to submitting
- `POST /api/jobs/:id/reject` — reject draft, set needs_manual
- `POST /api/jobs/:id/regenerate` — regenerate assets
- `POST /api/jobs/:id/retry` — re-queue a failed job
- `POST /api/jobs/:id/skip` — skip a job
- `POST /api/jobs/:id/board-url` — set board URL manually (body: `{url: "..."}`)
- `POST /api/jobs/:id/draft-overrides` — save field edits (body: `{overrides: {...}}`)
- `DELETE /api/jobs/:id` — delete a job

### Workers
- `GET /api/workers/status` — running/stopped, PID
- `POST /api/workers/start` — start worker subprocess
- `POST /api/workers/stop` — stop worker subprocess
- `POST /api/workers/restart` — clean restart (kill + reset stale + start)

### Stats
- `GET /api/stats/summary` — aggregate stats
- `GET /api/stats/phases` — phase avg durations
- `GET /api/stats/boards` — board error rates
- `GET /api/stats/processed` — jobs processed by time window

### WebSocket
- `ws://host:port/ws` — real-time updates

## Files to Create

- `scripts/job_web.py` — FastAPI app, all API routes, WebSocket handler, worker management
- `scripts/static/index.html` — single page with all views
- `scripts/static/app.js` — vanilla JS: DOM updates, WebSocket client, hash routing, inline editing
- `scripts/static/style.css` — all styles, responsive breakpoints

## Files to Modify

- `bin/job-assets` — add `web` command with `--host`, `--port` flags

## Testing

- Unit tests for API endpoints (FastAPI TestClient)
- WebSocket integration test (connect, receive job_bulk, verify format)
- Test inline edit → draft-overrides → approve flow
- Test worker start/stop/restart via API
- Test mobile responsive layout (viewport meta tag, touch targets)
