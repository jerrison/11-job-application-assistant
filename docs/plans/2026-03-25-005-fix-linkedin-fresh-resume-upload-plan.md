---
title: "fix: Force LinkedIn Easy Apply fresh resume upload"
type: fix
status: completed
date: 2026-03-25
origin: docs/brainstorms/2026-03-25-linkedin-fresh-resume-upload-requirements.md
---

# fix: Force LinkedIn Easy Apply fresh resume upload

## Overview

Refresh the LinkedIn-specific future work so it matches the approved resume-upload contract. The implementation needs to fix two separate seams: LinkedIn-sourced jobs can currently write `company_proper: Linkedin` into pipeline metadata, and the Easy Apply runtime only uploads a resume when it finds a raw file input. The desired behavior is narrower and more explicit than the previous plan: reuse the current role's existing generated resume file, force a fresh upload whenever LinkedIn exposes a visible resume-change path, allow hidden-control flows to continue to Review, and fail only when a visible upload attempt still cannot be verified (see origin: `docs/brainstorms/2026-03-25-linkedin-fresh-resume-upload-requirements.md`).

## Problem Frame

LinkedIn Easy Apply can preserve a previously selected resume across applications. In the observed Asurion draft, the live form still used `Jerrison Li Resume - Linkedin.pdf`, and the role metadata under `.pipeline_meta.json` also recorded `company_proper: "Linkedin"` instead of the actual employer. That means the current gap is not just "replace the attachment in the modal." The runtime first needs the correct employer metadata and employer-named resume artifact, then it needs explicit runtime handling for LinkedIn's visible upload/replace flows versus hidden resume-control flows (see origin: `docs/brainstorms/2026-03-25-linkedin-fresh-resume-upload-requirements.md`).

## Requirements Trace

- R1. LinkedIn drafts must target the current role's generated resume asset for the posting employer, not a generic LinkedIn-named asset.
- R2. LinkedIn-sourced jobs must persist employer-based metadata and filenames so generated resume artifacts use the actual employer name.
- R3. The flow must reuse the current role's existing generated resume file rather than regenerating a new file on every LinkedIn draft attempt.
- R4. If LinkedIn exposes a visible upload, replace, or change-resume path, the runtime must attempt a fresh upload of the current role's generated resume.
- R5. When a visible upload path is used, the runtime must require visible live-UI confirmation that the intended resume is attached before it counts as verified.
- R6. If LinkedIn reaches Review without ever exposing a visible resume upload or replace path, the draft may continue to Review.
- R7. If LinkedIn exposes a visible upload path, an upload attempt is made, and the result is still unclear or visibly wrong, the draft must remain incomplete.
- R8. Artifacts must distinguish `verified_fresh_upload`, `review_without_visible_resume_controls`, and `upload_verification_failed`.
- R9. Screenshot-visible LinkedIn UI state remains the source of truth whenever resume state is exposed.

## Scope Boundaries

- Do not change non-LinkedIn resume-upload behavior.
- Do not require per-attempt resume regeneration.
- Do not broaden this into a cross-board upload-replacement framework.
- Do not fail drafts solely because LinkedIn hides resume controls and still allows Review.
- Do not change the separate global positive-fit screening policy.

## Context & Research

### Relevant Code and Patterns

- `scripts/run_pipeline.py` derives `company` and `company_proper` before building role artifacts. The current fallback chain can keep generic LinkedIn metadata when the scraped JD company is weak, which then propagates into resume filenames.
- `scripts/scrape_job.py::_company_from_url()` falls back to the hostname and returns `Linkedin` for LinkedIn hosts, which is too generic for employer-named artifact generation.
- `scripts/asset_pipeline_state.py` already keys resume and cover-letter document filenames off `company_proper`; that naming seam exists and should be reused rather than replaced.
- `scripts/autofill_linkedin.py::_build_payload()` resolves `resume_path` from generated artifacts, but `_fill_wizard_step()` currently uploads only when `input[type="file"]` is directly visible.
- `scripts/autofill_common.py::write_report()` already supports explicit field states plus `planned_but_unconfirmed_fields`; the LinkedIn plan should reuse that reporting pattern rather than invent a disconnected artifact format.
- `tests/test_company_detection.py` already covers company-slug heuristics, `tests/test_asset_pipeline_state.py` covers employer-named document reuse, and `tests/test_autofill_linkedin.py` is the natural home for LinkedIn modal-state and upload-contract tests.
- `docs/autofill-patterns.md` and `docs/operational-rules.md` establish the key repo rule for this work: screenshots and confirmed live form state outrank planned payload values.

### Institutional Learnings

- `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`: when LinkedIn behavior is brittle or variant-heavy, move the decision into one explicit, test-covered path instead of scattering DOM heuristics across unrelated helpers.
- No `docs/solutions/patterns/critical-patterns.md` file exists in this repo today, so there is no separate critical-patterns document to account for here.

### External References

- None. The repo already has the relevant Playwright, artifact, and draft-verification patterns, and the remaining unknowns are LinkedIn DOM/runtime variants rather than framework-level design questions.

## Key Technical Decisions

- Keep this as a LinkedIn-only plan; no broader board generalization is required for this future work.
- Treat the problem as two linked seams: upstream LinkedIn employer normalization and downstream Easy Apply resume-state handling.
- Fix employer naming at the metadata-detection seam, not by renaming files after build.
- Reuse the current role's existing generated resume artifact; do not regenerate a new file solely because the user starts another LinkedIn draft.
- When LinkedIn exposes a visible resume-change path, always attempt a fresh upload and require visible confirmation before the resume field counts as verified.
- When LinkedIn never exposes a visible resume-change path but still reaches Review, continue the draft and emit the explicit artifact state `review_without_visible_resume_controls`.
- Use explicit artifact states instead of overloading a plain filled/unfilled boolean: `verified_fresh_upload`, `review_without_visible_resume_controls`, and `upload_verification_failed`.

## Open Questions

### Resolved During Planning

- This plan should be refreshed in place rather than replaced with a new file.
- The origin requirements document is the source of truth for behavior and scope (`docs/brainstorms/2026-03-25-linkedin-fresh-resume-upload-requirements.md`).
- Employer naming should be corrected upstream in LinkedIn company detection, not by a generic output-layout refactor.
- Hidden LinkedIn resume-control cases should continue to Review and be labeled explicitly rather than failing closed.
- Visible upload-path failures should fail closed and keep the draft incomplete.

### Deferred to Implementation

- Which exact LinkedIn DOM markers should count as a visible upload, replace, or change-resume path across modal variants.
- Which visible attachment markers are reliable enough to confirm the intended resume when LinkedIn truncates, reformats, or omits the full filename.
- Whether any LinkedIn variant requires removing a prior attachment before a fresh upload can take effect.

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```text
normalize_linkedin_employer_metadata(out_dir, jd_data, jd_raw)
  -> company/company_proper reflect employer, not LinkedIn host
  -> expected_resume_path points at employer-named generated file

detect_linkedin_resume_state(modal, expected_resume_path)
  -> visible_change_path
  -> visible_attachment_match
  -> no_visible_resume_controls

if visible_change_path:
  force_fresh_upload(expected_resume_path)
  if visible_attachment_match:
    record verified_fresh_upload
  else:
    record upload_verification_failed
    keep draft incomplete
elif review_reached_with_no_visible_resume_controls:
  record review_without_visible_resume_controls
  allow draft to continue
```

## Implementation Units

- [x] **Unit 1: Normalize LinkedIn employer metadata before asset selection**

**Goal:** Ensure LinkedIn-sourced roles persist the actual employer in metadata so the current role's resume artifact is employer-named before the Easy Apply runtime tries to upload anything.

**Requirements:** R1, R2, R3

**Dependencies:** None

**Files:**
- Modify: `scripts/scrape_job.py`
- Modify: `scripts/run_pipeline.py`
- Modify: `tests/test_company_detection.py`
- Test: `tests/test_asset_pipeline_state.py`

**Approach:**
- Tighten LinkedIn company detection so generic LinkedIn-host fallbacks do not become `company_proper` when better employer signals exist in JD title or scraped JD text.
- Preserve the existing `company_proper` -> document filename contract in `run_pipeline.py` and `asset_pipeline_state.py`; only the upstream company signal should change.
- Add or extend characterization coverage for LinkedIn-sourced JD titles/text so the metadata layer produces employer-named resume paths.

**Execution note:** Start with failing characterization tests for LinkedIn company detection before changing the fallback heuristics.

**Patterns to follow:**
- Existing company detection tests in `tests/test_company_detection.py`
- Existing employer-named document expectations in `tests/test_asset_pipeline_state.py`

**Test scenarios:**
- A LinkedIn-sourced JD title like `Asurion hiring Principal Product Manager in San Francisco Bay Area | LinkedIn` resolves employer metadata to `Asurion`, not `Linkedin`.
- Generic host-based company fallbacks still work for non-LinkedIn boards that do not expose better employer metadata.
- Employer-named document reuse in asset state still keys off the corrected `company_proper` value.

**Verification:**
- LinkedIn-origin output directories build or target `Jerrison Li Resume - Asurion.pdf` style artifacts when the employer is present in the JD signals.

- [x] **Unit 2: Detect LinkedIn resume state explicitly inside the Easy Apply modal**

**Goal:** Model the LinkedIn resume step as an explicit state machine instead of treating raw file inputs as the only actionable upload path.

**Requirements:** R3, R4, R6, R8, R9

**Dependencies:** Unit 1

**Files:**
- Modify: `scripts/autofill_linkedin.py`
- Test: `tests/test_autofill_linkedin.py`

**Approach:**
- Add one LinkedIn-specific resume-state helper that classifies the modal into states such as:
  - visible upload/replace/change path
  - visible matching attachment
  - visible non-matching attachment
  - no visible resume controls before Review
- Use that state helper to decide whether the runtime should attempt upload, accept hidden-control Review flow, or fail the draft after a visible-but-unverified upload path.
- Avoid scattering ad hoc DOM checks between `_build_payload()`, `_fill_wizard_step()`, and review-step handling.

**Execution note:** Add state-classification tests first so the LinkedIn DOM heuristics are locked down before upload behavior changes.

**Patterns to follow:**
- The explicit, centralized decision-path pattern from `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`
- Existing LinkedIn wizard-step handling in `scripts/autofill_linkedin.py`

**Test scenarios:**
- Raw `input[type="file"]` is visible.
- A visible `Upload`, `Replace`, or `Change` CTA exists but no raw file input is immediately visible.
- A visible attachment label already matches the expected employer-named file.
- The modal reaches Review without exposing any visible resume controls.

**Verification:**
- The LinkedIn runtime can distinguish "fresh upload must be attempted" from "hidden controls, continue to Review" and from "visible path exists but still unverified."

- [x] **Unit 3: Enforce the visible-upload contract for LinkedIn resume replacement**

**Goal:** Whenever LinkedIn exposes a visible resume-change path, force a fresh upload of the expected employer-named resume and fail closed if the live result remains unclear.

**Requirements:** R3, R4, R5, R7, R9

**Dependencies:** Unit 2

**Files:**
- Modify: `scripts/autofill_linkedin.py`
- Test: `tests/test_autofill_linkedin.py`

**Approach:**
- Reuse the current role's existing generated `resume_path`; do not regenerate content or documents in the submitter.
- Drive LinkedIn's visible upload/replace/change CTA path until the actual file input is available, then set the expected resume file.
- After the upload attempt, require a visible attachment confirmation signal before recording `verified_fresh_upload`.
- If a visible upload path was exposed and used but confirmation stays ambiguous or still reflects the wrong file, record `upload_verification_failed` and keep the draft incomplete.

**Patterns to follow:**
- Existing file-upload handling in `scripts/autofill_linkedin.py`
- Existing live-form-first draft philosophy in `docs/operational-rules.md` and `docs/autofill-patterns.md`

**Test scenarios:**
- Replace an existing visible stale LinkedIn resume with the expected employer-named resume.
- Upload from an empty visible state.
- A change CTA reveals the file input only after interaction.
- The upload path is visible but the post-upload UI never confirms the intended file, leaving the draft incomplete.

**Verification:**
- LinkedIn drafts only treat the resume as verified when a visible change path was exposed and the live UI confirms the intended attachment afterward.

- [x] **Unit 4: Surface LinkedIn resume-upload states in artifacts and docs**

**Goal:** Make the hidden-control path and visible-upload verification outcome explicit in reports and durable repo guidance.

**Requirements:** R6, R7, R8, R9

**Dependencies:** Unit 3

**Files:**
- Modify: `scripts/autofill_linkedin.py`
- Modify: `scripts/autofill_common.py`
- Modify: `README.md`
- Modify: `docs/autofill-patterns.md`
- Modify: `agent_preferences.md`
- Test: `tests/test_autofill_linkedin.py`
- Test: `tests/test_autofill_common.py`

**Approach:**
- Extend the LinkedIn runtime/report payload so artifacts can show whether the resume outcome was `verified_fresh_upload`, `review_without_visible_resume_controls`, or `upload_verification_failed`.
- Keep `review_without_visible_resume_controls` visibly separate from both success and failure so draft reviewers understand why Review was allowed without visible upload confirmation.
- Document the LinkedIn-specific rule that visible upload paths require fresh upload verification, while hidden-control Review paths are allowed but explicitly labeled.

**Patterns to follow:**
- `write_report()` conventions in `scripts/autofill_common.py`
- Existing LinkedIn board guidance in `docs/autofill-patterns.md`

**Test scenarios:**
- Report JSON and Markdown surface `verified_fresh_upload`.
- Report JSON and Markdown surface `review_without_visible_resume_controls` without misreporting it as a verified upload.
- Report JSON and Markdown surface `upload_verification_failed` as an actionable incomplete state.

**Verification:**
- Draft artifacts make the LinkedIn resume outcome explicit enough that reviewers do not have to infer it from raw screenshots or payload paths alone.

## Implementation Outcome

- `scripts/scrape_job.py` and `scripts/run_pipeline.py` now normalize LinkedIn-hosted roles to the posting employer instead of the `linkedin.com` host, so LinkedIn-sourced output metadata and generated document filenames use names like `Asurion` rather than `Linkedin`.
- `scripts/autofill_linkedin.py` now prefers the current role's employer-named resume artifact, explicitly models the LinkedIn resume step, intercepts visible upload/change controls through the file chooser path, and verifies selection from live LinkedIn UI markers before recording success.
- `scripts/autofill_common.py` now carries LinkedIn resume outcome artifacts through both JSON and Markdown reports so `verified_fresh_upload`, `review_without_visible_resume_controls`, and `upload_verification_failed` remain durable review states.
- Repo guidance was synced through `AGENTS.md`, `agent_preferences.md`, `docs/autofill-patterns.md`, `README.md`, and generated provider copies so the LinkedIn-specific resume contract is documented where implementers actually read it.

## Validation

- Real LinkedIn Easy Apply validation ran against `https://www.linkedin.com/jobs/view/4385754804/`.
- The live draft output moved to `output/asurion/asurion-hiring-principal-pm-in-san-francisco-bay-area-linkedin/`, proving the employer-normalization seam now uses `Asurion` metadata rather than `LinkedIn`.
- The generated resume file for that run was `output/asurion/asurion-hiring-principal-pm-in-san-francisco-bay-area-linkedin/documents/Jerrison Li Resume - Asurion.pdf`.
- The LinkedIn report at `output/asurion/asurion-hiring-principal-pm-in-san-francisco-bay-area-linkedin/submit/linkedin_autofill_report.md` records `resume_upload` with status `verified_fresh_upload` and expected file `Jerrison Li Resume - Asurion.pdf`.
- The screenshot artifact `output/asurion/asurion-hiring-principal-pm-in-san-francisco-bay-area-linkedin/submit/linkedin_autofill_pages/page_02.png` shows `Jerrison Li Resume - Asurion.pdf` selected in the LinkedIn resume step while the stale `Jerrison Li Resume - Linkedin.pdf` remains present but unselected.
- Verification completed with:
  - `uv run python -m pytest tests/ -v`
  - `uv run ruff check scripts/ tests/`
  - `uv run python scripts/check_architecture.py`
  - `uv run python scripts/check_agent_docs.py`
  - `uv run python scripts/sync_agent_files.py`
  - `uv run python scripts/sync_agent_files.py --check`

## System-Wide Impact

- **Interaction graph:** LinkedIn JD extraction and company normalization -> role metadata and document naming -> LinkedIn payload build -> Easy Apply modal state detection -> autofill report generation.
- **Error propagation:** Visible LinkedIn upload-path failures should stop LinkedIn draft completion; hidden-control Review paths should continue but remain explicitly labeled as unverified fresh uploads.
- **State lifecycle risks:** The persistent `.playwright-linkedin` profile can preserve stale resume state across different jobs and retries, so runtime decisions must distrust prior LinkedIn attachment state until it is re-evaluated.
- **API surface parity:** CLI, TUI, worker, web UI, and direct submit reruns all converge on the same LinkedIn autofill script and report artifacts, so LinkedIn-specific semantics must be encoded there rather than in one caller.
- **Integration coverage:** Unit tests are necessary but not sufficient; a future implementation should still validate at least one real LinkedIn Easy Apply flow with screenshots because DOM variants drive the core risk in this work.

## Risks & Dependencies

- LinkedIn resume widgets may vary between raw file inputs, CTAs that reveal file inputs, and attachment chips that truncate filenames.
- LinkedIn JD extraction may still produce weak employer metadata when the scraped page or Cloudflare-rendered content is thin, so company normalization must be resilient to partial signals.
- Allowing hidden-control Review flow is an intentional throughput tradeoff and must be documented clearly so reviewers do not misread it as a verified fresh upload.

## Documentation / Operational Notes

- This remains intentionally separate from the global positive-fit screening policy plan.
- If implementation uncovers the same "host metadata vs employer metadata" bug on other aggregator-sourced flows, that should trigger a separate generalization pass rather than silently expanding this LinkedIn-only plan.
- Because this behavior changes durable draft semantics, implementation should sync `AGENTS.md`-derived docs as needed once the code lands.

## Sources & References

- **Origin document:** [docs/brainstorms/2026-03-25-linkedin-fresh-resume-upload-requirements.md](../brainstorms/2026-03-25-linkedin-fresh-resume-upload-requirements.md)
- Related code: `scripts/run_pipeline.py`
- Related code: `scripts/scrape_job.py`
- Related code: `scripts/autofill_linkedin.py`
- Related code: `scripts/autofill_common.py`
- Related tests: `tests/test_company_detection.py`
- Related tests: `tests/test_asset_pipeline_state.py`
- Related tests: `tests/test_autofill_linkedin.py`
- Institutional learning: `docs/solutions/logic-errors/fragile-question-classifier-regression-cascade.md`
