#!/usr/bin/env python3
"""Helpers for current-attempt Greenhouse failure artifacts."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from output_layout import SUBMISSION_RESULT_JSON, role_submit_path

GREENHOUSE_UNKNOWN_QUESTIONS_FAILURE = "greenhouse_unknown_questions"
GREENHOUSE_REVIEW_PROOF_GAP_FAILURE = "greenhouse_review_proof_gap"
GREENHOUSE_SUBMIT_NAVIGATION_MISSING_FAILURE = "greenhouse_submit_navigation_missing"
GREENHOUSE_SECURITY_CODE_UNRESOLVED_FAILURE = "greenhouse_security_code_unresolved"
GREENHOUSE_SUBMIT_VALIDATION_FAILURE = "greenhouse_submit_validation"
GREENHOUSE_SUBMIT_NOT_CONFIRMED_FAILURE = "greenhouse_submit_not_confirmed"
GREENHOUSE_RUNTIME_FAILURE = "greenhouse_runtime_error"

_GREENHOUSE_FAILURE_ARTIFACT_KEYS = (
    "report_markdown",
    "report_json",
    "pre_submit_screenshot",
    "review_screenshot",
    "page_screenshots_dir",
    "unknown_questions_json",
    "submit_debug_html",
    "submit_debug_screenshot",
)


def greenhouse_submission_result_path(out_dir: str | Path, payload: dict | None = None) -> Path:
    artifacts = payload.get("artifacts") if isinstance(payload, dict) else None
    raw_path = str((artifacts or {}).get("submission_result_json") or "").strip()
    return Path(raw_path) if raw_path else role_submit_path(out_dir, SUBMISSION_RESULT_JSON)


def clear_greenhouse_failure_artifacts(out_dir: str | Path, payload: dict) -> None:
    result_path = greenhouse_submission_result_path(out_dir, payload)
    if result_path.exists():
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if not isinstance(data, dict) or str(data.get("status") or "").strip().casefold() == "failed":
            try:
                result_path.unlink()
            except OSError:
                pass

    artifacts = payload.get("artifacts") or {}
    for key in ("unknown_questions_json", "submit_debug_html", "submit_debug_screenshot"):
        raw_path = str(artifacts.get(key) or "").strip()
        if not raw_path:
            continue
        try:
            Path(raw_path).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            continue


def write_greenhouse_failed_result(
    out_dir: str | Path,
    payload: dict,
    *,
    failure_type: str,
    message: str,
    current_page: str | None = None,
    page_index: int | None = None,
    validation_errors: list[str] | None = None,
    unknown_questions: list[dict] | None = None,
) -> Path:
    result = {
        "status": "failed",
        "board": "greenhouse",
        "provider": "greenhouse",
        "website_confirmed": False,
        "updated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "job_url": payload.get("job_url", ""),
        "company": payload.get("company", ""),
        "job_title": payload.get("job_title", ""),
        "failure_type": failure_type,
        "message": message,
    }
    if current_page:
        result["current_page"] = current_page
    if page_index is not None:
        result["page_index"] = page_index
    if validation_errors:
        result["validation_errors"] = list(dict.fromkeys(str(error).strip() for error in validation_errors if error))
    if unknown_questions:
        result["unknown_questions"] = unknown_questions

    artifacts: dict[str, str] = {}
    payload_artifacts = payload.get("artifacts") or {}
    for key in _GREENHOUSE_FAILURE_ARTIFACT_KEYS:
        raw_path = str(payload_artifacts.get(key) or "").strip()
        if not raw_path:
            continue
        path = Path(raw_path)
        if path.exists():
            artifacts[key] = str(path)
    if artifacts:
        result["artifacts"] = artifacts

    result_path = greenhouse_submission_result_path(out_dir, payload)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result_path
