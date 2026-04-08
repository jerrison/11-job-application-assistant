# Shared Inputs

Required inputs for every job application session. These are the source files, templates, and scraping instructions that all providers (Claude, Gemini, Codex/GPT) must use when generating application materials or filling forms.

Referenced from `AGENTS.md`. Do not duplicate this content elsewhere.

---

## 1. Job Posting -- at least one of:
- A **URL** to the job posting (scrape it using the scraper script -- see below)
- **Pasted text** of the job description
- **Both** (prefer pasted text if they conflict; use URL for supplemental context)

### Scraping Job Postings from URLs

When the user provides a job posting URL, use `scripts/scrape_job.py` to extract the job description:

```bash
# Standard fetch (works for most job boards: Greenhouse, Lever, Ashby, Dover, etc.)
uv run python scripts/scrape_job.py "https://boards.greenhouse.io/company/jobs/12345"

# Stealth mode (auto-enabled for LinkedIn, Indeed, Glassdoor, Workday)
uv run python scripts/scrape_job.py "https://linkedin.com/jobs/view/12345" --stealth

# Save to file
uv run python scripts/scrape_job.py "https://example.com/jobs/123" --output jd.md
```

The script uses [scrapling](https://scrapling.readthedocs.io/en/latest/) with automatic fetcher selection:
- **Fetcher** -- fast HTTP with browser impersonation (default for most sites)
- **StealthyFetcher** -- anti-bot bypass with fingerprint spoofing (auto-enabled for protected sites, or use `--stealth`)

If the script is missing dependencies, it should bootstrap via the project's `uv` environment rather than trying to `pip install` into a system Python. If the first fetched page is an application shell or otherwise lacks a usable JD, the scraper should search same-site canonical or linked pages for the fuller posting before failing. If `scrapling` and the structured HTML fallback both fail, it may optionally use Cloudflare Browser Rendering's crawl endpoint when `CLOUDFLARE_ACCOUNT_ID` plus `CLOUDFLARE_BROWSER_RENDERING_API_TOKEN` (or `CLOUDFLARE_API_TOKEN`) are configured. If extraction still returns insufficient content, stop and fail rather than continuing with weak content; fall back to asking the user to paste the job description text.

## 2. Resume Template
- Always use this Google Doc as the resume template (for formatting/fonts/margins): https://docs.google.com/document/d/1qgyQ-pTmvXpWJnAoam6CLsvX1i1KJlp0Jk8FqwsOihY/edit?tab=t.0
- Do NOT ask the user for the resume link -- it is always the one above.

## 3. Master Resume (Bullet Pool)
- Use `master_resume.md` (in this project directory) as the **superset of all available bullet points** for each position.
- This file contains every bullet the candidate has ever written for each role. When tailoring, **select from this pool** -- you are not limited to the bullets currently on the Google Doc.
- The master resume is also the source of truth for the candidate's background when writing the cover letter.
- Do NOT ask the user for this file -- it is always `master_resume.md` in the project root.

## 4. Work Stories (Narrative Color)
- Use `work_stories.md` (in this project directory) for **additional narrative depth and behavioral evidence**.
- These stories provide specific context, stakeholder dynamics, and problem-solving approaches that go beyond bullet-point metrics.
- **For cover letters**: weave in story details to make claims concrete and human (e.g., "built a prototype in three days to prove feasibility" is more compelling than "delivered quickly").
- **For resumes**: use story details to enrich bullet rewrites with specificity (e.g., referencing the 4,000 test scenarios, the $15M account save, the compatibility layer approach).
- Stories are NOT a substitute for `master_resume.md` -- they supplement it. All core accomplishments and metrics still come from the master resume.
- **Before each job application**, re-fetch the source Google Doc to check for new or updated stories. The orchestrator (`run_pipeline.py`) does this automatically at the start of every run (hash-check, update only if changed). Use `--skip-sync` to skip when iterating quickly.
- Source: https://docs.google.com/document/d/1jjR7eekdzvTHkiTSMUShSpZ9ebBAfCmrGrxgD3S3EpQ/edit?tab=t.0

## 5. Candidate Context (Supplemental Background)
- Use `candidate_context.md` (in this project directory) for **additional context about the candidate's motivations, preferences, narrative voice, and broader background**.
- This file is supplemental. It can improve tailored reasoning and make answers more grounded, but it does **not** override `master_resume.md` as the source of truth for accomplishments, metrics, work history, education, or skills.
- **Before each job application**, re-fetch the source Google Doc to check for updates. The orchestrator (`run_pipeline.py`) does this automatically at the start of every run (hash-check, update only if changed). Use `--skip-sync` to skip when iterating quickly.
- Use this file to improve relevance and tone, not to surface private facts indiscriminately.
- **Never include sensitive or irrelevant personal details** from this file in resumes or cover letters unless the user explicitly asks and the detail is materially relevant.
- Source: https://docs.google.com/document/d/1tFu7zKQkxo7q3Ar4VXrqs7RT9YoiPF_4NXCsAADJD0c/edit?tab=t.0

## 6. Application Profile (Form Defaults)
- Use `application_profile.md` (in this project directory) for **editable defaults used in job-application forms**, including work authorization, sponsorship, minimum-years-experience checks, location/residency/relocation/on-site answers, compensation/salary-comfort yes/no gates, text-message consent, pronouns, LinkedIn, GitHub, website, voluntary self-identification answers, and the preferred email address for verification/security codes.
- This file is for application forms and autofill flows, not for resume bullets or cover-letter narrative.
- The file is intentionally human-readable and should be edited directly when these defaults change.
- **Do not surface protected characteristics from this file** in resumes, cover letters, or unrelated free-text answers unless the user explicitly asks or the field specifically requires them.
- When an application presents an email verification/security code, fetch it through `gws` (googleworkspace/cli) before treating the flow as blocked or asking the user for the code. Do not rely on browser-only flows for Google inbox access.

## 7. Implementation Workflow

See [`docs/autofill-patterns.md`](autofill-patterns.md) -- the "Implementation Rules" section contains all board-specific and cross-board implementation rules.

**If the job posting input is missing, ask for it immediately. Do not proceed without it.**
