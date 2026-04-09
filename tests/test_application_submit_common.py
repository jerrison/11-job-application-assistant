import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ApplicationSubmitCommonDocumentTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module("application_submit_common", "scripts/application_submit_common.py")

    def test_module_paths_follow_runtime_home_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "runtime-home"
            with mock.patch.dict(os.environ, {"JOB_ASSETS_APP_HOME": str(runtime_root)}, clear=True):
                mod = load_module("application_submit_common_runtime_home", "scripts/application_submit_common.py")

            self.assertEqual(mod.JOBS_DB_PATH, runtime_root / "jobs.db")
            self.assertEqual(mod.MASTER_RESUME_PATH, runtime_root / "master_resume.md")
            self.assertEqual(mod.WORK_STORIES_PATH, runtime_root / "work_stories.md")
            self.assertEqual(mod.CANDIDATE_CONTEXT_PATH, runtime_root / "candidate_context.md")
            self.assertEqual(mod.APPLICATION_PROFILE_PATH, runtime_root / "application_profile.md")

    def test_find_resume_file_prefers_canonical_company_named_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            docs_dir = out_dir / "documents"
            docs_dir.mkdir(parents=True)
            (out_dir / ".pipeline_meta.json").write_text(
                '{"company_proper": "Cresta", "jd_url": "https://linkedin.com/jobs/view/1/"}',
                encoding="utf-8",
            )
            (docs_dir / "Candidate Name Resume - Cresta.pdf").write_bytes(b"%PDF-canonical")
            (docs_dir / "Candidate Name Resume - Cresta..pdf").write_bytes(b"%PDF-stale")

            resolved = self.mod.find_resume_file(out_dir)

            self.assertEqual(resolved.name, "Candidate Name Resume - Cresta.pdf")

    def test_find_cover_letter_file_prefers_canonical_company_named_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            docs_dir = out_dir / "documents"
            docs_dir.mkdir(parents=True)
            (out_dir / ".pipeline_meta.json").write_text(
                '{"company_proper": "Cresta", "jd_url": "https://linkedin.com/jobs/view/1/"}',
                encoding="utf-8",
            )
            (docs_dir / "Candidate Name Cover Letter - Cresta.pdf").write_bytes(b"%PDF-canonical")
            (docs_dir / "Candidate Name Cover Letter - Cresta..pdf").write_bytes(b"%PDF-stale")

            resolved = self.mod.find_cover_letter_file(out_dir)

            self.assertEqual(resolved.name, "Candidate Name Cover Letter - Cresta.pdf")

    def test_preferred_meta_job_url_ignores_unresolved_template_urls(self):
        resolved = self.mod.preferred_meta_job_url(
            {
                "board_url": "https://jobs.jobvite.com/nutanix/job/oWGFzfwA/{{declineUrl}}",
                "jd_source_resolved": "https://jobs.jobvite.com/nutanix/job/oWGFzfwA/{{declineUrl}}",
                "jd_source": "https://jobs.jobvite.com/nutanix/job/oWGFzfwA?utm_source=trueup.io&utm_medium=website",
            }
        )

        self.assertEqual(
            resolved,
            "https://jobs.jobvite.com/nutanix/job/oWGFzfwA?utm_source=trueup.io&utm_medium=website",
        )

    def test_resolve_shared_question_policy_returns_yes_for_startup_experience(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy("Have you worked at a startup?", profile)
        self.assertEqual(policy.category, "startup_experience")
        self.assertEqual(policy.text_value, "Yes")

    def test_resolve_shared_question_policy_treats_label_only_startup_prompt_as_confirmation(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy("Startup experience", profile)
        self.assertEqual(policy.category, "startup_experience")
        self.assertEqual(policy.text_value, "Yes")

    def test_resolve_shared_question_policy_leaves_startup_narrative_prompt_for_biography_answer(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy("Briefly describe your startup experience.", profile)
        self.assertIsNone(policy)

    def test_build_truthful_work_authorization_answer_handles_employment_based_status_wording(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        answer = self.mod.build_truthful_work_authorization_answer(
            (
                "Will you now or in the future require our company to file a petition or application "
                "for employment-based immigration status on your behalf to begin or continue employment "
                "with our company?"
            ),
            profile,
        )
        self.assertEqual(answer, profile.sponsorship_answer)

    def test_build_truthful_work_authorization_answer_answers_no_for_long_yes_no_sponsorship_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        answer = self.mod.build_truthful_work_authorization_answer(
            "Will you now or in the future require sponsorship for employment visa status (e.g., H-1B visa status, spouse visa, etc)?",
            profile,
        )

        self.assertEqual(answer, "No")

    def test_build_truthful_work_authorization_answer_answers_no_for_visa_support_extension_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        answer = self.mod.build_truthful_work_authorization_answer(
            "Will you need visa support to continue or extend your current work authorization status? (ex.. H1-B, TN, OPT, CPT, etc.)",
            profile,
        )

        self.assertEqual(answer, "No")

    def test_build_truthful_work_authorization_answer_answers_no_for_canada_only_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        answer = self.mod.build_truthful_work_authorization_answer(
            "Are you legally authorized to work in Canada?",
            profile,
        )

        self.assertEqual(answer, "No")

    def test_build_truthful_work_authorization_answer_answers_yes_for_current_authorization_without_sponsorship_prompt(
        self,
    ):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        answer = self.mod.build_truthful_work_authorization_answer(
            "Are you currently authorized to work in the United States without the need for employer sponsorship?",
            profile,
        )

        self.assertEqual(answer, "Yes")

    def test_resolve_shared_question_policy_answers_no_for_mixed_work_authorization_or_sponsorship_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Will you now or in the future require any work authorization or sponsorship?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "work_authorization")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_yes_for_current_authorization_without_sponsorship_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Are you currently authorized to work in the United States without the need for employer sponsorship?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "work_authorization")
        self.assertTrue(policy.boolean_value)
        self.assertEqual(policy.text_value, "Yes")

    def test_resolve_shared_question_policy_answers_no_for_canada_only_work_authorization_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Are you legally authorized to work in Canada?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "work_authorization")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_keeps_yes_for_us_or_canada_work_authorization_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Are you legally authorized to work in the United States or Canada?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "work_authorization")
        self.assertTrue(policy.boolean_value)
        self.assertEqual(policy.text_value, "Yes")

    def test_build_truthful_work_authorization_answer_answers_yes_for_u_s_person_boolean_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        answer = self.mod.build_truthful_work_authorization_answer(
            "Are you a U.S. person?",
            profile,
        )

        self.assertEqual(answer, "Yes")

    def test_resolve_shared_question_policy_answers_u_s_person_status_from_resume_citizenship(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "A “U.S. person” is a citizen, legal permanent resident, or legal temporary resident (i.e., a refugee or asylee) of the United States. Which of the following best describes your “U.S. person” status?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "work_authorization")
        self.assertTrue(policy.boolean_value)
        self.assertEqual(policy.text_value, "I am a U.S. person")
        self.assertEqual(policy.source, "master_resume.md")

    def test_build_truthful_work_authorization_answer_answers_no_for_restricted_country_citizenship_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        answer = self.mod.build_truthful_work_authorization_answer(
            "Are you a citizen of Cuba, Iran, North Korea or Syria?",
            profile,
        )

        self.assertEqual(answer, "No")

    def test_resolve_shared_question_policy_answers_no_for_restricted_country_citizenship_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Are you a citizen of Cuba, Iran, North Korea or Syria?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "work_authorization")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")
        self.assertEqual(policy.source, "application_profile.md")

    def test_resolve_shared_question_policy_answers_no_for_restricted_country_residency_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Do you reside in or maintain an established permanent residence in any of the following countries: Cuba, Iran, North Korea, Syria, or the following territories of Ukraine (Luhansk, Donetsk, Crimea)?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "work_authorization")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")
        self.assertEqual(policy.source, "application_profile.md")

    def test_resolve_shared_question_policy_answers_no_for_prior_employment_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Have you worked at Snowflake in the past in a Full-time, Part-time, contractor or Intern capacity?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "prior_employment")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_no_for_affirm_prior_employment_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Have you previously been employed at Affirm for any length of time?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "prior_employment")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_no_for_employee_or_contractor_history_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Have you ever worked as an employee or contractor for Varo Money or Varo Bank?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "prior_employment")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_yes_for_legal_age_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Are you of legal age to work in the country in which this position will be based?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "legal_age")
        self.assertTrue(policy.boolean_value)
        self.assertEqual(policy.text_value, "Yes")

    def test_resolve_shared_question_policy_answers_date_for_desired_start_date_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "What is your desired start date?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "availability_timing")
        self.assertRegex(policy.text_value or "", r"^\d{2}/\d{2}/\d{4}$")

    def test_resolve_shared_question_policy_answers_yes_for_background_check_consent_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Are you willing to submit a background check during the hiring process?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "background_check_consent")
        self.assertTrue(policy.boolean_value)
        self.assertEqual(policy.text_value, "Yes")

    def test_resolve_shared_question_policy_answers_yes_for_background_check_undergo_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "If selected for this job, will you be willing to undergo Centific's background check process?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "background_check_consent")
        self.assertTrue(policy.boolean_value)
        self.assertEqual(policy.text_value, "Yes")

    def test_resolve_shared_question_policy_answers_yes_for_background_check_agree_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Do you agree to Tenable's Background and Reference Check Disclosure, which will be carried out only "
            "when necessary and as permitted by law? Background checks will not be performed immediately upon "
            "your application submission.",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "background_check_consent")
        self.assertTrue(policy.boolean_value)
        self.assertEqual(policy.text_value, "Yes")

    def test_resolve_shared_question_policy_answers_yes_for_interview_ai_policy_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "AI Policy for Interviewers\n"
            "Our interview process is designed to assess a candidate's fundamental, non-AI-assisted skills. "
            "Please do not use any AI tools during any part of the interview process. Please indicate Yes if "
            "you have read and agree.",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "interview_ai_policy_consent")
        self.assertTrue(policy.boolean_value)
        self.assertEqual(policy.text_value, "Yes")

    def test_resolve_shared_question_policy_answers_no_for_relocation_assistance_requirement(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "[Relocation] Samsara will not provide relocation assistance for this role. "
            "Do you require relocation assistance?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "relocation_assistance_requirement")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_yes_for_interview_recording_consent_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            (
                "The interview may be recorded in various formats, including audio, video, and/or transcript, "
                "and reviewed by the hiring team for internal evaluation purposes only."
            ),
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "interview_recording_consent")
        self.assertTrue(policy.boolean_value)
        self.assertEqual(policy.text_value, "Yes")
        self.assertEqual(policy.source, "application_profile.md")

    def test_resolve_shared_question_policy_returns_profile_backed_travel_percentage(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Please mention how much % can travel?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "travel_percentage")
        self.assertIsNone(policy.boolean_value)
        self.assertEqual(policy.text_value, "50%")
        self.assertEqual(policy.source, "application_profile.md")

    def test_resolve_shared_question_policy_answers_no_for_prior_employment_or_consulting_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Have you worked at or been a consultant for SoFi or any company subsequently acquired by a SoFi entity (including Galileo Financial Technologies, Technisys, Wyndham Capital Mortgage, Zenbanx, 8 Securities, and/or Golden Pacific Bancorp, Clara Lending)?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "prior_employment")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_no_for_rubrik_employment_or_services_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Are you currently or have you ever been employed by Rubrik or contracted to provide services to Rubrik?",
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "prior_employment")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_no_for_current_employer_affiliation_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Are you currently employed by an Aledade Partner Practice?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "current_employer_affiliation")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_no_for_tekion_dealer_partner_affiliation_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Are you currently working for a Tekion Dealer Partner or is the dealership you are working for in "
            "process to implement Tekion?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "current_employer_affiliation")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_no_for_tekion_relationship_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Are you related to, or in a relationship with, anyone that works for Tekion? If yes, what is your "
            "relationship to them?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "conflict_of_interest")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_no_for_scout_employee_connections_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Do you have any professional or personal connections to individuals currently employed by Scout Motors "
            "or any of its subsidiaries or related entities (for example, Volkswagen Group companies), including "
            "relationships as a colleague, friend, or family member?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "conflict_of_interest")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_no_for_airwallex_relatives_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Do you have any relatives currently working for Airwallex?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "conflict_of_interest")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_no_for_hometap_family_or_household_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Is a member of your family or household a current Hometap employee, contractor, or board member?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "conflict_of_interest")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_no_for_mcafee_related_employee_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Are you related to anyone currently employed with McAfee?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "conflict_of_interest")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_yes_for_sql_proficiency_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Are you proficient in SQL?",
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "skill_confirmation")
        self.assertTrue(policy.boolean_value)
        self.assertEqual(policy.text_value, "Yes")
        self.assertEqual(policy.source, "master_resume.md")

    def test_resolve_shared_question_policy_answers_yes_for_moderate_sql_experience_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Do you have moderate SQL experience?",
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "skill_confirmation")
        self.assertTrue(policy.boolean_value)
        self.assertEqual(policy.text_value, "Yes")
        self.assertEqual(policy.source, "master_resume.md")

    def test_resolve_shared_question_policy_answers_yes_for_positive_fit_skill_confirmation_without_explicit_resume_support(
        self,
    ):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            (
                "Do you have deep expertise in the technical fundamentals of the internet "
                "(including HTTP, TCP/UDP, BGP, GRE, DNS)?"
            ),
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "skill_confirmation")
        self.assertTrue(policy.boolean_value)
        self.assertEqual(policy.text_value, "Yes")
        self.assertEqual(policy.source, "shared_positive_fit_policy")

    def test_resolve_shared_question_policy_returns_profile_location_for_city_location_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Which location are you applying for?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "city_location")
        self.assertEqual(policy.text_value, "San Francisco, CA")
        self.assertEqual(policy.source, "application_profile.md")

    def test_build_optional_retry_blank_fallback_answers_returns_null_for_optional_freeform_field(self):
        overrides = self.mod.build_optional_retry_blank_fallback_answers(
            question_specs=[
                {
                    "field_name": "question_additional_info",
                    "label": "Additional Information",
                    "description": "Anything else you'd like us to know about you? Please share it here.",
                    "required": False,
                    "type": "textarea",
                }
            ],
            answers={
                "question_additional_info": "This is mostly fine but still slightly too interpretive.",
            },
            retry_feedback_by_field={
                "question_additional_info": [
                    "Anchor the company-fit claim more directly to documented strategy.",
                ]
            },
        )

        self.assertEqual(overrides, {"question_additional_info": None})

    def test_build_optional_retry_blank_fallback_answers_keeps_required_freeform_field_blocking(self):
        overrides = self.mod.build_optional_retry_blank_fallback_answers(
            question_specs=[
                {
                    "field_name": "question_why_company",
                    "label": "Why this company?",
                    "description": "",
                    "required": True,
                    "type": "textarea",
                }
            ],
            answers={
                "question_why_company": "This is still too interpretive.",
            },
            retry_feedback_by_field={
                "question_why_company": [
                    "Anchor the answer more tightly to documented company context.",
                ]
            },
        )

        self.assertEqual(overrides, {})

    def test_resolve_shared_question_policy_answers_no_for_government_procurement_employment_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Have you ever been directly employed by (1) any government or military entity, state-owned enterprise, or publicly-funded institution, or (2) a government contractor in a role that recommended Snowflake as part of Government procurement?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "conflict_of_interest")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_no_for_hpe_public_institution_employment_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "I have United States government or public institution employment experience (federal, state, or local) either as an employee or contractor/consultant.",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "conflict_of_interest")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_no_for_employee_referral_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Were you referred by a Veeva employee?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "employee_referral")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_no_for_plain_position_referral_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Were you referred for this position?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "employee_referral")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_yes_for_truthfulness_attestation(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            (
                'By clicking "Submit Application" I certify that all statements made in this application are '
                "true and complete."
            ),
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "truthfulness_attestation")
        self.assertTrue(policy.boolean_value)
        self.assertEqual(policy.text_value, "Yes")

    def test_resolve_shared_question_policy_answers_no_for_trustly_prior_application_prompt(self):
        from job_db import init_db

        profile = self.mod.parse_application_profile(self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs_db_path = Path(tmpdir) / "jobs.db"
            init_db(jobs_db_path).close()

            with mock.patch.object(self.mod, "JOBS_DB_PATH", jobs_db_path):
                policy = self.mod.resolve_shared_question_policy(
                    "Have you ever applied for a role at Trustly before?",
                    profile,
                    company_name="Trustly",
                )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "prior_application")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_prefers_metadata_source_for_how_did_you_hear_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "How did you hear about Veeva?",
            profile,
            company_name="Veeva Systems",
            job_url="https://jobs.lever.co/veeva/example?utm_source=trueup.io&utm_medium=website&ref=trueup",
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "how_did_you_hear")
        self.assertIsNone(policy.boolean_value)
        self.assertEqual(policy.text_value, "TrueUp")
        self.assertEqual(policy.source, "job_url.utm_source")

    def test_resolve_shared_question_policy_prefers_profile_value_for_where_did_you_hear_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Where did you hear about this role?",
            profile,
            company_name="Google DeepMind",
            job_url="https://boards.greenhouse.io/deepmind/jobs/example?gh_src=linkedin",
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "how_did_you_hear")
        self.assertEqual(policy.text_value, "Corporate website")
        self.assertEqual(policy.source, "application_profile.md")

    def test_generate_application_answers_clears_stale_answers_when_no_questions_remain(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir(parents=True)
            stale_answers = submit_dir / "application_answers.json"
            stale_raw = submit_dir / "application_answers_raw.txt"
            stale_fallback = submit_dir / "application_answers_fallback_raw.txt"
            stale_answers.write_text(
                json.dumps(
                    {
                        "questions": [{"field_name": "how_did_you_hear", "label": "Where did you hear about this role?"}],
                        "answers": {"how_did_you_hear": "LinkedIn"},
                    }
                ),
                encoding="utf-8",
            )
            stale_raw.write_text("stale raw", encoding="utf-8")
            stale_fallback.write_text("stale fallback", encoding="utf-8")

            answers = self.mod.generate_application_answers(
                out_dir=out_dir,
                meta={"company": "deepmind"},
                question_specs=[],
                provider="openai",
            )

            self.assertEqual(answers, {})
            self.assertFalse(stale_answers.exists())
            self.assertFalse(stale_raw.exists())
            self.assertFalse(stale_fallback.exists())

    def test_resolve_shared_question_policy_prefers_profile_value_for_how_did_you_first_hear_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "How did you first hear about this opportunity?",
            profile,
            company_name="Pinterest",
            job_url="https://boards.greenhouse.io/pinterest/jobs/example?gh_src=linkedin",
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "how_did_you_hear")
        self.assertEqual(policy.text_value, "Corporate website")
        self.assertEqual(policy.source, "application_profile.md")

    def test_resolve_shared_question_policy_answers_no_for_deloitte_independence_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Are you currently employed with or have been employed by Deloitte? Deloitte is our external financial auditor and due diligence may be performed prior to employment to ensure independence requirements are met. If you indicate yes, you agree that certain information may need to be disclosed to Deloitte before employment to confirm independence.",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "prior_employment")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_no_for_current_company_employee_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Are you currently a SoFi, Galileo or Technisys employee?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "current_employer_affiliation")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_no_for_career_marketing_opt_in_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Would you like to receive marketing communications about careers at SoFi and Galileo?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "culture_careers_optin")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_resolve_shared_question_policy_answers_no_for_future_job_openings_opt_in_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Please email me about future job openings",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "culture_careers_optin")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")

    def test_build_truthful_work_authorization_answer_answers_no_for_h1b_history_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        answer = self.mod.build_truthful_work_authorization_answer(
            (
                "Have you held H-1B status, or had an H-1B petition approved on your behalf, "
                "within the preceding 6 years for an employer other than a cap exempt institution?"
            ),
            profile,
        )

        self.assertEqual(answer, "No")

    def test_resolve_shared_question_policy_answers_yes_for_current_country_without_restrictions_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Are you able to work in your current country of residence without restrictions?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "work_authorization")
        self.assertTrue(policy.boolean_value)
        self.assertEqual(policy.text_value, "Yes")

    def test_resolve_shared_question_policy_answers_no_for_explicit_named_location_residency_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Do you currently reside in the Washington, D.C. metro area?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "location_residency")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")
        self.assertEqual(policy.source, "application_profile.md")

    def test_resolve_shared_question_policy_answers_yes_for_pm_people_management_threshold_from_profile(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Do you have 2+ years managing a team of product managers?",
            profile,
        )

        self.assertIsNotNone(policy)
        assert policy is not None
        self.assertEqual(policy.category, "pm_people_management")
        self.assertEqual(policy.source, "application_profile.md")
        self.assertEqual(policy.text_value, "Yes")
        self.assertTrue(policy.is_positive_fit)

    def test_resolve_shared_question_policy_answers_no_when_pm_people_management_threshold_exceeds_profile(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Do you have 4+ years managing a team of product managers?",
            profile,
        )

        self.assertIsNotNone(policy)
        assert policy is not None
        self.assertEqual(policy.category, "pm_people_management")
        self.assertEqual(policy.source, "application_profile.md")
        self.assertEqual(policy.text_value, "No")
        self.assertFalse(policy.is_positive_fit)

    def test_shared_text_answer_returns_none_for_current_finra_license_inventory_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        answer = self.mod.shared_text_answer_for_question(
            "What FINRA license(s), if any, do you currently hold?",
            profile,
        )

        self.assertEqual(answer, "None")

    def test_shared_text_answer_returns_resume_backed_current_professional_certification_inventory_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        answer = self.mod.shared_text_answer_for_question(
            "Please list any relevant professional certifications and/or licenses you currently hold.",
            profile,
        )

        self.assertEqual(answer, "Associate of the Casualty Actuarial Society (ACAS)")

    def test_shared_text_answer_returns_source_backed_product_analysis_tools_inventory(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answer = self.mod.shared_text_answer_for_question(
            "Which analytical tools or languages have you used for product analysis before?",
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertEqual(
            answer,
            (
                "I have used Python, SQL, an in-house A/B testing platform, analytics dashboards, "
                "session recordings, support ticket analysis, and user interviews for product analysis."
            ),
        )

    def test_shared_text_answer_returns_cautious_customer_experience_helpdesk_tools_answer(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answer = self.mod.shared_text_answer_for_question(
            "What customer experience / helpdesk tools have you implemented or integrated into an organization? If none, put N/A.",
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertEqual(
            answer,
            (
                "I have not implemented a named enterprise helpdesk platform. My closest relevant work has "
                "been improving an AI chatbot at Moody's, implementing automated chat workflows at Kyte, "
                "and building a self-service onboarding portal at T-Mobile."
            ),
        )

    def test_classified_shared_answers_uses_specialized_helpdesk_answer_for_detail_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answers = self.mod._classified_shared_answers(
            [
                {
                    "field_name": "question_helpdesk_tools",
                    "label": "What customer experience / helpdesk tools have you implemented or integrated into an organization? If none, put N/A.",
                    "type": "textarea",
                }
            ],
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertIn("question_helpdesk_tools", answers)
        self.assertIn("AI chatbot", answers["question_helpdesk_tools"])
        self.assertIn("Kyte", answers["question_helpdesk_tools"])

    def test_shared_text_answer_returns_source_backed_global_teams_answer(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answer = self.mod.shared_text_answer_for_question(
            "What is your experience working with global teams? If yes, please elaborate.",
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertIn("cross-cultural collaboration", answer)
        self.assertIn("Panama", answer)
        self.assertIn("Spanish, English, Cantonese, and Mandarin", answer)
        self.assertIn("Moody's, Kyte, and T-Mobile", answer)

    def test_classified_shared_answers_uses_specialized_global_teams_answer(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answers = self.mod._classified_shared_answers(
            [
                {
                    "field_name": "question_global_teams",
                    "label": "What is your experience working with global teams? If yes, please elaborate.",
                    "type": "textarea",
                }
            ],
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertIn("question_global_teams", answers)
        self.assertIn("Panama", answers["question_global_teams"])
        self.assertIn("Moody's, Kyte, and T-Mobile", answers["question_global_teams"])

    def test_shared_text_answer_returns_source_backed_fraud_risk_compliance_answer(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answer = self.mod.shared_text_answer_for_question(
            "Where have you worked on Fraud, Risk, or Compliance Case Management products?",
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertIn("Kyte and Lyft", answer)
        self.assertIn("ML risk engine", answer)
        self.assertIn("risk management function", answer)

    def test_shared_text_answer_returns_source_backed_behavioral_analytics_answer(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answer = self.mod.shared_text_answer_for_question(
            "Where have you worked on behavioral analytics products?",
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertIn("Kyte and Lyft", answer)
        self.assertIn("session recordings", answer)
        self.assertIn("behavioral analytics", answer)

    def test_shared_text_answer_returns_profile_backed_travel_percentage(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        answer = self.mod.shared_text_answer_for_question(
            "Please mention how much % can travel?",
            profile,
        )

        self.assertEqual(answer, "50%")

    def test_shared_text_answer_returns_profile_backed_notice_period(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        answer = self.mod.shared_text_answer_for_question(
            "What is your current notice period?",
            profile,
        )

        self.assertEqual(answer, "2 weeks")

    def test_shared_text_answer_returns_default_skill_years_for_known_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        answer = self.mod.shared_text_answer_for_question(
            "How many years of work experience do you have with Python (Programming Language)?",
            profile,
        )

        self.assertEqual(answer, "10")

    def test_shared_text_answer_returns_default_skill_years_for_alias_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        profile.skill_years = {**(profile.skill_years or {}), "engineering": "7"}
        profile.default_skill_years = "10"
        answer = self.mod.shared_text_answer_for_question(
            "How many years of ENG experience do you currently have?",
            profile,
        )

        self.assertEqual(answer, "10")

    def test_shared_text_answer_returns_default_skill_years_for_unlisted_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        answer = self.mod.shared_text_answer_for_question(
            "How many years of work experience do you have with Rust?",
            profile,
        )

        self.assertEqual(answer, "10")

    def test_resolve_shared_question_policy_derives_growth_experience_years_from_resume(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        policy = self.mod.resolve_shared_question_policy(
            "How many years of activation, onboarding and growth experience do you have?",
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertIsNotNone(policy)
        assert policy is not None
        self.assertEqual(policy.category, "skill_years_experience")
        self.assertEqual(policy.text_value, "6")
        self.assertEqual(policy.source, "master_resume.md")

    def test_resolve_shared_question_policy_derives_support_tooling_years_from_resume(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        policy = self.mod.resolve_shared_question_policy(
            "How many years of experience do you have of Customer Support tooling experience in a fast-paced SaaS environment or related?",
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertIsNotNone(policy)
        assert policy is not None
        self.assertEqual(policy.category, "skill_years_experience")
        self.assertEqual(policy.text_value, "6")
        self.assertEqual(policy.source, "master_resume.md")

    def test_resolve_shared_question_policy_derives_ai_support_tooling_years_from_resume(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        policy = self.mod.resolve_shared_question_policy(
            "How many years of  proven experience do you have managing AI support tools or LLM-based platforms (e.g., Decagon, Happy Robot, Intercom Fin, or similar)?",
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertIsNotNone(policy)
        assert policy is not None
        self.assertEqual(policy.category, "skill_years_experience")
        self.assertEqual(policy.text_value, "2")
        self.assertEqual(policy.source, "master_resume.md")

    def test_resume_domain_years_ignores_fragment_substrings_inside_unrelated_words(self):
        resume_text = """
        ACME — Product Manager
        Jan 2024 - Dec 2024
        * Improved storage provisioning workflows for enterprise infrastructure customers.

        BETA — Product Manager
        Jan 2023 - Dec 2023
        * Improved support chatbot retrieval quality using RAG architecture updates.
        """

        answer = self.mod._resume_domain_years_answer(
            "How many years of  proven experience do you have managing AI support tools or LLM-based platforms (e.g., Decagon, Happy Robot, Intercom Fin, or similar)?",
            master_resume_text=resume_text,
        )

        self.assertEqual(answer, "1")

    def test_shared_text_answer_returns_source_backed_change_reason_answer(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answer = self.mod.shared_text_answer_for_question(
            "What's reason you are looking for a change? Why are you leaving or left your previous company?",
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertIn("not looking for a change because of a problem at Moody's Analytics", answer)
        self.assertIn("financial infrastructure and payments", answer)

    def test_shared_text_answer_returns_source_backed_recent_cybersecurity_product_answer(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answer = self.mod.shared_text_answer_for_question(
            "Tell us about a recent cybersecurity product you helped bring to market:",
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertIn("SlipStream", answer)
        self.assertIn("60 minutes to 5", answer)
        self.assertNotIn("beta design", answer)

    def test_shared_text_answer_returns_source_backed_ai_llm_impact_examples(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answer = self.mod.shared_text_answer_for_question(
            (
                "AI/LLM Impact: Describe two specific examples where AI improved your work output "
                "(speed, quality, clarity, decision-making). Include what you were trying to do, "
                "what you asked the tool, and what changed as a result."
            ),
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertIn("Claude Code", answer)
        self.assertIn("IRP Navigator", answer)
        self.assertIn("31%", answer)

    def test_question_requires_pending_user_input_for_required_phonetic_name_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        reason = self.mod.question_requires_pending_user_input(
            "So we can pronounce it correctly, what is the phonetic spelling of your name? "
            "(e.g., Kristina would be chris-teen-uh)",
            profile,
        )

        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("pronunciation", reason.lower())

    def test_pending_user_input_reason_for_optional_phonetic_name_prompt_returns_none(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        reason = self.mod.pending_user_input_reason_for_spec(
            {
                "required": False,
                "label": "(Optional) Personal Preferences",
                "description": "How do you pronounce your name?",
                "type": "input_text",
            },
            profile,
        )

        self.assertIsNone(reason)

    def test_question_requires_pending_user_input_for_candidate_ai_guidance_attestation(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        reason = self.mod.question_requires_pending_user_input(
            (
                "AI Policy for Application\n"
                "We invite you to review our AI partnership guidelines for candidates and confirm your "
                "understanding by selecting Yes."
            ),
            profile,
        )

        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("ai-usage guidance", reason.lower())
        self.assertIn("confirmation", reason.lower())

    def test_question_requires_pending_user_input_for_prior_company_interview_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        reason = self.mod.question_requires_pending_user_input(
            "Have you ever interviewed at Anthropic before?",
            profile,
        )

        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("interview history", reason.lower())

    def test_question_requires_pending_user_input_skips_company_relationship_prompt_when_policy_answers_no(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        reason = self.mod.question_requires_pending_user_input(
            (
                "Do you have any family or personal connection with current or former employees at PayJoy? "
                "If yes please specify the name and relationship."
            ),
            profile,
        )

        self.assertIsNone(reason)

    def test_question_requires_pending_user_input_skips_noncompete_prompt_when_policy_answers_no(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        reason = self.mod.question_requires_pending_user_input(
            (
                "Are you subject to any restriction that could prevent or limit your ability to work for "
                "Scout Motors (e.g., non-compete agreement with your former employer)?"
            ),
            profile,
        )

        self.assertIsNone(reason)

    def test_question_requires_pending_user_input_for_professional_certification_inventory_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        reason = self.mod.question_requires_pending_user_input(
            "Please list any relevant professional certifications and/or licenses you currently hold.",
            profile,
        )

        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("certifications", reason.lower())

    def test_question_requires_pending_user_input_for_referral_detail_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        reason = self.mod.question_requires_pending_user_input(
            "Were you referred by a current or former Jama Software employee? If yes, please list their name below.",
            profile,
        )

        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("referral detail", reason.lower())

    def test_question_requires_pending_user_input_for_restricted_country_citizenship_prompt_when_profile_missing(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        profile.citizen_of_cuba_iran_north_korea_or_syria = None

        reason = self.mod.question_requires_pending_user_input(
            "Are you a citizen of Cuba, Iran, North Korea or Syria?",
            profile,
        )

        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("cuba", reason.lower())
        self.assertIn("syria", reason.lower())

    def test_question_requires_pending_user_input_for_travel_percentage_prompt_when_profile_missing(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        profile.maximum_travel_percentage = None

        reason = self.mod.question_requires_pending_user_input(
            "Please mention how much % can travel?",
            profile,
        )

        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("travel", reason.lower())
        self.assertIn("percentage", reason.lower())

    def test_question_requires_pending_user_input_for_interview_recording_prompt_when_profile_missing(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        profile.interview_recording_consent = None

        reason = self.mod.question_requires_pending_user_input(
            (
                "The interview may be recorded in various formats, including audio, video, and/or transcript, "
                "and reviewed by the hiring team for internal evaluation purposes only."
            ),
            profile,
        )

        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("record", reason.lower())
        self.assertIn("consent", reason.lower())

    def test_question_requires_pending_user_input_for_skill_years_prompt_when_profile_missing(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        profile.skill_years = None
        profile.default_skill_years = None

        reason = self.mod.question_requires_pending_user_input(
            "How many years of work experience do you have with Python (Programming Language)?",
            profile,
        )

        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("years", reason.lower())
        self.assertIn("python", reason.lower())

    def test_question_requires_pending_user_input_skips_outside_commitment_disclosure_prompt_when_policy_answers_no(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        reason = self.mod.question_requires_pending_user_input(
            (
                "Are you currently engage in any side businesses, hold board positions, "
                "serve in nonprofit roles, maintain academic commitments, or have any other obligations "
                "that you anticipate continuing while employed with us? If yes, please provide details."
            ),
            profile,
        )

        self.assertIsNone(reason)

    def test_question_requires_pending_user_input_for_financial_product_disclosure_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        reason = self.mod.question_requires_pending_user_input(
            "Have you owned or contributed to lending, financing, or credit products? If yes, list the role and product type.",
            profile,
        )

        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("credit-product experience", reason.lower())

    def test_shared_text_answer_answers_no_for_future_finra_license_intent_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        answer = self.mod.shared_text_answer_for_question(
            "Do you currently hold, or intend to hold, any FINRA licenses if employed by SoFi?",
            profile,
        )

        self.assertEqual(answer, "No")

    def test_resolve_shared_question_policy_answers_no_for_unsupported_professional_license_intent_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Do you currently hold, or intend to hold, any FINRA licenses if employed by SoFi?",
            profile,
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "credential_claim")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")
        self.assertEqual(policy.source, "deterministic_no_professional_credentials")

    def test_resolve_shared_question_policy_answers_yes_for_completed_bachelors_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        policy = self.mod.resolve_shared_question_policy(
            "Have you completed the following level of education: Bachelor's Degree?",
            profile,
        )

        self.assertIsNotNone(policy)
        assert policy is not None
        self.assertEqual(policy.category, "education")
        self.assertEqual(policy.text_value, "Yes")
        self.assertEqual(policy.source, "application_profile.md")

    def test_normalize_multi_select_answers_defaults_to_three_when_prompt_is_preference_like(self):
        specs = [
            {
                "field_name": "focus_areas",
                "label": "Which product areas are you most interested in?",
                "type": "multi_value_multi_select",
                "values": [
                    {"label": "Growth"},
                    {"label": "Platform"},
                    {"label": "AI"},
                    {"label": "Security"},
                ],
            }
        ]
        normalized = self.mod.normalize_multi_select_generated_answers(
            specs,
            {"focus_areas": ["Growth"]},
        )
        self.assertEqual(normalized["focus_areas"], ["Growth", "Platform", "AI"])

    def test_normalize_multi_select_answers_canonicalizes_none_alias_against_string_options(self):
        specs = [
            {
                "field_name": "finra_licenses",
                "label": "What FINRA license(s), if any, do you currently hold?",
                "type": "multi_value_multi_select",
                "options": ["N/A", "Series 7 (S7)"],
            }
        ]

        normalized = self.mod.normalize_multi_select_generated_answers(
            specs,
            {"finra_licenses": ["None"]},
        )

        self.assertEqual(normalized["finra_licenses"], ["N/A"])

    def test_classified_shared_answers_maps_prior_employment_multi_select_to_negative_option_label(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answers = self.mod._classified_shared_answers(
            [
                {
                    "field_name": "have_you_ever_worked_at_adobe_in_the_following_capacity",
                    "label": "Have you ever worked at Adobe in the following capacity:",
                    "type": "multi_value_multi_select",
                    "options": [
                        "Employee",
                        "Intern",
                        "Temporary Agency or Vendor",
                        "Other",
                        "I have not worked for Adobe in the past.",
                    ],
                }
            ],
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
            company_name="Adobe",
        )

        self.assertEqual(
            answers["have_you_ever_worked_at_adobe_in_the_following_capacity"],
            ["I have not worked for Adobe in the past."],
        )

    def test_classified_shared_answers_maps_rubrik_prior_employment_single_select_to_negative_option_label(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answers = self.mod._classified_shared_answers(
            [
                {
                    "field_name": "question_61704073",
                    "label": "Are you currently or have you ever been employed by Rubrik or contracted to provide services to Rubrik?",
                    "type": "multi_value_single_select",
                    "options": [
                        "I have never been employed by Rubrik or contracted to provide services to Rubrik",
                        "I am currently a full/part-time employee for Rubrik",
                        "I am a currently a contractor for Rubrik",
                        "I was previously a full/part-time employee for Rubrik",
                        "I was previously a contractor for Rubrik",
                    ],
                }
            ],
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
            company_name="Rubrik",
        )

        self.assertEqual(
            answers["question_61704073"],
            "I have never been employed by Rubrik or contracted to provide services to Rubrik",
        )

    def test_classified_shared_answers_uses_source_backed_affiliation_option(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answers = self.mod._classified_shared_answers(
            [
                {
                    "field_name": "please_select_any_afflilations",
                    "label": "Please select any Afflilations.",
                    "type": "multi_value_multi_select",
                    "options": ["Latinas in Tech", "Wharton Alumni Familia", "None"],
                }
            ],
            profile,
            candidate_context_text=(
                "* I am a board member and vice president of corporate sponsorship at the Wharton Alumni Familia."
            ),
        )

        self.assertEqual(
            answers["please_select_any_afflilations"],
            ["Wharton Alumni Familia"],
        )

    def test_classified_shared_answers_uses_truthful_none_affiliation_option_when_supported_affiliation_is_absent(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answers = self.mod._classified_shared_answers(
            [
                {
                    "field_name": "please_select_any_afflilations",
                    "label": "Please select any Afflilations.",
                    "type": "multi_value_multi_select",
                    "options": ["Latinas in Tech", "Women in Product", "I am not affiliated with any of these groups"],
                }
            ],
            profile,
            candidate_context_text=(
                "* I am a board member and vice president of corporate sponsorship at the Wharton Alumni Familia."
            ),
        )

        self.assertEqual(
            answers["please_select_any_afflilations"],
            ["I am not affiliated with any of these groups"],
        )

    def test_classified_shared_answers_leaves_affiliation_prompt_for_generation_without_supported_or_none_option(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answers = self.mod._classified_shared_answers(
            [
                {
                    "field_name": "please_select_any_afflilations",
                    "label": "Please select any Afflilations.",
                    "type": "multi_value_multi_select",
                    "options": ["Latinas in Tech", "Women in Product"],
                }
            ],
            profile,
            candidate_context_text=(
                "* I am a board member and vice president of corporate sponsorship at the Wharton Alumni Familia."
            ),
        )

        self.assertEqual(answers, {})

    def test_classified_shared_answers_defers_positive_fit_textarea_prompts_to_generation(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answers = self.mod._classified_shared_answers(
            [
                {
                    "field_name": "have_you_shipped_b2c",
                    "label": "Have you shipped B2C / consumer digital products with measurable impact on engagement, retention, completion, revenue, or similar product outcomes?",
                    "type": "textarea",
                }
            ],
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertEqual(answers, {})

    def test_classified_shared_answers_returns_specific_relocation_text_answer(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answers = self.mod._classified_shared_answers(
            [
                {
                    "field_name": "owner_location_fit",
                    "label": "Are you currently based in or near San Francisco (for periodic collaboration at our office) - or willing to relocate?",
                    "type": "input_text",
                }
            ],
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertEqual(
            answers,
            {
                "owner_location_fit": (
                    "Yes. I live in San Francisco, CA and am open to relocation as needed."
                )
            },
        )

    def test_classified_shared_answers_defers_conditional_followup_text_prompts_to_generation(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answers = self.mod._classified_shared_answers(
            [
                {
                    "field_name": "wing_relationship_details",
                    "label": (
                        "Do you have any relatives or personal relationships working at Wing? "
                        "If yes, please provide their name(s), department(s) and relationship(s) to you."
                    ),
                    "type": "input_text",
                }
            ],
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertEqual(answers, {})

    def test_classified_shared_answers_does_not_answer_conditional_prior_employment_details(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answers = self.mod._classified_shared_answers(
            [
                {
                    "field_name": "trade_desk_work_email",
                    "label": (
                        '(Required if you answered "yes" to "have you been employed with The Trade Desk?") '
                        "What was your TTD work email?"
                    ),
                    "type": "input_text",
                    "optional": True,
                }
            ],
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
            company_name="The Trade Desk",
        )

        self.assertEqual(answers, {})

    def test_education_discipline_option_matches_supported_majors_from_profile(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        matched = self.mod.education_discipline_option_matches(
            "What is your major?",
            ["Business Administration", "Computer Science/Software Engineering", "Other"],
            profile,
        )

        self.assertEqual(matched, ["Business Administration", "Computer Science/Software Engineering"])

    def test_education_level_option_matches_highest_degree_from_profile(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        matched = self.mod.education_level_option_matches(
            "What is your highest level of completed education?",
            ["High school diploma", "GED", "Bachelors Degree", "Masters Degree", "PHD"],
            profile,
        )

        self.assertEqual(matched, ["Masters Degree"])

    def test_select_truthful_age_option_matches_overlapping_bucket(self):
        selected = self.mod.select_truthful_age_option(
            ["Under 30", "30-39", "40-49", "50-59"],
            "35 - 44",
        )

        self.assertEqual(selected, "30-39")

    def test_shared_text_answer_returns_source_backed_proud_consumer_feature_story(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        master_resume_text = (
            "KYTE — Staff Product Manager\n"
            "* Built company's first ML risk engine from 0-to-1, reducing losses 23% and boosting revenue 7%.\n"
        )
        work_stories_text = (
            "ML-based verification risk engine\n"
            "We had a rules-based verification system screening customers post-booking.\n"
            "It was blocking around 12% of completed bookings.\n"
            "The results validated the thesis. Losses dropped 23% while revenue increased 7%.\n"
        )

        answer = self.mod.shared_text_answer_for_question(
            "What consumer-facing product/feature have you shipped that you are the most proud of?",
            profile,
            master_resume_text=master_resume_text,
            work_stories_text=work_stories_text,
        )

        self.assertEqual(
            answer,
            (
                "At Kyte, I'm most proud of shipping the post-booking verification flow powered by a new "
                "ML risk engine. We replaced a rules-based system that was blocking about 12% of completed "
                "bookings, and I partnered with a data scientist and engineer to get the model into "
                "production. After launch, losses fell 23% and revenue increased 7% from previously "
                "blocked good customers."
            ),
        )

    def test_shared_text_answer_returns_candidate_context_writing_sample_links(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp:
            candidate_context_path = Path(tmp) / "candidate_context.md"
            candidate_context_path.write_text(
                (
                    "Candidate Name Context\n"
                    "Writing Samples:\n"
                    "1. https://example.com/sample-one\n"
                    "2. https://example.com/sample-two\n"
                    "Work Material\n"
                ),
                encoding="utf-8",
            )

            with mock.patch.object(self.mod, "CANDIDATE_CONTEXT_PATH", candidate_context_path):
                answer = self.mod.shared_text_answer_for_question(
                    "Please share links to any writing samples that you have",
                    profile,
                )

        self.assertEqual(answer, "https://example.com/sample-one\nhttps://example.com/sample-two")

    def test_shared_text_answer_returns_repo_backed_language_proficiencies(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answer = self.mod.shared_text_answer_for_question(
            "Please indicate your language proficiencies and the level of fluency for each.",
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
        )

        self.assertEqual(
            answer,
            "Spanish (native), English (fluent), Cantonese (native), Mandarin (advanced)",
        )

    def test_shared_text_answer_returns_detailed_ai_agent_and_rag_answer(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answer = self.mod.shared_text_answer_for_question(
            (
                "Have you used AI agents such as Cursor or Claude Code to build software? "
                "Have you used markdown files within your codebase to guide the behavior of the coding agent? "
                "Have you ever built a system that uses large language models and/or RAG to solve a problem "
                "or answer a user query?"
            ),
            profile,
        )

        self.assertEqual(
            answer,
            (
                "Yes. I regularly use AI coding agents such as Claude Code and Codex to prototype, "
                "automate workflows, and accelerate product and engineering work. I also work in "
                "codebases that use markdown instruction files to guide agent behavior, workflow "
                "constraints, and quality checks. Professionally, I launched SlipStream at Moody's, "
                "a multi-agent LLM system that turns unstructured insurance documents into structured "
                "underwriting data, and I have also improved RAG-based support experiences and built "
                "smaller AI workflow automations personally. So yes across all three areas, with "
                "hands-on experience in both production systems and day-to-day development workflows."
            ),
        )

    def test_shared_text_answer_returns_ai_workflow_usage_story(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answer = self.mod.shared_text_answer_for_question(
            (
                "Describe a specific example of how you've used Gen AI tools in your product work, "
                "including the tools used, the problem you were solving, and the impact."
            ),
            profile,
        )

        self.assertEqual(
            answer,
            (
                "At Moody's, I used Claude Code with Figma to prototype a workflow solution for a "
                "$15M at-risk enterprise account. I built the prototype end to end with AI-assisted "
                "tooling, then hosted it in AWS so customers could interact with it directly and give "
                "feedback. The problem was that we needed fast evidence to resolve a product and "
                "engineering disagreement and keep the customer from churning. That prototype let us "
                "validate the direction quickly, align stakeholders on a scoped path forward, and help "
                "retain the account."
            ),
        )

    def test_shared_text_answer_returns_ai_tool_inventory_story(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answer = self.mod.shared_text_answer_for_question(
            (
                "AI/LLM Usage: Which AI/LLM/agent tools have you used in the last 6 months? "
                "For each, note frequency (daily, weekly, occasional) and what you use it for."
            ),
            profile,
            master_resume_text=self.mod.MASTER_RESUME_PATH.read_text(encoding="utf-8"),
            work_stories_text=self.mod.WORK_STORIES_PATH.read_text(encoding="utf-8"),
        )

        self.assertIsNotNone(answer)
        assert answer is not None
        self.assertIn("Claude Code", answer)
        self.assertIn("Codex", answer)
        self.assertIn("SlipStream", answer)
        self.assertIn("IRP Navigator", answer)

    def test_shared_text_answer_does_not_treat_name_pronunciation_as_pronouns(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answer = self.mod.shared_text_answer_for_question(
            "(Optional) Personal Preferences\nHow do you pronounce your name?",
            profile,
        )

        self.assertIsNone(answer)

    def test_shared_text_answer_returns_full_name_for_preferred_first_and_last_name_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answer = self.mod.shared_text_answer_for_question(
            "What is your preferred first name and last name? If your legal first and last name is your preferred name, you do not need to respond.",
            profile,
        )

        self.assertEqual(answer, "Candidate Name")

    def test_shared_text_answer_returns_full_name_for_legal_first_and_last_name_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answer = self.mod.shared_text_answer_for_question(
            "Please provide your legal first and last name.",
            profile,
        )

        self.assertEqual(answer, "Candidate Name")

    def test_shared_text_answer_returns_candidate_email_for_confirm_email_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answer = self.mod.shared_text_answer_for_question(
            "Confirm your email address",
            profile,
        )

        self.assertEqual(answer, "candidate@example.com")

    def test_shared_text_answer_returns_live_application_date_for_today_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        fake_now = self.mod.datetime(2026, 4, 5, 12, 0, 0, tzinfo=self.mod.UTC)
        with mock.patch.object(self.mod, "datetime", wraps=self.mod.datetime) as mocked_datetime:
            mocked_datetime.now.return_value = fake_now
            answer = self.mod.shared_text_answer_for_question(
                "Today's Date of Application (MM/DD/YY Format)",
                profile,
            )

        self.assertEqual(answer, "04/05/26")

    def test_shared_text_answer_does_not_substitute_generic_profile_for_explicit_platform_profile_url(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answer = self.mod.shared_text_answer_for_question(
            "Replit Profile URL",
            profile,
        )

        self.assertIsNone(answer)

    def test_shared_text_answer_does_not_substitute_generic_links_for_platform_specific_build_prompt(self):
        profile = self.mod.parse_application_profile(
            self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
        )

        answer = self.mod.shared_text_answer_for_question(
            "If you want to share something you built with Replit please share below.",
            profile,
        )

        self.assertIsNone(answer)


if __name__ == "__main__":
    unittest.main()
