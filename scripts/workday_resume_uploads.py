from __future__ import annotations

import re
import sys
from pathlib import Path


def _normalize_uploaded_filename_label(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold()).strip()


def _workday_resume_label_matches_filename(label: str | None, filename: str) -> bool:
    normalized_label = _normalize_uploaded_filename_label(label)
    if not normalized_label:
        return False
    normalized_filename = _normalize_uploaded_filename_label(Path(filename).name)
    normalized_stem = _normalize_uploaded_filename_label(Path(filename).stem)
    return (normalized_filename and normalized_filename in normalized_label) or (
        normalized_stem and normalized_stem in normalized_label
    )


def _workday_delete_label_looks_like_resume(label: str | None) -> bool:
    normalized_label = _normalize_uploaded_filename_label(label)
    if not normalized_label:
        return False
    return "resume" in normalized_label or re.search(r"\bcv\b", normalized_label) is not None


def _workday_resume_already_uploaded(page, filename: str) -> bool:
    """Return True when the current Workday page already lists the same uploaded resume."""
    if not filename:
        return False
    if _workday_uploaded_resume_delete_buttons(page, filename):
        return True
    normalized_filename = _normalize_uploaded_filename_label(Path(filename).name)
    normalized_stem = _normalize_uploaded_filename_label(Path(filename).stem)
    try:
        body_text = page.inner_text("body")
    except Exception:
        return False
    normalized_body = _normalize_uploaded_filename_label(body_text)
    if not normalized_body:
        return False
    filename_present = normalized_filename and normalized_filename in normalized_body
    stem_present = normalized_stem and normalized_stem in normalized_body
    if not filename_present and not stem_present:
        return False
    return "uploaded" in normalized_body or "resume cv" in normalized_body or "resume" in normalized_body


def _workday_uploaded_resume_delete_buttons(page, filename: str) -> list:
    """Return visible delete controls for uploaded resume cards matching ``filename``."""
    if not filename:
        return []
    buttons = page.locator("[data-automation-id='delete-file']")
    matches = []
    for index in range(buttons.count()):
        button = buttons.nth(index)
        try:
            aria_label = button.get_attribute("aria-label") or ""
        except Exception:
            continue
        if _workday_resume_label_matches_filename(aria_label, filename):
            matches.append(button)
    return matches


def _dedupe_workday_uploaded_resume_items(page, filename: str, *, keep: int = 1) -> int:
    """Delete stale or duplicate resume cards so Workday keeps a single current attachment."""
    buttons = page.locator("[data-automation-id='delete-file']")
    matching_buttons = []
    stale_resume_buttons = []
    for index in range(buttons.count()):
        button = buttons.nth(index)
        try:
            aria_label = button.get_attribute("aria-label") or ""
        except Exception:
            continue
        if _workday_resume_label_matches_filename(aria_label, filename):
            matching_buttons.append(button)
            continue
        if _workday_delete_label_looks_like_resume(aria_label):
            stale_resume_buttons.append(button)

    deleted = 0
    for button in reversed(stale_resume_buttons):
        try:
            button.click(force=True)
            page.wait_for_timeout(750)
            deleted += 1
        except Exception as exc:
            print(f"Workday: failed to remove stale uploaded resume: {exc}", file=sys.stderr)
            break
    for button in reversed(matching_buttons[keep:]):
        try:
            button.click(force=True)
            page.wait_for_timeout(750)
            deleted += 1
        except Exception as exc:
            print(f"Workday: failed to remove duplicate uploaded resume: {exc}", file=sys.stderr)
            break
    if deleted:
        print(f"Workday: removed {deleted} duplicate uploaded resume card(s) for {filename}", file=sys.stderr)
    return deleted
