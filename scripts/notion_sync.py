#!/usr/bin/env python3
"""Compatibility wrapper for the shared Notion job-application sync logic."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from notion_job_applications import (  # noqa: E402
    EMAIL_CONFIRMATION_JSON,
    NOTION_SYNC_STATUS_JSON,
    SUBMISSION_RESULT_JSON,
    WEBSITE_CONFIRMATION_JSON,
    find_email_confirmation,
    main,
    record_website_confirmation,
    sync_application,
    wait_for_email_confirmation,
)

__all__ = [
    "EMAIL_CONFIRMATION_JSON",
    "find_email_confirmation",
    "NOTION_SYNC_STATUS_JSON",
    "SUBMISSION_RESULT_JSON",
    "WEBSITE_CONFIRMATION_JSON",
    "main",
    "record_website_confirmation",
    "sync_application",
    "wait_for_email_confirmation",
]


if __name__ == "__main__":
    raise SystemExit(main())
