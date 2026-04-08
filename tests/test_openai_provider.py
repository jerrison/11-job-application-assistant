"""Tests for the OpenAI Responses API provider shim."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _make_function_call(name: str, arguments: dict, call_id: str = "call_1"):
    """Create a mock function_call output item."""
    call = MagicMock()
    call.type = "function_call"
    call.name = name
    call.arguments = json.dumps(arguments)
    call.call_id = call_id
    return call


def _make_text_response(text: str, response_id: str = "resp_1"):
    """Create a mock response with only text output (no tool calls)."""
    text_item = MagicMock()
    text_item.type = "text"
    resp = MagicMock()
    resp.output = [text_item]
    resp.output_text = text
    resp.id = response_id
    return resp


def _make_tool_call_response(calls: list, response_id: str = "resp_1"):
    """Create a mock response containing function_call items."""
    resp = MagicMock()
    resp.output = calls
    resp.output_text = ""
    resp.id = response_id
    return resp


class OpenAIProviderTests(unittest.TestCase):
    def test_basic_prompt_prints_to_stdout(self):
        """Mock client.responses.create, verify output is printed to stdout."""
        mock_response = MagicMock()
        mock_response.output_text = "Hello from GPT"

        mock_client_cls = MagicMock()
        mock_client_cls.return_value.responses.create.return_value = mock_response

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}, clear=False),
            patch.dict("sys.modules", {"openai": MagicMock(OpenAI=mock_client_cls)}),
            patch("sys.argv", ["openai_provider.py", "Say hello"]),
            patch("sys.stdout") as mock_stdout,
        ):
            provider = load_module("openai_provider", "scripts/openai_provider.py")
            result = provider.main()

        self.assertEqual(result, 0)
        mock_stdout.write.assert_any_call("Hello from GPT")

    def test_search_flag_adds_web_search_tool(self):
        """Verify --search adds tools=[{"type": "web_search"}] to API kwargs."""
        mock_response = MagicMock()
        mock_response.output_text = "search result"

        mock_client_instance = MagicMock()
        mock_client_instance.responses.create.return_value = mock_response
        mock_client_cls = MagicMock(return_value=mock_client_instance)

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}, clear=False),
            patch.dict("sys.modules", {"openai": MagicMock(OpenAI=mock_client_cls)}),
            patch("sys.argv", ["openai_provider.py", "--search", "Find info"]),
            patch("sys.stdout"),
        ):
            provider = load_module("openai_provider", "scripts/openai_provider.py")
            provider.main()

        call_kwargs = mock_client_instance.responses.create.call_args
        self.assertEqual(call_kwargs.kwargs.get("tools") or call_kwargs[1].get("tools"), [{"type": "web_search"}])

    def test_json_mode_flag(self):
        """Verify --json-mode adds text={"format": {"type": "json_object"}} to kwargs."""
        mock_response = MagicMock()
        mock_response.output_text = '{"key": "value"}'

        mock_client_instance = MagicMock()
        mock_client_instance.responses.create.return_value = mock_response
        mock_client_cls = MagicMock(return_value=mock_client_instance)

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}, clear=False),
            patch.dict("sys.modules", {"openai": MagicMock(OpenAI=mock_client_cls)}),
            patch("sys.argv", ["openai_provider.py", "--json-mode", "Return JSON"]),
            patch("sys.stdout"),
        ):
            provider = load_module("openai_provider", "scripts/openai_provider.py")
            provider.main()

        call_kwargs = mock_client_instance.responses.create.call_args
        text_arg = call_kwargs.kwargs.get("text") or call_kwargs[1].get("text")
        self.assertEqual(text_arg, {"format": {"type": "json_object"}})

    def test_json_schema_flag(self):
        """Verify --json-schema adds text.format json_schema to kwargs."""
        mock_response = MagicMock()
        mock_response.output_text = '{"key": "value"}'

        mock_client_instance = MagicMock()
        mock_client_instance.responses.create.return_value = mock_response
        mock_client_cls = MagicMock(return_value=mock_client_instance)
        schema = {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        }

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}, clear=False),
            patch.dict("sys.modules", {"openai": MagicMock(OpenAI=mock_client_cls)}),
            patch(
                "sys.argv",
                [
                    "openai_provider.py",
                    "--json-schema",
                    json.dumps(schema),
                    "--json-schema-name",
                    "application_answers",
                    "Return JSON",
                ],
            ),
            patch("sys.stdout"),
        ):
            provider = load_module("openai_provider", "scripts/openai_provider.py")
            provider.main()

        call_kwargs = mock_client_instance.responses.create.call_args
        text_arg = call_kwargs.kwargs.get("text") or call_kwargs[1].get("text")
        self.assertEqual(
            text_arg,
            {
                "format": {
                    "type": "json_schema",
                    "name": "application_answers",
                    "strict": True,
                    "schema": schema,
                }
            },
        )

    def test_missing_api_key_returns_error(self):
        """No OPENAI_API_KEY set should return exit code 1."""
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("sys.argv", ["openai_provider.py", "Hello"]),
            patch("sys.stderr"),
        ):
            # Need to ensure OPENAI_API_KEY is not in the environment
            import os

            os.environ.pop("OPENAI_API_KEY", None)
            provider = load_module("openai_provider", "scripts/openai_provider.py")
            result = provider.main()

        self.assertEqual(result, 1)

    def test_openai_api_keys_pool_selects_a_key(self):
        """OPENAI_API_KEYS should supply the API key when a pool is configured."""
        mock_response = MagicMock()
        mock_response.output_text = "Hello from GPT"

        mock_client_cls = MagicMock()
        mock_client_cls.return_value.responses.create.return_value = mock_response

        with (
            patch.dict(
                "os.environ",
                {"OPENAI_API_KEYS": "sk-pool-1, sk-pool-2\nsk-pool-3"},
                clear=True,
            ),
            patch.dict("sys.modules", {"openai": MagicMock(OpenAI=mock_client_cls)}),
            patch("sys.argv", ["openai_provider.py", "Say hello"]),
            patch("sys.stdout"),
        ):
            provider = load_module("openai_provider", "scripts/openai_provider.py")
            with patch.object(provider.secrets, "choice", return_value="sk-pool-2"):
                result = provider.main()

        self.assertEqual(result, 0)
        self.assertEqual(mock_client_cls.call_args.kwargs["api_key"], "sk-pool-2")

    def test_openai_api_keys_pool_overrides_single_key(self):
        """OPENAI_API_KEYS should take precedence over OPENAI_API_KEY when both exist."""
        mock_response = MagicMock()
        mock_response.output_text = "Hello from GPT"

        mock_client_cls = MagicMock()
        mock_client_cls.return_value.responses.create.return_value = mock_response

        with (
            patch.dict(
                "os.environ",
                {
                    "OPENAI_API_KEY": "sk-single-key",
                    "OPENAI_API_KEYS": "sk-pool-1,sk-pool-2",
                },
                clear=True,
            ),
            patch.dict("sys.modules", {"openai": MagicMock(OpenAI=mock_client_cls)}),
            patch("sys.argv", ["openai_provider.py", "Say hello"]),
            patch("sys.stdout"),
        ):
            provider = load_module("openai_provider", "scripts/openai_provider.py")
            with patch.object(provider.secrets, "choice", return_value="sk-pool-1"):
                result = provider.main()

        self.assertEqual(result, 0)
        self.assertEqual(mock_client_cls.call_args.kwargs["api_key"], "sk-pool-1")

    def test_api_error_returns_nonzero(self):
        """Mock an API exception and verify exit code is 1."""
        mock_client_instance = MagicMock()
        mock_client_instance.responses.create.side_effect = RuntimeError("API rate limit")
        mock_client_cls = MagicMock(return_value=mock_client_instance)

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}, clear=False),
            patch.dict("sys.modules", {"openai": MagicMock(OpenAI=mock_client_cls)}),
            patch("sys.argv", ["openai_provider.py", "Hello"]),
            patch("sys.stderr"),
        ):
            provider = load_module("openai_provider", "scripts/openai_provider.py")
            result = provider.main()

        self.assertEqual(result, 1)

    def test_stdin_prompt(self):
        """Verify prompt is read from stdin when '-' is passed."""
        mock_response = MagicMock()
        mock_response.output_text = "stdin response"

        mock_client_instance = MagicMock()
        mock_client_instance.responses.create.return_value = mock_response
        mock_client_cls = MagicMock(return_value=mock_client_instance)

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}, clear=False),
            patch.dict("sys.modules", {"openai": MagicMock(OpenAI=mock_client_cls)}),
            patch("sys.argv", ["openai_provider.py", "-"]),
            patch("sys.stdin") as mock_stdin,
            patch("sys.stdout"),
        ):
            mock_stdin.read.return_value = "prompt from stdin"
            provider = load_module("openai_provider", "scripts/openai_provider.py")
            result = provider.main()

        self.assertEqual(result, 0)
        call_kwargs = mock_client_instance.responses.create.call_args
        input_arg = call_kwargs.kwargs.get("input") or call_kwargs[1].get("input")
        self.assertEqual(input_arg, "prompt from stdin")

    def test_main_passes_reasoning_effort_to_responses_api(self):
        mock_response = MagicMock()
        mock_response.output_text = "OK"
        mock_response.output = []

        mock_client_instance = MagicMock()
        mock_client_instance.responses.create.return_value = mock_response
        mock_client_cls = MagicMock(return_value=mock_client_instance)

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}, clear=False),
            patch.dict("sys.modules", {"openai": MagicMock(OpenAI=mock_client_cls)}),
            patch("sys.argv", ["openai_provider.py", "--reasoning-effort", "xhigh", "Reply with OK."]),
            patch("sys.stdout"),
        ):
            provider = load_module("openai_provider", "scripts/openai_provider.py")
            result = provider.main()

        self.assertEqual(result, 0)
        call_kwargs = mock_client_instance.responses.create.call_args
        reasoning_arg = call_kwargs.kwargs.get("reasoning") or call_kwargs[1].get("reasoning")
        self.assertEqual(reasoning_arg, {"effort": "xhigh"})


class FileToolsTests(unittest.TestCase):
    """Tests for the --file-tools tool-use loop."""

    def test_file_tools_flag_adds_file_tools_to_kwargs(self):
        """Verify --file-tools adds read_file and write_file tools to API kwargs."""
        mock_response = _make_text_response("Done")
        mock_client_instance = MagicMock()
        mock_client_instance.responses.create.return_value = mock_response
        mock_client_cls = MagicMock(return_value=mock_client_instance)

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}, clear=False),
            patch.dict("sys.modules", {"openai": MagicMock(OpenAI=mock_client_cls)}),
            patch("sys.argv", ["openai_provider.py", "--file-tools", "Write a file"]),
            patch("sys.stdout"),
        ):
            provider = load_module("openai_provider", "scripts/openai_provider.py")
            provider.main()

        call_kwargs = mock_client_instance.responses.create.call_args
        tools = call_kwargs.kwargs.get("tools") or call_kwargs[1].get("tools")
        tool_names = [t["name"] for t in tools if t.get("type") == "function"]
        self.assertIn("read_file", tool_names)
        self.assertIn("write_file", tool_names)

    def test_file_tools_and_search_combined(self):
        """Verify --file-tools --search includes both web_search and file tools."""
        mock_response = _make_text_response("Done")
        mock_client_instance = MagicMock()
        mock_client_instance.responses.create.return_value = mock_response
        mock_client_cls = MagicMock(return_value=mock_client_instance)

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}, clear=False),
            patch.dict("sys.modules", {"openai": MagicMock(OpenAI=mock_client_cls)}),
            patch("sys.argv", ["openai_provider.py", "--file-tools", "--search", "Research"]),
            patch("sys.stdout"),
        ):
            provider = load_module("openai_provider", "scripts/openai_provider.py")
            provider.main()

        call_kwargs = mock_client_instance.responses.create.call_args
        tools = call_kwargs.kwargs.get("tools") or call_kwargs[1].get("tools")
        tool_types = [t.get("type") for t in tools]
        self.assertIn("web_search", tool_types)
        self.assertIn("function", tool_types)

    def test_tool_loop_write_file_creates_file(self):
        """Tool-use loop: write_file tool call actually creates the file on disk."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target_path = str(Path(tmp_dir) / "output.txt")

            write_call = _make_function_call(
                "write_file",
                {"path": target_path, "content": "Hello, world!"},
                call_id="call_write",
            )
            tool_response = _make_tool_call_response([write_call], response_id="resp_1")
            final_response = _make_text_response("Done writing.", response_id="resp_2")

            mock_client_instance = MagicMock()
            mock_client_instance.responses.create.side_effect = [tool_response, final_response]
            mock_client_cls = MagicMock(return_value=mock_client_instance)

            with (
                patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}, clear=False),
                patch.dict("sys.modules", {"openai": MagicMock(OpenAI=mock_client_cls)}),
                patch("sys.argv", ["openai_provider.py", "--file-tools", "Write output.txt"]),
                patch("sys.stdout"),
            ):
                provider = load_module("openai_provider", "scripts/openai_provider.py")
                result = provider.main()

            self.assertEqual(result, 0)
            self.assertTrue(Path(target_path).exists())
            self.assertEqual(Path(target_path).read_text(), "Hello, world!")

    def test_tool_loop_read_file_returns_contents(self):
        """Tool-use loop: read_file tool call returns the file contents."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            source_path = str(Path(tmp_dir) / "input.txt")
            Path(source_path).write_text("existing content")

            read_call = _make_function_call(
                "read_file",
                {"path": source_path},
                call_id="call_read",
            )
            tool_response = _make_tool_call_response([read_call], response_id="resp_1")
            final_response = _make_text_response("I read the file.", response_id="resp_2")

            mock_client_instance = MagicMock()
            mock_client_instance.responses.create.side_effect = [tool_response, final_response]
            mock_client_cls = MagicMock(return_value=mock_client_instance)

            with (
                patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}, clear=False),
                patch.dict("sys.modules", {"openai": MagicMock(OpenAI=mock_client_cls)}),
                patch("sys.argv", ["openai_provider.py", "--file-tools", "Read input.txt"]),
                patch("sys.stdout"),
            ):
                provider = load_module("openai_provider", "scripts/openai_provider.py")
                result = provider.main()

            self.assertEqual(result, 0)
            # Verify the second call sent the file content back as tool output.
            second_call = mock_client_instance.responses.create.call_args_list[1]
            tool_results = second_call.kwargs.get("input") or second_call[1].get("input")
            self.assertEqual(len(tool_results), 1)
            self.assertEqual(tool_results[0]["call_id"], "call_read")
            self.assertEqual(tool_results[0]["output"], "existing content")

    def test_tool_loop_read_file_not_found(self):
        """Tool-use loop: read_file on missing file returns error string (no crash)."""
        read_call = _make_function_call(
            "read_file",
            {"path": "/nonexistent/path/missing.txt"},
            call_id="call_read",
        )
        tool_response = _make_tool_call_response([read_call], response_id="resp_1")
        final_response = _make_text_response("File not found.", response_id="resp_2")

        mock_client_instance = MagicMock()
        mock_client_instance.responses.create.side_effect = [tool_response, final_response]
        mock_client_cls = MagicMock(return_value=mock_client_instance)

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}, clear=False),
            patch.dict("sys.modules", {"openai": MagicMock(OpenAI=mock_client_cls)}),
            patch("sys.argv", ["openai_provider.py", "--file-tools", "Read file"]),
            patch("sys.stdout"),
        ):
            provider = load_module("openai_provider", "scripts/openai_provider.py")
            result = provider.main()

        self.assertEqual(result, 0)
        second_call = mock_client_instance.responses.create.call_args_list[1]
        tool_results = second_call.kwargs.get("input") or second_call[1].get("input")
        self.assertIn("Error: file not found", tool_results[0]["output"])

    def test_tool_loop_max_iterations_guard(self):
        """Tool-use loop exits with error after MAX_TOOL_ITERATIONS."""
        # Create a response that always returns a tool call (infinite loop scenario).
        call = _make_function_call("read_file", {"path": "/tmp/x"}, call_id="call_loop")
        looping_response = _make_tool_call_response([call], response_id="resp_loop")

        mock_client_instance = MagicMock()
        # Return tool calls on every iteration.
        mock_client_instance.responses.create.return_value = looping_response
        mock_client_cls = MagicMock(return_value=mock_client_instance)

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}, clear=False),
            patch.dict("sys.modules", {"openai": MagicMock(OpenAI=mock_client_cls)}),
            patch("sys.argv", ["openai_provider.py", "--file-tools", "Loop forever"]),
            patch("sys.stdout"),
            patch("sys.stderr"),
        ):
            provider = load_module("openai_provider", "scripts/openai_provider.py")
            result = provider.main()

        self.assertEqual(result, 1)
        # Should have been called exactly MAX_TOOL_ITERATIONS times.
        self.assertEqual(
            mock_client_instance.responses.create.call_count,
            provider.MAX_TOOL_ITERATIONS,
        )

    def test_tool_loop_uses_previous_response_id(self):
        """Tool-use loop passes previous_response_id for conversation continuity."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target_path = str(Path(tmp_dir) / "out.txt")

            write_call = _make_function_call(
                "write_file",
                {"path": target_path, "content": "data"},
                call_id="call_w",
            )
            tool_response = _make_tool_call_response([write_call], response_id="resp_abc123")
            final_response = _make_text_response("Done.", response_id="resp_2")

            mock_client_instance = MagicMock()
            mock_client_instance.responses.create.side_effect = [tool_response, final_response]
            mock_client_cls = MagicMock(return_value=mock_client_instance)

            with (
                patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}, clear=False),
                patch.dict("sys.modules", {"openai": MagicMock(OpenAI=mock_client_cls)}),
                patch("sys.argv", ["openai_provider.py", "--file-tools", "Write file"]),
                patch("sys.stdout"),
            ):
                provider = load_module("openai_provider", "scripts/openai_provider.py")
                provider.main()

            second_call = mock_client_instance.responses.create.call_args_list[1]
            prev_id = second_call.kwargs.get("previous_response_id") or second_call[1].get("previous_response_id")
            self.assertEqual(prev_id, "resp_abc123")

    def test_tool_loop_batches_multiple_tool_calls(self):
        """Tool-use loop handles multiple tool calls in a single response."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            path_a = str(Path(tmp_dir) / "a.txt")
            path_b = str(Path(tmp_dir) / "b.txt")

            call_a = _make_function_call(
                "write_file",
                {"path": path_a, "content": "AAA"},
                call_id="call_a",
            )
            call_b = _make_function_call(
                "write_file",
                {"path": path_b, "content": "BBB"},
                call_id="call_b",
            )
            tool_response = _make_tool_call_response([call_a, call_b], response_id="resp_1")
            final_response = _make_text_response("Both written.", response_id="resp_2")

            mock_client_instance = MagicMock()
            mock_client_instance.responses.create.side_effect = [tool_response, final_response]
            mock_client_cls = MagicMock(return_value=mock_client_instance)

            with (
                patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}, clear=False),
                patch.dict("sys.modules", {"openai": MagicMock(OpenAI=mock_client_cls)}),
                patch("sys.argv", ["openai_provider.py", "--file-tools", "Write two files"]),
                patch("sys.stdout"),
            ):
                provider = load_module("openai_provider", "scripts/openai_provider.py")
                result = provider.main()

            self.assertEqual(result, 0)
            self.assertEqual(Path(path_a).read_text(), "AAA")
            self.assertEqual(Path(path_b).read_text(), "BBB")
            # Verify both results sent back in one call.
            second_call = mock_client_instance.responses.create.call_args_list[1]
            tool_results = second_call.kwargs.get("input") or second_call[1].get("input")
            self.assertEqual(len(tool_results), 2)

    def test_tool_loop_reapplies_reasoning_effort_after_tool_results(self):
        """Tool-use loop preserves reasoning config on the follow-up API call."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target_path = str(Path(tmp_dir) / "out.txt")

            write_call = _make_function_call(
                "write_file",
                {"path": target_path, "content": "data"},
                call_id="call_reasoning",
            )
            tool_response = _make_tool_call_response([write_call], response_id="resp_reasoning")
            final_response = _make_text_response("Done.", response_id="resp_done")

            mock_client_instance = MagicMock()
            mock_client_instance.responses.create.side_effect = [tool_response, final_response]
            mock_client_cls = MagicMock(return_value=mock_client_instance)

            with (
                patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}, clear=False),
                patch.dict("sys.modules", {"openai": MagicMock(OpenAI=mock_client_cls)}),
                patch(
                    "sys.argv",
                    [
                        "openai_provider.py",
                        "--file-tools",
                        "--reasoning-effort",
                        "xhigh",
                        "Write file",
                    ],
                ),
                patch("sys.stdout"),
            ):
                provider = load_module("openai_provider", "scripts/openai_provider.py")
                result = provider.main()

            self.assertEqual(result, 0)
            second_call = mock_client_instance.responses.create.call_args_list[1]
            reasoning = second_call.kwargs.get("reasoning") or second_call[1].get("reasoning")
            self.assertEqual(reasoning, {"effort": "xhigh"})

    def test_without_file_tools_flag_no_loop(self):
        """Without --file-tools, behavior is unchanged (simple request/response)."""
        mock_response = MagicMock()
        mock_response.output_text = "Simple response"

        mock_client_instance = MagicMock()
        mock_client_instance.responses.create.return_value = mock_response
        mock_client_cls = MagicMock(return_value=mock_client_instance)

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}, clear=False),
            patch.dict("sys.modules", {"openai": MagicMock(OpenAI=mock_client_cls)}),
            patch("sys.argv", ["openai_provider.py", "Say hello"]),
            patch("sys.stdout"),
        ):
            provider = load_module("openai_provider", "scripts/openai_provider.py")
            result = provider.main()

        self.assertEqual(result, 0)
        # Only one API call (no loop).
        self.assertEqual(mock_client_instance.responses.create.call_count, 1)
        # No tools in kwargs.
        call_kwargs = mock_client_instance.responses.create.call_args
        self.assertNotIn("tools", call_kwargs.kwargs)

    def test_handle_tool_call_read_file(self):
        """Unit test for _handle_tool_call with read_file."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_file = Path(tmp_dir) / "test.txt"
            test_file.write_text("file content here")
            provider = load_module("openai_provider", "scripts/openai_provider.py")
            result = provider._handle_tool_call("read_file", {"path": str(test_file)})
            self.assertEqual(result, "file content here")

    def test_handle_tool_call_read_file_not_found(self):
        """Unit test for _handle_tool_call with missing file."""
        provider = load_module("openai_provider", "scripts/openai_provider.py")
        result = provider._handle_tool_call("read_file", {"path": "/no/such/file.txt"})
        self.assertIn("Error: file not found", result)

    def test_handle_tool_call_write_file(self):
        """Unit test for _handle_tool_call with write_file."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "sub" / "output.txt"
            provider = load_module("openai_provider", "scripts/openai_provider.py")
            result = provider._handle_tool_call("write_file", {"path": str(target), "content": "hello"})
            self.assertIn("Wrote 5 chars", result)
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(), "hello")

    def test_handle_tool_call_unknown_tool(self):
        """Unit test for _handle_tool_call with unknown tool name."""
        provider = load_module("openai_provider", "scripts/openai_provider.py")
        result = provider._handle_tool_call("delete_file", {"path": "/tmp/x"})
        self.assertIn("Unknown tool: delete_file", result)


if __name__ == "__main__":
    unittest.main()
