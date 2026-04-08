#!/usr/bin/env python3
"""Repair missing answer-verification proof from the current draft artifacts."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from answer_generation_support import _build_answer_verification_source_bundle
from answer_state_sync import sync_current_attempt_answer_states_from_proof
from answer_verifier import verify_generated_answers
from application_models import parse_application_profile
from application_submit_common import (
    APPLICATION_PROFILE_PATH,
    CANDIDATE_CONTEXT_PATH,
    MASTER_RESUME_PATH,
    WORK_STORIES_PATH,
    load_meta,
    load_optional_json,
)
from output_layout import ANSWER_VERIFICATION_JSON, APPLICATION_ANSWER_CACHE, role_content_path, role_submit_dir
from project_env import load_project_env

load_project_env()


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


def _report_field_names(report_payload: dict) -> set[str]:
    field_names: set[str] = set()
    for field in report_payload.get("fields", []):
        if not isinstance(field, dict):
            continue
        field_name = str(field.get("field_name") or "").strip()
        if field_name:
            field_names.add(field_name)
    return field_names


def _load_current_attempt_autofill_report(
    submit_dir: Path,
    *,
    answers_payload: dict | None = None,
) -> tuple[dict | None, Path | None]:
    candidates: list[tuple[Path, dict]] = []
    for report_path in sorted(submit_dir.glob("*_autofill_report.json")):
        payload = load_optional_json(report_path)
        if isinstance(payload, dict):
            candidates.append((report_path, payload))
    if not candidates:
        return None, None

    selected_path, selected_payload = candidates[0]
    answer_field_names = _answer_payload_field_names(answers_payload)
    if answer_field_names:
        selected_overlap = len(_report_field_names(selected_payload) & answer_field_names)
        for report_path, payload in candidates[1:]:
            overlap = len(_report_field_names(payload) & answer_field_names)
            if overlap > selected_overlap:
                selected_path, selected_payload = report_path, payload
                selected_overlap = overlap
    return selected_payload, selected_path


def _report_board_name(report_path: Path | None) -> str | None:
    if report_path is None:
        return None
    stem = report_path.stem
    for suffix in ("_autofill_report", "_post_apply_report"):
        if stem.endswith(suffix):
            board_name = stem[: -len(suffix)].strip()
            return board_name or None
    return None


def _field_sources(report_payload: dict) -> dict[str, str]:
    sources: dict[str, str] = {}
    for field in report_payload.get("fields", []):
        if not isinstance(field, dict):
            continue
        field_name = str(field.get("field_name") or "").strip()
        if not field_name:
            continue
        source = str(field.get("source") or "").strip()
        if source:
            sources[field_name] = source
    return sources


def _rendered_verification_inputs(
    answers_payload: dict,
    report_payload: dict,
) -> tuple[list[dict], dict[str, object], set[str], int]:
    question_specs_raw = answers_payload.get("questions")
    answers_raw = answers_payload.get("answers")
    if not isinstance(question_specs_raw, list) or not isinstance(answers_raw, dict):
        return [], {}, set(), 0

    field_sources = _field_sources(report_payload)
    question_specs: list[dict] = []
    answers: dict[str, object] = {}
    deterministic_field_names: set[str] = set()
    llm_generated_count = 0

    for raw_spec in question_specs_raw:
        if not isinstance(raw_spec, dict):
            continue
        field_name = str(raw_spec.get("field_name") or "").strip()
        if not field_name or field_name not in answers_raw:
            continue
        source = field_sources.get(field_name)
        if not source:
            continue
        question_specs.append(dict(raw_spec))
        answers[field_name] = answers_raw[field_name]
        if source == "generated_application_answer":
            llm_generated_count += 1
        else:
            deterministic_field_names.add(field_name)

    return question_specs, answers, deterministic_field_names, llm_generated_count


def backfill_missing_answer_verification_from_current_proof(out_dir: str | Path) -> dict[str, object]:
    out_path = Path(out_dir)
    submit_dir = role_submit_dir(out_path)
    if (submit_dir / ANSWER_VERIFICATION_JSON).exists():
        return {"status": "skipped", "reason": "current_verification_present", "submit_dirname": submit_dir.name}

    answers_payload = load_optional_json(submit_dir / APPLICATION_ANSWER_CACHE)
    if not isinstance(answers_payload, dict):
        return {"status": "skipped", "reason": "missing_answers", "submit_dirname": submit_dir.name}

    report_payload, report_path = _load_current_attempt_autofill_report(submit_dir, answers_payload=answers_payload)
    if not isinstance(report_payload, dict):
        return {"status": "skipped", "reason": "missing_report", "submit_dirname": submit_dir.name}

    question_specs, answers, deterministic_field_names, llm_generated_count = _rendered_verification_inputs(
        answers_payload,
        report_payload,
    )
    if llm_generated_count == 0:
        sync_current_attempt_answer_states_from_proof(out_path, submit_dir.name)
        return {"status": "not_applicable", "reason": "no_generated_answers", "submit_dirname": submit_dir.name}
    if not question_specs:
        return {"status": "skipped", "reason": "missing_rendered_questions", "submit_dirname": submit_dir.name}

    meta = load_meta(out_path)
    report_board = _report_board_name(report_path)
    if report_board and not str(meta.get("board") or "").strip():
        meta = {**meta, "board": report_board}
    application_profile_text = APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
    master_resume_text = MASTER_RESUME_PATH.read_text(encoding="utf-8")
    work_stories_text = WORK_STORIES_PATH.read_text(encoding="utf-8")
    candidate_context_text = CANDIDATE_CONTEXT_PATH.read_text(encoding="utf-8")
    application_profile = parse_application_profile(application_profile_text)
    jd_parsed = (
        load_optional_json(role_content_path(out_path, "jd_parsed.json"))
        or load_optional_json(out_path / "jd_parsed.json")
        or {}
    )
    resume_content = load_optional_json(role_content_path(out_path, "resume_content.json")) or load_optional_json(
        out_path / "resume_content.json"
    )
    research_cache = load_optional_json(SCRIPT_DIR.parent / "output" / meta["company"] / "research_cache.json") or {}
    role_research = load_optional_json(role_content_path(out_path, "role_research_cache.json")) or load_optional_json(
        out_path / "role_research_cache.json"
    )
    if role_research and "role_context" in role_research:
        research_cache = {**research_cache, "role_context": role_research["role_context"]}

    verifier_source_bundle = _build_answer_verification_source_bundle(
        out_dir=out_path,
        application_profile_text=application_profile_text,
        master_resume_text=master_resume_text,
        work_stories_text=work_stories_text,
        candidate_context_text=candidate_context_text,
        jd_parsed=jd_parsed,
        resume_content=resume_content,
        research_cache=research_cache,
        linked_resource_payload=answers_payload.get("linked_resources") if isinstance(answers_payload, dict) else {},
        preference_research_payload=(
            answers_payload.get("preference_research") if isinstance(answers_payload, dict) else None
        ),
    )

    verification = verify_generated_answers(
        out_dir=out_path,
        meta=meta,
        question_specs=question_specs,
        answers=answers,
        application_profile=application_profile,
        deterministic_field_names=deterministic_field_names,
        answer_provider=str(answers_payload.get("provider") or "").strip() or None,
        source_bundle=verifier_source_bundle,
    )
    sync_current_attempt_answer_states_from_proof(out_path, submit_dir.name)
    return {
        "status": str(verification.get("status") or "unknown"),
        "submit_dirname": submit_dir.name,
        "llm_generated_count": llm_generated_count,
        "question_count": len(question_specs),
    }
