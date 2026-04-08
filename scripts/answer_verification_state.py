#!/usr/bin/env python3
"""Durable state helpers for answer verification proof."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from output_layout import ANSWER_VERIFICATION_JSON, role_submit_dir, role_submit_path

ANSWER_VERIFICATION_STATUS_JSON = "answer_verification_status.json"
ANSWER_VERIFICATION_STATE_VERSION = 1

STATUS_UNKNOWN = "unknown"
STATUS_PENDING = "pending"
STATUS_VERIFIED = "verified"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"

VALID_STATUSES = {
    STATUS_UNKNOWN,
    STATUS_PENDING,
    STATUS_VERIFIED,
    STATUS_NOT_APPLICABLE,
    STATUS_BLOCKED,
    STATUS_FAILED,
}


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def answer_verification_status_path(out_dir: str | Path) -> Path:
    return Path(out_dir) / ANSWER_VERIFICATION_STATUS_JSON


def _default_state() -> dict:
    return {
        "version": ANSWER_VERIFICATION_STATE_VERSION,
        "status": STATUS_UNKNOWN,
        "request_id": None,
        "requested_at_utc": None,
        "resolved_at_utc": None,
        "reason": None,
        "message": None,
        "verifier_provider": None,
        "verified_answer_count": None,
        "blocked_answer_count": None,
        "proof_submit_dir": None,
    }


def _write_state(out_dir: str | Path, state: dict) -> dict:
    path = answer_verification_status_path(out_dir)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return state


def load_answer_verification_state(out_dir: str | Path) -> dict:
    path = answer_verification_status_path(out_dir)
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


def mark_answer_verification_pending(out_dir: str | Path, *, message: str | None = None) -> dict:
    state = _default_state()
    state.update(
        {
            "status": STATUS_PENDING,
            "request_id": uuid4().hex,
            "requested_at_utc": _utc_now_iso(),
            "message": message or "Waiting for answer verification proof.",
        }
    )
    return _write_state(out_dir, state)


def current_answer_verification_request_id(out_dir: str | Path) -> str | None:
    state = load_answer_verification_state(out_dir)
    request_id = state.get("request_id")
    if state.get("status") != STATUS_PENDING or not isinstance(request_id, str) or not request_id:
        return None
    return request_id


def finalize_answer_verification(
    out_dir: str | Path,
    *,
    request_id: str,
    status: str,
    reason: str | None = None,
    message: str | None = None,
    verifier_provider: str | None = None,
    verified_answer_count: int | None = None,
    blocked_answer_count: int | None = None,
    proof_submit_dir: str | None = None,
) -> dict:
    current = load_answer_verification_state(out_dir)
    if not request_id or current.get("request_id") != request_id:
        return current
    if status not in VALID_STATUSES - {STATUS_UNKNOWN, STATUS_PENDING}:
        raise ValueError(f"Invalid final answer verification status: {status}")
    current.update(
        {
            "status": status,
            "resolved_at_utc": _utc_now_iso(),
            "reason": reason,
            "message": message or current.get("message"),
            "verifier_provider": verifier_provider,
            "verified_answer_count": verified_answer_count,
            "blocked_answer_count": blocked_answer_count,
            "proof_submit_dir": proof_submit_dir,
        }
    )
    return _write_state(out_dir, current)


def sync_answer_verification_state_from_current_proof(
    out_dir: str | Path,
    *,
    status: str,
    reason: str | None = None,
    message: str | None = None,
    verifier_provider: str | None = None,
    verified_answer_count: int | None = None,
    blocked_answer_count: int | None = None,
    proof_submit_dir: str | None = None,
) -> dict:
    if status not in VALID_STATUSES - {STATUS_UNKNOWN, STATUS_PENDING}:
        raise ValueError(f"Invalid synced answer verification status: {status}")
    current = load_answer_verification_state(out_dir)
    current.update(
        {
            "status": status,
            "requested_at_utc": current.get("requested_at_utc") or _utc_now_iso(),
            "resolved_at_utc": _utc_now_iso(),
            "reason": reason,
            "message": message or current.get("message"),
            "verifier_provider": verifier_provider,
            "verified_answer_count": verified_answer_count,
            "blocked_answer_count": blocked_answer_count,
            "proof_submit_dir": proof_submit_dir,
        }
    )
    return _write_state(out_dir, current)


def load_answer_verification_artifact_proof(out_dir: str | Path) -> dict | None:
    proof_path = role_submit_path(out_dir, ANSWER_VERIFICATION_JSON)
    if not proof_path.exists():
        return None
    try:
        payload = json.loads(proof_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return {
        "request_id": payload.get("request_id"),
        "verifier_provider": payload.get("verifier_provider"),
        "generated_at_utc": payload.get("generated_at_utc"),
        "status": payload.get("status"),
        "submit_dir": role_submit_dir(out_dir).name,
    }
