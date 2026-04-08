"""Runtime helpers for the singleton repair supervisor process."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from worker_subprocess import popen_worker_subprocess

_REPAIR_SUPERVISOR_PID_BASENAME = "jobs.db.repair_supervisor.pid"
_REPAIR_SUPERVISOR_START_LOCK_BASENAME = "jobs.db.repair_supervisor.start.lock"
_REPAIR_SUPERVISOR_START_LOCK_WAIT_TIMEOUT_SECONDS = 5.0
_REPAIR_SUPERVISOR_START_LOCK_POLL_INTERVAL_SECONDS = 0.05
_repair_supervisor_proc: subprocess.Popen | None = None


def _bool_from_env(name: str, default: bool, *, environ: Mapping[str, str] | None = None) -> bool:
    env = environ or os.environ
    raw = str(env.get(name, "")).strip().casefold()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return default


def repair_supervisor_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return _bool_from_env("JOB_ASSETS_ENABLE_REPAIR_SUPERVISOR", False, environ=environ)


@dataclass(frozen=True)
class RepairSupervisorConfig:
    provider: str
    model: str
    reasoning_effort: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> RepairSupervisorConfig:
        return cls(
            provider=env.get("ASSET_REPAIR_LLM_PROVIDER", "openai"),
            model=env.get("ASSET_REPAIR_OPENAI_MODEL", "gpt-5.4"),
            reasoning_effort=env.get("ASSET_REPAIR_OPENAI_REASONING_EFFORT", "xhigh"),
        )


def _pid_path(project_root: Path) -> Path:
    return project_root / _REPAIR_SUPERVISOR_PID_BASENAME


def _startup_lock_path(project_root: Path) -> Path:
    return project_root / _REPAIR_SUPERVISOR_START_LOCK_BASENAME


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_startup_lock_owner_pid(lock_path: Path) -> int | None:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _acquire_startup_lock(*, project_root: Path) -> bool:
    lock_path = _startup_lock_path(project_root)
    deadline = time.monotonic() + _REPAIR_SUPERVISOR_START_LOCK_WAIT_TIMEOUT_SECONDS

    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if is_repair_supervisor_running(project_root=project_root):
                return False
            owner_pid = _read_startup_lock_owner_pid(lock_path)
            if owner_pid is not None and _process_exists(owner_pid):
                time.sleep(_REPAIR_SUPERVISOR_START_LOCK_POLL_INTERVAL_SECONDS)
                continue
            if time.monotonic() >= deadline:
                lock_path.unlink(missing_ok=True)
                deadline = time.monotonic() + _REPAIR_SUPERVISOR_START_LOCK_WAIT_TIMEOUT_SECONDS
                continue
            time.sleep(_REPAIR_SUPERVISOR_START_LOCK_POLL_INTERVAL_SECONDS)
            continue

        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        return True


def _release_startup_lock(*, project_root: Path) -> None:
    _startup_lock_path(project_root).unlink(missing_ok=True)


def is_repair_supervisor_running(*, project_root: Path) -> bool:
    global _repair_supervisor_proc
    if _repair_supervisor_proc is not None and _repair_supervisor_proc.poll() is None:
        return True

    pid_path = _pid_path(project_root)
    if not pid_path.exists():
        return False

    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        pid_path.unlink(missing_ok=True)
        return False
    return True


def ensure_repair_supervisor_running(*, project_root: Path, environ: Mapping[str, str] | None = None) -> bool:
    global _repair_supervisor_proc
    env = dict(os.environ if environ is None else environ)
    if not repair_supervisor_enabled(env):
        return False
    if is_repair_supervisor_running(project_root=project_root):
        return False
    if not _acquire_startup_lock(project_root=project_root):
        return False

    try:
        if is_repair_supervisor_running(project_root=project_root):
            return False

        config = RepairSupervisorConfig.from_env(env)
        env.update(
            {
                "ASSET_REPAIR_LLM_PROVIDER": config.provider,
                "ASSET_REPAIR_OPENAI_MODEL": config.model,
                "ASSET_REPAIR_OPENAI_REASONING_EFFORT": config.reasoning_effort,
            }
        )

        _repair_supervisor_proc = popen_worker_subprocess(
            [
                "uv",
                "run",
                "--project",
                str(project_root),
                "python",
                str(project_root / "scripts" / "repair_supervisor.py"),
            ],
            cwd=project_root,
            env=env,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _pid_path(project_root).write_text(str(_repair_supervisor_proc.pid), encoding="utf-8")
        return True
    except Exception:
        if _repair_supervisor_proc is not None and _repair_supervisor_proc.poll() is None:
            stop_repair_supervisor(project_root=project_root)
        raise
    finally:
        _release_startup_lock(project_root=project_root)


def stop_repair_supervisor(*, project_root: Path) -> None:
    global _repair_supervisor_proc
    pid_path = _pid_path(project_root)
    pid: int | None = None
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None

    if _repair_supervisor_proc is not None and _repair_supervisor_proc.poll() is None:
        _repair_supervisor_proc.terminate()
        try:
            _repair_supervisor_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _repair_supervisor_proc.kill()
            _repair_supervisor_proc.wait(timeout=2)
        pid = None

    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    _repair_supervisor_proc = None
    pid_path.unlink(missing_ok=True)
