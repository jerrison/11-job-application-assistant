"""Shared draft/stopped audit helpers for post-run repair decisions."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
PROJECT_ROOT = SCRIPT_DIR.parent

from pipeline_draft_proof import current_rendered_audit_inputs, draft_review_state  # noqa: E402
from rendered_state_audit import audit_rendered_option_fields  # noqa: E402
from submit_review_common import (  # noqa: E402
    active_submit_dir_name,
    load_pending_user_input_for_submit_attempt,
    resolve_current_submit_artifacts,
)

_TRUTHFUL_TERMINAL_FAILURE_TYPES = frozenset(
    {
        "already_applied",
        "auth_failed",
        "auth_guarded",
        "duplicate",
        "external_apply",
        "job_closed",
        "no_apply_button",
        "pending_user_input",
        "unsupported",
        "user_rejected",
        "user_stopped",
    }
)

_REPAIRABLE_STOP_FAILURE_TYPES = frozenset(
    {
        "",
        "answer_refresh_failed",
        "incomplete",
        "retries_exhausted",
        "stopped_audit_repairable",
        "submit_failed",
        "unknown",
    }
)

_TERMINAL_RETRIES_EXHAUSTED_REASON_FRAGMENTS = (
    "job board is rate-limiting",
    "job board is rate limiting",
    "rate-limiting",
    "rate limiting",
    "html/login/blocker shell",
    "html login blocker shell",
    "provide jd text directly",
)


@dataclass(frozen=True)
class AuditDecision:
    kind: str
    failure_type: str | None
    reason: str
    repair_actions: tuple[str, ...] = ()
    artifacts: dict[str, str] = field(default_factory=dict)


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _markdown_relpath(base_file: Path, target: Path) -> str:
    try:
        return Path(os.path.relpath(target, start=base_file.parent)).as_posix()
    except ValueError:
        return target.as_posix()


def _looks_like_probe_worthy_supported_wrapper(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    host_labels = {label for label in host.split(".") if label}
    return bool(host_labels & {"apply", "career", "careers", "jobs"}) and any(
        fragment in path for fragment in ("/career", "/careers", "/job/")
    )


@lru_cache(maxsize=512)
def _current_supported_board_for_url(url: str) -> str | None:
    candidate = str(url or "").strip()
    if not candidate:
        return None
    try:
        from submit_application import _board_for_url, _direct_board_for_url

        direct_board = _direct_board_for_url(candidate)
        if direct_board:
            return direct_board
        if not _looks_like_probe_worthy_supported_wrapper(candidate):
            return None
        return _board_for_url(candidate)
    except (ImportError, ValueError):
        return None


def _infer_output_root(output_dir: str | Path | None) -> Path | None:
    if output_dir is None:
        return None
    path = Path(output_dir).resolve()
    for candidate in (path, *path.parents):
        if candidate.name == "output":
            return candidate
    return None


def _field_keys(field_name: object, label: object) -> set[str]:
    keys = set()
    for raw in (field_name, label):
        text = str(raw or "").strip()
        if text:
            keys.add(text.casefold())
    return keys


def _load_unknown_question_keys(submit_dir: Path, report_payload: dict) -> set[str]:
    keys: set[str] = set()
    for question in list(report_payload.get("unknown_questions") or []):
        if isinstance(question, dict):
            keys.update(_field_keys(question.get("field_name"), question.get("label")))

    for path in sorted(submit_dir.glob("*_unknown_questions.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        questions = payload.get("questions") if isinstance(payload, dict) else payload
        if not isinstance(questions, list):
            continue
        for question in questions:
            if isinstance(question, dict):
                keys.update(_field_keys(question.get("field_name"), question.get("label")))
    return keys


def _load_report_payload(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalized_option_type(raw_value: object) -> str:
    return "".join(ch for ch in str(raw_value or "").strip().casefold() if ch.isalnum())


def _report_can_support_rendered_audit_without_answers_payload(report_payload: dict) -> bool:
    deterministic_types = {
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
    has_deterministic_field = False
    for report_field in list(report_payload.get("fields") or []):
        if not isinstance(report_field, dict):
            continue
        option_type = _normalized_option_type(report_field.get("field_type") or report_field.get("kind"))
        if option_type not in deterministic_types:
            continue
        has_deterministic_field = True
        source = str(report_field.get("source") or "").strip()
        if not source or source == "generated_application_answer":
            return False
    return has_deterministic_field


def audit_draft_outcome(
    output_dir: str | Path | None,
    *,
    board_name: str | None = None,
    missing_items: list[str] | None = None,
) -> AuditDecision:
    if output_dir is None:
        return AuditDecision(
            kind="repairable",
            failure_type="draft_audit_incomplete",
            reason="Draft audit could not run because the output directory is missing.",
            repair_actions=("requeue",),
        )

    out_dir = Path(output_dir)
    pending = load_pending_user_input_for_submit_attempt(out_dir)
    if pending is not None:
        pending_path, _payload = pending
        return AuditDecision(
            kind="terminal",
            failure_type="pending_user_input",
            reason="Draft requires explicit user input before it can continue.",
            artifacts={"pending_user_input": str(pending_path)},
        )

    if missing_items is None:
        proof_state = draft_review_state(out_dir, board_name=board_name)
        proof_status = str(proof_state.get("state") or "").strip().casefold()
        if proof_status == "unavailable":
            return AuditDecision(
                kind="terminal",
                failure_type="job_closed",
                reason=str(proof_state.get("reason") or "The application is unavailable.").strip(),
            )
        if proof_status and proof_status != "ready":
            resolved = resolve_current_submit_artifacts(out_dir, board_name=board_name)
            submit_dir = Path(resolved["submit_dir"])
            return AuditDecision(
                kind="repairable",
                failure_type="draft_audit_incomplete",
                reason=str(proof_state.get("reason") or "The active submit attempt is missing required proof.").strip(),
                repair_actions=("clear_current_attempt_artifacts", "requeue"),
                artifacts={"submit_dir": str(submit_dir)},
            )

    resolved = resolve_current_submit_artifacts(out_dir, board_name=board_name)
    submit_dir = Path(resolved["submit_dir"])
    report_payload = _load_report_payload(resolved.get("report_json"))
    unknown_keys = _load_unknown_question_keys(submit_dir, report_payload)

    unaccounted_labels: list[str] = []
    for report_field in list(report_payload.get("fields") or []):
        if not isinstance(report_field, dict):
            continue
        status = str(report_field.get("status") or "").strip().casefold()
        if status == "filled":
            continue
        keys = _field_keys(report_field.get("field_name"), report_field.get("label"))
        if keys & unknown_keys:
            continue
        label = str(report_field.get("label") or report_field.get("field_name") or "unknown field").strip()
        unaccounted_labels.append(label)

    missing = [item for item in (missing_items or []) if str(item or "").strip()]
    if missing or unaccounted_labels:
        missing_chunks: list[str] = []
        if missing:
            missing_chunks.append("missing proof: " + ", ".join(missing))
        if unaccounted_labels:
            missing_chunks.append("unaccounted fields: " + ", ".join(unaccounted_labels))
        return AuditDecision(
            kind="repairable",
            failure_type="draft_audit_incomplete",
            reason="; ".join(missing_chunks),
            repair_actions=("clear_current_attempt_artifacts", "requeue"),
            artifacts={"submit_dir": str(submit_dir)},
        )

    rendered_inputs = current_rendered_audit_inputs(out_dir, board_name=board_name)
    missing_answers_payload = (
        rendered_inputs.observed_fields
        and not rendered_inputs.has_answers_payload
        and not _report_can_support_rendered_audit_without_answers_payload(report_payload)
    )
    partial_answers_payload = (
        rendered_inputs.deterministic_question_count > 0
        and len(rendered_inputs.expected_fields) < rendered_inputs.deterministic_question_count
    )
    if missing_answers_payload or partial_answers_payload:
        return AuditDecision(
            kind="repairable",
            failure_type="draft_audit_incomplete",
            reason="Draft audit could not verify rendered deterministic fields because application answers are missing.",
            repair_actions=("clear_current_attempt_artifacts", "requeue"),
            artifacts={"submit_dir": str(submit_dir)},
        )
    rendered_result = audit_rendered_option_fields(
        rendered_inputs.expected_fields,
        rendered_inputs.observed_fields,
        fallback_screenshot_path=rendered_inputs.screenshot_path,
    )
    if not rendered_result.ok:
        return AuditDecision(
            kind="repairable",
            failure_type="rendered_audit_mismatch",
            reason=rendered_result.reason,
            repair_actions=("clear_current_attempt_artifacts", "requeue"),
            artifacts={"screenshot": rendered_result.screenshot_path},
        )

    return AuditDecision(kind="ready", failure_type=None, reason="Draft audit passed.")


def audit_stopped_outcome(*, failure_type: str | None, error_message: str, job_url: str | None = None) -> AuditDecision:
    normalized = str(failure_type or "").strip().casefold()
    reason = str(error_message or normalized or "Stopped without a classified error.").strip()
    normalized_reason = reason.casefold()
    if normalized == "unsupported":
        current_board = _current_supported_board_for_url(str(job_url or "").strip())
        if current_board:
            return AuditDecision(
                kind="repairable",
                failure_type="stopped_audit_repairable",
                reason=f"Stopped as unsupported, but the current detector currently recognizes this URL as {current_board}: {job_url}",
                repair_actions=("requeue",),
            )
    if normalized in _TRUTHFUL_TERMINAL_FAILURE_TYPES:
        return AuditDecision(kind="terminal", failure_type=normalized or None, reason=reason)
    if normalized == "retries_exhausted" and any(
        fragment in normalized_reason for fragment in _TERMINAL_RETRIES_EXHAUSTED_REASON_FRAGMENTS
    ):
        return AuditDecision(kind="terminal", failure_type="service_unavailable", reason=reason)
    if normalized not in _REPAIRABLE_STOP_FAILURE_TYPES:
        return AuditDecision(kind="terminal", failure_type=normalized or None, reason=reason)
    return AuditDecision(
        kind="repairable",
        failure_type="stopped_audit_repairable",
        reason=reason,
        repair_actions=("requeue",),
    )


def write_audit_failure_report(
    *,
    output_dir: str | Path,
    job_id: int,
    summary: str,
    suggestions: list[str],
    attempts: list[str],
    output_root: str | Path | None = None,
) -> tuple[Path, Path]:
    out_dir = Path(output_dir)
    resolved = resolve_current_submit_artifacts(out_dir)
    submit_dir = Path(resolved.get("submit_dir") or (out_dir / active_submit_dir_name(out_dir)))
    submit_dir.mkdir(parents=True, exist_ok=True)
    note_path = submit_dir / "audit_failure.md"

    artifact_lines: list[str] = []
    screenshot_paths: list[str] = []
    screenshot_sections: list[str] = []
    for key in (
        "report_json",
        "pre_submit_screenshot",
        "review_screenshot",
        "post_submit_screenshot",
        "submit_debug_screenshot",
        "application_answers_json",
    ):
        raw_path = resolved.get(key)
        if not raw_path:
            continue
        path = Path(raw_path)
        rendered = _display_path(path)
        artifact_link = _markdown_relpath(note_path, path)
        artifact_lines.append(f"- `{key}`: [{rendered}]({artifact_link})")
        if key.endswith("screenshot"):
            screenshot_paths.append(rendered)
            screenshot_sections.extend(
                [
                    f"### `{key}`",
                    "",
                    f"[Open screenshot]({artifact_link})",
                    "",
                    f"![{key}]({artifact_link})",
                    "",
                ]
            )

    metadata = {
        "job_id": job_id,
        "summary": summary,
        "note_path": _display_path(note_path),
        "screenshots": screenshot_paths,
    }

    lines = [f"<!-- audit_failure_index: {json.dumps(metadata, sort_keys=True)} -->", "# Audit Failure", ""]
    lines.extend(
        [
            f"- Job ID: `{job_id}`",
            "",
            "## What Failed",
            "",
            summary,
            "",
            "## Repair Attempts",
            "",
        ]
    )
    if attempts:
        for attempt in attempts:
            lines.append(f"- {attempt}")
    else:
        lines.append("- No prior repair attempts were recorded.")
    lines.extend(["", "## Suggestions", ""])
    for suggestion in suggestions or ["Inspect the current proof artifacts and board-specific selectors before rerunning."]:
        lines.append(f"- {suggestion}")
    lines.extend(["", "## Evidence", ""])
    if artifact_lines:
        lines.extend(artifact_lines)
    else:
        lines.append("- No current submit artifacts were found.")
    lines.extend(["", "## Screenshot Evidence", ""])
    if screenshot_sections:
        lines.extend(screenshot_sections)
    else:
        lines.append("No current-attempt screenshots were captured.")
    note_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    index_path = refresh_active_audit_failure_index(output_root=output_root or _infer_output_root(out_dir))
    return note_path, index_path


def refresh_active_audit_failure_index(*, output_root: str | Path | None = None) -> Path:
    root = Path(output_root) if output_root is not None else (PROJECT_ROOT / "output")
    audit_dir = root / "_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    index_path = audit_dir / "active_audit_failures.md"

    entries: list[dict] = []
    for note_path in sorted(root.rglob("audit_failure.md")):
        if "_audit" in note_path.parts:
            continue
        try:
            first_line = note_path.read_text(encoding="utf-8").splitlines()[0]
        except (IndexError, OSError):
            continue
        prefix = "<!-- audit_failure_index: "
        suffix = " -->"
        if not (first_line.startswith(prefix) and first_line.endswith(suffix)):
            continue
        try:
            payload = json.loads(first_line[len(prefix) : -len(suffix)])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        payload["note_path"] = payload.get("note_path") or _display_path(note_path)
        payload["_note_file"] = str(note_path)
        entries.append(payload)

    lines = ["# Active Audit Failures", ""]
    if not entries:
        lines.append("No active audit failures.")
    else:
        for entry in entries:
            note_ref = str(entry.get("note_path") or "").strip()
            summary = str(entry.get("summary") or "").strip()
            note_file = Path(str(entry.get("_note_file") or note_ref))
            note_link = _markdown_relpath(index_path, note_file)
            lines.append(f"- [{note_ref}]({note_link}): {summary}")
            screenshots = list(entry.get("screenshots") or [])
            if screenshots:
                lines.append(f"  screenshots: {', '.join(f'`{shot}`' for shot in screenshots)}")
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index_path


def clear_audit_failure_report(
    output_dir: str | Path | None,
    *,
    output_root: str | Path | None = None,
) -> Path | None:
    if output_dir is None:
        return None
    out_dir = Path(output_dir)
    resolved = resolve_current_submit_artifacts(out_dir)
    submit_dir = Path(resolved.get("submit_dir") or (out_dir / active_submit_dir_name(out_dir)))
    try:
        (submit_dir / "audit_failure.md").unlink(missing_ok=True)
    except OSError:
        pass
    return refresh_active_audit_failure_index(output_root=output_root or _infer_output_root(out_dir))
