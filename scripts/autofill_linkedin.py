#!/usr/bin/env python3
"""LinkedIn Easy Apply autofill — multi-step wizard via persistent LinkedIn profile."""

from __future__ import annotations

import fcntl
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
_LINKEDIN_PROFILE_DIR = PROJECT_ROOT / ".playwright-linkedin"
_LINKEDIN_LOCK_FILE = PROJECT_ROOT / ".playwright-linkedin.lock"
_LINKEDIN_ZOOM_HOSTS = ("linkedin.com", "www.linkedin.com")

# ── Board constants ──────────────────────────────────────────────────────────
from application_submit_common import reply_to_confirmation_email
from autofill_common import (
    board_file_constants,
    capture_full_page,
    capture_locator_screenshot,
    capture_scrollable_locator_screenshot,
    concatenate_images_vertically,
    select_shared_policy_option,
)
from linkedin_submission_results import (
    _linkedin_job_closed_reason,
    _submission_result_path,
    _write_already_applied_result,
    _write_job_closed_result,
    _write_not_easy_apply_result,
)

_BOARD = "linkedin"
_BOARD_CONSTANTS = board_file_constants(_BOARD)
_LINKEDIN_VALIDATION_RETRY_LIMIT = 3


def _clear_current_attempt_linkedin_artifacts(payload: dict) -> None:
    from autofill_common import clear_current_attempt_artifacts

    clear_current_attempt_artifacts(payload)


def _capture_debug_screenshot(page, payload: dict) -> Path:
    """Capture the canonical current-attempt debug screenshot path."""

    debug_path = Path(payload["artifacts"]["submit_debug_screenshot"])
    try:
        _capture_linkedin_surface_screenshot(page, debug_path)
    except Exception:
        pass
    return debug_path


def _capture_not_easy_apply_screenshot(page, out_dir: Path, *, reason: str) -> Path | None:
    """Capture the current page when LinkedIn no longer exposes Easy Apply."""

    filename_by_reason = {
        "external_apply": "linkedin_external_apply_page.png",
        "no_apply_button": "linkedin_no_apply_debug.png",
    }
    filename = filename_by_reason.get(reason)
    if not filename:
        return None

    screenshot_path = out_dir / "submit" / filename
    try:
        _capture_linkedin_surface_screenshot(page, screenshot_path)
    except Exception:
        return None
    return screenshot_path


def _write_failed_result(
    out_dir: Path,
    payload: dict,
    *,
    failure_type: str,
    message: str,
    retry_class: str = "none",
    step_num: int | None = None,
    step_screenshot: Path | None = None,
    validation_errors: list[str] | None = None,
) -> None:
    """Persist a classified LinkedIn failure result for the current attempt."""

    submit_debug = Path(payload["artifacts"]["submit_debug_screenshot"])
    artifacts: dict[str, str] = {}
    if submit_debug.exists():
        artifacts["submit_debug_screenshot"] = str(submit_debug)
    if step_screenshot is not None and Path(step_screenshot).exists():
        artifacts["step_screenshot"] = str(step_screenshot)

    result = {
        "status": "failed",
        "board": _BOARD,
        "job_url": payload.get("job_url", ""),
        "company": payload.get("company", ""),
        "job_title": payload.get("job_title", ""),
        "failure_type": failure_type,
        "message": message,
        "retry_class": retry_class,
        "updated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }
    if step_num is not None:
        result["step_num"] = step_num
    if validation_errors:
        result["validation_errors"] = list(validation_errors)
    if artifacts:
        result["artifacts"] = artifacts

    result_path = _submission_result_path(out_dir)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def _build_payload(out_dir: Path, provider: str | None = None) -> dict:
    """Build a minimal payload — wizard steps are discovered at runtime."""
    from application_submit_common import (
        APPLICATION_PROFILE_PATH,
        MASTER_RESUME_PATH,
        find_cover_letter_file,
        find_resume_file,
        parse_application_profile,
        parse_master_resume,
    )
    from output_layout import role_documents_path, role_submit_path

    meta_path = out_dir / ".pipeline_meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    resume_md = out_dir / "content" / "master_resume.md"
    candidate_profile_path = resume_md if resume_md.exists() else MASTER_RESUME_PATH
    candidate_profile = (
        parse_master_resume(candidate_profile_path.read_text(encoding="utf-8"))
        if candidate_profile_path.exists()
        else None
    )

    app_profile_path = out_dir / "content" / "application_profile.md"
    application_profile_path = app_profile_path if app_profile_path.exists() else APPLICATION_PROFILE_PATH
    application_profile = (
        parse_application_profile(application_profile_path.read_text(encoding="utf-8"))
        if application_profile_path.exists()
        else None
    )

    company_display_name = str(meta.get("company_proper", meta.get("company", "")) or "").strip()

    # Prefer the current role's employer-named resume asset when it exists.
    resume_pdf = None
    if company_display_name:
        for preferred_path in (
            role_submit_path(out_dir, f"Jerrison Li Resume - {company_display_name}.pdf"),
            role_documents_path(out_dir, f"Jerrison Li Resume - {company_display_name}.pdf"),
            out_dir / f"Jerrison Li Resume - {company_display_name}.pdf",
        ):
            if preferred_path.exists():
                resume_pdf = str(preferred_path)
                break
    if resume_pdf is None:
        for pattern in ("submit/*.pdf", "documents/*.pdf", "*.pdf", "submit/*.docx", "documents/*.docx", "*.docx"):
            matches = sorted(out_dir.glob(pattern))
            for match in matches:
                if "cover" not in match.stem.lower():
                    resume_pdf = str(match)
                    break
            if resume_pdf:
                break
    if resume_pdf is None:
        try:
            resume_pdf = str(find_resume_file(out_dir))
        except FileNotFoundError:
            resume_pdf = None

    # Prefer the current role's employer-named cover letter asset when it exists.
    cover_letter_pdf = None
    if company_display_name:
        for preferred_path in (
            role_submit_path(out_dir, f"Jerrison Li Cover Letter - {company_display_name}.pdf"),
            role_documents_path(out_dir, f"Jerrison Li Cover Letter - {company_display_name}.pdf"),
            out_dir / f"Jerrison Li Cover Letter - {company_display_name}.pdf",
            role_submit_path(out_dir, f"Jerrison Li Cover Letter - {company_display_name}.docx"),
            role_documents_path(out_dir, f"Jerrison Li Cover Letter - {company_display_name}.docx"),
            out_dir / f"Jerrison Li Cover Letter - {company_display_name}.docx",
        ):
            if preferred_path.exists():
                cover_letter_pdf = str(preferred_path)
                break
    if cover_letter_pdf is None:
        for pattern in ("submit/*.pdf", "documents/*.pdf", "*.pdf", "submit/*.docx", "documents/*.docx", "*.docx"):
            matches = sorted(out_dir.glob(pattern))
            for match in matches:
                if "cover" in match.stem.lower():
                    cover_letter_pdf = str(match)
                    break
            if cover_letter_pdf:
                break
    if cover_letter_pdf is None:
        try:
            cover_letter_pdf = str(find_cover_letter_file(out_dir))
        except FileNotFoundError:
            cover_letter_pdf = None

    return {
        "board": _BOARD,
        "job_url": meta.get("jd_url", meta.get("jd_source", meta.get("board_url", meta.get("url", "")))),
        "out_dir": str(out_dir),
        "job_title": meta.get("role", ""),
        "company": company_display_name,
        "candidate_name": candidate_profile.full_name if candidate_profile else "",
        "candidate_email": candidate_profile.email if candidate_profile else "",
        "candidate_phone": candidate_profile.phone if candidate_profile else "",
        "candidate_location": (
            candidate_profile.location if candidate_profile else getattr(application_profile, "location", "") or ""
        ),
        "candidate_linkedin": (candidate_profile.linkedin if candidate_profile and candidate_profile.linkedin else "")
        or getattr(application_profile, "linkedin", "")
        or "",
        "candidate_website": (candidate_profile.website if candidate_profile and candidate_profile.website else "")
        or getattr(application_profile, "website", "")
        or "",
        "resume_path": resume_pdf,
        "cover_letter_path": cover_letter_pdf,
        "mode": "review-before-submit",
        "artifacts": {
            "report_markdown": str(role_submit_path(out_dir, _BOARD_CONSTANTS["report_md"])),
            "report_json": str(role_submit_path(out_dir, _BOARD_CONSTANTS["report_json"])),
            "pre_submit_screenshot": str(role_submit_path(out_dir, _BOARD_CONSTANTS["pre_submit_screenshot"])),
            "post_submit_screenshot": str(role_submit_path(out_dir, _BOARD_CONSTANTS["post_submit_screenshot"])),
            "page_screenshots_dir": str(role_submit_path(out_dir, _BOARD_CONSTANTS["page_screenshots_dir"])),
            "unknown_questions_json": str(role_submit_path(out_dir, _BOARD_CONSTANTS["unknown_questions_json"])),
            "submit_debug_html": str(role_submit_path(out_dir, _BOARD_CONSTANTS["submit_debug_html"])),
            "submit_debug_screenshot": str(role_submit_path(out_dir, _BOARD_CONSTANTS["submit_debug_screenshot"])),
            "payload_json": str(role_submit_path(out_dir, _BOARD_CONSTANTS["payload_json"])),
        },
        "steps": [],
        "fields": [],
        "unknown_questions": [],
    }


def _run_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    """Custom browser pipeline for LinkedIn Easy Apply wizard."""
    payload = json.loads(payload_path.read_text())
    out_dir = Path(payload["out_dir"])

    # LinkedIn Easy Apply always runs headed (auth challenges, captcha)
    headless = False

    lock_fd = open(_LINKEDIN_LOCK_FILE, "w")  # noqa: SIM115
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        return _run_easy_apply_wizard(payload, out_dir, headless, submit)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _run_easy_apply_wizard(
    payload: dict,
    out_dir: Path,
    headless: bool,
    submit: bool,
) -> int:
    """Navigate the LinkedIn Easy Apply multi-step wizard."""
    from browser_runtime import (
        launch_chromium_browser,
        normalize_chromium_profile_zoom,
        submit_slow_mo_ms,
        submit_viewport,
    )
    from playwright.sync_api import sync_playwright

    artifacts = payload["artifacts"]
    pages_dir = Path(artifacts["page_screenshots_dir"])
    pages_dir.mkdir(parents=True, exist_ok=True)

    _LINKEDIN_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    normalize_chromium_profile_zoom(
        _LINKEDIN_PROFILE_DIR,
        hosts=_LINKEDIN_ZOOM_HOSTS,
        reset_default_zoom=True,
    )

    with sync_playwright() as pw:
        viewport = submit_viewport()
        browser = launch_chromium_browser(
            pw,
            headless=headless,
            slow_mo=submit_slow_mo_ms(headless),
            channel_env_var="JOB_ASSETS_SUBMIT_BROWSER_CHANNEL",
            executable_env_var="JOB_ASSETS_SUBMIT_BROWSER_EXECUTABLE",
            persistent_profile_dir=str(_LINKEDIN_PROFILE_DIR),
            prefer_local_browser=True,
            viewport=viewport,
            device_scale_factor=2,
            purpose="LinkedIn autofill",
        )
        page = browser.new_page(viewport=viewport, device_scale_factor=2)
        try:
            return _wizard_flow(
                page,
                payload,
                out_dir,
                submit,
                pages_dir=pages_dir,
            )
        finally:
            browser.close()


def _dismiss_discard_dialog(page, step_num: int) -> bool:
    """Dismiss LinkedIn's 'discard previous application' confirmation overlay.

    Clicks the secondary "Keep"/"Continue" button to resume the previous
    application rather than "Discard" (which closes the wizard and loops).

    Returns True if a dialog was found and dismissed.
    """
    discard_dialog = page.locator(
        'div[data-test-modal-id="data-test-easy-apply-discard-confirmation"], '
        "div.artdeco-modal-overlay--layer-confirmation"
    ).first
    if discard_dialog.is_visible(timeout=2000):
        # Prefer the secondary/dismiss button ("Keep", "Continue editing")
        # over the primary "Discard" button to avoid closing the wizard
        keep_btn = discard_dialog.locator(
            'button[data-test-dialog-secondary-btn], button:has-text("Keep"), button:has-text("Continue")'
        ).first
        if keep_btn.is_visible(timeout=2000):
            log.info("keeping previous application at step %d", step_num)
            keep_btn.click()
            page.wait_for_timeout(1000)
            return True
        # Fallback: hide the overlay via JS (don't remove — removing breaks the wizard)
        log.info("hiding discard overlay via JS at step %d", step_num)
        page.evaluate("""() => {
            document.querySelectorAll('[data-test-modal-id="data-test-easy-apply-discard-confirmation"]')
                .forEach(el => { el.style.display = 'none'; el.style.pointerEvents = 'none'; });
            document.querySelectorAll('.artdeco-modal-overlay--layer-confirmation')
                .forEach(el => { el.style.display = 'none'; el.style.pointerEvents = 'none'; });
        }""")
        page.wait_for_timeout(500)
        return True
    return False


def _easy_apply_button(page):
    selector_specs = (
        ('button.jobs-apply-button:has-text("Easy Apply")', None),
        ('a.jobs-apply-button[href]:not([href*="/jobs/collections/similar-jobs/"]):has-text("Easy Apply")', None),
        ('button[aria-label*="Easy Apply to this job"]', None),
        ('a[href]:not([href*="/jobs/collections/similar-jobs/"])[aria-label*="Easy Apply to this job"]', None),
        ('button[aria-label*="Easy Apply"]', "Easy Apply"),
        ('a[href]:not([href*="/jobs/collections/similar-jobs/"])[aria-label*="Easy Apply"]', "Easy Apply"),
        ('button:has-text("Easy Apply")', "Easy Apply"),
        ('a[href]:not([href*="/jobs/collections/similar-jobs/"]):has-text("Easy Apply")', "Easy Apply"),
    )
    return _select_linkedin_apply_control(page, selector_specs, fallback_selector=selector_specs[0][0])


def _external_apply_button(page):
    selector_specs = (
        ('button.jobs-apply-button:has-text("Apply")', None),
        ('a.jobs-apply-button[href]:not([href*="/jobs/collections/similar-jobs/"]):has-text("Apply")', None),
        ('button[aria-label*="Apply to this job"]', None),
        ('a[href]:not([href*="/jobs/collections/similar-jobs/"])[aria-label*="Apply to this job"]', None),
        ('button[aria-label*="Apply"]', "Apply"),
        ('a[href]:not([href*="/jobs/collections/similar-jobs/"]):has-text("Apply")', "Apply"),
    )
    return _select_linkedin_apply_control(page, selector_specs, fallback_selector=selector_specs[0][0])


def _normalize_linkedin_control_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _locator_matches_exact_control_label(locator, label: str) -> bool:
    expected = _normalize_linkedin_control_text(label)
    if not expected:
        return False

    try:
        locator_text = _normalize_linkedin_control_text(locator.inner_text())
    except Exception:
        locator_text = ""
    if locator_text == expected:
        return True

    try:
        aria_label = _normalize_linkedin_control_text(locator.get_attribute("aria-label"))
    except Exception:
        aria_label = ""
    return aria_label in {expected, f"{expected} to this job"}


def _select_linkedin_apply_control(page, selector_specs: tuple[tuple[str, str | None], ...], *, fallback_selector: str):
    first_present = None
    for selector, exact_label in selector_specs:
        locator = page.locator(selector)
        try:
            count = locator.count()
        except Exception:
            continue
        for idx in range(count):
            candidate = locator.nth(idx)
            if exact_label is not None and not _locator_matches_exact_control_label(candidate, exact_label):
                continue
            if first_present is None:
                first_present = candidate
            if _locator_is_visible(candidate, timeout=0):
                return candidate
    return first_present if first_present is not None else page.locator(fallback_selector).first


def _easy_apply_modal(page):
    return page.locator("div.jobs-easy-apply-modal, div[data-test-modal], div.artdeco-modal").first


def _scroll_visible_apply_control_into_view(page) -> None:
    for locator_factory in (_easy_apply_button, _external_apply_button):
        try:
            locator = locator_factory(page)
        except Exception:
            continue
        if not _locator_is_visible(locator, timeout=0):
            continue
        try:
            locator.scroll_into_view_if_needed(timeout=1000)
        except Exception:
            pass
        return


def _first_visible_locator(page, selectors: tuple[str, ...]):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
        except Exception:
            continue
        try:
            if locator.count() and _locator_is_visible(locator, timeout=0):
                return locator
        except Exception:
            continue
    return None


def _capture_linkedin_modal_composite_screenshot(page, output_path: Path) -> bool:
    modal_locator = _easy_apply_modal(page)
    if not _locator_is_visible(modal_locator, timeout=0):
        return False

    content_locator = _first_visible_locator(
        page,
        (
            "div.jobs-easy-apply-modal__content",
            "div.artdeco-modal__content.jobs-easy-apply-modal__content",
            "div.artdeco-modal__content",
        ),
    )
    if content_locator is None:
        capture_scrollable_locator_screenshot(page, modal_locator, output_path)
        return True

    header_locator = _first_visible_locator(page, ("div.artdeco-modal__header",))
    if header_locator is None:
        capture_scrollable_locator_screenshot(page, content_locator, output_path)
        return True

    header_path = output_path.with_name(f"{output_path.stem}__header.png")
    content_path = output_path.with_name(f"{output_path.stem}__content.png")
    try:
        capture_locator_screenshot(header_locator, header_path)
        capture_scrollable_locator_screenshot(page, content_locator, content_path)
        concatenate_images_vertically([header_path, content_path], output_path)
    finally:
        for temp_path in (header_path, content_path):
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
    return True


def _capture_linkedin_structural_surface_screenshot(page, output_path: Path) -> bool:
    structural_selectors = (
        "main#workspace > div > div",
        "main#workspace > div",
        "#workspace > div > div",
        "#workspace > div",
        "main.scaffold-layout__main",
        "div.scaffold-layout__main",
        "div.jobs-search__job-details--wrapper",
        "div.jobs-search__job-details--container",
        "main",
    )
    if _first_visible_locator(page, structural_selectors) is not None:
        try:
            capture_full_page(page, output_path, preferred_selectors=structural_selectors)
            return True
        except Exception:
            pass

    for selector in (
        "div.job-details-jobs-unified-top-card__container--two-pane",
        "div.job-details-jobs-unified-top-card__container",
        "div.jobs-unified-top-card",
    ):
        locator = _first_visible_locator(page, (selector,))
        if locator is None:
            continue
        capture_locator_screenshot(locator, output_path)
        return True
    return False


def _capture_linkedin_surface_screenshot(page, output_path: Path, *, prefer_modal: bool = False) -> Path:
    """Capture the most relevant LinkedIn surface instead of the full viewport.

    LinkedIn evidence screenshots are high-resolution, but full-page viewport
    captures make the actual application state hard to read. Prefer the Easy
    Apply modal when available, otherwise crop the primary job-detail surface.
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if prefer_modal:
        try:
            if _capture_linkedin_modal_composite_screenshot(page, output_path):
                return output_path
        except Exception:
            pass

    try:
        if _capture_linkedin_structural_surface_screenshot(page, output_path):
            return output_path
    except Exception:
        pass

    _scroll_visible_apply_control_into_view(page)
    page.screenshot(path=str(output_path), full_page=False)
    return output_path


def _locator_is_visible(locator, *, timeout: int) -> bool:
    try:
        return locator.is_visible(timeout=timeout)
    except Exception:
        return False


def _attempt_reopen_easy_apply_modal(page, step_num: int) -> bool:
    modal = _easy_apply_modal(page)
    if _locator_is_visible(modal, timeout=1000):
        return True

    easy_apply_btn = _easy_apply_button(page)
    if not _locator_is_visible(easy_apply_btn, timeout=1500):
        return False

    for _ in range(2):
        try:
            easy_apply_btn.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            pass
        try:
            easy_apply_btn.click(force=True)
        except Exception:
            try:
                easy_apply_btn.click()
            except Exception:
                continue
        page.wait_for_timeout(1500)
        _dismiss_discard_dialog(page, step_num)
        if _locator_is_visible(modal, timeout=3000):
            log.info("recovered Easy Apply modal at step %d", step_num)
            return True

    return False


class _ResumeUploadVerificationError(RuntimeError):
    """Raised when LinkedIn exposes a visible resume upload path but verification fails."""

    def __init__(self, message: str, *, filled_steps: list[dict] | None = None) -> None:
        super().__init__(message)
        self.filled_steps = list(filled_steps or [])


def _visible_texts(locator) -> list[str]:
    texts: list[str] = []
    for index in range(locator.count()):
        candidate = locator.nth(index)
        try:
            if not candidate.is_visible(timeout=0):
                continue
        except Exception:
            continue
        try:
            text = " ".join(candidate.inner_text().split())
        except Exception:
            continue
        if text:
            texts.append(text)
    return texts


def _expected_resume_name(payload: dict) -> str:
    resume_path = payload.get("resume_path")
    return Path(resume_path).name if resume_path else ""


def _resume_text_matches_expected(text: str, payload: dict) -> bool:
    from application_submit_common import normalize_text

    normalized_text = normalize_text(text)
    if not normalized_text:
        return False

    expected_name = _expected_resume_name(payload)
    expected_stem = Path(expected_name).stem if expected_name else ""
    expected_company = str(payload.get("company") or "").strip()
    candidates = [expected_name, expected_stem, expected_company]
    for candidate in candidates:
        normalized_candidate = normalize_text(candidate)
        if normalized_candidate and normalized_candidate in normalized_text:
            return True
    return False


def _collect_linkedin_resume_markers(page, modal) -> dict:
    modal_text = ""
    try:
        modal_text = " ".join(modal.inner_text().split())
    except Exception:
        modal_text = ""

    selected_buttons = _visible_texts(
        modal.locator(
            'button:has-text("Deselect resume"), a:has-text("Deselect resume"), label:has-text("Deselect resume")'
        )
    )
    selection_buttons = _visible_texts(
        modal.locator('button:has-text("Select resume"), a:has-text("Select resume"), label:has-text("Select resume")')
    )
    upload_buttons = _visible_texts(
        modal.locator(
            'button:has-text("Upload resume"), '
            'button:has-text("Change resume"), '
            'button:has-text("Replace resume"), '
            'a:has-text("Upload resume"), '
            'a:has-text("Change resume"), '
            'a:has-text("Replace resume"), '
            'label:has-text("Upload resume"), '
            'label:has-text("Change resume"), '
            'label:has-text("Replace resume")'
        )
    )
    file_inputs = page.locator('input[type="file"]')
    file_input_markers = []
    for index in range(file_inputs.count()):
        input_locator = file_inputs.nth(index)
        try:
            visible = input_locator.is_visible(timeout=0)
        except Exception:
            visible = False
        file_input_markers.append({"visible": visible})

    return {
        "modalText": modal_text,
        "buttons": upload_buttons + selection_buttons + selected_buttons,
        "checked": selected_buttons,
        "fileInputs": file_input_markers,
    }


def _classify_linkedin_resume_markers(markers: dict, payload: dict) -> dict:
    from application_submit_common import normalize_text

    modal_text = str(markers.get("modalText", "") or "")
    button_texts = [str(text) for text in markers.get("buttons", []) if str(text).strip()]
    selected_texts = [str(text) for text in markers.get("checked", []) if str(text).strip()]
    file_inputs = markers.get("fileInputs", []) or []

    normalized_modal = normalize_text(modal_text)
    resume_step_visible = "resume" in normalized_modal or any(
        "resume" in normalize_text(text) for text in button_texts + selected_texts
    )
    visible_upload_path = any(
        "resume" in normalize_text(text)
        and any(token in normalize_text(text) for token in ("upload", "change", "replace"))
        for text in button_texts
    ) or any(bool(item.get("visible")) for item in file_inputs)

    selected_expected = any(_resume_text_matches_expected(text, payload) for text in selected_texts)
    selectable_expected = any(
        _resume_text_matches_expected(text, payload) and "select resume" in normalize_text(text)
        for text in button_texts
    )

    return {
        "resume_step_visible": resume_step_visible,
        "visible_upload_path": visible_upload_path,
        "selected_expected": selected_expected,
        "selectable_expected": selectable_expected,
        "selected_texts": selected_texts,
        "button_texts": button_texts,
    }


def _select_expected_resume_button(modal, payload: dict) -> bool:
    buttons = modal.locator(
        'button:has-text("Select resume"), a:has-text("Select resume"), label:has-text("Select resume")'
    )
    for index in range(buttons.count()):
        button = buttons.nth(index)
        try:
            if not button.is_visible(timeout=0):
                continue
            text = " ".join(button.inner_text().split())
        except Exception:
            continue
        if not _resume_text_matches_expected(text, payload):
            continue
        button.click(force=True)
        modal.page.wait_for_timeout(750)
        return True
    return False


def _attempt_resume_upload_through_visible_control(page, modal, payload: dict) -> tuple[bool, str | None]:
    resume_path = payload.get("resume_path")
    if not resume_path:
        return False, "missing_resume_path"

    file_inputs = page.locator('input[type="file"]')
    if file_inputs.count():
        file_inputs.first.set_input_files(resume_path)
        page.wait_for_timeout(1500)
        return True, None

    upload_button = modal.locator(
        'button:has-text("Upload resume"), '
        'button:has-text("Change resume"), '
        'button:has-text("Replace resume"), '
        'a:has-text("Upload resume"), '
        'a:has-text("Change resume"), '
        'a:has-text("Replace resume"), '
        'label:has-text("Upload resume"), '
        'label:has-text("Change resume"), '
        'label:has-text("Replace resume")'
    ).first
    if not upload_button.count():
        return False, "no_visible_resume_upload_path"

    try:
        with page.expect_file_chooser(timeout=5000) as chooser_info:
            upload_button.click(force=True)
        chooser_info.value.set_files(resume_path)
        page.wait_for_timeout(2000)
        return True, None
    except Exception:
        page.wait_for_timeout(500)
        file_inputs = page.locator('input[type="file"]')
        if file_inputs.count():
            file_inputs.first.set_input_files(resume_path)
            page.wait_for_timeout(2000)
            return True, None
        return False, "resume_upload_control_did_not_expose_file_chooser"


def _verify_expected_resume_selection(page, modal, payload: dict) -> dict:
    for _ in range(6):
        markers = _collect_linkedin_resume_markers(page, modal)
        state = _classify_linkedin_resume_markers(markers, payload)
        if state["selected_expected"]:
            return state
        if state["selectable_expected"] and _select_expected_resume_button(modal, payload):
            markers = _collect_linkedin_resume_markers(page, modal)
            state = _classify_linkedin_resume_markers(markers, payload)
            if state["selected_expected"]:
                return state
        page.wait_for_timeout(500)
    return _classify_linkedin_resume_markers(_collect_linkedin_resume_markers(page, modal), payload)


def _record_verified_resume_step(filled_steps: list[dict], payload: dict) -> None:
    if any(step.get("field_name") == "resume" and step.get("filled") for step in filled_steps):
        return
    filled_steps.append(
        {
            "field_name": "resume",
            "label": "upload resume",
            "kind": "file",
            "value": payload.get("resume_path", ""),
            "source": "generated_resume",
            "filled": True,
            "required": True,
        }
    )


def _resume_outcomes(resume_runtime: dict, payload: dict) -> list[dict]:
    status = str(resume_runtime.get("status") or "").strip()
    expected_file = _expected_resume_name(payload)
    observed_selection_labels = list(resume_runtime.get("observed_selection_labels") or [])

    if status == "verified_fresh_upload":
        return [
            {
                "name": "resume_upload",
                "status": "verified_fresh_upload",
                "expected_file": expected_file,
                "message": "LinkedIn exposed a visible resume upload path and the current role resume was re-uploaded and selected.",
                "observed_selection_labels": observed_selection_labels,
            }
        ]
    if status == "upload_verification_failed":
        return [
            {
                "name": "resume_upload",
                "status": "upload_verification_failed",
                "expected_file": expected_file,
                "message": resume_runtime.get("message")
                or "LinkedIn exposed a visible resume upload path, but the live UI never confirmed the expected resume.",
                "observed_selection_labels": observed_selection_labels,
            }
        ]
    return [
        {
            "name": "resume_upload",
            "status": "review_without_visible_resume_controls",
            "expected_file": expected_file,
            "message": "Review was reached without a visible LinkedIn resume upload or replace path, so the draft continued without fresh-upload verification.",
            "observed_selection_labels": observed_selection_labels,
        }
    ]


def _handle_linkedin_resume_step(page, modal, payload: dict, filled_steps: list[dict], resume_runtime: dict) -> None:
    markers = _collect_linkedin_resume_markers(page, modal)
    state = _classify_linkedin_resume_markers(markers, payload)
    if not state["resume_step_visible"]:
        return

    resume_runtime["observed_selection_labels"] = state["selected_texts"]
    if not state["visible_upload_path"]:
        return

    resume_runtime["visible_upload_path_seen"] = True
    if resume_runtime.get("status") == "verified_fresh_upload":
        return

    uploaded, reason = _attempt_resume_upload_through_visible_control(page, modal, payload)
    verification = _verify_expected_resume_selection(page, modal, payload)
    resume_runtime["observed_selection_labels"] = verification["selected_texts"]
    if uploaded and verification["selected_expected"]:
        resume_runtime["status"] = "verified_fresh_upload"
        _record_verified_resume_step(filled_steps, payload)
        return

    failure_steps = [
        {
            "field_name": "resume",
            "label": "upload resume",
            "kind": "file",
            "value": payload.get("resume_path", ""),
            "source": "generated_resume",
            "required": True,
            "status": "upload_verification_failed",
        }
    ]
    resume_runtime["status"] = "upload_verification_failed"
    resume_runtime["message"] = (
        "LinkedIn exposed a visible resume upload path, but the expected resume did not become the selected attachment"
        f" after the upload attempt ({reason or 'unknown reason'})."
    )
    raise _ResumeUploadVerificationError(resume_runtime["message"], filled_steps=failure_steps)


def _click_next_button_with_fallback(next_btn, *, step_num: int) -> None:
    click_error: Exception | None = None
    try:
        next_btn.click(force=True, timeout=3000, no_wait_after=True)
        return
    except Exception as click_exc:
        click_error = click_exc
        log.warning("LinkedIn next-button click timed out at step %d; falling back: %s", step_num, click_exc)

    try:
        next_btn.dispatch_event("click")
    except Exception as dispatch_exc:
        if click_error is not None:
            raise click_error from dispatch_exc
        raise


def _wizard_flow(
    page,
    payload: dict,
    out_dir: Path,
    submit: bool,
    *,
    pages_dir: Path,
) -> int:
    """Core wizard logic — navigate, fill, screenshot, submit."""
    from autofill_common import wait_for_captcha_resolution, write_report

    job_url = payload["job_url"]
    artifacts = payload["artifacts"]

    _clear_current_attempt_linkedin_artifacts(payload)

    # ── Step 1: Navigate to job page ─────────────────────────────────────
    log.info("navigating to %s", job_url)
    page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)

    # Handle auth wall
    if "authwall" in page.url or "/login" in page.url:
        from url_resolver import _ensure_linkedin_logged_in

        if not _ensure_linkedin_logged_in(page):
            log.error("LinkedIn login failed")
            return 1
        page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

    # ── Step 2: Check if already applied ─────────────────────────────────
    page_text = page.inner_text("body").lower()
    if "applied" in page_text and (
        "you applied" in page_text or "application submitted" in page_text or "applied on" in page_text
    ):
        log.info("already applied to this job — skipping")
        _write_already_applied_result(out_dir, payload)
        return 0

    # ── Step 3: Click Easy Apply button ──────────────────────────────────
    # LinkedIn uses both <button> and <a> for Easy Apply (markup varies)
    easy_apply_btn = _easy_apply_button(page)
    if not _locator_is_visible(easy_apply_btn, timeout=8000):
        closed_reason = _linkedin_job_closed_reason(page)
        if closed_reason is not None:
            screenshot_path = _capture_not_easy_apply_screenshot(page, out_dir, reason="no_apply_button")
            _write_job_closed_result(
                out_dir,
                payload,
                reason=closed_reason,
                screenshot_path=screenshot_path,
            )
            return 0

        # Check if there's an external "Apply" button instead
        external_apply = _external_apply_button(page)
        if external_apply.is_visible(timeout=3000):
            log.info("external Apply button found — not Easy Apply")
            screenshot_path = _capture_not_easy_apply_screenshot(page, out_dir, reason="external_apply")
            _write_not_easy_apply_result(
                out_dir,
                payload,
                reason="external_apply",
                screenshot_path=screenshot_path,
            )
        else:
            screenshot_path = _capture_not_easy_apply_screenshot(page, out_dir, reason="no_apply_button")
            if screenshot_path is not None:
                log.info("debug screenshot saved to %s", screenshot_path)
            log.info("no Apply button found — job may be taken down")
            _write_not_easy_apply_result(
                out_dir,
                payload,
                reason="no_apply_button",
                screenshot_path=screenshot_path,
            )
        return 0

    easy_apply_btn.click()
    page.wait_for_timeout(2000)

    # ── Step 3b: Dismiss "discard previous application" dialog if present ─
    # Clicks "Keep"/"Continue" to resume previous partial application
    _dismiss_discard_dialog(page, 0)

    # ── Step 4: Loop through wizard steps ────────────────────────────────
    step_num = 0
    all_filled_steps = []
    unknown_questions = []
    validation_retry_counts: dict[int, int] = {}
    resume_runtime = {
        "status": "",
        "message": "",
        "visible_upload_path_seen": False,
        "observed_selection_labels": [],
    }

    while True:
        step_num += 1
        log.info("processing wizard step %d", step_num)

        # Dismiss discard dialog if it appeared during step transition
        _dismiss_discard_dialog(page, step_num)

        # Wait for modal content to stabilize
        modal = _easy_apply_modal(page)
        if not _locator_is_visible(modal, timeout=10000):
            if _attempt_reopen_easy_apply_modal(page, step_num):
                step_num -= 1
                continue
            # Capture debug screenshot before failing
            debug_path = pages_dir / f"debug_step_{step_num:02d}.png"
            try:
                _capture_linkedin_surface_screenshot(page, debug_path)
            except Exception:
                pass
            _capture_debug_screenshot(page, payload)
            _write_failed_result(
                out_dir,
                payload,
                failure_type="linkedin_modal_missing",
                message=f"LinkedIn Easy Apply modal not visible at step {step_num}.",
                retry_class="targeted_retry",
                step_num=step_num,
                step_screenshot=debug_path,
            )
            log.error("Easy Apply modal not visible at step %d — see %s", step_num, debug_path)
            return 1

        page.wait_for_timeout(1000)
        step_screenshot: Path | None = None

        # Check if this is the review/submit step
        modal_text = modal.inner_text().lower()
        is_review_step = "review your application" in modal_text or "review and submit" in modal_text

        if is_review_step:
            all_filled_steps.extend(_uncheck_follow_company(modal))
            page.wait_for_timeout(250)
            step_screenshot = pages_dir / f"page_{step_num:02d}.png"
            _capture_linkedin_surface_screenshot(page, step_screenshot, prefer_modal=True)
            log.info("reached review/submit step at step %d", step_num)
            break

        # ── Discover and fill fields on this step ────────────────────
        step_unknown_start = len(unknown_questions)
        try:
            filled_on_step = _fill_wizard_step(
                page,
                modal,
                payload,
                out_dir,
                all_filled_steps,
                unknown_questions,
                resume_runtime=resume_runtime,
            )
        except _ResumeUploadVerificationError as exc:
            all_filled_steps.extend(exc.filled_steps)
            payload["steps"] = all_filled_steps
            payload["unknown_questions"] = unknown_questions

            page.wait_for_timeout(250)
            step_screenshot = pages_dir / f"page_{step_num:02d}.png"
            _capture_linkedin_surface_screenshot(page, step_screenshot, prefer_modal=True)

            pre_submit_path = Path(artifacts["pre_submit_screenshot"])
            _capture_linkedin_surface_screenshot(page, pre_submit_path, prefer_modal=True)

            write_report(
                payload,
                board_name=_BOARD,
                runtime={"steps": all_filled_steps, "outcomes": _resume_outcomes(resume_runtime, payload)},
            )
            _capture_debug_screenshot(page, payload)
            _write_failed_result(
                out_dir,
                payload,
                failure_type="linkedin_resume_upload_verification_failed",
                message=resume_runtime.get("message") or str(exc),
                retry_class="none",
                step_num=step_num,
                step_screenshot=step_screenshot,
            )
            log.error("resume upload verification failed at step %d: %s", step_num, exc)
            return 1
        all_filled_steps.extend(filled_on_step)

        # ── Uncheck "Follow company" if present ──────────────────────
        all_filled_steps.extend(_uncheck_follow_company(modal))

        # Capture the actual post-fill state for this step. LinkedIn screenshots
        # are our source of truth, so they need to reflect the answers after
        # the controls have been filled, not the blank state we started from.
        page.wait_for_timeout(250)
        step_screenshot = pages_dir / f"page_{step_num:02d}.png"
        _capture_linkedin_surface_screenshot(page, step_screenshot, prefer_modal=True)

        step_unknowns = unknown_questions[step_unknown_start:]
        if step_unknowns:
            payload["steps"] = all_filled_steps
            payload["unknown_questions"] = unknown_questions

            pre_submit_path = Path(artifacts["pre_submit_screenshot"])
            _capture_linkedin_surface_screenshot(page, pre_submit_path, prefer_modal=True)

            write_report(
                payload,
                board_name=_BOARD,
                runtime={"steps": all_filled_steps, "outcomes": _resume_outcomes(resume_runtime, payload)},
            )
            _capture_debug_screenshot(page, payload)
            blocker_labels = ", ".join(question["label"] for question in step_unknowns[:3])
            _write_failed_result(
                out_dir,
                payload,
                failure_type="linkedin_unknown_questions",
                message=(
                    f"LinkedIn Easy Apply stopped at step {step_num} because one or more visible questions "
                    f"still require manual review: {blocker_labels}"
                ),
                retry_class="none",
                step_num=step_num,
                step_screenshot=step_screenshot,
            )
            return 1

        # ── Click Next/Continue ──────────────────────────────────────
        next_btn = modal.locator(
            'button[aria-label="Continue to next step"], '
            'button:has-text("Next"), '
            'button:has-text("Continue"), '
            'button:has-text("Review"), '
            "button[data-easy-apply-next-button]"
        ).first
        if not next_btn.is_visible(timeout=3000):
            # Maybe single-step form — check for submit button
            submit_btn = modal.locator('button:has-text("Submit application")').first
            if submit_btn.is_visible(timeout=2000):
                log.info("single-step form — submit button found")
                break
            _capture_debug_screenshot(page, payload)
            _write_failed_result(
                out_dir,
                payload,
                failure_type="linkedin_navigation_missing",
                message=f"LinkedIn Easy Apply did not show a Next or Submit button at step {step_num}.",
                retry_class="targeted_retry",
                step_num=step_num,
                step_screenshot=step_screenshot,
            )
            log.error("no Next or Submit button found at step %d", step_num)
            return 1

        # Click Next — try Playwright first (short timeout), then JS fallback
        _dismiss_discard_dialog(page, step_num)
        _click_next_button_with_fallback(next_btn, step_num=step_num)
        page.wait_for_timeout(2000)

        # Check for validation errors after clicking Next
        error_msgs = modal.locator(
            ".artdeco-inline-feedback--error, [data-test-form-element-error], .fb-dash-form-element__error-text"
        )
        if error_msgs.count() > 0:
            error_texts = [e.inner_text() for e in error_msgs.all()]
            retry_count = validation_retry_counts.get(step_num, 0) + 1
            validation_retry_counts[step_num] = retry_count
            log.warning("validation errors at step %d: %s", step_num, error_texts)
            if retry_count >= _LINKEDIN_VALIDATION_RETRY_LIMIT:
                _capture_debug_screenshot(page, payload)
                _write_failed_result(
                    out_dir,
                    payload,
                    failure_type="linkedin_validation_loop",
                    message=(
                        f"LinkedIn Easy Apply validation errors persisted at step {step_num} "
                        f"after {retry_count} retry attempts."
                    ),
                    retry_class="targeted_retry",
                    step_num=step_num,
                    step_screenshot=step_screenshot,
                    validation_errors=error_texts,
                )
                return 1
            # Stay on this step — retry fill
            step_num -= 1
            continue
        validation_retry_counts.pop(step_num, None)

        # Safety: max 20 steps
        if step_num >= 20:
            _capture_debug_screenshot(page, payload)
            _write_failed_result(
                out_dir,
                payload,
                failure_type="linkedin_validation_loop",
                message=f"LinkedIn Easy Apply exceeded {step_num} wizard steps without reaching review.",
                retry_class="targeted_retry",
                step_num=step_num,
                step_screenshot=step_screenshot,
            )
            log.error("exceeded 20 wizard steps — aborting")
            return 1

    # ── Step 5: At review/submit step ────────────────────────────────────
    # Re-query modal to avoid stale DOM reference after wizard navigation
    modal = _easy_apply_modal(page)

    payload["steps"] = all_filled_steps
    payload["unknown_questions"] = unknown_questions

    # Capture pre-submit screenshot
    pre_submit_path = Path(artifacts["pre_submit_screenshot"])
    _capture_linkedin_surface_screenshot(page, pre_submit_path, prefer_modal=True)
    log.info("pre-submit screenshot saved to %s", pre_submit_path)

    # Write autofill report
    write_report(
        payload,
        board_name=_BOARD,
        runtime={"steps": all_filled_steps, "outcomes": _resume_outcomes(resume_runtime, payload)},
    )

    if not submit:
        log.info("draft mode — stopping before submit")
        return 0

    # ── Step 6: Check for captcha before submit ──────────────────────────
    captcha_selectors = (
        'iframe[src*="recaptcha"]',
        'iframe[src*="hcaptcha"]',
        ".g-recaptcha",
    )
    for sel in captcha_selectors:
        if page.locator(sel).count():
            log.info("captcha detected before submit — waiting for manual resolution")
            captcha_result = wait_for_captcha_resolution(
                page,
                headless=False,
                payload=payload,
                board_title="LinkedIn Easy Apply",
            )
            if captcha_result.get("status") == "blocked":
                log.error("captcha not resolved")
                return 75  # CAPTCHA_SKIP_EXIT_CODE
            break

    # ── Step 7: Submit ───────────────────────────────────────────────────
    submit_btn = modal.locator('button:has-text("Submit application")').first
    if not submit_btn.is_visible(timeout=5000):
        _capture_debug_screenshot(page, payload)
        _write_failed_result(
            out_dir,
            payload,
            failure_type="linkedin_navigation_missing",
            message="LinkedIn Easy Apply reached review but did not show a Submit application button.",
            retry_class="targeted_retry",
            step_num=step_num,
            step_screenshot=pre_submit_path,
        )
        log.error("Submit application button not found at review step")
        return 1

    submit_btn.click()
    log.info("clicked Submit application")
    page.wait_for_timeout(3000)

    # Check for confirmation
    body_text = page.inner_text("body").lower()
    if (
        "application submitted" in body_text
        or "your application was sent" in body_text
        or "application sent" in body_text
    ):
        log.info("application confirmed submitted")
        # Capture post-submit screenshot
        post_submit_path = Path(artifacts["post_submit_screenshot"])
        _capture_linkedin_surface_screenshot(page, post_submit_path)
        try:
            reply_to_confirmation_email(payload, board_name=_BOARD)
        except Exception as exc:
            log.warning("Email reply failed for LinkedIn: %s", exc)
        return 0

    log.warning("submit clicked but confirmation not detected — check manually")
    post_submit_path = out_dir / artifacts["post_submit_screenshot"]
    _capture_linkedin_surface_screenshot(page, post_submit_path)
    try:
        reply_to_confirmation_email(payload, board_name=_BOARD)
    except Exception as exc:
        log.warning("Email reply failed for LinkedIn (ambiguous submit): %s", exc)
    return 0


# ── Field filling logic ─────────────────────────────────────────────────────


def _fill_wizard_step(
    page,
    modal,
    payload: dict,
    out_dir: Path,
    prior_steps: list[dict],
    unknown_questions: list[dict],
    *,
    resume_runtime: dict,
) -> list[dict]:
    """Discover fields on the current wizard step and fill them.

    Returns list of step dicts that were filled.
    """
    filled_steps = []
    _handle_linkedin_resume_step(page, modal, payload, filled_steps, resume_runtime)
    resume_state = _classify_linkedin_resume_markers(_collect_linkedin_resume_markers(page, modal), payload)

    # ── File inputs (resume/cover letter upload) ─────────────────────
    file_inputs = modal.locator('input[type="file"]')
    for i in range(file_inputs.count()):
        fi = file_inputs.nth(i)
        # Find associated label
        input_id = fi.get_attribute("id") or ""
        label_el = modal.locator(f'label[for="{input_id}"]') if input_id else None
        label_text = label_el.inner_text().strip().lower() if label_el and label_el.count() else "resume"

        if "cover" in label_text and payload.get("cover_letter_path"):
            fi.set_input_files(payload["cover_letter_path"])
            filled_steps.append(
                {
                    "field_name": "cover_letter",
                    "label": label_text,
                    "kind": "file",
                    "value": payload["cover_letter_path"],
                    "source": "generated_cover_letter",
                    "filled": True,
                    "required": False,
                }
            )
        elif payload.get("resume_path") and not resume_state["resume_step_visible"]:
            fi.set_input_files(payload["resume_path"])
            filled_steps.append(
                {
                    "field_name": "resume",
                    "label": label_text,
                    "kind": "file",
                    "value": payload["resume_path"],
                    "source": "generated_resume",
                    "filled": True,
                    "required": True,
                }
            )
        page.wait_for_timeout(1000)

    # ── Text inputs ──────────────────────────────────────────────────
    validation_errors = _visible_texts(
        modal.locator(
            ".artdeco-inline-feedback--error, [data-test-form-element-error], .fb-dash-form-element__error-text"
        )
    )
    text_inputs = modal.locator(
        'input[type="text"]:visible, '
        'input[type="tel"]:visible, '
        'input[type="email"]:visible, '
        'input[type="number"]:visible'
    )
    for i in range(text_inputs.count()):
        inp = text_inputs.nth(i)
        _fill_text_field(
            inp,
            modal,
            payload,
            out_dir,
            filled_steps,
            unknown_questions,
            validation_errors=validation_errors,
        )

    # ── Textareas ────────────────────────────────────────────────────
    textareas = modal.locator("textarea:visible")
    for i in range(textareas.count()):
        ta = textareas.nth(i)
        _fill_textarea_field(ta, modal, payload, out_dir, filled_steps, unknown_questions)

    # ── Select dropdowns ─────────────────────────────────────────────
    selects = modal.locator("select:visible")
    for i in range(selects.count()):
        sel = selects.nth(i)
        _fill_select_field(sel, modal, payload, out_dir, filled_steps, unknown_questions)

    # ── LinkedIn custom dropdowns (artdeco) ──────────────────────────
    # LinkedIn uses custom dropdown components, not native <select>
    custom_dropdowns = modal.locator("[data-test-text-selectable-option], .fb-dash-form-element--select")
    for i in range(custom_dropdowns.count()):
        dd = custom_dropdowns.nth(i)
        _fill_custom_dropdown(dd, modal, payload, out_dir, filled_steps, unknown_questions)

    # ── Fieldset choice groups ───────────────────────────────────────
    choice_groups = modal.locator("fieldset:visible")
    for i in range(choice_groups.count()):
        fieldset = choice_groups.nth(i)
        if fieldset.locator('input[type="radio"]').count():
            _fill_radio_group(fieldset, payload, out_dir, filled_steps, unknown_questions)
            continue
        if fieldset.locator('input[type="checkbox"]').count():
            _fill_checkbox_group(fieldset, payload, out_dir, filled_steps, unknown_questions)

    # ── Checkboxes (non-follow) ──────────────────────────────────────
    checkboxes = modal.locator('input[type="checkbox"]:visible')
    for i in range(checkboxes.count()):
        cb = checkboxes.nth(i)
        _fill_checkbox(cb, modal, payload, filled_steps)

    return filled_steps


def _get_field_label(element, modal) -> str:
    """Extract the label text for a form element."""
    # Try aria-label
    aria = element.get_attribute("aria-label")
    if aria:
        return aria.strip()
    # Try associated label element
    el_id = element.get_attribute("id")
    if el_id:
        label = modal.locator(f'label[for="{el_id}"]')
        if label.count():
            return label.first.inner_text().strip()
    # Try parent label
    parent_label = element.locator("xpath=ancestor::label")
    if parent_label.count():
        return parent_label.first.inner_text().strip()
    if str(element.get_attribute("type") or "").strip().lower() == "checkbox" and hasattr(element, "evaluate"):
        try:
            wrapper_text = str(
                element.evaluate(
                    """
                    (el) => (
                      el.closest('label')?.innerText ||
                      el.parentElement?.innerText ||
                      ''
                    )
                    """
                )
                or ""
            ).strip()
        except Exception:
            wrapper_text = ""
        if wrapper_text:
            return wrapper_text
    # Try preceding sibling label
    name = element.get_attribute("name") or element.get_attribute("placeholder") or ""
    return name.strip()


def _is_headline_label(label: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(label or "").casefold()).strip()
    return "headline" in normalized


def _truncate_single_line_answer(answer: str, *, max_len: int) -> str:
    normalized = re.sub(r"\s+", " ", str(answer or "").strip())
    if len(normalized) <= max_len:
        return normalized
    truncated = normalized[:max_len].rsplit(" ", 1)[0].strip()
    return truncated or normalized[:max_len].strip()


def _normalize_text_field_answer(label: str, answer: str | None, *, input_type: str) -> str | None:
    from application_submit_common import normalize_text

    raw = str(answer or "").strip()
    if not raw:
        return None

    if _is_headline_label(label):
        return _truncate_single_line_answer(raw, max_len=100)

    normalized_label = normalize_text(label)
    expects_numeric_years = input_type == "number" or (
        "years" in normalized_label and "experience" in normalized_label and "how many" in normalized_label
    )
    if not expects_numeric_years:
        return raw

    digit_match = re.search(r"\b(\d{1,2})\b", raw)
    if digit_match:
        return digit_match.group(1)

    label_match = re.search(r"\b(?:at least\s+)?(\d{1,2})\+?\s+years?\b", normalized_label)
    if label_match:
        return label_match.group(1)

    return None if input_type == "number" else raw


def _validation_errors_require_numeric_retry(validation_errors: list[str] | None) -> bool:
    combined = " ".join(str(error or "").casefold() for error in (validation_errors or []))
    if not combined:
        return False
    return any(
        fragment in combined
        for fragment in (
            "enter a decimal number",
            "enter a whole number",
            "number larger than 0.0",
            "number greater than 0",
            "enter a number",
        )
    )


def _looks_like_short_skill_label(label: str) -> bool:
    from application_submit_common import normalize_text

    normalized = normalize_text(label)
    if not normalized:
        return False
    if any(
        token in normalized
        for token in (
            "name",
            "email",
            "phone",
            "address",
            "city",
            "location",
            "linkedin",
            "website",
            "portfolio",
            "headline",
            "salary",
            "compensation",
        )
    ):
        return False
    return 1 <= len(normalized.split()) <= 4


def _numeric_experience_prompt_label(label: str) -> str:
    return f"How many years of work experience do you have with {str(label or '').strip()}?"


def _answer_for_text_field_details(
    label: str,
    payload: dict,
    out_dir: Path,
    *,
    input_type: str,
    force_numeric_experience: bool = False,
) -> tuple[str | None, str]:
    from application_submit_common import (
        APPLICATION_PROFILE_PATH,
        generate_application_answers,
        normalize_text,
        parse_application_profile,
        resolve_shared_question_policy,
    )
    from autofill_common import label_matches
    from question_classifier import classify_question as _classify_question

    try:
        application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        application_profile = None

    location_value = (
        str(payload.get("candidate_location") or "").strip()
        or str(getattr(application_profile, "location", "") or "").strip()
        or "San Francisco, CA"
    )
    normalized_label = normalize_text(label)

    value = None
    source = "unknown"

    if normalized_label in {"first name", "given name", "preferred first name"}:
        value = payload.get("candidate_name", "").split()[0] if payload.get("candidate_name") else None
        source = "master_resume.md"
    elif normalized_label in {"last name", "family name", "surname"}:
        parts = payload.get("candidate_name", "").split()
        value = parts[-1] if len(parts) > 1 else None
        source = "master_resume.md"
    elif normalized_label in {"name", "your name", "full name", "legal name", "display name"}:
        value = payload.get("candidate_name")
        source = "master_resume.md"
    elif label_matches(label, "email"):
        value = payload.get("candidate_email")
        source = "master_resume.md"
    elif label_matches(label, "phone", "mobile", "cell", "telephone"):
        value = payload.get("candidate_phone")
        source = "master_resume.md"
    elif label_matches(
        label, "mailing address", "primary mailing", "residence address", "home address", "address"
    ) or label_matches(label, "city", "location"):
        value = location_value
        source = "application_profile.md" if application_profile is not None else "master_resume.md"
    elif label_matches(label, "salary", "compensation", "pay", "desired salary"):
        value = "Open and flexible"
        source = "hardcoded"
    elif label_matches(label, "linkedin"):
        value = payload.get("candidate_linkedin", "") or getattr(application_profile, "linkedin", None)
        source = "application_profile.md" if getattr(application_profile, "linkedin", None) else "master_resume.md"
    elif label_matches(label, "github"):
        value = getattr(application_profile, "github", None)
        source = "application_profile.md"
    elif label_matches(label, "website", "portfolio", "url"):
        value = payload.get("candidate_website", "") or getattr(application_profile, "website", None)
        source = "application_profile.md" if getattr(application_profile, "website", None) else "master_resume.md"

    coerced_value = _normalize_text_field_answer(label, value, input_type=input_type)
    if coerced_value is not None:
        return coerced_value, source

    if application_profile is not None:
        category = _classify_question(label)
        if category is not None:
            policy = resolve_shared_question_policy(label, application_profile)
            deterministic_value = None
            deterministic_source = "application_profile.md"
            if policy is not None and policy.category != "work_authorization" and policy.text_value is not None:
                deterministic_value = policy.text_value
                deterministic_source = policy.source
            if deterministic_value is None:
                deterministic_value = _linkedin_answer_from_category(category, label, application_profile)
            coerced_deterministic = _normalize_text_field_answer(label, deterministic_value, input_type=input_type)
            if coerced_deterministic is not None:
                return coerced_deterministic, deterministic_source

    meta = (
        json.loads((out_dir / ".pipeline_meta.json").read_text()) if (out_dir / ".pipeline_meta.json").exists() else {}
    )
    generator_label = label
    generator_input_type = input_type
    normalized_label = normalize_text(label)
    if force_numeric_experience:
        generator_input_type = "number"
        if "years" not in normalized_label and "experience" not in normalized_label:
            generator_label = _numeric_experience_prompt_label(label)
    field_name = _linkedin_field_name(generator_label)
    answers = generate_application_answers(
        out_dir=out_dir,
        meta=meta,
        question_specs=[
            {"field_name": field_name, "label": generator_label, "kind": "text", "type": "input_text", "required": True}
        ],
    )
    generated_value = _normalize_text_field_answer(
        generator_label,
        answers.get(field_name, ""),
        input_type=generator_input_type,
    )
    if generated_value is not None:
        return generated_value, "generated_application_answer"

    return None, "unknown"


def _fill_text_field(
    inp,
    modal,
    payload: dict,
    out_dir: Path,
    filled_steps: list[dict],
    unknown_questions: list[dict],
    *,
    validation_errors: list[str] | None = None,
) -> None:
    """Fill a text/tel/email/number input."""
    from application_submit_common import normalize_text

    label = _get_field_label(inp, modal)
    field_name = _linkedin_field_name(label)
    current_value = inp.input_value().strip()
    normalized_label = normalize_text(label)
    force_numeric_experience = _validation_errors_require_numeric_retry(validation_errors) and (
        _looks_like_short_skill_label(label) or "years" in normalized_label or "experience" in normalized_label
    )

    from generated_answer_validation import _is_conditional_followup

    if _is_conditional_followup({"label": label}):
        if current_value:
            _replace_field_value(inp, "")
        return

    # Skip if already filled with a reasonable value
    if current_value and len(current_value) > 1 and not _is_headline_label(label):
        if not (force_numeric_experience and re.search(r"[A-Za-z]", current_value)):
            filled_steps.append(
                {
                    "field_name": field_name,
                    "label": label,
                    "kind": "text",
                    "value": current_value,
                    "source": "pre-filled",
                    "filled": True,
                    "required": True,
                }
            )
            return

    input_type = (inp.get_attribute("type") or "text").strip().casefold()
    effective_input_type = "number" if force_numeric_experience else input_type
    value, source = _answer_for_text_field_details(
        label,
        payload,
        out_dir,
        input_type=effective_input_type,
        force_numeric_experience=force_numeric_experience,
    )

    if value:
        _replace_field_value(inp, value)
        filled_steps.append(
            {
                "field_name": field_name,
                "label": label,
                "kind": "text",
                "value": value,
                "source": source,
                "filled": True,
                "required": True,
            }
        )
        return

    unknown_questions.append({"field_name": field_name, "label": label, "kind": "text"})


_COMPENSATION_DEFLECT = (
    "I'm open and flexible on compensation. I'd prefer to learn more about "
    "the role's scope and total rewards package before discussing specific numbers. "
    "I'm confident we can find a mutually agreeable arrangement."
)


def _linkedin_answer_from_category(category: str, label: str, app_profile) -> str | None:
    """Map a classifier category to a deterministic text answer for LinkedIn textareas."""
    from application_submit_common import format_education_from_profile, shared_text_answer_for_question

    shared_answer = shared_text_answer_for_question(label, app_profile)
    if shared_answer is not None:
        return shared_answer

    if category == "education":
        return format_education_from_profile(app_profile) or None
    if category == "compensation":
        return _COMPENSATION_DEFLECT
    if category == "nda_noncompete":
        return "No"
    if category == "city_location":
        return getattr(app_profile, "location", None) or None
    if category == "culture_careers_optin":
        return "No"
    if category == "product_usage":
        return "Yes"
    if category == "interview_accommodation":
        return "No"
    if category == "reasonable_accommodation":
        return "Yes"
    if category == "experience_confirmation":
        return "Yes"
    if category == "minimum_experience":
        return "Yes"
    return None


def _is_cover_letter_label(label: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(label or "").casefold()).strip()
    return "cover letter" in normalized


def _linkedin_field_name(label: str) -> str:
    from text_normalization_helpers import slugify_label

    return slugify_label(str(label or "").strip())


def _normalize_textarea_value(value: str) -> str:
    return str(value or "").replace("\r\n", "\n").strip()


def _replace_field_value(locator, value: str) -> None:
    from browser_runtime import human_fill

    try:
        locator.clear()
    except Exception:
        pass
    human_fill(locator, value)


def _fill_textarea_field(
    ta,
    modal,
    payload: dict,
    out_dir: Path,
    filled_steps: list[dict],
    unknown_questions: list[dict],
) -> None:
    """Fill a textarea — may require LLM-generated answer."""

    label = _get_field_label(ta, modal)
    field_name = _linkedin_field_name(label)
    current_value = ta.input_value().strip()

    if _is_cover_letter_label(label):
        from application_submit_common import find_cover_letter_text

        cover_letter_text = find_cover_letter_text(out_dir)
        if _normalize_textarea_value(current_value) != _normalize_textarea_value(cover_letter_text):
            _replace_field_value(ta, cover_letter_text)
        filled_steps.append(
            {
                "field_name": field_name,
                "label": label,
                "kind": "textarea",
                "value": cover_letter_text,
                "source": "cover_letter_text.txt",
                "filled": True,
                "required": False,
            }
        )
        return

    if current_value and len(current_value) > 5:
        filled_steps.append(
            {
                "field_name": field_name,
                "label": label,
                "kind": "textarea",
                "value": current_value,
                "source": "pre-filled",
                "filled": True,
                "required": False,
            }
        )
        return

    # Deterministic answers via unified classifier
    from application_submit_common import (
        APPLICATION_PROFILE_PATH,
        parse_application_profile,
        resolve_shared_question_policy,
    )
    from question_classifier import classify_question as _classify_question

    category = _classify_question(label)
    if category is not None:
        app_profile_text = APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        app_profile = parse_application_profile(app_profile_text)
        policy = resolve_shared_question_policy(label, app_profile)
        deterministic_value = None
        source = "application_profile.md"
        if policy is not None and policy.category != "work_authorization" and policy.text_value is not None:
            deterministic_value = policy.text_value
            source = policy.source
        if deterministic_value is None:
            deterministic_value = _linkedin_answer_from_category(category, label, app_profile)
        if deterministic_value is not None:
            _replace_field_value(ta, deterministic_value)
            filled_steps.append(
                {
                    "field_name": field_name,
                    "label": label,
                    "kind": "textarea",
                    "value": deterministic_value,
                    "source": source,
                    "filled": True,
                    "required": True,
                }
            )
            return

    # Try LLM-generated answer
    from application_submit_common import generate_application_answers

    meta = (
        json.loads((out_dir / ".pipeline_meta.json").read_text()) if (out_dir / ".pipeline_meta.json").exists() else {}
    )
    answers = generate_application_answers(
        out_dir=out_dir,
        meta=meta,
        question_specs=[{"field_name": field_name, "label": label, "kind": "textarea", "required": True}],
    )
    answer = answers.get(field_name, "")
    if answer:
        _replace_field_value(ta, answer)
        filled_steps.append(
            {
                "field_name": field_name,
                "label": label,
                "kind": "textarea",
                "value": answer,
                "source": "generated_application_answer",
                "filled": True,
                "required": True,
            }
        )
    else:
        unknown_questions.append({"field_name": field_name, "label": label, "kind": "textarea"})


def _fill_select_field(
    sel,
    modal,
    payload: dict,
    out_dir: Path,
    filled_steps: list[dict],
    unknown_questions: list[dict],
) -> None:
    """Fill a native <select> dropdown."""
    from application_submit_common import normalize_text
    from autofill_common import select_option

    label = _get_field_label(sel, modal)

    # Get all options
    options = sel.locator("option")
    option_texts = [o.inner_text().strip() for o in options.all() if o.inner_text().strip()]

    answer, source = _answer_for_select_details(label, option_texts, payload, out_dir)
    matched = select_option(option_texts, answer) if answer else None
    selected_option = _selected_non_placeholder_option(sel)
    if selected_option is not None:
        if matched and normalize_text(selected_option) != normalize_text(matched):
            sel.select_option(label=matched)
            filled_steps.append(
                {
                    "field_name": _linkedin_field_name(label),
                    "label": label,
                    "kind": "select",
                    "value": matched,
                    "option": matched,
                    "source": source,
                    "filled": True,
                    "required": True,
                }
            )
            return
        filled_steps.append(
            {
                "field_name": _linkedin_field_name(label),
                "label": label,
                "kind": "select",
                "value": selected_option,
                "option": selected_option,
                "source": "pre-filled",
                "filled": True,
                "required": True,
            }
        )
        return

    if matched:
        sel.select_option(label=matched)
        filled_steps.append(
            {
                "field_name": _linkedin_field_name(label),
                "label": label,
                "kind": "select",
                "value": matched,
                "option": matched,
                "source": source,
                "filled": True,
                "required": True,
            }
        )
        return

    unknown_questions.append({"field_name": _linkedin_field_name(label), "label": label, "kind": "select"})


def _fill_custom_dropdown(
    dd,
    modal,
    payload: dict,
    out_dir: Path,
    filled_steps: list[dict],
    unknown_questions: list[dict],
) -> None:
    """Fill a LinkedIn artdeco custom dropdown (click to expand, pick option)."""
    from autofill_common import select_option

    if dd.locator("select").count():
        return

    label = _get_field_label(dd, modal)
    if not label:
        return

    # Click to expand
    trigger = dd.locator('button, [role="combobox"], [data-test-text-selectable-option__trigger]').first
    if trigger.count():
        trigger.click()
        dd.page.wait_for_timeout(500)

    # Read options from the dropdown list
    options = dd.locator('[role="option"], li[data-test-text-selectable-option__option]')
    option_texts = [o.inner_text().strip() for o in options.all() if o.inner_text().strip()]

    answer, source = _answer_for_select_details(label, option_texts, payload, out_dir)
    if answer:
        matched = select_option(option_texts, answer)
        if matched:
            for o in options.all():
                if o.inner_text().strip() == matched:
                    o.click()
                    filled_steps.append(
                        {
                            "field_name": _linkedin_field_name(label),
                            "label": label,
                            "kind": "select",
                            "value": matched,
                            "option": matched,
                            "source": source,
                            "filled": True,
                            "required": True,
                        }
                    )
                    dd.page.wait_for_timeout(500)
                    return

    # Close dropdown if no match
    dd.page.keyboard.press("Escape")
    unknown_questions.append({"field_name": _linkedin_field_name(label), "label": label, "kind": "select"})


def _answer_for_select(label: str, options: list[str], payload: dict, out_dir: Path) -> str | None:
    """Determine the best answer for a select/dropdown based on label."""
    answer, _ = _answer_for_select_details(label, options, payload, out_dir)
    return answer


def _linkedin_choice_requires_manual_review(label: str) -> bool:
    from application_submit_common import normalize_text

    normalized = normalize_text(label)
    if "citizen" in normalized:
        return True
    return "permanent resident" in normalized


def _looks_like_source_question(label: str) -> bool:
    from application_submit_common import normalize_text

    normalized = normalize_text(label)
    if not normalized:
        return False
    if normalized == "source":
        return True
    return any(
        fragment in normalized
        for fragment in (
            "how did you hear",
            "where did you hear",
            "heard about us",
            "hear about us",
            "how did you learn about",
            "how did you first learn",
            "how did you first hear",
            "learn about us",
            "where did you find",
            "how did you find out about",
        )
    )


def _answer_for_select_details(
    label: str,
    options: list[str],
    payload: dict,
    out_dir: Path,
) -> tuple[str | None, str]:
    """Determine the best answer and attribution for a select/dropdown."""
    from application_submit_common import (
        APPLICATION_PROFILE_PATH,
        _best_city_option,
        generate_application_answers,
        parse_application_profile,
        question_is_culture_careers_optin,
        question_is_salary_comfort_check,
        resolve_shared_question_policy,
    )
    from autofill_common import label_matches, select_option

    def _demographic_option_from_profile() -> str | None:
        if application_profile is None:
            return None

        candidates: list[str] = []

        def _extend(*values: str | None) -> None:
            for value in values:
                text = str(value or "").strip()
                if text and text not in candidates:
                    candidates.append(text)

        def _bool_candidate(value: str | None) -> str | None:
            low = str(value or "").strip().casefold()
            if not low:
                return None
            if any(token in low for token in ("prefer not", "do not wish", "don't wish", "decline", "choose not")):
                return "Prefer not to answer"
            if any(token in low for token in ("not a protected veteran", "do not have a disability", "not had one")):
                return "No"
            if low.startswith("no"):
                return "No"
            if low.startswith("yes"):
                return "Yes"
            return None

        if label_matches(label, "gender identity"):
            _extend(getattr(application_profile, "gender_identity", None), getattr(application_profile, "gender", None))
        elif label_matches(label, "gender"):
            _extend(getattr(application_profile, "gender", None), getattr(application_profile, "gender_identity", None))

        if label_matches(label, "hispanic", "latino"):
            race = getattr(application_profile, "race_or_ethnicity", "")
            if race:
                _extend("Yes" if any(token in race.casefold() for token in ("hispanic", "latino")) else "No", race)
        elif label_matches(label, "race", "ethnicity"):
            _extend(getattr(application_profile, "race_or_ethnicity", None))

        if label_matches(label, "veteran"):
            veteran = getattr(application_profile, "veteran_status", None)
            _extend(veteran, _bool_candidate(veteran))

        if label_matches(label, "disability"):
            disability = getattr(application_profile, "disability_status", None)
            _extend(disability, _bool_candidate(disability))

        if label_matches(label, "transgender"):
            transgender = getattr(application_profile, "transgender_status", None)
            _extend(transgender, _bool_candidate(transgender))

        if label_matches(label, "sexual orientation"):
            _extend(getattr(application_profile, "sexual_orientation", None))

        if label_matches(label, "pronoun", "pronouns"):
            _extend(getattr(application_profile, "pronouns", None))

        for candidate in candidates:
            matched = select_option(options, candidate)
            if matched:
                return matched
        return None

    if _looks_like_source_question(label):
        return "LinkedIn", "hardcoded"
    try:
        application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        application_profile = None
    if _linkedin_choice_requires_manual_review(label):
        return None, "manual_review_required"
    if application_profile is not None:
        policy = resolve_shared_question_policy(label, application_profile)
        if policy is not None and policy.text_value is not None:
            matched = select_shared_policy_option(options, policy, application_profile=application_profile)
            return matched or policy.text_value, policy.source
    if label_matches(label, "phone country code", "phone country"):
        country = str(getattr(application_profile, "country", "") or "").strip() if application_profile is not None else ""
        if country:
            matched = select_option(options, country)
            if matched:
                return matched, "application_profile.md"
            if country.casefold() in {"united states", "united states of america", "usa", "us"}:
                for candidate in ("United States (+1)", "United States", "+1"):
                    matched = select_option(options, candidate)
                    if matched:
                        return matched, "application_profile.md"
    if label_matches(label, "sponsor"):
        return "No", "application_profile.md"
    if label_matches(label, "authorization", "authorized", "legally", "work rights", "visa"):
        return "Yes", "application_profile.md"
    # Classifier-based routing for experience/qualification dropdowns
    from question_classifier import classify_question as _classify_question

    _category = _classify_question(label)
    if _category == "experience_confirmation":
        return "Yes", "shared_positive_fit_policy"
    if _category == "minimum_experience":
        return "Yes", "shared_positive_fit_policy"
    # Free-form "How many years" prompts need text/number handling rather than
    # choice generation, but generic yes/no experience questions should still
    # get the generated-choice fallback below.
    if "experience" in label.casefold() and "years" in label.casefold():
        return None, "unresolved"
    if question_is_salary_comfort_check(label):
        return "Yes", "application_profile.md"
    if question_is_culture_careers_optin(label):
        return "Yes", "deterministic"
    # EEO / voluntary self-identification
    if label_matches(
        label,
        "gender",
        "hispanic",
        "latino",
        "race",
        "ethnicity",
        "veteran",
        "disability",
        "self identification",
        "self-identification",
        "demographic",
        "eeo",
        "pronoun",
        "pronouns",
    ):
        matched = _demographic_option_from_profile()
        if matched:
            return matched, "application_profile.md"
        # Try matching common decline options
        for opt in options:
            low = opt.lower()
            if "decline" in low or "don't wish" in low or "prefer not" in low or "choose not" in low:
                return opt, "application_profile.md"
        return "Decline to self-identify", "application_profile.md"
    # City / state / location fields
    if label_matches(label, "city of residence", "current city", "city you live", "what city", "where do you live"):
        return "San Francisco", "application_profile.md"
    if label_matches(label, "state of residence", "state you live", "current state", "which state"):
        return "CA", "application_profile.md"
    candidate_location = (
        str(payload.get("candidate_location") or "").strip()
        or str(getattr(application_profile, "location", "")).strip()
    )
    if label_matches(
        label,
        "which location",
        "location are you applying",
        "preferred location",
        "preferred office",
        "which office",
        "available to work",
    ):
        best_city_option = _best_city_option(options, None, candidate_location)
        if best_city_option:
            return best_city_option, "application_profile.md" if application_profile is not None else "master_resume.md"
    # Location / commute / travel screening questions
    if label_matches(
        label, "located in the united states", "currently located", "reside in", "based in the u", "live in the u"
    ):
        return "Yes", "shared_positive_fit_policy"
    if label_matches(
        label,
        "commute",
        "commuting",
        "able to work from",
        "work on-site",
        "work onsite",
        "work in-person",
        "come to the office",
        "work hybrid",
    ):
        return "Yes", "shared_positive_fit_policy"
    if label_matches(label, "open to travel", "willing to travel", "travel to", "able to travel"):
        return "Yes", "shared_positive_fit_policy"
    if label_matches(label, "relocat", "open to moving", "willing to move"):
        return "Yes", "shared_positive_fit_policy"

    meta = (
        json.loads((out_dir / ".pipeline_meta.json").read_text()) if (out_dir / ".pipeline_meta.json").exists() else {}
    )
    meta.setdefault("company", "")
    field_name = _linkedin_field_name(label)
    answers = generate_application_answers(
        out_dir=out_dir,
        meta=meta,
        question_specs=[
            {
                "field_name": field_name,
                "label": label,
                "kind": "select",
                "type": "multi_value_single_select",
                "required": True,
                "options": options,
            }
        ],
    )
    raw_answer = answers.get(field_name)
    candidates = raw_answer if isinstance(raw_answer, list) else [raw_answer]
    for candidate in candidates:
        candidate_text = str(candidate or "").strip()
        if not candidate_text:
            continue
        matched = select_option(options, candidate_text)
        if matched:
            return matched, "generated_application_answer"
    return None, "unresolved"


def _answer_for_checkbox_group_details(
    label: str,
    options: list[str],
    payload: dict,
    out_dir: Path,
) -> tuple[list[str] | None, str]:
    from application_submit_common import (
        APPLICATION_PROFILE_PATH,
        education_discipline_option_matches,
        education_level_option_matches,
        generate_application_answers,
        parse_application_profile,
        question_is_culture_careers_optin,
    )
    from autofill_common import label_matches, select_option

    resolved_label = str(label or "").strip()
    if not resolved_label and len(options) == 1:
        resolved_label = str(options[0] or "").strip()

    if _looks_like_source_question(resolved_label):
        matched = select_option(options, "LinkedIn")
        if matched:
            return [matched], "hardcoded"

    if question_is_culture_careers_optin(resolved_label) or label_matches(
        resolved_label,
        "terms",
        "conditions",
        "privacy policy",
    ):
        matched = select_option(options, resolved_label) or (options[0] if len(options) == 1 else None)
        if matched:
            return [matched], "deterministic"

    try:
        application_profile = parse_application_profile(APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        application_profile = None

    education_level_matches = education_level_option_matches(resolved_label, options, application_profile)
    if education_level_matches:
        return education_level_matches, "application_profile.md"

    deterministic_matches = education_discipline_option_matches(resolved_label, options, application_profile)
    if deterministic_matches:
        return deterministic_matches, "application_profile.md"

    meta = (
        json.loads((out_dir / ".pipeline_meta.json").read_text()) if (out_dir / ".pipeline_meta.json").exists() else {}
    )
    field_name = _linkedin_field_name(resolved_label)
    answers = generate_application_answers(
        out_dir=out_dir,
        meta=meta,
        question_specs=[
            {
                "field_name": field_name,
                "label": resolved_label,
                "kind": "checkbox_group",
                "type": "multi_value_multi_select",
                "required": True,
                "options": options,
                "values": [{"label": option} for option in options],
            }
        ],
    )
    raw_answer = answers.get(field_name)
    if isinstance(raw_answer, list):
        candidates = [str(candidate).strip() for candidate in raw_answer if str(candidate).strip()]
    elif isinstance(raw_answer, str):
        candidates = [candidate.strip() for candidate in raw_answer.split(",") if candidate.strip()]
    else:
        candidates = []

    matched_options: list[str] = []
    for candidate in candidates:
        matched = select_option(options, candidate)
        if matched and matched not in matched_options:
            matched_options.append(matched)
    if matched_options:
        return matched_options, "generated_application_answer"
    return None, "unresolved"


def _selected_non_placeholder_option(sel) -> str | None:
    """Return the selected option text when it represents a real choice."""
    from application_submit_common import normalize_text

    selected = sel.locator("option:checked").first
    if not selected.count():
        return None
    selected_text = selected.inner_text().strip()
    if not selected_text or normalize_text(selected_text).startswith("select"):
        return None
    return selected_text


def _group_choice_label_text(scope, control) -> str:
    control_id = str(control.get_attribute("id") or "").strip()
    if control_id:
        label = scope.locator(f'label[for="{control_id}"]').first
        if label.count():
            text = str(label.inner_text() or "").strip()
            if text:
                return text

    aria_label = str(control.get_attribute("aria-label") or "").strip()
    if aria_label:
        return aria_label

    if hasattr(control, "evaluate"):
        try:
            text = str(
                control.evaluate(
                    """
                    (el) => (
                      el.closest('label')?.innerText ||
                      el.parentElement?.innerText ||
                      ''
                    )
                    """
                )
                or ""
            ).strip()
        except Exception:
            text = ""
        if text:
            return text

    return str(control.get_attribute("value") or "").strip()


def _fill_radio_group(
    fieldset,
    payload: dict,
    out_dir: Path,
    filled_steps: list[dict],
    unknown_questions: list[dict],
) -> None:
    """Fill a radio button group."""
    from application_submit_common import normalize_text
    from autofill_common import select_option

    legend = fieldset.locator("legend, span.fb-dash-form-element__label")
    label = legend.first.inner_text().strip() if legend.count() else ""

    radios = fieldset.locator('input[type="radio"]')
    if not radios.count():
        return

    # Get radio labels
    radio_options = []
    for r in radios.all():
        r_text = _group_choice_label_text(fieldset, r)
        radio_options.append((r, r_text))

    selected_option = next((text for radio, text in radio_options if radio.is_checked() and text.strip()), None)
    answer, source = _answer_for_select_details(label, [t for _, t in radio_options], payload, out_dir)
    if answer:
        if selected_option and select_option([selected_option], answer):
            filled_steps.append(
                {
                    "field_name": _linkedin_field_name(label),
                    "label": label,
                    "kind": "radio",
                    "value": selected_option,
                    "option": selected_option,
                    "source": source,
                    "filled": True,
                    "required": True,
                }
            )
            return
        matched = select_option([t for _, t in radio_options], answer)
        if matched:
            for r, t in radio_options:
                if t == matched:
                    if r.is_checked():
                        filled_steps.append(
                            {
                                "field_name": _linkedin_field_name(label),
                                "label": label,
                                "kind": "radio",
                                "value": matched,
                                "option": matched,
                                "source": source,
                                "filled": True,
                                "required": True,
                            }
                        )
                        return
                    # Use force=True to bypass overlay interception
                    r.click(force=True)
                    filled_steps.append(
                        {
                            "field_name": _linkedin_field_name(label),
                            "label": label,
                            "kind": "radio",
                            "value": matched,
                            "option": matched,
                            "source": source,
                            "filled": True,
                            "required": True,
                        }
                    )
                    return

    if selected_option and normalize_text(selected_option):
        filled_steps.append(
            {
                "field_name": _linkedin_field_name(label),
                "label": label,
                "kind": "radio",
                "value": selected_option,
                "option": selected_option,
                "source": "pre-filled",
                "filled": True,
                "required": True,
            }
        )
        return

    unknown_questions.append({"field_name": _linkedin_field_name(label), "label": label, "kind": "radio"})


def _fill_checkbox(cb, modal, payload: dict, filled_steps: list[dict]) -> None:
    """Fill a checkbox."""
    from autofill_common import label_matches

    label = _get_field_label(cb, modal)
    low = label.lower()

    # Skip follow company (handled separately) and priority
    if "follow" in low or "priority" in low:
        return

    from application_submit_common import question_is_culture_careers_optin

    if question_is_culture_careers_optin(label) or label_matches(label, "terms", "conditions", "privacy policy"):
        if not cb.is_checked():
            cb.click(force=True)
        filled_steps.append(
            {
                "field_name": _linkedin_field_name(label),
                "label": label,
                "kind": "checkbox",
                "checked": True,
                "source": "deterministic",
                "filled": True,
                "required": False,
            }
        )


def _fill_checkbox_group(
    fieldset,
    payload: dict,
    out_dir: Path,
    filled_steps: list[dict],
    unknown_questions: list[dict],
) -> None:
    """Fill a LinkedIn checkbox group, supporting both single- and multi-select groups."""
    from application_submit_common import normalize_text
    from autofill_common import select_option

    legend = fieldset.locator("legend, span.fb-dash-form-element__label")
    label = legend.first.inner_text().strip() if legend.count() else ""

    checkboxes = fieldset.locator('input[type="checkbox"]')
    if not checkboxes.count():
        return

    checkbox_options = []
    for checkbox in checkboxes.all():
        checkbox_text = _group_choice_label_text(fieldset, checkbox)
        checkbox_options.append((checkbox, checkbox_text))

    option_labels = [text for _, text in checkbox_options]
    if not label and len(option_labels) == 1:
        label = option_labels[0]

    def _is_binary_choice(options: list[str]) -> bool:
        normalized_options = [normalize_text(option) for option in options if normalize_text(option)]
        if not normalized_options or len(normalized_options) > 3:
            return False
        return all(
            normalized == "yes"
            or normalized.startswith(("yes ", "no "))
            or normalized == "no"
            or "prefer not" in normalized
            or "decline" in normalized
            for normalized in normalized_options
        )

    desired_options: list[str] | None = None
    source = "unresolved"

    if _is_binary_choice(option_labels):
        answer, source = _answer_for_select_details(label, option_labels, payload, out_dir)
        if answer:
            matched = select_option(option_labels, answer)
            if matched:
                desired_options = [matched]
    else:
        desired_options, source = _answer_for_checkbox_group_details(label, option_labels, payload, out_dir)

    if desired_options:
        desired_set = set(desired_options)
        recorded = False
        for checkbox, text in checkbox_options:
            should_be_checked = text in desired_set
            if checkbox.is_checked() != should_be_checked:
                checkbox.click(force=True)
            if not should_be_checked:
                continue
            filled_steps.append(
                {
                    "field_name": _linkedin_field_name(label),
                    "label": label,
                    "kind": "checkbox_group",
                    "value": text,
                    "option": text,
                    "source": source,
                    "filled": True,
                    "required": True,
                }
            )
            recorded = True
        if recorded:
            return

    unknown_questions.append({"field_name": _linkedin_field_name(label), "label": label, "kind": "checkbox_group"})


def _follow_checkbox_label_text(modal, checkbox) -> str:
    checkbox_id = str(checkbox.get_attribute("id") or "").strip()
    if checkbox_id:
        label = modal.locator(f'label[for="{checkbox_id}"]').first
        if label.count():
            text = str(label.inner_text() or "").strip()
            if text:
                return text
    aria_label = str(checkbox.get_attribute("aria-label") or "").strip()
    if aria_label:
        return aria_label
    return "Follow company"


def _uncheck_follow_company(modal) -> list[dict]:
    """Uncheck LinkedIn follow/update opt-ins when they appear."""
    selectors = (
        'input[type="checkbox"][id*="follow"]',
        'input[type="checkbox"][name*="follow"]',
        'label:has-text("Follow") input[type="checkbox"]',
        'label:has-text("Follow company") input[type="checkbox"]',
        'label:has-text("Get job updates") input[type="checkbox"]',
        'label:has-text("Keep me updated") input[type="checkbox"]',
    )
    unchecked_steps: list[dict] = []
    seen_keys: set[str] = set()
    for selector in selectors:
        follow_cb = modal.locator(selector)
        for i in range(follow_cb.count()):
            cb = follow_cb.nth(i)
            checkbox_id = str(cb.get_attribute("id") or "").strip()
            checkbox_name = str(cb.get_attribute("name") or "").strip()
            dedupe_key = checkbox_id or checkbox_name or f"{selector}:{i}"
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            try:
                checked = cb.is_checked()
            except Exception:
                checked = str(cb.get_attribute("aria-checked") or "").strip().lower() == "true"
            if not checked:
                continue
            label_text = _follow_checkbox_label_text(modal, cb)
            label = None
            if checkbox_id:
                label = modal.locator(f'label[for="{checkbox_id}"]').first
            unchecked = False
            if label is not None and label.count():
                try:
                    label.click(force=True)
                    unchecked = True
                except Exception:
                    pass
            try:
                if not unchecked:
                    cb.uncheck(force=True, timeout=1000)
                    unchecked = True
            except Exception:
                pass
            if not unchecked:
                try:
                    cb.click(force=True)
                    unchecked = True
                except Exception:
                    pass
            try:
                still_checked = cb.is_checked()
            except Exception:
                still_checked = str(cb.get_attribute("aria-checked") or "").strip().lower() == "true"
            if unchecked and not still_checked:
                unchecked_steps.append(
                    {
                        "field_name": "follow_company_opt_in",
                        "label": label_text,
                        "kind": "checkbox",
                        "value": False,
                        "source": "linkedin_opt_out_policy",
                        "filled": True,
                        "required": False,
                    }
                )
                log.info("unchecked Follow company checkbox: %s", label_text)
    return unchecked_steps


def main() -> int:
    from autofill_pipeline import autofill_main

    return autofill_main(
        board_name=_BOARD,
        build_payload_fn=_build_payload,
        run_browser_fn=_run_browser,
    )


if __name__ == "__main__":
    raise SystemExit(main())
