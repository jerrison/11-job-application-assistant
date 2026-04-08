#!/usr/bin/env python3
"""Open the persistent Playwright browser profile for manual setup.

Launch a headed browser using the same profile directory that autofill workers
use, so the user can sign into Google (improves reCAPTCHA v3 scores) or any
other service.  Close the browser window when done.

Usage:
    uv run python scripts/setup_browser_profile.py
    uv run python scripts/setup_browser_profile.py --url https://accounts.google.com
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from browser_runtime import (
    launch_chromium_browser,
    submit_browser_profile_dir,
    submit_viewport,
)
from project_env import load_project_env

load_project_env()

DEFAULT_URL = "https://accounts.google.com"


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up the Playwright browser profile (sign into Google, etc.).")
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"URL to open (default: {DEFAULT_URL}).",
    )
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: Playwright is not installed. Run: uv run playwright install chromium", file=sys.stderr)
        return 1

    profile_dir = submit_browser_profile_dir()
    print(f"Profile directory: {profile_dir}")
    print(f"Opening {args.url}")
    print("Sign into Google (and any other services), then close the browser window.")

    with sync_playwright() as playwright:
        viewport = submit_viewport()
        browser = launch_chromium_browser(
            playwright,
            headless=False,
            slow_mo=0,
            channel_env_var="JOB_ASSETS_SUBMIT_BROWSER_CHANNEL",
            executable_env_var="JOB_ASSETS_SUBMIT_BROWSER_EXECUTABLE",
            persistent_profile_dir=profile_dir,
            prefer_local_browser=True,
            viewport=viewport,
            device_scale_factor=2,
            purpose="browser profile setup",
        )
        page = browser.new_page(viewport=viewport, device_scale_factor=2)
        page.goto(args.url, wait_until="domcontentloaded")

        # Keep the browser open until the user closes it.
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        finally:
            browser.close()

    print("Profile setup complete. Future autofill runs will use this session.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
