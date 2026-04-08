"""Shared draft-proof helpers used by pipeline orchestration and review surfaces."""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from rendered_state_audit import DeterministicFieldExpectation, DeterministicFieldObservation

_GENERIC_CHECKBOX_AFFIRMATIVES = frozenset({"checked", "on", "selected", "true", "yes"})


@dataclass(frozen=True)
class RenderedAuditInputs:
    expected_fields: list[DeterministicFieldExpectation]
    observed_fields: list[DeterministicFieldObservation]
    deterministic_question_count: int
    screenshot_path: str
    has_answers_payload: bool


def _board_allows_empty_autofill_report(board_name: str | None) -> bool:
    return str(board_name or "").strip().casefold() in {"greenhouse"}


def _artifact_files_match(left: str | Path | None, right: str | Path | None) -> bool:
    if not left or not right:
        return False
    left_path = Path(left)
    right_path = Path(right)
    try:
        if left_path.resolve() == right_path.resolve():
            return True
    except OSError:
        if left_path == right_path:
            return True
    try:
        if left_path.stat().st_size != right_path.stat().st_size:
            return False
    except OSError:
        return False
    try:
        with left_path.open("rb") as left_file, right_path.open("rb") as right_file:
            left_digest = hashlib.file_digest(left_file, "sha256").digest()
            right_digest = hashlib.file_digest(right_file, "sha256").digest()
    except OSError:
        return False
    return left_digest == right_digest


def _artifact_paths_match(left: str | Path | None, right: str | Path | None) -> bool:
    if not left or not right:
        return False
    left_path = Path(left)
    right_path = Path(right)
    try:
        return left_path.resolve() == right_path.resolve()
    except OSError:
        return left_path == right_path


def _load_report_payload(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_application_answers_payload(path: Path | None) -> tuple[dict, bool]:
    if path is None:
        return {}, False
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, False
    if not isinstance(payload, dict):
        return {}, False
    return payload, True


def _iter_application_answer_rows(payload: dict) -> list[dict]:
    raw_answers = payload.get("answers")
    if isinstance(raw_answers, list):
        return [row for row in raw_answers if isinstance(row, dict)]
    if not isinstance(raw_answers, dict):
        return []

    rows: list[dict] = []
    for question in list(payload.get("questions") or []):
        if not isinstance(question, dict):
            continue
        field_name = str(question.get("field_name") or "").strip()
        label = str(question.get("label") or "").strip()
        answer_key = field_name or label
        if not answer_key or answer_key not in raw_answers:
            continue
        row = dict(question)
        answer_value = raw_answers.get(answer_key)
        if isinstance(answer_value, list):
            row["selected_labels"] = answer_value
        else:
            row["value"] = answer_value
        rows.append(row)
    return rows


def _normalized_selected_labels(row: dict, *, value_keys: tuple[str, ...] = ("value",)) -> list[str]:
    def _coerce_sequence(raw_value: object) -> list[str] | None:
        if isinstance(raw_value, list):
            normalized = [str(value).strip() for value in raw_value if str(value).strip()]
            return normalized or None
        serialized = str(raw_value or "").strip()
        if not (serialized.startswith("[") and serialized.endswith("]")):
            return None
        for loader in (json.loads, ast.literal_eval):
            try:
                parsed = loader(serialized)
            except (SyntaxError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(parsed, list):
                normalized = [str(value).strip() for value in parsed if str(value).strip()]
                return normalized or None
        return None

    raw_selected = row.get("selected_labels")
    coerced_selected = _coerce_sequence(raw_selected)
    if coerced_selected is not None:
        return coerced_selected
    if raw_selected is not None:
        selected = str(raw_selected).strip()
        return [selected] if selected else []
    for value_key in value_keys:
        raw_value = row.get(value_key)
        coerced_value = _coerce_sequence(raw_value)
        if coerced_value is not None:
            return coerced_value
        if raw_value is None:
            continue
        fallback = str(raw_value).strip()
        if fallback:
            return [fallback]
    return []


def _normalized_option_type(raw_value: object) -> str:
    return "".join(ch for ch in str(raw_value or "").strip().casefold() if ch.isalnum())


def _normalized_field_identity(raw_value: object) -> str:
    return " ".join(str(raw_value or "").strip().casefold().split())


def _is_deterministic_option_type(raw_value: object) -> bool:
    normalized = _normalized_option_type(raw_value)
    return normalized in {
        "boolean",
        "checkbox",
        "checkboxgroup",
        "choice",
        "combobox",
        "multiselect",
        "multivaluemultiselect",
        "multivalueselect",
        "multivaluesingleselect",
        "radio",
        "select",
        "valueselect",
    }


def _collapse_observed_selected_labels(
    selected_labels: frozenset[str],
    *,
    option_type: object,
) -> frozenset[str]:
    normalized_type = _normalized_option_type(option_type)
    if normalized_type not in {"checkbox", "checkboxgroup"} or len(selected_labels) <= 1:
        return selected_labels
    filtered = frozenset(
        label for label in selected_labels if label and label.strip() and label.strip().casefold() not in _GENERIC_CHECKBOX_AFFIRMATIVES
    )
    return filtered or selected_labels


def _count_deterministic_application_answer_questions(payload: dict) -> int:
    raw_answers = payload.get("answers")
    if isinstance(raw_answers, list):
        return sum(
            1
            for row in raw_answers
            if isinstance(row, dict) and _is_deterministic_option_type(row.get("type"))
        )
    if not isinstance(raw_answers, dict):
        return 0
    return sum(
        1
        for question in list(payload.get("questions") or [])
        if isinstance(question, dict) and _is_deterministic_option_type(question.get("type"))
    )


def current_rendered_audit_inputs(
    output_dir: Path,
    *,
    board_name: str | None,
) -> RenderedAuditInputs:
    from application_submit_common import resolve_current_submit_artifacts

    resolved = resolve_current_submit_artifacts(output_dir, board_name=board_name)
    report_payload = _load_report_payload(resolved.get("report_json"))
    answers_payload, has_answers_payload = _load_application_answers_payload(resolved.get("application_answers_json"))
    answer_rows = {
        str(row.get("field_name") or row.get("label") or "").strip(): row
        for row in _iter_application_answer_rows(answers_payload)
        if isinstance(row, dict) and str(row.get("field_name") or row.get("label") or "").strip()
    }
    screenshot_path = str(resolved.get("review_screenshot") or resolved.get("pre_submit_screenshot") or "")
    expected_fields = [
        DeterministicFieldExpectation(
            field_key=key,
            label=str(row.get("label") or key),
            selected_labels=frozenset(selected_labels := _normalized_selected_labels(row)),
            exact_count=len(selected_labels),
        )
        for key, row in answer_rows.items()
        if _is_deterministic_option_type(row.get("type"))
    ]
    expected_identities = {
        normalized
        for field in expected_fields
        for normalized in (
            _normalized_field_identity(field.field_key),
            _normalized_field_identity(field.label),
        )
        if normalized
    }
    observed_by_identity: dict[str, DeterministicFieldObservation] = {}
    for field in list(report_payload.get("fields") or []):
        if not isinstance(field, dict):
            continue
        option_type = field.get("field_type")
        if not _is_deterministic_option_type(option_type):
            option_type = field.get("kind")
        field_key = str(field.get("field_name") or field.get("label") or "").strip()
        label = str(field.get("label") or field.get("field_name") or "").strip()
        matching_expected_identity = any(
            normalized in expected_identities
            for normalized in (_normalized_field_identity(field_key), _normalized_field_identity(label))
            if normalized
        )
        if not _is_deterministic_option_type(option_type) and not matching_expected_identity:
            continue
        identity = field_key or label
        if not identity:
            continue
        selected_labels = _collapse_observed_selected_labels(
            frozenset(_normalized_selected_labels(field, value_keys=("selected_value", "value"))),
            option_type=option_type,
        )
        existing = observed_by_identity.get(identity)
        if existing is None:
            observed_by_identity[identity] = DeterministicFieldObservation(
                field_key=field_key,
                label=label,
                selected_labels=selected_labels,
                screenshot_path=screenshot_path,
            )
            continue
        observed_by_identity[identity] = DeterministicFieldObservation(
            field_key=existing.field_key or field_key,
            label=existing.label or label,
            selected_labels=_collapse_observed_selected_labels(
                existing.selected_labels | selected_labels,
                option_type=option_type,
            ),
            screenshot_path=existing.screenshot_path or screenshot_path,
        )
    observed_fields = list(observed_by_identity.values())
    return RenderedAuditInputs(
        expected_fields=expected_fields,
        observed_fields=observed_fields,
        deterministic_question_count=_count_deterministic_application_answer_questions(answers_payload),
        screenshot_path=screenshot_path,
        has_answers_payload=has_answers_payload,
    )


def _load_job_unavailable_artifact(output_dir: str | Path | None) -> tuple[Path, dict] | None:
    if not output_dir:
        return None
    from output_layout import JOB_UNAVAILABLE_JSON, current_submit_dir_name_for_reads

    out_dir = Path(output_dir)
    submit_dir = out_dir / current_submit_dir_name_for_reads(out_dir)
    path = submit_dir / JOB_UNAVAILABLE_JSON
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return path, payload


def _mark_job_unavailable_and_archive(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    output_dir: str | Path | None,
    error_message: str,
    initiator: str = "worker",
) -> None:
    from job_db import get_job, log_event, update_status

    evidence = _load_job_unavailable_artifact(output_dir)
    detail_json = None
    detail = error_message
    if evidence is not None:
        evidence_path, evidence_payload = evidence
        detail_json = dict(evidence_payload)
        detail_json["artifact_path"] = str(evidence_path)
        detail = str(evidence_payload.get("message") or detail).strip() or error_message

    update_status(
        conn,
        job_id,
        "stopped",
        error_message=detail[:500],
        failure_type="job_closed",
        archived=True,
    )
    job = get_job(conn, job_id)
    source_url = str((job or {}).get("source_url") or (job or {}).get("url") or "").strip()
    if "linkedin.com" in source_url:
        try:
            from url_resolver import dismiss_linkedin_job_recommendation

            dismissed = dismiss_linkedin_job_recommendation(source_url)
            if dismissed:
                log_event(
                    conn,
                    job_id,
                    "linkedin_dismissed",
                    detail="Hidden closed job from LinkedIn recommendations.",
                    detail_json={"url": source_url, "reason": "job_closed"},
                    initiator=initiator,
                )
            else:
                log_event(
                    conn,
                    job_id,
                    "linkedin_dismiss_failed",
                    detail="Could not hide closed job from LinkedIn recommendations.",
                    detail_json={"url": source_url, "reason": "job_closed"},
                    initiator=initiator,
                )
        except Exception as exc:
            log_event(
                conn,
                job_id,
                "linkedin_dismiss_failed",
                detail=str(exc),
                detail_json={"url": source_url, "reason": "job_closed"},
                initiator=initiator,
            )
    log_event(
        conn,
        job_id,
        "job_unavailable_auto_archived",
        detail=detail[:500],
        detail_json=detail_json,
        initiator=initiator,
    )


def _dedupe_pending_user_input_questions(existing: list[dict], new_questions: list[dict]) -> list[dict]:
    merged = list(existing)
    seen = {
        (
            str(question.get("field_name") or "").strip(),
            str(question.get("label") or "").strip(),
            str(question.get("artifact_key") or "").strip(),
            str(question.get("blocker_kind") or "").strip(),
        )
        for question in merged
    }
    for question in new_questions:
        key = (
            str(question.get("field_name") or "").strip(),
            str(question.get("label") or "").strip(),
            str(question.get("artifact_key") or "").strip(),
            str(question.get("blocker_kind") or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(question)
    return merged


def _draft_proof_blocker_entries(
    output_dir: Path,
    *,
    board_name: str | None = None,
) -> tuple[dict[str, object], list[dict]]:
    from application_submit_common import (
        APPLICATION_PROFILE_PATH,
        parse_application_profile,
        resolve_current_submit_artifacts,
    )
    from autofill_common import (
        blocking_unconfirmed_report_entries,
        board_requires_distinct_review_screenshot,
        infer_unknown_question_blocker_metadata,
        required_artifact_blocker_step,
    )

    proof = resolve_current_submit_artifacts(output_dir, board_name=board_name)
    submit_dir = Path(proof["submit_dir"])
    resolved_board = str(proof.get("board_name") or board_name or "").strip().casefold()
    blockers: list[dict] = []
    if not resolved_board:
        return proof, blockers

    if proof.get("report_json") is None:
        expected_path = submit_dir / f"{resolved_board}_autofill_report.json"
        blockers.append(
            required_artifact_blocker_step(
                field_name="autofill_report",
                label="Current-attempt autofill report",
                source="draft_proof_contract",
                artifact_key="report_json",
                expected_path=expected_path,
                note="The current submit attempt did not produce the autofill report needed to review this draft.",
                reason="The current submit attempt is missing the required autofill report proof.",
            )
        )
    if proof.get("pre_submit_screenshot") is None:
        expected_path = submit_dir / f"{resolved_board}_autofill_pre_submit.png"
        blockers.append(
            required_artifact_blocker_step(
                field_name="pre_submit_screenshot",
                label="Current-attempt pre-submit screenshot",
                source="draft_proof_contract",
                artifact_key="pre_submit_screenshot",
                expected_path=expected_path,
                note="The current submit attempt did not retain the pre-submit screenshot the draft review relies on.",
                reason="The current submit attempt is missing the required pre-submit screenshot proof.",
            )
        )
    payload_artifacts = dict(proof.get("payload_artifacts") or {})
    raw_review_screenshot = str(payload_artifacts.get("review_screenshot") or "").strip()
    requires_review_screenshot = (
        board_requires_distinct_review_screenshot(resolved_board)
        or bool(raw_review_screenshot)
    )
    if proof.get("review_screenshot") is None and requires_review_screenshot:
        blockers.append(
            required_artifact_blocker_step(
                field_name="review_screenshot",
                label="Current-attempt review screenshot",
                source="draft_proof_contract",
                artifact_key="review_screenshot",
                expected_path=Path(raw_review_screenshot) if raw_review_screenshot else submit_dir / f"{resolved_board}_autofill_review.png",
                note="This board requires a distinct review screenshot before the draft can be treated as ready.",
                reason="The current submit attempt is missing the required review screenshot proof.",
            )
        )
    pre_submit_screenshot = proof.get("pre_submit_screenshot")
    review_screenshot = proof.get("review_screenshot")
    if requires_review_screenshot and pre_submit_screenshot is not None and review_screenshot is not None:
        same_checkpoint_artifact = _artifact_paths_match(pre_submit_screenshot, review_screenshot)
        if not same_checkpoint_artifact and board_requires_distinct_review_screenshot(resolved_board):
            same_checkpoint_artifact = _artifact_files_match(pre_submit_screenshot, review_screenshot)
        if same_checkpoint_artifact:
            blockers.append(
                required_artifact_blocker_step(
                    field_name="distinct_review_screenshot",
                    label="Distinct review screenshot proof",
                    source="draft_proof_contract",
                    artifact_key="review_screenshot",
                    expected_path=Path(review_screenshot),
                    note="The current submit attempt reused the same screenshot for both pre-submit and review checkpoints.",
                    reason="The current submit attempt reused one screenshot for multiple required proof checkpoints.",
                )
            )

    try:
        application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    except Exception:
        application_profile = None

    report_payload = _load_report_payload(Path(proof.get("report_json")) if proof.get("report_json") else None)
    report_fields = [field for field in list(report_payload.get("fields") or []) if isinstance(field, dict)]
    if (
        proof.get("report_json") is not None
        and not report_fields
        and not _board_allows_empty_autofill_report(resolved_board)
    ):
        blockers.append(
            required_artifact_blocker_step(
                field_name="autofill_report_fields",
                label="Field-level autofill proof",
                source="draft_proof_contract",
                artifact_key="report_json",
                expected_path=Path(proof["report_json"]),
                note=(
                    "The current submit attempt produced an empty autofill report, so the screenshot does not show "
                    "what the automation filled."
                ),
                reason=(
                    "The current submit attempt produced an empty autofill report, so draft review lacks field-level proof."
                ),
            )
        )
    report_entries: list[dict] = []
    for bucket in ("unknown_questions", "planned_but_unconfirmed_fields"):
        for raw_entry in list(report_payload.get(bucket) or []):
            if not isinstance(raw_entry, dict):
                continue
            entry = dict(raw_entry)
            if bucket == "unknown_questions" and application_profile is not None and not entry.get("blocks_draft_completion"):
                entry.update(
                    infer_unknown_question_blocker_metadata(
                        field_name=entry.get("field_name"),
                        label=entry.get("label"),
                        application_profile=application_profile,
                    )
                )
            if not str(entry.get("reason") or "").strip():
                label = str(entry.get("label") or entry.get("field_name") or "This field").strip()
                blocker_kind = str(entry.get("blocker_kind") or "").strip()
                if blocker_kind == "visible_self_id":
                    entry["reason"] = (
                        f"The current submit attempt left {label} unresolved even though it has a deterministic "
                        "self-identification answer."
                    )
                elif blocker_kind == "visible_profile_field":
                    entry["reason"] = (
                        f"The current submit attempt left {label} unresolved even though it has a deterministic "
                        "profile-backed answer."
                    )
                elif entry.get("required"):
                    entry["reason"] = f"The current submit attempt left required field {label} unresolved."
            report_entries.append(entry)

    blockers.extend(blocking_unconfirmed_report_entries(report_entries))
    return proof, blockers


def _historical_proof_dirs(output_dir: Path, *, board_name: str | None, active_submit_dirname: str) -> list[str]:
    from application_submit_common import resolve_current_submit_artifacts
    from output_layout import existing_submit_dirs

    historical: list[str] = []
    for submit_dir in existing_submit_dirs(output_dir):
        if submit_dir.name == active_submit_dirname:
            continue
        proof = resolve_current_submit_artifacts(output_dir, board_name=board_name, submit_dirname=submit_dir.name)
        if any(
            proof.get(key) is not None
            for key in (
                "report_json",
                "pre_submit_screenshot",
                "review_screenshot",
                "payload_json",
            )
        ):
            historical.append(submit_dir.name)
    return historical


def _current_submit_attempt_is_confirmed(submit_dir: str | Path | None) -> bool:
    if not submit_dir:
        return False
    result_path = Path(submit_dir) / "application_submission_result.json"
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    status = str(payload.get("status") or "").strip().casefold()
    if status in {"confirmed", "submitted"}:
        return True
    return bool(payload.get("website_confirmed"))


def _optional_review_note_count(report_path: str | Path | None) -> int:
    from application_submit_common import APPLICATION_PROFILE_PATH, parse_application_profile
    from autofill_common import infer_unknown_question_blocker_metadata

    payload = _load_report_payload(Path(report_path) if report_path else None)
    if not payload:
        return 0
    try:
        application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    except Exception:
        application_profile = None

    count = 0
    seen_fields: set[tuple[str, str]] = set()
    for raw_entry in list(payload.get("unknown_questions") or []):
        if not isinstance(raw_entry, dict):
            continue
        entry = dict(raw_entry)
        if application_profile is not None and not entry.get("blocks_draft_completion"):
            entry.update(
                infer_unknown_question_blocker_metadata(
                    field_name=entry.get("field_name"),
                    label=entry.get("label"),
                    application_profile=application_profile,
                )
            )
        if bool(entry.get("required")) or bool(entry.get("blocks_draft_completion")):
            continue
        field_name = str(entry.get("field_name") or "").strip()
        label = str(entry.get("label") or "").strip()
        key = (field_name, label)
        if key in seen_fields:
            continue
        seen_fields.add(key)
        count += 1

    for raw_entry in list(payload.get("planned_but_unconfirmed_fields") or []):
        if not isinstance(raw_entry, dict):
            continue
        if bool(raw_entry.get("required")) or bool(raw_entry.get("blocks_draft_completion")):
            continue
        if str(raw_entry.get("status") or "").strip().casefold() == "skipped_not_found":
            continue
        field_name = str(raw_entry.get("field_name") or "").strip()
        label = str(raw_entry.get("label") or "").strip()
        key = (field_name, label)
        if key in seen_fields:
            continue
        seen_fields.add(key)
        count += 1

    return count


def draft_review_state(
    output_dir: Path | None,
    *,
    board_name: str | None = None,
) -> dict[str, object]:
    from application_submit_common import load_pending_user_input_for_submit_attempt

    if output_dir is None:
        return {"state": "missing", "reason": "No output directory was recorded for this draft."}

    out_dir = Path(output_dir)
    unavailable = _load_job_unavailable_artifact(out_dir)
    if unavailable is not None:
        _, payload = unavailable
        return {
            "state": "unavailable",
            "reason": str(payload.get("message") or "The application is explicitly unavailable.").strip(),
        }

    proof, blockers = _draft_proof_blocker_entries(out_dir, board_name=board_name)
    optional_review_note_count = _optional_review_note_count(proof.get("report_json"))
    pending = load_pending_user_input_for_submit_attempt(out_dir)
    pending_payload = pending[1] if pending is not None else None
    historical_dirs = _historical_proof_dirs(
        out_dir,
        board_name=proof.get("board_name") or board_name,
        active_submit_dirname=str(proof.get("submit_dirname") or "submit"),
    )
    artifact_sources = dict(proof.get("artifact_sources") or {})
    legacy_active = any(source == "legacy_default" for source in artifact_sources.values())
    duplicate_review_only = bool(blockers) and all(
        str(blocker.get("field_name") or "").strip() == "distinct_review_screenshot"
        for blocker in blockers
    )

    if legacy_active:
        return {
            "state": "legacy",
            "reason": "This draft only has legacy submit artifacts and does not satisfy the current draft-proof contract.",
            "submit_dirname": proof.get("submit_dirname"),
            "historical_submit_dirs": historical_dirs,
            "optional_review_note_count": optional_review_note_count,
        }

    if (
        duplicate_review_only
        and pending_payload is None
        and _current_submit_attempt_is_confirmed(proof.get("submit_dir"))
    ):
        return {
            "state": "legacy",
            "reason": (
                "This submitted job was confirmed before the current draft-proof contract required distinct "
                "pre-submit and review screenshots."
            ),
            "submit_dirname": proof.get("submit_dirname"),
            "historical_submit_dirs": historical_dirs,
            "optional_review_note_count": optional_review_note_count,
        }

    if blockers or pending_payload is not None:
        first_reason = ""
        if blockers:
            first_reason = str(blockers[0].get("reason") or "").strip()
        elif pending_payload:
            questions = list(pending_payload.get("questions") or [])
            if questions:
                first_reason = str(questions[0].get("reason") or questions[0].get("label") or "").strip()
        if historical_dirs:
            return {
                "state": "stale",
                "reason": (
                    "Historical proof exists, but the active submit attempt is missing required proof or still has blockers."
                    + (f" {first_reason}" if first_reason else "")
                ).strip(),
                "submit_dirname": proof.get("submit_dirname"),
                "historical_submit_dirs": historical_dirs,
                "optional_review_note_count": optional_review_note_count,
            }
        return {
            "state": "blocked",
            "reason": first_reason or "The active submit attempt is still missing required proof or review blockers.",
            "submit_dirname": proof.get("submit_dirname"),
            "historical_submit_dirs": historical_dirs,
            "optional_review_note_count": optional_review_note_count,
        }

    return {
        "state": "ready",
        "reason": "The active submit attempt has the required draft-proof artifacts.",
        "submit_dirname": proof.get("submit_dirname"),
        "historical_submit_dirs": historical_dirs,
        "optional_review_note_count": optional_review_note_count,
    }


def _sync_draft_proof_blockers(
    output_dir: Path,
    *,
    board_name: str | None = None,
    draft_meta: dict | None = None,
) -> list[dict]:
    from application_submit_common import (
        load_pending_user_input_for_submit_attempt,
        pending_user_input_questions_for_unconfirmed_fields,
        write_pending_user_input,
    )
    from draft_manager import generate_draft_summary

    proof, blockers = _draft_proof_blocker_entries(output_dir, board_name=board_name)
    if not blockers:
        return []

    pending = load_pending_user_input_for_submit_attempt(output_dir)
    existing_payload = pending[1] if pending is not None else {}
    merged_questions = _dedupe_pending_user_input_questions(
        list(existing_payload.get("questions") or []),
        pending_user_input_questions_for_unconfirmed_fields(blockers),
    )

    artifact_payload = dict(existing_payload.get("artifacts") or {})
    for key in ("report_json", "report_md", "pre_submit_screenshot", "review_screenshot"):
        path = proof.get(key)
        if path:
            artifact_payload[key] = str(path)

    write_pending_user_input(
        output_dir,
        board=str(proof.get("board_name") or board_name or "unknown"),
        questions=merged_questions,
        message=str(existing_payload.get("message") or "").strip()
        or "The current submit attempt is missing proof artifacts required to review this draft.",
        artifacts=artifact_payload or None,
    )

    submit_dir = Path(proof["submit_dir"])
    if draft_meta and submit_dir.is_dir():
        generate_draft_summary(output_dir, submit_dir, draft_meta)
    return blockers


def _validate_draft_completeness(output_dir: Path | None, *, board_name: str | None = None) -> list[str]:
    """Check that a draft has all required artifacts. Returns missing items."""
    if output_dir is None:
        return ["output directory"]

    missing = []
    od = Path(output_dir)
    docs_dir = od / "documents"

    has_resume = any(docs_dir.glob("*Resume*")) if docs_dir.is_dir() else False
    if not has_resume:
        has_resume = any(od.glob("*Resume*"))
    if not has_resume:
        missing.append("resume")

    has_cover_letter = any(docs_dir.glob("*Cover Letter*")) if docs_dir.is_dir() else False
    if not has_cover_letter:
        has_cover_letter = any(od.glob("*Cover Letter*"))
    if not has_cover_letter:
        missing.append("cover letter")

    proof, _ = _draft_proof_blocker_entries(od, board_name=board_name)
    _, blockers = _draft_proof_blocker_entries(od, board_name=board_name)
    if proof.get("report_json") is None:
        missing.append("current-attempt autofill report")
    if proof.get("pre_submit_screenshot") is None:
        missing.append("current-attempt pre-submit screenshot")
    for blocker in blockers:
        artifact_key = str(blocker.get("artifact_key") or "").strip()
        label = str(blocker.get("label") or "").strip().casefold()
        if artifact_key == "review_screenshot" and "distinct" in label:
            missing.append("distinct review screenshot proof")
        elif artifact_key == "review_screenshot":
            missing.append("current-attempt review screenshot")
    if not (od / "draft_summary.png").exists():
        missing.append("draft summary screenshot")
    return missing
