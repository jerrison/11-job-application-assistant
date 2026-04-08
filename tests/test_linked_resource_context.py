import importlib
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


class LinkedResourceContextTests(unittest.TestCase):
    def test_default_fetcher_passes_question_text_to_generic_adapter(self):
        linked = load_module("linked_resource_context_default_fetcher", "scripts/linked_resource_context.py")
        generic_fetch = importlib.import_module("linked_resource_adapters.generic_fetch")

        request = linked.LinkedResourceRequest(
            field_name="dataset_question",
            label="Use https://example.com/data.json to answer the question",
            description="Which card has the most spend?",
            required=True,
            question_text="Use https://example.com/data.json to answer the question\nWhich card has the most spend?",
            url="https://example.com/data.json",
        )

        with mock.patch.object(
            generic_fetch,
            "fetch_generic_resource",
            return_value={
                "status": "fetched",
                "adapter": "generic_json",
                "content_type": "application/json",
                "raw_text": '{"score": 42}',
                "raw_suffix": ".json",
                "normalized_payload": {"data": {"score": 42}},
                "derived_facts": [{"label": "score", "value": 42}],
                "prompt_context": "Score is 42.",
            },
        ) as fetch_generic_resource:
            linked._default_fetcher(request)

        fetch_generic_resource.assert_called_once_with(request.url, question_text=request.question_text)

    def test_required_linked_resource_failure_is_blocking_and_writes_artifacts(self):
        linked = load_module("linked_resource_context", "scripts/linked_resource_context.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()

            payload = linked.prepare_linked_resource_context(
                out_dir,
                [
                    {
                        "field_name": "sql_task",
                        "label": "Complete the exercise at https://example.com/sql",
                        "description": "",
                        "required": True,
                    }
                ],
                fetcher=lambda request: {
                    "status": "failed",
                    "adapter": "generic_html",
                    "failure_reason": f"Timed out fetching {request.url}",
                },
            )

            failures_path = out_dir / "submit" / "linked_resource_failures.json"
            context_path = out_dir / "submit" / "linked_resource_context.json"

            self.assertEqual(len(payload["blockers"]), 1)
            self.assertEqual(payload["failures"][0]["field_name"], "sql_task")
            self.assertTrue(context_path.exists())
            self.assertTrue(failures_path.exists())
            self.assertEqual(json.loads(failures_path.read_text(encoding="utf-8"))[0]["required"], True)

    def test_optional_linked_resource_failure_is_logged_but_non_blocking(self):
        linked = load_module("linked_resource_context_optional", "scripts/linked_resource_context.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()

            payload = linked.prepare_linked_resource_context(
                out_dir,
                [
                    {
                        "field_name": "optional_sql_task",
                        "label": "Optional exercise at https://example.com/optional",
                        "description": "",
                        "required": False,
                    }
                ],
                fetcher=lambda request: {
                    "status": "failed",
                    "adapter": "generic_html",
                    "failure_reason": f"404 for {request.url}",
                },
            )

            self.assertEqual(payload["blockers"], [])
            self.assertEqual(len(payload["failures"]), 1)
            self.assertIn("return json null", payload["prompt_context"].casefold())

    def test_matching_current_attempt_cache_is_reused_without_refetch(self):
        linked = load_module("linked_resource_context_cached", "scripts/linked_resource_context.py")
        fetch_count = {"count": 0}

        def fake_fetch(request):
            fetch_count["count"] += 1
            return {
                "status": "fetched",
                "adapter": "generic_json",
                "content_type": "application/json",
                "raw_text": '{"score": 42}',
                "raw_suffix": ".json",
                "normalized_payload": {"data": {"score": 42}},
                "derived_facts": [{"label": "score", "value": 42}],
                "prompt_context": "Score is 42.",
            }

        question_specs = [
            {
                "field_name": "dataset_question",
                "label": "Inspect https://example.com/data.json",
                "description": "",
                "required": True,
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()

            first = linked.prepare_linked_resource_context(out_dir, question_specs, fetcher=fake_fetch)
            second = linked.prepare_linked_resource_context(out_dir, question_specs, fetcher=fake_fetch)

            self.assertEqual(fetch_count["count"], 1)
            self.assertFalse(first["used_cached_artifacts"])
            self.assertTrue(second["used_cached_artifacts"])
            self.assertEqual(first["cache_key"], second["cache_key"])

    def test_matching_previous_submit_cache_is_copied_to_active_submit_dir(self):
        linked = load_module("linked_resource_context_previous", "scripts/linked_resource_context.py")

        question_specs = [
            {
                "field_name": "dataset_question",
                "label": "Inspect https://example.com/data.json",
                "description": "",
                "required": True,
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            previous_submit = out_dir / "submit-20260328T010101Z"
            previous_submit.mkdir()
            (out_dir / ".active_submit_dir").write_text("submit-20260328T010101Z\n", encoding="utf-8")

            first = linked.prepare_linked_resource_context(
                out_dir,
                question_specs,
                force_refresh=True,
                fetcher=lambda request: {
                    "status": "fetched",
                    "adapter": "generic_json",
                    "content_type": "application/json",
                    "raw_text": '{"score": 42}',
                    "raw_suffix": ".json",
                    "normalized_payload": {"data": {"score": 42}},
                    "derived_facts": [{"label": "score", "value": 42}],
                    "prompt_context": "Score is 42.",
                },
            )
            (out_dir / ".active_submit_dir").write_text("submit\n", encoding="utf-8")

            second = linked.prepare_linked_resource_context(out_dir, question_specs)

            self.assertEqual(first["cache_key"], second["cache_key"])
            self.assertTrue(second["used_cached_artifacts"])
            self.assertTrue((out_dir / "submit" / "linked_resource_context.json").exists())
            self.assertTrue((out_dir / "submit" / "linked_resource_evidence").is_dir())
