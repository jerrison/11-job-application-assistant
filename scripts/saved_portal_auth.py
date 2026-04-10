#!/usr/bin/env python3
"""Manual saved-portal sign-in helpers."""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from saved_portal_browser import saved_portal_browser_session


def _wait_for_browser_close(browser, *, poll_ms: int = 500) -> None:
    context = getattr(browser, "context", None)
    if context is None:
        return

    while True:
        try:
            open_pages = [page for page in list(getattr(context, "pages", [])) if not page.is_closed()]
        except Exception:  # noqa: BLE001
            open_pages = []

        if not open_pages:
            return

        try:
            open_pages[0].wait_for_timeout(poll_ms)
        except Exception:  # noqa: BLE001
            return


def _run_gws_json(args: list[str]) -> dict:
    if not shutil.which("gws"):
        raise RuntimeError("gws CLI is not installed.")

    completed = subprocess.run(
        ["gws", *args],
        cwd=str(Path(__file__).resolve().parent.parent),
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(
            "gws CLI failed while fetching a portal verification code. "
            "Make sure `gws auth login` is complete. "
            f"Command: {' '.join(['gws', *args])}. Details: {detail}"
        )

    output = (completed.stdout or "").strip()
    if not output:
        raise RuntimeError(f"gws CLI returned empty output for {' '.join(['gws', *args])}")

    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gws CLI returned non-JSON output for {' '.join(['gws', *args])}: {output[:400]}"
        ) from exc


def _gmail_after_query_term(min_received_at_utc: datetime | None) -> str:
    if min_received_at_utc is None:
        return "newer_than:1d"
    return f"after:{int(min_received_at_utc.timestamp())}"


def _gmail_message_received_at_utc(message: dict) -> datetime | None:
    raw_value = str(message.get("internalDate") or "").strip()
    if not raw_value.isdigit():
        return None
    try:
        return datetime.fromtimestamp(int(raw_value) / 1000, tz=UTC)
    except Exception:
        return None


def _iter_gmail_parts(part: dict):
    yield part
    for child in part.get("parts", []) or []:
        yield from _iter_gmail_parts(child)


def _decode_gmail_body_data(data: str | None) -> str:
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(f"{data}{padding}").decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_email_body(message: dict) -> str:
    payload = message.get("payload") or {}
    parts = [str(message.get("snippet") or "")]
    headers = payload.get("headers") or []
    for header in headers:
        if str(header.get("name") or "").casefold() == "subject":
            parts.append(str(header.get("value") or ""))
    body = payload.get("body") or {}
    parts.append(_decode_gmail_body_data(body.get("data")))
    for part in _iter_gmail_parts(payload):
        body = part.get("body") or {}
        parts.append(_decode_gmail_body_data(body.get("data")))
    return "\n".join(piece for piece in parts if piece)


def _extract_security_code(text: str) -> str | None:
    patterns = (
        re.compile(r"\b(\d{6})\b"),
        re.compile(r"(?i)(?:security|verification|one[- ]time|otp)\D{0,24}([A-Z0-9]{4,8})\b"),
    )
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(1).upper()
    return None


def fetch_security_code_from_gmail(
    query_terms: list[str],
    *,
    min_received_at_utc: datetime | None = None,
    wait_seconds: int = 120,
) -> str | None:
    query = " ".join(term for term in (_gmail_after_query_term(min_received_at_utc), *query_terms) if term).strip()
    deadline = time.monotonic() + max(wait_seconds, 0)

    while True:
        list_response = _run_gws_json(
            [
                "gmail",
                "users",
                "messages",
                "list",
                "--params",
                json.dumps({"userId": "me", "maxResults": 10, "q": query}),
            ]
        )
        for message_stub in list_response.get("messages", []):
            message_id = message_stub.get("id")
            if not message_id:
                continue
            message = _run_gws_json(
                [
                    "gmail",
                    "users",
                    "messages",
                    "get",
                    "--params",
                    json.dumps({"userId": "me", "id": message_id, "format": "full"}),
                ]
            )
            received_at = _gmail_message_received_at_utc(message)
            if min_received_at_utc and received_at and received_at < min_received_at_utc:
                continue
            code = _extract_security_code(_extract_email_body(message))
            if code:
                return code
        if time.monotonic() >= deadline:
            return None
        time.sleep(10)


def open_saved_portal_login(
    *,
    profile_dir: str | Path,
    lock_file: str | Path,
    url: str,
    purpose: str,
    normalize_zoom_hosts: tuple[str, ...] = (),
    reset_default_zoom: bool = False,
) -> None:
    print(f"Opening {url} with profile {Path(profile_dir).expanduser()}", flush=True)
    print("Sign in, then close the browser window to finish setup.", flush=True)

    with saved_portal_browser_session(
        profile_dir=profile_dir,
        lock_file=lock_file,
        headless=False,
        purpose=purpose,
        normalize_zoom_hosts=normalize_zoom_hosts,
        reset_default_zoom=reset_default_zoom,
    ) as browser:
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        _wait_for_browser_close(browser)
