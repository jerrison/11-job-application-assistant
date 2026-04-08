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


class BuildMacAppTests(unittest.TestCase):
    def test_pyinstaller_args_bundle_runtime_assets(self):
        build_mac_app = load_module("build_mac_app", "scripts/build_mac_app.py")

        args = build_mac_app.pyinstaller_args()
        joined = "\n".join(args)

        self.assertIn("--add-binary", args)
        self.assertIn("tls_client/dependencies", joined)
        self.assertIn("tls-client", joined)
        self.assertIn("assets:assets", joined)
        self.assertIn("scripts/prompts:scripts/prompts", joined)
        self.assertIn("scripts/static:scripts/static", joined)
        self.assertIn("governance/runtime-policy.json:governance", joined)
        self.assertIn(str(PROJECT_ROOT / "scripts" / "mac_app_launcher.py"), args)


if __name__ == "__main__":
    unittest.main()
