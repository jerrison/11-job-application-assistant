# TrueUp My Jobs Import — Design Spec

**Date:** 2026-03-29
**Status:** Draft

## Overview

Add support for importing saved jobs from `https://www.trueup.io/myjobs` into the job queue across the web UI, TUI, and CLI. The import must start from a logged-in TrueUp session, follow each saved job through TrueUp until it reaches the external application page, and only then add the resolved external URL to the queue.

This feature should reuse the existing duplicate-prevention path in `add_job()` so TrueUp imports behave the same as manual URL adds and LinkedIn saved-job imports. Jobs that never resolve off TrueUp are skipped and reported, not queued.

## Motivation

The repo already supports three ways of putting jobs into the queue:

- manual URL add via web/TUI/CLI
- LinkedIn saved-job import
- output-directory import for previously generated artifacts

TrueUp saved jobs belong with the first two, not the third. The user wants a seamless path from the saved-jobs portal into the existing draft-first application pipeline, with the same duplicate handling and without manual copy-paste of individual external links.

## Goals

- Import TrueUp saved jobs from a logged-in session.
- Resolve each saved job to the external application page before queueing it.
- Preserve the original TrueUp job URL as provenance.
- Expose the feature consistently in web UI, TUI, and CLI.
- Reuse the current duplicate rules and downstream JD dedup pass.

## Non-Goals

- No repo-managed TrueUp login/bootstrap flow in this feature.
- No new duplicate-matching heuristics beyond the current `add_job()` and JD-fingerprint logic.
- No change to submission behavior, worker orchestration, or draft-mode safeguards.
- No attempt to queue unresolved TrueUp pages as fallback jobs.

## User-Facing Behavior

### Success Boundary

A TrueUp job is considered importable only if the importer reaches a non-TrueUp external job page that the existing queue can process. That means the final target must be the real application destination, not the TrueUp listing itself, a generic TrueUp landing page, or an ambiguous redirect screen.

### Per-Row Outcomes

Each saved TrueUp row must end in exactly one of these states:

- **added**: the row resolved to an external job URL and `add_job()` inserted it
- **duplicate**: the row resolved, but `add_job()` rejected it as an existing active job
- **skipped_unresolved**: the row never exposed a trustworthy external target
- **error**: the importer hit an unexpected row-level failure such as timeout, DOM drift, or navigation error

Session-level auth failures are handled separately and stop the whole import early.

## Architecture

### Recommended Shape

Implement a thin shared saved-portal import runner with provider-specific adapters.

- A shared runner owns result accounting, dedup-aware insertion, downstream fingerprint backfill, and the common return shape.
- LinkedIn remains supported, but its current import path is refactored to feed the shared runner.
- TrueUp is added as a second adapter that performs the extra TrueUp-only resolution work.

This avoids a one-off TrueUp-only implementation while stopping short of a large framework rewrite.

### Shared Runner Responsibilities

A shared module should:

- accept a DB connection plus a portal adapter
- iterate scraped portal entries
- resolve each entry to a queueable external URL or a skip reason
- call `add_job()` with provider/priority overrides and import metadata
- count added, duplicates, unresolved skips, and errors
- run `backfill_jd_fingerprints()` and `find_jd_duplicates()` after import completes
- return one consistent result payload for CLI, web, and TUI consumers

### Adapter Responsibilities

Each portal adapter should own only portal-specific browser work.

**LinkedIn adapter**
- scrape saved jobs using the existing LinkedIn profile/session approach
- emit rows that already point at LinkedIn job URLs
- allow the shared runner to preserve existing duplicate and downstream dedup behavior

**TrueUp adapter**
- open `https://www.trueup.io/myjobs` with a persistent TrueUp browser profile
- scrape saved job rows and their detail URLs
- open each job detail and follow the visible external-apply path
- return the external URL plus best-effort company/title metadata when available
- classify unresolved and auth-required states explicitly

## Data Model And Provenance

### Required Record Semantics

TrueUp imports must queue the resolved external URL while still remembering that the job came from TrueUp.

The intended persisted shape is:

- `url`: resolved external application URL
- `board_url`: resolved external application URL when already known at import time
- `canonical_url`: same resolved external URL initially, then normal pipeline refinement as needed
- `source`: `trueup`
- `source_url`: original TrueUp job detail URL

### DB/API Implication

`add_job()` currently infers `source`, `source_url`, and `board_url` from the input URL. That is correct for manual adds, but insufficient for TrueUp because the queue target and provenance URL are different.

The narrow change is to extend `add_job()` so portal imports can override source metadata while still using the same insertion and dedup path. The design should not introduce a parallel job-insertion function.

### Source Taxonomy

Add `trueup` to the URL-source taxonomy so imported jobs can be filtered and reasoned about consistently alongside `linkedin`, `indeed`, and `direct`.

## Surface Design

### Web UI

Add a `Saved Portals` section inside the existing `Add Jobs` view beneath the manual URL textarea. It should expose:

- `Import LinkedIn Saved`
- `Import TrueUp My Jobs`

The queue-toolbar singleton LinkedIn button should become a compact pair of saved-portal quick actions so both portals are equally discoverable from the main workflow.

Result feedback should use one consistent summary language:

- added
- duplicates
- unresolved skipped
- errors

The add-jobs manual URL flow remains unchanged.

### TUI

Extend the existing `Add Jobs` screen with a second import action:

- `Import LinkedIn Saved`
- `Import TrueUp My Jobs`

No new mode or screen is needed. Feedback stays in the current `#add-feedback` area, expanded to include unresolved skips and errors.

### CLI

Do not overload the existing `job-assets import` command, because it already means “import output directories into the job database.”

Instead, extend `job-assets add` with a saved-portal mode:

```bash
job-assets add --saved-portal trueup
job-assets add --saved-portal linkedin
```

Behavioral rules:

- `urls` remain the normal required positional arguments for manual add
- `--saved-portal` makes URL positionals unnecessary for that invocation
- `--priority` and `--provider` still apply to imported jobs
- CLI summary output must match the same counters exposed in web/TUI

## TrueUp Resolution Flow

### Browser Profile

Use a dedicated persistent browser profile for TrueUp, parallel to the LinkedIn pattern. This feature assumes the TrueUp session is already logged in inside that profile. If it is not, the importer returns an auth-required result instead of trying to automate login.

### Resolution Algorithm

For each scraped TrueUp row:

1. Open the saved-job detail page on TrueUp.
2. Find the visible control that navigates to the external application destination.
3. Click through until the browser reaches a non-TrueUp external URL.
4. Validate that the final destination is trustworthy enough to queue:
   - not a TrueUp URL
   - not an empty/interstitial page
   - not a generic redirect hub with no stable job target
5. Return the resolved external URL plus the original TrueUp detail URL and best-effort metadata.

### Unresolved Classification

Return `skipped_unresolved` for cases such as:

- no visible external-apply control
- click leaves the user on TrueUp
- click opens an ambiguous or blank target
- redirect chain never reaches a stable external job page
- the target is clearly not a job application destination

### Auth Classification

Return an auth-required failure for cases such as:

- `/myjobs` redirects to login
- the saved jobs page cannot be read because the session is unauthenticated
- job detail pages consistently bounce to auth walls

Auth failures stop the whole import because they indicate the session is unusable, not that a particular row is bad.

## Result Contract

Portal imports should return a shared superset response shape:

```json
{
  "status": "ok",
  "message": "",
  "scraped": 0,
  "resolved": 0,
  "added": 0,
  "duplicates": 0,
  "skipped_unresolved": 0,
  "errors": 0,
  "fingerprints_added": 0,
  "duplicate_groups": [],
  "samples": {
    "unresolved": [],
    "errors": []
  }
}
```

Notes:

- `status` is `ok` for normal imports and `auth_required` when the session is unusable
- `message` carries the user-facing explanation for auth or portal-wide failures
- `resolved` counts rows that produced a queueable external URL before dedup
- LinkedIn can report `resolved == scraped` for rows that already produce queueable URLs
- `samples` is optional in the UI but useful for debugging and CLI detail

## Failure Handling

### Fail-Fast Conditions

Stop the import immediately when:

- the TrueUp session is not authenticated
- the saved-jobs root page is unavailable in a way that prevents scraping any rows
- the adapter detects a portal-wide structural failure rather than a row-specific miss

### Continue-On-Error Conditions

Continue row-by-row when:

- a single saved job detail page times out
- one row’s external-apply control is missing
- a row resolves to a dead or ambiguous destination
- one row triggers an unexpected browser/navigation exception

This preserves throughput without hiding broken sessions as partial success.

## Testing Strategy

### Unit Coverage

Add deterministic tests for:

- shared runner result accounting
- duplicate classification via negative `add_job()` returns
- unresolved-row counting and sampling
- auth-required short-circuit behavior
- metadata preservation for `source=trueup` and `source_url=<trueup url>`

### Adapter Coverage

TrueUp adapter tests should cover:

- scraping saved-job rows from `/myjobs`
- resolving a detail page to an external URL
- classifying unresolved rows correctly
- distinguishing row-level failures from auth/session failures

These tests should stub browser-facing helpers rather than rely on live TrueUp in CI.

### Surface Coverage

Add tests for:

- web endpoint/response shape for TrueUp import
- TUI Add Jobs screen exposing the TrueUp action
- CLI `job-assets add --saved-portal trueup` wiring and summary output
- regression coverage proving the existing manual `job-assets add <url...>` flow stays unchanged
- regression coverage proving LinkedIn import still works after moving to the shared runner

## Likely Files

### Create

- `scripts/import_trueup_saved.py` — TrueUp adapter and CLI entry point
- `scripts/saved_portal_import.py` — shared portal-import runner and result contract
- portal-specific tests for the new runner and TrueUp adapter

### Modify

- `scripts/import_linkedin_saved.py` — move to the shared runner while preserving behavior
- `scripts/job_db.py` — allow portal imports to override source metadata while keeping the same dedup path
- `scripts/url_resolver.py` — add `trueup` to source detection
- `scripts/job_web.py` — add a TrueUp import endpoint and shared response handling
- `scripts/static/index.html` — expose TrueUp import actions in Add Jobs and queue toolbar
- `scripts/static/app.js` — trigger the TrueUp import and render expanded feedback
- `scripts/job_tui.py` — add TrueUp action and feedback handling
- `bin/job-assets` — extend `add` with `--saved-portal`
- docs and tests that describe/import the new surface

## Operational Notes

- The feature inherits the repo’s existing duplicate semantics from `add_job()` and the post-import JD dedup pass.
- Manual session priming for the dedicated TrueUp profile is an operational prerequisite, not a productized workflow in this feature.
- The importer must not silently queue TrueUp wrapper URLs when external resolution fails.

## Out Of Scope Follow-Ups

- a first-class `trueup-login` bootstrap command
- broader “saved portals” onboarding UX
- richer unresolved-row inspection UI
- generalized support for additional saved-job portals beyond LinkedIn and TrueUp
