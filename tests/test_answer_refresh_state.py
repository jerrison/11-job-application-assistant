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


class AnswerRefreshStateTests(unittest.TestCase):
    def test_missing_state_defaults_to_unknown(self):
        refresh = load_module("answer_refresh_state", "scripts/answer_refresh_state.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            state = refresh.load_answer_refresh_state(Path(tmpdir))

        self.assertEqual(state["status"], refresh.STATUS_UNKNOWN)
        self.assertIsNone(state["request_id"])

    def test_mark_pending_supersedes_prior_request(self):
        refresh = load_module("answer_refresh_state", "scripts/answer_refresh_state.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            first = refresh.mark_answer_refresh_pending(out_dir, request_kind="reanswer")
            second = refresh.mark_answer_refresh_pending(out_dir, request_kind="restart_pipeline")
            state = refresh.load_answer_refresh_state(out_dir)

        self.assertEqual(state["status"], refresh.STATUS_PENDING)
        self.assertEqual(state["request_id"], second["request_id"])
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertEqual(state["request_kind"], "restart_pipeline")

    def test_finalize_stale_request_id_is_ignored(self):
        refresh = load_module("answer_refresh_state", "scripts/answer_refresh_state.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            first = refresh.mark_answer_refresh_pending(out_dir, request_kind="reanswer")
            second = refresh.mark_answer_refresh_pending(out_dir, request_kind="reanswer")
            state = refresh.finalize_answer_refresh(
                out_dir,
                request_id=first["request_id"],
                status=refresh.STATUS_FRESH,
                message="Should be ignored",
                answer_provider="claude",
                answer_generated_at_utc="2026-03-26T18:30:04+00:00",
                generated_answer_count=2,
                proof_submit_dir="submit",
            )

        self.assertEqual(state["status"], refresh.STATUS_PENDING)
        self.assertEqual(state["request_id"], second["request_id"])

    def test_finalize_fresh_persists_metadata(self):
        refresh = load_module("answer_refresh_state", "scripts/answer_refresh_state.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            pending = refresh.mark_answer_refresh_pending(out_dir, request_kind="regenerate")
            state = refresh.finalize_answer_refresh(
                out_dir,
                request_id=pending["request_id"],
                status=refresh.STATUS_FRESH,
                message="Fresh answer proof recorded.",
                answer_provider="claude",
                answer_generated_at_utc="2026-03-26T18:30:04+00:00",
                generated_answer_count=3,
                proof_submit_dir="submit-20260326T183004Z",
            )

        self.assertEqual(state["status"], refresh.STATUS_FRESH)
        self.assertEqual(state["answer_provider"], "claude")
        self.assertEqual(state["answer_generated_at_utc"], "2026-03-26T18:30:04+00:00")
        self.assertEqual(state["generated_answer_count"], 3)
        self.assertEqual(state["proof_submit_dir"], "submit-20260326T183004Z")
        self.assertIsNotNone(state["resolved_at_utc"])

    def test_load_answer_refresh_artifact_proof_uses_active_submit_dir(self):
        refresh = load_module("answer_refresh_state", "scripts/answer_refresh_state.py")
        layout = load_module("output_layout", "scripts/output_layout.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            layout.ensure_role_output_dirs(out_dir)
            submit_dir = out_dir / "submit-20260326T183004Z"
            submit_dir.mkdir()
            layout.set_active_submit_dir(out_dir, submit_dir.name)
            (submit_dir / layout.APPLICATION_ANSWER_CACHE).write_text(
                (
                    "{\n"
                    '  "refresh_request_id": "request-123",\n'
                    '  "provider": "openai",\n'
                    '  "generated_at_utc": "2026-03-26T18:30:04+00:00"\n'
                    "}\n"
                ),
                encoding="utf-8",
            )

            proof = refresh.load_answer_refresh_artifact_proof(out_dir)

        self.assertEqual(
            proof,
            {
                "request_id": "request-123",
                "provider": "openai",
                "generated_at_utc": "2026-03-26T18:30:04+00:00",
                "submit_dir": submit_dir.name,
            },
        )


if __name__ == "__main__":
    unittest.main()
