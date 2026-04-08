import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    scripts_dir = str(path.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_compact_resume_content_shortens_summary_and_older_roles():
    compact_resume_content = load_module("compact_resume_content", "scripts/compact_resume_content.py")

    data = {
        "summary": "Sentence one. Sentence two with extra detail.",
        "positions": {
            "moodys": [
                {"bold": "Kept in full, ", "text": "because the newest role should stay detailed."},
            ],
            "kyte": [
                {"bold": "Built the risk engine, ", "text": "cutting losses and growing revenue."},
            ],
            "tmobile": [
                {"bold": "Unlocked SMB growth, ", "text": "by redesigning onboarding."},
            ],
        },
    }

    changed, actions = compact_resume_content.compact_resume_content(data)

    assert changed is True
    assert data["summary"] == "Sentence one."
    assert data["positions"]["moodys"][0]["text"] == "because the newest role should stay detailed."
    assert data["positions"]["kyte"][0]["bold"] == "Built the risk engine. "
    assert data["positions"]["kyte"][0]["text"] == ""
    assert data["positions"]["tmobile"][0]["bold"] == "Unlocked SMB growth. "
    assert data["positions"]["tmobile"][0]["text"] == ""
    assert actions == [
        "shortened summary to one sentence",
        "compacted older-role bullets for kyte",
        "compacted older-role bullets for tmobile",
    ]


def test_compact_resume_content_keeps_first_populated_role_detailed_even_if_moodys_missing():
    compact_resume_content = load_module("compact_resume_content", "scripts/compact_resume_content.py")

    data = {
        "summary": "Only one sentence already.",
        "positions": {
            "moodys": [],
            "kyte": [
                {"bold": "Keep this role detailed, ", "text": "because it is the newest populated role."},
            ],
            "lyft": [
                {"bold": "Older role gets compacted, ", "text": "to save vertical space."},
            ],
        },
    }

    changed, actions = compact_resume_content.compact_resume_content(data)

    assert changed is True
    assert data["positions"]["kyte"][0]["text"] == "because it is the newest populated role."
    assert data["positions"]["lyft"][0]["bold"] == "Older role gets compacted. "
    assert data["positions"]["lyft"][0]["text"] == ""
    assert actions == ["compacted older-role bullets for lyft"]
