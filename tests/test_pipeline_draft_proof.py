import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_draft_review_state_blocks_empty_report_fields(tmp_path):
    proof = load_module("pipeline_draft_proof", "scripts/pipeline_draft_proof.py")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "workday_autofill_report.json").write_text(
        json.dumps({"fields": [], "unknown_questions": []}),
        encoding="utf-8",
    )
    (submit_dir / "workday_autofill_pre_submit.png").write_text("png", encoding="utf-8")

    state = proof.draft_review_state(out_dir, board_name="workday")

    assert state["state"] == "blocked"
    assert "empty autofill report" in state["reason"].casefold()
