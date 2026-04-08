import importlib.util
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sample_application_profile(common):
    return common.parse_application_profile(
        """
        - Country: United States
        - Location: San Francisco, CA
        - Work Authorization Statement: I am always authorized to work in the United States unconditionally.
        - Authorized to Work Unconditionally: Yes
        - Require Sponsorship Now: No
        - Require Sponsorship in Future: No
        - Meets Minimum Years of Experience Requirement: No
        - Live In Job Location: No
        - Willing to Relocate: No
        - Comfortable Working On Site: No
        - Comfortable With Posted Salary: No
        - Sponsorship Answer: No
        - Gender: Male
        - Race or Ethnicity: Hispanic or Latino
        - Veteran Status: I am not a protected veteran
        - Disability Status: No, I do not have a disability and have not had one in the past
        - Sexual Orientation: Straight / Heterosexual
        ## Education
        - The Wharton School, University of Pennsylvania; Master of Business Administration (M.B.A.)
        - Florida State University; Bachelor of Science in Actuarial Science & Computational Science (Dual Degree)
        """
    )


class SharedPositiveFitPolicyTests(unittest.TestCase):
    def _load(self):
        return load_module("application_submit_common", "scripts/application_submit_common.py")

    def test_hybrid_setting_defaults_to_affirmative_even_when_profile_flags_are_false(self):
        common = self._load()
        policy = common.resolve_shared_question_policy(
            "Are you comfortable working in a hybrid setting?",
            _sample_application_profile(common),
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "office_attendance")
        self.assertTrue(policy.is_positive_fit)
        self.assertTrue(policy.boolean_value)
        self.assertEqual(policy.text_value, "Yes")

    def test_extensive_experience_defaults_to_affirmative(self):
        common = self._load()
        policy = common.resolve_shared_question_policy(
            "Do you have extensive experience working with Data Science and AI?",
            _sample_application_profile(common),
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "experience_confirmation")
        self.assertTrue(policy.boolean_value)
        self.assertEqual(policy.text_value, "Yes")

    def test_open_ended_shipped_prompt_does_not_use_shared_positive_fit_policy(self):
        common = self._load()
        policy = common.resolve_shared_question_policy(
            "What consumer-facing product/feature have you shipped that you are the most proud of?",
            _sample_application_profile(common),
        )

        self.assertIsNone(policy)

    def test_supported_degree_claim_resolves_affirmatively(self):
        common = self._load()
        policy = common.resolve_shared_question_policy(
            "Do you have a Bachelor's degree?",
            _sample_application_profile(common),
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "credential_claim")
        self.assertTrue(policy.boolean_value)
        self.assertTrue(policy.credential_supported)
        self.assertEqual(policy.source, "application_profile.md")

    def test_generic_certification_claim_uses_resume_support(self):
        common = self._load()
        policy = common.resolve_shared_question_policy(
            "Do you hold any industry related certifications?",
            _sample_application_profile(common),
            master_resume_text="Certifications: Associate of the Casualty Actuarial Society (ACAS)",
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "credential_claim")
        self.assertTrue(policy.boolean_value)
        self.assertTrue(policy.credential_supported)
        self.assertEqual(policy.source, "master_resume.md")

    def test_unbacked_license_claim_does_not_force_yes(self):
        common = self._load()
        policy = common.resolve_shared_question_policy(
            "Do you currently hold, or intend to hold, any FINRA licenses if employed by SoFi?",
            _sample_application_profile(common),
            master_resume_text="Certifications: Associate of the Casualty Actuarial Society (ACAS)",
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "credential_claim")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")
        self.assertFalse(policy.credential_supported)

    def test_sponsorship_remains_profile_driven(self):
        common = self._load()
        policy = common.resolve_shared_question_policy(
            "Will you now or in the future require sponsorship?",
            _sample_application_profile(common),
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "work_authorization")
        self.assertFalse(policy.boolean_value)
        self.assertFalse(policy.is_positive_fit)

    def test_immigration_case_sponsorship_prompt_remains_profile_driven(self):
        common = self._load()
        policy = common.resolve_shared_question_policy(
            (
                "Will you now, or at any time in the reasonably foreseeable future, require an employer to "
                "proceed with an immigration case in order to legally employ you? This is sometimes called "
                '"sponsorship" for employment-based immigration status, such as H-1B visa.'
            ),
            _sample_application_profile(common),
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "work_authorization")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")
        self.assertFalse(policy.is_positive_fit)

    def test_require_work_authorization_or_sponsorship_prompt_remains_profile_driven(self):
        common = self._load()
        policy = common.resolve_shared_question_policy(
            "Do you require US work authorization / sponsorship in order to work in the United States?",
            _sample_application_profile(common),
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "work_authorization")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")
        self.assertFalse(policy.is_positive_fit)

    def test_narrative_prompt_stays_non_deterministic(self):
        common = self._load()
        policy = common.resolve_shared_question_policy(
            "Do you have experience with CRM systems? Please provide details.",
            _sample_application_profile(common),
        )

        self.assertIsNone(policy)

    def test_compensation_expectations_use_shared_profile_text(self):
        common = self._load()
        profile = common.parse_application_profile(
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

        policy = common.resolve_shared_question_policy("What are your compensation expectations?", profile)

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "compensation")
        self.assertEqual(policy.text_value, "I'm open and flexible on compensation.")
        self.assertEqual(policy.source, "application_profile.md")

    def test_undergraduate_gpa_uses_shared_profile_text(self):
        common = self._load()
        profile = common.parse_application_profile(
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

        policy = common.resolve_shared_question_policy("Please list your undergraduate (Bachelor's) GPA:", profile)

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "undergraduate_gpa")
        self.assertEqual(policy.text_value, "3.8/4.0")
        self.assertEqual(policy.source, "application_profile.md")

    def test_conflict_of_interest_prompt_defaults_to_negative_disclosure(self):
        common = self._load()
        policy = common.resolve_shared_question_policy(
            "To your knowledge, were you referred to this position by a senior leader or decision-maker at a current or prospective institutional client, business partner, or vendor of Coinbase?",
            _sample_application_profile(common),
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "conflict_of_interest")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")
        self.assertFalse(policy.is_positive_fit)

    def test_government_official_conflict_prompt_defaults_to_negative_disclosure(self):
        common = self._load()
        policy = common.resolve_shared_question_policy(
            "Robinhood adheres to applicable laws and regulations in relation to government officials given inherent bribery and/or corruption risk. "
            "A government official is any person that performs a public function on any level or acts in any official capacity on behalf of a government or government owned entity. \n"
            "a)  Do you currently hold or have you held, within the last 5 years, a position as a government official?\n"
            "b)  Have you been referred or recommended for this position by a government official?\n"
            "c)  Are you related to or have a close personal relationship with a government official?",
            _sample_application_profile(common),
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "conflict_of_interest")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")
        self.assertFalse(policy.is_positive_fit)

    def test_culture_careers_optin_defaults_to_negative_disclosure(self):
        common = self._load()
        policy = common.resolve_shared_question_policy(
            "Would you like to join Intuit's Talent Community to stay up to date on future opportunities?",
            _sample_application_profile(common),
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy.category, "culture_careers_optin")
        self.assertFalse(policy.boolean_value)
        self.assertEqual(policy.text_value, "No")
        self.assertFalse(policy.is_positive_fit)


if __name__ == "__main__":
    unittest.main()
