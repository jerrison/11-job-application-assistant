---
title: "feat: Add sticky dock to job detail view"
type: feat
status: active
date: 2026-03-26
origin: docs/brainstorms/2026-03-26-job-detail-sticky-dock-requirements.md
---

# feat: Add sticky dock to job detail view

## Overview

Add a persistent two-row sticky dock to the web job-detail view so section navigation and job actions remain available while the user scrolls through long content such as screenshots, logs, resumes, and cover letters. The change should stay within the existing FastAPI-served static frontend, reusing the current tabbed detail model rather than introducing a new information architecture (see origin: `docs/brainstorms/2026-03-26-job-detail-sticky-dock-requirements.md`).

## Problem Frame

The current detail page splits its controls across a sticky `.job-header`, a separately sticky `.action-row`, and a non-sticky tab bar. In practice, that means the user loses easy access to section navigation and some actions once they scroll down the page. The result is extra navigation friction during review, especially on long screenshot and log tabs, exactly where persistent controls matter most (see origin: `docs/brainstorms/2026-03-26-job-detail-sticky-dock-requirements.md`).

## Requirements Trace

- R1. Keep a persistent sticky dock visible while the user scrolls within any job-detail tab.
- R2. Anchor the dock below the fixed global navigation without visual or layering conflicts.
- R3. Use a two-row dock: row 1 for back affordance, compact job identity, status, and all exposed job actions; row 2 for section tabs.
- R4. Expose all relevant job actions directly in the dock, not in an overflow menu.
- R5. Preserve direct access to the existing job-detail tabs: Answers, Resume, Cover Letter, Screenshot, Confirmation, Logs, Timeline, and Interview Prep.
- R6. Keep navigation and actions available while viewing long-content tabs such as screenshots and logs.
- R7. On narrow laptop and mobile widths, keep the action rail and tab rail pinned and horizontally scrollable instead of wrapping into a tall header.
- R8. Preserve action hierarchy: `Approve + Submit` primary, `Stop` prominent when relevant, `Archive` and `Delete` visible but secondary.
- R9. Minimize disruptive layout shift during live status updates by keeping action order stable where possible.
- R10. Ensure sticky positioning does not obscure the selected tab's content.
- R11. Extend the existing tabbed job-detail experience rather than converting the page into a long anchor-based document.

## Scope Boundaries

- Do not redesign the queue view or the fixed global app navigation.
- Do not change backend action logic, permissions, or job-state transitions.
- Do not hide job actions behind overflow menus, drawers, or command-palette-only access.
- Do not convert job detail into a single expanded long-form page.
- Do not add new tabs or new job actions as part of this work.

## Context & Research

### Relevant Code and Patterns

- `scripts/static/index.html` currently renders the job-detail chrome as four separate siblings: `.back-bar`, `.job-header`, `.action-row`, and `.tab-bar`.
- `scripts/static/style.css` already uses `position: sticky` for `.job-header`, but `.action-row` is sticky at `top: 0` while `.tab-bar` is not sticky at all. This split-offset setup is the main source of the current control-loss behavior.
- `scripts/static/style.css` already contains horizontal overflow patterns worth reusing:
  - `.badge-bar` uses `overflow-x: auto` with hidden scrollbars
  - mobile `.tab-bar` already switches to horizontal scrolling
- `scripts/static/app.js` centralizes detail rendering through `renderJobDetail()`, `renderJobHeaderFull()`, `renderJobActionRow()`, `switchTab()`, and `updateJobDetailHeader()`. Those are the correct seams for the dock refactor.
- `scripts/static/app.js` already uses `scrollIntoView({ block: 'nearest' })` in the command palette list. That is a local pattern for keeping an active item visible within an overflow container.
- `scripts/job_web.py` serves `scripts/static/index.html` directly from `/`, so shell-level HTML regressions are testable in `tests/test_job_web.py` via `TestClient`.
- `tests/test_draft_web.py::test_dashboard_html` shows the repo's existing pattern for lightweight HTML smoke tests on FastAPI-served static pages.
- `docs/plans/2026-03-24-006-fix-dedup-modal-scroll-plan.md` is relevant prior art for this repo's CSS overflow and scroll-behavior fixes: solve the layout issue generically, not only for the first obvious broken surface.

### Institutional Learnings

- No directly relevant `docs/solutions/` entry currently covers sticky web-UI controls or tab-dock behavior.
- No `docs/solutions/patterns/critical-patterns.md` file exists in this repo today, so there is no separate global critical-patterns document to account for in this plan.

### External References

- None. The relevant stack and patterns are already established inside this repo: FastAPI-served static HTML, vanilla JS rendering, and existing overflow/sticky CSS patterns.

## Key Technical Decisions

- Replace the current split sticky elements with a single `job-detail-dock` container that is sticky below `--nav-height`. A single dock is simpler and avoids competing sticky offsets.
- Keep `board-url-bar`, `error-banner`, and `progress-wrap` below the dock instead of pulling them into the sticky surface. That preserves focus on persistent navigation/actions without permanently increasing dock height.
- Preserve the current tab-content loading model. The dock should wrap the existing tabbed experience, not replace it with anchor navigation or a full page rewrite.
- Continue hiding unavailable actions instead of rendering disabled placeholders. To satisfy R9, render visible actions from a single ordered descriptor list so shared actions keep a stable left-to-right position across job states.
- Use horizontal overflow rails with subtle visual affordances rather than wrapping or overflow menus on narrow widths.
- Treat user-triggered tab changes and passive data refreshes differently:
  - explicit tab changes should keep the dock visible and align the new tab content beneath it
  - passive re-renders from websocket/job-status updates should not force scroll jumps

## Open Questions

### Resolved During Planning

- **How should horizontally scrollable dock rows signal off-screen controls?** Use the repo's existing hidden-scrollbar overflow pattern plus subtle edge-fade or peek affordances. Also scroll the active tab into view when the user changes tabs on a narrow rail.
- **Should unavailable actions stay visible as disabled placeholders?** No. Keep unavailable actions hidden, but enforce deterministic ordering for the actions that remain visible.
- **Should progress, error, and board-URL surfaces become part of the sticky dock?** No. Keep them in normal flow below the dock to avoid turning the sticky surface into a tall control wall.
- **Should tab changes force the viewport to the start of the new tab content?** Yes for explicit user-initiated tab navigation; no for passive content reloads triggered by status updates.

### Deferred to Implementation

- The exact dock padding, row heights, and edge-fade dimensions should be tuned after seeing the real page in a browser.
- The tab-local regenerate bars (`_tabActionBar`) may need spacing or contrast adjustments after the dock lands, but the need is visual and should be confirmed in-browser.

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```text
view-job
  job-detail-dock (sticky, top = nav height)
    dock-row-primary
      back link
      compact job summary (title, meta, status)
      action rail (overflow-x auto on narrow widths)
    dock-row-tabs
      tab rail (overflow-x auto on narrow widths)
  board-url-bar / error-banner / progress-wrap (normal flow)
  active tab panel

renderJobDetail(job)
  -> render dock summary
  -> render ordered visible actions
  -> render active tab state
  -> render auxiliary bars
  -> load current tab content

switchTab(tab, { userInitiated })
  -> update active tab button
  -> reveal target panel
  -> if userInitiated:
       keep active tab button visible in the rail
       align panel start below sticky dock
```

## Implementation Units

- [ ] **Unit 1: Build the unified sticky dock shell in static markup and CSS**

**Goal:** Replace the current split header/action/tab chrome with a single sticky two-row dock anchored below the fixed nav.

**Requirements:** R1, R2, R3, R5, R7, R10, R11

**Dependencies:** None

**Files:**
- Modify: `scripts/static/index.html`
- Modify: `scripts/static/style.css`
- Test: `tests/test_job_web.py`

**Approach:**
- Introduce a `job-detail-dock` wrapper that contains the current back affordance, summary/status block, action rail, and tab rail.
- Replace the current separate sticky rules on `.job-header` and `.action-row` with dock-level sticky positioning anchored below `--nav-height`.
- Keep the dock visually subordinate to the fixed nav and mobile nav drawer by setting z-indexes intentionally rather than adding more sticky layers.
- Reuse existing visual language from the current `job-header`, `tab-bar`, and overflow rails so the dock feels native to the current UI.
- Keep the tab panels and non-dock helper bars in normal flow below the dock so the first content remains visible once the dock is pinned.

**Execution note:** Add a lightweight root-HTML smoke test first so the new dock shell and row containers are covered before the DOM refactor lands.

**Patterns to follow:**
- `scripts/static/style.css` existing `.job-header`
- `scripts/static/style.css` existing `.badge-bar`
- `scripts/static/style.css` mobile `.tab-bar`
- `tests/test_draft_web.py::test_dashboard_html`

**Test scenarios:**
- `/` serves HTML containing the new job-detail dock shell and both dock rows.
- The dock stays directly below the fixed nav instead of competing with it.
- Narrow widths use horizontal overflow rather than wrapping the dock into extra sticky lines.

**Verification:**
- In the browser, the dock remains visible while scrolling deep into long screenshot and log content, and the global nav still layers correctly above it.

- [ ] **Unit 2: Refactor job-detail rendering into an ordered, dock-aware control surface**

**Goal:** Populate the new dock from the existing detail-render pipeline while keeping live job updates and contextual action availability intact.

**Requirements:** R3, R4, R5, R8, R9, R11

**Dependencies:** Unit 1

**Files:**
- Modify: `scripts/static/app.js`
- Test: `tests/test_job_web.py`

**Approach:**
- Keep `renderJobDetail()` and `updateJobDetailHeader()` as the primary entry points, but adapt `renderJobHeaderFull()` and `renderJobActionRow()` to target the dock structure instead of the old sibling layout.
- Replace the current hard-coded action-append branches with one ordered action descriptor model so visible actions preserve a stable sequence across statuses.
- Keep the same contextual availability logic already used for processing, draft, stopped, submitted, archived, and captcha states.
- Preserve existing hash routing, command palette integration, and keyboard shortcuts by keeping tab keys and action handlers unchanged wherever possible.
- Ensure websocket-driven `job_update` refreshes re-render the dock cleanly without losing `currentTab`, `currentJobId`, or staged draft changes.

**Patterns to follow:**
- `scripts/static/app.js::renderJobDetail`
- `scripts/static/app.js::renderJobHeaderFull`
- `scripts/static/app.js::renderJobActionRow`
- `scripts/static/app.js::updateJobDetailHeader`

**Test scenarios:**
- Draft jobs render `Approve + Submit`, `Restart -> Draft`, `Restart -> Submit`, `Stop`, `Archive`, and `Delete` in a consistent order.
- Processing, stopped, archived, and submitted jobs only show valid actions, while shared actions keep their relative ordering.
- Live job updates continue to refresh the job detail dock without breaking the selected tab.

**Verification:**
- Repeated status transitions or action-triggered refreshes do not duplicate buttons, lose the selected tab, or collapse the dock into inconsistent layouts.

- [ ] **Unit 3: Make deep-scroll tab navigation predictable on long-content and narrow-width views**

**Goal:** Ensure users can switch tabs from deep scroll positions and still land on the top of the new content, with both dock rows remaining usable on narrow widths.

**Requirements:** R1, R6, R7, R9, R10

**Dependencies:** Unit 2

**Files:**
- Modify: `scripts/static/app.js`
- Modify: `scripts/static/style.css`
- Test: `tests/test_job_web.py`

**Approach:**
- Extend `switchTab()` so user-initiated tab changes keep the active tab button visible inside the horizontal rail and align the new tab panel under the dock.
- Preserve the current no-scroll-jump behavior for passive tab reloads triggered by `updateJobDetailHeader()` when job state changes.
- Add edge-fade or similar subtle affordances on the action rail and tab rail so off-screen controls are discoverable without visible scrollbars.
- Check the top spacing of tab-local regenerate bars and long surfaces such as iframes and screenshots so the dock does not obscure the first meaningful content.

**Patterns to follow:**
- `scripts/static/app.js::switchTab`
- Existing `scrollIntoView()` usage in `scripts/static/app.js`
- Existing overflow-x handling on `scripts/static/style.css` mobile `.tab-bar`

**Test scenarios:**
- While scrolled deep into a tall screenshot, selecting `Resume` or `Cover Letter` surfaces the top of the new content below the dock.
- Number-key tab changes and command-palette tab changes keep the selected tab visible in the horizontal rail.
- Narrow-width action and tab rails remain usable without wrapping into extra sticky lines.

**Verification:**
- In a real browser, 10+ repeated tab switches on desktop and narrow widths never require scrolling back to the top to reach tabs or actions.

## System-Wide Impact

- **Interaction graph:** `navigate()` -> `renderJobDetail()` -> `renderJobHeaderFull()` / `renderJobActionRow()` / `switchTab()` remains the main render path. Websocket `job_update` messages flow through `updateJobDetailHeader()`, and keyboard/command-palette tab navigation still flows through `switchTab()`.
- **Error propagation:** Failures should remain isolated to the web detail view. The plan does not change backend endpoints or job state transitions, so DOM mismatches are the primary risk surface.
- **State lifecycle risks:** `currentJobId`, `currentTab`, and `window.stagedChanges` must survive dock rerenders. User-triggered tab changes can intentionally reposition scroll, but passive updates must not.
- **API surface parity:** CLI, TUI, backend APIs, and worker behavior remain unchanged. This is a web-only presentation and interaction change.
- **Integration coverage:** TestClient can prove root-shell markup, but only browser verification can prove sticky positioning, overflow discoverability, repeated interactions, and long-content behavior.

## Risks & Dependencies

- Sticky layering can regress if the dock, global nav, mobile nav drawer, or command palette use conflicting z-index assumptions.
- Long titles plus the full draft action set can stress narrow-width layouts; the action rail must degrade cleanly without wrapping into a tall persistent header.
- Aggressive tab-switch scroll alignment can create jarring jumps if it also runs during passive refresh paths, so user-initiated and passive tab transitions must stay distinct.
- There is no dedicated JS unit-test harness in this repo today, so manual browser verification is a real dependency for confidence.

## Documentation / Operational Notes

- No backend or operator-facing docs need to change for correctness.
- If the repo still treats `docs/superpowers/specs/2026-03-16-web-ui-design.md` as a living design reference, update its job-detail section after implementation so it reflects the dock rather than the older split sticky/header model.
- During execution, browser behavior and screenshots should be treated as the source of truth for the sticky dock, consistent with the repo's broader screenshot-first verification posture.

## Sources & References

- **Origin document:** `docs/brainstorms/2026-03-26-job-detail-sticky-dock-requirements.md`
- Related code: `scripts/static/index.html`
- Related code: `scripts/static/app.js`
- Related code: `scripts/static/style.css`
- Related code: `scripts/job_web.py`
- Related tests: `tests/test_job_web.py`
- Related tests: `tests/test_draft_web.py`
- Related prior plan: `docs/plans/2026-03-24-006-fix-dedup-modal-scroll-plan.md`
- Related design note: `docs/superpowers/specs/2026-03-16-web-ui-design.md`
