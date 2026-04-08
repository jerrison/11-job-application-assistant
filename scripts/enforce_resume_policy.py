#!/usr/bin/env python3
"""Enforce deterministic resume content policies.

Current policies:
- Moody's must have at least 6 bullets.
- Kyte must have at least 5 bullets.
- T-Mobile must have at least 3 bullets.
- Lyft must have at least 1 bullet.
- Allstate must have at least 1 bullet.

When a position falls below its minimum, this script tops it up from the local
ranked_bullets.json file, preferring the highest-ranked bullets that are not
already materially represented in the current resume.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import json_lenient

MIN_BULLETS = {
    "moodys": 6,
    "kyte": 5,
    "tmobile": 3,
    "lyft": 1,
    "allstate": 1,
}

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "into",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "this",
    "to",
    "with",
    "without",
    "through",
    "across",
    "using",
    "used",
    "over",
    "under",
    "up",
    "down",
    "than",
    "then",
    "while",
    "where",
    "who",
    "which",
    "you",
    "your",
    "our",
    "its",
    "via",
}


def split_bold_text(bullet_text: str) -> dict[str, str]:
    """Split a bullet into {"bold": "...", "text": "..."}."""
    text = bullet_text.strip()

    pattern_after_number = re.compile(
        r"(.*?\d[\d,.]*[%KMB]?(?:\s+\w+){0,6}?[),.])\s+(.*)",
        re.DOTALL,
    )
    match = pattern_after_number.match(text)
    if match:
        bold_part = match.group(1).rstrip()
        rest = match.group(2)
        if len(bold_part) < len(text) * 0.65 and len(bold_part) > 10:
            return {"bold": bold_part + " ", "text": rest}

    transition_pattern = re.compile(
        r"^(.*?[,.])\s+"
        r"((?:by|enabling|transforming|achieving|unlocking|reducing|generating|"
        r"resulting|leading|expanding|creating|driving|increasing|establishing|"
        r"improving|launching|building|designing|partnering|delivering|"
        r"eliminating|automating|accelerating|balancing|ensuring|integrating|"
        r"performing|shortening|spanning|coordinating|streamlining)\s+.*)",
        re.IGNORECASE | re.DOTALL,
    )
    match = transition_pattern.match(text)
    if match:
        bold_part = match.group(1).rstrip()
        rest = match.group(2)
        if len(bold_part) < len(text) * 0.65 and len(bold_part) > 10:
            return {"bold": bold_part + " ", "text": rest}

    comma_idx = text.find(",")
    if comma_idx > 10 and comma_idx < len(text) * 0.65:
        return {"bold": text[: comma_idx + 1] + " ", "text": text[comma_idx + 2 :]}

    period_idx = text.find(".")
    if period_idx > 10 and period_idx < len(text) - 5:
        return {"bold": text[: period_idx + 1] + " ", "text": text[period_idx + 2 :]}

    return {"bold": text + " ", "text": ""}


def bullet_text(bullet: dict | str) -> str:
    if isinstance(bullet, dict):
        return f"{bullet.get('bold', '')}{bullet.get('text', '')}".strip()
    return str(bullet).strip()


def normalize_tokens(text: str) -> set[str]:
    raw_tokens = re.findall(r"[a-z0-9$%+]+", text.lower())
    return {token for token in raw_tokens if len(token) >= 3 and token not in STOPWORDS}


def materially_overlaps(candidate: str, existing_texts: list[str]) -> bool:
    candidate_tokens = normalize_tokens(candidate)
    if not candidate_tokens:
        return False

    for existing in existing_texts:
        existing_tokens = normalize_tokens(existing)
        if not existing_tokens:
            continue
        overlap = len(candidate_tokens & existing_tokens) / min(len(candidate_tokens), len(existing_tokens))
        if overlap >= 0.6:
            return True
    return False


def load_ranked_positions(resume_path: Path) -> dict:
    ranked_path = resume_path.with_name("ranked_bullets.json")
    if not ranked_path.exists():
        raise FileNotFoundError(f"Missing ranked bullets file: {ranked_path}")

    ranked_data = json_lenient.loads(ranked_path.read_text())
    return ranked_data.get("positions", ranked_data)


def minimum_bullet_deficits(data: dict) -> list[tuple[str, int, int]]:
    positions = data.get("positions", {}) if isinstance(data, dict) else {}
    deficits: list[tuple[str, int, int]] = []
    for position_id, minimum in MIN_BULLETS.items():
        current = len(positions.get(position_id, []) or [])
        if current < minimum:
            deficits.append((position_id, current, minimum))
    return deficits


def resume_meets_minimums(data: dict) -> bool:
    return not minimum_bullet_deficits(data)


def enforce_policies(resume_path: Path) -> tuple[bool, list[str]]:
    data = json_lenient.loads(resume_path.read_text())
    positions = data.setdefault("positions", {})
    ranked_positions = load_ranked_positions(resume_path)

    changed = False
    actions: list[str] = []

    for position_id, minimum in MIN_BULLETS.items():
        current_bullets = positions.setdefault(position_id, [])
        if len(current_bullets) >= minimum:
            continue

        current_texts = [bullet_text(item) for item in current_bullets]
        ranked_candidates = ranked_positions.get(position_id, [])

        for candidate in sorted(ranked_candidates, key=lambda item: item.get("score", 0), reverse=True):
            candidate_text = candidate.get("bullet", "").strip()
            if not candidate_text:
                continue
            if materially_overlaps(candidate_text, current_texts):
                continue

            current_bullets.append(split_bold_text(candidate_text))
            current_texts.append(candidate_text)
            changed = True
            actions.append(f"added {position_id} bullet: {candidate_text[:80]}")

            if len(current_bullets) >= minimum:
                break

        if len(current_bullets) < minimum:
            raise RuntimeError(
                f"Could not satisfy minimum bullet count for {position_id}: have {len(current_bullets)}, need {minimum}"
            )

    if changed:
        resume_path.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n")

    return changed, actions


def main() -> None:
    parser = argparse.ArgumentParser(description="Enforce deterministic resume content policies.")
    parser.add_argument("input", help="Path to resume_content.json")
    parser.add_argument("--check", action="store_true", help="Print policy status without modifying the file")
    args = parser.parse_args()

    resume_path = Path(args.input)
    if args.check:
        data = json_lenient.loads(resume_path.read_text())
        for position_id, current, minimum in minimum_bullet_deficits(data):
            print(f"FAIL: {position_id} has {current} bullets (minimum {minimum})")
        for position_id, minimum in MIN_BULLETS.items():
            current = len(data.get("positions", {}).get(position_id, []))
            if current >= minimum:
                print(f"OK: {position_id} has {current} bullets (minimum {minimum})")
        return

    changed, actions = enforce_policies(resume_path)
    if changed:
        print(f"Updated {resume_path}")
        for action in actions:
            print(f"  - {action}")
    else:
        print(f"No policy changes needed for {resume_path}")


if __name__ == "__main__":
    main()
