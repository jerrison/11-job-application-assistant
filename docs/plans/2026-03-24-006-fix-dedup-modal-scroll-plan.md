---
title: "fix: Dedup modal not scrollable — background scrolls instead"
type: fix
status: completed
date: 2026-03-24
---

# fix: Dedup modal not scrollable — background scrolls instead

The Duplicate Jobs modal with 15+ groups overflows the viewport but cannot be scrolled. Scroll events pass through to the background page instead.

## Root Cause

Classic flexbox overflow gotcha. `.modal-body` has `overflow-y: auto` but lacks `min-height: 0`. Flex children default to `min-height: auto`, which prevents them from shrinking below their content height — so the overflow never activates.

Additionally, the background page scroll is not locked when the modal is open.

## Acceptance Criteria

- [ ] Dedup modal body scrolls when content exceeds 80vh
- [ ] Background page does not scroll while modal is open
- [ ] Fix applies to all modals (dedup, add jobs, review changes, interview prep) — not just dedup

## Fix

### `scripts/static/style.css` — add `min-height: 0` to `.modal-body` (~line 1265)

```css
.modal-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
  min-height: 0;  /* ← required for flex overflow to activate */
}
```

### `scripts/static/style.css` — add body scroll lock class

```css
body.modal-open {
  overflow: hidden;
}
```

### `scripts/static/app.js` — toggle body class on modal open/close

Add `document.body.classList.add('modal-open')` when any modal opens, and `.remove('modal-open')` when it closes.

## Sources

- Modal CSS: `scripts/static/style.css:1230-1269`
- Modal HTML: `scripts/static/index.html:637-648`
- Dedup open/close: `scripts/static/app.js:3512-3525` (`runDedup`, `closeDedupModal`)
