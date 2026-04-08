import importlib.util
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_try_deterministic_answer_prefers_current_city_option_for_hybrid_location_prompt():
    mod = load_module("autofill_phenom", "scripts/autofill_phenom.py")
    profile = mod.parse_application_profile((PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8"))

    result = mod._try_deterministic_answer(
        "this is a hybrid position in san francisco. please select which applies:",
        {
            "type": "select",
            "options": [
                "You currently live in San Francisco",
                "You are looking to relocate to San Francisco",
            ],
        },
        profile,
    )

    assert result is not None
    assert result["value"] == "You currently live in San Francisco"
    assert result["source"] == "shared_positive_fit_policy"


def test_detect_phenom_auth_result_reports_auth_guarded_sign_in_gate():
    mod = load_module("autofill_phenom", "scripts/autofill_phenom.py")

    class _BodyLocator:
        def inner_text(self, timeout=5000):
            del timeout
            return (
                "Adobe Careers\n"
                "Sign In\n"
                "Create account\n"
                "Continue with Google\n"
                "Continue with LinkedIn\n"
            )

    class _Page:
        url = "https://careers.adobe.com/us/en/apply?jobSeqNo=ADOBUSR165601EXTERNALENUS"

        def locator(self, selector):
            assert selector == "body"
            return _BodyLocator()

    result = mod._detect_phenom_auth_result(
        _Page(),
        {
            "job_url": "https://careers.adobe.com/us/en/job/ADOBUSR165601EXTERNALENUS/Principal-Product-Manager-AI-Innovation",
            "company": "Adobe",
            "job_title": "Principal Product Manager, AI Innovation",
        },
    )

    assert result is not None
    assert result["status"] == "skipped_auth"
    assert result["failure_type"] == "auth_guarded"
    assert result["auth_state"] == "sign_in_gate"


def test_best_select_option_label_picks_available_fallback_without_retrying_missing_labels():
    mod = load_module("autofill_phenom", "scripts/autofill_phenom.py")

    result = mod._best_select_option_label(
        [
            "Please Select",
            "Adobe Source",
            "Contingent Worker",
            "External Organizations",
            "Job Boards",
            "Social Media",
        ],
        [
            "Corporate website",
            "Company Website",
            "Career Site",
            "Job Boards",
            "Social Media",
        ],
    )

    assert result == "Job Boards"


def test_best_select_option_label_supports_case_insensitive_prefix_matching():
    mod = load_module("autofill_phenom", "scripts/autofill_phenom.py")

    result = mod._best_select_option_label(
        [
            "Please Select",
            "Hispanic or Latino (United States of America)",
        ],
        [
            "hispanic or latino",
        ],
    )

    assert result == "Hispanic or Latino (United States of America)"


def test_candidate_name_parts_preserve_multi_word_last_names():
    mod = load_module("autofill_phenom", "scripts/autofill_phenom.py")

    assert mod._candidate_name_parts("Candidate Name") == ("Candidate", "Name")
    assert mod._candidate_name_parts("Mary Jane Watson") == ("Mary", "Jane Watson")


def test_terminal_apply_submit_result_from_body_detects_captcha_error():
    mod = load_module("autofill_phenom", "scripts/autofill_phenom.py")

    result = mod._terminal_apply_submit_result_from_body(
        '{"applySubmit":{"STATUS":"success","response":{"status":"failure","statusCode":"captcha-error"}}}'
    )

    assert result == {
        "status": "skipped_captcha",
        "failure_type": "skipped_captcha",
        "message": "Phenom application is blocked by a captcha challenge before the next step can load.",
    }
