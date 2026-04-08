#!/usr/bin/env python3
"""JazzHR / ApplyToJob application autofill."""

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
    MASTER_RESUME_PATH,
    build_simple_payload,
    find_cover_letter_file,
    find_cover_letter_text,
    find_resume_file,
    load_meta,
    parse_application_profile,
    parse_master_resume,
    resolve_shared_question_policy,
)
from autofill_common import (
    board_file_constants,
    classify_submit_state,
    fill_basic_step,
    label_matches,
    select_option,
    select_shared_policy_option,
)
from autofill_pipeline import autofill_main, run_simple_board_pipeline
from output_layout import migrate_role_output_layout
from project_env import load_project_env
from text_normalization_helpers import slugify_label

_BOARD = "jazzhr"
_BOARD_CONSTANTS = board_file_constants(_BOARD)
_FORM_SELECTOR = "form, input[type='file'], button[type='submit']"
_VISIBLE_FORM_SELECTOR = (
    "input[type='email']:visible, input[type='text']:visible, textarea:visible, "
    "select:visible, button[type='submit']:visible"
)
SUBMIT_BUTTON_NAMES = ("Submit Application", "Submit application", "Apply", "Continue")
_CONFIRM_PATTERNS = (
    re.compile(r"\bapplication submitted\b", re.I),
    re.compile(r"\bthank(?:s| you)\s+for\s+applying\b", re.I),
)
_REVIEW_PATTERNS = (
    re.compile(r"\bsubmit application\b", re.I),
    re.compile(r"applytojob", re.I),
)

load_project_env()


def _infer_deterministic(label: str, options: list[str]) -> str | None:
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    policy = resolve_shared_question_policy(label, application_profile)
    if policy is not None and policy.text_value is not None:
        return select_shared_policy_option(options, policy, application_profile=application_profile) or policy.text_value
    return None


def _base_field_name(label: str, raw_name: str) -> str:
    text = f"{label} {raw_name}".strip()
    if label_matches(text, "first name", "firstname"):
        return "first_name"
    if label_matches(text, "last name", "lastname"):
        return "last_name"
    if label_matches(text, "email"):
        return "email"
    if label_matches(text, "phone"):
        return "phone"
    if label_matches(text, "linkedin"):
        return "linkedin"
    if label_matches(text, "website", "portfolio"):
        return "website"
    if label_matches(text, "location"):
        return "location"
    if label_matches(text, "work authorization", "authorized to work", "authorised to work", "legally authorized"):
        return "work_authorization"
    if label_matches(text, "require sponsorship", "need sponsorship", "employer sponsorship"):
        return "sponsorship"
    if label_matches(text, "resume", "cv"):
        return "resume"
    if label_matches(text, "cover letter"):
        return "cover_letter"
    return slugify_label(label or raw_name or "field") or "field"


def _discover_live_fields(page) -> list[dict]:
    raw_fields = page.evaluate(
        """() => {
            const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const isVisible = (el) => {
              if (!el) return false;
              const style = window.getComputedStyle(el);
              if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') return false;
              const rect = el.getBoundingClientRect();
              return rect.width > 0 && rect.height > 0;
            };
            const labelFor = (el) => {
              const wrapper = el.closest('.resumator-field-wrapper');
              const wrapperLabel = normalize(wrapper?.querySelector('.resumator-label')?.innerText || '');
              if (wrapperLabel) return wrapperLabel;
              if (el.labels && el.labels.length) {
                const joined = Array.from(el.labels).map((label) => normalize(label.innerText)).filter(Boolean).join(' ');
                if (joined) return joined;
              }
              const aria = normalize(el.getAttribute('aria-label') || '');
              if (aria) return aria;
              return normalize(el.getAttribute('name') || el.id || '');
            };
            const controls = Array.from(
              document.querySelectorAll(
                'input:not([type="hidden"]):not([type="button"]):not([type="submit"]), textarea, select'
              )
            );
            const seen = new Set();
            const rows = [];
            for (const el of controls) {
              if (!isVisible(el) || el.disabled) continue;
              const label = labelFor(el);
              if (!label) continue;
              const kind = el.tagName === 'SELECT'
                ? 'select'
                : (el.tagName === 'TEXTAREA' ? 'textarea' : (el.type === 'file' ? 'file' : 'text'));
              const options = kind === 'select'
                ? Array.from(el.querySelectorAll('option')).map((option) => normalize(option.textContent)).filter(Boolean)
                : [];
              const key = `${kind}|${label}|${el.getAttribute('name') || ''}|${el.id || ''}`;
              if (seen.has(key)) continue;
              seen.add(key);
              rows.push({
                label,
                kind,
                required: label.includes('*'),
                name: el.getAttribute('name') || '',
                id: el.id || '',
                options,
              });
            }
            return rows;
        }"""
    )
    return raw_fields if isinstance(raw_fields, list) else []


def _update_step_for_live_field(step: dict, field: dict) -> dict:
    updated = dict(step)
    updated["label"] = field["label"]
    updated["required"] = bool(field.get("required"))
    live_name = str(field.get("name") or field.get("id") or "").strip()
    if live_name:
        updated["field_name"] = live_name
    kind = str(field.get("kind") or "text")
    if kind in {"text", "textarea", "file", "select"}:
        updated["kind"] = kind
    if kind == "select":
        updated["options"] = list(field.get("options") or [])
    return updated


def _deterministic_step_for_live_field(field: dict, *, profile, application_profile, out_dir: Path) -> dict | None:
    live_name = str(field.get("name") or field.get("id") or "").strip()
    base_name = _base_field_name(str(field.get("label") or ""), live_name)
    base = {
        "field_name": live_name or base_name,
        "label": str(field.get("label") or "").strip(),
        "kind": str(field.get("kind") or "text"),
        "required": bool(field.get("required")),
        "source": "live_application_form",
    }
    if field.get("options"):
        base["options"] = list(field["options"])

    if base_name == "first_name" and getattr(profile, "first_name", ""):
        return {**base, "kind": "text", "value": profile.first_name, "source": "master_resume.md"}
    if base_name == "last_name" and getattr(profile, "last_name", ""):
        return {**base, "kind": "text", "value": profile.last_name, "source": "master_resume.md"}
    if base_name == "email" and getattr(profile, "email", ""):
        return {**base, "kind": "text", "value": profile.email, "source": "master_resume.md"}
    if base_name == "phone" and getattr(profile, "phone", ""):
        return {**base, "kind": "text", "value": profile.phone, "source": "master_resume.md"}
    if base_name == "linkedin":
        linkedin = getattr(application_profile, "linkedin", "") or getattr(profile, "linkedin", "")
        if linkedin:
            source = "application_profile.md" if getattr(application_profile, "linkedin", "") else "master_resume.md"
            return {**base, "kind": "text", "value": linkedin, "source": source}
    if base_name == "website":
        website = getattr(application_profile, "website", "") or getattr(profile, "website", "")
        if website:
            source = "application_profile.md" if getattr(application_profile, "website", "") else "master_resume.md"
            return {**base, "kind": "text", "value": website, "source": source}
    if base_name == "location" and getattr(application_profile, "location", ""):
        return {**base, "kind": "text", "value": application_profile.location, "source": "application_profile.md"}
    if base_name == "resume" and base["kind"] == "file":
        return {
            **base,
            "kind": "file",
            "file_path": str(find_resume_file(out_dir)),
            "source": "existing_resume_asset",
        }
    if base_name == "cover_letter":
        if base["kind"] == "file":
            cover_letter_path = find_cover_letter_file(out_dir)
            if cover_letter_path and cover_letter_path.exists():
                return {
                    **base,
                    "kind": "file",
                    "file_path": str(cover_letter_path),
                    "source": "existing_cover_letter_asset",
                }
        if base["kind"] in {"text", "textarea"}:
            cover_letter_body = find_cover_letter_text(out_dir)
            if cover_letter_body:
                return {**base, "kind": base["kind"], "value": cover_letter_body, "source": "cover_letter_text.txt"}
    if base["kind"] == "select" and field.get("options"):
        selected = _infer_deterministic(base["label"], list(field["options"]))
        if selected:
            choice = select_option(list(field["options"]), selected) or selected
            return {**base, "kind": "select", "value": choice, "source": "application_profile.md"}
    return None


def _write_unknown_questions(out_dir: Path, unknown_questions: list[dict]) -> None:
    path = out_dir / "submit" / _BOARD_CONSTANTS["unknown_questions_json"]
    if not unknown_questions:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"questions": unknown_questions}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _sync_live_fields(page, payload_path: Path) -> None:
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    out_dir = Path(payload["out_dir"])
    live_fields = _discover_live_fields(page)
    if not live_fields:
        return

    profile = parse_master_resume(MASTER_RESUME_PATH.read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    steps = [dict(step) for step in list(payload.get("steps") or [])]
    step_indexes = {
        _base_field_name(str(step.get("label") or ""), str(step.get("field_name") or "")): index
        for index, step in enumerate(steps)
        if str(step.get("field_name") or "").strip() or str(step.get("label") or "").strip()
    }

    unknown_questions: list[dict] = []
    for field in live_fields:
        base_name = _base_field_name(str(field.get("label") or ""), str(field.get("name") or field.get("id") or ""))
        index = step_indexes.get(base_name)
        if index is not None:
            steps[index] = _update_step_for_live_field(steps[index], field)
            continue

        deterministic = _deterministic_step_for_live_field(
            field,
            profile=profile,
            application_profile=application_profile,
            out_dir=out_dir,
        )
        if deterministic is not None:
            step_indexes[base_name] = len(steps)
            steps.append(deterministic)
            continue

        if field.get("required"):
            unknown_questions.append(
                {
                    "field_name": str(field.get("name") or field.get("id") or base_name).strip(),
                    "label": str(field.get("label") or base_name).strip(),
                    "kind": str(field.get("kind") or "text"),
                    "required": True,
                    "status": "planned",
                    "source": "live_application_form",
                    "reason": (
                        "The live JazzHR form contains a required question without a deterministic repo-backed "
                        "answer in the current payload."
                    ),
                    "note": "Discovered from the visible JazzHR application form during draft rerun.",
                }
            )

    payload["steps"] = steps
    payload["unknown_questions"] = unknown_questions
    payload_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _write_unknown_questions(out_dir, unknown_questions)


def _build_payload(out_dir: Path, provider: str) -> dict:
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)
    profile = parse_master_resume(MASTER_RESUME_PATH.read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))

    try:
        resume_path = find_resume_file(out_dir)
    except FileNotFoundError:
        resume_path = None
    try:
        cover_letter_path = find_cover_letter_file(out_dir)
    except FileNotFoundError:
        cover_letter_path = None

    return build_simple_payload(
        board_name=_BOARD,
        out_dir=out_dir,
        provider=provider,
        meta=meta,
        profile=profile,
        application_profile=application_profile,
        resume_path=resume_path,
        cover_letter_path=cover_letter_path,
        notes=["JazzHR / ApplyToJob form. Stop at review in draft mode."],
    )


def _classify_submit_state(snapshot: dict) -> dict[str, object]:
    return classify_submit_state(
        snapshot,
        confirm_patterns=_CONFIRM_PATTERNS,
        review_patterns=_REVIEW_PATTERNS,
    )


def _click_apply_if_needed(page) -> None:
    """Expand JazzHR listing pages into the visible application form."""
    from playwright.sync_api import Error as PlaywrightError

    try:
        if page.locator(_VISIBLE_FORM_SELECTOR).count():
            return
    except PlaywrightError:
        pass

    for button_text in ("Apply Now", "Apply"):
        pattern = re.compile(rf"{re.escape(button_text)}", re.I)
        for role in ("button", "link"):
            try:
                locator = page.get_by_role(role, name=pattern).first
                if not locator.count() or not locator.is_visible():
                    continue
                try:
                    locator.scroll_into_view_if_needed()
                except PlaywrightError:
                    pass
                locator.click()
                page.wait_for_timeout(2000)
                return
            except PlaywrightError:
                continue


def _wait_for_jazzhr_form(page) -> None:
    """Wait for the visible JazzHR application form, not the collapsed shell."""
    try:
        page.wait_for_selector(
            'form, a:has-text("APPLY NOW"), a:has-text("Apply"), button:has-text("Apply")',
            timeout=25000,
        )
    except Exception:
        pass

    page.wait_for_timeout(1000)
    _click_apply_if_needed(page)
    page.wait_for_selector(_VISIBLE_FORM_SELECTOR, timeout=15000)
    page.wait_for_timeout(1000)


def _run_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    return run_simple_board_pipeline(
        payload_path,
        headless=headless,
        submit=submit,
        board_name=_BOARD,
        form_selector=_FORM_SELECTOR,
        submit_button_names=SUBMIT_BUTTON_NAMES,
        classify_state_fn=_classify_submit_state,
        fill_step_fn=fill_basic_step,
        form_ready_fn=_wait_for_jazzhr_form,
        post_navigate_hook=lambda page: _sync_live_fields(page, payload_path),
        preferred_capture_selectors=("form", "main"),
    )


def main() -> int:
    return autofill_main(_BOARD, _build_payload, run_browser_fn=_run_browser)


if __name__ == "__main__":
    raise SystemExit(main())
