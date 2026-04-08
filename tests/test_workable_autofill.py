import importlib.util
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


class WorkableAutofillTests(unittest.TestCase):
    def test_infer_deterministic_prefers_current_city_option_for_hybrid_location_prompt(self):
        autofill = load_module("autofill_workable", "scripts/autofill_workable.py")

        answer = autofill._infer_deterministic(
            "This is a Hybrid position in San Francisco. Please select which applies:",
            [
                "You currently live in San Francisco",
                "You are looking to relocate to San Francisco",
            ],
        )

        self.assertEqual(answer, "You currently live in San Francisco")

    def test_normalize_live_fields_ignores_internal_workable_file_input_ids(self):
        autofill = load_module("autofill_workable", "scripts/autofill_workable.py")

        fields = autofill._normalize_live_fields(
            [
                {
                    "label": "input_files_input_OsYQFr01J5qzXtA9",
                    "kind": "file",
                    "required": True,
                    "name": "input_files_input_OsYQFr01J5qzXtA9",
                    "id": "input_files_input_OsYQFr01J5qzXtA9",
                }
            ]
        )

        self.assertEqual(fields, [])

    def test_normalize_live_fields_strips_optional_suffix_from_labels(self):
        autofill = load_module("autofill_workable", "scripts/autofill_workable.py")

        fields = autofill._normalize_live_fields(
            [
                {
                    "label": "Cover letter (Optional)",
                    "kind": "textarea",
                    "required": False,
                    "name": "cover_letter",
                }
            ]
        )

        self.assertEqual(
            fields,
            [
                {
                    "field_name": "cover_letter",
                    "label": "Cover letter",
                    "kind": "textarea",
                    "required": False,
                    "name": "cover_letter",
                    "id": "",
                    "path": "cover_letter",
                    "options": [],
                    "field_type": "LongText",
                }
            ],
        )

    def test_reconcile_live_steps_replaces_cover_letter_upload_and_adds_generated_textarea_answer(self):
        autofill = load_module("autofill_workable", "scripts/autofill_workable.py")
        interest_label = "Why are you interested in joining the Blueprint team?"
        links_label = "Do you have any additional links you would like to share, i.e. portfolio/website/LinkedIn/socials?"
        cover_letter_text = "Blueprint is building something unusually ambitious, and that is exactly the kind of team I want to help scale."

        steps = [
            {
                "field_name": "website",
                "label": "Website",
                "kind": "text",
                "required": False,
                "value": "https://jerrisonli.com",
                "source": "master_resume.md",
            },
            {
                "field_name": "cover_letter",
                "label": "Cover letter",
                "kind": "file",
                "required": False,
                "file_path": "/tmp/Jerrison Li Cover Letter - Workable.pdf",
                "source": "existing_cover_letter_asset",
            },
        ]
        discovered_fields = [
            {"label": links_label, "kind": "textarea", "required": False},
            {"label": interest_label, "kind": "textarea", "required": True},
            {"label": "Cover letter", "kind": "textarea", "required": False},
        ]
        profile = SimpleNamespace(
            first_name="Jerrison",
            last_name="Li",
            email="jerrisonli@gmail.com",
            phone="510-613-5192",
            website="https://jerrisonli.com",
            linkedin="https://www.linkedin.com/in/jerrisonli/",
        )
        application_profile = SimpleNamespace(
            location="San Francisco, CA",
            website="https://jerrisonli.com",
            linkedin="https://www.linkedin.com/in/jerrisonli/",
        )
        generated_answer = "I want to help Blueprint turn an ambitious health vision into a product system that learns quickly from real users."

        with (
            mock.patch.object(autofill, "find_cover_letter_text", return_value=cover_letter_text),
            mock.patch.object(
                autofill,
                "generate_application_answers",
                return_value={autofill.slugify_label(interest_label): generated_answer},
            ),
        ):
            reconciled_steps, unknown_questions = autofill._reconcile_live_steps(
                steps=steps,
                discovered_fields=discovered_fields,
                out_dir=PROJECT_ROOT,
                meta={"company": "Workable"},
                profile=profile,
                application_profile=application_profile,
                provider="openai",
            )

        website_step = next(step for step in reconciled_steps if step["field_name"] == "website")
        self.assertEqual(website_step["label"], links_label)
        self.assertEqual(website_step["kind"], "textarea")
        self.assertEqual(website_step["value"], "https://jerrisonli.com")

        cover_letter_step = next(step for step in reconciled_steps if step["field_name"] == "cover_letter")
        self.assertEqual(cover_letter_step["kind"], "textarea")
        self.assertEqual(cover_letter_step["value"], cover_letter_text)
        self.assertEqual(cover_letter_step["source"], "cover_letter_text.txt")
        self.assertNotIn("file_path", cover_letter_step)

        interest_step = next(
            step
            for step in reconciled_steps
            if step["field_name"] == autofill.slugify_label(interest_label)
        )
        self.assertEqual(interest_step["kind"], "textarea")
        self.assertEqual(interest_step["value"], generated_answer)
        self.assertEqual(interest_step["source"], "generated_application_answer")
        self.assertEqual(unknown_questions, [])

    def test_reconcile_live_steps_marks_unanswered_required_textareas_unknown(self):
        autofill = load_module("autofill_workable", "scripts/autofill_workable.py")
        interest_label = "Why are you interested in joining the Blueprint team?"

        with mock.patch.object(autofill, "generate_application_answers", return_value={}):
            reconciled_steps, unknown_questions = autofill._reconcile_live_steps(
                steps=[],
                discovered_fields=[{"label": interest_label, "kind": "textarea", "required": True}],
                out_dir=PROJECT_ROOT,
                meta={"company": "Workable"},
                profile=SimpleNamespace(),
                application_profile=SimpleNamespace(),
                provider="openai",
            )

        self.assertEqual(reconciled_steps, [])
        self.assertEqual(
            unknown_questions,
            [
                {
                    "field_name": autofill.slugify_label(interest_label),
                    "label": interest_label,
                    "kind": "textarea",
                    "required": True,
                    "status": "unknown_required",
                }
            ],
        )

    def test_reconcile_live_steps_prefers_draft_overrides_for_discovered_textareas(self):
        autofill = load_module("autofill_workable", "scripts/autofill_workable.py")
        interest_label = "Why are you interested in joining the Blueprint team?"
        override_answer = "Reviewed answer from draft override."

        with mock.patch.object(
            autofill,
            "generate_application_answers",
            side_effect=AssertionError("draft overrides should bypass generation"),
        ):
            reconciled_steps, unknown_questions = autofill._reconcile_live_steps(
                steps=[],
                discovered_fields=[{"label": interest_label, "kind": "textarea", "required": True}],
                out_dir=PROJECT_ROOT,
                meta={"company": "Workable"},
                profile=SimpleNamespace(),
                application_profile=SimpleNamespace(),
                provider="openai",
                draft_overrides={autofill.slugify_label(interest_label): override_answer},
            )

        self.assertEqual(unknown_questions, [])
        self.assertEqual(
            reconciled_steps,
            [
                {
                    "field_name": autofill.slugify_label(interest_label),
                    "label": interest_label,
                    "kind": "textarea",
                    "required": True,
                    "field_type": "LongText",
                    "value": override_answer,
                    "source": "draft_overrides.json",
                }
            ],
        )

    def test_fill_text_field_targets_phone_number_input_in_composite_widget(self):
        autofill = load_module("autofill_workable", "scripts/autofill_workable.py")
        html = """
        <div class="field">
          <label for="country-code">Phone</label>
          <div class="phone-widget">
            <input id="country-code" type="text" value="+1" />
            <input id="phone-number" type="tel" />
          </div>
        </div>
        """

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)

            self.assertTrue(autofill._fill_text_field(page, "Phone", "510-613-5192"))
            self.assertEqual(page.locator("#country-code").input_value(), "+1")
            self.assertEqual(page.locator("#phone-number").input_value(), "510-613-5192")

            browser.close()
