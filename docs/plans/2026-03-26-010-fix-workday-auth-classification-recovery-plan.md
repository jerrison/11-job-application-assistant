---
title: "fix: Harden Workday auth classification and recovery"
type: fix
status: completed
date: 2026-03-26
origin: docs/brainstorms/2026-03-26-workday-auth-classification-and-recovery-requirements.md
---

# fix: Harden Workday auth classification and recovery

## Overview

Implement a Workday-specific auth-state model that stops flattening every gateway outcome into `auth_failed`, align the runtime to the approved recovery order (`sign in -> password reset -> create account`), scope lockout protection to a single Workday tenant, and surface enough durable evidence across worker, web, TUI, and artifacts that the user can tell whether a run hit credential rejection, maintenance, or an unknown auth state (see origin: `docs/brainstorms/2026-03-26-workday-auth-classification-and-recovery-requirements.md`).

## Problem Frame

The current Workday path is failing at the seam between board runtime and shared pipeline accounting. `scripts/autofill_workday.py` can leave the browser on a normal `Create Account / Sign In` screen, then `_write_auth_failure_log()` records a generic `auth_failed` artifact. `scripts/pipeline_orchestrator.py` treats every `*_auth_failure.json` artifact as a hard `auth_failed` stop, and `scripts/job_db.py::get_recent_auth_failures()` counts those failures board-wide. The result is both misleading and over-broad: ambiguous Workday gateway landings are recorded as true credential failures, and a few such stops can block later Workday jobs for unrelated employers.

The requirements narrow the product contract:
- only explicit credential rejection is a true Workday auth failure
- explicit maintenance is transient and retryable
- exhausting the approved recovery order without explicit rejection is an unknown auth state, not `auth_failed`
- protective skipping must be scoped to the Workday tenant/site, not to all Workday jobs

## Requirements Trace

- R1-R4. Replace the single generic Workday auth-failure path with a specific auth-state classifier and durable artifact contract.
- R5-R7, R13. Rebuild the Workday auth runtime around the approved recovery order and preserve rich evidence for unknown states and non-form authenticated landings.
- R8-R10. Route explicit maintenance into transient retry behavior and scope the auth guard to repeated explicit credential rejection on the same Workday tenant.
- R11-R12, R14. Surface the more specific auth reason consistently across worker/orchestrator status, DB/API data, web/TUI review surfaces, and draft-related submit flows.

## Scope Boundaries

- Do not redesign the global auth taxonomy for every board in this plan. Keep the classifier and guard behavior Workday-specific unless a shared abstraction falls out cheaply.
- Do not remove the protective guard entirely.
- Do not reopen the broader pipeline-resilience strategy beyond the Workday-specific transitions required by the origin requirements.
- Do not change draft-mode semantics or submit-mode semantics beyond the auth decision path.
- Do not introduce a separate credential-management product or secret-storage workflow.
- Do not treat ambiguous Workday auth states as transient retries by default after the approved recovery order is exhausted.

## Context & Research

### Relevant Code and Patterns

- `pyproject.toml` shows a Python 3.12 repo with pytest, Ruff, Playwright, Textual, and FastAPI. This work lives in the existing Python + static JS stack; no external framework decision is needed.
- `docs/board-architecture.md` already identifies Workday as an auth-required multi-page board using `autofill_main` with a custom `run_browser_fn`.
- `scripts/submit_application.py` routes every Workday submission entry point to `scripts/autofill_workday.py`, and `tests/test_submit_application.py` already locks that mapping. Fixing the Workday runtime contract in one place is therefore the right way to satisfy the origin requirement that draft generation, refresh/re-entry, and other Workday browser flows behave consistently.
- `docs/autofill-patterns.md` already documents the intended Workday order as sign-in first, then password reset, then create account. The runtime in `scripts/autofill_workday.py` has drifted from that contract: `_handle_auth()` and the later wizard loop still contain create-account-first branches.
- `scripts/autofill_workday.py` owns all Workday auth behavior today: `_handle_auth()`, `_do_sign_in()`, `_do_password_reset()`, `_do_create_account()`, `_is_application_page()`, the page loop in `_run_workday_browser()`, and `_write_auth_failure_log()`.
- `scripts/pipeline_orchestrator.py` has two Workday-sensitive seams:
  - the pre-submit auth guard that currently counts repeated `auth_failed` rows board-wide
  - the post-submit artifact parser that turns any `*_auth_failure.json` artifact into a hard `auth_failed` stop
- `scripts/job_db.py` currently persists only coarse auth failure information. `get_recent_auth_failures()` is board-scoped only, and `sync_job_from_disk()` only knows how to derive a generic error string from `workday_auth_failure.json`.
- `scripts/job_worker.py` only sets board cooldown when `_is_rate_limit_error(error_message)` matches rate-limit language. That means the new Workday error copy must stay specific enough that auth and maintenance states do not accidentally present themselves as rate limiting.
- `scripts/job_tui.py` already has an “Attention” tab pattern for auth failures and pending-user-input artifacts. It is the natural place to show richer Workday auth evidence.
- `scripts/static/app.js` and `scripts/job_web.py` already surface `error_message` inline in the queue and job detail, and `job_web.py` has a `/api/jobs/{job_id}/logs` path that currently includes `unsupported_board.json` but not Workday auth artifacts.
- There is no dedicated Workday auth test file today. The closest pattern is `tests/test_autofill_linkedin.py`, which uses pure helper-state tests (`_classify_linkedin_resume_markers`, `_resume_outcomes`) to lock down variant-heavy browser state before touching runtime flow. Workday should follow that pattern instead of relying only on end-to-end browser behavior.
- Existing output artifacts already provide characterization fixtures:
  - `output/factset/senior-pm-performance-solutions/submit/workday_auth_failure.json` and its debug PNG show the normal `Create Account / Sign In` screen
  - `output/snapchat/principal-pm-ads-platform/submit/workday_auth_failure.json` shows a Workday maintenance page

### Institutional Learnings

- `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`
  - Variant-heavy per-board logic becomes brittle when state detection is scattered across multiple ad hoc branches.
  - The recommended prevention pattern is one centralized, explicitly ordered classifier with direct helper tests and shared consumers.
- No `docs/solutions/patterns/critical-patterns.md` file exists in this repo today.

### External References

- None. The repo already has the relevant Playwright, artifact, retry, and cross-surface status patterns. The open questions are repo-specific workflow seams, not framework-API questions.

## Key Technical Decisions

- Keep `failure_type='auth_failed'` reserved for **explicit credential rejection only**. Do not reuse it for create-account landings, reset pages, maintenance, or ambiguous exhaustion of the recovery order.
- Add a durable `auth_scope` field to the `jobs` table and use it for Workday tenant-scoped auth accounting. Persist `auth_scope` only when it is known; non-Workday boards can keep board-wide behavior for now.
- Persist the finer-grained Workday `auth_state` in the Workday auth artifact payload and in structured event detail, while keeping job-row queue semantics simple through `failure_type` + `error_message`.
- Introduce explicit stopped-state semantics for at least:
  - `auth_failed` for explicit credential rejection
  - `auth_unknown` for exhausted recovery with no explicit rejection
  - `auth_guarded` for tenant-scoped protective skips
  - `service_unavailable` only if a maintenance path eventually stops after retry exhaustion
- Replace `_write_auth_failure_log()` with a richer Workday auth-result writer that can represent both stopped and retryable states. The artifact should be the source of truth for detailed diagnosis.
- Move Workday auth routing toward one explicit classifier/state-machine seam instead of continuing to branch implicitly in `_handle_auth()` and then re-branch again in the page loop.
- Create a dedicated `tests/test_autofill_workday.py` file and keep most Workday auth tests pure/helper-based. Use minimal fake marker/page objects and existing saved artifacts rather than relying on browser-only tests.
- Update queue/detail copy through the existing `error_message` path rather than inventing a new top-level UI card. Put deeper evidence in the TUI Attention panel and web logs path.

## System-Wide Impact

- **Board runtime:** `scripts/autofill_workday.py` becomes the single Workday auth-state producer.
- **Shared pipeline accounting:** `scripts/pipeline_orchestrator.py` stops treating all Workday auth artifacts as the same permanent failure.
- **DB/API model:** `scripts/job_db.py` gains scope-aware auth accounting and better artifact sync semantics.
- **Reviewer surfaces:** `scripts/job_tui.py`, `scripts/job_web.py`, and `scripts/static/app.js` expose clearer Workday auth reasons without requiring a rerun to understand the stop.
- **Documentation:** Workday guidance in `docs/autofill-patterns.md`, `docs/board-architecture.md`, and `docs/worker-pipeline-patterns.md` must match the new runtime contract.

## Open Questions

### Resolved During Planning

- External research is unnecessary; local repo patterns are sufficient.
- Workday should get a dedicated auth-state helper/test seam rather than more inline branching.
- `auth_scope` should be durable on the `jobs` row because the tenant-scoped guard needs to query historical failures cheaply and accurately.
- The finer-grained `auth_state` should live in Workday artifacts and event detail rather than becoming a new generic top-level job column.
- The web queue/detail should keep using `error_message` for the primary surface; deeper auth evidence should be available through logs/artifacts instead of a new job-detail panel.

### Deferred to Implementation

- What exact helper shape is least awkward for Playwright-facing Workday marker extraction: DOM text snapshot plus visible CTA labels, or a narrower typed marker object?
- Which authenticated non-form destinations need distinct recovery handling beyond `userHome` / candidate-home style pages?
- Whether the tenant key should prefer a normalized `host + site` format or a smaller board-specific slug, as long as it is stable across job URLs and redirect/login URLs.

## High-Level Technical Design

> This is directional guidance for implementation review, not implementation code.

```text
WorkdayAuthResult
  auth_state:
    credential_rejected
    maintenance
    create_account_gate
    password_reset_gate
    authenticated_non_form
    unknown
  auth_scope: workday:<tenant>/<site>
  last_attempted_step: sign_in | password_reset | create_account
  page_url
  heading_text
  alert_text
  visible_actions[]
  page_text_excerpt
  message
  suggestions[]

autofill_workday.py
  detect auth markers
  classify auth state
  run sign_in -> password_reset -> create_account in order
  write WorkdayAuthResult artifact

pipeline_orchestrator.py
  parse WorkdayAuthResult
  map auth_state to retry/stop behavior
  count only auth_failed rows with matching auth_scope
  emit auth_guarded stop when same-tenant credential failures reach threshold
```

## Implementation Units

- [x] **Unit 1: Centralize Workday auth-state detection and recovery order**

**Goal:** Make `scripts/autofill_workday.py` the single producer of a specific Workday auth-state result instead of a generic failure blob.

**Requirements:** R1-R7, R13, R14

**Dependencies:** None

**Files:**
- Modify: `scripts/autofill_workday.py`
- Add: `tests/test_autofill_workday.py`

**Approach:**
- Introduce pure helpers that extract and classify Workday auth markers from the current page: heading text, visible CTA labels, alert text, URL, maintenance text, and authenticated-non-form states.
- Rework `_handle_auth()` so the approved order is explicit and consistent: sign in first, then password reset, then create account.
- Remove or refactor create-account-first fallback branches in both `_handle_auth()` and the later Workday page loop so there is only one recovery ordering contract.
- Replace `_write_auth_failure_log()` with a richer writer that records `auth_state`, `auth_scope`, `last_attempted_step`, and the preserved evidence needed for diagnosis.
- Treat `/userHome` or similar authenticated-but-not-on-form landings as explicit intermediate states that can be resumed, not immediate auth failure.

**Execution note:** Add characterization coverage first using saved Workday artifacts and helper-state fixtures before reshaping the runtime branches.

**Patterns to follow:**
- Helper-state classification pattern in `tests/test_autofill_linkedin.py`
- Centralized-variant-handling guidance from `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`

**Test scenarios:**
- Normal `Create Account / Sign In` gateway with no explicit rejection.
- Explicit sign-in rejection via alert text.
- Forgot-password / reset-password page after failed sign-in.
- Explicit Workday maintenance / service interruption page.
- Authenticated redirect to `userHome` or equivalent non-form page.
- Create-account flow requiring email verification.

**Verification:**
- The Workday runtime produces a specific auth-state artifact for each characterization scenario.
- The runtime order is sign in, then password reset, then create account, with no hidden create-account-first branch remaining.

- [x] **Unit 2: Make auth accounting tenant-scoped and outcome-aware**

**Goal:** Move Workday auth counting and stop/retry behavior out of the generic board-wide `auth_failed` bucket.

**Requirements:** R3-R10, R14

**Dependencies:** Unit 1

**Files:**
- Modify: `scripts/job_db.py`
- Modify: `scripts/pipeline_orchestrator.py`
- Modify: `tests/test_pipeline_orchestrator.py`
- Modify: `tests/test_job_db.py`
- Modify: `tests/test_job_worker.py`

**Approach:**
- Add an `auth_scope` column and migration in `scripts/job_db.py` so Workday tenant identity is queryable without reparsing artifacts at guard time.
- Extend `get_recent_auth_failures()` to accept an optional `auth_scope` and count only explicit `auth_failed` rows within that scope when provided.
- Update the Workday pre-submit guard in `scripts/pipeline_orchestrator.py` to derive the tenant scope from the Workday URL and emit `auth_guarded` stops instead of generic `auth_failed` skips.
- Parse Workday auth artifacts by `auth_state` rather than globbing all `*_auth_failure.json` artifacts into a permanent stop. Route:
  - `credential_rejected` -> stopped `auth_failed`
  - `maintenance` -> transient retry/backoff path
  - `unknown` -> stopped `auth_unknown`
- Keep the current threshold of three failures in twenty-four hours, but scope it to the Workday tenant only.
- Clear stale Workday auth artifacts before reruns so an older failure artifact cannot mask a later recovery attempt.
- Keep board cooldown tied only to actual rate-limit messages; Workday auth and maintenance messages should be specific enough that `job_worker.py` does not treat them as 429-style board throttling.

**Patterns to follow:**
- Existing three-tier retry/failure classification in `scripts/pipeline_orchestrator.py`
- Existing DB migration and query-shaping patterns in `scripts/job_db.py`

**Test scenarios:**
- Three explicit credential failures on one Workday tenant trigger a guard skip for that same tenant only.
- A Workday maintenance artifact does not increment the guard counter and flows into retry behavior instead of permanent stop behavior.
- A stopped unknown Workday auth state does not count toward the guard.
- Non-Workday boards keep their existing board-wide auth-failure behavior.
- A rerun after stale Workday auth artifacts does not get short-circuited by the old artifact.

**Verification:**
- `get_recent_auth_failures()` distinguishes Workday tenant scope while preserving existing board-wide behavior for other boards.
- The orchestrator no longer turns every Workday auth artifact into `failure_type='auth_failed'`.

- [x] **Unit 3: Surface precise Workday auth reasons across DB sync, logs, and review UI**

**Goal:** Make the richer Workday auth result visible through the existing job surfaces without inventing a parallel UI system.

**Requirements:** R7, R11, R12

**Dependencies:** Units 1-2

**Files:**
- Modify: `scripts/job_db.py`
- Modify: `scripts/job_web.py`
- Modify: `scripts/static/app.js`
- Modify: `scripts/job_tui.py`
- Modify: `tests/test_job_web.py`

**Approach:**
- Update `sync_job_from_disk()` so Workday auth artifacts populate a useful `error_message` from the artifact `message` and preserve scope-aware semantics where appropriate.
- Extend `/api/jobs/{job_id}/logs` in `scripts/job_web.py` to include Workday auth artifacts, mirroring the existing unsupported-board path.
- Keep web queue/detail rendering on `error_message`, but make sure Workday-specific messages are precise enough to distinguish credential rejection, tenant guard skip, maintenance retry exhaustion, and unknown auth state.
- Expand the TUI Attention panel to display Workday `auth_state`, `auth_scope`, `last_attempted_step`, and suggestions when present.
- Preserve CLI/worker parity by keeping the primary summary on the same persisted `error_message` / `failure_type` fields that existing non-web surfaces already consume.
- Preserve the current minimal queue-row UI pattern rather than adding a new top-level Workday diagnostics card.

**Patterns to follow:**
- Existing queue/detail error-message surfaces in `scripts/static/app.js`
- Existing Attention-panel artifact-reading pattern in `scripts/job_tui.py`
- Existing job logs aggregation in `scripts/job_web.py`

**Test scenarios:**
- Workday auth artifact message is synced into the job row and visible in API payloads.
- Web logs endpoint includes Workday auth JSON alongside existing unsupported-board diagnostics.
- TUI Attention view renders richer Workday auth evidence instead of only a generic auth failure message.
- Guard skip rows clearly say the tenant was skipped due to repeated credential rejection on that tenant.

**Verification:**
- A reviewer can answer “was this credentials, maintenance, or unknown?” from current job surfaces and saved artifacts without rerunning the job.

- [x] **Unit 4: Sync Workday docs and contract language to the new runtime**

**Goal:** Bring repo guidance back into parity with the implemented Workday contract so the next edit does not reintroduce the old generic failure path.

**Requirements:** R5, R8-R12, R14

**Dependencies:** Units 1-3

**Files:**
- Modify: `docs/autofill-patterns.md`
- Modify: `docs/board-architecture.md`
- Modify: `docs/worker-pipeline-patterns.md`
- Modify: `agent_preferences.md` only if final implemented behavior differs from the already-approved order

**Approach:**
- Update Workday board guidance to describe the concrete auth-state model, sign-in-first recovery order, tenant-scoped guard, and maintenance retry behavior.
- Replace stale “auth failure vs captcha” phrasing that implies all Workday auth artifacts represent wrong-password or locked-account states.
- Keep agent preferences unchanged unless implementation requires a product-level change from the already approved order.

**Patterns to follow:**
- Existing board-specific guidance sections in `docs/autofill-patterns.md`
- Existing architecture and workflow docs that explain queue/worker/runtime contracts

**Test scenarios:**
- Documentation review only; no new code tests required.

**Verification:**
- The docs no longer contradict the Workday runtime or the new requirements doc.

## Recommended Test Matrix

- `tests/test_autofill_workday.py`
  - new pure helper tests for marker extraction, state classification, tenant extraction, and artifact payload shaping
- `tests/test_pipeline_orchestrator.py`
  - scope-aware auth counting, `auth_guarded` behavior, maintenance retry routing, unknown auth stop routing
- `tests/test_job_db.py`
  - `auth_scope` migration behavior and DB sync of Workday auth artifact messages
- `tests/test_job_web.py`
  - logs endpoint includes Workday auth artifact content
- `tests/test_job_worker.py`
  - current failure-type forwarding continues to work with new non-generic auth failure types

## Validation Strategy

- Characterization-first for Workday auth states: use the saved FactSet and Snapchat Workday artifacts as known examples of “normal auth gateway” vs “maintenance page”.
- Verify that the same-tenant guard trips only after three explicit credential rejections on one Workday tenant and does not block unrelated Workday employers.
- Verify that a Workday run reaching `userHome` or similar authenticated non-form states is resumed rather than immediately written as auth failure.
- Verify that web queue/detail, web logs, and TUI Attention all tell the same story for one stopped Workday job.

## Implementation Order & Dependencies

1. Unit 1 first. The runtime classifier and artifact contract define the data shape every later unit consumes.
2. Unit 2 second. Once Workday emits specific states, the orchestrator and DB can route and count them correctly.
3. Unit 3 third. After the data shape settles, wire the review surfaces and logs to it.
4. Unit 4 last. Sync docs to the final implemented behavior rather than documenting an intermediate shape.

## Risks & Dependencies

- The largest execution risk is classifier drift: if Workday marker extraction stays too ad hoc, the new taxonomy will still mislabel gateway states. Unit 1 needs direct helper tests to keep that risk contained.
- `auth_scope` migration work must preserve existing non-Workday auth-failure queries and not break older DB files that lack the new column before migration runs.
- Workday tenant derivation must stay stable across canonical job URLs, login redirects, and reset links; otherwise the tenant-scoped guard will quietly fragment or over-aggregate failures.
- Retry copy and stop copy must remain distinct from rate-limit language so worker cooldown behavior does not regress.

## Sources & References

- **Origin document:** [2026-03-26-workday-auth-classification-and-recovery-requirements.md](../brainstorms/2026-03-26-workday-auth-classification-and-recovery-requirements.md)
- Related code: [autofill_workday.py](../../scripts/autofill_workday.py), [pipeline_orchestrator.py](../../scripts/pipeline_orchestrator.py), [job_db.py](../../scripts/job_db.py), [job_tui.py](../../scripts/job_tui.py), [job_web.py](../../scripts/job_web.py), [submit_application.py](../../scripts/submit_application.py)
- Related tests: [test_autofill_linkedin.py](../../tests/test_autofill_linkedin.py), [test_pipeline_orchestrator.py](../../tests/test_pipeline_orchestrator.py), [test_job_db.py](../../tests/test_job_db.py), [test_job_web.py](../../tests/test_job_web.py), [test_job_worker.py](../../tests/test_job_worker.py), [test_submit_application.py](../../tests/test_submit_application.py)
- Institutional learning: [fragile-question-classifier-regression-cascade.md](../solutions/logic-errors/fragile-question-classifier-regression-cascade.md)

## Next Step

Execute this plan with `ce:work`, starting with a new `tests/test_autofill_workday.py` characterization layer and the Workday artifact contract before touching the tenant-scoped guard logic.
