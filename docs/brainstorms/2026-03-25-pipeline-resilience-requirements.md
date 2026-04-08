---
date: 2026-03-25
topic: pipeline-resilience
---

# Pipeline Resilience: Reduce Stopped Jobs

## Problem Frame

54% of jobs (219/403) end in `stopped` status. Excluding intentional stops (user_stopped, duplicate, unsupported = 29 jobs), 190 jobs stopped due to preventable failures. The two dominant root causes are LLM failures (35%) and rate limiting (29%), both of which have existing infrastructure that is either unconfigured or insufficient.

On 2026-03-27, the user resumed this topic with an operational audit request: inspect all currently stopped jobs, troubleshoot root causes, generalize fixes across boards and surfaces, and maintain tracker-ready notes covering to-dos, resolution steps, fix approach, comprehensive testing, and evidence. A live `jobs.db` audit at `2026-03-27 23:40 UTC` found 134 active stopped jobs. The largest active clusters were LinkedIn `retries_exhausted` (30), Greenhouse `retries_exhausted` (22), iCIMS `auth_failed` (15), unknown/unsupported (14), Workday `retries_exhausted` (11), Ashby `retries_exhausted` (10), null-board `retries_exhausted` (10), and Workday `auth_unknown` (6).

That audit changed the framing in two ways:

1. Provider capacity is still a live root cause. Twelve active stopped jobs still contain the explicit OpenAI quota text `You're out of extra usage · resets Mar 28 at 11pm (America/Los_Angeles)` in saved raw LLM artifacts.
2. The biggest diagnosis blocker is now failure-evidence quality. Dominant submit clusters often collapse into generic `retries_exhausted` / `submit_timeout` without preserving the board-local failure reason, screenshot, or debug artifact needed to generalize a fix.

The product decision for this brainstorm is still to optimize first for reducing future stopped jobs system-wide. The audit of currently stopped jobs remains supporting evidence and prioritization input, but it now justifies a targeted failure-evidence requirement plus a narrow set of dominant recurring board fixes because those clusters materially drive the stopped queue.

Canonical repo-local audit notes for this pass now live in `docs/solutions/workflow-issues/stopped-job-audits-must-be-repo-local-and-artifact-backed-2026-03-27.md`. Any external tracker is a convenience mirror, not the source of truth.

A refresh at `2026-03-28 16:29 UTC` still finds `133` active stopped jobs, but the priority read has shifted slightly. Workday `retries_exhausted` dropped from `11` to `10` after Autodesk reconciled out of the stopped queue, Ramp `pm-new-bets` is back to a fresh draft artifact set with merged linked-resource proof, and the `12` quota-hit rows now point at rerun follow-through rather than missing fallback logic because explicit provider-capacity detection has landed in `scripts/llm_common.sh` with regression coverage. LinkedIn `retries_exhausted` (`30`) remains the largest actionable board cluster, but the immediate post-fix validation slice is now a real worker-path rerun of at least one quota-hit stopped row to confirm the queue actually moves.

A second refresh at `2026-03-28 23:39 UTC` drops the active stopped count to `120` and changes the cluster shape again. Direct disk reconciliation plus traceback-backed sync removed stale generic stop states, so LinkedIn now splits into `linkedin_modal_missing` (`16`) and `linkedin_validation_loop` (`8`), Workday Turo `#143` now reads `application_questions_validation`, and Greenhouse now splits into `greenhouse_runtime_error` (`8`), `greenhouse_unknown_questions` (`1`), and a smaller residual `retries_exhausted` tail (`9`). The remaining largest active clusters are now iCIMS `auth_failed` (`16`), LinkedIn `linkedin_modal_missing` (`16`), Workday `auth_unknown` (`16`), unknown/unsupported (`15`), and null-board scrape/bootstrap `retries_exhausted` (`10`).

A third refresh at `2026-03-29 00:01 UTC` nudges the active stopped count back to `122` because the live queue moved after the earlier snapshot, not because the Greenhouse reclassification regressed. The dominant clusters are unchanged, but Workday's stale singleton `auth_failed` row is now correctly `service_unavailable`, and LinkedIn picked up two concrete runtime fixes: payloads now fall back to the canonical repo source files when role-local profile copies are missing, and the Easy Apply wizard now retries reopening the modal before classifying `linkedin_modal_missing`.

A fourth refresh at `2026-03-29 06:15 UTC` raises the active stopped count to `195` because the live queue moved again and the full repo-local sync repopulated newer rows, not because the earlier reclassification work regressed. The largest remaining clusters are now LinkedIn `linkedin_modal_missing` (`84`), unknown/unsupported (`19`), Workday `auth_unknown` (`16`), unknown `retries_exhausted` (`10`), LinkedIn `linkedin_validation_loop` (`9`), Greenhouse `retries_exhausted` (`8`), Greenhouse `incomplete` (`6`), and iCIMS `retries_exhausted` (`6`). The new rerun slice also proved that several stale LinkedIn stop states were not modal bugs at all once reruns cleared old submit artifacts and allowed `external_apply`, `no_apply_button`, and ready-draft proof to sync truthfully.

## Requirements

### R1. Enable LLM provider fallback chain

Configure `ASSET_LLM_PROVIDER_CHAIN=openai,gemini,claude` so that when the primary provider (OpenAI) fails, generation automatically falls through to Gemini, then Claude. Treat explicit provider-capacity / quota exhaustion messages as provider failures for this purpose instead of letting them burn outer retries. All three are subscription-based. This is primarily configuration plus end-to-end verification because the fallback infrastructure in `llm_common.sh` (`job_assets_run_prompt_with_fallback`) already exists.

**Acceptance:** A job whose OpenAI call fails, including explicit quota/usage exhaustion text, should automatically retry with Gemini, then Claude, before marking as `generation_failed`.

### R2. Add pipeline-level retry backoff

The auto-retry mechanism (`_auto_retry_if_transient`) requeues jobs immediately after transient failures. When a job is requeued after a rate limit, it gets picked up and hits the same rate limit. Add exponential backoff at the pipeline level: jobs requeued after transient failures should have a `retry_after` timestamp that the worker respects.

**Acceptance:** A rate-limited job retried 3 times should wait approximately 2min, 8min, 30min between retries (exponential with jitter), not be reprocessed immediately.

### R3. Fix non-transient errors misclassified as transient

Duplicate detection and auth failures currently match transient error patterns and get retried 3 times uselessly. These should be classified as non-transient and stop immediately on first detection.

**Acceptance:** A duplicate or auth-failed job stops on the first occurrence, not after 3 wasted retries.

### R4. Add JSON parse retry with stricter prompt

When LLM output fails JSON parsing even after `json_lenient.py` repair, retry the LLM call once with an explicit "return valid JSON only" system prompt addendum rather than immediately failing the job.

**Acceptance:** JSON parse errors trigger one retry with a stricter prompt before failing.

### R5. Escalate timeout on retry

When a job fails due to timeout, subsequent retries should use a progressively longer timeout (e.g., 1.5x on each retry) rather than the same timeout value.

**Acceptance:** A job that times out at 1200s retries at 1800s, then 2700s.

### R6. Add board-level cooldown after rate limit detection

When a board rate-limits one job, pause all pending jobs for that board for a cooldown period rather than letting them all hit the same rate limit.

**Acceptance:** After detecting a rate limit on Greenhouse, all queued Greenhouse jobs are delayed by the cooldown period (e.g., 5 minutes), not just the failed job.

### R7. Persist durable board-local failure evidence before retries collapse into generic stop states

When a submit attempt fails on a dominant recurring board path, the runtime must save enough structured evidence on disk for the next audit or fix pass to identify the real failure class without reopening worker logs or rerunning blindly. The audit showed this is currently weakest on LinkedIn and partially weak on Workday and Greenhouse.

**Acceptance:**
- A failed LinkedIn run writes a structured failure artifact that records the last observed step, a concrete failure class such as `modal_missing`, `validation_loop`, or `timeout_after_partial_fill`, and a screenshot path that actually exists on disk.
- After LinkedIn writes a concrete failure class such as `modal_missing` or `validation_loop`, the queue performs one targeted retry for that classified failure path, then stops with the saved evidence if the retry still fails.
- A failed Workday run writes a structured failure artifact that distinguishes `my_information_validation`, `create_account_gate`, `auth_unknown`, and `submit_validation`, and includes the captured screenshot/debug path plus any visible validation text.
- A failed Greenhouse run writes either `unknown_questions.json`, `submit_debug.{html,png}`, or an explicit proof-gap artifact when the run stops after payload generation but before fresh proof is recorded.
- Review surfaces and `answer_refresh_status.json` stop collapsing these cases into generic `retries_exhausted` / `submit_timeout` messages without a board-local artifact reference.

## Success Criteria

- Stopped job rate for new batches drops below 25% (from current 54%)
- Zero wasted retries on duplicate/auth failures (R3)
- LLM generation failures drop by at least 50% due to fallback chain (R1)
- Rate-limit stops drop by at least 50% due to backoff + cooldown (R2, R6)
- New LinkedIn stopped runs no longer end with `linkedin_autofill_report.json` containing `fields = []` plus a missing referenced pre-submit screenshot (R7)
- Dominant recurring submit failures can be diagnosed from saved artifacts alone without reopening `jobs.db.worker.log` (R7)

## Scope Boundaries

- **In scope:** Pipeline retry logic, provider chain config, worker scheduling, error classification, and the smallest set of dominant recurring board-specific failure contracts needed to materially reduce stopped jobs across all surfaces
- **In scope right now:** LinkedIn failure-artifact persistence and modal/validation classification, Workday `My Information` validation plus `Create Account` gate recovery/evidence, and Greenhouse proof-gap / debug-artifact persistence for current recurring stop clusters
- **In scope as supporting evidence, not primary scope:** Inspecting current stopped jobs to validate root-cause distribution, prioritize generalized fixes, and collect tracker-ready evidence
- **Out of scope:** One-off manual recovery for individual jobs, new board support, UI/UX changes to stopped job recovery, and speculative prompt-quality work beyond the strict JSON / provider-capacity fixes already captured above
- **Out of scope for now:** Explicit iCIMS credential failures and unsupported-board rows remain truthful/manual operational paths unless credentials or board support change
- **Out of scope:** `user_stopped` and resolved duplicate handling — these are working as designed

## Key Decisions

- **Provider chain order**: openai → gemini → claude (user preference: subscription-based fallbacks)
- **Backoff strategy**: Exponential with jitter at pipeline level, not just provider level
- **Board cooldown**: Board-wide pause rather than per-job delay, to prevent stampede
- **Primary optimization target**: Reduce future stopped jobs system-wide first. Use current stopped-job investigation to confirm priorities and document evidence, but do not let one-off recovery work drive the architecture.
- **Failure evidence first**: Before adding more retries, persist board-local failure reason plus artifact references so dominant stop clusters can be debugged from disk without replaying worker logs.
- **Dominant board exceptions are now in scope**: Even though this started as a cross-cutting resilience topic, recurring LinkedIn, Workday, and Greenhouse stop clusters now count as in-scope because they materially dominate the live queue and the fixes generalize across CLI/worker/web/TUI.
- **Immediate post-fix validation slice**: Re-run one representative quota-hit stopped row through the real worker path now that explicit quota/usage banner text forces provider fallback in `llm_common.sh`. This tells us whether the remaining `12`-row quota cluster is code-complete or still blocked elsewhere in queue handling.
- **Next board-local implementation slice**: After validating the quota-hit rerun path, resume LinkedIn failure-evidence persistence and classification because LinkedIn remains the largest actionable stopped cluster and still has the weakest durable artifacts.
- **LinkedIn retry policy**: Once LinkedIn emits a concrete failure class, allow one targeted retry for that classified path, then stop with durable evidence instead of continuing the broader generic retry loop.
- **Mercury/Stripe sampled Greenhouse payload gaps are no longer the current lead hypothesis**: The current payload artifacts for jobs `#183` and `#313` already include `question_15662463004` and `question_63058607`, so the remaining Greenhouse work shifts from those specific missing IDs toward proof-gap and failure-artifact persistence.
- **Keep operational/manual paths honest**: Active iCIMS `auth_failed` rows and unsupported-board rows already persist manual next-step artifacts and should stay on that truthful/manual path unless support or credentials change.

## Investigation Log

- `2026-03-27 23:39 UTC` Queried `jobs.db`: 134 active stopped jobs. Largest clusters were LinkedIn `retries_exhausted` (30), Greenhouse `retries_exhausted` (22), iCIMS `auth_failed` (15), unknown/unsupported (14), Workday `retries_exhausted` (11), and Workday `auth_unknown` (6).
- `2026-03-27 23:40 UTC` Audited active stopped artifacts. LinkedIn had 30/30 `linkedin_autofill_report.json` files with `fields = []`, and 28/30 referenced missing pre-submit screenshots. Workday had debug screenshots on all 17 active stopped rows, but 9 `retries_exhausted` rows had neither `application_submission_result.json` nor `workday_auth_failure.json`. Greenhouse had 16 active stopped rows with pre-submit screenshots, but only one `greenhouse_unknown_questions.json` and one `greenhouse_submit_debug.{html,png}` pair.
- `2026-03-27 23:40 UTC` Verified provider-capacity evidence. Twelve active stopped jobs still contain `You're out of extra usage · resets Mar 28 at 11pm (America/Los_Angeles)` in raw LLM output files.
- `2026-03-27 23:41 UTC` Reviewed representative Workday failures. Autodesk `#38` shows a `My Information` validation error on required `How Did You Hear About Us?`, and the current payload/report do not record that field as filled. Levi's `#375` and Calix `#386` both stop on visible `Create Account` pages with empty email/password fields.
- `2026-03-27 23:41 UTC` Reviewed representative Greenhouse failures. Mercury `#183` and Stripe `#313` payloads and reports already include `question_15662463004` and `question_63058607`, so those sampled IDs are present in current payloads even though the jobs remain stopped due to answer-refresh proof exhaustion.
- `2026-03-27 23:43 UTC` Re-verified manual-path clusters. Active iCIMS auth failures persist `icims_auth_failure.json` with truthful manual next steps, and unsupported rows persist `unsupported_board.json` with manual apply guidance.
- `2026-03-28 04:16 UTC` Re-ran the Autodesk Workday slice through the current branch fixes. The flow progressed through `My Information`, `My Experience`, `Application Questions`, `Voluntary Disclosures`, and `Self Identify`, then Workday switched the public job page to `You applied for this job` / `View Application`.
- `2026-03-28 04:20 UTC` Added Workday `already_applied` rerun detection for Autodesk-style `View Application` job pages so future reruns stop truthfully instead of collapsing into `auth_unknown`.
- `2026-03-28 04:25 UTC` Resynced Autodesk job `#328` from disk. `application_submission_result.json` now records `status = already_applied`, `website_confirmed = true`; the DB row moved `stopped -> submitted`, stale `retries_exhausted` metadata was cleared, and the active stopped-job count dropped from `134` to `133`.
- `2026-03-28 04:31 UTC` Matched the current Autodesk Gmail confirmation (`Confirmation of application received for 26WD96004 Senior Principal Product Manager, Advanced Solutions (Open)`) with a recent UTC floor, wrote the submit-attempt email artifact, and resynced job `#328` so `email_confirmed = true` without reusing the stale 2020 Autodesk thread.
- `2026-03-28 04:42 UTC` Documented the Autodesk rerun as a Workday draft-mode safety incident and added a fail-closed review-shell guard: explicit review roots or visible submit controls now force `PAGE_REVIEW`, and the generic Workday next-button helper no longer treats `Submit` as forward navigation in `--draft`.
- `2026-03-28 16:20 UTC` Re-ran Ramp `pm-new-bets` through `job-assets submit` after marking a fresh `reanswer` request. The refreshed draft artifacts now include `submit/application_answers.json`, `submit/linked_resource_context.json`, and `submit/linked_resource_evidence/` written from the merged db-fiddle adapter path.
- `2026-03-28 16:24 UTC` Finalized `output/ramp/pm-new-bets/answer_refresh_status.json` as `status = fresh`, `answer_provider = openai`, `generated_answer_count = 5`, with proof rooted in `submit/`.
- `2026-03-28 16:26 UTC` Added regression coverage in `tests/test_llm_common.py` and updated `scripts/llm_common.sh` so both chain mode and legacy fallback classify explicit quota/usage banner text as provider failure even when the provider exits `0` without writing outputs. A direct shell reproduction now advances `openai -> gemini` and writes fallback output from `gemini`.
- `2026-03-28 16:29 UTC` Refreshed the stopped-job audit from live repo state. Active stopped count remains `133`; top clusters are LinkedIn `retries_exhausted` (`30`), Greenhouse `retries_exhausted` (`22`), iCIMS `auth_failed` (`15`), unknown/unsupported (`14`), and Ashby/null-board/Workday `retries_exhausted` tied at `10`. The same `12` rows still contain explicit quota text in raw LLM artifacts, so the remaining follow-through is live rerun or queue movement rather than missing capacity-detection logic.
- `2026-03-28 23:39 UTC` Reconciled the live queue from current disk artifacts and traceback-backed sync. Active non-archived stopped count is now `120`; top clusters are iCIMS `auth_failed` (`16`), LinkedIn `linkedin_modal_missing` (`16`), Workday `auth_unknown` (`16`), unknown/unsupported (`15`), null-board `retries_exhausted` (`10`), Greenhouse `retries_exhausted` (`9`), Greenhouse `greenhouse_runtime_error` (`8`), and LinkedIn `linkedin_validation_loop` (`8`).
- `2026-03-28 23:41 UTC` Landed the Greenhouse routing fix in `scripts/autofill_greenhouse.py`: deterministic handling now covers non-compete, conflict-of-interest, city-location, education, and broader work-authorization variants, and payload-build missing-required-field failures now write a durable submission result instead of collapsing into `retries_exhausted`.
- `2026-03-28 23:42 UTC` Added traceback-based backfill in `scripts/job_db.py` so historical Greenhouse rows that died before writing `application_submission_result.json` can still sync to `greenhouse_runtime_error` / `greenhouse_unknown_questions`. Focused regression suites now pass: `tests/test_job_db.py`, `tests/test_greenhouse_autofill.py`, `tests/test_question_classifier.py`, `tests/test_positive_fit_screening_policy.py`, and `tests/test_autofill_common.py` (`249` tests total).
- `2026-03-29 00:01 UTC` Sampled the still-live LinkedIn stop clusters and found that representative stopped payloads had empty candidate profile/contact fields whenever the role output lacked local copies of `master_resume.md` and `application_profile.md`. Landed a repo-root fallback in `scripts/autofill_linkedin.py` plus an Easy Apply reopen retry before classifying `linkedin_modal_missing`.
- `2026-03-29 00:01 UTC` Normalized legacy Workday auth artifacts from saved evidence in `scripts/job_db.py`, allowing maintenance-page rows to sync as `service_unavailable` and replacing stale generic retry wrappers with the artifact-backed message. Snap `#107` is now the expected `service_unavailable` singleton instead of the earlier stale `auth_failed` row.
- `2026-03-29 00:01 UTC` Focused follow-up regression coverage passed for the new slice: `tests/test_autofill_linkedin.py` and `tests/test_job_db.py` (`108` tests).
- `2026-03-29 06:07 UTC` Re-ran Alt `#187` with the shared current-attempt cleanup path. The fresh LinkedIn report now shows `Does the listed salary meet your compensation requirements? = Yes` and the mixed immigration/sponsorship question = `No`; `draft_status.json` records `draft_review_state.state = ready`, and disk sync clears the stale failure metadata so the row returns to `draft`.
- `2026-03-29 06:10 UTC` Re-ran Gusto `#162` and Skylo `#178`. Both former `linkedin_modal_missing` rows now write `status = not_easy_apply`, splitting truthfully into `external_apply` and `no_apply_button` with current job-page screenshots.
- `2026-03-29 06:12 UTC` Verified ready-proof reconciliation on Hopper `#189`, Replicant `#272`, and ClickUp `#388`; sync now promotes proof-ready stopped rows to `draft` even when no fresh result artifact exists.
- `2026-03-29 06:15 UTC` Ran `uv run bin/job-assets sync`; the repo entrypoint passed and reported `Synced 421 of 476 jobs from disk artifacts`. Active non-archived stopped count is now `195`; the largest clusters are LinkedIn `linkedin_modal_missing` (`84`), unknown/unsupported (`19`), Workday `auth_unknown` (`16`), unknown `retries_exhausted` (`10`), LinkedIn `linkedin_validation_loop` (`9`), Greenhouse `retries_exhausted` (`8`), Greenhouse `incomplete` (`6`), and iCIMS `retries_exhausted` (`6`).
- `2026-03-29 14:22 UTC` Re-queued all `195` active non-archived stopped rows through the tracked CLI path (`job-assets retry`) so the queue-wide redraft is now recorded in `jobs.db` event history instead of happening as untracked ad hoc reruns.
- `2026-03-29 14:26 UTC` Re-ran the stale-company LinkedIn canaries directly: Charles Schwab job `#218` (`output/build-enablement-and-onboarding-plans-to/sr-genai-pm-aix-risk-management-compliance`) and Conviva job `#233` (`output/this/principal-product-builder-agent-analytics-optimization`) both now truthfully stop as `external_apply` and persist `submit/linkedin_external_apply_page.png` in `application_submission_result.json`.
- `2026-03-29 14:28 UTC` Landed the next LinkedIn generalization pass: pipe-delimited LinkedIn titles are now parsed as real employers in both `scripts/run_pipeline.py` and `scripts/scrape_job.py`, non-Easy-Apply exits persist current screenshot artifacts, Greenhouse review refresh now writes both pre-submit and review screenshots, and LinkedIn capture now prefers the modal / inner job-detail surface instead of the full workspace scaffold. Focused regressions passed for `tests/test_autofill_linkedin.py`, `tests/test_company_detection.py`, and `tests/test_greenhouse_autofill.py`.
- `2026-03-29 14:31 UTC` Re-ran the entire live Greenhouse `incomplete` cluster directly: Base `#232`, Fireblocks `#450`, Homeward `#454`, Klaviyo `#460`, Instacart `#470`, and Instacart `#475` now all preserve both `submit/greenhouse_autofill_pre_submit.png` and `submit/greenhouse_autofill_review.png`.
- `2026-03-29 14:32 UTC` Ran `uv run bin/job-assets sync` again after the targeted reruns. The sync reported `Synced 419 of 476 jobs from disk artifacts`; Base `#232`, Fireblocks `#450`, Homeward `#454`, Klaviyo `#460`, Instacart `#470`, and Instacart `#475` all moved from stopped `incomplete` to `draft`. Live status snapshot: `submitted 171`, `stopped 128`, `draft 71`, `queued 58`. The largest remaining stopped clusters are LinkedIn `linkedin_modal_missing` (`81`), Workday `auth_unknown` (`14`), LinkedIn `linkedin_validation_loop` (`9`), and LinkedIn `external_apply` (`5`).
- `2026-03-29 14:45 UTC` Root-caused the unreadable LinkedIn screenshot path on the Charles Schwab canary. LinkedIn was attempting a clipped capture of `main#workspace > div > div`, Playwright raised `Clipped area is either empty or outside the resulting image`, and `_capture_not_easy_apply_screenshot()` swallowed that exception, leaving the old `2258x9020` artifact in place. `scripts/autofill_linkedin.py` now falls back to a fresh viewport screenshot when the structural clip is invalid, and a live rerun rewrote `output/build-enablement-and-onboarding-plans-to/sr-genai-pm-aix-risk-management-compliance/submit/linkedin_external_apply_page.png` to a readable `2720x1800` image with `application_submission_result.json.artifacts.page_screenshot` restored.
- `2026-03-29 14:46 UTC` Verified why LinkedIn screenshots still feel smaller than other boards even when they are fresh: the shared LinkedIn browser profile reports `window.innerWidth = 4080`, `window.outerWidth = 1920`, and `window.devicePixelRatio ~= 0.67`. That zoomed-out profile state is now documented as a separate follow-up from the stale-artifact fix; the current runtime fix guarantees fresh readable evidence instead of the earlier blank/tiny stale rail capture.
- `2026-03-29 15:01 UTC` Closed the LinkedIn zoom follow-up in code. `scripts/browser_runtime.py` now scrubs stored Chromium zoom overrides before LinkedIn launches, and the shared `.playwright-linkedin/Default/Preferences` file no longer carries the old `partition.per_host_zoom_levels.www.linkedin.com.zoom_level = -6.025685102665476` entry. The same helper now runs from `scripts/autofill_linkedin.py`, `scripts/url_resolver.py`, and `scripts/import_linkedin_saved.py`, so autofill, saved-job import, URL resolution, and LinkedIn mark-applied/dismiss flows all start from `100%` zoom.
- `2026-03-29 15:01 UTC` Captured live proof after the zoom normalization pass. A real persistent-profile browser check on `https://www.linkedin.com/jobs/view/4356655127/` reported `window.innerWidth = 1360`, `window.innerHeight = 900`, `window.devicePixelRatio = 2`, and `visualViewport.scale = 1`, with evidence saved to `output/playwright/linkedin-zoom-reset-proof.png`. A fresh Charles Schwab rerun then rewrote `output/build-enablement-and-onboarding-plans-to/sr-genai-pm-aix-risk-management-compliance/submit/linkedin_external_apply_page.png` again, this time as a readable `2256x1648` cropped job-surface screenshot under the normalized profile.
- `2026-03-29 16:19 UTC` Closed the last known Workday auth classification gap. Walmart `#14` now reruns to `status = auth_failed`, `auth_state = credential_rejected`, and `credential_rejection_observed = true` with the explicit alert `You may have entered the wrong email address or password or your account might be locked.` The fix was two-part: `scripts/autofill_workday.py` now recognizes the real Workday wording variants (`wrong email address or password`, `account might be locked`) and `_build_workday_auth_result()` re-normalizes auth state from the saved page markers instead of trusting a stale `auth_state` field.
- `2026-03-29 16:27 UTC` Root-caused the live LinkedIn `linkedin_modal_missing` cluster with a same-profile Playwright probe on Anthropic `#521`. The current job page exposed only `Apply` plus `Responses managed off LinkedIn`, while the page also contained three offscreen recommended-job cards whose whole-card text included `Easy Apply`. The old whole-page fallbacks in `scripts/autofill_linkedin.py` were latching onto those recommendation cards instead of the current job's top-card control.
- `2026-03-29 16:36 UTC` Landed the LinkedIn apply-control scoping fix. `scripts/autofill_linkedin.py` now only accepts generic `Apply` / `Easy Apply` fallbacks when the control's own label is exactly `Apply` or `Easy Apply`, which preserves real buttons like Quizlet's top-card `a[aria-label*="Easy Apply to this job"]` while rejecting recommendation-card false positives. Live probe evidence is saved in `output/playwright/anthropic-step1-probe.png` and `output/playwright/anthropic-step1-probe-after-click.png`.
- `2026-03-29 16:37 UTC` Re-ran the full current-repo LinkedIn `linkedin_modal_missing` backlog through the tracked CLI path. `output/playwright/linkedin-modal-missing-rerun-20260329.jsonl` shows `79` reruns completed, all now truthfully classifying as `not_easy_apply`: `75` `external_apply` and `4` `no_apply_button`. After `uv run bin/job-assets sync`, the live queue snapshot moved to `submitted 186`, `stopped 176`, `draft 78`, `queued 57`, `generating 4`; the current-repo `linkedin_modal_missing` cluster is now `0`.
- `2026-03-29 16:49 UTC` Landed the next LinkedIn control-type generalization: required checkbox groups inside visible fieldsets now go through the same answer-selection path as radios. That change fixed the ValoreMVP-style yes/no checkbox question and the Headspace-style demographic checkbox section. A full rerun of the `linkedin_validation_loop` cluster is logged in `output/playwright/linkedin-validation-loop-rerun-20260329.jsonl`; after sync the cluster dropped from `7` to `1`, with SQUIRE `#221`, Snaplii `#315`, We/Headspace `#312`, and We/director-of-product `#478` syncing to `draft`, EarnIn `#273` and ValoreMVP `#304` reclassifying to truthful `linkedin_unknown_questions`, and one lone residual numeric-validation loop still remaining on Raydar / Our client `#459`.
- `2026-03-29 16:50 UTC` Current live stopped-cluster shape after the LinkedIn sweep: `external_apply 89`, `greenhouse job_closed 22`, `workday auth_unknown 16`, `linkedin no_apply_button 8`, `workday auth_failed 7`, `linkedin_unknown_questions 3`, and one remaining `linkedin_validation_loop` row. The last LinkedIn loop is now isolated to Raydar `#459`, whose saved screenshot `output/our-client/pm-integrations/submit/linkedin_autofill_pages/page_04.png` shows prose answers being placed into fields that the surface validates as decimal quantities.
- `2026-03-29 17:35 UTC` Closed the next Workday false-stop slice and re-ran the full current-repo `workday auth_unknown` backlog. The generalized fixes are now:
  - `scripts/workday_auth.py` extracts shared Workday auth classification/result building out of `scripts/autofill_workday.py`, keeping the board file back under the repo line-limit gate while preserving the new auth-state behavior.
  - `scripts/autofill_workday.py::_workday_preferred_locator()` now narrows even a single current match to a stable single locator, which fixes the live duplicate-email strict-mode failures that were previously stopping LiveRamp, GM, Nasdaq, and Levi's on `Create Account`.
  - `scripts/autofill_workday.py::_run_workday_auth_flow()` now recovers Autodesk-style post-auth states more truthfully:
    - authenticated `userHome` / `View Application` surfaces now sync as `already_applied` instead of collapsing into `auth_unknown`
    - Gmail polling failures from `gws` now surface the real OAuth error immediately but do **not** block later Workday recovery steps from producing a more specific final auth result
  - live evidence:
    - Autodesk rerun now exits directly as `status = already_applied` with `message = Workday already shows this job as applied.`
    - `gws auth status` reports `token_valid = false` and `token_error = Token has been expired or revoked.`, and direct Gmail calls now reproduce the same `invalid_grant` error outside the pipeline
    - `output/playwright/workday-auth-unknown-rerun-20260329T171633Z.jsonl` records a full rerun of all `12` current-repo `workday auth_unknown` rows
    - those reruns collapsed into concrete truths: `5` `auth_failed / credential_rejected`, `4` `auth_unknown / sign_in_gate` with explicit account-verification blockers, and `3` `auth_unknown / password_reset_gate` with explicit `gws auth login` blockers
  - after `uv run bin/job-assets sync`, the current-repo Workday cluster is now:
    - `workday auth_failed = 8`
    - `workday auth_unknown = 7`
    - the remaining `auth_unknown` rows are no longer opaque; they are account-verification / Gmail-auth environment blockers, not the earlier selector/classification bug class
  - current current-repo queue truth after the sweep:
    - `stopped = 122`
    - `draft = 77`
    - `submitted = 38`
    - `queued = 28`
- `2026-03-29 18:03 UTC` Landed the next queue-truth reconciliation slice across all remaining stopped families:
  - `scripts/job_db.py` now lets ready draft proof override stale failed `application_submission_result.json` artifacts instead of leaving proof-ready rows stuck as stopped. Live evidence: Rubrik `#360` now syncs from `greenhouse_runtime_error` to `draft`, backed by `output/rubrik-job-board/staff-platform-pm-platform-cloud-security/submit/greenhouse_autofill_pre_submit.png`.
  - `scripts/submit_review_common.py::load_pending_user_input_for_submit_attempt()` now ignores stale `pending_user_input.json` payloads from the wrong board and filters artifact blockers that current proof already satisfies. Live evidence: Engineer / Synopsys `#42` now syncs from stale Avature `retries_exhausted` to `draft`, backed by `output/engineer/engineer-the-future/submit/avature_autofill_pre_submit.png`.
  - `scripts/submit_review_common.py::resolve_current_submit_artifacts()` now ranks all current-board candidates by proof strength instead of stopping at the first hinted payload file. That prevents stale LinkedIn payloads from masking newer downstream Greenhouse / Lever / iCIMS proof on external-apply handoffs. Representative evidence:
    - Altana still truthfully stops as `external_apply`, and `output/altana/principal-pm-customer-data-platform/submit/linkedin_external_apply_page.png` proves the current top-card `Apply` state.
    - Aircall's active submit attempt now resolves as Lever proof instead of a stale LinkedIn payload, and `output/aircall/senior-pm-growth-for-small-businesses/submit/lever_autofill_review.png` shows the downstream form state with the real remaining blocker on `Current location`.
  - `scripts/job_db.py` now treats `workday_auth_failure.json` as a fallback classifier only when no newer `application_submission_result.json` already classified the run. Snap `#107` now syncs as truthful `workday / skipped_captcha` instead of reusing a stale maintenance-page `service_unavailable` classification.
  - Current non-archived queue truth after the full cluster audit:
    - `submitted = 173`
    - `stopped = 116`
    - `draft = 85`
    - `queued = 54`
    - `generating = 4`
  - Current stopped-cluster truth after the audit:
    - `linkedin external_apply = 83`
    - `workday auth_failed = 8`
    - `linkedin no_apply_button = 7`
    - `workday auth_unknown = 7`
    - `avature skipped_captcha = 4`
    - `icims skipped_captcha = 3`
    - `unknown unsupported = 3`
    - `workday skipped_captcha = 1`
  - There are now `0` stopped rows whose active repo-local proof already resolves as `ready`; the remaining stopped rows are either truthful/manual stop families or still-blocked current proof, not stale queue state.
- `2026-03-29 18:22 UTC` Closed the last stale stopped-row inconsistency inside the surviving LinkedIn manual clusters:
  - root cause:
    - `scripts/submit_application.py` can return exit code `0` even when the current attempt truthfully ends in a non-draft terminal state such as `not_easy_apply` or `skipped_captcha`
    - `scripts/pipeline_orchestrator.py` previously treated every draft-mode `0` return as "draft ready" and ran draft-summary / draft-proof validation anyway
    - that let terminal submit results get overwritten in-worker as stopped `incomplete`, after which disk sync could later restore the old `failure_type` and leave an internally inconsistent row
  - generalized fix:
    - `scripts/pipeline_orchestrator.py` now short-circuits draft-mode processing on terminal current-attempt `application_submission_result.json` states before any draft-proof generation or completeness validation
    - the same short-circuit now runs on both the first submit attempt and the auto-fix retry path
    - `scripts/job_db.py::_SUBMISSION_STATUS_MAP` now also classifies `pending_user_input`, `unknown`, and `skipped_auth_failure` so disk sync stays aligned with the board/runtime truth
  - live rerun proof:
    - CoreWeave `#504` now stops truthfully as LinkedIn `external_apply` with `output/coreweave/product-strategy-principal/submit/linkedin_external_apply_page.png`
    - Supermicro `#534` now also stops truthfully as LinkedIn `external_apply` with `output/supermicro/principal-pm-dcim-software-27484/submit/linkedin_external_apply_page.png`
    - both rows now log `submission_result_stopped` instead of `draft_incomplete`
  - queue truth after the reruns:
    - `submitted = 173`
    - `stopped = 116`
    - `draft = 85`
    - `queued = 54`
    - `generating = 4`
  - current stopped-cluster truth is now:
    - `linkedin external_apply = 84`
    - `workday auth_failed = 8`
    - `workday auth_unknown = 7`
    - `linkedin no_apply_button = 6`
    - `avature skipped_captcha = 4`
    - `icims skipped_captcha = 3`
    - `unknown unsupported = 3`
    - `workday skipped_captcha = 1`
  - the stale symptom is gone:
    - `0` stopped rows now carry `Incomplete draft ...`
    - LinkedIn stopped rows now split cleanly into `84` truthful `external_apply` messages and `6` truthful `no_apply_button` messages
- `2026-03-29 19:01 UTC` Landed the next screenshot-readability generalization across LinkedIn and Workday:
  - `scripts/autofill_common.py` now exposes `capture_scrollable_locator_screenshot()` plus `concatenate_images_vertically()`, and `capture_full_page()` now stitches internally scrollable preferred containers instead of only capturing the currently visible viewport
  - `scripts/autofill_linkedin.py` now captures Easy Apply proof as a composite of the modal header plus stitched modal content when those surfaces are present, and non-modal job pages now prefer structural job-detail capture instead of a top-card-only screenshot
  - `scripts/autofill_workday.py` now builds `workday_autofill_pre_submit.png` by concatenating the current wizard page captures, so the draft-proof artifact reflects the full multi-screen flow instead of only the last review surface
- `2026-03-29 19:01 UTC` Captured live proof after the screenshot-composition pass:
  - Greylock rerun at `output/greylock/sr-pm-security-ai/submit-20260329T185755Z` rewrote `linkedin_autofill_pre_submit.png` from `2720x1800` to a tighter `1488x2316` proof image, with current step captures preserved in `linkedin_autofill_pages/page_01.png` through `page_05.png`
  - Turo rerun at `output/turo/lead-pm-host/submit-20260329T185846Z` rewrote `workday_autofill_pre_submit.png` from `2048x4000` to `2048x12280`, concatenating `page_01_my_information.png`, `page_02_my_experience.png`, `page_03_application_questions.png`, and `page_04_review.png`
- `2026-03-29 19:21 UTC` Closed the remaining Workday account-verification classification gap and proved the next blocker is Gmail auth, not an opaque Workday shell:
  - `scripts/workday_auth.py` now classifies `Verify your account before you sign in or request a verification email.` and `Resend Account Verification` surfaces as `account_verification_gate` instead of flattening them into `sign_in_gate`
  - `scripts/autofill_workday.py` now follows the create-account verification path in both real variants:
    - when Workday shows the verification gate immediately after `Create Account`
    - when Workday first bounces back to `Sign In` and only reveals the verification gate after the fresh sign-in attempt
  - the new recovery branch clicks `Resend Account Verification`, waits for a Workday verification email via `gws`, opens the verification link, and then resumes sign-in if Workday returns to the auth shell
  - live Calix rerun proof at `output/calix/senior-pm-intelligent-access/submit-20260329T191800Z` now shows:
    - screenshot `workday_submit_debug.png` with the visible message `An email has been sent to you.` plus `Resend Account Verification`
    - `workday_auth_failure.json` with `auth_state = account_verification_gate` instead of the earlier generic `sign_in_gate`
    - runtime logs reaching `Workday: waiting for account verification email...` before stopping on the separate environment issue `gws invalid_grant`
- `2026-03-29 19:50 UTC` Gmail auth is restored and the full current-repo Workday `auth_unknown` cluster was rerun:
  - `gws auth login -s gmail` completed successfully
  - `gws auth status` now reports `token_valid = true`
  - a real Gmail probe `gws gmail users messages list --params {"userId":"me","maxResults":1,"q":"newer_than:1d from:otp.workday.com"}` now succeeds
  - full rerun log: `output/playwright/workday-auth-unknown-rerun-20260329T192713Z.jsonl`
  - real rerun outcomes:
    - Calix `#386` no longer stops in auth at all; it now reaches the form and exits with `failure_type = application_questions_validation` on `If you are willing to travel, what percentage of time?`
    - LiveRamp `#200` now truthfully lands on `account_verification_gate` after the new resend-account-verification branch runs
    - Etsy `#293`, Relativity `#19`, Qualys `#240`, and Houlihan Lokey `#423` now end on `create_account_gate` rather than a local Gmail-auth outage
    - OutSystems `#383` now preserves a more specific `sign_in_gate` with `Apply for Future Opportunities page is loaded`
  - one more generalized queue-truth fix landed alongside the rerun:
    - `scripts/job_db.py` now reads `application_submission_result.json` from the active submit dir before older `submit/` artifacts
    - this prevents fresh reruns like Calix from being overwritten by stale `submit/application_submission_result.json` or `workday_auth_failure.json`
    - regression coverage: `tests/test_job_db.py::test_sync_job_from_disk_prefers_active_workday_failed_result_over_stale_submit_auth_artifact`
  - after `uv run bin/job-assets sync`, the live Workday split is now:
    - `workday auth_failed = 8`
    - `workday auth_unknown = 6`
    - `workday application_questions_validation = 1`
    - `workday skipped_captcha = 1`
- `2026-03-29 20:46 UTC` Closed the next stale-cluster truth gap and landed richer Workday auth-state persistence:
  - `uv run bin/job-assets sync` proved the entire residual `greenhouse unknown = 11` cluster was stale queue state, not a live Greenhouse runtime bug:
    - `Chainguard #7` now syncs as truthful `submitted` because the active repo-local output already had website-confirmation evidence
    - `Databricks #10`, `Adyen #21`, `fal #63`, `Kalshi #77`, `Sage #102`, `Slide Insurance #133`, `Sixfold #140`, `Prove #141`, `Stripe #149`, and `LaunchDarkly #153` now sync back to `draft`
    - each of those rows already had active proof under `submit-20260329T18*` with `greenhouse_autofill_pre_submit.png` plus `greenhouse_autofill_review.png`
  - generalized DB fix:
    - `jobs.db` now stores `auth_state` alongside `failure_type` and `auth_scope`
    - both `scripts/job_db.py::sync_job_from_disk()` and `submission_result_outcomes.handle_draft_mode_submission_result()` persist the current auth substate instead of collapsing Workday back to generic `auth_unknown`
    - ready-proof promotion now clears stale `auth_state` metadata the same way it already cleared stale `failure_type`
  - current repo-local Workday split after the sync is now explicit:
    - `auth_failed / credential_rejected = 8`
    - `auth_unknown / create_account_gate = 4`
    - `auth_unknown / account_verification_gate = 1`
    - `auth_unknown / sign_in_gate = 1`
    - `skipped_captcha = 1`
  - live rerun sweep started with the richer DB contract in place:
    - rerun log: `output/playwright/workday-stopped-rerun-20260329T203637Z.jsonl`
    - early live proofs: Walmart `#14`, FactSet `#62`, General Motors `#208`, and Zillow `#314` all rerun to truthful `auth_failed / credential_rejected` with fresh `workday_auth_failure.json`, `application_submission_result.json`, and `workday_submit_debug.png`
- `2026-03-29 21:30 UTC` Closed the remaining ambiguity in both target clusters:
  - Greenhouse stale-truth cluster is now fully gone:
    - after the final targeted resync pass, there are no active `greenhouse / unknown` rows left
    - final truth for the investigated Greenhouse set is:
      - `Chainguard #7`, `Databricks #10`, `Adyen #21`, `fal #63`, `Kalshi #77`, `Sage #102`, `Slide Insurance #133`, `Sixfold #140`, `Prove #141`, `Stripe #149`, `LaunchDarkly #153 -> draft`
    - proof stayed repo-local the whole time; for example:
      - `output/chainguard/senior-pm-containers/submit-20260329T183545Z/greenhouse_autofill_review.png`
      - `output/databricks/sr-pm-data-governance/submit-20260329T183641Z/greenhouse_autofill_review.png`
  - Two generalized Workday fixes were confirmed with fresh reruns:
    - auth results now preserve the last informative auth gate instead of letting a later browser error page collapse the row back to generic `unknown`
      - evidence: `output/playwright/workday-followup-rerun-20260329T211841Z.jsonl`
      - LiveRamp `#200` now persists `auth_unknown / account_verification_gate`
      - screenshot proof: `output/liveramp/lead-pm-cloud-embedded-identity/submit-20260329T193004Z/workday_submit_debug.png`
    - Workday application detection now treats public `introduceYourself` forms as real application pages instead of routing them through auth recovery
      - OutSystems `#383` moved from `auth_unknown / sign_in_gate` to a truthful `my_experience_validation`
      - screenshot proof: `output/outsystems/outbound-pm-director-public-sector/submit-20260329T194038Z/workday_submit_debug.png`
  - One more DB generalization landed after the OutSystems rerun:
    - `scripts/job_db.py::sync_job_from_disk()` now clears stale `auth_state` and `auth_scope` when a current non-auth stop wins over an older auth artifact
    - `update_status()` also clears `auth_scope` when a row leaves `stopped`, matching the existing stale-failure cleanup behavior
    - ready-proof reconciliation now also overrides stale stopped artifacts even when the row was already marked `submitted`, which is what finally removed the last Greenhouse regressions during targeted resync
  - Final repo-local Workday stopped split after `uv run bin/job-assets sync` is now:
    - `auth_failed / credential_rejected = 8`
    - `auth_unknown / create_account_gate = 4`
    - `auth_unknown / account_verification_gate = 1`
    - `auth_unknown / password_reset_gate = 1`
    - `my_experience_validation = 1`
  - The original target buckets are therefore reduced to concrete truth instead of generic wrappers:
    - no residual `greenhouse / unknown`
    - no residual `workday / auth_unknown / unknown`
- `2026-04-03 21:01 UTC` Ran the next full stopped-job drain and repo-local audit refresh:
  - Current-pass baseline before rerun batching:
    - active non-archived `stopped = 950`
    - largest board buckets: `greenhouse = 243`, `workday = 160`, `unknown = 149`, `ashby = 93`, `linkedin = 91`, `lever = 60`
    - largest artifact-backed clusters:
      - `unknown / unsupported = 75`
      - `linkedin / not_easy_apply = 74`
      - `greenhouse / job_closed = 59`
      - `unknown / retries_exhausted = 56`
      - `greenhouse / retries_exhausted = 41`
      - `workday / auth_unknown = 37`
      - `workday / failed = 36`
  - Representative reruns proved the audit must keep preferring current screenshots over inherited stop labels:
    - Uber `group-pm-grocery-retail-advertising` is a truthful auth gate, not a first-name autofill bug:
      - `output/uber-eats-is-expanding-its-mission-to/group-pm-grocery-retail-advertising/submit/uber_autofill_pre_submit.png`
    - Qualcomm `director-pm` is a truthful Eightfold sign-in gate, not a fake first-name blocker:
      - `output/qualcomm/director-pm/submit/eightfold_autofill_pre_submit.png`
    - SoFi `principal-pm-ai-features` reruns cleanly to draft proof:
      - `output/sofi/principal-pm-ai-features/submit/greenhouse_autofill_pre_submit.png`
    - Databricks `sr-pm-compute-platform` reruns cleanly to draft proof:
      - `output/databricks/sr-pm-compute-platform/submit/greenhouse_autofill_pre_submit.png`
  - Bulk recovery work for this pass:
    - queued `576` safe reruns from the active stopped set
    - later found that the apparent legacy-root subset was not a missing-clone problem at all; `/Users/jerrison/00-projects/11-job-application-material-creation` is a symlink into the current repo
    - the real failure mode was stale string-path serialization plus ad-hoc rerun filters that treated the symlink alias as foreign
  - Generalized repo fix:
    - `scripts/job_db.py::_repo_local_output_candidate()` now canonicalizes output dirs that resolve inside the current repo even when the stored string still uses the legacy symlink path
    - `scripts/job_db.py::migrate_legacy_output_dirs()` now exists for genuine non-symlink legacy trees, so future audits can repoint or copy them into the current repo before reruns
    - targeted regression coverage:
      - `test_sync_job_from_disk_canonicalizes_symlinked_legacy_output_dir`
      - `test_migrate_legacy_output_dirs_copies_stopped_legacy_tree_into_current_repo`
      - `test_migrate_legacy_output_dirs_repoints_existing_repo_local_tree_without_copy`
  - Live recovery results after that fix:
    - `migrate_legacy_output_dirs(statuses=('stopped',)) -> repointed_existing = 10`
    - explicitly restarted the skipped alias-root jobs `#16`, `#73`, `#83`, `#99`, `#107`, `#123`, `#124`, `#169`, `#229`, and `#248`
    - current queue truth at `2026-04-03 21:01 UTC`:
      - `queued = 476`
      - `generating = 6`
      - `autofilling = 2`
      - `draft = 280`
      - `submitted = 238`
      - `stopped = 349`
  - Current interpretation:
    - the remaining stopped set is now mostly truthful manual or tenant-side terminal families (`unsupported`, `external_apply`, `job_closed`, auth/account gates, and explicit pending-user-input stops)
    - the next resilience work should focus on shrinking the remaining manual-validation and auth-gate families, not on another stale-stop reconciliation pass

## Outstanding Questions

### Resolve Before Planning

- None.

### Deferred to Planning

- [Affects R7] [Technical] Decide whether the lone Raydar `linkedin_validation_loop` outlier should be fixed by detecting numeric-validation shells after retry or by adding a narrower LinkedIn number-question heuristic for short skill labels like `Integrations` and `Partner Ecosystem`.
- [Affects R7] [Technical] Decide whether `gws` token health should be checked once at startup for every Gmail-dependent flow instead of being discovered lazily inside password-reset / verification polling.
- [Affects R7] [Technical] Decide whether to add one shared submit-failure artifact schema across boards or keep board-specific JSON files with a required common field contract.
- [Affects R7] [Technical] Decide whether a report that references a missing screenshot should hard-fail artifact generation immediately or emit a structured `screenshot_missing` failure artifact that blocks review.

## Next Steps

1. Re-queue the four rows stranded in `generating` by the worker-stop isolation step (`#16`, `#26`, `#73`, `#83`), restart the worker, and let it continue draining the remaining queued redrafts before taking the next queue snapshot.
2. Fix the lone Raydar / Our client `linkedin_validation_loop` outlier so LinkedIn no longer has any residual opaque validation-loop stops in the current repo.
3. Fix the Workday percentage-dropdown / value-selection path exposed by Calix `#386`, where the runtime now reaches `application_questions` but still fails to select percentage answers like `Up to 25%`.
4. Decide whether the `create_account_gate` subset (`Etsy`, `Relativity`, `Qualys`, `We`) needs broader reset-email queries or should stay on a truthful tenant-specific create-account stop.
