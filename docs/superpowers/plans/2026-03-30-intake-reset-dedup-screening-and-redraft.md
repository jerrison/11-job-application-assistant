# Intake, Reset, JD Dedup, Screening Fixes, and Queue Redraft Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Jack & Jill saved-portal import, add reset-to-newly-added behavior, run JD-language dedup during add when JD text is already available, fix the remaining 2026-03-30 screening regressions across shared policy, and then redraft every non-submitted, non-archived job without regressing draft-mode safeguards.

**Architecture:** Keep the existing repo seams and extend them narrowly. Saved-portal dispatch should be registry-driven in one shared module, add-time JD dedup should stay inside `add_job()` so every caller benefits, reset-to-newly-added should live next to the current regenerate path, and screening fixes should prefer `question_classifier.py` plus `application_submit_common.py` rather than board-specific heuristics. Treat the queue-wide redraft as an operational phase after the code is correct, not as a substitute for code fixes.

**Tech Stack:** Python, FastAPI, Textual, SQLite, Playwright, vanilla JS/HTML, pytest, ruff

---

## File Map

| File | Responsibility |
| --- | --- |
| `scripts/saved_portal_import.py` | Shared saved-portal registry, importer loading, shared import runner |
| `scripts/import_jackandjill_saved.py` | Jack & Jill browser session, auth detection, scrape/resolve flow |
| `scripts/import_trueup_saved.py` | Reference implementation for saved-portal browser-backed imports |
| `scripts/import_linkedin_saved.py` | LinkedIn saved-jobs importer using the shared runner |
| `scripts/saved_portal_browser.py` | Shared persistent-browser session helper for saved portals |
| `scripts/url_resolver.py` | Source detection, including `jackandjill` |
| `bin/job-assets` | CLI add/import surface |
| `scripts/job_web.py` | Web API routes for imports, reset-to-new, restart/regenerate flows |
| `scripts/draft_web.py` | Draft-review API surface for regenerate/reset actions |
| `scripts/job_tui.py` | TUI add-screen saved portals and draft actions |
| `scripts/static/index.html` | Web UI containers for saved-portal actions |
| `scripts/static/app.js` | Saved-portal button wiring and reset-to-new action hooks |
| `scripts/job_db.py` | Canonical `add_job()`, JD dedup, queue selection, candidate promotion touchpoints |
| `scripts/job_discovery.py` | Candidate promotion path that can pass `job_description` into `add_job()` |
| `scripts/pipeline_orchestrator.py` | `regenerate_job()` neighbor for reset-to-newly-added |
| `scripts/autofill_common.py` | Current-attempt artifact cleanup helpers |
| `scripts/question_classifier.py` | Shared category routing for startup/sponsorship |
| `scripts/application_submit_common.py` | Shared policy resolution, work-auth truth path, generated-answer normalization |
| `scripts/autofill_greenhouse.py` | Primary consumer of shared multi-select generated answers |
| `scripts/autofill_linkedin.py` | LinkedIn closed-job detection and terminal result writing |
| `tests/test_saved_portal_import.py` | Shared saved-portal runner and registry coverage |
| `tests/test_import_jackandjill_saved.py` | New Jack & Jill importer coverage |
| `tests/test_url_resolver.py` | Source detection coverage |
| `tests/test_job_assets_cli.py` | CLI saved-portal argument/dispatch coverage |
| `tests/test_job_web.py` | Web saved-portal and reset-to-new API/UI coverage |
| `tests/test_job_tui.py` | TUI saved-portal and draft-action coverage |
| `tests/test_draft_web.py` | Draft web endpoint coverage |
| `tests/test_job_db.py` | `add_job()` JD dedup and reset/redraft DB behavior |
| `tests/test_application_submit_common.py` | Shared screening and generated-answer normalization helpers |
| `tests/test_question_classifier.py` | Shared classifier regressions |
| `tests/test_greenhouse_autofill.py` | Screening + multi-select regressions on a concrete board |
| `tests/test_autofill_linkedin.py` | LinkedIn closed-job result coverage |
| `tests/test_pipeline_orchestrator.py` | Reset helper and existing `job_closed` archive path verification |

Do not touch unrelated `output/**` churn while doing the code tasks. Treat rerun artifacts and DB changes as a later, separate data pass.

---

### Task 1: Add a Shared Saved-Portal Registry and Jack & Jill Importer

**Files:**
- Modify: `scripts/saved_portal_import.py`
- Create: `scripts/import_jackandjill_saved.py`
- Modify: `scripts/url_resolver.py`
- Modify: `tests/test_saved_portal_import.py`
- Create: `tests/test_import_jackandjill_saved.py`
- Modify: `tests/test_url_resolver.py`

- [ ] **Step 1: Write the failing tests**

Add a registry test to `tests/test_saved_portal_import.py` and a source-detection regression to `tests/test_url_resolver.py`:

```python
def test_saved_portal_registry_lists_supported_portals():
    specs = saved_portal_import.list_saved_portals()

    assert [spec.key for spec in specs] == ["linkedin", "trueup", "jackandjill"]
    assert saved_portal_import.get_saved_portal("jackandjill").label == "Jack & Jill"
    assert saved_portal_import.get_saved_portal("jackandjill").module_name == "import_jackandjill_saved"


def test_detect_jackandjill_source():
    assert (
        detect_source("https://app.jackandjill.ai/jack/dashboard/jobs/opportunities")
        == "jackandjill"
    )
```

Create `tests/test_import_jackandjill_saved.py` with importer-shape coverage matching the TrueUp tests:

```python
def test_import_saved_jobs_queues_resolved_external_urls_with_jackandjill_provenance(tmp_path):
    for conn in _open_db(tmp_path):
        fake_context = object()
        with (
            patch("import_jackandjill_saved._jackandjill_context", return_value=nullcontext(fake_context)),
            patch(
                "import_jackandjill_saved._scrape_saved_jobs",
                return_value=[
                    {
                        "source_url": "https://app.jackandjill.ai/jack/dashboard/jobs/opportunities/alpha",
                        "company": "Alpha",
                        "role_title": "Principal PM",
                        "jd_text": "Long job description " * 50,
                    }
                ],
            ),
            patch(
                "import_jackandjill_saved._resolve_saved_job",
                return_value={
                    "status": "resolved",
                    "url": "https://boards.greenhouse.io/alpha/jobs/1",
                    "source_url": "https://app.jackandjill.ai/jack/dashboard/jobs/opportunities/alpha",
                    "company": "Alpha",
                    "role_title": "Principal PM",
                    "jd_text": "Long job description " * 50,
                },
            ),
        ):
            result = import_saved_jobs(conn, priority=5, provider="openai")

        row = conn.execute(
            "SELECT url, source, source_url, priority, provider FROM jobs"
        ).fetchone()
        assert result["status"] == "ok"
        assert result["added"] == 1
        assert row["source"] == "jackandjill"
        assert row["source_url"].startswith("https://app.jackandjill.ai/")


def test_import_saved_jobs_returns_auth_required_when_jackandjill_session_is_invalid(tmp_path):
    for conn in _open_db(tmp_path):
        fake_context = object()
        with (
            patch("import_jackandjill_saved._jackandjill_context", return_value=nullcontext(fake_context)),
            patch(
                "import_jackandjill_saved._scrape_saved_jobs",
                side_effect=AuthRequiredError("Jack & Jill session expired"),
            ),
        ):
            result = import_saved_jobs(conn)

        assert result["status"] == "auth_required"
        assert result["message"] == "Jack & Jill session expired"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
uv run python -m pytest \
  tests/test_saved_portal_import.py \
  tests/test_import_jackandjill_saved.py \
  tests/test_url_resolver.py -v
```

Expected:

```text
FAILED tests/test_saved_portal_import.py::test_saved_portal_registry_lists_supported_portals
FAILED tests/test_import_jackandjill_saved.py::test_import_saved_jobs_queues_resolved_external_urls_with_jackandjill_provenance
FAILED tests/test_url_resolver.py::test_detect_jackandjill_source
```

- [ ] **Step 3: Write the minimal implementation**

In `scripts/saved_portal_import.py`, introduce the shared registry and loader:

```python
@dataclass(frozen=True, slots=True)
class SavedPortalSpec:
    key: str
    label: str
    module_name: str


_SAVED_PORTALS = {
    "linkedin": SavedPortalSpec("linkedin", "LinkedIn", "import_linkedin_saved"),
    "trueup": SavedPortalSpec("trueup", "TrueUp", "import_trueup_saved"),
    "jackandjill": SavedPortalSpec("jackandjill", "Jack & Jill", "import_jackandjill_saved"),
}


def list_saved_portals() -> list[SavedPortalSpec]:
    return list(_SAVED_PORTALS.values())


def get_saved_portal(portal: str) -> SavedPortalSpec:
    try:
        return _SAVED_PORTALS[portal]
    except KeyError as exc:
        raise ValueError(f"Unknown saved portal: {portal}") from exc


def load_saved_portal_module(portal: str):
    spec = get_saved_portal(portal)
    return __import__(spec.module_name, fromlist=["import_saved_jobs"])
```

Create `scripts/import_jackandjill_saved.py` mirroring the TrueUp module shape:

```python
#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict

from saved_portal_browser import saved_portal_browser_session
from saved_portal_import import AuthRequiredError, import_saved_portal_jobs

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
_JACKANDJILL_PROFILE_DIR = PROJECT_ROOT / ".playwright-jackandjill"
_JACKANDJILL_LOCK_FILE = PROJECT_ROOT / ".playwright-jackandjill.lock"
OPPORTUNITIES_URL = "https://app.jackandjill.ai/jack/dashboard/jobs/opportunities"


class SavedJackAndJillJob(TypedDict, total=False):
    source_url: str
    external_url: str
    company: str | None
    role_title: str | None
    jd_text: str | None


@contextmanager
def _jackandjill_context():
    with saved_portal_browser_session(
        profile_dir=_JACKANDJILL_PROFILE_DIR,
        lock_file=_JACKANDJILL_LOCK_FILE,
        headless=False,
        purpose="Jack & Jill opportunities import",
    ) as browser:
        yield browser


def import_saved_jobs(conn: sqlite3.Connection, *, priority: int = 0, provider: str | None = None) -> dict:
    with _jackandjill_context() as browser:
        page = browser.new_page()

        def scrape_jobs():
            return _scrape_saved_jobs(page)

        def resolve_job(candidate):
            return _resolve_saved_job(page, candidate)

        return import_saved_portal_jobs(
            conn,
            portal_name="jackandjill",
            scrape_jobs=scrape_jobs,
            resolve_job=resolve_job,
            priority=priority,
            provider=provider,
        )
```

Also update `scripts/url_resolver.py`:

```python
SOURCE_PATTERNS: dict[str, tuple[str, ...]] = {
    "linkedin": ("linkedin.com",),
    "indeed": ("indeed.com",),
    "glassdoor": ("glassdoor.com",),
    "ziprecruiter": ("ziprecruiter.com",),
    "dice": ("dice.com",),
    "trueup": ("trueup.io",),
    "jackandjill": ("jackandjill.ai",),
    "wellfound": ("wellfound.com",),
    "builtin": ("builtin.com",),
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
uv run python -m pytest \
  tests/test_saved_portal_import.py \
  tests/test_import_jackandjill_saved.py \
  tests/test_url_resolver.py -v
```

Expected:

```text
PASSED tests/test_saved_portal_import.py::test_saved_portal_registry_lists_supported_portals
PASSED tests/test_import_jackandjill_saved.py::test_import_saved_jobs_queues_resolved_external_urls_with_jackandjill_provenance
PASSED tests/test_url_resolver.py::test_detect_jackandjill_source
```

- [ ] **Step 5: Commit**

```bash
git add \
  scripts/saved_portal_import.py \
  scripts/import_jackandjill_saved.py \
  scripts/url_resolver.py \
  tests/test_saved_portal_import.py \
  tests/test_import_jackandjill_saved.py \
  tests/test_url_resolver.py
git commit -m "feat(import): add jackandjill saved portal"
```

---

### Task 2: Wire the Saved-Portal Registry Through CLI, Web, and TUI

**Files:**
- Modify: `bin/job-assets`
- Modify: `scripts/job_web.py`
- Modify: `scripts/job_tui.py`
- Modify: `scripts/static/index.html`
- Modify: `scripts/static/app.js`
- Modify: `tests/test_job_assets_cli.py`
- Modify: `tests/test_job_web.py`
- Modify: `tests/test_job_tui.py`

- [ ] **Step 1: Write the failing tests**

Update the CLI, web, and TUI tests so `jackandjill` is a first-class portal:

```python
def test_build_parser_accepts_add_saved_portal_mode(self):
    cli = load_cli_module()
    parser = cli.build_parser()

    args = parser.parse_args(["add", "--saved-portal", "jackandjill", "--priority", "5", "--provider", "codex"])

    self.assertEqual(args.saved_portal, "jackandjill")
```

```python
def test_root_includes_job_detail_dock_shell(client):
    resp = client.get("/")
    assert 'id="jackandjill-import-btn"' in resp.text
    assert 'id="add-jackandjill-import-btn"' in resp.text
```

```python
@pytest.mark.parametrize(
    ("portal", "button_id", "label", "module_name"),
    [
        ("linkedin", "#btn-import-linkedin", "LinkedIn", "import_linkedin_saved"),
        ("trueup", "#btn-import-trueup", "TrueUp", "import_trueup_saved"),
        ("jackandjill", "#btn-import-jackandjill", "Jack & Jill", "import_jackandjill_saved"),
    ],
)
async def test_add_jobs_screen_runs_saved_portal_import_with_selected_provider_priority(
    monkeypatch, portal, button_id, label, module_name
):
    app = _AddJobsHarness()
    started = threading.Event()
    release = threading.Event()
    captured = []

    module = types.ModuleType(module_name)

    def fake_import_saved_jobs(conn, *, priority, provider):
        captured.append((provider, priority))
        started.set()
        assert release.wait(1.0)
        return {
            "status": "ok",
            "message": f"{label} note",
            "scraped": 4,
            "resolved": 3,
            "added": 2,
            "duplicates": 1,
            "skipped_unresolved": 1,
            "errors": 0,
        }

    module.import_saved_jobs = fake_import_saved_jobs
    monkeypatch.setattr(job_tui, "_get_conn", lambda: _DummyConn())
    monkeypatch.setitem(sys.modules, module_name, module)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#provider-select", Select).value = "codex"
        screen.query_one("#priority-select", Select).value = "5"
        await pilot.click(button_id)
        await _wait_until(started.is_set)
        release.set()
        await _wait_until(lambda: not screen.query_one("#btn-import-linkedin", Button).disabled)

    assert captured == [("codex", 5)]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
uv run python -m pytest \
  tests/test_job_assets_cli.py \
  tests/test_job_web.py \
  tests/test_job_tui.py -k "saved_portal or jackandjill or import" -v
```

Expected:

```text
FAILED tests/test_job_assets_cli.py::JobAssetsCliTests::test_build_parser_accepts_add_saved_portal_mode
FAILED tests/test_job_web.py::test_root_includes_job_detail_dock_shell
FAILED tests/test_job_tui.py::test_add_jobs_screen_runs_saved_portal_import_with_selected_provider_priority
```

- [ ] **Step 3: Write the minimal implementation**

In `bin/job-assets`, stop hardcoding module names and labels:

```python
def _import_saved_portal(saved_portal: str, *, priority: int, provider: str | None) -> dict:
    _maybe_reexec_saved_portal_with_uv()
    sys.path.insert(0, str(SCRIPTS_ROOT))
    from saved_portal_import import get_saved_portal, load_saved_portal_module

    module = load_saved_portal_module(saved_portal)
    conn = _open_job_db()
    try:
        return module.import_saved_jobs(conn, priority=priority, provider=provider)
    finally:
        conn.close()


def _print_saved_portal_summary(saved_portal: str, result: dict) -> None:
    from saved_portal_import import get_saved_portal

    label = get_saved_portal(saved_portal).label
    status = result.get("status", "unknown")
    scraped = result.get("scraped", 0)
    resolved = result.get("resolved", 0)
    added = result.get("added", 0)
    duplicates = result.get("duplicates", 0)
    unresolved = result.get("skipped_unresolved", 0)
    errors = result.get("errors", 0)
    print(
        f"{label} import: status={status} scraped={scraped} resolved={resolved} "
        f"added={added} duplicates={duplicates} unresolved={unresolved} errors={errors}"
    )
```

In `scripts/job_web.py`, reuse the same registry:

```python
def _import_saved_portal_jobs(conn: sqlite3.Connection, *, portal: str, priority: int = 0, provider: str | None = None) -> dict:
    from saved_portal_import import load_saved_portal_module

    try:
        module = load_saved_portal_module(portal)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    return module.import_saved_jobs(conn, priority=priority, provider=provider)


def _run_saved_portal_import(saved_portal: str, req: SavedPortalImportRequest | None = None) -> dict:
    from saved_portal_import import get_saved_portal

    try:
        get_saved_portal(saved_portal)
    except ValueError:
        raise HTTPException(404, "Unknown saved portal")
    options = req or SavedPortalImportRequest()
    conn = get_conn()
    return _import_saved_portal_jobs(
        conn,
        portal=saved_portal,
        priority=options.priority,
        provider=options.provider,
    )
```

In `scripts/job_tui.py`, render/import from a small portal definition table sourced from the shared registry:

```python
def _saved_portal_specs():
    from saved_portal_import import list_saved_portals

    return [(spec.key, spec.label, f"btn-import-{spec.key}") for spec in list_saved_portals()]


with Horizontal(id="add-actions"):
    yield Button("Add Jobs", variant="primary", id="btn-add")
    for portal, label, button_id in _saved_portal_specs():
        suffix = "Saved" if portal == "linkedin" else "My Jobs" if portal == "trueup" else "Opportunities"
        yield Button(f"Import {label} {suffix}", variant="default", id=button_id)
```

In `scripts/static/index.html` and `scripts/static/app.js`, add the third queue/add buttons and keep the JS generic:

```html
<button class="btn btn-sm btn-outline" onclick="importSavedPortalFromQueue('jackandjill')" id="jackandjill-import-btn" title="Import saved jobs from Jack & Jill">Import Jack & Jill Opportunities</button>
<button class="btn btn-outline" onclick="importSavedPortal('linkedin')" id="add-linkedin-import-btn">Import LinkedIn Saved</button>
<button class="btn btn-outline" onclick="importSavedPortal('trueup')" id="add-trueup-import-btn">Import TrueUp My Jobs</button>
<button class="btn btn-outline" onclick="importSavedPortal('jackandjill')" id="add-jackandjill-import-btn">Import Jack & Jill Opportunities</button>
```

```javascript
const SAVED_PORTAL_LABELS = {
  linkedin: 'LinkedIn',
  trueup: 'TrueUp',
  jackandjill: 'Jack & Jill',
};
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
uv run python -m pytest \
  tests/test_job_assets_cli.py \
  tests/test_job_web.py \
  tests/test_job_tui.py -k "saved_portal or jackandjill or import" -v
```

Expected:

```text
PASSED tests/test_job_assets_cli.py::JobAssetsCliTests::test_build_parser_accepts_add_saved_portal_mode
PASSED tests/test_job_web.py::test_root_includes_job_detail_dock_shell
PASSED tests/test_job_tui.py::test_add_jobs_screen_runs_saved_portal_import_with_selected_provider_priority
```

- [ ] **Step 5: Commit**

```bash
git add \
  bin/job-assets \
  scripts/job_web.py \
  scripts/job_tui.py \
  scripts/static/index.html \
  scripts/static/app.js \
  tests/test_job_assets_cli.py \
  tests/test_job_web.py \
  tests/test_job_tui.py
git commit -m "feat(import): wire saved portal registry through surfaces"
```

---

### Task 3: Run JD-Language Dedup During Add When JD Text Is Already Available

**Files:**
- Modify: `scripts/job_db.py`
- Modify: `scripts/saved_portal_import.py`
- Modify: `scripts/job_discovery.py`
- Modify: `tests/test_job_db.py`
- Modify: `tests/test_saved_portal_import.py`

- [ ] **Step 1: Write the failing tests**

Add a DB-level regression for archived-row JD duplicates and a runner regression for `jd_text` passthrough:

```python
def test_add_job_jd_duplicate_matches_archived_rows(db):
    jd = "Long job description " * 50
    archived_id = add_job(
        db,
        url="https://boards.greenhouse.io/acme/jobs/700",
        company="Acme",
        role_title="PM",
        jd_text=jd,
    )
    db.execute("UPDATE jobs SET archived = TRUE WHERE id = ?", (archived_id,))
    db.commit()

    duplicate_id = add_job(
        db,
        url="https://boards.greenhouse.io/acme/jobs/701",
        company="Acme",
        role_title="Staff PM",
        jd_text=jd,
    )

    assert duplicate_id == -archived_id
```

```python
def test_import_saved_portal_jobs_passes_jd_text_to_add_job(monkeypatch):
    captured = []

    def fake_add_job(conn_, url, **kwargs):
        captured.append(kwargs["jd_text"])
        return 42

    monkeypatch.setattr(saved_portal_import, "add_job", fake_add_job)
    monkeypatch.setattr(saved_portal_import, "backfill_jd_fingerprints", lambda _: (0, 0))
    monkeypatch.setattr(saved_portal_import, "find_jd_duplicates", lambda _: [])

    result = saved_portal_import.import_saved_portal_jobs(
        object(),
        portal_name="jackandjill",
        scrape_jobs=lambda: [{"source_url": "https://app.jackandjill.ai/jobs/1"}],
        resolve_job=lambda _candidate: {
            "status": "resolved",
            "url": "https://boards.greenhouse.io/acme/jobs/1",
            "company": "Acme",
            "role_title": "PM",
            "jd_text": "Long job description " * 50,
        },
    )

    assert result["added"] == 1
    assert captured == ["Long job description " * 50]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
uv run python -m pytest \
  tests/test_job_db.py \
  tests/test_saved_portal_import.py -k "jd_duplicate or jd_text" -v
```

Expected:

```text
FAILED tests/test_job_db.py::test_add_job_jd_duplicate_matches_archived_rows
FAILED tests/test_saved_portal_import.py::test_import_saved_portal_jobs_passes_jd_text_to_add_job
```

- [ ] **Step 3: Write the minimal implementation**

In `scripts/job_db.py`, extend `add_job()` so JD duplicates are checked before insert and fingerprints are stored immediately:

```python
def add_job(
    conn: sqlite3.Connection,
    url: str,
    *,
    priority: int = 0,
    provider: str | None = None,
    company: str | None = None,
    role_title: str | None = None,
    source_override: str | None = None,
    source_url_override: str | None = None,
    jd_text: str | None = None,
) -> int:
    source = detect_source(url)
    if source == "direct":
        board_url = url
        source_url = None
        canonical_url = url
    elif source != "unknown":
        board_url = None
        source_url = url
        canonical_url = url
    elif _is_known_board_url(url):
        source = "direct"
        board_url = url
        source_url = None
        canonical_url = url
    else:
        board_url = None
        source_url = url
        canonical_url = url
    jd_fp = jd_fingerprint(company, jd_text) if company and jd_text else None
    if jd_fp:
        duplicate = check_jd_duplicate(conn, company, jd_text)
        if duplicate is not None:
            log.info("Duplicate of job #%d (same JD fingerprint), skipping", duplicate["id"])
            return -int(duplicate["id"])
    cur = conn.execute(
        """INSERT INTO jobs (url, source, source_url, board_url, canonical_url, priority, provider,
           company, role_title, jd_fingerprint, status, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', CURRENT_TIMESTAMP)""",
        (url, source, source_url, board_url, canonical_url, priority, provider, company, role_title, jd_fp),
    )
```

In `scripts/saved_portal_import.py`, pass `jd_text` through:

```python
existing_or_new_id = add_job(
    conn,
    resolved_url,
    priority=priority,
    provider=provider,
    company=resolved.get("company"),
    role_title=resolved.get("role_title"),
    source_override=portal_name,
    source_url_override=source_url,
    jd_text=resolved.get("jd_text"),
)
```

In `scripts/job_discovery.py`, pass the candidate description when available:

```python
job_id = add_job(
    conn,
    url,
    company=candidate.get("company"),
    role_title=candidate.get("title"),
    jd_text=candidate.get("job_description"),
)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
uv run python -m pytest \
  tests/test_job_db.py \
  tests/test_saved_portal_import.py -k "jd_duplicate or jd_text" -v
```

Expected:

```text
PASSED tests/test_job_db.py::test_add_job_jd_duplicate_matches_archived_rows
PASSED tests/test_saved_portal_import.py::test_import_saved_portal_jobs_passes_jd_text_to_add_job
```

- [ ] **Step 5: Commit**

```bash
git add \
  scripts/job_db.py \
  scripts/saved_portal_import.py \
  scripts/job_discovery.py \
  tests/test_job_db.py \
  tests/test_saved_portal_import.py
git commit -m "fix(dedup): run jd duplicate checks during add"
```

---

### Task 4: Add Reset-To-Newly-Added Behavior Across the Active Operator Surfaces

**Files:**
- Modify: `scripts/pipeline_orchestrator.py`
- Modify: `scripts/job_web.py`
- Modify: `scripts/draft_web.py`
- Modify: `scripts/job_tui.py`
- Modify: `scripts/static/app.js`
- Modify: `tests/test_pipeline_orchestrator.py`
- Modify: `tests/test_job_web.py`
- Modify: `tests/test_draft_web.py`
- Modify: `tests/test_job_tui.py`

- [ ] **Step 1: Write the failing tests**

Add one orchestration test, one web endpoint test, one draft-web test, and one TUI action test:

```python
def test_reset_job_to_new_clears_transient_artifacts_and_queues_job(tmp_path):
    from job_db import init_db, add_job, update_status
    import pipeline_orchestrator

    conn = init_db(tmp_path / "jobs.db")
    job_id = add_job(conn, "https://boards.greenhouse.io/acme/jobs/1")
    out_dir = tmp_path / "output"
    submit_dir = out_dir / "submit"
    submit_dir.mkdir(parents=True)
    (out_dir / ".asset_pipeline_state.json").write_text("{}", encoding="utf-8")
    (out_dir / "draft_status.json").write_text("{}", encoding="utf-8")
    (submit_dir / "application_submission_result.json").write_text("{}", encoding="utf-8")
    update_status(conn, job_id, "draft", output_dir=str(out_dir), board="greenhouse")

    assert pipeline_orchestrator.reset_job_to_new(conn, job_id, initiator="test") is True
    row = conn.execute("SELECT status, error_message, progress FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "queued"
    assert not (out_dir / ".asset_pipeline_state.json").exists()
    assert not (submit_dir / "application_submission_result.json").exists()
```

```python
def test_reset_to_new_endpoint_requeues_draft_job(client, tmp_path):
    from job_db import update_status

    client.post("/api/jobs", json={"urls": ["https://boards.greenhouse.io/co/jobs/reset-to-new"]})
    out_dir = tmp_path / "job-output"
    out_dir.mkdir()
    import job_web

    conn = job_web.get_conn()
    update_status(conn, 1, "draft", output_dir=str(out_dir), board="greenhouse")
    resp = client.post("/api/jobs/1/reset-to-new")
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
```

```python
def test_regenerate_draft_page_exposes_reset_to_new_control(self):
    client = TestClient(create_app())
    resp = client.get("/drafts/1")
    self.assertIn("/api/drafts/1/reset", resp.text)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
uv run python -m pytest \
  tests/test_pipeline_orchestrator.py \
  tests/test_job_web.py \
  tests/test_draft_web.py \
  tests/test_job_tui.py -k "reset_to_new or reset-to-new or reset" -v
```

Expected:

```text
FAILED tests/test_pipeline_orchestrator.py::test_reset_job_to_new_clears_transient_artifacts_and_queues_job
FAILED tests/test_job_web.py::test_reset_to_new_endpoint_requeues_draft_job
FAILED tests/test_draft_web.py::DraftWebTests::test_dashboard_html
```

- [ ] **Step 3: Write the minimal implementation**

Add the orchestration helper in `scripts/pipeline_orchestrator.py`:

```python
def reset_job_to_new(conn: sqlite3.Connection, job_id: int, *, initiator: str = "web") -> bool:
    from job_db import get_job, update_status
    from autofill_common import board_file_constants, clear_current_attempt_artifacts

    job = get_job(conn, job_id)
    if not job or job.get("archived"):
        return False
    enforce_submission_lock(conn, job_id, target_status="queued")

    out_dir = Path(job["output_dir"]) if job.get("output_dir") else None
    if out_dir is not None:
        payload = {"out_dir": str(out_dir), "artifacts": {}}
        board = str(job.get("board") or "application").strip() or "application"
        constants = board_file_constants(board)
        submit_dir = out_dir / "submit"
        for key, filename in constants.items():
            payload["artifacts"][key] = str(submit_dir / filename)
        clear_current_attempt_artifacts(payload)
        for artifact in (
            out_dir / ".asset_pipeline_state.json",
            out_dir / "answer_refresh_status.json",
            out_dir / "draft_status.json",
            out_dir / "draft_summary.md",
            out_dir / "draft_summary.original.md",
            out_dir / "draft_summary.png",
        ):
            artifact.unlink(missing_ok=True)
        mark_answer_refresh_pending(out_dir, request_kind="reset_to_new")

    update_status(
        conn,
        job_id,
        "queued",
        error_message="",
        progress="",
        clear_provider=True,
        retry_after=RETRY_AFTER_SENTINEL,
    )
    log_event(conn, job_id, "reset_to_new_requested", initiator=initiator)
    return True
```

Wire it through `scripts/job_web.py` and `scripts/draft_web.py`:

```python
@app.post("/api/jobs/{job_id}/reset-to-new")
def reset_to_new(job_id: int):
    conn = get_conn()
    try:
        if not reset_job_to_new(conn, job_id, initiator="web"):
            raise HTTPException(409, "Job cannot be reset to a newly added state")
    except SubmissionLockError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"status": "queued"}
```

```python
@app.post("/api/drafts/{job_id}/reset")
def reset_draft(job_id: int):
    conn = _open_db()
    try:
        if not reset_job_to_new(conn, job_id, initiator="draft_web"):
            raise HTTPException(409, "Job cannot be reset to a newly added state")
    finally:
        conn.close()
    return {"status": "queued", "job_id": job_id}
```

In `scripts/job_tui.py` and `scripts/static/app.js`, add a distinct reset action rather than overloading regenerate:

```python
yield Button("Reset to New", variant="warning", id="btn-draft-reset")
elif event.button.id == "btn-draft-regenerate":
    self._draft_regenerate()
elif event.button.id == "btn-draft-reset":
    self._draft_reset()
```

```javascript
async function resetJobToNew(jobId) {
  await apiCall('POST', `/api/jobs/${jobId}/reset-to-new`);
  showToast('Job reset to newly added state', 'info');
  await loadJobDetail(jobId);
  scheduleQueueRefresh();
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
uv run python -m pytest \
  tests/test_pipeline_orchestrator.py \
  tests/test_job_web.py \
  tests/test_draft_web.py \
  tests/test_job_tui.py -k "reset_to_new or reset-to-new or reset" -v
```

Expected:

```text
PASSED tests/test_pipeline_orchestrator.py::test_reset_job_to_new_clears_transient_artifacts_and_queues_job
PASSED tests/test_job_web.py::test_reset_to_new_endpoint_requeues_draft_job
PASSED tests/test_draft_web.py::DraftWebTests::test_dashboard_html
PASSED tests/test_job_tui.py::test_add_jobs_screen_runs_saved_portal_import_with_selected_provider_priority
```

- [ ] **Step 5: Commit**

```bash
git add \
  scripts/pipeline_orchestrator.py \
  scripts/job_web.py \
  scripts/draft_web.py \
  scripts/job_tui.py \
  scripts/static/app.js \
  tests/test_pipeline_orchestrator.py \
  tests/test_job_web.py \
  tests/test_draft_web.py \
  tests/test_job_tui.py
git commit -m "feat(queue): add reset-to-new workflow"
```

---

### Task 5: Route Startup and Sponsorship Through Shared Screening Policy

**Files:**
- Modify: `scripts/question_classifier.py`
- Modify: `scripts/application_submit_common.py`
- Modify: `tests/test_question_classifier.py`
- Modify: `tests/test_application_submit_common.py`
- Modify: `tests/test_greenhouse_autofill.py`
- Modify: `tests/test_autofill_linkedin.py`

- [ ] **Step 1: Write the failing tests**

Add startup and sponsorship regressions at the classifier and shared-policy layer:

```python
def test_have_you_worked_at_a_startup_is_startup_experience(self):
    result = self.classify_question("Have you worked at a startup?")
    self.assertEqual(result, "startup_experience")


def test_employment_based_immigration_status_prompt_is_work_authorization(self):
    result = self.classify_question(
        "Will you now or in the future require sponsorship for employment-based immigration status?"
    )
    self.assertEqual(result, "work_authorization")
```

Add shared-policy tests in `tests/test_application_submit_common.py`:

```python
def test_resolve_shared_question_policy_returns_yes_for_startup_experience(self):
    profile = self.mod.parse_application_profile(
        self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
    )
    policy = self.mod.resolve_shared_question_policy("Have you worked at a startup?", profile)
    self.assertEqual(policy.category, "startup_experience")
    self.assertEqual(policy.text_value, "Yes")


def test_build_truthful_work_authorization_answer_handles_employment_based_status_wording(self):
    profile = self.mod.parse_application_profile(
        self.mod.APPLICATION_PROFILE_PATH.read_text(encoding="utf-8")
    )
    answer = self.mod.build_truthful_work_authorization_answer(
        "Will you now or in the future require sponsorship for employment-based immigration status?",
        profile,
    )
    self.assertIn("No", answer)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
uv run python -m pytest \
  tests/test_question_classifier.py \
  tests/test_application_submit_common.py \
  tests/test_greenhouse_autofill.py \
  tests/test_autofill_linkedin.py -k "startup or sponsorship or work_authorization" -v
```

Expected:

```text
FAILED tests/test_question_classifier.py::QuestionClassifierTests::test_have_you_worked_at_a_startup_is_startup_experience
FAILED tests/test_application_submit_common.py::ApplicationSubmitCommonDocumentTests::test_resolve_shared_question_policy_returns_yes_for_startup_experience
FAILED tests/test_greenhouse_autofill.py::test_question_step_prefers_sponsorship_for_mixed_work_authorization_prompt
```

- [ ] **Step 3: Write the minimal implementation**

In `scripts/question_classifier.py`, add a startup category ahead of generic experience confirmation and broaden work-auth fragments:

```python
_WORK_AUTH_FRAGMENTS = (
    "authorized to work",
    "authorization to work",
    "work authorization",
    "require sponsorship",
    "visa sponsorship",
    "employment-based immigration status",
    "employment sponsorship",
    "immigration sponsorship",
    "immigration status",
    "work permit",
    "employment visa status",
)


def _question_is_startup_experience(label: str) -> bool:
    normalized = normalize_text(label)
    return bool(normalized and question_requests_startup_experience(normalized))
```

```python
if _question_is_startup_experience(label):
    return "startup_experience"

if question_is_experience_confirmation(label):
    return "experience_confirmation"
```

In `scripts/application_submit_common.py`, add the new positive-fit category and broaden the sponsorship truth path:

```python
POSITIVE_FIT_CATEGORIES = frozenset(
    {
        "minimum_experience",
        "experience_confirmation",
        "startup_experience",
        "product_usage",
        "office_attendance",
        "relocation_willingness",
        "travel_willingness",
        "location_residency",
    }
)
```

```python
if any(
    fragment in normalized
    for fragment in (
        "require sponsorship",
        "visa sponsorship",
        "employment sponsorship",
        "employment-based immigration status",
        "immigration sponsorship",
        "immigration status",
    )
):
    return application_profile.require_sponsorship_now or application_profile.require_sponsorship_future
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
uv run python -m pytest \
  tests/test_question_classifier.py \
  tests/test_application_submit_common.py \
  tests/test_greenhouse_autofill.py \
  tests/test_autofill_linkedin.py -k "startup or sponsorship or work_authorization" -v
```

Expected:

```text
PASSED tests/test_question_classifier.py::QuestionClassifierTests::test_have_you_worked_at_a_startup_is_startup_experience
PASSED tests/test_application_submit_common.py::ApplicationSubmitCommonDocumentTests::test_resolve_shared_question_policy_returns_yes_for_startup_experience
PASSED tests/test_greenhouse_autofill.py::test_question_step_prefers_sponsorship_for_mixed_work_authorization_prompt
PASSED tests/test_autofill_linkedin.py::LinkedInSelectAnswerTests::test_answer_for_select_details_answers_no_for_mixed_sponsorship_visa_prompt
```

- [ ] **Step 5: Commit**

```bash
git add \
  scripts/question_classifier.py \
  scripts/application_submit_common.py \
  tests/test_question_classifier.py \
  tests/test_application_submit_common.py \
  tests/test_greenhouse_autofill.py \
  tests/test_autofill_linkedin.py
git commit -m "fix(screening): route startup and sponsorship through shared policy"
```

---

### Task 6: Normalize Shared Multi-Select Answers to the New “Choose Three” Rule

**Files:**
- Modify: `scripts/application_submit_common.py`
- Modify: `scripts/autofill_greenhouse.py`
- Modify: `tests/test_application_submit_common.py`
- Modify: `tests/test_greenhouse_autofill.py`

- [ ] **Step 1: Write the failing tests**

Add one shared helper test and one board integration regression:

```python
def test_normalize_multi_select_answers_defaults_to_three_when_prompt_is_preference_like(self):
    specs = [
        {
            "field_name": "focus_areas",
            "label": "Which product areas are you most interested in?",
            "type": "multi_value_multi_select",
            "values": [
                {"label": "Growth"},
                {"label": "Platform"},
                {"label": "AI"},
                {"label": "Security"},
            ],
        }
    ]
    normalized = self.mod.normalize_multi_select_generated_answers(
        specs,
        {"focus_areas": ["Growth"]},
    )
    self.assertEqual(normalized["focus_areas"], ["Growth", "Platform", "AI"])
```

```python
def test_validate_generated_answers_tops_up_preference_multi_select_to_three(self):
    specs = [
        {
            "field_name": "question_focus_areas",
            "label": "Select all that apply: Which product areas are you most interested in?",
            "type": "multi_value_multi_select",
            "values": [
                {"label": "Growth"},
                {"label": "Platform"},
                {"label": "AI"},
                {"label": "Security"},
            ],
        }
    ]

    validated = greenhouse._validate_generated_answers(specs, {"question_focus_areas": ["Growth"]})

    assert validated["question_focus_areas"] == ["Growth", "Platform", "AI"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
uv run python -m pytest \
  tests/test_application_submit_common.py \
  tests/test_greenhouse_autofill.py -k "multi_select and three" -v
```

Expected:

```text
FAILED tests/test_application_submit_common.py::ApplicationSubmitCommonDocumentTests::test_normalize_multi_select_answers_defaults_to_three_when_prompt_is_preference_like
FAILED tests/test_greenhouse_autofill.py::test_validate_generated_answers_tops_up_preference_multi_select_to_three
```

- [ ] **Step 3: Write the minimal implementation**

In `scripts/application_submit_common.py`, add a shared normalization pass that runs before validation:

```python
def normalize_multi_select_generated_answers(
    question_specs: list[dict],
    answers: dict[str, object],
) -> dict[str, object]:
    normalized_answers = dict(answers)
    for spec in question_specs:
        if spec.get("type") != "multi_value_multi_select":
            continue
        field_name = str(spec.get("field_name") or "").strip()
        if not field_name or field_name not in normalized_answers:
            continue
        raw_value = normalized_answers[field_name]
        if isinstance(raw_value, str):
            selected = [item.strip() for item in raw_value.split(",") if item.strip()]
        elif isinstance(raw_value, list):
            selected = [str(item).strip() for item in raw_value if str(item).strip()]
        else:
            continue

        option_labels = [str(v.get("label") or "").strip() for v in spec.get("values", []) if str(v.get("label") or "").strip()]
        normalized_label = normalize_text(spec.get("label"))
        wants_three = (
            "top 3" in normalized_label
            or "choose 3" in normalized_label
            or "choose three" in normalized_label
            or "at least three" in normalized_label
            or "most interested in" in normalized_label
            or "all that apply" in normalized_label
        )
        target_count = min(3, len(option_labels)) if wants_three else None
        if target_count and len(selected) < target_count:
            for option in option_labels:
                if option not in selected:
                    selected.append(option)
                if len(selected) >= target_count:
                    break
        normalized_answers[field_name] = selected
    return normalized_answers
```

Then call it from `generate_application_answers()` and the Greenhouse local validator:

```python
merged_answers = normalize_multi_select_generated_answers(question_specs, merged_answers)
answers = normalize_multi_select_generated_answers(question_specs, answers)
```

```python
validated, blockers = validate_generated_answers_with_blockers(
    question_specs,
    normalize_multi_select_generated_answers(question_specs, answers),
    application_profile=application_profile,
)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
uv run python -m pytest \
  tests/test_application_submit_common.py \
  tests/test_greenhouse_autofill.py -k "multi_select and three" -v
```

Expected:

```text
PASSED tests/test_application_submit_common.py::ApplicationSubmitCommonDocumentTests::test_normalize_multi_select_answers_defaults_to_three_when_prompt_is_preference_like
PASSED tests/test_greenhouse_autofill.py::test_validate_generated_answers_tops_up_preference_multi_select_to_three
```

- [ ] **Step 5: Commit**

```bash
git add \
  scripts/application_submit_common.py \
  scripts/autofill_greenhouse.py \
  tests/test_application_submit_common.py \
  tests/test_greenhouse_autofill.py
git commit -m "fix(screening): normalize multi-select counts to shared rule"
```

---

### Task 7: Emit `job_closed` From LinkedIn When the Posting Is Visibly Closed

**Files:**
- Modify: `scripts/autofill_linkedin.py`
- Modify: `tests/test_autofill_linkedin.py`
- Modify: `tests/test_pipeline_orchestrator.py`

- [ ] **Step 1: Write the failing tests**

Add a result-writer regression plus an early-detection regression:

```python
def test_write_job_closed_result(self):
    mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        payload = {"job_url": "https://www.linkedin.com/jobs/view/789/"}
        screenshot_path = out_dir / "submit" / "linkedin_job_closed.png"
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_path.write_text("png", encoding="utf-8")

        mod._write_job_closed_result(out_dir, payload, screenshot_path=screenshot_path, reason="This job is no longer accepting applications.")

        result = json.loads((out_dir / "submit" / "application_submission_result.json").read_text())
        assert result["status"] == "job_closed"
        assert result["failure_type"] == "job_closed"
        assert result["artifacts"]["page_screenshot"] == str(screenshot_path)
```

```python
def test_linkedin_job_closed_reason_detects_no_longer_accepting_applications(self):
    mod = load_module("autofill_linkedin", "scripts/autofill_linkedin.py")

    class _ClosedJobPage:
        def inner_text(self, _selector):
            return "This job is no longer accepting applications on LinkedIn."

    assert mod._linkedin_job_closed_reason(_ClosedJobPage()) == "no longer accepting applications"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
uv run python -m pytest \
  tests/test_autofill_linkedin.py \
  tests/test_pipeline_orchestrator.py -k "job_closed or no_longer_accepting" -v
```

Expected:

```text
FAILED tests/test_autofill_linkedin.py::LinkedInNotEasyApplyResultTests::test_write_job_closed_result
FAILED tests/test_autofill_linkedin.py::LinkedInNotEasyApplyResultTests::test_linkedin_job_closed_reason_detects_no_longer_accepting_applications
```

- [ ] **Step 3: Write the minimal implementation**

In `scripts/autofill_linkedin.py`, add a closed-job detector and terminal result writer before the Easy Apply / external-apply branch:

```python
def _linkedin_job_closed_reason(page) -> str | None:
    body_text = str(page.inner_text("body") or "").lower()
    markers = (
        "no longer accepting applications",
        "job is no longer open",
        "this job is closed",
        "job posting is no longer available",
    )
    for marker in markers:
        if marker in body_text:
            return marker
    return None


def _write_job_closed_result(
    out_dir: Path,
    payload: dict,
    *,
    reason: str,
    screenshot_path: Path | None = None,
) -> None:
    result = {
        "status": "job_closed",
        "failure_type": "job_closed",
        "message": f"LinkedIn job closed: {reason}",
        "job_url": payload["job_url"],
        "website_confirmed": True,
    }
    if screenshot_path is not None and screenshot_path.exists():
        result["artifacts"] = {"page_screenshot": str(screenshot_path)}
    _submission_result_path(out_dir).write_text(json.dumps(result, indent=2))
```

```python
closed_reason = _linkedin_job_closed_reason(page)
if closed_reason is not None:
    screenshot_path = _capture_not_easy_apply_screenshot(page, out_dir, reason="no_apply_button")
    _write_job_closed_result(out_dir, payload, reason=closed_reason, screenshot_path=screenshot_path)
    return 0
```

No pipeline archive logic change is needed beyond verifying the existing `job_closed` path still passes.

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
uv run python -m pytest \
  tests/test_autofill_linkedin.py \
  tests/test_pipeline_orchestrator.py -k "job_closed or no_longer_accepting" -v
```

Expected:

```text
PASSED tests/test_autofill_linkedin.py::LinkedInNotEasyApplyResultTests::test_write_job_closed_result
PASSED tests/test_pipeline_orchestrator.py::test_mark_job_unavailable_and_archive_sets_archived_and_logs_event
```

- [ ] **Step 5: Commit**

```bash
git add \
  scripts/autofill_linkedin.py \
  tests/test_autofill_linkedin.py \
  tests/test_pipeline_orchestrator.py
git commit -m "fix(linkedin): emit job_closed for closed postings"
```

---

### Task 8: Run Full Verification, Then Redraft Every Non-Submitted, Non-Archived Job

**Files:**
- No code changes required before starting this task if Tasks 1-7 are green
- Operational outputs: `jobs.db`, `jobs.db.backup`, `jobs.db.pre-migration`, `output/**`

- [ ] **Step 1: Run the repo verification suite before the live rerun**

Run:

```bash
uv run python -m pytest tests/ -v
uv run ruff check scripts/ tests/
uv run python scripts/check_architecture.py
uv run python scripts/sync_agent_files.py --check
uv run python scripts/check_agent_docs.py
```

Expected:

```text
all targeted tests pass
All checks passed
```

- [ ] **Step 2: Snapshot the target rerun scope and keep the list**

Run:

```bash
uv run python - <<'PY'
from pathlib import Path
import sqlite3

conn = sqlite3.connect("jobs.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    """
    SELECT id, company, role_title, status
    FROM jobs
    WHERE COALESCE(archived, 0) = 0
      AND status <> 'submitted'
    ORDER BY updated_at DESC
    """
).fetchall()
print(f"target_count={len(rows)}")
for row in rows[:25]:
    print(f"{row['id']}\t{row['status']}\t{row['company']}\t{row['role_title']}")
conn.close()
PY
```

Expected:

```text
target_count=<N>
412	draft	Acme	Principal Product Manager
398	stopped	Beta	Staff Product Manager
```

- [ ] **Step 3: Queue the rerun using the new reset-to-new helper**

Run:

```bash
uv run python - <<'PY'
from pathlib import Path
from job_db import init_db
from pipeline_orchestrator import reset_job_to_new

conn = init_db(Path("jobs.db"))
rows = conn.execute(
    """
    SELECT id
    FROM jobs
    WHERE COALESCE(archived, 0) = 0
      AND status <> 'submitted'
    ORDER BY updated_at DESC
    """
).fetchall()

queued = 0
skipped = 0
for row in rows:
    if reset_job_to_new(conn, int(row["id"]), initiator="batch_redraft"):
        queued += 1
    else:
        skipped += 1

print(f"queued={queued}")
print(f"skipped={skipped}")
conn.close()
PY
```

Expected:

```text
queued=<N>
skipped=<0 or known manual blockers>
```

- [ ] **Step 4: Start workers and watch for deterministic failures**

Run:

```bash
uv run python bin/job-assets worker --start --workers 10 --headless
uv run python bin/job-assets worker --status
```

Expected:

```text
Worker started
running
```

If a deterministic code bug appears:

```bash
uv run python -m pytest <targeted failing tests> -v
```

Fix the bug under the relevant task above, rerun the targeted tests, then restart the workers. Do not hand-edit unrelated output churn.

- [ ] **Step 5: Stop workers, inspect the final state, and decide the data commit boundary**

Run:

```bash
uv run python bin/job-assets worker --stop
uv run python - <<'PY'
import sqlite3

conn = sqlite3.connect("jobs.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    """
    SELECT status, COUNT(*) AS count
    FROM jobs
    WHERE COALESCE(archived, 0) = 0
    GROUP BY status
    ORDER BY count DESC, status
    """
).fetchall()
for row in rows:
    print(f"{row['status']}: {row['count']}")
conn.close()
PY
git status --short
```

Expected:

```text
draft: 24
stopped: 9
submitted: 181
```

Keep the final rerun artifacts in a separate data-only commit from the code commits above. Preserve the DB trio and authoritative proof artifacts; do not stage or revert suspicious duplicate output roots without inspecting them first.

---

## Plan Self-Review

**Spec coverage:**
- Jack & Jill import and shared saved-portal registry: Tasks 1-2
- reset-to-newly-added: Task 4
- add-time JD dedup against all existing jobs: Task 3
- startup/sponsorship shared policy: Task 5
- multi-select “choose three” handling: Task 6
- LinkedIn closed-job auto-archive path: Task 7
- full non-submitted, non-archived redraft: Task 8

**Placeholder scan:**
- No `TBD` / `TODO`
- Every code-changing task includes file paths, concrete code snippets, commands, and commit messages

**Type / naming consistency:**
- Shared reset helper name is `reset_job_to_new`
- Saved-portal registry accessors are `list_saved_portals()` / `get_saved_portal()`
- New portal key is consistently `jackandjill`
