# Answer Verification Judge Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the provider-backed reference verifier for non-deterministic free-text answers and wire one verifier-guided regeneration pass into answer generation.

**Architecture:** Keep the existing root status and per-attempt artifact contract. Extend `scripts/answer_verifier.py` from a rule-only blocker into a mixed evaluator: deterministic and user-required lanes stay local, lane-2 questions call an LLM judge with structured JSON output and repo-local source material. `scripts/application_submit_common.py` will consume retry feedback once, regenerate provider answers once, rerun validation, and then fail closed if verifier blockers or retry findings remain.

**Tech Stack:** Python, subprocess-based LLM provider integration, JSON sidecar artifacts, unittest/pytest

---

### Task 1: Add Failing Verifier Tests For The Model Judge

**Files:**
- Modify: `tests/test_answer_verifier.py`

- [ ] **Step 1: Write failing tests for provider-backed verdicts**
- [ ] **Step 2: Verify the new tests fail**
- [ ] **Step 3: Implement minimal verifier-provider path**
- [ ] **Step 4: Re-run verifier tests to green**

### Task 2: Add Failing Submit-Flow Tests For Verifier-Guided Retry

**Files:**
- Modify: `tests/test_submit_application.py`

- [ ] **Step 1: Write failing tests for one retry and final fail-closed behavior**
- [ ] **Step 2: Verify the new tests fail**
- [ ] **Step 3: Implement retry orchestration in `generate_application_answers()`**
- [ ] **Step 4: Re-run submit-flow tests to green**

### Task 3: Surface Retryable Verifier Findings In Draft Review

**Files:**
- Modify: `scripts/draft_manager.py`
- Modify: `tests/test_draft_manager.py`

- [ ] **Step 1: Write failing summary test for retryable verifier findings**
- [ ] **Step 2: Verify the new test fails**
- [ ] **Step 3: Render retry feedback and source refs in the summary**
- [ ] **Step 4: Re-run summary tests to green**

### Task 4: Verify The Final Slice

**Files:**
- Verify only

- [ ] **Step 1: Run targeted verifier and submit tests**
- [ ] **Step 2: Run Ruff on touched files**
- [ ] **Step 3: Run doc-health checks**
