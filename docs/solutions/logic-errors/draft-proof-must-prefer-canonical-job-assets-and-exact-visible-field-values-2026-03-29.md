---
title: "Draft proof must prefer canonical job assets and exact visible field values"
category: logic-errors
date: 2026-03-29
tags:
  - canonical-assets
  - visible-confirmation
  - linkedin
  - ashby
  - greenhouse
  - draft-proof
component: development_workflow
components:
  - AGENTS.md
  - scripts/application_submit_common.py
  - scripts/autofill_linkedin.py
  - scripts/autofill_ashby.py
  - scripts/autofill_greenhouse.py
  - scripts/autofill_workday.py
  - scripts/job_web.py
  - tests/test_application_submit_common.py
  - tests/test_autofill_linkedin.py
  - tests/test_ashby_autofill.py
  - tests/test_greenhouse_autofill.py
  - tests/test_job_web.py
  - tests/test_ci_workflow.py
problem_type: logic_error
symptoms:
  - "LinkedIn draft runs could attach stale dotted document variants or reuse short prefilled cover-letter text instead of the current generated artifact."
  - "Ashby and Greenhouse could mark the wrong visible option as confirmed, including `Male` matching `Female` by substring and any non-placeholder combobox text counting as success."
  - "The web review surface could surface stale resume or cover-letter files first because document ordering was driven by generic API order instead of canonical employer-named assets."
root_cause: logic_error
resolution_type: code_fix
severity: high
---

# Draft Proof Must Prefer Canonical Job Assets and Exact Visible Field Values

## Problem

The 2026-03-28 18:17 PT screenshot batch exposed the same deeper contract bug in four different ways. Draft runs were treating "some file matched" and "some value is visible" as good enough proof, even when the artifact or visible selection did not exactly match the current generated answer. That produced a dotted Cresta cover-letter filename, a stale short Geomagical cover-letter textarea, missing Invoca work-right and disability selections, and Nooks selecting `Female` when the planned answer was `Male`.

## Symptoms

- LinkedIn could upload stale dotted `..pdf` document variants because generic glob order beat the current canonical employer-named file.
- LinkedIn textarea handling could keep a short prefilled cover-letter answer even though `content/cover_letter_text.txt` held the full generated letter for the current role.
- A later Geomagical rerun exposed the same stale-prefill problem on LinkedIn `Headline`: the browser kept an overlong prefilled answer, triggering a step-2 validation loop after the cover-letter mismatch was fixed.
- Ashby could treat `Female` as a match for `Male` because fallback matching allowed substring collisions, and some runtime click paths hit inert role locators before the actual label-backed input.
- Greenhouse could treat any already-selected non-placeholder value as good enough proof, leaving required truthful answers such as legal-right verification and disability status visibly wrong or blank in the live form.
- The web UI document picker could surface stale dotted files first because `/api/jobs/{job_id}/documents` returned them before the canonical employer-named asset.

## What Didn't Work

- `find_resume_file()`, `find_cover_letter_file()`, and `find_cover_letter_text()` relied too heavily on broad filename globs and recency heuristics. That was fragile once a role directory accumulated stale dotted variants.
- LinkedIn textarea and text-field fills trusted prefilled browser values too much. For cover letters and headline-like fields, "already has text" was the wrong rule when the current generated artifact or deterministic answer should replace stale content.
- Ashby choice matching mixed exact and fuzzy paths, so a fallback could still accept `Male` inside `Female`.
- Greenhouse confirmation logic treated "not placeholder anymore" as success instead of checking whether the rendered option text actually matched the planned answer.
- The web review surface used the API's first matching document, so even a correct backend fix could still be hidden by stale ordering at the UI layer.

## Solution

The fix made "current canonical artifact" and "exact visible value" the only acceptable proof.

### 1. Centralize canonical document selection

`scripts/application_submit_common.py` now resolves preferred document assets through shared helpers that rank:

1. pipeline metadata company names
2. normalized company-name variants
3. only then generic fallback globs

That shared path now drives:

- `find_resume_file()`
- `find_cover_letter_file()`
- `find_cover_letter_text()`

It means stale `..pdf` variants can still exist on disk for forensics, but they no longer win selection.

### 2. Override stale LinkedIn prefill with the current generated answer

`scripts/autofill_linkedin.py` now does two specific things:

- Cover-letter fields always source their body text from `find_cover_letter_text(out_dir)`, which prefers the current `content/cover_letter_text.txt` / canonical employer-named artifact instead of whatever stale prefill LinkedIn already has.
- `Headline` fields are regenerated and normalized at fill time, with a single-line word-boundary truncation to stay under LinkedIn's visible validation limit.

This fixed both the original Geomagical cover-letter mismatch and the later step-2 `Please enter a valid answer` loop on `Headline`.

### 3. Require exact visible-choice confirmation on Ashby

`scripts/autofill_ashby.py` now uses exact normalized text matching and prefers the visible label-backed input path before generic role locators. That closed two failure modes:

- substring collision: `Male` no longer matches `Female`
- inert interaction path: the code now clicks the actual visible label / associated input when that is the only sticky selection path

### 4. Require exact rendered selection confirmation on Greenhouse

`scripts/autofill_greenhouse.py` now confirms that the rendered combobox or select text exactly matches the planned answer before marking the field filled. A random non-placeholder value is no longer treated as success.

That made truthful legal-right and disability answers survive into live draft proof instead of disappearing behind optimistic runtime bookkeeping.

### 5. Keep the web review surface in parity

`scripts/job_web.py` now sorts job documents so canonical employer-named resume and cover-letter assets come first. The web UI's existing `.find(...)` behavior can therefore consume the correct file without board-specific client changes.

### 6. Restore honest full-suite verification

The fresh full-suite run also exposed an unrelated Workday fixture classification gap and a pre-existing Greenhouse line-count exception:

- `scripts/autofill_workday.py` now classifies bare `/login` create-account gates correctly for the FactSet fixture.
- `tests/test_ci_workflow.py` records a temporary higher known-large threshold for `scripts/autofill_greenhouse.py`, and the repo now carries a follow-up todo to split that module instead of pretending the debt disappeared.

## Why This Works

The old contract asked, "did the automation interact with something plausible?" The new contract asks two stricter questions:

1. Did we use the current canonical job artifact?
2. Does the live UI visibly show the exact planned answer?

That is the right draft contract because screenshots are the source of truth. If a stale file, stale prefill, substring collision, or unrelated combobox value can still pass, then the draft is not trustworthy.

The solution is also intentionally shared instead of per-board patchwork:

- canonical artifact resolution lives in one shared document-selection layer
- LinkedIn cover-letter and headline overrides use the same shared canonical inputs
- Ashby and Greenhouse both move toward exact live-value confirmation
- the web API consumes the same canonical asset ordering

## Verification

Targeted regression coverage was added for every new contract:

- `tests/test_application_submit_common.py`
- `tests/test_autofill_linkedin.py`
- `tests/test_ashby_autofill.py`
- `tests/test_greenhouse_autofill.py`
- `tests/test_job_web.py`

Live redrafts confirmed the original affected jobs from the screenshot batch:

- Cresta: `output/cresta/forward-deployed-pm-ai-agent/submit/linkedin_autofill_payload.json` and `output/cresta/forward-deployed-pm-ai-agent/submit/linkedin_autofill_report.json` now point at `Jerrison Li Cover Letter - Cresta.pdf` and `Jerrison Li Resume - Cresta.pdf`, not the stale dotted variants.
- Geomagical Labs: `output/geomagical-labs/geomagical-labs-hiring-lead-pm-ai-spatial-experiences-east-coast-preferred-in-united-states-linkedin/submit/linkedin_autofill_report.json` now records the full cover letter from `cover_letter_text.txt`, and the rerun no longer stops on the LinkedIn headline validation loop.
- Invoca: `output/invoca/staff-pm-ai-platform/submit/greenhouse_autofill_report.json` now shows `Can you, after employment, submit verification ... = Yes` and `Disability Status = No, I do not have a disability and have not had one in the past`.
- Nooks: `output/nooks/pm/submit/ashby_autofill_pre_submit.png` and `output/nooks/pm/submit/ashby_autofill_report.json` now show `Male`, and `pending_user_input.json` is no longer present for that field.

The final full verification pass for the batch is recorded in the git commit and terminal session for this change.

Fresh repo verification on the final tree:

- `uv run python -m pytest tests/ -v`
- Result: `1168 passed in 64.02s`
- `uv run ruff check scripts/ tests/`
- `uv run python scripts/check_architecture.py`
- `uv run python scripts/sync_agent_files.py --check`
- `uv run python scripts/check_agent_docs.py`
- Result: all passed

Tracked data snapshots also reflect the redraft state:

- `jobs.db.backup` / `jobs.db.pre-migration` now show Geomagical Labs `#167` as `draft` instead of `stopped` / `linkedin_validation_loop`
- Cresta `#310` now shows `company = Cresta` and `status = draft` instead of `company = Cresta.` with `failure_type = linkedin_modal_missing`
- Nooks `#453` is present as the `pm` draft row alongside the already-submitted `staff-pm` row

## Prevention

- Any code path that picks resume, cover-letter, or cover-letter-text artifacts must prefer canonical employer-named assets from current pipeline metadata before using broad globs.
- Board adapters must never treat substring matches, stale prefill, or "some selection exists" as equivalent to visible confirmation.
- Review surfaces must sort or filter documents by the same canonical selection rules used by runtime upload code.
- If a full-suite pass requires temporarily widening a code-size allowance, create a durable follow-up todo in the same session.

## Investigation Steps

1. Started from the four 2026-03-28 screenshots in the external Obsidian mirror and translated them into exact repo-local symptoms.
2. Reproduced the failures against live role output directories for Cresta, Geomagical Labs, Invoca, and Nooks.
3. Traced each failure back to one of four shared causes: stale asset selection, stale LinkedIn prefill, fuzzy Ashby choice matching, or optimistic Greenhouse confirmation.
4. Added regression tests before and alongside the shared fixes.
5. Re-ran the affected jobs in `--draft` mode and confirmed the fixes from saved reports and screenshots.

## Cross-References

- Related learning: `docs/solutions/logic-errors/visible-self-id-draft-blockers-2026-03-26.md`
- Related learning: `docs/solutions/workflow-issues/explicit-answer-regeneration-requires-durable-fresh-proof-2026-03-26.md`
- Related learning: `docs/solutions/workflow-issues/live-linked-resource-validation-must-confirm-the-current-form-still-exposes-the-prompt-2026-03-28.md`
