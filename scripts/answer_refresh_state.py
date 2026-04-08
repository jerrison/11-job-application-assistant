#!/usr/bin/env python3
"""Durable state helpers for explicit answer refresh requests."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from output_layout import APPLICATION_ANSWER_CACHE, role_submit_dir, role_submit_path

ANSWER_REFRESH_STATUS_JSON = "answer_refresh_status.json"
ANSWER_REFRESH_STATE_VERSION = 1

STATUS_UNKNOWN = "unknown"
STATUS_PENDING = "pending"
STATUS_FRESH = "fresh"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_FAILED = "failed"

VALID_STATUSES = {
    STATUS_UNKNOWN,
    STATUS_PENDING,
    STATUS_FRESH,
    STATUS_NOT_APPLICABLE,
    STATUS_FAILED,
}


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def answer_refresh_status_path(out_dir: str | Path) -> Path:
    return Path(out_dir) / ANSWER_REFRESH_STATUS_JSON


def _default_state() -> dict:
    return {
        "version": ANSWER_REFRESH_STATE_VERSION,
        "status": STATUS_UNKNOWN,
        "request_id": None,
        "request_kind": None,
        "requested_at_utc": None,
        "resolved_at_utc": None,
        "answer_provider": None,
        "answer_generated_at_utc": None,
        "generated_answer_count": None,
        "reason": None,
        "message": None,
        "proof_submit_dir": None,
    }


def _write_state(out_dir: str | Path, state: dict) -> dict:
    path = answer_refresh_status_path(out_dir)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return state


def load_answer_refresh_state(out_dir: str | Path) -> dict:
    path = answer_refresh_status_path(out_dir)
    state = _default_state()
    if not path.exists():
        return state
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return state
    if not isinstance(payload, dict):
        return state
    for key in state:
        if key in payload:
            state[key] = payload[key]
    if state["status"] not in VALID_STATUSES:
        state["status"] = STATUS_UNKNOWN
    return state


def mark_answer_refresh_pending(
    out_dir: str | Path,
    *,
    request_kind: str,
    message: str | None = None,
) -> dict:
    state = _default_state()
    state.update(
        {
            "status": STATUS_PENDING,
            "request_id": uuid4().hex,
            "request_kind": request_kind,
            "requested_at_utc": _utc_now_iso(),
            "message": message or "Waiting for fresh answer generation proof.",
        }
    )
    return _write_state(out_dir, state)


def has_pending_answer_refresh(out_dir: str | Path) -> bool:
    state = load_answer_refresh_state(out_dir)
    return state["status"] == STATUS_PENDING and bool(state["request_id"])


def current_answer_refresh_request_id(out_dir: str | Path) -> str | None:
    state = load_answer_refresh_state(out_dir)
    request_id = state.get("request_id")
    if state.get("status") != STATUS_PENDING or not isinstance(request_id, str) or not request_id:
        return None
    return request_id


def finalize_answer_refresh(
    out_dir: str | Path,
    *,
    request_id: str,
    status: str,
    reason: str | None = None,
    message: str | None = None,
    answer_provider: str | None = None,
    answer_generated_at_utc: str | None = None,
    generated_answer_count: int | None = None,
    proof_submit_dir: str | None = None,
) -> dict:
    current = load_answer_refresh_state(out_dir)
    if not request_id or current.get("request_id") != request_id:
        return current
    if status not in VALID_STATUSES - {STATUS_UNKNOWN, STATUS_PENDING}:
        raise ValueError(f"Invalid final answer refresh status: {status}")
    current.update(
        {
            "status": status,
            "resolved_at_utc": _utc_now_iso(),
            "reason": reason,
            "message": message or current.get("message"),
            "answer_provider": answer_provider,
            "answer_generated_at_utc": answer_generated_at_utc,
            "generated_answer_count": generated_answer_count,
            "proof_submit_dir": proof_submit_dir,
        }
    )
    return _write_state(out_dir, current)


def sync_answer_refresh_state_from_current_proof(
    out_dir: str | Path,
    *,
    status: str,
    reason: str | None = None,
    message: str | None = None,
    answer_provider: str | None = None,
    answer_generated_at_utc: str | None = None,
    generated_answer_count: int | None = None,
    proof_submit_dir: str | None = None,
) -> dict:
    if status not in VALID_STATUSES - {STATUS_UNKNOWN, STATUS_PENDING}:
        raise ValueError(f"Invalid synced answer refresh status: {status}")
    current = load_answer_refresh_state(out_dir)
    current.update(
        {
            "status": status,
            "requested_at_utc": current.get("requested_at_utc") or _utc_now_iso(),
            "resolved_at_utc": _utc_now_iso(),
            "reason": reason,
            "message": message or current.get("message"),
            "answer_provider": answer_provider,
            "answer_generated_at_utc": answer_generated_at_utc,
            "generated_answer_count": generated_answer_count,
            "proof_submit_dir": proof_submit_dir,
        }
    )
    return _write_state(out_dir, current)


def fail_pending_answer_refresh(
    out_dir: str | Path,
    *,
    reason: str,
    message: str,
) -> dict:
    current = load_answer_refresh_state(out_dir)
    request_id = current.get("request_id")
    if current.get("status") != STATUS_PENDING or not isinstance(request_id, str) or not request_id:
        return current
    return finalize_answer_refresh(
        out_dir,
        request_id=request_id,
        status=STATUS_FAILED,
        reason=reason,
        message=message,
        answer_provider=current.get("answer_provider"),
        answer_generated_at_utc=current.get("answer_generated_at_utc"),
        generated_answer_count=current.get("generated_answer_count"),
        proof_submit_dir=current.get("proof_submit_dir"),
    )


def load_answer_refresh_artifact_proof(out_dir: str | Path) -> dict | None:
    answers_path = role_submit_path(out_dir, APPLICATION_ANSWER_CACHE)
    if not answers_path.exists():
        return None
    try:
        payload = json.loads(answers_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return {
        "request_id": payload.get("refresh_request_id"),
        "provider": payload.get("provider"),
        "generated_at_utc": payload.get("generated_at_utc"),
        "submit_dir": role_submit_dir(out_dir).name,
    }
