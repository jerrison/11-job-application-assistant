import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from job_board_urls import looks_like_paycor_url
from submit_application import _board_for_url


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_board_for_url_detects_paycor_direct_host():
    url = "https://recruitingbypaycor.com/Recruiting/Jobs/1234"
    assert looks_like_paycor_url(url)
    assert _board_for_url(url) == "paycor"


def test_board_for_url_detects_paycor_job_introduction_host():
    url = "https://recruitingbypaycor.com/career/JobIntroduction.action?clientId=8a7885ac6b4436b7016b47f8f4001d75&jobId=8abfbf7e95b506480195eeec9d483228"
    assert looks_like_paycor_url(url)
    assert _board_for_url(url) == "paycor"


def test_build_payload_marks_board_paycor(tmp_path):
    autofill = load_module("autofill_paycor", "scripts/autofill_paycor.py")
    out_dir = tmp_path / "role"
    out_dir.mkdir()
    resume_path = out_dir / "resume.pdf"
    resume_path.write_bytes(b"%PDF-fake")

    with (
        mock.patch.object(autofill, "migrate_role_output_layout"),
        mock.patch.object(
            autofill,
            "load_meta",
            return_value={
                "jd_source": "https://recruitingbypaycor.com/Recruiting/Jobs/1234",
                "company": "fortress-information-security",
                "company_proper": "Fortress Information Security",
                "jd_title": "Senior Product Manager",
            },
        ),
        mock.patch.object(
            autofill,
            "parse_master_resume",
            return_value=SimpleNamespace(
                first_name="Jerrison",
                last_name="Li",
                email="jerrison@example.com",
                phone="555-555-5555",
                location="San Francisco, CA",
                linkedin="https://www.linkedin.com/in/jerrisonli/",
                website="https://jerrison.li",
            ),
        ),
        mock.patch.object(
            autofill,
            "parse_application_profile",
            return_value=SimpleNamespace(
                location="San Francisco, CA",
                linkedin="https://www.linkedin.com/in/jerrisonli/",
                website="https://jerrison.li",
                verification_code_email=None,
            ),
        ),
        mock.patch.object(autofill, "find_resume_file", return_value=resume_path),
        mock.patch.object(autofill, "find_cover_letter_file", return_value=None),
        mock.patch.object(autofill, "resolve_shared_question_policy", return_value=None),
    ):
        payload = autofill._build_payload(out_dir, provider="openai")

    assert payload["board"] == "paycor"


def test_classify_submit_state_detects_paycor_review():
    autofill = load_module("autofill_paycor", "scripts/autofill_paycor.py")

    state = autofill._classify_submit_state(
        {
            "page_text": "Submit Application",
            "url": "https://recruitingbypaycor.com/career/JobIntroduction.action",
            "errors": [],
            "invalid_fields": [],
            "recaptcha_visible": False,
            "recaptcha_challenge_active": False,
        }
    )
    assert state["status"] == "review"
