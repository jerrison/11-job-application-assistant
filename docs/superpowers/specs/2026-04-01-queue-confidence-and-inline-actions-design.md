# Queue Confidence And Inline Actions Design

Date: 2026-04-01
Status: Approved for spec review
Related:

- [2026-03-16-web-ui-design.md](./2026-03-16-web-ui-design.md)
- [../../board-architecture.md](../../board-architecture.md)
- [../../operational-rules.md](../../operational-rules.md)
- [../../output-structure.md](../../output-structure.md)

## Problem

The current web queue is still optimized for opening job detail, not for high-speed draft review and action.

Today:

- the queue row is mostly a status summary built in `scripts/static/app.js`
- richer draft evidence exists, but it is shown only in job detail helpers
- per-job actions already exist, but they are rendered only in the job-detail dock
- the queue row uses relatively large controls and pills, which causes clipping pressure when more controls are added

That creates three problems:

- you cannot tell at a glance how trustworthy a draft is before opening it
- you cannot perform the common per-job actions directly from the queue
- the current button sizing wastes width in the exact view that needs denser controls

The user asked for a queue that answers two questions without opening a job:

1. how confident should I be in this draft?
2. what can I do with it right now?

## Goals

- Show a queue-level confidence signal for each job before detail view is opened.
- Make the queue a control-first surface for common per-job actions.
- Keep all valid row actions visible inline for each job state.
- Reduce button size and row chrome so the queue stops clipping on desktop.
- Reuse the existing submit/restart/archive logic instead of creating queue-only behavior.
- Keep the design compatible with the existing FastAPI + vanilla JS web UI.
- Keep confidence evidence-driven, not based on model self-reported certainty.

## Non-Goals

- Replacing the job detail page.
- Hiding actions in an overflow menu by default.
- Replacing existing proof artifacts or draft-review source-of-truth files.
- Rebuilding the queue into a completely different application shell.
- Introducing a frontend framework, build step, or separate queue micro-app.

## Current Constraints

The current implementation already has the pieces needed for a stronger queue, but they are split across different surfaces.

- `buildQueueRow()` in `scripts/static/app.js` renders a lightweight status-centric row.
- `renderJobDetailHelpers()` in `scripts/static/app.js` renders answer refresh and draft proof context only after detail view is opened.
- `getJobActionModels()` in `scripts/static/app.js` already knows which actions are valid for a given job, but only job detail uses it.
- `/api/queue` in `scripts/job_web.py` currently returns queue rows without a queue-specific confidence summary.
- `/api/jobs/{id}` already includes richer detail fields such as `answer_refresh` and `draft_review_state`.

The design should close that gap rather than inventing a parallel model.

## Approaches Considered

### 1. Add More Columns To The Existing Queue Table

Add a new confidence column and a new actions column to the existing row shape.

Pros:

- smallest structural change
- easy to implement incrementally

Cons:

- keeps the queue in a detail-first shape
- increases clipping pressure
- does not solve the oversized-control problem cleanly

Rejected.

### 2. Convert The Queue Into A Compressed Control Row

Keep the queue as a table, but reorganize each row around three dense working areas: job identity, draft confidence, and actions.

Pros:

- best fit for a control-first queue
- preserves scanability and sorting
- creates enough space for all visible actions without falling back to menus
- lets confidence become a first-class signal instead of a badge hidden in status text

Cons:

- requires backend and frontend changes rather than a CSS-only pass

Recommended.

### 3. Replace Desktop Table Rows With Desktop Cards

Promote the current mobile-style card model to desktop as well.

Pros:

- maximum visual flexibility
- easy to fit many actions and chips

Cons:

- weakens tabular scanning and sorting
- bigger conceptual shift than necessary

Rejected.

## Selected Design

### A. Queue Becomes Control-First

The queue remains a table, but the row is no longer primarily a click target for navigation.

Each row is reorganized into four zones:

- `Select`
- `Job`
- `Draft Confidence`
- `Actions`

The primary interaction shifts from row click to inline action.

Implications:

- row click should no longer be the main affordance
- opening job detail remains available through explicit affordances in the job cell: the title link and the job ID link
- clicking an inline action must never trigger navigation to job detail

This turns the queue into a working surface for review and dispatch, while still preserving access to the richer detail page.

### B. Row Layout

#### Job Zone

The job zone should contain the identity and orientation information a user needs before acting:

- company
- role title
- board
- source / board links
- recency such as "updated 4m ago"
- operational state badge such as `Draft`, `Stopped`, `Submitted`, `Queued`

This zone should preserve the current quick context but remove the need for a wide dedicated "links" column.

#### Draft Confidence Zone

The confidence zone becomes the main review signal for a draft-capable row.

It contains:

- one dominant overall confidence badge
- one short confidence label
- up to three compact reason chips

The overall confidence should carry more visual weight than the explanatory chips.

The row should answer, in under a second:

- can I trust this draft?
- why or why not?

#### Actions Zone

The actions zone shows all actions that are valid for the row's current state.

The queue should not reserve space for invalid actions.

Buttons should use a queue-specific compact treatment:

- smaller than current `btn-sm`
- short labels
- allowed to wrap to a second line on narrower desktop widths
- still large enough to click reliably

This is the explicit fix for the current "buttons are too big and get cut off" problem.

### C. Confidence Model

Queue confidence is evidence-driven. It does not use provider self-reported confidence as the top-level signal.

The signal is derived from:

- draft proof state
- answer verification state
- pending user input state
- manual-review / blocker state
- active draft-breaking errors

#### Overall States

- `High` — ready to submit
- `Medium` — usable draft, but review is still recommended
- `Low` — blocked or not trustworthy yet
- `Pending` — draft is still being built or refreshed
- `N/A` — no queue confidence concept applies yet

#### High

Use `High` when:

- draft proof is current / ready
- there is no pending user input
- there is no unresolved manual review or blocker state
- answer verification is `verified` or `not_applicable`
- there is no active error that undermines the draft

Display label:

- `Ready to submit`

#### Medium

Use `Medium` when:

- a usable draft exists, but the evidence is weaker than ideal

Examples:

- stale or legacy proof
- verification still pending
- stopped row with reusable draft assets
- generated-answer presence that still merits review but is not blocked

Display label:

- `Usable, but review recommended`

#### Low

Use `Low` when:

- there is an explicit blocker or contradiction that makes the draft unsafe to trust

Examples:

- blocked proof
- answer verification blocked or failed
- pending user input
- needs board URL
- unresolved manual review
- explicit draft error that invalidates trust

Display label:

- `Needs review before submit`

#### Pending

Use `Pending` when:

- the queue row is still in an in-progress phase and draft confidence is not final

Examples:

- queued
- generating
- reanswering
- regenerating

Display label:

- `Draft in progress`

#### N/A

Use `N/A` when:

- the queue row does not have a meaningful draft-confidence state yet
- or when the current evidence is truly not applicable rather than blocked

This should be rare and should still degrade gracefully instead of leaving the UI blank.

### D. Reason Chips

Show at most three reason chips per row.

Order them by:

1. severity
2. usefulness for the next decision

Recommended chip groups:

- proof state
  - `Proof current`
  - `Proof stale`
  - `Proof legacy`
  - `Proof blocked`
- answer state
  - `Answers verified`
  - `Verification pending`
  - `Verification blocked`
  - `3 AI answers`
- friction state
  - `No blockers`
  - `Pending input`
  - `Needs board URL`
  - `Manual review`
  - `1 blocker`

The chips explain the confidence score. They do not replace it.

### E. Actions Model

The queue reuses the same underlying action eligibility rules already used in job detail.

All visible means:

- show every action that is valid for the row's current state
- do not show disabled placeholders for actions that are impossible

Representative mappings:

- draft or stopped rows with reusable draft assets
  - `Submit`
  - `Redraw`
  - `Redraw + Submit`
  - `Archive`
- submitted rows
  - `Resubmit`
  - `Archive`
- processing rows
  - `Stop`
- locked resubmission rows
  - `Unlock to Resubmit`
  - plus any allowed archive or delete controls

Queue labels should be shorter than detail-view labels:

- `Approve + Submit` becomes `Submit`
- `Restart → Draft` becomes `Redraw`
- `Restart → Submit` becomes `Redraw + Submit`

The detail page may keep its longer, more explicit wording. The queue is optimized for speed and width.

### F. Backend Data Shape

The queue should not reconstruct confidence entirely in the browser from detail-only state. The server should provide a compact derived summary per row.

Extend `/api/queue` and WebSocket queue row payloads with a normalized object:

- `queue_review_summary`
  - `overall_confidence`: `high | medium | low | pending | na`
  - `confidence_label`: short text such as `Ready to submit`
  - `reason_chips`: normalized list of up to three chips
  - `proof_state`
  - `verification_state`
  - `visible_actions`: normalized list of queue-visible action ids already filtered for validity

This summary should be derived close to the existing draft-proof and answer-verification logic so:

- queue and detail cannot drift semantically
- WebSocket updates can replace a row without extra detail fetches
- the behavior is testable in one place

This design adds an aggregation layer, not a new persistence contract.

### G. Frontend Behavior

The queue implementation stays inside the existing web UI files:

- `scripts/static/index.html`
- `scripts/static/app.js`
- `scripts/static/style.css`

No framework migration or route redesign is required.

Behavior requirements:

- inline action click does not open the job
- explicit detail affordance remains available
- row updates in place after successful action
- queue filter, pagination, and selection context stay stable after actions
- rows that no longer match the current filter disappear naturally on refresh
- confidence renders gracefully for legacy or partially missing data

### H. Desktop And Mobile Layout

#### Desktop

Use the compressed control-row design:

- job identity on the left
- draft confidence in the middle
- action lane on the right

The action lane may wrap onto a second line on narrower desktop widths instead of overflowing offscreen.

#### Mobile

The queue may continue to stack into card-like rows, but the same information hierarchy should apply:

- job identity first
- confidence second
- actions below, all still visible

The mobile layout should preserve the control-first model rather than reverting to a details-first card.

## Error Handling And Graceful Degradation

Inline queue actions should fail locally:

- show a toast with the server error
- keep the user on the queue
- do not reset page/filter context unnecessarily
- do not force navigation into detail

If confidence inputs are missing or come from legacy drafts:

- render `Medium` or `N/A` rather than empty UI
- explain the downgrade with reason chips such as `Proof legacy` or `Verification unavailable`

## Testing And Verification

### Backend

- unit tests for queue confidence derivation from representative job states
- queue API test coverage for `queue_review_summary`
- WebSocket coverage for initial `job_bulk` and incremental `job_update` payloads that include the summary

### Frontend

- row renders overall confidence plus chips
- row renders the valid action set for each status
- action clicks do not trigger row navigation
- queue action updates keep context stable
- compact action styling wraps instead of clipping

### Visual Verification

- desktop queue at normal width
- narrower desktop width where action wrapping occurs
- mobile stacked layout check
- repeated clicks on inline actions to verify stable behavior under repeated interaction

### Regression Focus

- sorting and filtering continue to work
- detail-page actions remain consistent with queue actions
- resubmission lock behavior still prevents unsafe actions
- queue updates from WebSocket remain compatible with current refresh behavior

## Scope Boundary

This spec is intentionally limited to:

- queue-level confidence presentation
- queue-level inline actions
- supporting backend aggregation for those two capabilities

It does not include:

- unrelated job detail redesign
- broader dashboard redesign
- new proof generation logic
- changes to the underlying draft-verification contracts beyond queue aggregation
