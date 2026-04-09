#!/usr/bin/env python3
"""Build an unsigned macOS DMG from the packaged app bundle."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from build_mac_app import APP_NAME, PROJECT_ROOT, app_bundle_path, build_app


def dmg_path_for_tag(tag: str, *, distpath: Path) -> Path:
    return distpath / f"{APP_NAME.replace(' ', '-')}-{tag}-macos.dmg"


def build_dmg(
    *,
    tag: str,
    app_path: Path | None,
    distpath: Path,
    workpath: Path,
    build_app_if_missing: bool,
) -> Path:
    resolved_app_path = app_path or app_bundle_path(distpath)
    if not resolved_app_path.exists():
        if not build_app_if_missing:
            raise FileNotFoundError(f"Missing app bundle: {resolved_app_path}")
        resolved_app_path = build_app(
            distpath=distpath,
            workpath=workpath / "pyinstaller",
        )

    output_path = dmg_path_for_tag(tag, distpath=distpath)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workpath.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=workpath, prefix="macos-dmg-") as tmpdir:
        staging_root = Path(tmpdir) / "root"
        staged_app_path = staging_root / resolved_app_path.name
        staging_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(resolved_app_path, staged_app_path)
        subprocess.run(
            [
                "hdiutil",
                "create",
                "-ov",
                "-volname",
                APP_NAME,
                "-srcfolder",
                str(staging_root),
                "-fs",
                "HFS+",
                "-format",
                "UDZO",
                str(output_path),
            ],
            check=True,
        )

    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an unsigned macOS DMG.")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--app-path", type=Path)
    parser.add_argument("--distpath", type=Path, default=PROJECT_ROOT / "dist")
    parser.add_argument("--workpath", type=Path, default=PROJECT_ROOT / "build" / "dmg")
    parser.add_argument("--build-app", action="store_true")
    args = parser.parse_args()

    dmg_path = build_dmg(
        tag=args.tag,
        app_path=args.app_path,
        distpath=args.distpath,
        workpath=args.workpath,
        build_app_if_missing=args.build_app,
    )
    print(dmg_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
