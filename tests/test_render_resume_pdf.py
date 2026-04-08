import importlib.util
import os
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


class RenderResumePdfPathsTests(unittest.TestCase):
    def test_module_paths_follow_runtime_home_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "runtime-home"
            with mock.patch.dict(os.environ, {"JOB_ASSETS_APP_HOME": str(runtime_root)}, clear=True):
                render_resume_pdf = load_module("render_resume_pdf_runtime_home", "scripts/render_resume_pdf.py")

            self.assertEqual(render_resume_pdf.INPUT_MD, runtime_root / "master_resume.md")
            self.assertEqual(render_resume_pdf.OUTPUT_PDF, runtime_root / "output" / "pdf" / "master_resume.pdf")
            self.assertEqual(
                render_resume_pdf.TMP_FIT_PDF,
                runtime_root / "tmp" / "pdfs" / "master_resume_fit_check.pdf",
            )


if __name__ == "__main__":
    unittest.main()
