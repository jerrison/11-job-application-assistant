---
title: "Shared answer-refresh proof card obscured job-detail review content"
category: ui-bugs
module: Job Web UI
date: 2026-03-26
tags:
  - job-detail-view
  - web-ui
  - sticky-dock
  - answer-refresh
  - helper-stack
  - layout-regression
component: tooling
components:
  - scripts/static/index.html
  - scripts/static/style.css
  - scripts/static/app.js
  - tests/test_job_web.py
  - docs/board-architecture.md
  - docs/solutions/workflow-issues/explicit-answer-regeneration-requires-durable-fresh-proof-2026-03-26.md
problem_type: ui_bug
symptoms:
  - "Answers and generated assets appeared to disappear in the job-detail view."
  - "A large green answer-refresh proof panel inside the sticky dock obscured Answers, Resume, Cover Letter, and Screenshot content."
  - "The proof surface could render in the wrong place or twice because shared helper UI was split between the dock and the Answers tab."
root_cause: scope_issue
resolution_type: code_fix
severity: medium
---

# Shared Answer-Refresh Proof Card Obscured Job-Detail Review Content

## Problem

A job-detail web regression made it look like answers and generated assets had disappeared. The real failure was a layout collision: the answer-refresh proof surface was mounted inside the sticky dock, and the injected proof card inherited a global view-level `section` min-height rule, so the green `fresh` proof panel expanded into a large block that hid the real review content.

## Symptoms

- A large green box appeared in the job-detail view and pushed the actual Answers, Resume, Cover Letter, and Screenshot content out of view.
- The sticky dock stopped behaving like compact chrome and instead turned into a tall content panel.
- The proof surface could appear in more than one place in the web UI because the dock and the Answers tab both rendered proof blocks.
- Repeated tab switching made it easy to land back on proof UI instead of the tab's primary content.

## What Didn't Work

The broken setup was the combination of sticky placement, a broad tag selector, and duplicated proof rendering.

In `scripts/static/index.html`, the proof region lived inside the sticky dock:

```html
<div class="job-summary" id="job-header">
  <div class="job-title" id="job-title">Loading...</div>
  <div class="job-meta" id="job-meta"></div>
  <div class="answer-refresh-region" id="answer-refresh-proof"></div>
</div>
```

In `scripts/static/style.css`, the min-height rule applied to every nested `section`, not just top-level app views:

```css
section { min-height: calc(100vh - var(--nav-height) - 80px); }
```

In `scripts/static/app.js`, the injected proof card matched that selector:

```js
const card = document.createElement('section');
```

`loadAnswersTab()` also injected a second proof card into the Answers tab, so the same proof surface existed both in sticky chrome and inside tab content.

## Solution

The fix separated sticky chrome from helper surfaces and made the shared helper card the only web proof surface.

`scripts/static/index.html` now renders a dedicated helper stack below the sticky dock:

```html
<div class="job-detail-helper-stack" id="job-detail-helper-stack">
  <div class="board-url-bar" id="board-url-bar"></div>
  <div class="error-banner" id="error-banner"></div>
  <div class="progress-wrap" id="progress-wrap"></div>
  <div class="answer-refresh-region" id="answer-refresh-proof"></div>
</div>
```

`scripts/static/style.css` now scopes route sizing to top-level sections only:

```css
main > section { min-height: calc(100vh - var(--nav-height) - 80px); }
```

`scripts/static/app.js` now injects the proof card as a `div` and renders helper surfaces through one shared path:

```js
function buildAnswerRefreshCard(answerRefresh) {
  const card = document.createElement('div');
  card.className = 'answer-refresh-card';
  return card;
}
```

```js
function renderJobDetailHelpers(job) {
  const answerRefresh = document.getElementById('answer-refresh-proof');
  answerRefresh.innerHTML = '';
  const answerRefreshCard = buildAnswerRefreshCard(job.answer_refresh);
  if (answerRefreshCard) {
    answerRefresh.appendChild(answerRefreshCard);
  }
}
```

The implementation also:

1. Moved board URL, error, progress, and proof into `#job-detail-helper-stack` below `#job-detail-dock`.
2. Called `renderJobDetailHelpers(job)` from both `renderJobDetail()` and `updateJobDetailHeader()` so helper rendering is centralized instead of mixed into header rendering.
3. Removed Answers-tab proof injection from `loadAnswersTab()`, leaving one canonical `.answer-refresh-card`.
4. Extended tab-alignment logic to treat `#answer-refresh-proof` as a real helper anchor via visibility-aware helper detection.
5. Updated `tests/test_job_web.py` to assert the helper stack exists, the proof region is outside the dock, and `#progress-wrap` appears before `#answer-refresh-proof`.
6. Refreshed architecture and learning docs so they describe one shared non-sticky proof card below the dock across all tabs.

## Why This Works

The fix removes each part of the collision instead of patching only the visible symptom.

- The proof card no longer matches the global `section` min-height rule because route sizing is scoped to `main > section` and the injected card root is now a `div`.
- The sticky dock is back to core controls only, so helper surfaces can no longer expand sticky chrome into a blocking panel.
- There is now one canonical proof surface, which removes header/tab duplication and the synchronization drift that came with it.
- User-initiated tab switches align below the dock and any visible helper surface, so the first meaningful tab content stays reachable.

## Verification

- `node --check scripts/static/app.js`
- `uv run python -m pytest tests/test_job_web.py -v`
- Live browser verification against `http://127.0.0.1:8420/#job/405`

Browser verification confirmed:

- the shared proof card rendered below the sticky dock instead of inside it
- Answers, Resume, Cover Letter, and Screenshot content remained visible
- after 12 repeated tab switches, the page still had exactly 1 `.answer-refresh-card`
- `#tab-answers` contained 0 proof cards
- screenshots were saved under `output/playwright/job-405-desktop-answers.png`, `output/playwright/job-405-desktop-screenshot.png`, and `output/playwright/job-405-narrow-resume.png`

## Prevention

- Never use bare semantic selectors like `section { ... }` for route-level sizing. Scope layout rules to explicit containers such as `main > section` or view-specific classes.
- Treat sticky dock or header areas as chrome only. Helper, status, and proof UI should live in a dedicated normal-flow container with explicit ordering.
- For injected helper cards, default to `div` unless `section` semantics are truly needed and audited against global tag selectors.
- Keep DOM smoke assertions for helper-stack placement:

```python
dock_segment, helper_segment = resp.text.split('id=\"job-detail-helper-stack\"', maxsplit=1)
assert 'id=\"answer-refresh-proof\"' not in dock_segment
assert helper_segment.index('id=\"progress-wrap\"') < helper_segment.index('id=\"answer-refresh-proof\"')
```

- Keep browser regression assertions for repeated tab switching and proof-card uniqueness:

```js
document.querySelectorAll('.answer-refresh-card').length === 1;
document.querySelectorAll('#tab-answers .answer-refresh-card').length === 0;
```

- Use screenshots as the source of truth for layout regressions, especially for large tinted helper cards like the green `fresh` proof state.
- Keep answer-refresh lifecycle docs focused on freshness semantics and link out to UI-specific layout regressions instead of folding chrome-hardening lessons into workflow docs.

## Investigation Steps

1. Started from screenshots where a large green proof card appeared to hide all job-detail content.
2. Confirmed the underlying answers and generated assets still existed on disk and in the API response, so the issue was presentation, not data loss.
3. Traced the proof surface through `index.html`, `app.js`, and `style.css` and found the nested `<section>` plus global `section` min-height collision.
4. Hardened the layout boundary between sticky dock chrome and normal-flow helpers, then verified the fix with tests and repeated browser tab switching.

## Cross-References

- Brainstorm: `docs/brainstorms/2026-03-26-job-detail-sticky-dock-requirements.md`
- Brainstorm: `docs/brainstorms/2026-03-26-answer-regeneration-proof-requirements.md`
- Plan: `docs/plans/2026-03-26-009-fix-job-detail-chrome-hardening-plan.md`
- Related: `docs/solutions/workflow-issues/explicit-answer-regeneration-requires-durable-fresh-proof-2026-03-26.md`
- Related: `docs/solutions/logic-errors/visible-self-id-draft-blockers-2026-03-26.md`
- GitHub issues: no related issues found via `gh issue list --search "job detail sticky dock answer refresh proof card min-height web UI" --state all --limit 5`
