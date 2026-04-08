#!/usr/bin/env python3
"""Reconcile top-level answer state sidecars from the current submit proof."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from answer_refresh_state import (
    STATUS_FRESH as ANSWER_REFRESH_FRESH,
)
from answer_refresh_state import (
    STATUS_NOT_APPLICABLE as ANSWER_REFRESH_NOT_APPLICABLE,
)
from answer_refresh_state import (
    STATUS_PENDING as ANSWER_REFRESH_PENDING,
)
from answer_refresh_state import (
    load_answer_refresh_state,
    sync_answer_refresh_state_from_current_proof,
)
from answer_verification_state import (
    STATUS_BLOCKED as ANSWER_VERIFICATION_BLOCKED,
)
from answer_verification_state import (
    STATUS_FAILED as ANSWER_VERIFICATION_FAILED,
)
from answer_verification_state import (
    STATUS_NOT_APPLICABLE as ANSWER_VERIFICATION_NOT_APPLICABLE,
)
from answer_verification_state import (
    STATUS_PENDING as ANSWER_VERIFICATION_PENDING,
)
from answer_verification_state import (
    STATUS_VERIFIED as ANSWER_VERIFICATION_VERIFIED,
)
from answer_verification_state import (
    load_answer_verification_state,
    sync_answer_verification_state_from_current_proof,
)
from output_layout import ANSWER_VERIFICATION_JSON, APPLICATION_ANSWER_CACHE, role_submit_dir

_FINAL_VERIFICATION_STATUSES = {
    ANSWER_VERIFICATION_VERIFIED,
    ANSWER_VERIFICATION_NOT_APPLICABLE,
    ANSWER_VERIFICATION_BLOCKED,
    ANSWER_VERIFICATION_FAILED,
}


def _load_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _parse_autofill_report(report: dict) -> dict[str, int]:
    llm_generated = [
        field
        for field in report.get("fields", [])
        if isinstance(field, dict) and field.get("source") == "generated_application_answer"
    ]
    return {"llm_generated_count": len(llm_generated)}


def _answer_payload_field_names(payload: dict | None) -> set[str]:
    if not isinstance(payload, dict):
        return set()
    field_names: set[str] = set()
    questions = payload.get("questions")
    if isinstance(questions, list):
        for question in questions:
            if not isinstance(question, dict):
                continue
            field_name = str(question.get("field_name") or "").strip()
            if field_name:
                field_names.add(field_name)
    answers = payload.get("answers")
    if isinstance(answers, dict):
        for key in answers:
            field_name = str(key or "").strip()
            if field_name:
                field_names.add(field_name)
    return field_names


def _report_field_names(report: dict) -> set[str]:
    field_names: set[str] = set()
    for field in report.get("fields", []):
        if not isinstance(field, dict):
            continue
        field_name = str(field.get("field_name") or "").strip()
        if field_name:
            field_names.add(field_name)
    return field_names


def _field_answer_from_report(field: dict) -> object | None:
    selected_labels = field.get("selected_labels")
    if isinstance(selected_labels, list):
        normalized = [str(value).strip() for value in selected_labels if str(value).strip()]
        if normalized:
            return normalized
    value = field.get("value")
    if isinstance(value, list):
        normalized = [str(entry).strip() for entry in value if str(entry).strip()]
        return normalized or None
    if value is None:
        return None
    normalized_value = str(value).strip()
    return normalized_value or None


def _backfill_application_answers_from_report_if_needed(
    submit_dir: Path,
    *,
    report: dict | None,
    answers_payload: dict | None,
) -> dict | None:
    if answers_payload is not None:
        return answers_payload
    if not isinstance(report, dict):
        return None

    questions: list[dict[str, object]] = []
    answers: dict[str, object] = {}
    for field in list(report.get("fields") or []):
        if not isinstance(field, dict):
            continue
        field_name = str(field.get("field_name") or "").strip()
        label = str(field.get("label") or field_name).strip()
        if not field_name or not label:
            continue
        answer_value = _field_answer_from_report(field)
        if answer_value is None:
            continue
        question_type = str(field.get("field_type") or field.get("kind") or "text").strip() or "text"
        questions.append(
            {
                "field_name": field_name,
                "label": label,
                "required": bool(field.get("required")),
                "type": question_type,
            }
        )
        answers[field_name] = answer_value

    if not questions:
        return None

    payload = {
        "generated_at_utc": str(report.get("generated_at_utc") or "").strip() or None,
        "provider": "current_proof_backfill",
        "refresh_request_id": None,
        "questions": questions,
        "answers": answers,
    }
    (submit_dir / APPLICATION_ANSWER_CACHE).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def _load_current_attempt_autofill_report(
    submit_dir: Path,
    *,
    answers_payload: dict | None = None,
) -> tuple[dict | None, dict | None]:
    candidates: list[tuple[Path, dict]] = []
    for report_path in sorted(submit_dir.glob("*_autofill_report.json")):
        payload = _load_json(report_path)
        if payload is not None:
            candidates.append((report_path, payload))
    if not candidates:
        return None, None

    selected_payload = candidates[0][1]
    answer_field_names = _answer_payload_field_names(answers_payload)
    if answer_field_names:
        selected_overlap = len(_report_field_names(selected_payload) & answer_field_names)
        for _, payload in candidates[1:]:
            overlap = len(_report_field_names(payload) & answer_field_names)
            if overlap > selected_overlap:
                selected_payload = payload
                selected_overlap = overlap
    return selected_payload, _parse_autofill_report(selected_payload)


def _load_answer_refresh_proof(submit_dir: Path) -> dict | None:
    payload = _load_json(submit_dir / APPLICATION_ANSWER_CACHE)
    if payload is None:
        return None
    return {
        "request_id": payload.get("refresh_request_id"),
        "provider": payload.get("provider"),
        "generated_at_utc": payload.get("generated_at_utc"),
    }


def _load_answer_verification_artifact(submit_dir: Path) -> dict | None:
    return _load_json(submit_dir / ANSWER_VERIFICATION_JSON)


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _verification_message(status: str, *, retry_count: int = 0, blocked_count: int = 0) -> str:
    if status == ANSWER_VERIFICATION_VERIFIED:
        return "Generated answers passed reference-guided verification."
    if status == ANSWER_VERIFICATION_NOT_APPLICABLE:
        return "No non-deterministic generated answers required verification."
    if status == ANSWER_VERIFICATION_BLOCKED:
        if retry_count > 0 and blocked_count == 0:
            return "Answer verification requested generator retry for one or more generated answers."
        return "Answer verification blocked one or more generated answers."
    return "Answer verification failed before proof was recorded."


def _refresh_state_matches(
    current: dict,
    *,
    status: str,
    reason: str,
    message: str,
    answer_provider: str | None,
    answer_generated_at_utc: str | None,
    generated_answer_count: int,
    proof_submit_dir: str,
) -> bool:
    return (
        current.get("status") == status
        and current.get("reason") == reason
        and current.get("message") == message
        and current.get("answer_provider") == answer_provider
        and current.get("answer_generated_at_utc") == answer_generated_at_utc
        and current.get("generated_answer_count") == generated_answer_count
        and current.get("proof_submit_dir") == proof_submit_dir
    )


def _verification_state_matches(
    current: dict,
    *,
    status: str,
    message: str,
    verifier_provider: str | None,
    verified_answer_count: int,
    blocked_answer_count: int,
    proof_submit_dir: str,
) -> bool:
    return (
        current.get("status") == status
        and current.get("message") == message
        and current.get("verifier_provider") == verifier_provider
        and current.get("verified_answer_count") == verified_answer_count
        and current.get("blocked_answer_count") == blocked_answer_count
        and current.get("proof_submit_dir") == proof_submit_dir
    )


def sync_current_attempt_answer_states_from_proof(
    out_dir: str | Path,
    submit_dirname: str | None = None,
    *,
    allow_pending_override: bool = False,
) -> dict[str, object]:
    out_path = Path(out_dir)
    resolved_submit_dirname = submit_dirname or role_submit_dir(out_path).name
    submit_dir = out_path / resolved_submit_dirname
    answers_payload = _load_json(submit_dir / APPLICATION_ANSWER_CACHE)
    report_payload, field_counts = _load_current_attempt_autofill_report(submit_dir, answers_payload=answers_payload)
    result: dict[str, object] = {
        "submit_dirname": resolved_submit_dirname,
        "report_found": field_counts is not None,
        "llm_generated_count": None if field_counts is None else field_counts["llm_generated_count"],
        "refresh_synced": False,
        "verification_synced": False,
    }
    if field_counts is None:
        return result

    llm_generated_count = field_counts["llm_generated_count"]
    if llm_generated_count == 0:
        answers_payload = _backfill_application_answers_from_report_if_needed(
            submit_dir,
            report=report_payload,
            answers_payload=answers_payload,
        )
    current_refresh = load_answer_refresh_state(out_path)
    current_verification = load_answer_verification_state(out_path)
    refresh_pending = current_refresh.get("status") == ANSWER_REFRESH_PENDING and not allow_pending_override
    verification_pending = (
        current_verification.get("status") == ANSWER_VERIFICATION_PENDING and not allow_pending_override
    )

    if llm_generated_count == 0:
        refresh_message = "No generated application answers were present for this draft."
        if not refresh_pending and not _refresh_state_matches(
            current_refresh,
            status=ANSWER_REFRESH_NOT_APPLICABLE,
            reason="no_generated_answers",
            message=refresh_message,
            answer_provider=None,
            answer_generated_at_utc=None,
            generated_answer_count=0,
            proof_submit_dir=resolved_submit_dirname,
        ):
            sync_answer_refresh_state_from_current_proof(
                out_path,
                status=ANSWER_REFRESH_NOT_APPLICABLE,
                reason="no_generated_answers",
                message=refresh_message,
                generated_answer_count=0,
                proof_submit_dir=resolved_submit_dirname,
            )
            result["refresh_synced"] = True
        verification_message = "No non-deterministic generated answers required verification."
        if not verification_pending and not _verification_state_matches(
            current_verification,
            status=ANSWER_VERIFICATION_NOT_APPLICABLE,
            message=verification_message,
            verifier_provider=None,
            verified_answer_count=0,
            blocked_answer_count=0,
            proof_submit_dir=resolved_submit_dirname,
        ):
            sync_answer_verification_state_from_current_proof(
                out_path,
                status=ANSWER_VERIFICATION_NOT_APPLICABLE,
                reason="no_generated_answers",
                message=verification_message,
                verified_answer_count=0,
                blocked_answer_count=0,
                proof_submit_dir=resolved_submit_dirname,
            )
            result["verification_synced"] = True
        return result

    refresh_proof = _load_answer_refresh_proof(submit_dir)
    if refresh_proof is not None and not refresh_pending:
        refresh_message = "Fresh answer generation proof recorded."
        if not _refresh_state_matches(
            current_refresh,
            status=ANSWER_REFRESH_FRESH,
            reason="fresh_proof_recorded",
            message=refresh_message,
            answer_provider=str(refresh_proof.get("provider") or "").strip() or None,
            answer_generated_at_utc=str(refresh_proof.get("generated_at_utc") or "").strip() or None,
            generated_answer_count=llm_generated_count,
            proof_submit_dir=resolved_submit_dirname,
        ):
            sync_answer_refresh_state_from_current_proof(
                out_path,
                status=ANSWER_REFRESH_FRESH,
                reason="fresh_proof_recorded",
                message=refresh_message,
                answer_provider=str(refresh_proof.get("provider") or "").strip() or None,
                answer_generated_at_utc=str(refresh_proof.get("generated_at_utc") or "").strip() or None,
                generated_answer_count=llm_generated_count,
                proof_submit_dir=resolved_submit_dirname,
            )
            result["refresh_synced"] = True

    verification_artifact = _load_answer_verification_artifact(submit_dir)
    verification_status = str((verification_artifact or {}).get("status") or "").strip()
    if (
        verification_artifact is not None
        and verification_status in _FINAL_VERIFICATION_STATUSES
        and not verification_pending
    ):
        summary = verification_artifact.get("summary") if isinstance(verification_artifact.get("summary"), dict) else {}
        approved_count = _safe_int(summary.get("approved_count"))
        retry_count = _safe_int(summary.get("retry_count"))
        blocked_count = _safe_int(summary.get("blocked_count"))
        not_applicable_count = _safe_int(summary.get("not_applicable_count"))
        if verification_status == ANSWER_VERIFICATION_NOT_APPLICABLE and approved_count == 0 and blocked_count == 0:
            approved_count = max(llm_generated_count - not_applicable_count, 0)
        verification_message = _verification_message(
            verification_status,
            retry_count=retry_count,
            blocked_count=blocked_count,
        )
        verifier_provider = str(verification_artifact.get("verifier_provider") or "").strip() or None
        blocked_answer_count = blocked_count + retry_count
        if not _verification_state_matches(
            current_verification,
            status=verification_status,
            message=verification_message,
            verifier_provider=verifier_provider,
            verified_answer_count=approved_count,
            blocked_answer_count=blocked_answer_count,
            proof_submit_dir=resolved_submit_dirname,
        ):
            sync_answer_verification_state_from_current_proof(
                out_path,
                status=verification_status,
                message=verification_message,
                verifier_provider=verifier_provider,
                verified_answer_count=approved_count,
                blocked_answer_count=blocked_answer_count,
                proof_submit_dir=resolved_submit_dirname,
            )
            result["verification_synced"] = True

    return result
