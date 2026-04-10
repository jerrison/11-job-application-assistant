# Validator And Draft Summary Test Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add direct regression coverage for the repo validator scripts and draft summary renderer, fixing any defects those tests expose.

**Architecture:** Add focused unit-style tests around script-level helpers and CLI paths instead of broad integration coverage. Keep production changes minimal and only in response to red tests that reveal real defects in import normalization, markdown link scanning, or draft summary parsing.

**Tech Stack:** Python 3.12+, pytest, unittest.mock, tempfile/pathlib, Pillow

---

### Task 1: Harden `check_architecture.py`

**Files:**
- Modify: `tests/test_check_architecture.py`
- Modify: `scripts/check_architecture.py`

- [ ] **Step 1: Write the failing test**

```python
def test_check_architecture_flags_package_qualified_board_imports(tmp_path, monkeypatch):
    module = load_module("check_architecture_runtime", "scripts/check_architecture.py")
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "autofill_common.py").write_text("import scripts.autofill_greenhouse\n", encoding="utf-8")
    (scripts_dir / "autofill_pipeline.py").write_text("", encoding="utf-8")
    (scripts_dir / "application_submit_common.py").write_text("", encoding="utf-8")
    (scripts_dir / "autofill_greenhouse.py").write_text("", encoding="utf-8")

    monkeypatch.setattr(module, "SCRIPTS_DIR", scripts_dir)
    monkeypatch.setattr(module, "BOARD_MODULES", {"autofill_greenhouse"})
    monkeypatch.setattr(
        module,
        "FORBIDDEN_IMPORTS",
        {
            "autofill_common": {"autofill_pipeline", "autofill_greenhouse"},
            "autofill_pipeline": {"autofill_greenhouse"},
            "application_submit_common": {"autofill_greenhouse"},
            "autofill_greenhouse": set(),
        },
    )

    violations = module.check_architecture()

    assert any("autofill_common imports autofill_greenhouse" in violation for violation in violations)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_check_architecture.py -v`
Expected: FAIL because `scripts.autofill_greenhouse` is normalized to `scripts`, so no violation is reported.

- [ ] **Step 3: Write minimal implementation**

```python
def _import_root(name: str) -> str:
    parts = name.split(".")
    if len(parts) > 1 and parts[0] == "scripts":
        return parts[1]
    return parts[0]
```

Apply `_import_root(...)` to both `ast.Import` and `ast.ImportFrom` handling in `get_imports(...)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_check_architecture.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_check_architecture.py scripts/check_architecture.py
git commit -m "test: harden architecture import validation"
```

### Task 2: Harden `check_agent_docs.py`

**Files:**
- Modify: `tests/test_check_agent_docs.py`
- Modify: `scripts/check_agent_docs.py`

- [ ] **Step 1: Write the failing test**

```python
def test_check_links_ignores_tilde_fenced_code_blocks(tmp_path, monkeypatch):
    module = load_module("check_agent_docs_runtime", "scripts/check_agent_docs.py")
    existing = tmp_path / "real.md"
    existing.write_text("# real\n", encoding="utf-8")
    markdown = tmp_path / "sample.md"
    markdown.write_text(
        "~~~md\n[example](missing.md)\n~~~\n\n[real](real.md)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "_collect_md_files", lambda: [markdown])

    passes, failures = module.check_links()

    assert failures == []
    assert passes == ["All 1 internal doc links resolve"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_check_agent_docs.py -v`
Expected: FAIL because the current fenced-code regex only strips triple-backtick fences.

- [ ] **Step 3: Write minimal implementation**

```python
FENCED_CODE_RE = re.compile(
    r"^(?:```|~~~).*?^(?:```|~~~)\s*$",
    re.MULTILINE | re.DOTALL,
)
```

Keep the change scoped to fenced code stripping so markdown link validation behavior stays otherwise unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_check_agent_docs.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_check_agent_docs.py scripts/check_agent_docs.py
git commit -m "test: harden agent doc link validation"
```

### Task 3: Harden `build_draft_summary.py`

**Files:**
- Modify: `tests/test_build_draft_summary.py`
- Modify: `scripts/build_draft_summary.py`

- [ ] **Step 1: Write the failing test**

```python
def test_parse_md_preserves_multiline_answer_blocks():
    module = load_module("build_draft_summary_runtime", "scripts/build_draft_summary.py")
    data = module._parse_md(
        "# Draft: Role — Company\n"
        "**Board:** ashby | **Generated:** now\n\n"
        "## Application Answers\n\n"
        "### 1. Why this role? (why_role)\n"
        "- **Kind:** text | **Required:** yes\n"
        "- **Answer:** First line\n"
        "  Second line\n"
        "- **Status:** filled\n"
    )

    assert data["fields"][0]["answer"] == "First line\nSecond line"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_build_draft_summary.py -v`
Expected: FAIL because the current regex only captures the first line after `**Answer:**`.

- [ ] **Step 3: Write minimal implementation**

```python
def _extract_detail_block(block: str, label: str) -> str | None:
    # Find `- **Label:** ...` and keep indented continuation lines until the next
    # markdown detail bullet or field header.
```

Use the helper for `Answer` and `Linked Resource` extraction inside `_parse_md(...)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_build_draft_summary.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_build_draft_summary.py scripts/build_draft_summary.py
git commit -m "test: cover multiline draft summary rendering"
```

### Task 4: Run Focused And Full Verification

**Files:**
- Modify: `tests/test_check_architecture.py`
- Modify: `tests/test_check_agent_docs.py`
- Modify: `tests/test_build_draft_summary.py`
- Modify: `scripts/check_architecture.py`
- Modify: `scripts/check_agent_docs.py`
- Modify: `scripts/build_draft_summary.py`

- [ ] **Step 1: Run targeted regression tests**

```bash
uv run python -m pytest \
  tests/test_check_architecture.py \
  tests/test_check_agent_docs.py \
  tests/test_build_draft_summary.py -v
```

- [ ] **Step 2: Run the repo verification suite**

```bash
uv run ruff check scripts/ tests/
uv run python scripts/check_architecture.py
uv run python scripts/check_agent_docs.py
uv run python scripts/sync_agent_files.py --check
uv run python -m pytest tests/ -v
```

- [ ] **Step 3: Confirm clean status**

```bash
git status --short
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_check_architecture.py tests/test_check_agent_docs.py tests/test_build_draft_summary.py \
  scripts/check_architecture.py scripts/check_agent_docs.py scripts/build_draft_summary.py
git commit -m "test: harden validator and draft summary coverage"
```
