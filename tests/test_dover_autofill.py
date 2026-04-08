import importlib.util
import json
import tempfile
import unittest
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


class DoverAutofillTests(unittest.TestCase):
    def test_infer_yes_no_answer_handles_salary_comfort(self):
        autofill = load_module("autofill_dover", "scripts/autofill_dover.py")

        answer = autofill._infer_yes_no_answer(
            "Are you comfortable interviewing for the salary outlined in the job description?",
            mock.Mock(
                comfortable_with_posted_salary=False, require_sponsorship_now=False, require_sponsorship_future=False
            ),
        )

        self.assertFalse(answer)

    def test_infer_yes_no_answer_handles_transgender_status(self):
        autofill = load_module("autofill_dover", "scripts/autofill_dover.py")

        answer = autofill._infer_yes_no_answer(
            "Do you identify as transgender?",
            mock.Mock(
                transgender_status="No",
                comfortable_with_posted_salary=True,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
            ),
        )

        self.assertFalse(answer)

    def test_infer_yes_no_answer_ignores_narrative_experience_prompt(self):
        autofill = load_module("autofill_dover", "scripts/autofill_dover.py")

        answer = autofill._infer_yes_no_answer(
            "Homeowners / P&C product ownership: Describe your experience managing a homeowners or property line of business. Include market context (admitted vs E&S, state[s], peril) and your decision scope.",
            mock.Mock(
                comfortable_with_posted_salary=True,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
                minimum_years_experience=True,
                authorized_to_work_unconditionally=True,
                willing_to_relocate=True,
                comfortable_working_on_site=True,
                lives_in_job_location=True,
                text_message_consent=False,
            ),
        )

        self.assertIsNone(answer)

    def test_infer_yes_no_answer_overrides_relocation_profile_flag_for_positive_fit(self):
        autofill = load_module("autofill_dover", "scripts/autofill_dover.py")

        answer = autofill._infer_yes_no_answer(
            "Are you willing to relocate for this position if required?",
            mock.Mock(
                comfortable_with_posted_salary=True,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
                minimum_years_experience=False,
                authorized_to_work_unconditionally=True,
                willing_to_relocate=False,
                comfortable_working_on_site=False,
                lives_in_job_location=False,
                text_message_consent=False,
            ),
        )

        self.assertTrue(answer)

    def test_infer_yes_no_answer_overrides_travel_profile_flag_for_positive_fit(self):
        autofill = load_module("autofill_dover", "scripts/autofill_dover.py")

        answer = autofill._infer_yes_no_answer(
            "Are you able to travel up to 50% of the time?",
            mock.Mock(
                comfortable_with_posted_salary=True,
                require_sponsorship_now=False,
                require_sponsorship_future=False,
                minimum_years_experience=False,
                authorized_to_work_unconditionally=True,
                willing_to_relocate=False,
                comfortable_working_on_site=False,
                lives_in_job_location=False,
                text_message_consent=False,
            ),
        )

        self.assertTrue(answer)

    def test_infer_custom_answer_builds_onsite_start_location_response(self):
        autofill = load_module("autofill_dover", "scripts/autofill_dover.py")

        with mock.patch.object(
            autofill,
            "build_onsite_start_location_answer",
            return_value="Yes. The soonest I could start is March 23, 2026, and I would plan to work from San Francisco.",
        ):
            answer = autofill._infer_custom_answer(
                {
                    "question": "This is an onsite job in SF or Seattle, w 1-day a week wfh flexibility. Have you taken this into consideration & still want to proceed? If so, when is the soonest you could start? And at which location?",
                    "input_type": "LONG_ANSWER",
                },
                mock.Mock(),
                out_dir=PROJECT_ROOT,
            )

        self.assertEqual(
            answer,
            "Yes. The soonest I could start is March 23, 2026, and I would plan to work from San Francisco.",
        )

    def test_answer_from_classifier_uses_sponsorship_answer_for_employment_based_status_prompt(self):
        autofill = load_module("autofill_dover", "scripts/autofill_dover.py")
        application_profile = autofill.parse_application_profile(
            (PROJECT_ROOT / "application_profile.md").read_text(encoding="utf-8")
        )

        answer = autofill._answer_from_classifier(
            (
                "Will you now or in the future require our company to file a petition or application "
                "for employment-based immigration status on your behalf to begin or continue employment "
                "with our company?"
            ),
            application_profile,
            out_dir=PROJECT_ROOT,
        )

        self.assertEqual(answer, application_profile.sponsorship_answer)

    def test_build_payload_uses_only_custom_questions_and_forwards_referrer_source(self):
        autofill = load_module("autofill_dover", "scripts/autofill_dover.py")
        job_payload = {
            "id": "ea0d0b85-3700-407e-9f59-6ddcc6b9cfb8",
            "client_name": "Suppli",
            "title": "Founding Product Manager",
            "require_linkedin_profile_url": True,
            "application_questions": [
                {
                    "id": "q-auth",
                    "question": (
                        "Do you require US work authorization / sponsorship in order to work "
                        "in the United States? (This role is not sponsoring visas)"
                    ),
                    "input_type": "MULTIPLE_CHOICE",
                    "required": True,
                    "question_type": "CUSTOM",
                    "multiple_choice_options": ["Yes", "No"],
                    "hidden": False,
                },
                {
                    "id": "q-ai",
                    "question": "Are you comfortable building prototypes? What AI tools are you using?",
                    "input_type": "LONG_ANSWER",
                    "required": True,
                    "question_type": "CUSTOM",
                    "multiple_choice_options": None,
                    "hidden": False,
                },
                {
                    "id": "q-github",
                    "question": "GitHub Profile URL",
                    "input_type": "SHORT_ANSWER",
                    "required": False,
                    "question_type": "CUSTOM",
                    "multiple_choice_options": None,
                    "hidden": False,
                },
                {
                    "id": "q-linkedin",
                    "question": "LinkedIn Profile URL",
                    "input_type": "SHORT_ANSWER",
                    "required": True,
                    "question_type": "LINKEDIN_URL",
                    "multiple_choice_options": None,
                    "hidden": False,
                },
                {
                    "id": "q-resume",
                    "question": "Resume Upload",
                    "input_type": "FILE_UPLOAD",
                    "required": True,
                    "question_type": "RESUME",
                    "multiple_choice_options": None,
                    "hidden": False,
                },
                {
                    "id": "q-phone",
                    "question": "Phone",
                    "input_type": "SHORT_ANSWER",
                    "required": False,
                    "question_type": "PHONE_NUMBER",
                    "multiple_choice_options": None,
                    "hidden": False,
                },
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / "documents").mkdir()
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps(
                    {
                        "company": "suppli",
                        "company_proper": "Suppli",
                        "jd_title": "Founding Product Manager",
                        "jd_source": "https://app.dover.com/apply/suppli/ea0d0b85-3700-407e-9f59-6ddcc6b9cfb8?rs=42706078",
                        "jd_source_resolved": "https://app.dover.com/apply/suppli/ea0d0b85-3700-407e-9f59-6ddcc6b9cfb8?rs=42706078",
                    }
                ),
                encoding="utf-8",
            )
            resume_path = out_dir / "documents" / "Jerrison Li Resume - Suppli.pdf"
            resume_path.write_bytes(b"%PDF-1.4 test")
            ai_field_name = autofill._question_field_name(job_payload["application_questions"][1])

            with mock.patch.object(autofill, "_fetch_dover_job", return_value=job_payload):
                with mock.patch.object(
                    autofill,
                    "generate_application_answers",
                    return_value={ai_field_name: ("Yes. I build prototypes with Claude, GPT-5, and Cursor.")},
                ):
                    payload = autofill._build_payload(out_dir, provider="claude")

        request_payload = payload["request"]
        self.assertEqual(request_payload["referrer_source"], "42706078")
        self.assertEqual(request_payload["job_id"], "ea0d0b85-3700-407e-9f59-6ddcc6b9cfb8")
        self.assertTrue(request_payload["linkedin_url"].startswith("https://"))
        self.assertEqual(Path(request_payload["resume_path"]).name, resume_path.name)

        custom_answers = request_payload["application_questions"]
        self.assertEqual(len(custom_answers), 3)
        self.assertEqual(
            custom_answers[0],
            {
                "id": "q-auth",
                "question": (
                    "Do you require US work authorization / sponsorship in order to work "
                    "in the United States? (This role is not sponsoring visas)"
                ),
                "answer": "No",
            },
        )
        self.assertEqual(custom_answers[1]["id"], "q-ai")
        self.assertIn("Claude", custom_answers[1]["answer"])
        self.assertEqual(custom_answers[2]["id"], "q-github")
        self.assertEqual(custom_answers[2]["answer"], "https://github.com/jerrison")

    def test_build_payload_attaches_cover_letter_when_upload_question_exists(self):
        autofill = load_module("autofill_dover", "scripts/autofill_dover.py")
        job_payload = {
            "id": "ea0d0b85-3700-407e-9f59-6ddcc6b9cfb8",
            "client_name": "Suppli",
            "title": "Founding Product Manager",
            "application_questions": [
                {
                    "id": "q-cover-letter",
                    "question": "Cover Letter",
                    "input_type": "FILE_UPLOAD",
                    "required": True,
                    "question_type": "COVER_LETTER",
                    "multiple_choice_options": None,
                    "hidden": False,
                },
                {
                    "id": "q-resume",
                    "question": "Resume Upload",
                    "input_type": "FILE_UPLOAD",
                    "required": True,
                    "question_type": "RESUME",
                    "multiple_choice_options": None,
                    "hidden": False,
                },
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / "documents").mkdir()
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps(
                    {
                        "company": "suppli",
                        "company_proper": "Suppli",
                        "jd_title": "Founding Product Manager",
                        "jd_source": "https://app.dover.com/apply/suppli/ea0d0b85-3700-407e-9f59-6ddcc6b9cfb8?rs=42706078",
                        "jd_source_resolved": "https://app.dover.com/apply/suppli/ea0d0b85-3700-407e-9f59-6ddcc6b9cfb8?rs=42706078",
                    }
                ),
                encoding="utf-8",
            )
            resume_path = out_dir / "documents" / "Jerrison Li Resume - Suppli.pdf"
            resume_path.write_bytes(b"%PDF-1.4 resume")
            cover_letter_path = out_dir / "documents" / "Jerrison Li Cover Letter - Suppli.pdf"
            cover_letter_path.write_bytes(b"%PDF-1.4 cover")

            with mock.patch.object(autofill, "_fetch_dover_job", return_value=job_payload):
                with mock.patch.object(autofill, "generate_application_answers", return_value={}):
                    payload = autofill._build_payload(out_dir, provider="claude")

        self.assertEqual(Path(payload["request"]["cover_letter_path"]).name, cover_letter_path.name)
        self.assertEqual(payload["request"]["application_questions"], [])

    def test_build_payload_writes_pending_user_input_and_aborts_for_specialized_prompt(self):
        autofill = load_module("autofill_dover", "scripts/autofill_dover.py")
        job_payload = {
            "id": "ea0d0b85-3700-407e-9f59-6ddcc6b9cfb8",
            "client_name": "Suppli",
            "title": "Founding Product Manager",
            "application_questions": [
                {
                    "id": "q-homeowners",
                    "question": "Homeowners / P&C product ownership: Describe your experience managing a homeowners or property line of business. Include market context (admitted vs E&S, state[s], peril) and your decision scope.",
                    "input_type": "LONG_ANSWER",
                    "required": True,
                    "question_type": "CUSTOM",
                    "multiple_choice_options": None,
                    "hidden": False,
                },
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps(
                    {
                        "company": "suppli",
                        "company_proper": "Suppli",
                        "jd_title": "Founding Product Manager",
                        "jd_source": "https://app.dover.com/apply/suppli/ea0d0b85-3700-407e-9f59-6ddcc6b9cfb8?rs=42706078",
                        "jd_source_resolved": "https://app.dover.com/apply/suppli/ea0d0b85-3700-407e-9f59-6ddcc6b9cfb8?rs=42706078",
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(autofill, "_fetch_dover_job", return_value=job_payload):
                with self.assertRaisesRegex(ValueError, "pending_user_input.json"):
                    autofill._build_payload(out_dir, provider="claude")

            pending_path = out_dir / "submit" / "pending_user_input.json"
            payload = json.loads(pending_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "pending_user_input")
        self.assertEqual(payload["board"], "dover")
        self.assertEqual(len(payload["questions"]), 1)

    def test_parse_error_message_detects_cloudflare_security_page(self):
        autofill = load_module("autofill_dover", "scripts/autofill_dover.py")
        html = """
        <html>
          <head><title>Just a moment...</title></head>
          <body>
            app.dover.com
            Performing security verification
            This website uses a security service to protect against malicious bots.
            Performance and Security by Cloudflare
          </body>
        </html>
        """

        message = autofill._parse_error_message(html)

        self.assertIn("Cloudflare", message)


if __name__ == "__main__":
    unittest.main()
