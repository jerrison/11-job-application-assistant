# Captcha Manual Browser Escalation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a draft run hits captcha in headless mode, automatically rerun that submit phase once in a visible browser, keep the window open for manual solve, and only stop after that headed attempt fails.

**Architecture:** Keep the change in the shared Phase 3 orchestrator path. Detect captcha interruption from the current-attempt submission-result artifact or raw exit code, rerun once without `--headless`, and let the existing `awaiting_captcha.json` plus focus-browser path handle the live wait. Leave auto-submit behavior unchanged.

**Tech Stack:** Python 3.14, sqlite3 job state, Playwright subprocesses, pytest, Ruff

**Spec:** `docs/superpowers/specs/2026-03-31-captcha-manual-browser-escalation-design.md`

---

## File Map

| File | Responsibility | Action |
|------|---------------|--------|
| `scripts/pipeline_orchestrator.py` | Owns Phase 3 submit/draft orchestration, subprocess launch, status transitions | Extract one submit-attempt helper and add one-time headless-to-headed captcha escalation for draft mode |
| `tests/test_pipeline_orchestrator.py` | Regression coverage for `process_job()` behavior | Add tests for headless captcha escalation, exhausted headed retry, and unchanged auto-submit behavior |
| `docs/worker-pipeline-patterns.md` | Worker/runtime behavior reference | Document draft-mode captcha escalation and the `awaiting_captcha` handoff |

---

### Task 1: Add Focused Regression Tests For Draft-Mode Captcha Escalation

**Files:**
- Modify: `tests/test_pipeline_orchestrator.py`
- Test: `tests/test_pipeline_orchestrator.py`

- [ ] **Step 1: Add failing tests that pin the desired Phase 3 behavior**

```python
def test_process_job_draft_mode_relaunches_headed_after_headless_skipped_captcha(tmp_path):
    import pipeline_orchestrator

    conn = sqlite3.connect(":memory:")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)

    job = {
        "id": 1,
        "url": "https://jobs.avature.net/careers/JobDetail/123",
        "board_url": "https://jobs.avature.net/careers/JobDetail/123",
        "board": "avature",
        "status": "submitting",
        "output_dir": str(out_dir),
        "source": "direct",
        "company": "Example Co",
        "role_title": "principal-pm",
        "failure_type": None,
    }

    calls: list[list[str]] = []

    class _DummyThread:
        def __init__(self, *args, **kwargs):
            pass
        def start(self):
            pass
        def join(self, timeout=None):
            pass

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if len(calls) == 1:
            (submit_dir / "application_submission_result.json").write_text(
                json.dumps(
                    {
                        "status": "skipped_captcha",
                        "board": "avature",
                        "message": "Submission skipped: captcha required. Moving on to next job.",
                    }
                ),
                encoding="utf-8",
            )
        else:
            (submit_dir / "application_submission_result.json").unlink(missing_ok=True)
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = ""
        completed.stderr = ""
        return completed

    with (
        patch("job_db.get_job", return_value=job),
        patch("job_db.ensure_job_metrics"),
        patch("job_db.start_phase", return_value=1),
        patch("job_db.end_phase"),
        patch("job_db.log_event"),
        patch("job_db.update_job_metrics"),
        patch("job_db.update_progress"),
        patch("job_db.get_job_metrics", return_value=None),
        patch("job_db.get_recent_auth_failures", return_value=0),
        patch("job_db.update_status"),
        patch.object(pipeline_orchestrator, "_detect_and_log_content_edits"),
        patch.object(pipeline_orchestrator, "_finalize_pending_answer_refresh", return_value=None),
        patch.object(pipeline_orchestrator.threading, "Thread", _DummyThread),
        patch.object(pipeline_orchestrator.subprocess, "run", side_effect=_fake_run),
        patch("draft_manager.generate_draft_summary"),
        patch.object(pipeline_orchestrator, "_sync_draft_proof_blockers"),
        patch.object(pipeline_orchestrator, "_validate_draft_completeness", return_value=[]),
    ):
        status = pipeline_orchestrator.process_job(conn, 1, auto_submit=False)

    assert status == "draft"
    assert len(calls) == 2
    assert "--headless" in calls[0]
    assert "--headless" not in calls[1]


def test_process_job_draft_mode_stops_after_headed_captcha_retry_exhausted(tmp_path):
    import pipeline_orchestrator

    conn = sqlite3.connect(":memory:")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)

    job = {
        "id": 1,
        "url": "https://jobs.avature.net/careers/JobDetail/123",
        "board_url": "https://jobs.avature.net/careers/JobDetail/123",
        "board": "avature",
        "status": "submitting",
        "output_dir": str(out_dir),
        "source": "direct",
        "company": "Example Co",
        "role_title": "principal-pm",
        "failure_type": None,
    }

    calls: list[list[str]] = []
    update_calls: list[tuple[str, dict]] = []

    class _DummyThread:
        def __init__(self, *args, **kwargs):
            pass
        def start(self):
            pass
        def join(self, timeout=None):
            pass

    def _fake_update_status(_conn, _job_id, status, **kwargs):
        update_calls.append((status, kwargs))

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        (submit_dir / "application_submission_result.json").write_text(
            json.dumps(
                {
                    "status": "skipped_captcha",
                    "board": "avature",
                    "message": "Submission skipped: captcha required. Moving on to next job.",
                }
            ),
            encoding="utf-8",
        )
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = ""
        completed.stderr = ""
        return completed

    with (
        patch("job_db.get_job", return_value=job),
        patch("job_db.ensure_job_metrics"),
        patch("job_db.start_phase", return_value=1),
        patch("job_db.end_phase"),
        patch("job_db.log_event"),
        patch("job_db.update_job_metrics"),
        patch("job_db.update_progress"),
        patch("job_db.get_job_metrics", return_value=None),
        patch("job_db.get_recent_auth_failures", return_value=0),
        patch("job_db.update_status", side_effect=_fake_update_status),
        patch.object(pipeline_orchestrator, "_detect_and_log_content_edits"),
        patch.object(pipeline_orchestrator, "_finalize_pending_answer_refresh", return_value=None),
        patch.object(pipeline_orchestrator.threading, "Thread", _DummyThread),
        patch.object(pipeline_orchestrator.subprocess, "run", side_effect=_fake_run),
        patch("draft_manager.generate_draft_summary"),
        patch.object(pipeline_orchestrator, "_sync_draft_proof_blockers"),
        patch.object(pipeline_orchestrator, "_validate_draft_completeness", return_value=[]),
    ):
        status = pipeline_orchestrator.process_job(conn, 1, auto_submit=False)

    assert status == "stopped"
    assert len(calls) == 2
    assert "--headless" in calls[0]
    assert "--headless" not in calls[1]
    assert update_calls[-1][1]["failure_type"] == "skipped_captcha"


def test_process_job_submit_mode_does_not_escalate_captcha_to_headed_retry(tmp_path):
    import pipeline_orchestrator

    conn = sqlite3.connect(":memory:")
    out_dir = tmp_path / "job-output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)

    job = {
        "id": 1,
        "url": "https://jobs.avature.net/careers/JobDetail/123",
        "board_url": "https://jobs.avature.net/careers/JobDetail/123",
        "board": "avature",
        "status": "submitting",
        "output_dir": str(out_dir),
        "source": "direct",
        "company": "Example Co",
        "role_title": "principal-pm",
        "failure_type": None,
    }

    calls: list[list[str]] = []

    class _DummyThread:
        def __init__(self, *args, **kwargs):
            pass
        def start(self):
            pass
        def join(self, timeout=None):
            pass

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        (submit_dir / "application_submission_result.json").write_text(
            json.dumps(
                {
                    "status": "skipped_captcha",
                    "board": "avature",
                    "message": "Submission skipped: captcha required. Moving on to next job.",
                }
            ),
            encoding="utf-8",
        )
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = ""
        completed.stderr = ""
        return completed

    with (
        patch("job_db.get_job", return_value=job),
        patch("job_db.ensure_job_metrics"),
        patch("job_db.start_phase", return_value=1),
        patch("job_db.end_phase"),
        patch("job_db.log_event"),
        patch("job_db.update_job_metrics"),
        patch("job_db.update_progress"),
        patch("job_db.get_job_metrics", return_value=None),
        patch("job_db.get_recent_auth_failures", return_value=0),
        patch("job_db.update_status"),
        patch.object(pipeline_orchestrator, "_detect_and_log_content_edits"),
        patch.object(pipeline_orchestrator, "_finalize_pending_answer_refresh", return_value=None),
        patch.object(pipeline_orchestrator.threading, "Thread", _DummyThread),
        patch.object(pipeline_orchestrator.subprocess, "run", side_effect=_fake_run),
    ):
        status = pipeline_orchestrator.process_job(conn, 1, auto_submit=True)

    assert status == "stopped"
    assert len(calls) == 1
```

- [ ] **Step 2: Run the focused orchestrator tests to verify they fail**

Run: `uv run python -m pytest tests/test_pipeline_orchestrator.py -k "captcha and process_job" -v`
Expected: FAIL because `process_job()` currently treats `skipped_captcha` as a terminal stop and never relaunches a headed retry.

- [ ] **Step 3: Commit the failing-test checkpoint only after the implementation passes**

```bash
# Do not commit red tests. This step stays blocked until Task 2 passes.
```

### Task 2: Refactor Phase 3 Submit Launch And Add One Headed Retry

**Files:**
- Modify: `scripts/pipeline_orchestrator.py`
- Test: `tests/test_pipeline_orchestrator.py`

- [ ] **Step 1: Add a small helper that launches one submit attempt and returns the current-attempt context**

```python
def _run_submit_attempt(
    *,
    conn: sqlite3.Connection,
    job_id: int,
    output_dir: str | None,
    submit_target: str,
    auto_submit: bool,
    headless: bool,
    worker_id: int,
) -> tuple[subprocess.CompletedProcess[str], int, Path | None]:
    submit_script = str(SCRIPT_DIR / "submit_application.py")
    submit_cmd = _uv_python_cmd(submit_script, submit_target, "--submit" if auto_submit else "--draft")
    if headless:
        submit_cmd.append("--headless")

    env = os.environ.copy()
    if worker_id > 0:
        from browser_runtime import submit_browser_profile_dir

        profile = submit_browser_profile_dir(worker_id=worker_id)
        env["JOB_ASSETS_SUBMIT_BROWSER_PROFILE_DIR"] = str(profile)

    _db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    _signal_stop = threading.Event()
    _signal_thread = threading.Thread(
        target=_poll_captcha_signal,
        args=(_db_path, job_id, output_dir, _signal_stop),
        daemon=True,
    )
    _signal_thread.start()
    try:
        timeout = DEFAULT_SUBMIT_TIMEOUT if headless else (_CAPTCHA_TIMEOUT + 300)
        t0 = time.monotonic()
        completed = subprocess.run(
            submit_cmd,
            cwd=PROJECT_ROOT,
            env=env,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    finally:
        _signal_stop.set()
        _signal_thread.join(timeout=2)

    duration_ms = int((time.monotonic() - t0) * 1000)
    return completed, duration_ms, _active_submit_dir_for_output(output_dir)
```

- [ ] **Step 2: Add one helper that recognizes a captcha-interrupted draft attempt from either the raw exit code or the current-attempt submission result**

```python
def _draft_captcha_retry_needed(
    *,
    auto_submit: bool,
    headless: bool,
    captcha_retry_used: bool,
    submit_rc: int,
    submission_result: dict | None,
) -> bool:
    if auto_submit or not headless or captcha_retry_used:
        return False
    if submit_rc == CAPTCHA_SKIP_EXIT_CODE:
        return True
    if not isinstance(submission_result, dict):
        return False
    return str(submission_result.get("status") or "").strip().casefold() == "skipped_captcha"
```

- [ ] **Step 3: Replace the single submit subprocess block in `process_job()` with a two-attempt loop**

```python
requested_headless = headless if headless is not None else (not auto_submit)
attempt_headless = requested_headless
captcha_retry_used = False

while True:
    submit_result, duration_ms, _current_submit_dir = _run_submit_attempt(
        conn=conn,
        job_id=job_id,
        output_dir=output_dir,
        submit_target=submit_target,
        auto_submit=auto_submit,
        headless=attempt_headless,
        worker_id=worker_id,
    )
    submit_rc = submit_result.returncode
    current_submission_result = _load_application_submission_result(_current_submit_dir)

    log_event(
        conn,
        job_id,
        "submit_attempt" if auto_submit else "draft_attempt",
        detail_json={
            "exit_code": submit_rc,
            "duration_ms": duration_ms,
            "headless": attempt_headless,
            "captcha_retry_used": captcha_retry_used,
        },
        initiator="worker",
    )

    if _draft_captcha_retry_needed(
        auto_submit=auto_submit,
        headless=attempt_headless,
        captcha_retry_used=captcha_retry_used,
        submit_rc=submit_rc,
        submission_result=current_submission_result,
    ):
        captcha_retry_used = True
        attempt_headless = False
        update_progress(conn, job_id, "Captcha detected — relaunching visible browser for manual solve...")
        log_event(conn, job_id, "captcha_retry_headed", initiator="worker")
        continue

    break
```

- [ ] **Step 4: Keep the existing final-stop behavior for the second captcha miss**

```python
if submit_rc == CAPTCHA_SKIP_EXIT_CODE:
    ...
    return "stopped"

_draft_result_status = handle_draft_mode_submission_result(
    conn,
    job_id,
    _load_application_submission_result(_check_sd),
)
```

Implementation note: do not special-case `skipped_captcha` inside `submission_result_outcomes.py`. The orchestrator should consume the first headless `skipped_captcha` as a retry signal and let the existing terminal mapping stand for the second headed miss.

- [ ] **Step 5: Run the focused tests again**

Run: `uv run python -m pytest tests/test_pipeline_orchestrator.py -k "captcha and process_job" -v`
Expected: PASS

- [ ] **Step 6: Commit the orchestrator change**

```bash
git add scripts/pipeline_orchestrator.py tests/test_pipeline_orchestrator.py
git commit -m "fix(draft): relaunch headed browser after captcha in headless draft mode"
```

### Task 3: Document The New Runtime Behavior And Run Verification

**Files:**
- Modify: `docs/worker-pipeline-patterns.md`
- Test: `tests/test_pipeline_orchestrator.py`

- [ ] **Step 1: Add a short runtime note to the worker pipeline docs**

```markdown
## Draft-Mode Captcha Escalation

Draft attempts still start headless by default. If the current attempt hits captcha, the orchestrator reruns that submit phase once in headed mode, exposes the job as `awaiting_captcha`, and keeps the browser open for manual solve. If the headed retry still cannot clear the captcha, the job stops with the existing captcha failure result.
```

- [ ] **Step 2: Run targeted verification for the touched files**

Run: `uv run python -m pytest tests/test_pipeline_orchestrator.py -v`
Expected: PASS

Run: `uv run ruff check scripts/pipeline_orchestrator.py tests/test_pipeline_orchestrator.py`
Expected: PASS

- [ ] **Step 3: Run the repo verification commands before closing the work**

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

- [ ] **Step 4: Commit the doc update if it was not bundled with Task 2**

```bash
git add docs/worker-pipeline-patterns.md
git commit -m "docs: document draft captcha browser escalation"
```
