"""Core pipeline orchestration for the job worker, CLI, and TUI, covering provider_fallback, process_job lifecycle, retry_with_recording, and auto_fix."""

from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

PROJECT_ROOT = SCRIPT_DIR.parent

CAPTCHA_SKIP_EXIT_CODE = 75
AUTH_FAILURE_EXIT_CODE = 75  # same skip code for auth failures

from answer_refresh_state import (  # noqa: E402 — after sys.path setup
    STATUS_FAILED,
    STATUS_FRESH,
    STATUS_NOT_APPLICABLE,
    STATUS_PENDING,
    fail_pending_answer_refresh,
    finalize_answer_refresh,
    load_answer_refresh_artifact_proof,
    load_answer_refresh_state,
    mark_answer_refresh_pending,
)
from job_board_urls import icims_auth_scope, workday_auth_scope  # noqa: E402 — after sys.path setup
from job_db import (  # noqa: E402 — after sys.path setup
    RETRY_AFTER_SENTINEL,
    SubmissionLockError,
    enforce_submission_lock,
    get_job,
    log_event,
    repair_submission_locked_job,
)
from llm_provider import (  # noqa: E402 — after sys.path setup
    automation_provider_chain,
    default_active_provider,
    provider_available,
    provider_command_for_mode,
)
from pipeline_audit_loop import (
    audit_draft_outcome,
    audit_stopped_outcome,
    clear_audit_failure_report,
    write_audit_failure_report,
)
from pipeline_draft_proof import (  # noqa: E402 — after sys.path setup
    _mark_job_unavailable_and_archive,
    _sync_draft_proof_blockers,
    _validate_draft_completeness,
)
from pipeline_meta_common import (
    enrich_pipeline_meta_urls as _enrich_pipeline_meta,
)
from pipeline_meta_common import (
    load_pipeline_meta_if_present as _load_pipeline_meta,
)
from pipeline_reset_helpers import (  # noqa: E402 — after sys.path setup
    _finalize_reset_job_to_new,
    clear_restart_pipeline_artifacts,
)
from submission_result_outcomes import handle_draft_mode_submission_result
from worker_subprocess import prepare_worker_subprocess_kwargs

# Concurrent auto-fix workers. Default 3 (was 1, too restrictive).
_AUTO_FIX_CONCURRENCY = int(os.environ.get("AUTO_FIX_CONCURRENCY", "3"))
_auto_fix_semaphore = threading.Semaphore(_AUTO_FIX_CONCURRENCY)

# Maximum automatic retries before flagging as needs_attention.
MAX_AUTO_RETRIES = int(os.environ.get("MAX_AUTO_RETRIES", "3"))
_AUDIT_RETRY_LIMIT = 3

# In-place retries within provider_fallback for rate-limit errors.
# Avoids requeuing the entire job for transient rate limits.
_RATE_LIMIT_RETRIES = int(os.environ.get("RATE_LIMIT_RETRIES", "2"))

_RATE_LIMIT_PATTERNS = ("rate limit", "rate_limit", "ratelimit", "429", "too many requests", "overloaded")
_BOARD_RATE_LIMIT_PATTERNS = ("rate limit", "ratelimit", "429", "too many requests")
_PROVIDER_RATE_LIMIT_PATTERNS = _RATE_LIMIT_PATTERNS + (
    "out of extra usage",
    "usage limit",
    "exhausted your capacity on this model",
    "terminalquotaerror",
    "purchase more credits",
    "quota will reset after",
)
_RETRY_DELAYS = [120, 480, 1800]
_PROVIDER_INDEPENDENT_FAILURE_PATTERNS = (
    "url-based jd extraction did not produce a usable job description",
    "stopping instead of generating assets from weak content",
    "extracted page looks like an html/login/blocker shell instead of a job description",
    "parser could not identify a credible job title from the extracted page",
    "too little job-description text was extracted from the website",
    "parsed jd did not contain enough structured detail to trust the extraction",
    "job_closed:",
    "needs_board_url:",
    "skipped_captcha:",
    "unsupported:",
)

_NON_TRANSIENT_TYPES = frozenset(
    {
        "already_applied",
        "audit_failure",
        "auth_failed",
        "auth_guarded",
        "auth_unknown",
        "duplicate",
        "external_apply",
        "incomplete",
        "job_closed",
        "linkedin_unknown_questions",
        "no_apply_button",
        "pending_user_input",
        "skipped_captcha",
        "unsupported",
        "user_rejected",
        "user_stopped",
    }
)

_LINKEDIN_TARGETED_RETRY_FAILURE_TYPES = frozenset(
    {
        "linkedin_modal_missing",
        "linkedin_validation_loop",
        "linkedin_navigation_missing",
        "linkedin_timeout_after_partial_fill",
    }
)

# Transient failure patterns that should trigger auto-retry.
_TRANSIENT_PATTERNS = (
    "retry later",
    "rate limit",
    "timed out",
    "auto-fix slot busy",
    "timeout",
    "cannot commit",
    "no transaction",
    "connection reset",
    "connection refused",
    "service interruption",
    "currently unavailable",
)

_UNKNOWN_AUTO_RETRIES = min(MAX_AUTO_RETRIES, 2)

# Per-provider concurrency limiters to avoid rate limiting.
# Limits concurrent LLM subprocess calls per provider.
_provider_semaphores: dict[str, threading.Semaphore] = {}
_provider_sem_lock = threading.Lock()
_PROVIDER_CONCURRENCY = int(os.environ.get("LLM_PROVIDER_CONCURRENCY", "15"))


def _get_provider_semaphore(provider: str) -> threading.Semaphore:
    """Get or create a concurrency semaphore for a provider."""
    with _provider_sem_lock:
        if provider not in _provider_semaphores:
            _provider_semaphores[provider] = threading.Semaphore(_PROVIDER_CONCURRENCY)
        return _provider_semaphores[provider]


DEFAULT_ASSET_TIMEOUT = int(os.environ.get("JOB_ASSETS_GENERATION_TIMEOUT", "900"))  # 15 minutes
DEFAULT_SUBMIT_TIMEOUT = 900  # 15 minutes
_CAPTCHA_TIMEOUT = int(os.environ.get("JOB_ASSETS_CAPTCHA_TIMEOUT", "3600"))

log = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

# ANSI escape sequences and box-drawing characters that clutter error messages.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_BOX_DRAWING_RE = re.compile(
    r"^[\s─━│┃┄┅┆┇┈┉┊┋┌┍┎┏┐┑┒┓└┕┖┗┘┙┚┛├┝┞┟┠┡┢┣┤┥┦┧┨┩┪┫┬┭┮┯┰┱┲┳┴┵┶┷┸┹┺┻┼┽┾┿╀╁╂╃╄╅╆╇╈╉╊╋═║╔╗╚╝╞╟╠╡╢╣╤╥╦╧╨╩╪╫╬]+$"
)
_RETRY_AFTER_REGEXPS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"retry[- ]after[:= ]+(\d+)\b"), 1),
    (re.compile(r"retry in (\d+)\s*seconds?\b"), 1),
    (re.compile(r"retry in (\d+)\s*minutes?\b"), 60),
    (re.compile(r"try again in (\d+)\s*seconds?\b"), 1),
    (re.compile(r"try again in (\d+)\s*minutes?\b"), 60),
    (re.compile(r"wait (\d+)\s*seconds?\b"), 1),
    (re.compile(r"wait (\d+)\s*minutes?\b"), 60),
)


def _extract_error_hint(stderr: str, max_len: int = 500) -> str:
    """Extract a meaningful error hint from stderr output.

    Strips ANSI escapes and box-drawing lines, then returns the last
    meaningful content (up to *max_len* chars).
    """
    if not stderr or not isinstance(stderr, str):
        return ""
    cleaned = _ANSI_RE.sub("", stderr)
    lines = [ln.strip() for ln in cleaned.strip().splitlines() if ln.strip()]
    # Drop lines that are purely box-drawing / separator characters
    meaningful = [ln for ln in lines if not _BOX_DRAWING_RE.match(ln)]
    if not meaningful:
        return lines[-1][:max_len] if lines else ""
    # Take last N chars worth of meaningful lines
    result_lines: list[str] = []
    total = 0
    for ln in reversed(meaningful):
        if total + len(ln) + 1 > max_len:
            break
        result_lines.append(ln)
        total += len(ln) + 1
    return "\n".join(reversed(result_lines))


def _extract_terminal_generation_hint(text: str | None) -> str:
    cleaned = _ANSI_RE.sub("", text or "")
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.casefold()
        for prefix in ("job_closed:", "needs_board_url:", "skipped_captcha:", "unsupported:"):
            idx = lowered.find(prefix)
            if idx != -1:
                return line[idx:]
    return ""


def _escalated_timeout(base_timeout: int, last_failure_type: str | None) -> int:
    """Increase timeout only for retries following a timeout failure."""
    if last_failure_type != "timeout":
        return base_timeout
    return min(int(base_timeout * 1.5), DEFAULT_ASSET_TIMEOUT * 2)


def _is_provider_rate_limited_text(text: str | None) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    return any(pattern in lowered for pattern in _PROVIDER_RATE_LIMIT_PATTERNS)


def _retry_after_seconds_from_message(error_message: str | None) -> int | None:
    lowered = (error_message or "").lower()
    if not lowered:
        return None
    for pattern, multiplier in _RETRY_AFTER_REGEXPS:
        match = pattern.search(lowered)
        if match is None:
            continue
        try:
            return max(int(match.group(1)) * multiplier, 1)
        except ValueError:
            continue
    return None


def _is_provider_independent_failure(stderr: str) -> bool:
    """Return True when retrying another model cannot change the failure."""
    lowered = (stderr or "").lower()
    if not lowered:
        return False
    return any(pattern in lowered for pattern in _PROVIDER_INDEPENDENT_FAILURE_PATTERNS)


# ── Provider Fallback ────────────────────────────────────────────────────────


def provider_fallback(
    base_cmd: list[str],
    providers: list[str],
    *,
    timeout: int | None = None,
    progress_callback=None,
) -> tuple[str | None, int]:
    """Try providers in order via subprocess. Return (winning_provider, exit_code).

    Appends ``--provider <name>`` to *base_cmd* for each candidate.
    The first provider whose subprocess exits 0 wins.
    If all fail, returns ``(None, <last_exit_code>)``.
    If *providers* is empty, returns ``(None, 1)`` without running anything.
    Exceptions from subprocess (e.g. TimeoutExpired) are caught and treated
    as failures so the next provider can be tried.
    """
    if not providers:
        provider_fallback.last_failure_type = "generation_failed"  # type: ignore[attr-defined]
        return None, 1

    last_exit_code = 1
    last_error_hint = ""
    attempted_provider = False
    all_failures_rate_limited = True
    provider_fallback.last_failure_type = ""  # type: ignore[attr-defined]
    for i, provider in enumerate(providers, 1):
        attempted_provider = True
        # Exponential backoff with jitter between provider retries
        if i > 1:
            backoff = min(2 ** (i - 1) * 2, 30) + random.uniform(0, 2)
            log.info("rate-limit backoff: %.1fs before trying %s", backoff, provider)
            if progress_callback:
                progress_callback(f"Waiting {backoff:.0f}s before trying {provider}...")
            time.sleep(backoff)
        if progress_callback:
            progress_callback(f"Trying provider {provider} ({i}/{len(providers)})...")
        cmd = [*base_cmd, "--provider", provider]
        run_kwargs: dict = {"capture_output": True, "text": True}
        if timeout is not None:
            run_kwargs["timeout"] = timeout

        # Inner retry loop for rate-limit errors on the same provider.
        for attempt in range(_RATE_LIMIT_RETRIES + 1):
            sem = _get_provider_semaphore(provider)
            sem.acquire()
            try:
                result = _run_worker_subprocess(cmd, **run_kwargs)
                last_exit_code = result.returncode
                stdout_text = result.stdout if isinstance(result.stdout, str) else ""
                stderr_text = result.stderr if isinstance(result.stderr, str) else ""
                combined_output = "\n".join(
                    part for part in (stdout_text.strip(), stderr_text.strip()) if part
                )
                if result.returncode == 0:
                    provider_fallback.last_failure_type = ""  # type: ignore[attr-defined]
                    return provider, 0
                # Extract a meaningful error hint from stdout/stderr
                last_error_hint = _extract_terminal_generation_hint(combined_output) or _extract_error_hint(
                    combined_output
                ) or f"exit {last_exit_code}"
                # Check if this is a rate-limit error worth retrying in place
                is_rate_limited = _is_provider_rate_limited_text(combined_output)
                if is_rate_limited and attempt < _RATE_LIMIT_RETRIES:
                    backoff = min(2**attempt * 5, 120) + random.uniform(0, 3)
                    log.info(
                        "rate-limited by %s (attempt %d/%d), retrying in %.1fs",
                        provider,
                        attempt + 1,
                        _RATE_LIMIT_RETRIES,
                        backoff,
                    )
                    if progress_callback:
                        progress_callback(
                            f"Rate limited — retrying in {backoff:.0f}s (attempt {attempt + 1}/{_RATE_LIMIT_RETRIES})..."
                        )
                    time.sleep(backoff)
                    continue  # retry same provider
                if not is_rate_limited:
                    all_failures_rate_limited = False
                if _is_provider_independent_failure(stderr_text):
                    log.info(
                        "stopping provider fallback after %s due to provider-independent failure: %s",
                        provider,
                        last_error_hint,
                    )
                    if progress_callback:
                        progress_callback(f"Provider {provider} failed: {last_error_hint[:120]}")
                    provider_fallback.last_error_hint = last_error_hint  # type: ignore[attr-defined]
                    provider_fallback.last_failure_type = "generation_failed"  # type: ignore[attr-defined]
                    return None, last_exit_code
                if progress_callback:
                    progress_callback(f"Provider {provider} failed: {last_error_hint[:120]}")
            except subprocess.TimeoutExpired:
                log.warning("provider %s timed out", provider)
                last_error_hint = "timed out"
                all_failures_rate_limited = False
                if progress_callback:
                    progress_callback(f"Provider {provider} timed out")
                last_exit_code = 1
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("provider %s failed with exception: %s", provider, exc)
                last_error_hint = str(exc)[:120]
                all_failures_rate_limited = False
                if progress_callback:
                    progress_callback(f"Provider {provider} error: {last_error_hint}")
                last_exit_code = 1
            finally:
                sem.release()
            break  # non-rate-limit failure or exhausted retries — move to next provider

    # Store the error hint for callers to use in error messages
    provider_fallback.last_error_hint = last_error_hint  # type: ignore[attr-defined]
    provider_fallback.last_failure_type = (  # type: ignore[attr-defined]
        "llm_rate_limited" if attempted_provider and all_failures_rate_limited else "generation_failed"
    )
    return None, last_exit_code


# ── Draft CAS Transitions ───────────────────────────────────────────────────


_APPROVABLE_DRAFT_STATUSES = ("draft", "stopped", "submitted")


def current_draft_pending_user_input(conn: sqlite3.Connection, job_id: int) -> tuple[Path, dict] | None:
    """Return current pending-user-input payload for the active submit attempt."""
    from application_submit_common import load_pending_user_input_for_submit_attempt

    row = conn.execute("SELECT output_dir FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row or not row["output_dir"]:
        return None
    return load_pending_user_input_for_submit_attempt(Path(row["output_dir"]))


def approve_job_failure_message(conn: sqlite3.Connection, job_id: int) -> str:
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return f"Job #{job_id} was not found."
    status = str(row["status"] or "")
    if status not in _APPROVABLE_DRAFT_STATUSES:
        return f"Cannot approve job #{job_id} from status '{status}'."
    pending = current_draft_pending_user_input(conn, job_id)
    if pending is None:
        return f"Cannot approve job #{job_id}."
    _pending_path, payload = pending
    questions = list(payload.get("questions") or [])
    first_label = str((questions[0].get("label") if questions else "") or "").strip()
    if first_label:
        return f"Cannot approve incomplete draft: review {first_label} before submitting."
    return "Cannot approve incomplete draft: unresolved fields still need review before submitting."


def _repair_locked_job_for_retry_refusal(conn: sqlite3.Connection, job_id: int, *, initiator: str) -> None:
    if repair_submission_locked_job(conn, job_id, initiator=initiator):
        return
    row = conn.execute(
        "SELECT status, confirmed_at, submission_lock_state FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if not row:
        return
    lock_state = str(row["submission_lock_state"] or "").strip()
    is_locked = lock_state == "locked" or (lock_state != "unlocked_for_resubmit" and bool(row["confirmed_at"]))
    if not is_locked or str(row["status"] or "") != "stopped":
        return
    conn.execute(
        "UPDATE jobs SET status = 'submitted', provider = NULL, progress = '', retry_after = ?, "
        "submission_lock_state = 'locked', completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP), "
        "failure_type = NULL, auth_state = NULL, auth_scope = NULL, error_message = '' "
        "WHERE id = ?",
        (RETRY_AFTER_SENTINEL, job_id),
    )
    conn.commit()
    log_event(conn, job_id, "submission_lock_repaired", initiator=initiator)


def approve_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    initiator: str = "web",
    event_detail_json: dict | None = None,
    process_info: str | None = None,
) -> bool:
    """CAS transition: draft/stopped/submitted -> approved. Returns True if successful."""
    row = conn.execute("SELECT status, output_dir FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row or str(row["status"] or "") not in _APPROVABLE_DRAFT_STATUSES:
        return False
    if current_draft_pending_user_input(conn, job_id) is not None:
        return False
    try:
        enforce_submission_lock(conn, job_id, target_status="approved")
    except SubmissionLockError:
        _repair_locked_job_for_retry_refusal(conn, job_id, initiator=initiator)
        log_event(
            conn,
            job_id,
            "submission_lock_refused",
            detail="approved",
            detail_json=event_detail_json,
            initiator=initiator,
            process_info=process_info,
        )
        return False
    cur = conn.execute(
        "UPDATE jobs SET status = 'approved', error_message = '', progress = '', "
        "priority = MAX(priority, 100) "
        "WHERE id = ? AND status IN ('draft', 'stopped', 'submitted')",
        (job_id,),
    )
    if cur.rowcount > 0:
        submit_dir = _prepare_submit_dir_for_new_attempt(row["output_dir"])
        _clear_stale_submit_attempt_artifacts(submit_dir)
    conn.commit()
    if cur.rowcount > 0:
        log_event(
            conn,
            job_id,
            "status_change",
            detail="approved",
            detail_json=event_detail_json,
            initiator=initiator,
            process_info=process_info,
        )
        try:
            log_event(
                conn,
                job_id,
                "approved_for_submit",
                detail_json=event_detail_json,
                initiator=initiator,
                process_info=process_info,
            )
        except Exception:
            pass
        log.info("approve_job: job %d transitioned to approved", job_id)
    return cur.rowcount > 0


def regenerate_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    initiator: str = "web",
    event_detail_json: dict | None = None,
    process_info: str | None = None,
) -> bool:
    """Queue draft/stopped jobs for full regeneration by invalidating cached assets."""
    from job_db import get_job

    job = get_job(conn, job_id)
    output_dir = Path(job["output_dir"]) if job and job.get("output_dir") else None
    if job and job.get("output_dir"):
        clear_restart_pipeline_artifacts(output_dir, job.get("board"))
        log.info("regenerate_job: cleared stale current-attempt proof for job %d", job_id)
    try:
        enforce_submission_lock(conn, job_id, target_status="queued")
    except SubmissionLockError:
        _repair_locked_job_for_retry_refusal(conn, job_id, initiator=initiator)
        log_event(
            conn,
            job_id,
            "submission_lock_refused",
            detail="queued",
            detail_json=event_detail_json,
            initiator=initiator,
            process_info=process_info,
        )
        return False
    cur = conn.execute(
        "UPDATE jobs SET status = 'queued', provider = NULL, retry_after = ?, error_message = '', progress = '', "
        "priority = MAX(priority, 100) "
        "WHERE id = ? AND status IN ('draft', 'stopped')",
        (RETRY_AFTER_SENTINEL, job_id),
    )
    conn.commit()
    if cur.rowcount > 0 and output_dir is not None:
        mark_answer_refresh_pending(output_dir, request_kind="full_regenerate")
    if cur.rowcount > 0:
        log_event(
            conn,
            job_id,
            "status_change",
            detail="queued",
            detail_json=event_detail_json,
            initiator=initiator,
            process_info=process_info,
        )
    return cur.rowcount > 0


def reset_job_to_new(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    initiator: str = "web",
    event_detail_json: dict | None = None,
    process_info: str | None = None,
) -> bool:
    """Reset a job back to the newly-added queued state."""

    job = get_job(conn, job_id)
    if not job or bool(job.get("archived")):
        return False

    output_dir_raw = str(job.get("output_dir") or "").strip()
    output_dir = Path(output_dir_raw) if output_dir_raw else None
    board_name = job.get("board")
    try:
        enforce_submission_lock(conn, job_id, target_status="queued")
    except SubmissionLockError:
        _repair_locked_job_for_retry_refusal(conn, job_id, initiator=initiator)
        log_event(
            conn,
            job_id,
            "submission_lock_refused",
            detail="queued",
            detail_json=event_detail_json,
            initiator=initiator,
            process_info=process_info,
        )
        return False

    return _finalize_reset_job_to_new(
        conn,
        job_id,
        output_dir,
        board_name,
        initiator=initiator,
        event_detail_json=event_detail_json,
        process_info=process_info,
    )


# ── Autofill Report Parsing ──────────────────────────────────────────────────


def diff_draft_fields(original_fields: list[dict], edited_fields: list[dict]) -> list[dict]:
    """Return the field-level differences between original and edited drafts."""
    orig_map = {f["field_name"]: f.get("value", "") for f in original_fields}
    changes = []
    for field in edited_fields:
        name = field["field_name"]
        new_val = field.get("value", "")
        old_val = orig_map.get(name, "")
        if new_val != old_val:
            changes.append(
                {
                    "field_name": name,
                    "label": field.get("label", name),
                    "original": old_val,
                    "corrected": new_val,
                }
            )
    return changes


def detect_content_edits(original, current, filename: str) -> list[dict]:
    """Identify changed cover letter or resume content fields between original and current files."""
    changes = []

    if filename == "cover_letter_text.txt":
        if str(original) != str(current):
            changes.append(
                {
                    "field_name": "cover_letter_text",
                    "original": str(original)[:200],
                    "corrected": str(current)[:200],
                }
            )
        return changes

    # JSON diff for resume_content.json
    if not isinstance(original, dict) or not isinstance(current, dict):
        return changes

    for key in ("tagline", "summary", "page_break_before"):
        if original.get(key) != current.get(key):
            changes.append(
                {
                    "field_name": key,
                    "original": str(original.get(key, ""))[:200],
                    "corrected": str(current.get(key, ""))[:200],
                }
            )

    orig_positions = original.get("positions", {})
    curr_positions = current.get("positions", {})
    for company in set(list(orig_positions.keys()) + list(curr_positions.keys())):
        orig_bullets = orig_positions.get(company, [])
        curr_bullets = curr_positions.get(company, [])
        max_len = max(len(orig_bullets), len(curr_bullets))
        for i in range(max_len):
            orig_b = orig_bullets[i] if i < len(orig_bullets) else {}
            curr_b = curr_bullets[i] if i < len(curr_bullets) else {}
            if orig_b != curr_b:
                changes.append(
                    {
                        "field_name": f"positions.{company}[{i}]",
                        "original": str(orig_b)[:200],
                        "corrected": str(curr_b)[:200],
                    }
                )

    return changes


def _detect_and_log_content_edits(conn, job_id, output_dir):
    """Compare .original snapshots against current content files."""
    from job_db import ensure_job_metrics, get_job_metrics, log_field_correction, update_job_metrics

    content_dir = output_dir / "content"

    for filename, loader in [
        ("resume_content.json", lambda p: json.loads(p.read_text(encoding="utf-8"))),
        ("cover_letter_text.txt", lambda p: p.read_text(encoding="utf-8")),
    ]:
        original_path = content_dir / f"{filename}.original"
        current_path = content_dir / filename
        if not original_path.exists() or not current_path.exists():
            continue
        try:
            original = loader(original_path)
            current = loader(current_path)
            changes = detect_content_edits(original, current, filename)
            for change in changes:
                log_field_correction(
                    conn,
                    job_id,
                    change["field_name"],
                    change["original"],
                    change["corrected"],
                    "content_edit",
                )
            if changes:
                ensure_job_metrics(conn, job_id)
                m = get_job_metrics(conn, job_id)
                if m:
                    update_job_metrics(
                        conn,
                        job_id,
                        fields_corrected=m["fields_corrected"] + len(changes),
                        manual_interventions=m["manual_interventions"] + 1,
                    )
        except Exception:
            pass


def parse_autofill_report(report: dict) -> dict:
    """Summarize field counts and LLM-generated answers from an autofill report."""
    fields = report.get("fields", [])
    total = len(fields)
    filled = sum(1 for f in fields if f.get("status") == "filled")
    skipped = sum(1 for f in fields if f.get("status") == "skipped_not_found")
    unknown = len(report.get("unknown_questions", []))
    # Count LLM-generated answers (questions answered by AI, not from profile data)
    llm_generated = [f for f in fields if f.get("source") == "generated_application_answer"]
    return {
        "total_fields": total,
        "filled_fields": filled,
        "skipped_fields": skipped,
        "unknown_questions": unknown,
        "llm_generated_count": len(llm_generated),
        "llm_generated_labels": [f.get("label", f.get("field_name", "?")) for f in llm_generated],
    }


def _load_first_autofill_report(output_dir: Path) -> tuple[dict | None, dict | None]:
    for report_file in output_dir.rglob("*_autofill_report.json"):
        try:
            report_data = json.loads(report_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        return report_data, parse_autofill_report(report_data)
    return None, None


def _fail_pending_answer_refresh_for_output(
    conn: sqlite3.Connection,
    job_id: int,
    output_dir: str | Path | None,
    *,
    reason: str,
    message: str,
    initiator: str = "worker",
) -> dict | None:
    if not output_dir:
        return None
    out_path = Path(output_dir)
    current = load_answer_refresh_state(out_path)
    if current.get("status") != STATUS_PENDING or not current.get("request_id"):
        return current
    final = fail_pending_answer_refresh(out_path, reason=reason, message=message)
    if final.get("status") == STATUS_FAILED:
        log_event(conn, job_id, "answer_refresh_failed", detail=message, initiator=initiator)
    return final


def _finalize_pending_answer_refresh(
    conn: sqlite3.Connection,
    job_id: int,
    output_dir: str | Path | None,
    *,
    initiator: str = "worker",
) -> dict | None:
    if not output_dir:
        return None
    out_path = Path(output_dir)
    current = load_answer_refresh_state(out_path)
    request_id = current.get("request_id")
    if current.get("status") != STATUS_PENDING or not request_id:
        return None

    _report_data, field_counts = _load_first_autofill_report(out_path)
    if field_counts is None:
        return _fail_pending_answer_refresh_for_output(
            conn,
            job_id,
            out_path,
            reason="missing_autofill_report",
            message="Answer regeneration finished without a readable autofill report.",
            initiator=initiator,
        )

    proof = load_answer_refresh_artifact_proof(out_path)
    proof_submit_dir = proof.get("submit_dir") if proof else None
    if field_counts["llm_generated_count"] == 0:
        final = finalize_answer_refresh(
            out_path,
            request_id=request_id,
            status=STATUS_NOT_APPLICABLE,
            reason="no_generated_answers",
            message="No generated application answers were present for this draft.",
            generated_answer_count=0,
            proof_submit_dir=proof_submit_dir,
        )
        log_event(
            conn,
            job_id,
            "answer_refresh_not_applicable",
            detail="No generated application answers were present for this draft.",
            initiator=initiator,
        )
        return final

    if proof and proof.get("request_id") == request_id and proof.get("provider") and proof.get("generated_at_utc"):
        final = finalize_answer_refresh(
            out_path,
            request_id=request_id,
            status=STATUS_FRESH,
            reason="fresh_proof_recorded",
            message="Fresh answer generation proof recorded.",
            answer_provider=proof.get("provider"),
            answer_generated_at_utc=proof.get("generated_at_utc"),
            generated_answer_count=field_counts["llm_generated_count"],
            proof_submit_dir=proof_submit_dir,
        )
        log_event(
            conn,
            job_id,
            "answer_refresh_fresh",
            detail_json={
                "provider": proof.get("provider"),
                "generated_at_utc": proof.get("generated_at_utc"),
                "generated_answer_count": field_counts["llm_generated_count"],
            },
            initiator=initiator,
        )
        return final

    return _fail_pending_answer_refresh_for_output(
        conn,
        job_id,
        out_path,
        reason="missing_fresh_proof",
        message="Answer regeneration did not rewrite fresh answer artifacts for the current request.",
        initiator=initiator,
    )


# ── Process Job — Full Lifecycle ─────────────────────────────────────────────


def _get_provider_chain() -> list[str]:
    """Read provider chain from environment."""
    return list(automation_provider_chain())


def _uv_python_cmd(*script_and_args: str) -> list[str]:
    """Build a ``uv run python <script> ...`` command list."""
    if shutil.which("uv"):
        return ["uv", "run", "--project", str(PROJECT_ROOT), "python", *script_and_args]
    return [sys.executable, *script_and_args]


def _run_worker_subprocess(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a worker-launched child process without inheriting ambient stdin."""
    run_kwargs = prepare_worker_subprocess_kwargs(kwargs)
    timeout = run_kwargs.pop("timeout", None)
    if timeout is None:
        return subprocess.run(cmd, **run_kwargs)

    capture_output = bool(run_kwargs.pop("capture_output", False))
    if capture_output:
        run_kwargs.setdefault("stdout", subprocess.PIPE)
        run_kwargs.setdefault("stderr", subprocess.PIPE)
    run_kwargs.setdefault("start_new_session", True)
    process = subprocess.Popen(cmd, **run_kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_worker_subprocess_group(process)
        raise exc
    return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)


def _terminate_worker_subprocess_group(process: subprocess.Popen, *, grace_seconds: float = 5.0) -> None:
    """Terminate a timed-out worker child and its descendants."""
    try:
        pgid = os.getpgid(process.pid)
    except OSError:
        pgid = None

    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=grace_seconds)
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                return
            process.wait(timeout=grace_seconds)
            return

    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            return
        process.wait(timeout=grace_seconds)


def _run_phases_1_2(
    conn,
    job_id,
    job,
    url,
    *,
    headless=True,
    auto_submit=False,
) -> tuple[str | None, str | None, str]:
    """Run URL resolution (phase 1) and asset generation (phase 2).

    Returns (board_url, output_dir, source) on success.
    Returns (None, error_status, source) on failure — caller should return error_status.
    """
    from job_db import (
        _initial_canonical_job_url,
        end_phase,
        find_duplicate_job_match,
        log_event,
        log_provider_run,
        set_jd_fingerprint,
        start_phase,
        update_job_metrics,
        update_progress,
        update_status,
    )

    _phase_count = 0

    def _label_duplicate(match_type: str, duplicate_job: dict) -> tuple[str, str]:
        duplicate_label = (
            f"{duplicate_job.get('company') or '?'} — {duplicate_job.get('role_title') or '?'} "
            f"(job #{duplicate_job['id']})"
        )
        if match_type == "url":
            prefix = "Duplicate job URL"
        elif match_type == "jd":
            prefix = "Duplicate job description"
        else:
            prefix = "Duplicate job"
        return prefix, duplicate_label

    def _archive_duplicate(
        *,
        match_type: str,
        duplicate_job: dict,
        board_url_value: str,
        board_value: str | None = None,
        company_value: str | None = None,
        role_title_value: str | None = None,
        output_dir_value: Path | None = None,
    ) -> tuple[None, str, str]:
        prefix, duplicate_label = _label_duplicate(match_type, duplicate_job)
        detail_json = {
            "match_type": match_type,
            "duplicate_job_id": int(duplicate_job["id"]),
            "duplicate_label": duplicate_label,
            "duplicate_status": duplicate_job.get("status"),
            "duplicate_archived": bool(duplicate_job.get("archived")),
        }
        event_name = "jd_duplicate_detected" if match_type == "jd" else "duplicate_detected"
        log.warning("Job %d is a %s duplicate of %s", job_id, match_type, duplicate_label)
        log_event(conn, job_id, event_name, detail_json=detail_json, initiator="worker")
        update_status(
            conn,
            job_id,
            "stopped",
            error_message=f"{prefix} — matches job #{duplicate_job['id']} ({duplicate_label})",
            failure_type="duplicate",
            archived=True,
            board=board_value,
            board_url=board_url_value,
            company=company_value,
            role_title=role_title_value,
            output_dir=str(output_dir_value) if output_dir_value else None,
        )
        return None, "stopped", source

    # ── Phase 1: URL Resolution ──────────────────────────────────────────
    _p = start_phase(conn, job_id, "url_resolution")
    update_status(conn, job_id, "resolving", error_message="", progress="Resolving URL...")
    try:
        from url_resolver import detect_source, resolve_to_board_url

        if job.get("board_url"):
            board_url = job["board_url"]
            try:
                source = detect_source(url)
            except Exception:
                source = job.get("source") or "direct"
        else:
            source = detect_source(url)
            if source == "direct" or source == "unknown":
                board_url = url
            else:
                board_url = resolve_to_board_url(url)
                if board_url is None:
                    update_status(
                        conn,
                        job_id,
                        "needs_board_url",
                        error_message=f"Could not resolve aggregator URL to board URL: {url}",
                    )
                    log_event(
                        conn, job_id, "resolution_failed", detail=f"source={source}, url={url}", initiator="worker"
                    )
                    end_phase(conn, _p, exit_code=1)
                    _phase_count += 1
                    update_job_metrics(conn, job_id, phase_count=_phase_count)
                    return None, "needs_board_url", source

        detected_board = _detect_board_from_url(board_url)
        resolved_canonical_url = _initial_canonical_job_url(board_url)
        update_status(
            conn,
            job_id,
            "resolving",
            board_url=board_url,
            board=detected_board if detected_board != "unknown" else None,
        )
        duplicate_match = find_duplicate_job_match(
            conn,
            url=board_url,
            source_url=url,
            board_url=board_url,
            canonical_url=resolved_canonical_url,
            exclude_job_id=job_id,
            older_than_job_id=job_id,
        )
        if duplicate_match:
            match_type, duplicate_job = duplicate_match
            end_phase(conn, _p, exit_code=1)
            _phase_count += 1
            update_job_metrics(conn, job_id, phase_count=_phase_count)
            return _archive_duplicate(
                match_type=match_type,
                duplicate_job=duplicate_job,
                board_url_value=board_url,
                board_value=detected_board if detected_board != "unknown" else None,
            )
        log_event(
            conn,
            job_id,
            "url_resolved",
            detail_json={"source": source, "board_url": board_url, "board": detected_board},
            initiator="worker",
        )
        end_phase(conn, _p, exit_code=0)
        _phase_count += 1
    except Exception as exc:
        log.exception("Phase 1 failed for job %d", job_id)
        update_status(
            conn, job_id, "stopped", error_message=f"URL resolution error: {exc}", failure_type="resolution_failed"
        )
        end_phase(conn, _p, exit_code=1)
        _phase_count += 1
        update_job_metrics(conn, job_id, phase_count=_phase_count)
        return None, "stopped", "unknown"

    # ── Phase 2: Asset Generation ────────────────────────────────────────
    _p = start_phase(conn, job_id, "asset_generation")
    update_status(conn, job_id, "generating", progress="Starting asset generation...")
    try:
        apply_script = str(SCRIPT_DIR.parent / "apply.sh")
        base_cmd = ["bash", apply_script, board_url]

        providers = _get_provider_chain()
        if job.get("provider"):
            providers = [job["provider"]]

        def _update_gen_progress(msg):
            update_progress(conn, job_id, msg)

        asset_timeout = _escalated_timeout(DEFAULT_ASSET_TIMEOUT, job.get("failure_type"))
        if asset_timeout != DEFAULT_ASSET_TIMEOUT:
            log.info(
                "asset-generation timeout escalated for job %d: %ss -> %ss",
                job_id,
                DEFAULT_ASSET_TIMEOUT,
                asset_timeout,
            )
        t0 = time.monotonic()
        winning_provider, gen_exit_code = provider_fallback(
            base_cmd,
            providers,
            timeout=asset_timeout,
            progress_callback=_update_gen_progress,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)

        if winning_provider:
            log_provider_run(
                conn,
                job_id,
                provider=winning_provider,
                phase="asset_generation",
                exit_code=gen_exit_code,
                duration_ms=duration_ms,
            )
            update_status(conn, job_id, "generating", provider=winning_provider)
            log_event(conn, job_id, "assets_generated", detail=f"provider={winning_provider}", initiator="worker")
        else:
            error_hint = getattr(provider_fallback, "last_error_hint", "") or f"exit {gen_exit_code}"
            terminal_hint = _extract_terminal_generation_hint(error_hint)
            if terminal_hint:
                error_hint = terminal_hint
            # Detect suspiciously fast pipeline completions (likely no JD extracted)
            if duration_ms < 10_000 and not error_hint.strip():
                error_hint = f"Pipeline completed in {duration_ms}ms — likely no JD content extracted"
            log_event(
                conn,
                job_id,
                "asset_generation_failed",
                detail=f"all providers failed ({duration_ms}ms): {error_hint}",
                initiator="worker",
            )
            lower_hint = error_hint.casefold()
            if lower_hint.startswith("job_closed:"):
                end_phase(conn, _p, exit_code=1)
                _phase_count += 1
                update_job_metrics(conn, job_id, phase_count=_phase_count)
                _mark_job_unavailable_and_archive(
                    conn,
                    job_id,
                    output_dir=None,
                    error_message=error_hint,
                )
                return None, "stopped", source
            if lower_hint.startswith("needs_board_url:"):
                update_status(
                    conn,
                    job_id,
                    "needs_board_url",
                    error_message=f"Asset generation failed: {error_hint[:500]}",
                )
                end_phase(conn, _p, exit_code=1)
                _phase_count += 1
                update_job_metrics(conn, job_id, phase_count=_phase_count)
                return None, "needs_board_url", source
            if lower_hint.startswith("unsupported:"):
                update_status(
                    conn,
                    job_id,
                    "stopped",
                    error_message=f"Asset generation failed: {error_hint[:500]}",
                    failure_type="unsupported",
                )
                end_phase(conn, _p, exit_code=1)
                _phase_count += 1
                update_job_metrics(conn, job_id, phase_count=_phase_count)
                return None, "stopped", source
            if lower_hint.startswith("skipped_captcha:"):
                update_status(
                    conn,
                    job_id,
                    "stopped",
                    error_message=f"Asset generation failed: {error_hint[:500]}",
                    failure_type="skipped_captcha",
                )
                end_phase(conn, _p, exit_code=1)
                _phase_count += 1
                update_job_metrics(conn, job_id, phase_count=_phase_count)
                return None, "stopped", source
            update_status(
                conn,
                job_id,
                "stopped",
                error_message=f"Asset generation failed: {error_hint[:500]}",
                failure_type=(
                    "llm_rate_limited"
                    if getattr(provider_fallback, "last_failure_type", "") == "llm_rate_limited"
                    else "board_rate_limited"
                    if _is_board_rate_limited_generation_error(error_hint)
                    else "generation_failed"
                ),
            )
            end_phase(conn, _p, exit_code=1)
            _phase_count += 1
            update_job_metrics(conn, job_id, phase_count=_phase_count)
            return None, "stopped", source

        output_dir = _discover_output_dir(board_url)
        if output_dir:
            update_status(conn, job_id, "generating", output_dir=str(output_dir))
            meta = _load_pipeline_meta(output_dir)
            if meta:
                company_value = meta.get("company_proper") or meta.get("company")
                role_title_value = meta.get("role")
                board_value = meta.get("board") or _detect_board_from_url(board_url)
                update_status(
                    conn,
                    job_id,
                    "generating",
                    company=company_value,
                    role_title=role_title_value,
                    board=board_value,
                )
                _enrich_pipeline_meta(output_dir, meta, board_url=board_url, source_url=url, source=source)
                # ── Metadata/JD duplicate check ───────────────────────────
                _company = company_value or ""
                if _company:
                    _jd_raw_path = output_dir / "content" / "jd_raw.md"
                    if not _jd_raw_path.is_file():
                        _jd_raw_path = output_dir / "jd_raw.md"
                    _jd_text = _jd_raw_path.read_text(encoding="utf-8") if _jd_raw_path.is_file() else None
                    set_jd_fingerprint(conn, job_id, _company, _jd_text)
                    duplicate_match = find_duplicate_job_match(
                        conn,
                        url=url,
                        source_url=url,
                        board_url=board_url,
                        canonical_url=_initial_canonical_job_url(board_url),
                        company=_company,
                        role_title=role_title_value,
                        jd_text=_jd_text,
                        exclude_job_id=job_id,
                        older_than_job_id=job_id,
                    )
                    if duplicate_match:
                        match_type, duplicate_job = duplicate_match
                        end_phase(conn, _p, exit_code=1)
                        _phase_count += 1
                        update_job_metrics(conn, job_id, phase_count=_phase_count)
                        return _archive_duplicate(
                            match_type=match_type,
                            duplicate_job=duplicate_job,
                            board_url_value=board_url,
                            board_value=board_value,
                            company_value=company_value,
                            role_title_value=role_title_value,
                            output_dir_value=output_dir,
                        )
        end_phase(conn, _p, exit_code=0)
        _phase_count += 1
    except Exception as exc:
        log.exception("Phase 2 failed for job %d", job_id)
        update_status(
            conn, job_id, "stopped", error_message=f"Asset generation error: {exc}", failure_type="generation_failed"
        )
        end_phase(conn, _p, exit_code=1)
        _phase_count += 1
        update_job_metrics(conn, job_id, phase_count=_phase_count)
        return None, "stopped", source

    if auto_submit:
        update_progress(conn, job_id, "Assets generated, preparing submission...")
    else:
        update_progress(conn, job_id, "Assets generated, preparing draft...")
    return board_url, str(output_dir) if output_dir else None, source


def _is_transient_error(error_message: str | None) -> bool:
    """Check if an error message indicates a transient failure worth retrying."""
    if not error_message:
        return False
    lower = error_message.lower()
    return any(pat in lower for pat in _TRANSIENT_PATTERNS)


def _is_rate_limit_error(error_message: str | None) -> bool:
    """Check if an error is specifically a rate-limit failure."""
    if not error_message:
        return False
    lower = error_message.lower()
    return any(pat in lower for pat in _BOARD_RATE_LIMIT_PATTERNS)


def _is_board_rate_limited_generation_error(error_message: str | None) -> bool:
    """Detect transient board throttling during JD extraction without catching weak extracts."""
    if not error_message:
        return False
    if _is_rate_limit_error(error_message):
        return True

    lower = error_message.lower()
    if "please retry later or provide jd text directly." not in lower:
        return False
    if any(pattern in lower for pattern in _PROVIDER_INDEPENDENT_FAILURE_PATTERNS):
        return False
    return "failed with exit code" in lower


def _retry_classification(error_message: str | None, failure_type: str | None) -> tuple[str, int]:
    """Classify a failure into permanent, transient, or unknown retry behavior."""
    if failure_type in _NON_TRANSIENT_TYPES:
        return "permanent", 0
    if failure_type == "service_unavailable":
        return "transient", MAX_AUTO_RETRIES
    if _is_transient_error(error_message):
        return "transient", MAX_AUTO_RETRIES
    return "unknown", _UNKNOWN_AUTO_RETRIES


def _schedule_llm_rate_limit_retry(
    conn: sqlite3.Connection,
    job_id: int,
    error_message: str,
) -> str:
    return _schedule_rate_limit_retry(
        conn,
        job_id,
        error_message,
        event_type="llm_rate_limit_retry",
        progress_prefix="LLM rate-limit retry",
        log_label="llm-rate-limit",
    )


def _schedule_board_rate_limit_retry(
    conn: sqlite3.Connection,
    job_id: int,
    error_message: str,
) -> str:
    return _schedule_rate_limit_retry(
        conn,
        job_id,
        error_message,
        event_type="board_rate_limit_retry",
        progress_prefix="Board rate-limit retry",
        log_label="board-rate-limit",
    )


def _schedule_rate_limit_retry(
    conn: sqlite3.Connection,
    job_id: int,
    error_message: str,
    *,
    event_type: str,
    progress_prefix: str,
    log_label: str,
) -> str:
    from job_db import get_job, log_event

    job = get_job(conn, job_id)
    if job is None:
        return "stopped"

    row = conn.execute(
        "SELECT COUNT(*) AS count FROM events WHERE job_id = ? AND event_type = ?",
        (job_id, event_type),
    ).fetchone()
    retry_index = int((row["count"] if row else 0) or 0)
    retry_num = retry_index + 1
    hinted_delay_seconds = _retry_after_seconds_from_message(error_message)
    base_delay_seconds = _RETRY_DELAYS[min(retry_index, len(_RETRY_DELAYS) - 1)]
    if hinted_delay_seconds is None:
        delay = base_delay_seconds + random.uniform(0, 0.25 * base_delay_seconds)
        delay_seconds = max(1, int(delay))
    else:
        delay_seconds = max(base_delay_seconds, hinted_delay_seconds)

    try:
        enforce_submission_lock(conn, job_id, target_status="queued")
    except SubmissionLockError:
        _repair_locked_job_for_retry_refusal(conn, job_id, initiator="worker")
        log_event(conn, job_id, "submission_lock_refused", detail="queued", initiator="worker")
        return "submitted"
    cur = conn.execute(
        "UPDATE jobs SET status = 'queued', provider = NULL, retry_after = datetime('now', ?), "
        "progress = ?, error_message = '' "
        "WHERE id = ? AND status IN ('stopped', 'submitting', 'retrying', 'fix_in_progress')",
        (
            f"+{delay_seconds} seconds",
            f"{progress_prefix} {retry_num} in {delay_seconds}s...",
            job_id,
        ),
    )
    conn.commit()
    if cur.rowcount == 0:
        current = get_job(conn, job_id)
        log.warning("job %d: %s retry CAS skipped due to concurrent status change", job_id, log_label)
        return current.get("status", "stopped") if current else "stopped"
    log_event(
        conn,
        job_id,
        event_type,
        detail=f"{progress_prefix} {retry_num} after {delay_seconds}s: {error_message[:200]}",
        initiator="worker",
    )
    log.info(
        "job %d: queued %s retry %d after %ds (%s)",
        job_id,
        log_label,
        retry_num,
        delay_seconds,
        error_message[:80],
    )
    return "queued"


def _workday_auth_scope_for_job(job: dict, board_url: str) -> str | None:
    return _auth_scope_for_job(job, board_url, "workday")


def _auth_scope_for_job(job: dict, board_url: str, board: str | None) -> str | None:
    stored_scope = str(job.get("auth_scope") or "").strip()
    if stored_scope:
        return stored_scope
    resolver = {
        "icims": icims_auth_scope,
        "workday": workday_auth_scope,
    }.get(str(board or "").casefold())
    if resolver is None:
        return None
    for candidate in (board_url, job.get("board_url"), job.get("url")):
        scope = resolver(candidate or "")
        if scope:
            return scope
    return None


def _load_workday_auth_result(submit_dir: Path) -> dict | None:
    path = submit_dir / "workday_auth_failure.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _active_submit_dir_for_output(output_dir: str | Path | None) -> Path | None:
    if not output_dir:
        return None
    from output_layout import active_submit_dir_name as _asd

    return Path(output_dir) / _asd(output_dir)


def _prepare_submit_dir_for_new_attempt(output_dir: str | Path | None) -> Path | None:
    if not output_dir:
        return None
    from output_layout import set_active_submit_dir

    set_active_submit_dir(output_dir, "submit")
    return _active_submit_dir_for_output(output_dir)


def _clear_stale_submit_attempt_artifacts(submit_dir: Path | None) -> None:
    if submit_dir is None:
        return
    for stale_name in (
        "unsupported_board.json",
        "application_submission_result.json",
        "application_confirmation_website.json",
        "workday_auth_failure.json",
        "icims_auth_failure.json",
    ):
        stale_path = submit_dir / stale_name
        if stale_path.exists():
            stale_path.unlink()
    for auth_failure_path in submit_dir.glob("*_auth_failure.json"):
        if auth_failure_path.exists():
            auth_failure_path.unlink()


def _load_application_submission_result(submit_dir: Path | None) -> dict | None:
    if submit_dir is None:
        return None
    path = submit_dir / "application_submission_result.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_application_submission_result(submit_dir: Path, result: dict) -> None:
    path = submit_dir / "application_submission_result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def _load_failed_submission_result(submit_dir: Path | None) -> dict | None:
    data = _load_application_submission_result(submit_dir)
    if not data:
        return None
    if str(data.get("status") or "").strip().casefold() != "failed":
        return None
    failure_type = str(data.get("failure_type") or "").strip()
    if not failure_type:
        return None
    return data


def _load_linkedin_failure_result(submit_dir: Path | None) -> dict | None:
    data = _load_failed_submission_result(submit_dir)
    if not data:
        return None
    if str(data.get("board") or "").strip().casefold() != "linkedin":
        return None
    failure_type = str(data.get("failure_type") or "").strip()
    if not failure_type.startswith("linkedin_"):
        return None
    return data


def _synthesize_linkedin_timeout_result(submit_dir: Path | None) -> dict | None:
    if submit_dir is None or not submit_dir.exists():
        return None

    debug_screenshot = submit_dir / "linkedin_submit_debug.png"
    page_screens_dir = submit_dir / "linkedin_autofill_pages"
    page_screens = sorted(page_screens_dir.glob("*.png")) if page_screens_dir.exists() else []
    if not debug_screenshot.exists() and not page_screens:
        return None

    artifacts: dict[str, str] = {}
    if debug_screenshot.exists():
        artifacts["submit_debug_screenshot"] = str(debug_screenshot)
    if page_screens:
        artifacts["step_screenshot"] = str(page_screens[-1])

    result = {
        "status": "failed",
        "board": "linkedin",
        "failure_type": "linkedin_timeout_after_partial_fill",
        "message": "LinkedIn Easy Apply timed out after partial progress before fresh proof was recorded.",
        "retry_class": "targeted_retry",
        "updated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }
    if artifacts:
        result["artifacts"] = artifacts
    return result


def _handle_linkedin_failure_result(
    conn: sqlite3.Connection,
    job_id: int,
    output_dir: str | Path | None,
    failure_result: dict,
) -> str:
    from job_db import get_job, update_status

    job = get_job(conn, job_id)
    if job is None:
        return "stopped"

    failure_type = str(failure_result.get("failure_type") or "submit_failed").strip()
    message = str(failure_result.get("message") or "LinkedIn Easy Apply failed.").strip()
    retry_class = str(failure_result.get("retry_class") or "none").strip()
    fix_attempts = job.get("fix_attempts", 0) or 0
    job_url = str(job.get("board_url") or job.get("url") or "").strip()

    if retry_class == "targeted_retry" and failure_type in _LINKEDIN_TARGETED_RETRY_FAILURE_TYPES and fix_attempts < 1:
        retry_num = fix_attempts + 1
        delay = _RETRY_DELAYS[min(fix_attempts, len(_RETRY_DELAYS) - 1)]
        delay += random.uniform(0, 0.25 * delay)
        delay_seconds = max(1, int(delay))
        try:
            enforce_submission_lock(conn, job_id, target_status="queued")
        except SubmissionLockError:
            _repair_locked_job_for_retry_refusal(conn, job_id, initiator="worker")
            log_event(conn, job_id, "submission_lock_refused", detail="queued", initiator="worker")
            return "submitted"
        cur = conn.execute(
            "UPDATE jobs SET status = 'queued', provider = NULL, retry_after = datetime('now', ?), "
            "fix_attempts = ?, progress = ?, error_message = '' "
            "WHERE id = ? AND status IN ('stopped', 'submitting', 'retrying', 'fix_in_progress')",
            (
                f"+{delay_seconds} seconds",
                retry_num,
                f"LinkedIn targeted retry {retry_num}/1 in {delay_seconds}s...",
                job_id,
            ),
        )
        conn.commit()
        if cur.rowcount == 0:
            current = get_job(conn, job_id)
            log.warning("job %d: LinkedIn targeted retry CAS skipped due to concurrent status change", job_id)
            return current.get("status", "stopped") if current else "stopped"
        log_event(
            conn,
            job_id,
            "linkedin_targeted_retry",
            detail=message[:300],
            detail_json=failure_result,
            initiator="worker",
        )
        return "queued"

    decision = audit_stopped_outcome(failure_type=failure_type, error_message=message, job_url=job_url)
    if decision.kind == "repairable":
        _record_repair_cluster(
            conn,
            job_id,
            board="linkedin",
            phase="stopped_audit",
            failure_type=failure_type,
            summary=decision.reason,
            output_dir=output_dir,
            artifacts=failure_result.get("artifacts") if isinstance(failure_result.get("artifacts"), dict) else None,
        )
        if output_dir:
            _fail_pending_answer_refresh_for_output(
                conn,
                job_id,
                output_dir,
                reason=failure_type,
                message=message,
            )
        return _schedule_audit_retry(
            conn,
            job_id,
            decision.reason,
            initiator="worker",
            output_dir=output_dir,
            repair_actions=decision.repair_actions,
        )

    if output_dir:
        _fail_pending_answer_refresh_for_output(
            conn,
            job_id,
            output_dir,
            reason=failure_type,
            message=message,
        )
    update_status(conn, job_id, "stopped", error_message=message, failure_type=failure_type)
    log_event(
        conn,
        job_id,
        "linkedin_failure",
        detail=message[:300],
        detail_json=failure_result,
        initiator="worker",
    )
    return "stopped"


def _handle_failed_submission_result(
    conn: sqlite3.Connection,
    job_id: int,
    output_dir: str | Path | None,
    failure_result: dict,
) -> str:
    board = str(failure_result.get("board") or "").strip().casefold()
    if board == "linkedin":
        return _handle_linkedin_failure_result(conn, job_id, output_dir, failure_result)

    from job_db import update_status

    job = get_job(conn, job_id) or {}
    failure_type = str(failure_result.get("failure_type") or "submit_failed").strip()
    message = str(failure_result.get("message") or "Submission failed.").strip()
    job_url = str(job.get("board_url") or job.get("url") or "").strip()
    decision = audit_stopped_outcome(failure_type=failure_type, error_message=message, job_url=job_url)
    if decision.kind == "repairable":
        _record_repair_cluster(
            conn,
            job_id,
            board=board or "unknown",
            phase="stopped_audit",
            failure_type=failure_type,
            summary=decision.reason,
            output_dir=output_dir,
            artifacts=failure_result.get("artifacts") if isinstance(failure_result.get("artifacts"), dict) else None,
        )
        if output_dir:
            _fail_pending_answer_refresh_for_output(
                conn,
                job_id,
                output_dir,
                reason=failure_type,
                message=message,
            )
        return _schedule_audit_retry(
            conn,
            job_id,
            decision.reason,
            initiator="worker",
            output_dir=output_dir,
            repair_actions=decision.repair_actions,
        )
    if output_dir:
        _fail_pending_answer_refresh_for_output(
            conn,
            job_id,
            output_dir,
            reason=failure_type,
            message=message,
        )
    update_status(conn, job_id, "stopped", error_message=message, failure_type=failure_type)
    log_event(
        conn,
        job_id,
        "submission_failure",
        detail=message[:300],
        detail_json=failure_result,
        initiator="worker",
    )
    return "stopped"


def _auto_retry_if_transient(
    conn: sqlite3.Connection,
    job_id: int,
    error_message: str,
    *,
    failure_type: str | None = None,
) -> str:
    """Check if a failed job should be auto-retried. Returns final status.

    If the error is transient and retry count < MAX_AUTO_RETRIES, requeue
    the job with a backoff delay note. Otherwise mark as needs_attention
    with a clear message for the user.
    """
    from job_db import get_job, log_event, update_status

    job = get_job(conn, job_id)
    if job is None:
        return "stopped"

    if failure_type == "llm_rate_limited":
        return _schedule_llm_rate_limit_retry(conn, job_id, error_message)
    if failure_type == "board_rate_limited":
        return _schedule_board_rate_limit_retry(conn, job_id, error_message)

    fix_attempts = job.get("fix_attempts", 0) or 0
    retry_kind, retry_limit = _retry_classification(error_message, failure_type)

    if retry_kind == "permanent":
        log.info("job %d: not auto-retrying permanent failure_type=%s", job_id, failure_type)
        return "stopped"

    if fix_attempts < retry_limit:
        # Auto-retry: requeue with incremented retry counter
        retry_num = fix_attempts + 1
        delay = _RETRY_DELAYS[min(fix_attempts, len(_RETRY_DELAYS) - 1)]
        delay += random.uniform(0, 0.25 * delay)
        delay_seconds = max(1, int(delay))
        try:
            enforce_submission_lock(conn, job_id, target_status="queued")
        except SubmissionLockError:
            _repair_locked_job_for_retry_refusal(conn, job_id, initiator="worker")
            log_event(conn, job_id, "submission_lock_refused", detail="queued", initiator="worker")
            return "submitted"
        cur = conn.execute(
            "UPDATE jobs SET status = 'queued', provider = NULL, retry_after = datetime('now', ?), "
            "fix_attempts = ?, progress = ?, error_message = '' "
            "WHERE id = ? AND status IN ('stopped', 'submitting', 'retrying', 'fix_in_progress')",
            (
                f"+{delay_seconds} seconds",
                retry_num,
                f"Auto-retry {retry_num}/{retry_limit} in {delay_seconds}s...",
                job_id,
            ),
        )
        conn.commit()
        if cur.rowcount == 0:
            current = get_job(conn, job_id)
            log.warning("job %d: auto-retry CAS skipped due to concurrent status change", job_id)
            return current.get("status", "stopped") if current else "stopped"
        log_event(
            conn,
            job_id,
            "auto_retry",
            detail=f"{retry_kind} retry {retry_num}/{retry_limit} after {delay_seconds}s: {error_message[:200]}",
            initiator="worker",
        )
        log.info(
            "job %d: auto-retry %d/%d after %ds (%s: %s)",
            job_id,
            retry_num,
            retry_limit,
            delay_seconds,
            retry_kind,
            error_message[:80],
        )
        return "queued"

    if fix_attempts >= retry_limit:
        # Exhausted retries — flag for user attention
        suggestion = _suggest_resolution(error_message)
        msg = f"Failed after {fix_attempts} retries: {error_message}. Suggestion: {suggestion}"
        update_status(conn, job_id, "stopped", error_message=msg, failure_type="retries_exhausted")
        log_event(
            conn, job_id, "retries_exhausted", detail=f"After {fix_attempts} retries: {suggestion}", initiator="worker"
        )
        return "stopped"

    # Non-transient failure — keep as stopped with descriptive message
    return "stopped"


def _schedule_audit_retry(
    conn: sqlite3.Connection,
    job_id: int,
    error_message: str,
    *,
    initiator: str,
    output_dir: str | Path | None = None,
    repair_actions: tuple[str, ...] = (),
) -> str:
    from job_db import ensure_job_metrics, get_job_metrics, update_job_metrics, update_status

    job = get_job(conn, job_id)
    if job is None:
        return "stopped"

    ensure_job_metrics(conn, job_id)
    metrics = get_job_metrics(conn, job_id) or {}
    audit_attempts = int(metrics.get("audit_attempts", 0) or 0)

    if audit_attempts >= _AUDIT_RETRY_LIMIT:
        update_status(conn, job_id, "stopped", error_message=error_message[:500], failure_type="audit_failure")
        update_job_metrics(
            conn,
            job_id,
            audit_failure_count=int(metrics.get("audit_failure_count", 0) or 0) + 1,
        )
        if output_dir:
            attempt_rows = conn.execute(
                "SELECT detail FROM events WHERE job_id = ? AND event_type = 'audit_retry_scheduled' "
                "ORDER BY id DESC LIMIT ?",
                (job_id, _AUDIT_RETRY_LIMIT),
            ).fetchall()
            attempts = [str(row["detail"] or "").strip() for row in reversed(list(attempt_rows)) if row["detail"]]
            write_audit_failure_report(
                output_dir=output_dir,
                job_id=job_id,
                summary=error_message[:500],
                suggestions=[_suggest_resolution(error_message)],
                attempts=attempts,
            )
        log_event(conn, job_id, "audit_retry_exhausted", detail=error_message[:300], initiator=initiator)
        return "stopped"

    if output_dir:
        out_path = Path(output_dir)
        if "clear_current_attempt_artifacts" in repair_actions:
            from autofill_common import clear_current_attempt_artifacts
            from submit_review_common import resolve_current_submit_artifacts

            resolved = resolve_current_submit_artifacts(out_path)
            artifacts = {}
            for artifact_key, payload_key in (
                ("report_json", "report_json"),
                ("report_md", "report_markdown"),
                ("pre_submit_screenshot", "pre_submit_screenshot"),
                ("post_submit_screenshot", "post_submit_screenshot"),
                ("submit_debug_screenshot", "submit_debug_screenshot"),
                ("payload_json", "payload_json"),
            ):
                path = resolved.get(artifact_key)
                if path:
                    artifacts[payload_key] = str(path)
            clear_current_attempt_artifacts({"artifacts": artifacts, "out_dir": str(out_path)})
        if "clear_answer_cache" in repair_actions:
            _clear_cached_answers(out_path)
        clear_audit_failure_report(out_path)

    retry_num = audit_attempts + 1
    delay = _RETRY_DELAYS[min(audit_attempts, len(_RETRY_DELAYS) - 1)]
    delay += random.uniform(0, 0.25 * delay)
    delay_seconds = max(1, int(delay))
    try:
        enforce_submission_lock(conn, job_id, target_status="queued")
    except SubmissionLockError:
        _repair_locked_job_for_retry_refusal(conn, job_id, initiator=initiator)
        log_event(conn, job_id, "submission_lock_refused", detail="queued", initiator=initiator)
        return "submitted"
    cur = conn.execute(
        "UPDATE jobs SET status = 'queued', provider = NULL, retry_after = datetime('now', ?), "
        "progress = ?, error_message = '' WHERE id = ? "
        "AND status IN ('stopped', 'draft', 'submitting', 'retrying', 'fix_in_progress')",
        (
            f"+{delay_seconds} seconds",
            f"Audit retry {retry_num}/{_AUDIT_RETRY_LIMIT} in {delay_seconds}s...",
            job_id,
        ),
    )
    conn.commit()
    if cur.rowcount == 0:
        current = get_job(conn, job_id)
        log.warning("job %d: audit retry CAS skipped due to concurrent status change", job_id)
        return current.get("status", "stopped") if current else "stopped"
    update_job_metrics(conn, job_id, audit_attempts=retry_num)
    log_event(
        conn,
        job_id,
        "audit_retry_scheduled",
        detail=f"Audit retry {retry_num}/{_AUDIT_RETRY_LIMIT}: {error_message[:200]}",
        initiator=initiator,
    )
    return "queued"


def _record_repair_cluster(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    board: str,
    phase: str,
    failure_type: str,
    summary: str,
    output_dir: str | Path | None,
    artifacts: dict[str, str] | None = None,
    field_labels: list[str] | None = None,
) -> None:
    from job_db import ensure_job_metrics, update_job_metrics
    from repair_fingerprints import record_repairable_failure_cluster

    try:
        cluster = record_repairable_failure_cluster(
            conn,
            job_id=job_id,
            board=board,
            phase=phase,
            failure_type=failure_type,
            summary=summary,
            field_labels=field_labels,
            output_dir=output_dir,
            suggestions=[
                "Apply a generalized cross-board fix, then requeue impacted jobs.",
                "Review screenshot and report artifacts for representative jobs.",
            ],
            artifacts=artifacts,
        )
    except Exception as exc:
        log.warning("job %d: failed to persist repair cluster: %s", job_id, exc)
        return
    cluster_id = (cluster or {}).get("id")
    if cluster_id is None:
        return
    ensure_job_metrics(conn, job_id)
    update_job_metrics(conn, job_id, last_repair_cluster_id=cluster_id)


def requeue_jobs_for_repair_redraft(
    conn: sqlite3.Connection,
    job_ids: list[int],
    *,
    initiator: str = "repair_supervisor",
) -> list[int]:
    from job_db import SubmissionLockError, update_status

    updated: list[int] = []
    for raw_job_id in job_ids:
        try:
            job_id = int(raw_job_id)
        except (TypeError, ValueError):
            continue
        try:
            update_status(
                conn,
                job_id,
                "queued",
                error_message="",
                progress="Repair supervisor requested a fresh draft rerun.",
                clear_provider=True,
                retry_after=RETRY_AFTER_SENTINEL,
                initiator=initiator,
            )
        except SubmissionLockError:
            log_event(
                conn,
                job_id,
                "repair_redraft_skipped_locked",
                detail="queued",
                initiator=initiator,
            )
            continue
        log_event(conn, job_id, "repair_redraft_queued", detail="queued", initiator=initiator)
        updated.append(job_id)
    return updated


def prepare_jobs_for_repair_canary(
    conn: sqlite3.Connection,
    job_ids: list[int],
    *,
    initiator: str = "repair_supervisor",
) -> dict[int, dict[str, object]]:
    from job_db import get_job, update_status

    snapshots: dict[int, dict[str, object]] = {}
    for raw_job_id in job_ids:
        try:
            job_id = int(raw_job_id)
        except (TypeError, ValueError):
            continue
        job = get_job(conn, job_id)
        if job is None:
            continue
        snapshots[job_id] = {
            "status": job.get("status"),
            "error_message": job.get("error_message"),
            "failure_type": job.get("failure_type"),
            "progress": job.get("progress"),
            "provider": job.get("provider"),
            "retry_after": job.get("retry_after"),
            "auth_state": job.get("auth_state"),
            "auth_scope": job.get("auth_scope"),
        }
        update_status(
            conn,
            job_id,
            "fix_in_progress",
            error_message="",
            progress="Repair supervisor canary rerun in progress.",
            clear_provider=True,
            retry_after=RETRY_AFTER_SENTINEL,
            initiator=initiator,
        )
        log_event(conn, job_id, "repair_canary_staged", detail="fix_in_progress", initiator=initiator)
    return snapshots


def restore_jobs_after_failed_repair_canary(
    conn: sqlite3.Connection,
    snapshots: dict[int, dict[str, object]],
    *,
    initiator: str = "repair_supervisor",
) -> list[int]:
    restored: list[int] = []
    for job_id, snapshot in snapshots.items():
        conn.execute(
            """
            UPDATE jobs
               SET status = ?,
                   error_message = ?,
                   failure_type = ?,
                   progress = ?,
                   provider = ?,
                   retry_after = ?,
                   auth_state = ?,
                   auth_scope = ?
             WHERE id = ?
            """,
            (
                snapshot.get("status"),
                snapshot.get("error_message"),
                snapshot.get("failure_type"),
                snapshot.get("progress"),
                snapshot.get("provider"),
                snapshot.get("retry_after"),
                snapshot.get("auth_state"),
                snapshot.get("auth_scope"),
                job_id,
            ),
        )
        log_event(conn, job_id, "repair_canary_restored", detail=str(snapshot.get("status") or ""), initiator=initiator)
        restored.append(job_id)
    conn.commit()
    return restored


def stop_jobs_for_exhausted_repair_cluster(
    conn: sqlite3.Connection,
    job_ids: list[int],
    *,
    cluster_summary: str,
    initiator: str = "repair_supervisor",
) -> list[int]:
    from job_db import ensure_job_metrics, get_job, get_job_metrics, update_job_metrics, update_status

    updated: list[int] = []
    message = str(cluster_summary or "").strip()[:500] or "Repair supervisor exhausted bounded attempts."
    for raw_job_id in job_ids:
        try:
            job_id = int(raw_job_id)
        except (TypeError, ValueError):
            continue
        job = get_job(conn, job_id)
        if job is None:
            continue
        ensure_job_metrics(conn, job_id)
        metrics = get_job_metrics(conn, job_id) or {}
        current_status = str(job.get("status") or "").strip()
        current_failure_type = str(job.get("failure_type") or "").strip()
        truthful_terminal = current_status == "stopped" and current_failure_type not in {"", "audit_failure"}
        if truthful_terminal:
            log_event(conn, job_id, "repair_cluster_exhausted", detail=message[:300], initiator=initiator)
            updated.append(job_id)
            continue
        update_status(
            conn,
            job_id,
            "stopped",
            error_message=message,
            progress="Repair attempts exhausted; manual investigation required.",
            clear_provider=True,
            failure_type="audit_failure",
            initiator=initiator,
        )
        update_job_metrics(
            conn,
            job_id,
            audit_failure_count=int(metrics.get("audit_failure_count", 0) or 0) + 1,
        )
        output_dir = job.get("output_dir")
        if output_dir:
            write_audit_failure_report(
                output_dir=output_dir,
                job_id=job_id,
                summary=message,
                suggestions=["Apply a manual repair or rerun after investigating the recurring audit failure."],
                attempts=["Repair supervisor exhausted bounded attempts for this clustered failure."],
            )
        log_event(conn, job_id, "repair_cluster_exhausted", detail=message[:300], initiator=initiator)
        updated.append(job_id)
    return updated


def _handle_draft_audit_decision(
    conn: sqlite3.Connection,
    job_id: int,
    output_dir: str | Path | None,
    *,
    board_name: str | None = None,
    missing_items: list[str] | None = None,
    initiator: str = "worker",
) -> str:
    from job_db import get_job_metrics, update_job_metrics, update_status

    decision = audit_draft_outcome(
        output_dir,
        board_name=board_name,
        missing_items=missing_items,
    )
    if decision.kind == "ready":
        return "ready"
    if decision.kind == "terminal":
        update_status(
            conn,
            job_id,
            "stopped",
            error_message=decision.reason[:500],
            failure_type=decision.failure_type,
        )
        log_event(conn, job_id, "draft_audit_terminal", detail=decision.reason[:300], initiator=initiator)
        return "stopped"
    if decision.failure_type == "rendered_audit_mismatch":
        metrics = get_job_metrics(conn, job_id) or {}
        update_job_metrics(
            conn,
            job_id,
            rendered_audit_failures=int(metrics.get("rendered_audit_failures", 0) or 0) + 1,
        )
    _record_repair_cluster(
        conn,
        job_id,
        board=str(board_name or "unknown").strip() or "unknown",
        phase="draft_audit",
        failure_type=str(decision.failure_type or "unknown").strip() or "unknown",
        summary=decision.reason,
        output_dir=output_dir,
        artifacts=decision.artifacts,
    )
    return _schedule_audit_retry(
        conn,
        job_id,
        decision.reason,
        initiator=initiator,
        output_dir=output_dir,
        repair_actions=decision.repair_actions,
    )


def _suggest_resolution(error_message: str) -> str:
    """Generate a user-friendly suggestion based on the error type."""
    lower = (error_message or "").lower()
    if "retry later" in lower or "rate limit" in lower:
        return "Job board is rate-limiting. Try again in a few hours or provide JD text directly."
    if "timed out" in lower or "timeout" in lower:
        return "LLM or browser timed out. Check network or increase timeout."
    if "unsupported" in lower:
        return "This job board is not supported. Apply manually using generated resume/cover letter."
    if "captcha" in lower:
        return "CAPTCHA detected. Complete it manually, then restart the job."
    if "auth" in lower or "login" in lower:
        return "Authentication required. Log in to the job board manually first."
    if "auto-fix slot" in lower:
        return "Auto-fix was busy. Will auto-retry."
    if "json" in lower and "decode" in lower:
        return "LLM returned malformed JSON. Retry usually fixes this."
    return "Check the job logs for details and try restarting."


def _poll_captcha_signal(db_path, job_id, output_dir, stop_event):
    """Background thread: poll for awaiting_captcha.json and update DB status.

    Creates its own SQLite connection — must NOT share the caller's connection
    because this runs in a separate thread.
    """
    from job_db import log_event, open_db, update_status
    from output_layout import active_submit_dir_name

    if not output_dir:
        return
    conn = open_db(db_path)
    submit_dirname = active_submit_dir_name(output_dir) if output_dir else "submit"
    signal_path = Path(output_dir) / submit_dirname / "awaiting_captcha.json"
    notified = False
    while not stop_event.is_set():
        stop_event.wait(5)
        if signal_path.exists() and not notified:
            update_status(conn, job_id, "awaiting_captcha")
            log_event(conn, job_id, "awaiting_captcha", initiator="worker")
            notified = True
        elif not signal_path.exists() and notified:
            # Captcha resolved — restore to submitting
            update_status(conn, job_id, "submitting", progress="Captcha resolved, resuming...")
            log_event(conn, job_id, "captcha_resolved", initiator="worker")
            break
    conn.close()


def process_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    worker_id: int = 0,
    headless: bool | None = None,  # None = use default (submit→headed, draft→headless)
    auto_submit: bool = False,
) -> str:
    """Process a single job through the full lifecycle. Returns final status string.

    Phase 1: URL Resolution — detect source, resolve aggregator URLs
    Phase 2: Asset Generation — run run_pipeline.py with provider fallback
    Phase 3: Submit — run submit_application.py with --submit flag
    Phase 4: Auto-Fix — if submit fails, invoke configured provider CLI (skip if not on PATH)
    Phase 5: Retry with Recording — Playwright trace + action_log.md (final attempt)
    Phase 6: Post-Submit — Notion sync + email reply

    Transient failures (rate limits, timeouts) are auto-retried up to
    MAX_AUTO_RETRIES times. After that, the job is flagged as stopped
    with a descriptive message and suggestion.
    """
    from job_db import (
        end_phase,
        ensure_job_metrics,
        get_job,
        get_job_metrics,
        log_event,
        start_phase,
        update_job_metrics,
        update_progress,
        update_status,
    )

    job = get_job(conn, job_id)
    if job is None:
        log.error("job %d not found", job_id)
        return "stopped"

    url = job["url"]
    log.info("processing job %d: %s (status=%s)", job_id, url, job.get("status"))

    ensure_job_metrics(conn, job_id)
    _phase_count = 0

    # queued_submit → full pipeline with auto_submit
    _original_auto_submit = auto_submit
    if job.get("status") == "queued_submit":
        auto_submit = True

    # ── Regenerate-only: run phases 1-2 but skip autofill ───────────────
    _is_regenerating = job.get("status") == "regenerating"

    # ── Approved draft / reanswer: skip phases 1-2 ─────────────────────
    # IMPORTANT: Only honour "submitting" as approved-draft if auto_submit
    # was explicitly requested (queued_submit or --auto-submit).  A worker
    # that picked the job up from "queued" must NEVER auto-submit even if
    # a race condition left the status as "submitting" from another worker.
    _skip_status = job.get("status")
    if _skip_status in ("submitting", "reanswering") and job.get("output_dir"):
        board_url = job.get("board_url") or url
        output_dir = job["output_dir"]
        source = job.get("source") or "direct"
        if _skip_status == "submitting" and (_original_auto_submit or auto_submit):
            log.info("job %d: approved draft, resuming at submit phase", job_id)
            auto_submit = True
            update_progress(conn, job_id, "Approved — submitting application...")
        elif _skip_status == "submitting":
            # Race condition: another worker set "submitting" but we were not
            # asked to auto-submit. Treat as draft mode.
            log.warning(
                "job %d: found in 'submitting' status but auto_submit=False — "
                "treating as draft to prevent unauthorized submission",
                job_id,
            )
            auto_submit = False
            update_progress(conn, job_id, "Filling application (draft mode)...")
        else:
            log.info("job %d: reanswering — re-running autofill in draft mode", job_id)
            auto_submit = False
            update_progress(conn, job_id, "Re-answering application questions...")
            # Clear cached answers so they're regenerated fresh with draft overrides
            _clear_cached_answers(Path(output_dir))
        # Skip phases 1-2, fall through to phase 3 below
    else:
        # ── Pre-flight: sync master resume if stale (>24h) ────────────────
        try:
            from sync_master_resume import sync_if_stale

            if sync_if_stale():
                log.info("job %d: master resume synced from Google Doc", job_id)
        except Exception:
            pass  # non-fatal; use whatever local copy exists

        # ── Phases 1-2: resolve URL and generate assets ───────────────────
        board_url, output_dir, source = _run_phases_1_2(
            conn,
            job_id,
            job,
            url,
            headless=headless,
            auto_submit=auto_submit,
        )
        if board_url is None:
            return output_dir  # output_dir holds the error status string

    # Write auto_submit flag to pipeline meta for downstream verification
    if output_dir:
        _meta = _load_pipeline_meta(Path(output_dir)) or {}
        if _meta.get("auto_submit") != auto_submit:
            _meta["auto_submit"] = auto_submit
            try:
                (Path(output_dir) / ".pipeline_meta.json").write_text(json.dumps(_meta, indent=2) + "\n")
            except OSError:
                pass

    # ── Regenerate-only: skip autofill, go straight to draft ────────────
    if _is_regenerating and output_dir:
        log.info("job %d: regeneration complete — returning to draft (skipping autofill)", job_id)
        update_status(conn, job_id, "draft", progress="Draft ready for review")
        log_event(conn, job_id, "regeneration_complete", initiator="worker")
        return "draft"

    # ── Phase 3: Submit / Autofill ──────────────────────────────────────
    log.info(
        "job %d: entering phase 3 (auto_submit=%s, headless=%s, worker=%d)", job_id, auto_submit, headless, worker_id
    )
    _p = start_phase(conn, job_id, "submit")
    update_status(conn, job_id, "submitting" if auto_submit else "autofilling")

    # ── Guard: skip boards with repeated auth failures ────────────────
    _board_for_guard = job.get("board") or _detect_board_from_url(board_url)
    if _board_for_guard and _board_for_guard != "unknown":
        from job_db import get_recent_auth_failures

        _auth_scope = _auth_scope_for_job(job, board_url, _board_for_guard)
        _auth_fail_count = get_recent_auth_failures(conn, _board_for_guard, auth_scope=_auth_scope)
        if _auth_fail_count >= 3:
            if _auth_scope:
                _guard_subject = _auth_scope.partition(":")[2] or _auth_scope
                _skip_msg = (
                    f"Board '{_board_for_guard}' auth scope '{_guard_subject}' was guarded after "
                    f"{_auth_fail_count} credential rejections in the last 24h."
                )
            else:
                _skip_msg = (
                    f"Board '{_board_for_guard}' was guarded after {_auth_fail_count} credential rejections "
                    "in the last 24h."
                )
            log.warning("job %d: %s", job_id, _skip_msg)
            update_status(
                conn,
                job_id,
                "stopped",
                error_message=_skip_msg,
                failure_type="auth_guarded",
                auth_scope=_auth_scope,
            )
            log_event(
                conn,
                job_id,
                "auth_skip",
                detail=_skip_msg,
                detail_json={"board": _board_for_guard, "auth_scope": _auth_scope, "count": _auth_fail_count},
                initiator="worker",
            )
            end_phase(conn, _p, exit_code=1)
            _phase_count += 1
            update_job_metrics(conn, job_id, phase_count=_phase_count)
            return "stopped"

    if output_dir:
        _detect_and_log_content_edits(conn, job_id, Path(output_dir))
        # Clear stale submit artifacts from previous runs so they don't
        # mislead downstream consumers (e.g. a prior "unknown" result masking
        # a successful resubmit).
        _submit_dir = _prepare_submit_dir_for_new_attempt(output_dir)
        _clear_stale_submit_attempt_artifacts(_submit_dir)
    try:
        submit_script = str(SCRIPT_DIR / "submit_application.py")
        submit_target = str(output_dir) if output_dir else board_url
        if auto_submit:
            submit_cmd_base = _uv_python_cmd(submit_script, submit_target, "--submit")
        else:
            # Draft mode — fill form but do NOT click submit
            submit_cmd_base = _uv_python_cmd(submit_script, submit_target, "--draft")

        # Default: submit→headed, draft→headless. Explicit headless overrides.
        _headless = headless if headless is not None else (not auto_submit)

        env = os.environ.copy()
        if worker_id > 0:
            from browser_runtime import submit_browser_profile_dir

            profile = submit_browser_profile_dir(worker_id=worker_id)
            env["JOB_ASSETS_SUBMIT_BROWSER_PROFILE_DIR"] = str(profile)

        submit_result = None
        submit_rc = 1
        _submit_stdout = ""
        _submit_stderr = ""
        _current_submit_dir = None
        _submission_result = None
        _attempt_headless = _headless
        _headed_retry_used = False

        while True:
            submit_cmd = [*submit_cmd_base]
            if _attempt_headless:
                submit_cmd.append("--headless")

            log.info(
                "job %d: launching submit subprocess: %s (headless=%s)",
                job_id,
                " ".join(submit_cmd),
                _attempt_headless,
            )
            # Get db_path from the connection for the captcha thread's own connection
            _db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
            _signal_stop = threading.Event()
            _signal_thread = threading.Thread(
                target=_poll_captcha_signal,
                args=(_db_path, job_id, output_dir, _signal_stop),
                daemon=True,
            )
            _signal_thread.start()
            try:
                _submit_timeout = DEFAULT_SUBMIT_TIMEOUT if _attempt_headless else (_CAPTCHA_TIMEOUT + 300)
                t0 = time.monotonic()
                submit_result = _run_worker_subprocess(
                    submit_cmd,
                    cwd=PROJECT_ROOT,
                    env=env,
                    timeout=_submit_timeout,
                    capture_output=True,
                    text=True,
                )
            finally:
                _signal_stop.set()
                _signal_thread.join(timeout=2)
            duration_ms = int((time.monotonic() - t0) * 1000)
            submit_rc = submit_result.returncode

            event_type = "submit_attempt" if auto_submit else "draft_attempt"
            log_event(
                conn,
                job_id,
                event_type,
                detail_json={"exit_code": submit_rc, "duration_ms": duration_ms},
                initiator="worker",
            )
            # Log subprocess output for debugging (browser profile, session, etc.)
            _submit_stdout = (submit_result.stdout or "").strip()
            _submit_stderr = (submit_result.stderr or "").strip()
            _combined_output = "\n".join(filter(None, [_submit_stdout, _submit_stderr]))
            if _combined_output:
                log_event(conn, job_id, "submit_output", detail=_combined_output[:4000], initiator="worker")

            _current_submit_dir = _active_submit_dir_for_output(output_dir)
            _submission_result = _load_application_submission_result(_current_submit_dir)
            _result_status = str((_submission_result or {}).get("status") or "").strip().casefold()
            _auth_failure_sidecars = (
                list(_current_submit_dir.glob("*_auth_failure.json"))
                if _current_submit_dir is not None and _current_submit_dir.exists()
                else []
            )
            _output_lower = _combined_output.casefold()
            _captcha_result = _result_status == "skipped_captcha"
            _auth_like_result = _result_status in {
                "skipped_auth",
                "skipped_auth_failure",
                "auth_failed",
                "auth_unknown",
                "auth_guarded",
                "service_unavailable",
            }
            _captcha_output_result = (
                submit_rc == CAPTCHA_SKIP_EXIT_CODE and "captcha" in _output_lower and not _auth_like_result
            )
            _auth_sidecar_indicates_auth_skip = _captcha_result and bool(_auth_failure_sidecars)
            _should_retry_headed = (
                (not auto_submit)
                and _attempt_headless
                and (not _headed_retry_used)
                and (_captcha_result or _captcha_output_result)
                and (not _auth_sidecar_indicates_auth_skip)
            )
            if not _should_retry_headed:
                break

            _headed_retry_used = True
            log_event(
                conn,
                job_id,
                "draft_headed_retry_after_captcha",
                detail="Captcha interrupted headless draft attempt; relaunching once in headed mode.",
                detail_json={"exit_code": submit_rc, "submission_result_status": _result_status},
                initiator="worker",
            )
            if output_dir:
                _submit_dir = _prepare_submit_dir_for_new_attempt(output_dir)
                _clear_stale_submit_attempt_artifacts(_submit_dir)
            _attempt_headless = False

        # Preserve the effective mode from the terminal submit attempt.
        _headless = _attempt_headless

        if submit_rc != 0:
            if not auto_submit:
                _non_failed_submission_result = _submission_result
                if _non_failed_submission_result and (
                    str(_non_failed_submission_result.get("status") or "").strip().casefold() != "failed"
                ):
                    _draft_result_status = handle_draft_mode_submission_result(
                        conn,
                        job_id,
                        _non_failed_submission_result,
                    )
                    if _draft_result_status is not None:
                        end_phase(conn, _p, exit_code=1)
                        _phase_count += 1
                        update_job_metrics(conn, job_id, phase_count=_phase_count)
                        return _draft_result_status
            _failed_result = _load_failed_submission_result(_current_submit_dir)
            if _failed_result:
                result_status = _handle_failed_submission_result(conn, job_id, output_dir, _failed_result)
                end_phase(conn, _p, exit_code=1)
                _phase_count += 1
                update_job_metrics(conn, job_id, phase_count=_phase_count)
                return result_status

        if submit_rc == 0:
            # Check for unsupported board (exits 0 but didn't actually fill/submit)
            if output_dir:
                from output_layout import active_submit_dir_name as _asd

                _check_sd = Path(output_dir) / (_asd(output_dir) if output_dir else "submit")
                if (_check_sd / "unsupported_board.json").exists():
                    _fail_pending_answer_refresh_for_output(
                        conn,
                        job_id,
                        output_dir,
                        reason="unsupported_board",
                        message="Answer regeneration stopped because the board is unsupported.",
                    )
                    update_status(
                        conn,
                        job_id,
                        "stopped",
                        error_message="Unsupported job board — apply manually",
                        failure_type="unsupported",
                    )
                    log_event(conn, job_id, "unsupported_board", initiator="worker")
                    end_phase(conn, _p, exit_code=1)
                    _phase_count += 1
                    update_job_metrics(conn, job_id, phase_count=_phase_count)
                    return "stopped"

                _workday_auth = _load_workday_auth_result(_check_sd)
                if _workday_auth:
                    _af_status = _workday_auth.get("status") or "auth_unknown"
                    _af_msg = _workday_auth.get("message") or "Workday authentication did not reach the application."
                    _af_scope = _workday_auth.get("auth_scope") or _workday_auth_scope_for_job(job, board_url)
                    _af_state = str(_workday_auth.get("auth_state") or "").strip() or None
                    if "workday" not in _af_msg.lower():
                        _af_msg = f"[workday] {_af_msg}"

                    if _af_status == "service_unavailable":
                        _fail_pending_answer_refresh_for_output(
                            conn,
                            job_id,
                            output_dir,
                            reason="service_unavailable",
                            message="Answer regeneration stopped because Workday was temporarily unavailable.",
                        )
                        update_status(
                            conn,
                            job_id,
                            "stopped",
                            error_message=_af_msg,
                            failure_type="service_unavailable",
                            auth_state=_af_state,
                            auth_scope=_af_scope,
                        )
                        log_event(
                            conn,
                            job_id,
                            "workday_service_unavailable",
                            detail=_af_msg,
                            detail_json=_workday_auth,
                            initiator="worker",
                        )
                        end_phase(conn, _p, exit_code=1)
                        _phase_count += 1
                        update_job_metrics(conn, job_id, phase_count=_phase_count)
                        return "stopped"

                    _failure_type = "auth_failed" if _af_status == "auth_failed" else "auth_unknown"
                    _reason = _failure_type
                    _refresh_message = (
                        "Answer regeneration stopped because Workday rejected the configured credentials."
                        if _failure_type == "auth_failed"
                        else "Answer regeneration stopped because Workday ended in an unknown auth state."
                    )
                    _event_type = "auth_failure" if _failure_type == "auth_failed" else "auth_unknown"
                    _fail_pending_answer_refresh_for_output(
                        conn,
                        job_id,
                        output_dir,
                        reason=_reason,
                        message=_refresh_message,
                    )
                    update_status(
                        conn,
                        job_id,
                        "stopped",
                        error_message=_af_msg,
                        failure_type=_failure_type,
                        auth_state=_af_state,
                        auth_scope=_af_scope,
                    )
                    log_event(
                        conn,
                        job_id,
                        _event_type,
                        detail=_af_msg,
                        detail_json=_workday_auth,
                        initiator="worker",
                    )
                    end_phase(conn, _p, exit_code=1)
                    _phase_count += 1
                    update_job_metrics(conn, job_id, phase_count=_phase_count)
                    return "stopped"

                _auth_files = [
                    path for path in _check_sd.glob("*_auth_failure.json") if path.name != "workday_auth_failure.json"
                ]
                _draft_submission_result = _load_application_submission_result(_check_sd)
                _draft_result_status_name = str((_draft_submission_result or {}).get("status") or "").strip().casefold()
                _draft_result_has_explicit_auth_context = any(
                    str((_draft_submission_result or {}).get(key) or "").strip()
                    for key in ("failure_type", "auth_state", "auth_scope")
                )
                _prefer_auth_sidecar_over_generic_skipped_captcha = (
                    _draft_result_status_name == "skipped_captcha"
                    and bool(_auth_files)
                    and not _draft_result_has_explicit_auth_context
                )
                if not _prefer_auth_sidecar_over_generic_skipped_captcha:
                    _draft_result_status = handle_draft_mode_submission_result(
                        conn,
                        job_id,
                        _draft_submission_result,
                    )
                    if _draft_result_status is not None:
                        end_phase(conn, _p, exit_code=0 if _draft_result_status == "submitted" else 1)
                        _phase_count += 1
                        update_job_metrics(conn, job_id, phase_count=_phase_count)
                        return _draft_result_status

                # Check for auth failure (iCIMS and other boards still using generic auth files)
                if _auth_files:
                    try:
                        _af_data = json.loads(_auth_files[0].read_text(encoding="utf-8"))
                        _af_msg = _af_data.get("message", "Authentication failed")
                    except Exception:
                        _af_data = {}
                        _af_msg = "Authentication failed"
                    _af_board_name = _board_for_guard or "unknown"
                    _af_scope = str(_af_data.get("auth_scope") or "").strip() or _auth_scope_for_job(
                        job,
                        board_url,
                        _af_board_name,
                    )
                    _af_state = str(_af_data.get("auth_state") or "").strip() or None
                    if _af_board_name not in _af_msg.lower():
                        _af_msg = f"[{_af_board_name}] {_af_msg}"
                    _fail_pending_answer_refresh_for_output(
                        conn,
                        job_id,
                        output_dir,
                        reason="auth_failed",
                        message="Answer regeneration stopped because submission hit an authentication failure.",
                    )
                    update_status(
                        conn,
                        job_id,
                        "stopped",
                        error_message=_af_msg,
                        failure_type="auth_failed",
                        auth_state=_af_state,
                        auth_scope=_af_scope,
                    )
                    log_event(
                        conn,
                        job_id,
                        "auth_failure",
                        detail=_af_msg,
                        detail_json=_af_data or None,
                        initiator="worker",
                    )
                    end_phase(conn, _p, exit_code=1)
                    _phase_count += 1
                    update_job_metrics(conn, job_id, phase_count=_phase_count)
                    return "stopped"

            if not auto_submit:
                # Draft mode: form filled but not submitted — generate draft artifacts
                from draft_manager import generate_draft_summary
                from output_layout import active_submit_dir_name

                submit_dirname = active_submit_dir_name(output_dir) if output_dir else "submit"
                submit_dir = Path(output_dir) / submit_dirname if output_dir else None

                # Detect actual board from submit artifacts if DB board is unknown
                actual_board = job.get("board") or _detect_board_from_url(board_url)
                if (actual_board in (None, "unknown")) and submit_dir and submit_dir.is_dir():
                    actual_board = _detect_board_from_submit_dir(submit_dir) or actual_board
                if actual_board and actual_board != "unknown" and actual_board != job.get("board"):
                    update_status(conn, job_id, job.get("status", "autofilling"), board=actual_board)

                if submit_dir and submit_dir.is_dir():
                    pipeline_meta = _load_pipeline_meta(Path(output_dir)) or {}
                    draft_meta = {
                        "company": job.get("company")
                        or pipeline_meta.get("company_proper")
                        or pipeline_meta.get("company"),
                        "role_title": job.get("role_title") or pipeline_meta.get("role"),
                        "board": actual_board or "unknown",
                    }
                    generate_draft_summary(Path(output_dir), submit_dir, draft_meta)

                refresh_state = _finalize_pending_answer_refresh(conn, job_id, output_dir)
                if refresh_state and refresh_state.get("status") == STATUS_FAILED:
                    update_status(
                        conn,
                        job_id,
                        "stopped",
                        error_message=refresh_state.get("message")
                        or "Answer regeneration failed to produce fresh proof.",
                        failure_type="answer_refresh_failed",
                    )
                    end_phase(conn, _p, exit_code=1)
                    _phase_count += 1
                    update_job_metrics(conn, job_id, phase_count=_phase_count)
                    return "stopped"

                if output_dir and submit_dir and submit_dir.is_dir():
                    _sync_draft_proof_blockers(
                        Path(output_dir),
                        board_name=actual_board,
                        draft_meta=draft_meta,
                    )

                # Validate draft completeness before marking as draft
                missing = _validate_draft_completeness(
                    Path(output_dir) if output_dir else None,
                    board_name=actual_board,
                )
                draft_audit_status = _handle_draft_audit_decision(
                    conn,
                    job_id,
                    Path(output_dir) if output_dir else None,
                    board_name=actual_board,
                    missing_items=missing,
                    initiator="worker",
                )
                if draft_audit_status != "ready":
                    end_phase(conn, _p, exit_code=1)
                    _phase_count += 1
                    update_job_metrics(conn, job_id, phase_count=_phase_count)
                    return draft_audit_status

                update_status(conn, job_id, "draft", progress="Draft ready for review")
                log_event(conn, job_id, "draft_generated", initiator="worker")
                clear_audit_failure_report(Path(output_dir) if output_dir else None)
                ensure_job_metrics(conn, job_id)
                metrics = get_job_metrics(conn, job_id) or {}
                if metrics.get("audit_attempts", 0):
                    update_job_metrics(conn, job_id, audit_attempts=0)
                end_phase(conn, _p, exit_code=0)
                _phase_count += 1
                # Parse autofill report for field counts + LLM answer tracking
                if output_dir:
                    for report_file in Path(output_dir).rglob("*_autofill_report.json"):
                        try:
                            report_data = json.loads(report_file.read_text(encoding="utf-8"))
                            field_counts = parse_autofill_report(report_data)
                            update_job_metrics(
                                conn,
                                job_id,
                                total_fields=field_counts["total_fields"],
                                llm_generated_answers=field_counts["llm_generated_count"],
                                llm_generated_labels=json.dumps(field_counts["llm_generated_labels"]),
                            )
                            # Log LLM-generated answers for review
                            if field_counts["llm_generated_count"] > 0:
                                log_event(
                                    conn,
                                    job_id,
                                    "llm_answers_generated",
                                    detail=f"{field_counts['llm_generated_count']} AI-generated answer(s): "
                                    + ", ".join(field_counts["llm_generated_labels"][:5]),
                                    initiator="worker",
                                )
                            break
                        except Exception:
                            pass
                _finalize_pending_answer_refresh(conn, job_id, output_dir)
                update_job_metrics(conn, job_id, phase_count=_phase_count)
                return "draft"

            # Only mark as "submitted" if we have actual confirmation
            # (website confirmation or email confirmation from post-submit).
            # Otherwise mark as stopped so the user knows it wasn't confirmed.
            end_phase(conn, _p, exit_code=0)
            _phase_count += 1

            # Parse autofill report for field counts
            if output_dir:
                for report_file in Path(output_dir).rglob("*_autofill_report.json"):
                    try:
                        report_data = json.loads(report_file.read_text(encoding="utf-8"))
                        field_counts = parse_autofill_report(report_data)
                        update_job_metrics(conn, job_id, total_fields=field_counts["total_fields"])
                        break
                    except Exception:
                        pass
            _finalize_pending_answer_refresh(conn, job_id, output_dir)

            log_event(conn, job_id, "submit_attempt", initiator="worker")
            final_submit_status = _finalize_successful_submission(conn, job_id, output_dir, board_url)
            update_job_metrics(conn, job_id, phase_count=_phase_count)
            return final_submit_status

        if submit_rc == CAPTCHA_SKIP_EXIT_CODE:
            _fail_pending_answer_refresh_for_output(
                conn,
                job_id,
                output_dir,
                reason="captcha_encountered",
                message="Answer regeneration stopped because a captcha interrupted submission.",
            )
            update_status(
                conn, job_id, "stopped", error_message="Captcha encountered during submission", failure_type="captcha"
            )
            end_phase(conn, _p, exit_code=1)
            _phase_count += 1
            update_job_metrics(conn, job_id, phase_count=_phase_count)
            return "stopped"

        # Submit failed but not captcha — end phase as failure and continue to Phase 4/5
        end_phase(conn, _p, exit_code=1)
        _phase_count += 1

    except subprocess.TimeoutExpired:
        log.warning("Submit timed out for job %d", job_id)
        _submit_dir = _active_submit_dir_for_output(output_dir)
        if _detect_board_from_url(board_url) == "linkedin":
            _linkedin_timeout = _synthesize_linkedin_timeout_result(_submit_dir)
            if _linkedin_timeout and _submit_dir is not None:
                _write_application_submission_result(_submit_dir, _linkedin_timeout)
                result_status = _handle_linkedin_failure_result(conn, job_id, output_dir, _linkedin_timeout)
                end_phase(conn, _p, exit_code=1)
                _phase_count += 1
                update_job_metrics(conn, job_id, phase_count=_phase_count)
                return result_status
        _fail_pending_answer_refresh_for_output(
            conn,
            job_id,
            output_dir,
            reason="submit_timeout",
            message="Answer regeneration timed out before fresh proof was recorded.",
        )
        update_status(conn, job_id, "stopped", error_message="Submit timed out", failure_type="timeout")
        end_phase(conn, _p, exit_code=1)
        _phase_count += 1
        update_job_metrics(conn, job_id, phase_count=_phase_count)
        return "stopped"
    except Exception as exc:
        log.exception("Phase 3 failed for job %d", job_id)
        _fail_pending_answer_refresh_for_output(
            conn,
            job_id,
            output_dir,
            reason="submit_exception",
            message="Answer regeneration crashed before fresh proof was recorded.",
        )
        update_status(conn, job_id, "stopped", error_message=f"Submit error: {exc}", failure_type="crash")
        end_phase(conn, _p, exit_code=1)
        _phase_count += 1
        update_job_metrics(conn, job_id, phase_count=_phase_count)
        return "stopped"

    # ── Phase 4: Auto-Fix ────────────────────────────────────────────────
    # Skip auto-fix for reanswer runs — user doesn't want code changes
    if _skip_status == "reanswering" and submit_rc != 0:
        _fail_pending_answer_refresh_for_output(
            conn,
            job_id,
            output_dir,
            reason="reanswer_submit_failed",
            message="Answer regeneration failed before fresh proof was recorded.",
        )
        update_status(
            conn,
            job_id,
            "stopped",
            error_message=f"Re-answer submit failed (exit {submit_rc})",
            failure_type="submit_failed",
        )
        update_job_metrics(conn, job_id, phase_count=_phase_count)
        return "stopped"

    # Check if the failure is a server-side validation error (not a code bug).
    # Auto-fix can't help with these — just stop and let the user retry.
    _submit_stderr = getattr(submit_result, "stderr", "") or ""
    _VALIDATION_SKIP_PATTERNS = (
        "validation_error",
        "did not reach a confirmed completion state",
        "captcha required",
        "job_closed",
    )
    _is_validation_failure = any(p in _submit_stderr.lower() for p in _VALIDATION_SKIP_PATTERNS)
    if _is_validation_failure and submit_rc != 0:
        _err_lines = [line for line in _submit_stderr.strip().splitlines() if line.strip()]
        _err_msg = _err_lines[-1][:300] if _err_lines else f"Submit failed (exit {submit_rc})"
        _is_job_closed = "job_closed" in _submit_stderr.lower()
        _ftype = "job_closed" if _is_job_closed else "submit_failed"
        log.info(
            "job %d: skipping auto-fix — %s",
            job_id,
            "job closed/removed" if _is_job_closed else "server-side validation error",
        )
        _fail_pending_answer_refresh_for_output(
            conn,
            job_id,
            output_dir,
            reason="validation_failure" if not _is_job_closed else "job_closed",
            message="Answer regeneration stopped before fresh proof was recorded.",
        )
        if _is_job_closed:
            _mark_job_unavailable_and_archive(
                conn,
                job_id,
                output_dir=output_dir,
                error_message=_err_msg,
            )
        else:
            update_status(conn, job_id, "stopped", error_message=_err_msg, failure_type=_ftype)
        update_job_metrics(conn, job_id, phase_count=_phase_count)
        return "stopped"

    board = _detect_board_from_url(board_url)
    _active_provider = default_active_provider()
    if provider_available(_active_provider) and submit_rc != 0:
        # Only 1 worker runs auto-fix at a time to avoid starving the pool.
        if not _auto_fix_semaphore.acquire(blocking=False):
            log.info("job %d: auto-fix slot occupied, requeueing for retry", job_id)
            return _auto_retry_if_transient(
                conn,
                job_id,
                f"Submit failed (exit {submit_rc}); auto-fix slot busy",
                failure_type=None,
            )
        _p = start_phase(conn, job_id, "auto_fix")
        m = get_job_metrics(conn, job_id)
        update_job_metrics(conn, job_id, auto_fix_attempts=(m["auto_fix_attempts"] if m else 0) + 1)
        update_status(conn, job_id, "fix_in_progress")
        error_context = {
            "exit_code": submit_rc,
            "board": board,
            "url": board_url,
            "output_dir": str(output_dir) if output_dir else None,
        }
        try:
            fixed = auto_fix(error_context, board)
            log_event(
                conn, job_id, "auto_fix_attempt", detail_json={"fixed": fixed, "board": board}, initiator="worker"
            )
            if fixed:
                # Retry submit after fix — same mode as original (review unless auto_submit)
                update_status(conn, job_id, "retrying")
                _retry_timeout = DEFAULT_SUBMIT_TIMEOUT if _headless else (_CAPTCHA_TIMEOUT + 300)
                retry_result = _run_worker_subprocess(
                    submit_cmd,
                    cwd=PROJECT_ROOT,
                    env=env,
                    timeout=_retry_timeout,
                )
                if retry_result.returncode == 0:
                    end_phase(conn, _p, exit_code=0)
                    _phase_count += 1
                    if auto_submit:
                        final_submit_status = _finalize_successful_submission(conn, job_id, output_dir, board_url)
                        update_job_metrics(conn, job_id, phase_count=_phase_count)
                        return final_submit_status
                    # Go to draft for review, same as Phase 3 success
                    _retry_submit_dir = _active_submit_dir_for_output(output_dir)
                    _retry_result_status = handle_draft_mode_submission_result(
                        conn,
                        job_id,
                        _load_application_submission_result(_retry_submit_dir),
                    )
                    if _retry_result_status is not None:
                        update_job_metrics(conn, job_id, phase_count=_phase_count)
                        return _retry_result_status

                    from draft_manager import generate_draft_summary
                    from output_layout import active_submit_dir_name

                    submit_dirname = active_submit_dir_name(output_dir) if output_dir else "submit"
                    submit_dir = Path(output_dir) / submit_dirname if output_dir else None
                    _af_board = job.get("board") or _detect_board_from_url(board_url)
                    if (_af_board in (None, "unknown")) and submit_dir and submit_dir.is_dir():
                        _af_board = _detect_board_from_submit_dir(submit_dir) or _af_board
                    if _af_board and _af_board != "unknown" and _af_board != job.get("board"):
                        update_status(conn, job_id, job.get("status", "autofilling"), board=_af_board)
                    if submit_dir and submit_dir.is_dir():
                        pipeline_meta = _load_pipeline_meta(Path(output_dir)) or {}
                        draft_meta = {
                            "company": job.get("company") or pipeline_meta.get("company_proper"),
                            "role_title": job.get("role_title") or pipeline_meta.get("role"),
                            "board": _af_board or "unknown",
                        }
                        generate_draft_summary(Path(output_dir), submit_dir, draft_meta)
                    refresh_state = _finalize_pending_answer_refresh(conn, job_id, output_dir)
                    if refresh_state and refresh_state.get("status") == STATUS_FAILED:
                        update_status(
                            conn,
                            job_id,
                            "stopped",
                            error_message=refresh_state.get("message")
                            or "Answer regeneration failed to produce fresh proof.",
                            failure_type="answer_refresh_failed",
                        )
                        update_job_metrics(conn, job_id, phase_count=_phase_count)
                        return "stopped"
                    if output_dir and submit_dir and submit_dir.is_dir():
                        _sync_draft_proof_blockers(
                            Path(output_dir),
                            board_name=_af_board,
                            draft_meta=draft_meta,
                        )
                    missing = _validate_draft_completeness(
                        Path(output_dir) if output_dir else None,
                        board_name=_af_board,
                    )
                    draft_audit_status = _handle_draft_audit_decision(
                        conn,
                        job_id,
                        Path(output_dir) if output_dir else None,
                        board_name=_af_board,
                        missing_items=missing,
                        initiator="worker",
                    )
                    if draft_audit_status != "ready":
                        update_job_metrics(conn, job_id, phase_count=_phase_count)
                        return draft_audit_status
                    update_status(conn, job_id, "draft", progress="Draft ready for review")
                    log_event(conn, job_id, "draft_generated", initiator="worker")
                    clear_audit_failure_report(Path(output_dir) if output_dir else None)
                    ensure_job_metrics(conn, job_id)
                    metrics = get_job_metrics(conn, job_id) or {}
                    if metrics.get("audit_attempts", 0):
                        update_job_metrics(conn, job_id, audit_attempts=0)
                    update_job_metrics(conn, job_id, phase_count=_phase_count)
                    return "draft"
            end_phase(conn, _p, exit_code=1)
            _phase_count += 1
        except Exception as exc:
            log.warning("Auto-fix failed for job %d: %s", job_id, exc)
            log_event(conn, job_id, "auto_fix_error", detail=str(exc), initiator="worker")
            end_phase(conn, _p, exit_code=1)
            _phase_count += 1
        finally:
            _auto_fix_semaphore.release()

    # ── Phase 5: Retry with Recording ────────────────────────────────────
    if output_dir and submit_rc != 0 and submit_rc != CAPTCHA_SKIP_EXIT_CODE:
        _p = start_phase(conn, job_id, "retry_with_recording")
        m = get_job_metrics(conn, job_id)
        update_job_metrics(conn, job_id, retry_count=(m["retry_count"] if m else 0) + 1)
        update_status(conn, job_id, "retrying")
        try:
            payload_path = _find_payload_path(output_dir, board)
            if payload_path:
                recording_rc = retry_with_recording(
                    payload_path,
                    board,
                    headless=_headless,
                    worker_id=worker_id,
                )
                log_event(
                    conn, job_id, "retry_with_recording", detail_json={"exit_code": recording_rc}, initiator="worker"
                )
                if recording_rc == 0:
                    end_phase(conn, _p, exit_code=0)
                    _phase_count += 1
                    if auto_submit:
                        _finalize_pending_answer_refresh(conn, job_id, output_dir)
                        final_submit_status = _finalize_successful_submission(conn, job_id, output_dir, board_url)
                        update_job_metrics(conn, job_id, phase_count=_phase_count)
                        return final_submit_status
                    refresh_state = _finalize_pending_answer_refresh(conn, job_id, output_dir)
                    if refresh_state and refresh_state.get("status") == STATUS_FAILED:
                        update_status(
                            conn,
                            job_id,
                            "stopped",
                            error_message=refresh_state.get("message")
                            or "Answer regeneration failed to produce fresh proof.",
                            failure_type="answer_refresh_failed",
                        )
                        update_job_metrics(conn, job_id, phase_count=_phase_count)
                        return "stopped"
                    update_status(conn, job_id, "draft", progress="Draft ready for review")
                    log_event(conn, job_id, "draft_generated", initiator="worker")
                    update_job_metrics(conn, job_id, phase_count=_phase_count)
                    return "draft"
            end_phase(conn, _p, exit_code=1)
            _phase_count += 1
        except Exception as exc:
            log.warning("Retry with recording failed for job %d: %s", job_id, exc)
            end_phase(conn, _p, exit_code=1)
            _phase_count += 1

    # All attempts exhausted
    _fail_pending_answer_refresh_for_output(
        conn,
        job_id,
        output_dir,
        reason="retries_exhausted",
        message="Answer regeneration exhausted submission attempts before fresh proof was recorded.",
    )
    update_status(
        conn,
        job_id,
        "stopped",
        error_message=f"All submission attempts failed (last exit {submit_rc})",
        failure_type="retries_exhausted",
    )
    update_job_metrics(conn, job_id, phase_count=_phase_count)
    return "stopped"


# ── Retry with Recording ────────────────────────────────────────────────────


def retry_with_recording(
    payload_path: Path,
    board: str,
    *,
    headless: bool = True,
    worker_id: int = 0,
) -> int:
    """Final submission attempt with full Playwright trace recording.

    Saves trace.zip, action_log.md, step screenshots to a debug_recording/
    directory alongside the payload. Enables Playwright tracing via
    environment variables so the autofill script captures everything.
    """
    payload_path = Path(payload_path)
    output_dir = payload_path.parent
    recording_dir = output_dir / "debug_recording"
    recording_dir.mkdir(parents=True, exist_ok=True)

    submit_script = str(SCRIPT_DIR / "submit_application.py")
    cmd = _uv_python_cmd(
        submit_script,
        str(output_dir.parent) if output_dir.name == "submit" else str(output_dir),
        "--submit",
    )
    if headless:
        cmd.append("--headless")

    env = os.environ.copy()
    env["PLAYWRIGHT_TRACE_DIR"] = str(recording_dir)
    env["PLAYWRIGHT_TRACE_ENABLED"] = "1"

    if worker_id > 0:
        from browser_runtime import submit_browser_profile_dir

        profile = submit_browser_profile_dir(worker_id=worker_id)
        env["JOB_ASSETS_SUBMIT_BROWSER_PROFILE_DIR"] = str(profile)

    _recording_timeout = DEFAULT_SUBMIT_TIMEOUT if headless else (_CAPTCHA_TIMEOUT + 300)
    try:
        result = _run_worker_subprocess(
            cmd,
            cwd=PROJECT_ROOT,
            env=env,
            timeout=_recording_timeout,
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        log.warning("Retry with recording timed out for %s", payload_path)
        return 1
    except Exception as exc:
        log.warning("Retry with recording failed: %s", exc)
        return 1


# ── Auto-Fix ─────────────────────────────────────────────────────────────────


def auto_fix(
    error_context: dict,
    board: str,
    *,
    max_attempts: int = 3,
) -> bool:
    """Invoke LLM CLI to diagnose and fix an autofill error.

    Creates a git branch, runs the fix, runs tests. If tests pass, merges
    the fix back. If tests fail, discards the branch.

    Returns True if a fix was successfully applied.
    Skips auto-fix if the configured provider CLI is not available on PATH.
    """
    active_provider = default_active_provider()
    if not provider_available(active_provider):
        log.info("%s provider is not ready — skipping auto-fix", active_provider)
        return False

    branch_name = f"autofix/{board}-{uuid.uuid4().hex[:8]}"
    original_branch = _git_current_branch()
    if original_branch is None:
        log.warning("Could not determine current git branch — skipping auto-fix")
        return False

    try:
        for attempt in range(1, max_attempts + 1):
            log.info("Auto-fix attempt %d/%d for board=%s", attempt, max_attempts, board)
            try:
                # Create a fix branch
                attempt_branch = f"{branch_name}-{attempt}"
                subprocess.run(
                    ["git", "checkout", "-b", attempt_branch],
                    cwd=PROJECT_ROOT,
                    check=True,
                    capture_output=True,
                )

                # Invoke LLM provider to fix the error
                prompt = _build_fix_prompt(error_context, board)
                fix_result = _run_worker_subprocess(
                    provider_command_for_mode(
                        active_provider,
                        prompt,
                        mode="fix",
                    ),
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )

                if fix_result.returncode != 0:
                    log.warning("%s CLI exited %d on attempt %d", active_provider, fix_result.returncode, attempt)
                    _cleanup_fix_branch(attempt_branch, original_branch)
                    continue

                # Run tests to validate the fix
                test_result = _run_worker_subprocess(
                    _uv_python_cmd("-m", "pytest", "tests/", "-x", "-q"),
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    timeout=120,
                )

                if test_result.returncode == 0:
                    # Tests pass — merge the fix
                    subprocess.run(
                        ["git", "checkout", original_branch],
                        cwd=PROJECT_ROOT,
                        check=True,
                        capture_output=True,
                    )
                    subprocess.run(
                        ["git", "merge", "--no-edit", attempt_branch],
                        cwd=PROJECT_ROOT,
                        check=True,
                        capture_output=True,
                    )
                    log.info("Auto-fix applied from branch %s", attempt_branch)
                    return True
                log.warning("Tests failed after auto-fix attempt %d", attempt)
                _cleanup_fix_branch(attempt_branch, original_branch)

            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
                log.warning("Auto-fix attempt %d failed: %s", attempt, exc)
                _cleanup_fix_branch(attempt_branch, original_branch)
                continue

        return False
    finally:
        # Always return to original branch, even if killed mid-fix
        current = _git_current_branch()
        if current and current != original_branch:
            log.info("auto_fix cleanup: switching from %s back to %s", current, original_branch)
            subprocess.run(
                ["git", "checkout", original_branch],
                cwd=PROJECT_ROOT,
                capture_output=True,
            )


# ── Internal Helpers ─────────────────────────────────────────────────────────


def _build_fix_prompt(error_context: dict, board: str) -> str:
    """Build a prompt for the configured provider CLI to fix a submission error."""
    return (
        f"The autofill script for {board} failed with exit code {error_context.get('exit_code')}. "
        f"The job URL is {error_context.get('url')}. "
        f"Output directory: {error_context.get('output_dir')}. "
        f"Please diagnose the error by reading the autofill script and recent logs, "
        f"then fix the issue. Focus on scripts/autofill_{board}.py. "
        f"Do not run tests or git commands yourself."
    )


def _git_current_branch() -> str | None:
    """Return current git branch name, or None if not in a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _cleanup_fix_branch(branch: str, original_branch: str) -> None:
    """Switch back to original branch and delete the fix branch."""
    try:
        subprocess.run(
            ["git", "checkout", original_branch],
            cwd=PROJECT_ROOT,
            capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=PROJECT_ROOT,
            capture_output=True,
        )
    except Exception:
        pass


def _detect_board_from_url(url: str) -> str:
    """Detect the job board from a URL. Returns board name or 'unknown'."""
    try:
        from submit_application import _board_for_url

        return _board_for_url(url)
    except (ImportError, ValueError):
        pass
    # Minimal fallback detection
    url_lower = url.lower()
    board_patterns = {
        "greenhouse": ("greenhouse.io",),
        "ashby": ("ashbyhq.com",),
        "lever": ("lever.co",),
        "workday": ("myworkdayjobs.com", "myworkdaysite.com"),
        "dover": ("app.dover.com",),
        "icims": ("icims.com",),
        "linkedin": ("linkedin.com/jobs/view",),
    }
    for board, patterns in board_patterns.items():
        if any(p in url_lower for p in patterns):
            return board
    return "unknown"


def _detect_board_from_submit_dir(submit_dir: Path) -> str | None:
    """Detect actual board from autofill artifacts in the submit directory."""
    for f in submit_dir.iterdir():
        if f.name.endswith("_autofill_report.json"):
            return f.name.removesuffix("_autofill_report.json")
    return None


def _clear_cached_answers(output_dir: Path) -> None:
    """Delete cached application answers so they're regenerated fresh."""
    from application_submit_common import clear_answer_generation_artifacts

    clear_answer_generation_artifacts(output_dir)


def _discover_output_dir(url: str) -> Path | None:
    """Try to find the output directory for a given URL.

    Looks for the most recently modified .pipeline_meta.json that references
    this URL.
    """
    output_root = PROJECT_ROOT / "output"
    if not output_root.is_dir():
        return None

    candidates: list[tuple[float, Path]] = []
    for meta_path in output_root.rglob(".pipeline_meta.json"):
        try:
            meta = json.loads(meta_path.read_text())
            meta_urls = {
                meta.get("jd_source_resolved") or "",
                meta.get("jd_source") or "",
            }
            if url in meta_urls:
                candidates.append((meta_path.stat().st_mtime, meta_path.parent))
        except (json.JSONDecodeError, OSError):
            continue

    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return None


def _find_payload_path(output_dir: Path | str, board: str) -> Path | None:
    """Find the autofill payload JSON in the output directory."""
    output_dir = Path(output_dir)
    # Look in submit/ subdirectory first, then output_dir itself
    for parent in [output_dir / "submit", output_dir]:
        payload = parent / f"autofill_payload_{board}.json"
        if payload.is_file():
            return payload
        # Generic payload name
        payload = parent / "autofill_payload.json"
        if payload.is_file():
            return payload
    return None


def _load_submit_confirmation_payloads(output_dir: Path | str | None) -> list[dict]:
    from output_layout import preferred_submit_dir_name_for_post_submit

    if not output_dir:
        return []
    submit_name = preferred_submit_dir_name_for_post_submit(output_dir) or "submit"
    submit_dir = Path(output_dir) / submit_name
    payloads: list[dict] = []
    for name in (
        "application_confirmation_website.json",
        "application_submission_result.json",
        "notion_sync_status.json",
    ):
        path = submit_dir / name
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _confirmed_submission_timestamp(output_dir: Path | str | None) -> str | None:
    for payload in _load_submit_confirmation_payloads(output_dir):
        confirmed_at = payload.get("confirmed_at_utc") or payload.get("confirmed_at")
        if payload.get("website_confirmed") or payload.get("status") == "synced":
            value = str(confirmed_at or "").strip()
            return value or None
    return None


def _has_confirmed_submission_artifact(output_dir: Path | str | None) -> bool:
    for payload in _load_submit_confirmation_payloads(output_dir):
        if payload.get("website_confirmed") or payload.get("status") == "synced":
            return True
    return False


def _finalize_successful_submission(conn, job_id: int, output_dir: Path | str | None, board_url: str) -> str:
    from job_db import record_confirmed_submission, update_status

    _post_submit(conn, job_id, output_dir, board_url)
    confirmed_at = _confirmed_submission_timestamp(output_dir)
    if not _has_confirmed_submission_artifact(output_dir):
        update_status(
            conn,
            job_id,
            "stopped",
            error_message="Submit clicked but no confirmation detected — verify manually",
            failure_type="submit_failed",
        )
        log_event(
            conn,
            job_id,
            "submit_unconfirmed",
            detail="No website or email confirmation after submit",
            initiator="worker",
        )
        return "stopped"

    record_confirmed_submission(conn, job_id, confirmed_at=confirmed_at, initiator="worker")
    log_event(conn, job_id, "submitted", initiator="worker")
    return "submitted"


def _post_submit(
    conn: sqlite3.Connection,
    job_id: int,
    output_dir: Path | str | None,
    board_url: str,
) -> None:
    """Phase 6: Post-submit actions (Notion sync + email reply).

    Best-effort — failures are logged but don't change job status.
    """
    from job_db import end_phase, log_event, start_phase

    if not output_dir:
        return
    output_dir = Path(output_dir)
    if not output_dir:
        return

    _p = start_phase(conn, job_id, "post_submit")

    board = _detect_board_from_url(board_url)

    _notion_ok = False
    try:
        from application_submit_common import sync_notion_after_submit

        # Build a minimal payload dict for the sync function
        payload = {"out_dir": str(output_dir)}
        outcome = {"status": "confirmed", "website_confirmed": True}
        sync_result = sync_notion_after_submit(payload, outcome, provider=board)
        notion_url = None
        if isinstance(sync_result, dict):
            notion_url = sync_result.get("notion_url")
        if notion_url:
            conn.execute(
                "UPDATE jobs SET notion_url = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (notion_url, job_id)
            )
            conn.commit()
        log_event(conn, job_id, "notion_synced", detail_json={"notion_url": notion_url}, initiator="worker")
        _notion_ok = True
    except Exception as exc:
        log.warning("Notion sync failed for job %d: %s", job_id, exc)
        log_event(conn, job_id, "notion_sync_failed", detail=str(exc), initiator="worker")
        # Check if Notion was already synced by another process
        from output_layout import active_submit_dir_name as _asd_ns

        _ns_dir = output_dir / (_asd_ns(output_dir) or "submit")
        _ns_path = _ns_dir / "notion_sync_status.json"
        if _ns_path.exists():
            try:
                _ns_data = json.loads(_ns_path.read_text(encoding="utf-8"))
                if _ns_data.get("status") == "synced":
                    _notion_ok = True
                    log.info("job %d: Notion already synced by another process", job_id)
            except Exception:
                pass

    _email_ok = False
    try:
        from application_submit_common import send_confirmation_email_reply

        payload = {"out_dir": str(output_dir)}
        reply_result = send_confirmation_email_reply(payload, board_name=board, caller="worker_post_submit")
        reply_status = str(reply_result.get("status") or "")
        if reply_status == "sent":
            log_event(conn, job_id, "email_reply_sent", initiator="worker")
        elif reply_status == "skipped_duplicate":
            log_event(
                conn,
                job_id,
                "email_reply_skipped_duplicate",
                detail_json={
                    "reason": reply_result.get("reason"),
                    "submit_dir": reply_result.get("submit_dir"),
                    "state_path": reply_result.get("state_path"),
                },
                initiator="worker",
            )
        _email_ok = True  # No error even if not sent (email is best-effort)
    except Exception as exc:
        log.warning("Email reply failed for job %d: %s", job_id, exc)
        log_event(conn, job_id, "email_reply_failed", detail=str(exc), initiator="worker")

    # Mark as applied on LinkedIn only after all downstream actions confirmed
    if _notion_ok and _email_ok:
        try:
            from job_db import get_job

            job = get_job(conn, job_id)
            source_url = (job or {}).get("source_url") or (job or {}).get("url", "")
            if "linkedin.com" in source_url:
                from url_resolver import dismiss_linkedin_job_recommendation, mark_linkedin_job_applied

                marked = True
                if board != "linkedin":
                    marked = mark_linkedin_job_applied(source_url)
                    if marked:
                        log_event(
                            conn, job_id, "linkedin_marked_applied", detail_json={"url": source_url}, initiator="worker"
                        )
                        log.info("Marked job %d as applied on LinkedIn", job_id)
                    else:
                        log_event(
                            conn,
                            job_id,
                            "linkedin_mark_failed",
                            detail="Could not mark as applied on LinkedIn",
                            initiator="worker",
                        )
                if marked:
                    dismissed = dismiss_linkedin_job_recommendation(source_url)
                    if dismissed:
                        log_event(
                            conn, job_id, "linkedin_dismissed", detail_json={"url": source_url}, initiator="worker"
                        )
                        log.info("Dismissed LinkedIn job %d from recommendations", job_id)
                    else:
                        log_event(
                            conn,
                            job_id,
                            "linkedin_dismiss_failed",
                            detail="Could not hide job from LinkedIn recommendations",
                            initiator="worker",
                        )
        except Exception as exc:
            log.warning("LinkedIn mark-applied failed for job %d: %s", job_id, exc)
            log_event(conn, job_id, "linkedin_mark_failed", detail=str(exc), initiator="worker")
    else:
        log.info(
            "job %d: skipping LinkedIn mark-applied — downstream actions incomplete (notion=%s, email=%s)",
            job_id,
            _notion_ok,
            _email_ok,
        )
        log_event(
            conn,
            job_id,
            "linkedin_mark_deferred",
            detail=f"notion_ok={_notion_ok}, email_ok={_email_ok}",
            initiator="worker",
        )

    end_phase(conn, _p, exit_code=0)
