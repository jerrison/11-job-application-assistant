# Application Draft Mode — Design Spec

## Problem

The pipeline currently generates assets and submits applications in one shot. There is no pause point to review generated answers, catch autofill bugs, or verify the pre-submit state before the application is sent. Mistakes are discovered after submission — too late to fix.

## Solution

Add a `draft` status to the job lifecycle. The pipeline stops after autofill (form filled, screenshots taken, answers generated) but before clicking submit. The user reviews the draft via CLI, TUI, or LLM conversation. They can approve, reject, edit answers, or trigger code fixes and regenerate. Only after explicit approval does the application submit.

## Job Lifecycle

```
queued → resolving → generating → draft → submitting → submitted
                                    ↓
                              (user reviews)
                                    ↓
                         approve  → submitting → submitted
                         reject   → needs_manual
                         regenerate → generating → draft
```

- **Default:** Pipeline stops at `draft`. The existing `--submit` flag retains its current meaning ("click the submit button"). A new `--draft` flag on `submit_application.py` fills the form and takes screenshots but stops before clicking submit.
- **`--auto-submit`:** New flag for `pipeline_orchestrator.py` and the worker. Bypasses draft pause and submits immediately. Use for trusted batch runs.
- **Worker behavior:** Worker defaults to stopping at `draft`. Pass `--auto-submit` to `job-assets worker start` for fully autonomous mode.

## Stale Draft Policy

Drafts left in `awaiting_review` for more than 7 days (configurable via `JOB_ASSETS_DRAFT_TTL_DAYS` env var) are auto-transitioned to `needs_manual` by the worker's periodic sweep. The TUI dashboard shows a "stale drafts" warning count. `job-assets draft list` highlights stale drafts with age.

## Draft Artifacts

New draft artifacts live in the **role output root** (e.g. `output/bubble/group-pm/`), not inside versioned `submit/` directories. This ensures they persist across regenerations. Existing autofill artifacts stay in `submit/`.

| File | Location | Description | New? |
|------|----------|-------------|------|
| `application_answers.json` | `submit/` | All generated answers | Existing |
| `{board}_autofill_pre_submit.png` | `submit/` | Filled form screenshot | Existing |
| `{board}_autofill_report.json` | `submit/` | Field validation report | Existing |
| `draft_summary.md` | role root | Human-readable Q&A summary (editable) | New |
| `draft_summary.original.md` | role root | Immutable copy for diffing (never overwritten by user) | New |
| `draft_summary.png` | role root | Formatted image of all Q&A pairs | New |
| `draft_status.json` | role root | Draft lifecycle state | New |
| `draft_overrides.json` | role root | Manual answer overrides that persist across regenerations | New |
| `draft_fix_report.md` | role root | Auto-generated fix report from diff analysis for `auto_fix()` context | New |

### `draft_status.json`

```json
{
  "status": "awaiting_review",
  "created_at": "2026-03-15T10:30:00Z",
  "reviewed_at": null,
  "reviewed_action": null,
  "draft_version": 1
}
```

Status values: `awaiting_review`, `approved`, `rejected`, `regenerating`, `expired`.

DB status mapping: `draft` → `awaiting_review` or `regenerating`; `submitting` → `approved`; `needs_manual` → `rejected` or `expired` (stale draft sweep sets `expired`).

`draft_version` increments on each regeneration. Status transitions use compare-and-swap: `UPDATE jobs SET status='submitting' WHERE id=? AND status='draft'` — conflicting transitions fail gracefully.

### `draft_summary.md` Format

```markdown
# Draft: Group Product Manager — Bubble
**Board:** Ashby | **Generated:** 2026-03-15 10:30 PST | **Version:** 1

## Resume
- File: documents/Jerrison Li Resume - Bubble.pdf

## Cover Letter
- File: documents/Jerrison Li Cover Letter - Bubble.pdf

## Application Answers

### 1. What are your personal pronouns? (survey_1_what_are_your_personal_pronouns)
- **Kind:** choice | **Required:** no | **Source:** application_profile.md
- **Answer:** He/Him/His
- **Status:** filled

### 2. Have you used Bubble before? (application_have_you_used_bubble_before)
- **Kind:** choice | **Required:** yes | **Source:** deterministic_override
- **Answer:** Yes
- **Status:** filled

### 3. Some unfilled question (survey_1_unfilled)
- **Kind:** choice | **Required:** no | **Source:** —
- **Answer:** —
- **Status:** unfilled
```

The parenthetical after each question label (e.g. `(survey_1_what_are_your_personal_pronouns)`) is the **field path key** — the same key used in `application_answers.json` and `draft_overrides.json`. This is the authoritative mapping between the human-readable summary and the override mechanism.

### `draft_overrides.json`

```json
{
  "application_salary_range": "Open to discussion",
  "survey_1_unfilled": "He/Him/His"
}
```

Keys are field path keys (matching the parenthetical in `draft_summary.md`). Applied after `apply_generated_answer_overrides` but before board-specific `_infer_step`. Persists across regenerations until removed or draft approved. `application_answers.json` is never edited directly — `draft_overrides.json` is the single source of truth for answer mutations.

## Edit-to-Fix Loop

The user's edit IS the fix request. No manual annotations needed — the system infers intent from the diff.

When the user edits `draft_summary.md` and runs `draft regenerate`:

1. **Diff detection:** `draft_manager.py` diffs the edited file against `draft_summary.original.md` (written at generation time, never overwritten).
2. **Auto-classification:** Each change is classified automatically:
   - **Unfilled field now has an answer** → likely missing handler → added to fix report + stored in `draft_overrides.json`
   - **Answer changed to a different value** → likely wrong override or LLM answer → added to fix report + stored in `draft_overrides.json`
   - **"Yes"/"No" on a free-text field** → likely comfort-check false positive → added to fix report
   - **Answer doesn't match any available option** → option matching failure → added to fix report
3. **Fix report generation:** `draft_manager.py` writes `draft_fix_report.md` (role root) with full context per field: label, field type, old answer, new answer, available options, source that produced the old answer.
4. **Auto-fix:** If fix report is non-empty, calls `auto_fix()` from `pipeline_orchestrator.py` with the report. `auto_fix()` invokes the `claude` CLI to apply generalized code fixes across all boards.
5. **Regenerate:** After fixes are applied, the pipeline re-runs (research cache preserved). New draft is generated with `draft_version` incremented. Manual overrides from `draft_overrides.json` are preserved.
6. **No fix needed:** If all changes are simple value overrides (e.g. user prefers a different phrasing) with no structural issue detected, `auto_fix()` is skipped — the pipeline just regenerates with the overrides applied.

In LLM runtime, the user can also describe issues conversationally instead of editing the file. The LLM applies fixes directly and triggers regeneration.

## Review Surfaces

### CLI

**New subcommands:**

| Command | Description |
|---------|-------------|
| `job-assets draft list` | List all jobs in `draft` status (company, role, board, time, age) |
| `job-assets draft review <id\|url>` | Print Q&A summary to terminal; show screenshot paths; `--edit` opens `draft_summary.md` in `$EDITOR` |
| `job-assets draft approve <id\|url>` | Transition to `submitting`, resume pipeline |
| `job-assets draft reject <id\|url>` | Transition to `needs_manual` |
| `job-assets draft regenerate <id\|url>` | Process edits/fixes, transition to `generating`, re-run pipeline (research cache preserved) |

**Existing commands enhanced:**
- `job-assets status` — drafts appear with `draft` badge, count in summary
- `job-assets report` — includes draft count

### TUI

**QueueScreen:** Gains a "Drafts" filter tab alongside existing status filters.

**JobDetailScreen** (for draft jobs):
- Inline terminal rendering of `draft_summary.png` and `{board}_autofill_pre_submit.png` (scrollable, stacked)
- Scrollable `draft_summary.md` content
- Action buttons: **Approve**, **Reject**, **Regenerate**, **Edit** (opens `$EDITOR`)

**TUI Image Rendering:**
- Primary: `rich-pixels` or `climage` for ANSI/half-block character rendering (works in all terminals)
- Enhanced: Sixel or iTerm2 inline image protocol for higher fidelity in supported terminals (iTerm2, kitty, WezTerm)
- Sizing: Scale to TUI panel width; summary image ~80 columns, pre-submit screenshot full width
- Layout: Summary image on top, pre-submit screenshot below, scrollable
- **Fallback:** If image rendering dependency is unavailable, display file paths and offer to open in default viewer
- **Dependency:** `Pillow` for `build_draft_summary.py` PNG generation. Added to `pyproject.toml`. If unavailable at runtime, skip PNG generation (markdown summary still available).

### Web Interface

**Architecture:** FastAPI backend + lightweight frontend (HTML + HTMX or React). Same SQLite DB and output files as CLI/TUI — no separate data store.

**Launch:** `job-assets draft serve` starts a local server on `localhost:8420` (configurable via `JOB_ASSETS_DRAFT_PORT`).

**API endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/drafts` | GET | List all drafts (company, role, board, age, version) |
| `/api/drafts/{id}` | GET | Full draft detail: Q&A summary, metadata, available options per field |
| `/api/drafts/{id}/images/summary` | GET | Serve `draft_summary.png` |
| `/api/drafts/{id}/images/pre-submit` | GET | Serve `{board}_autofill_pre_submit.png` |
| `/api/drafts/{id}/resume` | GET | Serve resume PDF |
| `/api/drafts/{id}/cover-letter` | GET | Serve cover letter PDF |
| `/api/drafts/{id}/approve` | POST | Approve → transition to `submitting` |
| `/api/drafts/{id}/reject` | POST | Reject → transition to `needs_manual` |
| `/api/drafts/{id}/regenerate` | POST | Process edits, trigger regeneration |
| `/api/drafts/{id}/overrides` | PUT | Update `draft_overrides.json` (inline answer editing) |

**Frontend pages:**

- **Dashboard** (`/`) — Draft list with status badges, age indicators, stale warnings. Click to open detail.
- **Draft Detail** (`/drafts/{id}`) — Side-by-side layout:
  - Left panel: Q&A pairs with inline edit capability (click answer to modify, saves to overrides)
  - Right panel: Pre-submit screenshot + draft summary image (full resolution, zoomable)
  - Top bar: Resume/cover letter PDF preview links
  - Action bar: **Approve**, **Reject**, **Regenerate** buttons
- **Edit mode** — Clicking an answer field makes it editable inline. Changes are saved to `draft_overrides.json` via the PUT endpoint. Regenerate re-runs with overrides applied.

**Remote access (deployed mode):**

For reviewing from phone or other devices, expose the local server via tunnel:
- **Cloudflare Tunnel:** `cloudflared tunnel --url http://localhost:8420` — provides a public HTTPS URL with Cloudflare Access auth
- **ngrok:** `ngrok http 8420 --basic-auth="user:pass"` — quick alternative with basic auth
- **CLI shortcut:** `job-assets draft serve --tunnel cloudflare` or `--tunnel ngrok` wraps the tunnel setup automatically

No separate deployment, cloud storage, or database sync needed — the tunnel points at the local machine where the DB and files live.

**Dependencies:** `fastapi`, `uvicorn` added to `pyproject.toml` (optional dependency group `[web]`). Install via `uv pip install -e ".[web]"`.

### LLM Runtime (Claude/Gemini/GPT)

**Scope:** LLM runtime review applies only when the pipeline is invoked directly from a Claude/Gemini/GPT conversation, not from the background worker.

After pipeline generates a draft:

1. Display full Q&A summary inline in conversation
2. Show pre-submit screenshot (read image file)
3. Prompt: "Draft ready. Review the answers above. You can: approve, reject, edit specific answers, or describe issues for me to fix."
4. If user describes issues → LLM applies generalized code fixes → user says "regenerate" → re-runs pipeline
5. If user edits `draft_summary.md` directly → LLM diffs changes, infers systemic fix, applies it
6. If user approves → pipeline resumes submission

## Files to Create

| File | Purpose |
|------|---------|
| `scripts/draft_manager.py` | Core draft logic: generate summary md/image, read edits, auto-classify diffs, manage `draft_status.json` and `draft_overrides.json`, generate fix reports, invoke `auto_fix()` |
| `scripts/build_draft_summary.py` | Generate `draft_summary.png` from markdown (formatted Q&A image with status badges, color-coded filled/unfilled). Requires `Pillow`. |
| `scripts/draft_web.py` | FastAPI app: REST API + frontend for draft review. Serves images/PDFs from output dirs. |
| `scripts/draft_web_templates/` | HTML templates (or static frontend assets) for dashboard and draft detail pages |

## Files to Modify

| File | Changes |
|------|---------|
| `scripts/job_db.py` | Add `draft` to `JOB_STATUSES` |
| `scripts/pipeline_orchestrator.py` | Stop at `draft` after autofill (unless `--auto-submit`); add `regenerate_job()` (`draft` → `generating`); add `approve_job()` (`draft` → `submitting`); stale draft sweep in worker loop |
| `scripts/submit_application.py` | New `--draft` flag (fill + screenshot + stop); apply `draft_overrides.json` during answer generation |
| `scripts/application_submit_common.py` | Load and apply draft overrides in `apply_generated_answer_overrides` |
| `scripts/autofill_pipeline.py` | Respect `--draft` flag (skip submit button click) |
| `bin/job-assets` | Add `draft list\|review\|approve\|reject\|regenerate\|serve` subcommands; add `--auto-submit` flag to `worker start` |
| `scripts/job_tui.py` | Drafts filter tab on QueueScreen; approve/reject/regenerate/edit actions on JobDetailScreen; inline image rendering |
| `scripts/job_worker.py` | Stop at `draft` instead of auto-submitting (unless `--auto-submit` mode); periodic stale draft sweep |
| `pyproject.toml` | Add `Pillow` dependency; add optional `[web]` dependency group (`fastapi`, `uvicorn`) |
| `CLAUDE.md` | Document draft mode architecture and patterns |
| `AGENTS.md` / `GEMINI.md` | Document draft mode for LLM runtime |

## Testing

- **Unit tests:** `draft_manager.py` — summary generation, diff detection, auto-classification, override application, fix report generation, status transitions
- **Integration test:** Full `draft` → edit → regenerate → approve → submit lifecycle
- **CI test:** `draft` status recognized in `job_db.py`; compare-and-swap transitions
- **TUI test:** Draft filter tab renders; image display graceful fallback on terminals without image support
- **Web API tests:** `draft_web.py` — list/detail/approve/reject/regenerate endpoints; override PUT; image/PDF serving; auth when tunnel is active
- **Edge case tests:** Stale draft expiry, concurrent approve/regenerate race, regeneration preserves overrides across versioned submit dirs
