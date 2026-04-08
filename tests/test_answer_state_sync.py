import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class AnswerStateSyncTests(unittest.TestCase):
    def test_sync_current_attempt_answer_states_backfills_refresh_and_verification_from_current_proof(self):
        sync_mod = load_module("answer_state_sync", "scripts/answer_state_sync.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            (submit_dir / "ashby_autofill_report.json").write_text(
                json.dumps(
                    {
                        "fields": [
                            {
                                "field_name": "why_company",
                                "label": "Why this company?",
                                "status": "filled",
                                "source": "generated_application_answer",
                            },
                            {
                                "field_name": "why_now",
                                "label": "Why now?",
                                "status": "filled",
                                "source": "generated_application_answer",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (submit_dir / "application_answers.json").write_text(
                json.dumps(
                    {
                        "refresh_request_id": "refresh-123",
                        "provider": "openai",
                        "generated_at_utc": "2026-04-05T03:36:16+00:00",
                    }
                ),
                encoding="utf-8",
            )
            (submit_dir / "answer_verification.json").write_text(
                json.dumps(
                    {
                        "generated_at_utc": "2026-04-05T03:36:30+00:00",
                        "request_id": "verify-123",
                        "verifier_provider": "openai",
                        "status": "verified",
                        "summary": {
                            "approved_count": 2,
                            "retry_count": 0,
                            "blocked_count": 0,
                            "not_applicable_count": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = sync_mod.sync_current_attempt_answer_states_from_proof(out_dir, "submit")
            refresh_state = json.loads((out_dir / "answer_refresh_status.json").read_text(encoding="utf-8"))
            verification_state = json.loads((out_dir / "answer_verification_status.json").read_text(encoding="utf-8"))

        self.assertTrue(result["refresh_synced"])
        self.assertTrue(result["verification_synced"])
        self.assertEqual(refresh_state["status"], "fresh")
        self.assertEqual(refresh_state["answer_provider"], "openai")
        self.assertEqual(refresh_state["generated_answer_count"], 2)
        self.assertEqual(refresh_state["proof_submit_dir"], "submit")
        self.assertEqual(verification_state["status"], "verified")
        self.assertEqual(verification_state["verifier_provider"], "openai")
        self.assertEqual(verification_state["verified_answer_count"], 2)
        self.assertEqual(verification_state["blocked_answer_count"], 0)
        self.assertEqual(verification_state["proof_submit_dir"], "submit")

    def test_sync_current_attempt_answer_states_prefers_report_matching_answer_payload_fields(self):
        sync_mod = load_module("answer_state_sync_matching_report", "scripts/answer_state_sync.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            (submit_dir / "application_answers.json").write_text(
                json.dumps(
                    {
                        "refresh_request_id": "refresh-456",
                        "provider": "openai",
                        "generated_at_utc": "2026-04-06T23:10:00+00:00",
                        "questions": [
                            {
                                "field_name": "question_1",
                                "label": "Why this company?",
                                "required": True,
                                "type": "String",
                            }
                        ],
                        "answers": {"question_1": "Because the mission is compelling."},
                    }
                ),
                encoding="utf-8",
            )
            (submit_dir / "ashby_autofill_report.json").write_text(
                json.dumps(
                    {
                        "fields": [
                            {
                                "field_name": "application_why_company",
                                "label": "Why this company?",
                                "status": "filled",
                                "source": "generated_application_answer",
                            },
                            {
                                "field_name": "application_why_now",
                                "label": "Why now?",
                                "status": "filled",
                                "source": "generated_application_answer",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (submit_dir / "greenhouse_autofill_report.json").write_text(
                json.dumps(
                    {
                        "fields": [
                            {
                                "field_name": "question_1",
                                "label": "Why this company?",
                                "status": "filled",
                                "source": "generated_application_answer",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = sync_mod.sync_current_attempt_answer_states_from_proof(out_dir, "submit", allow_pending_override=True)
            refresh_state = json.loads((out_dir / "answer_refresh_status.json").read_text(encoding="utf-8"))

        self.assertTrue(result["refresh_synced"])
        self.assertEqual(result["llm_generated_count"], 1)
        self.assertEqual(refresh_state["status"], "fresh")
        self.assertEqual(refresh_state["generated_answer_count"], 1)
        self.assertEqual(refresh_state["proof_submit_dir"], "submit")

    def test_sync_current_attempt_answer_states_backfills_missing_answers_payload_for_deterministic_only_proof(self):
        sync_mod = load_module("answer_state_sync_backfill_answers", "scripts/answer_state_sync.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            (submit_dir / "lever_autofill_report.json").write_text(
                json.dumps(
                    {
                        "fields": [
                            {
                                "field_name": "work_auth",
                                "label": "Are you legally authorized to work in the United States?",
                                "kind": "radio",
                                "required": True,
                                "status": "filled",
                                "source": "application_profile.md",
                                "value": "Yes",
                            },
                            {
                                "field_name": "current_location",
                                "label": "Current location",
                                "kind": "text",
                                "required": True,
                                "status": "filled",
                                "source": "application_profile.md",
                                "value": "San Francisco, CA",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = sync_mod.sync_current_attempt_answer_states_from_proof(out_dir, "submit")
            answers_payload = json.loads((submit_dir / "application_answers.json").read_text(encoding="utf-8"))
            refresh_state = json.loads((out_dir / "answer_refresh_status.json").read_text(encoding="utf-8"))
            verification_state = json.loads((out_dir / "answer_verification_status.json").read_text(encoding="utf-8"))

        self.assertTrue(result["refresh_synced"])
        self.assertTrue(result["verification_synced"])
        self.assertEqual(refresh_state["status"], "not_applicable")
        self.assertEqual(verification_state["status"], "not_applicable")
        self.assertEqual(
            answers_payload["answers"],
            {
                "work_auth": "Yes",
                "current_location": "San Francisco, CA",
            },
        )
        self.assertEqual(
            answers_payload["questions"],
            [
                {
                    "field_name": "work_auth",
                    "label": "Are you legally authorized to work in the United States?",
                    "required": True,
                    "type": "radio",
                },
                {
                    "field_name": "current_location",
                    "label": "Current location",
                    "required": True,
                    "type": "text",
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
