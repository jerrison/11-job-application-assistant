# tests/test_pipeline_orchestrator.py
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest
from pipeline_audit_loop import AuditDecision
from pipeline_orchestrator import (
    RETRY_AFTER_SENTINEL,
    _build_fix_prompt,
    _escalated_timeout,
    _finalize_pending_answer_refresh,
    _find_payload_path,
    _get_provider_chain,
    _handle_draft_audit_decision,
    _run_phases_1_2,
    _schedule_audit_retry,
    _validate_draft_completeness,
    auto_fix,
    provider_fallback,
    requeue_jobs_for_repair_redraft,
    reset_job_to_new,
    stop_jobs_for_exhausted_repair_cluster,
)


def _mock_run_factory(results: dict[str, int]):
    """Create a mock subprocess.run that returns exit codes based on --provider arg."""

    def mock_run(cmd, **kw):
        provider = cmd[cmd.index("--provider") + 1] if "--provider" in cmd else None
        r = MagicMock()
        r.returncode = results.get(provider, 1)
        return r

    return mock_run


def test_provider_fallback_returns_first_success():
    """First provider that exits 0 wins."""
    results = {"gemini": 1, "gemini-flash": 1, "claude": 0}
    with patch("subprocess.run", side_effect=_mock_run_factory(results)) as mock_run:
        provider, rc = provider_fallback(
            base_cmd=["uv", "run", "python", "scripts/run_pipeline.py", "/tmp/test"],
            providers=["gemini", "gemini-flash", "claude"],
        )
    assert provider == "claude"
    assert rc == 0
    assert mock_run.call_count == 3


def test_provider_fallback_stops_after_first_success():
    """Should not try remaining providers after a success."""
    results = {"gemini": 0, "claude": 0}
    with patch("subprocess.run", side_effect=_mock_run_factory(results)) as mock_run:
        provider, rc = provider_fallback(
            base_cmd=["uv", "run", "python", "test.py"],
            providers=["gemini", "claude", "codex"],
        )
    assert provider == "gemini"
    assert rc == 0
    assert mock_run.call_count == 1


def test_provider_fallback_all_fail():
    """When all providers fail, return None and last exit code."""
    results = {"gemini": 1, "claude": 2}
    with patch("subprocess.run", side_effect=_mock_run_factory(results)) as mock_run:
        provider, rc = provider_fallback(
            base_cmd=["uv", "run", "python", "test.py"],
            providers=["gemini", "claude"],
        )
    assert provider is None
    assert rc == 2  # last exit code
    assert mock_run.call_count == 2


def test_provider_fallback_marks_all_provider_rate_limit_failures_as_llm_rate_limited(monkeypatch):
    import pipeline_orchestrator

    monkeypatch.setattr(pipeline_orchestrator, "_RATE_LIMIT_RETRIES", 0)

    with (
        patch(
            "subprocess.run",
            side_effect=[
                MagicMock(returncode=1, stdout="", stderr="429 Too Many Requests: retry later"),
                MagicMock(returncode=1, stdout="", stderr="You have hit your usage limit. Retry later."),
            ],
        ) as mock_run,
        patch("pipeline_orchestrator.time.sleep") as sleep_mock,
    ):
        provider, rc = provider_fallback(
            base_cmd=["uv", "run", "python", "test.py"],
            providers=["openai", "gemini"],
        )

    assert provider is None
    assert rc == 1
    assert provider_fallback.last_failure_type == "llm_rate_limited"
    assert mock_run.call_count == 2
    assert sleep_mock.called


def test_get_provider_chain_filters_to_openai_and_gemini(monkeypatch):
    monkeypatch.setenv("ASSET_LLM_PROVIDER", "openai")
    monkeypatch.setenv("ASSET_LLM_PROVIDER_CHAIN", "openai,gemini,claude,codex")

    assert _get_provider_chain() == ["openai", "gemini"]


def test_provider_fallback_appends_provider_flag():
    """Verify --provider <name> is appended to base_cmd."""
    with patch("subprocess.run", side_effect=_mock_run_factory({"gemini": 0})) as mock_run:
        provider_fallback(
            base_cmd=["uv", "run", "python", "test.py", "/some/path"],
            providers=["gemini"],
        )
    called_cmd = mock_run.call_args_list[0][0][0]
    assert "--provider" in called_cmd
    idx = called_cmd.index("--provider")
    assert called_cmd[idx + 1] == "gemini"


def test_provider_fallback_empty_providers():
    """Empty providers list should return None and exit code 1."""
    with patch("subprocess.run") as mock_run:
        provider, rc = provider_fallback(
            base_cmd=["uv", "run", "python", "test.py"],
            providers=[],
        )
    assert provider is None
    assert rc == 1
    mock_run.assert_not_called()


def test_provider_fallback_single_provider_success():
    """Single provider that succeeds."""
    with patch("subprocess.run", side_effect=_mock_run_factory({"claude": 0})):
        provider, rc = provider_fallback(
            base_cmd=["uv", "run", "python", "test.py"],
            providers=["claude"],
        )
    assert provider == "claude"
    assert rc == 0


def test_provider_fallback_single_provider_failure():
    """Single provider that fails."""
    with patch("subprocess.run", side_effect=_mock_run_factory({"claude": 42})):
        provider, rc = provider_fallback(
            base_cmd=["uv", "run", "python", "test.py"],
            providers=["claude"],
        )
    assert provider is None
    assert rc == 42


def test_provider_fallback_passes_timeout():
    """Timeout parameter should be forwarded to the worker subprocess helper."""
    completed = MagicMock(returncode=0, stdout="", stderr="")
    with patch("pipeline_orchestrator._run_worker_subprocess", return_value=completed) as mock_run:
        provider_fallback(
            base_cmd=["uv", "run", "python", "test.py"],
            providers=["gemini"],
            timeout=300,
        )
    _, kwargs = mock_run.call_args
    assert kwargs.get("timeout") == 300


def test_provider_fallback_uses_devnull_stdin():
    with patch("subprocess.run", side_effect=_mock_run_factory({"gemini": 0})) as mock_run:
        provider_fallback(
            base_cmd=["uv", "run", "python", "test.py"],
            providers=["gemini"],
        )
    _, kwargs = mock_run.call_args
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_provider_fallback_no_timeout_by_default():
    """When timeout is None, subprocess.run should not receive a timeout kwarg."""
    with patch("subprocess.run", side_effect=_mock_run_factory({"gemini": 0})) as mock_run:
        provider_fallback(
            base_cmd=["uv", "run", "python", "test.py"],
            providers=["gemini"],
        )
    _, kwargs = mock_run.call_args
    assert "timeout" not in kwargs


def test_run_worker_subprocess_kills_process_group_on_timeout():
    import pipeline_orchestrator

    process = MagicMock()
    process.pid = 4242
    process.communicate.side_effect = subprocess.TimeoutExpired(["bash", "apply.sh"], 300)

    with (
        patch("subprocess.Popen", return_value=process) as popen_mock,
        patch("pipeline_orchestrator.os.getpgid", return_value=4242),
        patch("pipeline_orchestrator.os.killpg") as killpg_mock,
    ):
        with pytest.raises(subprocess.TimeoutExpired):
            pipeline_orchestrator._run_worker_subprocess(
                ["bash", "apply.sh", "https://example.com/job"],
                capture_output=True,
                text=True,
                timeout=300,
            )

    _, kwargs = popen_mock.call_args
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    killpg_mock.assert_any_call(4242, pipeline_orchestrator.signal.SIGTERM)


def test_provider_fallback_stops_on_provider_independent_jd_extraction_failure():
    extraction_failure = MagicMock(
        returncode=1,
        stdout="",
        stderr=(
            "ERROR: URL-based JD extraction did not produce a usable job description.\n"
            "The workflow is stopping instead of generating assets from weak content.\n"
            "  - parser could not identify a credible job title from the extracted page\n"
        ),
    )

    with (
        patch("subprocess.run", side_effect=[extraction_failure]) as mock_run,
        patch("pipeline_orchestrator.time.sleep") as sleep_mock,
    ):
        provider, rc = provider_fallback(
            base_cmd=["uv", "run", "python", "test.py"],
            providers=["gemini", "claude", "openai"],
        )

    assert provider is None
    assert rc == 1
    assert mock_run.call_count == 1
    sleep_mock.assert_not_called()


def test_provider_fallback_stops_on_terminal_job_closed_extraction_failure():
    extraction_failure = MagicMock(
        returncode=1,
        stdout="",
        stderr=(
            "ERROR: URL-based JD extraction did not produce a usable job description.\n"
            "The workflow is stopping instead of generating assets from weak content.\n"
            "  - job_closed: URL returned HTTP 404 at https://example.com/jobs/123\n"
        ),
    )

    with (
        patch("subprocess.run", side_effect=[extraction_failure]) as mock_run,
        patch("pipeline_orchestrator.time.sleep") as sleep_mock,
    ):
        provider, rc = provider_fallback(
            base_cmd=["uv", "run", "python", "test.py"],
            providers=["gemini", "openai"],
        )

    assert provider is None
    assert rc == 1
    assert provider_fallback.last_error_hint.startswith("job_closed:")
    assert mock_run.call_count == 1
    sleep_mock.assert_not_called()


def test_provider_fallback_stops_on_terminal_skipped_captcha_extraction_failure():
    extraction_failure = MagicMock(
        returncode=1,
        stdout="",
        stderr=(
            "ERROR: URL-based JD extraction did not produce a usable job description.\n"
            "The workflow is stopping instead of generating assets from weak content.\n"
            "  - skipped_captcha: The job board blocked access to the job description behind an anti-bot challenge at https://www.tesla.com/careers/search/job/251870\n"
        ),
    )

    with (
        patch("subprocess.run", side_effect=[extraction_failure]) as mock_run,
        patch("pipeline_orchestrator.time.sleep") as sleep_mock,
    ):
        provider, rc = provider_fallback(
            base_cmd=["uv", "run", "python", "test.py"],
            providers=["gemini", "openai"],
        )

    assert provider is None
    assert rc == 1
    assert provider_fallback.last_error_hint.startswith("skipped_captcha:")
    assert mock_run.call_count == 1
    sleep_mock.assert_not_called()


def test_auto_fix_uses_openai_provider_command_with_file_tools_enabled():
    """OpenAI auto-fix must stay on OpenAI and enable file tools."""
    error_context = {
        "exit_code": 1,
        "url": "https://example.com/job/123",
        "output_dir": "/tmp/output",
    }

    def _mock_subprocess_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    with (
        patch.dict("os.environ", {"ASSET_LLM_PROVIDER": "openai"}, clear=False),
        patch("pipeline_orchestrator.provider_available", return_value=True) as provider_available_mock,
        patch("pipeline_orchestrator._git_current_branch", return_value="main"),
        patch(
            "pipeline_orchestrator.provider_command_for_mode",
            return_value=[
                sys.executable,
                "scripts/openai_provider.py",
                "--model",
                "gpt-5.4",
                "--file-tools",
                "prompt",
            ],
        ) as provider_command_mock,
        patch(
            "pipeline_orchestrator._run_worker_subprocess",
            side_effect=[
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ],
        ),
        patch("pipeline_orchestrator.subprocess.run", side_effect=_mock_subprocess_run),
    ):
        assert auto_fix(error_context, "greenhouse", max_attempts=1) is True

    provider_available_mock.assert_called_once_with("openai")
    args, kwargs = provider_command_mock.call_args
    assert args[0] == "openai"
    assert kwargs["mode"] == "fix"


def test_auto_fix_skips_when_openai_provider_is_not_ready():
    error_context = {
        "exit_code": 1,
        "url": "https://example.com/job/123",
        "output_dir": "/tmp/output",
    }

    with (
        patch.dict("os.environ", {"ASSET_LLM_PROVIDER": "openai"}, clear=False),
        patch("pipeline_orchestrator.provider_available", return_value=False) as provider_available_mock,
        patch("pipeline_orchestrator.subprocess.run") as subprocess_run_mock,
    ):
        assert auto_fix(error_context, "greenhouse", max_attempts=1) is False

    provider_available_mock.assert_called_once_with("openai")
    subprocess_run_mock.assert_not_called()


def test_build_fix_prompt_forbids_provider_from_running_tests_or_git():
    error_context = {
        "exit_code": 1,
        "url": "https://example.com/job/456",
        "output_dir": "/tmp/output",
    }

    prompt = _build_fix_prompt(error_context, "greenhouse")

    assert "Do not run tests or git commands yourself" in prompt
    assert "Run the relevant tests after your fix." not in prompt


def test_process_job_auto_fix_gate_does_not_rewrite_openai_to_claude(tmp_path):
    import pipeline_orchestrator

    conn = sqlite3.connect(":memory:")
    output_dir = tmp_path / "job-output"
    output_dir.mkdir()
    submit_dir = output_dir / "submit"
    submit_dir.mkdir()

    job = {
        "id": 1,
        "url": "https://boards.example/jobs/123",
        "board_url": "https://boards.example/jobs/123",
        "board": "greenhouse",
        "status": "submitting",
        "output_dir": str(output_dir),
        "source": "direct",
        "company": "Example Co",
        "role_title": "principal-pm",
        "failure_type": None,
    }

    class _DummyThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def join(self, timeout=None):
            pass

    submit_result = MagicMock()
    submit_result.returncode = 1
    submit_result.stdout = ""
    submit_result.stderr = "submit failed"

    with (
        patch.dict("os.environ", {"ASSET_LLM_PROVIDER": "openai"}, clear=False),
        patch("job_db.get_job", return_value=job),
        patch("job_db.ensure_job_metrics"),
        patch("job_db.start_phase", return_value=1),
        patch("job_db.end_phase"),
        patch("job_db.log_event"),
        patch("job_db.update_job_metrics"),
        patch("job_db.update_progress"),
        patch("job_db.get_job_metrics", return_value=None),
        patch("job_db.get_recent_auth_failures", return_value=0),
        patch("job_db.update_status"),
        patch.object(pipeline_orchestrator, "_detect_and_log_content_edits"),
        patch.object(pipeline_orchestrator, "_finalize_pending_answer_refresh", return_value=None),
        patch.object(pipeline_orchestrator.threading, "Thread", _DummyThread),
        patch.object(pipeline_orchestrator, "_run_worker_subprocess", return_value=submit_result),
        patch.object(
            pipeline_orchestrator,
            "provider_available",
            side_effect=lambda name: name == "openai",
        ) as provider_available_mock,
        patch.object(pipeline_orchestrator, "auto_fix", return_value=False) as auto_fix_mock,
        patch.object(pipeline_orchestrator, "_find_payload_path", return_value=None),
        patch.object(pipeline_orchestrator, "_auto_retry_if_transient", return_value="stopped"),
    ):
        status = pipeline_orchestrator.process_job(conn, 1, auto_submit=False)

    assert status == "stopped"
    auto_fix_mock.assert_called_once()
    consulted_providers = [call.args[0] for call in provider_available_mock.call_args_list]
    assert "openai" in consulted_providers
    assert "claude" not in consulted_providers


def test_find_payload_path_accepts_string_output_dir(tmp_path):
    submit_dir = tmp_path / "submit"
    submit_dir.mkdir()
    payload = submit_dir / "autofill_payload_linkedin.json"
    payload.write_text("{}", encoding="utf-8")

    assert _find_payload_path(str(tmp_path), "linkedin") == payload


def test_load_pipeline_meta_corrects_generic_company_from_saved_jd_raw(tmp_path):
    import pipeline_orchestrator

    content_dir = tmp_path / "content"
    content_dir.mkdir()
    (content_dir / "jd_raw.md").write_text(
        "Linktree hiring Principal Product Manager in San Francisco Bay Area | LinkedIn\n",
        encoding="utf-8",
    )
    meta_path = tmp_path / ".pipeline_meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "company": "the-role",
                "company_proper": "The Role",
                "role": "principal-product-manager",
                "jd_source": "https://www.linkedin.com/jobs/view/123",
                "jd_source_resolved": "https://www.linkedin.com/jobs/view/123",
            }
        ),
        encoding="utf-8",
    )

    meta = pipeline_orchestrator._load_pipeline_meta(tmp_path)
    saved = json.loads(meta_path.read_text(encoding="utf-8"))

    assert meta is not None
    assert meta["company_proper"] == "Linktree"
    assert meta["company"] == "linktree"
    assert saved["company_proper"] == "Linktree"
    assert saved["company"] == "linktree"


def test_load_pipeline_meta_corrects_workday_loaded_page_wrapper_company_from_saved_jd_raw(tmp_path):
    import pipeline_orchestrator

    content_dir = tmp_path / "content"
    content_dir.mkdir()
    (content_dir / "jd_raw.md").write_text(
        "# Careers\n\n"
        "Principal Product Manager page is loaded\n"
        "Principal Product Manager\n\n"
        "Position Overview\n"
        "At Autodesk, you will define the long-term product vision for opportunity lifecycle tooling.\n",
        encoding="utf-8",
    )
    meta_path = tmp_path / ".pipeline_meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "company": "autodesk",
                "company_proper": "Principal Product Manager page",
                "role": "principal-pm",
                "jd_title": "Principal Product Manager",
                "jd_source": "https://autodesk.wd1.myworkdayjobs.com/en-US/Ext/job/example",
                "jd_source_resolved": "https://autodesk.wd1.myworkdayjobs.com/en-US/Ext/job/example",
            }
        ),
        encoding="utf-8",
    )

    meta = pipeline_orchestrator._load_pipeline_meta(tmp_path)
    saved = json.loads(meta_path.read_text(encoding="utf-8"))

    assert meta is not None
    assert meta["company_proper"] == "Autodesk"
    assert meta["company"] == "autodesk"
    assert saved["company_proper"] == "Autodesk"
    assert saved["company"] == "autodesk"


def test_load_pipeline_meta_corrects_workday_careers_wrapper_company_from_saved_jd_raw(tmp_path):
    import pipeline_orchestrator

    content_dir = tmp_path / "content"
    content_dir.mkdir()
    (content_dir / "jd_raw.md").write_text(
        "# Careers\n\n"
        "Principal Product Manager\n"
        "Skip to main content\n"
        "Careers\n"
        "English\n"
        "Search for Jobs\n"
        "Principal Product Manager page is loaded\n"
        "Principal Product Manager\n\n"
        "Position Overview\n"
        "Autodesk's GTM Tech organization is hiring a Principal Product Manager to own the strategy.\n",
        encoding="utf-8",
    )
    meta_path = tmp_path / ".pipeline_meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "company": "autodesk",
                "company_proper": "Careers",
                "role": "principal-pm",
                "jd_title": "Principal Product Manager",
                "jd_source": "https://autodesk.wd1.myworkdayjobs.com/en-US/Ext/job/example",
                "jd_source_resolved": "https://autodesk.wd1.myworkdayjobs.com/en-US/Ext/job/example",
            }
        ),
        encoding="utf-8",
    )

    meta = pipeline_orchestrator._load_pipeline_meta(tmp_path)
    saved = json.loads(meta_path.read_text(encoding="utf-8"))

    assert meta is not None
    assert meta["company_proper"] == "Autodesk"
    assert meta["company"] == "autodesk"
    assert saved["company_proper"] == "Autodesk"
    assert saved["company"] == "autodesk"


def test_validate_draft_completeness_accepts_active_submit_proof(tmp_path):
    docs_dir = tmp_path / "documents"
    docs_dir.mkdir()
    (docs_dir / "Candidate Resume.pdf").write_text("pdf", encoding="utf-8")
    (docs_dir / "Candidate Cover Letter.pdf").write_text("pdf", encoding="utf-8")
    (tmp_path / "draft_summary.png").write_text("png", encoding="utf-8")

    active_submit = tmp_path / "submit-20260326T010203Z"
    active_submit.mkdir()
    (tmp_path / ".active_submit_dir").write_text("submit-20260326T010203Z\n", encoding="utf-8")
    (active_submit / "ashby_autofill_report.json").write_text("{}", encoding="utf-8")
    (active_submit / "ashby_autofill_pre_submit.png").write_text("png", encoding="utf-8")

    missing = _validate_draft_completeness(tmp_path, board_name="ashby")

    assert missing == []


def test_validate_draft_completeness_requires_screenshot_from_active_submit_attempt(tmp_path):
    docs_dir = tmp_path / "documents"
    docs_dir.mkdir()
    (docs_dir / "Candidate Resume.pdf").write_text("pdf", encoding="utf-8")
    (docs_dir / "Candidate Cover Letter.pdf").write_text("pdf", encoding="utf-8")
    (tmp_path / "draft_summary.png").write_text("png", encoding="utf-8")

    stale_submit = tmp_path / "submit"
    stale_submit.mkdir()
    (stale_submit / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")

    active_submit = tmp_path / "submit-20260326T010203Z"
    active_submit.mkdir()
    (tmp_path / ".active_submit_dir").write_text("submit-20260326T010203Z\n", encoding="utf-8")
    (active_submit / "greenhouse_autofill_report.json").write_text("{}", encoding="utf-8")

    missing = _validate_draft_completeness(tmp_path, board_name="greenhouse")

    assert "current-attempt pre-submit screenshot" in missing
    assert "current-attempt autofill report" not in missing


def test_validate_draft_completeness_requires_review_screenshot_when_claimed_by_payload(tmp_path):
    docs_dir = tmp_path / "documents"
    docs_dir.mkdir()
    (docs_dir / "Candidate Resume.pdf").write_text("pdf", encoding="utf-8")
    (docs_dir / "Candidate Cover Letter.pdf").write_text("pdf", encoding="utf-8")
    (tmp_path / "draft_summary.png").write_text("png", encoding="utf-8")

    active_submit = tmp_path / "submit-20260326T010203Z"
    active_submit.mkdir()
    (tmp_path / ".active_submit_dir").write_text("submit-20260326T010203Z\n", encoding="utf-8")
    (active_submit / "greenhouse_autofill_report.json").write_text("{}", encoding="utf-8")
    (active_submit / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    (active_submit / "greenhouse_autofill_payload.json").write_text(
        json.dumps(
            {
                "artifacts": {
                    "review_screenshot": str(active_submit / "greenhouse_autofill_review.png"),
                }
            }
        ),
        encoding="utf-8",
    )

    missing = _validate_draft_completeness(tmp_path, board_name="greenhouse")

    assert "current-attempt review screenshot" in missing


def test_validate_draft_completeness_allows_greenhouse_active_submit_without_review_screenshot(tmp_path):
    docs_dir = tmp_path / "documents"
    docs_dir.mkdir()
    (docs_dir / "Candidate Resume.pdf").write_text("pdf", encoding="utf-8")
    (docs_dir / "Candidate Cover Letter.pdf").write_text("pdf", encoding="utf-8")
    (tmp_path / "draft_summary.png").write_text("png", encoding="utf-8")

    active_submit = tmp_path / "submit-20260326T010203Z"
    active_submit.mkdir()
    (tmp_path / ".active_submit_dir").write_text("submit-20260326T010203Z\n", encoding="utf-8")
    (active_submit / "greenhouse_autofill_report.json").write_text("{}", encoding="utf-8")
    (active_submit / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")

    missing = _validate_draft_completeness(tmp_path, board_name="greenhouse")

    assert "current-attempt review screenshot" not in missing


def test_validate_draft_completeness_rejects_reused_review_screenshot_checkpoint(tmp_path):
    docs_dir = tmp_path / "documents"
    docs_dir.mkdir()
    (docs_dir / "Candidate Resume.pdf").write_text("pdf", encoding="utf-8")
    (docs_dir / "Candidate Cover Letter.pdf").write_text("pdf", encoding="utf-8")
    (tmp_path / "draft_summary.png").write_text("png", encoding="utf-8")

    active_submit = tmp_path / "submit-20260326T010203Z"
    active_submit.mkdir()
    (tmp_path / ".active_submit_dir").write_text("submit-20260326T010203Z\n", encoding="utf-8")
    screenshot_path = active_submit / "greenhouse_autofill_pre_submit.png"
    screenshot_path.write_text("png", encoding="utf-8")
    (active_submit / "greenhouse_autofill_report.json").write_text("{}", encoding="utf-8")
    (active_submit / "greenhouse_autofill_payload.json").write_text(
        json.dumps(
            {
                "artifacts": {
                    "pre_submit_screenshot": str(screenshot_path),
                    "review_screenshot": str(screenshot_path),
                }
            }
        ),
        encoding="utf-8",
    )

    missing = _validate_draft_completeness(tmp_path, board_name="greenhouse")

    assert "distinct review screenshot proof" in missing


def test_reset_job_to_new_clears_current_attempt_artifacts_and_requeues(tmp_path):
    from answer_refresh_state import load_answer_refresh_state
    from job_db import RETRY_AFTER_SENTINEL, add_job, init_db
    from output_layout import active_submit_dir_name, set_active_submit_dir

    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    historical_submit_dir = out_dir / "submit-20260329T190046Z"
    pages_dir = submit_dir / "greenhouse_autofill_pages"
    stale_pages_dir = submit_dir / "linkedin_autofill_pages"
    pages_dir.mkdir(parents=True)
    stale_pages_dir.mkdir(parents=True)
    historical_submit_dir.mkdir(parents=True)
    set_active_submit_dir(out_dir, historical_submit_dir.name)

    job_id = add_job(
        conn,
        "https://boards.greenhouse.io/example/jobs/reset-to-new",
        company="Example",
        role_title="Principal PM",
    )
    conn.execute(
        "UPDATE jobs SET status = 'draft', board = 'greenhouse', output_dir = ?, provider = ?, "
        "progress = ?, error_message = ?, failure_type = ? WHERE id = ?",
        (str(out_dir), "openai", "Draft ready", "Needs reset", "needs_attention", job_id),
    )
    conn.commit()

    for path in (
        out_dir / ".asset_pipeline_state.json",
        out_dir / "answer_refresh_status.json",
        out_dir / "draft_status.json",
        out_dir / "draft_summary.md",
        out_dir / "draft_summary.original.md",
        out_dir / "draft_summary.png",
        submit_dir / "greenhouse_autofill_report.md",
        submit_dir / "greenhouse_autofill_report.json",
        submit_dir / "greenhouse_autofill_pre_submit.png",
        submit_dir / "greenhouse_autofill_post_submit.png",
        submit_dir / "greenhouse_unknown_questions.json",
        submit_dir / "greenhouse_submit_debug.html",
        submit_dir / "greenhouse_submit_debug.png",
        submit_dir / "greenhouse_autofill_payload.json",
        submit_dir / "greenhouse_application_page.html",
        submit_dir / "linkedin_autofill_payload.json",
        submit_dir / "linkedin_submit_debug.png",
        submit_dir / "application_submission_result.json",
        submit_dir / "pending_user_input.json",
    ):
        path.write_text("{}", encoding="utf-8")
    (pages_dir / "page_01.png").write_text("png", encoding="utf-8")
    (stale_pages_dir / "page_legacy.png").write_text("png", encoding="utf-8")

    assert reset_job_to_new(conn, job_id, initiator="web") is True

    row = conn.execute(
        "SELECT status, provider, progress, error_message, failure_type, retry_after FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    assert row["status"] == "queued"
    assert row["provider"] is None
    assert row["progress"] == ""
    assert row["error_message"] == ""
    assert row["failure_type"] is None
    assert row["retry_after"] == RETRY_AFTER_SENTINEL

    assert not (out_dir / ".asset_pipeline_state.json").exists()
    assert not (out_dir / "draft_status.json").exists()
    assert not (out_dir / "draft_summary.md").exists()
    assert not (out_dir / "draft_summary.original.md").exists()
    assert not (out_dir / "draft_summary.png").exists()
    assert not (submit_dir / "greenhouse_autofill_report.md").exists()
    assert not (submit_dir / "greenhouse_autofill_report.json").exists()
    assert not (submit_dir / "greenhouse_autofill_pre_submit.png").exists()
    assert not (submit_dir / "greenhouse_autofill_payload.json").exists()
    assert not (submit_dir / "greenhouse_application_page.html").exists()
    assert not (submit_dir / "linkedin_autofill_payload.json").exists()
    assert not (submit_dir / "linkedin_submit_debug.png").exists()
    assert not (submit_dir / "application_submission_result.json").exists()
    assert not (submit_dir / "pending_user_input.json").exists()
    assert list(pages_dir.iterdir()) == []
    assert list(stale_pages_dir.iterdir()) == []
    assert active_submit_dir_name(out_dir) == "submit"
    assert not (out_dir / ".active_submit_dir").exists()

    state = load_answer_refresh_state(out_dir)
    assert state["status"] == "pending"
    assert state["request_kind"] == "reset_to_new"

    events = conn.execute(
        "SELECT event_type FROM events WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchall()
    assert [event["event_type"] for event in events] == ["reset_to_new_requested"]


def test_prepare_submit_dir_for_new_attempt_canonicalizes_stale_pointer(tmp_path):
    import pipeline_orchestrator
    from output_layout import active_submit_dir_name, set_active_submit_dir

    out_dir = tmp_path / "job-output"
    historical_submit_dir = out_dir / "submit-20260329T190046Z"
    historical_submit_dir.mkdir(parents=True)
    set_active_submit_dir(out_dir, historical_submit_dir.name)

    submit_dir = pipeline_orchestrator._prepare_submit_dir_for_new_attempt(out_dir)

    assert submit_dir == out_dir / "submit"
    assert submit_dir.exists()
    assert active_submit_dir_name(out_dir) == "submit"
    assert not (out_dir / ".active_submit_dir").exists()


def _run_process_job_with_submission_result(
    tmp_path: Path,
    result_payload: dict,
    *,
    board: str = "linkedin",
    board_url: str = "https://www.linkedin.com/jobs/view/4376210856/",
    submit_returncode: int = 0,
) -> tuple[str, list[tuple[str, dict]], mock.Mock, mock.Mock, mock.Mock]:
    import pipeline_orchestrator

    conn = sqlite3.connect(":memory:")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)

    job = {
        "id": 1,
        "url": board_url,
        "board_url": board_url,
        "board": board,
        "status": "submitting",
        "output_dir": str(out_dir),
        "source": "direct",
        "company": "Example Co",
        "role_title": "principal-pm",
        "failure_type": None,
    }
    update_calls: list[tuple[str, dict]] = []

    class _DummyThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def join(self, timeout=None):
            pass

    def _fake_update_status(_conn, _job_id, status, **kwargs):
        update_calls.append((status, kwargs))

    def _fake_run(cmd, **kwargs):
        (submit_dir / "application_submission_result.json").write_text(
            json.dumps(result_payload),
            encoding="utf-8",
        )
        completed = MagicMock()
        completed.returncode = submit_returncode
        completed.stdout = ""
        completed.stderr = ""
        return completed

    with (
        patch("job_db.get_job", return_value=job),
        patch("job_db.ensure_job_metrics"),
        patch("job_db.start_phase", return_value=1),
        patch("job_db.end_phase"),
        patch("job_db.log_event"),
        patch("job_db.update_job_metrics"),
        patch("job_db.update_progress"),
        patch("job_db.get_job_metrics", return_value=None),
        patch("job_db.get_recent_auth_failures", return_value=0),
        patch("job_db.update_status", side_effect=_fake_update_status),
        patch.object(pipeline_orchestrator, "_detect_and_log_content_edits"),
        patch.object(pipeline_orchestrator, "_finalize_pending_answer_refresh", return_value=None),
        patch.object(pipeline_orchestrator.threading, "Thread", _DummyThread),
        patch.object(pipeline_orchestrator, "_run_worker_subprocess", side_effect=_fake_run),
        patch("draft_manager.generate_draft_summary") as generate_draft_summary,
        patch.object(pipeline_orchestrator, "_sync_draft_proof_blockers") as sync_draft_proof_blockers,
        patch.object(
            pipeline_orchestrator,
            "_validate_draft_completeness",
            return_value=[],
        ) as validate_draft_completeness,
    ):
        status = pipeline_orchestrator.process_job(conn, 1, auto_submit=False)

    return (
        status,
        update_calls,
        generate_draft_summary,
        sync_draft_proof_blockers,
        validate_draft_completeness,
    )


def _run_process_job_with_submission_attempts(
    tmp_path: Path,
    attempt_results: list[dict],
    *,
    board: str = "linkedin",
    board_url: str = "https://www.linkedin.com/jobs/view/4376210856/",
    auto_submit: bool = False,
    headless: bool | None = None,
    require_fresh_result_on_attempt_indices: tuple[int, ...] = (),
    require_fresh_auth_sidecars_on_attempt_indices: tuple[int, ...] = (),
    preexisting_auth_sidecars: tuple[str, ...] = (),
) -> tuple[str, list[tuple[str, dict]], list[list[str]], list[dict]]:
    import pipeline_orchestrator

    conn = sqlite3.connect(":memory:")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    for sidecar_name in preexisting_auth_sidecars:
        (submit_dir / sidecar_name).write_text("{}", encoding="utf-8")

    job = {
        "id": 1,
        "url": board_url,
        "board_url": board_url,
        "board": board,
        "status": "submitting",
        "output_dir": str(out_dir),
        "source": "direct",
        "company": "Example Co",
        "role_title": "principal-pm",
        "failure_type": None,
    }
    update_calls: list[tuple[str, dict]] = []
    run_commands: list[list[str]] = []
    run_kwargs_list: list[dict] = []
    attempts_iter = iter(attempt_results)

    class _DummyThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def join(self, timeout=None):
            pass

    def _fake_update_status(_conn, _job_id, status, **kwargs):
        update_calls.append((status, kwargs))

    class _FakeProcess:
        def __init__(self, attempt: dict, attempt_number: int) -> None:
            self.pid = 4242 + attempt_number
            self.returncode = attempt.get("returncode", 0)
            self._attempt = attempt
            self._attempt_number = attempt_number

        def communicate(self, timeout=None):
            del timeout
            result_path = submit_dir / "application_submission_result.json"
            if self._attempt_number in require_fresh_result_on_attempt_indices:
                assert not result_path.exists(), "stale application_submission_result.json should be cleared"
            if self._attempt_number in require_fresh_auth_sidecars_on_attempt_indices:
                assert not list(submit_dir.glob("*_auth_failure.json")), "stale *_auth_failure.json should be cleared"
            if self._attempt.get("clear_submission_result") and result_path.exists():
                result_path.unlink()
            submission_result = self._attempt.get("submission_result")
            if submission_result is not None:
                result_path.write_text(json.dumps(submission_result), encoding="utf-8")
            auth_failure_sidecar = self._attempt.get("auth_failure_sidecar")
            if auth_failure_sidecar:
                (submit_dir / str(auth_failure_sidecar)).write_text("{}", encoding="utf-8")
            return (
                self._attempt.get("stdout", ""),
                self._attempt.get("stderr", ""),
            )

    def _fake_popen(cmd, **kwargs):
        run_commands.append(list(cmd))
        run_kwargs_list.append(dict(kwargs))
        attempt = next(attempts_iter)
        attempt_number = len(run_commands)
        return _FakeProcess(attempt, attempt_number)

    with (
        patch("job_db.get_job", return_value=job),
        patch("job_db.ensure_job_metrics"),
        patch("job_db.start_phase", return_value=1),
        patch("job_db.end_phase"),
        patch("job_db.log_event"),
        patch("job_db.update_job_metrics"),
        patch("job_db.update_progress"),
        patch("job_db.get_job_metrics", return_value=None),
        patch("job_db.get_recent_auth_failures", return_value=0),
        patch("job_db.update_status", side_effect=_fake_update_status),
        patch.object(pipeline_orchestrator, "_detect_and_log_content_edits"),
        patch.object(pipeline_orchestrator, "_finalize_pending_answer_refresh", return_value=None),
        patch.object(pipeline_orchestrator, "provider_available", return_value=False),
        patch.object(pipeline_orchestrator.threading, "Thread", _DummyThread),
        patch.object(pipeline_orchestrator.subprocess, "Popen", side_effect=_fake_popen),
        patch("draft_manager.generate_draft_summary"),
        patch.object(pipeline_orchestrator, "_sync_draft_proof_blockers"),
        patch.object(
            pipeline_orchestrator,
            "_validate_draft_completeness",
            return_value=[],
        ),
    ):
        status = pipeline_orchestrator.process_job(conn, 1, auto_submit=auto_submit, headless=headless)

    return status, update_calls, run_commands, run_kwargs_list


def test_process_job_draft_mode_honors_not_easy_apply_submission_result(tmp_path):
    status, update_calls, generate_draft_summary, sync_draft_proof_blockers, validate_draft_completeness = (
        _run_process_job_with_submission_result(
            tmp_path,
            {
                "status": "not_easy_apply",
                "board": "linkedin",
                "reason": "external_apply",
            },
        )
    )

    assert status == "stopped"
    assert update_calls[-1][0] == "stopped"
    assert update_calls[-1][1]["failure_type"] == "external_apply"
    assert "Incomplete draft" not in update_calls[-1][1]["error_message"]
    generate_draft_summary.assert_not_called()
    sync_draft_proof_blockers.assert_not_called()
    validate_draft_completeness.assert_not_called()


def test_process_job_draft_mode_honors_not_easy_apply_submission_result_after_nonzero_exit(tmp_path):
    status, update_calls, generate_draft_summary, sync_draft_proof_blockers, validate_draft_completeness = (
        _run_process_job_with_submission_result(
            tmp_path,
            {
                "status": "not_easy_apply",
                "board": "linkedin",
                "reason": "external_apply",
            },
            submit_returncode=1,
        )
    )

    assert status == "stopped"
    assert update_calls[-1][0] == "stopped"
    assert update_calls[-1][1]["failure_type"] == "external_apply"
    generate_draft_summary.assert_not_called()
    sync_draft_proof_blockers.assert_not_called()
    validate_draft_completeness.assert_not_called()


def test_process_job_draft_mode_honors_job_closed_submission_result(tmp_path):
    status, update_calls, generate_draft_summary, sync_draft_proof_blockers, validate_draft_completeness = (
        _run_process_job_with_submission_result(
            tmp_path,
            {
                "status": "job_closed",
                "board": "linkedin",
                "message": "LinkedIn job closed: no longer accepting applications",
            },
        )
    )

    assert status == "stopped"
    assert update_calls[-1][0] == "stopped"
    assert update_calls[-1][1]["failure_type"] == "job_closed"
    assert "Incomplete draft" not in update_calls[-1][1]["error_message"]
    generate_draft_summary.assert_not_called()
    sync_draft_proof_blockers.assert_not_called()
    validate_draft_completeness.assert_not_called()


def test_process_job_draft_mode_honors_skipped_captcha_submission_result(tmp_path):
    status, update_calls, generate_draft_summary, sync_draft_proof_blockers, validate_draft_completeness = (
        _run_process_job_with_submission_result(
            tmp_path,
            {
                "status": "skipped_captcha",
                "board": "avature",
                "message": "Submission skipped: captcha required. Moving on to next job.",
            },
            board="avature",
            board_url="https://jobs.avature.net/careers/JobDetail/123",
        )
    )

    assert status == "stopped"
    assert update_calls[-1][0] == "stopped"
    assert update_calls[-1][1]["failure_type"] == "skipped_captcha"
    assert update_calls[-1][1]["error_message"] == "Submission skipped: captcha required. Moving on to next job."
    generate_draft_summary.assert_not_called()
    sync_draft_proof_blockers.assert_not_called()
    validate_draft_completeness.assert_not_called()


def test_process_job_draft_mode_retries_headed_once_after_headless_skipped_captcha(tmp_path):
    status, update_calls, run_commands, _ = _run_process_job_with_submission_attempts(
        tmp_path,
        [
            {
                "returncode": 0,
                "submission_result": {
                    "status": "skipped_captcha",
                    "board": "avature",
                    "message": "Submission skipped: captcha required. Moving on to next job.",
                },
            },
            {
                "returncode": 0,
            },
        ],
        board="avature",
        board_url="https://jobs.avature.net/careers/JobDetail/123",
        require_fresh_result_on_attempt_indices=(2,),
        require_fresh_auth_sidecars_on_attempt_indices=(2,),
        preexisting_auth_sidecars=("avature_auth_failure.json",),
    )

    assert status == "draft"
    assert update_calls[-1][0] == "draft"
    assert len(run_commands) == 2
    assert "--headless" in run_commands[0]
    assert "--headless" not in run_commands[1]


def test_process_job_draft_mode_stops_after_second_skipped_captcha_no_extra_retries(tmp_path):
    status, update_calls, run_commands, _ = _run_process_job_with_submission_attempts(
        tmp_path,
        [
            {
                "returncode": 0,
                "submission_result": {
                    "status": "skipped_captcha",
                    "board": "avature",
                    "message": "Submission skipped: captcha required. Moving on to next job.",
                },
            },
            {
                "returncode": 0,
                "submission_result": {
                    "status": "skipped_captcha",
                    "board": "avature",
                    "message": "Submission skipped: captcha required. Moving on to next job.",
                },
            },
        ],
        board="avature",
        board_url="https://jobs.avature.net/careers/JobDetail/123",
    )

    assert status == "stopped"
    assert update_calls[-1][0] == "stopped"
    assert update_calls[-1][1]["failure_type"] == "skipped_captcha"
    assert len(run_commands) == 2
    assert "--headless" in run_commands[0]
    assert "--headless" not in run_commands[1]


def test_process_job_auto_submit_does_not_retry_headed_after_skipped_captcha(tmp_path):
    status, update_calls, run_commands, _ = _run_process_job_with_submission_attempts(
        tmp_path,
        [
            {
                "returncode": 0,
                "submission_result": {
                    "status": "skipped_captcha",
                    "board": "avature",
                    "message": "Submission skipped: captcha required. Moving on to next job.",
                },
            }
        ],
        board="avature",
        board_url="https://jobs.avature.net/careers/JobDetail/123",
        auto_submit=True,
        headless=True,
    )

    assert status == "stopped"
    assert update_calls[-1][0] == "stopped"
    assert len(run_commands) == 1
    assert "--headless" in run_commands[0]


def test_process_job_draft_mode_retries_headed_once_for_raw_captcha_exit_code(tmp_path):
    status, update_calls, run_commands, _ = _run_process_job_with_submission_attempts(
        tmp_path,
        [
            {
                "returncode": 75,
                "stderr": "captcha required: manual solve needed",
            },
            {
                "returncode": 0,
            },
        ],
        board="avature",
        board_url="https://jobs.avature.net/careers/JobDetail/123",
        require_fresh_result_on_attempt_indices=(2,),
    )

    assert status == "draft"
    assert update_calls[-1][0] == "draft"
    assert len(run_commands) == 2
    assert "--headless" in run_commands[0]
    assert "--headless" not in run_commands[1]


def test_process_job_draft_mode_does_not_retry_headed_for_auth_skip_exit_code(tmp_path):
    status, update_calls, run_commands, _ = _run_process_job_with_submission_attempts(
        tmp_path,
        [
            {
                "returncode": 75,
                "stderr": "auth failed: challenge required",
                "submission_result": {
                    "status": "skipped_auth",
                    "board": "avature",
                    "failure_type": "auth_failed",
                    "message": "Authentication required.",
                },
                "auth_failure_sidecar": "avature_auth_failure.json",
            },
        ],
        board="avature",
        board_url="https://jobs.avature.net/careers/JobDetail/123",
    )

    assert status == "stopped"
    assert update_calls[-1][0] == "stopped"
    assert update_calls[-1][1]["failure_type"] == "auth_failed"
    assert len(run_commands) == 1
    assert "--headless" in run_commands[0]


def test_process_job_draft_mode_wrapped_skipped_captcha_with_auth_sidecar_does_not_retry(tmp_path):
    status, update_calls, run_commands, _ = _run_process_job_with_submission_attempts(
        tmp_path,
        [
            {
                "returncode": 0,
                "submission_result": {
                    "status": "skipped_captcha",
                    "board": "avature",
                    "message": "Submission skipped: captcha required. Moving on to next job.",
                },
                "auth_failure_sidecar": "avature_auth_failure.json",
            }
        ],
        board="avature",
        board_url="https://jobs.avature.net/careers/JobDetail/123",
    )

    assert status == "stopped"
    assert update_calls[-1][0] == "stopped"
    assert update_calls[-1][1]["failure_type"] == "auth_failed"
    assert len(run_commands) == 1
    assert "--headless" in run_commands[0]


def test_process_job_prefers_terminal_submission_result_over_generic_auth_sidecar(tmp_path):
    status, update_calls, run_commands, _ = _run_process_job_with_submission_attempts(
        tmp_path,
        [
            {
                "returncode": 0,
                "submission_result": {
                    "status": "skipped_captcha",
                    "board": "icims",
                    "failure_type": "skipped_captcha",
                    "auth_state": "captcha_required",
                    "auth_scope": "icims:careers-example.icims.com",
                    "message": "Submission skipped: captcha required. Moving on to next job.",
                },
                "auth_failure_sidecar": "icims_auth_failure.json",
            }
        ],
        board="icims",
        board_url="https://careers-example.icims.com/jobs/123/job",
    )

    assert status == "stopped"
    assert update_calls[-1][0] == "stopped"
    assert update_calls[-1][1]["failure_type"] == "skipped_captcha"
    assert update_calls[-1][1]["auth_state"] == "captcha_required"
    assert update_calls[-1][1]["auth_scope"] == "icims:careers-example.icims.com"
    assert len(run_commands) == 1
    assert "--headless" in run_commands[0]


def test_process_job_icims_auth_guard_is_scope_aware_and_records_auth_guarded(tmp_path):
    import pipeline_orchestrator

    conn = sqlite3.connect(":memory:")
    output_dir = tmp_path / "job-output"
    output_dir.mkdir()
    job = {
        "id": 1,
        "url": "https://www.amazon.jobs/en/jobs/3163757/senior-product-manager-technical-smart-glasses",
        "board_url": "https://www.amazon.jobs/en/jobs/3163757/senior-product-manager-technical-smart-glasses",
        "board": "icims",
        "status": "submitting",
        "output_dir": str(output_dir),
        "source": "direct",
        "company": "Amazon",
        "role_title": "Principal Product Manager",
        "failure_type": None,
    }

    with (
        patch("job_db.get_job", return_value=job),
        patch("job_db.ensure_job_metrics"),
        patch("job_db.start_phase", return_value=1),
        patch("job_db.end_phase") as end_phase,
        patch("job_db.log_event") as log_event,
        patch("job_db.update_job_metrics"),
        patch("job_db.update_progress"),
        patch("job_db.get_job_metrics", return_value=None),
        patch("job_db.get_recent_auth_failures", return_value=3) as get_recent_auth_failures,
        patch("job_db.update_status") as update_status,
    ):
        status = pipeline_orchestrator.process_job(conn, 1, auto_submit=False)

    assert status == "stopped"
    get_recent_auth_failures.assert_called_once_with(
        conn,
        "icims",
        auth_scope="icims:www.amazon.jobs",
    )
    stop_call = update_status.call_args_list[-1]
    assert stop_call.args[:3] == (conn, 1, "stopped")
    assert stop_call.kwargs["failure_type"] == "auth_guarded"
    assert stop_call.kwargs["auth_scope"] == "icims:www.amazon.jobs"
    assert "www.amazon.jobs" in stop_call.kwargs["error_message"]
    auth_skip_call = next(call for call in log_event.call_args_list if call.args[2] == "auth_skip")
    assert auth_skip_call.kwargs["detail_json"]["auth_scope"] == "icims:www.amazon.jobs"
    end_phase.assert_called_once_with(conn, 1, exit_code=1)


def test_process_job_auth_guard_prefers_stored_auth_scope_for_icims_wrapper_job(tmp_path):
    import pipeline_orchestrator

    conn = sqlite3.connect(":memory:")
    output_dir = tmp_path / "job-output"
    output_dir.mkdir()
    job = {
        "id": 1,
        "url": "https://www.amazon.jobs/en/jobs/3163757/senior-product-manager-technical-smart-glasses",
        "board_url": "https://www.amazon.jobs/en/jobs/3163757/senior-product-manager-technical-smart-glasses",
        "board": "icims",
        "auth_scope": "icims:passport.amazon.jobs",
        "status": "submitting",
        "output_dir": str(output_dir),
        "source": "direct",
        "company": "Amazon",
        "role_title": "Principal Product Manager",
        "failure_type": None,
    }

    with (
        patch("job_db.get_job", return_value=job),
        patch("job_db.ensure_job_metrics"),
        patch("job_db.start_phase", return_value=1),
        patch("job_db.end_phase"),
        patch("job_db.log_event"),
        patch("job_db.update_job_metrics"),
        patch("job_db.update_progress"),
        patch("job_db.get_job_metrics", return_value=None),
        patch("job_db.get_recent_auth_failures", return_value=3) as get_recent_auth_failures,
        patch("job_db.update_status"),
    ):
        status = pipeline_orchestrator.process_job(conn, 1, auto_submit=False)

    assert status == "stopped"
    get_recent_auth_failures.assert_called_once_with(
        conn,
        "icims",
        auth_scope="icims:passport.amazon.jobs",
    )


def test_process_job_submit_subprocess_uses_devnull_stdin(tmp_path):
    status, update_calls, run_commands, run_kwargs_list = _run_process_job_with_submission_attempts(
        tmp_path,
        [
            {
                "returncode": 0,
                "submission_result": {
                    "status": "pending_user_input",
                    "board": "lever",
                    "message": "Submission paused because an answer requires manual review.",
                },
            }
        ],
        board="lever",
        board_url="https://jobs.lever.co/example/123",
        auto_submit=True,
    )

    assert status == "stopped"
    assert update_calls[-1][0] == "stopped"
    assert len(run_commands) == 1
    assert run_kwargs_list[0]["stdin"] is subprocess.DEVNULL


def test_process_job_auto_fix_retry_submit_subprocess_uses_devnull_stdin(tmp_path):
    import pipeline_orchestrator

    conn = sqlite3.connect(":memory:")
    output_dir = tmp_path / "job-output"
    output_dir.mkdir()
    submit_dir = output_dir / "submit"
    submit_dir.mkdir()

    job = {
        "id": 1,
        "url": "https://boards.example/jobs/123",
        "board_url": "https://boards.example/jobs/123",
        "board": "greenhouse",
        "status": "submitting",
        "output_dir": str(output_dir),
        "source": "direct",
        "company": "Example Co",
        "role_title": "principal-pm",
        "failure_type": None,
    }
    run_kwargs_list: list[dict] = []
    run_results = iter(
        [
            {"returncode": 1, "stderr": "submit failed"},
            {"returncode": 1, "stderr": "retry still failed"},
        ]
    )

    class _DummyThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def join(self, timeout=None):
            pass

    class _FakeProcess:
        def __init__(self, result_spec: dict, kwargs: dict) -> None:
            self.pid = 5252 + len(run_kwargs_list)
            self.returncode = result_spec["returncode"]
            self._stdout = result_spec.get("stdout", "")
            self._stderr = result_spec.get("stderr", "")
            run_kwargs_list.append(dict(kwargs))

        def communicate(self, timeout=None):
            del timeout
            return self._stdout, self._stderr

    def _fake_popen(cmd, **kwargs):
        result_spec = next(run_results)
        return _FakeProcess(result_spec, kwargs)

    with (
        patch.dict("os.environ", {"ASSET_LLM_PROVIDER": "openai"}, clear=False),
        patch("job_db.get_job", return_value=job),
        patch("job_db.ensure_job_metrics"),
        patch("job_db.start_phase", return_value=1),
        patch("job_db.end_phase"),
        patch("job_db.log_event"),
        patch("job_db.update_job_metrics"),
        patch("job_db.update_progress"),
        patch("job_db.get_job_metrics", return_value=None),
        patch("job_db.get_recent_auth_failures", return_value=0),
        patch("job_db.update_status"),
        patch.object(pipeline_orchestrator, "_detect_and_log_content_edits"),
        patch.object(pipeline_orchestrator, "_finalize_pending_answer_refresh", return_value=None),
        patch.object(pipeline_orchestrator.threading, "Thread", _DummyThread),
        patch.object(pipeline_orchestrator.subprocess, "Popen", side_effect=_fake_popen),
        patch.object(
            pipeline_orchestrator,
            "provider_available",
            side_effect=lambda name: name == "openai",
        ),
        patch.object(pipeline_orchestrator, "auto_fix", return_value=True),
        patch.object(pipeline_orchestrator, "_find_payload_path", return_value=None),
    ):
        status = pipeline_orchestrator.process_job(conn, 1, auto_submit=False)

    assert status == "stopped"
    assert len(run_kwargs_list) == 2
    assert run_kwargs_list[0]["stdin"] is subprocess.DEVNULL
    assert run_kwargs_list[1]["stdin"] is subprocess.DEVNULL


def test_retry_with_recording_uses_devnull_stdin(tmp_path):
    import pipeline_orchestrator

    payload_path = tmp_path / "submit" / "autofill_payload.json"
    payload_path.parent.mkdir(parents=True)
    payload_path.write_text("{}", encoding="utf-8")

    seen_kwargs = {}

    class _FakeProcess:
        pid = 6060
        returncode = 0

        def communicate(self, timeout=None):
            del timeout
            return "", ""

    def _fake_popen(cmd, **kwargs):
        del cmd
        seen_kwargs.update(kwargs)
        return _FakeProcess()

    with patch.object(pipeline_orchestrator.subprocess, "Popen", side_effect=_fake_popen):
        rc = pipeline_orchestrator.retry_with_recording(payload_path, "greenhouse")

    assert rc == 0
    assert seen_kwargs["stdin"] is subprocess.DEVNULL


def test_process_job_retry_with_recording_uses_headed_mode_after_captcha_escalation(tmp_path):
    import pipeline_orchestrator

    conn = sqlite3.connect(":memory:")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)

    job = {
        "id": 1,
        "url": "https://jobs.avature.net/careers/JobDetail/123",
        "board_url": "https://jobs.avature.net/careers/JobDetail/123",
        "board": "avature",
        "status": "submitting",
        "output_dir": str(out_dir),
        "source": "direct",
        "company": "Example Co",
        "role_title": "principal-pm",
        "failure_type": None,
    }
    submit_attempts = iter(
        [
            {
                "returncode": 0,
                "submission_result": {
                    "status": "skipped_captcha",
                    "board": "avature",
                    "message": "Submission skipped: captcha required. Moving on to next job.",
                },
            },
            {
                "returncode": 1,
                "stderr": "submission failed",
            },
        ]
    )

    class _DummyThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def join(self, timeout=None):
            pass

    def _fake_run(cmd, **kwargs):
        attempt = next(submit_attempts)
        submission_result = attempt.get("submission_result")
        if submission_result is not None:
            (submit_dir / "application_submission_result.json").write_text(
                json.dumps(submission_result),
                encoding="utf-8",
            )
        completed = MagicMock()
        completed.returncode = attempt.get("returncode", 0)
        completed.stdout = attempt.get("stdout", "")
        completed.stderr = attempt.get("stderr", "")
        return completed

    with (
        patch("job_db.get_job", return_value=job),
        patch("job_db.ensure_job_metrics"),
        patch("job_db.start_phase", side_effect=[1, 2]),
        patch("job_db.end_phase"),
        patch("job_db.log_event"),
        patch("job_db.update_job_metrics"),
        patch("job_db.update_progress"),
        patch("job_db.get_job_metrics", return_value=None),
        patch("job_db.get_recent_auth_failures", return_value=0),
        patch("job_db.update_status"),
        patch.object(pipeline_orchestrator, "_detect_and_log_content_edits"),
        patch.object(pipeline_orchestrator, "_finalize_pending_answer_refresh", return_value=None),
        patch.object(pipeline_orchestrator, "provider_available", return_value=False),
        patch.object(pipeline_orchestrator, "_find_payload_path", return_value=submit_dir / "autofill_payload.json"),
        patch.object(pipeline_orchestrator, "retry_with_recording", return_value=1) as retry_with_recording,
        patch.object(pipeline_orchestrator.threading, "Thread", _DummyThread),
        patch.object(pipeline_orchestrator, "_run_worker_subprocess", side_effect=_fake_run),
        patch("draft_manager.generate_draft_summary"),
        patch.object(pipeline_orchestrator, "_sync_draft_proof_blockers"),
        patch.object(pipeline_orchestrator, "_validate_draft_completeness", return_value=[]),
    ):
        status = pipeline_orchestrator.process_job(conn, 1, auto_submit=False)

    assert status == "stopped"
    retry_with_recording.assert_called_once()
    assert retry_with_recording.call_args.kwargs["headless"] is False


def test_escalated_timeout_only_increases_after_timeout_failure():
    assert _escalated_timeout(900, "timeout") == 1350
    assert _escalated_timeout(900, "generation_failed") == 900
    assert _escalated_timeout(900, None) == 900


def _run_phases_with_failure_type(failure_type: str | None) -> int:
    import pipeline_orchestrator

    job = {
        "board_url": "https://boards.example/jobs/123",
        "failure_type": failure_type,
    }

    with (
        patch("job_db.start_phase", side_effect=[1, 2]),
        patch("job_db.update_status"),
        patch("job_db.end_phase"),
        patch("job_db.find_duplicate_job_match", return_value=None),
        patch("job_db.log_event"),
        patch("job_db.update_job_metrics"),
        patch("job_db.update_progress"),
        patch("url_resolver.detect_source", return_value="direct"),
        patch.object(pipeline_orchestrator, "provider_fallback", return_value=(None, 1)) as fallback,
    ):
        fallback.last_error_hint = ""
        _run_phases_1_2(object(), 123, job, "https://boards.example/jobs/123")
    return fallback.call_args.kwargs["timeout"]


def test_run_phases_1_2_uses_escalated_timeout_after_timeout_failure():
    import pipeline_orchestrator

    timeout = _run_phases_with_failure_type("timeout")

    assert timeout == _escalated_timeout(pipeline_orchestrator.DEFAULT_ASSET_TIMEOUT, "timeout")


def test_run_phases_1_2_keeps_default_timeout_for_non_timeout_failure():
    import pipeline_orchestrator

    timeout = _run_phases_with_failure_type("generation_failed")

    assert timeout == pipeline_orchestrator.DEFAULT_ASSET_TIMEOUT


def test_run_phases_1_2_archives_job_closed_asset_generation_failure():
    import pipeline_orchestrator

    job = {
        "board_url": "https://boards.example/jobs/123",
        "failure_type": None,
    }

    with (
        patch("job_db.start_phase", side_effect=[1, 2]),
        patch("job_db.update_status"),
        patch("job_db.end_phase"),
        patch("job_db.find_duplicate_job_match", return_value=None),
        patch("job_db.log_event"),
        patch("job_db.update_job_metrics"),
        patch("job_db.update_progress"),
        patch("url_resolver.detect_source", return_value="direct"),
        patch.object(pipeline_orchestrator, "_mark_job_unavailable_and_archive") as mark_closed,
        patch.object(pipeline_orchestrator, "provider_fallback", return_value=(None, 1)) as fallback,
    ):
        fallback.last_error_hint = "job_closed: URL returned HTTP 404 at https://boards.example/jobs/123"
        fallback.last_failure_type = "generation_failed"
        resolved_url, status, source = _run_phases_1_2(object(), 123, job, "https://boards.example/jobs/123")

    assert resolved_url is None
    assert status == "stopped"
    assert source == "direct"
    mark_closed.assert_called_once()


def test_run_phases_1_2_marks_unsupported_asset_generation_failure():
    import pipeline_orchestrator

    conn = object()
    job = {
        "board_url": "https://kiteworks.careers.hibob.com/jobs/example/apply",
        "failure_type": None,
    }

    with (
        patch("job_db.start_phase", side_effect=[1, 2]),
        patch("job_db.update_status") as update_status,
        patch("job_db.end_phase"),
        patch("job_db.find_duplicate_job_match", return_value=None),
        patch("job_db.log_event"),
        patch("job_db.update_job_metrics"),
        patch("job_db.update_progress"),
        patch("url_resolver.detect_source", return_value="direct"),
        patch.object(pipeline_orchestrator, "provider_fallback", return_value=(None, 1)) as fallback,
    ):
        fallback.last_error_hint = (
            "unsupported: HiBob-hosted careers pages require dedicated board support and "
            "did not expose a static job description."
        )
        fallback.last_failure_type = "generation_failed"
        resolved_url, status, source = _run_phases_1_2(
            conn,
            123,
            job,
            "https://kiteworks.careers.hibob.com/jobs/example/apply",
        )

    assert resolved_url is None
    assert status == "stopped"
    assert source == "direct"
    update_status.assert_any_call(
        conn,
        123,
        "stopped",
        error_message=(
            "Asset generation failed: unsupported: HiBob-hosted careers pages require dedicated board support and "
            "did not expose a static job description."
        ),
        failure_type="unsupported",
    )


def test_run_phases_1_2_marks_skipped_captcha_asset_generation_failure():
    import pipeline_orchestrator

    conn = object()
    job = {
        "board_url": "https://www.tesla.com/careers/search/job/251870",
        "failure_type": None,
    }

    with (
        patch("job_db.start_phase", side_effect=[1, 2]),
        patch("job_db.update_status") as update_status,
        patch("job_db.end_phase"),
        patch("job_db.find_duplicate_job_match", return_value=None),
        patch("job_db.log_event"),
        patch("job_db.update_job_metrics"),
        patch("job_db.update_progress"),
        patch("url_resolver.detect_source", return_value="direct"),
        patch.object(pipeline_orchestrator, "provider_fallback", return_value=(None, 1)) as fallback,
    ):
        fallback.last_error_hint = (
            "skipped_captcha: The job board blocked access to the job description behind an anti-bot challenge "
            "at https://www.tesla.com/careers/search/job/251870"
        )
        fallback.last_failure_type = "generation_failed"
        resolved_url, status, source = _run_phases_1_2(
            conn,
            123,
            job,
            "https://www.tesla.com/careers/search/job/251870",
        )

    assert resolved_url is None
    assert status == "stopped"
    assert source == "direct"
    update_status.assert_any_call(
        conn,
        123,
        "stopped",
        error_message=(
            "Asset generation failed: skipped_captcha: The job board blocked access to the job description behind "
            "an anti-bot challenge at https://www.tesla.com/careers/search/job/251870"
        ),
        failure_type="skipped_captcha",
    )


def test_run_phases_1_2_marks_board_rate_limited_asset_generation_failure():
    import pipeline_orchestrator

    conn = object()
    job = {
        "board_url": "https://www.linkedin.com/jobs/view/4376974931/",
        "failure_type": None,
    }

    with (
        patch("job_db.start_phase", side_effect=[1, 2]),
        patch("job_db.update_status") as update_status,
        patch("job_db.end_phase"),
        patch("job_db.find_duplicate_job_match", return_value=None),
        patch("job_db.log_event"),
        patch("job_db.update_job_metrics"),
        patch("job_db.update_progress"),
        patch("url_resolver.detect_source", return_value="linkedin"),
        patch.object(pipeline_orchestrator, "provider_fallback", return_value=(None, 1)) as fallback,
    ):
        fallback.last_error_hint = (
            "Attempt: Scrape JD from URL\n"
            "- 429 Too Many Requests: retry later\n"
            "Please retry later or provide JD text directly."
        )
        fallback.last_failure_type = "generation_failed"
        resolved_url, status, source = _run_phases_1_2(
            conn,
            123,
            job,
            "https://www.linkedin.com/jobs/view/4376974931/",
        )

    assert resolved_url is None
    assert status == "stopped"
    assert source == "linkedin"
    update_status.assert_any_call(
        conn,
        123,
        "stopped",
        error_message=(
            "Asset generation failed: Attempt: Scrape JD from URL\n"
            "- 429 Too Many Requests: retry later\n"
            "Please retry later or provide JD text directly."
        ),
        failure_type="board_rate_limited",
    )


def test_run_phases_1_2_keeps_weak_extraction_failure_as_generation_failed():
    import pipeline_orchestrator

    conn = object()
    job = {
        "board_url": "https://www.linkedin.com/jobs/view/4382138139/",
        "failure_type": None,
    }

    with (
        patch("job_db.start_phase", side_effect=[1, 2]),
        patch("job_db.update_status") as update_status,
        patch("job_db.end_phase"),
        patch("job_db.find_duplicate_job_match", return_value=None),
        patch("job_db.log_event"),
        patch("job_db.update_job_metrics"),
        patch("job_db.update_progress"),
        patch("url_resolver.detect_source", return_value="linkedin"),
        patch.object(pipeline_orchestrator, "provider_fallback", return_value=(None, 1)) as fallback,
    ):
        fallback.last_error_hint = (
            "- Validate Greenhouse API JD did not produce a JD payload\n"
            "Attempt: Scrape JD from URL\n"
            "- parsed JD did not contain enough structured detail to trust the extraction\n"
            "Please retry later or provide JD text directly."
        )
        fallback.last_failure_type = "generation_failed"
        resolved_url, status, source = _run_phases_1_2(
            conn,
            123,
            job,
            "https://www.linkedin.com/jobs/view/4382138139/",
        )

    assert resolved_url is None
    assert status == "stopped"
    assert source == "linkedin"
    update_status.assert_any_call(
        conn,
        123,
        "stopped",
        error_message=(
            "Asset generation failed: - Validate Greenhouse API JD did not produce a JD payload\n"
            "Attempt: Scrape JD from URL\n"
            "- parsed JD did not contain enough structured detail to trust the extraction\n"
            "Please retry later or provide JD text directly."
        ),
        failure_type="generation_failed",
    )


def test_run_phases_1_2_archives_resolved_url_duplicate_before_generation(tmp_path):
    import pipeline_orchestrator
    from job_db import add_job, get_job, init_db

    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    board_url = "https://boards.greenhouse.io/acme/jobs/123"
    existing_id = add_job(
        conn,
        board_url,
        company="Acme",
        role_title="Senior Product Manager",
    )
    duplicate_url = "https://www.linkedin.com/jobs/view/1234567890/"
    duplicate_id = add_job(conn, duplicate_url)
    duplicate_job = get_job(conn, duplicate_id)

    with (
        patch("url_resolver.detect_source", return_value="linkedin"),
        patch("url_resolver.resolve_to_board_url", return_value=board_url),
        patch.object(pipeline_orchestrator, "provider_fallback") as fallback,
    ):
        resolved_url, status, source = _run_phases_1_2(conn, duplicate_id, duplicate_job, duplicate_url)

    duplicate_row = get_job(conn, duplicate_id)

    assert resolved_url is None
    assert status == "stopped"
    assert source == "linkedin"
    assert duplicate_row["status"] == "stopped"
    assert bool(duplicate_row["archived"]) is True
    assert duplicate_row["failure_type"] == "duplicate"
    assert duplicate_row["board_url"] == board_url
    assert f"job #{existing_id}" in duplicate_row["error_message"]
    assert conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE archived IS NULL OR archived = FALSE"
    ).fetchone()[0] == 1
    fallback.assert_not_called()


def test_run_phases_1_2_archives_cross_source_duplicate_after_metadata_resolution(tmp_path):
    import pipeline_orchestrator
    from job_db import add_job, get_job, init_db

    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    existing_id = add_job(
        conn,
        "https://jobs.ashbyhq.com/valon/0f1fbd7d-e30d-4b8c-9e3d-04ff6bfd7638",
        company="Valon Tech",
        role_title="senior-pm-product-infrastructure",
    )
    duplicate_url = "https://www.linkedin.com/jobs/view/4366508877/"
    duplicate_id = add_job(conn, duplicate_url)
    duplicate_job = get_job(conn, duplicate_id)
    output_dir = tmp_path / "output" / "valon"
    output_dir.mkdir(parents=True)
    meta = {
        "company": "Valon",
        "company_proper": "Valon",
        "role": "Valon hiring Senior Product Manager, Product Infrastructure in San Francisco, CA | LinkedIn",
        "board": "ashby",
    }

    with (
        patch("url_resolver.detect_source", return_value="linkedin"),
        patch(
            "url_resolver.resolve_to_board_url",
            return_value="https://www.valon.com/careers/senior-product-manager-product-infrastructure",
        ),
        patch.object(pipeline_orchestrator, "provider_fallback", return_value=("openai", 0)) as fallback,
        patch.object(pipeline_orchestrator, "_discover_output_dir", return_value=output_dir),
        patch.object(pipeline_orchestrator, "_load_pipeline_meta", return_value=meta),
        patch.object(pipeline_orchestrator, "_enrich_pipeline_meta", return_value=None),
    ):
        resolved_url, status, source = _run_phases_1_2(conn, duplicate_id, duplicate_job, duplicate_url)

    duplicate_row = get_job(conn, duplicate_id)

    assert resolved_url is None
    assert status == "stopped"
    assert source == "linkedin"
    assert duplicate_row["status"] == "stopped"
    assert bool(duplicate_row["archived"]) is True
    assert duplicate_row["failure_type"] == "duplicate"
    assert duplicate_row["company"] == "Valon"
    assert duplicate_row["role_title"] == meta["role"]
    assert duplicate_row["board"] == "ashby"
    assert str(duplicate_row["output_dir"]) == str(output_dir)
    assert f"job #{existing_id}" in duplicate_row["error_message"]
    assert conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE archived IS NULL OR archived = FALSE"
    ).fetchone()[0] == 1
    assert fallback.call_count == 1


def test_run_phases_1_2_keeps_older_job_when_only_newer_metadata_duplicate_exists(tmp_path):
    import pipeline_orchestrator
    from job_db import add_job, get_job, init_db, update_status

    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    older_url = "https://boards.greenhouse.io/acme/jobs/123"
    older_id = add_job(conn, older_url)
    older_job = get_job(conn, older_id)

    newer_id = add_job(conn, "https://www.linkedin.com/jobs/view/9999999999/")
    update_status(
        conn,
        newer_id,
        "queued",
        company="Acme",
        role_title="Senior Product Manager",
    )

    output_dir = tmp_path / "output" / "acme" / "senior-product-manager"
    content_dir = output_dir / "content"
    content_dir.mkdir(parents=True)
    (content_dir / "jd_raw.md").write_text("Build product strategy for Acme.", encoding="utf-8")
    meta = {
        "company": "Acme",
        "company_proper": "Acme",
        "role": "Senior Product Manager",
        "board": "greenhouse",
    }

    with (
        patch.object(pipeline_orchestrator, "provider_fallback", return_value=("openai", 0)) as fallback,
        patch.object(pipeline_orchestrator, "_discover_output_dir", return_value=output_dir),
        patch.object(pipeline_orchestrator, "_load_pipeline_meta", return_value=meta),
        patch.object(pipeline_orchestrator, "_enrich_pipeline_meta", return_value=None),
    ):
        resolved_url, output_dir_value, source = _run_phases_1_2(conn, older_id, older_job, older_url)

    refreshed_older = get_job(conn, older_id)
    newer = get_job(conn, newer_id)

    assert resolved_url == older_url
    assert output_dir_value == str(output_dir)
    assert source == "direct"
    assert refreshed_older["status"] == "generating"
    assert bool(refreshed_older["archived"]) is False
    assert refreshed_older["failure_type"] is None
    assert newer["status"] == "queued"
    assert bool(newer["archived"]) is False
    assert newer["failure_type"] is None
    fallback.assert_called_once()


def test_provider_fallback_subprocess_exception_treated_as_failure():
    """If subprocess.run raises an exception, treat as failure and try next provider."""
    call_count = 0

    def mock_run(cmd, **kw):
        nonlocal call_count
        call_count += 1
        provider = cmd[cmd.index("--provider") + 1] if "--provider" in cmd else None
        if provider == "gemini":
            raise subprocess.TimeoutExpired(cmd, 300)
        r = MagicMock()
        r.returncode = 0
        return r

    with patch("subprocess.run", side_effect=mock_run):
        provider, rc = provider_fallback(
            base_cmd=["uv", "run", "python", "test.py"],
            providers=["gemini", "claude"],
        )
    assert provider == "claude"
    assert rc == 0
    assert call_count == 2


def test_provider_fallback_all_raise_exceptions():
    """If all providers raise exceptions, return None with exit code 1."""

    def mock_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 300)

    with patch("subprocess.run", side_effect=mock_run):
        provider, rc = provider_fallback(
            base_cmd=["uv", "run", "python", "test.py"],
            providers=["gemini", "claude"],
        )
    assert provider is None
    assert rc == 1


def test_provider_fallback_preserves_base_cmd_order():
    """Base cmd args should come before --provider flag."""
    with patch("subprocess.run", side_effect=_mock_run_factory({"gemini": 0})) as mock_run:
        provider_fallback(
            base_cmd=["uv", "run", "python", "scripts/run_pipeline.py", "/output/dir", "--build"],
            providers=["gemini"],
        )
    called_cmd = mock_run.call_args_list[0][0][0]
    provider_idx = called_cmd.index("--provider")
    build_idx = called_cmd.index("--build")
    # --build from base_cmd should appear before --provider
    assert build_idx < provider_idx


def test_finalize_pending_answer_refresh_marks_fresh(tmp_path):
    from answer_refresh_state import load_answer_refresh_state, mark_answer_refresh_pending

    submit_dir = tmp_path / "submit"
    submit_dir.mkdir()
    pending = mark_answer_refresh_pending(tmp_path, request_kind="reanswer")
    (submit_dir / "ashby_autofill_report.json").write_text(
        json.dumps(
            {
                "fields": [
                    {
                        "field_name": "custom_question",
                        "label": "Why this role?",
                        "status": "filled",
                        "source": "generated_application_answer",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (submit_dir / "application_answers.json").write_text(
        json.dumps(
            {
                "provider": "claude",
                "generated_at_utc": "2026-03-26T18:30:00+00:00",
                "refresh_request_id": pending["request_id"],
            }
        ),
        encoding="utf-8",
    )

    with patch("pipeline_orchestrator.log_event") as log_event:
        final = _finalize_pending_answer_refresh(object(), 123, tmp_path)

    state = load_answer_refresh_state(tmp_path)
    assert final["status"] == "fresh"
    assert state["status"] == "fresh"
    assert state["answer_provider"] == "claude"
    assert state["generated_answer_count"] == 1
    log_event.assert_called()


def test_finalize_pending_answer_refresh_marks_not_applicable(tmp_path):
    from answer_refresh_state import load_answer_refresh_state, mark_answer_refresh_pending

    submit_dir = tmp_path / "submit"
    submit_dir.mkdir()
    mark_answer_refresh_pending(tmp_path, request_kind="reanswer")
    (submit_dir / "ashby_autofill_report.json").write_text(
        json.dumps(
            {
                "fields": [
                    {
                        "field_name": "location",
                        "label": "Location",
                        "status": "filled",
                        "source": "application_profile.md",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with patch("pipeline_orchestrator.log_event") as log_event:
        final = _finalize_pending_answer_refresh(object(), 123, tmp_path)

    state = load_answer_refresh_state(tmp_path)
    assert final["status"] == "not_applicable"
    assert state["status"] == "not_applicable"
    assert state["generated_answer_count"] == 0
    log_event.assert_called()


def test_finalize_pending_answer_refresh_ignores_stale_failed_state(tmp_path):
    from answer_refresh_state import fail_pending_answer_refresh, load_answer_refresh_state, mark_answer_refresh_pending

    mark_answer_refresh_pending(tmp_path, request_kind="reanswer")
    failed = fail_pending_answer_refresh(
        tmp_path,
        reason="missing_fresh_proof",
        message="Answer regeneration did not rewrite fresh answer artifacts for the current request.",
    )

    with patch("pipeline_orchestrator.log_event") as log_event:
        final = _finalize_pending_answer_refresh(object(), 123, tmp_path)

    state = load_answer_refresh_state(tmp_path)
    assert failed["status"] == "failed"
    assert final is None
    assert state["status"] == "failed"
    log_event.assert_not_called()


def _run_post_submit_with_email_status(email_status: str):
    import pipeline_orchestrator

    conn = MagicMock()
    output_dir = Path("/tmp/output-dir")

    with (
        patch("job_db.start_phase", return_value=1),
        patch("job_db.end_phase") as end_phase,
        patch("job_db.log_event") as log_event,
        patch("job_db.get_job", return_value={}),
        patch.object(pipeline_orchestrator, "_detect_board_from_url", return_value="greenhouse"),
        patch("application_submit_common.sync_notion_after_submit", return_value={}),
        patch(
            "application_submit_common.send_confirmation_email_reply",
            return_value={
                "status": email_status,
                "reason": "reply_already_sent" if email_status == "skipped_duplicate" else None,
                "submit_dir": str(output_dir / "submit"),
                "state_path": str(output_dir / "submit" / "confirmation_email_reply.json"),
            },
        ) as send_reply,
    ):
        pipeline_orchestrator._post_submit(conn, 123, output_dir, "https://boards.example/jobs/123")

    return log_event.call_args_list, end_phase, send_reply


def test_post_submit_logs_email_reply_sent_when_reply_is_sent():
    log_events, end_phase, send_reply = _run_post_submit_with_email_status("sent")

    send_reply.assert_called_once()
    event_names = [call.args[2] for call in log_events]
    assert "notion_synced" in event_names
    assert "email_reply_sent" in event_names
    end_phase.assert_called_once_with(mock.ANY, 1, exit_code=0)


def test_post_submit_logs_duplicate_skip_when_reply_already_sent():
    log_events, end_phase, send_reply = _run_post_submit_with_email_status("skipped_duplicate")

    send_reply.assert_called_once()
    duplicate_calls = [call for call in log_events if call.args[2] == "email_reply_skipped_duplicate"]
    assert len(duplicate_calls) == 1
    assert duplicate_calls[0].kwargs["detail_json"]["reason"] == "reply_already_sent"
    end_phase.assert_called_once_with(mock.ANY, 1, exit_code=0)


def test_post_submit_keeps_best_effort_behavior_when_reply_not_sent():
    log_events, end_phase, send_reply = _run_post_submit_with_email_status("not_sent")

    send_reply.assert_called_once()
    event_names = [call.args[2] for call in log_events]
    assert "notion_synced" in event_names
    assert "email_reply_sent" not in event_names
    assert "email_reply_skipped_duplicate" not in event_names
    assert "email_reply_failed" not in event_names
    end_phase.assert_called_once_with(mock.ANY, 1, exit_code=0)


def test_post_submit_hides_linkedin_job_after_marking_applied():
    import pipeline_orchestrator

    conn = MagicMock()
    output_dir = Path("/tmp/output-dir")
    source_url = "https://www.linkedin.com/jobs/view/12345"

    with (
        patch("job_db.start_phase", return_value=1),
        patch("job_db.end_phase") as end_phase,
        patch("job_db.log_event") as log_event,
        patch("job_db.get_job", return_value={"source_url": source_url, "url": source_url}),
        patch.object(pipeline_orchestrator, "_detect_board_from_url", return_value="greenhouse"),
        patch("application_submit_common.sync_notion_after_submit", return_value={}),
        patch("application_submit_common.send_confirmation_email_reply", return_value={"status": "sent"}),
        patch("url_resolver.mark_linkedin_job_applied", return_value=True) as mark_applied,
        patch("url_resolver.dismiss_linkedin_job_recommendation", return_value=True) as dismiss_job,
    ):
        pipeline_orchestrator._post_submit(conn, 123, output_dir, "https://boards.example/jobs/123")

    mark_applied.assert_called_once_with(source_url)
    dismiss_job.assert_called_once_with(source_url)
    event_names = [call.args[2] for call in log_event.call_args_list]
    assert "linkedin_marked_applied" in event_names
    assert "linkedin_dismissed" in event_names
    end_phase.assert_called_once_with(mock.ANY, 1, exit_code=0)


def test_post_submit_skips_hide_when_linkedin_mark_fails():
    import pipeline_orchestrator

    conn = MagicMock()
    output_dir = Path("/tmp/output-dir")
    source_url = "https://www.linkedin.com/jobs/view/23456"

    with (
        patch("job_db.start_phase", return_value=1),
        patch("job_db.end_phase") as end_phase,
        patch("job_db.log_event") as log_event,
        patch("job_db.get_job", return_value={"source_url": source_url, "url": source_url}),
        patch.object(pipeline_orchestrator, "_detect_board_from_url", return_value="greenhouse"),
        patch("application_submit_common.sync_notion_after_submit", return_value={}),
        patch("application_submit_common.send_confirmation_email_reply", return_value={"status": "sent"}),
        patch("url_resolver.mark_linkedin_job_applied", return_value=False) as mark_applied,
        patch("url_resolver.dismiss_linkedin_job_recommendation") as dismiss_job,
    ):
        pipeline_orchestrator._post_submit(conn, 123, output_dir, "https://boards.example/jobs/123")

    mark_applied.assert_called_once_with(source_url)
    dismiss_job.assert_not_called()
    event_names = [call.args[2] for call in log_event.call_args_list]
    assert "linkedin_mark_failed" in event_names
    assert "linkedin_dismissed" not in event_names
    end_phase.assert_called_once_with(mock.ANY, 1, exit_code=0)


def test_post_submit_hides_linkedin_board_job_without_extra_mark_step():
    import pipeline_orchestrator

    conn = MagicMock()
    output_dir = Path("/tmp/output-dir")
    source_url = "https://www.linkedin.com/jobs/view/34567"

    with (
        patch("job_db.start_phase", return_value=1),
        patch("job_db.end_phase") as end_phase,
        patch("job_db.log_event") as log_event,
        patch("job_db.get_job", return_value={"source_url": source_url, "url": source_url}),
        patch.object(pipeline_orchestrator, "_detect_board_from_url", return_value="linkedin"),
        patch("application_submit_common.sync_notion_after_submit", return_value={}),
        patch("application_submit_common.send_confirmation_email_reply", return_value={"status": "sent"}),
        patch("url_resolver.mark_linkedin_job_applied") as mark_applied,
        patch("url_resolver.dismiss_linkedin_job_recommendation", return_value=True) as dismiss_job,
    ):
        pipeline_orchestrator._post_submit(conn, 123, output_dir, source_url)

    mark_applied.assert_not_called()
    dismiss_job.assert_called_once_with(source_url)
    event_names = [call.args[2] for call in log_event.call_args_list]
    assert "linkedin_marked_applied" not in event_names
    assert "linkedin_dismissed" in event_names
    end_phase.assert_called_once_with(mock.ANY, 1, exit_code=0)


# ---------------------------------------------------------------------------
# _auto_retry_if_transient / _is_transient_error tests
# ---------------------------------------------------------------------------

from pipeline_orchestrator import (
    _auto_retry_if_transient,
    _extract_error_hint,
    _handle_failed_submission_result,
    _handle_linkedin_failure_result,
    _is_transient_error,
    _load_failed_submission_result,
    _load_linkedin_failure_result,
    _synthesize_linkedin_timeout_result,
)


def test_is_transient_error_matches_known_patterns():
    """Transient patterns should be detected."""
    assert _is_transient_error("Submit failed (exit 1); auto-fix slot busy")
    assert _is_transient_error("Please retry later or provide JD text")
    assert _is_transient_error("Request timed out after 600s")
    assert _is_transient_error("connection reset by peer")


def test_is_transient_error_rejects_non_transient():
    """Non-transient errors should not match."""
    assert not _is_transient_error("Workday authentication failed")
    assert not _is_transient_error("JSONDecodeError: Expecting comma")
    assert not _is_transient_error("")
    assert not _is_transient_error(None)


def _make_in_memory_db():
    """Create a minimal in-memory jobs DB for testing retry logic."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            url TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued',
            output_dir TEXT,
            confirmed_at TIMESTAMP,
            submission_lock_state TEXT NOT NULL DEFAULT 'open',
            resubmit_count INTEGER NOT NULL DEFAULT 0,
            last_resubmit_unlocked_at TIMESTAMP,
            last_resubmit_unlock_initiator TEXT,
            last_resubmit_confirmed_at TIMESTAMP,
            completed_at TIMESTAMP,
            error_message TEXT,
            failure_type TEXT,
            auth_state TEXT,
            auth_scope TEXT,
            archived BOOLEAN DEFAULT FALSE,
            fix_attempts INTEGER DEFAULT 0,
            progress TEXT,
            provider TEXT,
            retry_after TIMESTAMP DEFAULT '1970-01-01 00:00:00',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            event_type TEXT NOT NULL,
            detail TEXT,
            detail_json TEXT,
            initiator TEXT,
            process_info TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS job_metrics (
            job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
            total_fields INTEGER DEFAULT 0,
            fields_corrected INTEGER DEFAULT 0,
            field_error_rate REAL DEFAULT 0.0,
            manual_interventions INTEGER DEFAULT 0,
            auto_fix_attempts INTEGER DEFAULT 0,
            total_duration_ms INTEGER DEFAULT 0,
            phase_count INTEGER DEFAULT 0,
            retry_count INTEGER DEFAULT 0,
            audit_attempts INTEGER DEFAULT 0,
            audit_failure_count INTEGER DEFAULT 0,
            rendered_audit_failures INTEGER DEFAULT 0,
            last_repair_cluster_id INTEGER,
            last_rollout_sha TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS repair_clusters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'open',
            eligibility TEXT NOT NULL DEFAULT 'unknown',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            representative_job_ids TEXT NOT NULL DEFAULT '[]',
            latest_summary TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS repair_rollouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cluster_id INTEGER NOT NULL REFERENCES repair_clusters(id),
            commit_sha TEXT NOT NULL,
            status TEXT NOT NULL,
            baseline_metrics_json TEXT NOT NULL DEFAULT '{}',
            post_fix_metrics_json TEXT NOT NULL DEFAULT '{}',
            revert_sha TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS runtime_flags (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    return conn


def test_schedule_audit_retry_requeues_and_increments_audit_attempts(monkeypatch):
    import pipeline_orchestrator

    monkeypatch.setattr(pipeline_orchestrator.random, "uniform", lambda a, b: 0)

    conn = _make_in_memory_db()
    conn.execute("INSERT INTO jobs (id, url, status) VALUES (1, 'http://x', 'stopped')")
    conn.execute("INSERT INTO job_metrics (job_id, audit_attempts) VALUES (1, 0)")
    conn.commit()

    result = _schedule_audit_retry(conn, 1, "Draft audit found missing proof.", initiator="worker")

    assert result == "queued"
    row = conn.execute("SELECT status, error_message FROM jobs WHERE id = 1").fetchone()
    metrics = conn.execute("SELECT audit_attempts FROM job_metrics WHERE job_id = 1").fetchone()
    assert row["status"] == "queued"
    assert row["error_message"] == ""
    assert metrics["audit_attempts"] == 1


def test_schedule_audit_retry_exhausts_to_audit_failure(tmp_path):
    conn = _make_in_memory_db()
    out_dir = tmp_path / "output" / "audit-failure-job"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True, exist_ok=True)
    (submit_dir / "ashby_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    (submit_dir / "ashby_autofill_report.json").write_text(json.dumps({"fields": [], "unknown_questions": []}), encoding="utf-8")
    conn.execute("INSERT INTO jobs (id, url, status, output_dir) VALUES (1, 'http://x', 'stopped', ?)", (str(out_dir),))
    conn.execute("INSERT INTO job_metrics (job_id, audit_attempts) VALUES (1, 3)")
    conn.commit()

    result = _schedule_audit_retry(conn, 1, "Draft audit found missing proof.", initiator="worker", output_dir=out_dir)

    assert result == "stopped"
    row = conn.execute("SELECT status, failure_type, error_message FROM jobs WHERE id = 1").fetchone()
    metrics = conn.execute("SELECT audit_failure_count FROM job_metrics WHERE job_id = 1").fetchone()
    assert row["status"] == "stopped"
    assert row["failure_type"] == "audit_failure"
    assert "Draft audit found missing proof." in row["error_message"]
    assert metrics["audit_failure_count"] == 1
    assert (submit_dir / "audit_failure.md").exists()
    assert (tmp_path / "output" / "_audit" / "active_audit_failures.md").exists()


def test_handle_draft_audit_decision_requeues_repairable_draft(tmp_path):
    conn = _make_in_memory_db()
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "ashby_autofill_pre_submit.png").write_text("png", encoding="utf-8")
    (submit_dir / "ashby_autofill_report.json").write_text(
        json.dumps(
            {
                "fields": [
                    {
                        "field_name": "portfolio_link",
                        "label": "Portfolio Link",
                        "status": "planned",
                        "required": False,
                        "optional": True,
                    }
                ],
                "unknown_questions": [],
            }
        ),
        encoding="utf-8",
    )
    conn.execute("INSERT INTO jobs (id, url, status, output_dir) VALUES (1, 'http://x', 'stopped', ?)", (str(out_dir),))
    conn.execute("INSERT INTO job_metrics (job_id, audit_attempts) VALUES (1, 0)")
    conn.commit()

    result = _handle_draft_audit_decision(conn, 1, out_dir, board_name="ashby", missing_items=[])

    assert result == "queued"
    row = conn.execute("SELECT status FROM jobs WHERE id = 1").fetchone()
    assert row["status"] == "queued"


def test_handle_draft_audit_decision_increments_rendered_audit_failure_count(monkeypatch):
    import pipeline_orchestrator

    conn = _make_in_memory_db()
    conn.execute("INSERT INTO jobs (id, url, status) VALUES (1, 'http://x', 'stopped')")
    conn.execute("INSERT INTO job_metrics (job_id, rendered_audit_failures) VALUES (1, 2)")
    conn.commit()

    monkeypatch.setattr(
        pipeline_orchestrator,
        "audit_draft_outcome",
        lambda *_args, **_kwargs: AuditDecision(
            kind="repairable",
            failure_type="rendered_audit_mismatch",
            reason="Work authorization: missing selections: yes",
            repair_actions=("clear_current_attempt_artifacts", "requeue"),
        ),
    )
    monkeypatch.setattr(pipeline_orchestrator, "_schedule_audit_retry", lambda *_args, **_kwargs: "queued")

    result = _handle_draft_audit_decision(conn, 1, None, board_name="ashby", missing_items=[])

    row = conn.execute("SELECT rendered_audit_failures FROM job_metrics WHERE job_id = 1").fetchone()
    assert result == "queued"
    assert row["rendered_audit_failures"] == 3


def test_handle_draft_audit_decision_records_repair_cluster_and_metric(monkeypatch, tmp_path):
    import pipeline_orchestrator

    conn = _make_in_memory_db()
    out_dir = tmp_path / "output" / "acme" / "pm"
    out_dir.mkdir(parents=True, exist_ok=True)
    conn.execute("INSERT INTO jobs (id, url, status, output_dir) VALUES (1, 'http://x', 'stopped', ?)", (str(out_dir),))
    conn.commit()

    monkeypatch.setattr(
        pipeline_orchestrator,
        "audit_draft_outcome",
        lambda *_args, **_kwargs: AuditDecision(
            kind="repairable",
            failure_type="rendered_audit_mismatch",
            reason="Work authorization expected Yes observed No",
            repair_actions=("clear_current_attempt_artifacts", "requeue"),
            artifacts={},
        ),
    )
    monkeypatch.setattr(pipeline_orchestrator, "_schedule_audit_retry", lambda *_args, **_kwargs: "queued")

    result = _handle_draft_audit_decision(conn, 1, out_dir, board_name="greenhouse", missing_items=[])

    cluster = conn.execute("SELECT * FROM repair_clusters ORDER BY id DESC LIMIT 1").fetchone()
    metrics = conn.execute("SELECT last_repair_cluster_id FROM job_metrics WHERE job_id = 1").fetchone()
    assert result == "queued"
    assert cluster is not None
    assert cluster["status"] == "open"
    assert cluster["eligibility"] == "auto_repair_candidate"
    assert cluster["latest_summary"] == "Work authorization expected Yes observed No"
    assert metrics["last_repair_cluster_id"] == cluster["id"]


def test_canary_redraft_requeues_jobs_and_clears_retry_state():
    conn = _make_in_memory_db()
    conn.execute(
        "INSERT INTO jobs (id, url, status, error_message, failure_type, progress, provider, retry_after) "
        "VALUES (1, 'http://x', 'stopped', 'Submission failed.', 'submit_failed', 'Stopped on failure', 'openai', "
        "'2026-04-02 12:00:00')"
    )
    conn.commit()

    updated = requeue_jobs_for_repair_redraft(conn, [1], initiator="repair_supervisor")

    row = conn.execute(
        "SELECT status, error_message, failure_type, progress, provider, retry_after FROM jobs WHERE id = 1"
    ).fetchone()
    event = conn.execute(
        "SELECT event_type, detail, initiator FROM events WHERE job_id = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()

    assert updated == [1]
    assert row["status"] == "queued"
    assert row["error_message"] == ""
    assert row["failure_type"] is None
    assert row["provider"] is None
    assert row["retry_after"] == RETRY_AFTER_SENTINEL
    assert row["progress"] == "Repair supervisor requested a fresh draft rerun."
    assert event["event_type"] == "repair_redraft_queued"
    assert event["detail"] == "queued"
    assert event["initiator"] == "repair_supervisor"


def test_repair_loop_exhaustion_stops_jobs_as_audit_failure():
    import pipeline_orchestrator

    conn = _make_in_memory_db()
    out_dir = Path("/tmp/repair-exhaustion-proof")
    audit_reports: list[tuple[int, str]] = []
    conn.execute(
        "INSERT INTO jobs (id, url, status, error_message, failure_type, output_dir) VALUES "
        "(1, 'http://x/1', 'stopped', 'Auth required.', 'auth_failed', ?), "
        "(2, 'http://x/2', 'draft', '', NULL, ?)",
        (str(out_dir), str(out_dir)),
    )
    conn.execute("INSERT INTO job_metrics (job_id, audit_failure_count) VALUES (1, 0)")
    conn.execute("INSERT INTO job_metrics (job_id, audit_failure_count) VALUES (2, 1)")
    conn.commit()

    with patch.object(
        pipeline_orchestrator,
        "write_audit_failure_report",
        side_effect=lambda output_dir, job_id, summary, suggestions, attempts: audit_reports.append((job_id, summary)),
    ):
        updated = stop_jobs_for_exhausted_repair_cluster(
            conn,
            [1, 2],
            cluster_summary="Repair supervisor exhausted bounded attempts for work authorization mismatch.",
            initiator="repair_supervisor",
        )

    rows = conn.execute(
        "SELECT id, status, failure_type, error_message FROM jobs WHERE id IN (1, 2) ORDER BY id"
    ).fetchall()
    metrics = conn.execute(
        "SELECT job_id, audit_failure_count FROM job_metrics WHERE job_id IN (1, 2) ORDER BY job_id"
    ).fetchall()

    assert updated == [1, 2]
    assert [row["status"] for row in rows] == ["stopped", "stopped"]
    assert rows[0]["failure_type"] == "auth_failed"
    assert rows[0]["error_message"] == "Auth required."
    assert rows[1]["failure_type"] == "audit_failure"
    assert "exhausted bounded attempts" in rows[1]["error_message"]
    assert [row["audit_failure_count"] for row in metrics] == [0, 2]
    assert audit_reports == [
        (
            2,
            "Repair supervisor exhausted bounded attempts for work authorization mismatch.",
        )
    ]


def test_finalize_successful_submission_relocks_unlocked_job(tmp_path):
    import pipeline_orchestrator

    conn = _make_in_memory_db()
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "application_confirmation_website.json").write_text(
        json.dumps({"website_confirmed": True, "confirmed_at_utc": "2026-03-30T04:00:00+00:00"}),
        encoding="utf-8",
    )
    conn.execute(
        "INSERT INTO jobs (id, url, status, output_dir, confirmed_at, submission_lock_state, resubmit_count) "
        "VALUES (1, 'http://x', 'submitting', ?, '2026-03-18T17:11:18+00:00', 'unlocked_for_resubmit', 0)",
        (str(out_dir),),
    )
    conn.commit()

    with patch.object(pipeline_orchestrator, "_post_submit", return_value=None):
        result = pipeline_orchestrator._finalize_successful_submission(
            conn,
            1,
            out_dir,
            "https://boards.example/jobs/1",
        )

    row = conn.execute(
        "SELECT status, submission_lock_state, confirmed_at, last_resubmit_confirmed_at, resubmit_count "
        "FROM jobs WHERE id = 1"
    ).fetchone()
    assert result == "submitted"
    assert row["status"] == "submitted"
    assert row["submission_lock_state"] == "locked"
    assert row["confirmed_at"] == "2026-03-18T17:11:18+00:00"
    assert row["last_resubmit_confirmed_at"] == "2026-03-30T04:00:00+00:00"
    assert row["resubmit_count"] == 1


def test_finalize_successful_submission_counts_resubmit_without_confirmation_timestamp(tmp_path):
    import pipeline_orchestrator

    conn = _make_in_memory_db()
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (submit_dir / "notion_sync_status.json").write_text(
        json.dumps({"status": "synced"}),
        encoding="utf-8",
    )
    conn.execute(
        "INSERT INTO jobs (id, url, status, output_dir, confirmed_at, submission_lock_state, resubmit_count) "
        "VALUES (1, 'http://x', 'submitting', ?, '2026-03-18T17:11:18+00:00', 'unlocked_for_resubmit', 0)",
        (str(out_dir),),
    )
    conn.commit()

    with patch.object(pipeline_orchestrator, "_post_submit", return_value=None):
        result = pipeline_orchestrator._finalize_successful_submission(
            conn,
            1,
            out_dir,
            "https://boards.example/jobs/1",
        )

    row = conn.execute(
        "SELECT status, submission_lock_state, confirmed_at, last_resubmit_confirmed_at, resubmit_count "
        "FROM jobs WHERE id = 1"
    ).fetchone()
    assert result == "submitted"
    assert row["status"] == "submitted"
    assert row["submission_lock_state"] == "locked"
    assert row["confirmed_at"] == "2026-03-18T17:11:18+00:00"
    assert row["last_resubmit_confirmed_at"] is None
    assert row["resubmit_count"] == 1


def test_auto_retry_requeues_transient_error():
    """Transient error with retries remaining should requeue the job."""
    conn = _make_in_memory_db()
    conn.execute("INSERT INTO jobs (id, url, status, fix_attempts) VALUES (1, 'http://x', 'stopped', 0)")
    conn.commit()

    result = _auto_retry_if_transient(conn, 1, "Submit failed (exit 1); auto-fix slot busy")

    assert result == "queued"
    row = conn.execute("SELECT status, fix_attempts FROM jobs WHERE id = 1").fetchone()
    assert row["status"] == "queued"
    assert row["fix_attempts"] == 1


def test_auto_retry_refuses_locked_job_requeue():
    conn = _make_in_memory_db()
    conn.execute(
        "INSERT INTO jobs (id, url, status, fix_attempts, confirmed_at, submission_lock_state) "
        "VALUES (1, 'http://x', 'stopped', 0, '2026-03-18T17:11:18+00:00', 'locked')"
    )
    conn.commit()

    result = _auto_retry_if_transient(conn, 1, "auto-fix slot busy")

    row = conn.execute("SELECT status, submission_lock_state FROM jobs WHERE id = 1").fetchone()
    assert result == "submitted"
    assert row["status"] == "submitted"
    assert row["submission_lock_state"] == "locked"


def test_mark_job_unavailable_and_archive_sets_archived_and_logs_event(tmp_path):
    from pipeline_orchestrator import _mark_job_unavailable_and_archive

    conn = _make_in_memory_db()
    out_dir = tmp_path / "job-output"
    active_submit = out_dir / "submit-20260326T010203Z"
    active_submit.mkdir(parents=True)
    (out_dir / ".active_submit_dir").write_text("submit-20260326T010203Z\n", encoding="utf-8")
    (active_submit / "job_unavailable.json").write_text(
        json.dumps(
            {
                "status": "job_closed",
                "board": "greenhouse",
                "message": "job_closed: Job posting not found (HTTP 404)",
            }
        ),
        encoding="utf-8",
    )
    conn.execute("INSERT INTO jobs (id, url, status, output_dir) VALUES (1, 'http://x', 'stopped', ?)", (str(out_dir),))
    conn.commit()

    _mark_job_unavailable_and_archive(conn, 1, output_dir=out_dir, error_message="job_closed")

    row = conn.execute("SELECT status, failure_type, archived, error_message FROM jobs WHERE id = 1").fetchone()
    event = conn.execute("SELECT event_type, detail FROM events WHERE job_id = 1 ORDER BY id DESC LIMIT 1").fetchone()

    assert row["status"] == "stopped"
    assert row["failure_type"] == "job_closed"
    assert row["archived"] == 1
    assert "HTTP 404" in row["error_message"]
    assert event["event_type"] == "job_unavailable_auto_archived"


def test_mark_job_unavailable_and_archive_hides_linkedin_source_job(tmp_path):
    from pipeline_orchestrator import _mark_job_unavailable_and_archive

    conn = _make_in_memory_db()
    out_dir = tmp_path / "job-output"
    active_submit = out_dir / "submit"
    active_submit.mkdir(parents=True)
    (active_submit / "job_unavailable.json").write_text(
        json.dumps(
            {
                "status": "job_closed",
                "board": "greenhouse",
                "message": "job_closed: Job posting not found (HTTP 404)",
            }
        ),
        encoding="utf-8",
    )
    source_url = "https://www.linkedin.com/jobs/view/12345/"
    conn.execute(
        "INSERT INTO jobs (id, url, status, output_dir) VALUES (1, ?, 'stopped', ?)",
        (source_url, str(out_dir)),
    )
    conn.commit()

    with patch("url_resolver.dismiss_linkedin_job_recommendation", return_value=True) as dismiss_job:
        _mark_job_unavailable_and_archive(conn, 1, output_dir=out_dir, error_message="job_closed")

    dismiss_job.assert_called_once_with(source_url)
    event_names = [
        row["event_type"]
        for row in conn.execute("SELECT event_type FROM events WHERE job_id = 1 ORDER BY id").fetchall()
    ]
    assert "linkedin_dismissed" in event_names
    assert "job_unavailable_auto_archived" in event_names


def test_auto_retry_auth_failure_stays_stopped_even_with_retry_hint():
    """Known permanent failure types should never be retried."""
    conn = _make_in_memory_db()
    conn.execute(
        "INSERT INTO jobs (id, url, status, fix_attempts, failure_type) "
        "VALUES (1, 'http://x', 'stopped', 0, 'auth_failed')"
    )
    conn.commit()

    result = _auto_retry_if_transient(
        conn,
        1,
        "Authentication failed, please retry later",
        failure_type="auth_failed",
    )

    assert result == "stopped"
    row = conn.execute("SELECT status, fix_attempts FROM jobs WHERE id = 1").fetchone()
    assert row["status"] == "stopped"
    assert row["fix_attempts"] == 0


def test_auto_retry_service_unavailable_requeues():
    """Workday maintenance should use the transient retry path."""
    conn = _make_in_memory_db()
    conn.execute("INSERT INTO jobs (id, url, status, fix_attempts) VALUES (1, 'http://x', 'stopped', 0)")
    conn.commit()

    result = _auto_retry_if_transient(
        conn,
        1,
        "Workday is currently unavailable. The queue should auto-retry with backoff.",
        failure_type="service_unavailable",
    )

    assert result == "queued"
    row = conn.execute("SELECT status, fix_attempts FROM jobs WHERE id = 1").fetchone()
    assert row["status"] == "queued"
    assert row["fix_attempts"] == 1


def test_auto_retry_exhausted_stops_job():
    """When retries are exhausted, job should be stopped with retries_exhausted."""
    conn = _make_in_memory_db()
    conn.execute("INSERT INTO jobs (id, url, status, fix_attempts) VALUES (1, 'http://x', 'stopped', 3)")
    conn.commit()

    result = _auto_retry_if_transient(conn, 1, "auto-fix slot busy")

    assert result == "stopped"
    row = conn.execute("SELECT status, failure_type FROM jobs WHERE id = 1").fetchone()
    assert row["status"] == "stopped"
    assert row["failure_type"] == "retries_exhausted"


def test_auto_retry_unknown_error_uses_cautious_retry_budget():
    """Unknown errors should get a smaller retry budget than known transient ones."""
    conn = _make_in_memory_db()
    conn.execute("INSERT INTO jobs (id, url, status, fix_attempts) VALUES (1, 'http://x', 'stopped', 0)")
    conn.commit()

    assert _auto_retry_if_transient(conn, 1, "Unexpected provider hiccup") == "queued"
    conn.execute("UPDATE jobs SET status = 'stopped' WHERE id = 1")
    conn.commit()

    assert _auto_retry_if_transient(conn, 1, "Unexpected provider hiccup") == "queued"
    conn.execute("UPDATE jobs SET status = 'stopped' WHERE id = 1")
    conn.commit()

    assert _auto_retry_if_transient(conn, 1, "Unexpected provider hiccup") == "stopped"
    row = conn.execute("SELECT status, fix_attempts, failure_type FROM jobs WHERE id = 1").fetchone()
    assert row["status"] == "stopped"
    assert row["fix_attempts"] == 2
    assert row["failure_type"] == "retries_exhausted"


def test_auto_retry_llm_rate_limit_requeues_without_exhausting_fix_budget(monkeypatch):
    import pipeline_orchestrator

    monkeypatch.setattr(pipeline_orchestrator.random, "uniform", lambda a, b: 0)

    conn = _make_in_memory_db()
    conn.execute("INSERT INTO jobs (id, url, status, fix_attempts) VALUES (1, 'http://x', 'stopped', 0)")
    conn.commit()

    for expected_delay in (120, 480, 1800, 1800):
        result = _auto_retry_if_transient(
            conn,
            1,
            "OpenAI and Gemini both hit provider rate limits.",
            failure_type="llm_rate_limited",
        )

        assert result == "queued"
        row = conn.execute(
            "SELECT status, fix_attempts, "
            "CAST(strftime('%s', retry_after) AS INTEGER) - CAST(strftime('%s', 'now') AS INTEGER) AS delta "
            "FROM jobs WHERE id = 1"
        ).fetchone()
        assert row["status"] == "queued"
        assert row["fix_attempts"] == 0
        assert max(expected_delay - 2, 1) <= row["delta"] <= expected_delay

        conn.execute("UPDATE jobs SET status = 'stopped' WHERE id = 1")
        conn.commit()

    events = conn.execute(
        "SELECT COUNT(*) AS count FROM events WHERE job_id = 1 AND event_type = 'llm_rate_limit_retry'"
    ).fetchone()
    assert events["count"] == 4


def test_auto_retry_board_rate_limit_requeues_without_exhausting_fix_budget(monkeypatch):
    import pipeline_orchestrator

    monkeypatch.setattr(pipeline_orchestrator.random, "uniform", lambda a, b: 0)

    conn = _make_in_memory_db()
    conn.execute("INSERT INTO jobs (id, url, status, fix_attempts) VALUES (1, 'http://x', 'stopped', 0)")
    conn.commit()

    for expected_delay in (120, 480, 1800, 1800):
        result = _auto_retry_if_transient(
            conn,
            1,
            "429 Too Many Requests: retry later",
            failure_type="board_rate_limited",
        )

        assert result == "queued"
        row = conn.execute(
            "SELECT status, fix_attempts, "
            "CAST(strftime('%s', retry_after) AS INTEGER) - CAST(strftime('%s', 'now') AS INTEGER) AS delta "
            "FROM jobs WHERE id = 1"
        ).fetchone()
        assert row["status"] == "queued"
        assert row["fix_attempts"] == 0
        assert max(expected_delay - 2, 1) <= row["delta"] <= expected_delay

        conn.execute("UPDATE jobs SET status = 'stopped' WHERE id = 1")
        conn.commit()

    events = conn.execute(
        "SELECT COUNT(*) AS count FROM events WHERE job_id = 1 AND event_type = 'board_rate_limit_retry'"
    ).fetchone()
    assert events["count"] == 4


def test_auto_retry_sets_retry_after_backoff_steps(monkeypatch):
    """Transient retries should use the fixed retry-after delay table."""
    import pipeline_orchestrator

    monkeypatch.setattr(pipeline_orchestrator.random, "uniform", lambda a, b: 0)

    conn = _make_in_memory_db()
    conn.execute("INSERT INTO jobs (id, url, status, fix_attempts) VALUES (1, 'http://x', 'stopped', 0)")
    conn.commit()

    assert _auto_retry_if_transient(conn, 1, "auto-fix slot busy") == "queued"
    row = conn.execute(
        "SELECT CAST(strftime('%s', retry_after) AS INTEGER) - CAST(strftime('%s', 'now') AS INTEGER) AS delta "
        "FROM jobs WHERE id = 1"
    ).fetchone()
    assert 118 <= row["delta"] <= 120

    conn.execute("UPDATE jobs SET status = 'stopped' WHERE id = 1")
    conn.commit()
    assert _auto_retry_if_transient(conn, 1, "auto-fix slot busy") == "queued"
    row = conn.execute(
        "SELECT CAST(strftime('%s', retry_after) AS INTEGER) - CAST(strftime('%s', 'now') AS INTEGER) AS delta "
        "FROM jobs WHERE id = 1"
    ).fetchone()
    assert 478 <= row["delta"] <= 480

    conn.execute("UPDATE jobs SET status = 'stopped' WHERE id = 1")
    conn.commit()
    assert _auto_retry_if_transient(conn, 1, "auto-fix slot busy") == "queued"
    row = conn.execute(
        "SELECT CAST(strftime('%s', retry_after) AS INTEGER) - CAST(strftime('%s', 'now') AS INTEGER) AS delta "
        "FROM jobs WHERE id = 1"
    ).fetchone()
    assert 1798 <= row["delta"] <= 1800


def test_auto_retry_non_transient_failure_type_stays_stopped():
    """Known permanent failure types should not be retried."""
    conn = _make_in_memory_db()
    conn.execute(
        "INSERT INTO jobs (id, url, status, fix_attempts, failure_type) "
        "VALUES (1, 'http://x', 'stopped', 0, 'auth_failed')"
    )
    conn.commit()

    result = _auto_retry_if_transient(conn, 1, "Workday authentication failed", failure_type="auth_failed")

    assert result == "stopped"
    row = conn.execute("SELECT status, fix_attempts FROM jobs WHERE id = 1").fetchone()
    assert row["status"] == "stopped"
    assert row["fix_attempts"] == 0


@pytest.mark.parametrize("failure_type", ["external_apply", "linkedin_unknown_questions"])
def test_auto_retry_terminal_manual_failure_types_stay_stopped(failure_type):
    conn = _make_in_memory_db()
    conn.execute(
        "INSERT INTO jobs (id, url, status, fix_attempts, failure_type) "
        "VALUES (1, 'http://x', 'stopped', 0, ?)",
        (failure_type,),
    )
    conn.commit()

    result = _auto_retry_if_transient(conn, 1, "Manual review or external flow required", failure_type=failure_type)

    assert result == "stopped"
    row = conn.execute("SELECT status, fix_attempts FROM jobs WHERE id = 1").fetchone()
    assert row["status"] == "stopped"
    assert row["fix_attempts"] == 0


def test_load_linkedin_failure_result_reads_failed_linkedin_result(tmp_path):
    submit_dir = tmp_path / "submit"
    submit_dir.mkdir()
    (submit_dir / "application_submission_result.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "board": "linkedin",
                "failure_type": "linkedin_modal_missing",
                "message": "LinkedIn Easy Apply modal not visible at step 1.",
                "retry_class": "targeted_retry",
            }
        ),
        encoding="utf-8",
    )

    result = _load_linkedin_failure_result(submit_dir)

    assert result is not None
    assert result["failure_type"] == "linkedin_modal_missing"
    assert result["retry_class"] == "targeted_retry"


def test_handle_linkedin_failure_result_requeues_once_for_retryable_failure(tmp_path, monkeypatch):
    import pipeline_orchestrator

    monkeypatch.setattr(pipeline_orchestrator.random, "uniform", lambda a, b: 0)

    conn = _make_in_memory_db()
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()
    conn.execute(
        "INSERT INTO jobs (id, url, status, output_dir, fix_attempts) VALUES (1, 'http://x', 'stopped', ?, 0)",
        (str(out_dir),),
    )
    conn.commit()

    result = _handle_linkedin_failure_result(
        conn,
        1,
        out_dir,
        {
            "failure_type": "linkedin_modal_missing",
            "message": "LinkedIn Easy Apply modal not visible at step 1.",
            "retry_class": "targeted_retry",
            "step_num": 1,
        },
    )

    assert result == "queued"
    row = conn.execute("SELECT status, fix_attempts, provider, error_message FROM jobs WHERE id = 1").fetchone()
    assert row["status"] == "queued"
    assert row["fix_attempts"] == 1
    assert row["provider"] is None
    assert row["error_message"] == ""


def test_handle_linkedin_failure_result_stops_with_classified_failure_after_retry_used(tmp_path):
    conn = _make_in_memory_db()
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()
    conn.execute(
        "INSERT INTO jobs (id, url, status, output_dir, fix_attempts) VALUES (1, 'http://x', 'stopped', ?, 1)",
        (str(out_dir),),
    )
    conn.commit()

    result = _handle_linkedin_failure_result(
        conn,
        1,
        out_dir,
        {
            "failure_type": "linkedin_validation_loop",
            "message": "LinkedIn validation errors persisted at step 4.",
            "retry_class": "targeted_retry",
            "step_num": 4,
        },
    )

    assert result == "stopped"
    row = conn.execute("SELECT status, fix_attempts, failure_type, error_message FROM jobs WHERE id = 1").fetchone()
    assert row["status"] == "stopped"
    assert row["fix_attempts"] == 1
    assert row["failure_type"] == "linkedin_validation_loop"
    assert row["error_message"] == "LinkedIn validation errors persisted at step 4."


def test_handle_linkedin_failure_result_stops_immediately_for_non_retryable_failure(tmp_path):
    conn = _make_in_memory_db()
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()
    conn.execute(
        "INSERT INTO jobs (id, url, status, output_dir, fix_attempts) VALUES (1, 'http://x', 'stopped', ?, 0)",
        (str(out_dir),),
    )
    conn.commit()

    result = _handle_linkedin_failure_result(
        conn,
        1,
        out_dir,
        {
            "failure_type": "linkedin_resume_upload_verification_failed",
            "message": "LinkedIn exposed a visible resume upload path, but the expected resume was never selected.",
            "retry_class": "none",
        },
    )

    assert result == "stopped"
    row = conn.execute("SELECT status, fix_attempts, failure_type FROM jobs WHERE id = 1").fetchone()
    assert row["status"] == "stopped"
    assert row["fix_attempts"] == 0
    assert row["failure_type"] == "linkedin_resume_upload_verification_failed"


def test_synthesize_linkedin_timeout_result_uses_existing_debug_artifacts(tmp_path):
    submit_dir = tmp_path / "submit"
    submit_dir.mkdir()
    debug_screenshot = submit_dir / "linkedin_submit_debug.png"
    debug_screenshot.write_text("png", encoding="utf-8")
    page_dir = submit_dir / "linkedin_autofill_pages"
    page_dir.mkdir()
    page_screenshot = page_dir / "page_03.png"
    page_screenshot.write_text("png", encoding="utf-8")

    result = _synthesize_linkedin_timeout_result(submit_dir)

    assert result is not None
    assert result["status"] == "failed"
    assert result["failure_type"] == "linkedin_timeout_after_partial_fill"
    assert result["retry_class"] == "targeted_retry"
    assert result["artifacts"]["submit_debug_screenshot"] == str(debug_screenshot)
    assert result["artifacts"]["step_screenshot"] == str(page_screenshot)


def test_load_failed_submission_result_reads_generic_failed_result(tmp_path):
    submit_dir = tmp_path / "submit"
    submit_dir.mkdir()
    (submit_dir / "application_submission_result.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "board": "workday",
                "failure_type": "my_information_validation",
                "message": "Workday My Information page still shows required validation errors.",
            }
        ),
        encoding="utf-8",
    )

    result = _load_failed_submission_result(submit_dir)

    assert result is not None
    assert result["failure_type"] == "my_information_validation"
    assert result["board"] == "workday"


def test_handle_failed_submission_result_stops_with_classified_workday_failure(tmp_path):
    conn = _make_in_memory_db()
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()
    conn.execute(
        "INSERT INTO jobs (id, url, status, output_dir, fix_attempts) VALUES (1, 'http://x', 'stopped', ?, 0)",
        (str(out_dir),),
    )
    conn.commit()

    result = _handle_failed_submission_result(
        conn,
        1,
        out_dir,
        {
            "board": "workday",
            "failure_type": "my_information_validation",
            "message": "Workday My Information page still shows required validation errors.",
            "current_page": "my_information",
        },
    )

    assert result == "stopped"
    row = conn.execute("SELECT status, failure_type, error_message FROM jobs WHERE id = 1").fetchone()
    assert row["status"] == "stopped"
    assert row["failure_type"] == "my_information_validation"
    assert row["error_message"] == "Workday My Information page still shows required validation errors."


def test_handle_failed_submission_result_stops_with_classified_greenhouse_failure(tmp_path):
    conn = _make_in_memory_db()
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()
    conn.execute(
        "INSERT INTO jobs (id, url, status, output_dir, fix_attempts) VALUES (1, 'http://x', 'stopped', ?, 0)",
        (str(out_dir),),
    )
    conn.commit()

    result = _handle_failed_submission_result(
        conn,
        1,
        out_dir,
        {
            "board": "greenhouse",
            "failure_type": "greenhouse_review_proof_gap",
            "message": "Greenhouse autofill reached review with unconfirmed fields.",
            "current_page": "review",
        },
    )

    assert result == "stopped"
    row = conn.execute("SELECT status, failure_type, error_message FROM jobs WHERE id = 1").fetchone()
    assert row["status"] == "stopped"
    assert row["failure_type"] == "greenhouse_review_proof_gap"
    assert row["error_message"] == "Greenhouse autofill reached review with unconfirmed fields."


def test_handle_failed_submission_result_repairable_records_cluster_and_metric(monkeypatch, tmp_path):
    import pipeline_orchestrator

    conn = _make_in_memory_db()
    out_dir = tmp_path / "output" / "acme" / "pm"
    out_dir.mkdir(parents=True, exist_ok=True)
    conn.execute("INSERT INTO jobs (id, url, status, output_dir, fix_attempts) VALUES (1, 'http://x', 'stopped', ?, 0)", (str(out_dir),))
    conn.commit()
    monkeypatch.setattr(pipeline_orchestrator, "_schedule_audit_retry", lambda *_args, **_kwargs: "queued")

    result = _handle_failed_submission_result(
        conn,
        1,
        out_dir,
        {
            "board": "workday",
            "failure_type": "submit_failed",
            "message": "Submission failed.",
        },
    )

    cluster = conn.execute("SELECT * FROM repair_clusters ORDER BY id DESC LIMIT 1").fetchone()
    metrics = conn.execute("SELECT last_repair_cluster_id FROM job_metrics WHERE job_id = 1").fetchone()
    assert result == "queued"
    assert cluster is not None
    assert cluster["latest_summary"] == "Submission failed."
    assert metrics["last_repair_cluster_id"] == cluster["id"]


# ---------------------------------------------------------------------------
# _extract_error_hint tests
# ---------------------------------------------------------------------------


def test_extract_error_hint_strips_ansi():
    """ANSI escape codes should be removed."""
    stderr = "\x1b[31mERROR: something failed\x1b[0m\n"
    assert "ERROR: something failed" in _extract_error_hint(stderr)
    assert "\x1b" not in _extract_error_hint(stderr)


def test_extract_error_hint_skips_box_drawing():
    """Lines that are purely box-drawing should be skipped."""
    stderr = "Real error message\n────────────────────────────────\n"
    hint = _extract_error_hint(stderr)
    assert hint == "Real error message"


def test_extract_error_hint_returns_last_meaningful():
    """Should return the last meaningful line(s), not separator lines."""
    stderr = "Step 1: ok\nStep 2: ok\njson.decoder.JSONDecodeError: line 56\n─────────────────\n"
    hint = _extract_error_hint(stderr)
    assert "JSONDecodeError" in hint


def test_extract_error_hint_respects_max_len():
    """Output should be truncated to max_len."""
    stderr = "A" * 600 + "\n"
    hint = _extract_error_hint(stderr, max_len=100)
    assert len(hint) <= 100


def test_extract_error_hint_empty_input():
    """Empty or None input should return empty string."""
    assert _extract_error_hint("") == ""
    assert _extract_error_hint(None) == ""


def test_extract_error_hint_all_box_drawing():
    """When all lines are box-drawing, fall back to last raw line."""
    stderr = "────────────────────────────────\n━━━━━━━━━━━━━━━━━━━━━━\n"
    hint = _extract_error_hint(stderr)
    assert len(hint) > 0  # should return something rather than empty


# ---------------------------------------------------------------------------
# get_recent_auth_failures tests
# ---------------------------------------------------------------------------

from job_db import get_recent_auth_failures


def _make_auth_failure_db():
    """Create an in-memory DB with the board and failure_type columns needed."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            url TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued',
            board TEXT,
            auth_scope TEXT,
            auth_state TEXT,
            error_message TEXT,
            failure_type TEXT,
            fix_attempts INTEGER DEFAULT 0,
            progress TEXT,
            provider TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            event_type TEXT NOT NULL,
            detail TEXT,
            detail_json TEXT,
            initiator TEXT,
            process_info TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    return conn


def test_get_recent_auth_failures_returns_correct_count():
    """Should count only auth_failed jobs for the specified board."""
    conn = _make_auth_failure_db()
    # 3 auth failures for workday
    for i in range(1, 4):
        conn.execute(
            "INSERT INTO jobs (id, url, status, board, failure_type, updated_at) "
            "VALUES (?, 'http://x', 'stopped', 'workday', 'auth_failed', datetime('now', '-1 hour'))",
            (i,),
        )
    # 1 non-auth failure for workday (should not count)
    conn.execute(
        "INSERT INTO jobs (id, url, status, board, failure_type, updated_at) "
        "VALUES (4, 'http://x', 'stopped', 'workday', 'submit_failed', datetime('now', '-1 hour'))"
    )
    # 1 auth failure for icims (different board, should not count)
    conn.execute(
        "INSERT INTO jobs (id, url, status, board, failure_type, updated_at) "
        "VALUES (5, 'http://x', 'stopped', 'icims', 'auth_failed', datetime('now', '-1 hour'))"
    )
    conn.commit()

    assert get_recent_auth_failures(conn, "workday") == 3
    assert get_recent_auth_failures(conn, "icims") == 1
    assert get_recent_auth_failures(conn, "greenhouse") == 0


def test_get_recent_auth_failures_excludes_old_failures():
    """Auth failures older than the specified window should not be counted."""
    conn = _make_auth_failure_db()
    # Old failure (>24h ago)
    conn.execute(
        "INSERT INTO jobs (id, url, status, board, failure_type, updated_at) "
        "VALUES (1, 'http://x', 'stopped', 'workday', 'auth_failed', datetime('now', '-25 hours'))"
    )
    # Recent failure (<24h ago)
    conn.execute(
        "INSERT INTO jobs (id, url, status, board, failure_type, updated_at) "
        "VALUES (2, 'http://x', 'stopped', 'workday', 'auth_failed', datetime('now', '-1 hour'))"
    )
    conn.commit()

    assert get_recent_auth_failures(conn, "workday", hours=24) == 1


def test_get_recent_auth_failures_filters_by_auth_scope():
    """Workday guard counts should be tenant-scoped when auth_scope is provided."""
    conn = _make_auth_failure_db()
    conn.execute(
        "INSERT INTO jobs (id, url, status, board, auth_scope, failure_type, updated_at) "
        "VALUES (1, 'http://x', 'stopped', 'workday', 'workday:factset/factsetcareers', 'auth_failed', datetime('now', '-1 hour'))"
    )
    conn.execute(
        "INSERT INTO jobs (id, url, status, board, auth_scope, failure_type, updated_at) "
        "VALUES (2, 'http://x', 'stopped', 'workday', 'workday:autodesk/careers', 'auth_failed', datetime('now', '-1 hour'))"
    )
    conn.execute(
        "INSERT INTO jobs (id, url, status, board, auth_scope, failure_type, updated_at) "
        "VALUES (3, 'http://x', 'stopped', 'workday', 'workday:factset/factsetcareers', 'auth_failed', datetime('now', '-1 hour'))"
    )
    conn.commit()

    assert get_recent_auth_failures(conn, "workday") == 3
    assert get_recent_auth_failures(conn, "workday", auth_scope="workday:factset/factsetcareers") == 2
    assert get_recent_auth_failures(conn, "workday", auth_scope="workday:autodesk/careers") == 1


def test_get_recent_auth_failures_filters_by_icims_auth_scope():
    conn = _make_auth_failure_db()
    conn.execute(
        "INSERT INTO jobs (id, url, status, board, auth_scope, failure_type, updated_at) "
        "VALUES (1, 'http://x', 'stopped', 'icims', 'icims:amazonjobs-us.icims.com', 'auth_failed', datetime('now', '-1 hour'))"
    )
    conn.execute(
        "INSERT INTO jobs (id, url, status, board, auth_scope, failure_type, updated_at) "
        "VALUES (2, 'http://x', 'stopped', 'icims', 'icims:careers-docusign.icims.com', 'auth_failed', datetime('now', '-1 hour'))"
    )
    conn.execute(
        "INSERT INTO jobs (id, url, status, board, auth_scope, failure_type, updated_at) "
        "VALUES (3, 'http://x', 'stopped', 'icims', 'icims:amazonjobs-us.icims.com', 'auth_guarded', datetime('now', '-1 hour'))"
    )
    conn.commit()

    assert get_recent_auth_failures(conn, "icims") == 2
    assert get_recent_auth_failures(conn, "icims", auth_scope="icims:amazonjobs-us.icims.com") == 1
    assert get_recent_auth_failures(conn, "icims", auth_scope="icims:careers-docusign.icims.com") == 1


def test_get_recent_auth_failures_empty_board():
    """Empty or None board should return 0."""
    conn = _make_auth_failure_db()
    assert get_recent_auth_failures(conn, "") == 0
    assert get_recent_auth_failures(conn, None) == 0


def test_board_with_3_plus_auth_failures_detected():
    """When a board has 3+ auth failures, count should be >= 3 (triggering skip)."""
    conn = _make_auth_failure_db()
    for i in range(1, 5):  # 4 auth failures
        conn.execute(
            "INSERT INTO jobs (id, url, status, board, failure_type, updated_at) "
            "VALUES (?, 'http://x', 'stopped', 'workday', 'auth_failed', datetime('now', '-30 minutes'))",
            (i,),
        )
    conn.commit()

    count = get_recent_auth_failures(conn, "workday")
    assert count >= 3  # Should trigger the skip guard in the orchestrator
