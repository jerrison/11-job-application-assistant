---
title: "fix: Harden job detail chrome and helper surfaces"
type: fix
status: active
date: 2026-03-26
origin: docs/brainstorms/2026-03-26-job-detail-sticky-dock-requirements.md
---

# fix: Harden job detail chrome and helper surfaces

## Overview

Harden the web job-detail view so the sticky dock stays a compact control surface and auxiliary helper surfaces render below it without obscuring tab content. This plan combines the updated sticky-dock requirements with the web-specific proof-placement requirements from `docs/brainstorms/2026-03-26-answer-regeneration-proof-requirements.md`, and it intentionally focuses on the presentation seam between them rather than reopening the backend answer-refresh lifecycle already covered by `docs/plans/2026-03-26-007-fix-answer-regeneration-proof-plan.md`.

## Problem Frame

The recent sticky-dock work solved the original "controls disappear while scrolling" problem, but it also created a new regression at the seam between sticky chrome and answer-refresh proof. The current job-detail implementation now mounts the answer-refresh proof surface inside the sticky dock, while `board-url-bar`, `error-banner`, and `progress-wrap` remain below it. At the same time, the proof card is dynamically created as a nested `<section>`, which collides with the global view-level `section { min-height: ... }` rule and lets the proof surface expand into a viewport-dominating panel.

That breaks two product promises at once:
- the sticky dock no longer behaves like compact chrome
- visible answer-refresh proof can hide the very answers, resume, cover letter, and screenshots the user is trying to review

The updated requirements now define a clearer boundary: the sticky dock owns only core controls, and all auxiliary helper surfaces, including shared answer-refresh proof, belong below it in normal flow (see origin: `docs/brainstorms/2026-03-26-job-detail-sticky-dock-requirements.md`; secondary origin: `docs/brainstorms/2026-03-26-answer-regeneration-proof-requirements.md`).

## Requirements Trace

- Dock R1-R3. Keep a persistent two-row sticky dock for core job controls only.
- Dock R5-R7. Preserve direct tab access and narrow-width horizontal rails while reviewing long content.
- Dock R9-R10. Keep live updates stable and prevent sticky chrome from obscuring the first meaningful tab content.
- Dock R12-R16. Move helper surfaces below the dock in urgency order and insulate them from page-level sizing/layout rules that can create viewport-height panels.
- Answer R6. Preserve visible answer-refresh proof in the web draft UI.
- Answer R11-R14. Render one shared, non-sticky answer-refresh proof card below the dock on every tab, after board URL, error, and progress, without obscuring active tab content.

## Scope Boundaries

- Do not change the backend answer-refresh lifecycle, `answer_refresh_status.json` schema, or job detail API contract.
- Do not change CLI, TUI, or draft-summary artifact semantics for answer-refresh proof.
- Do not add new job-detail tabs, actions, or overflow menus.
- Do not redesign the queue view, dashboard, or global app navigation.
- Do not convert job detail into a single expanded document.

## Context & Research

### Relevant Code and Patterns

- `pyproject.toml` shows a Python 3.12 project with FastAPI for the local web UI, pytest for tests, Ruff for linting, and Playwright available for browser verification.
- `ARCHITECTURE.md` confirms the web UI is a local-only FastAPI app (`scripts/job_web.py`) backed by static frontend files in `scripts/static/`.
- `scripts/static/index.html` currently places `#answer-refresh-proof` inside `.job-summary` within `#job-detail-dock`, while `#board-url-bar`, `#error-banner`, and `#progress-wrap` already live below the dock.
- `scripts/static/app.js` centralizes job-detail rendering through `renderJobDetail()`, `renderJobHeaderFull()`, `renderJobActionRow()`, and `switchTab()`. Those are the correct seams for separating dock-only rendering from helper-surface rendering.
- `scripts/static/app.js::buildAnswerRefreshCard()` currently creates the proof card as a `<section>`, `renderJobHeaderFull()` mounts it in the dock, and `loadAnswersTab()` appends a second tab-local proof card, which creates duplication risk.
- `scripts/static/app.js::getJobDetailContentAnchor()` currently treats only board URL, error, and progress as helper anchors. If answer proof moves below the dock, that anchor logic must include it.
- `scripts/static/style.css` applies `section { min-height: calc(100vh - var(--nav-height) - 80px); }` globally, even though only the top-level `#view-*` sections need that sizing. That broad selector is the direct recurrence risk.
- `tests/test_job_web.py` already covers the root HTML shell and answer-refresh API exposure, and is the right place for HTML/helper-stack regression checks.
- `docs/plans/2026-03-24-006-fix-dedup-modal-scroll-plan.md` is relevant prior art: fix overflow and layout bugs at the shared layout seam, not only in the first broken component.

### Institutional Learnings

- `docs/solutions/workflow-issues/explicit-answer-regeneration-requires-durable-fresh-proof-2026-03-26.md`
  - Durable answer-refresh proof must remain visible and request-scoped.
  - The current prevention guidance is now stale because it still assumes header plus Answers-tab proof cards, so this work should refresh that guidance to avoid recreating duplication later.
- No `docs/solutions/patterns/critical-patterns.md` file exists in this repo today.

### External References

- None. Local repo patterns are sufficient for this plan.

## Key Technical Decisions

- Keep the sticky dock limited to core controls. Board URL prompts, error banners, progress, and shared answer-refresh proof all belong in one normal-flow helper stack below the dock.
- Keep existing helper IDs stable where practical, especially `answer-refresh-proof`, `board-url-bar`, `error-banner`, and `progress-wrap`, so the frontend refactor can reuse current selectors with minimal churn.
- Make the shared helper stack the only canonical web proof surface. Remove Answers-tab proof duplication instead of preserving two synchronized proof render paths.
- Narrow the global view sizing rule from all `section` elements to top-level view sections only, and stop using bare `<section>` for dynamically injected helper cards. This fixes the current regression and reduces recurrence risk for future nested helper surfaces.
- Update explicit tab-alignment logic so user-initiated tab switches anchor below the dock and the first visible helper surface, while passive job updates continue to avoid scroll jumps.
- Refresh docs that currently teach the older "header + Answers tab" proof model so future work does not reintroduce it.

## Open Questions

### Resolved During Planning

- Where should answer-refresh proof live in the web UI? In a single shared helper card below the sticky dock on every tab.
- Should helper surfaces remain sticky? No. Only the dock remains sticky.
- How should multiple helper surfaces be ordered? Urgency-first: board URL, error, progress, then answer-refresh proof.
- Should this be a narrow proof-card patch or a broader layout hardening? Broader hardening. The global `section` sizing seam is the real recurrence risk.

### Deferred to Implementation

- Exact helper-stack class names and whether the wrapper should collapse entirely when all helper surfaces are hidden.
- Minor spacing or chip-density tweaks for the shared proof card on narrow widths after browser verification.
- Whether the existing `answer-refresh-card-{context}` modifier naming survives the refactor or should be simplified once the header context disappears.

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```text
view-job
  job-detail-dock (sticky, core controls only)
    dock-row-primary
      back link
      compact job summary
      status
      action rail
    dock-row-tabs
      tab rail
  job-detail-helper-stack (normal flow, fixed order)
    board-url-bar?
    error-banner?
    progress-wrap?
    answer-refresh-proof?
  active tab panel

renderJobDetail(job)
  -> render dock-only summary + actions
  -> render helper stack from job state in fixed order
  -> load active tab content

switchTab(tab, { userInitiated })
  -> update active tab button
  -> reveal target panel
  -> if userInitiated:
       align viewport below dock and first visible helper
     else:
       do not force scroll movement
```

## Implementation Units

- [ ] **Unit 1: Separate dock chrome from helper surfaces in markup and CSS**

**Goal:** Make the sticky dock core-controls only, move shared helper surfaces below it, and eliminate the broad nested-section sizing hazard.

**Requirements:** Dock R1-R3, R10, R12-R16; Answer R11, R14

**Dependencies:** None

**Files:**
- Modify: `scripts/static/index.html`
- Modify: `scripts/static/style.css`
- Test: `tests/test_job_web.py`

**Approach:**
- Introduce a dedicated helper-stack container immediately below `#job-detail-dock`.
- Move `#answer-refresh-proof` out of `.job-summary` and into the helper stack after `#progress-wrap`, preserving urgency-first DOM order.
- Narrow the global `section` min-height rule so it targets only the top-level `#view-*` sections, not nested helper content.
- Keep helper surfaces visually related but clearly separate from the sticky dock so the dock remains compact even when helpers are visible.
- Use helper/card markup that cannot accidentally inherit page-level view sizing in the future.

**Execution note:** Start with a failing HTML smoke assertion for helper-stack placement before the DOM/CSS refactor lands.

**Patterns to follow:**
- Existing helper bars in `scripts/static/index.html`
- Existing dock shell in `scripts/static/index.html`
- Prior layout-hardening pattern in `docs/plans/2026-03-24-006-fix-dedup-modal-scroll-plan.md`

**Test scenarios:**
- `/` serves HTML containing the dock shell and a helper-stack region below it.
- `#answer-refresh-proof` appears outside `#job-detail-dock` and after `#progress-wrap` in DOM order.
- Top-level app views still retain their intended minimum height after the selector is narrowed.

**Verification:**
- The dock remains compact while helper surfaces render below it.
- No nested helper card can expand the dock into a viewport-height block.

- [ ] **Unit 2: Rewire shared helper rendering and tab-alignment behavior**

**Goal:** Render one canonical shared answer-refresh helper surface below the dock and keep tab navigation predictable as helper visibility changes.

**Requirements:** Dock R5-R7, R9-R10, R13-R15; Answer R6, R11-R14

**Dependencies:** Unit 1

**Files:**
- Modify: `scripts/static/app.js`
- Test: `tests/test_job_web.py`

**Approach:**
- Split dock rendering from helper rendering so `renderJobHeaderFull()` no longer owns the answer-proof block inside the dock.
- Keep `presentAnswerRefresh()` and the `job["answer_refresh"]` API contract intact; this work changes placement, not proof semantics.
- Remove Answers-tab-local proof insertion from `loadAnswersTab()` so the helper stack becomes the single web proof surface.
- Extend content-anchor logic to treat answer-refresh proof as part of the helper stack when aligning user-initiated tab switches.
- Preserve the current rule that passive job updates refresh the page state without forcing scroll jumps.

**Patterns to follow:**
- `scripts/static/app.js::renderJobDetail`
- `scripts/static/app.js::renderJobHeaderFull`
- `scripts/static/app.js::loadAnswersTab`
- `scripts/static/app.js::switchTab`
- `scripts/static/app.js::getJobDetailContentAnchor`

**Test scenarios:**
- Pending, fresh, failed, not_applicable, and unknown answer-refresh states render a shared helper card only once.
- Board URL, error, progress, and answer-refresh proof render in the required order when all are visible.
- When a reanswering job returns to `draft`, the current tab and helper surfaces refresh without duplicating proof content.

**Verification:**
- After repeated tab switches and status refreshes, there is still exactly one shared answer-refresh proof surface in the web UI.
- User-initiated tab switches land with the dock and any visible helper surfaces above the first meaningful tab content.

- [ ] **Unit 3: Strengthen regression coverage and browser verification for helper-stack behavior**

**Goal:** Lock in the helper-stack shell with low-cost tests and verify the repeated-interaction behavior that static HTML checks cannot prove.

**Requirements:** Dock success criteria; Answer success criteria

**Dependencies:** Unit 2

**Files:**
- Modify: `tests/test_job_web.py`
- Test: `tests/test_job_web.py`

**Approach:**
- Expand the root HTML smoke test to assert the helper-stack shell and the relocated answer-refresh region.
- Keep existing API-detail answer-refresh tests so the frontend still has the data it needs after the rendering refactor.
- Use browser verification to stress long-content tabs, narrow-width rails, helper permutations, and repeated tab switching.

**Execution note:** Browser verification must include 10+ repeated tab switches and helper permutations; screenshots are the source of truth for the final layout.

**Patterns to follow:**
- Existing TestClient coverage in `tests/test_job_web.py`
- Screenshot-first verification posture from `AGENTS.md`

**Test scenarios:**
- `/` includes the helper-stack shell outside the dock.
- The job-detail API still returns answer-refresh metadata used by the shared helper card.
- In a real browser, answer-refresh proof remains visible on Answers, Resume, Cover Letter, and Screenshot tabs without duplication.
- In a real browser, helper surfaces remain non-sticky and do not hide the first meaningful content on long tabs.
- In a real browser, narrow widths keep the action and tab rails usable while helper surfaces are visible.

**Verification:**
- Browser screenshots show dock, helper stack, and visible first tab content together.
- Repeated tab switching does not reintroduce duplicate proof cards or content-obscuring helper layout.

- [ ] **Unit 4: Refresh docs and institutional learnings to match the hardened web model**

**Goal:** Align durable docs with the single shared helper-card model so future work does not recreate header/tab proof duplication.

**Requirements:** Dock R13-R16; Answer R11-R14

**Dependencies:** Unit 2

**Files:**
- Modify: `docs/board-architecture.md`
- Modify: `docs/solutions/workflow-issues/explicit-answer-regeneration-requires-durable-fresh-proof-2026-03-26.md`

**Approach:**
- Update board-architecture documentation so the web proof surface is described as a shared non-sticky helper card below the dock on every tab.
- Refresh the solution doc's verification and prevention guidance so it no longer teaches "header + Answers tab" proof duplication.
- Keep the backend durability story unchanged; only the web placement and verification guidance should move.

**Patterns to follow:**
- Existing answer-refresh contract wording in `docs/output-structure.md`
- Existing documentation style in `docs/board-architecture.md`
- Existing solution-doc structure in `docs/solutions/workflow-issues/explicit-answer-regeneration-requires-durable-fresh-proof-2026-03-26.md`

**Test scenarios:**
- No durable doc still claims the web proof surface lives in the header or separately in the Answers tab.
- Docs consistently describe one shared helper-stack proof surface and the urgency-ordered helper stack.

**Verification:**
- Repo docs and institutional learnings agree on proof placement, helper ordering, and browser verification expectations.

## System-Wide Impact

- **Interaction graph:** `navigate()` -> `renderJobDetail()` -> dock render + helper render + `switchTab()` remains the main job-detail path. `updateJobDetailHeader()` and websocket-driven refreshes stay on that same path.
- **Error propagation:** This is a presentation-layer hardening. The main failure risk is DOM/layout drift, not backend state corruption.
- **State lifecycle risks:** `currentJobId`, `currentTab`, and live job updates must continue to coexist with helper-surface re-rendering without duplicate proof regions or unwanted scroll jumps.
- **API surface parity:** The `job["answer_refresh"]` response shape should remain unchanged. This plan is web-only placement and layout work.
- **Integration coverage:** TestClient can prove shell markup and API continuity, but only browser verification can prove sticky behavior, helper ordering under scroll, repeated tab switching, and narrow-width layout stability.

## Risks & Dependencies

- Narrowing the global `section` selector touches every top-level view, so queue/add/dashboard/stats screens need a quick visual smoke check even though the main change is in job detail.
- Moving `#answer-refresh-proof` out of the dock can break selectors or stale tests if IDs are renamed unnecessarily.
- The solution doc and board architecture docs are currently stale relative to the updated requirements; leaving them untouched increases the chance of reintroducing the old header/tab proof duplication later.
- There is no dedicated JS unit-test harness in this repo today, so browser verification is a real dependency for confidence.

## Documentation / Operational Notes

- This plan supersedes the web-layout guidance in `docs/plans/2026-03-26-004-feat-job-detail-sticky-dock-plan.md` and the web proof-surface guidance in `docs/plans/2026-03-26-007-fix-answer-regeneration-proof-plan.md`, while leaving the backend answer-refresh lifecycle from `007` intact.
- If `docs/superpowers/specs/2026-03-16-web-ui-design.md` is still treated as a living reference, update its job-detail section after implementation so it reflects the dock/helper split instead of the older sticky-header model.
- During execution, browser screenshots should be treated as the source of truth for final job-detail helper placement and tab-content visibility.

## Sources & References

- **Origin document:** `docs/brainstorms/2026-03-26-job-detail-sticky-dock-requirements.md`
- **Secondary requirements:** `docs/brainstorms/2026-03-26-answer-regeneration-proof-requirements.md`
- Related code: `scripts/static/index.html`
- Related code: `scripts/static/app.js`
- Related code: `scripts/static/style.css`
- Related code: `scripts/job_web.py`
- Related tests: `tests/test_job_web.py`
- Related docs: `docs/board-architecture.md`
- Related docs: `docs/output-structure.md`
- Related learning: `docs/solutions/workflow-issues/explicit-answer-regeneration-requires-durable-fresh-proof-2026-03-26.md`
- Related prior plan: `docs/plans/2026-03-24-006-fix-dedup-modal-scroll-plan.md`
- Related prior plan: `docs/plans/2026-03-26-004-feat-job-detail-sticky-dock-plan.md`
- Related prior plan: `docs/plans/2026-03-26-007-fix-answer-regeneration-proof-plan.md`
- Related design note: `docs/superpowers/specs/2026-03-16-web-ui-design.md`
