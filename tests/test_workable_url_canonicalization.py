import importlib.util
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


class WorkableUrlCanonicalizationTests(unittest.TestCase):
    def test_resolve_application_url_preserves_query_when_appending_apply(self):
        autofill = load_module("autofill_workable_url", "scripts/autofill_workable.py")
        url = "https://apply.workable.com/blueprint-bryanjohnson/j/AD79FD3CA3?utm_source=trueup.io&utm_medium=website&ref=trueup"

        self.assertEqual(
            autofill._resolve_application_url(url),
            "https://apply.workable.com/blueprint-bryanjohnson/j/AD79FD3CA3/apply/?utm_source=trueup.io&utm_medium=website&ref=trueup",
        )

    def test_navigate_to_apply_form_fallback_preserves_query_when_click_path_fails(self):
        autofill = load_module("autofill_workable_url_fallback", "scripts/autofill_workable.py")

        class StubLocator:
            @property
            def first(self):
                return self

            def count(self):
                return 0

            def is_visible(self):
                return False

            def click(self):
                raise AssertionError("click should not run when no apply control is visible")

        class StubPage:
            def __init__(self):
                self.url = (
                    "https://apply.workable.com/blueprint-bryanjohnson/j/AD79FD3CA3/"
                    "?utm_source=trueup.io&utm_medium=website&ref=trueup"
                )
                self.goto_calls: list[tuple[str, str, int]] = []

            def get_by_role(self, role, name=None):
                return StubLocator()

            def goto(self, url, wait_until=None, timeout=None):
                self.goto_calls.append((url, wait_until, timeout))
                self.url = url

        page = StubPage()
        payload: dict[str, str] = {}

        autofill._navigate_to_apply_form(page, payload)

        self.assertEqual(
            page.goto_calls,
            [
                (
                    "https://apply.workable.com/blueprint-bryanjohnson/j/AD79FD3CA3/apply/"
                    "?utm_source=trueup.io&utm_medium=website&ref=trueup",
                    "domcontentloaded",
                    30000,
                )
            ],
        )
        self.assertEqual(
            payload["application_url"],
            "https://apply.workable.com/blueprint-bryanjohnson/j/AD79FD3CA3/apply/?utm_source=trueup.io&utm_medium=website&ref=trueup",
        )
