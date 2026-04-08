import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from job_board_urls import looks_like_successfactors_url
from submit_application import _board_for_url


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_successfactors_direct_host_detected():
    url = "https://career4.successfactors.com/career?company=supermicro"
    assert looks_like_successfactors_url(url)
    assert _board_for_url(url) == "successfactors"


def test_successfactors_marketing_path_rejected():
    url = "https://www.successfactors.com/career-management"
    assert not looks_like_successfactors_url(url)
    with pytest.raises(ValueError):
        _board_for_url(url)


def test_successfactors_wrapper_html_triggers_detection(monkeypatch):
    class _Resp:
        url = "https://jobs.supermicro.com/job/pm"

        def read(self, _size: int = -1):
            return b"""
            <html>
              <head>
                <link rel="stylesheet" href="https://rmkcdn.successfactors.com/app.css"/>
                <script src="https://performancemanager4.successfactors.com/something.js"></script>
                <script src="/platform/js/j2w/min/j2w.apply.min.js"></script>
              </head>
              <body>
                <a href="/talentcommunity/apply/123">Apply</a>
              </body>
            </html>
            """

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: _Resp())
    assert _board_for_url("https://jobs.supermicro.com/job/pm") == "successfactors"


def test_classify_successfactors_sign_in_gate():
    autofill = load_module("autofill_successfactors", "scripts/autofill_successfactors.py")

    html = '<html><title>Sign In</title><input name="username"/><input name="password"/></html>'
    assert (
        autofill.classify_successfactors_auth_state(
            html=html,
            url="https://career4.successfactors.com/career",
        )
        == "sign_in_gate"
    )


def test_classify_successfactors_create_account_gate():
    autofill = load_module("autofill_successfactors", "scripts/autofill_successfactors.py")

    html = '<html><a>Create an account</a><input name="email"/></html>'
    assert (
        autofill.classify_successfactors_auth_state(
            html=html,
            url="https://career4.successfactors.com/career?login_ns=register",
        )
        == "create_account_gate"
    )


def test_classify_successfactors_sign_in_gate_from_page_title():
    autofill = load_module("autofill_successfactors", "scripts/autofill_successfactors.py")

    assert (
        autofill.classify_successfactors_auth_state(
            html="<html><body>Loading...</body></html>",
            url="https://career5.successfactors.eu/careers?company=SAP",
            page_title="Career Opportunities: Sign In",
        )
        == "sign_in_gate"
    )


def test_successfactors_redirected_to_careers_home_detected():
    autofill = load_module("autofill_successfactors", "scripts/autofill_successfactors.py")

    assert autofill.successfactors_redirected_to_careers_home(
        html="<html><body>Find your next role Search jobs Work area Career status Country</body></html>",
        url="https://jobs.sap.com/",
        page_title="Jobs at SAP | SAP Careers",
    )


def test_successfactors_redirected_to_talent_network_home_detected():
    autofill = load_module("autofill_successfactors", "scripts/autofill_successfactors.py")

    assert autofill.successfactors_redirected_to_careers_home(
        html=(
            "<html><body>"
            "Search Jobs View Profile Join our talent network Featured jobs "
            "Enter your email address to tell us about yourself."
            "</body></html>"
        ),
        url="https://jobs.supermicro.com/",
        page_title="Jobs at Supermicro",
    )


def test_build_payload_includes_resume_and_cover_letter(tmp_path):
    autofill = load_module("autofill_successfactors", "scripts/autofill_successfactors.py")
    out_dir = tmp_path / "role"
    out_dir.mkdir()
    resume_path = out_dir / "resume.pdf"
    cover_letter_path = out_dir / "cover-letter.pdf"
    resume_path.write_bytes(b"%PDF-fake")
    cover_letter_path.write_bytes(b"%PDF-fake")

    with (
        mock.patch.object(autofill, "migrate_role_output_layout"),
        mock.patch.object(
            autofill,
            "load_meta",
            return_value={
                "jd_source": "https://career4.successfactors.com/career?company=supermicro",
                "company": "supermicro",
                "company_proper": "Supermicro",
                "jd_title": "Senior Product Manager",
            },
        ),
        mock.patch.object(
            autofill,
            "parse_master_resume",
            return_value=SimpleNamespace(
                full_name="Candidate Name",
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
                verification_code_email=None,
            ),
        ),
        mock.patch.object(autofill, "find_resume_file", return_value=resume_path),
        mock.patch.object(autofill, "find_cover_letter_file", return_value=cover_letter_path),
    ):
        payload = autofill._build_payload(out_dir, provider="openai")

    assert payload["board"] == "successfactors"
    assert {step["field_name"] for step in payload["steps"]} >= {"resume", "cover_letter"}


def test_classify_submit_state_detects_review_page():
    autofill = load_module("autofill_successfactors", "scripts/autofill_successfactors.py")

    html = "<html><button>Submit Application</button></html>"
    assert autofill._classify_submit_state(html=html, url="https://career4.successfactors.com/career") == "review"


def test_maybe_enter_application_clicks_apply_link_with_suffix():
    autofill = load_module("autofill_successfactors", "scripts/autofill_successfactors.py")

    class FakeLocator:
        def __init__(self):
            self.first = self
            self.clicked = False

        def count(self):
            return 1

        def is_visible(self):
            return True

        def click(self):
            self.clicked = True

    class FakeMissingLocator:
        def __init__(self):
            self.first = self

        def count(self):
            return 0

        def is_visible(self):
            return False

        def click(self):
            raise AssertionError("should not click missing locator")

    class FakePage:
        def __init__(self):
            self.apply_locator = FakeLocator()
            self.waits = []

        def get_by_role(self, role, name=None):
            if role == "button" and name.search("Understood"):
                return FakeMissingLocator()
            if role == "link" and name.search("Apply now »"):
                return self.apply_locator
            return FakeMissingLocator()

        def wait_for_load_state(self, state, timeout):
            self.waits.append((state, timeout))

        def wait_for_timeout(self, timeout_ms):
            self.waits.append(("timeout", timeout_ms))

    page = FakePage()

    entered = autofill._maybe_enter_application(page)

    assert entered is True
    assert page.apply_locator.clicked is True
    assert ("domcontentloaded", 5000) in page.waits


def test_maybe_enter_application_prefers_apply_href_navigation():
    autofill = load_module("autofill_successfactors", "scripts/autofill_successfactors.py")

    class FakeLocator:
        def __init__(self):
            self.first = self
            self.clicked = False

        def count(self):
            return 1

        def is_visible(self):
            return True

        def click(self):
            self.clicked = True

        def get_attribute(self, name):
            assert name == "href"
            return "/talentcommunity/apply/1276862301/?locale=en_US"

    class FakeMissingLocator:
        def __init__(self):
            self.first = self

        def count(self):
            return 0

        def is_visible(self):
            return False

        def click(self):
            raise AssertionError("should not click missing locator")

        def get_attribute(self, name):
            del name
            return

    class FakePage:
        def __init__(self):
            self.url = "https://jobs.sap.com/job/example"
            self.apply_locator = FakeLocator()
            self.goto_calls = []
            self.waits = []

        def get_by_role(self, role, name=None):
            if role == "button" and name.search("Understood"):
                return FakeMissingLocator()
            if role == "link" and name.search("Apply now »"):
                return self.apply_locator
            return FakeMissingLocator()

        def goto(self, url, wait_until, timeout):
            self.goto_calls.append((url, wait_until, timeout))
            self.url = url

        def wait_for_load_state(self, state, timeout):
            self.waits.append((state, timeout))

        def wait_for_timeout(self, timeout_ms):
            self.waits.append(("timeout", timeout_ms))

    page = FakePage()

    entered = autofill._maybe_enter_application(page)

    assert entered is True
    assert page.apply_locator.clicked is False
    assert page.goto_calls == [("https://jobs.sap.com/talentcommunity/apply/1276862301/?locale=en_US", "domcontentloaded", 30000)]


def test_run_browser_registers_post_navigate_hook():
    autofill = load_module("autofill_successfactors", "scripts/autofill_successfactors.py")
    payload_path = Path("/tmp/test_successfactors_payload.json")
    payload_path.write_text('{"out_dir":"/tmp/test_successfactors"}', encoding="utf-8")

    with mock.patch.object(autofill, "run_simple_board_pipeline", return_value=0) as run_simple_board_pipeline:
        rc = autofill._run_browser(payload_path, headless=True, submit=False)

    assert rc == 0
    kwargs = run_simple_board_pipeline.call_args.kwargs
    assert callable(kwargs["post_navigate_hook"])
