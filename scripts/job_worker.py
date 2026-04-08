"""Worker pool with coordinator for processing jobs from the SQLite queue.

Each worker thread picks up jobs via the Coordinator, which rate-limits
by board so that only one job per board is processed concurrently.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import random
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from job_board_urls import (
    looks_like_breezy_url,
    looks_like_jazzhr_url,
    looks_like_jobvite_url,
    looks_like_paycor_url,
    looks_like_recruitee_url,
    looks_like_successfactors_url,
)
from job_db import (
    RETRY_AFTER_SENTINEL,
    claim_pending_job,
    get_pending_jobs,
    open_db,
    repair_stale_processing_jobs,
    repair_submission_locked_job,
    reset_stale_jobs,
)
from pipeline_orchestrator import process_job
from repair_runtime import ensure_repair_supervisor_running, repair_supervisor_enabled, stop_repair_supervisor

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

PROJECT_ROOT = SCRIPT_DIR.parent
PID_FILE = PROJECT_ROOT / "jobs.db.worker.pid"
STATE_FILE = PROJECT_ROOT / "jobs.db.worker_state.json"
COMMANDS_FILE = PROJECT_ROOT / "jobs.db.worker_commands.json"

log = logging.getLogger(__name__)

BOARD_COOLDOWN_SECONDS = int(os.environ.get("BOARD_COOLDOWN_SECONDS", "300"))
PROCESSING_REPAIR_INTERVAL_SECONDS = int(os.environ.get("PROCESSING_REPAIR_INTERVAL_SECONDS", "30"))
PROCESSING_REPAIR_STALE_THRESHOLD_SECONDS = int(os.environ.get("PROCESSING_REPAIR_STALE_THRESHOLD_SECONDS", "0"))
PROCESSING_REPAIR_BATCH_LIMIT = int(os.environ.get("PROCESSING_REPAIR_BATCH_LIMIT", "200"))

# Board detection patterns (lightweight fallback, no network calls)
_BOARD_PATTERNS: dict[str, tuple[str, ...]] = {
    "greenhouse": ("greenhouse.io",),
    "ashby": ("ashbyhq.com",),
    "lever": ("lever.co",),
    "workday": ("myworkdayjobs.com", "myworkdaysite.com"),
    "dover": ("app.dover.com",),
    "icims": ("icims.com",),
    "gem": ("gem.com",),
    "phenom": ("phenom.com",),
    "eightfold": ("eightfold.ai",),
    "smartrecruiters": ("smartrecruiters.com",),
    "workable": ("workable.com",),
    "comeet": ("comeet.com", "comeet.co"),
    "rippling": ("ats.rippling.com",),
    "uber": ("uber.com",),
    "motionrecruitment": ("motionrecruitment.com",),
    "reducto": ("reducto.ai",),
    "bytedance": ("jobs.bytedance.com", "joinbytedance.com"),
}


def _detect_board(url: str) -> str:
    """Detect board from URL using hostname patterns.

    Returns board name for known boards, or the hostname for unknown boards.
    Using hostname instead of a generic 'unknown' prevents unrelated companies
    from blocking each other in the rate limiter.
    """
    host = (urlparse(url).hostname or "").lower()
    if looks_like_successfactors_url(url):
        return "successfactors"
    if looks_like_breezy_url(url):
        return "breezy"
    if looks_like_recruitee_url(url):
        return "recruitee"
    if looks_like_jobvite_url(url):
        return "jobvite"
    if looks_like_jazzhr_url(url):
        return "jazzhr"
    if looks_like_paycor_url(url):
        return "paycor"
    for board, patterns in _BOARD_PATTERNS.items():
        if any(p in host for p in patterns):
            return board
    # Return hostname so different companies don't block each other
    return host or "unknown"


class Coordinator:
    """Polls DB for pending jobs, assigns to workers, rate limits by board."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()

    def _get_conn(self) -> sqlite3.Connection:
        """Return a per-thread SQLite connection (created on first access)."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = open_db(self._db_path, check_same_thread=False)
        return self._local.conn

    def _close_conn(self) -> None:
        """Close the current thread's coordinator connection, if any."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        self._local.conn = None
        try:
            conn.close()
        except Exception:
            pass

    def close(self) -> None:
        """Close any coordinator connection owned by the current thread."""
        self._close_conn()

    def _board_for_url(self, url: str) -> str:
        """Detect board from a job URL."""
        return _detect_board(url)

    def next_job(
        self,
        active_boards: set[str],
        *,
        board_cooldown_until=None,
        llm_cooldown_until=None,
    ) -> dict | None:
        """Get next pending job, atomically claiming it to prevent duplicates.

        Uses a CAS update to claim the job (status → 'resolving' or 'submitting')
        so no two workers process the same job. Board rate-limiting only applies
        to jobs that will interact with the browser (submitting, reanswering).
        """
        conn = self._get_conn()
        pending = get_pending_jobs(conn, limit=50)
        global_llm_cooldown = llm_cooldown_until() if llm_cooldown_until is not None else None
        if global_llm_cooldown is not None:
            log.info("next_job: skipping pending work — llm providers cooling down until %s", global_llm_cooldown)
            return None
        for job in pending:
            # Skip jobs already claimed by another worker
            progress = job.get("progress") or ""
            if progress.startswith("claimed:"):
                log.debug("next_job: skipping job %d — already claimed", job["id"])
                continue
            url = job.get("board_url") or job.get("url") or ""
            board = job.get("board") or (self._board_for_url(url) if url else "")
            if board and board_cooldown_until is not None:
                cooldown_until = board_cooldown_until(board)
                if cooldown_until is not None:
                    log.info(
                        "next_job: skipping job %d (board=%s) — rate-limited until %s",
                        job["id"],
                        board,
                        cooldown_until.isoformat(),
                    )
                    continue
            if job.get("status") in ("approved", "submitting", "reanswering"):
                if board in active_boards:
                    log.info(
                        "next_job: skipping job %d (board=%s) — board locked by active_boards=%s",
                        job["id"],
                        board,
                        active_boards,
                    )
                    continue
            # Atomically claim via CAS. The trick: always change the progress
            # field to include the worker thread ident so the UPDATE is a true
            # mutation. Two workers racing on the same row: only the first
            # succeeds because the second sees a different progress value.
            import threading as _th

            _tid = _th.current_thread().ident or 0
            claim_status = (
                "submitting"
                if job["status"] in ("approved", "submitting")
                else ("reanswering" if job["status"] == "reanswering" else "resolving")
            )
            old_progress = progress
            if claim_pending_job(
                conn,
                job_id=job["id"],
                expected_status=job["status"],
                expected_progress=old_progress,
                claim_status=claim_status,
                claim_progress=f"claimed:{_tid}",
            ):
                log.info(
                    "next_job: claimed job %d (status=%s, board=%s)", job["id"], job["status"], job.get("board", "?")
                )
                return job
            # Another worker already claimed it — log and try next
            log.warning(
                "next_job: CAS claim failed for job %d (status=%s) — another worker got it first",
                job["id"],
                job["status"],
            )
        return None


class WorkerPool:
    """Manages N concurrent worker threads.

    Each thread gets its own SQLite connection via ``_get_conn()`` (backed by
    ``threading.local``).  This avoids the corruption that occurs when multiple
    threads share a single ``sqlite3.Connection``.
    """

    def __init__(
        self,
        db_path: Path,
        num_workers: int = 40,
        headless: bool = True,
        headless_explicit: bool = False,
    ) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._num_workers = num_workers
        self._headless = headless
        self._headless_explicit = headless_explicit
        self._coordinator = Coordinator(db_path)
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._active_boards: set[str] = set()
        self._board_cooldowns: dict[str, datetime.datetime] = {}
        self._llm_cooldown_until: datetime.datetime | None = None
        self._lock = threading.Lock()
        self._assign_lock = threading.Lock()  # serialises next_job + add_active_board
        # Per-worker registry: worker_id → state dict
        self._worker_registry: dict[int, dict] = {}
        # Per-worker stop events for individual worker kill
        self._worker_stop_events: dict[int, threading.Event] = {}
        # Background thread for publishing state + processing commands
        self._state_thread: threading.Thread | None = None
        self._next_processing_repair_monotonic = 0.0

    def _get_conn(self) -> sqlite3.Connection:
        """Return a per-thread SQLite connection (created on first access)."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = open_db(self._db_path, check_same_thread=False)
        return self._local.conn

    def _close_conn(self) -> None:
        """Close the current thread's worker-pool connection, if any."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        self._local.conn = None
        try:
            conn.close()
        except Exception:
            pass

    def _add_active_board(self, board: str) -> None:
        with self._lock:
            self._active_boards.add(board)

    def _remove_active_board(self, board: str) -> None:
        with self._lock:
            self._active_boards.discard(board)

    def _get_active_boards(self) -> set[str]:
        with self._lock:
            return set(self._active_boards)

    def set_board_cooldown(self, board: str) -> None:
        """Pause new work for a board after a rate-limit failure."""
        if not board:
            return
        cooldown_until = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=BOARD_COOLDOWN_SECONDS)
        with self._lock:
            self._board_cooldowns[board] = cooldown_until
        log.warning("Board %s rate-limited until %s", board, cooldown_until.isoformat())

    def get_board_cooldown_until(self, board: str) -> datetime.datetime | None:
        """Return the active cooldown for a board, pruning expired entries."""
        if not board:
            return None
        now = datetime.datetime.now(datetime.UTC)
        with self._lock:
            cooldown_until = self._board_cooldowns.get(board)
            if cooldown_until is None:
                return None
            if cooldown_until <= now:
                self._board_cooldowns.pop(board, None)
                return None
            return cooldown_until

    def is_board_rate_limited(self, board: str) -> bool:
        """Check whether a board is currently in cooldown."""
        return self.get_board_cooldown_until(board) is not None

    def set_llm_cooldown_until(self, cooldown_until: datetime.datetime | None) -> None:
        """Pause new work after all configured LLM providers are rate-limited."""
        if cooldown_until is None:
            return
        normalized = cooldown_until.astimezone(datetime.UTC)
        if normalized <= datetime.datetime.now(datetime.UTC):
            return
        with self._lock:
            current = self._llm_cooldown_until
            if current is None or normalized > current:
                self._llm_cooldown_until = normalized
        log.warning("LLM providers rate-limited until %s", normalized.isoformat())

    def get_llm_cooldown_until(self) -> datetime.datetime | None:
        """Return the active global LLM cooldown, pruning it once expired."""
        now = datetime.datetime.now(datetime.UTC)
        with self._lock:
            cooldown_until = self._llm_cooldown_until
            if cooldown_until is None:
                return None
            if cooldown_until <= now:
                self._llm_cooldown_until = None
                return None
            return cooldown_until

    @staticmethod
    def _parse_retry_after_timestamp(value: object) -> datetime.datetime | None:
        raw = str(value or "").strip()
        if not raw or raw == RETRY_AFTER_SENTINEL:
            return None
        try:
            parsed = datetime.datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.UTC)
        return parsed.astimezone(datetime.UTC)

    def _is_worker_stopped(self, worker_id: int) -> bool:
        """Check if either global or per-worker stop event is set."""
        if self._stop_event.is_set():
            return True
        ev = self._worker_stop_events.get(worker_id)
        return ev is not None and ev.is_set()

    def _register_worker(
        self,
        worker_id: int,
        status: str = "idle",
        job: dict | None = None,
        phase: str = "",
        board: str = "",
        auto_submit: bool = False,
    ) -> None:
        """Update the worker registry entry."""
        with self._lock:
            entry = {
                "worker_id": worker_id,
                "status": status,
                "job_id": job["id"] if job else None,
                "company": job.get("company", "") if job else "",
                "role_title": job.get("role_title", "") if job else "",
                "board": board,
                "phase": phase,
                "auto_submit": auto_submit,
                "start_time": (datetime.datetime.now(datetime.UTC).isoformat() if status == "busy" else None),
                "progress": job.get("progress", "") if job else "",
            }
            self._worker_registry[worker_id] = entry

    def _unregister_worker(self, worker_id: int) -> None:
        """Remove worker from registry."""
        with self._lock:
            self._worker_registry.pop(worker_id, None)

    def _worker_loop(self, worker_id: int) -> None:
        """Main loop for a single worker thread.

        Each thread creates its own SQLite connection so there is no
        cross-thread sharing of connection state.
        """
        conn = self._get_conn()  # per-thread via threading.local
        log.info("worker %d started", worker_id)
        self._register_worker(worker_id, status="idle")
        try:
            while not self._is_worker_stopped(worker_id):
                # Atomically: read active boards -> pick a job -> mark board active.
                # This prevents two workers from both grabbing a job on the same board.
                with self._assign_lock:
                    active = self._get_active_boards()
                    job = self._coordinator.next_job(
                        active,
                        board_cooldown_until=self.get_board_cooldown_until,
                        llm_cooldown_until=self.get_llm_cooldown_until,
                    )
                    if job is None:
                        pass  # handled below
                    else:
                        url = job.get("board_url") or job.get("url") or ""
                        board = job.get("board") or _detect_board(url)
                        job_id = job["id"]
                        # Only rate-limit board for jobs that will hit the browser
                        _needs_board_lock = job.get("status") in (
                            "approved",
                            "submitting",
                            "reanswering",
                            "autofilling",
                        )
                        if _needs_board_lock:
                            self._add_active_board(board)

                if job is None:
                    # No eligible jobs — wait and retry
                    self._register_worker(worker_id, status="idle")
                    self._stop_event.wait(timeout=2.0)
                    continue
                # Stagger job starts to avoid simultaneous API hits
                stagger = random.uniform(0.5, 3.0)
                self._stop_event.wait(timeout=stagger)
                if self._is_worker_stopped(worker_id):
                    break
                # Determine initial phase from claimed status
                initial_phase = job.get("status", "resolving")
                if initial_phase in ("approved", "submitting", "reanswering"):
                    phase = "submitting"
                elif initial_phase == "autofilling":
                    phase = "autofilling"
                else:
                    phase = "resolving"
                is_submit = initial_phase in ("approved", "submitting")
                self._register_worker(
                    worker_id,
                    status="busy",
                    job=job,
                    phase=phase,
                    board=board,
                    auto_submit=is_submit,
                )
                try:
                    log.info("worker %d processing job %d (%s, board=%s)", worker_id, job_id, url, board)
                    _hl = self._headless if self._headless_explicit else None
                    result = process_job(
                        conn,
                        job_id,
                        worker_id=worker_id,
                        headless=_hl,
                        auto_submit=(initial_phase in ("approved", "submitting")),
                    )
                    # Auto-retry transient failures
                    if result == "stopped":
                        from job_db import get_job as _get_job
                        from pipeline_orchestrator import _auto_retry_if_transient, _is_rate_limit_error

                        _job = _get_job(conn, job_id)
                        if _job:
                            if board and _is_rate_limit_error(_job.get("error_message", "")):
                                self.set_board_cooldown(board)
                            retry_result = _auto_retry_if_transient(
                                conn,
                                job_id,
                                _job.get("error_message", ""),
                                failure_type=_job.get("failure_type"),
                            )
                            if retry_result == "queued" and _job.get("failure_type") == "llm_rate_limited":
                                refreshed_job = _get_job(conn, job_id)
                                if refreshed_job:
                                    self.set_llm_cooldown_until(
                                        self._parse_retry_after_timestamp(refreshed_job.get("retry_after"))
                                    )
                except Exception:
                    log.exception("worker %d: unhandled error processing job %d", worker_id, job_id)
                finally:
                    if _needs_board_lock:
                        self._remove_active_board(board)
                    self._register_worker(worker_id, status="idle")

                # Brief pause between jobs
                self._stop_event.wait(timeout=1.0)
        finally:
            self._close_conn()
            self._coordinator.close()
            self._unregister_worker(worker_id)
            log.info("worker %d stopped", worker_id)

    def get_worker_states(self) -> list[dict]:
        """Return a snapshot of all worker states from the registry."""
        with self._lock:
            states = [dict(v) for v in self._worker_registry.values()]
        # Enrich busy workers with live progress from the DB
        conn = self._get_conn()  # per-thread (state publisher thread)
        for state in states:
            if state["status"] == "busy" and state["job_id"]:
                try:
                    row = conn.execute(
                        "SELECT status, progress FROM jobs WHERE id = ?",
                        (state["job_id"],),
                    ).fetchone()
                    if row:
                        state["phase"] = row["status"] if row["status"] else state["phase"]
                        state["progress"] = row["progress"] if row["progress"] else ""
                except Exception:
                    pass
        return states

    def _publish_state(self) -> None:
        """Write worker state to JSON file for the web server to read."""
        try:
            states = self.get_worker_states()
            STATE_FILE.write_text(json.dumps(states, default=str), encoding="utf-8")
        except Exception:
            log.debug("Failed to publish worker state", exc_info=True)

    def _process_commands(self) -> None:
        """Read and execute commands from the command file."""
        if not COMMANDS_FILE.exists():
            return
        try:
            raw = COMMANDS_FILE.read_text(encoding="utf-8").strip()
            if not raw:
                return
            data = json.loads(raw)
            commands = data.get("commands", [])
            if not commands:
                return
            # Clear the file first to avoid re-processing
            COMMANDS_FILE.write_text("{}", encoding="utf-8")
            for cmd in commands:
                cmd_type = cmd.get("type", "")
                wid = cmd.get("worker_id")
                if cmd_type == "stop_worker" and wid is not None:
                    self._stop_single_worker(wid)
                elif cmd_type == "kill_worker" and wid is not None:
                    self._kill_single_worker(wid)
                elif cmd_type == "scale":
                    count = cmd.get("count", self._num_workers)
                    self._scale_workers(count)
                else:
                    log.warning("Unknown worker command: %s", cmd_type)
        except Exception:
            log.debug("Failed to process worker commands", exc_info=True)

    def _repair_stale_processing_jobs_if_due(self) -> None:
        """Periodically reconcile stale processing rows back to their true status."""
        interval = max(PROCESSING_REPAIR_INTERVAL_SECONDS, 0)
        now = time.monotonic()
        if interval and now < self._next_processing_repair_monotonic:
            return
        self._next_processing_repair_monotonic = now + interval

        with self._lock:
            active_job_ids = {
                int(entry["job_id"])
                for entry in self._worker_registry.values()
                if entry.get("status") == "busy" and entry.get("job_id") is not None
            }

        conn = self._get_conn()
        try:
            repaired = repair_stale_processing_jobs(
                conn,
                stale_threshold_seconds=PROCESSING_REPAIR_STALE_THRESHOLD_SECONDS,
                limit=PROCESSING_REPAIR_BATCH_LIMIT,
                exclude_job_ids=active_job_ids,
            )
        except Exception:
            log.exception("Failed stale processing repair pass")
            return

        if repaired.get("changed"):
            log.info("Repaired stale processing rows: %s", repaired)

    def _stop_single_worker(self, worker_id: int) -> None:
        """Gracefully stop a single worker."""
        ev = self._worker_stop_events.get(worker_id)
        if ev:
            with self._lock:
                entry = self._worker_registry.get(worker_id)
                if entry:
                    entry["status"] = "stopping"
            ev.set()
            log.info("Sent stop signal to worker %d", worker_id)

    def _kill_single_worker(self, worker_id: int) -> None:
        """Force-stop a worker and requeue its current job.

        Submit-phase jobs (submitting, reanswering, awaiting_captcha) are reset
        to 'draft' — they need explicit re-approval.  Other active jobs are
        safe to requeue automatically.
        """
        with self._lock:
            entry = self._worker_registry.get(worker_id)
            current_job_id = entry["job_id"] if entry else None
            if entry:
                entry["status"] = "stopping"
        # Set the stop event
        ev = self._worker_stop_events.get(worker_id)
        if ev:
            ev.set()
        # Requeue the job if one was active
        if current_job_id:
            conn = self._get_conn()  # per-thread (state publisher thread)
            try:
                lock_row = conn.execute(
                    "SELECT status, confirmed_at, submission_lock_state FROM jobs WHERE id = ?",
                    (current_job_id,),
                ).fetchone()
                if lock_row:
                    lock_state = str(lock_row["submission_lock_state"] or "").strip()
                    is_locked = lock_state == "locked" or (
                        lock_state != "unlocked_for_resubmit" and bool(lock_row["confirmed_at"])
                    )
                    if is_locked:
                        repaired = repair_submission_locked_job(conn, current_job_id, initiator="system")
                        if repaired:
                            log.info(
                                "Repaired locked submitted job %d from killed worker %d",
                                current_job_id,
                                worker_id,
                            )
                        else:
                            log.info(
                                "Skipped requeue for locked job %d from killed worker %d",
                                current_job_id,
                                worker_id,
                            )
                        return
                # Submit-phase jobs → draft (needs re-approval)
                _SUBMIT_PHASES = ("approved", "submitting", "reanswering", "awaiting_captcha")
                _ph = ",".join("?" * len(_SUBMIT_PHASES))
                cur = conn.execute(
                    "UPDATE jobs SET status = 'draft', provider = NULL, retry_after = ?, "
                    "progress = 'Submit interrupted — needs re-approval' "
                    f"WHERE id = ? AND status IN ({_ph})",
                    (RETRY_AFTER_SENTINEL, current_job_id, *_SUBMIT_PHASES),
                )
                conn.commit()
                if cur.rowcount > 0:
                    log.info(
                        "Reset job %d to draft from killed worker %d (was in submit phase)", current_job_id, worker_id
                    )
                else:
                    # autofilling → queued (no draft yet, safe to retry)
                    cur2 = conn.execute(
                        "UPDATE jobs SET status = 'queued', provider = NULL, retry_after = ?, progress = '' "
                        "WHERE id = ? AND status = 'autofilling'",
                        (RETRY_AFTER_SENTINEL, current_job_id),
                    )
                    conn.commit()
                    if cur2.rowcount > 0:
                        log.info("Requeued autofilling job %d from killed worker %d", current_job_id, worker_id)
                    else:
                        # Other non-submit-phase jobs → queued (safe to retry)
                        conn.execute(
                            "UPDATE jobs SET status = 'queued', provider = NULL, retry_after = ?, progress = '' "
                            "WHERE id = ? AND status NOT IN "
                            "('submitted', 'draft', 'archived', 'stopped')",
                            (RETRY_AFTER_SENTINEL, current_job_id),
                        )
                        conn.commit()
                        log.info("Requeued job %d from killed worker %d", current_job_id, worker_id)
            except Exception:
                log.exception("Failed to requeue job %d", current_job_id)

    def _scale_workers(self, target_count: int) -> None:
        """Scale the worker pool to the target number of workers."""
        target_count = max(1, min(target_count, 100))
        current = len(self._threads)
        if target_count > current:
            # Add more workers
            for i in range(current + 1, target_count + 1):
                ev = threading.Event()
                self._worker_stop_events[i] = ev
                t = threading.Thread(
                    target=self._worker_loop,
                    args=(i,),
                    name=f"job-worker-{i}",
                    daemon=True,
                )
                self._threads.append(t)
                t.start()
            log.info("Scaled up from %d to %d workers", current, target_count)
        elif target_count < current:
            # Stop excess workers
            for i in range(target_count + 1, current + 1):
                ev = self._worker_stop_events.get(i)
                if ev:
                    ev.set()
            log.info("Scaling down from %d to %d workers", current, target_count)
        self._num_workers = target_count

    def _state_publish_loop(self) -> None:
        """Background loop: publish state and process commands every 2 seconds."""
        try:
            while not self._stop_event.is_set():
                self._repair_stale_processing_jobs_if_due()
                self._publish_state()
                self._process_commands()
                self._stop_event.wait(timeout=2.0)
        finally:
            self._close_conn()
            # Final cleanup
            try:
                STATE_FILE.unlink(missing_ok=True)
            except Exception:
                pass

    def start(self) -> None:
        """Start the worker pool."""
        # init_db runs schema + migrations once; worker threads use open_db
        from job_db import init_db

        conn = init_db(self._db_path, check_same_thread=False)
        reset_stale_jobs(conn)
        conn.close()

        self._stop_event.clear()
        self._threads = []
        self._worker_registry = {}
        self._worker_stop_events = {}
        self._board_cooldowns = {}
        for i in range(1, self._num_workers + 1):
            ev = threading.Event()
            self._worker_stop_events[i] = ev
            t = threading.Thread(
                target=self._worker_loop,
                args=(i,),
                name=f"job-worker-{i}",
                daemon=True,
            )
            self._threads.append(t)
            t.start()
        # Start state publishing thread
        self._state_thread = threading.Thread(
            target=self._state_publish_loop,
            name="worker-state-publisher",
            daemon=True,
        )
        self._state_thread.start()
        log.info("started %d workers", self._num_workers)

    def stop(self) -> None:
        """Signal all workers to stop and wait for them to finish."""
        self._stop_event.set()
        # Also set all per-worker stop events
        for ev in self._worker_stop_events.values():
            ev.set()
        for t in self._threads:
            t.join(timeout=30)
        if self._state_thread:
            self._state_thread.join(timeout=5)
            self._state_thread = None
        self._threads = []
        self._worker_registry = {}
        self._close_conn()
        self._coordinator.close()
        log.info("all workers stopped")

    def close(self) -> None:
        """Release worker-pool resources for the current process."""
        self.stop()

    @property
    def is_running(self) -> bool:
        return bool(self._threads) and not self._stop_event.is_set()


def _kill_stale_workers(my_pid: int) -> None:
    """Kill any existing job_worker.py processes and their spawned claude
    auto-fix subprocesses (except ourselves)."""
    import subprocess as _sp

    # Kill stale job_worker.py processes
    for pattern in ("job_worker.py", "claude.*autofill.*failed"):
        try:
            result = _sp.run(
                ["pgrep", "-f", pattern],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in result.stdout.strip().splitlines():
                pid = int(line.strip())
                if pid == my_pid or pid == os.getppid():
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                    log.info("Killed stale process %d (pattern=%s)", pid, pattern)
                except (OSError, ProcessLookupError):
                    pass
        except Exception:
            pass


def main() -> None:
    """CLI entry point. Parses --workers N, --headless flags."""
    parser = argparse.ArgumentParser(description="Job worker pool")
    parser.add_argument("--workers", type=int, default=40, help="Number of concurrent workers (default: 40)")
    parser.add_argument("--headless", action="store_true", default=None, help="Force browsers to headless mode")
    parser.add_argument("--no-headless", dest="headless", action="store_false", help="Force browsers to visible mode")
    args = parser.parse_args()
    _headless_explicit = args.headless is not None
    _headless = args.headless if _headless_explicit else True

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    db_path = PROJECT_ROOT / "jobs.db"

    # Kill any stale worker processes before starting (prevents accumulation)
    _kill_stale_workers(os.getpid())

    # Write PID file for stop detection
    PID_FILE.write_text(str(os.getpid()))
    log.info("PID %d written to %s", os.getpid(), PID_FILE)

    supervisor_enabled = repair_supervisor_enabled()
    supervisor_started_here = False
    pool: WorkerPool | None = None

    cleaned_up = False

    def _cleanup() -> None:
        nonlocal cleaned_up
        if cleaned_up:
            return
        cleaned_up = True
        if pool is not None:
            pool.stop()
        if supervisor_enabled and supervisor_started_here:
            stop_repair_supervisor(project_root=PROJECT_ROOT)
        PID_FILE.unlink(missing_ok=True)

    try:
        if supervisor_enabled:
            supervisor_started_here = ensure_repair_supervisor_running(project_root=PROJECT_ROOT)
        pool = WorkerPool(db_path, num_workers=args.workers, headless=_headless, headless_explicit=_headless_explicit)
        pool.start()
    except Exception:
        _cleanup()
        raise

    # Handle graceful shutdown via SIGINT/SIGTERM
    def _shutdown(signum: int, frame) -> None:
        sig_name = signal.Signals(signum).name
        log.info("received %s — shutting down", sig_name)
        _cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Keep main thread alive
    try:
        while pool is not None and pool.is_running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
