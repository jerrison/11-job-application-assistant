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


class AnswerVerificationBackfillTests(unittest.TestCase):
    def test_backfill_missing_answer_verification_uses_current_rendered_sources(self):
        backfill = load_module("answer_verification_backfill", "scripts/answer_verification_backfill.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir(parents=True)
            (out_dir / "content").mkdir()
            (submit_dir / "application_answers.json").write_text(
                json.dumps(
                    {
                        "generated_at_utc": "2026-04-05T09:15:00+00:00",
                        "provider": "openai",
                        "questions": [
                            {
                                "field_name": "preferred_name",
                                "label": "Preferred name",
                                "description": "",
                                "required": False,
                                "type": "String",
                            },
                            {
                                "field_name": "why_company",
                                "label": "Why this company?",
                                "description": "",
                                "required": True,
                                "type": "String",
                            },
                        ],
                        "answers": {
                            "preferred_name": "Candidate",
                            "why_company": "Because the mission and product surface align with my background.",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (submit_dir / "ashby_autofill_report.json").write_text(
                json.dumps(
                    {
                        "fields": [
                            {"field_name": "preferred_name", "source": "application_profile.md"},
                            {"field_name": "why_company", "source": "generated_application_answer"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with (
                mock.patch.object(
                    backfill,
                    "load_meta",
                    return_value={"company": "evenup", "company_proper": "EvenUp", "board": "ashby"},
                ),
                mock.patch.object(
                    backfill,
                    "verify_generated_answers",
                    return_value={
                        "status": "verified",
                        "blockers": [],
                        "retry_feedback_by_field": {},
                        "questions": [],
                    },
                ) as verify,
            ):
                result = backfill.backfill_missing_answer_verification_from_current_proof(out_dir)

        self.assertEqual(result["status"], "verified")
        verify.assert_called_once()
        kwargs = verify.call_args.kwargs
        self.assertEqual([spec["field_name"] for spec in kwargs["question_specs"]], ["preferred_name", "why_company"])
        self.assertEqual(
            kwargs["answers"],
            {
                "preferred_name": "Candidate",
                "why_company": "Because the mission and product surface align with my background.",
            },
        )
        self.assertEqual(kwargs["deterministic_field_names"], {"preferred_name"})
        self.assertEqual(kwargs["meta"]["board"], "ashby")

    def test_backfill_prefers_report_matching_current_answer_payload_fields(self):
        backfill = load_module("answer_verification_backfill_matching_report", "scripts/answer_verification_backfill.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir(parents=True)
            (out_dir / "content").mkdir()
            (submit_dir / "application_answers.json").write_text(
                json.dumps(
                    {
                        "generated_at_utc": "2026-04-06T23:15:00+00:00",
                        "provider": "openai",
                        "questions": [
                            {
                                "field_name": "question_1",
                                "label": "Why this company?",
                                "description": "",
                                "required": True,
                                "type": "String",
                            }
                        ],
                        "answers": {"question_1": "Because the role fits my background."},
                    }
                ),
                encoding="utf-8",
            )
            (submit_dir / "ashby_autofill_report.json").write_text(
                json.dumps(
                    {
                        "fields": [
                            {"field_name": "application_why_company", "source": "generated_application_answer"},
                            {"field_name": "application_why_now", "source": "generated_application_answer"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (submit_dir / "greenhouse_autofill_report.json").write_text(
                json.dumps(
                    {
                        "fields": [
                            {"field_name": "question_1", "source": "generated_application_answer"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with (
                mock.patch.object(
                    backfill,
                    "load_meta",
                    return_value={"company": "acme", "company_proper": "Acme", "board": "greenhouse"},
                ),
                mock.patch.object(
                    backfill,
                    "verify_generated_answers",
                    return_value={
                        "status": "verified",
                        "blockers": [],
                        "retry_feedback_by_field": {},
                        "questions": [],
                    },
                ) as verify,
            ):
                result = backfill.backfill_missing_answer_verification_from_current_proof(out_dir)

        self.assertEqual(result["status"], "verified")
        verify.assert_called_once()
        kwargs = verify.call_args.kwargs
        self.assertEqual([spec["field_name"] for spec in kwargs["question_specs"]], ["question_1"])
        self.assertEqual(kwargs["answers"], {"question_1": "Because the role fits my background."})


if __name__ == "__main__":
    unittest.main()
