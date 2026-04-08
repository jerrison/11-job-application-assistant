"""Tests for lenient JSON parsing with progressive repair."""

import importlib.util
import json
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


jl = load_module("json_lenient", "scripts/json_lenient.py")


# ---------------------------------------------------------------------------
# Valid JSON passes through unchanged
# ---------------------------------------------------------------------------
class TestCleanJson(unittest.TestCase):
    def test_valid_object(self):
        data = jl.loads('{"name": "Alice", "age": 30}')
        self.assertEqual(data, {"name": "Alice", "age": 30})

    def test_valid_array(self):
        data = jl.loads("[1, 2, 3]")
        self.assertEqual(data, [1, 2, 3])

    def test_valid_nested(self):
        text = '{"items": [{"id": 1}, {"id": 2}], "count": 2}'
        data = jl.loads(text)
        self.assertEqual(data["count"], 2)
        self.assertEqual(len(data["items"]), 2)

    def test_diagnostics_clean(self):
        data, step = jl.loads_with_diagnostics('{"ok": true}')
        self.assertEqual(data, {"ok": True})
        self.assertEqual(step, "clean")


# ---------------------------------------------------------------------------
# Trailing commas (existing behavior)
# ---------------------------------------------------------------------------
class TestTrailingComma(unittest.TestCase):
    def test_trailing_comma_object(self):
        data = jl.loads('{"a": 1, "b": 2,}')
        self.assertEqual(data, {"a": 1, "b": 2})

    def test_trailing_comma_array(self):
        data = jl.loads("[1, 2, 3,]")
        self.assertEqual(data, [1, 2, 3])

    def test_trailing_comma_nested(self):
        text = '{"items": ["x", "y",], "done": true,}'
        data = jl.loads(text)
        self.assertEqual(data, {"items": ["x", "y"], "done": True})

    def test_diagnostics_trailing_comma(self):
        data, step = jl.loads_with_diagnostics('{"a": 1,}')
        self.assertEqual(step, "trailing_comma")


# ---------------------------------------------------------------------------
# Unescaped control characters in string values
# ---------------------------------------------------------------------------
class TestControlChars(unittest.TestCase):
    def test_literal_newline_in_string(self):
        # LLM produces a literal newline inside a JSON string value.
        text = '{"summary": "Line one\nLine two"}'
        data = jl.loads(text)
        self.assertEqual(data["summary"], "Line one\nLine two")

    def test_literal_tab_in_string(self):
        text = '{"note": "col1\tcol2"}'
        data = jl.loads(text)
        self.assertEqual(data["note"], "col1\tcol2")

    def test_literal_cr_in_string(self):
        text = '{"text": "before\rafter"}'
        data = jl.loads(text)
        self.assertEqual(data["text"], "before\rafter")

    def test_multiline_string_with_newlines(self):
        # Realistic LLM failure: multi-line content in a JSON string.
        text = (
            '{"bullet": "Led cross-functional team of 8 engineers\nto deliver API platform\nserving 10M requests/day"}'
        )
        data = jl.loads(text)
        self.assertIn("Led cross-functional", data["bullet"])
        self.assertIn("10M requests/day", data["bullet"])

    def test_preserves_structural_whitespace(self):
        # Structural newlines between key-value pairs should be preserved.
        text = '{\n  "a": 1,\n  "b": 2\n}'
        data = jl.loads(text)
        self.assertEqual(data, {"a": 1, "b": 2})

    def test_preserves_already_escaped(self):
        # Already-escaped sequences should NOT be double-escaped.
        text = '{"text": "line1\\nline2"}'
        data = jl.loads(text)
        self.assertEqual(data["text"], "line1\nline2")

    def test_diagnostics_control_chars(self):
        text = '{"summary": "Line one\nLine two"}'
        data, step = jl.loads_with_diagnostics(text)
        self.assertEqual(step, "control_chars")


# ---------------------------------------------------------------------------
# Brace / bracket balancing
# ---------------------------------------------------------------------------
class TestBalanceBraces(unittest.TestCase):
    def test_missing_closing_brace(self):
        text = '{"name": "test", "value": 42'
        data = jl.loads(text)
        self.assertEqual(data, {"name": "test", "value": 42})

    def test_missing_closing_bracket(self):
        text = '["a", "b", "c"'
        data = jl.loads(text)
        self.assertEqual(data, ["a", "b", "c"])

    def test_missing_nested_closers(self):
        text = '{"items": [1, 2, 3]'
        data = jl.loads(text)
        self.assertEqual(data, {"items": [1, 2, 3]})

    def test_missing_two_closers(self):
        text = '{"data": {"nested": true'
        data = jl.loads(text)
        self.assertEqual(data, {"data": {"nested": True}})

    def test_missing_three_closers(self):
        text = '{"a": {"b": [1, 2'
        data = jl.loads(text)
        self.assertEqual(data, {"a": {"b": [1, 2]}})

    def test_does_not_fix_severe_imbalance(self):
        # 4+ missing closers is too broken to guess.
        text = '{"a": {"b": {"c": {"d": [1'
        with self.assertRaises(json.JSONDecodeError):
            jl.loads(text)

    def test_diagnostics_balanced_braces(self):
        text = '{"name": "test"'
        data, step = jl.loads_with_diagnostics(text)
        self.assertEqual(step, "balanced_braces")


# ---------------------------------------------------------------------------
# Mismatched closing braces / brackets
# ---------------------------------------------------------------------------
class TestMismatchedClosers(unittest.TestCase):
    def test_array_closed_with_brace(self):
        text = '{"positions": {"kyte": [{"bold": "a", "text": "b"}}, "tmobile": []}}'
        data = jl.loads(text)
        self.assertEqual(data["positions"]["kyte"][0]["bold"], "a")
        self.assertEqual(data["positions"]["tmobile"], [])

    def test_object_closed_with_bracket(self):
        text = '{"meta": {"ok": true], "items": [1, 2]}'
        data = jl.loads(text)
        self.assertEqual(data["meta"]["ok"], True)
        self.assertEqual(data["items"], [1, 2])

    def test_diagnostics_mismatched_closers(self):
        text = '{"positions": {"kyte": [{"bold": "a", "text": "b"}}, "tmobile": []}}'
        _data, step = jl.loads_with_diagnostics(text)
        self.assertEqual(step, "mismatched_closers")


# ---------------------------------------------------------------------------
# Single-quoted strings
# ---------------------------------------------------------------------------
class TestSingleQuotes(unittest.TestCase):
    def test_simple_single_quotes(self):
        text = "{'name': 'Alice', 'age': 30}"
        data = jl.loads(text)
        self.assertEqual(data, {"name": "Alice", "age": 30})

    def test_single_quoted_array(self):
        text = "['one', 'two', 'three']"
        data = jl.loads(text)
        self.assertEqual(data, ["one", "two", "three"])

    def test_single_quotes_with_nested_double(self):
        # Single-quoted strings containing double quotes.
        text = """{'title': 'She said "hello"', 'count': 1}"""
        data = jl.loads(text)
        self.assertEqual(data["title"], 'She said "hello"')

    def test_single_quotes_with_boolean_none(self):
        # Python True/False/None vs JSON true/false/null.
        text = "{'active': True, 'deleted': False, 'notes': None}"
        data = jl.loads(text)
        self.assertEqual(data, {"active": True, "deleted": False, "notes": None})

    def test_diagnostics_single_quotes(self):
        text = "{'key': 'value'}"
        data, step = jl.loads_with_diagnostics(text)
        self.assertEqual(step, "single_quotes")


# ---------------------------------------------------------------------------
# Combined repairs (multiple issues in one document)
# ---------------------------------------------------------------------------
class TestCombinedRepairs(unittest.TestCase):
    def test_trailing_comma_and_control_chars(self):
        text = '{"a": "line\none", "b": 2,}'
        data = jl.loads(text)
        self.assertEqual(data["a"], "line\none")
        self.assertEqual(data["b"], 2)

    def test_control_chars_and_missing_brace(self):
        text = '{"summary": "first\nsecond"'
        data = jl.loads(text)
        self.assertEqual(data["summary"], "first\nsecond")

    def test_trailing_comma_and_missing_brace(self):
        text = '{"items": [1, 2,]'
        data = jl.loads(text)
        self.assertEqual(data, {"items": [1, 2]})


# ---------------------------------------------------------------------------
# Severely broken JSON still raises
# ---------------------------------------------------------------------------
class TestBrokenJson(unittest.TestCase):
    def test_total_garbage(self):
        with self.assertRaises(json.JSONDecodeError):
            jl.loads("this is not json at all")

    def test_empty_string(self):
        with self.assertRaises(json.JSONDecodeError):
            jl.loads("")

    def test_truncated_mid_string(self):
        # Truncated in the middle of a string value — unrecoverable.
        with self.assertRaises((json.JSONDecodeError, ValueError)):
            jl.loads('{"name": "Ali')

    def test_deeply_unbalanced(self):
        with self.assertRaises(json.JSONDecodeError):
            jl.loads("{{{{{")

    def test_diagnostics_raises_on_failure(self):
        with self.assertRaises(json.JSONDecodeError):
            jl.loads_with_diagnostics("not json")


# ---------------------------------------------------------------------------
# load() function (file-like input)
# ---------------------------------------------------------------------------
class TestLoadFromFile(unittest.TestCase):
    def test_load_valid(self):
        import io

        fh = io.StringIO('{"x": 1}')
        self.assertEqual(jl.load(fh), {"x": 1})

    def test_load_trailing_comma(self):
        import io

        fh = io.StringIO('{"x": 1,}')
        self.assertEqual(jl.load(fh), {"x": 1})


if __name__ == "__main__":
    unittest.main()
