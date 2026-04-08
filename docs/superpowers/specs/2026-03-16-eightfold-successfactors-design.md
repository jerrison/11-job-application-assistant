# Design: Eightfold AI + SAP SuccessFactors Board Support

**Date:** 2026-03-16
**Status:** Approved

## Summary

Add autofill support for two new job board platforms: Eightfold AI and SAP SuccessFactors. These are the two most common unsupported ATS platforms encountered in the pipeline. Each gets a new board-specific autofill script following existing architecture patterns.

## Board 1: Eightfold AI

### Complexity

Simple — single-page form, no auth. Same class as Ashby/Lever/Gem. Uses `run_browser_pipeline()`.

### URL Detection

- Hostname contains `eightfold.ai`
- Example: `paypal.eightfold.ai/careers?domain=paypal.com&query=R0132250&pid=274916506310`
- Application URL pattern: `/careers/apply?pid={pid}&domain={domain}`

### URL Canonicalization

Strip tracking/filter params (`Codes`, `sort_by`, `filter_distance`, `filter_include_remote`, `start`). Keep `pid`, `domain`, and `query` (requisition ID) as the canonical identifiers.

### Form Structure (Single Page)

1. **Resume** — file upload (pdf/doc/docx/txt), drag-and-drop zone with "Select file" button
2. **Contact Information** — First Name, Last Name, Email, Phone (country code combobox + number)
3. **Application Questions (My Information)** — comboboxes: "How did you hear about us?", "Are you a previous Employee of [company]?", "Country"
4. **Application Questions (Terms and Conditions)** — Privacy consent checkbox, NDA acknowledgment checkbox, Voluntary Disclosures (Veteran Status combobox, Gender combobox, Race/Ethnicity checkboxes, Disability section with Language combobox)
5. **Application Questions (Custom)** — varies by company. PayPal example: PEP-related comboboxes, work authorization combobox, sponsorship combobox, acknowledgment date picker
6. **Cancel / Submit application** buttons at bottom

### Deterministic Overrides

All standard overrides from `application_submit_common.py` apply:
- Work authorization → Yes (from `application_profile.md`)
- Sponsorship → No (from `application_profile.md`)
- Previous employee → No (deterministic)
- How did you hear → from `application_profile.md`
- PEP questions → No (deterministic)
- NDA/privacy checkboxes → check both (deterministic)
- Demographics → from `application_profile.md`
- Acknowledgment date → today's date (deterministic)
- Culture/careers opt-in → Yes (deterministic)

### Key Implementation Notes

- Comboboxes use Eightfold's custom widget (combobox + adjacent button to expand) — need to click the expand button, then select option from listbox
- Race/Ethnicity uses checkbox group (multi-select), not combobox
- Cookie consent banner may appear at page load — dismiss by clicking "Accept" before any form interaction
- Phone country code combobox — default to US (+1) if not pre-selected
- Initialize file constants via `board_file_constants("eightfold")` per convention
- Call `write_report()` to produce autofill report JSON/markdown after submission
- Navigation sections listed in left sidebar ("Resume", "Contact Information", etc.) are anchor links, not wizard pages — all sections visible on one page
- Resume upload uses drag-and-drop zone; use file input selector for Playwright `set_input_files()`

## Board 2: SAP SuccessFactors

### Complexity

High — auth-gated + multi-page wizard. Same class as Workday/iCIMS. Uses custom `run_browser_fn` callback.

### URL Detection

Two detection methods:

1. **Direct:** Hostname contains `successfactors.com` (e.g., `career4.successfactors.com/careers?company=supermicro`)
2. **Indirect (JD page probe):** Company-hosted JD pages (e.g., `jobs.supermicro.com/job/...`) where the "Apply now" link redirects to `successfactors.com`. Detect via:
   - Probe JD page HTML for `successfactors.com` or `j2w.Apply.init` or `j2w.init` strings
   - **Disambiguation with iCIMS:** The existing iCIMS fallback probe matches `talentcommunity` in HTML, which SuccessFactors also uses (via `/talentcommunity/apply/` paths). The SuccessFactors probe **must run before** the iCIMS fallback. Check for SuccessFactors-specific markers (`successfactors.com`, `j2w.Apply.init`) first; only fall through to iCIMS if those are absent. The iCIMS fallback's `talentcommunity` clause should be tightened to require `talentbrew` or `icims.com` alongside `talentcommunity`, not `talentcommunity` alone.

### URL Canonicalization

- JD URLs: strip `utm_source`, `utm_medium`, and other UTM/tracking params
- Keep the job ID in the path (e.g., `/job/.../1323446000/`)
- Application URLs contain `career_job_req_id` param — preserve this

### Auth Flow

Follows the same pattern as Workday (sign in first → password reset → create account):

1. Click "Apply now" on JD page → redirects to `career*.successfactors.com` sign-in page
2. **Try sign-in first** — fill Email Address + Password, click "Sign In"
3. **If sign-in fails → try password reset** — click "Forgot your password?" link, enter email, fetch reset link from Gmail (sender domain: `successfactors.com`), navigate to link, set new password, sign in with new password
4. **If no account exists → create account** — click "Create an account" link, fill:
   - Email Address (x2)
   - Password (x2) — requirements: 8-18 chars, at least one uppercase + one lowercase, at least one number or punctuation, no spaces or unicode
   - First Name, Last Name
   - Country/Region of Residence → "United States"
   - Notification checkbox (leave checked by default)
   - "Hear more about career opportunities" checkbox → check Yes (per culture/careers opt-in rule)
   - Terms of Use → click "Read and accept the data privacy statement" button
   - Click "Create Account"
5. Handle email verification if required (fetch verification code/link from Gmail)

### Credentials

Use env vars `SUCCESSFACTORS_EMAIL` and `SUCCESSFACTORS_PASSWORD` (fall back to `WORKDAY_EMAIL` / `WORKDAY_PASSWORD` if not set, matching iCIMS precedent). The password is a static env var used for both sign-in and account creation — not generated at runtime. Password constraints for account creation: 8-18 chars, mixed case, number/punctuation, no spaces/unicode.

### Auth Failure Handling

If sign-in fails AND password reset fails AND account creation fails:
- Write `submit/successfactors_auth_failure.json` with status, URL, company, role, reason
- Return `CAPTCHA_SKIP_EXIT_CODE` (75) for graceful batch skip
- Job status set to `skipped_auth_failure` in the database (matching Workday/iCIMS convention)
- Batch runs continue (not blocked)

### Multi-Page Wizard

**Provisional — will be finalized during implementation after browser exploration of 2-3 SuccessFactors instances.** Pages behind auth wall vary per company; based on SuccessFactors platform conventions, expect:

- **Personal Information** — name, address, phone, email
- **Resume / Experience** — resume upload, work history
- **Application Questions** — company-specific questions
- **Voluntary Disclosures** — EEO/OFCCP demographics (veteran, gender, race, disability)
- **Review / Submit** — final review page with submit button

Page detection via:
- Heading text inspection (h1/h2 containing "Personal Information", "Experience", etc.)
- Form field inspection (presence of resume upload, demographic fields, etc.)
- URL parameters if SuccessFactors uses them for page tracking

Max page attempts: 15 (safety limit, matching Workday).

### Deterministic Overrides

All standard overrides from `application_submit_common.py` apply (same as Eightfold). SuccessFactors-specific:
- Country/Region → "United States" (from `application_profile.md`)
- Former employee → No (deterministic)
- Terms of Use / privacy → accept (deterministic)

### Key Implementation Notes

- SuccessFactors uses SAP UI5 framework — standard HTML form elements with SAP-specific wrapper classes
- The sign-in page URL pattern: `career*.successfactors.com/careers?company={slug}`
- The create-account page URL pattern: `career*.successfactors.com/career?company={slug}&login_ns=register&...`
- Password reset URL pattern: `career*.successfactors.com/career?company={slug}&login_ns=forgot_pwd&...`
- The `career_job_req_id` parameter in URLs tracks which job the user is applying to through the auth flow
- Company slug extracted from `?company=` URL parameter
- JD pages use jQuery + j2w (jobs2web) framework for apply button initialization
- "Apply now" link path: `/talentcommunity/apply/{job_id}/?locale=en_US`
- "Data privacy statement" button during account creation — clicking it may open a modal; need to accept/close it to proceed. Explore exact behavior during implementation.
- Initialize file constants via `board_file_constants("successfactors")` per convention
- Call `write_report()` to produce autofill report JSON/markdown after submission
- On early-exit confirmation paths (e.g., "already applied"), call `write_report()` before returning (per Phenom precedent)

## Integration Points

### Board Detection (`scripts/job_board_urls.py`)

Add:
- `EIGHTFOLD_HOST_PATTERN` — `eightfold.ai`
- `SUCCESSFACTORS_HOST_PATTERNS` — `successfactors.com`
- `looks_like_eightfold_url(url)` — check hostname contains `eightfold.ai`
- `looks_like_successfactors_url(url)` — check hostname contains `successfactors.com`
- URL canonicalization functions for both boards
- Add both boards to `resolve_job_source_url()` dispatch chain (consistent with Lever/Workday/iCIMS/Phenom/Ashby)

### Board Routing (`scripts/submit_application.py`)

Add `"eightfold"` and `"successfactors"` to `_board_for_url()` dispatch:
- Eightfold → `autofill_eightfold.py` via `run_browser_pipeline()`
- SuccessFactors → `autofill_successfactors.py` via custom `run_browser_fn`

For SuccessFactors indirect detection: if URL doesn't match `successfactors.com` hostname, probe JD page HTML for SuccessFactors markers (same pattern as iCIMS fallback). **SuccessFactors probe must run before iCIMS probe** to avoid `talentcommunity` collision (see URL Detection section above).

### Worker Detection (`scripts/job_worker.py`)

Add to `_BOARD_PATTERNS` dict:
- `"eightfold.ai"` → `"eightfold"`
- `"successfactors.com"` → `"successfactors"`
- **Drive-by fix:** Add missing `"phenom.com"` → `"phenom"` pattern (pre-existing omission)

### Gmail Integration (SuccessFactors only)

Reuse existing Gmail watcher pattern from Workday:
- Watch for emails from `successfactors.com` domain for account verification + password reset links
- Watch for application confirmation emails

### New Files

- `scripts/autofill_eightfold.py` — Eightfold autofill (simple, `run_browser_pipeline()`)
- `scripts/autofill_successfactors.py` — SuccessFactors autofill (auth + wizard, custom `run_browser_fn`)
- `tests/test_eightfold_autofill.py` — Eightfold tests (URL detection, canonicalization, board routing, deterministic overrides, combobox/checkbox field mapping)
- `tests/test_successfactors_autofill.py` — SuccessFactors tests (URL detection, canonicalization, board routing, auth flow states, page detection, deterministic overrides, auth failure handling)

### Modified Files

- `scripts/job_board_urls.py` — URL detection + canonicalization for both boards
- `scripts/submit_application.py` — board dispatch for both boards
- `scripts/job_worker.py` — board patterns for rate limiting
- `tests/test_url_resolver.py` — URL detection tests
- `tests/test_submit_application.py` — board dispatch tests
- `docs/board-architecture.md` — document both boards
- `docs/autofill-patterns.md` — SuccessFactors-specific gotchas
- `CLAUDE.md` — update URL canonicalization section
