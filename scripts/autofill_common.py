"""Shared utilities for board-specific autofill scripts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_submit_common import (
    APPLICATION_PROFILE_PATH,
    json_dumps_pretty,
    normalize_text,
    parse_application_profile,
    resolve_shared_question_policy,
)
from output_layout import (
    ANSWER_VERIFICATION_JSON,
    ANSWER_VERIFICATION_RAW,
    APPLICATION_ANSWER_CACHE,
    APPLICATION_ANSWER_FALLBACK_RAW,
    APPLICATION_ANSWER_RAW,
)

VISIBLE_PROFILE_FIELD_BLOCKER_KIND = "visible_profile_field"
VISIBLE_SELF_ID_BLOCKER_KIND = "visible_self_id"
GENERATED_ANSWER_BLOCKER_KIND = "generated_answer"
REQUIRED_ARTIFACT_BLOCKER_KIND = "required_artifact"
_DEFAULT_VALIDATION_PATTERNS = (
    re.compile(r"\bthis field is required\b", re.I),
    re.compile(r"\bplease (?:enter|select|complete|fill)\b", re.I),
    re.compile(r"\binvalid\b", re.I),
    re.compile(r"\berror\b", re.I),
)
_TRANSIENT_CAPTURE_OVERLAY_SELECTORS = (
    ".dropdown-results",
    ".dropdown-container",
    ".dropdown-no-results",
    ".dropdown-loading-results",
    "[role='listbox']",
    "[role='menu']",
    "[data-reach-combobox-popover]",
    "[class*='autocomplete__menu']",
    "[class*='select__menu']",
)
# Greenhouse stops on the live application form itself; a second "review"
# screenshot was just a duplicated copy of the same stitched page.
_DISTINCT_REVIEW_SCREENSHOT_BOARDS = frozenset()


def board_file_constants(board_name: str) -> dict[str, str]:
    """Generate standard artifact filenames for a board."""
    return {
        "report_md": f"{board_name}_autofill_report.md",
        "report_json": f"{board_name}_autofill_report.json",
        "pre_submit_screenshot": f"{board_name}_autofill_pre_submit.png",
        "review_screenshot": f"{board_name}_autofill_review.png",
        "post_submit_screenshot": f"{board_name}_autofill_post_submit.png",
        "page_screenshots_dir": f"{board_name}_autofill_pages",
        "unknown_questions_json": f"{board_name}_unknown_questions.json",
        "submit_debug_html": f"{board_name}_submit_debug.html",
        "submit_debug_screenshot": f"{board_name}_submit_debug.png",
        "payload_json": f"{board_name}_autofill_payload.json",
        "application_page_html": f"{board_name}_application_page.html",
    }


def board_requires_distinct_review_screenshot(board_name: str | None) -> bool:
    normalized = str(board_name or "").strip().casefold()
    return normalized in _DISTINCT_REVIEW_SCREENSHOT_BOARDS


def _artifact_sha1(path: Path) -> str | None:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _artifact_dhash(path: Path, *, size: int = 16) -> tuple[int, ...] | None:
    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        with Image.open(path) as image:
            resized = image.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
            pixels = list(resized.tobytes())
    except OSError:
        return None

    bits: list[int] = []
    row_width = size + 1
    for row in range(size):
        row_offset = row * row_width
        for column in range(size):
            left = pixels[row_offset + column]
            right = pixels[row_offset + column + 1]
            bits.append(1 if left > right else 0)
    return tuple(bits)


def _artifact_dhash_distance(left: tuple[int, ...] | None, right: tuple[int, ...] | None) -> int | None:
    if left is None or right is None or len(left) != len(right):
        return None
    return sum(a != b for a, b in zip(left, right, strict=True))


def dedupe_page_screenshot_artifacts(
    page_screenshots: list[str] | None,
    *,
    pre_submit_screenshot: str | None = None,
    review_screenshot: str | None = None,
) -> list[str]:
    """Drop duplicate page screenshots, preferring the final review/pre-submit artifact.

    When single-page boards save both `page_01.png` and a stitched final
    `pre_submit.png`, those files are often identical. Keep the canonical
    pre-submit screenshot and remove duplicate page artifacts from both the
    report payload and disk.
    """

    deduped_paths: list[str] = []
    seen_paths: set[str] = set()
    for raw_path in page_screenshots or []:
        path = str(raw_path or "").strip()
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        deduped_paths.append(path)

    anchor_hashes: set[str] = set()
    for raw_anchor in (pre_submit_screenshot, review_screenshot):
        anchor_path = Path(str(raw_anchor or "").strip())
        if not raw_anchor or not anchor_path.exists():
            continue
        anchor_hash = _artifact_sha1(anchor_path)
        if anchor_hash:
            anchor_hashes.add(anchor_hash)

    kept_paths: list[str] = []
    seen_hashes: set[str] = set()
    previous_dhash: tuple[int, ...] | None = None
    for raw_path in deduped_paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        path_hash = _artifact_sha1(path)
        if path_hash and (path_hash in anchor_hashes or path_hash in seen_hashes):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        path_dhash = _artifact_dhash(path)
        if previous_dhash is not None:
            dhash_distance = _artifact_dhash_distance(previous_dhash, path_dhash)
            if dhash_distance is not None and dhash_distance <= 5:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
        if path_hash:
            seen_hashes.add(path_hash)
        if path_dhash is not None:
            previous_dhash = path_dhash
        kept_paths.append(raw_path)

    return kept_paths


def _resolve_report_markdown_path(artifacts: dict[str, str]) -> str:
    """Return the configured report markdown path, honoring both artifact keys."""

    for key in ("report_markdown", "report_md"):
        raw = str(artifacts.get(key) or "").strip()
        if raw:
            return raw
    raise KeyError("missing report markdown artifact path")


def clear_current_attempt_artifacts(payload: dict, *, preserve_answer_artifacts: bool = False) -> None:
    """Remove stale review/debug/result artifacts before a fresh autofill run."""

    artifacts = dict(payload.get("artifacts") or {})
    removable_artifact_keys = [
        "report_markdown",
        "report_md",
        "report_json",
        "pre_submit_screenshot",
        "review_screenshot",
        "post_submit_screenshot",
        "submit_debug_html",
        "submit_debug_screenshot",
        "application_answers_raw",
        "application_answers_fallback_raw",
        "answer_verification_json",
        "answer_verification_raw",
    ]
    if preserve_answer_artifacts:
        removable_artifact_keys = [
            key
            for key in removable_artifact_keys
            if key
            not in {
                "application_answers_raw",
                "application_answers_fallback_raw",
                "answer_verification_json",
                "answer_verification_raw",
            }
        ]
    for key in removable_artifact_keys:
        raw = str(artifacts.get(key) or "").strip()
        if not raw:
            continue
        try:
            Path(raw).unlink(missing_ok=True)
        except OSError:
            pass

    submit_dir: Path | None = None
    for key in ("report_json", "report_markdown", "report_md", "pre_submit_screenshot", "payload_json"):
        raw = str(artifacts.get(key) or "").strip()
        if raw:
            submit_dir = Path(raw).parent
            break
    if submit_dir is None:
        out_dir_raw = str(payload.get("out_dir") or "").strip()
        if out_dir_raw:
            submit_dir = Path(out_dir_raw) / "submit"
    if submit_dir is not None:
        try:
            (submit_dir / "application_submission_result.json").unlink(missing_ok=True)
        except OSError:
            pass
        try:
            (submit_dir / "pending_user_input.json").unlink(missing_ok=True)
        except OSError:
            pass
        for pattern in (
            "*_autofill_report.md",
            "*_autofill_report.json",
            "*_autofill_pre_submit.png",
            "*_autofill_review.png",
            "*_autofill_post_submit.png",
            "*_submit_debug.html",
            "*_submit_debug.png",
        ):
            for candidate in submit_dir.glob(pattern):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    continue
        stale_names = [
            APPLICATION_ANSWER_CACHE,
            APPLICATION_ANSWER_RAW,
            APPLICATION_ANSWER_FALLBACK_RAW,
            ANSWER_VERIFICATION_JSON,
            ANSWER_VERIFICATION_RAW,
        ]
        if preserve_answer_artifacts:
            stale_names = []
        for stale_name in stale_names:
            try:
                (submit_dir / stale_name).unlink(missing_ok=True)
            except OSError:
                continue
        for stale_pages_dir in submit_dir.glob("*_autofill_pages"):
            if not stale_pages_dir.is_dir():
                continue
            for child in stale_pages_dir.iterdir():
                try:
                    if child.is_file():
                        child.unlink(missing_ok=True)
                except OSError:
                    continue

    pages_dir_raw = str(artifacts.get("page_screenshots_dir") or "").strip()
    if not pages_dir_raw:
        return
    pages_dir = Path(pages_dir_raw)
    if not pages_dir.exists():
        return
    for child in pages_dir.iterdir():
        try:
            if child.is_file():
                child.unlink(missing_ok=True)
        except OSError:
            continue


def label_matches(
    text_or_field: str | dict,
    *fragments: str,
    word_boundary: bool = False,
) -> bool:
    """Check if text or field label matches any of the given fragments.

    Args:
        text_or_field: Raw text string or a dict with a "label" key.
        *fragments: One or more substrings to match against.
        word_boundary: If True, use alphanumeric-boundary regex matching
            (replicates Lever's ``(?<![a-z0-9])...(?![a-z0-9])`` pattern).
            If False, use simple substring containment.
    """
    if isinstance(text_or_field, dict):
        text = text_or_field.get("label", "")
    else:
        text = text_or_field
    normalized = normalize_text(text)
    if word_boundary:
        return any(
            re.search(
                rf"(?<![a-z0-9]){re.escape(normalize_text(f))}(?![a-z0-9])",
                normalized,
            )
            for f in fragments
        )
    return any(normalize_text(f) in normalized for f in fragments)


def select_option(
    options: list[str] | None,
    answer: str | None,
    *,
    filter_select_prefix: bool = False,
) -> str | None:
    """Fuzzy-match an answer to a list of option strings.

    Args:
        options: Available options to match against.
        answer: The desired answer text.
        filter_select_prefix: If True, exclude options starting with "select"
            (e.g. "Select an option") before matching. Used by Lever.
    """
    normalized_answer = normalize_text(answer)
    if not normalized_answer:
        return None

    raw_options = options or []
    if filter_select_prefix:
        raw_options = [o for o in raw_options if not normalize_text(o).startswith("select")]

    normalized_options = [(option, normalize_text(option)) for option in raw_options if normalize_text(option)]

    negation_tokens = {"not", "non", "no", "without"}

    def contains_candidate(normalized_candidate: str, normalized_option: str) -> bool:
        if normalized_candidate not in normalized_option:
            return False

        pattern = re.compile(
            rf"(?<![a-z0-9]){re.escape(normalized_candidate)}(?![a-z0-9])",
        )
        matches = list(pattern.finditer(normalized_option))
        if not matches:
            return False

        candidate_is_negated = normalized_candidate.split(" ", 1)[0] in negation_tokens
        if candidate_is_negated:
            return True

        for match in matches:
            prefix_tokens = normalized_option[: match.start()].split()
            if any(token in negation_tokens for token in prefix_tokens[-3:]):
                continue
            return True
        return False

    # Exact match
    for option, normalized_option in normalized_options:
        if normalized_option == normalized_answer:
            return option
    # Substring containment (either direction)
    for option, normalized_option in normalized_options:
        if contains_candidate(normalized_answer, normalized_option) or contains_candidate(
            normalized_option,
            normalized_answer,
        ):
            return option
    return None


def detect_live_required_unfilled_fields(page, *, steps: list[dict]) -> list[dict]:
    """Return required visible live-form fields that are empty and not modeled by planned steps."""

    known_identities = {
        normalized
        for step in steps
        if isinstance(step, dict)
        for normalized in (
            normalize_text(step.get("field_name", "")),
            normalize_text(step.get("label", "")),
        )
        if normalized
    }
    try:
        raw_fields = page.evaluate(
            """() => {
                const isVisible = (el) => {
                  if (!el) return false;
                  const style = window.getComputedStyle(el);
                  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                  const rect = el.getBoundingClientRect();
                  return rect.width > 0 && rect.height > 0;
                };
                const labelFor = (el) => {
                  if (el.labels && el.labels.length) {
                    const joined = Array.from(el.labels)
                      .map((label) => (label.innerText || '').trim())
                      .filter(Boolean)
                      .join(' ');
                    if (joined) return joined;
                  }
                  const ariaLabel = (el.getAttribute('aria-label') || '').trim();
                  if (ariaLabel) return ariaLabel;
                  const labelledBy = (el.getAttribute('aria-labelledby') || '').trim();
                  if (labelledBy) {
                    const text = labelledBy
                      .split(/\\s+/)
                      .map((id) => (document.getElementById(id)?.innerText || '').trim())
                      .filter(Boolean)
                      .join(' ');
                    if (text) return text;
                  }
                  if (el.id) {
                    const explicit = document.querySelector(`label[for="${el.id}"]`);
                    const explicitText = (explicit?.innerText || '').trim();
                    if (explicitText) return explicitText;
                  }
                  const container = el.closest('label, .form-group, .field, [class*="field"], td, li, div');
                  if (!container) return '';
                  const firstLine = (container.innerText || '')
                    .split('\\n')
                    .map((part) => part.trim())
                    .find(Boolean);
                  return firstLine || '';
                };
                const controls = Array.from(document.querySelectorAll('input, select, textarea'));
                const rows = [];
                for (const el of controls) {
                  if (!isVisible(el) || el.disabled) continue;
                  const tag = el.tagName.toLowerCase();
                  const type = (el.getAttribute('type') || '').toLowerCase();
                  if (['hidden', 'file', 'radio', 'checkbox', 'submit', 'button'].includes(type)) continue;
                  const label = labelFor(el);
                  const required = Boolean(el.required || el.getAttribute('aria-required') === 'true' || /\\*/.test(label));
                  if (!required) continue;
                  let empty = false;
                  if (tag === 'select') {
                    const selectedText = el.selectedIndex >= 0 ? (el.options[el.selectedIndex]?.text || '') : '';
                    empty = !String(el.value || '').trim() || /^select\\b/i.test(String(selectedText || '').trim());
                  } else {
                    empty = !String(el.value || '').trim();
                  }
                  if (!empty) continue;
                  rows.push({
                    field_name: String(el.getAttribute('name') || el.id || '').trim(),
                    label: String(label || '').trim(),
                    kind: tag === 'select' ? 'select' : (tag === 'textarea' ? 'textarea' : 'text'),
                  });
                }
                return rows;
            }"""
        )
    except Exception:
        return []

    blockers: list[dict] = []
    seen_identities: set[str] = set()
    for raw_field in list(raw_fields or []):
        if not isinstance(raw_field, dict):
            continue
        field_name = str(raw_field.get("field_name") or "").strip()
        label = str(raw_field.get("label") or field_name or "Required field").strip()
        identities = {
            normalized
            for normalized in (
                normalize_text(field_name),
                normalize_text(label),
            )
            if normalized
        }
        if identities & known_identities:
            continue
        dedupe_key = next(iter(identities), "")
        if dedupe_key and dedupe_key in seen_identities:
            continue
        if dedupe_key:
            seen_identities.add(dedupe_key)
        blockers.append(
            {
                "field_name": field_name or label,
                "label": label,
                "kind": str(raw_field.get("kind") or "text"),
                "source": "live_application_form",
                "required": True,
                "status": "planned",
                "reason": (
                    f"The live application form still shows required field {label} empty, but the current payload "
                    "did not plan a confirmed answer for it."
                ),
                "note": "Visible required live-form field discovered during draft verification.",
            }
        )
    return blockers


def simple_board_review_boundary_blocker(page) -> dict | None:
    """Return a blocker when a simple-board draft is still on a next-step boundary."""

    try:
        visible_buttons = page.evaluate(
            """() => {
                const isVisible = (el) => {
                  if (!el) return false;
                  const style = window.getComputedStyle(el);
                  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                  const rect = el.getBoundingClientRect();
                  return rect.width > 0 && rect.height > 0;
                };
                const textFor = (el) =>
                  String(el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
                return Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"]'))
                  .filter((el) => isVisible(el))
                  .map((el) => textFor(el))
                  .filter(Boolean);
            }"""
        )
    except Exception:
        return None

    normalized_buttons = [normalize_text(value) for value in list(visible_buttons or []) if normalize_text(value)]
    if not any(value == "next" or value.startswith("next ") for value in normalized_buttons):
        return None
    return {
        "field_name": "final_review_boundary",
        "label": "Final review boundary",
        "kind": "state",
        "source": "simple_board_pipeline",
        "required": True,
        "status": "planned",
        "reason": (
            "The draft stopped on an intermediate step with a visible Next action instead of a final review / "
            "ready-to-submit screen."
        ),
        "note": "Simple-board draft proof must stop at the final review boundary, not an earlier multi-step page.",
    }


_LOCATION_CURRENT_OPTION_FRAGMENTS = (
    "currently live",
    "current live",
    "currently based",
    "currently located",
    "already live",
    "already based",
    "already located",
    "already in",
    "live in",
    "reside in",
    "based in",
    "located in",
    "within commuting distance",
    "commuting distance",
    "commute",
    "able to commute",
    "can commute",
    "meet this requirement",
    "able to meet",
    "can meet",
    "work from the required location",
    "work in person",
    "work in-person",
    "work hybrid",
    "able to work hybrid",
    "can work hybrid",
    "work onsite",
    "work on site",
)
_LOCATION_RELOCATION_OPTION_FRAGMENTS = (
    "relocat",
    "move to",
    "moving to",
)
_LOCATION_NEGATIVE_OPTION_FRAGMENTS = (
    "do not",
    "dont",
    "can t",
    "cannot",
    "unable",
    "not able",
    "not willing",
)
_LOCATION_POLICY_CATEGORIES = frozenset({"office_attendance", "location_residency", "relocation_willingness"})
_EXPLICIT_STATE_LIST_FRAGMENTS = (
    "one of the following states",
    "one of these states",
    "any of these states",
    "any of the following states",
)
_US_STATE_ABBREVIATIONS = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "DC": "District of Columbia",
}
_US_STATE_NAME_TO_ABBREVIATION = {
    normalize_text(name): abbreviation for abbreviation, name in _US_STATE_ABBREVIATIONS.items()
}


def _normalized_location_markers(location: str | None) -> list[str]:
    parts = [part.strip() for part in str(location or "").split(",") if part.strip()]
    if not parts:
        return []

    candidates = [str(location or "").strip(), parts[0]]
    if len(parts) >= 2:
        candidates.append(parts[1])
    if normalize_text(parts[0]) == "san francisco":
        candidates.extend(["Bay Area", "SF"])
    if normalize_text(parts[0]) == "new york city":
        candidates.append("NYC")

    markers: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = normalize_text(candidate)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        markers.append(normalized)
    return markers


def _candidate_us_state_abbreviation(location: str | None) -> str:
    parts = [part.strip() for part in str(location or "").split(",") if part.strip()]
    candidates: list[str] = []
    if len(parts) >= 2:
        candidates.append(parts[1])
    if parts:
        candidates.append(parts[-1])
    candidates.append(str(location or ""))

    for candidate in candidates:
        token = candidate.strip().upper()
        if len(token) == 2 and token in _US_STATE_ABBREVIATIONS:
            return token
        normalized = normalize_text(candidate)
        abbreviation = _US_STATE_NAME_TO_ABBREVIATION.get(normalized)
        if abbreviation:
            return abbreviation
        for state_name, state_abbreviation in _US_STATE_NAME_TO_ABBREVIATION.items():
            if re.search(rf"\b{re.escape(state_name)}\b", normalized):
                return state_abbreviation
    return ""


def _listed_us_state_abbreviations(*texts: str | None) -> set[str]:
    raw_combined = " ".join(str(text or "") for text in texts if str(text or "").strip())
    listed = {token for token in re.findall(r"\b[A-Z]{2}\b", raw_combined) if token in _US_STATE_ABBREVIATIONS}

    normalized = normalize_text(raw_combined)
    for abbreviation, state_name in _US_STATE_ABBREVIATIONS.items():
        if re.search(rf"\b{re.escape(normalize_text(state_name))}\b", normalized):
            listed.add(abbreviation)
    return listed


def explicit_us_state_list_membership_answer(*texts: str | None, application_profile) -> bool | None:
    normalized_texts = [normalize_text(text) for text in texts if normalize_text(text)]
    if not normalized_texts:
        return None

    combined = " ".join(normalized_texts)
    if not any(fragment in combined for fragment in _EXPLICIT_STATE_LIST_FRAGMENTS):
        return None

    listed_states = _listed_us_state_abbreviations(*texts)
    if not listed_states:
        return None

    candidate_state = _candidate_us_state_abbreviation(getattr(application_profile, "location", None))
    if not candidate_state:
        return None

    return candidate_state in listed_states


def select_location_positive_fit_option(
    options: list[str] | None,
    *,
    application_profile,
    filter_select_prefix: bool = False,
    context_text: str | None = None,
) -> str | None:
    """Prefer current-location answers over relocation when options encode both.

    This targets hybrid / office-attendance prompts whose affirmative choices are
    phrased as location-specific statements instead of plain Yes/No.
    """

    raw_options = options or []
    if filter_select_prefix:
        raw_options = [option for option in raw_options if not normalize_text(option).startswith("select")]
    normalized_options = [(option, normalize_text(option)) for option in raw_options if normalize_text(option)]
    if not normalized_options:
        return None

    candidate_markers = _normalized_location_markers(getattr(application_profile, "location", None))
    normalized_context = normalize_text(context_text)
    context_markers = [marker for marker in candidate_markers if len(marker) > 2 or marker in {"sf", "nyc", "dc"}]

    def matches_any_marker(normalized_option: str) -> bool:
        return any(select_option([normalized_option], marker) == normalized_option for marker in candidate_markers)

    def has_fragment(normalized_option: str, fragments: tuple[str, ...]) -> bool:
        return any(fragment in normalized_option for fragment in fragments)

    context_mentions_candidate = bool(
        normalized_context
        and any(select_option([normalized_context], marker) == normalized_context for marker in context_markers)
    )

    for option, normalized_option in normalized_options:
        if (
            matches_any_marker(normalized_option)
            and has_fragment(normalized_option, _LOCATION_CURRENT_OPTION_FRAGMENTS)
            and not has_fragment(normalized_option, _LOCATION_NEGATIVE_OPTION_FRAGMENTS)
            and not has_fragment(normalized_option, _LOCATION_RELOCATION_OPTION_FRAGMENTS)
        ):
            return option

    if context_mentions_candidate:
        for option, normalized_option in normalized_options:
            if (
                has_fragment(normalized_option, _LOCATION_CURRENT_OPTION_FRAGMENTS)
                and not has_fragment(normalized_option, _LOCATION_NEGATIVE_OPTION_FRAGMENTS)
                and not has_fragment(normalized_option, _LOCATION_RELOCATION_OPTION_FRAGMENTS)
            ):
                return option

    for option, normalized_option in normalized_options:
        if has_fragment(normalized_option, _LOCATION_RELOCATION_OPTION_FRAGMENTS) and not has_fragment(
            normalized_option, _LOCATION_NEGATIVE_OPTION_FRAGMENTS
        ):
            return option

    return None


def _application_profile_country_markers(application_profile) -> tuple[str, ...]:
    raw_values = (
        getattr(application_profile, "country", None),
        getattr(application_profile, "location", None),
    )
    markers: list[str] = []
    for raw in raw_values:
        if not isinstance(raw, str):
            continue
        normalized = normalize_text(raw)
        if not normalized:
            continue
        if any(fragment in normalized for fragment in ("united states", "u s a", "u s")):
            markers.append("united states")
        if "canada" in normalized:
            markers.append("canada")
        if "united kingdom" in normalized or normalized in {"uk", "u k"}:
            markers.append("united kingdom")
    return tuple(dict.fromkeys(markers))


def _select_location_based_na_work_authorization_option(options: list[str] | None, application_profile) -> str | None:
    normalized_options = [(option, normalize_text(option)) for option in options or [] if normalize_text(option)]
    if not normalized_options:
        return None

    country_markers = _application_profile_country_markers(application_profile)
    if not country_markers:
        return None

    for option, normalized_option in normalized_options:
        if not normalized_option.startswith(("n a", "na", "not applicable")):
            continue
        if not any(fragment in normalized_option for fragment in ("based in", "located in", "reside in", "live in")):
            continue
        if any(marker in normalized_option for marker in country_markers):
            return option
    return None


def _select_work_authorization_policy_option(
    options: list[str] | None,
    *,
    expects_yes: bool,
    application_profile,
) -> str | None:
    normalized_options = [(option, normalize_text(option)) for option in options or [] if normalize_text(option)]
    if not normalized_options:
        return None

    location_based_na = _select_location_based_na_work_authorization_option(options, application_profile)
    if location_based_na is not None:
        return location_based_na

    exact_boolean_option = next(
        (
            option
            for option, normalized_option in normalized_options
            if normalized_option == ("yes" if expects_yes else "no")
        ),
        None,
    )
    if exact_boolean_option is not None:
        return exact_boolean_option

    def first_matching(include_fragments: tuple[str, ...], *, exclude_fragments: tuple[str, ...] = ()) -> str | None:
        for option, normalized_option in normalized_options:
            if not any(fragment in normalized_option for fragment in include_fragments):
                continue
            if any(fragment in normalized_option for fragment in exclude_fragments):
                continue
            return option
        return None

    authorization_style = any(
        any(
            fragment in normalized_option
            for fragment in (
                "authorized to work",
                "authorised to work",
                "not authorized to work",
                "not authorised to work",
                "due to my nationality",
                "can work for any employer",
                "authorized to work for any employer",
                "permanent resident",
                "green card",
                "us citizen",
            )
        )
        for _, normalized_option in normalized_options
    )
    if authorization_style:
        if expects_yes:
            return first_matching(
                (
                    "authorized to work",
                    "authorised to work",
                    "due to my nationality",
                    "can work for any employer",
                    "authorized to work for any employer",
                    "permanent resident",
                    "green card",
                    "us citizen",
                ),
                exclude_fragments=("not authorized", "not authorised"),
            )
        return first_matching(
            (
                "not authorized to work",
                "not authorised to work",
                "need visa support",
                "seeking work authorization",
                "can work for current employer",
                "need sponsorship",
                "require sponsorship",
            )
        )

    sponsorship_style = any(
        any(
            fragment in normalized_option
            for fragment in (
                "sponsorship",
                "visa support",
                "work permit",
                "visa sponsorship",
            )
        )
        for _, normalized_option in normalized_options
    )
    if sponsorship_style:
        if expects_yes:
            return first_matching(
                (
                    "yes i require",
                    "require sponsorship",
                    "need sponsorship",
                    "need visa support",
                    "seeking work authorization",
                ),
                exclude_fragments=("do not require", "do not need", "no sponsorship"),
            )
        return first_matching(
            (
                "no i do not require",
                "do not require sponsorship",
                "do not require visa sponsorship",
                "do not require work permit sponsorship",
                "no sponsorship needed",
                "do not need a company to sponsor",
                "do not need sponsorship",
            )
        )

    return None


def _select_prior_employment_policy_option(options: list[str] | None, *, expects_yes: bool) -> str | None:
    if expects_yes:
        return None

    normalized_options = [(option, normalize_text(option)) for option in options or [] if normalize_text(option)]
    if not normalized_options:
        return None

    negative_fragments = (
        "i have never been employed",
        "never been employed",
        "never worked",
        "never provided services",
        "i have not worked",
        "have not worked",
        "have not been employed",
        "have not previously worked",
        "not worked for",
        "not worked at",
        "none of the above",
    )
    for option, normalized_option in normalized_options:
        if any(fragment in normalized_option for fragment in negative_fragments):
            return option
    return None


def _select_interview_recording_consent_policy_option(
    options: list[str] | None,
    *,
    consents: bool,
) -> str | None:
    normalized_options = [(option, normalize_text(option)) for option in options or [] if normalize_text(option)]
    if not normalized_options:
        return None

    if consents:
        for option, normalized_option in normalized_options:
            if any(fragment in normalized_option for fragment in ("do not consent", "decline", "prefer not")):
                continue
            if any(
                fragment in normalized_option for fragment in ("provide consent", "i consent", "agree", "acknowledge")
            ):
                return option
        for option, normalized_option in normalized_options:
            if "consent" in normalized_option and "not" not in normalized_option:
                return option
    else:
        for option, normalized_option in normalized_options:
            if any(fragment in normalized_option for fragment in ("do not consent", "decline", "prefer not")):
                return option
    return None


def _select_availability_timing_policy_option(options: list[str] | None, answer: str | None) -> str | None:
    normalized_answer = normalize_text(answer)
    if not normalized_answer:
        return None
    normalized_options = [(option, normalize_text(option)) for option in options or [] if normalize_text(option)]
    if not normalized_options:
        return None

    if any(fragment in normalized_answer for fragment in ("2 weeks", "two weeks")):
        for option, normalized_option in normalized_options:
            if any(
                fragment in normalized_option
                for fragment in ("within 2 weeks", "within two weeks", "2 weeks", "two weeks")
            ):
                return option
        for option, normalized_option in normalized_options:
            if any(fragment in normalized_option for fragment in ("14 days", "15 days", "2 week")):
                return option
    return None


def _select_skill_years_policy_option(options: list[str] | None, years_text: str | None) -> str | None:
    if not years_text:
        return None
    years_match = re.search(r"\b(\d{1,2})\b", years_text)
    if not years_match:
        return None
    years_value = int(years_match.group(1))
    normalized_options = [
        (str(option).strip(), normalize_text(option))
        for option in options or []
        if normalize_text(option)
    ]
    if not normalized_options:
        return None

    for option, normalized_option in normalized_options:
        if normalized_option == str(years_value) or normalized_option == f"{years_value} years":
            return option

    for option, normalized_option in normalized_options:
        raw_option = option.casefold()
        if match := re.search(r"\b(?P<low>\d{1,2})\s*(?:[-–—]|to)\s*(?P<high>\d{1,2})\s*(?:years?)?\b", raw_option):
            low = int(match.group("low"))
            high = int(match.group("high"))
            if low <= years_value <= high:
                return option
        if match := re.search(r"\b(?P<low>\d{1,2})\s+(?P<high>\d{1,2})\s+years?\b", normalized_option):
            low = int(match.group("low"))
            high = int(match.group("high"))
            if low <= years_value <= high:
                return option
        if match := re.search(
            r"\b(?P<low>\d{1,2})\s*(?:\+|plus)\s*(?:years?)?\b",
            raw_option,
        ):
            low = int(match.group("low"))
            if years_value >= low:
                return option
        if match := re.search(
            r"\b(?P<low>\d{1,2})\s*(?:years?)?\s*(?:or more|and above|and up)\b",
            normalized_option,
        ):
            low = int(match.group("low"))
            if years_value >= low:
                return option
        if match := re.search(r"\b(?:at least|minimum of)\s*(?P<low>\d{1,2})\s*(?:years?)?\b", normalized_option):
            low = int(match.group("low"))
            if years_value >= low:
                return option
        if match := re.search(r"\b(?:less than|under)\s*(?P<high>\d{1,2})\s*(?:years?)?\b", normalized_option):
            high = int(match.group("high"))
            if years_value < high:
                return option
    for option, normalized_option in normalized_options:
        if match := re.search(r"\b(?P<exact>\d{1,2})\s+years?\b", normalized_option):
            if years_value == int(match.group("exact")):
                return option
    return None


def _select_city_location_policy_option(
    options: list[str] | None,
    *,
    application_profile,
    desired_location: str | None,
    filter_select_prefix: bool = False,
) -> str | None:
    raw_options = options or []
    if filter_select_prefix:
        raw_options = [option for option in raw_options if not normalize_text(option).startswith("select")]
    if not raw_options:
        return None

    desired = str(desired_location or getattr(application_profile, "location", "") or "").strip()
    exact = select_option(raw_options, desired)
    if exact is not None:
        return exact

    markers = _normalized_location_markers(desired)
    desired_state = _candidate_us_state_abbreviation(desired)
    normalized_options = [(option, normalize_text(option)) for option in raw_options if normalize_text(option)]

    for option, normalized_option in normalized_options:
        option_state = _candidate_us_state_abbreviation(option)
        if desired_state and option_state and option_state != desired_state:
            continue
        if any(select_option([normalized_option], marker) == normalized_option for marker in markers):
            return option

    if desired_state:
        for option, _ in normalized_options:
            if _candidate_us_state_abbreviation(option) == desired_state:
                return option
    return None


def select_shared_policy_option(
    options: list[str] | None,
    policy,
    *,
    application_profile,
    filter_select_prefix: bool = False,
) -> str | None:
    """Match a shared deterministic policy against board option labels."""

    if policy is None or getattr(policy, "text_value", None) is None:
        return None

    if getattr(policy, "category", None) in _LOCATION_POLICY_CATEGORIES:
        matched = select_location_positive_fit_option(
            options,
            application_profile=application_profile,
            filter_select_prefix=filter_select_prefix,
        )
        if matched is not None:
            return matched

    if getattr(policy, "category", None) == "city_location":
        matched = _select_city_location_policy_option(
            options,
            application_profile=application_profile,
            desired_location=getattr(policy, "text_value", None),
            filter_select_prefix=filter_select_prefix,
        )
        if matched is not None:
            return matched

    if getattr(policy, "category", None) == "work_authorization" and getattr(policy, "boolean_value", None) is not None:
        matched = _select_work_authorization_policy_option(
            options,
            expects_yes=bool(policy.boolean_value),
            application_profile=application_profile,
        )
        if matched is not None:
            return matched

    if getattr(policy, "category", None) == "prior_employment" and getattr(policy, "boolean_value", None) is not None:
        matched = _select_prior_employment_policy_option(
            options,
            expects_yes=bool(policy.boolean_value),
        )
        if matched is not None:
            return matched

    if (
        getattr(policy, "category", None) == "interview_recording_consent"
        and getattr(policy, "boolean_value", None) is not None
    ):
        matched = _select_interview_recording_consent_policy_option(
            options,
            consents=bool(policy.boolean_value),
        )
        if matched is not None:
            return matched

    if getattr(policy, "category", None) == "availability_timing":
        matched = _select_availability_timing_policy_option(
            options,
            getattr(policy, "text_value", None),
        )
        if matched is not None:
            return matched

    if getattr(policy, "category", None) == "skill_years_experience":
        matched = _select_skill_years_policy_option(
            options,
            getattr(policy, "text_value", None),
        )
        if matched is not None:
            return matched

    return select_option(
        options,
        policy.text_value,
        filter_select_prefix=filter_select_prefix,
    )


def _unique_text_candidates(candidates: Iterable[str | None]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        text = str(candidate or "").strip()
        normalized = normalize_text(text)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(text)
    return result


def _profile_option_candidates(answer: str | None, *, profile_field: str | None = None) -> list[str]:
    desired = str(answer or "").strip()
    normalized = normalize_text(desired)
    candidates: list[str | None] = [desired]

    if profile_field in {"gender", "gender_identity"}:
        if ("male" in normalized or normalized.endswith(" man") or normalized == "man") and "woman" not in normalized:
            candidates.extend(["Man", "Male", "Cisgender Male/Man"])
        if (
            "female" in normalized or normalized.endswith(" woman") or normalized == "woman"
        ) and "man" not in normalized:
            candidates.extend(["Woman", "Female", "Cisgender Female/Woman"])
        if "non binary" in normalized or "nonbinary" in normalized:
            candidates.extend(["Non-Binary", "Nonbinary"])
    elif profile_field == "transgender_status":
        if normalized == "no" or "not transgender" in normalized or "do not identify as transgender" in normalized:
            candidates.extend(
                [
                    "No",
                    "Not transgender",
                    "Do not identify as transgender",
                ]
            )
        elif normalized == "yes" or "identify as transgender" in normalized or normalized == "transgender":
            candidates.extend(
                [
                    "Yes",
                    "Transgender",
                    "Identify as transgender",
                ]
            )
    elif profile_field == "race_or_ethnicity":
        if any(token in normalized for token in ("hispanic", "latino", "latina", "latinx")):
            candidates.extend(
                [
                    "Hispanic or Latino",
                    "Hispanic / Latino",
                    "Hispanic or Latinx",
                    "Hispanic/Latinx",
                    "Latino/a",
                    "Latinx",
                    "Hispanic, Latinx or of Spanish-Origin",
                    "Hispanic/Latinx or of Spanish origin",
                ]
            )
        candidates.extend(
            [
                "Prefer not to say",
                "Prefer not to answer",
                "Choose not to disclose",
                "Do not wish to declare",
                "Do not wish to disclose",
                "Decline to State",
                "Decline to self identify",
                "Decline to self-identify",
                "I don't wish to answer",
                "I do not wish to answer",
                "Not specified",
            ]
        )
    elif profile_field == "veteran_status":
        if normalized == "no" or ("not" in normalized and "veteran" in normalized):
            candidates.extend(
                [
                    "No",
                    "I am not a veteran",
                    "I am not a protected veteran",
                    "I identify as a veteran, just not a protected veteran",
                    "No, I am not a veteran or active member",
                    "No, I am not a veteran or active member of the United States Armed Forces",
                ]
            )
    elif profile_field == "disability_status":
        if normalized == "no" or "do not have a disability" in normalized or "not disabled" in normalized:
            candidates.extend(
                [
                    "No",
                    "I do not have a disability",
                    "No, I do not have a disability and have not had one in the past",
                    "I do not identify as having a disability",
                    "Not disabled",
                    "None of these apply",
                    "None of the above",
                ]
            )
    elif profile_field == "sexual_orientation":
        if "straight" in normalized or "heterosexual" in normalized:
            candidates.extend(
                [
                    "Heterosexual / straight",
                    "Heterosexual/straight",
                    "Heterosexual",
                    "Straight",
                    "Straight / Heterosexual",
                    "Heterosexual/Straight",
                ]
            )
    elif profile_field == "communities":
        if "none" in normalized or "not applicable" in normalized or normalized in {"n a", "na"}:
            candidates.extend(["None of the above", "None", "Not applicable", "N/A"])
    elif profile_field == "pronouns":
        pronouns = normalized.replace("/", " ").replace(",", " ")
        if "he" in pronouns:
            candidates.extend(["He/Him", "He/Him/His", "he him", "he him his"])
        elif "she" in pronouns:
            candidates.extend(["She/Her", "She/Her/Hers", "she her", "she her hers"])
        elif "they" in pronouns:
            candidates.extend(["They/Them", "They/Them/Theirs", "they them", "they them theirs"])

    return _unique_text_candidates(candidates)


def select_profile_option(
    options: list[str] | None,
    answer: str | None,
    *,
    profile_field: str | None = None,
    filter_select_prefix: bool = False,
    extra_candidates: Iterable[str | None] = (),
) -> str | None:
    """Match profile-backed option labels with shared alias normalization."""

    for candidate in _unique_text_candidates(
        [answer, *list(extra_candidates), *_profile_option_candidates(answer, profile_field=profile_field)]
    ):
        result = select_option(options, candidate, filter_select_prefix=filter_select_prefix)
        if result is not None:
            return result
    return None


@lru_cache(maxsize=1)
def _load_select_matching_application_profile():
    try:
        return parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _select_input_choice(
    options: list[str] | None,
    *,
    label: str,
    field_name: str,
    value: str,
    profile_field: str | None = None,
) -> str | None:
    if not value:
        return None

    application_profile = _load_select_matching_application_profile()
    for question_text in _unique_text_candidates((label, field_name)):
        try:
            policy = resolve_shared_question_policy(question_text, application_profile)
        except Exception:
            policy = None
        if policy is None:
            continue
        matched = select_shared_policy_option(
            options,
            policy,
            application_profile=application_profile,
        )
        if matched is not None:
            return matched

    matched = select_profile_option(
        options,
        value,
        profile_field=profile_field,
    )
    if matched is not None:
        return matched

    return select_option(options, value)


def _profile_text_value(*sources: object, attr: str) -> str:
    for source in sources:
        if source is None:
            continue
        value = getattr(source, attr, None)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def infer_unknown_question_blocker_metadata(
    *,
    field_name: str | None,
    label: str | None,
    application_profile,
    profile=None,
) -> dict[str, object]:
    """Return blocker metadata for unresolved visible deterministic fields."""

    combined = " ".join(part for part in (str(field_name or "").strip(), str(label or "").strip()) if part).strip()
    if not combined:
        return {}
    normalized_combined = combined.casefold()

    # Conditional follow-up prompts ("If other...", "If yes...") are not
    # independently deterministic answers even when they mention a self-ID topic.
    if label_matches(
        combined,
        "if other",
        "if you selected other",
        "if selected other",
        "if yes",
        "if you selected yes",
        "if selected yes",
        "if you prefer to self describe",
        "if you prefer to self-describe",
        "if you prefer to self identify",
        "if you prefer to self-identify",
    ):
        return {}

    def has_value(attr: str) -> bool:
        return bool(_profile_text_value(application_profile, profile, attr=attr))

    def profile_value(attr: str) -> str:
        return _profile_text_value(application_profile, profile, attr=attr).strip()

    def visible_self_id(profile_field: str) -> dict[str, object]:
        return {
            "blocks_draft_completion": True,
            "blocker_kind": VISIBLE_SELF_ID_BLOCKER_KIND,
            "profile_field": profile_field,
        }

    def visible_profile(profile_field: str) -> dict[str, object]:
        return {
            "blocks_draft_completion": True,
            "blocker_kind": VISIBLE_PROFILE_FIELD_BLOCKER_KIND,
            "profile_field": profile_field,
        }

    if (
        "disability" in normalized_combined
        and "accommodation" in normalized_combined
        and any(
            fragment in normalized_combined
            for fragment in (
                "if you have a disability",
                "if this does not apply",
                "please skip this question",
            )
        )
    ):
        disability_status = profile_value("disability_status").casefold()
        if disability_status and "disabil" in disability_status and any(
            token in disability_status for token in ("no", "not", "none")
        ):
            return {}

    if label_matches(combined, "transgender") and has_value("transgender_status"):
        return visible_self_id("transgender_status")
    if label_matches(combined, "gender") and not label_matches(combined, "transgender"):
        if has_value("gender_identity"):
            return visible_self_id("gender_identity")
        if has_value("gender"):
            return visible_self_id("gender")
    if label_matches(combined, "race", "ethnicity", "racial", "ethnic") and has_value("race_or_ethnicity"):
        return visible_self_id("race_or_ethnicity")
    if label_matches(combined, "veteran") and has_value("veteran_status"):
        return visible_self_id("veteran_status")
    if label_matches(combined, "disability") and has_value("disability_status"):
        return visible_self_id("disability_status")
    if label_matches(combined, "sexual orientation", "orientation") and has_value("sexual_orientation"):
        return visible_self_id("sexual_orientation")
    if label_matches(combined, "communities", "community") and has_value("communities"):
        return visible_self_id("communities")
    if label_matches(combined, "pronoun", "pronouns") and has_value("pronouns"):
        return visible_self_id("pronouns")
    if label_matches(combined, "current age", "age group", "age range", "your age", "age:") and has_value("age_range"):
        return visible_profile("age_range")
    if label_matches(combined, "linkedin") and has_value("linkedin"):
        return visible_profile("linkedin")
    if (
        label_matches(combined, "website", "portfolio")
        and not label_matches(combined, "password")
        and has_value("website")
    ):
        return visible_profile("website")
    return {}


def _compile_patterns(patterns: Iterable[str | re.Pattern[str]]) -> tuple[re.Pattern[str], ...]:
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        if isinstance(pattern, re.Pattern):
            compiled.append(pattern)
        else:
            compiled.append(re.compile(pattern, re.I))
    return tuple(compiled)


def _label_pattern(label: str, field_name: str = "") -> re.Pattern[str]:
    candidate = label.strip() or field_name.strip() or "field"
    return re.compile(re.escape(candidate), re.I)


def _mark_not_found(step: dict) -> None:
    step.setdefault("status", "skipped_not_found")


def _fill_text_input(page, *, label: str, field_name: str, value: str, textarea_only: bool = False) -> bool:
    if not value:
        return False

    label_re = _label_pattern(label, field_name)
    try:
        locator = page.get_by_label(label_re).first
        if locator.count():
            tag_name = str(locator.evaluate("el => el.tagName.toLowerCase()"))
            if textarea_only and tag_name != "textarea":
                raise RuntimeError("label matched non-textarea input")
            locator.scroll_into_view_if_needed()
            locator.fill(value)
            return True
    except Exception:
        pass

    try:
        locator = page.get_by_role("textbox", name=label_re).first
        if locator.count():
            locator.scroll_into_view_if_needed()
            locator.fill(value)
            return True
    except Exception:
        pass

    selectors: list[str] = []
    if field_name:
        selectors.extend(
            [
                f'textarea[name*="{field_name}"]',
                f'textarea[id*="{field_name}"]',
                f'input[name*="{field_name}"]',
                f'input[id*="{field_name}"]',
            ]
        )
    selectors.extend(
        [
            f'textarea[placeholder*="{label}"]',
            f'input[placeholder*="{label}"]',
        ]
    )
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count():
                if textarea_only:
                    tag_name = str(locator.evaluate("el => el.tagName.toLowerCase()"))
                    if tag_name != "textarea":
                        continue
                locator.scroll_into_view_if_needed()
                locator.fill(value)
                return True
        except Exception:
            continue
    return False


def _fill_file_input(page, *, field_name: str, label: str, file_path: str) -> bool:
    if not file_path or not Path(file_path).exists():
        return False

    label_re = _label_pattern(label, field_name)
    try:
        locator = page.get_by_label(label_re).first
        if locator.count():
            locator.set_input_files(file_path)
            return True
    except Exception:
        pass

    try:
        inputs = page.locator("input[type='file']")
        count = inputs.count()
        if count == 0:
            return False
        if "cover" in field_name.casefold() and count > 1:
            inputs.nth(1).set_input_files(file_path)
            return True
        inputs.first.set_input_files(file_path)
        return True
    except Exception:
        return False


def _fill_select_input(page, *, label: str, field_name: str, value: str, profile_field: str | None = None) -> bool:
    if not value:
        return False

    label_re = _label_pattern(label, field_name)
    try:
        locator = page.get_by_label(label_re).first
        if locator.count():
            tag_name = str(locator.evaluate("el => el.tagName.toLowerCase()"))
            if tag_name == "select":
                try:
                    options = locator.locator("option")
                    option_values = [options.nth(i).inner_text().strip() for i in range(options.count())]
                    choice = _select_input_choice(
                        option_values,
                        label=label,
                        field_name=field_name,
                        value=value,
                        profile_field=profile_field,
                    ) or value
                except Exception:
                    choice = value
                locator.select_option(label=choice)
                return True
    except Exception:
        pass

    if field_name:
        for selector in (
            f'select[name="{field_name}"]',
            f'select[id="{field_name}"]',
            f'select[name*="{field_name}"]',
            f'select[id*="{field_name}"]',
        ):
            try:
                locator = page.locator(selector).first
                if not locator.count():
                    continue
                locator.scroll_into_view_if_needed()
                try:
                    options = locator.locator("option")
                    option_values = [options.nth(i).inner_text().strip() for i in range(options.count())]
                    choice = _select_input_choice(
                        option_values,
                        label=label,
                        field_name=field_name,
                        value=value,
                        profile_field=profile_field,
                    ) or value
                except Exception:
                    choice = value
                locator.select_option(label=choice)
                return True
            except Exception:
                continue

    try:
        locator = page.get_by_role("combobox", name=label_re).first
        if locator.count():
            locator.scroll_into_view_if_needed()
            try:
                options = locator.locator("option")
                option_values = [options.nth(i).inner_text().strip() for i in range(options.count())]
                choice = _select_input_choice(
                    option_values,
                    label=label,
                    field_name=field_name,
                    value=value,
                    profile_field=profile_field,
                ) or value
                locator.select_option(label=choice)
                return True
            except Exception:
                pass
            locator.click()
            page.wait_for_timeout(250)
            options = page.get_by_role("option")
            option_values: list[str] = []
            option_locators = []
            for index in range(options.count()):
                option = options.nth(index)
                try:
                    option_text = option.inner_text().strip()
                except Exception:
                    continue
                if not option_text:
                    continue
                option_values.append(option_text)
                option_locators.append(option)
            choice = _select_input_choice(
                option_values,
                label=label,
                field_name=field_name,
                value=value,
                profile_field=profile_field,
            ) or value
            for option, option_text in zip(option_locators, option_values, strict=False):
                if option_text == choice:
                    option.click()
                    page.wait_for_timeout(150)
                    return True
            try:
                locator.fill(choice)
                locator.press("Enter")
                return True
            except Exception:
                return False
    except Exception:
        pass
    return False


def _set_checkbox(page, *, label: str, field_name: str, checked: bool) -> bool:
    label_re = _label_pattern(label, field_name)
    for resolver in (
        lambda: page.get_by_role("checkbox", name=label_re).first,
        lambda: page.get_by_label(label_re).first,
    ):
        try:
            locator = resolver()
            if not locator.count():
                continue
            locator.scroll_into_view_if_needed()
            is_checked = locator.is_checked()
            if checked and not is_checked:
                locator.click()
            if not checked and is_checked:
                locator.click()
            return True
        except Exception:
            continue
    return False


def _set_radio(page, *, label: str, field_name: str, value: str) -> bool:
    if field_name:
        try:
            radios = page.locator(f'input[type="radio"][name="{field_name}"]')
            count = radios.count()
            option_values: list[str] = []
            option_locators = []
            for index in range(count):
                radio = radios.nth(index)
                option_value = str(radio.get_attribute("value") or "").strip()
                if not option_value:
                    try:
                        radio_id = str(radio.get_attribute("id") or "").strip()
                        if radio_id:
                            option_value = str(page.locator(f'label[for="{radio_id}"]').first.inner_text() or "").strip()
                    except Exception:
                        option_value = ""
                if not option_value:
                    continue
                option_values.append(option_value)
                option_locators.append(radio)
            choice = select_option(option_values, value) or value
            for radio, option_value in zip(option_locators, option_values, strict=False):
                if option_value != choice:
                    continue
                radio_id = str(radio.get_attribute("id") or "").strip()
                try:
                    radio.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    radio.check()
                except Exception:
                    if radio_id:
                        try:
                            label_locator = page.locator(f'label[for="{radio_id}"]').first
                            if label_locator.count():
                                try:
                                    label_locator.scroll_into_view_if_needed()
                                except Exception:
                                    pass
                                label_locator.click()
                                return True
                        except Exception:
                            pass
                    radio.click()
                return True
        except Exception:
            pass

    value_re = _label_pattern(value, field_name)
    try:
        locator = page.get_by_role("radio", name=value_re).first
        if locator.count():
            locator.scroll_into_view_if_needed()
            locator.click()
            return True
    except Exception:
        pass

    label_re = _label_pattern(label, field_name)
    for selector in (
        f'label:has-text("{value}")',
        f'[role="radio"][aria-label*="{value}"]',
        f'input[type="radio"][value*="{value}"]',
    ):
        try:
            locator = page.locator(selector).first
            if locator.count():
                locator.scroll_into_view_if_needed()
                locator.click()
                return True
        except Exception:
            continue
    try:
        locator = page.get_by_label(label_re).first
        if locator.count():
            locator.scroll_into_view_if_needed()
            locator.click()
            return True
    except Exception:
        pass
    return False


def fill_basic_step(page, step: dict) -> None:
    kind = str(step.get("kind") or "").strip().casefold()
    label = str(step.get("label") or "").strip()
    field_name = str(step.get("field_name") or "").strip()

    if kind == "file":
        file_path = str(step.get("file_path") or "")
        if _fill_file_input(page, field_name=field_name, label=label, file_path=file_path):
            step["filled"] = True
            step.pop("status", None)
        return

    if kind == "checkbox":
        checked = str(step.get("value") or "checked").strip().casefold() not in {"", "false", "no", "unchecked"}
        if _set_checkbox(page, label=label, field_name=field_name, checked=checked):
            step["filled"] = True
            step.pop("status", None)
        else:
            _mark_not_found(step)
        return

    if kind == "radio":
        value = str(step.get("value") or "")
        if _set_radio(page, label=label, field_name=field_name, value=value):
            step["filled"] = True
            step.pop("status", None)
        else:
            _mark_not_found(step)
        return

    if kind == "select":
        value = str(step.get("value") or "")
        profile_field = str(step.get("profile_field") or "").strip() or None
        if not value:
            _mark_not_found(step)
            return
        if _fill_select_input(
            page,
            label=label,
            field_name=field_name,
            value=value,
            profile_field=profile_field,
        ):
            step["filled"] = True
            step.pop("status", None)
        else:
            _mark_not_found(step)
        return

    if kind in {"text", "textarea"}:
        value = str(step.get("value") or "")
        if not value:
            _mark_not_found(step)
            return
        if _fill_text_input(page, label=label, field_name=field_name, value=value, textarea_only=kind == "textarea"):
            step["filled"] = True
            step.pop("status", None)
        else:
            _mark_not_found(step)
        return

    _mark_not_found(step)


def classify_submit_state(
    snapshot: dict,
    *,
    confirm_patterns: Iterable[str | re.Pattern[str]],
    review_patterns: Iterable[str | re.Pattern[str]] = (),
    validation_patterns: Iterable[str | re.Pattern[str]] = _DEFAULT_VALIDATION_PATTERNS,
    captcha_type: str = "recaptcha",
) -> dict[str, object]:
    page_text = str(snapshot.get("page_text") or "")
    page_url = str(snapshot.get("url") or "")
    page_title = str(snapshot.get("page_title") or "")
    invalid_fields = [str(item) for item in (snapshot.get("invalid_fields") or []) if str(item).strip()]
    explicit_errors = [str(item) for item in (snapshot.get("errors") or []) if str(item).strip()]
    combined = " ".join(part for part in (page_text, page_title, page_url, *invalid_fields, *explicit_errors) if part)

    compiled_confirm = _compile_patterns(confirm_patterns)
    compiled_review = _compile_patterns(review_patterns)
    compiled_validation = _compile_patterns(validation_patterns)

    if any(pattern.search(combined) for pattern in compiled_confirm):
        return {"status": "confirmed", "reason": "text"}

    captcha_visible_key = f"{captcha_type}_visible"
    captcha_active_key = f"{captcha_type}_challenge_active"
    if snapshot.get(captcha_visible_key) and snapshot.get(captcha_active_key):
        return {"status": "captcha_required", "reason": "challenge"}

    if invalid_fields or explicit_errors or any(pattern.search(combined) for pattern in compiled_validation):
        return {
            "status": "validation_error",
            "errors": explicit_errors or invalid_fields or [combined.strip() or "validation error"],
        }

    if any(pattern.search(combined) for pattern in compiled_review):
        return {"status": "review", "reason": "text"}

    return {"status": "unknown", "reason": "no_match"}


def match_prior_employer_option(option_labels: list[str], has_worked_for_company: bool) -> str | None:
    negative_fragments = (
        "have never worked",
        "never worked",
        "have not worked",
        "not worked",
        "never been employed",
        "have not been employed",
        "not been employed",
        "have not previously been employed",
        "not previously been employed",
        "have not previously worked",
        "not previously worked",
        "not a former employee",
        "not currently or previously employed",
    )
    negative_exact_matches = {"not applicable", "n a", "na", "n/a"}

    def is_negative(label: str) -> bool:
        normalized = normalize_text(label)
        return (
            normalized == "no"
            or normalized.startswith("no ")
            or normalized in negative_exact_matches
            or any(fragment in normalized for fragment in negative_fragments)
        )

    if not has_worked_for_company:
        return next((label for label in option_labels if is_negative(label)), None)

    positives = (
        "currently work",
        "currently employed",
        "current employee",
        "currently a contractor",
        "current contractor",
        "have previously worked",
        "previously worked",
        "previously a contractor",
        "worked for",
        "worked at",
        "have been employed",
        "former employee",
    )
    return next(
        (
            label
            for label in option_labels
            if not is_negative(label)
            and (
                (normalized := normalize_text(label)) == "yes"
                or normalized.startswith("yes ")
                or any(fragment in normalized for fragment in positives)
            )
        ),
        None,
    )


def mark_step_as_draft_blocker(
    step: dict | None,
    *,
    blocker_kind: str,
    profile_field: str | None = None,
    artifact_key: str | None = None,
) -> dict | None:
    """Mark a planned step as a draft blocker with shared metadata."""
    if step is None:
        return None
    step["blocks_draft_completion"] = True
    step["blocker_kind"] = blocker_kind
    if profile_field:
        step["profile_field"] = profile_field
    if artifact_key:
        step["artifact_key"] = artifact_key
    return step


def required_artifact_blocker_step(
    *,
    field_name: str,
    label: str,
    source: str,
    artifact_key: str,
    expected_path: str | Path | None = None,
    note: str | None = None,
    reason: str | None = None,
) -> dict:
    """Build a blocker entry for a missing required proof artifact."""
    step = {
        "field_name": field_name,
        "label": label,
        "kind": "artifact",
        "source": source,
        "required": True,
        "status": "missing",
    }
    if expected_path:
        step["value"] = str(expected_path)
    if note:
        step["note"] = note
    if reason:
        step["reason"] = reason
    return (
        mark_step_as_draft_blocker(
            step,
            blocker_kind=REQUIRED_ARTIFACT_BLOCKER_KIND,
            artifact_key=artifact_key,
        )
        or step
    )


def mark_visible_profile_field_step(
    step: dict | None,
    *,
    profile_field: str | None = None,
) -> dict | None:
    """Mark a planned step as a visible profile-backed blocker."""
    return mark_step_as_draft_blocker(
        step,
        blocker_kind=VISIBLE_PROFILE_FIELD_BLOCKER_KIND,
        profile_field=profile_field,
    )


def mark_visible_self_id_step(
    step: dict | None,
    *,
    profile_field: str | None = None,
) -> dict | None:
    """Mark a planned step as a visible self-ID blocker.

    The field only blocks draft completion if it remains planned/unconfirmed.
    Fields skipped as not present on the page stay non-blocking.
    """
    return mark_step_as_draft_blocker(
        step,
        blocker_kind=VISIBLE_SELF_ID_BLOCKER_KIND,
        profile_field=profile_field,
    )


def is_visible_self_id_blocker(step_or_entry: dict | None) -> bool:
    if not isinstance(step_or_entry, dict):
        return False
    if not step_or_entry.get("blocks_draft_completion"):
        return False
    return str(step_or_entry.get("blocker_kind") or "").strip() == VISIBLE_SELF_ID_BLOCKER_KIND


def is_visible_profile_field_blocker(step_or_entry: dict | None) -> bool:
    if not isinstance(step_or_entry, dict):
        return False
    if not step_or_entry.get("blocks_draft_completion"):
        return False
    return str(step_or_entry.get("blocker_kind") or "").strip() == VISIBLE_PROFILE_FIELD_BLOCKER_KIND


def is_visible_confirmation_blocker(step_or_entry: dict | None) -> bool:
    return is_visible_self_id_blocker(step_or_entry) or is_visible_profile_field_blocker(step_or_entry)


def report_entry_blocks_draft_completion(entry: dict) -> bool:
    """Return whether an unconfirmed report entry should block draft completion."""
    status = str(entry.get("status") or "").strip().casefold()
    if status in {"filled", "skipped_not_found"}:
        return False
    if entry.get("blocks_draft_completion"):
        return True
    if "required" in entry:
        return bool(entry.get("required"))
    if "optional" in entry:
        return not bool(entry.get("optional"))
    return True


def blocking_unconfirmed_report_entries(entries: list[dict]) -> list[dict]:
    return [entry for entry in entries if report_entry_blocks_draft_completion(entry)]


def _report_step_is_password(step: dict) -> bool:
    field_name = normalize_text(step.get("field_name", ""))
    label = normalize_text(step.get("label", ""))
    return "password" in field_name or "password" in label


def _report_step_value(step: dict) -> str:
    if step.get("report_value") is not None:
        return str(step["report_value"])
    if _report_step_is_password(step):
        return "[redacted password]"
    if step.get("kind") == "file" and step.get("file_path"):
        return str(step["file_path"])
    return str(step.get("value", step.get("option", step.get("file_path", step.get("search", "")))))


def _report_entry(step: dict) -> dict:
    """Build a report entry dict from a step dict."""
    entry = {
        "field_name": step.get("field_name", ""),
        "label": step.get("label", ""),
        "kind": step.get("kind", ""),
        "source": step.get("source", ""),
    }
    if "required" in step:
        entry["required"] = bool(step.get("required"))
    elif "optional" in step:
        entry["required"] = not bool(step.get("optional"))
    else:
        entry["required"] = bool(step.get("required"))
    if "optional" in step:
        entry["optional"] = bool(step.get("optional"))
    elif "required" in step:
        entry["optional"] = not bool(step.get("required"))
    if step.get("note"):
        entry["note"] = step.get("note")
    if step.get("page_index") is not None:
        entry["page_index"] = step.get("page_index")
    if step.get("blocks_draft_completion"):
        entry["blocks_draft_completion"] = True
    blocker_kind = str(step.get("blocker_kind") or "").strip()
    if blocker_kind:
        entry["blocker_kind"] = blocker_kind
    profile_field = str(step.get("profile_field") or "").strip()
    if profile_field:
        entry["profile_field"] = profile_field
    artifact_key = str(step.get("artifact_key") or "").strip()
    if artifact_key:
        entry["artifact_key"] = artifact_key
    explicit_status = str(step.get("status", "")).strip()
    if step.get("filled"):
        entry["status"] = "filled"
        entry["value"] = _report_step_value(step)
    elif explicit_status:
        entry["status"] = explicit_status
        entry["value"] = _report_step_value(step)
    else:
        entry["status"] = "planned"
        entry["value"] = _report_step_value(step)
    return entry


def write_report(
    payload: dict,
    *,
    board_name: str,
    runtime: dict | None = None,
) -> dict:
    """Write JSON and Markdown autofill reports. Returns the report payload dict."""
    steps = list(runtime.get("steps", payload["steps"]) if runtime else payload["steps"])
    outcomes = list(runtime.get("outcomes", payload.get("outcomes", [])) if runtime else payload.get("outcomes", []))
    report_entries = [_report_entry(step) for step in steps]
    filled_entries = [e for e in report_entries if e["status"] == "filled"]
    planned_entries = [
        e
        for e in report_entries
        if e["status"] != "filled"
        # Optional fields that were not found on the page are not actionable
        and not (e.get("status") == "skipped_not_found" and not e.get("required", True))
    ]
    artifacts = payload["artifacts"]
    report_markdown_path = _resolve_report_markdown_path(artifacts)
    artifacts.setdefault("report_markdown", report_markdown_path)
    page_screenshots: list[str] = []
    if runtime:
        page_screenshots = [str(page.get("screenshot") or "").strip() for page in runtime.get("pages", [])]
    elif artifacts.get("page_screenshots_dir"):
        pages_dir = Path(str(artifacts.get("page_screenshots_dir") or "").strip())
        if pages_dir.exists():
            page_screenshots = [str(path) for path in sorted(pages_dir.glob("page_*.png")) if path.is_file()]
    page_screenshots = dedupe_page_screenshot_artifacts(
        page_screenshots,
        pre_submit_screenshot=str(artifacts.get("pre_submit_screenshot") or "").strip() or None,
        review_screenshot=str(artifacts.get("review_screenshot") or "").strip() or None,
    )

    report_payload = {
        "job_title": payload["job_title"],
        "company": payload["company"],
        "job_url": payload["job_url"],
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "pre_submit_screenshot": artifacts["pre_submit_screenshot"],
        "fields": filled_entries,
        "unknown_questions": payload.get("unknown_questions", []),
    }
    if page_screenshots:
        report_payload["page_screenshots"] = page_screenshots
    if "application_url" in payload:
        report_payload["application_url"] = payload["application_url"]
    if outcomes:
        report_payload["outcomes"] = outcomes
    if planned_entries:
        report_payload["planned_but_unconfirmed_fields"] = planned_entries
    Path(artifacts["report_json"]).write_text(json_dumps_pretty(report_payload) + "\n", encoding="utf-8")

    board_title = board_name.capitalize()
    lines = [
        f"# {board_title} Autofill Report",
        "",
        f"- Company: {payload['company']}",
        f"- Job Title: {payload['job_title']}",
        f"- Job URL: {payload['job_url']}",
    ]
    if "application_url" in payload:
        lines.append(f"- Application URL: {payload['application_url']}")
    lines.extend(
        [
            f"- Generated At (UTC): {report_payload['generated_at_utc']}",
            f"- Pre-Submit Screenshot: {artifacts['pre_submit_screenshot']}",
            "",
        ]
    )
    if outcomes:
        lines.extend(["## Outcomes", ""])
        for index, outcome in enumerate(outcomes, start=1):
            lines.extend(
                [
                    f"### {index}. {outcome.get('name', 'outcome')}",
                    f"Status: `{outcome.get('status', '')}`",
                ]
            )
            if outcome.get("expected_file"):
                lines.append(f"Expected File: `{outcome['expected_file']}`")
            if outcome.get("message"):
                lines.append(f"Message: {outcome['message']}")
            observed_labels = outcome.get("observed_selection_labels") or []
            if observed_labels:
                lines.extend(
                    ["Observed Selection Labels:", "```text", "\n".join(str(label) for label in observed_labels), "```"]
                )
            lines.append("")
    lines.extend(
        [
            "## Filled Fields",
            "",
        ]
    )
    for index, entry in enumerate(filled_entries, start=1):
        lines.extend(
            [
                f"### {index}. {entry['label']} (`{entry['field_name']}`)",
                f"Source: `{entry['source']}`",
                f"Kind: `{entry['kind']}`",
                f"Required: `{'yes' if entry['required'] else 'no'}`",
                f"Status: `{entry['status']}`",
                "```text",
                str(entry["value"]),
                "```",
                "",
            ]
        )
    if planned_entries:
        lines.extend(["## Planned But Unconfirmed", ""])
        for entry in planned_entries:
            lines.extend(
                [
                    f"- {entry['label']} (`{entry['field_name']}`) from `{entry['source']}`",
                    f"  - Kind: `{entry['kind']}`",
                    f"  - Required: `{'yes' if entry['required'] else 'no'}`",
                    f"  - Status: `{entry['status']}`",
                ]
            )
            if entry.get("blocks_draft_completion"):
                lines.append("  - Blocks Draft Completion: `yes`")
            if entry.get("page_index") is not None:
                lines.append(f"  - Page: `{entry['page_index']}`")
            if entry.get("note"):
                lines.append(f"  - Note: {entry['note']}")
    if payload.get("unknown_questions"):
        lines.extend(["## Unresolved Questions", ""])
        for question in payload["unknown_questions"]:
            lines.append(f"- {question['label']} (`{question['field_name']}`)")
        lines.append("")
    Path(report_markdown_path).write_text("\n".join(lines), encoding="utf-8")
    return report_payload


def capture_locator_screenshot(locator, path) -> None:
    """Capture a locator screenshot, tolerating simple test doubles."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        locator.screenshot(path=str(path), type="png")
    except TypeError:
        locator.screenshot(path=str(path))


def capture_scrollable_locator_screenshot(page, locator, path) -> None:
    """Capture a locator, stitching vertical scroll segments when needed."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        metrics = locator.evaluate(
            """node => ({
                scrollHeight: Math.max(node.scrollHeight || 0, node.offsetHeight || 0),
                clientHeight: Math.max(node.clientHeight || 0, node.getBoundingClientRect().height || 0),
                scrollTop: node.scrollTop || 0,
                devicePixelRatio: window.devicePixelRatio || 1,
            })"""
        )
    except Exception:
        capture_locator_screenshot(locator, path)
        return

    total_height_css = max(int(metrics.get("scrollHeight") or 0), 0)
    viewport_height_css = max(int(metrics.get("clientHeight") or 0), 0)
    initial_scroll_css = max(int(metrics.get("scrollTop") or 0), 0)
    device_pixel_ratio = max(float(metrics.get("devicePixelRatio") or 1), 1.0)

    if total_height_css <= 0 or viewport_height_css <= 0 or total_height_css <= viewport_height_css + 1:
        capture_locator_screenshot(locator, path)
        return

    import io

    from PIL import Image

    final_height_px = max(int(round(total_height_css * device_pixel_ratio)), 1)
    stitched: Image.Image | None = None
    start_css = 0
    try:
        while start_css < total_height_css:
            end_css = min(start_css + viewport_height_css, total_height_css)
            scroll_target = max(0, end_css - viewport_height_css)
            try:
                actual_scroll_css = float(
                    locator.evaluate(
                        "(node, top) => { node.scrollTop = top; return node.scrollTop || 0; }", scroll_target
                    )
                    or 0
                )
            except Exception:
                actual_scroll_css = float(scroll_target)
            try:
                page.wait_for_timeout(120)
            except Exception:
                pass
            screenshot_bytes = locator.screenshot(type="png")
            with Image.open(io.BytesIO(screenshot_bytes)) as captured:
                image = captured.convert("RGB")
            if stitched is None:
                stitched = Image.new("RGB", (image.width, final_height_px), "white")
            crop_top_px = max(int(round((start_css - actual_scroll_css) * device_pixel_ratio)), 0)
            crop_bottom_px = min(
                int(round((end_css - actual_scroll_css) * device_pixel_ratio)),
                image.height,
            )
            if crop_bottom_px > crop_top_px:
                segment = image.crop((0, crop_top_px, image.width, crop_bottom_px))
                paste_y_px = max(int(round(start_css * device_pixel_ratio)), 0)
                overflow_px = (paste_y_px + segment.height) - final_height_px
                if overflow_px > 0:
                    segment = segment.crop((0, 0, segment.width, segment.height - overflow_px))
                if segment.height > 0:
                    stitched.paste(segment, (0, paste_y_px))
            start_css = end_css
    finally:
        try:
            locator.evaluate("(node, top) => { node.scrollTop = top; return node.scrollTop || 0; }", initial_scroll_css)
        except Exception:
            pass

    if stitched is None:
        capture_locator_screenshot(locator, path)
        return
    stitched.save(path)


def concatenate_images_vertically(image_paths: list[str | Path], output_path: str | Path) -> Path:
    """Stack images vertically into a single PNG artifact."""

    from PIL import Image

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    existing_paths = [Path(path) for path in image_paths if Path(path).exists()]
    if not existing_paths:
        raise FileNotFoundError(f"No input images found for {output_path}")

    images: list[Image.Image] = []
    try:
        for path in existing_paths:
            with Image.open(path) as opened:
                images.append(opened.convert("RGB"))
        width = max(image.width for image in images)
        height = sum(image.height for image in images)
        combined = Image.new("RGB", (width, height), "white")
        y_offset = 0
        for image in images:
            combined.paste(image, (0, y_offset))
            y_offset += image.height
        combined.save(output_path)
    finally:
        for image in images:
            image.close()
    return output_path


def _hide_transient_capture_overlays(page) -> None:
    try:
        page.evaluate(
            f"""
            () => {{
                try {{
                    const active = document.activeElement;
                    if (active && typeof active.blur === 'function') {{
                        active.blur();
                    }}
                }} catch (_error) {{
                    // Ignore focus cleanup failures during proof capture.
                }}

                const selectors = {json.dumps(list(_TRANSIENT_CAPTURE_OVERLAY_SELECTORS))};
                for (const selector of selectors) {{
                    for (const element of document.querySelectorAll(selector)) {{
                        const style = getComputedStyle(element);
                        if (style.display === 'none' || style.visibility === 'hidden') {{
                            continue;
                        }}
                        element.dataset.captureOverlayHidden = '1';
                        element.dataset.captureOverlayOriginalDisplay = element.style.display || '';
                        element.style.setProperty('display', 'none', 'important');
                    }}
                }}
            }}
            """
        )
    except Exception:
        pass


def _restore_transient_capture_overlays(page) -> None:
    try:
        page.evaluate(
            """
            () => {
                for (const element of document.querySelectorAll('[data-capture-overlay-hidden="1"]')) {
                    const originalDisplay = element.dataset.captureOverlayOriginalDisplay || '';
                    if (originalDisplay) {
                        element.style.display = originalDisplay;
                    } else {
                        element.style.removeProperty('display');
                    }
                    delete element.dataset.captureOverlayOriginalDisplay;
                    delete element.dataset.captureOverlayHidden;
                }
            }
            """
        )
    except Exception:
        pass


def _hide_fixed_capture_overlays(page) -> None:
    try:
        page.evaluate(
            """
            () => {
                const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
                const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
                for (const element of document.querySelectorAll('*')) {
                    const style = getComputedStyle(element);
                    if (style.position !== 'fixed' && style.position !== 'sticky') {
                        continue;
                    }
                    if (element === document.documentElement || element === document.body) {
                        continue;
                    }
                    const rect = element.getBoundingClientRect();
                    if (!rect.width || !rect.height) {
                        continue;
                    }
                    const identity = [
                        element.tagName || '',
                        element.id || '',
                        String(element.className || ''),
                        element.getAttribute('title') || '',
                        element.getAttribute('src') || '',
                    ]
                        .join(' ')
                        .toLowerCase();
                    const isSecurityChallengeOverlay =
                        identity.includes('captcha') ||
                        identity.includes('turnstile') ||
                        identity.includes('security challenge');
                    const coversViewport =
                        rect.width >= viewportWidth * 0.95 && rect.height >= viewportHeight * 0.95;
                    if (coversViewport && !isSecurityChallengeOverlay) {
                        continue;
                    }
                    element.dataset.captureFixedHidden = '1';
                    element.dataset.captureFixedOriginalVisibility = element.style.visibility || '';
                    element.style.setProperty('visibility', 'hidden', 'important');
                }
            }
            """
        )
    except Exception:
        pass


def _restore_fixed_capture_overlays(page) -> None:
    try:
        page.evaluate(
            """
            () => {
                for (const element of document.querySelectorAll('[data-capture-fixed-hidden="1"]')) {
                    const originalVisibility = element.dataset.captureFixedOriginalVisibility || '';
                    if (originalVisibility) {
                        element.style.visibility = originalVisibility;
                    } else {
                        element.style.removeProperty('visibility');
                    }
                    delete element.dataset.captureFixedOriginalVisibility;
                    delete element.dataset.captureFixedHidden;
                }
            }
            """
        )
    except Exception:
        pass


def capture_full_page(page, path, *, preferred_selectors: tuple[str, ...] = ()) -> None:
    """Capture a full-page screenshot, preferring a specific selector if found.

    Args:
        page: Playwright page object.
        path: Output path for the screenshot.
        preferred_selectors: Selectors to try before falling back to full_page.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _hide_transient_capture_overlays(page)
        for selector in preferred_selectors:
            try:
                locator = page.locator(selector).first
                if locator.count():
                    _hide_fixed_capture_overlays(page)
                    try:
                        capture_scrollable_locator_screenshot(page, locator, path)
                    finally:
                        _restore_fixed_capture_overlays(page)
                    return
            except Exception:
                continue
        # Hide sticky/fixed-position elements before full-page screenshot to
        # prevent them from repeating at every scroll position.
        try:
            page.evaluate("""
                () => {
                    for (const el of document.querySelectorAll('*')) {
                        const style = getComputedStyle(el);
                        if (style.position === 'fixed' || style.position === 'sticky') {
                            el.dataset._origPosition = style.position;
                            el.style.position = 'absolute';
                        }
                    }
                }
            """)
        except Exception:
            pass
        page.screenshot(path=str(path), full_page=True)
        # Restore original positions
        try:
            page.evaluate("""
                () => {
                    for (const el of document.querySelectorAll('[data-_orig-position]')) {
                        el.style.position = el.dataset._origPosition;
                        delete el.dataset._origPosition;
                    }
                }
            """)
        except Exception:
            pass
    finally:
        _restore_transient_capture_overlays(page)


def wait_for_pending_uploads(page, *, timeout_ms: int = 15000) -> None:
    """Wait for file uploads and async form processing to complete before submit.

    Many job boards (Ashby, Greenhouse, Lever, etc.) process file uploads
    asynchronously.  Clicking submit while an upload is in flight can cause
    a silent failure or a transient warning flash.  This function waits for
    common "uploading/pending" indicators to disappear and for in-flight
    network requests to settle.
    """
    # Common selectors for pending/uploading indicators across boards
    pending_selectors = [
        # Ashby pending overlay
        '[class*="pending-layer"]:not([data-state="hidden"])',
        '[class*="_pending"]:not([data-state="hidden"])',
        # Generic spinners/progress near file inputs
        '[class*="uploading"]',
        '[class*="file-upload"] [class*="spinner"]',
        '[class*="file-upload"] [class*="loading"]',
        '[aria-label*="uploading" i]',
        '[class*="progress-bar"]:not([aria-valuenow="100"])',
    ]
    combined = ", ".join(pending_selectors)
    try:
        page.wait_for_selector(combined, state="hidden", timeout=timeout_ms)
    except Exception:
        pass  # timeout or no matching elements — either way, proceed
    # Brief settle period for server-side processing
    page.wait_for_timeout(500)


def click_submit_button(page, *, button_names: tuple[str, ...] | list[str]) -> bool:
    """Find and click the first visible, enabled submit button.

    Waits for pending file uploads to complete before clicking.

    Args:
        page: Playwright page object.
        button_names: Button label names to try in order.

    Returns:
        True if a button was clicked, False otherwise.
    """
    wait_for_pending_uploads(page)
    for name in button_names:
        locator = page.get_by_role("button", name=name)
        count = locator.count()
        if not count:
            continue
        for index in range(count):
            button = locator.nth(index)
            try:
                if not button.is_visible() or not button.is_enabled():
                    continue
                button.click(timeout=5000, no_wait_after=True)
                return True
            except Exception:
                continue
    return False


def page_snapshot(page, *, form_selector: str, captcha_type: str) -> dict:
    """Capture a JS-based snapshot of the current page state.

    Args:
        page: Playwright page object.
        form_selector: CSS selector used to detect if the form is visible.
        captcha_type: Either "recaptcha" or "hcaptcha".

    Returns:
        Dict with keys: url, page_text, form_visible, {captcha_type}_visible,
        {captcha_type}_challenge_active, invalid_fields, errors.
    """
    if captcha_type == "recaptcha":
        captcha_js = """
            const isActuallyVisible = (element) => {
                const rect = element.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) return false;
                const style = window.getComputedStyle(element);
                if (!style) return true;
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                if (Number.parseFloat(style.opacity || '1') <= 0) return false;
                return true;
            };
            const captchaIframes = Array.from(document.querySelectorAll('iframe')).filter((frame) => {
                const title = (frame.getAttribute('title') || '').toLowerCase();
                const src = (frame.getAttribute('src') || '').toLowerCase();
                return title.includes('recaptcha') || src.includes('recaptcha');
            });
            const captchaVisible =
                captchaIframes.length > 0 || !!document.querySelector('.grecaptcha-badge');
            const captchaChallengeActive = captchaIframes.some((frame) => {
                const title = (frame.getAttribute('title') || '').toLowerCase();
                const src = (frame.getAttribute('src') || '').toLowerCase();
                if (!isActuallyVisible(frame)) return false;
                if (title.includes('challenge') || src.includes('/bframe')) return true;
                const rect = frame.getBoundingClientRect();
                return rect.height >= 120;
            });
        """
        captcha_visible_key = "recaptcha_visible"
        captcha_challenge_key = "recaptcha_challenge_active"
    else:
        # hcaptcha
        captcha_js = """
            const isActuallyVisible = (element) => {
                const rect = element.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) return false;
                const style = window.getComputedStyle(element);
                if (!style) return true;
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                if (Number.parseFloat(style.opacity || '1') <= 0) return false;
                return true;
            };
            const captchaIframes = Array.from(document.querySelectorAll('iframe')).filter((frame) => {
                const title = (frame.getAttribute('title') || '').toLowerCase();
                const src = (frame.getAttribute('src') || '').toLowerCase();
                return title.includes('hcaptcha') || src.includes('hcaptcha');
            });
            const captchaVisible =
                captchaIframes.length > 0 || !!document.querySelector('.h-captcha');
            const captchaChallengeActive = captchaIframes.some((frame) => {
                const title = (frame.getAttribute('title') || '').toLowerCase();
                const src = (frame.getAttribute('src') || '').toLowerCase();
                if (!isActuallyVisible(frame)) return false;
                if (title.includes('challenge') || src.includes('/challenge')) return true;
                const rect = frame.getBoundingClientRect();
                return rect.height >= 120;
            });
        """
        captcha_visible_key = "hcaptcha_visible"
        captcha_challenge_key = "hcaptcha_challenge_active"

    form_selector_literal = json.dumps(form_selector)
    invalid_field_selector_literal = json.dumps(f'{form_selector}, [class*="_fieldEntry_"]')

    js = f"""() => {{
        const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
        const pageText = normalize(document.body ? document.body.innerText : '');
        const invalidFields = Array.from(document.querySelectorAll('[aria-invalid="true"]'))
            .map((node) => {{
                const entry = node.closest({invalid_field_selector_literal});
                const label = entry ? entry.querySelector('label') : null;
                return normalize(label ? label.innerText : node.getAttribute('name') || node.getAttribute('id') || '');
            }})
            .filter(Boolean);
        const explicitErrors = Array.from(document.querySelectorAll('[role="alert"], [class*="error"], [class*="Error"]'))
            .map((node) => normalize(node.innerText))
            .filter(Boolean);
        {captcha_js}
        return {{
            url: window.location.href,
            page_text: pageText,
            form_visible: !!document.querySelector({form_selector_literal}),
            {captcha_visible_key}: captchaVisible,
            {captcha_challenge_key}: captchaChallengeActive,
            invalid_fields: invalidFields,
            errors: explicitErrors,
        }};
    }}"""

    return page.evaluate(js)


def write_submit_debug_artifacts(page, payload: dict, capture_fn) -> None:
    """Write HTML and screenshot debug artifacts after a failed submit.

    Args:
        page: Playwright page object.
        payload: The autofill payload dict (contains artifacts paths).
        capture_fn: Callable(page, path) that takes a screenshot.
    """
    debug_html = Path(payload["artifacts"]["submit_debug_html"])
    debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
    debug_html.write_text(page.content(), encoding="utf-8")
    capture_fn(page, debug_png)


def matches_confirm_patterns(page_text: str, patterns) -> bool:
    """Check if page text matches any confirmation pattern."""
    return any(pattern.search(page_text) for pattern in patterns)


def collect_validation_errors(snapshot: dict, validation_patterns) -> tuple[list[str], list[str]]:
    """Extract combined errors and invalid fields from a page snapshot."""
    page_text = str(snapshot.get("page_text") or "")
    explicit_errors = list(snapshot.get("errors") or [])
    page_level_errors = [page_text for pattern in validation_patterns if pattern.search(page_text)]
    combined_errors = list(dict.fromkeys(explicit_errors + page_level_errors))
    invalid_fields = list(snapshot.get("invalid_fields") or [])
    return combined_errors, invalid_fields


def yes_no_step(
    field: dict,
    *,
    value: bool,
    source: str,
    option_matcher,
) -> dict | None:
    """Build a step dict for a yes/no question.

    Args:
        field: The form field dict.
        value: True for "Yes", False for "No".
        source: The source identifier (e.g. "application_profile.md").
        option_matcher: Callable that takes a list of candidate strings
            and returns the matched option string, or None.
    """
    yes_candidates = ["Yes", "yes", "YES", "True", "true"]
    no_candidates = ["No", "no", "NO", "False", "false"]
    candidates = yes_candidates if value else no_candidates
    matched = option_matcher(candidates)
    if matched is None:
        return None
    return {
        "field_name": field.get("field_name", ""),
        "label": field.get("label", ""),
        "kind": field.get("kind", "radio"),
        "required": bool(field.get("required")),
        "index": field.get("index", 0),
        "value": matched,
        "source": source,
    }


def _notify_captcha(company: str, role: str) -> None:
    """Send macOS notification for captcha waiting. No-op on non-macOS."""
    import platform
    import subprocess as _sp

    if platform.system() != "Darwin":
        return
    title = "Job Assets — Captcha Required"
    body = f"{company} — {role}".replace('"', '\\"')
    try:
        _sp.run(
            [
                "osascript",
                "-e",
                f'display notification "{body}" with title "{title}"',
            ],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def wait_for_captcha_resolution(
    page,
    *,
    headless: bool,
    payload: dict,
    board_title: str,
    classify_state_fn,
    page_snapshot_fn,
    email_watcher,
    confirmed_outcome_from_email_fn,
    capture_fn,
    submit_started_at_utc: str,
) -> dict:
    """Wait for user to solve captcha in headed browser.

    Universal captcha wait used by ``run_browser_pipeline()`` for all boards.

    Returns:
        {"status": "confirmed", "outcome": ...} — user solved captcha
        {"status": "timeout"} — timeout expired
        {"status": "skipped"} — headless mode, cannot wait
    """
    if headless:
        return {"status": "skipped"}

    out_dir = Path(payload["out_dir"])
    submit_dir = out_dir / "submit"
    signal_file = submit_dir / "awaiting_captcha.json"
    company = payload.get("company", "Unknown")
    role = payload.get("job_title", "Unknown")
    timeout = int(os.environ.get("JOB_ASSETS_CAPTCHA_TIMEOUT", "3600"))

    # Write signal file for orchestrator to poll
    submit_dir.mkdir(parents=True, exist_ok=True)
    signal_file.write_text(
        json.dumps(
            {
                "company": company,
                "role": role,
                "timestamp": datetime.now(UTC).isoformat(),
                "timeout_seconds": timeout,
            }
        ),
        encoding="utf-8",
    )

    # Set page title for window identification
    try:
        title = f"[Captcha] {company} — {role}"
        page.evaluate(f"document.title = {json.dumps(title)}")
    except Exception:
        pass

    # Bring browser window to front so user can solve captcha
    try:
        from browser_runtime import reveal_manual_challenge

        reveal_manual_challenge(page)
    except Exception:
        pass

    # Notify user
    _notify_captcha(company, role)
    print(
        f"{board_title} captcha detected — browser open for manual solve (timeout: {timeout}s)",
        file=sys.stderr,
    )

    # Poll loop
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            page.wait_for_timeout(3000)
            snapshot = page_snapshot_fn(page)

            # Check email confirmation
            if email_watcher:
                email_confirmation = email_watcher.poll()
                if email_confirmation and confirmed_outcome_from_email_fn:
                    outcome = confirmed_outcome_from_email_fn(snapshot, email_confirmation)
                    return {"status": "confirmed", "outcome": outcome, "email_confirmation": email_confirmation}

            # Check page state
            state = classify_state_fn(snapshot)
            if state["status"] == "confirmed":
                outcome = {"status": "confirmed", "reason": state.get("reason"), "snapshot": snapshot}
                return {"status": "confirmed", "outcome": outcome}

        # Timeout — save debug artifacts
        if "artifacts" in payload:
            try:
                debug_html = Path(payload["artifacts"]["submit_debug_html"])
                debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
                debug_html.write_text(page.content(), encoding="utf-8")
                capture_fn(page, debug_png)
            except Exception:
                pass

        return {"status": "timeout"}
    finally:
        # Always clean up signal file
        try:
            signal_file.unlink(missing_ok=True)
        except Exception:
            pass
