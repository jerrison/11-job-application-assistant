import importlib.util
import sys
from pathlib import Path

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


def _patch_repo_paths(module, monkeypatch, root: Path) -> None:
    docs_root = root / "docs"
    exec_plans_root = docs_root / "exec-plans"
    monkeypatch.setattr(module, "PROJECT_ROOT", root)
    monkeypatch.setattr(module, "AGENTS_MD", root / "AGENTS.md")
    monkeypatch.setattr(module, "INDEX_MD", docs_root / "INDEX.md")
    monkeypatch.setattr(module, "SYNC_SCRIPT", root / "scripts" / "sync_agent_files.py")
    monkeypatch.setattr(module, "EXEC_PLANS_ROOT", exec_plans_root)
    monkeypatch.setattr(module, "EXEC_PLANS_ACTIVE", exec_plans_root / "active")
    monkeypatch.setattr(module, "EXEC_PLANS_COMPLETED", exec_plans_root / "completed")
    monkeypatch.setattr(module, "EXEC_PLANS_README", exec_plans_root / "README.md")
    monkeypatch.setattr(module, "PLAN_TEMPLATE", docs_root / "PLAN_TEMPLATE.md")


def test_check_links_ignores_tilde_fenced_code_blocks(tmp_path, monkeypatch):
    module = load_module("check_agent_docs_runtime", "scripts/check_agent_docs.py")
    existing = tmp_path / "real.md"
    existing.write_text("# real\n", encoding="utf-8")
    markdown = tmp_path / "sample.md"
    markdown.write_text(
        "~~~md\n"
        "[example](missing.md)\n"
        "~~~\n\n"
        "[real](real.md)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(module, "_collect_md_files", lambda: [markdown])

    passes, failures = module.check_links()

    assert failures == []
    assert passes == ["All 1 internal doc links resolve"]


def test_size_index_and_plan_checks_pass_for_valid_repo_layout(tmp_path, monkeypatch):
    module = load_module("check_agent_docs_paths_runtime", "scripts/check_agent_docs.py")
    docs_root = tmp_path / "docs"
    docs_root.mkdir()
    exec_plans_root = docs_root / "exec-plans"
    (exec_plans_root / "active").mkdir(parents=True)
    (exec_plans_root / "completed").mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("line 1\nline 2\n", encoding="utf-8")
    (tmp_path / "ARCHITECTURE.md").write_text("# Architecture\n", encoding="utf-8")
    (docs_root / "INDEX.md").write_text("[Architecture](../ARCHITECTURE.md)\n", encoding="utf-8")
    (exec_plans_root / "README.md").write_text("# Exec plans\n", encoding="utf-8")
    (docs_root / "PLAN_TEMPLATE.md").write_text(
        "\n".join(module.PLAN_TEMPLATE_SECTIONS) + "\n",
        encoding="utf-8",
    )
    _patch_repo_paths(module, monkeypatch, tmp_path)

    assert module.check_size()[1] == []
    assert module.check_index()[1] == []
    assert module.check_plans()[1] == []


def test_main_reports_selected_check_failures(monkeypatch, capsys):
    module = load_module("check_agent_docs_main_runtime", "scripts/check_agent_docs.py")
    monkeypatch.setitem(module.CHECKS, "size", lambda: ([], ["too many lines"]))
    monkeypatch.setattr(sys, "argv", ["check_agent_docs.py", "--check", "size"])

    assert module.main() == 1
    assert "too many lines" in capsys.readouterr().out
