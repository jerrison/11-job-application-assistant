import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_command_with_timeout.py"


class RunCommandWithTimeoutTests(unittest.TestCase):
    def test_runs_command_without_timeout_and_writes_output(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--timeout-seconds",
                "5",
                "--",
                sys.executable,
                "-c",
                "print('hello from subprocess')",
            ],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertIn("hello from subprocess", completed.stdout)

    def test_times_out_and_writes_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "timeout.log"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "--timeout-seconds",
                    "1",
                    "--log-file",
                    str(log_path),
                    "--",
                    sys.executable,
                    "-c",
                    "import time; time.sleep(2)",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
            )

            log_text = log_path.read_text(encoding="utf-8")

        self.assertEqual(completed.returncode, 124)
        self.assertIn("timed out after 1s", log_text)
