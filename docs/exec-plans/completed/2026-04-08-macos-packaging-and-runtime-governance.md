# macOS Packaging And Runtime Governance Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a real macOS app bundle that boots the existing local web UI from a packaged launcher, keeps all writable state under the user runtime home, and enforces shared runtime policy and redacted tracing across app, web, CLI, and TUI entry surfaces.

**Spec:** User follow-up request for the real macOS bundle, policy-as-code, explicit trace/processor wiring, and shared onboarding/settings parity across surfaces.

**Existing code:** `scripts/app_paths.py`, `scripts/runtime_entrypoints.py`, `scripts/runtime_policy.py`, `scripts/runtime_trace.py`, `scripts/mac_app_launcher.py`, `scripts/job_web.py`, `scripts/job_tui.py`, `bin/job-assets`, `scripts/settings_store.py`, `scripts/openai_provider.py`, `scripts/submit_application.py`

---

## Purpose / Big Picture

After this change, the repo can build a working `.app` bundle around the
existing local web UI, while runtime-home boundaries, governed actions, and
redacted traces stay consistent across packaged app, web, CLI, and TUI
surfaces.

---

## Milestones

1. **Runtime governance foundation is real:** runtime trace/policy/launcher tests exist and pass.
2. **Governance is wired into product actions:** settings, material imports, worker control, provider calls, and submit paths use the shared runtime policy and trace helpers.
3. **The app bundle builds:** PyInstaller produces a working macOS `.app` bundle from `scripts/mac_app_launcher.py`.
4. **Docs reflect the shipped boundary:** runtime traces are treated as local state, and the execution-plan record is archived as completed.

---

## Progress

| Step | Status | Updated |
|------|--------|---------|
| Runtime trace/policy/launcher tests added and passing | Complete | 2026-04-08 |
| Packaged-safe subprocess and runtime-home path work | Complete | 2026-04-08 |
| Shared policy and trace wiring across surfaces | Complete | 2026-04-08 |
| Docs updated for packaging/runtime governance | Complete | 2026-04-08 |
| Full verification rerun | Complete | 2026-04-08 |
| Real macOS bundle build and smoke test | Complete | 2026-04-08 |

---

## Surprises & Discoveries

- Packaging required bundling the platform-specific `tls_client` native
  dependency explicitly. Generic collection was not sufficient for the built
  app to boot cleanly.
- The highest-leverage governance improvement was startup-time registration of
  trace processors so each long-running surface shares the same redaction and
  sink behavior by default.
- `job_web.py` needed route extraction into `scripts/web_settings_api.py` to
  stay within the repo's line-budget guardrail after the settings/onboarding
  work expanded.

---

## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-04-08 | Keep the packaged mac app as a launcher around the existing local web UI | Reuses the shared settings/onboarding contract and avoids a second product surface |
| 2026-04-08 | Register runtime trace processors explicitly at startup for app, web, CLI, TUI, submit, and provider entrypoints | Keeps long-running surfaces aligned and auditable |
| 2026-04-08 | Enforce required action metadata in policy-as-code | Makes governed actions fail closed when context needed for approval or audit is missing |

---

## Verification

Completed with fresh evidence on 2026-04-08:

- `uv run ruff check scripts/ tests/ bin/job-assets`
- `uv run python scripts/check_architecture.py`
- `uv run python scripts/check_agent_docs.py`
- `uv run python scripts/sync_agent_files.py --check`
- `uv run python -m pytest tests/ -q`
  - Result: `2702 passed, 16 subtests passed`
- `uv run --with pyinstaller python scripts/build_mac_app.py`
  - Result: `.app` bundle created at `dist/Job Application Assistant.app`
- Packaged smoke test:
  - Ran `dist/Job Application Assistant.app/Contents/MacOS/Job Application Assistant --no-browser --port 8588`
  - Verified `curl -fsS http://127.0.0.1:8588/api/health`
  - Result: `{"status":"ok","worker_running":false}`

---

## Outcomes & Retrospective

- **Achieved:** Built and smoke-tested the macOS app bundle, added explicit
  runtime policy and trace wiring, kept shared onboarding/settings parity
  across surfaces, and documented the packaged/runtime boundary.
- **Remaining:** Distribution steps such as signing, notarization, and release
  automation can now build on the packaged launcher, but they were not
  executed in this pass.
- **Lessons:** Packaging pressure was useful because it forced the runtime
  contract, subprocess boundaries, and governance sinks to become explicit
  rather than ambient.
