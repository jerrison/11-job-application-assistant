# Self-Repair Supervisor And Rendered-State Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add exact-match rendered-state draft auditing plus a bounded autonomous repair supervisor that patches eligible clustered failures, verifies them with tests and canaries, pushes green fixes to `origin/main`, redrafts affected jobs, and pauses/reverts on confirmed rollout regressions.

**Architecture:** Extend the current draft-audit loop with a focused rendered-state comparator and repair-cluster persistence layer, then run a singleton `repair_supervisor.py` process beside the worker pool. Keep model invocation, git promotion, canary reruns, and rollout monitoring in separate units so each stage can fail closed without leaving the live queue in an ambiguous state.

**Tech Stack:** Python, sqlite3, git worktrees, OpenAI Responses API, pytest, Ruff, FastAPI worker/web bootstrap

---

## File Structure

### New Files

- `scripts/rendered_state_audit.py`
  - Pure comparison helpers for deterministic option fields.
- `scripts/repair_fingerprints.py`
  - Failure fingerprinting, cluster identity, and repair-cluster upserts.
- `scripts/repair_runtime.py`
  - Supervisor PID/state helpers and explicit repair-model configuration.
- `scripts/repair_supervisor.py`
  - Background cluster watcher, bounded repair loop, canary reruns, and rollout promotion.
- `scripts/repair_rollout_monitor.py`
  - Comparable-cohort rollout checks plus pause-and-confirm revert decisions.
- `tests/test_rendered_state_audit.py`
  - Unit tests for exact-match and exact-cardinality rules.
- `tests/test_repair_fingerprints.py`
  - Cluster creation, persistence, and repair-report tests.
- `tests/test_repair_supervisor.py`
  - Supervisor config, repair loop gating, and requeue/push tests.
- `tests/test_repair_rollout_monitor.py`
  - Regression-threshold and confirm-before-revert tests.
- `output/_audit/repair_clusters/.gitkeep`
  - Keeps the repair-cluster report directory in git.

### Modified Files

- `scripts/pipeline_audit_loop.py`
  - Call rendered-state audit, classify mismatches, and write richer repair notes.
- `scripts/pipeline_orchestrator.py`
  - Emit repair clusters, increment rendered-audit metrics, and trigger repairable paths.
- `scripts/pipeline_draft_proof.py`
  - Expose the normalized current-attempt inputs used by rendered-state audit.
- `scripts/job_db.py`
  - Add metric columns, repair-cluster/rollout/runtime-flag tables, and helper queries.
- `scripts/job_worker.py`
  - Bootstrap the singleton repair supervisor and honor queue-pause flags.
- `scripts/job_web.py`
  - Surface repair-supervisor state through the existing worker status API.
- `scripts/llm_provider.py`
  - Thread OpenAI reasoning effort through shared command generation.
- `scripts/openai_provider.py`
  - Accept `--reasoning-effort` and pass it to the Responses API.
- `tests/test_pipeline_audit_loop.py`
  - Cover rendered-state mismatch paths and repair-cluster reporting.
- `tests/test_pipeline_orchestrator.py`
  - Cover rendered-audit requeue/exhaustion and repair-cluster emission.
- `tests/test_job_worker.py`
  - Cover supervisor bootstrap and queue pause behavior.
- `tests/test_job_web.py`
  - Cover repair-supervisor status in the web payload.
- `tests/test_llm_provider.py`
  - Cover OpenAI reasoning-effort command building.
- `tests/test_openai_provider.py`
  - Cover Responses API payload construction with reasoning effort.
- `docs/worker-pipeline-patterns.md`
  - Document rendered-state audit, supervisor launch, and rollout guard.
- `docs/operational-rules.md`
  - Document auto-push, pause-confirm rollback, and human-readable repair artifacts.

### Decomposition Notes

- Keep rendered-state comparison pure and deterministic in `rendered_state_audit.py`; it should not touch the DB.
- Keep supervisor process management (`repair_runtime.py`) separate from repair logic (`repair_supervisor.py`) so worker bootstrap tests can stay narrow.
- Keep rollout evaluation (`repair_rollout_monitor.py`) separate from cluster creation (`repair_fingerprints.py`) so thresholds can evolve without touching audit classification.

### Task 1: Add Pure Rendered-State Exact-Match Helpers

**Files:**
- Create: `scripts/rendered_state_audit.py`
- Test: `tests/test_rendered_state_audit.py`

- [ ] **Step 1: Write the failing rendered-state tests**

```python
from rendered_state_audit import (
    DeterministicFieldExpectation,
    DeterministicFieldObservation,
    audit_rendered_option_field,
)


def test_audit_rendered_option_field_requires_exact_match():
    result = audit_rendered_option_field(
        DeterministicFieldExpectation(
            field_key="work_auth",
            label="Are you legally authorized to work in the US?",
            selected_labels=frozenset({"Yes"}),
            exact_count=1,
        ),
        DeterministicFieldObservation(
            field_key="work_auth",
            label="Are you legally authorized to work in the US?",
            selected_labels=frozenset({"No"}),
            screenshot_path="output/acme/pm/submit/greenhouse_autofill_review.png",
        ),
    )
    assert result.ok is False
    assert result.expected_labels == ["Yes"]
    assert result.observed_labels == ["No"]


def test_audit_rendered_option_field_rejects_extra_selection():
    result = audit_rendered_option_field(
        DeterministicFieldExpectation(
            field_key="countries",
            label="Country selection",
            selected_labels=frozenset({"United States", "Canada"}),
            exact_count=2,
        ),
        DeterministicFieldObservation(
            field_key="countries",
            label="Country selection",
            selected_labels=frozenset({"United States", "Canada", "Mexico"}),
            screenshot_path="output/acme/pm/submit/review.png",
        ),
    )
    assert result.ok is False
    assert "extra selections" in result.reason


def test_audit_rendered_option_field_normalizes_common_equivalents():
    result = audit_rendered_option_field(
        DeterministicFieldExpectation(
            field_key="country",
            label="Country",
            selected_labels=frozenset({"US"}),
            exact_count=1,
        ),
        DeterministicFieldObservation(
            field_key="country",
            label="Country",
            selected_labels=frozenset({"United States"}),
            screenshot_path="output/acme/pm/submit/review.png",
        ),
    )
    assert result.ok is True


def test_audit_rendered_option_field_enforces_exact_cardinality():
    result = audit_rendered_option_field(
        DeterministicFieldExpectation(
            field_key="interests",
            label="Areas of interest",
            selected_labels=frozenset({"Platform", "AI", "Growth"}),
            exact_count=3,
        ),
        DeterministicFieldObservation(
            field_key="interests",
            label="Areas of interest",
            selected_labels=frozenset({"Platform", "AI"}),
            screenshot_path="output/acme/pm/submit/review.png",
        ),
    )
    assert result.ok is False
    assert "expected 3 selections" in result.reason
```

- [ ] **Step 2: Run the rendered-state tests to verify they fail**

Run: `uv run python -m pytest tests/test_rendered_state_audit.py -v`
Expected: FAIL because `scripts/rendered_state_audit.py` does not exist yet.

- [ ] **Step 3: Implement the pure rendered-state comparator**

```python
# scripts/rendered_state_audit.py
from dataclasses import dataclass


@dataclass(frozen=True)
class DeterministicFieldExpectation:
    field_key: str
    label: str
    selected_labels: frozenset[str]
    exact_count: int | None = None


@dataclass(frozen=True)
class DeterministicFieldObservation:
    field_key: str
    label: str
    selected_labels: frozenset[str]
    screenshot_path: str


@dataclass(frozen=True)
class RenderedFieldAuditResult:
    ok: bool
    label: str
    reason: str
    expected_labels: list[str]
    observed_labels: list[str]
    screenshot_path: str


_ALIASES = {
    "us": "united states",
    "u.s.": "united states",
    "united states of america": "united states",
    "n/a": "not applicable",
    "na": "not applicable",
}


def normalize_option_label(value: str) -> str:
    normalized = " ".join(value.strip().casefold().split())
    return _ALIASES.get(normalized, normalized)


def audit_rendered_option_field(
    expected: DeterministicFieldExpectation,
    observed: DeterministicFieldObservation,
) -> RenderedFieldAuditResult:
    normalized_expected = {normalize_option_label(value) for value in expected.selected_labels}
    normalized_observed = {normalize_option_label(value) for value in observed.selected_labels}
    expected_labels = sorted(normalized_expected)
    observed_labels = sorted(normalized_observed)

    if expected.exact_count is not None and len(normalized_observed) != expected.exact_count:
        return RenderedFieldAuditResult(
            ok=False,
            label=expected.label,
            reason=f"{expected.label}: expected {expected.exact_count} selections but observed {len(normalized_observed)}",
            expected_labels=expected_labels,
            observed_labels=observed_labels,
            screenshot_path=observed.screenshot_path,
        )

    if normalized_expected != normalized_observed:
        missing = sorted(normalized_expected - normalized_observed)
        extra = sorted(normalized_observed - normalized_expected)
        parts = []
        if missing:
            parts.append("missing selections: " + ", ".join(missing))
        if extra:
            parts.append("extra selections: " + ", ".join(extra))
        return RenderedFieldAuditResult(
            ok=False,
            label=expected.label,
            reason=f"{expected.label}: " + "; ".join(parts),
            expected_labels=expected_labels,
            observed_labels=observed_labels,
            screenshot_path=observed.screenshot_path,
        )

    return RenderedFieldAuditResult(
        ok=True,
        label=expected.label,
        reason="",
        expected_labels=expected_labels,
        observed_labels=observed_labels,
        screenshot_path=observed.screenshot_path,
    )


def audit_rendered_option_fields(
    expected_fields: list[DeterministicFieldExpectation],
    observed_fields: list[DeterministicFieldObservation],
) -> RenderedFieldAuditResult:
    observed_by_key = {field.field_key: field for field in observed_fields}
    for expected in expected_fields:
        observed = observed_by_key.get(expected.field_key)
        if observed is None:
            return RenderedFieldAuditResult(
                ok=False,
                label=expected.label,
                reason=f"{expected.label}: rendered field missing from current-attempt evidence",
                expected_labels=sorted(normalize_option_label(value) for value in expected.selected_labels),
                observed_labels=[],
                screenshot_path="",
            )
        result = audit_rendered_option_field(expected, observed)
        if not result.ok:
            return result
    return RenderedFieldAuditResult(
        ok=True,
        label="",
        reason="",
        expected_labels=[],
        observed_labels=[],
        screenshot_path=observed_fields[0].screenshot_path if observed_fields else "",
    )
```

- [ ] **Step 4: Run the rendered-state tests again**

Run: `uv run python -m pytest tests/test_rendered_state_audit.py -v`
Expected: PASS

- [ ] **Step 5: Commit the rendered-state comparator**

```bash
git add scripts/rendered_state_audit.py tests/test_rendered_state_audit.py
git commit -m "feat: add rendered-state exact-match comparator"
```

### Task 2: Enforce Rendered-State Audit In The Draft Path

**Files:**
- Modify: `scripts/pipeline_audit_loop.py`
- Modify: `scripts/pipeline_draft_proof.py`
- Modify: `scripts/pipeline_orchestrator.py`
- Modify: `scripts/job_db.py`
- Test: `tests/test_pipeline_audit_loop.py`
- Test: `tests/test_pipeline_orchestrator.py`

- [ ] **Step 1: Write the failing audit-loop and orchestrator tests**

```python
def test_audit_draft_outcome_flags_rendered_option_mismatch(tmp_path):
    out_dir = tmp_path / "role"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    _write_json(
        submit_dir / "application_answers.json",
        {
            "answers": [
                {
                    "field_name": "work_auth",
                    "label": "Are you legally authorized to work in the US?",
                    "type": "select",
                    "selected_labels": ["Yes"],
                }
            ]
        },
    )
    _write_json(
        submit_dir / "greenhouse_autofill_report.json",
        {
            "fields": [
                {
                    "field_name": "work_auth",
                    "label": "Are you legally authorized to work in the US?",
                    "status": "filled",
                    "field_type": "select",
                    "selected_labels": ["No"],
                    "required": True,
                    "optional": False,
                }
            ],
            "unknown_questions": [],
        },
    )
    (submit_dir / "greenhouse_autofill_review.png").write_text("png", encoding="utf-8")

    decision = audit_draft_outcome(out_dir, board_name="greenhouse", missing_items=[])

    assert decision.kind == "repairable"
    assert decision.failure_type == "rendered_audit_mismatch"
    assert "expected selected values" in decision.reason


def test_handle_draft_audit_decision_increments_rendered_audit_failures(tmp_path):
    conn = _make_in_memory_db()
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    _write_json(
        submit_dir / "application_answers.json",
        {
            "answers": [
                {
                    "field_name": "work_auth",
                    "label": "Are you legally authorized to work in the US?",
                    "type": "select",
                    "selected_labels": ["Yes"],
                }
            ]
        },
    )
    _write_json(
        submit_dir / "greenhouse_autofill_report.json",
        {
            "fields": [
                {
                    "field_name": "work_auth",
                    "label": "Are you legally authorized to work in the US?",
                    "status": "filled",
                    "field_type": "select",
                    "selected_labels": ["No"],
                    "required": True,
                    "optional": False,
                }
            ],
            "unknown_questions": [],
        },
    )
    (submit_dir / "greenhouse_autofill_review.png").write_text("png", encoding="utf-8")
    conn.execute("INSERT INTO jobs (id, url, status, output_dir) VALUES (1, 'http://x', 'stopped', ?)", (str(out_dir),))
    conn.execute(
        "INSERT INTO job_metrics (job_id, audit_attempts, rendered_audit_failures) VALUES (1, 0, 0)"
    )
    conn.commit()
    result = _handle_draft_audit_decision(conn, 1, out_dir, board_name="greenhouse", missing_items=[])
    metrics = conn.execute("SELECT rendered_audit_failures FROM job_metrics WHERE job_id = 1").fetchone()
    assert result == "queued"
    assert metrics["rendered_audit_failures"] == 1
```

- [ ] **Step 2: Run the focused audit tests to verify they fail**

Run: `uv run python -m pytest tests/test_pipeline_audit_loop.py tests/test_pipeline_orchestrator.py -k "rendered" -v`
Expected: FAIL because the draft audit does not compare rendered selected values yet and `job_metrics.rendered_audit_failures` does not exist.

- [ ] **Step 3: Add rendered-state inputs, mismatch classification, and the new metric**

```python
# scripts/pipeline_draft_proof.py
from rendered_state_audit import DeterministicFieldExpectation, DeterministicFieldObservation


def current_rendered_audit_inputs(output_dir: Path, *, board_name: str | None) -> tuple[
    list[DeterministicFieldExpectation],
    list[DeterministicFieldObservation],
]:
    resolved = resolve_current_submit_artifacts(output_dir, board_name=board_name)
    report_payload = _load_report_payload(resolved.get("report_json"))
    answers_payload = _load_application_answers_payload(resolved.get("application_answers_json"))
    answer_rows = {
        str(row.get("field_name") or row.get("label") or "").strip(): row
        for row in list(answers_payload.get("answers") or [])
        if isinstance(row, dict)
    }
    screenshot_path = str(resolved.get("review_screenshot") or resolved.get("pre_submit_screenshot") or "")
    expected_fields = [
        DeterministicFieldExpectation(
            field_key=key,
            label=str(row.get("label") or key),
            selected_labels=frozenset(row.get("selected_labels") or [row.get("value")] if row.get("value") else []),
            exact_count=len(row.get("selected_labels") or [row.get("value")] if row.get("value") else []),
        )
        for key, row in answer_rows.items()
        if row.get("type") in {"radio", "select", "checkbox", "multiselect", "boolean"}
    ]
    observed_fields = [
        DeterministicFieldObservation(
            field_key=str(field.get("field_name") or field.get("label") or "").strip(),
            label=str(field.get("label") or field.get("field_name") or "").strip(),
            selected_labels=frozenset(field.get("selected_labels") or []),
            screenshot_path=screenshot_path,
        )
        for field in list(report_payload.get("fields") or [])
        if isinstance(field, dict)
        and field.get("field_type") in {"radio", "select", "checkbox", "multiselect", "boolean"}
    ]
    return expected_fields, observed_fields
```

```python
# scripts/pipeline_audit_loop.py
from rendered_state_audit import audit_rendered_option_fields


def audit_draft_outcome(
    output_dir: str | Path | None,
    *,
    board_name: str | None = None,
    missing_items: list[str] | None = None,
) -> AuditDecision:
    if output_dir is None:
        return AuditDecision(
            kind="repairable",
            failure_type="draft_audit_incomplete",
            reason="Draft audit could not run because the output directory is missing.",
            repair_actions=("requeue",),
        )
    out_dir = Path(output_dir)
    expected_fields, observed_fields = current_rendered_audit_inputs(out_dir, board_name=board_name)
    rendered_result = audit_rendered_option_fields(expected_fields, observed_fields)
    if not rendered_result.ok:
        return AuditDecision(
            kind="repairable",
            failure_type="rendered_audit_mismatch",
            reason=rendered_result.reason,
            repair_actions=("clear_current_attempt_artifacts", "requeue"),
            artifacts={"screenshot": rendered_result.screenshot_path},
        )
```

```python
# scripts/job_db.py
_SCHEMA_UPDATES = (
    ("rendered_audit_failures", "ALTER TABLE job_metrics ADD COLUMN rendered_audit_failures INTEGER DEFAULT 0"),
    ("last_repair_cluster_id", "ALTER TABLE job_metrics ADD COLUMN last_repair_cluster_id TEXT"),
    ("last_rollout_sha", "ALTER TABLE job_metrics ADD COLUMN last_rollout_sha TEXT"),
)
```

- [ ] **Step 4: Count rendered mismatches in the orchestrator**

```python
# scripts/pipeline_orchestrator.py
if decision.failure_type == "rendered_audit_mismatch":
    metrics = get_job_metrics(conn, job_id) or {}
    update_job_metrics(
        conn,
        job_id,
        rendered_audit_failures=int(metrics.get("rendered_audit_failures", 0) or 0) + 1,
    )
```

- [ ] **Step 5: Run the focused audit tests again**

Run: `uv run python -m pytest tests/test_pipeline_audit_loop.py tests/test_pipeline_orchestrator.py -k "rendered" -v`
Expected: PASS

- [ ] **Step 6: Commit the rendered-state audit integration**

```bash
git add scripts/pipeline_audit_loop.py scripts/pipeline_draft_proof.py scripts/pipeline_orchestrator.py scripts/job_db.py tests/test_pipeline_audit_loop.py tests/test_pipeline_orchestrator.py
git commit -m "feat: enforce rendered-state audit for draft jobs"
```

### Task 3: Add Repair Fingerprinting, Persistence, And Cluster Reports

**Files:**
- Create: `scripts/repair_fingerprints.py`
- Modify: `scripts/job_db.py`
- Modify: `scripts/pipeline_audit_loop.py`
- Modify: `scripts/pipeline_orchestrator.py`
- Create: `output/_audit/repair_clusters/.gitkeep`
- Test: `tests/test_repair_fingerprints.py`
- Test: `tests/test_pipeline_orchestrator.py`

- [ ] **Step 1: Write the failing fingerprint and cluster tests**

```python
from repair_fingerprints import build_repair_fingerprint, upsert_repair_cluster


def test_build_repair_fingerprint_groups_equivalent_greenhouse_mismatch():
    left = build_repair_fingerprint(
        board="greenhouse",
        phase="draft_audit",
        failure_type="rendered_audit_mismatch",
        message="Work authorization expected Yes observed No",
        field_labels=["Are you legally authorized to work in the US?"],
    )
    right = build_repair_fingerprint(
        board="greenhouse",
        phase="draft_audit",
        failure_type="rendered_audit_mismatch",
        message="Work authorization expected YES observed NO",
        field_labels=["Are you legally authorized to work in the US?"],
    )
    assert left == right


def test_upsert_repair_cluster_reuses_existing_fingerprint(tmp_path):
    conn = init_db(tmp_path / "jobs.db", check_same_thread=False)
    fingerprint = "greenhouse:draft_audit:rendered_audit_mismatch:work_auth"
    first = upsert_repair_cluster(conn, fingerprint=fingerprint, summary="first", job_id=10)
    second = upsert_repair_cluster(conn, fingerprint=fingerprint, summary="second", job_id=11)
    assert first["id"] == second["id"]
    assert sorted(second["representative_job_ids"]) == [10, 11]
```

- [ ] **Step 2: Run the fingerprint tests to verify they fail**

Run: `uv run python -m pytest tests/test_repair_fingerprints.py -v`
Expected: FAIL because the fingerprint module and repair-cluster tables do not exist yet.

- [ ] **Step 3: Add repair-cluster, rollout, and runtime-flag schema helpers**

```python
# scripts/job_db.py
CREATE TABLE IF NOT EXISTS repair_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'open',
    eligibility TEXT NOT NULL DEFAULT 'unknown',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    representative_job_ids TEXT NOT NULL DEFAULT '[]',
    latest_summary TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS repair_rollouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id INTEGER NOT NULL REFERENCES repair_clusters(id),
    commit_sha TEXT NOT NULL,
    status TEXT NOT NULL,
    baseline_metrics_json TEXT NOT NULL DEFAULT '{}',
    post_fix_metrics_json TEXT NOT NULL DEFAULT '{}',
    revert_sha TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runtime_flags (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

- [ ] **Step 4: Implement fingerprint builders and repair-cluster reports**

```python
# scripts/repair_fingerprints.py
def build_repair_fingerprint(
    *,
    board: str,
    phase: str,
    failure_type: str,
    message: str,
    field_labels: list[str] | None = None,
) -> str:
    normalized = {
        "board": " ".join(board.casefold().split()),
        "phase": " ".join(phase.casefold().split()),
        "failure_type": " ".join(failure_type.casefold().split()),
        "message": " ".join(message.casefold().split())[:160],
        "field_labels": sorted(" ".join(label.casefold().split()) for label in (field_labels or [])),
    }
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def upsert_repair_cluster(conn: sqlite3.Connection, *, fingerprint: str, summary: str, job_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM repair_clusters WHERE fingerprint = ?", (fingerprint,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO repair_clusters (fingerprint, latest_summary, representative_job_ids) VALUES (?, ?, ?)",
            (fingerprint, summary, json.dumps([job_id])),
        )
    else:
        job_ids = sorted(set(json.loads(row["representative_job_ids"]) + [job_id]))
        conn.execute(
            "UPDATE repair_clusters SET latest_summary = ?, representative_job_ids = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (summary, json.dumps(job_ids), row["id"]),
        )
    conn.commit()
    return conn.execute("SELECT * FROM repair_clusters WHERE fingerprint = ?", (fingerprint,)).fetchone()


def write_repair_cluster_report(output_root: Path, *, cluster_row: sqlite3.Row, suggestions: list[str]) -> Path:
    report_path = output_root / "_audit" / "repair_clusters" / f"{cluster_row['fingerprint']}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        "\n".join(
            [
                "# Repair Cluster",
                "",
                f"- Fingerprint: `{cluster_row['fingerprint']}`",
                f"- Status: `{cluster_row['status']}`",
                f"- Representative jobs: `{cluster_row['representative_job_ids']}`",
                "",
                "## Latest Summary",
                "",
                cluster_row["latest_summary"],
                "",
                "## Suggestions",
                "",
                *[f"- {suggestion}" for suggestion in suggestions],
            ]
        ),
        encoding="utf-8",
    )
    return report_path


def refresh_active_repair_failure_index(output_root: Path) -> Path:
    index_path = output_root / "_audit" / "active_repair_failures.md"
    cluster_dir = output_root / "_audit" / "repair_clusters"
    lines = ["# Active Repair Failures", ""]
    for note_path in sorted(cluster_dir.glob("*.md")):
        rel = note_path.relative_to(output_root / "_audit")
        lines.append(f"- [{note_path.stem}](./{rel.as_posix()})")
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index_path
```

```python
# scripts/pipeline_orchestrator.py
cluster = record_repairable_failure_cluster(
    conn,
    job_id=job_id,
    board=board_name or board,
    phase="draft_audit",
    failure_type=decision.failure_type or "unknown",
    summary=decision.reason,
    artifacts=decision.artifacts,
)
update_job_metrics(conn, job_id, last_repair_cluster_id=str(cluster["id"]))
```

- [ ] **Step 5: Run the fingerprint and repair-cluster tests again**

Run: `uv run python -m pytest tests/test_repair_fingerprints.py tests/test_pipeline_orchestrator.py -k "cluster or repairable" -v`
Expected: PASS

- [ ] **Step 6: Commit fingerprinting and repair-cluster persistence**

```bash
git add scripts/repair_fingerprints.py scripts/job_db.py scripts/pipeline_audit_loop.py scripts/pipeline_orchestrator.py tests/test_repair_fingerprints.py tests/test_pipeline_orchestrator.py output/_audit/repair_clusters/.gitkeep
git commit -m "feat: persist repair clusters and rollout metadata"
```

### Task 4: Teach The Shared OpenAI Path About Reasoning Effort And Repair Defaults

**Files:**
- Modify: `scripts/llm_provider.py`
- Modify: `scripts/openai_provider.py`
- Test: `tests/test_llm_provider.py`
- Test: `tests/test_openai_provider.py`

- [ ] **Step 1: Write the failing provider tests**

```python
def test_effective_provider_settings_reads_openai_reasoning_effort():
    provider = load_module("llm_provider", "scripts/llm_provider.py")
    openai = provider.effective_provider_settings(
        "openai",
        environ={"OPENAI_MODEL": "gpt-5.4", "OPENAI_REASONING_EFFORT": "xhigh"},
    )
    assert openai["model"] == "gpt-5.4"
    assert openai["reasoning_effort"] == "xhigh"


def test_provider_command_builds_openai_exec_with_reasoning_effort():
    provider = load_module("llm_provider", "scripts/llm_provider.py")
    command = provider.provider_command("openai", "Reply with OK.", environ={"OPENAI_REASONING_EFFORT": "xhigh"})
    assert "--reasoning-effort" in command
    assert command[command.index("--reasoning-effort") + 1] == "xhigh"
```

```python
def test_main_passes_reasoning_effort_to_responses_api():
    with patch("sys.argv", ["openai_provider.py", "--reasoning-effort", "xhigh", "Reply with OK."]):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            mock_client = Mock()
            mock_client.responses.create.return_value = Mock(output_text="OK", output=[])
            with patch("openai.OpenAI", return_value=mock_client):
                provider = load_module("openai_provider", "scripts/openai_provider.py")
                assert provider.main() == 0
        mock_client.responses.create.assert_called_once_with(
            model="gpt-5.4",
            input="Reply with OK.",
            reasoning={"effort": "xhigh"},
        )
```

- [ ] **Step 2: Run the provider tests to verify they fail**

Run: `uv run python -m pytest tests/test_llm_provider.py tests/test_openai_provider.py -k "reasoning_effort" -v`
Expected: FAIL because OpenAI settings and the provider shim do not expose `reasoning.effort` yet.

- [ ] **Step 3: Add OpenAI reasoning-effort plumbing**

```python
# scripts/llm_provider.py
if provider == "openai":
    return {
        "model": _clean(env.get("OPENAI_MODEL")) or DEFAULT_OPENAI_MODEL,
        "reasoning_effort": _clean(env.get("OPENAI_REASONING_EFFORT")) or "",
        "effort": "",
        "permission_mode": "",
        "setting_sources": "",
        "no_session_persistence": "",
        "disable_slash_commands": "",
        "strict_mcp_config": "",
        "mcp_config": "",
        "asset_primary_timeout_seconds": "",
        "asset_fallback_provider": "",
        "profile": "",
        "approval_policy": "",
        "sandbox_mode": "",
        "extra_args": _clean(env.get("OPENAI_EXTRA_ARGS")) or "",
        "timeout_seconds": str(provider_timeout_seconds(environ=env)),
        "asset_timeout_seconds": str(asset_provider_timeout_seconds(environ=env)),
    }
```

```python
# scripts/llm_provider.py
if provider == "openai":
    cmd = [sys.executable, str(OPENAI_PROVIDER_SCRIPT)]
    cmd.extend(["--model", settings["model"]])
    if settings["reasoning_effort"]:
        cmd.extend(["--reasoning-effort", settings["reasoning_effort"]])
```

```python
# scripts/openai_provider.py
parser.add_argument(
    "--reasoning-effort",
    choices=("none", "low", "medium", "high", "xhigh"),
    default="",
    help="Responses API reasoning.effort value.",
)
kwargs = {
    "model": args.model,
    "input": prompt,
}
if args.reasoning_effort:
    kwargs["reasoning"] = {"effort": args.reasoning_effort}
```

- [ ] **Step 4: Run the provider tests again**

Run: `uv run python -m pytest tests/test_llm_provider.py tests/test_openai_provider.py -k "reasoning_effort" -v`
Expected: PASS

- [ ] **Step 5: Commit the OpenAI reasoning-effort support**

```bash
git add scripts/llm_provider.py scripts/openai_provider.py tests/test_llm_provider.py tests/test_openai_provider.py
git commit -m "feat: support OpenAI reasoning effort in provider shim"
```

### Task 5: Add The Singleton Repair Supervisor Backend Process

**Files:**
- Create: `scripts/repair_runtime.py`
- Create: `scripts/repair_supervisor.py`
- Modify: `scripts/job_worker.py`
- Modify: `scripts/job_web.py`
- Test: `tests/test_repair_supervisor.py`
- Test: `tests/test_job_worker.py`
- Test: `tests/test_job_web.py`

- [ ] **Step 1: Write the failing bootstrap and config tests**

```python
from repair_runtime import RepairSupervisorConfig, ensure_repair_supervisor_running


def test_repair_supervisor_config_defaults_to_openai_gpt_5_4_xhigh():
    config = RepairSupervisorConfig.from_env({})
    assert config.provider == "openai"
    assert config.model == "gpt-5.4"
    assert config.reasoning_effort == "xhigh"


def test_ensure_repair_supervisor_running_starts_singleton(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: spawned.append(args) or DummyProc(123))
    ensure_repair_supervisor_running(project_root=tmp_path)
    ensure_repair_supervisor_running(project_root=tmp_path)
    assert len(spawned) == 1


def test_worker_start_bootstraps_repair_supervisor(monkeypatch, tmp_path):
    class DummyPool:
        def __init__(self, *args, **kwargs):
            self.is_running = False

        def start(self):
            return None

        def stop(self):
            return None

    called = []
    monkeypatch.setattr(job_worker, "ensure_repair_supervisor_running", lambda **_: called.append("started"))
    monkeypatch.setattr(job_worker, "WorkerPool", DummyPool)
    monkeypatch.setattr(job_worker, "_kill_stale_workers", lambda *_: None)
    with patch.object(sys, "argv", ["job_worker.py", "--workers", "1"]):
        job_worker.main()
    assert called == ["started"]
```

- [ ] **Step 2: Run the bootstrap tests to verify they fail**

Run: `uv run python -m pytest tests/test_repair_supervisor.py tests/test_job_worker.py tests/test_job_web.py -k "repair_supervisor" -v`
Expected: FAIL because no repair-runtime module or supervisor bootstrap exists.

- [ ] **Step 3: Implement repair runtime config, PID files, and singleton start helpers**

```python
# scripts/repair_runtime.py
@dataclass(frozen=True)
class RepairSupervisorConfig:
    provider: str
    model: str
    reasoning_effort: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "RepairSupervisorConfig":
        return cls(
            provider=env.get("ASSET_REPAIR_LLM_PROVIDER", "openai"),
            model=env.get("ASSET_REPAIR_OPENAI_MODEL", "gpt-5.4"),
            reasoning_effort=env.get("ASSET_REPAIR_OPENAI_REASONING_EFFORT", "xhigh"),
        )

def ensure_repair_supervisor_running(*, project_root: Path) -> None:
    if is_repair_supervisor_running(project_root=project_root):
        return
    env = os.environ.copy()
    config = RepairSupervisorConfig.from_env(env)
    env.update(
        {
            "ASSET_REPAIR_LLM_PROVIDER": config.provider,
            "ASSET_REPAIR_OPENAI_MODEL": config.model,
            "ASSET_REPAIR_OPENAI_REASONING_EFFORT": config.reasoning_effort,
        }
    )
    proc = subprocess.Popen(
        ["uv", "run", "--project", str(project_root), "python", str(project_root / "scripts" / "repair_supervisor.py")],
        cwd=project_root,
        env=env,
        start_new_session=True,
    )
    (project_root / "jobs.db.repair_supervisor.pid").write_text(str(proc.pid), encoding="utf-8")


def stop_repair_supervisor(*, project_root: Path) -> None:
    pid_path = project_root / "jobs.db.repair_supervisor.pid"
    if not pid_path.exists():
        return
    pid = int(pid_path.read_text(encoding="utf-8").strip())
    os.kill(pid, signal.SIGTERM)
    pid_path.unlink(missing_ok=True)
```

- [ ] **Step 4: Start and stop the supervisor with the worker lifecycle**

```python
# scripts/job_worker.py
from repair_runtime import ensure_repair_supervisor_running, stop_repair_supervisor


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    ensure_repair_supervisor_running(project_root=PROJECT_ROOT)
    pool = WorkerPool(db_path=PROJECT_ROOT / "jobs.db", num_workers=args.workers, headless=_headless, headless_explicit=_headless_explicit)
    pool.start()
    def _shutdown(signum: int, frame) -> None:
        pool.stop()
        stop_repair_supervisor(project_root=PROJECT_ROOT)
```

```python
# scripts/job_web.py
def _read_runtime_services() -> dict:
    return {
        "workers_running": is_worker_running(),
        "repair_supervisor_running": is_repair_supervisor_running(project_root=PROJECT_ROOT),
    }
```

- [ ] **Step 5: Run the bootstrap tests again**

Run: `uv run python -m pytest tests/test_repair_supervisor.py tests/test_job_worker.py tests/test_job_web.py -k "repair_supervisor" -v`
Expected: PASS

- [ ] **Step 6: Commit the supervisor bootstrap**

```bash
git add scripts/repair_runtime.py scripts/repair_supervisor.py scripts/job_worker.py scripts/job_web.py tests/test_repair_supervisor.py tests/test_job_worker.py tests/test_job_web.py
git commit -m "feat: launch singleton repair supervisor with workers"
```

### Task 6: Implement The Bounded Repair Loop, Canary Reruns, And Auto-Redraft

**Files:**
- Create: `scripts/repair_git.py`
- Modify: `scripts/repair_supervisor.py`
- Modify: `scripts/job_db.py`
- Modify: `scripts/pipeline_orchestrator.py`
- Test: `tests/test_repair_supervisor.py`
- Test: `tests/test_pipeline_orchestrator.py`

- [ ] **Step 1: Write the failing repair-loop tests**

```python
def test_repair_supervisor_requires_failing_regression_before_promotion(monkeypatch, tmp_path):
    supervisor = RepairSupervisor(project_root=tmp_path, db_path=tmp_path / "jobs.db")
    monkeypatch.setattr(supervisor, "_run_targeted_verification", lambda *_: True)
    monkeypatch.setattr(supervisor, "_detect_new_regression_test", lambda *_: False)
    result = supervisor._attempt_cluster_repair(cluster_id=1)
    assert result.status == "failed"
    assert result.reason == "missing_failing_regression"


def test_successful_repair_pushes_and_requeues_jobs(monkeypatch, tmp_path):
    supervisor = RepairSupervisor(project_root=tmp_path, db_path=tmp_path / "jobs.db")
    monkeypatch.setattr(supervisor, "_run_targeted_verification", lambda *_: True)
    monkeypatch.setattr(supervisor, "_run_canary_jobs", lambda *_: CanaryOutcome(ok=True, job_ids=[42, 43]))
    pushed = []
    monkeypatch.setattr(supervisor, "_push_main", lambda sha: pushed.append(sha))
    candidate = PromotedRepair(pre_sha="deadbeef", promoted_sha="abc1234", cluster_id=1, job_ids=[42, 43])
    result = supervisor._promote_repair_candidate(candidate)
    assert pushed == ["abc1234"]
    assert result.status == "promoted"
```

- [ ] **Step 2: Run the repair-loop tests to verify they fail**

Run: `uv run python -m pytest tests/test_repair_supervisor.py tests/test_pipeline_orchestrator.py -k "repair_loop or canary" -v`
Expected: FAIL because the supervisor has no repair loop, no git helpers, and no canary promotion path.

- [ ] **Step 3: Add isolated git helpers and the repair packet shape**

```python
# scripts/repair_git.py
@dataclass(frozen=True)
class RepairWorktree:
    path: Path
    branch: str
    base_sha: str

def create_repair_worktree(*, project_root: Path, cluster_fingerprint: str) -> RepairWorktree:
    branch = f"autofix/{cluster_fingerprint[:12]}"
    base_sha = subprocess.run(
        ["git", "rev-parse", "main"],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    path = project_root.parent / f"{project_root.name}-repair-{cluster_fingerprint[:12]}"
    subprocess.run(
        ["git", "worktree", "add", "-B", branch, str(path), "main"],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return RepairWorktree(path=path, branch=branch, base_sha=base_sha)


def commit_repair_candidate(worktree: RepairWorktree, *, message: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=worktree.path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", message], cwd=worktree.path, check=True, capture_output=True, text=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree.path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def cleanup_repair_worktree(worktree: RepairWorktree) -> None:
    subprocess.run(["git", "worktree", "remove", "--force", str(worktree.path)], check=True, cwd=worktree.path.parent)
```

```python
# scripts/repair_supervisor.py
@dataclass(frozen=True)
class RepairPacket:
    cluster_id: int
    fingerprint: str
    job_ids: list[int]
    prompt: str
    likely_files: list[str]
    verification_commands: list[list[str]]
```

- [ ] **Step 4: Implement the bounded repair loop and canary promotion path**

```python
# scripts/repair_supervisor.py
def _attempt_cluster_repair(self, cluster_id: int) -> RepairAttemptResult:
    packet = self._build_repair_packet(cluster_id)
    worktree = create_repair_worktree(project_root=self.project_root, cluster_fingerprint=packet.fingerprint)
    candidate_sha = self._run_repair_agent(packet, worktree)
    self._require_failing_regression(packet, worktree)
    self._run_targeted_verification(packet, worktree)
    promoted = self._promote_locally_for_canary(candidate_sha)
    canary = self._run_canary_jobs(packet.job_ids[:3])
    if not canary.ok:
        self._rollback_local_promotion(promoted)
        return RepairAttemptResult(status="failed", reason="canary_failed")
    self._push_main(promoted.promoted_sha)
    self._requeue_jobs(packet.job_ids)
    return RepairAttemptResult(status="promoted", reason="")
```

- [ ] **Step 5: Run the repair-loop tests again**

Run: `uv run python -m pytest tests/test_repair_supervisor.py tests/test_pipeline_orchestrator.py -k "repair_loop or canary" -v`
Expected: PASS

- [ ] **Step 6: Commit the repair loop and canary redraft path**

```bash
git add scripts/repair_git.py scripts/repair_supervisor.py scripts/job_db.py scripts/pipeline_orchestrator.py tests/test_repair_supervisor.py tests/test_pipeline_orchestrator.py
git commit -m "feat: add bounded repair loop with canary promotion"
```

### Task 7: Add Rollout Monitoring And Pause-Confirm-Revert

**Files:**
- Create: `scripts/repair_rollout_monitor.py`
- Modify: `scripts/repair_supervisor.py`
- Modify: `scripts/job_worker.py`
- Modify: `scripts/job_db.py`
- Test: `tests/test_repair_rollout_monitor.py`
- Test: `tests/test_job_worker.py`

- [ ] **Step 1: Write the failing rollout-monitor tests**

```python
from repair_rollout_monitor import evaluate_rollout, RolloutDecision


def test_rollout_monitor_pauses_new_work_when_original_fingerprint_repeats():
    decision = evaluate_rollout(
        baseline={"comparable_jobs": 5, "hard_failures": 0, "fingerprint_hits": 0},
        observed={"comparable_jobs": 5, "hard_failures": 2, "fingerprint_hits": 2},
    )
    assert decision == RolloutDecision.PAUSE_AND_CONFIRM


def test_rollout_monitor_requires_confirmation_before_revert():
    state = RolloutConfirmationState(first_signal_seen=True, confirmation_passed=False)
    result = maybe_confirm_and_revert(state, confirmed=True)
    assert result.action == "revert"
    assert result.resume_queue is False


def test_coordinator_skips_jobs_when_pause_flag_is_set(db, coordinator_factory):
    add_job(db, url="https://boards.greenhouse.io/acme/jobs/1")
    set_runtime_flag(db, "pause_new_work", "1")
    coord = coordinator_factory()
    assert coord.next_job(active_boards=set()) is None
```

- [ ] **Step 2: Run the rollout tests to verify they fail**

Run: `uv run python -m pytest tests/test_repair_rollout_monitor.py tests/test_job_worker.py -k "pause or rollout" -v`
Expected: FAIL because there is no rollout monitor and `Coordinator.next_job()` does not honor runtime pause flags.

- [ ] **Step 3: Implement rollout evaluation and runtime pause helpers**

```python
# scripts/repair_rollout_monitor.py
class RolloutDecision(str, Enum):
    HEALTHY = "healthy"
    PAUSE_AND_CONFIRM = "pause_and_confirm"
    REVERT = "revert"


def evaluate_rollout(*, baseline: dict, observed: dict) -> RolloutDecision:
    baseline_jobs = max(int(baseline.get("comparable_jobs", 0) or 0), 1)
    observed_jobs = int(observed.get("comparable_jobs", 0) or 0)
    if observed_jobs >= 5 and int(observed.get("fingerprint_hits", 0) or 0) >= 2:
        return RolloutDecision.PAUSE_AND_CONFIRM
    baseline_hard_failure_rate = (int(baseline.get("hard_failures", 0) or 0) / baseline_jobs)
    observed_hard_failure_rate = (
        int(observed.get("hard_failures", 0) or 0) / max(observed_jobs, 1)
        if observed_jobs
        else 0.0
    )
    if observed_jobs >= 10 and (observed_hard_failure_rate - baseline_hard_failure_rate) >= 0.15:
        return RolloutDecision.PAUSE_AND_CONFIRM
    return RolloutDecision.HEALTHY


def maybe_confirm_and_revert(state: RolloutConfirmationState, confirmed: bool) -> RolloutConfirmationResult:
    if not state.first_signal_seen:
        return RolloutConfirmationResult(action="pause", resume_queue=False)
    if confirmed:
        return RolloutConfirmationResult(action="revert", resume_queue=False)
    return RolloutConfirmationResult(action="resume", resume_queue=True)
```

```python
# scripts/job_db.py
def set_runtime_flag(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO runtime_flags (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
        (key, value),
    )
    conn.commit()


def get_runtime_flag(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM runtime_flags WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])
```

- [ ] **Step 4: Stop claiming fresh jobs while a rollout is paused**

```python
# scripts/job_worker.py
from job_db import get_runtime_flag


def next_job(self, active_boards: set[str], *, board_cooldown_until=None) -> dict | None:
    conn = self._get_conn()
    if get_runtime_flag(conn, "pause_new_work") == "1":
        log.info("next_job: queue paused by repair rollout monitor")
        return None
    pending = get_pending_jobs(conn, limit=50)
    for job in pending:
        progress = job.get("progress") or ""
        if progress.startswith("claimed:"):
            continue
        return job
    return None
```

```python
# scripts/repair_supervisor.py
decision = evaluate_rollout(baseline=baseline_metrics, observed=observed_metrics)
if decision is RolloutDecision.PAUSE_AND_CONFIRM:
    set_runtime_flag(conn, "pause_new_work", "1")
    confirmation = self._confirm_rollout_regression(rollout_id)
    if confirmation.confirmed:
        self._revert_rollout(rollout_id)
    else:
        set_runtime_flag(conn, "pause_new_work", "0")
```

- [ ] **Step 5: Run the rollout tests again**

Run: `uv run python -m pytest tests/test_repair_rollout_monitor.py tests/test_job_worker.py -k "pause or rollout" -v`
Expected: PASS

- [ ] **Step 6: Commit the rollout monitor and pause-confirm-revert path**

```bash
git add scripts/repair_rollout_monitor.py scripts/repair_supervisor.py scripts/job_worker.py scripts/job_db.py tests/test_repair_rollout_monitor.py tests/test_job_worker.py
git commit -m "feat: monitor repair rollouts and pause queue on regressions"
```

### Task 8: Update Docs And Run Full Verification

**Files:**
- Modify: `docs/worker-pipeline-patterns.md`
- Modify: `docs/operational-rules.md`

- [ ] **Step 1: Update the worker and operational docs**

```markdown
## Repair Supervisor

Workers now launch a singleton repair supervisor that watches repairable failure clusters, uses OpenAI `gpt-5.4` with `xhigh` reasoning, and only pushes verified fixes after tests plus live canaries pass.

## Rollout Guard

If a promoted repair causes comparable post-fix regressions, the runtime pauses new work, performs one confirmation pass, and only then auto-reverts the rollout.
```

- [ ] **Step 2: Run the focused new-test suite**

Run: `uv run python -m pytest tests/test_rendered_state_audit.py tests/test_pipeline_audit_loop.py tests/test_repair_fingerprints.py tests/test_repair_supervisor.py tests/test_repair_rollout_monitor.py tests/test_llm_provider.py tests/test_openai_provider.py tests/test_job_worker.py tests/test_pipeline_orchestrator.py -v`
Expected: PASS

- [ ] **Step 3: Run repo-wide verification**

Run: `uv run python -m pytest tests/ -v`
Expected: PASS

Run: `uv run ruff check scripts/ tests/`
Expected: PASS

Run: `uv run python scripts/check_architecture.py`
Expected: PASS

Run: `uv run python scripts/sync_agent_files.py --check`
Expected: PASS

Run: `uv run python scripts/check_agent_docs.py`
Expected: PASS

- [ ] **Step 4: Commit the docs and final verification pass**

```bash
git add docs/worker-pipeline-patterns.md docs/operational-rules.md
git commit -m "docs: describe repair supervisor and rollout guard"
```
