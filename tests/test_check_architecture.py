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


def test_check_architecture_flags_package_qualified_board_imports(tmp_path, monkeypatch):
    module = load_module("check_architecture_runtime", "scripts/check_architecture.py")
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "autofill_common.py").write_text("import scripts.autofill_greenhouse\n", encoding="utf-8")
    (scripts_dir / "autofill_pipeline.py").write_text("", encoding="utf-8")
    (scripts_dir / "application_submit_common.py").write_text("", encoding="utf-8")
    (scripts_dir / "autofill_greenhouse.py").write_text("", encoding="utf-8")

    board_modules = {"autofill_greenhouse"}
    monkeypatch.setattr(module, "SCRIPTS_DIR", scripts_dir)
    monkeypatch.setattr(module, "BOARD_MODULES", board_modules)
    monkeypatch.setattr(
        module,
        "FORBIDDEN_IMPORTS",
        {
            "autofill_common": board_modules | {"autofill_pipeline"},
            "autofill_pipeline": set(board_modules),
            "application_submit_common": set(board_modules),
            "autofill_greenhouse": set(),
        },
    )

    violations = module.check_architecture()

    assert any("autofill_common imports autofill_greenhouse" in violation for violation in violations)


def test_main_reports_success_when_no_violations(monkeypatch, capsys):
    module = load_module("check_architecture_main_runtime", "scripts/check_architecture.py")
    monkeypatch.setattr(module, "check_architecture", list)

    assert module.main() == 0
    assert "Architecture validation passed" in capsys.readouterr().out
