import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class AshbyAutofillTests(unittest.TestCase):
    def test_application_url_for_job_url_preserves_query(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        url = "https://jobs.ashbyhq.com/canals/6815edc9-2ebb-400a-b974-67a119a71f74?utm_source=VANyKzAEAm"
        self.assertEqual(
            autofill._application_url_for_job_url(url),
            "https://jobs.ashbyhq.com/canals/6815edc9-2ebb-400a-b974-67a119a71f74/application?utm_source=VANyKzAEAm",
        )

    def test_application_url_for_company_hosted_wrapper_url_resolves_to_direct_ashby_posting(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        url = (
            "https://www.standinsurance.com/careers?"
            "ashby_jid=0f8f1869-bbd5-4d7a-bd48-5b30b258b5f7&utm_source=QNwjOoM9DP"
        )
        direct = "https://jobs.ashbyhq.com/standinsurance/0f8f1869-bbd5-4d7a-bd48-5b30b258b5f7?utm_source=QNwjOoM9DP"

        with mock.patch.object(autofill, "resolve_ashby_wrapper_url", return_value=direct):
            self.assertEqual(
                autofill._application_url_for_job_url(url),
                "https://jobs.ashbyhq.com/standinsurance/0f8f1869-bbd5-4d7a-bd48-5b30b258b5f7/application?utm_source=QNwjOoM9DP",
            )

    def test_extract_app_data_from_window_payload(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        html = """
        <html>
          <head></head>
          <body>
            <script>
              window.__appData = {"posting":{"title":"Senior Product Manager","applicationForm":{"entries":[{"isRequired":true,"field":{"title":"Name","path":"_systemfield_name","type":"String"}}]}}};
            </script>
          </body>
        </html>
        """
        app_data = autofill._extract_app_data(html)
        self.assertEqual(app_data["posting"]["title"], "Senior Product Manager")

    def test_field_entries_from_app_data_flattens_application_form(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        app_data = {
            "posting": {
                "applicationForm": {
                    "entries": [
                        {
                            "isRequired": True,
                            "field": {
                                "title": "Name",
                                "path": "_systemfield_name",
                                "type": "String",
                            },
                        },
                        {
                            "isRequired": True,
                            "field": {
                                "title": "Resume",
                                "path": "_systemfield_resume",
                                "type": "File",
                            },
                        },
                    ]
                }
            }
        }
        fields = autofill._field_entries_from_app_data(app_data)
        self.assertEqual(
            fields,
            [
                {
                    "field_name": "application_name",
                    "label": "Name",
                    "description": "",
                    "path": "_systemfield_name",
                    "required": True,
                    "field_type": "String",
                    "form_name": "application",
                    "raw_entry": app_data["posting"]["applicationForm"]["entries"][0],
                    "raw_field": app_data["posting"]["applicationForm"]["entries"][0]["field"],
                },
                {
                    "field_name": "application_resume",
                    "label": "Resume",
                    "description": "",
                    "path": "_systemfield_resume",
                    "required": True,
                    "field_type": "File",
                    "form_name": "application",
                    "raw_entry": app_data["posting"]["applicationForm"]["entries"][1],
                    "raw_field": app_data["posting"]["applicationForm"]["entries"][1]["field"],
                },
            ],
        )

    def test_field_entries_from_app_data_extracts_plain_text_description(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        app_data = {
            "posting": {
                "applicationForm": {
                    "entries": [
                        {
                            "isRequired": True,
                            "field": {
                                "title": "Do you live within 60 miles of one of Socure's Product Talent Hubs?",
                                "path": "socure_hub_distance",
                                "type": "Boolean",
                            },
                            "description": {
                                "content": {
                                    "type": "doc",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [
                                                {"text": "Eligible Hub Locations:", "type": "text"},
                                            ],
                                        },
                                        {
                                            "type": "bulletList",
                                            "content": [
                                                {
                                                    "type": "listItem",
                                                    "content": [
                                                        {
                                                            "type": "paragraph",
                                                            "content": [{"text": "San Francisco, CA", "type": "text"}],
                                                        }
                                                    ],
                                                },
                                                {
                                                    "type": "listItem",
                                                    "content": [
                                                        {
                                                            "type": "paragraph",
                                                            "content": [{"text": "New York, NY", "type": "text"}],
                                                        }
                                                    ],
                                                },
                                            ],
                                        },
                                    ],
                                }
                            },
                        }
                    ]
                }
            }
        }

        fields = autofill._field_entries_from_app_data(app_data)

        self.assertEqual(len(fields), 1)
        self.assertIn("Eligible Hub Locations", fields[0]["description"])
        self.assertIn("San Francisco, CA", fields[0]["description"])
        self.assertIn("New York, NY", fields[0]["description"])

    def test_question_specs_include_field_description_for_generated_fields(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        specs = autofill._question_specs(
            [
                {
                    "field_name": "application_socure_hub_distance",
                    "label": "Do you live within 60 miles of one of Socure's Product Talent Hubs?",
                    "description": "Eligible Hub Locations:\nSan Francisco, CA\nNew York, NY",
                    "required": True,
                    "field_type": "Boolean",
                }
            ],
            {"application_socure_hub_distance"},
        )

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["description"], "Eligible Hub Locations:\nSan Francisco, CA\nNew York, NY")

    def test_state_country_variants_expands_state_abbreviation(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        self.assertEqual(
            autofill._state_country_variants("San Francisco, CA", "United States"),
            [
                "San Francisco, CA",
                "San Francisco, California, United States",
                "San Francisco, United States",
            ],
        )

    def test_infer_step_ignores_blank_generated_answer(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        field = {
            "field_name": "application_if_other_please_let_us_know_your_pronouns",
            "label": "If other, please let us know your pronouns",
            "path": "pronouns_other",
            "required": False,
            "field_type": "String",
        }

        step = autofill._infer_step(
            field,
            meta={},
            profile=SimpleNamespace(),
            application_profile=SimpleNamespace(),
            out_dir=PROJECT_ROOT,
            generated_answers={field["field_name"]: "   "},
        )

        self.assertIsNone(step)

    def test_infer_step_fills_string_pronouns_from_application_profile(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        field = {
            "field_name": "application_pronouns",
            "label": "Pronouns",
            "path": "application_pronouns",
            "required": False,
            "field_type": "String",
        }

        step = autofill._infer_step(
            field,
            meta={},
            profile=SimpleNamespace(),
            application_profile=SimpleNamespace(pronouns="He/Him"),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "text")
        self.assertEqual(step["value"], "He/Him")
        self.assertEqual(step["source"], "application_profile.md")
        self.assertTrue(step["blocks_draft_completion"])
        self.assertEqual(step["profile_field"], "pronouns")

    def test_infer_step_handles_required_identity_and_location_fields(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        profile = SimpleNamespace(first_name="Candidate")
        application_profile = SimpleNamespace(
            location="San Francisco, CA",
            country="United States",
            lives_in_job_location=True,
            comfortable_working_on_site=True,
        )

        preferred_name = autofill._infer_step(
            {
                "field_name": "application_preferred_first_name",
                "label": "Preferred First Name",
                "path": "preferred_name",
                "required": True,
                "field_type": "String",
            },
            meta={},
            profile=profile,
            application_profile=application_profile,
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )
        city = autofill._infer_step(
            {
                "field_name": "application_city_of_residence",
                "label": "City of Residence",
                "path": "_systemfield_location",
                "required": True,
                "field_type": "Location",
            },
            meta={},
            profile=profile,
            application_profile=application_profile,
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )
        hybrid = autofill._infer_step(
            {
                "field_name": "application_hybrid_commute",
                "label": "This is a hybrid role. Are you currently based within a commutable distance of the listed office(s) and willing to commute into the office two days a week?",
                "path": "hybrid_commute",
                "required": True,
                "field_type": "Boolean",
            },
            meta={},
            profile=profile,
            application_profile=application_profile,
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(preferred_name["value"], "Candidate")
        self.assertEqual(city["kind"], "location")
        self.assertEqual(city["value"], "San Francisco, CA")
        self.assertEqual(hybrid["kind"], "choice")
        self.assertEqual(hybrid["value"], "Yes")

    def test_infer_step_matches_hyphenated_email_label(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        step = autofill._infer_step(
            {
                "field_name": "application_e_mail",
                "label": "E-mail",
                "path": "_systemfield_email",
                "required": True,
                "field_type": "Email",
            },
            meta={},
            profile=SimpleNamespace(email="candidate@example.com"),
            application_profile=SimpleNamespace(),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )
        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "text")
        self.assertEqual(step["value"], "candidate@example.com")

    def test_infer_step_prefers_company_website_for_how_did_you_hear_when_metadata_is_direct(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_how_did_you_hear_about_this_role",
                "label": "How did you hear about this role?",
                "path": "application_how_did_you_hear_about_this_role",
                "required": True,
                "field_type": "ValueSelect",
                "raw_field": {
                    "selectableValues": [
                        {"label": "Other"},
                        {"label": "Company Website / Careers Page"},
                        {"label": "LinkedIn"},
                    ]
                },
            },
            meta={
                "company_proper": "0G Labs",
                "jd_source": "https://jobs.ashbyhq.com/0glabs/123",
                "source": "direct",
            },
            profile=SimpleNamespace(),
            application_profile=SimpleNamespace(how_did_you_hear="Other"),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "choice")
        self.assertEqual(step["value"], "Company Website / Careers Page")
        self.assertEqual(step["source"], "job.source")

    def test_infer_step_maps_hybrid_work_interest_choice_from_application_profile(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_hybrid_interest",
                "label": "What best describes your interest in hybrid work at Homebase?",
                "path": "hybrid_interest",
                "required": True,
                "field_type": "ValueSelect",
                "raw_field": {
                    "selectableValues": [
                        {
                            "label": "I live near a Homebase hub and can work hybrid (in-office Tues/Wed)",
                            "value": "hybrid",
                        },
                        {
                            "label": "I don’t live near a hub and prefer remote opportunities",
                            "value": "remote",
                        },
                    ]
                },
            },
            meta={},
            profile=SimpleNamespace(),
            application_profile=SimpleNamespace(
                lives_in_job_location=True,
                comfortable_working_on_site=True,
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(step["kind"], "choice")
        self.assertEqual(step["value"], "I live near a Homebase hub and can work hybrid (in-office Tues/Wed)")
        self.assertEqual(step["source"], "shared_positive_fit_policy")

    def test_infer_step_maps_remote_work_interest_choice_when_not_local(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_hybrid_interest",
                "label": "What best describes your interest in hybrid work at Homebase?",
                "path": "hybrid_interest",
                "required": True,
                "field_type": "ValueSelect",
                "raw_field": {
                    "selectableValues": [
                        {
                            "label": "I live near a Homebase hub and can work hybrid (in-office Tues/Wed)",
                            "value": "hybrid",
                        },
                        {
                            "label": "I don’t live near a hub and prefer remote opportunities",
                            "value": "remote",
                        },
                    ]
                },
            },
            meta={},
            profile=SimpleNamespace(),
            application_profile=SimpleNamespace(
                lives_in_job_location=False,
                comfortable_working_on_site=False,
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(step["kind"], "choice")
        self.assertEqual(step["value"], "I don’t live near a hub and prefer remote opportunities")
        self.assertEqual(step["source"], "shared_positive_fit_policy")

    def test_infer_step_handles_office_days_per_week_variant(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_are_you_willing_and_able_to_come_into_our_downtown_sf_office_3_days_per_week",
                "label": "Are you willing and able to come into our downtown SF office 3 days per week?",
                "path": "office_attendance",
                "required": True,
                "field_type": "ValueSelect",
                "raw_field": {"selectableValues": [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}]},
            },
            meta={},
            profile=SimpleNamespace(),
            application_profile=SimpleNamespace(
                lives_in_job_location=True,
                comfortable_working_on_site=True,
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(step["kind"], "choice")
        self.assertEqual(step["value"], "Yes")
        self.assertEqual(step["source"], "shared_positive_fit_policy")

    def test_infer_step_uses_generated_answer_for_open_ended_shipped_prompt(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_consumer_product_feature",
                "label": "What consumer-facing product/feature have you shipped that you are the most proud of?",
                "path": "consumer_product_feature",
                "required": False,
                "field_type": "String",
            },
            meta={},
            profile=SimpleNamespace(),
            application_profile=SimpleNamespace(),
            out_dir=PROJECT_ROOT,
            generated_answers={
                "application_consumer_product_feature": "I led member-growth experiments across onboarding and referrals."
            },
        )

        self.assertEqual(step["kind"], "text")
        self.assertEqual(step["value"], "I led member-growth experiments across onboarding and referrals.")
        self.assertEqual(step["source"], "generated_application_answer")

    def test_infer_step_uses_cover_letter_text_for_additional_information(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        field = {
            "field_name": "application_additional_information",
            "label": "Additional Information",
            "path": "additional_information",
            "required": False,
            "field_type": "LongText",
        }

        with mock.patch.object(autofill, "find_cover_letter_text", return_value="Tailored additional context."):
            step = autofill._infer_step(
                field,
                meta={},
                profile=SimpleNamespace(),
                application_profile=SimpleNamespace(),
                out_dir=PROJECT_ROOT,
                generated_answers={},
            )

        self.assertEqual(step["kind"], "text")
        self.assertEqual(step["value"], "Tailored additional context.")
        self.assertEqual(step["source"], "cover_letter_text.txt")

    def test_infer_step_free_text_city_state_returns_location_not_yes(self):
        """Regression: 'What city and state do you currently live in?' must return location, not 'Yes'."""
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        application_profile = SimpleNamespace(
            location="San Francisco, CA",
            country="United States",
            lives_in_job_location=True,
        )

        step = autofill._infer_step(
            {
                "field_name": "_systemfield_city_state",
                "label": "What city and state do you currently live in?",
                "path": "city_state",
                "required": True,
                "field_type": "String",
            },
            meta={},
            profile=SimpleNamespace(),
            application_profile=application_profile,
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["value"], "San Francisco, CA")
        self.assertEqual(step["kind"], "text")
        self.assertTrue(step["blocks_draft_completion"])
        self.assertEqual(step["blocker_kind"], "visible_profile_field")
        self.assertEqual(step["profile_field"], "location")

    def test_infer_step_free_text_city_country_currently_located_returns_location(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        application_profile = SimpleNamespace(
            location="San Francisco, CA",
            country="United States",
            lives_in_job_location=True,
        )

        step = autofill._infer_step(
            {
                "field_name": "application_city_country",
                "label": "What city and country are you currently located in?",
                "path": "city_country",
                "required": True,
                "field_type": "LongText",
            },
            meta={},
            profile=SimpleNamespace(),
            application_profile=application_profile,
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["value"], "San Francisco, CA")
        self.assertEqual(step["kind"], "text")
        self.assertTrue(step["blocks_draft_completion"])
        self.assertEqual(step["blocker_kind"], "visible_profile_field")
        self.assertEqual(step["profile_field"], "location")

    def test_infer_step_answers_salary_comfort_from_application_profile(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_salary_range",
                "label": "Are you comfortable interviewing for the salary outlined in the job description?",
                "path": "salary_range",
                "required": True,
                "field_type": "Boolean",
            },
            meta={},
            profile=SimpleNamespace(employers=set()),
            application_profile=SimpleNamespace(
                comfortable_with_posted_salary=False,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(step["kind"], "choice")
        self.assertEqual(step["value"], "No")
        self.assertEqual(step["source"], "application_profile.md")

    def test_infer_step_answers_compensation_text_from_application_profile(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_compensation",
                "label": "What are your compensation expectations?",
                "path": "compensation_expectations",
                "required": True,
                "field_type": "String",
            },
            meta={},
            profile=SimpleNamespace(employers=set()),
            application_profile=SimpleNamespace(
                compensation_expectations=(
                    "I'm open and flexible on compensation. I'd prefer to learn more about the role's scope "
                    "and total rewards package before discussing specific numbers."
                ),
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(step["kind"], "text")
        self.assertEqual(
            step["value"],
            "I'm open and flexible on compensation. I'd prefer to learn more about the role's scope and total rewards package before discussing specific numbers.",
        )
        self.assertEqual(step["source"], "application_profile.md")

    def test_infer_step_answers_prior_employment_boolean_no_from_resume_history(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_prior_employment",
                "label": "Have you worked at Snowflake in the past in a Full-time, Part-time, contractor or Intern capacity?",
                "path": "prior_employment",
                "required": True,
                "field_type": "Boolean",
            },
            meta={},
            profile=SimpleNamespace(employers={"moody's analytics", "kyte", "allstate"}),
            application_profile=SimpleNamespace(
                location="San Francisco, CA",
                country="United States",
                require_sponsorship_now=False,
                require_sponsorship_future=False,
                authorized_to_work_unconditionally=True,
                minimum_years_experience=True,
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(step["kind"], "choice")
        self.assertEqual(step["value"], "No")
        self.assertEqual(step["source"], "master_resume.md")

    def test_infer_step_answers_auditor_independence_no_from_resume_history(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_auditor_independence",
                "label": (
                    "Due to SEC auditor independence requirements, please let us know whether you have previously "
                    "worked at, or if currently working at PricewaterhouseCoopers (PwC), who is our independent auditor."
                ),
                "path": "auditor_independence",
                "required": True,
                "field_type": "Boolean",
            },
            meta={},
            profile=SimpleNamespace(employers={"moody's analytics", "kyte", "allstate"}),
            application_profile=SimpleNamespace(
                location="San Francisco, CA",
                country="United States",
                require_sponsorship_now=False,
                require_sponsorship_future=False,
                authorized_to_work_unconditionally=True,
                minimum_years_experience=True,
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(step["kind"], "choice")
        self.assertEqual(step["value"], "No")
        self.assertEqual(step["source"], "master_resume.md")

    def test_infer_step_answers_explicit_state_list_residency_from_candidate_location(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_state_list_residency",
                "label": "Do you currently reside in any of the following states: DE, HI, IA, KY, MS, NE, NM, SD, VT, WV, WY?",
                "path": "state_list_residency",
                "required": True,
                "field_type": "Boolean",
            },
            meta={},
            profile=SimpleNamespace(employers=set()),
            application_profile=SimpleNamespace(
                location="San Francisco, CA",
                country="United States",
                require_sponsorship_now=False,
                require_sponsorship_future=False,
                authorized_to_work_unconditionally=True,
                minimum_years_experience=True,
                lives_in_job_location=True,
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(step["kind"], "choice")
        self.assertEqual(step["value"], "No")
        self.assertEqual(step["source"], "application_profile.md")

    def test_infer_step_answers_current_company_from_master_resume(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        with mock.patch.object(autofill, "primary_employer_name", return_value="Moody's Analytics"):
            step = autofill._infer_step(
                {
                    "field_name": "application_current_company",
                    "label": "Current company",
                    "path": "currentCompany",
                    "required": False,
                    "field_type": "String",
                },
                meta={},
                profile=SimpleNamespace(employers=set()),
                application_profile=SimpleNamespace(),
                out_dir=PROJECT_ROOT,
                generated_answers={},
            )

        self.assertEqual(step["kind"], "text")
        self.assertEqual(step["value"], "Moody's Analytics")
        self.assertEqual(step["source"], "master_resume.md")
        self.assertTrue(step["blocks_draft_completion"])
        self.assertEqual(step["blocker_kind"], "visible_profile_field")
        self.assertEqual(step["profile_field"], "current_employer")

    def test_infer_step_answers_undergraduate_gpa_from_application_profile(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_undergraduate_gpa",
                "label": "Please list your undergraduate (Bachelor's) GPA:",
                "path": "undergraduate_gpa",
                "required": True,
                "field_type": "LongText",
            },
            meta={},
            profile=SimpleNamespace(employers=set()),
            application_profile=SimpleNamespace(undergraduate_gpa="3.8/4.0"),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(step["kind"], "text")
        self.assertEqual(step["value"], "3.8/4.0")
        self.assertEqual(step["source"], "application_profile.md")

    def test_pending_user_input_fields_detect_missing_shared_gpa_value(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        fields = [
            {
                "field_name": "application_undergraduate_gpa",
                "label": "Please list your undergraduate (Bachelor's) GPA:",
                "path": "undergraduate_gpa",
                "required": True,
                "field_type": "String",
            }
        ]

        pending = autofill._pending_user_input_fields(
            fields,
            application_profile=SimpleNamespace(undergraduate_gpa=None, compensation_expectations=None),
        )

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["field_name"], "application_undergraduate_gpa")

    def test_infer_step_answers_transgender_from_application_profile(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_transgender",
                "label": "Do you identify as transgender?",
                "path": "transgender",
                "required": True,
                "field_type": "ValueSelect",
                "raw_field": {
                    "selectableValues": [
                        {"label": "Yes"},
                        {"label": "No"},
                    ]
                },
            },
            meta={},
            profile=SimpleNamespace(employers=set()),
            application_profile=SimpleNamespace(transgender_status="No"),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(step["kind"], "choice")
        self.assertEqual(step["value"], "No")
        self.assertEqual(step["source"], "application_profile.md")

    def test_infer_step_answers_linked_in_variant_from_application_profile(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_linked_in",
                "label": "Linked In",
                "path": "application_linked_in",
                "required": True,
                "field_type": "Url",
            },
            meta={},
            profile=SimpleNamespace(linkedin=None),
            application_profile=SimpleNamespace(linkedin="https://linkedin.com/in/candidate/"),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "text")
        self.assertEqual(step["value"], "https://linkedin.com/in/candidate/")
        self.assertEqual(step["source"], "application_profile.md")
        self.assertEqual(step["profile_field"], "linkedin")

    def test_infer_step_matches_gender_identity_alias_against_man_option(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "survey_1_what_is_your_gender_identity",
                "label": "What is your gender identity?",
                "path": "gender_identity",
                "required": False,
                "field_type": "ValueSelect",
                "raw_field": {
                    "selectableValues": [
                        {"label": "Man"},
                        {"label": "Woman"},
                    ]
                },
            },
            meta={},
            profile=SimpleNamespace(employers=set()),
            application_profile=SimpleNamespace(gender="Male", gender_identity="Cisgender Male/Man"),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["value"], "Man")
        self.assertEqual(step["profile_field"], "gender_identity")

    def test_infer_step_uses_full_onsite_start_location_answer_for_text_prompt(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        with mock.patch.object(
            autofill,
            "build_onsite_start_location_answer",
            return_value="Yes. The soonest I could start is March 23, 2026, and I would plan to work from San Francisco.",
        ):
            step = autofill._infer_step(
                {
                    "field_name": "application_start_location",
                    "label": "This is an onsite job in SF or Seattle, w 1-day a week wfh flexibility. Have you taken this into consideration & still want to proceed? If so, when is the soonest you could start? And at which location?",
                    "path": "start_location",
                    "required": True,
                    "field_type": "String",
                },
                meta={},
                profile=SimpleNamespace(employers=set()),
                application_profile=SimpleNamespace(),
                out_dir=PROJECT_ROOT,
                generated_answers={},
            )

        self.assertEqual(step["kind"], "text")
        self.assertEqual(
            step["value"],
            "Yes. The soonest I could start is March 23, 2026, and I would plan to work from San Francisco.",
        )
        self.assertEqual(step["source"], "application_profile.md")

    def test_infer_step_does_not_treat_narrative_experience_prompt_as_boolean(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_homeowners_experience",
                "label": "Homeowners / P&C product ownership: Describe your experience managing a homeowners or property line of business. Include market context (admitted vs E&S, state[s], peril) and your decision scope.",
                "path": "homeowners_experience",
                "required": True,
                "field_type": "String",
            },
            meta={},
            profile=SimpleNamespace(employers=set()),
            application_profile=SimpleNamespace(
                require_sponsorship_now=False,
                require_sponsorship_future=False,
                authorized_to_work_unconditionally=True,
                minimum_years_experience=True,
                lives_in_job_location=True,
                willing_to_relocate=True,
                comfortable_working_on_site=True,
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNone(step)

    def test_infer_step_uses_cover_letter_text_for_text_cover_letter_fields(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        with mock.patch.object(autofill, "find_cover_letter_text", return_value="Dear Hiring Team,\n\nThanks.\n"):
            step = autofill._infer_step(
                {
                    "field_name": "application_cover_letter_text",
                    "label": "Cover Letter",
                    "path": "cover_letter_text",
                    "required": False,
                    "field_type": "LongText",
                },
                meta={},
                profile=SimpleNamespace(employers=set()),
                application_profile=SimpleNamespace(),
                out_dir=PROJECT_ROOT,
                generated_answers={},
            )

        self.assertEqual(step["kind"], "text")
        self.assertEqual(step["source"], "cover_letter_text.txt")
        self.assertIn("Dear Hiring Team", step["value"])

    def test_infer_step_uses_cover_letter_file_for_portfolio_or_cover_letter_upload(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        with mock.patch.object(autofill, "find_cover_letter_file", return_value=PROJECT_ROOT / "cover-letter.pdf"):
            step = autofill._infer_step(
                {
                    "field_name": "application_portfolio_or_cover_letter",
                    "label": "Portfolio or Cover Letter",
                    "path": "portfolio_or_cover_letter",
                    "required": False,
                    "field_type": "File",
                },
                meta={},
                profile=SimpleNamespace(website="https://candidate.example.com", employers=set()),
                application_profile=SimpleNamespace(website="https://candidate.example.com"),
                out_dir=PROJECT_ROOT,
                generated_answers={},
            )

        self.assertEqual(step["kind"], "file")
        self.assertEqual(step["source"], "existing_cover_letter_asset")
        self.assertTrue(step["file_path"].endswith("cover-letter.pdf"))

    def test_infer_step_fills_github_profile_from_application_profile(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_github_profile",
                "label": "GitHub Profile",
                "path": "github_profile",
                "required": False,
                "field_type": "String",
            },
            meta={},
            profile=SimpleNamespace(linkedin=None, website=None),
            application_profile=SimpleNamespace(github="https://github.com/candidate"),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(step["value"], "https://github.com/candidate")
        self.assertEqual(step["source"], "application_profile.md")

    def test_build_payload_writes_pending_user_input_and_aborts_for_specialized_prompt(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        app_data = {
            "posting": {
                "title": "Senior Insurance Product Manager",
                "applicationForm": {
                    "entries": [
                        {
                            "isRequired": True,
                            "field": {
                                "title": "Homeowners / P&C product ownership: Describe your experience managing a homeowners or property line of business. Include market context (admitted vs E&S, state[s], peril) and your decision scope.",
                                "path": "homeowners_experience",
                                "type": "String",
                            },
                        }
                    ]
                },
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps(
                    {
                        "company": "stand-insurance",
                        "company_proper": "Stand Insurance",
                        "jd_title": "Senior Insurance Product Manager",
                        "jd_source": "https://jobs.ashbyhq.com/standinsurance/123",
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(autofill, "parse_master_resume", return_value=SimpleNamespace(employers=set())):
                with mock.patch.object(autofill, "parse_application_profile", return_value=SimpleNamespace()):
                    with mock.patch.object(autofill, "_fetch_application_html", return_value="<html></html>"):
                        with mock.patch.object(autofill, "_extract_app_data", return_value=app_data):
                            with self.assertRaisesRegex(ValueError, "pending_user_input.json"):
                                autofill._build_payload(out_dir, provider="claude")

            pending_path = out_dir / "submit" / "pending_user_input.json"
            payload = json.loads(pending_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "pending_user_input")
        self.assertEqual(payload["board"], "ashby")
        self.assertEqual(len(payload["questions"]), 1)

    def test_build_payload_answers_no_for_outside_commitment_disclosure_prompt(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        app_data = {
            "posting": {
                "title": "Principal Product Manager",
                "applicationForm": {
                    "entries": [
                        {
                            "isRequired": True,
                            "field": {
                                "title": (
                                    "Are you currently engage in any side businesses, hold board positions, "
                                    "serve in nonprofit roles, maintain academic commitments, or have any other "
                                    "obligations that you anticipate continuing while employed with us? "
                                    "If yes, please provide details."
                                ),
                                "path": "outside_commitments",
                                "type": "MultiValueSelect",
                                "selectableValues": [{"label": "Yes", "value": "Yes"}, {"label": "No", "value": "No"}],
                            },
                        }
                    ]
                },
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps(
                    {
                        "company": "pinecone",
                        "company_proper": "Pinecone",
                        "jd_title": "Principal Product Manager",
                        "jd_source": "https://jobs.ashbyhq.com/pinecone/123",
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(autofill, "parse_master_resume", return_value=SimpleNamespace(employers=set())),
                mock.patch.object(autofill, "parse_application_profile", return_value=SimpleNamespace()),
                mock.patch.object(autofill, "_fetch_application_html", return_value="<html></html>"),
                mock.patch.object(autofill, "_extract_app_data", return_value=app_data),
                mock.patch.object(autofill, "generate_application_answers", return_value={}),
            ):
                payload = autofill._build_payload(out_dir, provider="claude")

        self.assertFalse((out_dir / "submit" / "pending_user_input.json").exists())
        outside_commitment_step = next(step for step in payload["steps"] if step["path"] == "outside_commitments")
        self.assertEqual(outside_commitment_step["value"], "No")

    def test_build_payload_omits_redundant_review_screenshot_artifact(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        app_data = {
            "posting": {
                "title": "Senior Insurance Product Manager",
                "applicationForm": {
                    "entries": [
                        {
                            "isRequired": True,
                            "field": {
                                "title": "Name",
                                "path": "_systemfield_name",
                                "type": "String",
                            },
                        }
                    ]
                },
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps(
                    {
                        "company": "stand-insurance",
                        "company_proper": "Stand Insurance",
                        "jd_title": "Senior Insurance Product Manager",
                        "jd_source": "https://jobs.ashbyhq.com/standinsurance/123",
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    autofill,
                    "parse_master_resume",
                    return_value=SimpleNamespace(
                        full_name="Candidate Name",
                        first_name="Candidate",
                        email="candidate@example.com",
                        phone="555-555-5555",
                        linkedin="https://linkedin.com/in/candidate/",
                        website="https://candidate.example.com",
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "parse_application_profile",
                    return_value=SimpleNamespace(
                        verification_code_email="",
                        linkedin="https://linkedin.com/in/candidate/",
                        github="https://github.com/candidate",
                        website="https://candidate.example.com",
                        location="San Francisco, CA",
                        country="United States",
                        text_message_consent=False,
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "_fetch_application_html",
                    return_value="<html></html>",
                ),
                mock.patch.object(
                    autofill,
                    "_extract_app_data",
                    return_value=app_data,
                ),
                mock.patch.object(
                    autofill,
                    "generate_application_answers",
                    return_value={},
                ),
            ):
                payload = autofill._build_payload(out_dir, provider="claude")

        self.assertNotIn("review_screenshot", payload["artifacts"])
        self.assertIn("pre_submit_screenshot", payload["artifacts"])

    def test_build_payload_maps_office_location_choice_from_application_profile(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        app_data = {
            "posting": {
                "title": "Don't See Your Role? Apply Here!",
                "applicationForm": {
                    "entries": [
                        {
                            "isRequired": True,
                            "field": {
                                "title": "Office location of choice",
                                "path": "office_location",
                                "type": "ValueSelect",
                                "selectableValues": [
                                    {"label": "New York", "value": "New York"},
                                    {"label": "San Francisco", "value": "San Francisco"},
                                    {"label": "London", "value": "London"},
                                ],
                            },
                        }
                    ]
                },
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps(
                    {
                        "company": "reflection",
                        "company_proper": "Reflection",
                        "jd_title": "Don't See Your Role? Apply Here!",
                        "jd_source": "https://jobs.ashbyhq.com/reflectionai/123",
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    autofill,
                    "parse_master_resume",
                    return_value=SimpleNamespace(
                        full_name="Candidate Name",
                        first_name="Candidate",
                        email="candidate@example.com",
                        phone="555-555-5555",
                        linkedin="https://linkedin.com/in/candidate/",
                        website="https://candidate.example.com",
                        employers=set(),
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "parse_application_profile",
                    return_value=SimpleNamespace(
                        verification_code_email="",
                        linkedin="https://linkedin.com/in/candidate/",
                        github="https://github.com/candidate",
                        website="https://candidate.example.com",
                        location="San Francisco, CA",
                        country="United States",
                        text_message_consent=False,
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "_fetch_application_html",
                    return_value="<html></html>",
                ),
                mock.patch.object(
                    autofill,
                    "_extract_app_data",
                    return_value=app_data,
                ),
                mock.patch.object(
                    autofill,
                    "generate_application_answers",
                    return_value={},
                ),
            ):
                payload = autofill._build_payload(out_dir, provider="claude")

        self.assertEqual(payload["unknown_questions"], [])
        office_location = next(
            step for step in payload["steps"] if step["field_name"] == "application_office_location_of_choice"
        )
        self.assertEqual(office_location["kind"], "choice")
        self.assertEqual(office_location["value"], "San Francisco")
        self.assertEqual(office_location["source"], "application_profile.md")

    def test_build_payload_excludes_age_select_from_generated_answer_questions(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        app_data = {
            "posting": {
                "title": "Staff Product Manager, Growth",
                "applicationForm": {"entries": []},
                "surveyForms": [
                    {
                        "entries": [
                            {
                                "isRequired": False,
                                "field": {
                                    "title": "What is your current age?",
                                    "path": "cf_age",
                                    "type": "ValueSelect",
                                    "selectableValues": [
                                        {"label": "Under 30", "value": "under_30"},
                                        {"label": "30-39", "value": "30_39"},
                                        {"label": "40-49", "value": "40_49"},
                                    ],
                                },
                            }
                        ]
                    }
                ],
            }
        }

        def _assert_generated_questions(*, question_specs, **kwargs):
            self.assertFalse(
                any(spec.get("label") == "What is your current age?" for spec in question_specs),
            )
            return {}

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps(
                    {
                        "company": "homebase",
                        "company_proper": "Homebase",
                        "jd_title": "Staff Product Manager, Growth",
                        "jd_source": "https://jobs.ashbyhq.com/homebase/123",
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    autofill,
                    "parse_master_resume",
                    return_value=SimpleNamespace(
                        full_name="Candidate Name",
                        first_name="Candidate",
                        email="candidate@example.com",
                        phone="555-555-5555",
                        linkedin="https://linkedin.com/in/candidate/",
                        website="https://candidate.example.com",
                        employers=set(),
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "parse_application_profile",
                    return_value=SimpleNamespace(
                        verification_code_email="",
                        linkedin="https://linkedin.com/in/candidate/",
                        github="https://github.com/candidate",
                        website="https://candidate.example.com",
                        location="San Francisco, CA",
                        country="United States",
                        text_message_consent=False,
                        age_range="35 - 44",
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "_fetch_application_html",
                    return_value="<html></html>",
                ),
                mock.patch.object(
                    autofill,
                    "_extract_app_data",
                    return_value=app_data,
                ),
                mock.patch.object(
                    autofill,
                    "generate_application_answers",
                    side_effect=_assert_generated_questions,
                ),
            ):
                payload = autofill._build_payload(out_dir, provider="claude")

        age_step = next(step for step in payload["steps"] if step["label"] == "What is your current age?")
        self.assertEqual(age_step["value"], "30-39")
        self.assertEqual(age_step["source"], "application_profile.md")

    def test_build_payload_excludes_privacy_consent_from_generated_answer_questions(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        app_data = {
            "posting": {
                "title": "Staff Product Manager",
                "applicationForm": {"entries": []},
                "surveyForms": [
                    {
                        "entries": [
                            {
                                "isRequired": True,
                                "field": {
                                    "title": (
                                        "Do you consent to Socure processing your data to verify your identity "
                                        "under Socure's Recruiting Privacy Policy?"
                                    ),
                                    "path": "cf_socure_privacy",
                                    "type": "ValueSelect",
                                    "selectableValues": [
                                        {"label": "I agree", "value": "agree"},
                                    ],
                                },
                            }
                        ]
                    }
                ],
            }
        }

        def _assert_generated_questions(*, question_specs, **kwargs):
            self.assertFalse(
                any("Recruiting Privacy Policy" in str(spec.get("label") or "") for spec in question_specs),
            )
            return {}

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps(
                    {
                        "company": "socure",
                        "company_proper": "Socure",
                        "jd_title": "Staff Product Manager",
                        "jd_source": "https://jobs.ashbyhq.com/socure/123",
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    autofill,
                    "parse_master_resume",
                    return_value=SimpleNamespace(
                        full_name="Candidate Name",
                        first_name="Candidate",
                        email="candidate@example.com",
                        phone="555-555-5555",
                        linkedin="https://linkedin.com/in/candidate/",
                        website="https://candidate.example.com",
                        employers=set(),
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "parse_application_profile",
                    return_value=SimpleNamespace(
                        verification_code_email="",
                        linkedin="https://linkedin.com/in/candidate/",
                        github="https://github.com/candidate",
                        website="https://candidate.example.com",
                        location="San Francisco, CA",
                        country="United States",
                        text_message_consent=False,
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "_fetch_application_html",
                    return_value="<html></html>",
                ),
                mock.patch.object(
                    autofill,
                    "_extract_app_data",
                    return_value=app_data,
                ),
                mock.patch.object(
                    autofill,
                    "generate_application_answers",
                    side_effect=_assert_generated_questions,
                ),
            ):
                payload = autofill._build_payload(out_dir, provider="claude")

        privacy_step = next(
            step
            for step in payload["steps"]
            if "Recruiting Privacy Policy" in step["label"]
        )
        self.assertEqual(privacy_step["value"], "I agree")
        self.assertEqual(privacy_step["source"], "auto_consent")

    def test_build_payload_auto_consents_data_processing_select(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        app_data = {
            "posting": {
                "title": "Senior Product Manager",
                "applicationForm": {"entries": []},
                "surveyForms": [
                    {
                        "entries": [
                            {
                                "isRequired": True,
                                "field": {
                                    "title": "Consent to Data Processing",
                                    "path": "cf_data_processing",
                                    "type": "MultiValueSelect",
                                    "selectableValues": [
                                        {"label": "I consent", "value": "consent"},
                                    ],
                                },
                            }
                        ]
                    }
                ],
            }
        }

        def _assert_generated_questions(*, question_specs, **kwargs):
            self.assertFalse(any(spec.get("label") == "Consent to Data Processing" for spec in question_specs))
            return {}

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps(
                    {
                        "company": "dandy",
                        "company_proper": "Dandy",
                        "jd_title": "Senior Product Manager",
                        "jd_source": "https://jobs.ashbyhq.com/dandy/example",
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    autofill,
                    "parse_master_resume",
                    return_value=SimpleNamespace(
                        full_name="Candidate Name",
                        first_name="Candidate",
                        email="candidate@example.com",
                        phone="555-555-5555",
                        linkedin="https://linkedin.com/in/candidate/",
                        website="https://candidate.example.com",
                        employers=set(),
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "parse_application_profile",
                    return_value=SimpleNamespace(
                        verification_code_email="",
                        linkedin="https://linkedin.com/in/candidate/",
                        github="https://github.com/candidate",
                        website="https://candidate.example.com",
                        location="San Francisco, CA",
                        country="United States",
                        text_message_consent=False,
                    ),
                ),
                mock.patch.object(autofill, "_fetch_application_html", return_value="<html></html>"),
                mock.patch.object(autofill, "_extract_app_data", return_value=app_data),
                mock.patch.object(
                    autofill,
                    "generate_application_answers",
                    side_effect=_assert_generated_questions,
                ),
            ):
                payload = autofill._build_payload(out_dir, provider="claude")

        consent_step = next(step for step in payload["steps"] if step["label"] == "Consent to Data Processing")
        self.assertEqual(consent_step["value"], "I consent")
        self.assertEqual(consent_step["source"], "auto_consent")

    def test_infer_step_auto_consents_ai_policy_text_acknowledgement(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        field = {
            "field_name": "application_ai_policy",
            "label": "Do you acknowledge and agree to comply with Abridge's AI policy during the interview process?",
            "path": "cf_ai_policy",
            "required": True,
            "field_type": "String",
        }

        step = autofill._infer_step(
            field,
            meta={},
            profile=SimpleNamespace(),
            application_profile=SimpleNamespace(),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "text")
        self.assertEqual(step["value"], "Yes")
        self.assertEqual(step["source"], "auto_consent")

    def test_infer_step_auto_consents_airwallex_ai_tools_agreement(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        field = {
            "field_name": "application_ai_tools_agreement",
            "label": (
                "At Airwallex, we recognize the benefits AI brings to professional environments, and welcome its use "
                "after you join our team. However, for this application and for any future interviews with our team, "
                "we ask that you do not use any AI tools. This helps us get a true sense of who you are and how you "
                "express yourself in your own words. Please indicate your understanding and agreement with this "
                "approach by selecting 'Yes' below."
            ),
            "path": "cf_ai_tools_agreement",
            "required": True,
            "field_type": "ValueSelect",
            "raw_field": {
                "selectableValues": [
                    {"label": "Yes", "value": "yes"},
                    {"label": "No", "value": "no"},
                ]
            },
        }

        step = autofill._infer_step(
            field,
            meta={},
            profile=SimpleNamespace(),
            application_profile=SimpleNamespace(),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "choice")
        self.assertEqual(step["value"], "Yes")
        self.assertEqual(step["source"], "auto_consent")

    def test_build_payload_excludes_disability_status_from_generated_answer_questions(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        app_data = {
            "posting": {
                "title": "Senior Product Manager",
                "applicationForm": {"entries": []},
                "surveyForms": [
                    {
                        "entries": [
                            {
                                "isRequired": False,
                                "field": {
                                    "title": "Disability Status",
                                    "path": "cf_disability_status",
                                    "type": "ValueSelect",
                                    "selectableValues": [
                                        {
                                            "label": "No, I do not have a disability and have not had one in the past",
                                            "value": "no_disability",
                                        },
                                        {"label": "Yes", "value": "yes"},
                                    ],
                                },
                            }
                        ]
                    }
                ],
            }
        }

        def _assert_generated_questions(*, question_specs, **kwargs):
            self.assertFalse(
                any(spec.get("label") == "Disability Status" for spec in question_specs),
            )
            return {}

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps(
                    {
                        "company": "handshake",
                        "company_proper": "Handshake",
                        "jd_title": "Senior Product Manager",
                        "jd_source": "https://jobs.ashbyhq.com/handshake/123",
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    autofill,
                    "parse_master_resume",
                    return_value=SimpleNamespace(
                        full_name="Candidate Name",
                        first_name="Candidate",
                        email="candidate@example.com",
                        phone="555-555-5555",
                        linkedin="https://linkedin.com/in/candidate/",
                        website="https://candidate.example.com",
                        employers=set(),
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "parse_application_profile",
                    return_value=SimpleNamespace(
                        verification_code_email="",
                        linkedin="https://linkedin.com/in/candidate/",
                        github="https://github.com/candidate",
                        website="https://candidate.example.com",
                        location="San Francisco, CA",
                        country="United States",
                        text_message_consent=False,
                        disability_status="No, I do not have a disability and have not had one in the past",
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "_fetch_application_html",
                    return_value="<html></html>",
                ),
                mock.patch.object(
                    autofill,
                    "_extract_app_data",
                    return_value=app_data,
                ),
                mock.patch.object(
                    autofill,
                    "generate_application_answers",
                    side_effect=_assert_generated_questions,
                ),
            ):
                payload = autofill._build_payload(out_dir, provider="claude")

        disability_step = next(step for step in payload["steps"] if step["label"] == "Disability Status")
        self.assertEqual(
            disability_step["value"],
            "No, I do not have a disability and have not had one in the past",
        )
        self.assertEqual(disability_step["source"], "application_profile.md")

    def test_build_payload_routes_positive_fit_string_prompt_through_generated_answer_questions(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        prompt = "Do you have experience with GenAI/LLMs?"
        app_data = {
            "posting": {
                "title": "Staff Product Manager",
                "applicationForm": {
                    "entries": [
                        {
                            "isRequired": True,
                            "field": {
                                "title": prompt,
                                "path": "application_genai_experience",
                                "type": "String",
                            },
                        }
                    ]
                },
            }
        }

        def _assert_generated_questions(*, question_specs, **kwargs):
            self.assertTrue(any(spec.get("label") == prompt for spec in question_specs))
            field_name = next(spec["field_name"] for spec in question_specs if spec.get("label") == prompt)
            return {field_name: "Yes. I have used AI coding agents and shipped AI-assisted workflows in product work."}

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps({"company": "fiddler-ai", "company_proper": "Fiddler AI", "jd_source": "https://jobs.ashbyhq.com/fiddler-ai/123"}),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    autofill,
                    "parse_master_resume",
                    return_value=SimpleNamespace(
                        full_name="Candidate Name",
                        first_name="Candidate",
                        email="candidate@example.com",
                        phone="555-555-5555",
                        linkedin="https://linkedin.com/in/candidate/",
                        website="https://candidate.example.com",
                        employers=set(),
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "parse_application_profile",
                    return_value=SimpleNamespace(
                        verification_code_email="",
                        linkedin="https://linkedin.com/in/candidate/",
                        github="https://github.com/candidate",
                        website="https://candidate.example.com",
                        location="San Francisco, CA",
                        country="United States",
                        text_message_consent=False,
                    ),
                ),
                mock.patch.object(autofill, "_fetch_application_html", return_value="<html></html>"),
                mock.patch.object(autofill, "_extract_app_data", return_value=app_data),
                mock.patch.object(
                    autofill,
                    "generate_application_answers",
                    side_effect=_assert_generated_questions,
                ),
            ):
                payload = autofill._build_payload(out_dir, provider="claude")

        step = next(step for step in payload["steps"] if step["label"] == prompt)
        self.assertIn("AI coding agents", step["value"])
        self.assertEqual(step["source"], "generated_application_answer")

    def test_build_payload_routes_accommodation_detail_prompt_through_generated_answer_questions(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        prompt = (
            "If you have a disability as defined by applicable laws or regulations and need an accommodation to "
            "enable you to go through our recruitment process to be considered for an open role with ClickUp, "
            "please describe your requested accommodation(s). ClickUp will evaluate every accommodation request "
            "on a case-by-case basis and will discuss with you whether your request is something ClickUp can "
            "reasonably provide. If this does not apply to you, please skip this question."
        )
        app_data = {
            "posting": {
                "title": "Staff Product Manager, AI",
                "applicationForm": {
                    "entries": [
                        {
                            "isRequired": False,
                            "field": {
                                "title": prompt,
                                "path": "application_clickup_accommodation",
                                "type": "String",
                            },
                        }
                    ]
                },
            }
        }

        def _assert_generated_questions(*, question_specs, **kwargs):
            self.assertTrue(any(spec.get("label") == prompt for spec in question_specs))
            field_name = next(spec["field_name"] for spec in question_specs if spec.get("label") == prompt)
            return {field_name: "N/A"}

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps({"company": "clickup", "company_proper": "ClickUp", "jd_source": "https://jobs.ashbyhq.com/clickup/123"}),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    autofill,
                    "parse_master_resume",
                    return_value=SimpleNamespace(
                        full_name="Candidate Name",
                        first_name="Candidate",
                        email="candidate@example.com",
                        phone="555-555-5555",
                        linkedin="https://linkedin.com/in/candidate/",
                        website="https://candidate.example.com",
                        employers=set(),
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "parse_application_profile",
                    return_value=SimpleNamespace(
                        verification_code_email="",
                        linkedin="https://linkedin.com/in/candidate/",
                        github="https://github.com/candidate",
                        website="https://candidate.example.com",
                        location="San Francisco, CA",
                        country="United States",
                        text_message_consent=False,
                    ),
                ),
                mock.patch.object(autofill, "_fetch_application_html", return_value="<html></html>"),
                mock.patch.object(autofill, "_extract_app_data", return_value=app_data),
                mock.patch.object(
                    autofill,
                    "generate_application_answers",
                    side_effect=_assert_generated_questions,
                ),
            ):
                payload = autofill._build_payload(out_dir, provider="claude")

        step = next(step for step in payload["steps"] if step["label"] == prompt)
        self.assertEqual(step["value"], "N/A")
        self.assertEqual(step["source"], "generated_application_answer")

    def test_build_payload_auto_agrees_to_future_contact_consent(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        app_data = {
            "posting": {
                "title": "Senior Product Manager",
                "applicationForm": {"entries": []},
                "surveyForms": [
                    {
                        "entries": [
                            {
                                "isRequired": False,
                                "field": {
                                    "title": "Future Contact Consent",
                                    "path": "_systemfield_data_consent_ack",
                                    "type": "MultiValueSelect",
                                    "selectableValues": [
                                        {"label": "I agree", "value": "data_consent_ack"},
                                    ],
                                },
                            }
                        ]
                    }
                ],
            }
        }

        def _assert_generated_questions(*, question_specs, **kwargs):
            self.assertFalse(
                any(spec.get("label") == "Future Contact Consent" for spec in question_specs),
            )
            return {}

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps(
                    {
                        "company": "socure",
                        "company_proper": "Socure",
                        "jd_title": "Senior Product Manager",
                        "jd_source": "https://jobs.ashbyhq.com/socure/456",
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    autofill,
                    "parse_master_resume",
                    return_value=SimpleNamespace(
                        full_name="Candidate Name",
                        first_name="Candidate",
                        email="candidate@example.com",
                        phone="555-555-5555",
                        linkedin="https://linkedin.com/in/candidate/",
                        website="https://candidate.example.com",
                        employers=set(),
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "parse_application_profile",
                    return_value=SimpleNamespace(
                        verification_code_email="",
                        linkedin="https://linkedin.com/in/candidate/",
                        github="https://github.com/candidate",
                        website="https://candidate.example.com",
                        location="San Francisco, CA",
                        country="United States",
                        text_message_consent=False,
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "_fetch_application_html",
                    return_value="<html></html>",
                ),
                mock.patch.object(
                    autofill,
                    "_extract_app_data",
                    return_value=app_data,
                ),
                mock.patch.object(
                    autofill,
                    "generate_application_answers",
                    side_effect=_assert_generated_questions,
                ),
            ):
                payload = autofill._build_payload(out_dir, provider="claude")

        self.assertEqual(payload["unknown_questions"], [])
        self.assertEqual(
            payload["steps"],
            [
                {
                    "field_name": "survey_1_future_contact_consent",
                    "label": "Future Contact Consent",
                    "field_type": "MultiValueSelect",
                    "kind": "choice",
                    "required": False,
                    "value": "I agree",
                    "path": "_systemfield_data_consent_ack",
                    "source": "auto_consent",
                }
            ],
        )

    def test_build_payload_skips_optional_name_pronunciation_without_source(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        app_data = {
            "posting": {
                "title": "Senior Product Manager",
                "applicationForm": {
                    "entries": [
                        {
                            "isRequired": False,
                            "field": {
                                "title": "Name Pronunciation",
                                "path": "cf_name_pronunciation",
                                "type": "String",
                            },
                        }
                    ]
                },
                "surveyForms": [],
            }
        }

        def _assert_generated_questions(*, question_specs, **kwargs):
            self.assertFalse(any(spec.get("label") == "Name Pronunciation" for spec in question_specs))
            return {}

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps(
                    {
                        "company": "Handshake",
                        "company_proper": "Handshake",
                        "jd_title": "Senior Product Manager",
                        "jd_source": "https://jobs.ashbyhq.com/handshake/example",
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    autofill,
                    "parse_master_resume",
                    return_value=SimpleNamespace(
                        full_name="Candidate Name",
                        first_name="Candidate",
                        email="candidate@example.com",
                        phone="555-555-5555",
                        linkedin="https://linkedin.com/in/candidate/",
                        website="https://candidate.example.com",
                        employers=set(),
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "parse_application_profile",
                    return_value=SimpleNamespace(
                        verification_code_email="",
                        linkedin="https://linkedin.com/in/candidate/",
                        github="https://github.com/candidate",
                        website="https://candidate.example.com",
                        location="San Francisco, CA",
                        country="United States",
                        text_message_consent=False,
                    ),
                ),
                mock.patch.object(autofill, "_fetch_application_html", return_value="<html></html>"),
                mock.patch.object(autofill, "_extract_app_data", return_value=app_data),
                mock.patch.object(
                    autofill,
                    "generate_application_answers",
                    side_effect=_assert_generated_questions,
                ),
            ):
                payload = autofill._build_payload(out_dir, provider="claude")

        self.assertEqual(
            payload["unknown_questions"],
            [
                {
                    "field_name": "application_name_pronunciation",
                    "label": "Name Pronunciation",
                    "field_type": "String",
                    "required": False,
                    "path": "cf_name_pronunciation",
                    "status": "unknown_optional",
                }
            ],
        )

    def test_build_payload_skips_if_yes_followup_when_parent_resolves_to_no(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        app_data = {
            "posting": {
                "title": "Staff Product Manager",
                "applicationForm": {"entries": []},
                "surveyForms": [
                    {
                        "entries": [
                            {
                                "isRequired": True,
                                "field": {
                                    "title": (
                                        "Are you subject to a non-competition agreement or any other agreement "
                                        "which would preclude or restrict your employment at NerdWallet or any "
                                        "other NerdWallet subsidiary?"
                                    ),
                                    "path": "cf_noncompete",
                                    "type": "Boolean",
                                },
                            },
                            {
                                "isRequired": False,
                                "field": {
                                    "title": (
                                        "If yes, please specify the jurisdiction (state, province, or country) "
                                        "and provide details about your agreements:"
                                    ),
                                    "path": "cf_noncompete_details",
                                    "type": "LongText",
                                },
                            },
                        ]
                    }
                ],
            }
        }

        def _assert_generated_questions(*, question_specs, **kwargs):
            self.assertFalse(
                any(
                    "If yes, please specify the jurisdiction" in str(spec.get("label") or "")
                    for spec in question_specs
                ),
            )
            return {}

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps(
                    {
                        "company": "nerdwallet",
                        "company_proper": "NerdWallet",
                        "jd_title": "Staff Product Manager",
                        "jd_source": "https://jobs.ashbyhq.com/nerdwallet/789",
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    autofill,
                    "parse_master_resume",
                    return_value=SimpleNamespace(
                        full_name="Candidate Name",
                        first_name="Candidate",
                        email="candidate@example.com",
                        phone="555-555-5555",
                        linkedin="https://linkedin.com/in/candidate/",
                        website="https://candidate.example.com",
                        employers={"moody's analytics", "kyte", "allstate"},
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "parse_application_profile",
                    return_value=SimpleNamespace(
                        verification_code_email="",
                        linkedin="https://linkedin.com/in/candidate/",
                        github="https://github.com/candidate",
                        website="https://candidate.example.com",
                        location="San Francisco, CA",
                        country="United States",
                        text_message_consent=False,
                        require_sponsorship_now=False,
                        require_sponsorship_future=False,
                        authorized_to_work_unconditionally=True,
                        minimum_years_experience=True,
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "_fetch_application_html",
                    return_value="<html></html>",
                ),
                mock.patch.object(
                    autofill,
                    "_extract_app_data",
                    return_value=app_data,
                ),
                mock.patch.object(
                    autofill,
                    "generate_application_answers",
                    side_effect=_assert_generated_questions,
                ),
            ):
                payload = autofill._build_payload(out_dir, provider="claude")

        self.assertEqual(payload["unknown_questions"], [])
        noncompete_step = next(
            step for step in payload["steps"] if "non-competition agreement" in step["label"]
        )
        self.assertEqual(noncompete_step["value"], "No")

    def test_build_payload_resolves_shared_policy_select_fields_for_education_and_years_experience(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        app_data = {
            "posting": {
                "title": "Senior Product Manager",
                "applicationForm": {"entries": []},
                "surveyForms": [
                    {
                        "entries": [
                            {
                                "isRequired": True,
                                "field": {
                                    "title": "Did you graduate from a 4 year university?",
                                    "path": "cf_four_year_degree",
                                    "type": "ValueSelect",
                                    "selectableValues": [
                                        {"label": "Yes", "value": "yes"},
                                        {"label": "No", "value": "no"},
                                    ],
                                },
                            },
                            {
                                "isRequired": True,
                                "field": {
                                    "title": "How many years of industry experience do you have?",
                                    "path": "cf_industry_years",
                                    "type": "ValueSelect",
                                    "selectableValues": [
                                        {"label": "0-4 years", "value": "0_4"},
                                        {"label": "5-9 years", "value": "5_9"},
                                        {"label": "10+ years", "value": "10_plus"},
                                    ],
                                },
                            },
                        ]
                    }
                ],
            }
        }

        def _assert_generated_questions(*, question_specs, **kwargs):
            labels = {str(spec.get("label") or "") for spec in question_specs}
            self.assertNotIn("Did you graduate from a 4 year university?", labels)
            self.assertNotIn("How many years of industry experience do you have?", labels)
            return {}

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps(
                    {
                        "company": "rain",
                        "company_proper": "Rain",
                        "jd_title": "Senior Product Manager",
                        "jd_source": "https://jobs.ashbyhq.com/rain/example",
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    autofill,
                    "parse_master_resume",
                    return_value=SimpleNamespace(
                        full_name="Candidate Name",
                        first_name="Candidate",
                        email="candidate@example.com",
                        phone="555-555-5555",
                        linkedin="https://linkedin.com/in/candidate/",
                        website="https://candidate.example.com",
                        employers={"moody's analytics", "kyte", "t-mobile"},
                    ),
                ),
                mock.patch.object(
                    autofill,
                    "parse_application_profile",
                    return_value=SimpleNamespace(
                        verification_code_email="",
                        linkedin="https://linkedin.com/in/candidate/",
                        github="https://github.com/candidate",
                        website="https://candidate.example.com",
                        location="San Francisco, CA",
                        country="United States",
                        text_message_consent=False,
                        education_entries=[
                            "Florida State University; Bachelor of Science in Actuarial Science & Computational Science (Dual Degree)"
                        ],
                        default_skill_years="10",
                    ),
                ),
                mock.patch.object(autofill, "_fetch_application_html", return_value="<html></html>"),
                mock.patch.object(autofill, "_extract_app_data", return_value=app_data),
                mock.patch.object(
                    autofill,
                    "generate_application_answers",
                    side_effect=_assert_generated_questions,
                ),
            ):
                payload = autofill._build_payload(out_dir, provider="claude")

        self.assertEqual(payload["unknown_questions"], [])
        four_year_step = next(step for step in payload["steps"] if step["label"] == "Did you graduate from a 4 year university?")
        experience_step = next(
            step for step in payload["steps"] if step["label"] == "How many years of industry experience do you have?"
        )
        self.assertEqual(four_year_step["kind"], "choice")
        self.assertEqual(four_year_step["value"], "Yes")
        self.assertEqual(experience_step["kind"], "choice")
        self.assertEqual(experience_step["value"], "10+ years")

    def test_write_report_splits_planned_but_unconfirmed_fields(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            payload = {
                "job_title": "Senior Insurance Product Manager",
                "company": "Stand Insurance",
                "job_url": "https://jobs.ashbyhq.com/standinsurance/123",
                "application_url": "https://jobs.ashbyhq.com/standinsurance/123/application",
                "artifacts": {
                    "report_json": str(out_dir / "ashby_autofill_report.json"),
                    "report_markdown": str(out_dir / "ashby_autofill_report.md"),
                    "pre_submit_screenshot": str(out_dir / "ashby_autofill_pre_submit.png"),
                },
                "unknown_questions": [],
                "steps": [],
            }
            runtime = {
                "steps": [
                    {
                        "field_name": "first_name",
                        "label": "First Name",
                        "kind": "text",
                        "value": "Candidate",
                        "source": "master_resume.md",
                        "required": True,
                        "filled": True,
                    },
                    {
                        "field_name": "cover_letter",
                        "label": "Cover Letter",
                        "kind": "file",
                        "file_path": "/tmp/Candidate Name Cover Letter - Stand.pdf",
                        "source": "existing_cover_letter_asset",
                        "required": True,
                    },
                ]
            }

            report_payload = autofill._write_report(payload, runtime)
            saved = json.loads((out_dir / "ashby_autofill_report.json").read_text(encoding="utf-8"))

        self.assertEqual(len(saved["fields"]), 1)
        self.assertEqual(saved["fields"][0]["field_name"], "first_name")
        self.assertEqual(saved["planned_but_unconfirmed_fields"][0]["field_name"], "cover_letter")
        self.assertEqual(report_payload["planned_but_unconfirmed_fields"][0]["field_name"], "cover_letter")

    def test_fill_step_fails_when_cover_letter_text_cannot_be_confirmed(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        class FakeLocator:
            pass

        step = {
            "field_name": "application_cover_letter_text",
            "label": "Cover Letter",
            "path": "cover_letter_text",
            "field_type": "LongText",
            "kind": "text",
            "value": "Dear Hiring Team,\n\nThanks.\n",
            "source": "cover_letter_text.txt",
        }

        with mock.patch.object(autofill, "_field_entry"):
            with mock.patch.object(autofill, "_fillable_text_locator", return_value=FakeLocator()):
                with mock.patch.object(autofill, "_fill_text_value"):
                    with mock.patch.object(autofill, "_confirm_cover_letter_text", return_value=False):
                        with self.assertRaisesRegex(RuntimeError, "Ashby cover letter text"):
                            autofill._fill_step(page=None, step=step)

    def test_fill_step_leaves_visible_self_id_unconfirmed_when_confirmation_fails(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = {
            "field_name": "survey_race",
            "label": "Race or Ethnicity",
            "path": "race",
            "field_type": "ValueSelect",
            "kind": "choice",
            "value": "Hispanic or Latino",
            "source": "application_profile.md",
            "blocks_draft_completion": True,
            "blocker_kind": "visible_self_id",
        }

        with mock.patch.object(autofill, "_field_entry", return_value=object()):
            with mock.patch.object(autofill, "_click_choice", return_value=True):
                with mock.patch.object(autofill, "_confirm_visible_self_id_step", return_value=False):
                    autofill._fill_step(page=mock.Mock(), step=step)

        self.assertFalse(step.get("filled", False))
        self.assertEqual(step["status"], "planned")
        self.assertIn("could not confirm", step["note"].lower())

    def test_infer_step_prefers_sponsorship_for_mixed_work_authorization_prompt(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_future_sponsorship",
                "label": "Would you someday require immigration sponsorship for work authorization?",
                "path": "future_sponsorship",
                "required": True,
                "field_type": "Boolean",
            },
            meta={},
            profile=SimpleNamespace(employers=set()),
            application_profile=SimpleNamespace(
                authorized_to_work_unconditionally=True,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["kind"], "choice")
        self.assertEqual(step["value"], "No")

    def test_infer_step_answers_no_for_canada_only_work_authorization_prompt(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_authorized_canada",
                "label": "Are you legally authorized to work in Canada?",
                "path": "authorized_canada",
                "required": True,
                "field_type": "Boolean",
            },
            meta={},
            profile=SimpleNamespace(employers=set()),
            application_profile=SimpleNamespace(
                authorized_to_work_unconditionally=True,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["kind"], "choice")
        self.assertEqual(step["value"], "No")

    def test_infer_step_leaves_conditional_sponsorship_followup_blank(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_sponsorship_details",
                "label": (
                    "If you answered 'Yes' to requiring sponsorship now or in the future, "
                    "please feel free to provide additional details (Optional)"
                ),
                "path": "sponsorship_details",
                "required": False,
                "field_type": "LongText",
            },
            meta={},
            profile=SimpleNamespace(employers=set()),
            application_profile=SimpleNamespace(
                authorized_to_work_unconditionally=True,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNone(step)

    def test_infer_step_matches_u_s_person_valueselect_option(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_us_person_status",
                "label": "A “U.S. person” is a citizen, legal permanent resident, or legal temporary resident (i.e., a refugee or asylee) of the United States. Which of the following best describes your “U.S. person” status?",
                "path": "us_person_status",
                "required": True,
                "field_type": "ValueSelect",
                "raw_field": {
                    "selectableValues": [
                        {"label": "I am a U.S. person", "value": "I am a U.S. person"},
                        {
                            "label": "I am a citizen of Cuba, Iran, North Korea, or Syria AND I am NOT a U.S. person",
                            "value": "I am a citizen of Cuba, Iran, North Korea, or Syria AND I am NOT a U.S. person",
                        },
                        {
                            "label": "None of the above; I am a citizen of a different country",
                            "value": "None of the above; I am a citizen of a different country",
                        },
                    ]
                },
            },
            meta={},
            profile=SimpleNamespace(employers=set()),
            application_profile=SimpleNamespace(
                authorized_to_work_unconditionally=True,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["kind"], "choice")
        self.assertEqual(step["value"], "I am a U.S. person")
        self.assertEqual(step["source"], "master_resume.md")

    def test_infer_step_answers_no_for_government_procurement_employment_prompt(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_public_sector_procurement_employment",
                "label": "Have you ever been directly employed by (1) any government or military entity, state-owned enterprise, or publicly-funded institution, or (2) a government contractor in a role that recommended Snowflake as part of Government procurement?",
                "path": "public_sector_procurement_employment",
                "required": True,
                "field_type": "Boolean",
            },
            meta={},
            profile=SimpleNamespace(employers=set()),
            application_profile=SimpleNamespace(
                authorized_to_work_unconditionally=True,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["kind"], "choice")
        self.assertEqual(step["value"], "No")
        self.assertEqual(step["source"], "deterministic")

    def test_infer_step_sponsorship_valueselect_matches_descriptive_options(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        selectable_values = [
            {
                "label": "No, I do not require visa sponsorship to work in the United States",
                "value": "No, I do not require visa sponsorship to work in the United States",
            },
            {
                "label": "Yes, I require visa sponsorship to work in the United States",
                "value": "Yes, I require visa sponsorship to work in the United States",
            },
            {
                "label": "No, I do not require work permit sponsorship to work in Canada",
                "value": "No, I do not require work permit sponsorship to work in Canada",
            },
            {
                "label": "Yes, I require work permit sponsorship to work in Canada",
                "value": "Yes, I require work permit sponsorship to work in Canada",
            },
        ]
        field = {
            "field_name": "application_visa_sponsorship",
            "label": "Do you now, or will you at any time in the future, require visa/work permit sponsorship?",
            "path": "visa_field_uuid",
            "required": True,
            "field_type": "ValueSelect",
            "raw_field": {"selectableValues": selectable_values},
        }

        step = autofill._infer_step(
            field,
            meta={},
            profile=SimpleNamespace(employers=set()),
            application_profile=SimpleNamespace(
                authorized_to_work_unconditionally=True,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["kind"], "choice")
        self.assertEqual(step["value"], "No, I do not require visa sponsorship to work in the United States")
        self.assertEqual(step["source"], "application_profile.md")

    def test_infer_step_sponsorship_valueselect_prefers_location_based_na_option(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        field = {
            "field_name": "application_canada_sponsorship",
            "label": "Will you now or in the future require sponsorship for employment in Canada?",
            "path": "canada_sponsorship_uuid",
            "required": True,
            "field_type": "ValueSelect",
            "raw_field": {
                "selectableValues": [
                    {
                        "label": "N/A - I am based in the United States",
                        "value": "N/A - I am based in the United States",
                    },
                    {
                        "label": "I will require sponsorship in the future for employment in Canada",
                        "value": "I will require sponsorship in the future for employment in Canada",
                    },
                    {
                        "label": "I do not require sponsorship in the future for employment in Canada",
                        "value": "I do not require sponsorship in the future for employment in Canada",
                    },
                ]
            },
        }

        step = autofill._infer_step(
            field,
            meta={},
            profile=SimpleNamespace(employers=set()),
            application_profile=SimpleNamespace(
                country="United States",
                location="San Francisco, CA",
                authorized_to_work_unconditionally=True,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
                sponsorship_answer="No, I do not require sponsorship.",
                work_authorization_statement="I am always authorized to work in the United States unconditionally.",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["kind"], "choice")
        self.assertEqual(step["value"], "N/A - I am based in the United States")
        self.assertEqual(step["source"], "application_profile.md")

    def test_infer_step_handles_commuting_distance_variant(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_commuting_distance",
                "label": "Are you currently based within commuting distance of downtown San Francisco",
                "path": "commuting_distance",
                "required": True,
                "field_type": "Boolean",
            },
            meta={},
            profile=SimpleNamespace(),
            application_profile=SimpleNamespace(
                lives_in_job_location=True,
                comfortable_working_on_site=True,
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(step["kind"], "choice")
        self.assertEqual(step["value"], "Yes")
        self.assertEqual(step["source"], "shared_positive_fit_policy")

    def test_infer_step_adds_text_message_consent_to_phone_fields(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_phone_number",
                "label": "Phone Number",
                "path": "candidate_phone",
                "required": True,
                "field_type": "Phone",
            },
            meta={},
            profile=SimpleNamespace(phone="555-0100"),
            application_profile=SimpleNamespace(text_message_consent=False),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(step["kind"], "text")
        self.assertEqual(step["value"], "555-0100")
        self.assertFalse(step["text_message_consent"])
        self.assertTrue(step["blocks_draft_completion"])
        self.assertEqual(step["blocker_kind"], "visible_profile_field")
        self.assertEqual(step["profile_field"], "phone")

    def test_infer_step_answers_insurance_product_manager_background_gate(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_insurance_background",
                "label": "This role requires Insurance Industry background. Have you been a Product Manager within the Insurance industry?",
                "path": "insurance_background",
                "required": True,
                "field_type": "Boolean",
            },
            meta={},
            profile=SimpleNamespace(employers={"moody's analytics"}),
            application_profile=SimpleNamespace(),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(step["kind"], "choice")
        self.assertEqual(step["value"], "Yes")
        self.assertEqual(step["source"], "master_resume.md")

    def test_infer_step_maps_optional_eeoc_value_selects(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        gender_step = autofill._infer_step(
            {
                "field_name": "survey_1_gender",
                "label": "Gender",
                "path": "_systemfield_eeoc_gender",
                "required": False,
                "field_type": "ValueSelect",
                "raw_field": {
                    "selectableValues": [
                        {"label": "Male", "value": "male"},
                        {"label": "Female", "value": "female"},
                    ]
                },
            },
            meta={},
            profile=SimpleNamespace(employers=set()),
            application_profile=SimpleNamespace(
                gender="Male",
                race_or_ethnicity="Hispanic or Latino",
                veteran_status="I am not a protected veteran",
                disability_status="No, I do not have a disability and have not had one in the past",
                sexual_orientation="Straight / Heterosexual",
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(gender_step["kind"], "choice")
        self.assertEqual(gender_step["value"], "Male")

    def test_infer_step_maps_age_range_value_select_to_truthful_bucket(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        age_step = autofill._infer_step(
            {
                "field_name": "survey_1_current_age",
                "label": "What is your current age?",
                "path": "_systemfield_eeoc_age",
                "required": False,
                "field_type": "ValueSelect",
                "raw_field": {
                    "selectableValues": [
                        {"label": "Under 30", "value": "under_30"},
                        {"label": "30-39", "value": "30_39"},
                        {"label": "40-49", "value": "40_49"},
                    ]
                },
            },
            meta={},
            profile=SimpleNamespace(employers=set()),
            application_profile=SimpleNamespace(age_range="35 - 44"),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(age_step["kind"], "choice")
        self.assertEqual(age_step["value"], "30-39")
        self.assertEqual(age_step["source"], "application_profile.md")
        self.assertEqual(age_step["profile_field"], "age_range")

    def test_choice_text_matches_rejects_male_inside_female(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        self.assertFalse(autofill._choice_text_matches("Male", "Female"))
        self.assertTrue(autofill._choice_text_matches("Male", "Cisgender Male/Man"))
        self.assertFalse(
            autofill._choice_text_matches(
                "Yes",
                "Are you legally authorized to work in the United States? (Yes/No)",
            )
        )
        self.assertFalse(
            autofill._choice_text_matches(
                "No",
                "Are you legally authorized to work in the United States? (Yes/No)",
            )
        )

    def test_click_choice_prefers_visible_label_input_for_exact_match(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        class _FakeCollection:
            def __init__(self, items=None):
                self.items = items or []

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

            def filter(self, *, has_text=None):
                if has_text is None:
                    return self
                return _FakeCollection(
                    [item for item in self.items if has_text in getattr(item, "text", "")]
                )

            @property
            def first(self):
                return self.items[0] if self.items else _FakeEmptyLocator()

        class _FakeEmptyLocator:
            def count(self):
                return 0

        class _FakeInput:
            def __init__(self):
                self.check_calls = 0
                self.checked = False

            def count(self):
                return 1

            def check(self, force=False):
                self.check_calls += 1
                self.checked = True

            def is_checked(self):
                return self.checked

        class _FakeLabel:
            def __init__(self, text, *, visible, wrapped_input):
                self.text = text
                self.visible = visible
                self.wrapped_input = wrapped_input
                self.click_calls = 0

            def is_visible(self):
                return self.visible

            def inner_text(self):
                return self.text

            def locator(self, selector):
                if selector == "input[type='radio'], input[type='checkbox']":
                    return _FakeCollection([self.wrapped_input])
                return _FakeCollection()

            def click(self):
                self.click_calls += 1

        class _FakeRoleChoice:
            def __init__(self, *, visible):
                self.visible = visible
                self.click_calls = 0

            def is_visible(self):
                return self.visible

            def click(self):
                self.click_calls += 1

        class _FakeEntry:
            def __init__(self, labels, *, roles=None):
                self.labels = labels
                self.roles = roles or {}

            def get_by_role(self, role, name):
                return self.roles.get((role, name), _FakeCollection())

            def locator(self, selector):
                if selector == "label":
                    return _FakeCollection(self.labels)
                return _FakeCollection()

        hidden_input = _FakeInput()
        visible_input = _FakeInput()
        radio_candidate = _FakeRoleChoice(visible=True)
        entry = _FakeEntry(
            [
                _FakeLabel("Male", visible=False, wrapped_input=hidden_input),
                _FakeLabel("Male", visible=True, wrapped_input=visible_input),
            ],
            roles={("radio", "Male"): _FakeCollection([radio_candidate])},
        )

        self.assertTrue(autofill._click_choice(entry, "Male"))
        self.assertEqual(radio_candidate.click_calls, 0)
        self.assertEqual(hidden_input.check_calls, 0)
        self.assertEqual(visible_input.check_calls, 1)

    def test_click_choice_skips_question_label_that_only_mentions_yes_no(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        class _FakeCollection:
            def __init__(self, items=None):
                self.items = items or []

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

            def filter(self, *, has_text=None):
                if has_text is None:
                    return self
                return _FakeCollection(
                    [item for item in self.items if has_text in getattr(item, "text", "")]
                )

            @property
            def first(self):
                return self.items[0] if self.items else _FakeEmptyLocator()

        class _FakeEmptyLocator:
            def count(self):
                return 0

        class _FakeInput:
            def __init__(self):
                self.check_calls = 0

            def count(self):
                return 1

            def check(self, force=False):
                del force
                self.check_calls += 1

            def is_checked(self):
                return self.check_calls > 0

        class _FakeLabel:
            def __init__(self, text, *, visible=True, wrapped_input=None):
                self.text = text
                self.visible = visible
                self.wrapped_input = wrapped_input
                self.click_calls = 0

            def is_visible(self):
                return self.visible

            def inner_text(self):
                return self.text

            def locator(self, selector):
                if selector == "input[type='radio'], input[type='checkbox']" and self.wrapped_input is not None:
                    return _FakeCollection([self.wrapped_input])
                return _FakeCollection()

            def click(self):
                self.click_calls += 1

        class _FakeEntry:
            def __init__(self, labels):
                self.labels = labels

            def get_by_role(self, role, name):
                del role, name
                return _FakeCollection()

            def locator(self, selector):
                if selector == "label":
                    return _FakeCollection(self.labels)
                return _FakeCollection()

        question_label = _FakeLabel("Are you legally authorized to work in the United States? (Yes/No)")
        yes_input = _FakeInput()
        yes_label = _FakeLabel("Yes", wrapped_input=yes_input)
        no_label = _FakeLabel("No", wrapped_input=_FakeInput())
        entry = _FakeEntry([question_label, yes_label, no_label])

        self.assertTrue(autofill._click_choice(entry, "Yes"))
        self.assertEqual(question_label.click_calls, 0)
        self.assertEqual(yes_input.check_calls, 1)

    def test_click_choice_scrolls_and_clicks_visible_label_when_for_linked_input_does_not_wrap_control(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        class _FakeCollection:
            def __init__(self, items=None):
                self.items = items or []

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

            def filter(self, *, has_text=None):
                if has_text is None:
                    return self
                return _FakeCollection(
                    [item for item in self.items if has_text in getattr(item, "text", "")]
                )

            @property
            def first(self):
                return self.items[0] if self.items else _FakeEmptyLocator()

        class _FakeEmptyLocator:
            def count(self):
                return 0

            def get_attribute(self, _name):
                return None

        class _FakeInput:
            def __init__(self):
                self.dom_click_calls = 0

        class _FakeOption:
            def __init__(self, text):
                self.text = text
                self.scroll_calls = 0

            def count(self):
                return 1

            def scroll_into_view_if_needed(self):
                self.scroll_calls += 1

        class _FakeLabel:
            def __init__(self, text, *, for_id, option):
                self.text = text
                self.for_id = for_id
                self.option = option
                self.click_calls = 0
                self.scroll_calls = 0

            def is_visible(self):
                return True

            def inner_text(self):
                return self.text

            def scroll_into_view_if_needed(self):
                self.scroll_calls += 1

            def locator(self, selector):
                del selector
                return _FakeCollection()

            def get_attribute(self, name):
                return self.for_id if name == "for" else None

            def click(self):
                if not self.scroll_calls or not self.option.scroll_calls:
                    raise AssertionError("option and label must be scrolled into view before click")
                self.click_calls += 1

        class _FakeEntry:
            def __init__(self, labels, *, options, inputs_by_id):
                self.labels = labels
                self.options = options
                self.inputs_by_id = inputs_by_id
                self.page = mock.Mock()

            def get_by_role(self, role, name):
                del role, name
                return _FakeCollection()

            def locator(self, selector):
                if selector == "label":
                    return _FakeCollection(self.labels)
                if selector == 'div[class*="_option_"]':
                    return _FakeCollection(self.options)
                return _FakeCollection()

        no_input = _FakeInput()
        no_option = _FakeOption("No")
        no_label = _FakeLabel("No", for_id="canada-no", option=no_option)
        entry = _FakeEntry(
            [no_label],
            options=[no_option],
            inputs_by_id={"canada-no": no_input},
        )

        self.assertTrue(autofill._click_choice(entry, "No"))
        entry.page.wait_for_timeout.assert_called_once_with(250)
        self.assertEqual(no_option.scroll_calls, 1)
        self.assertEqual(no_label.scroll_calls, 1)
        self.assertEqual(no_label.click_calls, 1)

    def test_fill_step_accepts_choice_rendered_as_combobox(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        class _FakeLocator:
            def __init__(self, count=0):
                self._count = count

            def count(self):
                return self._count

            @property
            def first(self):
                return self

        class _FakeEntry:
            def locator(self, selector):
                if selector == '[role="combobox"]':
                    return _FakeLocator(1)
                return _FakeLocator(0)

        page = mock.Mock()
        step = {
            "label": "How did you hear about this job?",
            "path": "how_did_you_hear",
            "kind": "choice",
            "value": "NerdWallet Careers Page",
            "required": True,
            "field_type": "ValueSelect",
        }

        with (
            mock.patch.object(autofill, "_field_entry", return_value=_FakeEntry()),
            mock.patch.object(autofill, "_click_choice", return_value=False),
            mock.patch.object(autofill, "_select_choice_via_combobox", return_value=True) as select_via_combobox,
        ):
            autofill._fill_step(page, step)

        select_via_combobox.assert_called_once()
        self.assertTrue(step["filled"])

    def test_fill_step_matches_label_less_consent_field_by_path(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        html = """
        <fieldset class="_container_1v5e2_29 _fieldEntry_17tft_29">
          <label class="_heading_101oc_53 _label_17tft_43 ashby-application-form-question-title" for="_systemfield_data_consent_ack"></label>
          <div class="_description_17tft_49 ashby-application-form-question-description">
            <p>By providing my personal information, I consent to Tools For Humanity Corp. (TFH).</p>
          </div>
          <div class="_option_1v5e2_35">
            <span class="_container_1hpbx_29" data-disabled="false">
              <input
                type="checkbox"
                id="0ce25c67-8062-4313-ba0f-88369f99d28c__systemfield_data_consent_ack-labeled-checkbox-0"
                name="I agree"
              />
            </span>
            <label
              for="0ce25c67-8062-4313-ba0f-88369f99d28c__systemfield_data_consent_ack-labeled-checkbox-0"
              class="_label_1v5e2_43"
            >
              I agree
            </label>
          </div>
        </fieldset>
        """

        step = {
            "field_name": "survey_1_future_contact_consent",
            "label": "Future Contact Consent",
            "path": "_systemfield_data_consent_ack",
            "required": False,
            "field_type": "MultiValueSelect",
            "kind": "choice",
            "value": "I agree",
            "source": "auto_consent",
        }

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)

            autofill._fill_step(page, step)

            self.assertTrue(step["filled"])
            self.assertTrue(page.locator('input[type="checkbox"]').is_checked())
            browser.close()

    def test_confirm_visible_self_id_step_rejects_substring_collision(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        class _FakeEntry:
            def __init__(self, selected_texts):
                self.selected_texts = selected_texts

            def evaluate(self, _script):
                return self.selected_texts

        self.assertFalse(
            autofill._confirm_visible_self_id_step(
                _FakeEntry(["Female"]),
                {"kind": "choice", "value": "Male"},
            )
        )
        self.assertTrue(
            autofill._confirm_visible_self_id_step(
                _FakeEntry(["Cisgender Male/Man"]),
                {"kind": "choice", "value": "Male"},
            )
        )
        self.assertFalse(
            autofill._confirm_visible_self_id_step(
                _FakeEntry(["Are you legally authorized to work in the United States? (Yes/No)"]),
                {"kind": "choice", "value": "Yes"},
            )
        )

    def test_infer_step_maps_metro_area_select_from_candidate_location(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        application_profile = SimpleNamespace(
            location="San Francisco, CA",
            country="United States",
        )

        metro_area = autofill._infer_step(
            {
                "field_name": "application_metro_area",
                "label": "Do you live in one of the following metro areas?",
                "path": "metro_area",
                "required": True,
                "field_type": "ValueSelect",
                "raw_field": {
                    "selectableValues": [
                        {"label": "Chicago Metro Area", "value": "Chicago Metro Area"},
                        {"label": "NYC Metro Area", "value": "NYC Metro Area"},
                        {"label": "None", "value": "None"},
                        {"label": "San Francisco Bay Area", "value": "San Francisco Bay Area"},
                    ]
                },
            },
            meta={},
            profile=SimpleNamespace(),
            application_profile=application_profile,
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(metro_area["kind"], "choice")
        self.assertEqual(metro_area["value"], "San Francisco Bay Area")

    def test_infer_step_maps_office_location_choice_from_candidate_location(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        application_profile = SimpleNamespace(
            location="San Francisco, CA",
            country="United States",
        )

        office_location = autofill._infer_step(
            {
                "field_name": "application_office_location_of_choice",
                "label": "Office location of choice",
                "path": "office_location",
                "required": True,
                "field_type": "ValueSelect",
                "raw_field": {
                    "selectableValues": [
                        {"label": "London", "value": "London"},
                        {"label": "New York", "value": "New York"},
                        {"label": "San Francisco", "value": "San Francisco"},
                    ]
                },
            },
            meta={},
            profile=SimpleNamespace(),
            application_profile=application_profile,
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(office_location["kind"], "choice")
        self.assertEqual(office_location["value"], "San Francisco")

    def test_infer_step_maps_which_office_preference_variant_from_candidate_location(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        application_profile = SimpleNamespace(
            location="San Francisco, CA",
            country="United States",
        )

        office_location = autofill._infer_step(
            {
                "field_name": "application_which_office_would_prefer_to_work_out_of",
                "label": "Which Office would prefer to work out of?",
                "path": "office_preference",
                "required": True,
                "field_type": "MultiValueSelect",
                "raw_field": {
                    "selectableValues": [
                        {"label": "Portland Oregon", "value": "Portland Oregon"},
                        {"label": "San Francisco", "value": "San Francisco"},
                        {"label": "Either One", "value": "Either One"},
                    ]
                },
            },
            meta={},
            profile=SimpleNamespace(),
            application_profile=application_profile,
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertEqual(office_location["kind"], "choice")
        self.assertEqual(office_location["value"], "San Francisco")

    def test_infer_step_maps_plain_location_select_from_candidate_location(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        application_profile = SimpleNamespace(
            location="San Francisco, CA",
            country="United States",
        )

        location_step = autofill._infer_step(
            {
                "field_name": "application_location",
                "label": "Location",
                "path": "application_location",
                "required": True,
                "field_type": "ValueSelect",
                "raw_field": {
                    "selectableValues": [
                        {"label": "San Francisco Bay area", "value": "San Francisco Bay area"},
                        {"label": "New York City area", "value": "New York City area"},
                        {"label": "Remote", "value": "Remote"},
                    ]
                },
            },
            meta={},
            profile=SimpleNamespace(),
            application_profile=application_profile,
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(location_step)
        self.assertEqual(location_step["kind"], "choice")
        self.assertEqual(location_step["value"], "San Francisco Bay area")
        self.assertEqual(location_step["source"], "application_profile.md")

    def test_infer_step_maps_where_are_you_currently_located_select_from_candidate_country(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        application_profile = SimpleNamespace(
            location="San Francisco, CA",
            country="United States",
        )

        location_step = autofill._infer_step(
            {
                "field_name": "application_current_region",
                "label": "Where are you currently located?",
                "path": "application_current_region",
                "required": True,
                "field_type": "ValueSelect",
                "raw_field": {
                    "selectableValues": [
                        {"label": "United States", "value": "United States"},
                        {"label": "Canada", "value": "Canada"},
                        {"label": "Other", "value": "Other"},
                    ]
                },
            },
            meta={},
            profile=SimpleNamespace(),
            application_profile=application_profile,
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(location_step)
        self.assertEqual(location_step["kind"], "choice")
        self.assertEqual(location_step["value"], "United States")
        self.assertEqual(location_step["source"], "application_profile.md")
        self.assertEqual(location_step["profile_field"], "location")

    def test_infer_step_maps_hub_proximity_select_from_candidate_location(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        application_profile = SimpleNamespace(
            location="San Francisco, CA",
            country="United States",
        )

        hub_step = autofill._infer_step(
            {
                "field_name": "application_we_are_a_flexible_remote_first_company",
                "label": (
                    "We are a flexible remote-first company, but we do require employees "
                    "to reside within 50 miles of the hub advertised on the job posting "
                    "for this role. At the time of hire, will you be located within 50 "
                    "miles of one of our hubs? If so, please select which location."
                ),
                "path": "997f6361-8bec-498e-99d5-c4200bd4375d",
                "required": True,
                "field_type": "ValueSelect",
                "raw_field": {
                    "selectableValues": [
                        {"label": "San Francisco", "value": "sf"},
                        {"label": "Los Angeles", "value": "la"},
                        {"label": "New York", "value": "ny"},
                        {"label": "Seattle", "value": "sea"},
                    ]
                },
            },
            meta={},
            profile=SimpleNamespace(),
            application_profile=application_profile,
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(hub_step)
        self.assertEqual(hub_step["kind"], "choice")
        self.assertEqual(hub_step["value"], "San Francisco")
        self.assertEqual(hub_step["source"], "application_profile.md")

    def test_infer_step_prefers_current_hub_residency_over_relocation_when_candidate_is_in_listed_hub(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        application_profile = SimpleNamespace(
            location="San Francisco, CA",
            country="United States",
        )

        hub_step = autofill._infer_step(
            {
                "field_name": "application_hub_location",
                "label": (
                    "For some of our roles, we prefer the employee to sit out of our hubs in "
                    "NYC, SF, Austin or DC. Are you currently located in or interested in relocating to any hub?"
                ),
                "description": "",
                "path": "hub_location",
                "required": True,
                "field_type": "MultiValueSelect",
                "raw_field": {
                    "selectableValues": [
                        {"label": "Currently reside in a hub", "value": "current"},
                        {"label": "Open to relocating to any hub", "value": "relocate"},
                        {"label": "Only considering remote", "value": "remote"},
                    ]
                },
            },
            meta={},
            profile=SimpleNamespace(),
            application_profile=application_profile,
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(hub_step)
        self.assertEqual(hub_step["kind"], "choice")
        self.assertEqual(hub_step["value"], "Currently reside in a hub")
        self.assertEqual(hub_step["source"], "application_profile.md")

    def test_infer_step_does_not_fill_optional_website_password_field(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_portfolio_password",
                "label": "Portfolio/Website password if applicable",
                "path": "portfolio_password",
                "required": False,
                "field_type": "String",
            },
            meta={},
            profile=SimpleNamespace(),
            application_profile=SimpleNamespace(),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNone(step)

    def test_fill_step_uses_choice_controls_for_boolean_buttons(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        class FakeLocator:
            def __init__(self, *, count=0):
                self._count = count
                self.first = self
                self.clicked = False

            def count(self):
                return self._count

            def click(self):
                self.clicked = True

            def filter(self, **kwargs):
                del kwargs
                return FakeLocator()

            def locator(self, *args, **kwargs):
                del args, kwargs
                return FakeLocator()

            def get_by_role(self, role, name=None):
                if role == "button" and name == "Yes":
                    return FakeLocator(count=1)
                return FakeLocator()

        button = FakeLocator(count=1)
        entry = mock.Mock()
        entry.get_by_role.side_effect = lambda role, name=None: (
            button if role == "button" and name == "Yes" else FakeLocator()
        )
        entry.locator.return_value = FakeLocator()
        entry.evaluate.return_value = ["Yes"]

        step = {
            "field_name": "authorized",
            "label": "Are you authorized to work lawfully in the United States?",
            "path": "2be8a091-6ce1-4bcd-aaff-d06b1f8963ee",
            "kind": "choice",
            "value": "Yes",
        }

        with (
            mock.patch.object(autofill, "_field_entry", return_value=entry),
            mock.patch.object(
                autofill,
                "_fillable_text_locator",
                return_value=None,
            ),
            mock.patch.object(autofill, "human_fill") as human_fill,
        ):
            autofill._fill_step(mock.Mock(), step)

        human_fill.assert_not_called()
        self.assertTrue(step["filled"])

    def test_fill_step_leaves_choice_unconfirmed_when_live_selection_mismatches(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        entry = mock.Mock()
        entry.evaluate.return_value = ["Yes"]

        step = {
            "field_name": "authorized_canada",
            "label": "Are you legally authorized to work in Canada?",
            "path": "0a00d4af-cb26-4375-b923-eff08a762886",
            "kind": "choice",
            "value": "No",
        }

        with (
            mock.patch.object(autofill, "_field_entry", return_value=entry),
            mock.patch.object(autofill, "_click_choice", return_value=True),
            mock.patch.object(autofill, "_fillable_text_locator", return_value=None),
        ):
            autofill._fill_step(mock.Mock(), step)

        self.assertFalse(step["filled"])
        self.assertEqual(step["status"], "planned")
        self.assertIn("could not confirm", step["note"])

    def test_fill_step_rechecks_choice_confirmation_with_fresh_entry_after_rerender(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        stale_entry = mock.Mock()
        stale_entry.evaluate.return_value = ["Yes"]
        fresh_entry = mock.Mock()
        fresh_entry.count.return_value = 1
        fresh_entry.evaluate.return_value = ["No"]

        step = {
            "field_name": "authorized_canada",
            "label": "Are you legally authorized to work in Canada?",
            "path": "0a00d4af-cb26-4375-b923-eff08a762886",
            "kind": "choice",
            "value": "No",
        }

        with (
            mock.patch.object(autofill, "_field_entry", side_effect=[stale_entry, fresh_entry]),
            mock.patch.object(autofill, "_click_choice", return_value=True),
            mock.patch.object(autofill, "_fillable_text_locator", return_value=None),
        ):
            autofill._fill_step(mock.Mock(), step)

        self.assertTrue(step["filled"])

    def test_fill_phone_text_message_consent_uses_visible_label_when_present(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        class FakeLocator:
            def __init__(self, *, count=0):
                self._count = count
                self.first = self
                self.clicked = False
                self.checked = False

            def count(self):
                return self._count

            def click(self):
                self.clicked = True

            def check(self, force=False):
                self.checked = force

        radios = FakeLocator(count=1)
        no_label = FakeLocator(count=1)
        entry = mock.Mock()

        def locate(selector):
            if selector == 'input[name="communicationConsent"]':
                return radios
            if selector == 'label:has(input[name="communicationConsent"][value="notGiven"])':
                return no_label
            return FakeLocator()

        entry.locator.side_effect = locate

        self.assertTrue(autofill._fill_phone_text_message_consent(entry, False))
        self.assertTrue(no_label.clicked)
        self.assertFalse(getattr(radios, "checked", False))

    def test_fill_step_applies_phone_text_message_consent(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        entry = mock.Mock()
        locator = mock.Mock()
        page = mock.Mock()
        step = {
            "field_name": "application_phone_number",
            "label": "Phone Number",
            "path": "candidate_phone",
            "kind": "text",
            "field_type": "Phone",
            "value": "555-0100",
            "text_message_consent": False,
        }

        with (
            mock.patch.object(autofill, "_field_entry", return_value=entry),
            mock.patch.object(
                autofill,
                "_fillable_text_locator",
                return_value=locator,
            ),
            mock.patch.object(autofill, "_fill_text_value") as fill_text,
            mock.patch.object(
                autofill,
                "_fill_phone_text_message_consent",
                return_value=True,
            ) as fill_consent,
        ):
            autofill._fill_step(page, step)

        fill_text.assert_called_once_with(locator, "555-0100", field_type="Phone")
        fill_consent.assert_called_once_with(entry, False)
        self.assertTrue(step["filled"])

    def test_fill_text_value_uses_direct_fill_for_long_text(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        locator = mock.Mock()
        long_text = "platform " * 100

        with mock.patch.object(autofill, "human_fill") as human_fill:
            autofill._fill_text_value(locator, long_text, field_type="LongText")

        locator.click.assert_called_once()
        locator.fill.assert_called_once_with(long_text)
        human_fill.assert_not_called()

    def test_click_submit_button_avoids_navigation_wait(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        button = mock.Mock()
        button.is_visible.return_value = True
        button.is_enabled.return_value = True
        locator = mock.Mock()
        locator.count.return_value = 1
        locator.nth.return_value = button
        page = mock.Mock()
        page.get_by_role.return_value = locator

        clicked = autofill._click_submit_button(page)

        self.assertTrue(clicked)
        button.click.assert_called_once_with(timeout=5000, no_wait_after=True)

    def test_classify_submit_state_prefers_validation_error_over_captcha(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        snapshot = {
            "page_text": (
                "Your form needs corrections. Missing entry for required field: "
                "Are you authorized to work lawfully in the United States?"
            ),
            "form_visible": True,
            "recaptcha_visible": True,
            "recaptcha_challenge_active": False,
            "invalid_fields": ["Phone Number"],
            "errors": [],
        }

        state = autofill._classify_submit_state(snapshot)

        self.assertEqual(state["status"], "validation_error")
        self.assertIn("Phone Number", state["invalid_fields"])

    def test_classify_submit_state_detects_successfully_submitted_banner(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        snapshot = {
            "page_text": (
                "Success Your application was successfully submitted. We'll contact you if there are next steps."
            ),
            "form_visible": False,
            "recaptcha_visible": True,
            "recaptcha_challenge_active": False,
            "invalid_fields": [],
            "errors": [],
        }

        state = autofill._classify_submit_state(snapshot)

        self.assertEqual(state["status"], "confirmed")

    def test_classify_submit_state_ignores_recaptcha_badge_without_active_challenge(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        snapshot = {
            "page_text": "Submit Application",
            "form_visible": True,
            "recaptcha_visible": True,
            "recaptcha_challenge_active": False,
            "invalid_fields": [],
            "errors": [],
        }

        state = autofill._classify_submit_state(snapshot)

        self.assertEqual(state["status"], "pending")

    def test_classify_submit_state_requires_manual_action_for_active_recaptcha_challenge(self):
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")
        snapshot = {
            "page_text": "Submit Application",
            "form_visible": True,
            "recaptcha_visible": True,
            "recaptcha_challenge_active": True,
            "invalid_fields": [],
            "errors": [],
        }

        state = autofill._classify_submit_state(snapshot)

        self.assertEqual(state["status"], "captcha_required")

    def test_infer_step_work_authorization_valueselect_non_yesno(self):
        """ValueSelect with authorization-level options (not yes/no) should match correctly."""
        autofill = load_module("autofill_ashby", "scripts/autofill_ashby.py")

        step = autofill._infer_step(
            {
                "field_name": "application_u_s_work_authorization_status",
                "label": "U.S. Work Authorization Status",
                "path": "73834d29-f2c2-4fee-a651-b987ca034810",
                "required": True,
                "field_type": "ValueSelect",
                "raw_field": {
                    "selectableValues": [
                        {"label": "Can work for any employer", "value": "can_work_any"},
                        {"label": "Can work for current employer", "value": "can_work_current"},
                        {"label": "Seeking work authorization", "value": "seeking"},
                    ]
                },
            },
            meta={},
            profile=SimpleNamespace(employers=set()),
            application_profile=SimpleNamespace(
                authorized_to_work_unconditionally=True,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
            ),
            out_dir=PROJECT_ROOT,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["kind"], "choice")
        self.assertEqual(step["value"], "Can work for any employer")
        self.assertEqual(step["source"], "application_profile.md")


if __name__ == "__main__":
    unittest.main()
