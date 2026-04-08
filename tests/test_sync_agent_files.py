"""Tests for scripts/sync_agent_files.py — idempotency, staleness, CLI, content, errors."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Import the module from its file path (it isn't on the Python path)
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "sync_agent_files.py"
_spec = importlib.util.spec_from_file_location("sync_agent_files", _SCRIPT_PATH)
assert _spec and _spec.loader
sync_mod = importlib.util.module_from_spec(_spec)
sys.modules["sync_agent_files"] = sync_mod
_spec.loader.exec_module(sync_mod)

# Convenience aliases
generate = sync_mod.generate
check = sync_mod.check
main = sync_mod.main

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_AGENTS_BODY = "# AGENTS\n\nThis is the canonical agent prompt.\n"


@pytest.fixture()
def isolated_env(tmp_path: Path):
    """Set up an isolated filesystem with a fake AGENTS.md and TARGETS."""
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text(FAKE_AGENTS_BODY)

    fake_targets: dict[str, dict] = {
        "alpha": {
            "path": tmp_path / "ALPHA.md",
            "header": "<!-- GENERATED alpha -->\n",
            "builder": None,
        },
        "beta": {
            "path": tmp_path / "sub" / "BETA.md",
            "header": "<!-- GENERATED beta -->\n",
            "builder": None,
        },
    }

    with (
        patch.object(sync_mod, "AGENTS_MD", agents_md),
        patch.object(sync_mod, "PROJECT_ROOT", tmp_path),
        patch.object(sync_mod, "TARGETS", fake_targets),
    ):
        yield tmp_path, fake_targets


# ===================================================================
# Category A: Idempotency
# ===================================================================


class TestIdempotency:
    def test_generate_returns_true_on_first_write(self, isolated_env):
        """Generating a missing file should write it and return True."""
        tmp_path, targets = isolated_env
        assert not targets["alpha"]["path"].exists()
        assert generate("alpha") is True
        assert targets["alpha"]["path"].exists()

    def test_generate_returns_false_when_no_change(self, isolated_env):
        """Second generate of the same target returns False (no change)."""
        tmp_path, targets = isolated_env
        generate("alpha")
        assert generate("alpha") is False

    def test_generate_twice_produces_identical_output(self, isolated_env):
        """Content after first and second generate must be identical."""
        tmp_path, targets = isolated_env
        generate("alpha")
        content_first = targets["alpha"]["path"].read_text()
        generate("alpha")
        content_second = targets["alpha"]["path"].read_text()
        assert content_first == content_second


# ===================================================================
# Category B: Staleness Detection
# ===================================================================


class TestStalenessDetection:
    def test_check_passes_when_file_is_current(self, isolated_env):
        """After generating, check should return True."""
        generate("alpha")
        assert check("alpha") is True

    def test_check_fails_when_file_is_missing(self, isolated_env):
        """Check on a file that was never generated should return False."""
        tmp_path, targets = isolated_env
        assert not targets["alpha"]["path"].exists()
        assert check("alpha") is False

    def test_check_fails_when_file_is_stale(self, isolated_env):
        """Writing wrong content then checking should return False."""
        tmp_path, targets = isolated_env
        dest = targets["alpha"]["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("stale content that does not match\n")
        assert check("alpha") is False


# ===================================================================
# Category C: CLI Exit Codes
# ===================================================================


class TestCLIExitCodes:
    def test_main_check_returns_0_when_all_in_sync(self, isolated_env):
        """Generate all targets, then --check should exit 0."""
        tmp_path, targets = isolated_env
        for name in targets:
            generate(name)
        assert main(["--check"]) == 0

    def test_main_check_returns_1_when_any_stale(self, isolated_env):
        """Generate only some targets, then --check should exit 1."""
        tmp_path, targets = isolated_env
        generate("alpha")
        # "beta" is not generated
        assert main(["--check"]) == 1

    def test_main_returns_1_when_agents_md_missing(self, isolated_env):
        """If AGENTS.md is missing, main should exit 1."""
        tmp_path, targets = isolated_env
        agents_md = tmp_path / "AGENTS.md"
        agents_md.unlink()
        assert main([]) == 1


# ===================================================================
# Category D: Content Validation
# ===================================================================


class TestContentValidation:
    def test_generated_file_starts_with_header(self, isolated_env):
        """Generated file should start with the header comment."""
        tmp_path, targets = isolated_env
        generate("alpha")
        content = targets["alpha"]["path"].read_text()
        assert content.startswith("<!-- GENERATED alpha -->")

    def test_generated_file_contains_agents_md_body(self, isolated_env):
        """Generated file should contain the full AGENTS.md body."""
        tmp_path, targets = isolated_env
        generate("alpha")
        content = targets["alpha"]["path"].read_text()
        assert FAKE_AGENTS_BODY in content


# ===================================================================
# Category E: Error Handling
# ===================================================================


class TestErrorHandling:
    def test_generate_creates_parent_directories(self, isolated_env):
        """Generating to a nested path should create parent dirs."""
        tmp_path, targets = isolated_env
        dest = targets["beta"]["path"]
        assert not dest.parent.exists()
        assert generate("beta") is True
        assert dest.exists()
        assert dest.parent.is_dir()
