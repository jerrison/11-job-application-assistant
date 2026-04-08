"""Core draft mode logic: summary generation, diff detection, override management."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from answer_refresh_state import load_answer_refresh_state
from answer_state_sync import sync_current_attempt_answer_states_from_proof
from answer_verification_state import load_answer_verification_state
from application_submit_common import load_pending_user_input_for_submit_attempt, resolve_current_submit_artifacts
from output_layout import ANSWER_VERIFICATION_JSON
from pipeline_draft_proof import draft_review_state
from runtime_entrypoints import python_script_command

logger = logging.getLogger(__name__)


def _format_utc_display(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def _format_request_kind(value: str | None) -> str | None:
    labels = {
        "reanswer": "Answers only",
        "draft_overrides": "Draft overrides",
        "full_regenerate": "Full regenerate",
        "restart_pipeline": "Restart pipeline",
    }
    return labels.get(value, value.replace("_", " ").title() if value else None)


def _default_answer_refresh_message(status: str) -> str:
    messages = {
        "pending": "Waiting for fresh answer generation proof.",
        "fresh": "Fresh answer generation proof recorded.",
        "not_applicable": "No generated application answers were present for this draft.",
        "failed": "Answer regeneration failed before fresh proof was recorded.",
        "unknown": "This draft predates the answer refresh proof contract.",
    }
    return messages.get(status, "Answer refresh state is not available.")


def _default_answer_verification_message(status: str) -> str:
    messages = {
        "pending": "Waiting for answer verification proof.",
        "verified": "Generated answers passed rule-based verification.",
        "not_applicable": "No non-deterministic generated answers required verification.",
        "blocked": "Answer verification blocked one or more generated answers.",
        "failed": "Answer verification failed before proof was recorded.",
        "unknown": "This draft predates the answer verification proof contract.",
    }
    return messages.get(status, "Answer verification state is not available.")


def _build_answer_refresh_lines(out_dir: Path) -> list[str]:
    refresh = load_answer_refresh_state(out_dir)
    message = refresh.get("message") or _default_answer_refresh_message(refresh.get("status", "unknown"))
    lines = [
        "## Answer Refresh",
        "",
        f"- **Status:** {refresh.get('status', 'unknown')}",
        f"- **Request:** {_format_request_kind(refresh.get('request_kind')) or 'Not recorded'}",
        f"- **Message:** {message}",
    ]

    requested_at = _format_utc_display(refresh.get("requested_at_utc"))
    resolved_at = _format_utc_display(refresh.get("resolved_at_utc"))
    generated_at = _format_utc_display(refresh.get("answer_generated_at_utc"))
    if requested_at:
        lines.append(f"- **Requested:** {requested_at}")
    if resolved_at:
        lines.append(f"- **Resolved:** {resolved_at}")
    if refresh.get("answer_provider"):
        lines.append(f"- **Provider:** {refresh['answer_provider']}")
    if generated_at:
        lines.append(f"- **Answer Generated:** {generated_at}")
    if refresh.get("generated_answer_count") is not None:
        lines.append(f"- **Generated Answers:** {refresh['generated_answer_count']}")
    lines.append("")
    return lines


def _load_answer_verification_artifact(submit_dir: Path) -> dict | None:
    artifact_path = submit_dir / ANSWER_VERIFICATION_JSON
    if not artifact_path.exists():
        return None
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _build_answer_verification_lines(out_dir: Path, submit_dir: Path) -> list[str]:
    verification = load_answer_verification_state(out_dir)
    artifact = _load_answer_verification_artifact(submit_dir)
    message = verification.get("message") or _default_answer_verification_message(
        verification.get("status", "unknown")
    )
    lines = [
        "## Answer Verification",
        "",
        f"- **Status:** {verification.get('status', 'unknown')}",
        f"- **Message:** {message}",
    ]

    requested_at = _format_utc_display(verification.get("requested_at_utc"))
    resolved_at = _format_utc_display(verification.get("resolved_at_utc"))
    if verification.get("request_id"):
        lines.append(f"- **Request ID:** {verification['request_id']}")
    if requested_at:
        lines.append(f"- **Requested:** {requested_at}")
    if resolved_at:
        lines.append(f"- **Resolved:** {resolved_at}")
    if verification.get("verifier_provider"):
        lines.append(f"- **Provider:** {verification['verifier_provider']}")
    if verification.get("proof_submit_dir"):
        lines.append(f"- **Proof Submit Attempt:** {verification['proof_submit_dir']}")
    if verification.get("verified_answer_count") is not None:
        lines.append(f"- **Approved Answers:** {verification['verified_answer_count']}")
    if verification.get("blocked_answer_count") is not None:
        lines.append(f"- **Blocked Answers:** {verification['blocked_answer_count']}")

    if artifact:
        generated_at = _format_utc_display(artifact.get("generated_at_utc"))
        summary = artifact.get("summary") if isinstance(artifact.get("summary"), dict) else {}
        if artifact.get("answer_provider"):
            lines.append(f"- **Answer Provider:** {artifact['answer_provider']}")
        if artifact.get("status"):
            lines.append(f"- **Artifact Status:** {artifact['status']}")
        if generated_at:
            lines.append(f"- **Generated:** {generated_at}")
        if summary.get("question_count") is not None:
            lines.append(f"- **Question Count:** {summary['question_count']}")
        if summary.get("retry_count") is not None:
            lines.append(f"- **Retry Needed:** {summary['retry_count']}")
        if summary.get("not_applicable_count") is not None:
            lines.append(f"- **Not Applicable:** {summary['not_applicable_count']}")

        actionable_questions = [
            question
            for question in (artifact.get("questions") or [])
            if isinstance(question, dict)
            and (
                str(question.get("verdict") or "").startswith("blocked")
                or str(question.get("verdict") or "") == "retry_with_feedback"
            )
        ]
        if actionable_questions:
            lines.append("")
            for index, question in enumerate(actionable_questions, start=1):
                field_name = question.get("field_name") or "unknown"
                label = question.get("label") or field_name
                lines.append(f"### {index}. {label} ({field_name})")
                lines.append(f"- **Lane:** {question.get('verification_lane') or 'unknown'}")
                lines.append(f"- **Verdict:** {question.get('verdict') or 'unknown'}")
                if question.get("reason"):
                    lines.append(f"- **Reason:** {question['reason']}")
                if question.get("answer_text") not in (None, ""):
                    lines.append(f"- **Draft Answer:** {question['answer_text']}")
                for feedback in question.get("feedback_for_regeneration") or []:
                    lines.append(f"- **Feedback:** {feedback}")
                source_refs = [str(ref).strip() for ref in (question.get("source_refs") or []) if str(ref).strip()]
                if source_refs:
                    lines.append(f"- **Source Refs:** {', '.join(source_refs)}")
                lines.append("")
            return lines

    lines.append("")
    return lines


def _build_needs_review_lines(pending_payload: dict | None) -> list[str]:
    questions = list((pending_payload or {}).get("questions") or [])
    if not questions:
        return []

    lines = ["## Needs Review", ""]
    for index, question in enumerate(questions, start=1):
        field_name = question.get("field_name") or "unknown"
        label = question.get("label") or field_name
        kind = question.get("kind") or "unknown"
        required = "yes" if question.get("required") else "no"
        source = question.get("source") or "—"
        status = question.get("status") or "planned"
        lines.append(f"### {index}. {label} ({field_name})")
        lines.append(f"- **Kind:** {kind} | **Required:** {required} | **Source:** {source}")
        planned_value = question.get("planned_value")
        if question.get("artifact_key"):
            lines.append(f"- **Artifact Key:** {question['artifact_key']}")
            if planned_value:
                lines.append(f"- **Expected Path:** {planned_value}")
        elif planned_value:
            lines.append(f"- **Planned Answer:** {planned_value}")
        if question.get("page_index") is not None:
            lines.append(f"- **Page:** {question['page_index']}")
        if question.get("reason"):
            lines.append(f"- **Reason:** {question['reason']}")
        if question.get("note"):
            lines.append(f"- **Note:** {question['note']}")
        lines.append(f"- **Status:** {status}")
        lines.append("")
    return lines


def _build_draft_review_state_lines(review_state: dict | None) -> list[str]:
    if not review_state:
        return []
    lines = [
        "## Draft Review State",
        "",
        f"- **State:** {review_state.get('state', 'unknown')}",
        f"- **Reason:** {review_state.get('reason', 'Not recorded')}",
    ]
    if review_state.get("submit_dirname"):
        lines.append(f"- **Active Submit Attempt:** {review_state['submit_dirname']}")
    historical = list(review_state.get("historical_submit_dirs") or [])
    if historical:
        lines.append(f"- **Historical Proof:** {', '.join(historical)}")
    lines.append("")
    return lines


def generate_draft_summary(
    out_dir: Path,
    submit_dir: Path,
    meta: dict,
) -> dict:
    """Generate draft_summary.md, draft_summary.original.md, and draft_status.json.

    Returns dict with paths to generated files.
    """
    board = meta.get("board", "unknown")
    sync_current_attempt_answer_states_from_proof(out_dir, submit_dir.name)
    resolved_artifacts = resolve_current_submit_artifacts(out_dir, board_name=board, submit_dirname=submit_dir.name)
    report_path = Path(resolved_artifacts["report_json"]) if resolved_artifacts.get("report_json") else None
    if report_path is None:
        candidate = submit_dir / f"{board}_autofill_report.json"
        if candidate.exists():
            report_path = candidate
    if report_path is None:
        generic_report = submit_dir / "autofill_report.json"
        if generic_report.exists():
            report_path = generic_report
    report = json.loads(report_path.read_text()) if report_path and report_path.exists() else {"fields": []}
    fields = report.get("fields", [])
    pending = load_pending_user_input_for_submit_attempt(out_dir, submit_dirname=submit_dir.name)
    pending_payload = pending[1] if pending is not None else None
    answers_path = (
        Path(resolved_artifacts["application_answers_json"])
        if resolved_artifacts.get("application_answers_json")
        else None
    )
    answers_payload = (
        json.loads(answers_path.read_text(encoding="utf-8"))
        if answers_path is not None and answers_path.exists()
        else {"questions": [], "answers": {}, "linked_resources": {}}
    )
    linked_resource_payload = answers_payload.get("linked_resources") if isinstance(answers_payload, dict) else {}
    linked_success_by_field: dict[str, list[dict]] = {}
    linked_failure_by_field: dict[str, list[dict]] = {}
    for resource in (
        (linked_resource_payload.get("resources") or []) if isinstance(linked_resource_payload, dict) else []
    ):
        if isinstance(resource, dict):
            linked_success_by_field.setdefault(str(resource.get("field_name") or "").strip(), []).append(resource)
    for failure in (linked_resource_payload.get("failures") or []) if isinstance(linked_resource_payload, dict) else []:
        if isinstance(failure, dict):
            linked_failure_by_field.setdefault(str(failure.get("field_name") or "").strip(), []).append(failure)
    if not fields and isinstance(answers_payload, dict):
        for spec in answers_payload.get("questions") or []:
            field_name = str(spec.get("field_name") or "").strip()
            answer_value = (answers_payload.get("answers") or {}).get(field_name)
            fields.append(
                {
                    "field_name": field_name,
                    "label": str(spec.get("label") or field_name),
                    "kind": str(spec.get("type") or "text"),
                    "required": bool(spec.get("required")),
                    "status": "filled" if answer_value else "unfilled",
                    "value": answer_value,
                    "source": "generated_application_answer",
                }
            )

    company = meta.get("company", "Unknown")
    role = meta.get("role_title", "Unknown")
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    review_state = draft_review_state(out_dir, board_name=board)

    lines = [
        f"# Draft: {role} — {company}",
        f"**Board:** {board} | **Generated:** {now}",
        "",
    ]

    # Resume and cover letter references
    docs_dir = out_dir / "documents"
    if docs_dir.exists():
        lines.append("## Resume & Cover Letter")
        for f in sorted(docs_dir.glob("*.pdf")):
            lines.append(f"- File: documents/{f.name}")
        lines.append("")

    lines.extend(_build_answer_refresh_lines(out_dir))
    lines.extend(_build_answer_verification_lines(out_dir, submit_dir))
    lines.extend(_build_draft_review_state_lines(review_state))
    lines.extend(_build_needs_review_lines(pending_payload))

    # Application answers
    lines.append("## Application Answers")
    lines.append("")
    for i, field in enumerate(fields, 1):
        fname = field.get("field_name", "unknown")
        label = field.get("label", "Unknown")
        kind = field.get("kind", "text")
        required = "yes" if field.get("required") else "no"
        source = field.get("source") or "—"
        value = field.get("value") or "—"
        status = field.get("status", "unfilled")

        lines.append(f"### {i}. {label} ({fname})")
        lines.append(f"- **Kind:** {kind} | **Required:** {required} | **Source:** {source}")
        lines.append(f"- **Answer:** {value}")
        lines.append(f"- **Status:** {status}")
        if linked_success_by_field.get(fname):
            linked = linked_success_by_field[fname][0]
            evidence_name = Path(str(linked.get("payload_json") or "linked_resource_context.json")).name
            lines.append(
                f"- **Linked Resource:** {linked.get('adapter')} via {linked.get('url')} | Evidence: submit/{evidence_name}"
            )
        elif linked_failure_by_field.get(fname):
            linked_failure = linked_failure_by_field[fname][0]
            lines.append(
                "- **Linked Resource:** "
                f"failed to fetch {linked_failure.get('url')} ({linked_failure.get('failure_reason')}) | "
                "Evidence: submit/linked_resource_failures.json"
            )
        lines.append("")

    md_text = "\n".join(lines)

    summary_path = out_dir / "draft_summary.md"
    original_path = out_dir / "draft_summary.original.md"
    summary_path.write_text(md_text, encoding="utf-8")
    original_path.write_text(md_text, encoding="utf-8")

    # Write draft_status.json
    status_path = out_dir / "draft_status.json"
    existing_version = 0
    if status_path.exists():
        try:
            existing_version = json.loads(status_path.read_text()).get("draft_version", 0)
        except (json.JSONDecodeError, KeyError):
            pass

    status_data = {
        "status": "awaiting_review",
        "created_at": datetime.now(UTC).isoformat(),
        "reviewed_at": None,
        "reviewed_action": None,
        "draft_version": existing_version + 1,
        "draft_review_state": review_state,
    }
    status_path.write_text(json.dumps(status_data, indent=2), encoding="utf-8")

    # Generate draft_summary.png via build_draft_summary.py
    png_path = out_dir / "draft_summary.png"
    result_dict = {
        "summary": str(summary_path),
        "original": str(original_path),
        "status": str(status_path),
    }

    script = Path(__file__).resolve().parent / "build_draft_summary.py"
    try:
        proc = subprocess.run(
            python_script_command(script, str(summary_path), "-o", str(png_path)),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0 and png_path.exists():
            result_dict["png"] = str(png_path)
        else:
            logger.warning("draft_summary.png generation failed: %s", proc.stderr.strip())
    except Exception as exc:
        logger.warning("draft_summary.png generation skipped: %s", exc)

    return result_dict


# ── Diff detection and auto-classification ──────────────────────────────────

_FIELD_RE = re.compile(
    r"###\s+\d+\.\s+.+?\((\S+)\)\s*\n"
    r"(?:.*?\n)*?"
    r"-\s+\*\*Answer:\*\*\s*(.*?)\s*\n"
    r"-\s+\*\*Status:\*\*\s*(\S+)",
    re.MULTILINE,
)


def _parse_fields(text: str) -> dict[str, dict]:
    """Parse draft_summary.md into {field_key: {answer, status}} dict."""
    fields = {}
    for m in _FIELD_RE.finditer(text):
        fields[m.group(1)] = {"answer": m.group(2), "status": m.group(3)}
    return fields


def classify_draft_edits(original_text: str, edited_text: str) -> list[dict]:
    """Diff original vs edited draft summary and classify each change.

    Returns list of dicts with: field_key, old_answer, new_answer, classification.
    Classifications:
      - "missing_handler": unfilled field now has an answer
      - "wrong_answer": filled field answer changed
      - "value_override": minor text change (likely preference, not bug)
    """
    orig = _parse_fields(original_text)
    edited = _parse_fields(edited_text)
    changes = []

    for key, ed in edited.items():
        og = orig.get(key)
        if og is None:
            continue
        if og["answer"] == ed["answer"]:
            continue

        classification = "wrong_answer"
        if og["answer"] == "\u2014" or og["status"] == "unfilled":
            classification = "missing_handler"

        changes.append(
            {
                "field_key": key,
                "old_answer": og["answer"],
                "new_answer": ed["answer"],
                "old_status": og["status"],
                "classification": classification,
            }
        )

    return changes


# ── Override persistence and fix report generation ──────────────────────────


def apply_draft_edits(out_dir: Path, changes: list[dict]) -> dict:
    """Process classified changes: write overrides and generate fix report.

    Returns dict with "overrides_path" and optionally "fix_report_path".
    """
    # Load existing overrides
    overrides_path = out_dir / "draft_overrides.json"
    overrides = {}
    if overrides_path.exists():
        try:
            overrides = json.loads(overrides_path.read_text())
        except json.JSONDecodeError:
            pass

    # All changes become overrides (applied on next regeneration)
    fix_items = []
    for change in changes:
        overrides[change["field_key"]] = change["new_answer"]
        if change["classification"] in ("missing_handler", "wrong_answer"):
            fix_items.append(change)

    overrides_path.write_text(json.dumps(overrides, indent=2), encoding="utf-8")

    result = {"overrides_path": str(overrides_path)}

    # Generate fix report if there are code-level issues
    if fix_items:
        lines = ["# Draft Fix Report", "", "Auto-generated from user edits to draft_summary.md.", ""]
        for item in fix_items:
            lines.append(f"## {item['field_key']}")
            lines.append(f"- **Classification:** {item['classification']}")
            lines.append(f"- **Old answer:** {item['old_answer']}")
            lines.append(f"- **New answer (user expects):** {item['new_answer']}")
            lines.append(f"- **Old status:** {item['old_status']}")
            lines.append("")

        report_path = out_dir / "draft_fix_report.md"
        report_path.write_text("\n".join(lines), encoding="utf-8")
        result["fix_report_path"] = str(report_path)

    return result


def diff_draft_fields_from_overrides(out_dir: Path, overrides: dict) -> dict:
    """Compare overrides against the autofill report and generate a fix report.

    Returns dict with optionally "fix_report_path".
    """
    result = {}

    # Find the autofill report to get original values
    report_data = None
    for pattern in out_dir.glob("submit/*_autofill_report.json"):
        try:
            report_data = json.loads(pattern.read_text(encoding="utf-8"))
            break
        except (json.JSONDecodeError, OSError):
            pass
    if not report_data:
        return result

    fields_by_name = {}
    for field in report_data.get("fields", []):
        fn = field.get("field_name", "")
        if fn:
            fields_by_name[fn] = field

    fix_items = []
    for field_name, new_value in overrides.items():
        orig = fields_by_name.get(field_name, {})
        old_value = orig.get("value", "")
        if str(old_value) != str(new_value):
            fix_items.append(
                {
                    "field_key": field_name,
                    "label": orig.get("label", field_name),
                    "old_answer": str(old_value),
                    "new_answer": str(new_value),
                    "old_status": orig.get("status", "unknown"),
                    "source": orig.get("source", "unknown"),
                    "kind": orig.get("kind", "unknown"),
                }
            )

    if fix_items:
        lines = ["# Draft Fix Report", "", "Auto-generated from user edits to application answers.", ""]
        for item in fix_items:
            lines.append(f"## {item['label']}")
            lines.append(f"- **Field:** `{item['field_key']}`")
            lines.append(f"- **Kind:** {item['kind']}")
            lines.append(f"- **Source:** {item['source']}")
            lines.append(f"- **Old answer:** {item['old_answer']}")
            lines.append(f"- **User expects:** {item['new_answer']}")
            lines.append(f"- **Old status:** {item['old_status']}")
            lines.append("")

        report_path = out_dir / "draft_fix_report.md"
        report_path.write_text("\n".join(lines), encoding="utf-8")
        result["fix_report_path"] = str(report_path)

    return result
