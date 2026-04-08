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


class ComeetPayloadTests(unittest.TestCase):
    def test_build_payload_uses_shared_application_profile_linkedin_field(self):
        autofill = load_module("autofill_comeet", "scripts/autofill_comeet.py")

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
                        "jd_source": "https://careers.example.com/jobs/comeet-1",
                        "company_proper": "Example",
                        "jd_title": "Principal PM",
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
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "parse_application_profile",
                    return_value=SimpleNamespace(
                        linkedin="https://linkedin.com/in/candidate/",
                        verification_code_email=None,
                    ),
                ),
                mock.patch.object(autofill, "find_resume_file", return_value=resume_path),
                mock.patch.object(autofill, "find_cover_letter_file", return_value=None),
            ):
                payload = autofill._build_payload(out_dir, provider="claude")

        linkedin_step = next(step for step in payload["steps"] if step["field_name"] == "linkedin")
        self.assertEqual(linkedin_step["value"], "https://linkedin.com/in/candidate/")

    def test_wait_for_comeet_form_uses_targeted_fallback_selector(self):
        autofill = load_module("autofill_comeet", "scripts/autofill_comeet.py")
        page = mock.Mock()
        page.wait_for_selector.side_effect = [RuntimeError("missing"), None]

        with (
            mock.patch.object(autofill, "_dismiss_cookie_banner"),
            mock.patch.object(autofill, "_click_apply_if_needed") as click_apply,
        ):
            autofill._wait_for_comeet_form(page)

        click_apply.assert_called_once_with(page)
        fallback_selector = page.wait_for_selector.call_args_list[1].args[0]
        self.assertNotEqual(fallback_selector, "form, input, button")
        self.assertIn("button[type='submit']", fallback_selector)
