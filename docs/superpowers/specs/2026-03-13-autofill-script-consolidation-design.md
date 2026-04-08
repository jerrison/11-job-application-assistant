# Autofill Script Consolidation Design

## Problem

Five board-specific autofill scripts (`autofill_greenhouse.py`, `autofill_ashby.py`, `autofill_lever.py`, `autofill_gem.py`, `autofill_dover.py`) total ~10K lines with ~60-70% duplication. Bug fixes, LLM provider changes, and feature additions must be replicated across all scripts. Future boards (e.g. Workday) will compound this problem.

## Goals

- **Reduce maintenance burden**: bug fixes and features happen once
- **Reduce line count**: eliminate ~57% of code across the 4 smaller scripts
- **Preserve exact functionality**: zero behavior change
- **Stay flexible**: future boards with radically different flows (API-based, multi-page, etc.) must not be forced into an ill-fitting abstraction

## Scope

**In scope (this design):**
- Ashby (1,482 lines), Lever (1,300 lines), Gem (1,044 lines), Dover (663 lines)
- Two new shared modules: `autofill_common.py`, `autofill_pipeline.py`

**Out of scope (deferred):**
- Greenhouse (5,462 lines) — too large/complex, has its own diverged copies of many utilities, handles multiple form types (classic HTML + React). Consolidating it is a separate effort.

## Architecture: Composition Over Inheritance

We use a **layered composition** approach rather than class inheritance:

1. **`autofill_common.py`** — shared utility functions, board-agnostic
2. **`autofill_pipeline.py`** — standard browser-based orchestration as callable functions (opt-in)
3. **Each board script** — thin wrapper with board-specific logic only

This allows boards like Dover (API-based) or future Workday (possibly multi-page wizard) to cherry-pick shared utilities without inheriting a flow that doesn't fit.

### Design Principle

Every shared function takes board-specific behavior as **parameters** (selectors, patterns, button names) — not through config objects, registries, or inheritance. Simple function calls, no magic.

## Module: `autofill_common.py` — Shared Utilities

Pure/near-pure functions extracted from duplicated code across 2+ scripts.

### Functions

| Function | Signature | Extracted From |
|----------|-----------|---------------|
| `label_matches` | `(text_or_field: str \| dict, *fragments: str) -> bool` | All 5 scripts (3 signatures unified) |
| `select_option` | `(options: list[str] \| None, answer: str \| None, *, filter_select_prefix: bool = False) -> str \| None` | Dover, Lever |
| `capture_full_page` | `(page, path: Path, *, preferred_selectors: tuple[str, ...] = ()) -> None` | Ashby, Lever, Gem |
| `page_snapshot` | `(page, *, form_selector: str, captcha_type: str) -> dict` | Ashby, Lever, Gem |
| `classify_submit_state` | `(snapshot: dict, *, confirm_patterns, validation_patterns, captcha_key: str) -> dict` | Ashby, Lever, Gem |
| `click_submit_button` | `(page, *, button_names: tuple[str, ...]) -> bool` | Ashby, Lever, Gem |
| `yes_no_step` | `(field: dict, *, value: bool, source: str, option_matcher: Callable) -> dict \| None` | Ashby, Lever (see note below) |
| `choice_step` | `(field: dict, option: str \| None, *, source: str) -> dict \| None` | Lever (others inline it) |
| `write_report` | `(payload: dict, *, board_name: str, runtime: dict \| None = None) -> dict` | Ashby, Lever, Gem |
| `write_submit_debug_artifacts` | `(page, payload: dict) -> tuple[Path, Path]` | Lever (named fn), Gem (inline) |
| `wait_for_manual_captcha_resolution` | `(page, *, timeout, snapshot_fn, classify_fn, email_watcher) -> tuple` | Lever, Gem |
| `board_file_constants` | `(board_name: str) -> dict[str, str]` | All 4 (generates artifact filenames) |

### `label_matches` Unification

Currently three signatures with two matching strategies:
- Dover: `_label_matches(text: str, *fragments: str)` — operates on raw text, **substring containment**
- Ashby: `_label_matches(field: dict, *fragments: str)` — extracts `field.get("label")`, **substring containment**
- Gem: `_label_matches(field: dict, *fragments: str)` — extracts `field.get("label")`, **substring containment**
- Lever: `_label_matches(field: dict, *fragments: str)` — extracts `field.get("label")`, **alphanumeric-boundary regex matching** using `(?<![a-z0-9])...(?![a-z0-9])` (NOT `\b` — differs in underscore handling)

Unified signature: `label_matches(text_or_field: str | dict, *fragments: str, word_boundary: bool = False) -> bool`

If dict, extract text via `field.get("label", "")`. All current dict-based implementations use the `"label"` key. The `word_boundary` parameter preserves Lever's stricter matching semantics — when `True`, uses `(?<![a-z0-9])fragment(?![a-z0-9])` negative lookaround (replicating Lever's exact existing pattern). Lever call sites pass `word_boundary=True`, all others use the default `False` for substring containment.

### `yes_no_step` Divergence

Ashby and Lever both have `_yes_no_step` but differ in how they find selectable options:
- **Ashby**: calls `_match_selectable_label(field, ...)` which checks `field["field_type"]` being `"ValueSelect"` or `"MultiValueSelect"`
- **Lever**: calls `_select_option(field, ...)` and checks `field["kind"]` being `"select"`, `"radio"`, or `"checkbox"`

The shared `yes_no_step` takes an `option_matcher: Callable` parameter that each board provides — Ashby passes its Ashby-specific matcher, Lever passes its Lever-specific one. The shared function handles the common logic: building "Yes"/"No" candidate lists, constructing the step dict with the matched option.

### `select_option` Notes

Dover's signature: `(options: list[str] | None, answer: str | None)` — simple fuzzy match.
Lever's signature: `(field: dict, candidates: list[str])` — extracts options from field, filters out "select" prefixed options, accepts multiple candidates.

Unified: use Dover's cleaner signature. Add `filter_select_prefix: bool = False` for Lever. Lever call sites extract options from the field dict before calling the shared function.

### `page_snapshot` Parametrization

The JS evaluation is structurally similar across Ashby/Lever/Gem but with meaningful differences in what data is collected:

- **Form visibility selector**: `.ashby-application-form-field-entry` vs `#application-form` vs `.form-33`
- **Captcha type**: recaptcha (Ashby) vs hcaptcha (Lever, Gem)
- **Invalid field extraction**: Ashby and Lever extract `invalid_fields` via `[aria-invalid="true"]`; Gem does not collect `invalid_fields` at all
- **Captcha detail level**: Ashby and Lever distinguish `captcha_visible` from `captcha_challenge_active`; Gem only reports `hcaptcha_visible` (no challenge detection)

Approach: the unified JS template **always collects the full set of fields** (form_visible, captcha_visible, captcha_challenge_active, invalid_fields, errors). Gem's snapshot output will now include `invalid_fields` (empty array) and `hcaptcha_challenge_active` (false when no challenge iframe detected). This is safe because Gem's `_classify_submit_state` only checks `hcaptcha_visible` — the extra fields are ignored. This avoids conditional JS paths and makes all snapshots structurally identical.

Parametrize via `form_selector` and `captcha_type` arguments. The JS template interpolates these.

### `classify_submit_state` Parametrization

Same core logic (check confirmation patterns, check errors, check captcha, return status) but with three distinct orderings and captcha key differences:

- **Ashby**: confirmation → errors (including `invalid_fields`) → captcha (`recaptcha_challenge_active`) → pending
- **Gem**: confirmation → captcha (`hcaptcha_visible`) → errors (no `invalid_fields`) → pending
- **Lever**: confirmation → captcha-if-no-errors (`hcaptcha_challenge_active` AND `not invalid_fields`) → errors → captcha-again (`hcaptcha_challenge_active`) → pending

**Chosen approach:** Each board keeps its own `_classify_submit_state` inline (~15-20 lines each) and passes it as the `classify_state_fn` callback to `run_browser_pipeline`. The shared `page_snapshot` (which is the larger, more duplicated function at ~30-40 lines of JS each) still gets extracted. `autofill_common` provides helper predicates (`matches_confirm_patterns(page_text, patterns)`, `collect_validation_errors(snapshot, validation_patterns)`) that the per-board classify functions can call, but the ordering logic stays in each board.

## Module: `autofill_pipeline.py` — Browser Orchestration

Provides the standard browser-based autofill flow as a callable function. Boards opt in by calling it.

### `run_browser_pipeline`

```python
def run_browser_pipeline(
    payload_path: Path,
    *,
    headless: bool,
    submit: bool,
    # Board-specific hooks (required):
    form_ready_selector: str,
    fill_step_fn: Callable[[Page, dict], None],
    page_snapshot_fn: Callable[[Page], dict],
    classify_state_fn: Callable[[dict], dict],
    click_submit_fn: Callable[[Page], bool],
    capture_fn: Callable[[Page, Path], None],
    # Optional hooks:
    pre_submit_hook: Callable | None = None,
    post_navigate_hook: Callable | None = None,
) -> int:
```

**Flow:**
1. Load payload JSON from disk
2. Launch Chromium via `launch_chromium_browser()` (Steel or local)
3. Navigate to application URL
4. Wait for `form_ready_selector`
5. Loop through steps, calling `fill_step_fn(page, step)` for each
6. Capture pre-submit screenshot via `capture_fn`
7. Handle pending user input (write JSON, print instructions, wait)
8. Call `pre_submit_hook` if provided (e.g. Lever's hCaptcha pre-submit wait)
9. If `submit`: click submit via `click_submit_fn`, poll for confirmation
10. Build email watcher, poll Gmail + page state for confirmation
11. Call `sync_notion_after_submit()`
12. Return exit code

### `autofill_main`

```python
def autofill_main(
    board_name: str,
    build_payload_fn: Callable[[Path, str], dict],
    *,
    has_browser: bool = True,
    run_browser_fn: Callable[[Path, bool, bool], int] | None = None,
) -> int:
```

Handles: CLI argument parsing (including `--provider` with all current providers: `gemini`, `claude`, `codex`), env setup, payload building, report writing, payload JSON writing, dispatching to browser pipeline or returning early.

## Board Script Structure After Consolidation

Each board script contains only its unique logic:

### Ashby (~300-400 lines, from 1,482)
- Board constants (selectors, button names, captcha type, capture selectors)
- `_fetch_form_data(url)` — fetch HTML, extract `window.__appData`
- `_extract_fields(app_data)` — parse Ashby's field format
- `_infer_step(field, ...)` — field inference with Ashby's path-based matching
- `_fill_step(page, step)` — fill using Ashby CSS selectors
- `_build_payload(out_dir, provider)` — orchestrates the above
- `main()` — ~5 lines calling `autofill_main()`

### Lever (~250-350 lines, from 1,300)
- Same structure as Ashby
- Board-specific: live Playwright form inspection, hCaptcha pre-submit handling
- `pre_submit_hook` for captcha wait

### Gem (~200-300 lines, from 1,044)
- Same structure as Ashby
- Board-specific: live Playwright form inspection via custom JS

### Dover (~400 lines, from 663)
- Uses `autofill_common` utilities (label_matches, select_option, board_file_constants)
- Does NOT use `autofill_pipeline` (API-based submission)
- Keeps its own `_submit_payload()`, `_build_payload()`, API-specific logic
- Calls `autofill_main(..., has_browser=False)`

### Greenhouse (unchanged)
- No modifications in this pass
- Future consolidation is a separate effort

## Migration Strategy

**One board at a time, not big-bang.**

### Order
1. **Gem** (smallest browser-based) — proves the pattern, creates shared modules
2. **Lever** — validates captcha hook parametrization
3. **Ashby** — validates recaptcha vs hcaptcha handling
4. **Dover** — only uses `autofill_common`

### Per-Board Steps
1. Create/extend `autofill_common.py` and `autofill_pipeline.py` (Gem creates them, later boards extend)
2. Refactor the board script to import from new modules
3. Run existing automated tests — must pass
4. Generate a payload for a real job posting, diff against old code output — must be JSON-identical (modulo timestamps)
5. Run browser tests if they exist for that board
6. Commit — one commit per board for easy revert

### Risk Mitigation
- Each board is a separate commit — easy to revert without affecting others
- Old functions aren't deleted until migration is verified
- `autofill_common.py` is additive — no changes to `application_submit_common.py`
- No changes to `autofill_greenhouse.py`

## Testing Strategy

- **Automated tests**: must keep passing at every migration step
- **Payload diffing**: for each migrated board, generate payload with old code, save it, generate with new code, assert JSON equality (modulo timestamps)
- **Browser flow**: manual verification on at least one real posting per board (harder to automate)

## Estimated Impact

| Script | Before | After | Savings |
|--------|--------|-------|---------|
| autofill_gem.py | 1,044 | ~250 | ~794 |
| autofill_lever.py | 1,300 | ~300 | ~1,000 |
| autofill_ashby.py | 1,482 | ~350 | ~1,132 |
| autofill_dover.py | 663 | ~400 | ~263 |
| autofill_common.py | 0 | ~350 | +350 |
| autofill_pipeline.py | 0 | ~300 | +300 |
| **Net** | **4,489** | **~1,950** | **~2,539 (~57%)** |

Beyond line count, the primary win is **maintenance burden reduction**: bug fixes, new provider support, and feature additions happen once instead of 4-5 times.
