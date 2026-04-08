import importlib.util
import sys
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


class ApplicationSubmitCommonPathSuffixTests(unittest.TestCase):
    def test_append_url_path_suffix_preserves_query_and_fragment(self):
        common = load_module("application_submit_common_path_suffix", "scripts/application_submit_common.py")

        self.assertEqual(
            common.append_url_path_suffix(
                "https://apply.workable.com/company/j/123?utm_source=trueup#section",
                "/apply/",
            ),
            "https://apply.workable.com/company/j/123/apply/?utm_source=trueup#section",
        )

    def test_append_url_path_suffix_does_not_duplicate_existing_suffix(self):
        common = load_module("application_submit_common_path_suffix", "scripts/application_submit_common.py")

        self.assertEqual(
            common.append_url_path_suffix(
                "https://ats.rippling.com/rippling/jobs/123/apply?utm_source=trueup",
                "/apply",
            ),
            "https://ats.rippling.com/rippling/jobs/123/apply?utm_source=trueup",
        )
