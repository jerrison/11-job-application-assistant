import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import saved_portal_import
from job_db import add_job, init_db


def _open_db(tmp_path: Path):
    conn = init_db(tmp_path / "saved_portal_import.db")
    try:
        yield conn
    finally:
        conn.close()


def test_import_saved_portal_jobs_passes_jd_text_and_counts_results(monkeypatch):
    conn = object()
    portal_name = "trueup"
    duplicate_groups = [[{"id": 10}, {"id": 11}]]

    candidates = [
        {
            "source_url": "https://www.trueup.io/jobs/1",
            "company": "Acme",
            "role_title": "Senior Product Manager",
        },
        {
            "source_url": "https://www.trueup.io/jobs/2",
            "company": "Acme",
            "role_title": "Staff Product Manager",
        },
        {
            "source_url": "https://www.trueup.io/jobs/3",
            "company": "Acme",
            "role_title": "Group Product Manager",
        },
        {
            "source_url": "https://www.trueup.io/jobs/4",
            "company": "Acme",
            "role_title": "Principal Product Manager",
        },
    ]

    def scrape_jobs():
        return candidates

    def resolve_job(candidate):
        source_url = candidate["source_url"]
        if source_url.endswith("/1"):
            return {
                "status": "resolved",
                "url": "https://boards.greenhouse.io/acme/jobs/101",
                "company": "Acme",
                "role_title": "Senior Product Manager",
                "jd_text": "Platform roadmap and marketplace integrations.",
                "source_url": source_url,
            }
        if source_url.endswith("/2"):
            return {
                "status": "resolved",
                "url": "https://boards.greenhouse.io/acme/jobs/102",
                "company": "Acme",
                "role_title": "Staff Product Manager",
                "jd_text": "Staff PM role focused on platform scale.",
            }
        if source_url.endswith("/3"):
            return {
                "status": "unresolved",
                "reason": "no external application URL found",
                "source_url": source_url,
            }
        raise RuntimeError("resolver crashed")

    add_calls = []

    def fake_add_job(conn_, url, **kwargs):
        add_calls.append((conn_, url, kwargs))
        if url.endswith("/102"):
            return -77
        return 42

    duplicate_calls = []

    def on_duplicate(conn_, resolved, existing_id):
        duplicate_calls.append((conn_, resolved, existing_id))

    monkeypatch.setattr(saved_portal_import, "add_job", fake_add_job)
    monkeypatch.setattr(saved_portal_import, "backfill_jd_fingerprints", lambda _: (3, 0))
    monkeypatch.setattr(saved_portal_import, "find_jd_duplicates", lambda _: duplicate_groups)

    result = saved_portal_import.import_saved_portal_jobs(
        conn,
        portal_name=portal_name,
        scrape_jobs=scrape_jobs,
        resolve_job=resolve_job,
        priority=7,
        provider="openai",
        on_duplicate=on_duplicate,
    )

    assert result["status"] == "ok"
    assert result["message"] == ""
    assert result["scraped"] == 4
    assert result["resolved"] == 2
    assert result["added"] == 1
    assert result["duplicates"] == 1
    assert result["skipped_unresolved"] == 1
    assert result["errors"] == 1
    assert result["fingerprints_added"] == 3
    assert result["duplicate_groups"] == duplicate_groups
    assert len(result["samples"]["unresolved"]) == 1
    assert len(result["samples"]["errors"]) == 1
    assert result["samples"]["unresolved"][0]["source_url"] == "https://www.trueup.io/jobs/3"
    assert result["samples"]["errors"][0]["source_url"] == "https://www.trueup.io/jobs/4"
    assert "resolver crashed" in result["samples"]["errors"][0]["reason"]

    assert len(add_calls) == 2
    assert add_calls[0][0] is conn
    assert add_calls[0][1] == "https://boards.greenhouse.io/acme/jobs/101"
    assert add_calls[0][2] == {
        "priority": 7,
        "provider": "openai",
        "company": "Acme",
        "role_title": "Senior Product Manager",
        "jd_text": "Platform roadmap and marketplace integrations.",
        "source_override": "trueup",
        "source_url_override": "https://www.trueup.io/jobs/1",
    }
    assert add_calls[1][1] == "https://boards.greenhouse.io/acme/jobs/102"
    assert add_calls[1][2]["source_url_override"] == "https://www.trueup.io/jobs/2"
    assert add_calls[1][2]["jd_text"] == "Staff PM role focused on platform scale."

    assert duplicate_calls == [
        (
            conn,
            {
                "status": "resolved",
                "url": "https://boards.greenhouse.io/acme/jobs/102",
                "company": "Acme",
                "role_title": "Staff Product Manager",
                "jd_text": "Staff PM role focused on platform scale.",
            },
            77,
        )
    ]


def test_import_saved_portal_jobs_returns_auth_required_when_scrape_fails():
    conn = object()

    def scrape_jobs():
        raise saved_portal_import.AuthRequiredError("login required to access saved jobs")

    def resolve_job(_candidate):
        raise AssertionError("resolve_job should not be called")

    result = saved_portal_import.import_saved_portal_jobs(
        conn,
        portal_name="linkedin",
        scrape_jobs=scrape_jobs,
        resolve_job=resolve_job,
    )

    assert result["status"] == "auth_required"
    assert result["message"] == "login required to access saved jobs"
    assert result["scraped"] == 0
    assert result["resolved"] == 0
    assert result["added"] == 0
    assert result["duplicates"] == 0
    assert result["skipped_unresolved"] == 0
    assert result["errors"] == 0
    assert result["fingerprints_added"] == 0
    assert result["duplicate_groups"] == []
    assert result["samples"] == {"unresolved": [], "errors": []}


def test_import_saved_portal_jobs_calls_on_duplicate_with_none_for_integrity_error(tmp_path):
    for conn in _open_db(tmp_path):
        external_url = "https://boards.greenhouse.io/acme/jobs/101"
        add_job(
            conn,
            external_url,
            company="Existing Company",
            role_title="Existing Role",
        )

        duplicate_calls = []

        def scrape_jobs():
            return [
                {
                    "source_url": "https://www.trueup.io/jobs/acme-101",
                    "company": "Acme",
                    "role_title": "Senior Product Manager",
                }
            ]

        def resolve_job(candidate, *, external_url=external_url):
            return {
                "status": "resolved",
                "url": external_url,
                "source_url": candidate["source_url"],
                "company": candidate["company"],
                "role_title": candidate["role_title"],
            }

        def on_duplicate(conn_, resolved, existing_id, *, duplicate_calls=duplicate_calls):
            duplicate_calls.append((conn_, resolved, existing_id))

        result = saved_portal_import.import_saved_portal_jobs(
            conn,
            portal_name="trueup",
            scrape_jobs=scrape_jobs,
            resolve_job=resolve_job,
            on_duplicate=on_duplicate,
        )

        assert result["status"] == "ok"
        assert result["scraped"] == 1
        assert result["resolved"] == 1
        assert result["added"] == 0
        assert result["duplicates"] == 1
        assert result["errors"] == 0
        assert duplicate_calls == [
            (
                conn,
                {
                    "status": "resolved",
                    "url": external_url,
                    "source_url": "https://www.trueup.io/jobs/acme-101",
                    "company": "Acme",
                    "role_title": "Senior Product Manager",
                },
                None,
            )
        ]


def test_saved_portal_registry_lists_and_loads_specs():
    portals = saved_portal_import.list_saved_portals()
    assert [portal.key for portal in portals] == ["linkedin", "trueup", "jackandjill"]
    assert [portal.label for portal in portals] == ["LinkedIn", "TrueUp", "Jack & Jill"]
    assert [portal.module_name for portal in portals] == [
        "import_linkedin_saved",
        "import_trueup_saved",
        "import_jackandjill_saved",
    ]

    spec = saved_portal_import.get_saved_portal("jackandjill")
    assert spec.key == "jackandjill"
    assert spec.label == "Jack & Jill"
    assert spec.module_name == "import_jackandjill_saved"

    module = saved_portal_import.load_saved_portal_module("jackandjill")
    assert hasattr(module, "import_saved_jobs")


def test_launch_saved_portal_auth_setup_dispatches_to_module_launcher():
    fake_module = mock.Mock()

    with mock.patch("saved_portal_import.load_saved_portal_module", return_value=fake_module) as load_module:
        saved_portal_import.launch_saved_portal_auth_setup("trueup")

    load_module.assert_called_once_with("trueup")
    fake_module.launch_auth_setup.assert_called_once_with()
