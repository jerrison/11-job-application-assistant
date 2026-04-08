#!/usr/bin/env python3
"""Load local, gitignored project environment files."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from collections.abc import Mapping, MutableMapping
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from app_paths import code_root, env_file_paths, uv_cache_dir

PROJECT_ROOT = code_root()
PROJECT_ENV_FILES = env_file_paths()
ENV_LINE_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def _parse_env_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        quote = value[0]
        inner = value[1:-1]
        if quote == '"':
            inner = bytes(inner, "utf-8").decode("unicode_escape")
        return inner
    return value


def parse_env_file(path: Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    if not path.exists():
        return parsed

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = ENV_LINE_RE.match(line)
        if not match:
            continue
        key, raw_value = match.groups()
        parsed[key] = _parse_env_value(raw_value)
    return parsed


def load_project_env(
    *,
    files: list[Path] | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    target = environ if environ is not None else os.environ
    locked_keys = set(target.keys())
    loaded: dict[str, str] = {}
    for path in files or PROJECT_ENV_FILES:
        for key, value in parse_env_file(path).items():
            if key in locked_keys:
                continue
            target[key] = value
            loaded[key] = value
    if "UV_CACHE_DIR" not in target:
        effective_env = dict(os.environ)
        effective_env.update(target)
        default_uv_cache_dir = str(uv_cache_dir(environ=effective_env))
        target["UV_CACHE_DIR"] = default_uv_cache_dir
        loaded["UV_CACHE_DIR"] = default_uv_cache_dir
    return loaded


def shell_exports(
    *,
    files: list[Path] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    target = dict(environ or os.environ)
    return load_project_env(files=files, environ=target)


def main() -> int:
    parser = argparse.ArgumentParser(description="Emit shell exports for local project env files.")
    parser.add_argument(
        "--shell",
        action="store_true",
        help="Print shell export statements for values loaded from .env and .env.local.",
    )
    args = parser.parse_args()

    loaded = load_project_env()
    if args.shell:
        for key, value in loaded.items():
            print(f"export {key}={shlex.quote(value)}")
        return 0

    for key, value in loaded.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
