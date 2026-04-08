# [Feature Name] Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

<!-- Goal: One sentence describing the user-facing outcome — what someone can do
     after this change that they could not do before. -->
**Goal:** [1-2 sentences: what someone can do after this change that they could not do before]

**Spec:** `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`

**Existing code:** [Key files and modules this plan touches]

---

## Purpose / Big Picture

<!-- Describe what someone gains after this change and how they can verify it.
     Keep this user-visible and outcome-oriented. -->

[What will exist after this lands, and how someone can observe it working.]

---

## Context and Orientation

<!-- Orient the next contributor quickly. Link the important docs, point at the
     code hotspots, and spell out any constraints they need before editing. -->

- **Docs to read:** `docs/...`
- **Primary files:** `path/to/file.py`, `path/to/other.py`
- **Constraints:** [Architecture, rollout, or safety constraints]

---

## Milestones

<!-- Milestones are narrative checkpoints. Each should be independently
     verifiable and explain what new thing exists at the end. -->

1. **Milestone 1:** [Scope, expected proof, and verification command]
2. **Milestone 2:** [Scope, expected proof, and verification command]

---

## Progress

<!-- Granular status tracker. Update timestamps as work proceeds so anyone can
     see at a glance what is done and what remains. -->

| Step | Status | Updated |
|------|--------|---------|
| Chunk 1 / Task 1 | Not started | |
| ... | | |

---

## File Structure

### New files:
- `path/to/new_file.py` — brief purpose

### Modified files:
- `path/to/existing.py` — what changes

---

## Chunks & Tasks

<!-- Organize as Chunk (theme) → Task (unit of work) → Steps (checkboxes).
     Each task should be independently testable. Each step is small enough to
     verify before moving on. Include test-first steps where applicable. -->

### Chunk 1: [Theme]

#### Task 1: [Unit of work]

**Files:**
- Create: `path/to/file.py`
- Modify: `path/to/other.py`

- [ ] **Step 1:** Write test for ...
- [ ] **Step 2:** Run test, verify it fails
- [ ] **Step 3:** Implement ...
- [ ] **Step 4:** Run test, verify it passes
- [ ] **Step 5:** Commit

#### Task 2: ...

- [ ] **Step 1:** ...

---

### Chunk 2: [Theme]

#### Task 3: ...

- [ ] **Step 1:** ...

---

## Surprises & Discoveries

<!-- Unexpected behaviors, bugs, or architectural insights found during
     implementation. Include evidence (error messages, file paths, test output). -->

- None yet.

---

## Decision Log

<!-- Every fork-in-the-road decision with date and rationale. Helps future
     readers understand why the code looks the way it does. -->

| Date | Decision | Rationale |
|------|----------|-----------|
| | | |

---

## Outcomes & Retrospective

<!-- Fill after completion. Captures what shipped, what was deferred, and
     lessons for the next plan. -->

- **Achieved:** ...
- **Remaining:** ...
- **Lessons:** ...
