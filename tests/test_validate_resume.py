import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    spec.loader.exec_module(module)
    return module


class _FakePage:
    def __init__(self, text: str):
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakePdf:
    def __init__(self, texts: list[str]):
        self.pages = [_FakePage(text) for text in texts]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_validate_resume_accepts_lenient_resume_content_json(tmp_path, capsys):
    validate_resume = load_module("validate_resume", "scripts/validate_resume.py")

    out_dir = tmp_path / "output" / "acme" / "senior-pm"
    content_dir = out_dir / "content"
    documents_dir = out_dir / "documents"
    content_dir.mkdir(parents=True)
    documents_dir.mkdir(parents=True)

    pdf_path = documents_dir / "Candidate Name Resume - Acme.pdf"
    pdf_path.write_text("pdf placeholder", encoding="utf-8")
    content_dir.joinpath("resume_content.json").write_text(
        (
            '{"tagline":"Senior Product Manager | AI | Wharton MBA",'
            '"summary":"Strong fit for the role.",'
            '"positions":{"moodys":['
            '{"bold":"Moody bullet 1, ","text":"text"},'
            '{"bold":"Moody bullet 2, ","text":"text"},'
            '{"bold":"Moody bullet 3, ","text":"text"},'
            '{"bold":"Moody bullet 4, ","text":"text"},'
            '{"bold":"Moody bullet 5, ","text":"text"},'
            '{"bold":"Moody bullet 6, ","text":"text"}],'
            '"kyte":['
            '{"bold":"Kyte bullet 1, ","text":"text"},'
            '{"bold":"Kyte bullet 2, ","text":"text"},'
            '{"bold":"Kyte bullet 3, ","text":"text"},'
            '{"bold":"Kyte bullet 4, ","text":"text"},'
            '{"bold":"Kyte bullet 5, ","text":"text"}],'
            '"tmobile":['
            '{"bold":"T-Mobile bullet 1, ","text":"text"},'
            '{"bold":"T-Mobile bullet 2, ","text":"text"},'
            '{"bold":"T-Mobile bullet 3, ","text":"text"}],'
            '"lyft":[{"bold":"Lyft bullet 1, ","text":"text"}],'
            '"allstate":[{"bold":"Allstate bullet 1, ","text":"text"}]}}'
        ),
        encoding="utf-8",
    )

    fake_pdf = _FakePdf(["Candidate Name", "More Candidate Name"])

    with (
        patch.object(validate_resume.pdfplumber, "open", return_value=fake_pdf),
        patch.object(sys, "argv", ["validate_resume.py", str(pdf_path)]),
        pytest.raises(SystemExit) as excinfo,
    ):
        validate_resume.main()

    captured = capsys.readouterr()
    assert excinfo.value.code == 0
    assert "PASS: Resume summary is present." in captured.out
    assert "PASS: Resume is exactly 2 pages." in captured.out
