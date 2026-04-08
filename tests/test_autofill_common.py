# tests/test_autofill_common.py
import importlib.util
import json
import re
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


class BoardFileConstantsTests(unittest.TestCase):
    def test_generates_artifact_filenames_for_gem(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        constants = common.board_file_constants("gem")
        self.assertEqual(constants["report_md"], "gem_autofill_report.md")
        self.assertEqual(constants["report_json"], "gem_autofill_report.json")
        self.assertEqual(constants["pre_submit_screenshot"], "gem_autofill_pre_submit.png")
        self.assertEqual(constants["review_screenshot"], "gem_autofill_review.png")
        self.assertEqual(constants["page_screenshots_dir"], "gem_autofill_pages")
        self.assertEqual(constants["unknown_questions_json"], "gem_unknown_questions.json")
        self.assertEqual(constants["submit_debug_html"], "gem_submit_debug.html")
        self.assertEqual(constants["submit_debug_screenshot"], "gem_submit_debug.png")
        self.assertEqual(constants["payload_json"], "gem_autofill_payload.json")
        self.assertEqual(constants["application_page_html"], "gem_application_page.html")

    def test_generates_artifact_filenames_for_ashby(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        constants = common.board_file_constants("ashby")
        self.assertEqual(constants["report_md"], "ashby_autofill_report.md")
        self.assertEqual(constants["payload_json"], "ashby_autofill_payload.json")


class ReportArtifactContractTests(unittest.TestCase):
    def test_board_constants_report_md_path_survives_reporting_and_cleanup(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        constants = common.board_file_constants("gem")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            artifacts = {
                "report_json": str(out_dir / constants["report_json"]),
                "report_md": str(out_dir / constants["report_md"]),
                "pre_submit_screenshot": str(out_dir / constants["pre_submit_screenshot"]),
            }
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com/job",
                "artifacts": artifacts,
                "unknown_questions": [],
                "steps": [],
            }

            common.write_report(payload, board_name="gem")
            self.assertTrue(Path(artifacts["report_md"]).exists())

            common.clear_current_attempt_artifacts(payload)
            self.assertFalse(Path(artifacts["report_md"]).exists())
            self.assertFalse(Path(artifacts["report_json"]).exists())

    def test_dedupe_page_screenshot_artifacts_removes_duplicate_pre_submit_copy(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            pre_submit = out_dir / "pre_submit.png"
            page_one = out_dir / "page_01.png"
            page_two = out_dir / "page_02.png"
            pre_submit.write_bytes(b"same-image")
            page_one.write_bytes(b"same-image")
            page_two.write_bytes(b"different-image")

            kept = common.dedupe_page_screenshot_artifacts(
                [str(page_one), str(page_two)],
                pre_submit_screenshot=str(pre_submit),
            )

            self.assertEqual(kept, [str(page_two)])
            self.assertFalse(page_one.exists())
            self.assertTrue(page_two.exists())

    def test_dedupe_page_screenshot_artifacts_removes_near_duplicate_adjacent_pages(self):
        from PIL import Image, ImageDraw

        common = load_module("autofill_common", "scripts/autofill_common.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            page_one = out_dir / "page_01.png"
            page_two = out_dir / "page_02.png"
            page_three = out_dir / "page_03.png"

            base = Image.new("RGB", (320, 200), "white")
            draw = ImageDraw.Draw(base)
            draw.rectangle((16, 16, 304, 56), outline="black", width=2)
            draw.text((24, 24), "Self Identify", fill="black")
            draw.text((24, 92), "No, I do not have a disability", fill="black")
            base.save(page_one)

            near_duplicate = base.copy()
            near_duplicate.putpixel((310, 190), (240, 240, 240))
            near_duplicate.save(page_two)

            distinct = Image.new("RGB", (320, 200), "white")
            draw = ImageDraw.Draw(distinct)
            draw.rectangle((16, 16, 304, 56), outline="black", width=2)
            draw.text((24, 24), "Review", fill="black")
            draw.text((24, 92), "Submit", fill="black")
            distinct.save(page_three)

            kept = common.dedupe_page_screenshot_artifacts(
                [str(page_one), str(page_two), str(page_three)],
            )

            self.assertEqual(kept, [str(page_one), str(page_three)])
            self.assertTrue(page_one.exists())
            self.assertFalse(page_two.exists())
            self.assertTrue(page_three.exists())

    def test_clear_current_attempt_artifacts_removes_stale_result_and_debug_outputs(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            pages_dir = submit_dir / "pages"
            submit_dir.mkdir(parents=True)
            pages_dir.mkdir()
            payload = {
                "out_dir": str(out_dir),
                "artifacts": {
                    "report_json": str(submit_dir / "report.json"),
                    "report_markdown": str(submit_dir / "report.md"),
                    "pre_submit_screenshot": str(submit_dir / "pre_submit.png"),
                    "review_screenshot": str(submit_dir / "review.png"),
                    "submit_debug_html": str(submit_dir / "debug.html"),
                    "submit_debug_screenshot": str(submit_dir / "debug.png"),
                    "payload_json": str(submit_dir / "payload.json"),
                    "unknown_questions_json": str(submit_dir / "unknown.json"),
                    "page_screenshots_dir": str(pages_dir),
                    "application_answers_raw": str(submit_dir / "application_answers_raw.txt"),
                    "application_answers_fallback_raw": str(submit_dir / "application_answers_fallback_raw.txt"),
                    "answer_verification_json": str(submit_dir / "answer_verification.json"),
                    "answer_verification_raw": str(submit_dir / "answer_verification_raw.txt"),
                },
            }
            for key in (
                "report_json",
                "report_markdown",
                "pre_submit_screenshot",
                "review_screenshot",
                "submit_debug_html",
                "submit_debug_screenshot",
                "application_answers_raw",
                "application_answers_fallback_raw",
                "answer_verification_json",
                "answer_verification_raw",
            ):
                Path(payload["artifacts"][key]).write_text("stale", encoding="utf-8")
            Path(payload["artifacts"]["payload_json"]).write_text("{}", encoding="utf-8")
            Path(payload["artifacts"]["unknown_questions_json"]).write_text("[]", encoding="utf-8")
            (submit_dir / "application_answers.json").write_text("{}", encoding="utf-8")
            (submit_dir / "application_submission_result.json").write_text("{}", encoding="utf-8")
            (pages_dir / "page_01.png").write_text("png", encoding="utf-8")

            common.clear_current_attempt_artifacts(payload)

            self.assertFalse(Path(payload["artifacts"]["report_json"]).exists())
            self.assertFalse(Path(payload["artifacts"]["report_markdown"]).exists())
            self.assertFalse(Path(payload["artifacts"]["pre_submit_screenshot"]).exists())
            self.assertFalse(Path(payload["artifacts"]["review_screenshot"]).exists())
            self.assertFalse(Path(payload["artifacts"]["submit_debug_html"]).exists())
            self.assertFalse(Path(payload["artifacts"]["submit_debug_screenshot"]).exists())
            self.assertFalse(Path(payload["artifacts"]["application_answers_raw"]).exists())
            self.assertFalse(Path(payload["artifacts"]["application_answers_fallback_raw"]).exists())
            self.assertFalse(Path(payload["artifacts"]["answer_verification_json"]).exists())
            self.assertFalse(Path(payload["artifacts"]["answer_verification_raw"]).exists())
            self.assertFalse((submit_dir / "application_answers.json").exists())
            self.assertFalse((submit_dir / "application_submission_result.json").exists())
            self.assertFalse((pages_dir / "page_01.png").exists())
            self.assertTrue(Path(payload["artifacts"]["payload_json"]).exists())
            self.assertTrue(Path(payload["artifacts"]["unknown_questions_json"]).exists())


class LabelMatchesTests(unittest.TestCase):
    def test_substring_match_with_string(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        self.assertTrue(common.label_matches("First Name", "first name"))
        self.assertFalse(common.label_matches("First Name", "last name"))

    def test_substring_match_with_dict(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        field = {"label": "LinkedIn Profile URL"}
        self.assertTrue(common.label_matches(field, "linkedin"))
        self.assertFalse(common.label_matches(field, "github"))

    def test_multiple_fragments_any_match(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        self.assertTrue(common.label_matches("Email Address", "email", "phone"))
        self.assertFalse(common.label_matches("Full Name", "email", "phone"))

    def test_word_boundary_mode(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        field = {"label": "I identify my ethnicity as: Select all that apply"}
        self.assertTrue(common.label_matches(field, "ethnicity", word_boundary=True))
        self.assertFalse(common.label_matches(field, "city", word_boundary=True))

    def test_word_boundary_uses_alphanumeric_lookaround_not_backslash_b(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        # \b treats underscore as word char; our pattern should NOT
        self.assertTrue(common.label_matches("some_city value", "city", word_boundary=True))


class SelectOptionTests(unittest.TestCase):
    def test_exact_match(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        self.assertEqual(common.select_option(["Yes", "No"], "Yes"), "Yes")

    def test_case_insensitive_match(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        self.assertEqual(common.select_option(["Male", "Female", "Non-binary"], "male"), "Male")

    def test_substring_match(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        result = common.select_option(
            ["Yes - I require sponsorship", "No - I do not require sponsorship"],
            "No",
        )
        self.assertIn("No", result)

    def test_no_match_returns_none(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        self.assertIsNone(common.select_option(["Yes", "No"], "Maybe"))

    def test_does_not_match_negated_multiword_substring(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        self.assertIsNone(
            common.select_option(
                [
                    "American Indian or Alaska Native (Not Hispanic or Latino) (United States of America)",
                    "Asian (Not Hispanic or Latino) (United States of America)",
                ],
                "Hispanic or Latino",
            )
        )


class SelectInputChoiceTests(unittest.TestCase):
    def test_select_input_choice_matches_shared_work_authorization_policy_option(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        submit_common = load_module("application_submit_common", "scripts/application_submit_common.py")
        application_profile = submit_common.parse_application_profile(
            """
## Work Authorization
- Country: United States
- Location: San Francisco, CA
- Work Authorization Statement: I am always authorized to work in the United States unconditionally.
- Authorized to Work Unconditionally: Yes
- Require Sponsorship Now: No
- Require Sponsorship in Future: No
- Sponsorship Answer: No, I do not require sponsorship now or in the future. I am always authorized to work in the United States unconditionally.
- Gender: Male
- Race or Ethnicity: Hispanic or Latino
- Veteran Status: I am not a protected veteran
- Disability Status: No, I do not have a disability and have not had one in the past
- Sexual Orientation: Straight / Heterosexual
"""
        )

        with mock.patch.object(common, "_load_select_matching_application_profile", return_value=application_profile):
            choice = common._select_input_choice(
                [
                    "Select an option...",
                    "I am authorized to work in the country in which this job will be performed",
                    "I will require visa sponsorship to work in the country in which this job will be performed",
                ],
                label="Work authorization",
                field_name="work_authorization",
                value="Yes",
            )

        self.assertEqual(choice, "I am authorized to work in the country in which this job will be performed")

    def test_fill_select_input_combobox_uses_resolved_choice_before_selecting(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        selected_labels: list[str] = []

        class EmptyLocator:
            first = None

            def count(self):
                return 0

            def click(self):
                raise AssertionError("combobox fallback should not click option list when select_option succeeds")

            def fill(self, _value):
                raise AssertionError("combobox fallback should not type when select_option succeeds")

            def press(self, _key):
                raise AssertionError("combobox fallback should not type when select_option succeeds")

        EmptyLocator.first = EmptyLocator()

        class FakeOptionText:
            def __init__(self, text):
                self._text = text

            def inner_text(self):
                return self._text

        class FakeOptions:
            def __init__(self, texts):
                self._texts = list(texts)

            def count(self):
                return len(self._texts)

            def nth(self, index):
                return FakeOptionText(self._texts[index])

        class FakeCombobox:
            def __init__(self, option_texts):
                self._option_texts = option_texts
                self.first = self

            def count(self):
                return 1

            def scroll_into_view_if_needed(self):
                return None

            def locator(self, selector):
                assert selector == "option"
                return FakeOptions(self._option_texts)

            def select_option(self, *, label):
                selected_labels.append(label)

        class FakePage:
            def __init__(self):
                self._combobox = FakeCombobox(
                    [
                        "Select an option...",
                        "I am authorized to work in the country in which this job will be performed",
                        "I will require visa sponsorship to work in the country in which this job will be performed",
                    ]
                )

            def get_by_label(self, _pattern):
                return EmptyLocator()

            def locator(self, _selector):
                return EmptyLocator()

            def get_by_role(self, role, name=None):
                if role == "combobox":
                    assert getattr(name, "search", None) is not None
                    return self._combobox
                if role == "option":
                    return EmptyLocator()
                raise AssertionError(f"unexpected role lookup: {role}")

            def wait_for_timeout(self, _ms):
                return None

        with mock.patch.object(
            common,
            "_select_input_choice",
            return_value="I am authorized to work in the country in which this job will be performed",
        ) as choice_mock:
            filled = common._fill_select_input(
                FakePage(),
                label="Work authorization",
                field_name="work_authorization",
                value="Yes",
            )

        self.assertTrue(filled)
        self.assertEqual(selected_labels, ["I am authorized to work in the country in which this job will be performed"])
        choice_mock.assert_called()


class RadioFallbackTests(unittest.TestCase):
    def test_named_radio_falls_back_to_clicking_associated_label_when_input_is_hidden(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        clicked: list[str] = []

        class EmptyLocator:
            first = None

            def count(self):
                return 0

        EmptyLocator.first = EmptyLocator()

        class FakeLabel:
            def __init__(self):
                self.first = self

            def count(self):
                return 1

            def scroll_into_view_if_needed(self):
                return None

            def click(self):
                clicked.append("label")

            def inner_text(self):
                return "Male"

        class FakeRadio:
            def get_attribute(self, name):
                if name == "value":
                    return "Male"
                if name == "id":
                    return "jv-field-f3-0"
                return None

            def scroll_into_view_if_needed(self):
                raise RuntimeError("hidden")

            def check(self):
                raise RuntimeError("hidden")

            def click(self):
                raise RuntimeError("hidden")

        class FakeRadioCollection:
            def __init__(self):
                self._radio = FakeRadio()

            def count(self):
                return 1

            def nth(self, index):
                assert index == 0
                return self._radio

        class FakePage:
            def locator(self, selector):
                if selector == 'input[type="radio"][name="f3"]':
                    return FakeRadioCollection()
                if selector == 'label[for="jv-field-f3-0"]':
                    return FakeLabel()
                raise AssertionError(selector)

            def get_by_role(self, role, name=None):
                del role, name
                return EmptyLocator()

            def get_by_label(self, pattern):
                del pattern
                return EmptyLocator()

        self.assertTrue(common._set_radio(FakePage(), label="Gender", field_name="f3", value="Male"))
        self.assertEqual(clicked, ["label"])


class LiveDraftProofHelpersTests(unittest.TestCase):
    def test_detect_live_required_unfilled_fields_filters_known_steps(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        class FakePage:
            def evaluate(self, script):
                self.script = script
                return [
                    {"field_name": "company", "label": "Company*", "kind": "text"},
                    {"field_name": "country", "label": "Country*", "kind": "select"},
                    {
                        "field_name": "graduation_month_year",
                        "label": "Graduation Month and Year (MM/YYYY)*",
                        "kind": "text",
                    },
                ]

        blockers = common.detect_live_required_unfilled_fields(
            FakePage(),
            steps=[
                {"field_name": "company", "label": "Company", "kind": "text"},
                {"field_name": "country", "label": "Country", "kind": "select"},
            ],
        )

        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0]["field_name"], "graduation_month_year")
        self.assertEqual(blockers[0]["source"], "live_application_form")
        self.assertIn("visible required", blockers[0]["note"].casefold())

    def test_simple_board_review_boundary_blocker_requires_final_review_boundary(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        class FakePage:
            def __init__(self, visible_buttons):
                self.visible_buttons = visible_buttons

            def evaluate(self, script):
                self.script = script
                return list(self.visible_buttons)

        blocker = common.simple_board_review_boundary_blocker(FakePage(["Back", "Next →"]))
        self.assertIsNotNone(blocker)
        assert blocker is not None
        self.assertEqual(blocker["field_name"], "final_review_boundary")

        self.assertIsNone(common.simple_board_review_boundary_blocker(FakePage(["Submit Application"])))


class SharedPolicyOptionTests(unittest.TestCase):
    def test_explicit_us_state_list_membership_answer_uses_candidate_state_against_abbreviation_list(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        result = common.explicit_us_state_list_membership_answer(
            "Do you currently reside in any of the following states: DE, HI, IA, KY, MS, NE, NM, SD, VT, WV, WY?",
            application_profile=SimpleNamespace(location="San Francisco, CA"),
        )

        self.assertFalse(result)

    def test_select_shared_policy_option_matches_location_based_na_work_authorization_choice(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        matched = common.select_shared_policy_option(
            [
                "N/A - I am based in the United States",
                "I will require sponsorship in the future for employment in Canada",
                "I do not require sponsorship in the future for employment in Canada",
            ],
            SimpleNamespace(category="work_authorization", boolean_value=True, text_value="Yes"),
            application_profile=SimpleNamespace(country="United States", location="San Francisco, CA"),
        )

        self.assertEqual(matched, "N/A - I am based in the United States")

    def test_select_shared_policy_option_matches_negative_canada_authorization_choice(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        matched = common.select_shared_policy_option(
            [
                "I am authorized to work in the country due to my nationality",
                "I am authorized to work in the country based on a valid work permit and do not need a company to sponsor my visa",
                "I am authorized to work in the country based on a valid work permit which needs to be sponsored by the company I work for",
                "I am not authorized to work in the country and need visa support",
            ],
            SimpleNamespace(category="work_authorization", boolean_value=False, text_value="No"),
            application_profile=SimpleNamespace(location="San Francisco, CA"),
        )

        self.assertEqual(matched, "I am not authorized to work in the country and need visa support")

    def test_select_shared_policy_option_matches_plain_yes_no_work_authorization_choice(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        matched = common.select_shared_policy_option(
            ["Yes", "No"],
            SimpleNamespace(
                category="work_authorization",
                boolean_value=True,
                text_value="I am always authorized to work in the United States unconditionally.",
            ),
            application_profile=SimpleNamespace(location="San Francisco, CA"),
        )

        self.assertEqual(matched, "Yes")

    def test_select_shared_policy_option_matches_negative_prior_employment_choice(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        matched = common.select_shared_policy_option(
            [
                "Employee",
                "Intern",
                "Temporary Agency or Vendor",
                "Other",
                "I have not worked for Adobe in the past.",
            ],
            SimpleNamespace(category="prior_employment", boolean_value=False, text_value="No"),
            application_profile=SimpleNamespace(location="San Francisco, CA"),
        )

        self.assertEqual(matched, "I have not worked for Adobe in the past.")

    def test_select_shared_policy_option_matches_never_employed_or_services_choice(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        matched = common.select_shared_policy_option(
            [
                "I have never been employed by Rubrik or contracted to provide services to Rubrik",
                "I am currently a full/part-time employee for Rubrik",
                "I am a currently a contractor for Rubrik",
            ],
            SimpleNamespace(category="prior_employment", boolean_value=False, text_value="No"),
            application_profile=SimpleNamespace(location="San Francisco, CA"),
        )

        self.assertEqual(
            matched,
            "I have never been employed by Rubrik or contracted to provide services to Rubrik",
        )

    def test_select_shared_policy_option_matches_positive_interview_recording_consent_choice(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        matched = common.select_shared_policy_option(
            [
                "I do not consent.",
                "I understand and provide consent.",
            ],
            SimpleNamespace(category="interview_recording_consent", boolean_value=True, text_value="Yes"),
            application_profile=SimpleNamespace(location="San Francisco, CA"),
        )

        self.assertEqual(matched, "I understand and provide consent.")

    def test_select_shared_policy_option_matches_two_week_availability_choice(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        matched = common.select_shared_policy_option(
            [
                "Immediately",
                "Within 2 weeks",
                "Within 1 month",
            ],
            SimpleNamespace(category="availability_timing", boolean_value=None, text_value="2 weeks from the application time"),
            application_profile=SimpleNamespace(location="San Francisco, CA"),
        )

        self.assertEqual(matched, "Within 2 weeks")

    def test_select_shared_policy_option_matches_numeric_years_range_choice(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        matched = common.select_shared_policy_option(
            [
                "0-2 years",
                "3-5 years",
                "5-7 years",
                "8+ years",
            ],
            SimpleNamespace(category="skill_years_experience", boolean_value=None, text_value="7"),
            application_profile=SimpleNamespace(location="San Francisco, CA"),
        )

        self.assertEqual(matched, "5-7 years")

    def test_select_shared_policy_option_matches_plus_years_choice(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        matched = common.select_shared_policy_option(
            [
                "Less than 3 years",
                "3-4 years",
                "5-7 years",
                "7+ years",
            ],
            SimpleNamespace(category="skill_years_experience", boolean_value=None, text_value="10"),
            application_profile=SimpleNamespace(location="San Francisco, CA"),
        )

        self.assertEqual(matched, "7+ years")

    def test_select_shared_policy_option_matches_less_than_years_choice(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        matched = common.select_shared_policy_option(
            [
                "Less than 3 years",
                "3-4 years",
                "5-7 years",
                "7+ years",
            ],
            SimpleNamespace(category="skill_years_experience", boolean_value=None, text_value="2"),
            application_profile=SimpleNamespace(location="San Francisco, CA"),
        )

        self.assertEqual(matched, "Less than 3 years")

    def test_select_shared_policy_option_matches_city_location_choice_to_candidate_city(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        matched = common.select_shared_policy_option(
            [
                "San Francisco, CA",
                "New York City, NY",
            ],
            SimpleNamespace(category="city_location", boolean_value=None, text_value="San Francisco, CA"),
            application_profile=SimpleNamespace(location="San Francisco, CA"),
        )

        self.assertEqual(matched, "San Francisco, CA")

    def test_none_options_returns_none(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        self.assertIsNone(common.select_option(None, "Yes"))

    def test_none_answer_returns_none(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        self.assertIsNone(common.select_option(["Yes", "No"], None))

    def test_filter_select_prefix(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        result = common.select_option(
            ["Select an option", "Yes", "No"],
            "Yes",
            filter_select_prefix=True,
        )
        self.assertEqual(result, "Yes")

    def test_select_option_does_not_match_short_token_inside_unrelated_word(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        result = common.select_option(
            ["Currently reside in a hub", "Open to relocating to any hub"],
            "CA",
        )

        self.assertIsNone(result)

    def test_select_location_positive_fit_option_prefers_current_city_over_relocation(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        result = common.select_location_positive_fit_option(
            [
                "You currently live in San Francisco",
                "You are looking to relocate to San Francisco",
            ],
            application_profile=SimpleNamespace(
                location="San Francisco, CA",
                lives_in_job_location=True,
            ),
        )

        self.assertEqual(result, "You currently live in San Francisco")

    def test_select_location_positive_fit_option_uses_relocation_when_candidate_is_not_local(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        result = common.select_location_positive_fit_option(
            [
                "You currently live in San Francisco",
                "You are looking to relocate to San Francisco",
            ],
            application_profile=SimpleNamespace(
                location="Los Angeles, CA",
                lives_in_job_location=False,
            ),
        )

        self.assertEqual(result, "You are looking to relocate to San Francisco")

    def test_select_location_positive_fit_option_prefers_current_hub_when_context_mentions_candidate_city(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        result = common.select_location_positive_fit_option(
            [
                "Currently reside in a hub",
                "Open to relocating to any hub",
                "Only considering remote",
            ],
            application_profile=SimpleNamespace(
                location="San Francisco, CA",
                lives_in_job_location=False,
            ),
            context_text="We prefer employees to sit out of our hubs in NYC, SF, Austin or DC.",
        )

        self.assertEqual(result, "Currently reside in a hub")

    def test_select_profile_option_matches_gender_identity_alias(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        result = common.select_profile_option(
            ["Man", "Woman", "Non-Binary"],
            "Cisgender Male/Man",
            profile_field="gender_identity",
        )

        self.assertEqual(result, "Man")

    def test_select_profile_option_matches_veteran_and_disability_no_aliases(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        veteran = common.select_profile_option(
            [
                "Yes, I am a veteran or active member",
                "No, I am not a veteran or active member",
            ],
            "I am not a protected veteran",
            profile_field="veteran_status",
        )
        disability = common.select_profile_option(
            ["Yes", "No", "I don't wish to answer"],
            "No, I do not have a disability and have not had one in the past",
            profile_field="disability_status",
        )

        self.assertEqual(veteran, "No, I am not a veteran or active member")
        self.assertEqual(disability, "No")

    def test_select_profile_option_matches_disability_none_of_these_apply_alias(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        disability = common.select_profile_option(
            [
                "Blindness",
                "Chronic illness or pain",
                "None of these apply",
            ],
            "No, I do not have a disability and have not had one in the past",
            profile_field="disability_status",
        )

        self.assertEqual(disability, "None of these apply")

    def test_select_profile_option_uses_truthful_decline_for_split_race_prompts_without_exact_match(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        result = common.select_profile_option(
            [
                "American Indian or Alaska Native",
                "Asian",
                "Black or African American",
                "I do not wish to answer. (United States of America)",
                "Native Hawaiian or Other Pacific Islander",
                "Two or More Races",
                "White",
            ],
            "Hispanic or Latino",
            profile_field="race_or_ethnicity",
        )

        self.assertEqual(result, "I do not wish to answer. (United States of America)")

    def test_select_profile_option_matches_workday_decline_to_declare_race_alias(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        result = common.select_profile_option(
            [
                "American Indian or Alaska Native (United States of America)",
                "Asian (United States of America)",
                "Do not wish to declare (United States of America)",
                "White (United States of America)",
            ],
            "Hispanic or Latino",
            profile_field="race_or_ethnicity",
        )

        self.assertEqual(result, "Do not wish to declare (United States of America)")

    def test_select_profile_option_prefers_exact_race_match_over_negated_demographic_options(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        result = common.select_profile_option(
            [
                "American Indian or Alaska Native (Not Hispanic or Latino) (United States of America)",
                "Asian (Not Hispanic or Latino) (United States of America)",
                "Hispanic or Latino (United States of America)",
                "I do not wish to answer. (United States of America)",
            ],
            "Hispanic or Latino",
            profile_field="race_or_ethnicity",
        )

        self.assertEqual(result, "Hispanic or Latino (United States of America)")


class UnknownQuestionDraftBlockerMetadataTests(unittest.TestCase):
    def test_marks_optional_visible_self_id_unknown_as_draft_blocker(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        metadata = common.infer_unknown_question_blocker_metadata(
            field_name="survey_1_what_is_your_gender_identity",
            label="What is your gender identity?",
            application_profile=SimpleNamespace(
                gender="Male",
                gender_identity="Cisgender Male/Man",
            ),
        )

        self.assertEqual(
            metadata,
            {
                "blocks_draft_completion": True,
                "blocker_kind": common.VISIBLE_SELF_ID_BLOCKER_KIND,
                "profile_field": "gender_identity",
            },
        )

    def test_ignores_optional_unknown_without_truthful_source(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        metadata = common.infer_unknown_question_blocker_metadata(
            field_name="application_name_pronunciation",
            label="Name Pronunciation",
            application_profile=SimpleNamespace(
                pronouns="He / Him / His",
                gender="Male",
            ),
        )

        self.assertEqual(metadata, {})

    def test_ignores_if_other_pronouns_follow_up(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        metadata = common.infer_unknown_question_blocker_metadata(
            field_name="application_if_other_please_let_us_know_your_pronouns",
            label="If other, please let us know your pronouns",
            application_profile=SimpleNamespace(
                pronouns="He / Him / His",
            ),
        )

        self.assertEqual(metadata, {})

    def test_ignores_optional_disability_accommodation_follow_up_when_not_applicable(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        metadata = common.infer_unknown_question_blocker_metadata(
            field_name="application_clickup_accommodation",
            label=(
                "If you have a disability as defined by applicable laws or regulations and need an "
                "accommodation to enable you to go through our recruitment process to be considered "
                "for an open role, please describe your requested accommodation(s). If this does not "
                "apply to you, please skip this question."
            ),
            application_profile=SimpleNamespace(
                disability_status="No, I do not have a disability and have not had one in the past",
            ),
        )

        self.assertEqual(metadata, {})


class MatchPriorEmployerOptionTests(unittest.TestCase):
    def test_matches_never_employed_or_contracted_negative_option(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        option = common.match_prior_employer_option(
            [
                "I am currently a full-time employee for Rubrik",
                "I am currently a contractor for Rubrik",
                "I have never been employed by Rubrik or contracted to provide services to Rubrik",
            ],
            has_worked_for_company=False,
        )

        self.assertEqual(
            option,
            "I have never been employed by Rubrik or contracted to provide services to Rubrik",
        )


class WriteReportTests(unittest.TestCase):
    def test_splits_filled_and_planned_entries(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com/job",
                "application_url": "https://example.com/apply",
                "artifacts": {
                    "report_json": str(out_dir / "report.json"),
                    "report_markdown": str(out_dir / "report.md"),
                    "pre_submit_screenshot": str(out_dir / "pre_submit.png"),
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
                        "value": "test@test.com",
                        "source": "master_resume.md",
                        "required": True,
                        "filled": True,
                    },
                    {
                        "field_name": "cover_letter",
                        "label": "Cover Letter",
                        "kind": "file",
                        "file_path": "/tmp/cover.pdf",
                        "source": "existing_cover_letter_asset",
                        "required": False,
                    },
                ],
            }
            common.write_report(payload, board_name="gem", runtime=runtime)
            saved = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))

        self.assertEqual(len(saved["fields"]), 1)
        self.assertEqual(saved["fields"][0]["field_name"], "email")
        self.assertEqual(saved["planned_but_unconfirmed_fields"][0]["field_name"], "cover_letter")

    def test_blocking_policy_keeps_optional_non_self_id_non_blocking_but_self_id_blocking(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        optional_cover_letter = {
            "field_name": "cover_letter",
            "label": "Cover Letter",
            "kind": "file",
            "required": False,
            "status": "planned",
        }
        visible_self_id = common.mark_visible_self_id_step(
            {
                "field_name": "race_ethnicity",
                "label": "Race or Ethnicity",
                "kind": "choice",
                "required": False,
                "status": "planned",
                "value": "Hispanic or Latino",
            },
            profile_field="race_or_ethnicity",
        )

        blockers = common.blocking_unconfirmed_report_entries(
            [
                common._report_entry(optional_cover_letter),
                common._report_entry(visible_self_id),
            ]
        )

        self.assertEqual([entry["field_name"] for entry in blockers], ["race_ethnicity"])
        self.assertTrue(blockers[0]["blocks_draft_completion"])
        self.assertEqual(blockers[0]["blocker_kind"], common.VISIBLE_SELF_ID_BLOCKER_KIND)

    def test_generalized_blocker_helpers_support_profile_fields_and_artifacts(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        visible_location = common.mark_visible_profile_field_step(
            {
                "field_name": "candidate_location",
                "label": "Current Location",
                "kind": "text",
                "required": False,
                "status": "planned",
                "value": "San Francisco, CA",
            },
            profile_field="location",
        )
        missing_screenshot = common.required_artifact_blocker_step(
            field_name="pre_submit_screenshot",
            label="Pre-submit Screenshot",
            source="autofill_pipeline",
            artifact_key="pre_submit_screenshot",
            expected_path="/tmp/greenhouse_autofill_pre_submit.png",
            reason="Screenshot proof is missing.",
        )

        blockers = common.blocking_unconfirmed_report_entries(
            [
                common._report_entry(visible_location),
                common._report_entry(missing_screenshot),
            ]
        )

        self.assertEqual(
            [entry["field_name"] for entry in blockers],
            ["candidate_location", "pre_submit_screenshot"],
        )
        self.assertEqual(blockers[0]["blocker_kind"], common.VISIBLE_PROFILE_FIELD_BLOCKER_KIND)
        self.assertEqual(blockers[0]["profile_field"], "location")
        self.assertEqual(blockers[1]["blocker_kind"], common.REQUIRED_ARTIFACT_BLOCKER_KIND)
        self.assertEqual(blockers[1]["artifact_key"], "pre_submit_screenshot")
        self.assertEqual(blockers[1]["value"], "/tmp/greenhouse_autofill_pre_submit.png")

    def test_markdown_header_uses_board_name(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com/job",
                "application_url": "https://example.com/apply",
                "artifacts": {
                    "report_json": str(out_dir / "report.json"),
                    "report_markdown": str(out_dir / "report.md"),
                    "pre_submit_screenshot": str(out_dir / "pre_submit.png"),
                },
                "unknown_questions": [],
                "steps": [],
            }
            common.write_report(payload, board_name="lever")
            md = (out_dir / "report.md").read_text(encoding="utf-8")

        self.assertTrue(md.startswith("# Lever Autofill Report"))

    def test_write_report_includes_optional_outcomes(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com/job",
                "artifacts": {
                    "report_json": str(out_dir / "report.json"),
                    "report_markdown": str(out_dir / "report.md"),
                    "pre_submit_screenshot": str(out_dir / "pre_submit.png"),
                },
                "unknown_questions": [],
                "steps": [],
            }
            runtime = {
                "steps": [],
                "outcomes": [
                    {
                        "name": "resume_upload",
                        "status": "verified_fresh_upload",
                        "expected_file": "Candidate Name Resume - Acme.pdf",
                        "message": "LinkedIn re-uploaded the current role resume.",
                    }
                ],
            }
            common.write_report(payload, board_name="linkedin", runtime=runtime)
            saved = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
            markdown = (out_dir / "report.md").read_text(encoding="utf-8")

        self.assertEqual(saved["outcomes"][0]["status"], "verified_fresh_upload")
        self.assertIn("## Outcomes", markdown)
        self.assertIn("Candidate Name Resume - Acme.pdf", markdown)

    def test_write_report_redacts_passwords_and_prefers_report_value(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com/job",
                "artifacts": {
                    "report_json": str(out_dir / "report.json"),
                    "report_markdown": str(out_dir / "report.md"),
                    "pre_submit_screenshot": str(out_dir / "pre_submit.png"),
                },
                "unknown_questions": [],
                "steps": [],
            }
            runtime = {
                "steps": [
                    {
                        "field_name": "password",
                        "label": "Password",
                        "kind": "text",
                        "value": "SuperSecret123!",
                        "source": "env",
                        "filled": True,
                    },
                    {
                        "field_name": "security_code",
                        "label": "Security code",
                        "kind": "text",
                        "value": "ABCD1234",
                        "report_value": "[redacted 8-character code]",
                        "source": "gmail",
                        "filled": True,
                    },
                ],
            }
            common.write_report(payload, board_name="avature", runtime=runtime)
            saved = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
            markdown = (out_dir / "report.md").read_text(encoding="utf-8")

        values = {field["field_name"]: field["value"] for field in saved["fields"]}
        self.assertEqual(values["password"], "[redacted password]")
        self.assertEqual(values["security_code"], "[redacted 8-character code]")
        self.assertIn("[redacted password]", markdown)
        self.assertNotIn("SuperSecret123!", markdown)


class CaptureFullPageTests(unittest.TestCase):
    def test_uses_preferred_selector_when_found(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        screenshots = []

        class FakeLocator:
            def count(self):
                return 1

            def screenshot(self, path):
                screenshots.append(path)

        class FakeLocatorResult:
            first = FakeLocator()

        class FakePage:
            def locator(self, selector):
                return FakeLocatorResult()

            def screenshot(self, path, full_page=False):
                screenshots.append(("full_page", path))

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sub" / "screenshot.png"
            common.capture_full_page(FakePage(), path, preferred_selectors=("#my-form",))

        self.assertEqual(len(screenshots), 1)
        self.assertEqual(screenshots[0], str(path))

    def test_hides_transient_dropdown_overlays_around_preferred_selector_capture(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        events = []

        class FakeLocator:
            def count(self):
                return 1

            def screenshot(self, path, type="png"):
                del type
                events.append(("locator_screenshot", path))

        class FakeLocatorResult:
            first = FakeLocator()

        class FakePage:
            def locator(self, selector):
                return FakeLocatorResult()

            def evaluate(self, script):
                if ".dropdown-results" in script:
                    events.append(("prepare", None))
                elif "data-capture-overlay-hidden" in script:
                    events.append(("restore", None))

            def wait_for_timeout(self, timeout_ms):
                del timeout_ms

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sub" / "screenshot.png"
            common.capture_full_page(FakePage(), path, preferred_selectors=("#application-form",))

        self.assertEqual(
            events,
            [
                ("prepare", None),
                ("locator_screenshot", str(path)),
                ("restore", None),
            ],
        )

    def test_hides_fixed_and_sticky_overlays_around_preferred_selector_capture(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        events = []

        class FakeLocator:
            def count(self):
                return 1

            def screenshot(self, path, type="png"):
                del type
                events.append(("locator_screenshot", path))

        class FakeLocatorResult:
            first = FakeLocator()

        class FakePage:
            def locator(self, selector):
                del selector
                return FakeLocatorResult()

            def evaluate(self, script):
                if "captureFixedHidden" in script and "querySelectorAll('*')" in script:
                    events.append(("prepare_fixed", None))
                elif "data-capture-fixed-hidden" in script:
                    events.append(("restore_fixed", None))
                elif ".dropdown-results" in script:
                    events.append(("prepare_transient", None))
                elif "data-capture-overlay-hidden" in script:
                    events.append(("restore_transient", None))

            def wait_for_timeout(self, timeout_ms):
                del timeout_ms

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sub" / "screenshot.png"
            common.capture_full_page(FakePage(), path, preferred_selectors=("#application-form",))

        self.assertEqual(
            events,
            [
                ("prepare_transient", None),
                ("prepare_fixed", None),
                ("locator_screenshot", str(path)),
                ("restore_fixed", None),
                ("restore_transient", None),
            ],
        )

    def test_hides_full_viewport_captcha_iframe_around_preferred_selector_capture(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        from PIL import Image
        from playwright.sync_api import sync_playwright

        html = """
        <style>
          body { margin: 0; background: #ffffff; }
          #application-form {
            width: 420px;
            height: 260px;
            background: rgb(255, 0, 0);
            margin: 24px;
          }
          iframe.challenge {
            position: fixed;
            inset: 0;
            width: 100vw;
            height: 100vh;
            border: 0;
            z-index: 2147483647;
          }
        </style>
        <div id="application-form"></div>
        <iframe
          class="challenge"
          title="Widget containing checkbox for hCaptcha security challenge"
          src="data:text/html,<body style='margin:0;background:rgb(0,0,255)'></body>"
        ></iframe>
        """

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "captcha-hidden.png"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 800, "height": 600})
                page.set_content(html)
                page.wait_for_timeout(250)

                common.capture_full_page(page, path, preferred_selectors=("#application-form",))

                with Image.open(path) as captured:
                    center_pixel = captured.convert("RGB").getpixel((captured.width // 2, captured.height // 2))
                browser.close()

        self.assertGreater(center_pixel[0], 200)
        self.assertLess(center_pixel[1], 80)
        self.assertLess(center_pixel[2], 80)

    def test_falls_back_to_full_page(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        screenshots = []

        class FakeLocator:
            def count(self):
                return 0

        class FakeLocatorResult:
            first = FakeLocator()

        class FakePage:
            def locator(self, selector):
                return FakeLocatorResult()

            def screenshot(self, path, full_page=False):
                screenshots.append(("full_page", path, full_page))

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "screenshot.png"
            common.capture_full_page(FakePage(), path)

        self.assertEqual(len(screenshots), 1)
        self.assertTrue(screenshots[0][2])  # full_page=True

    def test_capture_scrollable_locator_screenshot_stitches_vertical_segments(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        class FakeScrollableLocator:
            def __init__(self):
                self.scroll_top = 0
                self.screenshot_scroll_positions: list[int] = []

            def evaluate(self, script, arg=None):
                if "scrollHeight" in script:
                    return {
                        "scrollHeight": 30,
                        "clientHeight": 10,
                        "scrollTop": 0,
                        "devicePixelRatio": 1,
                    }
                if "node.scrollTop" in script:
                    self.scroll_top = int(arg or 0)
                    return self.scroll_top
                raise AssertionError(f"Unexpected script: {script}")

            def screenshot(self, path=None, type="png"):
                del type
                self.screenshot_scroll_positions.append(self.scroll_top)
                import io

                from PIL import Image

                image = Image.new("RGB", (12, 10), (self.scroll_top, 0, 0))
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                data = buffer.getvalue()
                if path is not None:
                    Path(path).write_bytes(data)
                    return None
                return data

        class FakePage:
            def wait_for_timeout(self, timeout_ms: int) -> None:
                del timeout_ms

        locator = FakeScrollableLocator()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "stitched.png"
            common.capture_scrollable_locator_screenshot(FakePage(), locator, path)

            from PIL import Image

            image = Image.open(path)
            self.assertEqual(image.size, (12, 30))
            self.assertEqual(image.getpixel((0, 0)), (0, 0, 0))
            self.assertEqual(image.getpixel((0, 10)), (10, 0, 0))
            self.assertEqual(image.getpixel((0, 20)), (20, 0, 0))

        self.assertEqual(locator.screenshot_scroll_positions, [0, 10, 20])

    def test_concatenate_images_vertically_stacks_images(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            image_paths = []

            from PIL import Image

            for idx, color, size in (
                (1, (255, 0, 0), (10, 8)),
                (2, (0, 255, 0), (6, 12)),
                (3, (0, 0, 255), (8, 4)),
            ):
                path = tmp_path / f"image_{idx}.png"
                Image.new("RGB", size, color).save(path)
                image_paths.append(path)

            output_path = tmp_path / "combined.png"
            common.concatenate_images_vertically(image_paths, output_path)

            combined = Image.open(output_path)
            self.assertEqual(combined.size, (10, 24))
            self.assertEqual(combined.getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(combined.getpixel((2, 10)), (0, 255, 0))
            self.assertEqual(combined.getpixel((1, 22)), (0, 0, 255))


class ClickSubmitButtonTests(unittest.TestCase):
    def test_clicks_first_visible_enabled_button(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        clicked = []

        class FakeButton:
            def is_visible(self):
                return True

            def is_enabled(self):
                return True

            def click(self, **kwargs):
                clicked.append(True)

        class FakeLocator:
            def count(self):
                return 1

            def nth(self, index):
                return FakeButton()

        class FakePage:
            def get_by_role(self, role, name=None):
                return FakeLocator()

            def wait_for_selector(self, selector, **kwargs):
                pass

            def wait_for_timeout(self, ms):
                pass

        result = common.click_submit_button(FakePage(), button_names=("Submit",))
        self.assertTrue(result)
        self.assertEqual(len(clicked), 1)

    def test_returns_false_when_no_buttons_found(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        class FakeLocator:
            def count(self):
                return 0

        class FakePage:
            def get_by_role(self, role, name=None):
                return FakeLocator()

            def wait_for_selector(self, selector, **kwargs):
                pass

            def wait_for_timeout(self, ms):
                pass

        result = common.click_submit_button(FakePage(), button_names=("Submit",))
        self.assertFalse(result)


class PageSnapshotTests(unittest.TestCase):
    def test_calls_evaluate_with_form_selector_and_captcha_type(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        evaluated_js = []

        class FakePage:
            def evaluate(self, js):
                evaluated_js.append(js)
                return {
                    "url": "https://example.com",
                    "page_text": "Hello",
                    "form_visible": True,
                    "hcaptcha_visible": False,
                    "hcaptcha_challenge_active": False,
                    "invalid_fields": [],
                    "errors": [],
                }

        result = common.page_snapshot(FakePage(), form_selector=".form-33", captcha_type="hcaptcha")
        self.assertEqual(len(evaluated_js), 1)
        self.assertIn(".form-33", evaluated_js[0])
        self.assertIn("hcaptcha", evaluated_js[0])
        self.assertEqual(result["url"], "https://example.com")

    def test_escapes_single_quoted_form_selectors_without_double_escaping_backslashes(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        evaluated_js = []

        class FakePage:
            def evaluate(self, js):
                evaluated_js.append(js)
                return {
                    "url": "https://example.com",
                    "page_text": "Hello",
                    "form_visible": True,
                    "recaptcha_visible": False,
                    "recaptcha_challenge_active": False,
                    "invalid_fields": [],
                    "errors": [],
                }

        common.page_snapshot(
            FakePage(),
            form_selector="form, input[type='file'], button[type='submit']",
            captcha_type="recaptcha",
        )

        self.assertEqual(len(evaluated_js), 1)
        self.assertIn("input[type='file']", evaluated_js[0])
        self.assertNotIn("\\\\'", evaluated_js[0])

    def test_hidden_fullscreen_hcaptcha_iframe_is_not_treated_as_active_challenge(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")

        from playwright.sync_api import sync_playwright

        html = """
        <style>
          body { margin: 0; }
          #application-form { width: 420px; height: 260px; margin: 24px; background: #ffffff; }
          iframe.challenge {
            position: fixed;
            inset: 0;
            width: 100vw;
            height: 100vh;
            border: 0;
            z-index: 2147483647;
            visibility: hidden;
          }
        </style>
        <div id="application-form"></div>
        <iframe
          class="challenge"
          title="Widget containing checkbox for hCaptcha security challenge"
          src="https://newassets.hcaptcha.com/captcha/v1/example/static/hcaptcha.html"
        ></iframe>
        """

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 800, "height": 600})
            page.set_content(html)
            page.wait_for_timeout(250)

            snapshot = common.page_snapshot(page, form_selector="#application-form", captcha_type="hcaptcha")

            browser.close()

        self.assertTrue(snapshot["form_visible"])
        self.assertTrue(snapshot["hcaptcha_visible"])
        self.assertFalse(snapshot["hcaptcha_challenge_active"])


class WriteSubmitDebugArtifactsTests(unittest.TestCase):
    def test_writes_html_and_screenshot(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        captured = []

        class FakePage:
            def content(self):
                return "<html>debug</html>"

        with tempfile.TemporaryDirectory() as tmpdir:
            payload = {
                "artifacts": {
                    "submit_debug_html": str(Path(tmpdir) / "debug.html"),
                    "submit_debug_screenshot": str(Path(tmpdir) / "debug.png"),
                }
            }
            common.write_submit_debug_artifacts(
                FakePage(),
                payload,
                capture_fn=lambda page, path: captured.append(str(path)),
            )
            html_content = Path(payload["artifacts"]["submit_debug_html"]).read_text()

        self.assertEqual(html_content, "<html>debug</html>")
        self.assertEqual(len(captured), 1)


class MatchesConfirmPatternsTests(unittest.TestCase):
    def test_matches_known_pattern(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        patterns = (re.compile(r"\bthank(?:s| you)\s+for\s+applying\b", re.I),)
        self.assertTrue(common.matches_confirm_patterns("Thanks for applying to Acme", patterns))

    def test_no_match(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        patterns = (re.compile(r"\bthank(?:s| you)\s+for\s+applying\b", re.I),)
        self.assertFalse(common.matches_confirm_patterns("Please fill out all fields", patterns))


class CollectValidationErrorsTests(unittest.TestCase):
    def test_combines_explicit_and_page_level_errors(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        validation_patterns = (re.compile(r"please (?:complete|fill)", re.I),)
        snapshot = {
            "page_text": "Please complete all required fields",
            "errors": ["Email is required"],
            "invalid_fields": ["email"],
        }
        errors, invalid = common.collect_validation_errors(snapshot, validation_patterns)
        self.assertIn("Email is required", errors)
        self.assertIn("Please complete all required fields", errors)
        self.assertEqual(invalid, ["email"])

    def test_empty_when_no_errors(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        snapshot = {"page_text": "Form loaded", "errors": [], "invalid_fields": []}
        errors, invalid = common.collect_validation_errors(snapshot, ())
        self.assertEqual(errors, [])
        self.assertEqual(invalid, [])


class YesNoStepTests(unittest.TestCase):
    def test_builds_yes_step(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        field = {"label": "Authorized?", "field_name": "auth", "kind": "radio", "required": True, "index": 0}

        def matcher(candidates):
            return "Yes"

        step = common.yes_no_step(field, value=True, source="profile", option_matcher=matcher)
        self.assertEqual(step["value"], "Yes")
        self.assertEqual(step["source"], "profile")

    def test_returns_none_when_no_option_matches(self):
        common = load_module("autofill_common", "scripts/autofill_common.py")
        field = {"label": "Authorized?", "field_name": "auth", "kind": "radio", "required": True, "index": 0}

        def matcher(candidates):
            return None

        step = common.yes_no_step(field, value=True, source="profile", option_matcher=matcher)
        self.assertIsNone(step)


class CaptchaWaitTests(unittest.TestCase):
    def test_captcha_wait_skips_when_headless(self):
        """Headless mode returns 'skipped' immediately."""
        common = load_module("autofill_common", "scripts/autofill_common.py")

        result = common.wait_for_captcha_resolution(
            page=None,
            headless=True,
            payload={"out_dir": "/tmp/test", "company": "Acme"},
            board_title="Test",
            classify_state_fn=lambda s: {"status": "captcha_required"},
            page_snapshot_fn=lambda p: {},
            email_watcher=None,
            confirmed_outcome_from_email_fn=None,
            capture_fn=lambda p, path: None,
            submit_started_at_utc="2026-01-01T00:00:00",
        )
        self.assertEqual(result["status"], "skipped")

    def test_captcha_wait_detects_confirmation(self):
        """When classify_state_fn returns confirmed, wait returns confirmed."""
        from unittest.mock import MagicMock

        common = load_module("autofill_common", "scripts/autofill_common.py")

        page = MagicMock()
        page.evaluate = MagicMock()
        page.wait_for_timeout = MagicMock()

        call_count = 0

        def classify(snapshot):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                return {"status": "confirmed", "reason": "thank you page"}
            return {"status": "captcha_required"}

        email_watcher = MagicMock()
        email_watcher.poll = MagicMock(return_value=None)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            submit_dir = tmp_path / "submit"
            submit_dir.mkdir()

            result = common.wait_for_captcha_resolution(
                page=page,
                headless=False,
                payload={"out_dir": str(tmp_path), "company": "Acme", "job_title": "PM"},
                board_title="Test",
                classify_state_fn=classify,
                page_snapshot_fn=lambda p: {"url": "https://example.com"},
                email_watcher=email_watcher,
                confirmed_outcome_from_email_fn=None,
                capture_fn=lambda p, path: None,
                submit_started_at_utc="2026-01-01T00:00:00",
            )
            self.assertEqual(result["status"], "confirmed")
            self.assertFalse((submit_dir / "awaiting_captcha.json").exists())

    def test_captcha_wait_times_out(self):
        """When timeout expires, returns timeout status."""
        import os
        from unittest.mock import MagicMock

        common = load_module("autofill_common", "scripts/autofill_common.py")

        # Set timeout to 1 second via env var
        old_val = os.environ.get("JOB_ASSETS_CAPTCHA_TIMEOUT")
        os.environ["JOB_ASSETS_CAPTCHA_TIMEOUT"] = "1"
        try:
            page = MagicMock()
            page.evaluate = MagicMock()
            page.wait_for_timeout = MagicMock()
            page.content = MagicMock(return_value="<html></html>")

            email_watcher = MagicMock()
            email_watcher.poll = MagicMock(return_value=None)

            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir)
                submit_dir = tmp_path / "submit"
                submit_dir.mkdir()

                result = common.wait_for_captcha_resolution(
                    page=page,
                    headless=False,
                    payload={
                        "out_dir": str(tmp_path),
                        "company": "Acme",
                        "job_title": "PM",
                        "artifacts": {
                            "submit_debug_html": str(submit_dir / "debug.html"),
                            "submit_debug_screenshot": str(submit_dir / "debug.png"),
                        },
                    },
                    board_title="Test",
                    classify_state_fn=lambda s: {"status": "captcha_required"},
                    page_snapshot_fn=lambda p: {"url": "https://example.com"},
                    email_watcher=email_watcher,
                    confirmed_outcome_from_email_fn=None,
                    capture_fn=lambda p, path: None,
                    submit_started_at_utc="2026-01-01T00:00:00",
                )
                self.assertEqual(result["status"], "timeout")
                self.assertFalse((submit_dir / "awaiting_captcha.json").exists())
        finally:
            if old_val is None:
                os.environ.pop("JOB_ASSETS_CAPTCHA_TIMEOUT", None)
            else:
                os.environ["JOB_ASSETS_CAPTCHA_TIMEOUT"] = old_val


if __name__ == "__main__":
    unittest.main()
