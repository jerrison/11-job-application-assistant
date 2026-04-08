import importlib.util
import json
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


class GemAutofillTests(unittest.TestCase):
    def test_gem_job_closed_reason_detects_removed_posting_shell(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        reason = autofill._gem_job_closed_reason(
            "https://jobs.gem.com/linktree/example",
            "Job not found\nThe link you followed may be out of date or this job post may have been removed.\nView all open jobs",
        )

        self.assertIsNotNone(reason)
        self.assertIn("job_closed:", reason)
        self.assertIn("removed or missing posting shell", reason)

    def test_build_payload_writes_job_unavailable_artifact_for_job_closed_signal(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)

            with (
                mock.patch.object(autofill, "migrate_role_output_layout"),
                mock.patch.object(
                    autofill,
                    "load_meta",
                    return_value={
                        "company": "linktree",
                        "company_proper": "Linktree",
                        "jd_source": "https://jobs.gem.com/linktree/example",
                        "jd_source_resolved": "https://jobs.gem.com/linktree/example?src=LinkedIn+Paid",
                        "board_url": "https://jobs.gem.com/linktree/example?src=LinkedIn+Paid",
                        "jd_title": "Staff AI Product Manager",
                    },
                ),
                mock.patch.object(
                    autofill,
                    "parse_master_resume",
                    return_value=mock.Mock(email="jerrisonli@gmail.com"),
                ),
                mock.patch.object(
                    autofill,
                    "parse_application_profile",
                    return_value=mock.Mock(verification_code_email=None),
                ),
                mock.patch.object(
                    autofill,
                    "_inspect_gem_form",
                    side_effect=RuntimeError("job_closed: Gem showed a removed or missing posting shell"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "job_closed"):
                    autofill._build_payload(out_dir)

            unavailable_payload = json.loads((out_dir / "submit" / "job_unavailable.json").read_text(encoding="utf-8"))

        self.assertEqual(unavailable_payload["status"], "job_closed")
        self.assertEqual(unavailable_payload["board"], "gem")
        self.assertIn("removed or missing posting shell", unavailable_payload["message"])

    def test_build_payload_omits_redundant_review_screenshot_artifact(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")
        inspection = {
            "title": "Senior TPM",
            "fields": [
                {
                    "index": 0,
                    "label": "Email",
                    "required": True,
                    "kind": "text",
                    "options": [],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            with (
                mock.patch.object(autofill, "migrate_role_output_layout"),
                mock.patch.object(
                    autofill,
                    "load_meta",
                    return_value={
                        "jd_source": "https://jobs.gem.com/example/1",
                        "company_proper": "Example",
                        "jd_title": "Senior TPM",
                    },
                ),
                mock.patch.object(
                    autofill,
                    "parse_master_resume",
                    return_value=SimpleNamespace(
                        first_name="Jerrison",
                        last_name="Li",
                        email="jerrisonli@gmail.com",
                        linkedin="https://www.linkedin.com/in/jerrisonli/",
                        website="https://jerrison.li",
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "parse_application_profile",
                    return_value=SimpleNamespace(
                        verification_code_email="",
                        github="https://github.com/jerrison",
                        linkedin="https://www.linkedin.com/in/jerrisonli/",
                        website="https://jerrison.li",
                        location="San Francisco, CA",
                        how_did_you_hear="",
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "_inspect_gem_form",
                    return_value=inspection,
                ),
                mock.patch.object(
                    autofill,
                    "generate_application_answers",
                    return_value={},
                ),
                mock.patch.object(
                    autofill,
                    "find_cover_letter_text",
                    return_value="",
                ),
            ):
                payload = autofill._build_payload(out_dir, provider="claude")

        self.assertNotIn("review_screenshot", payload["artifacts"])
        self.assertIn("pre_submit_screenshot", payload["artifacts"])

    def test_write_report_splits_planned_but_unconfirmed_fields(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            payload = {
                "job_title": "Product Manager",
                "company": "Example",
                "job_url": "https://jobs.gem.com/example/1",
                "artifacts": {
                    "report_json": str(out_dir / "gem_autofill_report.json"),
                    "report_markdown": str(out_dir / "gem_autofill_report.md"),
                    "pre_submit_screenshot": str(out_dir / "gem_autofill_pre_submit.png"),
                },
                "unknown_questions": [],
                "steps": [],
            }
            runtime = {
                "steps": [
                    {
                        "field_name": "email",
                        "label": "Email",
                        "kind": "text",
                        "value": "jerrisonli@gmail.com",
                        "source": "master_resume.md",
                        "required": True,
                        "filled": True,
                    },
                    {
                        "field_name": "work_authorization",
                        "label": "Are you authorized to work?",
                        "kind": "radio",
                        "option": "Yes",
                        "source": "application_profile.md",
                        "required": True,
                        "filled": True,
                    },
                    {
                        "field_name": "cover_letter",
                        "label": "Cover Letter",
                        "kind": "file",
                        "file_path": "/tmp/Jerrison Li Cover Letter - Example.pdf",
                        "source": "existing_cover_letter_asset",
                        "required": False,
                    },
                ]
            }

            report_payload = common.write_report(payload, board_name="gem", runtime=runtime)
            saved = json.loads((out_dir / "gem_autofill_report.json").read_text(encoding="utf-8"))

        self.assertEqual(len(saved["fields"]), 2)
        self.assertEqual(saved["fields"][0]["field_name"], "email")
        self.assertEqual(saved["fields"][1]["field_name"], "work_authorization")
        self.assertEqual(saved["fields"][1]["value"], "Yes")
        self.assertEqual(saved["planned_but_unconfirmed_fields"][0]["field_name"], "cover_letter")
        self.assertEqual(report_payload["planned_but_unconfirmed_fields"][0]["field_name"], "cover_letter")

    def test_infer_step_fills_github_profile_from_application_profile(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "github_profile",
                "label": "GitHub Profile",
                "required": False,
                "kind": "text",
                "index": 0,
            },
            meta={},
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(
                github="https://github.com/jerrison",
                linkedin=None,
                website=None,
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(step["value"], "https://github.com/jerrison")
        self.assertEqual(step["source"], "application_profile.md")

    def test_infer_step_does_not_fill_optional_website_password_field(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "portfolio_password",
                "label": "[Optional] If your portfolio requires a password please include here",
                "required": False,
                "kind": "text",
                "index": 0,
            },
            meta={},
            profile=SimpleNamespace(linkedin=None, website="https://jerrison.li"),
            application_profile=SimpleNamespace(
                github="https://github.com/jerrison",
                linkedin=None,
                website="https://jerrison.li",
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNone(step)

    def test_infer_step_answers_salary_comfort_from_application_profile(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "salary_comfort",
                "label": "Are you comfortable interviewing for the salary outlined in the job description?",
                "required": True,
                "kind": "radio",
                "index": 0,
            },
            meta={},
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(
                comfortable_with_posted_salary=False,
                github="https://github.com/jerrison",
                linkedin=None,
                website=None,
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(step["option"], "No")
        self.assertEqual(step["source"], "application_profile.md")

    def test_infer_step_answers_transgender_from_application_profile(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "transgender_status",
                "label": "Do you identify as transgender?",
                "required": True,
                "kind": "radio",
                "index": 0,
                "options": ["Yes", "No"],
            },
            meta={},
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(
                transgender_status="No",
                comfortable_with_posted_salary=False,
                github="https://github.com/jerrison",
                linkedin=None,
                website=None,
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(step["option"], "No")

    def test_infer_step_answers_gender_select_from_application_profile(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "gender_identity",
                "label": "Gender",
                "required": False,
                "kind": "select",
                "index": 0,
                "options": [],
            },
            meta={},
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(
                gender="Male",
                gender_identity="Cisgender Male/Man",
                comfortable_with_posted_salary=True,
                github="https://github.com/jerrison",
                linkedin=None,
                website=None,
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "select")
        self.assertEqual(step["option"], "Cisgender Male/Man")
        self.assertEqual(step["source"], "application_profile.md")
        self.assertTrue(step["blocks_draft_completion"])
        self.assertEqual(step["profile_field"], "gender_identity")

    def test_infer_step_matches_gender_identity_alias_against_man_option(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "gender_identity",
                "label": "Gender",
                "required": False,
                "kind": "select",
                "index": 0,
                "options": ["Man", "Woman"],
            },
            meta={},
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(
                gender="Male",
                gender_identity="Cisgender Male/Man",
                comfortable_with_posted_salary=True,
                github="https://github.com/jerrison",
                linkedin=None,
                website=None,
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["option"], "Man")

    def test_infer_step_answers_race_select_from_application_profile(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "race_or_ethnicity",
                "label": "Race",
                "required": False,
                "kind": "select",
                "index": 0,
                "options": [],
            },
            meta={},
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(
                race_or_ethnicity="Hispanic or Latino",
                comfortable_with_posted_salary=True,
                github="https://github.com/jerrison",
                linkedin=None,
                website=None,
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "select")
        self.assertEqual(step["option"], "Hispanic or Latino")
        self.assertEqual(step["source"], "application_profile.md")
        self.assertTrue(step["blocks_draft_completion"])
        self.assertEqual(step["profile_field"], "race_or_ethnicity")
        self.assertEqual(step["source"], "application_profile.md")

    def test_infer_step_uses_full_onsite_start_location_answer_for_text_prompt(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        with mock.patch.object(
            autofill,
            "build_onsite_start_location_answer",
            return_value="Yes. The soonest I could start is March 23, 2026, and I would plan to work from San Francisco.",
        ):
            step = autofill._infer_step(
                {
                    "field_name": "onsite_start_location",
                    "label": "This is an onsite job in SF or Seattle, w 1-day a week wfh flexibility. Have you taken this into consideration & still want to proceed? If so, when is the soonest you could start? And at which location?",
                    "required": True,
                    "kind": "textarea",
                    "index": 0,
                },
                meta={},
                profile=SimpleNamespace(linkedin=None, website=None),
                application_profile=SimpleNamespace(
                    comfortable_with_posted_salary=False,
                    github="https://github.com/jerrison",
                    linkedin=None,
                    website=None,
                    location="San Francisco, CA",
                ),
                out_dir=PROJECT_ROOT,
                generated_answers={},
            )

        self.assertEqual(step["kind"], "textarea")
        self.assertEqual(
            step["value"],
            "Yes. The soonest I could start is March 23, 2026, and I would plan to work from San Francisco.",
        )

    def test_infer_step_does_not_treat_narrative_experience_prompt_as_boolean(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "homeowners_experience",
                "label": "Homeowners / P&C product ownership: Describe your experience managing a homeowners or property line of business. Include market context (admitted vs E&S, state[s], peril) and your decision scope.",
                "required": True,
                "kind": "radio",
                "index": 0,
            },
            meta={},
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(
                authorized_to_work_unconditionally=True,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
                comfortable_with_posted_salary=True,
                github="https://github.com/jerrison",
                linkedin=None,
                website=None,
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNone(step)

    def test_infer_step_uses_cover_letter_file_for_upload_fields(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        with mock.patch.object(autofill, "find_cover_letter_file", return_value=PROJECT_ROOT / "cover-letter.pdf"):
            step = autofill._infer_step(
                {
                    "field_name": "cover_letter",
                    "label": "Cover Letter",
                    "required": False,
                    "kind": "file",
                    "index": 0,
                },
                meta={},
                profile=SimpleNamespace(linkedin=None, website=None),
                application_profile=SimpleNamespace(
                    github="https://github.com/jerrison",
                    linkedin=None,
                    website=None,
                    location="San Francisco, CA",
                ),
                out_dir=PROJECT_ROOT,
                generated_answers={},
            )

        self.assertEqual(step["source"], "existing_cover_letter_asset")
        self.assertTrue(step["file_path"].endswith("cover-letter.pdf"))

    def test_fill_step_fails_when_cover_letter_text_cannot_be_confirmed(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        class FakeTextarea:
            pass

        class FakeLocatorResult:
            def __init__(self, locator):
                self.first = locator

        class FakeGroup:
            def locator(self, selector):
                self.selector = selector
                return FakeLocatorResult(FakeTextarea())

        step = {
            "kind": "textarea",
            "label": "Cover Letter",
            "value": "Dear Hiring Team,\n\nThanks.\n",
            "source": "cover_letter_text.txt",
        }

        with mock.patch.object(autofill, "_field_group", return_value=FakeGroup()):
            with mock.patch.object(autofill, "_fill_text_value"):
                with mock.patch.object(autofill, "_confirm_cover_letter_text", return_value=False):
                    with self.assertRaisesRegex(RuntimeError, "Gem cover letter text"):
                        autofill._fill_step(page=None, step=step)

    def test_fill_step_leaves_visible_self_id_unconfirmed_when_confirmation_fails(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        class FakeLocator:
            def __init__(self, has_elements=True):
                self._has_elements = has_elements
                self.first = self

            def count(self):
                return 1 if self._has_elements else 0

            def click(self):
                pass

        class FakeGroup:
            def locator(self, selector, **kwargs):
                if "label" in selector:
                    return FakeLocator(True)
                return FakeLocator(False)

        step = {
            "kind": "radio",
            "label": "Pronouns",
            "option": "He/Him",
            "source": "application_profile.md",
            "blocks_draft_completion": True,
            "blocker_kind": "visible_self_id",
            "form_index": 1,
        }

        with mock.patch.object(autofill, "_field_group", return_value=FakeGroup()):
            with mock.patch.object(autofill, "_confirm_visible_self_id_step", return_value=False):
                autofill._fill_step(page=mock.Mock(), step=step)

        self.assertFalse(step.get("filled", False))
        self.assertEqual(step["status"], "planned")
        self.assertIn("could not confirm", step["note"].lower())

    def test_fill_step_radio_falls_back_to_dropdown_button(self):
        """When no <label> exists, _fill_step should click a dropdown button and select the option."""
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        clicks = []

        class FakeLocator:
            def __init__(self, name, *, has_elements=True):
                self._name = name
                self._has_elements = has_elements
                self.first = self

            def count(self):
                return 1 if self._has_elements else 0

            def click(self):
                clicks.append(self._name)

        class FakeGroup:
            def locator(self, selector, **kwargs):
                if "label" in selector:
                    return FakeLocator("label", has_elements=False)
                if "button" in selector:
                    return FakeLocator("dropdown_button", has_elements=True)
                return FakeLocator("other", has_elements=False)

        class FakePage:
            def wait_for_timeout(self, ms):
                pass

            def get_by_text(self, text, exact=False):
                return FakeLocator(f"option:{text}", has_elements=True)

        step = {
            "kind": "radio",
            "label": "Are you open to working out of our LA or Bay Area office?",
            "option": "Yes",
            "form_index": 9,
        }

        with mock.patch.object(autofill, "_field_group", return_value=FakeGroup()):
            with mock.patch.object(autofill, "_confirm_choice_step", return_value=True):
                autofill._fill_step(page=FakePage(), step=step)

        self.assertTrue(step.get("filled"))
        self.assertIn("dropdown_button", clicks)
        self.assertIn("option:Yes", clicks)

    def test_fill_step_dropdown_uses_visible_menu_options_instead_of_page_text(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        clicks = []

        class FakeLocator:
            def __init__(self, name, *, has_elements=True, text=None):
                self._name = name
                self._has_elements = has_elements
                self._text = text or name
                self.first = self
                self.last = self

            def count(self):
                return 1 if self._has_elements else 0

            def click(self):
                clicks.append(self._name)

            def is_visible(self):
                return self._has_elements

            def inner_text(self):
                return self._text

        class FakeLocatorList:
            def __init__(self, locators):
                self._locators = locators

            def count(self):
                return len(self._locators)

            def nth(self, index):
                return self._locators[index]

            @property
            def first(self):
                return self._locators[0]

        class FakeGroup:
            def locator(self, selector, **kwargs):
                if "label" in selector:
                    return FakeLocator("label", has_elements=False)
                if "button" in selector:
                    return FakeLocator("dropdown_button", has_elements=True)
                return FakeLocator("other", has_elements=False)

        class FakePage:
            def wait_for_timeout(self, ms):
                pass

            def locator(self, selector):
                if selector == "[role='option'], [role='menuitem']":
                    return FakeLocatorList(
                        [
                            FakeLocator("menu_no", text="No"),
                            FakeLocator("menu_yes", text="Yes"),
                        ]
                    )
                return FakeLocatorList([])

            def get_by_text(self, text, exact=False):
                return FakeLocator(f"wrong_page_text:{text}", has_elements=True)

        step = {
            "kind": "select",
            "label": "Are you open to working out of our LA or Bay Area office?",
            "option": "Yes",
            "form_index": 9,
        }

        with mock.patch.object(autofill, "_field_group", return_value=FakeGroup()):
            with mock.patch.object(autofill, "_confirm_choice_step", return_value=True):
                autofill._fill_step(page=FakePage(), step=step)

        self.assertTrue(step.get("filled"))
        self.assertIn("dropdown_button", clicks)
        self.assertIn("menu_yes", clicks)
        self.assertNotIn("wrong_page_text:Yes", clicks)

    def test_fill_step_radio_uses_label_when_available(self):
        """When <label> exists, _fill_step should use the standard radio label click."""
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        clicks = []

        class FakeLocator:
            def __init__(self, name, *, has_elements=True):
                self._name = name
                self._has_elements = has_elements
                self.first = self

            def count(self):
                return 1 if self._has_elements else 0

            def click(self):
                clicks.append(self._name)

        class FakeGroup:
            def locator(self, selector, **kwargs):
                if "label" in selector:
                    return FakeLocator("label", has_elements=True)
                return FakeLocator("other", has_elements=False)

        step = {
            "kind": "radio",
            "label": "Do you require sponsorship?",
            "option": "No",
            "form_index": 8,
        }

        with mock.patch.object(autofill, "_field_group", return_value=FakeGroup()):
            with mock.patch.object(autofill, "_confirm_choice_step", return_value=True):
                autofill._fill_step(page=None, step=step)

        self.assertTrue(step.get("filled"))
        self.assertEqual(clicks, ["label"])

    def test_confirm_choice_step_accepts_checked_checkbox_widgets(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        class FakeGroup:
            def __init__(self):
                self.script = ""
                self.expected = ""

            def evaluate(self, script, expected):
                self.script = script
                self.expected = expected
                return (
                    "input[type='checkbox']:checked" in script
                    and "[role='checkbox'][aria-checked='true']" in script
                    and "[role='menuitemcheckbox'][aria-checked='true']" in script
                    and expected == "other"
                )

        group = FakeGroup()

        confirmed = autofill._confirm_choice_step(
            group,
            {
                "kind": "radio",
                "label": "How did you hear about this role?",
                "option": "Other",
            },
        )

        self.assertTrue(confirmed)
        self.assertIn("input[type='checkbox']:checked", group.script)
        self.assertIn("[role='checkbox'][aria-checked='true']", group.script)
        self.assertIn("[role='menuitemcheckbox'][aria-checked='true']", group.script)

    def test_infer_step_handles_experience_confirmation_radio(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "have_you_shipped_an_ai_powered_feature_or_product",
                "label": "Have you shipped an AI-powered feature or product?",
                "required": True,
                "kind": "radio",
                "index": 0,
                "options": ["Yes", "No"],
            },
            meta={},
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(
                authorized_to_work_unconditionally=True,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
                comfortable_with_posted_salary=True,
                github="https://github.com/jerrison",
                linkedin=None,
                website=None,
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "radio")
        self.assertEqual(step["option"], "Yes")
        self.assertEqual(step["source"], "shared_positive_fit_policy")

    def test_infer_step_handles_office_attendance_unknown_kind(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "are_you_open_to_working_out_of_our_la_or_bay_area_office_approx_3x_week",
                "label": "Are you open to working out of our LA or Bay Area office approx. 3x/week?",
                "required": True,
                "kind": "unknown",
                "index": 0,
                "options": [],
            },
            meta={},
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(
                comfortable_with_posted_salary=True,
                lives_in_job_location=True,
                comfortable_working_on_site=True,
                github="https://github.com/jerrison",
                linkedin=None,
                website=None,
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "radio")
        self.assertEqual(step["option"], "Yes")
        self.assertEqual(step["source"], "shared_positive_fit_policy")

    def test_experience_confirmation_excludes_open_ended_prompts(self):
        common = load_module("application_submit_common", "scripts/application_submit_common.py")

        # "share more" should be excluded
        self.assertFalse(
            common.question_is_experience_confirmation(
                "If yes can you share more about the AI-powered feature or products you've shipped?"
            )
        )
        # Direct yes/no should match
        self.assertTrue(
            common.question_is_experience_confirmation("Have you shipped an AI-powered feature or product?")
        )

    def test_office_attendance_matches_nx_week_pattern(self):
        common = load_module("application_submit_common", "scripts/application_submit_common.py")

        self.assertTrue(
            common.question_is_office_attendance_prompt(
                "Are you open to working out of our LA or Bay Area office approx. 3x/week?"
            )
        )
        self.assertTrue(common.question_is_office_attendance_prompt("Can you come to the office 2x per week?"))

    def test_infer_step_handles_how_did_you_hear_radio(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "how_did_you_hear_about_retool",
                "label": "How did you hear about Retool?",
                "required": True,
                "kind": "radio",
                "index": 0,
                "options": [
                    "Job Site (i.e. Glassdoor, Indeed)",
                    "Social (i.e. LinkedIn, Facebook)",
                    "Retool Careers Page",
                    "Recruiter",
                    "Billboard/ads",
                    "Referral",
                    "Other",
                ],
            },
            meta={"jd_source": "https://jobs.gem.com/retool/x", "company_proper": "Retool"},
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(
                authorized_to_work_unconditionally=True,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
                comfortable_with_posted_salary=True,
                github="https://github.com/jerrison",
                linkedin=None,
                website=None,
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "radio")
        self.assertEqual(step["option"], "Retool Careers Page")
        self.assertEqual(step["source"], "application_profile.md")

    def test_infer_step_handles_how_did_you_hear_checkbox_with_trueup_source(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "how_did_you_hear_about_this_role",
                "label": "How did you hear about this role?",
                "required": True,
                "kind": "checkbox",
                "index": 0,
                "options": [
                    "Event",
                    "Friend",
                    "Linkedin",
                    "Recruiter reached out",
                    "Work at a startup/YC",
                    "WellFound/Angelist",
                    "Other",
                ],
            },
            meta={
                "jd_source": "https://jobs.gem.com/onesignal/1?utm_source=trueup.io&utm_medium=website&ref=trueup",
                "company_proper": "OneSignal",
            },
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(
                how_did_you_hear="",
                comfortable_with_posted_salary=True,
                github="https://github.com/jerrison",
                linkedin=None,
                website=None,
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "radio")
        self.assertEqual(step["option"], "Other")
        self.assertEqual(step["source"], "job_url.utm_source")

    def test_infer_step_handles_work_authorization_checkbox_prompt(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "are_you_currently_authorized_to_work_in_the_united_states_without_the_need_for_employer_sponsorship",
                "label": "Are you currently authorized to work in the United States without the need for employer sponsorship?",
                "required": True,
                "kind": "checkbox",
                "index": 0,
                "options": ["Yes", "No"],
            },
            meta={},
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(
                authorized_to_work_unconditionally=True,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
                comfortable_with_posted_salary=True,
                github="https://github.com/jerrison",
                linkedin=None,
                website=None,
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "radio")
        self.assertEqual(step["option"], "Yes")
        self.assertEqual(step["source"], "application_profile.md")

    def test_infer_step_handles_sponsorship_checkbox_prompt(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "will_you_now_or_in_the_future_require_sponsorship_for_employment_visa_status_in_the_united_states",
                "label": "Will you now or in the future require sponsorship for employment visa status in the United States?",
                "required": True,
                "kind": "checkbox",
                "index": 0,
                "options": ["Yes", "No"],
            },
            meta={},
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(
                authorized_to_work_unconditionally=True,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
                comfortable_with_posted_salary=True,
                github="https://github.com/jerrison",
                linkedin=None,
                website=None,
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "radio")
        self.assertEqual(step["option"], "No")
        self.assertEqual(step["source"], "application_profile.md")

    def test_infer_step_handles_where_are_you_located_state_radio(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "where_are_you_located",
                "label": "Where are you located?",
                "required": True,
                "kind": "radio",
                "index": 0,
                "options": ["New York", "California", "Texas", "Other"],
            },
            meta={},
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(
                authorized_to_work_unconditionally=True,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
                comfortable_with_posted_salary=True,
                github="https://github.com/jerrison",
                linkedin=None,
                website=None,
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "radio")
        self.assertEqual(step["option"], "California")
        self.assertEqual(step["source"], "application_profile.md")

    def test_infer_step_handles_work_authorization_country_options(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "are_you_authorized_to_work_in_one_of_the_following_locations",
                "label": "Are you authorized to work in one of the following locations?",
                "required": True,
                "kind": "radio",
                "index": 0,
                "options": ["US 🇺🇸", "Canada 🇨🇦", "🇮🇪 Ireland"],
            },
            meta={},
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(
                country="United States",
                work_authorization_statement="Authorized to work in the United States without sponsorship.",
                authorized_to_work_unconditionally=True,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
                comfortable_with_posted_salary=True,
                github="https://github.com/jerrison",
                linkedin=None,
                website=None,
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "radio")
        self.assertEqual(step["option"], "US 🇺🇸")
        self.assertEqual(step["source"], "application_profile.md")

    def test_infer_step_handles_where_did_you_hear_checkbox_with_trueup_source(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "where_did_you_hear_about_biorender",
                "label": "Where did you hear about BioRender?",
                "required": True,
                "kind": "checkbox",
                "index": 0,
                "options": [
                    "I'm a BioRender user",
                    "Someone from BioRender reached out to me",
                    "LinkedIn",
                    "Y-Combinator",
                    "Great Places to Work listing",
                    "Referred by a BioRender Team member",
                    "Other",
                ],
            },
            meta={
                "jd_source": "https://jobs.gem.com/biorender/1?utm_source=trueup.io&utm_medium=website&ref=trueup",
                "company_proper": "BioRender",
            },
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(
                how_did_you_hear="",
                authorized_to_work_unconditionally=True,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
                comfortable_with_posted_salary=True,
                github="https://github.com/jerrison",
                linkedin=None,
                website=None,
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "radio")
        self.assertEqual(step["option"], "Other")
        self.assertEqual(step["source"], "job_url.utm_source")

    def test_pick_hear_about_option_priority_order(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        # Company careers page wins over LinkedIn
        self.assertEqual(
            autofill._pick_hear_about_option(
                ["LinkedIn", "Acme Careers Page", "Other"],
                "Acme",
            ),
            "Acme Careers Page",
        )

        # LinkedIn wins over Job Site when no careers page
        self.assertEqual(
            autofill._pick_hear_about_option(
                ["Job Site", "Social (i.e. LinkedIn)", "Referral", "Other"],
                "Acme",
            ),
            "Social (i.e. LinkedIn)",
        )

        # Falls back to Other when nothing matches
        self.assertEqual(
            autofill._pick_hear_about_option(
                ["Referral", "Billboard", "Other"],
                "Acme",
            ),
            "Other",
        )

    def test_infer_step_handles_how_did_you_hear_text(self):
        """Ensure the broadened fragment also works for text fields."""
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "how_did_you_hear_about_acme",
                "label": "How did you hear about Acme?",
                "required": False,
                "kind": "text",
                "index": 0,
            },
            meta={"jd_source": "https://jobs.gem.com/acme/x?utm_source=GemJobBoardLink", "company_proper": "Acme"},
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(
                how_did_you_hear="",
                comfortable_with_posted_salary=True,
                github="",
                linkedin=None,
                website=None,
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "text")
        self.assertIn("Gemjobboardlink", step["value"])

    def test_infer_step_handles_how_did_you_hear_other_followup_text(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "if_you_answered_other_please_share_how_you_heard_about_us",
                "label": 'If you answered "other," please share how you heard about us',
                "required": False,
                "kind": "text",
                "index": 0,
            },
            meta={
                "jd_source": "https://jobs.gem.com/onesignal/1?utm_source=trueup.io&utm_medium=website&ref=trueup",
                "company_proper": "OneSignal",
            },
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(
                how_did_you_hear="",
                comfortable_with_posted_salary=True,
                github="https://github.com/jerrison",
                linkedin=None,
                website=None,
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "text")
        self.assertEqual(step["value"], "TrueUp")
        self.assertEqual(step["source"], "job_url.utm_source")

    def test_infer_step_handles_selected_other_followup_text_variant(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "if_you_selected_other_please_share_more_details_below",
                "label": "If you selected 'Other,' please share more details below.",
                "required": False,
                "kind": "text",
                "index": 0,
            },
            meta={
                "jd_source": "https://jobs.gem.com/biorender/1?utm_source=trueup.io&utm_medium=website&ref=trueup",
                "company_proper": "BioRender",
            },
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(
                how_did_you_hear="",
                comfortable_with_posted_salary=True,
                github="https://github.com/jerrison",
                linkedin=None,
                website=None,
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "text")
        self.assertEqual(step["value"], "TrueUp")
        self.assertEqual(step["source"], "job_url.utm_source")

    def test_infer_step_handles_working_from_state_radio(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")

        step = autofill._infer_step(
            {
                "field_name": "which_state_would_you_be_working_from",
                "label": "Which state would you be working from?",
                "required": True,
                "kind": "radio",
                "index": 0,
                "options": [
                    "California",
                    "Colorado",
                    "Massachusetts",
                    "New Jersey",
                    "New York",
                    "Oregon",
                    "Other",
                ],
            },
            meta={"jd_source": "https://jobs.gem.com/example/1", "company_proper": "Example"},
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(
                comfortable_with_posted_salary=True,
                github="https://github.com/jerrison",
                linkedin=None,
                website=None,
                location="San Francisco, CA",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "radio")
        self.assertEqual(step["option"], "California")
        self.assertEqual(step["source"], "application_profile.md")

    def test_build_payload_does_not_generate_optional_website_password_field(self):
        autofill = load_module("autofill_gem", "scripts/autofill_gem.py")
        inspection = {
            "title": "Principal PM",
            "fields": [
                {
                    "index": 0,
                    "label": "Email",
                    "required": True,
                    "kind": "text",
                    "options": [],
                },
                {
                    "index": 1,
                    "label": "[Optional] If your portfolio requires a password please include here",
                    "required": False,
                    "kind": "text",
                    "options": [],
                },
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            generate_answers = mock.Mock(return_value={})
            with (
                mock.patch.object(autofill, "migrate_role_output_layout"),
                mock.patch.object(
                    autofill,
                    "load_meta",
                    return_value={
                        "jd_source": "https://jobs.gem.com/example/1",
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
                        linkedin="https://www.linkedin.com/in/jerrisonli/",
                        website="https://jerrison.li",
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "parse_application_profile",
                    return_value=SimpleNamespace(
                        verification_code_email="",
                        github="https://github.com/jerrison",
                        linkedin="https://www.linkedin.com/in/jerrisonli/",
                        website="https://jerrison.li",
                        location="San Francisco, CA",
                        how_did_you_hear="",
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "_inspect_gem_form",
                    return_value=inspection,
                ),
                mock.patch.object(
                    autofill,
                    "generate_application_answers",
                    generate_answers,
                ),
                mock.patch.object(
                    autofill,
                    "find_cover_letter_text",
                    return_value="",
                ),
            ):
                payload = autofill._build_payload(out_dir, provider="openai")

        question_specs = generate_answers.call_args.kwargs["question_specs"]
        self.assertEqual(question_specs, [])
        self.assertEqual(len(payload["unknown_questions"]), 1)
        self.assertEqual(
            payload["unknown_questions"][0]["field_name"],
            "optional_if_your_portfolio_requires_a_password_please_include_here",
        )
        self.assertEqual(payload["unknown_questions"][0]["status"], "unknown_optional")


if __name__ == "__main__":
    unittest.main()
