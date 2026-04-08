---
title: "Cross-source duplicate jobs — same position imported multiple times from different boards"
category: database-issues
date: 2026-03-23
tags:
  - dedup
  - job-import
  - add_job
  - canonical_url
  - linkedin
  - greenhouse
components:
  - scripts/job_db.py
  - scripts/job_web.py
problem_type: data-integrity
severity: medium
---

# Cross-Source Duplicate Jobs

## Problem

The same job position appeared 2-3 times in the queue when imported from different sources. Example: Stampli sr-pm-procurement was imported via company careers page (`stampli.com/careers/...`), Greenhouse board (`job-boards.greenhouse.io/...`), AND LinkedIn (`linkedin.com/jobs/view/...`). 25 duplicate groups with 75 extra jobs found.

## Root Cause

`add_job()` in `job_db.py` deduplicated on `canonical_url` only. Jobs from different sources have genuinely different canonical URLs for the same position. No company+role-level dedup existed.

## Solution

Added `_is_duplicate_by_company_role()` check in `add_job()` before the INSERT:

```python
def _is_duplicate_by_company_role(conn, company: str, role_title: str) -> int | None:
    if not company or not role_title:
        return None
    row = conn.execute(
        "SELECT id FROM jobs WHERE LOWER(company) = LOWER(?) AND LOWER(role_title) = LOWER(?) "
        "AND status NOT IN ('archived') LIMIT 1",
        (company, role_title),
    ).fetchone()
    return row[0] if row else None
```

**Caller convention:** Returns `-existing_id` (negative) when duplicate found. Callers check `result < 0` to report "duplicate skipped" vs "job added." The web endpoint now returns `{"added": N, "duplicates": M}`.

**Edge cases handled:**
- Null company/role_title → skip check (broken imports)
- Archived jobs → don't block reimport
- Case-insensitive matching via `LOWER()`

## Investigation Steps

1. Opened "Duplicate Jobs" modal — 25 groups found
2. Queried database for Stampli duplicates — 3 different URLs (careers page, Greenhouse, LinkedIn)
3. Confirmed `canonical_url` was different for each → URL-based dedup can't catch cross-source dupes
4. Added company+role check → exact case-insensitive match covers the common case

## Cleanup Performed

Archived 75 jobs: 25 cross-source duplicates (kept best-status version) + 50 broken imports (null company).

## Prevention

- The `_is_duplicate_by_company_role()` check now runs at import time for all sources
- Future: consider fuzzy matching for company name variants (e.g., "HPE" vs "Hp") — exact match is sufficient for now
- Future: `role_title` slug normalization across boards if slug formats diverge

## Cross-References

- Plan: `docs/plans/2026-03-23-009-fix-cross-source-duplicate-prevention-plan.md`
- Related: `docs/solutions/integration-issues/adding-new-llm-provider.md` (also involved multi-layer sync gaps)
- Dedup detection UI: `scripts/job_web.py` (post-hoc modal still available for manual review)
