#!/usr/bin/env python3
"""Sync the master resume from a configured remote source to a local markdown file."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from app_paths import material_path, sync_state_path

MASTER_RESUME_SOURCE_URL_ENV = "JOB_ASSETS_MASTER_RESUME_SOURCE_URL"
_GOOGLE_DOC_RE = re.compile(r"^https?://docs\.google\.com/document/d/([A-Za-z0-9_-]+)")

OUTPUT_MD = material_path("master_resume.md")
STATE_JSON = sync_state_path(".master_resume_sync_state.json")


def _source_urls() -> tuple[str, str]:
    source_url = os.environ.get(MASTER_RESUME_SOURCE_URL_ENV, "").strip()
    if not source_url:
        raise RuntimeError(f"Set {MASTER_RESUME_SOURCE_URL_ENV} to a public text source before syncing.")

    match = _GOOGLE_DOC_RE.match(source_url)
    if match:
        doc_id = match.group(1)
        export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    else:
        export_url = source_url
    return source_url, export_url


def fetch_resume_text(export_url: str) -> str:
    req = Request(export_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=60) as response:
            payload = response.read()
    except HTTPError as exc:
        raise RuntimeError(f"HTTP error while downloading resume: {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error while downloading resume: {exc.reason}") from exc

    text = payload.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    return text.rstrip("\n") + "\n"


def compute_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_previous_hash() -> str | None:
    if not STATE_JSON.exists():
        return None
    try:
        state = json.loads(STATE_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return state.get("content_sha256")


def render_markdown(body_text: str, content_sha256: str, *, source_url: str) -> str:
    synced_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    return (
        "# Master Resume\n\n"
        f"Source: {source_url}\n\n"
        f"Last synced (UTC): {synced_at}\n\n"
        f"Content SHA256: `{content_sha256}`\n\n"
        "---\n\n"
        f"{body_text}"
    )


def write_state(content_sha256: str, *, source_url: str, export_url: str) -> None:
    state_payload = {
        "source_url": source_url,
        "export_url": export_url,
        "content_sha256": content_sha256,
        "updated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }
    STATE_JSON.write_text(
        json.dumps(state_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


SYNC_STALENESS_SECONDS = 24 * 60 * 60  # 24 hours


def _last_synced_at() -> datetime | None:
    """Return the timestamp of the last successful sync, or None."""
    if not STATE_JSON.exists():
        return None
    try:
        state = json.loads(STATE_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    ts = state.get("updated_at_utc")
    if ts is None:
        return None
    return datetime.fromisoformat(ts)


def sync_if_stale(max_age_seconds: int = SYNC_STALENESS_SECONDS) -> bool:
    """Sync resume from a configured source if last sync is older than *max_age_seconds*.

    Returns True if a sync was performed, False if still fresh.
    Safe to call from any context — errors are logged and swallowed.
    """
    import logging

    log = logging.getLogger(__name__)
    if not os.environ.get(MASTER_RESUME_SOURCE_URL_ENV, "").strip():
        log.info("master resume sync skipped; %s is not set", MASTER_RESUME_SOURCE_URL_ENV)
        return False
    last = _last_synced_at()
    if last is not None:
        age = (datetime.now(UTC) - last).total_seconds()
        if age < max_age_seconds:
            return False

    log.info("master resume sync is stale (last=%s), syncing from configured source...", last)
    try:
        main()
        return True
    except (RuntimeError, OSError) as exc:
        log.warning("resume sync failed (non-fatal): %s", exc)
        return False


def main() -> int:
    source_url, export_url = _source_urls()
    text = fetch_resume_text(export_url)
    new_hash = compute_sha256(text)
    old_hash = read_previous_hash()

    markdown_missing = not OUTPUT_MD.exists()
    changed = markdown_missing or new_hash != old_hash

    if not changed:
        print("No resume changes detected; markdown is already up to date.")
        return 0

    markdown = render_markdown(text, new_hash, source_url=source_url)
    OUTPUT_MD.write_text(markdown, encoding="utf-8")
    write_state(new_hash, source_url=source_url, export_url=export_url)

    if markdown_missing:
        print(f"Created {OUTPUT_MD} from configured source.")
    else:
        print(f"Updated {OUTPUT_MD} because resume content changed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
