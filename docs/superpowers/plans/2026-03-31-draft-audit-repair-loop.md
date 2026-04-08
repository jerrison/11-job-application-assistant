# Draft Audit Repair Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add repo-native draft/stopped auditing that retries repairable outcomes up to three times, preserves truthful terminal outcomes, and enforces full field accounting including optionals.

**Architecture:** Add a shared audit module that classifies current-attempt artifacts, integrate it into `process_job()` before rows settle as `draft` or `stopped`, and add a narrow worker sweep for rows that reach those states outside the normal path. Keep audit attempt accounting separate from existing transient retry counters.

**Tech Stack:** Python, sqlite3-backed job state, pytest, Ruff

---

### Task 1: Add Shared Audit State And Result Helpers

**Files:**
- Create: `scripts/pipeline_audit_loop.py`
- Modify: `scripts/job_db.py`
- Test: `tests/test_pipeline_audit_loop.py`

- [ ] **Step 1: Write the failing tests for audit classification and attempt accounting**

```python
def test_draft_audit_flags_unaccounted_optional_field(tmp_path):
    result = audit_draft_outcome(...)
    assert result.kind == "repairable"
    assert result.failure_type == "audit_failure"

def test_truthful_terminal_outcome_is_not_retryable(tmp_path):
    result = audit_stopped_outcome(...)
    assert result.kind == "terminal"

def test_audit_attempt_metrics_update_is_separate_from_fix_attempts(conn):
    ensure_job_metrics(conn, 1)
    update_job_metrics(conn, 1, audit_attempts=1)
    row = conn.execute("SELECT fix_attempts FROM jobs WHERE id = 1").fetchone()
    metrics = get_job_metrics(conn, 1)
    assert row["fix_attempts"] == 0
    assert metrics["audit_attempts"] == 1
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run python -m pytest tests/test_pipeline_audit_loop.py -v`
Expected: FAIL because the module and metric fields do not exist yet.

- [ ] **Step 3: Add the shared audit module and metric fields**

```python
# scripts/pipeline_audit_loop.py
@dataclass(frozen=True)
class AuditDecision:
    kind: str
    failure_type: str | None
    reason: str
    repair_actions: tuple[str, ...]
    artifacts: dict[str, str]

def audit_draft_outcome(...): ...
def audit_stopped_outcome(...): ...
def repairable_terminal_failure_type(failure_type: str | None) -> bool: ...
```

```python
# scripts/job_db.py
CREATE TABLE IF NOT EXISTS job_metrics (
    ...
    audit_attempts      INTEGER DEFAULT 0,
    audit_failure_count INTEGER DEFAULT 0,
    ...
)
```

- [ ] **Step 4: Run the focused tests again**

Run: `uv run python -m pytest tests/test_pipeline_audit_loop.py -v`
Expected: PASS

- [ ] **Step 5: Commit the shared audit primitives**

```bash
git add scripts/pipeline_audit_loop.py scripts/job_db.py tests/test_pipeline_audit_loop.py
git commit -m "feat: add shared draft audit loop primitives"
```

### Task 2: Enforce The Audit Loop In Pipeline Orchestration

**Files:**
- Modify: `scripts/pipeline_orchestrator.py`
- Modify: `scripts/pipeline_draft_proof.py`
- Test: `tests/test_pipeline_orchestrator.py`

- [ ] **Step 1: Write the failing orchestrator tests**

```python
def test_draft_audit_requeues_repairable_incomplete_draft(tmp_path, conn):
    status = process_job(conn, 1, auto_submit=False)
    assert status == "queued"

def test_draft_audit_exhausts_after_three_attempts(tmp_path, conn):
    ...
    assert row["status"] == "stopped"
    assert row["failure_type"] == "audit_failure"

def test_truthful_terminal_stopped_outcome_bypasses_audit_retry(tmp_path, conn):
    ...
    assert row["failure_type"] == "external_apply"
```

- [ ] **Step 2: Run the focused orchestrator tests to verify they fail**

Run: `uv run python -m pytest tests/test_pipeline_orchestrator.py -k "audit" -v`
Expected: FAIL because no audit loop is wired into `process_job()`.

- [ ] **Step 3: Integrate audit decisions into draft and stopped exits**

```python
# scripts/pipeline_orchestrator.py
decision = audit_draft_outcome(...)
if decision.kind == "repairable":
    return _schedule_audit_retry(...)
if decision.kind == "audit_failed":
    update_status(..., "stopped", failure_type="audit_failure")
```

```python
decision = audit_stopped_outcome(...)
if decision.kind == "repairable":
    return _schedule_audit_retry(...)
```

- [ ] **Step 4: Reuse shared proof helpers instead of duplicating artifact checks**

```python
# scripts/pipeline_draft_proof.py
def current_draft_field_audit_inputs(...): ...
```

- [ ] **Step 5: Run the focused orchestrator tests again**

Run: `uv run python -m pytest tests/test_pipeline_orchestrator.py -k "audit" -v`
Expected: PASS

- [ ] **Step 6: Commit the orchestrator integration**

```bash
git add scripts/pipeline_orchestrator.py scripts/pipeline_draft_proof.py tests/test_pipeline_orchestrator.py
git commit -m "feat: enforce draft audit repair loop in orchestrator"
```

### Task 3: Add Worker Catch-Up Sweep For External Draft/Stopped Rows

**Files:**
- Modify: `scripts/job_worker.py`
- Modify: `scripts/job_db.py`
- Modify: `scripts/pipeline_audit_loop.py`
- Test: `tests/test_job_worker.py`

- [ ] **Step 1: Write the failing worker sweep tests**

```python
def test_worker_sweep_requeues_recent_repairable_draft(conn, tmp_path):
    ...
    assert row["status"] == "queued"

def test_worker_sweep_skips_rows_with_final_audit_failure(conn, tmp_path):
    ...
    assert row["status"] == "stopped"
```

- [ ] **Step 2: Run the worker tests to verify they fail**

Run: `uv run python -m pytest tests/test_job_worker.py -k "audit" -v`
Expected: FAIL because no sweep exists.

- [ ] **Step 3: Implement the narrow audit sweep**

```python
# scripts/job_worker.py
def _audit_recent_terminal_rows(conn: sqlite3.Connection, *, limit: int = 10) -> None:
    ...
```

- [ ] **Step 4: Run the worker tests again**

Run: `uv run python -m pytest tests/test_job_worker.py -k "audit" -v`
Expected: PASS

- [ ] **Step 5: Commit the worker sweep**

```bash
git add scripts/job_worker.py scripts/job_db.py tests/test_job_worker.py
git commit -m "feat: add worker audit sweep for draft and stopped rows"
```

### Task 4: Write Human-Readable Audit Failure Reports

**Files:**
- Modify: `scripts/pipeline_audit_loop.py`
- Create: `output/_audit/.gitkeep`
- Test: `tests/test_pipeline_audit_loop.py`

- [ ] **Step 1: Write the failing markdown-report tests**

```python
def test_exhausted_audit_failure_writes_job_markdown_note(tmp_path):
    ...
    assert (submit_dir / "audit_failure.md").exists()

def test_exhausted_audit_failure_updates_repo_index(tmp_path):
    ...
    index_path = tmp_path / "output" / "_audit" / "active_audit_failures.md"
    assert index_path.exists()
```

- [ ] **Step 2: Run the focused report tests to verify they fail**

Run: `uv run python -m pytest tests/test_pipeline_audit_loop.py -k "audit_failure_markdown" -v`
Expected: FAIL because no markdown reporting exists.

- [ ] **Step 3: Implement per-job and rolling markdown output**

```python
# scripts/pipeline_audit_loop.py
def write_audit_failure_report(...): ...
def refresh_active_audit_failure_index(...): ...
```

- [ ] **Step 4: Run the focused report tests again**

Run: `uv run python -m pytest tests/test_pipeline_audit_loop.py -k "audit_failure_markdown" -v`
Expected: PASS

- [ ] **Step 5: Commit the markdown reporting**

```bash
git add scripts/pipeline_audit_loop.py output/_audit/.gitkeep tests/test_pipeline_audit_loop.py
git commit -m "feat: add human-readable audit failure reports"
```

### Task 5: Verify End-To-End And Requeue The Active Batch

**Files:**
- Modify: `docs/worker-pipeline-patterns.md`
- Modify: `agent_preferences.md`

- [ ] **Step 1: Update the docs to reflect the new audit policy**

```markdown
## Draft Completeness

Drafts now require full field accounting, including optionals. Repairable audit failures requeue up to three times before `audit_failure`.
```

- [ ] **Step 2: Run focused verification, then full verification**

Run: `uv run python -m pytest tests/test_pipeline_audit_loop.py tests/test_pipeline_orchestrator.py tests/test_job_worker.py -v`
Expected: PASS

Run: `uv run python -m pytest tests/ -v`
Expected: PASS

Run: `uv run ruff check scripts/ tests/`
Expected: PASS

Run: `uv run python scripts/check_architecture.py`
Expected: PASS

- [ ] **Step 3: Restart or continue the worker and requeue the active draft slice**

Run: `uv run bin/job-assets worker status`
Expected: worker running or a clear stopped state to restart.

Run: `uv run bin/job-assets retry --status stopped --status draft --not-archived --dry-run`
Expected: shows the repairable slice that the new audit loop will own.

- [ ] **Step 4: Commit docs and verification-driven cleanup**

```bash
git add docs/worker-pipeline-patterns.md agent_preferences.md
git commit -m "docs: describe draft audit repair loop"
```
