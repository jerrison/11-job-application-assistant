"""Standalone helpers for text normalization and multi-select answers."""

from __future__ import annotations

import re

NORMALIZED_TEXT_RE = re.compile(r"[^a-z0-9]+")
_NOT_APPLICABLE_ALIASES = frozenset({"na", "n a", "not applicable", "none", "none of the above"})


def normalize_text(value: str | None) -> str:
    return NORMALIZED_TEXT_RE.sub(" ", (value or "").casefold()).strip()


def slugify_label(value: str) -> str:
    slug = NORMALIZED_TEXT_RE.sub("_", value.casefold()).strip("_")
    return slug or "field"


def _spec_option_labels(spec: dict) -> list[str]:
    labels: list[str] = []
    raw_options = spec.get("values") or spec.get("options") or []
    for option in raw_options:
        if isinstance(option, dict):
            label = option.get("label") or option.get("value") or option.get("text") or ""
        else:
            label = option
        text = str(label or "").strip()
        if text:
            labels.append(text)
    return labels


def _canonicalize_selection_to_option_label(selection: str, option_labels: list[str]) -> str:
    text = str(selection or "").strip()
    if not text or not option_labels:
        return text

    normalized_selection = normalize_text(text)
    if not normalized_selection:
        return text

    normalized_options = {normalize_text(option): option for option in option_labels if normalize_text(option)}
    if normalized_selection in normalized_options:
        return normalized_options[normalized_selection]

    if normalized_selection in _NOT_APPLICABLE_ALIASES:
        for option in option_labels:
            if normalize_text(option) in _NOT_APPLICABLE_ALIASES:
                return option

    return text


def normalize_multi_select_generated_answers(
    question_specs: list[dict],
    answers: dict[str, object],
) -> dict[str, object]:
    normalized_answers = dict(answers)
    for spec in question_specs:
        if spec.get("type") != "multi_value_multi_select":
            continue
        field_name = str(spec.get("field_name") or "").strip()
        if not field_name or field_name not in normalized_answers:
            continue
        raw_value = normalized_answers[field_name]
        if isinstance(raw_value, str):
            selected = [item.strip() for item in raw_value.split(",") if item.strip()]
        elif isinstance(raw_value, list):
            selected = [str(item).strip() for item in raw_value if str(item).strip()]
        else:
            continue

        option_labels = _spec_option_labels(spec)
        canonical_selected: list[str] = []
        seen_selected: set[str] = set()
        for item in selected:
            canonical_item = _canonicalize_selection_to_option_label(item, option_labels)
            if canonical_item in seen_selected:
                continue
            seen_selected.add(canonical_item)
            canonical_selected.append(canonical_item)
        selected = canonical_selected
        normalized_label = normalize_text(spec.get("label"))
        wants_three = (
            "top 3" in normalized_label
            or "choose 3" in normalized_label
            or "choose three" in normalized_label
            or "at least three" in normalized_label
            or "most interested in" in normalized_label
            or "all that apply" in normalized_label
        )
        target_count = min(3, len(option_labels)) if wants_three else None
        if target_count and len(selected) < target_count:
            for option in option_labels:
                if option not in selected:
                    selected.append(option)
                if len(selected) >= target_count:
                    break
        normalized_answers[field_name] = selected
    return normalized_answers
