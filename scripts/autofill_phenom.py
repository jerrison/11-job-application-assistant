#!/usr/bin/env python3
"""Generate and optionally run a Phenom application autofill flow.

Phenom uses a multi-page wizard (6 steps), so this board script uses
``autofill_main`` with a custom ``run_browser_fn`` instead of the shared
``run_browser_pipeline`` (which assumes a single-form model).

Phenom ATS structure (observed on HPE, Adobe, Genentech, Circle, etc.):
  Step 1: My information (personal info, resume upload, source, consent)
  Step 2: My experience (work history, education)
  Step 3: Application questions (custom questions per job)
  Step 4: Voluntary Disclosures (OFCCP demographics)
  Step 5: Self identity (additional demographics)
  Step 6: Review (summary, submit)
"""

from __future__ import annotations

import json
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
    write_submission_result,
)
from autofill_common import (
    board_file_constants,
    capture_full_page,
    page_snapshot,
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
from output_layout import migrate_role_output_layout, role_submit_path
from project_env import load_project_env
from question_classifier import classify_question

_BOARD_CONSTANTS = board_file_constants("phenom")
AUTOFILL_PAYLOAD_JSON = _BOARD_CONSTANTS["payload_json"]
APPLICATION_PAGE_HTML = _BOARD_CONSTANTS["application_page_html"]

SUBMIT_CONFIRM_PATTERNS = (
    re.compile(r"\bthank(?:s| you)\s+for\s+applying\b", re.I),
    re.compile(r"\bthank you,?\s*$", re.I | re.M),
    re.compile(r"\balready applied\b", re.I),
    re.compile(r"\bapplication (?:has been )?(?:received|submitted)\b", re.I),
    re.compile(r"\byour application has been submitted\b", re.I),
    re.compile(r"\bsubmitted successfully\b", re.I),
    re.compile(r"\bsuccessfully submitted\b", re.I),
    re.compile(r"\bsuccessfully applied\b", re.I),
    re.compile(r"\bapplication is processing\b", re.I),
    re.compile(r"\bwe(?:'|')ll be in touch\b", re.I),
    re.compile(r"\bwe(?:'|')ve received your application\b", re.I),
)
VALIDATION_ERROR_PATTERNS = (
    re.compile(r"\bthis field is required\b", re.I),
    re.compile(r"\bplease (?:enter|select|complete|fill)\b", re.I),
    re.compile(r"\binvalid\b", re.I),
)
PREFERRED_CAPTURE_SELECTORS = (".application-container", "main", ".content-wrapper")


load_project_env()


# ─── Page constants ───────────────────────────────────────────────────────────

PAGE_MY_INFO = "my_information"
PAGE_EXPERIENCE = "my_experience"
PAGE_APPLICATION_QUESTIONS = "application_questions"
PAGE_VOLUNTARY_DISCLOSURES = "voluntary_disclosures"
PAGE_SELF_IDENTITY = "self_identity"
PAGE_REVIEW = "review"
PAGE_UNKNOWN = "unknown"


# ─── URL helpers ──────────────────────────────────────────────────────────────


def _phenom_application_url(job_url: str) -> str:
    """Construct the Phenom application URL from a JD URL.

    Phenom JD URLs look like:
      https://careers.{company}.com/us/en/job/{id}/{slug}
    Application URLs look like:
      https://careers.{company}.com/us/en/apply?jobSeqNo={id}

    If the URL already has ``/apply`` or ``jobSeqNo``, return as-is.
    """
    from urllib.parse import urlencode, urlparse, urlunparse

    parsed = urlparse(job_url)
    path = parsed.path.rstrip("/")

    # Already an apply URL
    if "/apply" in path or "jobSeqNo" in parsed.query:
        return job_url

    # Extract jobSeqNo from /job/{id}/... path pattern
    # Phenom job IDs can be numeric (e.g. "1201851") or alphanumeric
    # (e.g. "GENEUS202508120667EXTERNALENUS")
    match = re.search(r"/job/([A-Za-z0-9_-]+)", path)
    if match:
        seq_no = match.group(1)
        # Build apply path: replace /job/{id}/... with /apply
        base_path = path[: match.start()] + "/apply"
        query = urlencode({"jobSeqNo": seq_no})
        return urlunparse(parsed._replace(path=base_path, query=query))

    # Fallback: return original URL (let the browser handle it)
    return job_url


# ─── Page detection ───────────────────────────────────────────────────────────


def _detect_current_page(page) -> str:
    """Detect which Phenom wizard page is currently active."""
    # Check URL stepname parameter first (most reliable)
    from urllib.parse import parse_qs, urlparse

    current_url = page.url
    parsed = urlparse(current_url)
    params = parse_qs(parsed.query)
    stepname = (params.get("stepname") or [""])[0].lower()

    if stepname == "personalinformation":
        return PAGE_MY_INFO
    if stepname == "myexperience":
        return PAGE_EXPERIENCE
    if stepname == "applicationquestions":
        return PAGE_APPLICATION_QUESTIONS
    if stepname in ("voluntarydisclosures", "voluntaryselfdisclosure"):
        return PAGE_VOLUNTARY_DISCLOSURES
    if stepname in ("selfidentity", "selfidentify", "selfidentification"):
        return PAGE_SELF_IDENTITY
    if stepname == "review":
        return PAGE_REVIEW

    # Fallback: check visible headings and step indicators
    body_text = ""
    try:
        body_text = page.inner_text("body")[:5000].lower()
    except Exception:
        pass

    # Check for active step in the toolbar
    active_step = page.locator(".step-active, .active-step, [aria-current='step'], .stepper-item.active")
    if active_step.count():
        step_text = active_step.first.inner_text().strip().lower()
        if "information" in step_text or "personal" in step_text:
            return PAGE_MY_INFO
        if "experience" in step_text:
            return PAGE_EXPERIENCE
        if "question" in step_text:
            return PAGE_APPLICATION_QUESTIONS
        if "disclosure" in step_text or "voluntary" in step_text:
            return PAGE_VOLUNTARY_DISCLOSURES
        if "identity" in step_text or "self" in step_text:
            return PAGE_SELF_IDENTITY
        if "review" in step_text:
            return PAGE_REVIEW

    # Fallback: check body text content
    if "my information" in body_text or ("email" in body_text and "phone" in body_text and "privacy" in body_text):
        return PAGE_MY_INFO
    if "my experience" in body_text or "work experience" in body_text:
        return PAGE_EXPERIENCE
    if "application question" in body_text:
        return PAGE_APPLICATION_QUESTIONS
    if "voluntary" in body_text and ("disclosure" in body_text or "self" in body_text):
        return PAGE_VOLUNTARY_DISCLOSURES
    if "self identity" in body_text or "self-identity" in body_text or "self identification" in body_text:
        return PAGE_SELF_IDENTITY
    if "review" in body_text and "submit" in body_text:
        return PAGE_REVIEW

    return PAGE_UNKNOWN


# ─── Form filling helpers ─────────────────────────────────────────────────────


def _is_placeholder_option(text: str) -> bool:
    """Check if a select option text is a placeholder (not a real value)."""
    t = text.strip().lower()
    return (
        not t
        or t.startswith(("please", "select"))
        or t in ("--", "---", "- select -", "-- select --", "choose", "choose one")
    )


def _normalize_option_text(text: str) -> str:
    """Normalize select option text for case-insensitive matching."""

    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def _candidate_name_parts(full_name: str) -> tuple[str, str]:
    """Split a full name into first and last components for required Phenom fields."""

    first_name, _, last_name = full_name.strip().partition(" ")
    return first_name, last_name.strip()


def _best_select_option_label(option_labels: list[str], candidates: list[str] | tuple[str, ...]) -> str | None:
    """Choose the best live option label for a prioritized candidate list.

    Matching strategy (in priority order):
    1. Candidate-order exact normalized match
    2. Candidate-order prefix match
    3. Candidate-order contains / token-subset match
    """

    live_options = [
        (label.strip(), _normalize_option_text(label))
        for label in option_labels
        if label and not _is_placeholder_option(label)
    ]
    if not live_options:
        return None

    normalized_candidates = [
        (candidate.strip(), _normalize_option_text(candidate))
        for candidate in candidates
        if candidate and candidate.strip()
    ]

    for _, normalized_candidate in normalized_candidates:
        if not normalized_candidate:
            continue
        for label, normalized_label in live_options:
            if normalized_label == normalized_candidate:
                return label

    best_label: str | None = None
    best_score: tuple[int, int, int] | None = None
    for candidate_index, (_, normalized_candidate) in enumerate(normalized_candidates):
        if not normalized_candidate:
            continue
        candidate_words = normalized_candidate.split()
        for label, normalized_label in live_options:
            if normalized_label.startswith(normalized_candidate):
                score = (candidate_index, 0, abs(len(normalized_label) - len(normalized_candidate)))
            elif normalized_candidate in normalized_label:
                score = (candidate_index, 1, abs(len(normalized_label) - len(normalized_candidate)))
            elif candidate_words and all(word in normalized_label for word in candidate_words):
                score = (candidate_index, 2, abs(len(normalized_label) - len(normalized_candidate)))
            else:
                continue
            if best_score is None or score < best_score:
                best_score = score
                best_label = label

    return best_label


def _select_locator_option_from_candidates(select, candidates: list[str] | tuple[str, ...]) -> str | None:
    """Resolve a prioritized candidate list against the live option labels once."""

    try:
        option_labels = [
            option.inner_text().strip()
            for option in select.locator("option").all()
            if (option.get_attribute("value") or "").strip()
        ]
    except Exception:
        return None

    chosen_label = _best_select_option_label(option_labels, candidates)
    if not chosen_label:
        return None

    try:
        select.select_option(label=chosen_label)
    except Exception:
        return None
    return chosen_label


def _fill_native_select(page, selector: str, value: str) -> bool:
    """Fill a native <select> dropdown."""

    select = page.locator(selector).first
    if not select.count():
        return False
    return _select_locator_option_from_candidates(select, [value]) is not None


def _fill_text_field(page, selector: str, value: str) -> bool:
    """Fill a text input field."""
    field = page.locator(selector).first
    if not field.count():
        return False
    try:
        field.click()
        field.fill("")
        human_fill(field, value)
        return True
    except Exception:
        return False


def _upload_file_with_chooser(page, button_selector: str, file_path: str) -> bool:
    """Upload a file by clicking a button and handling the file chooser.

    Phenom uses a styled "Upload Resume" button that triggers a hidden
    file input or a file chooser dialog.
    """
    # First try: look for a hidden file input near the button
    file_input = page.locator("input[type='file']").first
    if file_input.count():
        try:
            file_input.set_input_files(file_path)
            page.wait_for_timeout(3000)
            return True
        except Exception:
            pass

    # Second try: click the button and handle file chooser
    btn = page.locator(button_selector).first
    if not btn.count():
        return False
    try:
        with page.expect_file_chooser(timeout=5000) as fc_info:
            btn.click()
        file_chooser = fc_info.value
        file_chooser.set_files(file_path)
        page.wait_for_timeout(3000)
        return True
    except Exception:
        return False


def _check_checkbox(page, selector: str, *, checked: bool = True) -> bool:
    """Check or uncheck a checkbox."""
    cb = page.locator(selector).first
    if not cb.count():
        return False
    try:
        if checked:
            cb.check(force=True)
        else:
            cb.uncheck(force=True)
        return True
    except Exception:
        try:
            cb.evaluate(
                """(el, shouldCheck) => {
                    el.checked = shouldCheck;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                checked,
            )
            return True
        except Exception:
            return False


def _dismiss_popups(page) -> None:
    """Dismiss feedback surveys, cookie banners, and Qualtrics popups."""
    for popup_sel in (
        "button:has-text('Accept All')",
        "button:has-text('Accept Cookies')",
        "#onetrust-accept-btn-handler",
        "[class*='QSIPopOver'] button[class*='close']",
        "[class*='QSIPopOver'] [class*='close']",
        "[id*='QSI'] button",
        "button:has-text('×')",
        "button[aria-label='Close']",
        "[class*='feedback'] button[class*='close']",
        "[class*='survey'] button[class*='close']",
        "[class*='cookie'] button[class*='accept' i]",
        "[class*='consent'] button[class*='accept' i]",
    ):
        try:
            popup_btn = page.locator(popup_sel)
            if popup_btn.count() and popup_btn.first.is_visible():
                popup_btn.first.click(timeout=1000)
                page.wait_for_timeout(500)
                return
        except Exception:
            pass

    # Qualtrics "How would you rate..." popup — dismiss by pressing Escape
    # or clicking outside
    try:
        qualtrics = page.locator("[class*='QSI'], [id*='QSI']")
        if qualtrics.count() and qualtrics.first.is_visible():
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            return
    except Exception:
        pass

    # Generic: hide any visible popup/overlay via JS
    try:
        page.evaluate("""() => {
            const popups = document.querySelectorAll(
                '[class*="QSIPopOver"], [class*="feedback-popup"], [class*="survey-popup"], [id*="QSI"]'
            );
            for (const p of popups) {
                p.style.display = 'none';
            }
        }""")
    except Exception:
        pass


def _click_next_button(page) -> bool:
    """Click the Next button on the current wizard page."""
    # Phenom Next button selectors
    for selector in (
        "button:has-text('Next')",
        "button.next-btn",
        "button[data-ph-at-id='next-button']",
        "button[data-ph-at-id='apply-next-btn']",
        "[class*='next'] button",
        "button:has-text('Save and Continue')",
        "button:has-text('Continue')",
    ):
        btn = page.locator(selector)
        if btn.count() and btn.first.is_visible():
            try:
                btn.first.click(force=True)
                page.wait_for_timeout(3000)
                return True
            except Exception:
                continue
    return False


def _click_submit_button(page) -> bool:
    """Click the Submit button on the review page."""
    for selector in (
        "button:has-text('Submit')",
        "button:has-text('Submit Application')",
        "button[data-ph-at-id='submit-button']",
        "button[data-ph-at-id='apply-submit-btn']",
        "button.submit-btn",
    ):
        btn = page.locator(selector)
        if btn.count() and btn.first.is_visible():
            try:
                btn.first.click(force=True)
                page.wait_for_timeout(3000)
                return True
            except Exception:
                continue
    return False


def _capture(page, path: Path) -> None:
    """Capture a screenshot."""
    capture_full_page(page, path, preferred_selectors=PREFERRED_CAPTURE_SELECTORS)


def _page_snapshot_fn(page) -> dict:
    """Take a page snapshot for state detection."""
    return page_snapshot(page, form_selector="form, .application-container, main", captcha_type="recaptcha")


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
    invalid_fields = list(snapshot.get("invalid_fields") or [])

    if snapshot.get("recaptcha_challenge_active"):
        return {"status": "captcha_required"}

    if combined_errors:
        return {
            "status": "validation_error",
            "errors": combined_errors,
            "invalid_fields": invalid_fields,
        }

    return {"status": "pending"}


def _is_confirmation_page(page) -> bool:
    """Check if the current page is a submission confirmation."""
    # URL-based detection: talent community redirect means app was submitted
    url_lower = page.url.lower()
    if "talentcommunity" in url_lower or "talent-community" in url_lower:
        return True
    try:
        text = page.inner_text("body")[:3000].lower()
    except Exception:
        return False
    return any(p.search(text) for p in SUBMIT_CONFIRM_PATTERNS)


def _terminal_apply_submit_result_from_body(body: str) -> dict[str, str] | None:
    """Interpret terminal applySubmit responses that should stop Phenom retries."""

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None

    apply_submit = payload.get("applySubmit")
    if not isinstance(apply_submit, dict):
        return None
    response = apply_submit.get("response")
    if not isinstance(response, dict):
        return None

    status_code = str(response.get("statusCode") or "").strip().casefold()
    if "captcha" not in status_code:
        return None

    return {
        "status": "skipped_captcha",
        "failure_type": "skipped_captcha",
        "message": "Phenom application is blocked by a captcha challenge before the next step can load.",
    }


def _detect_phenom_auth_result(page, payload: dict[str, object]) -> dict[str, object] | None:
    try:
        body_text = re.sub(r"\s+", " ", page.locator("body").inner_text(timeout=5000)).strip().casefold()
    except Exception:
        return None
    if not body_text:
        return None

    has_sign_in_gate = "sign in" in body_text and "create account" in body_text
    has_identity_provider = any(
        fragment in body_text
        for fragment in (
            "continue with google",
            "continue with linkedin",
            "continue with apple",
            "sign in with google",
            "sign in with linkedin",
        )
    )
    if not (has_sign_in_gate and has_identity_provider):
        return None

    from urllib.parse import urlparse

    job_url = str(payload.get("job_url") or page.url or "").strip()
    host = urlparse(job_url).netloc or "unknown"
    return {
        "status": "skipped_auth",
        "failure_type": "auth_guarded",
        "auth_state": "sign_in_gate",
        "auth_scope": f"phenom:{host}",
        "message": "Phenom requires sign in or account creation before the application form is available.",
        "job_url": job_url,
        "company": str(payload.get("company") or "").strip(),
        "job_title": str(payload.get("job_title") or "").strip(),
    }


def _record_phenom_terminal_result(page, payload: dict[str, object], out_dir: Path, result: dict[str, object]) -> None:
    debug_html = Path(payload["artifacts"]["submit_debug_html"])
    debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
    debug_html.write_text(page.content(), encoding="utf-8")
    _capture(page, debug_png)
    write_submission_result(
        out_dir=out_dir,
        status=str(result["status"]),
        job_url=str(result.get("job_url") or page.url or "").strip(),
        message=str(result["message"]),
        failure_type=str(result["failure_type"]),
        auth_state=str(result["auth_state"]) if result.get("auth_state") else None,
        auth_scope=str(result["auth_scope"]) if result.get("auth_scope") else None,
        board=str(payload.get("board") or "phenom"),
        provider=str(payload.get("provider") or "").strip() or None,
        artifacts={
            "submit_debug_html": str(debug_html),
            "submit_debug_screenshot": str(debug_png),
        },
    )


# ─── Page-specific fill functions ────────────────────────────────────────────


def _fill_my_information(page, profile, application_profile, out_dir: Path) -> list[dict]:
    """Fill the My Information page (Step 1).

    Fields observed on Phenom:
    - Upload Resume (file upload)
    - Country (native select, usually pre-selected)
    - Email address (text input, required)
    - Phone Device Type (native select)
    - Country Phone Code (native select, usually +1)
    - Phone number (text input, required)
    - Source (native select)
    - Text message consent (checkbox)
    - Privacy consent checkbox (required)
    """
    filled = []

    # Dismiss any popups (feedback surveys, cookie banners, Qualtrics, etc.)
    _dismiss_popups(page)

    # Resume upload
    try:
        resume_path = find_resume_file(out_dir)
        if _upload_file_with_chooser(
            page,
            "button:has-text('Upload Resume'), button:has-text('Upload'), [class*='upload'] button",
            str(resume_path),
        ):
            filled.append(
                {
                    "field_name": "resume",
                    "value": str(resume_path.name),
                    "source": "documents/",
                    "filled": True,
                }
            )
            # Wait for resume parsing
            page.wait_for_timeout(3000)
    except FileNotFoundError:
        print("Phenom: resume file not found, skipping upload.", file=sys.stderr)

    first_name, last_name = _candidate_name_parts(profile.full_name)
    resolved_last_name = last_name or first_name

    for sel in (
        "input[name='cntryFields.firstName']",
        "input[id='cntryFields.firstName']",
        "input[name*='firstName' i]",
        "input[id*='firstName' i]",
        "input[aria-label*='first name' i]",
    ):
        if first_name and _fill_text_field(page, sel, first_name):
            filled.append(
                {
                    "field_name": "first_name",
                    "value": first_name,
                    "source": "master_resume.md",
                    "filled": True,
                }
            )
            break

    for sel in (
        "input[name='cntryFields.lastName']",
        "input[id='cntryFields.lastName']",
        "input[name*='lastName' i]",
        "input[id*='lastName' i]",
        "input[aria-label*='last name' i]",
    ):
        if resolved_last_name and _fill_text_field(page, sel, resolved_last_name):
            filled.append(
                {
                    "field_name": "last_name",
                    "value": resolved_last_name,
                    "source": "master_resume.md",
                    "filled": True,
                }
            )
            break

    # Email address
    email_selectors = [
        "input[name*='email' i]",
        "input[type='email']",
        "input[id*='email' i]",
        "input[placeholder*='email' i]",
        "input[aria-label*='email' i]",
    ]
    for sel in email_selectors:
        if _fill_text_field(page, sel, profile.email):
            filled.append(
                {
                    "field_name": "email",
                    "value": profile.email,
                    "source": "master_resume.md",
                    "filled": True,
                }
            )
            break

    # Address fields — some Phenom instances (HPE) require address
    city = ""
    if profile.location:
        _city_state = profile.location.split(",")
        if len(_city_state) >= 2:
            city = _city_state[0].strip()
    address_selectors = [
        "input[name*='addressLine1' i]",
        "input[id*='addressLine1' i]",
        "input[name*='address' i]:not([name*='email' i])",
        "input[id*='address' i]:not([id*='email' i])",
        "input[placeholder*='address' i]",
        "input[aria-label*='Address Line 1' i]",
    ]
    for sel in address_selectors:
        if _fill_text_field(page, sel, city or "San Francisco"):
            filled.append(
                {
                    "field_name": "address_line_1",
                    "value": city or "San Francisco",
                    "source": "master_resume.md",
                    "filled": True,
                }
            )
            break

    # City
    city_selectors = [
        "input[name*='city' i]",
        "input[id*='city' i]",
        "input[placeholder*='city' i]",
        "input[aria-label*='city' i]",
    ]
    for sel in city_selectors:
        if _fill_text_field(page, sel, city or "San Francisco"):
            filled.append(
                {"field_name": "city", "value": city or "San Francisco", "source": "master_resume.md", "filled": True}
            )
            break

    # Parse state from profile location (e.g. "San Francisco, CA")
    _loc_parts = profile.location.split(",") if profile.location else []
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
    state_value = _STATE_MAP.get(_state_abbr, _state_abbr) or "California"

    # State (native select) — Phenom uses id="cntryFields.region"
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
            chosen_state = _select_locator_option_from_candidates(loc, [state_value, _state_abbr])
            if chosen_state:
                filled.append(
                    {"field_name": "state", "value": chosen_state, "source": "master_resume.md", "filled": True}
                )
                break

    # Postal Code (text input) — derive from city/state if not in profile
    postal_code = getattr(profile, "postal_code", "") or getattr(profile, "zip_code", "") or ""
    if not postal_code and _state_abbr == "CA" and "san francisco" in profile.location.lower():
        postal_code = "94105"
    if postal_code:
        zip_selectors = [
            "input[name*='postalCode' i]",
            "input[name*='postal_code' i]",
            "input[name*='zipCode' i]",
            "input[name*='zip' i]",
            "input[id*='postalCode' i]",
            "input[placeholder*='postal' i]",
            "input[placeholder*='zip' i]",
            "input[aria-label*='postal code' i]",
        ]
        for sel in zip_selectors:
            if _fill_text_field(page, sel, postal_code):
                filled.append(
                    {"field_name": "postal_code", "value": postal_code, "source": "master_resume.md", "filled": True}
                )
                break

    # Phone Device Type — Phenom uses id="deviceType"
    phone_type_selectors = [
        "select[id='deviceType']",
        "select[id*='deviceType' i]",
        "select[id*='device_type' i]",
        "select[name*='phoneDeviceType' i]",
        "select[name*='phone_device' i]",
        "select[id*='phoneDeviceType' i]",
        "select[id*='phone_type' i]",
    ]
    for sel in phone_type_selectors:
        loc = page.locator(sel).first
        if loc.count():
            chosen_phone_type = _select_locator_option_from_candidates(
                loc, ["Mobile", "Personal", "Pers Mobile", "Work", "Home", "Main", "Other"]
            )
            if chosen_phone_type:
                filled.append(
                    {
                        "field_name": "phone_device_type",
                        "value": chosen_phone_type,
                        "source": "deterministic",
                        "filled": True,
                    }
                )
                break

    # Fallback: find select elements by nearby label text
    if not any(f.get("field_name") == "phone_device_type" for f in filled):
        try:
            labels = page.locator("label:has-text('Phone Device Type'), label:has-text('Phone Type')")
            for i in range(labels.count()):
                label_el = labels.nth(i)
                for_id = label_el.get_attribute("for") or ""
                if for_id:
                    sel = page.locator(f"select#{for_id}").first
                else:
                    sel = label_el.locator("xpath=following::select[1]").first
                if sel.count():
                    chosen_phone_type = _select_locator_option_from_candidates(
                        sel, ["Mobile", "Personal", "Pers Mobile", "Work", "Home", "Main", "Other"]
                    )
                    if chosen_phone_type:
                        filled.append(
                            {
                                "field_name": "phone_device_type",
                                "value": chosen_phone_type,
                                "source": "deterministic",
                                "filled": True,
                            }
                        )
                        break
        except Exception:
            pass

    # Phone number
    phone = profile.phone
    # Strip country code prefix if present
    phone_digits = re.sub(r"^\+?1[\s-]?", "", phone)
    phone_selectors = [
        "input[name*='phoneNumber' i]",
        "input[name*='phone_number' i]",
        "input[id*='phoneNumber' i]",
        "input[type='tel']",
        "input[placeholder*='phone' i]",
        "input[aria-label*='phone number' i]",
    ]
    for sel in phone_selectors:
        if _fill_text_field(page, sel, phone_digits):
            filled.append(
                {
                    "field_name": "phone_number",
                    "value": phone_digits,
                    "source": "master_resume.md",
                    "filled": True,
                }
            )
            break

    # Source dropdown ("How Did You Hear About Us?")
    source_value = getattr(application_profile, "how_did_you_hear", "Corporate website") or "Corporate website"
    source_selectors = [
        "select#source",
        "select[id='source']",
        "select[id*='source' i]",
        "select[name*='source' i]",
        "select[name*='howDidYouHear' i]",
        "select[name*='how_did_you_hear' i]",
        "select[id*='howDidYouHear' i]",
    ]
    source_candidates = (
        source_value,
        "Corporate website",
        "Company Website",
        "Career site",
        "Career Site",
        "HPE Career site",
        "Website",
        "Blog",
        "Company Blog",
        "LinkedIn",
        "Job Boards",
        "Job Board",
        "External Organizations",
        "Social Media",
        "Direct",
        "Other",
        "Internet",
    )
    for sel in source_selectors:
        loc = page.locator(sel).first
        if loc.count():
            # Check if already filled (e.g. by resume parser)
            try:
                current_text = loc.evaluate("el => el.options[el.selectedIndex]?.text || ''")
                if current_text and not _is_placeholder_option(current_text):
                    filled.append(
                        {"field_name": "source", "value": current_text, "source": "resume_parser", "filled": True}
                    )
                    break
            except Exception:
                pass
            chosen_source = _select_locator_option_from_candidates(loc, source_candidates)
            if chosen_source:
                filled.append(
                    {
                        "field_name": "source",
                        "value": chosen_source,
                        "source": "application_profile.md",
                        "filled": True,
                    }
                )
            break

    # Handle conditional sub-source dropdown (e.g. "applicantSource" appears
    # after "Adobe Source" is selected in the main source dropdown)
    page.wait_for_timeout(1500)  # Wait for conditional fields to render
    sub_source_selectors = [
        "select[id='applicantSource']",
        "select[id*='applicantSource' i]",
        "select[id*='subSource' i]",
    ]
    for sel in sub_source_selectors:
        loc = page.locator(sel).first
        if loc.count() and loc.is_visible():
            # Pick the most generic option: company website, career site, etc.
            options = loc.locator("option").all()
            chosen = None
            for pref in ("Adobe.com", "Company Website", "Career Site", "Website", "Internet", "Other"):
                for opt in options:
                    if pref.lower() in opt.inner_text().strip().lower():
                        chosen = opt.inner_text().strip()
                        break
                if chosen:
                    break
            if not chosen:
                # Pick first non-placeholder option
                for opt in options:
                    val = opt.get_attribute("value") or ""
                    if val and not _is_placeholder_option(opt.inner_text().strip()):
                        chosen = opt.inner_text().strip()
                        break
            if chosen:
                try:
                    loc.select_option(label=chosen)
                    filled.append(
                        {"field_name": "applicant_source", "value": chosen, "source": "deterministic", "filled": True}
                    )
                except Exception:
                    pass
            break

    # Fallback: find "How Did You Hear" by label text
    if not any(f.get("field_name") == "source" for f in filled):
        try:
            labels = page.locator("label:has-text('How Did You Hear'), label:has-text('How did you hear')")
            for i in range(labels.count()):
                label_el = labels.nth(i)
                for_id = label_el.get_attribute("for") or ""
                if for_id:
                    sel = page.locator(f"select#{for_id}").first
                else:
                    sel = label_el.locator("xpath=following::select[1]").first
                if sel.count():
                    chosen_source = _select_locator_option_from_candidates(sel, source_candidates)
                    if chosen_source:
                        filled.append(
                            {
                                "field_name": "source",
                                "value": chosen_source,
                                "source": "application_profile.md",
                                "filled": True,
                            }
                        )
                        break
        except Exception:
            pass

    # "Have you been employed by [company]?" — always No
    # Phenom instances vary: some use value="true"/"false", others use
    # radio labels "Yes"/"No". Some use <div role="radiogroup">, others
    # use plain <div> containers with radio buttons.
    def _fill_former_employee_no(page, filled) -> bool:
        """Ensure the former-employee radio is set to 'No'."""

        # Approach 1: Click the "No" label directly near employment text
        try:
            emp_text = page.locator(
                "text=/[Hh]ave you.*(?:been |previously )?employed/,"
                "text=/[Pp]reviously.*employed/,"
                "text=/[Ff]ormer.*employee/"
            )
            if emp_text.count():
                # Find the container holding the radio buttons
                container = emp_text.first.locator("xpath=ancestor::*[.//input[@type='radio']][1]")
                if container.count():
                    # Try clicking the "No" label
                    no_label = container.first.locator("label:has-text('No')")
                    if no_label.count():
                        no_label.first.click()
                        page.wait_for_timeout(500)
                        return True

                    # Try radio by value
                    radios = container.first.locator("input[type='radio']")
                    for r in range(radios.count()):
                        radio = radios.nth(r)
                        val = radio.get_attribute("value") or ""
                        radio_id = radio.get_attribute("id") or ""
                        label_text = ""
                        if radio_id:
                            lbl = page.locator(f"label[for='{radio_id}']")
                            if lbl.count():
                                label_text = lbl.first.inner_text().strip().lower()
                        if val.lower() in ("no", "false") or label_text == "no":
                            radio.check(force=True)
                            page.wait_for_timeout(500)
                            return True
        except Exception:
            pass

        # Approach 2: Find radiogroup with "employed" text
        try:
            emp_group = page.locator(
                "[role='radiogroup']:has-text('employed'),[role='radiogroup']:has-text('previously')"
            )
            if emp_group.count():
                no_radio = emp_group.first.locator("input[type='radio'][value='false']")
                if no_radio.count():
                    no_radio.check(force=True)
                    return True
                no_label = emp_group.first.locator("label:has-text('No')")
                if no_label.count():
                    no_label.first.click()
                    return True
        except Exception:
            pass

        return False

    if _fill_former_employee_no(page, filled):
        filled.append({"field_name": "former_employee", "value": "No", "source": "deterministic", "filled": True})

    # Privacy consent checkbox (required)
    # Phenom uses id="emailAgreement" with the parent div as the click target.
    # IMPORTANT: Must click the container (not force-check the input) to trigger
    # Phenom's React state update for validation to pass.
    def _try_check_privacy(page, filled):
        """Try multiple approaches to check the privacy/agreement checkbox.

        Phenom instances use different IDs:
        - Adobe: id="emailAgreement"
        - HPE:   id="Additional Fields.noticeAgreement"
        """
        # Approach 1: Known Phenom privacy checkbox IDs — click parent label
        for cb_id in ("emailAgreement", "Additional Fields.noticeAgreement"):
            try:
                # Use CSS.escape for IDs with spaces/dots
                cb = (
                    page.locator(f"input#{cb_id}")
                    if " " not in cb_id and "." not in cb_id
                    else page.locator(f"input[id='{cb_id}']")
                )
                if cb.count():
                    if cb.is_checked():
                        return True
                    # Click the parent label (Phenom wraps in <label>)
                    parent_label = cb.locator("xpath=ancestor::label[1]")
                    if parent_label.count():
                        parent_label.click()
                        page.wait_for_timeout(500)
                        if cb.is_checked():
                            return True
                    # Click parent div
                    parent = cb.locator("xpath=..")
                    if parent.count():
                        parent.click()
                        page.wait_for_timeout(500)
                        if cb.is_checked():
                            return True
                    # Click the checkbox directly
                    cb.click()
                    page.wait_for_timeout(300)
                    if cb.is_checked():
                        return True
            except Exception:
                pass

        # Approach 2: Click label/container with privacy/agreement text
        for consent_text in (
            "I agree to the processing",
            "privacy",
            "consent",
            "Privacy Policy",
            "notice",
            "Adobe family",
        ):
            try:
                el = page.locator(f"label:has-text('{consent_text}')")
                if el.count():
                    el.first.click()
                    page.wait_for_timeout(300)
                    return True
            except Exception:
                continue

        # Approach 3: Standard checkbox selectors
        for sel in (
            "input[id*='noticeAgreement' i][type='checkbox']",
            "input[id*='agreement' i][type='checkbox']",
            "input[name*='privacy' i][type='checkbox']",
            "input[name*='consent' i][type='checkbox']",
            "input[id*='privacy' i][type='checkbox']",
            "input[id*='consent' i][type='checkbox']",
            "input[id*='agree' i][type='checkbox']",
        ):
            try:
                el = page.locator(sel).first
                if el.count():
                    if el.is_checked():
                        return True
                    el.click()
                    page.wait_for_timeout(300)
                    if el.is_checked():
                        return True
            except Exception:
                continue

        # Approach 4: JS — find and click required unchecked checkboxes
        try:
            result = page.evaluate("""() => {
                // Try known IDs first
                for (const id of ['emailAgreement', 'Additional Fields.noticeAgreement']) {
                    const cb = document.getElementById(id);
                    if (cb && !cb.checked) { cb.click(); if (cb.checked) return true; }
                    if (cb && cb.checked) return true;
                }
                // Fallback: find required unchecked checkbox
                const cbs = document.querySelectorAll('input[type="checkbox"][required]');
                for (const cb of cbs) {
                    if (!cb.checked) { cb.click(); return cb.checked; }
                }
                return false;
            }""")
            if result:
                return True
        except Exception:
            pass

        return False

    if _try_check_privacy(page, filled):
        filled.append({"field_name": "privacy_consent", "value": "checked", "source": "deterministic", "filled": True})

    # Text message consent — honor application_profile setting
    text_consent = getattr(application_profile, "text_message_consent", False)
    sms_selectors = [
        "input#smsOptIn",
        "input[id='smsOptIn']",
        "input[name*='textMessage' i][type='checkbox']",
        "input[name*='sms' i][type='checkbox']",
        "input[id*='textMessage' i][type='checkbox']",
        "input[id*='sms' i][type='checkbox']",
    ]
    for sel in sms_selectors:
        if _check_checkbox(page, sel, checked=text_consent):
            filled.append(
                {
                    "field_name": "text_message_consent",
                    "value": "checked" if text_consent else "unchecked",
                    "source": "application_profile.md",
                    "filled": True,
                }
            )
            break

    # Generic fallback: fill any remaining required selects still showing "Please Select"
    # Read all options and pick an intelligent default based on label context.
    try:
        all_selects = page.locator("select").all()
        for sel_el in all_selects:
            try:
                current_val = sel_el.evaluate("el => el.options[el.selectedIndex]?.text || ''").strip()
                if not _is_placeholder_option(current_val):
                    continue
                # Get the label for this select
                sel_id = sel_el.get_attribute("id") or ""
                sel_name = sel_el.get_attribute("name") or sel_id
                label_text = ""
                if sel_id:
                    lbl = page.locator(f"label[for='{sel_id}']")
                    if lbl.count():
                        label_text = lbl.first.inner_text().strip()
                if not label_text:
                    # Try parent form-group text
                    try:
                        parent = sel_el.locator(
                            "xpath=ancestor::*[contains(@class,'form-group') or contains(@class,'field')][1]"
                        )
                        if parent.count():
                            label_text = parent.first.inner_text()[:500].strip()
                    except Exception:
                        pass
                if not label_text:
                    label_text = sel_name
                label_lower = label_text.lower()

                # Read all non-placeholder options
                options = sel_el.locator("option").all()
                valid_options = []
                for opt in options:
                    opt_text = opt.inner_text().strip()
                    opt_val = opt.get_attribute("value") or ""
                    if not opt_val or _is_placeholder_option(opt_text):
                        continue
                    valid_options.append(opt_text)

                if not valid_options:
                    continue

                # Intelligent default based on label context
                chosen = None
                options_lower = [o.lower() for o in valid_options]
                is_yes_no = any(o in ("yes", "no") for o in options_lower)

                if is_yes_no:
                    # Former employee / worked for [company] → No
                    if any(
                        frag in label_lower
                        for frag in (
                            "worked for",
                            "employed",
                            "former",
                            "current employee",
                            "previously worked",
                            "government",
                        )
                    ):
                        answer = "No"
                    # Work permit / authorization → Yes
                    elif any(
                        frag in label_lower
                        for frag in (
                            "work permit",
                            "authorized",
                            "legal age",
                            "eligible",
                        )
                    ):
                        answer = "Yes"
                    else:
                        answer = "No"  # Safe default for unknown Yes/No questions
                    for vo in valid_options:
                        if vo.lower() == answer.lower():
                            chosen = vo
                            break
                elif "source" in label_lower:
                    for pref in (
                        "Career Site",
                        "Company Website",
                        "Website",
                        "Internet",
                        "Job Board",
                        "Job Boards",
                        "Other",
                    ):
                        for vo in valid_options:
                            if pref.lower() in vo.lower():
                                chosen = vo
                                break
                        if chosen:
                            break
                if not chosen:
                    chosen = valid_options[0]

                sel_el.select_option(label=chosen)
                filled.append(
                    {
                        "field_name": f"generic_select_{sel_name}",
                        "value": chosen,
                        "source": "generic_fallback",
                        "filled": True,
                    }
                )
                print(f"Phenom: generic fallback filled '{label_text}' with '{chosen}'", file=sys.stderr)
            except Exception:
                continue
    except Exception:
        pass

    return filled


def _fill_my_experience(page, out_dir: Path) -> list[dict]:
    """Fill the My Experience page (Step 2) — upload resume if not already done."""
    filled = []

    # Check for resume upload on this page too (some Phenom instances
    # put resume upload on Step 2 instead of Step 1)
    file_input = page.locator("input[type='file']").first
    if file_input.count():
        try:
            resume_path = find_resume_file(out_dir)
            file_input.set_input_files(str(resume_path))
            page.wait_for_timeout(5000)
            filled.append(
                {
                    "field_name": "resume",
                    "value": str(resume_path.name),
                    "source": "documents/",
                    "filled": True,
                }
            )
        except (FileNotFoundError, Exception) as exc:
            print(f"Phenom: resume upload on experience page failed: {exc}", file=sys.stderr)

    # Upload cover letter if there's a second file input
    try:
        cover_letter_path = find_cover_letter_file(out_dir)
    except FileNotFoundError:
        cover_letter_path = None

    if cover_letter_path:
        all_file_inputs = page.locator("input[type='file']")
        if all_file_inputs.count() > 1:
            try:
                all_file_inputs.nth(1).set_input_files(str(cover_letter_path))
                page.wait_for_timeout(3000)
                filled.append(
                    {
                        "field_name": "cover_letter",
                        "value": str(cover_letter_path.name),
                        "source": "documents/",
                        "filled": True,
                    }
                )
            except Exception:
                pass

    # IMPORTANT: Fill selects BEFORE textareas to avoid React re-render
    # clearing previously filled textareas when dropdown values change.

    # Fill any remaining unfilled required selects (Degree, Field of Study, etc.)
    try:
        all_selects = page.locator("select").all()
        for sel_el in all_selects:
            try:
                current_val = sel_el.evaluate("el => el.options[el.selectedIndex]?.text || ''").strip()
                if not _is_placeholder_option(current_val):
                    continue
                sel_id = sel_el.get_attribute("id") or sel_el.get_attribute("name") or ""
                sel_lower = sel_id.lower()

                # Read valid options
                options = sel_el.locator("option").all()
                valid_opts = []
                for opt in options:
                    t = opt.inner_text().strip()
                    v = opt.get_attribute("value") or ""
                    if v and not _is_placeholder_option(t):
                        valid_opts.append(t)

                if not valid_opts:
                    continue

                chosen = None
                # Degree selects — prefer Master's/Bachelor's
                if "degree" in sel_lower:
                    for pref in ("Master", "Bachelor", "MBA"):
                        for vo in valid_opts:
                            if pref.lower() in vo.lower():
                                chosen = vo
                                break
                        if chosen:
                            break
                # Field of study — prefer business/management/CS
                # Use priority scoring: starts-with > contains, shorter > longer
                elif "field" in sel_lower or "study" in sel_lower or "major" in sel_lower:
                    best_score = (999, 999)
                    for pref in (
                        "Business Administration",
                        "Business",
                        "Management",
                        "Computer Science",
                        "Engineering",
                        "Economics",
                    ):
                        for vo in valid_opts:
                            vl = vo.lower()
                            pl = pref.lower()
                            if vl.startswith(pl):
                                score = (0, len(vo))
                            elif pl in vl:
                                score = (1, len(vo))
                            else:
                                continue
                            if score < best_score:
                                best_score = score
                                chosen = vo
                        if best_score[0] == 0:
                            break

                if not chosen:
                    chosen = valid_opts[0]

                sel_el.select_option(label=chosen)
                filled.append(
                    {
                        "field_name": f"experience_select_{sel_id}",
                        "value": chosen,
                        "source": "deterministic",
                        "filled": True,
                    }
                )
                print(f"Phenom: experience fallback filled '{sel_id}' with '{chosen}'", file=sys.stderr)
            except Exception:
                continue
    except Exception:
        pass

    # Wait for React re-renders to settle after select changes
    page.wait_for_timeout(2000)

    # Fill empty "Role Description" textareas from master_resume.md
    # Phenom's resume parser fills basic info (company, title, dates)
    # but often leaves role descriptions empty.
    resume_text = (PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8")

    # Parse work experience sections: ## COMPANY — Title\n* bullet\n* bullet
    exp_blocks: list[dict] = []
    current_block: dict | None = None
    for line in resume_text.split("\n"):
        if line.startswith("## ") and "—" in line:
            if current_block:
                exp_blocks.append(current_block)
            parts = line[3:].split("—", 1)
            current_block = {
                "company": parts[0].strip(),
                "title": parts[1].strip() if len(parts) > 1 else "",
                "bullets": [],
            }
        elif line.startswith("* ") and current_block is not None:
            current_block["bullets"].append(line[2:].strip())
        elif line.startswith("# ") and "EDUCATION" in line.upper():
            if current_block:
                exp_blocks.append(current_block)
            break
    else:
        if current_block:
            exp_blocks.append(current_block)

    if exp_blocks:
        # Use JS to find all description textareas — their IDs contain
        # brackets (e.g. "experienceData[0].description") which are
        # problematic in CSS selectors.
        page.evaluate("() => document.querySelectorAll('textarea').length")
        ta_ids: list[str] = page.evaluate("""() => {
            const tas = document.querySelectorAll('textarea');
            return Array.from(tas)
                .filter(t => t.id.toLowerCase().includes('description'))
                .map(t => t.id);
        }""")
        print(f"Phenom: found {len(ta_ids)} description textareas", file=sys.stderr)

        for i, ta_id in enumerate(ta_ids):
            try:
                # Match to experience block positionally
                matched_block = exp_blocks[i] if i < len(exp_blocks) else None
                if not matched_block or not matched_block["bullets"]:
                    continue

                # Truncate to first 3 bullets to keep type() fast
                bullets = matched_block["bullets"][:3]
                description = "\n".join(f"• {b}" for b in bullets)

                textarea = page.locator(f"id={ta_id}")
                if not textarea.count():
                    continue

                # Try three strategies to satisfy React validation:
                # 1. type() — real keyboard events that React synthetic event
                #    system picks up reliably
                # 2. fill() + native events — fallback
                # 3. React fiber onChange — direct call
                textarea.click()
                page.wait_for_timeout(200)

                # Strategy 1: triple-click to select all, then type new value
                textarea.click(click_count=3)
                page.wait_for_timeout(100)
                textarea.type(description, delay=5)
                page.wait_for_timeout(300)

                # Verify React recognised the value
                dom_val = textarea.evaluate("el => el.value")
                if not dom_val or len(dom_val.strip()) < 10:
                    # Strategy 2: fill() + native events
                    print(f"Phenom: type() didn't stick for {ta_id}, trying fill()", file=sys.stderr)
                    textarea.fill("")
                    page.wait_for_timeout(100)
                    textarea.fill(description)
                    page.wait_for_timeout(200)
                    textarea.evaluate("""(el) => {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""")

                # Strategy 3: try to trigger React's onChange via fiber
                textarea.evaluate("""(el) => {
                    // Find React fiber instance and trigger onChange
                    const key = Object.keys(el).find(k => k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$'));
                    if (key) {
                        let fiber = el[key];
                        while (fiber) {
                            if (fiber.memoizedProps && fiber.memoizedProps.onChange) {
                                fiber.memoizedProps.onChange({ target: el, currentTarget: el });
                                break;
                            }
                            fiber = fiber.return;
                        }
                    }
                }""")

                # Blur to trigger validation
                textarea.evaluate("el => el.dispatchEvent(new Event('blur', { bubbles: true }))")
                page.wait_for_timeout(100)

                filled.append(
                    {
                        "field_name": f"role_description_{i}",
                        "value": f"{matched_block['company']}: {len(bullets)} bullets",
                        "source": "master_resume.md",
                        "filled": True,
                    }
                )
                print(f"Phenom: filled role description for {matched_block['company']}", file=sys.stderr)
            except Exception as exc:
                print(f"Phenom: failed to fill role description {i}: {exc}", file=sys.stderr)

    # After filling all textareas, click somewhere neutral to trigger
    # any remaining blur/validation handlers
    try:
        page.click("h1, h2, .page-title, header", timeout=2000)
    except Exception:
        pass
    page.wait_for_timeout(500)

    return filled


def _fill_application_questions(page, out_dir: Path, meta: dict, provider: str | None) -> list[dict]:
    """Fill custom application questions (Step 3) using LLM-generated answers."""
    filled = []
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))

    # Pre-fill: check "I have not worked for [company] in the past" if present
    try:
        not_worked = page.locator("label:has-text('I have not worked'), label:has-text('have not worked')")
        for i in range(not_worked.count()):
            lbl = not_worked.nth(i)
            try:
                lbl.click()
                filled.append(
                    {"field_name": "not_worked_before", "value": "checked", "source": "deterministic", "filled": True}
                )
                page.wait_for_timeout(300)
                break
            except Exception:
                continue
    except Exception:
        pass

    # Collect visible questions from the page
    # Phenom wraps questions in form groups/fieldsets
    question_groups = page.locator(
        ".form-group, .question-group, fieldset, "
        "[class*='question'], [class*='field-group'], "
        "[data-ph-at-id*='question'], .form-field"
    )
    question_specs = []
    field_map: dict[str, dict] = {}
    count = question_groups.count()

    for i in range(count):
        group = question_groups.nth(i)
        label_el = group.locator("label, legend, .field-label, [class*='label']").first
        if not label_el.count():
            continue
        label_text = label_el.inner_text().strip()
        if not label_text or len(label_text) < 3:
            continue

        field_name = slugify_label(label_text)
        field_info = {
            "index": i,
            "label": label_text,
            "field_name": field_name,
            "group_locator": group,
        }

        # Detect field type
        select = group.locator("select").first
        textarea = group.locator("textarea").first
        text_input = group.locator("input[type='text'], input[type='number'], input:not([type])").first
        radio = group.locator("input[type='radio']").first
        checkbox = group.locator("input[type='checkbox']").first

        if select.count():
            field_info["type"] = "select"
            field_info["locator"] = select
            try:
                field_info["options"] = [
                    option.inner_text().strip() for option in select.locator("option").all() if option.inner_text().strip()
                ]
            except Exception:
                pass
        elif textarea.count():
            field_info["type"] = "textarea"
            field_info["locator"] = textarea
        elif radio.count():
            field_info["type"] = "radio"
            field_info["locator"] = radio
            try:
                option_labels: list[str] = []
                radios = group.locator("input[type='radio']")
                for radio_index in range(radios.count()):
                    radio_input = radios.nth(radio_index)
                    radio_id = radio_input.get_attribute("id") or ""
                    if radio_id:
                        option_label = group.locator(f"label[for='{radio_id}']").first
                        if option_label.count():
                            text = option_label.inner_text().strip()
                            if text:
                                option_labels.append(text)
                                continue
                    value = (radio_input.get_attribute("value") or "").strip()
                    if value:
                        option_labels.append(value)
                if option_labels:
                    field_info["options"] = option_labels
            except Exception:
                pass
        elif checkbox.count():
            field_info["type"] = "checkbox"
            field_info["locator"] = checkbox
        elif text_input.count():
            field_info["type"] = "text"
            field_info["locator"] = text_input
        else:
            field_info["type"] = "unknown"
            continue

        # Try deterministic answers first
        label_lower = label_text.lower()
        deterministic = _try_deterministic_answer(label_lower, field_info, application_profile)
        if deterministic:
            _apply_field_answer(page, field_info, deterministic["value"])
            filled.append(
                {
                    "field_name": field_name,
                    "label": label_text,
                    "value": deterministic["value"],
                    "source": deterministic["source"],
                    "filled": True,
                }
            )
            continue

        # Queue for LLM generation
        field_map[field_name] = field_info
        question_specs.append(
            {
                "field_name": field_name,
                "label": label_text,
                "description": "",
                "required": True,
                "type": field_info["type"],
            }
        )

    # Generate answers for non-deterministic questions
    if question_specs:
        # Mark all question_specs as optional for Phenom to avoid crash on
        # questions where label text was extracted from internal attributes
        for qs in question_specs:
            qs["required"] = False
        try:
            generated_answers = generate_application_answers(
                out_dir=out_dir,
                meta=meta,
                question_specs=question_specs,
                provider=provider,
            )
        except (ValueError, RuntimeError) as exc:
            print(f"Phenom: application answer generation failed: {exc}", file=sys.stderr)
            generated_answers = {}
        for field_name, answer in generated_answers.items():
            info = field_map.get(field_name)
            if not info or not answer:
                continue
            _apply_field_answer(page, info, answer)
            filled.append(
                {
                    "field_name": field_name,
                    "label": info["label"],
                    "value": answer,
                    "source": "generated_application_answer",
                    "filled": True,
                }
            )

    return filled


def _try_deterministic_answer(label_lower: str, field_info: dict, application_profile) -> dict | None:
    """Try to answer a question deterministically using the unified classifier.

    Falls back to Phenom-specific inline checks for categories not (yet)
    covered by the unified classifier.
    """
    requires_sponsorship = application_profile.require_sponsorship_now or application_profile.require_sponsorship_future

    # --- Unified classifier dispatch ---
    category = classify_question(label_lower)
    if category is not None:
        policy = resolve_shared_question_policy(label_lower, application_profile)
        if policy is not None and policy.category != "work_authorization" and policy.text_value is not None:
            matched = select_shared_policy_option(
                field_info.get("options"),
                policy,
                application_profile=application_profile,
            )
            return {"value": matched or policy.text_value, "source": policy.source}
        if category == "education":
            education_text = format_education_from_profile(application_profile)
            if education_text:
                return {"value": education_text, "source": "application_profile.md"}
        elif category == "compensation":
            return {
                "value": (
                    "I'm open and flexible on compensation. I'd prefer to learn more about "
                    "the role's scope and total rewards package before discussing specific numbers. "
                    "I'm confident we can find a mutually agreeable arrangement."
                ),
                "source": "deterministic",
            }
        elif category == "nda_noncompete":
            return {"value": "No", "source": "deterministic"}
        elif category == "work_authorization":
            if field_info["type"] in ("select", "radio"):
                if policy is not None and policy.boolean_value is not None:
                    return {
                        "value": "Yes" if policy.boolean_value else "No",
                        "source": policy.source,
                    }
                return {
                    "value": "Yes" if application_profile.authorized_to_work_unconditionally else "No",
                    "source": "application_profile.md",
                }
            sponsorship_answer = build_truthful_work_authorization_answer(label_lower, application_profile)
            if sponsorship_answer:
                return {"value": sponsorship_answer, "source": "application_profile.md"}
            return {
                "value": application_profile.work_authorization_statement or "Yes",
                "source": "application_profile.md",
            }
        elif category == "city_location":
            return {"value": application_profile.location or "", "source": "application_profile.md"}
        elif category == "current_company":
            val = getattr(application_profile, "current_company", None)
            if val:
                return {"value": val, "source": "application_profile.md"}
        elif category == "culture_careers_optin":
            return {"value": "No", "source": "deterministic"}
        elif category in ("product_usage", "reasonable_accommodation"):
            return {"value": "Yes", "source": "deterministic"}
        elif category == "interview_accommodation":
            return {"value": "No", "source": "deterministic"}
        elif category == "salary_comfort":
            return {
                "value": "Yes" if application_profile.comfortable_with_posted_salary else "No",
                "source": "application_profile.md",
            }
        elif category in ("minimum_experience", "experience_confirmation", "office_attendance"):
            return {"value": "Yes", "source": "application_profile.md"}
        elif category == "company_engagement":
            return {"value": "Yes", "source": "deterministic"}

    # --- Phenom-specific inline checks (not covered by unified classifier) ---

    # Sponsorship (select/radio vs free text)
    if any(frag in label_lower for frag in ("sponsorship", "sponsor", "visa")):
        if field_info["type"] in ("select", "radio"):
            return {"value": "Yes" if requires_sponsorship else "No", "source": "application_profile.md"}
        return {"value": application_profile.sponsorship_answer, "source": "application_profile.md"}

    # Former/current employee
    if any(
        frag in label_lower for frag in ("former employee", "current employee", "previously employed", "worked for")
    ):
        return {"value": "No", "source": "deterministic"}

    # Legal age / eligible to work
    if any(frag in label_lower for frag in ("legal age", "of legal age", "legally eligible", "18 years")):
        return {"value": "Yes", "source": "deterministic"}

    # Background check / drug screening
    if any(frag in label_lower for frag in ("background check", "drug screen", "drug test", "willing to submit")):
        return {"value": "Yes", "source": "deterministic"}

    # Provide documentation / identity proof
    if any(
        frag in label_lower
        for frag in ("provide documentation", "provide identification", "identity and right to work")
    ):
        return {"value": "Yes", "source": "deterministic"}

    # Relocate / onsite / location
    if any(frag in label_lower for frag in ("relocate", "willing to relocate", "own expense", "willing to work from")):
        return {"value": "Yes", "source": "application_profile.md"}

    # Employment visa
    if "employment visa" in label_lower:
        return {"value": "Yes" if requires_sponsorship else "No", "source": "application_profile.md"}

    # Worked at [company] checkbox — "I have not worked for"
    if "i have not worked" in label_lower or "have not worked" in label_lower:
        return {"value": "checked", "source": "deterministic"}

    return None


def _apply_field_answer(page, field_info: dict, value: str) -> None:
    """Apply an answer value to a field on the page."""
    field_type = field_info.get("type", "text")
    locator = field_info.get("locator")
    group = field_info.get("group_locator")

    if not locator:
        return

    try:
        if field_type == "text":
            locator.click()
            locator.fill("")
            human_fill(locator, value)
        elif field_type == "textarea":
            locator.click()
            if len(value) > 400:
                locator.fill(value)
            else:
                locator.fill("")
                human_fill(locator, value)
        elif field_type == "select":
            try:
                locator.select_option(label=value)
            except Exception:
                # Try partial match
                options = locator.locator("option").all()
                for opt in options:
                    text = opt.inner_text().strip()
                    if value.lower() in text.lower() or text.lower() in value.lower():
                        locator.select_option(label=text)
                        break
        elif field_type == "radio":
            # Find the radio option matching the value
            if group:
                matching_label = group.locator(f"label:has-text('{value}')")
                if matching_label.count():
                    matching_label.first.click()
                else:
                    radio = group.locator(f"input[type='radio'][value='{value}']")
                    if radio.count():
                        radio.first.check(force=True)
        elif field_type == "checkbox":
            checked = value.lower() in ("yes", "true", "checked")
            if checked:
                locator.check(force=True)
            else:
                locator.uncheck(force=True)
    except Exception as exc:
        print(f"Phenom: failed to fill {field_info.get('label', '?')}: {exc}", file=sys.stderr)


def _fill_voluntary_disclosures(page, application_profile) -> list[dict]:
    """Fill the Voluntary Disclosures / Self Identity page (Steps 4-5)."""
    filled = []

    # Gender
    gender = getattr(application_profile, "gender", "")
    if gender:
        for sel in ("select[name*='gender' i]", "select[id*='gender' i]", "select[name*='Gender' i]"):
            if _fill_native_select(page, sel, gender):
                filled.append(
                    {"field_name": "gender", "value": gender, "source": "application_profile.md", "filled": True}
                )
                break
        else:
            # Try radio buttons
            gender_label = page.locator(f"label:has-text('{gender}')")
            if gender_label.count():
                try:
                    gender_label.first.click()
                    filled.append(
                        {"field_name": "gender", "value": gender, "source": "application_profile.md", "filled": True}
                    )
                except Exception:
                    pass

    # Hispanic or Latino (separate Yes/No field on some Phenom instances like HPE)
    race = getattr(application_profile, "race_or_ethnicity", "")
    is_hispanic = "hispanic" in race.lower() or "latino" in race.lower()
    for sel in (
        "select[id='eeoUSA.hispanicOrLatino']",
        "select[id*='hispanicOrLatino' i]",
        "select[id*='hispanic' i]",
    ):
        loc = page.locator(sel).first
        if loc.count():
            answer = "Yes" if is_hispanic else "No"
            try:
                loc.select_option(label=answer)
                filled.append(
                    {
                        "field_name": "hispanic_or_latino",
                        "value": answer,
                        "source": "application_profile.md",
                        "filled": True,
                    }
                )
            except Exception:
                pass
            break

    # Race / Ethnicity
    if race:
        race_filled = False
        for sel in (
            "select[id='eeoUSA.ethnicity']",
            "select[id*='ethnicity' i]",
            "select[name*='race' i]",
            "select[name*='ethnicity' i]",
            "select[id*='race' i]",
        ):
            if _fill_native_select(page, sel, race):
                filled.append(
                    {"field_name": "race_ethnicity", "value": race, "source": "application_profile.md", "filled": True}
                )
                race_filled = True
                break
        if not race_filled:
            race_label = page.locator(f"label:has-text('{race}')")
            if race_label.count():
                try:
                    race_label.first.click()
                    filled.append(
                        {
                            "field_name": "race_ethnicity",
                            "value": race,
                            "source": "application_profile.md",
                            "filled": True,
                        }
                    )
                except Exception:
                    pass

    # Veteran Status
    # Phenom uses ALL CAPS options like "I AM NOT A VETERAN",
    # "I IDENTIFY AS A VETERAN, JUST NOT A PROTECTED VETERAN", etc.
    # Profile value is "I am not a protected veteran" — try multiple variants.
    veteran = getattr(application_profile, "veteran_status", "")
    if veteran:
        veteran_candidates = [
            veteran,
            veteran.upper(),
            "I AM NOT A VETERAN",
            "I am not a veteran",
            "I IDENTIFY AS A VETERAN, JUST NOT A PROTECTED VETERAN",
            "I am not a protected veteran",
            "I DO NOT WISH TO SELF-IDENTIFY",
        ]
        vet_filled = False
        for sel in (
            "select[id='eeoUSA.veteranStatus']",
            "select[id*='veteranStatus' i]",
            "select[name*='veteran' i]",
            "select[id*='veteran' i]",
        ):
            loc = page.locator(sel).first
            if not loc.count():
                continue
            for candidate in veteran_candidates:
                try:
                    loc.select_option(label=candidate)
                    filled.append(
                        {
                            "field_name": "veteran_status",
                            "value": candidate,
                            "source": "application_profile.md",
                            "filled": True,
                        }
                    )
                    vet_filled = True
                    break
                except Exception:
                    continue
            if vet_filled:
                break
        if not vet_filled:
            vet_label = page.locator(f"label:has-text('{veteran}')")
            if vet_label.count():
                try:
                    vet_label.first.click()
                    filled.append(
                        {
                            "field_name": "veteran_status",
                            "value": veteran,
                            "source": "application_profile.md",
                            "filled": True,
                        }
                    )
                except Exception:
                    pass

    # Disability Status
    disability = getattr(application_profile, "disability_status", "")
    if disability:
        for sel in ("select[name*='disability' i]", "select[id*='disability' i]"):
            if _fill_native_select(page, sel, disability):
                filled.append(
                    {
                        "field_name": "disability_status",
                        "value": disability,
                        "source": "application_profile.md",
                        "filled": True,
                    }
                )
                break
        else:
            dis_label = page.locator(f"label:has-text('{disability}')")
            if dis_label.count():
                try:
                    dis_label.first.click()
                    filled.append(
                        {
                            "field_name": "disability_status",
                            "value": disability,
                            "source": "application_profile.md",
                            "filled": True,
                        }
                    )
                except Exception:
                    pass

    # Sexual Orientation
    orientation = getattr(application_profile, "sexual_orientation", "")
    if orientation:
        for sel in ("select[name*='sexual' i]", "select[name*='orientation' i]", "select[id*='sexual' i]"):
            if _fill_native_select(page, sel, orientation):
                filled.append(
                    {
                        "field_name": "sexual_orientation",
                        "value": orientation,
                        "source": "application_profile.md",
                        "filled": True,
                    }
                )
                break

    # Transgender Status
    trans_status = getattr(application_profile, "transgender_status", "")
    if trans_status:
        for sel in ("select[name*='transgender' i]", "select[id*='transgender' i]"):
            if _fill_native_select(page, sel, trans_status):
                filled.append(
                    {
                        "field_name": "transgender_status",
                        "value": trans_status,
                        "source": "application_profile.md",
                        "filled": True,
                    }
                )
                break

    # Terms & Conditions / "confirm the statement above" checkbox
    # Phenom uses id="agreementCheck" with class="checkbox-control".
    # IMPORTANT: Must use .click() (not .check(force=True)) to trigger
    # Phenom's React state update for validation to pass.
    try:

        def _try_check_terms(page) -> bool:
            # Approach 1: Click the agreementCheck checkbox directly
            try:
                cb = page.locator("input#agreementCheck")
                if cb.count():
                    if cb.is_checked():
                        return True
                    # Click the parent label (Phenom wraps in <label>)
                    parent_label = cb.locator("xpath=ancestor::label[1]")
                    if parent_label.count():
                        parent_label.click()
                        page.wait_for_timeout(300)
                        if cb.is_checked():
                            return True
                    # Click the checkbox itself
                    cb.click()
                    page.wait_for_timeout(300)
                    if cb.is_checked():
                        return True
            except Exception:
                pass

            # Approach 2: Click labels containing checkbox-related text
            for label_text in (
                "Check this box",
                "confirm the statement",
                "Terms and Conditions",
                "acknowledge",
            ):
                try:
                    lbl = page.locator(f"label:has-text('{label_text}')")
                    if lbl.count():
                        lbl.first.click()
                        page.wait_for_timeout(300)
                        return True
                except Exception:
                    continue

            # Approach 3: ID-based checkbox selectors with click
            for sel in (
                "input[type='checkbox'][id*='agreement' i]",
                "input[type='checkbox'][id*='terms' i]",
                "input[type='checkbox'][id*='confirm' i]",
                "input[type='checkbox'][id*='acknowledge' i]",
            ):
                try:
                    el = page.locator(sel).first
                    if el.count() and not el.is_checked():
                        el.click()
                        page.wait_for_timeout(300)
                        return True
                    if el.count() and el.is_checked():
                        return True
                except Exception:
                    continue

            # Approach 4: JS fallback — find and click unchecked checkboxes
            # near terms/confirm text
            try:
                result = page.evaluate("""() => {
                    const cbs = document.querySelectorAll('input[type="checkbox"]');
                    for (const cb of cbs) {
                        if (cb.checked) continue;
                        const container = cb.closest('label') || cb.parentElement;
                        if (!container) continue;
                        const text = container.textContent.toLowerCase();
                        if (text.includes('terms') || text.includes('confirm')
                            || text.includes('check this box')
                            || text.includes('acknowledge')
                            || text.includes('agreement')) {
                            cb.click();
                            return cb.checked;
                        }
                    }
                    return false;
                }""")
                if result:
                    return True
            except Exception:
                pass

            return False

        if _try_check_terms(page):
            filled.append(
                {"field_name": "terms_conditions", "value": "checked", "source": "deterministic", "filled": True}
            )
    except Exception:
        pass

    # Generic fallback: fill remaining unfilled selects on the disclosures page.
    # HPE embeds questionnaire-type Yes/No selects (government official,
    # restrictions, work permit) alongside demographic fields.
    all_selects = page.locator("select").all()
    filled_ids = set()
    for f in filled:
        filled_ids.add(f.get("field_name", ""))

    for sel_el in all_selects:
        try:
            sel_id = sel_el.get_attribute("id") or sel_el.get_attribute("name") or ""
            if not sel_id:
                continue
            slug = slugify_label(sel_id)
            if slug in filled_ids:
                continue
            # Check if already has a non-placeholder value selected
            try:
                current_text = sel_el.evaluate("el => el.options[el.selectedIndex]?.text || ''")
                if current_text and not _is_placeholder_option(current_text):
                    continue
            except Exception:
                pass

            # Read label context and all option texts
            label_text = ""
            try:
                lbl = page.locator(f"label[for='{sel_id}']")
                if lbl.count():
                    label_text = lbl.first.inner_text().strip().lower()
            except Exception:
                pass
            if not label_text:
                # Try parent container text
                try:
                    parent = sel_el.locator(
                        "xpath=ancestor::*[contains(@class,'form-group') or contains(@class,'field')][1]"
                    )
                    if parent.count():
                        label_text = parent.first.inner_text()[:500].strip().lower()
                except Exception:
                    pass

            options_text = []
            try:
                opts = sel_el.locator("option").all()
                for o in opts:
                    t = o.inner_text().strip()
                    v = o.get_attribute("value") or ""
                    if v and not _is_placeholder_option(t):
                        options_text.append(t)
            except Exception:
                pass

            # Determine answer based on label context and option types
            chosen = None

            # Yes/No question — use deterministic logic
            options_lower = [o.lower() for o in options_text]
            is_yes_no = any(o in ("yes", "no") for o in options_lower)

            if is_yes_no:
                # Default to "No" for most compliance/government questions
                answer = "No"
                # Answer "Yes" for work permit / residency permit / authorization
                if any(
                    frag in label_text
                    for frag in (
                        "work permit",
                        "residency permit",
                        "authorized",
                        "right to work",
                        "eligible to work",
                        "legal age",
                    )
                ):
                    answer = "Yes"
                # Find matching option (case-insensitive)
                for ot in options_text:
                    if ot.lower() == answer.lower():
                        chosen = ot
                        break
                if not chosen:
                    # Try uppercase variant
                    for ot in options_text:
                        if ot.upper() == answer.upper():
                            chosen = ot
                            break

            # Demographic decline options
            if not chosen:
                decline_options = (
                    "I am Not a Protected Veteran",
                    "I am not a protected veteran",
                    "I AM NOT A VETERAN",
                    "Decline to Self Identify",
                    "Prefer not to say",
                    "Choose not to disclose",
                    "I don't wish to answer",
                    "I DO NOT WISH TO SELF-IDENTIFY",
                    "Not Specified",
                )
                for candidate in decline_options:
                    try:
                        sel_el.select_option(label=candidate)
                        chosen = candidate
                        break
                    except Exception:
                        continue

            # Last resort: pick first non-placeholder option
            if not chosen and options_text:
                chosen = options_text[0]

            if chosen:
                try:
                    sel_el.select_option(label=chosen)
                    filled.append(
                        {
                            "field_name": slug,
                            "value": chosen,
                            "source": "deterministic",
                            "filled": True,
                        }
                    )
                    filled_ids.add(slug)
                    print(f"Phenom: disclosures fallback filled '{sel_id}' with '{chosen}'", file=sys.stderr)
                except Exception:
                    pass
        except Exception:
            continue

    # PrimeVue-style terms checkbox — HPE uses custom Vue components
    # instead of native checkboxes. Try clicking any element that looks
    # like an unchecked terms/consent checkbox.
    if not any(f.get("field_name") == "terms_conditions" for f in filled):
        try:
            result = page.evaluate("""() => {
                // Look for unchecked custom checkbox elements near terms text
                const containers = document.querySelectorAll(
                    '.p-checkbox, [class*="checkbox"], [role="checkbox"]'
                );
                for (const el of containers) {
                    const parent = el.closest('.form-group, .field, div');
                    if (!parent) continue;
                    const text = parent.textContent.toLowerCase();
                    if (text.includes('terms') || text.includes('consent')
                        || text.includes('i have read') || text.includes('agree')) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }""")
            if result:
                filled.append(
                    {"field_name": "terms_conditions", "value": "checked", "source": "deterministic", "filled": True}
                )
        except Exception:
            pass

    return filled


# ─── Build payload ────────────────────────────────────────────────────────────


def _resolve_phenom_url(meta: dict, out_dir: Path) -> str:
    """Resolve the Phenom job URL for this role.

    The meta's ``jd_source_resolved`` may point to a different board (e.g.
    Greenhouse) when the JD was originally scraped from a redirect URL.
    Fall back to ``board_url`` in the meta or jobs.db to find the actual
    Phenom URL.
    """
    from job_board_urls import looks_like_phenom_url

    # Prefer jd_source_resolved / jd_source if they look like Phenom
    for key in ("jd_source_resolved", "jd_source", "board_url"):
        candidate = str(meta.get(key) or "")
        if candidate and looks_like_phenom_url(candidate):
            return candidate

    # Fall back to jobs.db board_url
    import sqlite3

    db_path = PROJECT_ROOT / "jobs.db"
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            row = conn.execute(
                "SELECT board_url, url FROM jobs WHERE output_dir = ? OR output_dir = ? LIMIT 1",
                (str(out_dir), str(out_dir.relative_to(PROJECT_ROOT))),
            ).fetchone()
            conn.close()
            if row:
                for val in row:
                    if val and looks_like_phenom_url(val):
                        return val
        except sqlite3.Error:
            pass

    # Last resort: use whatever the meta has
    return str(meta.get("jd_source_resolved") or meta["jd_source"])


def _build_payload(out_dir: Path, provider: str | None = None) -> dict:
    """Build the autofill payload for a Phenom application."""
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)
    profile = parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
    application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))

    job_url = _resolve_phenom_url(meta, out_dir)
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

    application_url = _phenom_application_url(job_url)
    constants = board_file_constants("phenom")

    return {
        "board": "phenom",
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
            "payload_json": str(role_submit_path(out_dir, constants["payload_json"])),
            "report_json": str(role_submit_path(out_dir, constants["report_json"])),
            "report_markdown": str(role_submit_path(out_dir, constants["report_md"])),
            "pre_submit_screenshot": str(role_submit_path(out_dir, constants["pre_submit_screenshot"])),
            "page_screenshots_dir": str(role_submit_path(out_dir, constants["page_screenshots_dir"])),
            "submit_debug_html": str(role_submit_path(out_dir, constants["submit_debug_html"])),
            "submit_debug_screenshot": str(role_submit_path(out_dir, constants["submit_debug_screenshot"])),
        },
    }


# ─── Custom browser pipeline ─────────────────────────────────────────────────


def _run_phenom_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    """Custom browser pipeline for Phenom multi-page wizard."""
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
            purpose="Phenom autofill",
        )
        page = browser.new_page(viewport=viewport, device_scale_factor=2)
        terminal_apply_submit_result: dict[str, str] | None = None

        def _on_response(response) -> None:
            nonlocal terminal_apply_submit_result
            if terminal_apply_submit_result is not None or "applySubmit" not in response.url:
                return
            try:
                response_body = response.text()
            except Exception:
                return
            result = _terminal_apply_submit_result_from_body(response_body)
            if result is not None:
                terminal_apply_submit_result = result

        page.on("response", _on_response)

        try:
            # --- Phase 1: Navigate to application URL ---
            application_url = payload.get("application_url", payload["job_url"])
            print(f"Phenom: navigating to {application_url}", file=sys.stderr)
            page.goto(application_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)

            # If we landed on the JD page, look for "Apply Now" button
            apply_btn = page.locator(
                "a:has-text('Apply Now'), a:has-text('Apply'), button:has-text('Apply Now'), button:has-text('Apply')"
            )
            if apply_btn.count() and "/apply" not in page.url:
                try:
                    apply_btn.first.click()
                    page.wait_for_timeout(5000)
                except Exception:
                    pass

            # --- Phase 2: Page-by-page form filling ---
            max_pages = 10  # Safety limit
            prev_page = None
            same_page_count = 0
            for page_attempt in range(max_pages):
                page.wait_for_timeout(2000)

                # Dismiss feedback/survey popups and cookie banners
                _dismiss_popups(page)

                auth_result = _detect_phenom_auth_result(page, payload)
                if auth_result is not None:
                    _record_phenom_terminal_result(page, payload, out_dir, auth_result)
                    print(auth_result["message"], file=sys.stderr)
                    return 0

                if terminal_apply_submit_result is not None:
                    _record_phenom_terminal_result(page, payload, out_dir, terminal_apply_submit_result)
                    print(terminal_apply_submit_result["message"], file=sys.stderr)
                    return 0

                current_page = _detect_current_page(page)
                print(f"Phenom: on page '{current_page}' (step {page_attempt + 1})", file=sys.stderr)

                # Stuck-page detection: if we've been on the same page 3+ times, abort
                if current_page == prev_page:
                    same_page_count += 1
                    if same_page_count >= 3:
                        print(
                            f"Phenom: stuck on '{current_page}' for {same_page_count} attempts. "
                            f"Likely unfillable required fields. Aborting.",
                            file=sys.stderr,
                        )
                        # Save debug artifacts
                        debug_html = Path(payload["artifacts"]["submit_debug_html"])
                        debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
                        debug_html.write_text(page.content(), encoding="utf-8")
                        _capture(page, debug_png)
                        return 1
                else:
                    same_page_count = 0
                prev_page = current_page

                # Screenshot each page
                page_idx += 1
                page_path = page_screenshots_dir / f"page_{page_idx:02d}_{current_page}.png"
                _capture(page, page_path)

                # Check if we landed on a confirmation / "already applied" page
                if _is_confirmation_page(page):
                    print("Phenom: confirmation/already-applied page detected.", file=sys.stderr)
                    pre_submit_path = Path(payload["artifacts"]["pre_submit_screenshot"])
                    _capture(page, pre_submit_path)
                    # Write report with all filled fields before returning
                    payload["steps"] = all_filled
                    write_report(payload, board_name="phenom", runtime={"steps": all_filled})
                    outcome = {
                        "status": "confirmed",
                        "reason": "already_applied_or_confirmation",
                        "snapshot": _page_snapshot_fn(page),
                    }
                    sync_notion_after_submit(payload, outcome, provider="phenom")
                    reply_to_confirmation_email(payload, board_name="phenom")
                    return 0

                if current_page == PAGE_MY_INFO:
                    step_filled = _fill_my_information(page, profile, application_profile, out_dir)
                    all_filled.extend(step_filled)

                elif current_page == PAGE_EXPERIENCE:
                    step_filled = _fill_my_experience(page, out_dir)
                    all_filled.extend(step_filled)

                elif current_page == PAGE_APPLICATION_QUESTIONS:
                    step_filled = _fill_application_questions(page, out_dir, meta, payload.get("provider"))
                    all_filled.extend(step_filled)

                elif current_page in (PAGE_VOLUNTARY_DISCLOSURES, PAGE_SELF_IDENTITY):
                    step_filled = _fill_voluntary_disclosures(page, application_profile)
                    all_filled.extend(step_filled)

                elif current_page == PAGE_REVIEW:
                    # Take pre-submit screenshot
                    pre_submit_path = Path(payload["artifacts"]["pre_submit_screenshot"])
                    _capture(page, pre_submit_path)

                    # Write report with all filled fields
                    payload["steps"] = all_filled
                    write_report(payload, board_name="phenom", runtime={"steps": all_filled})

                    if not submit:
                        print(
                            f"Phenom: filled application for review: {pre_submit_path.relative_to(PROJECT_ROOT)}",
                            file=sys.stderr,
                        )
                        return 0

                    # Submit!
                    if not _click_submit_button(page):
                        print("Phenom: submit button not found on review page.", file=sys.stderr)
                        return 1

                    # Poll for confirmation — Adobe Phenom processes async
                    # and may take 30+ seconds to redirect to thank-you page.
                    submit_started_at_utc = datetime.now(UTC).replace(microsecond=0).isoformat()
                    email_watcher = build_email_confirmation_watcher(payload, min_received_at_utc=submit_started_at_utc)

                    for poll_i in range(60):
                        page.wait_for_timeout(1000)
                        # Dismiss feedback popups that may block confirmation
                        if poll_i % 5 == 0:
                            for popup_sel in (
                                "[class*='QSIPopOver'] button[class*='close']",
                                "[class*='feedback'] button[class*='close']",
                                "[class*='survey'] button[class*='close']",
                                "button[aria-label='Close']",
                            ):
                                try:
                                    popup = page.locator(popup_sel)
                                    if popup.count() and popup.first.is_visible():
                                        popup.first.click(timeout=1000)
                                except Exception:
                                    pass
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
                                provider="phenom",
                                email_confirmation=email_confirmation,
                                min_received_at_utc=submit_started_at_utc,
                            )
                            reply_to_confirmation_email(
                                payload,
                                board_name="phenom",
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
                                provider="phenom",
                                min_received_at_utc=submit_started_at_utc,
                            )
                            reply_to_confirmation_email(payload, board_name="phenom")
                            return 0
                        if state["status"] == "captcha_required":
                            print("Phenom submission: captcha detected, waiting for resolution...", file=sys.stderr)
                            _wait_result = wait_for_captcha_resolution(
                                page,
                                headless=headless,
                                payload=payload,
                                board_title="Phenom",
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
                                    provider="phenom",
                                    email_confirmation=_email_conf,
                                    min_received_at_utc=submit_started_at_utc,
                                )
                                reply_to_confirmation_email(
                                    payload,
                                    board_name="phenom",
                                    email_confirmation=_email_conf,
                                )
                                return 0
                            return CAPTCHA_SKIP_EXIT_CODE
                        if state["status"] == "validation_error":
                            # Check if submit button is disabled — means form
                            # was submitted and is processing. The Review page
                            # text may contain "Please Select" from displayed
                            # form values, triggering a false validation_error.
                            submit_btn_disabled = False
                            try:
                                submit_btn = page.locator("button[type='submit'], button.btn-submit")
                                if submit_btn.count():
                                    submit_btn_disabled = submit_btn.first.is_disabled()
                            except Exception:
                                pass
                            if submit_btn_disabled:
                                # Form was submitted, keep polling
                                continue
                            # Real validation error — wait and re-check
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
                                    provider="phenom",
                                    min_received_at_utc=submit_started_at_utc,
                                )
                                reply_to_confirmation_email(payload, board_name="phenom")
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
                            provider="phenom",
                            email_confirmation=email_confirmation,
                            min_received_at_utc=submit_started_at_utc,
                        )
                        reply_to_confirmation_email(
                            payload,
                            board_name="phenom",
                            email_confirmation=email_confirmation,
                        )
                        return 0

                    debug_html = Path(payload["artifacts"]["submit_debug_html"])
                    debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
                    debug_html.write_text(page.content(), encoding="utf-8")
                    _capture(page, debug_png)
                    print(
                        f"Phenom submit did not reach confirmed state. "
                        f"See {debug_html.relative_to(PROJECT_ROOT)} and "
                        f"{debug_png.relative_to(PROJECT_ROOT)}.",
                        file=sys.stderr,
                    )
                    return 1

                # Click Next to proceed to next page
                if current_page != PAGE_REVIEW:
                    if not _click_next_button(page):
                        # Maybe we're already past the wizard
                        if _is_confirmation_page(page):
                            snapshot = _page_snapshot_fn(page)
                            state = _classify_submit_state(snapshot)
                            if state["status"] == "confirmed":
                                outcome = {
                                    "status": "confirmed",
                                    "reason": "text",
                                    "snapshot": snapshot,
                                }
                                sync_notion_after_submit(payload, outcome, provider="phenom")
                                reply_to_confirmation_email(payload, board_name="phenom")
                                return 0
                        print("Phenom: could not find Next button.", file=sys.stderr)
                        debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
                        _capture(page, debug_png)

                    # Wait for page transition
                    page.wait_for_timeout(2000)
                    if terminal_apply_submit_result is not None:
                        _record_phenom_terminal_result(page, payload, out_dir, terminal_apply_submit_result)
                        print(terminal_apply_submit_result["message"], file=sys.stderr)
                        return 0

            # Exhausted max pages
            print("Phenom: exceeded max page attempts.", file=sys.stderr)
            return 1

        finally:
            browser.close()


# ─── Entry point ─────────────────────────────────────────────────────────────


def main() -> int:
    return autofill_main(
        board_name="phenom",
        build_payload_fn=_build_payload,
        run_browser_fn=_run_phenom_browser,
    )


if __name__ == "__main__":
    raise SystemExit(main())
