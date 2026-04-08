#!/usr/bin/env python3
"""Shared helpers for Greenhouse page state and unavailable-job artifacts."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from output_layout import role_submit_path

GENERIC_SUBDOMAINS = frozenset(
    {
        "careers",
        "jobs",
        "apply",
        "hire",
        "work",
        "join",
        "talent",
        "recruiting",
        "employment",
        "opportunities",
        "boards",
        "job-boards",
    }
)


def is_greenhouse_error_page(html: str) -> bool:
    """Detect Greenhouse error/redirect pages that contain no application form."""
    return "error=true" in html[:4000]


def greenhouse_browser_job_closed_reason(url: str, page_text: str) -> str | None:
    """Return a job_closed reason when the browser lands on an unavailable page."""
    lowered_url = str(url or "").casefold()
    normalized_text = re.sub(r"\s+", " ", str(page_text or "").casefold()).strip()

    if "error=true" in lowered_url:
        return f"job_closed: Greenhouse browser reached an unavailable page at {url}"
    if "the job you are looking for is no longer open" in normalized_text:
        return f"job_closed: Greenhouse browser showed an explicit closed-job message at {url}"
    if "sorry, but we can't find that page" in normalized_text:
        return f"job_closed: Greenhouse browser reached a missing application page at {url}"
    if "page not found" in normalized_text and "job board you were viewing is no longer active" in normalized_text:
        return f"job_closed: Greenhouse browser reported that the job board is no longer active at {url}"
    return None


def parse_yes_no_field(value: str, *, field_name: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"yes", "y", "true"}:
        return True
    if normalized in {"no", "n", "false"}:
        return False
    raise ValueError(f"Expected Yes/No for {field_name}, got {value!r}")


def write_job_unavailable_artifact(
    out_dir: Path,
    *,
    job_unavailable_filename: str,
    application_url: str,
    source_url: str | None,
    message: str,
) -> Path:
    path = role_submit_path(out_dir, job_unavailable_filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "job_closed",
        "board": "greenhouse",
        "application_url": application_url,
        "source_url": source_url,
        "message": message,
        "recorded_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def probe_greenhouse_board_slug(job_id: str, candidates: list[str]) -> str | None:
    """Probe the Greenhouse API with candidate board slugs and return the first that works."""
    for slug in candidates:
        api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"
        req = Request(api_url, method="HEAD", headers={"User-Agent": "autofill_greenhouse/1.0"})
        try:
            with urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return slug
        except (HTTPError, URLError, OSError):
            continue
    return None
