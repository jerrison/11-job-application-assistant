import importlib.util
import os
import sys
import types
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


class BrowserRuntimeTests(unittest.TestCase):
    def test_aerospace_target_workspace_defaults_to_q_on_macos(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        with (
            mock.patch.object(browser_runtime.sys, "platform", "darwin"),
            mock.patch.object(browser_runtime.shutil, "which", return_value=None),
        ):
            self.assertEqual(browser_runtime._aerospace_target_workspace(environ={}), "Q")

    def test_submit_browser_profile_dir_uses_default_home_path(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")
        path = browser_runtime.submit_browser_profile_dir(environ={})
        self.assertEqual(
            path,
            Path.home() / ".job-assets" / "playwright-submit-profile",
        )

    def test_submit_browser_profile_dir_uses_app_home_browser_root_when_configured(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")
        path = browser_runtime.submit_browser_profile_dir(environ={"JOB_ASSETS_APP_HOME": "/tmp/job-assets-home"})
        self.assertEqual(path, Path("/tmp/job-assets-home/.job-assets/playwright-submit-profile"))

    def test_submit_browser_profile_dir_honors_env_override(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")
        path = browser_runtime.submit_browser_profile_dir(
            environ={"JOB_ASSETS_SUBMIT_BROWSER_PROFILE_DIR": "~/tmp/job-assets-profile"}
        )
        self.assertEqual(path, Path.home() / "tmp" / "job-assets-profile")

    def test_submit_slow_mo_defaults_to_human_pacing(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")
        self.assertEqual(browser_runtime.submit_slow_mo_ms(False, environ={}), 125)
        self.assertEqual(browser_runtime.submit_slow_mo_ms(True, environ={}), 0)
        self.assertEqual(browser_runtime.submit_type_delay_ms(environ={}), 45)
        self.assertEqual(browser_runtime.submit_viewport(environ={}), {"width": 1360, "height": 900})

    def test_human_fill_falls_back_to_programmatic_focus_when_click_is_blocked(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        class FakeLocator:
            def __init__(self):
                self.click_attempts = 0
                self.click_timeouts = []
                self.evaluated = []
                self.filled = []
                self.typed = []

            def click(self, timeout=None):
                self.click_attempts += 1
                self.click_timeouts.append(timeout)
                raise RuntimeError("iframe intercepts pointer events")

            def scroll_into_view_if_needed(self):
                self.scrolled = True

            def evaluate(self, script):
                self.evaluated.append(script)

            def fill(self, value):
                self.filled.append(value)

            def press_sequentially(self, value, delay):
                self.typed.append((value, delay))

        locator = FakeLocator()
        browser_runtime.human_fill(locator, "WeRide", delay_ms=7)

        self.assertEqual(locator.click_attempts, 1)
        self.assertEqual(locator.click_timeouts, [browser_runtime.DEFAULT_HUMAN_FILL_CLICK_TIMEOUT_MS])
        self.assertTrue(locator.evaluated)
        self.assertEqual(locator.filled, [""])
        self.assertEqual(locator.typed, [("WeRide", 7)])

    def test_human_fill_uses_direct_fill_for_long_values(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        class FakeLocator:
            def __init__(self):
                self.click_timeouts = []
                self.filled = []
                self.typed = []

            def click(self, timeout=None):
                self.click_timeouts.append(timeout)

            def fill(self, value):
                self.filled.append(value)

            def press_sequentially(self, value, delay):
                self.typed.append((value, delay))

        locator = FakeLocator()
        long_value = "x" * (browser_runtime.DEFAULT_HUMAN_FILL_DIRECT_FILL_THRESHOLD + 1)

        browser_runtime.human_fill(locator, long_value, delay_ms=9)

        self.assertEqual(locator.click_timeouts, [browser_runtime.DEFAULT_HUMAN_FILL_CLICK_TIMEOUT_MS])
        self.assertEqual(locator.filled, ["", long_value])
        self.assertEqual(locator.typed, [])

    def test_normalize_chromium_profile_zoom_removes_matching_host_entries(self):
        import json
        import tempfile

        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp)
            prefs_path = profile_dir / "Default" / "Preferences"
            prefs_path.parent.mkdir(parents=True, exist_ok=True)
            prefs_path.write_text(
                json.dumps(
                    {
                        "partition": {
                            "per_host_zoom_levels": {
                                "x": {
                                    "www.linkedin.com": {"zoom_level": -6.0},
                                    "www.google.com": {"zoom_level": -1.0},
                                }
                            }
                        },
                        "profile": {"default_zoom_level": -2.0},
                    }
                )
            )

            changed = browser_runtime.normalize_chromium_profile_zoom(
                profile_dir,
                hosts=("linkedin.com", "www.linkedin.com"),
                reset_default_zoom=True,
            )

            self.assertTrue(changed)
            updated = json.loads(prefs_path.read_text())
            self.assertEqual(
                updated["partition"]["per_host_zoom_levels"]["x"],
                {"www.google.com": {"zoom_level": -1.0}},
            )
            self.assertEqual(updated["profile"]["default_zoom_level"], 0.0)

    def test_steel_provider_defaults_and_overrides(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")
        self.assertEqual(browser_runtime.submit_browser_provider(environ={}), "local")
        self.assertEqual(
            browser_runtime.submit_browser_provider(environ={"JOB_ASSETS_BROWSER_PROVIDER": "steel"}),
            "steel",
        )
        self.assertEqual(browser_runtime.steel_base_url(environ={}), "https://api.steel.dev")
        self.assertEqual(
            browser_runtime.steel_base_url(environ={"STEEL_LOCAL": "true"}),
            "http://localhost:3000",
        )
        self.assertTrue(browser_runtime.steel_use_proxy(environ={}))
        self.assertFalse(browser_runtime.steel_use_proxy(environ={"STEEL_LOCAL": "true"}))
        self.assertTrue(browser_runtime.steel_solve_captcha(environ={}))
        self.assertEqual(
            browser_runtime.steel_cdp_url("wss://connect.steel.dev?sessionId=abc", api_key="secret"),
            "wss://connect.steel.dev?sessionId=abc&apiKey=secret",
        )

    def test_browser_launch_attempts_prefer_local_browser_before_bundled(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")
        attempts = browser_runtime._browser_launch_attempts(
            prefer_local_browser=True,
            environ={},
            existing_paths={"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"},
        )
        labels = [label for label, _ in attempts]
        self.assertEqual(labels[0], "local browser channel chrome")
        self.assertIn("bundled Chromium", labels[-1])

    def test_browser_launch_attempts_skip_duplicate_local_chrome_executable_after_channel(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")
        attempts = browser_runtime._browser_launch_attempts(
            prefer_local_browser=True,
            environ={},
            existing_paths={
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            },
        )
        labels = [label for label, _ in attempts]
        self.assertEqual(
            labels,
            [
                "local browser channel chrome",
                "common browser executable Microsoft Edge",
                "bundled Chromium",
            ],
        )

    def test_browser_launch_attempts_skip_duplicate_env_executable_when_same_browser_channel_present(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")
        attempts = browser_runtime._browser_launch_attempts(
            channel_env_var="TEST_BROWSER_CHANNEL",
            executable_env_var="TEST_BROWSER_EXECUTABLE",
            prefer_local_browser=False,
            environ={
                "TEST_BROWSER_CHANNEL": "chrome",
                "TEST_BROWSER_EXECUTABLE": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            },
        )
        labels = [label for label, _ in attempts]
        self.assertEqual(
            labels,
            [
                "executable path from TEST_BROWSER_EXECUTABLE",
                "bundled Chromium",
            ],
        )

    def test_browser_launch_env_skips_home_override_on_macos(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        with mock.patch.object(browser_runtime.sys, "platform", "darwin"):
            env = browser_runtime._browser_launch_env(
                str(Path.home() / ".job-assets" / "playwright-submit-profile"),
                headless=False,
            )

        self.assertIsNone(env)

    def test_browser_launch_env_uses_isolated_home_on_headless_macos(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")
        profile_dir = Path.home() / ".job-assets" / "playwright-submit-profile"

        with mock.patch.object(browser_runtime.sys, "platform", "darwin"):
            env = browser_runtime._browser_launch_env(str(profile_dir), headless=True)

        assert env is not None
        self.assertEqual(env["HOME"], str(profile_dir.parent / "playwright-submit-profile-home"))

    def test_browser_launch_env_redirects_home_off_macos(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")
        profile_dir = Path.home() / ".job-assets" / "playwright-submit-profile"

        with mock.patch.object(browser_runtime.sys, "platform", "linux"):
            env = browser_runtime._browser_launch_env(str(profile_dir), headless=False)

        assert env is not None
        self.assertEqual(env["HOME"], str(profile_dir.parent / "playwright-submit-profile-home"))
        self.assertEqual(
            env["XDG_CONFIG_HOME"],
            str(profile_dir.parent / "playwright-submit-profile-home" / ".config"),
        )

    def test_macos_headed_browser_attempt_block_reason_only_blocks_bundled_testing_browser(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")
        profile_dir = Path.home() / ".job-assets" / "playwright-submit-profile"

        self.assertIsNotNone(
            browser_runtime._macos_headed_browser_attempt_block_reason(
                "bundled Chromium",
                {},
                headless=False,
                persistent_profile_dir=str(profile_dir),
                platform="darwin",
            )
        )
        self.assertIsNotNone(
            browser_runtime._macos_headed_browser_attempt_block_reason(
                "env executable",
                {"executable_path": "/tmp/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"},
                headless=False,
                persistent_profile_dir=str(profile_dir),
                platform="darwin",
            )
        )
        self.assertIsNone(
            browser_runtime._macos_headed_browser_attempt_block_reason(
                "common browser executable Google Chrome",
                {"executable_path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"},
                headless=False,
                persistent_profile_dir=str(profile_dir),
                platform="darwin",
            )
        )
        self.assertIsNone(
            browser_runtime._macos_headed_browser_attempt_block_reason(
                "bundled Chromium",
                {},
                headless=True,
                persistent_profile_dir=str(profile_dir),
                platform="darwin",
            )
        )

    def test_launch_chromium_browser_uses_steel_session_when_configured(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        class FakePage:
            def __init__(self):
                self.viewport = None
                self.url = "about:blank"

            def set_viewport_size(self, viewport):
                self.viewport = viewport

        class FakeContext:
            def __init__(self):
                self.pages = []
                self.created_pages = []

            def new_page(self):
                page = FakePage()
                self.created_pages.append(page)
                return page

        class FakeBrowser:
            def __init__(self):
                self.contexts = [FakeContext()]
                self.closed = False

            def close(self):
                self.closed = True

        class FakeChromium:
            def __init__(self):
                self.connected_url = None
                self.connected_slow_mo = None
                self.browser = FakeBrowser()

            def connect_over_cdp(self, endpoint_url, slow_mo=0):
                self.connected_url = endpoint_url
                self.connected_slow_mo = slow_mo
                return self.browser

        class FakePlaywright:
            def __init__(self):
                self.chromium = FakeChromium()

        class FakeSessions:
            def __init__(self):
                self.created = None
                self.released = []

            def create(self, **kwargs):
                self.created = kwargs
                return types.SimpleNamespace(
                    id="sess_123",
                    websocket_url="wss://connect.steel.dev?sessionId=sess_123",
                    session_viewer_url="https://viewer.steel.dev/sess_123",
                )

            def release(self, session_id):
                self.released.append(session_id)

        fake_sessions = FakeSessions()

        class FakeSteel:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.sessions = fake_sessions
                self.closed = False

            def close(self):
                self.closed = True

        fake_steel_module = types.ModuleType("steel")
        fake_steel_module.Steel = FakeSteel
        playwright = FakePlaywright()

        with mock.patch.dict(sys.modules, {"steel": fake_steel_module}):
            with mock.patch.dict(
                os.environ,
                {
                    "JOB_ASSETS_BROWSER_PROVIDER": "steel",
                    "STEEL_API_KEY": "steel_secret",
                },
                clear=False,
            ):
                session = browser_runtime.launch_chromium_browser(
                    playwright,
                    headless=False,
                    slow_mo=123,
                    persistent_profile_dir=str(Path.home() / ".job-assets" / "profile"),
                    prefer_local_browser=True,
                    viewport={"width": 900, "height": 700},
                    purpose="test automation",
                )

        self.assertEqual(session.provider, "steel")
        self.assertEqual(session.session_id, "sess_123")
        self.assertEqual(session.session_viewer_url, "https://viewer.steel.dev/sess_123")
        self.assertEqual(
            playwright.chromium.connected_url,
            "wss://connect.steel.dev?sessionId=sess_123&apiKey=steel_secret",
        )
        self.assertEqual(playwright.chromium.connected_slow_mo, 123)
        self.assertEqual(fake_sessions.created["dimensions"], {"width": 900, "height": 700})
        self.assertTrue(fake_sessions.created["use_proxy"])
        page = session.new_page(viewport={"width": 900, "height": 700})
        self.assertEqual(page.viewport, {"width": 900, "height": 700})
        session.close()
        self.assertEqual(fake_sessions.released, ["sess_123"])
        self.assertTrue(playwright.chromium.browser.closed)

    def test_chromium_browser_session_closes_context_pages_before_context(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")
        close_order: list[str] = []

        class FakePage:
            def __init__(self, name: str) -> None:
                self.name = name

            def close(self):
                close_order.append(f"page:{self.name}")

        class FakeContext:
            def __init__(self) -> None:
                self.pages = [FakePage("existing"), FakePage("created")]

            def close(self):
                close_order.append("context")

        context = FakeContext()
        session = browser_runtime.ChromiumBrowserSession(context=context)

        session.close()

        self.assertEqual(close_order, ["page:existing", "page:created", "context"])

    def test_launch_chromium_browser_uses_platform_safe_env_for_persistent_profile(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        class FakeChromium:
            def __init__(self):
                self.calls = []

            def launch_persistent_context(self, profile_dir, **kwargs):
                self.calls.append((profile_dir, kwargs))
                return object()

        class FakePlaywright:
            def __init__(self):
                self.chromium = FakeChromium()

        playwright = FakePlaywright()
        profile_dir = Path.home() / ".job-assets" / "playwright-submit-profile"

        session = browser_runtime.launch_chromium_browser(
            playwright,
            headless=True,
            persistent_profile_dir=str(profile_dir),
        )

        self.assertEqual(session.provider, "local")
        launched_profile_dir, kwargs = playwright.chromium.calls[0]
        self.assertEqual(launched_profile_dir, str(profile_dir))
        self.assertEqual(kwargs["env"]["HOME"], str(profile_dir.parent / "playwright-submit-profile-home"))
        self.assertEqual(
            kwargs["env"]["XDG_CONFIG_HOME"],
            str(profile_dir.parent / "playwright-submit-profile-home" / ".config"),
        )

    def test_launch_chromium_browser_provider_override_forces_local_session(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        class FakeChromium:
            def __init__(self):
                self.calls = []

            def launch_persistent_context(self, profile_dir, **kwargs):
                self.calls.append((profile_dir, kwargs))
                return object()

        class FakePlaywright:
            def __init__(self):
                self.chromium = FakeChromium()

        playwright = FakePlaywright()
        profile_dir = Path.home() / ".job-assets" / "playwright-submit-profile"

        with mock.patch.dict(
            os.environ,
            {
                "JOB_ASSETS_BROWSER_PROVIDER": "steel",
                "STEEL_API_KEY": "steel_secret",
            },
            clear=False,
        ):
            session = browser_runtime.launch_chromium_browser(
                playwright,
                headless=True,
                persistent_profile_dir=str(profile_dir),
                provider="local",
            )

        self.assertEqual(session.provider, "local")
        self.assertEqual(len(playwright.chromium.calls), 1)

    def test_launch_chromium_browser_moves_new_window_to_aerospace_workspace(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        class FakeChromium:
            def __init__(self):
                self.calls = []

            def launch_persistent_context(self, profile_dir, **kwargs):
                self.calls.append((profile_dir, kwargs))
                return object()

        class FakePlaywright:
            def __init__(self):
                self.chromium = FakeChromium()

        playwright = FakePlaywright()
        profile_dir = Path.home() / ".job-assets" / "playwright-submit-profile"

        with (
            mock.patch.object(browser_runtime, "_browser_launch_attempts", return_value=[("local browser channel chrome", {"channel": "chrome"})]),
            mock.patch.object(browser_runtime, "submit_browser_aerospace_workspace", return_value="2"),
            mock.patch.object(browser_runtime, "_aerospace_chromium_window_ids", side_effect=[{101, 202}, {101, 202, 303}]),
            mock.patch.object(browser_runtime, "_move_aerospace_window_to_workspace") as move_window,
            mock.patch.object(browser_runtime.sys, "platform", "darwin"),
        ):
            session = browser_runtime.launch_chromium_browser(
                playwright,
                headless=False,
                persistent_profile_dir=str(profile_dir),
                prefer_local_browser=True,
            )

        self.assertEqual(session.provider, "local")
        move_window.assert_called_once_with(303, "2", environ=None)

    def test_reveal_manual_challenge_brings_captcha_window_to_front_on_macos(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        page = mock.Mock()

        with (
            mock.patch.object(browser_runtime.sys, "platform", "darwin"),
            mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as run_command,
        ):
            browser_runtime.reveal_manual_challenge(page)

        page.evaluate.assert_called_once()
        run_command.assert_called_once()
        osascript_command = run_command.call_args.args[0]
        self.assertEqual(osascript_command[:2], ["osascript", "-e"])
        self.assertIn('set frontmost to true', osascript_command[2])
        self.assertIn('set miniaturized of targetWindow to false', osascript_command[2])
        self.assertIn('perform action "AXRaise" of targetWindow', osascript_command[2])
        self.assertIn('first window whose name contains "[Captcha]"', osascript_command[2])

    def test_launch_chromium_browser_retries_headless_macos_profile_launch_without_persistence(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        class FakeBrowser:
            def __init__(self):
                self.context_kwargs = None

            def new_context(self, **kwargs):
                self.context_kwargs = kwargs
                return object()

        class FakeChromium:
            def __init__(self):
                self.persistent_calls = []
                self.launch_calls = []
                self.browser = FakeBrowser()

            def launch_persistent_context(self, profile_dir, **kwargs):
                self.persistent_calls.append((profile_dir, kwargs))
                raise RuntimeError("persistent profile launch crashed")

            def launch(self, **kwargs):
                self.launch_calls.append(kwargs)
                return self.browser

        class FakePlaywright:
            def __init__(self):
                self.chromium = FakeChromium()

        playwright = FakePlaywright()
        profile_dir = Path.home() / ".job-assets" / "playwright-submit-profile"

        with mock.patch.object(browser_runtime.sys, "platform", "darwin"):
            session = browser_runtime.launch_chromium_browser(
                playwright,
                headless=True,
                persistent_profile_dir=str(profile_dir),
                viewport={"width": 1200, "height": 800},
                device_scale_factor=2,
                prefer_local_browser=True,
            )

        self.assertEqual(session.provider, "local")
        self.assertEqual(len(playwright.chromium.persistent_calls), 1)
        self.assertEqual(len(playwright.chromium.launch_calls), 1)
        self.assertEqual(
            playwright.chromium.browser.context_kwargs,
            {"viewport": {"width": 1200, "height": 800}, "device_scale_factor": 2},
        )

    def test_launch_chromium_browser_retries_headed_macos_profile_launch_without_persistence(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        class FakeBrowser:
            def __init__(self):
                self.context_kwargs = None

            def new_context(self, **kwargs):
                self.context_kwargs = kwargs
                return object()

        class FakeChromium:
            def __init__(self):
                self.persistent_calls = []
                self.launch_calls = []
                self.browser = FakeBrowser()

            def launch_persistent_context(self, profile_dir, **kwargs):
                self.persistent_calls.append((profile_dir, kwargs))
                raise RuntimeError("persistent profile launch crashed")

            def launch(self, **kwargs):
                self.launch_calls.append(kwargs)
                return self.browser

        class FakePlaywright:
            def __init__(self):
                self.chromium = FakeChromium()

        playwright = FakePlaywright()
        profile_dir = Path.home() / ".job-assets" / "playwright-submit-profile"

        with mock.patch.object(browser_runtime.sys, "platform", "darwin"):
            session = browser_runtime.launch_chromium_browser(
                playwright,
                headless=False,
                persistent_profile_dir=str(profile_dir),
                viewport={"width": 1200, "height": 800},
                device_scale_factor=2,
                prefer_local_browser=True,
            )

        self.assertEqual(session.provider, "local")
        self.assertEqual(len(playwright.chromium.persistent_calls), 1)
        self.assertEqual(len(playwright.chromium.launch_calls), 1)
        self.assertEqual(
            playwright.chromium.browser.context_kwargs,
            {"viewport": {"width": 1200, "height": 800}, "device_scale_factor": 2},
        )

    def test_launch_chromium_browser_skips_bundled_testing_browser_for_headed_macos(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        class FakeChromium:
            def __init__(self):
                self.calls = []

            def launch_persistent_context(self, profile_dir, **kwargs):
                self.calls.append((profile_dir, kwargs))
                if kwargs.get("channel") == "chrome":
                    raise RuntimeError("missing local chrome")
                if kwargs.get("executable_path"):
                    raise RuntimeError("missing local executable")
                raise AssertionError("bundled Chromium should have been skipped on macOS")

        class FakePlaywright:
            def __init__(self):
                self.chromium = FakeChromium()

        playwright = FakePlaywright()
        profile_dir = Path.home() / ".job-assets" / "playwright-submit-profile"

        with mock.patch.object(browser_runtime.sys, "platform", "darwin"):
            with mock.patch.object(
                browser_runtime,
                "_browser_launch_attempts",
                return_value=[
                    ("local browser channel chrome", {"channel": "chrome"}),
                    ("bundled Chromium", {}),
                ],
            ):
                with self.assertRaises(RuntimeError) as context:
                    browser_runtime.launch_chromium_browser(
                        playwright,
                        headless=False,
                        persistent_profile_dir=str(profile_dir),
                        prefer_local_browser=True,
                        purpose="test automation",
                    )

        self.assertEqual(len(playwright.chromium.calls), 1)
        self.assertIn("bundled Chrome for Testing is disabled for headed macOS submit flows", str(context.exception))
        self.assertIn("Headed macOS submit flows require an installed local browser", str(context.exception))

    def test_ensure_google_session_continues_when_page_content_probe_raises(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        class FakePage:
            def __init__(self):
                self.url = "https://accounts.google.com"
                self.goto_calls = []
                self.wait_calls = []
                self.inner_text_calls = 0
                self.content_calls = 0
                self.poll_count = 0

            def goto(self, url, wait_until=None, timeout=None):
                self.goto_calls.append((url, wait_until, timeout))

            def inner_text(self, selector, timeout=None):
                self.inner_text_calls += 1
                return "[]" if self.inner_text_calls == 1 else ""

            def wait_for_timeout(self, timeout_ms):
                self.wait_calls.append(timeout_ms)
                self.poll_count += 1

            def content(self):
                self.content_calls += 1
                if self.content_calls == 1:
                    raise RuntimeError("content unavailable")
                return "data-email=\"bot@example.com\""

        page = FakePage()

        with mock.patch.object(browser_runtime.sys, "platform", "linux"):
            browser_runtime.ensure_google_session(page, headless=False)

        assert [call[0] for call in page.goto_calls] == [
            "https://accounts.google.com/ListAccounts?gpsia=1&source=ChromiumBrowser",
            "https://accounts.google.com",
        ]
        assert page.wait_calls == [2000, 2000]
        assert page.content_calls == 2

    def test_ensure_google_session_reports_active_signed_in_session(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        class FakePage:
            url = "about:blank"

            def goto(self, url, wait_until=None, timeout=None):
                self.last_goto = (url, wait_until, timeout)

            def inner_text(self, selector, timeout=None):
                return '[["gaia.l.a","user@example.com","User Example",0,0,0,0,1,null,"avatar"]]'

        page = FakePage()
        with mock.patch("builtins.print") as print_mock:
            browser_runtime.ensure_google_session(page, headless=False)

        print_mock.assert_any_call("Google session: active (signed in)")

    def test_ensure_google_session_headless_warns_without_interactive_reauth(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        class FakePage:
            def __init__(self):
                self.goto_calls = []

            def goto(self, url, wait_until=None, timeout=None):
                self.goto_calls.append(url)

            def inner_text(self, selector, timeout=None):
                return "[]"

        page = FakePage()
        with mock.patch("builtins.print") as print_mock:
            browser_runtime.ensure_google_session(page, headless=True)

        assert page.goto_calls == [
            "https://accounts.google.com/ListAccounts?gpsia=1&source=ChromiumBrowser"
        ]
        print_mock.assert_any_call("WARNING: Google session expired (headless — cannot re-authenticate).")

    def test_run_osascript_returns_false_when_subprocess_raises(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        with (
            mock.patch.object(browser_runtime.sys, "platform", "darwin"),
            mock.patch("subprocess.run", side_effect=OSError("osascript missing")),
        ):
            assert browser_runtime._run_osascript('display notification "x"') is False

    def test_focus_chromium_window_without_title_uses_first_window_script(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        with mock.patch.object(browser_runtime, "_run_osascript", return_value=True) as run_osascript:
            browser_runtime.focus_chromium_window(title_substring="")

        script = run_osascript.call_args.args[0]
        assert "set targetWindow to first window" in script
        assert "whose name contains" not in script

    def test_detect_webapp_screen_origin_matches_window_to_screen(self):
        browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")

        responses = [
            mock.Mock(stdout="1440,100\n"),
            mock.Mock(stdout='[{"x":0,"y":0,"w":1440,"h":900},{"x":1440,"y":0,"w":1440,"h":900}]'),
        ]

        with (
            mock.patch.object(browser_runtime.sys, "platform", "darwin"),
            mock.patch("subprocess.run", side_effect=responses),
        ):
            assert browser_runtime._detect_webapp_screen_origin() == (1440, 0)


def test_submit_browser_profile_dir_with_worker_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JOB_ASSETS_SUBMIT_BROWSER_PROFILE_DIR", raising=False)
    browser_runtime = load_module("browser_runtime", "scripts/browser_runtime.py")
    # Patch the module-level default so it picks up the monkeypatched HOME
    monkeypatch.setattr(
        browser_runtime,
        "DEFAULT_SUBMIT_BROWSER_PROFILE_DIR",
        Path(tmp_path) / ".job-assets" / "playwright-submit-profile",
    )
    p = browser_runtime.submit_browser_profile_dir(worker_id=2)
    assert p.name == "worker-2"
    assert "playwright-submit-profile" in str(p.parent)


if __name__ == "__main__":
    unittest.main()
