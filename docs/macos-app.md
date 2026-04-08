# macOS App

This repo can be built as a local macOS app bundle that launches the existing
web UI on `127.0.0.1` and keeps all writable state under the user runtime
home.

## Build

Install dependencies first:

```bash
uv sync
```

Build the `.app` bundle with PyInstaller:

```bash
uv run --with pyinstaller python scripts/build_mac_app.py
```

Bundle output:

```text
dist/Job Application Assistant.app
```

## What The Bundle Contains

The build includes the runtime resources that still need to exist as files at
execution time:

- `scripts/static/`
- `scripts/prompts/`
- `assets/fonts/`
- `governance/runtime-policy.json`

Internal Python subprocesses are routed through
`scripts/runtime_entrypoints.py`, so packaged mode does not depend on raw
source-script paths.

## Runtime Model

The packaged app is a launcher around the same backend used by the local web
app. It does not create a second settings system.

- `scripts/mac_app_launcher.py` prepares runtime-home directories, loads local
  env files, configures trace processors, resolves a local port, and starts
  the FastAPI app.
- `scripts/web_settings_api.py` exposes the same settings and onboarding API
  used by the local web surface.
- `scripts/runtime_policy.py` enforces policy-as-code decisions for shared
  actions such as settings saves, material imports, worker control, provider
  calls, and live submit.
- `scripts/runtime_trace.py` registers redacted JSONL processors at startup so
  governed actions emit local traces without leaking secrets or prompt bodies.

## Runtime Home

When packaged mode is enabled, writable state moves under:

```text
~/Library/Application Support/Job Assets/
```

Important runtime locations:

- `master_resume.md`
- `work_stories.md`
- `candidate_context.md`
- `application_profile.md`
- `.env` and `.env.local`
- `jobs.db`
- `output/`
- `.job-assets/` browser state
- `logs/`
- `traces/runtime-trace.jsonl`

## Shared Onboarding And Settings

The packaged app, local web app, CLI, and TUI all use the same settings
backend in `scripts/settings_store.py`.

Supported onboarding inputs:

- paste text or markdown
- upload `.txt`, `.md`, or `.docx`
- import from a public Google Drive or Google Docs link
- configure OpenAI, Gemini, Codex, Anthropic, and Steel credentials

## Local Simulation

To simulate packaged runtime behavior without building the bundle:

```bash
JOB_ASSETS_PACKAGED=1 uv run python scripts/mac_app_launcher.py --no-browser
```

This follows the same runtime-home, policy, and trace rules as the built app
while still running from the source tree.

## Verification

Recommended checks before shipping the bundle:

```bash
uv run python -m pytest tests/test_mac_app_launcher.py tests/test_build_mac_app.py -q
uv run --with pyinstaller python scripts/build_mac_app.py
```
