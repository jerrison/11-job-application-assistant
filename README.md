# Job Application Assistant

Local-first job application automation for tailored resumes, cover letters, and
draft-mode autofill across multiple job boards.

This repo is the product codebase only. Your resume, context files, API keys,
browser state, SQLite data, screenshots, and generated application assets live
in the runtime home, not in git.

## What This Is

This project turns job applications into a local operator system:

- one shared candidate context model
- one shared settings backend for mac app, web app, CLI, and TUI
- one draft-first automation pipeline that generates materials, fills forms,
  captures proof, and stops before live submit unless you explicitly approve it

It is not a spray-and-pray submitter. The default path is still reviewable
draft generation with proof artifacts.

## Core Capabilities

| Capability | What it does |
|------------|--------------|
| Shared onboarding | Import `master_resume.md`, `work_stories.md`, `candidate_context.md`, and `application_profile.md` from text, local files, or public Google Drive/Docs links |
| Shared credentials | Store OpenAI, Gemini, Codex, Anthropic, and Steel settings through one runtime-backed settings layer |
| Tailored assets | Generate resume, cover letter, interview prep, and related role-specific artifacts |
| Draft autofill | Run browser automation in review-first mode with screenshots and structured result JSON |
| Multi-surface control | Use the same backend through the packaged mac app, local web UI, CLI, or TUI |
| Runtime governance | Enforce policy-as-code approval gates and write redacted local trace events for governed actions |

## Local-First Runtime Model

The application code is read-only product state. User-owned state is runtime
state:

- `master_resume.md`
- `work_stories.md`
- `candidate_context.md`
- `application_profile.md`
- `.env` / `.env.local`
- `jobs.db`
- `output/`
- browser profiles
- `logs/`
- `traces/runtime-trace.jsonl`

Do not commit any of those files or directories.

## Quick Start

1. Install dependencies:

```bash
uv sync
```

2. Optionally install the CLI wrapper:

```bash
python3 bin/job-assets install
```

3. Start one surface:

```bash
uv run python scripts/job_web.py
```

```bash
uv run python scripts/job_tui.py
```

```bash
job-assets settings show
```

4. Open the shared Settings surface and add:

- `master_resume.md`
- `work_stories.md`
- `candidate_context.md`
- `application_profile.md`
- at least one provider credential

5. Run a role through the pipeline:

```bash
job-assets "https://boards.greenhouse.io/company/jobs/12345"
job-assets apply "https://boards.greenhouse.io/company/jobs/12345"
job-assets submit output/company/role-slug
```

The default automation path is draft-only. The system should stop before live
submit unless you explicitly cross that boundary.

## Shared Onboarding

All operator surfaces use the same backend in `scripts/settings_store.py`.

Supported onboarding inputs:

- paste text or markdown
- upload `.txt`, `.md`, or `.docx`
- import from a public Google Drive or Google Docs link
- configure provider credentials and model defaults

CLI equivalents:

```bash
job-assets settings show
job-assets settings import master_resume --file ~/resume.docx
job-assets settings import work_stories --url "https://drive.google.com/file/d/FILE_ID/view?usp=sharing"
job-assets settings set --openai-api-key sk-... --gemini-api-key ai-...
```

Optional remote syncs are opt-in via runtime env vars:

- `JOB_ASSETS_MASTER_RESUME_SOURCE_URL`
- `JOB_ASSETS_WORK_STORIES_SOURCE_URL`
- `JOB_ASSETS_CANDIDATE_CONTEXT_SOURCE_URL`

## Surfaces

| Surface | Entrypoint | Notes |
|---------|------------|-------|
| mac app | `scripts/mac_app_launcher.py` | Packaged desktop launcher around the same local web UI |
| web app | `scripts/job_web.py` | FastAPI local UI with queue, proof, and settings |
| CLI | `bin/job-assets` | Single-job pipeline, settings, queue operations, and utilities |
| TUI | `scripts/job_tui.py` | Terminal queue and settings interface backed by the same runtime store |

## How It Works

```text
Add candidate context in Settings
        |
        v
Paste or import a job
        |
        v
Resolve board + generate tailored assets
        |
        v
Run draft-mode autofill with screenshots and result JSON
        |
        v
Review proof in web/TUI
        |
        v
Explicitly approve live submit only if you want to cross the boundary
```

## Project Structure

```text
bin/                 CLI wrappers
scripts/             pipeline, web, TUI, workers, board handlers, packaging helpers
assets/              bundled fonts and app resources needed at runtime
governance/          policy-as-code runtime controls
docs/                product, ops, governance, and architecture docs
tests/               regression and contract coverage
```

## Packaged macOS App

Build the desktop app bundle with PyInstaller:

```bash
uv run --with pyinstaller python scripts/build_mac_app.py
```

The bundle launches the same local web UI, routes internal subprocesses through
packaged-safe entrypoints, and keeps writable state under the user runtime
home. See [docs/macos-app.md](docs/macos-app.md).

## Safety Model

- Draft flows are the normal path.
- Live submit is an explicit approval boundary.
- Runtime policy is defined in `governance/runtime-policy.json`.
- Governed actions emit redacted local traces to `traces/runtime-trace.jsonl`.
- Candidate data, secrets, and generated assets are runtime state, not repo
  state.

## Documentation Map

Start here:

- [docs/INDEX.md](docs/INDEX.md)
- [AGENTS.md](AGENTS.md)
- [ARCHITECTURE.md](ARCHITECTURE.md)
- [docs/harness-governance.md](docs/harness-governance.md)
- [docs/macos-app.md](docs/macos-app.md)

## Verification

```bash
uv run ruff check scripts/ tests/
uv run python -m pytest tests/ -v
uv run python scripts/check_architecture.py
uv run python scripts/check_agent_docs.py
uv run python scripts/sync_agent_files.py --check
```
