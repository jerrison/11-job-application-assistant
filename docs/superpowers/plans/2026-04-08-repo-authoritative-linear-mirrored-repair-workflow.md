# Repo-Authoritative, Linear-Mirrored Repair Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Phase 1, Phase 2, and Phase 3 backlog handling repo-authoritative so items are only considered handled after repo-native transitions record current-wave repair attempts or blocker proof, while keeping Linear synchronized as the truthful human-facing tracking surface.

**Architecture:** Keep immutable snapshots and append-only ledgers as the per-run source of truth, and add a shared sweep controller plus repair-wave fingerprinting on top of them instead of inventing a second persistence system. Phase 1 gets the same snapshot/ledger discipline as stopped and draft sweeps, `draft_web` routes through the shared controller, and verifier gates expand from "coverage exists" to "current-wave attempt/proof and Linear sync exist."

**Tech Stack:** Python 3.14, SQLite-backed repo state, TSV ledgers, JSON trace/manifests, FastAPI, pytest, Ruff, `uv run python`, optional `httpx` for direct Linear API access

**Spec:** `docs/superpowers/specs/2026-04-08-repo-authoritative-linear-mirrored-repair-workflow-design.md`

**Existing code:** `scripts/init_backlog_sweep.py`, `scripts/check_backlog_sweep.py`, `scripts/verify_active_sweep.py`, `scripts/backlog_sweep_recorder.py`, `scripts/record_backlog_sweep_result.py`, `scripts/draft_web.py`, `scripts/job_db.py`, `tests/test_backlog_sweep_harness.py`, `tests/test_check_backlog_sweep.py`, `tests/test_draft_web.py`

---

## Purpose / Big Picture

After this lands, the repo will have one enforced workflow for Linear Todo issues, stopped jobs, and drafted-job review defects. An agent will no longer be able to "make progress" by editing Linear, parking a row, or describing a fix without a current-wave repo-native transition. A fresh session should be able to resume an in-flight sweep entirely from repo state, and final completion should fail unless Linear is synchronized to the latest repo-known truth.

---

## Context and Orientation

- **Docs to read:** `AGENTS.md`, `docs/operational-rules.md`, `docs/backlog-sweep.md`, `docs/runbooks/repeatable-backlog-sweep.md`, `docs/worker-pipeline-patterns.md`, `docs/superpowers/specs/2026-04-08-repo-authoritative-linear-mirrored-repair-workflow-design.md`
- **Primary files:** `scripts/init_backlog_sweep.py`, `scripts/check_backlog_sweep.py`, `scripts/verify_active_sweep.py`, `scripts/backlog_sweep_recorder.py`, `scripts/draft_web.py`, `scripts/job_db.py`
- **Constraints:** Always keep `--draft` fail-closed semantics; screenshots remain the source of truth; repo-native transitions must write durable local state first and only then mirror to Linear; do not make Linear the enforcement layer; do not rely on transitive dependencies without declaring them in `pyproject.toml`

---

## Milestones

1. **Milestone 1:** Phase 1 snapshot/ledger and repair-wave fingerprinting exist, and the active-manifest flow can bootstrap or resume all three phases with deterministic current-wave metadata. Verification: `uv run python -m pytest tests/test_backlog_sweep_harness.py -v` plus `uv run python -m pytest tests/test_sweep_repair_wave.py -v`
2. **Milestone 2:** Shared controller actions can record repair attempts, blocker states, and verification transitions for all phases, and `draft_web` uses the shared controller rather than a phase-specific shortcut. Verification: `uv run python -m pytest tests/test_sweep_controller.py -v` plus `uv run python -m pytest tests/test_draft_web.py -v`
3. **Milestone 3:** The backlog verifier fails unless every Phase 1/2/3 row has a current-wave terminal state and synced Linear mirror status, with a direct Linear API backend when `LINEAR_API_TOKEN` is present and a repo-owned pending-sync queue otherwise. Verification: `uv run python -m pytest tests/test_check_backlog_sweep.py -v`, `uv run python -m pytest tests/test_sweep_linear_sync.py -v`, and `uv run python scripts/verify_active_sweep.py --active`

---

## Progress

| Step | Status | Updated |
|------|--------|---------|
| Chunk 1 / Task 1 | Not started | |
| Chunk 1 / Task 2 | Not started | |
| Chunk 2 / Task 3 | Not started | |
| Chunk 2 / Task 4 | Not started | |
| Chunk 3 / Task 5 | Not started | |
| Chunk 3 / Task 6 | Not started | |
| Chunk 4 / Task 7 | Not started | |
| Chunk 4 / Task 8 | Not started | |

---

## File Structure

### New files:
- `docs/templates/phase1-linear-results-template.tsv` — append-only Phase 1 results ledger header
- `scripts/sweep_repair_wave.py` — current repair-wave fingerprinting helpers
- `scripts/sweep_controller.py` — shared state machine for Phase 1/2/3 snapshot rows
- `scripts/sweep_linear_sync.py` — local Linear API backend plus pending-sync queue fallback
- `scripts/resume_or_start_backlog_sweep.py` — repo-native wrapper to resume a valid active sweep or start a new one
- `tests/test_sweep_repair_wave.py` — repair-wave fingerprint coverage
- `tests/test_sweep_controller.py` — controller transition and current-wave enforcement coverage
- `tests/test_sweep_linear_sync.py` — Linear sync backend and pending-sync queue coverage

### Modified files:
- `pyproject.toml` — declare `httpx` directly for the local Linear API client
- `scripts/init_backlog_sweep.py` — add Phase 1 support and hand off to the Linear snapshot backend
- `scripts/backlog_sweep_recorder.py` — become a compatibility shim over the shared controller for Phase 2/3
- `scripts/record_backlog_sweep_result.py` — route through the shared controller contract
- `scripts/check_backlog_sweep.py` — add Phase 1, repair-wave, and Linear-sync validation
- `scripts/verify_active_sweep.py` — include Phase 1 and fail on unsynced or stale unresolved items
- `scripts/draft_web.py` — mark-reviewed and defect-driven actions call the shared controller
- `tests/test_backlog_sweep_harness.py` — cover Phase 1 manifest/start behavior and resume/start wrapper
- `tests/test_backlog_sweep_recorder.py` — keep the Phase 2/3 compatibility shim honest while the controller takes over
- `tests/test_check_backlog_sweep.py` — validate current-wave and sync rules
- `tests/test_draft_web.py` — verify `draft_web` integration through the controller
- `docs/backlog-sweep.md` — document the controller actions, Phase 1, and repair-wave rules
- `docs/runbooks/repeatable-backlog-sweep.md` — update operator flow to use the resume/start wrapper

---

## Chunks & Tasks

### Chunk 1: Manifest And Repair-Wave Foundations

#### Task 1: Add repair-wave fingerprinting and declare the Linear client dependency

**Files:**
- Modify: `pyproject.toml`
- Create: `scripts/sweep_repair_wave.py`
- Create: `tests/test_sweep_repair_wave.py`

- [ ] **Step 1: Write the failing repair-wave tests**

```python
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop(name, None)
    sys.modules[name] = module
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_repair_wave_fingerprint_changes_when_repair_relevant_file_changes(tmp_path: Path):
    module = load_module("sweep_repair_wave", "scripts/sweep_repair_wave.py")
    target = tmp_path / "scripts" / "controller.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("print('v1')\n", encoding="utf-8")

    first = module.compute_repair_wave_fingerprint([target])
    target.write_text("print('v2')\n", encoding="utf-8")
    second = module.compute_repair_wave_fingerprint([target])

    assert first != second


def test_repair_wave_fingerprint_ignores_generated_artifacts(tmp_path: Path):
    module = load_module("sweep_repair_wave", "scripts/sweep_repair_wave.py")
    tracked = tmp_path / "scripts" / "controller.py"
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text("print('stable')\n", encoding="utf-8")
    ignored = tmp_path / "output" / "example" / "draft_summary.png"
    ignored.parent.mkdir(parents=True, exist_ok=True)
    ignored.write_bytes(b"proof-a")

    first = module.compute_repair_wave_fingerprint([tracked], ignored_paths=[ignored])
    ignored.write_bytes(b"proof-b")
    second = module.compute_repair_wave_fingerprint([tracked], ignored_paths=[ignored])

    assert first == second
```

- [ ] **Step 2: Run the tests and verify they fail because the module does not exist**

Run: `uv run python -m pytest tests/test_sweep_repair_wave.py -v`

Expected: `ModuleNotFoundError` or missing-file assertion for `scripts/sweep_repair_wave.py`

- [ ] **Step 3: Declare `httpx` directly and implement the fingerprint helper**

```toml
[project]
dependencies = [
    "python-docx>=1.1",
    "lxml>=5.0",
    "scrapling>=0.4",
    "pypdf>=5.0",
    "pdfplumber>=0.11",
    "reportlab>=4.0",
    "playwright>=1.58.0",
    "steel-sdk>=0.1",
    "textual>=8.1.1",
    "Pillow>=11.0",
    "python-jobspy>=1.1",
    "openai>=2.29.0",
    "httpx>=0.28",
]
```

```python
#!/usr/bin/env python3
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GLOBS = (
    "scripts/**/*.py",
    "scripts/static/**",
    "AGENTS.md",
    "docs/operational-rules.md",
    "docs/backlog-sweep.md",
    "docs/runbooks/repeatable-backlog-sweep.md",
)


def iter_repair_wave_paths(project_root: Path = PROJECT_ROOT) -> list[Path]:
    paths: list[Path] = []
    for pattern in DEFAULT_GLOBS:
        if "*" in pattern:
            paths.extend(path for path in project_root.glob(pattern) if path.is_file())
        else:
            path = project_root / pattern
            if path.is_file():
                paths.append(path)
    return sorted(set(paths))


PathLike = str | Path


def _hash_identifier(path: Path) -> bytes:
    try:
        identifier = path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        identifier = path.as_posix()
    return identifier.encode("utf-8")


def compute_repair_wave_fingerprint(paths: Iterable[PathLike], *, ignored_paths: Iterable[PathLike] = ()) -> str:
    ignored = {Path(path).resolve() for path in ignored_paths}
    digest = hashlib.sha256()
    for path in sorted(Path(path).resolve() for path in paths):
        if path in ignored:
            continue
        digest.update(_hash_identifier(path))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def current_repair_wave_fingerprint(project_root: Path = PROJECT_ROOT) -> str:
    return compute_repair_wave_fingerprint(iter_repair_wave_paths(project_root))
```

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `uv run python -m pytest tests/test_sweep_repair_wave.py -v`

Expected: both tests pass

- [ ] **Step 5: Commit the dependency and repair-wave helper**

```bash
git add pyproject.toml scripts/sweep_repair_wave.py tests/test_sweep_repair_wave.py
git commit -m "feat: add repair wave fingerprinting"
```

#### Task 2: Add Phase 1 snapshot/ledger support and a single resume-or-start entrypoint

**Files:**
- Create: `docs/templates/phase1-linear-results-template.tsv`
- Modify: `scripts/init_backlog_sweep.py`
- Create: `scripts/resume_or_start_backlog_sweep.py`
- Modify: `tests/test_backlog_sweep_harness.py`

- [ ] **Step 1: Write failing harness tests for Phase 1 and the resume-or-start wrapper**

```python
def test_init_backlog_sweep_starts_phase1_from_linear_snapshot_source(tmp_path: Path):
    module = load_module("init_backlog_sweep", "scripts/init_backlog_sweep.py")
    todos_dir = tmp_path / ".context" / "compound-engineering" / "todos"
    manifest_path = todos_dir / "current_backlog_sweep.json"
    phase1_template = tmp_path / "phase1-template.tsv"
    phase1_template.write_text(
        "handled_at_utc\tlinear_issue_id\ttitle\tlabels\tstatus\trelated_job_id\trelated_output_dir\toutcome\thandled_via\treview_trace_path\tartifact_manifest_path\tproof_generated_at_utc\trepair_wave_fingerprint\tlinear_sync_status\tlinear_sync_payload_path\tnotes\n",
        encoding="utf-8",
    )

    fake_phase1_rows = [
        {
            "linear_issue_id": "NAD-101",
            "title": "Fix draft proof drift",
            "labels": "bug",
            "status": "Todo",
            "related_job_id": "42",
            "related_output_dir": "output/acme/pm",
            "requires_user_input": "false",
            "captured_at_utc": "2026-04-08T10:00:00Z",
        }
    ]

    with (
        patch.object(module, "TODOS_DIR", todos_dir),
        patch.object(module, "DEFAULT_MANIFEST_PATH", manifest_path),
        patch.object(module, "PHASE1_TEMPLATE", phase1_template),
        patch.object(module, "load_phase1_linear_rows", return_value=fake_phase1_rows),
        patch.object(module, "load_repo_state", return_value={"head": "abc", "branch": "main", "dirty_paths_count": 0}),
        patch.object(module, "load_job_status_counts", return_value={"draft": 4, "stopped": 2}),
    ):
        code, stdout, stderr = run_main(module, ["--date", "2026-04-08"])
        assert code == 0, stderr or stdout
        code, stdout, stderr = run_main(module, ["--start-phase", "phase1", "--manifest", str(manifest_path)])

    assert code == 0, stderr or stdout
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["phase1_snapshot_count"] == 1
    assert Path(manifest["phase1_results"]).read_text(encoding="utf-8").startswith("handled_at_utc\tlinear_issue_id")


def test_resume_or_start_reuses_valid_active_manifest(tmp_path: Path):
    module = load_module("resume_or_start_backlog_sweep", "scripts/resume_or_start_backlog_sweep.py")
    manifest_path = tmp_path / ".context" / "compound-engineering" / "todos" / "current_backlog_sweep.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"phase2_snapshot": "phase2.tsv", "phase2_results": "phase2-results.tsv"}) + "\n",
        encoding="utf-8",
    )

    with patch.object(module, "DEFAULT_MANIFEST_PATH", manifest_path):
        code, stdout, stderr = run_main(module, ["--active"])

    assert code == 0
    assert "resume" in (stdout + stderr).lower()
```

- [ ] **Step 2: Run the harness tests and verify they fail**

Run: `uv run python -m pytest tests/test_backlog_sweep_harness.py -v`

Expected: failure because `PHASE1_TEMPLATE`, `load_phase1_linear_rows`, and `scripts/resume_or_start_backlog_sweep.py` do not exist

- [ ] **Step 3: Add the Phase 1 template, manifest keys, and the wrapper script**

```text
handled_at_utc	linear_issue_id	title	labels	status	related_job_id	related_output_dir	outcome	handled_via	review_trace_path	artifact_manifest_path	proof_generated_at_utc	repair_wave_fingerprint	linear_sync_status	linear_sync_payload_path	notes
```

```python
PHASE1_TEMPLATE = PROJECT_ROOT / "docs" / "templates" / "phase1-linear-results-template.tsv"
PHASE1_SNAPSHOT_FIELDS = (
    "linear_issue_id",
    "title",
    "labels",
    "status",
    "related_job_id",
    "related_output_dir",
    "requires_user_input",
    "captured_at_utc",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Date tag for generated files in YYYY-MM-DD format. Defaults to the current UTC date.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing active sweep manifest and generated files.")
    parser.add_argument(
        "--new-run",
        action="store_true",
        help="Archive any existing active manifest and bootstrap a fresh run using the current repo and queue state.",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH, help="Path to write the active sweep manifest.")
    parser.add_argument(
        "--start-phase",
        choices=("phase1", "phase2", "phase3"),
        help="Materialize the snapshot and results ledger for the requested phase using the active manifest date tag.",
    )
    return parser


def phase_artifact_paths(manifest_path: Path, date_tag: str) -> dict[str, Path]:
    todos_dir = manifest_path.parent
    return {
        "phase1_snapshot": todos_dir / f"phase1-linear-snapshot-{date_tag}.tsv",
        "phase1_results": todos_dir / f"phase1-linear-results-{date_tag}.tsv",
        "phase2_snapshot": todos_dir / f"phase2-stopped-snapshot-{date_tag}.tsv",
        "phase2_results": todos_dir / f"phase2-stopped-results-{date_tag}.tsv",
        "phase3_snapshot": todos_dir / f"phase3-draft-snapshot-{date_tag}.tsv",
        "phase3_results": todos_dir / f"phase3-draft-results-{date_tag}.tsv",
    }


def load_phase1_linear_rows() -> list[dict[str, str]]:
    from sweep_linear_sync import fetch_phase1_linear_todo_rows

    return fetch_phase1_linear_todo_rows()


def write_snapshot(path: Path, rows: list[dict[str, str]], *, fields: tuple[str, ...] = SNAPSHOT_FIELDS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "\t".join(fields)
    lines = [header]
    for row in rows:
        lines.append("\t".join(row.get(field, "") for field in fields))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def start_phase(
    manifest_path: Path,
    *,
    phase_key: str,
    force: bool,
) -> tuple[dict[str, object], Path, Path, list[dict[str, str]]]:
    payload = load_manifest(manifest_path)
    date_tag = str(payload.get("date_tag") or "").strip()
    if not date_tag:
        raise ValueError(f"{manifest_path} is missing date_tag")

    paths = phase_artifact_paths(manifest_path, date_tag)
    snapshot_path = paths[f"{phase_key}_snapshot"]
    results_path = paths[f"{phase_key}_results"]
    ensure_destinations_clear([snapshot_path, results_path], force=force)

    if phase_key == "phase1":
        rows = load_phase1_linear_rows()
        template_path = PHASE1_TEMPLATE
        fields = PHASE1_SNAPSHOT_FIELDS
    elif phase_key == "phase2":
        rows = load_snapshot_rows("stopped")
        template_path = PHASE2_TEMPLATE
        fields = SNAPSHOT_FIELDS
    else:
        rows = load_snapshot_rows("draft")
        template_path = PHASE3_TEMPLATE
        fields = SNAPSHOT_FIELDS

    write_snapshot(snapshot_path, rows, fields=fields)
    initialize_results_ledger(results_path, template_path)

    payload[f"{phase_key}_snapshot"] = str(snapshot_path)
    payload[f"{phase_key}_results"] = str(results_path)
    payload[f"{phase_key}_snapshot_count"] = len(rows)
    payload[f"{phase_key}_started_at_utc"] = utc_now_iso()
    payload[f"{phase_key}_repo_state"] = load_repo_state()
    payload[f"{phase_key}_job_status_counts"] = load_job_status_counts()
    payload["checker_command"] = checker_command(manifest_path)
    payload["verifier_command"] = verifier_command(manifest_path)
    write_manifest(manifest_path, payload)
    return payload, snapshot_path, results_path, rows
```

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from init_backlog_sweep import DEFAULT_MANIFEST_PATH, bootstrap_manifest, utc_run_tag


def manifest_is_resumable(path: Path) -> bool:
    if not path.exists():
        return False
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return False
    phase_pairs = (
        ("phase1_snapshot", "phase1_results"),
        ("phase2_snapshot", "phase2_results"),
        ("phase3_snapshot", "phase3_results"),
    )
    for snapshot_key, results_key in phase_pairs:
        snapshot_raw = str(payload.get(snapshot_key) or "").strip()
        results_raw = str(payload.get(results_key) or "").strip()
        if not snapshot_raw or not results_raw:
            continue
        if Path(snapshot_raw).exists() and Path(results_raw).exists():
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--active", action="store_true")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    args = parser.parse_args(argv)
    manifest_path = args.manifest
    if manifest_is_resumable(manifest_path):
        print(f"Resume active sweep: {manifest_path}")
        return 0
    bootstrap_manifest(manifest_path, date_tag=utc_run_tag(), force=False, new_run=True)
    print(f"Started new active sweep: {manifest_path}")
    return 0
```

- [ ] **Step 4: Run the harness tests and verify they pass**

Run: `uv run python -m pytest tests/test_backlog_sweep_harness.py -v`

Expected: Phase 1 bootstrap and resume/start tests pass alongside existing harness coverage

- [ ] **Step 5: Commit the Phase 1 and resume/start plumbing**

```bash
git add docs/templates/phase1-linear-results-template.tsv scripts/init_backlog_sweep.py scripts/resume_or_start_backlog_sweep.py tests/test_backlog_sweep_harness.py
git commit -m "feat: add phase1 sweep bootstrap and resume wrapper"
```

### Chunk 2: Shared Controller And Phase Transitions

#### Task 3: Introduce the shared sweep controller and move Phase 2/3 recording behind it

**Files:**
- Create: `scripts/sweep_controller.py`
- Modify: `scripts/backlog_sweep_recorder.py`
- Modify: `scripts/record_backlog_sweep_result.py`
- Create: `tests/test_sweep_controller.py`
- Modify: `tests/test_backlog_sweep_recorder.py`

- [ ] **Step 1: Write failing controller tests for current-wave transitions**

```python
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop(name, None)
    sys.modules[name] = module
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_record_transition_writes_current_wave_to_latest_row(tmp_path: Path):
    module = load_module("sweep_controller", "scripts/sweep_controller.py")
    manifest_path = tmp_path / "current_backlog_sweep.json"
    snapshot_path = tmp_path / "phase2-snapshot.tsv"
    results_path = tmp_path / "phase2-results.tsv"
    output_dir = tmp_path / "output" / "acme"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "draft_summary.png").write_bytes(b"proof")
    snapshot_path.write_text(
        "id\tcompany\trole_title\tboard\toutput_dir\n"
        f"1\tAcme\tPM\tgreenhouse\t{output_dir}\n",
        encoding="utf-8",
    )
    results_path.write_text(
        "handled_at_utc\tid\tcompany\trole_title\tboard\toutput_dir\toutcome\tissue_id\tevidence_paths\thandled_via\treview_trace_path\tartifact_manifest_path\tproof_generated_at_utc\trepair_wave_fingerprint\tlinear_sync_status\tlinear_sync_payload_path\tnotes\n",
        encoding="utf-8",
    )
    manifest_path.write_text(
        json.dumps({"run_id": "wave-1", "phase2_snapshot": str(snapshot_path), "phase2_results": str(results_path)}) + "\n",
        encoding="utf-8",
    )

    recorded = module.record_transition(
        manifest_path=manifest_path,
        phase_key="phase2",
        row_id="1",
        outcome="fixed_redrafted",
        handled_via="cli_manual",
        repair_wave_fingerprint="wave-a",
        linear_sync_status="pending",
    )

    assert recorded["repair_wave_fingerprint"] == "wave-a"
    assert recorded["linear_sync_status"] == "pending"
```

- [ ] **Step 2: Run the focused controller tests and verify they fail**

Run: `uv run python -m pytest tests/test_sweep_controller.py -v`

Expected: missing-module or missing-function failure for `scripts/sweep_controller.py`

- [ ] **Step 3: Implement the controller and keep recorder compatibility**

```python
#!/usr/bin/env python3
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sweep_repair_wave import current_repair_wave_fingerprint

# Start this file by moving these helpers unchanged out of `scripts/backlog_sweep_recorder.py`:
# `DEFAULT_MANIFEST_PATH`, `TRACE_ROOT_DIRNAME`, `BROWSER_HANDLED_VIA`, `utc_now_iso`,
# `_load_manifest`, `_read_tsv_rows`, `_phase_paths`, `_normalize_paths`,
# `_artifact_entry`, `_gather_current_artifacts`, `_default_evidence_paths`, `_write_json`.

PHASE_ALLOWED_OUTCOMES: dict[str, tuple[str, ...]] = {
    "phase2": (
        "fixed_redrafted",
        "parked_requires_user_input",
        "nad_created",
        "duplicate_archived",
        "unsupported_parked",
        "terminal_external_confirmed",
    ),
    "phase3": (
        "reviewed_ready",
        "fixed_redrafted",
        "parked_requires_user_input",
        "nad_created",
        "duplicate_archived",
    ),
}
PHASE_RESULT_FIELDS: dict[str, tuple[str, ...]] = {
    "phase2": (
        "handled_at_utc",
        "id",
        "company",
        "role_title",
        "board",
        "output_dir",
        "outcome",
        "issue_id",
        "evidence_paths",
        "handled_via",
        "review_trace_path",
        "artifact_manifest_path",
        "proof_generated_at_utc",
        "repair_wave_fingerprint",
        "linear_sync_status",
        "linear_sync_payload_path",
        "notes",
    ),
    "phase3": (
        "handled_at_utc",
        "id",
        "company",
        "role_title",
        "board",
        "output_dir",
        "outcome",
        "issue_id",
        "evidence_paths",
        "handled_via",
        "review_trace_path",
        "artifact_manifest_path",
        "proof_generated_at_utc",
        "repair_wave_fingerprint",
        "linear_sync_status",
        "linear_sync_payload_path",
        "notes",
    ),
}


def _snapshot_row(snapshot_path: Path, row_id: str, *, id_field: str = "id") -> dict[str, str]:
    _, rows = _read_tsv_rows(snapshot_path)
    for row in rows:
        if row.get(id_field, "").strip() == row_id:
            return row
    raise ValueError(f"{snapshot_path} does not contain job id {row_id}")


def _append_results_row(results_path: Path, fieldnames: tuple[str, ...], row: dict[str, str]) -> None:
    import csv

    results_path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = results_path.exists()
    with results_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), delimiter="\t")
        if not file_exists or results_path.stat().st_size == 0:
            writer.writeheader()
        writer.writerow(row)


def record_transition(
    *,
    manifest_path: Path,
    phase_key: str,
    row_id: str,
    outcome: str,
    handled_via: str,
    issue_id: str = "",
    notes: str = "",
    evidence_paths: Sequence[str | Path] | None = None,
    repair_wave_fingerprint: str | None = None,
    linear_sync_status: str = "pending",
    linear_sync_payload_path: str = "",
    detail_json: Mapping[str, Any] | None = None,
    proof_generated_at_utc: str | None = None,
) -> dict[str, str]:
    repair_wave = repair_wave_fingerprint or current_repair_wave_fingerprint()
    manifest_path = Path(manifest_path)
    manifest = _load_manifest(manifest_path)
    if phase_key not in PHASE_ALLOWED_OUTCOMES:
        allowed = ", ".join(sorted(PHASE_ALLOWED_OUTCOMES))
        raise ValueError(f"Unsupported phase_key '{phase_key}'. Allowed values: {allowed}")
    if outcome not in PHASE_ALLOWED_OUTCOMES[phase_key]:
        allowed = ", ".join(PHASE_ALLOWED_OUTCOMES[phase_key])
        raise ValueError(f"Unsupported outcome '{outcome}' for {phase_key}. Allowed values: {allowed}")
    if phase_key == "phase3" and outcome == "reviewed_ready" and handled_via not in BROWSER_HANDLED_VIA:
        allowed = ", ".join(sorted(BROWSER_HANDLED_VIA))
        raise ValueError(f"Phase 3 reviewed_ready requires browser handled_via. Allowed values: {allowed}")

    snapshot_path, results_path = _phase_paths(manifest_path, manifest, phase_key)
    snapshot_row = _snapshot_row(snapshot_path, row_id)
    output_dir = Path(snapshot_row["output_dir"])
    if not output_dir.exists():
        raise ValueError(f"Output directory does not exist for job {row_id}: {output_dir}")

    artifacts = _gather_current_artifacts(output_dir, board_name=snapshot_row.get("board") or None)
    chosen_evidence = _normalize_paths(evidence_paths or _default_evidence_paths(artifacts))
    if not chosen_evidence:
        raise ValueError(f"No current screenshot evidence found for job {row_id}. Provide evidence_paths explicitly.")

    proof_generated_at = str(proof_generated_at_utc or utc_now_iso()).strip()
    handled_at_utc = utc_now_iso()
    run_id = str(manifest.get("run_id") or "adhoc-run").strip() or "adhoc-run"
    review_root = manifest_path.parent / TRACE_ROOT_DIRNAME / run_id / phase_key / row_id
    artifact_manifest_path = review_root / "artifact-manifest.json"
    review_trace_path = review_root / "review-trace.json"

    artifact_manifest = {
        "phase": phase_key,
        "job_id": row_id,
        "output_dir": snapshot_row["output_dir"],
        "generated_at_utc": proof_generated_at,
        "evidence_paths": chosen_evidence,
        "artifacts": artifacts,
    }
    review_trace = {
        "phase": phase_key,
        "job_id": row_id,
        "outcome": outcome,
        "handled_via": handled_via,
        "review_kind": "manual_browser_review" if handled_via in BROWSER_HANDLED_VIA else "manual_row_review",
        "proof_generated_at_utc": proof_generated_at,
        "handled_at_utc": handled_at_utc,
        "issue_id": issue_id,
        "notes": notes,
        "artifacts_reviewed": chosen_evidence,
        "artifacts_reviewed_count": len(chosen_evidence),
        "detail": dict(detail_json or {}),
    }
    _write_json(artifact_manifest_path, artifact_manifest)
    _write_json(review_trace_path, review_trace)

    row = {
        "handled_at_utc": handled_at_utc,
        "id": row_id,
        "company": snapshot_row.get("company", ""),
        "role_title": snapshot_row.get("role_title", ""),
        "board": snapshot_row.get("board", ""),
        "output_dir": snapshot_row.get("output_dir", ""),
        "outcome": outcome,
        "issue_id": issue_id,
        "evidence_paths": "|".join(chosen_evidence),
        "handled_via": handled_via,
        "review_trace_path": str(review_trace_path),
        "artifact_manifest_path": str(artifact_manifest_path),
        "proof_generated_at_utc": proof_generated_at,
        "repair_wave_fingerprint": repair_wave,
        "linear_sync_status": linear_sync_status,
        "linear_sync_payload_path": linear_sync_payload_path,
        "notes": notes,
    }
    _append_results_row(results_path, PHASE_RESULT_FIELDS[phase_key], row)
    return row
```

```python
from sweep_controller import DEFAULT_MANIFEST_PATH, record_transition


def record_backlog_sweep_result(
    *,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    phase_key: str,
    job_id: str | int,
    outcome: str,
    handled_via: str,
    issue_id: str = "",
    notes: str = "",
    evidence_paths: Sequence[str | Path] | None = None,
    detail_json: Mapping[str, Any] | None = None,
    proof_generated_at_utc: str | None = None,
) -> dict[str, str]:
    return record_transition(
        manifest_path=manifest_path,
        phase_key=phase_key,
        row_id=str(job_id),
        outcome=outcome,
        handled_via=handled_via,
        issue_id=issue_id,
        notes=notes,
        evidence_paths=evidence_paths,
        detail_json=dict(detail_json or {}),
        proof_generated_at_utc=proof_generated_at_utc,
    )
```

```python
from sweep_controller import DEFAULT_MANIFEST_PATH, record_transition


recorded = record_transition(
    manifest_path=manifest_path,
    phase_key=args.phase,
    row_id=args.id,
    outcome=args.outcome,
    handled_via=args.handled_via,
    issue_id=args.issue_id,
    notes=args.notes,
    evidence_paths=args.evidence_paths,
)
```

- [ ] **Step 4: Run the controller and compatibility tests**

Run: `uv run python -m pytest tests/test_sweep_controller.py tests/test_backlog_sweep_recorder.py -v`

Expected: controller transitions and recorder compatibility tests pass

- [ ] **Step 5: Commit the controller slice**

```bash
git add scripts/sweep_controller.py scripts/backlog_sweep_recorder.py scripts/record_backlog_sweep_result.py tests/test_sweep_controller.py tests/test_backlog_sweep_recorder.py
git commit -m "feat: add shared sweep transition controller"
```

#### Task 4: Route `draft_web` through the controller instead of phase-specific shortcuts

**Files:**
- Modify: `scripts/draft_web.py`
- Modify: `tests/test_draft_web.py`

- [ ] **Step 1: Write the failing `draft_web` integration test against the controller**

```python
def test_mark_reviewed_route_calls_sweep_controller(self):
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        self.skipTest("fastapi not installed")

    import draft_web
    from job_db import add_job, init_db

    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp)
        db_path = project_root / "jobs.db"
        todos_dir = project_root / ".context" / "compound-engineering" / "todos"
        todos_dir.mkdir(parents=True, exist_ok=True)
        out_dir = project_root / "output" / "example"
        out_dir.mkdir(parents=True)
        (out_dir / "draft_summary.png").write_bytes(b"proof")

        conn = init_db(db_path)
        job_id = add_job(
            conn,
            "https://boards.greenhouse.io/example/jobs/1",
            company="Example",
            role_title="Principal PM",
        )
        conn.execute(
            "UPDATE jobs SET status = 'draft', board = 'greenhouse', output_dir = ? WHERE id = ?",
            (str(out_dir), job_id),
        )
        conn.commit()
        conn.close()

        original_root = draft_web.PROJECT_ROOT
        draft_web.PROJECT_ROOT = project_root
        try:
            with mock.patch(
                "sweep_controller.record_transition",
                return_value={
                    "id": str(job_id),
                    "outcome": "reviewed_ready",
                    "review_trace_path": str(todos_dir / "trace.json"),
                    "artifact_manifest_path": str(todos_dir / "artifact-manifest.json"),
                    "linear_sync_status": "pending",
                },
            ) as record_transition:
                client = TestClient(draft_web.create_app())
                page = client.get(f"/drafts/{job_id}")
                resp = client.post(f"/api/drafts/{job_id}/mark-reviewed")

            self.assertEqual(page.status_code, 200)
            self.assertIn("Mark Reviewed", page.text)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["linear_sync_status"], "pending")
            record_transition.assert_called_once()
        finally:
            draft_web.PROJECT_ROOT = original_root
```

- [ ] **Step 2: Run the web tests and verify they fail against the old import path**

Run: `uv run python -m pytest tests/test_draft_web.py -v`

Expected: mock target mismatch because `draft_web` still imports `backlog_sweep_recorder.record_backlog_sweep_result`

- [ ] **Step 3: Update the route to use the controller**

```python
@app.post("/api/drafts/{job_id}/mark-reviewed")
def mark_reviewed(job_id: int, request: FastAPIRequest):
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    from sweep_controller import record_transition

    conn = _open_db()
    try:
        row = conn.execute(
            "SELECT id FROM jobs WHERE id = ? AND status = 'draft'",
            (job_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Draft not found")
        action_detail_json, _action_process_info = _request_action_audit(request)
    finally:
        conn.close()

    manifest_path = PROJECT_ROOT / ".context" / "compound-engineering" / "todos" / "current_backlog_sweep.json"
    recorded = record_transition(
        manifest_path=manifest_path,
        phase_key="phase3",
        row_id=str(job_id),
        outcome="reviewed_ready",
        handled_via="draft_web_browser",
        notes="Recorded from draft_web browser review.",
        detail_json=action_detail_json,
    )
    return {
        "status": "recorded",
        "job_id": job_id,
        "outcome": recorded["outcome"],
        "review_trace_path": recorded["review_trace_path"],
        "artifact_manifest_path": recorded["artifact_manifest_path"],
        "linear_sync_status": recorded["linear_sync_status"],
    }
```

- [ ] **Step 4: Run the draft web tests and verify they pass**

Run: `uv run python -m pytest tests/test_draft_web.py -v`

Expected: the mark-reviewed route passes through the shared controller and existing draft routes stay green

- [ ] **Step 5: Commit the `draft_web` integration**

```bash
git add scripts/draft_web.py tests/test_draft_web.py
git commit -m "refactor: route draft review actions through sweep controller"
```

### Chunk 3: Linear Sync Backend And Phase 1 Control Surface

#### Task 5: Implement the Linear sync backend and transitional pending-sync queue

**Files:**
- Create: `scripts/sweep_linear_sync.py`
- Create: `tests/test_sweep_linear_sync.py`

- [ ] **Step 1: Write failing tests for both backends**

```python
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop(name, None)
    sys.modules[name] = module
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_queue_sync_payload_writes_pending_file(tmp_path: Path):
    module = load_module("sweep_linear_sync", "scripts/sweep_linear_sync.py")
    payload = module.queue_sync_payload(
        tmp_path,
        item_id="phase2:1",
        action="update_issue",
        body={"issue_id": "NAD-1", "state": "Todo"},
    )
    assert Path(payload).exists()


def test_fetch_phase1_linear_todo_rows_uses_local_api_when_token_present(tmp_path: Path):
    module = load_module("sweep_linear_sync", "scripts/sweep_linear_sync.py")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": {
                    "issues": {
                        "nodes": [
                            {
                                "identifier": "NAD-101",
                                "title": "Fix proof drift",
                                "state": {"name": "Todo"},
                                "labels": {"nodes": [{"name": "bug"}]},
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }

    with patch.dict(os.environ, {"LINEAR_API_TOKEN": "token"}), patch("httpx.post", return_value=FakeResponse()):
        rows = module.fetch_phase1_linear_todo_rows()

    assert rows[0]["linear_issue_id"] == "NAD-101"


def test_fetch_phase1_linear_todo_rows_reads_bridge_export_when_token_missing(tmp_path: Path):
    module = load_module("sweep_linear_sync", "scripts/sweep_linear_sync.py")
    export_path = tmp_path / "phase1-todo-export.json"
    export_path.write_text(
        json.dumps(
            [
                {
                    "identifier": "NAD-202",
                    "title": "Fix queue drift",
                    "state": {"name": "Todo"},
                    "labels": {"nodes": [{"name": "requires-user-input"}]},
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with patch.object(module, "PHASE1_BRIDGE_EXPORT", export_path), patch.dict(os.environ, {}, clear=True):
        rows = module.fetch_phase1_linear_todo_rows()

    assert rows[0]["linear_issue_id"] == "NAD-202"
    assert rows[0]["requires_user_input"] == "true"
```

- [ ] **Step 2: Run the sync backend tests and verify they fail**

Run: `uv run python -m pytest tests/test_sweep_linear_sync.py -v`

Expected: missing-module failure for `scripts/sweep_linear_sync.py`

- [ ] **Step 3: Implement the Linear sync module**

```python
#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PENDING_SYNC_DIR = PROJECT_ROOT / ".context" / "compound-engineering" / "linear-sync"
PHASE1_BRIDGE_EXPORT = PENDING_SYNC_DIR / "phase1-todo-export.json"
LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
PHASE1_TODO_QUERY = """
query BacklogSweepTodoIssues($after: String) {
  issues(
    first: 250
    after: $after
    filter: { state: { name: { eq: "Todo" } } }
  ) {
    nodes {
      identifier
      title
      state {
        name
      }
      labels {
        nodes {
          name
        }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def queue_sync_payload(root: Path, *, item_id: str, action: str, body: dict[str, object]) -> str:
    path = root / f"{item_id.replace(':', '__')}--{action}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"item_id": item_id, "action": action, "body": body}, indent=2) + "\n", encoding="utf-8")
    return str(path)


def _normalize_issue_node(node: dict[str, object], *, captured_at_utc: str) -> dict[str, str]:
    labels = [str(label.get("name") or "").strip() for label in node.get("labels", {}).get("nodes", [])]
    normalized_labels = [label for label in labels if label]
    return {
        "linear_issue_id": str(node.get("identifier") or "").strip(),
        "title": str(node.get("title") or "").strip(),
        "labels": ",".join(normalized_labels),
        "status": str(node.get("state", {}).get("name") or "").strip(),
        "related_job_id": "",
        "related_output_dir": "",
        "requires_user_input": "true" if "requires-user-input" in normalized_labels else "false",
        "captured_at_utc": captured_at_utc,
    }


def _fetch_issue_nodes_from_api(token: str) -> list[dict[str, object]]:
    nodes: list[dict[str, object]] = []
    cursor: str | None = None
    while True:
        response = httpx.post(
            LINEAR_GRAPHQL_URL,
            headers={"Authorization": token, "Content-Type": "application/json"},
            json={"query": PHASE1_TODO_QUERY, "variables": {"after": cursor}},
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        issue_payload = payload["data"]["issues"]
        nodes.extend(issue_payload["nodes"])
        page_info = issue_payload["pageInfo"]
        if not page_info["hasNextPage"]:
            return nodes
        cursor = page_info["endCursor"]


def fetch_phase1_linear_todo_rows() -> list[dict[str, str]]:
    token = os.environ.get("LINEAR_API_TOKEN", "").strip()
    captured_at_utc = utc_now_iso()
    if token:
        return [_normalize_issue_node(node, captured_at_utc=captured_at_utc) for node in _fetch_issue_nodes_from_api(token)]
    if PHASE1_BRIDGE_EXPORT.exists():
        payload = json.loads(PHASE1_BRIDGE_EXPORT.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"{PHASE1_BRIDGE_EXPORT} must contain a JSON list")
        return [_normalize_issue_node(node, captured_at_utc=captured_at_utc) for node in payload]
    raise RuntimeError(
        "LINEAR_API_TOKEN is not set and no bridge export exists at "
        f"{PHASE1_BRIDGE_EXPORT}. Sync Linear context before starting phase1."
    )
```

- [ ] **Step 4: Run the focused sync tests and verify they pass**

Run: `uv run python -m pytest tests/test_sweep_linear_sync.py -v`

Expected: pending-sync payload and direct-API snapshot tests pass

- [ ] **Step 5: Commit the Linear backend**

```bash
git add scripts/sweep_linear_sync.py tests/test_sweep_linear_sync.py
git commit -m "feat: add linear sync backend for sweep workflow"
```

#### Task 6: Extend Phase 1 through the shared controller and queue Linear sync payloads

**Files:**
- Modify: `scripts/sweep_controller.py`
- Modify: `scripts/init_backlog_sweep.py`
- Modify: `tests/test_sweep_controller.py`
- Modify: `tests/test_backlog_sweep_harness.py`

- [ ] **Step 1: Add failing tests for Phase 1 transitions and pending sync payloads**

```python
def test_record_transition_phase1_queues_linear_sync_payload(tmp_path: Path):
    module = load_module("sweep_controller", "scripts/sweep_controller.py")
    manifest_path = tmp_path / "current_backlog_sweep.json"
    snapshot_path = tmp_path / "phase1-snapshot.tsv"
    results_path = tmp_path / "phase1-results.tsv"
    snapshot_path.write_text(
        "linear_issue_id\ttitle\tlabels\tstatus\trelated_job_id\trelated_output_dir\trequires_user_input\tcaptured_at_utc\n"
        "NAD-101\tFix proof drift\tbug\tTodo\t42\toutput/acme/pm\tfalse\t2026-04-08T10:00:00Z\n",
        encoding="utf-8",
    )
    results_path.write_text(
        "handled_at_utc\tlinear_issue_id\ttitle\tlabels\tstatus\trelated_job_id\trelated_output_dir\toutcome\thandled_via\treview_trace_path\tartifact_manifest_path\tproof_generated_at_utc\trepair_wave_fingerprint\tlinear_sync_status\tlinear_sync_payload_path\tnotes\n",
        encoding="utf-8",
    )
    manifest_path.write_text(
        json.dumps({"run_id": "wave-1", "phase1_snapshot": str(snapshot_path), "phase1_results": str(results_path)}) + "\n",
        encoding="utf-8",
    )

    recorded = module.record_transition(
        manifest_path=manifest_path,
        phase_key="phase1",
        row_id="NAD-101",
        outcome="blocked_external",
        handled_via="cli_manual",
        linear_sync_status="pending",
    )

    assert recorded["linear_sync_status"] == "pending"
    assert recorded["linear_sync_payload_path"]
```

- [ ] **Step 2: Run the controller tests and verify they fail on missing Phase 1 support**

Run: `uv run python -m pytest tests/test_sweep_controller.py tests/test_backlog_sweep_harness.py -v`

Expected: controller rejects Phase 1 or fails to write a pending sync payload

- [ ] **Step 3: Extend the controller and manifest handling for Phase 1**

```python
from sweep_linear_sync import PENDING_SYNC_DIR, queue_sync_payload


PHASE_ALLOWED_OUTCOMES = {
    "phase1": ("fixed_verified", "blocked_user", "blocked_external", "duplicate_closed", "not_a_bug_closed"),
    "phase2": (
        "fixed_redrafted",
        "parked_requires_user_input",
        "nad_created",
        "duplicate_archived",
        "unsupported_parked",
        "terminal_external_confirmed",
    ),
    "phase3": (
        "reviewed_ready",
        "fixed_redrafted",
        "parked_requires_user_input",
        "nad_created",
        "duplicate_archived",
    ),
}


PHASE_ROW_ID_FIELD = {"phase1": "linear_issue_id", "phase2": "id", "phase3": "id"}
PHASE_RESULT_FIELDS["phase1"] = (
    "handled_at_utc",
    "linear_issue_id",
    "title",
    "labels",
    "status",
    "related_job_id",
    "related_output_dir",
    "outcome",
    "handled_via",
    "review_trace_path",
    "artifact_manifest_path",
    "proof_generated_at_utc",
    "repair_wave_fingerprint",
    "linear_sync_status",
    "linear_sync_payload_path",
    "notes",
)


def _queue_linear_sync_if_needed(
    *,
    phase_key: str,
    row_id: str,
    snapshot_row: dict[str, str],
    outcome: str,
    linear_sync_status: str,
    linear_sync_payload_path: str,
    notes: str,
    issue_id: str,
) -> tuple[str, str]:
    if linear_sync_payload_path:
        return linear_sync_payload_path, linear_sync_status
    if phase_key == "phase1":
        payload_path = queue_sync_payload(
            PENDING_SYNC_DIR,
            item_id=f"{phase_key}:{row_id}",
            action="sync_phase1_issue_state",
            body={
                "linear_issue_id": snapshot_row["linear_issue_id"],
                "title": snapshot_row["title"],
                "outcome": outcome,
                "notes": notes,
            },
        )
        return payload_path, "pending"
    if issue_id:
        payload_path = queue_sync_payload(
            PENDING_SYNC_DIR,
            item_id=f"{phase_key}:{row_id}",
            action="sync_backlog_issue_state",
            body={"linear_issue_id": issue_id, "outcome": outcome, "notes": notes},
        )
        return payload_path, "pending"
    return "", linear_sync_status or "pending"


id_field = PHASE_ROW_ID_FIELD[phase_key]
snapshot_row = _snapshot_row(snapshot_path, row_id, id_field=id_field)
queued_payload_path, effective_sync_status = _queue_linear_sync_if_needed(
    phase_key=phase_key,
    row_id=row_id,
    snapshot_row=snapshot_row,
    outcome=outcome,
    linear_sync_status=linear_sync_status,
    linear_sync_payload_path=linear_sync_payload_path,
    notes=notes,
    issue_id=issue_id,
)
if phase_key == "phase1":
    artifact_manifest = {
        "phase": phase_key,
        "job_id": row_id,
        "linear_issue_id": snapshot_row["linear_issue_id"],
        "related_job_id": snapshot_row.get("related_job_id", ""),
        "related_output_dir": snapshot_row.get("related_output_dir", ""),
        "generated_at_utc": proof_generated_at,
        "artifacts": [],
    }
    row = {
        "handled_at_utc": handled_at_utc,
        "linear_issue_id": snapshot_row["linear_issue_id"],
        "title": snapshot_row.get("title", ""),
        "labels": snapshot_row.get("labels", ""),
        "status": snapshot_row.get("status", ""),
        "related_job_id": snapshot_row.get("related_job_id", ""),
        "related_output_dir": snapshot_row.get("related_output_dir", ""),
        "outcome": outcome,
        "handled_via": handled_via,
        "review_trace_path": str(review_trace_path),
        "artifact_manifest_path": str(artifact_manifest_path),
        "proof_generated_at_utc": proof_generated_at,
        "repair_wave_fingerprint": repair_wave,
        "linear_sync_status": effective_sync_status,
        "linear_sync_payload_path": queued_payload_path,
        "notes": notes,
    }
```

- [ ] **Step 4: Run the focused tests and verify Phase 1 works**

Run: `uv run python -m pytest tests/test_sweep_controller.py tests/test_backlog_sweep_harness.py -v`

Expected: Phase 1 transition rows and manifest handling pass

- [ ] **Step 5: Commit the Phase 1 control path**

```bash
git add scripts/sweep_controller.py scripts/init_backlog_sweep.py tests/test_sweep_controller.py tests/test_backlog_sweep_harness.py
git commit -m "feat: add phase1 controller transitions and sync payloads"
```

### Chunk 4: Verifier Enforcement And Docs

#### Task 7: Enforce current-wave attempts and synced Linear mirrors in the checker/verifier

**Files:**
- Modify: `scripts/check_backlog_sweep.py`
- Modify: `scripts/verify_active_sweep.py`
- Modify: `tests/test_check_backlog_sweep.py`
- Modify: `tests/test_backlog_sweep_harness.py`

- [ ] **Step 1: Write failing verifier tests for stale waves and unsynced Linear state**

```python
from unittest.mock import patch


def test_checker_rejects_latest_row_from_old_repair_wave(tmp_path: Path):
    module = load_module()
    job = make_job_row(tmp_path, "101", "Acme", "pm", "greenhouse")
    snapshot = write_tsv(tmp_path / "phase2-snapshot.tsv", SNAPSHOT_FIELDS, [job])
    trace_bundle = build_trace_bundle(
        tmp_path,
        phase="phase2",
        job=job,
        outcome="fixed_redrafted",
        handled_via="cli_manual",
        proof_generated_at_utc="2026-04-08T01:00:00Z",
    )
    results = write_tsv(
        tmp_path / "phase2-results.tsv",
        RESULT_FIELDS + ["repair_wave_fingerprint", "linear_sync_status", "linear_sync_payload_path"],
        [
            {
                **make_result_row(job, handled_at_utc="2026-04-08T01:00:00Z", outcome="fixed_redrafted", trace_bundle=trace_bundle),
                "repair_wave_fingerprint": "old-wave",
                "linear_sync_status": "synced",
                "linear_sync_payload_path": "",
            }
        ],
    )
    manifest = write_manifest(
        tmp_path / "current_backlog_sweep.json",
        {
            "phase2_snapshot": str(snapshot),
            "phase2_results": str(results),
            "phase2_started_at_utc": "2026-04-08T00:30:00Z",
        },
    )

    code, stdout, stderr = run_checker(module, ["--manifest", str(manifest)])

    assert code == 1
    assert "repair_wave_fingerprint" in (stdout + stderr)


def test_checker_rejects_unsynced_phase1_terminal_row(tmp_path: Path):
    module = load_module()
    phase1_snapshot = write_tsv(
        tmp_path / "phase1-snapshot.tsv",
        [
            "linear_issue_id",
            "title",
            "labels",
            "status",
            "related_job_id",
            "related_output_dir",
            "requires_user_input",
            "captured_at_utc",
        ],
        [
            {
                "linear_issue_id": "NAD-101",
                "title": "Fix proof drift",
                "labels": "bug",
                "status": "Todo",
                "related_job_id": "42",
                "related_output_dir": "output/acme/pm",
                "requires_user_input": "false",
                "captured_at_utc": "2026-04-08T10:00:00Z",
            }
        ],
    )
    review_dir = tmp_path / "review-proof" / "phase1" / "NAD-101"
    review_dir.mkdir(parents=True, exist_ok=True)
    artifact_manifest_path = review_dir / "artifact-manifest.json"
    review_trace_path = review_dir / "review-trace.json"
    artifact_manifest_path.write_text(
        json.dumps(
            {
                "phase": "phase1",
                "job_id": "NAD-101",
                "linear_issue_id": "NAD-101",
                "related_job_id": "42",
                "related_output_dir": "output/acme/pm",
                "artifacts": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    review_trace_path.write_text(
        json.dumps(
            {
                "phase": "phase1",
                "job_id": "NAD-101",
                "outcome": "blocked_external",
                "handled_via": "cli_manual",
                "review_kind": "manual_row_review",
                "proof_generated_at_utc": "2026-04-08T10:05:00Z",
                "artifacts_reviewed": [],
                "artifacts_reviewed_count": 0,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    results = write_tsv(
        tmp_path / "phase1-results.tsv",
        [
            "handled_at_utc",
            "linear_issue_id",
            "title",
            "labels",
            "status",
            "related_job_id",
            "related_output_dir",
            "outcome",
            "handled_via",
            "review_trace_path",
            "artifact_manifest_path",
            "proof_generated_at_utc",
            "repair_wave_fingerprint",
            "linear_sync_status",
            "linear_sync_payload_path",
            "notes",
        ],
        [
            {
                "handled_at_utc": "2026-04-08T10:05:00Z",
                "linear_issue_id": "NAD-101",
                "title": "Fix proof drift",
                "labels": "bug",
                "status": "Todo",
                "related_job_id": "42",
                "related_output_dir": "output/acme/pm",
                "outcome": "blocked_external",
                "handled_via": "cli_manual",
                "review_trace_path": str(review_trace_path),
                "artifact_manifest_path": str(artifact_manifest_path),
                "proof_generated_at_utc": "2026-04-08T10:05:00Z",
                "repair_wave_fingerprint": "current-wave",
                "linear_sync_status": "pending",
                "linear_sync_payload_path": str(tmp_path / "linear-sync" / "phase1.json"),
                "notes": "requires vendor escalation",
            }
        ],
    )
    manifest = write_manifest(
        tmp_path / "current_backlog_sweep.json",
        {
            "phase1_snapshot": str(phase1_snapshot),
            "phase1_results": str(results),
            "phase1_started_at_utc": "2026-04-08T10:00:00Z",
        },
    )

    with patch.object(module, "current_repair_wave_fingerprint", return_value="current-wave"):
        code, stdout, stderr = run_checker(module, ["--manifest", str(manifest)])

    assert code == 1
    assert "not synced to Linear" in (stdout + stderr)
```

- [ ] **Step 2: Run the checker tests and verify they fail**

Run: `uv run python -m pytest tests/test_check_backlog_sweep.py -v`

Expected: checker does not yet know about `repair_wave_fingerprint`, Phase 1, or `linear_sync_status`

- [ ] **Step 3: Extend the checker and verifier command bundle**

```python
from sweep_repair_wave import current_repair_wave_fingerprint


PHASE_ROW_ID_FIELD = {"phase1": "linear_issue_id", "phase2": "id", "phase3": "id"}
PHASE_REQUIRED_SNAPSHOT_COLUMNS = {
    "phase1": ("linear_issue_id",),
    "phase2": ("id",),
    "phase3": ("id",),
}
PHASE_REQUIRED_RESULTS_COLUMNS = {
    "phase1": (
        "linear_issue_id",
        "outcome",
        "handled_via",
        "review_trace_path",
        "artifact_manifest_path",
        "proof_generated_at_utc",
        "repair_wave_fingerprint",
        "linear_sync_status",
        "linear_sync_payload_path",
    ),
    "phase2": (
        "id",
        "outcome",
        "evidence_paths",
        "handled_via",
        "review_trace_path",
        "artifact_manifest_path",
        "proof_generated_at_utc",
        "repair_wave_fingerprint",
        "linear_sync_status",
        "linear_sync_payload_path",
    ),
    "phase3": (
        "id",
        "outcome",
        "evidence_paths",
        "handled_via",
        "review_trace_path",
        "artifact_manifest_path",
        "proof_generated_at_utc",
        "repair_wave_fingerprint",
        "linear_sync_status",
        "linear_sync_payload_path",
    ),
}
PHASE_CURRENT_WAVE_EXEMPT = {
    "phase1": {"blocked_user", "blocked_external", "duplicate_closed", "not_a_bug_closed"},
    "phase2": {"parked_requires_user_input", "terminal_external_confirmed", "duplicate_archived", "unsupported_parked"},
    "phase3": {"parked_requires_user_input", "duplicate_archived"},
}


def _load_snapshot_ids(path: Path, *, id_field: str, required_columns: tuple[str, ...]) -> tuple[list[dict[str, str]], list[str]]:
    fieldnames, rows = _read_tsv(path)
    missing = _missing_columns(fieldnames, required_columns)
    if missing:
        raise ValueError(f"{path} is missing required snapshot columns: {', '.join(missing)}")
    seen: set[str] = set()
    errors: list[str] = []
    for row_number, row in enumerate(rows, start=2):
        row_id = row.get(id_field, "").strip()
        if not row_id:
            errors.append(f"{path}:{row_number}: missing snapshot row id in column {id_field}")
            continue
        if row_id in seen:
            errors.append(f"{path}:{row_number}: duplicate snapshot id {row_id}")
            continue
        seen.add(row_id)
    if errors:
        raise ValueError("\n".join(errors))
    return rows, sorted(seen, key=lambda value: int(value) if value.isdigit() else value)


def _validate_trace_payloads(
    phase_key: str,
    results_path: Path,
    row_number: int,
    row: dict[str, str],
    *,
    context: PhaseContext | None,
) -> list[str]:
    row_id = row.get(PHASE_ROW_ID_FIELD[phase_key], "").strip()
    errors: list[str] = []
    review_trace_path = _normalize_local_path(row.get("review_trace_path", ""))
    artifact_manifest_path = _normalize_local_path(row.get("artifact_manifest_path", ""))
    review_trace, trace_errors = _load_json_file(
        review_trace_path,
        label="review_trace_path",
        results_path=results_path,
        row_number=row_number,
        job_id=row_id,
    )
    errors.extend(trace_errors)
    artifact_manifest, manifest_errors = _load_json_file(
        artifact_manifest_path,
        label="artifact_manifest_path",
        results_path=results_path,
        row_number=row_number,
        job_id=row_id,
    )
    errors.extend(manifest_errors)
    if review_trace is None or artifact_manifest is None:
        return errors
    if phase_key == "phase1":
        if str(review_trace.get("job_id") or "").strip() != row_id:
            errors.append(f"{results_path}:{row_number}: review trace job_id mismatch for {row_id}")
        if str(artifact_manifest.get("linear_issue_id") or "").strip() != row_id:
            errors.append(f"{results_path}:{row_number}: artifact manifest linear_issue_id mismatch for {row_id}")
        return errors
    evidence_errors, resolved_evidence = _validate_evidence_paths(
        results_path,
        row_number,
        row_id,
        row.get("evidence_paths", ""),
        output_dir_raw=row.get("output_dir", ""),
        review_dir=review_trace_path.parent,
    )
    errors.extend(evidence_errors)
    if not resolved_evidence:
        errors.append(f"{results_path}:{row_number}: no valid local evidence paths remain for id {row_id}")
    return errors


id_field = PHASE_ROW_ID_FIELD[phase_key]
required_result_columns = PHASE_REQUIRED_RESULTS_COLUMNS[phase_key]
missing_result_columns = _missing_columns(result_fieldnames, required_result_columns)
current_wave = current_repair_wave_fingerprint()
row_id = row.get(id_field, "").strip()
if row.get("repair_wave_fingerprint", "").strip() != current_wave and outcome not in PHASE_CURRENT_WAVE_EXEMPT[phase_key]:
    errors.append(f"{results_path}:{row_number}: stale repair_wave_fingerprint for id {row_id}")
if row.get("linear_sync_status", "").strip() != "synced":
    errors.append(f"{results_path}:{row_number}: latest row for id {row_id} is not synced to Linear")
```

```python
REQUIRED_MANIFEST_KEYS = (
    "phase1_snapshot",
    "phase1_results",
    "phase2_snapshot",
    "phase2_results",
    "phase3_snapshot",
    "phase3_results",
)


def verification_commands(manifest_path: Path) -> list[list[str]]:
    return [
        ["uv", "run", "python", "scripts/check_backlog_sweep.py", "--manifest", str(manifest_path)],
        ["uv", "run", "python", "-m", "pytest", "tests/", "-v"],
        ["uv", "run", "ruff", "check", "scripts/", "tests/"],
        ["uv", "run", "python", "scripts/check_architecture.py"],
        ["uv", "run", "python", "scripts/sync_agent_files.py", "--check"],
        ["uv", "run", "python", "scripts/check_agent_docs.py"],
    ]
```

Also update both `verify_active_sweep` fixture manifests in `tests/test_backlog_sweep_harness.py` so they include dummy `phase1_snapshot` and `phase1_results` entries before asserting the command bundle.

- [ ] **Step 4: Run the focused verifier tests and verify they pass**

Run: `uv run python -m pytest tests/test_check_backlog_sweep.py tests/test_backlog_sweep_harness.py -v`

Expected: stale-wave and unsynced-row failures are enforced, and the command bundle still runs in order

- [ ] **Step 5: Commit the verifier hardening**

```bash
git add scripts/check_backlog_sweep.py scripts/verify_active_sweep.py tests/test_check_backlog_sweep.py tests/test_backlog_sweep_harness.py
git commit -m "feat: enforce current-wave and linear-sync verifier rules"
```

#### Task 8: Document the new wrapper workflow and run the full verification bundle

**Files:**
- Modify: `docs/backlog-sweep.md`
- Modify: `docs/runbooks/repeatable-backlog-sweep.md`

- [ ] **Step 1: Update the docs to point at the new wrapper commands**

```markdown
Before running a sweep, use:

```bash
uv run python scripts/resume_or_start_backlog_sweep.py --active
```

Phase 1 now snapshots current Linear `Todo` issues into the active sweep manifest and requires repo-native transitions plus synced Linear mirror state before completion.
```

- [ ] **Step 2: Run the focused doc/contract checks**

Run: `uv run python scripts/check_agent_docs.py`

Expected: exits `0`

- [ ] **Step 3: Run the full repo verification bundle**

Run: `uv run python scripts/verify_active_sweep.py --active`

Expected: exits `0` after the implementation is complete and the active sweep rows satisfy the new contract

- [ ] **Step 4: Commit the docs and final contract updates**

```bash
git add docs/backlog-sweep.md docs/runbooks/repeatable-backlog-sweep.md
git commit -m "docs: describe repo-authoritative sweep workflow"
```

---

## Surprises & Discoveries

- `httpx` is available transitively in `uv.lock`, but not declared directly in `pyproject.toml`. The plan makes it explicit before using it for the local Linear API backend.
- The current repo already has strong trace-backed Phase 2/3 proof recording through `scripts/backlog_sweep_recorder.py`; the implementation should reuse that model rather than inventing a second persistence layer.
- There is no visible repo-owned Linear API integration today, so the plan keeps a pending-sync queue fallback even while adding the direct API backend.

---

## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-04-08 | Keep snapshots and ledgers as the authoritative per-run state instead of adding a second database-backed sweep system. | The repo already treats immutable snapshots and append-only ledgers as the durable contract for sweep completion, and extending that pattern minimizes migration risk. |
| 2026-04-08 | Add a direct Linear API backend and a pending-sync queue fallback in the same module. | The workflow should be as code-enforced as possible, but the repo must still function when a local Linear token is unavailable. |
| 2026-04-08 | Route `draft_web` through the shared controller rather than leaving it as a phase-specific shortcut. | Browser review is already the authoritative review surface for Phase 3, so it must participate in the same current-wave and sync-enforced state machine. |

---

## Outcomes & Retrospective

- **Achieved:** Not started.
- **Remaining:** Execute the plan task-by-task, keep the per-run ledgers current, and verify the active sweep under the new current-wave and Linear-sync contract.
- **Lessons:** The repo already had the right primitives for proof, retries, and resumable state. The missing piece is not more prompt text; it is turning "handled" into a repo-native transition with explicit revisit rules.
