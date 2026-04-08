# tests/test_autofill_pipeline.py
import importlib.util
import json
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
    spec.loader.exec_module(module)
    return module


class AutofillMainTests(unittest.TestCase):
    def test_payload_only_writes_json_and_returns_zero(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")
        load_module("autofill_common", "scripts/autofill_common.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com",
                "steps": [],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(out_dir / "submit" / "report.json"),
                    "report_markdown": str(out_dir / "submit" / "report.md"),
                    "pre_submit_screenshot": str(out_dir / "submit" / "pre.png"),
                },
            }

            def fake_build(od, provider):
                return payload

            with mock.patch("sys.argv", ["test", str(out_dir), "--payload-only"]):
                with mock.patch.object(pipeline, "find_output_dir", return_value=out_dir):
                    with mock.patch.object(pipeline, "write_report"):
                        with mock.patch.object(pipeline, "PROJECT_ROOT", out_dir):
                            with mock.patch.object(pipeline, "default_answer_provider", return_value="claude"):
                                rc = pipeline.autofill_main("gem", fake_build)

        self.assertEqual(rc, 0)

    def test_build_payload_failure_clears_stale_current_attempt_artifacts_before_generation(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")
        load_module("autofill_common", "scripts/autofill_common.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            stale_report = submit_dir / "ashby_autofill_report.json"
            stale_markdown = submit_dir / "ashby_autofill_report.md"
            stale_screenshot = submit_dir / "ashby_autofill_pre_submit.png"
            stale_result = submit_dir / "application_submission_result.json"
            stale_pages_dir = submit_dir / "ashby_autofill_pages"
            stale_pages_dir.mkdir()
            stale_page = stale_pages_dir / "page-1.png"
            stale_report.write_text("{}", encoding="utf-8")
            stale_markdown.write_text("# stale", encoding="utf-8")
            stale_screenshot.write_bytes(b"png")
            stale_result.write_text("{}", encoding="utf-8")
            stale_page.write_bytes(b"png")

            def fake_build(od, provider):
                raise ValueError("payload failed")

            with mock.patch("sys.argv", ["test", str(out_dir)]):
                with mock.patch.object(pipeline, "find_output_dir", return_value=out_dir):
                    with mock.patch.object(pipeline, "PROJECT_ROOT", out_dir):
                        with mock.patch.object(pipeline, "default_answer_provider", return_value="claude"):
                            with self.assertRaisesRegex(ValueError, "payload failed"):
                                pipeline.autofill_main("ashby", fake_build)
            self.assertFalse(stale_report.exists())
            self.assertFalse(stale_markdown.exists())
            self.assertFalse(stale_screenshot.exists())
            self.assertFalse(stale_result.exists())
            self.assertFalse(stale_page.exists())

    def test_no_browser_board_skips_playwright(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com",
                "steps": [],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(out_dir / "submit" / "report.json"),
                    "report_markdown": str(out_dir / "submit" / "report.md"),
                    "pre_submit_screenshot": str(out_dir / "submit" / "pre.png"),
                },
            }

            def fake_build(od, provider):
                return payload

            with mock.patch("sys.argv", ["test", str(out_dir)]):
                with mock.patch.object(pipeline, "find_output_dir", return_value=out_dir):
                    with mock.patch.object(pipeline, "write_report"):
                        with mock.patch.object(pipeline, "PROJECT_ROOT", out_dir):
                            with mock.patch.object(pipeline, "default_answer_provider", return_value="claude"):
                                rc = pipeline.autofill_main("dover", fake_build, has_browser=False)

        self.assertEqual(rc, 0)


class RunBrowserPipelineTests(unittest.TestCase):
    def test_preserves_fresh_application_answers_written_during_payload_build(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            (submit_dir / "lever_autofill_pages").mkdir()
            answers_path = submit_dir / "application_answers.json"
            answers_path.write_text(
                json.dumps(
                    {
                        "generated_at_utc": "2026-04-08T00:00:00+00:00",
                        "provider": "openai",
                        "questions": [
                            {
                                "field_name": "why_company",
                                "label": "Why this company?",
                                "required": True,
                                "type": "textarea",
                            }
                        ],
                        "answers": {"why_company": "Because the mission is compelling."},
                    }
                ),
                encoding="utf-8",
            )
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com/job/123",
                "out_dir": str(out_dir),
                "steps": [],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(submit_dir / "report.json"),
                    "report_markdown": str(submit_dir / "report.md"),
                    "pre_submit_screenshot": str(submit_dir / "pre.png"),
                    "page_screenshots_dir": str(submit_dir / "lever_autofill_pages"),
                },
            }
            payload_path = submit_dir / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            class FakePage:
                def goto(self, url, **kw):
                    del url, kw

                def wait_for_selector(self, sel, **kw):
                    del sel, kw

                def wait_for_timeout(self, ms):
                    del ms

                def screenshot(self, path, full_page=False):
                    del path, full_page

                def locator(self, sel):
                    del sel

                    class L:
                        first = self

                        def count(self):
                            return 0

                    return L()

            class FakeBrowser:
                session_viewer_url = None

                def new_page(self, **kw):
                    del kw
                    return FakePage()

                def close(self):
                    pass

            fake_pw = mock.MagicMock()
            fake_pw.__enter__ = mock.Mock(return_value=fake_pw)
            fake_pw.__exit__ = mock.Mock(return_value=False)
            with mock.patch("playwright.sync_api.sync_playwright", return_value=fake_pw):
                with mock.patch.object(pipeline, "launch_chromium_browser", return_value=FakeBrowser()):
                    with mock.patch.object(pipeline, "write_report", return_value={}):
                        with mock.patch.object(
                            pipeline, "write_pending_user_input_for_unconfirmed_fields", return_value=None
                        ):
                            with mock.patch.object(pipeline, "PROJECT_ROOT", out_dir):
                                rc = pipeline.run_browser_pipeline(
                                    payload_path,
                                    headless=True,
                                    submit=False,
                                    board_name="lever",
                                    form_ready_selector="form",
                                    fill_step_fn=lambda page, step: None,
                                    page_snapshot_fn=lambda page: {},
                                    classify_state_fn=lambda snap: {"status": "pending"},
                                    click_submit_fn=lambda page: True,
                                    capture_fn=lambda page, path: None,
                                )
            self.assertEqual(rc, 0)
            self.assertTrue(answers_path.exists())

    def test_prefers_application_url_for_initial_navigation(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            (submit_dir / "jobvite_autofill_pages").mkdir()
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com/job/123",
                "application_url": "https://example.com/job/123/apply",
                "out_dir": str(out_dir),
                "steps": [],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(submit_dir / "report.json"),
                    "report_markdown": str(submit_dir / "report.md"),
                    "pre_submit_screenshot": str(submit_dir / "pre.png"),
                    "page_screenshots_dir": str(submit_dir / "jobvite_autofill_pages"),
                },
            }
            payload_path = submit_dir / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            class FakePage:
                def __init__(self):
                    self.goto_url = None

                def goto(self, url, **kw):
                    self.goto_url = url

                def wait_for_selector(self, sel, **kw):
                    pass

                def wait_for_timeout(self, ms):
                    pass

                def screenshot(self, path, full_page=False):
                    pass

                def locator(self, sel):
                    class L:
                        first = self

                        def count(self):
                            return 0

                    return L()

            fake_page = FakePage()

            class FakeBrowser:
                session_viewer_url = None

                def new_page(self, **kw):
                    return fake_page

                def close(self):
                    pass

            fake_pw = mock.MagicMock()
            fake_pw.__enter__ = mock.Mock(return_value=fake_pw)
            fake_pw.__exit__ = mock.Mock(return_value=False)
            with mock.patch("playwright.sync_api.sync_playwright", return_value=fake_pw):
                with mock.patch.object(pipeline, "launch_chromium_browser", return_value=FakeBrowser()):
                    with mock.patch.object(pipeline, "write_report", return_value={}):
                        with mock.patch.object(
                            pipeline, "write_pending_user_input_for_unconfirmed_fields", return_value=None
                        ):
                            with mock.patch.object(pipeline, "PROJECT_ROOT", out_dir):
                                rc = pipeline.run_browser_pipeline(
                                    payload_path,
                                    headless=True,
                                    submit=False,
                                    board_name="jobvite",
                                    form_ready_selector="form",
                                    fill_step_fn=lambda page, step: None,
                                    page_snapshot_fn=lambda page: {},
                                    classify_state_fn=lambda snap: {"status": "pending"},
                                    click_submit_fn=lambda page: True,
                                    capture_fn=lambda page, path: None,
                                )

            self.assertEqual(rc, 0)
            self.assertEqual(fake_page.goto_url, "https://example.com/job/123/apply")

    def test_returns_zero_when_submit_false_after_filling(self):
        """Verify the pipeline fills steps, captures screenshot, and returns 0 when not submitting."""
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / "submit" / "gem_autofill_pages").mkdir()
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com",
                "out_dir": str(out_dir),
                "steps": [
                    {
                        "field_name": "email",
                        "kind": "text",
                        "value": "test@test.com",
                        "label": "Email",
                        "source": "resume",
                        "required": True,
                    }
                ],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(out_dir / "submit" / "report.json"),
                    "report_markdown": str(out_dir / "submit" / "report.md"),
                    "pre_submit_screenshot": str(out_dir / "submit" / "pre.png"),
                    "page_screenshots_dir": str(out_dir / "submit" / "gem_autofill_pages"),
                },
            }
            payload_path = out_dir / "submit" / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            fill_calls = []

            class FakePage:
                def goto(self, url, **kw):
                    pass

                def wait_for_selector(self, sel, **kw):
                    pass

                def wait_for_timeout(self, ms):
                    pass

                def screenshot(self, path, full_page=False):
                    pass

                def locator(self, sel):
                    class L:
                        first = self

                        def count(self):
                            return 0

                    return L()

            class FakeBrowser:
                session_viewer_url = None

                def new_page(self, **kw):
                    return FakePage()

                def close(self):
                    pass

            # Mock sync_playwright at the import location inside run_browser_pipeline
            fake_pw = mock.MagicMock()
            fake_pw.__enter__ = mock.Mock(return_value=fake_pw)
            fake_pw.__exit__ = mock.Mock(return_value=False)
            with mock.patch("playwright.sync_api.sync_playwright", return_value=fake_pw):
                with mock.patch.object(pipeline, "launch_chromium_browser", return_value=FakeBrowser()):
                    with mock.patch.object(pipeline, "write_report", return_value={}):
                        with mock.patch.object(
                            pipeline, "write_pending_user_input_for_unconfirmed_fields", return_value=None
                        ):
                            with mock.patch.object(pipeline, "PROJECT_ROOT", out_dir):
                                rc = pipeline.run_browser_pipeline(
                                    payload_path,
                                    headless=True,
                                    submit=False,
                                    board_name="gem",
                                    form_ready_selector=".form-33",
                                    fill_step_fn=lambda page, step: fill_calls.append(step),
                                    page_snapshot_fn=lambda page: {},
                                    classify_state_fn=lambda snap: {"status": "pending"},
                                    click_submit_fn=lambda page: True,
                                    capture_fn=lambda page, path: None,
                                )

            self.assertEqual(rc, 0)
            self.assertEqual(len(fill_calls), 1)

    def test_draft_mode_writes_skipped_captcha_result_when_challenge_active(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            (submit_dir / "lever_autofill_pages").mkdir()
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com",
                "out_dir": str(out_dir),
                "steps": [],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(submit_dir / "report.json"),
                    "report_markdown": str(submit_dir / "report.md"),
                    "pre_submit_screenshot": str(submit_dir / "pre.png"),
                    "page_screenshots_dir": str(submit_dir / "lever_autofill_pages"),
                },
            }
            payload_path = submit_dir / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            class FakePage:
                def goto(self, url, **kw):
                    pass

                def wait_for_selector(self, sel, **kw):
                    pass

                def wait_for_timeout(self, ms):
                    pass

                def screenshot(self, path, full_page=False):
                    pass

                def locator(self, sel):
                    class L:
                        first = self

                        def count(self):
                            return 0

                    return L()

            class FakeBrowser:
                session_viewer_url = None

                def new_page(self, **kw):
                    return FakePage()

                def close(self):
                    pass

            def fake_capture(page, path):
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_text("png", encoding="utf-8")

            fake_pw = mock.MagicMock()
            fake_pw.__enter__ = mock.Mock(return_value=fake_pw)
            fake_pw.__exit__ = mock.Mock(return_value=False)
            with mock.patch("playwright.sync_api.sync_playwright", return_value=fake_pw):
                with mock.patch.object(pipeline, "launch_chromium_browser", return_value=FakeBrowser()):
                    with mock.patch.object(pipeline, "write_report", return_value={}):
                        with mock.patch.object(
                            pipeline, "write_pending_user_input_for_unconfirmed_fields", return_value=None
                        ) as write_pending:
                            with mock.patch.object(pipeline, "PROJECT_ROOT", out_dir):
                                rc = pipeline.run_browser_pipeline(
                                    payload_path,
                                    headless=True,
                                    submit=False,
                                    board_name="lever",
                                    form_ready_selector="form",
                                    fill_step_fn=lambda page, step: None,
                                    page_snapshot_fn=lambda page: {
                                        "form_visible": True,
                                        "hcaptcha_visible": True,
                                        "hcaptcha_challenge_active": True,
                                    },
                                    classify_state_fn=lambda snap: {"status": "captcha_required"},
                                    click_submit_fn=lambda page: True,
                                    capture_fn=fake_capture,
                                )

            self.assertEqual(rc, 0)
            self.assertFalse(write_pending.called)
            result = json.loads((submit_dir / "application_submission_result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "skipped_captcha")
            self.assertEqual(result["failure_type"], "skipped_captcha")
            self.assertEqual(result["message"], "Submission skipped: captcha required. Moving on to next job.")
            self.assertTrue((submit_dir / "pre.png").exists())
            self.assertTrue((submit_dir / "lever_autofill_pages" / "page_01.png").exists())

    def test_writes_pending_user_input_for_live_required_and_boundary_blockers(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            (submit_dir / "jobvite_autofill_pages").mkdir()
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com",
                "out_dir": str(out_dir),
                "steps": [],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(submit_dir / "report.json"),
                    "report_markdown": str(submit_dir / "report.md"),
                    "pre_submit_screenshot": str(submit_dir / "pre.png"),
                    "submit_debug_html": str(submit_dir / "debug.html"),
                    "submit_debug_screenshot": str(submit_dir / "debug.png"),
                    "page_screenshots_dir": str(submit_dir / "jobvite_autofill_pages"),
                },
            }
            payload_path = submit_dir / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            class FakePage:
                def goto(self, url, **kw):
                    pass

                def wait_for_selector(self, sel, **kw):
                    pass

                def wait_for_timeout(self, ms):
                    pass

                def locator(self, sel):
                    class L:
                        first = self

                        def count(self):
                            return 0

                    return L()

            class FakeBrowser:
                session_viewer_url = None

                def new_page(self, **kw):
                    return FakePage()

                def close(self):
                    pass

            fake_pw = mock.MagicMock()
            fake_pw.__enter__ = mock.Mock(return_value=fake_pw)
            fake_pw.__exit__ = mock.Mock(return_value=False)
            with mock.patch("playwright.sync_api.sync_playwright", return_value=fake_pw):
                with mock.patch.object(pipeline, "launch_chromium_browser", return_value=FakeBrowser()):
                    with mock.patch.object(pipeline, "write_report", return_value={}):
                        with mock.patch.object(
                            pipeline,
                            "write_pending_user_input_for_unconfirmed_fields",
                            return_value=submit_dir / "pending_user_input.json",
                        ) as write_pending:
                            with mock.patch.object(pipeline, "PROJECT_ROOT", out_dir):
                                rc = pipeline.run_browser_pipeline(
                                    payload_path,
                                    headless=True,
                                    submit=False,
                                    board_name="jobvite",
                                    form_ready_selector="form",
                                    fill_step_fn=lambda page, step: None,
                                    page_snapshot_fn=lambda page: {},
                                    classify_state_fn=lambda snap: {"status": "unknown"},
                                    click_submit_fn=lambda page: True,
                                    capture_fn=lambda page, path: Path(path).write_text("png", encoding="utf-8"),
                                    live_required_field_scan_fn=lambda page, steps: [
                                        {
                                            "field_name": "graduation_month_year",
                                            "label": "Graduation Month and Year (MM/YYYY)",
                                            "kind": "text",
                                            "source": "live_application_form",
                                            "required": True,
                                            "status": "planned",
                                        }
                                    ],
                                    draft_boundary_scan_fn=lambda page: {
                                        "field_name": "final_review_boundary",
                                        "label": "Final review boundary",
                                        "kind": "state",
                                        "source": "simple_board_pipeline",
                                        "required": True,
                                        "status": "planned",
                                    },
                                )

            self.assertEqual(rc, 0)
            submitted_fields = write_pending.call_args.kwargs["fields"]
            self.assertEqual(
                [entry["field_name"] for entry in submitted_fields],
                ["graduation_month_year", "final_review_boundary"],
            )

    def test_writes_pending_user_input_for_required_unknown_questions(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            (submit_dir / "jazzhr_autofill_pages").mkdir()
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com",
                "out_dir": str(out_dir),
                "steps": [],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(submit_dir / "report.json"),
                    "report_markdown": str(submit_dir / "report.md"),
                    "pre_submit_screenshot": str(submit_dir / "pre.png"),
                    "submit_debug_html": str(submit_dir / "debug.html"),
                    "submit_debug_screenshot": str(submit_dir / "debug.png"),
                    "page_screenshots_dir": str(submit_dir / "jazzhr_autofill_pages"),
                },
            }
            payload_path = submit_dir / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            class FakePage:
                def goto(self, url, **kw):
                    pass

                def wait_for_selector(self, sel, **kw):
                    pass

                def wait_for_timeout(self, ms):
                    pass

                def locator(self, sel):
                    class L:
                        first = self

                        def count(self):
                            return 0

                    return L()

            class FakeBrowser:
                session_viewer_url = None

                def new_page(self, **kw):
                    return FakePage()

                def close(self):
                    pass

            fake_pw = mock.MagicMock()
            fake_pw.__enter__ = mock.Mock(return_value=fake_pw)
            fake_pw.__exit__ = mock.Mock(return_value=False)
            with mock.patch("playwright.sync_api.sync_playwright", return_value=fake_pw):
                with mock.patch.object(pipeline, "launch_chromium_browser", return_value=FakeBrowser()):
                    with mock.patch.object(
                        pipeline,
                        "write_report",
                        return_value={
                            "unknown_questions": [
                                {
                                    "field_name": "payments_experience",
                                    "label": "Payments experience",
                                    "kind": "select",
                                    "required": True,
                                    "status": "planned",
                                    "source": "live_application_form",
                                }
                            ]
                        },
                    ):
                        with mock.patch.object(
                            pipeline,
                            "write_pending_user_input_for_unconfirmed_fields",
                            return_value=submit_dir / "pending_user_input.json",
                        ) as write_pending:
                            with mock.patch.object(pipeline, "PROJECT_ROOT", out_dir):
                                rc = pipeline.run_browser_pipeline(
                                    payload_path,
                                    headless=True,
                                    submit=False,
                                    board_name="jazzhr",
                                    form_ready_selector="form",
                                    fill_step_fn=lambda page, step: None,
                                    page_snapshot_fn=lambda page: {},
                                    classify_state_fn=lambda snap: {"status": "unknown"},
                                    click_submit_fn=lambda page: True,
                                    capture_fn=lambda page, path: Path(path).write_text("png", encoding="utf-8"),
                                )

            self.assertEqual(rc, 0)
            submitted_fields = write_pending.call_args.kwargs["fields"]
            self.assertEqual([entry["field_name"] for entry in submitted_fields], ["payments_experience"])

    def test_retries_visible_self_id_once_and_filters_optional_non_blockers(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / "submit" / "gem_autofill_pages").mkdir()
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com",
                "out_dir": str(out_dir),
                "steps": [
                    {
                        "field_name": "race_ethnicity",
                        "kind": "radio",
                        "option": "Hispanic or Latino",
                        "label": "Race or Ethnicity",
                        "source": "application_profile.md",
                        "required": False,
                        "blocks_draft_completion": True,
                        "blocker_kind": "visible_self_id",
                    },
                    {
                        "field_name": "cover_letter",
                        "kind": "file",
                        "file_path": "/tmp/cover.pdf",
                        "label": "Cover Letter",
                        "source": "existing_cover_letter_asset",
                        "required": False,
                    },
                ],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(out_dir / "submit" / "report.json"),
                    "report_markdown": str(out_dir / "submit" / "report.md"),
                    "pre_submit_screenshot": str(out_dir / "submit" / "pre.png"),
                    "page_screenshots_dir": str(out_dir / "submit" / "gem_autofill_pages"),
                },
            }
            payload_path = out_dir / "submit" / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            fill_counts = {"race_ethnicity": 0, "cover_letter": 0}

            def fake_fill_step(_page, step):
                fill_counts[step["field_name"]] += 1
                if step["field_name"] == "race_ethnicity" and fill_counts["race_ethnicity"] >= 2:
                    step["filled"] = True

            class FakePage:
                def goto(self, url, **kw):
                    pass

                def wait_for_selector(self, sel, **kw):
                    pass

                def wait_for_timeout(self, ms):
                    pass

                def locator(self, sel):
                    class L:
                        first = self

                        def count(self):
                            return 0

                    return L()

            class FakeBrowser:
                session_viewer_url = None

                def new_page(self, **kw):
                    return FakePage()

                def close(self):
                    pass

            fake_pw = mock.MagicMock()
            fake_pw.__enter__ = mock.Mock(return_value=fake_pw)
            fake_pw.__exit__ = mock.Mock(return_value=False)
            with mock.patch("playwright.sync_api.sync_playwright", return_value=fake_pw):
                with mock.patch.object(pipeline, "launch_chromium_browser", return_value=FakeBrowser()):
                    with mock.patch.object(
                        pipeline,
                        "write_pending_user_input_for_unconfirmed_fields",
                        return_value=None,
                    ) as write_pending:
                        with mock.patch.object(pipeline, "PROJECT_ROOT", out_dir):
                            rc = pipeline.run_browser_pipeline(
                                payload_path,
                                headless=True,
                                submit=False,
                                board_name="gem",
                                retry_unconfirmed_visible_self_id_once=True,
                                form_ready_selector=".form-33",
                                fill_step_fn=fake_fill_step,
                                page_snapshot_fn=lambda page: {},
                                classify_state_fn=lambda snap: {"status": "pending"},
                                click_submit_fn=lambda page: True,
                                capture_fn=lambda page, path: Path(path).write_text("png", encoding="utf-8"),
                            )

            self.assertEqual(rc, 0)
            self.assertEqual(fill_counts["race_ethnicity"], 2)
            self.assertEqual(fill_counts["cover_letter"], 1)
            self.assertEqual(write_pending.call_args.kwargs["fields"], [])

    def test_retries_visible_profile_field_blocker_once(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / "submit" / "gem_autofill_pages").mkdir()
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com",
                "out_dir": str(out_dir),
                "steps": [
                    {
                        "field_name": "candidate_location",
                        "kind": "text",
                        "value": "San Francisco, CA",
                        "label": "Current Location",
                        "source": "application_profile.md",
                        "required": True,
                        "blocks_draft_completion": True,
                        "blocker_kind": "visible_profile_field",
                        "profile_field": "location",
                    }
                ],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(out_dir / "submit" / "report.json"),
                    "report_markdown": str(out_dir / "submit" / "report.md"),
                    "pre_submit_screenshot": str(out_dir / "submit" / "pre.png"),
                    "page_screenshots_dir": str(out_dir / "submit" / "gem_autofill_pages"),
                },
            }
            payload_path = out_dir / "submit" / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            fill_count = 0

            def fake_fill_step(_page, step):
                nonlocal fill_count
                fill_count += 1
                if fill_count >= 2:
                    step["filled"] = True

            class FakePage:
                def goto(self, url, **kw):
                    pass

                def wait_for_selector(self, sel, **kw):
                    pass

                def wait_for_timeout(self, ms):
                    pass

                def locator(self, sel):
                    class L:
                        first = self

                        def count(self):
                            return 0

                    return L()

            class FakeBrowser:
                session_viewer_url = None

                def new_page(self, **kw):
                    return FakePage()

                def close(self):
                    pass

            fake_pw = mock.MagicMock()
            fake_pw.__enter__ = mock.Mock(return_value=fake_pw)
            fake_pw.__exit__ = mock.Mock(return_value=False)
            with mock.patch("playwright.sync_api.sync_playwright", return_value=fake_pw):
                with mock.patch.object(pipeline, "launch_chromium_browser", return_value=FakeBrowser()):
                    with mock.patch.object(
                        pipeline,
                        "write_pending_user_input_for_unconfirmed_fields",
                        return_value=None,
                    ):
                        with mock.patch.object(pipeline, "PROJECT_ROOT", out_dir):
                            rc = pipeline.run_browser_pipeline(
                                payload_path,
                                headless=True,
                                submit=False,
                                board_name="gem",
                                retry_unconfirmed_visible_self_id_once=True,
                                form_ready_selector=".form-33",
                                fill_step_fn=fake_fill_step,
                                page_snapshot_fn=lambda page: {},
                                classify_state_fn=lambda snap: {"status": "pending"},
                                click_submit_fn=lambda page: True,
                                capture_fn=lambda page, path: Path(path).write_text("png", encoding="utf-8"),
                            )

            self.assertEqual(rc, 0)
            self.assertEqual(fill_count, 2)

    def test_successful_rerun_clears_stale_pending_user_input(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            (submit_dir / "gem_autofill_pages").mkdir()
            stale_pending = submit_dir / "pending_user_input.json"
            stale_pending.write_text('{"status":"pending_user_input"}', encoding="utf-8")
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com",
                "out_dir": str(out_dir),
                "steps": [],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(submit_dir / "report.json"),
                    "report_markdown": str(submit_dir / "report.md"),
                    "pre_submit_screenshot": str(submit_dir / "pre.png"),
                    "page_screenshots_dir": str(submit_dir / "gem_autofill_pages"),
                },
            }
            payload_path = submit_dir / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            class FakePage:
                def goto(self, url, **kw):
                    pass

                def wait_for_selector(self, sel, **kw):
                    pass

                def wait_for_timeout(self, ms):
                    pass

                def locator(self, sel):
                    class L:
                        first = self

                        def count(self):
                            return 0

                    return L()

            class FakeBrowser:
                session_viewer_url = None

                def new_page(self, **kw):
                    return FakePage()

                def close(self):
                    pass

            fake_pw = mock.MagicMock()
            fake_pw.__enter__ = mock.Mock(return_value=fake_pw)
            fake_pw.__exit__ = mock.Mock(return_value=False)
            with mock.patch("playwright.sync_api.sync_playwright", return_value=fake_pw):
                with mock.patch.object(pipeline, "launch_chromium_browser", return_value=FakeBrowser()):
                    with mock.patch.object(pipeline, "write_report", return_value={}):
                        with mock.patch.object(
                            pipeline,
                            "write_pending_user_input_for_unconfirmed_fields",
                            return_value=None,
                        ):
                            with mock.patch.object(pipeline, "PROJECT_ROOT", out_dir):
                                rc = pipeline.run_browser_pipeline(
                                    payload_path,
                                    headless=True,
                                    submit=False,
                                    board_name="gem",
                                    form_ready_selector=".form-33",
                                    fill_step_fn=lambda page, step: None,
                                    page_snapshot_fn=lambda page: {},
                                    classify_state_fn=lambda snap: {"status": "pending"},
                                    click_submit_fn=lambda page: True,
                                    capture_fn=lambda page, path: Path(path).write_text("png", encoding="utf-8"),
                                )

            self.assertEqual(rc, 0)
            self.assertFalse(stale_pending.exists())

    def test_missing_pre_submit_screenshot_becomes_artifact_blocker(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / "submit" / "gem_autofill_pages").mkdir()
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com",
                "out_dir": str(out_dir),
                "steps": [],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(out_dir / "submit" / "report.json"),
                    "report_markdown": str(out_dir / "submit" / "report.md"),
                    "pre_submit_screenshot": str(out_dir / "submit" / "pre.png"),
                    "page_screenshots_dir": str(out_dir / "submit" / "gem_autofill_pages"),
                },
            }
            payload_path = out_dir / "submit" / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            capture_calls = []

            class FakePage:
                def goto(self, url, **kw):
                    pass

                def wait_for_selector(self, sel, **kw):
                    pass

                def wait_for_timeout(self, ms):
                    pass

                def locator(self, sel):
                    class L:
                        first = self

                        def count(self):
                            return 0

                    return L()

            class FakeBrowser:
                session_viewer_url = None

                def new_page(self, **kw):
                    return FakePage()

                def close(self):
                    pass

            def fake_capture(_page, path):
                capture_calls.append(Path(path).name)
                if Path(path).name == "page_01.png":
                    Path(path).write_text("png", encoding="utf-8")

            fake_pw = mock.MagicMock()
            fake_pw.__enter__ = mock.Mock(return_value=fake_pw)
            fake_pw.__exit__ = mock.Mock(return_value=False)
            with mock.patch("playwright.sync_api.sync_playwright", return_value=fake_pw):
                with mock.patch.object(pipeline, "launch_chromium_browser", return_value=FakeBrowser()):
                    with mock.patch.object(pipeline, "write_report", return_value={}) as write_report:
                        with mock.patch.object(
                            pipeline,
                            "write_pending_user_input_for_unconfirmed_fields",
                            return_value=None,
                        ) as write_pending:
                            with mock.patch.object(pipeline, "PROJECT_ROOT", out_dir):
                                rc = pipeline.run_browser_pipeline(
                                    payload_path,
                                    headless=True,
                                    submit=False,
                                    board_name="gem",
                                    form_ready_selector=".form-33",
                                    fill_step_fn=lambda page, step: None,
                                    page_snapshot_fn=lambda page: {},
                                    classify_state_fn=lambda snap: {"status": "pending"},
                                    click_submit_fn=lambda page: True,
                                    capture_fn=fake_capture,
                                )

            self.assertEqual(rc, 0)
            self.assertEqual(capture_calls.count("pre.png"), 2)
            self.assertEqual(capture_calls.count("page_01.png"), 1)
            self.assertEqual(write_report.call_count, 1)
            fields = write_pending.call_args.kwargs["fields"]
            self.assertEqual(len(fields), 1)
            self.assertEqual(fields[0]["artifact_key"], "pre_submit_screenshot")
            self.assertEqual(fields[0]["blocker_kind"], "required_artifact")

    def test_post_navigate_submission_result_short_circuits_before_fill_loop(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            (submit_dir / "gem_autofill_pages").mkdir()
            stale_pending = submit_dir / "pending_user_input.json"
            stale_pending.write_text('{"status":"pending_user_input"}', encoding="utf-8")
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://example.com",
                "out_dir": str(out_dir),
                "steps": [
                    {
                        "field_name": "email",
                        "kind": "text",
                        "value": "test@test.com",
                        "label": "Email",
                        "source": "master_resume.md",
                        "required": True,
                    }
                ],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(submit_dir / "report.json"),
                    "report_markdown": str(submit_dir / "report.md"),
                    "pre_submit_screenshot": str(submit_dir / "pre.png"),
                    "page_screenshots_dir": str(submit_dir / "gem_autofill_pages"),
                },
            }
            payload_path = submit_dir / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            result_path = submit_dir / "application_submission_result.json"
            fill_calls = []

            class FakePage:
                def goto(self, url, **kw):
                    pass

                def wait_for_selector(self, sel, **kw):
                    pass

                def wait_for_timeout(self, ms):
                    pass

                def locator(self, sel):
                    class L:
                        first = self

                        def count(self):
                            return 0

                    return L()

            class FakeBrowser:
                session_viewer_url = None

                def new_page(self, **kw):
                    return FakePage()

                def close(self):
                    pass

            def fake_post_navigate(_page):
                result_path.write_text(
                    json.dumps(
                        {
                            "status": "skipped_auth",
                            "board": "gem",
                            "failure_type": "auth_guarded",
                            "message": "Authentication required before the application form becomes available.",
                        }
                    ),
                    encoding="utf-8",
                )

            fake_pw = mock.MagicMock()
            fake_pw.__enter__ = mock.Mock(return_value=fake_pw)
            fake_pw.__exit__ = mock.Mock(return_value=False)
            with mock.patch("playwright.sync_api.sync_playwright", return_value=fake_pw):
                with mock.patch.object(pipeline, "launch_chromium_browser", return_value=FakeBrowser()):
                    with mock.patch.object(pipeline, "write_report") as write_report:
                        with mock.patch.object(
                            pipeline,
                            "write_pending_user_input_for_unconfirmed_fields",
                        ) as write_pending:
                            with mock.patch.object(pipeline, "PROJECT_ROOT", out_dir):
                                rc = pipeline.run_browser_pipeline(
                                    payload_path,
                                    headless=True,
                                    submit=False,
                                    board_name="gem",
                                    form_ready_selector=".form-33",
                                    fill_step_fn=lambda page, step: fill_calls.append(step),
                                    page_snapshot_fn=lambda page: {},
                                    classify_state_fn=lambda snap: {"status": "pending"},
                                    click_submit_fn=lambda page: True,
                                    capture_fn=lambda page, path: Path(path).write_text("png", encoding="utf-8"),
                                    post_navigate_hook=fake_post_navigate,
                                )

            self.assertEqual(rc, 0)
            self.assertEqual(fill_calls, [])
            self.assertFalse(write_report.called)
            self.assertFalse(write_pending.called)
            self.assertTrue((submit_dir / "pre.png").exists())
            self.assertTrue((submit_dir / "gem_autofill_pages" / "page_01.png").exists())
            self.assertFalse(stale_pending.exists())

    def test_pre_form_timeout_returns_captcha_skip_for_access_restricted_interstitial(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            (submit_dir / "gem_autofill_pages").mkdir()
            payload = {
                "job_title": "PM",
                "company": "Acme",
                "job_url": "https://jobs.smartrecruiters.com/example/1",
                "out_dir": str(out_dir),
                "steps": [],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(submit_dir / "report.json"),
                    "report_markdown": str(submit_dir / "report.md"),
                    "pre_submit_screenshot": str(submit_dir / "pre.png"),
                    "submit_debug_html": str(submit_dir / "debug.html"),
                    "submit_debug_screenshot": str(submit_dir / "debug.png"),
                    "page_screenshots_dir": str(submit_dir / "gem_autofill_pages"),
                },
            }
            payload_path = submit_dir / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            class FakeBodyLocator:
                first = None

                def count(self):
                    return 1

                def inner_text(self, timeout=None):
                    return (
                        "Access is temporarily restricted\n"
                        "We detected unusual activity from your device or network."
                    )

            class FakePage:
                url = "https://jobs.smartrecruiters.com/oneclick-ui/company/Intuitive/publication/abc"

                def goto(self, url, **kw):
                    self.url = url

                def wait_for_selector(self, sel, **kw):
                    raise PlaywrightTimeoutError("timed out")

                def wait_for_timeout(self, ms):
                    pass

                def content(self):
                    return (
                        "<html><body>Access is temporarily restricted"
                        "<iframe src='https://geo.captcha-delivery.com/interstitial/'></iframe>"
                        "</body></html>"
                    )

                def locator(self, sel):
                    if sel == "body":
                        return FakeBodyLocator()

                    class EmptyLocator:
                        first = None

                        def count(self):
                            return 0

                    return EmptyLocator()

            class FakeBrowser:
                session_viewer_url = None

                def new_page(self, **kw):
                    return FakePage()

                def close(self):
                    pass

            fake_pw = mock.MagicMock()
            fake_pw.__enter__ = mock.Mock(return_value=fake_pw)
            fake_pw.__exit__ = mock.Mock(return_value=False)
            with mock.patch("playwright.sync_api.sync_playwright", return_value=fake_pw):
                with mock.patch.object(pipeline, "launch_chromium_browser", return_value=FakeBrowser()):
                    with mock.patch.object(pipeline, "PROJECT_ROOT", out_dir):
                        rc = pipeline.run_browser_pipeline(
                            payload_path,
                            headless=True,
                            submit=False,
                            board_name="gem",
                            form_ready_selector=".form-33",
                            fill_step_fn=lambda page, step: None,
                            page_snapshot_fn=lambda page: {},
                            classify_state_fn=lambda snap: {"status": "pending"},
                            click_submit_fn=lambda page: True,
                            capture_fn=lambda page, path: Path(path).write_text("png", encoding="utf-8"),
                        )

            self.assertEqual(rc, pipeline.CAPTCHA_SKIP_EXIT_CODE)
            self.assertTrue((submit_dir / "debug.html").exists())
            self.assertTrue((submit_dir / "debug.png").exists())

    def test_pre_form_timeout_records_service_unavailable_for_cloudfront_error_page(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            (submit_dir / "comeet_autofill_pages").mkdir()
            payload = {
                "job_title": "Open Opportunities",
                "company": "Cellebrite",
                "job_url": "https://cellebrite.com/en/about/careers/positions/?comeet_cat=us-dc&comeet_pos=2B.36C",
                "out_dir": str(out_dir),
                "steps": [],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(submit_dir / "report.json"),
                    "report_markdown": str(submit_dir / "report.md"),
                    "pre_submit_screenshot": str(submit_dir / "pre.png"),
                    "submit_debug_html": str(submit_dir / "debug.html"),
                    "submit_debug_screenshot": str(submit_dir / "debug.png"),
                    "page_screenshots_dir": str(submit_dir / "comeet_autofill_pages"),
                },
            }
            payload_path = submit_dir / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            class FakeBodyLocator:
                first = None

                def count(self):
                    return 1

                def inner_text(self, timeout=None):
                    return (
                        "403 ERROR\n"
                        "The request could not be satisfied.\n"
                        "Request blocked. We can't connect to the server for this app or website at this time."
                    )

            class FakePage:
                url = "https://cellebrite.com/en/about/careers/positions/?comeet_cat=us-dc&comeet_pos=2B.36C"

                def goto(self, url, **kw):
                    self.url = url

                def wait_for_selector(self, sel, **kw):
                    raise PlaywrightTimeoutError("timed out")

                def wait_for_timeout(self, ms):
                    pass

                def content(self):
                    return (
                        "<html><body><h1>403 ERROR</h1>"
                        "<h2>The request could not be satisfied.</h2>"
                        "<p>Request blocked.</p>"
                        "<p>Generated by cloudfront (CloudFront)</p>"
                        "</body></html>"
                    )

                def locator(self, sel):
                    if sel == "body":
                        return FakeBodyLocator()

                    class EmptyLocator:
                        first = None

                        def count(self):
                            return 0

                    return EmptyLocator()

            class FakeBrowser:
                session_viewer_url = None

                def new_page(self, **kw):
                    return FakePage()

                def close(self):
                    pass

            fake_pw = mock.MagicMock()
            fake_pw.__enter__ = mock.Mock(return_value=fake_pw)
            fake_pw.__exit__ = mock.Mock(return_value=False)
            with mock.patch("playwright.sync_api.sync_playwright", return_value=fake_pw):
                with mock.patch.object(pipeline, "launch_chromium_browser", return_value=FakeBrowser()):
                    with mock.patch.object(pipeline, "PROJECT_ROOT", out_dir):
                        rc = pipeline.run_browser_pipeline(
                            payload_path,
                            headless=True,
                            submit=False,
                            board_name="comeet",
                            form_ready_selector=".comeet-apply-form",
                            fill_step_fn=lambda page, step: None,
                            page_snapshot_fn=lambda page: {},
                            classify_state_fn=lambda snap: {"status": "pending"},
                            click_submit_fn=lambda page: True,
                            capture_fn=lambda page, path: Path(path).write_text("png", encoding="utf-8"),
                        )

            result_path = submit_dir / "application_submission_result.json"
            self.assertEqual(rc, 1)
            self.assertTrue(result_path.exists())
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "service_unavailable")
            self.assertEqual(result["failure_type"], "service_unavailable")
            self.assertTrue((submit_dir / "debug.html").exists())
            self.assertTrue((submit_dir / "debug.png").exists())

    def test_pre_form_timeout_records_job_closed_for_missing_posting_page(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            (submit_dir / "ashby_autofill_pages").mkdir()
            payload = {
                "job_title": "Staff Product Manager, Vault",
                "company": "Harvey",
                "job_url": "https://jobs.ashbyhq.com/harvey/example/application",
                "out_dir": str(out_dir),
                "steps": [],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(submit_dir / "report.json"),
                    "report_markdown": str(submit_dir / "report.md"),
                    "pre_submit_screenshot": str(submit_dir / "pre.png"),
                    "submit_debug_html": str(submit_dir / "debug.html"),
                    "submit_debug_screenshot": str(submit_dir / "debug.png"),
                    "page_screenshots_dir": str(submit_dir / "ashby_autofill_pages"),
                },
            }
            payload_path = submit_dir / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            class FakeBodyLocator:
                first = None

                def count(self):
                    return 1

                def inner_text(self, timeout=None):
                    return "Job not found\n\nThe job you requested was not found.\n\nView all open positions"

            class FakePage:
                url = "https://jobs.ashbyhq.com/harvey/example/application"

                def goto(self, url, **kw):
                    self.url = url

                def wait_for_selector(self, sel, **kw):
                    raise PlaywrightTimeoutError("timed out")

                def wait_for_timeout(self, ms):
                    pass

                def content(self):
                    return (
                        "<html><body><h1>Job not found</h1>"
                        "<p>The job you requested was not found.</p>"
                        "<p>View all open positions</p>"
                        "<script>window.__appData = {\"organization\": null, \"posting\": null, \"jobBoard\": null};</script>"
                        "</body></html>"
                    )

                def locator(self, sel):
                    if sel == "body":
                        return FakeBodyLocator()

                    class EmptyLocator:
                        first = None

                        def count(self):
                            return 0

                    return EmptyLocator()

            class FakeBrowser:
                session_viewer_url = None

                def new_page(self, **kw):
                    return FakePage()

                def close(self):
                    pass

            fake_pw = mock.MagicMock()
            fake_pw.__enter__ = mock.Mock(return_value=fake_pw)
            fake_pw.__exit__ = mock.Mock(return_value=False)
            with mock.patch("playwright.sync_api.sync_playwright", return_value=fake_pw):
                with mock.patch.object(pipeline, "launch_chromium_browser", return_value=FakeBrowser()):
                    with mock.patch.object(pipeline, "PROJECT_ROOT", out_dir):
                        rc = pipeline.run_browser_pipeline(
                            payload_path,
                            headless=True,
                            submit=False,
                            board_name="ashby",
                            form_ready_selector=".ashby-application-form-field-entry",
                            fill_step_fn=lambda page, step: None,
                            page_snapshot_fn=lambda page: {},
                            classify_state_fn=lambda snap: {"status": "pending"},
                            click_submit_fn=lambda page: True,
                            capture_fn=lambda page, path: Path(path).write_text("png", encoding="utf-8"),
                        )

            result_path = submit_dir / "application_submission_result.json"
            self.assertEqual(rc, 1)
            self.assertTrue(result_path.exists())
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "job_closed")
            self.assertEqual(result["failure_type"], "job_closed")
            self.assertIn("job_closed:", result["message"])
            self.assertTrue((submit_dir / "debug.html").exists())
            self.assertTrue((submit_dir / "debug.png").exists())

    def test_pre_form_job_closed_scans_iframe_text_for_inactive_posting(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        class FakeBodyLocator:
            def __init__(self, text):
                self._text = text

            def count(self):
                return 1

            def inner_text(self, timeout=None):
                del timeout
                return self._text

        class FakeFrame:
            def locator(self, selector):
                assert selector == "body"
                return FakeBodyLocator("Sorry, this job is no longer active. You can view our main careers page.")

            def content(self):
                return "<html><body>Sorry, this job is no longer active.</body></html>"

        class FakePage:
            frames = [FakeFrame()]

            def locator(self, selector):
                assert selector == "body"
                return FakeBodyLocator("Open Positions at Fortress")

            def content(self):
                return "<html><body><h1>Open Positions at Fortress</h1><iframe></iframe></body></html>"

        result = pipeline._pre_form_job_closed(FakePage())

        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("job_closed:", result["message"])
        self.assertIn("no longer active", result["message"])

    def test_pre_form_job_closed_detects_filled_position_shell(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        class FakeBodyLocator:
            def count(self):
                return 1

            def inner_text(self, timeout=None):
                del timeout
                return (
                    "Search Jobs View Profile Staff Product Manager (28205) "
                    "Sorry, this position has been filled."
                )

        class FakePage:
            frames = []

            def locator(self, selector):
                assert selector == "body"
                return FakeBodyLocator()

            def content(self):
                return "<html><body>Sorry, this position has been filled.</body></html>"

        result = pipeline._pre_form_job_closed(FakePage())

        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("job_closed:", result["message"])
        self.assertIn("filled", result["message"])

    def test_post_navigation_job_closed_preempts_pending_user_input_for_iframe_wrappers(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            (submit_dir / "paycor_autofill_pages").mkdir()
            payload = {
                "job_title": "Sign up for our",
                "company": "Fortress Information Security",
                "job_url": "https://recruitingbypaycor.com/career/JobIntroduction.action?id=example",
                "out_dir": str(out_dir),
                "provider": "openai",
                "steps": [
                    {
                        "field_name": "resume",
                        "label": "Resume",
                        "kind": "file",
                        "required": True,
                        "source": "existing_resume_asset",
                        "file_path": str(out_dir / "resume.pdf"),
                    }
                ],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(submit_dir / "report.json"),
                    "report_markdown": str(submit_dir / "report.md"),
                    "pre_submit_screenshot": str(submit_dir / "pre.png"),
                    "submit_debug_html": str(submit_dir / "debug.html"),
                    "submit_debug_screenshot": str(submit_dir / "debug.png"),
                    "page_screenshots_dir": str(submit_dir / "paycor_autofill_pages"),
                },
            }
            Path(payload["steps"][0]["file_path"]).write_bytes(b"%PDF-fake")
            payload_path = submit_dir / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            class FakeBodyLocator:
                def __init__(self, text):
                    self._text = text
                    self.first = self

                def count(self):
                    return 1

                def inner_text(self, timeout=None):
                    del timeout
                    return self._text

            class EmptyLocator:
                first = None

                def count(self):
                    return 0

            class FakeFrame:
                def locator(self, selector):
                    assert selector == "body"
                    return FakeBodyLocator("Sorry, this job is no longer active. Click here to view careers.")

                def content(self):
                    return "<html><body>Sorry, this job is no longer active.</body></html>"

            class FakePage:
                def __init__(self):
                    self.url = "https://www.fortressinfosec.com/careers/jobs-listing?gnk=job&gni=example"
                    self.frames = [FakeFrame()]

                def goto(self, url, **kw):
                    del url, kw

                def wait_for_selector(self, sel, **kw):
                    del sel, kw

                def wait_for_timeout(self, ms):
                    del ms

                def content(self):
                    return "<html><body><h1>Open Positions at Fortress</h1><iframe></iframe></body></html>"

                def locator(self, selector):
                    if selector == "body":
                        return FakeBodyLocator("Open Positions at Fortress")
                    return EmptyLocator()

            class FakeBrowser:
                session_viewer_url = None

                def new_page(self, **kw):
                    del kw
                    return FakePage()

                def close(self):
                    pass

            fake_pw = mock.MagicMock()
            fake_pw.__enter__ = mock.Mock(return_value=fake_pw)
            fake_pw.__exit__ = mock.Mock(return_value=False)
            with mock.patch("playwright.sync_api.sync_playwright", return_value=fake_pw):
                with mock.patch.object(pipeline, "launch_chromium_browser", return_value=FakeBrowser()):
                    with mock.patch.object(
                        pipeline,
                        "write_pending_user_input_for_unconfirmed_fields",
                        return_value=submit_dir / "pending_user_input.json",
                    ) as write_pending:
                        with mock.patch.object(pipeline, "PROJECT_ROOT", out_dir):
                            rc = pipeline.run_browser_pipeline(
                                payload_path,
                                headless=True,
                                submit=False,
                                board_name="paycor",
                                form_ready_selector="body",
                                fill_step_fn=lambda page, step: None,
                                page_snapshot_fn=lambda page: {"page_text": "", "url": page.url},
                                classify_state_fn=lambda snap: {"status": "review"},
                                click_submit_fn=lambda page: True,
                                capture_fn=lambda page, path: Path(path).write_text("png", encoding="utf-8"),
                            )

            result_path = submit_dir / "application_submission_result.json"
            self.assertEqual(rc, 1)
            self.assertTrue(result_path.exists())
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "job_closed")
            self.assertEqual(result["failure_type"], "job_closed")
            self.assertEqual(result["board"], "paycor")
            self.assertEqual(result["provider"], "openai")
            self.assertIn("job_closed:", result["message"])
            self.assertFalse(write_pending.called)
            self.assertTrue((submit_dir / "debug.html").exists())
            self.assertTrue((submit_dir / "debug.png").exists())

    def test_pre_submit_hook_can_signal_submit_already_clicked(self):
        pipeline = load_module("autofill_pipeline", "scripts/autofill_pipeline.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            (submit_dir / "gem_autofill_pages").mkdir()
            payload = {
                "job_title": "Senior Product Manager",
                "company": "DexCare",
                "job_url": "https://jobs.example.com/dexcare",
                "out_dir": str(out_dir),
                "steps": [],
                "unknown_questions": [],
                "artifacts": {
                    "report_json": str(submit_dir / "report.json"),
                    "report_markdown": str(submit_dir / "report.md"),
                    "pre_submit_screenshot": str(submit_dir / "pre.png"),
                    "post_submit_screenshot": str(submit_dir / "post.png"),
                    "submit_debug_html": str(submit_dir / "debug.html"),
                    "submit_debug_screenshot": str(submit_dir / "debug.png"),
                    "page_screenshots_dir": str(submit_dir / "gem_autofill_pages"),
                },
            }
            payload_path = submit_dir / "payload.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            class FakePage:
                def goto(self, url, **kw):
                    pass

                def wait_for_selector(self, sel, **kw):
                    pass

                def wait_for_timeout(self, ms):
                    pass

                def evaluate(self, script):
                    return None

                def locator(self, sel):
                    class L:
                        first = self

                        def count(self):
                            return 0

                    return L()

            class FakeBrowser:
                session_viewer_url = None

                def new_page(self, **kw):
                    return FakePage()

                def close(self):
                    pass

            class FakeWatcher:
                def __init__(self):
                    self.poll_calls = 0

                def poll(self, force=False):
                    self.poll_calls += 1
                    return {
                        "thread_id": "19d4ed3d800b335f",
                        "subject": "DexCare - Application Received - Senior Product Manager",
                    }

            watcher = FakeWatcher()
            fake_pw = mock.MagicMock()
            fake_pw.__enter__ = mock.Mock(return_value=fake_pw)
            fake_pw.__exit__ = mock.Mock(return_value=False)

            def fail_if_clicked(_page):
                raise AssertionError("submit button should not be clicked twice")

            with mock.patch("playwright.sync_api.sync_playwright", return_value=fake_pw):
                with mock.patch.object(pipeline, "launch_chromium_browser", return_value=FakeBrowser()):
                    with mock.patch.object(pipeline, "write_report", return_value={}):
                        with mock.patch.object(
                            pipeline,
                            "write_pending_user_input_for_unconfirmed_fields",
                            return_value=None,
                        ):
                            with mock.patch.object(
                                pipeline,
                                "build_email_confirmation_watcher",
                                return_value=watcher,
                            ) as build_watcher:
                                with mock.patch.object(pipeline, "sync_notion_after_submit") as sync_notion:
                                    with mock.patch.object(
                                        pipeline,
                                        "reply_to_confirmation_email",
                                    ) as reply_email:
                                        with mock.patch.object(pipeline, "PROJECT_ROOT", out_dir):
                                            rc = pipeline.run_browser_pipeline(
                                                payload_path,
                                                headless=True,
                                                submit=True,
                                                board_name="gem",
                                                form_ready_selector=".form-33",
                                                fill_step_fn=lambda page, step: None,
                                                page_snapshot_fn=lambda page: {},
                                                classify_state_fn=lambda snap: {"status": "pending"},
                                                click_submit_fn=fail_if_clicked,
                                                capture_fn=lambda page, path: Path(path).write_text(
                                                    "png", encoding="utf-8"
                                                ),
                                                pre_submit_hook=lambda page: True,
                                                confirmed_outcome_from_email_fn=lambda snap, email: {
                                                    "status": "confirmed",
                                                    "reason": "email_confirmation",
                                                    "email_confirmation": email,
                                                },
                                            )

            self.assertEqual(rc, 0)
            self.assertEqual(watcher.poll_calls, 1)
            build_watcher.assert_called_once()
            self.assertIsNotNone(build_watcher.call_args.kwargs["min_received_at_utc"])
            sync_notion.assert_called_once()
            reply_email.assert_called_once()


if __name__ == "__main__":
    unittest.main()
