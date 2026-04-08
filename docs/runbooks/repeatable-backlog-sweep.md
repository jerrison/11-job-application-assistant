# Repeatable Backlog Sweep

Use this runbook when you want to rerun the Linear Todo + stopped-job + drafted-job review sweep against the **current** queue and **current** repo state, even if an older sweep already exists.

## Why This Is Repeatable

- `uv run python scripts/resume_or_start_backlog_sweep.py --active` resumes a valid active sweep or bootstraps a fresh active manifest
- `uv run python scripts/init_backlog_sweep.py --new-run` still exists when you intentionally want to replace the active sweep
- the active manifest records current git state and current job-status counts
- Phase 1 snapshots current Linear `Todo` issues into the same sweep contract as Phase 2 and Phase 3
- Phase 2 and Phase 3 snapshots are created only when those phases actually start
- `uv run python scripts/check_backlog_sweep.py --active` validates snapshot coverage, current-wave freshness, and synced Linear mirror state
- `uv run python scripts/verify_active_sweep.py --active` is the final completion gate

## Operator Commands

Before any sweep session:

```bash
uv run python scripts/resume_or_start_backlog_sweep.py --active
```

At the start of Phase 1, if the active manifest does not already include Phase 1 artifacts:

```bash
uv run python scripts/init_backlog_sweep.py --start-phase phase1
```

At the start of Phase 2:

```bash
uv run python scripts/init_backlog_sweep.py --start-phase phase2
```

At the start of Phase 3:

```bash
uv run python scripts/init_backlog_sweep.py --start-phase phase3
```

During the sweep:

```bash
uv run python scripts/check_backlog_sweep.py --active
```

Before claiming completion:

```bash
uv run python scripts/verify_active_sweep.py --active
```

To generate a fresh-session handoff prompt for the active sweep:

```bash
uv run python scripts/print_backlog_sweep_handoff.py --active
```

## Prompt

```text
$using-superpowers

Follow `AGENTS.md`, `docs/operational-rules.md`, and `docs/backlog-sweep.md`.
Always use `--draft`. Never auto-submit. Fail closed at the final review boundary.
Screenshots are the source of truth.
Use repo-native commands and `uv run python`, not bare `python`.
If you change agent/provider instructions, regenerate provider files.
You may use sub-agents and parallel work for independent Phase 2 and Phase 3 batches.
Use repo-native controller-backed commands or web actions for every handled snapshot row; do not hand-edit or bulk-backfill ledgers.

Before Phase 1, run:

`uv run python scripts/resume_or_start_backlog_sweep.py --active`

If the active manifest does not already define `phase1_snapshot` and `phase1_results`, run:

`uv run python scripts/init_backlog_sweep.py --start-phase phase1`

Then execute all three phases per the repo contracts, reusing existing active artifacts when resuming.

Phase 1:
- Phase 1 is a real snapshot phase. Only work counted against `phase1_snapshot` and `phase1_results` can satisfy Phase 1 completion.
- Fix every Linear issue currently in `Todo`.
- If an issue has `requires-user-input`, read the user comments and use that input.
- Default to shipping a fix, not just describing the issue. Reproduce the defect, implement the smallest generalized repair, rerun the canonical affected job in `--draft`, and only then mark the issue `Done`.
- Creating or updating another Linear issue does not count as fixing the current Linear issue.
- Mirror the latest repo truth back to Linear. A Phase 1 row does not count as complete unless the latest result is synced.
- If it still needs human action, leave it parked, keep `requires-user-input`, document the blocker, and continue.
- For every issue you fix, complete it end to end, generalize the fix across relevant boards and surfaces, rerun the canonical affected job in `--draft`, attach self-contained screenshot-backed evidence, and mark it `Done` only after verification.

Phase 2:
- At the start of Phase 2, run:

`uv run python scripts/init_backlog_sweep.py --start-phase phase2`

- Inspect each stopped snapshot row individually.
- Default to repair. If the stopped row looks repo-repairable, reproduce it, attempt a concrete generalized fix, and rerun the canonical job before recording the row.
- Do not convert a likely fixable defect into a parked Linear issue just because the first inspection explains it.
- Creating or updating a Linear issue does not satisfy Phase 2 handling by itself.
- Audits and helper scripts may help triage, but they do not count as handling a row.
- Record each handled row immediately through the repo-native sweep recorder.
- Exhaust the active stopped-job snapshot and keep the active Phase 2 ledger current.
- If later repo changes alter the repair wave, retry unresolved repair rows instead of treating old attempts as current.

Phase 3:
- At the start of Phase 3, run:

`uv run python scripts/init_backlog_sweep.py --start-phase phase3`

- Exhaust the active draft-review snapshot.
- Manually open every drafted job in the browser review surface, screenshot by screenshot.
- If a draft review reveals a wrong answer, missing answer, bad screenshot, stale artifact, or not-ready-to-submit state, treat that as a fix task first: implement the smallest generalized repair, redraft, and re-review before recording the row.
- Creating or updating a Linear issue does not satisfy Phase 3 handling by itself.
- Record `reviewed_ready` only through the browser review action or the repo-native sweep recorder with browser `handled_via`.
- Keep the active Phase 3 ledger current.
- If later repo changes alter the repair wave, retry unresolved repair rows instead of treating old attempts as current.

Shortcuts that do not count:
- bulk-populating Phase 2 or Phase 3 ledgers from audits, SQL, helper scripts, or existing artifacts
- using one shared batch evidence file for multiple snapshot rows
- treating snapshot creation, queue counts, rerun counts, or a failing verifier as completion of a phase

Progress reporting:
- After every 25 Phase 2 jobs reviewed, report progress from the active ledger.
- After every 25 Phase 3 jobs reviewed, report progress from the active ledger.

Use this as the fast coverage gate during the sweep:

`uv run python scripts/check_backlog_sweep.py --active`

Do not claim completion unless this full verifier passes:

`uv run python scripts/verify_active_sweep.py --active`

If the checker or verifier fails because rows remain uncovered, continue the sweep unless time or context is exhausted.

If anything remains, report `incomplete` and list every remaining snapshot row without a valid latest ledger row.

Before your final message, report:
- whether you resumed the active sweep or started a fresh one
- Linear `Todo` issues fixed: X
- Concrete repair attempts made: X
- Stopped jobs reviewed: X / Y
- Stopped jobs redrafted: X
- Drafted jobs manually reviewed in browser: X / Y
- Drafted jobs fixed/redrafted: X
- Jobs still blocked on human input: list every job ID and Linear issue ID
- Rows parked or left open without a code change: list every job/issue and the proof that the blocker is external, unsupported, or user-gated
- Remaining stopped snapshot jobs without valid latest ledger rows: list every job ID and reason, or `none`
- Remaining drafted snapshot jobs without valid latest ledger rows: list every job ID and reason, or `none`
- NAD issues created/updated: list every issue ID
- Final status: `complete` or `incomplete`
```
