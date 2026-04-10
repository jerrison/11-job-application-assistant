import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_parse_md_preserves_multiline_answer_blocks():
    module = load_module("build_draft_summary_runtime", "scripts/build_draft_summary.py")
    data = module._parse_md(
        "# Draft: Role \u2014 Company\n"
        "**Board:** ashby | **Generated:** now\n\n"
        "## Application Answers\n\n"
        "### 1. Why this role? (why_role)\n"
        "- **Kind:** text | **Required:** yes\n"
        "- **Answer:** First line\n"
        "  Second line\n"
        "- **Status:** filled\n",
    )

    assert data["fields"][0]["answer"] == "First line\nSecond line"


def test_parse_md_preserves_multiline_linked_resource_blocks():
    module = load_module("build_draft_summary_links_runtime", "scripts/build_draft_summary.py")
    data = module._parse_md(
        "# Draft: Role \u2014 Company\n"
        "**Board:** ashby | **Generated:** now\n\n"
        "## Application Answers\n\n"
        "### 1. Portfolio (portfolio)\n"
        "- **Kind:** text | **Required:** no\n"
        "- **Answer:** Included below\n"
        "- **Linked Resource:** https://example.test/resource\n"
        "  with extra detail\n"
        "- **Status:** filled\n",
    )

    assert data["fields"][0]["linked_resource"] == "https://example.test/resource\nwith extra detail"


def test_render_png_writes_output_for_multiline_content(tmp_path):
    module = load_module("build_draft_summary_render_runtime", "scripts/build_draft_summary.py")
    data = module._parse_md(
        "# Draft: Role \u2014 Company\n"
        "**Board:** ashby | **Generated:** now\n\n"
        "## Answer Refresh\n\n"
        "- **Status:** fresh\n"
        "- **Generated Answers:** 1\n\n"
        "## Application Answers\n\n"
        "### 1. Why this role? (why_role)\n"
        "- **Kind:** text | **Required:** yes\n"
        "- **Answer:** First line\n"
        "  Second line\n"
        "- **Status:** filled\n",
    )

    output_path = tmp_path / "draft_summary.png"
    module.render_png(data, output_path)

    assert output_path.exists()
    assert output_path.stat().st_size > 0


def test_main_exits_when_input_markdown_is_missing(monkeypatch):
    module = load_module("build_draft_summary_main_runtime", "scripts/build_draft_summary.py")
    missing = PROJECT_ROOT / "missing-draft-summary.md"
    monkeypatch.setattr(sys, "argv", ["build_draft_summary.py", str(missing)])

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert exc.value.code == 1
