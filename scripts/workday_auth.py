#!/usr/bin/env python3
"""Shared Workday authentication state classification and result building."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from job_board_urls import workday_auth_scope

_WORKDAY_MAINTENANCE_PATTERNS = (
    "workday is currently unavailable",
    "service interruption",
    "check back later",
    "maintenance-page",
)
_WORKDAY_JOB_UNAVAILABLE_PATTERNS = (
    "the page you are looking for doesn't exist",
    "the page you are looking for does not exist",
    "this job is no longer available",
    "this position is no longer available",
    "job posting is no longer available",
    "posting is no longer available",
    "no longer accepting applications",
)
_WORKDAY_CREDENTIAL_REJECTION_PATTERNS = (
    "invalid email or password",
    "invalid username or password",
    "incorrect email or password",
    "incorrect username or password",
    "wrong email address or password",
    "wrong username or password",
    "password is incorrect",
    "email or password is incorrect",
    "account is locked",
    "account might be locked",
    "too many failed login",
    "too many failed sign in",
    "too many failed sign-in",
    "too many failed attempts",
)
_WORKDAY_AUTHENTICATED_NON_FORM_MARKERS = (
    "candidate home",
    "my applications",
    "my tasks",
    "search for jobs",
    "job alerts",
)


def looks_like_workday_authenticated_non_form(
    *,
    page_url: str = "",
    page_text: str = "",
    heading_text: str = "",
    visible_actions: list[str] | None = None,
) -> bool:
    lower_url = page_url.casefold()
    if any(token in lower_url for token in ("/userhome", "/candidate-home", "/candidatehome")):
        return True

    combined = " ".join(
        part.casefold()
        for part in (
            str(heading_text or ""),
            str(page_text or ""),
            " ".join(visible_actions or []),
        )
        if str(part or "").strip()
    )
    marker_hits = sum(1 for marker in _WORKDAY_AUTHENTICATED_NON_FORM_MARKERS if marker in combined)
    return "candidate home" in combined or marker_hits >= 2


def classify_workday_auth_state(
    *,
    page_url: str = "",
    page_text: str = "",
    alert_text: str = "",
    heading_text: str = "",
    visible_actions: list[str] | None = None,
) -> dict[str, object]:
    lower_url = page_url.casefold()
    lower_text = page_text.casefold()
    lower_heading = heading_text.casefold()
    lower_alert = alert_text.casefold()
    lower_actions = " ".join(visible_actions or []).casefold()
    combined = " ".join(part for part in (lower_heading, lower_alert, lower_text, lower_actions) if part)

    if any(pattern in combined or pattern in lower_url for pattern in _WORKDAY_MAINTENANCE_PATTERNS):
        return {"auth_state": "maintenance"}

    if any(pattern in combined for pattern in _WORKDAY_JOB_UNAVAILABLE_PATTERNS):
        return {"auth_state": "job_unavailable"}

    if any(pattern in lower_alert for pattern in _WORKDAY_CREDENTIAL_REJECTION_PATTERNS):
        return {"auth_state": "credential_rejected"}

    if looks_like_workday_authenticated_non_form(
        page_url=page_url,
        page_text=page_text,
        heading_text=heading_text,
        visible_actions=visible_actions,
    ):
        return {"auth_state": "authenticated_non_form"}

    if (
        "verify your account" in combined
        or "resend account verification" in combined
        or "request a verification email" in combined
    ):
        return {"auth_state": "account_verification_gate"}

    if (
        "/passwordreset/" in lower_url
        or "forgot password" in combined
        or "reset password" in combined
        or "password has been reset" in combined
        or "verification code" in combined
    ):
        return {"auth_state": "password_reset_gate"}

    if (
        "create account/sign in" in combined
        or "password requirements" in combined
        or "verify new password" in combined
        or "already have an account?" in combined
        or lower_heading.startswith("create account")
    ):
        return {"auth_state": "create_account_gate"}

    if lower_heading.startswith("sign in") or "/login" in lower_url or "sign in" in combined:
        return {"auth_state": "sign_in_gate"}

    return {"auth_state": "unknown"}


def build_workday_auth_result(
    payload: dict,
    markers: dict[str, object],
    *,
    auth_scope: str | None,
    last_attempted_step: str | None,
    credential_rejection_observed: bool,
    auth_state_hint: str | None = None,
) -> dict[str, object]:
    normalized_auth_state = str(
        classify_workday_auth_state(
            page_url=str(markers.get("page_url") or ""),
            page_text=str(markers.get("page_text_excerpt") or ""),
            alert_text=str(markers.get("alert_text") or ""),
            heading_text=str(markers.get("heading_text") or ""),
            visible_actions=list(markers.get("visible_actions") or []),
        ).get("auth_state")
        or "unknown"
    )
    auth_state = normalized_auth_state
    hinted_auth_state = str(auth_state_hint or "").strip()
    if auth_state == "unknown" and hinted_auth_state and hinted_auth_state != "unknown":
        auth_state = hinted_auth_state
    credential_rejection_observed = credential_rejection_observed or auth_state == "credential_rejected"
    scope = (
        auth_scope
        or workday_auth_scope(str(markers.get("page_url") or ""))
        or workday_auth_scope(payload.get("job_url", ""))
    )
    updated_at = datetime.now(UTC).replace(microsecond=0).isoformat()

    if auth_state == "maintenance":
        status = "service_unavailable"
        message = "Workday is currently unavailable for this tenant. The queue should auto-retry with backoff."
        suggestions = [
            "Wait for the automatic retry to run.",
            "If retries keep failing, open the saved screenshot and auth artifact to confirm the outage persisted.",
        ]
        retryable = True
    elif auth_state == "job_unavailable":
        status = "job_closed"
        message = "job_closed: Workday resolved to a missing or unavailable posting shell instead of the application form."
        suggestions = [
            "Review the saved auth artifact and screenshot to confirm the posting is genuinely unavailable.",
            "Archive the job if the tenant still shows the missing-page shell on rerun.",
        ]
        retryable = False
    elif credential_rejection_observed or auth_state == "credential_rejected":
        auth_state = "credential_rejected"
        status = "auth_failed"
        message = "Workday explicitly rejected the configured credentials after the approved recovery steps."
        suggestions = [
            "Sign in manually on this Workday tenant to confirm the current password still works.",
            "If sign-in fails, use 'Forgot your password?' to reset the password before retrying.",
            "After access works, rerun: uv run scripts/submit_application.py <out_dir> --submit",
        ]
        retryable = False
    else:
        status = "auth_unknown"
        message = (
            "Workday never reached the application form after trying sign in, password reset, "
            "and create account. Saved evidence for diagnosis."
        )
        suggestions = [
            "Review the saved auth artifact, screenshot, and page text to see which gateway state remained.",
            "Try the same tenant manually in this order: sign in, password reset, then create account.",
            "After the tenant state is clear, rerun: uv run scripts/submit_application.py <out_dir> --submit",
        ]
        retryable = False

    return {
        "status": status,
        "board": "workday",
        "auth_state": auth_state,
        "auth_scope": scope,
        "retryable": retryable,
        "credential_rejection_observed": credential_rejection_observed,
        "last_attempted_step": last_attempted_step,
        "job_url": payload.get("job_url", ""),
        "company": payload.get("company", ""),
        "job_title": payload.get("job_title", ""),
        "email": payload.get("candidate_email", ""),
        "page_url": str(markers.get("page_url") or ""),
        "heading_text": str(markers.get("heading_text") or ""),
        "alert_text": str(markers.get("alert_text") or ""),
        "visible_actions": list(markers.get("visible_actions") or []),
        "page_text_excerpt": str(markers.get("page_text_excerpt") or ""),
        "updated_at_utc": updated_at,
        "message": message,
        "suggestions": suggestions,
    }


def write_auth_failure_log(
    out_dir: Path,
    payload: dict,
    page,
    *,
    extract_markers_fn: Callable[[object], dict[str, object]],
    build_result_fn: Callable[..., dict[str, object]],
    write_result_fn: Callable[[Path, dict[str, object]], None],
    auth_scope_fn: Callable[[str], str | None],
) -> None:
    """Write auth failure artifact using injected Workday page/result helpers."""
    markers = (
        extract_markers_fn(page)
        if page
        else {
            "page_url": "",
            "page_text_excerpt": "",
            "heading_text": "",
            "alert_text": "",
            "visible_actions": [],
            "auth_state": "unknown",
        }
    )
    result = build_result_fn(
        payload,
        markers,
        auth_scope=auth_scope_fn(payload.get("job_url", "")),
        last_attempted_step=None,
        credential_rejection_observed=markers.get("auth_state") == "credential_rejected",
    )
    write_result_fn(out_dir, result)
