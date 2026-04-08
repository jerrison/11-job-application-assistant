import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_shell(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["bash", "-lc", f"source scripts/llm_common.sh; {script}"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=merged_env,
    )


class CacheIsFreshTests(unittest.TestCase):
    def test_default_ttl_is_30_days(self):
        """job_assets_cache_is_fresh with no explicit TTL arg uses 30 days."""
        with tempfile.NamedTemporaryFile(suffix=".json") as f:
            f.write(b"{}")
            f.flush()
            # File just created — should be fresh with 30-day default
            result = run_shell(f'job_assets_cache_is_fresh "{f.name}"')
            self.assertEqual(result.returncode, 0)

    def test_env_var_overrides_default_ttl(self):
        """JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS overrides the 30-day default."""
        with tempfile.NamedTemporaryFile(suffix=".json") as f:
            f.write(b"{}")
            f.flush()
            # Set TTL to 0 — file should be stale immediately
            result = run_shell(
                f'job_assets_cache_is_fresh "{f.name}"',
                env={"JOB_ASSETS_RESEARCH_CACHE_TTL_DAYS": "0"},
            )
            self.assertEqual(result.returncode, 1)


class RoleCacheIsFreshTests(unittest.TestCase):
    def test_fresh_when_hash_matches_and_within_ttl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jd_path = Path(tmpdir) / "jd_parsed.json"
            jd_path.write_text('{"title": "PM"}')
            jd_hash = hashlib.sha256(jd_path.read_bytes()).hexdigest()

            role_cache = Path(tmpdir) / "role_research_cache.json"
            role_cache.write_text(
                json.dumps(
                    {
                        "role_context": "test",
                        "researched_at": "2026-03-13T00:00:00Z",
                        "jd_hash": jd_hash,
                    }
                )
            )

            result = run_shell(f'job_assets_role_cache_is_fresh "{role_cache}" "{jd_path}" 30')
            self.assertEqual(result.returncode, 0)

    def test_stale_when_hash_differs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jd_path = Path(tmpdir) / "jd_parsed.json"
            jd_path.write_text('{"title": "PM"}')

            role_cache = Path(tmpdir) / "role_research_cache.json"
            role_cache.write_text(
                json.dumps(
                    {
                        "role_context": "test",
                        "researched_at": "2026-03-13T00:00:00Z",
                        "jd_hash": "wrong_hash_value",
                    }
                )
            )

            result = run_shell(f'job_assets_role_cache_is_fresh "{role_cache}" "{jd_path}" 30')
            self.assertEqual(result.returncode, 1)

    def test_stale_when_role_cache_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jd_path = Path(tmpdir) / "jd_parsed.json"
            jd_path.write_text('{"title": "PM"}')

            result = run_shell(f'job_assets_role_cache_is_fresh "{tmpdir}/nonexistent.json" "{jd_path}" 30')
            self.assertEqual(result.returncode, 1)

    def test_stale_when_jd_parsed_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            role_cache = Path(tmpdir) / "role_research_cache.json"
            role_cache.write_text(
                json.dumps(
                    {
                        "role_context": "test",
                        "jd_hash": "abc123",
                    }
                )
            )

            result = run_shell(f'job_assets_role_cache_is_fresh "{role_cache}" "{tmpdir}/missing_jd.json" 30')
            self.assertEqual(result.returncode, 1)

    def test_stale_when_over_ttl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jd_path = Path(tmpdir) / "jd_parsed.json"
            jd_path.write_text('{"title": "PM"}')
            jd_hash = hashlib.sha256(jd_path.read_bytes()).hexdigest()

            role_cache = Path(tmpdir) / "role_research_cache.json"
            role_cache.write_text(
                json.dumps(
                    {
                        "role_context": "test",
                        "jd_hash": jd_hash,
                    }
                )
            )
            # Set mtime to 31 days ago
            old_time = time.time() - (31 * 86400)
            os.utime(role_cache, (old_time, old_time))

            result = run_shell(f'job_assets_role_cache_is_fresh "{role_cache}" "{jd_path}" 30')
            self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
