"""Lenient JSON parsing for LLM-generated output.

LLMs sometimes produce JSON with issues that standard json.loads rejects:
- Trailing commas (e.g. [1, 2, 3,] or {"a": 1,})
- Unescaped control characters (literal newlines/tabs inside string values)
- Prematurely truncated JSON (missing closing braces/brackets)
- Single-quoted strings instead of double-quoted

These helpers attempt progressively more aggressive repairs before giving up.
"""

from __future__ import annotations

import ast
import json
import re

_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")

# Maximum brace/bracket imbalance we'll attempt to fix.
_MAX_BRACE_IMBALANCE = 3


def _strip_trailing_commas(text: str) -> str:
    """Remove trailing commas before closing braces/brackets."""
    return _TRAILING_COMMA_RE.sub(r"\1", text)


def _fix_control_chars(text: str) -> str:
    """Escape literal control characters inside JSON string values.

    Replaces literal newlines, carriage returns, and tabs that appear
    inside double-quoted strings with their JSON escape sequences.
    Structural whitespace outside strings is left untouched.
    """
    result: list[str] = []
    in_string = False
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if in_string:
            if ch == "\\" and i + 1 < length:
                # Keep existing escape sequences as-is.
                result.append(ch)
                result.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
                result.append(ch)
            elif ch == "\n":
                result.append("\\n")
            elif ch == "\r":
                result.append("\\r")
            elif ch == "\t":
                result.append("\\t")
            else:
                result.append(ch)
        else:
            if ch == '"':
                in_string = True
            result.append(ch)
        i += 1
    return "".join(result)


def _balance_braces(text: str) -> str:
    """Append missing closing braces/brackets if the imbalance is small.

    Only fixes imbalances of 1-3 characters.  Larger imbalances indicate
    severely broken JSON that shouldn't be guessed at.
    """
    # Track openers/closers respecting strings.
    stack: list[str] = []
    in_string = False
    i = 0
    length = len(text)
    _MATCH = {"{": "}", "[": "]"}
    while i < length:
        ch = text[i]
        if in_string:
            if ch == "\\" and i + 1 < length:
                i += 2
                continue
            if ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch in ("{", "["):
                stack.append(_MATCH[ch])
            elif ch in ("}", "]"):
                if stack and stack[-1] == ch:
                    stack.pop()
                # Mismatched closer — leave as-is, don't try to fix.
        i += 1

    if not stack:
        return text
    if len(stack) > _MAX_BRACE_IMBALANCE:
        return text  # Too broken to fix safely.

    # Append closers in reverse order (innermost first).
    return text.rstrip() + "".join(reversed(stack))


def _fix_mismatched_closers(text: str) -> str:
    """Replace a wrong closing brace/bracket with the expected closer.

    LLMs occasionally close an array with ``}`` or an object with ``]`` while
    otherwise producing structurally valid JSON. When we know the currently
    open container from the parse stack, prefer the expected closer instead of
    preserving the wrong one.
    """
    result: list[str] = []
    stack: list[str] = []
    in_string = False
    i = 0
    length = len(text)
    _MATCH = {"{": "}", "[": "]"}

    while i < length:
        ch = text[i]
        if in_string:
            result.append(ch)
            if ch == "\\" and i + 1 < length:
                result.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            result.append(ch)
        elif ch in ("{", "["):
            stack.append(_MATCH[ch])
            result.append(ch)
        elif ch in ("}", "]"):
            if stack:
                expected = stack.pop()
                result.append(expected if ch != expected else ch)
            else:
                result.append(ch)
        else:
            result.append(ch)
        i += 1

    return "".join(result)


def _single_to_double_quotes(text: str) -> str:
    """Convert single-quoted JSON to double-quoted.

    First tries a simple character replacement.  If that fails, falls back
    to ``ast.literal_eval`` → ``json.dumps`` roundtrip which handles
    nested quotes correctly.
    """
    # Simple replacement: swap outer single quotes for double quotes.
    simple = text.replace("'", '"')
    try:
        json.loads(simple)
        return simple
    except (json.JSONDecodeError, ValueError):
        pass

    # ast.literal_eval can parse Python dict/list literals with single quotes.
    try:
        obj = ast.literal_eval(text)
        return json.dumps(obj, ensure_ascii=False)
    except (ValueError, SyntaxError):
        pass

    return text  # Return original; caller will see the final parse failure.


# Ordered list of (step_name, repair_function) pairs.
# Each function takes a string and returns a (possibly repaired) string.
_REPAIR_STEPS: list[tuple[str, callable]] = [
    ("trailing_comma", _strip_trailing_commas),
    ("control_chars", _fix_control_chars),
    ("mismatched_closers", _fix_mismatched_closers),
    ("balanced_braces", _balance_braces),
    ("single_quotes", _single_to_double_quotes),
]


def loads_with_diagnostics(text: str) -> tuple[object, str]:
    """Parse JSON with progressive repair, returning the step that worked.

    Returns
    -------
    (parsed_data, repair_step_name)
        ``repair_step_name`` is one of: ``"clean"``, ``"trailing_comma"``,
        ``"control_chars"``, ``"mismatched_closers"``, ``"balanced_braces"``,
        ``"single_quotes"``.

    Raises
    ------
    json.JSONDecodeError
        If all repair strategies fail.
    """
    # Try clean parse first.
    try:
        return json.loads(text), "clean"
    except json.JSONDecodeError:
        pass

    # Try each repair step cumulatively (each builds on the previous).
    repaired = text
    for step_name, repair_fn in _REPAIR_STEPS:
        repaired = repair_fn(repaired)
        try:
            return json.loads(repaired), step_name
        except json.JSONDecodeError:
            continue

    # All repairs failed — raise from a clean attempt for a clear traceback.
    return json.loads(text), "unreachable"


def loads(text: str) -> object:
    """Like json.loads but tolerates common LLM JSON quirks.

    Attempts repairs in order: trailing commas, unescaped control characters,
    wrong closing braces/brackets, brace balancing, single-quote conversion.
    """
    data, _step = loads_with_diagnostics(text)
    return data


def load(fh) -> object:
    """Like json.load but tolerates common LLM JSON quirks."""
    return loads(fh.read())
