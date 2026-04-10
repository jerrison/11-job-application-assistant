import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import import_trueup_saved
from import_trueup_saved import MY_JOBS_URL, AuthRequiredError, import_saved_jobs
from job_db import init_db


def _open_db(tmp_path: Path):
    conn = init_db(tmp_path / "trueup_jobs.db")
    try:
        yield conn
    finally:
        conn.close()


def test_import_saved_jobs_queues_resolved_external_urls_with_trueup_provenance(tmp_path):
    for conn in _open_db(tmp_path):
        fake_context = object()
        with (
            patch("import_trueup_saved._trueup_context", return_value=nullcontext(fake_context)),
            patch(
                "import_trueup_saved._scrape_saved_jobs",
                return_value=[
                    {
                        "source_url": "https://www.trueup.io/jobs/acme-ai-pm",
                        "company": "Acme",
                        "role_title": "AI PM",
                    },
                    {
                        "source_url": "https://www.trueup.io/jobs/beta-platform-pm",
                        "company": "Beta",
                        "role_title": "Platform PM",
                    },
                ],
            ),
            patch(
                "import_trueup_saved._resolve_saved_job",
                side_effect=[
                    {
                        "status": "resolved",
                        "url": "https://boards.greenhouse.io/acme/jobs/1",
                        "source_url": "https://www.trueup.io/jobs/acme-ai-pm",
                        "company": "Acme",
                        "role_title": "AI PM",
                    },
                    {
                        "status": "unresolved",
                        "source_url": "https://www.trueup.io/jobs/beta-platform-pm",
                        "reason": "no external apply link",
                    },
                ],
            ),
        ):
            result = import_saved_jobs(conn, priority=5, provider="codex")

        assert result["status"] == "ok"
        assert result["added"] == 1
        assert result["skipped_unresolved"] == 1

        row = conn.execute("SELECT url, source, source_url, priority, provider FROM jobs").fetchone()
        assert row["url"] == "https://boards.greenhouse.io/acme/jobs/1"
        assert row["source"] == "trueup"
        assert row["source_url"] == "https://www.trueup.io/jobs/acme-ai-pm"
        assert row["priority"] == 5
        assert row["provider"] == "codex"


def test_import_saved_jobs_returns_auth_required_when_trueup_session_is_invalid(tmp_path):
    for conn in _open_db(tmp_path):
        fake_context = object()
        with (
            patch("import_trueup_saved._trueup_context", return_value=nullcontext(fake_context)),
            patch(
                "import_trueup_saved._scrape_saved_jobs",
                side_effect=AuthRequiredError("TrueUp session expired"),
            ),
        ):
            result = import_saved_jobs(conn)

        assert result["status"] == "auth_required"
        assert result["message"] == "TrueUp session expired"
        assert result["added"] == 0


def test_trueup_context_uses_saved_portal_browser_session(monkeypatch):
    from contextlib import contextmanager

    fake_browser = object()
    calls = []

    @contextmanager
    def fake_saved_portal_browser_session(**kwargs):
        calls.append(kwargs)
        yield fake_browser

    monkeypatch.delenv("TRUEUP_IMPORT_HEADLESS", raising=False)
    monkeypatch.setattr(import_trueup_saved, "saved_portal_browser_session", fake_saved_portal_browser_session)

    with import_trueup_saved._trueup_context() as browser:
        assert browser is fake_browser

    assert calls == [
        {
            "profile_dir": import_trueup_saved._TRUEUP_PROFILE_DIR,
            "lock_file": import_trueup_saved._TRUEUP_LOCK_FILE,
            "headless": False,
            "purpose": "TrueUp saved jobs import",
        }
    ]


def test_launch_auth_setup_opens_trueup_login_with_dedicated_profile(monkeypatch):
    calls = []

    def fake_open_saved_portal_login(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(import_trueup_saved, "open_saved_portal_login", fake_open_saved_portal_login)

    import_trueup_saved.launch_auth_setup()

    assert calls == [
        {
            "profile_dir": import_trueup_saved._TRUEUP_PROFILE_DIR,
            "lock_file": import_trueup_saved._TRUEUP_LOCK_FILE,
            "url": import_trueup_saved.MY_JOBS_URL,
            "purpose": "TrueUp saved jobs auth setup",
        }
    ]


def test_sanitize_saved_jobs_filters_external_source_urls_but_keeps_reopenable_cards():
    jobs = import_trueup_saved._sanitize_saved_jobs(
        [
            {
                "source_url": "https://www.trueup.io/jobs/acme-ai-pm",
                "company": " Acme ",
                "role_title": " AI PM ",
            },
            {
                "source_url": "https://boards.greenhouse.io/acme/jobs/1",
                "company": "External Only",
                "role_title": "Should Drop",
            },
            {
                "source_url": "https://boards.greenhouse.io/acme/jobs/2",
                "company": " Beta ",
                "role_title": " Platform PM ",
                "card_index": 3,
            },
            {
                "company": "No Card",
                "role_title": "No Source",
            },
        ]
    )

    assert jobs == [
        {
            "source_url": "https://www.trueup.io/jobs/acme-ai-pm",
            "company": "Acme",
            "role_title": "AI PM",
        },
        {
            "company": "Beta",
            "role_title": "Platform PM",
            "card_index": 3,
        },
    ]


def test_sanitize_saved_jobs_keeps_direct_external_jobs_with_trueup_provenance():
    jobs = import_trueup_saved._sanitize_saved_jobs(
        [
            {
                "source_url": MY_JOBS_URL,
                "external_url": "https://boards.greenhouse.io/acme/jobs/1?utm_source=trueup.io",
                "company": " Acme ",
                "role_title": " AI PM ",
            },
            {
                "source_url": MY_JOBS_URL,
                "external_url": "https://boards.greenhouse.io/acme/jobs/1?utm_source=trueup.io",
                "company": " Acme ",
                "role_title": " AI PM ",
            },
        ]
    )

    assert jobs == [
        {
            "source_url": MY_JOBS_URL,
            "external_url": "https://boards.greenhouse.io/acme/jobs/1?utm_source=trueup.io",
            "company": "Acme",
            "role_title": "AI PM",
        }
    ]


def test_pick_external_destination_prefers_apply_labels_and_preserves_query_string():
    preferred = import_trueup_saved._pick_external_destination(
        [
            {
                "url": "https://example.com/company-careers?id=123",
                "label": "Company careers",
            },
            {
                "url": "https://boards.greenhouse.io/acme/jobs/1?gh_jid=1",
                "label": "View job",
            },
        ]
    )
    fallback = import_trueup_saved._pick_external_destination(
        [
            {
                "url": "https://example.com/company-careers?id=123",
                "label": "Company careers",
            }
        ]
    )

    assert preferred == "https://boards.greenhouse.io/acme/jobs/1?gh_jid=1"
    assert fallback == "https://example.com/company-careers?id=123"


def test_find_external_destination_prefers_click_path_before_generic_external_fallback(monkeypatch):
    monkeypatch.setattr(
        import_trueup_saved,
        "_collect_external_link_candidates",
        lambda _page: [{"url": "https://example.com/company-careers?id=123", "label": "Company careers"}],
    )
    monkeypatch.setattr(
        import_trueup_saved,
        "_click_external_destination_control",
        lambda _page: "https://boards.greenhouse.io/acme/jobs/1?gh_jid=1",
    )

    destination = import_trueup_saved._find_external_destination(object())

    assert destination == "https://boards.greenhouse.io/acme/jobs/1?gh_jid=1"


class _FakePage:
    def __init__(self):
        self.url = MY_JOBS_URL
        self.goto_calls: list[str] = []
        self.closed = False

    def goto(self, url, **_kwargs):
        self.url = url
        self.goto_calls.append(url)

    def wait_for_timeout(self, _timeout):
        return None

    def wait_for_load_state(self, _state, **_kwargs):
        return None

    def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, page: _FakePage):
        self._page = page

    def new_page(self):
        return self._page


class _FakeShowMoreButton:
    def __init__(self, page, *, visible: bool = True):
        self._page = page
        self._visible = visible

    def count(self):
        return 1 if self._visible else 0

    def is_visible(self):
        return self._visible

    def is_disabled(self):
        return False

    def scroll_into_view_if_needed(self, **_kwargs):
        return None

    def click(self, **_kwargs):
        self._page.advance()


class _FakeExpandablePage(_FakePage):
    def __init__(self, counts: list[int]):
        super().__init__()
        self._counts = counts
        self._index = 0
        self.show_more_clicks = 0

    @property
    def extracted_count(self) -> int:
        return self._counts[self._index]

    def advance(self):
        self.show_more_clicks += 1
        if self._index < len(self._counts) - 1:
            self._index += 1

    def get_by_role(self, role, name=None):
        if role == "button" and name == "Show more":
            visible = self._index < len(self._counts) - 1
            return _FakeShowMoreButton(self, visible=visible)
        return _FakeShowMoreButton(self, visible=False)


def test_resolve_saved_job_loads_reopened_trueup_source_url_before_external_resolution(monkeypatch):
    page = _FakePage()
    context = _FakeContext(page)

    monkeypatch.setattr(
        import_trueup_saved,
        "_open_saved_job_from_my_jobs",
        lambda _page, _job: "https://www.trueup.io/jobs/acme-ai-pm",
    )
    monkeypatch.setattr(import_trueup_saved, "_ensure_trueup_logged_in", lambda _page: None)
    monkeypatch.setattr(
        import_trueup_saved,
        "_find_external_destination",
        lambda current_page: (
            "https://boards.greenhouse.io/acme/jobs/1?gh_jid=1"
            if current_page.url == "https://www.trueup.io/jobs/acme-ai-pm"
            else None
        ),
    )
    monkeypatch.setattr(
        import_trueup_saved,
        "_extract_page_jd_text",
        lambda current_page, source_url: "Factory systems product lead role.",
    )

    result = import_trueup_saved._resolve_saved_job(
        context,
        {
            "company": "Acme",
            "role_title": "AI PM",
            "card_index": 0,
        },
    )

    assert result["status"] == "resolved"
    assert result["url"] == "https://boards.greenhouse.io/acme/jobs/1?gh_jid=1"
    assert result["source_url"] == "https://www.trueup.io/jobs/acme-ai-pm"
    assert result["jd_text"] == "Factory systems product lead role."
    assert page.goto_calls == ["https://www.trueup.io/jobs/acme-ai-pm"]
    assert page.closed is True


def test_resolve_saved_job_rejects_non_trueup_source_urls_without_auth_failure():
    page = _FakePage()
    context = _FakeContext(page)

    result = import_trueup_saved._resolve_saved_job(
        context,
        {
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "company": "Acme",
            "role_title": "AI PM",
        },
    )

    assert result["status"] == "unresolved"
    assert result["source_url"] == MY_JOBS_URL
    assert "TrueUp job URL" in result["reason"]
    assert page.goto_calls == []
    assert page.closed is True


def test_resolve_saved_job_returns_direct_external_url_with_best_effort_jd_text(monkeypatch):
    page = _FakePage()
    context = _FakeContext(page)
    external_url = "https://boards.greenhouse.io/acme/jobs/1?utm_source=trueup.io"

    monkeypatch.setattr(
        import_trueup_saved,
        "_extract_page_jd_text",
        lambda current_page, source_url: "Platform roadmap and marketplace integrations.",
    )

    result = import_trueup_saved._resolve_saved_job(
        context,
        {
            "source_url": MY_JOBS_URL,
            "external_url": external_url,
            "company": "Acme",
            "role_title": "AI PM",
        },
    )

    assert result["status"] == "resolved"
    assert result["source_url"] == MY_JOBS_URL
    assert result["url"] == external_url
    assert result["company"] == "Acme"
    assert result["role_title"] == "AI PM"
    assert result["jd_text"] == "Platform roadmap and marketplace integrations."
    assert page.goto_calls == [external_url]
    assert page.closed is True


def test_expand_all_saved_jobs_clicks_show_more_until_job_count_stops(monkeypatch):
    page = _FakeExpandablePage([20, 40, 60, 80])

    monkeypatch.setattr(
        import_trueup_saved,
        "_extract_page_jobs",
        lambda current_page: [{}] * current_page.extracted_count,
    )
    monkeypatch.setattr(import_trueup_saved, "_wait_for_page_settle", lambda _page: None)

    final_count = import_trueup_saved._expand_all_saved_jobs(page)

    assert final_count == 80
    assert page.show_more_clicks == 3


def test_trueup_credentials_fall_back_to_shared_login_email(monkeypatch):
    monkeypatch.setenv("JOB_ASSETS_LOGIN_EMAIL", "shared@example.test")
    monkeypatch.delenv("TRUEUP_EMAIL", raising=False)
    monkeypatch.setenv("TRUEUP_PASSWORD", "secret-password")

    assert import_trueup_saved._trueup_credentials() == ("shared@example.test", "secret-password")


def test_ensure_trueup_logged_in_auto_submits_sign_in_form_from_env(monkeypatch):
    class FakeLocator:
        def __init__(self):
            self.filled: list[str] = []
            self.clicked = 0

        def first(self):
            return self

        def count(self):
            return 1

        def fill(self, value):
            self.filled.append(value)

        def click(self):
            self.clicked += 1

    class FakePage:
        def __init__(self):
            self.url = "https://www.trueup.io/sign-in"
            self.email = FakeLocator()
            self.password = FakeLocator()
            self.submit = FakeLocator()

        def title(self):
            return "Sign in to TrueUp"

        def locator(self, selector):
            if selector == "body":
                return type("BodyLocator", (), {"inner_text": lambda self, timeout=4000: "Sign in to TrueUp"})()
            if "input" in selector and "password" not in selector:
                return self.email
            if "password" in selector:
                return self.password
            if "button" in selector:
                return self.submit
            raise AssertionError(f"Unexpected selector: {selector}")

        def wait_for_timeout(self, _timeout):
            self.url = "https://www.trueup.io/myjobs"

    monkeypatch.setenv("JOB_ASSETS_LOGIN_EMAIL", "shared@example.test")
    monkeypatch.delenv("TRUEUP_EMAIL", raising=False)
    monkeypatch.setenv("TRUEUP_PASSWORD", "trueup-secret")

    page = FakePage()

    import_trueup_saved._ensure_trueup_logged_in(page)

    assert page.email.filled == ["shared@example.test"]
    assert page.password.filled == ["trueup-secret"]
    assert page.submit.clicked == 1
    assert page.url == "https://www.trueup.io/myjobs"


def test_ensure_trueup_logged_in_raises_auth_required_without_credentials(monkeypatch):
    class FakePage:
        url = "https://www.trueup.io/sign-in"

        def title(self):
            return "Sign in to TrueUp"

        def locator(self, selector):
            if selector == "body":
                return type("BodyLocator", (), {"inner_text": lambda self, timeout=4000: "Sign in to TrueUp"})()
            raise AssertionError(f"Unexpected selector: {selector}")

    monkeypatch.delenv("JOB_ASSETS_LOGIN_EMAIL", raising=False)
    monkeypatch.delenv("TRUEUP_EMAIL", raising=False)
    monkeypatch.delenv("TRUEUP_PASSWORD", raising=False)

    with pytest.raises(AuthRequiredError, match="TrueUp authentication required"):
        import_trueup_saved._ensure_trueup_logged_in(FakePage())


def test_ensure_trueup_logged_in_fetches_security_code_from_gmail(monkeypatch):
    class FakeLocator:
        def __init__(self, *, visible: bool = True, on_click=None):
            self.visible = visible
            self.on_click = on_click
            self.filled: list[str] = []
            self.clicked = 0

        def first(self):
            return self

        def count(self):
            return 1 if self.visible else 0

        def fill(self, value):
            self.filled.append(value)

        def click(self):
            self.clicked += 1
            if self.on_click is not None:
                self.on_click()

    class FakePage:
        def __init__(self):
            self.stage = "login"
            self.url = "https://www.trueup.io/sign-in"
            self.email = FakeLocator()
            self.password = FakeLocator()
            self.code = FakeLocator()
            self.submit = FakeLocator(on_click=self._after_login_submit)
            self.verify = FakeLocator(on_click=self._after_code_submit)

        def _after_login_submit(self):
            self.stage = "code"
            self.url = "https://www.trueup.io/security-check"

        def _after_code_submit(self):
            self.stage = "done"
            self.url = "https://www.trueup.io/myjobs"

        def title(self):
            if self.stage == "login":
                return "Sign in to TrueUp"
            if self.stage == "code":
                return "Security Code"
            return "My Jobs"

        def locator(self, selector):
            if selector == "body":
                text = {
                    "login": "Sign in to TrueUp",
                    "code": "Enter the security code we emailed you.",
                    "done": "My Jobs",
                }[self.stage]
                return type("BodyLocator", (), {"inner_text": lambda self, timeout=4000, value=text: value})()
            if "code" in selector or "inputmode" in selector or "one-time-code" in selector:
                return self.code if self.stage == "code" else FakeLocator(visible=False)
            if "password" in selector:
                return self.password if self.stage == "login" else FakeLocator(visible=False)
            if "input" in selector:
                return self.email if self.stage == "login" else FakeLocator(visible=False)
            if "button" in selector:
                return self.submit if self.stage == "login" else self.verify
            raise AssertionError(f"Unexpected selector: {selector}")

        def wait_for_timeout(self, _timeout):
            return None

    monkeypatch.setenv("JOB_ASSETS_LOGIN_EMAIL", "shared@example.test")
    monkeypatch.delenv("TRUEUP_EMAIL", raising=False)
    monkeypatch.setenv("TRUEUP_PASSWORD", "trueup-secret")
    monkeypatch.setattr(import_trueup_saved, "fetch_security_code_from_gmail", lambda *args, **kwargs: "123456")

    page = FakePage()

    import_trueup_saved._ensure_trueup_logged_in(page)

    assert page.email.filled == ["shared@example.test"]
    assert page.password.filled == ["trueup-secret"]
    assert page.code.filled == ["123456"]
    assert page.verify.clicked == 1
    assert page.url == "https://www.trueup.io/myjobs"
