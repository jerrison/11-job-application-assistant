import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from job_board_urls import looks_like_recruitee_url
from submit_application import _board_for_url


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_board_for_url_detects_recruitee_direct_host():
    url = "https://apply.recruitee.com/o/senior-product-manager"
    assert looks_like_recruitee_url(url)
    assert _board_for_url(url) == "recruitee"


def test_board_for_url_detects_recruitee_wrapper(monkeypatch):
    wrapper_url = "https://careers.distribusion.com/o/senior-pm"

    class _Resp:
        url = wrapper_url

        def read(self, _size: int = -1):
            return b'<html><script src="https://cdn.recruitee.com/assets/app.js"></script></html>'

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: _Resp())
    assert _board_for_url(wrapper_url) == "recruitee"


def test_board_for_url_rejects_non_role_recruitee_wrapper(monkeypatch):
    landing_url = "https://careers.distribusion.com/careers"

    class _Resp:
        url = landing_url

        def read(self, _size: int = -1):
            return b'<html><script src="https://cdn.recruitee.com/assets/app.js"></script></html>'

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: _Resp())
    with pytest.raises(ValueError):
        _board_for_url(landing_url)


def test_classify_submit_state_detects_review_page():
    autofill = load_module("autofill_recruitee", "scripts/autofill_recruitee.py")

    state = autofill._classify_submit_state(
        {
            "page_text": "Submit application",
            "url": "https://company.recruitee.com",
            "errors": [],
            "invalid_fields": [],
            "recaptcha_visible": False,
            "recaptcha_challenge_active": False,
        }
    )
    assert state["status"] == "review"


def test_build_payload_includes_resume(tmp_path):
    autofill = load_module("autofill_recruitee", "scripts/autofill_recruitee.py")
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
                "jd_source": "https://apply.recruitee.com/o/senior-product-manager",
                "company": "example",
                "company_proper": "Example",
                "jd_title": "Senior Product Manager",
            },
        ),
        mock.patch.object(
            autofill,
            "parse_master_resume",
            return_value=SimpleNamespace(
                first_name="Candidate",
                last_name="Name",
                email="candidate@example.com",
                phone="555-555-5555",
                location="San Francisco, CA",
                linkedin="https://linkedin.com/in/candidate/",
                website="https://candidate.example.com",
            ),
        ),
        mock.patch.object(
            autofill,
            "parse_application_profile",
            return_value=SimpleNamespace(
                location="San Francisco, CA",
                linkedin="https://linkedin.com/in/candidate/",
                website="https://candidate.example.com",
                gender="Male",
                veteran_status="I am not a protected veteran",
                disability_status="No, I do not have a disability and have not had one in the past",
                verification_code_email=None,
            ),
        ),
        mock.patch.object(autofill, "find_resume_file", return_value=resume_path),
        mock.patch.object(autofill, "find_cover_letter_file", return_value=None),
        mock.patch.object(autofill, "resolve_shared_question_policy", return_value=None),
    ):
        payload = autofill._build_payload(out_dir, provider="openai")

    assert payload["board"] == "recruitee"
    assert any(step["kind"] == "file" for step in payload["steps"])
