# Agent Registry

Register every long-running automation surface here so reviewers can see who
owns it, what authority it has, and how it is evaluated.

| Surface | Entrypoint | Owner | Purpose | Risk tiers | Validation / proof | Approval boundary |
|---------|------------|-------|---------|------------|--------------------|-------------------|
| CLI | `bin/job-assets`, `scripts/run_pipeline.py`, `scripts/job_assets_pipeline.py` | Automation runtime | Single-job orchestration, settings management, and operator entrypoint for local automation | `L1-L3` | `tests/test_job_assets_cli.py`, `tests/test_run_pipeline_sync.py`, per-job `submit/` artifacts | `--submit` and any publication step require explicit `L3` approval |
| macOS app | `scripts/mac_app_launcher.py`, `scripts/build_mac_app.py` | Packaged app runtime | Starts the local web UI in packaged mode and owns runtime-home bootstrap for distributed desktop use | `L1-L3` | `tests/test_mac_app_launcher.py`, packaged runtime smoke checks, bundle build output in `dist/` | Same as web: local settings and drafts are allowed; live submit, push, merge, and public release stay `L3` |
| Web app | `scripts/job_web.py` | Interface runtime | Local queue, settings, worker control, and draft review UI | `L1-L3` | `tests/test_job_web.py`, browser screenshots, queue/result artifacts | Draft flows are `L2`; live submit and publication are `L3` |
| TUI | `scripts/job_tui.py` | Interface runtime | Terminal queue, settings, and review surface with the same backend contract as web | `L1-L3` | `tests/test_job_tui.py`, shared settings/bootstrap payloads | Same as web: `L2` for drafts, `L3` for submit/publication |
| Worker orchestration | `scripts/job_worker.py`, `scripts/pipeline_orchestrator.py` | Automation runtime | Background job processing, retries, audit loops, and status transitions | `L2-L3` | `tests/test_pipeline_orchestrator.py`, `tests/test_pipeline_audit_loop.py`, per-job result JSON | Must fail closed before submit unless the operator approved `L3` |
| Draft review surface | `scripts/draft_web.py`, `scripts/draft_manager.py` | Review runtime | Human review, proof inspection, and resume-before-submit workflows | `L2` | `tests/test_draft_manager.py`, screenshot proof, draft summaries | Cannot cross into live submit without explicit user approval |

When a new surface is added, update this table before push or merge.
