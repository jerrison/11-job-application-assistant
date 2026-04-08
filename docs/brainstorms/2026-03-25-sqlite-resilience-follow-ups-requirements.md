---
date: 2026-03-25
topic: sqlite-resilience-follow-ups
---

# SQLite Resilience Follow-Ups

## Problem Frame

PR #34 hardened the web UI against DB corruption (global exception handler, timeline degradation, dual backup, integrity_check upgrade). Four follow-up items were identified by review agents but deferred to keep the PR focused. Each addresses a remaining gap in corruption resilience.

## Requirements

- R1. **TUI timeline error handling** — `job_tui.py` imports `get_job_timeline` (line 52) and calls it without error handling. If the events table is corrupted, the TUI crashes with an unhandled `DatabaseError`. Add a targeted try/except in the TUI's timeline-rendering widget, mirroring the pattern used in `job_web.py`'s `get_job_detail`.

- R2. **Full `draft_web.py` connection tracking** — PR #34 replaced `_open_db()` with `job_db.open_db()`, but `draft_web.py` still creates its own `FastAPI()` app without connection tracking or a global exception handler. Connections opened by `draft_web.py` are invisible to `close_all_connections()` in `job_web.py`. Add connection tracking (register/deregister via `job_web`'s `_connections` set) and a `DatabaseError` exception handler to `draft_web.py`.

- R3. **SQLite version upgrade past 3.50.7** — Python 3.14 bundles SQLite 3.50.4, which has a known WAL-reset data race (fixed in 3.50.7 / 3.51.3). This is a potential root cause for the recurring corruption. Upgrade via `pysqlite3` package with a manually-built SQLite 3.51.3+, or add `PRAGMA wal_autocheckpoint=4000` as a temporary mitigation to reduce WAL reset frequency.

- R4. **Per-table startup probe after integrity_check** — `integrity_check` validates B-tree structure but can miss scenarios where a specific table's root page is corrupted in a way that passes the check but fails on actual queries. After `integrity_check` passes in `init_db()`, probe every table with `SELECT COUNT(*) FROM {table}` (~1ms per table) to confirm readability. This would have caught the third corruption incident immediately.

## Success Criteria

- TUI does not crash when events table is corrupted (R1)
- `draft_web.py` connections are tracked and cleaned up on shutdown (R2)
- SQLite version is 3.50.7+ or WAL-reset mitigation is in place (R3)
- Startup detects per-table corruption that integrity_check misses (R4)

## Scope Boundaries

- R1-R4 are independent and can be implemented in any order or as separate commits
- No changes to the recovery script (`recover_db.py`) — that was completed in PR #34
- No changes to the web UI exception handler — already done in PR #34
- R3 (SQLite upgrade) may require investigation into `pysqlite3` compatibility; if too complex, the `wal_autocheckpoint` mitigation is acceptable

## Key Decisions

- **Middleware, not per-function:** Error handling pattern established in PR #34 — TUI and draft_web get their own surface-appropriate handlers, `job_db.py` stays clean
- **`pysqlite3` vs mitigation:** If `pysqlite3` integration is straightforward, prefer it. If it requires significant build tooling changes, use `wal_autocheckpoint=4000` as interim mitigation.

## Dependencies / Assumptions

- PR #34 is merged (confirmed — merged to main)
- `job_tui.py` uses Textual framework for widgets
- `draft_web.py` runs as a separate FastAPI app, not part of `job_web.py`

## Outstanding Questions

### Deferred to Planning

- [Affects R2][Technical] How should `draft_web.py` register connections with `job_web`'s tracking set? Import directly, or extract connection tracking to a shared module in `job_db.py`?
- [Affects R3][Needs research] Does `pysqlite3` work as a drop-in replacement with the existing `sqlite3` imports, or does it require import aliasing across the codebase?
- [Affects R4][Technical] Which tables should the startup probe check? All tables in `_SCHEMA`, or only the ones with known corruption history (`events`, `provider_runs`, `job_phase_durations`)?

## Next Steps

`/ce:plan` for structured implementation planning
