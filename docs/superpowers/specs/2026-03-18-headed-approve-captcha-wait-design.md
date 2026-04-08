# Headed Approve with Captcha Wait — Design Spec

## Problem

Approve runs (draft → submit) use headless browsers, which trigger anti-bot captcha detection on boards like Ashby. When captcha appears, the pipeline skips the job immediately — no way to intervene.

## Solution

Run all submit runs in a **headed (visible) browser** by default. If captcha is detected, keep the browser open and wait for the user to solve it manually. After the user solves the captcha and the page confirms, run the same downstream actions (Notion sync, email reply) automatically.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Headed vs headless | Submit → headed (default), Draft → headless (default) | Headed prevents most captchas; headless fine for drafts since no submit happens |
| Explicit override | `--headless` / `--no-headless` flags preserved | Edge cases like remote servers with no display |
| Worker behavior during captcha | Worker blocks (with timeout) | Simple; captcha situations are rare, throughput impact minimal |
| Captcha completion detection | Existing confirmation polling (page state + email) | No extra user action needed — pipeline already has this |
| Notification | macOS notification + web UI badge | No auto-focus; user decides when to switch |
| Browser identification | Set page title to `[Captcha] {Company} — {Role}` | Enables AppleScript focus-by-title from web UI |
| Signal file for status | `submit/awaiting_captcha.json` | Keeps pipeline DB-free; orchestrator polls for it |
| Approve routing | Web UI approve routes through worker (not raw subprocess) | Consistent captcha wait + status tracking + downstream actions |

## Detailed Design

### 1. Shared Captcha Wait Function — `autofill_common.py`

New **default implementation** of `wait_for_captcha_fn` hook:

```python
def wait_for_captcha_resolution(
    page,
    *,
    headless: bool,
    payload: dict,
    board_title: str,
    classify_state_fn: Callable,
    page_snapshot_fn: Callable,
    email_watcher,
    confirmed_outcome_from_email_fn: Callable | None,
    capture_fn: Callable,
    submit_started_at_utc: str,
) -> dict:
    """Wait for user to solve captcha in headed browser.

    This is the default implementation for the `wait_for_captcha_fn` parameter
    in `run_browser_pipeline()`. Boards can override with their own implementation.

    Returns:
        {"status": "confirmed", "outcome": ...} — user solved captcha, page confirmed
        {"status": "timeout"} — timeout expired
        {"status": "skipped"} — headless mode, cannot wait
    """
```

Behavior:
- If `headless=True`: return `{"status": "skipped"}` immediately
- Write `submit/awaiting_captcha.json` signal file with `{"company": ..., "role": ..., "timestamp": ..., "timeout_seconds": ...}`
- Set page title: `page.evaluate('document.title = "[Captcha] {company} — {role}"')`
- Send macOS notification via `osascript` (with platform guard — silently no-op on non-macOS)
- Poll loop: every 3 seconds, call `classify_state_fn` + `email_watcher.poll()` for up to `JOB_ASSETS_CAPTCHA_TIMEOUT` seconds (default 3600, env-configurable)
- On confirmation: delete signal file, return `{"status": "confirmed", "outcome": ...}`. **Does NOT call Notion sync or email reply** — the caller handles downstream actions based on the return value, preventing double-execution.
- On timeout: save debug artifacts (HTML + screenshot), delete signal file, return `{"status": "timeout"}`

Helper for macOS notification:

```python
def _notify_captcha(company: str, role: str) -> None:
    """Send macOS notification for captcha waiting. No-op on non-macOS."""
    import platform, subprocess
    if platform.system() != "Darwin":
        return
    title = "Job Assets — Captcha Required"
    body = f"{company} — {role}"
    subprocess.Popen([
        "osascript", "-e",
        f'display notification "{body}" with title "{title}"',
    ])
```

### 2. Pipeline Integration — `autofill_pipeline.py`

`run_browser_pipeline()` already declares a `wait_for_captcha_fn: Callable | None = None` parameter that is **never called** in the function body. Three boards (Ashby, Lever, Gem) already pass implementations for this hook.

**Change:** Wire up the existing hook. At the two captcha exit points (Phase 7 line 278 break, Phase 8 lines 310-315), call `wait_for_captcha_fn` (or the default `wait_for_captcha_resolution` if no board override is provided).

**Phase 7 captcha break (line 278):** Instead of `break`, call the captcha wait function. If confirmed → run Notion sync + email reply (same as normal confirm path) → return 0. If timeout/skipped → fall through to existing `CAPTCHA_SKIP_EXIT_CODE` return.

**Phase 8 captcha check (lines 310-315):** Same — call captcha wait function instead of immediate return.

The `headless` parameter is already available in `run_browser_pipeline`'s scope. The downstream actions (Notion sync, email reply) are called by `run_browser_pipeline` itself on confirmed result — the wait function just reports status.

### 3. Board-Specific Scripts

**Captcha-only sites (not auth failures):**

`autofill_phenom.py`, `autofill_workday.py`, `autofill_icims.py` each have `CAPTCHA_SKIP_EXIT_CODE` returns. These must be categorized:

| Board | Location | Type | Action |
|-------|----------|------|--------|
| `autofill_phenom.py:2206` | Captcha detected after submit | Captcha | Replace with `wait_for_captcha_resolution()` |
| `autofill_workday.py:1520` | Captcha detected after submit | Captcha | Replace with `wait_for_captcha_resolution()` |
| `autofill_workday.py:1421` | Auth failure (wrong password/locked) | Auth failure | **Leave as `CAPTCHA_SKIP_EXIT_CODE`** — not a solvable captcha |
| `autofill_icims.py:1278` | Captcha detected after submit | Captcha | Replace with `wait_for_captcha_resolution()` |
| `autofill_icims.py:1161` | Auth failure | Auth failure | **Leave as `CAPTCHA_SKIP_EXIT_CODE`** — not a solvable captcha |

At captcha-only sites, replace with:
```python
result = wait_for_captcha_resolution(page, headless=headless, ...)
if result["status"] == "confirmed":
    return 0
return CAPTCHA_SKIP_EXIT_CODE
```

Each board script already has access to `headless` and `page`. The additional parameters (`classify_state_fn`, `email_watcher`, etc.) need to be threaded through — some boards may need minor refactoring to make these available at the captcha detection point.

### 4. Orchestrator — `pipeline_orchestrator.py`

**Headless default flip:** The override point is in `process_job()` where the submit command is built (line 646-652). New logic:
- If `auto_submit=True` and caller did not explicitly pass `headless=True` → default to `headless=False` (headed)
- If `auto_submit=False` (draft) → default to `headless=True` (headless)
- Explicit `--headless` / `--no-headless` CLI flags always override

**Subprocess timeout:** Currently `DEFAULT_SUBMIT_TIMEOUT = 900` (15 min). When `headless=False`, increase subprocess timeout to `JOB_ASSETS_CAPTCHA_TIMEOUT + 300` (captcha timeout + 5 min buffer) to prevent the orchestrator from killing the captcha-waiting subprocess.

**Signal file polling:** While the `submit_application.py` subprocess is running, poll for `submit/awaiting_captcha.json` every 5 seconds in a background thread. When detected:
- `update_status(conn, job_id, "awaiting_captcha")`
- `log_event(conn, job_id, "awaiting_captcha", initiator="worker")`

When subprocess finishes, the background thread stops. Status is updated based on exit code as before. If the signal file was written and deleted quickly (fast captcha solve), the DB may never see `awaiting_captcha` — this is acceptable.

### 5. Worker — `job_worker.py`

- Default worker count: 20 → 40
- Worker blocks during captcha wait (subprocess hasn't returned yet)
- `awaiting_captcha` included in worker state broadcasts

### 6. Web UI — `draft_web.py`

**Approve routing change:** Remove the raw `subprocess.run` call in the approve endpoint. Instead, just call `approve_job()` to set status to `submitting` and let the worker pick it up. The worker handles headed browser, captcha wait, and downstream actions.

```python
@app.post("/api/drafts/{job_id}/approve")
def approve_draft(job_id: int):
    from pipeline_orchestrator import approve_job
    conn = _open_db()
    try:
        if not approve_job(conn, job_id):
            raise HTTPException(409, "Job is not in draft status")
    finally:
        conn.close()
    return {"status": "approved", "job_id": job_id}
```

**New endpoint:**

```python
@app.post("/api/jobs/{job_id}/focus-browser")
def focus_browser(job_id: int):
    """Bring the captcha browser window to foreground via AppleScript."""
    conn = _open_db()
    try:
        row = conn.execute(
            "SELECT company, role_title FROM jobs WHERE id = ? AND status = 'awaiting_captcha'",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Job not in awaiting_captcha status")

    title = f"[Captcha] {row['company']} — {row['role_title']}"
    # Bring Chromium to front and activate the window matching the captcha title
    subprocess.Popen([
        "osascript", "-e",
        f'''tell application "System Events"
            tell (first process whose name contains "Chrom")
                set frontmost to true
                try
                    perform action "AXRaise" of (first window whose name contains "[Captcha]")
                end try
            end tell
        end tell''',
    ])
    return {"status": "focused"}
```

**UI updates:**
- `awaiting_captcha` jobs show orange "Awaiting Captcha" badge
- "Focus Browser" button visible for `awaiting_captcha` jobs

### 7. TUI — `job_tui.py`

- `awaiting_captcha` status rendered with distinct color (orange/yellow) in queue table
- Job detail view shows the status

### 8. DB — `job_db.py`

- `awaiting_captcha` added as a valid status value (if status validation exists)
- No new columns needed

### 9. Env Var Naming

Lever's `autofill_lever.py` already reads `JOB_ASSETS_CAPTCHA_WAIT_SECONDS` for its per-board captcha wait. The new global captcha timeout uses `JOB_ASSETS_CAPTCHA_TIMEOUT` to distinguish scope: `CAPTCHA_WAIT_SECONDS` is board-specific polling interval, `CAPTCHA_TIMEOUT` is the max wall-clock time to keep the browser open.

## Configuration

| Env Var | Default | Purpose |
|---------|---------|---------|
| `JOB_ASSETS_CAPTCHA_TIMEOUT` | `3600` | Seconds to wait for captcha resolution before timing out |

## Status Lifecycle

```
draft → submitting (approve) → awaiting_captcha (if captcha detected)
                                    ↓                        ↓
                              user solves captcha      timeout expires
                                    ↓                        ↓
                              confirmed/submitted       stopped
```

## Files Changed

| File | Change |
|------|--------|
| `autofill_common.py` | New `wait_for_captcha_resolution()`, `_notify_captcha()` |
| `autofill_pipeline.py` | Replace captcha skip with `wait_for_captcha_resolution()` call |
| `autofill_phenom.py` | Replace `CAPTCHA_SKIP_EXIT_CODE` returns with shared wait function |
| `autofill_workday.py` | Same |
| `autofill_icims.py` | Same |
| `pipeline_orchestrator.py` | Signal file polling thread; submit defaults to headed; `awaiting_captcha` status; subprocess timeout increase for headed runs |
| `job_worker.py` | Default workers 20 → 40 |
| `draft_web.py` | Approve routes through worker (remove raw subprocess); `/api/jobs/{id}/focus-browser` endpoint; `awaiting_captcha` badge + "Focus Browser" button |
| `job_web.py` | `awaiting_captcha` orange badge in main job list table; consistent with TUI status colors |
| `job_tui.py` | `awaiting_captcha` status color |
| `job_db.py` | `awaiting_captcha` in valid statuses |
