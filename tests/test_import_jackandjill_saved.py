import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import import_jackandjill_saved
from import_jackandjill_saved import AuthRequiredError, import_saved_jobs
from job_db import init_db


def _open_db(tmp_path: Path):
    conn = init_db(tmp_path / "jackandjill_jobs.db")
    try:
        yield conn
    finally:
        conn.close()


def test_import_saved_jobs_queues_resolved_urls_with_jackandjill_provenance(tmp_path):
    for conn in _open_db(tmp_path):
        fake_context = object()
        with (
            patch("import_jackandjill_saved._jackandjill_context", return_value=nullcontext(fake_context)),
            patch(
                "import_jackandjill_saved._scrape_saved_jobs",
                return_value=[
                    {
                        "source_url": "https://app.jackandjill.ai/jack/opportunities/1",
                        "company": "Acme",
                        "role_title": "AI PM",
                    },
                    {
                        "source_url": "https://app.jackandjill.ai/jack/opportunities/2",
                        "company": "Beta",
                        "role_title": "Platform PM",
                    },
                ],
            ),
            patch(
                "import_jackandjill_saved._resolve_saved_job",
                side_effect=[
                    {
                        "status": "resolved",
                        "url": "https://boards.greenhouse.io/acme/jobs/1",
                        "source_url": "https://app.jackandjill.ai/jack/opportunities/1",
                        "company": "Acme",
                        "role_title": "AI PM",
                    },
                    {
                        "status": "unresolved",
                        "source_url": "https://app.jackandjill.ai/jack/opportunities/2",
                        "reason": "missing job URL",
                    },
                ],
            ),
        ):
            result = import_saved_jobs(conn, priority=4, provider="codex")

        assert result["status"] == "ok"
        assert result["added"] == 1
        assert result["skipped_unresolved"] == 1

        row = conn.execute("SELECT url, source, source_url, priority, provider FROM jobs").fetchone()
        assert row["url"] == "https://boards.greenhouse.io/acme/jobs/1"
        assert row["source"] == "jackandjill"
        assert row["source_url"] == "https://app.jackandjill.ai/jack/opportunities/1"
        assert row["priority"] == 4
        assert row["provider"] == "codex"


def test_import_saved_jobs_returns_auth_required_when_session_is_invalid(tmp_path):
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
        assert result["added"] == 0


def test_launch_auth_setup_opens_jackandjill_login_with_dedicated_profile(monkeypatch):
    calls = []

    def fake_open_saved_portal_login(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(import_jackandjill_saved, "open_saved_portal_login", fake_open_saved_portal_login)

    import_jackandjill_saved.launch_auth_setup()

    assert calls == [
        {
            "profile_dir": import_jackandjill_saved._JACKANDJILL_PROFILE_DIR,
            "lock_file": import_jackandjill_saved._JACKANDJILL_LOCK_FILE,
            "url": import_jackandjill_saved.OPPORTUNITIES_URL,
            "purpose": "Jack & Jill saved jobs auth setup",
        }
    ]


class _FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.goto_calls: list[str] = []
        self.closed = False

    def goto(self, url, **_kwargs):
        self.url = url
        self.goto_calls.append(url)

    def wait_for_timeout(self, _timeout):
        return None

    def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, page: _FakePage):
        self._page = page

    def new_page(self):
        return self._page


def test_resolve_saved_job_opens_source_url_and_returns_external_destination(monkeypatch):
    page = _FakePage()
    context = _FakeContext(page)

    monkeypatch.setattr(import_jackandjill_saved, "_ensure_jackandjill_logged_in", lambda _page: None)
    monkeypatch.setattr(
        import_jackandjill_saved,
        "_find_external_destination",
        lambda _page: "https://boards.greenhouse.io/acme/jobs/1",
    )

    result = import_jackandjill_saved._resolve_saved_job(
        context,
        {
            "source_url": "https://app.jackandjill.ai/jack/opportunities/1",
            "company": "Acme",
            "role_title": "AI PM",
        },
    )

    assert result["status"] == "resolved"
    assert result["url"] == "https://boards.greenhouse.io/acme/jobs/1"
    assert result["source_url"] == "https://app.jackandjill.ai/jack/opportunities/1"
    assert result["company"] == "Acme"
    assert result["role_title"] == "AI PM"
    assert page.goto_calls == ["https://app.jackandjill.ai/jack/opportunities/1"]
    assert page.closed is True


def test_pick_external_destination_skips_non_job_links_when_fallback_only():
    candidates = [
        {"url": "https://example.com/privacy", "label": "Privacy policy"},
        {"url": "https://example.com/blog", "label": "Company blog"},
    ]
    assert import_jackandjill_saved._pick_external_destination(candidates) is None


def test_pick_external_destination_prefers_job_shaped_fallback_over_noise():
    candidates = [
        {"url": "https://example.com/privacy", "label": "Privacy policy"},
        {"url": "https://boards.greenhouse.io/acme/jobs/1", "label": ""},
    ]
    assert (
        import_jackandjill_saved._pick_external_destination(candidates)
        == "https://boards.greenhouse.io/acme/jobs/1"
    )


class _FakeClickContext:
    def __init__(self):
        self.pages = []


class _FakeClickPage:
    def __init__(self, context: _FakeClickContext, url: str):
        self.context = context
        self.url = url

    def locator(self, _selector):
        return _FakeClickLocator(self)

    def wait_for_timeout(self, _timeout):
        return None

    def wait_for_load_state(self, _state, **_kwargs):
        return None

    def trigger_click(self):
        new_page = _FakeClickPage(self.context, "https://boards.greenhouse.io/acme/jobs/1")
        self.context.pages.append(new_page)


class _FakeClickLocator:
    def __init__(self, page: _FakeClickPage):
        self._page = page
        self.first = self

    def count(self):
        return 1

    def click(self, **_kwargs):
        self._page.trigger_click()


def test_click_external_destination_control_returns_new_page_url(monkeypatch):
    context = _FakeClickContext()
    page = _FakeClickPage(context, "https://app.jackandjill.ai/jack/opportunities/1")
    context.pages.append(page)

    monkeypatch.setattr(import_jackandjill_saved, "_mark_external_destination_control", lambda _page: True)
    monkeypatch.setattr(import_jackandjill_saved, "_collect_external_link_candidates", lambda _page: [])

    destination = import_jackandjill_saved._click_external_destination_control(page)

    assert destination == "https://boards.greenhouse.io/acme/jobs/1"
