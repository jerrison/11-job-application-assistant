#!/usr/bin/env python3
"""Track reusable content/build state for single-job pipeline reruns."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(SCRIPT_DIR))

from candidate_runtime import document_filename
from enforce_resume_policy import resume_meets_minimums
from output_layout import migrate_role_output_layout, role_content_path, role_documents_path

STATE_FILENAME = ".asset_pipeline_state.json"


def state_path(out_dir: str | Path) -> Path:
    return Path(out_dir) / STATE_FILENAME


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_state(out_dir: str | Path) -> dict:
    path = state_path(out_dir)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_state(out_dir: str | Path, payload: dict) -> None:
    path = state_path(out_dir)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _content_files(out_dir: str | Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    migrate_role_output_layout(out_dir)
    return {
        "draft": role_content_path(out_dir, "resume_content_draft.json"),
        "resume_content": role_content_path(out_dir, "resume_content.json"),
        "cover_letter_text": role_content_path(out_dir, "cover_letter_text.txt"),
    }


def _document_files(out_dir: str | Path, company_proper: str) -> dict[str, Path]:
    out_dir = Path(out_dir)
    migrate_role_output_layout(out_dir)
    return {
        "resume_docx": role_documents_path(out_dir, document_filename("Resume", company_proper, ".docx")),
        "resume_pdf": role_documents_path(out_dir, document_filename("Resume", company_proper, ".pdf")),
        "cover_letter_docx": role_documents_path(out_dir, document_filename("Cover Letter", company_proper, ".docx")),
        "cover_letter_pdf": role_documents_path(out_dir, document_filename("Cover Letter", company_proper, ".pdf")),
        "cover_letter_txt": role_documents_path(out_dir, document_filename("Cover Letter", company_proper, ".txt")),
    }


def can_reuse_content(out_dir: str | Path) -> bool:
    files = _content_files(out_dir)
    if not all(path.exists() for path in files.values()):
        return False
    state = _load_state(out_dir)
    expected = state.get("draft_sha256")
    if not expected:
        return False
    return expected == _sha256(files["draft"]) and _content_quality_ok(out_dir)


def _content_quality_ok(out_dir: str | Path) -> bool:
    """Quick sanity check on resume_content.json — reject invalid or stale reusable content."""
    rc_path = role_content_path(Path(out_dir), "resume_content.json")
    if not rc_path.exists():
        return False
    try:
        data = json.loads(rc_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not data.get("summary"):
        return False
    tagline = data.get("tagline") or ""
    if "Principal Product Manager" in tagline:
        return False
    return resume_meets_minimums(data)


def can_reuse_build(out_dir: str | Path, company_proper: str) -> bool:
    content_files = _content_files(out_dir)
    document_files = _document_files(out_dir, company_proper)
    if not all(path.exists() for path in content_files.values()):
        return False
    if not all(path.exists() for path in document_files.values()):
        return False
    if not _content_quality_ok(out_dir):
        return False
    state = _load_state(out_dir)
    expected_resume = state.get("resume_content_sha256")
    expected_cl = state.get("cover_letter_text_sha256")
    if not expected_resume or not expected_cl:
        return False
    return expected_resume == _sha256(content_files["resume_content"]) and expected_cl == _sha256(
        content_files["cover_letter_text"]
    )


def record_content(out_dir: str | Path) -> dict:
    files = _content_files(out_dir)
    if not all(path.exists() for path in files.values()):
        missing = [str(path) for path in files.values() if not path.exists()]
        raise FileNotFoundError(f"Missing content files: {', '.join(missing)}")
    state = _load_state(out_dir)
    state.update(
        {
            "recorded_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "draft_sha256": _sha256(files["draft"]),
        }
    )
    _write_state(out_dir, state)
    return state


def record_build(out_dir: str | Path, company_proper: str) -> dict:
    content_files = _content_files(out_dir)
    document_files = _document_files(out_dir, company_proper)
    if not all(path.exists() for path in content_files.values()):
        missing = [str(path) for path in content_files.values() if not path.exists()]
        raise FileNotFoundError(f"Missing content files: {', '.join(missing)}")
    if not all(path.exists() for path in document_files.values()):
        missing = [str(path) for path in document_files.values() if not path.exists()]
        raise FileNotFoundError(f"Missing document files: {', '.join(missing)}")
    state = record_content(out_dir)
    state.update(
        {
            "recorded_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "resume_content_sha256": _sha256(content_files["resume_content"]),
            "cover_letter_text_sha256": _sha256(content_files["cover_letter_text"]),
            "company_proper": company_proper,
        }
    )
    _write_state(out_dir, state)
    return state


def stash_generated_content(out_dir: str | Path) -> dict:
    files = _content_files(out_dir)
    moved: dict[str, str] = {}
    for key in ("resume_content", "cover_letter_text"):
        path = files[key]
        if not path.exists():
            continue
        backup_path = Path(f"{path}.stale")
        backup_path.unlink(missing_ok=True)
        path.replace(backup_path)
        moved[key] = str(backup_path)
    return moved


def _status_payload(out_dir: str | Path, company_proper: str) -> dict:
    return {
        "out_dir": str(Path(out_dir)),
        "state_path": str(state_path(out_dir)),
        "reuse_content": can_reuse_content(out_dir),
        "reuse_build": can_reuse_build(out_dir, company_proper),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="Print reuse status as JSON.")
    status_parser.add_argument("out_dir")
    status_parser.add_argument("company_proper")

    content_parser = subparsers.add_parser("can-reuse-content", help="Exit 0 when content can be reused.")
    content_parser.add_argument("out_dir")

    build_parser = subparsers.add_parser("can-reuse-build", help="Exit 0 when built docs can be reused.")
    build_parser.add_argument("out_dir")
    build_parser.add_argument("company_proper")

    record_content_parser = subparsers.add_parser("record-content", help="Record content state.")
    record_content_parser.add_argument("out_dir")

    record_build_parser = subparsers.add_parser("record-build", help="Record content and build state.")
    record_build_parser.add_argument("out_dir")
    record_build_parser.add_argument("company_proper")

    stash_content_parser = subparsers.add_parser(
        "stash-generated-content",
        help="Move stale generated content files aside before a fresh LLM content pass.",
    )
    stash_content_parser.add_argument("out_dir")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "status":
        print(json.dumps(_status_payload(args.out_dir, args.company_proper), indent=2, ensure_ascii=False))
        return 0
    if args.command == "can-reuse-content":
        return 0 if can_reuse_content(args.out_dir) else 1
    if args.command == "can-reuse-build":
        return 0 if can_reuse_build(args.out_dir, args.company_proper) else 1
    if args.command == "record-content":
        print(json.dumps(record_content(args.out_dir), indent=2, ensure_ascii=False))
        return 0
    if args.command == "record-build":
        print(json.dumps(record_build(args.out_dir, args.company_proper), indent=2, ensure_ascii=False))
        return 0
    if args.command == "stash-generated-content":
        print(json.dumps(stash_generated_content(args.out_dir), indent=2, ensure_ascii=False))
        return 0
    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
