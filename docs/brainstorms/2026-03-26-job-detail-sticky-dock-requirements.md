---
date: 2026-03-26
topic: job-detail-sticky-dock
---

# Job Detail Sticky Dock

## Problem Frame

The current job detail view exposes section navigation and job actions near the top of the page, but those controls become hard or impossible to reach once the user scrolls deep into long content such as screenshots, logs, or generated materials.

This slows down review work and creates avoidable friction in the draft workflow. The user wants to be able to switch between job-detail sections and trigger any job action at any moment while staying in context on the detail page.

Recent job-detail changes exposed a second failure mode: auxiliary helper surfaces such as answer freshness proof, board-URL prompts, error banners, and progress indicators can accidentally turn the sticky dock into a tall content wall. When that happens, the dock stops feeling like chrome and starts hiding the very assets and answers it is supposed to help the user review.

## Requirements

- R1. The job detail view shall keep a persistent sticky dock visible while the user scrolls within any job-detail tab.
- R2. The sticky dock shall sit below the fixed global app navigation and shall not visually fight with it or cover its controls.
- R3. The sticky dock shall use a two-row layout:
  - row 1: back affordance, compact job identity, status, and all exposed job actions
  - row 2: section navigation tabs
- R4. Row 1 shall expose all job actions directly in the dock. Actions shall not be hidden behind an overflow menu or secondary dialog launcher.
- R5. The dock shall preserve direct access to the existing job-detail sections, including Answers, Resume, Cover Letter, Screenshot, Confirmation, Logs, Timeline, and Interview Prep.
- R6. The dock shall remain available while viewing long content, including tall screenshots and long logs, so the user can switch sections or take actions without scrolling back to the top.
- R7. On narrow laptop and mobile widths, the action row and the tab row shall each remain pinned and horizontally scrollable rather than wrapping into a tall multi-line dock.
- R8. The sticky dock shall keep the primary hierarchy clear:
  - `Approve + Submit` remains the primary positive action
  - `Stop` remains visually prominent when relevant
  - `Archive` and `Delete` remain visible but visually secondary
- R9. The dock shall avoid disruptive layout shift while job status updates arrive. Action order should remain stable where possible, with availability changing contextually.
- R10. Sticky positioning shall not obscure the selected tab's content. The content area shall account for dock height so the first meaningful content remains visible.
- R11. The interaction model shall extend the existing tabbed job-detail experience. This work shall not require converting the page into a single long document with in-page anchor sections.
- R12. The sticky dock shall contain only core job controls:
  - back affordance
  - compact job identity
  - job status
  - direct job actions
  - section tabs
- R13. Auxiliary helper surfaces shall render below the sticky dock in normal flow rather than inside the sticky surface. This includes:
  - board-URL required prompts
  - error banners
  - progress indicators
  - shared answer freshness proof
- R14. When multiple helper surfaces are present at the same time, they shall appear below the dock in urgency-first order:
  - board URL
  - error
  - progress
  - answer freshness proof
- R15. Helper surfaces below the dock shall remain available across job-detail tabs without becoming sticky, and shall not cover or materially delay access to the first meaningful content of the active tab.
- R16. Job-detail chrome and helper surfaces shall be insulated from page-level sizing or layout rules that could expand them into viewport-height panels or otherwise obscure tab content.

## Success Criteria

- While deep in a long screenshot or log view, the user can immediately switch to Resume, Cover Letter, or Answers without scrolling back to the top.
- While deep in any job-detail content, the user can immediately trigger actions such as `Approve + Submit`, `Restart -> Draft`, `Restart -> Submit`, `Stop`, `Archive`, or `Delete` when those actions are valid for the current job state.
- On narrow widths, the sticky dock stays usable without growing so tall that it dominates the viewport.
- The sticky dock feels like a persistent job control surface rather than a fragile header that disappears during review.
- When board URL, error, progress, and answer freshness proof are all present, they appear below the dock in the defined order and the active tab's content remains immediately reachable.
- Answer freshness proof remains visible on every job-detail tab without turning into a tall sticky panel that hides resume, cover letter, screenshot, or answers content.

## Scope Boundaries

- NOT redesigning the queue view or global app navigation.
- NOT changing the underlying action logic, permissions, or state transitions for job actions.
- NOT introducing overflow menus, command palettes, or hidden action drawers as the primary way to reach job actions.
- NOT converting job detail into a long single-page document with all sections expanded at once.
- NOT adding new job-detail tabs or new job actions as part of this change.
- NOT making auxiliary helper surfaces sticky.
- NOT redefining the backend meaning of answer freshness proof; this work only governs where shared proof is surfaced in the job-detail UI.

## Key Decisions

- Extend the existing job-detail pattern rather than replacing it with a new information architecture.
- Use a persistent two-row sticky dock instead of a single dense bar.
- Keep all actions exposed in the sticky dock at all times they are relevant to the current job state.
- On narrow widths, prefer horizontal scrolling within each dock row over wrapping or switching to a different mobile-only control pattern.
- Optimize for review throughput and control availability over maximizing raw content height.
- Keep the sticky surface limited to core controls so the dock remains compact and predictable.
- Render auxiliary helper surfaces in normal flow below the dock instead of treating them as sticky chrome.
- Use urgency-first ordering for helper surfaces: board URL, error, progress, then answer freshness proof.
- Treat shared answer freshness proof as a cross-tab helper surface, not as part of the sticky dock.

## Dependencies / Assumptions

- The current web UI already supports the relevant job actions and job-detail tabs; this work is about access and layout, not net-new capabilities.
- The existing fixed top navigation remains part of the page chrome and the sticky dock must coexist with it cleanly.
- Long screenshot and log views are common enough that persistent navigation and actions materially improve workflow speed.
- Answer freshness proof remains a required visible surface for regenerated answers, but it does not need to live inside the sticky dock to satisfy that requirement.

## Alternatives Considered

- Single dense sticky bar: rejected because it compresses actions and tabs into one crowded strip that is harder to scan.
- Auto-shrinking sticky dock: rejected for now because it adds behavior complexity without being necessary to solve the core access problem.
- Wrapping the dock into extra lines on narrow widths: rejected because it creates an overly tall persistent header.
- Mobile bottom action bar plus top tabs: rejected because the user prefers one consistent interaction pattern across screen sizes.
- Keeping answer freshness proof inside the sticky dock: rejected because it makes the dock taller and risks obscuring the active tab's content.
- Making all helper surfaces sticky: rejected because it turns job-detail chrome into a stacked status wall instead of a compact control surface.

## Outstanding Questions

### Resolve Before Planning

- None.

### Deferred to Planning

- What is the cleanest visual treatment for horizontally scrollable pinned rows so it is obvious that more actions or tabs exist off-screen?
- Which job states should keep disabled placeholders versus fully hiding unavailable actions, if any edge cases still create unstable layout?
- What shared card language and spacing best distinguish helper surfaces from tab content without making the helper stack feel like a second header?

## Next Steps

-> `/prompts:ce-plan` for structured implementation planning of the sticky job-detail dock
