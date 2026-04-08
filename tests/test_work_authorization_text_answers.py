import importlib.util
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PETITION_PROMPT = (
    "Will you now or in the future require our company to file a petition or application "
    "for employment-based immigration status on your behalf to begin or continue employment "
    "with our company?"
)


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_bamboohr_text_helper_uses_sponsorship_answer_for_employment_based_status_prompt():
    mod = load_module("autofill_bamboohr", "scripts/autofill_bamboohr.py")
    profile = mod.parse_application_profile((PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8"))

    answer = mod._answer_from_classifier_text(PETITION_PROMPT, profile)

    assert answer == profile.sponsorship_answer


def test_icims_classifier_uses_sponsorship_answer_for_text_employment_based_status_prompt():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    profile = mod.parse_application_profile((PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8"))

    answer, source = mod._answer_from_classifier(PETITION_PROMPT, profile, kind="textarea")

    assert answer == profile.sponsorship_answer
    assert source == "application_profile.md"


def test_icims_classifier_keeps_yes_no_for_select_employment_based_status_prompt():
    mod = load_module("autofill_icims", "scripts/autofill_icims.py")
    profile = mod.parse_application_profile((PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8"))

    answer, source = mod._answer_from_classifier(PETITION_PROMPT, profile, kind="select")

    assert answer == "No"
    assert source == "application_profile.md"


def test_phenom_text_helper_uses_sponsorship_answer_for_employment_based_status_prompt():
    mod = load_module("autofill_phenom", "scripts/autofill_phenom.py")
    profile = mod.parse_application_profile((PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8"))

    result = mod._try_deterministic_answer(PETITION_PROMPT.casefold(), {"type": "textarea"}, profile)

    assert result is not None
    assert result["value"] == profile.sponsorship_answer
    assert result["source"] == "application_profile.md"


def test_phenom_select_helper_keeps_yes_no_for_employment_based_status_prompt():
    mod = load_module("autofill_phenom", "scripts/autofill_phenom.py")
    profile = mod.parse_application_profile((PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8"))

    result = mod._try_deterministic_answer(PETITION_PROMPT.casefold(), {"type": "select"}, profile)

    assert result is not None
    assert result["value"] == "No"
    assert result["source"] == "application_profile.md"
