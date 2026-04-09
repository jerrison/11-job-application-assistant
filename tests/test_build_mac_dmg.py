import importlib.util
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


class BuildMacDmgTests(unittest.TestCase):
    def test_dmg_path_for_tag_uses_release_naming_contract(self):
        module = load_module("build_mac_dmg_paths", "scripts/build_mac_dmg.py")
        distpath = PROJECT_ROOT / "dist"

        self.assertEqual(
            module.dmg_path_for_tag("v1.0.0", distpath=distpath),
            distpath / "Job-Application-Assistant-v1.0.0-macos.dmg",
        )

    def test_build_dmg_raises_when_app_is_missing_without_build_flag(self):
        module = load_module("build_mac_dmg_missing_app", "scripts/build_mac_dmg.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaises(FileNotFoundError):
                module.build_dmg(
                    tag="v1.0.0",
                    app_path=root / "dist" / "Job Application Assistant.app",
                    distpath=root / "dist",
                    workpath=root / "build" / "dmg",
                    build_app_if_missing=False,
                )

    def test_build_dmg_stages_app_and_calls_hdiutil(self):
        module = load_module("build_mac_dmg_hdiutil", "scripts/build_mac_dmg.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app_path = root / "dist" / "Job Application Assistant.app"
            app_path.mkdir(parents=True)

            with mock.patch("subprocess.run") as run_mock:
                output_path = module.build_dmg(
                    tag="v1.0.0",
                    app_path=app_path,
                    distpath=root / "dist",
                    workpath=root / "build" / "dmg",
                    build_app_if_missing=False,
                )

            self.assertEqual(
                output_path,
                root / "dist" / "Job-Application-Assistant-v1.0.0-macos.dmg",
            )
            run_mock.assert_called_once()
            args = run_mock.call_args.args[0]
            self.assertEqual(args[:2], ["hdiutil", "create"])
            self.assertIn("-srcfolder", args)
            self.assertIn("-volname", args)
            self.assertEqual(args[-1], str(output_path))


if __name__ == "__main__":
    unittest.main()
