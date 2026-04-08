# Interview Prep Guide — Design Spec

**Date**: 2026-03-20
**Status**: Draft
**Author**: Claude + Jerrison

---

## Problem

After applying to jobs, the user needs to prepare for interviews. Today this requires manually researching companies, interview formats, and crafting question banks — a multi-hour process per role. The system already has rich context (parsed JD, company research, tailored resume, work stories) that can be leveraged to auto-generate comprehensive, personalized interview prep guides.

## Solution

A new `generate_interview_prep.py` script that spawns a Claude CLI subprocess with deep web research capabilities to produce a 9-section interview preparation guide. Accessible via CLI and web UI.

---

## Architecture

### Core Script: `scripts/generate_interview_prep.py`

Single entry point for all surfaces. Responsibilities:

1. Gather context files from the job's output directory
2. Construct a dynamic user message from inputs (stage, interviewers, notes)
3. Spawn Claude CLI subprocess with system prompt + context files + tool access
4. Claude performs web research and writes `interview_prep.md` to the output directory
5. Post-process: convert markdown to .docx and .pdf

### Invocation

**CLI:**
```bash
uv run python scripts/generate_interview_prep.py <output_dir> \
  [--stage onsite] \
  [--interviewer "Jane Doe, VP Product, linkedin.com/in/janedoe"] \
  [--interviewer "John Smith, Eng Director"] \
  [--notes "focus on AI experience"] \
  [--force]  # regenerate even if exists
```

**Web API:**
```
POST /api/jobs/{id}/interview-prep
Body: { "stage": "onsite", "interviewers": "...", "notes": "..." }

GET /api/jobs/{id}/interview-prep
Returns: { "exists": true, "markdown": "...", "docx_path": "...", "pdf_path": "..." }
```

### Output Location

All files in a single dedicated folder:

```
output/{company}/{role}/
  interview_prep/
    interview_prep.md
    Jerrison Li Interview Prep - {Company}.docx
    Jerrison Li Interview Prep - {Company}.pdf
```

### Claude CLI Subprocess

**Pattern**: Same as auto-fix — spawn `claude` CLI with a system prompt file, context via `--read` flags, and allowed tools.

**System prompt**: Stored at `scripts/prompts/interview_prep_system.md` (new directory — establishes convention for future prompt files). Contains:
- Level calibration logic (FAANG L6 vs. startup stage mapping)
- Company type detection instructions
- Story indexing protocol (reads work_stories.md, maps to competencies, flags gaps)
- 9-section output structure with per-section guidance
- Research protocol (company deep dive, interview format, interviewer profiles)
- Quality filters (level calibration > company-type calibration > personalization)

**Context files passed via `--read`:**
- `content/jd_parsed.json` — structured JD
- `content/jd_raw.md` — raw JD text
- `content/role_research_cache.json` — existing research (if available)
- `master_resume.md` — full career history
- `work_stories.md` — STAR narratives
- `candidate_context.md` — background and preferences
- `application_profile.md` — form defaults (location, links, etc.)

**Tools allowed:**
- WebSearch — company research, interview format, Glassdoor, Blind
- WebFetch — fetch full pages (company blogs, news articles, interviewer profiles)
- Playwright MCP — fallback when WebFetch returns 403
- Read — read project files
- Write — write interview_prep.md
- Glob, Grep — search project files

**User message**: Dynamically constructed:
```
Generate an interview preparation guide for:
- Company: {company_proper}
- Role: {jd_title}
- JD URL: {jd_source}
- Interview Stage: {stage or "General"}
- Interviewers: {interviewers or "Not provided"}
- Additional Notes: {notes or "None"}

Write the complete guide to: {output_dir}/interview_prep/interview_prep.md
```

---

## Output Structure: 9 Sections

### 1. Executive Summary
- Role snapshot (title, level-equivalent, team, location, comp range)
- Company snapshot (stage, momentum, strategic direction, why hiring now)
- Company type classification (FAANG / late-stage / growth-stage / early-stage)
- Interview format overview
- Top 3 strategic narratives (from user's actual career arc in work_stories.md)
- Level differentiation summary
- Story readiness traffic-light assessment (GREEN / YELLOW / RED per competency)

### 2. Company Intelligence
- Business model, financials (earnings for public; funding/revenue for startups)
- Product portfolio, PM org structure, PM culture
- Founding team profiles (for startups), investor thesis, PMF assessment
- Recent news (last 6 months, with dates and sources)
- Competitive landscape (comparison table)
- Key challenges and open strategic questions
- What problem this hire solves

### 3. Interview Format & Process
- Round-by-round breakdown with durations and focus areas
- What each round evaluates at target seniority
- Founder round guidance (for startups)
- Common rejection/downlevel patterns
- Candidate tips from Glassdoor, Blind, coaching sources

### 4. Interviewer Profiles (only if interviewers provided)
- Background, role, tenure, career trajectory
- Published content (articles, talks, patents)
- Connection points with user (overlapping companies, schools, domains)
- Likely interview focus areas and calibration advice

### 5. Behavioral Questions (12-15)
- Grouped by competency cluster (strategic leadership, cross-org influence, ambiguity navigation, exec/founder management, team building, high-stakes tradeoffs, builder mentality)
- Each question includes: question text, why this company cares, senior vs. mid-level differentiation, STAR guidance, "Your best story" mapping from work_stories.md (or STORY GAP flag)

### 6. Product Sense Questions (8-10)
- Using company's actual products and strategic context
- Categories: product improvement, zero-to-one, portfolio/roadmap, cross-product tradeoffs, market entry/pricing, estimation
- Each includes: question, recommended framework, company-specific considerations, structured answer outline, metrics framework, domain credibility anchor from user's experience

### 7. Execution & Technical Questions (8-12)
- Merged execution + technical for efficiency
- Includes: metric diagnosis, experiment design, resource allocation, AI/ML product reasoning (for AI companies), system design, build-vs-buy
- Each includes: question contextualized to company, structured approach, relevant metrics/frameworks, senior vs. mid-level pitfalls

### 8. Questions to Ask (10-12)
- Organized: strategic/product direction (3-4), org design/PM culture (3-4), team/execution (2-3), interviewer-specific (1-2)
- Each with a note on what signal it sends

### 9. Preparation Strategy
- Positioning narrative (2-3 sentences tying user's background to this role)
- Proof point inventory (5-6 stories from work_stories.md mapped to competencies, with framing advice and opening sentence suggestions)
- Story gap report with actionable guidance
- Downlevel/rejection risk mitigation (personalized)
- Company-specific vocabulary and key numbers
- Time-based prep plans (30 min / 2 hour / full day)

---

## Document Generation

### Markdown → Word (.docx)

Post-processing step after Claude writes `interview_prep.md`. Uses `python-docx` to produce a formatted document:

- Title page: role title, company, level, preparation date
- Table of contents
- Professional heading hierarchy (Calibri, consistent with existing resume/cover letter style)
- Tables for structured data (competitive landscape, round-by-round breakdown)
- US Letter (8.5x11), 0.75in margins
- Clean, scannable layout

Implementation: new function `build_interview_prep_docx()` in `generate_interview_prep.py` that parses the markdown and renders to docx. Reuses styling patterns from `build_resume.py` and `build_cover_letter.py`.

### Word → PDF

Uses LibreOffice conversion, same as existing resume/cover letter pipeline (`subprocess` call to `libreoffice --headless --convert-to pdf`).

---

## Web UI Integration

### Job Detail Page

**New tab: "Interview Prep"**
- Appears as the last tab (position 7) after Timeline — avoids breaking existing keyboard shortcuts for tabs 1-6
- Tab visibility: always shown (badge indicator when prep exists for discoverability)
- If prep exists: renders markdown content with download links (.docx, .pdf) at top
- If no prep exists: shows "Generate Interview Prep" button

**Generate Modal**
- Triggered by "Generate Interview Prep" button (or "Regenerate" if prep exists)
- Fields:
  - Interview Stage: dropdown (General / Recruiter Screen / Phone Screen / Onsite / Final Round)
  - Interviewers: optional textarea (names, titles, LinkedIn URLs — one per line)
  - Notes: optional textarea (free text)
- "Generate" button submits to `POST /api/jobs/{id}/interview-prep`

**Progress**
- While generating: tab shows spinner with status text
- File-based progress polling: the generation script writes `interview_prep/.progress.json` (e.g., `{"status": "researching", "detail": "Searching company intelligence..."}`) at each major step. The UI polls `GET /api/jobs/{id}/interview-prep` on a 3-second interval while generating, which reads this file.
- Status stages: "Starting research..." → "Researching company..." → "Researching interview format..." → "Generating prep guide..." → "Building documents..." → "Complete"

**Regenerate**
- If prep already exists, "Regenerate" button appears at top of tab
- Opens same modal, pre-populated with last inputs if available
- Overwrites existing files

### API Endpoints

**`POST /api/jobs/{id}/interview-prep`**
- Accepts: `{ "stage": "onsite", "interviewers": "...", "notes": "..." }`
- Validates job exists and has an output_dir
- **Concurrency guard**: checks for `interview_prep/.generating` lock file. If present and the PID inside is still alive, returns 409 Conflict. Otherwise, writes lock file with current PID before proceeding.
- Spawns a `threading.Thread` (daemon) that internally runs `subprocess.run(["uv", "run", "python", "scripts/generate_interview_prep.py", ...], timeout=900)`. On completion/failure, logs the event and removes the lock file. Matches the `_score_background` pattern in `job_web.py`.
- Logs `interview_prep_started` event
- Returns: `{ "status": "started" }`
- On completion: logs `interview_prep_completed` or `interview_prep_failed` event, removes `.generating` lock file
- Always regenerates (no `force` flag needed from web — the "Generate" vs "Regenerate" button is the UX guardrail). CLI uses `--force` to skip the "already exists" confirmation.

**`GET /api/jobs/{id}/interview-prep`**
- Checks for `interview_prep/interview_prep.md` in job's output_dir
- Also reads `interview_prep/.progress.json` for in-progress status
- If complete: returns `{ "exists": true, "markdown": "<content>", "docx_download": "/api/jobs/{id}/interview-prep/download/docx", "pdf_download": "/api/jobs/{id}/interview-prep/download/pdf" }`
- If generating: returns `{ "exists": false, "generating": true, "progress": {"status": "researching", "detail": "..."} }`
- If not started: returns `{ "exists": false, "generating": false }`

**`GET /api/jobs/{id}/interview-prep/download/{format}`**
- Serves the .docx or .pdf file for download
- Format: `docx` or `pdf`

---

## Progress Tracking

No new DB table. Uses existing infrastructure:

- **Events table**: `interview_prep_started`, `interview_prep_completed`, `interview_prep_failed` event types logged with job_id
- **File existence**: web UI checks for `interview_prep/interview_prep.md` to determine state
- **Progress file**: `interview_prep/.progress.json` written by the generation script at each major step, polled by the GET endpoint
- **Lock file**: `interview_prep/.generating` with PID, prevents concurrent generation, cleaned up on completion/failure

---

## Error Handling

- If Claude CLI subprocess fails (non-zero exit): log `interview_prep_failed` event with error message, surface in UI
- If context files are missing (no jd_parsed.json): script exits with clear error — "Run the pipeline first to generate JD and research context"
- If web research fails (all sources 403): Playwright MCP fallback. If still failing, generate guide with available cached context and note "Limited web research — some sections may be less detailed"
- If work_stories.md or master_resume.md not found: proceed but flag in output — "No personal stories found. Upload work_stories.md for personalized story mapping."

---

## Files to Create/Modify

### New Files
| File | Purpose |
|------|---------|
| `scripts/generate_interview_prep.py` | Core script — context gathering, Claude CLI spawn, docx/pdf post-processing |
| `scripts/prompts/interview_prep_system.md` | System prompt for Claude CLI subprocess |

### Modified Files
| File | Change |
|------|--------|
| `scripts/job_web.py` | Add 3 API endpoints (POST, GET, GET download) |
| `scripts/static/index.html` | Add "Interview Prep" tab to job detail view |
| `scripts/static/app.js` | Tab rendering, generate modal, progress polling, download links |
| `scripts/static/style.css` | Styling for prep tab, modal, markdown rendering |
| `docs/output-structure.md` | Document new `interview_prep/` subdirectory |

---

## Non-Goals

- **Incremental updates** — regenerate the full guide each time
- **New DB table** — reuse events table
- **Auto-generation in pipeline** — only on explicit user request
- **Interview scheduling/calendar integration** — out of scope
- **Mock interview functionality** — out of scope
