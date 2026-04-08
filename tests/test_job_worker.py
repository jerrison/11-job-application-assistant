# tests/test_job_worker.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import job_worker
import pytest
from job_db import RETRY_AFTER_SENTINEL, add_job, get_job, init_db, set_runtime_flag, update_status
from job_worker import Coordinator, WorkerPool


@pytest.fixture
def db_path(tmp_path):
    """Return path to a test database (initialised on first connection)."""
    return tmp_path / "test_jobs.db"


@pytest.fixture
def db(db_path):
    """Return a connection to the test database for setup/verification."""
    conn = init_db(db_path, check_same_thread=False)
    yield conn
    conn.close()


@pytest.fixture
def coordinator_factory(db_path):
    """Create coordinators and ensure their connections are closed after each test."""
    coordinators: list[Coordinator] = []

    def factory() -> Coordinator:
        coord = Coordinator(db_path)
        coordinators.append(coord)
        return coord

    yield factory

    for coord in coordinators:
        coord.close()


# ── Coordinator Tests ────────────────────────────────────────────────────────


class TestCoordinatorNextJob:
    """Test Coordinator.next_job() ordering and board rate limiting."""

    def test_returns_highest_priority_first(self, db, coordinator_factory):
        add_job(db, url="https://boards.greenhouse.io/a/jobs/1", priority=0)
        add_job(db, url="https://boards.greenhouse.io/b/jobs/2", priority=10)
        coord = coordinator_factory()
        job = coord.next_job(active_boards=set())
        assert job is not None
        assert job["id"] == 2  # higher priority

    def test_returns_oldest_when_same_priority(self, db, coordinator_factory):
        add_job(db, url="https://boards.greenhouse.io/a/jobs/1", priority=0)
        add_job(db, url="https://jobs.lever.co/company/abc", priority=0)
        coord = coordinator_factory()
        job = coord.next_job(active_boards=set())
        assert job is not None
        assert job["id"] == 1  # created first

    def test_queued_jobs_ignore_board_limits(self, db, coordinator_factory):
        """Queued jobs (asset generation) are never blocked by active boards."""
        add_job(db, url="https://boards.greenhouse.io/a/jobs/1")
        coord = coordinator_factory()
        job = coord.next_job(active_boards={"greenhouse"})
        assert job is not None  # queued = asset gen, no board limit

    def test_submitting_jobs_respect_board_limits(self, db, coordinator_factory):
        """Submitting jobs (browser interaction) are blocked by active boards."""
        j1 = add_job(db, url="https://boards.greenhouse.io/a/jobs/1")
        update_status(db, j1, "submitting")
        j2 = add_job(db, url="https://jobs.lever.co/company/abc")
        update_status(db, j2, "submitting")
        coord = coordinator_factory()
        job = coord.next_job(active_boards={"greenhouse"})
        assert job is not None
        assert job["id"] == j2  # lever, since greenhouse is active

    def test_returns_none_when_all_submit_boards_active(self, db, coordinator_factory):
        j1 = add_job(db, url="https://boards.greenhouse.io/a/jobs/1")
        update_status(db, j1, "submitting")
        j2 = add_job(db, url="https://boards.greenhouse.io/b/jobs/2")
        update_status(db, j2, "submitting")
        coord = coordinator_factory()
        job = coord.next_job(active_boards={"greenhouse"})
        assert job is None

    def test_returns_none_when_no_queued_jobs(self, db_path, coordinator_factory):
        # Init DB so tables exist
        init_db(db_path).close()
        coord = coordinator_factory()
        job = coord.next_job(active_boards=set())
        assert job is None

    def test_skips_non_queued_jobs(self, db, coordinator_factory):
        job_id = add_job(db, url="https://boards.greenhouse.io/a/jobs/1")
        update_status(db, job_id, "generating")
        coord = coordinator_factory()
        job = coord.next_job(active_boards=set())
        assert job is None

    def test_picks_up_submitting_jobs(self, db, coordinator_factory):
        """Approved drafts (status=submitting) should be picked up by workers."""
        job_id = add_job(db, url="https://boards.greenhouse.io/a/jobs/1")
        update_status(db, job_id, "submitting")
        coord = coordinator_factory()
        job = coord.next_job(active_boards=set())
        assert job is not None
        assert job["id"] == job_id

    def test_submitting_prioritized_over_queued(self, db, coordinator_factory):
        """Approved drafts should be processed before queued jobs."""
        add_job(db, url="https://boards.greenhouse.io/a/jobs/1")
        s_id = add_job(db, url="https://boards.greenhouse.io/b/jobs/2")
        update_status(db, s_id, "submitting")
        coord = coordinator_factory()
        job = coord.next_job(active_boards=set())
        assert job["id"] == s_id

    def test_board_detection_from_url_for_submitting(self, db, coordinator_factory):
        """Board rate limiting applies to submitting jobs based on URL detection."""
        j = add_job(db, url="https://jobs.ashbyhq.com/company/abc")
        update_status(db, j, "submitting")
        coord = coordinator_factory()
        job = coord.next_job(active_boards={"ashby"})
        assert job is None  # ashby is active + job is submitting

    def test_unknown_board_not_rate_limited(self, db, coordinator_factory):
        """Jobs with unrecognizable board URLs are not rate-limited out."""
        add_job(db, url="https://some-custom-site.com/careers/123")
        coord = coordinator_factory()
        job = coord.next_job(active_boards={"greenhouse"})
        assert job is not None  # unknown board != greenhouse

    def test_multiple_submit_boards_some_active(self, db, coordinator_factory):
        j1 = add_job(db, url="https://boards.greenhouse.io/a/jobs/1")
        update_status(db, j1, "submitting")
        j2 = add_job(db, url="https://jobs.lever.co/company/abc")
        update_status(db, j2, "submitting")
        j3 = add_job(db, url="https://jobs.ashbyhq.com/company/xyz")
        update_status(db, j3, "submitting")
        coord = coordinator_factory()
        job = coord.next_job(active_boards={"greenhouse", "lever"})
        assert job is not None
        assert job["id"] == j3  # only ashby is available

    def test_rate_limited_boards_are_skipped_for_queued_jobs(self, db, coordinator_factory, db_path):
        greenhouse_id = add_job(db, url="https://boards.greenhouse.io/a/jobs/1")
        lever_id = add_job(db, url="https://jobs.lever.co/company/abc")
        coord = coordinator_factory()
        pool = WorkerPool(db_path, num_workers=1, headless=True)
        pool.set_board_cooldown("greenhouse")

        job = coord.next_job(active_boards=set(), board_cooldown_until=pool.get_board_cooldown_until)

        assert job is not None
        assert job["id"] == lever_id
        assert job["id"] != greenhouse_id

    def test_llm_cooldown_skips_pending_jobs(self, db, coordinator_factory):
        add_job(db, url="https://boards.greenhouse.io/a/jobs/1")
        coord = coordinator_factory()

        job = coord.next_job(
            active_boards=set(),
            llm_cooldown_until=lambda: datetime.now(UTC) + timedelta(minutes=5),
        )

        assert job is None

    def test_stale_pending_batch_cannot_claim_queued_job_when_pause_activates(
        self, db, coordinator_factory, monkeypatch
    ):
        job_id = add_job(db, url="https://boards.greenhouse.io/a/jobs/1")
        stale_batch = [dict(db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())]
        coord = coordinator_factory()

        monkeypatch.setattr(job_worker, "get_pending_jobs", lambda conn, limit=50: list(stale_batch))

        set_runtime_flag(db, "repair_pause_new_queued_work", "not-json")

        job = coord.next_job(active_boards=set())

        assert job is None
        refreshed = get_job(db, job_id)
        assert refreshed["status"] == "queued"
        assert str(refreshed["progress"] or "") == ""


class TestCoordinatorBoardDetection:
    """Test the board detection helper used by Coordinator."""

    def test_greenhouse_board(self, db_path, coordinator_factory):
        init_db(db_path).close()
        coord = coordinator_factory()
        assert coord._board_for_url("https://boards.greenhouse.io/co/jobs/1") == "greenhouse"

    def test_ashby_board(self, db_path, coordinator_factory):
        init_db(db_path).close()
        coord = coordinator_factory()
        assert coord._board_for_url("https://jobs.ashbyhq.com/co/abc") == "ashby"

    def test_successfactors_marketing_host_not_classified(self, db_path, coordinator_factory):
        init_db(db_path).close()
        coord = coordinator_factory()
        assert coord._board_for_url("https://www.successfactors.com/") == "www.successfactors.com"

    def test_recruitee_marketing_with_assets_not_classified(self, db_path, coordinator_factory):
        init_db(db_path).close()
        coord = coordinator_factory()
        assert coord._board_for_url("https://careers.distribusion.com/careers") == "careers.distribusion.com"

    def test_lever_board(self, db_path, coordinator_factory):
        init_db(db_path).close()
        coord = coordinator_factory()
        assert coord._board_for_url("https://jobs.lever.co/co/abc") == "lever"

    def test_workday_board(self, db_path, coordinator_factory):
        init_db(db_path).close()
        coord = coordinator_factory()
        assert coord._board_for_url("https://company.myworkdayjobs.com/en-US/jobs/1") == "workday"

    def test_dover_board(self, db_path, coordinator_factory):
        init_db(db_path).close()
        coord = coordinator_factory()
        assert coord._board_for_url("https://app.dover.com/apply/co/abc") == "dover"

    def test_bytedance_board(self, db_path, coordinator_factory):
        init_db(db_path).close()
        coord = coordinator_factory()
        assert (
            coord._board_for_url(
                "https://jobs.bytedance.com/en/position/7613140316427045125/detail?utm_source=trueup.io"
            )
            == "bytedance"
        )

    def test_unknown_board(self, db_path, coordinator_factory):
        init_db(db_path).close()
        coord = coordinator_factory()
        assert coord._board_for_url("https://random-site.com/jobs/1") == "random-site.com"


def test_coordinator_close_closes_thread_local_connection(db_path):
    """Coordinator.close closes the connection created for the current thread."""
    init_db(db_path).close()
    coord = Coordinator(db_path)
    coord.next_job(active_boards=set())
    conn = coord._local.conn

    coord.close()

    with pytest.raises(sqlite3.ProgrammingError) as excinfo:
        conn.execute("SELECT 1")
    assert "closed" in str(excinfo.value).lower()
    assert coord._local.conn is None


def test_worker_main_bootstraps_repair_supervisor_when_enabled(monkeypatch, tmp_path):
    class DummyPool:
        def __init__(self, *args, **kwargs):
            self.is_running = False

        def start(self):
            return None

        def stop(self):
            return None

    called: list[str] = []
    monkeypatch.setattr(job_worker, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(job_worker, "PID_FILE", tmp_path / "jobs.db.worker.pid")
    monkeypatch.setattr(job_worker, "repair_supervisor_enabled", lambda: True)
    monkeypatch.setattr(
        job_worker,
        "ensure_repair_supervisor_running",
        lambda **_: called.append("started") or True,
    )
    monkeypatch.setattr(job_worker, "stop_repair_supervisor", lambda **_: called.append("stopped"))
    monkeypatch.setattr(job_worker, "WorkerPool", DummyPool)
    monkeypatch.setattr(job_worker, "_kill_stale_workers", lambda *_: None)
    monkeypatch.setattr(job_worker.signal, "signal", lambda *_: None)

    with patch.object(sys, "argv", ["job_worker.py", "--workers", "1"]):
        job_worker.main()

    assert called == ["started", "stopped"]


def test_worker_pool_repairs_stale_processing_jobs_on_interval(monkeypatch, db_path):
    pool = WorkerPool(db_path, num_workers=1, headless=True)
    calls: list[tuple[int, int, tuple[int, ...]]] = []

    monkeypatch.setattr(job_worker, "PROCESSING_REPAIR_INTERVAL_SECONDS", 30)
    monkeypatch.setattr(job_worker, "PROCESSING_REPAIR_STALE_THRESHOLD_SECONDS", 60)
    monkeypatch.setattr(job_worker, "PROCESSING_REPAIR_BATCH_LIMIT", 25)
    monotonic_values = iter((100.0, 110.0, 131.0))
    monkeypatch.setattr(job_worker.time, "monotonic", lambda: next(monotonic_values))
    pool._worker_registry = {
        1: {"worker_id": 1, "status": "busy", "job_id": 42},
        2: {"worker_id": 2, "status": "idle", "job_id": None},
    }

    def _fake_repair(conn, *, stale_threshold_seconds, limit, exclude_job_ids=None):
        calls.append((stale_threshold_seconds, limit, tuple(sorted(exclude_job_ids or ()))))
        return {"scanned": 0, "changed": 0}

    monkeypatch.setattr(job_worker, "repair_stale_processing_jobs", _fake_repair)

    try:
        pool._repair_stale_processing_jobs_if_due()
        pool._repair_stale_processing_jobs_if_due()
        pool._repair_stale_processing_jobs_if_due()
    finally:
        pool.stop()

    assert calls == [(60, 25, (42,)), (60, 25, (42,))]


def test_worker_main_skips_repair_supervisor_when_feature_flag_disabled(monkeypatch, tmp_path):
    class DummyPool:
        def __init__(self, *args, **kwargs):
            self.is_running = False

        def start(self):
            return None

        def stop(self):
            return None

    called: list[str] = []
    monkeypatch.setattr(job_worker, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(job_worker, "PID_FILE", tmp_path / "jobs.db.worker.pid")
    monkeypatch.setattr(job_worker, "repair_supervisor_enabled", lambda: False)
    monkeypatch.setattr(job_worker, "ensure_repair_supervisor_running", lambda **_: called.append("started"))
    monkeypatch.setattr(job_worker, "stop_repair_supervisor", lambda **_: called.append("stopped"))
    monkeypatch.setattr(job_worker, "WorkerPool", DummyPool)
    monkeypatch.setattr(job_worker, "_kill_stale_workers", lambda *_: None)
    monkeypatch.setattr(job_worker.signal, "signal", lambda *_: None)

    with patch.object(sys, "argv", ["job_worker.py", "--workers", "1"]):
        job_worker.main()

    assert called == []


def test_worker_main_cleans_up_repair_supervisor_when_startup_fails(monkeypatch, tmp_path):
    class ExplodingPool:
        def __init__(self, *args, **kwargs):
            self.is_running = False

        def start(self):
            raise RuntimeError("boom")

        def stop(self):
            return None

    called: list[str] = []
    pid_file = tmp_path / "jobs.db.worker.pid"
    monkeypatch.setattr(job_worker, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(job_worker, "PID_FILE", pid_file)
    monkeypatch.setattr(job_worker, "repair_supervisor_enabled", lambda: True)
    monkeypatch.setattr(
        job_worker,
        "ensure_repair_supervisor_running",
        lambda **_: called.append("started") or True,
    )
    monkeypatch.setattr(job_worker, "stop_repair_supervisor", lambda **_: called.append("stopped"))
    monkeypatch.setattr(job_worker, "WorkerPool", ExplodingPool)
    monkeypatch.setattr(job_worker, "_kill_stale_workers", lambda *_: None)
    monkeypatch.setattr(job_worker.signal, "signal", lambda *_: None)

    with patch.object(sys, "argv", ["job_worker.py", "--workers", "1"]):
        with pytest.raises(RuntimeError, match="boom"):
            job_worker.main()

    assert called == ["started", "stopped"]
    assert not pid_file.exists()


def test_worker_main_cleans_up_repair_supervisor_when_pool_construction_fails(monkeypatch, tmp_path):
    called: list[str] = []
    pid_file = tmp_path / "jobs.db.worker.pid"
    monkeypatch.setattr(job_worker, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(job_worker, "PID_FILE", pid_file)
    monkeypatch.setattr(job_worker, "repair_supervisor_enabled", lambda: True)
    monkeypatch.setattr(
        job_worker,
        "ensure_repair_supervisor_running",
        lambda **_: called.append("started") or True,
    )
    monkeypatch.setattr(job_worker, "stop_repair_supervisor", lambda **_: called.append("stopped"))
    monkeypatch.setattr(job_worker, "WorkerPool", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(job_worker, "_kill_stale_workers", lambda *_: None)
    monkeypatch.setattr(job_worker.signal, "signal", lambda *_: None)

    with patch.object(sys, "argv", ["job_worker.py", "--workers", "1"]):
        with pytest.raises(RuntimeError, match="boom"):
            job_worker.main()

    assert called == ["started", "stopped"]
    assert not pid_file.exists()


def test_worker_main_does_not_stop_existing_supervisor_it_did_not_start(monkeypatch, tmp_path):
    class DummyPool:
        def __init__(self, *args, **kwargs):
            self.is_running = False

        def start(self):
            return None

        def stop(self):
            return None

    called: list[str] = []
    monkeypatch.setattr(job_worker, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(job_worker, "PID_FILE", tmp_path / "jobs.db.worker.pid")
    monkeypatch.setattr(job_worker, "repair_supervisor_enabled", lambda: True)
    monkeypatch.setattr(
        job_worker,
        "ensure_repair_supervisor_running",
        lambda **_: called.append("ensure") or False,
    )
    monkeypatch.setattr(job_worker, "stop_repair_supervisor", lambda **_: called.append("stopped"))
    monkeypatch.setattr(job_worker, "WorkerPool", DummyPool)
    monkeypatch.setattr(job_worker, "_kill_stale_workers", lambda *_: None)
    monkeypatch.setattr(job_worker.signal, "signal", lambda *_: None)

    with patch.object(sys, "argv", ["job_worker.py", "--workers", "1"]):
        job_worker.main()

    assert called == ["ensure"]


# ── WorkerPool Tests ────────────────────────────────────────────────────────


class TestWorkerPool:
    """Test WorkerPool start/stop lifecycle."""

    @patch("job_worker.process_job")
    @patch("job_worker.reset_stale_jobs", return_value=[])
    def test_start_and_stop(self, mock_reset, mock_process, db, db_path):
        """Pool starts worker threads and stops gracefully."""
        pool = WorkerPool(db_path, num_workers=2, headless=True)
        assert not pool.is_running
        pool.start()
        assert pool.is_running
        # Let threads initialize
        time.sleep(0.1)
        pool.stop()
        assert not pool.is_running

    @patch("job_worker.process_job")
    @patch("job_worker.reset_stale_jobs", return_value=[])
    def test_stop_is_idempotent(self, mock_reset, mock_process, db, db_path):
        """Calling stop multiple times doesn't raise."""
        pool = WorkerPool(db_path, num_workers=1, headless=True)
        pool.start()
        time.sleep(0.05)
        pool.stop()
        pool.stop()  # should not raise
        assert not pool.is_running

    @patch("job_worker.process_job")
    @patch("job_worker.reset_stale_jobs", return_value=[])
    def test_workers_call_reset_stale_on_start(self, mock_reset, mock_process, db, db_path):
        """On startup, reset_stale_jobs is called."""
        pool = WorkerPool(db_path, num_workers=1, headless=True)
        pool.start()
        time.sleep(0.05)
        pool.stop()
        mock_reset.assert_called_once()

    @patch("job_worker.process_job")
    @patch("job_worker.reset_stale_jobs", return_value=[])
    def test_workers_process_queued_jobs(self, mock_reset, mock_process, db, db_path):
        """Workers pick up and process queued jobs."""
        add_job(db, url="https://boards.greenhouse.io/a/jobs/1")
        mock_process.return_value = "submitted"

        pool = WorkerPool(db_path, num_workers=1, headless=True)
        pool.start()
        # Give worker time to pick up and "process" the job
        # (includes up to 3s stagger delay for rate limiting)
        time.sleep(5.0)
        pool.stop()

        assert mock_process.called
        call_args = mock_process.call_args
        # conn is now a per-thread connection (not the test fixture's conn)
        assert isinstance(call_args[0][0], type(db))  # is a sqlite3 connection
        assert call_args[0][1] == 1  # job_id
        assert call_args[1]["worker_id"] == 1
        assert call_args[1]["headless"] is None  # None = use smart default (submit→headed, draft→headless)

    @patch("pipeline_orchestrator._auto_retry_if_transient")
    @patch("job_worker.reset_stale_jobs", return_value=[])
    def test_workers_pass_failure_type_into_auto_retry(self, mock_reset, mock_auto_retry, db, db_path):
        """Stopped jobs should forward failure_type into retry classification."""
        add_job(db, url="https://boards.greenhouse.io/a/jobs/1")

        def stop_with_failure(conn, job_id, *, worker_id=0, headless=True, auto_submit=False):
            update_status(
                conn,
                job_id,
                "stopped",
                error_message="Authentication failed, retry later",
                failure_type="auth_failed",
            )
            return "stopped"

        with patch("job_worker.process_job", side_effect=stop_with_failure):
            pool = WorkerPool(db_path, num_workers=1, headless=True)
            pool.start()
            time.sleep(5.0)
            pool.stop()

        mock_auto_retry.assert_called()
        assert mock_auto_retry.call_args.kwargs["failure_type"] == "auth_failed"

    @patch("job_worker.process_job")
    @patch("job_worker.reset_stale_jobs", return_value=[])
    def test_num_workers_matches_thread_count(self, mock_reset, mock_process, db, db_path):
        """Number of started threads matches num_workers."""
        pool = WorkerPool(db_path, num_workers=3, headless=True)
        pool.start()
        time.sleep(0.05)
        assert len(pool._threads) == 3
        pool.stop()

    @patch("job_worker.process_job")
    @patch("job_worker.reset_stale_jobs", return_value=[])
    def test_board_rate_limiting_across_workers(self, mock_reset, mock_process, db, db_path):
        """Two greenhouse submitting jobs should not be processed concurrently."""
        j1 = add_job(db, url="https://boards.greenhouse.io/a/jobs/1")
        update_status(db, j1, "submitting")
        j2 = add_job(db, url="https://boards.greenhouse.io/b/jobs/2")
        update_status(db, j2, "submitting")

        processing_concurrently = []
        active_count_lock = threading.Lock()
        active_greenhouse = [0]  # mutable counter

        def slow_process(conn, job_id, *, worker_id=0, headless=True):
            with active_count_lock:
                active_greenhouse[0] += 1
                processing_concurrently.append(active_greenhouse[0])
            time.sleep(0.3)
            with active_count_lock:
                active_greenhouse[0] -= 1
            return "submitted"

        mock_process.side_effect = slow_process

        pool = WorkerPool(db_path, num_workers=2, headless=True)
        pool.start()
        time.sleep(1.0)  # enough for both jobs to process sequentially
        pool.stop()

        # At no point should we have >1 greenhouse job active
        assert all(c <= 1 for c in processing_concurrently), (
            f"Concurrent greenhouse jobs detected: {processing_concurrently}"
        )


class TestWorkerPoolActiveBoards:
    """Test the thread-safe active board tracking."""

    @patch("job_worker.reset_stale_jobs", return_value=[])
    def test_active_boards_add_and_remove(self, mock_reset, db_path):
        init_db(db_path).close()
        pool = WorkerPool(db_path, num_workers=1, headless=True)
        pool._add_active_board("greenhouse")
        assert "greenhouse" in pool._active_boards
        pool._remove_active_board("greenhouse")
        assert "greenhouse" not in pool._active_boards

    @patch("job_worker.reset_stale_jobs", return_value=[])
    def test_active_boards_thread_safe(self, mock_reset, db_path):
        """Concurrent add/remove of active boards doesn't corrupt state."""
        init_db(db_path).close()
        pool = WorkerPool(db_path, num_workers=1, headless=True)
        errors = []

        def add_remove(board_name, iterations):
            try:
                for _ in range(iterations):
                    pool._add_active_board(board_name)
                    pool._remove_active_board(board_name)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_remove, args=(f"board-{i}", 100)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(pool._active_boards) == 0


# ── Race Condition Tests ──────────────────────────────────────────────────


class TestCoordinatorRaceConditions:
    """Verify CAS prevents two coordinators from claiming the same job."""

    def test_only_one_worker_claims_same_job(self, db, coordinator_factory):
        """Two workers racing on the same job — only one should succeed."""
        add_job(db, url="https://boards.greenhouse.io/race/jobs/1")
        coord1 = coordinator_factory()
        coord2 = coordinator_factory()

        job1 = coord1.next_job(set())
        job2 = coord2.next_job(set())

        # Exactly one should get the job
        assert (job1 is not None) != (job2 is not None) or (job1 is None and job2 is None), (
            "Both coordinators claimed the same job — race condition!"
        )
        assert job1 is not None or job2 is not None, "Nobody claimed the job"

    def test_only_one_worker_claims_submitting_job(self, db, coordinator_factory):
        """submitting→submitting CAS must not let two workers through."""
        add_job(db, url="https://boards.greenhouse.io/race/jobs/2")
        # Simulate an approved draft
        job_id = db.execute("SELECT id FROM jobs ORDER BY id DESC LIMIT 1").fetchone()[0]
        update_status(db, job_id, "submitting")

        coord1 = coordinator_factory()
        coord2 = coordinator_factory()

        job1 = coord1.next_job(set())
        job2 = coord2.next_job(set())

        claimed = [j for j in [job1, job2] if j is not None]
        assert len(claimed) == 1, f"Expected exactly 1 claim, got {len(claimed)}"


# ── ACID Submit Safety Tests ─────────────────────────────────────────────


class TestACIDSubmitSafety:
    """Interrupted submit-phase jobs must reset to draft, not be re-queued."""

    @patch("job_worker.process_job")
    def test_submitting_job_resets_to_draft_on_startup(self, mock_process, db, db_path):
        """Jobs in submitting status are reset to draft when workers start."""
        job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/1")
        update_status(db, job_id, "submitting")

        pool = WorkerPool(db_path, num_workers=1, headless=True)
        pool.start()
        time.sleep(0.1)
        pool.stop()

        row = db.execute("SELECT status, progress FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["status"] == "draft", f"Expected 'draft' but got '{row['status']}'"
        assert "re-approval" in row["progress"]
        assert get_job(db, job_id)["retry_after"] == RETRY_AFTER_SENTINEL

    @patch("job_worker.process_job")
    def test_reanswering_job_resets_to_draft_on_startup(self, mock_process, db, db_path):
        """Jobs in reanswering status are reset to draft when workers start."""
        job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/2")
        update_status(db, job_id, "reanswering")

        pool = WorkerPool(db_path, num_workers=1, headless=True)
        pool.start()
        time.sleep(0.1)
        pool.stop()

        row = db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["status"] == "draft"

    @patch("job_worker.process_job")
    def test_reanswering_job_startup_reset_marks_pending_answer_refresh_failed(
        self, mock_process, db, db_path, tmp_path
    ):
        from answer_refresh_state import load_answer_refresh_state, mark_answer_refresh_pending

        out_dir = tmp_path / "job-output"
        out_dir.mkdir()
        job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/2-refresh")
        update_status(db, job_id, "reanswering", output_dir=str(out_dir))
        mark_answer_refresh_pending(out_dir, request_kind="reanswer")

        pool = WorkerPool(db_path, num_workers=1, headless=True)
        pool.start()
        time.sleep(0.1)
        pool.stop()

        state = load_answer_refresh_state(out_dir)
        assert state["status"] == "failed"
        assert state["reason"] == "interrupted_by_reset"

    @patch("job_worker.process_job")
    def test_awaiting_captcha_resets_to_draft_on_startup(self, mock_process, db, db_path):
        """Jobs in awaiting_captcha status are reset to draft when workers start."""
        job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/3")
        update_status(db, job_id, "awaiting_captcha")

        pool = WorkerPool(db_path, num_workers=1, headless=True)
        pool.start()
        time.sleep(0.1)
        pool.stop()

        row = db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["status"] == "draft"

    @patch("job_worker.process_job")
    @patch("job_worker.reset_stale_jobs", return_value=[])
    def test_killed_worker_repairs_locked_job_instead_of_requeueing(self, mock_reset, mock_process, db, db_path):
        job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/locked-kill")
        db.execute(
            "UPDATE jobs SET status = 'queued', confirmed_at = ?, submission_lock_state = 'locked' WHERE id = ?",
            ("2026-03-18T17:11:18+00:00", job_id),
        )
        db.commit()

        pool = WorkerPool(db_path, num_workers=1, headless=True)
        try:
            pool._worker_stop_events[1] = threading.Event()
            pool._worker_registry[1] = {
                "worker_id": 1,
                "status": "busy",
                "job_id": job_id,
                "company": "",
                "role_title": "",
                "board": "greenhouse",
                "phase": "generating",
                "start_time": None,
                "progress": "",
            }

            pool._kill_single_worker(1)

            row = db.execute("SELECT status, submission_lock_state FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert row["status"] == "submitted"
            assert row["submission_lock_state"] == "locked"
        finally:
            pool.stop()

    @patch("job_worker.process_job")
    @patch("job_worker.reset_stale_jobs", return_value=[])
    def test_killed_worker_resets_submitting_to_draft(self, mock_reset, mock_process, db, db_path):
        """When a worker is killed mid-submit, the job goes to draft."""
        job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/4")
        update_status(db, job_id, "submitting")

        pool = WorkerPool(db_path, num_workers=1, headless=True)
        try:
            # Simulate: worker 1 is processing this submitting job
            pool._worker_stop_events[1] = threading.Event()
            pool._worker_registry[1] = {
                "worker_id": 1,
                "status": "busy",
                "job_id": job_id,
                "company": "",
                "role_title": "",
                "board": "greenhouse",
                "phase": "submitting",
                "start_time": None,
                "progress": "",
            }

            pool._kill_single_worker(1)

            row = db.execute("SELECT status, progress FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert row["status"] == "draft", f"Expected 'draft' but got '{row['status']}'"
            assert "re-approval" in row["progress"]
        finally:
            pool.stop()

    @patch("job_worker.process_job")
    @patch("job_worker.reset_stale_jobs", return_value=[])
    def test_killed_worker_requeues_non_submit_job(self, mock_reset, mock_process, db, db_path):
        """Non-submit-phase jobs are requeued (not drafted) when worker is killed."""
        job_id = add_job(db, url="https://boards.greenhouse.io/co/jobs/5")
        update_status(db, job_id, "generating")

        pool = WorkerPool(db_path, num_workers=1, headless=True)
        try:
            pool._worker_stop_events[1] = threading.Event()
            pool._worker_registry[1] = {
                "worker_id": 1,
                "status": "busy",
                "job_id": job_id,
                "company": "",
                "role_title": "",
                "board": "greenhouse",
                "phase": "generating",
                "start_time": None,
                "progress": "",
            }

            pool._kill_single_worker(1)

            row = db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert row["status"] == "queued", f"Expected 'queued' but got '{row['status']}'"
            assert get_job(db, job_id)["retry_after"] == RETRY_AFTER_SENTINEL
        finally:
            pool.stop()

    @patch("job_worker.process_job")
    @patch("job_worker.reset_stale_jobs", return_value=[])
    def test_draft_jobs_unaffected_by_startup_reset(self, mock_reset, mock_process, db, db_path):
        """Draft and submitted jobs are not touched by the startup safety reset."""
        j1 = add_job(db, url="https://boards.greenhouse.io/co/jobs/6")
        update_status(db, j1, "draft")
        j2 = add_job(db, url="https://boards.greenhouse.io/co/jobs/7")
        update_status(db, j2, "submitted")

        pool = WorkerPool(db_path, num_workers=1, headless=True)
        pool.start()
        time.sleep(0.1)
        pool.stop()

        r1 = db.execute("SELECT status FROM jobs WHERE id = ?", (j1,)).fetchone()
        r2 = db.execute("SELECT status FROM jobs WHERE id = ?", (j2,)).fetchone()
        assert r1["status"] == "draft"
        assert r2["status"] == "submitted"
