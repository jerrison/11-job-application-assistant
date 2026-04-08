import sys
import threading
from pathlib import Path

import anyio
import pytest

pytest.importorskip("textual", reason="textual not installed")

from textual.app import App
from textual.widgets import Button, Select, Static, TextArea

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import job_tui
from job_tui import AddJobsScreen, JobDetailScreen, _format_saved_portal_import_feedback


class _DummyConn:
    def close(self) -> None:
        return None


def _feedback_text(widget: Static) -> str:
    renderable = widget.render()
    return renderable.plain if hasattr(renderable, "plain") else str(renderable)


async def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    deadline = anyio.current_time() + timeout
    while anyio.current_time() < deadline:
        if predicate():
            return
        await anyio.sleep(0.01)
    raise AssertionError("condition not met before timeout")


class _AddJobsHarness(App):
    def on_mount(self) -> None:
        self.install_screen(AddJobsScreen(), "add")
        self.push_screen("add")


class _DetailHarness(App):
    def on_mount(self) -> None:
        self.install_screen(JobDetailScreen(1), "detail")
        self.push_screen("detail")


@pytest.mark.anyio
async def test_add_jobs_screen_renders_saved_portal_buttons():
    app = _AddJobsHarness()

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen

        assert isinstance(screen, AddJobsScreen)
        suffixes = {
            "linkedin": "Saved",
            "trueup": "My Jobs",
            "jackandjill": "Opportunities",
        }
        for spec in job_tui.SAVED_PORTAL_SPECS:
            button = screen.query_one(f"#btn-import-{spec.key}", Button)
            expected = f"Import {spec.label} {suffixes.get(spec.key, '')}".strip()
            assert button.label == expected


@pytest.mark.anyio
async def test_add_jobs_screen_reads_selected_provider_and_priority():
    app = _AddJobsHarness()

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen

        screen.query_one("#provider-select", Select).value = "codex"
        screen.query_one("#priority-select", Select).value = "5"

        assert screen._selected_provider_priority() == ("codex", 5)


@pytest.mark.anyio
async def test_add_jobs_screen_treats_negative_add_job_results_as_duplicates(monkeypatch):
    app = _AddJobsHarness()
    job_ids = iter([101, -202])

    monkeypatch.setattr(job_tui, "_get_conn", lambda: _DummyConn())
    monkeypatch.setattr(job_tui, "add_job", lambda *args, **kwargs: next(job_ids))

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        url_input = screen.query_one("#url-input", TextArea)
        url_input.text = "https://example.com/jobs/1\nhttps://example.com/jobs/2"

        await pilot.click("#btn-add")
        await pilot.pause()

        feedback = _feedback_text(screen.query_one("#add-feedback", Static))
        assert "1 job(s) added" in feedback
        assert "1 duplicate(s) skipped" in feedback
        assert screen.query_one("#url-input", TextArea).text == ""


def test_format_saved_portal_import_feedback_includes_unresolved_and_auth_message():
    message = _format_saved_portal_import_feedback(
        "TrueUp",
        {
            "status": "auth_required",
            "message": "TrueUp session expired",
            "scraped": 6,
            "resolved": 4,
            "added": 2,
            "duplicates": 1,
            "skipped_unresolved": 3,
            "errors": 0,
        },
    )

    assert message.startswith("[yellow]TrueUp: 2 added, 1 duplicates, 3 unresolved")
    assert "(6 scraped, 4 resolved, status=auth_required)" in message
    assert "; TrueUp session expired" in message


def test_format_saved_portal_import_feedback_escapes_markup_and_warns_on_partial_ok():
    message = _format_saved_portal_import_feedback(
        "TrueUp",
        {
            "status": "ok",
            "message": "Needs [manual] review",
            "scraped": 4,
            "resolved": 3,
            "added": 2,
            "duplicates": 0,
            "skipped_unresolved": 1,
            "errors": 0,
        },
    )

    assert message.startswith("[yellow]TrueUp: 2 added, 1 unresolved")
    assert "; Needs \\[manual] review" in message


def test_start_workers_launches_worker_subprocess_with_devnull_stdin(monkeypatch):
    import types

    popen_calls: list[dict] = []

    def _fake_popen(cmd, **kwargs):
        popen_calls.append({"cmd": list(cmd), "kwargs": dict(kwargs)})
        return types.SimpleNamespace(pid=4242, poll=lambda: None)

    monkeypatch.setattr(job_tui.JobApp, "notify", lambda self, message: None)
    monkeypatch.setattr(job_tui.JobApp, "_is_worker_running", lambda self: False)
    monkeypatch.setattr(job_tui.subprocess, "Popen", _fake_popen)

    app = job_tui.JobApp()
    app._start_workers()

    assert len(popen_calls) == 1
    assert popen_calls[0]["kwargs"]["stdin"] is job_tui.subprocess.DEVNULL


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("portal", "button_id", "label"),
    [
        ("linkedin", "#btn-import-linkedin", "LinkedIn"),
        ("trueup", "#btn-import-trueup", "TrueUp"),
        ("jackandjill", "#btn-import-jackandjill", "Jack & Jill"),
    ],
)
async def test_add_jobs_screen_runs_saved_portal_import_with_selected_provider_priority(
    monkeypatch, portal, button_id, label
):
    app = _AddJobsHarness()
    started = threading.Event()
    release = threading.Event()
    captured: list[tuple[str | None, int]] = []

    module = type("FakeSavedPortalModule", (), {})()

    def fake_import_saved_jobs(conn, *, priority, provider):
        captured.append((provider, priority))
        started.set()
        assert release.wait(1.0)
        return {
            "status": "ok",
            "message": f"{label} [manual] note",
            "scraped": 5,
            "resolved": 4,
            "added": 2,
            "duplicates": 1,
            "skipped_unresolved": 1,
            "errors": 0,
        }

    module.import_saved_jobs = fake_import_saved_jobs

    monkeypatch.setattr(job_tui, "_get_conn", lambda: _DummyConn())
    monkeypatch.setattr(
        job_tui.saved_portal_import,
        "load_saved_portal_module",
        lambda name: module if name == portal else None,
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#provider-select", Select).value = "codex"
        screen.query_one("#priority-select", Select).value = "5"

        await pilot.click(button_id)
        await _wait_until(started.is_set)
        await pilot.pause()

        assert screen.query_one("#btn-import-linkedin", Button).disabled is True
        assert screen.query_one("#btn-import-trueup", Button).disabled is True
        assert screen.query_one("#btn-import-jackandjill", Button).disabled is True
        assert "Importing" in _feedback_text(screen.query_one("#add-feedback", Static))

        release.set()
        await _wait_until(lambda: not screen.query_one("#btn-import-linkedin", Button).disabled)
        await pilot.pause()

        feedback = _feedback_text(screen.query_one("#add-feedback", Static))
        assert captured == [("codex", 5)]
        assert screen.query_one("#btn-import-linkedin", Button).disabled is False
        assert screen.query_one("#btn-import-trueup", Button).disabled is False
        assert screen.query_one("#btn-import-jackandjill", Button).disabled is False
        assert feedback.startswith(f"{label}: 2 added, 1 duplicates, 1 unresolved")
        assert "status=ok" in feedback
        assert "[manual] note" in feedback


@pytest.mark.anyio
async def test_job_detail_screen_exposes_distinct_reset_to_new_action(monkeypatch, tmp_path):
    app = _DetailHarness()
    called = threading.Event()

    monkeypatch.setattr(job_tui, "_get_conn", lambda: _DummyConn())
    monkeypatch.setattr(
        job_tui,
        "get_job",
        lambda conn, job_id: {
            "id": job_id,
            "status": "draft",
            "company": "Example",
            "role_title": "Principal PM",
            "board": "greenhouse",
            "source": "direct",
            "provider": "openai",
            "url": "https://boards.greenhouse.io/example/jobs/1",
            "output_dir": str(tmp_path),
            "error_message": "",
            "progress": "",
            "archived": 0,
        },
    )
    monkeypatch.setattr(job_tui, "get_job_timeline", lambda conn, job_id: [])
    monkeypatch.setattr(job_tui, "ensure_job_metrics", lambda conn, job_id: None)
    monkeypatch.setattr(job_tui, "get_job_metrics", lambda conn, job_id: {"manual_interventions": 0})
    monkeypatch.setattr(job_tui, "update_job_metrics", lambda conn, job_id, manual_interventions: None)

    def fake_reset_job_to_new(conn, job_id, *, initiator="web", **_kwargs):
        called.set()
        return True

    monkeypatch.setattr(job_tui, "reset_job_to_new", fake_reset_job_to_new)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen

        reset_button = screen.query_one("#btn-draft-reset-to-new", Button)
        assert reset_button.label == "Reset to New"

        await pilot.click("#btn-draft-reset-to-new")
        await _wait_until(called.is_set)
