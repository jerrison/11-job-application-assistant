# Autofill Script Consolidation Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract ~60-70% duplicated code from 4 autofill scripts (Gem, Lever, Ashby, Dover) into two shared modules (`autofill_common.py`, `autofill_pipeline.py`), preserving exact functionality.

**Architecture:** Composition-based — shared utility functions in `autofill_common.py`, opt-in browser orchestration pipeline in `autofill_pipeline.py`. Board scripts become thin wrappers containing only board-specific logic. See `docs/superpowers/specs/2026-03-13-autofill-script-consolidation-design.md` for full design.

**Tech Stack:** Python 3, Playwright (browser automation), unittest (testing)

---

## Chunk 1: Create `autofill_common.py` — Shared Utilities

### Task 1: Create `autofill_common.py` with `board_file_constants` and `label_matches`

**Files:**
- Create: `scripts/autofill_common.py`
- Create: `tests/test_autofill_common.py`

- [ ] **Step 1: Write failing tests for `board_file_constants`**

```python
# tests/test_autofill_common.py
import importlib.util
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BoardFileConstantsTests(unittest.TestCase):
    def test_generates_artifact_filenames_for_gem(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        constants = common.board_file_constants("gem")
        self.assertEqual(constants["report_md"], "gem_autofill_report.md")
        self.assertEqual(constants["report_json"], "gem_autofill_report.json")
        self.assertEqual(constants["pre_submit_screenshot"], "gem_autofill_pre_submit.png")
        self.assertEqual(constants["page_screenshots_dir"], "gem_autofill_pages")
        self.assertEqual(constants["unknown_questions_json"], "gem_unknown_questions.json")
        self.assertEqual(constants["submit_debug_html"], "gem_submit_debug.html")
        self.assertEqual(constants["submit_debug_screenshot"], "gem_submit_debug.png")
        self.assertEqual(constants["payload_json"], "gem_autofill_payload.json")
        self.assertEqual(constants["application_page_html"], "gem_application_page.html")

    def test_generates_artifact_filenames_for_ashby(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        constants = common.board_file_constants("ashby")
        self.assertEqual(constants["report_md"], "ashby_autofill_report.md")
        self.assertEqual(constants["payload_json"], "ashby_autofill_payload.json")


class LabelMatchesTests(unittest.TestCase):
    def test_substring_match_with_string(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        self.assertTrue(common.label_matches("First Name", "first name"))
        self.assertFalse(common.label_matches("First Name", "last name"))

    def test_substring_match_with_dict(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        field = {"label": "LinkedIn Profile URL"}
        self.assertTrue(common.label_matches(field, "linkedin"))
        self.assertFalse(common.label_matches(field, "github"))

    def test_multiple_fragments_any_match(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        self.assertTrue(common.label_matches("Email Address", "email", "phone"))
        self.assertFalse(common.label_matches("Full Name", "email", "phone"))

    def test_word_boundary_mode(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        field = {"label": "I identify my ethnicity as: Select all that apply"}
        self.assertTrue(common.label_matches(field, "ethnicity", word_boundary=True))
        self.assertFalse(common.label_matches(field, "city", word_boundary=True))

    def test_word_boundary_uses_alphanumeric_lookaround_not_backslash_b(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        # \b treats underscore as word char; our pattern should NOT
        self.assertTrue(common.label_matches("some_city value", "city", word_boundary=True))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_autofill_common.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement `board_file_constants` and `label_matches`**

```python
# scripts/autofill_common.py
"""Shared utilities for board-specific autofill scripts."""

from __future__ import annotations

import re

from application_submit_common import normalize_text


def board_file_constants(board_name: str) -> dict[str, str]:
    """Generate standard artifact filenames for a board."""
    return {
        "report_md": f"{board_name}_autofill_report.md",
        "report_json": f"{board_name}_autofill_report.json",
        "pre_submit_screenshot": f"{board_name}_autofill_pre_submit.png",
        "page_screenshots_dir": f"{board_name}_autofill_pages",
        "unknown_questions_json": f"{board_name}_unknown_questions.json",
        "submit_debug_html": f"{board_name}_submit_debug.html",
        "submit_debug_screenshot": f"{board_name}_submit_debug.png",
        "payload_json": f"{board_name}_autofill_payload.json",
        "application_page_html": f"{board_name}_application_page.html",
    }


def label_matches(
    text_or_field: str | dict,
    *fragments: str,
    word_boundary: bool = False,
) -> bool:
    """Check if text or field label matches any of the given fragments.

    Args:
        text_or_field: Raw text string or a dict with a "label" key.
        *fragments: One or more substrings to match against.
        word_boundary: If True, use alphanumeric-boundary regex matching
            (replicates Lever's ``(?<![a-z0-9])...(?![a-z0-9])`` pattern).
            If False, use simple substring containment.
    """
    if isinstance(text_or_field, dict):
        text = text_or_field.get("label", "")
    else:
        text = text_or_field
    normalized = normalize_text(text)
    if word_boundary:
        return any(
            re.search(
                rf"(?<![a-z0-9]){re.escape(normalize_text(f))}(?![a-z0-9])",
                normalized,
            )
            for f in fragments
        )
    return any(normalize_text(f) in normalized for f in fragments)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_autofill_common.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/autofill_common.py tests/test_autofill_common.py
git commit -m "feat: create autofill_common.py with board_file_constants and label_matches"
```

### Task 2: Add `select_option` to `autofill_common.py`

**Files:**
- Modify: `scripts/autofill_common.py`
- Modify: `tests/test_autofill_common.py`

- [ ] **Step 1: Write failing tests for `select_option`**

Append to `tests/test_autofill_common.py`:

```python
class SelectOptionTests(unittest.TestCase):
    def test_exact_match(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        self.assertEqual(common.select_option(["Yes", "No"], "Yes"), "Yes")

    def test_case_insensitive_match(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        self.assertEqual(common.select_option(["Male", "Female", "Non-binary"], "male"), "Male")

    def test_substring_match(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        result = common.select_option(
            ["Yes - I require sponsorship", "No - I do not require sponsorship"],
            "No",
        )
        self.assertIn("No", result)

    def test_no_match_returns_none(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        self.assertIsNone(common.select_option(["Yes", "No"], "Maybe"))

    def test_none_options_returns_none(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        self.assertIsNone(common.select_option(None, "Yes"))

    def test_none_answer_returns_none(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        self.assertIsNone(common.select_option(["Yes", "No"], None))

    def test_filter_select_prefix(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        result = common.select_option(
            ["Select an option", "Yes", "No"],
            "Yes",
            filter_select_prefix=True,
        )
        self.assertEqual(result, "Yes")
```

Reference existing Dover implementation at `scripts/autofill_dover.py:108-131` and Lever at `scripts/autofill_lever.py:171-192` for exact matching logic.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_autofill_common.py::SelectOptionTests -v`
Expected: FAIL

- [ ] **Step 3: Implement `select_option`**

Add to `scripts/autofill_common.py`:

```python
def select_option(
    options: list[str] | None,
    answer: str | None,
    *,
    filter_select_prefix: bool = False,
) -> str | None:
    """Fuzzy-match an answer to a list of option strings.

    Args:
        options: Available options to match against.
        answer: The desired answer text.
        filter_select_prefix: If True, exclude options starting with "select"
            (e.g. "Select an option") before matching. Used by Lever.
    """
    normalized_answer = normalize_text(answer)
    if not normalized_answer:
        return None

    raw_options = options or []
    if filter_select_prefix:
        raw_options = [o for o in raw_options if not normalize_text(o).startswith("select")]

    normalized_options = [
        (option, normalize_text(option))
        for option in raw_options
        if normalize_text(option)
    ]
    # Exact match
    for option, normalized_option in normalized_options:
        if normalized_option == normalized_answer:
            return option
    # Substring containment (either direction)
    for option, normalized_option in normalized_options:
        if normalized_answer in normalized_option or normalized_option in normalized_answer:
            return option
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_autofill_common.py::SelectOptionTests -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/autofill_common.py tests/test_autofill_common.py
git commit -m "feat: add select_option to autofill_common.py"
```

### Task 3: Add `write_report` to `autofill_common.py`

**Files:**
- Modify: `scripts/autofill_common.py`
- Modify: `tests/test_autofill_common.py`

- [ ] **Step 1: Write failing tests for `write_report`**

Append to `tests/test_autofill_common.py`:

```python
import json
import tempfile


class WriteReportTests(unittest.TestCase):
    def test_splits_filled_and_planned_entries(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com/job",
                "application_url": "https://example.com/apply",
                "artifacts": {
                    "report_json": str(out_dir / "report.json"),
                    "report_markdown": str(out_dir / "report.md"),
                    "pre_submit_screenshot": str(out_dir / "pre_submit.png"),
                },
                "unknown_questions": [],
                "steps": [],
            }
            runtime = {
                "steps": [
                    {
                        "field_name": "email",
                        "label": "Email",
                        "kind": "text",
                        "value": "test@test.com",
                        "source": "master_resume.md",
                        "required": True,
                        "filled": True,
                    },
                    {
                        "field_name": "cover_letter",
                        "label": "Cover Letter",
                        "kind": "file",
                        "file_path": "/tmp/cover.pdf",
                        "source": "existing_cover_letter_asset",
                        "required": False,
                    },
                ],
            }
            report = common.write_report(payload, board_name="gem", runtime=runtime)
            saved = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))

        self.assertEqual(len(saved["fields"]), 1)
        self.assertEqual(saved["fields"][0]["field_name"], "email")
        self.assertEqual(saved["planned_but_unconfirmed_fields"][0]["field_name"], "cover_letter")

    def test_markdown_header_uses_board_name(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com/job",
                "application_url": "https://example.com/apply",
                "artifacts": {
                    "report_json": str(out_dir / "report.json"),
                    "report_markdown": str(out_dir / "report.md"),
                    "pre_submit_screenshot": str(out_dir / "pre_submit.png"),
                },
                "unknown_questions": [],
                "steps": [],
            }
            common.write_report(payload, board_name="lever")
            md = (out_dir / "report.md").read_text(encoding="utf-8")

        self.assertTrue(md.startswith("# Lever Autofill Report"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_autofill_common.py::WriteReportTests -v`
Expected: FAIL

- [ ] **Step 3: Implement `write_report`**

Add to `scripts/autofill_common.py`:

```python
def _report_entry(step: dict) -> dict:
    """Build a report entry dict from a step dict."""
    entry = {
        "field_name": step.get("field_name", ""),
        "label": step.get("label", ""),
        "kind": step.get("kind", ""),
        "required": bool(step.get("required")),
        "source": step.get("source", ""),
    }
    if step.get("filled"):
        entry["status"] = "filled"
        entry["value"] = step.get("value", step.get("file_path", ""))
    else:
        entry["status"] = "planned"
        entry["value"] = step.get("value", step.get("file_path", ""))
    return entry


def write_report(
    payload: dict,
    *,
    board_name: str,
    runtime: dict | None = None,
) -> dict:
    """Write JSON and Markdown autofill reports. Returns the report payload dict."""
    steps = list(runtime.get("steps", payload["steps"]) if runtime else payload["steps"])
    report_entries = [_report_entry(step) for step in steps]
    filled_entries = [e for e in report_entries if e["status"] == "filled"]
    planned_entries = [e for e in report_entries if e["status"] != "filled"]
    artifacts = payload["artifacts"]

    report_payload = {
        "job_title": payload["job_title"],
        "company": payload["company"],
        "job_url": payload["job_url"],
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "pre_submit_screenshot": artifacts["pre_submit_screenshot"],
        "fields": filled_entries,
        "unknown_questions": payload.get("unknown_questions", []),
    }
    if "application_url" in payload:
        report_payload["application_url"] = payload["application_url"]
    if planned_entries:
        report_payload["planned_but_unconfirmed_fields"] = planned_entries
    Path(artifacts["report_json"]).write_text(
        json_dumps_pretty(report_payload) + "\n", encoding="utf-8"
    )

    board_title = board_name.capitalize()
    lines = [
        f"# {board_title} Autofill Report",
        "",
        f"- Company: {payload['company']}",
        f"- Job Title: {payload['job_title']}",
        f"- Job URL: {payload['job_url']}",
    ]
    if "application_url" in payload:
        lines.append(f"- Application URL: {payload['application_url']}")
    lines.extend([
        f"- Generated At (UTC): {report_payload['generated_at_utc']}",
        f"- Pre-Submit Screenshot: {artifacts['pre_submit_screenshot']}",
        "",
        "## Filled Fields",
        "",
    ])
    for index, entry in enumerate(filled_entries, start=1):
        lines.extend([
            f"### {index}. {entry['label']} (`{entry['field_name']}`)",
            f"Source: `{entry['source']}`",
            f"Kind: `{entry['kind']}`",
            f"Required: `{'yes' if entry['required'] else 'no'}`",
            f"Status: `{entry['status']}`",
            "```text",
            str(entry["value"]),
            "```",
            "",
        ])
    if planned_entries:
        lines.extend(["## Planned But Unconfirmed", ""])
        for entry in planned_entries:
            lines.extend([
                f"- {entry['label']} (`{entry['field_name']}`) from `{entry['source']}`",
                f"  - Kind: `{entry['kind']}`",
                f"  - Required: `{'yes' if entry['required'] else 'no'}`",
                f"  - Status: `{entry['status']}`",
            ])
    if payload.get("unknown_questions"):
        lines.extend(["## Unresolved Questions", ""])
        for question in payload["unknown_questions"]:
            lines.append(f"- {question['label']} (`{question['field_name']}`)")
        lines.append("")
    Path(artifacts["report_markdown"]).write_text("\n".join(lines), encoding="utf-8")
    return report_payload
```

Requires adding imports at the top of `autofill_common.py`: `from datetime import datetime, timezone` and `from application_submit_common import json_dumps_pretty`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_autofill_common.py::WriteReportTests -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/autofill_common.py tests/test_autofill_common.py
git commit -m "feat: add write_report to autofill_common.py"
```

### Task 4: Add browser utility functions to `autofill_common.py`

**Files:**
- Modify: `scripts/autofill_common.py`
- Modify: `tests/test_autofill_common.py`

- [ ] **Step 1: Write failing tests for `capture_full_page`, `click_submit_button`, `page_snapshot`, and `write_submit_debug_artifacts`**

These functions interact with Playwright `page` objects. Tests use mock objects similar to `tests/test_lever_autofill.py:369-407`. Append to `tests/test_autofill_common.py`:

```python
class CaptureFullPageTests(unittest.TestCase):
    def test_uses_preferred_selector_when_found(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        screenshots = []

        class FakeLocator:
            def count(self):
                return 1

            def screenshot(self, path):
                screenshots.append(path)

        class FakeLocatorResult:
            first = FakeLocator()

        class FakePage:
            def locator(self, selector):
                return FakeLocatorResult()

            def screenshot(self, path, full_page=False):
                screenshots.append(("full_page", path))

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sub" / "screenshot.png"
            common.capture_full_page(FakePage(), path, preferred_selectors=("#my-form",))

        self.assertEqual(len(screenshots), 1)
        self.assertEqual(screenshots[0], str(path))

    def test_falls_back_to_full_page(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        screenshots = []

        class FakeLocator:
            def count(self):
                return 0

        class FakeLocatorResult:
            first = FakeLocator()

        class FakePage:
            def locator(self, selector):
                return FakeLocatorResult()

            def screenshot(self, path, full_page=False):
                screenshots.append(("full_page", path, full_page))

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "screenshot.png"
            common.capture_full_page(FakePage(), path)

        self.assertEqual(len(screenshots), 1)
        self.assertTrue(screenshots[0][2])  # full_page=True


class ClickSubmitButtonTests(unittest.TestCase):
    def test_clicks_first_visible_enabled_button(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        clicked = []

        class FakeButton:
            def is_visible(self):
                return True

            def is_enabled(self):
                return True

            def click(self, **kwargs):
                clicked.append(True)

        class FakeLocator:
            def count(self):
                return 1

            def nth(self, index):
                return FakeButton()

        class FakePage:
            def get_by_role(self, role, name=None):
                return FakeLocator()

        result = common.click_submit_button(FakePage(), button_names=("Submit",))
        self.assertTrue(result)
        self.assertEqual(len(clicked), 1)

    def test_returns_false_when_no_buttons_found(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        class FakeLocator:
            def count(self):
                return 0

        class FakePage:
            def get_by_role(self, role, name=None):
                return FakeLocator()

        result = common.click_submit_button(FakePage(), button_names=("Submit",))
        self.assertFalse(result)


class PageSnapshotTests(unittest.TestCase):
    def test_calls_evaluate_with_form_selector_and_captcha_type(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        evaluated_js = []

        class FakePage:
            def evaluate(self, js):
                evaluated_js.append(js)
                return {
                    "url": "https://example.com",
                    "page_text": "Hello",
                    "form_visible": True,
                    "hcaptcha_visible": False,
                    "hcaptcha_challenge_active": False,
                    "invalid_fields": [],
                    "errors": [],
                }

        result = common.page_snapshot(FakePage(), form_selector=".form-33", captcha_type="hcaptcha")
        self.assertEqual(len(evaluated_js), 1)
        self.assertIn(".form-33", evaluated_js[0])
        self.assertIn("hcaptcha", evaluated_js[0])
        self.assertEqual(result["url"], "https://example.com")


class WriteSubmitDebugArtifactsTests(unittest.TestCase):
    def test_writes_html_and_screenshot(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        captured = []

        class FakePage:
            def content(self):
                return "<html>debug</html>"

        with tempfile.TemporaryDirectory() as tmpdir:
            payload = {
                "artifacts": {
                    "submit_debug_html": str(Path(tmpdir) / "debug.html"),
                    "submit_debug_screenshot": str(Path(tmpdir) / "debug.png"),
                }
            }
            common.write_submit_debug_artifacts(
                FakePage(), payload,
                capture_fn=lambda page, path: captured.append(str(path)),
            )
            html_content = Path(payload["artifacts"]["submit_debug_html"]).read_text()

        self.assertEqual(html_content, "<html>debug</html>")
        self.assertEqual(len(captured), 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_autofill_common.py::CaptureFullPageTests tests/test_autofill_common.py::ClickSubmitButtonTests -v`
Expected: FAIL

- [ ] **Step 3: Implement browser utility functions**

Add to `scripts/autofill_common.py`:

1. `capture_full_page(page, path, *, preferred_selectors=())` — extracted from `scripts/autofill_ashby.py:854-865`. Logic: try each preferred selector, then fall back to `page.screenshot(full_page=True)`.

2. `click_submit_button(page, *, button_names)` — extracted from `scripts/autofill_ashby.py:1168-1183`. Logic: iterate button names, find by role "button", click first visible+enabled one.

3. `page_snapshot(page, *, form_selector, captcha_type)` — extracted from `scripts/autofill_ashby.py:1106-1146`. Parametrize the JS template to interpolate `form_selector` and `captcha_type` (either "recaptcha" or "hcaptcha"). Always collect the full set of fields: `url`, `page_text`, `form_visible`, `{captcha_type}_visible`, `{captcha_type}_challenge_active`, `invalid_fields`, `errors`.

4. `write_submit_debug_artifacts(page, payload, capture_fn)` — extracted from `scripts/autofill_lever.py:954-959`. Takes a `capture_fn` callback for screenshot capture.

5. `matches_confirm_patterns(page_text, patterns)` — helper for classify functions.

6. `collect_validation_errors(snapshot, validation_patterns)` — helper for classify functions. Returns `(combined_errors, invalid_fields)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_autofill_common.py -v`
Expected: All PASS

- [ ] **Step 5: Run all existing tests to confirm no regressions**

Run: `python -m pytest tests/ -v`
Expected: All existing tests still pass (new module is additive)

- [ ] **Step 6: Commit**

```bash
git add scripts/autofill_common.py tests/test_autofill_common.py
git commit -m "feat: add browser utilities to autofill_common.py"
```

### Task 4a: Add classify helpers and `yes_no_step` to `autofill_common.py`

**Files:**
- Modify: `scripts/autofill_common.py`
- Modify: `tests/test_autofill_common.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_autofill_common.py`:

```python
class MatchesConfirmPatternsTests(unittest.TestCase):
    def test_matches_known_pattern(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        patterns = (re.compile(r"\bthank(?:s| you)\s+for\s+applying\b", re.I),)
        self.assertTrue(common.matches_confirm_patterns("Thanks for applying to Acme", patterns))

    def test_no_match(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        patterns = (re.compile(r"\bthank(?:s| you)\s+for\s+applying\b", re.I),)
        self.assertFalse(common.matches_confirm_patterns("Please fill out all fields", patterns))


class CollectValidationErrorsTests(unittest.TestCase):
    def test_combines_explicit_and_page_level_errors(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        validation_patterns = (re.compile(r"please (?:complete|fill)", re.I),)
        snapshot = {
            "page_text": "Please complete all required fields",
            "errors": ["Email is required"],
            "invalid_fields": ["email"],
        }
        errors, invalid = common.collect_validation_errors(snapshot, validation_patterns)
        self.assertIn("Email is required", errors)
        self.assertIn("Please complete all required fields", errors)
        self.assertEqual(invalid, ["email"])

    def test_empty_when_no_errors(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        snapshot = {"page_text": "Form loaded", "errors": [], "invalid_fields": []}
        errors, invalid = common.collect_validation_errors(snapshot, ())
        self.assertEqual(errors, [])
        self.assertEqual(invalid, [])


class YesNoStepTests(unittest.TestCase):
    def test_builds_yes_step(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        field = {"label": "Authorized?", "field_name": "auth", "kind": "radio", "required": True, "index": 0}
        matcher = lambda candidates: "Yes"
        step = common.yes_no_step(field, value=True, source="profile", option_matcher=matcher)
        self.assertEqual(step["value"], "Yes")
        self.assertEqual(step["source"], "profile")

    def test_returns_none_when_no_option_matches(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        field = {"label": "Authorized?", "field_name": "auth", "kind": "radio", "required": True, "index": 0}
        matcher = lambda candidates: None
        step = common.yes_no_step(field, value=True, source="profile", option_matcher=matcher)
        self.assertIsNone(step)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_autofill_common.py::MatchesConfirmPatternsTests tests/test_autofill_common.py::CollectValidationErrorsTests tests/test_autofill_common.py::YesNoStepTests -v`
Expected: FAIL

- [ ] **Step 3: Implement functions**

Add to `scripts/autofill_common.py`:

```python
def matches_confirm_patterns(page_text: str, patterns) -> bool:
    """Check if page text matches any confirmation pattern."""
    return any(pattern.search(page_text) for pattern in patterns)


def collect_validation_errors(
    snapshot: dict, validation_patterns
) -> tuple[list[str], list[str]]:
    """Extract combined errors and invalid fields from a page snapshot."""
    page_text = str(snapshot.get("page_text") or "")
    explicit_errors = list(snapshot.get("errors") or [])
    page_level_errors = [
        pattern.search(page_text).group(0)
        for pattern in validation_patterns
        if pattern.search(page_text)
    ]
    combined_errors = list(dict.fromkeys(explicit_errors + page_level_errors))
    invalid_fields = list(snapshot.get("invalid_fields") or [])
    return combined_errors, invalid_fields


def yes_no_step(
    field: dict,
    *,
    value: bool,
    source: str,
    option_matcher,
) -> dict | None:
    """Build a step dict for a yes/no question.

    Args:
        field: The form field dict.
        value: True for "Yes", False for "No".
        source: The source identifier (e.g. "application_profile.md").
        option_matcher: Callable that takes a list of candidate strings
            and returns the matched option string, or None.
    """
    yes_candidates = ["Yes", "yes", "YES", "True", "true"]
    no_candidates = ["No", "no", "NO", "False", "false"]
    candidates = yes_candidates if value else no_candidates
    matched = option_matcher(candidates)
    if matched is None:
        return None
    return {
        "field_name": field.get("field_name", ""),
        "label": field.get("label", ""),
        "kind": field.get("kind", "radio"),
        "required": bool(field.get("required")),
        "index": field.get("index", 0),
        "value": matched,
        "source": source,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_autofill_common.py -v`
Expected: All PASS

- [ ] **Step 5: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add scripts/autofill_common.py tests/test_autofill_common.py
git commit -m "feat: add classify helpers and yes_no_step to autofill_common.py"
```

## Chunk 2: Create `autofill_pipeline.py` — Browser Orchestration & `autofill_main`

### Task 5: Create `autofill_pipeline.py` with `autofill_main`

**Files:**
- Create: `scripts/autofill_pipeline.py`
- Create: `tests/test_autofill_pipeline.py`

- [ ] **Step 1: Write failing tests for `autofill_main`**

```python
# tests/test_autofill_pipeline.py
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class AutofillMainTests(unittest.TestCase):
    def test_payload_only_writes_json_and_returns_zero(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")
        common = load_module("autofill_common", "scripts/autofill_common.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com",
                "steps": [],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(out_dir / "submit" / "report.json"),
                    "report_markdown": str(out_dir / "submit" / "report.md"),
                    "pre_submit_screenshot": str(out_dir / "submit" / "pre.png"),
                },
            }

            def fake_build(od, provider):
                return payload

            with mock.patch("sys.argv", ["test", str(out_dir), "--payload-only"]):
                with mock.patch.object(pipeline, "find_output_dir", return_value=out_dir):
                    with mock.patch.object(pipeline, "write_report"):
                        rc = pipeline.autofill_main("gem", fake_build)

        self.assertEqual(rc, 0)

    def test_no_browser_board_skips_playwright(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com",
                "steps": [],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(out_dir / "submit" / "report.json"),
                    "report_markdown": str(out_dir / "submit" / "report.md"),
                    "pre_submit_screenshot": str(out_dir / "submit" / "pre.png"),
                },
            }

            def fake_build(od, provider):
                return payload

            with mock.patch("sys.argv", ["test", str(out_dir)]):
                with mock.patch.object(pipeline, "find_output_dir", return_value=out_dir):
                    with mock.patch.object(pipeline, "write_report"):
                        rc = pipeline.autofill_main("dover", fake_build, has_browser=False)

        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_autofill_pipeline.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `autofill_main`**

```python
# scripts/autofill_pipeline.py
"""Shared autofill orchestration: CLI entry point and browser pipeline."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from application_submit_common import (
    PROJECT_ROOT,
    default_answer_provider,
    find_output_dir,
    json_dumps_pretty,
)
from autofill_common import write_report
from output_layout import role_submit_path
from project_env import load_project_env

load_project_env()


def autofill_main(
    board_name: str,
    build_payload_fn: Callable[[Path, str], dict],
    *,
    has_browser: bool = True,
    run_browser_fn: Callable[[Path, bool, bool], int] | None = None,
) -> int:
    """Shared CLI entry point for all autofill scripts."""
    board_title = board_name.capitalize()
    parser = argparse.ArgumentParser(
        description=f"Autofill a {board_title} application using existing job assets."
    )
    parser.add_argument(
        "target",
        help="Output directory (e.g. output/company/role-slug) or a job URL.",
    )
    parser.add_argument(
        "--payload-only",
        action="store_true",
        help=f"Only generate {board_name}_autofill_payload.json.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Launch the Playwright runtime in headless mode.",
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Submit the application after autofill.",
    )
    parser.add_argument(
        "--browser-provider",
        choices=("local", "steel"),
        default=None,
        help="Browser runtime (default: env or local).",
    )
    parser.add_argument(
        "--provider",
        choices=("gemini", "claude", "codex"),
        default=None,
        help="LLM provider for generated answers.",
    )
    args = parser.parse_args()

    out_dir = find_output_dir(args.target)
    if args.browser_provider:
        os.environ["JOB_ASSETS_BROWSER_PROVIDER"] = args.browser_provider
    if args.provider:
        os.environ["ASSET_LLM_PROVIDER"] = args.provider

    provider = args.provider or default_answer_provider()
    payload = build_payload_fn(out_dir, provider)
    write_report(payload, board_name=board_name)

    from autofill_common import board_file_constants
    constants = board_file_constants(board_name)
    payload_path = role_submit_path(out_dir, constants["payload_json"])
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_text(json_dumps_pretty(payload) + "\n", encoding="utf-8")
    print(f"Wrote {payload_path.relative_to(PROJECT_ROOT)}")

    if args.payload_only:
        return 0

    if not has_browser or run_browser_fn is None:
        return 0

    return run_browser_fn(payload_path, args.headless, args.submit)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_autofill_pipeline.py -v`
Expected: All PASS

- [ ] **Step 5: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add scripts/autofill_pipeline.py tests/test_autofill_pipeline.py
git commit -m "feat: create autofill_pipeline.py with autofill_main"
```

### Task 6: Add `run_browser_pipeline` to `autofill_pipeline.py`

**Files:**
- Modify: `scripts/autofill_pipeline.py`

- [ ] **Step 1: Implement `run_browser_pipeline`**

Add to `scripts/autofill_pipeline.py`. This is the core orchestration function — extracted from `scripts/autofill_gem.py:789-987`, generalized to work for all 3 browser-based boards.

**Spec divergence note:** The spec's signature lists `form_ready_selector: str` as required. This plan adds `form_ready_fn` (alternative to selector for Gem's JS-based wait), `wait_for_captcha_fn`, `confirmed_outcome_from_email_fn`, and `board_name` parameters. These additions are necessary to handle board-specific behaviors discovered during detailed code analysis.

```python
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Callable

from application_submit_common import (
    PROJECT_ROOT,
    build_email_confirmation_watcher,
    json_dumps_pretty,
    sync_notion_after_submit,
    write_pending_user_input_for_unconfirmed_fields,
)
from autofill_common import capture_full_page, write_report
from browser_runtime import (
    launch_chromium_browser,
    reveal_manual_challenge,
    submit_browser_profile_dir,
    submit_slow_mo_ms,
    submit_viewport,
)


def run_browser_pipeline(
    payload_path: Path,
    *,
    headless: bool,
    submit: bool,
    board_name: str,
    form_ready_selector: str | None = None,
    form_ready_fn: Callable | None = None,
    fill_step_fn: Callable,
    page_snapshot_fn: Callable,
    classify_state_fn: Callable,
    click_submit_fn: Callable,
    capture_fn: Callable,
    pre_submit_hook: Callable | None = None,
    post_navigate_hook: Callable | None = None,
    wait_for_captcha_fn: Callable | None = None,
    confirmed_outcome_from_email_fn: Callable | None = None,
) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: Playwright is not installed.", file=sys.stderr)
        return 1

    board_title = board_name.capitalize()
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    out_dir = Path(payload["out_dir"])
    page_screenshots_dir = Path(payload["artifacts"]["page_screenshots_dir"])
    page_screenshots_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        viewport = submit_viewport()
        browser = launch_chromium_browser(
            playwright,
            headless=headless,
            slow_mo=submit_slow_mo_ms(headless),
            channel_env_var="JOB_ASSETS_SUBMIT_BROWSER_CHANNEL",
            executable_env_var="JOB_ASSETS_SUBMIT_BROWSER_EXECUTABLE",
            persistent_profile_dir=submit_browser_profile_dir(),
            prefer_local_browser=True,
            viewport=viewport,
            device_scale_factor=2,
            purpose=f"{board_title} autofill",
        )
        page = browser.new_page(viewport=viewport, device_scale_factor=2)
        try:
            # --- Phase 1: Navigate and wait for form ---
            page.goto(payload["job_url"], wait_until="domcontentloaded", timeout=30000)
            if form_ready_fn is not None:
                form_ready_fn(page)
            elif form_ready_selector is not None:
                page.wait_for_selector(form_ready_selector, timeout=25000)
            page.wait_for_timeout(1000)
            if post_navigate_hook is not None:
                post_navigate_hook(page)

            # --- Phase 2: Fill form fields ---
            steps = [dict(step) for step in payload["steps"]]
            for step in steps:
                fill_step_fn(page, step)
                page.wait_for_timeout(100)

            # --- Phase 3: Capture screenshots and write report ---
            pre_submit_path = Path(payload["artifacts"]["pre_submit_screenshot"])
            capture_fn(page, pre_submit_path)
            page_path = page_screenshots_dir / "page_01.png"
            capture_fn(page, page_path)
            runtime = {"steps": steps, "pages": [{"page_index": 1, "screenshot": str(page_path)}]}
            report_payload = write_report(payload, board_name=board_name, runtime=runtime)

            # --- Phase 4: Check for pending user input ---
            pending_path = write_pending_user_input_for_unconfirmed_fields(
                out_dir,
                board=board_name,
                fields=list(report_payload.get("planned_but_unconfirmed_fields") or []),
                report_json=payload["artifacts"]["report_json"],
                report_markdown=payload["artifacts"]["report_markdown"],
                pre_submit_screenshot=payload["artifacts"]["pre_submit_screenshot"],
            )
            if pending_path is not None:
                print(
                    f"{board_title} autofill left planned fields unconfirmed. "
                    f"See {pending_path.relative_to(PROJECT_ROOT)} before submitting.",
                    file=sys.stderr,
                )
                if submit:
                    return 1

            if not submit:
                print(f"Filled {board_title} application for review: {pre_submit_path.relative_to(PROJECT_ROOT)}")
                if browser.session_viewer_url:
                    print(f"{board_title} Steel session viewer: {browser.session_viewer_url}")
                return 0

            # --- Phase 5: Pre-submit hook (e.g. Lever hCaptcha wait) ---
            if pre_submit_hook is not None:
                pre_submit_hook(page)

            # --- Phase 6: Click submit ---
            if not click_submit_fn(page):
                print(f"{board_title} submit buttons were not available after autofill.", file=sys.stderr)
                return 1
            submit_started_at_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            email_watcher = build_email_confirmation_watcher(payload, min_received_at_utc=submit_started_at_utc)

            # --- Phase 7: Poll for confirmation ---
            last_snapshot = None
            for _ in range(30):
                page.wait_for_timeout(500)
                last_snapshot = page_snapshot_fn(page)
                # Check email confirmation
                email_confirmation = email_watcher.poll()
                if email_confirmation and confirmed_outcome_from_email_fn:
                    outcome = confirmed_outcome_from_email_fn(last_snapshot, email_confirmation)
                    sync_notion_after_submit(payload, outcome, provider=board_name, email_confirmation=email_confirmation, min_received_at_utc=submit_started_at_utc)
                    return 0
                # Check page state
                state = classify_state_fn(last_snapshot)
                if state["status"] == "confirmed":
                    outcome = {"status": "confirmed", "reason": state.get("reason"), "snapshot": last_snapshot}
                    sync_notion_after_submit(payload, outcome, provider=board_name, min_received_at_utc=submit_started_at_utc)
                    return 0
                if state["status"] == "captcha_required" and not headless:
                    reveal_manual_challenge(page)
                    wait_seconds = int(os.environ.get("JOB_ASSETS_CAPTCHA_WAIT_SECONDS", "300"))
                    print(f"{board_title} submit is waiting up to {wait_seconds}s for manual captcha solve...", file=sys.stderr)
                    if browser.session_viewer_url:
                        print(f"Steel session viewer: {browser.session_viewer_url}", file=sys.stderr)
                    if wait_for_captcha_fn is not None:
                        last_snapshot, state = wait_for_captcha_fn(page, timeout_seconds=wait_seconds, email_watcher=email_watcher)
                        if state["status"] == "confirmed":
                            email_conf = state.get("email_confirmation")
                            outcome = {"status": "confirmed", "reason": state.get("reason"), "snapshot": last_snapshot}
                            sync_notion_after_submit(payload, outcome, provider=board_name, email_confirmation=email_conf, min_received_at_utc=submit_started_at_utc)
                            return 0
                        if state["status"] != "captcha_required":
                            break
                if state["status"] in {"captcha_required", "validation_error"}:
                    break

            # --- Phase 8: Final email check and debug artifacts ---
            email_confirmation = email_watcher.poll(force=True)
            if email_confirmation and confirmed_outcome_from_email_fn:
                outcome = confirmed_outcome_from_email_fn(last_snapshot, email_confirmation)
                sync_notion_after_submit(payload, outcome, provider=board_name, email_confirmation=email_confirmation, min_received_at_utc=submit_started_at_utc)
                return 0

            debug_html = Path(payload["artifacts"]["submit_debug_html"])
            debug_png = Path(payload["artifacts"]["submit_debug_screenshot"])
            debug_html.write_text(page.content(), encoding="utf-8")
            capture_fn(page, debug_png)
            snapshot = last_snapshot or page_snapshot_fn(page)
            state = classify_state_fn(snapshot)
            print(
                f"{board_title} submit did not reach a confirmed completion state: {state['status']}. "
                f"See {debug_html.relative_to(PROJECT_ROOT)} and {debug_png.relative_to(PROJECT_ROOT)}.",
                file=sys.stderr,
            )
            return 1
        finally:
            browser.close()
```

- [ ] **Step 2: Write tests for `run_browser_pipeline`**

Add to `tests/test_autofill_pipeline.py`:

```python
class RunBrowserPipelineTests(unittest.TestCase):
    def test_returns_zero_when_submit_false_after_filling(self):
        """Verify the pipeline fills steps, captures screenshot, and returns 0 when not submitting."""
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / "submit" / "gem_autofill_pages").mkdir()
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com",
                "out_dir": str(out_dir),
                "steps": [{"field_name": "email", "kind": "text", "value": "test@test.com", "label": "Email", "source": "resume", "required": True}],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(out_dir / "submit" / "report.json"),
                    "report_markdown": str(out_dir / "submit" / "report.md"),
                    "pre_submit_screenshot": str(out_dir / "submit" / "pre.png"),
                    "page_screenshots_dir": str(out_dir / "submit" / "gem_autofill_pages"),
                },
            }
            payload_path = out_dir / "submit" / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            fill_calls = []

            class FakePage:
                def goto(self, url, **kw): pass
                def wait_for_selector(self, sel, **kw): pass
                def wait_for_timeout(self, ms): pass
                def screenshot(self, path, full_page=False): pass
                def locator(self, sel):
                    class L:
                        first = self
                        def count(self): return 0
                    return L()

            class FakeBrowser:
                session_viewer_url = None
                def new_page(self, **kw): return FakePage()
                def close(self): pass

            # Mock sync_playwright at the import location inside run_browser_pipeline
            fake_pw = mock.MagicMock()
            fake_pw.__enter__ = mock.Mock(return_value=fake_pw)
            fake_pw.__exit__ = mock.Mock(return_value=False)
            with mock.patch("playwright.sync_api.sync_playwright", return_value=fake_pw):
                with mock.patch.object(pipeline, "launch_chromium_browser", return_value=FakeBrowser()):
                    with mock.patch.object(pipeline, "write_report", return_value={}):
                        with mock.patch.object(pipeline, "write_pending_user_input_for_unconfirmed_fields", return_value=None):
                            rc = pipeline.run_browser_pipeline(
                            payload_path,
                            headless=True,
                            submit=False,
                            board_name="gem",
                            form_ready_selector=".form-33",
                            fill_step_fn=lambda page, step: fill_calls.append(step),
                            page_snapshot_fn=lambda page: {},
                            classify_state_fn=lambda snap: {"status": "pending"},
                            click_submit_fn=lambda page: True,
                            capture_fn=lambda page, path: None,
                            )

            self.assertEqual(rc, 0)
            self.assertEqual(len(fill_calls), 1)
```

- [ ] **Step 3: Run all tests to confirm no regressions**

Run: `python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add scripts/autofill_pipeline.py tests/test_autofill_pipeline.py
git commit -m "feat: add run_browser_pipeline to autofill_pipeline.py"
```

## Chunk 3: Migrate Gem

### Task 7: Refactor `autofill_gem.py` to use shared modules

**Files:**
- Modify: `scripts/autofill_gem.py`

- [ ] **Step 1: Replace `main()` with `autofill_main` call**

Replace the entire `main()` function (lines 990-1044) with:

```python
def main() -> int:
    from autofill_pipeline import autofill_main, run_browser_pipeline
    return autofill_main(
        board_name="gem",
        build_payload_fn=_build_payload,
        run_browser_fn=lambda pp, headless, submit: run_browser_pipeline(
            pp,
            headless=headless,
            submit=submit,
            board_name="gem",
            form_ready_fn=_wait_for_gem_form,
            fill_step_fn=_fill_step,
            page_snapshot_fn=lambda page: page_snapshot(page, form_selector=".form-33", captcha_type="hcaptcha"),
            classify_state_fn=_classify_submit_state,
            click_submit_fn=lambda page: click_submit_button(page, button_names=SUBMIT_BUTTON_NAMES),
            capture_fn=lambda page, path: capture_full_page(page, path, preferred_selectors=PREFERRED_CAPTURE_SELECTORS),
            wait_for_captcha_fn=_wait_for_manual_captcha_resolution_gem,
            confirmed_outcome_from_email_fn=_confirmed_outcome_from_email,
        ),
    )
```

Also add the `_wait_for_gem_form(page)` helper that wraps the `page.wait_for_function()` call currently at `scripts/autofill_gem.py:822-828`.

Add board constants at the top of the file:
```python
SUBMIT_BUTTON_NAMES = ("Apply without saving", "Apply and save", "Submit application", "Submit", "Apply")
PREFERRED_CAPTURE_SELECTORS = (".formContainer-21",)  # Board-specific; shared capture_full_page handles generic fallbacks
```

- [ ] **Step 2: Replace duplicated utility functions with imports from `autofill_common`**

Delete or replace these functions in `autofill_gem.py` with imports:
- `_label_matches` → `from autofill_common import label_matches`
- `_capture_full_page` → `from autofill_common import capture_full_page`
- `_click_submit_button` → `from autofill_common import click_submit_button`
- `_page_snapshot` → `from autofill_common import page_snapshot`
- `_write_report` → `from autofill_common import write_report`

Update all call sites within `autofill_gem.py` to use the new function names (drop the underscore prefix) and pass board-specific parameters.

For `_classify_submit_state`: keep inline (per spec — ordering differs per board).

- [ ] **Step 3: Delete `_run_playwright` from `autofill_gem.py`**

The entire `_run_playwright` function (lines 789-987) is replaced by the `run_browser_pipeline` call in `main()`. Delete it. Any helper functions it used that were only called from `_run_playwright` and are now handled by the pipeline should also be deleted (e.g. parts of the confirmation polling loop).

Keep `_wait_for_manual_captcha_resolution` as `_wait_for_manual_captcha_resolution_gem` if its signature differs from other boards, or wrap it to match the pipeline's expected callback signature.

Keep `_confirmed_outcome_from_email` as it builds the outcome dict from an email confirmation.

- [ ] **Step 4: Update Gem tests to use shared `write_report`**

In `tests/test_gem_autofill.py`, update `test_write_report_splits_planned_but_unconfirmed_fields`:
- Change `autofill._write_report(payload, runtime)` to use the shared module:
  ```python
  common = load_module("autofill_common", "scripts/autofill_common.py")
  report_payload = common.write_report(payload, board_name="gem", runtime=runtime)
  ```
- The `_write_report` function no longer exists in `autofill_gem.py`, so this test **will** fail without this update.

Run: `python -m pytest tests/test_gem_autofill.py -v`
Expected: All PASS

- [ ] **Step 5: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add scripts/autofill_gem.py tests/test_gem_autofill.py
git commit -m "refactor: migrate autofill_gem.py to shared modules"
```

## Chunk 4: Migrate Lever

### Task 8: Refactor `autofill_lever.py` to use shared modules

**Files:**
- Modify: `scripts/autofill_lever.py`
- Modify: `tests/test_lever_autofill.py`

- [ ] **Step 1: Replace `main()` with `autofill_main` call**

Same pattern as Gem. Add board constants:
```python
SUBMIT_BUTTON_NAMES = ("Submit application", "Submit", "Apply")
PREFERRED_CAPTURE_SELECTORS = ("#application-form", ".content-wrapper", "main")
FORM_READY_SELECTOR = "#application-form"
```

Replace `main()` (lines 1246-1300) with:

```python
def main() -> int:
    from autofill_pipeline import autofill_main, run_browser_pipeline
    return autofill_main(
        board_name="lever",
        build_payload_fn=_build_payload,
        run_browser_fn=lambda pp, headless, submit: run_browser_pipeline(
            pp,
            headless=headless,
            submit=submit,
            board_name="lever",
            form_ready_selector=FORM_READY_SELECTOR,
            fill_step_fn=_fill_step,
            page_snapshot_fn=lambda page: page_snapshot(page, form_selector=FORM_READY_SELECTOR, captcha_type="hcaptcha"),
            classify_state_fn=_classify_submit_state,
            click_submit_fn=lambda page: click_submit_button(page, button_names=SUBMIT_BUTTON_NAMES),
            capture_fn=lambda page, path: capture_full_page(page, path, preferred_selectors=PREFERRED_CAPTURE_SELECTORS),
            pre_submit_hook=_wait_for_pre_submit_manual_challenge,
            wait_for_captcha_fn=_wait_for_manual_captcha_resolution_lever,
            confirmed_outcome_from_email_fn=_confirmed_outcome_from_email,
        ),
    )
```

Lever-specific: the `pre_submit_hook` passes `_wait_for_pre_submit_manual_challenge` which handles hCaptcha challenges that appear before the submit button is clickable. Rename existing `_wait_for_manual_captcha_resolution` to `_wait_for_manual_captcha_resolution_lever` to avoid name collision with the shared module.

- [ ] **Step 2: Replace duplicated utility functions with imports**

Delete and replace with imports from `autofill_common`:
- `_label_matches` → `label_matches` (Lever call sites must pass `word_boundary=True`)
- `_capture_full_page` → `capture_full_page`
- `_click_submit_button` → `click_submit_button`
- `_page_snapshot` → `page_snapshot` (with `form_selector="#application-form"`, `captcha_type="hcaptcha"`)
- `_write_report` → `write_report`
- `_select_option` → `select_option` (Lever call sites extract options from field dict first, pass `filter_select_prefix=True`)
- `_write_submit_debug_artifacts` → `write_submit_debug_artifacts`

Keep inline: `_classify_submit_state`, `_fill_step`, `_field_group`, `_infer_step`, `_build_payload`, `_set_choice_checked`, board-specific helpers.

- [ ] **Step 3: Delete `_run_playwright` and update `_label_matches` call sites**

Delete `_run_playwright`. Search for all `_label_matches(field,` calls and replace with `label_matches(field, ..., word_boundary=True)`.

Search for all `_select_option(field, candidates)` calls. Lever's `_select_option` extracts options from the field dict. Replace with the shared function using `filter_select_prefix=True`:
```python
select_option(field.get("options"), candidate, filter_select_prefix=True)
```

- [ ] **Step 4: Update tests**

In `tests/test_lever_autofill.py`:
- `test_label_matches_uses_word_boundaries`: This test calls `autofill._label_matches(field, "ethnicity")`. After migration, `_label_matches` is removed from `autofill_lever.py`. Either:
  - Update the test to import from `autofill_common` directly and test `label_matches(field, "ethnicity", word_boundary=True)`, OR
  - Keep a thin wrapper `_label_matches` in `autofill_lever.py` that calls `label_matches(..., word_boundary=True)` — the test then still works.

  Prefer the wrapper approach (simpler migration, fewer test changes).

- `test_write_report_splits_planned_but_unconfirmed_fields`: Change `autofill._write_report(payload, runtime)` to:
  ```python
  common = load_module("autofill_common", "scripts/autofill_common.py")
  report_payload = common.write_report(payload, board_name="lever", runtime=runtime)
  ```

- [ ] **Step 5: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add scripts/autofill_lever.py tests/test_lever_autofill.py
git commit -m "refactor: migrate autofill_lever.py to shared modules"
```

## Chunk 5: Migrate Ashby

### Task 9: Refactor `autofill_ashby.py` to use shared modules

**Files:**
- Modify: `scripts/autofill_ashby.py`
- Modify: `tests/test_ashby_autofill.py`

- [ ] **Step 1: Replace `main()` with `autofill_main` call**

Add board constants:
```python
SUBMIT_BUTTON_NAMES = ("Submit Application", "Submit", "Apply")
PREFERRED_CAPTURE_SELECTORS = ("main", "#root")
FORM_READY_SELECTOR = ".ashby-application-form-field-entry"
```

Replace `main()` (lines 1428-1482) with:

```python
def main() -> int:
    from autofill_pipeline import autofill_main, run_browser_pipeline
    return autofill_main(
        board_name="ashby",
        build_payload_fn=_build_payload,
        run_browser_fn=lambda pp, headless, submit: run_browser_pipeline(
            pp,
            headless=headless,
            submit=submit,
            board_name="ashby",
            form_ready_selector=FORM_READY_SELECTOR,
            fill_step_fn=_fill_step,
            page_snapshot_fn=lambda page: page_snapshot(page, form_selector=FORM_READY_SELECTOR, captcha_type="recaptcha"),
            classify_state_fn=_classify_submit_state,
            click_submit_fn=lambda page: click_submit_button(page, button_names=SUBMIT_BUTTON_NAMES),
            capture_fn=lambda page, path: capture_full_page(page, path, preferred_selectors=PREFERRED_CAPTURE_SELECTORS),
            wait_for_captcha_fn=_wait_for_manual_captcha_resolution_ashby,
            confirmed_outcome_from_email_fn=_confirmed_outcome_from_email,
        ),
    )
```

Ashby uses `recaptcha` (not hcaptcha). Rename existing `_wait_for_manual_captcha_resolution` to `_wait_for_manual_captcha_resolution_ashby`.

- [ ] **Step 2: Replace duplicated utility functions with imports**

Delete and replace with imports from `autofill_common`:
- `_label_matches` → `label_matches` (substring mode, default)
- `_capture_full_page` → `capture_full_page`
- `_click_submit_button` → `click_submit_button`
- `_page_snapshot` → `page_snapshot` (with `form_selector=".ashby-application-form-field-entry"`, `captcha_type="recaptcha"`)
- `_write_report` → `write_report`
- `_yes_no_step` → `yes_no_step` (passing Ashby's `_match_selectable_label` as the `option_matcher`)

Keep inline: `_classify_submit_state`, `_fill_step`, `_field_entry`, `_fillable_text_locator`, `_click_choice`, `_infer_step`, `_build_payload`, Ashby-specific field helpers.

- [ ] **Step 3: Delete `_run_playwright`**

Delete `_run_playwright` (lines 1232-1427). All its logic is handled by `run_browser_pipeline`.

- [ ] **Step 4: Update tests**

In `tests/test_ashby_autofill.py`:
- Update `test_write_report_*` tests: change `autofill._write_report(payload, runtime)` to:
  ```python
  common = load_module("autofill_common", "scripts/autofill_common.py")
  report_payload = common.write_report(payload, board_name="ashby", runtime=runtime)
  ```
- All other tests should pass without changes since `_infer_step`, `_fill_step`, `_build_payload` stay in the board script.

- [ ] **Step 5: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add scripts/autofill_ashby.py tests/test_ashby_autofill.py
git commit -m "refactor: migrate autofill_ashby.py to shared modules"
```

## Chunk 6: Migrate Dover

### Task 10: Refactor `autofill_dover.py` to use shared modules

**Files:**
- Modify: `scripts/autofill_dover.py`
- Modify: `tests/test_dover_autofill.py`

- [ ] **Step 1: Replace `main()` with `autofill_main` call using `run_browser_fn` for API submission**

Dover is API-based but we can use `run_browser_fn` as a generic "post-payload callback" — it doesn't have to launch a browser. This keeps Dover integrated with `autofill_main`'s CLI handling (including `--payload-only`, `--submit`, `--provider`).

Replace `main()` (lines 575-659) with:

```python
def main() -> int:
    from autofill_pipeline import autofill_main
    return autofill_main(
        board_name="dover",
        build_payload_fn=_build_payload,
        has_browser=True,  # Use run_browser_fn as submit callback
        run_browser_fn=_run_submit,
    )


def _run_submit(payload_path: Path, headless: bool, submit: bool) -> int:
    """API-based submission — headless/browser flags are ignored."""
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    out_dir = Path(payload["out_dir"])

    if not submit:
        return 0

    submitted_at_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    response = _submit_payload(payload)
    response_artifact = _response_artifact(response["status_code"], response["raw_text"])
    role_submit_path(out_dir, SUBMISSION_RESPONSE_JSON).write_text(
        json_dumps_pretty(response_artifact) + "\n", encoding="utf-8",
    )

    error = response.get("error")
    status_code = int(response["status_code"])
    if error or status_code < 200 or status_code >= 300:
        raise RuntimeError(
            f"Dover submit failed (HTTP {status_code}). {error or 'No error details provided.'} "
            f"See {role_submit_path(out_dir, SUBMISSION_RESPONSE_JSON)}"
        )

    outcome = {
        "status": "confirmed",
        "reason": "api",
        "snapshot": {"url": payload["job_url"], "page_text": "Dover API accepted the application payload."},
        "errors": [],
        "invalid_fields": [],
    }
    sync_result = sync_notion_after_submit(payload, outcome, provider="dover", min_received_at_utc=submitted_at_utc)
    print("Dover application submitted successfully.")
    status = str(sync_result.get("status") or "")
    if status:
        print(f"Notion sync status: {status}")
    if status == "pending_email_confirmation":
        print(f"Waiting for the confirmation email or rerun:\n  job-assets notion-sync {out_dir}")
    return 0
```

Note: Dover does not have a `_write_report` function — it has no report-writing logic (API-based boards don't generate autofill reports). So no `write_report` replacement is needed.

- [ ] **Step 2: Replace `_label_matches` with import**

Replace `_label_matches` with `from autofill_common import label_matches`. Update call sites — Dover passes raw strings, so no `word_boundary` needed.

- [ ] **Step 3: Replace `_select_option` with import**

Replace `_select_option` with `from autofill_common import select_option`. Dover's signature is the same as the unified one, so this is a direct swap.

- [ ] **Step 4: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add scripts/autofill_dover.py
git commit -m "refactor: migrate autofill_dover.py to shared modules"
```

## Chunk 7: Verification & Cleanup

### Task 11: Final verification

**Files:**
- All modified files

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 2: Verify line counts match expectations**

Run: `wc -l scripts/autofill_gem.py scripts/autofill_lever.py scripts/autofill_ashby.py scripts/autofill_dover.py scripts/autofill_common.py scripts/autofill_pipeline.py`

Expected approximate results:
- `autofill_gem.py`: ~250 lines (down from 1,044)
- `autofill_lever.py`: ~300 lines (down from 1,300)
- `autofill_ashby.py`: ~350 lines (down from 1,482)
- `autofill_dover.py`: ~400 lines (down from 663)
- `autofill_common.py`: ~350 lines (new)
- `autofill_pipeline.py`: ~300 lines (new)

- [ ] **Step 3: Verify Greenhouse is untouched**

Run: `git diff scripts/autofill_greenhouse.py`
Expected: No changes

- [ ] **Step 4: Run existing Greenhouse tests**

Run: `python -m pytest tests/test_greenhouse_autofill.py -v`
Expected: All pass (confirming Greenhouse was not affected)

- [ ] **Step 5: Commit any final cleanup**

Stage only the specific files modified during consolidation:

```bash
git add scripts/autofill_common.py scripts/autofill_pipeline.py scripts/autofill_gem.py scripts/autofill_lever.py scripts/autofill_ashby.py scripts/autofill_dover.py tests/test_autofill_common.py tests/test_autofill_pipeline.py tests/test_gem_autofill.py tests/test_lever_autofill.py tests/test_ashby_autofill.py tests/test_dover_autofill.py
git commit -m "chore: final cleanup after autofill consolidation"
```
