---
title: "Stopped-job audits must be repo-local and artifact-backed"
category: workflow-issues
date: 2026-03-27
tags:
  - stopped-jobs
  - failure-evidence
  - workflow
  - investigations
  - documentation
component: development_workflow
components:
  - jobs.db
  - docs/brainstorms/2026-03-25-pipeline-resilience-requirements.md
  - docs/operational-rules.md
  - scripts/application_submit_common.py
  - scripts/autofill_common.py
  - scripts/autofill_linkedin.py
  - scripts/autofill_pipeline.py
  - scripts/autofill_workday.py
  - scripts/autofill_greenhouse.py
  - scripts/pipeline_orchestrator.py
  - scripts/job_worker.py
problem_type: workflow_issue
root_cause: missing_repo_record
resolution_type: workflow_improvement
severity: high
---

# Stopped-Job Audits Must Be Repo-Local and Artifact-Backed

## Problem

The live stopped-job investigation initially leaned on an external Obsidian tracker for convenience. That was fine as a scratchpad, but it is not a sufficient system of record for a repo that multiple agents and future sessions need to reason about autonomously. The repo must carry its own audit trail, current failure mix, and artifact-backed conclusions.

## Symptoms

- Important live-queue findings could exist only in an external note, making the repo incomplete for future debugging or planning.
- Earlier hypotheses could linger in convenience notes after the repo state had already changed.
- Agents resuming the work could not tell whether a conclusion came from live artifacts, worker logs, or an external summary.
- Dominant clusters such as LinkedIn, Workday, and Greenhouse were hard to prioritize because generic `retries_exhausted` states were not paired with one canonical in-repo audit snapshot.

## What Didn't Work

- Treating the Obsidian file as more than a mirror was too fragile. It is convenient for quick note-taking, but it is outside the repo and therefore outside the durable engineering context.
- Relying on queue counts alone was insufficient. The queue only showed coarse `failure_type` and error text, not whether saved artifacts were actually good enough to diagnose the root cause.
- Relying on one or two sampled historical hypotheses was not enough. The live artifact audit showed that some earlier explanations had already drifted.

## Solution

The stopped-job audit now lives in the repo first, with external notes treated as mirrors only.

1. Update the active brainstorm doc with a timestamped UTC investigation log and the current failure-cluster distribution:
   - `docs/brainstorms/2026-03-25-pipeline-resilience-requirements.md`
2. Record the durable workflow learning in this repo-local solution doc so future sessions understand the rule:
   - convenience trackers are mirrors
   - repo docs are the canonical engineering record
3. Anchor conclusions to saved artifacts and live DB queries instead of external summaries alone.

## Current Audit Snapshot

Audit windows:

- Initial baseline: `2026-03-27 23:39 UTC` through `2026-03-27 23:45 UTC`
- Workday/Autodesk refresh: `2026-03-28 04:16 UTC` through `2026-03-28 04:42 UTC`
- Linked-resource + provider-capacity refresh: `2026-03-28 16:19 UTC` through `2026-03-28 16:29 UTC`
- Greenhouse reclassification + disk-sync refresh: `2026-03-28 23:39 UTC` through `2026-03-28 23:42 UTC`
- LinkedIn/Workday follow-up refresh: `2026-03-29 00:01 UTC`
- Stopped-job rerun + sync verification refresh: `2026-03-29 06:07 UTC` through `2026-03-29 06:15 UTC`

Active stopped queue from `jobs.db`:

Baseline at `2026-03-27 23:40 UTC`:

- LinkedIn `retries_exhausted`: 30
- Greenhouse `retries_exhausted`: 22
- iCIMS `auth_failed`: 15
- unknown/unsupported: 14
- Workday `retries_exhausted`: 11
- Ashby `retries_exhausted`: 10
- null-board `retries_exhausted`: 10
- Workday `auth_unknown`: 6

Refresh at `2026-03-28 16:29 UTC`:

- LinkedIn `retries_exhausted`: 30
- Greenhouse `retries_exhausted`: 22
- iCIMS `auth_failed`: 15
- unknown/unsupported: 14
- Ashby `retries_exhausted`: 10
- null-board `retries_exhausted`: 10
- Workday `retries_exhausted`: 10
- Workday `auth_unknown`: 6

Refresh at `2026-03-28 23:39 UTC` after direct disk reconciliation and Greenhouse traceback backfill:

- iCIMS `auth_failed`: 16
- LinkedIn `linkedin_modal_missing`: 16
- Workday `auth_unknown`: 16
- unknown/unsupported: 15
- null-board `retries_exhausted`: 10
- Greenhouse `retries_exhausted`: 9
- Greenhouse `greenhouse_runtime_error`: 8
- LinkedIn `linkedin_validation_loop`: 8
- Ashby `retries_exhausted`: 6
- iCIMS `retries_exhausted`: 4

Refresh at `2026-03-29 00:01 UTC` after the LinkedIn payload/modal follow-up and legacy Workday auth normalization:

- Active non-archived stopped count: 122
- iCIMS `auth_failed`: 16
- LinkedIn `linkedin_modal_missing`: 16
- Workday `auth_unknown`: 16
- unknown/unsupported: 15
- null-board `retries_exhausted`: 10
- Greenhouse `retries_exhausted`: 9
- Greenhouse `greenhouse_runtime_error`: 8
- LinkedIn `linkedin_validation_loop`: 8
- Ashby `retries_exhausted`: 6
- iCIMS `retries_exhausted`: 4
- Workday `service_unavailable`: 1

Refresh at `2026-03-29 06:15 UTC` after targeted reruns and repo-local `bin/job-assets sync` verification:

The higher count reflects live queue movement plus a full repo-local sync of newer rows, not a reversal of the earlier Greenhouse and LinkedIn truthfulness fixes.

- Active non-archived stopped count: 195
- LinkedIn `linkedin_modal_missing`: 84
- unknown/unsupported: 19
- Workday `auth_unknown`: 16
- unknown `retries_exhausted`: 10
- LinkedIn `linkedin_validation_loop`: 9
- Greenhouse `retries_exhausted`: 8
- Greenhouse `incomplete`: 6
- iCIMS `retries_exhausted`: 6
- Avature `auth_failed`: 5

Artifact-backed findings from the live audit:

- LinkedIn is the largest active cluster, but the bigger problem is missing failure evidence:
  - 30/30 active stopped LinkedIn rows had `submit/linkedin_autofill_report.json` with `fields = []`
  - 28/30 referenced missing pre-submit screenshots
  - the queue mostly collapsed those rows into generic timeout/retry messages
- Workday splits into two concrete failure classes:
  - Autodesk `#38` shows a visible `My Information` validation failure on `How Did You Hear About Us?`
  - Levi's `#375` and Calix `#386` stop on visible `Create Account` pages with empty email/password fields
- Greenhouse needs better proof-gap capture more than the earlier sampled payload-gap hypothesis:
  - Mercury `#183` and Stripe `#313` already include the previously suspected missing custom question ids in their current payloads
  - only one active stopped Greenhouse row preserved `greenhouse_unknown_questions.json`
  - only one preserved `greenhouse_submit_debug.{html,png}`
- Ramp's stale linked-resource follow-up is now closed without changing the live stopped count:
  - `output/ramp/pm-new-bets/submit/application_answers.json` now includes a `linked_resources` block rooted in the merged db-fiddle adapter output
  - `output/ramp/pm-new-bets/submit/linked_resource_context.json` and `submit/linked_resource_evidence/` were rewritten during the `2026-03-28 16:20 UTC` rerun
  - `output/ramp/pm-new-bets/answer_refresh_status.json` now reads `status = fresh`, `answer_provider = openai`, `generated_answer_count = 5`, `resolved_at_utc = 2026-03-28T16:24:12+00:00`
- Provider quota exhaustion is still live:
  - 12 active stopped jobs still contain `You're out of extra usage · resets Mar 28 at 11pm (America/Los_Angeles)` in saved raw LLM files
  - `scripts/llm_common.sh` now treats explicit quota/usage banner text as a provider failure in both chain mode and legacy fallback even when the provider exits `0` and writes no output files
  - `tests/test_llm_common.py` now covers both the chain-mode and single-provider fallback cases, and a direct shell reproduction advances `openai -> gemini`
  - because the same 12 rows still appear in the refreshed stopped audit, the remaining work is live rerun or queue movement, not more quota-string detection logic
- Disk-to-DB reconciliation removed the stale failure-type drift that was hiding current reality:
  - the stale result-vs-DB count is now `0`
  - 23 LinkedIn rows now classify as `linkedin_modal_missing` or `linkedin_validation_loop` instead of generic `retries_exhausted`
  - Workday Turo `#143` now classifies as `application_questions_validation`
- LinkedIn follow-up now fixes two concrete runtime gaps surfaced by the stopped-job audit:
  - `scripts/autofill_linkedin.py::_build_payload()` now falls back to repo-root `master_resume.md` and `application_profile.md` when a role output lacks local copies, so candidate contact/profile fields no longer go blank in stopped-job payloads such as Geomagical and WitnessAI
  - `scripts/autofill_linkedin.py::_wizard_flow()` now retries reopening Easy Apply before classifying `linkedin_modal_missing`, which turns a plain job-page bounce into a recoverable resume-previous-application attempt instead of an immediate terminal stop
- Fresh reruns now clear stale current-attempt artifacts across LinkedIn and the shared browser pipeline:
  - `scripts/autofill_common.py::clear_current_attempt_artifacts()` removes stale report, review screenshot, submit debug, `application_submission_result.json`, and page-screenshot artifacts before a fresh fill while preserving payload and unknown-question files
  - `scripts/autofill_pipeline.py` and `scripts/autofill_linkedin.py` both call the shared helper, so boards using the shared browser pipeline inherit the same rerun-truthfulness contract
- The latest reruns prove that several former `linkedin_modal_missing` rows were stale or misclassified, not genuine modal failures:
  - Alt `#187` previously inherited a stale submit result; after cleanup and rerun, `output/alt/senior-pm-pricing/submit/linkedin_autofill_report.json` now shows `Does the listed salary meet your compensation requirements? = Yes` and the mixed immigration/sponsorship question = `No`, `output/alt/senior-pm-pricing/draft_status.json` records `draft_review_state.state = ready`, and the DB row now syncs as `draft` with cleared failure metadata
  - Gusto `#162` is not a modal bug. The rerun now writes `output/gusto/principal-pm-tax-platform/submit/application_submission_result.json` with `status = not_easy_apply`, `failure_type = external_apply`, and the current job-page screenshot `output/gusto/principal-pm-tax-platform/submit/linkedin_external_apply_page.png`
  - Skylo `#178` is not a modal bug either. The rerun now writes `output/skylo/principal-pm-ecosystem/submit/application_submission_result.json` with `status = not_easy_apply`, `failure_type = no_apply_button`, and the current job-page screenshot `output/skylo/principal-pm-ecosystem/submit/linkedin_no_apply_debug.png`
  - Hopper `#189`, Replicant `#272`, and ClickUp `#388` already had complete draft proof on disk; `scripts/job_db.py::sync_job_from_disk()` now promotes those rows back to `draft` instead of leaving stale stop metadata in place
- LinkedIn selector and answer-routing fixes from this slice generalize beyond the sampled reruns:
  - `_easy_apply_button()` and `_external_apply_button()` now ignore similar-jobs sidebar links, so future reruns click the primary job-detail control instead of a recommendation card
  - `scripts/application_submit_common.py` now classifies `Does the listed salary meet your compensation requirements?` as a salary-comfort check, and mixed sponsorship/visa prompts stay on the sponsorship-first truthful path instead of the earlier unconditional-authorization fallback
- The repo-local sync entrypoint is now re-verified on the real code path:
  - `uv run bin/job-assets sync` completed at `2026-03-29 06:15 UTC` and reported `Synced 421 of 476 jobs from disk artifacts`
- Refresh at `2026-03-29 14:32 UTC` after queue-wide redraft kickoff plus targeted LinkedIn and Greenhouse reruns:
  - All `195` active non-archived stopped rows were re-queued via `job-assets retry`, then the worker pool was restarted so the full stopped queue is now being redrafted through tracked DB events instead of one-off manual runs.
  - The stale-company LinkedIn canaries now have truthful current-attempt evidence on disk:
    - Charles Schwab job `#218` (`output/build-enablement-and-onboarding-plans-to/sr-genai-pm-aix-risk-management-compliance`) now writes `status = not_easy_apply`, `failure_type = external_apply`, and `submit/linkedin_external_apply_page.png`
    - Conviva job `#233` (`output/this/principal-product-builder-agent-analytics-optimization`) now writes `status = not_easy_apply`, `failure_type = external_apply`, and `submit/linkedin_external_apply_page.png`
  - The earlier Greenhouse proof-gap cluster is now resolved on live rows, not just in code:
    - Base `#232`, Fireblocks `#450`, Homeward `#454`, Klaviyo `#460`, Instacart `#470`, and Instacart `#475` all now preserve `submit/greenhouse_autofill_pre_submit.png` plus `submit/greenhouse_autofill_review.png`
    - after `uv run bin/job-assets sync` at `2026-03-29 14:32 UTC`, all six rows sync as `draft` instead of stopped `incomplete`
  - The latest sync reported `Synced 419 of 476 jobs from disk artifacts`
  - Live queue snapshot after the targeted reruns:
    - `submitted`: `171`
    - `stopped`: `128`
    - `draft`: `71`
    - `queued`: `58`
  - Largest remaining stopped clusters after this rerun slice:
    - LinkedIn `linkedin_modal_missing`: `81`
    - Workday `auth_unknown`: `14`
    - LinkedIn `linkedin_validation_loop`: `9`
    - LinkedIn `external_apply`: `5`
  - The generalized code follow-up from this slice is now also in the repo:
    - pipe-delimited LinkedIn titles like `Role | Charles Schwab | LinkedIn` and `Role | Conviva | LinkedIn` now resolve the real employer in both `scripts/run_pipeline.py` and `scripts/scrape_job.py`
    - non-Easy-Apply LinkedIn exits now always persist a current screenshot artifact in `application_submission_result.json`
    - Greenhouse review refresh now writes both required screenshot artifacts, which is why the former `incomplete` rows can now sync back to `draft`
- Refresh at `2026-03-29 14:45 UTC` after the LinkedIn screenshot-readability root-cause pass:
  - Charles Schwab `#218` exposed a second bug hiding behind the earlier screenshot complaint:
    - `scripts/autofill_linkedin.py::_capture_linkedin_surface_screenshot()` tried to clip `main#workspace > div > div`
    - Playwright raised `Clipped area is either empty or outside the resulting image`
    - `_capture_not_easy_apply_screenshot()` swallowed that exception, so the old `2258x9020` screenshot survived on disk and `application_submission_result.json` lost `artifacts.page_screenshot`
  - The fix now fails over to a fresh viewport screenshot whenever the structural clip is invalid:
    - focused regression coverage now includes `test_capture_linkedin_surface_screenshot_falls_back_to_page_viewport_when_clip_is_invalid`
    - a live rerun of `output/build-enablement-and-onboarding-plans-to/sr-genai-pm-aix-risk-management-compliance` rewrote `submit/linkedin_external_apply_page.png` from the stale `2258x9020` rail capture to a fresh readable `2720x1800` screenshot
    - the rerun also restored `application_submission_result.json.artifacts.page_screenshot`
  - Separate environment finding from the same investigation:
    - the shared LinkedIn browser profile currently reports `window.innerWidth = 4080`, `window.outerWidth = 1920`, and `window.devicePixelRatio ~= 0.67`
    - that zoomed-out profile state explains why even fresh LinkedIn screenshots look smaller than other boards
    - the code fix above removes the stale/blank evidence regression immediately; profile-zoom normalization is now an explicit follow-up instead of an unexplained annoyance
- Refresh at `2026-03-29 15:01 UTC` after the LinkedIn zoom-normalization pass:
  - The durable root cause lived in Chromium's persistent profile, not the screenshot code path:
    - `.playwright-linkedin/Default/Preferences` stored `partition.per_host_zoom_levels.www.linkedin.com.zoom_level = -6.025685102665476`
    - that value is roughly a one-third zoom level, which matches the earlier live page metrics (`devicePixelRatio ~= 0.67` with the submit flow's `device_scale_factor = 2`)
  - The generalized fix is now shared across every current LinkedIn entry point:
    - `scripts/browser_runtime.py::normalize_chromium_profile_zoom()` removes stored per-host zoom overrides and resets `profile.default_zoom_level` back to `0.0` when needed
    - `scripts/autofill_linkedin.py`, `scripts/url_resolver.py`, and `scripts/import_linkedin_saved.py` all invoke that helper before launching the persistent `.playwright-linkedin` browser profile
  - Live verification closed the loop:
    - the normalized profile now reports `window.innerWidth = 1360`, `window.innerHeight = 900`, `window.devicePixelRatio = 2`, and `visualViewport.scale = 1`
    - `.playwright-linkedin/Default/Preferences` now shows `per_host_zoom_levels = {}` and no default zoom override
    - proof screenshot: `output/playwright/linkedin-zoom-reset-proof.png`
    - proof rerun: Charles Schwab `#218` rewrote `submit/linkedin_external_apply_page.png` to a readable `2256x1648` crop under the normalized profile
- Refresh at `2026-03-29 19:01 UTC` after the LinkedIn / Workday screenshot-composition pass:
  - The next bottleneck was proof composition, not browser zoom:
    - LinkedIn Easy Apply was still storing a single modal viewport shot, which kept the text small even after the profile zoom was normalized
    - Workday proof still stopped at the last review surface even though the real autofill evidence lives across several wizard screens
  - The generalized fix is now shared in the common capture layer:
    - `scripts/autofill_common.py::capture_scrollable_locator_screenshot()` stitches internally scrollable locators into one PNG
    - `scripts/autofill_common.py::concatenate_images_vertically()` composes multi-part board proof into one artifact
    - `scripts/autofill_common.py::capture_full_page()` now uses the stitched-locator path for preferred selectors instead of only grabbing the visible scroll window
  - Board-specific proof capture now uses those primitives:
    - `scripts/autofill_linkedin.py` captures Easy Apply proof as modal header + stitched modal content when available, and non-modal LinkedIn pages now prefer structural job-detail capture before falling back to the top card
    - `scripts/autofill_workday.py` now writes `workday_autofill_pre_submit.png` by concatenating the current wizard page screenshots
  - Live evidence closed the loop:
    - Greylock rerun `output/greylock/sr-pm-security-ai/submit-20260329T185755Z/linkedin_autofill_pre_submit.png` moved from `2720x1800` to a tighter `1488x2316` proof image, with current step captures saved in `linkedin_autofill_pages/page_01.png` through `page_05.png`
    - Turo rerun `output/turo/lead-pm-host/submit-20260329T185846Z/workday_autofill_pre_submit.png` moved from `2048x4000` to `2048x12280`, concatenating `page_01_my_information.png`, `page_02_my_experience.png`, `page_03_application_questions.png`, and `page_04_review.png`
- Refresh at `2026-03-29 16:50 UTC` after the LinkedIn cluster sweep and Workday auth follow-up:
  - Workday's remaining classification hole is now closed, not just documented:
    - Walmart `#14` reruns to `status = auth_failed` and `auth_state = credential_rejected`
    - the durable fix in `scripts/autofill_workday.py` adds the real Workday wording variants (`wrong email address or password`, `account might be locked`) and re-normalizes auth state from saved markers before writing `workday_auth_failure.json`
  - The dominant LinkedIn stop cluster was a selector bug, not a modal bug:
    - a same-profile Playwright probe on Anthropic `#521` showed the real job page only exposed `Apply` plus `Responses managed off LinkedIn`
    - the same page also contained three offscreen recommendation cards whose whole-card text included `Easy Apply`
    - the old whole-page fallback selectors in `scripts/autofill_linkedin.py` were treating those recommendation cards as the current job's apply control
    - live probe artifacts: `output/playwright/anthropic-step1-probe.png` and `output/playwright/anthropic-step1-probe-after-click.png`
  - The generalized LinkedIn apply-control fix is now in code and validated on the full current backlog:
    - `scripts/autofill_linkedin.py` now only accepts generic `Apply` / `Easy Apply` fallbacks when the control's own label is exactly `Apply` or `Easy Apply`
    - `output/playwright/linkedin-modal-missing-rerun-20260329.jsonl` records a batch rerun of all `79` current-repo `linkedin_modal_missing` rows
    - those reruns collapsed completely into truthful non-Easy-Apply exits: `75` `external_apply` and `4` `no_apply_button`
    - after `uv run bin/job-assets sync`, the live queue has `0` current-repo `linkedin_modal_missing` rows
  - LinkedIn checkbox-group handling is now generalized across wizard steps:
    - visible fieldsets with required checkboxes now use the same answer-selection path as radio groups
    - that closes the ValoreMVP yes/no checkbox case and the Headspace demographic checkbox case
    - `output/playwright/linkedin-validation-loop-rerun-20260329.jsonl` shows the resulting rerun slice for the former `linkedin_validation_loop` cluster
    - the cluster dropped from `7` to `1`: SQUIRE `#221`, Snaplii `#315`, We/Headspace `#312`, and We/director-of-product `#478` are now `draft`; EarnIn `#273` and ValoreMVP `#304` are now truthful `linkedin_unknown_questions`

- Refresh at `2026-04-04 06:26 UTC` after the stopped+draft truth pass and fresh canary reruns:
  - Repo-local cohort manifest now lives at `output/_audit/current_repairable_cohorts.json`
  - Live non-archived queue truth from the shared audit helpers is:
    - `stopped`: `259 repairable`, `475 terminal`
    - `draft`: `269 repairable`, `94 ready`
  - The first shared queue-truth bug from this pass is now fixed and regression-covered:
    - `scripts/job_db.py::sync_job_from_disk()` no longer trusts stale `draft_status.json` ready hints when current repo-local proof is blocked
    - regression coverage: `tests/test_job_db.py::test_sync_job_from_disk_does_not_trust_stale_ready_draft_hint_when_current_proof_is_blocked`
  - A second shared draft-audit parser bug also turned out to be backlog-wide, not board-specific:
    - `scripts/pipeline_draft_proof.py::current_rendered_audit_inputs()` now accepts report-backed deterministic observations stored as `kind` + `value`
    - repeated `checkbox_group` rows now merge into one rendered observation instead of overwriting each other
    - report rows with blank `kind` still participate when they match a deterministic expected field
    - duplicate checkbox rows now ignore redundant generic affirmative markers such as `Yes` when a more specific selected label is present
    - regression coverage now includes:
      - `tests/test_pipeline_audit_loop.py::test_audit_draft_outcome_accepts_report_kind_and_value_for_rendered_audit`
      - `tests/test_pipeline_audit_loop.py::test_audit_draft_outcome_merges_checkbox_group_report_rows_for_rendered_audit`
      - `tests/test_pipeline_audit_loop.py::test_audit_draft_outcome_uses_matching_expected_field_when_report_kind_is_blank`
      - `tests/test_pipeline_audit_loop.py::test_audit_draft_outcome_ignores_generic_checkbox_affirmative_when_specific_value_exists`
  - Fresh canary reruns already changed the live truth in a useful way:
    - dbt Labs `#279` (`output/dbt-labs/staff-pm-developer-experience`) reran cleanly to `draft` with fresh current-attempt proof:
      - `submit/greenhouse_autofill_report.json`
      - `submit/greenhouse_autofill_pre_submit.png`
      - `submit/greenhouse_autofill_review.png`
      - `audit_draft_outcome(...) -> ready`
    - Plaid `#94` (`output/plaid/senior-pm`) no longer survives as a silent rendered mismatch:
      - rerun result: `status = stopped`, `failure_type = pending_user_input`
      - blocker artifact: `submit/pending_user_input.json`
      - blocker reason: optional LGBTQ+ identification could not be truthfully auto-confirmed on the live form, so the pipeline stopped before submit
    - Astera Labs `#36` was not a rerunnable draft at all:
      - the reset attempt repaired it back to `submitted`, which confirmed it was stale queue truth, not a fresh draft-proof problem
  - The residual repairable draft set is now much smaller and more honest:
    - stale inventory remains the largest family (`missing proof` or `application answers are missing`)
    - the remaining true rendered mismatches are concrete answer/evidence problems such as:
      - Plaid `#94`: `New York` expected vs `San Francisco` rendered on Lever
      - Robinhood `#234`: the office-willingness question is still missing from current Greenhouse evidence
      - Databricks `#411`, `#668`, `#1342`: the second sanctions/export-control checkbox question renders `U.S. citizen` even though the planned answer is `Not applicable`
      - Wheel `#855` and Valon Tech `#1135`: `Gender` still lacks current Ashby rendered evidence
  - The active `unsupported` bucket still includes jobs on boards we already support, so that tail is now documented as rerun debt instead of missing implementation:
    - Breezy-hosted domains: `zero-hash.breezy.hr = 3`, `bond.breezy.hr = 1`, `avantos.breezy.hr = 1`
    - Jobvite-hosted domains: `jobs.jobvite.com = 2`
    - Paycor-hosted domains: `recruitingbypaycor.com = 1`
  - As of this refresh, two slower canaries were still running under the worker pool and had not reached a terminal rerun result yet:
    - Zapier `#124` (`output/zapier/lead-pm-ai-capabilities`)
    - Zero Hash Breezy `#734` (`output/senior-product-manager/senior-pm-payments`)
  - Current queue truth after the sweep:
    - `submitted`: `186`
    - `stopped`: `172`
    - `draft`: `82`
    - `queued`: `57`
    - `generating`: `4`
  - Largest remaining stopped clusters are now:
    - LinkedIn `external_apply`: `89`
    - Greenhouse `job_closed`: `22`
    - Workday `auth_unknown`: `16`
    - LinkedIn `no_apply_button`: `8`
    - Workday `auth_failed`: `7`
    - LinkedIn `linkedin_unknown_questions`: `3`
    - LinkedIn `linkedin_validation_loop`: `1`
  - The lone remaining LinkedIn validation loop is now isolated to Raydar / Our client `#459`:
    - `output/our-client/pm-integrations/submit/linkedin_autofill_pages/page_04.png` shows prose answers being written into fields the page validates as decimal quantities
    - that is now a narrow numeric-question/labeling bug, not a broad wizard-loop class
- Refresh at `2026-04-04 06:38 UTC` after the locked-submission correction:
  - User feedback on Astera Labs was correct: locked submissions must stay `submitted`, not drift into `draft`.
  - The shared bug turned out to be duplicate proof ownership, not a worker transition bug:
    - `8` later rows were still pointing at repo-local proof already owned by older submitted jobs
    - the live repairs were `#508`, `#529`, `#539`, `#676`, `#723`, `#850`, `#1083`, and `#1326`
    - after re-sync, there are now `0` resolved proof directories where a submitted row still has a sibling in `draft`
  - Generalized fixes now in repo:
    - `scripts/job_db.py::_find_locked_output_dir_owner()` detects older submitted/locked owners of the same repo-local proof tree
    - `scripts/job_db.py::sync_job_from_disk()` now stops/archives later rows instead of letting them inherit `draft` truth from someone else's proof
    - `scripts/job_normalization.py::company_match_variants()` now adds collapsed slug-style aliases, and `jd_fingerprint()` now uses the same collapsed company identity
  - Regression coverage now includes:
    - `tests/test_job_db.py::test_sync_job_from_disk_archives_ready_draft_duplicate_when_locked_submission_owns_output_dir`
    - `tests/test_job_db.py::test_jd_fingerprint_matches_slug_company_variants`
    - `tests/test_job_db.py::test_add_job_duplicate_company_role_matches_slug_company_names`
  - Targeted verification passed:
    - `uv run python -m pytest tests/test_job_db.py -k "reconcile_duplicate_jobs or duplicate_company_role or sync_job_from_disk" -v`
    - `uv run ruff check scripts/job_db.py scripts/job_normalization.py tests/test_job_db.py`
  - Refreshed repo-local cohort manifest at `output/_audit/current_repairable_cohorts.json` now shows:
    - `stopped`: `258 repairable`, `474 terminal`
    - `draft`: `264 repairable`, `94 ready`
- Refresh at `2026-04-04 06:41 UTC` after batch-resetting the refreshed repairable cohort:
  - `520` of the refreshed repairable stopped/draft rows were reset through `pipeline_orchestrator.reset_job_to_new(..., initiator='codex_batch_redraft')`
  - only `2` rows refused reset:
    - Snorkel AI `#676`
    - Sigma Computing `#1083`
    - both had already been archived by the duplicate-proof repair and correctly stayed out of the active queue
  - Current live queue handoff while the worker keeps draining is:
    - `queued = 522`
    - `resolving = 1`
    - `generating = 3`
    - `draft = 94`
    - `stopped = 477`
    - `submitted = 244`
  - The remaining active stopped/draft backlog is now:
    - `stopped`: `1 repairable`, `476 terminal`
    - `draft`: `37 repairable`, `94 ready`
  - The refreshed handoff manifest is still `output/_audit/current_repairable_cohorts.json`
- Refresh at `2026-03-29 17:45 UTC` after the Workday auth sweep:
  - The dominant remaining Workday stop bucket is no longer opaque:
    - `scripts/workday_auth.py` now owns the shared Workday auth-state classifier/result builder, which keeps `scripts/autofill_workday.py` back under the repo file-size gate
    - `scripts/autofill_workday.py::_workday_preferred_locator()` now narrows even a single current match to a stable single locator, which removes the duplicate-email strict-mode crash that had been freezing LiveRamp, GM, Nasdaq, and Levi's at `Create Account`
    - `scripts/autofill_workday.py::_run_workday_auth_flow()` now:
      - truthfully exits Autodesk-style authenticated `View Application` pages as `already_applied`
      - surfaces revoked Gmail automation credentials from `gws` immediately
      - but still keeps later Workday recovery steps alive so a truer final auth result can override the environment warning when possible
  - Live evidence closed the loop:
    - Autodesk rerun now exits immediately as `already_applied` instead of the earlier false `auth_unknown`
    - `gws auth status` now proves the environment issue behind the password-reset subset: `token_valid = false`, `token_error = Token has been expired or revoked.`
    - `output/playwright/workday-auth-unknown-rerun-20260329T171633Z.jsonl` records a full rerun of all `12` current-repo `workday auth_unknown` rows
  - The `12` reruns split into two real manual buckets plus explicit credential rejection:
    - `5` reruns are now `auth_failed / credential_rejected` (`FactSet #426`, `GM #208`, `Nasdaq #347`, `Levi's #375`, `Zillow #314`)
    - `4` reruns remain `auth_unknown / sign_in_gate` because the tenant requires account verification before sign-in (`Calix #386`, `LiveRamp #200`, `OutSystems #383`, and one weaker sign-in-gate row on `We #423`)
    - `3` reruns remain `auth_unknown / password_reset_gate`, all with explicit `gws auth login` blockers (`Etsy #293`, `Relativity #19`, `Qualys #240`)
  - After sync, the current-repo Workday cluster is now:
    - `auth_failed = 8`
    - `auth_unknown = 7`
    - this is a net reduction from `16` opaque `auth_unknown` rows to `7`, with the remainder explicitly explained by account verification or revoked Gmail automation rather than a Workday selector/classification bug
- Refresh at `2026-03-29 18:03 UTC` after the queue-wide stopped-cluster proof audit:
  - Two more stale-stop families are now fixed in shared sync/proof code:
    - `scripts/job_db.py` now promotes ready draft proof even when a stale failed `application_submission_result.json` is still on disk
    - `scripts/submit_review_common.py::load_pending_user_input_for_submit_attempt()` now ignores wrong-board stale pending payloads and drops artifact blockers that the active proof already satisfies
  - Live queue movement from those fixes is immediate and artifact-backed:
    - Rubrik `#360` moved from stopped `greenhouse_runtime_error` to `draft`, with current proof in `output/rubrik-job-board/staff-platform-pm-platform-cloud-security/submit/greenhouse_autofill_pre_submit.png`
    - Engineer / Synopsys `#42` moved from stopped `retries_exhausted` to `draft`, with current proof in `output/engineer/engineer-the-future/submit/avature_autofill_pre_submit.png`
  - Cross-board handoff proof now resolves from the strongest current artifacts instead of the first stale hinted payload:
    - `scripts/submit_review_common.py::resolve_current_submit_artifacts()` now ranks candidate boards by proof strength
    - that stops stale LinkedIn payload files from masking downstream Greenhouse / Lever / iCIMS proof after `external_apply` / `no_apply_button` handoffs
    - representative evidence:
      - Altana `#529` still truthfully exits on LinkedIn `external_apply`, proven by `output/altana/principal-pm-customer-data-platform/submit/linkedin_external_apply_page.png`
      - Aircall `#542` now resolves as a Lever proof-bound stop instead of a stale LinkedIn-only stop, with the remaining blocker visible in `output/aircall/senior-pm-growth-for-small-businesses/submit/lever_autofill_review.png`
  - Workday stale-auth precedence is now correct:
    - `scripts/job_db.py` only lets `workday_auth_failure.json` classify the row when no newer `application_submission_result.json` already did
    - Snap `#107` now syncs as `workday / skipped_captcha` instead of the stale singleton `service_unavailable`
  - Current queue truth after the audit:
    - `submitted`: `173`
    - `stopped`: `116`
    - `draft`: `85`
    - `queued`: `54`
    - `generating`: `4`
  - Current largest stopped clusters after the audit:
    - LinkedIn `external_apply`: `83`
    - Workday `auth_failed`: `8`
    - LinkedIn `no_apply_button`: `7`
    - Workday `auth_unknown`: `7`
    - Avature `skipped_captcha`: `4`
    - iCIMS `skipped_captcha`: `3`
    - Unknown `unsupported`: `3`
    - Workday `skipped_captcha`: `1`
  - Most important audit conclusion:
    - there are now `0` stopped rows whose active repo-local proof already resolves as `ready`
    - the remaining stopped rows are truthful/manual stop families or still-blocked active proof, not stale queue metadata
- Refresh at `2026-03-29 18:22 UTC` after the draft-mode terminal-result reconciliation pass:
  - The final stale inconsistency hiding inside the LinkedIn manual buckets was an orchestration bug, not a board-state bug:
    - `scripts/submit_application.py` can return exit code `0` while still writing a terminal non-draft result such as `not_easy_apply`, `skipped_captcha`, or `pending_user_input`
    - `scripts/pipeline_orchestrator.py` previously treated every draft-mode `0` as "generate draft proof now"
    - that let truthful terminal outcomes get overwritten as stopped `incomplete` inside the worker, after which later disk sync could restore the old `failure_type` and leave an internally inconsistent row
  - The generalized fix is now shared across both draft submit paths:
    - `scripts/pipeline_orchestrator.py` short-circuits on terminal current-attempt `application_submission_result.json` states before any draft-summary generation or draft-proof completeness checks
    - the same short-circuit runs on both the first draft submit attempt and the auto-fix retry path
    - `scripts/job_db.py::_SUBMISSION_STATUS_MAP` now also maps `pending_user_input`, `unknown`, and `skipped_auth_failure` so disk sync stays aligned with the statuses already emitted by board scripts
  - Live reruns closed the loop on the only two remaining inconsistent rows:
    - CoreWeave `#504` now stops truthfully as LinkedIn `external_apply`, with fresh proof in `output/coreweave/product-strategy-principal/submit/linkedin_external_apply_page.png`
    - Supermicro `#534` now also stops truthfully as LinkedIn `external_apply`, with fresh proof in `output/supermicro/principal-pm-dcim-software-27484/submit/linkedin_external_apply_page.png`
    - both reruns now log `submission_result_stopped` instead of `draft_incomplete`
  - Queue truth after those reruns is stable:
    - `submitted`: `173`
    - `stopped`: `116`
    - `draft`: `85`
    - `queued`: `54`
    - `generating`: `4`
  - The live stopped-cluster split is now clean:
    - LinkedIn `external_apply`: `84`
    - Workday `auth_failed`: `8`
    - Workday `auth_unknown`: `7`
    - LinkedIn `no_apply_button`: `6`
    - Avature `skipped_captcha`: `4`
    - iCIMS `skipped_captcha`: `3`
    - Unknown `unsupported`: `3`
    - Workday `skipped_captcha`: `1`
  - The stale symptom is now gone from the live queue:
    - `0` stopped rows still say `Incomplete draft ...`
    - LinkedIn stopped rows now break cleanly into `84` truthful `external_apply` messages and `6` truthful `no_apply_button` messages
- Some stopped rows remain on truthful non-code paths after the rerun slice:
  - Toast `#169` is currently a Greenhouse JD-validation / scrape-rate-limit stop, not a draft-proof regression
  - Aircall `#542` is currently a Lever proof-bound stop on an unconfirmed visible `Current location` field, not a stale LinkedIn `external_apply` state
- Greenhouse payload-build regressions are now both fixed in code and reclassified in the live queue:
  - `scripts/autofill_greenhouse.py` now consumes the shared `nda_noncompete`, `conflict_of_interest`, `city_location`, `education`, and broader `work_authorization` categories instead of silently dropping those required fields from the payload
  - build-time missing-required-field failures now write `application_submission_result.json` with `failure_type = greenhouse_runtime_error`
  - `scripts/job_db.py` now backfills historical Greenhouse pre-result failures from the latest `submit_output` traceback, which reclassified Base `#232`, EnergyHub `#275`, Stripe `#313`, SoFi `#335`, Rubrik `#360`, Axon `#362`, Afresh `#393`, and Fivetran `#422`, plus Riot `#248` as `greenhouse_unknown_questions`
- Legacy Workday auth artifacts now normalize from saved evidence instead of trusting stale status strings:
  - `scripts/job_db.py` now maps maintenance-page evidence to `service_unavailable`, sign-in-gate exhaustions to `auth_unknown`, and explicit rejection evidence to `auth_failed`
  - stale generic retry wrappers can now be replaced by the artifact-backed Workday reason during disk sync
  - before the `18:03 UTC` result-precedence fix, Snap `#107` synced as `service_unavailable` from stale maintenance evidence instead of the newer submit result
- The remaining nine Greenhouse `retries_exhausted` rows are a smaller mixed tail, not the original payload-gap cluster:
  - six preserve no `submit_output` traceback at all (`#83`, `#122`, `#177`, `#284`, `#382`, `#400`)
  - two end with the plain review-mode exit line `Application filled. Review-before-submit mode is active.` (`Toast #169`, `Postman #198`)
  - one points at a Greenhouse error page / possibly unavailable role (`Affirm #26`)
- The null-board `retries_exhausted` cluster is a scrape/bootstrap failure, not a submit regression:
  - all ten rows are LinkedIn-source URLs with no `output_dir` and no board metadata because JD scraping exhausted retries before the pipeline wrote its normal repo-local artifacts
- The remaining generic `retries_exhausted` rows outside Greenhouse are now mostly pre-submit generation/scrape issues:
  - Ashby still has one Playwright timeout and several generic `All submission attempts failed` rows
  - iCIMS `retries_exhausted` is down to four rows, mostly JD scrape/rate-limit failures rather than auth-state or submit-surface regressions
- iCIMS auth failures and unsupported boards are still correctly on the truthful/manual path:
  - active iCIMS auth rows preserve `icims_auth_failure.json`
  - active unsupported rows preserve `unsupported_board.json`
- Autodesk is no longer part of the live stopped Workday cluster:
  - the Workday progression fixes now get Autodesk through `My Information`, `My Experience`, `Application Questions`, and the main `Voluntary Disclosures` flow
  - the repo-local artifacts also show that one supposed draft rerun crossed the final Workday review boundary and produced a real confirmation email at `2026-03-28 04:15:03 UTC`, so Autodesk exposed a draft-mode safety incident even though it no longer counts as a live stopped-job row
  - a final rerun at `2026-03-28 04:20 UTC` exited as `already_applied`
  - resyncing job `#328` from disk at `2026-03-28 04:25 UTC` moved it to `submitted`, cleared its stale `retries_exhausted` failure metadata, and reduced the active stopped count from `134` to `133`
  - a current Gmail confirmation was matched at `2026-03-28 04:31 UTC` (`Confirmation of application received for 26WD96004 Senior Principal Product Manager, Advanced Solutions (Open)`), and the DB row now carries `email_confirmed = true`
  - the preventive follow-up landed at `2026-03-28 04:42 UTC`: Workday draft reruns now fail closed on explicit review shells / visible submit controls so generic navigation cannot click `Submit` in `--draft`
- The next slice chosen from the refreshed repo-local audit is a real rerun of one quota-hit stopped row through the worker path:
  - that is the narrowest way to turn the provider-capacity fix into visible queue movement
  - if that canary moves cleanly, LinkedIn failure-evidence persistence remains the next largest actionable code cluster
- The latest repo-local implementation slices were Greenhouse queue truthfulness plus LinkedIn/Workday follow-up cleanup:
  - `tests/test_job_db.py`, `tests/test_greenhouse_autofill.py`, `tests/test_question_classifier.py`, `tests/test_positive_fit_screening_policy.py`, and `tests/test_autofill_common.py` now cover the routing fix plus historical traceback backfill (`249` tests passed on `2026-03-28 23:42 UTC`)
  - the live queue now shows Greenhouse as `9` opaque `retries_exhausted`, `8` `greenhouse_runtime_error`, and `1` `greenhouse_unknown_questions` instead of a single generic `18`-row bucket
  - `tests/test_autofill_linkedin.py` and `tests/test_job_db.py` now cover LinkedIn payload fallback, Easy Apply modal reopen retry, and legacy Workday auth normalization (`108` tests passed on `2026-03-29 00:01 UTC`)
- Refresh at `2026-03-29 19:21 UTC` after the Workday account-verification pass:
  - The remaining Workday verification rows are no longer stuck behind a generic sign-in label:
    - `scripts/workday_auth.py` now classifies `Verify your account...` / `Resend Account Verification` surfaces as `account_verification_gate`
    - `scripts/autofill_workday.py` now follows that gate through the real recovery path by clicking `Resend Account Verification`, polling Gmail for the verification email, opening the verification link, and then retrying sign-in if Workday returns to the auth shell
    - the create-account recovery now handles both live variants: immediate verification-gate landings and the Calix-style flow where the verification requirement only appears after the first post-create sign-in attempt
  - Live repo-local proof shows the new branch is real:
    - Calix rerun `output/calix/senior-pm-intelligent-access/submit-20260329T191800Z/workday_submit_debug.png` now shows `An email has been sent to you.` with a visible `Resend Account Verification` control
    - the matching `output/calix/senior-pm-intelligent-access/submit-20260329T191800Z/workday_auth_failure.json` now records `auth_state = account_verification_gate` instead of the earlier stale `sign_in_gate`
    - the runtime reached `Workday: waiting for account verification email...`, proving the code cleared the Workday-side ambiguity and that the remaining blocker is the separate environment issue `gws invalid_grant`
  - The next stop reason is now explicit:
    - `gws auth login` was started and is still waiting on browser approval
    - until that OAuth refresh completes, the Workday password-reset and account-verification subsets will truthfully remain blocked on Gmail access rather than Workday selectors or auth-state classification
- Refresh at `2026-03-29 19:50 UTC` after the Gmail-auth rerun + sync pass:
  - The local Gmail outage is gone:
    - `gws auth login -s gmail` completed successfully
    - `gws auth status` now reports `token_valid = true`
    - a real Gmail read probe against `from:otp.workday.com` succeeds again
  - Full current-repo rerun log: `output/playwright/workday-auth-unknown-rerun-20260329T192713Z.jsonl`
  - The Workday cluster now reflects real tenant outcomes instead of the old Gmail token failure:
    - Calix `#386` moved out of auth entirely and now stops on `application_questions_validation`
      - saved result: `output/calix/senior-pm-intelligent-access/submit-20260329T191800Z/application_submission_result.json`
- Refresh on `2026-04-03` for unsupported ATS Wave 1 support:
  - The repeated `unknown / unsupported` family now has first-class board coverage in code for:
    - SuccessFactors / Jobs2Web
    - Breezy
    - Recruitee
    - Jobvite
    - JazzHR / ApplyToJob
    - Paycor Recruiting
  - Routing now treats custom-hosted jobs pages as evidence-driven, not hostname-only:
    - company-hosted jobs pages with `jobs2web`, `j2w.apply`, or SuccessFactors assets route to `successfactors`
    - company-hosted `/o/...` role pages with Recruitee assets route to `recruitee`
    - direct `*.breezy.hr/p/...`, `jobs.jobvite.com/.../job/...`, `applytojob.com/apply/...`, and `recruitingbypaycor.com/Recruiting/Jobs/...` URLs now route to explicit submitters instead of the unsupported bucket
  - The implementation split matches the real board families:
    - `scripts/autofill_successfactors.py` is auth-aware and stops truthfully on sign-in, create-account, or password-reset gates
    - Breezy, Recruitee, Jobvite, JazzHR, and Paycor now share the single-page payload/fill/classify contract through the allowed shared layers (`application_submit_common.py`, `autofill_common.py`, `autofill_pipeline.py`) while each board keeps explicit naming and submit-state rules
  - Regression coverage is green for the routing plus new board-script slice:
    - `uv run python -m pytest tests/test_submit_application.py tests/test_url_resolver.py tests/test_successfactors_autofill.py tests/test_breezy_autofill.py tests/test_recruitee_autofill.py tests/test_jobvite_autofill.py tests/test_jazzhr_autofill.py tests/test_paycor_autofill.py -v`
  - Live stopped-job reruns for these families are still pending; this refresh records the support rollout and detector truthfulness, not a completed rerun wave.
      - proof pages: `page_01_my_information.png` through `page_05_application_questions.png`
      - the concrete blocker is `If you are willing to travel, what percentage of time?`
    - LiveRamp `#200` now truthfully lands on `account_verification_gate` with `An email has been sent to you.`
    - Etsy `#293`, Relativity `#19`, Qualys `#240`, and Houlihan Lokey `#423` now land on `create_account_gate`
    - OutSystems `#383` now preserves a narrower `sign_in_gate` with `Apply for Future Opportunities page is loaded`
  - One more generalized sync bug was fixed at the same time:
    - `scripts/job_db.py` previously read stale `submit/application_submission_result.json` before newer active-submit reruns
    - that let an older Workday auth stop mask a fresher rerun result
    - the sync path now reads `application_submission_result.json` from `existing_submit_dirs(out)` so the active submit dir wins
    - regression coverage: `tests/test_job_db.py::test_sync_job_from_disk_prefers_active_workday_failed_result_over_stale_submit_auth_artifact`
  - After `uv run bin/job-assets sync`, the live Workday split is now:
    - `workday auth_failed = 8`
    - `workday auth_unknown = 6`
    - `workday application_questions_validation = 1`
    - `workday skipped_captcha = 1`
- Refresh at `2026-03-29 20:46 UTC` after repo-local sync plus the next Workday rerun slice:
  - The residual `greenhouse unknown` cluster was stale queue truth, not a Greenhouse board bug:
    - `uv run bin/job-assets sync` collapsed all `11` rows immediately from current repo-local proof
    - `Chainguard #7` now syncs as truthful `submitted` because the active role directory already held website-confirmation evidence
    - the other ten rows (`Databricks #10`, `Adyen #21`, `fal #63`, `Kalshi #77`, `Sage #102`, `Slide Insurance #133`, `Sixfold #140`, `Prove #141`, `Stripe #149`, `LaunchDarkly #153`) now sync back to `draft`
    - every one of those outputs already had active `submit-20260329T18*` proof with both `greenhouse_autofill_pre_submit.png` and `greenhouse_autofill_review.png`
  - Workday auth evidence is now preserved structurally in SQLite instead of being flattened away:
    - `jobs.db` now stores `auth_state` alongside `failure_type` and `auth_scope`
    - `scripts/job_db.py::sync_job_from_disk()` persists `auth_state` from current `application_submission_result.json` / `workday_auth_failure.json` and clears stale auth metadata when proof promotes a row back to `draft`
    - `submission_result_outcomes.handle_draft_mode_submission_result()` now preserves `auth_state` on the immediate runtime stop path too, so worker-time and sync-time truth match
  - The repo-local Workday split is now queryable without reopening artifact files:
    - `auth_failed / credential_rejected = 8`
    - `auth_unknown / create_account_gate = 4`
    - `auth_unknown / account_verification_gate = 1`
    - `auth_unknown / sign_in_gate = 1`
    - `skipped_captcha = 1`
  - Fresh rerun evidence with the new DB contract is already on disk:
    - log: `output/playwright/workday-stopped-rerun-20260329T203637Z.jsonl`
    - rerun canaries completed so far: Walmart `#14`, FactSet `#62`, General Motors `#208`, and Zillow `#314`
    - all four stayed truthfully `auth_failed / credential_rejected` and rewrote current-attempt `workday_auth_failure.json`, `application_submission_result.json`, and `workday_submit_debug.png`
- Refresh at `2026-03-29 21:30 UTC` after the follow-up reruns + final sync:
  - The Greenhouse `unknown` cluster is fully eliminated:
    - a final targeted resync reapplied the ready-proof override across the full investigated set
    - `Chainguard #7`, `Databricks #10`, `Adyen #21`, `fal #63`, `Kalshi #77`, `Sage #102`, `Slide Insurance #133`, `Sixfold #140`, `Prove #141`, `Stripe #149`, and `LaunchDarkly #153` now all sit truthfully at `draft`
    - current query result: no active `greenhouse / unknown` rows remain
  - Follow-up Workday rerun log: `output/playwright/workday-followup-rerun-20260329T211841Z.jsonl`
  - The LiveRamp rerun proved the auth-state preservation fix:
    - `#200` now stores `auth_unknown / account_verification_gate`
    - the fresh artifact and screenshot both show the real gate:
      - `output/liveramp/lead-pm-cloud-embedded-identity/submit-20260329T193004Z/workday_auth_failure.json`
      - `output/liveramp/lead-pm-cloud-embedded-identity/submit-20260329T193004Z/workday_submit_debug.png`
  - The OutSystems rerun proved the public-form detector fix:
    - `#383` now bypasses auth recovery, enters the live form, uploads the resume, and stops on a truthful `my_experience_validation`
    - proof:
      - `output/outsystems/outbound-pm-director-public-sector/submit-20260329T194038Z/application_submission_result.json`
      - `output/outsystems/outbound-pm-director-public-sector/submit-20260329T194038Z/workday_submit_debug.png`
  - One more sync correctness rule was required after that rerun:
    - when a current `application_submission_result.json` describes a non-auth stop, stale `workday_auth_failure.json` from an older submit dir must not rehydrate `auth_state` / `auth_scope`
    - `scripts/job_db.py::sync_job_from_disk()` now binds `workday_auth_failure.json` to the same submit dir as the current result when one exists, and clears stale auth metadata otherwise
    - ready draft proof can now override stale stopped artifacts even when the DB row was already `submitted`, preventing older `submit/application_submission_result.json` files from pulling a truthful draft back into `stopped / unknown`
  - Final repo-local Workday split is now:
    - `auth_failed / credential_rejected = 8`
    - `auth_unknown / create_account_gate = 4`
    - `auth_unknown / account_verification_gate = 1`
    - `auth_unknown / password_reset_gate = 1`
    - `my_experience_validation = 1`
  - The remaining Workday cluster is now concrete and board-truthful rather than generic:
    - no residual `auth_unknown / unknown`
- Refresh at `2026-04-03 21:01 UTC` after the cross-board stopped-job rerun wave:
  - The stopped queue had drifted far beyond the earlier March audit. The pre-rerun baseline for this pass was:
    - active non-archived stopped count: `950`
    - largest board buckets: Greenhouse `243`, Workday `160`, unknown `149`, Ashby `93`, LinkedIn `91`, Lever `60`
    - largest artifact-backed clusters:
      - `unknown / unsupported = 75`
      - `linkedin / not_easy_apply = 74`
      - `greenhouse / job_closed = 59`
      - `unknown / retries_exhausted = 56`
      - `greenhouse / retries_exhausted = 41`
      - `workday / auth_unknown = 37`
      - `workday / failed = 36`
  - Representative reruns proved that several historically scary clusters were stale queue truth, not live board bugs:
    - Uber `group-pm-grocery-retail-advertising` now truthfully stops on an auth gate instead of the old fake `First name` blocker:
      - `output/uber-eats-is-expanding-its-mission-to/group-pm-grocery-retail-advertising/submit/uber_autofill_pre_submit.png`
    - Qualcomm `director-pm` now truthfully stops on the Eightfold sign-in gate instead of the old fake `First Name` blocker:
      - `output/qualcomm/director-pm/submit/eightfold_autofill_pre_submit.png`
    - SoFi `principal-pm-ai-features` reruns cleanly to draft review instead of the old demographic blockers:
      - `output/sofi/principal-pm-ai-features/submit/greenhouse_autofill_pre_submit.png`
    - Databricks `sr-pm-compute-platform` also reruns cleanly to draft review instead of the old country blocker:
      - `output/databricks/sr-pm-compute-platform/submit/greenhouse_autofill_pre_submit.png`
  - Operational recovery completed in two stages:
    - Stage 1: queued `576` safe reruns from the active stopped set and left only truthful/manual families stopped
    - Stage 2: found that the apparent "old repo root" blocker was actually a symlinked path alias, not a missing artifact tree:
      - `/Users/jerrison/00-projects/11-job-application-material-creation -> /Users/jerrison/00-projects/00-career/01-prep/11-job-application-material-creation`
      - the real bug was stale string-path serialization plus ad-hoc filters that treated the alias as foreign
  - Generalized fix now in repo:
    - `scripts/job_db.py::_repo_local_output_candidate()` canonicalizes output dirs that resolve inside the current repo even when the stored string still uses a symlinked legacy path
    - `scripts/job_db.py::migrate_legacy_output_dirs()` can now repair genuinely external legacy output trees by copying or repointing them into the current repo before sync/rerun
    - regression coverage:
      - `tests/test_job_db.py::test_sync_job_from_disk_canonicalizes_symlinked_legacy_output_dir`
      - `tests/test_job_db.py::test_migrate_legacy_output_dirs_copies_stopped_legacy_tree_into_current_repo`
      - `tests/test_job_db.py::test_migrate_legacy_output_dirs_repoints_existing_repo_local_tree_without_copy`
  - Live DB repair results for the alias-stranded slice:
    - `migrate_legacy_output_dirs(statuses=('stopped',)) -> repointed_existing = 10`
    - explicitly restarted the formerly skipped jobs:
      - `#16`, `#73`, `#83`, `#99`, `#107`, `#123`, `#124`, `#169`, `#229`, `#248`
  - Queue truth at `2026-04-03 21:01 UTC` after the repair:
    - `queued = 476`
    - `generating = 6`
    - `autofilling = 2`
    - `draft = 280`
    - `submitted = 238`
    - `stopped = 349`
    - live queue screenshot:
      - `output/playwright/stopped-rerun-wave-20260403.png`
  - The remaining stopped set is now dominated by truthful/manual families rather than stale processing drift:
    - `unknown / unsupported = 76`
    - `linkedin / external_apply = 73`
    - `greenhouse / job_closed = 59`
    - `workday / auth_failed / credential_rejected = 27`
    - `workday / auth_guarded = 22`
    - `workday / auth_unknown / create_account_gate = 19`
    - `icims / auth_failed = 16`
    - `linkedin / job_closed = 13`
    - `workday / auth_unknown / sign_in_gate = 13`
    - `unknown / duplicate = 13`

## 2026-04-04 Follow-up: Visible Self-ID Unknowns Must Block Draft

- User-reported bug:
  - Homebase `#1594` reached `draft` even though the saved screenshot still showed unresolved deterministic self-ID fields and the accommodation answer had flipped to `Yes`.
- Root causes:
  - `scripts/pipeline_draft_proof.py` only blocked on saved artifact presence plus explicit blocker metadata, so older reports with `unknown_optional` self-ID entries still looked `ready`.
  - Retroactive blocker inference was dead code because it imported `parse_application_profile` and called it without loading `application_profile.md`, so the helper always fell back to `None`.
  - Several boards still matched profile-backed self-ID options too literally:
    - Ashby / Lever / Gem needed shared alias matching for `Male -> Man`, veteran/disability `No` variants, and similar profile-backed labels.
    - Greenhouse, SmartRecruiters, and Eightfold still had board-local gaps on those same aliases.
  - Ashby only inferred pronouns when the field rendered as `ValueSelect` / `MultiValueSelect`; Homebase exposed `Pronouns` as a plain `String` field, so the real rerun still failed closed on a visible deterministic self-ID miss after the broader alias fixes landed.
  - Mixed application-process accommodation prompts were still being classified as `reasonable_accommodation`, which defaulted to `Yes`, instead of the interview/application-process accommodation path that defaults to `No`.
- Generalized repo fixes:
  - `scripts/pipeline_draft_proof.py` now loads the real `application_profile.md` before retroactively inferring blockers from `unknown_questions`, so older reports are re-evaluated correctly across all boards.
  - `scripts/application_submit_common.py::question_is_interview_accommodation_request()` now recognizes broader application/interview-process accommodation prompts, not just literal `reasonable accommodation` text.
  - `scripts/autofill_ashby.py` now fills `Pronouns` when Ashby renders that deterministic self-ID prompt as a `String` / `LongText` field, while still skipping `If other, please let us know your pronouns` follow-up prompts.
  - Shared profile-option aliasing already added for Ashby / Lever / Gem is now mirrored in the remaining live select paths:
    - `scripts/autofill_greenhouse.py`
    - `scripts/autofill_smartrecruiters.py`
    - `scripts/autofill_eightfold.py`
  - SmartRecruiters and Eightfold demographic payload steps are now explicitly marked as visible self-ID steps so unresolved deterministic self-ID answers fail closed instead of silently remaining optional review noise.
- Verification:
  - targeted draft + accommodation + board regression slice:
    - `uv run python -m pytest tests/test_job_web.py::test_get_job_detail_blocks_visible_self_id_unknown_optional_question tests/test_job_web.py::test_queue_endpoint_downgrades_ready_drafts_with_optional_unknown_questions tests/test_submit_application.py::QuestionIsReasonableAccommodationTests::test_interview_accommodation_prompt_is_excluded tests/test_submit_application.py::QuestionIsReasonableAccommodationTests::test_mixed_application_process_prompt_is_excluded tests/test_submit_application.py::QuestionIsInterviewAccommodationTests::test_detects_interview_process_accommodation tests/test_submit_application.py::QuestionIsInterviewAccommodationTests::test_detects_mixed_application_process_accommodation_prompt tests/test_ashby_autofill.py::AshbyAutofillTests::test_infer_step_matches_gender_identity_alias_against_man_option tests/test_lever_autofill.py::LeverAutofillTests::test_infer_step_matches_gender_identity_alias_against_man_option tests/test_gem_autofill.py::GemAutofillTests::test_infer_step_matches_gender_identity_alias_against_man_option tests/test_greenhouse_autofill.py::GreenhouseAutofillTests::test_greenhouse_profile_option_label_matches_gender_identity_alias_against_man_option tests/test_greenhouse_autofill.py::GreenhouseAutofillTests::test_greenhouse_profile_option_label_matches_disability_no_alias tests/test_autofill_smartrecruiters.py::SmartRecruitersPayloadTests::test_build_payload_uses_shared_application_profile_url_fields tests/test_autofill_smartrecruiters.py::SmartRecruitersPayloadTests::test_resolve_smartrecruiters_select_label_matches_gender_identity_alias tests/test_eightfold_autofill.py::test_fill_eightfold_combobox_matches_company_website_aliases_for_referral_source tests/test_eightfold_autofill.py::test_fill_eightfold_combobox_matches_gender_identity_alias_against_man_option -v`
  - targeted Ashby pronouns rerun slice:
    - `uv run python -m pytest tests/test_ashby_autofill.py::AshbyAutofillTests::test_infer_step_fills_string_pronouns_from_application_profile tests/test_ashby_autofill.py::AshbyAutofillTests::test_infer_step_ignores_blank_generated_answer tests/test_job_web.py::test_get_job_detail_blocks_visible_self_id_unknown_optional_question tests/test_submit_application.py::QuestionIsInterviewAccommodationTests::test_detects_mixed_application_process_accommodation_prompt -v`
  - broader board regression slice:
    - `uv run python -m pytest tests/test_autofill_smartrecruiters.py tests/test_eightfold_autofill.py tests/test_greenhouse_autofill.py -k "match_option_label or smartrecruiters or eightfold" -v`
  - lint:
    - `uv run ruff check scripts/pipeline_draft_proof.py scripts/autofill_greenhouse.py scripts/autofill_smartrecruiters.py scripts/autofill_eightfold.py tests/test_greenhouse_autofill.py tests/test_autofill_smartrecruiters.py tests/test_eightfold_autofill.py`
    - `uv run ruff check scripts/autofill_ashby.py tests/test_ashby_autofill.py`
  - live rerun proof:
    - `job-assets submit output/homebase/product-lead-partnerships-distribution-hybrid --provider openai --headless --reapply`
    - active artifact: `output/homebase/product-lead-partnerships-distribution-hybrid/submit-20260404T144547Z/ashby_autofill_pre_submit.png`

## 2026-04-04 Follow-up: Rechecked Other Drafts For The Same Self-ID Issue

- Scope:
  - Re-audited every current `draft` row with `draft_review_state(...)`.
  - Initial post-fix snapshot for this pass was `126` drafts total:
    - `119` `ready`
    - `7` `blocked`
  - Ran a keyword scan over every `ready` draft's active `unknown_questions` and `planned_but_unconfirmed_fields` buckets for self-ID / accommodation labels (`pronoun`, `gender`, `transgender`, `disability`, `veteran`, `ethnicity`, `race`, `sexual orientation`, `age`, `accommodation`).
  - Result: `0` `ready` drafts were still carrying self-ID or accommodation-looking unknowns in their active proof.
- Root causes in the remaining same-issue draft rows:
  - Horizon3 AI `#666` and Homebase `#1586` were stale Ashby attempts from before the broader self-ID fixes landed. Fresh reruns with the existing code cleared both.
  - Ironclad `#1304` exposed one remaining shared inference bug:
    - `scripts/autofill_common.py::infer_unknown_question_blocker_metadata()` was still treating conditional follow-up prompts like `If other, please let us know your pronouns` as deterministic self-ID blockers.
    - The follow-up prompt is optional context, not the canonical deterministic pronouns field, so it should stay a review note instead of blocking draft readiness.
- Generalized repo fix:
  - `scripts/autofill_common.py::infer_unknown_question_blocker_metadata()` now ignores conditional follow-up prompts when inferring draft blockers, including labels like:
    - `If other ...`
    - `If selected other ...`
    - `If selected yes ...`
    - `If you prefer to self-describe ...`
  - This keeps the draft gate strict for the real deterministic self-ID field while avoiding false blockers on optional explanation textboxes across boards.
- Verification:
  - red/green test:
    - `uv run python -m pytest tests/test_autofill_common.py::UnknownQuestionDraftBlockerMetadataTests::test_ignores_if_other_pronouns_follow_up -v`
  - focused regression slice:
    - `uv run python -m pytest tests/test_autofill_common.py::UnknownQuestionDraftBlockerMetadataTests::test_marks_optional_visible_self_id_unknown_as_draft_blocker tests/test_autofill_common.py::UnknownQuestionDraftBlockerMetadataTests::test_ignores_if_other_pronouns_follow_up -v`
  - live reruns:
    - `job-assets submit output/horizon3-ai/senior-staff-pm-msp --provider openai --headless --reapply`
    - `job-assets submit output/homebase/staff-pm-platform-hybrid --provider openai --headless --reapply`
    - `job-assets submit output/ironclad/staff-pm-workflow-manager --provider openai --headless --reapply`
  - rerun proof artifacts:
    - `output/horizon3-ai/senior-staff-pm-msp/submit-20260404T145954Z/ashby_autofill_pre_submit.png`
    - `output/homebase/staff-pm-platform-hybrid/submit-20260404T145928Z/ashby_autofill_pre_submit.png`
    - `output/ironclad/staff-pm-workflow-manager/submit-20260404T150107Z/ashby_autofill_pre_submit.png`
- Current boundary after the recheck:
  - `126` drafts total:
    - `122` `ready`
    - `4` `blocked`
  - The remaining blocked drafts are unrelated to this self-ID issue:
    - Applied Materials `#58` (`icims`) missing current-attempt report + screenshot proof
    - Uber Eats `#155` (`uber`) required identity fields unresolved
    - career `#341` (`lever`) required resume/contact/location fields unresolved
    - Plaid `#494` (`linkedin`) missing current-attempt report + screenshot proof

## 2026-04-05 Follow-up: Rippling Path-Aware Fill Closed The Malwarebytes Selector Gap

- Live queue truth after this slice:
  - active status counts after the bulk repairable requeue:
    - `stopped = 561`
    - `queued = 424`
    - `draft = 145`
    - `submitted = 262`
    - `generating = 1`
  - the stopped audit now shows `0` repairable stopped rows; the remaining stopped rows are truthful/manual terminal families (`pending_user_input`, `external_apply`, `unsupported`, auth gates, validation blockers, captcha, and similar states)
- Root cause for the Rippling Malwarebytes stop:
  - payload generation already knew about the live additional questions, but `scripts/autofill_rippling.py` still delegated `_fill_step(...)` to the generic `fill_basic_step(...)` path
  - the live DOM did not expose those controls through the shared selectors:
    - schema `Location (city only)` rendered as a shorter visible `Location` field under `data-testid="location"`
    - custom text questions rendered `data-input="customQuestions....<uuid>"`
    - custom dropdown questions rendered `data-testid*="<uuid>"` containers with nested `role="combobox"`
  - because the runtime also dropped the live schema `path` on many steps, deterministic answers existed but the actual DOM lookup still left the fields `skipped_not_found`
- Generalized repo fix:
  - Rippling steps now retain live schema `path` and `field_type`
  - the Rippling runtime now prefers path-aware locators for custom text/select controls and the location autocomplete path before falling back to shared label selectors
  - location autocomplete now selects the live option instead of stopping at raw text entry
  - the board runtime confirms the rendered value before marking the step filled
- Real pre/post evidence:
  - pre-fix temp reproduction forcing the old shared-only fill path:
    - screenshot: `output/playwright/nad-31-malwarebytes-pre.png`
    - report: `output/playwright/nad-31-malwarebytes-pre-report.json`
    - unresolved required fields in `planned_but_unconfirmed_fields`:
      - `Location (city only) = San Francisco`
      - salary expectations = open/flexible compensation text
      - U.S.-person question = `Yes`
      - export-control follow-up = `N/A`
  - canonical post-fix rerun of `output/malwarebytes/sr-technical-pm-core-tech`:
    - screenshot: `output/malwarebytes/sr-technical-pm-core-tech/submit/rippling_autofill_pre_submit.png`
    - report: `output/malwarebytes/sr-technical-pm-core-tech/submit/rippling_autofill_report.json`
    - all four fields above now record `status = filled`
  - `uv run bin/job-assets sync` now promotes Malwarebytes `#1439` from `stopped` / `pending_user_input` back to `draft`
- Linear proof:
  - created and closed `NAD-31` with self-contained description, proof comment, and attached pre/post screenshots visible in Linear
- Separate truth surfaced during the same pass:
  - Rippling `#99` (`output/rippling/product-lead-automation-platform`) now returns HTTP `404` for both the listing and `/apply` paths via `curl -I`
  - that is a separate stale-job / unavailable classification issue and should not be mixed into `NAD-31`
- Queue action after the fix:
  - used `pipeline_audit_loop.audit_stopped_outcome(...)` plus `pipeline_orchestrator.requeue_jobs_for_repair_redraft(...)` to requeue all `418` currently repairable stopped rows with initiator `codex-backlog-sweep`
  - the background worker was already running, so the repo is now actively redrafting those previously stale repairable rows through the fixed code paths

## Why This Works

Keeping the audit in the repo makes the project self-contained for future agents, planning passes, and code changes. Pairing that audit with saved artifacts keeps it honest. The repo can now answer:

- what the dominant live clusters are
- which conclusions came from current saved evidence
- which earlier hypotheses were invalidated by newer artifacts
- what belongs in code, and what remains a manual path

That is exactly the context planning and implementation need. The Obsidian file can still exist, but only as a convenience mirror.

## Prevention

- Treat external trackers as mirrors, not as the source of truth for engineering state.
- When a stopped-job investigation changes prioritization or invalidates an earlier hypothesis, update the repo doc in the same session.
- Always include timestamps in UTC for audit snapshots so future readers can compare them to DB rows and artifacts without timezone ambiguity.
- Prefer conclusions backed by `jobs.db`, saved submit artifacts, and current output files over convenience notes.
- Before any fresh draft rerun, delete stale current-attempt report/debug/result artifacts from the active `submit/` directory so a new attempt cannot inherit old failure proof.
- Disk sync should trust current draft proof and truthful non-Easy-Apply artifacts over stale DB failure metadata; clear stale stop state when the repo-local artifacts show a newer truth.
- If a current non-auth submission result exists, never let an older auth artifact in another submit dir rehydrate stale `auth_state` or `auth_scope`.
- When provider wrappers can emit quota or capacity banners while still exiting `0`, treat the saved log plus missing outputs as the real fallback signal instead of trusting the exit code alone.
- If a dominant failure cluster cannot be diagnosed from saved artifacts alone, treat that as a product/engineering requirement, not just a debugging inconvenience.

## Cross-References

- Active brainstorm: `docs/brainstorms/2026-03-25-pipeline-resilience-requirements.md`
- Related learning: `docs/solutions/workflow-issues/explicit-answer-regeneration-requires-durable-fresh-proof-2026-03-26.md`
- Related learning: `docs/solutions/workflow-issues/workday-draft-reruns-must-fail-closed-before-submit-2026-03-28.md`
- Related learning: `docs/solutions/logic-errors/submit-attempt-scoped-confirmation-email-replies.md`
- Related learning: `docs/solutions/integration-issues/adding-new-llm-provider.md`
- Related learning: `docs/solutions/integration-issues/provider-capacity-fallback-must-inspect-logs-not-just-exit-codes-2026-03-28.md`
