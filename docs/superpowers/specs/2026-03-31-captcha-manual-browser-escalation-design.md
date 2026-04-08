# Captcha Manual Browser Escalation

Date: 2026-03-31
Status: Approved for spec review

## Problem

Draft runs currently default to headless browser mode. When a board detects a captcha during a draft attempt, the shared submit pipeline returns `CAPTCHA_SKIP_EXIT_CODE`, the orchestrator maps that to `stopped`, and the browser process exits. That prevents the user from solving the captcha live even though the repo already has an `awaiting_captcha` status, focus-browser UI, and manual captcha wait path for headed runs.

## Goals

- Keep captcha handling generalized across boards and surfaces.
- Preserve draft-mode safety: never auto-submit.
- Let the user solve captchas live without restarting the job manually.
- Reuse the existing `awaiting_captcha` signal, UI affordances, and manual wait logic.

## Non-Goals

- Board-specific captcha solvers.
- Infinite captcha retry loops.
- Changing the default non-captcha draft experience to always-headed.

## Current Root Cause

The shared captcha wait helper already pauses for manual solve in headed mode, writes `awaiting_captcha.json`, and lets the orchestrator expose the job as `awaiting_captcha`. The gap is draft-mode launch policy: draft runs are headless by default, so captcha exits as `skipped` before that manual path can be used. The orchestrator then treats exit code `75` as a terminal stop.

## Options Considered

### 1. Auto-escalate only captcha-interrupted draft runs to headed mode

Recommended.

Pros:
- Preserves low-noise headless drafts for normal jobs.
- Uses the existing manual captcha infrastructure.
- Applies uniformly through the shared orchestrator path.

Cons:
- Requires one controlled rerun of the submit phase.

### 2. Make all draft runs headed

Pros:
- Simplest mental model.

Cons:
- Opens many unnecessary windows.
- Worse batch ergonomics.

### 3. Keep current behavior and only preserve already-headed sessions

Pros:
- Smallest code change.

Cons:
- Fails the user requirement for the common headless draft path.

## Selected Design

When a draft-mode submit attempt hits captcha while running headless, the shared orchestrator will immediately rerun the same submit phase once in headed mode instead of finalizing the job as `stopped`.

### Flow

1. Draft attempt starts in headless mode as it does today.
2. If the submit subprocess exits with `CAPTCHA_SKIP_EXIT_CODE` and the attempt was headless draft mode:
   - do not mark the job `stopped`
   - relaunch the same submit phase once without `--headless`
3. The headed rerun uses the existing `wait_for_captcha_resolution()` path.
4. While the captcha is present, `awaiting_captcha.json` keeps the job in `awaiting_captcha`, and the browser window remains open for manual solve.
5. If the user resolves the captcha and the draft completes, the job continues through the normal draft-proof path and lands in `draft`.

### Limits

- Only one automatic headless-to-headed escalation per draft attempt.
- If the headed rerun still does not complete because the captcha times out, the user closes the window, or the board keeps blocking progress, the job stops with an explicit captcha failure instead of looping forever.

### Status Behavior

- `autofilling` or `submitting` -> `awaiting_captcha` when the headed rerun writes the existing signal file.
- `awaiting_captcha` -> normal in-flight status when the signal clears.
- Final success remains `draft`.
- Final failure remains `stopped`, but only after the headed manual attempt fails.

### Surface Coverage

Because the change lives in the shared orchestrator and submit pipeline path, it applies to:

- CLI
- TUI
- web app
- LLM-driven runs that use the standard pipeline

### Error Handling

- Headless captcha on draft mode: escalate once to headed.
- Headed captcha timeout or unresolved manual challenge: stop with explicit captcha failure.
- Auto-submit flows are unchanged. This feature only alters draft-mode captcha behavior.

## Testing Strategy

Add failing tests first for:

- draft-mode headless captcha exit triggers a single headed rerun instead of immediate `stopped`
- the rerun preserves the existing `awaiting_captcha` status path
- a second captcha failure or timeout on the headed rerun stops cleanly without infinite retries
- non-draft and non-captcha paths remain unchanged

## Implementation Notes

- Prefer orchestrator-owned retry logic rather than board-specific patches.
- Reuse the current browser runtime so the manual browser window still lands in the expected user workspace behavior.
- Keep the final submit safety boundary unchanged: this is still draft mode, and visible submit controls remain a stop boundary rather than an automatic submit.
