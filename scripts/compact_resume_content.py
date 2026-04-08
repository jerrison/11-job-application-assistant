#!/usr/bin/env python3
"""Deterministically compact overlong resume content while preserving bullet counts.

This is a last-resort fallback for resumes that still exceed 2 pages after the
LLM fix loop. It keeps the newest populated role fully detailed, compresses the
summary to one sentence, and collapses older-role bullets down to their lead
accomplishment clause.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import json_lenient

POSITION_ORDER = ("moodys", "kyte", "tmobile", "lyft", "allstate")
_TRAILING_PUNCTUATION_RE = re.compile(r"[\s,;:]+$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _ordered_populated_positions(data: dict) -> list[str]:
    positions = data.get("positions", {}) if isinstance(data, dict) else {}
    ordered = [pos_id for pos_id in POSITION_ORDER if positions.get(pos_id)]
    extras = [pos_id for pos_id, bullets in positions.items() if bullets and pos_id not in POSITION_ORDER]
    return ordered + extras


def _compact_summary(summary: str | None) -> str | None:
    if not isinstance(summary, str):
        return summary

    text = " ".join(summary.split()).strip()
    if not text:
        return summary

    sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]
    compact = sentences[0] if sentences else text
    if compact and compact[-1] not in ".!?":
        compact += "."
    return compact


def _compact_bullet(item: dict | str) -> tuple[dict | str, bool]:
    if not isinstance(item, dict):
        return item, False

    tail = str(item.get("text") or "").strip()
    if not tail:
        return item, False

    lead = str(item.get("bold") or "").rstrip()
    if lead:
        compact_lead = _TRAILING_PUNCTUATION_RE.sub("", lead)
    else:
        compact_lead = _compact_summary(tail) or ""

    if compact_lead and compact_lead[-1] not in ".!?":
        compact_lead += "."

    updated = dict(item)
    updated["bold"] = (compact_lead + " ") if compact_lead else ""
    updated["text"] = ""
    return updated, True


def compact_resume_content(data: dict) -> tuple[bool, list[str]]:
    positions = data.get("positions", {}) if isinstance(data, dict) else {}
    actions: list[str] = []
    changed = False

    compact_summary = _compact_summary(data.get("summary"))
    if compact_summary != data.get("summary"):
        data["summary"] = compact_summary
        actions.append("shortened summary to one sentence")
        changed = True

    populated = _ordered_populated_positions(data)
    if not populated:
        return changed, actions

    for pos_id in populated[1:]:
        bullets = positions.get(pos_id) or []
        role_changed = False
        updated_bullets: list[dict | str] = []
        for bullet in bullets:
            updated, bullet_changed = _compact_bullet(bullet)
            updated_bullets.append(updated)
            role_changed = role_changed or bullet_changed
        if role_changed:
            positions[pos_id] = updated_bullets
            actions.append(f"compacted older-role bullets for {pos_id}")
            changed = True

    return changed, actions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Path to resume_content.json")
    args = parser.parse_args()

    resume_path = Path(args.input)
    data = json_lenient.loads(resume_path.read_text(encoding="utf-8"))
    changed, actions = compact_resume_content(data)

    if changed:
        resume_path.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Updated {resume_path}")
        for action in actions:
            print(f"  - {action}")
    else:
        print(f"No compaction changes needed for {resume_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
