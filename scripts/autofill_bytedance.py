#!/usr/bin/env python3
"""ByteDance application preflight.

ByteDance's careers surface currently redirects unauthenticated users to a
rendered sign-in wall. This board script classifies that rendered surface
truthfully so the shared draft pipeline records the real blocker.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_submit_common import load_meta, write_submission_result
from autofill_common import board_file_constants
from autofill_pipeline import autofill_main, run_simple_board_pipeline
from output_layout import migrate_role_output_layout, role_submit_path
from project_env import load_project_env

_BOARD = "bytedance"
_BOARD_CONSTANTS = board_file_constants(_BOARD)
APPLICATION_PAGE_HTML = _BOARD_CONSTANTS["application_page_html"]

load_project_env()


def _canonical_url_without_query(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query="", fragment=""))


def _bytedance_application_url(job_url: str) -> str:
    parsed = urlparse(job_url)
    scheme = parsed.scheme or "https"
    parts = [part for part in parsed.path.split("/") if part]
    locale = "en"
    offset = 0
    if parts and re.fullmatch(r"[a-z]{2}(?:_[a-z]{2})?", parts[0], flags=re.I):
        locale = parts[0]
        offset = 1

    if len(parts) >= offset + 3 and parts[offset] == "resume" and parts[offset + 2] == "apply":
        return _canonical_url_without_query(job_url)
    if len(parts) >= offset + 3 and parts[offset] == "position" and parts[offset + 2] == "detail":
        return f"{scheme}://jobs.bytedance.com/{locale}/resume/{parts[offset + 1]}/apply"
    if len(parts) >= offset + 2 and parts[offset] == "search":
        return f"{scheme}://jobs.bytedance.com/{locale}/resume/{parts[offset + 1]}/apply"
    return _canonical_url_without_query(job_url)


def _build_payload(out_dir: Path, provider: str) -> dict:
    migrate_role_output_layout(out_dir)
    meta = load_meta(out_dir)
    source_job_url = str(meta.get("board_url") or meta.get("jd_source_resolved") or meta.get("jd_source") or "")
    application_url = _bytedance_application_url(source_job_url)
    return {
        "job_url": application_url,
        "application_url": application_url,
        "source_job_url": source_job_url,
        "out_dir": str(out_dir),
        "job_title": str(meta.get("jd_title") or ""),
        "company": str(meta.get("company_proper") or meta.get("company") or ""),
        "answer_provider": provider,
        "mode": "review-before-submit",
        "notes": [
            "ByteDance preflight stops truthfully at the rendered login wall or unsupported live surface.",
            "This script does not attempt live ByteDance autofill yet.",
        ],
        "artifacts": {
            "payload_path": str(role_submit_path(out_dir, _BOARD_CONSTANTS["payload_json"])),
            "report_markdown": str(role_submit_path(out_dir, _BOARD_CONSTANTS["report_md"])),
            "report_json": str(role_submit_path(out_dir, _BOARD_CONSTANTS["report_json"])),
            "pre_submit_screenshot": str(role_submit_path(out_dir, _BOARD_CONSTANTS["pre_submit_screenshot"])),
            "page_screenshots_dir": str(role_submit_path(out_dir, _BOARD_CONSTANTS["page_screenshots_dir"])),
            "unknown_questions_json": str(role_submit_path(out_dir, _BOARD_CONSTANTS["unknown_questions_json"])),
            "submit_debug_html": str(role_submit_path(out_dir, _BOARD_CONSTANTS["submit_debug_html"])),
            "submit_debug_screenshot": str(role_submit_path(out_dir, _BOARD_CONSTANTS["submit_debug_screenshot"])),
            "application_page_html": str(role_submit_path(out_dir, APPLICATION_PAGE_HTML)),
        },
        "steps": [],
        "unknown_questions": [],
    }


def _body_text(page) -> str:
    try:
        return re.sub(r"\s+", " ", page.locator("body").inner_text(timeout=5000)).strip()
    except Exception:
        return ""


def _base_result(page, payload: dict[str, object]) -> dict[str, object]:
    return {
        "status": "unknown",
        "board": _BOARD,
        "provider": _BOARD,
        "website_confirmed": False,
        "updated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "job_url": str(payload.get("application_url") or payload.get("job_url") or page.url or "").strip(),
        "company": str(payload.get("company") or "").strip(),
        "job_title": str(payload.get("job_title") or "").strip(),
    }


def _detect_bytedance_auth_result(page, payload: dict[str, object]) -> dict[str, object] | None:
    body_text = _body_text(page).casefold()
    current_url = str(page.url or "").strip()
    if not body_text:
        return None
    if "/login" not in current_url.casefold() and not all(
        fragment in body_text for fragment in ("sign in", "sign in with email", "sign in with mobile")
    ):
        return None

    result = _base_result(page, payload)
    host = urlparse(str(result["job_url"])).netloc or "unknown"
    result.update(
        {
            "status": "skipped_auth",
            "failure_type": "auth_guarded",
            "auth_state": "sign_in_gate",
            "auth_scope": f"{_BOARD}:{host}",
            "message": "ByteDance requires sign in before the application form is available.",
        }
    )
    return result


def _detect_bytedance_live_surface_result(page, payload: dict[str, object]) -> dict[str, object] | None:
    body_text = _body_text(page).casefold()
    if not body_text:
        return None
    markers = ("resume", "basic information", "attachment", "email", "mobile")
    if not all(marker in body_text for marker in markers):
        return None

    result = _base_result(page, payload)
    result.update(
        {
            "status": "unknown",
            "failure_type": "unsupported",
            "message": "ByteDance application surface loaded, but automation is not implemented yet.",
        }
    )
    return result


def _write_terminal_result(page, payload: dict[str, object], result: dict[str, object]) -> None:
    out_dir = Path(str(payload["out_dir"]))
    application_page_html = Path(payload["artifacts"]["application_page_html"])
    application_page_html.parent.mkdir(parents=True, exist_ok=True)
    application_page_html.write_text(page.content(), encoding="utf-8")
    write_submission_result(
        out_dir=out_dir,
        status=str(result["status"]),
        job_url=str(result.get("job_url") or page.url or "").strip(),
        message=str(result["message"]),
        failure_type=str(result["failure_type"]) if result.get("failure_type") else None,
        auth_state=str(result["auth_state"]) if result.get("auth_state") else None,
        auth_scope=str(result["auth_scope"]) if result.get("auth_scope") else None,
        board=str(payload.get("board") or _BOARD),
        provider=str(payload.get("provider") or "").strip() or None,
        artifacts={"application_page_html": str(application_page_html)},
    )


def _post_navigate(page, payload_path: Path) -> None:
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    page.wait_for_timeout(2000)
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass

    result = _detect_bytedance_auth_result(page, payload)
    if result is None:
        result = _detect_bytedance_live_surface_result(page, payload)
    if result is None:
        result = _base_result(page, payload)
        result["message"] = "ByteDance did not present a recognizable login gate or supported live application surface."

    _write_terminal_result(page, payload, result)
    print(str(result["message"]), file=sys.stderr)


def _fill_step(page, step: dict) -> None:
    del page, step


def _classify_submit_state(page) -> dict[str, object]:
    del page
    return {"status": "pending"}


def _run_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    del submit
    return run_simple_board_pipeline(
        payload_path,
        headless=headless,
        submit=False,
        board_name=_BOARD,
        form_selector="body",
        submit_button_names=("Submit",),
        fill_step_fn=_fill_step,
        classify_state_fn=_classify_submit_state,
        preferred_capture_selectors=("body",),
        post_navigate_hook=lambda page: _post_navigate(page, payload_path),
    )


def main() -> int:
    return autofill_main(
        board_name=_BOARD,
        build_payload_fn=_build_payload,
        has_browser=True,
        run_browser_fn=_run_browser,
    )


if __name__ == "__main__":
    raise SystemExit(main())
