#!/usr/bin/env python3
"""Shared browser-session helpers for saved-portal imports."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def saved_portal_browser_session(
    *,
    profile_dir: str | Path,
    lock_file: str | Path,
    headless: bool,
    purpose: str,
    normalize_zoom_hosts: tuple[str, ...] = (),
    reset_default_zoom: bool = False,
) -> Iterator[object]:
    from browser_runtime import launch_chromium_browser, normalize_chromium_profile_zoom
    from playwright.sync_api import sync_playwright

    resolved_profile_dir = Path(profile_dir).expanduser()
    resolved_lock_file = Path(lock_file).expanduser()

    resolved_profile_dir.mkdir(parents=True, exist_ok=True)
    if normalize_zoom_hosts or reset_default_zoom:
        normalize_chromium_profile_zoom(
            resolved_profile_dir,
            hosts=normalize_zoom_hosts,
            reset_default_zoom=reset_default_zoom,
        )

    lock_fd = open(resolved_lock_file, "w")  # noqa: SIM115
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        with sync_playwright() as playwright:
            browser = launch_chromium_browser(
                playwright,
                headless=headless,
                persistent_profile_dir=str(resolved_profile_dir),
                prefer_local_browser=True,
                provider="local",
                purpose=purpose,
            )
            try:
                yield browser
            finally:
                browser.close()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
