# LinkedIn Easy Apply Board — Design Spec

**Date:** 2026-03-20
**Status:** Approved

## Summary

Add LinkedIn Easy Apply as a new board (`"linkedin"`) in the existing autofill architecture. Jobs that have Easy Apply (no external board URL) will be handled directly on LinkedIn instead of going to `needs_board_url`.

## Approach

LinkedIn becomes just another board — same pipeline, same draft/submit flow, same answer generation. The multi-step wizard complexity lives entirely inside `autofill_linkedin.py`.

- **Single shared profile**: Uses `.playwright-linkedin/` with file lock — serialized, one Easy Apply at a time
- **Reuses existing infrastructure**: `autofill_pipeline.py`, `application_submit_common.py`, answer generation, screenshot capture, draft/submit flow

## 1. URL Resolution Changes

### `url_resolver.py`

`_extract_linkedin_apply_url()` currently returns `None` for Easy Apply jobs. Change:

- If the page has an "Easy Apply" button (not "Apply on company website"), return the original LinkedIn job URL itself as the board URL
- Add `linkedin.com` to `_is_known_board_url()` so the returned URL is recognized as a valid board URL and does not get re-resolved via `_resolve_company_url_to_board()`
- `detect_source()` continues to return `"linkedin"` — no change there. The change is in `_resolve_via_browser()` which now returns a valid URL instead of `None`

### `pipeline_orchestrator.py`

Phase 1 URL resolution:
- When `resolve_to_board_url()` returns a `linkedin.com/jobs/view/` URL, `_detect_board_from_url()` maps it to `"linkedin"` board
- Add `"linkedin": ("linkedin.com/jobs/view",)` to the fallback `board_patterns` dict in `_detect_board_from_url` (in case `_board_for_url()` import fails)
- Job proceeds to Phase 2 (asset generation) and Phase 3 (autofill) instead of stopping at `needs_board_url`
- Skip `mark_linkedin_job_applied()` in post-submit when `board == "linkedin"` — redundant since submission happened on LinkedIn itself

## 2. Board Registration

### `submit_application.py`

- Add `LINKEDIN_HOST_PATTERNS = ("linkedin.com",)` to `_board_for_url()` — must be placed BEFORE the Greenhouse fallback + HTML probing section so it matches early by hostname
- Add `"linkedin"` → `autofill_linkedin.py` in `_script_for_board()`

### `job_board_urls.py`

- Add `looks_like_linkedin_easy_apply_url()` — matches `linkedin.com/jobs/view/` URLs
- Add `canonical_linkedin_job_url()` — strip query params (`?currentJobId=`, `?refId=`, `?trk=`, etc.), keep just the `/jobs/view/{id}/` path

## 3. Autofill Script — `autofill_linkedin.py`

### Entry Point

Uses `autofill_main(board_name="linkedin", build_payload_fn=..., run_browser_fn=...)` for consistency with Workday/Phenom. Handles CLI argument parsing, payload writing, `--payload-only` mode automatically.

### Payload Structure (`_build_payload`)

LinkedIn's wizard steps are dynamic and unknown until the modal is opened. The payload is minimal:

```python
{
    "board": "linkedin",
    "job_url": str,              # LinkedIn job page URL
    "out_dir": str,
    "job_title": str,
    "company": str,
    "candidate_name": str,
    "candidate_email": str,
    "candidate_phone": str,
    "resume_path": str,          # path to generated resume PDF
    "cover_letter_path": str,    # path to generated cover letter PDF (may be None)
    "artifacts": { ... },        # standard board_file_constants("linkedin")
    "steps": [],                 # empty — discovered at runtime inside browser
    "fields": [],                # empty — discovered at runtime
    "unknown_questions": [],
}
```

Field discovery and step building happen inside `run_browser_fn` as the wizard pages are navigated. This matches the pattern used by Workday for dynamic multi-step forms.

### Why Custom Pipeline

LinkedIn Easy Apply is a **multi-step modal wizard**, not a single-page form. Like Greenhouse, it needs a custom `_run_playwright()` instead of the generic `run_browser_pipeline()`.

### Browser Profile — NOT the Standard Submit Profile

LinkedIn Easy Apply must NOT use `launch_chromium_browser()` with `submit_browser_profile_dir()`. Instead, it launches a separate Playwright persistent context from `.playwright-linkedin/` directly, as the existing `_resolve_linkedin_with_lock()` does. Reason: LinkedIn requires its own session cookies (login state) separate from the Google session profile (`~/.job-assets/playwright-submit-profile`) used by other boards.

`ensure_google_session()` is skipped for LinkedIn Easy Apply since it runs in a separate profile without Google sign-in.

### Wizard Flow

1. Acquire file lock on `.playwright-linkedin.lock`
2. Launch persistent browser context from `.playwright-linkedin/`
3. Navigate to LinkedIn job page
4. Handle auth wall if session expired (`_ensure_linkedin_logged_in()`)
5. Check for "Already applied" — if detected, skip gracefully (mark `submitted` with note, check on the job page text/badges before clicking Easy Apply)
6. Click "Easy Apply" button to open the modal
7. Loop through wizard steps:
   - Snapshot current step's fields (inputs, selects, radios, checkboxes, file uploads)
   - Match fields to answers (candidate profile, application profile, or LLM-generated)
   - Fill fields, upload resume PDF
   - Uncheck "Follow company" if present
   - Capture per-step screenshot to `linkedin_autofill_pages/page_01.png`, `page_02.png`, etc.
   - Click "Next" (or "Review" on last step)
8. At the review step:
   - **Draft mode** (`--draft`): capture screenshot, write autofill report, exit 0
   - **Submit mode** (`--submit`): capture screenshot, click "Submit application", wait for confirmation, capture post-submit screenshot
9. Release file lock and close browser

### Step Count

Do NOT hardcode step count. Loop until "Review" or "Submit application" step is detected. LinkedIn varies the number of steps per job.

### Headed vs Headless

- **Draft mode**: headed (not headless). LinkedIn Easy Apply is more likely to trigger challenges/2FA than URL resolution. Headed draft avoids silent failures from auth issues.
- **Submit mode**: headed (same as all other boards in submit mode).

### Standard Artifacts

Uses `board_file_constants("linkedin")`:
- `linkedin_autofill_report.json` / `.md`
- `linkedin_autofill_pre_submit.png`
- `linkedin_autofill_post_submit.png`
- `linkedin_autofill_payload.json`
- `linkedin_autofill_pages/` — per-step screenshots (`page_01.png`, `page_02.png`, etc.)
- `linkedin_unknown_questions.json`

## 4. Field Handling

| Field Type | Source | Handling |
|---|---|---|
| Name, Email, Phone | `CandidateProfile` | Verify pre-filled values; overwrite only if empty or incorrect |
| Resume | Generated PDF | Upload from `output/{company}/{role}/` |
| Location | `ApplicationProfile` | "San Francisco, CA" for Bay Area jobs |
| Work authorization | `ApplicationProfile` | Standard sponsorship/auth answers |
| Years of experience | LLM-generated | Match to dropdown/radio options via `select_option()` |
| Skill-specific questions | LLM-generated | Via `generate_application_answers()` |
| "How did you hear" | Hardcoded | "LinkedIn" |
| "Follow company" checkbox | Hardcoded | **Always uncheck** |
| "Priority" marking | Hardcoded | **Never mark** |
| Salary expectations | Hardcoded | Deflect — "open and flexible" |
| Cover letter (optional) | Generated PDF | Upload if field present (rare on LinkedIn) |

### Pre-filled Fields

LinkedIn often pre-fills contact info from the user's profile. Check existing values before overwriting — only fill if empty or incorrect.

### Dropdown/Combobox Strategy

Click to expand, read options, pick best match. No type-to-filter.

## 5. Browser Profile & Concurrency

- Uses existing `.playwright-linkedin/` persistent profile — NOT `~/.job-assets/playwright-submit-profile`
- File lock (`.playwright-linkedin.lock`) held for the entire browser session (open → close)
- **Serialized**: Only one LinkedIn Easy Apply job runs at a time across all workers
- Other workers continue processing non-LinkedIn jobs concurrently
- Auth handled by existing `_ensure_linkedin_logged_in()` using `LINKEDIN_EMAIL` / `LINKEDIN_PASSWORD` env vars
- `ensure_google_session()` is NOT called — separate profile, no Google sign-in

## 6. Error Handling

| Scenario | Handling |
|---|---|
| LinkedIn session expired | `_ensure_linkedin_logged_in()` — reuse existing |
| "Easy Apply" button not found | Job taken down or changed. Exit with skip code, job → `stopped` |
| Unexpected step count | Loop until "Review"/"Submit" detected — no hardcoded count |
| reCAPTCHA challenge | Use `wait_for_captcha_resolution()` — headed browser, macOS notification, `awaiting_captcha` |
| "Already applied" detected | Detect on job page (badges/text) before clicking Easy Apply. Skip gracefully — mark as `submitted` with note in `application_submission_result.json` (`website_confirmed: true`) |
| File lock contention | Workers block on `fcntl.flock()` — serialized by design |
| Modal closes unexpectedly | Detect via missing modal selector, retry opening once, then fail |

## 7. Files to Create/Modify

### New Files
- `scripts/autofill_linkedin.py` — Board autofill script with custom wizard pipeline
- `tests/test_autofill_linkedin.py` — Unit tests for LinkedIn board

### Modified Files
- `scripts/url_resolver.py` — Return LinkedIn URL for Easy Apply instead of `None`; add LinkedIn to `_is_known_board_url()`
- `scripts/submit_application.py` — Register `"linkedin"` board + script mapping (before HTML probing fallback)
- `scripts/job_board_urls.py` — Add `looks_like_linkedin_easy_apply_url()` + `canonical_linkedin_job_url()`
- `scripts/pipeline_orchestrator.py` — Recognize `linkedin.com` in `_detect_board_from_url()` fallback; skip `mark_linkedin_job_applied()` when `board == "linkedin"`
- `CLAUDE.md` — Document LinkedIn Easy Apply in autofill patterns
- `docs/autofill-patterns.md` — Add LinkedIn Easy Apply section
