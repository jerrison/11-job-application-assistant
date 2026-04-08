from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop(name, None)
    sys.modules[name] = module
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_repair_wave_fingerprint_changes_when_repair_relevant_file_changes(tmp_path: Path):
    module = load_module("sweep_repair_wave", "scripts/sweep_repair_wave.py")
    target = tmp_path / "scripts" / "controller.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("print('v1')\n", encoding="utf-8")

    first = module.compute_repair_wave_fingerprint([target])
    target.write_text("print('v2')\n", encoding="utf-8")
    second = module.compute_repair_wave_fingerprint([target])

    assert first != second


def test_repair_wave_fingerprint_ignores_generated_artifacts(tmp_path: Path):
    module = load_module("sweep_repair_wave", "scripts/sweep_repair_wave.py")
    tracked = tmp_path / "scripts" / "controller.py"
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text("print('stable')\n", encoding="utf-8")
    ignored = tmp_path / "output" / "example" / "draft_summary.png"
    ignored.parent.mkdir(parents=True, exist_ok=True)
    ignored.write_bytes(b"proof-a")

    first = module.compute_repair_wave_fingerprint([tracked, ignored], ignored_paths=[ignored])
    ignored.write_bytes(b"proof-b")
    second = module.compute_repair_wave_fingerprint([tracked, ignored], ignored_paths=[ignored])

    assert first == second
