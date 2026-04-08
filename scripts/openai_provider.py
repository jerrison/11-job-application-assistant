#!/usr/bin/env python3
"""Subprocess shim for the OpenAI Responses API.

Called by ``provider_command("openai", prompt)`` via ``subprocess.run()``.
Reads the prompt, calls the OpenAI Responses API, and prints the response
text to stdout.  Diagnostics go to stderr.

Usage::

    python openai_provider.py [--model MODEL] [--reasoning-effort LEVEL] [--search] \
                              [--json-mode | --json-schema JSON] \
                               [--json-schema-name NAME] [--file-tools] [--timeout SECS] \
                               [--max-retries N] PROMPT

    # Long prompts via stdin:
    echo "very long prompt" | python openai_provider.py --model gpt-5.4-mini -

"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

DEFAULT_MODEL = "gpt-5.4"
DEFAULT_TIMEOUT = 180
DEFAULT_MAX_RETRIES = 3
MAX_TOOL_ITERATIONS = 20

FILE_TOOLS = [
    {
        "type": "function",
        "name": "read_file",
        "description": "Read the contents of a file",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative file path"},
            },
            "required": ["path"],
        },
    },
    {
        "type": "function",
        "name": "write_file",
        "description": "Write content to a file, creating directories if needed",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative file path"},
                "content": {"type": "string", "description": "File content to write"},
            },
            "required": ["path", "content"],
        },
    },
]


def _parse_openai_api_keys(value: str | None) -> list[str]:
    if value is None:
        return []

    keys: list[str] = []
    seen: set[str] = set()
    normalized = value.replace(",", "\n")
    for raw_key in normalized.splitlines():
        key = raw_key.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


def configured_openai_api_keys(*, environ: Mapping[str, str] | None = None) -> list[str]:
    env = environ if environ is not None else os.environ
    pooled_keys = _parse_openai_api_keys(env.get("OPENAI_API_KEYS"))
    if pooled_keys:
        return pooled_keys

    api_key = (env.get("OPENAI_API_KEY") or "").strip()
    return [api_key] if api_key else []


def select_openai_api_key(keys: Sequence[str]) -> str:
    if len(keys) == 1:
        return keys[0]
    return secrets.choice(keys)


def _handle_tool_call(call_name: str, args: dict) -> str:
    """Execute a single tool call and return the result string."""
    if call_name == "read_file":
        try:
            return Path(args["path"]).read_text()
        except FileNotFoundError:
            return f"Error: file not found: {args['path']}"
    if call_name == "write_file":
        path = Path(args["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"])
        return f"Wrote {len(args['content'])} chars to {path}"
    return f"Unknown tool: {call_name}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Call the OpenAI Responses API and print the result to stdout.",
    )
    parser.add_argument("prompt", help='The prompt text, or "-" to read from stdin.')
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model name (default: {DEFAULT_MODEL}).")
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "low", "medium", "high", "xhigh"),
        default="",
        help="Responses API reasoning.effort value.",
    )
    parser.add_argument("--search", action="store_true", help="Enable the web_search tool.")
    parser.add_argument(
        "--file-tools", action="store_true", help="Enable read_file/write_file tools for agentic file access."
    )
    json_group = parser.add_mutually_exclusive_group()
    json_group.add_argument("--json-mode", action="store_true", help="Request JSON object output format.")
    json_group.add_argument("--json-schema", help="Request structured JSON output using the provided schema.")
    parser.add_argument(
        "--json-schema-name",
        default="response",
        help="Schema name to send with --json-schema (default: response).",
    )
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"Request timeout in seconds (default: {DEFAULT_TIMEOUT})."
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"Max retries on transient errors (default: {DEFAULT_MAX_RETRIES}).",
    )
    return parser


def _response_text_format(args: argparse.Namespace) -> dict | None:
    if args.json_schema:
        try:
            schema = json.loads(args.json_schema)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid --json-schema payload: {exc}") from exc
        if not isinstance(schema, dict):
            raise ValueError("--json-schema must decode to a JSON object")
        return {
            "type": "json_schema",
            "name": args.json_schema_name or "response",
            "strict": True,
            "schema": schema,
        }
    if args.json_mode:
        return {"type": "json_object"}
    return None


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # Read prompt from stdin if "-" is passed.
    if args.prompt == "-":
        prompt = sys.stdin.read()
        if not prompt.strip():
            print("Error: empty prompt from stdin", file=sys.stderr)
            return 1
    else:
        prompt = args.prompt

    try:
        text_format = _response_text_format(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    api_keys = configured_openai_api_keys()
    if not api_keys:
        print("Error: OPENAI_API_KEYS or OPENAI_API_KEY environment variable is not set", file=sys.stderr)
        return 1
    api_key = select_openai_api_key(api_keys)

    try:
        from openai import OpenAI
    except ImportError:
        print("Error: openai package is not installed — run: uv add openai", file=sys.stderr)
        return 1

    client = OpenAI(
        api_key=api_key,
        timeout=float(args.timeout),
        max_retries=args.max_retries,
    )

    # Build API call kwargs.
    kwargs: dict = {
        "model": args.model,
        "input": prompt,
    }
    if args.reasoning_effort:
        kwargs["reasoning"] = {"effort": args.reasoning_effort}

    # Assemble tools list: web_search (server-side) and/or file tools (client-side).
    tools: list[dict] = []
    if args.search:
        tools.append({"type": "web_search"})
    if args.file_tools:
        tools.extend(FILE_TOOLS)
    if tools:
        kwargs["tools"] = tools

    if text_format is not None:
        kwargs["text"] = {"format": text_format}

    try:
        if not args.file_tools:
            # Simple path: no tool-use loop needed.
            response = client.responses.create(**kwargs)
            text = response.output_text
            if text:
                print(text)
            else:
                print("Error: OpenAI API returned empty response", file=sys.stderr)
                return 1
        else:
            # Tool-use loop: handle function_call responses from the API.
            for _iteration in range(MAX_TOOL_ITERATIONS):
                response = client.responses.create(**kwargs)

                # Check if response contains function_call items.
                tool_calls = [item for item in response.output if item.type == "function_call"]
                if not tool_calls:
                    # No tool calls — print final text and exit.
                    text = response.output_text
                    if text:
                        print(text)
                    break

                # Handle ALL tool calls and batch results.
                tool_results: list[dict] = []
                for call in tool_calls:
                    call_args = json.loads(call.arguments)
                    result = _handle_tool_call(call.name, call_args)
                    tool_results.append(
                        {
                            "type": "function_call_output",
                            "call_id": call.call_id,
                            "output": result,
                        }
                    )

                # Continue conversation with tool results + previous context.
                kwargs = {
                    "model": kwargs["model"],
                    "previous_response_id": response.id,
                    "input": tool_results,
                    "tools": kwargs.get("tools", []),
                }
                if args.reasoning_effort:
                    kwargs["reasoning"] = {"effort": args.reasoning_effort}
                # Preserve text format if set.
                if text_format is not None:
                    kwargs["text"] = {"format": text_format}
            else:
                print(
                    f"Error: tool-use loop hit max iterations ({MAX_TOOL_ITERATIONS})",
                    file=sys.stderr,
                )
                return 1
    except Exception as exc:
        print(f"Error: OpenAI API call failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
