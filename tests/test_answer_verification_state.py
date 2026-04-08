import importlib.util
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


class AnswerVerificationStateTests(unittest.TestCase):
    def test_missing_state_defaults_to_unknown(self):
        verify = load_module("answer_verification_state", "scripts/answer_verification_state.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            state = verify.load_answer_verification_state(Path(tmpdir))

        self.assertEqual(state["status"], verify.STATUS_UNKNOWN)
        self.assertIsNone(state["request_id"])

    def test_mark_pending_supersedes_prior_request(self):
        verify = load_module("answer_verification_state", "scripts/answer_verification_state.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            first = verify.mark_answer_verification_pending(out_dir)
            second = verify.mark_answer_verification_pending(out_dir)
            state = verify.load_answer_verification_state(out_dir)

        self.assertEqual(state["status"], verify.STATUS_PENDING)
        self.assertEqual(state["request_id"], second["request_id"])
        self.assertNotEqual(first["request_id"], second["request_id"])

    def test_finalize_verified_persists_metadata(self):
        verify = load_module("answer_verification_state", "scripts/answer_verification_state.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            pending = verify.mark_answer_verification_pending(out_dir)
            state = verify.finalize_answer_verification(
                out_dir,
                request_id=pending["request_id"],
                status=verify.STATUS_VERIFIED,
                message="Answer verification passed.",
                verifier_provider="local_rule_based",
                verified_answer_count=2,
                blocked_answer_count=0,
                proof_submit_dir="submit",
            )

        self.assertEqual(state["status"], verify.STATUS_VERIFIED)
        self.assertEqual(state["verifier_provider"], "local_rule_based")
        self.assertEqual(state["verified_answer_count"], 2)
        self.assertEqual(state["blocked_answer_count"], 0)
        self.assertEqual(state["proof_submit_dir"], "submit")
        self.assertIsNotNone(state["resolved_at_utc"])

    def test_load_answer_verification_artifact_proof_uses_active_submit_dir(self):
        verify = load_module("answer_verification_state", "scripts/answer_verification_state.py")
        layout = load_module("output_layout", "scripts/output_layout.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            layout.ensure_role_output_dirs(out_dir)
            submit_dir = out_dir / "submit-20260401T201500Z"
            submit_dir.mkdir()
            layout.set_active_submit_dir(out_dir, submit_dir.name)
            (submit_dir / "answer_verification.json").write_text(
                (
                    "{\n"
                    '  "request_id": "verify-123",\n'
                    '  "verifier_provider": "local_rule_based",\n'
                    '  "generated_at_utc": "2026-04-01T20:15:00+00:00",\n'
                    '  "status": "verified"\n'
                    "}\n"
                ),
                encoding="utf-8",
            )

            proof = verify.load_answer_verification_artifact_proof(out_dir)

        self.assertEqual(
            proof,
            {
                "request_id": "verify-123",
                "verifier_provider": "local_rule_based",
                "generated_at_utc": "2026-04-01T20:15:00+00:00",
                "status": "verified",
                "submit_dir": submit_dir.name,
            },
        )


if __name__ == "__main__":
    unittest.main()
