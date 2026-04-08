import importlib.util
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_write_greenhouse_failed_result_persists_current_attempt_artifacts(tmp_path):
    mod = load_module("greenhouse_failure_artifacts", "scripts/greenhouse_failure_artifacts.py")

    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    debug_html = submit_dir / "greenhouse_submit_debug.html"
    debug_png = submit_dir / "greenhouse_submit_debug.png"
    unknown_questions = submit_dir / "greenhouse_unknown_questions.json"
    report_md = submit_dir / "greenhouse_autofill_report.md"
    report_json = submit_dir / "greenhouse_autofill_report.json"

    for path, contents in (
        (debug_html, "<html></html>"),
        (debug_png, "png"),
        (unknown_questions, "{}"),
        (report_md, "# report\n"),
        (report_json, "{}\n"),
    ):
        path.write_text(contents, encoding="utf-8")

    payload = {
        "job_url": "https://boards.greenhouse.io/acme/jobs/1",
        "company": "Acme",
        "job_title": "Principal Product Manager",
        "artifacts": {
            "report_markdown": str(report_md),
            "report_json": str(report_json),
            "unknown_questions_json": str(unknown_questions),
            "submit_debug_html": str(debug_html),
            "submit_debug_screenshot": str(debug_png),
        },
    }

    result_path = mod.write_greenhouse_failed_result(
        out_dir,
        payload,
        failure_type=mod.GREENHOUSE_REVIEW_PROOF_GAP_FAILURE,
        message="Greenhouse autofill reached review with unconfirmed fields.",
        current_page="review",
        page_index=4,
        validation_errors=["question_123", "question_456"],
    )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["board"] == "greenhouse"
    assert result["failure_type"] == "greenhouse_review_proof_gap"
    assert result["current_page"] == "review"
    assert result["page_index"] == 4
    assert result["validation_errors"] == ["question_123", "question_456"]
    assert result["artifacts"]["submit_debug_html"] == str(debug_html)
    assert result["artifacts"]["submit_debug_screenshot"] == str(debug_png)
    assert result["artifacts"]["unknown_questions_json"] == str(unknown_questions)


def test_clear_greenhouse_failure_artifacts_removes_only_failed_submission_results(tmp_path):
    mod = load_module("greenhouse_failure_artifacts", "scripts/greenhouse_failure_artifacts.py")

    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    result_path = submit_dir / "application_submission_result.json"
    debug_html = submit_dir / "greenhouse_submit_debug.html"
    debug_png = submit_dir / "greenhouse_submit_debug.png"
    unknown_questions = submit_dir / "greenhouse_unknown_questions.json"

    payload = {
        "artifacts": {
            "submit_debug_html": str(debug_html),
            "submit_debug_screenshot": str(debug_png),
            "unknown_questions_json": str(unknown_questions),
        }
    }

    result_path.write_text(json.dumps({"status": "failed"}) + "\n", encoding="utf-8")
    debug_html.write_text("<html></html>", encoding="utf-8")
    debug_png.write_text("png", encoding="utf-8")
    unknown_questions.write_text("{}", encoding="utf-8")

    mod.clear_greenhouse_failure_artifacts(out_dir, payload)

    assert not result_path.exists()
    assert not debug_html.exists()
    assert not debug_png.exists()
    assert not unknown_questions.exists()

    result_path.write_text(json.dumps({"status": "confirmed"}) + "\n", encoding="utf-8")
    mod.clear_greenhouse_failure_artifacts(out_dir, payload)
    assert result_path.exists()
