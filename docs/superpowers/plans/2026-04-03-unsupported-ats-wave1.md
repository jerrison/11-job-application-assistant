# Unsupported ATS Family Support (Wave 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the repeated `unsupported` stopped-job families with first-class draft-mode support for SuccessFactors, Breezy, Recruitee, Jobvite, JazzHR, and Paycor, including wrapper detection, real redrafts, and screenshot-backed evidence.

**Architecture:** Extend the existing board-family detector stack first, then add explicit board scripts that reuse `autofill_common.py` and `autofill_pipeline.py` instead of building a generic fallback. SuccessFactors uses a custom auth/wizard `run_browser_fn` like Workday/iCIMS; Breezy, Recruitee, Jobvite, JazzHR, and Paycor use `run_browser_pipeline()` with family-specific selectors and submit-state classifiers.

**Tech Stack:** Python 3.14, Playwright, SQLite, pytest, Ruff, `uv run python`

**Spec:** `docs/superpowers/specs/2026-04-03-unsupported-ats-board-support-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `scripts/autofill_successfactors.py` | SuccessFactors / Jobs2Web auth + wizard draft autofill |
| Create | `scripts/autofill_breezy.py` | Breezy HR single-page draft autofill |
| Create | `scripts/autofill_recruitee.py` | Recruitee single-page draft autofill |
| Create | `scripts/autofill_jobvite.py` | Jobvite single-page draft autofill |
| Create | `scripts/autofill_jazzhr.py` | JazzHR / ApplyToJob single-page draft autofill |
| Create | `scripts/autofill_paycor.py` | Paycor Recruiting draft autofill |
| Create | `tests/test_successfactors_autofill.py` | SuccessFactors detection, routing, auth/result, wizard behavior |
| Create | `tests/test_breezy_autofill.py` | Breezy detection, payload, submit-state, fill behavior |
| Create | `tests/test_recruitee_autofill.py` | Recruitee detection, payload, submit-state, fill behavior |
| Create | `tests/test_jobvite_autofill.py` | Jobvite detection, payload, submit-state, fill behavior |
| Create | `tests/test_jazzhr_autofill.py` | JazzHR detection, payload, submit-state, fill behavior |
| Create | `tests/test_paycor_autofill.py` | Paycor detection, payload, submit-state, fill behavior |
| Modify | `scripts/job_board_urls.py` | Add host patterns, wrapper helpers, canonicalization, `resolve_job_source_url()` support |
| Modify | `scripts/submit_application.py` | Extend `_direct_board_for_url()`, `_board_for_url()`, and `_script_for_board()` |
| Modify | `scripts/url_resolver.py` | Recognize the new families as direct boards and preserve company-hosted wrapper resolution |
| Modify | `scripts/job_worker.py` | Add board-pattern / rate-limit support for the new families |
| Modify | `tests/test_submit_application.py` | Add routing/dispatch assertions for all six families |
| Modify | `tests/test_url_resolver.py` | Add direct-board and wrapper-resolution assertions |
| Modify | `docs/board-architecture.md` | Document the new supported boards |
| Modify | `docs/autofill-patterns.md` | Add family-specific gotchas and generalized lessons |
| Modify | `docs/solutions/workflow-issues/stopped-job-audits-must-be-repo-local-and-artifact-backed-2026-03-27.md` | Record new family support rollout and rerun evidence |
| Modify | `/Users/jerrison/Documents/Documents - J MacBook Pro 16/jerrison-personal/job-applications/2026-04-03 stopped-job audit.md` | Mirror the rollout/results into the Obsidian audit note |

---

### Task 1: Add board-family detection and routing for Wave 1

**Files:**
- Modify: `scripts/job_board_urls.py`
- Modify: `scripts/submit_application.py`
- Modify: `scripts/url_resolver.py`
- Modify: `scripts/job_worker.py`
- Modify: `tests/test_submit_application.py`
- Modify: `tests/test_url_resolver.py`
- Create: `tests/test_successfactors_autofill.py`
- Create: `tests/test_breezy_autofill.py`
- Create: `tests/test_recruitee_autofill.py`
- Create: `tests/test_jobvite_autofill.py`
- Create: `tests/test_jazzhr_autofill.py`
- Create: `tests/test_paycor_autofill.py`

- [ ] **Step 1: Write the failing routing and detector tests**

Create the new family test files with detector coverage, for example:

```python
# tests/test_breezy_autofill.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from submit_application import _board_for_url


def test_board_for_url_detects_breezy_direct_host():
    assert _board_for_url("https://zero-hash.breezy.hr/p/7801647b617f-role") == "breezy"
```

```python
# tests/test_recruitee_autofill.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from submit_application import _board_for_url


def test_board_for_url_detects_recruitee_company_wrapper(monkeypatch):
    class _Resp:
        url = "https://careers.distribusion.com/o/senior-pm"

        def read(self, _n=None):
            return b'<html><script src="https://cdn.recruitee.com/assets/app.js"></script></html>'

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: _Resp())
    assert _board_for_url("https://careers.distribusion.com/o/senior-pm") == "recruitee"
```

Add similar tests for:

- `successfactors` direct host and wrapper (`jobs.supermicro.com`)
- `jobvite` direct host
- `jazzhr` via `applytojob.com`
- `paycor` via `recruitingbypaycor.com`

Add URL resolver tests like:

```python
def test_detect_source_returns_direct_for_breezy():
    assert detect_source("https://zero-hash.breezy.hr/p/7801647b617f-role") == "direct"
```

- [ ] **Step 2: Run the detector slice and verify it fails**

Run:

```bash
uv run python -m pytest \
  tests/test_submit_application.py \
  tests/test_url_resolver.py \
  tests/test_successfactors_autofill.py \
  tests/test_breezy_autofill.py \
  tests/test_recruitee_autofill.py \
  tests/test_jobvite_autofill.py \
  tests/test_jazzhr_autofill.py \
  tests/test_paycor_autofill.py -v
```

Expected: FAIL because the new detector helpers, routing branches, and script dispatches do not exist yet.

- [ ] **Step 3: Implement the routing foundation**

In `scripts/job_board_urls.py`, add host patterns and direct helpers:

```python
SUCCESSFACTORS_HOST_PATTERNS = ("successfactors.com",)
BREEZY_HOST_PATTERNS = ("breezy.hr",)
RECRUITEE_HOST_PATTERNS = ("recruitee.com",)
JOBVITE_HOST_PATTERNS = ("jobvite.com",)
JAZZHR_HOST_PATTERNS = ("applytojob.com",)
PAYCOR_HOST_PATTERNS = ("recruitingbypaycor.com",)


def looks_like_successfactors_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return any(pattern in host for pattern in SUCCESSFACTORS_HOST_PATTERNS)


def looks_like_breezy_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return any(pattern in host for pattern in BREEZY_HOST_PATTERNS)


def looks_like_recruitee_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return any(pattern in host for pattern in RECRUITEE_HOST_PATTERNS)


def looks_like_jobvite_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return any(pattern in host for pattern in JOBVITE_HOST_PATTERNS)


def looks_like_jazzhr_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return any(pattern in host for pattern in JAZZHR_HOST_PATTERNS)


def looks_like_paycor_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return any(pattern in host for pattern in PAYCOR_HOST_PATTERNS)
```

Also add deterministic HTML marker probes:

```python
def html_looks_like_successfactors(html: str) -> bool:
    lowered = html.casefold()
    return any(marker in lowered for marker in ("successfactors.com", "jobs2web", "j2w.apply", "j2w.init"))


def html_looks_like_recruitee(html: str) -> bool:
    lowered = html.casefold()
    return "recruitee" in lowered
```

In `scripts/submit_application.py`, extend `_direct_board_for_url()` and `_board_for_url()`:

```python
    if looks_like_successfactors_url(url):
        return "successfactors"
    if looks_like_breezy_url(url):
        return "breezy"
    if looks_like_recruitee_url(url):
        return "recruitee"
    if looks_like_jobvite_url(url):
        return "jobvite"
    if looks_like_jazzhr_url(url):
        return "jazzhr"
    if looks_like_paycor_url(url):
        return "paycor"
```

And in the HTML probe section:

```python
        if _html_looks_like_successfactors(html):
            return "successfactors"
        if "breezy.hr" in html_lower or "breezy" in html_lower:
            return "breezy"
        if "recruitee" in html_lower:
            return "recruitee"
        if "jobvite" in html_lower:
            return "jobvite"
        if "applytojob" in html_lower or "jazzhr" in html_lower:
            return "jazzhr"
        if "recruitingbypaycor.com" in html_lower or "paycor" in html_lower:
            return "paycor"
```

Extend `_script_for_board()`:

```python
    if board == "successfactors":
        return SCRIPT_DIR / "autofill_successfactors.py"
    if board == "breezy":
        return SCRIPT_DIR / "autofill_breezy.py"
    if board == "recruitee":
        return SCRIPT_DIR / "autofill_recruitee.py"
    if board == "jobvite":
        return SCRIPT_DIR / "autofill_jobvite.py"
    if board == "jazzhr":
        return SCRIPT_DIR / "autofill_jazzhr.py"
    if board == "paycor":
        return SCRIPT_DIR / "autofill_paycor.py"
```

In `scripts/url_resolver.py`, add the new families to `_is_known_board_url()` imports and checks. In `scripts/job_worker.py`, extend the board detector/rate-limit patterns for the six new families.

- [ ] **Step 4: Run the detector slice and verify it passes**

Run:

```bash
uv run python -m pytest \
  tests/test_submit_application.py \
  tests/test_url_resolver.py \
  tests/test_successfactors_autofill.py \
  tests/test_breezy_autofill.py \
  tests/test_recruitee_autofill.py \
  tests/test_jobvite_autofill.py \
  tests/test_jazzhr_autofill.py \
  tests/test_paycor_autofill.py -v
```

Expected: PASS for routing/detection assertions, while the board-script behavior tests still fail because the submitters do not exist yet.

- [ ] **Step 5: Commit the routing foundation**

```bash
command git add \
  scripts/job_board_urls.py \
  scripts/submit_application.py \
  scripts/url_resolver.py \
  scripts/job_worker.py \
  tests/test_submit_application.py \
  tests/test_url_resolver.py \
  tests/test_successfactors_autofill.py \
  tests/test_breezy_autofill.py \
  tests/test_recruitee_autofill.py \
  tests/test_jobvite_autofill.py \
  tests/test_jazzhr_autofill.py \
  tests/test_paycor_autofill.py
command git commit -m "feat(boards): add wave 1 unsupported ATS detection"
```

---

### Task 2: Implement SuccessFactors / Jobs2Web support

**Files:**
- Create: `scripts/autofill_successfactors.py`
- Modify: `tests/test_successfactors_autofill.py`
- Modify: `docs/autofill-patterns.md`

- [ ] **Step 1: Write the failing SuccessFactors behavior tests**

Add tests that lock the auth/result contract:

```python
def test_classify_successfactors_sign_in_gate():
    html = '<html><title>Sign In</title><input name="username"/><input name="password"/></html>'
    assert classify_successfactors_auth_state(html=html, url="https://career4.successfactors.com/career") == "sign_in_gate"


def test_classify_successfactors_create_account_gate():
    html = '<html><a>Create an account</a><input name="email"/></html>'
    assert classify_successfactors_auth_state(html=html, url="https://career4.successfactors.com/career?login_ns=register") == "create_account_gate"
```

Add payload/build tests:

```python
def test_build_payload_includes_resume_and_cover_letter(tmp_path, monkeypatch):
    payload = _build_payload(tmp_path, provider="openai")
    kinds = {step["kind"] for step in payload["steps"]}
    assert "file" in kinds
    assert payload["board"] == "successfactors"
```

Add submit-state tests:

```python
def test_classify_submit_state_detects_review_page():
    html = "<html><button>Submit Application</button></html>"
    assert _classify_submit_state(html, url="https://career4.successfactors.com/career") == "review"
```

- [ ] **Step 2: Run the SuccessFactors tests to verify they fail**

Run:

```bash
uv run python -m pytest tests/test_successfactors_autofill.py -v
```

Expected: FAIL because `scripts/autofill_successfactors.py` and its helpers do not exist yet.

- [ ] **Step 3: Implement the minimal SuccessFactors submitter**

Create `scripts/autofill_successfactors.py` with the existing Workday/iCIMS pattern:

```python
#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from autofill_pipeline import CAPTCHA_SKIP_EXIT_CODE, autofill_main
from autofill_common import board_file_constants, capture_full_page, page_snapshot
from output_layout import migrate_role_output_layout

_BOARD = "successfactors"
_BOARD_CONSTANTS = board_file_constants(_BOARD)


def classify_successfactors_auth_state(*, html: str, url: str) -> str | None:
    lowered = html.casefold()
    if "login_ns=register" in url or "create an account" in lowered:
        return "create_account_gate"
    if "forgot your password" in lowered:
        return "password_reset_gate"
    if "sign in" in lowered and "password" in lowered:
        return "sign_in_gate"
    return None
```

Add a `run_browser_fn` that:

- navigates from wrapper or direct URL into the application flow
- classifies sign-in/create-account/password-reset gates
- authenticates using repo env vars if possible
- fills visible form fields with shared policy answers
- captures a pre-submit screenshot at review
- exits as:
  - `0` for draft-ready proof
  - `CAPTCHA_SKIP_EXIT_CODE` for truthful auth/manual/captcha blocks

Reuse these existing patterns explicitly:

- `autofill_workday.py` for auth/result handling
- `autofill_pipeline.py` for current-attempt artifact cleanup and terminal result writing
- `write_report()` / screenshot capture contracts from the other boards

- [ ] **Step 4: Run the SuccessFactors test slice and verify it passes**

Run:

```bash
uv run python -m pytest tests/test_successfactors_autofill.py -v
```

Expected: PASS

- [ ] **Step 5: Commit SuccessFactors support**

```bash
command git add scripts/autofill_successfactors.py tests/test_successfactors_autofill.py docs/autofill-patterns.md
command git commit -m "feat(successfactors): add draft autofill support"
```

---

### Task 3: Implement Breezy support

**Files:**
- Create: `scripts/autofill_breezy.py`
- Modify: `tests/test_breezy_autofill.py`

- [ ] **Step 1: Write the failing Breezy behavior tests**

```python
def test_build_payload_marks_board_breezy(tmp_path):
    payload = _build_payload(tmp_path, provider="openai")
    assert payload["board"] == "breezy"


def test_classify_submit_state_detects_thank_you():
    html = "<html><h1>Thank you for applying</h1></html>"
    assert _classify_submit_state(html, url="https://company.breezy.hr") == "confirmed"
```

- [ ] **Step 2: Run the Breezy test slice and verify it fails**

Run:

```bash
uv run python -m pytest tests/test_breezy_autofill.py -v
```

Expected: FAIL because `scripts/autofill_breezy.py` does not exist yet.

- [ ] **Step 3: Implement the minimal Breezy submitter**

Create `scripts/autofill_breezy.py` following the Workable/SmartRecruiters pattern:

```python
#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_submit_common import (
    APPLICATION_PROFILE_PATH,
    PROJECT_ROOT,
    find_cover_letter_file,
    find_resume_file,
    load_meta,
    parse_application_profile,
    parse_master_resume,
    resolve_shared_question_policy,
)
from autofill_common import board_file_constants, click_submit_button
from autofill_pipeline import autofill_main, run_browser_pipeline
```

Implement:

- `_infer_deterministic(label, options)`
- `_build_payload(out_dir, provider)`
- `_classify_submit_state(html, url, page_title="")`
- `_fill_step(page, step)` if Breezy widgets need special handling

Use `run_browser_pipeline()` as the execution model.

- [ ] **Step 4: Run the Breezy slice and verify it passes**

Run:

```bash
uv run python -m pytest tests/test_breezy_autofill.py -v
```

Expected: PASS

- [ ] **Step 5: Commit Breezy support**

```bash
command git add scripts/autofill_breezy.py tests/test_breezy_autofill.py
command git commit -m "feat(breezy): add draft autofill support"
```

---

### Task 4: Implement Recruitee support

**Files:**
- Create: `scripts/autofill_recruitee.py`
- Modify: `tests/test_recruitee_autofill.py`

- [ ] **Step 1: Write the failing Recruitee behavior tests**

```python
def test_classify_submit_state_detects_review_page():
    html = "<html><button>Submit application</button></html>"
    assert _classify_submit_state(html, url="https://company.recruitee.com") == "review"


def test_build_payload_includes_resume(tmp_path):
    payload = _build_payload(tmp_path, provider="openai")
    assert any(step["kind"] == "file" for step in payload["steps"])
```

- [ ] **Step 2: Run the Recruitee test slice and verify it fails**

Run:

```bash
uv run python -m pytest tests/test_recruitee_autofill.py -v
```

Expected: FAIL because `scripts/autofill_recruitee.py` does not exist yet.

- [ ] **Step 3: Implement the minimal Recruitee submitter**

Create `scripts/autofill_recruitee.py` with the same single-page family shape as Breezy. Add selector handling for:

- file upload
- standard text inputs
- select / combobox options
- privacy / consent checkboxes

Anchor the file around:

```python
_BOARD = "recruitee"
_BOARD_CONSTANTS = board_file_constants(_BOARD)


def _classify_submit_state(html: str, url: str, page_title: str = "") -> str:
    lowered = html.casefold()
    if "thank you for applying" in lowered or "application submitted" in lowered:
        return "confirmed"
    if "submit application" in lowered:
        return "review"
    if "this field is required" in lowered:
        return "validation_error"
    return "unknown"
```

- [ ] **Step 4: Run the Recruitee slice and verify it passes**

Run:

```bash
uv run python -m pytest tests/test_recruitee_autofill.py -v
```

Expected: PASS

- [ ] **Step 5: Commit Recruitee support**

```bash
command git add scripts/autofill_recruitee.py tests/test_recruitee_autofill.py
command git commit -m "feat(recruitee): add draft autofill support"
```

---

### Task 5: Implement Jobvite support

**Files:**
- Create: `scripts/autofill_jobvite.py`
- Modify: `tests/test_jobvite_autofill.py`

- [ ] **Step 1: Write the failing Jobvite behavior tests**

```python
def test_board_payload_uses_jobvite_name(tmp_path):
    payload = _build_payload(tmp_path, provider="openai")
    assert payload["board"] == "jobvite"


def test_jobvite_submit_state_detects_confirmation():
    html = "<html><div>Thank you for your interest</div></html>"
    assert _classify_submit_state(html, url="https://jobs.jobvite.com/company/job/abc") == "confirmed"
```

- [ ] **Step 2: Run the Jobvite test slice and verify it fails**

Run:

```bash
uv run python -m pytest tests/test_jobvite_autofill.py -v
```

Expected: FAIL because `scripts/autofill_jobvite.py` does not exist yet.

- [ ] **Step 3: Implement the minimal Jobvite submitter**

Create `scripts/autofill_jobvite.py` using the same single-page pipeline shape as the other simple ATS families. Include explicit handling for:

- resume upload
- cover letter upload or textarea if exposed
- shared deterministic fields
- validation-error detection

Base the file around:

```python
_BOARD = "jobvite"
SUBMIT_BUTTON_NAMES = ("Submit Application", "Submit application", "Apply", "Apply Now")
```

- [ ] **Step 4: Run the Jobvite slice and verify it passes**

Run:

```bash
uv run python -m pytest tests/test_jobvite_autofill.py -v
```

Expected: PASS

- [ ] **Step 5: Commit Jobvite support**

```bash
command git add scripts/autofill_jobvite.py tests/test_jobvite_autofill.py
command git commit -m "feat(jobvite): add draft autofill support"
```

---

### Task 6: Implement JazzHR / ApplyToJob support

**Files:**
- Create: `scripts/autofill_jazzhr.py`
- Modify: `tests/test_jazzhr_autofill.py`

- [ ] **Step 1: Write the failing JazzHR behavior tests**

```python
def test_classify_submit_state_detects_applytojob_confirmation():
    html = "<html><h2>Application Submitted</h2></html>"
    assert _classify_submit_state(html, url="https://bitpay.applytojob.com/apply/jobs/details/123") == "confirmed"
```

- [ ] **Step 2: Run the JazzHR test slice and verify it fails**

Run:

```bash
uv run python -m pytest tests/test_jazzhr_autofill.py -v
```

Expected: FAIL because `scripts/autofill_jazzhr.py` does not exist yet.

- [ ] **Step 3: Implement the minimal JazzHR submitter**

Create `scripts/autofill_jazzhr.py` and keep the board naming consistent:

```python
_BOARD = "jazzhr"
```

Use `run_browser_pipeline()` and explicit selectors for:

- file uploads
- text fields
- yes/no radio groups
- select elements

Make sure `_classify_submit_state()` recognizes both `applytojob` URL patterns and page-level confirmation text.

- [ ] **Step 4: Run the JazzHR slice and verify it passes**

Run:

```bash
uv run python -m pytest tests/test_jazzhr_autofill.py -v
```

Expected: PASS

- [ ] **Step 5: Commit JazzHR support**

```bash
command git add scripts/autofill_jazzhr.py tests/test_jazzhr_autofill.py
command git commit -m "feat(jazzhr): add draft autofill support"
```

---

### Task 7: Implement Paycor support

**Files:**
- Create: `scripts/autofill_paycor.py`
- Modify: `tests/test_paycor_autofill.py`

- [ ] **Step 1: Write the failing Paycor behavior tests**

```python
def test_classify_submit_state_detects_paycor_review():
    html = "<html><button>Submit Application</button></html>"
    assert _classify_submit_state(html, url="https://recruitingbypaycor.com/career/JobIntroduction.action") == "review"
```

- [ ] **Step 2: Run the Paycor test slice and verify it fails**

Run:

```bash
uv run python -m pytest tests/test_paycor_autofill.py -v
```

Expected: FAIL because `scripts/autofill_paycor.py` does not exist yet.

- [ ] **Step 3: Implement the minimal Paycor submitter**

Create `scripts/autofill_paycor.py` using the single-page pipeline shape. Include special handling for:

- legacy form ids / action URLs like `JobIntroduction.action`
- required profile/contact fields
- select/radio groups
- explicit validation-error detection

Core structure:

```python
_BOARD = "paycor"
SUBMIT_BUTTON_NAMES = ("Submit Application", "Submit application", "Apply", "Continue")
```

- [ ] **Step 4: Run the Paycor slice and verify it passes**

Run:

```bash
uv run python -m pytest tests/test_paycor_autofill.py -v
```

Expected: PASS

- [ ] **Step 5: Commit Paycor support**

```bash
command git add scripts/autofill_paycor.py tests/test_paycor_autofill.py
command git commit -m "feat(paycor): add draft autofill support"
```

---

### Task 8: Update docs and rerun the stopped unsupported jobs

**Files:**
- Modify: `docs/board-architecture.md`
- Modify: `docs/autofill-patterns.md`
- Modify: `docs/solutions/workflow-issues/stopped-job-audits-must-be-repo-local-and-artifact-backed-2026-03-27.md`
- Modify: `/Users/jerrison/Documents/Documents - J MacBook Pro 16/jerrison-personal/job-applications/2026-04-03 stopped-job audit.md`
- Modify: `jobs.db`
- Modify: affected `output/**/submit/*` artifacts for rerun jobs

- [ ] **Step 1: Write the failing documentation assertions**

Add or update small tests that lock the supported-board set and docs references. For example in `tests/test_submit_application.py`:

```python
def test_script_for_board_supports_wave1_families():
    for board in ("successfactors", "breezy", "recruitee", "jobvite", "jazzhr", "paycor"):
        assert _script_for_board(board).name == f"autofill_{board}.py"
```

- [ ] **Step 2: Run the support matrix tests**

Run:

```bash
uv run python -m pytest tests/test_submit_application.py tests/test_url_resolver.py -v
```

Expected: PASS

- [ ] **Step 3: Update docs with the new families and rollout notes**

Update `docs/board-architecture.md` supported-board list and unsupported-board note. Add board-family gotchas to `docs/autofill-patterns.md`, for example:

```markdown
- **SuccessFactors / Jobs2Web wrapper routing:** company-hosted `jobs.*` pages may be SuccessFactors-backed via `jobs2web` or `j2w.apply` markers even when the hostname is not `successfactors.com`.
- **Recruitee wrappers:** company-hosted `/o/...` pages should route to `recruitee` when the HTML exposes Recruitee assets or branding.
- **JazzHR / ApplyToJob:** `applytojob.com` pages are JazzHR-backed and should route to the `jazzhr` submitter.
```

- [ ] **Step 4: Redraft the stopped jobs in the new families and capture proof**

For each stopped job with `failure_type='unsupported'` and a new supported family:

1. requeue it through the worker or a targeted retry path
2. rerun in `--draft`
3. verify the fresh screenshot artifact exists
4. classify the real outcome

Example targeted rerun commands:

```bash
uv run python scripts/submit_application.py output/super-micro-computer/<role-dir> --headless --draft --provider openai
uv run python scripts/submit_application.py output/zero-hash/<role-dir> --headless --draft --provider openai
uv run python scripts/submit_application.py output/distribusion-technologies-gmbh/<role-dir> --headless --draft --provider openai
uv run python scripts/submit_application.py output/garten/<role-dir> --headless --draft --provider openai
uv run python scripts/submit_application.py output/bitpay/<role-dir> --headless --draft --provider openai
uv run python scripts/submit_application.py output/fortress-information-security/<role-dir> --headless --draft --provider openai
```

Use Playwright evidence capture where the web UI presentation itself needs proof:

```bash
uv run python - <<'PY'
from pathlib import Path
from playwright.sync_api import sync_playwright

target = Path("output/playwright/unsupported-ats-wave1-proof.png")
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 1600})
    page.goto("http://127.0.0.1:8420/#queue", wait_until="domcontentloaded")
    page.screenshot(path=str(target), full_page=True)
    browser.close()
PY
```

- [ ] **Step 5: Run the full verification slice and commit the rollout**

Run:

```bash
uv run python -m pytest \
  tests/test_submit_application.py \
  tests/test_url_resolver.py \
  tests/test_successfactors_autofill.py \
  tests/test_breezy_autofill.py \
  tests/test_recruitee_autofill.py \
  tests/test_jobvite_autofill.py \
  tests/test_jazzhr_autofill.py \
  tests/test_paycor_autofill.py -v
uv run ruff check scripts/ tests/
uv run python scripts/check_architecture.py
uv run python scripts/sync_agent_files.py --check
uv run python scripts/check_agent_docs.py
```

Expected: all commands succeed.

Then commit:

```bash
command git add \
  scripts/autofill_successfactors.py \
  scripts/autofill_breezy.py \
  scripts/autofill_recruitee.py \
  scripts/autofill_jobvite.py \
  scripts/autofill_jazzhr.py \
  scripts/autofill_paycor.py \
  scripts/job_board_urls.py \
  scripts/submit_application.py \
  scripts/url_resolver.py \
  scripts/job_worker.py \
  tests/test_submit_application.py \
  tests/test_url_resolver.py \
  tests/test_successfactors_autofill.py \
  tests/test_breezy_autofill.py \
  tests/test_recruitee_autofill.py \
  tests/test_jobvite_autofill.py \
  tests/test_jazzhr_autofill.py \
  tests/test_paycor_autofill.py \
  docs/board-architecture.md \
  docs/autofill-patterns.md \
  docs/solutions/workflow-issues/stopped-job-audits-must-be-repo-local-and-artifact-backed-2026-03-27.md \
  "docs/superpowers/plans/2026-04-03-unsupported-ats-wave1.md"
command git commit -m "feat(boards): add wave 1 unsupported ATS support"
```

---

## Self-Review

### Spec coverage

- Wave-1 families covered: yes, via Tasks 1-7.
- Wrapper detection covered: yes, in Task 1.
- Draft-only fail-closed behavior covered: yes, in Tasks 2-7 and rerun verification in Task 8.
- Real stopped-job redrafts and screenshot evidence covered: yes, in Task 8.
- Repo and Obsidian tracking covered: yes, in Task 8.
- Apple/Google left for later wave: yes, intentionally out of scope.

### Placeholder scan

- No `TODO` / `TBD` placeholders remain.
- Every task includes explicit files, commands, and concrete code snippets.

### Type and naming consistency

- Board names are consistently: `successfactors`, `breezy`, `recruitee`, `jobvite`, `jazzhr`, `paycor`.
- New script names and test names match those board names exactly.
