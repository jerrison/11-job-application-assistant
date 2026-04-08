# Backlog Sweep Proof Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make backlog sweep completion depend on per-job proof traces and repo-native review recording rather than generic ledger coverage.

**Architecture:** Add a small recorder layer that writes per-job trace and artifact-manifest files, teach `draft_web` to use it for Phase 3 browser review, then tighten the sweep checker and docs so only trace-backed rows satisfy the contract.

**Tech Stack:** Python 3.14, FastAPI, SQLite-backed repo state, TSV ledgers, pytest, `uv run python`

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `docs/templates/phase2-stopped-results-template.tsv` | Add required provenance columns to Phase 2 ledger rows |
| Modify | `docs/templates/phase3-draft-results-template.tsv` | Add required provenance columns to Phase 3 ledger rows |
| Create | `scripts/backlog_sweep_recorder.py` | Shared helper for validating snapshot rows, writing traces/manifests, and appending ledger rows |
| Create | `scripts/record_backlog_sweep_result.py` | CLI wrapper for repo-native backlog sweep result recording |
| Modify | `scripts/draft_web.py` | Add a browser-review action that records Phase 3 review proof |
| Modify | `scripts/check_backlog_sweep.py` | Reject rows without valid per-job traces, artifact manifests, and allowed evidence roots |
| Modify | `docs/backlog-sweep.md` | Document the stricter contract and anti-shortcut rules |
| Modify | `docs/runbooks/repeatable-backlog-sweep.md` | Strengthen the reusable prompt and operator workflow |
| Modify | `tests/test_check_backlog_sweep.py` | Cover stricter checker semantics |
| Modify | `tests/test_backlog_sweep_harness.py` | Keep init/verify harness expectations aligned with new ledger headers |
| Modify | `tests/test_draft_web.py` | Cover Phase 3 review recording from the browser surface |
| Create | `tests/test_backlog_sweep_recorder.py` | Cover recorder helper/CLI behavior |

### Task 1: Lock the new ledger contract in tests

**Files:**
- Modify: `tests/test_check_backlog_sweep.py`
- Modify: `tests/test_backlog_sweep_harness.py`
- Modify: `tests/test_draft_web.py`
- Create: `tests/test_backlog_sweep_recorder.py`

- [ ] Add red tests for new required ledger columns and valid trace-backed rows.
- [ ] Add a red test proving Phase 3 `reviewed_ready` fails without a browser-review trace.
- [ ] Add a red test proving shared/off-row evidence paths are rejected.
- [ ] Add a red test for recorder output and `draft_web` review recording.

### Task 2: Implement repo-native sweep result recording

**Files:**
- Create: `scripts/backlog_sweep_recorder.py`
- Create: `scripts/record_backlog_sweep_result.py`

- [ ] Add helper functions to load the active manifest, validate snapshot membership, gather current artifacts, write per-job trace JSON, write artifact-manifest JSON, and append a TSV row.
- [ ] Keep evidence roots restricted to the job `output_dir` or the recorder-owned per-job trace directory.
- [ ] Provide a small CLI for manual Phase 2/Phase 3 row recording.

### Task 3: Wire Phase 3 browser review into the recorder

**Files:**
- Modify: `scripts/draft_web.py`
- Modify: `tests/test_draft_web.py`

- [ ] Add a `mark-reviewed` endpoint and button path that records `reviewed_ready` through the shared recorder helper.
- [ ] Preserve the existing approve/reject/regenerate/reset behavior.
- [ ] Return the written trace paths in the API response for operator visibility.

### Task 4: Tighten the checker

**Files:**
- Modify: `scripts/check_backlog_sweep.py`
- Modify: `tests/test_check_backlog_sweep.py`

- [ ] Require provenance columns on latest result rows.
- [ ] Validate trace JSON and artifact-manifest JSON contents against the row.
- [ ] Enforce browser-review `handled_via` values for Phase 3 `reviewed_ready`.
- [ ] Validate `proof_generated_at_utc` and evidence path roots.

### Task 5: Harden docs and prompt

**Files:**
- Modify: `docs/backlog-sweep.md`
- Modify: `docs/runbooks/repeatable-backlog-sweep.md`

- [ ] Document the recorder command and the new required row fields.
- [ ] Add explicit “shortcuts that do not count” language.
- [ ] Replace the vague Phase 2 wording with row-by-row handling language.
- [ ] Require the repo-native review action for Phase 3 `reviewed_ready`.

### Task 6: Verify

**Files:**
- Modify: `.context/compound-engineering/todos/007-ready-p1-harden-backlog-sweep-proof-contract.md`

- [ ] Run targeted pytest coverage for the changed modules.
- [ ] Run `uv run ruff check scripts/ tests/`.
- [ ] Run broader repo verification if the targeted pass is clean and time permits.
- [ ] Record outcomes in the todo work log.
