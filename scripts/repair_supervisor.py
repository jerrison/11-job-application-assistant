"""Singleton repair supervisor process bootstrap and bounded repair loop."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

import repair_rollouts
from job_db import (
    count_repair_rollouts,
    ensure_job_metrics,
    get_repair_cluster,
    init_db,
    list_exhausted_repair_clusters,
    list_open_repair_clusters,
    record_repair_rollout,
    update_job_metrics,
    update_repair_cluster,
)
from llm_provider import provider_available, provider_command_for_mode
from pipeline_orchestrator import (
    prepare_jobs_for_repair_canary,
    requeue_jobs_for_repair_redraft,
    restore_jobs_after_failed_repair_canary,
    stop_jobs_for_exhausted_repair_cluster,
)
from repair_git import (
    cleanup_repair_worktree,
    commit_repair_candidate,
    create_detached_verification_worktree,
    create_repair_worktree,
    push_main,
    revert_main,
    sync_runtime_repo_to_promoted_main,
    verify_candidate_commit,
)
from repair_runtime import RepairSupervisorConfig
from worker_subprocess import run_worker_subprocess

log = logging.getLogger(__name__)
_DEFAULT_REPAIR_POLL_INTERVAL_SECONDS = 30.0
_DEFAULT_REPAIR_MAX_ROLLOUTS_PER_CLUSTER = 3
_SHARED_PIPELINE_SURFACES = frozenset(
    {
        "scripts/pipeline_orchestrator.py",
        "scripts/job_db.py",
        "scripts/repair_supervisor.py",
    }
)
_CANARY_TRUTHFUL_TERMINAL_FAILURE_TYPES = frozenset(
    {
        "already_applied",
        "auth_failed",
        "auth_guarded",
        "auth_unknown",
        "duplicate",
        "external_apply",
        "job_closed",
        "no_apply_button",
        "pending_user_input",
        "skipped_captcha",
        "unsupported",
        "user_rejected",
        "user_stopped",
    }
)


@dataclass(frozen=True)
class RepairPacket:
    cluster_id: int
    fingerprint: str
    job_ids: list[int]
    prompt: str
    likely_files: list[str]
    verification_commands: list[list[str]]
    regression_test_paths: list[str] = field(default_factory=list)
    regression_test_commands: list[list[str]] = field(default_factory=list)


@dataclass(frozen=True)
class RepairAttemptResult:
    status: str
    reason: str


@dataclass(frozen=True)
class CanaryOutcome:
    ok: bool
    job_ids: list[int]
    rerun_statuses: dict[int, str]


@dataclass(frozen=True)
class CanaryRerunResult:
    job_id: int
    status: str
    failure_type: str | None
    error_message: str | None
    fingerprint_recurred: bool


@dataclass(frozen=True)
class PromotedRepair:
    pre_sha: str
    promoted_sha: str
    cluster_id: int
    job_ids: list[int]
    worktree_path: Path
    fingerprint: str = ""


class RepairSupervisor:
    def __init__(
        self,
        *,
        project_root: Path,
        db_path: Path,
        poll_interval_seconds: float = _DEFAULT_REPAIR_POLL_INTERVAL_SECONDS,
        max_rollouts_per_cluster: int = _DEFAULT_REPAIR_MAX_ROLLOUTS_PER_CLUSTER,
    ) -> None:
        self.project_root = Path(project_root)
        self.source_root = Path(__file__).resolve().parent.parent
        self.db_path = Path(db_path)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.max_rollouts_per_cluster = int(max_rollouts_per_cluster)

    @property
    def _output_root(self) -> Path:
        return self.project_root / "output"

    def run_forever(self, *, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                handled = self.run_once()
            except Exception as exc:  # pragma: no cover - defensive runtime guard
                handled = False
                log.exception("repair supervisor loop crashed: %s", exc)
            if handled:
                continue
            stop_event.wait(self.poll_interval_seconds)

    def run_once(self) -> bool:
        if self._monitor_active_rollouts():
            return True

        with init_db(self.db_path) as conn:
            exhausted = list_exhausted_repair_clusters(
                conn,
                limit=1,
                max_rollouts=self.max_rollouts_per_cluster,
            )
        if exhausted:
            self._exhaust_existing_cluster(exhausted[0], reason="repair_attempts_exhausted")
            return True

        with init_db(self.db_path) as conn:
            clusters = list_open_repair_clusters(
                conn,
                limit=1,
            )
        if not clusters:
            return False
        cluster_id = int(clusters[0]["id"])
        result = self._attempt_cluster_repair(cluster_id=cluster_id)
        log.info("repair cluster %d finished with status=%s reason=%s", cluster_id, result.status, result.reason)
        return True

    def _attempt_cluster_repair(self, cluster_id: int) -> RepairAttemptResult:
        packet = self._build_repair_packet(cluster_id)
        worktree = create_repair_worktree(project_root=self.project_root, cluster_fingerprint=packet.fingerprint)
        candidate_sha = ""
        with init_db(self.db_path) as conn:
            update_repair_cluster(conn, cluster_id, status="repairing")
        try:
            candidate_sha = self._run_repair_agent(packet, worktree)
            if not self._require_failing_regression(packet, worktree):
                return self._record_failed_attempt(
                    packet,
                    commit_sha=candidate_sha or worktree.base_sha,
                    rollout_status="failed_missing_regression",
                    reason="missing_failing_regression",
                )
            if not self._run_targeted_verification(packet, worktree):
                return self._record_failed_attempt(
                    packet,
                    commit_sha=candidate_sha or worktree.base_sha,
                    rollout_status="failed_verification",
                    reason="verification_failed",
                )
            promoted = self._promote_locally_for_canary(packet, worktree, candidate_sha)
            result = self._promote_repair_candidate(promoted)
            rollout_status = "promoted" if result.status == "promoted" else f"failed_{result.reason or 'promotion'}"
            self._record_attempt(cluster_id, promoted.promoted_sha, rollout_status)
            with init_db(self.db_path) as conn:
                update_repair_cluster(
                    conn,
                    cluster_id,
                    status="promoted" if result.status == "promoted" else "open",
                    latest_summary=result.reason or "promoted",
                )
            return result
        except Exception as exc:
            log.exception("repair cluster %d failed during execution: %s", cluster_id, exc)
            return self._record_failed_attempt(
                packet,
                commit_sha=candidate_sha or worktree.base_sha,
                rollout_status="failed_exception",
                reason="repair_execution_failed",
                detail=str(exc),
            )
        finally:
            cleanup_repair_worktree(worktree)

    def _build_repair_packet(self, cluster_id: int) -> RepairPacket:
        with init_db(self.db_path) as conn:
            cluster = get_repair_cluster(conn, cluster_id)
        if cluster is None:
            raise ValueError(f"repair cluster {cluster_id} not found")
        job_ids = self._coerce_job_ids(cluster.get("representative_job_ids"))
        fingerprint = str(cluster.get("fingerprint") or f"cluster-{cluster_id}")
        summary = str(cluster.get("latest_summary") or "").strip()
        board, phase, failure_type, *_ = (fingerprint.split(":") + ["unknown", "unknown", "unknown"])[:4]
        regression_test_paths = self._regression_test_paths_for_packet(board=board, phase=phase, failure_type=failure_type)
        regression_filter = self._regression_filter_for_packet(board=board, phase=phase, failure_type=failure_type)
        regression_test_commands = self._pytest_commands_for_paths(regression_test_paths, keyword=regression_filter)
        likely_files = self._likely_files_for_packet(board=board, phase=phase)
        verification_paths = self._verification_test_paths_for_packet(
            board=board,
            phase=phase,
            failure_type=failure_type,
            likely_files=likely_files,
        )
        verification_commands = self._pytest_commands_for_paths(verification_paths, keyword=regression_filter)
        return RepairPacket(
            cluster_id=cluster_id,
            fingerprint=fingerprint,
            job_ids=job_ids,
            prompt=f"Repair cluster {fingerprint}: {summary}",
            likely_files=likely_files,
            verification_commands=verification_commands,
            regression_test_paths=regression_test_paths,
            regression_test_commands=regression_test_commands,
        )

    def _run_repair_agent(self, packet: RepairPacket, worktree) -> str:
        config = RepairSupervisorConfig.from_env(os.environ)
        if not provider_available(config.provider):
            raise RuntimeError(f"{config.provider} provider is not available for repair automation")
        prompt = "\n".join(
            [
                packet.prompt,
                "",
                "Likely files:",
                *[f"- {path}" for path in packet.likely_files],
                "",
                "Verification commands:",
                *["- " + " ".join(command) for command in packet.verification_commands],
                "",
                "Requirements:",
                "- Add or update a failing regression test before finalizing the fix.",
                "- Generalize the fix across boards and surfaces touched by the failure.",
                "- Do not run git commands yourself.",
            ]
        )
        result: subprocess.CompletedProcess = run_worker_subprocess(
            provider_command_for_mode(
                config.provider,
                prompt,
                mode="fix",
                project_root=worktree.path,
                environ=os.environ,
            ),
            cwd=worktree.path,
            capture_output=True,
            text=True,
            timeout=900,
        )
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(stderr or f"{config.provider} repair agent exited {result.returncode}")
        message = f"fix(repair): address {packet.fingerprint}"
        return commit_repair_candidate(worktree, message=message)

    def _changed_paths_since_base(self, _packet: RepairPacket, worktree) -> list[str]:
        result = run_worker_subprocess(
            ["git", "diff", "--name-only", f"{worktree.base_sha}..HEAD"],
            cwd=worktree.path,
            check=True,
            capture_output=True,
            text=True,
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _require_failing_regression(self, packet: RepairPacket, worktree) -> bool:
        if not packet.regression_test_paths or not packet.regression_test_commands:
            return False
        changed_paths = set(self._changed_paths_since_base(packet, worktree))
        relevant_paths = [path for path in packet.regression_test_paths if path in changed_paths]
        if not relevant_paths:
            return False
        relevant_commands = [
            command
            for command in packet.regression_test_commands
            if any(path in command for path in relevant_paths)
        ]
        if len(relevant_commands) != len(relevant_paths):
            return False
        baseline_worktree = create_detached_verification_worktree(
            project_root=self.project_root,
            ref=worktree.base_sha,
            cluster_fingerprint=packet.fingerprint,
            label="repair-baseline",
        )
        try:
            baseline_passed = self._run_command_sequence(relevant_commands, baseline_worktree.path)
        finally:
            cleanup_repair_worktree(baseline_worktree)
        if baseline_passed:
            return False
        return self._run_command_sequence(relevant_commands, worktree.path)

    def _run_targeted_verification(self, packet: RepairPacket, worktree) -> bool:
        commands = self._verification_commands_for_packet(packet, worktree)
        if not commands:
            return False
        return self._run_command_sequence(commands, worktree.path, cluster_id=packet.cluster_id)

    def _verification_commands_for_packet(self, packet: RepairPacket, worktree) -> list[list[str]]:
        commands = [list(command) for command in packet.verification_commands]
        changed_paths = self._changed_paths_since_base(packet, worktree)
        ruff_paths = [path for path in changed_paths if path.endswith(".py")]
        if ruff_paths:
            commands.append(["uv", "run", "ruff", "check", *ruff_paths])
        if self._touches_shared_pipeline_surfaces(packet, changed_paths):
            commands.append(["uv", "run", "python", "scripts/check_architecture.py"])
        return commands

    def _promote_locally_for_canary(self, packet: RepairPacket, worktree, candidate_sha: str) -> PromotedRepair:
        verified_base_sha = verify_candidate_commit(
            self.project_root,
            candidate_sha=candidate_sha,
            expected_base_sha=worktree.base_sha,
        )
        return PromotedRepair(
            pre_sha=verified_base_sha,
            promoted_sha=candidate_sha,
            cluster_id=packet.cluster_id,
            job_ids=list(packet.job_ids),
            worktree_path=Path(worktree.path),
            fingerprint=packet.fingerprint,
        )

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

    def _run_canary_jobs(self, candidate: PromotedRepair, requested_job_ids: list[int]) -> CanaryOutcome:
        queued: list[int] = []
        for raw_job_id in requested_job_ids:
            try:
                job_id = int(raw_job_id)
            except (TypeError, ValueError):
                continue
            if job_id not in queued:
                queued.append(job_id)
        if not queued:
            return CanaryOutcome(ok=False, job_ids=[], rerun_statuses={})
        packet = RepairPacket(
            cluster_id=candidate.cluster_id,
            fingerprint=candidate.fingerprint or f"cluster-{candidate.cluster_id}",
            job_ids=list(queued),
            prompt="",
            likely_files=[],
            verification_commands=[],
        )
        with init_db(self.db_path) as conn:
            snapshots = prepare_jobs_for_repair_canary(conn, queued, initiator="repair_supervisor")
        if list(snapshots) != queued:
            with init_db(self.db_path) as conn:
                restore_jobs_after_failed_repair_canary(conn, snapshots, initiator="repair_supervisor")
            return CanaryOutcome(ok=False, job_ids=[], rerun_statuses={})
        rerun_statuses: dict[int, str] = {}
        successful: list[int] = []
        for job_id in queued:
            result = self._run_canary_rerun(candidate, job_id)
            if isinstance(result, str):
                result = CanaryRerunResult(
                    job_id=job_id,
                    status=result,
                    failure_type=None,
                    error_message=None,
                    fingerprint_recurred=False,
                )
            rerun_statuses[job_id] = result.status
            if self._canary_result_counts_as_success(packet, result):
                successful.append(job_id)
                continue
            with init_db(self.db_path) as conn:
                restore_jobs_after_failed_repair_canary(conn, snapshots, initiator="repair_supervisor")
            return CanaryOutcome(ok=False, job_ids=[], rerun_statuses=rerun_statuses)
        return CanaryOutcome(ok=True, job_ids=successful, rerun_statuses=rerun_statuses)

    def _run_canary_rerun(self, candidate: PromotedRepair, job_id: int) -> CanaryRerunResult:
        with init_db(self.db_path) as conn:
            cluster = get_repair_cluster(conn, candidate.cluster_id) or {}
            prior_attempt_count = int(cluster.get("attempt_count") or 0)
        script = "\n".join(
            [
                "import sys",
                "from pathlib import Path",
                f"scripts_dir = Path({str((candidate.worktree_path / 'scripts').resolve())!r})",
                "sys.path.insert(0, str(scripts_dir))",
                "from job_db import init_db",
                "from pipeline_orchestrator import process_job",
                f"db_path = Path({str(self.db_path)!r})",
                f"job_id = {int(job_id)}",
                "with init_db(db_path) as conn:",
                "    process_job(conn, job_id, worker_id=0, headless=True, auto_submit=False)",
            ]
        )
        result = run_worker_subprocess(
            ["uv", "run", "python", "-c", script],
            cwd=candidate.worktree_path,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if result.returncode != 0:
            log.warning(
                "canary rerun failed for cluster %d job %d: %s",
                candidate.cluster_id,
                job_id,
                (result.stderr or result.stdout).strip(),
            )
        with init_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT status, failure_type, error_message FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            cluster = get_repair_cluster(conn, candidate.cluster_id) or {}
        status = str((row["status"] if row else "") or "missing")
        failure_type = str((row["failure_type"] if row else "") or "") or None
        error_message = str((row["error_message"] if row else "") or "") or None
        return CanaryRerunResult(
            job_id=job_id,
            status=status if result.returncode == 0 else "failed",
            failure_type=failure_type,
            error_message=error_message or (result.stderr or result.stdout or "").strip() or None,
            fingerprint_recurred=int(cluster.get("attempt_count") or 0) > prior_attempt_count,
        )

    def _rollback_local_promotion(self, candidate: PromotedRepair) -> None:
        log.warning(
            "repair cluster %d failed canary after local promotion candidate=%s pre_sha=%s",
            candidate.cluster_id,
            candidate.promoted_sha,
            candidate.pre_sha,
        )

    def _push_main(self, promoted_sha: str) -> None:
        push_main(self.project_root, promoted_sha)

    def _sync_runtime_repo(self, promoted_sha: str) -> None:
        sync_runtime_repo_to_promoted_main(self.project_root, promoted_sha=promoted_sha)

    def _revert_main(self, promoted_sha: str) -> str:
        return revert_main(self.project_root, promoted_sha)

    def _requeue_jobs(self, job_ids: list[int]) -> list[int]:
        with init_db(self.db_path) as conn:
            return requeue_jobs_for_repair_redraft(conn, list(job_ids), initiator="repair_supervisor")

    def _record_rollout_sha(self, job_ids: list[int], promoted_sha: str) -> None:
        with init_db(self.db_path) as conn:
            for raw_job_id in job_ids:
                try:
                    job_id = int(raw_job_id)
                except (TypeError, ValueError):
                    continue
                ensure_job_metrics(conn, job_id)
                update_job_metrics(conn, job_id, last_rollout_sha=promoted_sha)

    def _record_attempt(self, cluster_id: int, commit_sha: str, status: str) -> None:
        with init_db(self.db_path) as conn:
            record_repair_rollout(
                conn,
                cluster_id,
                commit_sha=commit_sha,
                status=status,
            )

    def _record_failed_attempt(
        self,
        packet: RepairPacket,
        *,
        commit_sha: str,
        rollout_status: str,
        reason: str,
        detail: str | None = None,
    ) -> RepairAttemptResult:
        self._record_attempt(packet.cluster_id, commit_sha, rollout_status)
        with init_db(self.db_path) as conn:
            attempt_count = count_repair_rollouts(conn, packet.cluster_id)
            if attempt_count >= self.max_rollouts_per_cluster:
                pass
            else:
                update_repair_cluster(conn, packet.cluster_id, status="open", latest_summary=detail or reason)
        if attempt_count >= self.max_rollouts_per_cluster:
            self._mark_cluster_exhausted(packet, reason=detail or reason)
        return RepairAttemptResult(status="failed", reason=reason)

    def _mark_cluster_exhausted(self, packet: RepairPacket, *, reason: str) -> None:
        summary = f"Repair supervisor exhausted bounded attempts for {packet.fingerprint}: {reason}"
        with init_db(self.db_path) as conn:
            update_repair_cluster(
                conn,
                packet.cluster_id,
                status="exhausted",
                eligibility="manual_only",
                latest_summary=summary,
            )
            stop_jobs_for_exhausted_repair_cluster(
                conn,
                packet.job_ids,
                cluster_summary=summary,
                initiator="repair_supervisor",
            )

    def _exhaust_existing_cluster(self, cluster_row: dict, *, reason: str) -> None:
        packet = self._build_repair_packet(int(cluster_row["id"]))
        self._mark_cluster_exhausted(packet, reason=reason)

    def _monitor_active_rollouts(self) -> bool:
        with init_db(self.db_path) as conn:
            rollouts = repair_rollouts.list_active_rollouts(conn)
            if not rollouts:
                return False
            for rollout in rollouts:
                confirmation = str(rollout.get("status") or "") == "paused_pending_confirmation"
                evaluation = repair_rollouts.evaluate_rollout(conn, rollout, confirmation=confirmation)
                if evaluation.action == "monitor":
                    repair_rollouts.update_rollout_status(
                        conn,
                        int(rollout["id"]),
                        status="active",
                        post_fix_metrics=evaluation.post_fix_metrics,
                        output_root=self._output_root,
                    )
                    continue

                if evaluation.action == "pause":
                    repair_rollouts.update_rollout_status(
                        conn,
                        int(rollout["id"]),
                        status="paused_pending_confirmation",
                        post_fix_metrics={**evaluation.post_fix_metrics, "reason": evaluation.reason},
                        output_root=self._output_root,
                    )
                    return True

                if evaluation.action == "clear":
                    repair_rollouts.clear_repair_queue_pause(conn)
                    repair_rollouts.update_rollout_status(
                        conn,
                        int(rollout["id"]),
                        status="monitoring_resumed",
                        post_fix_metrics=evaluation.post_fix_metrics,
                        output_root=self._output_root,
                    )
                    return True

                if evaluation.action == "revert":
                    try:
                        baseline = json.loads(str(rollout.get("baseline_metrics_json") or "{}"))
                    except json.JSONDecodeError:
                        baseline = {}
                    monitored_job_ids = [
                        int(job_id)
                        for job_id in baseline.get("monitored_job_ids", [])
                        if str(job_id).strip()
                    ]
                    revert_sha = self._revert_main(str(rollout["commit_sha"]))
                    self._sync_runtime_repo(revert_sha)
                    self._requeue_jobs(monitored_job_ids)
                    repair_rollouts.update_rollout_status(
                        conn,
                        int(rollout["id"]),
                        status="reverted",
                        post_fix_metrics=evaluation.post_fix_metrics,
                        revert_sha=revert_sha,
                        output_root=self._output_root,
                    )
                    return True
            return False

    def _run_command_sequence(
        self,
        commands: list[list[str]],
        cwd: Path,
        *,
        cluster_id: int | None = None,
    ) -> bool:
        for command in commands:
            result = run_worker_subprocess(command, cwd=cwd, capture_output=True, text=True)
            if result.returncode != 0:
                if cluster_id is not None:
                    log.warning("repair verification failed for cluster %d: %s", cluster_id, result.stderr.strip())
                return False
        return True

    def _regression_test_paths_for_packet(self, *, board: str, phase: str, failure_type: str) -> list[str]:
        candidates: list[str] = []
        board_test_path = self._existing_repo_test_path(f"tests/test_autofill_{board}.py")
        if board_test_path:
            candidates.append(board_test_path)
        if phase in {"draft_audit", "stopped_audit"}:
            candidates.append("tests/test_pipeline_orchestrator.py")
        if failure_type == "rendered_audit_mismatch":
            candidates.append("tests/test_pipeline_orchestrator.py")
        return self._real_repo_test_paths(candidates)

    def _verification_test_paths_for_packet(
        self,
        *,
        board: str,
        phase: str,
        failure_type: str,
        likely_files: list[str],
    ) -> list[str]:
        candidates = self._regression_test_paths_for_packet(board=board, phase=phase, failure_type=failure_type)
        if phase in {"draft_audit", "stopped_audit"} or failure_type == "rendered_audit_mismatch":
            candidates.append("tests/test_pipeline_audit_loop.py")
        if {"scripts/pipeline_orchestrator.py", "scripts/job_db.py"} & set(likely_files):
            candidates.append("tests/test_pipeline_orchestrator.py")
        return self._real_repo_test_paths(candidates)

    @staticmethod
    def _likely_files_for_packet(*, board: str, phase: str) -> list[str]:
        files: list[str] = []
        if board and board != "unknown":
            files.append(f"scripts/autofill_{board}.py")
        if phase in {"draft_audit", "stopped_audit"}:
            files.append("scripts/pipeline_orchestrator.py")
        files.append("scripts/repair_supervisor.py")
        return list(dict.fromkeys(files))

    @staticmethod
    def _regression_filter_for_packet(*, board: str, phase: str, failure_type: str) -> str:
        tokens = [board, phase, failure_type]
        if failure_type == "rendered_audit_mismatch":
            tokens.append("rendered")
        filtered = [token.replace("-", "_") for token in tokens if token and token != "unknown"]
        return " or ".join(filtered) if filtered else ""

    @staticmethod
    def _pytest_commands_for_paths(paths: list[str], *, keyword: str) -> list[list[str]]:
        normalized = [path for path in paths if path]
        if not normalized:
            return []
        commands: list[list[str]] = []
        for path in normalized:
            command = ["uv", "run", "python", "-m", "pytest", path]
            if keyword:
                command.extend(["-k", keyword])
            command.append("-v")
            commands.append(command)
        return commands

    def _touches_shared_pipeline_surfaces(self, packet: RepairPacket, changed_paths: list[str]) -> bool:
        changed = set(changed_paths)
        likely = set(packet.likely_files)
        return bool((changed | likely) & _SHARED_PIPELINE_SURFACES)

    def _canary_result_counts_as_success(self, packet: RepairPacket, result: CanaryRerunResult) -> bool:
        del packet
        if result.fingerprint_recurred:
            return False
        if result.status == "draft":
            return True
        return result.status in {"stopped", "submitted"} and (
            result.failure_type or ""
        ) in _CANARY_TRUTHFUL_TERMINAL_FAILURE_TYPES

    def _existing_repo_test_path(self, path: str) -> str | None:
        candidate = str(path or "").strip()
        if not candidate:
            return None
        return candidate if (self.source_root / candidate).is_file() else None

    def _real_repo_test_paths(self, paths: list[str]) -> list[str]:
        realized = [path for path in (self._existing_repo_test_path(candidate) for candidate in paths) if path]
        return list(dict.fromkeys(realized))

    @staticmethod
    def _coerce_job_ids(raw_value: object) -> list[int]:
        if raw_value is None:
            return []
        if isinstance(raw_value, list):
            values = raw_value
        else:
            try:
                values = json.loads(str(raw_value))
            except (TypeError, json.JSONDecodeError):
                values = []
        job_ids: list[int] = []
        for value in values:
            try:
                job_ids.append(int(value))
            except (TypeError, ValueError):
                continue
        return sorted(set(job_ids))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    config = RepairSupervisorConfig.from_env(os.environ)
    log.info(
        "repair supervisor started (provider=%s, model=%s, reasoning_effort=%s)",
        config.provider,
        config.model,
        config.reasoning_effort,
    )

    stop_event = threading.Event()

    def _shutdown(signum: int, frame) -> None:
        log.info("repair supervisor received %s", signal.Signals(signum).name)
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    project_root = Path(__file__).resolve().parent.parent
    supervisor = RepairSupervisor(project_root=project_root, db_path=project_root / "jobs.db")
    supervisor.run_forever(stop_event=stop_event)


if __name__ == "__main__":
    main()
