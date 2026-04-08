#!/usr/bin/env python3
"""Workable application autofill.

Single-page form at apply.workable.com -- no auth, no wizard.
Uses run_browser_pipeline(). Handles Cloudflare Turnstile CAPTCHA.

URL patterns:
  - apply.workable.com/{company}/j/{job_id}/  (application form)
  - jobs.workable.com/view/{id}/              (listing -> links to apply form)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_submit_common import (
    APPLICATION_PROFILE_PATH,
    PROJECT_ROOT,
    append_url_path_suffix,
    find_cover_letter_file,
    find_cover_letter_text,
    find_resume_file,
    generate_application_answers,
    load_meta,
    parse_application_profile,
    parse_master_resume,
    question_is_culture_careers_optin,
    resolve_shared_question_policy,
    slugify_label,
)
from autofill_common import (
    board_file_constants,
    capture_full_page,
    click_submit_button,
    label_matches,
    select_option,
    select_shared_policy_option,
)
from output_layout import migrate_role_output_layout, role_submit_path
from project_env import load_project_env

_BOARD = "workable"
_BOARD_CONSTANTS = board_file_constants(_BOARD)
AUTOFILL_PAYLOAD_JSON = _BOARD_CONSTANTS["payload_json"]
APPLICATION_PAGE_HTML = _BOARD_CONSTANTS["application_page_html"]

SUBMIT_BUTTON_NAMES = (
    "Submit application",
    "Submit Application",
    "Submit",
    "Apply",
)

SUBMIT_CONFIRM_PATTERNS = (
    re.compile(r"\bthank(?:s| you)\s+for\s+(?:applying|your application)\b", re.I),
    re.compile(r"\bapplication (?:has been )?(?:received|submitted)\b", re.I),
    re.compile(r"\bwe(?:'|')ve received your application\b", re.I),
    re.compile(r"\bsuccessfully submitted\b", re.I),
    re.compile(r"\bsubmitted successfully\b", re.I),
    re.compile(r"\balready applied\b", re.I),
)

VALIDATION_ERROR_PATTERNS = (
    re.compile(r"\bthis field is required\b", re.I),
    re.compile(r"\bplease (?:enter|select|complete|fill)\b", re.I),
    re.compile(r"\binvalid\b", re.I),
    re.compile(r"\berror\b", re.I),
)

load_project_env()

_TEXT_LIKE_KINDS = {"text", "textarea"}


# --- Deterministic overrides ---


def _infer_deterministic(label: str, options: list[str]) -> str | None:
    """Return a deterministic answer or None to defer to LLM."""
    ll = label.casefold()
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    policy = resolve_shared_question_policy(label, application_profile)
    if policy is not None and policy.text_value is not None:
        return select_shared_policy_option(options, policy, application_profile=application_profile) or policy.text_value

    # Previous employee -> No
    if "previous employee" in ll or "former employee" in ll:
        return select_option(options, "No") or "No"

    # Work authorisation -> Yes
    if (
        "authorized to work" in ll
        or "authorised to work" in ll
        or "legally authorized" in ll
        or "eligible to work" in ll
        or "legally eligible" in ll
    ):
        return select_option(options, "Yes") or "Yes"

    # Sponsorship -> No
    if "require sponsorship" in ll or "need sponsorship" in ll:
        return select_option(options, "No") or "No"

    # Age verification -> Yes
    if "18 years" in ll or "legal age" in ll:
        return select_option(options, "Yes") or "Yes"

    # Culture/careers opt-in -> Yes
    if question_is_culture_careers_optin(label):
        return select_option(options, "Yes") or "Yes"

    return None


def _field_label_matches(text_or_field: str | dict, *fragments: str) -> bool:
    return label_matches(text_or_field, *fragments)


def _normalize_live_kind(raw_kind: str | None) -> str:
    kind = str(raw_kind or "").strip().casefold()
    if kind in {"text", "textarea", "file", "select"}:
        return kind
    return "text"


def _clean_live_label(label: str) -> str:
    text = str(label or "").strip()
    text = re.sub(r"\s*\(\s*optional\s*\)\s*$", "", text, flags=re.I)
    return text.strip()


def _live_field_base_name(field: dict) -> str:
    label = str(field.get("label") or "")
    kind = str(field.get("kind") or "")
    if _field_label_matches(label, "first name"):
        return "first_name"
    if _field_label_matches(label, "last name"):
        return "last_name"
    if _field_label_matches(label, "email"):
        return "email"
    if _field_label_matches(label, "phone"):
        return "phone"
    if kind == "file" and _field_label_matches(label, "resume", "cv"):
        return "resume"
    if _field_label_matches(label, "cover letter"):
        return "cover_letter"
    if _field_label_matches(label, "additional links", "portfolio", "website", "socials"):
        return "website"
    if _field_label_matches(label, "linkedin"):
        return "linkedin"
    if _field_label_matches(label, "current location"):
        return "current_location"
    if _field_label_matches(label, "city"):
        return "city"
    if _field_label_matches(label, "location"):
        return "location"
    fallback = slugify_label(label or str(field.get("name") or "") or str(field.get("id") or "") or "field")
    return fallback or "field"


def _live_field_type(kind: str) -> str:
    if kind == "textarea":
        return "LongText"
    if kind == "select":
        return "Select"
    if kind == "file":
        return "File"
    return "ShortText"


def _normalize_live_fields(discovered_fields: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    fields: list[dict] = []
    for raw_field in discovered_fields:
        label = _clean_live_label(raw_field.get("label") or "")
        if not label:
            continue
        kind = _normalize_live_kind(raw_field.get("kind"))
        raw_name = str(raw_field.get("name") or "").strip()
        raw_id = str(raw_field.get("id") or "").strip()
        if kind == "file" and label in {raw_name, raw_id}:
            continue
        if kind == "file" and label.casefold().startswith("input_files_input_"):
            continue
        base_name = _live_field_base_name({**raw_field, "label": label, "kind": kind})
        counts[base_name] = counts.get(base_name, 0) + 1
        field_name = base_name if counts[base_name] == 1 else f"{base_name}_{counts[base_name]}"
        path = str(raw_field.get("name") or raw_field.get("id") or "").strip()
        options = [str(option).strip() for option in list(raw_field.get("options") or []) if str(option).strip()]
        fields.append(
            {
                "field_name": field_name,
                "label": label,
                "kind": kind,
                "required": bool(raw_field.get("required")),
                "name": str(raw_field.get("name") or "").strip(),
                "id": str(raw_field.get("id") or "").strip(),
                "path": path,
                "options": options,
                "field_type": _live_field_type(kind),
            }
        )
    return fields


def _step_kind_matches_live_kind(step_kind: str, live_kind: str) -> bool:
    if step_kind in _TEXT_LIKE_KINDS and live_kind in _TEXT_LIKE_KINDS:
        return True
    return step_kind == live_kind


def _apply_live_field_to_step(step: dict, field: dict) -> dict:
    updated = dict(step)
    updated["label"] = field["label"]
    updated["required"] = bool(field["required"])
    updated["field_type"] = field["field_type"]
    if field.get("name"):
        updated["name"] = field["name"]
    if field.get("path"):
        updated["path"] = field["path"]
    if field["kind"] in _TEXT_LIKE_KINDS and updated.get("kind") in _TEXT_LIKE_KINDS:
        updated["kind"] = field["kind"]
    elif field["kind"] == "file":
        updated["kind"] = "file"
    if field["kind"] == "select":
        updated["kind"] = "select"
        if field.get("options"):
            updated["options"] = list(field["options"])
    return updated


def _deterministic_step_for_live_field(
    field: dict,
    *,
    profile,
    application_profile,
    out_dir: Path,
    draft_overrides: dict[str, object] | None = None,
) -> dict | None:
    base = {
        "field_name": field["field_name"],
        "label": field["label"],
        "kind": field["kind"],
        "required": field["required"],
        "field_type": field["field_type"],
    }
    if field.get("name"):
        base["name"] = field["name"]
    if field.get("path"):
        base["path"] = field["path"]
    if field.get("options"):
        base["options"] = list(field["options"])

    field_name = field["field_name"]
    kind = field["kind"]
    override_value = None if draft_overrides is None else draft_overrides.get(field_name)
    if kind in _TEXT_LIKE_KINDS and isinstance(override_value, str) and override_value.strip():
        return {
            **base,
            "kind": kind,
            "value": override_value.strip(),
            "source": "draft_overrides.json",
        }
    if kind == "select" and isinstance(override_value, str) and override_value.strip():
        return {
            **base,
            "kind": "select",
            "value": override_value.strip(),
            "source": "draft_overrides.json",
        }

    if field_name == "first_name" and getattr(profile, "first_name", ""):
        return {**base, "kind": "text", "value": profile.first_name, "source": "master_resume.md"}
    if field_name == "last_name" and getattr(profile, "last_name", ""):
        return {**base, "kind": "text", "value": profile.last_name, "source": "master_resume.md"}
    if field_name == "email" and getattr(profile, "email", ""):
        return {**base, "kind": "text", "value": profile.email, "source": "master_resume.md"}
    if field_name == "phone" and getattr(profile, "phone", ""):
        return {**base, "kind": kind if kind in _TEXT_LIKE_KINDS else "text", "value": profile.phone, "source": "master_resume.md"}
    if field_name == "resume" and kind == "file":
        return {
            **base,
            "kind": "file",
            "file_path": str(find_resume_file(out_dir)),
            "source": "existing_resume_asset",
        }
    if field_name == "cover_letter":
        if kind == "file":
            cover_letter_path = find_cover_letter_file(out_dir)
            if cover_letter_path and cover_letter_path.exists():
                return {
                    **base,
                    "kind": "file",
                    "file_path": str(cover_letter_path),
                    "source": "existing_cover_letter_asset",
                }
            return None
        if kind in _TEXT_LIKE_KINDS:
            cover_letter_body = find_cover_letter_text(out_dir)
            if cover_letter_body:
                return {
                    **base,
                    "kind": kind,
                    "value": cover_letter_body,
                    "source": "cover_letter_text.txt",
                }
            return None
    if field_name == "website":
        website = getattr(application_profile, "website", "") or getattr(profile, "website", "")
        if website:
            source = "application_profile.md" if getattr(application_profile, "website", "") else "master_resume.md"
            return {**base, "kind": kind if kind in _TEXT_LIKE_KINDS else "text", "value": website, "source": source}
        return None
    if field_name == "linkedin":
        linkedin = getattr(application_profile, "linkedin", "") or getattr(profile, "linkedin", "")
        if linkedin:
            source = "application_profile.md" if getattr(application_profile, "linkedin", "") else "master_resume.md"
            return {**base, "kind": kind if kind in _TEXT_LIKE_KINDS else "text", "value": linkedin, "source": source}
        return None
    if field_name in {"city", "location", "current_location"}:
        location = getattr(application_profile, "location", "")
        if location:
            return {**base, "kind": kind if kind in _TEXT_LIKE_KINDS else "text", "value": location, "source": "application_profile.md"}
        return None
    if kind == "select" and field.get("options"):
        selected = _infer_deterministic(field["label"], field["options"])
        if selected:
            source = "application_profile.md" if resolve_shared_question_policy(field["label"], application_profile) else "deterministic_override"
            return {**base, "kind": "select", "value": selected, "source": source}
    return None


def _build_unknown_question(field: dict) -> dict:
    return {
        "field_name": field["field_name"],
        "label": field["label"],
        "kind": field["kind"],
        "required": bool(field["required"]),
        "status": "unknown_required" if field["required"] else "unknown_optional",
    }


def _load_draft_overrides(out_dir: Path) -> dict[str, object]:
    path = out_dir / "draft_overrides.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items()}


def _reconcile_live_steps(
    *,
    steps: list[dict],
    discovered_fields: list[dict],
    out_dir: Path,
    meta: dict,
    profile,
    application_profile,
    provider: str,
    draft_overrides: dict[str, object] | None = None,
) -> tuple[list[dict], list[dict]]:
    normalized_fields = _normalize_live_fields(discovered_fields)
    reconciled_steps = [dict(step) for step in steps]
    step_indexes: dict[str, list[int]] = {}
    for index, step in enumerate(reconciled_steps):
        field_name = str(step.get("field_name") or "").strip()
        if not field_name:
            continue
        step_indexes.setdefault(field_name, []).append(index)

    generation_candidates: list[dict] = []
    unknown_questions: list[dict] = []

    for field in normalized_fields:
        indices = step_indexes.get(field["field_name"], [])
        compatible_index = next(
            (
                index
                for index in indices
                if _step_kind_matches_live_kind(str(reconciled_steps[index].get("kind") or ""), field["kind"])
            ),
            None,
        )
        if compatible_index is not None:
            reconciled_steps[compatible_index] = _apply_live_field_to_step(reconciled_steps[compatible_index], field)
            continue

        if field["field_name"] == "cover_letter" and indices and field["kind"] in _TEXT_LIKE_KINDS:
            replacement = _deterministic_step_for_live_field(
                field,
                profile=profile,
                application_profile=application_profile,
                out_dir=out_dir,
                draft_overrides=draft_overrides,
            )
            if replacement is not None:
                reconciled_steps[indices[0]] = replacement
                continue

        deterministic_step = _deterministic_step_for_live_field(
            field,
            profile=profile,
            application_profile=application_profile,
            out_dir=out_dir,
            draft_overrides=draft_overrides,
        )
        if deterministic_step is not None:
            step_indexes.setdefault(field["field_name"], []).append(len(reconciled_steps))
            reconciled_steps.append(deterministic_step)
            continue

        if field["kind"] in _TEXT_LIKE_KINDS:
            generation_candidates.append(field)
            continue

        unknown_questions.append(_build_unknown_question(field))

    if generation_candidates:
        answers = generate_application_answers(
            out_dir=out_dir,
            meta=meta,
            question_specs=[
                {
                    "field_name": field["field_name"],
                    "label": field["label"],
                    "field_type": field["field_type"],
                    "required": field["required"],
                }
                for field in generation_candidates
            ],
            provider=provider,
        )
        for field in generation_candidates:
            answer = answers.get(field["field_name"])
            if isinstance(answer, str):
                answer = answer.strip()
            if answer:
                step = {
                    "field_name": field["field_name"],
                    "label": field["label"],
                    "kind": field["kind"],
                    "required": field["required"],
                    "field_type": field["field_type"],
                    "value": answer,
                    "source": "generated_application_answer",
                }
                if field.get("name"):
                    step["name"] = field["name"]
                if field.get("path"):
                    step["path"] = field["path"]
                reconciled_steps.append(step)
                continue
            unknown_questions.append(_build_unknown_question(field))

    return reconciled_steps, unknown_questions


def _discover_live_fields(page) -> list[dict]:
    js = """() => {
        const normalize = (value) => (value || '').replace(/\\s+/g, ' ').replace(/\\*/g, ' ').trim();
        const isVisible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            if (style.visibility === 'hidden' || style.display === 'none') return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        };
        const controls = Array.from(
            document.querySelectorAll(
                'input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]):not([type="file"]), textarea, select'
            )
        );
        const seen = new Set();
        const results = [];
        for (const el of controls) {
            if (!isVisible(el)) continue;
            const type = (el.getAttribute('type') || '').toLowerCase();
            let kind = 'text';
            if (el.tagName === 'TEXTAREA') kind = 'textarea';
            else if (el.tagName === 'SELECT') kind = 'select';
            let label = normalize(el.getAttribute('aria-label'));
            if (!label && el.id) {
                const explicitLabel = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                if (explicitLabel) label = normalize(explicitLabel.innerText);
            }
            if (!label) {
                const wrappingLabel = el.closest('label');
                if (wrappingLabel) {
                    const clone = wrappingLabel.cloneNode(true);
                    clone.querySelectorAll('input, textarea, select').forEach((node) => node.remove());
                    label = normalize(clone.innerText);
                }
            }
            if (!label) label = normalize(el.getAttribute('placeholder') || el.getAttribute('name') || el.id);
            if (!label) continue;
            const required = Boolean(
                el.required ||
                el.getAttribute('aria-required') === 'true' ||
                /(^|\\s)\\*/.test((el.getAttribute('aria-label') || '')) ||
                (() => {
                    const explicit = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
                    return explicit ? explicit.innerText.includes('*') : false;
                })()
            );
            const key = `${kind}|${label}|${el.getAttribute('name') || ''}|${el.id || ''}`;
            if (seen.has(key)) continue;
            seen.add(key);
            results.push({
                label,
                kind,
                required,
                name: el.getAttribute('name') || '',
                id: el.id || '',
                options: kind === 'select'
                    ? Array.from(el.querySelectorAll('option')).map((option) => normalize(option.textContent)).filter(Boolean)
                    : [],
            });
        }
        return results;
    }"""
    try:
        discovered_fields = page.evaluate(js)
    except Exception:
        return []
    return discovered_fields if isinstance(discovered_fields, list) else []


def _write_unknown_questions(out_dir: Path, unknown_questions: list[dict]) -> None:
    path = role_submit_path(out_dir, _BOARD_CONSTANTS["unknown_questions_json"])
    if not unknown_questions:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"questions": unknown_questions}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _discover_and_inject_live_questions(page, payload_path: Path) -> None:
    import json as _json

    payload = _json.loads(payload_path.read_text(encoding="utf-8"))
    out_dir = Path(payload["out_dir"])
    discovered_fields = _discover_live_fields(page)
    if not discovered_fields:
        return

    meta = load_meta(out_dir)
    profile = parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    draft_overrides = _load_draft_overrides(out_dir)
    steps, unknown_questions = _reconcile_live_steps(
        steps=list(payload.get("steps") or []),
        discovered_fields=discovered_fields,
        out_dir=out_dir,
        meta=meta,
        profile=profile,
        application_profile=application_profile,
        provider=str(payload.get("answer_provider") or "openai"),
        draft_overrides=draft_overrides,
    )
    payload["steps"] = steps
    payload["unknown_questions"] = unknown_questions
    payload_path.write_text(
        _json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _write_unknown_questions(out_dir, unknown_questions)

    required_unknown = [question for question in unknown_questions if question.get("required")]
    if required_unknown:
        labels = ", ".join(str(question["label"]) for question in required_unknown)
        raise ValueError(
            f"Autofill payload is missing answers for required Workable fields: {labels}. "
            f"See {role_submit_path(out_dir, _BOARD_CONSTANTS['unknown_questions_json'])}"
        )


# --- Payload builder ---


def _resolve_application_url(job_url: str) -> str:
    """Resolve a Workable listing URL to the actual apply form URL.

    apply.workable.com/{company}/j/{id}/  →  …/j/{id}/apply/
    """
    from urllib.parse import urlparse

    parsed = urlparse(job_url)
    host = (parsed.hostname or "").casefold()

    if host == "apply.workable.com":
        path = parsed.path.rstrip("/")
        if not path.endswith("/apply"):
            return append_url_path_suffix(job_url, "/apply/")
        return job_url

    # jobs.workable.com listing -- resolved at runtime via _navigate_to_apply_form
    return job_url


def _build_payload(out_dir: Path, provider: str) -> dict:
    """Build the autofill payload for a Workable application."""
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)
    job_url = str(meta.get("jd_source_resolved") or meta.get("jd_source") or "")
    application_url = _resolve_application_url(job_url)
    profile = parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))

    resume_path = find_resume_file(out_dir)
    cover_letter_path = find_cover_letter_file(out_dir)

    # Build deterministic steps for standard fields
    steps: list[dict] = []

    # Personal info fields
    steps.extend(
        [
            {
                "field_name": "first_name",
                "label": "First name",
                "kind": "text",
                "required": True,
                "value": profile.first_name,
                "source": "master_resume.md",
            },
            {
                "field_name": "last_name",
                "label": "Last name",
                "kind": "text",
                "required": True,
                "value": profile.last_name,
                "source": "master_resume.md",
            },
            {
                "field_name": "email",
                "label": "Email",
                "kind": "text",
                "required": True,
                "value": profile.email,
                "source": "master_resume.md",
            },
            {
                "field_name": "phone",
                "label": "Phone",
                "kind": "text",
                "required": False,
                "value": profile.phone or "",
                "source": "master_resume.md",
            },
        ]
    )

    # Resume upload
    steps.append(
        {
            "field_name": "resume",
            "label": "Resume",
            "kind": "file",
            "required": True,
            "file_path": str(resume_path),
            "source": "existing_resume_asset",
        }
    )

    # Cover letter upload (if file exists)
    if cover_letter_path and cover_letter_path.exists():
        steps.append(
            {
                "field_name": "cover_letter",
                "label": "Cover letter",
                "kind": "file",
                "required": False,
                "file_path": str(cover_letter_path),
                "source": "existing_cover_letter_asset",
            }
        )

    # Location fields — try multiple label variants
    steps.extend(
        [
            {
                "field_name": "city",
                "label": "City",
                "kind": "text",
                "required": False,
                "value": application_profile.location or "",
                "source": "application_profile.md",
            },
            {
                "field_name": "location",
                "label": "Location",
                "kind": "text",
                "required": False,
                "value": application_profile.location or "",
                "source": "application_profile.md",
            },
            {
                "field_name": "current_location",
                "label": "Current location",
                "kind": "text",
                "required": False,
                "value": application_profile.location or "",
                "source": "application_profile.md",
            },
        ]
    )

    # LinkedIn / portfolio
    steps.extend(
        [
            {
                "field_name": "linkedin",
                "label": "LinkedIn",
                "kind": "text",
                "required": False,
                "value": profile.linkedin or "",
                "source": "master_resume.md",
            },
            {
                "field_name": "website",
                "label": "Website",
                "kind": "text",
                "required": False,
                "value": profile.website or "",
                "source": "master_resume.md",
            },
        ]
    )

    payload = {
        "job_url": job_url,
        "application_url": application_url,
        "out_dir": str(out_dir),
        "job_title": str(meta.get("jd_title") or ""),
        "company": str(meta.get("company_proper") or meta.get("company") or ""),
        "candidate_email": profile.email,
        "verification_code_email": application_profile.verification_code_email or profile.email,
        "answer_provider": provider,
        "mode": "review-before-submit",
        "notes": [
            "Workable single-page form. Uses Cloudflare Turnstile CAPTCHA.",
            "For jobs.workable.com URLs, the browser navigates to the apply form.",
        ],
        "artifacts": {
            "payload_path": str(role_submit_path(out_dir, AUTOFILL_PAYLOAD_JSON)),
            "report_markdown": str(role_submit_path(out_dir, _BOARD_CONSTANTS["report_md"])),
            "report_json": str(role_submit_path(out_dir, _BOARD_CONSTANTS["report_json"])),
            "pre_submit_screenshot": str(role_submit_path(out_dir, _BOARD_CONSTANTS["pre_submit_screenshot"])),
            "page_screenshots_dir": str(role_submit_path(out_dir, _BOARD_CONSTANTS["page_screenshots_dir"])),
            "unknown_questions_json": str(role_submit_path(out_dir, _BOARD_CONSTANTS["unknown_questions_json"])),
            "submit_debug_html": str(role_submit_path(out_dir, _BOARD_CONSTANTS["submit_debug_html"])),
            "submit_debug_screenshot": str(role_submit_path(out_dir, _BOARD_CONSTANTS["submit_debug_screenshot"])),
            "application_page_html": str(role_submit_path(out_dir, APPLICATION_PAGE_HTML)),
        },
        "steps": steps,
        "unknown_questions": [],
    }
    return payload


# --- Browser pipeline callbacks ---


def _dismiss_cookie_banner(page) -> None:
    """Dismiss cookie consent banner if present."""
    try:
        for name in ("Accept", "Accept all", "Accept All", "Accept cookies", "Got it"):
            btn = page.get_by_role("button", name=name)
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                page.wait_for_timeout(500)
                return
    except Exception:
        pass


def _navigate_to_apply_form(page, payload: dict) -> None:
    """Navigate from a Workable listing page to the actual apply form.

    Workable URL patterns:
      - apply.workable.com/{company}/j/{id}/       → job listing (has "Apply" button)
      - apply.workable.com/{company}/j/{id}/apply/  → actual application form
      - jobs.workable.com/view/{id}/                → external listing
    """
    from urllib.parse import urlparse

    current_url = page.url
    parsed = urlparse(current_url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.rstrip("/")

    # apply.workable.com listing → need to reach the /apply/ sub-path
    if host == "apply.workable.com":
        if path.endswith("/apply"):
            return  # Already on the application form

        # Try clicking the "Apply for this job" link/button first
        for role in ("link", "button"):
            try:
                el = page.get_by_role(role, name=re.compile(r"apply", re.I)).first
                if el.count() and el.is_visible():
                    el.click()
                    page.wait_for_timeout(3000)
                    payload["application_url"] = page.url
                    return
            except Exception:
                pass

        # Preserve query params when deriving the canonical /apply/ path.
        apply_url = append_url_path_suffix(current_url, "/apply/")
        page.goto(apply_url, wait_until="domcontentloaded", timeout=30000)
        payload["application_url"] = page.url
        return

    if "jobs.workable.com" not in host:
        return

    # jobs.workable.com listing → look for link to apply.workable.com
    try:
        apply_link = page.get_by_role("link", name=re.compile(r"apply", re.I)).first
        if apply_link.count() and apply_link.is_visible():
            href = apply_link.get_attribute("href") or ""
            if "apply.workable.com" in href:
                page.goto(href, wait_until="domcontentloaded", timeout=30000)
                payload["application_url"] = page.url
                return
            apply_link.click()
            page.wait_for_timeout(3000)
            payload["application_url"] = page.url
            return
    except Exception:
        pass

    # Try button variant
    try:
        apply_btn = page.get_by_role("button", name=re.compile(r"apply", re.I)).first
        if apply_btn.count() and apply_btn.is_visible():
            apply_btn.click()
            page.wait_for_timeout(3000)
            payload["application_url"] = page.url
            return
    except Exception:
        pass


def _fill_text_field(page, label: str, value: str) -> bool:
    """Fill a text input or textarea by label. Returns True if successful."""
    if not value:
        return False
    if _field_label_matches(label, "phone") and _fill_workable_phone_field(page, value):
        return True
    try:
        # Try textbox role first (covers input[type=text], input[type=email], etc.)
        locator = page.get_by_role("textbox", name=re.compile(re.escape(label), re.I)).first
        if locator.count():
            locator.scroll_into_view_if_needed()
            locator.fill(str(value))
            return True
    except Exception:
        pass

    # Try by label text
    try:
        locator = page.get_by_label(re.compile(re.escape(label), re.I)).first
        if locator.count():
            locator.scroll_into_view_if_needed()
            locator.fill(str(value))
            return True
    except Exception:
        pass

    return False


def _fill_workable_phone_field(page, value: str) -> bool:
    """Fill Workable's composite phone widget without clobbering the country-code control."""
    normalized_value = str(value or "").strip()
    if not normalized_value:
        return False

    fill_values = [normalized_value]
    digits_only = re.sub(r"\D+", "", normalized_value)
    if digits_only and digits_only != normalized_value:
        fill_values.append(digits_only)
        if len(digits_only) == 11 and digits_only.startswith("1"):
            fill_values.append(digits_only[1:])

    candidates = page.locator(
        "input[type='tel'], "
        "input[inputmode='tel'], "
        "input[autocomplete='tel'], "
        "input[autocomplete='tel-national'], "
        "input[name*='phone' i], "
        "input[id*='phone' i], "
        "input[aria-label*='phone' i]"
    )
    scored_candidates: list[tuple[int, object]] = []
    for index in range(candidates.count()):
        locator = candidates.nth(index)
        try:
            if not locator.is_visible() or locator.is_disabled():
                continue
        except Exception:
            continue

        score = 0
        input_type = str(locator.get_attribute("type") or "").strip().casefold()
        if input_type == "tel":
            score += 5

        metadata = " ".join(
            part
            for part in (
                locator.get_attribute("name") or "",
                locator.get_attribute("id") or "",
                locator.get_attribute("autocomplete") or "",
                locator.get_attribute("aria-label") or "",
            )
            if part
        ).casefold()
        if "phone" in metadata or "tel" in metadata:
            score += 3

        try:
            current_value = str(locator.input_value() or "").strip()
        except Exception:
            current_value = ""
        if current_value.startswith("+") and len(current_value) <= 4:
            score -= 6

        try:
            bounds = locator.bounding_box()
        except Exception:
            bounds = None
        if bounds:
            width = float(bounds.get("width") or 0)
            if width >= 160:
                score += 3
            elif 0 < width <= 120:
                score -= 3

        scored_candidates.append((score, locator))

    for _score, locator in sorted(scored_candidates, key=lambda item: item[0], reverse=True):
        for candidate_value in fill_values:
            try:
                locator.scroll_into_view_if_needed()
                locator.fill(candidate_value)
                current_value = str(locator.input_value() or "").strip()
            except Exception:
                continue

            if current_value == candidate_value:
                return True

            normalized_current = re.sub(r"\D+", "", current_value)
            normalized_candidate = re.sub(r"\D+", "", candidate_value)
            if normalized_current and normalized_candidate and (
                normalized_current.endswith(normalized_candidate)
                or normalized_candidate.endswith(normalized_current)
            ):
                return True

    return False


def _fill_file_field(page, label: str, file_path: str) -> bool:
    """Upload a file to an input[type=file]. Returns True if successful."""
    if not file_path or not Path(file_path).exists():
        return False

    try:
        # Workable forms typically have file inputs with associated labels
        label_lower = label.casefold()

        # Try by label association
        file_input = page.get_by_label(re.compile(re.escape(label), re.I)).first
        if file_input.count():
            file_input.set_input_files(file_path)
            return True
    except Exception:
        pass

    try:
        # Fall back to finding file inputs and matching by context
        file_inputs = page.locator("input[type='file']")
        count = file_inputs.count()
        if count == 0:
            return False

        # For resume: use the first file input
        # For cover letter: use the second file input (if available)
        if "resume" in label_lower or "cv" in label_lower:
            file_inputs.first.set_input_files(file_path)
            return True
        if "cover" in label_lower and count > 1:
            file_inputs.nth(1).set_input_files(file_path)
            return True
        if count == 1 and "resume" in label_lower:
            file_inputs.first.set_input_files(file_path)
            return True
    except Exception:
        pass

    return False


def _fill_select_field(page, label: str, value: str) -> bool:
    """Fill a select/dropdown field. Returns True if successful."""
    if not value:
        return False
    from playwright.sync_api import Error as PlaywrightError

    try:
        # Try combobox role
        combobox = page.get_by_role("combobox", name=re.compile(re.escape(label), re.I)).first
        if combobox.count():
            combobox.scroll_into_view_if_needed()
            combobox.click()
            page.wait_for_timeout(400)

            # Look for matching option
            options = page.get_by_role("option")
            for i in range(options.count()):
                opt = options.nth(i)
                try:
                    text = opt.inner_text().strip()
                except PlaywrightError:
                    continue
                if value.casefold() in text.casefold() or text.casefold() in value.casefold():
                    opt.click()
                    page.wait_for_timeout(300)
                    return True

            # Close dropdown if no match
            combobox.press("Escape")
            page.wait_for_timeout(200)
            return False
    except PlaywrightError:
        pass

    # Try native <select> element
    try:
        sel_by_label = page.get_by_label(re.compile(re.escape(label), re.I)).first
        if sel_by_label.count():
            sel_by_label.select_option(label=value)
            return True
    except Exception:
        pass

    return False


def _fill_checkbox_field(page, label: str) -> bool:
    """Check a checkbox by label. Returns True if successful."""
    try:
        locator = page.get_by_role("checkbox", name=re.compile(re.escape(label), re.I)).first
        if locator.count() and not locator.is_checked():
            locator.scroll_into_view_if_needed()
            locator.click()
            page.wait_for_timeout(200)
        return True
    except Exception:
        pass
    return False


def _fill_radio_field(page, label: str, value: str) -> bool:
    """Select a radio button by label and value. Returns True if successful."""
    try:
        radio = page.get_by_role("radio", name=re.compile(re.escape(value), re.I)).first
        if radio.count():
            radio.scroll_into_view_if_needed()
            radio.click()
            page.wait_for_timeout(200)
            return True
    except Exception:
        pass
    return False


def _fill_step(page, step: dict) -> None:
    """Fill a single form field on the Workable application page."""
    kind = step.get("kind", "")
    label = step.get("label", "")
    value = step.get("value", "")

    if kind == "file":
        file_path = step.get("file_path", "")
        if _fill_file_field(page, label, file_path):
            step["filled"] = True
        return

    if kind == "text" or kind == "textarea":
        if _fill_text_field(page, label, value):
            step["filled"] = True
        return

    if kind == "select" or kind == "combobox":
        if _fill_select_field(page, label, value):
            step["filled"] = True
        return

    if kind == "checkbox":
        if _fill_checkbox_field(page, label):
            step["filled"] = True
        return

    if kind == "radio":
        if _fill_radio_field(page, label, value):
            step["filled"] = True
        return


def _page_snapshot_workable(page) -> dict:
    """Capture a snapshot of the Workable page state.

    Detects Cloudflare Turnstile CAPTCHA instead of reCAPTCHA/hCaptcha.
    """
    js = """() => {
        const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
        const pageText = normalize(document.body ? document.body.innerText : '');

        // Cloudflare Turnstile detection
        const turnstileIframes = Array.from(document.querySelectorAll('iframe')).filter((frame) => {
            const src = (frame.getAttribute('src') || '').toLowerCase();
            return src.includes('turnstile') || src.includes('challenges.cloudflare.com');
        });
        const turnstileWidget = !!document.querySelector('.cf-turnstile, [data-sitekey]');
        const turnstileVisible = turnstileIframes.length > 0 || turnstileWidget;
        const turnstileChallengeActive = turnstileIframes.some((frame) => {
            const rect = frame.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0 && rect.height >= 60;
        });

        const formSelector = 'form';
        const invalidFields = Array.from(document.querySelectorAll('[aria-invalid="true"]'))
            .map((node) => {
                const entry = node.closest('form, [class*="field"], [class*="Field"]');
                const label = entry ? entry.querySelector('label') : null;
                return normalize(label ? label.innerText : node.getAttribute('name') || node.getAttribute('id') || '');
            })
            .filter(Boolean);
        const explicitErrors = Array.from(document.querySelectorAll('[role="alert"], [class*="error"], [class*="Error"]'))
            .map((node) => normalize(node.innerText))
            .filter(Boolean);

        return {
            url: window.location.href,
            page_text: pageText,
            form_visible: !!document.querySelector(formSelector),
            turnstile_visible: turnstileVisible,
            turnstile_challenge_active: turnstileChallengeActive,
            invalid_fields: invalidFields,
            errors: explicitErrors,
        };
    }"""

    return page.evaluate(js)


def _classify_submit_state(snapshot: dict) -> dict[str, object]:
    """Classify page state after submit click."""
    page_text = str(snapshot.get("page_text") or "")

    # Check for confirmation
    if any(pattern.search(page_text) for pattern in SUBMIT_CONFIRM_PATTERNS):
        return {"status": "confirmed", "reason": "text"}

    # Check for Turnstile CAPTCHA challenge
    if snapshot.get("turnstile_challenge_active"):
        return {"status": "captcha_required", "reason": "turnstile_challenge"}

    # Check for validation errors
    errors = list(snapshot.get("errors") or [])
    page_level_errors = [
        pattern.search(page_text).group(0) for pattern in VALIDATION_ERROR_PATTERNS if pattern.search(page_text)
    ]
    combined_errors = list(dict.fromkeys(errors + page_level_errors))
    if combined_errors:
        return {"status": "validation_error", "errors": combined_errors}

    return {"status": "pending"}


def _wait_for_workable_form(page) -> None:
    """Wait for the Workable application form to render."""
    page.wait_for_selector(
        'button:has-text("Submit"), button:has-text("Apply"), form, [data-ui="application"]',
        timeout=25000,
    )
    page.wait_for_timeout(2000)  # Let React hydrate
    _dismiss_cookie_banner(page)


# --- Entry point ---


def _run_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    """Wrap run_browser_pipeline with Workable-specific callbacks."""
    import json

    from autofill_pipeline import run_browser_pipeline

    # Read the payload to check if we need to navigate from listing to apply form
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    navigate_url = payload.get("application_url") or payload.get("job_url", "")

    def _post_navigate(page):
        """After initial navigation, handle listing -> apply form redirect."""
        # Wait for the listing page to be interactive before trying to click Apply.
        page.wait_for_load_state("domcontentloaded")
        _navigate_to_apply_form(page, payload)
        if payload.get("application_url") and payload["application_url"] != navigate_url:
            payload_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        # Wait for the actual application form to render after navigation
        _wait_for_workable_form(page)
        _discover_and_inject_live_questions(page, payload_path)

    return run_browser_pipeline(
        payload_path,
        headless=headless,
        submit=submit,
        board_name=_BOARD,
        # form_ready_fn is intentionally omitted here — the listing page
        # (job_url) does not contain the application form.  _post_navigate
        # handles navigating from the listing to /apply/ and then waits for
        # the form via _wait_for_workable_form.
        fill_step_fn=_fill_step,
        page_snapshot_fn=lambda page: _page_snapshot_workable(page),
        classify_state_fn=_classify_submit_state,
        click_submit_fn=lambda page: click_submit_button(page, button_names=SUBMIT_BUTTON_NAMES),
        capture_fn=lambda page, path: capture_full_page(page, path),
        post_navigate_hook=_post_navigate,
    )


def main() -> int:
    from autofill_pipeline import autofill_main

    return autofill_main(
        board_name=_BOARD,
        build_payload_fn=_build_payload,
        has_browser=True,
        run_browser_fn=_run_browser,
    )


if __name__ == "__main__":
    raise SystemExit(main())
