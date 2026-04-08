# Linear, Stopped, and Draft Sweep Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refresh the live queue truth, fix any remaining repairable stopped/draft regressions, redraft affected jobs with fresh proof, and leave Linear plus the repo in a self-contained state that another agent can resume without external context.

**Spec:** `docs/brainstorms/2026-03-25-pipeline-resilience-requirements.md`

**Existing code:** `scripts/pipeline_audit_loop.py`, `scripts/pipeline_orchestrator.py`, `scripts/job_db.py`, `scripts/submit_application.py`, board autofill adapters, `output/_audit/*`, Linear project `Job Application Automation`

---

## Purpose / Big Picture

This sweep treats the repo-local artifacts as the source of truth. The work starts by regenerating the current repairable-vs-terminal queue split, then samples representative stopped/draft rows to find real remaining bugs versus truthful manual blockers, then applies only generalized fixes with fresh reruns, screenshots, and Linear evidence.

---

## Context and Orientation

- **Docs to read:** `AGENTS.md`, `docs/operational-rules.md`, `docs/worker-pipeline-patterns.md`, `docs/solutions/workflow-issues/stopped-job-audits-must-be-repo-local-and-artifact-backed-2026-03-27.md`, `docs/solutions/workflow-issues/current-draft-proof-must-backfill-stale-root-answer-state-from-active-submit-artifacts-2026-04-05.md`, `docs/solutions/logic-errors/draft-proof-must-prefer-canonical-job-assets-and-exact-visible-field-values-2026-03-29.md`
- **Primary files:** `scripts/pipeline_audit_loop.py`, `scripts/pipeline_orchestrator.py`, `scripts/job_db.py`, `scripts/draft_manager.py`, `scripts/job_web.py`, `scripts/autofill_*.py`, `output/_audit/current_repairable_cohorts.json`
- **Constraints:** Always use `--draft`; fail closed at submit boundaries; screenshots are the source of truth; stale submit artifacts must be cleared before reruns; Linear evidence must be self-contained; if a blocker still needs the user, keep it parked and label it `requires-user-input`.

---

## Milestones

1. **Milestone 1:** Current queue truth is refreshed from the live repo and written back to the active audit/plan files. Verification: repo-local audit script output and updated plan progress.
2. **Milestone 2:** Any remaining repairable bug classes are reproduced from current artifacts, fixed with tests first, and generalized across boards/surfaces. Verification: targeted failing/passing tests plus representative rerun evidence.
3. **Milestone 3:** Affected jobs are redrafted with fresh screenshots, Linear issues are updated or created with self-contained proof, and verification/lint/CI are clean before handoff. Verification: rerun artifacts, Linear comments/issues, and project verification commands.

---

## Progress

| Step | Status | Updated |
|------|--------|---------|
| Refresh live queue + Linear scope | Completed | 2026-04-07 |
| Sample representative stopped/draft cohorts | Completed | 2026-04-07 |
| Implement generalized fixes with TDD | Completed | 2026-04-07 |
| Redraft affected jobs and capture proof | Completed | 2026-04-07 |
| Update Linear with self-contained evidence | Completed | 2026-04-07 |
| Run verification, sync data, and ship handoff | In progress | 2026-04-07 |

---

## File Structure

### New files:
- `docs/exec-plans/active/2026-04-07-linear-stopped-draft-sweep.md` — repo-local execution record for this sweep
- `output/_linear_evidence/*.png` — rendered evidence cards used for self-contained Linear proof

### Modified files:
- `output/_audit/current_repairable_cohorts.json` — refreshed live repairable-vs-terminal manifest
- `output/_audit/active_repair_failures.md` — live repair-cluster index if new repairable clusters are recorded
- `docs/solutions/...` — updated only if this sweep uncovers a new generalized learning
- `scripts/*.py`, `tests/*.py` — only if current sampling proves a new repairable bug class

---

## Chunks & Tasks

### Chunk 1: Refresh the Live Audit

#### Task 1: Confirm the current Linear/project scope and queue truth

**Files:**
- Modify: `docs/exec-plans/active/2026-04-07-linear-stopped-draft-sweep.md`
- Modify: `output/_audit/current_repairable_cohorts.json`

- [x] **Step 1:** Confirm whether any project issues are still open or parked in Linear.
- [x] **Step 2:** Recompute the live stopped/draft repairable split from `jobs.db` using `audit_stopped_outcome(...)` and `audit_draft_outcome(...)`.
- [x] **Step 3:** Record the current counts, representative clusters, and any delta from the stale April 4 manifest in this plan.

#### Task 2: Map the likely canaries

**Files:**
- Modify: `docs/exec-plans/active/2026-04-07-linear-stopped-draft-sweep.md`

- [x] **Step 1:** Pull representative rows for the largest current repairable stopped/draft clusters.
- [x] **Step 2:** Cross-reference them against recent NAD issues, existing solution docs, and current output directories.
- [x] **Step 3:** Separate truthful/manual blockers from candidate generalized bugs before any reruns.

---

### Chunk 2: Repair Only Real Bugs

#### Task 3: Reproduce and root-cause any current repairable bug classes

**Files:**
- Modify: `scripts/*.py`
- Modify: `tests/*.py`
- Modify: `docs/solutions/...` as needed

- [x] **Step 1:** Reproduce the current blocker from saved artifacts or a fresh canary rerun.
- [x] **Step 2:** Add or extend the smallest failing regression test that captures the bug.
- [x] **Step 3:** Implement the minimal generalized fix across boards/surfaces.
- [x] **Step 4:** Re-run the focused regression tests and confirm green.

#### Task 4: Refresh provider instructions and repo-local learnings for any new generalized rule

**Files:**
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: `GEMINI.md`
- Modify: `CODEX.md`
- Modify: `GPT.md`
- Modify: `.github/copilot-instructions.md`

- [ ] **Step 1:** Update `AGENTS.md` only if the fix changes a standing rule or learned preference.
- [ ] **Step 2:** Run `uv run python scripts/sync_agent_files.py`.
- [ ] **Step 3:** Confirm generated provider files are in sync.

---

### Chunk 3: Redraft, Evidence, and Handoff

#### Task 5: Redraft affected jobs and capture fresh proof

**Files:**
- Modify: job output dirs under `output/*/*`
- Modify: `output/_audit/current_repairable_cohorts.json`

- [x] **Step 1:** Clear stale current-attempt artifacts implicitly via the supported rerun path.
- [x] **Step 2:** Re-run affected jobs in `--draft` mode and inspect the fresh proof artifacts.
- [x] **Step 3:** Sync repo-local disk truth back into `jobs.db` and refresh the audit manifest.

#### Task 6: Update Linear and finish operational verification

**Files:**
- Modify: Linear issues/comments
- Modify: `docs/exec-plans/active/2026-04-07-linear-stopped-draft-sweep.md`

- [x] **Step 1:** Update or create NAD issues with self-contained before/after screenshots and detailed proof.
- [x] **Step 2:** Park any still-user-blocked items and label them `requires-user-input`.
- [ ] **Step 3:** Run verification commands, fix remaining lint/CI issues, then commit/push/merge and write the next-agent handoff.

---

## Surprises & Discoveries

- The initial repo-wide `draft_status.json` mismatch scan overstated queue risk because it included already-submitted jobs that still retain historical draft artifacts. Restricting the scan to current `jobs.db.status = 'draft'` shows the real live result: `draft_jobs = 335`, `mismatch_count = 0`.
- The refreshed `output/_audit/current_repairable_cohorts.json` now reflects the current queue truth: `draft:ready = 337`, `repairable.stopped = []`, and `repairable.draft = []`.
- New Linear issue coverage created during this pass:
  - `NAD-91` done: saved draft statuses refresh from live proof after review-rule changes
  - `NAD-92` done: Ashby graduate / years-of-experience select handling
  - `NAD-93` done: rate-limited blocker-shell `retries_exhausted` rows classify as `service_unavailable`
  - `NAD-94`..`NAD-97` parked with `requires-user-input`
- Existing parked user-input blockers still open: `NAD-87`, `NAD-88`, `NAD-89`.
- Representative repaired reruns:
  - `804` Rain now returns to `draft`
  - `802` Uniphore now returns to `draft`
  - `803` Redfin now returns to `draft`
- Truthful remaining manual / external blockers confirmed with evidence:
  - `787` ByteDance sign-in wall
  - `795` SmartRecruiters captcha
  - `1044` Joby iCIMS auth failure
  - `349` board-side blocker shell / rate limit requiring cooldown or direct JD text

---

## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-04-07 | Treat team-level NAD issues plus repo-local job artifacts as the source of truth. | Project-scoped summaries were incomplete; direct team-level issue inspection plus on-disk artifacts produced the reliable queue slice. |
| 2026-04-07 | Do not assume every stopped row is a bug. Recompute repairable vs terminal first. | The project already distinguishes truthful/manual states from repairable regressions, and the live queue was too large to triage blindly. |
| 2026-04-07 | Only treat saved-vs-live draft-status drift on current `status = draft` jobs as a queue-truth bug. | Submitted jobs can legitimately retain historical draft artifacts without affecting the current queue. |
| 2026-04-07 | Park remaining external/manual blockers in Linear with inline screenshots instead of forcing more automation. | The current blockers are sign-in, captcha, auth, or board-side rate limits, not repo-side defects. |

---

## Outcomes & Retrospective

- **Achieved:** Live queue truth refreshed; current repairable cohorts reduced to zero; generalized fixes landed for `NAD-91`, `NAD-92`, and `NAD-93`; canonical reruns regenerated fresh proof; new parked blockers documented in `NAD-94`..`NAD-97` with inline evidence; earlier parked `NAD-87`..`NAD-89` remain explicit and labeled.
- **Remaining:** Run the full repo verification commands, fix any failures, resolve any CI/lint fallout, then commit/push/merge and write the next-agent handoff command.
- **Lessons:** Queue-truth sweeps need to distinguish live draft rows from historical draft artifacts on submitted jobs; otherwise the saved-vs-live mismatch scan creates false alarms.
