import importlib.util
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_run_pipeline = None


def _get_run_pipeline():
    global _run_pipeline
    if _run_pipeline is None:
        _run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
    return _run_pipeline


class _DummyResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class RunPipelineSyncTests(unittest.TestCase):
    def test_sync_google_doc_writes_output_and_state(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        payload = b"candidate context\n"

        def opener(_request, timeout=30):
            self.assertEqual(timeout, 30)
            return _DummyResponse(payload)

        with TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "candidate_context.md"
            state_path = Path(tmp_dir) / ".candidate_context_sync_state.json"

            run_pipeline._sync_google_doc(
                label="candidate_context.md",
                output_path=output_path,
                state_path=state_path,
                export_url="https://example.com/export.txt",
                opener=opener,
            )

            self.assertEqual(output_path.read_bytes(), payload)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["sha256"], run_pipeline._sha256(payload))

    def test_sync_supporting_docs_runs_both_syncs(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")

        with (
            mock.patch.object(run_pipeline, "sync_work_stories") as sync_work_stories,
            mock.patch.object(run_pipeline, "sync_candidate_context") as sync_candidate_context,
        ):
            run_pipeline.sync_supporting_docs()

        sync_work_stories.assert_called_once_with()
        sync_candidate_context.assert_called_once_with()

    def test_run_step_uses_devnull_stdin(self):
        run_pipeline = _get_run_pipeline()
        completed = mock.Mock(returncode=0)

        with mock.patch.object(run_pipeline.subprocess, "run", return_value=completed) as run:
            result = run_pipeline._run_step(
                "Build resume",
                ["uv", "run", "python", "scripts/build_resume.py"],
                check=False,
            )

        self.assertIs(result, completed)
        _, kwargs = run.call_args
        self.assertIs(kwargs["stdin"], run_pipeline.subprocess.DEVNULL)


class PipelineTempDirTests(unittest.TestCase):
    def test_create_pipeline_tmp_dir_is_unique_per_run(self):
        run_pipeline = _get_run_pipeline()

        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            first = run_pipeline._create_pipeline_tmp_dir(root)
            second = run_pipeline._create_pipeline_tmp_dir(root)

            self.assertNotEqual(first, second)
            self.assertEqual(first.parent, root)
            self.assertEqual(second.parent, root)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())


class UserAgentRotationTests(unittest.TestCase):
    """Test that UA rotation cycles through the pool."""

    def test_next_user_agent_cycles_through_pool(self):
        rp = _get_run_pipeline()
        # Reset index to a known state
        rp._ua_index = 0
        pool_size = len(rp._UA_POOL)

        seen = []
        for _ in range(pool_size * 2):
            seen.append(rp._next_user_agent())

        # First cycle should match the pool exactly
        self.assertEqual(seen[:pool_size], rp._UA_POOL)
        # Second cycle should repeat
        self.assertEqual(seen[pool_size:], rp._UA_POOL)

    def test_next_user_agent_returns_strings(self):
        rp = _get_run_pipeline()
        rp._ua_index = 0
        ua = rp._next_user_agent()
        self.assertIsInstance(ua, str)
        self.assertIn("Mozilla", ua)

    def test_ua_pool_has_at_least_5_entries(self):
        rp = _get_run_pipeline()
        self.assertGreaterEqual(len(rp._UA_POOL), 5)


class DomainRateLimitTests(unittest.TestCase):
    """Test per-domain rate limiting enforces 3s minimum gap."""

    def setUp(self):
        rp = _get_run_pipeline()
        # Clear state between tests
        rp._domain_last_fetch.clear()

    def test_first_request_no_delay(self):
        rp = _get_run_pipeline()
        t0 = time.monotonic()
        rp._enforce_domain_rate_limit("https://example.com/job/123")
        elapsed = time.monotonic() - t0
        # First request should not sleep
        self.assertLess(elapsed, 0.5)

    def test_rapid_second_request_is_delayed(self):
        rp = _get_run_pipeline()
        rp._enforce_domain_rate_limit("https://example.com/job/123")
        t0 = time.monotonic()
        rp._enforce_domain_rate_limit("https://example.com/job/456")
        elapsed = time.monotonic() - t0
        # Should have waited close to 3 seconds
        self.assertGreaterEqual(elapsed, 2.5)

    def test_different_domains_independent(self):
        rp = _get_run_pipeline()
        rp._enforce_domain_rate_limit("https://alpha.com/job/1")
        t0 = time.monotonic()
        rp._enforce_domain_rate_limit("https://beta.com/job/2")
        elapsed = time.monotonic() - t0
        # Different domain — no delay
        self.assertLess(elapsed, 0.5)

    def test_gap_constant_value(self):
        rp = _get_run_pipeline()
        self.assertEqual(rp._DOMAIN_MIN_GAP_SECONDS, 3.0)


class JDFetchMaxRetriesTests(unittest.TestCase):
    """Test that JD_FETCH_MAX_RETRIES default is 5."""

    def test_default_retries_is_five(self):
        import os

        # Remove env var to test default
        old_val = os.environ.pop("JD_FETCH_MAX_RETRIES", None)
        try:
            val = int(os.environ.get("JD_FETCH_MAX_RETRIES", "5"))
            self.assertEqual(val, 5)
        finally:
            if old_val is not None:
                os.environ["JD_FETCH_MAX_RETRIES"] = old_val

    def test_env_var_overrides_default(self):
        import os

        old_val = os.environ.get("JD_FETCH_MAX_RETRIES")
        os.environ["JD_FETCH_MAX_RETRIES"] = "7"
        try:
            val = int(os.environ.get("JD_FETCH_MAX_RETRIES", "5"))
            self.assertEqual(val, 7)
        finally:
            if old_val is not None:
                os.environ["JD_FETCH_MAX_RETRIES"] = old_val
            else:
                del os.environ["JD_FETCH_MAX_RETRIES"]

    def test_main_stops_retrying_after_terminal_job_closed_extraction_hint(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")

        with TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            extraction_attempt = mock.Mock(
                return_value=(
                    None,
                    ["job_closed: URL returned HTTP 404 at https://example.com/jobs/123"],
                )
            )
            sleep = mock.Mock()

            with (
                mock.patch.object(
                    run_pipeline.sys,
                    "argv",
                    [
                        "scripts/run_pipeline.py",
                        "https://example.com/jobs/123",
                        "--skip-sync",
                    ],
                ),
                mock.patch.object(run_pipeline, "_create_pipeline_tmp_dir", return_value=tmp_root),
                mock.patch.object(run_pipeline, "_run_url_extraction_attempt", extraction_attempt),
                mock.patch.object(run_pipeline, "_enforce_domain_rate_limit"),
                mock.patch.object(run_pipeline.time, "sleep", sleep),
            ):
                with self.assertRaises(SystemExit) as exc:
                    run_pipeline.main()

        self.assertEqual(exc.exception.code, 1)
        self.assertEqual(extraction_attempt.call_count, 1)
        sleep.assert_not_called()


class ScrapeJobUserAgentTests(unittest.TestCase):
    """Test that scrape_job.py respects JOB_ASSETS_USER_AGENT env var."""

    def test_get_user_agent_uses_env_var(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        import os

        old_val = os.environ.get("JOB_ASSETS_USER_AGENT")
        os.environ["JOB_ASSETS_USER_AGENT"] = "TestBot/1.0"
        try:
            self.assertEqual(scrape_job._get_user_agent(), "TestBot/1.0")
        finally:
            if old_val is not None:
                os.environ["JOB_ASSETS_USER_AGENT"] = old_val
            else:
                os.environ.pop("JOB_ASSETS_USER_AGENT", None)

    def test_get_user_agent_falls_back_to_default(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        import os

        old_val = os.environ.pop("JOB_ASSETS_USER_AGENT", None)
        try:
            ua = scrape_job._get_user_agent()
            self.assertIn("Mozilla", ua)
            self.assertEqual(ua, scrape_job._DEFAULT_USER_AGENT)
        finally:
            if old_val is not None:
                os.environ["JOB_ASSETS_USER_AGENT"] = old_val


if __name__ == "__main__":
    unittest.main()
