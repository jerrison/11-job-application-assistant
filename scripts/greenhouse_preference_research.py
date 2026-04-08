#!/usr/bin/env python3
"""Greenhouse preference / ranking research helpers."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_submit_common import extract_json_object
from llm_provider import automation_provider_chain, provider_binary, provider_command_for_mode, provider_timeout_seconds
from output_layout import (
    PREFERENCE_RESEARCH_CONTEXT_JSON,
    PREFERENCE_RESEARCH_FAILURES_JSON,
    PREFERENCE_RESEARCH_RAW,
    existing_submit_dirs,
    role_submit_dir,
)

NORMALIZED_TEXT_RE = re.compile(r"[^a-z0-9]+")


def _json_dumps_pretty(data: object) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _normalize_text(value: str | None) -> str:
    return NORMALIZED_TEXT_RE.sub(" ", (value or "").casefold()).strip()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _eligible_specs(question_specs: list[dict]) -> list[dict]:
    return [
        dict(spec) for spec in question_specs if str(spec.get("research_mode") or "").strip() == "preference_ranking"
    ]


def _request_signature(question_specs: list[dict]) -> str | None:
    eligible = _eligible_specs(question_specs)
    if not eligible:
        return None
    normalized = [
        {
            "field_name": spec.get("field_name"),
            "label": spec.get("label"),
            "required": bool(spec.get("required")),
            "type": spec.get("type"),
            "options": list(spec.get("options") or []),
            "selection_limit": spec.get("selection_limit"),
        }
        for spec in eligible
    ]
    return _sha256_text(_json_dumps_pretty(normalized))


def _cache_key(questions: list[dict], failures: list[dict], request_signature: str | None) -> str | None:
    if request_signature is None:
        return None
    payload = {
        "request_signature": request_signature,
        "questions": [
            {
                "field_name": question.get("field_name"),
                "selected_options": question.get("selected_options"),
                "summary": question.get("summary"),
            }
            for question in questions
        ],
        "failures": [
            {
                "field_name": failure.get("field_name"),
                "failure_reason": failure.get("failure_reason"),
            }
            for failure in failures
        ],
    }
    return _sha256_text(_json_dumps_pretty(payload))


def _exact_option_label(options: list[str], raw_value: str | None) -> str | None:
    normalized_value = _normalize_text(raw_value)
    if not normalized_value:
        return None
    for option in options:
        if _normalize_text(option) == normalized_value:
            return option
    return None


def _coerce_list(raw_value: object) -> list[str]:
    if raw_value is None:
        return []
    if isinstance(raw_value, list):
        return [str(item).strip() for item in raw_value if str(item).strip()]
    text = str(raw_value).strip()
    if not text:
        return []
    if "\n" in text:
        parts = [part.strip(" -\t") for part in text.splitlines()]
    else:
        parts = [part.strip() for part in text.split(",")]
    return [part for part in parts if part]


def _evidence_entry(evidence: dict, field_name: str) -> dict:
    candidate = evidence.get(field_name)
    return dict(candidate) if isinstance(candidate, dict) else {}


def _failure_record(spec: dict, *, reason: str, raw_value: object = None) -> dict:
    record = {
        "field_name": str(spec.get("field_name") or "").strip(),
        "label": str(spec.get("label") or spec.get("field_name") or "").strip(),
        "required": bool(spec.get("required")),
        "type": str(spec.get("type") or "").strip(),
        "options": list(spec.get("options") or []),
        "selection_limit": spec.get("selection_limit"),
        "failure_reason": reason,
    }
    if isinstance(raw_value, list):
        values = [str(item).strip() for item in raw_value if str(item).strip()]
        if values:
            record["raw_value"] = values
    else:
        text = str(raw_value or "").strip()
        if text:
            record["raw_value"] = text
    return record


def _validated_question(spec: dict, evidence: dict, selected_options: list[str]) -> dict:
    supporting_evidence = evidence.get("supporting_evidence")
    if not isinstance(supporting_evidence, list):
        supporting_evidence = []
    return {
        "field_name": str(spec.get("field_name") or "").strip(),
        "label": str(spec.get("label") or spec.get("field_name") or "").strip(),
        "required": bool(spec.get("required")),
        "type": str(spec.get("type") or "").strip(),
        "options": list(spec.get("options") or []),
        "selection_limit": spec.get("selection_limit"),
        "selected_options": selected_options,
        "summary": str(evidence.get("summary") or "").strip(),
        "supporting_evidence": [str(item).strip() for item in supporting_evidence if str(item).strip()],
    }


def _validate_answer(spec: dict, raw_value: object, evidence: dict) -> tuple[object | None, dict | None, dict | None]:
    options = [str(option).strip() for option in spec.get("options") or [] if str(option).strip()]
    selection_limit = spec.get("selection_limit")
    field_type = str(spec.get("type") or "").strip()

    if field_type == "multi_value_multi_select":
        selected_options: list[str] = []
        unmatched: list[str] = []
        for item in _coerce_list(raw_value):
            matched = _exact_option_label(options, item)
            if matched is None:
                unmatched.append(item)
                continue
            if matched not in selected_options:
                selected_options.append(matched)
        if unmatched:
            return (
                None,
                _failure_record(
                    spec,
                    reason=(
                        "Preference research returned option labels that are not present in the current live options: "
                        + ", ".join(unmatched)
                    ),
                    raw_value=raw_value,
                ),
                None,
            )
        if selection_limit and len(selected_options) > int(selection_limit):
            return (
                None,
                _failure_record(
                    spec,
                    reason=(
                        f"Preference research returned {len(selected_options)} selections for a field limited to "
                        f"{selection_limit}."
                    ),
                    raw_value=selected_options,
                ),
                None,
            )
        if not selected_options:
            return (
                None,
                _failure_record(
                    spec,
                    reason="Preference research did not return any validated live option labels for this field.",
                    raw_value=raw_value,
                ),
                None,
            )
        return selected_options, None, _validated_question(spec, evidence, selected_options)

    values = _coerce_list(raw_value)
    if len(values) > 1:
        return (
            None,
            _failure_record(
                spec,
                reason="Preference research returned multiple selections for a single-select field.",
                raw_value=values,
            ),
            None,
        )
    matched = _exact_option_label(options, values[0] if values else raw_value if isinstance(raw_value, str) else None)
    if matched is None:
        return (
            None,
            _failure_record(
                spec,
                reason="Preference research returned an option label that is not present in the current live options.",
                raw_value=raw_value,
            ),
            None,
        )
    return matched, None, _validated_question(spec, evidence, [matched])


def _build_json_schema(question_specs: list[dict]) -> dict:
    answer_properties: dict[str, object] = {}
    for spec in _eligible_specs(question_specs):
        field_name = str(spec.get("field_name") or "").strip()
        if not field_name:
            continue
        if spec.get("type") == "multi_value_multi_select":
            answer_properties[field_name] = {
                "anyOf": [
                    {"type": "array", "items": {"type": "string"}},
                    {"type": "null"},
                ]
            }
        else:
            answer_properties[field_name] = {"type": ["string", "null"]}
    return {
        "type": "object",
        "properties": {
            "answers": {
                "type": "object",
                "properties": answer_properties,
                "required": sorted(answer_properties),
                "additionalProperties": False,
            },
            "evidence": {"type": "object"},
        },
        "required": ["answers"],
        "additionalProperties": False,
    }


def _build_prompt(
    *,
    meta: dict,
    question_specs: list[dict],
    jd_parsed: dict,
    resume_content: dict | None,
    research_cache: dict | None,
    cover_letter_text: str,
    master_resume_text: str,
    work_stories_text: str,
    candidate_context_text: str,
    application_profile_text: str,
) -> str:
    instructions = [
        "You are researching Greenhouse preference and ranking questions before the submit-answer stage.",
        "Use the existing repo context first. Use live web research only when it helps distinguish the current options.",
        "Treat the live option labels in the field specs as the hard answer boundary.",
        "Do not invent, paraphrase, broaden, abbreviate, or merge option labels.",
        "Research unfamiliar teams, functions, product areas, or factors before choosing.",
        "For multi-select fields, stay within the explicit selection limit when one is provided.",
        "If evidence is insufficient for a required field, return JSON null for that field and explain the ambiguity in evidence.",
        'Return JSON only with the shape {"answers": {...}, "evidence": {...}}.',
    ]
    return (
        "\n".join(instructions)
        + "\n\nFields to research:\n"
        + _json_dumps_pretty(_eligible_specs(question_specs))
        + "\n\nContext: pipeline metadata\n"
        + _json_dumps_pretty(meta)
        + "\n\nContext: jd_parsed.json\n"
        + _json_dumps_pretty(jd_parsed)
        + "\n\nContext: resume_content.json\n"
        + _json_dumps_pretty(resume_content)
        + "\n\nContext: research_cache.json\n"
        + _json_dumps_pretty(research_cache)
        + "\n\nContext: cover_letter_text.txt\n"
        + cover_letter_text
        + "\n\nContext: master_resume.md\n"
        + master_resume_text
        + "\n\nContext: work_stories.md\n"
        + work_stories_text
        + "\n\nContext: candidate_context.md\n"
        + candidate_context_text
        + "\n\nContext: application_profile.md\n"
        + application_profile_text
    )


def _fallback_provider(provider: str) -> str | None:
    chain = list(automation_provider_chain())
    try:
        current_index = chain.index(provider)
    except ValueError:
        return None
    for candidate in chain[current_index + 1 :]:
        if shutil.which(provider_binary(candidate)):
            return candidate
    return None


def _run_provider(
    *,
    provider: str,
    prompt: str,
    raw_output_path: Path,
    timeout_seconds: int,
    question_specs: list[dict],
    command_builder=None,
) -> tuple[dict | None, str, Exception | None]:
    command_builder = command_builder or provider_command_for_mode
    attempts_log: list[str] = []
    last_error: Exception | None = None
    providers = [provider]
    fallback = _fallback_provider(provider)
    if fallback:
        providers.append(fallback)

    for current_provider in providers:
        json_schema = _build_json_schema(question_specs) if current_provider == "openai" else None
        cmd = command_builder(
            current_provider,
            prompt,
            mode="research",
            json_schema=json_schema,
            json_schema_name="greenhouse_preference_research" if json_schema else None,
        )
        for attempt in range(2):
            header = f"INFO: provider={current_provider} mode=research attempt={attempt + 1}"
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
                parts.append(f"ERROR: Preference research via {current_provider} timed out after {timeout_seconds}s.")
                attempts_log.append("\n".join(part for part in parts if part))
                last_error = RuntimeError(
                    f"Preference research via {current_provider} timed out after {timeout_seconds}s."
                )
                raw_output_path.write_text("\n\n".join(part for part in attempts_log if part) + "\n", encoding="utf-8")
                break

            parts = [header]
            if completed.stdout:
                parts.append(completed.stdout.rstrip())
            if completed.stderr:
                parts.append(completed.stderr.rstrip())
            attempts_log.append("\n".join(part for part in parts if part))
            raw_output_path.write_text("\n\n".join(part for part in attempts_log if part) + "\n", encoding="utf-8")

            if completed.returncode != 0:
                last_error = RuntimeError(f"Preference research via {current_provider} failed.")
                break

            try:
                parsed = extract_json_object((completed.stdout or "").strip(), provider=current_provider)
            except (json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                if attempt == 0:
                    attempts_log[-1] = "\n".join(
                        part
                        for part in (attempts_log[-1], f"ERROR: Invalid JSON from {current_provider}: {exc}.")
                        if part
                    )
                    raw_output_path.write_text(
                        "\n\n".join(part for part in attempts_log if part) + "\n",
                        encoding="utf-8",
                    )
                    continue
                break
            return parsed, current_provider, None

    return None, providers[-1], last_error or RuntimeError("Preference research failed.")


def _validate_cached_payload(payload: dict, submit_dir: Path) -> bool:
    if not isinstance(payload, dict):
        return False
    if not isinstance(payload.get("questions"), list):
        return False
    if not isinstance(payload.get("answers"), dict):
        return False
    if not isinstance(payload.get("failures"), list):
        return False
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        return False
    raw_output = str(artifacts.get("raw_output") or "").strip()
    if raw_output:
        raw_path = Path(raw_output)
        if not raw_path.exists() or not str(raw_path).startswith(str(submit_dir)):
            return False
    return True


def _load_cached_context(path: Path, *, request_signature: str | None) -> dict | None:
    if request_signature is None or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if str(payload.get("request_signature") or "") != request_signature:
        return None
    if not _validate_cached_payload(payload, path.parent):
        return None
    return payload


def _copy_cached_artifacts(candidate_payload: dict, current_submit_dir: Path) -> dict:
    copied = dict(candidate_payload)
    artifacts = dict(candidate_payload.get("artifacts") or {})
    raw_output = str(artifacts.get("raw_output") or "").strip()
    if raw_output:
        source = Path(raw_output)
        destination = current_submit_dir / PREFERENCE_RESEARCH_RAW
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
        artifacts["raw_output"] = str(destination)
    artifacts["context_json"] = str(current_submit_dir / PREFERENCE_RESEARCH_CONTEXT_JSON)
    artifacts["failures_json"] = str(current_submit_dir / PREFERENCE_RESEARCH_FAILURES_JSON)
    copied["artifacts"] = artifacts
    return copied


def clear_preference_research_artifacts(out_dir: str | Path) -> None:
    submit_dir = role_submit_dir(out_dir)
    for filename in (
        PREFERENCE_RESEARCH_CONTEXT_JSON,
        PREFERENCE_RESEARCH_FAILURES_JSON,
        PREFERENCE_RESEARCH_RAW,
    ):
        try:
            (submit_dir / filename).unlink()
        except FileNotFoundError:
            pass


def prepare_preference_research_context(
    out_dir: str | Path,
    *,
    meta: dict,
    question_specs: list[dict],
    provider: str,
    jd_parsed: dict,
    resume_content: dict | None,
    research_cache: dict | None,
    cover_letter_text: str,
    master_resume_text: str,
    work_stories_text: str,
    candidate_context_text: str,
    application_profile_text: str,
    force_refresh: bool = False,
    command_builder=None,
    timeout_seconds: int | None = None,
) -> dict:
    out_dir = Path(out_dir)
    submit_dir = role_submit_dir(out_dir)
    submit_dir.mkdir(parents=True, exist_ok=True)
    request_signature = _request_signature(question_specs)
    if request_signature is None:
        return {
            "generated_at_utc": None,
            "request_signature": None,
            "cache_key": None,
            "provider": None,
            "questions": [],
            "answers": {},
            "failures": [],
            "artifacts": {},
            "used_cached_artifacts": False,
            "blockers": [],
        }

    context_path = submit_dir / PREFERENCE_RESEARCH_CONTEXT_JSON
    failures_path = submit_dir / PREFERENCE_RESEARCH_FAILURES_JSON
    raw_output_path = submit_dir / PREFERENCE_RESEARCH_RAW

    if not force_refresh:
        current_payload = _load_cached_context(context_path, request_signature=request_signature)
        if current_payload is not None:
            current_payload["used_cached_artifacts"] = True
            current_payload["blockers"] = [
                failure for failure in current_payload.get("failures") or [] if failure.get("required")
            ]
            return current_payload
        for candidate_submit_dir in existing_submit_dirs(out_dir):
            if candidate_submit_dir == submit_dir:
                continue
            candidate_context = _load_cached_context(
                candidate_submit_dir / PREFERENCE_RESEARCH_CONTEXT_JSON,
                request_signature=request_signature,
            )
            if candidate_context is None:
                continue
            copied = _copy_cached_artifacts(candidate_context, submit_dir)
            context_path.write_text(_json_dumps_pretty(copied) + "\n", encoding="utf-8")
            failures_path.write_text(_json_dumps_pretty(copied.get("failures") or []) + "\n", encoding="utf-8")
            copied["used_cached_artifacts"] = True
            copied["blockers"] = [failure for failure in copied.get("failures") or [] if failure.get("required")]
            return copied

    clear_preference_research_artifacts(out_dir)
    prompt = _build_prompt(
        meta=meta,
        question_specs=question_specs,
        jd_parsed=jd_parsed,
        resume_content=resume_content,
        research_cache=research_cache,
        cover_letter_text=cover_letter_text,
        master_resume_text=master_resume_text,
        work_stories_text=work_stories_text,
        candidate_context_text=candidate_context_text,
        application_profile_text=application_profile_text,
    )
    timeout = timeout_seconds if timeout_seconds is not None else provider_timeout_seconds()
    raw_data, final_provider, error = _run_provider(
        provider=provider,
        prompt=prompt,
        raw_output_path=raw_output_path,
        timeout_seconds=timeout,
        question_specs=question_specs,
        command_builder=command_builder,
    )

    answers: dict[str, object] = {}
    questions: list[dict] = []
    failures: list[dict] = []

    if error is not None:
        for spec in _eligible_specs(question_specs):
            failures.append(
                _failure_record(
                    spec,
                    reason=f"Preference research provider failed before a validated answer was produced: {error}",
                )
            )
    else:
        raw_answers = raw_data.get("answers") if isinstance(raw_data.get("answers"), dict) else {}
        if not raw_answers:
            raw_answers = {
                str(spec.get("field_name") or "").strip(): raw_data.get(str(spec.get("field_name") or "").strip())
                for spec in _eligible_specs(question_specs)
                if str(spec.get("field_name") or "").strip() in raw_data
            }
        evidence = raw_data.get("evidence") if isinstance(raw_data.get("evidence"), dict) else {}
        for spec in _eligible_specs(question_specs):
            field_name = str(spec.get("field_name") or "").strip()
            validated_answer, failure, question = _validate_answer(
                spec,
                raw_answers.get(field_name),
                _evidence_entry(evidence, field_name),
            )
            if failure is not None:
                failures.append(failure)
                continue
            if question is None or validated_answer is None:
                continue
            questions.append(question)
            answers[field_name] = validated_answer

    payload = {
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "request_signature": request_signature,
        "cache_key": _cache_key(questions, failures, request_signature),
        "provider": final_provider,
        "questions": questions,
        "answers": answers,
        "failures": failures,
        "artifacts": {
            "context_json": str(context_path),
            "failures_json": str(failures_path),
            "raw_output": str(raw_output_path),
        },
        "used_cached_artifacts": False,
        "blockers": [failure for failure in failures if failure.get("required")],
    }
    context_path.write_text(_json_dumps_pretty(payload) + "\n", encoding="utf-8")
    failures_path.write_text(_json_dumps_pretty(failures) + "\n", encoding="utf-8")
    return payload
