import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SmartRecruitersPayloadTests(unittest.TestCase):
    def test_build_payload_uses_shared_application_profile_url_fields(self):
        autofill = load_module("autofill_smartrecruiters", "scripts/autofill_smartrecruiters.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            resume_path = out_dir / "resume.pdf"
            resume_path.write_bytes(b"%PDF-fake")
            with (
                mock.patch.object(autofill, "migrate_role_output_layout"),
                mock.patch.object(
                    autofill,
                    "load_meta",
                    return_value={
                        "jd_source": "https://jobs.smartrecruiters.com/example/1",
                        "company_proper": "Example",
                        "jd_title": "Principal PM",
                    },
                ),
                mock.patch.object(
                    autofill,
                    "parse_master_resume",
                    return_value=SimpleNamespace(
                        first_name="Jerrison",
                        last_name="Li",
                        email="jerrisonli@gmail.com",
                        phone="555-555-5555",
                        location="San Francisco, CA",
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "parse_application_profile",
                    return_value=SimpleNamespace(
                        location="San Francisco, CA",
                        linkedin="https://www.linkedin.com/in/jerrisonli/",
                        website="https://jerrison.li",
                        gender="Male",
                        gender_identity="Cisgender Male/Man",
                        race_or_ethnicity="Hispanic or Latino",
                        veteran_status="I am not a protected veteran",
                        disability_status="No, I do not have a disability and have not had one in the past",
                        verification_code_email=None,
                    ),
                ),
                mock.patch.object(autofill, "find_resume_file", return_value=resume_path),
                mock.patch.object(autofill, "find_cover_letter_file", return_value=None),
            ):
                payload = autofill._build_payload(out_dir, provider="claude")

        linkedin_step = next(step for step in payload["steps"] if step["field_name"] == "linkedin")
        website_step = next(step for step in payload["steps"] if step["field_name"] == "website")
        gender_step = next(step for step in payload["steps"] if step["field_name"] == "gender")
        veteran_step = next(step for step in payload["steps"] if step["field_name"] == "veteran_status")
        self.assertEqual(linkedin_step["value"], "https://www.linkedin.com/in/jerrisonli/")
        self.assertEqual(website_step["value"], "https://jerrison.li")
        self.assertEqual(gender_step["value"], "Cisgender Male/Man")
        self.assertEqual(gender_step["profile_field"], "gender_identity")
        self.assertTrue(gender_step["blocks_draft_completion"])
        self.assertEqual(veteran_step["profile_field"], "veteran_status")

    def test_resolve_smartrecruiters_select_label_matches_gender_identity_alias(self):
        autofill = load_module("autofill_smartrecruiters", "scripts/autofill_smartrecruiters.py")

        selected = autofill._resolve_smartrecruiters_select_label(
            "Gender",
            "Male",
            ["Woman", "Man"],
        )

        self.assertEqual(selected, "Man")

    def test_navigate_to_apply_form_uses_im_interested_link(self):
        autofill = load_module("autofill_smartrecruiters", "scripts/autofill_smartrecruiters.py")

        apply_url = "https://jobs.smartrecruiters.com/oneclick-ui/company/Intuitive/publication/abc?dcr_ci=Intuitive"
        payload = {}

        class FakeLinkLocator:
            first = None

            def __init__(self, href: str):
                self.first = self
                self._href = href

            def count(self):
                return 1

            def is_visible(self):
                return True

            def get_attribute(self, name: str):
                if name == "href":
                    return self._href
                return None

        class EmptyLocator:
            first = None

            def __init__(self):
                self.first = self

            def count(self):
                return 0

            def is_visible(self):
                return False

        class FakePage:
            def __init__(self):
                self.url = "https://jobs.smartrecruiters.com/Intuitive/744000098691595"
                self.goto_calls = []

            def get_by_role(self, role, name=None):
                if role == "link":
                    return FakeLinkLocator(apply_url)
                return EmptyLocator()

            def goto(self, href, **kwargs):
                self.goto_calls.append((href, kwargs))
                self.url = href

            def wait_for_timeout(self, ms):
                pass

        page = FakePage()

        autofill._navigate_to_apply_form(page, payload)

        self.assertEqual(page.goto_calls[0][0], apply_url)
        self.assertEqual(payload["application_url"], apply_url)

    def test_wait_for_smartrecruiters_form_raises_job_closed_for_expired_page(self):
        autofill = load_module("autofill_smartrecruiters", "scripts/autofill_smartrecruiters.py")

        class FakeBodyLocator:
            def inner_text(self, timeout=None):
                return "Sorry, this job has expired"

        class FakePage:
            url = "https://jobs.smartrecruiters.com/Intuitive/744000107297051"

            def wait_for_selector(self, *args, **kwargs):
                raise TimeoutError("timed out waiting for form")

            def wait_for_timeout(self, ms):
                raise AssertionError("should not continue waiting after expired-page detection")

            def locator(self, selector):
                self.last_selector = selector
                return FakeBodyLocator()

        with self.assertRaisesRegex(RuntimeError, "job_closed:"):
            autofill._wait_for_smartrecruiters_form(FakePage())
