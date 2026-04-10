# Browser-Heavy Risk Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add focused regression coverage for shared browser-runtime and draft-proof state paths, fixing any defects those tests expose.

**Architecture:** Start with a guaranteed red-green cycle in `scripts/browser_runtime.py` around the interactive Google re-auth polling loop, because that code is shared across headed browser runs and currently has unguarded page-content reads. After that, extend direct tests for browser-runtime helpers and draft-proof state transitions without broadening scope into board-specific submit orchestration.

**Tech Stack:** Python 3.12+, pytest, unittest.mock, pathlib/tempfile, importlib, fake page/browser test doubles

---

## File Structure

- `tests/test_browser_runtime.py`: direct unit-style coverage for shared browser helpers, browser launch behavior, Google session recovery, AppleScript wrappers, and screen-origin detection.
- `scripts/browser_runtime.py`: production helper implementation; only change this file if the new browser-runtime tests expose defects.
- `tests/test_pipeline_draft_proof.py`: direct state-machine tests for `ready`, `blocked`, `stale`, `legacy`, and `unavailable` draft review outcomes.
- `scripts/pipeline_draft_proof.py`: production draft-proof logic; only change this file if the new direct state tests expose a real classification bug.

### Task 1: Fix The Browser Re-Auth Polling Crash

**Files:**
- Modify: `tests/test_browser_runtime.py`
- Modify: `scripts/browser_runtime.py`

- [ ] **Step 1: Write the failing test**

```python
    def test_ensure_google_session_continues_when_page_content_probe_raises(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        class FakePage:
            def __init__(self):
                self.url = "https://accounts.google.com"
                self.goto_calls = []
                self.wait_calls = []
                self.inner_text_calls = 0

            def goto(self, url, wait_until=None, timeout=None):
                self.goto_calls.append((url, wait_until, timeout))

            def inner_text(self, selector, timeout=None):
                self.inner_text_calls += 1
                return "[]" if self.inner_text_calls == 1 else ""

            def wait_for_timeout(self, timeout_ms):
                self.wait_calls.append(timeout_ms)
                self.url = "https://myaccount.google.com/"

            def content(self):
                raise RuntimeError("content unavailable")

        page = FakePage()

        with mock.patch.object(browser_runtime.sys, "platform", "linux"):
            browser_runtime.ensure_google_session(page, headless=False)

        assert [call[0] for call in page.goto_calls] == [
            "https://accounts.google.com/ListAccounts?gpsia=1&source=ChromiumBrowser",
            "https://accounts.google.com",
        ]
        assert page.wait_calls == [2000]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_browser_runtime.py -k page_content_probe_raises -v`

Expected: FAIL with `RuntimeError: content unavailable`, proving the interactive re-auth loop crashes if `page.content()` throws while checking for restored sign-in state.

- [ ] **Step 3: Write minimal implementation**

```python
        try:
            page_content = page.content() or ""
        except Exception:
            page_content = ""
        if "SignOutOptions" in body or "data-email" in page_content:
            break
```

Apply this inside `ensure_google_session(...)` so the helper keeps polling instead of crashing when Playwright cannot read full page HTML during an intermediate auth state.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_browser_runtime.py -k page_content_probe_raises -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_browser_runtime.py scripts/browser_runtime.py
git commit -m "test: harden browser reauth polling"
```

### Task 2: Expand Shared Browser Runtime Coverage

**Files:**
- Modify: `tests/test_browser_runtime.py`
- Modify: `scripts/browser_runtime.py`

- [ ] **Step 1: Write the next focused tests**

```python
    def test_ensure_google_session_reports_active_signed_in_session(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        class FakePage:
            url = "about:blank"

            def goto(self, url, wait_until=None, timeout=None):
                self.last_goto = (url, wait_until, timeout)

            def inner_text(self, selector, timeout=None):
                return '[["gaia.l.a","user@example.com","User Example",0,0,0,0,1,null,"avatar"]]'

        page = FakePage()
        with mock.patch("builtins.print") as print_mock:
            browser_runtime.ensure_google_session(page, headless=False)

        print_mock.assert_any_call("Google session: active (signed in)")

    def test_ensure_google_session_headless_warns_without_interactive_reauth(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        class FakePage:
            def __init__(self):
                self.goto_calls = []

            def goto(self, url, wait_until=None, timeout=None):
                self.goto_calls.append(url)

            def inner_text(self, selector, timeout=None):
                return "[]"

        page = FakePage()
        with mock.patch("builtins.print") as print_mock:
            browser_runtime.ensure_google_session(page, headless=True)

        assert page.goto_calls == [
            "https://accounts.google.com/ListAccounts?gpsia=1&source=ChromiumBrowser"
        ]
        print_mock.assert_any_call("WARNING: Google session expired (headless — cannot re-authenticate).")

    def test_run_osascript_returns_false_when_subprocess_raises(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        with (
            mock.patch.object(browser_runtime.sys, "platform", "darwin"),
            mock.patch("subprocess.run", side_effect=OSError("osascript missing")),
        ):
            assert browser_runtime._run_osascript('display notification "x"') is False

    def test_focus_chromium_window_without_title_uses_first_window_script(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        with mock.patch.object(browser_runtime, "_run_osascript", return_value=True) as run_osascript:
            browser_runtime.focus_chromium_window(title_substring="")

        script = run_osascript.call_args.args[0]
        assert "set targetWindow to first window" in script
        assert "whose name contains" not in script

    def test_detect_webapp_screen_origin_matches_window_to_screen(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        responses = [
            mock.Mock(stdout="1440,100\n"),
            mock.Mock(stdout='[{"x":0,"y":0,"w":1440,"h":900},{"x":1440,"y":0,"w":1440,"h":900}]'),
        ]

        with (
            mock.patch.object(browser_runtime.sys, "platform", "darwin"),
            mock.patch("subprocess.run", side_effect=responses),
        ):
            assert browser_runtime._detect_webapp_screen_origin() == (1440, 0)
```

- [ ] **Step 2: Run tests to verify current behavior**

Run: `uv run python -m pytest tests/test_browser_runtime.py -k "google_session or run_osascript or focus_chromium_window or detect_webapp_screen_origin" -v`

Expected: PASS after Task 1. If any test fails, treat it as a real shared-runtime defect and fix only the specific failing branch in `scripts/browser_runtime.py`.

- [ ] **Step 3: Write minimal implementation if a new branch fails**

```python
if sys.platform != "darwin":
    return None
```

or

```python
target_window = "first window"
if title_substring:
    target_window = f'first window whose name contains {json.dumps(title_substring)}'
```

or

```python
try:
    screens = _json.loads(scr_result.stdout.strip())
except Exception:
    return None
```

Use only the branch-local patch required by the failing test. Do not refactor unrelated browser-launch code.

- [ ] **Step 4: Run the targeted browser-runtime file**

Run: `uv run python -m pytest tests/test_browser_runtime.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_browser_runtime.py scripts/browser_runtime.py
git commit -m "test: expand shared browser runtime coverage"
```

### Task 3: Add Direct Draft-Proof State Tests

**Files:**
- Modify: `tests/test_pipeline_draft_proof.py`
- Modify: `scripts/pipeline_draft_proof.py`

- [ ] **Step 1: Write the direct state-machine tests**

```python
def test_draft_review_state_marks_legacy_when_only_legacy_artifacts_exist(tmp_path, monkeypatch):
    proof = load_module("pipeline_draft_proof_legacy", "scripts/pipeline_draft_proof.py")
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()

    monkeypatch.setitem(
        sys.modules,
        "application_submit_common",
        type(
            "SubmitCommon",
            (),
            {
                "load_pending_user_input_for_submit_attempt": staticmethod(lambda _output_dir: None),
            },
        )(),
    )
    monkeypatch.setattr(
        proof,
        "_draft_proof_blocker_entries",
        lambda *_args, **_kwargs: (
            {
                "submit_dirname": "submit",
                "submit_dir": out_dir / "submit",
                "board_name": "greenhouse",
                "artifact_sources": {"report_json": "legacy_default"},
                "report_json": out_dir / "submit" / "greenhouse_autofill_report.json",
            },
            [],
        ),
    )
    monkeypatch.setattr(proof, "_optional_review_note_count", lambda _path: 0)
    monkeypatch.setattr(proof, "_historical_proof_dirs", lambda *_args, **_kwargs: [])

    state = proof.draft_review_state(out_dir, board_name="greenhouse")

    assert state["state"] == "legacy"

def test_draft_review_state_marks_stale_when_blocked_current_attempt_has_historical_proof(tmp_path, monkeypatch):
    proof = load_module("pipeline_draft_proof_stale", "scripts/pipeline_draft_proof.py")
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()

    monkeypatch.setitem(
        sys.modules,
        "application_submit_common",
        type(
            "SubmitCommon",
            (),
            {
                "load_pending_user_input_for_submit_attempt": staticmethod(lambda _output_dir: None),
            },
        )(),
    )
    monkeypatch.setattr(
        proof,
        "_draft_proof_blocker_entries",
        lambda *_args, **_kwargs: (
            {
                "submit_dirname": "submit",
                "submit_dir": out_dir / "submit",
                "board_name": "greenhouse",
                "artifact_sources": {"report_json": "active_submit"},
            },
            [{"field_name": "pre_submit_screenshot", "reason": "Missing current screenshot proof."}],
        ),
    )
    monkeypatch.setattr(proof, "_optional_review_note_count", lambda _path: 0)
    monkeypatch.setattr(proof, "_historical_proof_dirs", lambda *_args, **_kwargs: ["submit-20260401T010101Z"])

    state = proof.draft_review_state(out_dir, board_name="greenhouse")

    assert state["state"] == "stale"
    assert "historical proof exists" in state["reason"].casefold()
```

```python
def test_draft_review_state_marks_ready_when_required_proof_exists(tmp_path, monkeypatch):
    proof = load_module("pipeline_draft_proof_ready", "scripts/pipeline_draft_proof.py")
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()

    monkeypatch.setitem(
        sys.modules,
        "application_submit_common",
        type(
            "SubmitCommon",
            (),
            {
                "load_pending_user_input_for_submit_attempt": staticmethod(lambda _output_dir: None),
            },
        )(),
    )
    monkeypatch.setattr(
        proof,
        "_draft_proof_blocker_entries",
        lambda *_args, **_kwargs: (
            {
                "submit_dirname": "submit",
                "submit_dir": out_dir / "submit",
                "board_name": "greenhouse",
                "artifact_sources": {"report_json": "active_submit", "pre_submit_screenshot": "active_submit"},
                "report_json": out_dir / "submit" / "greenhouse_autofill_report.json",
            },
            [],
        ),
    )
    monkeypatch.setattr(proof, "_optional_review_note_count", lambda _path: 0)
    monkeypatch.setattr(proof, "_historical_proof_dirs", lambda *_args, **_kwargs: [])

    state = proof.draft_review_state(out_dir, board_name="greenhouse")

    assert state["state"] == "ready"
```

- [ ] **Step 2: Run tests to verify current behavior**

Run: `uv run python -m pytest tests/test_pipeline_draft_proof.py -v`

Expected: PASS if the direct state logic already matches the existing indirect coverage. If a new test fails, fix only the specific state-classification branch in `scripts/pipeline_draft_proof.py`.

- [ ] **Step 3: Write minimal implementation if classification is wrong**

```python
if historical_dirs:
    return {
        "state": "stale",
        "reason": (
            "Historical proof exists, but the active submit attempt is missing required proof or still has blockers."
            + (f" {first_reason}" if first_reason else "")
        ).strip(),
        "submit_dirname": proof.get("submit_dirname"),
        "historical_submit_dirs": historical_dirs,
        "optional_review_note_count": optional_review_note_count,
    }
```

or

```python
if legacy_active:
    return {
        "state": "legacy",
        "reason": "This draft only has legacy submit artifacts and does not satisfy the current draft-proof contract.",
        "submit_dirname": proof.get("submit_dirname"),
        "historical_submit_dirs": historical_dirs,
        "optional_review_note_count": optional_review_note_count,
    }
```

Keep the fix local to the failing condition instead of restructuring the entire state function.

- [ ] **Step 4: Run the targeted draft-proof file**

Run: `uv run python -m pytest tests/test_pipeline_draft_proof.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_pipeline_draft_proof.py scripts/pipeline_draft_proof.py
git commit -m "test: add direct draft proof state coverage"
```

### Task 4: Full Verification

**Files:**
- Modify: `tests/test_browser_runtime.py`
- Modify: `tests/test_pipeline_draft_proof.py`
- Modify: `scripts/browser_runtime.py`
- Modify: `scripts/pipeline_draft_proof.py`

- [ ] **Step 1: Run targeted tests**

```bash
uv run python -m pytest tests/test_browser_runtime.py tests/test_pipeline_draft_proof.py -v
```

- [ ] **Step 2: Run repo verification**

```bash
uv run ruff check scripts/ tests/
uv run python scripts/check_architecture.py
uv run python scripts/check_agent_docs.py
uv run python scripts/sync_agent_files.py --check
uv run python -m pytest tests/ -v
```

- [ ] **Step 3: Confirm worktree state**

```bash
git status --short
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_browser_runtime.py tests/test_pipeline_draft_proof.py scripts/browser_runtime.py scripts/pipeline_draft_proof.py
git commit -m "test: harden browser runtime and draft proof coverage"
```
