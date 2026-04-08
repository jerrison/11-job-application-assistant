#!/usr/bin/env python3
"""Shared linked-resource and verifier-feedback helpers for answer generation."""

from __future__ import annotations

import os
from pathlib import Path

from generated_answer_validation import _generated_answer_blocker_step
from llm_provider import default_active_provider, provider_available
from submit_review_common import (
    pending_user_input_questions_for_unconfirmed_fields,
    write_pending_user_input,
)


def default_answer_provider() -> str:
    if provider := os.getenv("ASSET_LLM_PROVIDER"):
        return provider
    primary = default_active_provider()
    for candidate in (primary, "gemini", "claude", "codex"):
        if provider_available(candidate):
            return candidate
    raise RuntimeError(
        "No answer-generation provider found. Configure `openai`, or install `gemini`, `claude`, or `codex`, or set ASSET_LLM_PROVIDER."
    )


def _linked_resource_failure_blockers(linked_resource_payload: dict) -> list[dict]:
    blockers: list[dict] = []
    failures = linked_resource_payload.get("failures") if isinstance(linked_resource_payload, dict) else None
    if not isinstance(failures, list):
        return blockers
    for failure in failures:
        if not isinstance(failure, dict) or not failure.get("required"):
            continue
        spec = {
            "field_name": failure.get("field_name"),
            "label": failure.get("label"),
            "required": True,
            "type": "String",
        }
        reason = (
            "This required question depends on a linked resource that could not be fetched or parsed: "
            f"{failure.get('url')} ({failure.get('failure_reason')})."
        )
        step = _generated_answer_blocker_step(spec, raw_value=failure.get("url"), reason=reason)
        step["source"] = "linked_resource_fetch"
        step["artifact_key"] = "linked_resource_failures_json"
        step["note"] = reason
        blockers.append(step)
    return blockers


def _write_pending_linked_resource_blockers(out_dir: Path, *, meta: dict, blockers: list[dict], payload: dict) -> None:
    if not blockers:
        return
    artifacts: dict[str, str] = {}
    payload_artifacts = payload.get("artifacts") if isinstance(payload, dict) else None
    if isinstance(payload_artifacts, dict):
        for source_key, target_key in (
            ("context_json", "linked_resource_context_json"),
            ("failures_json", "linked_resource_failures_json"),
        ):
            raw = str(payload_artifacts.get(source_key) or "").strip()
            if raw:
                artifacts[target_key] = raw
    write_pending_user_input(
        out_dir,
        board=str(meta.get("board") or "unknown"),
        questions=pending_user_input_questions_for_unconfirmed_fields(blockers),
        artifacts=artifacts or None,
        message=(
            "The draft stopped before answer generation because one or more required linked resources "
            "could not be fetched or parsed."
        ),
    )


def _linked_resource_answer_payload(linked_resource_payload: dict) -> dict | None:
    if not isinstance(linked_resource_payload, dict):
        return None
    resources = linked_resource_payload.get("resources")
    failures = linked_resource_payload.get("failures")
    if not resources and not failures:
        return None
    return {
        "cache_key": linked_resource_payload.get("cache_key"),
        "artifacts": linked_resource_payload.get("artifacts") or {},
        "resources": [
            {
                "field_name": resource.get("field_name"),
                "label": resource.get("label"),
                "url": resource.get("url"),
                "adapter": resource.get("adapter"),
                "content_fingerprint": resource.get("content_fingerprint"),
                "payload_json": resource.get("payload_json"),
                "raw_artifact": resource.get("raw_artifact"),
                "derived_facts": resource.get("derived_facts") or [],
                "deterministic_answer": resource.get("deterministic_answer"),
            }
            for resource in (resources or [])
            if isinstance(resource, dict)
        ],
        "failures": [
            {
                "field_name": failure.get("field_name"),
                "label": failure.get("label"),
                "url": failure.get("url"),
                "adapter": failure.get("adapter"),
                "required": bool(failure.get("required")),
                "failure_reason": failure.get("failure_reason"),
            }
            for failure in (failures or [])
            if isinstance(failure, dict)
        ],
    }


def _source_ref_for_path(out_dir: Path, raw_path: object) -> str | None:
    path_text = str(raw_path or "").strip()
    if not path_text:
        return None
    path = Path(path_text)
    try:
        return str(path.relative_to(out_dir))
    except ValueError:
        return path.name if path.name else None


def _build_answer_verification_source_bundle(
    *,
    out_dir: Path,
    application_profile_text: str,
    master_resume_text: str,
    work_stories_text: str,
    candidate_context_text: str,
    jd_parsed: dict,
    resume_content: dict | None,
    research_cache: dict,
    linked_resource_payload: dict,
    preference_research_payload: dict | None = None,
) -> dict:
    linked_resources = _linked_resource_answer_payload(linked_resource_payload)
    preference_research = preference_research_payload if isinstance(preference_research_payload, dict) else {}
    source_refs = [
        "application_profile.md",
        "master_resume.md",
        "work_stories.md",
        "candidate_context.md",
    ]
    if jd_parsed:
        source_refs.append("content/jd_parsed.json")
    if resume_content:
        source_refs.append("content/resume_content.json")
    if research_cache:
        source_refs.append("content/role_research_cache.json")

    if linked_resources:
        artifacts = linked_resources.get("artifacts") if isinstance(linked_resources, dict) else None
        if isinstance(artifacts, dict):
            for artifact_path in artifacts.values():
                source_ref = _source_ref_for_path(out_dir, artifact_path)
                if source_ref:
                    source_refs.append(source_ref)
        for resource in linked_resources.get("resources") or []:
            if not isinstance(resource, dict):
                continue
            for key in ("payload_json", "raw_artifact"):
                source_ref = _source_ref_for_path(out_dir, resource.get(key))
                if source_ref:
                    source_refs.append(source_ref)

    preference_artifacts = preference_research.get("artifacts") if isinstance(preference_research, dict) else None
    if isinstance(preference_artifacts, dict):
        for artifact_path in preference_artifacts.values():
            source_ref = _source_ref_for_path(out_dir, artifact_path)
            if source_ref:
                source_refs.append(source_ref)

    deduped_source_refs: list[str] = []
    for source_ref in source_refs:
        normalized = str(source_ref or "").strip()
        if normalized and normalized not in deduped_source_refs:
            deduped_source_refs.append(normalized)

    return {
        "source_refs": deduped_source_refs,
        "application_profile_text": application_profile_text,
        "master_resume_text": master_resume_text,
        "work_stories_text": work_stories_text,
        "candidate_context_text": candidate_context_text,
        "jd_parsed": jd_parsed,
        "resume_content": resume_content or {},
        "research_cache": research_cache,
        "linked_resources": linked_resources or {},
        "preference_research": preference_research or {},
    }


def _linked_resource_deterministic_answers(linked_resource_payload: dict) -> dict[str, str]:
    answers: dict[str, str] = {}
    resources = linked_resource_payload.get("resources") if isinstance(linked_resource_payload, dict) else None
    if not isinstance(resources, list):
        return answers
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        field_name = str(resource.get("field_name") or "").strip()
        deterministic_answer = str(resource.get("deterministic_answer") or "").strip()
        if field_name and deterministic_answer:
            answers[field_name] = deterministic_answer
    return answers


def _verification_retry_feedback_by_field(verification: dict) -> dict[str, list[str]]:
    raw_feedback = verification.get("retry_feedback_by_field")
    if isinstance(raw_feedback, dict):
        sanitized: dict[str, list[str]] = {}
        for field_name, feedback in raw_feedback.items():
            items = [str(item or "").strip() for item in (feedback or []) if str(item or "").strip()]
            if items:
                sanitized[str(field_name)] = items
        if sanitized:
            return sanitized

    derived: dict[str, list[str]] = {}
    for question in verification.get("questions") or []:
        if not isinstance(question, dict):
            continue
        if str(question.get("verdict") or "").strip() != "retry_with_feedback":
            continue
        field_name = str(question.get("field_name") or "").strip()
        feedback = [str(item or "").strip() for item in (question.get("feedback_for_regeneration") or []) if str(item or "").strip()]
        if field_name and feedback:
            derived[field_name] = feedback
    return derived


def _augment_answer_generation_prompt_with_verifier_feedback(
    prompt: str,
    *,
    question_specs: list[dict],
    retry_feedback_by_field: dict[str, list[str]],
) -> str:
    if not retry_feedback_by_field:
        return prompt

    specs_by_field = {
        str(spec.get("field_name") or "").strip(): spec
        for spec in question_specs
        if str(spec.get("field_name") or "").strip()
    }
    lines = [
        prompt.rstrip(),
        "",
        "Verifier feedback for regeneration:",
        "Rewrite only the flagged answers below. Keep every claim grounded in the provided source material.",
        "",
    ]
    for field_name, feedback in retry_feedback_by_field.items():
        spec = specs_by_field.get(field_name, {})
        label = str(spec.get("label") or field_name)
        lines.append(f"- {label} ({field_name})")
        for item in feedback:
            lines.append(f"  - {item}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _verifier_retry_feedback_blockers(
    *,
    question_specs: list[dict],
    answers: dict[str, object],
    verification: dict,
) -> list[dict]:
    retry_feedback_by_field = _verification_retry_feedback_by_field(verification)
    questions_by_field = {
        str(question.get("field_name") or "").strip(): question
        for question in verification.get("questions") or []
        if isinstance(question, dict) and str(question.get("field_name") or "").strip()
    }
    blockers: list[dict] = []
    for spec in question_specs:
        field_name = str(spec.get("field_name") or "").strip()
        feedback = retry_feedback_by_field.get(field_name)
        if not feedback:
            continue
        question = questions_by_field.get(field_name, {})
        reason = (
            str(question.get("reason") or "").strip()
            or "Answer verification could not approve this answer after one guided regeneration."
        )
        blocker = _generated_answer_blocker_step(spec, raw_value=answers.get(field_name), reason=reason)
        blocker["source"] = "answer_verifier"
        blocker["note"] = "Verifier feedback: " + " ".join(feedback)
        blockers.append(blocker)
    return blockers
