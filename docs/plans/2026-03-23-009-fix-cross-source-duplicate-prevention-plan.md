---
title: "fix: Prevent cross-source duplicate jobs at import time"
type: fix
status: completed
date: 2026-03-23
---

# fix: Prevent cross-source duplicate jobs at import time

## Problem

The same job appears in the queue 2-3 times when imported from different sources. Example: Stampli sr-pm-procurement was imported via company careers page, Greenhouse board, AND LinkedIn — all with different URLs. The URL-based dedup (`canonical_url` uniqueness) can't catch these because the URLs are genuinely different.

25 duplicate groups found with 75 extra jobs archived in cleanup.

## Root Cause

`add_job()` in `job_db.py` deduplicates on `canonical_url` only. Jobs from different sources (LinkedIn, Greenhouse, company site) have different canonical URLs for the same position. The system has no company+role-level dedup.

## Proposed Solution

Add a **fuzzy company+role dedup check** at import time, alongside the existing URL dedup.

### Implementation

#### 1. Add `_is_duplicate_by_company_role()` to `job_db.py`

Before inserting a new job, check if a job with the same company+role already exists:

```python
def _is_duplicate_by_company_role(conn, company: str, role_title: str) -> int | None:
    """Return the ID of an existing job matching company+role, or None."""
    if not company or not role_title:
        return None
    row = conn.execute(
        "SELECT id FROM jobs WHERE LOWER(company) = LOWER(?) AND LOWER(role_title) = LOWER(?) "
        "AND status NOT IN ('archived') LIMIT 1",
        (company, role_title),
    ).fetchone()
    return row[0] if row else None
```

#### 2. Check in `add_job()` before INSERT

```python
existing_id = _is_duplicate_by_company_role(conn, company, role_title)
if existing_id is not None:
    log.info("Duplicate of job #%d (same company+role), skipping", existing_id)
    return -existing_id  # negative ID signals duplicate to callers
```

**Caller convention:** Negative return = duplicate was found, `abs(return)` = existing job ID. Callers (web endpoint, import scripts) can check `result < 0` and report "duplicate skipped" to the user instead of "job added."

#### 3. Handle edge cases

- **company is None**: Skip the check (broken imports, handled separately)
- **Same company, different roles**: Not duplicates (different role_title)
- **Same role at different companies**: Not duplicates (different company)
- **Resubmission after archive**: If the user archived a job and wants to re-apply, the check should skip archived jobs (already handled by `status NOT IN ('archived')`)

## Acceptance Criteria

- [ ] Importing a LinkedIn URL for a job already queued via Greenhouse is detected and blocked
- [ ] The existing job ID is returned (not a new row created)
- [ ] Truly different jobs at the same company are not blocked
- [ ] Archived jobs don't block new imports of the same position
- [ ] Null-company jobs don't trigger false matches
- [ ] Existing tests pass

## Sources

- `add_job()`: `scripts/job_db.py:306`
- URL dedup: `canonical_url` column uniqueness check
- Duplicate detection modal: `scripts/job_web.py` (post-hoc detection)
