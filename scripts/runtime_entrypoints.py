#!/usr/bin/env python3
"""Internal python entrypoints that work in repo and packaged runtimes."""

from __future__ import annotations

import runpy
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from app_paths import code_root

PACKAGED_ENTRYPOINT_FLAG = "--job-assets-entrypoint"


def _module_name_for_script(script: str | Path) -> str:
    path = Path(script)
    candidate = path.stem if path.suffix else path.name
    if not candidate:
        raise ValueError(f"Cannot derive module name from script path: {script}")
    return candidate


def python_script_command(
    script: str | Path,
    *script_args: str,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    root = code_root(environ=dict(environ) if environ is not None else None)
    resolved = Path(script)
    if not resolved.is_absolute():
        resolved = root / str(resolved)
    if getattr(sys, "frozen", False):
        return [sys.executable, PACKAGED_ENTRYPOINT_FLAG, _module_name_for_script(resolved), *map(str, script_args)]
    env = environ if environ is not None else {}
    if env.get("JOB_ASSETS_USE_UV_ENTRYPOINTS") and shutil.which("uv"):
        return ["uv", "run", "--project", str(root), "python", str(resolved), *map(str, script_args)]
    return [sys.executable, str(resolved), *map(str, script_args)]


def maybe_dispatch_packaged_entrypoint(argv: Sequence[str] | None = None) -> bool:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != PACKAGED_ENTRYPOINT_FLAG:
        return False
    if len(args) < 2:
        raise SystemExit("Missing packaged entrypoint module name")
    module_name = args[1]
    module_args = args[2:]
    original_argv = sys.argv[:]
    try:
        sys.argv = [module_name, *module_args]
        runpy.run_module(module_name, run_name="__main__")
    finally:
        sys.argv = original_argv
    return True
