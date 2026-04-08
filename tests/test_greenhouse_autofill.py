import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def schema_allows_type(schema: object, expected_type: str) -> bool:
    if not isinstance(schema, dict):
        return False
    schema_type = schema.get("type")
    if schema_type == expected_type:
        return True
    if isinstance(schema_type, list) and expected_type in schema_type:
        return True
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list) and any(schema_allows_type(branch, expected_type) for branch in branches):
            return True
    return False


class GreenhouseAutofillTests(unittest.TestCase):
    def test_validate_generated_answers_allows_blank_optional_field(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        specs = [
            {"field_name": "optional_follow_up", "required": False},
            {"field_name": "required_answer", "required": True},
        ]
        answers = {
            "optional_follow_up": "",
            "required_answer": "I want to join because the platform work compounds product velocity.",
        }

        validated = autofill._validate_generated_answers(specs, answers)

        self.assertEqual(
            validated,
            {"required_answer": "I want to join because the platform work compounds product velocity."},
        )

    def test_validate_generated_answers_treats_recent_grad_gpa_prompt_as_conditional(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        application_profile = autofill._parse_application_profile(
            """
            - Country: United States
            - Location: San Francisco, CA
            - Work Authorization Statement: I am always authorized to work in the United States unconditionally.
            - Authorized to Work Unconditionally: Yes
            - Require Sponsorship Now: No
            - Require Sponsorship in Future: No
            - Sponsorship Answer: No
            - Compensation Expectations: I'm open and flexible on compensation.
            - Undergraduate GPA: 3.8/4.0
            - Gender: Male
            - Race or Ethnicity: Hispanic or Latino
            - Veteran Status: I am not a protected veteran
            - Disability Status: No, I do not have a disability and have not had one in the past
            - Sexual Orientation: Straight / Heterosexual
            """
        )
        specs = [
            {
                "field_name": "question_35137926002",
                "label": "If you're less than 3 years out of school, what is your undergraduate GPA?",
                "description": "",
                "required": True,
                "type": "input_text",
            }
        ]

        for answers in ({}, {"question_35137926002": None}, {"question_35137926002": ""}):
            validated = autofill._validate_generated_answers(
                specs,
                answers,
                application_profile=application_profile,
            )
            self.assertEqual(validated["question_35137926002"], "3.8/4.0")

    def test_sync_notion_after_submit_delegates_to_helper_module(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            payload = {"out_dir": str(out_dir)}
            outcome = {"status": "confirmed", "reason": "url", "snapshot": {"url": "https://example.com/confirmation"}}

            helper = mock.Mock()
            helper.sync_application.return_value = {"status": "pending_email_confirmation"}

            with mock.patch.object(autofill, "_load_notion_sync_module", return_value=helper):
                result = autofill._sync_notion_after_submit(payload, outcome)

            helper.record_website_confirmation.assert_called_once()
            helper.sync_application.assert_called_once_with(
                out_dir,
                wait_for_email_seconds=90,
                allow_pending_email=True,
                fail_on_missing_token=False,
            )
            self.assertEqual(result["status"], "pending_email_confirmation")

    def test_extract_job_post_from_remix_context(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        html = """
        <html>
          <body>
            <script>
              window.__remixContext = {"state":{"loaderData":{"routes/$url_token_.jobs_.$job_post_id":{"jobPost":{"company_name":"Figma","questions":[{"required":true,"label":"First Name","fields":[{"name":"first_name","type":"input_text"}]}]}}}}};
            </script>
          </body>
        </html>
        """
        job_post = autofill._extract_job_post(html)
        self.assertEqual(job_post["company_name"], "Figma")
        self.assertEqual(job_post["questions"][0]["fields"][0]["name"], "first_name")

    def test_extract_job_post_from_classic_application_form_html(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        html = """
        <html>
          <body>
            <form id="application_form">
              <div class="field">
                <label>First Name <span class="asterisk">*</span>
                  <input type="text" id="first_name" name="job_application[first_name]" aria-required="true" />
                </label>
              </div>
              <div class="field">
                <label>Cover Letter
                  <textarea id="cover_letter_text" name="job_application[cover_letter_text]"></textarea>
                </label>
              </div>
              <div class="field">
                <label>Do you have the right to work in the country you are applying to?
                  <input type="hidden" name="job_application[answers_attributes][0][question_id]" value="8110111006" />
                  <select id="job_application_answers_attributes_0_boolean_value" name="job_application[answers_attributes][0][boolean_value]" aria-required="true">
                    <option value="">--</option>
                    <option value="1">Yes</option>
                    <option value="0">No</option>
                  </select>
                </label>
              </div>
              <div class="field" id="race_dropdown_container">
                <label for="job_application_race">Please identify your race</label>
                <select id="job_application_race" name="job_application[race]">
                  <option value="">Please select</option>
                  <option value="4">Hispanic or Latino</option>
                  <option value="5">White</option>
                </select>
              </div>
            </form>
          </body>
        </html>
        """

        job_post = autofill._extract_job_post(html)
        questions = {question["label"]: question for question in job_post["questions"]}

        self.assertEqual(questions["First Name"]["fields"][0]["name"], "first_name")
        self.assertTrue(questions["First Name"]["required"])
        self.assertEqual(questions["Cover Letter"]["fields"][0]["name"], "cover_letter_text")
        self.assertEqual(
            questions["Do you have the right to work in the country you are applying to?"]["fields"][0]["name"],
            "job_application_answers_attributes_0_boolean_value",
        )
        self.assertEqual(
            questions["Do you have the right to work in the country you are applying to?"]["fields"][0]["values"],
            [{"label": "Yes", "value": "1"}, {"label": "No", "value": "0"}],
        )
        self.assertEqual(questions["Please identify your race"]["fields"][0]["name"], "race")

    def test_extract_job_post_from_classic_form_prefers_attach_or_paste_cover_letter_upload(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        html = """
        <html>
          <body>
            <form id="application_form">
              <div class="field">
                <fieldset id="cover_letter_fieldset">
                  <legend><label id="cover_letter">Cover Letter</label></legend>
                  <div class="attach-or-paste" data-field="cover_letter">
                    <button type="button" data-source="attach">Attach</button>
                    <button type="button" data-source="paste">or enter manually</button>
                    <textarea id="cover_letter_text" name="job_application[cover_letter_text]"></textarea>
                  </div>
                </fieldset>
              </div>
            </form>
          </body>
        </html>
        """

        job_post = autofill._extract_job_post(html)
        question = next(question for question in job_post["questions"] if question["label"] == "Cover Letter")

        self.assertEqual(question["fields"][0]["name"], "cover_letter")
        self.assertEqual(question["fields"][0]["type"], "input_file")

    def test_ensure_checkbox_checked_falls_back_to_click_when_force_check_does_not_stick(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        class FakeCheckbox:
            def __init__(self):
                self.checked = False
                self.check_calls = 0
                self.click_calls = 0
                self.scroll_calls = 0

            def is_checked(self):
                return self.checked

            def scroll_into_view_if_needed(self):
                self.scroll_calls += 1

            def check(self, *, force=False):
                self.check_calls += 1

            def click(self, *, force=False):
                self.click_calls += 1
                self.checked = True

            def get_attribute(self, _name):
                return None

        class EmptyLocator:
            def count(self):
                return 0

            @property
            def first(self):
                return self

            def click(self, *, force=False):
                raise AssertionError("Label fallback should not be needed for this regression.")

        class FakePage:
            def __init__(self):
                self.wait_calls = []

            def wait_for_timeout(self, ms):
                self.wait_calls.append(ms)

            def locator(self, _selector):
                return EmptyLocator()

        checkbox = FakeCheckbox()
        page = FakePage()

        self.assertTrue(autofill._ensure_checkbox_checked(page, checkbox))
        self.assertTrue(checkbox.checked)
        self.assertEqual(checkbox.check_calls, 1)
        self.assertEqual(checkbox.click_calls, 1)
        self.assertGreaterEqual(checkbox.scroll_calls, 1)
        self.assertTrue(page.wait_calls)

    def test_review_validation_blockers_ignore_checked_consent_checkbox(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        label = (
            "By checking this box, I consent to Invoca collecting, storing, and processing my responses "
            "to the demographic data surveys above."
        )
        snapshot = {
            "invalid_fields": [label],
            "errors": ["This field is required."],
            "checked_fields": [label],
        }

        self.assertEqual(autofill._review_validation_blockers_from_snapshot(snapshot), [])

    def test_review_validation_blockers_ignore_invalid_checkbox_options_when_group_has_checked_values(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        group_label = "What tangible factors are most important to you when considering a job opportunity?"
        snapshot = {
            "invalid_fields": ["Work-life Balance", "Remote Work"],
            "invalid_field_groups": [
                {"field": "Work-life Balance", "group": group_label},
                {"field": "Remote Work", "group": group_label},
            ],
            "checked_fields": ["Career Growth", "Culture", "Company Outlook"],
            "checked_checkbox_groups": [group_label],
            "errors": ["This field is required."],
        }

        self.assertEqual(autofill._review_validation_blockers_from_snapshot(snapshot), [])

    def test_review_validation_blockers_ignore_group_when_checked_peer_only_appears_in_checked_fields(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        group_label = "Which FINRA licenses do you currently hold?"
        snapshot = {
            "invalid_fields": ["Series 3", "Series 4", "N/A"],
            "invalid_field_groups": [
                {"field": "Series 3", "group": group_label},
                {"field": "Series 4", "group": group_label},
                {"field": "N/A", "group": group_label},
            ],
            "checked_fields": ["N/A"],
            "errors": ["This field is required."],
        }

        self.assertEqual(autofill._review_validation_blockers_from_snapshot(snapshot), [])

    def test_review_validation_blockers_collapse_unsatisfied_group_to_single_group_label(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        group_label = "Which location are you applying for?"
        snapshot = {
            "invalid_fields": ["Austin, TX", "Columbus, OH", "Remote"],
            "invalid_field_groups": [
                {"field": "Austin, TX", "group": group_label},
                {"field": "Columbus, OH", "group": group_label},
                {"field": "Remote", "group": group_label},
            ],
            "errors": ["This field is required."],
        }

        self.assertEqual(
            autofill._review_validation_blockers_from_snapshot(snapshot),
            [{"label": group_label, "message": "This field is required."}],
        )

    def test_greenhouse_application_url_uses_embed_endpoint_for_wrapper_urls(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        resolved = autofill._greenhouse_application_url(
            "https://coreweave.com/careers/job?4638816006&board=coreweave&gh_jid=4638816006"
        )

        self.assertEqual(
            resolved,
            "https://boards.greenhouse.io/embed/job_app?for=coreweave&token=4638816006",
        )

    def test_greenhouse_application_url_extracts_slug_from_boards_path(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        resolved = autofill._greenhouse_application_url(
            "https://boards.greenhouse.io/cockroachlabs/jobs/7487285?source=LinkedIn"
        )

        self.assertEqual(
            resolved,
            "https://boards.greenhouse.io/embed/job_app?for=cockroachlabs&token=7487285",
        )

    def test_greenhouse_application_url_converts_direct_job_boards_urls_to_embed(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        direct_url = (
            "https://job-boards.greenhouse.io/figma/jobs/5574247004"
            "?gh_jid=5574247004&gh_src=28109e334us&source=LinkedIn"
        )

        resolved = autofill._greenhouse_application_url(direct_url)

        self.assertEqual(
            resolved,
            "https://boards.greenhouse.io/embed/job_app?for=figma&token=5574247004",
        )

    def test_greenhouse_application_url_converts_canonical_direct_url_to_embed_for_hosted_pages(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        hosted_url = "https://navan.com/careers/openings/7660273?gh_jid=7660273&gh_src=g19hdlp11us"
        canonical_url = "https://job-boards.greenhouse.io/tripactions/jobs/7660273"

        with mock.patch.object(
            autofill,
            "canonical_greenhouse_job_url",
            create=True,
            return_value=canonical_url,
        ) as canonical_mock:
            resolved = autofill._greenhouse_application_url(hosted_url, company_hint="navan")

        canonical_mock.assert_called_once_with(hosted_url)
        self.assertEqual(resolved, "https://boards.greenhouse.io/embed/job_app?for=tripactions&token=7660273")

    def test_greenhouse_embedded_application_url_extracts_job_app_iframe_src(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        page_url = "https://www.pinterestcareers.com/jobs/7368194/staff-product-manager-aiml-personalization/"
        html = """
        <div id="grnhse_app">
          <iframe
            id="grnhse_iframe"
            src="https://job-boards.greenhouse.io/embed/job_app?for=pinterest&validityToken=test-token&token=7368194"
          ></iframe>
        </div>
        """

        resolved = autofill._greenhouse_embedded_application_url(page_url, html)

        self.assertEqual(
            resolved,
            "https://job-boards.greenhouse.io/embed/job_app?for=pinterest&validityToken=test-token&token=7368194",
        )

    def test_greenhouse_browser_job_closed_reason_detects_unavailable_pages(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        reason = autofill._greenhouse_browser_job_closed_reason(
            "https://job-boards.greenhouse.io/samsungresearchamerica?error=true",
            "The job you are looking for is no longer open. Current openings at Samsung Research America",
        )

        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("job_closed:", reason)

    def test_greenhouse_browser_job_closed_reason_ignores_live_form_pages(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        reason = autofill._greenhouse_browser_job_closed_reason(
            "https://app.greenhouse.io/embed/job_app?token=123",
            "Apply for this job First Name Last Name Email",
        )

        self.assertIsNone(reason)

    def test_fetch_greenhouse_html_reuses_latest_fallback_cache_when_network_fails(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            current_cache = tmp_path / "submit-20260313T172038Z" / "greenhouse_application_page.html"
            current_cache.parent.mkdir(parents=True, exist_ok=True)
            prior_cache = tmp_path / "submit" / "greenhouse_application_page.html"
            prior_cache.parent.mkdir(parents=True, exist_ok=True)
            prior_cache.write_text("<html>cached greenhouse form</html>", encoding="utf-8")

            with mock.patch.object(
                autofill,
                "urlopen",
                side_effect=autofill.URLError(OSError(8, "nodename nor servname provided, or not known")),
            ):
                html = autofill._fetch_greenhouse_html(
                    "https://boards.greenhouse.io/embed/job_app?for=coreweave&token=4638816006",
                    cache_path=current_cache,
                    fallback_cache_paths=[prior_cache],
                )

            self.assertEqual(html, "<html>cached greenhouse form</html>")
            self.assertEqual(current_cache.read_text(encoding="utf-8"), "<html>cached greenhouse form</html>")

    def test_fetch_greenhouse_html_uses_fallback_cache_on_http_404(self):
        """HTTPError (e.g. 404) should fall back to cached HTML, not crash."""
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            current_cache = tmp_path / "submit-20260313T172038Z" / "greenhouse_application_page.html"
            current_cache.parent.mkdir(parents=True, exist_ok=True)
            prior_cache = tmp_path / "submit" / "greenhouse_application_page.html"
            prior_cache.parent.mkdir(parents=True, exist_ok=True)
            prior_cache.write_text("<html>cached greenhouse form</html>", encoding="utf-8")

            http_error = autofill.HTTPError(
                "https://boards.greenhouse.io/robinhood/jobs/7705327",
                404,
                "Not Found",
                {},
                None,
            )
            with mock.patch.object(
                autofill,
                "urlopen",
                side_effect=http_error,
            ):
                html = autofill._fetch_greenhouse_html(
                    "https://boards.greenhouse.io/embed/job_app?for=robinhood&token=7705327",
                    cache_path=current_cache,
                    fallback_cache_paths=[prior_cache],
                )

            self.assertEqual(html, "<html>cached greenhouse form</html>")
            self.assertEqual(current_cache.read_text(encoding="utf-8"), "<html>cached greenhouse form</html>")

    def test_fetch_greenhouse_html_raises_on_http_404_without_cache(self):
        """HTTPError (e.g. 404) with no cache should raise RuntimeError."""
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            current_cache = tmp_path / "submit" / "greenhouse_application_page.html"
            current_cache.parent.mkdir(parents=True, exist_ok=True)

            http_error = autofill.HTTPError(
                "https://boards.greenhouse.io/robinhood/jobs/7705327",
                404,
                "Not Found",
                {},
                None,
            )
            with mock.patch.object(
                autofill,
                "urlopen",
                side_effect=http_error,
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    autofill._fetch_greenhouse_html(
                        "https://boards.greenhouse.io/embed/job_app?for=robinhood&token=7705327",
                        cache_path=current_cache,
                        fallback_cache_paths=[],
                    )
            self.assertIn("404", str(ctx.exception))
            self.assertIn("job_closed", str(ctx.exception))

    def test_discovered_field_step_prefers_field_id_selector_for_comboboxes(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        step = autofill.discovered_field_step(
            {
                "field_name": "sexual_orientation",
                "label": "How would you describe your sexual orientation?",
                "type": "text",
                "role": "combobox",
            }
        )

        self.assertEqual(
            step,
            {
                "kind": "combobox",
                "field_name": "sexual_orientation",
                "label": "How would you describe your sexual orientation?",
                "selector": '[id="sexual_orientation"]',
            },
        )

    def test_greenhouse_combobox_display_text_prefers_multi_value_chips(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        value = autofill._greenhouse_combobox_display_text(
            "Select...",
            "",
            ["Man", "Another label that should not appear"],
        )

        self.assertEqual(value, "Man, Another label that should not appear")

    def test_greenhouse_combobox_display_text_falls_back_to_single_value(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        value = autofill._greenhouse_combobox_display_text(
            "Select...",
            "No, I do not have a disability and have not had one in the past",
            [],
        )

        self.assertEqual(
            value,
            "No, I do not have a disability and have not had one in the past",
        )

    def test_greenhouse_combobox_display_text_ignores_search_query_when_placeholder_remains_visible(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        value = autofill._greenhouse_combobox_display_text(
            "No, I do not have a disability and have not had one in the past",
            "",
            [],
            "Select...",
        )

        self.assertEqual(value, "")

    def test_greenhouse_combobox_display_text_ignores_open_combobox_search_query_without_selection(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        value = autofill._greenhouse_combobox_display_text(
            "Corporate website",
            "",
            [],
            "",
            menu_expanded=True,
        )

        self.assertEqual(value, "")

    def test_greenhouse_combobox_display_text_keeps_collapsed_raw_input_value(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        value = autofill._greenhouse_combobox_display_text(
            "Corporate website",
            "",
            [],
            "",
            menu_expanded=False,
        )

        self.assertEqual(value, "Corporate website")

    def test_greenhouse_combobox_candidate_listbox_ids_infers_react_select_listbox_from_input_id(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        listbox_ids = autofill._greenhouse_combobox_candidate_listbox_ids("4004786007", None, None)

        self.assertEqual(listbox_ids, ["react-select-4004786007-listbox"])

    def test_security_code_is_excluded_from_generated_answers_and_required_validation(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "Security Code",
                    "fields": [{"name": "security_code", "type": "input_text"}],
                },
                {
                    "required": True,
                    "label": "Current Company",
                    "fields": [{"name": "current_company", "type": "input_text"}],
                },
            ],
            "eeoc_sections": [],
        }

        specs = autofill._application_question_specs(job_post)
        self.assertEqual(specs, [])

        autofill._validate_required_questions(
            job_post,
            [{"field_name": "current_company", "kind": "text", "value": "CoreWeave"}],
        )

    def test_application_question_specs_ignore_required_classic_demographic_fields(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "Gender Identity",
                    "fields": [
                        {
                            "name": "job_application[demographic_answers][][answer_options][][answer_option_id]",
                            "type": "input_text",
                        }
                    ],
                },
                {
                    "required": True,
                    "label": "Current Company",
                    "fields": [{"name": "current_company", "type": "input_text"}],
                },
            ],
            "eeoc_sections": [],
        }

        specs = autofill._application_question_specs(job_post)

        self.assertEqual(specs, [])

    def test_application_question_specs_ignore_required_family_or_household_current_employee_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "Is a member of your family or household a current Hometap employee, contractor, or board member?",
                    "fields": [
                        {
                            "name": "question_11603646007",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes"}, {"label": "No"}],
                        }
                    ],
                }
            ],
            "eeoc_sections": [],
        }

        specs = autofill._application_question_specs(job_post)

        self.assertEqual(specs, [])

    def test_application_question_specs_ignore_required_sql_proficiency_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "Are you proficient in SQL?",
                    "fields": [
                        {
                            "name": "question_11199571007",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes"}, {"label": "No"}],
                        }
                    ],
                }
            ],
            "eeoc_sections": [],
        }

        specs = autofill._application_question_specs(job_post)

        self.assertEqual(specs, [])

    def test_application_question_specs_ignore_acknowledgment_only_confirmation_questions(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "Applicant Privacy Acknowledgement",
                    "fields": [
                        {
                            "name": "question_privacy_acknowledgement",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes"}],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": "Please double-check all the information provided above.",
                    "fields": [
                        {
                            "name": "question_final_review_confirmation",
                            "type": "multi_value_single_select",
                            "values": [{"label": "I have reviewed and confirmed that all information is accurate."}],
                        }
                    ],
                },
            ],
            "eeoc_sections": [],
        }

        specs = autofill._application_question_specs(job_post)

        self.assertEqual(specs, [])

    def test_validate_required_questions_ignores_required_classic_demographic_fields(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "Gender Identity",
                    "fields": [
                        {
                            "name": "job_application[demographic_answers][][answer_options][][answer_option_id]",
                            "type": "input_text",
                        }
                    ],
                },
                {
                    "required": True,
                    "label": "Current Company",
                    "fields": [{"name": "current_company", "type": "input_text"}],
                },
            ],
            "eeoc_sections": [],
        }

        autofill._validate_required_questions(
            job_post,
            [{"field_name": "current_company", "kind": "text", "value": "CoreWeave"}],
        )

    def test_application_question_specs_include_required_opt_follow_ups(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "If so, are you eligible or currently in a period of Optional Practical Training (OPT)?",
                    "fields": [
                        {
                            "name": "question_35137924002",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes"}, {"label": "No"}, {"label": "NA"}],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": (
                        "After the OPT, are you eligible for a 24-month OPT extension or are currently in a "
                        "24-month OPT extension based upon a degree from a qualifying U.S. institution in "
                        "Science, Technology, Engineering, or Mathematics after the Optional Practical Training "
                        "(OPT)?"
                    ),
                    "fields": [
                        {
                            "name": "question_35137925002",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes"}, {"label": "No"}, {"label": "NA"}],
                        }
                    ],
                },
            ],
            "eeoc_sections": [],
        }

        specs = autofill._application_question_specs(job_post)

        self.assertEqual(
            [spec["field_name"] for spec in specs],
            ["question_35137924002", "question_35137925002"],
        )

    def test_required_opt_follow_ups_survive_na_alias_and_required_validation(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "If so, are you eligible or currently in a period of Optional Practical Training (OPT)?",
                    "fields": [
                        {
                            "name": "question_35137924002",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes"}, {"label": "No"}, {"label": "NA"}],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": (
                        "After the OPT, are you eligible for a 24-month OPT extension or are currently in a "
                        "24-month OPT extension based upon a degree from a qualifying U.S. institution in "
                        "Science, Technology, Engineering, or Mathematics after the Optional Practical Training "
                        "(OPT)?"
                    ),
                    "fields": [
                        {
                            "name": "question_35137925002",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes"}, {"label": "No"}, {"label": "NA"}],
                        }
                    ],
                },
            ],
            "eeoc_sections": [],
        }
        generated_answers = {
            "question_35137924002": "N/A",
            "question_35137925002": "N/A",
        }

        steps = []
        for question in job_post["questions"]:
            step = autofill._question_step(
                question=question,
                profile=profile,
                application_profile=application_profile,
                company_name="Duolingo",
                cover_letter="Test cover letter.",
                cover_letter_file=None,
                generated_answers=generated_answers,
            )
            self.assertIsNotNone(step)
            steps.append(step)

        autofill._validate_required_questions(job_post, steps)

    def test_build_application_answers_prompt_includes_em_dash_style_guidance(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        prompt = autofill._build_application_answers_prompt(
            provider="claude",
            meta={"company": "figma"},
            question_specs=[
                {
                    "field_name": "why_figma",
                    "label": "Why Figma?",
                    "description": "",
                    "required": True,
                    "type": "input_text",
                }
            ],
            jd_parsed={"company": "Figma"},
            resume_content=None,
            research_cache=None,
            cover_letter_text="I am excited about Figma's collaborative product tools.",
            master_resume_text="## Example Corp — Senior Product Manager\n",
            work_stories_text="Built collaboration workflows across platform surfaces.",
            candidate_context_text="Interested in product design infrastructure roles.",
            application_profile_text="- Work Authorization Statement: Authorized to work in the United States.",
        )

        self.assertIn(autofill.APPLICATION_ANSWER_EM_DASH_GUIDANCE, prompt)

    def test_build_application_answers_prompt_uses_json_null_for_blank_optional_fields(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        prompt = autofill._build_application_answers_prompt(
            provider="openai",
            meta={"company": "figma"},
            question_specs=[
                {
                    "field_name": "if_other",
                    "label": "If other, please specify",
                    "description": "",
                    "required": False,
                    "type": "input_text",
                }
            ],
            jd_parsed={"company": "Figma"},
            resume_content=None,
            research_cache=None,
            cover_letter_text="I am excited about Figma's collaborative product tools.",
            master_resume_text="## Example Corp — Senior Product Manager\n",
            work_stories_text="Built collaboration workflows across platform surfaces.",
            candidate_context_text="Interested in product design infrastructure roles.",
            application_profile_text="- Work Authorization Statement: Authorized to work in the United States.",
        )

        self.assertIn("json null", prompt.casefold())
        self.assertNotIn("return an empty string unless the condition clearly applies", prompt.casefold())

    def test_generate_application_answers_openai_uses_nullable_schema_for_optional_fields(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "Why Figma?",
                    "fields": [{"name": "why_figma", "type": "input_text"}],
                },
                {
                    "required": False,
                    "label": "Anything else you'd like us to know?",
                    "fields": [{"name": "question_extra_context", "type": "input_text"}],
                },
                {
                    "required": False,
                    "label": "Which work setup do you prefer?",
                    "fields": [
                        {
                            "name": "question_work_setup",
                            "type": "multi_value_single_select",
                            "values": [
                                {"label": "Remote", "value": 1},
                                {"label": "Hybrid", "value": 2},
                            ],
                        }
                    ],
                },
            ],
            "eeoc_sections": [],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "content").mkdir()
            (out_dir / "submit").mkdir()
            (out_dir / "content" / "cover_letter_text.txt").write_text(
                "Figma's collaborative product tools are compelling.",
                encoding="utf-8",
            )

            with mock.patch.object(
                autofill, "provider_command_for_mode", return_value=["openai_provider.py", "prompt"]
            ) as builder:
                with mock.patch.object(
                    autofill.subprocess,
                    "run",
                    return_value=mock.Mock(
                        returncode=0,
                        stdout=(
                            '{"why_figma":"Because collaborative workflows compound design leverage.",'
                            '"question_extra_context":null,"question_work_setup":null}'
                        ),
                        stderr="",
                    ),
                ):
                    answers = autofill._generate_application_answers(
                        out_dir=out_dir,
                        meta={"company": "figma"},
                        job_post=job_post,
                        provider="openai",
                    )

        self.assertEqual(
            answers,
            {"why_figma": "Because collaborative workflows compound design leverage."},
        )
        schema = builder.call_args.kwargs["json_schema"]
        self.assertEqual(
            schema["required"],
            ["why_figma", "question_extra_context", "question_work_setup"],
        )
        self.assertTrue(schema_allows_type(schema["properties"]["question_extra_context"], "null"))
        self.assertTrue(schema_allows_type(schema["properties"]["question_work_setup"], "null"))

    def test_generate_application_answers_falls_back_via_openai_gemini_chain(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "Why Starburst?",
                    "fields": [{"name": "why_starburst", "type": "input_text"}],
                }
            ],
            "eeoc_sections": [],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "content").mkdir()
            (out_dir / "submit").mkdir()
            (out_dir / "content" / "cover_letter_text.txt").write_text(
                "Starburst's insights platform is compelling.",
                encoding="utf-8",
            )

            commands = [["python", "openai_provider.py", "prompt"], ["gemini", "-p", "prompt"]]
            runs = [
                mock.Mock(returncode=1, stdout="", stderr="Not logged in"),
                mock.Mock(
                    returncode=0,
                    stdout='{"why_starburst":"Because administrator insights compound platform value."}',
                    stderr="",
                ),
            ]

            with mock.patch.dict("os.environ", {"ASSET_LLM_PROVIDER_CHAIN": "openai,gemini,claude"}):
                with mock.patch.object(autofill, "provider_command_for_mode", side_effect=commands):
                    with mock.patch.object(
                        autofill.shutil,
                        "which",
                        side_effect=lambda name: "/usr/bin/gemini" if name == "gemini" else sys.executable,
                    ):
                        with mock.patch.object(autofill.subprocess, "run", side_effect=runs):
                            answers = autofill._generate_application_answers(
                                out_dir=out_dir,
                                meta={"company": "starburst"},
                                job_post=job_post,
                                provider="openai",
                            )

            self.assertEqual(
                answers,
                {"why_starburst": "Because administrator insights compound platform value."},
            )
            payload = json.loads((out_dir / "submit" / autofill.APPLICATION_ANSWER_CACHE).read_text(encoding="utf-8"))
            self.assertEqual(payload["provider"], "gemini")
            self.assertIn(
                "Not logged in", (out_dir / "submit" / autofill.APPLICATION_ANSWER_RAW).read_text(encoding="utf-8")
            )
            self.assertIn(
                "administrator insights compound platform value",
                (out_dir / "submit" / autofill.APPLICATION_ANSWER_FALLBACK_RAW).read_text(encoding="utf-8"),
            )

    def test_generate_application_answers_times_out_then_falls_back_via_openai_gemini_chain(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "Why Starburst?",
                    "fields": [{"name": "why_starburst", "type": "input_text"}],
                }
            ],
            "eeoc_sections": [],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "content").mkdir()
            (out_dir / "submit").mkdir()
            (out_dir / "content" / "cover_letter_text.txt").write_text(
                "Starburst's insights platform is compelling.",
                encoding="utf-8",
            )

            commands = [["python", "openai_provider.py", "prompt"], ["gemini", "-p", "prompt"]]
            runs = [
                subprocess.TimeoutExpired(
                    cmd=["python", "openai_provider.py", "prompt"],
                    timeout=7,
                    output="partial output",
                    stderr="still thinking",
                ),
                mock.Mock(
                    returncode=0,
                    stdout='{"why_starburst":"Because administrator insights compound platform value."}',
                    stderr="",
                ),
            ]

            with mock.patch.dict("os.environ", {"ASSET_LLM_PROVIDER_CHAIN": "openai,gemini,claude"}):
                with mock.patch.object(autofill, "provider_command_for_mode", side_effect=commands):
                    with mock.patch.object(autofill, "provider_timeout_seconds", return_value=7):
                        with mock.patch.object(
                            autofill.shutil,
                            "which",
                            side_effect=lambda name: "/usr/bin/gemini" if name == "gemini" else sys.executable,
                        ):
                            with mock.patch.object(autofill.subprocess, "run", side_effect=runs):
                                answers = autofill._generate_application_answers(
                                    out_dir=out_dir,
                                    meta={"company": "starburst"},
                                    job_post=job_post,
                                    provider="openai",
                                )

            self.assertEqual(
                answers,
                {"why_starburst": "Because administrator insights compound platform value."},
            )
            self.assertIn(
                "timed out after 7s",
                (out_dir / "submit" / autofill.APPLICATION_ANSWER_RAW).read_text(encoding="utf-8"),
            )
            self.assertIn(
                "administrator insights compound platform value",
                (out_dir / "submit" / autofill.APPLICATION_ANSWER_FALLBACK_RAW).read_text(encoding="utf-8"),
            )

    def test_generate_application_answers_retries_once_after_json_parse_failure(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "Why Starburst?",
                    "fields": [{"name": "why_starburst", "type": "input_text"}],
                }
            ],
            "eeoc_sections": [],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "content").mkdir()
            (out_dir / "submit").mkdir()
            (out_dir / "content" / "cover_letter_text.txt").write_text(
                "Starburst's insights platform is compelling.",
                encoding="utf-8",
            )

            runs = [
                mock.Mock(returncode=0, stdout="not json", stderr=""),
                mock.Mock(
                    returncode=0,
                    stdout='{"why_starburst":"Because administrator insights compound platform value."}',
                    stderr="",
                ),
            ]

            with mock.patch.object(autofill, "provider_command_for_mode", return_value=["claude", "--print", "prompt"]):
                with mock.patch.object(autofill.subprocess, "run", side_effect=runs) as run:
                    answers = autofill._generate_application_answers(
                        out_dir=out_dir,
                        meta={"company": "starburst"},
                        job_post=job_post,
                        provider="claude",
                    )

            raw_text = (out_dir / "submit" / autofill.APPLICATION_ANSWER_RAW).read_text(encoding="utf-8")

        self.assertEqual(
            answers,
            {"why_starburst": "Because administrator insights compound platform value."},
        )
        self.assertEqual(run.call_count, 2)
        self.assertIn("Invalid JSON from claude", raw_text)

    def test_build_steps_verifies_generated_answers_before_rendering(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {"questions": [], "eeoc_sections": []}

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / "content").mkdir()
            profile = mock.Mock(
                first_name="Jerrison",
                last_name="Li",
                email="jerrisonli@gmail.com",
                phone="510-613-5192",
                employers=set(),
            )
            application_profile = autofill._parse_application_profile(
                """
                - Country: United States
                - Location: San Francisco, CA
                - Work Authorization Statement: I am always authorized to work in the United States unconditionally.
                - Authorized to Work Unconditionally: Yes
                - Require Sponsorship Now: No
                - Require Sponsorship in Future: No
                - Sponsorship Answer: No
                - Gender: Male
                - Race or Ethnicity: Hispanic or Latino
                - Veteran Status: I am not a protected veteran
                - Disability Status: No, I do not have a disability and have not had one in the past
                - Sexual Orientation: Straight / Heterosexual
                """
            )

            with (
                mock.patch.object(autofill, "_default_answer_provider", return_value="openai"),
                mock.patch.object(
                    autofill,
                    "_generate_application_answers",
                    return_value={"why_coreweave": "Because AI infrastructure needs strong product judgment."},
                ),
                mock.patch.object(autofill, "_verify_generated_answers_for_current_draft", create=True) as verify,
                mock.patch.object(autofill, "_find_cover_letter_text", return_value=""),
                mock.patch.object(autofill, "_find_resume_file", return_value=None),
                mock.patch.object(autofill, "_find_cover_letter_file", side_effect=FileNotFoundError()),
                mock.patch.object(autofill, "_preferred_education_entry", return_value=None),
                mock.patch.object(autofill, "_all_questions", return_value=[]),
            ):
                autofill._build_steps(
                    job_post,
                    {"company": "coreweave", "company_proper": "CoreWeave"},
                    profile,
                    application_profile,
                    out_dir,
                )

        verify.assert_called_once_with(
            out_dir=out_dir,
            meta={"company": "coreweave", "company_proper": "CoreWeave"},
            job_post=job_post,
            generated_answers={"why_coreweave": "Because AI infrastructure needs strong product judgment."},
            application_profile=application_profile,
        )

    def test_generate_application_answers_reuses_matching_cache_from_previous_submit_attempt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "Why CoreWeave?",
                    "fields": [{"name": "why_coreweave", "type": "input_text"}],
                }
            ],
            "eeoc_sections": [],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "content").mkdir()
            (out_dir / "content" / "cover_letter_text.txt").write_text(
                "CoreWeave's AI cloud platform is compelling.",
                encoding="utf-8",
            )
            (out_dir / "submit").mkdir()
            (out_dir / "submit" / autofill.APPLICATION_ANSWER_CACHE).write_text(
                json.dumps(
                    {
                        "questions": autofill._application_question_specs(job_post),
                        "answers": {"why_coreweave": "Because AI infra growth needs strong product judgment."},
                    }
                ),
                encoding="utf-8",
            )
            (out_dir / ".active_submit_dir").write_text("submit-20260313T172234Z\n", encoding="utf-8")
            (out_dir / "submit-20260313T172234Z").mkdir()

            with mock.patch.object(autofill, "provider_command_for_mode") as provider_command:
                with mock.patch.object(autofill.subprocess, "run") as run:
                    answers = autofill._generate_application_answers(
                        out_dir=out_dir,
                        meta={"company": "coreweave"},
                        job_post=job_post,
                        provider="claude",
                    )

            self.assertEqual(
                answers,
                {"why_coreweave": "Because AI infra growth needs strong product judgment."},
            )
            provider_command.assert_not_called()
            run.assert_not_called()
            self.assertTrue((out_dir / "submit-20260313T172234Z" / autofill.APPLICATION_ANSWER_CACHE).exists())

    def test_generate_application_answers_bypasses_matching_cache_when_refresh_pending(self):
        refresh = load_module("answer_refresh_state", "scripts/answer_refresh_state.py")
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "Why CoreWeave?",
                    "fields": [{"name": "why_coreweave", "type": "input_text"}],
                }
            ],
            "eeoc_sections": [],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "content").mkdir()
            (out_dir / "content" / "cover_letter_text.txt").write_text(
                "CoreWeave's AI cloud platform is compelling.",
                encoding="utf-8",
            )
            (out_dir / "submit").mkdir()
            (out_dir / "submit" / autofill.APPLICATION_ANSWER_CACHE).write_text(
                json.dumps(
                    {
                        "questions": autofill._application_question_specs(job_post),
                        "answers": {"why_coreweave": "Because the stale cache was reused."},
                    }
                ),
                encoding="utf-8",
            )
            pending = refresh.mark_answer_refresh_pending(out_dir, request_kind="reanswer")

            with mock.patch.object(
                autofill, "provider_command_for_mode", return_value=["claude", "--print", "prompt"]
            ) as provider_command:
                with mock.patch.object(
                    autofill.subprocess,
                    "run",
                    return_value=mock.Mock(
                        returncode=0,
                        stdout='{"why_coreweave":"Because fresh proof matters."}',
                        stderr="",
                    ),
                ) as run:
                    answers = autofill._generate_application_answers(
                        out_dir=out_dir,
                        meta={"company": "coreweave"},
                        job_post=job_post,
                        provider="claude",
                    )

            self.assertEqual(answers, {"why_coreweave": "Because fresh proof matters."})
            provider_command.assert_called_once()
            run.assert_called_once()
            payload = json.loads((out_dir / "submit" / autofill.APPLICATION_ANSWER_CACHE).read_text(encoding="utf-8"))
            self.assertEqual(payload["refresh_request_id"], pending["request_id"])
            raw_text = (out_dir / "submit" / autofill.APPLICATION_ANSWER_RAW).read_text(encoding="utf-8")
            self.assertIn(f"request_id={pending['request_id']}", raw_text)

    def test_generate_application_answers_ignores_stale_active_and_previous_submit_caches(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "If so, are you eligible or currently in a period of Optional Practical Training (OPT)?",
                    "fields": [
                        {
                            "name": "question_35137924002",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes"}, {"label": "No"}, {"label": "NA"}],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": (
                        "After the OPT, are you eligible for a 24-month OPT extension or are currently in a "
                        "24-month OPT extension based upon a degree from a qualifying U.S. institution in "
                        "Science, Technology, Engineering, or Mathematics after the Optional Practical Training "
                        "(OPT)?"
                    ),
                    "fields": [
                        {
                            "name": "question_35137925002",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes"}, {"label": "No"}, {"label": "NA"}],
                        }
                    ],
                },
            ],
            "eeoc_sections": [],
        }
        stale_payload = {
            "questions": [
                {
                    "field_name": "question_35137924002",
                    "label": "If so, are you eligible or currently in a period of Optional Practical Training (OPT)?",
                    "description": "",
                    "required": True,
                    "type": "multi_value_single_select",
                    "options": ["Yes", "No", "NA"],
                }
            ],
            "answers": {"question_35137924002": "N/A"},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            active_submit_dir = out_dir / "submit-20260313T172234Z"
            older_submit_dir = out_dir / "submit-20260313T160000Z"
            (out_dir / "content").mkdir()
            (out_dir / "content" / "cover_letter_text.txt").write_text(
                "Duolingo's product learning loops are compelling.",
                encoding="utf-8",
            )
            (out_dir / "submit").mkdir()
            active_submit_dir.mkdir()
            older_submit_dir.mkdir()
            (out_dir / ".active_submit_dir").write_text(f"{active_submit_dir.name}\n", encoding="utf-8")

            for submit_dir in (out_dir / "submit", active_submit_dir, older_submit_dir):
                (submit_dir / autofill.APPLICATION_ANSWER_CACHE).write_text(
                    json.dumps(stale_payload),
                    encoding="utf-8",
                )

            with mock.patch.object(
                autofill, "provider_command_for_mode", return_value=["claude", "--print", "prompt"]
            ) as provider_command:
                with mock.patch.object(
                    autofill.subprocess,
                    "run",
                    return_value=mock.Mock(
                        returncode=0,
                        stdout=('{"question_35137924002":"N/A","question_35137925002":"N/A"}'),
                        stderr="",
                    ),
                ) as run:
                    answers = autofill._generate_application_answers(
                        out_dir=out_dir,
                        meta={"company": "duolingo"},
                        job_post=job_post,
                        provider="claude",
                    )

            self.assertEqual(
                answers,
                {
                    "question_35137924002": "N/A",
                    "question_35137925002": "N/A",
                },
            )
            provider_command.assert_called_once()
            run.assert_called_once()

            active_payload = json.loads(
                (active_submit_dir / autofill.APPLICATION_ANSWER_CACHE).read_text(encoding="utf-8")
            )
            self.assertEqual(active_payload["questions"], autofill._application_question_specs(job_post))
            self.assertEqual(active_payload["answers"], answers)

    def test_generate_application_answers_skips_recent_grad_gpa_when_classifier_marks_it_deterministic(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "If you're less than 3 years out of school, what is your undergraduate GPA?",
                    "fields": [{"name": "question_35137926002", "type": "input_text"}],
                }
            ],
            "eeoc_sections": [],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "content").mkdir()
            (out_dir / "content" / "cover_letter_text.txt").write_text(
                "Duolingo's learning loops are compelling.",
                encoding="utf-8",
            )

            with mock.patch.object(
                autofill,
                "provider_command_for_mode",
                return_value=["codex", "exec", "--skip-git-repo-check", "-"],
            ) as provider_command:
                with mock.patch.object(
                    autofill.subprocess,
                    "run",
                    return_value=mock.Mock(
                        returncode=0,
                        stdout='{"question_35137926002":""}',
                        stderr="",
                    ),
                ) as run:
                    answers = autofill._generate_application_answers(
                        out_dir=out_dir,
                        meta={"company": "duolingo"},
                        job_post=job_post,
                        provider="codex",
                    )

            self.assertEqual(answers, {})
            provider_command.assert_not_called()
            run.assert_not_called()

    def test_generate_application_answers_clears_stale_artifacts_when_only_deterministic_fields_remain(self):
        autofill = load_module("autofill_greenhouse_no_generated_answers", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "Where did you hear about this role?",
                    "fields": [
                        {
                            "name": "question_63833648",
                            "type": "multi_value_single_select",
                            "values": [{"label": "DeepMind website"}, {"label": "LinkedIn"}],
                        }
                    ],
                }
            ],
            "eeoc_sections": [],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "content").mkdir()
            (out_dir / "content" / "cover_letter_text.txt").write_text(
                "DeepMind's agent work is compelling.",
                encoding="utf-8",
            )
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            stale_answers = submit_dir / autofill.APPLICATION_ANSWER_CACHE
            stale_raw = submit_dir / autofill.APPLICATION_ANSWER_RAW
            stale_fallback = submit_dir / autofill.APPLICATION_ANSWER_FALLBACK_RAW
            stale_answers.write_text(
                json.dumps(
                    {
                        "questions": [
                            {
                                "field_name": "question_63833648",
                                "label": "Where did you hear about this role?",
                                "type": "multi_value_single_select",
                                "options": ["DeepMind website", "LinkedIn"],
                            }
                        ],
                        "answers": {"question_63833648": "LinkedIn"},
                    }
                ),
                encoding="utf-8",
            )
            stale_raw.write_text("stale raw", encoding="utf-8")
            stale_fallback.write_text("stale fallback", encoding="utf-8")

            with mock.patch.object(
                autofill,
                "clear_preference_research_artifacts",
                wraps=autofill.clear_preference_research_artifacts,
            ) as clear_pref:
                answers = autofill._generate_application_answers(
                    out_dir=out_dir,
                    meta={"company": "deepmind", "board": "greenhouse"},
                    job_post=job_post,
                    provider="openai",
                )

            self.assertEqual(answers, {})
            self.assertFalse(stale_answers.exists())
            self.assertFalse(stale_raw.exists())
            self.assertFalse(stale_fallback.exists())
            clear_pref.assert_called_once_with(out_dir)

    def test_generate_application_answers_merges_shared_ai_workflow_answer_before_provider_call(self):
        autofill = load_module("autofill_greenhouse_ai_workflow_merge", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": (
                        "Describe a specific example of how you've used Gen AI tools in your product work, "
                        "including the tools used, the problem you were solving, and the impact."
                    ),
                    "fields": [{"name": "question_ai_workflow", "type": "textarea"}],
                },
                {
                    "required": True,
                    "label": "Why Hungryroot?",
                    "fields": [{"name": "why_hungryroot", "type": "input_text"}],
                },
            ],
            "eeoc_sections": [],
        }

        expected_answer = (
            "At Moody's, I used Claude Code with Figma to prototype a workflow solution for a "
            "$15M at-risk enterprise account. I built the prototype end to end with AI-assisted "
            "tooling, then hosted it in AWS so customers could interact with it directly and give "
            "feedback. The problem was that we needed fast evidence to resolve a product and "
            "engineering disagreement and keep the customer from churning. That prototype let us "
            "validate the direction quickly, align stakeholders on a scoped path forward, and help "
            "retain the account."
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "content").mkdir()
            (out_dir / "submit").mkdir()
            (out_dir / "content" / "cover_letter_text.txt").write_text(
                "Hungryroot's AI personalization is compelling.",
                encoding="utf-8",
            )
            application_profile_path = out_dir / "application_profile.md"
            application_profile_path.write_text(
                (
                    "- Country: United States\n"
                    "- Location: San Francisco, CA\n"
                    "- Work Authorization Statement: I am always authorized to work in the United States unconditionally.\n"
                    "- Authorized to Work Unconditionally: Yes\n"
                    "- Require Sponsorship Now: No\n"
                    "- Require Sponsorship in Future: No\n"
                    "- Sponsorship Answer: No\n"
                    "- Gender: Male\n"
                    "- Race or Ethnicity: Hispanic or Latino\n"
                    "- Veteran Status: I am not a protected veteran\n"
                    "- Disability Status: No, I do not have a disability and have not had one in the past\n"
                    "- Sexual Orientation: Straight / Heterosexual\n"
                ),
                encoding="utf-8",
            )
            master_resume_path = out_dir / "master_resume.md"
            master_resume_path.write_text(
                (
                    "MOODY'S ANALYTICS — Director Product Management\n"
                    "* Rescued $15M at-risk enterprise account by building functional prototype in 3 days, "
                    "deploying for direct customer validation, and running weekly design sessions to lock requirements.\n"
                ),
                encoding="utf-8",
            )
            work_stories_path = out_dir / "work_stories.md"
            work_stories_path.write_text(
                (
                    "At Moody's, I built a working prototype in Figma and Claude Code, ran it past customers "
                    "for two weeks, and collected structured feedback comparing the old and new experiences.\n"
                ),
                encoding="utf-8",
            )
            candidate_context_path = out_dir / "candidate_context.md"
            candidate_context_path.write_text(
                "I use claude code as well as codex to prototype UI designs to share with design partners.\n",
                encoding="utf-8",
            )

            with (
                mock.patch.object(
                    autofill,
                    "prepare_preference_research_context",
                    return_value={
                        "cache_key": None,
                        "provider": "openai",
                        "questions": [],
                        "answers": {},
                        "failures": [],
                        "artifacts": {},
                    },
                ),
                mock.patch.object(
                    autofill,
                    "prepare_linked_resource_context",
                    return_value={
                        "cache_key": None,
                        "prompt_context": None,
                        "resources": [],
                        "failures": [],
                        "artifacts": {},
                    },
                ),
                mock.patch.object(autofill, "APPLICATION_PROFILE_PATH", application_profile_path),
                mock.patch.object(autofill, "MASTER_RESUME_PATH", master_resume_path),
                mock.patch.object(autofill, "WORK_STORIES_PATH", work_stories_path),
                mock.patch.object(autofill, "CANDIDATE_CONTEXT_PATH", candidate_context_path),
                mock.patch.object(
                    autofill,
                    "_run_answer_generation_provider",
                    return_value=({"why_hungryroot": "Because AI-enabled recipe infrastructure compounds value."}, None),
                ) as runner,
            ):
                answers = autofill._generate_application_answers(
                    out_dir=out_dir,
                    meta={"company": "hungryroot", "board": "greenhouse"},
                    job_post=job_post,
                    provider="openai",
                )

            self.assertEqual(answers["question_ai_workflow"], expected_answer)
            self.assertEqual(
                answers["why_hungryroot"],
                "Because AI-enabled recipe infrastructure compounds value.",
            )
            provider_specs = runner.call_args.kwargs["question_specs"]
            self.assertEqual([spec["field_name"] for spec in provider_specs], ["why_hungryroot"])

    def test_generate_application_answers_skips_optional_unsupported_profile_and_timing_prompts(self):
        autofill = load_module("autofill_greenhouse_optional_skip", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": False,
                    "label": "(Optional) Personal Preferences",
                    "description": "How do you pronounce your name?",
                    "fields": [{"name": "name_pronunciation", "type": "input_text"}],
                },
                {
                    "required": False,
                    "label": "When is the earliest you would want to start working with us?",
                    "fields": [{"name": "earliest_start", "type": "input_text"}],
                },
                {
                    "required": False,
                    "label": "Do you have any deadlines or timeline considerations we should be aware of?",
                    "fields": [{"name": "timeline_constraints", "type": "input_text"}],
                },
                {
                    "required": False,
                    "label": "X Profile URL",
                    "fields": [{"name": "x_profile", "type": "input_text"}],
                },
                {
                    "required": False,
                    "label": "Date",
                    "fields": [{"name": "signature_date", "type": "input_text"}],
                },
                {
                    "required": True,
                    "label": "Why Anthropic?",
                    "fields": [{"name": "why_company", "type": "textarea"}],
                },
            ],
            "eeoc_sections": [],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "content").mkdir()
            (out_dir / "submit").mkdir()
            (out_dir / "content" / "cover_letter_text.txt").write_text("Anthropic fit.", encoding="utf-8")

            with (
                mock.patch.object(
                    autofill,
                    "prepare_preference_research_context",
                    return_value={
                        "cache_key": None,
                        "provider": "openai",
                        "questions": [],
                        "answers": {},
                        "failures": [],
                        "artifacts": {},
                    },
                ),
                mock.patch.object(
                    autofill,
                    "prepare_linked_resource_context",
                    return_value={
                        "cache_key": None,
                        "prompt_context": None,
                        "resources": [],
                        "failures": [],
                        "artifacts": {},
                    },
                ),
                mock.patch.object(
                    autofill,
                    "_run_answer_generation_provider",
                    return_value=({"why_company": "The mission and developer tooling are compelling."}, None),
                ) as runner,
            ):
                answers = autofill._generate_application_answers(
                    out_dir=out_dir,
                    meta={"company": "anthropic", "board": "greenhouse"},
                    job_post=job_post,
                    provider="openai",
                )

            self.assertEqual(
                answers["why_company"],
                "The mission and developer tooling are compelling.",
            )
            provider_specs = runner.call_args.kwargs["question_specs"]
            self.assertEqual([spec["field_name"] for spec in provider_specs], ["why_company"])

    def test_generate_application_answers_rewrites_stale_cached_shared_ai_workflow_answer(self):
        autofill = load_module("autofill_greenhouse_ai_workflow_cache", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": (
                        "Describe a specific example of how you've used Gen AI tools in your product work, "
                        "including the tools used, the problem you were solving, and the impact."
                    ),
                    "fields": [{"name": "question_ai_workflow", "type": "textarea"}],
                }
            ],
            "eeoc_sections": [],
        }

        expected_answer = (
            "At Moody's, I used Claude Code with Figma to prototype a workflow solution for a "
            "$15M at-risk enterprise account. I built the prototype end to end with AI-assisted "
            "tooling, then hosted it in AWS so customers could interact with it directly and give "
            "feedback. The problem was that we needed fast evidence to resolve a product and "
            "engineering disagreement and keep the customer from churning. That prototype let us "
            "validate the direction quickly, align stakeholders on a scoped path forward, and help "
            "retain the account."
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "content").mkdir()
            (out_dir / "submit").mkdir()
            (out_dir / "content" / "cover_letter_text.txt").write_text(
                "Hungryroot's AI personalization is compelling.",
                encoding="utf-8",
            )
            application_profile_path = out_dir / "application_profile.md"
            application_profile_path.write_text(
                (
                    "- Country: United States\n"
                    "- Location: San Francisco, CA\n"
                    "- Work Authorization Statement: I am always authorized to work in the United States unconditionally.\n"
                    "- Authorized to Work Unconditionally: Yes\n"
                    "- Require Sponsorship Now: No\n"
                    "- Require Sponsorship in Future: No\n"
                    "- Sponsorship Answer: No\n"
                    "- Gender: Male\n"
                    "- Race or Ethnicity: Hispanic or Latino\n"
                    "- Veteran Status: I am not a protected veteran\n"
                    "- Disability Status: No, I do not have a disability and have not had one in the past\n"
                    "- Sexual Orientation: Straight / Heterosexual\n"
                ),
                encoding="utf-8",
            )
            master_resume_path = out_dir / "master_resume.md"
            master_resume_path.write_text(
                (
                    "MOODY'S ANALYTICS — Director Product Management\n"
                    "* Rescued $15M at-risk enterprise account by building functional prototype in 3 days, "
                    "deploying for direct customer validation, and running weekly design sessions to lock requirements.\n"
                ),
                encoding="utf-8",
            )
            work_stories_path = out_dir / "work_stories.md"
            work_stories_path.write_text(
                (
                    "At Moody's, I built a working prototype in Figma and Claude Code, ran it past customers "
                    "for two weeks, and collected structured feedback comparing the old and new experiences.\n"
                ),
                encoding="utf-8",
            )
            candidate_context_path = out_dir / "candidate_context.md"
            candidate_context_path.write_text(
                "I use claude code as well as codex to prototype UI designs to share with design partners.\n",
                encoding="utf-8",
            )
            (out_dir / "submit" / autofill.APPLICATION_ANSWER_CACHE).write_text(
                json.dumps(
                    {
                        "questions": autofill._application_question_specs(job_post),
                        "answers": {
                            "question_ai_workflow": (
                                "At Moody's Analytics, I led SlipStream, an agentic GenAI pipeline that converts "
                                "unstructured insurance policy documents into structured data for underwriting use."
                            )
                        },
                    }
                ),
                encoding="utf-8",
            )

            with (
                mock.patch.object(
                    autofill,
                    "prepare_preference_research_context",
                    return_value={
                        "cache_key": None,
                        "provider": "openai",
                        "questions": [],
                        "answers": {},
                        "failures": [],
                        "artifacts": {},
                    },
                ),
                mock.patch.object(
                    autofill,
                    "prepare_linked_resource_context",
                    return_value={
                        "cache_key": None,
                        "prompt_context": None,
                        "resources": [],
                        "failures": [],
                        "artifacts": {},
                    },
                ),
                mock.patch.object(autofill, "APPLICATION_PROFILE_PATH", application_profile_path),
                mock.patch.object(autofill, "MASTER_RESUME_PATH", master_resume_path),
                mock.patch.object(autofill, "WORK_STORIES_PATH", work_stories_path),
                mock.patch.object(autofill, "CANDIDATE_CONTEXT_PATH", candidate_context_path),
                mock.patch.object(
                    autofill,
                    "_run_answer_generation_provider",
                    side_effect=AssertionError("provider should not be called"),
                ) as runner,
            ):
                answers = autofill._generate_application_answers(
                    out_dir=out_dir,
                    meta={"company": "hungryroot", "board": "greenhouse"},
                    job_post=job_post,
                    provider="openai",
                )

            self.assertEqual(answers["question_ai_workflow"], expected_answer)
            runner.assert_not_called()
            rewritten_payload = json.loads(
                (out_dir / "submit" / autofill.APPLICATION_ANSWER_CACHE).read_text(encoding="utf-8")
            )
            self.assertEqual(rewritten_payload["answers"]["question_ai_workflow"], expected_answer)

    def test_question_step_uses_company_website_option_for_how_did_you_hear_multiselect(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "How did you hear about us?",
            "required": True,
            "fields": [
                {
                    "name": "question_1",
                    "type": "multi_value_single_select",
                    "values": [
                        {"value": 1, "label": "LinkedIn"},
                        {"value": 2, "label": "Starburst Website"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Starburst",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["option"], "Starburst Website")

    def test_question_step_uses_company_careers_option_for_how_did_you_find_this_position_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "How did you find this position?",
            "required": True,
            "fields": [
                {
                    "name": "question_1",
                    "type": "multi_value_single_select",
                    "values": [
                        {"value": 1, "label": "LinkedIn Job Post"},
                        {"value": 2, "label": "Planet Careers Page"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Planet",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["option"], "Planet Careers Page")

    def test_question_step_prefers_job_board_option_when_trueup_source_offers_other_job_board_and_company_website(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "How did you hear about us?",
            "required": True,
            "fields": [
                {
                    "name": "question_1",
                    "type": "multi_value_single_select",
                    "values": [
                        {"value": 1, "label": "LinkedIn"},
                        {"value": 2, "label": "Other Job Board"},
                        {"value": 3, "label": "Starburst Website"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Starburst",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
            job_url="https://boards.greenhouse.io/starburst/jobs/example?utm_source=trueup.io&utm_medium=website&ref=trueup",
        )

        self.assertEqual(step["option"], "Other Job Board")
        self.assertEqual(step["source"], "job_url.utm_source")

    def test_question_step_uses_company_website_option_for_where_did_you_hear_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Where did you hear about this role?",
            "required": True,
            "fields": [
                {
                    "name": "question_1",
                    "type": "multi_value_single_select",
                    "values": [
                        {"value": 1, "label": "LinkedIn"},
                        {"value": 2, "label": "Google DeepMind Website"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Google DeepMind",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
            job_url="https://boards.greenhouse.io/deepmind/jobs/example?gh_src=linkedin",
        )

        self.assertEqual(step["option"], "Google DeepMind Website")
        self.assertEqual(step["source"], "application_profile.md")

    def test_question_step_uses_company_website_option_for_how_did_you_first_hear_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "How did you first hear about this opportunity?",
            "required": True,
            "fields": [
                {
                    "name": "question_1",
                    "type": "multi_value_single_select",
                    "values": [
                        {"value": 1, "label": "LinkedIn"},
                        {"value": 2, "label": "Pinterest Careers"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Pinterest",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
            job_url="https://boards.greenhouse.io/pinterest/jobs/example?gh_src=linkedin",
        )

        self.assertEqual(step["option"], "Pinterest Careers")
        self.assertEqual(step["source"], "application_profile.md")

    def test_how_did_you_hear_option_matching_accepts_company_website_careers_page_variant(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        self.assertTrue(
            autofill._greenhouse_option_text_matches(
                "how_did_you_hear",
                "Corporate website",
                "Company Website / Careers Page",
            )
        )

    def test_resolve_discovered_demographic_option_text_prefers_job_board_for_trueup_source(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )

        resolved = autofill._resolve_discovered_demographic_option_text(
            field_name="how_did_you_hear",
            desired="Corporate website",
            option_texts=["LinkedIn", "Other Job Board", "Starburst Website"],
            application_profile=application_profile,
            company_name="Starburst",
            job_url="https://boards.greenhouse.io/starburst/jobs/example?utm_source=trueup.io&utm_medium=website&ref=trueup",
        )

        self.assertEqual(resolved, "Other Job Board")

    def test_discovered_demographic_field_name_recognizes_where_did_you_hear_heading(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        self.assertEqual(
            autofill._greenhouse_discovered_demographic_field_name("Where did you hear about this role?"),
            "how_did_you_hear",
        )

    def test_discovered_demographic_field_name_recognizes_categories_describe_you_heading(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        self.assertEqual(
            autofill._greenhouse_discovered_demographic_field_name(
                "Which categories describe you? Select all that apply to you:"
            ),
            "race",
        )

    def test_question_step_auto_confirms_single_option_privacy_acknowledgement(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Candidate Privacy Policy",
            "required": True,
            "fields": [
                {
                    "name": "question_2",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Acknowledge/Confirm"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Starburst",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["option"], "Acknowledge/Confirm")

    def test_question_step_privacy_acknowledgement_ignores_description_keyword_collisions(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Privacy Notice Acknowledgement",
            "description": (
                "Please review our privacy notice at https://example.com/privacy. "
                "This notice explains how we process candidate data across countries and websites."
            ),
            "required": True,
            "fields": [
                {
                    "name": "question_privacy_notice",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Acknowledge"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Iterable",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["option"], "Acknowledge")

    def test_question_step_auto_confirms_single_option_application_sms_acknowledgement(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": (
                "I acknowledge that by providing my phone number, I agree to receive text messages "
                "from SoFi Technologies in relation to this job application. Message frequency varies. "
                "Reply STOP to opt-out of future messaging. Reply HELP for help. Message and data rates may apply."
            ),
            "required": True,
            "fields": [
                {
                    "name": "question_sms_acknowledgement",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="SoFi",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["option"], "Yes")

    def test_question_step_answers_follow_up_sms_application_status_opt_in_with_yes(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": (
                "If you provided a phone number, do you consent to receiving follow-up communication via text "
                "message (or SMS message) regarding your application status?"
            ),
            "required": True,
            "fields": [
                {
                    "name": "question_sms_status",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Grove Collaborative",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["option"], "Yes")

    def test_question_step_answers_nda_noncompete_with_no(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Are you currently subject to a non-compete agreement or an agreement not to solicit customers with your current or prior employer which may prevent you from performing the job for which you are applying?",
            "required": True,
            "fields": [
                {
                    "name": "question_non_compete",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Fivetran",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["option"], "No")
        self.assertEqual(step["source"], "deterministic")

    def test_question_step_answers_prior_employment_or_consulting_prompt_with_no(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": (
                "Have you worked at or been a consultant for SoFi or any company subsequently acquired by a SoFi "
                "entity (including Galileo Financial Technologies, Technisys, Wyndham Capital Mortgage, Zenbanx, "
                "8 Securities, and/or Golden Pacific Bancorp, Clara Lending)?"
            ),
            "required": True,
            "fields": [
                {
                    "name": "question_prior_employment_consulting",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="SoFi",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["option"], "No")
        self.assertEqual(step["source"], "master_resume.md")

    def test_question_step_answers_tekion_contractor_history_text_prompt_with_no(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": (
                "If you are presently working, or have worked in the past, as a contractor or consultant for Tekion, "
                "please provide the dates and name of the agency/company."
            ),
            "required": True,
            "fields": [{"name": "question_tekion_history", "type": "textarea"}],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Tekion",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["value"], "No")
        self.assertEqual(step["source"], "deterministic")

    def test_question_step_answers_employed_with_company_prompt_with_no(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Have you been employed with The Trade Desk?",
            "required": True,
            "fields": [
                {
                    "name": "question_employed_with_company",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="The Trade Desk",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["option"], "No")
        self.assertEqual(step["source"], "master_resume.md")

    def test_question_step_answers_employed_with_company_custom_negative_option(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Have you been employed with The Trade Desk?",
            "required": True,
            "fields": [
                {
                    "name": "question_employed_with_company_custom",
                    "type": "multi_value_single_select",
                    "values": [
                        {"label": "N/A"},
                        {"label": "Former Employee"},
                        {"label": "Current Contractor"},
                        {"label": "Former Contractor"},
                        {"label": "Former Intern"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="The Trade Desk",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["option"], "N/A")
        self.assertEqual(step["source"], "master_resume.md")

    def test_question_step_answers_tekion_dealer_partner_prompt_with_no(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": (
                "Are you currently working for a Tekion Dealer Partner or is the dealership you are working for "
                "in process to implement Tekion?"
            ),
            "required": True,
            "fields": [
                {
                    "name": "question_tekion_partner",
                    "type": "multi_value_single_select",
                    "values": [{"label": "Yes"}, {"label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Tekion",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["option"], "No")
        self.assertEqual(step["source"], "master_resume.md")

    def test_question_step_answers_conflict_of_interest_referral_with_no(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "To your knowledge, were you referred to this position by a senior leader or decision-maker at a current or prospective institutional client, business partner, or vendor of Coinbase?",
            "required": True,
            "fields": [
                {
                    "name": "question_conflict_referral",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Coinbase",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["option"], "No")
        self.assertEqual(step["source"], "deterministic")

    def test_question_step_answers_tekion_relationship_prompt_with_no(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": (
                "Are you related to, or in a relationship with, anyone that works for Tekion? "
                "If yes, what is your relationship to them?"
            ),
            "required": True,
            "fields": [{"name": "question_tekion_relationship", "type": "textarea"}],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Tekion",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["value"], "No")
        self.assertEqual(step["source"], "deterministic")

    def test_question_step_answers_robinhood_conflict_bundle_with_no(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": (
                "Do you have:\n"
                "a) any Personal/Familial Relationships (current Robinhood employees or employees of Robinhood’s vendors); \n"
                "b) any Outside Business Activities that you wish to continue; \n"
                "c) any investment that is greater than 5% of the outstanding shares of a publicly-traded company;\n"
                "d) any investment in a private company that has a business relationship or that is a current competitor of Robinhood; or \n"
                "e) any Intellectual Property Ownership (patents, trademarks, copyrights) that you wish to retain and/or create/develop while at Robinhood?"
            ),
            "required": True,
            "fields": [
                {
                    "name": "question_62764175",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Robinhood",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["option"], "No")
        self.assertEqual(step["source"], "deterministic")

    def test_question_step_answers_robinhood_government_official_bundle_with_no(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": (
                "Robinhood adheres to applicable laws and regulations in relation to government officials given inherent bribery and/or corruption risk. "
                "A government official is any person that performs a public function on any level or acts in any official capacity on behalf of a government or government owned entity. \n"
                "a)  Do you currently hold or have you held, within the last 5 years, a position as a government official?\n"
                "b)  Have you been referred or recommended for this position by a government official?\n"
                "c)  Are you related to or have a close personal relationship with a government official?"
            ),
            "required": True,
            "fields": [
                {
                    "name": "question_62764177",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Robinhood",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["option"], "No")
        self.assertEqual(step["source"], "deterministic")

    def test_question_step_marks_big_data_experience_as_yes(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Do you have experience with Big Data technologies?",
            "required": True,
            "fields": [
                {
                    "name": "question_3",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Starburst",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["option"], "Yes")

    def test_question_step_uses_full_onsite_start_location_answer_for_text_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "This is an onsite job in SF or Seattle, w 1-day a week wfh flexibility. Have you taken this into consideration & still want to proceed? If so, when is the soonest you could start? And at which location?",
            "required": True,
            "fields": [
                {
                    "name": "question_start_location",
                    "type": "textarea",
                }
            ],
        }

        with mock.patch.object(
            autofill,
            "build_onsite_start_location_answer",
            return_value="Yes. The soonest I could start is March 23, 2026, and I would plan to work from San Francisco.",
        ):
            step = autofill._question_step(
                question=question,
                profile=profile,
                application_profile=application_profile,
                company_name="Stand Insurance",
                cover_letter="Test cover letter.",
                cover_letter_file=None,
                generated_answers={},
            )

        self.assertEqual(step["kind"], "textarea")
        self.assertEqual(
            step["value"],
            "Yes. The soonest I could start is March 23, 2026, and I would plan to work from San Francisco.",
        )
        self.assertEqual(step["source"], "application_profile.md")

    def test_pending_user_input_questions_detect_specialized_prompts(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "Creative execution under constraints",
                    "description": (
                        "Describe a product decision or launch where regulatory, actuarial, or carrier constraints "
                        "blocked the obvious solution. What alternative approach did you design, and why did it work?"
                    ),
                    "fields": [{"name": "creative_constraints", "type": "textarea"}],
                }
            ],
            "eeoc_sections": [],
        }

        pending = autofill._pending_user_input_questions(
            job_post,
            autofill._parse_application_profile((PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")),
        )

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["field_name"], "creative_constraints")

    def test_pending_user_input_questions_skip_optional_name_pronunciation_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": False,
                    "label": "(Optional) Personal Preferences",
                    "description": "How do you pronounce your name?",
                    "fields": [{"name": "question_name_pronunciation", "type": "input_text"}],
                }
            ],
            "eeoc_sections": [],
        }

        pending = autofill._pending_user_input_questions(
            job_post,
            autofill._parse_application_profile((PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")),
        )

        self.assertEqual(pending, [])

    def test_pending_user_input_questions_detect_candidate_ai_guidance_attestation(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "AI Policy for Application",
                    "description": (
                        "We invite you to review our AI partnership guidelines for candidates and confirm "
                        "your understanding by selecting Yes."
                    ),
                    "fields": [{"name": "question_ai_policy", "type": "multi_value_single_select"}],
                }
            ],
            "eeoc_sections": [],
        }

        pending = autofill._pending_user_input_questions(
            job_post,
            autofill._parse_application_profile((PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")),
        )

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["field_name"], "question_ai_policy")

    def test_pending_user_input_questions_detect_company_relationship_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": (
                        "Do you have any relatives or personal relationships working at Wing? "
                        "If yes, please provide their name(s), department(s) and relationship(s) to you."
                    ),
                    "description": "",
                    "fields": [{"name": "question_relationship", "type": "input_text"}],
                }
            ],
            "eeoc_sections": [],
        }

        pending = autofill._pending_user_input_questions(
            job_post,
            autofill._parse_application_profile((PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")),
        )

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["field_name"], "question_relationship")

    def test_pending_user_input_questions_detect_missing_shared_gpa_value(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "Please list your undergraduate (Bachelor's) GPA:",
                    "description": "",
                    "fields": [{"name": "question_gpa", "type": "input_text"}],
                }
            ],
            "eeoc_sections": [],
        }
        application_profile = autofill._parse_application_profile(
            """
            - Country: United States
            - Location: San Francisco, CA
            - Work Authorization Statement: I am always authorized to work in the United States unconditionally.
            - Authorized to Work Unconditionally: Yes
            - Require Sponsorship Now: No
            - Require Sponsorship in Future: No
            - Sponsorship Answer: No
            - Compensation Expectations: I'm open and flexible on compensation.
            - Gender: Male
            - Race or Ethnicity: Hispanic or Latino
            - Veteran Status: I am not a protected veteran
            - Disability Status: No, I do not have a disability and have not had one in the past
            - Sexual Orientation: Straight / Heterosexual
            """
        )

        pending = autofill._pending_user_input_questions(job_post, application_profile)

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["field_name"], "question_gpa")

    def test_build_payload_writes_pending_user_input_for_generated_answer_blockers(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            blocker = {
                "field_name": "question_sql",
                "label": "Describe your SQL fluency and the types of analyses you have run.",
                "kind": "textarea",
                "required": True,
                "source": "generated_application_answer",
                "status": "planned",
                "blocker_kind": "generated_answer",
                "blocks_draft_completion": True,
                "reason": "The current run routed this required question through generated-answer handling, but no answer was returned.",
            }

            with (
                mock.patch.object(
                    autofill,
                    "_load_meta",
                    return_value={
                        "company": "acme",
                        "company_proper": "Acme",
                        "jd_source": "https://boards.greenhouse.io/acme/jobs/123",
                        "jd_title": "Principal Product Manager",
                    },
                ),
                mock.patch.object(
                    autofill, "_parse_master_resume", return_value=mock.Mock(email="jerrisonli@gmail.com")
                ),
                mock.patch.object(
                    autofill, "_parse_application_profile", return_value=mock.Mock(verification_code_email=None)
                ),
                mock.patch.object(
                    autofill, "_greenhouse_application_url", return_value="https://boards.greenhouse.io/acme/jobs/123"
                ),
                mock.patch.object(autofill, "_fetch_greenhouse_html", return_value="<html></html>"),
                mock.patch.object(autofill, "_extract_job_post", return_value={"questions": [], "eeoc_sections": []}),
                mock.patch.object(
                    autofill,
                    "_build_steps",
                    side_effect=autofill.GeneratedAnswerBlockersError([blocker]),
                ),
            ):
                with self.assertRaisesRegex(ValueError, "generated-answer regressions"):
                    autofill._build_payload(out_dir)

            pending_payload = json.loads((out_dir / "submit" / "pending_user_input.json").read_text(encoding="utf-8"))

        self.assertEqual(pending_payload["status"], "pending_user_input")
        self.assertEqual(pending_payload["questions"][0]["field_name"], "question_sql")
        self.assertEqual(pending_payload["questions"][0]["blocker_kind"], "generated_answer")
        self.assertIn("generated-answer handling", pending_payload["questions"][0]["reason"])

    def test_build_payload_prefers_resolved_board_url_over_wrapper_source(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            wrapper_url = "https://www.linkedin.com/jobs/view/4382138139/"
            resolved_board_url = "https://navan.com/careers/openings/7660273?gh_jid=7660273&gh_src=g19hdlp11us"
            application_url = "https://boards.greenhouse.io/embed/job_app?for=navan&token=7660273"

            with (
                mock.patch.object(
                    autofill,
                    "_load_meta",
                    return_value={
                        "company": "navan",
                        "company_proper": "Navan",
                        "jd_source": wrapper_url,
                        "jd_source_resolved": resolved_board_url,
                        "board_url": resolved_board_url,
                        "jd_title": "Staff Product Manager, AI",
                    },
                ),
                mock.patch.object(
                    autofill, "_parse_master_resume", return_value=mock.Mock(email="jerrisonli@gmail.com")
                ),
                mock.patch.object(
                    autofill, "_parse_application_profile", return_value=mock.Mock(verification_code_email=None)
                ),
                mock.patch.object(
                    autofill,
                    "_greenhouse_application_url",
                    return_value=application_url,
                ) as application_url_mock,
                mock.patch.object(autofill, "_fetch_greenhouse_html", return_value="<html></html>"),
                mock.patch.object(autofill, "_extract_job_post", return_value={"questions": [], "eeoc_sections": []}),
                mock.patch.object(autofill, "_build_steps", return_value=[]),
            ):
                payload = autofill._build_payload(out_dir)

        application_url_mock.assert_called_once_with(resolved_board_url, company_hint="navan")
        self.assertEqual(payload["job_url"], application_url)
        self.assertEqual(payload["job_source_url"], wrapper_url)

    def test_verify_generated_answers_retries_once_on_verifier_feedback(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            question_specs = [
                {
                    "field_name": "question_why_company",
                    "label": "Why Robinhood?",
                    "description": "",
                    "required": True,
                    "type": "input_text",
                }
            ]
            (submit_dir / autofill.APPLICATION_ANSWER_CACHE).write_text(
                json.dumps(
                    {
                        "provider": "openai",
                        "questions": question_specs,
                        "answers": {
                            "question_why_company": "I want to own Robinhood's platform roadmap end to end.",
                        },
                    }
                ),
                encoding="utf-8",
            )
            generated_answers = {
                "question_why_company": "I want to own Robinhood's platform roadmap end to end.",
            }

            retry_verification = {
                "status": "blocked",
                "questions": [
                    {
                        "field_name": "question_why_company",
                        "label": question_specs[0]["label"],
                        "verdict": "retry_with_feedback",
                        "feedback_for_regeneration": [
                            "Remove the unsupported claim about owning Robinhood's roadmap.",
                            "Ground the answer in supported workflow automation and AI product experience.",
                        ],
                        "source_refs": ["master_resume.md"],
                    }
                ],
                "blockers": [],
                "retry_feedback_by_field": {
                    "question_why_company": [
                        "Remove the unsupported claim about owning Robinhood's roadmap.",
                        "Ground the answer in supported workflow automation and AI product experience.",
                    ]
                },
            }
            approved_verification = {
                "status": "verified",
                "questions": [
                    {
                        "field_name": "question_why_company",
                        "label": question_specs[0]["label"],
                        "verdict": "approved",
                        "feedback_for_regeneration": [],
                        "source_refs": ["master_resume.md"],
                    }
                ],
                "blockers": [],
                "retry_feedback_by_field": {},
            }

            with (
                mock.patch.object(
                    autofill,
                    "verify_generated_answers",
                    side_effect=[retry_verification, approved_verification],
                ) as verify,
                mock.patch.object(
                    autofill,
                    "_run_answer_generation_provider",
                    return_value=(
                        {
                            "question_why_company": (
                                "I'm excited by Robinhood's mission to expand access and by the chance to apply my "
                                "workflow automation, AI, and regulated-product experience to a complex consumer platform."
                            )
                        },
                        None,
                    ),
                ) as run_provider,
            ):
                verification = autofill._verify_generated_answers_for_current_draft(
                    out_dir=out_dir,
                    meta={"company": "robinhood", "company_proper": "Robinhood"},
                    job_post={"questions": [], "eeoc_sections": []},
                    generated_answers=generated_answers,
                    application_profile=autofill._parse_application_profile(
                        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
                    ),
                )

            self.assertEqual(verification["status"], "verified")
            self.assertEqual(run_provider.call_count, 1)
            self.assertEqual(verify.call_count, 2)
            self.assertIn("workflow automation", generated_answers["question_why_company"])
            payload = json.loads((submit_dir / autofill.APPLICATION_ANSWER_CACHE).read_text(encoding="utf-8"))
            self.assertEqual(payload["answers"]["question_why_company"], generated_answers["question_why_company"])

    def test_verify_generated_answers_blanks_optional_field_after_retry_feedback_exhaustion(self):
        autofill = load_module("autofill_greenhouse_optional_retry_blank", "scripts/autofill_greenhouse.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            question_specs = [
                {
                    "field_name": "question_additional_info",
                    "label": "Additional Information",
                    "description": "Anything else you'd like us to know about you? Please share it here.",
                    "required": False,
                    "type": "textarea",
                }
            ]
            (submit_dir / autofill.APPLICATION_ANSWER_CACHE).write_text(
                json.dumps(
                    {
                        "provider": "openai",
                        "questions": question_specs,
                        "answers": {
                            "question_additional_info": (
                                "I am excited by the role and believe my background aligns well with where the company is headed."
                            ),
                        },
                    }
                ),
                encoding="utf-8",
            )
            generated_answers = {
                "question_additional_info": (
                    "I am excited by the role and believe my background aligns well with where the company is headed."
                ),
            }

            retry_feedback_verification = {
                "status": "blocked",
                "questions": [
                    {
                        "field_name": "question_additional_info",
                        "label": question_specs[0]["label"],
                        "verdict": "retry_with_feedback",
                        "feedback_for_regeneration": [
                            "Anchor the company-fit statement more directly to documented company direction.",
                        ],
                        "source_refs": ["master_resume.md", "content/jd_parsed.json"],
                    }
                ],
                "blockers": [],
                "retry_feedback_by_field": {
                    "question_additional_info": [
                        "Anchor the company-fit statement more directly to documented company direction.",
                    ]
                },
            }
            blank_verified = {
                "status": "verified",
                "questions": [
                    {
                        "field_name": "question_additional_info",
                        "label": question_specs[0]["label"],
                        "verdict": "approved",
                        "feedback_for_regeneration": [],
                        "source_refs": [],
                    }
                ],
                "blockers": [],
                "retry_feedback_by_field": {},
            }

            with (
                mock.patch.object(
                    autofill,
                    "verify_generated_answers",
                    side_effect=[retry_feedback_verification, retry_feedback_verification, blank_verified],
                ) as verify,
                mock.patch.object(
                    autofill,
                    "_run_answer_generation_provider",
                    return_value=(
                        {
                            "question_additional_info": (
                                "I am especially energized by the role and my background seems aligned with the company direction."
                            )
                        },
                        None,
                    ),
                ) as run_provider,
            ):
                verification = autofill._verify_generated_answers_for_current_draft(
                    out_dir=out_dir,
                    meta={"company": "hometap", "company_proper": "Hometap"},
                    job_post={"questions": [], "eeoc_sections": []},
                    generated_answers=generated_answers,
                    application_profile=autofill._parse_application_profile(
                        (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
                    ),
                )

            self.assertEqual(verification["status"], "verified")
            self.assertEqual(run_provider.call_count, 1)
            self.assertEqual(verify.call_count, 3)
            self.assertNotIn("question_additional_info", generated_answers)
            payload = json.loads((submit_dir / autofill.APPLICATION_ANSWER_CACHE).read_text(encoding="utf-8"))
            self.assertNotIn("question_additional_info", payload["answers"])

    def test_build_payload_writes_job_unavailable_artifact_for_job_closed_signal(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)

            with (
                mock.patch.object(
                    autofill,
                    "_load_meta",
                    return_value={
                        "company": "acme",
                        "company_proper": "Acme",
                        "jd_source": "https://boards.greenhouse.io/acme/jobs/123",
                        "jd_title": "Principal Product Manager",
                    },
                ),
                mock.patch.object(
                    autofill, "_parse_master_resume", return_value=mock.Mock(email="jerrisonli@gmail.com")
                ),
                mock.patch.object(
                    autofill, "_parse_application_profile", return_value=mock.Mock(verification_code_email=None)
                ),
                mock.patch.object(
                    autofill, "_greenhouse_application_url", return_value="https://boards.greenhouse.io/acme/jobs/123"
                ),
                mock.patch.object(
                    autofill,
                    "_fetch_greenhouse_html",
                    side_effect=RuntimeError("job_closed: Job posting not found (HTTP 404)"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "job_closed"):
                    autofill._build_payload(out_dir)

            unavailable_payload = json.loads((out_dir / "submit" / "job_unavailable.json").read_text(encoding="utf-8"))

        self.assertEqual(unavailable_payload["status"], "job_closed")
        self.assertEqual(unavailable_payload["board"], "greenhouse")
        self.assertIn("HTTP 404", unavailable_payload["message"])

    def test_write_payload_build_failure_result_prefers_resolved_board_url_over_wrapper_source(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            wrapper_url = "https://www.linkedin.com/jobs/view/4382138139/"
            resolved_board_url = "https://navan.com/careers/openings/7660273?gh_jid=7660273&gh_src=g19hdlp11us"
            application_url = "https://boards.greenhouse.io/embed/job_app?for=navan&token=7660273"

            with (
                mock.patch.object(
                    autofill,
                    "_load_meta",
                    return_value={
                        "company": "navan",
                        "company_proper": "Navan",
                        "jd_source": wrapper_url,
                        "jd_source_resolved": resolved_board_url,
                        "board_url": resolved_board_url,
                        "jd_title": "Staff Product Manager, AI",
                    },
                ),
                mock.patch.object(
                    autofill,
                    "_greenhouse_application_url",
                    return_value=application_url,
                ) as application_url_mock,
            ):
                result_path = autofill._write_payload_build_failure_result(
                    out_dir,
                    ValueError("Autofill payload is missing required Greenhouse fields: first_name"),
                )

            result = json.loads(result_path.read_text(encoding="utf-8"))

        application_url_mock.assert_called_once_with(resolved_board_url, company_hint="navan")
        self.assertEqual(result["job_url"], application_url)
        self.assertEqual(result["company"], "Navan")
        self.assertEqual(result["failure_type"], autofill.GREENHOUSE_RUNTIME_FAILURE)

    def test_write_payload_build_failure_result_persists_missing_required_fields(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir(parents=True)
            report_path = submit_dir / autofill.AUTOFILL_REPORT_MD
            report_path.write_text("# report\n", encoding="utf-8")

            with mock.patch.object(
                autofill,
                "_load_meta",
                return_value={
                    "company": "acme",
                    "company_proper": "Acme",
                    "jd_source": "https://boards.greenhouse.io/acme/jobs/123",
                    "jd_title": "Principal Product Manager",
                },
            ):
                result_path = autofill._write_payload_build_failure_result(
                    out_dir,
                    ValueError("Autofill payload is missing required Greenhouse fields: question_123, question_456"),
                )

            failed_payload = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertEqual(failed_payload["status"], "failed")
        self.assertEqual(failed_payload["board"], "greenhouse")
        self.assertEqual(failed_payload["failure_type"], autofill.GREENHOUSE_RUNTIME_FAILURE)
        self.assertEqual(failed_payload["current_page"], "build_payload")
        self.assertEqual(failed_payload["validation_errors"], ["question_123", "question_456"])
        self.assertEqual(failed_payload["company"], "Acme")
        self.assertEqual(failed_payload["job_title"], "Principal Product Manager")
        self.assertEqual(failed_payload["artifacts"]["report_markdown"], str(report_path))

    def test_question_step_maps_city_profile_to_state_only_location_option(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "What location do you intend to work out of?",
            "required": True,
            "fields": [
                {
                    "name": "question_location",
                    "type": "multi_value_single_select",
                    "values": [
                        {"value": 1, "label": "California"},
                        {"value": 2, "label": "Colorado"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Starburst",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["option"], "California")
        self.assertEqual(step["search"], "California")

    def test_question_step_current_residence_state_uses_candidate_state_not_role_location(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Which state do you currently reside in?",
            "required": True,
            "fields": [
                {
                    "name": "question_current_residence_state",
                    "type": "multi_value_single_select",
                    "values": [
                        {"value": 1, "label": "California"},
                        {"value": 2, "label": "Massachusetts"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="ezCater",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
            role_location="Boston, MA",
        )

        self.assertEqual(step["option"], "California")
        self.assertEqual(step["search"], "California")

    def test_question_step_skips_voluntary_demographic_decline_opt_out_when_follow_ups_exist(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "U.S. Equal Opportunity Employment Information (Completion is voluntary)",
            "description": (
                "Submitting this information is entirely voluntary. We invite candidates to self-identify "
                "as to the categories below."
            ),
            "required": False,
            "fields": [
                {
                    "name": "question_eeo_decline[]",
                    "type": "multi_value_multi_select",
                    "values": [{"value": 1, "label": "I decline to answer the following questions."}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Airbnb",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={"question_eeo_decline[]": ["I decline to answer the following questions."]},
        )

        self.assertIsNone(step)

    def test_question_step_state_of_residence_uses_candidate_state(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "State of residence",
            "required": True,
            "fields": [
                {
                    "name": "question_state_of_residence",
                    "type": "multi_value_single_select",
                    "values": [
                        {"value": 1, "label": "California"},
                        {"value": 2, "label": "Massachusetts"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="ezCater",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
            role_location="Boston, MA",
        )

        self.assertEqual(step["option"], "California")
        self.assertEqual(step["search"], "California")

    def test_question_step_where_currently_based_uses_candidate_location(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Where are you currently based?",
            "required": True,
            "fields": [
                {
                    "name": "question_currently_based",
                    "type": "input_text",
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="ezCater",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
            role_location="Boston, MA",
        )

        self.assertEqual(step["value"], "San Francisco, CA")
        self.assertEqual(step["source"], "application_profile.md")
        self.assertTrue(step["blocks_draft_completion"])
        self.assertEqual(step["blocker_kind"], "visible_profile_field")
        self.assertEqual(step["profile_field"], "location")

    def test_question_step_work_location_prefers_san_francisco_over_role_city(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Which office location do you prefer?",
            "required": True,
            "fields": [
                {
                    "name": "question_preferred_office",
                    "type": "multi_value_single_select",
                    "values": [
                        {"value": "nyc", "label": "New York"},
                        {"value": "sf", "label": "San Francisco"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="ezCater",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
            role_location="New York, NY",
        )

        self.assertEqual(step["option"], "San Francisco")
        self.assertEqual(step["profile_field"], "location")
        self.assertEqual(step["blocker_kind"], "visible_profile_field")

    def test_question_step_current_state_abbreviation_uses_candidate_state(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Which state or province do you currently live in?",
            "required": True,
            "fields": [
                {
                    "name": "question_current_state_or_province",
                    "type": "multi_value_single_select",
                    "values": [
                        {"value": 1, "label": "CA"},
                        {"value": 2, "label": "MA"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="ezCater",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
            role_location="Boston, MA",
        )

        self.assertEqual(step["option"], "CA")
        self.assertEqual(step["search"], "CA")

    def test_question_step_intended_work_location_prefers_candidate_location(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Where do you intend to work out of?",
            "required": True,
            "fields": [
                {
                    "name": "question_intended_work_location",
                    "type": "multi_value_single_select",
                    "values": [
                        {"value": 1, "label": "California"},
                        {"value": 2, "label": "Massachusetts"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="ezCater",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
            role_location="Boston, MA",
        )

        self.assertEqual(step["option"], "California")
        self.assertEqual(step["search"], "California")

    def test_question_step_yes_no_residency_gate_stays_on_location_residency_path(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Do you currently reside in the location specified for this role?",
            "required": True,
            "fields": [
                {
                    "name": "question_role_location_gate",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="ezCater",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
            role_location="Boston, MA",
        )

        self.assertEqual(step["option"], "Yes")

    def test_question_step_explicit_named_location_residency_uses_profile_location_truthfully(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Do you currently reside in the Washington, D.C. metro area?",
            "required": True,
            "fields": [
                {
                    "name": "question_dc_residency_gate",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="ID.me",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
            role_location="McLean, VA",
        )

        self.assertEqual(step["option"], "No")
        self.assertEqual(step["source"], "application_profile.md")

    def test_question_step_current_residence_state_fails_closed_when_profile_state_is_unparseable(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        application_profile.location = "San Francisco"
        question = {
            "label": "Which state do you currently reside in?",
            "required": True,
            "fields": [
                {
                    "name": "question_current_residence_state",
                    "type": "multi_value_single_select",
                    "values": [
                        {"value": 1, "label": "California"},
                        {"value": 2, "label": "Massachusetts"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="ezCater",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
            role_location="Boston, MA",
        )

        self.assertEqual(step["kind"], "combobox")
        self.assertEqual(step["status"], "planned")
        self.assertTrue(step["skip_runtime_fill"])
        self.assertIn("parseable state", step["report_value"])
        self.assertIn("application_profile.md", step["report_value"])
        self.assertFalse(step.get("option"))
        self.assertFalse(step.get("search"))

    def test_question_step_current_residence_state_multi_select_uses_candidate_state_option(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "What state do you currently reside in?",
            "required": True,
            "fields": [
                {
                    "name": "question_current_residence_state_multi",
                    "type": "multi_value_multi_select",
                    "values": [
                        {"value": 1, "label": "California"},
                        {"value": 2, "label": "Massachusetts"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Figure",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
            role_location="New York, NY",
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "checkbox")
        self.assertEqual(step["option"], "California")
        self.assertEqual(step["source"], "application_profile.md")

    def test_question_step_city_location_category_uses_profile_location_for_text_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "What city/state do you currently reside?",
            "required": True,
            "fields": [{"name": "question_city_state", "type": "input_text"}],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Afresh",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["value"], application_profile.location)
        self.assertEqual(step["source"], "application_profile.md")

    def test_question_step_cities_multi_select_produces_checkbox_step(self):
        """multi_value_multi_select city questions must produce a checkbox step."""
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "In what cities are you available to work?",
            "required": True,
            "fields": [
                {
                    "name": "question_61965875",
                    "type": "multi_value_multi_select",
                    "values": [
                        {"value": 1, "label": "New York City"},
                        {"value": 2, "label": "San Francisco"},
                        {"value": 3, "label": "Boston"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Datadog",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step, "multi_value_multi_select city question must not return None")
        self.assertEqual(step["kind"], "checkbox")
        self.assertEqual(step["option"], "San Francisco")

    def test_application_question_specs_include_multi_select_questions(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "label": "Which of these roles are you most interested in? Select up to 3.",
                    "required": True,
                    "fields": [
                        {
                            "name": "question_preferred_roles",
                            "type": "multi_value_multi_select",
                            "values": [
                                {"value": 1, "label": "Product Manager"},
                                {"value": 2, "label": "Platform PM"},
                                {"value": 3, "label": "Growth PM"},
                            ],
                        }
                    ],
                }
            ],
            "eeoc_sections": [],
        }

        specs = autofill._application_question_specs(job_post)

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["field_name"], "question_preferred_roles")
        self.assertEqual(specs[0]["type"], "multi_value_multi_select")
        self.assertEqual(specs[0]["options"], ["Product Manager", "Platform PM", "Growth PM"])

    def test_application_question_specs_mark_preference_research_questions(self):
        autofill = load_module("autofill_greenhouse_pref_specs", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "label": "Which of these roles are you most interested in? Select up to 3.",
                    "required": True,
                    "fields": [
                        {
                            "name": "question_preferred_roles",
                            "type": "multi_value_multi_select",
                            "values": [
                                {"value": 1, "label": "Product Manager"},
                                {"value": 2, "label": "Platform PM"},
                                {"value": 3, "label": "Growth PM"},
                            ],
                        }
                    ],
                },
                {
                    "label": "What location do you intend to work out of?",
                    "required": True,
                    "fields": [
                        {
                            "name": "question_location",
                            "type": "multi_value_single_select",
                            "values": [
                                {"value": 1, "label": "San Francisco"},
                                {"value": 2, "label": "New York"},
                            ],
                        }
                    ],
                },
            ],
            "eeoc_sections": [],
        }

        specs = autofill._application_question_specs(job_post)

        self.assertEqual(specs[0]["research_mode"], "preference_ranking")
        self.assertEqual(specs[0]["selection_limit"], 3)
        self.assertNotIn("research_mode", specs[1])

    def test_generate_application_answers_merges_preference_research_before_provider_call(self):
        autofill = load_module("autofill_greenhouse_pref_merge", "scripts/autofill_greenhouse.py")
        job_post = {
            "questions": [
                {
                    "required": True,
                    "label": "Which of these roles are you most interested in? Select up to 2.",
                    "fields": [
                        {
                            "name": "question_preferred_roles",
                            "type": "multi_value_multi_select",
                            "values": [
                                {"label": "Product Manager", "value": 1},
                                {"label": "Platform PM", "value": 2},
                                {"label": "Growth PM", "value": 3},
                            ],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": "Why Acme?",
                    "fields": [{"name": "why_acme", "type": "input_text"}],
                },
            ],
            "eeoc_sections": [],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "content").mkdir()
            (out_dir / "submit").mkdir()
            (out_dir / "content" / "cover_letter_text.txt").write_text(
                "Acme's platform and growth work are compelling.",
                encoding="utf-8",
            )

            preference_payload = {
                "cache_key": "pref-v1",
                "provider": "openai",
                "questions": [
                    {
                        "field_name": "question_preferred_roles",
                        "label": "Which of these roles are you most interested in? Select up to 2.",
                        "required": True,
                        "type": "multi_value_multi_select",
                        "options": ["Product Manager", "Platform PM", "Growth PM"],
                        "selection_limit": 2,
                        "selected_options": ["Platform PM", "Growth PM"],
                        "summary": "Platform and growth align best with the role scope.",
                        "supporting_evidence": ["JD emphasizes platform leverage and growth loops."],
                    }
                ],
                "answers": {"question_preferred_roles": ["Platform PM", "Growth PM"]},
                "failures": [],
                "artifacts": {
                    "context_json": str(out_dir / "submit" / "preference_research_context.json"),
                    "failures_json": str(out_dir / "submit" / "preference_research_failures.json"),
                    "raw_output": str(out_dir / "submit" / "preference_research_raw.txt"),
                },
                "used_cached_artifacts": False,
            }

            with (
                mock.patch.object(autofill, "prepare_preference_research_context", return_value=preference_payload),
                mock.patch.object(
                    autofill,
                    "prepare_linked_resource_context",
                    return_value={
                        "cache_key": None,
                        "prompt_context": None,
                        "resources": [],
                        "failures": [],
                        "artifacts": {},
                    },
                ),
                mock.patch.object(
                    autofill,
                    "_run_answer_generation_provider",
                    return_value=({"why_acme": "Because the platform work compounds product leverage."}, None),
                ) as runner,
            ):
                answers = autofill._generate_application_answers(
                    out_dir=out_dir,
                    meta={"company": "acme", "board": "greenhouse"},
                    job_post=job_post,
                    provider="openai",
                )

            self.assertEqual(
                answers,
                {
                    "question_preferred_roles": ["Platform PM", "Growth PM"],
                    "why_acme": "Because the platform work compounds product leverage.",
                },
            )
            provider_specs = runner.call_args.kwargs["question_specs"]
            self.assertEqual([spec["field_name"] for spec in provider_specs], ["why_acme"])
            payload = json.loads((out_dir / "submit" / autofill.APPLICATION_ANSWER_CACHE).read_text(encoding="utf-8"))
            self.assertEqual(payload["preference_research"]["cache_key"], "pref-v1")
            self.assertEqual(
                payload["preference_research"]["questions"][0]["selected_options"],
                ["Platform PM", "Growth PM"],
            )

    def test_build_steps_blocks_when_preference_research_answer_drifts_from_live_options(self):
        autofill = load_module("autofill_greenhouse_pref_drift", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        job_post = {
            "questions": [
                {
                    "label": "Which of these roles are you most interested in? Select up to 2.",
                    "required": True,
                    "fields": [
                        {
                            "name": "question_preferred_roles",
                            "type": "multi_value_multi_select",
                            "values": [
                                {"value": 1, "label": "Product Manager"},
                                {"value": 2, "label": "Growth PM"},
                            ],
                        }
                    ],
                }
            ],
            "eeoc_sections": [],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "content").mkdir()
            (out_dir / "submit").mkdir()
            (out_dir / "content" / "cover_letter_text.txt").write_text("Acme cover letter.", encoding="utf-8")
            (out_dir / "submit" / autofill.APPLICATION_ANSWER_CACHE).write_text(
                json.dumps(
                    {
                        "questions": autofill._application_question_specs(job_post),
                        "answers": {"question_preferred_roles": ["Platform PM"]},
                        "preference_research": {
                            "cache_key": "pref-v1",
                            "questions": [
                                {
                                    "field_name": "question_preferred_roles",
                                    "label": "Which of these roles are you most interested in? Select up to 2.",
                                    "required": True,
                                    "type": "multi_value_multi_select",
                                    "selected_options": ["Platform PM"],
                                }
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )
            (out_dir / "submit" / "preference_research_context.json").write_text("{}", encoding="utf-8")

            with self.assertRaises(autofill.GeneratedAnswerBlockersError) as excinfo:
                autofill._build_steps(
                    job_post,
                    {"company_proper": "Acme"},
                    profile,
                    application_profile,
                    out_dir,
                    generated_answers={"question_preferred_roles": ["Platform PM"]},
                )

            self.assertEqual(excinfo.exception.blockers[0]["artifact_key"], "preference_research_failures_json")

    def test_question_step_multi_select_with_generated_list_produces_multiple_steps(self):
        """multi_value_multi_select with generated list answer produces N checkbox steps."""
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "What tangible factors are most important to you? Select your top 3.",
            "required": True,
            "fields": [
                {
                    "name": "question_tangible_factors",
                    "type": "multi_value_multi_select",
                    "values": [
                        {"value": 1, "label": "Career Growth"},
                        {"value": 2, "label": "Work-life Balance"},
                        {"value": 3, "label": "Remote Work"},
                        {"value": 4, "label": "Culture"},
                        {"value": 5, "label": "Company Outlook"},
                    ],
                }
            ],
        }

        steps = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Motive",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={"question_tangible_factors": ["Career Growth", "Culture", "Company Outlook"]},
        )

        self.assertIsInstance(steps, list, "multi-select with list answer must return a list")
        self.assertEqual(len(steps), 3, "should produce 3 checkbox steps for 3 selections")
        self.assertTrue(all(s["kind"] == "checkbox" for s in steps))
        options = [s["option"] for s in steps]
        self.assertEqual(options, ["Career Growth", "Culture", "Company Outlook"])

    def test_question_step_matches_custom_never_worked_option(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Have you ever worked for Robinhood as an employee, intern or contractor?",
            "required": True,
            "fields": [
                {
                    "name": "question_prior_employment",
                    "type": "multi_value_single_select",
                    "values": [
                        {"value": 1, "label": "I currently work at Robinhood as a full-time employee or intern"},
                        {
                            "value": 2,
                            "label": "I have previously worked at Robinhood as a full-time employee or intern (Hoodie Alumni)",
                        },
                        {"value": 3, "label": "I currently work at Robinhood in a contractor role"},
                        {"value": 4, "label": "I have previously worked at Robinhood in a contractor role"},
                        {"value": 5, "label": "I have never worked at Robinhood"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Robinhood",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["kind"], "combobox")
        self.assertEqual(step["option"], "I have never worked at Robinhood")

    def test_question_targets_work_location_ignores_sponsorship_current_location_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        question = {
            "label": "Will you now or in the future require sponsorship for a visa to remain in your current location?",
            "fields": [{"name": "question_current_location_sponsorship", "type": "multi_value_single_select"}],
        }

        self.assertFalse(autofill._question_targets_work_location(question))

    def test_question_targets_work_location_matches_closest_office_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        question = {
            "label": "Which Scout Motors location are you closest to?",
            "fields": [{"name": "question_closest_location", "type": "multi_value_single_select"}],
        }

        self.assertTrue(autofill._question_targets_work_location(question))

    def test_question_step_answers_closest_location_prompt_with_same_state_option(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Which Scout Motors location are you closest to?",
            "required": True,
            "fields": [
                {
                    "name": "question_closest_location",
                    "type": "multi_value_single_select",
                    "values": [
                        {"label": "Charlotte, NC"},
                        {"label": "Fremont, CA"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Scout Motors",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["option"], "Fremont, CA")
        self.assertEqual(step["source"], "application_profile.md")

    def test_question_requires_generated_answer_excludes_product_management_experience_range_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        question = {
            "label": (
                "How many years of experience do you have in product management, "
                "with a strong emphasis on technical platforms or complex integrations?"
            ),
            "required": True,
            "fields": [
                {
                    "name": "question_pm_experience_range",
                    "type": "multi_value_single_select",
                    "values": [
                        {"label": "3–5 years"},
                        {"label": "5–7 years"},
                        {"label": "7–10 years"},
                    ],
                }
            ],
        }

        self.assertFalse(autofill._question_requires_generated_answer(question))

    def test_question_requires_generated_answer_for_office_attendance_detail_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        question = {
            "label": (
                "This role is primarily based in San Francisco, CA or New York City, with a requirement to be in "
                "the office a few days a week. Are you able to relocate or work from one of these locations? "
                "Please provide details."
            ),
            "required": True,
            "fields": [{"name": "question_office_detail", "type": "textarea"}],
        }

        self.assertTrue(autofill._question_requires_generated_answer(question))

    def test_question_step_answers_product_management_experience_range_from_resume_tenure(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": (
                "How many years of experience do you have in product management, "
                "with a strong emphasis on technical platforms or complex integrations?"
            ),
            "required": True,
            "fields": [
                {
                    "name": "question_pm_experience_range",
                    "type": "multi_value_single_select",
                    "values": [
                        {"label": "No experience"},
                        {"label": "Less than 1 year"},
                        {"label": "1–3 years"},
                        {"label": "3–5 years"},
                        {"label": "5–7 years"},
                        {"label": "7–10 years"},
                        {"label": "10–15 years"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Scout Motors",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["option"], "5–7 years")
        self.assertEqual(step["source"], "master_resume.md")

    def test_question_step_answers_product_usage_yes(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Have you used Robinhood?",
            "required": True,
            "fields": [
                {
                    "name": "question_product_usage",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Robinhood",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["option"], "Yes")

    def test_question_step_age_group_missing_profile_value_fails_closed(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        master_resume = """
        JERRISON LI
        Principal Product Manager
        San Francisco, CA  |  jerrisonli@gmail.com  |  510-613-5192  |  linkedin.com/in/jerrison/  |  jerrisonli.com
        ## MOODY'S ANALYTICS — Associate Director, Product Management
        Work Authorization: United States Citizen
        """
        application_profile_text = """
        - Country: United States
        - Location: San Francisco, CA
        - Work Authorization Statement: I am always authorized to work in the United States unconditionally.
        - Authorized to Work Unconditionally: Yes
        - Require Sponsorship Now: No
        - Require Sponsorship in Future: No
        - Sponsorship Answer: No
        - Gender: Male
        - Race or Ethnicity: Hispanic or Latino
        - Sexual Orientation: Straight / Heterosexual
        - Veteran Status: I am not a protected veteran
        - Disability Status: No, I do not have a disability and have not had one in the past
        """
        profile = autofill._parse_master_resume(master_resume)
        application_profile = autofill._parse_application_profile(application_profile_text)
        question = {
            "required": True,
            "label": "Which age group do you belong to?",
            "fields": [
                {
                    "name": "question_age_group",
                    "type": "multi_value_single_select",
                    "values": [{"label": "18-24", "value": 1}, {"label": "25-34", "value": 2}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Robinhood",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["kind"], "combobox")
        self.assertEqual(step["status"], "planned")
        self.assertTrue(step["skip_runtime_fill"])
        self.assertIn("Age Range / Age Group", step["report_value"])

    def test_question_step_handles_what_is_your_age_variant(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "required": False,
            "label": "What is your age?",
            "fields": [
                {
                    "name": "question_age",
                    "type": "multi_value_single_select",
                    "values": [{"label": "25-34", "value": 1}, {"label": "35-44", "value": 2}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="ezCater",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["option"], "35-44")
        self.assertEqual(step["source"], "application_profile.md")

    def test_validate_generated_answers_accepts_list_for_multi_select(self):
        """_validate_generated_answers accepts list[str] for multi_value_multi_select."""
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        specs = [
            {
                "field_name": "tangible_factors",
                "required": True,
                "type": "multi_value_multi_select",
                "options": ["Career Growth", "Culture", "Company Outlook"],
            }
        ]
        answers = {"tangible_factors": ["Career Growth", "Culture"]}
        result = autofill._validate_generated_answers(specs, answers)
        self.assertEqual(result["tangible_factors"], ["Career Growth", "Culture"])

    def test_validate_generated_answers_splits_comma_string_for_multi_select(self):
        """_validate_generated_answers splits comma-separated string for multi_value_multi_select."""
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        specs = [
            {
                "field_name": "tangible_factors",
                "required": True,
                "type": "multi_value_multi_select",
                "options": ["Career Growth", "Culture"],
            }
        ]
        answers = {"tangible_factors": "Career Growth, Culture"}
        result = autofill._validate_generated_answers(specs, answers)
        self.assertEqual(result["tangible_factors"], ["Career Growth", "Culture"])

    def test_validate_generated_answers_tops_up_preference_multi_select_to_three(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        specs = [
            {
                "field_name": "question_focus_areas",
                "label": "Select all that apply: Which product areas are you most interested in?",
                "type": "multi_value_multi_select",
                "values": [
                    {"label": "Growth"},
                    {"label": "Platform"},
                    {"label": "AI"},
                    {"label": "Security"},
                ],
            }
        ]

        validated = autofill._validate_generated_answers(specs, {"question_focus_areas": ["Growth"]})

        self.assertEqual(validated["question_focus_areas"], ["Growth", "Platform", "AI"])

    def test_match_option_label_expands_location_state_abbreviation(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        field = {
            "name": "question_location",
            "values": [
                {"label": "California"},
                {"label": "Colorado"},
            ],
        }

        selected = autofill._match_option_label(field, "San Francisco, CA")

        self.assertEqual(selected, "California")

    def test_match_option_label_maps_na_alias(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        field = {
            "name": "question_35137925002",
            "values": [{"label": "Yes"}, {"label": "No"}, {"label": "NA"}],
        }

        selected = autofill._match_option_label(field, "N/A")

        self.assertEqual(selected, "NA")

    def test_match_option_label_maps_none_alias_to_na(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        field = {
            "name": "question_finra",
            "values": [{"label": "Series 7"}, {"label": "N/A"}],
        }

        selected = autofill._match_option_label(field, "None")

        self.assertEqual(selected, "N/A")

    def test_match_option_label_rejects_gender_substring_false_positive(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        field = {
            "name": "gender",
            "values": [{"label": "Female"}, {"label": "Decline To Self Identify"}],
        }

        with self.assertRaises(ValueError):
            autofill._match_option_label(field, "Male")

    def test_greenhouse_profile_option_label_matches_gender_identity_alias_against_man_option(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        field = {
            "name": "question_35137925002",
            "values": [{"label": "Man"}, {"label": "Woman"}],
        }

        selected = autofill._greenhouse_profile_option_label(
            field,
            "Male",
            profile_field="gender_identity",
        )

        self.assertEqual(selected, "Man")

    def test_greenhouse_profile_option_label_matches_cisgender_male_slash_alias(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        field = {
            "name": "question_35137925002",
            "values": [{"label": "Cisgender man"}, {"label": "Cisgender woman"}],
        }

        selected = autofill._greenhouse_profile_option_label(
            field,
            "Cisgender Male/Man",
            profile_field="gender_identity",
        )

        self.assertEqual(selected, "Cisgender man")

    def test_greenhouse_profile_option_label_matches_cisgender_male_slash_alias_against_generic_masculine_option(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        field = {
            "name": "question_35137925002",
            "values": [{"label": "Man, male or masculine"}, {"label": "Woman, female or feminine"}],
        }

        selected = autofill._greenhouse_profile_option_label(
            field,
            "Cisgender Male/Man",
            profile_field="gender_identity",
        )

        self.assertEqual(selected, "Man, male or masculine")

    def test_greenhouse_profile_option_label_matches_disability_no_alias(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        field = {
            "name": "question_35137925003",
            "values": [
                {"label": "I do not identify as having a disability"},
                {"label": "Yes"},
            ],
        }

        selected = autofill._greenhouse_profile_option_label(
            field,
            "No, I do not have a disability and have not had one in the past",
            profile_field="disability_status",
        )

        self.assertEqual(selected, "I do not identify as having a disability")

    def test_greenhouse_profile_option_label_matches_disability_no_someone_alias(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        field = {
            "name": "question_35137925003",
            "values": [
                {"label": "I do not identify as someone with a disability"},
                {"label": "Yes"},
            ],
        }

        selected = autofill._greenhouse_profile_option_label(
            field,
            "No, I do not have a disability and have not had one in the past",
            profile_field="disability_status",
        )

        self.assertEqual(selected, "I do not identify as someone with a disability")

    def test_greenhouse_profile_option_label_matches_disability_none_of_these_apply_alias(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        field = {
            "name": "question_35137925003",
            "values": [
                {"label": "Blindness"},
                {"label": "Chronic illness or pain"},
                {"label": "None of these apply"},
            ],
        }

        selected = autofill._greenhouse_profile_option_label(
            field,
            "No, I do not have a disability and have not had one in the past",
            profile_field="disability_status",
        )

        self.assertEqual(selected, "None of these apply")

    def test_question_step_selects_not_applicable_for_work_authorization_kind_followup(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "required": True,
            "label": (
                "If you require work authorization, what kind? "
                "(If you do not require work authorization sponsorship please select Not Applicable.)"
            ),
            "fields": [
                {
                    "name": "question_35899451002",
                    "type": "multi_value_single_select",
                    "values": [
                        {"label": "CPT"},
                        {"label": "OPT"},
                        {"label": "H-1B"},
                        {"label": "E-3/H-1B1"},
                        {"label": "TN"},
                        {"label": "J-1"},
                        {"label": "Other"},
                        {"label": "I don't know"},
                        {"label": "Not Applicable"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Wing",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["option"], "Not Applicable")

    def test_question_step_treats_country_region_permanent_resident_prompt_as_yes_no(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "required": True,
            "label": (
                "Since obtaining your most recent citizenship, did you afterwards become a permanent "
                "resident in any other country/region? This does not include temporary statuses such "
                "as student visas or time-limited work permits."
            ),
            "fields": [
                {
                    "name": "question_35492682002",
                    "type": "multi_value_single_select",
                    "values": [{"label": "Yes"}, {"label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Twitch",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["option"], "No")

    def test_validate_generated_answers_missing_required_conditional_follow_up_builds_na_step(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "required": True,
            "label": "If so, are you eligible or currently in a period of Optional Practical Training (OPT)?",
            "fields": [
                {
                    "name": "question_35137924002",
                    "type": "multi_value_single_select",
                    "values": [{"label": "Yes"}, {"label": "No"}, {"label": "NA"}],
                }
            ],
        }
        spec = {
            "field_name": "question_35137924002",
            "label": question["label"],
            "description": "",
            "required": True,
            "type": "multi_value_single_select",
            "options": ["Yes", "No", "NA"],
        }

        for raw_answers in ({}, {"question_35137924002": None}, {"question_35137924002": ""}):
            validated = autofill._validate_generated_answers([spec], raw_answers)

            self.assertEqual(validated["question_35137924002"], "N/A")

            step = autofill._question_step(
                question=question,
                profile=profile,
                application_profile=application_profile,
                company_name="Duolingo",
                cover_letter="Test cover letter.",
                cover_letter_file=None,
                generated_answers=validated,
            )

            self.assertIsNotNone(step)
            self.assertEqual(step["option"], "NA")

    def test_question_step_answers_current_finra_license_inventory_with_no_license_option(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "required": True,
            "label": "What FINRA license(s), if any, do you currently hold?",
            "fields": [
                {
                    "name": "question_finra",
                    "type": "multi_value_multi_select",
                    "values": [{"label": "Series 7"}, {"label": "Series 24"}, {"label": "N/A"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="SoFi",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["option"], "N/A")
        self.assertEqual(step["source"], "deterministic_no_professional_credentials")

    def test_question_step_answers_future_finra_license_intent_with_no(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "required": True,
            "label": "Do you currently hold, or intend to hold, any FINRA licenses if employed by SoFi?",
            "fields": [
                {
                    "name": "question_finra_intent",
                    "type": "multi_value_single_select",
                    "values": [{"label": "Yes"}, {"label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="SoFi",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["option"], "No")
        self.assertEqual(step["source"], "deterministic_no_professional_credentials")

    def test_question_step_answers_explicit_state_list_from_actual_residence(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": (
                "Do you live in one of the following states?\n"
                "Alabama, Alaska, Delaware, Kansas, Maine, Mississippi, Montana, Nebraska, "
                "New Mexico, North Dakota, South Dakota, West Virginia, or Wyoming."
            ),
            "description": (
                "<p>Please mark '<strong>YES</strong>' if you currently live in one of these states. "
                "If you do <strong>NOT</strong> currently live in one of these states, mark '<strong>NO</strong>.'</p>"
            ),
            "required": True,
            "fields": [
                {
                    "name": "question_state_residence",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="CoreWeave",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["option"], "No")

    def test_question_step_answers_salary_comfort_from_application_profile(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Are you comfortable interviewing for the salary outlined in the job description ?",
            "required": False,
            "fields": [
                {
                    "name": "question_salary",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="CoreWeave",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["option"], "Yes")
        self.assertEqual(step["source"], "application_profile.md")

    def test_question_step_answers_salary_expectation_text_from_application_profile(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "What are your compensation expectations?",
            "required": True,
            "fields": [
                {
                    "name": "question_compensation",
                    "type": "input_text",
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="CoreWeave",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(
            step["value"],
            "I'm open and flexible on compensation. I'd prefer to learn more about the role's scope and total rewards package before discussing specific numbers.",
        )
        self.assertEqual(step["source"], "application_profile.md")

    def test_question_step_flags_numeric_only_salary_ranges_for_manual_review(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "What are your salary expectations?",
            "description": "Annual gross salary in euros for a full-time position",
            "required": True,
            "fields": [
                {
                    "name": "question_salary_expectations",
                    "type": "multi_value_single_select",
                    "values": [
                        {"value": 1, "label": "EUR 80,000 - 90,000"},
                        {"value": 2, "label": "EUR 90,000 - 100,000"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="refurbed",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["status"], "planned")
        self.assertTrue(step["skip_runtime_fill"])
        self.assertIn("numeric salary ranges", step["note"])

    def test_question_step_answers_linkedin_profile_included_confirmation_with_yes(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Did you include your LinkedIn profile as part of your application?",
            "required": True,
            "fields": [
                {
                    "name": "question_linkedin_confirmation",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="League Inc.",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["option"], "Yes")

    def test_question_step_answers_undergraduate_gpa_from_application_profile(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Please list your undergraduate (Bachelor's) GPA:",
            "required": True,
            "fields": [
                {
                    "name": "question_undergraduate_gpa",
                    "type": "textarea",
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="CoreWeave",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["value"], "3.8/4.0")
        self.assertEqual(step["source"], "application_profile.md")

    def test_question_step_answers_most_recent_degree_from_preferred_education_entry(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "What is the most recent degree you obtained?",
            "required": True,
            "fields": [{"name": "question_recent_degree", "type": "input_text"}],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Stripe",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["value"], "Master of Business Administration (M.B.A.)")
        self.assertEqual(step["source"], "master_resume.md")

    def test_question_step_answers_office_attendance_days_a_week_variant(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Are you able and willing to come into the office three days a week?",
            "required": True,
            "fields": [
                {
                    "name": "question_14778052004",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Sage",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["field_name"], "question_14778052004")
        self.assertEqual(step["option"], "Yes")
        self.assertEqual(step["source"], "shared_positive_fit_policy")

    def test_question_step_prefers_current_city_option_over_relocation_for_hybrid_location_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "This is a Hybrid position in San Francisco. Please select which applies:",
            "required": True,
            "fields": [
                {
                    "name": "question_5080473009",
                    "type": "multi_value_single_select",
                    "values": [
                        {"value": 1, "label": "You currently live in San Francisco"},
                        {"value": 2, "label": "You are looking to relocate to San Francisco"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Attentive",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["field_name"], "question_5080473009")
        self.assertEqual(step["option"], "You currently live in San Francisco")
        self.assertEqual(step["source"], "shared_positive_fit_policy")

    def test_question_step_answers_multi_select_relocation_with_current_city_yes_option(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Are you open to relocating for this position?",
            "required": True,
            "fields": [
                {
                    "name": "question_relocate_multi[]",
                    "type": "multi_value_multi_select",
                    "values": [
                        {"value": 1, "label": "No"},
                        {"value": 2, "label": "Yes - New York"},
                        {"value": 3, "label": "Yes - San Francisco"},
                        {"value": 4, "label": "Yes - Seattle"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Headway",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["field_name"], "question_relocate_multi[]")
        self.assertEqual(step["kind"], "checkbox")
        self.assertEqual(step["option"], "Yes - San Francisco")
        self.assertEqual(step["source"], "shared_positive_fit_policy")

    def test_question_step_answers_experience_confirmation_with_shared_positive_fit_policy(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Do you have experience building and scaling internal platform products within the fintech industry?",
            "required": True,
            "fields": [
                {
                    "name": "question_15662463004",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Mercury",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["field_name"], "question_15662463004")
        self.assertEqual(step["option"], "Yes")
        self.assertEqual(step["source"], "shared_positive_fit_policy")

    def test_question_step_answers_skill_confirmation_with_master_resume_support(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Are you proficient in SQL?",
            "required": True,
            "fields": [
                {
                    "name": "question_sql_proficiency",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="SmarterDx",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["field_name"], "question_sql_proficiency")
        self.assertEqual(step["option"], "Yes")
        self.assertEqual(step["source"], "master_resume.md")

    def test_question_step_answers_skill_years_experience_with_shared_resume_policy(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "How many years of activation, onboarding and growth experience do you have?",
            "required": True,
            "fields": [
                {
                    "name": "question_growth_years",
                    "type": "multi_value_single_select",
                    "values": [
                        {"value": 1, "label": "0-2 years"},
                        {"value": 2, "label": "3-5 years"},
                        {"value": 3, "label": "6-8 years"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Twin Health",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["field_name"], "question_growth_years")
        self.assertEqual(step["option"], "6-8 years")
        self.assertEqual(step["source"], "master_resume.md")

    def test_question_step_answers_skill_years_experience_with_plus_range_option(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "How many years of experience do you have in Product Manager roles?",
            "description": "(only Product Manager, Sr. Product Manager or Principal Product Manager roles)",
            "required": True,
            "fields": [
                {
                    "name": "question_pm_roles_years",
                    "type": "multi_value_single_select",
                    "values": [
                        {"value": 1, "label": "Less than 3 years"},
                        {"value": 2, "label": "3-4 years"},
                        {"value": 3, "label": "5-7 years"},
                        {"value": 4, "label": "7+ years"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="BetterHelp",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["field_name"], "question_pm_roles_years")
        self.assertEqual(step["option"], "7+ years")
        self.assertEqual(step["source"], "application_profile.md")

    def test_question_step_plans_required_skill_years_experience_when_options_do_not_match(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "How many years of  proven experience do you have managing AI support tools or LLM-based platforms (e.g., Decagon, Happy Robot, Intercom Fin, or similar)?",
            "required": True,
            "fields": [
                {
                    "name": "question_ai_support_years",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "5+ years"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Samsara",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["status"], "planned")
        self.assertEqual(step["source"], "master_resume.md")
        self.assertIn("shared answer '2'", step["report_value"])

    def test_resolved_greenhouse_source_url_prefers_direct_source_url_over_wrapper_board_url(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        resolved = autofill._resolved_greenhouse_source_url(
            {
                "source_url": "https://job-boards.greenhouse.io/jamasoftware/jobs/7729121",
                "jd_source": "https://www.jamasoftware.com/company/careers/posting/7729121?gh_jid=7729121&utm_source=trueup.io",
                "board_url": "https://www.jamasoftware.com/company/careers/posting/",
                "jd_source_resolved": "https://www.jamasoftware.com/company/careers/posting/",
            }
        )

        self.assertEqual(resolved, "https://job-boards.greenhouse.io/jamasoftware/jobs/7729121")

    def test_question_step_answers_positive_fit_skill_confirmation_affirmatively(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Are you proficient in Rust?",
            "required": True,
            "fields": [
                {
                    "name": "question_rust_proficiency",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="SmarterDx",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["kind"], "combobox")
        self.assertEqual(step["option"], "Yes")
        self.assertEqual(step["search"], "Yes")
        self.assertEqual(step["source"], "shared_positive_fit_policy")

    def test_skip_runtime_fill_step_is_not_eligible_for_deterministic_refill(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        self.assertFalse(
            autofill._should_refill_visible_deterministic_step(
                {
                    "kind": "combobox",
                    "source": "master_resume.md",
                    "skip_runtime_fill": True,
                }
            )
        )

    def test_question_step_answers_background_check_consent_deterministically(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "As part of our hiring process, all final candidates will undergo and must successfully pass a background check. Are you willing to complete a background check if selected for this role?",
            "required": True,
            "fields": [
                {
                    "name": "question_background_check_consent",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="SmarterDx",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["field_name"], "question_background_check_consent")
        self.assertEqual(step["option"], "Yes")
        self.assertEqual(step["source"], "deterministic")

    def test_question_step_answers_temp_role_commitment_with_shared_positive_fit_policy(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "This is a US-based, remote Temp role that runs for about 6 months with an expected schedule of 20-40 hours per week. Are you comfortable committing to these details?",
            "required": True,
            "fields": [
                {
                    "name": "question_temp_commitment",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Headspace",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["field_name"], "question_temp_commitment")
        self.assertEqual(step["option"], "Yes")
        self.assertEqual(step["source"], "shared_positive_fit_policy")

    def test_question_step_answers_unsupported_clinical_product_experience_as_no(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Do you have prior Clinical Product experience (e.g. translating clinical requirements for product development)?",
            "required": True,
            "fields": [
                {
                    "name": "question_clinical_product",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Headspace",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["field_name"], "question_clinical_product")
        self.assertEqual(step["option"], "No")
        self.assertEqual(step["source"], "unsupported_specialist_domain_experience")

    def test_question_step_uses_primary_employer_for_current_company_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Current Company",
            "description": "Please share the list of companies that you've worked at.",
            "required": True,
            "fields": [
                {
                    "name": "question_12956443008",
                    "type": "input_text",
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Quince",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["value"], "Moody's Analytics")
        self.assertEqual(step["source"], "master_resume.md")

    def test_question_step_matches_pronoun_variants_from_profile(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Pronouns",
            "required": False,
            "fields": [
                {
                    "name": "question_pronouns",
                    "type": "multi_value_single_select",
                    "values": [
                        {"value": 1, "label": "He/Him"},
                        {"value": 2, "label": "She/Her"},
                        {"value": 3, "label": "They/Them"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Klaviyo",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertEqual(step["option"], "He/Him")
        self.assertEqual(step["source"], "application_profile.md")
        self.assertTrue(step["blocks_draft_completion"])
        self.assertEqual(step["blocker_kind"], "visible_self_id")

    def test_match_discovered_demographic_question_prefers_pronouns_over_gender(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )

        pronouns_match = autofill._match_greenhouse_discovered_demographic_question(
            "What gender pronouns do you prefer?",
            application_profile,
        )
        gender_match = autofill._match_greenhouse_discovered_demographic_question(
            "What is your gender?",
            application_profile,
        )

        self.assertEqual(pronouns_match, ("pronouns", "He / Him / His"))
        self.assertEqual(gender_match, ("gender", "Male"))

    def test_match_discovered_demographic_question_handles_gender_veteran_and_age_variants(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )

        gender_match = autofill._match_greenhouse_discovered_demographic_question(
            "What gender do you identify as?",
            application_profile,
        )
        veteran_match = autofill._match_greenhouse_discovered_demographic_question(
            "Are you a protected veteran?",
            application_profile,
        )
        age_match = autofill._match_greenhouse_discovered_demographic_question(
            "What is your age?",
            application_profile,
        )

        self.assertEqual(gender_match, ("gender", "Male"))
        self.assertEqual(veteran_match, ("veteran_status", "I am not a protected veteran"))
        self.assertEqual(age_match, ("age_group", "35 - 44"))

    def test_match_discovered_demographic_question_handles_ethnicity_cc305_and_armed_forces_variants(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )

        ethnicities_match = autofill._match_greenhouse_discovered_demographic_question(
            "Please select up to 2 ethnicities that you most closely identify with.",
            application_profile,
        )
        ethnic_group_match = autofill._match_greenhouse_discovered_demographic_question(
            "Indicate Ethnic group:",
            application_profile,
        )
        disability_match = autofill._match_greenhouse_discovered_demographic_question(
            "Please review Form CC-305 at the link above before checking one of the boxes below.",
            application_profile,
        )
        veteran_match = autofill._match_greenhouse_discovered_demographic_question(
            "Do you identify as a veteran or someone with a background in the armed forces? (Note: This includes any armed forces globally.)",
            application_profile,
        )

        self.assertEqual(ethnicities_match, ("race", "Hispanic or Latino"))
        self.assertEqual(ethnic_group_match, ("race", "Hispanic or Latino"))
        self.assertEqual(disability_match, ("disability_status", "No, I do not have a disability and have not had one in the past"))
        self.assertEqual(veteran_match, ("veteran_status", "I am not a protected veteran"))

    def test_match_discovered_demographic_question_handles_chime_cisgender_transgender_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )

        match = autofill._match_greenhouse_discovered_demographic_question(
            "I identify as:",
            application_profile,
        )

        self.assertEqual(match, ("transgender_status", "No"))

    def test_mark_visible_self_id_greenhouse_step_marks_age_group_as_blocking(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        step = autofill._mark_visible_self_id_greenhouse_step(
            {
                "kind": "combobox",
                "field_name": "age_group",
                "label": "What is your age?",
                "value": "35 - 44",
                "status": "planned",
            }
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertTrue(step["blocks_draft_completion"])
        self.assertEqual(step["blocker_kind"], "visible_self_id")
        self.assertEqual(step["profile_field"], "age_range")

    def test_greenhouse_discovered_combobox_desired_values_preserve_single_select_answers_with_commas(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        disability_values = autofill._greenhouse_discovered_combobox_desired_values(
            "disability_status",
            "No, I do not have a disability and have not had one in the past",
        )
        race_values = autofill._greenhouse_discovered_combobox_desired_values(
            "race",
            "Hispanic, Latinx, or Spanish",
        )

        self.assertEqual(
            disability_values,
            ["No, I do not have a disability and have not had one in the past"],
        )
        self.assertEqual(race_values, ["Hispanic, Latinx, or Spanish"])

    def test_greenhouse_discovered_combobox_desired_values_split_true_multi_value_fields(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        self.assertEqual(
            autofill._greenhouse_discovered_combobox_desired_values(
                "languages_spoken",
                "English,Spanish,Mandarin",
            ),
            ["English", "Spanish", "Mandarin"],
        )

    def test_match_discovered_demographic_question_uses_truthful_work_auth_and_sponsorship_yes_no(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )

        work_auth_match = autofill._match_greenhouse_discovered_demographic_question(
            "Do you have unrestricted work authorization in the United States?",
            application_profile,
        )
        sponsorship_match = autofill._match_greenhouse_discovered_demographic_question(
            "Will you now, or in the future, require immigration sponsorship for continued employment in the United States?",
            application_profile,
        )
        mixed_sponsorship_match = autofill._match_greenhouse_discovered_demographic_question(
            "Would you, now or in the future, require immigration sponsorship for work authorization?",
            application_profile,
        )

        self.assertEqual(work_auth_match, ("work_authorization", "Yes"))
        self.assertEqual(sponsorship_match, ("sponsorship", "No"))
        self.assertEqual(mixed_sponsorship_match, ("sponsorship", "No"))

    def test_resolve_discovered_demographic_option_uses_descriptive_work_authorization_label(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )

        selected = autofill._resolve_discovered_demographic_option_text(
            field_name="work_authorization",
            desired="Yes",
            option_texts=[
                "I am authorized to work in the country due to my nationality",
                "I am authorized to work in the country based on a valid work permit and do not need a company to sponsor my visa",
                "I am authorized to work in the country based on a valid work permit which needs to be sponsored by the company I work for",
                "I am not authorized to work in the country and need visa support",
            ],
            application_profile=application_profile,
        )

        self.assertEqual(
            selected,
            "I am authorized to work in the country based on a valid work permit and do not need a company to sponsor my visa",
        )

    def test_resolve_discovered_demographic_option_maps_not_disabled_alias(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )

        selected = autofill._resolve_discovered_demographic_option_text(
            field_name="disability_status",
            desired="No, I do not have a disability and have not had one in the past",
            option_texts=[
                "Disabled",
                "Not disabled",
                "Prefer not to say",
            ],
            application_profile=application_profile,
        )

        self.assertEqual(selected, "Not disabled")

    def test_runtime_confirmation_step_from_discovered_group_marks_blank_work_auth_and_disability(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )

        work_authorization_step = autofill._greenhouse_runtime_confirmation_step_from_discovered_group(
            answer={
                "heading": "Can you, after employment, submit verification of your legal right to work in the United States?",
                "value": "",
                "kind": "combobox",
            },
            application_profile=application_profile,
            page_index=1,
        )
        disability_step = autofill._greenhouse_runtime_confirmation_step_from_discovered_group(
            answer={
                "heading": "Disability Status",
                "value": "",
                "kind": "combobox",
            },
            application_profile=application_profile,
            page_index=1,
        )

        self.assertIsNotNone(work_authorization_step)
        assert work_authorization_step is not None
        self.assertEqual(work_authorization_step["field_name"], "work_authorization")
        self.assertEqual(work_authorization_step["status"], "planned")
        self.assertTrue(work_authorization_step["blocks_draft_completion"])
        self.assertEqual(work_authorization_step["blocker_kind"], "visible_profile_field")
        self.assertEqual(work_authorization_step["profile_field"], "work_authorization")
        self.assertEqual(work_authorization_step["observed_value"], "")

        self.assertIsNotNone(disability_step)
        assert disability_step is not None
        self.assertEqual(disability_step["field_name"], "disability_status")
        self.assertEqual(disability_step["status"], "planned")
        self.assertTrue(disability_step["blocks_draft_completion"])
        self.assertEqual(disability_step["blocker_kind"], "visible_self_id")
        self.assertEqual(disability_step["profile_field"], "disability_status")
        self.assertEqual(disability_step["observed_value"], "")

    def test_greenhouse_live_value_matches_step_handles_text_and_combobox_values(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        self.assertTrue(
            autofill._greenhouse_live_value_matches_step(
                {
                    "field_name": "question_35502351002",
                    "kind": "text",
                    "value": "Corporate website",
                },
                "Corporate website",
            )
        )
        self.assertFalse(
            autofill._greenhouse_live_value_matches_step(
                {
                    "field_name": "question_35502351002",
                    "kind": "text",
                    "value": "Corporate website",
                },
                "",
            )
        )
        self.assertTrue(
            autofill._greenhouse_live_value_matches_step(
                {
                    "field_name": "work_authorization",
                    "kind": "combobox",
                    "option": "Yes",
                },
                "Yes",
            )
        )
        self.assertFalse(
            autofill._greenhouse_live_value_matches_step(
                {
                    "field_name": "work_authorization",
                    "kind": "combobox",
                    "option": "Yes",
                },
                "",
            )
        )

    def test_greenhouse_live_value_matches_step_accepts_expanded_candidate_location(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        self.assertTrue(
            autofill._greenhouse_live_value_matches_step(
                {
                    "field_name": "candidate_location",
                    "kind": "text",
                    "value": "San Francisco, CA",
                },
                "San Francisco, California, United States",
            )
        )

    def test_report_entry_matches_expected_value_accepts_expanded_candidate_location(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        self.assertTrue(
            autofill._report_entry_matches_expected_value(
                {
                    "field_name": "candidate_location",
                    "kind": "text",
                    "value": "San Francisco, CA",
                    "blocker_kind": "visible_profile_field",
                },
                {
                    "field_name": "candidate_location",
                    "kind": "text",
                    "value": "San Francisco, California, United States",
                },
            )
        )

    def test_runtime_confirmation_step_from_discovered_group_ignores_hidden_duplicates_and_keeps_text_values(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )

        hidden_step = autofill._greenhouse_runtime_confirmation_step_from_discovered_group(
            answer={
                "heading": "Can you, after employment, submit verification of your legal right to work in the United States?",
                "value": "",
                "kind": "combobox",
                "visible": False,
            },
            application_profile=application_profile,
            page_index=1,
        )
        heard_step = autofill._greenhouse_runtime_confirmation_step_from_discovered_group(
            answer={
                "heading": "How did you hear about this job?*",
                "value": "Corporate website",
                "kind": "text",
                "visible": True,
            },
            application_profile=application_profile,
            page_index=1,
        )

        self.assertIsNone(hidden_step)
        self.assertIsNotNone(heard_step)
        assert heard_step is not None
        self.assertEqual(heard_step["field_name"], "how_did_you_hear")
        self.assertEqual(heard_step["status"], "filled")
        self.assertEqual(heard_step["value"], "Corporate website")

    def test_greenhouse_discovered_group_key_distinguishes_duplicate_semantic_fields_by_control_id(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        primary_key = autofill._greenhouse_discovered_group_key(
            "work_authorization",
            control_id="question_35502353002",
            label="Can you, after employment, submit verification of your legal right to work in the United States*",
        )
        duplicate_wrapper_key = autofill._greenhouse_discovered_group_key(
            "work_authorization",
            control_id="question_35502353002",
            label="Can you, after employment, submit verification of your legal right to work in the United States*",
        )
        demographic_key = autofill._greenhouse_discovered_group_key(
            "work_authorization",
            control_id="4001635002",
            label="Can you, after employment, submit verification of your legal right to work in the United States?",
        )

        self.assertEqual(primary_key, duplicate_wrapper_key)
        self.assertNotEqual(primary_key, demographic_key)

    def test_cover_letter_step_is_treated_as_required_when_field_exists(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Cover Letter",
            "required": False,
            "fields": [{"name": "cover_letter_text", "type": "textarea"}],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="CoreWeave",
            cover_letter="Dear Hiring Team,\n\nThanks.\n",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertFalse(step["optional"])

    def test_cover_letter_step_prefers_file_upload_when_both_file_and_textarea_exist(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Cover Letter",
            "required": False,
            "fields": [
                {"name": "cover_letter_text", "type": "textarea"},
                {"name": "question_cover_letter", "type": "input_file"},
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="CoreWeave",
            cover_letter="Dear Hiring Team,\n\nThanks.\n",
            cover_letter_file=PROJECT_ROOT / "cover-letter.pdf",
            generated_answers={},
        )

        self.assertEqual(step["kind"], "file")
        self.assertEqual(step["field_name"], "question_cover_letter")
        self.assertEqual(step["source"], "existing_cover_letter_asset")

    def test_submission_snapshot_prioritizes_security_code_flow_over_validation_error(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        outcome = autofill._classify_submission_snapshot(
            {
                "url": "https://boards.greenhouse.io/embed/job_app?for=coreweave&token=4638816006",
                "page_text": "Enter the verification code sent to jerrisonli@gmail.com to confirm you are not a robot.",
                "form_visible": True,
                "security_code_visible": True,
                "errors": ["Please select", "is required"],
                "invalid_fields": ["Security Code"],
            }
        )

        self.assertEqual(outcome, {"status": "security_code_required"})

    def test_parse_master_resume_contact_line(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        sample = """
        JERRISON LI
        Principal Product Manager
        San Francisco, CA  |  jerrisonli@gmail.com  |  510-613-5192  |  linkedin.com/in/jerrison/  |  jerrisonli.com
        ## MOODY'S ANALYTICS — Associate Director, Product Management
        Work Authorization: United States Citizen
        """
        profile = autofill._parse_master_resume(sample)
        self.assertEqual(profile.first_name, "Jerrison")
        self.assertEqual(profile.last_name, "Li")
        self.assertEqual(profile.linkedin, "https://linkedin.com/in/jerrison/")
        self.assertTrue(profile.work_authorized)
        self.assertIn("moody's analytics", profile.employers)

    def test_parse_master_resume_contact_line_without_top_level_location(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        sample = """
        # Master Resume
        JERRISON LI
        Principal Product Manager  |  AI/ML & Enterprise B2B
        jerrisonli@gmail.com  |  510-613-5192  |  linkedin.com/in/jerrison/  |  jerrisonli.com
        ## MOODY'S ANALYTICS — Associate Director, Product Management
        San Francisco, CA | 2024–Present
        Work Authorization: United States Citizen
        """
        profile = autofill._parse_master_resume(sample)
        self.assertEqual(profile.location, "San Francisco, CA")
        self.assertEqual(profile.email, "jerrisonli@gmail.com")
        self.assertEqual(profile.phone, "510-613-5192")

    def test_parse_master_resume_contact_line_four_parts_with_location(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        sample = """
        JERRISON LI
        Principal Product Manager  |  AI/ML & Enterprise B2B
        San Francisco, CA  |  jerrisonli@gmail.com  |  510-613-5192  |  jerrisonli.com
        ## MOODY'S ANALYTICS — Associate Director, Product Management
        Work Authorization: United States Citizen
        """
        profile = autofill._parse_master_resume(sample)
        self.assertEqual(profile.location, "San Francisco, CA")
        self.assertEqual(profile.email, "jerrisonli@gmail.com")
        self.assertEqual(profile.phone, "510-613-5192")
        self.assertEqual(profile.website, "https://jerrisonli.com")
        self.assertFalse(profile.linkedin)

    def test_build_company_specific_answer_prefers_company_paragraphs(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        paragraphs = [
            "At Moody's, I shipped workflow automation across insurance products.",
            "When Figma launched Draw, it signaled a deep commitment to craft. That resonated with me.",
            "What draws me specifically to Figma is its belief that design quality matters in the AI era.",
        ]
        answer = autofill._build_company_specific_answer("Figma", paragraphs)
        self.assertIn("Figma launched Draw", answer)
        self.assertIn("design quality matters", answer)
        self.assertNotIn("Moody's", answer)

    def test_parse_application_profile(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        sample = """
        # Application Profile
        - Country: United States
        - Location: San Francisco, CA
        - Work Authorization Statement: I am always authorized to work in the United States unconditionally.
        - Authorized to Work Unconditionally: Yes
        - Require Sponsorship Now: No
        - Require Sponsorship in Future: No
        - Meets Minimum Years of Experience Requirement: Yes
        - Live In Job Location: Yes
        - Willing to Relocate: Yes
        - Comfortable Working On Site: Yes
        - Compensation Expectations: I'm open and flexible on compensation.
        - Undergraduate GPA: 3.8/4.0
        - Sponsorship Answer: No, I do not require sponsorship now or in the future.
        - Gender: Male
        - Transgender Status: No
        - Race or Ethnicity: Hispanic or Latino
        - Sexual Orientation: Straight / Heterosexual
        - Veteran Status: I am not a protected veteran
        - Disability Status: No, I do not have a disability and have not had one in the past
        - Pronouns: He / Him / His
        - How Did You Hear About Us: Corporate website
        - Verification Code Email: jerrisonli@gmail.com
        - LinkedIn: https://www.linkedin.com/in/jerrison/
        - GitHub: https://github.com/jerrison
        - Website: https://jerrisonli.com
        """
        profile = autofill._parse_application_profile(sample)
        self.assertEqual(profile.country, "United States")
        self.assertEqual(profile.location, "San Francisco, CA")
        self.assertTrue(profile.authorized_to_work_unconditionally)
        self.assertFalse(profile.require_sponsorship_now)
        self.assertFalse(profile.require_sponsorship_future)
        self.assertTrue(profile.minimum_years_experience)
        self.assertTrue(profile.lives_in_job_location)
        self.assertTrue(profile.willing_to_relocate)
        self.assertTrue(profile.comfortable_working_on_site)
        self.assertEqual(profile.compensation_expectations, "I'm open and flexible on compensation.")
        self.assertEqual(profile.undergraduate_gpa, "3.8/4.0")
        self.assertEqual(profile.gender, "Male")
        self.assertEqual(profile.transgender_status, "No")
        self.assertEqual(profile.race_or_ethnicity, "Hispanic or Latino")
        self.assertEqual(profile.sexual_orientation, "Straight / Heterosexual")
        self.assertEqual(profile.veteran_status, "I am not a protected veteran")
        self.assertEqual(
            profile.disability_status,
            "No, I do not have a disability and have not had one in the past",
        )
        self.assertEqual(profile.pronouns, "He / Him / His")
        self.assertEqual(profile.how_did_you_hear, "Corporate website")
        self.assertEqual(profile.verification_code_email, "jerrisonli@gmail.com")
        self.assertEqual(profile.linkedin, "https://www.linkedin.com/in/jerrison/")
        self.assertEqual(profile.github, "https://github.com/jerrison")
        self.assertEqual(profile.website, "https://jerrisonli.com")

    def test_parse_application_profile_exposes_education_entries_for_shared_policy_checks(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        sample = """
        # Application Profile
        - Country: United States
        - Location: San Francisco, CA
        - Work Authorization Statement: I am always authorized to work in the United States unconditionally.
        - Authorized to Work Unconditionally: Yes
        - Require Sponsorship Now: No
        - Require Sponsorship in Future: No
        - Sponsorship Answer: No
        - Gender: Male
        - Race or Ethnicity: Hispanic or Latino
        - Veteran Status: I am not a protected veteran
        - Disability Status: No, I do not have a disability and have not had one in the past
        - Sexual Orientation: Straight / Heterosexual

        ## Education
        - The Wharton School, Master of Business Administration
        - Florida State University, Bachelor of Science in Actuarial Science
        """

        profile = autofill._parse_application_profile(sample)

        self.assertEqual(
            profile.education_entries,
            [
                "The Wharton School, Master of Business Administration",
                "Florida State University, Bachelor of Science in Actuarial Science",
            ],
        )

    def test_parse_application_profile_keeps_available_cities(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        sample = """
        # Application Profile
        - Country: United States
        - Location: San Francisco, CA
        - Work Authorization Statement: I am always authorized to work in the United States unconditionally.
        - Authorized to Work Unconditionally: Yes
        - Require Sponsorship Now: No
        - Require Sponsorship in Future: No
        - Sponsorship Answer: No
        - Available Cities: San Francisco, Seattle, New York
        - Gender: Male
        - Race or Ethnicity: Hispanic or Latino
        - Veteran Status: I am not a protected veteran
        - Disability Status: No, I do not have a disability and have not had one in the past
        - Sexual Orientation: Straight / Heterosexual
        """

        profile = autofill._parse_application_profile(sample)

        self.assertEqual(profile.available_cities, ["San Francisco", "Seattle", "New York"])

    def test_selector_for_field_uses_attribute_selector_for_bracketed_names(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        selector = autofill._selector_for_field(
            "job_application[demographic_answers][][answer_options][][answer_option_id]"
        )

        self.assertEqual(
            selector,
            '[id="job_application[demographic_answers][][answer_options][][answer_option_id]"], '
            '[name="job_application[demographic_answers][][answer_options][][answer_option_id]"]',
        )

    def test_selector_for_field_maps_candidate_location_variants(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        self.assertEqual(autofill._selector_for_field("candidate_location"), "#candidate-location")
        self.assertEqual(autofill._selector_for_field("candidate-location"), "#candidate-location")

    def test_normalized_option_match_candidates_include_gender_and_decline_aliases(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        gender_candidates = autofill._normalized_option_match_candidates("gender", "Male")
        decline_candidates = autofill._normalized_option_match_candidates("pronouns", "I don't wish to answer")

        self.assertIn("man", gender_candidates)
        self.assertIn("i do not want to answer", decline_candidates)
        self.assertIn("decline to self identify", decline_candidates)

    def test_normalized_option_match_candidates_include_cisgender_gender_aliases(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        candidates = autofill._normalized_option_match_candidates("gender_identity", "Cisgender Male/Man")

        self.assertIn("cisgender man", candidates)
        self.assertIn("man", candidates)
        self.assertIn("masculine", candidates)

    def test_normalized_option_match_candidates_include_transgender_no_aliases(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        candidates = autofill._normalized_option_match_candidates("transgender_status", "No")

        self.assertIn("not transgender", candidates)
        self.assertIn("do not identify as transgender", candidates)
        self.assertIn("cisgender", candidates)

    def test_normalized_option_match_candidates_include_pronoun_variants(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        candidates = autofill._normalized_option_match_candidates("pronouns", "He / Him / His")

        self.assertIn("he him", candidates)
        self.assertIn("he him his", candidates)

    def test_school_option_score_rejects_partial_school_name_collisions(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        self.assertEqual(
            autofill._school_option_score("University of Pennsylvania", "University of Pennsylvania"),
            (3, 0),
        )
        self.assertEqual(
            autofill._school_option_score(
                "University of Pennsylvania",
                "The Wharton School, University of Pennsylvania",
            ),
            (2, -3),
        )
        self.assertIsNone(
            autofill._school_option_score(
                "University of Pennsylvania",
                "Bloomsburg University of Pennsylvania",
            )
        )

    def test_normalized_option_match_candidates_cover_hover_demographic_variants(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        race_candidates = autofill._normalized_option_match_candidates("race", "Hispanic or Latino")
        orientation_candidates = autofill._normalized_option_match_candidates(
            "sexual_orientation", "Straight / Heterosexual"
        )
        veteran_candidates = autofill._normalized_option_match_candidates(
            "veteran_status", "I am not a protected veteran"
        )
        disability_candidates = autofill._normalized_option_match_candidates(
            "disability_status", "No, I do not have a disability and have not had one in the past"
        )

        self.assertIn("hispanic latinx or of spanish origin", race_candidates)
        self.assertIn("heterosexual", orientation_candidates)
        self.assertIn("i have never served in the military", veteran_candidates)
        self.assertIn("i identify as a non protected veteran", veteran_candidates)
        self.assertIn("no i am not a veteran or active member", veteran_candidates)
        self.assertIn("no", disability_candidates)
        self.assertIn("i do not identify as having a disability", disability_candidates)
        self.assertIn("i do not identify as someone with a disability", disability_candidates)

    def test_greenhouse_combobox_value_matches_expected_rejects_unconfirmed_or_wrong_values(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        self.assertFalse(autofill._greenhouse_combobox_value_matches_expected("question_work_auth", "Yes", "No"))
        self.assertFalse(
            autofill._greenhouse_combobox_value_matches_expected(
                "disability_status",
                "I do not have a disability and have not had one in the past",
                "Select...",
            )
        )
        self.assertTrue(autofill._greenhouse_combobox_value_matches_expected("question_work_auth", "Yes", "Yes"))
        self.assertTrue(
            autofill._greenhouse_combobox_value_matches_expected(
                "disability_status",
                "No, I do not have a disability and have not had one in the past",
                "I do not identify as having a disability",
            )
        )
        self.assertTrue(
            autofill._greenhouse_combobox_value_matches_expected(
                "disability_status",
                "No, I do not have a disability and have not had one in the past",
                "I do not identify as someone with a disability",
            )
        )
        self.assertFalse(autofill._greenhouse_combobox_value_matches_expected("country", "United States", "+1"))

    def test_confirmed_combobox_selection_requires_visible_match(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        self.assertIsNone(autofill._confirmed_combobox_selection_value("gender", "Male", ""))
        self.assertIsNone(autofill._confirmed_combobox_selection_value("gender", "Male", "Female"))
        self.assertEqual(autofill._confirmed_combobox_selection_value("gender", "Male", "Male"), "Male")

    def test_discovered_combobox_can_skip_open_when_single_select_already_matches(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        self.assertTrue(
            autofill._greenhouse_discovered_combobox_can_skip_open(
                "gender",
                ["Male"],
                "Male",
            )
        )
        self.assertTrue(
            autofill._greenhouse_discovered_combobox_can_skip_open(
                "disability_status",
                ["No, I do not have a disability and have not had one in the past"],
                "I do not identify as having a disability",
            )
        )

    def test_discovered_combobox_cannot_skip_open_for_mismatch_or_multi_value(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        self.assertFalse(
            autofill._greenhouse_discovered_combobox_can_skip_open(
                "gender",
                ["Male"],
                "Female",
            )
        )
        self.assertFalse(
            autofill._greenhouse_discovered_combobox_can_skip_open(
                "languages_spoken",
                ["English", "Spanish"],
                "English",
            )
        )

    def test_write_or_clear_json_artifact_removes_stale_file_when_payload_is_none(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "greenhouse_unknown_questions.json"
            path.write_text("stale", encoding="utf-8")

            autofill._write_or_clear_json_artifact(path, None)

            self.assertFalse(path.exists())

    def test_build_steps_uses_application_profile_for_sponsorship_and_eeoc(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        master_resume = """
        JERRISON LI
        Principal Product Manager
        San Francisco, CA  |  jerrisonli@gmail.com  |  510-613-5192  |  linkedin.com/in/jerrison/  |  jerrisonli.com
        ## MOODY'S ANALYTICS — Associate Director, Product Management
        Work Authorization: United States Citizen
        """
        application_profile_text = """
        - Country: United States
        - Location: San Francisco, CA
        - Work Authorization Statement: I am always authorized to work in the United States unconditionally.
        - Authorized to Work Unconditionally: Yes
        - Require Sponsorship Now: No
        - Require Sponsorship in Future: No
        - Meets Minimum Years of Experience Requirement: Yes
        - Live In Job Location: Yes
        - Willing to Relocate: Yes
        - Comfortable Working On Site: Yes
        - Sponsorship Answer: No, I do not require sponsorship now or in the future. I am always authorized to work in the United States unconditionally.
        - Gender: Male
        - Race or Ethnicity: Hispanic or Latino
        - Sexual Orientation: Straight / Heterosexual
        - Veteran Status: I am not a protected veteran
        - Disability Status: No, I do not have a disability and have not had one in the past
        - Pronouns:
        - How Did You Hear About Us: Corporate website
        - Verification Code Email: jerrisonli@gmail.com
        - LinkedIn: https://www.linkedin.com/in/jerrison/
        - GitHub: https://github.com/jerrison
        - Website: https://jerrisonli.com
        """
        candidate_profile = autofill._parse_master_resume(master_resume)
        application_profile = autofill._parse_application_profile(application_profile_text)
        job_post = {
            "questions": [
                {
                    "required": False,
                    "label": "LinkedIn Profile",
                    "fields": [{"name": "question_linkedin", "type": "input_text"}],
                },
                {
                    "required": False,
                    "label": "GitHub Profile",
                    "fields": [{"name": "question_github", "type": "input_text"}],
                },
                {"required": False, "label": "Website", "fields": [{"name": "question_website", "type": "input_text"}]},
                {
                    "required": False,
                    "label": "How did you hear about us?",
                    "fields": [{"name": "question_source", "type": "input_text"}],
                },
                {
                    "required": False,
                    "label": "Cover Letter",
                    "fields": [{"name": "question_cover_letter", "type": "input_file"}],
                },
                {
                    "required": True,
                    "label": "Why do you want to join Figma?",
                    "description": "<p>Please share 3-4 sentences on why you want to join Figma</p>",
                    "fields": [{"name": "question_why", "type": "textarea"}],
                },
                {
                    "required": False,
                    "label": "Additional Information",
                    "description": "<p>Add a cover letter or anything else you'd like to share</p>",
                    "fields": [{"name": "question_additional", "type": "textarea"}],
                },
                {
                    "required": True,
                    "label": "Candidate Location",
                    "fields": [{"name": "candidate_location", "type": "input_text"}],
                },
                {
                    "required": True,
                    "label": "Are you currently based in any of these countries? Please note these are the only countries where we are accepting applications",
                    "fields": [
                        {
                            "name": "question_country_gate",
                            "type": "multi_value_single_select",
                            "values": [
                                {"label": "United States", "value": "us"},
                                {"label": "Canada", "value": "ca"},
                                {"label": "Other", "value": "other"},
                            ],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": "Do you currently live in the location for this role?",
                    "fields": [
                        {
                            "name": "question_live_in_location",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": "Are you willing to relocate for this role?",
                    "fields": [
                        {
                            "name": "question_relocate",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": "Are you comfortable working on site for this role?",
                    "fields": [
                        {
                            "name": "question_on_site",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": (
                        "Commute & Office Proximity: Are you currently located within commuting distance "
                        "to either of our offices in San Francisco or New York City?"
                    ),
                    "fields": [
                        {
                            "name": "job_application_answers_attributes_1_answer_selected_options_attributes_1_question_option_id",
                            "type": "multi_value_single_select",
                            "values": [
                                {"label": "Yes, San Francisco", "value": "sf"},
                                {"label": "Yes, New York City", "value": "nyc"},
                                {"label": "No, but I am planning to relocate", "value": "relocate"},
                                {"label": "No (I understand this may disqualify me from this role)", "value": "no"},
                            ],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": (
                        "In-Office Availability: This role requires being in the office at least 3 days per week "
                        "(Monday-Friday) in either our San Francisco or New York City location. "
                        "Are you able to meet this requirement?"
                    ),
                    "fields": [
                        {
                            "name": "job_application_answers_attributes_2_answer_selected_options_attributes_2_question_option_id",
                            "type": "multi_value_single_select",
                            "values": [
                                {"label": "Yes", "value": "yes"},
                                {"label": "No (I understand this may disqualify me from this role)", "value": "no"},
                            ],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": "Your authorization to work in the country where you live. Please choose the option that describes your work authorization.",
                    "fields": [
                        {
                            "name": "question_descriptive_authorization",
                            "type": "multi_value_single_select",
                            "values": [
                                {
                                    "label": "I am authorized to work in the country due to my nationality",
                                    "value": "nationality",
                                },
                                {
                                    "label": "I am authorized to work in the country based on a valid work permit and do not need a company to sponsor my visa",
                                    "value": "permit_no_sponsor",
                                },
                                {
                                    "label": "I am authorized to work in the country based on a valid work permit which needs to be sponsored by the company I work for",
                                    "value": "permit_needs_sponsor",
                                },
                                {
                                    "label": "I am not authorized to work in the country and need visa support",
                                    "value": "need_support",
                                },
                            ],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": "Are you authorized to work in the country for which you applied?",
                    "fields": [
                        {
                            "name": "question_authorized",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": "Will you now or in the future require sponsorship?",
                    "fields": [
                        {
                            "name": "question_sponsorship",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": "By submitting my application, I acknowledge that I have read and understand the Job Applicant Privacy Notice.",
                    "fields": [
                        {
                            "name": "question_acknowledge",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Acknowledge/Confirm", "value": "ack"}],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": "Please double-check all the information provided above. Ensuring accuracy is crucial, as any errors or omissions may impact the review of your application.",
                    "fields": [
                        {
                            "name": "question_review_confirm",
                            "type": "multi_value_single_select",
                            "values": [
                                {
                                    "label": "I have reviewed and confirmed that all the information provided is accurate and complete.",
                                    "value": "confirm",
                                }
                            ],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": "Do you have the minimum years of experience required for this role?",
                    "fields": [
                        {
                            "name": "question_minimum_experience",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": "Have you ever worked for Figma before?",
                    "fields": [
                        {
                            "name": "question_prior_employment",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": "Are you open to working 3 days from one of our office hubs in NYC, NJ, CA, WA?",
                    "fields": [
                        {
                            "name": "question_office_hub",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": "Are you a former Figma employee?",
                    "fields": [
                        {
                            "name": "question_former_employee",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": "Are you now or have you ever been employed by Figma?",
                    "fields": [
                        {
                            "name": "question_employed_by_company",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}],
                        }
                    ],
                },
                {
                    "required": True,
                    "label": "Please indicate whether you are a U.S. person.",
                    "fields": [
                        {
                            "name": "question_us_person",
                            "type": "multi_value_single_select",
                            "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}],
                        }
                    ],
                },
            ],
            "eeoc_sections": [
                {
                    "description": "<p>Voluntary Self-Identification</p>",
                    "questions": [
                        {
                            "required": False,
                            "label": "Gender",
                            "fields": [
                                {
                                    "name": "gender",
                                    "type": "multi_value_single_select",
                                    "values": [{"label": "Male", "value": "1"}, {"label": "Female", "value": "2"}],
                                }
                            ],
                        },
                        {
                            "required": False,
                            "label": "Race",
                            "fields": [
                                {
                                    "name": "race",
                                    "type": "multi_value_single_select",
                                    "values": [
                                        {"label": "Hispanic or Latino", "value": "4"},
                                        {"label": "White", "value": "5"},
                                    ],
                                }
                            ],
                        },
                        {
                            "required": False,
                            "label": "Veteran Status",
                            "fields": [
                                {
                                    "name": "veteran_status",
                                    "type": "multi_value_single_select",
                                    "values": [
                                        {"label": "I am not a protected veteran", "value": "1"},
                                        {"label": "I don't wish to answer", "value": "2"},
                                    ],
                                }
                            ],
                        },
                        {
                            "required": False,
                            "label": "Disability Status",
                            "fields": [
                                {
                                    "name": "disability_status",
                                    "type": "multi_value_single_select",
                                    "values": [
                                        {
                                            "label": "No, I do not have a disability and have not had one in the past",
                                            "value": "2",
                                        }
                                    ],
                                }
                            ],
                        },
                        {
                            "required": False,
                            "label": "Sexual Orientation",
                            "fields": [
                                {
                                    "name": "sexual_orientation",
                                    "type": "multi_value_single_select",
                                    "values": [
                                        {"label": "Straight / Heterosexual", "value": "1"},
                                        {"label": "I don't wish to answer", "value": "2"},
                                    ],
                                }
                            ],
                        },
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir)
            (out_dir / "cover_letter_text.txt").write_text(
                "Dear Hiring Team,\n\nFigma is where I want to build next.\n\nBest regards,\nJerrison Li\n",
                encoding="utf-8",
            )
            (out_dir / "Jerrison Li Cover Letter - Figma.pdf").write_bytes(b"%PDF-1.4")
            (out_dir / "Jerrison Li Resume - Figma.pdf").write_bytes(b"%PDF-1.4")

            steps = autofill._build_steps(
                job_post,
                {"company_proper": "Figma"},
                candidate_profile,
                application_profile,
                out_dir,
                generated_answers={"question_why": "Fresh tailored answer."},
            )

        steps_by_field = {step["field_name"]: step for step in steps}
        self.assertEqual(steps_by_field["question_linkedin"]["value"], "https://www.linkedin.com/in/jerrison/")
        self.assertEqual(steps_by_field["question_linkedin"]["source"], "application_profile.md")
        self.assertEqual(steps_by_field["question_github"]["value"], "https://github.com/jerrison")
        self.assertEqual(steps_by_field["question_github"]["source"], "application_profile.md")
        self.assertEqual(steps_by_field["question_website"]["value"], "https://jerrisonli.com")
        self.assertEqual(steps_by_field["question_website"]["source"], "application_profile.md")
        self.assertEqual(steps_by_field["question_source"]["value"], "Corporate website")
        self.assertEqual(steps_by_field["question_source"]["source"], "application_profile.md")
        self.assertTrue(
            steps_by_field["question_cover_letter"]["file_path"].endswith("Jerrison Li Cover Letter - Figma.pdf")
        )
        self.assertEqual(steps_by_field["question_why"]["value"], "Fresh tailored answer.")
        self.assertIn("Dear Hiring Team", steps_by_field["question_additional"]["value"])
        self.assertEqual(steps_by_field["candidate_location"]["value"], "San Francisco, CA")
        self.assertEqual(steps_by_field["candidate_location"]["source"], "application_profile.md")
        self.assertEqual(steps_by_field["question_country_gate"]["option"], "United States")
        self.assertEqual(steps_by_field["question_live_in_location"]["option"], "Yes")
        self.assertEqual(steps_by_field["question_relocate"]["option"], "Yes")
        self.assertEqual(steps_by_field["question_on_site"]["option"], "Yes")
        self.assertEqual(
            steps_by_field[
                "job_application_answers_attributes_1_answer_selected_options_attributes_1_question_option_id"
            ]["option"],
            "Yes, San Francisco",
        )
        self.assertEqual(
            steps_by_field[
                "job_application_answers_attributes_2_answer_selected_options_attributes_2_question_option_id"
            ]["option"],
            "Yes",
        )
        self.assertEqual(
            steps_by_field["question_descriptive_authorization"]["option"],
            "I am authorized to work in the country based on a valid work permit and do not need a company to sponsor my visa",
        )
        self.assertEqual(steps_by_field["question_authorized"]["option"], "Yes")
        self.assertEqual(steps_by_field["question_sponsorship"]["option"], "No")
        self.assertEqual(steps_by_field["question_acknowledge"]["option"], "Acknowledge/Confirm")
        self.assertEqual(
            steps_by_field["question_review_confirm"]["option"],
            "I have reviewed and confirmed that all the information provided is accurate and complete.",
        )
        self.assertEqual(steps_by_field["question_minimum_experience"]["option"], "Yes")
        self.assertEqual(steps_by_field["question_prior_employment"]["option"], "No")
        self.assertEqual(steps_by_field["question_office_hub"]["option"], "Yes")
        self.assertEqual(steps_by_field["question_former_employee"]["option"], "No")
        self.assertEqual(steps_by_field["question_employed_by_company"]["option"], "No")
        self.assertEqual(steps_by_field["question_us_person"]["option"], "Yes")
        self.assertEqual(steps_by_field["gender"]["option"], "Male")
        self.assertEqual(steps_by_field["race"]["option"], "Hispanic or Latino")
        self.assertEqual(steps_by_field["sexual_orientation"]["option"], "Straight / Heterosexual")
        self.assertEqual(steps_by_field["veteran_status"]["option"], "I am not a protected veteran")
        self.assertEqual(
            steps_by_field["disability_status"]["option"],
            "No, I do not have a disability and have not had one in the past",
        )

    def test_question_step_prefers_sponsorship_for_mixed_work_authorization_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        master_resume = """
        JERRISON LI
        Principal Product Manager
        San Francisco, CA  |  jerrisonli@gmail.com  |  510-613-5192  |  linkedin.com/in/jerrison/  |  jerrisonli.com
        ## MOODY'S ANALYTICS — Associate Director, Product Management
        Work Authorization: United States Citizen
        """
        application_profile_text = """
        - Country: United States
        - Location: San Francisco, CA
        - Work Authorization Statement: I am always authorized to work in the United States unconditionally.
        - Authorized to Work Unconditionally: Yes
        - Require Sponsorship Now: No
        - Require Sponsorship in Future: No
        - Sponsorship Answer: No, I do not require sponsorship now or in the future. I am always authorized to work in the United States unconditionally.
        - Gender: Male
        - Race or Ethnicity: Hispanic or Latino
        - Sexual Orientation: Straight / Heterosexual
        - Veteran Status: I am not a protected veteran
        - Disability Status: No, I do not have a disability and have not had one in the past
        """
        profile = autofill._parse_master_resume(master_resume)
        application_profile = autofill._parse_application_profile(application_profile_text)
        question = {
            "required": True,
            "label": "Would you someday require immigration sponsorship for work authorization?",
            "fields": [
                {
                    "name": "question_future_sponsorship",
                    "type": "multi_value_single_select",
                    "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Klaviyo",
            cover_letter="",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["option"], "No")
        self.assertEqual(step["source"], "application_profile.md")

    def test_question_step_uses_combined_truthful_answer_for_mixed_work_authorization_text_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        master_resume = """
        JERRISON LI
        Principal Product Manager
        San Francisco, CA  |  jerrisonli@gmail.com  |  510-613-5192  |  linkedin.com/in/jerrison/  |  jerrisonli.com
        ## MOODY'S ANALYTICS — Associate Director, Product Management
        Work Authorization: United States Citizen
        """
        application_profile_text = """
        - Country: United States
        - Location: San Francisco, CA
        - Work Authorization Statement: I am always authorized to work in the United States unconditionally.
        - Authorized to Work Unconditionally: Yes
        - Require Sponsorship Now: No
        - Require Sponsorship in Future: No
        - Sponsorship Answer: No, I do not require sponsorship now or in the future. I am always authorized to work in the United States unconditionally.
        - Gender: Male
        - Race or Ethnicity: Hispanic or Latino
        - Sexual Orientation: Straight / Heterosexual
        - Veteran Status: I am not a protected veteran
        - Disability Status: No, I do not have a disability and have not had one in the past
        """
        profile = autofill._parse_master_resume(master_resume)
        application_profile = autofill._parse_application_profile(application_profile_text)
        question = {
            "required": True,
            "label": "Please describe your work authorization and whether you require sponsorship now or in the future.",
            "fields": [
                {
                    "name": "question_work_auth_summary",
                    "type": "textarea",
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Klaviyo",
            cover_letter="",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(
            step["value"],
            "No, I do not require sponsorship now or in the future. I am always authorized to work in the United States unconditionally.",
        )
        self.assertEqual(step["source"], "application_profile.md")

    def test_question_step_prefers_not_applicable_for_prior_none_of_the_above_follow_up(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "required": True,
            "label": (
                "If you selected a response to the prior question other than none of the above, "
                "please confirm whether any of the following also applies to you. Select all that apply."
            ),
            "fields": [
                {
                    "name": "question_export_controls_followup",
                    "type": "multi_value_multi_select",
                    "values": [
                        {"label": "U.S. citizen", "value": 1},
                        {"label": "U.S. permanent resident (Green Card holder)", "value": 2},
                        {"label": "Not applicable (i.e., I selected \"none of the above\" for the prior question)", "value": 3},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Databricks",
            cover_letter="",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(
            step["option"],
            "Not applicable (i.e., I selected \"none of the above\" for the prior question)",
        )
        self.assertEqual(step["source"], "application_profile.md")

    def test_question_step_matches_authorized_to_lawfully_work_variant(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        master_resume = """
        JERRISON LI
        Principal Product Manager
        San Francisco, CA  |  jerrisonli@gmail.com  |  510-613-5192  |  linkedin.com/in/jerrison/  |  jerrisonli.com
        ## MOODY'S ANALYTICS — Associate Director, Product Management
        Work Authorization: United States Citizen
        """
        application_profile_text = """
        - Country: United States
        - Location: San Francisco, CA
        - Work Authorization Statement: I am always authorized to work in the United States unconditionally.
        - Authorized to Work Unconditionally: Yes
        - Require Sponsorship Now: No
        - Require Sponsorship in Future: No
        - Sponsorship Answer: No
        - Gender: Male
        - Race or Ethnicity: Hispanic or Latino
        - Sexual Orientation: Straight / Heterosexual
        - Veteran Status: I am not a protected veteran
        - Disability Status: No, I do not have a disability and have not had one in the past
        """
        profile = autofill._parse_master_resume(master_resume)
        application_profile = autofill._parse_application_profile(application_profile_text)
        question = {
            "required": True,
            "label": "Are you authorized to lawfully work in the country where this role is located?",
            "fields": [
                {
                    "name": "question_29983284003",
                    "type": "multi_value_single_select",
                    "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="SoFi",
            cover_letter="",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["option"], "Yes")
        self.assertEqual(step["source"], "application_profile.md")

    def test_question_step_work_authorization_category_catches_authorization_to_work_variant(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Can you provide verification of both your identity and authorization to work in the United States, to the extent required by law?",
            "required": True,
            "fields": [
                {
                    "name": "question_identity_work_auth",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Axon",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["option"], "Yes")
        self.assertEqual(step["source"], "application_profile.md")

    def test_question_step_answers_no_for_restricted_country_residency_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": (
                "Do you reside in or maintain an established permanent residence in any of the following "
                "countries: Cuba, Iran, North Korea, Syria, or the following territories of Ukraine "
                "(Luhansk, Donetsk, Crimea)?"
            ),
            "required": True,
            "fields": [
                {
                    "name": "question_restricted_country",
                    "type": "multi_value_single_select",
                    "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Planet",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["option"], "No")
        self.assertEqual(step["source"], "application_profile.md")

    def test_question_step_uses_sponsorship_answer_for_employment_based_status_text_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        master_resume = """
        JERRISON LI
        Principal Product Manager
        San Francisco, CA  |  jerrisonli@gmail.com  |  510-613-5192  |  linkedin.com/in/jerrison/  |  jerrisonli.com
        ## MOODY'S ANALYTICS — Associate Director, Product Management
        Work Authorization: United States Citizen
        """
        application_profile_text = """
        - Country: United States
        - Location: San Francisco, CA
        - Work Authorization Statement: I am always authorized to work in the United States unconditionally.
        - Authorized to Work Unconditionally: Yes
        - Require Sponsorship Now: No
        - Require Sponsorship in Future: No
        - Sponsorship Answer: No, I do not require sponsorship now or in the future. I am always authorized to work in the United States unconditionally.
        - Gender: Male
        - Race or Ethnicity: Hispanic or Latino
        - Sexual Orientation: Straight / Heterosexual
        - Veteran Status: I am not a protected veteran
        - Disability Status: No, I do not have a disability and have not had one in the past
        """
        profile = autofill._parse_master_resume(master_resume)
        application_profile = autofill._parse_application_profile(application_profile_text)
        question = {
            "required": True,
            "label": (
                "Will you now or in the future require our company to file a petition or application "
                "for employment-based immigration status on your behalf to begin or continue employment "
                "with our company?"
            ),
            "fields": [{"name": "question_employment_based_status", "type": "textarea"}],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Klaviyo",
            cover_letter="",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(
            step["value"],
            "No, I do not require sponsorship now or in the future. I am always authorized to work in the United States unconditionally.",
        )
        self.assertEqual(step["source"], "application_profile.md")

    def test_question_step_uses_full_name_for_nda_acknowledgement_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Please Review the NDA and indicate your agreement by typing your full name below",
            "required": True,
            "fields": [{"name": "question_nda_name", "type": "input_text"}],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Rubrik",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["value"], profile.full_name)
        self.assertEqual(step["source"], "master_resume.md")

    def test_build_steps_skips_classic_demographic_answer_fields(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        master_resume = """
        JERRISON LI
        Principal Product Manager
        San Francisco, CA  |  jerrisonli@gmail.com  |  510-613-5192  |  linkedin.com/in/jerrison/  |  jerrisonli.com
        ## MOODY'S ANALYTICS — Associate Director, Product Management
        Work Authorization: United States Citizen
        """
        application_profile_text = """
        - Country: United States
        - Location: San Francisco, CA
        - Work Authorization Statement: I am always authorized to work in the United States unconditionally.
        - Authorized to Work Unconditionally: Yes
        - Require Sponsorship Now: No
        - Require Sponsorship in Future: No
        - Sponsorship Answer: No
        - Meets Minimum Years of Experience Requirement: Yes
        - Live In Job Location: Yes
        - Willing to Relocate: Yes
        - Comfortable Working On Site: Yes
        - Gender: Male
        - Race or Ethnicity: Hispanic or Latino
        - Sexual Orientation: Straight / Heterosexual
        - Veteran Status: I am not a protected veteran
        - Disability Status: No, I do not have a disability and have not had one in the past
        """
        candidate_profile = autofill._parse_master_resume(master_resume)
        application_profile = autofill._parse_application_profile(application_profile_text)
        job_post = {
            "questions": [
                {
                    "required": False,
                    "label": "Man Non-binary Woman I prefer to self-describe I don't wish to answer",
                    "fields": [
                        {
                            "name": "job_application[demographic_answers][][answer_options][][answer_option_id]",
                            "type": "input_text",
                        }
                    ],
                },
                {
                    "required": True,
                    "label": "LinkedIn Profile",
                    "fields": [{"name": "question_linkedin", "type": "input_text"}],
                },
            ],
            "eeoc_sections": [],
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir)
            (out_dir / "cover_letter_text.txt").write_text("Dear Hiring Team,\n\nThanks.\n", encoding="utf-8")
            (out_dir / "Jerrison Li Cover Letter - Test.pdf").write_bytes(b"%PDF-1.4")
            (out_dir / "Jerrison Li Resume - Test.pdf").write_bytes(b"%PDF-1.4")

            steps = autofill._build_steps(
                job_post,
                {"company_proper": "Test"},
                candidate_profile,
                application_profile,
                out_dir,
                generated_answers={},
            )

        field_names = {step["field_name"] for step in steps}
        self.assertNotIn(
            "job_application[demographic_answers][][answer_options][][answer_option_id]",
            field_names,
        )
        self.assertIn("question_linkedin", field_names)

    def test_preferred_education_entry_prefers_mba_finance_degree(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))

        education = autofill._preferred_education_entry(profile)

        self.assertIsNotNone(education)
        assert education is not None
        self.assertEqual(education.school, "University of Pennsylvania")
        self.assertEqual(education.degree_option, "Master of Business Administration (M.B.A.)")
        self.assertEqual(education.discipline_option, "Finance")
        self.assertEqual(education.end_year, "2020")

    def test_build_steps_include_education_fields_from_master_resume(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        candidate_profile = autofill._parse_master_resume(
            (PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8")
        )
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        job_post = {"questions": [], "eeoc_sections": []}

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir)
            (out_dir / "cover_letter_text.txt").write_text("Dear Hiring Team,\n\nThanks.\n", encoding="utf-8")
            (out_dir / "Jerrison Li Cover Letter - Test.pdf").write_bytes(b"%PDF-1.4")
            (out_dir / "Jerrison Li Resume - Test.pdf").write_bytes(b"%PDF-1.4")

            steps = autofill._build_steps(
                job_post,
                {"company_proper": "Test"},
                candidate_profile,
                application_profile,
                out_dir,
                generated_answers={},
            )

        steps_by_field = {step["field_name"]: step for step in steps}
        self.assertNotIn("country", steps_by_field)
        self.assertEqual(
            steps_by_field["job_application[educations][][school_name_id]"]["search"],
            "University of Pennsylvania",
        )
        self.assertEqual(
            steps_by_field["job_application[educations][][school_name_id]"]["selector"],
            '#application_form [id^="s2id_education_school_name_"] a.select2-choice, #application-form [id^="s2id_education_school_name_"] a.select2-choice',
        )
        self.assertEqual(
            steps_by_field["job_application[educations][][degree_id]"]["option"],
            "Master of Business Administration (M.B.A.)",
        )
        self.assertEqual(
            steps_by_field["job_application[educations][][degree_id]"]["fallback_options"],
            ["Master Degree", "Master's Degree"],
        )
        self.assertEqual(
            steps_by_field["job_application[educations][][degree_id]"]["selector"],
            '#application_form select[id^="education_degree_"], #application-form select[id^="education_degree_"]',
        )
        self.assertEqual(
            steps_by_field["job_application[educations][][discipline_id]"]["option"],
            "Finance",
        )
        self.assertEqual(
            steps_by_field["job_application[educations][][discipline_id]"]["fallback_options"],
            ["Computer Science"],
        )
        self.assertEqual(
            steps_by_field["job_application[educations][][discipline_id]"]["selector"],
            '#application_form select[id^="education_discipline_"], #application-form select[id^="education_discipline_"]',
        )
        self.assertEqual(
            steps_by_field["job_application[educations][][end_date][year]"]["value"],
            "2020",
        )

    def test_build_steps_skip_hidden_education_config_fields(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        candidate_profile = autofill._parse_master_resume(
            (PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8")
        )
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        job_post = {
            "questions": [],
            "eeoc_sections": [],
            "education_config": {
                "school_name": "hidden",
                "degree": "hidden",
                "discipline": "hidden",
                "start_month": "hidden",
                "start_year": "hidden",
                "end_month": "hidden",
                "end_year": "hidden",
            },
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir)
            (out_dir / "cover_letter_text.txt").write_text("Dear Hiring Team,\n\nThanks.\n", encoding="utf-8")
            (out_dir / "Jerrison Li Cover Letter - Test.pdf").write_bytes(b"%PDF-1.4")
            (out_dir / "Jerrison Li Resume - Test.pdf").write_bytes(b"%PDF-1.4")

            steps = autofill._build_steps(
                job_post,
                {"company_proper": "Test"},
                candidate_profile,
                application_profile,
                out_dir,
                generated_answers={},
            )

        field_names = {step["field_name"] for step in steps}
        self.assertNotIn("job_application[educations][][school_name_id]", field_names)
        self.assertNotIn("job_application[educations][][degree_id]", field_names)
        self.assertNotIn("job_application[educations][][discipline_id]", field_names)
        self.assertNotIn("job_application[educations][][end_date][year]", field_names)

    def test_greenhouse_file_upload_confirmed_requires_widget_filename_when_widget_present(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        snapshot = {
            "matching_file_input": True,
            "widget_present": True,
            "widget_text": "Resume/CV Attach Dropbox Google Drive Enter manually",
            "chosen_text": "",
            "body_text": "Resume/CV Attach Dropbox Google Drive Enter manually",
        }

        self.assertFalse(
            autofill._greenhouse_file_upload_confirmed(
                snapshot,
                "Jerrison Li Resume - Quince.pdf",
            )
        )

    def test_greenhouse_file_upload_confirmed_allows_simple_input_without_widget(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        snapshot = {
            "matching_file_input": True,
            "widget_present": False,
            "widget_text": "",
            "chosen_text": "",
            "body_text": "",
        }

        self.assertTrue(
            autofill._greenhouse_file_upload_confirmed(
                snapshot,
                "Jerrison Li Resume - Example.pdf",
            )
        )

    def test_wait_for_greenhouse_file_upload_confirmation_polls_for_visible_widget_state(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        class FakePage:
            def __init__(self):
                self.calls = []

            def wait_for_function(self, script, *, arg, timeout):
                self.calls.append({"script": script, "payload": arg, "timeout": timeout})

        page = FakePage()
        step = {
            "label": "Resume/CV",
            "file_path": "/tmp/Jerrison Li Resume - Example.pdf",
        }

        confirmed = autofill._wait_for_greenhouse_file_upload_confirmation(page, step, timeout_ms=3200)

        self.assertTrue(confirmed)
        self.assertEqual(
            page.calls,
            [
                {
                    "script": mock.ANY,
                    "payload": {
                        "uploadKey": "resume",
                        "expectedName": "Jerrison Li Resume - Example.pdf",
                    },
                    "timeout": 3200,
                }
            ],
        )

    def test_wait_for_greenhouse_file_upload_confirmation_returns_false_after_timeout(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        class FakePage:
            def wait_for_function(self, script, *, arg, timeout):
                raise TimeoutError("timed out")

        step = {
            "label": "Cover Letter",
            "file_path": "/tmp/Jerrison Li Cover Letter - Example.pdf",
        }

        confirmed = autofill._wait_for_greenhouse_file_upload_confirmation(FakePage(), step, timeout_ms=1200)

        self.assertFalse(confirmed)

    def test_extract_security_code_from_gmail_message(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        message = {
            "snippet": "Your verification code is ABCD1234.",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Greenhouse verification code"},
                ],
                "body": {"data": ""},
            },
        }

        code = autofill._extract_security_code_from_gmail_message(message)

        self.assertEqual(code, "ABCD1234")

    def test_fetch_security_code_from_gmail_filters_out_stale_messages(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        threshold = datetime(2026, 3, 13, 13, 0, 0, tzinfo=UTC)
        stale_ms = str(int(datetime(2026, 3, 13, 12, 59, 0, tzinfo=UTC).timestamp() * 1000))
        fresh_ms = str(int(datetime(2026, 3, 13, 13, 0, 5, tzinfo=UTC).timestamp() * 1000))
        responses = [
            {"messages": [{"id": "stale"}, {"id": "fresh"}]},
            {
                "id": "stale",
                "internalDate": stale_ms,
                "snippet": "Copy and paste this code into the security code field on your application: OLD12345",
                "payload": {"headers": [], "body": {"data": ""}},
            },
            {
                "id": "fresh",
                "internalDate": fresh_ms,
                "snippet": "Copy and paste this code into the security code field on your application: NEW12345",
                "payload": {"headers": [], "body": {"data": ""}},
            },
        ]

        with mock.patch.object(autofill, "_run_gws_json", side_effect=responses):
            code = autofill._fetch_security_code_from_gmail(
                "jerrisonli@gmail.com",
                min_received_at_utc=threshold,
                wait_seconds=0,
            )

        self.assertEqual(code, "NEW12345")

    def test_choose_capture_root_prefers_scrollable_form_ancestor(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        chosen = autofill._choose_capture_root(
            {
                "viewport_width": 1600,
                "viewport_height": 1200,
                "candidates": [
                    {
                        "key": "__document__",
                        "kind": "document",
                        "contains_form": True,
                        "scroll_height": 1200,
                        "client_height": 1200,
                        "width": 1600,
                        "height": 1200,
                    },
                    {
                        "key": "app-shell",
                        "kind": "ancestor",
                        "contains_form": True,
                        "scroll_height": 3200,
                        "client_height": 1000,
                        "width": 1400,
                        "height": 1000,
                    },
                ],
            }
        )

        self.assertEqual(chosen["key"], "app-shell")

    def test_choose_capture_root_falls_back_to_document(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        chosen = autofill._choose_capture_root(
            {
                "viewport_width": 1600,
                "viewport_height": 1200,
                "candidates": [
                    {
                        "key": "__document__",
                        "kind": "document",
                        "contains_form": True,
                        "scroll_height": 2400,
                        "client_height": 1200,
                        "width": 1600,
                        "height": 1200,
                    },
                    {
                        "key": "small-panel",
                        "kind": "ancestor",
                        "contains_form": True,
                        "scroll_height": 900,
                        "client_height": 850,
                        "width": 420,
                        "height": 260,
                    },
                ],
            }
        )

        self.assertEqual(chosen["key"], "__document__")

    def test_capture_scroll_helpers_use_capture_root_key(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        class FakePage:
            def __init__(self):
                self.calls = []

            def evaluate(self, script, payload):
                self.calls.append(payload)
                if "target" in payload:
                    return payload["target"]
                return {
                    "scrollHeight": 2400,
                    "viewportHeight": 900,
                    "devicePixelRatio": 2,
                }

        page = FakePage()

        metrics = autofill._capture_scroll_metrics(page, root_key="capture-root")
        actual = autofill._set_capture_scroll_position(
            page,
            root_key="capture-root",
            target_css=600,
        )

        self.assertEqual(metrics["scrollHeight"], 2400)
        self.assertEqual(metrics["viewportHeight"], 900)
        self.assertEqual(actual, 600.0)
        self.assertEqual(page.calls[0], {"rootKey": "capture-root"})
        self.assertEqual(page.calls[1], {"rootKey": "capture-root", "target": 600})

    def test_classify_submission_snapshot_detects_confirmation_and_validation(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        confirmed = autofill._classify_submission_snapshot(
            {
                "url": "https://boards.greenhouse.io/embed/job_app?token=123",
                "page_text": "Thank you for applying. We have received your application.",
                "page_title": "Thank you for applying",
                "form_visible": False,
                "errors": [],
                "invalid_fields": [],
                "security_code_visible": False,
                "confirmation_visible": False,
            }
        )
        classic_confirmed = autofill._classify_submission_snapshot(
            {
                "url": "https://boards.greenhouse.io/embed/job_app?for=coreweave&token=123",
                "page_text": "Thank you for your applying to CoreWeave. Your application is under review.",
                "page_title": "Thank you for applying",
                "form_visible": False,
                "errors": [],
                "invalid_fields": [],
                "security_code_visible": False,
                "confirmation_visible": True,
            }
        )
        validation_error = autofill._classify_submission_snapshot(
            {
                "url": "https://boards.greenhouse.io/embed/job_app?token=123",
                "page_text": "Please enter a valid security code.",
                "page_title": "Apply",
                "form_visible": True,
                "errors": ["Please enter a valid security code."],
                "invalid_fields": ["Security code"],
                "security_code_visible": True,
                "confirmation_visible": False,
            }
        )
        security_code = autofill._classify_submission_snapshot(
            {
                "url": "https://boards.greenhouse.io/embed/job_app?token=123",
                "page_text": "Enter the security code we emailed you.",
                "page_title": "Apply",
                "form_visible": True,
                "errors": [],
                "invalid_fields": [],
                "security_code_visible": True,
                "confirmation_visible": False,
            }
        )

        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(classic_confirmed["status"], "confirmed")
        self.assertEqual(validation_error["status"], "validation_error")
        self.assertEqual(security_code["status"], "security_code_required")

    def test_write_autofill_report(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir)
            (out_dir / "greenhouse_autofill_pages").mkdir(parents=True)
            (out_dir / "greenhouse_autofill_pre_submit.png").write_bytes(b"pre-submit")
            (out_dir / "greenhouse_autofill_pages" / "page_01.png").write_bytes(b"page-01")
            (out_dir / "greenhouse_autofill_pages" / "page_02.png").write_bytes(b"page-02")
            payload = {
                "company": "Figma",
                "job_title": "Product Manager, Design Tools",
                "job_url": "https://example.com/job",
                "artifacts": {
                    "report_markdown": str(out_dir / "greenhouse_autofill_report.md"),
                    "report_json": str(out_dir / "greenhouse_autofill_report.json"),
                    "pre_submit_screenshot": str(out_dir / "greenhouse_autofill_pre_submit.png"),
                    "page_screenshots_dir": str(out_dir / "greenhouse_autofill_pages"),
                    "unknown_questions_json": str(out_dir / "greenhouse_unknown_questions.json"),
                },
                "steps": [
                    {
                        "field_name": "first_name",
                        "label": "First Name",
                        "kind": "text",
                        "value": "Jerrison",
                        "source": "master_resume.md",
                        "page_index": 1,
                    },
                    {
                        "field_name": "resume",
                        "label": "Resume/CV",
                        "kind": "file",
                        "file_path": "/tmp/Jerrison Li Resume - Figma.pdf",
                        "source": "existing_resume_asset",
                        "page_index": 1,
                    },
                    {
                        "field_name": "cover_letter",
                        "label": "Cover Letter",
                        "kind": "file",
                        "file_path": "/tmp/Jerrison Li Cover Letter - Figma.pdf",
                        "source": "existing_cover_letter_asset",
                    },
                ],
            }
            runtime = {
                "pages": [
                    {"index": 1, "screenshot": str(out_dir / "greenhouse_autofill_pages" / "page_01.png")},
                    {"index": 2, "screenshot": str(out_dir / "greenhouse_autofill_pages" / "page_02.png")},
                ],
                "extra_report_steps": [
                    {
                        "field_name": "security_code",
                        "label": "Security code",
                        "kind": "text",
                        "value": "ABCD1234",
                        "report_value": "[redacted 8-character code]",
                        "source": "googleworkspace/cli:gmail",
                        "page_index": 2,
                    }
                ],
                "unknown_questions": [],
            }

            autofill._write_autofill_report(payload, runtime)

            markdown = (out_dir / "greenhouse_autofill_report.md").read_text(encoding="utf-8")
            report_json = json.loads((out_dir / "greenhouse_autofill_report.json").read_text(encoding="utf-8"))

        self.assertIn("First Name (`first_name`)", markdown)
        self.assertIn("Resume/CV (`resume`)", markdown)
        self.assertIn("Security code (`security_code`)", markdown)
        self.assertIn("Page: `2`", markdown)
        self.assertIn("## Planned But Unconfirmed", markdown)
        self.assertIn("Cover Letter (`cover_letter`) from `existing_cover_letter_asset`", markdown)
        self.assertEqual(report_json["company"], "Figma")
        self.assertEqual(report_json["fields"][0]["value"], "Jerrison")
        self.assertEqual(report_json["fields"][1]["value"], "/tmp/Jerrison Li Resume - Figma.pdf")
        self.assertEqual(report_json["fields"][2]["value"], "[redacted 8-character code]")
        self.assertEqual(report_json["planned_but_unconfirmed_fields"][0]["field_name"], "cover_letter")
        self.assertEqual(len(report_json["page_screenshots"]), 2)

    def test_write_autofill_report_dedupes_page_screenshot_that_matches_pre_submit(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir)
            pages_dir = out_dir / "greenhouse_autofill_pages"
            pages_dir.mkdir(parents=True)
            pre_submit = out_dir / "greenhouse_autofill_pre_submit.png"
            page_one = pages_dir / "page_01.png"
            page_two = pages_dir / "page_02.png"
            pre_submit.write_bytes(b"same-image")
            page_one.write_bytes(b"same-image")
            page_two.write_bytes(b"different-image")
            payload = {
                "company": "Figma",
                "job_title": "Product Manager, Design Tools",
                "job_url": "https://example.com/job",
                "artifacts": {
                    "report_markdown": str(out_dir / "greenhouse_autofill_report.md"),
                    "report_json": str(out_dir / "greenhouse_autofill_report.json"),
                    "pre_submit_screenshot": str(pre_submit),
                    "page_screenshots_dir": str(pages_dir),
                    "unknown_questions_json": str(out_dir / "greenhouse_unknown_questions.json"),
                },
                "steps": [],
            }
            runtime = {
                "pages": [
                    {"index": 1, "screenshot": str(page_one)},
                    {"index": 2, "screenshot": str(page_two)},
                ],
                "unknown_questions": [],
            }

            autofill._write_autofill_report(payload, runtime)
            report_json = json.loads((out_dir / "greenhouse_autofill_report.json").read_text(encoding="utf-8"))

        self.assertEqual(report_json["page_screenshots"], [str(page_two)])

    def test_write_autofill_report_moves_review_validation_blockers_out_of_filled_fields(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir)
            payload = {
                "company": "Klaviyo",
                "job_title": "Head of Product - AI Applications",
                "job_url": "https://example.com/job",
                "artifacts": {
                    "report_markdown": str(out_dir / "greenhouse_autofill_report.md"),
                    "report_json": str(out_dir / "greenhouse_autofill_report.json"),
                    "pre_submit_screenshot": str(out_dir / "greenhouse_autofill_pre_submit.png"),
                    "page_screenshots_dir": str(out_dir / "greenhouse_autofill_pages"),
                    "unknown_questions_json": str(out_dir / "greenhouse_unknown_questions.json"),
                },
                "steps": [
                    {
                        "field_name": "first_name",
                        "label": "First Name",
                        "kind": "text",
                        "value": "Jerrison",
                        "source": "master_resume.md",
                        "page_index": 1,
                    },
                    {
                        "field_name": "question_29573290003",
                        "label": "How did you hear about us?",
                        "kind": "combobox",
                        "value": "Company Website / Careers Page",
                        "source": "application_profile.md",
                        "page_index": 1,
                    },
                    {
                        "field_name": "gender_identity",
                        "label": "Gender Identity",
                        "kind": "combobox",
                        "value": "Man",
                        "source": "application_profile.md",
                        "page_index": 1,
                    },
                ],
            }
            runtime = {
                "pages": [],
                "extra_report_steps": [],
                "unknown_questions": [],
                "review_validation_blockers": [
                    {"label": "How did you hear about us?", "message": "This field is required."},
                    {"label": "Gender Identity", "message": "This field is required."},
                ],
            }

            autofill._write_autofill_report(payload, runtime)

            report_json = json.loads((out_dir / "greenhouse_autofill_report.json").read_text(encoding="utf-8"))

        self.assertEqual([entry["field_name"] for entry in report_json["fields"]], ["first_name"])
        blocker_entries = {entry["field_name"]: entry for entry in report_json["planned_but_unconfirmed_fields"]}
        self.assertEqual(
            blocker_entries["question_29573290003"]["note"],
            "Visible validation error on page: This field is required.",
        )
        self.assertEqual(
            blocker_entries["gender_identity"]["note"],
            "Visible validation error on page: This field is required.",
        )

    def test_write_autofill_report_moves_visible_self_id_runtime_mismatch_out_of_filled_fields(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir)
            payload = {
                "company": "Snorkel AI",
                "job_title": "Senior Product Manager - Platform",
                "job_url": "https://example.com/job",
                "artifacts": {
                    "report_markdown": str(out_dir / "greenhouse_autofill_report.md"),
                    "report_json": str(out_dir / "greenhouse_autofill_report.json"),
                    "pre_submit_screenshot": str(out_dir / "greenhouse_autofill_pre_submit.png"),
                    "page_screenshots_dir": str(out_dir / "greenhouse_autofill_pages"),
                    "unknown_questions_json": str(out_dir / "greenhouse_unknown_questions.json"),
                },
                "steps": [
                    {
                        "field_name": "first_name",
                        "label": "First Name",
                        "kind": "text",
                        "value": "Jerrison",
                        "source": "master_resume.md",
                        "page_index": 1,
                    },
                    {
                        "field_name": "gender",
                        "label": "Gender",
                        "kind": "combobox",
                        "value": "Male",
                        "source": "application_profile.md",
                        "page_index": 1,
                        "blocks_draft_completion": True,
                        "blocker_kind": "visible_self_id",
                        "profile_field": "gender",
                    },
                ],
            }
            runtime = {
                "pages": [],
                "extra_report_steps": [
                    {
                        "field_name": "gender",
                        "label": "Gender",
                        "kind": "combobox",
                        "value": "Female",
                        "source": "application_profile.md",
                        "page_index": 1,
                        "filled": True,
                        "status": "filled",
                        "blocks_draft_completion": True,
                        "blocker_kind": "visible_self_id",
                        "profile_field": "gender",
                    }
                ],
                "unknown_questions": [],
                "review_validation_blockers": [],
            }

            autofill._write_autofill_report(payload, runtime)

            report_json = json.loads((out_dir / "greenhouse_autofill_report.json").read_text(encoding="utf-8"))

        self.assertEqual([entry["field_name"] for entry in report_json["fields"]], ["first_name"])
        mismatch_entry = report_json["planned_but_unconfirmed_fields"][0]
        self.assertEqual(mismatch_entry["field_name"], "gender")
        self.assertEqual(mismatch_entry["value"], "Male")
        self.assertEqual(
            mismatch_entry["note"],
            "Live form showed 'Female' instead of expected 'Male'.",
        )

    def test_write_autofill_report_moves_blank_visible_profile_runtime_entry_out_of_filled_fields(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir)
            payload = {
                "company": "Invoca",
                "job_title": "Staff PM - AI Platform",
                "job_url": "https://example.com/job",
                "artifacts": {
                    "report_markdown": str(out_dir / "greenhouse_autofill_report.md"),
                    "report_json": str(out_dir / "greenhouse_autofill_report.json"),
                    "pre_submit_screenshot": str(out_dir / "greenhouse_autofill_pre_submit.png"),
                    "page_screenshots_dir": str(out_dir / "greenhouse_autofill_pages"),
                    "unknown_questions_json": str(out_dir / "greenhouse_unknown_questions.json"),
                },
                "steps": [
                    {
                        "field_name": "first_name",
                        "label": "First Name",
                        "kind": "text",
                        "value": "Jerrison",
                        "source": "master_resume.md",
                        "page_index": 1,
                    },
                    {
                        "field_name": "work_authorization",
                        "label": "Can you, after employment, submit verification of your legal right to work in the United States*",
                        "kind": "combobox",
                        "value": "Yes",
                        "source": "application_profile.md",
                        "page_index": 1,
                    },
                ],
            }
            runtime = {
                "pages": [],
                "extra_report_steps": [
                    {
                        "field_name": "work_authorization",
                        "label": "Can you, after employment, submit verification of your legal right to work in the United States?",
                        "kind": "combobox",
                        "value": "Yes",
                        "observed_value": "",
                        "report_value": "Yes",
                        "source": "application_profile.md",
                        "page_index": 1,
                        "status": "planned",
                        "blocks_draft_completion": True,
                        "blocker_kind": "visible_profile_field",
                        "profile_field": "work_authorization",
                    }
                ],
                "unknown_questions": [],
                "review_validation_blockers": [],
            }

            autofill._write_autofill_report(payload, runtime)

            report_json = json.loads((out_dir / "greenhouse_autofill_report.json").read_text(encoding="utf-8"))

        self.assertEqual([entry["field_name"] for entry in report_json["fields"]], ["first_name"])
        missing_entry = report_json["planned_but_unconfirmed_fields"][0]
        self.assertEqual(missing_entry["field_name"], "work_authorization")
        self.assertEqual(missing_entry["value"], "Yes")
        self.assertEqual(
            missing_entry["note"],
            "Live form showed no selected value instead of expected 'Yes'.",
        )

    def test_write_autofill_report_preserves_multiple_checkbox_values_for_same_field(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir)
            payload = {
                "company": "Motive",
                "job_title": "Staff Product Manager, Data Platform (as a Product)",
                "job_url": "https://example.com/job",
                "artifacts": {
                    "report_markdown": str(out_dir / "greenhouse_autofill_report.md"),
                    "report_json": str(out_dir / "greenhouse_autofill_report.json"),
                    "pre_submit_screenshot": str(out_dir / "greenhouse_autofill_pre_submit.png"),
                    "page_screenshots_dir": str(out_dir / "greenhouse_autofill_pages"),
                    "unknown_questions_json": str(out_dir / "greenhouse_unknown_questions.json"),
                },
                "steps": [
                    {
                        "field_name": "question_34340118002[]",
                        "label": "What tangible factors are most important to you when considering a job opportunity?",
                        "kind": "checkbox",
                        "value": "Career Growth",
                        "source": "generated_application_answer",
                        "page_index": 1,
                        "filled": True,
                    },
                    {
                        "field_name": "question_34340118002[]",
                        "label": "What tangible factors are most important to you when considering a job opportunity?",
                        "kind": "checkbox",
                        "value": "Culture",
                        "source": "generated_application_answer",
                        "page_index": 1,
                        "filled": True,
                    },
                    {
                        "field_name": "question_34340118002[]",
                        "label": "What tangible factors are most important to you when considering a job opportunity?",
                        "kind": "checkbox",
                        "value": "Company Outlook",
                        "source": "generated_application_answer",
                        "page_index": 1,
                        "filled": True,
                    },
                ],
            }
            runtime = {
                "pages": [],
                "extra_report_steps": [],
                "unknown_questions": [],
                "review_validation_blockers": [],
            }

            autofill._write_autofill_report(payload, runtime)

            report_json = json.loads((out_dir / "greenhouse_autofill_report.json").read_text(encoding="utf-8"))

        self.assertEqual(
            [entry["value"] for entry in report_json["fields"]],
            ["Career Growth", "Culture", "Company Outlook"],
        )
        self.assertFalse(report_json.get("planned_but_unconfirmed_fields"))

    def test_capture_review_checkpoint_artifacts_honors_configured_screenshot_artifacts(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")

        class _FakePage:
            def __init__(self):
                self.screenshot_calls = []

            def screenshot(self, *, path, type="png", full_page=False):
                self.screenshot_calls.append({"path": path, "type": type, "full_page": full_page})

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir)
            pre_submit = out_dir / "greenhouse_autofill_pre_submit.png"
            page = _FakePage()
            stitched_calls = []

            with mock.patch.object(
                autofill,
                "capture_stitched_screenshot",
                side_effect=lambda page_arg, output_path: stitched_calls.append((page_arg, output_path)),
            ):
                autofill._capture_review_checkpoint_artifacts(
                    page,
                    {
                        "pre_submit_screenshot": str(pre_submit),
                    },
                )

        self.assertEqual(page.screenshot_calls, [])
        self.assertEqual(stitched_calls, [(page, str(pre_submit))])

    def test_question_step_answers_affirm_custom_prior_employment_negative_option(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Have you previously been employed at Affirm for any length of time?",
            "required": True,
            "fields": [
                {
                    "name": "question_affirm_prior_employment",
                    "type": "multi_value_single_select",
                    "values": [
                        {"label": "I have previously been employed at Affirm"},
                        {"label": "I have not previously been employed at Affirm"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Affirm",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["option"], "I have not previously been employed at Affirm")

    def test_question_step_answers_affirm_employer_discovery_prompt_with_corporate_website(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "How did you first learn about Affirm as an employer?",
            "required": True,
            "fields": [
                {
                    "name": "question_affirm_employer_source",
                    "type": "multi_value_single_select",
                    "values": [{"label": "Corporate website"}, {"label": "I have used Affirm as a product"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Affirm",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["option"], "Corporate website")
        self.assertEqual(step["source"], "application_profile.md")

    def test_question_step_prefers_company_career_site_variant_for_trueup_source(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "How did you first learn about Affirm as an employer?",
            "required": True,
            "fields": [
                {
                    "name": "question_affirm_employer_source",
                    "type": "multi_value_single_select",
                    "values": [{"label": "Affirm’s Career Site"}, {"label": "Other"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Affirm",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
            job_url="https://job-boards.greenhouse.io/affirm/jobs/7661569003?utm_source=trueup.io&utm_medium=website&ref=trueup",
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["option"], "Affirm’s Career Site")
        self.assertEqual(step["source"], "job_url.utm_source")

    def test_question_step_maps_trueup_source_to_other_job_site_variant(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "How did you first hear about Planet before applying for this position?",
            "required": True,
            "fields": [
                {
                    "name": "question_planet_source",
                    "type": "multi_value_single_select",
                    "values": [
                        {"label": "BuiltIn Article"},
                        {"label": "BuiltIn Job Search"},
                        {"label": "Conference"},
                        {"label": "Event"},
                        {"label": "Glassdoor Article"},
                        {"label": "Glassdoor Job Search"},
                        {"label": "Indeed"},
                        {"label": "Instagram"},
                        {"label": "LinkedIn Company Post"},
                        {"label": "LinkedIn Employee Post"},
                        {"label": "LinkedIn Job Search"},
                        {"label": "News Article"},
                        {"label": "Other - Event"},
                        {"label": "Other - Job Site"},
                        {"label": "Other - Social Media"},
                        {"label": "Other - Webinar"},
                        {"label": "Otta"},
                        {"label": "Planet Event"},
                        {"label": "Planet Webinar"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Planet",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
            job_url="https://job-boards.greenhouse.io/planet/jobs/example?utm_source=trueup.io&utm_medium=website&ref=trueup",
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["option"], "Other - Job Site")
        self.assertEqual(step["source"], "job_url.utm_source")

    def test_resolve_discovered_demographic_option_text_prefers_affirm_career_site_variant_for_trueup_source(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )

        resolved = autofill._resolve_discovered_demographic_option_text(
            field_name="how_did_you_hear",
            desired="Corporate website",
            option_texts=["Affirm’s Career Site", "LinkedIn", "Other"],
            application_profile=application_profile,
            company_name="Affirm",
            job_url="https://job-boards.greenhouse.io/affirm/jobs/7661569003?utm_source=trueup.io&utm_medium=website&ref=trueup",
        )

        self.assertEqual(resolved, "Affirm’s Career Site")

    def test_question_step_answers_current_professional_credential_inventory_with_none(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Please list any relevant professional certifications and/or licenses you currently hold.",
            "required": False,
            "fields": [{"name": "question_credentials", "type": "textarea"}],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Scout Motors",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["value"], "Associate of the Casualty Actuarial Society (ACAS)")
        self.assertEqual(step["source"], "master_resume.md")

    def test_question_step_answers_scout_employee_connections_prompt_with_no(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": (
                "Do you have any professional or personal connections to individuals currently employed by Scout "
                "Motors or any of its subsidiaries or related entities (for example, Volkswagen Group companies), "
                "including relationships as a colleague, friend, or family member?"
            ),
            "required": True,
            "fields": [
                {
                    "name": "question_scout_connections",
                    "type": "multi_value_single_select",
                    "values": [{"label": "Yes"}, {"label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Scout Motors",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["option"], "No")
        self.assertEqual(step["source"], "deterministic")

    def test_question_step_answers_scout_conflicts_prompt_with_no(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": (
                "Do you have any conflicts of interest that could affect your ability to perform the duties of the "
                "role you are applying for at Scout Motors, including financial interests held by you or your "
                "immediate family in competitors, vendors, or clients relevant to this role, or any personal or "
                "professional relationships that could create a conflict? Please describe any such conflicts."
            ),
            "required": True,
            "fields": [{"name": "question_scout_conflicts", "type": "textarea"}],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Scout Motors",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        self.assertEqual(step["value"], "No")
        self.assertEqual(step["source"], "deterministic")

    def test_question_step_answers_current_or_former_alphabet_prompt_with_not_applicable(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": (
                "Are you a current or former Alphabet employee, intern, temporary worker, contractor, or vendor "
                "(including Google, Wing, and all other Alphabet subsidiaries)? If so, please select your most "
                "recent Alphabet company affiliation."
            ),
            "required": True,
            "fields": [
                {
                    "name": "question_35899449002",
                    "type": "multi_value_single_select",
                    "values": [
                        {"label": "Not Applicable"},
                        {"label": "Bet employee (Wing, X, Waymo, Other)"},
                        {"label": "Google employee"},
                        {"label": "TVC (Temp, Vendor, Contractor)"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Wing",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["option"], "Not Applicable")

    def test_question_step_answers_startup_experience_yes(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Do you have experience in an early stage startup building 0>1?",
            "required": True,
            "fields": [
                {
                    "name": "question_runway_startup_experience",
                    "type": "multi_value_single_select",
                    "values": [{"label": "Yes"}, {"label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Runway",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["option"], "Yes")

    def test_question_step_answers_travel_customer_facing_prompt_yes(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Are you able to Travel and be Customer Facing up to 20%?",
            "required": True,
            "fields": [
                {
                    "name": "question_tekion_travel",
                    "type": "multi_value_single_select",
                    "values": [{"label": "Yes"}, {"label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Tekion",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["option"], "Yes")

    def test_question_step_answers_bachelors_degree_prompt_yes(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Do you have a Bachelor's degree?",
            "required": True,
            "fields": [
                {
                    "name": "question_securityscorecard_degree",
                    "type": "multi_value_single_select",
                    "values": [{"label": "Yes"}, {"label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="SecurityScorecard",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["option"], "Yes")

    def test_question_step_answers_engineering_background_prompt_yes(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Do you have an engineering background (either degree or professional experience)?",
            "required": True,
            "fields": [
                {
                    "name": "question_runway_engineering_background",
                    "type": "multi_value_single_select",
                    "values": [{"label": "Yes"}, {"label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Runway",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["option"], "Yes")

    def test_question_step_uses_full_name_for_legal_first_and_last_name_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Please provide your legal first and last name.",
            "required": True,
            "fields": [{"name": "question_figure_legal_name", "type": "input_text"}],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Figure",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["value"], profile.full_name)

    def test_question_step_uses_candidate_email_for_confirm_email_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Confirm your email address",
            "required": True,
            "fields": [{"name": "question_spring_health_confirm_email", "type": "input_text"}],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Spring Health",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["value"], profile.email)

    def test_question_step_answers_h1b_history_prompt_with_no(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": (
                "Have you held H-1B status, or had an H-1B petition approved on your behalf, within the "
                "preceding 6 years for an employer other than a cap exempt institution?"
            ),
            "required": True,
            "fields": [
                {
                    "name": "question_twitch_h1b_history",
                    "type": "multi_value_single_select",
                    "values": [{"label": "Yes"}, {"label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Twitch",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["option"], "No")

    def test_question_step_answers_future_job_openings_prompt_with_no(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Please email me about future job openings",
            "required": True,
            "fields": [
                {
                    "name": "question_dropbox_future_openings",
                    "type": "multi_value_single_select",
                    "values": [{"label": "Yes"}, {"label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Dropbox",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["option"], "No")

    def test_question_step_answers_company_familiarity_prompt_with_non_user_option(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Are you familiar with Twitch?",
            "description": (
                "We review all applications equally, whether you're an advanced user or new to the platform. "
                "Let us know your history with us!"
            ),
            "required": True,
            "fields": [
                {
                    "name": "question_twitch_familiarity",
                    "type": "multi_value_single_select",
                    "values": [
                        {"label": "Yes, I'm a Twitch Partner"},
                        {"label": "Yes, I'm a Twitch Affiliate"},
                        {"label": "Yes, I use Twitch (I'm a streamer and a viewer)"},
                        {"label": "Yes, I use Twitch (I'm a viewer)"},
                        {"label": "Yes, I'm familiar with Twitch, but I'm not a user"},
                        {"label": "No, I'm not on Twitch"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Twitch",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["option"], "Yes, I'm familiar with Twitch, but I'm not a user")

    def test_question_step_answers_pm_people_management_prompt_with_yes(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Do you have 2+ years managing a team of product managers?",
            "required": True,
            "fields": [
                {
                    "name": "question_pm_people_management",
                    "type": "multi_value_single_select",
                    "values": [{"label": "Yes"}, {"label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Dropbox",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["option"], "Yes")

    def test_question_step_answers_location_cost_tier_with_high_cost(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Location Cost Tier",
            "required": False,
            "fields": [
                {
                    "name": "question_location_cost_tier",
                    "type": "multi_value_single_select",
                    "values": [
                        {"label": "High Cost"},
                        {"label": "Mid Cost"},
                        {"label": "Low Cost"},
                        {"label": "Unknown"},
                    ],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Dropbox",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["option"], "High Cost")

    def test_question_step_prefers_generated_answer_for_relationship_conflict_text_prompt(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": (
                "Do you have any relatives or personal relationships working at Wing? If yes, "
                "please provide their name(s), department(s) and relationship(s) to you."
            ),
            "required": True,
            "fields": [{"name": "question_relationship_conflict", "type": "input_text"}],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Wing",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={"question_relationship_conflict": "N/A"},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["value"], "N/A")
        self.assertEqual(step["source"], "generated_application_answer")

    def test_question_step_answers_current_country_without_restrictions_prompt_with_yes(self):
        autofill = load_module("autofill_greenhouse", "scripts/autofill_greenhouse.py")
        profile = autofill._parse_master_resume((PROJECT_ROOT / "master_resume.md").read_text(encoding="utf-8"))
        application_profile = autofill._parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )
        question = {
            "label": "Are you able to work in your current country of residence without restrictions?",
            "required": True,
            "fields": [
                {
                    "name": "question_ava_work_country",
                    "type": "multi_value_single_select",
                    "values": [{"label": "Yes"}, {"label": "No"}],
                }
            ],
        }

        step = autofill._question_step(
            question=question,
            profile=profile,
            application_profile=application_profile,
            company_name="Ava Labs",
            cover_letter="Test cover letter.",
            cover_letter_file=None,
            generated_answers={},
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertEqual(step["option"], "Yes")


if __name__ == "__main__":
    unittest.main()
