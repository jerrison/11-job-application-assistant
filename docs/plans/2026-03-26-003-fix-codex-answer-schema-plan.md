---
title: "fix: Codex submit answer schema contract"
type: fix
status: active
date: 2026-03-26
origin: docs/brainstorms/2026-03-23-gpt-provider-support-requirements.md
---

# fix: Codex submit answer schema contract

## Overview

Live draft-safe submit reproduction exposed a narrower follow-up bug in the provider rollout: answer generation failed with an OpenAI structured-output schema 400 for `application_answers`, complaining that an optional question field was missing from the root `required` array. Local tracing shows the shared submit schema builder only marks required form questions as required, which no longer matches the current strict structured-output contract. This plan fixes the shared schema/prompt/validation contract and adds provider-path characterization so future live repros clearly show whether Codex CLI, the direct OpenAI shim, or a fallback provider actually executed.

## Problem Frame

The original Codex provider rollout made `ASSET_LLM_PROVIDER=codex` a supported path, but live submit verification surfaced a provider/schema seam that was not pinned by direct tests.

- `scripts/application_submit_common.py::generate_application_answers()` and `scripts/autofill_greenhouse.py::_generate_application_answers()` both delegate to the shared `_run_answer_generation_provider()` path.
- That shared runner builds a strict JSON schema for submit answers via `build_application_answers_json_schema()` when the provider path requests the OpenAI shim.
- The current builder only includes form-required fields in the root `required` array, even when optional fields are present in `properties`.
- Current OpenAI structured-output guidance requires every declared property to appear in `required`, with optionality modeled via `null` unions instead of omission.
- The live raw artifact surfaced this OpenAI schema failure during a `codex` reproduction, so the implementation must both fix the schema contract and make the executing provider path explicit rather than assuming the route is already understood.

## Requirements Trace

- R1. The Codex provider rollout from the origin doc remains usable for submit answer generation without manual provider-specific edits.
- R2. Shared answer generation must emit a schema accepted by the current OpenAI strict structured-output contract.
- R3. Optional questions must remain skippable without forcing junk filler into cached answers, payload steps, or board-specific answer handling.
- R4. The fix must apply through shared answer-generation seams used across boards, including Greenhouse’s imported runner path.
- R5. Raw artifacts and characterization coverage must make it obvious which provider path actually ran when live submit verification fails.

## Scope Boundaries

- In scope: submit/autofill answer-generation schema construction, prompt contract, optional-answer validation semantics, provider-path characterization, and regression coverage.
- In scope: shared code used by non-Greenhouse boards and the Greenhouse path that imports the shared submit runner.
- Out of scope: resume/cover-letter drafting, general Codex CLI parity work, direct file-tool behavior in `scripts/openai_provider.py`, and unrelated provider auth/setup issues.
- Out of scope: changing question-classification policy or adding board-local answer overrides unless the shared contract reveals a true gap.

## Context & Research

### Relevant Code and Patterns

- `scripts/application_submit_common.py`
  - `build_application_answers_json_schema()`
  - `_run_answer_generation_provider()`
  - `generate_application_answers()`
  - `validate_generated_answers()`
- `scripts/autofill_greenhouse.py`
  - `_generate_application_answers()`
  - `_validate_generated_answers()`
  - shared import of `_run_answer_generation_provider()`
- `scripts/llm_provider.py`
  - `provider_command()`
  - `provider_command_for_mode()`
  - `effective_provider_settings()`
- `scripts/openai_provider.py`
  - `_response_text_format()`
  - strict `json_schema` wrapper for the Responses API
- Existing tests to extend
  - `tests/test_submit_application.py`
  - `tests/test_greenhouse_autofill.py`
  - `tests/test_llm_provider.py`
  - `tests/test_openai_provider.py`

### Institutional Learnings

- `docs/solutions/integration-issues/adding-new-llm-provider.md`
  - Provider issues in this repo often span more than one layer. Plans should inspect Python, shell, stored-provider state, and env inheritance instead of assuming one layer is authoritative.
- `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`
  - Keep fixes in shared classify/generate logic rather than board-local post-processing, and use corpus-backed regressions to prevent cross-board drift.
- There is no existing `docs/solutions/` entry for OpenAI strict-schema `required` compliance yet. This bug should become a new learning after implementation.

### External References

- OpenAI Structured Outputs guide: root schemas must be objects, `additionalProperties: false` is required, every declared property must be present in `required`, and optionality should be modeled with `null` unions.
- OpenAI strict function-calling guidance repeats the same rule set for strict schemas and nullable optional parameters.

## Key Technical Decisions

- Fix the contract in shared answer-generation code, not with a Greenhouse-only patch.
  - Rationale: most boards call `generate_application_answers()` directly, and Greenhouse imports the same shared runner seam for subprocess invocation and fallback behavior.
- Model optional submit answers as explicit `null` at the schema/prompt boundary instead of omitted keys.
  - Rationale: current OpenAI strict-mode rules require every field in `required`, while the existing validators already tolerate `None` for optional values.
- Preserve required conditional follow-up fallback to `N/A` / `NA` separately from optional-null semantics.
  - Rationale: these fields are still required by the application surface and should not be collapsed into ordinary optional omission.
- Add characterization around submit-mode provider routing.
  - Rationale: the live repro presented as a Codex run while surfacing an OpenAI schema-name error, so the fix must make the executing provider path provable.

## Open Questions

### Resolved During Planning

- The primary origin document for this follow-up is `docs/brainstorms/2026-03-23-gpt-provider-support-requirements.md`. The direct OpenAI API requirements doc is adjacent context, not the main source, because the user-reported failure sits on the Codex provider rollout.
- The completed March 23 provider plans remain historical baseline. This is a new follow-up fix plan rather than a reopening of those completed artifacts.
- The implementation should start in shared answer-generation code and use Greenhouse only as the highest-signal reproduction target.

### Deferred to Implementation

- Whether the live `codex` repro was truly executed by Codex CLI or by a misrouted/fallback OpenAI shim path must be settled by early characterization coverage and artifact inspection.
- Whether raw answer-generation artifacts should prepend structured provider metadata or whether strengthened characterization tests alone are sufficient can be decided once the minimal production change is clear.
- The exact helper shape for nullable schema branches (`type: ["string", "null"]` versus `oneOf`) should be chosen after touching the current builder, but it must preserve the existing multi-select contract.

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```text
submit/autofill caller
  -> question specs
  -> shared answer runner
       -> if provider uses strict OpenAI structured output:
            build schema where every field_name is in root required
            optional fields => nullable branch
          else:
            keep prompt-only JSON path
       -> invoke provider command
       -> capture raw artifact + provider identity
       -> parse JSON
       -> validate answers
            optional null => skip field
            required conditional blank/null => N/A
            required standard blank/null => error
       -> write cache / build board-specific steps
```

## Implementation Units

- [ ] **Unit 1: Rebuild the shared submit answer schema for current strict-mode rules**

**Goal:** Make `build_application_answers_json_schema()` produce a schema that current OpenAI strict structured-output validation accepts without weakening required-field guarantees.

**Requirements:** R2, R3, R4

**Dependencies:** None

**Files:**
- Modify: `scripts/application_submit_common.py`
- Test: `tests/test_submit_application.py`
- Test: `tests/test_openai_provider.py`

**Approach:**
- Refactor `build_application_answers_json_schema()` so the root `required` array contains every `field_name`, not only form-required questions.
- Model optional text and single-select answers as nullable schema branches while keeping non-empty string constraints for required fields.
- Model optional multi-select answers as nullable string/array unions so the current parser contract remains compatible.
- Keep `additionalProperties: false` at the root and on any nested object branches if new object helpers are introduced.
- Add direct schema-builder coverage. The repo currently has no focused tests pinning optional-field behavior at this layer.

**Execution note:** Start with schema-builder characterization tests before changing prompt text so the contract change is isolated and reviewable.

**Patterns to follow:**
- Existing schema assembly in `build_application_answers_json_schema()`
- Existing OpenAI shim request assertions in `tests/test_openai_provider.py`
- Existing submit-path schema wiring assertion in `tests/test_submit_application.py`

**Test scenarios:**
- A mixed question set with required and optional text fields yields a root `required` array containing both field names.
- Optional text fields are nullable instead of omitted from the schema contract.
- Optional multi-select fields accept `null`, a string, or an array without dropping the current string/array compatibility.
- Required fields still reject empty-string payloads after validation.

**Verification:**
- OpenAI-schema-focused tests prove the built schema matches current strict-mode requirements and still passes through the provider abstraction unchanged.

- [ ] **Unit 2: Align prompt and validation semantics with nullable optional answers**

**Goal:** Ensure shared and Greenhouse answer-generation flows treat optional blanks as `null` while preserving required conditional follow-up fallbacks and board-specific option mapping.

**Requirements:** R2, R3, R4

**Dependencies:** Unit 1

**Files:**
- Modify: `scripts/application_submit_common.py`
- Modify: `scripts/autofill_greenhouse.py`
- Test: `tests/test_submit_application.py`
- Test: `tests/test_greenhouse_autofill.py`

**Approach:**
- Update the common and Greenhouse answer-generation prompt builders so optional fields the model would otherwise leave blank may be returned as JSON `null`.
- Keep required conditional follow-ups on the existing `N/A` path; do not reinterpret them as optional/null.
- Reuse existing validator behavior for `None` where possible, and only adjust if the new prompt contract reveals a real gap.
- Ensure cached answers created before the fix still load, while new answers with explicit `null` for optional fields validate and write cleanly.
- Keep deterministic-answer logic and board-local option aliasing unchanged unless the schema/prompt change exposes a true regression.

**Patterns to follow:**
- Current `validate_generated_answers()` and Greenhouse `_validate_generated_answers()` handling of `None`
- Existing Greenhouse `N/A` to `NA` option aliasing
- Existing stale-cache guards keyed by `question_specs`

**Test scenarios:**
- Optional pronunciation or extra-detail fields returned as `null` are skipped cleanly and do not poison cached answer reuse.
- Required conditional follow-ups still become `N/A` / `NA` when omitted or blank.
- Greenhouse payload generation with mixed optional and required questions succeeds without reintroducing the Duolingo OPT follow-up failure.
- A non-Greenhouse board using `generate_application_answers()` accepts the new nullable optional contract.

**Verification:**
- Shared submit and Greenhouse tests both pass with explicit `null` optional answers while preserving current required-field behavior.

- [ ] **Unit 3: Characterize and harden provider-path observability for submit-mode answer generation**

**Goal:** Remove ambiguity around whether a failing submit answer-generation run used Codex CLI, the OpenAI shim, or a fallback provider.

**Requirements:** R1, R4, R5

**Dependencies:** Unit 1

**Files:**
- Modify: `scripts/application_submit_common.py`
- Modify: `scripts/llm_provider.py`
- Test: `tests/test_submit_application.py`
- Test: `tests/test_llm_provider.py`
- Test: `tests/test_greenhouse_autofill.py`

**Approach:**
- Add characterization coverage that proves submit-mode `openai` requests receive `json_schema=application_answers` and submit-mode `codex` requests do not.
- If minimal production instrumentation is warranted, add lightweight provider metadata to raw answer-generation artifacts or failure messages without duplicating prompt contents.
- Verify fallback behavior keeps provider identity clear when the primary provider fails and the next provider in the chain runs.
- Keep this unit narrowly focused on provider observability and routing; do not reopen broader provider-parity work.

**Patterns to follow:**
- Existing `provider_command()` / `provider_command_for_mode()` tests in `tests/test_llm_provider.py`
- Existing raw-artifact failure handling in `_run_answer_generation_provider()`
- Existing fallback tests in `tests/test_submit_application.py` and `tests/test_greenhouse_autofill.py`

**Test scenarios:**
- `provider="openai"` attaches the schema name `application_answers`; `provider="codex"` does not.
- A failing primary provider writes artifacts that still identify the actual provider that failed.
- A fallback-chain run records the fallback provider clearly enough to explain which contract path executed.
- Greenhouse submit-mode tests can distinguish a shared schema failure from a board-local validation failure.

**Verification:**
- Tests make the provider/schema seam explicit, and a future live raw artifact cannot present a schema-name/provider mismatch without either failing characterization coverage or surfacing provider metadata.

- [ ] **Unit 4: Reproduce the fixed contract against live submit artifacts and capture the new learning**

**Goal:** Close the gap between mocked unit coverage and the live draft-safe submit flow, then document the structured-output contract pitfall for future provider work.

**Requirements:** R1, R2, R5

**Dependencies:** Units 1-3

**Files:**
- Test: `tests/test_submit_application.py`
- Test: `tests/test_greenhouse_autofill.py`
- Modify: `docs/solutions/` (new learning or refresh target determined during implementation)

**Approach:**
- Re-run draft-safe payload generation against a temp copy of a real saved submit artifact set, especially a Greenhouse role with optional and required follow-up fields.
- Verify both direct-OpenAI and Codex-configured paths as provider quota/auth allows, but keep deterministic mocked coverage as the primary contract guard.
- Capture a new `docs/solutions/` entry describing the strict-schema `required`/nullable rule once the implementation is validated, since the repo currently has no learning for this failure class.
- If live verification shows the `codex` repro was actually an `openai` or fallback path, record that explicitly in the solution doc rather than leaving it as tribal knowledge.

**Patterns to follow:**
- Existing temp-copy payload-only verification style used for submit/autofill regressions
- Existing `docs/solutions` frontmatter and cross-reference conventions

**Test scenarios:**
- A real Greenhouse temp-copy payload build no longer fails on optional-field schema validation.
- Cached answers from earlier runs still load when question specs match.
- The fix does not reintroduce stale-cache reuse of incomplete question sets.
- The documented learning explains the failure mode and the chosen contract shape.

**Verification:**
- Draft-safe live reproduction completes past answer generation for the affected role, and a new solution doc captures the resolved contract pitfall.

## System-Wide Impact

- **Interaction graph:** `generate_application_answers()` is used by Ashby, BambooHR, Dover, Gem, iCIMS, Lever, LinkedIn, Phenom, Workday, and other board submit flows. Greenhouse imports the same shared runner for provider invocation and fallback behavior.
- **Error propagation:** Invalid schema failures currently abort the primary provider and may or may not continue through the configured provider chain. The fix must preserve fallback behavior while making the failing provider explicit.
- **State lifecycle risks:** Cached `application_answers.json` payloads are keyed by `question_specs`, not provider. Nullable optional answers must therefore validate cleanly in both fresh runs and cache reuse paths.
- **API surface parity:** The direct `openai` shim and any future strict-structured-output path depend on the same answer schema contract. Non-OpenAI providers still rely on prompt-only JSON and should remain untouched unless characterization proves otherwise.
- **Integration coverage:** Unit tests will not prove live provider/auth/quota state, so one draft-safe payload reproduction against saved submit artifacts remains necessary after implementation.

## Risks & Dependencies

- Risk: Making every field required at the schema layer causes the model to fabricate filler for optional questions.
  - Mitigation: pair the schema change with prompt guidance that optional blanks should be returned as `null`, and validate that optional `None` values are skipped.
- Risk: A schema-only fix leaves the Codex/OpenAI path ambiguity unresolved.
  - Mitigation: add explicit characterization and lightweight provider observability in Unit 3.
- Risk: Board-local question peculiarities reappear if the implementation special-cases Greenhouse.
  - Mitigation: keep the contract in shared code and use Greenhouse only as the highest-signal reproduction target.
- Dependency: current OpenAI strict structured-output rules require root objects, `additionalProperties: false`, and every declared property to appear in `required`, with optionality modeled via `null` unions.
- Dependency: live provider verification may be limited by local Codex/Claude/OpenAI auth or quota state; mocked characterization coverage should therefore be treated as the primary merge gate, with live repro as confirmation rather than the only proof.

## Documentation / Operational Notes

- After implementation, capture this as a new `docs/solutions/` entry because the repo currently lacks institutional knowledge for strict-schema `required` compliance.
- If live reproduction still shows a `codex` run surfacing an OpenAI schema name after Unit 3 lands, treat that as a separate routing/observability follow-up rather than folding it back into board logic.
- Keep draft-safe verification on temp copies of saved submit artifacts; this bug class is in the answer-generation/payload phase and does not require live submission.

## Sources & References

- **Origin document:** `docs/brainstorms/2026-03-23-gpt-provider-support-requirements.md`
- Related plans:
  - `docs/plans/2026-03-23-001-feat-codex-cli-provider-parity-plan.md`
  - `docs/plans/2026-03-23-008-fix-openai-provider-cannot-write-files-plan.md`
- Related code:
  - `scripts/application_submit_common.py`
  - `scripts/autofill_greenhouse.py`
  - `scripts/llm_provider.py`
  - `scripts/openai_provider.py`
- Institutional learnings:
  - `docs/solutions/integration-issues/adding-new-llm-provider.md`
  - `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`
- External docs:
  - OpenAI Structured Outputs guide
  - OpenAI strict function-calling guide
