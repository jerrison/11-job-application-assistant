"""Tests for generate_interview_prep provider guard and fallback logic."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from scripts.generate_interview_prep import _run_provider


@pytest.fixture()
def prep_dir(tmp_path: Path) -> Path:
    d = tmp_path / "interview_prep"
    d.mkdir()
    return d


DUMMY_PROMPT = "test prompt"


# ---------------------------------------------------------------------------
# Provider guard: configured provider binary unavailable — skip
# ---------------------------------------------------------------------------


@patch("scripts.generate_interview_prep.provider_available", return_value=False)
def test_provider_skips_when_configured_binary_unavailable(
    mock_provider_available: MagicMock,
    prep_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When configured provider is unavailable, skip with warning."""
    with patch.dict("os.environ", {"ASSET_LLM_PROVIDER": "codex"}, clear=False):
        import logging

        with caplog.at_level(logging.WARNING, logger="scripts.generate_interview_prep"):
            result = _run_provider(DUMMY_PROMPT, prep_dir)

    assert result == ""
    assert "not available" in caplog.text


# ---------------------------------------------------------------------------
# OpenAI path: provider_command is used with shared search/file flags
# ---------------------------------------------------------------------------


@patch("scripts.generate_interview_prep.provider_command")
@patch("scripts.generate_interview_prep.provider_available", return_value=True)
@patch("scripts.generate_interview_prep.subprocess.run")
def test_no_env_defaults_to_openai_provider(
    mock_run: MagicMock,
    mock_provider_available: MagicMock,
    mock_provider_command: MagicMock,
    prep_dir: Path,
) -> None:
    """Interview prep defaults to OpenAI when no provider env var is set."""
    mock_provider_command.return_value = ["openai-provider"]
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="# Interview Prep",
        stderr="",
    )

    with patch.dict("os.environ", {}, clear=True):
        result = _run_provider(DUMMY_PROMPT, prep_dir)

    assert result == "# Interview Prep"
    mock_provider_available.assert_called_once_with("openai")
    mock_provider_command.assert_called_once_with(
        "openai",
        DUMMY_PROMPT,
        project_root=Path(__file__).resolve().parent.parent,
        search_enabled=True,
        file_tools_enabled=True,
        claude_allowed_tools=None,
    )


@patch("scripts.generate_interview_prep.provider_command")
@patch("scripts.generate_interview_prep.provider_available", return_value=True)
@patch("scripts.generate_interview_prep.subprocess.run")
def test_openai_interview_prep_uses_provider_command_with_search_and_file_tools(
    mock_run: MagicMock,
    mock_provider_available: MagicMock,
    mock_provider_command: MagicMock,
    prep_dir: Path,
) -> None:
    """OpenAI interview prep routes through provider_command with shared flags."""
    mock_provider_command.return_value = ["openai-provider"]
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="# Interview Prep",
        stderr="",
    )

    with patch.dict("os.environ", {"ASSET_LLM_PROVIDER": "openai"}, clear=False):
        result = _run_provider(DUMMY_PROMPT, prep_dir)

    assert result == "# Interview Prep"
    mock_provider_available.assert_called_once_with("openai")
    mock_provider_command.assert_called_once_with(
        "openai",
        DUMMY_PROMPT,
        project_root=Path(__file__).resolve().parent.parent,
        search_enabled=True,
        file_tools_enabled=True,
        claude_allowed_tools=None,
    )
    mock_run.assert_called_once_with(
        ["openai-provider"],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
        timeout=900,
    )


# ---------------------------------------------------------------------------
# Configured provider is used directly (no Claude fallback behavior)
# ---------------------------------------------------------------------------


@patch("scripts.generate_interview_prep.provider_command")
@patch("scripts.generate_interview_prep.provider_available", return_value=True)
@patch("scripts.generate_interview_prep.subprocess.run")
def test_uses_configured_provider_without_claude_fallback(
    mock_run: MagicMock,
    mock_provider_available: MagicMock,
    mock_provider_command: MagicMock,
    prep_dir: Path,
) -> None:
    """Interview prep uses configured provider directly (e.g. codex)."""
    mock_provider_command.return_value = ["codex", "exec"]
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="# Prep",
        stderr="",
    )

    with patch.dict("os.environ", {"ASSET_LLM_PROVIDER": "codex"}, clear=False):
        result = _run_provider(DUMMY_PROMPT, prep_dir)

    assert result == "# Prep"
    mock_provider_available.assert_called_once_with("codex")
    mock_provider_command.assert_called_once_with(
        "codex",
        DUMMY_PROMPT,
        project_root=Path(__file__).resolve().parent.parent,
        search_enabled=True,
        file_tools_enabled=False,
        claude_allowed_tools=None,
    )


# ---------------------------------------------------------------------------
# Error messages are provider-specific / provider-agnostic
# ---------------------------------------------------------------------------


@patch("scripts.generate_interview_prep.provider_command", return_value=["openai-provider"])
@patch("scripts.generate_interview_prep.provider_available", return_value=True)
@patch("scripts.generate_interview_prep.subprocess.run")
def test_error_message_on_nonzero_exit_mentions_provider(
    mock_run: MagicMock,
    mock_provider_available: MagicMock,
    mock_provider_command: MagicMock,
    prep_dir: Path,
) -> None:
    """Error on non-zero exit mentions the configured provider."""
    mock_run.return_value = MagicMock(
        returncode=1,
        stdout="",
        stderr="some error",
    )

    with patch.dict("os.environ", {"ASSET_LLM_PROVIDER": "openai"}, clear=False):
        with pytest.raises(RuntimeError, match=r"openai provider exited with code 1"):
            _run_provider(DUMMY_PROMPT, prep_dir)


@patch("scripts.generate_interview_prep.provider_command", return_value=["openai-provider"])
@patch("scripts.generate_interview_prep.provider_available", return_value=True)
@patch("scripts.generate_interview_prep.subprocess.run")
def test_error_message_on_empty_output_mentions_provider(
    mock_run: MagicMock,
    mock_provider_available: MagicMock,
    mock_provider_command: MagicMock,
    prep_dir: Path,
) -> None:
    """Error on empty output mentions the configured provider."""
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="",
        stderr="",
    )

    with patch.dict("os.environ", {"ASSET_LLM_PROVIDER": "openai"}, clear=False):
        with pytest.raises(RuntimeError, match=r"openai provider produced no output"):
            _run_provider(DUMMY_PROMPT, prep_dir)


# ---------------------------------------------------------------------------
# File write fallback: provider wrote the file via Write tool
# ---------------------------------------------------------------------------


@patch("scripts.generate_interview_prep.provider_command", return_value=["claude-provider"])
@patch("scripts.generate_interview_prep.provider_available", return_value=True)
@patch("scripts.generate_interview_prep.subprocess.run")
def test_reads_file_if_provider_wrote_it(
    mock_run: MagicMock,
    mock_provider_available: MagicMock,
    mock_provider_command: MagicMock,
    prep_dir: Path,
) -> None:
    """If provider wrote interview_prep.md via Write tool, read from file."""
    output_path = prep_dir / "interview_prep.md"
    output_path.write_text("# Written by Provider\nContent here", encoding="utf-8")

    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="ignored stdout",
        stderr="",
    )

    with patch.dict("os.environ", {"ASSET_LLM_PROVIDER": "claude"}, clear=False):
        result = _run_provider(DUMMY_PROMPT, prep_dir)

    assert result == "# Written by Provider\nContent here"
