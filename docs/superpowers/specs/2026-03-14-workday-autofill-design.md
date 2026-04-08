# Workday Board Autofill — Design Spec

**Date:** 2026-03-14
**Status:** Draft

## Problem

Workday (`*.myworkdayjobs.com`) is a major ATS with no existing autofill support. Unlike single-form boards (Lever, Gem, Ashby), Workday requires account authentication and uses a multi-page wizard for applications.

## Design Decisions

### 1. Architecture: Custom `run_browser_fn` (like Dover)

Workday uses `autofill_main` with a **custom `run_browser_fn`** callback rather than the standard `run_browser_pipeline`. Reasons:
- Multi-page wizard doesn't fit the single-form navigate→fill→submit model
- Auth pre-step (login/signup) has no equivalent in other boards
- Page-by-page filling requires sequential navigation with "Next"/"Continue" buttons

All shared utilities are still imported from `autofill_common.py` and `application_submit_common.py`.

### 2. URL Handling

**Canonicalization** (`job_board_urls.py`):
- Strip `?source=LinkedIn` and similar query params from Workday URLs for JD scraping
- Keep the full path (no `/apply` suffix to strip — Workday uses the same URL)

**Company detection** (`run_pipeline.py`):
- Workday URLs use subdomain pattern: `factset.wd108.myworkdayjobs.com`
- URL-derived company slug should take priority over text-derived for Workday, since page titles like "FactSet Careers" produce incorrect slugs like "factset-careers"

**Board detection** (`submit_application.py`):
- Add `WORKDAY_HOST_PATTERNS = ("myworkdayjobs.com",)` to `_board_for_url()`

### 3. Authentication Flow

Workday requires login before applying. The flow:

1. Navigate to job URL, click "Apply"
2. Detect auth state:
   - **Sign-in page visible**: Try login with configured credentials
   - **Create account page**: Create new account
   - **Already authenticated**: Proceed to form
3. Login credentials:
   - Email: from `application_profile.md` `Verification Code Email` field (jerrisonli@gmail.com)
   - Password: from env var `WORKDAY_PASSWORD` (default: `ComidaComida1@!`)
4. If login fails (wrong password):
   - Click "Forgot Password"
   - Use `gws` CLI to fetch password reset email
   - Extract reset link, navigate to it
   - Set new password using configured value
5. If account creation requires email verification:
   - Use `gws` CLI to fetch verification code/link
   - Complete verification

### 4. Multi-Page Form Filling

Workday applications have sequential pages. Each page is filled before clicking "Next":

| Page | Fields | Source |
|------|--------|--------|
| **My Information** | Name, email, phone, address, country | `master_resume.md`, `application_profile.md` |
| **My Experience** | Resume upload (auto-parses) | `find_resume_file()` |
| **Work Experience** | Pre-filled from resume parse; verify/skip | Skip if auto-filled |
| **Education** | Pre-filled from resume parse; verify/skip | Skip if auto-filled |
| **Voluntary Self-ID** | Gender, race, veteran, disability | `application_profile.md` |
| **Custom Questions** | Role-specific open-ended questions | LLM-generated via `generate_application_answers()` |
| **Review** | Verify all fields | Screenshot before submit |
| **Submit** | Click submit | Standard confirmation flow |

**Page detection strategy**: Check for distinctive headings/sections on each page to determine the current step and which fill logic to apply.

### 5. Form Element Handling

Workday uses custom React-based form controls:
- **Text inputs**: Standard `input[type="text"]` — use `human_fill()`
- **Custom dropdowns**: Click to open, wait for options, click matching option (similar to OFCCP combobox strategy from `application_profile.md` feedback)
- **File upload**: Standard `input[type="file"]` — use `set_input_files()`
- **Date pickers**: May need special handling (type date string directly)
- **Radio buttons/checkboxes**: Click matching labels

### 6. State Classification

Confirmation patterns for Workday:
- "Thank you for applying" / "Your application has been submitted"
- "Application received" / "Successfully submitted"

Validation error patterns:
- "This field is required"
- "Please enter" / "Please select"
- Form validation error indicators

No captcha expected on Workday (they use account auth instead).

### 7. File Structure

```
scripts/
  autofill_workday.py      — Board-specific: auth, page detection, fill, state classification
  job_board_urls.py         — Add Workday URL canonicalization
  submit_application.py     — Add Workday board detection
```

### 8. Artifacts

Standard board artifact pattern (via `board_file_constants("workday")`):
- `workday_autofill_payload.json`
- `workday_autofill_report.json` / `.md`
- `workday_autofill_pre_submit.png`
- `workday_autofill_pages/`
- `workday_submit_debug.html` / `.png`

## Implementation Plan

1. **URL/board detection** — Add Workday to `job_board_urls.py`, `submit_application.py`, fix company detection priority in `run_pipeline.py`
2. **Auth module** — Login/signup/password-reset flow with `gws` CLI integration
3. **Page-by-page fill** — Detect current page, fill deterministic fields, handle custom controls
4. **LLM answers** — Generate answers for custom questions using existing `generate_application_answers()`
5. **Submit and confirm** — Click submit, poll for confirmation text, check email
6. **Integration** — Wire into `autofill_main` with custom `run_browser_fn`, add tests
7. **Instructions** — Update CLAUDE.md, AGENTS.md, GEMINI.md

## Scope Constraints

- **No resume auto-parse correction**: If Workday auto-fills from resume upload, accept it rather than trying to fix misparses
- **No multi-application batch**: One job at a time (same as other boards)
- **Skip captcha**: Per project policy, if captcha is encountered, skip and return 0
