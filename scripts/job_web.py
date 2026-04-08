"""Web UI backend — FastAPI with WebSocket for real-time job monitoring."""

from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from functools import cmp_to_key
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
STATIC_DIR = SCRIPT_DIR / "static"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import saved_portal_import
from answer_refresh_state import load_answer_refresh_state, mark_answer_refresh_pending
from app_paths import jobs_db_path
from application_submit_common import load_pending_user_input_for_submit_attempt, resolve_current_submit_artifacts
from job_action_audit import build_action_process_info, extract_action_detail_json_from_headers
from job_db import (
    RETRY_AFTER_SENTINEL,
    SubmissionLockError,
    add_job,
    backfill_jd_fingerprints,
    close_all_connections,
    count_queue_jobs,
    enforce_submission_lock,
    ensure_job_metrics,
    find_jd_duplicates,
    get_board_error_rates,
    get_field_corrections,
    get_job,
    get_job_metrics,
    get_job_timeline,
    get_jobs_processed_counts,
    get_phase_avg_durations,
    get_queue_counts,
    get_repair_queue_pause,
    get_status_counts,
    get_summary_stats,
    init_db,
    lock_job_for_resubmit,
    log_event,
    open_db,
    open_db_tracked,
    query_jobs,
    query_queue_jobs,
    reconcile_duplicate_jobs,
    reset_stale_jobs,
    sync_job_from_disk,
    unlock_job_for_resubmit,
    update_job_metrics,
    update_status,
)
from job_worker import COMMANDS_FILE as WORKER_COMMANDS_FILE
from job_worker import PID_FILE as WORKER_PID_FILE
from job_worker import STATE_FILE as WORKER_STATE_FILE
from material_ingest import import_material_content
from output_layout import default_role_submit_dir, role_submit_path
from pipeline_draft_proof import draft_review_state
from pipeline_orchestrator import approve_job, approve_job_failure_message, regenerate_job, reset_job_to_new
from pipeline_reset_helpers import clear_restart_pipeline_artifacts
from queue_review_summary import attach_queue_review_summary
from repair_runtime import is_repair_supervisor_running
from settings_store import load_bootstrap as load_user_bootstrap
from settings_store import load_settings as load_user_settings
from settings_store import save_settings as save_user_settings

# ── Database ─────────────────────────────────────────────────────────────

DB_PATH = jobs_db_path()
_local = threading.local()
_QUEUE_CACHE_TTL_SECONDS = 10.0
_QUEUE_CACHE_MAX_ENTRIES = 4096
_queue_response_sync_cache: dict[tuple[object, ...], float] = {}
_queue_review_summary_cache: dict[tuple[object, ...], tuple[float, dict]] = {}


def get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = open_db_tracked(DB_PATH, check_same_thread=False)
    return _local.conn


def _request_action_audit(request: Request) -> tuple[dict | None, str | None]:
    detail_json = extract_action_detail_json_from_headers(request.headers, route=request.url.path)
    return detail_json, build_action_process_info(detail_json)


def _queue_cache_key(job: dict) -> tuple[object, ...]:
    return (
        int(job.get("id") or 0),
        str(job.get("updated_at") or ""),
        str(job.get("status") or ""),
        str(job.get("output_dir") or ""),
        str(job.get("board") or ""),
        str(job.get("confirmed_at") or ""),
        str(job.get("failure_type") or ""),
        str(job.get("auth_state") or ""),
        str(job.get("auth_scope") or ""),
        bool(job.get("archived")),
    )


def _queue_cache_hit(expires_at: float) -> bool:
    return expires_at > time.monotonic()


def _trim_queue_cache(cache: dict) -> None:
    while len(cache) > _QUEUE_CACHE_MAX_ENTRIES:
        cache.pop(next(iter(cache)))


def _evict_job_queue_caches(job_id: int) -> None:
    for cache in (_queue_response_sync_cache, _queue_review_summary_cache):
        stale_keys = [key for key in cache if int(key[0] or 0) == job_id]
        for key in stale_keys:
            cache.pop(key, None)


def _add_submitted_flags(conn: sqlite3.Connection, jobs: list[dict]) -> list[dict]:
    """Add previously_submitted flag and LLM answer info to a list of job dicts."""
    if not jobs:
        return jobs
    ids = [j["id"] for j in jobs]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id FROM jobs WHERE confirmed_at IS NOT NULL AND id IN ({placeholders})",
        ids,
    ).fetchall()
    submitted_ids = {r["id"] for r in rows}
    metrics_rows = conn.execute(
        f"SELECT job_id, llm_generated_answers FROM job_metrics WHERE job_id IN ({placeholders})",
        ids,
    ).fetchall()
    llm_counts = {r["job_id"]: r["llm_generated_answers"] or 0 for r in metrics_rows}
    for j in jobs:
        j["previously_submitted"] = j["id"] in submitted_ids
        j["llm_generated_answers"] = llm_counts.get(j["id"], 0)
    return jobs


def _query_jobs_by_ids(conn: sqlite3.Connection, job_ids: list[int]) -> list[dict]:
    if not job_ids:
        return []
    placeholders = ",".join("?" * len(job_ids))
    rows = conn.execute(f"SELECT * FROM jobs WHERE id IN ({placeholders})", job_ids).fetchall()
    order = {job_id: idx for idx, job_id in enumerate(job_ids)}
    jobs = [dict(row) for row in rows]
    jobs.sort(key=lambda job: order.get(int(job.get("id") or 0), len(order)))
    return jobs


def _enrich_queue_rows(conn: sqlite3.Connection, jobs: list[dict]) -> list[dict]:
    if not jobs:
        return jobs
    _add_submitted_flags(conn, jobs)
    uncached_jobs: list[dict] = []
    now = time.monotonic()
    for job in jobs:
        cache_key = _queue_cache_key(job)
        cached_entry = _queue_review_summary_cache.get(cache_key)
        if cached_entry is not None and _queue_cache_hit(cached_entry[0]):
            job["queue_review_summary"] = copy.deepcopy(cached_entry[1])
            continue
        if cached_entry is not None:
            _queue_review_summary_cache.pop(cache_key, None)
        uncached_jobs.append(job)
    if uncached_jobs:
        attach_queue_review_summary(uncached_jobs)
        expires_at = now + _QUEUE_CACHE_TTL_SECONDS
        for job in uncached_jobs:
            summary = copy.deepcopy(job.get("queue_review_summary") or {})
            _queue_review_summary_cache[_queue_cache_key(job)] = (expires_at, summary)
        _trim_queue_cache(_queue_review_summary_cache)
    return jobs


def _sync_jobs_for_response(conn: sqlite3.Connection, jobs: list[dict]) -> bool:
    changed = False
    seen_ids: set[int] = set()
    now = time.monotonic()
    for job in jobs:
        job_id = int(job.get("id") or 0)
        if not job_id or job_id in seen_ids or not job.get("output_dir"):
            continue
        seen_ids.add(job_id)
        cache_key = _queue_cache_key(job)
        cached_until = _queue_response_sync_cache.get(cache_key)
        if cached_until is not None and _queue_cache_hit(cached_until):
            continue
        if cached_until is not None:
            _queue_response_sync_cache.pop(cache_key, None)
        sync_result = sync_job_from_disk(conn, job_id)
        if sync_result.get("changed", bool(sync_result.get("updates"))):
            changed = True
            _evict_job_queue_caches(job_id)
            continue
        _queue_response_sync_cache[cache_key] = now + _QUEUE_CACHE_TTL_SECONDS
        _trim_queue_cache(_queue_response_sync_cache)
    return changed


def _load_queue_page(
    conn: sqlite3.Connection,
    *,
    status: str | None,
    board: str | None,
    search: str | None,
    sort_field: str,
    sort_dir: str,
    limit: int,
    offset: int,
) -> tuple[int, list[dict]]:
    total = count_queue_jobs(conn, status=status, board=board, search=search)
    if sort_field in QUEUE_DERIVED_SORT_FIELDS and total:
        jobs = query_queue_jobs(
            conn,
            status=status,
            board=board,
            search=search,
            sort_field="updated_at",
            sort_dir="desc",
            limit=total,
            offset=0,
        )
        if sort_field == "confidence":
            jobs = _enrich_queue_rows(conn, jobs)
        jobs = _sort_queue_rows(jobs, sort_field=sort_field, sort_dir=sort_dir)
        jobs = jobs[offset : offset + limit]
    else:
        jobs = query_queue_jobs(
            conn,
            status=status,
            board=board,
            search=search,
            sort_field=sort_field,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
    if sort_field != "confidence":
        jobs = _enrich_queue_rows(conn, jobs)
    return total, jobs


QUEUE_DERIVED_SORT_FIELDS = {"status_entered_at", "confidence"}
QUEUE_CONFIDENCE_RANK = {
    "pending": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
}


def _queue_sort_value(job: dict[str, object], sort_field: str) -> tuple[object | None, bool]:
    if sort_field == "status_entered_at":
        value = job.get("status_entered_at")
        return value, not bool(value)
    if sort_field == "confidence":
        confidence = str(((job.get("queue_review_summary") or {}).get("overall_confidence")) or "").strip().lower()
        rank = QUEUE_CONFIDENCE_RANK.get(confidence)
        return rank, rank is None
    value = job.get(sort_field)
    missing = value is None or (isinstance(value, str) and not value.strip())
    if isinstance(value, str):
        value = value.casefold()
    return value, missing


def _compare_queue_rows(left: dict[str, object], right: dict[str, object], *, sort_field: str, sort_dir: str) -> int:
    left_value, left_missing = _queue_sort_value(left, sort_field)
    right_value, right_missing = _queue_sort_value(right, sort_field)
    if left_missing != right_missing:
        return 1 if left_missing else -1
    if left_value != right_value:
        if left_value < right_value:
            return -1 if sort_dir == "asc" else 1
        return 1 if sort_dir == "asc" else -1
    left_id = int(left.get("id") or 0)
    right_id = int(right.get("id") or 0)
    if left_id == right_id:
        return 0
    return -1 if left_id > right_id else 1


def _sort_queue_rows(jobs: list[dict], *, sort_field: str, sort_dir: str) -> list[dict]:
    if not jobs:
        return jobs
    direction = "asc" if str(sort_dir).lower() == "asc" else "desc"
    return sorted(
        jobs,
        key=cmp_to_key(lambda left, right: _compare_queue_rows(left, right, sort_field=sort_field, sort_dir=direction)),
    )


def _serialize_proof_artifacts(output_dir: str | Path | None, board_name: str | None) -> dict | None:
    if not output_dir:
        return None
    proof = resolve_current_submit_artifacts(Path(output_dir), board_name=board_name)
    visible_review_screenshot = proof.get("review_screenshot")
    if visible_review_screenshot is None and str(proof.get("submit_dirname") or "submit") != "submit":
        resolved_board = str(proof.get("board_name") or board_name or "").strip()
        if resolved_board:
            from autofill_common import board_file_constants

            review_candidate = Path(proof["submit_dir"]) / board_file_constants(resolved_board)["review_screenshot"]
            if review_candidate.exists():
                visible_review_screenshot = review_candidate
    proof_paths = [
        path
        for path in (
            proof.get("application_answers_json"),
            proof.get("report_json"),
            proof.get("report_md"),
            proof.get("pre_submit_screenshot"),
            visible_review_screenshot,
            proof.get("post_submit_screenshot"),
            proof.get("submit_debug_screenshot"),
            proof.get("linked_resource_context_json"),
            proof.get("linked_resource_failures_json"),
        )
        if path is not None
    ]
    proof_revision = str(proof.get("submit_dirname") or "")
    if proof_paths:
        latest_mtime_ns = max(path.stat().st_mtime_ns for path in proof_paths)
        proof_revision = f"{proof_revision}:{latest_mtime_ns}" if proof_revision else str(latest_mtime_ns)
    return {
        "board": proof.get("board_name"),
        "submit_dirname": proof.get("submit_dirname"),
        "proof_revision": proof_revision,
        "application_answers_json": proof["application_answers_json"].name
        if proof.get("application_answers_json")
        else None,
        "report_json": proof["report_json"].name if proof.get("report_json") else None,
        "report_markdown": proof["report_md"].name if proof.get("report_md") else None,
        "pre_submit_screenshot": proof["pre_submit_screenshot"].name if proof.get("pre_submit_screenshot") else None,
        "review_screenshot": visible_review_screenshot.name if visible_review_screenshot else None,
        "post_submit_screenshot": proof["post_submit_screenshot"].name if proof.get("post_submit_screenshot") else None,
        "submit_debug_screenshot": proof["submit_debug_screenshot"].name
        if proof.get("submit_debug_screenshot")
        else None,
        "linked_resource_context_json": proof["linked_resource_context_json"].name
        if proof.get("linked_resource_context_json")
        else None,
        "linked_resource_failures_json": proof["linked_resource_failures_json"].name
        if proof.get("linked_resource_failures_json")
        else None,
    }


def _path_within_base(base: Path, candidate: Path) -> bool:
    base_resolved = base.resolve()
    candidate_resolved = candidate.resolve()
    try:
        candidate_resolved.relative_to(base_resolved)
    except ValueError:
        return False
    return True


# ── Worker Management ────────────────────────────────────────────────────

_worker_proc: subprocess.Popen | None = None


def is_worker_running() -> bool:
    global _worker_proc
    if _worker_proc and _worker_proc.poll() is None:
        return True
    if WORKER_PID_FILE.exists():
        try:
            pid = int(WORKER_PID_FILE.read_text().strip())
            os.kill(pid, 0)
            return True
        except (ValueError, OSError):
            pass
    return False


_configured_num_workers = 16  # Updated by CLI args


def start_workers(num_workers: int | None = None) -> None:
    """Start a worker pool subprocess.

    Only starts if no workers are already running (checked via PID file).
    Does NOT kill existing workers — they are independent processes.
    """
    global _worker_proc, _configured_num_workers
    if num_workers is not None:
        _configured_num_workers = num_workers
    if is_worker_running():
        log.info("Workers already running — skipping start")
        return
    num_workers = _configured_num_workers
    worker_script = str(SCRIPT_DIR / "job_worker.py")
    # Don't pass --headless: workers use smart defaults (submit→headed, draft→headless)
    cmd = ["uv", "run", "--project", str(PROJECT_ROOT), "python", worker_script, "--workers", str(num_workers)]
    # Log worker stderr to a file so startup errors are visible (not DEVNULL)
    _worker_log = PROJECT_ROOT / "jobs.db.worker.log"
    _worker_proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=_worker_log.open("a"),  # noqa: SIM115 — Popen owns the fd
        start_new_session=True,
    )
    log.info("Started worker pool (PID %d, %d workers, log=%s)", _worker_proc.pid, num_workers, _worker_log)


def stop_workers() -> None:
    """Stop the worker pool.

    Only stops the worker process started by this web server, or the one
    referenced by the PID file. Does NOT pkill all job_worker processes —
    workers started independently (e.g. via CLI) are left alone.
    """
    global _worker_proc
    if _worker_proc and _worker_proc.poll() is None:
        try:
            os.killpg(os.getpgid(_worker_proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            _worker_proc.terminate()
        try:
            _worker_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(_worker_proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                _worker_proc.kill()
            _worker_proc.wait(timeout=2)
        _worker_proc = None
        log.info("Worker pool stopped")
    elif WORKER_PID_FILE.exists():
        try:
            pid = int(WORKER_PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            log.info("Sent SIGTERM to worker PID %d", pid)
        except (ValueError, OSError, ProcessLookupError):
            pass
    # Fallback: kill any orphaned job_worker.py processes AND their spawned
    # claude auto-fix subprocesses that survived PID-based kill.
    _my_pid = os.getpid()
    for pattern in ("job_worker.py", "claude.*autofill.*failed"):
        try:
            result = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in result.stdout.strip().splitlines():
                pid = int(line.strip())
                if pid == _my_pid:
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                    log.info("Killed orphaned process %d (pattern=%s)", pid, pattern)
                except (OSError, ProcessLookupError):
                    pass
        except Exception:
            pass
    # Ensure we're on main branch (auto-fix may have left us on an autofix branch)
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if branch != "main" and branch.startswith("autofix/"):
            log.warning("Server restarting on branch %s — switching back to main", branch)
            subprocess.run(["git", "checkout", "main"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    # Reset stale jobs
    conn = get_conn()
    reset_stale_jobs(conn, stale_threshold_seconds=0)
    # Only reset jobs that were actively being processed (not user-stopped/rejected jobs)
    conn.execute(
        "UPDATE jobs SET status = 'queued', provider = NULL, retry_after = ?, error_message = '', progress = '' "
        "WHERE status IN ('resolving', 'generating', 'autofilling', 'fix_in_progress', 'retrying')",
        (RETRY_AFTER_SENTINEL,),
    )
    # Submit-phase jobs go back to draft (ACID: needs re-approval)
    conn.execute(
        "UPDATE jobs SET status = 'draft', provider = NULL, retry_after = ?, "
        "progress = 'Submit interrupted — needs re-approval' "
        "WHERE status IN ('submitting', 'reanswering', 'awaiting_captcha')",
        (RETRY_AFTER_SENTINEL,),
    )
    conn.commit()


def _read_worker_states() -> list[dict]:
    """Read worker state from the shared JSON file written by the worker pool."""
    if not WORKER_STATE_FILE.exists():
        return []
    try:
        raw = WORKER_STATE_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            return []
        return json.loads(raw)
    except Exception:
        return []


def _send_worker_command(cmd: dict) -> None:
    """Append a command to the worker commands file."""
    try:
        existing: list[dict] = []
        if WORKER_COMMANDS_FILE.exists():
            raw = WORKER_COMMANDS_FILE.read_text(encoding="utf-8").strip()
            if raw:
                data = json.loads(raw)
                existing = data.get("commands", [])
        existing.append(cmd)
        WORKER_COMMANDS_FILE.write_text(
            json.dumps({"commands": existing}),
            encoding="utf-8",
        )
    except Exception:
        log.exception("Failed to send worker command")


def _read_runtime_services() -> dict[str, object]:
    conn = get_conn()
    pause = get_repair_queue_pause(conn)
    return {
        "workers_running": is_worker_running(),
        "repair_supervisor_running": is_repair_supervisor_running(project_root=PROJECT_ROOT),
        "repair_queue_paused": pause is not None,
        "repair_queue_pause": pause,
    }


# ── WebSocket Manager ────────────────────────────────────────────────────


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []
        self._last_state: dict[int, str] = {}  # job_id -> updated_at

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        try:
            self.active.remove(ws)
        except ValueError:
            pass

    async def broadcast(self, message: dict):
        data = json.dumps(message, default=str)
        for ws in list(self.active):
            try:
                await ws.send_text(data)
            except Exception:
                self.active.remove(ws)

    def get_changed_jobs(self, conn: sqlite3.Connection) -> list[dict]:
        """Return jobs whose updated_at changed since last check."""
        changed = []
        new_state = {}
        changed_ids: list[int] = []
        state_rows = conn.execute("SELECT id, updated_at FROM jobs ORDER BY updated_at DESC LIMIT 500").fetchall()
        for row in state_rows:
            jid = int(row["id"])
            updated = row["updated_at"] or ""
            new_state[jid] = updated
            if self._last_state.get(jid) != updated:
                changed_ids.append(jid)
        if changed_ids:
            changed.extend(_enrich_queue_rows(conn, _query_jobs_by_ids(conn, changed_ids)))
        # Detect deletions
        for jid in set(self._last_state) - set(new_state):
            changed.append({"id": jid, "_deleted": True})
        self._last_state = new_state
        return changed


manager = ConnectionManager()


# ── Request Models ────────────────────────────────────────────────────────


def _synthetic_timeline(job: dict) -> list[dict]:
    """Build synthetic timeline entries from file timestamps for draft-only jobs."""
    import datetime

    base = Path(job["output_dir"])
    entries: list[dict] = []

    # Check both new layout (content/) and old layout (root)
    file_checks = [
        ("jd_parsed", "Job description parsed", ["content/jd_parsed.json", "jd_parsed.json"]),
        ("resume_generated", "Resume content generated", ["content/resume_content.json", "resume_content.json"]),
        (
            "cover_letter_generated",
            "Cover letter generated",
            ["content/cover_letter_text.txt", "cover_letter_text.txt"],
        ),
    ]
    for event_type, description, candidates in file_checks:
        for rel_path in candidates:
            fpath = base / rel_path
            if fpath.exists():
                mtime = fpath.stat().st_mtime
                ts = datetime.datetime.fromtimestamp(mtime, tz=datetime.UTC)
                entries.append(
                    {
                        "event_type": event_type,
                        "detail": description,
                        "created_at": ts.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
                break

    # Check for PDFs in documents/ and root (old layout)
    for search_dir in [base / "documents", base]:
        if search_dir.is_dir():
            for pdf in sorted(search_dir.glob("*.pdf")):
                mtime = pdf.stat().st_mtime
                ts = datetime.datetime.fromtimestamp(mtime, tz=datetime.UTC)
                entries.append(
                    {
                        "event_type": "document_created",
                        "detail": pdf.name,
                        "created_at": ts.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )

    # Sort by timestamp
    entries.sort(key=lambda e: e["created_at"])
    return entries


class AddJobsRequest(BaseModel):
    urls: list[str]
    provider: str | None = None
    priority: int = 0


class BoardUrlRequest(BaseModel):
    url: str


class DraftOverridesRequest(BaseModel):
    overrides: dict


class _RegenRequest(BaseModel):
    target: str


class DiscoverSearchRequest(BaseModel):
    search_term: str
    location: str = "San Francisco, CA"
    sources: list[str] | None = None
    results_wanted: int = 50


class PromoteBulkRequest(BaseModel):
    ids: list[int]


# ── Saved Portal Imports ─────────────────────────────────────────────────


class SavedPortalImportRequest(BaseModel):
    provider: str | None = None
    priority: int = 0


class _LegacySavedPortalImportRequest(SavedPortalImportRequest):
    portal: str


class SaveSettingsRequest(BaseModel):
    materials: dict[str, str] = Field(default_factory=dict)
    providers: dict[str, str | bool | None] = Field(default_factory=dict)
    credentials: dict[str, str | None] = Field(default_factory=dict)


class ImportMaterialRequest(BaseModel):
    material_key: str
    text: str | None = None
    source_url: str | None = None
    file_name: str | None = None
    content_type: str | None = None
    content_base64: str | None = None


def _import_saved_portal_jobs(
    conn: sqlite3.Connection,
    *,
    portal: str,
    priority: int = 0,
    provider: str | None = None,
) -> dict:
    try:
        module = saved_portal_import.load_saved_portal_module(portal)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Failed to load saved-portal importer: {portal}: {exc}") from exc

    try:
        return module.import_saved_jobs(conn, priority=priority, provider=provider)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Saved-portal importer failed ({portal}): {exc}") from exc


def _run_saved_portal_import(saved_portal: str, req: SavedPortalImportRequest | None = None) -> dict:
    try:
        saved_portal_import.get_saved_portal(saved_portal)
    except ValueError as exc:
        raise HTTPException(404, "Unknown saved portal") from exc

    options = req or SavedPortalImportRequest()
    conn = get_conn()
    try:
        return _import_saved_portal_jobs(
            conn,
            portal=saved_portal,
            priority=options.priority,
            provider=options.provider,
        )
    except RuntimeError as exc:
        log.warning("Saved-portal import failed: %s", exc, exc_info=True)
        raise HTTPException(500, str(exc)) from exc


# ── App Factory ──────────────────────────────────────────────────────────


async def _broadcast_changes():
    """Background task: poll DB for changes and push diffs to WebSocket clients."""
    while True:
        try:
            await asyncio.sleep(2)
            if not manager.active:
                continue
            conn = get_conn()
            changed = manager.get_changed_jobs(conn)
            if changed:
                for job in changed:
                    if job.get("_deleted"):
                        await manager.broadcast({"type": "job_deleted", "id": job["id"]})
                    else:
                        await manager.broadcast({"type": "job_update", "job": job})
            active = conn.execute(
                """SELECT id, company, role_title, board, status, progress, provider
                   FROM jobs WHERE status IN ('generating', 'resolving', 'submitting',
                   'autofilling', 'retrying', 'fix_in_progress', 'reanswering')
                   ORDER BY updated_at DESC"""
            ).fetchall()
            runtime_services = _read_runtime_services()
            await manager.broadcast(
                {
                    "type": "worker_status",
                    "running": runtime_services["workers_running"],
                    "repair_supervisor_running": runtime_services["repair_supervisor_running"],
                    "repair_queue_paused": runtime_services["repair_queue_paused"],
                    "repair_queue_pause": runtime_services["repair_queue_pause"],
                    "active_jobs": [dict(r) for r in active],
                }
            )
            # Broadcast per-worker detail from state file
            worker_states = _read_worker_states()
            await manager.broadcast(
                {
                    "type": "worker_detail",
                    "workers": worker_states,
                    "running": is_worker_running(),
                }
            )
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Broadcast loop error")
            await asyncio.sleep(5)


_auto_start_workers = False  # Set by CLI --with-workers flag


def _backup_db(source_path: Path, backup_path: Path) -> None:
    """Create a consistent backup using SQLite's backup API (WAL-safe)."""
    try:
        src = sqlite3.connect(str(source_path))
        dst = sqlite3.connect(str(backup_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        os.chmod(str(backup_path), 0o600)
        log.info("Pre-migration backup created: %s", backup_path)
    except Exception:
        log.warning("Pre-migration backup failed (non-fatal)", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import atexit

    # Pre-migration backup — preserves last-known state for migration rollback.
    if DB_PATH.exists():
        _backup_db(DB_PATH, DB_PATH.with_suffix(".db.pre-migration"))

    # Run schema + migrations once at startup (matches worker pool pattern).
    # If this raises (e.g. corruption), uvicorn aborts startup.
    conn = init_db(DB_PATH, check_same_thread=False)
    conn.close()

    # Post-migration backup — known-good, schema up to date.
    # Only runs after integrity check passes, so corruption can't overwrite a good backup.
    if DB_PATH.exists():
        _backup_db(DB_PATH, DB_PATH.with_suffix(".db.backup"))

    # Safety net for double-SIGINT (uvicorn skips lifespan shutdown)
    atexit.register(close_all_connections)

    # Only auto-start workers if explicitly requested (--with-workers)
    if _auto_start_workers and not is_worker_running():
        start_workers()
    task = asyncio.create_task(_broadcast_changes())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Graceful shutdown: close all tracked DB connections
    close_all_connections()


def create_app() -> FastAPI:
    app = FastAPI(title="Job Applications", lifespan=lifespan)

    @app.exception_handler(sqlite3.DatabaseError)
    async def db_error_handler(request: Request, exc: sqlite3.DatabaseError) -> JSONResponse:
        log.error("Database error on %s: %s", request.url.path, exc, exc_info=True)
        return JSONResponse(
            status_code=503,
            content={"error": "database_error", "detail": str(exc)},
        )

    # Serve static files with no-cache headers so restarts always pick up changes
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import Response as StarletteResponse

    _LOCAL_IPS = {"127.0.0.1", "::1", "localhost"}

    class LocalOnlyMiddleware(BaseHTTPMiddleware):
        """Block requests from non-local IPs (scanners, bots via tunnels)."""

        async def dispatch(self, request, call_next):
            client_ip = request.client.host if request.client else None
            if client_ip not in _LOCAL_IPS:
                return StarletteResponse(status_code=403)
            return await call_next(request)

    class NoCacheStaticMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            if request.url.path.startswith("/static/"):
                response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            return response

    app.add_middleware(NoCacheStaticMiddleware)
    app.add_middleware(LocalOnlyMiddleware)

    @app.get("/")
    def root():
        return HTMLResponse((STATIC_DIR / "index.html").read_text())

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "worker_running": is_worker_running()}

    @app.get("/api/bootstrap")
    def bootstrap():
        payload = load_user_bootstrap()
        payload["worker_running"] = is_worker_running()
        return payload

    @app.get("/api/settings")
    def get_settings():
        return load_user_settings()

    @app.post("/api/settings")
    def save_settings(payload: SaveSettingsRequest):
        try:
            return save_user_settings(payload.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/settings/materials/import")
    def import_material(payload: ImportMaterialRequest):
        try:
            content_bytes = None
            if payload.content_base64 is not None:
                content_bytes = base64.b64decode(payload.content_base64, validate=True)

            imported_text = import_material_content(
                text=payload.text,
                source_url=payload.source_url,
                file_name=payload.file_name,
                content_type=payload.content_type,
                content_bytes=content_bytes,
            )
            settings = save_user_settings({"materials": {payload.material_key: imported_text}})
            return {
                "settings": settings,
                "bootstrap": load_user_bootstrap(),
            }
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/kill")
    def kill():
        """Kill the web server and stop workers."""
        import threading

        def _kill():
            stop_workers()
            close_all_connections()
            # Send SIGTERM to self — let uvicorn handle graceful shutdown
            # (instead of os._exit which bypasses cleanup and corrupts WAL)
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=_kill, daemon=True).start()
        return {"status": "killed"}

    @app.post("/api/restart")
    def restart():
        """Restart the web server only. Workers continue running independently."""
        import threading

        def _do_restart():
            import time

            time.sleep(0.5)
            # Close all connections and checkpoint WAL before replacing process
            close_all_connections()
            try:
                ckpt = sqlite3.connect(str(DB_PATH), timeout=5)
                ckpt.execute("PRAGMA wal_checkpoint(PASSIVE)")
                ckpt.close()
            except Exception:
                pass
            # Do NOT stop workers — they run independently
            os.execv(sys.executable, [sys.executable] + sys.argv)

        threading.Thread(target=_do_restart, daemon=True).start()
        return {"status": "restarting"}

    # ── Job endpoints ────────────────────────────────────────────────
    @app.get("/api/jobs")
    def list_jobs(
        status: str | None = None,
        board: str | None = None,
        search: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ):
        conn = get_conn()
        jobs = query_jobs(conn, status=status, board=board, search=search, limit=limit, offset=offset)
        if _sync_jobs_for_response(conn, jobs):
            jobs = query_jobs(conn, status=status, board=board, search=search, limit=limit, offset=offset)
        return _add_submitted_flags(conn, jobs)

    @app.get("/api/queue")
    def list_queue(
        status: str | None = None,
        board: str | None = None,
        search: str | None = None,
        sort_field: str = "updated_at",
        sort_dir: str = "desc",
        limit: int = 200,
        offset: int = 0,
    ):
        if limit < 1 or limit > 500:
            raise HTTPException(400, "limit must be between 1 and 500")
        if offset < 0:
            raise HTTPException(400, "offset must be non-negative")
        conn = get_conn()
        sort_field = str(sort_field or "updated_at").strip()
        sort_dir = "asc" if str(sort_dir).lower() == "asc" else "desc"
        total, jobs = _load_queue_page(
            conn,
            status=status,
            board=board,
            search=search,
            sort_field=sort_field,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
        if _sync_jobs_for_response(conn, jobs):
            total, jobs = _load_queue_page(
                conn,
                status=status,
                board=board,
                search=search,
                sort_field=sort_field,
                sort_dir=sort_dir,
                limit=limit,
                offset=offset,
            )
        return {
            "jobs": jobs,
            "counts": get_queue_counts(conn),
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/jobs/{job_id}")
    def get_job_detail(job_id: int):
        conn = get_conn()
        job = get_job(conn, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if _sync_jobs_for_response(conn, [job]):
            job = get_job(conn, job_id)
            if not job:
                raise HTTPException(404, "Job not found")
        _enrich_queue_rows(conn, [job])
        try:
            timeline = get_job_timeline(conn, job_id)
        except sqlite3.DatabaseError:
            log.warning("Timeline query failed for job %d, returning empty", job_id, exc_info=True)
            timeline = []
        if not timeline and job.get("output_dir"):
            timeline = _synthetic_timeline(job)
        job["timeline"] = timeline
        job["metrics"] = get_job_metrics(conn, job_id)
        job["field_corrections"] = get_field_corrections(conn, job_id)
        job["previously_submitted"] = job.get("confirmed_at") is not None
        job["answer_refresh"] = load_answer_refresh_state(Path(job["output_dir"])) if job.get("output_dir") else None
        if job.get("output_dir"):
            pending = load_pending_user_input_for_submit_attempt(Path(job["output_dir"]))
            job["pending_user_input"] = pending[1] if pending is not None else None
            job["proof_artifacts"] = _serialize_proof_artifacts(job["output_dir"], job.get("board"))
            job["draft_review_state"] = draft_review_state(Path(job["output_dir"]), board_name=job.get("board"))
        else:
            job["pending_user_input"] = None
            job["proof_artifacts"] = None
            job["draft_review_state"] = None
        return job

    @app.post("/api/jobs")
    def add_jobs(req: AddJobsRequest):
        conn = get_conn()
        added = 0
        duplicates = 0
        for url in req.urls:
            url = url.strip()
            if not url or not url.startswith("http"):
                continue
            try:
                result = add_job(conn, url, priority=req.priority, provider=req.provider)
                if result < 0:
                    duplicates += 1
                else:
                    added += 1
            except sqlite3.IntegrityError:
                duplicates += 1
        return {"added": added, "duplicates": duplicates}

    @app.post("/api/jobs/dedup")
    def dedup_jobs():
        """Backfill JD fingerprints and return duplicate groups."""
        conn = get_conn()
        updated, skipped = backfill_jd_fingerprints(conn)
        groups = find_jd_duplicates(conn)
        return {
            "fingerprints_added": updated,
            "fingerprints_skipped": skipped,
            "duplicate_groups": groups,
        }

    @app.post("/api/jobs/dedup/reconcile")
    def reconcile_dedup_jobs():
        """Retroactively archive safe duplicate rows after metadata resolution."""
        conn = get_conn()
        return reconcile_duplicate_jobs(conn, initiator="web")

    @app.post("/api/jobs/import/{saved_portal}")
    def import_saved_portal(saved_portal: str, req: SavedPortalImportRequest | None = None):
        """Import jobs from a saved-job portal (synchronous)."""
        return _run_saved_portal_import(saved_portal, req)

    @app.post("/api/jobs/import-saved-portal/{saved_portal}")
    def import_saved_portal_legacy_path(saved_portal: str, req: SavedPortalImportRequest | None = None):
        """Backward-compatible path alias for saved-portal imports."""
        return _run_saved_portal_import(saved_portal, req)

    @app.post("/api/jobs/import-saved-portal")
    def import_saved_portal_legacy(req: _LegacySavedPortalImportRequest):
        """Backward-compatible body-based alias for saved-portal imports."""
        return _run_saved_portal_import(
            req.portal,
            SavedPortalImportRequest(provider=req.provider, priority=req.priority),
        )

    @app.post("/api/jobs/import-linkedin-saved")
    def import_linkedin_saved():
        """Backward-compatible alias for saved-portal imports (synchronous)."""
        return _run_saved_portal_import("linkedin")

    def _enforce_submission_unlock(
        conn: sqlite3.Connection,
        job_id: int,
        *,
        target_status: str,
        initiator: str = "web",
        event_detail_json: dict | None = None,
        process_info: str | None = None,
    ) -> None:
        try:
            enforce_submission_lock(conn, job_id, target_status=target_status)
        except SubmissionLockError as exc:
            conn.rollback()
            log_event(
                conn,
                job_id,
                "submission_lock_refused",
                detail=target_status,
                detail_json=event_detail_json,
                initiator=initiator,
                process_info=process_info,
            )
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/jobs/{job_id}/unlock-resubmit")
    def unlock_resubmit(job_id: int, request: Request):
        conn = get_conn()
        job = get_job(conn, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        action_detail_json, action_process_info = _request_action_audit(request)
        if not unlock_job_for_resubmit(
            conn,
            job_id,
            initiator="web",
            event_detail_json=action_detail_json,
            process_info=action_process_info,
        ):
            raise HTTPException(409, "Job is not submission-locked")
        return {"status": "unlocked_for_resubmit"}

    @app.post("/api/jobs/{job_id}/lock-resubmit")
    def lock_resubmit(job_id: int, request: Request):
        conn = get_conn()
        job = get_job(conn, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        action_detail_json, action_process_info = _request_action_audit(request)
        if not lock_job_for_resubmit(
            conn,
            job_id,
            initiator="web",
            event_detail_json=action_detail_json,
            process_info=action_process_info,
        ):
            raise HTTPException(409, "Job is not unlocked for resubmission")
        return {"status": "locked"}

    @app.post("/api/jobs/{job_id}/approve")
    def approve(job_id: int, request: Request):
        conn = get_conn()
        action_detail_json, action_process_info = _request_action_audit(request)
        _enforce_submission_unlock(
            conn,
            job_id,
            target_status="approved",
            event_detail_json=action_detail_json,
            process_info=action_process_info,
        )
        ok = approve_job(
            conn,
            job_id,
            initiator="web",
            event_detail_json=action_detail_json,
            process_info=action_process_info,
        )
        if not ok:
            detail = approve_job_failure_message(conn, job_id)
            status_code = 409 if "incomplete draft" in detail.casefold() else 400
            raise HTTPException(status_code, detail)
        ensure_job_metrics(conn, job_id)
        m = get_job_metrics(conn, job_id)
        if m:
            update_job_metrics(conn, job_id, manual_interventions=m["manual_interventions"] + 1)
        return {"status": "approved"}

    @app.post("/api/jobs/{job_id}/focus-browser")
    def focus_browser(job_id: int):
        """Bring the captcha browser window to foreground via AppleScript."""
        conn = get_conn()
        row = conn.execute("SELECT company, role_title, status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Job {job_id} not found")
        if row["status"] != "awaiting_captcha":
            raise HTTPException(409, detail={"error": "wrong_status", "current_status": row["status"]})

        import platform

        if platform.system() == "Darwin":
            from browser_runtime import focus_chromium_window

            focus_chromium_window()
        return {"status": "focused"}

    @app.post("/api/jobs/{job_id}/reject")
    def reject(job_id: int, request: Request):
        conn = get_conn()
        action_detail_json, action_process_info = _request_action_audit(request)
        update_status(
            conn,
            job_id,
            "stopped",
            error_message="Rejected via web UI",
            failure_type="user_rejected",
            initiator="web",
            process_info=action_process_info,
            event_detail_json=action_detail_json,
        )
        return {"status": "rejected"}

    @app.post("/api/jobs/{job_id}/regenerate")
    def regenerate(job_id: int, request: Request):
        conn = get_conn()
        action_detail_json, action_process_info = _request_action_audit(request)
        _enforce_submission_unlock(
            conn,
            job_id,
            target_status="queued",
            event_detail_json=action_detail_json,
            process_info=action_process_info,
        )
        # Boost priority so user-initiated regenerations jump the queue
        conn.execute("UPDATE jobs SET priority = MAX(priority, 100) WHERE id = ?", (job_id,))
        conn.commit()
        ok = regenerate_job(
            conn,
            job_id,
            initiator="web",
            event_detail_json=action_detail_json,
            process_info=action_process_info,
        )
        if not ok:
            raise HTTPException(400, "Cannot regenerate from current status")
        return {"status": "queued"}

    @app.post("/api/jobs/{job_id}/reset-to-new")
    def reset_to_new(job_id: int, request: Request):
        conn = get_conn()
        action_detail_json, action_process_info = _request_action_audit(request)
        if not reset_job_to_new(
            conn,
            job_id,
            initiator="web",
            event_detail_json=action_detail_json,
            process_info=action_process_info,
        ):
            raise HTTPException(400, "Cannot reset job to new from current state")
        return {"status": "queued"}

    @app.post("/api/jobs/{job_id}/regenerate-asset")
    def regenerate_asset(job_id: int, req: _RegenRequest, request: Request):
        """Regenerate a specific asset (resume, cover_letter, or answers)."""
        conn = get_conn()
        job = get_job(conn, job_id)
        if not job or not job.get("output_dir"):
            raise HTTPException(400, "No output directory")
        out_dir = Path(job["output_dir"])
        active = {
            "generating",
            "resolving",
            "approved",
            "submitting",
            "autofilling",
            "retrying",
            "fix_in_progress",
            "reanswering",
            "regenerating",
        }
        if job["status"] in active:
            raise HTTPException(400, "Job is currently running — stop it first")

        target_status = "reanswering" if req.target == "answers" else "regenerating"
        action_detail_json, action_process_info = _request_action_audit(request)
        _enforce_submission_unlock(
            conn,
            job_id,
            target_status=target_status,
            event_detail_json=action_detail_json,
            process_info=action_process_info,
        )

        # Boost priority so user-initiated regenerations jump the queue
        conn.execute("UPDATE jobs SET priority = MAX(priority, 100) WHERE id = ?", (job_id,))

        if req.target == "resume":
            for f in [out_dir / "content" / "resume_content.json"]:
                if f.exists():
                    f.unlink()
            for f in (out_dir / "documents").glob("*Resume*"):
                f.unlink()
            update_status(
                conn,
                job_id,
                "regenerating",
                error_message="",
                progress="Regenerating resume...",
                initiator="web",
                process_info=action_process_info,
                event_detail_json=action_detail_json,
            )
            log_event(
                conn,
                job_id,
                "regen_resume_requested",
                detail_json=action_detail_json,
                initiator="web",
                process_info=action_process_info,
            )
            return {"status": "regenerating"}

        if req.target == "cover_letter":
            for f in [out_dir / "content" / "cover_letter_text.txt"]:
                if f.exists():
                    f.unlink()
            for f in (out_dir / "documents").glob("*Cover Letter*"):
                f.unlink()
            update_status(
                conn,
                job_id,
                "regenerating",
                error_message="",
                progress="Regenerating cover letter...",
                initiator="web",
                process_info=action_process_info,
                event_detail_json=action_detail_json,
            )
            log_event(
                conn,
                job_id,
                "regen_cover_letter_requested",
                detail_json=action_detail_json,
                initiator="web",
                process_info=action_process_info,
            )
            return {"status": "regenerating"}

        if req.target == "answers":
            # Same as reanswer
            mark_answer_refresh_pending(out_dir, request_kind="reanswer")
            update_status(
                conn,
                job_id,
                "reanswering",
                initiator="web",
                process_info=action_process_info,
                event_detail_json=action_detail_json,
            )
            log_event(
                conn,
                job_id,
                "reanswer_requested",
                detail_json=action_detail_json,
                initiator="web",
                process_info=action_process_info,
            )
            return {"status": "reanswering"}

        raise HTTPException(400, f"Unknown target: {req.target}")

    @app.post("/api/jobs/{job_id}/reanswer")
    def reanswer(job_id: int, request: Request):
        """Re-run autofill (answers only) without regenerating resume/cover letter."""
        conn = get_conn()
        job = get_job(conn, job_id)
        if not job or not job.get("output_dir"):
            raise HTTPException(400, "No output directory — cannot reanswer")
        if job["status"] not in ("draft", "submitted", "stopped", "approved", "submitting"):
            raise HTTPException(400, f"Cannot reanswer from status: {job['status']}")
        action_detail_json, action_process_info = _request_action_audit(request)
        _enforce_submission_unlock(
            conn,
            job_id,
            target_status="reanswering",
            event_detail_json=action_detail_json,
            process_info=action_process_info,
        )
        conn.execute("UPDATE jobs SET priority = MAX(priority, 100) WHERE id = ?", (job_id,))
        mark_answer_refresh_pending(Path(job["output_dir"]), request_kind="reanswer")
        update_status(
            conn,
            job_id,
            "reanswering",
            initiator="web",
            process_info=action_process_info,
            event_detail_json=action_detail_json,
        )
        log_event(
            conn,
            job_id,
            "reanswer_requested",
            detail_json=action_detail_json,
            initiator="web",
            process_info=action_process_info,
        )
        return {"status": "reanswering"}

    @app.post("/api/jobs/{job_id}/retry")
    def retry(job_id: int, request: Request):
        conn = get_conn()
        job = get_job(conn, job_id)
        if not job or job["status"] not in ("stopped", "needs_board_url"):
            raise HTTPException(400, "Cannot retry this job")
        action_detail_json, action_process_info = _request_action_audit(request)
        _enforce_submission_unlock(
            conn,
            job_id,
            target_status="queued",
            event_detail_json=action_detail_json,
            process_info=action_process_info,
        )
        update_status(
            conn,
            job_id,
            "queued",
            error_message="",
            progress="",
            clear_provider=True,
            retry_after=RETRY_AFTER_SENTINEL,
            initiator="web",
            process_info=action_process_info,
            event_detail_json=action_detail_json,
        )
        return {"status": "queued"}

    @app.post("/api/jobs/{job_id}/skip")
    def skip(job_id: int, request: Request):
        conn = get_conn()
        action_detail_json, action_process_info = _request_action_audit(request)
        update_status(
            conn,
            job_id,
            "stopped",
            error_message="Skipped via web UI",
            failure_type="user_rejected",
            initiator="web",
            process_info=action_process_info,
            event_detail_json=action_detail_json,
        )
        return {"status": "stopped"}

    @app.post("/api/jobs/{job_id}/archive")
    def archive_job(job_id: int, request: Request):
        conn = get_conn()
        job = get_job(conn, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        action_detail_json, action_process_info = _request_action_audit(request)
        # Preserve real status, only set the archived boolean
        conn.execute(
            "UPDATE jobs SET archived = TRUE, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (job_id,),
        )
        conn.commit()
        log_event(
            conn,
            job_id,
            "archived",
            detail_json=action_detail_json,
            initiator="web",
            process_info=action_process_info,
        )
        return {"status": job["status"]}

    @app.post("/api/jobs/{job_id}/unarchive")
    def unarchive_job(job_id: int, request: Request):
        conn = get_conn()
        job = get_job(conn, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if not job.get("archived"):
            raise HTTPException(400, "Job is not archived")
        action_detail_json, action_process_info = _request_action_audit(request)
        # Clear the archived boolean, preserve real status
        conn.execute(
            "UPDATE jobs SET archived = FALSE, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (job_id,),
        )
        conn.commit()
        log_event(
            conn,
            job_id,
            "unarchived",
            detail_json=action_detail_json,
            initiator="web",
            process_info=action_process_info,
        )
        return {"status": job["status"]}

    @app.post("/api/jobs/{job_id}/stop")
    def stop_job(job_id: int, request: Request):
        """Stop a running job by setting status to failed."""
        conn = get_conn()
        job = get_job(conn, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job["status"] not in (
            "generating",
            "resolving",
            "approved",
            "submitting",
            "autofilling",
            "retrying",
            "fix_in_progress",
            "reanswering",
            "regenerating",
            "queued",
        ):
            raise HTTPException(400, f"Cannot stop job in status: {job['status']}")
        action_detail_json, action_process_info = _request_action_audit(request)
        update_status(
            conn,
            job_id,
            "stopped",
            error_message="Stopped by user",
            failure_type="user_stopped",
            initiator="web",
            process_info=action_process_info,
            event_detail_json=action_detail_json,
        )
        log_event(
            conn,
            job_id,
            "stopped_by_user",
            detail_json=action_detail_json,
            initiator="web",
            process_info=action_process_info,
        )
        return {"status": "stopped"}

    class _RestartRequest(BaseModel):
        auto_submit: bool = False

    @app.post("/api/jobs/{job_id}/restart-pipeline")
    def restart_pipeline(job_id: int, request: Request, req: _RestartRequest | None = None):
        """Re-run the entire pipeline from scratch."""
        conn = get_conn()
        job = get_job(conn, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        active = {
            "generating",
            "resolving",
            "approved",
            "submitting",
            "autofilling",
            "retrying",
            "fix_in_progress",
            "reanswering",
            "regenerating",
        }
        if job["status"] in active:
            raise HTTPException(400, "Job is currently running — stop it first")
        auto_submit = req.auto_submit if req else False
        status = "queued_submit" if auto_submit else "queued"
        action_detail_json, action_process_info = _request_action_audit(request)
        _enforce_submission_unlock(
            conn,
            job_id,
            target_status=status,
            event_detail_json=action_detail_json,
            process_info=action_process_info,
        )
        # Invalidate asset cache so content is actually regenerated
        if job.get("output_dir"):
            out_dir = Path(job["output_dir"])
            clear_restart_pipeline_artifacts(out_dir, job.get("board"))
            mark_answer_refresh_pending(out_dir, request_kind="restart_pipeline")
        # Boost priority so user-initiated restarts jump the queue; clear stale provider
        conn.execute("UPDATE jobs SET priority = MAX(priority, 100) WHERE id = ?", (job_id,))
        update_status(
            conn,
            job_id,
            status,
            error_message="",
            progress="",
            clear_provider=True,
            retry_after=RETRY_AFTER_SENTINEL,
            initiator="web",
            process_info=action_process_info,
            event_detail_json=action_detail_json,
        )
        log_event(
            conn,
            job_id,
            "pipeline_restarted",
            detail="auto_submit" if auto_submit else "draft",
            detail_json=action_detail_json,
            initiator="web",
            process_info=action_process_info,
        )
        return {"status": status}

    @app.post("/api/jobs/{job_id}/board-url")
    def set_board_url(job_id: int, req: BoardUrlRequest, request: Request):
        conn = get_conn()
        action_detail_json, action_process_info = _request_action_audit(request)
        _enforce_submission_unlock(
            conn,
            job_id,
            target_status="queued",
            event_detail_json=action_detail_json,
            process_info=action_process_info,
        )
        update_status(
            conn,
            job_id,
            "queued",
            board_url=req.url,
            canonical_url=req.url,
            error_message="",
            clear_provider=True,
            retry_after=RETRY_AFTER_SENTINEL,
            initiator="web",
            process_info=action_process_info,
            event_detail_json=action_detail_json,
        )
        log_event(
            conn,
            job_id,
            "board_url_set_manually",
            detail=req.url,
            detail_json=action_detail_json,
            initiator="web",
            process_info=action_process_info,
        )
        return {"status": "queued"}

    @app.post("/api/jobs/{job_id}/draft-overrides")
    def save_draft_overrides(job_id: int, req: DraftOverridesRequest, request: Request):
        """Save overrides and trigger re-answer."""
        conn = get_conn()
        job = get_job(conn, job_id)
        if not job or not job.get("output_dir"):
            raise HTTPException(400, "No output directory")
        out_dir = Path(job["output_dir"])
        can_reanswer = job["status"] in ("draft", "submitted", "stopped", "approved", "submitting")
        action_detail_json, action_process_info = _request_action_audit(request)
        if can_reanswer:
            _enforce_submission_unlock(
                conn,
                job_id,
                target_status="reanswering",
                event_detail_json=action_detail_json,
                process_info=action_process_info,
            )

        # Save overrides
        overrides_path = out_dir / "draft_overrides.json"
        overrides_path.write_text(json.dumps(req.overrides, indent=2), encoding="utf-8")

        # Generate fix report for future reference
        from draft_manager import diff_draft_fields_from_overrides

        diff_draft_fields_from_overrides(out_dir, req.overrides)

        # Trigger re-answer with overrides applied
        if can_reanswer:
            mark_answer_refresh_pending(out_dir, request_kind="draft_overrides")
            update_status(
                conn,
                job_id,
                "reanswering",
                initiator="web",
                process_info=action_process_info,
                event_detail_json=action_detail_json,
            )
            log_event(
                conn,
                job_id,
                "reanswer_after_edit",
                detail=f"{len(req.overrides)} field(s) changed",
                detail_json=action_detail_json,
                initiator="web",
                process_info=action_process_info,
            )

        return {"status": "saved_and_reanswering", "fields": len(req.overrides)}

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: int, request: Request):
        conn = get_conn()
        job = get_job(conn, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        _, action_process_info = _request_action_audit(request)
        log.info("delete_job: job %d %s", job_id, action_process_info or "action_trigger=api")
        conn.execute("DELETE FROM events WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM fix_attempts WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM provider_runs WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM job_phase_durations WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM field_corrections WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM job_metrics WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        conn.commit()
        return {"status": "deleted"}

    # ── Interview Prep endpoints ──────────────────────────────────────

    @app.get("/api/jobs/{job_id}/interview-prep")
    async def get_interview_prep(job_id: int):
        conn = get_conn()
        row = conn.execute("SELECT output_dir FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row or not row["output_dir"]:
            raise HTTPException(404, "Job not found or no output directory")

        prep_dir = Path(row["output_dir"]) / "interview_prep"
        prep_md = prep_dir / "interview_prep.md"
        progress_file = prep_dir / ".progress.json"
        generating_file = prep_dir / ".generating"

        # Check if currently generating (PID-based lock)
        is_generating = False
        if generating_file.exists():
            try:
                pid = int(generating_file.read_text().strip())
                os.kill(pid, 0)  # Check if alive
                is_generating = True
            except (ValueError, ProcessLookupError, PermissionError):
                generating_file.unlink(missing_ok=True)

        if prep_md.exists():
            md_content = prep_md.read_text(encoding="utf-8")
            return {
                "exists": True,
                "generating": is_generating,
                "markdown": md_content,
                "docx_download": f"/api/jobs/{job_id}/interview-prep/download/docx",
                "pdf_download": f"/api/jobs/{job_id}/interview-prep/download/pdf",
            }

        progress = None
        if progress_file.exists():
            try:
                progress = json.loads(progress_file.read_text())
            except Exception:
                pass

        return {"exists": False, "generating": is_generating, "progress": progress}

    @app.post("/api/jobs/{job_id}/interview-prep")
    async def generate_interview_prep_endpoint(job_id: int, request: Request):
        conn = get_conn()
        row = conn.execute("SELECT output_dir, company, role_title FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row or not row["output_dir"]:
            raise HTTPException(404, "Job not found or no output directory")

        prep_dir = Path(row["output_dir"]) / "interview_prep"
        generating_file = prep_dir / ".generating"

        # Concurrency guard
        if generating_file.exists():
            try:
                pid = int(generating_file.read_text().strip())
                os.kill(pid, 0)
                raise HTTPException(409, "Interview prep generation already in progress")
            except (ValueError, ProcessLookupError, PermissionError):
                generating_file.unlink(missing_ok=True)

        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        stage = body.get("stage", "General")
        interviewers = body.get("interviewers", "")
        notes = body.get("notes", "")

        # Build CLI args
        cmd = [
            "uv",
            "run",
            "python",
            "scripts/generate_interview_prep.py",
            row["output_dir"],
            "--stage",
            stage,
            "--force",
        ]
        if interviewers:
            for line in interviewers.strip().splitlines():
                line = line.strip()
                if line:
                    cmd.extend(["--interviewer", line])
        if notes:
            cmd.extend(["--notes", notes])

        # Log event
        log_event(
            conn,
            job_id,
            "interview_prep_started",
            detail_json={"stage": stage, "interviewers": interviewers, "notes": notes},
            initiator="web",
        )

        # Spawn in background thread
        def _run_prep():
            bg_conn = open_db(DB_PATH, check_same_thread=False)
            prep_dir.mkdir(parents=True, exist_ok=True)
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(PROJECT_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                )
                generating_file.write_text(str(proc.pid))
                stdout, stderr = proc.communicate(timeout=900)
                if proc.returncode == 0:
                    log_event(bg_conn, job_id, "interview_prep_completed", initiator="web")
                else:
                    log_event(bg_conn, job_id, "interview_prep_failed", detail=stderr[:500], initiator="web")
            except subprocess.TimeoutExpired:
                log_event(
                    bg_conn,
                    job_id,
                    "interview_prep_failed",
                    detail="Generation timed out after 15 minutes",
                    initiator="web",
                )
            except Exception as exc:
                log_event(bg_conn, job_id, "interview_prep_failed", detail=str(exc)[:500], initiator="web")
            finally:
                generating_file.unlink(missing_ok=True)
                bg_conn.close()

        threading.Thread(target=_run_prep, daemon=True).start()
        return {"status": "started"}

    @app.get("/api/jobs/{job_id}/interview-prep/download/{fmt}")
    async def download_interview_prep(job_id: int, fmt: str):
        if fmt not in ("docx", "pdf"):
            raise HTTPException(400, "Format must be 'docx' or 'pdf'")
        conn = get_conn()
        row = conn.execute("SELECT output_dir FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row or not row["output_dir"]:
            raise HTTPException(404, "Job not found")
        prep_dir = Path(row["output_dir"]) / "interview_prep"
        matches = list(prep_dir.glob(f"*.{fmt}"))
        if not matches:
            raise HTTPException(404, f"No .{fmt} file found")
        file_path = matches[0]
        media_type = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if fmt == "docx"
            else "application/pdf"
        )
        return FileResponse(file_path, media_type=media_type, filename=file_path.name)

    # ── Worker endpoints ─────────────────────────────────────────────
    @app.get("/api/workers/status")
    def worker_status():
        conn = get_conn()
        # Active jobs = what workers are currently doing
        active = conn.execute(
            """SELECT id, company, role_title, board, status, progress, provider
               FROM jobs WHERE status IN ('generating', 'resolving', 'submitting',
               'retrying', 'fix_in_progress', 'reanswering')
               ORDER BY updated_at DESC"""
        ).fetchall()
        active_list = [dict(r) for r in active]
        _add_submitted_flags(conn, active_list)
        runtime_services = _read_runtime_services()
        return {
            "running": runtime_services["workers_running"],
            "repair_supervisor_running": runtime_services["repair_supervisor_running"],
            "repair_queue_paused": runtime_services["repair_queue_paused"],
            "repair_queue_pause": runtime_services["repair_queue_pause"],
            "active_jobs": active_list,
        }

    @app.post("/api/workers/start")
    def worker_start():
        start_workers()
        return {"status": "started"}

    @app.post("/api/workers/stop")
    def worker_stop():
        stop_workers()
        return {"status": "stopped"}

    @app.post("/api/workers/restart")
    def worker_restart():
        stop_workers()
        start_workers()
        return {"status": "restarted"}

    @app.get("/api/workers/detail")
    def worker_detail():
        """Return per-worker states from the worker registry."""
        return {
            "workers": _read_worker_states(),
            "running": is_worker_running(),
        }

    @app.post("/api/workers/{worker_id}/stop")
    def worker_stop_single(worker_id: int):
        """Gracefully stop a single worker."""
        _send_worker_command({"type": "stop_worker", "worker_id": worker_id})
        return {"status": "stop_requested", "worker_id": worker_id}

    @app.post("/api/workers/{worker_id}/kill")
    def worker_kill_single(worker_id: int):
        """Kill a single worker and requeue its current job."""
        _send_worker_command({"type": "kill_worker", "worker_id": worker_id})
        return {"status": "kill_requested", "worker_id": worker_id}

    class _ScaleRequest(BaseModel):
        count: int

    @app.post("/api/workers/scale")
    def worker_scale(req: _ScaleRequest):
        """Scale the worker pool to the given count."""
        if req.count < 1 or req.count > 100:
            raise HTTPException(400, "Worker count must be between 1 and 100")
        _send_worker_command({"type": "scale", "count": req.count})
        return {"status": "scale_requested", "count": req.count}

    # ── Stats endpoints ──────────────────────────────────────────────
    @app.get("/api/stats/summary")
    def stats_summary(since: str | None = None):
        conn = get_conn()
        return get_summary_stats(conn, since=since)

    @app.get("/api/stats/phases")
    def stats_phases():
        conn = get_conn()
        return get_phase_avg_durations(conn)

    @app.get("/api/stats/boards")
    def stats_boards():
        conn = get_conn()
        return get_board_error_rates(conn)

    @app.get("/api/stats/processed")
    def stats_processed():
        conn = get_conn()
        return get_jobs_processed_counts(conn)

    @app.get("/api/stats/counts")
    def stats_counts():
        conn = get_conn()
        return get_status_counts(conn)

    # ── Content endpoints ────────────────────────────────────────────
    @app.get("/api/jobs/{job_id}/content/{filename}")
    def get_job_content(job_id: int, filename: str):
        """Read a content file from the job's output directory."""
        conn = get_conn()
        job = get_job(conn, job_id)
        if not job or not job.get("output_dir"):
            raise HTTPException(404, "No output directory")
        # Whitelist allowed filenames
        allowed = {
            "application_answers.json",
            "resume_content.json",
            "cover_letter_text.txt",
            "jd_parsed.json",
            "draft_overrides.json",
            "linked_resource_context.json",
            "linked_resource_failures.json",
            "autofill_report.json",
            "autofill_report.md",
            "autofill_pre_submit.png",
            "autofill_review.png",
            "autofill_post_submit.png",
            "submit_debug.png",
        }
        if filename not in allowed:
            if not filename.endswith(
                (
                    "_autofill_report.json",
                    "_pre_submit.png",
                    "_review.png",
                    "_post_submit.png",
                    "_submit_debug.png",
                    "_autofill_report.md",
                    ".pdf",
                    ".docx",
                )
            ):
                raise HTTPException(403, "File not allowed")
        # Block path traversal
        if "/" in filename or filename.startswith("..") or "/.." in filename:
            raise HTTPException(403, "Invalid filename")
        base = Path(job["output_dir"]).resolve()
        proof = resolve_current_submit_artifacts(base, board_name=job.get("board"))
        alias_map = {
            "application_answers.json": proof.get("application_answers_json"),
            "autofill_report.json": proof.get("report_json"),
            "autofill_report.md": proof.get("report_md"),
            "autofill_pre_submit.png": proof.get("pre_submit_screenshot"),
            "autofill_review.png": proof.get("review_screenshot"),
            "autofill_post_submit.png": proof.get("post_submit_screenshot"),
            "submit_debug.png": proof.get("submit_debug_screenshot"),
            "linked_resource_context.json": proof.get("linked_resource_context_json"),
            "linked_resource_failures.json": proof.get("linked_resource_failures_json"),
        }
        alias_candidate = alias_map.get(filename)
        if alias_candidate is not None:
            candidate = Path(alias_candidate).resolve()
            if not _path_within_base(base, candidate):
                raise HTTPException(403, "Path traversal blocked")
            if candidate.exists():
                if filename.endswith(".png"):
                    return FileResponse(
                        candidate,
                        media_type="image/png",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
                    )
                text = candidate.read_text(encoding="utf-8")
                if filename.endswith(".json"):
                    return json.loads(text)
                return {"text": text}
        # Serve PDF/DOCX from documents/ or root (old layout)
        if filename.endswith((".pdf", ".docx")):
            for subdir in ("documents", ""):
                candidate = (base / subdir / filename if subdir else base / filename).resolve()
                if _path_within_base(base, candidate) and candidate.exists():
                    media = (
                        "application/pdf"
                        if filename.endswith(".pdf")
                        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    )
                    return FileResponse(
                        candidate, media_type=media, headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
                    )
            raise HTTPException(404, f"{filename} not found")
        path = base
        submit_candidates = []
        active_submit_candidate = role_submit_path(base, filename).resolve()
        submit_candidates.append(active_submit_candidate)
        default_submit_candidate = (default_role_submit_dir(base) / filename).resolve()
        if default_submit_candidate != active_submit_candidate:
            submit_candidates.append(default_submit_candidate)
        for candidate in [*(submit_candidates), (path / "content" / filename).resolve(), (path / filename).resolve()]:
            if not _path_within_base(base, candidate):
                raise HTTPException(403, "Path traversal blocked")
            if candidate.exists():
                # Serve images as FileResponse
                if filename.endswith(".png"):
                    return FileResponse(
                        candidate,
                        media_type="image/png",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
                    )
                text = candidate.read_text(encoding="utf-8")
                if filename.endswith(".json"):
                    return json.loads(text)
                return {"text": text}
        raise HTTPException(404, f"{filename} not found")

    @app.get("/api/jobs/{job_id}/logs")
    def get_job_logs(job_id: int, lines: int = 500):
        """Get recent log output for a job from various log sources."""
        conn = get_conn()
        job = get_job(conn, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        output_parts = []

        # 1. Collect log files from output_dir
        if job.get("output_dir"):
            base = Path(job["output_dir"])
            log_patterns = [
                "content/llm_research_raw.txt",
                "content/llm_drafting_raw.txt",
                "content/llm_fix_attempt_*_raw.txt",
                "submit*/*_autofill_report.md",
                "submit*/application_submission_result.json",
                "submit*/pending_user_input.json",
                "submit*/job_unavailable.json",
                "submit*/unsupported_board.json",
                "submit*/workday_auth_failure.json",
                "submit*/icims_auth_failure.json",
            ]
            for pattern in log_patterns:
                for f in sorted(base.glob(pattern)):
                    try:
                        text = f.read_text(encoding="utf-8", errors="replace")
                        if text.strip():
                            header = f"── {f.relative_to(base)} ──"
                            output_parts.append(f"{header}\n{text[-2000:]}")
                    except OSError:
                        pass

        # 2. Events as structured log
        events = get_job_timeline(conn, job_id)
        if events:
            event_lines = []
            for ev in events[:50]:
                ts = ev.get("created_at", "")
                etype = ev.get("event_type", "")
                detail = ev.get("detail", "") or ""
                event_lines.append(f"[{ts}] {etype}  {detail}")
            output_parts.append("── events ──\n" + "\n".join(event_lines))

        if not output_parts and job.get("output_dir"):
            # For draft-only jobs with no logs/events, show materials summary
            # Check both new layout (content/) and old layout (root)
            base = Path(job["output_dir"])
            materials = []
            if (base / "content" / "resume_content.json").exists() or (base / "resume_content.json").exists():
                materials.append("Resume \u2713")
            if (base / "content" / "cover_letter_text.txt").exists() or (base / "cover_letter_text.txt").exists():
                materials.append("Cover Letter \u2713")
            if (base / "content" / "jd_parsed.json").exists() or (base / "jd_parsed.json").exists():
                materials.append("JD Parsed \u2713")
            # Check both documents/ and root for PDFs
            pdfs = []
            for search_dir in [base / "documents", base]:
                if search_dir.is_dir():
                    pdfs.extend(search_dir.glob("*.pdf"))
            if pdfs:
                materials.append(f"{len(pdfs)} PDF(s) \u2713")
            if materials:
                output_parts.append(
                    "\u2500\u2500 materials generated \u2500\u2500\n"
                    + "  ".join(materials)
                    + "\n\nNot yet submitted \u2014 no autofill logs available."
                )

        output = "\n\n".join(output_parts) if output_parts else "(no output yet)"
        # Trim to last N lines
        all_lines = output.split("\n")
        if len(all_lines) > lines:
            output = "\n".join(all_lines[-lines:])
        return {"output": output}

    @app.get("/api/jobs/{job_id}/documents")
    def list_documents(job_id: int):
        """List available document files (PDFs, DOCX) for a job.

        Checks both new layout (documents/) and old layout (root).
        """
        conn = get_conn()
        job = get_job(conn, job_id)
        if not job or not job.get("output_dir"):
            return {"files": []}
        base = Path(job["output_dir"])
        files = []
        seen = set()
        # Check new layout first, then old layout (root)
        for search_dir in [base / "documents", base]:
            if not search_dir.is_dir():
                continue
            for f in sorted(search_dir.iterdir()):
                if f.suffix in (".pdf", ".docx", ".txt") and f.name not in seen:
                    # Skip non-document txt files in root (e.g., jd_raw.md, cover_letter_text.txt)
                    if search_dir == base and f.suffix == ".txt" and "Jerrison" not in f.name:
                        continue
                    files.append({"name": f.name, "type": f.suffix[1:]})
                    seen.add(f.name)

        preferred_names: list[str] = []
        try:
            from application_submit_common import find_cover_letter_file, find_resume_file

            for resolver in (find_resume_file, find_cover_letter_file):
                try:
                    preferred_name = resolver(base).name
                except FileNotFoundError:
                    continue
                if preferred_name not in preferred_names:
                    preferred_names.append(preferred_name)
        except Exception:
            preferred_names = []

        preferred_rank = {name: index for index, name in enumerate(preferred_names)}
        files.sort(
            key=lambda item: (
                preferred_rank.get(item["name"], len(preferred_names)),
                0 if item["type"] == "pdf" else 1,
                item["name"],
            )
        )
        return {"files": files}

    @app.get("/api/events/recent")
    def recent_events(limit: int = 30):
        conn = get_conn()
        rows = conn.execute(
            """SELECT e.*, j.company, j.role_title
               FROM events e LEFT JOIN jobs j ON e.job_id = j.id
               ORDER BY e.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await manager.connect(ws)
        try:
            # Send initial bulk
            conn = get_conn()
            jobs = _enrich_queue_rows(conn, query_queue_jobs(conn, limit=200))
            await ws.send_text(json.dumps({"type": "job_bulk", "jobs": jobs}, default=str))
            # Also send worker status
            runtime_services = _read_runtime_services()
            await ws.send_text(
                json.dumps(
                    {
                        "type": "worker_status",
                        "running": runtime_services["workers_running"],
                        "repair_supervisor_running": runtime_services["repair_supervisor_running"],
                        "repair_queue_paused": runtime_services["repair_queue_paused"],
                        "repair_queue_pause": runtime_services["repair_queue_pause"],
                    },
                    default=str,
                )
            )
            # Keep alive — client can send pings
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(ws)

    # ── Discover endpoints ───────────────────────────────────────────
    from job_discovery import (  # noqa: I001
        delete_candidate,
        find_duplicate_jobs,
        get_candidate_stats,
        list_candidates,
        promote_candidate,
        score_candidate,
        score_unscored_candidates,
        search_jobs as discover_search_jobs,
        skip_candidate,
    )

    @app.get("/api/discover/candidates")
    def discover_list_candidates(
        status: str | None = None,
        source: str | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ):
        conn = get_conn()
        candidates = list_candidates(
            conn,
            status=status,
            source=source,
            search=search,
            limit=limit,
            offset=offset,
        )
        stats = get_candidate_stats(conn)
        # Tag candidates that match existing jobs in the queue
        duplicates = find_duplicate_jobs(conn, candidates)
        for c in candidates:
            c["duplicate_of"] = duplicates.get(c["id"])
        return {"candidates": candidates, "stats": stats}

    @app.post("/api/discover/search")
    def discover_search(req: DiscoverSearchRequest):
        conn = get_conn()
        inserted = discover_search_jobs(
            conn,
            search_term=req.search_term,
            location=req.location,
            results_wanted=req.results_wanted,
            sources=req.sources,
        )

        # Score in background
        def _score_background():
            bg_conn = open_db(DB_PATH, check_same_thread=False)
            try:
                score_unscored_candidates(bg_conn)
            except Exception:
                log.exception("Background scoring failed")
            finally:
                bg_conn.close()

        threading.Thread(target=_score_background, daemon=True).start()

        return {"inserted": len(inserted), "candidates": inserted}

    @app.post("/api/discover/candidates/{candidate_id}/promote")
    def discover_promote(candidate_id: int):
        conn = get_conn()
        try:
            job_id = promote_candidate(conn, candidate_id)
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
        if job_id is None:
            raise HTTPException(400, "Already promoted or duplicate URL")
        return {"status": "promoted", "job_id": job_id}

    @app.post("/api/discover/candidates/{candidate_id}/skip")
    def discover_skip(candidate_id: int):
        conn = get_conn()
        skip_candidate(conn, candidate_id)
        return {"status": "skipped"}

    @app.post("/api/discover/candidates/{candidate_id}/unskip")
    def discover_unskip(candidate_id: int):
        conn = get_conn()
        row = conn.execute("SELECT score FROM candidate_jobs WHERE id = ?", (candidate_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Candidate not found")
        new_status = "scored" if row["score"] is not None else "new"
        conn.execute(
            "UPDATE candidate_jobs SET status = ? WHERE id = ?",
            (new_status, candidate_id),
        )
        conn.commit()
        return {"status": new_status}

    @app.delete("/api/discover/candidates/{candidate_id}")
    def discover_delete(candidate_id: int):
        conn = get_conn()
        delete_candidate(conn, candidate_id)
        return {"status": "deleted"}

    @app.post("/api/discover/candidates/promote-bulk")
    def discover_promote_bulk(req: PromoteBulkRequest):
        conn = get_conn()
        promoted = []
        failed = []
        for candidate_id in req.ids:
            try:
                job_id = promote_candidate(conn, candidate_id)
                if job_id is not None:
                    promoted.append({"candidate_id": candidate_id, "job_id": job_id})
                else:
                    failed.append({"candidate_id": candidate_id, "reason": "duplicate or already promoted"})
            except Exception as e:  # noqa: BLE001
                failed.append({"candidate_id": candidate_id, "reason": str(e)})
        return {"promoted": promoted, "failed": failed}

    @app.post("/api/discover/candidates/{candidate_id}/score")
    def discover_score_single(candidate_id: int):
        def _score_bg():
            bg_conn = open_db(DB_PATH, check_same_thread=False)
            try:
                score_candidate(bg_conn, candidate_id)
            except Exception:
                log.exception("Scoring candidate %d failed", candidate_id)
            finally:
                bg_conn.close()

        threading.Thread(target=_score_bg, daemon=True).start()
        return {"status": "scoring_started"}

    @app.post("/api/discover/score-all")
    def discover_score_all():
        """Score all unscored candidates in a background thread."""
        conn = get_conn()
        unscored = conn.execute(
            "SELECT COUNT(*) FROM candidate_jobs WHERE status = 'new' AND score IS NULL"
        ).fetchone()[0]
        if unscored == 0:
            return {"status": "nothing_to_score", "unscored": 0}

        def _score_all_bg():
            bg_conn = open_db(DB_PATH, check_same_thread=False)
            try:
                score_unscored_candidates(bg_conn)
            except Exception:
                log.exception("Background score-all failed")
            finally:
                bg_conn.close()

        threading.Thread(target=_score_all_bg, daemon=True).start()
        return {"status": "scoring_started", "unscored": unscored}

    return app


def _kill_port(port: int) -> None:
    """Kill any process listening on the given port (SIGTERM first, then SIGKILL)."""
    import time

    result = subprocess.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True)
    pids = []
    for pid_str in result.stdout.strip().split():
        try:
            pid = int(pid_str)
            if pid == os.getpid():
                continue  # Don't kill ourselves
            pids.append(pid)
        except ValueError:
            continue

    if not pids:
        return

    # Send SIGTERM first — gives the old process time to close DB connections
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    # Wait up to 3 seconds for graceful exit
    for _ in range(30):
        pids = [p for p in pids if _pid_alive(p)]
        if not pids:
            return
        time.sleep(0.1)

    # Escalate to SIGKILL for any remaining processes
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    """Check if a process is still running."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main():
    global _auto_start_workers, _configured_num_workers

    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Job Application Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (use 0.0.0.0 for LAN)")
    parser.add_argument("--port", type=int, default=8420, help="Port (default: 8420)")
    parser.add_argument("--workers", type=int, default=16, help="Number of job workers")
    parser.add_argument("--with-workers", action="store_true", help="Auto-start workers with the web server")
    args = parser.parse_args()

    _kill_port(args.port)
    _configured_num_workers = args.workers

    if args.with_workers:
        _auto_start_workers = True

    app = create_app()
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    except KeyboardInterrupt:
        pass
    finally:
        close_all_connections()
        sys.exit(0)


if __name__ == "__main__":
    main()
