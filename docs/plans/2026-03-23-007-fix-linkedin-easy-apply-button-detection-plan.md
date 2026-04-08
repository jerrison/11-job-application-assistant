---
title: "fix: LinkedIn Easy Apply button not detected — false 'not_easy_apply'"
type: fix
status: completed
date: 2026-03-23
---

# fix: LinkedIn Easy Apply button not detected

## Problem

LinkedIn Easy Apply jobs are incorrectly reported as `"not_easy_apply"` with reason `"no_apply_button"`. The Easy Apply button IS present on the page (confirmed manually), but the Playwright selector fails to match it within the 5-second timeout.

Seen on: GMI Cloud — Inference Engine PM (job #255, LinkedIn URL: `https://www.linkedin.com/jobs/view/4354530385/`)

## Root Cause

The Easy Apply button selector at `autofill_linkedin.py:176-179` uses:
```python
'button.jobs-apply-button:has-text("Easy Apply"), '
'button[aria-label*="Easy Apply"]'
```

LinkedIn likely changed the button's CSS class or DOM structure. The selector no longer matches.

## Investigation Needed

The fix requires inspecting the actual DOM of a LinkedIn job page with Easy Apply to find the current selector. Options:

1. **Use Playwright to inspect**: Navigate to the LinkedIn job page, snapshot the DOM, and find the Easy Apply button's actual element structure
2. **Add broader selectors**: Add fallback selectors that are more resilient to LinkedIn UI changes (e.g., `:has-text("Easy Apply")` without class restriction, `a:has-text("Easy Apply")`)

## Proposed Fix

### `scripts/autofill_linkedin.py:176-179`

Broaden the Easy Apply button selector with additional fallbacks:

```python
easy_apply_btn = page.locator(
    'button.jobs-apply-button:has-text("Easy Apply"), '
    'button[aria-label*="Easy Apply"], '
    'button:has-text("Easy Apply"), '              # fallback: any button
    '.jobs-apply-button--top-card:has-text("Easy Apply"), '  # possible new class
    'div.jobs-apply-button--top-card button'       # container-based
).first
```

**Note:** The exact selector needs to be confirmed by inspecting the live DOM. The fallback `button:has-text("Easy Apply")` is the safest broad catch but could match wrong buttons if LinkedIn has multiple "Easy Apply" text elements.

## Acceptance Criteria

- [ ] The GMI Cloud job (#255) is detected as Easy Apply and enters the wizard
- [ ] Pre-submit screenshot is captured
- [ ] Existing Easy Apply jobs continue to work
- [ ] No false positives (non-Easy-Apply jobs shouldn't match)

## Sources

- Button detection: `scripts/autofill_linkedin.py:176-179`
- Not-Easy-Apply handler: `scripts/autofill_linkedin.py:188-192`
- Job result: `output/linkedin/gmi-cloud.../submit/application_submission_result.json`
