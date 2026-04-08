import importlib.util
import json
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


class _NullLocator:
    first = None

    def __init__(self):
        self.first = self

    def count(self):
        return 0

    def scroll_into_view_if_needed(self):
        return None

    def click(self):
        return None

    def fill(self, _value):
        return None

    def input_value(self):
        return ""

    def inner_text(self):
        return ""


class _FakeLocator:
    def __init__(self, *, text: str = "", value: str = "", on_click=None):
        self.first = self
        self.text = text
        self.value = value
        self.on_click = on_click
        self.filled_values: list[str] = []

    def count(self):
        return 1

    def scroll_into_view_if_needed(self):
        return None

    def click(self):
        if callable(self.on_click):
            self.on_click()

    def fill(self, value: str):
        self.filled_values.append(value)
        self.value = value

    def input_value(self):
        return self.value

    def inner_text(self):
        return self.text or self.value


class _FakeOptionsCollection:
    def __init__(self, options: list[_FakeLocator]):
        self.options = options
        self.first = options[0] if options else _NullLocator()

    def count(self):
        return len(self.options)

    def nth(self, index: int):
        return self.options[index]


class _FakePage:
    def __init__(self, *, selector_map=None, options=None):
        self.selector_map = selector_map or {}
        self.options = options or _FakeOptionsCollection([])

    def locator(self, selector):
        return self.selector_map.get(selector, _NullLocator())

    def get_by_label(self, _pattern):
        return _NullLocator()

    def get_by_role(self, role, name=None):
        del name
        if role == "option":
            return self.options
        return _NullLocator()

    def wait_for_timeout(self, _timeout_ms):
        return None


class RipplingAutofillTests(unittest.TestCase):
    @staticmethod
    def _rippling_html(*, custom_fields: list[dict], additional_questions: list[dict] | None = None) -> str:
        payload = {
            "props": {
                "pageProps": {
                    "activeJobApplication": {
                        "customQuestions": {"fields": custom_fields},
                        "additionalQuestions": additional_questions or [],
                    }
                }
            }
        }
        return f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script></body></html>'

    def _profile(self, **overrides):
        base = {
            "first_name": "Jerrison",
            "last_name": "Li",
            "email": "jerrisonli@gmail.com",
            "phone": "510-613-5192",
            "linkedin": "https://www.linkedin.com/in/stale-profile/",
            "website": "https://jerrisonli.com",
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    def _application_profile(self, **overrides):
        base = {
            "location": "San Francisco, CA",
            "verification_code_email": "jerrisonli@gmail.com",
            "linkedin": "https://www.linkedin.com/in/jerrison/",
            "website": "https://jerrisonli.com",
            "pronouns": "He / Him / His",
            "text_message_consent": False,
            "comfortable_with_posted_salary": True,
            "compensation_expectations": (
                "I'm open and flexible on compensation. I'd prefer to learn more about the role's scope "
                "and total rewards package before discussing specific numbers."
            ),
            "authorized_to_work_unconditionally": True,
            "require_sponsorship_now": False,
            "require_sponsorship_future": False,
            "sponsorship_answer": "No, I do not require sponsorship.",
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_build_payload_preserves_query_when_appending_apply(self):
        autofill = load_module("autofill_rippling", "scripts/autofill_rippling.py")
        out_dir = PROJECT_ROOT / "output" / "rippling" / "query-preserve"
        job_url = "https://ats.rippling.com/rippling/jobs/123?utm_source=trueup"
        html = self._rippling_html(
            custom_fields=[
                {"title": "First name", "fieldType": "SHORT_ANSWER", "oid": "first_name", "required": True},
                {"title": "Last name", "fieldType": "SHORT_ANSWER", "oid": "last_name", "required": True},
                {"title": "Email", "fieldType": "SHORT_ANSWER", "oid": "email", "required": True},
                {"title": "Phone number", "fieldType": "PHONE_NUMBER", "oid": "phone_number", "required": True},
                {"title": "Location (city only)", "fieldType": "SHORT_ANSWER", "oid": "location", "required": True},
                {"title": "Resume", "fieldType": "FILE", "oid": "resume", "required": True},
            ]
        )

        with (
            mock.patch.object(autofill, "load_meta", return_value={"jd_source": job_url}),
            mock.patch.object(autofill, "_fetch_application_html", return_value=html),
            mock.patch.object(autofill, "find_resume_file", return_value=PROJECT_ROOT / "master_resume.md"),
            mock.patch.object(
                autofill,
                "parse_master_resume",
                return_value=self._profile(linkedin="https://www.linkedin.com/in/jerrisonli/"),
            ),
            mock.patch.object(
                autofill,
                "parse_application_profile",
                return_value=self._application_profile(linkedin=None),
            ),
            mock.patch.object(autofill, "primary_employer_name", return_value="OpenAI"),
            mock.patch.object(autofill, "generate_application_answers", return_value={}),
        ):
            payload = autofill._build_payload(out_dir, provider="openai")

        self.assertEqual(
            payload["application_url"],
            "https://ats.rippling.com/rippling/jobs/123/apply?utm_source=trueup",
        )

    def test_build_payload_prefers_application_profile_linkedin(self):
        autofill = load_module("autofill_rippling", "scripts/autofill_rippling.py")
        out_dir = PROJECT_ROOT / "output" / "rippling" / "linkedin-source"
        job_url = "https://ats.rippling.com/rippling/jobs/123"
        html = self._rippling_html(
            custom_fields=[
                {"title": "First name", "fieldType": "SHORT_ANSWER", "oid": "first_name", "required": True},
                {"title": "Email", "fieldType": "SHORT_ANSWER", "oid": "email", "required": True},
                {"title": "LinkedIn Link", "fieldType": "SHORT_ANSWER", "oid": "linkedin", "required": True},
                {"title": "Resume", "fieldType": "FILE", "oid": "resume", "required": True},
            ]
        )

        with (
            mock.patch.object(autofill, "load_meta", return_value={"jd_source": job_url}),
            mock.patch.object(autofill, "_fetch_application_html", return_value=html),
            mock.patch.object(autofill, "find_resume_file", return_value=PROJECT_ROOT / "master_resume.md"),
            mock.patch.object(
                autofill,
                "parse_master_resume",
                return_value=self._profile(),
            ),
            mock.patch.object(
                autofill,
                "parse_application_profile",
                return_value=self._application_profile(),
            ),
            mock.patch.object(autofill, "primary_employer_name", return_value="OpenAI"),
            mock.patch.object(autofill, "generate_application_answers", return_value={}),
        ):
            payload = autofill._build_payload(out_dir, provider="openai")

        linkedin_step = next(step for step in payload["steps"] if step["field_name"] == "linkedin")
        self.assertEqual(linkedin_step["value"], "https://www.linkedin.com/in/jerrison/")
        self.assertEqual(linkedin_step["source"], "application_profile.md")

    def test_build_payload_uses_live_additional_questions_and_omits_absent_linkedin(self):
        autofill = load_module("autofill_rippling", "scripts/autofill_rippling.py")
        out_dir = PROJECT_ROOT / "output" / "rippling" / "live-schema"
        job_url = "https://ats.rippling.com/malwarebytes/jobs/123"
        html = self._rippling_html(
            custom_fields=[
                {"title": "First name", "fieldType": "SHORT_ANSWER", "oid": "first_name", "required": True},
                {"title": "Last name", "fieldType": "SHORT_ANSWER", "oid": "last_name", "required": True},
                {"title": "Email", "fieldType": "SHORT_ANSWER", "oid": "email", "required": True},
                {"title": "Phone number", "fieldType": "PHONE_NUMBER", "oid": "phone_number", "required": True},
                {"title": "Location (city only)", "fieldType": "SHORT_ANSWER", "oid": "location", "required": True},
                {"title": "Resume", "fieldType": "FILE", "oid": "resume", "required": True},
            ],
            additional_questions=[
                {
                    "name": "Compensation Question",
                    "form": {
                        "questions": [
                            {
                                "uniqueKey": "salary_key",
                                "title": "What are your gross annual salary expectations for this role?",
                                "description": "",
                                "questionType": "SHORT_ANSWER",
                                "dataType": "Text",
                                "isRequired": True,
                                "strChoices": [],
                                "isMultiSelectEnabled": False,
                            },
                            {
                                "uniqueKey": "us_person_key",
                                "title": (
                                    "Are you a U.S. citizen, U.S. permanent resident (Green Card holder), "
                                    "or a protected individual?"
                                ),
                                "description": "",
                                "questionType": "SINGLE_SELECT_DROPDOWN",
                                "dataType": "enum",
                                "isRequired": True,
                                "strChoices": ["Yes", "No"],
                                "isMultiSelectEnabled": False,
                            },
                            {
                                "uniqueKey": "export_followup_key",
                                "title": (
                                    "If “No”, is your most recent citizenship or permanent residency in one of the "
                                    "following listed countries?"
                                ),
                                "description": "",
                                "questionType": "SINGLE_SELECT_DROPDOWN",
                                "dataType": "enum",
                                "isRequired": True,
                                "strChoices": ["Yes", "No", "N/A"],
                                "isMultiSelectEnabled": False,
                            },
                        ]
                    },
                }
            ],
        )

        with (
            mock.patch.object(autofill, "load_meta", return_value={"jd_source": job_url, "company_proper": "Malwarebytes"}),
            mock.patch.object(autofill, "_fetch_application_html", return_value=html),
            mock.patch.object(autofill, "find_resume_file", return_value=PROJECT_ROOT / "master_resume.md"),
            mock.patch.object(autofill, "parse_master_resume", return_value=self._profile()),
            mock.patch.object(autofill, "parse_application_profile", return_value=self._application_profile()),
            mock.patch.object(autofill, "primary_employer_name", return_value="OpenAI"),
            mock.patch.object(autofill, "generate_application_answers", return_value={}) as generate_answers,
        ):
            payload = autofill._build_payload(out_dir, provider="openai")

        self.assertFalse(any(step["field_name"] == "linkedin" for step in payload["steps"]))
        self.assertFalse(generate_answers.called)

        salary_step = next(step for step in payload["steps"] if step["field_name"] == "salary_key")
        self.assertIn("open and flexible on compensation", salary_step["value"])
        self.assertEqual(salary_step["source"], "application_profile.md")

        us_person_step = next(step for step in payload["steps"] if step["field_name"] == "us_person_key")
        self.assertEqual(us_person_step["value"], "Yes")
        self.assertEqual(us_person_step["source"], "master_resume.md")

        export_followup_step = next(step for step in payload["steps"] if step["field_name"] == "export_followup_key")
        self.assertEqual(export_followup_step["value"], "N/A")
        self.assertEqual(export_followup_step["source"], "deterministic")

    def test_build_payload_routes_unmapped_live_question_to_generated_answers(self):
        autofill = load_module("autofill_rippling", "scripts/autofill_rippling.py")
        out_dir = PROJECT_ROOT / "output" / "rippling" / "generated-answer"
        job_url = "https://ats.rippling.com/company/jobs/123"
        html = self._rippling_html(
            custom_fields=[
                {"title": "First name", "fieldType": "SHORT_ANSWER", "oid": "first_name", "required": True},
                {"title": "Email", "fieldType": "SHORT_ANSWER", "oid": "email", "required": True},
                {"title": "Resume", "fieldType": "FILE", "oid": "resume", "required": True},
            ],
            additional_questions=[
                {
                    "name": "Hiring Manager Questions",
                    "form": {
                        "questions": [
                            {
                                "uniqueKey": "why_join_key",
                                "title": "Why are you interested in this role?",
                                "description": "",
                                "questionType": "SHORT_ANSWER",
                                "dataType": "Text",
                                "isRequired": True,
                                "strChoices": [],
                                "isMultiSelectEnabled": False,
                            }
                        ]
                    },
                }
            ],
        )

        with (
            mock.patch.object(autofill, "load_meta", return_value={"jd_source": job_url, "company_proper": "Company"}),
            mock.patch.object(autofill, "_fetch_application_html", return_value=html),
            mock.patch.object(autofill, "find_resume_file", return_value=PROJECT_ROOT / "master_resume.md"),
            mock.patch.object(autofill, "parse_master_resume", return_value=self._profile()),
            mock.patch.object(autofill, "parse_application_profile", return_value=self._application_profile()),
            mock.patch.object(autofill, "primary_employer_name", return_value="OpenAI"),
            mock.patch.object(
                autofill,
                "generate_application_answers",
                return_value={"why_join_key": "The role lines up with my product and platform experience."},
            ),
        ):
            payload = autofill._build_payload(out_dir, provider="openai")

        generated_step = next(step for step in payload["steps"] if step["field_name"] == "why_join_key")
        self.assertEqual(generated_step["source"], "generated_application_answer")
        self.assertIn("product and platform experience", generated_step["value"])

    def test_fill_step_uses_live_path_selector_for_custom_text_fields(self):
        autofill = load_module("autofill_rippling", "scripts/autofill_rippling.py")
        text_locator = _FakeLocator()
        page = _FakePage(
            selector_map={
                '[data-input*="salary_key"]': text_locator,
            }
        )
        step = {
            "field_name": "salary_key",
            "label": "What are your salary expectations?",
            "kind": "text",
            "path": "salary_key",
            "value": "I'm open and flexible on compensation.",
        }

        autofill._fill_step(page, step)

        self.assertTrue(step.get("filled"))
        self.assertEqual(text_locator.input_value(), "I'm open and flexible on compensation.")
        self.assertNotIn("status", step)

    def test_fill_step_uses_live_path_selector_for_custom_select_fields(self):
        autofill = load_module("autofill_rippling", "scripts/autofill_rippling.py")
        combobox = _FakeLocator(text="Select")
        yes_option = _FakeLocator(text="Yes", on_click=lambda: setattr(combobox, "text", "Yes"))
        page = _FakePage(
            selector_map={
                '[data-testid*="us_person_key"] [role="combobox"]': combobox,
            },
            options=_FakeOptionsCollection([
                _FakeLocator(text="No"),
                yes_option,
                _FakeLocator(text="N/A"),
            ]),
        )
        step = {
            "field_name": "us_person_key",
            "label": "Are you a U.S. person?",
            "kind": "select",
            "path": "us_person_key",
            "value": "Yes",
        }

        autofill._fill_step(page, step)

        self.assertTrue(step.get("filled"))
        self.assertEqual(combobox.inner_text(), "Yes")
        self.assertNotIn("status", step)

    def test_fill_step_uses_location_testid_when_schema_label_is_city_only(self):
        autofill = load_module("autofill_rippling", "scripts/autofill_rippling.py")
        location_input = _FakeLocator()
        page = _FakePage(
            selector_map={
                '[data-testid="location"] input': location_input,
            }
        )
        step = {
            "field_name": "location",
            "label": "Location (city only)",
            "kind": "text",
            "path": "location",
            "value": "San Francisco",
        }

        autofill._fill_step(page, step)

        self.assertTrue(step.get("filled"))
        self.assertEqual(location_input.input_value(), "San Francisco")
        self.assertNotIn("status", step)
