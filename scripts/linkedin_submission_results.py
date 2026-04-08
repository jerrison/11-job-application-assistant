from __future__ import annotations

import json
from pathlib import Path


def _submission_result_path(out_dir: Path) -> Path:
    return out_dir / "submit" / "application_submission_result.json"


def _linkedin_job_closed_reason(page) -> str | None:
    body_text = str(page.inner_text("body") or "").lower()
    markers = (
        "no longer accepting applications",
        "job is no longer open",
        "this job is closed",
        "job posting is no longer available",
    )
    for marker in markers:
        if marker in body_text:
            return marker
    return None


def _write_already_applied_result(out_dir: Path, payload: dict) -> None:
    result = {
        "status": "already_applied",
        "website_confirmed": True,
        "job_url": payload["job_url"],
    }
    result_path = _submission_result_path(out_dir)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2))


def _write_not_easy_apply_result(
    out_dir: Path,
    payload: dict,
    *,
    reason: str,
    screenshot_path: Path | None = None,
) -> None:
    message_by_reason = {
        "external_apply": "LinkedIn job no longer exposes Easy Apply; an external Apply flow is shown instead.",
        "no_apply_button": "LinkedIn job does not currently expose an Easy Apply or external Apply control.",
    }
    result = {
        "status": "not_easy_apply",
        "reason": reason,
        "failure_type": reason,
        "message": message_by_reason.get(reason, f"LinkedIn job is not Easy Apply: {reason}."),
        "job_url": payload["job_url"],
    }
    if screenshot_path is not None and screenshot_path.exists():
        result["artifacts"] = {"page_screenshot": str(screenshot_path)}
    result_path = _submission_result_path(out_dir)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2))


def _write_job_closed_result(
    out_dir: Path,
    payload: dict,
    *,
    reason: str,
    screenshot_path: Path | None = None,
) -> None:
    result = {
        "status": "job_closed",
        "failure_type": "job_closed",
        "message": f"LinkedIn job closed: {reason}",
        "job_url": payload["job_url"],
        "website_confirmed": True,
    }
    if screenshot_path is not None and screenshot_path.exists():
        result["artifacts"] = {"page_screenshot": str(screenshot_path)}
    result_path = _submission_result_path(out_dir)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2))
