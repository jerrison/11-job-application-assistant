# Repair Rollout Monitoring And Pause-Confirm-Revert Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add active repair rollout monitoring that records repo-local rollout state, pauses fresh queued work when a promoted repair appears to regress comparable jobs, confirms once before rollback, auto-reverts confirmed bad repairs on `origin/main`, and exposes rollout-pause state through worker status surfaces.

**Architecture:** Introduce a focused `repair_rollouts.py` helper for runtime flags, active rollout persistence, comparable-cohort metrics, regression evaluation, and markdown reporting. Keep `repair_supervisor.py` as the orchestrator for promotion, monitoring, pause-confirm logic, and revert sequencing, while `job_db.py` owns queue gating and `repair_git.py` owns the non-destructive revert push.

**Tech Stack:** Python, sqlite3, git worktrees/helpers, FastAPI, pytest, Ruff

**Spec:** `docs/superpowers/specs/2026-04-01-repair-rollout-monitoring-and-pause-confirm-revert-design.md`

**Existing code:** `scripts/job_db.py`, `scripts/repair_supervisor.py`, `scripts/repair_git.py`, `scripts/job_web.py`, `tests/test_job_db.py`, `tests/test_repair_supervisor.py`, `tests/test_job_web.py`

---

## File Map

| File | Responsibility |
| --- | --- |
| `scripts/job_db.py` | Runtime-flag helpers, repair queue pause helpers, and queue gating in `get_pending_jobs()` |
| `scripts/repair_rollouts.py` | Active rollout persistence, comparable metrics, regression evaluation, pause-state transitions, and rollout markdown refresh |
| `scripts/repair_git.py` | Non-destructive revert helper for promoted repair commits |
| `scripts/repair_supervisor.py` | Record active rollouts on promotion, monitor active rollouts before new repairs, orchestrate pause-confirm-revert |
| `scripts/job_web.py` | Surface rollout-pause state in worker status and websocket payloads |
| `tests/test_job_db.py` | Runtime-flag and paused-queue selection coverage |
| `tests/test_repair_rollouts.py` | Rollout helper persistence, evaluation, and markdown-report coverage |
| `tests/test_repair_supervisor.py` | Supervisor promotion, pause-confirm, and revert orchestration coverage |
| `tests/test_job_web.py` | Worker-status/websocket rollout-pause coverage |

## Decomposition Notes

- Keep `repair_rollouts.py` pure with respect to orchestration. It may query/update SQLite and write markdown, but it should not spawn git commands or rerun jobs.
- Use `runtime_flags` for repo-native pause state instead of a second process-local mutex or JSON sidecar.
- Because `jobs` rows do not persist a dedicated phase column, comparable rollout metrics should use board-scoped post-fix job counts plus exact repair-fingerprint recurrence from `repair_clusters`.
- Split rollout-helper tests into `tests/test_repair_rollouts.py` so rollout logic can be verified without monkeypatch-heavy supervisor fixtures.

---

### Task 1: Add runtime flag helpers and pause-aware queue selection

**Files:**
- Modify: `scripts/job_db.py`
- Test: `tests/test_job_db.py`

- [ ] **Step 1: Write the failing runtime-flag and queue-pause tests**

Add these imports to `tests/test_job_db.py`:

```python
from job_db import (
    clear_repair_queue_pause,
    get_repair_queue_pause,
    get_runtime_flag_json,
    set_repair_queue_pause,
    set_runtime_flag,
)
```

Then add:

```python
def test_runtime_flag_round_trips_json_payload(db):
    payload = {"rollout_id": 7, "reason": "fingerprint_recurred"}

    set_runtime_flag(db, "repair_pause_new_queued_work", payload)

    assert get_runtime_flag_json(db, "repair_pause_new_queued_work") == payload


def test_get_pending_jobs_skips_fresh_queue_when_repair_pause_active(db):
    queued_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/queued")
    queued_submit_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/queued-submit")
    approved_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/approved")
    update_status(db, queued_submit_id, "queued_submit")
    update_status(db, approved_id, "approved")

    set_repair_queue_pause(
        db,
        rollout_id=3,
        commit_sha="abc1234",
        cluster_id=1,
        fingerprint="greenhouse:draft_audit:rendered_audit_mismatch:work-auth",
        reason="fingerprint_recurred",
    )

    pending = get_pending_jobs(db, limit=10)

    assert queued_id not in [job["id"] for job in pending]
    assert queued_submit_id not in [job["id"] for job in pending]
    assert [job["id"] for job in pending] == [approved_id]


def test_get_pending_jobs_allows_in_flight_submit_states_when_repair_pause_active(db):
    submitting_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/submitting")
    reanswering_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/reanswering")
    regenerating_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/regenerating")
    update_status(db, submitting_id, "submitting")
    update_status(db, reanswering_id, "reanswering")
    update_status(db, regenerating_id, "regenerating")

    set_repair_queue_pause(
        db,
        rollout_id=4,
        commit_sha="feedface",
        cluster_id=2,
        fingerprint="shared:draft_audit:rendered_audit_mismatch:work-auth",
        reason="rendered_audit_regressed",
    )

    pending = get_pending_jobs(db, limit=10)

    assert [job["id"] for job in pending] == [submitting_id, reanswering_id, regenerating_id]
    assert get_repair_queue_pause(db)["rollout_id"] == 4

    clear_repair_queue_pause(db)
    assert get_repair_queue_pause(db) is None
```

- [ ] **Step 2: Run the targeted DB tests and verify they fail**

Run:

```bash
uv run python -m pytest tests/test_job_db.py -k "runtime_flag or repair_pause" -v
```

Expected:
- `ImportError` or `AttributeError` for the new runtime-flag helpers
- `FAIL` because `get_pending_jobs()` still returns `queued` / `queued_submit` rows while paused

- [ ] **Step 3: Implement runtime-flag helpers and queue gating**

In `scripts/job_db.py`, add these helpers near the existing repair-rollout helpers:

```python
_REPAIR_QUEUE_PAUSE_FLAG = "repair_pause_new_queued_work"


def set_runtime_flag(conn: sqlite3.Connection, key: str, value: dict | list | str) -> None:
    serialized = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    conn.execute(
        """
        INSERT INTO runtime_flags (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = CURRENT_TIMESTAMP
        """,
        (str(key).strip(), serialized),
    )
    conn.commit()


def get_runtime_flag(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM runtime_flags WHERE key = ?", (str(key).strip(),)).fetchone()
    return str(row["value"]) if row else None


def get_runtime_flag_json(conn: sqlite3.Connection, key: str) -> dict | list | None:
    raw = get_runtime_flag(conn, key)
    if raw is None:
        return None
    return json.loads(raw)


def delete_runtime_flag(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM runtime_flags WHERE key = ?", (str(key).strip(),))
    conn.commit()


def set_repair_queue_pause(
    conn: sqlite3.Connection,
    *,
    rollout_id: int,
    commit_sha: str,
    cluster_id: int,
    fingerprint: str,
    reason: str,
) -> None:
    set_runtime_flag(
        conn,
        _REPAIR_QUEUE_PAUSE_FLAG,
        {
            "rollout_id": int(rollout_id),
            "commit_sha": str(commit_sha),
            "cluster_id": int(cluster_id),
            "fingerprint": str(fingerprint),
            "reason": str(reason),
            "paused_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def get_repair_queue_pause(conn: sqlite3.Connection) -> dict | None:
    payload = get_runtime_flag_json(conn, _REPAIR_QUEUE_PAUSE_FLAG)
    return payload if isinstance(payload, dict) else None


def clear_repair_queue_pause(conn: sqlite3.Connection) -> None:
    delete_runtime_flag(conn, _REPAIR_QUEUE_PAUSE_FLAG)
```

Then make `get_pending_jobs()` pause-aware after it builds the `result` list:

```python
    pause = get_repair_queue_pause(conn)
    if pause is not None:
        result = [row for row in result if row.get("status") not in QUEUE_QUEUED_STATUSES]
```

Ensure `scripts/job_db.py` imports:

```python
from datetime import datetime, timezone
```

- [ ] **Step 4: Re-run the targeted DB tests**

Run:

```bash
uv run python -m pytest tests/test_job_db.py -k "runtime_flag or repair_pause" -v
```

Expected:
- All three new tests `PASS`

- [ ] **Step 5: Commit the runtime-flag slice**

```bash
git add scripts/job_db.py tests/test_job_db.py
git commit -m "feat: add repair rollout pause flags"
```

---

### Task 2: Create rollout persistence, evaluation, and markdown reporting helpers

**Files:**
- Create: `scripts/repair_rollouts.py`
- Create: `tests/test_repair_rollouts.py`

- [ ] **Step 1: Write the failing rollout-helper tests**

Create `tests/test_repair_rollouts.py` with:

```python
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from job_db import init_db
from repair_rollouts import (
    evaluate_rollout,
    list_active_rollouts,
    record_active_rollout,
    refresh_active_repair_rollouts_index,
)


def test_record_active_rollout_persists_metadata_and_writes_index(tmp_path):
    db_path = tmp_path / "jobs.db"
    output_root = tmp_path / "output"
    with init_db(db_path) as conn:
        conn.execute(
            "INSERT INTO repair_clusters (id, fingerprint, status, eligibility, representative_job_ids, latest_summary) "
            "VALUES (1, 'greenhouse:draft_audit:rendered_audit_mismatch:work-auth', 'open', "
            "'auto_repair_candidate', '[42, 43]', 'Work authorization mismatch')"
        )
        rollout_id = record_active_rollout(
            conn,
            cluster_id=1,
            commit_sha="abc1234",
            fingerprint="greenhouse:draft_audit:rendered_audit_mismatch:work-auth",
            touched_files=["scripts/autofill_greenhouse.py", "scripts/repair_supervisor.py"],
            monitored_job_ids=[42, 43],
            output_root=output_root,
        )
        row = conn.execute("SELECT * FROM repair_rollouts WHERE id = ?", (rollout_id,)).fetchone()

    baseline = json.loads(row["baseline_metrics_json"])
    assert row["status"] == "active"
    assert baseline["fingerprint"] == "greenhouse:draft_audit:rendered_audit_mismatch:work-auth"
    assert baseline["board"] == "greenhouse"
    assert baseline["phase"] == "draft_audit"
    assert baseline["monitored_job_ids"] == [42, 43]
    index_text = (output_root / "_audit" / "active_repair_rollouts.md").read_text(encoding="utf-8")
    assert "abc1234" in index_text
    assert "greenhouse:draft_audit:rendered_audit_mismatch:work-auth" in index_text


def test_evaluate_rollout_requests_pause_when_fingerprint_reappears(tmp_path):
    db_path = tmp_path / "jobs.db"
    with init_db(db_path) as conn:
        conn.execute(
            "INSERT INTO repair_clusters (id, fingerprint, status, eligibility, representative_job_ids, latest_summary, created_at, updated_at) "
            "VALUES (1, 'greenhouse:draft_audit:rendered_audit_mismatch:work-auth', 'open', "
            "'auto_repair_candidate', '[42]', 'Work authorization mismatch', "
            "'2026-04-01 00:00:00', '2026-04-01 00:00:00')"
        )
        rollout_id = record_active_rollout(
            conn,
            cluster_id=1,
            commit_sha="abc1234",
            fingerprint="greenhouse:draft_audit:rendered_audit_mismatch:work-auth",
            touched_files=["scripts/autofill_greenhouse.py"],
            monitored_job_ids=[42],
            output_root=tmp_path / "output",
        )
        conn.execute(
            "INSERT INTO repair_clusters (fingerprint, status, eligibility, representative_job_ids, latest_summary, created_at, updated_at) "
            "VALUES (?, 'open', 'auto_repair_candidate', '[99]', 'recurrence', '2026-04-01 00:10:00', '2026-04-01 00:10:00')",
            ("greenhouse:draft_audit:rendered_audit_mismatch:work-auth",),
        )
        conn.commit()
        rollout = list_active_rollouts(conn)[0]
        evaluation = evaluate_rollout(conn, rollout, confirmation=False)

    assert evaluation.action == "pause"
    assert evaluation.reason == "fingerprint_recurred"
    assert evaluation.post_fix_metrics["fingerprint_recurrences"] == 1


def test_refresh_active_repair_rollouts_index_reports_empty_state(tmp_path):
    output_root = tmp_path / "output"
    index_path = refresh_active_repair_rollouts_index(output_root=output_root, rollouts=[])

    assert index_path.read_text(encoding="utf-8").strip().endswith("No active repair rollouts.")
```

- [ ] **Step 2: Run the rollout-helper tests to verify they fail**

Run:

```bash
uv run python -m pytest tests/test_repair_rollouts.py -v
```

Expected:
- `ImportError` because `scripts/repair_rollouts.py` does not exist yet

- [ ] **Step 3: Implement `repair_rollouts.py`**

Create `scripts/repair_rollouts.py` with:

```python
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from job_db import clear_repair_queue_pause, record_repair_rollout, set_repair_queue_pause

_ACTIVE_ROLLOUT_STATUSES = frozenset({"active", "paused_pending_confirmation", "monitoring_resumed"})
_NON_REGRESSION_FAILURE_TYPES = frozenset(
    {
        "already_applied",
        "auth_failed",
        "auth_guarded",
        "auth_unknown",
        "external_apply",
        "job_closed",
        "pending_user_input",
        "skipped_captcha",
        "unsupported",
        "user_rejected",
        "user_stopped",
    }
)


@dataclass(frozen=True)
class RolloutEvaluation:
    action: str
    reason: str
    post_fix_metrics: dict[str, object]


def _scope_from_fingerprint(fingerprint: str) -> tuple[str, str]:
    board, phase, *_ = (str(fingerprint or "").split(":") + ["unknown", "unknown"])
    return board or "unknown", phase or "unknown"


def record_active_rollout(
    conn: sqlite3.Connection,
    *,
    cluster_id: int,
    commit_sha: str,
    fingerprint: str,
    touched_files: list[str],
    monitored_job_ids: list[int],
    output_root: Path,
) -> int:
    board, phase = _scope_from_fingerprint(fingerprint)
    rollout_id = record_repair_rollout(
        conn,
        cluster_id,
        commit_sha=commit_sha,
        status="active",
        baseline_metrics_json={
            "fingerprint": fingerprint,
            "board": board,
            "phase": phase,
            "touched_files": list(dict.fromkeys(touched_files)),
            "monitored_job_ids": list(dict.fromkeys(int(job_id) for job_id in monitored_job_ids)),
            "fingerprint_recurrences": 0,
            "unexpected_hard_failures": 0,
        },
    )
    refresh_active_repair_rollouts_index(output_root=output_root, rollouts=list_active_rollouts(conn))
    return rollout_id


def list_active_rollouts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT *
        FROM repair_rollouts
        WHERE status IN ('active', 'paused_pending_confirmation', 'monitoring_resumed')
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def update_rollout_status(
    conn: sqlite3.Connection,
    rollout_id: int,
    *,
    status: str,
    post_fix_metrics: dict[str, object],
    output_root: Path,
    revert_sha: str | None = None,
) -> None:
    row = conn.execute("SELECT baseline_metrics_json FROM repair_rollouts WHERE id = ?", (int(rollout_id),)).fetchone()
    baseline = json.loads(str(row["baseline_metrics_json"] or "{}")) if row else {}
    conn.execute(
        """
        UPDATE repair_rollouts
        SET status = ?,
            post_fix_metrics_json = ?,
            revert_sha = COALESCE(?, revert_sha),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            str(status),
            json.dumps(post_fix_metrics, sort_keys=True),
            revert_sha,
            int(rollout_id),
        ),
    )
    conn.commit()
    refresh_active_repair_rollouts_index(output_root=output_root, rollouts=list_active_rollouts(conn))


def evaluate_rollout(conn: sqlite3.Connection, rollout: dict, *, confirmation: bool) -> RolloutEvaluation:
    baseline = json.loads(str(rollout.get("baseline_metrics_json") or "{}"))
    fingerprint = str(baseline.get("fingerprint") or "")
    board = str(baseline.get("board") or "unknown")
    created_at = str(rollout.get("created_at") or "")

    recurrence_row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM repair_clusters
        WHERE fingerprint = ?
          AND created_at > ?
        """,
        (fingerprint, created_at),
    ).fetchone()
    hard_failure_row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM jobs
        WHERE board = ?
          AND updated_at > ?
          AND status = 'stopped'
          AND COALESCE(failure_type, '') NOT IN ({})
        """.format(", ".join(f"'{value}'" for value in sorted(_NON_REGRESSION_FAILURE_TYPES))),
        (board, created_at),
    ).fetchone()

    metrics = {
        "fingerprint_recurrences": int(recurrence_row["count"] or 0),
        "unexpected_hard_failures": int(hard_failure_row["count"] or 0),
    }
    if metrics["fingerprint_recurrences"] > 0:
        return RolloutEvaluation(
            action="revert" if confirmation else "pause",
            reason="fingerprint_recurred",
            post_fix_metrics=metrics,
        )
    if metrics["unexpected_hard_failures"] > 0:
        return RolloutEvaluation(
            action="revert" if confirmation else "pause",
            reason="unexpected_hard_failures",
            post_fix_metrics=metrics,
        )
    return RolloutEvaluation(
        action="clear" if confirmation else "monitor",
        reason="no_regression_signal",
        post_fix_metrics=metrics,
    )


def refresh_active_repair_rollouts_index(*, output_root: Path, rollouts: list[dict]) -> Path:
    audit_dir = Path(output_root) / "_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    index_path = audit_dir / "active_repair_rollouts.md"
    lines = ["# Active Repair Rollouts", ""]
    if not rollouts:
        lines.append("No active repair rollouts.")
    else:
        for rollout in rollouts:
            baseline = json.loads(str(rollout.get("baseline_metrics_json") or "{}"))
            lines.append(
                f"- rollout `{rollout['id']}` commit `{rollout['commit_sha']}` "
                f"status `{rollout['status']}` scope `{baseline.get('board')}:{baseline.get('phase')}` "
                f"fingerprint `{baseline.get('fingerprint')}`"
            )
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index_path
```

- [ ] **Step 4: Re-run the rollout-helper tests**

Run:

```bash
uv run python -m pytest tests/test_repair_rollouts.py -v
```

Expected:
- All three rollout-helper tests `PASS`

- [ ] **Step 5: Commit the rollout-helper slice**

```bash
git add scripts/repair_rollouts.py tests/test_repair_rollouts.py
git commit -m "feat: add repair rollout monitoring helpers"
```

---

### Task 3: Integrate supervisor promotion, pause-confirm logic, and revert flow

**Files:**
- Modify: `scripts/repair_supervisor.py`
- Modify: `scripts/repair_git.py`
- Modify: `tests/test_repair_supervisor.py`

- [ ] **Step 1: Write the failing supervisor/revert tests**

In `tests/test_repair_supervisor.py`, add imports for `repair_rollouts` and then add:

```python
def test_promote_repair_candidate_records_active_rollout(monkeypatch, tmp_path):
    db_path = tmp_path / "jobs.db"
    with init_db(db_path) as conn:
        conn.execute(
            "INSERT INTO repair_clusters (id, fingerprint, status, eligibility, representative_job_ids, latest_summary) "
            "VALUES (1, 'greenhouse:draft_audit:rendered_audit_mismatch:work-auth', 'open', "
            "'auto_repair_candidate', '[42, 43]', 'Work authorization mismatch')"
        )
        conn.commit()

    supervisor = RepairSupervisor(project_root=tmp_path, db_path=db_path)
    rollout_calls: list[dict] = []
    monkeypatch.setattr(
        supervisor,
        "_run_canary_jobs",
        lambda *_: CanaryOutcome(ok=True, job_ids=[42], rerun_statuses={42: "draft"}),
    )
    monkeypatch.setattr(supervisor, "_push_main", lambda sha: None)
    monkeypatch.setattr(supervisor, "_sync_runtime_repo", lambda sha: None)
    monkeypatch.setattr(supervisor, "_requeue_jobs", lambda job_ids: [43])
    monkeypatch.setattr(
        repair_supervisor.repair_rollouts,
        "record_active_rollout",
        lambda conn, **kwargs: rollout_calls.append(kwargs) or 7,
    )

    candidate = PromotedRepair(
        pre_sha="deadbeef",
        promoted_sha="abc1234",
        cluster_id=1,
        job_ids=[42, 43],
        worktree_path=tmp_path / "repair-worktree",
        fingerprint="greenhouse:draft_audit:rendered_audit_mismatch:work-auth",
    )

    result = supervisor._promote_repair_candidate(candidate)

    assert result.status == "promoted"
    assert rollout_calls[0]["commit_sha"] == "abc1234"
    assert rollout_calls[0]["monitored_job_ids"] == [42, 43]


def test_monitor_active_rollouts_pauses_before_starting_new_repairs(monkeypatch, tmp_path):
    db_path = tmp_path / "jobs.db"
    with init_db(db_path) as conn:
        conn.execute(
            "INSERT INTO repair_clusters (id, fingerprint, status, eligibility, representative_job_ids, latest_summary) "
            "VALUES (1, 'greenhouse:draft_audit:rendered_audit_mismatch:work-auth', 'open', "
            "'auto_repair_candidate', '[42]', 'Work authorization mismatch')"
        )
        conn.execute(
            "INSERT INTO repair_rollouts (cluster_id, commit_sha, status, baseline_metrics_json) "
            "VALUES (1, 'abc1234', 'active', '{\"fingerprint\": \"greenhouse:draft_audit:rendered_audit_mismatch:work-auth\", "
            "\"board\": \"greenhouse\", \"phase\": \"draft_audit\", \"monitored_job_ids\": [42]}')"
        )
        conn.commit()

    supervisor = RepairSupervisor(project_root=tmp_path, db_path=db_path)
    paused: list[str] = []
    monkeypatch.setattr(
        repair_supervisor.repair_rollouts,
        "evaluate_rollout",
        lambda *_args, **_kwargs: repair_rollouts.RolloutEvaluation(
            action="pause",
            reason="fingerprint_recurred",
            post_fix_metrics={"fingerprint_recurrences": 1},
        ),
    )
    monkeypatch.setattr(repair_supervisor.repair_rollouts, "set_repair_queue_pause", lambda *_args, **_kwargs: paused.append("paused"))
    monkeypatch.setattr(supervisor, "_attempt_cluster_repair", lambda **_: (_ for _ in ()).throw(AssertionError("repair should not start")))

    handled = supervisor.run_once()

    assert handled is True
    assert paused == ["paused"]


def test_monitor_active_rollouts_reverts_after_confirmed_regression(monkeypatch, tmp_path):
    db_path = tmp_path / "jobs.db"
    with init_db(db_path) as conn:
        conn.execute(
            "INSERT INTO repair_clusters (id, fingerprint, status, eligibility, representative_job_ids, latest_summary) "
            "VALUES (1, 'greenhouse:draft_audit:rendered_audit_mismatch:work-auth', 'open', "
            "'auto_repair_candidate', '[42, 43]', 'Work authorization mismatch')"
        )
        conn.execute(
            "INSERT INTO repair_rollouts (cluster_id, commit_sha, status, baseline_metrics_json) "
            "VALUES (1, 'abc1234', 'paused_pending_confirmation', '{\"fingerprint\": \"greenhouse:draft_audit:rendered_audit_mismatch:work-auth\", "
            "\"board\": \"greenhouse\", \"phase\": \"draft_audit\", \"monitored_job_ids\": [42, 43]}')"
        )
        conn.commit()

    supervisor = RepairSupervisor(project_root=tmp_path, db_path=db_path)
    monkeypatch.setattr(
        repair_supervisor.repair_rollouts,
        "evaluate_rollout",
        lambda *_args, **_kwargs: repair_rollouts.RolloutEvaluation(
            action="revert",
            reason="fingerprint_recurred",
            post_fix_metrics={"fingerprint_recurrences": 2},
        ),
    )
    reverted: list[str] = []
    synced: list[str] = []
    requeued: list[list[int]] = []
    monkeypatch.setattr(supervisor, "_revert_main", lambda sha: reverted.append(sha) or "revert5678")
    monkeypatch.setattr(supervisor, "_sync_runtime_repo", lambda sha: synced.append(sha))
    monkeypatch.setattr(supervisor, "_requeue_jobs", lambda job_ids: requeued.append(list(job_ids)) or list(job_ids))

    handled = supervisor.run_once()

    assert handled is True
    assert reverted == ["abc1234"]
    assert synced == ["revert5678"]
    assert requeued == [[42, 43]]
```

- [ ] **Step 2: Run the supervisor tests and verify they fail**

Run:

```bash
uv run python -m pytest tests/test_repair_supervisor.py -k "rollout or revert" -v
```

Expected:
- `FAIL` because `record_active_rollout`, `_revert_main`, and rollout-monitor orchestration are not wired in yet

- [ ] **Step 3: Implement `revert_main()` and supervisor rollout orchestration**

In `scripts/repair_git.py`, add:

```python
def revert_main(project_root: Path, promoted_sha: str) -> str:
    verified_origin_main_sha(project_root)
    worktree = create_detached_verification_worktree(
        project_root=project_root,
        ref=_ORIGIN_MAIN_REF,
        cluster_fingerprint=promoted_sha,
        label="repair-revert",
    )
    try:
        _run_git(worktree.path, ["revert", "--no-edit", promoted_sha])
        revert_sha = read_ref_sha(worktree.path, "HEAD")
        _run_git(worktree.path, ["push", "origin", f"{revert_sha}:main"])
        return revert_sha
    finally:
        cleanup_repair_worktree(worktree)
```

Then in `scripts/repair_supervisor.py`:

```python
import repair_rollouts
from repair_git import revert_main
```

Add the helper wrappers:

```python
    def _revert_main(self, promoted_sha: str) -> str:
        return revert_main(self.project_root, promoted_sha)

    @property
    def _output_root(self) -> Path:
        return self.project_root / "output"
```

Record active rollouts during promotion:

```python
    def _promote_repair_candidate(self, candidate: PromotedRepair) -> RepairAttemptResult:
        canary = self._run_canary_jobs(candidate, candidate.job_ids[:3])
        if not canary.ok:
            self._rollback_local_promotion(candidate)
            return RepairAttemptResult(status="failed", reason="canary_failed")
        self._push_main(candidate.promoted_sha)
        self._sync_runtime_repo(candidate.promoted_sha)
        with init_db(self.db_path) as conn:
            repair_rollouts.record_active_rollout(
                conn,
                cluster_id=candidate.cluster_id,
                commit_sha=candidate.promoted_sha,
                fingerprint=candidate.fingerprint,
                touched_files=["scripts/repair_supervisor.py"],
                monitored_job_ids=list(candidate.job_ids),
                output_root=self._output_root,
            )
        canary_job_ids = set(canary.job_ids)
        rerun_job_ids = self._requeue_jobs([job_id for job_id in candidate.job_ids if job_id not in canary_job_ids])
        promoted_job_ids = list(dict.fromkeys([*canary.job_ids, *rerun_job_ids]))
        self._record_rollout_sha(promoted_job_ids, candidate.promoted_sha)
        return RepairAttemptResult(status="promoted", reason="")
```

Monitor rollouts before starting new repairs:

```python
    def _monitor_active_rollouts(self) -> bool:
        with init_db(self.db_path) as conn:
            rollouts = repair_rollouts.list_active_rollouts(conn)
        if not rollouts:
            return False
        for rollout in rollouts:
            confirmation = rollout["status"] == "paused_pending_confirmation"
            with init_db(self.db_path) as conn:
                evaluation = repair_rollouts.evaluate_rollout(conn, rollout, confirmation=confirmation)
                if evaluation.action == "monitor":
                    repair_rollouts.update_rollout_status(
                        conn,
                        rollout["id"],
                        status="active",
                        post_fix_metrics=evaluation.post_fix_metrics,
                        output_root=self._output_root,
                    )
                    continue
                if evaluation.action == "pause":
                    baseline = json.loads(str(rollout["baseline_metrics_json"] or "{}"))
                    repair_rollouts.set_repair_queue_pause(
                        conn,
                        rollout_id=int(rollout["id"]),
                        commit_sha=str(rollout["commit_sha"]),
                        cluster_id=int(rollout["cluster_id"]),
                        fingerprint=str(baseline["fingerprint"]),
                        reason=evaluation.reason,
                    )
                    repair_rollouts.update_rollout_status(
                        conn,
                        rollout["id"],
                        status="paused_pending_confirmation",
                        post_fix_metrics=evaluation.post_fix_metrics,
                        output_root=self._output_root,
                    )
                    return True
                if evaluation.action == "clear":
                    repair_rollouts.clear_repair_queue_pause(conn)
                    repair_rollouts.update_rollout_status(
                        conn,
                        rollout["id"],
                        status="monitoring_resumed",
                        post_fix_metrics=evaluation.post_fix_metrics,
                        output_root=self._output_root,
                    )
                    return True
                if evaluation.action == "revert":
                    baseline = json.loads(str(rollout["baseline_metrics_json"] or "{}"))
                    revert_sha = self._revert_main(str(rollout["commit_sha"]))
                    self._sync_runtime_repo(revert_sha)
                    self._requeue_jobs([int(job_id) for job_id in baseline.get("monitored_job_ids", [])])
                    repair_rollouts.clear_repair_queue_pause(conn)
                    repair_rollouts.update_rollout_status(
                        conn,
                        rollout["id"],
                        status="reverted",
                        post_fix_metrics=evaluation.post_fix_metrics,
                        revert_sha=revert_sha,
                        output_root=self._output_root,
                    )
                    return True
        return False
```

Call the monitor first in `run_once()`:

```python
    def run_once(self) -> bool:
        if self._monitor_active_rollouts():
            return True
        ...
```

- [ ] **Step 4: Re-run the supervisor tests**

Run:

```bash
uv run python -m pytest tests/test_repair_supervisor.py -k "rollout or revert" -v
```

Expected:
- All three new rollout/revert tests `PASS`

- [ ] **Step 5: Commit the supervisor orchestration slice**

```bash
git add scripts/repair_git.py scripts/repair_supervisor.py tests/test_repair_supervisor.py
git commit -m "feat: monitor and revert repair rollouts"
```

---

### Task 4: Expose rollout pause state in worker-status surfaces and run full verification

**Files:**
- Modify: `scripts/job_web.py`
- Modify: `tests/test_job_web.py`

- [ ] **Step 1: Write the failing worker-status tests**

In `tests/test_job_web.py`, add:

```python
def test_worker_status_reports_repair_queue_pause_state(client):
    with (
        mock.patch("job_web.is_worker_running", return_value=False),
        mock.patch("job_web.is_repair_supervisor_running", return_value=True),
        mock.patch(
            "job_web.get_repair_queue_pause",
            return_value={"rollout_id": 7, "reason": "fingerprint_recurred"},
        ),
    ):
        resp = client.get("/api/workers/status")

    assert resp.status_code == 200
    assert resp.json()["repair_queue_paused"] is True
    assert resp.json()["repair_queue_pause"]["rollout_id"] == 7


def test_websocket_initial_worker_status_includes_repair_queue_pause(client):
    with (
        mock.patch("job_web.is_worker_running", return_value=False),
        mock.patch("job_web.is_repair_supervisor_running", return_value=True),
        mock.patch(
            "job_web.get_repair_queue_pause",
            return_value={"rollout_id": 9, "reason": "unexpected_hard_failures"},
        ),
    ):
        with client.websocket_connect("/ws") as ws:
            _bulk = ws.receive_json()
            status = ws.receive_json()

    assert status["repair_queue_paused"] is True
    assert status["repair_queue_pause"]["rollout_id"] == 9
```

- [ ] **Step 2: Run the worker-status tests and verify they fail**

Run:

```bash
uv run python -m pytest tests/test_job_web.py -k "repair_queue_pause or worker_status" -v
```

Expected:
- `FAIL` because worker-status payloads do not include `repair_queue_paused` or `repair_queue_pause`

- [ ] **Step 3: Extend `job_web.py` runtime-service payloads**

Update the imports in `scripts/job_web.py`:

```python
from job_db import get_repair_queue_pause
```

Then expand `_read_runtime_services()`:

```python
def _read_runtime_services() -> dict[str, object]:
    conn = get_conn()
    pause = get_repair_queue_pause(conn)
    return {
        "workers_running": is_worker_running(),
        "repair_supervisor_running": is_repair_supervisor_running(project_root=PROJECT_ROOT),
        "repair_queue_paused": pause is not None,
        "repair_queue_pause": pause,
    }
```

Thread those keys through every worker-status payload:

```python
        return {
            "running": runtime_services["workers_running"],
            "repair_supervisor_running": runtime_services["repair_supervisor_running"],
            "repair_queue_paused": runtime_services["repair_queue_paused"],
            "repair_queue_pause": runtime_services["repair_queue_pause"],
            "active_jobs": active_list,
        }
```

And the websocket payloads:

```python
                {
                    "type": "worker_status",
                    "running": runtime_services["workers_running"],
                    "repair_supervisor_running": runtime_services["repair_supervisor_running"],
                    "repair_queue_paused": runtime_services["repair_queue_paused"],
                    "repair_queue_pause": runtime_services["repair_queue_pause"],
                    "active_jobs": [dict(r) for r in active],
                }
```

- [ ] **Step 4: Re-run the worker-status tests, then run full verification**

Run:

```bash
uv run python -m pytest tests/test_job_web.py -k "repair_queue_pause or worker_status" -v
uv run python -m pytest tests/ -v
uv run ruff check scripts/ tests/
uv run python scripts/check_architecture.py
uv run python scripts/sync_agent_files.py --check
uv run python scripts/check_agent_docs.py
```

Expected:
- The targeted `tests/test_job_web.py` selection `PASS`
- The full repo verification suite finishes green

- [ ] **Step 5: Commit the status-surface and verification slice**

```bash
git add scripts/job_web.py tests/test_job_web.py
git commit -m "feat: surface repair rollout pause state"
```

---

## Self-Review Checklist

- Spec coverage:
  - runtime-flag-backed pause gate: Task 1
  - active rollout persistence and markdown index: Task 2
  - pause-confirm-revert orchestration: Task 3
  - worker-status truthfulness: Task 4
- Placeholder scan:
  - no placeholder or deferred implementation markers remain
- Type/name consistency:
  - helper names used later in the plan are defined earlier: `set_repair_queue_pause`, `get_repair_queue_pause`, `record_active_rollout`, `evaluate_rollout`, `revert_main`
