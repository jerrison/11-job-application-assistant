---
title: "fix: OpenAI API provider can't write files — add file I/O tools"
type: fix
status: completed
date: 2026-03-23
---

# fix: OpenAI API provider can't write files — add file I/O tools

## Problem

Pipeline prompts tell the LLM to read input files and write output files (resume, cover letter, research). Claude CLI and Codex CLI have file I/O tools built in. The OpenAI API provider (`openai_provider.py`) only returns text — no file access. The LLM responds "Done — I wrote the files" but nothing is actually written to disk.

## Proposed Solution

Add file I/O tools to `openai_provider.py` using the OpenAI Responses API's built-in tool-use mechanism — the same pattern already used for `web_search`. **No prompt changes, no pipeline changes needed.** The OpenAI provider simply becomes capable of file access like Claude/Codex.

### Changes to `scripts/openai_provider.py`

#### 1. Define file tools

```python
FILE_TOOLS = [
    {
        "type": "function",
        "name": "read_file",
        "description": "Read the contents of a file",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative file path"}
            },
            "required": ["path"]
        }
    },
    {
        "type": "function",
        "name": "write_file",
        "description": "Write content to a file, creating directories if needed",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative file path"},
                "content": {"type": "string", "description": "File content to write"}
            },
            "required": ["path", "content"]
        }
    },
]
```

#### 2. Add `--file-tools` CLI flag

```python
parser.add_argument("--file-tools", action="store_true",
                    help="Enable read_file/write_file tools for agentic file access.")
```

#### 3. Implement tool-use loop

When the API returns `function_call` items, handle them locally and feed results back. **Note:** `web_search` is server-side (no loop needed), but custom function tools require a client-side loop — this is new code.

```python
MAX_TOOL_ITERATIONS = 20

for iteration in range(MAX_TOOL_ITERATIONS):
    response = client.responses.create(**kwargs)

    # Check if response contains tool calls
    tool_calls = [item for item in response.output if item.type == "function_call"]
    if not tool_calls:
        # No tool calls — print final text and exit
        print(response.output_text)
        break

    # Handle ALL tool calls and batch results
    tool_results = []
    for call in tool_calls:
        args = json.loads(call.arguments)  # arguments is a JSON string
        if call.name == "read_file":
            try:
                result = Path(args["path"]).read_text()
            except FileNotFoundError:
                result = f"Error: file not found: {args['path']}"
        elif call.name == "write_file":
            path = Path(args["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args["content"])
            result = f"Wrote {len(args['content'])} chars to {path}"
        else:
            result = f"Unknown tool: {call.name}"

        tool_results.append({
            "type": "function_call_output",
            "call_id": call.call_id,
            "output": result,
        })

    # Continue conversation with tool results + previous context
    kwargs = {
        "model": kwargs["model"],
        "previous_response_id": response.id,  # maintains full context
        "input": tool_results,
        "tools": kwargs.get("tools", []),
    }
else:
    print(f"Error: tool-use loop hit max iterations ({MAX_TOOL_ITERATIONS})", file=sys.stderr)
    return 1
```

**Key details:**
- `previous_response_id` maintains conversation context across rounds (no need to re-send prompts)
- `json.loads(call.arguments)` — the API returns arguments as a JSON string, not a dict
- All tool results are batched and sent together (not one at a time)
- Max iteration guard prevents infinite loops

#### 4. Wire into `llm_provider.py`

Add `--file-tools` flag for draft/fix modes (modes that need file access):

```python
# In provider_command() for openai:
if mode in ("draft", "fix", "content"):
    cmd.append("--file-tools")
```

Research mode already uses `--search` and may also need `--file-tools` for writing research cache files.

### What This Enables

| Before | After |
|--------|-------|
| LLM says "I wrote the file" (text) | LLM calls `write_file()` tool → file is actually written |
| LLM says "I read the input" (hallucinated) | LLM calls `read_file()` tool → gets actual file contents |
| Pipeline finds missing files | Pipeline finds files as expected |

**Same prompts, same pipeline, same behavior as Claude/Codex.**

## Scope

- `scripts/openai_provider.py` — add file tools and tool-use loop
- `scripts/llm_provider.py` — add `--file-tools` flag for appropriate modes
- Tests: mock the tool-use loop, verify files are written

## Scope Boundaries

- No prompt changes
- No pipeline changes (`apply.sh`, `llm_common.sh`)
- No changes to Claude/Codex paths
- No `extract_llm_output.py` helper needed

## Acceptance Criteria

- [ ] `ASSET_LLM_PROVIDER=openai` generates `resume_content.json` via `write_file` tool call
- [ ] `ASSET_LLM_PROVIDER=openai` generates `cover_letter_text.txt` via `write_file` tool call
- [ ] Resume has a non-null `summary` field (tailored content, not generic)
- [ ] Research files written correctly
- [ ] Fix iterations produce updated files via `read_file` → modify → `write_file`
- [ ] Claude/Codex paths unaffected
- [ ] Tool-use loop terminates (max iterations guard to prevent infinite loops)
- [ ] Existing tests pass, new tests cover tool-use loop

## Dependencies & Risks

- **OpenAI Responses API tool use**: Custom function tools use a different pattern than `web_search` (which is server-side). Function tools require a client-side loop: API returns `function_call` items → client handles them → sends `function_call_output` items back with `previous_response_id`.
- **Infinite loop risk**: The tool-use loop must have a max iteration guard (e.g., 20 iterations) to prevent runaway API calls if the LLM keeps requesting tools.
- **File path security**: The `write_file` tool should validate paths are within the project output directory to prevent writing to arbitrary locations.
- **Cost**: Each tool call round-trip is an API call. A typical drafting session needs ~3-5 tool calls (read draft, read research, write resume, write cover letter). Minimal cost increase.

## Sources

- OpenAI provider: `scripts/openai_provider.py` (current: text-only, no tools except web_search)
- Provider command builder: `scripts/llm_provider.py` (wires CLI flags per mode)
- Tool use pattern: `web_search` tool already in use (line 81)
- OpenAI Responses API tool use: standard `function_call` / `function_call_output` pattern
