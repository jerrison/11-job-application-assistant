#!/usr/bin/env python3
"""Shared helpers for organizing per-role output directories."""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

CONTENT_DIRNAME = "content"
DOCUMENTS_DIRNAME = "documents"
SUBMIT_DIRNAME = "submit"
ACTIVE_SUBMIT_DIR_POINTER = ".active_submit_dir"
ACTIVE_SUBMIT_DIR_ENV = "JOB_ASSETS_ACTIVE_SUBMIT_DIR"
SUBMISSION_RESULT_JSON = "application_submission_result.json"
WEBSITE_CONFIRMATION_JSON = "application_confirmation_website.json"
CONFIRMATION_EMAIL_REPLY_JSON = "confirmation_email_reply.json"
JOB_UNAVAILABLE_JSON = "job_unavailable.json"
APPLICATION_ANSWER_CACHE = "application_answers.json"
APPLICATION_ANSWER_RAW = "application_answers_raw.txt"
APPLICATION_ANSWER_FALLBACK_RAW = "application_answers_fallback_raw.txt"
ANSWER_VERIFICATION_JSON = "answer_verification.json"
ANSWER_VERIFICATION_RAW = "answer_verification_raw.txt"
LINKED_RESOURCE_CONTEXT_JSON = "linked_resource_context.json"
LINKED_RESOURCE_FAILURES_JSON = "linked_resource_failures.json"
LINKED_RESOURCE_EVIDENCE_DIR = "linked_resource_evidence"
PREFERENCE_RESEARCH_CONTEXT_JSON = "preference_research_context.json"
PREFERENCE_RESEARCH_FAILURES_JSON = "preference_research_failures.json"
PREFERENCE_RESEARCH_RAW = "preference_research_raw.txt"

CONTENT_FILENAMES = (
    "jd_raw.md",
    "jd_parsed.json",
    "ranked_bullets.json",
    "resume_content_draft.json",
    "resume_content.json",
    "cover_letter_text.txt",
)

DOCUMENT_PATTERNS = (
    "*Resume*.docx",
    "*Resume*.pdf",
    "*Cover Letter*.docx",
    "*Cover Letter*.pdf",
    "*Cover Letter*.txt",
)

SUBMIT_FILE_PATTERNS = (
    APPLICATION_ANSWER_CACHE,
    APPLICATION_ANSWER_RAW,
    APPLICATION_ANSWER_FALLBACK_RAW,
    ANSWER_VERIFICATION_JSON,
    ANSWER_VERIFICATION_RAW,
    LINKED_RESOURCE_CONTEXT_JSON,
    LINKED_RESOURCE_FAILURES_JSON,
    PREFERENCE_RESEARCH_CONTEXT_JSON,
    PREFERENCE_RESEARCH_FAILURES_JSON,
    PREFERENCE_RESEARCH_RAW,
    "pending_user_input.json",
    SUBMISSION_RESULT_JSON,
    WEBSITE_CONFIRMATION_JSON,
    CONFIRMATION_EMAIL_REPLY_JSON,
    JOB_UNAVAILABLE_JSON,
    "application_confirmation_email.json",
    "notion_sync_status.json",
    "*_application_page.html",
    "*_autofill_payload.json",
    "*_autofill_report.md",
    "*_autofill_report.json",
    "*_autofill_review.png",
    "*_autofill_pre_submit.png",
    "*_unknown_questions.json",
    "*_submit_debug.html",
    "*_submit_debug.png",
)

SUBMIT_DIR_PATTERNS = ("*_autofill_pages", LINKED_RESOURCE_EVIDENCE_DIR)

BUCKET_DIRNAMES = {
    "content": CONTENT_DIRNAME,
    "documents": DOCUMENTS_DIRNAME,
    "submit": SUBMIT_DIRNAME,
}


def role_content_dir(out_dir: str | Path) -> Path:
    return Path(out_dir) / CONTENT_DIRNAME


def role_documents_dir(out_dir: str | Path) -> Path:
    return Path(out_dir) / DOCUMENTS_DIRNAME


def default_role_submit_dir(out_dir: str | Path) -> Path:
    return Path(out_dir) / SUBMIT_DIRNAME


def _active_submit_dir_pointer_path(out_dir: str | Path) -> Path:
    return Path(out_dir) / ACTIVE_SUBMIT_DIR_POINTER


def _is_safe_submit_dirname(name: str) -> bool:
    stripped = name.strip()
    return bool(stripped) and Path(stripped).name == stripped


def active_submit_dir_name(out_dir: str | Path) -> str:
    out_dir = Path(out_dir)
    env_override = str(os.environ.get(ACTIVE_SUBMIT_DIR_ENV, "")).strip()
    if _is_safe_submit_dirname(env_override):
        return env_override
    pointer_path = _active_submit_dir_pointer_path(out_dir)
    if pointer_path.exists():
        try:
            configured = pointer_path.read_text(encoding="utf-8").strip()
        except OSError:
            configured = ""
        if _is_safe_submit_dirname(configured):
            return configured
    return SUBMIT_DIRNAME


def set_active_submit_dir(out_dir: str | Path, dirname: str) -> Path:
    out_dir = Path(out_dir)
    if not _is_safe_submit_dirname(dirname):
        raise ValueError(f"Invalid submit directory name: {dirname!r}")

    submit_dir = out_dir / dirname
    submit_dir.mkdir(parents=True, exist_ok=True)
    pointer_path = _active_submit_dir_pointer_path(out_dir)
    if dirname == SUBMIT_DIRNAME:
        try:
            pointer_path.unlink()
        except OSError:
            pass
    else:
        pointer_path.write_text(f"{dirname}\n", encoding="utf-8")
    return submit_dir


def create_reapply_submit_dir(out_dir: str | Path) -> Path:
    out_dir = Path(out_dir)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base_name = f"{SUBMIT_DIRNAME}-{timestamp}"
    candidate = base_name
    suffix = 2
    while (out_dir / candidate).exists():
        candidate = f"{base_name}-{suffix}"
        suffix += 1
    return set_active_submit_dir(out_dir, candidate)


def ensure_reapply_submit_dir(out_dir: str | Path) -> Path:
    out_dir = Path(out_dir)
    current_name = active_submit_dir_name(out_dir)
    if current_name != SUBMIT_DIRNAME:
        current_dir = out_dir / current_name
        if current_dir.is_dir():
            return current_dir
    return create_reapply_submit_dir(out_dir)


def role_submit_dir(out_dir: str | Path) -> Path:
    return Path(out_dir) / active_submit_dir_name(out_dir)


def submit_dirs_by_mtime(out_dir: str | Path) -> list[Path]:
    out_dir = Path(out_dir)
    submit_dirs = [
        path
        for path in out_dir.iterdir()
        if path.is_dir() and (path.name == SUBMIT_DIRNAME or path.name.startswith(f"{SUBMIT_DIRNAME}-"))
    ]
    submit_dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return submit_dirs


def _latest_submit_artifact_mtime(submit_dir: Path) -> float | None:
    if not submit_dir.is_dir():
        return None

    latest: float | None = None
    for pattern in SUBMIT_FILE_PATTERNS:
        try:
            matches = submit_dir.glob(pattern)
        except OSError:
            continue
        for path in matches:
            if not path.is_file():
                continue
            try:
                modified_at = path.stat().st_mtime
            except OSError:
                continue
            latest = modified_at if latest is None else max(latest, modified_at)
    return latest


def current_submit_dir_name_for_reads(out_dir: str | Path) -> str:
    """Resolve the freshest submit attempt to use for read-only review/sync paths."""
    out_dir = Path(out_dir)
    env_override = str(os.environ.get(ACTIVE_SUBMIT_DIR_ENV, "")).strip()
    if _is_safe_submit_dirname(env_override):
        return env_override

    active_name = active_submit_dir_name(out_dir)
    seen: set[Path] = set()
    candidates: list[Path] = []

    def add(path: Path) -> None:
        if not path.is_dir():
            return
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        candidates.append(path)

    add(out_dir / active_name)
    add(out_dir / SUBMIT_DIRNAME)
    for path in submit_dirs_by_mtime(out_dir):
        add(path)

    preferred_name = active_name
    preferred_mtime = _latest_submit_artifact_mtime(out_dir / active_name)
    for candidate in candidates:
        latest = _latest_submit_artifact_mtime(candidate)
        if latest is None:
            continue
        if preferred_mtime is None or latest > preferred_mtime + 1:
            preferred_name = candidate.name
            preferred_mtime = latest

    return preferred_name


def _read_submit_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def latest_confirmed_submit_dir(out_dir: str | Path) -> Path | None:
    for submit_dir in submit_dirs_by_mtime(out_dir):
        for name in (SUBMISSION_RESULT_JSON, WEBSITE_CONFIRMATION_JSON):
            payload = _read_submit_json(submit_dir / name)
            if isinstance(payload, dict) and payload.get("website_confirmed"):
                return submit_dir
    return None


def preferred_submit_dir_name_for_post_submit(out_dir: str | Path) -> str | None:
    env_override = str(os.environ.get(ACTIVE_SUBMIT_DIR_ENV, "")).strip()
    if _is_safe_submit_dirname(env_override):
        return None
    confirmed_dir = latest_confirmed_submit_dir(out_dir)
    if confirmed_dir is not None:
        return confirmed_dir.name
    return None


def existing_submit_dirs(out_dir: str | Path) -> list[Path]:
    out_dir = Path(out_dir)
    candidates: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen or not path.is_dir():
            return
        seen.add(resolved)
        candidates.append(path)

    add(out_dir / current_submit_dir_name_for_reads(out_dir))
    add(role_submit_dir(out_dir))
    add(default_role_submit_dir(out_dir))
    for path in submit_dirs_by_mtime(out_dir):
        add(path)
    return candidates


def ensure_role_output_dirs(out_dir: str | Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    directories = {
        "content": role_content_dir(out_dir),
        "documents": role_documents_dir(out_dir),
        "submit": role_submit_dir(out_dir),
    }
    for path in directories.values():
        path.mkdir(parents=True, exist_ok=True)
    return directories


def role_bucket_path(out_dir: str | Path, bucket: str, name: str) -> Path:
    out_dir = Path(out_dir)
    dirname = active_submit_dir_name(out_dir) if bucket == "submit" else BUCKET_DIRNAMES[bucket]
    return out_dir / dirname / name


def role_content_path(out_dir: str | Path, name: str) -> Path:
    return role_bucket_path(out_dir, "content", name)


def role_documents_path(out_dir: str | Path, name: str) -> Path:
    return role_bucket_path(out_dir, "documents", name)


def role_submit_path(out_dir: str | Path, name: str) -> Path:
    return role_bucket_path(out_dir, "submit", name)


def role_file_candidates(out_dir: str | Path, name: str, *, bucket: str | None = None) -> list[Path]:
    out_dir = Path(out_dir)
    candidates: list[Path] = []
    if bucket is not None:
        candidates.append(role_bucket_path(out_dir, bucket, name))
    else:
        candidates.extend(
            [
                role_content_path(out_dir, name),
                role_documents_path(out_dir, name),
                role_submit_path(out_dir, name),
            ]
        )
    candidates.append(out_dir / name)

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


def find_role_file(out_dir: str | Path, name: str, *, bucket: str | None = None) -> Path | None:
    for candidate in role_file_candidates(out_dir, name, bucket=bucket):
        if candidate.exists():
            return candidate
    return None


def glob_role_files(out_dir: str | Path, pattern: str, *, bucket: str | None = None) -> list[Path]:
    out_dir = Path(out_dir)
    matches: list[Path] = []
    if bucket is not None:
        bucket_dir = out_dir / BUCKET_DIRNAMES[bucket]
        if bucket_dir.exists():
            matches.extend(bucket_dir.glob(pattern))
    matches.extend(out_dir.glob(pattern))

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in matches:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def role_layout_metadata(out_dir: str | Path) -> dict[str, str]:
    out_dir = Path(out_dir)
    return {
        "content_dir": str(role_content_dir(out_dir)),
        "documents_dir": str(role_documents_dir(out_dir)),
        "submit_dir": str(role_submit_dir(out_dir)),
        "active_submit_dirname": active_submit_dir_name(out_dir),
    }


def migrate_role_output_layout(out_dir: str | Path) -> list[tuple[Path, Path]]:
    out_dir = Path(out_dir)
    ensure_role_output_dirs(out_dir)
    moved: list[tuple[Path, Path]] = []

    def move_if_present(source: Path, destination: Path) -> None:
        if not source.exists() or destination.exists():
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        moved.append((source, destination))

    for name in CONTENT_FILENAMES:
        move_if_present(out_dir / name, role_content_path(out_dir, name))

    for pattern in DOCUMENT_PATTERNS:
        for source in out_dir.glob(pattern):
            if source.is_dir():
                continue
            move_if_present(source, role_documents_path(out_dir, source.name))

    for pattern in SUBMIT_FILE_PATTERNS:
        for source in out_dir.glob(pattern):
            if source.is_dir():
                continue
            move_if_present(source, default_role_submit_dir(out_dir) / source.name)

    for pattern in SUBMIT_DIR_PATTERNS:
        for source in out_dir.glob(pattern):
            if not source.is_dir():
                continue
            move_if_present(source, default_role_submit_dir(out_dir) / source.name)

    return moved
