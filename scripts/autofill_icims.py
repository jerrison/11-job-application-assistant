#!/usr/bin/env python3
"""Generate and optionally run an iCIMS application autofill flow.

iCIMS uses a multi-page authenticated wizard (login/register -> profile ->
application questions -> demographics -> review), so this board script uses
``autofill_main`` with a custom ``run_browser_fn`` instead of the shared
``run_browser_pipeline`` (which assumes a single-form model).
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_submit_common import (
    APPLICATION_PROFILE_PATH,
    PROJECT_ROOT,
    build_email_confirmation_watcher,
    build_truthful_work_authorization_answer,
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
    select_shared_policy_option,
    wait_for_captcha_resolution,
    write_report,
)
from autofill_pipeline import CAPTCHA_SKIP_EXIT_CODE, autofill_main
from browser_runtime import (
    human_fill,
    launch_chromium_browser,
    submit_browser_profile_dir,
    submit_slow_mo_ms,
    submit_viewport,
)
from job_board_urls import icims_auth_scope, looks_like_oracle_hcm_url
from output_layout import migrate_role_output_layout, role_submit_path
from project_env import load_project_env
from question_classifier import classify_question

_BOARD_CONSTANTS = board_file_constants("icims")
AUTOFILL_PAYLOAD_JSON = _BOARD_CONSTANTS["payload_json"]
APPLICATION_PAGE_HTML = _BOARD_CONSTANTS["application_page_html"]

ICIMS_EMAIL_ENV = "ICIMS_EMAIL"
ICIMS_PASSWORD_ENV = "ICIMS_PASSWORD"

SUBMIT_CONFIRM_PATTERNS = (
    re.compile(r"\bthank(?:s| you)\s+for\s+(?:applying|your (?:application|interest))\b", re.I),
    re.compile(r"\bapplication (?:has been )?(?:received|submitted)\b", re.I),
    re.compile(r"\byour application has been submitted\b", re.I),
    re.compile(r"\bsubmitted successfully\b", re.I),
    re.compile(r"\bsuccessfully submitted\b", re.I),
    re.compile(r"\bwe(?:'|')ll be in touch\b", re.I),
    re.compile(r"\bwe(?:'|')ve received your application\b", re.I),
    re.compile(r"\bapplication complete\b", re.I),
)
VALIDATION_ERROR_PATTERNS = (
    re.compile(r"\bthis field is required\b", re.I),
    re.compile(r"\bplease (?:enter|select|complete|fill)\b", re.I),
    re.compile(r"\binvalid\b", re.I),
    re.compile(r"\brequired field\b", re.I),
)
PREFERRED_CAPTURE_SELECTORS = ("main", "#content", ".iCIMS_MainWrapper", "body")

FORM_SELECTOR = "form"
AUTH_EMAIL_SELECTOR = (
    "input[name='css_loginName'], input[type='email'], input[name='email'], "
    "input[name='username'], input[id='email'], input[id='username'], "
    "input[placeholder*='mail'], input[placeholder*='Email']"
)
AUTH_PASSWORD_SELECTOR = "input[type='password'], input[name='password'], input[id='password']"
AUTH_SUBMIT_SELECTOR = (
    "#enterEmailSubmitButton, button[type='submit'], button:has-text('Sign In'), "
    "button:has-text('Log In'), button:has-text('Login'), button:has-text('Continue'), "
    "button:has-text('Next'), input[type='submit'], #continue"
)
AUTH_SIGN_IN_SELECTOR = (
    "a:has-text('Sign In'), a:has-text('Log In'), a:has-text('Login'), "
    "button:has-text('Sign In'), [data-tab='login'], #loginTab"
)
AUTH_CONTEXT_HINTS = (
    "enter your information",
    "sign in",
    "log in",
    "login",
    "create account",
    "register",
)


def _dismiss_cookie_banner(page) -> None:
    """Dismiss cookie consent banner (OneTrust, generic) if present."""
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


def _dismiss_privacy_overlay(page) -> None:
    """Dismiss 'Data Privacy Agreement' dialog if present.

    TalentBrew and iCIMS sites (e.g. Applied Materials, Synopsys) may show
    a modal overlay requiring the user to agree to a data privacy policy
    before the page is interactive.  Must be dismissed before clicking Apply.
    """
    for selector in (
        "button:has-text('I Agree')",
        "button:has-text('Agree')",
        "a:has-text('I Agree')",
        "a:has-text('Agree')",
        "button:has-text('I Accept')",
        "input[type='submit'][value*='Agree' i]",
    ):
        try:
            btn = page.locator(selector).first
            if btn.count() and btn.is_visible():
                btn.click()
                page.wait_for_timeout(1000)
                return
        except Exception:
            continue


load_project_env()


# ─── URL helpers ─────────────────────────────────────────────────────────────


def _icims_application_url(job_url: str) -> str:
    """Derive the iCIMS application URL from a JD URL.

    If the URL already contains icims.com, ensure it ends with /login.
    Otherwise, return as-is (the browser pipeline will discover the apply link).
    """
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(job_url)
    host = (parsed.hostname or "").casefold()
    if "icims.com" in host:
        path = parsed.path.rstrip("/")
        # Ensure path ends at the job level (e.g. /jobs/28629)
        # Remove /login or /job suffix if present, then re-add /login
        path = re.sub(r"/(login|job|apply)$", "", path)
        return urlunparse(parsed._replace(path=f"{path}/login"))
    return job_url


def _discover_icims_url_from_page(page) -> str | None:
    """Look for an iCIMS application link on the current page.

    Checks for direct ``icims.com/jobs/`` links first, then for TalentBrew
    apply links (``/job/…/apply``) commonly used on sites like
    ``jobs.intuit.com`` that embed iCIMS behind a TalentBrew layer.
    """
    return page.evaluate(
        """() => {
            const links = Array.from(document.querySelectorAll('a[href]'));
            // Priority 1: direct icims.com links
            for (const link of links) {
                const href = link.href || '';
                if (href.includes('icims.com/jobs/')) {
                    return href;
                }
            }
            // Priority 2: TalentBrew apply links (/job/.../apply or ?apply)
            // Use startsWith instead of === because TalentBrew often appends
            // the job title for accessibility (e.g. "Apply Now for Director …").
            for (const link of links) {
                const href = link.href || '';
                const text = (link.textContent || '').trim().toLowerCase();
                if ((text.startsWith('apply now') || text === 'apply')
                    && (href.includes('/apply') || href.includes('?apply'))) {
                    return href;
                }
            }
            return null;
        }"""
    )


# ─── Auth flow ───────────────────────────────────────────────────────────────


def _icims_credentials() -> tuple[str, str]:
    """Return (email, password) for iCIMS login.

    Uses ICIMS_EMAIL / ICIMS_PASSWORD env vars, falling back to the
    application profile's verification_code_email and WORKDAY_PASSWORD
    (many users reuse credentials across boards).
    """
    profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    email = (
        os.environ.get(ICIMS_EMAIL_ENV)
        or os.environ.get("WORKDAY_EMAIL")
        or getattr(profile, "verification_code_email", "")
        or ""
    )
    password = os.environ.get(ICIMS_PASSWORD_ENV) or os.environ.get("WORKDAY_PASSWORD", "")
    if not password:
        raise RuntimeError(
            "iCIMS password not configured. Set ICIMS_PASSWORD (or WORKDAY_PASSWORD) "
            "in .env.local or as an environment variable."
        )
    if not email:
        raise RuntimeError(
            "iCIMS email not configured. Set ICIMS_EMAIL env var or Verification Code Email in application_profile.md."
        )
    return email, password


def _handle_auth(page, email: str, password: str) -> bool:
    """Handle iCIMS login/registration page.

    iCIMS typically has:
    1. A login form with email + password
    2. A "Create Account" or "Register" option
    3. Sometimes a "Sign in with LinkedIn" button

    Strategy: try to sign in first. If that fails, try to create account.
    """
    page.wait_for_timeout(2000)

    # Check if we're already past auth (on the application form)
    if _is_application_page(page):
        return True

    for scope in _candidate_auth_scopes(page):
        try:
            if _do_sign_in(scope, email, password):
                return True
        except Exception as exc:
            print(f"iCIMS: sign-in scope failed: {exc}", file=sys.stderr)
        try:
            if _do_create_account(scope, email, password):
                return True
        except Exception as exc:
            print(f"iCIMS: create-account scope failed: {exc}", file=sys.stderr)

    return _is_application_page(page)


def _locator_is_editable(locator) -> bool:
    try:
        return locator.is_editable()
    except Exception:
        return True


def _first_visible_enabled_locator(page, selector: str, *, require_editable: bool = False):
    locator = page.locator(selector)
    try:
        locator_count = locator.count()
    except Exception:
        return None
    for index in range(locator_count):
        candidate = locator.nth(index)
        try:
            if not candidate.is_visible() or not candidate.is_enabled():
                continue
            if require_editable and not _locator_is_editable(candidate):
                continue
            return candidate
        except Exception:
            continue
    return None


def _first_visible_locator(page, selector: str):
    locator = page.locator(selector)
    try:
        locator_count = locator.count()
    except Exception:
        return None
    for index in range(locator_count):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible():
                return candidate
        except Exception:
            continue
    return None


def _accept_auth_privacy_consent(page) -> bool:
    """Accept a required auth-side privacy/GDPR confirmation when present."""

    consent_checkbox = _first_visible_locator(
        page,
        "#accept_gdpr, "
        "input[type='checkbox'][name*='gdpr' i], input[type='checkbox'][id*='gdpr' i], "
        "input[type='checkbox'][name*='privacy' i], input[type='checkbox'][id*='privacy' i], "
        "input[type='checkbox'][name*='consent' i], input[type='checkbox'][id*='consent' i]",
    )
    if consent_checkbox is None:
        return False

    try:
        if consent_checkbox.is_checked():
            return False
    except Exception:
        pass

    for attempt in (
        lambda: consent_checkbox.check(force=True),
        lambda: consent_checkbox.click(force=True),
        lambda: _first_visible_locator(
            page,
            "label[for='accept_gdpr'], label:has-text('I confirm'), label:has-text('I agree'), "
            "button:has-text('I confirm'), button:has-text('I agree'), "
            "a:has-text('I confirm'), a:has-text('I agree')",
        ).click(force=True),
    ):
        try:
            attempt()
            page.wait_for_timeout(500)
            return True
        except Exception:
            continue

    return False


def _click_auth_action(button, page, *, wait_ms: int) -> None:
    try:
        button.click()
    except Exception as exc:
        message = str(exc).casefold()
        if "intercepts pointer events" not in message and "timeout" not in message:
            raise
        button.click(force=True)
    page.wait_for_timeout(wait_ms)


def _scope_has_auth_markers(scope) -> bool:
    if _first_visible_enabled_locator(scope, "input[name='css_loginName'], #email") is not None:
        return True
    if _first_visible_enabled_locator(scope, AUTH_PASSWORD_SELECTOR) is not None:
        return True
    if _first_visible_enabled_locator(scope, "#enterEmailSubmitButton"):
        return True
    if _first_visible_enabled_locator(scope, AUTH_SIGN_IN_SELECTOR):
        return True
    try:
        body_text = scope.inner_text("body").casefold()
    except Exception:
        return False
    return any(hint in body_text for hint in AUTH_CONTEXT_HINTS)


def _auth_scope_score(scope) -> int:
    score = 0
    if _first_visible_enabled_locator(scope, "input[name='css_loginName'], #email") is not None:
        score += 8
    if _first_visible_enabled_locator(scope, AUTH_PASSWORD_SELECTOR) is not None:
        score += 4
    if _first_visible_enabled_locator(scope, "#enterEmailSubmitButton"):
        score += 2
    try:
        body_text = scope.inner_text("body").casefold()
    except Exception:
        body_text = ""
    if "enter your information" in body_text:
        score += 2
    if any(hint in body_text for hint in ("sign in", "create account", "register")):
        score += 1
    return score


def _candidate_auth_scopes(page) -> list[object]:
    scopes: list[object] = []
    seen: set[int] = set()

    def remember(scope) -> None:
        marker = id(scope)
        if marker in seen:
            return
        seen.add(marker)
        scopes.append(scope)

    for scope in list(getattr(page, "frames", []) or []):
        if _scope_has_auth_markers(scope):
            remember(scope)
    if _scope_has_auth_markers(page):
        remember(page)

    if not scopes:
        return [page]
    return sorted(scopes, key=_auth_scope_score, reverse=True)


def _do_sign_in(page, email: str, password: str) -> bool:
    """Fill sign-in form and submit."""
    # Look for sign-in tab/link first (iCIMS often has Login/Register tabs)
    sign_in_tab = page.locator(AUTH_SIGN_IN_SELECTOR)
    email_input = _first_visible_enabled_locator(page, AUTH_EMAIL_SELECTOR, require_editable=True)
    password_input = _first_visible_enabled_locator(page, AUTH_PASSWORD_SELECTOR, require_editable=True)
    if email_input is None and sign_in_tab.count():
        try:
            sign_in_tab.first.click()
            page.wait_for_timeout(1000)
        except Exception:
            pass
        email_input = _first_visible_enabled_locator(page, AUTH_EMAIL_SELECTOR, require_editable=True)
        password_input = _first_visible_enabled_locator(page, AUTH_PASSWORD_SELECTOR, require_editable=True)

    # Fill email
    if email_input is None:
        return False
    email_input.fill(email)
    _accept_auth_privacy_consent(page)

    # Fill password
    if password_input is None:
        continue_btn = _first_visible_enabled_locator(page, AUTH_SUBMIT_SELECTOR)
        if continue_btn is None:
            return False
        _click_auth_action(continue_btn, page, wait_ms=1000)
        password_input = _first_visible_enabled_locator(page, AUTH_PASSWORD_SELECTOR, require_editable=True)
        if password_input is None and _accept_auth_privacy_consent(page):
            continue_btn = _first_visible_enabled_locator(page, AUTH_SUBMIT_SELECTOR)
            if continue_btn is None:
                return False
            _click_auth_action(continue_btn, page, wait_ms=1000)
            password_input = _first_visible_enabled_locator(page, AUTH_PASSWORD_SELECTOR, require_editable=True)
        if password_input is None:
            return False
    password_input.fill(password)
    _accept_auth_privacy_consent(page)

    # Click sign-in button
    sign_in_btn = _first_visible_enabled_locator(page, AUTH_SUBMIT_SELECTOR)
    if sign_in_btn is not None:
        _click_auth_action(sign_in_btn, page, wait_ms=5000)

    # Check for success
    if _is_application_page(page):
        return True

    # Check for error (wrong password, etc.)
    error_el = page.locator("[role='alert'], .error, .errorMessage, .iCIMS_ErrorMessage, .alert-danger")
    if error_el.count():
        err = error_el.first.inner_text()
        print(f"iCIMS sign-in error: {err}", file=sys.stderr)

    return False


def _do_create_account(page, email: str, password: str) -> bool:
    """Create a new iCIMS account.

    Handles two patterns:
    1. Traditional register form (Register/Create Account tab with name/email/password)
    2. "First time here?" flow where the user uploads a resume via "My Computer"
       or clicks "Without Resume" to proceed directly.
    """
    # --- Pattern 2: "First time here?" flow ---
    # Some iCIMS instances combine login and first-time upload on one page.
    # Click "Without Resume" to proceed — the resume gets uploaded on the
    # profile page via _fill_profile_page.
    for btn_text in ("Without Resume", "My Computer"):
        btn = page.locator(f"button:has-text('{btn_text}'), a:has-text('{btn_text}')").first
        if btn.count():
            try:
                if btn_text == "My Computer":
                    # "My Computer" opens a file chooser — dismiss it and
                    # fall through so _fill_profile_page handles the upload.
                    with page.expect_file_chooser(timeout=5000) as fc_info:
                        btn.click()
                    fc_info.value.set_files([])
                else:
                    btn.click()
                page.wait_for_timeout(5000)
                if _is_application_page(page):
                    return True
            except Exception as exc:
                print(f"iCIMS: '{btn_text}' click failed: {exc}", file=sys.stderr)

    # --- Pattern 1: Traditional register form ---
    register_tab = page.locator(
        "a:has-text('Register'), a:has-text('Create Account'), "
        "a:has-text('Create an Account'), a:has-text('Sign Up'), "
        "button:has-text('Register'), button:has-text('Create Account'), "
        "[data-tab='register'], #registerTab"
    )
    if register_tab.count():
        try:
            register_tab.first.click()
            page.wait_for_timeout(1000)
        except Exception:
            pass

    # iCIMS registration typically asks for: email, password, confirm password,
    # and sometimes first/last name.
    master_profile = parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))

    def fill_visible_registration_fields() -> None:
        first_name_input = _first_visible_enabled_locator(
            page,
            "input[name='firstName'], input[name='first_name'], input[id='firstName'], input[placeholder*='First']",
            require_editable=True,
        )
        if first_name_input is not None:
            first_name_input.fill(master_profile.first_name)

        last_name_input = _first_visible_enabled_locator(
            page,
            "input[name='lastName'], input[name='last_name'], input[id='lastName'], input[placeholder*='Last']",
            require_editable=True,
        )
        if last_name_input is not None:
            last_name_input.fill(master_profile.last_name)

        email_input = _first_visible_enabled_locator(
            page,
            "input[type='email'], input[name='email'], input[id='email'], input[placeholder*='mail']",
            require_editable=True,
        )
        if email_input is not None:
            email_input.fill(email)

        password_inputs = page.locator("input[type='password']")
        filled_passwords = 0
        for index in range(password_inputs.count()):
            candidate = password_inputs.nth(index)
            try:
                if not candidate.is_visible() or not candidate.is_enabled() or not _locator_is_editable(candidate):
                    continue
                candidate.fill(password)
                filled_passwords += 1
                if filled_passwords >= 2:
                    break
            except Exception:
                continue

    for _ in range(2):
        fill_visible_registration_fields()
        _accept_auth_privacy_consent(page)

        terms_checkbox = page.locator(
            "input[type='checkbox'][name*='terms'], "
            "input[type='checkbox'][name*='agree'], "
            "input[type='checkbox'][id*='terms']"
        ).first
        if terms_checkbox.count() and not terms_checkbox.is_checked():
            try:
                terms_checkbox.check()
            except Exception:
                terms_checkbox.click(force=True)

        register_btn = _first_visible_enabled_locator(
            page,
            "button:has-text('Register'), button:has-text('Create Account'), "
            "button:has-text('Sign Up'), button[type='submit'], "
            "input[type='submit']",
        )
        if register_btn is None:
            break
        _click_auth_action(register_btn, page, wait_ms=5000)
        if _is_application_page(page):
            return True

    return _is_application_page(page)


def _has_login_indicators(page) -> bool:
    """Check if the page has login/registration form indicators."""
    try:
        body_text = page.inner_text("body")[:3000].lower()
    except Exception:
        return False

    # Registration options page (e.g., "Choose an Option to Register")
    # has no password field — just action buttons.  Detect it early.
    registration_option_phrases = (
        "option to register",
        "choose an option to register",
    )
    if any(phrase in body_text for phrase in registration_option_phrases):
        return True

    login_phrases = (
        "already registered",
        "first time here",
        "create account",
        "sign in",
    )
    has_login_text = any(phrase in body_text for phrase in login_phrases)
    has_password_field = page.locator("input[type='password']").count() > 0
    return has_login_text and has_password_field


def _scope_body_text(scope, *, limit: int = 5000) -> str:
    try:
        return scope.inner_text("body")[:limit].lower()
    except Exception:
        return ""


def _scope_locator_count(scope, selector: str) -> int:
    try:
        return scope.locator(selector).count()
    except Exception:
        return 0


def _scope_has_pre_application_auth_gate(scope) -> bool:
    if _scope_has_auth_markers(scope):
        return True
    body_text = _scope_body_text(scope)
    if "enter your information" in body_text and "data privacy" in body_text:
        return True
    if "data privacy" in body_text and "i confirm" in body_text:
        return (
            _scope_locator_count(
                scope,
                "#enterEmailSubmitButton, "
                "input[type='checkbox'][name*='gdpr' i], input[type='checkbox'][id*='gdpr' i], "
                "input[type='checkbox'][name*='privacy' i], input[type='checkbox'][id*='privacy' i], "
                "input[type='checkbox'][name*='consent' i], input[type='checkbox'][id*='consent' i]",
            )
            > 0
        )
    return False


def _page_has_embedded_pre_application_auth_gate(page) -> bool:
    if _scope_has_pre_application_auth_gate(page):
        return True
    return any(_scope_has_pre_application_auth_gate(scope) for scope in list(getattr(page, "frames", []) or []))


def _page_has_active_hcaptcha_challenge(page) -> bool:
    for scope in list(getattr(page, "frames", []) or []):
        try:
            scope_url = (scope.url or "").lower()
        except Exception:
            scope_url = ""
        if "hcaptcha" not in scope_url:
            continue
        if "frame=challenge" in scope_url or "/challenge" in scope_url:
            return True
        body_text = _scope_body_text(scope)
        if any(marker in body_text for marker in ("please try again", "verify", "i am human")):
            return True
    return False


def _scope_has_application_markers(scope) -> bool:
    if _scope_has_pre_application_auth_gate(scope):
        return False
    body_text = _scope_body_text(scope)
    if any(
        marker in body_text
        for marker in (
            "candidate profile",
            "additional data",
            "application questions",
            "screening questions",
            "review",
            "resume",
            "cover letter",
            "first name",
            "last name",
            "phone",
        )
    ):
        return True
    if any(
        _scope_locator_count(scope, selector) > 0
        for selector in (
            "input[type='file']",
            "label:has-text('Resume')",
            "label:has-text('Cover Letter')",
        )
    ):
        return True
    return _scope_locator_count(scope, "form input, form select, form textarea") >= 5


def _application_scope_score(scope) -> int:
    score = 0
    body_text = _scope_body_text(scope)
    if "candidate profile" in body_text:
        score += 8
    if "resume" in body_text:
        score += 4
    if "cover letter" in body_text:
        score += 4
    if any(token in body_text for token in ("first name", "last name", "phone", "additional data")):
        score += 2
    score += min(_scope_locator_count(scope, "input, select, textarea"), 10)
    if _scope_locator_count(scope, "input[type='file']") > 0:
        score += 3
    return score


def _active_application_scope(page):
    scopes: list[object] = []
    seen: set[int] = set()

    def remember(scope) -> None:
        marker = id(scope)
        if marker in seen:
            return
        seen.add(marker)
        scopes.append(scope)

    for scope in list(getattr(page, "frames", []) or []):
        if _scope_has_application_markers(scope):
            remember(scope)
    if _scope_has_application_markers(page):
        remember(page)
    if not scopes:
        return page
    return max(scopes, key=_application_scope_score)


def _is_application_page(page) -> bool:
    """Check if we're on an iCIMS application form page (past login).

    Look for common iCIMS application form indicators.
    """
    # Negative: still on login/registration page
    if _page_has_embedded_pre_application_auth_gate(page):
        return False

    url = page.url.lower()
    if "/login" in url and "icims.com" in url:
        body_text = page.inner_text("body")[:3000].lower()
        if "sign in" in body_text and "create account" in body_text:
            return False

    # Positive indicators
    # iCIMS application pages typically have file upload, text areas,
    # or specific iCIMS class names
    indicators = [
        "input[type='file']",
        ".iCIMS_MainWrapper",
        ".iCIMS_JobContent",
        "[class*='iCIMS']",
        "form[action*='icims']",
        # Common form field indicators
        "label:has-text('Resume')",
        "label:has-text('Cover Letter')",
    ]
    for selector in indicators:
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            continue

    # Check for form with multiple input fields (likely an application form).
    # Require form-specific labels — generic keywords like "experience" or
    # "phone" also appear on JD pages and cause false positives.
    try:
        form_inputs = page.locator("form input, form select, form textarea")
        if form_inputs.count() >= 3:
            form_labels = page.locator("form label").all_inner_texts()
            label_text = " ".join(form_labels).lower()
            if any(kw in label_text for kw in ("resume", "cover letter", "first name", "last name")):
                return True
    except Exception:
        pass

    return False


# ─── Page detection ──────────────────────────────────────────────────────────

PAGE_LOGIN = "login"
PAGE_PROFILE = "profile"
PAGE_APPLICATION = "application"
PAGE_DEMOGRAPHICS = "demographics"
PAGE_REVIEW = "review"
PAGE_CONFIRMATION = "confirmation"
PAGE_UNKNOWN = "unknown"


def _detect_current_page(page) -> str:
    """Detect which iCIMS wizard page is currently active."""
    url = page.url.lower()
    body_text = page.inner_text("body")[:5000].lower()

    # Check for confirmation first
    if any(p.search(body_text) for p in SUBMIT_CONFIRM_PATTERNS):
        return PAGE_CONFIRMATION

    # Login page — detect by content (password field + login phrases), not just URL
    if _page_has_embedded_pre_application_auth_gate(page):
        return PAGE_LOGIN
    if "/login" in url:
        if "sign in" in body_text or "create account" in body_text or "log in" in body_text:
            return PAGE_LOGIN

    # Look for page-specific headings/content
    headings = []
    try:
        heading_els = page.locator("h1, h2, h3").all()[:10]
        headings = [el.inner_text().strip().lower() for el in heading_els]
    except Exception:
        pass
    heading_text = " ".join(headings)

    # Review page
    if "review" in heading_text or "review" in body_text[:500]:
        if "submit" in body_text:
            return PAGE_REVIEW

    strong_profile_markers = any(
        kw in heading_text or kw in body_text[:2000]
        for kw in (
            "candidate profile",
            "upload resume",
            "upload your resume",
            "attach resume",
            "resume",
            "cover letter",
            "first name",
            "last name",
            "phone number",
            "phone",
        )
    )
    if strong_profile_markers:
        return PAGE_PROFILE

    # Demographics / EEO
    if any(
        kw in heading_text or kw in body_text[:1000]
        for kw in (
            "voluntary self-identification",
            "equal employment",
            "eeo",
            "demographics",
            "self-identify",
            "gender identity",
            "veteran status",
            "disability",
        )
    ):
        return PAGE_DEMOGRAPHICS

    # Application questions
    if any(
        kw in heading_text
        for kw in (
            "application questions",
            "questionnaire",
            "additional questions",
            "screening questions",
        )
    ):
        return PAGE_APPLICATION

    # Check for file upload (usually profile page)
    if page.locator("input[type='file']").count() > 0:
        return PAGE_PROFILE

    # Check for many form fields (application page)
    try:
        form_fields = page.locator("form input, form select, form textarea")
        if form_fields.count() >= 5:
            return PAGE_APPLICATION
    except Exception:
        pass

    return PAGE_UNKNOWN


# ─── Form filling helpers ────────────────────────────────────────────────────


def _fill_text_field(page, selector: str, value: str) -> bool:
    """Fill a text field found by selector."""
    field = page.locator(selector).first
    if not field.count():
        return False
    try:
        existing = field.input_value()
        if existing and existing.strip():
            return True  # Already filled
        field.click()
        field.fill("")
        human_fill(field, value)
        return True
    except Exception:
        return False


def _fill_by_label(page, label_text: str, value: str) -> bool:
    """Fill a form field by its label text."""
    try:
        fields = page.get_by_label(label_text, exact=False)
        candidates = []
        for index in range(fields.count()):
            candidate = fields.nth(index)
            try:
                if not candidate.is_visible():
                    continue
                if not candidate.is_enabled():
                    continue
            except Exception:
                continue
            candidates.append(candidate)

        if not candidates and fields.count():
            candidates.append(fields.first)

        for field in candidates:
            tag = field.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                # For select elements, try to select the option
                try:
                    field.select_option(label=value)
                    return True
                except Exception:
                    try:
                        field.select_option(value=value)
                        return True
                    except Exception:
                        continue
            existing = field.input_value()
            if existing and existing.strip():
                return True  # Already filled
            field.fill(value)
            return True
        return False
    except Exception:
        return False


def _select_dropdown(page, selector: str, value: str) -> bool:
    """Select an option from a dropdown."""
    dropdown = page.locator(selector).first
    if not dropdown.count():
        return False
    try:
        dropdown.select_option(label=value)
        return True
    except Exception:
        pass
    try:
        # Try partial match
        options = dropdown.locator("option").all()
        for opt in options:
            opt_text = opt.inner_text().strip()
            if value.lower() in opt_text.lower() or opt_text.lower() in value.lower():
                dropdown.select_option(label=opt_text)
                return True
    except Exception:
        pass
    return False


def _upload_file(page, file_path: str) -> bool:
    """Upload a file to the first visible file input."""
    file_input = page.locator("input[type='file']").first
    if not file_input.count():
        return False
    try:
        file_input.set_input_files(file_path)
        page.wait_for_timeout(3000)
        return True
    except Exception:
        return False


def _click_next_button(page) -> bool:
    """Click the Next/Continue/Save button to advance the wizard."""
    # Scroll toward the bottom so wizard navigation buttons become visible
    # (Phenom multi-step forms often have the Next button below the fold)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(500)

    for name in (
        "Next",
        "Continue",
        "Submit Profile",
        "Save and Continue",
        "Save & Continue",
        "Submit",
        "Proceed",
    ):
        btn = page.get_by_role("button", name=name)
        if btn.count() and btn.first.is_visible():
            try:
                btn.first.scroll_into_view_if_needed()
                btn.first.click()
                page.wait_for_timeout(3000)
                return True
            except Exception:
                continue
    # Fallback: <a> links styled as navigation buttons (common on Phenom sites)
    for text in ("Next", "Continue"):
        link = page.locator(f"a:has-text('{text}')").first
        if link.count() and link.is_visible():
            try:
                link.scroll_into_view_if_needed()
                link.click()
                page.wait_for_timeout(3000)
                return True
            except Exception:
                continue
    # Fallback: try input[type='submit'] or generic submit button
    submit_btn = page.locator("input[type='submit'], button[type='submit']").first
    if submit_btn.count() and submit_btn.is_visible():
        try:
            submit_btn.scroll_into_view_if_needed()
            submit_btn.click()
            page.wait_for_timeout(3000)
            return True
        except Exception:
            pass
    return False


def _click_submit_button(page) -> bool:
    """Click the Submit button on the review page."""
    for name in ("Submit", "Submit Application", "Apply", "Submit & Finish"):
        btn = page.get_by_role("button", name=name)
        if btn.count() and btn.first.is_visible():
            try:
                btn.first.click()
                page.wait_for_timeout(3000)
                return True
            except Exception:
                continue
    return False


# ─── Page-specific fill functions ────────────────────────────────────────────


def _fill_profile_page(
    page,
    profile,
    application_profile,
    out_dir: Path,
    *,
    login_email: str | None = None,
    login_password: str | None = None,
) -> list[dict]:
    """Fill the profile/personal info page.

    Common iCIMS profile fields:
    - Resume upload
    - Cover letter upload
    - First Name / Last Name
    - Email
    - Phone
    - Address / City / State / Zip
    - LinkedIn URL
    """
    filled = []

    # Upload resume
    try:
        resume_path = find_resume_file(out_dir)
        if _upload_file(page, str(resume_path)):
            filled.append(
                {
                    "field_name": "resume",
                    "label": "Resume",
                    "value": resume_path.name,
                    "source": "documents/",
                    "filled": True,
                }
            )
    except FileNotFoundError:
        print("iCIMS: resume file not found, skipping upload.", file=sys.stderr)

    # Upload cover letter (look for a second file input or one labeled cover letter)
    try:
        cover_letter_path = find_cover_letter_file(out_dir)
        # Try to find a cover letter-specific file input
        cl_input = page.locator(
            "input[type='file'][name*='cover'], "
            "input[type='file'][id*='cover'], "
            "input[type='file'][aria-label*='cover' i]"
        ).first
        if cl_input.count():
            cl_input.set_input_files(str(cover_letter_path))
            page.wait_for_timeout(2000)
            filled.append(
                {
                    "field_name": "cover_letter",
                    "label": "Cover Letter",
                    "value": cover_letter_path.name,
                    "source": "documents/",
                    "filled": True,
                }
            )
        else:
            # Try second file input
            all_file_inputs = page.locator("input[type='file']")
            if all_file_inputs.count() > 1:
                all_file_inputs.nth(1).set_input_files(str(cover_letter_path))
                page.wait_for_timeout(2000)
                filled.append(
                    {
                        "field_name": "cover_letter",
                        "label": "Cover Letter",
                        "value": cover_letter_path.name,
                        "source": "documents/",
                        "filled": True,
                    }
                )
    except FileNotFoundError:
        pass

    if login_email and _fill_by_label(page, "Login", login_email):
        filled.append(
            {
                "field_name": "login",
                "label": "Login",
                "value": login_email,
                "source": "application_profile.md",
                "filled": True,
            }
        )

    if login_password and _fill_by_label(page, "Password", login_password):
        filled.append(
            {
                "field_name": "login_password",
                "label": "Password",
                "value": "[redacted]",
                "source": "deterministic",
                "filled": True,
            }
        )

    if login_password and (
        _fill_by_label(page, "Password (Re-enter)", login_password)
        or _fill_by_label(page, "Confirm Password", login_password)
    ):
        filled.append(
            {
                "field_name": "login_password_reenter",
                "label": "Password (Re-enter)",
                "value": "[redacted]",
                "source": "deterministic",
                "filled": True,
            }
        )

    # First Name
    if _fill_by_label(page, "First Name", profile.first_name):
        filled.append(
            {
                "field_name": "first_name",
                "label": "First Name",
                "value": profile.first_name,
                "source": "master_resume.md",
                "filled": True,
            }
        )

    # Last Name
    if _fill_by_label(page, "Last Name", profile.last_name):
        filled.append(
            {
                "field_name": "last_name",
                "label": "Last Name",
                "value": profile.last_name,
                "source": "master_resume.md",
                "filled": True,
            }
        )

    # Email
    if _fill_by_label(page, "Email", profile.email):
        filled.append(
            {
                "field_name": "email",
                "label": "Email",
                "value": profile.email,
                "source": "master_resume.md",
                "filled": True,
            }
        )

    # Phone
    if profile.phone:
        phone = re.sub(r"^\+?1[\s-]?", "", profile.phone)
        if _fill_by_label(page, "Phone", phone):
            filled.append(
                {
                    "field_name": "phone",
                    "label": "Phone",
                    "value": phone,
                    "source": "master_resume.md",
                    "filled": True,
                }
            )

    # Address
    addr = getattr(application_profile, "street_address", "") or ""
    if addr:
        _fill_by_label(page, "Address", addr)
        _fill_by_label(page, "Street", addr)

    # Parse city/state from profile.location (e.g. "San Francisco, CA")
    _loc = profile.location or application_profile.location or ""
    _loc_parts = _loc.split(",") if _loc else []
    city = _loc_parts[0].strip() if _loc_parts else ""
    _state_abbr = _loc_parts[-1].strip() if len(_loc_parts) >= 2 else ""
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
    state_full = _STATE_MAP.get(_state_abbr, _state_abbr) or "California"

    city = city or "San Francisco"
    city_filled = _fill_by_label(page, "City", city)
    if not city_filled:
        # Fallback: CSS selector approach (Phenom pages may not use <label>)
        for sel in (
            "input[name*='city' i]",
            "input[id*='city' i]",
            "input[placeholder*='city' i]",
            "input[aria-label*='city' i]",
        ):
            if _fill_text_field(page, sel, city):
                city_filled = True
                break
    if city_filled:
        filled.append(
            {
                "field_name": "city",
                "label": "City",
                "value": city,
                "source": "master_resume.md",
                "filled": True,
            }
        )

    # State — try full name then abbreviation (for <select> dropdowns)
    state_filled = False
    for state_val in (state_full, _state_abbr):
        if not state_val:
            continue
        if _fill_by_label(page, "State", state_val):
            state_filled = True
            filled.append(
                {
                    "field_name": "state",
                    "label": "State",
                    "value": state_val,
                    "source": "master_resume.md",
                    "filled": True,
                }
            )
            break
    # Fallback: CSS selector approach for native <select> (Phenom-style pages)
    if not state_filled:
        state_selectors = [
            "select[id='cntryFields.region']",
            "select[id*='region' i]",
            "select[name*='state' i]",
            "select[id*='state' i]",
            "select[name*='region' i]",
            "select[aria-label*='state' i]",
        ]
        for sel in state_selectors:
            loc = page.locator(sel).first
            if loc.count():
                for sv in (state_full, _state_abbr):
                    if not sv:
                        continue
                    try:
                        loc.select_option(label=sv)
                        state_filled = True
                        filled.append(
                            {
                                "field_name": "state",
                                "label": "State",
                                "value": sv,
                                "source": "master_resume.md",
                                "filled": True,
                            }
                        )
                        break
                    except Exception:
                        pass
                if state_filled:
                    break

    # Postal Code
    zip_code = getattr(application_profile, "zip_code", "") or ""
    if not zip_code and _state_abbr == "CA" and "san francisco" in _loc.lower():
        zip_code = "94105"
    if zip_code:
        if _fill_by_label(page, "Zip", zip_code) or _fill_by_label(page, "Postal", zip_code):
            filled.append(
                {
                    "field_name": "postal_code",
                    "label": "Postal Code",
                    "value": zip_code,
                    "source": "master_resume.md",
                    "filled": True,
                }
            )

    # Phone Device Type — native <select> dropdown
    phone_type_selectors = [
        "select[id='deviceType']",
        "select[id*='deviceType' i]",
        "select[id*='device_type' i]",
        "select[name*='phoneDeviceType' i]",
        "select[name*='phone_device' i]",
        "select[id*='phoneDeviceType' i]",
        "select[id*='phone_type' i]",
        "select[aria-label*='device' i]",
    ]
    phone_type_filled = False
    for sel in phone_type_selectors:
        loc = page.locator(sel).first
        if loc.count():
            for candidate in ("Mobile", "Pers Mobile", "Home", "Main", "Other"):
                try:
                    loc.select_option(label=candidate)
                    filled.append(
                        {
                            "field_name": "phone_device_type",
                            "label": "Phone Device Type",
                            "value": candidate,
                            "source": "deterministic",
                            "filled": True,
                        }
                    )
                    phone_type_filled = True
                    break
                except Exception:
                    continue
            break
    # Fallback: find by label text
    if not phone_type_filled:
        for label_text in ("Phone Device Type", "Phone Type"):
            if _fill_by_label(page, label_text, "Mobile"):
                filled.append(
                    {
                        "field_name": "phone_device_type",
                        "label": "Phone Device Type",
                        "value": "Mobile",
                        "source": "deterministic",
                        "filled": True,
                    }
                )
                phone_type_filled = True
                break
    # Fallback: find select by nearby label element
    if not phone_type_filled:
        try:
            labels = page.locator("label:has-text('Phone Device Type'), label:has-text('Phone Type')")
            for i in range(labels.count()):
                label_el = labels.nth(i)
                for_id = label_el.get_attribute("for") or ""
                if for_id:
                    sel_el = page.locator(f"select#{for_id}").first
                else:
                    sel_el = label_el.locator("xpath=following::select[1]").first
                if sel_el.count():
                    for candidate in ("Mobile", "Pers Mobile", "Home", "Main", "Other"):
                        try:
                            sel_el.select_option(label=candidate)
                            filled.append(
                                {
                                    "field_name": "phone_device_type",
                                    "label": "Phone Device Type",
                                    "value": candidate,
                                    "source": "deterministic",
                                    "filled": True,
                                }
                            )
                            phone_type_filled = True
                            break
                        except Exception:
                            continue
                    break
        except Exception:
            pass

    # LinkedIn
    linkedin = getattr(application_profile, "linkedin", "") or getattr(profile, "linkedin", "") or ""
    if linkedin:
        _fill_by_label(page, "LinkedIn", linkedin)

    return filled


def _fill_profile_step(
    *,
    page,
    profile,
    application_profile,
    out_dir: Path,
    meta: dict,
    provider: str | None,
    login_email: str | None = None,
    login_password: str | None = None,
) -> list[dict]:
    filled = list(
        _fill_profile_page(
            page,
            profile,
            application_profile,
            out_dir,
            login_email=login_email,
            login_password=login_password,
        )
    )
    filled.extend(
        _fill_application_questions(
            page,
            out_dir,
            meta,
            provider,
            application_profile,
            allow_generated=False,
        )
    )
    return filled


_COMPENSATION_DEFLECT = (
    "I'm open and flexible on compensation. I'd prefer to learn more about "
    "the role's scope and total rewards package before discussing specific numbers. "
    "I'm confident we can find a mutually agreeable arrangement."
)


def _answer_from_classifier(
    label: str,
    application_profile,
    *,
    kind: str | None = None,
    options: list[str] | None = None,
) -> tuple[str, str] | None:
    """Use the unified question classifier to produce a deterministic answer.

    Returns (answer, source) tuple if the classifier identifies the question
    category and a deterministic answer can be produced, otherwise None.
    """
    category = classify_question(label)
    if category is None:
        return None

    policy = resolve_shared_question_policy(label, application_profile)
    if policy is not None and policy.category != "work_authorization" and policy.text_value is not None:
        matched = select_shared_policy_option(options, policy, application_profile=application_profile)
        return (matched or policy.text_value, policy.source)

    if category == "education":
        val = format_education_from_profile(application_profile)
        if val:
            return (val, "application_profile.md")
        return None
    if category == "compensation":
        return (_COMPENSATION_DEFLECT, "deterministic")
    if category == "nda_noncompete":
        return ("No", "deterministic")
    if category == "work_authorization":
        if kind in {"text", "textarea"}:
            value = build_truthful_work_authorization_answer(label, application_profile)
            if value:
                return (value, "application_profile.md")
        if policy is not None and policy.boolean_value is not None:
            return ("Yes" if policy.boolean_value else "No", policy.source)
        return (
            "Yes" if getattr(application_profile, "authorized_to_work_unconditionally", True) else "No",
            "application_profile.md",
        )
    if category == "city_location":
        val = getattr(application_profile, "location", None)
        if val:
            return (val, "application_profile.md")
        return None
    if category == "current_company":
        val = getattr(application_profile, "current_company", None)
        if val:
            return (val, "application_profile.md")
        return None
    if category == "culture_careers_optin":
        return ("No", "deterministic")
    if category == "product_usage":
        return ("Yes", "deterministic")
    if category == "interview_accommodation":
        return ("No", "deterministic")
    if category == "reasonable_accommodation":
        return ("Yes", "deterministic")
    if category == "salary_comfort":
        return (
            "Yes" if getattr(application_profile, "comfortable_with_posted_salary", True) else "No",
            "application_profile.md",
        )
    if category == "minimum_experience":
        return ("Yes", "application_profile.md")
    if category == "experience_confirmation":
        return ("Yes", "application_profile.md")
    if category == "office_attendance":
        return ("Yes", "application_profile.md")
    if category == "company_engagement":
        return ("Yes", "deterministic")
    return None


def _fill_application_questions(
    page,
    out_dir: Path,
    meta: dict,
    provider: str | None,
    application_profile,
    *,
    allow_generated: bool = True,
) -> list[dict]:
    """Fill custom application questions using deterministic answers and LLM fallback."""
    filled = []

    # Collect visible form fields
    # iCIMS uses various structures; look for label + input pairs
    question_specs = []
    field_elements = []

    # Strategy 1: look for label elements paired with inputs
    labels = page.locator("label").all()
    for label_el in labels:
        try:
            label_text = label_el.inner_text().strip()
            if not label_text or len(label_text) > 500:
                continue
            # Clean label
            label_text = re.sub(r"\s*\*\s*$", "", label_text).strip()
            if not label_text:
                continue

            # Find the associated input
            for_attr = label_el.get_attribute("for")
            if for_attr:
                input_el = page.locator(f"#{for_attr}").first
            else:
                # Look for input inside or after the label
                input_el = label_el.locator("input, select, textarea").first
                if not input_el.count():
                    parent = label_el.locator("..")
                    input_el = parent.locator("input, select, textarea").first

            if not input_el.count():
                continue

            tag = input_el.evaluate("el => el.tagName.toLowerCase()")
            input_type = input_el.get_attribute("type") or ""
            # Skip hidden, submit, file inputs (handled separately)
            if input_type in ("hidden", "submit", "button", "file"):
                continue

            kind = "text"
            if tag == "textarea":
                kind = "textarea"
            elif tag == "select":
                kind = "select"
            elif input_type == "radio":
                kind = "radio"
            elif input_type == "checkbox":
                kind = "checkbox"

            field_name = slugify_label(label_text)
            question_specs.append(
                {
                    "field_name": field_name,
                    "label": label_text,
                    "kind": kind,
                    "required": input_el.get_attribute("required") is not None,
                }
            )
            field_elements.append((label_text, input_el, kind))
        except Exception:
            continue

    if not question_specs:
        return filled

    # First pass: try deterministic answers via unified classifier
    requires_sponsorship = getattr(application_profile, "require_sponsorship_now", False) or getattr(
        application_profile, "require_sponsorship_future", False
    )

    llm_candidates = []
    for spec, (label_text, input_el, kind) in zip(question_specs, field_elements, strict=False):
        label_lower = label_text.lower()
        answered = False

        # Unified classifier dispatch
        classified = _answer_from_classifier(
            label_text,
            application_profile,
            kind=kind,
            options=_field_option_labels(page, input_el, kind),
        )
        if classified is not None:
            answer, source = classified
            if _fill_field_element(page, input_el, kind, answer):
                filled.append(
                    {
                        "field_name": spec["field_name"],
                        "label": label_text,
                        "value": answer,
                        "source": source,
                        "filled": True,
                    }
                )
                answered = True

        # --- iCIMS-specific fallbacks not covered by unified classifier ---

        # Sponsorship (yes/no based on profile)
        if not answered and any(kw in label_lower for kw in ("sponsorship", "sponsor", "visa")):
            answer = "Yes" if requires_sponsorship else "No"
            if _fill_field_element(page, input_el, kind, answer):
                filled.append(
                    {
                        "field_name": spec["field_name"],
                        "label": label_text,
                        "value": answer,
                        "source": "application_profile.md",
                        "filled": True,
                    }
                )
                answered = True

        # Former/current employee
        if not answered and any(
            kw in label_lower for kw in ("former employee", "previously employed", "current employee", "worked for")
        ):
            answer = "No"
            if _fill_field_element(page, input_el, kind, answer):
                filled.append(
                    {
                        "field_name": spec["field_name"],
                        "label": label_text,
                        "value": answer,
                        "source": "deterministic",
                        "filled": True,
                    }
                )
                answered = True

        # Relocate / on-site
        if not answered and any(kw in label_lower for kw in ("relocate", "commute", "on-site", "onsite", "in person")):
            answer = "Yes"
            if _fill_field_element(page, input_el, kind, answer):
                filled.append(
                    {
                        "field_name": spec["field_name"],
                        "label": label_text,
                        "value": answer,
                        "source": "application_profile.md",
                        "filled": True,
                    }
                )
                answered = True

        # How did you hear
        if not answered and ("how did you hear" in label_lower or "how did you learn" in label_lower):
            source_val = getattr(application_profile, "how_did_you_hear", "Corporate website") or "Corporate website"
            if _fill_field_element(page, input_el, kind, source_val):
                filled.append(
                    {
                        "field_name": spec["field_name"],
                        "label": label_text,
                        "value": source_val,
                        "source": "application_profile.md",
                        "filled": True,
                    }
                )
                answered = True

        if not answered:
            llm_candidates.append(spec)

    # Generate LLM answers for remaining questions
    if allow_generated and llm_candidates:
        llm_specs = [
            {
                "field_name": spec["field_name"],
                "label": spec["label"],
                "description": "",
                "required": spec["required"],
                "type": spec["kind"],
            }
            for spec in llm_candidates
        ]
        answers = generate_application_answers(
            out_dir=out_dir,
            meta=meta,
            question_specs=llm_specs,
            provider=provider,
        )

        # Fill LLM-generated answers
        for spec in llm_candidates:
            answer = answers.get(spec["field_name"], "")
            if not answer:
                continue
            # Find the corresponding element
            for label_text, input_el, kind in field_elements:
                if slugify_label(label_text) == spec["field_name"]:
                    if _fill_field_element(page, input_el, kind, answer):
                        filled.append(
                            {
                                "field_name": spec["field_name"],
                                "label": spec["label"],
                                "value": answer[:200],
                                "source": "generated",
                                "filled": True,
                            }
                        )
                    break

    return filled


def _field_option_labels(page, input_el, kind: str) -> list[str] | None:
    if kind == "select":
        try:
            options = [option.inner_text().strip() for option in input_el.locator("option").all()]
            return [option for option in options if option]
        except Exception:
            return None
    if kind == "radio":
        try:
            name_attr = input_el.get_attribute("name") or ""
            if not name_attr:
                return None
            labels: list[str] = []
            radios = page.locator(f"input[type='radio'][name='{name_attr}']")
            for index in range(radios.count()):
                radio = radios.nth(index)
                radio_id = radio.get_attribute("id") or ""
                if radio_id:
                    label = page.locator(f"label[for='{radio_id}']").first
                    if label.count():
                        text = label.inner_text().strip()
                        if text:
                            labels.append(text)
                            continue
                value = (radio.get_attribute("value") or "").strip()
                if value:
                    labels.append(value)
            return labels or None
        except Exception:
            return None
    return None


def _fill_field_element(page, input_el, kind: str, value: str) -> bool:
    """Fill a form field element with the given value."""
    try:
        if kind == "select":
            try:
                input_el.select_option(label=value)
                return True
            except Exception:
                pass
            # Try partial match
            try:
                options = input_el.locator("option").all()
                for opt in options:
                    opt_text = opt.inner_text().strip()
                    if value.lower() in opt_text.lower() or opt_text.lower() in value.lower():
                        input_el.select_option(label=opt_text)
                        return True
            except Exception:
                pass
            return False
        if kind == "radio":
            # For radio buttons, find the one with matching value/label
            name_attr = input_el.get_attribute("name") or ""
            if name_attr:
                radios = page.locator(f"input[type='radio'][name='{name_attr}']")
                for i in range(radios.count()):
                    radio = radios.nth(i)
                    radio_value = radio.get_attribute("value") or ""
                    if radio_value.lower() == value.lower():
                        radio.check(force=True)
                        return True
                    # Check label
                    radio_id = radio.get_attribute("id") or ""
                    if radio_id:
                        label = page.locator(f"label[for='{radio_id}']")
                        if label.count():
                            label_text = label.first.inner_text().strip().lower()
                            if value.lower() in label_text or label_text in value.lower():
                                radio.check(force=True)
                                return True
            return False
        if kind == "checkbox":
            if value.lower() in ("yes", "true", "1"):
                if not input_el.is_checked():
                    input_el.check(force=True)
            else:
                if input_el.is_checked():
                    input_el.uncheck(force=True)
            return True
        if kind == "textarea":
            input_el.fill(value)
            return True
        # text input
        existing = input_el.input_value()
        if existing and existing.strip():
            return True  # Already filled
        input_el.fill(value)
        return True
    except Exception as exc:
        print(f"iCIMS: failed to fill field ({kind}): {exc}", file=sys.stderr)
        return False


def _fill_demographics_page(page, application_profile) -> list[dict]:
    """Fill the voluntary self-identification / demographics page."""
    filled = []

    # Gender
    gender = getattr(application_profile, "gender", "")
    if gender:
        if _fill_by_label(page, "Gender", gender):
            filled.append({"field_name": "gender", "value": gender, "source": "application_profile.md", "filled": True})

    # Race / Ethnicity
    race = getattr(application_profile, "race_or_ethnicity", "")
    if race:
        if _fill_by_label(page, "Race", race) or _fill_by_label(page, "Ethnicity", race):
            filled.append(
                {"field_name": "race_ethnicity", "value": race, "source": "application_profile.md", "filled": True}
            )

    # Veteran Status
    veteran = getattr(application_profile, "veteran_status", "")
    if veteran:
        if _fill_by_label(page, "Veteran", veteran):
            filled.append(
                {"field_name": "veteran_status", "value": veteran, "source": "application_profile.md", "filled": True}
            )

    # Disability Status
    disability = getattr(application_profile, "disability_status", "")
    if disability:
        if _fill_by_label(page, "Disability", disability):
            filled.append(
                {
                    "field_name": "disability_status",
                    "value": disability,
                    "source": "application_profile.md",
                    "filled": True,
                }
            )

    # Try radio buttons and dropdowns for these fields as well
    _try_demographic_radio(page, "gender", gender, filled)
    _try_demographic_radio(page, "race", race, filled)
    _try_demographic_radio(page, "ethnicity", race, filled)
    _try_demographic_radio(page, "veteran", veteran, filled)
    _try_demographic_radio(page, "disability", disability, filled)

    return filled


def _try_demographic_radio(page, field_fragment: str, value: str, filled: list[dict]) -> None:
    """Try to select a radio/checkbox option matching the value."""
    if not value:
        return
    try:
        labels = page.locator(f"label:has-text('{value}')")
        if labels.count():
            labels.first.click()
            filled.append(
                {
                    "field_name": field_fragment,
                    "value": value,
                    "source": "application_profile.md",
                    "filled": True,
                }
            )
    except Exception:
        pass


# ─── Snapshot and state classification ───────────────────────────────────────


def _page_snapshot_fn(page) -> dict:
    """Capture a page snapshot for state detection."""
    # iCIMS doesn't use a specific captcha type consistently,
    # but we use hcaptcha as default (falls through in page_snapshot)
    from autofill_common import page_snapshot

    return page_snapshot(page, form_selector=FORM_SELECTOR, captcha_type="hcaptcha")


def _classify_submit_state(snapshot: dict) -> dict[str, object]:
    """Classify the current page state after submit."""
    page_text = str(snapshot.get("page_text") or "")
    if any(pattern.search(page_text) for pattern in SUBMIT_CONFIRM_PATTERNS):
        return {"status": "confirmed", "reason": "text"}
    errors = list(snapshot.get("errors") or [])
    page_level_errors = [
        pattern.search(page_text).group(0) for pattern in VALIDATION_ERROR_PATTERNS if pattern.search(page_text)
    ]
    combined_errors = list(dict.fromkeys(errors + page_level_errors))
    if combined_errors:
        return {"status": "validation_error", "errors": combined_errors}
    if snapshot.get("hcaptcha_challenge_active"):
        return {"status": "captcha_required"}
    return {"status": "pending"}


def _classify_auth_gate_state(snapshot: dict) -> dict[str, object]:
    if snapshot.get("hcaptcha_challenge_active"):
        return {"status": "captcha_required"}
    return {"status": "confirmed"}


def _classify_icims_auth_blocker(page, snapshot: dict | None = None) -> dict[str, object]:
    snapshot = snapshot or {}
    if _classify_auth_gate_state(snapshot)["status"] == "captcha_required" or _page_has_active_hcaptcha_challenge(page):
        return {
            "detail_status": "captcha_required",
            "submission_status": "skipped_captcha",
            "submission_failure_type": "skipped_captcha",
            "message": (
                "iCIMS authentication is blocked by a captcha challenge before sign in or account creation can continue."
            ),
            "suggestions": [
                "Rerun in a headed browser and solve the captcha when prompted.",
                "Then rerun the canonical output dir in --draft mode for fresh proof.",
            ],
            "auth_state": "captcha_required",
        }

    page_url = ""
    try:
        page_url = page.url or ""
    except Exception:
        pass
    page_text = ""
    html = ""
    try:
        page_text = page.inner_text("body")
    except Exception:
        pass
    try:
        html = page.content()
    except Exception:
        pass
    combined = "\n".join(part for part in (page_text, html) if part).casefold()
    normalized_url = page_url.casefold()
    if "/jobs/search" in normalized_url and "notfound=1" in normalized_url:
        return {
            "detail_status": "job_closed",
            "submission_status": "job_closed",
            "submission_failure_type": "job_closed",
            "message": "job_closed: The iCIMS application page resolved to a generic search shell instead of the target job posting.",
            "suggestions": [
                "Open the source careers page manually to confirm the posting is still listed.",
                "If the job is gone, archive the row instead of retrying sign-in.",
                "If the job moved, update the canonical URL and rerun the canonical output dir in --draft mode.",
            ],
            "auth_state": None,
        }
    if any(
        marker in combined
        for marker in (
            "502 error",
            "503 error",
            "504 error",
            "the request could not be satisfied",
            "generated by cloudfront",
            "origin closed the connection",
            "service unavailable",
            "temporarily unavailable",
            "try again later",
        )
    ):
        return {
            "detail_status": "service_unavailable",
            "submission_status": "service_unavailable",
            "submission_failure_type": "service_unavailable",
            "message": (
                "iCIMS did not reach a usable authentication form because the site returned an upstream error or "
                "temporary service outage."
            ),
            "suggestions": [
                "Wait for the automatic retry or rerun after the tenant recovers.",
                "Review the saved debug screenshot if the outage persists.",
            ],
            "auth_state": "service_unavailable",
        }

    return {
        "detail_status": "auth_failed",
        "submission_status": "auth_failed",
        "submission_failure_type": "auth_failed",
        "message": "iCIMS authentication failed. The account may not exist or the password may be incorrect.",
        "suggestions": [
            "Visit the iCIMS application page manually and try signing in.",
            "Create an account if you don't have one.",
            "Set ICIMS_PASSWORD in .env.local.",
            "Then rerun: uv run scripts/submit_application.py <out_dir> --submit",
        ],
        "auth_state": None,
    }


def _capture(page, path: Path, *, preferred_selectors: tuple[str, ...] = ()) -> None:
    selectors = (*preferred_selectors, *PREFERRED_CAPTURE_SELECTORS)
    capture_full_page(page, path, preferred_selectors=selectors)


def _is_confirmation_page(page) -> bool:
    text = page.inner_text("body")[:3000].lower()
    return any(p.search(text) for p in SUBMIT_CONFIRM_PATTERNS)


# ─── Payload building ───────────────────────────────────────────────────────


def _build_payload(out_dir: Path, provider: str | None = None) -> dict:
    """Build the autofill payload for an iCIMS application."""
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)
    profile = parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))

    job_url = str(meta.get("jd_source_resolved") or meta["jd_source"])
    company_proper = str(meta.get("company_proper") or meta.get("company") or "")
    jd_title = str(meta.get("jd_title") or "")

    # Find resume and cover letter files
    try:
        resume_path = str(find_resume_file(out_dir))
    except FileNotFoundError:
        resume_path = ""
    try:
        cover_letter_path = str(find_cover_letter_file(out_dir))
    except FileNotFoundError:
        cover_letter_path = ""

    constants = board_file_constants("icims")

    return {
        "board": "icims",
        "job_url": job_url,
        "application_url": _icims_application_url(job_url),
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
            "payload_json": str(role_submit_path(out_dir, constants["payload_json"])),
            "report_json": str(role_submit_path(out_dir, constants["report_json"])),
            "report_markdown": str(role_submit_path(out_dir, constants["report_md"])),
            "pre_submit_screenshot": str(role_submit_path(out_dir, constants["pre_submit_screenshot"])),
            "page_screenshots_dir": str(role_submit_path(out_dir, constants["page_screenshots_dir"])),
            "submit_debug_html": str(role_submit_path(out_dir, constants["submit_debug_html"])),
            "submit_debug_screenshot": str(role_submit_path(out_dir, constants["submit_debug_screenshot"])),
            "application_page_html": str(role_submit_path(out_dir, constants["application_page_html"])),
        },
    }


# ─── Custom browser pipeline ────────────────────────────────────────────────


def _run_icims_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    """Custom browser pipeline for iCIMS multi-page wizard."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: Playwright is not installed.", file=sys.stderr)
        return 1

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    job_url = payload.get("application_url") or payload.get("job_url", "")
    if looks_like_oracle_hcm_url(job_url):
        print(
            f"ERROR: URL is Oracle Cloud HCM, not iCIMS: {job_url}",
            file=sys.stderr,
        )
        return 1
    out_dir = Path(payload["out_dir"])
    meta = load_meta(out_dir)
    profile = parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    email, password = _icims_credentials()

    page_screenshots_dir = Path(payload["artifacts"]["page_screenshots_dir"])
    page_screenshots_dir.mkdir(parents=True, exist_ok=True)
    page_idx = 0

    all_filled: list[dict] = []

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
            purpose="iCIMS autofill",
        )
        page = browser.new_page(viewport=viewport, device_scale_factor=2)

        try:
            # --- Phase 1: Navigate to application URL ---
            application_url = payload.get("application_url", payload["job_url"])
            print(f"iCIMS: navigating to {application_url}", file=sys.stderr)
            page.goto(application_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)

            # Dismiss cookie consent and data privacy overlays before interacting
            _dismiss_cookie_banner(page)
            _dismiss_privacy_overlay(page)

            # If the URL doesn't have icims.com, try to discover the apply link
            if "icims.com" not in page.url.lower():
                icims_url = _discover_icims_url_from_page(page)
                if icims_url:
                    print(f"iCIMS: discovered application URL: {icims_url}", file=sys.stderr)
                    page.goto(icims_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3000)
                    # TalentBrew apply pages may show a fresh privacy overlay
                    _dismiss_cookie_banner(page)
                    _dismiss_privacy_overlay(page)
                else:
                    # Try clicking "Apply Now" button.
                    # TalentBrew sites may open the iCIMS form in a new tab
                    # (target="_blank"), so listen for popups.
                    apply_btn = page.locator(
                        "a:has-text('Apply Now'), a:has-text('Apply'), "
                        "button:has-text('Apply Now'), button:has-text('Apply')"
                    ).first
                    if apply_btn.count():
                        try:
                            with page.context.expect_page(timeout=8000) as new_page_info:
                                apply_btn.click()
                            new_tab = new_page_info.value
                            new_tab.wait_for_load_state("domcontentloaded")
                            new_tab.wait_for_timeout(3000)
                            page.close()
                            page = new_tab
                            print(f"iCIMS: followed Apply link to new tab: {page.url}", file=sys.stderr)
                        except Exception:
                            # No new tab opened — click happened in the same page
                            page.wait_for_timeout(5000)
                        # Clicking Apply may (re-)trigger the Data Privacy
                        # Agreement overlay on TalentBrew sites — dismiss it
                        # before continuing to the auth phase.
                        _dismiss_cookie_banner(page)
                        _dismiss_privacy_overlay(page)

            # --- Phase 2: Authenticate ---
            print("iCIMS: authenticating...", file=sys.stderr)
            if not _handle_auth(page, email, password):
                auth_snapshot = _page_snapshot_fn(page)
                auth_blocker = _classify_icims_auth_blocker(page, auth_snapshot)
                if auth_blocker["submission_status"] == "skipped_captcha":
                    print("iCIMS: auth step is blocked by captcha, waiting for resolution...", file=sys.stderr)
                    wait_result = wait_for_captcha_resolution(
                        page,
                        headless=headless,
                        payload=payload,
                        board_title="iCIMS",
                        classify_state_fn=_classify_auth_gate_state,
                        page_snapshot_fn=_page_snapshot_fn,
                        email_watcher=None,
                        confirmed_outcome_from_email_fn=None,
                        capture_fn=_capture,
                        submit_started_at_utc=datetime.now(UTC).replace(microsecond=0).isoformat(),
                    )
                    if wait_result["status"] == "confirmed" and _handle_auth(page, email, password):
                        print("iCIMS: authentication succeeded after captcha resolution.", file=sys.stderr)
                    else:
                        debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
                        _capture(page, debug_png)
                        _write_auth_outcome_log(
                            out_dir,
                            payload,
                            page,
                            detail_status=str(auth_blocker["detail_status"]),
                            message=str(auth_blocker["message"]),
                            suggestions=list(auth_blocker["suggestions"]),
                            submission_status=str(auth_blocker["submission_status"]),
                            submission_failure_type=str(auth_blocker["submission_failure_type"]),
                            auth_state=str(auth_blocker["auth_state"]),
                        )
                        return CAPTCHA_SKIP_EXIT_CODE
                else:
                    print(f"iCIMS: authentication blocked ({auth_blocker['detail_status']}).", file=sys.stderr)
                    debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
                    _capture(page, debug_png)
                    _write_auth_outcome_log(
                        out_dir,
                        payload,
                        page,
                        detail_status=str(auth_blocker["detail_status"]),
                        message=str(auth_blocker["message"]),
                        suggestions=list(auth_blocker["suggestions"]),
                        submission_status=str(auth_blocker["submission_status"]),
                        submission_failure_type=str(auth_blocker["submission_failure_type"]),
                        auth_state=(
                            None
                            if auth_blocker.get("auth_state") in (None, "")
                            else str(auth_blocker["auth_state"])
                        ),
                    )
                    return CAPTCHA_SKIP_EXIT_CODE  # Skip gracefully
            print("iCIMS: authentication succeeded.", file=sys.stderr)

            # --- Phase 3: Page-by-page form filling ---
            max_pages = 15  # Safety limit
            prev_page_type = None
            stuck_count = 0
            for page_attempt in range(max_pages):
                page.wait_for_timeout(2000)
                current_scope = _active_application_scope(page)
                current_page = _detect_current_page(current_scope)
                print(f"iCIMS: on page '{current_page}' (step {page_attempt + 1})", file=sys.stderr)

                # Stuck detection: same page type appearing repeatedly
                if current_page == prev_page_type:
                    stuck_count += 1
                else:
                    stuck_count = 0
                prev_page_type = current_page

                if stuck_count >= 3:
                    print(
                        f"iCIMS: stuck on '{current_page}' for {stuck_count + 1} consecutive iterations, aborting.",
                        file=sys.stderr,
                    )
                    debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
                    _capture(page, debug_png)
                    return 1

                # Screenshot each page
                page_idx += 1
                page_path = page_screenshots_dir / f"page_{page_idx:02d}_{current_page}.png"
                _capture(page, page_path)

                if current_page == PAGE_LOGIN:
                    if page_attempt >= 3:
                        print("iCIMS: auth failed after multiple attempts.", file=sys.stderr)
                        debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
                        _capture(page, debug_png)
                        return 1
                    _handle_auth(page, email, password)
                    continue

                if current_page == PAGE_CONFIRMATION:
                    # Already confirmed!
                    print("iCIMS: application confirmed!", file=sys.stderr)
                    snapshot = _page_snapshot_fn(page)
                    outcome = {"status": "confirmed", "reason": "text", "snapshot": snapshot}
                    sync_notion_after_submit(payload, outcome, provider="icims")
                    reply_to_confirmation_email(payload, board_name="icims")
                    return 0

                if current_page == PAGE_PROFILE:
                    filled = _fill_profile_step(
                        page=current_scope,
                        profile=profile,
                        application_profile=application_profile,
                        out_dir=out_dir,
                        meta=meta,
                        provider=payload.get("provider"),
                        login_email=email,
                        login_password=password,
                    )
                    all_filled.extend(filled)

                elif current_page == PAGE_APPLICATION:
                    filled = _fill_application_questions(
                        current_scope,
                        out_dir,
                        meta,
                        payload.get("provider"),
                        application_profile,
                    )
                    all_filled.extend(filled)

                elif current_page == PAGE_DEMOGRAPHICS:
                    filled = _fill_demographics_page(current_scope, application_profile)
                    all_filled.extend(filled)

                elif current_page == PAGE_REVIEW:
                    # Take pre-submit screenshot
                    pre_submit_path = Path(payload["artifacts"]["pre_submit_screenshot"])
                    _capture(page, pre_submit_path)

                    # Write report with filled fields
                    runtime_payload = dict(payload)
                    runtime_payload["steps"] = all_filled
                    write_report(runtime_payload, board_name="icims", runtime={"steps": all_filled})

                    if not submit:
                        print(
                            f"iCIMS: filled application for review: {pre_submit_path.relative_to(PROJECT_ROOT)}",
                            file=sys.stderr,
                        )
                        return 0

                    # Submit!
                    if not _click_submit_button(page):
                        print("iCIMS: submit button not found on review page.", file=sys.stderr)
                        return 1

                    # Poll for confirmation
                    submit_started_at_utc = datetime.now(UTC).replace(microsecond=0).isoformat()
                    email_watcher = build_email_confirmation_watcher(
                        payload,
                        min_received_at_utc=submit_started_at_utc,
                    )

                    for _ in range(30):
                        page.wait_for_timeout(500)
                        snapshot = _page_snapshot_fn(page)
                        email_confirmation = email_watcher.poll()
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
                                provider="icims",
                                email_confirmation=email_confirmation,
                                min_received_at_utc=submit_started_at_utc,
                            )
                            reply_to_confirmation_email(
                                payload,
                                board_name="icims",
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
                                provider="icims",
                                min_received_at_utc=submit_started_at_utc,
                            )
                            reply_to_confirmation_email(payload, board_name="icims")
                            return 0
                        if state["status"] == "captcha_required":
                            print("iCIMS submission: captcha detected, waiting for resolution...", file=sys.stderr)
                            _wait_result = wait_for_captcha_resolution(
                                page,
                                headless=headless,
                                payload=payload,
                                board_title="iCIMS",
                                classify_state_fn=_classify_submit_state,
                                page_snapshot_fn=_page_snapshot_fn,
                                email_watcher=email_watcher,
                                confirmed_outcome_from_email_fn=None,
                                capture_fn=_capture,
                                submit_started_at_utc=submit_started_at_utc,
                            )
                            if _wait_result["status"] == "confirmed":
                                _outcome = _wait_result.get("outcome", {})
                                _email_conf = _wait_result.get("email_confirmation")
                                sync_notion_after_submit(
                                    payload,
                                    _outcome,
                                    provider="icims",
                                    email_confirmation=_email_conf,
                                    min_received_at_utc=submit_started_at_utc,
                                )
                                reply_to_confirmation_email(
                                    payload,
                                    board_name="icims",
                                    email_confirmation=_email_conf,
                                )
                                return 0
                            return CAPTCHA_SKIP_EXIT_CODE
                        if state["status"] == "validation_error":
                            # Wait and re-check for transient errors
                            page.wait_for_timeout(2000)
                            snapshot = _page_snapshot_fn(page)
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
                                    provider="icims",
                                    min_received_at_utc=submit_started_at_utc,
                                )
                                reply_to_confirmation_email(payload, board_name="icims")
                                return 0
                            break

                    # Final email check
                    email_confirmation = email_watcher.poll(force=True)
                    if email_confirmation:
                        outcome = {
                            "status": "confirmed",
                            "reason": "email_confirmation",
                            "snapshot": _page_snapshot_fn(page),
                            "email_confirmation": email_confirmation,
                        }
                        sync_notion_after_submit(
                            payload,
                            outcome,
                            provider="icims",
                            email_confirmation=email_confirmation,
                            min_received_at_utc=submit_started_at_utc,
                        )
                        reply_to_confirmation_email(
                            payload,
                            board_name="icims",
                            email_confirmation=email_confirmation,
                        )
                        return 0

                    debug_html = Path(payload["artifacts"]["submit_debug_html"])
                    debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
                    debug_html.write_text(page.content(), encoding="utf-8")
                    _capture(page, debug_png)
                    print(
                        f"iCIMS submit did not reach confirmed state. "
                        f"See {debug_html.relative_to(PROJECT_ROOT)} and "
                        f"{debug_png.relative_to(PROJECT_ROOT)}.",
                        file=sys.stderr,
                    )
                    return 1

                # Click Next to proceed
                if current_page not in (PAGE_REVIEW, PAGE_CONFIRMATION):
                    if not _click_next_button(current_scope):
                        # Maybe we're already on confirmation
                        if _is_confirmation_page(page):
                            snapshot = _page_snapshot_fn(page)
                            state = _classify_submit_state(snapshot)
                            if state["status"] == "confirmed":
                                outcome = {
                                    "status": "confirmed",
                                    "reason": "text",
                                    "snapshot": snapshot,
                                }
                                sync_notion_after_submit(
                                    payload,
                                    outcome,
                                    provider="icims",
                                )
                                reply_to_confirmation_email(payload, board_name="icims")
                                return 0
                        print("iCIMS: could not find Next button.", file=sys.stderr)
                        debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
                        _capture(page, debug_png)
                        return 1

            # Exhausted max pages
            print("iCIMS: exceeded max page attempts.", file=sys.stderr)
            return 1

        finally:
            browser.close()


def _write_auth_outcome_log(
    out_dir: Path,
    payload: dict,
    page,
    *,
    detail_status: str,
    message: str,
    suggestions: list[str],
    submission_status: str,
    submission_failure_type: str,
    auth_state: str | None = None,
) -> None:
    """Write a user-facing auth artifact plus pipeline result for iCIMS blockers."""
    submit_dir = role_submit_path(out_dir, "")
    submit_dir.mkdir(parents=True, exist_ok=True)
    log_path = submit_dir / "icims_auth_failure.json"
    page_text = page.inner_text("body")[:1000] if page else ""
    auth_scope = icims_auth_scope((page.url if page else "") or payload.get("job_url", ""))
    result = {
        "status": detail_status,
        "board": "icims",
        "job_url": payload.get("job_url", ""),
        "company": payload.get("company", ""),
        "job_title": payload.get("job_title", ""),
        "email": payload.get("candidate_email", ""),
        "page_url": page.url if page else "",
        "page_text_excerpt": page_text,
        "updated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "message": message,
        "suggestions": suggestions,
    }
    if auth_state:
        result["auth_state"] = auth_state
    if auth_scope:
        result["auth_scope"] = auth_scope
    log_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    try:
        log_ref = log_path.relative_to(PROJECT_ROOT)
    except ValueError:
        log_ref = log_path
    print(f"iCIMS auth failure details: {log_ref}", file=sys.stderr)

    # Write submission result for pipeline tracking
    submission_result = {
        "status": submission_status,
        "website_confirmed": False,
        "provider": "icims",
        "board": "icims",
        "job_url": payload.get("job_url", ""),
        "updated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "message": result["message"],
        "failure_type": submission_failure_type,
        "artifacts": {
            "auth_sidecar": str(log_path),
        },
    }
    if auth_state:
        submission_result["auth_state"] = auth_state
    if auth_scope:
        submission_result["auth_scope"] = auth_scope
    result_path = submit_dir / "application_submission_result.json"
    result_path.write_text(json.dumps(submission_result, indent=2) + "\n", encoding="utf-8")


def _write_auth_failure_log(out_dir: Path, payload: dict, page) -> None:
    """Backward-compatible wrapper for credential/auth failures."""
    _write_auth_outcome_log(
        out_dir,
        payload,
        page,
        detail_status="auth_failed",
        message="iCIMS authentication failed. The account may not exist or the password may be incorrect.",
        suggestions=[
            "Visit the iCIMS application page manually and try signing in.",
            "Create an account if you don't have one.",
            "Set ICIMS_PASSWORD in .env.local.",
            "Then rerun: uv run scripts/submit_application.py <out_dir> --submit",
        ],
        submission_status="auth_failed",
        submission_failure_type="auth_failed",
    )


# ─── Entry point ─────────────────────────────────────────────────────────────


def main() -> int:
    return autofill_main(
        board_name="icims",
        build_payload_fn=_build_payload,
        run_browser_fn=_run_icims_browser,
    )


if __name__ == "__main__":
    raise SystemExit(main())
