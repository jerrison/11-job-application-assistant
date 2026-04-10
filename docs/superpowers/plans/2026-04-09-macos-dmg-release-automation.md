# macOS DMG Release Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a durable unsigned DMG packaging path, attach that DMG to future GitHub releases automatically, and backfill the current `v1.0.0` release.

**Architecture:** Keep `scripts/build_mac_app.py` as the source of truth for the PyInstaller `.app` bundle, add a separate `scripts/build_mac_dmg.py` that wraps that bundle with `hdiutil`, and keep GitHub Actions narrow by having a dedicated macOS release-asset workflow call the local packaging scripts and upload the resulting DMG with `gh release upload --clobber`.

**Tech Stack:** Python 3.12+, argparse/pathlib/tempfile/shutil/subprocess, PyInstaller, macOS `hdiutil`, GitHub Actions, GitHub CLI, unittest/pytest

---

## File Structure

- `scripts/build_mac_app.py`: existing PyInstaller bundle builder; add only the minimum reusable helper surface the DMG script needs.
- `tests/test_build_mac_app.py`: focused unit-style coverage for `.app` builder helpers and arguments.
- `scripts/build_mac_dmg.py`: new unsigned DMG packager that stages the built app and shells out to `hdiutil`.
- `tests/test_build_mac_dmg.py`: direct unit-style coverage for DMG path naming, missing-app failures, and `hdiutil` command construction.
- `.github/workflows/release-macos-dmg.yml`: macOS-only release asset workflow for future published releases and manual backfills.
- `tests/test_ci_workflow.py`: structural assertions that the new workflow exists, supports published-release and manual-tag flows, and uploads with `--clobber`.
- `README.md`: public surface overview; mention release DMGs in the packaged macOS section.
- `docs/macos-app.md`: local build/run documentation; add DMG build steps, output contract, and unsigned Gatekeeper caveat.

### Task 1: Extract Reusable `.app` Build Helpers

**Files:**
- Modify: `tests/test_build_mac_app.py`
- Modify: `scripts/build_mac_app.py`

- [ ] **Step 1: Write the failing helper test**

```python
    def test_app_bundle_path_uses_distpath_and_app_name(self):
        build_mac_app = load_module("build_mac_app_bundle_path", "scripts/build_mac_app.py")
        distpath = PROJECT_ROOT / "dist-test"

        self.assertEqual(
            build_mac_app.app_bundle_path(distpath),
            distpath / f"{build_mac_app.APP_NAME}.app",
        )
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `uv run python -m pytest tests/test_build_mac_app.py -k app_bundle_path_uses_distpath_and_app_name -v`

Expected: FAIL with `AttributeError: module 'build_mac_app_bundle_path' has no attribute 'app_bundle_path'`.

- [ ] **Step 3: Add the minimal reusable implementation**

```python
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
```

Keep all existing PyInstaller arguments intact. Do not add DMG logic to this file.

- [ ] **Step 4: Run the full `.app` builder test file**

Run: `uv run python -m pytest tests/test_build_mac_app.py -v`

Expected: PASS

- [ ] **Step 5: Commit the helper extraction**

```bash
git add tests/test_build_mac_app.py scripts/build_mac_app.py
git commit -m "refactor: expose reusable mac app build helpers"
```

### Task 2: Add Direct DMG Builder Coverage And Implementation

**Files:**
- Create: `tests/test_build_mac_dmg.py`
- Create: `scripts/build_mac_dmg.py`

- [ ] **Step 1: Write the failing DMG builder tests**

```python
import importlib.util
import sys
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
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class BuildMacDmgTests(unittest.TestCase):
    def test_dmg_path_for_tag_uses_release_naming_contract(self):
        module = load_module("build_mac_dmg_paths", "scripts/build_mac_dmg.py")
        distpath = PROJECT_ROOT / "dist"

        self.assertEqual(
            module.dmg_path_for_tag("v1.0.0", distpath=distpath),
            distpath / "Job-Application-Assistant-v1.0.0-macos.dmg",
        )

    def test_build_dmg_raises_when_app_is_missing_without_build_flag(self):
        module = load_module("build_mac_dmg_missing_app", "scripts/build_mac_dmg.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaises(FileNotFoundError):
                module.build_dmg(
                    tag="v1.0.0",
                    app_path=root / "dist" / "Job Application Assistant.app",
                    distpath=root / "dist",
                    workpath=root / "build" / "dmg",
                    build_app_if_missing=False,
                )

    def test_build_dmg_stages_app_and_calls_hdiutil(self):
        module = load_module("build_mac_dmg_hdiutil", "scripts/build_mac_dmg.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app_path = root / "dist" / "Job Application Assistant.app"
            app_path.mkdir(parents=True)

            with mock.patch("subprocess.run") as run_mock:
                output_path = module.build_dmg(
                    tag="v1.0.0",
                    app_path=app_path,
                    distpath=root / "dist",
                    workpath=root / "build" / "dmg",
                    build_app_if_missing=False,
                )

            self.assertEqual(
                output_path,
                root / "dist" / "Job-Application-Assistant-v1.0.0-macos.dmg",
            )
            run_mock.assert_called_once()
            args = run_mock.call_args.args[0]
            self.assertEqual(args[:2], ["hdiutil", "create"])
            self.assertIn("-srcfolder", args)
            self.assertIn("-volname", args)
            self.assertEqual(args[-1], str(output_path))
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run python -m pytest tests/test_build_mac_dmg.py -v`

Expected: FAIL because `scripts/build_mac_dmg.py` does not exist yet.

- [ ] **Step 3: Implement the narrow DMG packager**

```python
#!/usr/bin/env python3
"""Build an unsigned macOS DMG from the packaged app bundle."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

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
```

Keep the artifact unsigned and read-only. Do not add signing, notarization, or release-upload logic here.

- [ ] **Step 4: Run the DMG builder tests to verify they pass**

Run: `uv run python -m pytest tests/test_build_mac_dmg.py -v`

Expected: PASS

- [ ] **Step 5: Commit the DMG packager**

```bash
git add tests/test_build_mac_dmg.py scripts/build_mac_dmg.py
git commit -m "feat: add macos dmg build script"
```

### Task 3: Add The Release Asset Workflow

**Files:**
- Modify: `tests/test_ci_workflow.py`
- Create: `.github/workflows/release-macos-dmg.yml`

- [ ] **Step 1: Write the failing workflow contract test**

```python
    def test_release_macos_dmg_workflow_supports_publish_and_backfill(self):
        workflow_path = PROJECT_ROOT / ".github" / "workflows" / "release-macos-dmg.yml"
        workflow = workflow_path.read_text(encoding="utf-8")

        self.assertIn("release:", workflow)
        self.assertIn("- published", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("tag:", workflow)
        self.assertIn("runs-on: macos-latest", workflow)
        self.assertIn("permissions:", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("scripts/build_mac_dmg.py --tag", workflow)
        self.assertIn("gh release upload", workflow)
        self.assertIn("--clobber", workflow)
```

- [ ] **Step 2: Run the focused workflow test to verify it fails**

Run: `uv run python -m pytest tests/test_ci_workflow.py -k release_macos_dmg -v`

Expected: FAIL with `FileNotFoundError` because `.github/workflows/release-macos-dmg.yml` does not exist yet.

- [ ] **Step 3: Add the workflow**

```yaml
name: Release macOS DMG

on:
  release:
    types:
      - published
  workflow_dispatch:
    inputs:
      tag:
        description: "Release tag to build and upload"
        required: true
        type: string

permissions:
  contents: write

jobs:
  build-and-upload-dmg:
    runs-on: macos-latest
    env:
      RELEASE_TAG: ${{ github.event.release.tag_name || inputs.tag }}
      GH_TOKEN: ${{ github.token }}
    steps:
      - name: Check out repository
        uses: actions/checkout@v4
        with:
          ref: ${{ github.event.release.tag_name || inputs.tag }}

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Set up uv
        uses: astral-sh/setup-uv@v4

      - name: Build app bundle
        run: uv run --with pyinstaller python scripts/build_mac_app.py

      - name: Build DMG
        run: uv run python scripts/build_mac_dmg.py --tag "$RELEASE_TAG"

      - name: Verify release exists
        run: gh release view "$RELEASE_TAG" --repo "$GITHUB_REPOSITORY"

      - name: Upload DMG asset
        run: |
          gh release upload \
            "$RELEASE_TAG" \
            "dist/Job-Application-Assistant-$RELEASE_TAG-macos.dmg" \
            --repo "$GITHUB_REPOSITORY" \
            --clobber
```

Keep this workflow separate from `ci.yml`. Do not create releases or generate notes here.

- [ ] **Step 4: Run the focused workflow test to verify it passes**

Run: `uv run python -m pytest tests/test_ci_workflow.py -k release_macos_dmg -v`

Expected: PASS

- [ ] **Step 5: Commit the workflow**

```bash
git add tests/test_ci_workflow.py .github/workflows/release-macos-dmg.yml
git commit -m "ci: attach macos dmg to releases"
```

### Task 4: Document The DMG Build And Distribution Contract

**Files:**
- Modify: `tests/test_ci_workflow.py`
- Modify: `README.md`
- Modify: `docs/macos-app.md`

- [ ] **Step 1: Write the failing documentation contract test**

```python
    def test_macos_docs_cover_dmg_build_and_unsigned_release_artifact(self):
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        macos_doc = (PROJECT_ROOT / "docs" / "macos-app.md").read_text(encoding="utf-8")

        self.assertIn("build_mac_dmg.py", readme)
        self.assertIn("GitHub releases", readme)
        self.assertIn("build_mac_dmg.py", macos_doc)
        self.assertIn("Job-Application-Assistant-<tag>-macos.dmg", macos_doc)
        self.assertIn("unsigned", macos_doc)
        self.assertIn("Gatekeeper", macos_doc)
```

- [ ] **Step 2: Run the focused documentation test to verify it fails**

Run: `uv run python -m pytest tests/test_ci_workflow.py -k macos_docs_cover_dmg -v`

Expected: FAIL because the current docs only describe `.app` bundle creation.

- [ ] **Step 3: Update the public docs**

```markdown
## Packaged macOS App

Build the desktop app bundle with PyInstaller:

~~~bash
uv run --with pyinstaller python scripts/build_mac_app.py
uv run python scripts/build_mac_dmg.py --tag v1.0.0
~~~

GitHub releases can include a packaged macOS DMG:

- `dist/Job-Application-Assistant-v1.0.0-macos.dmg`
```

and in `docs/macos-app.md`:

```markdown
Build the `.app` bundle with PyInstaller:

~~~bash
uv run --with pyinstaller python scripts/build_mac_app.py
~~~

Build the unsigned `.dmg` release artifact:

~~~bash
uv run python scripts/build_mac_dmg.py --tag v1.0.0
~~~

DMG output:

~~~text
dist/Job-Application-Assistant-v1.0.0-macos.dmg
~~~

This DMG is unsigned in the current release model. macOS Gatekeeper may warn on
first open until signing and notarization are added in a later pass.
```

- [ ] **Step 4: Run the focused documentation test to verify it passes**

Run: `uv run python -m pytest tests/test_ci_workflow.py -k macos_docs_cover_dmg -v`

Expected: PASS

- [ ] **Step 5: Commit the docs**

```bash
git add tests/test_ci_workflow.py README.md docs/macos-app.md
git commit -m "docs: describe macos dmg release artifacts"
```

### Task 5: Verify Packaging End-To-End And Backfill `v1.0.0`

**Files:**
- Modify: `scripts/build_mac_app.py`
- Create: `scripts/build_mac_dmg.py`
- Modify: `tests/test_build_mac_app.py`
- Create: `tests/test_build_mac_dmg.py`
- Modify: `tests/test_ci_workflow.py`
- Create: `.github/workflows/release-macos-dmg.yml`
- Modify: `README.md`
- Modify: `docs/macos-app.md`

- [ ] **Step 1: Run the focused packaging and workflow tests**

```bash
uv run python -m pytest \
  tests/test_build_mac_app.py \
  tests/test_build_mac_dmg.py \
  tests/test_ci_workflow.py -v
```

Expected: PASS

- [ ] **Step 2: Build the `.app` and `.dmg` locally**

```bash
uv run --with pyinstaller python scripts/build_mac_app.py
uv run python scripts/build_mac_dmg.py --tag v1.0.0
```

Expected:
- `dist/Job Application Assistant.app` exists
- `dist/Job-Application-Assistant-v1.0.0-macos.dmg` exists

- [ ] **Step 3: Smoke-mount the DMG and confirm the app bundle is inside**

```bash
MOUNT_OUTPUT="$(hdiutil attach "dist/Job-Application-Assistant-v1.0.0-macos.dmg")"
echo "$MOUNT_OUTPUT"
test -d "/Volumes/Job Application Assistant/Job Application Assistant.app"
hdiutil detach "/Volumes/Job Application Assistant"
```

Expected:
- `hdiutil attach` prints a mounted `/Volumes/Job Application Assistant`
- `test -d` exits zero
- `hdiutil detach` succeeds

- [ ] **Step 4: Run the repo verification suite**

```bash
uv run ruff check scripts/ tests/
uv run python scripts/check_architecture.py
uv run python scripts/check_agent_docs.py
uv run python scripts/sync_agent_files.py --check
uv run python -m pytest tests/ -v
```

Expected: PASS

- [ ] **Step 5: Confirm the local worktree state before publication**

```bash
git status --short
```

Expected: only the intended packaging, workflow, test, and doc changes remain.

- [ ] **Step 6: After the workflow file is on `main`, dispatch the backfill run**

```bash
gh workflow run release-macos-dmg.yml \
  --repo jerrison/11-job-application-assistant \
  --ref main \
  -f tag=v1.0.0
```

Expected: GitHub accepts the manual run request and returns without error.

- [ ] **Step 7: Wait for the workflow and confirm the release asset**

```bash
RUN_ID="$(gh run list \
  --repo jerrison/11-job-application-assistant \
  --workflow release-macos-dmg.yml \
  --limit 1 \
  --json databaseId \
  --jq '.[0].databaseId')"
gh run watch "$RUN_ID" --repo jerrison/11-job-application-assistant
gh release view v1.0.0 --repo jerrison/11-job-application-assistant
```

Expected:
- `gh run watch` finishes with a successful run
- `gh release view` lists `Job-Application-Assistant-v1.0.0-macos.dmg` as a release asset
