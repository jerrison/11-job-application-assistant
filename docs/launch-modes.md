# Launch Modes

## Mode 1: Single Job (Quick Launch)

The user gives you a job URL or pastes a JD. Process it immediately:

1. Scrape the URL with `scripts/scrape_job.py` (or use pasted text)
2. Produce the tailored resume
3. Produce the cover letter
4. Deliver all files

Preferred local CLI:

```bash
# End-to-end pipeline with review by default; add --submit to actually send
job-assets <jd_source>
job-assets --submit <jd_source>
# Assets only
job-assets apply <jd_source>
job-assets-codex <jd_source>
job-assets-claude <jd_source>
```

Example user messages that trigger this:
- `"https://boards.greenhouse.io/company/jobs/12345"`
- `"Apply to this: [url]"`
- `"Here's a JD I want to apply to: [pasted text]"`

## Mode 2: Batch from Notion

The user asks you to process jobs from their Notion "Job Applications" database. This database lives at:
- **Database ID:** `2e238885-a751-80cd-bd2c-da1a28dc3edb`
- **Data Source ID:** `2e238885-a751-802d-8274-000bd78e05b4`

### Database Schema (Key Fields)
| Property | Type | Values |
|----------|------|--------|
| `Name` | title | Usually the source page title, such as `<Role> | <Company>` |
| `Position` | text | Role title |
| `Status` | status | Not Started, Applied, Recruiter Reached Out, Interview Scheduled, Interview Completed, Offer Received, Accepted, Rejected |
| `Priority` | select | High, Medium, Low |
| `URL` | url | Job posting URL |
| `Application Date` | date | When applied |
| `Job Type` | select | Technical PM, Growth PM, Consumer PM, Enterprise PM, Core PM, Data PM |

### Available Views
| View | URL | Description |
|------|-----|-------------|
| All Applications | `view://2e238885-a751-80e8-9da2-000c3903bfc1` | Table sorted by Application Date desc |
| Application Status | `view://2e238885-a751-8050-b783-000c74a8fcbd` | Board grouped by Status |
| High Priority Jobs | `view://2e238885-a751-8094-a9d9-000c39517fe3` | Filtered to Priority = High |

### Batch Workflow

When the user says something like "process all Not Started jobs" or "create materials for my high priority jobs":

1. **Query the Notion database** using the appropriate view or fetch + filter. The user will specify a constraint (e.g., `Status = "Not Started"`, `Priority = "High"`, or a combination).
2. **For each matching row:**
   a. Extract the job URL from `URL`
   b. Fetch the Notion page content (which may already contain the scraped JD)
   c. If the page has JD content, use it. If not, scrape the URL with `scripts/scrape_job.py`
   d. Produce the tailored resume and cover letter
   e. Deliver the files (save to output directory — see [`output-structure.md`](output-structure.md))
   f. If the workflow actually submits the application and captures both website and email confirmations, sync the Notion row automatically: set `Status` to `Applied`, set `Application Date`, preserve/update the existing row when the job URL already exists, and include the JD plus application metadata on new pages. For an existing Notion page, keep `Application Date` pinned to the first confirmed submission. Later resubmissions append history to `Notes` and the page body instead of overwriting the original application date.
3. **Report progress** after each job is processed, and provide a summary at the end.

Example user messages that trigger batch mode:
- `"Process all Not Started jobs"`
- `"Create materials for my high priority jobs"`
- `"Apply to everything that's Not Started"`
- `"Process jobs from my Notion board where status is Not Started"`

Preferred local CLI:

```bash
job-assets parallel --provider codex
job-assets parallel --provider claude
job-assets-codex parallel --max-parallel 8
job-assets-claude batch --dry-run
```

### Important Notion Notes
- Each job application page may already contain the scraped JD in its content body. Check the page content first before re-scraping.
- The title property often contains the full source page title (e.g., "Job Application for Enterprise Product Manager | fal") rather than just the company name — parse the actual company name from it.
- The live tracker schema can drift from the markdown examples, so prefer discovering the current field names from the Notion API before writing.
- Public page access is enough to inspect the board shape, but writes still require `NOTION_API_TOKEN` (or `NOTION_TOKEN`) plus integration access to the target data source.
