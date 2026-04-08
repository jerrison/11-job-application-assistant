#!/usr/bin/env python3
"""Reference-guided proof helpers for generated application answers."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from answer_verification_state import (
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_NOT_APPLICABLE,
    STATUS_VERIFIED,
    finalize_answer_verification,
    mark_answer_verification_pending,
)
from generated_answer_validation import _generated_answer_blocker_step
from llm_provider import (
    automation_provider_chain,
    provider_available,
    provider_command_for_mode,
    provider_timeout_seconds,
)
from output_layout import ANSWER_VERIFICATION_JSON, ANSWER_VERIFICATION_RAW, role_submit_dir, role_submit_path

LOCAL_RULE_VERIFIER_PROVIDER = "local_rule_based"

LANE_DETERMINISTIC_RENDERED_ONLY = "deterministic_rendered_only"
LANE_REFERENCE_VERIFIED_GENERATED_TEXT = "reference_verified_generated_text"
LANE_USER_REQUIRED = "user_required"

VERDICT_APPROVED = "approved"
VERDICT_RETRY_WITH_FEEDBACK = "retry_with_feedback"
VERDICT_BLOCKED_REQUIRES_USER_INPUT = "blocked_requires_user_input"
VERDICT_BLOCKED_SYSTEM_FAILURE = "blocked_system_failure"
VERDICT_NOT_APPLICABLE = "not_applicable"

VALID_REFERENCE_VERDICTS = {
    VERDICT_APPROVED,
    VERDICT_RETRY_WITH_FEEDBACK,
    VERDICT_BLOCKED_REQUIRES_USER_INPUT,
    VERDICT_BLOCKED_SYSTEM_FAILURE,
}
AUTOMATION_VERIFIER_PROVIDERS = frozenset({"openai", "gemini", "gemini-flash"})

RUBRIC_CRITERIA = (
    "answers_the_prompt",
    "grounded_in_allowed_sources",
    "truthful_and_non_fabricated",
    "policy_compliant",
    "tone_and_length_fit",
    "specificity",
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _json_dumps_pretty(data: object) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _question_text(spec: dict) -> str:
    return "\n".join(
        part.strip()
        for part in (str(spec.get("label") or ""), str(spec.get("description") or ""))
        if part and str(part).strip()
    )


def _sanitize_text_list(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    sanitized: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text:
            sanitized.append(text)
    return sanitized


def _truncate_text(value: str, *, limit: int = 6000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n...[truncated]..."


def _sanitize_source_bundle(value: object) -> object:
    if isinstance(value, str):
        return _truncate_text(value)
    if isinstance(value, list):
        return [_sanitize_source_bundle(item) for item in value[:20]]
    if isinstance(value, dict):
        return {str(key): _sanitize_source_bundle(item) for key, item in value.items()}
    return value


def _provider_questions_payload(payload: object) -> list[dict] | None:
    if not isinstance(payload, dict):
        return None
    for key in ("questions", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
    if str(payload.get("field_name") or "").strip() and str(payload.get("verdict") or "").strip():
        return [payload]
    return None


def _normalize_provider_rubric(value: object) -> dict[str, dict[str, object]]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items() if isinstance(item, dict)}
    if not isinstance(value, list):
        return {}
    normalized: dict[str, dict[str, object]] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        criterion = str(item.get("criterion") or item.get("criteria") or "").strip()
        if not criterion:
            continue
        normalized[criterion] = {
            "pass": bool(item.get("pass")),
            "notes": str(item.get("notes") or "").strip(),
        }
    return normalized


def _extract_json_object(text: str, *, provider: str | None = None) -> dict:
    from application_submit_common import extract_json_object

    return extract_json_object(text, provider=provider)


def _user_required_reason(question_text: str, application_profile) -> str | None:
    from application_submit_common import question_requires_pending_user_input

    return question_requires_pending_user_input(question_text, application_profile)


def _resolve_verifier_provider(answer_provider: str | None, verifier_provider: str | None = None) -> str:
    explicit = str(verifier_provider or "").strip()
    if explicit in AUTOMATION_VERIFIER_PROVIDERS:
        return explicit
    implicit = str(answer_provider or "").strip()
    if implicit in AUTOMATION_VERIFIER_PROVIDERS:
        return implicit
    for candidate in automation_provider_chain():
        if provider_available(candidate):
            return candidate
    if provider_available("gemini-flash"):
        return "gemini-flash"
    raise RuntimeError("No automation-compatible verifier provider found. Configure OpenAI or Gemini.")


def classify_verification_lane(
    spec: dict,
    *,
    application_profile,
    deterministic_field_names: set[str],
    user_provided_field_names: set[str] | None = None,
) -> str:
    field_name = str(spec.get("field_name") or "").strip()
    if field_name in deterministic_field_names or field_name in (user_provided_field_names or set()):
        return LANE_DETERMINISTIC_RENDERED_ONLY
    if _user_required_reason(_question_text(spec), application_profile):
        return LANE_USER_REQUIRED
    return LANE_REFERENCE_VERIFIED_GENERATED_TEXT


def _question_result(
    spec: dict,
    *,
    lane: str,
    verdict: str,
    answer_value,
    reason: str | None = None,
    confidence: str | None = None,
    score: float | None = None,
    rubric: dict | None = None,
    feedback_for_regeneration: list[str] | None = None,
    source_refs: list[str] | None = None,
) -> dict:
    result = {
        "field_name": str(spec.get("field_name") or "").strip(),
        "label": str(spec.get("label") or spec.get("field_name") or "").strip(),
        "required": bool(spec.get("required")),
        "verification_lane": lane,
        "verdict": verdict,
        "answer_text": answer_value,
        "feedback_for_regeneration": list(feedback_for_regeneration or []),
        "source_refs": list(source_refs or []),
        "rubric": dict(rubric or {}),
    }
    if reason:
        result["reason"] = reason
    if confidence:
        result["confidence"] = confidence
    if isinstance(score, (int, float)):
        result["score"] = float(score)
    return result


def _rubric_item_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "pass": {"type": "boolean"},
            "notes": {"type": "string"},
        },
        "required": ["pass", "notes"],
        "additionalProperties": False,
    }


def build_answer_verification_json_schema(question_specs: list[dict]) -> dict:
    return {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field_name": {"type": "string"},
                        "verdict": {
                            "type": "string",
                            "enum": sorted(VALID_REFERENCE_VERDICTS),
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                        "score": {
                            "type": ["number", "null"],
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "summary": {"type": "string"},
                        "source_refs": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                        "feedback_for_regeneration": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                        "rubric": {
                            "type": "object",
                            "properties": {criterion: _rubric_item_schema() for criterion in RUBRIC_CRITERIA},
                            "required": list(RUBRIC_CRITERIA),
                            "additionalProperties": False,
                        },
                    },
                    "required": [
                        "field_name",
                        "verdict",
                        "confidence",
                        "score",
                        "summary",
                        "source_refs",
                        "feedback_for_regeneration",
                        "rubric",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["questions"],
        "additionalProperties": False,
    }


def _build_reference_verifier_prompt(
    *,
    meta: dict,
    question_specs: list[dict],
    answers: dict[str, object],
    source_bundle: dict | None,
) -> str:
    sanitized_source_bundle = _sanitize_source_bundle(source_bundle or {})
    allowed_source_refs = _sanitize_text_list(
        (sanitized_source_bundle.get("source_refs") if isinstance(sanitized_source_bundle, dict) else None) or []
    )
    questions_payload = [
        {
            "field_name": str(spec.get("field_name") or "").strip(),
            "label": str(spec.get("label") or "").strip(),
            "description": str(spec.get("description") or "").strip(),
            "required": bool(spec.get("required")),
            "type": str(spec.get("type") or ""),
            "answer_text": answers.get(str(spec.get("field_name") or "").strip()),
        }
        for spec in question_specs
    ]
    return (
        "You are a strict verifier for generated job-application answers in draft mode.\n"
        "Judge each answer only against the provided repo-local sources.\n"
        "Do not browse. Do not invent facts. Do not rewrite the answers.\n"
        "Return exactly one JSON object and nothing else.\n\n"
        "Verdict rules:\n"
        f"- `{VERDICT_APPROVED}`: the answer is truthful, grounded, and policy compliant.\n"
        f"- `{VERDICT_RETRY_WITH_FEEDBACK}`: the answer is supportable from the sources, but needs one guided rewrite.\n"
        f"- `{VERDICT_BLOCKED_REQUIRES_USER_INPUT}`: the repo does not contain enough truthful support for the answer.\n"
        f"- `{VERDICT_BLOCKED_SYSTEM_FAILURE}`: the verifier cannot evaluate the answer from the provided inputs.\n\n"
        "Policy rules:\n"
        "- No numeric salary or compensation answers.\n"
        "- No unsupported credential, degree, license, or certification claims.\n"
        "- No fabricated companies, products, metrics, ownership, or timelines.\n"
        "- No contradictions of application-profile truths.\n\n"
        "For each question, return:\n"
        "- `field_name`\n"
        "- `verdict`\n"
        "- `confidence` as `low`, `medium`, or `high`\n"
        "- `score` from 0 to 1, or null if you cannot score it\n"
        "- `summary` with one concise reason\n"
        "- `source_refs` using only the allowed source refs when relevant\n"
        "- `feedback_for_regeneration` with 1-3 concise bullets only for retry verdicts, otherwise []\n"
        "- `rubric` entries for all criteria with `pass` and `notes`\n\n"
        "Allowed source refs:\n"
        + _json_dumps_pretty(allowed_source_refs)
        + "\n\nMeta:\n"
        + _json_dumps_pretty(meta)
        + "\n\nQuestions:\n"
        + _json_dumps_pretty(questions_payload)
        + "\n\nSource bundle:\n"
        + _json_dumps_pretty(sanitized_source_bundle)
        + "\n"
    )


def _run_reference_verifier_provider(
    *,
    provider: str,
    prompt: str,
    raw_output_path: Path,
    timeout_seconds: int,
    question_specs: list[dict],
    request_id: str | None = None,
    command_builder=None,
) -> tuple[dict[str, Any] | None, Exception | None]:
    command_builder = command_builder or provider_command_for_mode
    json_schema = build_answer_verification_json_schema(question_specs) if provider == "openai" else None
    cmd = command_builder(
        provider,
        prompt,
        mode="submit",
        json_mode=json_schema is None,
        json_schema=json_schema,
        json_schema_name="answer_verification" if json_schema else None,
    )
    attempt_logs: list[str] = []

    for attempt in range(2):
        header = f"INFO: provider={provider} mode=submit attempt={attempt + 1} purpose=answer_verification"
        if request_id:
            header += f" request_id={request_id}"
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(SCRIPT_DIR.parent),
                text=True,
                capture_output=True,
                timeout=timeout_seconds or None,
            )
        except subprocess.TimeoutExpired as exc:
            parts = [header]
            if exc.stdout:
                parts.append(exc.stdout.rstrip())
            if exc.stderr:
                parts.append(exc.stderr.rstrip())
            parts.append(f"ERROR: Answer verification via {provider} timed out after {timeout_seconds}s.")
            attempt_logs.append("\n".join(part for part in parts if part))
            raw_output_path.write_text("\n\n".join(part for part in attempt_logs if part) + "\n", encoding="utf-8")
            return None, RuntimeError(
                f"Answer verification via {provider} timed out after {timeout_seconds}s. See {raw_output_path} for details."
            )

        parts = [header]
        if completed.stdout:
            parts.append(completed.stdout.rstrip())
        if completed.stderr:
            parts.append(completed.stderr.rstrip())
        attempt_logs.append("\n".join(part for part in parts if part))
        raw_output_path.write_text("\n\n".join(part for part in attempt_logs if part) + "\n", encoding="utf-8")

        if completed.returncode != 0:
            return None, RuntimeError(f"Answer verification via {provider} failed. See {raw_output_path} for details.")

        try:
            payload = _extract_json_object((completed.stdout or "").strip(), provider=provider)
        except (json.JSONDecodeError, ValueError) as exc:
            if attempt == 0:
                retry_note = f"ERROR: Invalid JSON from answer verifier {provider}: {exc}. Retrying once."
                attempt_logs[-1] = "\n".join(part for part in (attempt_logs[-1], retry_note) if part)
                raw_output_path.write_text("\n\n".join(part for part in attempt_logs if part) + "\n", encoding="utf-8")
                continue
            return None, exc
        return payload, None

    return None, RuntimeError(f"Answer verification via {provider} failed without producing valid JSON.")


def _normalize_reference_question_result(
    spec: dict,
    *,
    answer_value,
    provider_question: dict,
    allowed_source_refs: set[str],
) -> dict:
    verdict = str(provider_question.get("verdict") or "").strip()
    if verdict not in VALID_REFERENCE_VERDICTS:
        raise ValueError(f"Verifier returned invalid verdict for {spec.get('field_name')}: {verdict}")

    confidence = str(provider_question.get("confidence") or "").strip()
    confidence = confidence if confidence in {"low", "medium", "high"} else None

    score = provider_question.get("score")
    if not isinstance(score, (int, float)):
        score = None

    reason = str(provider_question.get("summary") or provider_question.get("reason") or "").strip() or None
    feedback = _sanitize_text_list(provider_question.get("feedback_for_regeneration"))
    source_refs = [
        ref for ref in _sanitize_text_list(provider_question.get("source_refs")) if ref in allowed_source_refs
    ]
    rubric = _normalize_provider_rubric(provider_question.get("rubric"))

    return _question_result(
        spec,
        lane=LANE_REFERENCE_VERIFIED_GENERATED_TEXT,
        verdict=verdict,
        answer_value=answer_value,
        reason=reason,
        confidence=confidence,
        score=score,
        rubric=rubric,
        feedback_for_regeneration=feedback,
        source_refs=source_refs,
    )


def _write_artifact(out_dir: Path, payload: dict) -> None:
    role_submit_path(out_dir, ANSWER_VERIFICATION_JSON).write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def verify_generated_answers(
    *,
    out_dir: Path,
    meta: dict,
    question_specs: list[dict],
    answers: dict[str, object],
    application_profile,
    deterministic_field_names: set[str],
    user_provided_field_names: set[str] | None = None,
    answer_provider: str | None = None,
    source_bundle: dict | None = None,
    verifier_provider: str | None = None,
    command_builder=None,
    timeout_seconds: int | None = None,
) -> dict:
    pending = mark_answer_verification_pending(out_dir)
    generated_at_utc = _utc_now_iso()
    submit_dir = role_submit_dir(out_dir).name
    blockers: list[dict] = []
    questions: list[dict] = []
    verified_answer_count = 0
    blocked_requires_user_input_count = 0
    retry_count = 0
    reference_specs: list[dict] = []
    question_order: list[tuple[str, dict | str]] = []
    resolved_verifier_provider = LOCAL_RULE_VERIFIER_PROVIDER

    try:
        for spec in question_specs:
            field_name = str(spec.get("field_name") or "").strip()
            answer_value = answers.get(field_name)
            lane = classify_verification_lane(
                spec,
                application_profile=application_profile,
                deterministic_field_names=deterministic_field_names,
                user_provided_field_names=user_provided_field_names,
            )
            if lane == LANE_USER_REQUIRED:
                reason = _user_required_reason(_question_text(spec), application_profile) or (
                    "This question requires explicit user input instead of an inferred generated answer."
                )
                blocker = _generated_answer_blocker_step(spec, raw_value=answer_value, reason=reason)
                blocker["source"] = "answer_verifier"
                blockers.append(blocker)
                blocked_requires_user_input_count += 1
                question_order.append(
                    (
                        "result",
                        _question_result(
                            spec,
                            lane=lane,
                            verdict=VERDICT_BLOCKED_REQUIRES_USER_INPUT,
                            answer_value=answer_value,
                            reason=reason,
                        ),
                    )
                )
                continue
            if lane == LANE_DETERMINISTIC_RENDERED_ONLY:
                question_order.append(
                    (
                        "result",
                        _question_result(
                            spec,
                            lane=lane,
                            verdict=VERDICT_NOT_APPLICABLE,
                            answer_value=answer_value,
                        ),
                    )
                )
                continue
            reference_specs.append(spec)
            question_order.append(("reference", field_name))

        reference_results_by_field: dict[str, dict] = {}
        if reference_specs:
            resolved_verifier_provider = _resolve_verifier_provider(answer_provider, verifier_provider)
            prompt = _build_reference_verifier_prompt(
                meta=meta,
                question_specs=reference_specs,
                answers=answers,
                source_bundle=source_bundle,
            )
            raw_output_path = role_submit_path(out_dir, ANSWER_VERIFICATION_RAW)
            payload, error = _run_reference_verifier_provider(
                provider=resolved_verifier_provider,
                prompt=prompt,
                raw_output_path=raw_output_path,
                timeout_seconds=timeout_seconds or provider_timeout_seconds(),
                question_specs=reference_specs,
                request_id=pending["request_id"],
                command_builder=command_builder,
            )
            if error is not None:
                raise error
            provider_questions = _provider_questions_payload(payload)
            if provider_questions is None:
                raise ValueError("Answer verifier output did not contain a `questions` array.")

            expected_field_names = {str(spec.get("field_name") or "").strip() for spec in reference_specs}
            provider_questions_by_field = {
                str(question.get("field_name") or "").strip(): question
                for question in provider_questions
                if isinstance(question, dict) and str(question.get("field_name") or "").strip()
            }
            unexpected_field_names = sorted(
                field_name for field_name in provider_questions_by_field if field_name not in expected_field_names
            )
            if unexpected_field_names:
                raise ValueError(
                    "Answer verifier returned unexpected result field_name(s): "
                    + ", ".join(unexpected_field_names)
                )
            allowed_source_refs = set(
                _sanitize_text_list((source_bundle or {}).get("source_refs") if isinstance(source_bundle, dict) else [])
            )

            for spec in reference_specs:
                field_name = str(spec.get("field_name") or "").strip()
                provider_question = provider_questions_by_field.get(field_name)
                if provider_question is None:
                    raise ValueError(f"Answer verifier omitted a result for {field_name}.")
                result = _normalize_reference_question_result(
                    spec,
                    answer_value=answers.get(field_name),
                    provider_question=provider_question,
                    allowed_source_refs=allowed_source_refs,
                )
                if result["verdict"] == VERDICT_BLOCKED_SYSTEM_FAILURE:
                    raise RuntimeError(
                        result.get("reason")
                        or f"Answer verifier reported a system failure for {result.get('label') or field_name}."
                    )
                if result["verdict"] == VERDICT_BLOCKED_REQUIRES_USER_INPUT:
                    blocker = _generated_answer_blocker_step(
                        spec,
                        raw_value=result.get("answer_text"),
                        reason=result.get("reason")
                        or "The verifier determined this question cannot be answered truthfully from repo sources.",
                    )
                    blocker["source"] = "answer_verifier"
                    blockers.append(blocker)
                    blocked_requires_user_input_count += 1
                elif result["verdict"] == VERDICT_RETRY_WITH_FEEDBACK:
                    retry_count += 1
                elif result["verdict"] == VERDICT_APPROVED:
                    verified_answer_count += 1
                reference_results_by_field[field_name] = result

        for kind, payload in question_order:
            if kind == "result":
                questions.append(payload)  # type: ignore[arg-type]
                continue
            field_name = str(payload)
            questions.append(reference_results_by_field[field_name])

        if blockers:
            status = STATUS_BLOCKED
            message = "Answer verification blocked one or more generated answers."
        elif retry_count:
            status = STATUS_BLOCKED
            message = "Answer verification requested generator retry for one or more generated answers."
        elif verified_answer_count == 0:
            status = STATUS_NOT_APPLICABLE
            message = "No non-deterministic generated answers required verification."
        else:
            status = STATUS_VERIFIED
            message = "Generated answers passed reference-guided verification."

        retry_feedback_by_field = {
            str(question.get("field_name") or "").strip(): list(question.get("feedback_for_regeneration") or [])
            for question in questions
            if isinstance(question, dict) and question.get("verdict") == VERDICT_RETRY_WITH_FEEDBACK
        }

        payload = {
            "generated_at_utc": generated_at_utc,
            "request_id": pending["request_id"],
            "board": str(meta.get("board") or "unknown"),
            "submit_dir": submit_dir,
            "answer_provider": str(answer_provider or "").strip() or None,
            "verifier_provider": resolved_verifier_provider,
            "status": status,
            "summary": {
                "question_count": len(question_specs),
                "approved_count": verified_answer_count,
                "retry_count": retry_count,
                "blocked_count": blocked_requires_user_input_count,
                "not_applicable_count": sum(
                    1 for question in questions if question.get("verdict") == VERDICT_NOT_APPLICABLE
                ),
            },
            "questions": questions,
        }
        _write_artifact(out_dir, payload)
        finalize_answer_verification(
            out_dir,
            request_id=pending["request_id"],
            status=status,
            message=message,
            verifier_provider=resolved_verifier_provider,
            verified_answer_count=verified_answer_count,
            blocked_answer_count=blocked_requires_user_input_count + retry_count,
            proof_submit_dir=submit_dir,
        )
        payload["blockers"] = blockers
        payload["retry_feedback_by_field"] = retry_feedback_by_field
        return payload
    except Exception:
        finalize_answer_verification(
            out_dir,
            request_id=pending["request_id"],
            status=STATUS_FAILED,
            message="Answer verification failed before proof was recorded.",
            verifier_provider=resolved_verifier_provider,
            verified_answer_count=verified_answer_count,
            blocked_answer_count=blocked_requires_user_input_count + retry_count,
            proof_submit_dir=submit_dir,
        )
        raise
