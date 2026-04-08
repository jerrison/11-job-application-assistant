import importlib.util
import json
import sys
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
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PROFILE_TEXT = """
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


class AnswerVerifierTests(unittest.TestCase):
    def test_openai_schema_does_not_enum_field_names(self):
        verifier = load_module("answer_verifier_schema", "scripts/answer_verifier.py")
        schema = verifier.build_answer_verification_json_schema(
            [
                {
                    "field_name": "have_you_previously_worked_for_headspace/ginger?\n"
                    "have_you_previously_worked_for_headspace/ginger?",
                    "label": "Have you previously worked for Headspace/Ginger?",
                    "required": True,
                    "type": "radio",
                }
            ]
        )

        field_name_schema = schema["properties"]["questions"]["items"]["properties"]["field_name"]

        self.assertEqual(field_name_schema["type"], "string")
        self.assertNotIn("enum", field_name_schema)

    def test_user_required_question_becomes_blocker(self):
        common = load_module("application_submit_common_verify_blocker", "scripts/application_submit_common.py")
        verifier = load_module("answer_verifier", "scripts/answer_verifier.py")
        application_profile = common.parse_application_profile(PROFILE_TEXT)

        spec = {
            "field_name": "question_market_context",
            "label": "Please describe the carrier market context you would evaluate here.",
            "required": True,
            "type": "textarea",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            result = verifier.verify_generated_answers(
                out_dir=out_dir,
                meta={"board": "ashby", "company": "Acme"},
                question_specs=[spec],
                answers={"question_market_context": "I would evaluate carrier constraints and regulation."},
                application_profile=application_profile,
                deterministic_field_names=set(),
            )

            artifact = json.loads((out_dir / "submit" / "answer_verification.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["questions"][0]["verdict"], "blocked_requires_user_input")
        self.assertEqual(artifact["status"], "blocked")
        self.assertEqual(artifact["questions"][0]["field_name"], "question_market_context")
        self.assertEqual(len(result["blockers"]), 1)

    def test_user_required_question_uses_user_provided_override_without_blocker(self):
        common = load_module("application_submit_common_verify_override", "scripts/application_submit_common.py")
        verifier = load_module("answer_verifier_override", "scripts/answer_verifier.py")
        application_profile = common.parse_application_profile(PROFILE_TEXT)

        spec = {
            "field_name": "residency_permit_question",
            "label": "Do you possess a valid residency permit in the country for which the position resides?",
            "required": True,
            "type": "radio",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            result = verifier.verify_generated_answers(
                out_dir=out_dir,
                meta={"board": "workday", "company": "Acme"},
                question_specs=[spec],
                answers={"residency_permit_question": "Yes"},
                application_profile=application_profile,
                deterministic_field_names=set(),
                user_provided_field_names={"residency_permit_question"},
            )

            artifact = json.loads((out_dir / "submit" / "answer_verification.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "not_applicable")
        self.assertEqual(result["questions"][0]["verification_lane"], "deterministic_rendered_only")
        self.assertEqual(result["questions"][0]["verdict"], "not_applicable")
        self.assertEqual(result["blockers"], [])
        self.assertEqual(artifact["status"], "not_applicable")
        self.assertEqual(artifact["questions"][0]["field_name"], "residency_permit_question")
        self.assertEqual(artifact["questions"][0]["answer_text"], "Yes")

    def test_deterministic_only_questions_finalize_not_applicable(self):
        common = load_module("application_submit_common_verify_deterministic", "scripts/application_submit_common.py")
        verifier = load_module("answer_verifier_det", "scripts/answer_verifier.py")
        application_profile = common.parse_application_profile(PROFILE_TEXT)

        spec = {
            "field_name": "preferred_name",
            "label": "Preferred name",
            "required": True,
            "type": "text",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            result = verifier.verify_generated_answers(
                out_dir=out_dir,
                meta={"board": "ashby", "company": "Acme"},
                question_specs=[spec],
                answers={"preferred_name": "Candidate"},
                application_profile=application_profile,
                deterministic_field_names={"preferred_name"},
            )

        self.assertEqual(result["status"], "not_applicable")
        self.assertEqual(result["questions"][0]["verification_lane"], "deterministic_rendered_only")
        self.assertEqual(result["questions"][0]["verdict"], "not_applicable")
        self.assertEqual(result["blockers"], [])

    def test_resolve_verifier_provider_prefers_automation_providers_for_legacy_answer_artifacts(self):
        verifier = load_module("answer_verifier_provider_fallback", "scripts/answer_verifier.py")

        with (
            mock.patch.object(verifier, "automation_provider_chain", return_value=("openai", "gemini")),
            mock.patch.object(verifier, "provider_available", side_effect=lambda provider: provider == "openai"),
        ):
            self.assertEqual(verifier._resolve_verifier_provider("claude"), "openai")
            self.assertEqual(verifier._resolve_verifier_provider("codex", verifier_provider="claude"), "openai")

    def test_resolve_verifier_provider_keeps_allowed_automation_provider(self):
        verifier = load_module("answer_verifier_provider_allowed", "scripts/answer_verifier.py")

        self.assertEqual(verifier._resolve_verifier_provider("gemini-flash"), "gemini-flash")
        self.assertEqual(verifier._resolve_verifier_provider("claude", verifier_provider="gemini"), "gemini")

    def test_reference_verified_questions_finalize_verified(self):
        common = load_module("application_submit_common_verify_reference", "scripts/application_submit_common.py")
        verifier = load_module("answer_verifier_ref", "scripts/answer_verifier.py")
        application_profile = common.parse_application_profile(PROFILE_TEXT)

        spec = {
            "field_name": "why_company",
            "label": "Why this company?",
            "required": True,
            "type": "textarea",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            with mock.patch.object(
                verifier,
                "_run_reference_verifier_provider",
                return_value=(
                    {
                        "questions": [
                            {
                                "field_name": "why_company",
                                "verdict": "approved",
                                "confidence": "high",
                                "score": 0.91,
                                "summary": "Grounded in allowed sources.",
                                "source_refs": ["master_resume.md"],
                                "feedback_for_regeneration": [],
                                "rubric": {
                                    "answers_the_prompt": {"pass": True, "notes": ""},
                                    "grounded_in_allowed_sources": {"pass": True, "notes": ""},
                                    "truthful_and_non_fabricated": {"pass": True, "notes": ""},
                                    "policy_compliant": {"pass": True, "notes": ""},
                                    "tone_and_length_fit": {"pass": True, "notes": ""},
                                    "specificity": {"pass": True, "notes": ""},
                                },
                            }
                        ]
                    },
                    None,
                ),
            ):
                result = verifier.verify_generated_answers(
                    out_dir=out_dir,
                    meta={"board": "ashby", "company": "Acme"},
                    question_specs=[spec],
                    answers={"why_company": "I am excited about the role and the chance to build useful products."},
                    application_profile=application_profile,
                    deterministic_field_names=set(),
                    answer_provider="openai",
                    source_bundle={"source_refs": ["master_resume.md"]},
                )

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["questions"][0]["verification_lane"], "reference_verified_generated_text")
        self.assertEqual(result["questions"][0]["verdict"], "approved")
        self.assertEqual(result["blockers"], [])

    def test_reference_verified_question_uses_model_judge_and_records_rubric(self):
        common = load_module("application_submit_common_verify_model_approve", "scripts/application_submit_common.py")
        verifier = load_module("answer_verifier_model_approve", "scripts/answer_verifier.py")
        application_profile = common.parse_application_profile(PROFILE_TEXT)

        spec = {
            "field_name": "why_company",
            "label": "Why this company?",
            "required": True,
            "type": "textarea",
        }
        provider_payload = {
            "questions": [
                {
                    "field_name": "why_company",
                    "verdict": "approved",
                    "confidence": "high",
                    "score": 0.91,
                    "summary": "Grounded in resume and candidate context.",
                    "source_refs": ["master_resume.md", "candidate_context.md"],
                    "feedback_for_regeneration": [],
                    "rubric": {
                        "answers_the_prompt": {"pass": True, "notes": ""},
                        "grounded_in_allowed_sources": {"pass": True, "notes": ""},
                        "truthful_and_non_fabricated": {"pass": True, "notes": ""},
                        "policy_compliant": {"pass": True, "notes": ""},
                        "tone_and_length_fit": {"pass": True, "notes": ""},
                        "specificity": {"pass": True, "notes": ""},
                    },
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            with mock.patch.object(
                verifier,
                "_run_reference_verifier_provider",
                return_value=(provider_payload, None),
            ) as runner:
                result = verifier.verify_generated_answers(
                    out_dir=out_dir,
                    meta={"board": "ashby", "company": "Acme"},
                    question_specs=[spec],
                    answers={"why_company": "I like the role because it fits my automation and AI product work."},
                    application_profile=application_profile,
                    deterministic_field_names=set(),
                    answer_provider="openai",
                    source_bundle={
                        "source_refs": ["master_resume.md", "candidate_context.md"],
                        "application_profile": PROFILE_TEXT,
                    },
                )

            artifact = json.loads((out_dir / "submit" / "answer_verification.json").read_text(encoding="utf-8"))

        runner.assert_called_once()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["questions"][0]["verdict"], "approved")
        self.assertEqual(result["questions"][0]["source_refs"], ["master_resume.md", "candidate_context.md"])
        self.assertEqual(artifact["answer_provider"], "openai")
        self.assertEqual(artifact["summary"]["approved_count"], 1)

    def test_reference_verified_question_accepts_results_alias_and_rubric_list(self):
        common = load_module("application_submit_common_verify_model_results_alias", "scripts/application_submit_common.py")
        verifier = load_module("answer_verifier_model_results_alias", "scripts/answer_verifier.py")
        application_profile = common.parse_application_profile(PROFILE_TEXT)

        spec = {
            "field_name": "why_company",
            "label": "Why this company?",
            "required": True,
            "type": "textarea",
        }
        provider_payload = {
            "results": [
                {
                    "field_name": "why_company",
                    "verdict": "approved",
                    "confidence": "high",
                    "score": 0.92,
                    "summary": "Grounded in resume and candidate context.",
                    "source_refs": ["master_resume.md", "candidate_context.md"],
                    "feedback_for_regeneration": [],
                    "rubric": [
                        {
                            "criterion": "grounded_in_sources",
                            "pass": True,
                            "notes": "Supported by resume bullets and candidate context.",
                        }
                    ],
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            with mock.patch.object(
                verifier,
                "_run_reference_verifier_provider",
                return_value=(provider_payload, None),
            ):
                result = verifier.verify_generated_answers(
                    out_dir=out_dir,
                    meta={"board": "ashby", "company": "Acme"},
                    question_specs=[spec],
                    answers={"why_company": "I like the role because it fits my automation and AI product work."},
                    application_profile=application_profile,
                    deterministic_field_names=set(),
                    answer_provider="claude",
                    verifier_provider="openai",
                    source_bundle={
                        "source_refs": ["master_resume.md", "candidate_context.md"],
                        "application_profile": PROFILE_TEXT,
                    },
                )

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["questions"][0]["verdict"], "approved")
        self.assertEqual(
            result["questions"][0]["rubric"]["grounded_in_sources"]["notes"],
            "Supported by resume bullets and candidate context.",
        )

    def test_reference_verified_question_accepts_single_object_payload_and_criteria_alias(self):
        common = load_module("application_submit_common_verify_model_single_object", "scripts/application_submit_common.py")
        verifier = load_module("answer_verifier_model_single_object", "scripts/answer_verifier.py")
        application_profile = common.parse_application_profile(PROFILE_TEXT)

        spec = {
            "field_name": "why_company",
            "label": "Why this company?",
            "required": True,
            "type": "textarea",
        }
        provider_payload = {
            "field_name": "why_company",
            "verdict": "approved",
            "confidence": "high",
            "score": 0.93,
            "summary": "Grounded in resume evidence.",
            "source_refs": ["master_resume.md"],
            "feedback_for_regeneration": [],
            "rubric": [
                {
                    "criteria": "grounded_in_sources",
                    "pass": True,
                    "notes": "Supported by the supplied resume excerpt.",
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            with mock.patch.object(
                verifier,
                "_run_reference_verifier_provider",
                return_value=(provider_payload, None),
            ):
                result = verifier.verify_generated_answers(
                    out_dir=out_dir,
                    meta={"board": "ashby", "company": "Acme"},
                    question_specs=[spec],
                    answers={"why_company": "I like the role because it fits my automation and AI product work."},
                    application_profile=application_profile,
                    deterministic_field_names=set(),
                    answer_provider="gemini",
                    source_bundle={"source_refs": ["master_resume.md"]},
                )

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["questions"][0]["verdict"], "approved")
        self.assertEqual(
            result["questions"][0]["rubric"]["grounded_in_sources"]["notes"],
            "Supported by the supplied resume excerpt.",
        )

    def test_reference_verified_question_returns_retry_feedback(self):
        common = load_module("application_submit_common_verify_model_retry", "scripts/application_submit_common.py")
        verifier = load_module("answer_verifier_model_retry", "scripts/answer_verifier.py")
        application_profile = common.parse_application_profile(PROFILE_TEXT)

        spec = {
            "field_name": "why_company",
            "label": "Why this company?",
            "required": True,
            "type": "textarea",
        }
        provider_payload = {
            "questions": [
                {
                    "field_name": "why_company",
                    "verdict": "retry_with_feedback",
                    "confidence": "medium",
                    "score": 0.62,
                    "summary": "Mentions unsupported platform ownership.",
                    "source_refs": ["master_resume.md", "content/jd_parsed.json"],
                    "feedback_for_regeneration": [
                        "Remove unsupported claim about owning the platform roadmap.",
                        "Ground the answer in workflow automation and AI product experience.",
                    ],
                    "rubric": {
                        "answers_the_prompt": {"pass": True, "notes": ""},
                        "grounded_in_allowed_sources": {
                            "pass": False,
                            "notes": "Platform ownership claim is unsupported.",
                        },
                        "truthful_and_non_fabricated": {
                            "pass": False,
                            "notes": "Direct ownership claim is not present in resume materials.",
                        },
                        "policy_compliant": {"pass": True, "notes": ""},
                        "tone_and_length_fit": {"pass": True, "notes": ""},
                        "specificity": {"pass": True, "notes": ""},
                    },
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            with mock.patch.object(
                verifier,
                "_run_reference_verifier_provider",
                return_value=(provider_payload, None),
            ):
                result = verifier.verify_generated_answers(
                    out_dir=out_dir,
                    meta={"board": "ashby", "company": "Acme"},
                    question_specs=[spec],
                    answers={"why_company": "I want to own your platform roadmap from day one."},
                    application_profile=application_profile,
                    deterministic_field_names=set(),
                    answer_provider="openai",
                    source_bundle={"source_refs": ["master_resume.md", "content/jd_parsed.json"]},
                )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["questions"][0]["verdict"], "retry_with_feedback")
        self.assertEqual(
            result["retry_feedback_by_field"]["why_company"][0],
            "Remove unsupported claim about owning the platform roadmap.",
        )
        self.assertEqual(result["blockers"], [])

    def test_reference_verifier_system_failure_marks_state_failed(self):
        common = load_module("application_submit_common_verify_model_fail", "scripts/application_submit_common.py")
        verifier = load_module("answer_verifier_model_fail", "scripts/answer_verifier.py")
        application_profile = common.parse_application_profile(PROFILE_TEXT)

        spec = {
            "field_name": "why_company",
            "label": "Why this company?",
            "required": True,
            "type": "textarea",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            with mock.patch.object(
                verifier,
                "_run_reference_verifier_provider",
                return_value=(None, RuntimeError("Verifier timed out.")),
            ):
                with self.assertRaises(RuntimeError):
                    verifier.verify_generated_answers(
                        out_dir=out_dir,
                        meta={"board": "ashby", "company": "Acme"},
                        question_specs=[spec],
                        answers={"why_company": "I like the role because it fits my background."},
                        application_profile=application_profile,
                        deterministic_field_names=set(),
                        answer_provider="openai",
                        source_bundle={"source_refs": ["master_resume.md"]},
                    )

            state = json.loads((out_dir / "answer_verification_status.json").read_text(encoding="utf-8"))

        self.assertEqual(state["status"], "failed")

    def test_reference_verifier_rejects_unexpected_field_name(self):
        common = load_module("application_submit_common_verify_unexpected_field", "scripts/application_submit_common.py")
        verifier = load_module("answer_verifier_unexpected_field", "scripts/answer_verifier.py")
        application_profile = common.parse_application_profile(PROFILE_TEXT)

        spec = {
            "field_name": "why_company",
            "label": "Why this company?",
            "required": True,
            "type": "textarea",
        }
        provider_payload = {
            "questions": [
                {
                    "field_name": "why_company",
                    "verdict": "approved",
                    "confidence": "high",
                    "score": 0.91,
                    "summary": "Grounded in resume evidence.",
                    "source_refs": ["master_resume.md"],
                    "feedback_for_regeneration": [],
                    "rubric": {
                        "answers_the_prompt": {"pass": True, "notes": ""},
                        "grounded_in_allowed_sources": {"pass": True, "notes": ""},
                        "truthful_and_non_fabricated": {"pass": True, "notes": ""},
                        "policy_compliant": {"pass": True, "notes": ""},
                        "tone_and_length_fit": {"pass": True, "notes": ""},
                        "specificity": {"pass": True, "notes": ""},
                    },
                },
                {
                    "field_name": "hallucinated_extra_field",
                    "verdict": "approved",
                    "confidence": "high",
                    "score": 0.88,
                    "summary": "Unexpected extra field.",
                    "source_refs": ["master_resume.md"],
                    "feedback_for_regeneration": [],
                    "rubric": {
                        "answers_the_prompt": {"pass": True, "notes": ""},
                        "grounded_in_allowed_sources": {"pass": True, "notes": ""},
                        "truthful_and_non_fabricated": {"pass": True, "notes": ""},
                        "policy_compliant": {"pass": True, "notes": ""},
                        "tone_and_length_fit": {"pass": True, "notes": ""},
                        "specificity": {"pass": True, "notes": ""},
                    },
                },
            ]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            with mock.patch.object(
                verifier,
                "_run_reference_verifier_provider",
                return_value=(provider_payload, None),
            ):
                with self.assertRaisesRegex(ValueError, "unexpected result"):
                    verifier.verify_generated_answers(
                        out_dir=out_dir,
                        meta={"board": "ashby", "company": "Acme"},
                        question_specs=[spec],
                        answers={"why_company": "I like the role because it fits my background."},
                        application_profile=application_profile,
                        deterministic_field_names=set(),
                        answer_provider="openai",
                        source_bundle={"source_refs": ["master_resume.md"]},
                    )


if __name__ == "__main__":
    unittest.main()
