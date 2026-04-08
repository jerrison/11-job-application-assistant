# Application Draft Mode Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `draft` pause point to the job pipeline so users can review assets, answers, and pre-submit screenshots before approving submission — via CLI, TUI, web, or LLM conversation.

**Architecture:** New `draft` DB status between `generating` and `submitting`. Pipeline stops after autofill, writes draft artifacts (summary md/png, status/overrides JSON). User reviews via any surface, edits trigger auto-classified fix reports. Approval resumes submission.

**Tech Stack:** Python 3.14, SQLite (WAL), FastAPI + uvicorn (optional `[web]` group), Pillow (PNG generation), Textual (TUI), argparse (CLI)

**Spec:** `docs/superpowers/specs/2026-03-15-application-draft-mode-design.md`

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `scripts/draft_manager.py` | Core draft logic: generate summary md/png, diff edits, auto-classify changes, manage draft_status.json and draft_overrides.json, generate fix reports |
| `scripts/build_draft_summary.py` | CLI tool: render `draft_summary.png` from autofill report data using Pillow |
| `scripts/draft_web.py` | FastAPI app: REST API + HTML frontend for draft review |
| `scripts/templates/draft_dashboard.html` | Jinja2 template: draft list page |
| `scripts/templates/draft_detail.html` | Jinja2 template: single draft review page with inline editing |
| `tests/test_draft_manager.py` | Unit tests for draft_manager |
| `tests/test_draft_web.py` | API tests for draft_web |

### Modified Files
| File | Changes |
|------|---------|
| `scripts/job_db.py:8-13` | Add `"draft"` to `JOB_STATUSES` |
| `scripts/pipeline_orchestrator.py:205-219` | Stop at `draft` unless `--auto-submit`; add `approve_job()`, `regenerate_job()` |
| `scripts/submit_application.py:359-361` | Add `--draft` flag |
| `scripts/application_submit_common.py:1254-1310` | Load + apply `draft_overrides.json` |
| `scripts/autofill_pipeline.py:246` | Respect `--draft` flag (skip submit click) |
| `scripts/autofill_common.py:122` | After `write_report()`, call draft summary generation |
| `bin/job-assets:707-920` | Add `draft` subcommand group |
| `scripts/job_tui.py:268,619` | Draft filter tab + draft detail actions + image rendering |
| `scripts/job_worker.py:109-146` | Stop at `draft`; add `--auto-submit` flag; stale draft sweep |
| `pyproject.toml` | Add `Pillow`; add optional `[web]` group (`fastapi`, `uvicorn`, `jinja2`) |

---

## Chunk 1: Core — DB Status + Draft Manager

### Task 1: Add `draft` to DB statuses

**Files:**
- Modify: `scripts/job_db.py:8-13`
- Test: `tests/test_draft_manager.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_draft_manager.py
import unittest

class DraftStatusTests(unittest.TestCase):
    def test_draft_is_valid_job_status(self):
        from job_db import JOB_STATUSES
        self.assertIn("draft", JOB_STATUSES)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_draft_manager.py::DraftStatusTests::test_draft_is_valid_job_status -v`
Expected: FAIL — `"draft"` not in tuple

- [ ] **Step 3: Add `draft` to JOB_STATUSES**

In `scripts/job_db.py`, change line 9:
```python
JOB_STATUSES = (
    "queued", "resolving", "generating", "draft", "submitting",
    "submitted", "retrying", "fix_in_progress",
    "failed", "skipped_captcha", "skipped_auth",
    "needs_manual", "needs_board_url",
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_draft_manager.py::DraftStatusTests::test_draft_is_valid_job_status -v`
Expected: PASS

- [ ] **Step 5: Run full test suite for regressions**

Run: `uv run python -m pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add scripts/job_db.py tests/test_draft_manager.py
git commit -m "feat(draft): add 'draft' to JOB_STATUSES"
```

---

### Task 2: Create `draft_manager.py` — summary generation

**Files:**
- Create: `scripts/draft_manager.py`
- Test: `tests/test_draft_manager.py`

- [ ] **Step 1: Write failing test for summary markdown generation**

```python
# tests/test_draft_manager.py (append)
import json
import tempfile
from pathlib import Path

class DraftSummaryTests(unittest.TestCase):
    def test_generate_draft_summary_md(self):
        from draft_manager import generate_draft_summary

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()

            # Minimal autofill report
            report = {
                "board": "ashby",
                "fields": [
                    {
                        "field_name": "application_name",
                        "label": "Full Name",
                        "kind": "text",
                        "required": True,
                        "status": "filled",
                        "value": "Jerrison Li",
                        "source": "master_resume.md",
                    },
                    {
                        "field_name": "survey_1_pronouns",
                        "label": "Pronouns",
                        "kind": "choice",
                        "required": False,
                        "status": "unfilled",
                        "value": None,
                        "source": None,
                    },
                ],
            }
            (submit_dir / "ashby_autofill_report.json").write_text(json.dumps(report))

            meta = {"company": "Bubble", "role_title": "Group PM", "board": "ashby"}
            result = generate_draft_summary(out_dir, submit_dir, meta)

            self.assertTrue((out_dir / "draft_summary.md").exists())
            self.assertTrue((out_dir / "draft_summary.original.md").exists())
            md = (out_dir / "draft_summary.md").read_text()
            self.assertIn("Bubble", md)
            self.assertIn("(application_name)", md)
            self.assertIn("**Status:** filled", md)
            self.assertIn("**Status:** unfilled", md)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_draft_manager.py::DraftSummaryTests::test_generate_draft_summary_md -v`
Expected: FAIL — `draft_manager` module not found

- [ ] **Step 3: Implement `generate_draft_summary`**

Create `scripts/draft_manager.py`:

```python
"""Core draft mode logic: summary generation, diff detection, override management."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


def generate_draft_summary(
    out_dir: Path,
    submit_dir: Path,
    meta: dict,
) -> dict:
    """Generate draft_summary.md, draft_summary.original.md, and draft_status.json.

    Returns dict with paths to generated files.
    """
    board = meta.get("board", "unknown")
    report_path = submit_dir / f"{board}_autofill_report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else {"fields": []}
    fields = report.get("fields", [])

    company = meta.get("company", "Unknown")
    role = meta.get("role_title", "Unknown")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"# Draft: {role} — {company}",
        f"**Board:** {board} | **Generated:** {now}",
        "",
    ]

    # Resume and cover letter references
    docs_dir = out_dir / "documents"
    if docs_dir.exists():
        lines.append("## Resume & Cover Letter")
        for f in sorted(docs_dir.glob("*.pdf")):
            lines.append(f"- File: documents/{f.name}")
        lines.append("")

    # Application answers
    lines.append("## Application Answers")
    lines.append("")
    for i, field in enumerate(fields, 1):
        fname = field.get("field_name", "unknown")
        label = field.get("label", "Unknown")
        kind = field.get("kind", "text")
        required = "yes" if field.get("required") else "no"
        source = field.get("source") or "—"
        value = field.get("value") or "—"
        status = field.get("status", "unfilled")

        lines.append(f"### {i}. {label} ({fname})")
        lines.append(f"- **Kind:** {kind} | **Required:** {required} | **Source:** {source}")
        lines.append(f"- **Answer:** {value}")
        lines.append(f"- **Status:** {status}")
        lines.append("")

    md_text = "\n".join(lines)

    summary_path = out_dir / "draft_summary.md"
    original_path = out_dir / "draft_summary.original.md"
    summary_path.write_text(md_text, encoding="utf-8")
    original_path.write_text(md_text, encoding="utf-8")

    # Write draft_status.json
    status_path = out_dir / "draft_status.json"
    existing_version = 0
    if status_path.exists():
        try:
            existing_version = json.loads(status_path.read_text()).get("draft_version", 0)
        except (json.JSONDecodeError, KeyError):
            pass

    status_data = {
        "status": "awaiting_review",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reviewed_at": None,
        "reviewed_action": None,
        "draft_version": existing_version + 1,
    }
    status_path.write_text(json.dumps(status_data, indent=2), encoding="utf-8")

    return {
        "summary": str(summary_path),
        "original": str(original_path),
        "status": str(status_path),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_draft_manager.py::DraftSummaryTests::test_generate_draft_summary_md -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/draft_manager.py tests/test_draft_manager.py
git commit -m "feat(draft): add draft_manager with summary generation"
```

---

### Task 3: Draft manager — diff detection and auto-classification

**Files:**
- Modify: `scripts/draft_manager.py`
- Test: `tests/test_draft_manager.py`

- [ ] **Step 1: Write failing test for diff detection**

```python
# tests/test_draft_manager.py (append)
class DraftDiffTests(unittest.TestCase):
    def test_classify_answer_change(self):
        from draft_manager import classify_draft_edits

        original = "### 1. Salary (app_salary)\n- **Answer:** Yes\n- **Status:** filled"
        edited = "### 1. Salary (app_salary)\n- **Answer:** Open to negotiation\n- **Status:** filled"

        changes = classify_draft_edits(original, edited)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["field_key"], "app_salary")
        self.assertEqual(changes[0]["old_answer"], "Yes")
        self.assertEqual(changes[0]["new_answer"], "Open to negotiation")

    def test_classify_unfilled_to_filled(self):
        from draft_manager import classify_draft_edits

        original = "### 1. Pronouns (survey_pronouns)\n- **Answer:** —\n- **Status:** unfilled"
        edited = "### 1. Pronouns (survey_pronouns)\n- **Answer:** He/Him\n- **Status:** unfilled"

        changes = classify_draft_edits(original, edited)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["classification"], "missing_handler")

    def test_no_changes_returns_empty(self):
        from draft_manager import classify_draft_edits

        text = "### 1. Name (app_name)\n- **Answer:** Jerrison\n- **Status:** filled"
        changes = classify_draft_edits(text, text)
        self.assertEqual(changes, [])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_draft_manager.py::DraftDiffTests -v`
Expected: FAIL — `classify_draft_edits` not found

- [ ] **Step 3: Implement `classify_draft_edits`**

Add to `scripts/draft_manager.py`:

```python
import re

_FIELD_RE = re.compile(
    r"###\s+\d+\.\s+.+?\((\S+)\)\s*\n"
    r".*?\n"
    r"-\s+\*\*Answer:\*\*\s*(.*?)\s*\n"
    r"-\s+\*\*Status:\*\*\s*(\S+)",
    re.MULTILINE,
)


def _parse_fields(text: str) -> dict[str, dict]:
    """Parse draft_summary.md into {field_key: {answer, status}} dict."""
    fields = {}
    for m in _FIELD_RE.finditer(text):
        fields[m.group(1)] = {"answer": m.group(2), "status": m.group(3)}
    return fields


def classify_draft_edits(original_text: str, edited_text: str) -> list[dict]:
    """Diff original vs edited draft summary and classify each change.

    Returns list of dicts with: field_key, old_answer, new_answer, classification.
    Classifications:
      - "missing_handler": unfilled field now has an answer
      - "wrong_answer": filled field answer changed
      - "value_override": minor text change (likely preference, not bug)
    """
    orig = _parse_fields(original_text)
    edited = _parse_fields(edited_text)
    changes = []

    for key, ed in edited.items():
        og = orig.get(key)
        if og is None:
            continue
        if og["answer"] == ed["answer"]:
            continue

        classification = "wrong_answer"
        if og["answer"] == "—" or og["status"] == "unfilled":
            classification = "missing_handler"

        changes.append({
            "field_key": key,
            "old_answer": og["answer"],
            "new_answer": ed["answer"],
            "old_status": og["status"],
            "classification": classification,
        })

    return changes
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_draft_manager.py::DraftDiffTests -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/draft_manager.py tests/test_draft_manager.py
git commit -m "feat(draft): add diff detection and auto-classification"
```

---

### Task 4: Draft manager — override persistence and fix report generation

**Files:**
- Modify: `scripts/draft_manager.py`
- Test: `tests/test_draft_manager.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_draft_manager.py (append)
class DraftOverrideTests(unittest.TestCase):
    def test_apply_edits_writes_overrides(self):
        from draft_manager import apply_draft_edits

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            changes = [
                {"field_key": "app_salary", "old_answer": "Yes", "new_answer": "Open", "old_status": "filled", "classification": "wrong_answer"},
            ]
            apply_draft_edits(out_dir, changes)

            overrides = json.loads((out_dir / "draft_overrides.json").read_text())
            self.assertEqual(overrides["app_salary"], "Open")

    def test_apply_edits_generates_fix_report(self):
        from draft_manager import apply_draft_edits

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            changes = [
                {"field_key": "survey_pronouns", "old_answer": "—", "new_answer": "He/Him", "old_status": "unfilled", "classification": "missing_handler"},
            ]
            apply_draft_edits(out_dir, changes)

            report_path = out_dir / "draft_fix_report.md"
            self.assertTrue(report_path.exists())
            report = report_path.read_text()
            self.assertIn("survey_pronouns", report)
            self.assertIn("missing_handler", report)

    def test_apply_edits_preserves_existing_overrides(self):
        from draft_manager import apply_draft_edits

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            (out_dir / "draft_overrides.json").write_text(json.dumps({"existing_key": "keep"}))

            changes = [
                {"field_key": "new_key", "old_answer": "X", "new_answer": "Y", "old_status": "filled", "classification": "wrong_answer"},
            ]
            apply_draft_edits(out_dir, changes)

            overrides = json.loads((out_dir / "draft_overrides.json").read_text())
            self.assertEqual(overrides["existing_key"], "keep")
            self.assertEqual(overrides["new_key"], "Y")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_draft_manager.py::DraftOverrideTests -v`
Expected: FAIL

- [ ] **Step 3: Implement `apply_draft_edits`**

Add to `scripts/draft_manager.py`:

```python
def apply_draft_edits(out_dir: Path, changes: list[dict]) -> dict:
    """Process classified changes: write overrides and generate fix report.

    Returns dict with "overrides_path" and optionally "fix_report_path".
    """
    # Load existing overrides
    overrides_path = out_dir / "draft_overrides.json"
    overrides = {}
    if overrides_path.exists():
        try:
            overrides = json.loads(overrides_path.read_text())
        except json.JSONDecodeError:
            pass

    # All changes become overrides (applied on next regeneration)
    fix_items = []
    for change in changes:
        overrides[change["field_key"]] = change["new_answer"]
        if change["classification"] in ("missing_handler", "wrong_answer"):
            fix_items.append(change)

    overrides_path.write_text(json.dumps(overrides, indent=2), encoding="utf-8")

    result = {"overrides_path": str(overrides_path)}

    # Generate fix report if there are code-level issues
    if fix_items:
        lines = ["# Draft Fix Report", "", "Auto-generated from user edits to draft_summary.md.", ""]
        for item in fix_items:
            lines.append(f"## {item['field_key']}")
            lines.append(f"- **Classification:** {item['classification']}")
            lines.append(f"- **Old answer:** {item['old_answer']}")
            lines.append(f"- **New answer (user expects):** {item['new_answer']}")
            lines.append(f"- **Old status:** {item['old_status']}")
            lines.append("")

        report_path = out_dir / "draft_fix_report.md"
        report_path.write_text("\n".join(lines), encoding="utf-8")
        result["fix_report_path"] = str(report_path)

    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_draft_manager.py::DraftOverrideTests -v`
Expected: PASS

- [ ] **Step 5: Write CAS (compare-and-swap) transition tests**

```python
# tests/test_draft_manager.py (append)
import sqlite3

class DraftCASTransitionTests(unittest.TestCase):
    def _create_test_db(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("""CREATE TABLE jobs (
            id INTEGER PRIMARY KEY, status TEXT, canonical_url TEXT UNIQUE,
            output_dir TEXT, company TEXT, role_title TEXT, board TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP, error_message TEXT)""")
        return conn

    def test_approve_job_cas_rejects_non_draft(self):
        """Compare-and-swap: approve only works on draft status."""
        from pipeline_orchestrator import approve_job
        conn = self._create_test_db()
        conn.execute("INSERT INTO jobs (id, status) VALUES (1, 'submitted')")
        conn.commit()
        self.assertFalse(approve_job(conn, 1))

    def test_approve_job_cas_accepts_draft(self):
        """Compare-and-swap: approve works on draft status."""
        from pipeline_orchestrator import approve_job
        conn = self._create_test_db()
        conn.execute("INSERT INTO jobs (id, status) VALUES (1, 'draft')")
        conn.commit()
        self.assertTrue(approve_job(conn, 1))

    def test_concurrent_approve_regenerate_one_wins(self):
        """Only one of approve/regenerate succeeds on the same draft."""
        from pipeline_orchestrator import approve_job, regenerate_job
        conn = self._create_test_db()
        conn.execute("INSERT INTO jobs (id, status) VALUES (1, 'draft')")
        conn.commit()
        # First call wins
        self.assertTrue(approve_job(conn, 1))
        # Second call fails — status is no longer 'draft'
        self.assertFalse(regenerate_job(conn, 1))
```

- [ ] **Step 6: Run CAS tests**

Run: `uv run python -m pytest tests/test_draft_manager.py::DraftCASTransitionTests -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add scripts/draft_manager.py tests/test_draft_manager.py
git commit -m "feat(draft): add override persistence, fix reports, and CAS transition tests"
```

---

## Chunk 2: Pipeline Integration

### Task 5: Apply draft overrides in answer generation

**Files:**
- Modify: `scripts/application_submit_common.py:1254-1310`
- Test: `tests/test_submit_application.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_submit_application.py (append to existing test class)
def test_apply_generated_answer_overrides_uses_draft_overrides(self):
    """Draft overrides take precedence over LLM-generated answers."""
    import tempfile, json
    from pathlib import Path
    from application_submit_common import apply_generated_answer_overrides

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        (out_dir / "draft_overrides.json").write_text(json.dumps({
            "app_salary": "Open to negotiation"
        }))

        specs = [{"field_name": "app_salary", "label": "Salary", "options": []}]
        answers = {"app_salary": "Yes"}  # wrong LLM answer

        result = apply_generated_answer_overrides(specs, answers, out_dir=out_dir)
        self.assertEqual(result["app_salary"], "Open to negotiation")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_submit_application.py::SubmitApplicationTests::test_apply_generated_answer_overrides_uses_draft_overrides -v`
Expected: FAIL — draft overrides not applied

- [ ] **Step 3: Add draft override loading to `apply_generated_answer_overrides`**

In `scripts/application_submit_common.py`, at the end of `apply_generated_answer_overrides` (before `return overridden`), add:

```python
    # Apply draft overrides last — user edits take highest precedence
    if out_dir:
        draft_overrides_path = out_dir / "draft_overrides.json"
        if draft_overrides_path.exists():
            try:
                draft_overrides = json.loads(draft_overrides_path.read_text())
                for field_name, value in draft_overrides.items():
                    overridden[field_name] = value  # Add even if not already present
            except (json.JSONDecodeError, KeyError):
                pass

    return overridden
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_submit_application.py::SubmitApplicationTests::test_apply_generated_answer_overrides_uses_draft_overrides -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `uv run python -m pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add scripts/application_submit_common.py tests/test_submit_application.py
git commit -m "feat(draft): apply draft_overrides.json in answer generation"
```

---

### Task 6: Add `--draft` flag to submit pipeline

**Files:**
- Modify: `scripts/submit_application.py:359-361`
- Modify: `scripts/autofill_pipeline.py:118,246`

- [ ] **Step 1: Add `--draft` flag to submit_application.py argparse**

In `scripts/submit_application.py`, after the `--submit` argument (line ~361), add:

```python
parser.add_argument(
    "--draft",
    action="store_true",
    help="Fill the form and take screenshots but stop before submitting. Generates draft artifacts for review.",
)
```

Pass `args.draft` through to the board-specific autofill script invocation, alongside `--submit`. When `--draft` is set, pass `--draft` instead of `--submit` to the board script.

- [ ] **Step 2: Add `--draft` support to `autofill_pipeline.py`**

In `scripts/autofill_pipeline.py`, `--draft` is passed as `submit=False` to `run_browser_pipeline`. No new parameter needed. The existing `if not submit: return 0` path at line 235 already handles this. The only change needed is in `autofill_main()`: parse `--draft` from argv and set `submit = not args.draft` (mutually exclusive with `--submit`).

```python
# In autofill_main(), add to argparse:
group = parser.add_mutually_exclusive_group()
group.add_argument("--submit", action="store_true")
group.add_argument("--draft", action="store_true")
# ...
submit = args.submit and not args.draft  # --draft forces submit=False
```

- [ ] **Step 3: Verify `--submit` still works (regression test)**

Run: `uv run python -m pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add scripts/submit_application.py scripts/autofill_pipeline.py
git commit -m "feat(draft): add --draft flag to stop before submit"
```

---

### Task 7: Pipeline orchestrator — stop at `draft`

**Files:**
- Modify: `scripts/pipeline_orchestrator.py:95-302`

- [ ] **Step 1: Add `auto_submit` parameter to `process_job()`**

In `scripts/pipeline_orchestrator.py`, modify `process_job()` signature:

```python
def process_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    worker_id: int = 0,
    headless: bool = True,
    auto_submit: bool = False,
) -> str:
```

- [ ] **Step 2: Change Phase 3 (submit) to use `--draft` by default**

At lines ~205-219 where submit is called, change:

```python
# Before:
submit_cmd = _uv_python_cmd(submit_script, str(output_dir), "--submit")

# After:
if auto_submit:
    submit_cmd = _uv_python_cmd(submit_script, str(output_dir), "--submit")
else:
    submit_cmd = _uv_python_cmd(submit_script, str(output_dir), "--draft")
```

- [ ] **Step 3: After draft autofill, generate draft artifacts and set status**

After the submit subprocess returns successfully in draft mode:

```python
if not auto_submit:
    from draft_manager import generate_draft_summary
    from output_layout import active_submit_dir
    # Find the active submit directory (contains autofill report from the just-completed run)
    submit_dir = active_submit_dir(Path(output_dir))
    meta = {"company": company, "role_title": role_title, "board": board}
    generate_draft_summary(Path(output_dir), submit_dir, meta)
    update_status(conn, job_id, "draft")
    return "draft"
```

- [ ] **Step 4: Add `approve_job()` and `regenerate_job()`**

```python
def approve_job(conn: sqlite3.Connection, job_id: int) -> bool:
    """Transition draft → submitting. Returns True on success (compare-and-swap)."""
    cur = conn.execute(
        "UPDATE jobs SET status = 'submitting' WHERE id = ? AND status = 'draft'",
        (job_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def regenerate_job(conn: sqlite3.Connection, job_id: int) -> bool:
    """Transition draft → generating. Returns True on success (compare-and-swap)."""
    cur = conn.execute(
        "UPDATE jobs SET status = 'generating' WHERE id = ? AND status = 'draft'",
        (job_id,),
    )
    conn.commit()
    return cur.rowcount > 0
```

- [ ] **Step 5: Run full test suite**

Run: `uv run python -m pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add scripts/pipeline_orchestrator.py
git commit -m "feat(draft): orchestrator stops at draft, adds approve/regenerate"
```

---

## Chunk 3: CLI — Draft Subcommands

### Task 8: Add `draft` command group to CLI

**Files:**
- Modify: `bin/job-assets:707-920`

- [ ] **Step 1: Add `draft` subparser with sub-subcommands**

In `bin/job-assets`, after the existing subparser definitions (~line 920):

```python
draft_parser = subparsers.add_parser("draft", help="Draft review commands")
draft_sub = draft_parser.add_subparsers(dest="draft_command", required=True)

# draft list
draft_list_parser = draft_sub.add_parser("list", help="List jobs in draft status")
draft_list_parser.set_defaults(func=cmd_draft_list)

# draft review
draft_review_parser = draft_sub.add_parser("review", help="Show draft summary for a job")
draft_review_parser.add_argument("target", help="Job ID or URL")
draft_review_parser.add_argument("--edit", action="store_true", help="Open draft_summary.md in $EDITOR")
draft_review_parser.set_defaults(func=cmd_draft_review)

# draft approve
draft_approve_parser = draft_sub.add_parser("approve", help="Approve draft and submit")
draft_approve_parser.add_argument("target", help="Job ID or URL")
draft_approve_parser.set_defaults(func=cmd_draft_approve)

# draft reject
draft_reject_parser = draft_sub.add_parser("reject", help="Reject draft → needs_manual")
draft_reject_parser.add_argument("target", help="Job ID or URL")
draft_reject_parser.set_defaults(func=cmd_draft_reject)

# draft regenerate
draft_regen_parser = draft_sub.add_parser("regenerate", help="Process edits and regenerate draft (research cache preserved)")
draft_regen_parser.add_argument("target", help="Job ID or URL")
draft_regen_parser.set_defaults(func=cmd_draft_regenerate)

# draft serve
draft_serve_parser = draft_sub.add_parser("serve", help="Start web review server")
draft_serve_parser.add_argument("--port", type=int, default=8420)
draft_serve_parser.add_argument("--tunnel", choices=["cloudflare", "ngrok"], default=None)
draft_serve_parser.set_defaults(func=cmd_draft_serve)
```

- [ ] **Step 2: Implement `cmd_draft_list`**

```python
def cmd_draft_list(args):
    conn = _open_db()
    rows = conn.execute(
        "SELECT id, company, role_title, board, updated_at FROM jobs WHERE status = 'draft' ORDER BY updated_at DESC"
    ).fetchall()
    if not rows:
        print("No drafts pending review.")
        return
    print(f"{'ID':>5}  {'Company':<20}  {'Role':<30}  {'Board':<12}  {'Age'}")
    print("-" * 85)
    for row in rows:
        age = _format_age(row[4])
        print(f"{row[0]:>5}  {(row[1] or '?'):<20}  {(row[2] or '?'):<30}  {(row[3] or '?'):<12}  {age}")
```

- [ ] **Step 3: Implement `cmd_draft_review`**

```python
def cmd_draft_review(args):
    conn = _open_db()
    job = _resolve_job(conn, args.target)
    out_dir = Path(job["output_dir"])
    summary_path = out_dir / "draft_summary.md"

    if not summary_path.exists():
        print(f"No draft summary found at {summary_path}", file=sys.stderr)
        sys.exit(1)

    if args.edit:
        editor = os.environ.get("EDITOR", "vi")
        os.execvp(editor, [editor, str(summary_path)])
    else:
        print(summary_path.read_text())
        # Show image paths
        for img in out_dir.glob("*.png"):
            print(f"\nImage: {img}")
```

- [ ] **Step 4: Implement `cmd_draft_approve`, `cmd_draft_reject`, `cmd_draft_regenerate`**

```python
def cmd_draft_approve(args):
    conn = _open_db()
    job = _resolve_job(conn, args.target)
    from pipeline_orchestrator import approve_job
    if approve_job(conn, job["id"]):
        print(f"Draft #{job['id']} approved. Submitting...")
        # Resume from submit phase only — don't restart the full pipeline
        out_dir = Path(job["output_dir"])
        submit_script = str(Path(__file__).resolve().parent / "scripts" / "submit_application.py")
        import subprocess
        result = subprocess.run(
            ["uv", "run", "python", submit_script, str(out_dir), "--submit"],
            capture_output=False,
        )
        if result.returncode == 0:
            from job_db import update_status
            update_status(conn, job["id"], "submitted")
            print(f"Draft #{job['id']} submitted successfully.")
        else:
            from job_db import update_status
            update_status(conn, job["id"], "failed", error_message="Submit failed after approval")
            print(f"Draft #{job['id']} submission failed.", file=sys.stderr)
    else:
        print(f"Job #{job['id']} is not in draft status.", file=sys.stderr)


def cmd_draft_reject(args):
    conn = _open_db()
    job = _resolve_job(conn, args.target)
    from job_db import update_status
    update_status(conn, job["id"], "needs_manual")
    print(f"Draft #{job['id']} rejected → needs_manual.")


def cmd_draft_regenerate(args):
    conn = _open_db()
    job = _resolve_job(conn, args.target)
    out_dir = Path(job["output_dir"])

    from draft_manager import classify_draft_edits, apply_draft_edits
    original = (out_dir / "draft_summary.original.md").read_text()
    edited = (out_dir / "draft_summary.md").read_text()
    changes = classify_draft_edits(original, edited)

    if changes:
        result = apply_draft_edits(out_dir, changes)
        print(f"Detected {len(changes)} change(s).")
        if "fix_report_path" in result:
            print(f"Fix report: {result['fix_report_path']}")
            from pipeline_orchestrator import auto_fix
            auto_fix({"fix_report": result["fix_report_path"], "board": job["board"]}, job["board"])

    from pipeline_orchestrator import regenerate_job
    if regenerate_job(conn, job["id"]):
        print(f"Regenerating draft #{job['id']}...")
        from pipeline_orchestrator import process_job
        process_job(conn, job["id"])
    else:
        print(f"Job #{job['id']} is not in draft status.", file=sys.stderr)
```

- [ ] **Step 5: Implement `cmd_draft_serve`** (placeholder — full web implementation in Chunk 5)

```python
def cmd_draft_serve(args):
    try:
        from draft_web import create_app
    except ImportError:
        print("Web dependencies not installed. Run: uv pip install -e '.[web]'", file=sys.stderr)
        sys.exit(1)
    app = create_app()
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port)
```

- [ ] **Step 6: Run full test suite**

Run: `uv run python -m pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add bin/job-assets
git commit -m "feat(draft): add CLI draft list/review/approve/reject/regenerate/serve"
```

---

### Task 9: Worker — stop at draft + auto-submit flag + stale sweep

**Files:**
- Modify: `scripts/job_worker.py:109-146,180-228`

- [ ] **Step 1: Add `--auto-submit` flag**

In `job_worker.py` `main()` (~line 180), add argument:

```python
parser.add_argument("--auto-submit", action="store_true",
                    help="Bypass draft review, submit immediately")
```

Pass to `WorkerPool.__init__()` and store as `self._auto_submit`.

- [ ] **Step 2: Pass `auto_submit` to `process_job()`**

In `_worker_loop()` (~line 133), change:

```python
process_job(self._conn, job_id, worker_id=worker_id, headless=self._headless,
            auto_submit=self._auto_submit)
```

- [ ] **Step 3: Add stale draft sweep**

Add a periodic sweep in the worker main thread (every 60 seconds) or as a separate thread:

```python
def _sweep_stale_drafts(self) -> None:
    """Transition drafts older than TTL to needs_manual and update draft_status.json."""
    ttl_days = int(os.environ.get("JOB_ASSETS_DRAFT_TTL_DAYS", "7"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    rows = self._conn.execute(
        "SELECT id, output_dir FROM jobs WHERE status = 'draft' AND updated_at < ?",
        (cutoff.isoformat(),),
    ).fetchall()
    for row in rows:
        # Update draft_status.json to reflect expiry
        out_dir = Path(row[1]) if row[1] else None
        if out_dir:
            status_path = out_dir / "draft_status.json"
            if status_path.exists():
                import json
                data = json.loads(status_path.read_text())
                data["status"] = "expired"
                data["reviewed_at"] = datetime.now(timezone.utc).isoformat()
                status_path.write_text(json.dumps(data, indent=2))
    # Bulk update DB
    self._conn.execute(
        "UPDATE jobs SET status = 'needs_manual' WHERE status = 'draft' AND updated_at < ?",
        (cutoff.isoformat(),),
    )
    self._conn.commit()
```

Call `_sweep_stale_drafts()` in the main keep-alive loop every 60 seconds.

- [ ] **Step 4: Run full test suite**

Run: `uv run python -m pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add scripts/job_worker.py
git commit -m "feat(draft): worker stops at draft, adds --auto-submit and stale sweep"
```

---

## Chunk 4: Draft Summary PNG + TUI

### Task 10: Build `draft_summary.png` with Pillow

**Files:**
- Create: `scripts/build_draft_summary.py`
- Modify: `pyproject.toml`
- Test: `tests/test_draft_manager.py`

- [ ] **Step 1: Add Pillow to pyproject.toml**

```toml
dependencies = [
    # ... existing ...
    "Pillow>=11.0",
]
```

- [ ] **Step 2: Write failing test**

```python
# tests/test_draft_manager.py (append)
class DraftSummaryPngTests(unittest.TestCase):
    def test_build_summary_png_creates_file(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            md_path = Path(tmp) / "draft_summary.md"
            md_path.write_text("# Draft: Test\n\n### 1. Name (app_name)\n- **Answer:** Jerrison\n- **Status:** filled\n")
            out_path = Path(tmp) / "draft_summary.png"

            result = subprocess.run(
                ["uv", "run", "python", "scripts/build_draft_summary.py", str(md_path), "-o", str(out_path)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out_path.exists())
            self.assertGreater(out_path.stat().st_size, 0)
```

- [ ] **Step 3: Implement `build_draft_summary.py`**

Create `scripts/build_draft_summary.py` — a CLI tool that reads `draft_summary.md` and renders a formatted PNG using Pillow. Key features:
- Green badges for "filled" fields, red for "unfilled"
- Company/role header at top
- Monospace font for field names, proportional for answers
- ~800px wide, auto-height based on field count
- Fallback: if Pillow is unavailable, print warning and skip

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_draft_manager.py::DraftSummaryPngTests -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/build_draft_summary.py pyproject.toml tests/test_draft_manager.py
git commit -m "feat(draft): add build_draft_summary.py for PNG generation"
```

---

### Task 11: TUI — Draft filter tab and detail view

**Files:**
- Modify: `scripts/job_tui.py:268,619`

- [ ] **Step 1: Add "Drafts" filter to QueueScreen**

In `QueueScreen` (~line 268), add a "Drafts" button/tab to the existing filter bar that filters the job list to `status = 'draft'` only.

- [ ] **Step 2: Enhance JobDetailScreen for draft actions**

In `JobDetailScreen` (~line 619), when the job status is `"draft"`:
- Show `draft_summary.md` content in a scrollable text area
- Add action buttons: **Approve** (calls `approve_job`), **Reject** (sets `needs_manual`), **Regenerate** (calls `regenerate_job`), **Edit** (opens `$EDITOR`)
- Display `draft_summary.png` and `{board}_autofill_pre_submit.png` using terminal image rendering

- [ ] **Step 3: Add terminal image rendering**

Add an image widget that:
1. Tries `rich-pixels` for ANSI half-block rendering (primary)
2. Falls back to displaying the file path with "Open" button that calls `open` (macOS) or `xdg-open`

Add `rich-pixels` to `pyproject.toml` dependencies (or make it optional with graceful fallback).

- [ ] **Step 4: Manual test — launch TUI and verify draft tab**

Run: `uv run python scripts/job_tui.py`
Verify: Drafts tab appears, draft jobs are filterable, detail view shows summary + actions.

- [ ] **Step 5: Commit**

```bash
git add scripts/job_tui.py pyproject.toml
git commit -m "feat(draft): TUI draft filter tab, detail view, image rendering"
```

---

## Chunk 5: Web Interface

### Task 12: FastAPI backend

**Files:**
- Create: `scripts/draft_web.py`
- Modify: `pyproject.toml`
- Test: `tests/test_draft_web.py`

- [ ] **Step 1: Add optional web dependencies to pyproject.toml**

```toml
[project.optional-dependencies]
fetchers = ["scrapling[fetchers]"]
web = ["fastapi>=0.115", "uvicorn>=0.34", "jinja2>=3.1", "markdown>=3.7"]
```

- [ ] **Step 2: Write failing test for list endpoint**

```python
# tests/test_draft_web.py
import unittest

class DraftWebTests(unittest.TestCase):
    def test_list_drafts_endpoint(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi not installed")

        from draft_web import create_app
        client = TestClient(create_app())
        resp = client.get("/api/drafts")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), list)
```

- [ ] **Step 3: Implement `draft_web.py` with FastAPI**

Create `scripts/draft_web.py`:

```python
"""FastAPI app for draft review — local server + optional tunnel."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def create_app() -> FastAPI:
    app = FastAPI(title="Job Application Draft Review")

    def _open_db():
        import sqlite3
        conn = sqlite3.connect(PROJECT_ROOT / "jobs.db")
        conn.row_factory = sqlite3.Row
        return conn

    @app.get("/api/drafts")
    def list_drafts():
        conn = _open_db()
        rows = conn.execute(
            "SELECT id, company, role_title, board, output_dir, updated_at "
            "FROM jobs WHERE status = 'draft' ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/drafts/{job_id}")
    def get_draft(job_id: int):
        conn = _open_db()
        row = conn.execute("SELECT * FROM jobs WHERE id = ? AND status = 'draft'", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Draft not found")
        job = dict(row)
        out_dir = Path(job["output_dir"])
        summary_path = out_dir / "draft_summary.md"
        job["summary_md"] = summary_path.read_text() if summary_path.exists() else ""
        return job

    @app.get("/api/drafts/{job_id}/images/{image_type}")
    def get_image(job_id: int, image_type: str):
        conn = _open_db()
        row = conn.execute("SELECT output_dir, board FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        out_dir = Path(row["output_dir"])
        if image_type == "summary":
            path = out_dir / "draft_summary.png"
        elif image_type == "pre-submit":
            path = next(out_dir.glob("submit/*_pre_submit.png"), None)
        else:
            raise HTTPException(400, "Invalid image type")
        if not path or not path.exists():
            raise HTTPException(404, "Image not found")
        return FileResponse(path, media_type="image/png")

    @app.post("/api/drafts/{job_id}/approve")
    def approve_draft(job_id: int):
        from pipeline_orchestrator import approve_job
        conn = _open_db()
        if approve_job(conn, job_id):
            # Trigger just the submit phase, not the full pipeline
            row = conn.execute("SELECT output_dir FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row:
                import subprocess, threading
                out_dir = row["output_dir"]
                submit_script = str(Path(__file__).resolve().parent / "submit_application.py")
                def _run_submit():
                    result = subprocess.run(
                        ["uv", "run", "python", submit_script, out_dir, "--submit"],
                        capture_output=True,
                    )
                    from job_db import update_status
                    c = _open_db()
                    if result.returncode == 0:
                        update_status(c, job_id, "submitted")
                    else:
                        update_status(c, job_id, "failed", error_message="Submit failed after approval")
                threading.Thread(target=_run_submit, daemon=True).start()
            return {"status": "approved", "job_id": job_id}
        raise HTTPException(409, "Job is not in draft status")

    @app.post("/api/drafts/{job_id}/reject")
    def reject_draft(job_id: int):
        from job_db import update_status
        conn = _open_db()
        update_status(conn, job_id, "needs_manual")
        return {"status": "rejected", "job_id": job_id}

    @app.post("/api/drafts/{job_id}/regenerate")
    def regenerate_draft(job_id: int):
        from pipeline_orchestrator import regenerate_job
        conn = _open_db()
        if regenerate_job(conn, job_id):
            return {"status": "regenerating", "job_id": job_id}
        raise HTTPException(409, "Job is not in draft status")

    @app.put("/api/drafts/{job_id}/overrides")
    def update_overrides(job_id: int, overrides: dict):
        conn = _open_db()
        row = conn.execute("SELECT output_dir FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        out_dir = Path(row["output_dir"])
        overrides_path = out_dir / "draft_overrides.json"
        existing = {}
        if overrides_path.exists():
            existing = json.loads(overrides_path.read_text())
        existing.update(overrides)
        overrides_path.write_text(json.dumps(existing, indent=2))
        return {"status": "updated", "overrides": existing}

    # HTML frontend — serve Jinja2 templates
    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        drafts = list_drafts()
        # Inline HTML for simplicity — move to templates/ for production
        rows_html = "".join(
            f'<tr><td><a href="/drafts/{d["id"]}">{d["id"]}</a></td>'
            f'<td>{d.get("company","?")}</td>'
            f'<td>{d.get("role_title","?")}</td>'
            f'<td>{d.get("board","?")}</td>'
            f'<td>{d.get("updated_at","?")}</td></tr>'
            for d in drafts
        )
        return f"""<!DOCTYPE html><html><head><title>Draft Review</title>
        <style>body{{font-family:system-ui;max-width:1200px;margin:0 auto;padding:2rem}}
        table{{width:100%;border-collapse:collapse}}th,td{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}</style>
        </head><body><h1>Application Drafts</h1>
        <table><tr><th>ID</th><th>Company</th><th>Role</th><th>Board</th><th>Updated</th></tr>
        {rows_html}</table></body></html>"""

    @app.get("/drafts/{job_id}", response_class=HTMLResponse)
    def draft_detail_page(job_id: int):
        draft = get_draft(job_id)
        import markdown
        summary_html = markdown.markdown(draft.get("summary_md", ""))
        return f"""<!DOCTYPE html><html><head><title>Draft #{job_id}</title>
        <style>body{{font-family:system-ui;max-width:1400px;margin:0 auto;padding:2rem}}
        .layout{{display:grid;grid-template-columns:1fr 1fr;gap:2rem}}
        .actions{{margin:1rem 0}}button{{padding:8px 16px;margin-right:8px;cursor:pointer}}
        img{{max-width:100%;border:1px solid #ddd;border-radius:4px}}</style>
        </head><body>
        <h1>{draft.get('company','?')} — {draft.get('role_title','?')}</h1>
        <div class="actions">
          <button onclick="fetch('/api/drafts/{job_id}/approve',{{method:'POST'}}).then(()=>location.reload())">Approve</button>
          <button onclick="fetch('/api/drafts/{job_id}/reject',{{method:'POST'}}).then(()=>location.reload())">Reject</button>
          <button onclick="fetch('/api/drafts/{job_id}/regenerate',{{method:'POST'}}).then(()=>location.reload())">Regenerate</button>
        </div>
        <div class="layout">
          <div>{summary_html}</div>
          <div>
            <h3>Pre-Submit Screenshot</h3>
            <img src="/api/drafts/{job_id}/images/pre-submit" onerror="this.outerHTML='<p>No screenshot</p>'">
            <h3>Draft Summary</h3>
            <img src="/api/drafts/{job_id}/images/summary" onerror="this.outerHTML='<p>No summary image</p>'">
          </div>
        </div></body></html>"""

    return app
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_draft_web.py -v`
Expected: PASS (or skip if fastapi not installed)

- [ ] **Step 5: Commit**

```bash
git add scripts/draft_web.py tests/test_draft_web.py pyproject.toml
git commit -m "feat(draft): add FastAPI web interface for draft review"
```

---

## Chunk 6: Documentation + Integration Tests

### Task 13: Integration test — full draft lifecycle

**Files:**
- Test: `tests/test_draft_manager.py`

- [ ] **Step 1: Write integration test**

```python
class DraftLifecycleTests(unittest.TestCase):
    def test_generate_edit_regenerate_approve(self):
        """Full lifecycle: generate draft → edit answer → classify → apply overrides → regenerate."""
        from draft_manager import generate_draft_summary, classify_draft_edits, apply_draft_edits

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()

            report = {
                "board": "ashby",
                "fields": [
                    {"field_name": "app_name", "label": "Name", "kind": "text", "required": True, "status": "filled", "value": "Jerrison Li", "source": "master_resume.md"},
                    {"field_name": "app_salary", "label": "Salary", "kind": "text", "required": True, "status": "filled", "value": "Yes", "source": "deterministic_override"},
                    {"field_name": "survey_pronouns", "label": "Pronouns", "kind": "choice", "required": False, "status": "unfilled", "value": None, "source": None},
                ],
            }
            (submit_dir / "ashby_autofill_report.json").write_text(json.dumps(report))
            meta = {"company": "Bubble", "role_title": "Group PM", "board": "ashby"}

            # Phase 1: Generate draft
            generate_draft_summary(out_dir, submit_dir, meta)
            self.assertTrue((out_dir / "draft_summary.md").exists())
            self.assertTrue((out_dir / "draft_summary.original.md").exists())
            self.assertTrue((out_dir / "draft_status.json").exists())

            # Phase 2: User edits
            md = (out_dir / "draft_summary.md").read_text()
            md = md.replace("- **Answer:** Yes", "- **Answer:** Open to negotiation")
            md = md.replace("- **Answer:** —", "- **Answer:** He/Him")
            (out_dir / "draft_summary.md").write_text(md)

            # Phase 3: Classify edits
            original = (out_dir / "draft_summary.original.md").read_text()
            edited = (out_dir / "draft_summary.md").read_text()
            changes = classify_draft_edits(original, edited)
            self.assertEqual(len(changes), 2)

            salary_change = next(c for c in changes if c["field_key"] == "app_salary")
            self.assertEqual(salary_change["classification"], "wrong_answer")

            pronoun_change = next(c for c in changes if c["field_key"] == "survey_pronouns")
            self.assertEqual(pronoun_change["classification"], "missing_handler")

            # Phase 4: Apply edits
            result = apply_draft_edits(out_dir, changes)
            overrides = json.loads((out_dir / "draft_overrides.json").read_text())
            self.assertEqual(overrides["app_salary"], "Open to negotiation")
            self.assertEqual(overrides["survey_pronouns"], "He/Him")
            self.assertIn("fix_report_path", result)  # fix report generated for missing_handler
```

- [ ] **Step 2: Run integration test**

Run: `uv run python -m pytest tests/test_draft_manager.py::DraftLifecycleTests -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_draft_manager.py
git commit -m "test(draft): add full lifecycle integration test"
```

---

### Task 14: Update CLAUDE.md, AGENTS.md, GEMINI.md

**Files:**
- Modify: `CLAUDE.md`
- Modify: `AGENTS.md`
- Modify: `GEMINI.md`

- [ ] **Step 1: Add draft mode section to CLAUDE.md**

Add after "TUI, Worker, and Job Queue Architecture":

```markdown
## Draft Mode

Pipeline stops at `draft` status after autofill (form filled, screenshots taken) but before clicking submit. User reviews via CLI (`job-assets draft list|review|approve|reject|regenerate`), TUI (Drafts filter tab + detail view), web (`job-assets draft serve`), or LLM conversation.

**Key files:** `draft_manager.py` (summary generation, diff classification, override management), `build_draft_summary.py` (PNG rendering), `draft_web.py` (FastAPI server).

**Draft artifacts** (in role output root): `draft_summary.md` (editable), `draft_summary.original.md` (immutable for diffing), `draft_summary.png`, `draft_status.json`, `draft_overrides.json`, `draft_fix_report.md`.

**Edit-to-fix loop:** User edits `draft_summary.md` → `draft_manager.py` auto-classifies changes (missing_handler, wrong_answer) → generates fix report → `auto_fix()` applies generalized code fixes → pipeline regenerates with research cache preserved and `draft_overrides.json` applied.

**Flags:** `--draft` on `submit_application.py` (fill + stop). `--auto-submit` on worker/orchestrator (bypass draft). Stale drafts (>7 days, configurable via `JOB_ASSETS_DRAFT_TTL_DAYS`) auto-expire to `needs_manual`.
```

- [ ] **Step 2: Add draft mode instructions to AGENTS.md**

Add to the Shared Inputs section describing the draft review flow for LLM runtime: after pipeline generates a draft, display Q&A summary inline, show pre-submit screenshot, offer approve/reject/edit/regenerate actions.

- [ ] **Step 3: Sync GEMINI.md**

```bash
cp AGENTS.md GEMINI.md
```

- [ ] **Step 4: Verify CI test**

Run: `uv run python -m pytest tests/test_ci_workflow.py -v -k agents_md`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `uv run python -m pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md AGENTS.md GEMINI.md
git commit -m "docs(draft): document draft mode in CLAUDE.md and AGENTS.md/GEMINI.md"
```

---

### Task 15: Final verification

- [ ] **Step 1: Run full test suite**

Run: `uv run python -m pytest tests/ -v`
Expected: All pass including new draft tests

- [ ] **Step 2: Verify draft CLI commands parse**

Run: `uv run python bin/job-assets draft list`
Expected: "No drafts pending review." (or list of drafts if any exist)

- [ ] **Step 3: Verify web server starts** (if web deps installed)

Run: `uv pip install -e ".[web]" && uv run python bin/job-assets draft serve &`
Then: `curl http://localhost:8420/api/drafts`
Expected: `[]` (empty list)
Kill the server after verification.

- [ ] **Step 4: Final commit with any remaining fixes**

```bash
git add -A && git commit -m "feat(draft): application draft mode — complete implementation"
git push
```
