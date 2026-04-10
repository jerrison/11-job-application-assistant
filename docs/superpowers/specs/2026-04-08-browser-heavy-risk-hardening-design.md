# Browser-Heavy Residual Risk Hardening Design

## Goal

Reduce the highest remaining regression risk in browser-heavy runtime paths by adding focused tests around shared browser runtime behavior first, then draft-proof review-state edges if time permits in the same pass.

## Why This Slice

The previous hardening pass improved validator and summary coverage, but the remaining risk is concentrated in code that coordinates live browser behavior and fail-closed draft review rules. Those paths are shared across many boards, so a small regression there can invalidate multiple flows at once.

The highest-leverage target is `scripts/browser_runtime.py` because it owns shared behaviors used by headed and headless runs, including:

- human-style field filling fallback behavior
- Google session detection and interactive re-auth flow
- AppleScript-based browser window management on macOS
- Chromium launch attempt ordering and environment isolation
- monitor/window-origin detection used to keep browser sessions visible and recoverable

The second-best target is `scripts/pipeline_draft_proof.py`, which decides whether a draft is `ready`, `blocked`, `stale`, `legacy`, or `unavailable`. That module is browser-adjacent rather than browser-driving, but it enforces the final fail-closed contract for current-attempt proof.

## Constraints

- Keep this pass narrow enough to finish with fresh repo-wide verification.
- Prefer shared runtime and state-machine coverage over board-specific tests.
- Follow TDD for any production changes: write failing tests first, verify the failure, then apply the minimal fix.
- Only modify production code if the new tests reveal a real defect.
- Preserve the repo's current fail-closed behavior in draft mode.

## Options Considered

### Option 1: Shared Browser Runtime Hardening

Add focused tests to `tests/test_browser_runtime.py` for under-covered branches in `scripts/browser_runtime.py`.

Pros:

- Highest leverage across many boards and launch modes
- Mostly unit-style tests with controllable fakes and mocks
- Fastest route to meaningful confidence gain in browser-heavy code

Cons:

- Does not directly exercise draft review state transitions

### Option 2: Draft-Proof State Hardening

Add focused tests to `tests/test_pipeline_draft_proof.py` for stale, legacy, blocked, and ready transitions.

Pros:

- Protects the final draft review boundary directly
- High product value because it governs whether draft mode stops safely

Cons:

- More review-contract oriented than browser-runtime oriented
- Slightly narrower blast radius than shared runtime coverage

### Option 3: Submit Orchestration Hardening

Add tests deeper in `submit_application.py` or `application_submit_common.py`.

Pros:

- Broad orchestration surface

Cons:

- Very large files with slower iteration
- Higher risk of spending time on glue code rather than the most failure-prone browser edges

## Recommended Design

Use a two-step hardening pass:

1. Harden `scripts/browser_runtime.py` first.
2. If the first slice finishes cleanly, add a smaller follow-on pass for `scripts/pipeline_draft_proof.py`.

This keeps the work anchored on shared browser behavior while still covering the fail-closed review boundary if the first pass leaves enough room.

## Design Details

### 1. Browser Runtime Test Expansion

Extend `tests/test_browser_runtime.py` with explicit branch coverage for:

- `ensure_google_session(...)`
  - signed-in state returns early and logs the active session
  - expired session in headless mode warns without attempting interactive re-auth
  - expired session in headed mode opens the sign-in page and exits after detecting a restored session
  - headed mode timeout path logs a warning and still returns cleanly
- `_run_osascript(...)`
  - returns `False` on non-macOS
  - returns `False` when subprocess execution raises
  - returns `True` only when `osascript` exits successfully
- `focus_chromium_window(...)`
  - builds the generic first-window AppleScript when no title substring is provided
  - builds the filtered window-selection AppleScript when a title substring is provided
- `_detect_webapp_screen_origin(...)`
  - returns `None` off macOS
  - returns `None` when AppleScript/JXA output is empty or malformed
  - returns the matching screen origin when both window coordinates and screen frames resolve successfully

These tests should stay unit-style, using fake page objects and patched subprocess calls rather than real browser launches.

### 2. Draft-Proof Follow-On Coverage

If the browser-runtime slice stays small and green, extend `tests/test_pipeline_draft_proof.py` with state-transition cases for:

- `legacy` state when only legacy artifact sources are active
- `legacy` state for already-confirmed submissions missing only the now-required distinct review screenshot
- `stale` state when historical proof exists but the active attempt remains blocked
- `ready` state when required current-attempt proof exists and there are no blockers

These tests should isolate draft state computation by writing only the minimum output-tree artifacts required for each case.

## Production Change Policy

Production changes are not part of the design by default. They are allowed only if the new red tests reveal an actual defect in:

- browser session handling
- AppleScript helper behavior
- window-origin detection
- draft-proof state classification

Any fix should be minimal and local to the failing branch.

## Verification Plan

At minimum:

- targeted runs for the browser-runtime and draft-proof test files after each red-green cycle
- `uv run ruff check scripts/ tests/`
- `uv run python scripts/check_architecture.py`
- `uv run python scripts/check_agent_docs.py`
- `uv run python scripts/sync_agent_files.py --check`
- `uv run python -m pytest tests/ -v`

## Out Of Scope

- board-specific autofill regressions
- end-to-end live browser runs against external job boards
- large refactors inside `application_submit_common.py` or `submit_application.py`
- coverage chasing in unrelated low-coverage utility scripts

## Success Criteria

This pass is successful if:

- shared browser-runtime edge cases have direct regression tests
- draft-proof state transitions gain coverage if the follow-on slice fits
- any real defects exposed by those tests are fixed
- the full repo verification suite passes afterward
