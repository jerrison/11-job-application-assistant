import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import repair_git
import repair_runtime
import repair_supervisor
from job_db import init_db
from repair_runtime import RepairSupervisorConfig, ensure_repair_supervisor_running, repair_supervisor_enabled
from repair_supervisor import CanaryOutcome, CanaryRerunResult, PromotedRepair, RepairPacket, RepairSupervisor


class DummyProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.terminated = False

    def poll(self) -> None:
        return None if not self.terminated else 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


@pytest.fixture(autouse=True)
def reset_repair_runtime_state():
    repair_runtime._repair_supervisor_proc = None
    yield
    repair_runtime._repair_supervisor_proc = None


def test_repair_supervisor_config_defaults_to_openai_gpt_5_4_xhigh():
    config = RepairSupervisorConfig.from_env({})

    assert config.provider == "openai"
    assert config.model == "gpt-5.4"
    assert config.reasoning_effort == "xhigh"


def test_repair_supervisor_feature_flag_defaults_disabled():
    assert repair_supervisor_enabled({}) is False
    assert repair_supervisor_enabled({"JOB_ASSETS_ENABLE_REPAIR_SUPERVISOR": "1"}) is True


def test_ensure_repair_supervisor_running_starts_singleton_when_enabled(tmp_path, monkeypatch):
    spawned: list[tuple[tuple, dict]] = []

    def fake_popen(*args, **kwargs):
        spawned.append((args, kwargs))
        return DummyProc(123)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    env = {"JOB_ASSETS_ENABLE_REPAIR_SUPERVISOR": "true"}
    ensure_repair_supervisor_running(project_root=tmp_path, environ=env)
    ensure_repair_supervisor_running(project_root=tmp_path, environ=env)

    assert len(spawned) == 1
    assert (tmp_path / "jobs.db.repair_supervisor.pid").read_text(encoding="utf-8") == "123"
    assert spawned[0][1]["stdin"] is subprocess.DEVNULL
    assert spawned[0][1]["env"]["ASSET_REPAIR_LLM_PROVIDER"] == "openai"
    assert spawned[0][1]["env"]["ASSET_REPAIR_OPENAI_MODEL"] == "gpt-5.4"
    assert spawned[0][1]["env"]["ASSET_REPAIR_OPENAI_REASONING_EFFORT"] == "xhigh"


def test_ensure_repair_supervisor_running_skips_when_feature_flag_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("spawned")))

    ensure_repair_supervisor_running(project_root=tmp_path, environ={})

    assert not (tmp_path / "jobs.db.repair_supervisor.pid").exists()


def test_ensure_repair_supervisor_running_coordinates_parallel_startup(tmp_path, monkeypatch):
    spawned: list[int] = []
    gate = threading.Event()

    def fake_popen(*args, **kwargs):
        spawned.append(1)
        gate.wait(timeout=1)
        time.sleep(0.05)
        return DummyProc(123)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    env = {"JOB_ASSETS_ENABLE_REPAIR_SUPERVISOR": "true"}
    first = threading.Thread(target=ensure_repair_supervisor_running, kwargs={"project_root": tmp_path, "environ": env})
    second = threading.Thread(target=ensure_repair_supervisor_running, kwargs={"project_root": tmp_path, "environ": env})

    first.start()
    second.start()
    time.sleep(0.05)
    gate.set()
    first.join()
    second.join()

    assert len(spawned) == 1


def test_ensure_repair_supervisor_running_keeps_singleton_when_spawn_exceeds_lock_timeout(tmp_path, monkeypatch):
    spawned: list[int] = []
    gate = threading.Event()

    monkeypatch.setattr(repair_runtime, "_REPAIR_SUPERVISOR_START_LOCK_WAIT_TIMEOUT_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(repair_runtime, "_REPAIR_SUPERVISOR_START_LOCK_POLL_INTERVAL_SECONDS", 0.001, raising=False)

    def fake_popen(*args, **kwargs):
        spawned.append(1)
        gate.wait(timeout=1)
        time.sleep(0.03)
        return DummyProc(123)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    env = {"JOB_ASSETS_ENABLE_REPAIR_SUPERVISOR": "true"}
    first = threading.Thread(target=ensure_repair_supervisor_running, kwargs={"project_root": tmp_path, "environ": env})
    second = threading.Thread(target=ensure_repair_supervisor_running, kwargs={"project_root": tmp_path, "environ": env})

    first.start()
    second.start()
    time.sleep(0.015)
    gate.set()
    first.join()
    second.join()

    assert len(spawned) == 1


def test_ensure_repair_supervisor_running_cleans_up_spawned_process_when_pid_write_fails(tmp_path, monkeypatch):
    proc = DummyProc(123)
    pid_path = tmp_path / "jobs.db.repair_supervisor.pid"
    original_write_text = Path.write_text

    def fake_write_text(self: Path, data: str, *args, **kwargs):
        if self == pid_path:
            raise OSError("disk full")
        return original_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(Path, "write_text", fake_write_text)

    with pytest.raises(OSError):
        ensure_repair_supervisor_running(
            project_root=tmp_path,
            environ={"JOB_ASSETS_ENABLE_REPAIR_SUPERVISOR": "true"},
        )

    assert proc.terminated is True
    assert repair_runtime._repair_supervisor_proc is None


def test_repair_loop_requires_failing_regression_before_promotion(monkeypatch, tmp_path):
    db_path = tmp_path / "jobs.db"
    with init_db(db_path) as conn:
        conn.execute(
            "INSERT INTO repair_clusters (id, fingerprint, status, eligibility, representative_job_ids, latest_summary) "
            "VALUES (1, 'greenhouse:draft_audit:rendered_audit_mismatch:work-auth', 'open', "
            "'auto_repair_candidate', '[42, 43]', 'Work authorization mismatch')"
        )
        conn.commit()

    supervisor = RepairSupervisor(project_root=tmp_path, db_path=db_path)
    packet = RepairPacket(
        cluster_id=1,
        fingerprint="greenhouse:draft_audit:rendered_audit_mismatch:work-auth",
        job_ids=[42, 43],
        prompt="Fix the clustered failure.",
        likely_files=["scripts/greenhouse.py"],
        verification_commands=[["uv", "run", "python", "-m", "pytest", "tests/test_greenhouse.py", "-v"]],
    )
    cleaned: list[Path] = []

    monkeypatch.setattr(supervisor, "_build_repair_packet", lambda *_: packet)
    monkeypatch.setattr(
        repair_supervisor,
        "create_repair_worktree",
        lambda **_: SimpleNamespace(path=tmp_path / "repair-worktree", branch="autofix/test", base_sha="deadbeef"),
    )
    monkeypatch.setattr(supervisor, "_run_repair_agent", lambda *_: "abc1234")
    monkeypatch.setattr(supervisor, "_require_failing_regression", lambda *_: False)
    monkeypatch.setattr(supervisor, "_run_targeted_verification", lambda *_: True)
    monkeypatch.setattr(repair_supervisor, "cleanup_repair_worktree", lambda worktree: cleaned.append(worktree.path))

    result = supervisor._attempt_cluster_repair(cluster_id=1)

    assert result.status == "failed"
    assert result.reason == "missing_failing_regression"
    assert cleaned == [tmp_path / "repair-worktree"]


def test_run_repair_agent_uses_devnull_stdin(monkeypatch, tmp_path):
    worktree_path = tmp_path / "repair-worktree"
    worktree_path.mkdir()
    supervisor = RepairSupervisor(project_root=tmp_path, db_path=tmp_path / "jobs.db")
    packet = RepairPacket(
        cluster_id=1,
        fingerprint="greenhouse:stopped:submit_failed:bad_stdin",
        job_ids=[42],
        prompt="Fix the clustered failure.",
        likely_files=["scripts/pipeline_orchestrator.py"],
        verification_commands=[["uv", "run", "python", "-m", "pytest", "tests/test_pipeline_orchestrator.py", "-v"]],
    )
    captured_kwargs: list[dict] = []

    def fake_run(_cmd, **kwargs):
        captured_kwargs.append(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(repair_supervisor, "provider_available", lambda *_: True)
    monkeypatch.setattr(
        repair_supervisor,
        "provider_command_for_mode",
        lambda *_args, **_kwargs: ["uv", "run", "python", "scripts/openai_provider.py"],
    )
    monkeypatch.setattr(repair_supervisor, "commit_repair_candidate", lambda *_args, **_kwargs: "abc1234")
    monkeypatch.setattr(repair_supervisor.subprocess, "run", fake_run)

    commit_sha = supervisor._run_repair_agent(packet, SimpleNamespace(path=worktree_path))

    assert commit_sha == "abc1234"
    assert captured_kwargs[0]["stdin"] is subprocess.DEVNULL


def test_run_canary_rerun_uses_devnull_stdin(monkeypatch, tmp_path):
    db_path = tmp_path / "jobs.db"
    with init_db(db_path) as conn:
        conn.execute("INSERT INTO jobs (id, url, status) VALUES (42, 'http://x/42', 'draft')")
        conn.commit()

    supervisor = RepairSupervisor(project_root=tmp_path, db_path=db_path)
    candidate = PromotedRepair(
        pre_sha="deadbeef",
        promoted_sha="abc1234",
        cluster_id=1,
        job_ids=[42],
        worktree_path=tmp_path / "repair-worktree",
    )
    captured_kwargs: list[dict] = []

    def fake_run(_cmd, **kwargs):
        captured_kwargs.append(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(repair_supervisor.subprocess, "run", fake_run)

    result = supervisor._run_canary_rerun(candidate, 42)

    assert result.status == "draft"
    assert captured_kwargs[0]["stdin"] is subprocess.DEVNULL


def test_repair_loop_worktree_uses_verified_origin_main_base(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    class _Result:
        def __init__(self, stdout: str = "") -> None:
            self.stdout = stdout

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["git", "rev-parse", "origin/main"]:
            return _Result("feedface\n")
        return _Result("")

    monkeypatch.setattr(repair_git.subprocess, "run", fake_run)

    worktree = repair_git.create_repair_worktree(project_root=tmp_path, cluster_fingerprint="greenhouse:repair:work-auth")

    assert worktree.base_sha == "feedface"
    assert ["git", "fetch", "origin", "main"] in calls
    assert ["git", "rev-parse", "origin/main"] in calls
    assert any(cmd[:4] == ["git", "worktree", "add", "-B"] and cmd[-1] == "origin/main" for cmd in calls)


def test_repair_loop_uses_packet_specific_regression_commands(tmp_path):
    db_path = tmp_path / "jobs.db"
    with init_db(db_path) as conn:
        conn.execute(
            "INSERT INTO repair_clusters (id, fingerprint, status, eligibility, representative_job_ids, latest_summary) "
            "VALUES (1, 'greenhouse:draft_audit:rendered_audit_mismatch:work-auth', 'open', "
            "'auto_repair_candidate', '[42]', 'Work authorization mismatch')"
        )
        conn.commit()

    supervisor = RepairSupervisor(project_root=tmp_path, db_path=db_path)

    packet = supervisor._build_repair_packet(1)
    joined_commands = " || ".join(" ".join(command) for command in packet.verification_commands)

    assert packet.regression_test_paths
    assert packet.regression_test_commands
    assert "repair_loop or canary" not in joined_commands
    assert "greenhouse" in joined_commands.casefold() or "rendered" in joined_commands.casefold()


def test_repair_loop_uses_only_real_repo_test_files(tmp_path):
    db_path = tmp_path / "jobs.db"
    with init_db(db_path) as conn:
        conn.execute(
            "INSERT INTO repair_clusters (id, fingerprint, status, eligibility, representative_job_ids, latest_summary) "
            "VALUES (1, 'greenhouse:draft_audit:rendered_audit_mismatch:work-auth', 'open', "
            "'auto_repair_candidate', '[42]', 'Work authorization mismatch')"
        )
        conn.commit()

    supervisor = RepairSupervisor(project_root=tmp_path, db_path=db_path)

    packet = supervisor._build_repair_packet(1)

    assert "tests/test_autofill_greenhouse.py" not in packet.regression_test_paths
    assert packet.regression_test_paths == ["tests/test_pipeline_orchestrator.py"]


def test_repair_loop_requires_relevant_failing_regression_path(monkeypatch, tmp_path):
    db_path = tmp_path / "jobs.db"
    with init_db(db_path) as conn:
        conn.execute(
            "INSERT INTO repair_clusters (id, fingerprint, status, eligibility, representative_job_ids, latest_summary) "
            "VALUES (1, 'greenhouse:draft_audit:rendered_audit_mismatch:work-auth', 'open', "
            "'auto_repair_candidate', '[42]', 'Work authorization mismatch')"
        )
        conn.commit()

    supervisor = RepairSupervisor(project_root=tmp_path, db_path=db_path)
    packet = RepairPacket(
        cluster_id=1,
        fingerprint="greenhouse:draft_audit:rendered_audit_mismatch:work-auth",
        job_ids=[42],
        prompt="Fix the clustered failure.",
        likely_files=["scripts/autofill_greenhouse.py"],
        verification_commands=[["uv", "run", "python", "-m", "pytest", "tests/test_pipeline_orchestrator.py", "-k", "rendered", "-v"]],
        regression_test_paths=["tests/test_autofill_greenhouse.py"],
        regression_test_commands=[
            ["uv", "run", "python", "-m", "pytest", "tests/test_autofill_greenhouse.py", "-k", "work_auth", "-v"]
        ],
    )

    monkeypatch.setattr(supervisor, "_changed_paths_since_base", lambda *_: ["tests/test_pipeline_orchestrator.py"])

    result = supervisor._require_failing_regression(packet, SimpleNamespace(path=tmp_path / "repair-worktree", base_sha="deadbeef"))

    assert result is False


def test_targeted_verification_includes_pytest_ruff_and_architecture(monkeypatch, tmp_path):
    supervisor = RepairSupervisor(project_root=tmp_path, db_path=tmp_path / "jobs.db")
    packet = RepairPacket(
        cluster_id=1,
        fingerprint="shared:draft_audit:rendered_audit_mismatch:work-auth",
        job_ids=[42],
        prompt="Fix the clustered failure.",
        likely_files=["scripts/pipeline_orchestrator.py", "scripts/repair_supervisor.py"],
        verification_commands=[["uv", "run", "python", "-m", "pytest", "tests/test_pipeline_orchestrator.py", "-k", "rendered", "-v"]],
        regression_test_paths=["tests/test_pipeline_orchestrator.py"],
        regression_test_commands=[["uv", "run", "python", "-m", "pytest", "tests/test_pipeline_orchestrator.py", "-k", "rendered", "-v"]],
    )

    monkeypatch.setattr(
        supervisor,
        "_changed_paths_since_base",
        lambda *_: ["scripts/pipeline_orchestrator.py", "scripts/repair_supervisor.py", "tests/test_pipeline_orchestrator.py"],
    )

    commands = supervisor._verification_commands_for_packet(
        packet,
        SimpleNamespace(path=tmp_path / "repair-worktree", base_sha="deadbeef"),
    )

    assert commands[0][:5] == ["uv", "run", "python", "-m", "pytest"]
    assert any(command[:4] == ["uv", "run", "ruff", "check"] for command in commands)
    assert ["uv", "run", "python", "scripts/check_architecture.py"] in commands


def test_canary_requires_live_rerun_success_before_promotion(monkeypatch, tmp_path):
    db_path = tmp_path / "jobs.db"
    with init_db(db_path) as conn:
        conn.execute("INSERT INTO jobs (id, url, status) VALUES (42, 'http://x/42', 'stopped')")
        conn.execute("INSERT INTO jobs (id, url, status) VALUES (43, 'http://x/43', 'stopped')")
        conn.commit()

    supervisor = RepairSupervisor(project_root=tmp_path, db_path=db_path)
    candidate = PromotedRepair(
        pre_sha="deadbeef",
        promoted_sha="abc1234",
        cluster_id=1,
        job_ids=[42, 43],
        worktree_path=tmp_path / "repair-worktree",
    )

    monkeypatch.setattr(supervisor, "_requeue_jobs", lambda job_ids: [42])
    monkeypatch.setattr(supervisor, "_run_canary_rerun", lambda promoted, job_id: "stopped")

    outcome = supervisor._run_canary_jobs(candidate, [42, 43])

    assert outcome.ok is False
    assert outcome.job_ids == []
    assert outcome.rerun_statuses == {42: "stopped"}


def test_canary_accepts_truthful_terminal_outcome_without_recurrence(tmp_path):
    supervisor = RepairSupervisor(project_root=tmp_path, db_path=tmp_path / "jobs.db")
    packet = RepairPacket(
        cluster_id=1,
        fingerprint="greenhouse:draft_audit:rendered_audit_mismatch:work-auth",
        job_ids=[42],
        prompt="Fix the clustered failure.",
        likely_files=["scripts/pipeline_orchestrator.py"],
        verification_commands=[],
        regression_test_paths=["tests/test_pipeline_orchestrator.py"],
        regression_test_commands=[["uv", "run", "python", "-m", "pytest", "tests/test_pipeline_orchestrator.py", "-k", "rendered", "-v"]],
    )
    result = CanaryRerunResult(
        job_id=42,
        status="stopped",
        failure_type="auth_failed",
        error_message="Authentication required.",
        fingerprint_recurred=False,
    )

    assert supervisor._canary_result_counts_as_success(packet, result) is True


def test_failed_canary_restores_jobs_instead_of_leaking_to_queue(monkeypatch, tmp_path):
    db_path = tmp_path / "jobs.db"
    with init_db(db_path) as conn:
        conn.execute(
            "INSERT INTO jobs (id, url, status, error_message, failure_type, progress, provider, retry_after) "
            "VALUES (42, 'http://x/42', 'stopped', 'Submission failed.', 'submit_failed', 'Stopped on failure', 'openai', "
            "'2026-04-02 12:00:00')"
        )
        conn.commit()

    supervisor = RepairSupervisor(project_root=tmp_path, db_path=db_path)
    candidate = PromotedRepair(
        pre_sha="deadbeef",
        promoted_sha="abc1234",
        cluster_id=1,
        job_ids=[42],
        worktree_path=tmp_path / "repair-worktree",
    )
    monkeypatch.setattr(
        supervisor,
        "_run_canary_rerun",
        lambda *_: CanaryRerunResult(
            job_id=42,
            status="queued",
            failure_type="rendered_audit_mismatch",
            error_message="recurrence",
            fingerprint_recurred=True,
        ),
    )

    outcome = supervisor._run_canary_jobs(candidate, [42])

    with init_db(db_path) as conn:
        row = conn.execute("SELECT status, failure_type, error_message, provider FROM jobs WHERE id = 42").fetchone()

    assert outcome.ok is False
    assert row["status"] == "stopped"
    assert row["failure_type"] == "submit_failed"
    assert row["error_message"] == "Submission failed."
    assert row["provider"] == "openai"


def test_canary_promotion_pushes_and_records_only_requeued_jobs(monkeypatch, tmp_path):
    db_path = tmp_path / "jobs.db"
    with init_db(db_path) as conn:
        conn.execute("INSERT INTO jobs (id, url, status) VALUES (42, 'http://x/42', 'stopped')")
        conn.execute("INSERT INTO jobs (id, url, status) VALUES (43, 'http://x/43', 'stopped')")
        conn.commit()

    supervisor = RepairSupervisor(project_root=tmp_path, db_path=db_path)
    pushed: list[str] = []
    order: list[str] = []
    rollout_updates: list[tuple[list[int], str]] = []

    monkeypatch.setattr(
        supervisor,
        "_run_canary_jobs",
        lambda *_: CanaryOutcome(ok=True, job_ids=[42], rerun_statuses={42: "draft"}),
    )
    monkeypatch.setattr(supervisor, "_push_main", lambda sha: (pushed.append(sha), order.append(f"push:{sha}")))
    monkeypatch.setattr(supervisor, "_sync_runtime_repo", lambda sha: order.append(f"sync:{sha}"))
    monkeypatch.setattr(supervisor, "_requeue_jobs", lambda job_ids: [42])
    monkeypatch.setattr(supervisor, "_record_rollout_sha", lambda job_ids, sha: rollout_updates.append((list(job_ids), sha)))
    monkeypatch.setattr(repair_supervisor.repair_rollouts, "record_active_rollout", lambda *args, **kwargs: 7)

    candidate = PromotedRepair(
        pre_sha="deadbeef",
        promoted_sha="abc1234",
        cluster_id=1,
        job_ids=[42, 43],
        worktree_path=tmp_path / "repair-worktree",
    )

    result = supervisor._promote_repair_candidate(candidate)

    assert pushed == ["abc1234"]
    assert order == ["push:abc1234", "sync:abc1234"]
    assert result.status == "promoted"
    assert rollout_updates == [([42], "abc1234")]


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
    monkeypatch.setattr(supervisor, "_push_main", lambda *_: None)
    monkeypatch.setattr(supervisor, "_sync_runtime_repo", lambda *_: None)
    monkeypatch.setattr(supervisor, "_requeue_jobs", lambda job_ids: [43])
    monkeypatch.setattr(supervisor, "_record_rollout_sha", lambda *_: None)

    def fake_record_active_rollout(*args, **kwargs):
        rollout_calls.append(dict(kwargs))
        return 7

    monkeypatch.setattr(repair_supervisor.repair_rollouts, "record_active_rollout", fake_record_active_rollout)

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
            "VALUES (?, ?, ?, ?)",
            (
                1,
                "abc1234",
                "active",
                json.dumps(
                    {
                        "fingerprint": "greenhouse:draft_audit:rendered_audit_mismatch:work-auth",
                        "board": "greenhouse",
                        "phase": "draft_audit",
                        "monitored_job_ids": [42],
                    },
                    sort_keys=True,
                ),
            ),
        )
        conn.commit()

    supervisor = RepairSupervisor(project_root=tmp_path, db_path=db_path)
    paused: list[str] = []

    monkeypatch.setattr(
        repair_supervisor.repair_rollouts,
        "evaluate_rollout",
        lambda *_args, **_kwargs: repair_supervisor.repair_rollouts.RolloutEvaluation(
            action="pause",
            reason="fingerprint_recurred",
            post_fix_metrics={"fingerprint_recurrences": 1},
        ),
    )
    monkeypatch.setattr(
        repair_supervisor.repair_rollouts,
        "set_repair_queue_pause",
        lambda *_args, **_kwargs: paused.append("paused"),
    )
    monkeypatch.setattr(
        supervisor,
        "_attempt_cluster_repair",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not start new repair")),
    )

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
            "VALUES (?, ?, ?, ?)",
            (
                1,
                "abc1234",
                "paused_pending_confirmation",
                json.dumps(
                    {
                        "fingerprint": "greenhouse:draft_audit:rendered_audit_mismatch:work-auth",
                        "board": "greenhouse",
                        "phase": "draft_audit",
                        "monitored_job_ids": [42, 43],
                    },
                    sort_keys=True,
                ),
            ),
        )
        conn.commit()

    supervisor = RepairSupervisor(project_root=tmp_path, db_path=db_path)
    reverted: list[str] = []
    synced: list[str] = []
    requeued: list[list[int]] = []

    monkeypatch.setattr(
        repair_supervisor.repair_rollouts,
        "evaluate_rollout",
        lambda *_args, **_kwargs: repair_supervisor.repair_rollouts.RolloutEvaluation(
            action="revert",
            reason="fingerprint_recurred",
            post_fix_metrics={"fingerprint_recurrences": 2},
        ),
    )
    monkeypatch.setattr(supervisor, "_revert_main", lambda promoted_sha: reverted.append(promoted_sha) or "revert5678")
    monkeypatch.setattr(supervisor, "_sync_runtime_repo", lambda revert_sha: synced.append(revert_sha))
    monkeypatch.setattr(
        supervisor,
        "_requeue_jobs",
        lambda job_ids: requeued.append(list(job_ids)) or list(job_ids),
    )

    handled = supervisor.run_once()

    assert handled is True
    assert reverted == ["abc1234"]
    assert synced == ["revert5678"]
    assert requeued == [[42, 43]]


def test_repair_loop_exhausts_cluster_after_max_failed_cycles(monkeypatch, tmp_path):
    db_path = tmp_path / "jobs.db"
    with init_db(db_path) as conn:
        conn.execute(
            "INSERT INTO jobs (id, url, status) VALUES (42, 'http://x/42', 'stopped')"
        )
        conn.execute(
            "INSERT INTO repair_clusters (id, fingerprint, status, eligibility, representative_job_ids, latest_summary) "
            "VALUES (1, 'greenhouse:draft_audit:rendered_audit_mismatch:work-auth', 'open', "
            "'auto_repair_candidate', '[42]', 'Work authorization mismatch')"
        )
        conn.execute(
            "INSERT INTO repair_rollouts (cluster_id, commit_sha, status) VALUES (1, 'old1', 'failed_missing_regression')"
        )
        conn.execute(
            "INSERT INTO repair_rollouts (cluster_id, commit_sha, status) VALUES (1, 'old2', 'failed_verification')"
        )
        conn.commit()

    supervisor = RepairSupervisor(project_root=tmp_path, db_path=db_path, max_rollouts_per_cluster=3)
    packet = RepairPacket(
        cluster_id=1,
        fingerprint="greenhouse:draft_audit:rendered_audit_mismatch:work-auth",
        job_ids=[42],
        prompt="Fix the clustered failure.",
        likely_files=["scripts/autofill_greenhouse.py"],
        verification_commands=[["uv", "run", "python", "-m", "pytest", "tests/test_pipeline_orchestrator.py", "-k", "rendered", "-v"]],
        regression_test_paths=["tests/test_autofill_greenhouse.py"],
        regression_test_commands=[
            ["uv", "run", "python", "-m", "pytest", "tests/test_autofill_greenhouse.py", "-k", "work_auth", "-v"]
        ],
    )

    monkeypatch.setattr(supervisor, "_build_repair_packet", lambda *_: packet)
    monkeypatch.setattr(
        repair_supervisor,
        "create_repair_worktree",
        lambda **_: SimpleNamespace(path=tmp_path / "repair-worktree", branch="autofix/test", base_sha="deadbeef"),
    )
    monkeypatch.setattr(supervisor, "_run_repair_agent", lambda *_: "abc1234")
    monkeypatch.setattr(supervisor, "_require_failing_regression", lambda *_: False)
    monkeypatch.setattr(repair_supervisor, "cleanup_repair_worktree", lambda *_: None)

    result = supervisor._attempt_cluster_repair(cluster_id=1)

    with init_db(db_path) as conn:
        cluster = conn.execute("SELECT status, latest_summary FROM repair_clusters WHERE id = 1").fetchone()
        job = conn.execute("SELECT status, failure_type FROM jobs WHERE id = 42").fetchone()
        metrics = conn.execute("SELECT audit_failure_count FROM job_metrics WHERE job_id = 42").fetchone()

    assert result.status == "failed"
    assert cluster["status"] == "exhausted"
    assert job["status"] == "stopped"
    assert job["failure_type"] == "audit_failure"
    assert metrics["audit_failure_count"] == 1
