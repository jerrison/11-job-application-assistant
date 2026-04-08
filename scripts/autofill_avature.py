#!/usr/bin/env python3
"""Avature application autofill.

Avature appears both on ``*.avature.net`` tenants and on branded hosts such as
``careers.jacobs.com``. The flow typically looks like:

1. Public job page or Avature JobDetail page
2. Entry gate (Apply / RegistrationMethods / ApplicationMethods / Login)
3. Multi-step wizard with profile questions, custom questions, disclosures
4. Review / Submit

This script uses ``autofill_main`` with a custom browser flow because the entry
gate and wizard are not compatible with the shared single-form pipeline.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_models import parse_candidate_contact_details
from application_submit_common import (
    APPLICATION_PROFILE_PATH,
    CANDIDATE_CONTEXT_PATH,
    PROJECT_ROOT,
    _best_engagement_option,
    build_email_confirmation_watcher,
    find_cover_letter_file,
    find_resume_file,
    format_education_from_profile,
    generate_application_answers,
    load_meta,
    parse_application_profile,
    parse_master_resume,
    reply_to_confirmation_email,
    resolve_shared_question_policy,
    slugify_label,
    sync_notion_after_submit,
)
from autofill_common import (
    board_file_constants,
    capture_full_page,
    label_matches,
    match_prior_employer_option,
    select_option,
    select_shared_policy_option,
    write_report,
)
from autofill_pipeline import CAPTCHA_SKIP_EXIT_CODE, autofill_main
from browser_runtime import (
    launch_chromium_browser,
    submit_browser_profile_dir,
    submit_slow_mo_ms,
    submit_viewport,
)
from job_board_urls import looks_like_avature_url
from output_layout import migrate_role_output_layout, role_submit_path
from project_env import load_project_env
from question_classifier import classify_question

_BOARD = "avature"
_BOARD_CONSTANTS = board_file_constants(_BOARD)
AUTOFILL_PAYLOAD_JSON = _BOARD_CONSTANTS["payload_json"]
APPLICATION_PAGE_HTML = _BOARD_CONSTANTS["application_page_html"]

AVATURE_EMAIL_ENV = "AVATURE_EMAIL"
AVATURE_PASSWORD_ENV = "AVATURE_PASSWORD"

PAGE_ENTRY = "entry"
PAGE_FORM = "form"
PAGE_REVIEW = "review"
PAGE_CONFIRMATION = "confirmation"
PAGE_UNKNOWN = "unknown"

NEXT_BUTTON_NAMES = (
    "Save",
    "Save & continue",
    "Save and continue",
    "Continue",
    "Next",
    "Review",
    "Proceed",
)
SUBMIT_BUTTON_NAMES = (
    "Submit",
    "Submit application",
    "Submit Application",
    "Apply",
    "Finish",
)

SUBMIT_CONFIRM_PATTERNS = (
    re.compile(r"\bthank(?:s| you)\s+for\s+(?:applying|your (?:application|interest))\b", re.I),
    re.compile(r"\bapplication (?:has been )?(?:received|submitted)\b", re.I),
    re.compile(r"\byour application has been submitted\b", re.I),
    re.compile(r"\bsuccessfully submitted\b", re.I),
    re.compile(r"\bwe(?:'|')ve received your application\b", re.I),
    re.compile(r"\bapplication complete\b", re.I),
)
VALIDATION_ERROR_PATTERNS = (
    re.compile(r"\bthis field is required\b", re.I),
    re.compile(r"\bplease (?:enter|select|complete|fill)\b", re.I),
    re.compile(r"\binvalid\b", re.I),
    re.compile(r"\brequired field\b", re.I),
)
ENTRY_GATE_PATTERNS = (
    "choose an option to register",
    "option to register",
    "already registered",
    "first time applicant",
    "create profile",
    "without resume",
    "upload cv file",
    "upload resume",
    "can't create user",
    "existing account with the email address you entered",
)

_STATE_MAP = {
    "CA": "California",
    "NY": "New York",
    "TX": "Texas",
    "WA": "Washington",
    "IL": "Illinois",
    "MA": "Massachusetts",
    "CO": "Colorado",
    "GA": "Georgia",
    "PA": "Pennsylvania",
    "FL": "Florida",
    "OR": "Oregon",
    "NC": "North Carolina",
    "VA": "Virginia",
    "MD": "Maryland",
    "NJ": "New Jersey",
    "AZ": "Arizona",
    "OH": "Ohio",
    "MN": "Minnesota",
    "MI": "Michigan",
    "CT": "Connecticut",
    "DC": "District of Columbia",
    "UT": "Utah",
    "TN": "Tennessee",
    "MO": "Missouri",
}

load_project_env()


def _normalize_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_label(value: str | None) -> str:
    return _normalize_space(value).removesuffix("*").strip()


def _avature_debug_progress_enabled() -> bool:
    return os.environ.get("AVATURE_DEBUG_PROGRESS", "").strip().lower() in {"1", "true", "yes", "on"}


def _debug_progress(message: str) -> None:
    if _avature_debug_progress_enabled():
        print(f"Avature debug: {message}", file=sys.stderr)


def _candidate_contact_details():
    try:
        return parse_candidate_contact_details(CANDIDATE_CONTEXT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return parse_candidate_contact_details("")


def _location_parts(profile, application_profile, candidate_contact) -> tuple[str, str, str, str, str]:
    location = (profile.location or application_profile.location or "").strip()
    parts = [part.strip() for part in location.split(",") if part.strip()]
    city = parts[0] if parts else (getattr(candidate_contact, "city", None) or "San Francisco")
    state_abbr = parts[-1] if len(parts) >= 2 else (getattr(candidate_contact, "state", None) or "CA")
    state_full = _STATE_MAP.get(state_abbr, state_abbr) or "California"
    country = getattr(application_profile, "country", None) or "United States"
    zip_code = getattr(application_profile, "zip_code", None) or getattr(candidate_contact, "zip_code", None) or ""
    if not zip_code and city.casefold() == "san francisco" and state_abbr == "CA":
        zip_code = "94105"
    return city, state_abbr, state_full, country, zip_code


def _canonical_avature_application_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    job_detail_match = re.match(
        r"^(?P<prefix>/(?:[A-Za-z]{2}_[A-Za-z]{2}/)?careers)/JobDetail/[^/]+/(?P<job_id>\d+)/?$",
        path,
        flags=re.I,
    )
    if job_detail_match:
        prefix = job_detail_match.group("prefix")
        job_id = job_detail_match.group("job_id")
        return urlunparse(parsed._replace(path=f"{prefix}/ApplicationMethods", query=f"jobId={job_id}"))
    return url


def _title_from_avature_job_url(url: str) -> str | None:
    path = urlparse(url).path or ""
    match = re.match(
        r"^/(?:[A-Za-z]{2}_[A-Za-z]{2}/)?careers/JobDetail/(?P<title_slug>[^/]+)/\d+/?$",
        path,
        flags=re.I,
    )
    if not match:
        return None
    title_slug = _normalize_space(match.group("title_slug").replace("-", " "))
    return title_slug or None


def _resolved_job_title(meta: dict, job_url: str) -> str:
    saved_title = _normalize_space(meta.get("jd_title"))
    derived_title = _title_from_avature_job_url(job_url)
    if not derived_title:
        return saved_title
    if not saved_title:
        return derived_title

    normalized_saved = saved_title.casefold()
    suspicious_titles = {
        _normalize_space(meta.get("company_proper")).casefold(),
        _normalize_space(meta.get("company")).casefold(),
    }
    host = (urlparse(job_url).hostname or "").casefold()
    host_parts = [part for part in host.split(".") if part and part not in {"www", "jobs", "careers", "job"}]
    if host_parts:
        suspicious_titles.add(_normalize_space(host_parts[0]).casefold())

    if normalized_saved in suspicious_titles:
        return derived_title
    return saved_title


def _avature_credentials() -> tuple[str, str]:
    profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    email = (
        os.environ.get(AVATURE_EMAIL_ENV)
        or os.environ.get("WORKDAY_EMAIL")
        or os.environ.get("ICIMS_EMAIL")
        or getattr(profile, "verification_code_email", "")
        or ""
    )
    password = (
        os.environ.get(AVATURE_PASSWORD_ENV)
        or os.environ.get("WORKDAY_PASSWORD")
        or os.environ.get("ICIMS_PASSWORD")
        or ""
    )
    if not password:
        raise RuntimeError(
            "Avature password not configured. Set AVATURE_PASSWORD in .env.local or as an environment variable."
        )
    if not email:
        raise RuntimeError(
            "Avature email not configured. Set AVATURE_EMAIL env var or Verification Code Email in application_profile.md."
        )
    return email, password


def _capture(page, path: Path) -> None:
    capture_full_page(page, path, preferred_selectors=("main", "form", "body"))


def _dismiss_cookie_banner(page) -> None:
    for selector in (
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept All')",
        "button:has-text('Accept Cookies')",
        "button:has-text('Accept')",
        "button:has-text('Got it')",
    ):
        try:
            btn = page.locator(selector).first
            if btn.count() and btn.is_visible():
                btn.click()
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def _field_label(input_el) -> str:
    try:
        return _normalize_label(
            input_el.evaluate(
                """
                (el) => {
                  const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                  const fromLabels = el.labels ? Array.from(el.labels).map((node) => clean(node.innerText || node.textContent)).filter(Boolean) : [];
                  if (fromLabels.length) return fromLabels.join(' ');
                  const aria = clean(el.getAttribute('aria-label'));
                  if (aria) return aria;
                  const labelledBy = clean((el.getAttribute('aria-labelledby') || '').split(/\\s+/).map((id) => {
                    const node = id ? document.getElementById(id) : null;
                    return node ? (node.innerText || node.textContent || '') : '';
                  }).join(' '));
                  if (labelledBy) return labelledBy;
                  const fieldset = el.closest('fieldset');
                  const legend = fieldset ? clean(fieldset.querySelector('legend')?.innerText || fieldset.querySelector('legend')?.textContent || '') : '';
                  if (legend) return legend;
                  const parentLabel = el.closest('label');
                  if (parentLabel) return clean(parentLabel.innerText || parentLabel.textContent);
                  const container = el.closest('.field, .fieldset, .formField, .inputGroup, .question, .row, .column');
                  if (container) {
                    const labelNode = container.querySelector('label, legend, h1, h2, h3, h4, h5');
                    if (labelNode) return clean(labelNode.innerText || labelNode.textContent);
                  }
                  const placeholder = clean(el.getAttribute('placeholder'));
                  if (placeholder) return placeholder;
                  return clean(el.name || el.id || '');
                }
                """
            )
        )
    except Exception:
        return ""


def _checkable_option_label(page, input_el) -> str:
    try:
        field_id = input_el.get_attribute("id") or ""
    except Exception:
        field_id = ""
    if field_id:
        try:
            label = page.locator(f"label[for='{field_id}']").first
            if label.count():
                return _normalize_label(label.inner_text())
        except Exception:
            pass
    return _field_label(input_el)


def _click_first_visible(page, selectors: tuple[str, ...], *, force: bool = False) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                locator.scroll_into_view_if_needed()
                locator.click(force=force)
                page.wait_for_timeout(1500)
                return True
        except Exception:
            continue
    return False


def _set_text_field(field, value: str, *, overwrite: bool = True) -> bool:
    try:
        existing = field.input_value()
    except Exception:
        existing = ""
    if existing.strip() == value.strip():
        return True
    if existing.strip() and not overwrite:
        return True
    try:
        field.fill("")
        field.fill(value)
        return True
    except Exception:
        return False


def _fill_by_label(page, label_text: str, value: str, *, overwrite: bool = True) -> bool:
    try:
        field = page.get_by_label(label_text, exact=False).first
        if not field.count():
            return False
        tag = field.evaluate("el => el.tagName.toLowerCase()")
        if tag == "select":
            return _select_field(field, value)
        field_type = (field.get_attribute("type") or "").casefold()
        if field_type == "checkbox":
            desired = value.casefold() in {"yes", "true", "1"}
            if desired and not field.is_checked():
                field.check(force=True)
            if not desired and field.is_checked():
                field.uncheck(force=True)
            return True
        return _set_text_field(field, value, overwrite=overwrite)
    except Exception:
        return False


def _select_field(field, value: str) -> bool:
    try:
        field.select_option(label=value)
        return True
    except Exception:
        pass
    try:
        options = field.locator("option").all_inner_texts()
        matched = select_option(options, value, filter_select_prefix=True)
        if matched:
            field.select_option(label=matched)
            return True
    except Exception:
        pass
    return False


def _fill_text_selector(page, selectors: tuple[str, ...], value: str, *, overwrite: bool = True) -> bool:
    for selector in selectors:
        try:
            field = page.locator(selector).first
            if field.count() and field.is_visible():
                if _set_text_field(field, value, overwrite=overwrite):
                    return True
        except Exception:
            continue
    return False


def _select_selector(page, selectors: tuple[str, ...], value: str) -> bool:
    for selector in selectors:
        try:
            field = page.locator(selector).first
            if field.count():
                if _select_field(field, value):
                    return True
        except Exception:
            continue
    return False


def _current_step_token(page) -> str:
    try:
        step = page.locator("input[name='currentStepIndex']").first
        if step.count():
            step_value = step.input_value().strip()
        else:
            step_value = ""
    except Exception:
        step_value = ""
    try:
        heading = _normalize_space(page.locator("h1, h2, h3").first.inner_text())
    except Exception:
        heading = ""
    return f"{page.url}|{step_value}|{heading[:160]}"


def _discover_avature_application_url(page) -> str | None:
    try:
        discovered = page.evaluate(
            """
            () => {
              const text = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
              const candidates = [];
              for (const node of document.querySelectorAll('a[href], button[href]')) {
                const href = node.href || node.getAttribute('href') || '';
                const label = text(node);
                const loweredHref = href.toLowerCase();
                const loweredLabel = label.toLowerCase();
                let score = 0;
                if (loweredHref.includes('avature')) score += 10;
                if (loweredHref.includes('jobapplication')) score += 15;
                if (loweredHref.includes('applicationmethods')) score += 14;
                if (loweredHref.includes('registrationmethods')) score += 13;
                if (loweredHref.includes('jobdetail')) score += 6;
                if (loweredLabel.includes('apply now')) score += 12;
                else if (loweredLabel.includes('apply')) score += 8;
                if (score > 0) candidates.push({href, score});
              }
              candidates.sort((a, b) => b.score - a.score);
              return candidates.length ? candidates[0].href : null;
            }
            """
        )
        return str(discovered or "").strip() or None
    except Exception:
        return None


def _navigate_to_application_entry(page, application_url: str) -> None:
    page.goto(application_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    _dismiss_cookie_banner(page)

    current_url = page.url
    if looks_like_avature_url(current_url):
        canonical = _canonical_avature_application_url(current_url)
        if canonical != current_url:
            page.goto(canonical, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
            _dismiss_cookie_banner(page)
        return

    discovered = _discover_avature_application_url(page)
    if discovered:
        page.goto(discovered, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
        _dismiss_cookie_banner(page)
        current_url = page.url
        canonical = _canonical_avature_application_url(current_url)
        if canonical != current_url:
            page.goto(canonical, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
            _dismiss_cookie_banner(page)


def _page_text(page, limit: int = 4000) -> str:
    try:
        return page.inner_text("body")[:limit]
    except Exception:
        return ""


def _is_confirmation_page(page) -> bool:
    text = _page_text(page, limit=3000)
    return any(pattern.search(text) for pattern in SUBMIT_CONFIRM_PATTERNS)


def _has_visible_submit_control(page) -> bool:
    for name in SUBMIT_BUTTON_NAMES:
        try:
            btn = page.get_by_role("button", name=name)
            if btn.count() and btn.first.is_visible():
                return True
        except Exception:
            continue
        try:
            locator = page.locator(f"input[type='submit'][value*='{name}' i]").first
            if locator.count() and locator.is_visible():
                return True
        except Exception:
            continue
    return False


def _looks_like_entry_gate(page) -> bool:
    text = _page_text(page, limit=3000).casefold()
    if any(marker in text for marker in ENTRY_GATE_PATTERNS):
        return True
    try:
        if page.locator("input#resumeFile, input[type='file'][name*='resume' i]").count():
            entry_markers = ("register", "without resume", "upload cv", "upload resume")
            if any(marker in text for marker in entry_markers):
                return True
    except Exception:
        pass
    return "/login" in page.url.casefold()


def _entry_gate_error_message(page) -> str | None:
    text = _normalize_space(_page_text(page, limit=3000))
    if not text:
        return None
    patterns = (
        r"(Can(?:'|’)t create user: There is an existing account with the email address you entered\.)",
        r"(The username or password may be incorrect, or access might be restricted\.)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return match.group(1)
    return None


def _detect_current_page(page) -> str:
    if _is_confirmation_page(page):
        return PAGE_CONFIRMATION
    if _looks_like_entry_gate(page):
        return PAGE_ENTRY
    if _has_visible_submit_control(page):
        return PAGE_REVIEW
    try:
        if page.locator("form input, form select, form textarea").count() >= 3:
            return PAGE_FORM
    except Exception:
        pass
    return PAGE_UNKNOWN


def _try_resume_entry(page, payload: dict) -> bool:
    resume_path = str(payload.get("resume_path") or "").strip()
    has_resume = bool(resume_path)

    if has_resume:
        _click_first_visible(
            page,
            (
                "#methodButton--file",
                "button:has-text('My Computer')",
                "a:has-text('My Computer')",
                "button:has-text('Upload CV file')",
                "button:has-text('Upload Resume')",
            ),
            force=True,
        )
        for selector in (
            "input#resumeFile",
            "input[type='file'][name='resumeFile']",
            "input[type='file'][name*='resume' i]",
            "input[type='file']",
        ):
            try:
                file_input = page.locator(selector).first
                if file_input.count():
                    file_input.set_input_files(resume_path)
                    page.wait_for_timeout(750)
                    break
            except Exception:
                continue
        if _click_first_visible(
            page,
            (
                "button#uploadFileResume",
                "button:has-text('Continue')",
                "input[type='submit'][value*='Continue' i]",
            ),
            force=True,
        ):
            return True

    return _click_first_visible(
        page,
        (
            "button:has-text('Without Resume')",
            "a:has-text('Without Resume')",
            "button:has-text('Upload CV later')",
            "a:has-text('Upload CV later')",
        ),
        force=True,
    )


def _try_sign_in(page, email: str, password: str) -> bool:
    email_filled = _fill_text_selector(
        page,
        (
            "input[type='email']",
            "input[name*='user' i]",
            "input[id*='user' i]",
            "input[name*='email' i]",
            "input[id*='email' i]",
        ),
        email,
    )
    password_filled = _fill_text_selector(
        page,
        (
            "input[type='password']",
            "input[name='password']",
            "input[id='password']",
        ),
        password,
    )
    if not (email_filled and password_filled):
        return False
    if not _click_first_visible(
        page,
        (
            "button:has-text('Log in')",
            "button:has-text('Login')",
            "button:has-text('Sign in')",
            "input[type='submit'][value*='Log' i]",
            "input[type='submit'][value*='Sign' i]",
        ),
        force=True,
    ):
        return False
    page.wait_for_timeout(3000)
    return not _looks_like_entry_gate(page)


def _handle_entry_gate(page, payload: dict, email: str, password: str) -> bool:
    if "jobdetail" in page.url.casefold():
        canonical = _canonical_avature_application_url(page.url)
        if canonical != page.url:
            page.goto(canonical, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
            _dismiss_cookie_banner(page)
            return True

    text = _page_text(page, limit=3000).casefold()
    existing_account_error = _entry_gate_error_message(page)
    existing_account_conflict = bool(
        existing_account_error and "existing account with the email address you entered" in existing_account_error.casefold()
    )
    if existing_account_conflict:
        if _click_first_visible(
            page,
            (
                "a:has-text('Login')",
                "button:has-text('Login')",
                "a:has-text('Log in')",
                "button:has-text('Log in')",
            ),
            force=True,
        ):
            page.wait_for_timeout(3000)
            if _try_sign_in(page, email, password):
                return True
        return False

    has_password_field = False
    try:
        has_password_field = page.locator("input[type='password']").count() > 0
    except Exception:
        has_password_field = False

    if has_password_field:
        if _try_sign_in(page, email, password):
            return True
    if "register" in text or "without resume" in text or "upload cv" in text or "upload resume" in text:
        if _try_resume_entry(page, payload):
            page.wait_for_timeout(3000)
            return True
    if has_password_field:
        if _click_first_visible(
            page,
            (
                "a:has-text('Create profile')",
                "button:has-text('Create profile')",
                "a:has-text('Register')",
                "button:has-text('Register')",
            ),
            force=True,
        ):
            page.wait_for_timeout(3000)
            return True
    discovered = _discover_avature_application_url(page)
    if discovered and discovered != page.url:
        page.goto(discovered, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
        _dismiss_cookie_banner(page)
        return True
    return False


def _upload_named_file(page, label_fragments: tuple[str, ...], file_path: str) -> bool:
    if not file_path:
        return False
    try:
        file_inputs = page.locator("input[type='file']")
        for index in range(file_inputs.count()):
            field = file_inputs.nth(index)
            label = _field_label(field)
            if label_matches(label, *label_fragments):
                field.set_input_files(file_path)
                page.wait_for_timeout(750)
                return True
    except Exception:
        pass
    return False


def _selected_multiselect_labels(page, field_id: str) -> list[str]:
    labels: list[str] = []
    for selector in (
        f".select2Container{field_id} .select2-selection__choice",
        f"#fieldSpecContainer{field_id} .select2-selection__choice",
    ):
        try:
            labels = [
                _normalize_space(text) for text in page.locator(selector).all_inner_texts() if _normalize_space(text)
            ]
        except Exception:
            labels = []
        if labels:
            break
    return [label.lstrip("×").strip() for label in labels if label.lstrip("×").strip()]


def _viable_select2_options(page) -> list[tuple[object, str]]:
    viable: list[tuple[object, str]] = []
    options = page.locator(".select2-results__option")
    for index in range(options.count()):
        option = options.nth(index)
        try:
            option_text = _normalize_space(option.inner_text())
        except Exception:
            continue
        lowered = option_text.casefold()
        if not option_text or "no results" in lowered or "searching" in lowered or "loading" in lowered:
            continue
        viable.append((option, option_text))
    return viable


def _select2_add_value(page, field_id: str, search_terms: tuple[str, ...]) -> bool:
    search_input = page.locator(f"input[id='{field_id}-search__field']").first
    selection = page.locator(f".select2Container{field_id}, #fieldSpecContainer{field_id} .select2-selection").first
    if not search_input.count() and not selection.count():
        return False

    for term in search_terms:
        try:
            if selection.count():
                selection.click()
            if search_input.count():
                search_input.click()
                search_input.press("Enter")
                page.wait_for_timeout(250)
                search_input.fill("")
                search_input.type(term, delay=80)
            page.wait_for_timeout(1500)
            viable_options = _viable_select2_options(page)
            picked = False
            for option, option_text in viable_options:
                if term.casefold() not in option_text.casefold():
                    continue
                option.click()
                picked = True
                break
            if not picked and search_input.count():
                search_input.press("ArrowDown")
                search_input.press("Enter")
            page.wait_for_timeout(750)
            if _selected_multiselect_labels(page, field_id):
                return True
        except Exception:
            continue
    return False


def _source_option_match(options: list[str] | None, application_profile) -> tuple[str, str] | None:
    preferred = getattr(application_profile, "how_did_you_hear", None)
    candidate_sources: list[tuple[str, str]] = []
    if preferred:
        candidate_sources.append((preferred, "application_profile.md"))
        if _normalize_space(preferred).casefold() == "corporate website":
            candidate_sources.extend(
                [
                    ("Careers Site", "application_profile.md"),
                    ("Career Site", "application_profile.md"),
                    ("Company Website", "application_profile.md"),
                    ("Employer Website", "application_profile.md"),
                    ("Website", "application_profile.md"),
                ]
            )
    candidate_sources.extend(
        [
            ("LinkedIn", "deterministic"),
            ("Job Board", "deterministic"),
            ("Careers Site", "deterministic"),
            ("Corporate website", "deterministic"),
        ]
    )

    seen: set[str] = set()
    for candidate, source in candidate_sources:
        normalized = _normalize_space(candidate).casefold()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        matched = select_option(options, candidate, filter_select_prefix=True) if options else candidate
        if matched:
            return (matched, source)
    return None


def _fill_required_multiselects(page) -> list[dict]:
    filled: list[dict] = []
    try:
        selects = page.locator("select[multiple]")
    except Exception:
        return filled

    for index in range(selects.count()):
        field = selects.nth(index)
        field_id = _normalize_space(field.get_attribute("id"))
        label = _field_label(field)
        if not field_id or not label:
            continue
        selected = _selected_multiselect_labels(page, field_id)
        if selected:
            continue
        search_terms: tuple[str, ...] | None = None
        if label_matches(label, "capabilities"):
            search_terms = ("Digital", "Analytics", "Technology", "Data")
        elif label_matches(label, "market"):
            search_terms = ("Transportation", "Digital", "Infrastructure", "Technology")
        if not search_terms:
            continue
        if _select2_add_value(page, field_id, search_terms):
            selected = _selected_multiselect_labels(page, field_id)
            filled.append(
                {
                    "field_name": slugify_label(label),
                    "label": label,
                    "kind": "select",
                    "value": ", ".join(selected) if selected else search_terms[0],
                    "source": "deterministic",
                    "filled": True,
                }
            )
    return filled


def _fill_required_acknowledgements(page) -> list[dict]:
    filled: list[dict] = []
    try:
        checkboxes = page.locator("input[type='checkbox']")
    except Exception:
        return filled

    for index in range(checkboxes.count()):
        field = checkboxes.nth(index)
        label = _field_label(field)
        if not label_matches(label, "accept and acknowledge", "privacy", "terms", "consent"):
            continue
        try:
            if not field.is_checked():
                field.check(force=True)
            filled.append(
                {
                    "field_name": slugify_label(label),
                    "label": label,
                    "kind": "checkbox",
                    "value": "Yes",
                    "source": "deterministic",
                    "filled": True,
                }
            )
        except Exception:
            continue
    return filled


def _fill_profile_like_fields(page, profile, application_profile, payload: dict, password: str) -> list[dict]:
    filled: list[dict] = []
    candidate_contact = _candidate_contact_details()
    city, state_abbr, state_full, country, zip_code = _location_parts(profile, application_profile, candidate_contact)
    street_address = (
        getattr(application_profile, "street_address", "") or getattr(candidate_contact, "street_address", "") or ""
    )
    linkedin = getattr(application_profile, "linkedin", None) or profile.linkedin or ""
    resume_path = str(payload.get("resume_path") or "")
    cover_letter_path = str(payload.get("cover_letter_path") or "")

    if resume_path and _upload_named_file(page, ("resume", "cv"), resume_path):
        filled.append(
            {
                "field_name": "resume",
                "label": "Resume",
                "kind": "file",
                "value": Path(resume_path).name,
                "source": "documents/",
                "filled": True,
            }
        )
    if cover_letter_path and _upload_named_file(page, ("cover letter", "portfolio"), cover_letter_path):
        filled.append(
            {
                "field_name": "cover_letter",
                "label": "Cover Letter",
                "kind": "file",
                "value": Path(cover_letter_path).name,
                "source": "documents/",
                "filled": True,
            }
        )

    field_plan = [
        ("preferred_first_name", ("Preferred first name",), profile.first_name, "master_resume.md"),
        ("first_name", ("Legal first name", "First name"), profile.first_name, "master_resume.md"),
        ("last_name", ("Legal last name", "Last name"), profile.last_name, "master_resume.md"),
        ("email", ("Email", "Username"), profile.email, "master_resume.md"),
        ("phone", ("Phone", "Phone number"), re.sub(r"^\\+?1[\\s-]?", "", profile.phone), "master_resume.md"),
        ("linkedin", ("LinkedIn", "Professional site URL"), linkedin, "application_profile.md"),
        ("city", ("Current location", "Home city", "City"), city, "application_profile.md"),
        ("postal_code", ("Zip", "Postal"), zip_code, "application_profile.md"),
        ("password", ("Password",), password, "env"),
        ("password_confirmation", ("Password confirmation", "Verify password"), password, "env"),
    ]

    for field_name, labels, value, source in field_plan:
        if not value:
            continue
        for label in labels:
            _debug_progress(f"profile field {field_name}: trying label '{label}'")
            if _fill_by_label(page, label, value, overwrite=True):
                _debug_progress(f"profile field {field_name}: filled via label '{label}'")
                filled.append(
                    {
                        "field_name": field_name,
                        "label": label,
                        "kind": "text",
                        "value": value,
                        "source": source,
                        "filled": True,
                    }
                )
                break

    for candidate in (country, "United States", "USA"):
        _debug_progress(f"profile field country: trying '{candidate}'")
        if candidate and (
            _fill_by_label(page, "Home country", candidate)
            or _fill_by_label(page, "Country", candidate)
            or _select_selector(
                page,
                (
                    "select[name*='country' i]",
                    "select[id*='country' i]",
                ),
                candidate,
            )
        ):
            filled.append(
                {
                    "field_name": "country",
                    "label": "Country",
                    "kind": "select",
                    "value": candidate,
                    "source": "application_profile.md",
                    "filled": True,
                }
            )
            _debug_progress(f"profile field country: filled '{candidate}'")
            break

    for candidate in (state_full, state_abbr):
        _debug_progress(f"profile field state: trying '{candidate}'")
        if candidate and (
            _fill_by_label(page, "Home state/province", candidate)
            or _fill_by_label(page, "State/province", candidate)
            or _fill_by_label(page, "State", candidate)
            or _select_selector(
                page,
                (
                    "select[name*='state' i]",
                    "select[id*='state' i]",
                    "select[name*='province' i]",
                    "select[id*='province' i]",
                    "select[name*='region' i]",
                    "select[id*='region' i]",
                ),
                candidate,
            )
        ):
            filled.append(
                {
                    "field_name": "state",
                    "label": "State/Province",
                    "kind": "select",
                    "value": candidate,
                    "source": "application_profile.md",
                    "filled": True,
                }
            )
            _debug_progress(f"profile field state: filled '{candidate}'")
            break

    _debug_progress("profile field street_address: trying address selectors")
    if street_address and (
        _fill_by_label(page, "Street Address", street_address)
        or _fill_by_label(page, "Address", street_address)
        or _fill_text_selector(
            page,
            (
                "input[name*='address' i]",
                "input[id*='address' i]",
                "input[name*='street' i]",
                "input[id*='street' i]",
            ),
            street_address,
        )
    ):
        filled.append(
            {
                "field_name": "street_address",
                "label": "Street Address",
                "kind": "text",
                "value": street_address,
                "source": "application_profile.md",
                "filled": True,
            }
        )
        _debug_progress("profile field street_address: filled")

    _debug_progress("profile fields: trying required multiselects")
    filled.extend(_fill_required_multiselects(page))
    _debug_progress("profile fields: trying required acknowledgements")
    filled.extend(_fill_required_acknowledgements(page))
    _debug_progress(f"profile fields: completed with {len(filled)} entries")

    return filled


def _answer_from_classifier(
    label: str, application_profile, options: list[str] | None = None
) -> tuple[str, str] | None:
    if label_matches(label, "how did you hear", "how did you learn", "heard about", "specific information"):
        return _source_option_match(options, application_profile)

    category = classify_question(label)
    if category is None:
        if label_matches(label, "age", "age group", "age range") and getattr(application_profile, "age_range", None):
            return (application_profile.age_range, "application_profile.md")
        return None

    policy = resolve_shared_question_policy(label, application_profile)
    if policy is not None and policy.text_value is not None:
        matched = select_shared_policy_option(options, policy, application_profile=application_profile)
        return (matched or policy.text_value, policy.source)

    if category == "education":
        value = format_education_from_profile(application_profile)
        return (value, "application_profile.md") if value else None
    if category == "work_authorization":
        return (
            "Yes" if getattr(application_profile, "authorized_to_work_unconditionally", True) else "No",
            "application_profile.md",
        )
    if category == "city_location":
        location = getattr(application_profile, "location", None)
        return (location, "application_profile.md") if location else None
    if category == "salary_comfort":
        return (
            "Yes" if getattr(application_profile, "comfortable_with_posted_salary", True) else "No",
            "application_profile.md",
        )
    if category in {"minimum_experience", "experience_confirmation", "office_attendance"}:
        return ("Yes", "application_profile.md")
    if category == "product_usage":
        return ("Yes", "deterministic")
    if category == "company_engagement":
        matched = _best_engagement_option(options or [])
        return (matched, "deterministic") if matched else ("Yes", "deterministic")
    if category == "interview_accommodation":
        return ("No", "deterministic")
    if category == "reasonable_accommodation":
        return ("Yes", "deterministic")
    return None


def _looks_like_profile_question(label: str) -> bool:
    normalized = _normalize_label(label).casefold()
    if not normalized:
        return False

    known_prefixes = (
        "preferred first name",
        "legal first name",
        "first name",
        "legal last name",
        "last name",
        "email",
        "username",
        "phone",
        "phone number",
        "mobile phone number",
        "home phone number",
        "linkedin",
        "professional site url",
        "home country",
        "home state/province",
        "home city",
        "home zip code/postal code",
        "street address",
        "password",
        "password confirmation",
        "verify password",
    )
    for prefix in known_prefixes:
        if normalized == prefix or normalized.startswith((f"{prefix} ", f"{prefix} (")):
            return True
    return False


def _field_has_existing_value(input_el, kind: str) -> bool:
    try:
        if kind in {"text", "textarea", "select"}:
            return bool(_normalize_space(input_el.input_value()))
        if kind == "checkbox":
            return input_el.is_checked()
        if kind == "radio":
            return input_el.is_checked()
    except Exception:
        return False
    return False


def _looks_like_acknowledgement_prompt(label: str) -> bool:
    return label_matches(label, "privacy", "terms", "consent", "agree", "acknowledge", "accept")


def _binary_answer_for_label(
    label: str,
    options: list[str],
    application_profile,
    payload: dict,
) -> tuple[str, str] | None:
    matched = _answer_from_classifier(label, application_profile, options)
    if matched is not None:
        return matched

    if not options:
        return None

    normalized_options = [_normalize_space(option).casefold() for option in options if _normalize_space(option)]
    if not normalized_options:
        return None

    if label_matches(
        label,
        "worked for",
        "former employee",
        "current employee",
        "subsidiar",
        "previously worked",
        "employed in the past",
    ):
        explicit = match_prior_employer_option(options, has_worked_for_company=False)
        if explicit:
            return (explicit, "deterministic")
        return ("No", "deterministic")
    if label_matches(
        label,
        "family member",
        "relative",
        "ernst and young",
        "independent auditing firm",
        "conflict of interest",
    ):
        return ("No", "deterministic")
    if label_matches(label, "text message", "sms"):
        return (
            ("Yes" if getattr(application_profile, "text_message_consent", False) else "No"),
            "application_profile.md",
        )
    if label_matches(label, "privacy", "terms", "consent", "agree", "acknowledge", "accept"):
        return ("Yes", "deterministic")

    yes_like = any(option in {"yes", "y"} or option.startswith("yes ") for option in normalized_options)
    no_like = any(option in {"no", "n"} or option.startswith("no ") for option in normalized_options)
    if not (yes_like and no_like):
        return None

    if label_matches(label, "worked for", payload.get("company", "")):
        return ("No", "deterministic")
    if label_matches(label, "license", "certification", "certificate", "cpa", "bar admission"):
        return None
    return ("Yes", "deterministic")


def _collect_radio_groups(page) -> tuple[list[dict], list[tuple[str, object, str, list[str]]], set[str]]:
    question_specs: list[dict] = []
    field_elements: list[tuple[str, object, str, list[str]]] = []
    handled_names: set[str] = set()
    try:
        _debug_progress("collect radio groups: querying fieldsets")
        fieldsets = page.locator("fieldset").all()
    except Exception:
        return question_specs, field_elements, handled_names

    _debug_progress(f"collect radio groups: inspecting {len(fieldsets)} fieldsets")

    for index, fieldset in enumerate(fieldsets, start=1):
        if index <= 5 or index == len(fieldsets):
            _debug_progress(f"collect radio groups: visiting fieldset {index}/{len(fieldsets)}")
        try:
            legend_locator = fieldset.locator("legend")
            if not legend_locator.count():
                if index <= 5 or index == len(fieldsets):
                    _debug_progress(f"collect radio groups: fieldset {index} had no legend")
                continue
            legend = _normalize_label(legend_locator.first.text_content())
        except Exception:
            legend = ""
        if not legend:
            if index <= 5 or index == len(fieldsets):
                _debug_progress(f"collect radio groups: fieldset {index} had no legend")
            continue
        radios = fieldset.locator("input[type='radio']")
        try:
            radio_count = radios.count()
        except Exception:
            continue
        if index <= 5 or index == len(fieldsets):
            _debug_progress(
                f"collect radio groups: fieldset {index} legend '{legend[:80]}' has {radio_count} radios"
            )
        if radio_count <= 0:
            continue
        first = radios.first
        try:
            name = first.get_attribute("name") or first.get_attribute("id") or ""
        except Exception:
            name = ""
        if name and name in handled_names:
            continue
        options: list[str] = []
        for index in range(radio_count):
            radio = radios.nth(index)
            try:
                if not radio.is_visible():
                    continue
            except Exception:
                continue
            label = _checkable_option_label(page, radio)
            if label and label not in options:
                options.append(label)
        if not options:
            continue
        if name:
            handled_names.add(name)
        question_specs.append(
            {
                "field_name": slugify_label(legend),
                "label": legend,
                "kind": "radio",
                "required": first.get_attribute("required") is not None,
                "options": options,
            }
        )
        field_elements.append((legend, first, "radio", options))
        if index <= 5 or index == len(fieldsets):
            _debug_progress(
                f"collect radio groups: fieldset {index} captured legend '{legend[:80]}' with {len(options)} options"
            )
    return question_specs, field_elements, handled_names


def _looks_like_resume_dataset_field(name: str, field_id: str, label: str) -> bool:
    normalized_name = _normalize_space(name).casefold()
    normalized_label = _normalize_space(label).casefold()
    if normalized_name.startswith("multipledatasetentry_"):
        return True
    if re.fullmatch(r"\d+-\d+-(?:sample|\d+)(?:_hidden)?", normalized_name):
        return True
    if normalized_label in {"employer", "position title", "current position?", "start date", "end date"} and re.match(
        r"^\d+-\d+-",
        normalized_name,
    ):
        return True
    normalized_id = _normalize_space(field_id).casefold()
    return bool(re.fullmatch(r"\d+-\d+-(?:sample|\d+)(?:_hidden)?", normalized_id))


def _collect_question_fields(page) -> tuple[list[dict], list[tuple[str, object, str, list[str]]]]:
    _debug_progress("collect question fields: starting radio-group pass")
    question_specs, field_elements, handled_radio_names = _collect_radio_groups(page)
    _debug_progress(
        f"collect question fields: radio-group pass produced {len(question_specs)} specs and {len(handled_radio_names)} handled names"
    )

    try:
        _debug_progress("collect question fields: querying labels")
        labels = page.locator("label").all()
    except Exception:
        return question_specs, field_elements

    _debug_progress(f"collect question fields: inspecting {len(labels)} labels")

    for index, label_el in enumerate(labels, start=1):
        if index <= 5 or index % 25 == 0:
            _debug_progress(f"collect question fields: visiting label {index}/{len(labels)}")
        try:
            label_text = _normalize_label(label_el.inner_text())
        except Exception:
            continue
        if not label_text or len(label_text) > 400:
            continue

        try:
            for_attr = label_el.get_attribute("for")
        except Exception:
            for_attr = None
        if for_attr:
            input_el = page.locator(f"[id='{for_attr}']").first
        else:
            input_el = label_el.locator("input, select, textarea").first
            if not input_el.count():
                input_el = label_el.locator(
                    "xpath=following::input[1] | xpath=following::select[1] | xpath=following::textarea[1]"
                ).first
        if not input_el.count():
            continue

        try:
            if not input_el.is_visible() and (input_el.get_attribute("type") or "").casefold() != "file":
                continue
        except Exception:
            continue

        try:
            tag = input_el.evaluate("el => el.tagName.toLowerCase()")
        except Exception:
            continue
        input_type = (input_el.get_attribute("type") or "").casefold()
        name_attr = input_el.get_attribute("name") or ""
        id_attr = input_el.get_attribute("id") or ""
        if input_type in {"hidden", "submit", "button", "reset", "file"}:
            continue
        if _looks_like_resume_dataset_field(name_attr, id_attr, label_text):
            continue

        if input_type == "radio":
            name = name_attr or id_attr
            if name in handled_radio_names:
                continue
            if _normalize_space(label_text).casefold() in {"yes", "no", "prefer not to say"}:
                continue

        kind = "text"
        if tag == "textarea":
            kind = "textarea"
        elif tag == "select":
            kind = "select"
        elif input_type == "checkbox":
            kind = "checkbox"
        elif input_type == "radio":
            kind = "radio"

        options: list[str] = []
        if kind == "select":
            try:
                options = [
                    _normalize_space(option)
                    for option in input_el.locator("option").all_inner_texts()
                    if _normalize_space(option)
                ]
            except Exception:
                options = []
        elif kind == "radio":
            name = name_attr or id_attr
            try:
                radios = page.locator(f"input[type='radio'][name='{name}']")
                for index in range(radios.count()):
                    option_label = _checkable_option_label(page, radios.nth(index))
                    if option_label and option_label not in options:
                        options.append(option_label)
            except Exception:
                options = []
            if name:
                handled_radio_names.add(name)

        question_specs.append(
            {
                "field_name": slugify_label(label_text),
                "label": label_text,
                "kind": kind,
                "required": input_el.get_attribute("required") is not None,
                "options": options,
            }
        )
        field_elements.append((label_text, input_el, kind, options))

    return question_specs, field_elements


def _fill_field_element(page, input_el, kind: str, value: str) -> bool:
    try:
        if kind == "select":
            return _select_field(input_el, value)
        if kind == "radio":
            name_attr = input_el.get_attribute("name") or ""
            if name_attr:
                radios = page.locator(f"input[type='radio'][name='{name_attr}']")
                for index in range(radios.count()):
                    radio = radios.nth(index)
                    option_label = _checkable_option_label(page, radio).casefold()
                    radio_value = _normalize_space(radio.get_attribute("value")).casefold()
                    target = _normalize_space(value).casefold()
                    if option_label == target or target in option_label or option_label in target:
                        radio.check(force=True)
                        return True
                    if radio_value and (radio_value == target or target in radio_value or radio_value in target):
                        radio.check(force=True)
                        return True
            return False
        if kind == "checkbox":
            desired = value.casefold() in {"yes", "true", "1"}
            if desired and not input_el.is_checked():
                input_el.check(force=True)
            if not desired and input_el.is_checked():
                input_el.uncheck(force=True)
            return True
        return _set_text_field(input_el, value, overwrite=True)
    except Exception:
        return False


def _fill_application_questions(
    page,
    out_dir: Path,
    meta: dict,
    provider: str | None,
    application_profile,
    payload: dict,
) -> list[dict]:
    filled: list[dict] = []
    _debug_progress("application questions: collecting question fields")
    question_specs, field_elements = _collect_question_fields(page)
    _debug_progress(
        f"application questions: collected {len(question_specs)} question specs and {len(field_elements)} field elements"
    )
    if not question_specs:
        return filled

    llm_candidates: list[dict] = []
    for spec, (label_text, input_el, kind, options) in zip(question_specs, field_elements, strict=False):
        if _looks_like_profile_question(label_text) and _field_has_existing_value(input_el, kind):
            _debug_progress(f"application questions: skipping already-filled profile-like field '{label_text}'")
            continue
        if _looks_like_acknowledgement_prompt(label_text):
            if kind in {"checkbox", "radio", "select"} and _fill_field_element(page, input_el, kind, "Yes"):
                filled.append(
                    {
                        "field_name": spec["field_name"],
                        "label": label_text,
                        "kind": kind,
                        "value": "Yes",
                        "source": "deterministic",
                        "required": spec["required"],
                        "filled": True,
                    }
                )
            else:
                _debug_progress(f"application questions: skipping acknowledgement-like field '{label_text}'")
            continue
        answer_source = _answer_from_classifier(label_text, application_profile, options)
        if answer_source is None and kind in {"radio", "select", "checkbox"}:
            answer_source = _binary_answer_for_label(label_text, options, application_profile, payload)
        if answer_source is None and label_matches(
            label_text,
            "how did you hear",
            "heard about",
            "specific information",
        ):
            answer_source = _source_option_match(options, application_profile)
        if answer_source is not None:
            answer, source = answer_source
            if answer and _fill_field_element(page, input_el, kind, answer):
                filled.append(
                    {
                        "field_name": spec["field_name"],
                        "label": label_text,
                        "kind": kind,
                        "value": answer,
                        "source": source,
                        "required": spec["required"],
                        "filled": True,
                    }
                )
                continue
        llm_candidates.append(spec)

    _debug_progress(
        f"application questions: deterministic filled {len(filled)} fields; unresolved {len(llm_candidates)} fields"
    )
    if not llm_candidates:
        return filled
    if len(llm_candidates) > 12:
        print(f"Avature: skipping LLM on page because {len(llm_candidates)} fields look unresolved.", file=sys.stderr)
        return filled

    llm_specs = []
    for spec in llm_candidates:
        description = ""
        options = spec.get("options") or []
        if options:
            description = f"Options: {' | '.join(options)}"
        llm_specs.append(
            {
                "field_name": spec["field_name"],
                "label": spec["label"],
                "description": description,
                "required": spec["required"],
                "type": spec["kind"],
            }
        )

    print(f"Avature: generating answers for {len(llm_candidates)} unresolved fields.", file=sys.stderr)
    answers = generate_application_answers(
        out_dir=out_dir,
        meta=meta,
        question_specs=llm_specs,
        provider=provider,
    )
    for spec in llm_candidates:
        _debug_progress(f"question field {spec['field_name']}: applying generated answer")
        answer = _normalize_space(answers.get(spec["field_name"], ""))
        if not answer:
            continue
        if spec["kind"] in {"radio", "select"}:
            matched = select_option(spec.get("options"), answer, filter_select_prefix=True)
            if matched:
                answer = matched
        for label_text, input_el, kind, _options in field_elements:
            if slugify_label(label_text) != spec["field_name"]:
                continue
            if _fill_field_element(page, input_el, kind, answer):
                filled.append(
                    {
                        "field_name": spec["field_name"],
                        "label": spec["label"],
                        "kind": kind,
                        "value": answer,
                        "source": "generated",
                        "required": spec["required"],
                        "filled": True,
                    }
                )
            break

    return filled


def _click_action_button_with_fallback(control, *, label: str) -> None:
    click_error: Exception | None = None
    try:
        control.click(timeout=3000, no_wait_after=True)
        return
    except Exception as exc:
        click_error = exc
        print(f"Avature: {label} click timed out; falling back to dispatch_event.", file=sys.stderr)

    try:
        control.dispatch_event("click")
    except Exception as dispatch_exc:
        if click_error is not None:
            raise click_error from dispatch_exc
        raise


def _click_next_button(page) -> bool:
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(500)
    for name in NEXT_BUTTON_NAMES:
        try:
            button = page.get_by_role("button", name=name)
            if button.count() and button.first.is_visible():
                button.first.scroll_into_view_if_needed()
                _click_action_button_with_fallback(button.first, label=name)
                page.wait_for_timeout(2500)
                return True
        except Exception:
            continue
        try:
            locator = page.locator(f"input[type='submit'][value*='{name}' i]").first
            if locator.count() and locator.is_visible():
                _click_action_button_with_fallback(locator, label=name)
                page.wait_for_timeout(2500)
                return True
        except Exception:
            continue
    return False


def _click_submit_button(page) -> bool:
    for name in SUBMIT_BUTTON_NAMES:
        try:
            button = page.get_by_role("button", name=name)
            if button.count() and button.first.is_visible():
                button.first.scroll_into_view_if_needed()
                _click_action_button_with_fallback(button.first, label=name)
                page.wait_for_timeout(2500)
                return True
        except Exception:
            continue
        try:
            locator = page.locator(f"input[type='submit'][value*='{name}' i]").first
            if locator.count() and locator.is_visible():
                _click_action_button_with_fallback(locator, label=name)
                page.wait_for_timeout(2500)
                return True
        except Exception:
            continue
    return False


def _page_snapshot(page) -> dict:
    page_text = _page_text(page, limit=5000)
    errors = [pattern.search(page_text).group(0) for pattern in VALIDATION_ERROR_PATTERNS if pattern.search(page_text)]
    return {
        "url": page.url,
        "page_text": page_text,
        "errors": list(dict.fromkeys(errors)),
    }


def _classify_submit_state(snapshot: dict[str, object]) -> dict[str, object]:
    page_text = str(snapshot.get("page_text") or "")
    if any(pattern.search(page_text) for pattern in SUBMIT_CONFIRM_PATTERNS):
        return {"status": "confirmed", "reason": "text"}
    errors = list(snapshot.get("errors") or [])
    if errors:
        return {"status": "validation_error", "errors": errors}
    return {"status": "pending"}


def _build_payload(out_dir: Path, provider: str | None = None) -> dict:
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)
    profile = parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))

    job_url = str(meta.get("jd_source_resolved") or meta["jd_source"])
    company_proper = str(meta.get("company_proper") or meta.get("company") or "")
    jd_title = _resolved_job_title(meta, job_url)

    try:
        resume_path = str(find_resume_file(out_dir))
    except FileNotFoundError:
        resume_path = ""
    try:
        cover_letter_path = str(find_cover_letter_file(out_dir))
    except FileNotFoundError:
        cover_letter_path = ""

    application_url = _canonical_avature_application_url(job_url) if looks_like_avature_url(job_url) else job_url

    return {
        "board": _BOARD,
        "job_url": job_url,
        "application_url": application_url,
        "out_dir": str(out_dir),
        "company": company_proper,
        "company_slug": str(meta.get("company") or ""),
        "candidate_name": profile.full_name,
        "candidate_email": profile.email,
        "verification_code_email": getattr(application_profile, "verification_code_email", "") or profile.email,
        "job_title": jd_title,
        "provider": provider,
        "resume_path": resume_path,
        "cover_letter_path": cover_letter_path,
        "fields": [],
        "steps": [],
        "artifacts": {
            "payload_json": str(role_submit_path(out_dir, _BOARD_CONSTANTS["payload_json"])),
            "report_json": str(role_submit_path(out_dir, _BOARD_CONSTANTS["report_json"])),
            "report_markdown": str(role_submit_path(out_dir, _BOARD_CONSTANTS["report_md"])),
            "pre_submit_screenshot": str(role_submit_path(out_dir, _BOARD_CONSTANTS["pre_submit_screenshot"])),
            "page_screenshots_dir": str(role_submit_path(out_dir, _BOARD_CONSTANTS["page_screenshots_dir"])),
            "submit_debug_html": str(role_submit_path(out_dir, _BOARD_CONSTANTS["submit_debug_html"])),
            "submit_debug_screenshot": str(role_submit_path(out_dir, _BOARD_CONSTANTS["submit_debug_screenshot"])),
            "application_page_html": str(role_submit_path(out_dir, _BOARD_CONSTANTS["application_page_html"])),
        },
    }


def _write_auth_failure_log(out_dir: Path, payload: dict, page, message: str) -> None:
    submit_dir = role_submit_path(out_dir, "")
    submit_dir.mkdir(parents=True, exist_ok=True)
    log_path = submit_dir / "avature_auth_failure.json"
    result = {
        "status": "auth_failed",
        "board": _BOARD,
        "job_url": payload.get("job_url", ""),
        "company": payload.get("company", ""),
        "job_title": payload.get("job_title", ""),
        "email": payload.get("candidate_email", ""),
        "page_url": page.url if page else "",
        "page_text_excerpt": _page_text(page, limit=1000) if page else "",
        "updated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "message": message,
        "suggestions": [
            "Visit the Avature application page manually and confirm whether the account already exists.",
            "If sign-in fails, create the profile manually once and rerun the draft flow.",
            "Set AVATURE_PASSWORD in .env.local if this tenant requires account creation.",
            "Then rerun: uv run python scripts/submit_application.py <out_dir> --draft",
        ],
    }
    log_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    submission_result = {
        "status": "skipped_auth_failure",
        "website_confirmed": False,
        "provider": _BOARD,
        "board": _BOARD,
        "updated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "message": message,
    }
    (submit_dir / "application_submission_result.json").write_text(
        json.dumps(submission_result, indent=2) + "\n",
        encoding="utf-8",
    )


def _run_avature_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: Playwright is not installed.", file=sys.stderr)
        return 1

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    out_dir = Path(payload["out_dir"])
    meta = load_meta(out_dir)
    profile = parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    email, password = _avature_credentials()

    page_screenshots_dir = Path(payload["artifacts"]["page_screenshots_dir"])
    page_screenshots_dir.mkdir(parents=True, exist_ok=True)
    all_filled: list[dict] = []
    page_index = 0

    with sync_playwright() as playwright:
        viewport = submit_viewport()
        browser = launch_chromium_browser(
            playwright,
            headless=headless,
            slow_mo=submit_slow_mo_ms(headless),
            channel_env_var="JOB_ASSETS_SUBMIT_BROWSER_CHANNEL",
            executable_env_var="JOB_ASSETS_SUBMIT_BROWSER_EXECUTABLE",
            persistent_profile_dir=submit_browser_profile_dir(),
            prefer_local_browser=True,
            viewport=viewport,
            device_scale_factor=2,
            purpose="Avature autofill",
        )
        page = browser.new_page(viewport=viewport, device_scale_factor=2)

        try:
            application_url = str(payload.get("application_url") or payload["job_url"])
            _navigate_to_application_entry(page, application_url)
            Path(payload["artifacts"]["application_page_html"]).write_text(page.content(), encoding="utf-8")

            for attempt in range(15):
                _debug_progress(f"main loop: attempt {attempt + 1}")
                page.wait_for_timeout(1500)
                _dismiss_cookie_banner(page)
                current_page = _detect_current_page(page)
                print(f"Avature: on page '{current_page}' (step {attempt + 1})", file=sys.stderr)

                page_index += 1
                page_path = page_screenshots_dir / f"page_{page_index:02d}_{current_page}.png"
                _capture(page, page_path)

                if current_page == PAGE_CONFIRMATION:
                    outcome = {
                        "status": "confirmed",
                        "reason": "text",
                        "snapshot": _page_snapshot(page),
                    }
                    sync_notion_after_submit(payload, outcome, provider=_BOARD)
                    reply_to_confirmation_email(payload, board_name=_BOARD)
                    return 0

                if current_page == PAGE_ENTRY:
                    if _handle_entry_gate(page, payload, email, password):
                        print("Avature: advanced through entry gate.", file=sys.stderr)
                        continue
                    debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
                    debug_html = Path(payload["artifacts"]["submit_debug_html"])
                    debug_html.write_text(page.content(), encoding="utf-8")
                    _capture(page, debug_png)
                    _write_auth_failure_log(
                        out_dir,
                        payload,
                        page,
                        _entry_gate_error_message(page)
                        or "Avature never reached the application form after trying the visible entry or auth flow.",
                    )
                    return CAPTCHA_SKIP_EXIT_CODE

                if current_page in {PAGE_FORM, PAGE_REVIEW} or _has_visible_submit_control(page):
                    _debug_progress(f"main loop: filling page classified as {current_page}")
                    filled = _fill_profile_like_fields(page, profile, application_profile, payload, password)
                    _debug_progress(f"main loop: profile-like fill produced {len(filled)} entries")
                    filled.extend(
                        _fill_application_questions(
                            page,
                            out_dir,
                            meta,
                            payload.get("provider"),
                            application_profile,
                            payload,
                        )
                    )
                    _debug_progress(f"main loop: total filled entries now {len(filled)}")
                    print(f"Avature: recorded {len(filled)} filled fields on this page.", file=sys.stderr)
                    for entry in filled:
                        entry["page_index"] = page_index
                    all_filled.extend(filled)

                    if _has_visible_submit_control(page):
                        pre_submit_path = Path(payload["artifacts"]["pre_submit_screenshot"])
                        _capture(page, pre_submit_path)
                        runtime_payload = dict(payload)
                        runtime_payload["steps"] = all_filled
                        write_report(runtime_payload, board_name=_BOARD, runtime={"steps": all_filled})

                        if not submit:
                            return 0

                        if not _click_submit_button(page):
                            break

                        submit_started_at_utc = datetime.now(UTC).replace(microsecond=0).isoformat()
                        email_watcher = build_email_confirmation_watcher(
                            payload,
                            min_received_at_utc=submit_started_at_utc,
                        )
                        for _ in range(30):
                            page.wait_for_timeout(1000)
                            email_confirmation = email_watcher.poll()
                            snapshot = _page_snapshot(page)
                            if email_confirmation:
                                outcome = {
                                    "status": "confirmed",
                                    "reason": "email_confirmation",
                                    "snapshot": snapshot,
                                    "email_confirmation": email_confirmation,
                                }
                                sync_notion_after_submit(
                                    payload,
                                    outcome,
                                    provider=_BOARD,
                                    email_confirmation=email_confirmation,
                                    min_received_at_utc=submit_started_at_utc,
                                )
                                reply_to_confirmation_email(
                                    payload,
                                    board_name=_BOARD,
                                    email_confirmation=email_confirmation,
                                )
                                return 0
                            state = _classify_submit_state(snapshot)
                            if state["status"] == "confirmed":
                                outcome = {
                                    "status": "confirmed",
                                    "reason": state.get("reason"),
                                    "snapshot": snapshot,
                                }
                                sync_notion_after_submit(
                                    payload,
                                    outcome,
                                    provider=_BOARD,
                                    min_received_at_utc=submit_started_at_utc,
                                )
                                reply_to_confirmation_email(payload, board_name=_BOARD)
                                return 0
                        break

                    before = _current_step_token(page)
                    if not _click_next_button(page):
                        print("Avature: could not find a next button.", file=sys.stderr)
                        break
                    after = _current_step_token(page)
                    if before == after and not _has_visible_submit_control(page):
                        print("Avature: page token did not change after clicking next.", file=sys.stderr)
                        break
                    continue

                break

            debug_html = Path(payload["artifacts"]["submit_debug_html"])
            debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
            debug_html.write_text(page.content(), encoding="utf-8")
            _capture(page, debug_png)
            if all_filled:
                runtime_payload = dict(payload)
                runtime_payload["steps"] = all_filled
                write_report(runtime_payload, board_name=_BOARD, runtime={"steps": all_filled})
            return 1
        finally:
            browser.close()


def main() -> int:
    return autofill_main(
        board_name=_BOARD,
        build_payload_fn=_build_payload,
        run_browser_fn=_run_avature_browser,
    )


if __name__ == "__main__":
    raise SystemExit(main())
