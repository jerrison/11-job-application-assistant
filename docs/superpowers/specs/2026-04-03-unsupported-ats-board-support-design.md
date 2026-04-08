# Design: Unsupported ATS Family Support (Wave 1)

**Date:** 2026-04-03
**Status:** Draft

## Summary

Add first-class support for the repeated unsupported ATS families surfaced in the stopped-job audit, while leaving proprietary Apple Careers and Google Careers flows for a later wave.

Wave 1 covers:

- SAP SuccessFactors / Jobs2Web
- Breezy HR
- Recruitee
- Jobvite
- JazzHR / ApplyToJob
- Paycor Recruiting

The implementation must generalize across direct ATS hosts and company-hosted wrappers, reuse the shared autofill architecture, keep draft-mode fail-closed behavior, and convert the current unsupported stopped rows into truthful draft reruns or truthful terminal blockers with current proof.

## Problem

The stopped-job audit showed that `unsupported` is not a single one-off class. It includes a repeated set of ATS families that recur across multiple employers:

- `jobs.supermicro.com` pages that are actually SuccessFactors / Jobs2Web
- `*.breezy.hr` pages
- `careers.* /o/...` pages that are actually Recruitee
- `jobs.jobvite.com`
- `*.applytojob.com` pages that are JazzHR-backed
- `recruitingbypaycor.com`

Today these roles stop with `unsupported_board.json`, even though they are common enough to deserve first-class support. The real gap is board-family detection plus board-family submitters, not a lack of isolated one-off scrapers.

## Scope

### In Scope

- Add board-family detection and wrapper recognition for:
  - `successfactors`
  - `breezy`
  - `recruitee`
  - `jobvite`
  - `jazzhr`
  - `paycor`
- Add board-specific autofill submitters for those families.
- Redraft the currently stopped unsupported jobs that map into those families.
- Capture new real screenshot proof for rerun outcomes.
- Update repo docs and the Obsidian audit mirror with findings and rollout evidence.

### Out of Scope

- Apple Careers
- Google Careers
- A generic unknown-board autofiller
- Live auto-submit behavior changes
- Manual-only proprietary flows beyond truthful unsupported/manual classification

## Current Unsupported Family Map

Based on the stopped-job audit as of 2026-04-03:

- SuccessFactors / Jobs2Web: `jobs.supermicro.com` and similar hosts
- Breezy: `*.breezy.hr`
- Recruitee: company-hosted `/o/...` role pages with Recruitee markers
- Jobvite: `jobs.jobvite.com`
- JazzHR: `*.applytojob.com`
- Paycor: `recruitingbypaycor.com`

Representative unsupported URLs:

- `https://jobs.supermicro.com/job/...`
- `https://zero-hash.breezy.hr/p/...`
- `https://careers.distribusion.com/o/...`
- `https://jobs.jobvite.com/garten/job/...`
- `https://bitpay.applytojob.com/apply/jobs/details/...`
- `https://recruitingbypaycor.com/career/JobIntroduction.action?...`

## Recommended Architecture

### 1. Extend board-family detection first

The core extension point is the URL-to-board-family path:

- `scripts/job_board_urls.py`
- `scripts/submit_application.py`
- `scripts/url_resolver.py`
- `scripts/job_worker.py`

Wave 1 adds explicit family detection for the new ATS families plus HTML marker probes for company-hosted wrappers.

Examples:

- SuccessFactors wrapper markers:
  - `successfactors.com`
  - `jobs2web`
  - `j2w.apply`
  - `j2w.init`
- Breezy markers:
  - `breezy.hr`
  - Breezy page scripts / branding
- Recruitee markers:
  - `recruitee.com`
  - company-hosted `/o/...` pattern with Recruitee assets
- Jobvite markers:
  - `jobvite.com`
- JazzHR markers:
  - `applytojob.com`
  - `jazzhr`
- Paycor markers:
  - `recruitingbypaycor.com`
  - `paycor`

### 2. Keep explicit per-family submitters

Do not build a generic unsupported ATS fallback. The repo already has the right abstraction:

- shared utilities in `autofill_common.py`
- shared orchestration in `autofill_pipeline.py`
- explicit board scripts for selectors and submit-state logic

Wave 1 should follow that pattern.

#### SuccessFactors / Jobs2Web

- Board name: `successfactors`
- Shape: auth-gated / wizard / multi-step
- Implementation style: custom `run_browser_fn`, similar to Workday and iCIMS
- Truthfulness requirements:
  - explicit auth states
  - explicit account/sign-in/create-account handling
  - explicit draft-proof and terminal-result classification

#### Breezy, Recruitee, Jobvite, JazzHR, Paycor

- Board names:
  - `breezy`
  - `recruitee`
  - `jobvite`
  - `jazzhr`
  - `paycor`
- Shape: mostly single-page or simple stepper flows
- Implementation style: board script + `run_browser_pipeline()`
- Truthfulness requirements:
  - explicit submit-state classifiers
  - explicit attachment confirmation
  - explicit required-field confirmation before draft-ready

## Board Behavior Requirements

Every new family must inherit the repo's existing standards from the start:

- `--draft` only during this rollout
- stop at the final review boundary
- screenshots are the source of truth
- clear stale current-attempt artifacts before reruns
- deterministic questions use shared profile/policy paths
- unsupported narrative prompts become `pending_user_input`
- exact live-value confirmation is required before a field counts as filled
- structured JSON artifacts must exist for terminal skip/failure/auth states

No new family should add a weaker contract than the current supported boards.

## Detection Design

### Direct host detection

Add first-class direct detection helpers and host patterns for:

- `looks_like_successfactors_url()`
- `looks_like_breezy_url()`
- `looks_like_recruitee_url()`
- `looks_like_jobvite_url()`
- `looks_like_jazzhr_url()`
- `looks_like_paycor_url()`

### Wrapper detection

When the direct hostname does not identify the ATS family, `_board_for_url()` should probe the JD HTML for platform markers before falling back to unsupported.

This wrapper probe must remain deterministic and family-based:

- it may inspect HTML markers, known scripts, form actions, and branded assets
- it must not guess based on vague strings
- it must return a board family only when markers clearly match

### Routing and worker awareness

All new board families must be recognized in:

- submit dispatch
- queue board assignment
- worker board rate limiting / scheduling
- URL resolver direct-board detection

This prevents a board from being detectable at intake but still treated as `unknown` later in the worker or sync paths.

## Implementation Order

### Phase 1: SuccessFactors / Jobs2Web

Why first:

- largest repeated unsupported family
- already has prior design/plan material in the repo
- exercises the hardest auth + wizard path first

Deliverables:

- detection and wrapper recognition
- `autofill_successfactors.py`
- tests for routing, canonicalization, auth/result classification, and draft behavior
- reruns of stopped SuccessFactors rows with screenshot evidence

### Phase 2: Single-page ATS family wave

Implement in this order:

1. Breezy
2. Recruitee
3. Jobvite
4. JazzHR
5. Paycor

Reasoning:

- Breezy and Recruitee appear likely to settle the shared single-page ATS patterns first.
- Jobvite, JazzHR, and Paycor can then reuse those lessons while keeping explicit board-family selectors.

## Verification Strategy

### TDD

All work follows test-first behavior changes:

1. failing detection tests
2. failing dispatch tests
3. failing board-behavior tests
4. implementation
5. real reruns

### Real rerun evidence

For each supported family:

- redraft the currently stopped jobs in that family
- attach fresh screenshot artifacts from the real reruns
- record whether each job became:
  - `draft`
  - truthful terminal auth blocker
  - truthful pending-user-input blocker
  - truthful job-closed / external blocker

### Documentation

Update:

- `docs/board-architecture.md`
- `docs/autofill-patterns.md`
- stopped-job audit docs
- Obsidian audit mirror

## Success Criteria

Wave 1 is complete when:

1. the six ATS families are recognized as supported board families
2. their stopped unsupported rows can be redrafted through real board scripts
3. screenshot-backed rerun evidence exists for those rows
4. unsupported stopped counts decrease because repeated ATS families are no longer routed to manual by default
5. remaining unsupported rows are primarily proprietary families or truly novel platforms, not these repeated ATS families

## Risks

- SuccessFactors wrapper detection may collide with existing iCIMS/TalentBrew heuristics unless marker ordering is tightened.
- Recruitee company-hosted wrappers may look like generic company sites unless asset markers are probed carefully.
- Single-page ATS boards may still vary enough that shared helpers should be extracted only after two or more real implementations prove the pattern.

## Recommendation

Implement wave 1 as explicit family support, not as a generic unknown-board autofiller. The right level of generalization is:

- stronger board-family detection
- explicit board-family submitters
- shared proof and orchestration contracts

This preserves the repo's architecture and keeps future board additions understandable for both humans and agents.
