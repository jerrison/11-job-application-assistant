import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import import_linkedin_saved
import saved_portal_import
from import_linkedin_saved import import_saved_jobs
from job_db import add_job, init_db, update_status


def _open_db(tmp_path: Path):
    conn = init_db(tmp_path / "test_jobs.db")
    try:
        yield conn
    finally:
        conn.close()


def test_import_saved_jobs_skips_submitted_company_role_duplicate_and_syncs_linkedin(tmp_path):
    for conn in _open_db(tmp_path):
        existing_id = add_job(
            conn,
            url="https://boards.greenhouse.io/acme/jobs/1",
            company="Acme",
            role_title="Staff Product Manager",
        )
        update_status(conn, existing_id, "submitted", board="greenhouse")

        with (
            patch(
                "import_linkedin_saved._scrape_saved_jobs",
                return_value=[
                    {
                        "url": "https://www.linkedin.com/jobs/view/12345",
                        "company": "Acme",
                        "role_title": "Staff Product Manager",
                    }
                ],
            ),
            patch("import_linkedin_saved._mark_and_hide_linkedin_job", return_value=(True, True)) as sync_linkedin,
        ):
            result = import_saved_jobs(conn)

        assert result["scraped"] == 1
        assert result["added"] == 0
        assert result["duplicates"] == 1
        assert result["errors"] == 0
        assert result["linkedin_marked"] == 1
        assert result["linkedin_hidden"] == 1
        sync_linkedin.assert_called_once_with("https://www.linkedin.com/jobs/view/12345")
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_import_saved_jobs_skips_non_submitted_duplicate_without_linkedin_sync(tmp_path):
    for conn in _open_db(tmp_path):
        add_job(
            conn,
            url="https://boards.greenhouse.io/acme/jobs/2",
            company="Acme",
            role_title="Senior Product Manager",
        )

        with (
            patch(
                "import_linkedin_saved._scrape_saved_jobs",
                return_value=[
                    {
                        "url": "https://www.linkedin.com/jobs/view/23456",
                        "company": "Acme",
                        "role_title": "Senior Product Manager",
                    }
                ],
            ),
            patch("import_linkedin_saved._mark_and_hide_linkedin_job") as sync_linkedin,
        ):
            result = import_saved_jobs(conn)

        assert result["scraped"] == 1
        assert result["added"] == 0
        assert result["duplicates"] == 1
        assert result["errors"] == 0
        assert result["linkedin_marked"] == 0
        assert result["linkedin_hidden"] == 0
        sync_linkedin.assert_not_called()
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_import_saved_jobs_syncs_submitted_exact_url_duplicate_without_metadata(tmp_path):
    for conn in _open_db(tmp_path):
        linkedin_url = "https://www.linkedin.com/jobs/view/34567"
        existing_id = add_job(conn, url=linkedin_url)
        update_status(conn, existing_id, "submitted", board="greenhouse")

        with (
            patch(
                "import_linkedin_saved._scrape_saved_jobs",
                return_value=[{"url": linkedin_url, "company": None, "role_title": None}],
            ),
            patch("import_linkedin_saved._mark_and_hide_linkedin_job", return_value=(True, False)) as sync_linkedin,
        ):
            result = import_saved_jobs(conn)

        assert result["scraped"] == 1
        assert result["added"] == 0
        assert result["duplicates"] == 1
        assert result["errors"] == 0
        assert result["linkedin_marked"] == 1
        assert result["linkedin_hidden"] == 0
        sync_linkedin.assert_called_once_with(linkedin_url)
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_import_saved_jobs_syncs_archived_submitted_exact_url_duplicate_without_metadata(tmp_path):
    for conn in _open_db(tmp_path):
        linkedin_url = "https://www.linkedin.com/jobs/view/34568"
        existing_id = add_job(conn, url=linkedin_url)
        update_status(conn, existing_id, "submitted", board="greenhouse")
        conn.execute("UPDATE jobs SET archived = TRUE WHERE id = ?", (existing_id,))
        conn.commit()

        with (
            patch(
                "import_linkedin_saved._scrape_saved_jobs",
                return_value=[{"url": linkedin_url, "company": None, "role_title": None}],
            ),
            patch("import_linkedin_saved._mark_and_hide_linkedin_job", return_value=(True, True)) as sync_linkedin,
        ):
            result = import_saved_jobs(conn)

        assert result["scraped"] == 1
        assert result["added"] == 0
        assert result["duplicates"] == 1
        assert result["errors"] == 0
        assert result["linkedin_marked"] == 1
        assert result["linkedin_hidden"] == 1
        sync_linkedin.assert_called_once_with(linkedin_url)
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_import_saved_jobs_returns_shared_result_fields(tmp_path):
    for conn in _open_db(tmp_path):
        linkedin_url = "https://www.linkedin.com/jobs/view/45678"
        with patch(
            "import_linkedin_saved._scrape_saved_jobs",
            return_value=[
                {
                    "url": linkedin_url,
                    "company": "Acme",
                    "role_title": "Principal Product Manager",
                }
            ],
        ):
            result = import_saved_jobs(conn)

        row = conn.execute("SELECT url, source, source_url, status FROM jobs").fetchone()

        assert result["status"] == "ok"
        assert result["resolved"] == 1
        assert result["skipped_unresolved"] == 0
        assert result["added"] == 1
        assert row["url"] == linkedin_url
        assert row["source"] == "linkedin"
        assert row["source_url"] == linkedin_url
        assert row["status"] == "queued"


def test_resolve_saved_job_drops_location_label_company():
    resolved = import_linkedin_saved._resolve_saved_job(
        {
            "url": "https://www.linkedin.com/jobs/view/56789",
            "company": "Remote",
            "role_title": "Staff AI Product Manager",
        }
    )

    assert resolved["company"] is None


def test_scrape_saved_jobs_uses_saved_portal_browser_session(monkeypatch):
    linkedin_url = "https://www.linkedin.com/jobs/view/98765"

    class FakePage:
        def __init__(self):
            self.url = "about:blank"
            self.goto_calls = []

        def goto(self, url, **_kwargs):
            self.url = url
            self.goto_calls.append(url)

        def wait_for_timeout(self, _timeout):
            return None

        def query_selector(self, _selector):
            return None

    class FakeBrowser:
        def __init__(self, page):
            self.page = page

        def new_page(self):
            return self.page

    fake_page = FakePage()
    calls = []

    @contextmanager
    def fake_saved_portal_browser_session(**kwargs):
        calls.append(kwargs)
        yield FakeBrowser(fake_page)

    monkeypatch.setattr(import_linkedin_saved, "saved_portal_browser_session", fake_saved_portal_browser_session)
    monkeypatch.setattr(
        import_linkedin_saved,
        "_extract_page_jobs",
        lambda _page: [{"url": linkedin_url, "company": "Acme", "role_title": "Principal Product Manager"}],
    )

    jobs = import_linkedin_saved._scrape_saved_jobs(max_pages=1)

    assert jobs == [{"url": linkedin_url, "company": "Acme", "role_title": "Principal Product Manager"}]
    assert fake_page.goto_calls == [import_linkedin_saved.SAVED_JOBS_URL]
    assert calls == [
        {
            "profile_dir": import_linkedin_saved._LINKEDIN_PROFILE_DIR,
            "lock_file": import_linkedin_saved._LINKEDIN_LOCK_FILE,
            "headless": True,
            "purpose": "LinkedIn saved jobs import",
            "normalize_zoom_hosts": ("linkedin.com", "www.linkedin.com"),
            "reset_default_zoom": True,
        }
    ]


def test_scrape_saved_jobs_raises_auth_required_when_linkedin_login_is_unavailable(monkeypatch):
    import url_resolver

    class FakePage:
        def __init__(self):
            self.url = "about:blank"
            self.goto_calls = []

        def goto(self, url, **_kwargs):
            self.goto_calls.append(url)
            self.url = "https://www.linkedin.com/authwall?trk=guest_homepage"

        def wait_for_timeout(self, _timeout):
            return None

        def query_selector(self, _selector):
            return None

    class FakeBrowser:
        def __init__(self, page):
            self.page = page

        def new_page(self):
            return self.page

    fake_page = FakePage()

    @contextmanager
    def fake_saved_portal_browser_session(**_kwargs):
        yield FakeBrowser(fake_page)

    monkeypatch.setattr(import_linkedin_saved, "saved_portal_browser_session", fake_saved_portal_browser_session)
    monkeypatch.setattr(url_resolver, "_ensure_linkedin_logged_in", lambda _page: False)

    with pytest.raises(saved_portal_import.AuthRequiredError, match="LinkedIn authentication required"):
        import_linkedin_saved._scrape_saved_jobs(max_pages=1)
