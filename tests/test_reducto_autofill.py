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


class ReductoAutofillTests(unittest.TestCase):
    def test_build_payload_uses_application_profile_linkedin_and_us_country_code_phone(self):
        autofill = load_module("autofill_reducto", "scripts/autofill_reducto.py")

        profile = SimpleNamespace(
            first_name="Candidate",
            last_name="Name",
            full_name="Candidate Name",
            email="candidate@example.com",
            phone="415-555-0100",
            linkedin="https://www.linkedin.com/in/stale-profile/",
        )
        application_profile = SimpleNamespace(
            pronouns="He / Him / His",
            linkedin="https://linkedin.com/in/candidate/",
            verification_code_email="candidate@example.com",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            resume_path = out_dir / "Candidate Name Resume - Reducto.pdf"
            resume_path.write_text("resume", encoding="utf-8")

            with (
                mock.patch.object(autofill, "migrate_role_output_layout"),
                mock.patch.object(
                    autofill,
                    "load_meta",
                    return_value={
                        "jd_source": "https://www.reducto.ai/careers/founding-pm",
                        "jd_source_resolved": "https://www.reducto.ai/careers/founding-pm",
                        "jd_title": "Founding Product Manager",
                        "company_proper": "Reducto",
                    },
                ),
                mock.patch.object(autofill, "parse_master_resume", return_value=profile),
                mock.patch.object(autofill, "parse_application_profile", return_value=application_profile),
                mock.patch.object(autofill, "find_resume_file", return_value=resume_path),
            ):
                payload = autofill._build_payload(out_dir, provider="claude")

        steps = {step["field_name"]: step for step in payload["steps"]}

        self.assertEqual(steps["phone"]["value"], "14155550100")
        self.assertEqual(steps["linkedin"]["value"], "https://linkedin.com/in/candidate/")
        self.assertEqual(steps["linkedin"]["source"], "application_profile.md")
        self.assertEqual(steps["worked_at_startup"]["value"], "Yes")
        self.assertEqual(steps["require_sponsorship"]["value"], "No")

    def test_fill_radio_field_scopes_to_labeled_group_before_global_yes_no(self):
        autofill = load_module("autofill_reducto", "scripts/autofill_reducto.py")

        class EmptyLocator:
            first = None

            def count(self):
                return 0

        EmptyLocator.first = EmptyLocator()

        class FakeRadio:
            def __init__(self, name: str):
                self.name = name
                self.clicked = False
                self.first = self

            def count(self):
                return 1

            def scroll_into_view_if_needed(self):
                return None

            def click(self):
                self.clicked = True

        class FakeLocator:
            def __init__(self, items):
                self._items = list(items)
                self.first = self._items[0] if self._items else EmptyLocator()

            def count(self):
                return len(self._items)

            def nth(self, index):
                return self._items[index]

        class FakeContainer:
            def __init__(self, text: str, radios: list[FakeRadio]):
                self._text = text
                self._radios = radios

            def inner_text(self):
                return self._text

            def get_by_role(self, role: str, name=None):
                assert role == "radio"
                matched = [radio for radio in self._radios if name.search(radio.name)]
                return FakeLocator(matched)

            def locator(self, selector: str):
                assert selector == "[role='group'], [role='radiogroup'], fieldset"
                return FakeLocator([])

        startup_no = FakeRadio("No")
        sponsorship_no = FakeRadio("No")

        class FakePage:
            def __init__(self):
                self.global_no = startup_no
                self.containers = [
                    FakeContainer(
                        "Have you worked at a startup? Yes No",
                        [FakeRadio("Yes"), startup_no],
                    ),
                    FakeContainer(
                        "Will you now or in the future require sponsorship? Yes No",
                        [FakeRadio("Yes"), sponsorship_no],
                    ),
                ]

            def get_by_role(self, role: str, name=None):
                assert role == "radio"
                matched = [self.global_no] if name.search(self.global_no.name) else []
                return FakeLocator(matched)

            def locator(self, selector: str):
                assert selector == "fieldset, div, section, [role='group'], [role='radiogroup']"
                return FakeLocator(self.containers)

            def wait_for_timeout(self, timeout_ms: int):
                return None

        page = FakePage()

        filled = autofill._fill_radio_field(
            page,
            label="Will you now or in the future require sponsorship?",
            value="No",
        )

        self.assertTrue(filled)
        self.assertFalse(startup_no.clicked)
        self.assertTrue(sponsorship_no.clicked)


if __name__ == "__main__":
    unittest.main()
