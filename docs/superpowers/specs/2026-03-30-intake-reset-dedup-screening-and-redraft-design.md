# Intake, Reset, JD Dedup, Screening Fixes, and Queue Redraft Design

## Overview

This batch combines three user-visible needs that touch the same workflow boundary:

1. add more jobs into the queue from a new saved-jobs portal
2. reset existing draft jobs back to a freshly added state so new fixes can apply cleanly
3. close the remaining 2026-03-30 regressions before rerunning the active queue

The right design is one sequenced batch, not a pile of one-off fixes. Intake and reset determine which jobs enter the queue and what baseline state they start from. Shared screening and closed-job handling determine whether reruns stop truthfully. Only after those rules are in place should the system redraft every job that is neither submitted nor archived.

## Motivation

The current repo already has a saved-portal import pattern, a regenerate path, shared question classification, and draft-mode result syncing. The tracker asks for extensions of those capabilities, not a new workflow.

What is missing today is consistency:

- saved-portal support is not registry-driven, so adding Jack & Jill would currently require more hardcoded branching across CLI, TUI, and web
- rerun tooling can regenerate a draft, but it cannot reset a row back to the semantics of “just added”
- JD dedup exists later in the pipeline, but not on every add path
- some screening decisions still depend on board-local heuristics instead of shared policy
- LinkedIn still classifies some visibly closed jobs as generic not-easy-apply outcomes instead of archiving them truthfully

## Goals

- Add Jack & Jill saved-job import support across CLI, TUI, and web.
- Replace hardcoded saved-portal branching with a shared portal registry that can scale beyond LinkedIn and TrueUp.
- Add a reset action that returns a job to the same effective state as a newly added row without deleting the row or falsifying history.
- Run JD-language dedup for every new add path that already has JD text available, and make that path reusable for future importers.
- Promote the remaining 2026-03-30 screening bugs into shared policy instead of one board patch at a time.
- Detect LinkedIn closed-job states early enough to emit `job_closed` and auto-archive through the existing draft/result sync flow.
- After the code fixes land, redraft every non-submitted, non-archived job and fix operational errors encountered during that rerun.

## Non-Goals

- No auto-submit behavior changes. Draft mode remains fail-closed.
- No attempt to automate Jack & Jill authentication inside the repo.
- No fuzzy semantic-embedding dedup system. This batch uses the existing JD fingerprint model and moves it earlier in the add flow.
- No destructive reset that deletes proof history or prior submission evidence.
- No board-specific policy forks for startup, sponsorship, or multi-select unless a board cannot consume the shared policy hook.

## Options Considered

### 1. Patch each tracker item independently

Add Jack & Jill wherever needed, add a one-off reset endpoint, patch Greenhouse or LinkedIn locally, then run the queue.

Rejected because it would preserve the current drift across CLI, TUI, web, and board adapters. The next saved portal or screening bug would create the same cleanup again.

### 2. Large workflow rewrite before fixing the bugs

Introduce a new intake framework, a new reset state machine, and a new universal answer planner before addressing the tracker items.

Rejected because it is too much change for a bug-and-rerun batch. The repo already has the right seams; the work is to consolidate and extend them.

### 3. Shared registry and shared policy, then targeted rerun

Generalize the existing saved-portal and screening seams, add one reset-to-newly-added capability, and only then rerun the live queue.

Chosen because it solves the named issues with the smallest durable extension of current architecture.

## Chosen Design

The batch is delivered in three slices:

1. **Intake and reset**
   - Jack & Jill saved-job import
   - shared saved-portal registry across CLI, TUI, and web
   - reset-to-newly-added action
   - add-time JD dedup hook
2. **Shared board-agnostic bug fixes**
   - multi-select selection floor and “all that apply” handling
   - startup and sponsorship routing through shared policy
   - LinkedIn closed-job detection that yields `job_closed`
3. **Operational rerun**
   - redraft every job that is neither submitted nor archived
   - keep draft-mode safeguards
   - fix deterministic rerun failures that surface during the batch

Each slice should be shippable and testable on its own. The rerun depends on the first two slices being complete.

## Slice 1: Intake and Reset

### Saved-Portal Registry

Saved-portal support should move from repeated `if portal == "linkedin"` branching to a shared registry that owns:

- portal identifier
- user-facing label
- importer module or adapter callable
- whether the portal needs a persistent authenticated profile
- any portal-specific result wording

CLI, TUI, and web should all read from the same registry so a new portal is exposed once and rendered everywhere.

### Jack & Jill Import

Jack & Jill should follow the same architectural shape as the existing saved-portal import flow:

- scrape the saved/opportunities list from the logged-in Jack & Jill session
- resolve each row to the queueable job URL
- preserve Jack & Jill provenance separately from the resolved job URL
- feed the resolved target through the standard insertion path

The importer should stop early on session/auth failure and continue row-by-row on row-specific resolution failures.

### Provenance Model

The resolved queue URL and the portal provenance URL are different concepts and both must survive insertion.

For Jack & Jill imports:

- `url`, `board_url`, and initial `canonical_url` should be the resolved external job URL
- `source` should be `jackandjill`
- `source_url` should be the original Jack & Jill listing/detail URL

This should use the same narrow source-override mechanism already needed by saved portals rather than a parallel insert path.

### Reset-To-Newly-Added

The new reset action is not the same as regenerate.

`regenerate` means “keep the current row in the draft pipeline and recompute assets.”

`reset_to_new` means:

- preserve the row identity and durable metadata
- clear draft-era transient state so the row behaves like a fresh add
- remove current-attempt proof/debug/result artifacts that would otherwise masquerade as the next run
- return the row to the same status family that a newly added job uses before generation begins
- schedule any answer-refresh or pipeline work from that clean baseline

Reset should be available anywhere the user can currently rerun jobs:

- web job detail / queue actions
- draft web actions where relevant
- TUI actions

The action should refuse submitted locked rows and archived rows in this batch. Reopening archived jobs remains a separate workflow.

### Add-Time JD Dedup

JD-language dedup should happen for every add path that can provide JD text at insertion time, including new saved portals and future board discovery flows.

The canonical shape is:

- extend the add/insertion path to accept optional `jd_text`
- normalize and fingerprint the JD before or during insertion when `jd_text` is available
- compare against all existing jobs, including archived rows
- if a duplicate exists, skip or archive according to the current duplicate policy instead of letting the row survive into active queue state

Manual URL adds that do not yet have JD text should keep the current later-stage JD dedup behavior. This batch should not invent fake JD text or force scraping before simple manual adds complete.

## Slice 2: Shared Board-Agnostic Bug Fixes

### Multi-Select / “Choose At Least Three” Policy

The remaining bug is not just a Greenhouse issue. The repo needs one shared selection planner for discrete positive-fit multi-select prompts.

The shared rule for non-truth-sensitive checkbox/ranking prompts is:

- when the prompt implies ranked preferences, “choose three,” or “choose at least three,” choose exactly three best-fit options when at least three sensible options are present
- when the prompt says “all that apply,” choose the best-fit subset using the same positive-fit evidence instead of collapsing to one option or skipping
- do not override truthful paths for work authorization, compensation, self-ID, degree, license, or certification claims

This policy should sit above board adapters so Greenhouse, Lever, and other boards can consume the same answer plan when they support multi-select.

### Startup Experience

Startup-experience yes/no prompts should route through shared deterministic policy, not depend on provider text generation or board-local special cases.

The system already has source-backed biography helpers and tracker evidence that startup should resolve affirmatively for this profile. The fix should make startup-style labels classify into the same shared positive-fit family used for other deterministic screening prompts.

### Sponsorship

Sponsorship should remain on the existing truthful work-authorization path, but the remaining skipped cases indicate classification drift and board-specific bypasses.

The fix should:

- broaden or normalize shared sponsorship/work-authorization classification so common wording variants are caught
- route boards that still use inline sponsorship heuristics back through shared policy where possible
- keep the final answer truthful rather than affirmative-by-default

### LinkedIn Closed Jobs

When LinkedIn visibly shows that a job is no longer accepting applications or is no longer open, the board adapter should emit a terminal `job_closed` result instead of a generic `not_easy_apply` or modal-missing failure.

That result should flow through the existing archive behavior:

- draft-mode result handling marks the row archived
- LinkedIn-sourced rows keep the existing dismissal behavior where applicable
- disk sync continues to trust the terminal current-attempt result as source of truth

The key change is earlier and more accurate detection inside the LinkedIn adapter, not a new archive system.

## Slice 3: Queue-Wide Redraft

### Target Set

After slices 1 and 2 land, rerun every job that is:

- not submitted
- not archived

If a row is in draft, stopped, queued, generating, or another active non-submitted state, it belongs in scope.

### Rerun Strategy

Use reset-to-newly-added only where a clean baseline is necessary to pick up the new behavior. For the rest, use the existing regenerate/redraft path if it already preserves the correct semantics.

The batch should prefer truthful repair over brute-force rewriting:

- if a rerun ends in `job_closed`, archive it
- if it hits `already_applied`, submitted, captcha/manual auth, or another truthful terminal state, persist that result instead of forcing more retries
- if it reveals a deterministic code bug, fix the code and continue the batch

### Artifact Hygiene

Before each fresh autofill/redraft attempt, clear stale current-attempt review/debug/result artifacts from the active `submit/` directory in line with current repo rules, so old screenshots or submission results cannot masquerade as the fresh attempt.

### Operational Boundaries

This is still a repo-local proof-first rerun:

- no live submit
- screenshots remain the source of truth
- stop on ambiguous final-review boundaries
- preserve truthful manual blockers instead of flattening them into generic failures

## Surface Design

### Web

The web app should:

- render saved-portal actions from the shared registry, adding Jack & Jill beside LinkedIn and TrueUp
- expose the new reset action distinctly from regenerate/reanswer
- use the same saved-portal summary language for all portals

### TUI

The TUI should:

- render saved-portal buttons from the shared registry
- expose reset-to-newly-added distinctly from regenerate
- keep feedback copy aligned with the web behavior

### CLI

The CLI should:

- keep `job-assets add --saved-portal <portal>` as the saved-portal entry point
- accept `jackandjill` alongside `linkedin` and `trueup`
- not add a new standalone reset command in this batch; reset-to-newly-added is exposed through the existing interactive/operator surfaces and backend APIs

The important invariant is that the behavior exists across the repo’s active operator surfaces, not that every internal route gets a brand-new CLI alias.

## Testing Strategy

Add targeted coverage for:

- saved-portal registry expansion and Jack & Jill dispatch across web/TUI/CLI
- Jack & Jill auth-required, resolved, duplicate, unresolved, and row-error result accounting
- reset-to-newly-added state transitions and artifact cleanup behavior
- add-time JD dedup when `jd_text` is supplied, including duplicate detection against archived rows
- startup and sponsorship classification regressions
- shared multi-select selection planning for “choose three,” “at least three,” and “all that apply”
- LinkedIn closed-job detection yielding `job_closed`
- rerun helpers selecting the intended non-submitted, non-archived scope

Prefer shared-policy tests over board-specific snapshot tests when the behavior is meant to generalize.

## Implementation Notes

Expected touchpoints include:

- `scripts/saved_portal_import.py`
- a new Jack & Jill importer module beside the existing LinkedIn and TrueUp ones
- `scripts/job_db.py`
- `scripts/url_resolver.py`
- `scripts/job_web.py`
- `scripts/job_tui.py`
- `scripts/draft_web.py`
- `scripts/static/index.html`
- `scripts/static/app.js`
- `bin/job-assets`
- `scripts/question_classifier.py`
- `scripts/application_submit_common.py`
- `scripts/autofill_linkedin.py`
- tests covering DB, web, TUI, classifier, and board adapters

The batch should stay additive and narrow. Reuse the current seams, remove hardcoded branching where the tracker proved it has become repetitive, and keep the rerun phase operationally honest rather than optimistic.
