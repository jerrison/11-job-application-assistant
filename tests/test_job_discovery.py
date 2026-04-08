"""Tests for job discovery: search, scoring, promotion, and filtering."""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    from job_db import init_db

    conn = init_db(":memory:")
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jobs_df(rows: list[dict]) -> object:
    """Build a minimal pandas DataFrame-like stub for mocking scrape_jobs."""
    try:
        import pandas as pd

        return pd.DataFrame(rows)
    except ImportError:
        # Build a minimal stub that supports iterrows()
        class _Row:
            def __init__(self, data: dict) -> None:
                self._data = data

            def to_dict(self) -> dict:
                return dict(self._data)

        class _DF:
            def __init__(self, rows: list[dict]) -> None:
                self._rows = rows

            def iterrows(self):  # noqa: ANN201
                for i, r in enumerate(self._rows):
                    yield i, _Row(r)

            def __len__(self) -> int:
                return len(self._rows)

        return _DF(rows)


_SAMPLE_JOB = {
    "site": "linkedin",
    "job_url": "https://linkedin.com/jobs/view/999",
    "job_url_direct": "https://company.com/apply/999",
    "title": "Senior Product Manager",
    "company": "Acme Corp",
    "location": "San Francisco, CA",
    "min_amount": 150000,
    "max_amount": 200000,
    "currency": "USD",
    "interval": "yearly",
    "job_type": "fulltime",
    "job_level": "Senior",
    "is_remote": True,
    "date_posted": "2026-03-15",
    "description": "We are looking for a senior PM to join our team.",
    "company_industry": "Technology",
    "company_rating": 4.2,
}


# ---------------------------------------------------------------------------
# TestCandidateJobsSchema
# ---------------------------------------------------------------------------


class TestCandidateJobsSchema:
    def test_table_exists(self, db):
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "candidate_jobs" in tables

    def test_insert_works(self, db):
        db.execute(
            """
            INSERT INTO candidate_jobs (source, title, company, job_url)
            VALUES ('linkedin', 'PM', 'Acme', 'https://example.com/job/1')
            """
        )
        db.commit()
        row = db.execute("SELECT * FROM candidate_jobs WHERE id = 1").fetchone()
        assert row is not None
        assert row["title"] == "PM"
        assert row["status"] == "new"

    def test_unique_constraint_on_job_url(self, db):
        import sqlite3

        db.execute(
            "INSERT INTO candidate_jobs (source, title, company, job_url) "
            "VALUES ('linkedin', 'PM', 'Acme', 'https://example.com/job/1')"
        )
        db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO candidate_jobs (source, title, company, job_url) "
                "VALUES ('indeed', 'PM', 'Acme', 'https://example.com/job/1')"
            )
            db.commit()

    def test_indexes_exist(self, db):
        indexes = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert "idx_candidate_status" in indexes
        assert "idx_candidate_score" in indexes
        assert "idx_candidate_source" in indexes


# ---------------------------------------------------------------------------
# TestJobSearch
# ---------------------------------------------------------------------------


class TestJobSearch:
    def test_search_returns_candidates(self, db):
        from job_discovery import search_jobs

        mock_df = _make_jobs_df([_SAMPLE_JOB])

        with patch("job_discovery.scrape_jobs", return_value=mock_df):
            results = search_jobs(db, "Product Manager", "San Francisco, CA")

        assert len(results) == 1
        assert results[0]["title"] == "Senior Product Manager"
        assert results[0]["company"] == "Acme Corp"
        assert results[0]["source"] == "linkedin"
        assert results[0]["status"] == "new"

    def test_search_stores_salary(self, db):
        from job_discovery import search_jobs

        mock_df = _make_jobs_df([_SAMPLE_JOB])

        with patch("job_discovery.scrape_jobs", return_value=mock_df):
            results = search_jobs(db, "PM", "SF")

        assert results[0]["salary"] is not None
        assert "150,000" in results[0]["salary"]

    def test_search_deduplication(self, db):
        from job_discovery import search_jobs

        mock_df = _make_jobs_df([_SAMPLE_JOB])

        with patch("job_discovery.scrape_jobs", return_value=mock_df):
            first = search_jobs(db, "PM", "SF")
            second = search_jobs(db, "PM", "SF")

        # First call inserts 1, second call returns 0 new (duplicate)
        assert len(first) == 1
        assert len(second) == 0

        # Only 1 row in the DB
        count = db.execute("SELECT COUNT(*) FROM candidate_jobs").fetchone()[0]
        assert count == 1

    def test_search_skips_rows_without_url(self, db):
        from job_discovery import search_jobs

        bad_job = dict(_SAMPLE_JOB, job_url="", site="indeed")
        mock_df = _make_jobs_df([bad_job])

        with patch("job_discovery.scrape_jobs", return_value=mock_df):
            results = search_jobs(db, "PM", "SF")

        assert len(results) == 0

    def test_search_uses_custom_sources(self, db):
        from job_discovery import search_jobs

        mock_df = _make_jobs_df([])

        with patch("job_discovery.scrape_jobs", return_value=mock_df) as mock_scrape:
            search_jobs(db, "PM", "SF", sources=["indeed"])

        call_kwargs = mock_scrape.call_args
        assert "indeed" in call_kwargs[1].get("site_name", call_kwargs[0][0] if call_kwargs[0] else [])


# ---------------------------------------------------------------------------
# TestJobScoring
# ---------------------------------------------------------------------------


class TestJobScoring:
    def _insert_candidate(self, db, url: str = "https://example.com/job/1") -> int:
        db.execute(
            "INSERT INTO candidate_jobs (source, title, company, job_url, job_description) "
            "VALUES ('linkedin', 'Senior PM', 'Acme Corp', ?, 'Great job opportunity')",
            (url,),
        )
        db.commit()
        return db.execute("SELECT id FROM candidate_jobs WHERE job_url = ?", (url,)).fetchone()[0]

    def test_score_candidate_updates_db(self, db):
        from job_discovery import score_candidate

        cid = self._insert_candidate(db)

        mock_response = {"score": 85, "reason": "Strong PM background matches requirements."}

        with patch("job_discovery._call_llm_score", return_value=mock_response):
            result = score_candidate(db, cid)

        assert result["score"] == 85
        assert result["status"] == "scored"
        assert result["scored_at"] is not None
        assert "PM" in result["score_reason"]

    def test_score_candidate_handles_llm_error(self, db):
        from job_discovery import score_candidate

        cid = self._insert_candidate(db)

        with patch("job_discovery._call_llm_score", side_effect=ValueError("LLM failed")):
            result = score_candidate(db, cid)

        # Should still update status and set score to 0
        assert result["status"] == "scored"
        assert result["score"] == 0

    def test_score_unscored_candidates(self, db):
        from job_discovery import score_unscored_candidates

        # Insert 3 candidates
        for i in range(3):
            db.execute(
                "INSERT INTO candidate_jobs (source, title, company, job_url) VALUES ('linkedin', 'PM', 'Acme', ?)",
                (f"https://example.com/job/{i}",),
            )
        db.commit()

        mock_response = {"score": 70, "reason": "Good fit."}

        with patch("job_discovery._call_llm_score", return_value=mock_response):
            count = score_unscored_candidates(db)

        assert count == 3

        # All should be scored now
        scored = db.execute("SELECT COUNT(*) FROM candidate_jobs WHERE status = 'scored'").fetchone()[0]
        assert scored == 3

    def test_score_unscored_skips_already_scored(self, db):
        from job_discovery import score_unscored_candidates

        # Insert one new and one already-scored candidate
        db.execute(
            "INSERT INTO candidate_jobs (source, title, company, job_url, status, score) "
            "VALUES ('linkedin', 'PM', 'Acme', 'https://example.com/job/0', 'scored', 80)"
        )
        db.execute(
            "INSERT INTO candidate_jobs (source, title, company, job_url) "
            "VALUES ('linkedin', 'PM', 'Acme', 'https://example.com/job/1')"
        )
        db.commit()

        mock_response = {"score": 70, "reason": "Good fit."}

        with patch("job_discovery._call_llm_score", return_value=mock_response) as mock_llm:
            count = score_unscored_candidates(db)

        # Only 1 new candidate was scored
        assert count == 1
        assert mock_llm.call_count == 1


# ---------------------------------------------------------------------------
# TestPromotion
# ---------------------------------------------------------------------------


class TestPromotion:
    def _insert_candidate(
        self,
        db,
        url: str = "https://jobs.lever.co/acme/abc123",
        application_url: str | None = None,
    ) -> int:
        db.execute(
            "INSERT INTO candidate_jobs "
            "(source, title, company, job_url, application_url, status, score) "
            "VALUES ('linkedin', 'Senior PM', 'Acme Corp', ?, ?, 'scored', 85)",
            (url, application_url),
        )
        db.commit()
        return db.execute("SELECT id FROM candidate_jobs WHERE job_url = ?", (url,)).fetchone()[0]

    def test_promote_to_queue(self, db):
        from job_discovery import promote_candidate

        cid = self._insert_candidate(db)
        job_id = promote_candidate(db, cid)

        assert job_id is not None
        assert isinstance(job_id, int)

        # Verify candidate is marked as promoted
        candidate = dict(db.execute("SELECT * FROM candidate_jobs WHERE id = ?", (cid,)).fetchone())
        assert candidate["status"] == "promoted"
        assert candidate["promoted_job_id"] == job_id
        assert candidate["promoted_at"] is not None

    def test_promote_uses_application_url_when_available(self, db):
        from job_discovery import promote_candidate

        app_url = "https://jobs.lever.co/acme/abc123/apply"
        cid = self._insert_candidate(
            db,
            url="https://linkedin.com/jobs/view/12345",
            application_url=app_url,
        )
        job_id = promote_candidate(db, cid)

        assert job_id is not None
        job = dict(db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
        # The URL should be the application_url (or its canonical form)
        assert job["url"] == app_url or job["source_url"] == app_url or job["board_url"] == app_url

    def test_already_promoted_returns_none(self, db):
        from job_discovery import promote_candidate

        cid = self._insert_candidate(db)
        first_job_id = promote_candidate(db, cid)
        assert first_job_id is not None

        # Promote again — should return None
        result = promote_candidate(db, cid)
        assert result is None

    def test_promote_duplicate_url_returns_none(self, db):
        from job_db import add_job
        from job_discovery import promote_candidate

        url = "https://jobs.lever.co/acme/dup123"
        # Pre-insert this URL into the jobs table
        add_job(db, url)

        # Now try to promote a candidate with the same URL
        db.execute(
            "INSERT INTO candidate_jobs (source, title, company, job_url) VALUES ('linkedin', 'PM', 'Acme', ?)",
            (url,),
        )
        db.commit()
        cid = db.execute("SELECT id FROM candidate_jobs WHERE job_url = ?", (url,)).fetchone()[0]

        result = promote_candidate(db, cid)
        assert result is None

    def test_promote_duplicate_company_role_skips_candidate_and_links_existing_job(self, db):
        from job_db import add_job
        from job_discovery import promote_candidate

        existing_job_id = add_job(
            db,
            "https://jobs.ashbyhq.com/valon/0f1fbd7d-e30d-4b8c-9e3d-04ff6bfd7638",
            company="Valon Tech",
            role_title="senior-pm-product-infrastructure",
        )
        db.execute(
            """
            INSERT INTO candidate_jobs (source, title, company, job_url, status, score)
            VALUES ('linkedin', ?, ?, ?, 'scored', 91)
            """,
            (
                "Valon hiring Senior Product Manager, Product Infrastructure in San Francisco, CA | LinkedIn",
                "Valon",
                "https://www.linkedin.com/jobs/view/4366508877/",
            ),
        )
        db.commit()
        cid = db.execute("SELECT id FROM candidate_jobs ORDER BY id DESC LIMIT 1").fetchone()[0]

        result = promote_candidate(db, cid)

        assert result is None
        candidate = dict(db.execute("SELECT * FROM candidate_jobs WHERE id = ?", (cid,)).fetchone())
        assert candidate["status"] == "skipped"
        assert candidate["promoted_job_id"] == existing_job_id

    def test_promote_duplicate_jd_skips_candidate_and_links_existing_job(self, db):
        from job_db import add_job, set_jd_fingerprint
        from job_discovery import promote_candidate

        jd = "Lead roadmap for a platform product across billing and analytics. " * 5
        existing_job_id = add_job(
            db,
            "https://boards.greenhouse.io/acme/jobs/dup-jd-existing",
            company="Acme",
            role_title="Platform Product Manager",
        )
        set_jd_fingerprint(db, existing_job_id, "Acme", jd)
        db.execute(
            """
            INSERT INTO candidate_jobs (source, title, company, job_url, job_description, status, score)
            VALUES ('linkedin', ?, ?, ?, ?, 'scored', 91)
            """,
            (
                "Principal Product Manager",
                "Acme",
                "https://www.linkedin.com/jobs/view/duplicate-jd",
                jd,
            ),
        )
        db.commit()
        cid = db.execute("SELECT id FROM candidate_jobs ORDER BY id DESC LIMIT 1").fetchone()[0]

        result = promote_candidate(db, cid)

        assert result is None
        candidate = dict(db.execute("SELECT * FROM candidate_jobs WHERE id = ?", (cid,)).fetchone())
        assert candidate["status"] == "skipped"
        assert candidate["promoted_job_id"] == existing_job_id


# ---------------------------------------------------------------------------
# TestListAndFilter
# ---------------------------------------------------------------------------


class TestListAndFilter:
    def _insert_candidates(self, db) -> None:
        rows = [
            ("linkedin", "Senior PM", "Acme", "https://example.com/1", "new", 90),
            ("indeed", "Staff PM", "Beta", "https://example.com/2", "scored", 75),
            ("glassdoor", "PM", "Gamma", "https://example.com/3", "scored", 50),
            ("linkedin", "Lead PM", "Delta", "https://example.com/4", "skipped", None),
        ]
        for source, title, company, url, status, score in rows:
            db.execute(
                "INSERT INTO candidate_jobs (source, title, company, job_url, status, score) VALUES (?, ?, ?, ?, ?, ?)",
                (source, title, company, url, status, score),
            )
        db.commit()

    def test_list_all(self, db):
        from job_discovery import list_candidates

        self._insert_candidates(db)
        results = list_candidates(db)
        assert len(results) == 4

    def test_list_by_status(self, db):
        from job_discovery import list_candidates

        self._insert_candidates(db)
        results = list_candidates(db, status="scored")
        assert len(results) == 2
        assert all(r["status"] == "scored" for r in results)

    def test_list_by_source(self, db):
        from job_discovery import list_candidates

        self._insert_candidates(db)
        results = list_candidates(db, source="linkedin")
        assert len(results) == 2
        assert all(r["source"] == "linkedin" for r in results)

    def test_list_by_search(self, db):
        from job_discovery import list_candidates

        self._insert_candidates(db)
        results = list_candidates(db, search="Staff")
        assert len(results) == 1
        assert results[0]["title"] == "Staff PM"

    def test_list_sorted_by_score_desc(self, db):
        from job_discovery import list_candidates

        self._insert_candidates(db)
        results = list_candidates(db, status="scored")
        scores = [r["score"] for r in results if r["score"] is not None]
        assert scores == sorted(scores, reverse=True)

    def test_list_with_limit_offset(self, db):
        from job_discovery import list_candidates

        self._insert_candidates(db)
        page1 = list_candidates(db, limit=2, offset=0)
        page2 = list_candidates(db, limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert {r["id"] for r in page1}.isdisjoint({r["id"] for r in page2})

    def test_skip_candidate(self, db):
        from job_discovery import list_candidates, skip_candidate

        self._insert_candidates(db)
        # Get a non-skipped candidate
        results = list_candidates(db, status="new")
        assert len(results) == 1
        cid = results[0]["id"]

        skip_candidate(db, cid)

        updated = dict(db.execute("SELECT * FROM candidate_jobs WHERE id = ?", (cid,)).fetchone())
        assert updated["status"] == "skipped"

    def test_delete_candidate(self, db):
        from job_discovery import delete_candidate

        self._insert_candidates(db)
        total_before = db.execute("SELECT COUNT(*) FROM candidate_jobs").fetchone()[0]

        cid = db.execute("SELECT id FROM candidate_jobs LIMIT 1").fetchone()[0]
        delete_candidate(db, cid)

        total_after = db.execute("SELECT COUNT(*) FROM candidate_jobs").fetchone()[0]
        assert total_after == total_before - 1

        gone = db.execute("SELECT * FROM candidate_jobs WHERE id = ?", (cid,)).fetchone()
        assert gone is None

    def test_get_candidate_stats(self, db):
        from job_discovery import get_candidate_stats

        self._insert_candidates(db)
        stats = get_candidate_stats(db)

        assert isinstance(stats, dict)
        assert stats.get("new") == 1
        assert stats.get("scored") == 2
        assert stats.get("skipped") == 1


# ---------------------------------------------------------------------------
# End-to-end flow
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """Full flow: search → score → promote → verify job in queue."""

    def test_full_discovery_to_draft_flow(self, db):
        import pandas as pd
        from job_discovery import (
            list_candidates,
            promote_candidate,
            score_candidate,
            search_jobs,
        )

        # 1. Search (mocked)
        mock_df = pd.DataFrame(
            [
                {
                    "site": "linkedin",
                    "title": "Staff PM",
                    "company": "CoolStartup",
                    "job_url": "https://linkedin.com/jobs/e2e-test",
                    "location": "San Francisco",
                    "description": "AI product manager with ML experience",
                    "min_amount": 200000,
                    "max_amount": 250000,
                    "currency": "USD",
                    "interval": "YEARLY",
                }
            ]
        )
        with patch("job_discovery.scrape_jobs", return_value=mock_df):
            results = search_jobs(db, "product manager", "San Francisco", 10)
        assert len(results) == 1
        cid = results[0]["id"]

        # 2. Score
        mock_score = {"score": 92, "reason": "Excellent PM+AI fit"}
        with patch("job_discovery._call_llm_score", return_value=mock_score):
            score = score_candidate(db, cid)
        assert score["score"] == 92

        # 3. List (should show scored)
        candidates = list_candidates(db, status="scored")
        assert len(candidates) == 1
        assert candidates[0]["score"] == 92

        # 4. Promote
        job_id = promote_candidate(db, cid)
        assert job_id is not None

        # 5. Verify in queue
        job = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert job is not None
        assert job["status"] == "queued"

        # 6. Candidate marked as promoted
        candidate = db.execute("SELECT * FROM candidate_jobs WHERE id = ?", (cid,)).fetchone()
        assert candidate["status"] == "promoted"
        assert candidate["promoted_job_id"] == job_id
