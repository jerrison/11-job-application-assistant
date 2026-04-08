import importlib.util
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class NotionSyncTests(unittest.TestCase):
    def test_job_url_identity_matches_greenhouse_embed_and_canonical_urls(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        canonical = "https://job-boards.greenhouse.io/amplitude/jobs/8457963002"
        embed = "https://boards.greenhouse.io/embed/job_app?token=8457963002&gh_src=ab9f35b82"
        self.assertEqual(notion_sync._job_url_identity(canonical), notion_sync._job_url_identity(embed))

    def test_job_url_identity_matches_ashby_wrapper_and_canonical_urls(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        canonical = "https://jobs.ashbyhq.com/standinsurance/0f8f1869-bbd5-4d7a-bd48-5b30b258b5f7"
        wrapper = (
            "https://www.standinsurance.com/careers?"
            "ashby_jid=0f8f1869-bbd5-4d7a-bd48-5b30b258b5f7&utm_source=QNwjOoM9DP"
        )
        self.assertEqual(notion_sync._job_url_identity(canonical), notion_sync._job_url_identity(wrapper))

    def test_job_url_identity_matches_lever_posting_and_apply_urls(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        posting = "https://jobs.lever.co/weride/47eb9bfe-6b36-4543-9039-f315f26c9b1e/"
        apply = "https://jobs.lever.co/weride/47eb9bfe-6b36-4543-9039-f315f26c9b1e/apply"
        self.assertEqual(notion_sync._job_url_identity(posting), notion_sync._job_url_identity(apply))

    def test_job_url_identity_matches_dover_apply_urls_despite_tracking_query(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        canonical = "https://app.dover.com/apply/suppli/ea0d0b85-3700-407e-9f59-6ddcc6b9cfb8"
        tracked = (
            "https://app.dover.com/apply/suppli/ea0d0b85-3700-407e-9f59-6ddcc6b9cfb8?rs=42706078&utm_source=LinkedIn"
        )
        self.assertEqual(notion_sync._job_url_identity(canonical), notion_sync._job_url_identity(tracked))

    def test_candidate_page_titles_prefers_cached_html_title(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            (out_dir / "submit" / "greenhouse_application_page.html").write_text(
                "<html><head><title>Job Application for Product Manager, Design Tools at Figma </title></head></html>",
                encoding="utf-8",
            )
            titles = notion_sync._candidate_page_titles(
                {"jd_title": "Product Manager, Design Tools", "company_proper": "Figma"},
                out_dir,
            )
        self.assertEqual(titles[0], "Job Application for Product Manager, Design Tools | Figma")

    def test_page_properties_use_live_notion_field_names(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        schema = {
            "properties": {
                "Name": {"type": "title"},
                "Status": {"type": "status"},
                "URL": {"type": "url"},
                "Position": {"type": "rich_text"},
                "Application Date": {"type": "date"},
                "Notes": {"type": "rich_text"},
                "Job Type": {"type": "select"},
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "content").mkdir()
            (out_dir / "content" / "jd_parsed.json").write_text(
                json.dumps({"keywords": ["technical"]}),
                encoding="utf-8",
            )
            properties = notion_sync._page_properties(
                schema,
                meta={
                    "jd_title": "Senior Technical Product Manager",
                    "company_proper": "BackOps AI",
                    "jd_source": "https://jobs.gem.com/backops-ai/am9icG9zdDqvIJK0_QtZ-fZgDog6VG8z?utm_source=jackandjill",
                },
                out_dir=out_dir,
                website_confirmation={
                    "confirmed_at_utc": "2026-03-12T22:00:00+00:00",
                    "url": "https://example.com/confirmation",
                },
                email_confirmation={"subject": "Thanks for applying", "date": "Thu, 12 Mar 2026 15:00:00 -0700"},
            )
        self.assertEqual(properties["Status"]["status"]["name"], "Applied")
        self.assertEqual(
            properties["URL"]["url"],
            "https://jobs.gem.com/backops-ai/am9icG9zdDqvIJK0_QtZ-fZgDog6VG8z?utm_source=jackandjill",
        )
        self.assertEqual(properties["Position"]["rich_text"][0]["text"]["content"], "Senior Technical Product Manager")
        self.assertEqual(properties["Job Type"]["select"]["name"], "Technical PM")

    def test_page_properties_preserve_existing_application_date_for_existing_page(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        schema = {
            "properties": {
                "Name": {"type": "title"},
                "Status": {"type": "status"},
                "Application Date": {"type": "date"},
                "Notes": {"type": "rich_text"},
                "Position": {"type": "rich_text"},
            }
        }
        existing_page = {
            "properties": {
                "Application Date": {"type": "date", "date": {"start": "2026-03-18T17:11:18+00:00"}},
                "Notes": {"type": "rich_text", "rich_text": [{"plain_text": "Applied via automation."}]},
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "content").mkdir()
            (out_dir / "content" / "jd_parsed.json").write_text(json.dumps({}), encoding="utf-8")
            properties = notion_sync._page_properties(
                schema,
                meta={"jd_title": "Senior Product Manager", "company_proper": "Valon Tech"},
                out_dir=out_dir,
                website_confirmation={
                    "confirmed_at_utc": "2026-03-30T04:00:00+00:00",
                    "url": "https://example.com/confirmation",
                },
                email_confirmation=None,
                existing_page=existing_page,
                submission_history={
                    "confirmed_at": "2026-03-18T17:11:18+00:00",
                    "last_resubmit_unlocked_at": "2026-03-30T03:45:00+00:00",
                    "last_resubmit_confirmed_at": "2026-03-30T04:00:00+00:00",
                    "resubmit_count": 1,
                },
            )

        self.assertEqual(properties["Application Date"]["date"]["start"], "2026-03-18T17:11:18+00:00")

    def test_load_submission_history_for_output_migrates_legacy_jobs_db(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "jobs.db"
            raw = sqlite3.connect(db_path)
            raw.executescript(
                """
                CREATE TABLE jobs (
                    id INTEGER PRIMARY KEY,
                    output_dir TEXT,
                    confirmed_at TIMESTAMP
                );
                INSERT INTO jobs (id, output_dir, confirmed_at)
                VALUES (1, '/tmp/output/valon', '2026-03-18T17:11:18+00:00');
                """
            )
            raw.commit()
            raw.close()

            with mock.patch.object(notion_sync, "PROJECT_ROOT", root):
                history = notion_sync._load_submission_history_for_output(Path("/tmp/output/valon"))

        self.assertEqual(history["confirmed_at"], "2026-03-18T17:11:18+00:00")
        self.assertIn("resubmit_count", history)

    def test_notes_text_includes_resubmission_history_lines(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        notes = notion_sync._notes_text(
            Path("/tmp/valon"),
            {"confirmed_at_utc": "2026-03-30T04:00:00+00:00", "url": "https://example.com/confirmation"},
            {"subject": "Thanks for applying", "date": "Mon, 30 Mar 2026 04:01:00 +0000"},
            submission_history={
                "confirmed_at": "2026-03-18T17:11:18+00:00",
                "last_resubmit_unlocked_at": "2026-03-30T03:45:00+00:00",
                "last_resubmit_confirmed_at": "2026-03-30T04:00:00+00:00",
                "resubmit_count": 1,
            },
            existing_notes="Applied via automation.",
        )

        self.assertIn("Originally applied: 2026-03-18T17:11:18+00:00", notes)
        self.assertIn("Unlocked for resubmit: 2026-03-30T03:45:00+00:00", notes)
        self.assertIn("Resubmit count: 1", notes)

    def test_notes_text_replaces_generated_lines_from_existing_notes(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        notes = notion_sync._notes_text(
            Path("/tmp/valon"),
            {"confirmed_at_utc": "2026-03-30T04:00:00+00:00", "url": "https://example.com/confirmation"},
            {"subject": "Thanks for applying", "date": "Mon, 30 Mar 2026 04:01:00 +0000"},
            submission_history={
                "confirmed_at": "2026-03-18T17:11:18+00:00",
                "last_resubmit_unlocked_at": "2026-03-30T03:45:00+00:00",
                "last_resubmit_confirmed_at": "2026-03-30T04:00:00+00:00",
                "resubmit_count": 1,
            },
            existing_notes=(
                "Manual note.\n"
                "Website confirmed at: 2026-03-18T17:11:18+00:00\n"
                "Latest resubmitted at: 2026-03-29T20:00:00+00:00"
            ),
        )

        self.assertIn("Manual note.", notes)
        self.assertIn("Website confirmed at: 2026-03-30T04:00:00+00:00", notes)
        self.assertIn("Latest resubmitted at: 2026-03-30T04:00:00+00:00", notes)
        self.assertNotIn("Website confirmed at: 2026-03-18T17:11:18+00:00", notes)
        self.assertNotIn("Latest resubmitted at: 2026-03-29T20:00:00+00:00", notes)

    def test_notes_text_skips_missing_latest_resubmit_timestamp(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        notes = notion_sync._notes_text(
            Path("/tmp/valon"),
            {"confirmed_at_utc": "2026-03-30T04:00:00+00:00", "url": "https://example.com/confirmation"},
            None,
            submission_history={
                "confirmed_at": "2026-03-18T17:11:18+00:00",
                "last_resubmit_unlocked_at": "2026-03-30T03:45:00+00:00",
                "last_resubmit_confirmed_at": None,
                "resubmit_count": 1,
            },
        )

        self.assertIn("Originally applied: 2026-03-18T17:11:18+00:00", notes)
        self.assertIn("Unlocked for resubmit: 2026-03-30T03:45:00+00:00", notes)
        self.assertIn("Resubmit count: 1", notes)
        self.assertNotIn("Latest resubmitted at: None", notes)

    def test_resubmission_history_blocks_skip_missing_latest_resubmit_timestamp(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        blocks = notion_sync._resubmission_history_blocks(
            {
                "confirmed_at": "2026-03-18T17:11:18+00:00",
                "last_resubmit_unlocked_at": "2026-03-30T03:45:00+00:00",
                "last_resubmit_confirmed_at": None,
                "resubmit_count": 1,
            }
        )

        encoded = json.dumps(blocks)
        self.assertIn("Originally applied: 2026-03-18T17:11:18+00:00", encoded)
        self.assertIn("Unlocked for resubmit: 2026-03-30T03:45:00+00:00", encoded)
        self.assertIn("Resubmit count: 1", encoded)
        self.assertNotIn("Latest resubmitted at: None", encoded)

    def test_resubmission_history_marker_falls_back_to_unlock_time(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        marker = notion_sync._resubmission_history_marker(
            {
                "confirmed_at": "2026-03-18T17:11:18+00:00",
                "last_resubmit_unlocked_at": "2026-03-30T03:45:00+00:00",
                "last_resubmit_confirmed_at": None,
                "resubmit_count": 1,
            }
        )

        self.assertEqual(marker, "2026-03-30T03:45:00+00:00")

    def test_sync_resubmission_history_blocks_replaces_existing_section_for_new_marker(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")

        def heading(block_id: str, text: str) -> dict:
            return {
                "id": block_id,
                "type": "heading_2",
                "heading_2": {"rich_text": [{"plain_text": text, "type": "text", "text": {"content": text}}]},
            }

        def bullet(block_id: str, text: str) -> dict:
            return {
                "id": block_id,
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"plain_text": text, "type": "text", "text": {"content": text}}]},
            }

        class FakeClient:
            def __init__(self) -> None:
                self.archived: list[str] = []
                self.appended: list[tuple[str, list[dict]]] = []

            def archive_block(self, block_id: str) -> None:
                self.archived.append(block_id)

            def append_block_children(self, block_id: str, children: list[dict]) -> None:
                self.appended.append((block_id, children))

        client = FakeClient()
        existing_blocks = [
            heading("heading-1", "Application Metadata"),
            bullet("bullet-1", "Website confirmed: 2026-03-18T17:11:18+00:00"),
            heading("history-heading", "Resubmission History"),
            bullet("history-original", "Originally applied: 2026-03-18T17:11:18+00:00"),
            bullet("history-latest", "Latest resubmitted at: 2026-03-29T20:00:00+00:00"),
        ]

        changed = notion_sync._sync_resubmission_history_blocks(
            client,
            "page-1",
            existing_blocks,
            {
                "confirmed_at": "2026-03-18T17:11:18+00:00",
                "last_resubmit_unlocked_at": "2026-03-30T03:45:00+00:00",
                "last_resubmit_confirmed_at": "2026-03-30T04:00:00+00:00",
                "resubmit_count": 2,
            },
        )

        self.assertTrue(changed)
        self.assertEqual(client.archived, ["history-heading", "history-original", "history-latest"])
        self.assertEqual(len(client.appended), 1)
        self.assertEqual(client.appended[0][0], "page-1")
        self.assertIn("Latest resubmitted at: 2026-03-30T04:00:00+00:00", json.dumps(client.appended[0][1]))

    def test_sync_resubmission_history_blocks_skips_when_latest_marker_is_present(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")

        def heading(block_id: str, text: str) -> dict:
            return {
                "id": block_id,
                "type": "heading_2",
                "heading_2": {"rich_text": [{"plain_text": text, "type": "text", "text": {"content": text}}]},
            }

        def bullet(block_id: str, text: str) -> dict:
            return {
                "id": block_id,
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"plain_text": text, "type": "text", "text": {"content": text}}]},
            }

        class FakeClient:
            def __init__(self) -> None:
                self.archived: list[str] = []
                self.appended: list[tuple[str, list[dict]]] = []

            def archive_block(self, block_id: str) -> None:
                self.archived.append(block_id)

            def append_block_children(self, block_id: str, children: list[dict]) -> None:
                self.appended.append((block_id, children))

        client = FakeClient()
        existing_blocks = [
            heading("history-heading", "Resubmission History"),
            bullet("history-original", "Originally applied: 2026-03-18T17:11:18+00:00"),
            bullet("history-latest", "Latest resubmitted at: 2026-03-30T04:00:00+00:00"),
        ]

        changed = notion_sync._sync_resubmission_history_blocks(
            client,
            "page-1",
            existing_blocks,
            {
                "confirmed_at": "2026-03-18T17:11:18+00:00",
                "last_resubmit_unlocked_at": "2026-03-30T03:45:00+00:00",
                "last_resubmit_confirmed_at": "2026-03-30T04:00:00+00:00",
                "resubmit_count": 1,
            },
        )

        self.assertFalse(changed)
        self.assertEqual(client.archived, [])
        self.assertEqual(client.appended, [])

    def test_match_existing_page_uses_url_identity(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        schema = {
            "properties": {
                "Name": {"type": "title"},
                "URL": {"type": "url"},
            }
        }
        pages = [
            {
                "id": "page-1",
                "properties": {
                    "Name": {
                        "type": "title",
                        "title": [
                            {
                                "plain_text": "Application for Principal Product Manager, AI - People & Places | Amplitude"
                            }
                        ],
                    },
                    "URL": {
                        "type": "url",
                        "url": "https://boards.greenhouse.io/embed/job_app?token=8457963002&gh_src=ab9f35b82",
                    },
                },
            }
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            match = notion_sync._match_existing_page(
                pages,
                schema=schema,
                meta={
                    "jd_title": "Principal Product Manager, AI - People & Places",
                    "company_proper": "Amplitude",
                    "jd_source": "https://job-boards.greenhouse.io/amplitude/jobs/8457963002",
                },
                out_dir=out_dir,
            )
        self.assertIsNotNone(match)
        self.assertEqual(match["id"], "page-1")

    def test_match_existing_page_ignores_job_only_matches_without_company_or_title(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        schema = {
            "properties": {
                "Name": {"type": "title"},
                "URL": {"type": "url"},
            }
        }
        pages = [
            {
                "id": "page-1",
                "properties": {
                    "Name": {"type": "title", "title": [{"plain_text": "Principal Product Manager, AI | Hover"}]},
                    "URL": {"type": "url", "url": "https://job-boards.greenhouse.io/hover/jobs/7671291"},
                },
            }
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            match = notion_sync._match_existing_page(
                pages,
                schema=schema,
                meta={
                    "jd_title": "Product Manager, AI",
                    "company_proper": "Figma",
                    "jd_source": "https://job-boards.greenhouse.io/figma/jobs/5574247004",
                },
                out_dir=out_dir,
            )
        self.assertIsNone(match)

    def test_match_existing_page_ignores_company_only_matches_for_different_roles(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        schema = {
            "properties": {
                "Name": {"type": "title"},
                "URL": {"type": "url"},
            }
        }
        pages = [
            {
                "id": "page-1",
                "properties": {
                    "Name": {
                        "type": "title",
                        "title": [{"plain_text": "Job Application for Principal Product Manager, Consumer Innovation | Instacart"}],
                    },
                    "URL": {"type": "url", "url": "https://instacart.careers/job/?gh_jid=7561999&gh_src=26e143c51"},
                },
            }
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            match = notion_sync._match_existing_page(
                pages,
                schema=schema,
                meta={
                    "jd_title": "Senior Product Manager, Enterprise Picking & Tools",
                    "company_proper": "Instacart",
                    "jd_source": "https://instacart.careers/job/?gh_jid=7744296&gh_src=26e143c51",
                },
                out_dir=out_dir,
            )
        self.assertIsNone(match)

    def test_list_block_children_paginates_all_results(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        client = notion_sync.NotionClient(
            token="token",
            data_source_id="data-source",
            database_id="database",
            notion_version="2026-03-11",
        )
        first_page = {
            "results": [{"id": f"block-{index}"} for index in range(20)],
            "has_more": True,
            "next_cursor": "cursor-2",
        }
        second_page = {"results": [{"id": "block-20"}], "has_more": False, "next_cursor": None}

        with mock.patch.object(client, "_request", side_effect=[first_page, second_page]) as request:
            blocks = client.list_block_children("page-1")

        self.assertEqual([block["id"] for block in blocks], [f"block-{index}" for index in range(21)])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].args, ("GET", "/blocks/page-1/children?page_size=20"))
        self.assertEqual(
            request.call_args_list[1].args,
            ("GET", "/blocks/page-1/children?page_size=20&start_cursor=cursor-2"),
        )

    def test_record_website_confirmation_writes_compatibility_artifacts(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            notion_sync.record_website_confirmation(
                out_dir,
                {
                    "status": "confirmed",
                    "confirmed_at_utc": "2026-04-02T06:05:40+00:00",
                    "reason": "url",
                    "page_excerpt": "Recovered local post-submit state from Gmail confirmation evidence.",
                    "snapshot": {"url": "https://example.com/confirmation"},
                },
                provider="greenhouse",
            )
            generic_payload = json.loads(
                (out_dir / "submit" / notion_sync.SUBMISSION_RESULT_JSON).read_text(encoding="utf-8")
            )
            website_payload = json.loads(
                (out_dir / "submit" / notion_sync.WEBSITE_CONFIRMATION_JSON).read_text(encoding="utf-8")
            )
        self.assertTrue(generic_payload["website_confirmed"])
        self.assertEqual(generic_payload["provider"], "greenhouse")
        self.assertEqual(generic_payload["confirmed_at_utc"], "2026-04-02T06:05:40+00:00")
        self.assertEqual(
            generic_payload["page_excerpt"],
            "Recovered local post-submit state from Gmail confirmation evidence.",
        )
        self.assertEqual(website_payload["reason"], "url")

    def test_record_website_confirmation_clears_active_reapply_pointer_after_confirmed_submit(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        layout = load_module("output_layout", "scripts/output_layout.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            reapply_dir = layout.create_reapply_submit_dir(out_dir)

            notion_sync.record_website_confirmation(
                out_dir,
                {
                    "status": "confirmed",
                    "reason": "url",
                    "snapshot": {"url": "https://example.com/confirmation", "page_text": "Thank you for applying"},
                },
                provider="greenhouse",
            )

            self.assertFalse((out_dir / layout.ACTIVE_SUBMIT_DIR_POINTER).exists())
            self.assertTrue((reapply_dir / notion_sync.SUBMISSION_RESULT_JSON).exists())

    def test_website_confirmation_prefers_latest_confirmed_reapply_attempt(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            default_submit = out_dir / "submit"
            default_submit.mkdir()
            (default_submit / notion_sync.SUBMISSION_RESULT_JSON).write_text(
                json.dumps({"website_confirmed": True, "confirmed_at_utc": "2026-03-13T16:00:00+00:00"}),
                encoding="utf-8",
            )
            latest_submit = out_dir / "submit-20260313T171928Z"
            latest_submit.mkdir()
            (latest_submit / notion_sync.SUBMISSION_RESULT_JSON).write_text(
                json.dumps({"website_confirmed": True, "confirmed_at_utc": "2026-03-13T17:19:28+00:00"}),
                encoding="utf-8",
            )
            os.utime(default_submit, (1, 1))
            os.utime(latest_submit, None)

            payload = notion_sync._website_confirmation(out_dir)

        self.assertEqual(payload["confirmed_at_utc"], "2026-03-13T17:19:28+00:00")

    def test_email_confirmation_score_rejects_unrelated_generic_application_email(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        message = {
            "snippet": "WATI.io Your application for the Staff/ Lead Product Manager job was submitted successfully.",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Thanks for applying to WATI.io"},
                    {"name": "From", "value": "Workable <noreply@candidates.workablemail.com>"},
                ]
            },
        }

        score = notion_sync._email_confirmation_score(
            message,
            company="Vercel",
            job_title="Product Manager - Agent Platform",
            url_identity="greenhouse:5808590004",
        )

        self.assertEqual(score, 0)

    def test_email_confirmation_score_accepts_company_matched_confirmation(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        message = {
            "snippet": "Thank you for applying to CoreWeave. Your application has been received.",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Thank you for applying to CoreWeave"},
                    {"name": "From", "value": "no-reply@us.greenhouse-mail.io"},
                ]
            },
        }

        score = notion_sync._email_confirmation_score(
            message,
            company="CoreWeave",
            job_title="Staff AI & Agents Growth Product Manager",
            url_identity="greenhouse:4638816006",
        )

        self.assertGreaterEqual(score, 75)

    def test_email_confirmation_score_accepts_html_escaped_we_received_variant(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        message = {
            "snippet": (
                "Hi Candidate, Thanks so much for applying to roles here at Reflection. "
                "We&#39;ve received your application,"
            ),
            "payload": {"mimeType": "multipart/alternative"},
        }

        score = notion_sync._email_confirmation_score(
            message,
            company="Reflection",
            job_title="Don't See Your Role? Apply Here!",
            url_identity="ashby:bb02e922-8df8-4641-9aab-910997f76392",
        )

        self.assertGreaterEqual(score, 75)

    def test_email_confirmation_score_rejects_provider_only_confirmation_for_other_company(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        message = {
            "snippet": (
                "Hi Candidate, Thank you for your interest in Handshake! "
                "We have received your application for the Senior Product Manager role."
            ),
            "payload": {"mimeType": "multipart/alternative"},
        }

        score = notion_sync._email_confirmation_score(
            message,
            company="Reflection",
            job_title="Don't See Your Role? Apply Here!",
            url_identity="ashby:bb02e922-8df8-4641-9aab-910997f76392",
        )

        self.assertEqual(score, 0)

    def test_email_confirmation_query_specs_scope_by_company_and_submit_date(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")

        specs = notion_sync._email_confirmation_query_specs(
            {
                "company_proper": "Mandolin",
                "jd_title": "Senior Product Manager",
                "jd_source": "https://jobs.ashbyhq.com/mandolin/84421bfb-dc71-4838-a518-f5b209d69d9a?src=LinkedIn",
            },
            min_received_at_utc="2026-03-13T11:45:00+00:00",
        )

        self.assertGreaterEqual(len(specs), 3)
        self.assertIn("after:2026/03/13", specs[0]["q"])
        self.assertIn("-in:sent", specs[0]["q"])
        self.assertIn('"Mandolin"', specs[0]["q"])
        self.assertIn("ashby", " ".join(spec["q"] for spec in specs))

    def test_find_email_confirmation_uses_targeted_queries_and_metadata_fetches(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        calls: list[list[str]] = []

        def fake_run_gws_json(args):
            calls.append(args)
            params = json.loads(args[-1])
            if args[:4] == ["gmail", "users", "messages", "list"]:
                query = params["q"]
                if '"Mandolin"' in query and '"thank you for applying"' in query:
                    return {"messages": [{"id": "m1"}, {"id": "m1"}]}
                if '"Mandolin"' in query and "application" in query:
                    return {"messages": [{"id": "m2"}]}
                return {"messages": []}
            if args[:4] == ["gmail", "users", "messages", "get"]:
                return {
                    "id": params["id"],
                    "threadId": f"thread-{params['id']}",
                    "internalDate": "1773402305000",
                    "snippet": "Thank you for applying to Mandolin. Your application has been received.",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Thank you for applying to Mandolin"},
                            {"name": "From", "value": "Mandolin via Ashby <jobs@ashbyhq.com>"},
                            {"name": "Date", "value": "Fri, 13 Mar 2026 04:45:05 -0700"},
                        ]
                    },
                }
            raise AssertionError(f"Unexpected gws args: {args}")

        with mock.patch.object(notion_sync, "_run_gws_json", side_effect=fake_run_gws_json):
            match = notion_sync._find_email_confirmation(
                {
                    "company_proper": "Mandolin",
                    "jd_title": "Senior Product Manager",
                    "jd_source": "https://jobs.ashbyhq.com/mandolin/84421bfb-dc71-4838-a518-f5b209d69d9a?src=LinkedIn",
                },
                min_received_at_utc="2026-03-13T11:45:00+00:00",
            )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["subject"], "Thank you for applying to Mandolin")
        list_queries = [
            json.loads(call[-1])["q"] for call in calls if call[:4] == ["gmail", "users", "messages", "list"]
        ]
        self.assertTrue(any("after:2026/03/13" in query for query in list_queries))
        self.assertTrue(any('"Mandolin"' in query for query in list_queries))
        get_calls = [json.loads(call[-1]) for call in calls if call[:4] == ["gmail", "users", "messages", "get"]]
        self.assertEqual([params["id"] for params in get_calls], ["m1", "m2"])
        self.assertTrue(all(params["format"] == "metadata" for params in get_calls))
        self.assertTrue(all(params["metadataHeaders"] == ["Subject", "From", "Date"] for params in get_calls))

    def test_find_email_confirmation_prefers_company_match_over_provider_only_noise(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")

        def fake_run_gws_json(args):
            params = json.loads(args[-1])
            if args[:4] == ["gmail", "users", "messages", "list"]:
                query = params["q"]
                if '"Reflection"' in query and "application" in query:
                    return {"messages": [{"id": "reflection"}]}
                if "application" in query:
                    return {"messages": [{"id": "handshake"}]}
                return {"messages": []}
            if args[:4] == ["gmail", "users", "messages", "get"]:
                if params["id"] == "reflection":
                    return {
                        "id": "reflection",
                        "threadId": "thread-reflection",
                        "internalDate": "1773447512000",
                        "snippet": (
                            "Hi Candidate, Thanks so much for applying to roles here at Reflection. "
                            "We&#39;ve received your application,"
                        ),
                        "payload": {"mimeType": "multipart/alternative"},
                    }
                if params["id"] == "handshake":
                    return {
                        "id": "handshake",
                        "threadId": "thread-handshake",
                        "internalDate": "1773448634000",
                        "snippet": (
                            "Hi Candidate, Thank you for your interest in Handshake! "
                            "We have received your application for the Senior Product Manager, "
                            "Operator Experience - Handshake AI role."
                        ),
                        "payload": {"mimeType": "multipart/alternative"},
                    }
            raise AssertionError(f"Unexpected gws args: {args}")

        with mock.patch.object(notion_sync, "_run_gws_json", side_effect=fake_run_gws_json):
            match = notion_sync._find_email_confirmation(
                {
                    "company_proper": "Reflection",
                    "jd_title": "Don't See Your Role? Apply Here!",
                    "jd_source": "https://jobs.ashbyhq.com/reflectionai/bb02e922-8df8-4641-9aab-910997f76392",
                },
                min_received_at_utc="2026-03-14T00:18:31+00:00",
            )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["message_id"], "reflection")
        self.assertGreaterEqual(match["score"], 75)

    def test_find_email_confirmation_ignores_sent_self_reply(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")

        def fake_run_gws_json(args):
            params = json.loads(args[-1])
            if args[:4] == ["gmail", "users", "messages", "list"]:
                return {"messages": [{"id": "reply"}, {"id": "receipt"}]}
            if args[:4] == ["gmail", "users", "messages", "get"]:
                if params["id"] == "reply":
                    return {
                        "id": "reply",
                        "threadId": "thread-dexcare",
                        "labelIds": ["SENT"],
                        "internalDate": "1775152205000",
                        "snippet": "# Lever Autofill Report - Company: DexCare",
                        "payload": {
                            "headers": [
                                {
                                    "name": "Subject",
                                    "value": "Re: DexCare - Application Received - Senior Product Manager",
                                },
                                {"name": "From", "value": "candidate@example.com"},
                                {"name": "Date", "value": "Thu, 2 Apr 2026 13:30:05 -0700"},
                            ]
                        },
                    }
                if params["id"] == "receipt":
                    return {
                        "id": "receipt",
                        "threadId": "thread-dexcare",
                        "labelIds": ["INBOX"],
                        "internalDate": "1775151199000",
                        "snippet": "Thank you for your interest in DexCare! Your application has been received.",
                        "payload": {
                            "headers": [
                                {
                                    "name": "Subject",
                                    "value": "DexCare - Application Received - Senior Product Manager",
                                },
                                {"name": "From", "value": "DexCare <no-reply@hire.lever.co>"},
                                {"name": "Date", "value": "Thu, 2 Apr 2026 13:13:19 -0700"},
                            ]
                        },
                    }
            raise AssertionError(f"Unexpected gws args: {args}")

        with mock.patch.object(notion_sync, "_run_gws_json", side_effect=fake_run_gws_json):
            match = notion_sync._find_email_confirmation(
                {
                    "company_proper": "DexCare",
                    "jd_title": "Senior Product Manager",
                    "jd_source": "https://jobs.lever.co/dexcarehealth/a8ebecdd-d36e-470f-9edb-4243c52d8121",
                },
                min_received_at_utc="2026-04-02T15:31:55+00:00",
            )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["message_id"], "receipt")
        self.assertEqual(match["from"], "DexCare <no-reply@hire.lever.co>")

    def test_sync_application_returns_pending_email_status_when_allowed(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps(
                    {
                        "jd_title": "Senior Product Manager",
                        "company_proper": "Canals",
                        "jd_source": "https://jobs.ashbyhq.com/canals/6815edc9-2ebb-400a-b974-67a119a71f74?utm_source=VANyKzAEAm",
                    }
                ),
                encoding="utf-8",
            )
            notion_sync.record_website_confirmation(
                out_dir,
                {
                    "status": "confirmed",
                    "snapshot": {"url": "https://example.com/confirmation", "page_text": "Submitted"},
                },
            )
            with mock.patch.object(notion_sync, "_find_email_confirmation", return_value=None):
                result = notion_sync.sync_application(
                    out_dir,
                    wait_for_email_seconds=0,
                    allow_pending_email=True,
                    allow_website_only=False,
                    fail_on_missing_token=False,
                )
        self.assertEqual(result["status"], "pending_email_confirmation")

    def test_sync_application_defaults_min_received_at_from_website_confirmation(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps(
                    {
                        "jd_title": "Product Manager, AI",
                        "company_proper": "Figma",
                        "jd_source": "https://job-boards.greenhouse.io/figma/jobs/5574247004",
                    }
                ),
                encoding="utf-8",
            )
            notion_sync.record_website_confirmation(
                out_dir,
                {
                    "status": "confirmed",
                    "confirmed_at_utc": "2026-03-13T10:06:34+00:00",
                    "snapshot": {"url": "https://example.com/confirmation", "page_text": "Submitted"},
                },
            )
            recorded_confirmation = json.loads(
                (out_dir / "submit" / notion_sync.WEBSITE_CONFIRMATION_JSON).read_text(encoding="utf-8")
            )
            with mock.patch.object(notion_sync, "wait_for_email_confirmation", return_value=None) as wait_for_email:
                result = notion_sync.sync_application(
                    out_dir,
                    wait_for_email_seconds=0,
                    allow_pending_email=True,
                    allow_website_only=False,
                    fail_on_missing_token=False,
                )

        self.assertEqual(result["status"], "pending_email_confirmation")
        _, kwargs = wait_for_email.call_args
        self.assertEqual(kwargs["min_received_at_utc"], recorded_confirmation["confirmed_at_utc"])

    def test_find_email_confirmation_clears_stale_artifact_when_no_match(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps(
                    {
                        "jd_title": "Product Manager, AI",
                        "company_proper": "Figma",
                        "jd_source": "https://job-boards.greenhouse.io/figma/jobs/5574247004",
                    }
                ),
                encoding="utf-8",
            )
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            stale_path = submit_dir / notion_sync.EMAIL_CONFIRMATION_JSON
            stale_path.write_text(json.dumps({"subject": "Old confirmation"}), encoding="utf-8")
            with mock.patch.object(notion_sync, "_find_email_confirmation", return_value=None):
                result = notion_sync.find_email_confirmation(out_dir, write_artifact=True)

            self.assertIsNone(result)
            self.assertFalse(stale_path.exists())

    def test_sync_application_returns_missing_token_status_when_email_exists(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / ".pipeline_meta.json").write_text(
                json.dumps(
                    {
                        "jd_title": "Senior Technical Product Manager",
                        "company_proper": "BackOps AI",
                        "jd_source": "https://jobs.gem.com/backops-ai/am9icG9zdDqvIJK0_QtZ-fZgDog6VG8z?utm_source=jackandjill",
                    }
                ),
                encoding="utf-8",
            )
            notion_sync.record_website_confirmation(
                out_dir,
                {
                    "status": "confirmed",
                    "snapshot": {"url": "https://example.com/confirmation", "page_text": "Submitted"},
                },
            )
            with mock.patch.object(
                notion_sync, "_find_email_confirmation", return_value={"subject": "Thanks for applying"}
            ):
                with mock.patch.dict(os.environ, {"NOTION_API_TOKEN": "", "NOTION_TOKEN": ""}, clear=False):
                    result = notion_sync.sync_application(
                        out_dir,
                        wait_for_email_seconds=0,
                        allow_pending_email=True,
                        fail_on_missing_token=False,
                    )
            email_payload = json.loads(
                (out_dir / "submit" / notion_sync.EMAIL_CONFIRMATION_JSON).read_text(encoding="utf-8")
            )
        self.assertEqual(result["status"], "missing_notion_token")
        self.assertEqual(email_payload["subject"], "Thanks for applying")

    def test_run_gws_json_times_out_with_clear_error(self):
        notion_sync = load_module("notion_job_applications", "scripts/notion_job_applications.py")

        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["gws", "gmail"], timeout=10)):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                notion_sync._run_gws_json(["gmail", "messages", "list"])


if __name__ == "__main__":
    unittest.main()
