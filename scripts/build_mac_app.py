#!/usr/bin/env python3
"""Build the macOS .app bundle with PyInstaller."""

from __future__ import annotations

import argparse
import inspect
import platform
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "Job Application Assistant"
APP_BUNDLE_ID = "com.jobassets.assistant"


def hidden_import_modules() -> list[str]:
    entrypoints = {
        "job_web",
        "job_worker",
        "submit_application",
        "generate_interview_prep",
        "repair_supervisor",
        "openai_provider",
        "codex_exec_wrapper",
        "build_draft_summary",
    }
    entrypoints.update(path.stem for path in (PROJECT_ROOT / "scripts").glob("autofill_*.py"))
    return sorted(entrypoints)


def tls_client_binary_args() -> list[str]:
    import tls_client

    package_root = Path(inspect.getfile(tls_client)).resolve().parent
    dependency_root = package_root / "dependencies"
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        binary_name = "tls-client-arm64.dylib" if machine in {"arm64", "aarch64"} else "tls-client-x86.dylib"
    elif sys.platform.startswith("linux"):
        binary_name = "tls-client-arm64.so" if machine in {"arm64", "aarch64"} else "tls-client-amd64.so"
    elif sys.platform.startswith("win"):
        binary_name = "tls-client-64.dll" if "64" in machine else "tls-client-32.dll"
    else:
        return []

    binary_path = dependency_root / binary_name
    if not binary_path.exists():
        raise FileNotFoundError(f"Missing tls_client dependency binary: {binary_path}")
    return ["--add-binary", f"{binary_path}:tls_client/dependencies"]


def pyinstaller_args(*, distpath: Path | None = None, workpath: Path | None = None) -> list[str]:
    args = [
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        APP_NAME,
        "--osx-bundle-identifier",
        APP_BUNDLE_ID,
        "--paths",
        str(PROJECT_ROOT),
        "--paths",
        str(PROJECT_ROOT / "scripts"),
        "--collect-submodules",
        "fastapi",
        "--collect-submodules",
        "starlette",
        "--collect-submodules",
        "uvicorn",
        "--add-data",
        f"{PROJECT_ROOT / 'assets'}:assets",
        "--add-data",
        f"{PROJECT_ROOT / 'scripts' / 'prompts'}:scripts/prompts",
        "--add-data",
        f"{PROJECT_ROOT / 'scripts' / 'static'}:scripts/static",
        "--add-data",
        f"{PROJECT_ROOT / 'governance' / 'runtime-policy.json'}:governance",
        str(PROJECT_ROOT / "scripts" / "mac_app_launcher.py"),
    ]
    args.extend(tls_client_binary_args())
    for module_name in hidden_import_modules():
        args.extend(["--hidden-import", module_name])
    if distpath is not None:
        args.extend(["--distpath", str(distpath)])
    if workpath is not None:
        args.extend(["--workpath", str(workpath), "--specpath", str(workpath)])
    return args


def app_bundle_path(distpath: Path) -> Path:
    return distpath / f"{APP_NAME}.app"


def build_app(*, distpath: Path, workpath: Path) -> Path:
    from PyInstaller.__main__ import run

    run(pyinstaller_args(distpath=distpath, workpath=workpath))
    return app_bundle_path(distpath)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the macOS app bundle with PyInstaller.")
    parser.add_argument("--distpath", type=Path, default=PROJECT_ROOT / "dist")
    parser.add_argument("--workpath", type=Path, default=PROJECT_ROOT / "build" / "pyinstaller")
    args = parser.parse_args()

    build_app(distpath=args.distpath, workpath=args.workpath)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
