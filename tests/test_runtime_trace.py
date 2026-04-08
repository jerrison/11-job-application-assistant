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


class RuntimeTraceTests(unittest.TestCase):
    def test_emit_trace_writes_redacted_jsonl_event_under_runtime_home(self):
        runtime_trace = load_module("runtime_trace", "scripts/runtime_trace.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            event = runtime_trace.emit_trace(
                "settings_saved",
                action="settings_save",
                metadata={
                    "openai_api_key": "sk-live-12345678",
                    "email": "candidate@example.com",
                    "material_key": "master_resume",
                },
                environ={"JOB_ASSETS_APP_HOME": str(runtime_root)},
            )

            trace_path = runtime_root / "traces" / "runtime-trace.jsonl"
            lines = trace_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(event["event_type"], "settings_saved")
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["action"], "settings_save")
        self.assertEqual(payload["metadata"]["material_key"], "master_resume")
        self.assertNotEqual(payload["metadata"]["openai_api_key"], "sk-live-12345678")
        self.assertNotEqual(payload["metadata"]["email"], "candidate@example.com")

    def test_configure_runtime_trace_registers_custom_processors(self):
        runtime_trace = load_module("runtime_trace_configured", "scripts/runtime_trace.py")

        class RecordingProcessor:
            def __init__(self):
                self.events: list[dict[str, object]] = []

            def process_event(self, event):
                self.events.append(dict(event))

        recording = RecordingProcessor()
        processor = runtime_trace.RedactingTraceProcessor(recording)
        runtime_trace.clear_trace_processors()
        runtime_trace.configure_runtime_trace(processors=(processor,), replace=True)
        try:
            runtime_trace.emit_trace(
                "provider_call_started",
                action="provider_call",
                metadata={"surface": "provider_subprocess", "openai_api_key": "sk-live-12345678"},
            )
        finally:
            runtime_trace.clear_trace_processors()

        self.assertEqual(len(recording.events), 1)
        self.assertEqual(recording.events[0]["action"], "provider_call")
        self.assertEqual(recording.events[0]["metadata"]["surface"], "provider_subprocess")
        self.assertNotEqual(recording.events[0]["metadata"]["openai_api_key"], "sk-live-12345678")


if __name__ == "__main__":
    unittest.main()
