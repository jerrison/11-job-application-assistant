#!/usr/bin/env python3
"""Shared Playwright browser-launch helpers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from app_paths import browser_root

DEFAULT_SUBMIT_BROWSER_CHANNELS = ("chrome",)
DEFAULT_SUBMIT_BROWSER_PROVIDER = "local"
COMMON_CHROMIUM_EXECUTABLES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
DEFAULT_SUBMIT_BROWSER_PROFILE_DIR = Path.home() / ".job-assets" / "playwright-submit-profile"
DEFAULT_SUBMIT_VIEWPORT = {"width": 1360, "height": 900}
DEFAULT_STEEL_CLOUD_BASE_URL = "https://api.steel.dev"
DEFAULT_STEEL_LOCAL_BASE_URL = "http://localhost:3000"
DEFAULT_STEEL_API_TIMEOUT_MS = 1_800_000
DEFAULT_HUMAN_FILL_CLICK_TIMEOUT_MS = 1_500
DEFAULT_HUMAN_FILL_DIRECT_FILL_THRESHOLD = 400
JOB_ASSETS_AEROSPACE_WORKSPACE_ENV = "JOB_ASSETS_AEROSPACE_WORKSPACE"
DEFAULT_AEROSPACE_WORKSPACE = "Q"
AEROSPACE_CHROMIUM_APP_NAMES = frozenset(
    {"Google Chrome", "Google Chrome for Testing", "Chromium", "Microsoft Edge"}
)
DEFAULT_AEROSPACE_WINDOW_POLL_ATTEMPTS = 10
DEFAULT_AEROSPACE_WINDOW_POLL_DELAY_SECONDS = 0.2


def _bool_from_env(name: str, default: bool, *, environ: dict[str, str] | None = None) -> bool:
    env = environ or os.environ
    raw = env.get(name, "").strip().casefold()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _split_csv_env(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _non_negative_int_from_env(name: str, default: int, *, environ: dict[str, str] | None = None) -> int:
    env = environ or os.environ
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def submit_browser_profile_dir(*, environ: dict[str, str] | None = None, worker_id: int | None = None) -> Path:
    env = environ or os.environ
    # Use real Chrome profile if configured (better captcha behavior via
    # existing cookies/fingerprint). Falls back to isolated Playwright profile.
    chrome_dir = env.get("JOB_ASSETS_CHROME_USER_DATA_DIR", "").strip()
    if chrome_dir:
        return Path(chrome_dir).expanduser()
    raw = env.get("JOB_ASSETS_SUBMIT_BROWSER_PROFILE_DIR", "").strip()
    if raw:
        base = Path(raw).expanduser()
    else:
        base = browser_root(environ=env) / "playwright-submit-profile"
    if worker_id is not None:
        return base / f"worker-{worker_id}"
    # All workers share one profile so that a single Google sign-in
    # (via setup_browser_profile.py) benefits every worker.  Workers
    # run browsers serially so profile locking is not an issue.
    return base


def submit_browser_provider(*, environ: dict[str, str] | None = None) -> str:
    env = environ or os.environ
    value = env.get("JOB_ASSETS_BROWSER_PROVIDER", DEFAULT_SUBMIT_BROWSER_PROVIDER).strip().casefold()
    return value if value in {"local", "steel"} else DEFAULT_SUBMIT_BROWSER_PROVIDER


def submit_slow_mo_ms(headless: bool, *, environ: dict[str, str] | None = None, default: int = 125) -> int:
    if headless:
        return 0
    return _non_negative_int_from_env("JOB_ASSETS_SUBMIT_SLOW_MO_MS", default, environ=environ)


def submit_type_delay_ms(*, environ: dict[str, str] | None = None, default: int = 45) -> int:
    return _non_negative_int_from_env("JOB_ASSETS_SUBMIT_TYPE_DELAY_MS", default, environ=environ)


def submit_viewport(*, environ: dict[str, str] | None = None) -> dict[str, int]:
    env = environ or os.environ
    width = _non_negative_int_from_env(
        "JOB_ASSETS_SUBMIT_VIEWPORT_WIDTH", DEFAULT_SUBMIT_VIEWPORT["width"], environ=env
    )
    height = _non_negative_int_from_env(
        "JOB_ASSETS_SUBMIT_VIEWPORT_HEIGHT", DEFAULT_SUBMIT_VIEWPORT["height"], environ=env
    )
    return {"width": width, "height": height}


def _host_matches_zoom_target(host: str, targets: tuple[str, ...]) -> bool:
    normalized_host = host.strip().casefold().lstrip(".")
    if not normalized_host:
        return False
    for target in targets:
        normalized_target = target.strip().casefold().lstrip(".")
        if not normalized_target:
            continue
        if normalized_host == normalized_target or normalized_host.endswith(f".{normalized_target}"):
            return True
    return False


def normalize_chromium_profile_zoom(
    profile_dir: str | os.PathLike[str],
    *,
    hosts: tuple[str, ...] = (),
    reset_default_zoom: bool = False,
) -> bool:
    """Remove stored Chromium zoom overrides for a persistent profile.

    Playwright reuses Chromium's on-disk Preferences for persistent profiles.
    If a prior manual session zoomed a site in or out, Chromium will restore
    that scale on the next automated run, which changes layout metrics and can
    make screenshots unreadable. This helper scrubs those stored zoom entries
    before the browser launches.
    """

    prefs_path = Path(profile_dir).expanduser() / "Default" / "Preferences"
    if not prefs_path.exists():
        return False

    try:
        data = json.loads(prefs_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    changed = False
    if hosts:
        partition = data.get("partition")
        if isinstance(partition, dict):
            per_host_zoom_levels = partition.get("per_host_zoom_levels")
            if isinstance(per_host_zoom_levels, dict):
                empty_partitions: list[str] = []
                for partition_key, partition_zoom_levels in per_host_zoom_levels.items():
                    if not isinstance(partition_zoom_levels, dict):
                        continue
                    hosts_to_remove = [
                        host for host in list(partition_zoom_levels) if _host_matches_zoom_target(host, hosts)
                    ]
                    for host in hosts_to_remove:
                        del partition_zoom_levels[host]
                        changed = True
                    if not partition_zoom_levels:
                        empty_partitions.append(str(partition_key))
                for partition_key in empty_partitions:
                    del per_host_zoom_levels[partition_key]
                    changed = True

    if reset_default_zoom:
        profile = data.get("profile")
        if isinstance(profile, dict) and profile.get("default_zoom_level") not in (None, 0, 0.0):
            profile["default_zoom_level"] = 0.0
            changed = True

    if not changed:
        return False

    try:
        prefs_path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    except OSError:
        return False
    return True


def _run_aerospace_command(
    args: list[str],
    *,
    timeout: float = 2.0,
    environ: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str] | None:
    if sys.platform != "darwin":
        return None
    if not shutil.which("aerospace"):
        return None
    try:
        result = subprocess.run(
            ["aerospace", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environ,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result if result.returncode == 0 else None


def submit_browser_aerospace_workspace(*, environ: dict[str, str] | None = None) -> str | None:
    env = environ or os.environ
    explicit = str(env.get(JOB_ASSETS_AEROSPACE_WORKSPACE_ENV, "")).strip()
    if explicit:
        return explicit
    return DEFAULT_AEROSPACE_WORKSPACE if sys.platform == "darwin" else None


def _aerospace_target_workspace(*, environ: dict[str, str] | None = None) -> str | None:
    return submit_browser_aerospace_workspace(environ=environ)


def _aerospace_chromium_window_ids(*, environ: dict[str, str] | None = None) -> set[int]:
    result = _run_aerospace_command(
        ["list-windows", "--all", "--format", "%{window-id}|%{app-name}"],
        environ=environ,
    )
    if result is None:
        return set()

    window_ids: set[int] = set()
    for line in result.stdout.splitlines():
        window_id_text, _, app_name = line.partition("|")
        if app_name.strip() not in AEROSPACE_CHROMIUM_APP_NAMES:
            continue
        try:
            window_ids.add(int(window_id_text.strip()))
        except ValueError:
            continue
    return window_ids


def _move_aerospace_window_to_workspace(
    window_id: int,
    workspace: str,
    *,
    environ: dict[str, str] | None = None,
) -> bool:
    result = _run_aerospace_command(
        ["move-node-to-workspace", "--window-id", str(window_id), workspace],
        environ=environ,
    )
    return result is not None


def _place_new_chromium_windows_on_workspace(
    workspace: str | None,
    previous_window_ids: set[int],
    *,
    environ: dict[str, str] | None = None,
    attempts: int = DEFAULT_AEROSPACE_WINDOW_POLL_ATTEMPTS,
    delay_seconds: float = DEFAULT_AEROSPACE_WINDOW_POLL_DELAY_SECONDS,
) -> None:
    if not workspace:
        return

    for _ in range(attempts):
        new_window_ids = sorted(_aerospace_chromium_window_ids(environ=environ) - previous_window_ids)
        if new_window_ids:
            for window_id in new_window_ids:
                _move_aerospace_window_to_workspace(window_id, workspace, environ=environ)
            return
        time.sleep(delay_seconds)


def steel_local_mode(*, environ: dict[str, str] | None = None) -> bool:
    return _bool_from_env("STEEL_LOCAL", False, environ=environ)


def steel_base_url(*, environ: dict[str, str] | None = None) -> str:
    env = environ or os.environ
    raw = env.get("STEEL_BASE_URL", "").strip()
    if raw:
        return raw
    return DEFAULT_STEEL_LOCAL_BASE_URL if steel_local_mode(environ=env) else DEFAULT_STEEL_CLOUD_BASE_URL


def steel_use_proxy(*, environ: dict[str, str] | None = None) -> bool:
    env = environ or os.environ
    default = not steel_local_mode(environ=env)
    return _bool_from_env("JOB_ASSETS_STEEL_USE_PROXY", default, environ=env)


def steel_solve_captcha(*, environ: dict[str, str] | None = None) -> bool:
    return _bool_from_env("JOB_ASSETS_STEEL_SOLVE_CAPTCHA", True, environ=environ)


def steel_api_timeout_ms(*, environ: dict[str, str] | None = None) -> int:
    return _non_negative_int_from_env(
        "JOB_ASSETS_STEEL_API_TIMEOUT_MS",
        DEFAULT_STEEL_API_TIMEOUT_MS,
        environ=environ,
    )


def steel_api_key(*, environ: dict[str, str] | None = None) -> str:
    env = environ or os.environ
    return env.get("STEEL_API_KEY", "").strip()


def _append_query_parameter(url: str, key: str, value: str | None) -> str:
    if not value:
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if query.get(key):
        return url
    query[key] = value
    return urlunsplit(parts._replace(query=urlencode(query)))


def steel_cdp_url(websocket_url: str, *, api_key: str | None) -> str:
    return _append_query_parameter(websocket_url, "apiKey", api_key or "")


def _focus_without_click(locator) -> None:
    try:
        locator.scroll_into_view_if_needed()
    except Exception:
        pass
    locator.evaluate(
        """(element) => {
            element.focus();
            if (typeof element.select === 'function') {
                element.select();
            }
        }"""
    )


def human_fill(locator, value: str, *, delay_ms: int | None = None) -> None:
    delay = submit_type_delay_ms() if delay_ms is None else max(delay_ms, 0)
    try:
        locator.click(timeout=DEFAULT_HUMAN_FILL_CLICK_TIMEOUT_MS)
    except Exception:
        _focus_without_click(locator)
    locator.fill("")
    if not value:
        return
    if len(value) > DEFAULT_HUMAN_FILL_DIRECT_FILL_THRESHOLD:
        locator.fill(value)
        return
    try:
        locator.press_sequentially(value, delay=delay)
    except AttributeError:  # pragma: no cover - depends on Playwright version
        locator.type(value, delay=delay)


def ensure_google_session(page, *, headless: bool = False) -> None:
    """Check Google sign-in status; prompt interactive re-auth if expired.

    Navigates to a lightweight Google endpoint, checks if the user is signed
    in, and if not (and running headed) opens the sign-in page and waits for
    the user to complete authentication before returning.
    """
    check_url = "https://accounts.google.com/ListAccounts?gpsia=1&source=ChromiumBrowser"
    try:
        page.goto(check_url, wait_until="domcontentloaded", timeout=10000)
    except Exception:
        return  # network issue — don't block the pipeline
    try:
        body = page.inner_text("body", timeout=3000)
    except Exception:
        return
    # ListAccounts returns a JSON-ish array.  An empty array "[]" means no
    # signed-in accounts; a populated array means at least one session exists.
    signed_in = body.strip() not in ("", "[]", "[[]]")
    if signed_in:
        print("Google session: active (signed in)")
        return

    if headless:
        print("WARNING: Google session expired (headless — cannot re-authenticate).")
        return

    print("Google session expired — opening sign-in page. Please sign in and then return to this window.")
    if sys.platform == "darwin":
        try:
            import subprocess as _sp

            _sp.run(
                [
                    "osascript",
                    "-e",
                    'display notification "Google session expired — sign in to continue" with title "Job Applications"',
                ],
                timeout=3,
                capture_output=True,
            )
        except Exception:
            pass
    page.goto("https://accounts.google.com", wait_until="domcontentloaded", timeout=15000)
    # Wait until the user completes sign-in (myaccount page or signed-in state).
    # Poll every 2s for up to 5 minutes.
    for _ in range(150):
        page.wait_for_timeout(2000)
        current = page.url
        if "myaccount.google.com" in current or "mail.google.com" in current:
            break
        try:
            body = page.inner_text("body", timeout=2000)
        except Exception:
            continue
        # Re-check via the current URL / page content
        if "SignOutOptions" in body or "data-email" in (page.content() or ""):
            break
    else:
        print("WARNING: Timed out waiting for Google sign-in (5 min). Continuing anyway.")
    print("Google session restored.")


def _detect_webapp_screen_origin() -> tuple[int, int] | None:
    """Find which monitor the web app window is on and return its top-left corner.

    Uses AppleScript to locate the "Job Applications" browser window, then JXA
    with AppKit to find which screen contains it. Returns (x, y) in the
    coordinate system used by Chrome's ``--window-position`` flag (top-left of
    primary display, Y increasing downward). Returns ``None`` on failure or
    non-macOS.
    """
    if sys.platform != "darwin":
        return None
    try:
        import subprocess as _sp

        # Step 1: find web app window position via AppleScript
        pos_result = _sp.run(
            [
                "osascript",
                "-e",
                'tell application "System Events"\n'
                "  repeat with p in (every process whose background only is false)\n"
                "    try\n"
                "      repeat with w in windows of p\n"
                '        if name of w contains "Job Applications" then\n'
                "          set pos to position of w\n"
                '          return ((item 1 of pos) as text) & "," & ((item 2 of pos) as text)\n'
                "        end if\n"
                "      end repeat\n"
                "    end try\n"
                "  end repeat\n"
                '  return ""\n'
                "end tell",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        pos_str = pos_result.stdout.strip()
        if not pos_str:
            return None
        wx, wy = int(pos_str.split(",")[0]), int(pos_str.split(",")[1])

        # Step 2: get screen frames via JXA (AppKit) and convert to Chrome coords
        scr_result = _sp.run(
            [
                "osascript",
                "-l",
                "JavaScript",
                "-e",
                'ObjC.import("AppKit");\n'
                "const screens = $.NSScreen.screens;\n"
                "const pH = screens.objectAtIndex(0).frame.size.height;\n"
                "const r = [];\n"
                "for (let i = 0; i < screens.count; i++) {\n"
                "  const f = screens.objectAtIndex(i).frame;\n"
                "  r.push({x: f.origin.x,\n"
                "          y: pH - f.origin.y - f.size.height,\n"
                "          w: f.size.width, h: f.size.height});\n"
                "}\n"
                "JSON.stringify(r);",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        import json as _json

        screens = _json.loads(scr_result.stdout.strip())
        for scr in screens:
            if scr["x"] <= wx < scr["x"] + scr["w"] and scr["y"] <= wy < scr["y"] + scr["h"]:
                return (int(scr["x"]), int(scr["y"]))
        return None
    except Exception:
        return None


def _run_osascript(script: str, *, timeout: float = 3.0) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import subprocess as _sp

        result = _sp.run(
            ["osascript", "-e", script],
            timeout=timeout,
            capture_output=True,
        )
    except Exception:
        return False
    return result.returncode == 0


def minimize_browser_window(page) -> None:
    """Minimize the browser window on macOS using AppleScript."""
    _run_osascript(
        'tell application "System Events" to set miniaturized of '
        "first window of (first process whose frontmost is true) to true"
    )


def focus_chromium_window(*, title_substring: str = "[Captcha]") -> bool:
    target_window = "first window"
    if title_substring:
        target_window = f'first window whose name contains {json.dumps(title_substring)}'
    return _run_osascript(
        """tell application "System Events"
            tell (first process whose name contains "Chrom")
                set frontmost to true
                set targetWindow to """
        + target_window
        + """
                try
                    set miniaturized of targetWindow to false
                end try
                try
                    perform action "AXRaise" of targetWindow
                end try
            end tell
        end tell"""
    )


def reveal_manual_challenge(page) -> None:
    page.evaluate(
        """() => {
            const isVisible = (node) => {
              if (!node) return false;
              const style = window.getComputedStyle(node);
              if (style.display === 'none' || style.visibility === 'hidden') return false;
              const rect = node.getBoundingClientRect();
              return rect.width > 0 && rect.height > 0;
            };

            const elementText = (node) => (node?.textContent || node?.value || '').replace(/\\s+/g, ' ').trim();

            const isScrollable = (node) => {
              if (!node) return false;
              const style = window.getComputedStyle(node);
              const overflow = `${style.overflowY} ${style.overflow}`.toLowerCase();
              return /(auto|scroll)/.test(overflow) && node.scrollHeight > node.clientHeight + 4;
            };

            const visibleNodes = (selector) => Array.from(document.querySelectorAll(selector)).filter(isVisible);

            const submitButton = Array.from(document.querySelectorAll('button, input[type="submit"]'))
              .filter(isVisible)
              .find((node) => /submit|apply|continue/i.test(elementText(node))) || null;

            const formFooter = Array.from(document.querySelectorAll('p, div, span, small'))
              .filter(isVisible)
              .find((node) => /protected by reCAPTCHA|privacy policy|terms of service/i.test(elementText(node))) || null;

            const scrollAncestorsIntoView = (node) => {
              let current = node?.parentElement || null;
              while (current) {
                if (isScrollable(current)) {
                  const rect = node.getBoundingClientRect();
                  const parentRect = current.getBoundingClientRect();
                  current.scrollTop += rect.top - parentRect.top - Math.max((current.clientHeight - rect.height) / 2, 40);
                }
                current = current.parentElement;
              }
            };

            const scrollAncestorsToBottom = (node) => {
              let current = node;
              while (current) {
                if (isScrollable(current)) {
                  current.scrollTop = current.scrollHeight;
                }
                current = current.parentElement;
              }
            };

            const challengeSelectors = [
              'iframe[title*="recaptcha" i]',
              'iframe[src*="recaptcha"]',
              '.grecaptcha-badge',
              '[data-sitekey]',
            ];
            let target = null;
            for (const selector of challengeSelectors) {
              target = visibleNodes(selector)[0] || target;
              if (target) break;
            }
            if (!target) {
              target = submitButton || formFooter;
            }

            const main = document.querySelector('main, [role="main"], form');
            const primaryScrollable =
              [target, submitButton, formFooter, main]
                .flatMap((node) => {
                  const nodes = [];
                  let current = node;
                  while (current) {
                    if (isScrollable(current)) nodes.push(current);
                    current = current.parentElement;
                  }
                  return nodes;
                })
                .sort((a, b) => b.clientHeight - a.clientHeight)[0] || null;

            if (primaryScrollable) {
              primaryScrollable.scrollTop = primaryScrollable.scrollHeight;
              if (primaryScrollable instanceof HTMLElement) {
                primaryScrollable.tabIndex = -1;
                primaryScrollable.focus({ preventScroll: true });
              }
            }

            if (document.scrollingElement) {
              document.scrollingElement.scrollTop = document.scrollingElement.scrollHeight;
            }
            window.scrollTo({ top: document.body.scrollHeight, behavior: 'instant' });

            const activeTarget = target || submitButton || formFooter || main;
            if (!activeTarget) {
              window.scrollTo({ top: document.body.scrollHeight, behavior: 'instant' });
              return;
            }

            scrollAncestorsToBottom(activeTarget);
            scrollAncestorsIntoView(activeTarget);
            activeTarget.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
            window.scrollBy({ top: Math.max(window.innerHeight * 0.15, 120), behavior: 'instant' });

            if (activeTarget instanceof HTMLElement) {
              activeTarget.focus({ preventScroll: true });
            }
        }"""
    )
    focus_chromium_window()


def _browser_launch_attempts(
    *,
    channel_env_var: str | None = None,
    executable_env_var: str | None = None,
    prefer_local_browser: bool = False,
    environ: dict[str, str] | None = None,
    existing_paths: set[str] | None = None,
) -> list[tuple[str, dict]]:
    env = environ or os.environ
    known_paths = existing_paths

    attempts: list[tuple[str, dict]] = []
    seen_targets: set[str] = set()

    def append_attempt(label: str, kwargs: dict) -> None:
        target = _browser_attempt_target(label, kwargs)
        if target and target in seen_targets:
            return
        if target:
            seen_targets.add(target)
        attempts.append((label, kwargs))

    executable_path = env.get(executable_env_var or "", "").strip() if executable_env_var else ""
    if executable_path:
        append_attempt(
            f"executable path from {executable_env_var}",
            {"executable_path": str(Path(executable_path).expanduser())},
        )

    for channel in _split_csv_env(env.get(channel_env_var or "", "") if channel_env_var else None):
        append_attempt(f"browser channel {channel}", {"channel": channel})

    if prefer_local_browser:
        for channel in DEFAULT_SUBMIT_BROWSER_CHANNELS:
            append_attempt(f"local browser channel {channel}", {"channel": channel})
        for path in COMMON_CHROMIUM_EXECUTABLES:
            if known_paths is None:
                exists = Path(path).exists()
            else:
                exists = path in known_paths
            if exists:
                append_attempt(
                    f"common browser executable {Path(path).name}",
                    {"executable_path": path},
                )

    append_attempt("bundled Chromium", {})

    deduped: list[tuple[str, dict]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for label, kwargs in attempts:
        key = tuple(sorted((name, str(value)) for name, value in kwargs.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append((label, kwargs))
    return deduped


def _browser_attempt_target(label: str, kwargs: dict) -> str | None:
    channel = str(kwargs.get("channel", "")).strip().casefold()
    if channel:
        channel_targets = {
            "chrome": "chrome",
            "msedge": "edge",
            "microsoft-edge": "edge",
        }
        return channel_targets.get(channel, f"channel:{channel}")

    executable_path = str(kwargs.get("executable_path", "")).strip().casefold()
    if executable_path:
        if "google chrome for testing.app" in executable_path:
            return "chrome-testing"
        executable_targets = (
            ("google chrome.app", "chrome"),
            ("chromium.app", "chromium-app"),
            ("microsoft edge.app", "edge"),
        )
        for marker, target in executable_targets:
            if marker in executable_path:
                return target
        return f"path:{executable_path}"

    if label == "bundled Chromium":
        return "bundled-chromium"
    return None


class ChromiumBrowserSession:
    def __init__(
        self,
        *,
        browser: object | None = None,
        context: object | None = None,
        close_target: object | None = None,
        provider: str = DEFAULT_SUBMIT_BROWSER_PROVIDER,
        session_id: str | None = None,
        session_viewer_url: str | None = None,
        release_callback=None,
    ):
        self.browser = browser
        self.context = context
        self._close_target = close_target if close_target is not None else (context if context is not None else browser)
        self._claimed_existing_page = False
        self._created_pages: list = []
        self.provider = provider
        self.session_id = session_id
        self.session_viewer_url = session_viewer_url
        self.release_callback = release_callback

    def new_page(self, **kwargs):
        if self.context is None:
            assert self.browser is not None
            return self.browser.new_page(**kwargs)

        page = None
        if not self._claimed_existing_page:
            pages = list(getattr(self.context, "pages", []))
            if pages:
                candidate = pages[0]
                if candidate.url == "about:blank":
                    page = candidate
            self._claimed_existing_page = True
        if page is None:
            page = self.context.new_page()
            self._created_pages.append(page)

        viewport = kwargs.pop("viewport", None)
        kwargs.pop("device_scale_factor", None)
        if viewport:
            page.set_viewport_size(viewport)
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise TypeError(f"Unsupported page options for persistent Chromium session: {unexpected}")
        return page

    def close(self) -> None:
        # Close open pages before shutting down a persistent context. Some
        # Playwright persistent sessions can hang indefinitely inside
        # context.close() unless pages are closed first.
        pages_to_close: list = []
        if self.context is not None:
            try:
                pages_to_close = list(getattr(self.context, "pages", []))
            except Exception:
                pages_to_close = []
        elif self._close_target is None:
            pages_to_close = list(self._created_pages)

        for page in pages_to_close:
            try:
                page.close()
            except Exception:
                pass
        self._created_pages.clear()

        # For CDP sessions (close_target=None): close only the pages we created,
        # not the user's browser.
        if self._close_target is not None:
            try:
                self._close_target.close()
            except Exception:  # pragma: no cover - depends on browser runtime behavior
                pass
        if self.release_callback is not None:
            try:
                self.release_callback()
            except Exception:  # pragma: no cover - depends on Steel/network state
                pass


def _launch_steel_browser(
    playwright,
    *,
    headless: bool,
    slow_mo: int,
    viewport: dict[str, int] | None,
    purpose: str,
    environ: dict[str, str] | None = None,
):
    env = environ or os.environ
    try:
        from steel import Steel
    except ImportError as exc:  # pragma: no cover - depends on optional dependency state
        raise RuntimeError(
            "Steel browser support requires the `steel-sdk` package. Run `uv add steel-sdk` first."
        ) from exc

    base_url = steel_base_url(environ=env)
    api_key = steel_api_key(environ=env)
    if not steel_local_mode(environ=env) and not api_key:
        raise RuntimeError(
            "Steel browser provider requires STEEL_API_KEY in cloud mode. "
            "Set STEEL_API_KEY or set STEEL_LOCAL=true for a self-hosted Steel instance."
        )

    client_kwargs = {"base_url": base_url}
    if api_key:
        client_kwargs["steel_api_key"] = api_key
    client = Steel(**client_kwargs)

    session = client.sessions.create(
        api_timeout=steel_api_timeout_ms(environ=env),
        use_proxy=steel_use_proxy(environ=env),
        solve_captcha=steel_solve_captcha(environ=env),
        dimensions=viewport or submit_viewport(environ=env),
    )
    connect_url = steel_cdp_url(str(session.websocket_url), api_key=api_key)
    try:
        browser = playwright.chromium.connect_over_cdp(connect_url, slow_mo=slow_mo)
    except Exception:
        try:
            client.sessions.release(session.id)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        raise

    contexts = list(getattr(browser, "contexts", []))
    context = contexts[0] if contexts else browser.new_context()

    def release_session() -> None:
        try:
            client.sessions.release(session.id)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    return ChromiumBrowserSession(
        browser=browser,
        context=context,
        close_target=browser,
        provider="steel",
        session_id=str(session.id),
        session_viewer_url=getattr(session, "session_viewer_url", None),
        release_callback=release_session,
    )


def _browser_launch_env(
    persistent_profile_dir: str | os.PathLike[str] | None,
    *,
    headless: bool,
    environ: dict[str, str] | None = None,
) -> dict[str, str] | None:
    if not persistent_profile_dir:
        return None
    if sys.platform == "darwin" and not headless:
        # On macOS, headed Chrome is a full AppKit app. Rewriting HOME/XDG for a
        # persistent Playwright launch can trip HIServices/AppKit startup and
        # crash or hang the browser before the first page appears. The
        # launch_persistent_context user-data dir already gives us the browser
        # isolation we need, so keep the native GUI environment on headed macOS.
        return None
    env = dict(environ or os.environ)
    profile_dir = Path(persistent_profile_dir).expanduser()
    browser_home = profile_dir.parent / f"{profile_dir.name}-home"
    browser_home.mkdir(parents=True, exist_ok=True)
    (browser_home / "Library" / "Application Support").mkdir(parents=True, exist_ok=True)
    (browser_home / ".config").mkdir(parents=True, exist_ok=True)
    (browser_home / ".cache").mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(browser_home)
    env["XDG_CONFIG_HOME"] = str(browser_home / ".config")
    env["XDG_CACHE_HOME"] = str(browser_home / ".cache")
    return env


def _macos_headed_browser_attempt_block_reason(
    label: str,
    kwargs: dict,
    *,
    headless: bool,
    persistent_profile_dir: str | os.PathLike[str] | None,
    platform: str | None = None,
) -> str | None:
    active_platform = platform or sys.platform
    if active_platform != "darwin" or headless or not persistent_profile_dir:
        return None

    channel = str(kwargs.get("channel", "")).strip().casefold()
    executable_path = str(kwargs.get("executable_path", "")).strip().casefold()
    is_bundled_attempt = label == "bundled Chromium" or channel == "chromium"
    is_testing_bundle = "google chrome for testing.app" in executable_path
    if not is_bundled_attempt and not is_testing_bundle:
        return None

    return (
        "bundled Chrome for Testing is disabled for headed macOS submit flows because it can abort "
        "during AppKit/HIServices startup; use an installed local browser or JOB_ASSETS_BROWSER_PROVIDER=steel."
    )


def _should_retry_without_persistent_profile(
    *,
    headless: bool,
    persistent_profile_dir: str | os.PathLike[str] | None,
    platform: str | None = None,
) -> bool:
    active_platform = platform or sys.platform
    return active_platform == "darwin" and bool(persistent_profile_dir)


def launch_chromium_browser(
    playwright,
    *,
    headless: bool,
    slow_mo: int = 0,
    channel_env_var: str | None = None,
    executable_env_var: str | None = None,
    persistent_profile_dir: str | os.PathLike[str] | None = None,
    prefer_local_browser: bool = False,
    viewport: dict[str, int] | None = None,
    device_scale_factor: float | None = None,
    provider: str | None = None,
    purpose: str = "browser automation",
):
    """Launch Playwright Chromium and return a session wrapper with new_page/close."""

    configured_provider = (provider or "").strip().casefold()
    if configured_provider not in {"local", "steel"}:
        configured_provider = submit_browser_provider()

    if configured_provider == "steel":
        return _launch_steel_browser(
            playwright,
            headless=headless,
            slow_mo=slow_mo,
            viewport=viewport,
            purpose=purpose,
        )

    # CDP connection: reuse the user's running Chrome instance.
    # Start Chrome with: open -a "Google Chrome" --args --remote-debugging-port=9222
    cdp_url = os.environ.get("JOB_ASSETS_CHROME_CDP_URL", "").strip()
    if cdp_url:
        try:
            browser_obj = playwright.chromium.connect_over_cdp(cdp_url)
            context = browser_obj.contexts[0] if browser_obj.contexts else browser_obj.new_context()
            print(f"Connected to running Chrome via CDP: {cdp_url}", flush=True)
            return ChromiumBrowserSession(
                browser=browser_obj,
                context=context,
                close_target=None,  # Don't close the user's Chrome!
                provider="cdp",
            )
        except Exception as exc:
            print(f"CDP connection to {cdp_url} failed ({exc}), falling back to launch", flush=True)

    attempts = _browser_launch_attempts(
        channel_env_var=channel_env_var,
        executable_env_var=executable_env_var,
        prefer_local_browser=prefer_local_browser,
    )
    aerospace_workspace = submit_browser_aerospace_workspace() if not headless else None
    aerospace_window_ids = _aerospace_chromium_window_ids() if aerospace_workspace else set()

    # Chrome flags for headed submit browsers:
    # --disable-blink-features=AutomationControlled: hide navigator.webdriver flag
    _extra_args: list[str] = []
    _ignore_defaults: list[str] = []
    if not headless:
        _extra_args.append("--disable-blink-features=AutomationControlled")
        # Remove the "Chrome is being controlled by automated test software" infobar
        _ignore_defaults.append("--enable-automation")
        # Suppress "--no-sandbox is unsupported" banner
        _ignore_defaults.append("--no-sandbox")
        # Open on the same monitor as the web app
        _screen_origin = _detect_webapp_screen_origin()
        if _screen_origin:
            print(f"Browser: placing on web app monitor at ({_screen_origin[0]}, {_screen_origin[1]})", flush=True)
            _extra_args.append(f"--window-position={_screen_origin[0]},{_screen_origin[1]}")

    errors: list[str] = []
    first_exception: Exception | None = None
    for label, kwargs in attempts:
        block_reason = _macos_headed_browser_attempt_block_reason(
            label,
            kwargs,
            headless=headless,
            persistent_profile_dir=persistent_profile_dir,
        )
        if block_reason:
            errors.append(f"{label}: {block_reason}")
            continue
        try:
            browser_env = _browser_launch_env(persistent_profile_dir, headless=headless)
            if persistent_profile_dir:
                profile_dir = Path(persistent_profile_dir).expanduser()
                profile_dir.mkdir(parents=True, exist_ok=True)
                print(f"Browser: launching with persistent profile {profile_dir}", flush=True)
                try:
                    context = playwright.chromium.launch_persistent_context(
                        str(profile_dir),
                        headless=headless,
                        slow_mo=slow_mo,
                        viewport=viewport,
                        device_scale_factor=device_scale_factor,
                        env=browser_env,
                        args=_extra_args or None,
                        ignore_default_args=_ignore_defaults or None,
                        **kwargs,
                    )
                    print(f"Browser: persistent context OK ({label})", flush=True)
                    _place_new_chromium_windows_on_workspace(aerospace_workspace, aerospace_window_ids)
                    return ChromiumBrowserSession(context=context, provider="local")
                except Exception as persistent_exc:
                    print(f"Browser: persistent context FAILED ({persistent_exc}), trying fallback", flush=True)
                    if not _should_retry_without_persistent_profile(
                        headless=headless,
                        persistent_profile_dir=persistent_profile_dir,
                    ):
                        raise
                    try:
                        browser = playwright.chromium.launch(
                            headless=headless,
                            slow_mo=slow_mo,
                            env=browser_env,
                            args=_extra_args or None,
                            ignore_default_args=_ignore_defaults or None,
                            **kwargs,
                        )
                    except Exception as non_persistent_exc:
                        raise RuntimeError(
                            "persistent-profile Chromium launch failed and retrying without persistence also "
                            f"failed (persistent: {persistent_exc}; non-persistent: {non_persistent_exc})"
                        ) from non_persistent_exc
                    print("Browser: fallback launch OK (NO persistent profile)", flush=True)
                    context = browser.new_context(
                        viewport=viewport,
                        device_scale_factor=device_scale_factor,
                    )
                    _place_new_chromium_windows_on_workspace(aerospace_workspace, aerospace_window_ids)
                    return ChromiumBrowserSession(
                        browser=browser,
                        context=context,
                        close_target=browser,
                        provider="local",
                    )
            browser = playwright.chromium.launch(
                headless=headless,
                slow_mo=slow_mo,
                env=browser_env,
                args=_extra_args or None,
                ignore_default_args=_ignore_defaults or None,
                **kwargs,
            )
            _place_new_chromium_windows_on_workspace(aerospace_workspace, aerospace_window_ids)
            return ChromiumBrowserSession(browser=browser, provider="local")
        except Exception as exc:  # pragma: no cover - depends on local browser state
            if first_exception is None:
                first_exception = exc
            errors.append(f"{label}: {exc}")

    details = " | ".join(errors) if errors else "no launch attempts were made"
    hint_bits: list[str] = []
    if sys.platform == "darwin" and persistent_profile_dir and not headless:
        hint_bits.append(
            "Headed macOS submit flows require an installed local browser (Chrome/Chromium/Edge) "
            "or `JOB_ASSETS_BROWSER_PROVIDER=steel`; the bundled Chrome for Testing fallback is skipped "
            "because it can abort in AppKit/HIServices."
        )
    else:
        hint_bits.append("Run `uv run playwright install chromium` to install the bundled browser.")
    if persistent_profile_dir:
        hint_bits.append(f"Persistent submit profile path: {Path(persistent_profile_dir).expanduser()}")
    if channel_env_var:
        hint_bits.append(
            f"If you need a local browser instead, set {channel_env_var} to an installed Playwright channel."
        )
    if executable_env_var:
        hint_bits.append(f"You can also point {executable_env_var} at a specific browser executable.")
    raise RuntimeError(
        f"Could not launch a browser for {purpose}. {' '.join(hint_bits)} Launch errors: {details}"
    ) from first_exception
