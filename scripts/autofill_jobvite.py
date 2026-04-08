#!/usr/bin/env python3
"""Jobvite application autofill."""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse, urlunparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_submit_common import (
    APPLICATION_PROFILE_PATH,
    PROJECT_ROOT,
    build_simple_payload,
    find_cover_letter_file,
    find_resume_file,
    load_meta,
    parse_application_profile,
    parse_master_resume,
    preferred_meta_job_url,
    primary_employer_name,
    resolve_shared_question_policy,
)
from autofill_common import (
    classify_submit_state,
    detect_live_required_unfilled_fields,
    fill_basic_step,
    select_option,
    select_shared_policy_option,
)
from autofill_pipeline import autofill_main, run_simple_board_pipeline
from job_board_urls import canonical_jobvite_job_url
from output_layout import migrate_role_output_layout
from project_env import load_project_env

_BOARD = "jobvite"
_FORM_SELECTOR = "form, input[type='file'], button[type='submit']"
SUBMIT_BUTTON_NAMES = ("Submit Application", "Submit application", "Apply", "Apply Now", "Continue")
_CONFIRM_PATTERNS = (
    re.compile(r"\bthank you for your interest\b", re.I),
    re.compile(r"\bthank(?:s| you)\s+for\s+applying\b", re.I),
    re.compile(r"\bapplication submitted\b", re.I),
)
_REVIEW_PATTERNS = (
    re.compile(r"\bsubmit application\b", re.I),
    re.compile(r"\bapply now\b", re.I),
)
_JOBVITE_LOCATION_GATE_LABEL_RE = re.compile(r"location of residence and language", re.I)
_JOBVITE_GATE_SUBMIT_RE = re.compile(r"^(submit|continue|next)$", re.I)
_JOBVITE_APPLY_GATE_RE = re.compile(r"^(apply|apply now)$", re.I)
_JOBVITE_VIEW_FULL_APPLICATION_FORM_RE = re.compile(r"view full application form", re.I)
_JOBVITE_NEXT_REVIEW_RE = re.compile(r"^next(?:\b|$)", re.I)
_JOBVITE_FINAL_ACTION_RE = re.compile(r"^(submit application|submit|apply now|apply)$", re.I)
_JOBVITE_FORM_READY_SELECTOR = "input[type='file'], input[type='text'], input[type='email'], textarea, button:has-text('Next')"

load_project_env()


def _infer_deterministic(label: str, options: list[str]) -> str | None:
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    policy = resolve_shared_question_policy(label, application_profile)
    if policy is not None and policy.text_value is not None:
        return select_shared_policy_option(options, policy, application_profile=application_profile) or policy.text_value
    return None


def _jobvite_highest_education_level(application_profile) -> str | None:
    entries = [str(entry or "").strip().casefold() for entry in list(application_profile.education_entries or []) if str(entry).strip()]
    if not entries:
        return None
    combined = " ".join(entries)
    if any(fragment in combined for fragment in ("doctoral", "ph.d", "phd")):
        return "Doctoral Degree (PhD)"
    if any(fragment in combined for fragment in ("master", "m.b.a", "mba", "m.s", "ms ")) or "graduate degree" in combined:
        return "Master's Degree"
    if any(fragment in combined for fragment in ("bachelor", "b.s", "b.a", "undergraduate")):
        return "Bachelor's Degree"
    return None


def _jobvite_primary_school_name(application_profile) -> str | None:
    entries = list(application_profile.education_entries or [])
    if not entries:
        return None
    school = str(entries[0]).split(";", 1)[0].strip()
    return school or None


def _jobvite_primary_graduation_month_year(application_profile) -> str | None:
    raw_dates = list(getattr(application_profile, "education_graduation_month_years", []) or [])
    if not raw_dates:
        return None
    primary = str(raw_dates[0] or "").strip()
    if primary:
        return primary
    for raw_value in raw_dates[1:]:
        candidate = str(raw_value or "").strip()
        if candidate:
            return candidate
    return None


def _jobvite_referral_source(application_profile) -> str | None:
    heard_about_us = str(getattr(application_profile, "how_did_you_hear", "") or "").strip()
    if not heard_about_us:
        return None
    if heard_about_us.casefold() == "corporate website":
        return "Other - please list source below"
    return heard_about_us


def _jobvite_today_iso() -> str:
    return date.today().isoformat()


def _jobvite_full_name(profile) -> str:
    full_name = str(getattr(profile, "full_name", "") or "").strip()
    if full_name:
        return full_name
    first_name = str(getattr(profile, "first_name", "") or "").strip()
    last_name = str(getattr(profile, "last_name", "") or "").strip()
    return " ".join(part for part in (first_name, last_name) if part).strip()


def _jobvite_second_page_self_id_steps(profile, application_profile) -> list[dict]:
    full_name = _jobvite_full_name(profile)
    today_iso = _jobvite_today_iso()

    def build_step(
        *,
        field_name: str,
        label: str,
        kind: str,
        value: str,
        source: str,
        profile_field: str | None = None,
    ) -> dict:
        step = {
            "field_name": field_name,
            "label": label,
            "kind": kind,
            "required": True,
            "value": value,
            "source": source,
            "page_index": 2,
        }
        if profile_field:
            step["profile_field"] = profile_field
        return step

    steps: list[dict] = []
    gender = str(getattr(application_profile, "gender", "") or "").strip()
    if gender:
        steps.append(
            build_step(
                field_name="f3",
                label="Gender",
                kind="radio",
                value=gender,
                source="application_profile.md",
                profile_field="gender",
            )
        )
    race_or_ethnicity = str(getattr(application_profile, "race_or_ethnicity", "") or "").strip()
    if race_or_ethnicity:
        steps.append(
            build_step(
                field_name="f5",
                label="Race or Ethnicity",
                kind="radio",
                value=race_or_ethnicity,
                source="application_profile.md",
                profile_field="race_or_ethnicity",
            )
        )
    if full_name:
        for field_name, label in (("f7", "Your Name"), ("f20", "Name"), ("f35", "Name")):
            steps.append(
                build_step(
                    field_name=field_name,
                    label=label,
                    kind="text",
                    value=full_name,
                    source="master_resume.md",
                )
            )
    if today_iso:
        for field_name, label in (("date8", "Today's Date"), ("date21", "Date"), ("date36", "Date")):
            steps.append(
                build_step(
                    field_name=field_name,
                    label=label,
                    kind="text",
                    value=today_iso,
                    source="deterministic_current_date",
                )
            )
    veteran_status = str(getattr(application_profile, "veteran_status", "") or "").strip()
    if veteran_status:
        steps.append(
            build_step(
                field_name="f18",
                label="Veteran Status",
                kind="radio",
                value=veteran_status,
                source="application_profile.md",
                profile_field="veteran_status",
            )
        )
    disability_status = str(getattr(application_profile, "disability_status", "") or "").strip()
    if disability_status:
        steps.append(
            build_step(
                field_name="f33",
                label="Disability Status",
                kind="radio",
                value=disability_status,
                source="application_profile.md",
                profile_field="disability_status",
            )
        )
    return steps


def _jobvite_application_url(job_url: str) -> str:
    canonical = canonical_jobvite_job_url(job_url)
    if not canonical:
        return job_url
    parsed = urlparse(canonical)
    path = parsed.path.rstrip("/")
    if not path.casefold().endswith("/apply"):
        path = f"{path}/apply"
    return urlunparse(parsed._replace(path=path, query="", fragment=""))


def _build_payload(out_dir: Path, provider: str) -> dict:
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)
    master_resume_text = (PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8")
    profile = parse_master_resume(master_resume_text)
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    job_url = preferred_meta_job_url(meta, keys=("jd_source_resolved", "jd_source"))

    try:
        resume_path = find_resume_file(out_dir)
    except FileNotFoundError:
        resume_path = None
    try:
        cover_letter_path = find_cover_letter_file(out_dir)
    except FileNotFoundError:
        cover_letter_path = None

    extra_steps = [
        {
            "field_name": "company",
            "label": "Company",
            "kind": "text",
            "required": True,
            "value": primary_employer_name(master_resume_text),
            "source": "master_resume.md",
        },
        {
            "field_name": "country",
            "label": "Country",
            "kind": "select",
            "required": True,
            "value": str(getattr(application_profile, "country", "") or ""),
            "source": "application_profile.md",
        },
        {
            "field_name": "how_did_you_hear",
            "label": "How did you hear about us?",
            "kind": "select",
            "required": True,
            "value": _jobvite_referral_source(application_profile) or "",
            "source": "application_profile.md",
        },
        {
            "field_name": "additional_info",
            "label": "Additional Info",
            "kind": "text",
            "required": False,
            "value": str(getattr(application_profile, "how_did_you_hear", "") or ""),
            "source": "application_profile.md",
        },
        {
            "field_name": "school_name",
            "label": "School Name",
            "kind": "text",
            "required": True,
            "value": _jobvite_primary_school_name(application_profile) or "",
            "source": "application_profile.md",
        },
        {
            "field_name": "graduation_month_year",
            "label": "Graduation Month and Year (MM/YYYY)",
            "kind": "text",
            "required": True,
            "value": _jobvite_primary_graduation_month_year(application_profile) or "",
            "source": "application_profile.md",
        },
        {
            "field_name": "highest_level_of_qualification",
            "label": "Highest Level of Qualification",
            "kind": "select",
            "required": True,
            "value": _jobvite_highest_education_level(application_profile) or "",
            "source": "application_profile.md",
        },
    ]
    gender = str(getattr(application_profile, "gender", "") or "").strip()
    if gender:
        extra_steps.append(
            {
                "field_name": "gender",
                "label": "Please Indicate Your Gender",
                "kind": "radio",
                "required": False,
                "value": gender,
                "source": "application_profile.md",
            }
        )
    extra_steps.extend(_jobvite_second_page_self_id_steps(profile, application_profile))

    return build_simple_payload(
        board_name=_BOARD,
        out_dir=out_dir,
        provider=provider,
        meta=meta,
        profile=profile,
        application_profile=application_profile,
        resume_path=resume_path,
        cover_letter_path=cover_letter_path,
        notes=["Jobvite application flow. Draft mode must stop at a real ready-to-submit boundary."],
        extra_steps=extra_steps,
        application_url=_jobvite_application_url(job_url),
    )


def _classify_submit_state(snapshot: dict) -> dict[str, object]:
    return classify_submit_state(
        snapshot,
        confirm_patterns=_CONFIRM_PATTERNS,
        review_patterns=_REVIEW_PATTERNS,
    )


def _jobvite_location_gate_option(options: list[str]) -> str | None:
    try:
        application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    except Exception:
        application_profile = None

    candidates = [
        getattr(application_profile, "country", None),
        getattr(application_profile, "location", None),
        "United States",
        "All Locations (English)",
        "English",
    ]
    for candidate in candidates:
        matched = select_option(options, candidate)
        if matched:
            return matched

    return next(
        (
            option
            for option in options
            if option.strip() and "select your location" not in option.casefold() and "select an option" not in option.casefold()
        ),
        None,
    )


def _advance_jobvite_location_gate(page) -> bool:
    if _expand_jobvite_full_application_form(page):
        return True

    applied_gate = False

    for _attempt in range(2):
        try:
            if page.locator("input[type='file']").count():
                return applied_gate
        except Exception:
            pass

        try:
            location_select = page.get_by_label(_JOBVITE_LOCATION_GATE_LABEL_RE).first
        except Exception:
            location_select = None

        if location_select is not None and location_select.count():
            option_labels: list[str] = []
            try:
                options = location_select.locator("option")
                option_labels = [options.nth(index).inner_text().strip() for index in range(options.count())]
            except Exception:
                option_labels = []

            advanced = applied_gate
            option_label = _jobvite_location_gate_option(option_labels)
            if option_label:
                location_select.select_option(label=option_label)
                page.wait_for_timeout(250)
                advanced = True

            try:
                consent_button = page.get_by_role("button", name=_JOBVITE_GATE_SUBMIT_RE).first
                if consent_button.count():
                    consent_button.click()
                    page.wait_for_timeout(500)
                    advanced = True
            except Exception:
                pass

            if advanced:
                try:
                    page.wait_for_selector(_JOBVITE_FORM_READY_SELECTOR, timeout=15000)
                except Exception:
                    pass
            return advanced

        if not applied_gate and _expand_jobvite_apply_gate(page):
            applied_gate = True
            continue

        return False

    return applied_gate


def _jobvite_button_available(locator) -> bool:
    try:
        if not locator.count():
            return False
        button = locator.first
        if hasattr(button, "is_visible") and not button.is_visible():
            return False
        return not (hasattr(button, "is_enabled") and not button.is_enabled())
    except Exception:
        return False


def _expand_jobvite_full_application_form(page) -> bool:
    for role in ("button", "link"):
        try:
            expand_control = page.get_by_role(role, name=_JOBVITE_VIEW_FULL_APPLICATION_FORM_RE).first
            if not expand_control.count():
                continue
            expand_control.click()
            page.wait_for_timeout(500)
            try:
                page.wait_for_selector(_JOBVITE_FORM_READY_SELECTOR, timeout=15000)
            except Exception:
                pass
            return True
        except Exception:
            continue
    return False


def _expand_jobvite_apply_gate(page) -> bool:
    for role in ("button", "link"):
        try:
            apply_control = page.get_by_role(role, name=_JOBVITE_APPLY_GATE_RE).first
            if not apply_control.count():
                continue
            apply_control.click()
            page.wait_for_timeout(500)
            try:
                page.wait_for_selector(_JOBVITE_FORM_READY_SELECTOR, timeout=15000)
            except Exception:
                pass
            return True
        except Exception:
            continue
    return False


def _jobvite_page_signature(page) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    snapshot = page.evaluate(
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
                if (el.id) {
                    const explicit = document.querySelector(`label[for="${el.id}"]`);
                    const explicitText = (explicit?.innerText || '').trim();
                    if (explicitText) return explicitText;
                }
                return '';
            };
            const controls = Array.from(document.querySelectorAll('input, select, textarea'))
                .filter((el) => isVisible(el))
                .map((el) => {
                    const tag = (el.tagName || '').toLowerCase();
                    const type = (el.getAttribute('type') || '').toLowerCase();
                    const name = (el.getAttribute('name') || '').trim();
                    const id = (el.id || '').trim();
                    const label = labelFor(el);
                    return [tag, type, name, id, label].join('::');
                })
                .filter(Boolean);
            const buttons = Array.from(document.querySelectorAll('button, [role="button"]'))
                .filter((el) => isVisible(el))
                .map((el) => String(el.innerText || el.getAttribute('aria-label') || '').trim())
                .filter(Boolean);
            return {
                url: window.location.href,
                controls,
                buttons,
            };
        }"""
    )
    return (
        str(snapshot.get("url") or ""),
        tuple(str(item) for item in (snapshot.get("controls") or [])),
        tuple(str(item) for item in (snapshot.get("buttons") or [])),
    )


def _advance_jobvite_page(page) -> bool:
    try:
        next_button = page.get_by_role("button", name=_JOBVITE_NEXT_REVIEW_RE)
    except Exception:
        return False
    if not _jobvite_button_available(next_button):
        return False

    before_signature = _jobvite_page_signature(page)
    next_button.first.click()
    page.wait_for_timeout(750)
    try:
        page.wait_for_selector(_JOBVITE_FORM_READY_SELECTOR, timeout=5000)
    except Exception:
        pass
    for _ in range(3):
        try:
            if _jobvite_page_signature(page) != before_signature:
                return True
        except Exception:
            return True
        page.wait_for_timeout(250)
    return False


def _build_fill_step_fn(*, base_fill_step_fn=fill_basic_step):
    state = {"page_index": 1, "blocked_target_page": None}

    def fill_step(page, step: dict) -> None:
        raw_target_page = step.get("page_index")
        try:
            target_page = int(raw_target_page) if raw_target_page is not None else 1
        except (TypeError, ValueError):
            target_page = 1

        blocked_target_page = state.get("blocked_target_page")
        if (
            blocked_target_page is not None
            and state["page_index"] < target_page
            and int(blocked_target_page) <= target_page
        ):
            step.setdefault("status", "skipped_not_found")
            step["note"] = (
                f"Jobvite form could not advance to page {blocked_target_page}; "
                f"skipping {step.get('label') or step.get('field_name') or 'field'} until the earlier page blocker is resolved."
            )
            return

        while state["page_index"] < target_page:
            if not _advance_jobvite_page(page):
                state["blocked_target_page"] = state["page_index"] + 1
                step.setdefault("status", "skipped_not_found")
                step["note"] = (
                    f"Could not advance Jobvite form to page {target_page} before filling {step.get('label') or step.get('field_name') or 'field'}."
                )
                return
            state["page_index"] += 1
            state["blocked_target_page"] = None

        base_fill_step_fn(page, step)

    return fill_step


def _advance_jobvite_to_final_review_boundary(page) -> bool:
    try:
        if detect_live_required_unfilled_fields(page, steps=[]):
            return False
    except Exception:
        pass

    advanced = False
    for _ in range(3):
        try:
            if _jobvite_button_available(page.get_by_role("button", name=_JOBVITE_FINAL_ACTION_RE)):
                break
        except Exception:
            break

        if not _advance_jobvite_page(page):
            break
        advanced = True

    return advanced


def _run_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    return run_simple_board_pipeline(
        payload_path,
        headless=headless,
        submit=submit,
        board_name=_BOARD,
        form_selector=_FORM_SELECTOR,
        submit_button_names=SUBMIT_BUTTON_NAMES,
        classify_state_fn=_classify_submit_state,
        fill_step_fn=_build_fill_step_fn(),
        preferred_capture_selectors=("form", "main"),
        post_navigate_hook=_advance_jobvite_location_gate,
        post_fill_hook=_advance_jobvite_to_final_review_boundary,
    )


def main() -> int:
    return autofill_main(_BOARD, _build_payload, run_browser_fn=_run_browser)


if __name__ == "__main__":
    raise SystemExit(main())
