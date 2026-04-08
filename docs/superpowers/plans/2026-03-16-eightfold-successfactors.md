# Eightfold AI + SAP SuccessFactors Board Support — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add autofill support for Eightfold AI (single-page form) and SAP SuccessFactors (auth-gated wizard) job boards.

**Architecture:** Two new board-specific autofill scripts following existing composition patterns. Eightfold uses `run_browser_pipeline()` (like Gem/Ashby). SuccessFactors uses custom `run_browser_fn` (like Workday/iCIMS). Both integrate via shared URL detection, board routing, and deterministic overrides.

**Tech Stack:** Python 3.14, Playwright, `uv run python`, pytest

**Spec:** `docs/superpowers/specs/2026-03-16-eightfold-successfactors-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `scripts/autofill_eightfold.py` | Eightfold single-page autofill (build payload + browser fill) |
| Create | `scripts/autofill_successfactors.py` | SuccessFactors auth + multi-page wizard autofill |
| Create | `tests/test_eightfold_autofill.py` | Eightfold URL detection, canonicalization, overrides |
| Create | `tests/test_successfactors_autofill.py` | SuccessFactors URL detection, canonicalization, auth states, overrides |
| Modify | `scripts/job_board_urls.py:20-27,259-279` | Add host patterns, detection functions, canonicalization, resolve dispatch |
| Modify | `scripts/submit_application.py:69-134` | Add board routing + fix iCIMS `talentcommunity` collision |
| Modify | `scripts/job_worker.py:32-40` | Add board patterns for rate limiting (+ Phenom drive-by fix) |
| Modify | `tests/test_url_resolver.py` | Add URL detection tests for both boards |
| Modify | `tests/test_submit_application.py` | Add board dispatch tests for both boards |
| Modify | `docs/board-architecture.md` | Document both boards |
| Modify | `docs/autofill-patterns.md` | SuccessFactors-specific gotchas |
| Modify | `CLAUDE.md` | Update URL canonicalization section |

---

### Task 1: URL Detection + Canonicalization for Eightfold

**Files:**
- Modify: `scripts/job_board_urls.py:20-27` (add constant), `:200` area (add functions), `:259-279` (add to dispatch)
- Test: `tests/test_eightfold_autofill.py` (create)

- [ ] **Step 1: Write failing tests for Eightfold URL detection**

Create `tests/test_eightfold_autofill.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from job_board_urls import (
    looks_like_eightfold_url,
    canonical_eightfold_job_url,
)


def test_eightfold_direct_host():
    assert looks_like_eightfold_url("https://paypal.eightfold.ai/careers?pid=123")


def test_eightfold_subdomain():
    assert looks_like_eightfold_url("https://netflix.eightfold.ai/careers/apply?pid=456&domain=netflix.com")


def test_not_eightfold():
    assert not looks_like_eightfold_url("https://boards.greenhouse.io/company/jobs/123")
    assert not looks_like_eightfold_url("https://jobs.lever.co/company/abc")


def test_canonical_strips_tracking_params():
    url = (
        "https://paypal.eightfold.ai/careers"
        "?domain=paypal.com&Codes=W-LINKEDIN&query=R0132250"
        "&start=0&location=San+Francisco&pid=274916506310"
        "&sort_by=relevance&filter_distance=80&filter_include_remote=1"
    )
    canon = canonical_eightfold_job_url(url)
    assert "Codes=" not in canon
    assert "sort_by=" not in canon
    assert "filter_distance=" not in canon
    assert "filter_include_remote=" not in canon
    assert "start=" not in canon
    assert "location=" not in canon
    assert "pid=274916506310" in canon
    assert "domain=paypal.com" in canon
    assert "query=R0132250" in canon


def test_canonical_preserves_apply_url():
    url = "https://paypal.eightfold.ai/careers/apply?pid=274916506310&domain=paypal.com"
    canon = canonical_eightfold_job_url(url)
    assert "pid=274916506310" in canon
    assert "domain=paypal.com" in canon


def test_canonical_noop_for_non_eightfold():
    url = "https://boards.greenhouse.io/company/jobs/123"
    assert canonical_eightfold_job_url(url) == url
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_eightfold_autofill.py -v`
Expected: FAIL with `ImportError: cannot import name 'looks_like_eightfold_url'`

- [ ] **Step 3: Implement Eightfold URL detection + canonicalization**

In `scripts/job_board_urls.py`:

After line 27 (`PHENOM_HOST_PATTERNS`), add:
```python
EIGHTFOLD_HOST_PATTERNS = ("eightfold.ai",)
```

After `looks_like_phenom_url()` and `canonical_phenom_job_url()` (around line 257), add:
```python
def looks_like_eightfold_url(url: str) -> bool:
    """Detect Eightfold AI ATS URLs (e.g. paypal.eightfold.ai)."""
    host = (urlparse(url).hostname or "").casefold()
    return any(pattern in host for pattern in EIGHTFOLD_HOST_PATTERNS)


_EIGHTFOLD_KEEP_PARAMS = {"pid", "domain", "query"}


def canonical_eightfold_job_url(url: str) -> str:
    """Strip tracking/filter params, keep pid + domain + query."""
    if not looks_like_eightfold_url(url):
        return url
    parsed = urlparse(url)
    kept = [(k, v) for k, v in parse_qsl(parsed.query) if k in _EIGHTFOLD_KEEP_PARAMS]
    return urlunparse(parsed._replace(query=urlencode(kept)))
```

In `resolve_job_source_url()` (line 259), add before the final `return url` (line 279):
```python
    if looks_like_eightfold_url(url):
        return canonical_eightfold_job_url(url)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_eightfold_autofill.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/job_board_urls.py tests/test_eightfold_autofill.py
git commit -m "feat: add Eightfold AI URL detection and canonicalization"
```

---

### Task 2: URL Detection + Canonicalization for SuccessFactors

**Files:**
- Modify: `scripts/job_board_urls.py` (add constant, functions, dispatch)
- Test: `tests/test_successfactors_autofill.py` (create)

- [ ] **Step 1: Write failing tests for SuccessFactors URL detection**

Create `tests/test_successfactors_autofill.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from job_board_urls import (
    looks_like_successfactors_url,
    canonical_successfactors_job_url,
)


def test_successfactors_direct_host():
    assert looks_like_successfactors_url(
        "https://career4.successfactors.com/careers?company=supermicro"
    )


def test_successfactors_career_subdomain():
    assert looks_like_successfactors_url(
        "https://career2.successfactors.com/career?company=acme&login_ns=register"
    )


def test_not_successfactors():
    assert not looks_like_successfactors_url("https://boards.greenhouse.io/company/jobs/123")
    assert not looks_like_successfactors_url("https://jobs.lever.co/company/abc")
    assert not looks_like_successfactors_url("https://jobs.supermicro.com/job/PM/123/")


def test_canonical_noop_for_company_hosted_jd():
    """Company-hosted JD pages are not successfactors.com hosts, so canonicalization is a noop."""
    url = (
        "https://jobs.supermicro.com/job/San-Jose-PM-Cali/1323446000/"
        "?utm_source=LINKEDIN&utm_medium=referrer"
    )
    assert canonical_successfactors_job_url(url) == url


def test_canonical_preserves_career_job_req_id():
    url = (
        "https://career4.successfactors.com/career?company=supermicro"
        "&career_job_req_id=27484&jobPipeline=LinkedIn"
    )
    canon = canonical_successfactors_job_url(url)
    assert "career_job_req_id=27484" in canon
    assert "company=supermicro" in canon
    assert "jobPipeline" not in canon


def test_canonical_noop_for_non_successfactors():
    url = "https://boards.greenhouse.io/company/jobs/123"
    assert canonical_successfactors_job_url(url) == url
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_successfactors_autofill.py -v`
Expected: FAIL with `ImportError: cannot import name 'looks_like_successfactors_url'`

- [ ] **Step 3: Implement SuccessFactors URL detection + canonicalization**

In `scripts/job_board_urls.py`:

After `EIGHTFOLD_HOST_PATTERNS`, add:
```python
SUCCESSFACTORS_HOST_PATTERNS = ("successfactors.com",)
```

After the Eightfold functions, add:
```python
def looks_like_successfactors_url(url: str) -> bool:
    """Detect SAP SuccessFactors ATS URLs (e.g. career4.successfactors.com)."""
    host = (urlparse(url).hostname or "").casefold()
    return any(pattern in host for pattern in SUCCESSFACTORS_HOST_PATTERNS)


_SUCCESSFACTORS_KEEP_PARAMS = {"company", "career_job_req_id", "career_ns", "locale"}


def canonical_successfactors_job_url(url: str) -> str:
    """Strip tracking params from SuccessFactors URLs, keeping functional params.

    Only handles successfactors.com hostnames. Company-hosted JD pages
    (e.g. jobs.supermicro.com) are not detected by looks_like_successfactors_url()
    and are handled separately via the HTML probe in _board_for_url().
    """
    if not looks_like_successfactors_url(url):
        return url
    parsed = urlparse(url)
    kept = [(k, v) for k, v in parse_qsl(parsed.query) if k in _SUCCESSFACTORS_KEEP_PARAMS]
    return urlunparse(parsed._replace(query=urlencode(kept)))
```

In `resolve_job_source_url()`, add before the final `return url`:
```python
    if looks_like_successfactors_url(url):
        return canonical_successfactors_job_url(url)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_successfactors_autofill.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/job_board_urls.py tests/test_successfactors_autofill.py
git commit -m "feat: add SuccessFactors URL detection and canonicalization"
```

---

### Task 3: Board Routing + iCIMS Disambiguation

**Files:**
- Modify: `scripts/submit_application.py:69-134`
- Modify: `tests/test_submit_application.py`

- [ ] **Step 1: Write failing tests for board routing**

Append to `tests/test_eightfold_autofill.py`:
```python
from submit_application import _board_for_url


def test_board_routing_eightfold():
    assert _board_for_url("https://paypal.eightfold.ai/careers/apply?pid=123") == "eightfold"
```

Append to `tests/test_successfactors_autofill.py`:
```python
from submit_application import _board_for_url


def test_board_routing_successfactors():
    assert _board_for_url("https://career4.successfactors.com/careers?company=supermicro") == "successfactors"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_eightfold_autofill.py::test_board_routing_eightfold tests/test_successfactors_autofill.py::test_board_routing_successfactors -v`
Expected: FAIL (ValueError: Unsupported application board)

- [ ] **Step 3: Add Eightfold + SuccessFactors to `_board_for_url()` and fix iCIMS collision**

In `scripts/submit_application.py`, in `_board_for_url()` (line 69):

Add host pattern constants near the existing ones (lines 41-59 of `submit_application.py` where `GEM_HOST_PATTERNS`, `LEVER_HOST_PATTERNS`, etc. are duplicated locally — follow the existing convention of duplicating, not importing):
```python
EIGHTFOLD_HOST_PATTERNS = ("eightfold.ai",)
SUCCESSFACTORS_HOST_PATTERNS = ("successfactors.com",)
```

In `_board_for_url()`, after the Greenhouse check (line 94-95) and before the iCIMS fallback probe (line 96), add:
```python
    if any(pattern in host for pattern in EIGHTFOLD_HOST_PATTERNS):
        return "eightfold"
    if any(pattern in host for pattern in SUCCESSFACTORS_HOST_PATTERNS):
        return "successfactors"
```

Fix the iCIMS fallback probe (lines 96-111). Replace:
```python
        if "icims.com" in html or "talentbrew" in html_lower or "talentcommunity" in html_lower:
            return "icims"
```
With:
```python
        # SuccessFactors probe MUST run before iCIMS to avoid talentcommunity collision.
        # Both platforms use /talentcommunity/ paths, so we check for SuccessFactors-specific
        # markers first. The standalone "talentcommunity" clause is removed from iCIMS —
        # iCIMS detection now requires icims.com or talentbrew explicitly.
        if "successfactors.com" in html_lower or "j2w.apply.init" in html_lower or "j2w.init" in html_lower:
            return "successfactors"
        if "icims.com" in html or "talentbrew" in html_lower:
            return "icims"
```

In `_script_for_board()` (line 115), add before the final `raise`:
```python
    if board == "eightfold":
        return SCRIPT_DIR / "autofill_eightfold.py"
    if board == "successfactors":
        return SCRIPT_DIR / "autofill_successfactors.py"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_eightfold_autofill.py tests/test_successfactors_autofill.py -v`
Expected: All PASS

- [ ] **Step 5: Write disambiguation tests**

Append to `tests/test_successfactors_autofill.py`:
```python
from unittest.mock import patch, MagicMock


def test_board_routing_successfactors_via_html_probe():
    """A page with successfactors.com in HTML routes to successfactors, not icims."""
    html = b'<html><script src="https://career4.successfactors.com/js/app.js"></script><a href="/talentcommunity/apply/123">Apply</a></html>'

    def mock_urlopen(req, timeout=15):
        resp = MagicMock()
        resp.read.return_value = html
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    with patch("submit_application.urlopen", mock_urlopen):
        assert _board_for_url("https://jobs.supermicro.com/job/PM/123/") == "successfactors"


def test_board_routing_icims_talentbrew_still_works():
    """talentbrew pages still route to icims after the disambiguation change."""
    html = b'<html><link href="https://rmkcdn.successfactors.com/talentbrew/css/app.css"><a href="/talentcommunity/apply/123">Apply</a></html>'

    def mock_urlopen(req, timeout=15):
        resp = MagicMock()
        resp.read.return_value = html
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    # talentbrew in HTML → still routes to icims (successfactors probe finds no j2w markers)
    with patch("submit_application.urlopen", mock_urlopen):
        result = _board_for_url("https://jobs.example.com/job/PM/123/")
        assert result == "icims"


def test_board_routing_talentcommunity_only_is_not_icims():
    """A page with only talentcommunity (no icims.com, no talentbrew, no successfactors) raises ValueError."""
    html = b'<html><a href="/talentcommunity/apply/123">Apply</a></html>'

    def mock_urlopen(req, timeout=15):
        resp = MagicMock()
        resp.read.return_value = html
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    import pytest

    with patch("submit_application.urlopen", mock_urlopen):
        with pytest.raises(ValueError, match="Unsupported"):
            _board_for_url("https://jobs.example.com/job/PM/123/")
```

- [ ] **Step 6: Run existing tests to verify no regression**

Run: `uv run python -m pytest tests/test_submit_application.py tests/test_url_resolver.py -v`
Expected: All PASS (iCIMS disambiguation should not break existing iCIMS detection)

- [ ] **Step 6: Commit**

```bash
git add scripts/submit_application.py tests/test_eightfold_autofill.py tests/test_successfactors_autofill.py
git commit -m "feat: add Eightfold + SuccessFactors board routing, fix iCIMS talentcommunity collision"
```

---

### Task 4: Worker Board Patterns (+ Phenom Drive-by Fix)

**Files:**
- Modify: `scripts/job_worker.py:32-40`

- [ ] **Step 1: Add board patterns**

In `scripts/job_worker.py`, update `_BOARD_PATTERNS` (lines 32-40):

```python
_BOARD_PATTERNS: dict[str, tuple[str, ...]] = {
    "greenhouse": ("greenhouse.io",),
    "ashby": ("ashbyhq.com",),
    "lever": ("lever.co",),
    "workday": ("myworkdayjobs.com", "myworkdaysite.com"),
    "dover": ("app.dover.com",),
    "icims": ("icims.com",),
    "gem": ("gem.com",),
    "phenom": ("phenom.com",),
    "eightfold": ("eightfold.ai",),
    "successfactors": ("successfactors.com",),
}
```

- [ ] **Step 2: Run worker tests**

Run: `uv run python -m pytest tests/test_job_worker.py -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add scripts/job_worker.py
git commit -m "feat: add Eightfold + SuccessFactors + Phenom to worker board patterns"
```

---

### Task 5: Eightfold Autofill Script

**Files:**
- Create: `scripts/autofill_eightfold.py`
- Test: `tests/test_eightfold_autofill.py` (append)

This is modeled on `autofill_gem.py` — single-page form using `run_browser_pipeline()`.

- [ ] **Step 1: Write failing tests for Eightfold payload building**

Append to `tests/test_eightfold_autofill.py`:

```python
def test_eightfold_deterministic_previous_employee():
    """Previous employee questions should always answer No."""
    from autofill_eightfold import _infer_deterministic

    answer = _infer_deterministic("Are you a previous Employee of PayPal or any of its subsidiaries?", [])
    assert answer is not None
    assert answer.casefold() == "no"


def test_eightfold_deterministic_pep():
    """PEP (Politically Exposed Person) questions should always answer No."""
    from autofill_eightfold import _infer_deterministic

    answer = _infer_deterministic(
        "I am related to or associated with a Politically Exposed Person (PEP).",
        ["Yes", "No"],
    )
    assert answer is not None
    assert answer.casefold() == "no"


def test_eightfold_deterministic_nda():
    """NDA acknowledgment should always be checked."""
    from autofill_eightfold import _infer_deterministic

    answer = _infer_deterministic(
        "Yes, I acknowledge and agree to abide by the terms of this Nondisclosure Agreement.",
        [],
    )
    assert answer is not None


def test_eightfold_deterministic_privacy():
    """Privacy consent should always be checked."""
    from autofill_eightfold import _infer_deterministic

    answer = _infer_deterministic(
        "Yes, I have read and consent to this Privacy Statement.",
        [],
    )
    assert answer is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_eightfold_autofill.py::test_eightfold_deterministic_previous_employee -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'autofill_eightfold'`

- [ ] **Step 3: Create `scripts/autofill_eightfold.py`**

Create the full autofill script. Use `autofill_gem.py` as the structural template. Key elements:

```python
#!/usr/bin/env python3
"""Eightfold AI application autofill.

Single-page form — no auth, no wizard. Uses run_browser_pipeline().
Ref: docs/superpowers/specs/2026-03-16-eightfold-successfactors-design.md
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from autofill_common import board_file_constants, label_matches, select_option, write_report
from autofill_pipeline import autofill_main, run_browser_pipeline
from application_submit_common import (
    apply_generated_answer_overrides,
    generate_application_answers,
    question_is_culture_careers_optin,
)
from project_env import load_project_env

_BOARD = "eightfold"
_BOARD_CONSTANTS = board_file_constants(_BOARD)

# --- Deterministic overrides ---

def _infer_deterministic(label: str, options: list[str]) -> str | None:
    """Return a deterministic answer or None to defer to LLM."""
    ll = label.casefold()

    # Previous employee → No
    if "previous employee" in ll or "former employee" in ll:
        return select_option(options, "No") or "No"

    # PEP questions → No
    if "politically exposed person" in ll or "pep" in ll.split():
        return select_option(options, "No") or "No"

    # Relationship to company employee → No
    if "related to" in ll and "employee" in ll and "working in" in ll:
        return select_option(options, "No") or "No"

    # NDA / privacy consent → check (return truthy string)
    if "acknowledge and agree" in ll and "nondisclosure" in ll:
        return "checked"
    if "read and consent" in ll and "privacy" in ll:
        return "checked"

    # Acknowledgment date → today
    if "acknowledgement" in ll or ("declaration" in ll and "date" in ll):
        from datetime import date
        return date.today().isoformat()

    return None


# --- Payload builder ---

def build_payload(out_dir: Path, provider: str) -> dict:
    """Build the autofill payload for an Eightfold application."""
    # Standard payload building: parse JD, generate answers, apply overrides
    # Follow the pattern from autofill_gem.py:build_payload()
    # 1. Load JD, resume, cover letter paths
    # 2. Call generate_application_answers() for LLM-driven answers
    # 3. Apply _infer_deterministic() for board-specific overrides
    # 4. Apply apply_generated_answer_overrides() for shared overrides
    # 5. Return payload dict with steps
    ...  # Full implementation during execution


# --- Browser pipeline callbacks ---

def _fill_step(page, step, *, submit: bool) -> None:
    """Fill a single form field on the Eightfold application page."""
    # Handle field types: textbox, combobox, checkbox, file upload
    # Eightfold comboboxes: click expand button adjacent to combobox, then select option
    # Race/Ethnicity: checkbox group (multi-select)
    # Resume: use set_input_files() on hidden file input
    ...  # Full implementation during execution


def _classify_submit_state(page) -> str:
    """Classify page state after submit click."""
    ...  # Full implementation during execution


# --- Entry point ---

def _run_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    """Wrap run_browser_pipeline with Eightfold-specific callbacks."""
    return run_browser_pipeline(
        payload_path,
        headless=headless,
        submit=submit,
        board_name=_BOARD,
        fill_step_fn=_fill_step,
        page_snapshot_fn=lambda page: page.accessibility.snapshot(),
        classify_state_fn=_classify_submit_state,
        click_submit_fn=lambda page: page.get_by_role("button", name="Submit application").click(),
        capture_fn=lambda page, path: page.screenshot(path=path, full_page=True),
    )


def main() -> int:
    load_project_env()
    return autofill_main(
        _BOARD,
        build_payload,
        has_browser=True,
        run_browser_fn=_run_browser,
    )


if __name__ == "__main__":
    raise SystemExit(main())
```

Note: The `...` placeholders for `build_payload`, `_fill_step`, and `_classify_submit_state` will be fully implemented during execution. The structure, imports, deterministic overrides, and entry point are complete. The browser interaction code requires Playwright testing against a live Eightfold instance to finalize selectors.

- [ ] **Step 4: Run tests to verify deterministic overrides pass**

Run: `uv run python -m pytest tests/test_eightfold_autofill.py -v -k deterministic`
Expected: All deterministic tests PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/autofill_eightfold.py tests/test_eightfold_autofill.py
git commit -m "feat: add Eightfold autofill script with deterministic overrides"
```

---

### Task 6: SuccessFactors Autofill Script — Auth Flow

**Files:**
- Create: `scripts/autofill_successfactors.py`
- Test: `tests/test_successfactors_autofill.py` (append)

This is modeled on `autofill_workday.py` — auth-gated wizard using custom `run_browser_fn`.

- [ ] **Step 1: Write failing tests for SuccessFactors auth state detection**

Append to `tests/test_successfactors_autofill.py`:

```python
def test_successfactors_credentials_env_vars():
    """Credential env vars should fall back to Workday vars."""
    import os
    from autofill_successfactors import _get_credentials

    # Clear both sets of env vars for clean test
    for var in ("SUCCESSFACTORS_EMAIL", "SUCCESSFACTORS_PASSWORD", "WORKDAY_EMAIL", "WORKDAY_PASSWORD"):
        os.environ.pop(var, None)

    os.environ["WORKDAY_EMAIL"] = "test@example.com"
    os.environ["WORKDAY_PASSWORD"] = "TestPass1!"
    try:
        email, password = _get_credentials()
        assert email == "test@example.com"
        assert password == "TestPass1!"
    finally:
        os.environ.pop("WORKDAY_EMAIL", None)
        os.environ.pop("WORKDAY_PASSWORD", None)


def test_successfactors_credentials_prefer_own():
    """SUCCESSFACTORS_* env vars take precedence over WORKDAY_*."""
    import os
    from autofill_successfactors import _get_credentials

    os.environ["SUCCESSFACTORS_EMAIL"] = "sf@example.com"
    os.environ["SUCCESSFACTORS_PASSWORD"] = "SfPass1!"
    os.environ["WORKDAY_EMAIL"] = "wd@example.com"
    os.environ["WORKDAY_PASSWORD"] = "WdPass1!"
    try:
        email, password = _get_credentials()
        assert email == "sf@example.com"
        assert password == "SfPass1!"
    finally:
        for var in ("SUCCESSFACTORS_EMAIL", "SUCCESSFACTORS_PASSWORD", "WORKDAY_EMAIL", "WORKDAY_PASSWORD"):
            os.environ.pop(var, None)


def test_successfactors_page_detection_sign_in():
    """Sign-in page detected by heading text."""
    from autofill_successfactors import _detect_page_from_text

    assert _detect_page_from_text("Career Opportunities: Sign In") == "sign_in"


def test_successfactors_page_detection_create_account():
    from autofill_successfactors import _detect_page_from_text

    assert _detect_page_from_text("Career Opportunities: Create an Account") == "create_account"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_successfactors_autofill.py -v -k "credentials or page_detection"`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create `scripts/autofill_successfactors.py` scaffold with auth flow**

```python
#!/usr/bin/env python3
"""SAP SuccessFactors application autofill.

Auth-gated multi-page wizard. Uses custom run_browser_fn (like Workday/iCIMS).
Ref: docs/superpowers/specs/2026-03-16-eightfold-successfactors-design.md
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from autofill_common import board_file_constants, label_matches, select_option, write_report
from autofill_pipeline import autofill_main
from application_submit_common import (
    apply_generated_answer_overrides,
    generate_application_answers,
    question_is_culture_careers_optin,
)
from output_layout import role_submit_path
from project_env import load_project_env

_BOARD = "successfactors"
_BOARD_CONSTANTS = board_file_constants(_BOARD)

CAPTCHA_SKIP_EXIT_CODE = 75

_SF_EMAIL_ENV = "SUCCESSFACTORS_EMAIL"
_SF_PASSWORD_ENV = "SUCCESSFACTORS_PASSWORD"
_WD_EMAIL_ENV = "WORKDAY_EMAIL"
_WD_PASSWORD_ENV = "WORKDAY_PASSWORD"

# --- Page constants (provisional — see spec) ---
PAGE_SIGN_IN = "sign_in"
PAGE_CREATE_ACCOUNT = "create_account"
PAGE_FORGOT_PASSWORD = "forgot_password"
PAGE_PERSONAL_INFO = "personal_info"
PAGE_EXPERIENCE = "experience"
PAGE_APPLICATION_QUESTIONS = "application_questions"
PAGE_VOLUNTARY_DISCLOSURES = "voluntary_disclosures"
PAGE_REVIEW = "review"
PAGE_CONFIRMATION = "confirmation"
PAGE_UNKNOWN = "unknown"


def _get_credentials() -> tuple[str, str]:
    """Get email/password from env vars, falling back to Workday vars."""
    email = os.environ.get(_SF_EMAIL_ENV) or os.environ.get(_WD_EMAIL_ENV, "")
    password = os.environ.get(_SF_PASSWORD_ENV) or os.environ.get(_WD_PASSWORD_ENV, "")
    return email, password


def _detect_page_from_text(heading_text: str) -> str:
    """Detect the current SuccessFactors page from heading text."""
    ht = heading_text.casefold()
    if "sign in" in ht:
        return PAGE_SIGN_IN
    if "create an account" in ht or "create account" in ht:
        return PAGE_CREATE_ACCOUNT
    if "forgot" in ht and "password" in ht:
        return PAGE_FORGOT_PASSWORD
    if "personal" in ht or "my information" in ht:
        return PAGE_PERSONAL_INFO
    if "experience" in ht or "resume" in ht:
        return PAGE_EXPERIENCE
    if "application question" in ht or "questionnaire" in ht:
        return PAGE_APPLICATION_QUESTIONS
    if "voluntary" in ht or "disclosure" in ht or "self-identify" in ht:
        return PAGE_VOLUNTARY_DISCLOSURES
    if "review" in ht or "submit" in ht:
        return PAGE_REVIEW
    if "confirmation" in ht or "thank you" in ht or "already applied" in ht:
        return PAGE_CONFIRMATION
    return PAGE_UNKNOWN


def _write_auth_failure_log(out_dir: Path, url: str, company: str, role: str, reason: str) -> None:
    """Write auth failure JSON and return CAPTCHA_SKIP_EXIT_CODE for graceful skip."""
    log = {
        "status": "skipped_auth_failure",
        "url": url,
        "company": company,
        "role": role,
        "reason": reason,
        "logged_at_utc": datetime.now(UTC).isoformat(),
    }
    submit_path = role_submit_path(out_dir, "successfactors_auth_failure.json")
    submit_path.parent.mkdir(parents=True, exist_ok=True)
    submit_path.write_text(json.dumps(log, indent=2))


# --- Auth functions (browser-driven) ---

async def _do_sign_in(page, email: str, password: str) -> bool:
    """Attempt sign-in on SuccessFactors. Returns True if successful."""
    ...  # Full implementation during execution — fill email/password, click Sign In, check for error

async def _do_password_reset(page, email: str) -> bool:
    """Attempt password reset via Gmail link. Returns True if successful."""
    ...  # Full implementation during execution — click Forgot Password, fetch Gmail link

async def _do_create_account(page, email: str, password: str, first_name: str, last_name: str) -> bool:
    """Create a new SuccessFactors account. Returns True if successful."""
    ...  # Full implementation during execution — fill registration form


# --- Wizard page fillers ---

async def _fill_personal_info(page, payload: dict) -> None:
    ...  # Full implementation during execution

async def _fill_experience(page, payload: dict) -> None:
    ...  # Full implementation during execution — resume upload

async def _fill_application_questions(page, payload: dict) -> None:
    ...  # Full implementation during execution

async def _fill_voluntary_disclosures(page, payload: dict) -> None:
    ...  # Full implementation during execution


# --- Browser runner ---

def _run_browser(payload_path: Path, headless: bool, submit: bool) -> int:
    """Custom browser runner for SuccessFactors auth + wizard flow."""
    ...  # Full implementation during execution
    # 1. Navigate to job URL
    # 2. Handle auth (sign in → password reset → create account)
    # 3. Loop through wizard pages (max 15 attempts)
    # 4. Fill each page, click Next
    # 5. On review page: screenshot, submit if --submit
    # 6. Check confirmation
    # 7. Write report


# --- Payload builder ---

def build_payload(out_dir: Path, provider: str) -> dict:
    """Build the autofill payload for a SuccessFactors application."""
    ...  # Full implementation during execution


# --- Entry point ---

def main() -> int:
    load_project_env()
    return autofill_main(
        _BOARD,
        build_payload,
        has_browser=True,
        run_browser_fn=_run_browser,
    )


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_successfactors_autofill.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/autofill_successfactors.py tests/test_successfactors_autofill.py
git commit -m "feat: add SuccessFactors autofill scaffold with auth flow and page detection"
```

---

### Task 7: Eightfold Browser Fill Implementation

**Files:**
- Modify: `scripts/autofill_eightfold.py` (flesh out `build_payload`, `_fill_step`, `_classify_submit_state`)

This task requires a live Eightfold instance for Playwright testing. Use the PayPal Eightfold URL from the spec.

- [ ] **Step 1: Implement `build_payload()` following `autofill_gem.py` pattern**

Read `autofill_gem.py:build_payload()` for the exact pattern (load JD, generate answers, apply overrides). Adapt for Eightfold's form structure. The payload should include steps for each form section observed in the accessibility snapshot.

- [ ] **Step 2: Implement `_fill_step()` for Eightfold form fields**

Handle these field types from the Eightfold form:
- **Textbox** — `page.fill(selector, value)`
- **Combobox** — click expand button (adjacent `button` ref), wait for listbox, select matching option
- **Checkbox** — click if not already checked; Race/Ethnicity is a checkbox group
- **File upload** — find hidden `input[type=file]` and use `set_input_files()`
- **Date picker** — fill the combobox with ISO date string

Cookie consent: dismiss at page load by clicking "Accept" button if visible.

- [ ] **Step 3: Implement `_classify_submit_state()`**

Check page snapshot after submit for:
- Success: "application submitted", "thank you", "confirmation"
- Error: "error", "required field", validation messages
- Captcha: reCAPTCHA or similar

- [ ] **Step 4: Test against live Eightfold instance**

Run: `uv run python scripts/autofill_eightfold.py --payload-only --provider gemini path/to/eightfold/output/dir`

Verify payload JSON is generated correctly. Then test browser fill with `--draft` mode.

- [ ] **Step 5: Commit**

```bash
git add scripts/autofill_eightfold.py
git commit -m "feat: implement Eightfold browser fill and payload building"
```

---

### Task 8: SuccessFactors Browser Implementation

**Files:**
- Modify: `scripts/autofill_successfactors.py` (flesh out all `...` placeholders)

This task requires a live SuccessFactors instance. Use the Supermicro URL from the spec. **The wizard pages section is provisional** — explore 2-3 SuccessFactors instances to finalize page detection and filling logic.

- [ ] **Step 1: Implement auth functions (`_do_sign_in`, `_do_password_reset`, `_do_create_account`)**

Follow `autofill_workday.py` auth pattern:
- Sign in: fill email/password textboxes, click "Sign In", check for error messages
- Password reset: click "Forgot your password?" link, fill email, fetch Gmail link, navigate + set new password
- Create account: fill all fields (email x2, password x2, name, country combobox → "United States", check career opportunities checkbox, accept terms), click "Create Account"

- [ ] **Step 2: Implement `_run_browser()` wizard loop**

Follow `autofill_workday.py` wizard pattern:
- Loop max 15 pages
- On each page: detect page type via heading, call appropriate filler, click Next
- On auth pages: run auth flow
- On review page: screenshot + submit
- On confirmation: write report + return 0
- On auth failure: write auth failure log + return 75

- [ ] **Step 3: Implement page fillers**

Flesh out `_fill_personal_info`, `_fill_experience`, `_fill_application_questions`, `_fill_voluntary_disclosures` based on actual form fields observed behind the auth wall.

- [ ] **Step 4: Implement `build_payload()`**

Follow the Workday pattern — parse JD, generate answers, apply shared overrides.

- [ ] **Step 5: Test against live SuccessFactors instance**

Run: `uv run python scripts/autofill_successfactors.py --draft path/to/successfactors/output/dir`

Verify auth flow works, wizard pages are detected, form fields are filled.

- [ ] **Step 6: Commit**

```bash
git add scripts/autofill_successfactors.py
git commit -m "feat: implement SuccessFactors auth flow and wizard page filling"
```

---

### Task 9: Documentation Updates

**Files:**
- Modify: `docs/board-architecture.md`
- Modify: `docs/autofill-patterns.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update `docs/board-architecture.md`**

Add Eightfold and SuccessFactors to the board support table. Include:
- Board name, URL patterns, complexity class
- Eightfold: single-page, `run_browser_pipeline()`, no auth
- SuccessFactors: auth-gated wizard, custom `run_browser_fn`, sign-in/password-reset/create-account flow

- [ ] **Step 2: Update `docs/autofill-patterns.md`**

Add SuccessFactors-specific gotchas:
- SAP UI5 framework quirks
- Auth flow: sign in first → password reset → create account
- Company-hosted JD pages redirect to `career*.successfactors.com`
- `talentcommunity` paths shared with iCIMS — disambiguation logic
- Provisional wizard page structure

- [ ] **Step 3: Update `CLAUDE.md` URL canonicalization section**

Add:
- **Eightfold** — strips tracking params (`Codes`, `sort_by`, `filter_distance`, etc.), keeps `pid` + `domain` + `query`
- **SuccessFactors** — strips UTM tracking params from company-hosted JD pages; keeps `company` + `career_job_req_id` for `successfactors.com` URLs

- [ ] **Step 4: Commit**

```bash
git add docs/board-architecture.md docs/autofill-patterns.md CLAUDE.md
git commit -m "docs: add Eightfold + SuccessFactors to board architecture and patterns"
```

---

### Task 10: Full Test Suite Verification

- [ ] **Step 1: Run lint**

Run: `uv run ruff check scripts/ tests/`
Expected: No errors

- [ ] **Step 2: Run architecture check**

Run: `uv run python scripts/check_architecture.py`
Expected: PASS

- [ ] **Step 3: Run full test suite**

Run: `uv run python -m pytest tests/ -v`
Expected: All PASS, no regressions

- [ ] **Step 4: Final commit if any fixes needed**

```bash
git add -u
git commit -m "fix: address lint/test issues from Eightfold + SuccessFactors integration"
```
