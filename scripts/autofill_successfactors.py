#!/usr/bin/env python3
"""SuccessFactors / Jobs2Web application autofill."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_submit_common import (
    APPLICATION_PROFILE_PATH,
    MASTER_RESUME_PATH,
    build_simple_payload,
    find_cover_letter_file,
    find_resume_file,
    load_meta,
    parse_application_profile,
    parse_master_resume,
    write_submission_result,
)
from autofill_common import capture_full_page, fill_basic_step
from autofill_pipeline import CAPTCHA_SKIP_EXIT_CODE, autofill_main, run_simple_board_pipeline
from output_layout import migrate_role_output_layout, role_submit_path
from project_env import load_project_env

_BOARD = "successfactors"
_FORM_SELECTOR = "form, input[name='username'], input[name='email'], button[type='submit']"
SUBMIT_BUTTON_NAMES = ("Submit Application", "Review and Submit", "Apply", "Continue")
_CONFIRM_PATTERNS = (
    re.compile(r"\bthank(?:s| you)\s+for\s+applying\b", re.I),
    re.compile(r"\bapplication submitted\b", re.I),
)
_REVIEW_PATTERNS = (
    re.compile(r"\bsubmit application\b", re.I),
    re.compile(r"\breview(?: and)? submit\b", re.I),
)
_VALIDATION_PATTERNS = (
    re.compile(r"\bthis field is required\b", re.I),
    re.compile(r"\bplease (?:enter|select|complete|fill)\b", re.I),
    re.compile(r"\binvalid\b", re.I),
    re.compile(r"\berror\b", re.I),
)
_AUTH_MESSAGES = {
    "sign_in_gate": "SuccessFactors stopped at a sign-in gate before reaching the application form.",
    "create_account_gate": "SuccessFactors stopped at an account-creation gate before reaching the application form.",
    "password_reset_gate": "SuccessFactors stopped at a password-reset gate before reaching the application form.",
}
_ENTRY_PATTERNS = (
    re.compile(r"^apply\b", re.I),
    re.compile(r"^continue\b", re.I),
    re.compile(r"^submit application\b", re.I),
)
_COOKIE_ACCEPT_PATTERNS = (
    re.compile(r"^understood$", re.I),
    re.compile(r"^accept all cookies$", re.I),
    re.compile(r"^accept$", re.I),
)

load_project_env()


def classify_successfactors_auth_state(*, html: str, url: str, page_title: str = "") -> str | None:
    lowered = html.casefold()
    url_lower = url.casefold()
    title_lower = page_title.casefold()
    combined = " ".join(part for part in (lowered, url_lower, title_lower) if part)
    if "login_ns=register" in url_lower or "create an account" in lowered:
        return "create_account_gate"
    if "forgot your password" in combined or "reset password" in combined:
        return "password_reset_gate"
    if "career opportunities: sign in" in title_lower:
        return "sign_in_gate"
    if "sign in" in combined and ("password" in combined or "username" in combined):
        return "sign_in_gate"
    return None


def successfactors_redirected_to_careers_home(*, html: str, url: str, page_title: str = "") -> bool:
    combined = " ".join(part for part in (html, page_title, url) if part).casefold()
    careers_home_markers = (
        "find your next role" in combined,
        "search jobs" in combined and "work area" in combined and "career status" in combined and "country" in combined,
        "search jobs" in combined and "featured jobs" in combined and "join our talent network" in combined,
    )
    if not any(careers_home_markers):
        return False
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    title_lower = page_title.casefold()
    return not path and ("jobs at " in title_lower or "careers" in title_lower or "search jobs" in combined)


def _classify_submit_state(*, html: str, url: str, page_title: str = "") -> str:
    combined = " ".join(part for part in (html, url, page_title) if part).casefold()
    if any(pattern.search(combined) for pattern in _CONFIRM_PATTERNS):
        return "confirmed"
    if any(pattern.search(combined) for pattern in _VALIDATION_PATTERNS):
        return "validation_error"
    if any(pattern.search(combined) for pattern in _REVIEW_PATTERNS):
        return "review"
    return "unknown"


def _classify_snapshot(snapshot: dict) -> dict[str, object]:
    page_text = str(snapshot.get("page_text") or "")
    page_url = str(snapshot.get("url") or "")
    status = _classify_submit_state(html=page_text, url=page_url, page_title=str(snapshot.get("page_title") or ""))
    if status == "confirmed":
        return {"status": "confirmed", "reason": "text"}
    if snapshot.get("recaptcha_visible") and snapshot.get("recaptcha_challenge_active"):
        return {"status": "captcha_required", "reason": "challenge"}
    if status == "validation_error" or snapshot.get("errors") or snapshot.get("invalid_fields"):
        return {
            "status": "validation_error",
            "errors": list(snapshot.get("errors") or snapshot.get("invalid_fields") or [page_text or page_url]),
        }
    if status == "review":
        return {"status": "review", "reason": "text"}
    return {"status": "unknown", "reason": "no_match"}


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
        notes=["SuccessFactors / Jobs2Web flow. Stop before live submit in draft mode."],
    )


def _maybe_enter_application(page) -> bool:
    for pattern in _COOKIE_ACCEPT_PATTERNS:
        try:
            cookie_button = page.get_by_role("button", name=pattern).first
            if cookie_button.count() and cookie_button.is_visible():
                cookie_button.click()
                page.wait_for_timeout(250)
                break
        except Exception:
            continue

    for role in ("link", "button"):
        for pattern in _ENTRY_PATTERNS:
            try:
                locator = page.get_by_role(role, name=pattern).first
                if locator.count() and locator.is_visible():
                    href = ""
                    try:
                        href = str(locator.get_attribute("href") or "").strip()
                    except Exception:
                        href = ""
                    if role == "link" and href:
                        page.goto(urljoin(page.url, href), wait_until="domcontentloaded", timeout=30000)
                    else:
                        locator.click()
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        pass
                    page.wait_for_timeout(1500)
                    return True
            except Exception:
                continue
    return False


def _run_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    out_dir = Path(payload["out_dir"])

    def _post_navigate_hook(page) -> None:
        _maybe_enter_application(page)
        page.wait_for_timeout(1000)

        try:
            current_payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except Exception:
            current_payload = payload
        current_payload["application_url"] = page.url
        payload_path.write_text(json.dumps(current_payload, indent=2) + "\n", encoding="utf-8")

        try:
            html = page.content()
        except Exception:
            html = ""
        try:
            page_title = page.title()
        except Exception:
            page_title = ""
        auth_state = classify_successfactors_auth_state(html=html, url=page.url, page_title=page_title)
        debug_html = role_submit_path(out_dir, "successfactors_submit_debug.html")
        debug_png = role_submit_path(out_dir, "successfactors_submit_debug.png")
        if auth_state is not None:
            debug_html.write_text(html, encoding="utf-8")
            capture_full_page(page, debug_png, preferred_selectors=("form", "main"))
            write_submission_result(
                out_dir=out_dir,
                status="auth_unknown",
                failure_type="auth_unknown",
                job_url=str(current_payload.get("job_url") or page.url),
                message=_AUTH_MESSAGES.get(auth_state, "SuccessFactors stopped at an authentication gate."),
                auth_state=auth_state,
                auth_scope="application",
                board=str(current_payload.get("board") or _BOARD),
                provider=str(current_payload.get("provider") or "").strip() or None,
                artifacts={"page_screenshot": str(debug_png)},
            )
            return
        if not successfactors_redirected_to_careers_home(html=html, url=page.url, page_title=page_title):
            return
        debug_html.write_text(html, encoding="utf-8")
        capture_full_page(page, debug_png, preferred_selectors=("form", "main"))
        write_submission_result(
            out_dir=out_dir,
            status="unknown",
            failure_type="unknown",
            job_url=str(current_payload.get("job_url") or page.url),
            message="SuccessFactors redirected to the generic careers home/search page instead of an application form.",
            board=str(current_payload.get("board") or _BOARD),
            provider=str(current_payload.get("provider") or "").strip() or None,
            artifacts={"page_screenshot": str(debug_png)},
        )

    rc = run_simple_board_pipeline(
        payload_path,
        headless=headless,
        submit=submit,
        board_name=_BOARD,
        form_selector=_FORM_SELECTOR,
        submit_button_names=SUBMIT_BUTTON_NAMES,
        classify_state_fn=_classify_snapshot,
        fill_step_fn=fill_basic_step,
        preferred_capture_selectors=("form", "main"),
        post_navigate_hook=_post_navigate_hook,
    )
    return CAPTCHA_SKIP_EXIT_CODE if rc == 1 and role_submit_path(out_dir, "application_submission_result.json").exists() else rc


def main() -> int:
    return autofill_main(_BOARD, _build_payload, run_browser_fn=_run_browser)


if __name__ == "__main__":
    raise SystemExit(main())
