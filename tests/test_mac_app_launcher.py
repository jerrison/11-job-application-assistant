import importlib.util
import socket
import sys
import tempfile
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


class MacAppLauncherTests(unittest.TestCase):
    def test_prepare_packaged_environment_creates_runtime_directories(self):
        launcher = load_module("mac_app_launcher", "scripts/mac_app_launcher.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "app-home"
            environ = {"JOB_ASSETS_APP_HOME": str(runtime_root)}

            prepared = launcher.prepare_packaged_environment(environ=environ)
            self.assertEqual(prepared["JOB_ASSETS_PACKAGED"], "1")
            self.assertEqual(prepared["UV_CACHE_DIR"], str(runtime_root / ".uv-cache"))
            self.assertTrue((runtime_root / "output").is_dir())
            self.assertTrue((runtime_root / ".job-assets").is_dir())
            self.assertTrue((runtime_root / "logs").is_dir())
            self.assertTrue((runtime_root / "traces").is_dir())

    def test_resolve_server_port_advances_when_preferred_port_is_busy(self):
        launcher = load_module("mac_app_launcher", "scripts/mac_app_launcher.py")

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            preferred_port = busy.getsockname()[1]

            resolved = launcher.resolve_server_port("127.0.0.1", preferred_port)

        self.assertNotEqual(resolved, preferred_port)
        self.assertGreater(resolved, 0)


if __name__ == "__main__":
    unittest.main()
