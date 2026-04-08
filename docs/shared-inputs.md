# Shared Inputs

The pipeline is driven by user-owned runtime inputs resolved from the shared
runtime home in `scripts/app_paths.py`. These files are intentionally not
committed.

## Canonical files

- `master_resume.md`
  Source of truth for candidate experience, bullets, and contact details.
- `work_stories.md`
  Supporting stories and examples used to enrich cover letters and application answers.
- `candidate_context.md`
  Voice, motivations, preferences, and other supplemental context.
- `application_profile.md`
  Structured defaults for form filling, links, work authorization, and related profile answers.

## Surface Parity

The packaged app, web app, CLI, and TUI must all use the same
onboarding/settings backend in `scripts/settings_store.py`.
They must read and write the same canonical materials and credentials rather
than keeping surface-specific copies.

## How users provide them

- paste text directly
- upload a local `.txt`, `.md`, or `.docx` file
- provide a public Google Drive, Google Docs, or other fetchable source URL

Accepted source URLs include public text endpoints, Google Drive links, and Google Docs links. Google Docs edit URLs are normalized to export URLs at runtime, and Google Drive share links are normalized to direct-download URLs.

Provider credentials such as `OPENAI_API_KEY`, `GEMINI_API_KEY`,
`CODEX_API_KEY`, `ANTHROPIC_API_KEY`, and `STEEL_API_KEY` are part of the
same shared settings model and must not be configured separately per surface.

## Optional sync configuration

Remote syncs are opt-in and configured through environment variables:

- `JOB_ASSETS_MASTER_RESUME_SOURCE_URL`
- `JOB_ASSETS_WORK_STORIES_SOURCE_URL`
- `JOB_ASSETS_CANDIDATE_CONTEXT_SOURCE_URL`

If these variables are unset, the pipeline uses the local runtime files and skips remote sync.

## Public repo rule

Do not commit personal source URLs, API keys, candidate data, generated outputs, or browser state to this repository.
