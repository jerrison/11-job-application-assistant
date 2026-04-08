import importlib.util
import json
import sys
import tempfile
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


class RuntimePolicyTests(unittest.TestCase):
    def test_live_submit_requires_explicit_approval(self):
        runtime_policy = load_module("runtime_policy", "scripts/runtime_policy.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            runtime_trace = sys.modules["runtime_trace"]
            runtime_trace.clear_trace_processors()
            runtime_trace.configure_runtime_trace(
                environ={"JOB_ASSETS_APP_HOME": str(runtime_root)},
                replace=True,
            )

            try:
                with self.assertRaises(PermissionError):
                    runtime_policy.ensure_action_allowed(
                        "live_submit",
                        explicit_approval=False,
                        environ={"JOB_ASSETS_APP_HOME": str(runtime_root)},
                    )

                trace_path = runtime_root / "traces" / "runtime-trace.jsonl"
                denied = json.loads(trace_path.read_text(encoding="utf-8").splitlines()[-1])

                approved = runtime_policy.ensure_action_allowed(
                    "live_submit",
                    explicit_approval=True,
                    metadata={
                        "surface": "submit_application",
                        "board": "greenhouse",
                        "output_dir": "output/acme/staff-pm",
                    },
                    environ={"JOB_ASSETS_APP_HOME": str(runtime_root)},
                )
            finally:
                runtime_trace.clear_trace_processors()

        self.assertEqual(denied["status"], "blocked")
        self.assertEqual(denied["action"], "live_submit")
        self.assertTrue(approved.allowed)
        self.assertEqual(approved.tier, "L3")
        self.assertEqual(approved.policy_version, 2)

    def test_policy_blocks_actions_missing_required_metadata(self):
        runtime_policy = load_module("runtime_policy_missing_metadata", "scripts/runtime_policy.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            runtime_trace = sys.modules["runtime_trace"]
            runtime_trace.clear_trace_processors()
            runtime_trace.configure_runtime_trace(
                environ={"JOB_ASSETS_APP_HOME": str(runtime_root)},
                replace=True,
            )
            try:
                with self.assertRaises(PermissionError):
                    runtime_policy.ensure_action_allowed(
                        "provider_call",
                        metadata={"provider": "openai", "model": "gpt-5.4"},
                        environ={"JOB_ASSETS_APP_HOME": str(runtime_root)},
                    )

                trace_path = runtime_root / "traces" / "runtime-trace.jsonl"
                blocked = json.loads(trace_path.read_text(encoding="utf-8").splitlines()[-1])
            finally:
                runtime_trace.clear_trace_processors()

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["metadata"]["missing_metadata"], ["surface"])

    def test_invalid_policy_payload_raises_validation_error(self):
        runtime_policy = load_module("runtime_policy_invalid_policy", "scripts/runtime_policy.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            invalid_policy = runtime_root / "invalid-policy.json"
            invalid_policy.write_text(
                json.dumps(
                    {
                        "version": 99,
                        "actions": {
                            "provider_call": {
                                "tier": "L9",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                runtime_policy.load_runtime_policy(
                    environ={
                        "JOB_ASSETS_APP_HOME": str(runtime_root),
                        "JOB_ASSETS_POLICY_PATH": str(invalid_policy),
                    }
                )


if __name__ == "__main__":
    unittest.main()
