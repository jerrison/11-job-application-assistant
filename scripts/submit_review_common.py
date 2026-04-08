"""Shared helpers for current-attempt proof artifacts and pending review state."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from output_layout import (
    active_submit_dir_name,
    current_submit_dir_name_for_reads,
    migrate_role_output_layout,
    role_submit_path,
)

PENDING_USER_INPUT_JSON = "pending_user_input.json"

# Intentionally re-exported for audit helpers that import it from this module.
__all__ = ["active_submit_dir_name"]


def json_dumps_pretty(data: object) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _load_payload_artifacts(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    artifacts = payload.get("artifacts")
    return dict(artifacts) if isinstance(artifacts, dict) else {}


def _resolve_artifact_path(
    artifacts: dict,
    artifact_key: str,
    board_name: str,
    submit_dir: Path,
    *,
    fallback_key: str | None = None,
) -> Path | None:
    """Resolve an artifact path from payload artifacts or board defaults."""
    raw = artifacts.get(artifact_key)
    if raw:
        path = Path(raw)
        if path.exists():
            return path

    from autofill_common import board_file_constants

    filename = board_file_constants(board_name).get(fallback_key or artifact_key, "")
    if not filename:
        return None
    path = submit_dir / filename
    return path if path.exists() else None


def resolve_submit_artifact_path(
    out_dir: Path,
    *,
    board_name: str,
    artifact_key: str,
    artifacts: dict | None = None,
    submit_dirname: str | None = None,
    fallback_key: str | None = None,
) -> Path | None:
    """Resolve a current-attempt artifact path using freshest-submit semantics."""
    out_dir = Path(out_dir)
    migrate_role_output_layout(out_dir)
    dirname = submit_dirname or current_submit_dir_name_for_reads(out_dir)
    return _resolve_artifact_path(
        artifacts or {},
        artifact_key,
        board_name,
        out_dir / dirname,
        fallback_key=fallback_key,
    )


def _normalized_board_hint(board_name: str | None) -> str | None:
    normalized = str(board_name or "").strip().casefold()
    if not normalized or normalized == "unknown":
        return None
    return normalized


def _submit_dir_artifact_board_hints(submit_dir: Path) -> list[str]:
    hints: list[str] = []
    if not submit_dir.is_dir():
        return hints
    suffixes = (
        "_autofill_report.json",
        "_autofill_report.md",
        "_autofill_pre_submit.png",
        "_autofill_review.png",
        "_autofill_post_submit.png",
        "_submit_debug.png",
        "_autofill_payload.json",
    )
    try:
        children = sorted(submit_dir.iterdir(), key=lambda path: path.name)
    except OSError:
        return hints
    for path in children:
        for suffix in suffixes:
            if not path.name.endswith(suffix):
                continue
            board_name = path.name.removesuffix(suffix).strip().casefold()
            if board_name and board_name not in hints:
                hints.append(board_name)
            break
    return hints


def _artifact_files_match(left: Path | None, right: Path | None) -> bool:
    if left is None or right is None:
        return False
    try:
        if left.resolve() == right.resolve():
            return True
    except OSError:
        if left == right:
            return True
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
    except OSError:
        return False
    try:
        with left.open("rb") as left_file, right.open("rb") as right_file:
            left_digest = hashlib.file_digest(left_file, "sha256").digest()
            right_digest = hashlib.file_digest(right_file, "sha256").digest()
    except OSError:
        return False
    return left_digest == right_digest


def resolve_current_submit_artifacts(
    out_dir: Path,
    *,
    board_name: str | None = None,
    artifacts: dict | None = None,
    submit_dirname: str | None = None,
) -> dict[str, object]:
    """Resolve current-attempt proof artifacts from the freshest submit boundary."""
    out_dir = Path(out_dir)
    migrate_role_output_layout(out_dir)
    dirname = submit_dirname or current_submit_dir_name_for_reads(out_dir)
    submit_dir = out_dir / dirname

    candidate_boards: list[str] = []
    hinted_board = _normalized_board_hint(board_name)
    if hinted_board:
        candidate_boards.append(hinted_board)
    for candidate in _submit_dir_artifact_board_hints(submit_dir):
        if candidate not in candidate_boards:
            candidate_boards.append(candidate)

    resolved: dict[str, object] = {
        "board_name": hinted_board,
        "submit_dirname": dirname,
        "submit_dir": submit_dir,
        "application_answers_json": None,
        "report_json": None,
        "report_md": None,
        "pre_submit_screenshot": None,
        "review_screenshot": None,
        "post_submit_screenshot": None,
        "submit_debug_screenshot": None,
        "payload_json": None,
        "linked_resource_context_json": None,
        "linked_resource_failures_json": None,
        "linked_resource_evidence_dir": None,
        "payload_artifacts": {},
        "artifact_sources": {},
    }
    best_candidate_rank: tuple[int, int, int, int] | None = None
    best_candidate: dict[str, object] | None = None
    for candidate_index, candidate_board in enumerate(candidate_boards):
        explicit_artifacts = dict(artifacts or {})
        payload_json = resolve_submit_artifact_path(
            out_dir,
            board_name=candidate_board,
            artifact_key="payload_json",
            artifacts=explicit_artifacts,
            submit_dirname=dirname,
        )
        payload_artifacts = _load_payload_artifacts(payload_json)
        effective_artifacts = {**payload_artifacts, **explicit_artifacts}
        artifact_sources: dict[str, str] = {}

        def resolve_candidate_artifact(
            artifact_key: str,
            *,
            fallback_key: str | None = None,
            _effective_artifacts: dict = effective_artifacts,
            _artifact_sources: dict[str, str] = artifact_sources,
            _candidate_board: str = candidate_board,
        ) -> Path | None:
            raw = _effective_artifacts.get(artifact_key)
            if raw:
                path = Path(raw)
                if path.exists():
                    _artifact_sources[artifact_key] = "payload"
                    return path
            path = resolve_submit_artifact_path(
                out_dir,
                board_name=_candidate_board,
                artifact_key=artifact_key,
                artifacts={},
                submit_dirname=dirname,
                fallback_key=fallback_key,
            )
            if path is not None:
                _artifact_sources[artifact_key] = "board_default"
            return path

        candidate_paths = {
            "report_json": resolve_candidate_artifact("report_json"),
            "report_md": resolve_candidate_artifact("report_markdown", fallback_key="report_md"),
            "pre_submit_screenshot": resolve_candidate_artifact("pre_submit_screenshot"),
            "review_screenshot": resolve_candidate_artifact("review_screenshot"),
            "post_submit_screenshot": resolve_candidate_artifact("post_submit_screenshot"),
            "submit_debug_screenshot": resolve_candidate_artifact("submit_debug_screenshot"),
            "payload_json": payload_json,
        }
        if payload_json is not None:
            artifact_sources["payload_json"] = "payload" if "payload_json" in effective_artifacts else "board_default"
        if not any(path is not None for path in candidate_paths.values()):
            continue
        strong_proof_count = sum(
            candidate_paths[key] is not None
            for key in (
                "report_json",
                "pre_submit_screenshot",
                "review_screenshot",
                "post_submit_screenshot",
            )
        )
        secondary_proof_count = sum(
            candidate_paths[key] is not None for key in ("report_md", "submit_debug_screenshot")
        )
        payload_count = int(payload_json is not None)
        candidate_rank = (strong_proof_count, secondary_proof_count, payload_count, -candidate_index)
        if best_candidate_rank is not None and candidate_rank <= best_candidate_rank:
            continue
        best_candidate_rank = candidate_rank
        best_candidate = {
            "board_name": candidate_board,
            "candidate_paths": candidate_paths,
            "payload_artifacts": payload_artifacts,
            "artifact_sources": artifact_sources,
        }

    if best_candidate is not None:
        resolved["board_name"] = best_candidate["board_name"]
        resolved.update(best_candidate["candidate_paths"])
        resolved["payload_artifacts"] = best_candidate["payload_artifacts"]
        resolved["artifact_sources"] = best_candidate["artifact_sources"]

    legacy_names = {
        "report_json": "autofill_report.json",
        "report_md": "autofill_report.md",
        "pre_submit_screenshot": "pre_submit_screenshot.png",
        "review_screenshot": "review_screenshot.png",
        "post_submit_screenshot": "post_submit_screenshot.png",
        "submit_debug_screenshot": "submit_debug.png",
        "payload_json": "autofill_payload.json",
    }
    for key, filename in legacy_names.items():
        candidate = submit_dir / filename
        if candidate.exists():
            resolved[key] = candidate
            resolved["artifact_sources"][key] = "legacy_default"
    if resolved.get("payload_json") is not None:
        resolved["payload_artifacts"] = _load_payload_artifacts(resolved["payload_json"])
        raw_review = resolved["payload_artifacts"].get("review_screenshot")
        if raw_review:
            review_path = Path(raw_review)
            if review_path.exists():
                resolved["review_screenshot"] = review_path
                resolved["artifact_sources"]["review_screenshot"] = "payload"
    generic_artifacts = {
        "application_answers_json": submit_dir / "application_answers.json",
        "linked_resource_context_json": submit_dir / "linked_resource_context.json",
        "linked_resource_failures_json": submit_dir / "linked_resource_failures.json",
    }
    for key, candidate in generic_artifacts.items():
        if candidate.exists():
            resolved[key] = candidate
            resolved["artifact_sources"][key] = "submit_default"
    evidence_dir = submit_dir / "linked_resource_evidence"
    if evidence_dir.is_dir():
        resolved["linked_resource_evidence_dir"] = evidence_dir
        resolved["artifact_sources"]["linked_resource_evidence_dir"] = "submit_default"

    from autofill_common import board_requires_distinct_review_screenshot

    normalized_board = _normalized_board_hint(str(resolved.get("board_name") or board_name or ""))
    pre_submit_screenshot = resolved.get("pre_submit_screenshot")
    review_screenshot = resolved.get("review_screenshot")
    if (
        isinstance(pre_submit_screenshot, Path)
        and isinstance(review_screenshot, Path)
        and resolved["artifact_sources"].get("review_screenshot") != "payload"
        and not board_requires_distinct_review_screenshot(normalized_board)
        and _artifact_files_match(pre_submit_screenshot, review_screenshot)
    ):
        resolved["review_screenshot"] = None
        resolved["artifact_sources"].pop("review_screenshot", None)
    return resolved


def write_pending_user_input(
    out_dir: Path,
    *,
    board: str,
    questions: list[dict],
    message: str | None = None,
    artifacts: dict[str, object] | None = None,
) -> Path:
    migrate_role_output_layout(out_dir)
    path = role_submit_path(out_dir, PENDING_USER_INPUT_JSON)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "board": board,
        "status": "pending_user_input",
        "message": message
        or "The submitter stopped before submission because one or more questions require explicit user-provided input.",
        "questions": questions,
    }
    if artifacts:
        payload["artifacts"] = artifacts
    path.write_text(json_dumps_pretty(payload) + "\n", encoding="utf-8")
    return path


def clear_pending_user_input(out_dir: Path) -> None:
    migrate_role_output_layout(out_dir)
    try:
        role_submit_path(out_dir, PENDING_USER_INPUT_JSON).unlink()
    except FileNotFoundError:
        pass


def _pending_user_input_planned_value(value: object, *, max_chars: int = 1200) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 18].rstrip() + " …[truncated]"


def pending_user_input_questions_for_unconfirmed_fields(fields: list[dict]) -> list[dict]:
    questions: list[dict] = []
    for field in fields:
        status = str(field.get("status") or "").strip().casefold()
        if status == "filled":
            continue
        label = str(field.get("label") or field.get("field_name") or "").strip()
        if not label:
            continue
        question = {
            "field_name": str(field.get("field_name") or "").strip(),
            "label": label,
            "reason": str(field.get("reason") or "").strip()
            or (
                "Autofill planned a value for this field but could not confirm that the value was present on the "
                "live application form. Review and correct it before submitting."
            ),
            "status": str(field.get("status") or "planned"),
        }
        kind = str(field.get("kind") or "").strip()
        if kind:
            question["kind"] = kind
        source = str(field.get("source") or "").strip()
        if source:
            question["source"] = source
        if "required" in field:
            question["required"] = bool(field.get("required"))
        elif "optional" in field:
            question["required"] = not bool(field.get("optional"))
        planned_value = _pending_user_input_planned_value(field.get("value"))
        if planned_value is not None:
            question["planned_value"] = planned_value
        note = str(field.get("note") or "").strip()
        if note:
            question["note"] = note
        page_index = field.get("page_index")
        if page_index is not None:
            question["page_index"] = page_index
        if field.get("blocks_draft_completion"):
            question["blocks_draft_completion"] = True
        blocker_kind = str(field.get("blocker_kind") or "").strip()
        if blocker_kind:
            question["blocker_kind"] = blocker_kind
        profile_field = str(field.get("profile_field") or "").strip()
        if profile_field:
            question["profile_field"] = profile_field
        artifact_key = str(field.get("artifact_key") or "").strip()
        if artifact_key:
            question["artifact_key"] = artifact_key
        questions.append(question)
    return questions


def load_pending_user_input_for_submit_attempt(
    out_dir: Path,
    *,
    submit_dirname: str | None = None,
    started_at_utc: datetime | None = None,
) -> tuple[Path, dict] | None:
    """Load pending-user-input payload for the freshest/current submit attempt."""
    migrate_role_output_layout(out_dir)
    dirname = submit_dirname or current_submit_dir_name_for_reads(out_dir)
    path = out_dir / dirname / PENDING_USER_INPUT_JSON
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("status") != "pending_user_input":
        return None
    if started_at_utc is not None:
        try:
            modified_at_utc = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except OSError:
            return None
        if modified_at_utc + timedelta(seconds=1) < started_at_utc:
            return None
    resolved = resolve_current_submit_artifacts(out_dir, submit_dirname=dirname)
    resolved_board = _normalized_board_hint(resolved.get("board_name"))
    payload_board = _normalized_board_hint(payload.get("board"))
    if resolved_board and payload_board and resolved_board != payload_board:
        return None

    questions = list(payload.get("questions") or [])
    if questions:
        filtered_questions: list[dict] = []
        for question in questions:
            artifact_key = str(question.get("artifact_key") or "").strip()
            blocker_kind = str(question.get("blocker_kind") or "").strip().casefold()
            if artifact_key and blocker_kind == "required_artifact" and resolved.get(artifact_key) is not None:
                continue
            filtered_questions.append(question)
        if not filtered_questions:
            return None
        if len(filtered_questions) != len(questions):
            payload = dict(payload)
            payload["questions"] = filtered_questions

    return path, payload


def write_pending_user_input_for_unconfirmed_fields(
    out_dir: Path,
    *,
    board: str,
    fields: list[dict],
    report_json: str | None = None,
    report_markdown: str | None = None,
    pre_submit_screenshot: str | None = None,
    artifacts: dict[str, object] | None = None,
) -> Path | None:
    questions = pending_user_input_questions_for_unconfirmed_fields(fields)
    if not questions:
        return None

    payload_artifacts: dict[str, object] = dict(artifacts or {})
    if report_json:
        payload_artifacts["report_json"] = report_json
    if report_markdown:
        payload_artifacts["report_markdown"] = report_markdown
    if pre_submit_screenshot:
        payload_artifacts["pre_submit_screenshot"] = pre_submit_screenshot

    return write_pending_user_input(
        out_dir,
        board=board,
        questions=questions,
        message=(
            "The submitter stopped before submission because one or more planned fields could not be confirmed on "
            "the live form. Every field must be confirmed before submit."
        ),
        artifacts=payload_artifacts or None,
    )
