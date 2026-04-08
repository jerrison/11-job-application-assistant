#!/usr/bin/env python3
"""Launch the packaged macOS app by serving the local web UI."""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import MutableMapping
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from app_paths import (
    app_home,
    browser_root,
    env_file_paths,
    logs_root,
    output_root,
    traces_root,
    uv_cache_dir,
)
from project_env import load_project_env
from runtime_entrypoints import maybe_dispatch_packaged_entrypoint
from runtime_policy import ensure_action_allowed
from runtime_trace import configure_runtime_trace, emit_trace

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8420
DEFAULT_WORKERS = 16
log = logging.getLogger(__name__)


def prepare_packaged_environment(*, environ: MutableMapping[str, str] | None = None) -> MutableMapping[str, str]:
    target = environ if environ is not None else os.environ
    target["JOB_ASSETS_PACKAGED"] = "1"
    root = app_home(environ=target)
    for path in (
        root,
        output_root(environ=target),
        browser_root(environ=target),
        logs_root(environ=target),
        traces_root(environ=target),
        uv_cache_dir(environ=target),
    ):
        path.mkdir(parents=True, exist_ok=True)
    target.setdefault("UV_CACHE_DIR", str(uv_cache_dir(environ=target)))
    load_project_env(files=env_file_paths(environ=target), environ=target)
    configure_runtime_trace(environ=target, replace=True)
    return target


def _port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def resolve_server_port(host: str, preferred_port: int, *, search_limit: int = 25) -> int:
    for port in range(preferred_port, preferred_port + search_limit + 1):
        if _port_available(host, port):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def build_app_url(host: str, port: int) -> str:
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{browser_host}:{port}/"


def _wait_for_server(host: str, port: int, *, timeout_seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def _configure_logging(target_env: MutableMapping[str, str]) -> None:
    log_path = logs_root(environ=target_env) / "mac-app-launcher.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8")],
    )


def main() -> int:
    target_env = prepare_packaged_environment()
    if maybe_dispatch_packaged_entrypoint():
        return 0

    parser = argparse.ArgumentParser(description="Launch the packaged Job Application Assistant macOS app.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--with-workers", action="store_true", help="Start the shared worker pool on launch.")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open the browser UI.")
    args = parser.parse_args()

    _configure_logging(target_env)
    ensure_action_allowed(
        "mac_app_launch",
        explicit_approval=True,
        metadata={"surface": "mac_app", "with_workers": args.with_workers},
        environ=target_env,
    )

    port = resolve_server_port(args.host, args.port)
    app_url = build_app_url(args.host, port)

    if not args.no_browser:
        def _open_browser() -> None:
            if _wait_for_server(args.host, port):
                webbrowser.open(app_url, new=1)

        threading.Thread(target=_open_browser, daemon=True).start()

    emit_trace(
        "mac_app_launch",
        action="mac_app_launch",
        metadata={"surface": "mac_app", "url": app_url, "with_workers": args.with_workers, "workers": args.workers},
        environ=target_env,
    )

    import job_web
    import uvicorn

    job_web._configured_num_workers = args.workers
    if args.with_workers:
        job_web._auto_start_workers = True
    app = job_web.create_app()
    try:
        uvicorn.run(app, host=args.host, port=port)
    finally:
        emit_trace(
            "mac_app_exit",
            action="mac_app_launch",
            metadata={"surface": "mac_app", "url": app_url, "with_workers": args.with_workers},
            environ=target_env,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
