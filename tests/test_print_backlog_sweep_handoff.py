from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    assert path.exists(), f"Missing {relative_path}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop(name, None)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write_tsv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "\t".join(fieldnames)
    body = ["\t".join(row.get(field, "") for field in fieldnames) for row in rows]
    path.write_text("\n".join([header, *body]) + "\n", encoding="utf-8")
    return path


def test_build_handoff_prompt_includes_active_phase_progress(tmp_path: Path):
    module = load_module("print_backlog_sweep_handoff", "scripts/print_backlog_sweep_handoff.py")

    phase2_snapshot = write_tsv(
        tmp_path / "phase2-snapshot.tsv",
        ["id", "company", "role_title", "board", "output_dir"],
        [
            {"id": "11", "company": "Acme", "role_title": "PM", "board": "greenhouse", "output_dir": "output/acme/pm"},
            {"id": "12", "company": "Beta", "role_title": "PM", "board": "ashby", "output_dir": "output/beta/pm"},
        ],
    )
    phase2_results = write_tsv(
        tmp_path / "phase2-results.tsv",
        ["id", "outcome"],
        [
            {"id": "11", "outcome": "fixed_redrafted"},
            {"id": "11", "outcome": "nad_created"},
        ],
    )
    phase3_snapshot = write_tsv(
        tmp_path / "phase3-snapshot.tsv",
        ["id", "company", "role_title", "board", "output_dir"],
        [{"id": "21", "company": "Gamma", "role_title": "Principal PM", "board": "lever", "output_dir": "output/gamma/pm"}],
    )
    phase3_results = write_tsv(
        tmp_path / "phase3-results.tsv",
        ["id", "outcome"],
        [{"id": "21", "outcome": "reviewed_ready"}],
    )
    manifest_path = tmp_path / ".context" / "compound-engineering" / "todos" / "current_backlog_sweep.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "2026-04-08T06-23-18Z",
                "repo_state": {"dirty_paths_count": 7},
                "phase2_snapshot": str(phase2_snapshot),
                "phase2_results": str(phase2_results),
                "phase3_snapshot": str(phase3_snapshot),
                "phase3_results": str(phase3_results),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    prompt = module.build_handoff_prompt(manifest_path)

    assert "$using-superpowers" in prompt
    assert "Do not start a new sweep run" in prompt
    assert "Current sweep run id: `2026-04-08T06-23-18Z`." in prompt
    assert "Phase 2 snapshot: " in prompt
    assert "(2 rows)" in prompt
    assert "Phase 2 results ledger: " in prompt
    assert "1 latest covered rows; outcomes: nad_created=1" in prompt
    assert "Phase 3 results ledger: " in prompt
    assert "1 latest covered rows; outcomes: reviewed_ready=1" in prompt
    assert "Default to repair, not description." in prompt
    assert "Creating or updating a Linear issue does not count" in prompt
    assert "uv run python scripts/check_backlog_sweep.py --active" in prompt
    assert "uv run python scripts/print_backlog_sweep_handoff.py --active" in prompt
