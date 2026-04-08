import importlib.util
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from job_board_urls import looks_like_breezy_url
from submit_application import _board_for_url


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_board_for_url_detects_breezy_direct_host():
    url = "https://zero-hash.breezy.hr/p/7801647b617f-role"
    assert looks_like_breezy_url(url)
    assert _board_for_url(url) == "breezy"


def test_build_payload_marks_board_breezy(tmp_path):
    autofill = load_module("autofill_breezy", "scripts/autofill_breezy.py")
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
                "jd_source": "https://zero-hash.breezy.hr/p/7801647b617f-role",
                "company": "zero-hash",
                "company_proper": "Zero Hash",
                "jd_title": "Senior Product Manager",
            },
        ),
        mock.patch.object(
            autofill,
            "parse_master_resume",
            return_value=SimpleNamespace(
                first_name="Candidate",
                last_name="Name",
                full_name="Candidate Name",
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
        mock.patch.object(autofill, "find_cover_letter_file", return_value=cover_letter_path),
        mock.patch.object(autofill, "resolve_shared_question_policy", return_value=None),
    ):
        payload = autofill._build_payload(out_dir, provider="openai")

    assert payload["board"] == "breezy"
    assert any(step["kind"] == "file" for step in payload["steps"])
    assert any(
        step["field_name"] == "full_name" and step["value"] == "Candidate Name"
        for step in payload["steps"]
    )


def test_classify_submit_state_detects_thank_you():
    autofill = load_module("autofill_breezy", "scripts/autofill_breezy.py")

    state = autofill._classify_submit_state(
        {
            "page_text": "Thank you for applying",
            "url": "https://company.breezy.hr",
            "errors": [],
            "invalid_fields": [],
            "recaptcha_visible": False,
            "recaptcha_challenge_active": False,
        }
    )
    assert state["status"] == "confirmed"


def test_breezy_post_navigate_hook_clicks_apply_when_form_is_not_visible():
    autofill = load_module("autofill_breezy", "scripts/autofill_breezy.py")

    class FakeCountLocator:
        def __init__(self, count):
            self._count = count
            self.first = self

        def count(self):
            return self._count

    class FakeApplyLocator(FakeCountLocator):
        def __init__(self, page, count):
            super().__init__(count)
            self._page = page

        def click(self):
            self._page.clicked = True
            self._page.url = f"{self._page.url.rstrip('/')}/apply"

    class FakePage:
        def __init__(self):
            self.clicked = False
            self.url = "https://company.breezy.hr/p/role"
            self.waited_for_selector = None

        def locator(self, selector):
            if selector in {
                "form",
                "input[type='file']",
                "input[type='text']",
                "input[type='email']",
                "textarea",
            }:
                return FakeCountLocator(0)
            raise AssertionError(selector)

        def get_by_role(self, role, name=None):
            assert role in {"button", "link"}
            assert name is not None and name.search("Apply To Position")
            return FakeApplyLocator(self, 1 if role == "button" else 0)

        def wait_for_selector(self, selector, timeout):
            self.waited_for_selector = (selector, timeout)

    page = FakePage()

    advanced = autofill._advance_breezy_apply_gate(page)

    assert advanced is True
    assert page.clicked is True
    assert page.url.endswith("/apply")
    assert page.waited_for_selector is not None
    assert "input[type='file']" in page.waited_for_selector[0]


def test_breezy_post_navigate_hook_skips_when_form_is_already_visible():
    autofill = load_module("autofill_breezy", "scripts/autofill_breezy.py")

    class FakeCountLocator:
        def __init__(self, count):
            self._count = count
            self.first = self

        def count(self):
            return self._count

    class FakePage:
        def __init__(self):
            self.role_queries = []

        def locator(self, selector):
            if selector in {
                "form",
                "input[type='file']",
                "input[type='text']",
                "input[type='email']",
                "textarea",
            }:
                return FakeCountLocator(1 if selector == "form" else 0)
            raise AssertionError(selector)

        def get_by_role(self, role, name=None):
            self.role_queries.append((role, name.pattern if isinstance(name, re.Pattern) else name))
            raise AssertionError("apply CTA should not be queried once the Breezy form is already visible")

    page = FakePage()

    advanced = autofill._advance_breezy_apply_gate(page)

    assert advanced is False
    assert page.role_queries == []


def test_run_browser_registers_post_navigate_hook():
    autofill = load_module("autofill_breezy", "scripts/autofill_breezy.py")

    with mock.patch("autofill_pipeline.run_browser_pipeline", return_value=0) as run_browser_pipeline:
        rc = autofill._run_browser(Path("/tmp/payload.json"), headless=True, submit=False)

    assert rc == 0
    kwargs = run_browser_pipeline.call_args.kwargs
    assert callable(kwargs["post_navigate_hook"])
