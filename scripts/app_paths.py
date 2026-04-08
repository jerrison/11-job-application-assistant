#!/usr/bin/env python3
"""Runtime path helpers for repo and packaged launch modes."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_HOME_ENV = "JOB_ASSETS_APP_HOME"
CODE_ROOT_ENV = "JOB_ASSETS_CODE_ROOT"
PACKAGED_RUNTIME_ENV = "JOB_ASSETS_PACKAGED"
APPLICATION_SUPPORT_DIRNAME = "Job Assets"
_SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_CODE_ROOT = _SCRIPT_DIR.parent


def _env(environ: dict[str, str] | None = None) -> dict[str, str]:
    return environ if environ is not None else os.environ


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "on"}


def is_packaged_runtime(*, environ: dict[str, str] | None = None) -> bool:
    env = _env(environ)
    if _truthy(env.get(PACKAGED_RUNTIME_ENV)):
        return True
    return bool(getattr(sys, "frozen", False))


def code_root(*, environ: dict[str, str] | None = None) -> Path:
    env = _env(environ)
    explicit = env.get(CODE_ROOT_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if is_packaged_runtime(environ=env):
        bundle_root = getattr(sys, "_MEIPASS", "")
        if bundle_root:
            return Path(bundle_root)
    return _DEFAULT_CODE_ROOT


def app_home(*, environ: dict[str, str] | None = None) -> Path:
    env = _env(environ)
    explicit = env.get(APP_HOME_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if is_packaged_runtime(environ=env):
        return Path.home() / "Library" / "Application Support" / APPLICATION_SUPPORT_DIRNAME
    return code_root(environ=env)


def jobs_db_path(*, environ: dict[str, str] | None = None) -> Path:
    return app_home(environ=environ) / "jobs.db"


def output_root(*, environ: dict[str, str] | None = None) -> Path:
    return app_home(environ=environ) / "output"


def materials_root(*, environ: dict[str, str] | None = None) -> Path:
    return app_home(environ=environ)


def env_file_paths(*, environ: dict[str, str] | None = None) -> list[Path]:
    root = app_home(environ=environ)
    return [root / ".env", root / ".env.local"]


def uv_cache_dir(*, environ: dict[str, str] | None = None) -> Path:
    return app_home(environ=environ) / ".uv-cache"


def browser_root(*, environ: dict[str, str] | None = None) -> Path:
    env = _env(environ)
    if env.get(APP_HOME_ENV, "").strip() or is_packaged_runtime(environ=env):
        return app_home(environ=env) / ".job-assets"
    return Path.home() / ".job-assets"
