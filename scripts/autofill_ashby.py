#!/usr/bin/env python3
"""Generate and optionally run an Ashby application autofill flow."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_submit_common import (
    APPLICATION_PROFILE_PATH,
    PROJECT_ROOT,
    GeneratedAnswerBlockersError,
    build_onsite_start_location_answer,
    clear_pending_user_input,
    find_cover_letter_file,
    find_cover_letter_text,
    find_resume_file,
    format_education_from_profile,
    generate_application_answers,
    json_dumps_pretty,
    load_meta,
    normalize_text,
    parse_application_profile,
    parse_master_resume,
    pending_user_input_reason_for_spec,
    primary_employer_name,
    profile_available_start_date,
    question_is_current_company_field,
    question_prefers_generated_free_text_answer,
    resolve_how_did_you_hear_option_candidates,
    resolve_shared_question_policy,
    select_truthful_age_option,
    slugify_label,
    write_pending_user_input,
    write_pending_user_input_for_unconfirmed_fields,
)
from autofill_common import (
    board_file_constants,
    capture_full_page,
    click_submit_button,
    explicit_us_state_list_membership_answer,
    infer_unknown_question_blocker_metadata,
    is_visible_confirmation_blocker,
    label_matches,
    mark_visible_profile_field_step,
    mark_visible_self_id_step,
    page_snapshot,
    select_location_positive_fit_option,
    select_profile_option,
    select_shared_policy_option,
    yes_no_step,
)
from browser_runtime import (
    human_fill,
)
from generated_answer_validation import _is_conditional_followup
from job_board_urls import resolve_ashby_wrapper_url
from output_layout import migrate_role_output_layout, role_submit_path
from project_env import load_project_env
from question_classifier import classify_question

_BOARD_CONSTANTS = board_file_constants("ashby")
AUTOFILL_REPORT_MD = _BOARD_CONSTANTS["report_md"]
AUTOFILL_REPORT_JSON = _BOARD_CONSTANTS["report_json"]
AUTOFILL_PRE_SUBMIT_SCREENSHOT = _BOARD_CONSTANTS["pre_submit_screenshot"]
AUTOFILL_PAGE_SCREENSHOTS_DIR = _BOARD_CONSTANTS["page_screenshots_dir"]
AUTOFILL_UNKNOWN_QUESTIONS_JSON = _BOARD_CONSTANTS["unknown_questions_json"]
AUTOFILL_SUBMIT_DEBUG_HTML = _BOARD_CONSTANTS["submit_debug_html"]
AUTOFILL_SUBMIT_DEBUG_SCREENSHOT = _BOARD_CONSTANTS["submit_debug_screenshot"]
AUTOFILL_PAYLOAD_JSON = _BOARD_CONSTANTS["payload_json"]
APPLICATION_PAGE_HTML = _BOARD_CONSTANTS["application_page_html"]
WEBSITE_CONFIRMATION_JSON = "application_confirmation_website.json"
EMAIL_CONFIRMATION_JSON = "application_confirmation_email.json"
NOTION_SYNC_STATUS_JSON = "notion_sync_status.json"
UNKNOWN_STATUSES = {"unknown_required", "unknown_optional"}
ASHBY_APP_DATA_TOKEN = "window.__appData = "
DEFAULT_CAPTCHA_WAIT_SECONDS = 300
SUBMIT_BUTTON_NAMES = ("Submit Application", "Submit", "Apply")
PREFERRED_CAPTURE_SELECTORS = ("main", "#root")
FORM_READY_SELECTOR = ".ashby-application-form-field-entry"
SUBMIT_CONFIRM_PATTERNS = (
    re.compile(r"\bthank(?:s| you)\s+for\s+applying\b", re.I),
    re.compile(r"\bapplication (?:has been )?(?:received|submitted)\b", re.I),
    re.compile(r"\bapplication was successfully submitted\b", re.I),
    re.compile(r"\bsuccessfully submitted\b", re.I),
    re.compile(r"\bwe(?:'|’)ve received your application\b", re.I),
    re.compile(r"\bwe have received your application\b", re.I),
    re.compile(r"\bwe will be in touch\b", re.I),
    re.compile(r"\bsubmitted successfully\b", re.I),
)
VALIDATION_ERROR_PATTERNS = (
    re.compile(r"\bthis field is required\b", re.I),
    re.compile(r"\bplease (?:enter|select|complete)\b", re.I),
    re.compile(r"\binvalid\b", re.I),
    re.compile(r"\berror\b", re.I),
)
FIELD_ENTRY_SELECTOR = '.ashby-application-form-field-entry, [class*="_fieldEntry_"]'
STATE_NAME_MAP = {
    "CA": "California",
    "CO": "Colorado",
    "NY": "New York",
    "WA": "Washington",
}


load_project_env()


def _maybe_reexec_with_uv() -> None:
    if os.environ.get("JOB_ASSETS_ASHBY_BOOTSTRAPPED") == "1":
        return
    if not shutil.which("uv"):
        return
    env = os.environ.copy()
    env["JOB_ASSETS_ASHBY_BOOTSTRAPPED"] = "1"
    cmd = ["uv", "run", "--project", str(PROJECT_ROOT), "python", __file__, *sys.argv[1:]]
    raise SystemExit(subprocess.call(cmd, cwd=PROJECT_ROOT, env=env))


def _application_url_for_job_url(job_url: str) -> str:
    job_url = resolve_ashby_wrapper_url(job_url)
    parsed = urllib.parse.urlparse(job_url)
    path = parsed.path.rstrip("/")
    if not path.endswith("/application"):
        path = f"{path}/application"
    return urllib.parse.urlunparse(parsed._replace(path=path))


def _fetch_application_html(application_url: str, cache_path: Path | None = None) -> str:
    request = Request(
        application_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0 Safari/537.36"
            )
        },
    )
    with urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", errors="replace")
    if cache_path is not None:
        cache_path.write_text(html, encoding="utf-8")
    return html


def _extract_json_object_after(text: str, token: str) -> str | None:
    start = text.find(token)
    if start == -1:
        return None
    brace_start = text.find("{", start)
    if brace_start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for index in range(brace_start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start : index + 1]
    return None


def _extract_app_data(html: str) -> dict:
    payload = _extract_json_object_after(html, ASHBY_APP_DATA_TOKEN)
    if not payload:
        raise ValueError("Could not find window.__appData in Ashby application page")
    return json.loads(payload)


def _iter_form_entries(form: dict | None) -> list[dict]:
    if not isinstance(form, dict):
        return []
    entries = form.get("entries") or form.get("fieldEntries") or []
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("field")]


def _ashby_rich_text_lines(node: object) -> list[str]:
    if isinstance(node, list):
        lines: list[str] = []
        for child in node:
            lines.extend(_ashby_rich_text_lines(child))
        return lines
    if not isinstance(node, dict):
        return []
    if "type" not in node and "content" in node:
        return _ashby_rich_text_lines(node.get("content"))

    node_type = str(node.get("type") or "")
    if node_type == "text":
        text = str(node.get("text") or "")
        return [text] if text else []

    child_content = node.get("content")
    if node_type in {"paragraph", "heading"}:
        text = "".join(_ashby_rich_text_lines(child_content)).strip()
        return [text] if text else []

    return _ashby_rich_text_lines(child_content)


def _ashby_rich_text_to_plain_text(value: object) -> str:
    return "\n".join(line for line in _ashby_rich_text_lines(value) if str(line).strip())


def _field_entries_from_app_data(app_data: dict) -> list[dict]:
    posting = app_data.get("posting") or {}
    fields: list[dict] = []
    forms: list[tuple[str, dict]] = [("application", posting.get("applicationForm") or {})]
    for index, survey_form in enumerate(posting.get("surveyForms") or app_data.get("surveyForms") or []):
        if isinstance(survey_form, dict):
            forms.append((f"survey_{index + 1}", survey_form))

    counts: dict[str, int] = {}
    for form_name, form in forms:
        for entry in _iter_form_entries(form):
            field = entry["field"]
            label = str(field.get("title") or field.get("humanReadablePath") or field.get("path") or "").strip()
            path = str(field.get("path") or field.get("id") or "").strip()
            if not label or not path:
                continue
            slug = slugify_label(f"{form_name}_{label}")
            counts[slug] = counts.get(slug, 0) + 1
            field_name = slug if counts[slug] == 1 else f"{slug}_{counts[slug]}"
            fields.append(
                {
                    "field_name": field_name,
                    "label": label,
                    "description": _ashby_rich_text_to_plain_text(entry.get("description")),
                    "path": path,
                    "required": bool(entry.get("isRequired")),
                    "field_type": str(field.get("type") or ""),
                    "form_name": form_name,
                    "raw_entry": entry,
                    "raw_field": field,
                }
            )
    return fields


def _label_matches(field: dict, *fragments: str) -> bool:
    return label_matches(field, *fragments)


def _is_linkedin_field(field: dict) -> bool:
    if _label_matches(field, "linkedin", "linked in"):
        return True
    description = normalize_text(field.get("description"))
    return bool(description) and "linkedin profile" in description and _label_matches(field, "profile")


def _is_website_field(field: dict) -> bool:
    return _label_matches(field, "website", "portfolio") and not _label_matches(field, "password", "cover letter")


def _is_name_pronunciation_field(field: dict) -> bool:
    label = normalize_text(field.get("label"))
    return "pronunciation" in label and "name" in label


def _is_auto_consent_field(field: dict) -> bool:
    label = normalize_text(field.get("label"))
    path = normalize_text(field.get("path"))
    description = normalize_text(field.get("description"))
    if path == "_systemfield_data_consent_ack":
        return True
    if "future contact consent" in label:
        return True
    if description and "privacy notice" in description and "consent" in description:
        return True
    if not label:
        return False
    if any(
        fragment in label for fragment in ("privacy policy", "candidate privacy", "data protection", "data processing")
    ):
        return True
    if "ai tools" in label and any(
        fragment in label
        for fragment in (
            "ai policy",
            "understanding and agreement",
            "understanding and agree",
            "acknowledge and agree",
            "agree to comply",
            "during the interview process",
            "for this application",
        )
    ):
        return True
    acknowledges = any(fragment in label for fragment in ("acknowledge", "agree", "consent", "confirm", "certify"))
    policy_gate = any(fragment in label for fragment in ("policy", "privacy", "data processing", "data protection"))
    return acknowledges and policy_gate


def _field_supports_generated_answer(field: dict) -> bool:
    return field["field_type"] in {"String", "LongText", "ValueSelect", "MultiValueSelect", "Number", "Boolean"}


def _state_country_variants(location: str, country: str) -> list[str]:
    parts = [part.strip() for part in location.split(",") if part.strip()]
    if not parts:
        return [location]

    variants = [location]
    if len(parts) >= 2:
        state = parts[1]
        expanded_state = STATE_NAME_MAP.get(state.upper())
        if expanded_state:
            variants.append(", ".join([parts[0], expanded_state, country]))
    variants.append(f"{parts[0]}, {country}")
    return list(dict.fromkeys(variants))


def _selectable_labels(field: dict) -> list[str]:
    selectable_values = (field.get("raw_field") or {}).get("selectableValues") or []
    labels: list[str] = []
    for selectable in selectable_values:
        if not isinstance(selectable, dict):
            continue
        label = str(selectable.get("label") or selectable.get("value") or "").strip()
        if label:
            labels.append(label)
    return labels


def _match_selectable_label(field: dict, candidates: list[str], *, profile_field: str | None = None) -> str | None:
    labels = _selectable_labels(field)
    if not labels:
        return None

    for candidate in candidates:
        result = (
            select_profile_option(labels, candidate, profile_field=profile_field)
            if profile_field
            else select_profile_option(labels, candidate)
        )
        if result is not None:
            return result
    return None


def _metro_area_choice(field: dict, *, location: str, country: str) -> str | None:
    parts = [part.strip() for part in location.split(",") if part.strip()]
    candidates: list[str] = []
    if parts:
        city = parts[0]
        candidates.append(city)
        if normalize_text(city) == "new york city":
            candidates.append("nyc")
        if normalize_text(city) == "san francisco":
            candidates.append("bay area")
    if len(parts) >= 2:
        state = parts[1]
        candidates.append(state)
        expanded_state = STATE_NAME_MAP.get(state.upper())
        if expanded_state:
            candidates.append(expanded_state)
    candidates.extend(_state_country_variants(location, country))

    selected = _match_selectable_label(field, candidates)
    if selected:
        return selected
    return _match_selectable_label(field, ["None", "None of the above"])


def _office_location_choice(field: dict, *, location: str, country: str) -> str | None:
    parts = [part.strip() for part in location.split(",") if part.strip()]
    candidates: list[str] = []
    if parts:
        city = parts[0]
        candidates.append(city)
        if normalize_text(city) == "new york city":
            candidates.append("NYC")
        if normalize_text(city) == "san francisco":
            candidates.extend(["San Francisco Bay Area", "Bay Area"])
    if len(parts) >= 2:
        state = parts[1]
        candidates.append(state)
        expanded_state = STATE_NAME_MAP.get(state.upper())
        if expanded_state:
            candidates.append(expanded_state)
    candidates.extend(_state_country_variants(location, country))

    return _match_selectable_label(field, candidates)


def _hybrid_work_interest_choice(field: dict, *, prefers_hybrid: bool) -> str | None:
    labels = _selectable_labels(field)
    if not labels:
        return None

    preferred_fragments = (
        ["can work hybrid", "work hybrid", "hybrid", "in office", "commute", "live within"]
        if prefers_hybrid
        else ["prefer remote", "remote opportunities", "remote", "do not live near", "don t live near"]
    )
    rejected_fragments = (
        ["prefer remote", "remote opportunities", "do not live near", "don t live near"]
        if prefers_hybrid
        else ["can work hybrid", "work hybrid", "in office", "commute", "live within"]
    )

    normalized_preferred = [normalize_text(fragment) for fragment in preferred_fragments if normalize_text(fragment)]
    normalized_rejected = [normalize_text(fragment) for fragment in rejected_fragments if normalize_text(fragment)]

    best_label: str | None = None
    best_score = 0
    for label in labels:
        normalized_label = normalize_text(label)
        if any(fragment in normalized_label for fragment in normalized_rejected):
            continue
        score = max(
            (len(fragment) for fragment in normalized_preferred if fragment in normalized_label),
            default=0,
        )
        if score > best_score:
            best_score = score
            best_label = label

    if best_label:
        return best_label

    return _match_selectable_label(field, preferred_fragments)


def _is_conditional_yes_followup(field: dict) -> bool:
    label = normalize_text(field.get("label"))
    if not label:
        return False
    return label.startswith(("if yes", "if you selected yes", "if selected yes"))


def _step_is_negative_yes_no(step: dict | None) -> bool:
    if not isinstance(step, dict):
        return False
    normalized_value = normalize_text(step.get("value"))
    return normalized_value == "no" or normalized_value.startswith("no ")


def _choice_kind(field: dict) -> str:
    return "choice" if field["field_type"] in {"Boolean", "ValueSelect", "MultiValueSelect"} else "text"


def _ashby_option_matcher(field: dict, candidates: list[str]) -> str | None:
    """Match candidates against selectable labels, or return first candidate for non-select types."""
    if field["field_type"] in {"ValueSelect", "MultiValueSelect"}:
        return _match_selectable_label(field, candidates)
    # For non-select types (e.g. Boolean), the value is literal "Yes"/"No"
    return candidates[0] if candidates else None


def _yes_no_step(field: dict, *, value: bool, source: str) -> dict | None:
    result = yes_no_step(
        field,
        value=value,
        source=source,
        option_matcher=lambda candidates: _ashby_option_matcher(field, candidates),
    )
    if result is None:
        return None
    # Preserve Ashby-specific keys in the step dict
    result["path"] = field["path"]
    result["field_type"] = field["field_type"]
    result["kind"] = _choice_kind(field)
    return result


def _infer_step(
    field: dict,
    *,
    meta: dict,
    profile,
    application_profile,
    out_dir: Path,
    generated_answers: dict[str, str],
) -> dict | None:
    base = {
        "field_name": field["field_name"],
        "label": field["label"],
        "path": field["path"],
        "required": field["required"],
        "field_type": field["field_type"],
    }

    if _label_matches(field, "name") and field["path"] == "_systemfield_name":
        return {**base, "kind": "text", "value": profile.full_name, "source": "master_resume.md"}
    if _label_matches(field, "full name", "legal name"):
        return {**base, "kind": "text", "value": profile.full_name, "source": "master_resume.md"}
    if _label_matches(field, "preferred first name"):
        return {**base, "kind": "text", "value": profile.first_name, "source": "master_resume.md"}
    if _label_matches(field, "first name") and not _label_matches(field, "preferred"):
        return {**base, "kind": "text", "value": profile.first_name, "source": "master_resume.md"}
    if _label_matches(field, "last name"):
        return {**base, "kind": "text", "value": profile.last_name, "source": "master_resume.md"}
    if _label_matches(field, "email", "e mail") or field["path"] == "_systemfield_email":
        return {**base, "kind": "text", "value": profile.email, "source": "master_resume.md"}
    if _label_matches(field, "phone"):
        return mark_visible_profile_field_step(
            {
                **base,
                "kind": "text",
                "value": profile.phone,
                "source": "master_resume.md",
                "text_message_consent": application_profile.text_message_consent,
            },
            profile_field="phone",
        )
    if question_is_current_company_field(field_name=field.get("path"), label=field.get("label")):
        employer_name = primary_employer_name()
        if field["field_type"] in {"ValueSelect", "MultiValueSelect"}:
            selected = _match_selectable_label(field, [employer_name])
            if selected:
                return mark_visible_profile_field_step(
                    {
                        **base,
                        "kind": _choice_kind(field),
                        "value": selected,
                        "source": "master_resume.md",
                    },
                    profile_field="current_employer",
                )
        return mark_visible_profile_field_step(
            {
                **base,
                "kind": "text",
                "value": employer_name,
                "source": "master_resume.md",
            },
            profile_field="current_employer",
        )
    if _label_matches(field, "zip code", "postal code", "zip/postal"):
        zip_code = ""
        if "san francisco" in application_profile.location.lower():
            zip_code = "94105"
        if zip_code:
            return {**base, "kind": "text", "value": zip_code, "source": "application_profile.md"}
    if _is_linkedin_field(field):
        return mark_visible_profile_field_step(
            {
                **base,
                "kind": "text",
                "value": application_profile.linkedin or profile.linkedin or "",
                "source": "application_profile.md" if application_profile.linkedin else "master_resume.md",
            },
            profile_field="linkedin",
        )
    if _label_matches(field, "github"):
        return {
            **base,
            "kind": "text",
            "value": application_profile.github or "",
            "source": "application_profile.md" if application_profile.github else "not_provided",
        }
    if _is_website_field(field):
        return {
            **base,
            "kind": "text",
            "value": application_profile.website or profile.website or "",
            "source": "application_profile.md" if application_profile.website else "master_resume.md",
        }
    # "Current location" can be String, Location (combobox), or ValueSelect — handle all.
    if _label_matches(field, "current location"):
        if field["field_type"] == "Location":
            return mark_visible_profile_field_step(
                {
                    **base,
                    "kind": "location",
                    "value": application_profile.location,
                    "search_variants": _state_country_variants(
                        application_profile.location, application_profile.country
                    ),
                    "source": "application_profile.md",
                },
                profile_field="location",
            )
        if field["field_type"] == "ValueSelect":
            choice = _office_location_choice(
                field, location=application_profile.location, country=application_profile.country
            )
            if choice:
                return mark_visible_profile_field_step(
                    {**base, "kind": "choice", "value": choice, "source": "application_profile.md"},
                    profile_field="location",
                )
        # Default: treat as plain text (String type or unknown)
        return mark_visible_profile_field_step(
            {
                **base,
                "kind": "text",
                "value": application_profile.location,
                "source": "application_profile.md",
            },
            profile_field="location",
        )
    if _label_matches(field, "location") and field["field_type"] == "Location":
        return mark_visible_profile_field_step(
            {
                **base,
                "kind": "location",
                "value": application_profile.location,
                "search_variants": _state_country_variants(application_profile.location, application_profile.country),
                "source": "application_profile.md",
            },
            profile_field="location",
        )
    if field["field_type"] == "Location" and _label_matches(field, "city of residence", "residence"):
        return mark_visible_profile_field_step(
            {
                **base,
                "kind": "location",
                "value": application_profile.location,
                "search_variants": _state_country_variants(application_profile.location, application_profile.country),
                "source": "application_profile.md",
            },
            profile_field="location",
        )
    # Catch-all for any remaining Location fields (country, "be based", hiring location, etc.)
    if field["field_type"] == "Location":
        return {
            **base,
            "kind": "location",
            "value": application_profile.location,
            "search_variants": _state_country_variants(application_profile.location, application_profile.country),
            "source": "application_profile.md",
        }
    if _label_matches(field, "resume") and field["field_type"] == "File":
        return {
            **base,
            "kind": "file",
            "file_path": str(find_resume_file(out_dir)),
            "source": "existing_resume_asset",
        }
    if _label_matches(field, "cover letter") and field["field_type"] == "File":
        return {
            **base,
            "kind": "file",
            "file_path": str(find_cover_letter_file(out_dir)),
            "source": "existing_cover_letter_asset",
        }
    if field["field_type"] in {"String", "LongText"} and classify_question(field.get("label", "")) == "education":
        education_text = format_education_from_profile(application_profile)
        if education_text:
            return {**base, "kind": "text", "value": education_text, "source": "application_profile.md"}
    if (
        field["field_type"] in {"String", "LongText"}
        and _label_matches(field, "pronoun", "pronouns")
        and not _label_matches(field, "if other")
    ):
        pronouns = str(getattr(application_profile, "pronouns", "") or "").strip()
        if pronouns:
            return mark_visible_self_id_step(
                {
                    **base,
                    "kind": "text",
                    "value": pronouns,
                    "source": "application_profile.md",
                },
                profile_field="pronouns",
            )
    if _label_matches(field, "cover letter") and field["field_type"] in {"String", "LongText"}:
        return {
            **base,
            "kind": "text",
            "value": find_cover_letter_text(out_dir),
            "source": "cover_letter_text.txt",
        }
    if (
        field["field_type"] in {"String", "LongText"}
        and _label_matches(field, "anything else", "additional information", "comments")
        and not _label_matches(field, "california resident", "california residents")
    ):
        return {
            **base,
            "kind": "text",
            "value": find_cover_letter_text(out_dir),
            "source": "cover_letter_text.txt",
        }
    if field["field_type"] in {"String", "LongText"}:
        onsite_answer = build_onsite_start_location_answer(field.get("label"), application_profile)
        if onsite_answer:
            return {
                **base,
                "kind": "text",
                "value": onsite_answer,
                "source": "application_profile.md",
            }
    if _label_matches(field, "how did you hear", "how did you learn", "how did you first learn"):
        desired_values, heard_source = resolve_how_did_you_hear_option_candidates(
            application_profile,
            _selectable_labels(field),
            company_name=(meta or {}).get("company_proper") or (meta or {}).get("company"),
            job_url=(meta or {}).get("jd_source_resolved") or (meta or {}).get("jd_source"),
            source_url=(meta or {}).get("source_url"),
            source_hint=(meta or {}).get("source"),
        )
        desired = desired_values[0] if desired_values else (application_profile.how_did_you_hear or "Corporate website")
        if field["field_type"] in {"ValueSelect", "MultiValueSelect"}:
            selected = _match_selectable_label(field, desired_values or [desired])
            if selected:
                return {**base, "kind": _choice_kind(field), "value": selected, "source": heard_source}
        return {
            **base,
            "kind": "text",
            "value": desired,
            "source": heard_source,
        }
    if field["field_type"] == "ValueSelect" and _label_matches(field, "metro area"):
        choice = _metro_area_choice(
            field,
            location=application_profile.location,
            country=application_profile.country,
        )
        if choice:
            return mark_visible_profile_field_step(
                {
                    **base,
                    "kind": "choice",
                    "value": choice,
                    "source": "application_profile.md",
                },
                profile_field="location",
            )
    if field["field_type"] in {"ValueSelect", "MultiValueSelect"} and (
        _label_matches(
            field,
            "location",
            "office location",
            "location of choice",
            "preferred office",
            "preferred location",
            "open to working from",
            "which location",
            "which office",
        )
        or (_label_matches(field, "office") and _label_matches(field, "work out of", "preference", "prefer"))
    ):
        choice = _office_location_choice(
            field,
            location=application_profile.location,
            country=application_profile.country,
        )
        if choice:
            return mark_visible_profile_field_step(
                {
                    **base,
                    "kind": "choice",
                    "value": choice,
                    "source": "application_profile.md",
                },
                profile_field="location",
            )
    # Hub proximity / "select which location" questions (e.g. Whatnot's "within 50 miles of our hubs")
    if field["field_type"] in {"ValueSelect", "MultiValueSelect"} and (
        _label_matches(field, "select which location")
        or (_label_matches(field, "hub") and _label_matches(field, "located"))
    ):
        choice = select_location_positive_fit_option(
            _selectable_labels(field),
            application_profile=application_profile,
            context_text=" ".join(
                part
                for part in (str(field.get("label") or "").strip(), str(field.get("description") or "").strip())
                if part
            ),
        ) or _office_location_choice(
            field,
            location=application_profile.location,
            country=application_profile.country,
        )
        if choice:
            return mark_visible_profile_field_step(
                {
                    **base,
                    "kind": "choice",
                    "value": choice,
                    "source": "application_profile.md",
                },
                profile_field="location",
            )
    if (
        field["field_type"] in {"ValueSelect", "MultiValueSelect"}
        and _label_matches(field, "hybrid work")
        and _label_matches(field, "interest", "describe")
    ):
        prefers_hybrid = bool(
            getattr(application_profile, "comfortable_working_on_site", False)
            and getattr(application_profile, "lives_in_job_location", False)
        )
        selected = _hybrid_work_interest_choice(field, prefers_hybrid=prefers_hybrid)
        if selected:
            return mark_visible_profile_field_step(
                {
                    **base,
                    "kind": "choice",
                    "value": selected,
                    "source": "shared_positive_fit_policy",
                },
                profile_field="location",
            )
    if field["field_type"] in {"ValueSelect", "MultiValueSelect"} and _label_matches(field, "transgender"):
        selected = _match_selectable_label(
            field,
            [getattr(application_profile, "transgender_status", None) or "No"],
            profile_field="transgender_status",
        )
        if selected:
            return mark_visible_self_id_step(
                {**base, "kind": "choice", "value": selected, "source": "application_profile.md"},
                profile_field="transgender_status",
            )
    if field["field_type"] in {"ValueSelect", "MultiValueSelect"} and _label_matches(
        field, "current age", "age group", "age range"
    ):
        selected = select_truthful_age_option(
            _selectable_labels(field),
            getattr(application_profile, "age_range", None),
        )
        if selected:
            return mark_visible_profile_field_step(
                {**base, "kind": "choice", "value": selected, "source": "application_profile.md"},
                profile_field="age_range",
            )
    if (
        field["field_type"] in {"ValueSelect", "MultiValueSelect"}
        and _label_matches(field, "gender")
        and not _label_matches(field, "transgender")
    ):
        profile_field = "gender_identity" if getattr(application_profile, "gender_identity", None) else "gender"
        selected = _match_selectable_label(
            field,
            [getattr(application_profile, "gender_identity", None) or application_profile.gender or ""],
            profile_field=profile_field,
        )
        if selected:
            return mark_visible_self_id_step(
                {**base, "kind": "choice", "value": selected, "source": "application_profile.md"},
                profile_field=profile_field,
            )
    if field["field_type"] in {"ValueSelect", "MultiValueSelect"} and _label_matches(field, "race", "ethnicity"):
        raw_race = application_profile.race_or_ethnicity or ""
        candidates = [raw_race]
        rl = raw_race.lower()
        if "hispanic" in rl or "latino" in rl:
            candidates.extend(
                [
                    "Hispanic or Latino",
                    "Hispanic/Latino",
                    "Hispanic or Latino/a/x",
                    "Hispanic",
                    "Latino",
                    "Latinx",
                    "Hispanic or Latinx",
                    "Hispanic/Latinx or of Spanish origin",
                ]
            )
        selected = _match_selectable_label(field, candidates, profile_field="race_or_ethnicity")
        if selected:
            return mark_visible_self_id_step(
                {**base, "kind": "choice", "value": selected, "source": "application_profile.md"},
                profile_field="race_or_ethnicity",
            )
    if field["field_type"] in {"ValueSelect", "MultiValueSelect"} and _label_matches(field, "veteran"):
        selected = _match_selectable_label(
            field,
            [application_profile.veteran_status or ""],
            profile_field="veteran_status",
        )
        if selected:
            return mark_visible_self_id_step(
                {**base, "kind": "choice", "value": selected, "source": "application_profile.md"},
                profile_field="veteran_status",
            )
    if field["field_type"] in {"ValueSelect", "MultiValueSelect"} and _label_matches(field, "disability"):
        selected = _match_selectable_label(
            field,
            [application_profile.disability_status or ""],
            profile_field="disability_status",
        )
        if selected:
            return mark_visible_self_id_step(
                {**base, "kind": "choice", "value": selected, "source": "application_profile.md"},
                profile_field="disability_status",
            )
    if field["field_type"] in {"ValueSelect", "MultiValueSelect"} and _label_matches(
        field, "sexual orientation", "orientation"
    ):
        raw_so = application_profile.sexual_orientation or ""
        so_candidates = [raw_so]
        sl = raw_so.lower()
        if "straight" in sl or "heterosexual" in sl:
            so_candidates.extend(
                [
                    "Heterosexual / straight",
                    "Heterosexual/straight",
                    "Heterosexual",
                    "Straight",
                    "Straight / Heterosexual",
                ]
            )
        elif "bisexual" in sl:
            so_candidates.extend(["Bisexual"])
        elif "gay" in sl:
            so_candidates.extend(["Gay"])
        elif "lesbian" in sl:
            so_candidates.extend(["Lesbian"])
        elif "queer" in sl:
            so_candidates.extend(["Queer"])
        selected = _match_selectable_label(field, so_candidates, profile_field="sexual_orientation")
        if selected:
            return mark_visible_self_id_step(
                {**base, "kind": "choice", "value": selected, "source": "application_profile.md"},
                profile_field="sexual_orientation",
            )
    if field["field_type"] in {"ValueSelect", "MultiValueSelect"} and _label_matches(field, "communities", "community"):
        raw_comm = getattr(application_profile, "communities", None) or "None of the above"
        comm_candidates = [raw_comm]
        cl = raw_comm.lower()
        if "none" in cl:
            comm_candidates.extend(["None of the above", "None", "Not applicable", "N/A"])
        selected = _match_selectable_label(field, comm_candidates, profile_field="communities")
        if selected:
            return mark_visible_self_id_step(
                {**base, "kind": "choice", "value": selected, "source": "application_profile.md"},
                profile_field="communities",
            )
    if field["field_type"] in {"ValueSelect", "MultiValueSelect"} and _label_matches(field, "pronoun", "pronouns"):
        raw_pronouns = application_profile.pronouns or ""
        candidates = [raw_pronouns]
        lp = raw_pronouns.lower().replace("/", " ").replace(",", " ")
        if "he" in lp:
            candidates.extend(["He/Him", "He/Him/His", "he him", "he him his"])
        elif "she" in lp:
            candidates.extend(["She/Her", "She/Her/Hers", "she her", "she her hers"])
        elif "they" in lp:
            candidates.extend(["They/Them", "They/Them/Theirs", "they them", "they them theirs"])
        selected = _match_selectable_label(field, candidates, profile_field="pronouns")
        if selected:
            return mark_visible_self_id_step(
                {**base, "kind": "choice", "value": selected, "source": "application_profile.md"},
                profile_field="pronouns",
            )
    if _is_name_pronunciation_field(field):
        pronunciation = str(getattr(application_profile, "name_pronunciation", "") or "").strip()
        if pronunciation:
            return {**base, "kind": "text", "value": pronunciation, "source": "application_profile.md"}
        return None
    if _label_matches(field, "insurance industry", "insurance background") and _label_matches(field, "product manager"):
        has_insurance_pm_background = any(
            employer in profile.employers for employer in {"moody's analytics", "allstate", "lyft"}
        )
        return _yes_no_step(
            field,
            value=has_insurance_pm_background,
            source="master_resume.md",
        )
    _label_category = classify_question(field.get("label") or "")
    policy = resolve_shared_question_policy(field.get("label") or "", application_profile)
    prefer_generated_free_text = question_prefers_generated_free_text_answer(
        field.get("label"),
        field_type=field.get("field_type"),
        category=_label_category,
        policy=policy,
    )
    work_authorization_policy_value = (
        policy.boolean_value
        if policy is not None and policy.category == "work_authorization" and policy.boolean_value is not None
        else None
    )
    if policy is not None and policy.category == "work_authorization":
        if field["field_type"] in {"ValueSelect", "MultiValueSelect"} and policy.text_value is not None:
            selected = _match_selectable_label(field, [policy.text_value])
            if selected:
                return {**base, "kind": _choice_kind(field), "value": selected, "source": policy.source}
        if field["field_type"] == "Boolean" and policy.boolean_value is not None:
            step = _yes_no_step(
                field,
                value=policy.boolean_value,
                source=policy.source,
            )
            if step is not None:
                return step
        if field["field_type"] in {"String", "LongText"} and policy.text_value is not None:
            return {
                **base,
                "kind": "text",
                "value": policy.text_value,
                "source": policy.source,
            }
    if field["field_type"] in {"ValueSelect", "MultiValueSelect"} and policy is not None:
        selected = select_shared_policy_option(
            _selectable_labels(field),
            policy,
            application_profile=application_profile,
        )
        if selected:
            return {**base, "kind": _choice_kind(field), "value": selected, "source": policy.source}
    generated_value = generated_answers.get(field["field_name"])
    if prefer_generated_free_text:
        if isinstance(generated_value, str) and generated_value.strip():
            return {
                **base,
                "kind": "text",
                "value": generated_value.strip(),
                "source": "generated_application_answer",
            }
        return None
    if field["field_type"] in {"String", "LongText"} and _is_conditional_followup({"label": field.get("label") or ""}):
        if isinstance(generated_value, str) and generated_value.strip():
            return {
                **base,
                "kind": "text",
                "value": generated_value.strip(),
                "source": "generated_application_answer",
            }
        return None
    if policy is not None and policy.category != "work_authorization" and policy.boolean_value is not None:
        step = _yes_no_step(
            field,
            value=policy.boolean_value,
            source=policy.source,
        )
        if step is not None:
            return step
    if (
        policy is not None
        and policy.category in {"compensation", "undergraduate_gpa"}
        and field["field_type"] in {"String", "LongText"}
        and policy.text_value is not None
    ):
        return {
            **base,
            "kind": "text",
            "value": policy.text_value,
            "source": policy.source,
        }
    if _label_category == "salary_comfort":
        return _yes_no_step(
            field,
            value=getattr(application_profile, "comfortable_with_posted_salary", True),
            source="application_profile.md",
        )
    if _label_category == "product_usage":
        return _yes_no_step(
            field,
            value=True,
            source="deterministic_override",
        )
    if _label_matches(field, "sponsorship", "sponsor", "visa"):
        requires_sponsorship = (
            work_authorization_policy_value
            if work_authorization_policy_value is not None
            else (application_profile.require_sponsorship_now or application_profile.require_sponsorship_future)
        )
        source = policy.source if work_authorization_policy_value is not None else "application_profile.md"
        step = _yes_no_step(
            field,
            value=requires_sponsorship,
            source=source,
        )
        if step is not None:
            return step
        # Options are not simple yes/no — likely a ValueSelect with descriptive choices
        if field["field_type"] in {"ValueSelect", "MultiValueSelect"}:
            if policy is not None:
                selected = select_shared_policy_option(
                    _selectable_labels(field),
                    policy,
                    application_profile=application_profile,
                )
                if selected:
                    return {**base, "kind": "choice", "value": selected, "source": policy.source}
            if not requires_sponsorship:
                candidates = [
                    "No, I do not require visa sponsorship",
                    "No, I do not require work permit sponsorship",
                    "No, I do not require sponsorship",
                    "No sponsorship needed",
                    "I do not require sponsorship",
                    "No visa sponsorship required",
                ]
            else:
                candidates = [
                    "Yes, I require visa sponsorship",
                    "Yes, I require work permit sponsorship",
                    "Yes, I require sponsorship",
                    "I require sponsorship",
                ]
            selected = _match_selectable_label(field, candidates)
            if selected:
                return {**base, "kind": "choice", "value": selected, "source": source}
    if _label_matches(
        field,
        "authorized to work",
        "authorised to work",
        "work authorization",
        "work authorisation",
        "right to work",
        "legally authorized",
        "legally authorised",
    ):
        authorized_to_work = (
            work_authorization_policy_value
            if work_authorization_policy_value is not None
            else application_profile.authorized_to_work_unconditionally
        )
        source = policy.source if work_authorization_policy_value is not None else "application_profile.md"
        step = _yes_no_step(
            field,
            value=authorized_to_work,
            source=source,
        )
        if step is not None:
            return step
        # Options are not yes/no — likely a ValueSelect with authorization-level choices
        if field["field_type"] in {"ValueSelect", "MultiValueSelect"}:
            if policy is not None:
                selected = select_shared_policy_option(
                    _selectable_labels(field),
                    policy,
                    application_profile=application_profile,
                )
                if selected:
                    return {**base, "kind": "choice", "value": selected, "source": policy.source}
            if authorized_to_work:
                candidates = [
                    "Can work for any employer",
                    "Authorized to work for any employer",
                    "US Citizen",
                    "Green Card",
                    "Permanent Resident",
                    "I am authorized to work in the country due to my nationality",
                    "I am authorized to work in the country based on a valid work permit and do not need a company to sponsor my visa",
                    "I am authorized to work in the country based on a valid work permit which needs to be sponsored by the company I work for",
                ]
            else:
                candidates = [
                    "Seeking work authorization",
                    "Can work for current employer",
                    "Need sponsorship",
                    "Require sponsorship",
                    "I am not authorized to work in the country and need visa support",
                ]
            selected = _match_selectable_label(field, candidates)
            if selected:
                return {**base, "kind": "choice", "value": selected, "source": source}
    if _label_category == "minimum_experience":
        return _yes_no_step(
            field,
            value=policy.boolean_value
            if policy is not None and policy.boolean_value is not None
            else application_profile.minimum_years_experience,
            source=policy.source
            if policy is not None and policy.boolean_value is not None
            else "application_profile.md",
        )
    # Free-text location questions: "What city and state do you currently live in?"
    # Must come before the yes/no "live in" check to avoid returning "Yes" as text.
    label_lower = (field.get("label") or "").strip().lower()
    if any(label_lower.startswith(w) for w in ("what ", "which ", "where ")) and _label_matches(
        field, "live in", "currently live", "reside", "based", "located in", "currently located"
    ):
        if field["field_type"] in {"ValueSelect", "MultiValueSelect"}:
            matched = _match_selectable_label(
                field,
                [str(application_profile.country or "").strip()],
            ) or _office_location_choice(
                field,
                location=application_profile.location,
                country=application_profile.country,
            )
            if matched:
                return mark_visible_profile_field_step(
                    {
                        **base,
                        "kind": _choice_kind(field),
                        "value": matched,
                        "source": "application_profile.md",
                    },
                    profile_field="location",
                )
        return mark_visible_profile_field_step(
            {
                **base,
                "kind": "text",
                "value": application_profile.location,
                "source": "application_profile.md",
            },
            profile_field="location",
        )
    explicit_state_list_membership = explicit_us_state_list_membership_answer(
        field.get("label"),
        field.get("description"),
        application_profile=application_profile,
    )
    if explicit_state_list_membership is not None:
        step = _yes_no_step(
            field,
            value=explicit_state_list_membership,
            source="application_profile.md",
        )
        if step is not None:
            return step
    if _label_matches(field, "live in", "currently live in", "reside in", "based in"):
        step = _yes_no_step(
            field,
            value=policy.boolean_value
            if policy is not None and policy.boolean_value is not None
            else application_profile.lives_in_job_location,
            source=policy.source
            if policy is not None and policy.boolean_value is not None
            else "application_profile.md",
        )
        if step is not None:
            return step
        # Options are not yes/no — likely a state/province/location dropdown.
        matched = _metro_area_choice(
            field,
            location=application_profile.location,
            country=application_profile.country,
        )
        if matched:
            return mark_visible_profile_field_step(
                {
                    **base,
                    "kind": _choice_kind(field),
                    "value": matched,
                    "source": "application_profile.md",
                },
                profile_field="location",
            )
    if _label_matches(field, "willing to relocate", "open to relocate", "open to relocation", "relocate"):
        return _yes_no_step(
            field,
            value=policy.boolean_value
            if policy is not None and policy.boolean_value is not None
            else application_profile.willing_to_relocate,
            source=policy.source
            if policy is not None and policy.boolean_value is not None
            else "application_profile.md",
        )
    if _label_matches(
        field, "willing to work from the required location", "willing to work from", "location alignment"
    ):
        willing = True
        return _yes_no_step(
            field,
            value=willing,
            source="shared_positive_fit_policy",
        )
    if _label_matches(field, "on site", "on-site", "onsite", "in person", "in-person"):
        return _yes_no_step(
            field,
            value=policy.boolean_value
            if policy is not None and policy.boolean_value is not None
            else application_profile.comfortable_working_on_site,
            source=policy.source
            if policy is not None and policy.boolean_value is not None
            else "application_profile.md",
        )
    if (
        _label_category == "office_attendance"
        or _label_matches(field, "commutable distance", "commuting distance", "commute into the office", "hybrid role")
        or (_label_matches(field, "come into our") and _label_matches(field, "office"))
        or (_label_matches(field, "days per week") and _label_matches(field, "office"))
    ):
        matched = select_location_positive_fit_option(
            _selectable_labels(field),
            application_profile=application_profile,
        )
        if matched is not None:
            return {
                **base,
                "kind": _choice_kind(field),
                "value": matched,
                "source": policy.source
                if policy is not None and policy.boolean_value is not None
                else "shared_positive_fit_policy",
            }
        can_commute = (
            policy.boolean_value
            if policy is not None and policy.boolean_value is not None
            else (application_profile.lives_in_job_location and application_profile.comfortable_working_on_site)
        )
        return _yes_no_step(
            field,
            value=can_commute,
            source=policy.source
            if policy is not None and policy.boolean_value is not None
            else "application_profile.md",
        )
    # Privacy/consent/acknowledgment prompts — auto-agree
    if _is_auto_consent_field(field):
        if field["field_type"] in {"MultiValueSelect", "ValueSelect"}:
            labels = _selectable_labels(field)
            if labels:
                return {**base, "kind": _choice_kind(field), "value": labels[0], "source": "auto_consent"}
        if field["field_type"] == "Boolean":
            return _yes_no_step(field, value=True, source="auto_consent")
        if field["field_type"] in {"String", "LongText"}:
            return {**base, "kind": "text", "value": "Yes", "source": "auto_consent"}
    # Date fields — "when can you start", "start date", "available date", etc.
    if field["field_type"] == "Date":
        start = profile_available_start_date(application_profile)
        return {**base, "kind": "text", "value": start.isoformat(), "source": "application_profile.md"}
    if isinstance(generated_value, str) and generated_value.strip():
        # For ValueSelect/MultiValueSelect fields, match the generated text against options
        if field["field_type"] in {"ValueSelect", "MultiValueSelect"}:
            matched_option = _ashby_option_matcher(field, [generated_value.strip()])
            if matched_option:
                return {
                    **base,
                    "kind": _choice_kind(field),
                    "value": matched_option,
                    "source": "generated_application_answer",
                }
            return None
        if field["field_type"] == "Number":
            # Extract numeric value from generated text
            digits = re.sub(r"[^\d]", "", generated_value.strip())
            if digits:
                return {
                    **base,
                    "kind": "text",
                    "value": digits,
                    "source": "generated_application_answer",
                }
            return None
        if field["field_type"] == "Boolean":
            val_lower = generated_value.strip().lower()
            bool_val = val_lower in {"yes", "true", "1"}
            return _yes_no_step(
                field,
                value=bool_val,
                source="generated_application_answer",
            )
        return {
            **base,
            "kind": "text",
            "value": generated_value.strip(),
            "source": "generated_application_answer",
        }
    return None


def _pending_user_input_fields(fields: list[dict], *, application_profile=None) -> list[dict]:
    pending: list[dict] = []
    for field in fields:
        reason = pending_user_input_reason_for_spec(field, application_profile)
        if not reason:
            continue
        pending.append(
            {
                "field_name": field["field_name"],
                "label": field["label"],
                "field_type": field["field_type"],
                "required": field["required"],
                "path": field["path"],
                "reason": reason,
            }
        )
    return pending


def _question_specs(fields: list[dict], generated_field_names: set[str]) -> list[dict]:
    specs = []
    for field in fields:
        if field["field_name"] not in generated_field_names:
            continue
        spec: dict = {
            "field_name": field["field_name"],
            "label": field["label"],
            "description": str(field.get("description") or "").strip(),
            "required": field["required"],
            "type": field["field_type"],
        }
        # Include options for ValueSelect/MultiValueSelect so the LLM knows valid answers
        if field["field_type"] in {"ValueSelect", "MultiValueSelect"}:
            options = _selectable_labels(field)
            if options:
                spec["options"] = options
        specs.append(spec)
    return specs


def _write_unknown_questions(out_dir: Path, unknown_questions: list[dict]) -> None:
    path = role_submit_path(out_dir, AUTOFILL_UNKNOWN_QUESTIONS_JSON)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "questions": unknown_questions,
    }
    path.write_text(json_dumps_pretty(payload) + "\n", encoding="utf-8")


def _build_payload(out_dir: Path, provider: str | None = None) -> dict:
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)
    profile = parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    job_url = str(meta.get("jd_source_resolved") or meta["jd_source"])
    application_url = _application_url_for_job_url(job_url)
    html = _fetch_application_html(application_url, cache_path=role_submit_path(out_dir, APPLICATION_PAGE_HTML))
    app_data = _extract_app_data(html)
    posting = app_data.get("posting") or {}
    fields = _field_entries_from_app_data(app_data)
    pending_user_input = _pending_user_input_fields(fields, application_profile=application_profile)
    if pending_user_input:
        pending_path = write_pending_user_input(
            out_dir,
            board="ashby",
            questions=pending_user_input,
        )
        labels = ", ".join(question["label"] for question in pending_user_input)
        raise ValueError(
            f"Ashby submit requires explicit user input before submission for: {labels}. See {pending_path}"
        )
    clear_pending_user_input(out_dir)

    deterministic_fields: list[dict] = []
    generated_candidates: list[dict] = []
    unknown_questions: list[dict] = []
    for field in fields:
        website_field = _is_website_field(field)
        linkedin_field = _is_linkedin_field(field)
        is_personal_name = field["path"] == "_systemfield_name" or _label_matches(
            field, "first name", "last name", "full name", "preferred first name"
        )
        label_category = classify_question(field.get("label", ""))
        shared_policy = resolve_shared_question_policy(field.get("label") or "", application_profile)
        prefer_generated_free_text = question_prefers_generated_free_text_answer(
            field.get("label"),
            field_type=field.get("field_type"),
            category=label_category,
            policy=shared_policy,
        )
        excluded_generated_label = _label_matches(
            field,
            "email",
            "phone",
            "location",
            "zip code",
            "postal code",
            "resume",
            "cover letter",
            "how did you hear",
            "how did you learn",
            "authorized",
            "sponsorship",
            "minimum years",
            "relocate",
            "willing to work from",
            "on site",
            "in person",
            "live in",
            "based in",
            "current age",
            "age group",
            "age range",
            "future contact consent",
            "gender",
            "transgender",
            "race",
            "ethnicity",
            "veteran",
            "disability",
            "sexual orientation",
            "orientation",
            "community",
            "communities",
            "pronoun",
            "if yes",
            "if you selected yes",
            "if selected yes",
        )
        if (
            _field_supports_generated_answer(field)
            and not is_personal_name
            and not linkedin_field
            and not _is_name_pronunciation_field(field)
            and not _is_auto_consent_field(field)
            and not _is_conditional_yes_followup(field)
            and (prefer_generated_free_text or not excluded_generated_label)
            and (label_category is None or prefer_generated_free_text)
            and not build_onsite_start_location_answer(field.get("label"), application_profile)
            and not website_field
        ):
            generated_candidates.append(field)
            continue
        deterministic_fields.append(field)

    try:
        generated_answers = generate_application_answers(
            out_dir=out_dir,
            meta=meta,
            question_specs=_question_specs(fields, {field["field_name"] for field in generated_candidates}),
            provider=provider,
        )
    except GeneratedAnswerBlockersError as exc:
        pending_path = write_pending_user_input_for_unconfirmed_fields(
            out_dir,
            board="ashby",
            fields=exc.blockers,
        )
        labels = ", ".join(
            str(blocker.get("label") or blocker.get("field_name") or "").strip() for blocker in exc.blockers
        )
        raise ValueError(
            f"Ashby submit requires review for generated-answer regressions: {labels}. See {pending_path}"
        ) from exc

    steps: list[dict] = []
    previous_step: dict | None = None
    for field in fields:
        if _is_conditional_yes_followup(field) and _step_is_negative_yes_no(previous_step):
            previous_step = None
            continue
        step = _infer_step(
            field,
            meta=meta,
            profile=profile,
            application_profile=application_profile,
            out_dir=out_dir,
            generated_answers=generated_answers,
        )
        if step:
            steps.append(step)
            previous_step = step
            continue
        status = "unknown_required" if field["required"] else "unknown_optional"
        unknown_questions.append(
            {
                "field_name": field["field_name"],
                "label": field["label"],
                "field_type": field["field_type"],
                "required": field["required"],
                "path": field["path"],
                "status": status,
                **infer_unknown_question_blocker_metadata(
                    field_name=field["field_name"],
                    label=field["label"],
                    application_profile=application_profile,
                    profile=profile,
                ),
            }
        )
        previous_step = None

    _write_unknown_questions(out_dir, unknown_questions)
    missing_required = [question for question in unknown_questions if question["required"]]
    if missing_required:
        labels = ", ".join(question["label"] for question in missing_required)
        raise ValueError(
            f"Autofill payload is missing answers for required Ashby fields: {labels}. "
            f"See {role_submit_path(out_dir, AUTOFILL_UNKNOWN_QUESTIONS_JSON)}"
        )

    return {
        "job_url": application_url,
        "application_url": application_url,
        "out_dir": str(out_dir),
        "job_title": str(posting.get("title") or meta.get("jd_title") or ""),
        "company": meta["company_proper"],
        "mode": "review-before-submit",
        "artifacts": {
            "payload_path": str(role_submit_path(out_dir, AUTOFILL_PAYLOAD_JSON)),
            "report_markdown": str(role_submit_path(out_dir, AUTOFILL_REPORT_MD)),
            "report_json": str(role_submit_path(out_dir, AUTOFILL_REPORT_JSON)),
            "pre_submit_screenshot": str(role_submit_path(out_dir, AUTOFILL_PRE_SUBMIT_SCREENSHOT)),
            "page_screenshots_dir": str(role_submit_path(out_dir, AUTOFILL_PAGE_SCREENSHOTS_DIR)),
            "unknown_questions_json": str(role_submit_path(out_dir, AUTOFILL_UNKNOWN_QUESTIONS_JSON)),
            "submit_debug_html": str(role_submit_path(out_dir, AUTOFILL_SUBMIT_DEBUG_HTML)),
            "submit_debug_screenshot": str(role_submit_path(out_dir, AUTOFILL_SUBMIT_DEBUG_SCREENSHOT)),
            "application_page_html": str(role_submit_path(out_dir, APPLICATION_PAGE_HTML)),
            "application_profile": str(APPLICATION_PROFILE_PATH),
            "website_confirmation_json": str(role_submit_path(out_dir, WEBSITE_CONFIRMATION_JSON)),
            "email_confirmation_json": str(role_submit_path(out_dir, EMAIL_CONFIRMATION_JSON)),
            "notion_sync_status_json": str(role_submit_path(out_dir, NOTION_SYNC_STATUS_JSON)),
        },
        "fields": fields,
        "steps": steps,
        "unknown_questions": unknown_questions,
    }


def _write_report(payload: dict, runtime: dict | None = None) -> dict:
    from autofill_common import write_report

    return write_report(payload, board_name="ashby", runtime=runtime)


def _capture_full_page(page, path: Path, *, preferred_selectors: tuple[str, ...] = ()) -> None:
    selectors = (*preferred_selectors, *PREFERRED_CAPTURE_SELECTORS)
    capture_full_page(page, path, preferred_selectors=selectors)


def _field_entry(page, label: str, *, path: str | None = None):
    entries = page.locator(FIELD_ENTRY_SELECTOR)

    # When a unique path (field UUID) is provided, try to locate the field entry
    # that contains an element whose name or id matches the path.  This
    # disambiguates fields that share the same visible label.
    if path:
        path_selectors = (
            f'[name="{path}"]',
            f'[id="{path}"]',
            f'label[for="{path}"]',
            # Some Ashby survey controls render generated ids that append the
            # field path, so exact path matching cannot find the live checkbox.
            f'[id*="__{path}"]',
            f'[id*="{path}-"]',
            f'[for*="__{path}"]',
            f'[for*="{path}-"]',
        )
        for selector in path_selectors:
            by_path = entries.filter(has=page.locator(selector)).first
            if by_path.count():
                return by_path

    entry = entries.filter(has=page.locator("label", has_text=label)).first
    if entry.count():
        return entry
    entry = entries.filter(has_text=label).first
    if entry.count():
        return entry
    return entries.filter(has=page.get_by_text(label, exact=True)).first


def _fillable_text_locator(page, path: str):
    selectors = (
        f'input[name="{path}"]',
        f'textarea[name="{path}"]',
        f'input[id="{path}"]',
        f'textarea[id="{path}"]',
    )
    for selector in selectors:
        locator = page.locator(selector).first
        if not locator.count():
            continue
        try:
            # Scroll into view before visibility check — textareas below the
            # fold may report is_visible()=False until they enter the viewport.
            try:
                locator.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            if not locator.is_visible():
                continue
            input_type = locator.evaluate(
                """(element) => {
                    const type = (element.getAttribute('type') || '').toLowerCase();
                    return type;
                }"""
            )
            if input_type in {"checkbox", "radio", "hidden", "file"}:
                continue
        except Exception:
            continue
        return locator
    return None


def _click_choice(entry, value: str) -> bool:
    def _scroll_into_view(locator) -> None:
        if not hasattr(locator, "scroll_into_view_if_needed"):
            return
        try:
            locator.scroll_into_view_if_needed(timeout=2000)
        except TypeError:
            locator.scroll_into_view_if_needed()
        except Exception:
            return

    def _first_visible(locator):
        if not hasattr(locator, "nth"):
            candidate = getattr(locator, "first", locator)
            try:
                if hasattr(candidate, "is_visible") and not candidate.is_visible():
                    return None
            except Exception:
                return None
            return candidate
        for index in range(locator.count()):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible():
                    return candidate
            except Exception:
                continue
        return None

    button_option = _first_visible(entry.get_by_role("button", name=value))
    if button_option is not None:
        _scroll_into_view(button_option)
        button_option.click()
        return True

    labels = entry.locator("label")
    for index in range(labels.count()):
        label_option = labels.nth(index)
        try:
            if not label_option.is_visible():
                continue
            label_text = label_option.inner_text().strip()
        except Exception:
            continue
        if _choice_text_matches(value, label_text):
            option_container = entry.locator('div[class*="_option_"]').filter(has_text=label_text).first
            if option_container.count():
                _scroll_into_view(option_container)
            _scroll_into_view(label_option)
            try:
                entry.page.wait_for_timeout(250)
            except Exception:
                pass
            wrapped_input = label_option.locator("input[type='radio'], input[type='checkbox']").first
            if wrapped_input.count():
                _scroll_into_view(wrapped_input)
                try:
                    wrapped_input.check(force=True)
                    return True
                except Exception:
                    try:
                        if wrapped_input.is_checked():
                            return True
                    except Exception:
                        pass
            try:
                label_option.click()
                return True
            except Exception:
                pass
            try:
                target_id = label_option.get_attribute("for")
            except Exception:
                target_id = None
            if target_id:
                linked_input = entry.locator(f'input[id="{target_id}"]').first
                if linked_input.count():
                    _scroll_into_view(linked_input)
                    try:
                        linked_input.check(force=True)
                        return True
                    except Exception:
                        try:
                            if linked_input.is_checked():
                                return True
                        except Exception:
                            pass

    radio = _first_visible(entry.get_by_role("radio", name=value))
    if radio is not None:
        _scroll_into_view(radio)
        radio.click()
        return True

    checkbox = _first_visible(entry.get_by_role("checkbox", name=value))
    if checkbox is not None:
        _scroll_into_view(checkbox)
        checkbox.click()
        return True

    option = _first_visible(entry.get_by_role("option", name=value))
    if option is not None:
        _scroll_into_view(option)
        option.click()
        return True

    return False


def _fill_text_value(locator, value: str, *, field_type: str) -> None:
    if field_type == "LongText" or len(value) >= 400:
        locator.scroll_into_view_if_needed()
        locator.click()
        locator.page.wait_for_timeout(200)
        locator.fill(value)
        locator.page.wait_for_timeout(300)
        # Verify the value persisted — React controlled components can reset
        # the textarea on re-render if the fill events aren't processed.
        actual = _read_control_value(locator)
        if not actual.strip():
            # Fallback: use native setter to bypass React's controlled input check
            locator.evaluate(
                """(el, val) => {
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    )?.set || Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    )?.set;
                    if (nativeSetter) nativeSetter.call(el, val);
                    else el.value = val;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                value,
            )
            locator.page.wait_for_timeout(300)
            actual = _read_control_value(locator)
            if not actual.strip():
                # Last resort: focus and type a small prefix then fill
                locator.click()
                locator.press_sequentially(value[:5], delay=30)
                locator.page.wait_for_timeout(100)
                locator.fill(value)
        return
    human_fill(locator, value)


def _is_cover_letter_step(step: dict) -> bool:
    if step.get("source") in {"cover_letter_text.txt", "existing_cover_letter_asset"}:
        return True
    if "cover_letter" in str(step.get("field_name") or ""):
        return True
    return "cover letter" in normalize_text(step.get("label"))


def _read_control_value(locator) -> str:
    try:
        return locator.input_value()
    except Exception:
        return str(
            locator.evaluate(
                """(element) => {
                    if (typeof element.value === 'string') {
                        return element.value;
                    }
                    return element.textContent || '';
                }"""
            )
            or ""
        )


def _choice_text_matches(expected: str, observed: str) -> bool:
    expected_normalized = normalize_text(expected)
    observed_normalized = normalize_text(observed)
    if not expected_normalized or not observed_normalized:
        return False
    if expected_normalized == observed_normalized:
        return True

    expected_tokens = expected_normalized.split()
    observed_tokens = observed_normalized.split()
    if len(expected_tokens) == 1 and expected_normalized in {"yes", "no"}:
        opposite = "no" if expected_normalized == "yes" else "yes"
        # Avoid matching field labels like "(Yes/No)" or container text that
        # mentions both choices when we need the concrete selected option.
        if opposite in observed_tokens:
            return False
    if len(expected_tokens) == 1:
        return expected_normalized in observed_tokens
    if len(observed_tokens) == 1:
        return observed_normalized in expected_tokens
    return expected_normalized in observed_normalized or observed_normalized in expected_normalized


def _confirm_cover_letter_text(locator, expected: str) -> bool:
    return normalize_text(_read_control_value(locator)) == normalize_text(expected)


def _confirm_file_attached(locator) -> bool:
    try:
        return bool(locator.evaluate("(element) => !!(element.files && element.files.length > 0)"))
    except Exception:
        return False


def _mark_choice_step_unconfirmed(step: dict) -> None:
    step["filled"] = False
    step["status"] = "planned"
    note = str(step.get("note") or "").strip()
    confirmation_note = "Selected the planned answer but could not confirm it remained visible on the live form."
    if confirmation_note not in note:
        step["note"] = f"{note} {confirmation_note}".strip()


def _mark_visible_self_id_unconfirmed(step: dict) -> None:
    step["filled"] = False
    step["status"] = "planned"
    step["note"] = "Filled the planned value but could not confirm it remained visible on the live form."


def _confirm_choice_step(entry, step: dict, *, locator=None) -> bool:
    expected = normalize_text(step.get("value"))
    if not expected:
        return False
    if step.get("kind") in {"text", "textarea", "location"} and locator is not None:
        try:
            actual = locator.input_value()
        except Exception:
            try:
                actual = locator.inner_text()
            except Exception:
                actual = ""
        normalized_actual = normalize_text(actual)
        return bool(normalized_actual) and (normalized_actual == expected or expected in normalized_actual)
    try:
        selected_texts = entry.evaluate(
            """(node) => {
                const selectedTexts = [];
                const push = (value) => {
                    if (value) selectedTexts.push(value);
                };
                for (const input of node.querySelectorAll("input[type='radio']:checked, input[type='checkbox']:checked")) {
                    const labels = Array.from(input.labels || []).map(label => label.textContent || "");
                    if (labels.length) {
                        labels.forEach(push);
                    } else if (input.closest("label")) {
                        push(input.closest("label").textContent || "");
                    } else if (input.parentElement) {
                        push(input.parentElement.textContent || "");
                    }
                }
                for (const active of node.querySelectorAll(
                    "[role='radio'][aria-checked='true'], [role='checkbox'][aria-checked='true'], "
                    + "button._active, button[class*='_active'], button[aria-pressed='true'], "
                    + "option:checked, [role='option'][aria-selected='true']"
                )) {
                    push(active.textContent || "");
                }
                const combobox = node.querySelector("[role='combobox']");
                if (combobox) {
                    push(combobox.value || combobox.textContent || "");
                }
                return selectedTexts;
            }"""
        )
    except Exception:
        return False
    return any(_choice_text_matches(expected, str(text or "")) for text in (selected_texts or []))


def _confirm_visible_self_id_step(entry, step: dict, *, locator=None) -> bool:
    return _confirm_choice_step(entry, step, locator=locator)


def _fill_phone_text_message_consent(entry, consent: bool) -> bool:
    radio_group = entry.locator('input[name="communicationConsent"]').first
    if not radio_group.count():
        return False

    target_value = "given" if consent else "notGiven"
    label = entry.locator(f'label:has(input[name="communicationConsent"][value="{target_value}"])').first
    if label.count():
        label.click()
        return True

    radio = entry.locator(f'input[name="communicationConsent"][value="{target_value}"]').first
    if radio.count():
        radio.check(force=True)
        return True

    target_label = (
        "Yes - I consent to receiving text messages" if consent else "No - I do not consent to receiving text messages"
    )
    return bool(_click_choice(entry, target_label))


def _select_location(page, step: dict) -> None:
    entry = _field_entry(page, step["label"])
    combobox = entry.locator('[role="combobox"]').first
    if not combobox.count():
        raise RuntimeError(f"Could not find Ashby location combobox for {step['label']}")

    last_error: Exception | None = None
    for search_text in step.get("search_variants", [step["value"]]):
        try:
            human_fill(combobox, search_text)
            page.wait_for_timeout(500)
            options = page.locator('[role="option"]')
            options.first.wait_for(timeout=5000)
            option_count = options.count()
            preferred = normalize_text(search_text.split(",")[0])
            country = normalize_text("United States")
            selected = None
            for index in range(option_count):
                option = options.nth(index)
                text = option.inner_text().strip()
                normalized = normalize_text(text)
                if preferred in normalized and country in normalized:
                    selected = option
                    break
            if selected is None:
                selected = options.first
            selected.click()
            page.wait_for_timeout(500)
            return combobox
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"Could not select Ashby location for {step['value']}") from last_error


def _select_choice_via_combobox(page, entry, value: str) -> bool:
    """Select a choice for Ashby fields that render ValueSelects as comboboxes."""
    combobox = entry.locator('[role="combobox"]').first
    if not combobox.count():
        return False

    human_fill(combobox, value)
    page.wait_for_timeout(300)
    options = page.locator('[role="option"]')
    option_count = options.count()
    if not option_count:
        return False

    preferred = normalize_text(value)
    for index in range(option_count):
        option = options.nth(index)
        try:
            option_text = option.inner_text().strip()
        except Exception:
            continue
        if preferred in normalize_text(option_text):
            option.click()
            return True

    options.first.click()
    return True


def _ensure_boolean_checkbox(page, entry, value: str) -> None:
    """Ensure Ashby Boolean field's hidden checkbox matches the button selection.

    Ashby Boolean Yes/No components use a hidden checkbox underneath the buttons.
    Clicking the button adds the visual ``_active`` class but may not update the
    checkbox ``checked`` property, causing form validation to reject the field.
    """
    checkbox = entry.locator('input[type="checkbox"]').first
    if not checkbox.count():
        return
    should_be_checked = value.lower() in {"yes", "true"}
    try:
        is_checked = checkbox.is_checked()
    except Exception:
        return
    if should_be_checked != is_checked:
        checkbox.evaluate(
            """(el, checked) => {
                el.checked = checked;
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('input', { bubbles: true }));
            }""",
            should_be_checked,
        )
        page.wait_for_timeout(100)


def _fill_step(page, step: dict) -> None:
    kind = step["kind"]
    path = step["path"]

    if kind == "location":
        locator = _select_location(page, step)
        if is_visible_confirmation_blocker(step) and not _confirm_visible_self_id_step(
            _field_entry(page, step["label"]),
            step,
            locator=locator,
        ):
            _mark_visible_self_id_unconfirmed(step)
            return
        step["filled"] = True
        return

    if kind == "file":
        locator = page.locator(f'input[id="{path}"]').first
        if not locator.count():
            locator = page.locator(f'input[name="{path}"]').first
        if not locator.count():
            raise RuntimeError(f"Could not find Ashby file input for {step['label']}")
        locator.set_input_files(step["file_path"])
        if _is_cover_letter_step(step) and not _confirm_file_attached(locator):
            raise RuntimeError(f"Could not confirm Ashby cover letter upload for {step['label']}")
        step["filled"] = True
        return

    entry = _field_entry(page, step["label"], path=path)
    if kind == "choice":
        if _click_choice(entry, step["value"]):
            # Ashby Boolean fields render as Yes/No buttons with a hidden checkbox.
            # The button click may add the visual _active class without updating the
            # checkbox, causing form validation to fail.  Force-set the checkbox state.
            if step.get("field_type") == "Boolean":
                _ensure_boolean_checkbox(page, entry, step["value"])
            refreshed_entry = _field_entry(page, step["label"], path=path)
            try:
                if refreshed_entry.count():
                    entry = refreshed_entry
            except Exception:
                pass
            if is_visible_confirmation_blocker(step) and not _confirm_visible_self_id_step(entry, step):
                _mark_visible_self_id_unconfirmed(step)
                return
            if not _confirm_choice_step(entry, step):
                _mark_choice_step_unconfirmed(step)
                return
            step["filled"] = True
            return
        if _select_choice_via_combobox(page, entry, step["value"]):
            if is_visible_confirmation_blocker(step) and not _confirm_visible_self_id_step(entry, step):
                _mark_visible_self_id_unconfirmed(step)
                return
            step["filled"] = True
            return
        if not step.get("required", True):
            step["filled"] = False
            step["status"] = "skipped_not_found"
            return
        raise RuntimeError(f"Could not find Ashby choice input for {step['label']}")

    locator = _fillable_text_locator(page, path)
    # Fallback: find input/textarea inside the field entry container (Ashby React
    # may not set name/id attributes on custom fields like "Current location").
    if locator is None:
        for sel in ("input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=file])", "textarea"):
            candidate = entry.locator(sel).first
            if candidate.count() and candidate.is_visible():
                locator = candidate
                break
    if locator is not None:
        _fill_text_value(locator, step["value"], field_type=step.get("field_type", ""))
        if _is_cover_letter_step(step) and not _confirm_cover_letter_text(locator, str(step["value"] or "")):
            raise RuntimeError(f"Could not confirm Ashby cover letter text for {step['label']}")
        if step.get("field_type") == "Phone" and "text_message_consent" in step:
            consent_filled = _fill_phone_text_message_consent(entry, bool(step["text_message_consent"]))
            if not consent_filled and entry.locator('input[name="communicationConsent"]').count():
                raise RuntimeError(f"Could not set Ashby text message consent for {step['label']}")
        if is_visible_confirmation_blocker(step) and not _confirm_visible_self_id_step(entry, step, locator=locator):
            _mark_visible_self_id_unconfirmed(step)
            return
        step["filled"] = True
        return

    if _select_choice_via_combobox(page, entry, step["value"]):
        if is_visible_confirmation_blocker(step) and not _confirm_visible_self_id_step(entry, step):
            _mark_visible_self_id_unconfirmed(step)
            return
        step["filled"] = True
        return

    if _click_choice(entry, step["value"]):
        if is_visible_confirmation_blocker(step) and not _confirm_visible_self_id_step(entry, step):
            _mark_visible_self_id_unconfirmed(step)
            return
        step["filled"] = True
        return

    if not step.get("required", True):
        step["filled"] = False
        step["status"] = "skipped_not_found"
        return
    raise RuntimeError(f"Could not find Ashby field input for {step['label']}")


def _page_snapshot(page) -> dict:
    return page_snapshot(page, form_selector=FORM_READY_SELECTOR, captcha_type="recaptcha")


_ASHBY_CAPTCHA_REJECTION_RE = re.compile(
    r"(?:couldn't|could not) submit your application",
    re.I,
)


def _classify_submit_state(snapshot: dict) -> dict[str, object]:
    page_text = snapshot.get("page_text", "")
    if any(pattern.search(page_text) for pattern in SUBMIT_CONFIRM_PATTERNS):
        return {"status": "confirmed", "reason": "text"}
    invalid_fields = list(snapshot.get("invalid_fields") or [])
    explicit_errors = list(snapshot.get("errors") or [])

    # Ashby invisible reCAPTCHA rejection shows a generic alert
    # "We couldn't submit your application" with no specific invalid
    # fields.  Treat this as a captcha block so the pipeline skips
    # gracefully instead of reporting a validation error.
    if not invalid_fields and any(_ASHBY_CAPTCHA_REJECTION_RE.search(e) for e in explicit_errors):
        return {"status": "captcha_required"}
    # Also check page_text for spam rejection (may not appear in explicit_errors)
    if _ASHBY_CAPTCHA_REJECTION_RE.search(page_text) and "flagged" in page_text.lower() and "spam" in page_text.lower():
        return {"status": "captcha_required"}

    # Only use page-level pattern matching as a signal when there are also
    # structural indicators (invalid_fields or explicit_errors).  On their
    # own, page_level_errors produce false positives from descriptive form
    # text (e.g. "Please select where you first heard about this job?").
    page_level_errors = [
        pattern.search(page_text).group(0) for pattern in VALIDATION_ERROR_PATTERNS if pattern.search(page_text)
    ]
    combined_errors = list(dict.fromkeys(explicit_errors + page_level_errors))
    if invalid_fields or explicit_errors:
        return {
            "status": "validation_error",
            "errors": combined_errors,
            "invalid_fields": invalid_fields,
        }
    if snapshot.get("recaptcha_challenge_active") and snapshot.get("form_visible"):
        return {"status": "captcha_required"}
    return {"status": "pending"}


def _click_submit_button(page) -> bool:
    # Generic wait_for_pending_uploads is called inside click_submit_button
    return click_submit_button(page, button_names=SUBMIT_BUTTON_NAMES)


def _confirmed_outcome_from_email(snapshot: dict | None, email_confirmation: dict) -> dict[str, object]:
    snapshot = dict(snapshot or {})
    snapshot.setdefault("page_text", "(matched application confirmation email while browser confirmation was pending)")
    return {
        "status": "confirmed",
        "reason": "email_confirmation",
        "snapshot": snapshot,
        "email_confirmation": email_confirmation,
    }


def _wait_for_ashby_form(page, payload) -> None:
    """Navigate to Ashby's application URL and wait for the form."""
    application_url = payload.get("application_url", payload["job_url"])
    current = page.url.rstrip("/")
    target = application_url.rstrip("/")
    if current != target:
        page.goto(application_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_selector(FORM_READY_SELECTOR, timeout=25000)


def main() -> int:
    from autofill_pipeline import autofill_main, run_browser_pipeline

    return autofill_main(
        board_name="ashby",
        build_payload_fn=_build_payload,
        run_browser_fn=lambda pp, headless, submit: run_browser_pipeline(
            pp,
            headless=headless,
            submit=submit,
            board_name="ashby",
            retry_unconfirmed_visible_self_id_once=True,
            form_ready_fn=lambda page: _wait_for_ashby_form(page, json.loads(pp.read_text(encoding="utf-8"))),
            fill_step_fn=_fill_step,
            page_snapshot_fn=_page_snapshot,
            classify_state_fn=_classify_submit_state,
            click_submit_fn=_click_submit_button,
            capture_fn=lambda page, path: _capture_full_page(page, path),
            confirmed_outcome_from_email_fn=_confirmed_outcome_from_email,
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
