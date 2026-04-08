# LinkedIn Easy Apply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add LinkedIn Easy Apply as a new board so jobs without external apply URLs are autofilled directly on LinkedIn instead of going to `needs_board_url`.

**Architecture:** LinkedIn becomes board `"linkedin"` in the existing registry. URL resolver returns the LinkedIn URL itself (instead of `None`) for Easy Apply jobs. `autofill_linkedin.py` implements a custom multi-step wizard pipeline using the `.playwright-linkedin/` persistent profile with file lock serialization. Reuses `autofill_main()` entry point and all existing answer generation infrastructure.

**Tech Stack:** Python 3.14, Playwright (sync API), existing `autofill_pipeline.py` / `application_submit_common.py` / `autofill_common.py`

**Spec:** `docs/superpowers/specs/2026-03-20-linkedin-easy-apply-design.md`

---

## File Structure

### New Files
- `scripts/autofill_linkedin.py` — Board autofill script with custom wizard pipeline (~400 lines)
- `tests/test_autofill_linkedin.py` — Unit tests for LinkedIn board

### Modified Files
- `scripts/url_resolver.py` — Return LinkedIn URL for Easy Apply; add to `_is_known_board_url()`
- `scripts/submit_application.py` — Register `"linkedin"` board + script mapping
- `scripts/job_board_urls.py` — Add `looks_like_linkedin_easy_apply_url()` + `canonical_linkedin_job_url()`
- `scripts/pipeline_orchestrator.py` — Add `"linkedin"` to fallback `_detect_board_from_url()`; skip `mark_linkedin_job_applied()` when board is `"linkedin"`
- `CLAUDE.md` — Document LinkedIn Easy Apply
- `docs/autofill-patterns.md` — Add LinkedIn Easy Apply section

---

## Task 1: URL Detection — `job_board_urls.py`

**Files:**
- Modify: `scripts/job_board_urls.py` (after `looks_like_bamboohr_url()` ~line 282)
- Test: `tests/test_autofill_linkedin.py` (create)

- [ ] **Step 1: Create test file with URL detection tests**

```python
# tests/test_autofill_linkedin.py
"""Tests for LinkedIn Easy Apply board."""

import importlib.util
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LinkedInUrlDetectionTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module("job_board_urls", "scripts/job_board_urls.py")

    def test_looks_like_linkedin_easy_apply_url_positive(self):
        assert self.mod.looks_like_linkedin_easy_apply_url(
            "https://www.linkedin.com/jobs/view/1234567890/"
        )

    def test_looks_like_linkedin_easy_apply_url_with_query_params(self):
        assert self.mod.looks_like_linkedin_easy_apply_url(
            "https://www.linkedin.com/jobs/view/1234567890/?currentJobId=123&refId=abc"
        )

    def test_looks_like_linkedin_easy_apply_url_negative_non_job(self):
        assert not self.mod.looks_like_linkedin_easy_apply_url(
            "https://www.linkedin.com/in/someone/"
        )

    def test_looks_like_linkedin_easy_apply_url_negative_other_site(self):
        assert not self.mod.looks_like_linkedin_easy_apply_url(
            "https://lever.co/jobs/1234"
        )

    def test_canonical_linkedin_job_url_strips_query_params(self):
        result = self.mod.canonical_linkedin_job_url(
            "https://www.linkedin.com/jobs/view/1234567890/?currentJobId=123&refId=abc&trk=foo"
        )
        assert result == "https://www.linkedin.com/jobs/view/1234567890/"

    def test_canonical_linkedin_job_url_adds_trailing_slash(self):
        result = self.mod.canonical_linkedin_job_url(
            "https://www.linkedin.com/jobs/view/1234567890"
        )
        assert result == "https://www.linkedin.com/jobs/view/1234567890/"

    def test_canonical_linkedin_job_url_preserves_already_clean(self):
        url = "https://www.linkedin.com/jobs/view/1234567890/"
        assert self.mod.canonical_linkedin_job_url(url) == url
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_autofill_linkedin.py -v`
Expected: FAIL — `looks_like_linkedin_easy_apply_url` not found

- [ ] **Step 3: Implement URL detection functions**

Add to `scripts/job_board_urls.py` after the last `looks_like_*` function:

```python
def looks_like_linkedin_easy_apply_url(url: str) -> bool:
    """True if *url* is a LinkedIn job view page (potential Easy Apply)."""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    return "linkedin.com" in host and "/jobs/view/" in parsed.path


def canonical_linkedin_job_url(url: str) -> str:
    """Normalize a LinkedIn job URL: strip query params, ensure trailing slash."""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.rstrip("/") + "/"
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
```

Also wire `canonical_linkedin_job_url` into the `canonical_url()` dispatcher function (~line 308 in `job_board_urls.py`). Add a check before the existing board-specific canonical URL calls:

```python
if looks_like_linkedin_easy_apply_url(url):
    return canonical_linkedin_job_url(url)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_autofill_linkedin.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Lint**

Run: `uv run ruff check scripts/job_board_urls.py tests/test_autofill_linkedin.py`
Expected: All checks passed

- [ ] **Step 6: Commit**

```bash
git add scripts/job_board_urls.py tests/test_autofill_linkedin.py
git commit -m "feat(linkedin): add URL detection and canonical URL functions"
```

---

## Task 2: URL Resolver — Return LinkedIn URL for Easy Apply

**Files:**
- Modify: `scripts/url_resolver.py` — `_extract_linkedin_apply_url()` (~line 442), `_is_known_board_url()` (~line 29)
- Test: `tests/test_autofill_linkedin.py` (add tests)

- [ ] **Step 1: Add URL resolver tests**

Append to `tests/test_autofill_linkedin.py`:

```python
class LinkedInUrlResolverTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module("url_resolver", "scripts/url_resolver.py")

    def test_is_known_board_url_recognizes_linkedin_jobs_view(self):
        assert self.mod._is_known_board_url(
            "https://www.linkedin.com/jobs/view/1234567890/"
        )

    def test_is_known_board_url_rejects_linkedin_profile(self):
        assert not self.mod._is_known_board_url(
            "https://www.linkedin.com/in/someone/"
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_autofill_linkedin.py::LinkedInUrlResolverTests -v`
Expected: FAIL — `_is_known_board_url` returns False for LinkedIn job URLs

- [ ] **Step 3: Add LinkedIn to `_is_known_board_url()`**

In `scripts/url_resolver.py`, find `_is_known_board_url()` (~line 29). Add a check for LinkedIn job view URLs:

```python
from job_board_urls import looks_like_linkedin_easy_apply_url
# Inside _is_known_board_url():
if looks_like_linkedin_easy_apply_url(url):
    return True
```

- [ ] **Step 4: Modify `_extract_linkedin_apply_url()` to return LinkedIn URL for Easy Apply**

In `scripts/url_resolver.py`, find `_extract_linkedin_apply_url()` (~line 442). Currently returns `None` when no external apply URL is found. Change the end of the function:

Before the final `return None`, add Easy Apply detection:

```python
# Check if this is an Easy Apply job (has Easy Apply button, no external link)
easy_apply_btn = page.query_selector(
    'button.jobs-apply-button[aria-label*="Easy Apply"], '
    'button[aria-label*="Easy Apply"]'
)
if easy_apply_btn:
    log.info("LinkedIn Easy Apply detected — returning LinkedIn URL as board URL")
    # Return the canonical LinkedIn job URL itself
    from job_board_urls import canonical_linkedin_job_url
    return canonical_linkedin_job_url(page.url)
# No apply mechanism found
return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_autofill_linkedin.py -v`
Expected: All tests PASS

- [ ] **Step 6: Lint**

Run: `uv run ruff check scripts/url_resolver.py tests/test_autofill_linkedin.py`
Expected: All checks passed

- [ ] **Step 7: Commit**

```bash
git add scripts/url_resolver.py tests/test_autofill_linkedin.py
git commit -m "feat(linkedin): return LinkedIn URL for Easy Apply instead of None"
```

---

## Task 3: Board Registration — `submit_application.py`

**Files:**
- Modify: `scripts/submit_application.py` — `_board_for_url()` (~line 78), `_script_for_board()` (~line 156)
- Test: `tests/test_autofill_linkedin.py` (add tests)

- [ ] **Step 1: Add board registration tests**

Append to `tests/test_autofill_linkedin.py`:

```python
class LinkedInBoardRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module("submit_application", "scripts/submit_application.py")

    def test_board_for_url_detects_linkedin(self):
        result = self.mod._board_for_url(
            "https://www.linkedin.com/jobs/view/1234567890/",
            extraction_method="",
            application_method="",
        )
        assert result == "linkedin"

    def test_script_for_board_returns_linkedin_script(self):
        path = self.mod._script_for_board("linkedin")
        assert path.name == "autofill_linkedin.py"
        assert path.parent.name == "scripts"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_autofill_linkedin.py::LinkedInBoardRegistrationTests -v`
Expected: FAIL — `_board_for_url` doesn't recognize LinkedIn

- [ ] **Step 3: Add LinkedIn host pattern to `_board_for_url()`**

In `scripts/submit_application.py`, add after the existing HOST_PATTERNS definitions (~line 68):

```python
LINKEDIN_HOST_PATTERNS = ("linkedin.com",)
```

Then inside `_board_for_url()`, add the LinkedIn hostname check BEFORE the Greenhouse fallback + HTML probing section (before the `# Fallback: fetch page and probe HTML` comment). Place it with the other simple hostname checks:

```python
if any(h in hostname for h in LINKEDIN_HOST_PATTERNS) and "/jobs/view/" in url:
    return "linkedin"
```

The `/jobs/view/` path check prevents matching non-job LinkedIn URLs.

- [ ] **Step 4: Add LinkedIn to `_script_for_board()`**

In `scripts/submit_application.py`, inside `_script_for_board()` (~line 156), add:

```python
if board == "linkedin":
    return SCRIPT_DIR / "autofill_linkedin.py"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_autofill_linkedin.py -v`
Expected: All tests PASS

- [ ] **Step 6: Lint**

Run: `uv run ruff check scripts/submit_application.py tests/test_autofill_linkedin.py`
Expected: All checks passed

- [ ] **Step 7: Commit**

```bash
git add scripts/submit_application.py tests/test_autofill_linkedin.py
git commit -m "feat(linkedin): register linkedin board in submit_application"
```

---

## Task 4: Pipeline Orchestrator — Board Detection Fallback & Skip Marking

**Files:**
- Modify: `scripts/pipeline_orchestrator.py` — `_detect_board_from_url()` (~line 1354), `_post_submit()` (~line 1540)

- [ ] **Step 1: Add LinkedIn to fallback board_patterns dict**

In `scripts/pipeline_orchestrator.py`, find `_detect_board_from_url()` (~line 1354). In the `board_patterns` dict (~line 1363), add:

```python
"linkedin": ("linkedin.com/jobs/view",),
```

- [ ] **Step 2: Skip `mark_linkedin_job_applied()` when board is `"linkedin"`**

In `scripts/pipeline_orchestrator.py`, find the post-submit section where `mark_linkedin_job_applied()` is called (~line 1540). The existing code checks `if "linkedin.com" in source_url`. Wrap the call with a board check to skip when the application was already submitted on LinkedIn:

```python
if "linkedin.com" in source_url and board != "linkedin":
    from url_resolver import mark_linkedin_job_applied
    marked = mark_linkedin_job_applied(source_url)
```

Note: use `board` (already computed at line ~1488) and `source_url` (fetched from DB at ~line 1542), not `source`.

- [ ] **Step 3: Lint**

Run: `uv run ruff check scripts/pipeline_orchestrator.py`
Expected: All checks passed

- [ ] **Step 4: Run full test suite**

Run: `uv run python -m pytest tests/ -v -k "not test_submit_browser_profile_dir_with_worker_id and not test_greenhouse_application_url_keeps_direct"`
Expected: All pass (excluding 2 pre-existing failures)

- [ ] **Step 5: Commit**

```bash
git add scripts/pipeline_orchestrator.py
git commit -m "feat(linkedin): add fallback board detection and skip redundant marking"
```

---

## Task 5: Core Autofill Script — `autofill_linkedin.py` Scaffold

**Files:**
- Create: `scripts/autofill_linkedin.py`

This is the largest task. Build the scaffold first (entry point, payload builder, browser function signature), then fill in the wizard logic.

- [ ] **Step 1: Create scaffold with `autofill_main()` entry point**

Create `scripts/autofill_linkedin.py`:

```python
#!/usr/bin/env python3
"""LinkedIn Easy Apply autofill — multi-step wizard via persistent LinkedIn profile."""

from __future__ import annotations

import fcntl
import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
_LINKEDIN_PROFILE_DIR = PROJECT_ROOT / ".playwright-linkedin"
_LINKEDIN_LOCK_FILE = PROJECT_ROOT / ".playwright-linkedin.lock"

# ── Board constants ──────────────────────────────────────────────────────────
from autofill_common import board_file_constants

_BOARD = "linkedin"
_BOARD_CONSTANTS = board_file_constants(_BOARD)


def _build_payload(out_dir: Path, provider: str | None = None) -> dict:
    """Build a minimal payload — wizard steps are discovered at runtime."""
    from application_submit_common import (
        parse_application_profile,
        parse_master_resume,
    )

    meta_path = out_dir / ".pipeline_meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    resume_md = (out_dir / "content" / "master_resume.md")
    candidate = parse_master_resume(resume_md.read_text()) if resume_md.exists() else None

    app_profile_path = out_dir / "content" / "application_profile.md"
    app_profile = (
        parse_application_profile(app_profile_path.read_text())
        if app_profile_path.exists()
        else None
    )

    # Find generated resume PDF
    resume_pdf = None
    for pattern in ("submit/*.pdf", "*.pdf"):
        pdfs = sorted(out_dir.glob(pattern))
        for p in pdfs:
            if "cover" not in p.stem.lower():
                resume_pdf = str(p)
                break
        if resume_pdf:
            break

    # Find cover letter PDF (optional)
    cover_letter_pdf = None
    for pattern in ("submit/*cover*.pdf", "*cover*.pdf"):
        covers = sorted(out_dir.glob(pattern))
        if covers:
            cover_letter_pdf = str(covers[0])
            break

    return {
        "board": _BOARD,
        "job_url": meta.get("jd_url", meta.get("url", "")),
        "out_dir": str(out_dir),
        "job_title": meta.get("role", ""),
        "company": meta.get("company_proper", meta.get("company", "")),
        "candidate_name": candidate.full_name if candidate else "",
        "candidate_email": candidate.email if candidate else "",
        "candidate_phone": candidate.phone if candidate else "",
        "resume_path": resume_pdf,
        "cover_letter_path": cover_letter_pdf,
        "mode": "review-before-submit",
        "artifacts": _BOARD_CONSTANTS,
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
    payload: dict, out_dir: Path, headless: bool, submit: bool,
) -> int:
    """Navigate the LinkedIn Easy Apply multi-step wizard."""
    # Placeholder — implemented in Task 6
    raise NotImplementedError("Easy Apply wizard not yet implemented")


def main() -> int:
    from autofill_pipeline import autofill_main

    return autofill_main(
        board_name=_BOARD,
        build_payload_fn=_build_payload,
        run_browser_fn=_run_browser,
    )


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Verify it imports cleanly**

Run: `uv run python -c "import sys; sys.path.insert(0, 'scripts'); import autofill_linkedin; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Lint**

Run: `uv run ruff check scripts/autofill_linkedin.py`
Expected: All checks passed

- [ ] **Step 4: Commit**

```bash
git add scripts/autofill_linkedin.py
git commit -m "feat(linkedin): scaffold autofill_linkedin.py with payload builder"
```

---

## Task 6: Easy Apply Wizard — Browser Automation

**Files:**
- Modify: `scripts/autofill_linkedin.py` — replace `_run_easy_apply_wizard()` placeholder

- [ ] **Step 1: Implement the wizard flow**

Replace `_run_easy_apply_wizard()` in `scripts/autofill_linkedin.py`:

```python
def _run_easy_apply_wizard(
    payload: dict, out_dir: Path, headless: bool, submit: bool,
) -> int:
    """Navigate the LinkedIn Easy Apply multi-step wizard."""
    import random

    from application_submit_common import (
        generate_application_answers,
        parse_application_profile,
        parse_master_resume,
    )
    from autofill_common import (
        capture_full_page,
        label_matches,
        select_option,
        write_report,
    )
    from browser_runtime import human_fill
    from playwright.sync_api import sync_playwright

    job_url = payload["job_url"]
    artifacts = payload["artifacts"]
    pages_dir = out_dir / artifacts["page_screenshots_dir"]
    pages_dir.mkdir(parents=True, exist_ok=True)

    _LINKEDIN_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(_LINKEDIN_PROFILE_DIR),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1360, "height": 900},
            slow_mo=125 if not headless else 0,
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            return _wizard_flow(
                page, payload, out_dir, submit,
                pages_dir=pages_dir,
            )
        finally:
            context.close()


def _wizard_flow(
    page, payload: dict, out_dir: Path, submit: bool, *, pages_dir: Path,
) -> int:
    """Core wizard logic — navigate, fill, screenshot, submit."""
    import random

    from autofill_common import write_report

    job_url = payload["job_url"]
    artifacts = payload["artifacts"]

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
        "you applied" in page_text
        or "application submitted" in page_text
        or "applied on" in page_text
    ):
        log.info("already applied to this job — skipping")
        _write_already_applied_result(out_dir, payload)
        return 0

    # ── Step 3: Click Easy Apply button ──────────────────────────────────
    easy_apply_btn = page.locator(
        'button.jobs-apply-button:has-text("Easy Apply"), '
        'button[aria-label*="Easy Apply"]'
    ).first
    if not easy_apply_btn.is_visible(timeout=5000):
        log.error("Easy Apply button not found — job may be taken down or external apply")
        return 1

    easy_apply_btn.click()
    page.wait_for_timeout(2000)

    # ── Step 4: Loop through wizard steps ────────────────────────────────
    step_num = 0
    all_filled_steps = []
    unknown_questions = []

    while True:
        step_num += 1
        log.info("processing wizard step %d", step_num)

        # Wait for modal content to stabilize
        modal = page.locator(
            'div.jobs-easy-apply-modal, '
            'div[data-test-modal], '
            'div.artdeco-modal'
        ).first
        if not modal.is_visible(timeout=10000):
            log.error("Easy Apply modal not visible at step %d", step_num)
            return 1

        page.wait_for_timeout(1000)

        # Capture per-step screenshot
        step_screenshot = pages_dir / f"page_{step_num:02d}.png"
        page.screenshot(path=str(step_screenshot), full_page=False)

        # Check if this is the review/submit step
        modal_text = modal.inner_text().lower()
        is_review_step = (
            "review your application" in modal_text
            or "review and submit" in modal_text
        )
        is_submit_step = bool(
            modal.locator('button:has-text("Submit application")').count()
        )

        if is_review_step or is_submit_step:
            log.info("reached review/submit step at step %d", step_num)
            break

        # ── Discover and fill fields on this step ────────────────────
        filled_on_step = _fill_wizard_step(
            page, modal, payload, out_dir, all_filled_steps, unknown_questions,
        )
        all_filled_steps.extend(filled_on_step)

        # ── Uncheck "Follow company" if present ──────────────────────
        _uncheck_follow_company(modal)

        # ── Click Next/Continue ──────────────────────────────────────
        next_btn = modal.locator(
            'button[aria-label="Continue to next step"], '
            'button:has-text("Next"), '
            'button:has-text("Continue"), '
            'button[data-easy-apply-next-button]'
        ).first
        if not next_btn.is_visible(timeout=3000):
            # Maybe single-step form — check for submit button
            submit_btn = modal.locator('button:has-text("Submit application")').first
            if submit_btn.is_visible(timeout=2000):
                log.info("single-step form — submit button found")
                break
            log.error("no Next or Submit button found at step %d", step_num)
            return 1

        next_btn.click()
        page.wait_for_timeout(2000)

        # Check for validation errors after clicking Next
        error_msgs = modal.locator(
            '.artdeco-inline-feedback--error, '
            '[data-test-form-element-error], '
            '.fb-dash-form-element__error-text'
        )
        if error_msgs.count() > 0:
            error_texts = [e.inner_text() for e in error_msgs.all()]
            log.warning("validation errors at step %d: %s", step_num, error_texts)
            # Stay on this step — retry fill
            step_num -= 1
            continue

        # Safety: max 20 steps
        if step_num >= 20:
            log.error("exceeded 20 wizard steps — aborting")
            return 1

    # ── Step 5: At review/submit step ────────────────────────────────────
    # Re-query modal to avoid stale DOM reference after wizard navigation
    modal = page.locator(
        'div.jobs-easy-apply-modal, '
        'div[data-test-modal], '
        'div.artdeco-modal'
    ).first

    payload["steps"] = all_filled_steps
    payload["unknown_questions"] = unknown_questions

    # Capture pre-submit screenshot
    pre_submit_path = out_dir / artifacts["pre_submit_screenshot"]
    pre_submit_path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(pre_submit_path), full_page=False)
    log.info("pre-submit screenshot saved to %s", pre_submit_path)

    # Write autofill report
    write_report(payload, board_name=_BOARD)

    if not submit:
        log.info("draft mode — stopping before submit")
        return 0

    # ── Step 6: Check for captcha before submit ──────────────────────────
    from autofill_common import wait_for_captcha_resolution

    captcha_selectors = (
        'iframe[src*="recaptcha"]',
        'iframe[src*="hcaptcha"]',
        '.g-recaptcha',
    )
    for sel in captcha_selectors:
        if page.locator(sel).count():
            log.info("captcha detected before submit — waiting for manual resolution")
            captcha_result = wait_for_captcha_resolution(
                page, headless=False, payload=payload,
                board_title="LinkedIn Easy Apply",
            )
            if captcha_result.get("status") == "blocked":
                log.error("captcha not resolved")
                return 75  # CAPTCHA_SKIP_EXIT_CODE
            break

    # ── Step 7: Submit ───────────────────────────────────────────────────
    submit_btn = modal.locator('button:has-text("Submit application")').first
    if not submit_btn.is_visible(timeout=5000):
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
        post_submit_path = out_dir / artifacts["post_submit_screenshot"]
        page.screenshot(path=str(post_submit_path), full_page=False)
        return 0

    log.warning("submit clicked but confirmation not detected — check manually")
    post_submit_path = out_dir / artifacts["post_submit_screenshot"]
    page.screenshot(path=str(post_submit_path), full_page=False)
    return 0


def _write_already_applied_result(out_dir: Path, payload: dict) -> None:
    """Write a result file indicating the job was already applied to."""
    result = {
        "status": "already_applied",
        "website_confirmed": True,
        "job_url": payload["job_url"],
    }
    result_path = out_dir / "submit" / "application_submission_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2))
```

- [ ] **Step 2: Lint and verify imports**

Run: `uv run ruff check scripts/autofill_linkedin.py`
Expected: All checks passed

- [ ] **Step 3: Commit**

```bash
git add scripts/autofill_linkedin.py
git commit -m "feat(linkedin): implement Easy Apply wizard flow"
```

---

## Task 7: Field Filling Logic

**Files:**
- Modify: `scripts/autofill_linkedin.py` — add `_fill_wizard_step()` and `_uncheck_follow_company()`

- [ ] **Step 1: Implement `_fill_wizard_step()`**

Add to `scripts/autofill_linkedin.py`:

```python
def _fill_wizard_step(
    page, modal, payload: dict, out_dir: Path,
    prior_steps: list[dict], unknown_questions: list[dict],
) -> list[dict]:
    """Discover fields on the current wizard step and fill them.

    Returns list of step dicts that were filled.
    """
    filled_steps = []

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
            filled_steps.append({
                "field_name": "cover_letter", "label": label_text,
                "kind": "file", "value": payload["cover_letter_path"],
                "source": "generated_cover_letter", "filled": True,
                "required": False,
            })
        elif payload.get("resume_path"):
            fi.set_input_files(payload["resume_path"])
            filled_steps.append({
                "field_name": "resume", "label": label_text,
                "kind": "file", "value": payload["resume_path"],
                "source": "generated_resume", "filled": True,
                "required": True,
            })
        page.wait_for_timeout(1000)

    # ── Text inputs ──────────────────────────────────────────────────
    text_inputs = modal.locator(
        'input[type="text"]:visible, '
        'input[type="tel"]:visible, '
        'input[type="email"]:visible, '
        'input[type="number"]:visible'
    )
    for i in range(text_inputs.count()):
        inp = text_inputs.nth(i)
        _fill_text_field(inp, modal, payload, filled_steps)

    # ── Textareas ────────────────────────────────────────────────────
    textareas = modal.locator('textarea:visible')
    for i in range(textareas.count()):
        ta = textareas.nth(i)
        _fill_textarea_field(ta, modal, payload, out_dir, filled_steps, unknown_questions)

    # ── Select dropdowns ─────────────────────────────────────────────
    selects = modal.locator('select:visible')
    for i in range(selects.count()):
        sel = selects.nth(i)
        _fill_select_field(sel, modal, payload, out_dir, filled_steps, unknown_questions)

    # ── LinkedIn custom dropdowns (artdeco) ──────────────────────────
    # LinkedIn uses custom dropdown components, not native <select>
    custom_dropdowns = modal.locator(
        '[data-test-text-selectable-option], '
        '.fb-dash-form-element--select'
    )
    for i in range(custom_dropdowns.count()):
        dd = custom_dropdowns.nth(i)
        _fill_custom_dropdown(dd, modal, payload, out_dir, filled_steps, unknown_questions)

    # ── Radio buttons ────────────────────────────────────────────────
    radio_groups = modal.locator('fieldset:visible')
    for i in range(radio_groups.count()):
        fieldset = radio_groups.nth(i)
        _fill_radio_group(fieldset, payload, out_dir, filled_steps, unknown_questions)

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
    # Try preceding sibling label
    name = element.get_attribute("name") or element.get_attribute("placeholder") or ""
    return name.strip()


def _fill_text_field(inp, modal, payload: dict, filled_steps: list[dict]) -> None:
    """Fill a text/tel/email/number input."""
    from autofill_common import label_matches
    from browser_runtime import human_fill

    label = _get_field_label(inp, modal)
    current_value = inp.input_value().strip()

    # Skip if already filled with a reasonable value
    if current_value and len(current_value) > 1:
        filled_steps.append({
            "field_name": label.lower().replace(" ", "_"),
            "label": label, "kind": "text", "value": current_value,
            "source": "pre-filled", "filled": True, "required": True,
        })
        return

    value = None
    source = "unknown"

    if label_matches(label, "first", "name") or label_matches(label, "given", "name"):
        value = payload.get("candidate_name", "").split()[0] if payload.get("candidate_name") else None
        source = "master_resume.md"
    elif label_matches(label, "last", "name") or label_matches(label, "family", "name") or label_matches(label, "surname"):
        parts = payload.get("candidate_name", "").split()
        value = parts[-1] if len(parts) > 1 else None
        source = "master_resume.md"
    elif label_matches(label, "full", "name") or label_matches(label, "your name"):
        value = payload.get("candidate_name")
        source = "master_resume.md"
    elif label_matches(label, "email"):
        value = payload.get("candidate_email")
        source = "master_resume.md"
    elif label_matches(label, "phone", "mobile", "cell", "telephone"):
        value = payload.get("candidate_phone")
        source = "master_resume.md"
    elif label_matches(label, "city", "location"):
        value = "San Francisco, CA"
        source = "application_profile.md"
    elif label_matches(label, "salary", "compensation", "pay", "desired salary"):
        value = "Open and flexible"
        source = "hardcoded"
    elif label_matches(label, "linkedin"):
        value = payload.get("candidate_linkedin", "")
        source = "master_resume.md"
    elif label_matches(label, "website", "portfolio", "url", "github"):
        value = payload.get("candidate_website", "")
        source = "master_resume.md"

    if value:
        inp.clear()
        human_fill(inp, value)
        filled_steps.append({
            "field_name": label.lower().replace(" ", "_"),
            "label": label, "kind": "text", "value": value,
            "source": source, "filled": True, "required": True,
        })


def _fill_textarea_field(
    ta, modal, payload: dict, out_dir: Path,
    filled_steps: list[dict], unknown_questions: list[dict],
) -> None:
    """Fill a textarea — may require LLM-generated answer."""
    from application_submit_common import generate_application_answers
    from autofill_common import label_matches
    from browser_runtime import human_fill

    label = _get_field_label(ta, modal)
    current_value = ta.input_value().strip()
    if current_value and len(current_value) > 5:
        filled_steps.append({
            "field_name": label.lower().replace(" ", "_"),
            "label": label, "kind": "textarea", "value": current_value,
            "source": "pre-filled", "filled": True, "required": False,
        })
        return

    # Try LLM-generated answer
    meta = json.loads((out_dir / ".pipeline_meta.json").read_text()) if (out_dir / ".pipeline_meta.json").exists() else {}
    field_name = label.lower().replace(" ", "_")
    answers = generate_application_answers(
        out_dir=out_dir, meta=meta,
        question_specs=[{"field_name": field_name, "label": label, "kind": "textarea", "required": True}],
    )
    answer = answers.get(field_name, "")
    if answer:
        ta.clear()
        human_fill(ta, answer)
        filled_steps.append({
            "field_name": field_name, "label": label, "kind": "textarea",
            "value": answer, "source": "generated_application_answer",
            "filled": True, "required": True,
        })
    else:
        unknown_questions.append({"field_name": field_name, "label": label, "kind": "textarea"})


def _fill_select_field(
    sel, modal, payload: dict, out_dir: Path,
    filled_steps: list[dict], unknown_questions: list[dict],
) -> None:
    """Fill a native <select> dropdown."""
    from autofill_common import label_matches, select_option

    label = _get_field_label(sel, modal)

    # Get all options
    options = sel.locator("option")
    option_texts = [o.inner_text().strip() for o in options.all() if o.inner_text().strip()]

    answer = _answer_for_select(label, option_texts, payload, out_dir)
    if answer:
        matched = select_option(option_texts, answer)
        if matched:
            sel.select_option(label=matched)
            filled_steps.append({
                "field_name": label.lower().replace(" ", "_"),
                "label": label, "kind": "select", "option": matched,
                "source": "application_profile.md", "filled": True, "required": True,
            })
            return

    unknown_questions.append({"field_name": label.lower().replace(" ", "_"), "label": label, "kind": "select"})


def _fill_custom_dropdown(
    dd, modal, payload: dict, out_dir: Path,
    filled_steps: list[dict], unknown_questions: list[dict],
) -> None:
    """Fill a LinkedIn artdeco custom dropdown (click to expand, pick option)."""
    from autofill_common import label_matches, select_option

    label = _get_field_label(dd, modal)

    # Click to expand
    trigger = dd.locator('button, [role="combobox"], [data-test-text-selectable-option__trigger]').first
    if trigger.count():
        trigger.click()
        dd.page.wait_for_timeout(500)

    # Read options from the dropdown list
    options = dd.locator('[role="option"], li[data-test-text-selectable-option__option]')
    option_texts = [o.inner_text().strip() for o in options.all() if o.inner_text().strip()]

    answer = _answer_for_select(label, option_texts, payload, out_dir)
    if answer:
        matched = select_option(option_texts, answer)
        if matched:
            for o in options.all():
                if o.inner_text().strip() == matched:
                    o.click()
                    filled_steps.append({
                        "field_name": label.lower().replace(" ", "_"),
                        "label": label, "kind": "select", "option": matched,
                        "source": "application_profile.md", "filled": True, "required": True,
                    })
                    dd.page.wait_for_timeout(500)
                    return

    # Close dropdown if no match
    dd.page.keyboard.press("Escape")
    unknown_questions.append({"field_name": label.lower().replace(" ", "_"), "label": label, "kind": "select"})


def _answer_for_select(label: str, options: list[str], payload: dict, out_dir: Path) -> str | None:
    """Determine the best answer for a select/dropdown based on label."""
    from application_submit_common import (
        question_is_culture_careers_optin,
        question_is_salary_comfort_check,
    )
    from autofill_common import label_matches

    low = label.lower()
    if label_matches(label, "how", "hear", "find", "learn", "source"):
        return "LinkedIn"
    if label_matches(label, "authorization", "authorized", "legally", "work rights", "visa"):
        return "Yes"
    if label_matches(label, "sponsor"):
        return "No"
    if label_matches(label, "experience", "years"):
        # Try to find best match from options
        return None  # Let LLM handle via generate_application_answers
    if question_is_salary_comfort_check(label):
        return "Yes"
    if question_is_culture_careers_optin(label):
        return "Yes"
    if label_matches(label, "gender", "race", "ethnicity", "veteran", "disability"):
        return "Decline to self-identify"
    return None


def _fill_radio_group(
    fieldset, payload: dict, out_dir: Path,
    filled_steps: list[dict], unknown_questions: list[dict],
) -> None:
    """Fill a radio button group."""
    from autofill_common import label_matches

    legend = fieldset.locator("legend, span.fb-dash-form-element__label")
    label = legend.first.inner_text().strip() if legend.count() else ""

    radios = fieldset.locator('input[type="radio"]')
    if not radios.count():
        return

    # Get radio labels
    radio_options = []
    for r in radios.all():
        r_id = r.get_attribute("id") or ""
        r_label = fieldset.locator(f'label[for="{r_id}"]')
        r_text = r_label.first.inner_text().strip() if r_label.count() else r.get_attribute("value") or ""
        radio_options.append((r, r_text))

    answer = _answer_for_select(label, [t for _, t in radio_options], payload, out_dir)
    if answer:
        from autofill_common import select_option
        matched = select_option([t for _, t in radio_options], answer)
        if matched:
            for r, t in radio_options:
                if t == matched:
                    r.click()
                    filled_steps.append({
                        "field_name": label.lower().replace(" ", "_"),
                        "label": label, "kind": "radio", "option": matched,
                        "source": "application_profile.md", "filled": True, "required": True,
                    })
                    return

    unknown_questions.append({"field_name": label.lower().replace(" ", "_"), "label": label, "kind": "radio"})


def _fill_checkbox(cb, modal, payload: dict, filled_steps: list[dict]) -> None:
    """Fill a checkbox."""
    label = _get_field_label(cb, modal)
    low = label.lower()

    # Skip follow company (handled separately) and priority
    if "follow" in low or "priority" in low:
        return

    from application_submit_common import question_is_culture_careers_optin
    if question_is_culture_careers_optin(label):
        if not cb.is_checked():
            cb.click()
        filled_steps.append({
            "field_name": label.lower().replace(" ", "_"),
            "label": label, "kind": "checkbox", "checked": True,
            "source": "hardcoded", "filled": True, "required": False,
        })


def _uncheck_follow_company(modal) -> None:
    """Uncheck the 'Follow company' checkbox if present."""
    follow_cb = modal.locator(
        'input[type="checkbox"][id*="follow"], '
        'label:has-text("Follow") input[type="checkbox"]'
    )
    for i in range(follow_cb.count()):
        cb = follow_cb.nth(i)
        if cb.is_checked():
            cb.click()
            log.info("unchecked Follow company checkbox")
```

- [ ] **Step 2: Lint**

Run: `uv run ruff check scripts/autofill_linkedin.py`
Expected: All checks passed

- [ ] **Step 3: Verify imports**

Run: `uv run python -c "import sys; sys.path.insert(0, 'scripts'); import autofill_linkedin; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add scripts/autofill_linkedin.py
git commit -m "feat(linkedin): implement field filling logic for Easy Apply wizard"
```

---

## Task 8: Documentation Updates

**Files:**
- Modify: `CLAUDE.md`, `docs/autofill-patterns.md`

- [ ] **Step 1: Update CLAUDE.md**

Add to the board list in the project overview line:

```
(Greenhouse, Ashby, Lever, Gem, Dover, Workday, Phenom, iCIMS, Eightfold, BambooHR, SmartRecruiters, Workable, Comeet, Rippling, Uber, Motion Recruitment, Reducto, LinkedIn Easy Apply)
```

- [ ] **Step 2: Update `docs/autofill-patterns.md`**

Add a new section:

```markdown
## LinkedIn Easy Apply

- **Board name**: `linkedin`
- **Detection**: `linkedin.com/jobs/view/` URLs where Easy Apply button is present (no external apply link)
- **Browser profile**: `.playwright-linkedin/` (NOT `~/.job-assets/playwright-submit-profile`). Requires LinkedIn login via `LINKEDIN_EMAIL`/`LINKEDIN_PASSWORD` env vars
- **Concurrency**: Serialized via `.playwright-linkedin.lock` file lock — one Easy Apply at a time across all workers
- **Wizard**: Multi-step modal. Steps discovered dynamically (not hardcoded). Loop until "Review" or "Submit application" detected.
- **Draft mode**: Headed browser. Fills all wizard steps, captures screenshot at review page, exits.
- **Submit mode**: Headed browser. Fills all steps, clicks "Submit application", waits for confirmation.
- **Hardcoded behaviors**: "Follow company" = always uncheck. Priority = never mark. "How did you hear" = "LinkedIn". Salary = "Open and flexible".
- **Already applied**: Detected on job page before clicking Easy Apply. Writes `application_submission_result.json` with `status: "already_applied"`.
- **Post-submit**: `mark_linkedin_job_applied()` is skipped (redundant — already on LinkedIn).
- **`ensure_google_session()`**: Skipped — separate browser profile without Google sign-in.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/autofill-patterns.md
git commit -m "docs: add LinkedIn Easy Apply to board list and autofill patterns"
```

---

## Task 9: Integration Test — End-to-End Smoke Test

**Files:**
- Modify: `tests/test_autofill_linkedin.py`

- [ ] **Step 1: Add payload builder test**

Append to `tests/test_autofill_linkedin.py`:

```python
class LinkedInPayloadTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

    def test_build_payload_returns_correct_board(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            content_dir = out_dir / "content"
            content_dir.mkdir()
            # Write minimal meta
            (out_dir / ".pipeline_meta.json").write_text(
                '{"jd_url": "https://www.linkedin.com/jobs/view/123/", "role": "PM", "company": "Acme"}'
            )
            payload = self.mod._build_payload(out_dir)
            assert payload["board"] == "linkedin"
            assert payload["job_url"] == "https://www.linkedin.com/jobs/view/123/"
            assert payload["company"] == "Acme"
            assert payload["steps"] == []
            assert payload["fields"] == []

    def test_build_payload_finds_resume_pdf(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            content_dir = out_dir / "content"
            content_dir.mkdir()
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            (out_dir / ".pipeline_meta.json").write_text('{"jd_url": "https://linkedin.com/jobs/view/1/"}')
            # Create a fake resume PDF
            (submit_dir / "resume.pdf").write_bytes(b"%PDF-fake")
            payload = self.mod._build_payload(out_dir)
            assert payload["resume_path"] is not None
            assert "resume.pdf" in payload["resume_path"]


class LinkedInUncheckFollowTests(unittest.TestCase):
    def test_uncheck_follow_company_function_exists(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        assert callable(mod._uncheck_follow_company)

    def test_answer_for_select_returns_linkedin_for_how_hear(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        result = mod._answer_for_select(
            "How did you hear about us?",
            ["LinkedIn", "Google", "Referral", "Other"],
            {}, Path("/tmp"),
        )
        assert result == "LinkedIn"

    def test_answer_for_select_returns_yes_for_authorization(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        result = mod._answer_for_select(
            "Are you legally authorized to work in the United States?",
            ["Yes", "No"],
            {}, Path("/tmp"),
        )
        assert result == "Yes"

    def test_answer_for_select_returns_no_for_sponsorship(self):
        mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
        result = mod._answer_for_select(
            "Will you require sponsorship?",
            ["Yes", "No"],
            {}, Path("/tmp"),
        )
        assert result == "No"
```

- [ ] **Step 2: Run all LinkedIn tests**

Run: `uv run python -m pytest tests/test_autofill_linkedin.py -v`
Expected: All tests PASS

- [ ] **Step 3: Run full test suite**

Run: `uv run python -m pytest tests/ -v -k "not test_submit_browser_profile_dir_with_worker_id and not test_greenhouse_application_url_keeps_direct"`
Expected: All pass (excluding 2 pre-existing failures)

- [ ] **Step 4: Lint all changed files**

Run: `uv run ruff check scripts/autofill_linkedin.py scripts/url_resolver.py scripts/submit_application.py scripts/job_board_urls.py scripts/pipeline_orchestrator.py tests/test_autofill_linkedin.py`
Expected: All checks passed

- [ ] **Step 5: Commit**

```bash
git add tests/test_autofill_linkedin.py
git commit -m "test: add unit tests for LinkedIn Easy Apply board"
```

- [ ] **Step 6: Push all commits**

```bash
git push
```
