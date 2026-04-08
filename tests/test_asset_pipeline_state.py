import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

VALID_RESUME_CONTENT = json.dumps(
    {
        "tagline": "Senior PM | AI/ML | Wharton MBA",
        "summary": "A strong PM.",
        "positions": {
            "moodys": [{"bold": f"Moody's bullet {idx}, ", "text": "text"} for idx in range(1, 7)],
            "kyte": [{"bold": f"Kyte bullet {idx}, ", "text": "text"} for idx in range(1, 6)],
            "tmobile": [{"bold": f"T-Mobile bullet {idx}, ", "text": "text"} for idx in range(1, 4)],
            "lyft": [{"bold": "Lyft bullet 1, ", "text": "text"}],
            "allstate": [{"bold": "Allstate bullet 1, ", "text": "text"}],
        },
    }
)


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class AssetPipelineStateTests(unittest.TestCase):
    def test_record_content_enables_content_reuse_until_draft_changes(self):
        state = load_module("asset_pipeline_state", "scripts/asset_pipeline_state.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            content_dir = out_dir / "content"
            content_dir.mkdir(parents=True)
            (content_dir / "resume_content_draft.json").write_text('{"draft":1}', encoding="utf-8")
            (content_dir / "resume_content.json").write_text(VALID_RESUME_CONTENT, encoding="utf-8")
            (content_dir / "cover_letter_text.txt").write_text("cover letter", encoding="utf-8")

            state.record_content(out_dir)

            self.assertTrue(state.can_reuse_content(out_dir))

            (content_dir / "resume_content_draft.json").write_text('{"draft":2}', encoding="utf-8")

            self.assertFalse(state.can_reuse_content(out_dir))

    def test_record_build_requires_matching_content_hashes_and_documents(self):
        state = load_module("asset_pipeline_state", "scripts/asset_pipeline_state.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            content_dir = out_dir / "content"
            documents_dir = out_dir / "documents"
            content_dir.mkdir(parents=True)
            documents_dir.mkdir(parents=True)
            (content_dir / "resume_content_draft.json").write_text('{"draft":1}', encoding="utf-8")
            (content_dir / "resume_content.json").write_text(VALID_RESUME_CONTENT, encoding="utf-8")
            (content_dir / "cover_letter_text.txt").write_text("cover letter", encoding="utf-8")
            company = "Acme"
            for name in (
                f"Candidate Name Resume - {company}.docx",
                f"Candidate Name Resume - {company}.pdf",
                f"Candidate Name Cover Letter - {company}.docx",
                f"Candidate Name Cover Letter - {company}.pdf",
                f"Candidate Name Cover Letter - {company}.txt",
            ):
                (documents_dir / name).write_text(name, encoding="utf-8")

            state.record_build(out_dir, company)

            self.assertTrue(state.can_reuse_build(out_dir, company))

            (content_dir / "resume_content.json").write_text(
                json.dumps({"tagline": "Senior PM | AI/ML | Wharton MBA", "summary": "Changed.", "positions": {}}),
                encoding="utf-8",
            )

            self.assertFalse(state.can_reuse_build(out_dir, company))

    def test_status_reports_reuse_flags(self):
        state = load_module("asset_pipeline_state", "scripts/asset_pipeline_state.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            content_dir = out_dir / "content"
            documents_dir = out_dir / "documents"
            content_dir.mkdir(parents=True)
            documents_dir.mkdir(parents=True)
            (content_dir / "resume_content_draft.json").write_text('{"draft":1}', encoding="utf-8")
            (content_dir / "resume_content.json").write_text(VALID_RESUME_CONTENT, encoding="utf-8")
            (content_dir / "cover_letter_text.txt").write_text("cover letter", encoding="utf-8")
            company = "Acme"
            for name in (
                f"Candidate Name Resume - {company}.docx",
                f"Candidate Name Resume - {company}.pdf",
                f"Candidate Name Cover Letter - {company}.docx",
                f"Candidate Name Cover Letter - {company}.pdf",
                f"Candidate Name Cover Letter - {company}.txt",
            ):
                (documents_dir / name).write_text(name, encoding="utf-8")
            state.record_build(out_dir, company)

            payload = state._status_payload(out_dir, company)

        self.assertTrue(payload["reuse_content"])
        self.assertTrue(payload["reuse_build"])
        self.assertEqual(payload["state_path"], str(out_dir / state.STATE_FILENAME))

    def test_can_reuse_build_rejects_null_summary(self):
        state = load_module("asset_pipeline_state", "scripts/asset_pipeline_state.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            content_dir = out_dir / "content"
            documents_dir = out_dir / "documents"
            content_dir.mkdir(parents=True)
            documents_dir.mkdir(parents=True)
            (content_dir / "resume_content_draft.json").write_text('{"draft":1}', encoding="utf-8")
            # Resume content with null summary — should fail quality gate
            (content_dir / "resume_content.json").write_text(
                json.dumps({"tagline": "Senior PM | AI/ML | Wharton MBA", "summary": None, "positions": {}}),
                encoding="utf-8",
            )
            (content_dir / "cover_letter_text.txt").write_text("cover letter", encoding="utf-8")
            company = "Acme"
            for name in (
                f"Candidate Name Resume - {company}.docx",
                f"Candidate Name Resume - {company}.pdf",
                f"Candidate Name Cover Letter - {company}.docx",
                f"Candidate Name Cover Letter - {company}.pdf",
                f"Candidate Name Cover Letter - {company}.txt",
            ):
                (documents_dir / name).write_text(name, encoding="utf-8")

            state.record_build(out_dir, company)

            # Should NOT be reusable because summary is null
            self.assertFalse(state.can_reuse_build(out_dir, company))

    def test_can_reuse_content_rejects_malformed_resume_content(self):
        state = load_module("asset_pipeline_state", "scripts/asset_pipeline_state.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            content_dir = out_dir / "content"
            content_dir.mkdir(parents=True)
            (content_dir / "resume_content_draft.json").write_text('{"draft":1}', encoding="utf-8")
            (content_dir / "resume_content.json").write_text('{"tagline":"bad"', encoding="utf-8")
            (content_dir / "cover_letter_text.txt").write_text("cover letter", encoding="utf-8")

            state.record_content(out_dir)

            self.assertFalse(state.can_reuse_content(out_dir))

    def test_stash_generated_content_moves_stale_files_aside(self):
        state = load_module("asset_pipeline_state", "scripts/asset_pipeline_state.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            content_dir = out_dir / "content"
            content_dir.mkdir(parents=True)
            resume_path = content_dir / "resume_content.json"
            cover_path = content_dir / "cover_letter_text.txt"
            (content_dir / "resume_content_draft.json").write_text('{"draft":1}', encoding="utf-8")
            resume_path.write_text(VALID_RESUME_CONTENT, encoding="utf-8")
            cover_path.write_text("cover letter", encoding="utf-8")

            moved = state.stash_generated_content(out_dir)

            self.assertEqual(
                moved,
                {
                    "resume_content": str(Path(f"{resume_path}.stale")),
                    "cover_letter_text": str(Path(f"{cover_path}.stale")),
                },
            )
            self.assertFalse(resume_path.exists())
            self.assertFalse(cover_path.exists())
            self.assertEqual(Path(moved["resume_content"]).read_text(encoding="utf-8"), VALID_RESUME_CONTENT)
            self.assertEqual(Path(moved["cover_letter_text"]).read_text(encoding="utf-8"), "cover letter")


if __name__ == "__main__":
    unittest.main()
