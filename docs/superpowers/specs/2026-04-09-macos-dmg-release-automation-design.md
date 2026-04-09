# Durable macOS DMG Release Automation Design

## Goal

Add durable repo support for macOS release artifacts by:

- producing an unsigned `.dmg` from the existing PyInstaller-built `.app`
- backfilling the current `v1.0.0` GitHub release with that DMG
- automatically attaching the same DMG artifact to future GitHub releases

The repo already builds `dist/Job Application Assistant.app`. This design adds
the missing distribution layer without changing the draft-first runtime model or
introducing signing/notarization work.

## Current Context

- `scripts/build_mac_app.py` is the current source of truth for building the
  packaged macOS `.app` bundle with PyInstaller.
- `docs/macos-app.md` documents the `.app` build path only.
- `docs/exec-plans/completed/2026-04-08-macos-packaging-and-runtime-governance.md`
  explicitly lists signing, notarization, and release automation as remaining
  work.
- The repo now has a public `v1.0.0` GitHub release, but it has no packaged
  macOS asset attached.

## Goals

- Keep the existing `.app` builder intact as the package-source-of-truth.
- Add a local scriptable path for building a deterministic unsigned DMG.
- Add a GitHub Actions workflow that can:
  - backfill an existing release by tag
  - attach a DMG to future published releases automatically
- Keep the packaging contract inspectable and reproducible both locally and in
  CI.

## Non-Goals

- code signing
- notarization
- release-note generation
- automatic release creation
- Windows/Linux installer packaging

## Recommended Approach

Use a script-first hybrid model:

1. Keep `scripts/build_mac_app.py` focused on creating
   `dist/Job Application Assistant.app`.
2. Add a separate `scripts/build_mac_dmg.py` that wraps the built app into an
   unsigned DMG using macOS-native tooling.
3. Add a dedicated GitHub Actions workflow that resolves a target tag, builds
   the app and DMG on a macOS runner, and uploads the DMG to the matching
   release.

This keeps local packaging and CI packaging on the same contract while avoiding
overloading the existing app builder or coupling release creation to artifact
generation.

## Packaging Boundary

### `scripts/build_mac_app.py`

This file remains responsible only for building the `.app` bundle. It may gain
small reusable exports if needed, such as app-name constants or output-path
helpers, but it should not absorb DMG logic.

### New `scripts/build_mac_dmg.py`

Add a separate packaging script with a narrow contract:

- inputs:
  - `--tag <tag>` for asset naming, such as `v1.0.0`
  - optional `--app-path` to reuse an existing `.app`
  - optional `--distpath` and `--workpath` overrides
  - optional `--build-app` flag to build the `.app` first if it is missing
- behavior:
  - resolve the source app bundle
  - fail if the app bundle is missing and `--build-app` was not requested
  - create a temporary staging directory
  - copy `Job Application Assistant.app` into the staging root
  - run `hdiutil create` to build an unsigned read-only DMG
  - write a deterministic output such as
    `dist/Job-Application-Assistant-v1.0.0-macos.dmg`
- outputs:
  - print the created DMG path
  - exit nonzero on staging or `hdiutil` failure

The script should keep all mutable work under temp directories or the selected
`dist/` and `work/` locations. It should not mutate tracked repo files.

## Release Automation

Add a new workflow at
`.github/workflows/release-macos-dmg.yml`, with a single responsibility:
attach a DMG asset to a GitHub release.

### Triggers

- `release` with type `published`
- `workflow_dispatch` with required input `tag`

### Runner And Permissions

- `runs-on: macos-latest`
- `permissions: contents: write`

### Workflow Behavior

1. Resolve the tag:
   - `github.event.release.tag_name` for published releases
   - `inputs.tag` for manual backfills
2. Check out the repo at that tag.
3. Set up Python and `uv`.
4. Build the app bundle:
   - `uv run --with pyinstaller python scripts/build_mac_app.py`
5. Build the DMG:
   - `uv run python scripts/build_mac_dmg.py --tag "$TAG"`
6. Upload the DMG to the matching release:
   - use `gh release upload "$TAG" <artifact> --clobber`

Using `--clobber` keeps reruns and backfills idempotent. The workflow should
fail loudly if the tag does not map to an existing release during manual
backfill.

### Backfill

The same workflow handles the existing `v1.0.0` release via `workflow_dispatch`
without special-case code. The operator provides `v1.0.0`, and the workflow
builds and uploads the artifact to that release.

## Artifact Contract

The public release artifact name should be deterministic and stable:

- `Job-Application-Assistant-<tag>-macos.dmg`

This keeps release pages predictable and avoids exposing CI temp-directory
structure in artifact names.

## Testing And Verification

### Unit Tests

Add direct tests for the new DMG script in
`tests/test_build_mac_dmg.py`, covering:

- deterministic artifact naming from a tag
- failure when the source `.app` is missing and app build was not requested
- `hdiutil` command construction
- staging-root behavior
- reuse of an explicitly provided `.app` path

Mock subprocess execution so tests assert behavior without requiring real DMG
creation.

### Existing Build Test Coverage

Keep `tests/test_build_mac_app.py` focused on `.app` builder arguments and
runtime assets. Only extend it if the app builder needs small reusable exports.

### Local Verification

Recommended local checks for the feature:

```bash
uv run python -m pytest tests/test_build_mac_app.py tests/test_build_mac_dmg.py -q
uv run --with pyinstaller python scripts/build_mac_app.py
uv run python scripts/build_mac_dmg.py --tag v1.0.0
```

Optional smoke validation for the produced artifact:

```bash
hdiutil attach "dist/Job-Application-Assistant-v1.0.0-macos.dmg"
```

That smoke step should confirm the mounted volume contains
`Job Application Assistant.app`, then detach it.

## Documentation Updates

Update:

- `README.md`
  - mention that GitHub releases can include a packaged macOS DMG
- `docs/macos-app.md`
  - add local DMG build instructions
  - document the artifact name and output path
  - note that DMGs are unsigned in this phase and may show Gatekeeper warnings

No separate release-process doc is required unless the workflow semantics become
too complex for the existing docs.

## Failure Model

- fail hard if the `.app` bundle does not exist and cannot be built
- fail hard if `hdiutil` returns nonzero
- fail hard if a manual backfill tag does not correspond to an existing release
- do not silently skip upload on asset conflicts; rely on explicit `--clobber`
  semantics
- keep signing and notarization out of scope for this pass

## Implementation Notes

- Prefer small reusable helpers over a new abstraction layer.
- If both scripts need shared constants such as app name or default dist paths,
  reuse the existing exports from `scripts/build_mac_app.py` rather than
  introducing a new packaging module prematurely.
- Keep the release workflow separate from `ci.yml` so packaging failures do not
  affect normal Linux test jobs or PR feedback loops.

## Success Criteria

This work is complete when:

1. A local command path can build an unsigned DMG from the packaged `.app`.
2. The new workflow can backfill `v1.0.0` with that DMG.
3. Publishing a future GitHub release automatically attaches the same DMG
   artifact.
4. Docs describe the local DMG build path and the unsigned-distribution caveat.
