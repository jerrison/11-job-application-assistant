# OpenAI Key Rotation And Provider Failure Instrumentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Spread OpenAI API traffic across a configured key pool, record exact provider-attempt failures live, and surface those counts in the web UI.

**Architecture:** Keep the existing provider chain and concurrency guardrails, but add per-attempt telemetry. OpenAI key rotation should happen inside the OpenAI shim so all OpenAI call sites benefit automatically, while provider-attempt logging should be recorded centrally from the orchestrator and submit-answer paths into `provider_runs`, then aggregated through a new stats endpoint for the dashboard/stats UI.

**Tech Stack:** Python, SQLite, FastAPI, vanilla JS, pytest

---

### Task 1: OpenAI Key Pool TDD

**Files:**
- Modify: `scripts/openai_provider.py`
- Modify: `tests/test_openai_provider.py`
- Modify: `docs/provider-setup.md`

- [ ] Add failing tests for parsing multiple OpenAI keys from env without exposing secrets.
- [ ] Add failing tests for deterministic key-slot selection metadata and round-robin spreading across repeated calls.
- [ ] Add failing tests proving single-key behavior still works when only `OPENAI_API_KEY` is present.
- [ ] Implement minimal key-pool parsing and key-slot selection in `scripts/openai_provider.py`.
- [ ] Emit non-secret telemetry (`provider=openai`, key slot, pool size) to stderr for downstream logging.
- [ ] Update provider setup docs with the new env contract.

### Task 2: Provider Attempt Logging TDD

**Files:**
- Modify: `scripts/job_db.py`
- Modify: `scripts/pipeline_orchestrator.py`
- Modify: `scripts/application_submit_common.py`
- Modify: `tests/test_job_db.py`
- Modify: `tests/test_pipeline_orchestrator.py`
- Modify: `tests/test_submit_application.py`

- [ ] Add failing tests showing asset-generation provider failures are logged, not only the winning provider.
- [ ] Add failing tests showing submit-answer provider attempts are logged with failure classification and OpenAI key slot when available.
- [ ] Add any required `provider_runs` schema evolution tests before changing the schema.
- [ ] Implement normalized provider-attempt logging for both asset generation and submit-answer generation.
- [ ] Classify common provider failures at write time or aggregation time: `invalid_api_key`, `invalid_json_schema`, `rate_limit`, `timeout`, `capacity`, `other`.

### Task 3: Live Provider Failure Stats TDD

**Files:**
- Modify: `scripts/job_db.py`
- Modify: `scripts/job_web.py`
- Modify: `tests/test_job_db.py`
- Modify: `tests/test_job_web.py`

- [ ] Add failing tests for a provider-stats aggregation helper over `provider_runs`.
- [ ] Add a failing API test for a new provider-stats endpoint.
- [ ] Implement backend aggregation that returns totals by provider, phase, failure type, and OpenAI key slot.
- [ ] Wire the new aggregation into a FastAPI stats endpoint.

### Task 4: Dashboard And Stats Surface

**Files:**
- Modify: `scripts/static/index.html`
- Modify: `scripts/static/app.js`
- Modify: `scripts/static/style.css`

- [ ] Add a small dashboard panel for live provider-failure counts.
- [ ] Add a stats-view table or summary block for provider failures by provider and failure class.
- [ ] Keep the UI additive and lightweight; do not disturb queue workflows.

### Task 5: OpenAI Structured-Output Regression Coverage

**Files:**
- Modify: `tests/test_submit_application.py`
- Modify: `tests/test_greenhouse_autofill.py`
- Modify: `scripts/application_submit_common.py` (only if tests prove a live bug)

- [ ] Add regression tests covering historical failing shapes: multi-select questions, bracketed field names, and required conditional follow-ups.
- [ ] Run those tests against the current shared schema builder first.
- [ ] Only change production schema code if the new regression tests expose a real current failure.

### Task 6: Verification

**Files:**
- Modify: `docs/provider-setup.md` if implementation adds user-facing env vars

- [ ] Run targeted pytest for the touched modules.
- [ ] Run `uv run ruff check` on changed Python files.
- [ ] If frontend files change, run the relevant tests or smoke checks available in-repo.
- [ ] Summarize what was fixed, what still needs the user to configure, and whether lowering `LLM_PROVIDER_CONCURRENCY` is still warranted after the new telemetry lands.
