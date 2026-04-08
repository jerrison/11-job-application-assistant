import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import json
import tempfile
import unittest
from contextlib import closing


class DraftStatusTests(unittest.TestCase):
    def test_draft_is_valid_job_status(self):
        from job_db import JOB_STATUSES

        self.assertIn("draft", JOB_STATUSES)


class DraftSummaryTests(unittest.TestCase):
    def test_generate_draft_summary_md(self):
        from draft_manager import generate_draft_summary

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()

            # Minimal autofill report
            report = {
                "board": "ashby",
                "fields": [
                    {
                        "field_name": "application_name",
                        "label": "Full Name",
                        "kind": "text",
                        "required": True,
                        "status": "filled",
                        "value": "Jerrison Li",
                        "source": "master_resume.md",
                    },
                    {
                        "field_name": "survey_1_pronouns",
                        "label": "Pronouns",
                        "kind": "choice",
                        "required": False,
                        "status": "unfilled",
                        "value": None,
                        "source": None,
                    },
                ],
            }
            (submit_dir / "ashby_autofill_report.json").write_text(json.dumps(report))

            meta = {"company": "Bubble", "role_title": "Group PM", "board": "ashby"}
            generate_draft_summary(out_dir, submit_dir, meta)

            self.assertTrue((out_dir / "draft_summary.md").exists())
            self.assertTrue((out_dir / "draft_summary.original.md").exists())
            md = (out_dir / "draft_summary.md").read_text()
            self.assertIn("Bubble", md)
            self.assertIn("## Answer Refresh", md)
            self.assertIn("**Status:** not_applicable", md)
            self.assertIn("(application_name)", md)
            self.assertIn("**Status:** filled", md)
            self.assertIn("**Status:** unfilled", md)

    def test_generate_draft_summary_includes_fresh_answer_refresh_proof(self):
        from answer_refresh_state import finalize_answer_refresh, mark_answer_refresh_pending
        from draft_manager import generate_draft_summary

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()

            report = {
                "board": "ashby",
                "fields": [
                    {
                        "field_name": "app_name",
                        "label": "Full Name",
                        "kind": "text",
                        "required": True,
                        "status": "filled",
                        "value": "Jerrison Li",
                        "source": "generated_application_answer",
                    },
                ],
            }
            (submit_dir / "ashby_autofill_report.json").write_text(json.dumps(report))

            pending = mark_answer_refresh_pending(out_dir, request_kind="reanswer")
            finalize_answer_refresh(
                out_dir,
                request_id=pending["request_id"],
                status="fresh",
                message="Fresh answer generation proof recorded.",
                answer_provider="claude",
                answer_generated_at_utc="2026-03-26T18:30:00+00:00",
                generated_answer_count=1,
            )

            meta = {"company": "TestCo", "role_title": "Engineer", "board": "ashby"}
            generate_draft_summary(out_dir, submit_dir, meta)

            md = (out_dir / "draft_summary.md").read_text()
            self.assertIn("**Status:** fresh", md)
            self.assertIn("**Provider:** claude", md)
            self.assertIn("**Generated Answers:** 1", md)

    def test_generate_draft_summary_backfills_missing_answer_refresh_from_current_proof(self):
        from answer_refresh_state import load_answer_refresh_state
        from draft_manager import generate_draft_summary

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()

            report = {
                "board": "ashby",
                "fields": [
                    {
                        "field_name": "why_company",
                        "label": "Why this company?",
                        "kind": "textarea",
                        "required": True,
                        "status": "filled",
                        "value": "I want to build durable workflow automation here.",
                        "source": "generated_application_answer",
                    }
                ],
            }
            (submit_dir / "ashby_autofill_report.json").write_text(json.dumps(report), encoding="utf-8")
            (submit_dir / "application_answers.json").write_text(
                json.dumps(
                    {
                        "refresh_request_id": "refresh-123",
                        "provider": "openai",
                        "generated_at_utc": "2026-04-05T03:36:16+00:00",
                    }
                ),
                encoding="utf-8",
            )

            meta = {"company": "TestCo", "role_title": "Engineer", "board": "ashby"}
            generate_draft_summary(out_dir, submit_dir, meta)

            md = (out_dir / "draft_summary.md").read_text()
            refresh_state = load_answer_refresh_state(out_dir)
            self.assertIn("**Status:** fresh", md)
            self.assertIn("**Provider:** openai", md)
            self.assertIn("**Generated Answers:** 1", md)
            self.assertEqual(refresh_state["status"], "fresh")
            self.assertEqual(refresh_state["proof_submit_dir"], "submit")

    def test_generate_draft_summary_surfaces_needs_review_before_answers(self):
        from draft_manager import generate_draft_summary

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()

            report = {
                "board": "ashby",
                "fields": [
                    {
                        "field_name": "app_name",
                        "label": "Full Name",
                        "kind": "text",
                        "required": True,
                        "status": "filled",
                        "value": "Jerrison Li",
                        "source": "master_resume.md",
                    }
                ],
            }
            (submit_dir / "ashby_autofill_report.json").write_text(json.dumps(report))
            (submit_dir / "pending_user_input.json").write_text(
                json.dumps(
                    {
                        "status": "pending_user_input",
                        "questions": [
                            {
                                "field_name": "race_ethnicity",
                                "label": "Race or Ethnicity",
                                "kind": "choice",
                                "source": "application_profile.md",
                                "required": False,
                                "planned_value": "Hispanic or Latino",
                                "note": "Still blank in the screenshot.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            meta = {"company": "TestCo", "role_title": "Engineer", "board": "ashby"}
            generate_draft_summary(out_dir, submit_dir, meta)

            md = (out_dir / "draft_summary.md").read_text()
            self.assertIn("## Needs Review", md)
            self.assertIn("Race or Ethnicity", md)
            self.assertIn("Hispanic or Latino", md)
            self.assertLess(md.index("## Needs Review"), md.index("## Application Answers"))

    def test_generate_draft_summary_includes_answer_verification_blockers(self):
        from draft_manager import generate_draft_summary

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()

            report = {
                "board": "ashby",
                "fields": [
                    {
                        "field_name": "market_context",
                        "label": "Carrier market context",
                        "kind": "textarea",
                        "required": True,
                        "status": "filled",
                        "value": "I would evaluate carrier relationships.",
                        "source": "generated_application_answer",
                    }
                ],
            }
            (submit_dir / "ashby_autofill_report.json").write_text(json.dumps(report), encoding="utf-8")
            (out_dir / "answer_verification_status.json").write_text(
                json.dumps(
                    {
                        "status": "blocked",
                        "message": "Answer verification blocked one or more generated answers.",
                        "verifier_provider": "local_rule_based",
                        "verified_answer_count": 0,
                        "blocked_answer_count": 1,
                        "proof_submit_dir": "submit",
                    }
                ),
                encoding="utf-8",
            )
            (submit_dir / "answer_verification.json").write_text(
                json.dumps(
                    {
                        "generated_at_utc": "2026-04-01T01:02:03+00:00",
                        "request_id": "req-123",
                        "board": "ashby",
                        "submit_dir": "submit",
                        "verifier_provider": "local_rule_based",
                        "status": "blocked",
                        "summary": {
                            "question_count": 1,
                            "approved_count": 0,
                            "blocked_count": 1,
                            "not_applicable_count": 0,
                        },
                        "questions": [
                            {
                                "field_name": "market_context",
                                "label": "Carrier market context",
                                "required": True,
                                "verification_lane": "user_required",
                                "verdict": "blocked_requires_user_input",
                                "answer_text": "I would evaluate carrier relationships.",
                                "reason": "This question requires explicit user input instead of an inferred generated answer.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            meta = {"company": "TestCo", "role_title": "Engineer", "board": "ashby"}
            generate_draft_summary(out_dir, submit_dir, meta)

            md = (out_dir / "draft_summary.md").read_text(encoding="utf-8")
            self.assertIn("## Answer Verification", md)
            self.assertIn("**Status:** blocked", md)
            self.assertIn("blocked_requires_user_input", md)
            self.assertIn("local_rule_based", md)
            self.assertIn("Carrier market context", md)
            self.assertLess(md.index("## Answer Verification"), md.index("## Application Answers"))

    def test_generate_draft_summary_includes_retryable_answer_verification_feedback(self):
        from draft_manager import generate_draft_summary

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()

            report = {
                "board": "ashby",
                "fields": [
                    {
                        "field_name": "why_company",
                        "label": "Why this company?",
                        "kind": "textarea",
                        "required": True,
                        "status": "filled",
                        "value": "I want to own your platform roadmap from day one.",
                        "source": "generated_application_answer",
                    }
                ],
            }
            (submit_dir / "ashby_autofill_report.json").write_text(json.dumps(report), encoding="utf-8")
            (out_dir / "answer_verification_status.json").write_text(
                json.dumps(
                    {
                        "status": "blocked",
                        "message": "Answer verification requested generator retry for one or more generated answers.",
                        "verifier_provider": "openai",
                        "verified_answer_count": 0,
                        "blocked_answer_count": 1,
                        "proof_submit_dir": "submit",
                    }
                ),
                encoding="utf-8",
            )
            (submit_dir / "answer_verification.json").write_text(
                json.dumps(
                    {
                        "generated_at_utc": "2026-04-01T01:02:03+00:00",
                        "request_id": "req-456",
                        "board": "ashby",
                        "submit_dir": "submit",
                        "answer_provider": "openai",
                        "verifier_provider": "openai",
                        "status": "blocked",
                        "summary": {
                            "question_count": 1,
                            "approved_count": 0,
                            "retry_count": 1,
                            "blocked_count": 0,
                            "not_applicable_count": 0,
                        },
                        "questions": [
                            {
                                "field_name": "why_company",
                                "label": "Why this company?",
                                "required": True,
                                "verification_lane": "reference_verified_generated_text",
                                "verdict": "retry_with_feedback",
                                "answer_text": "I want to own your platform roadmap from day one.",
                                "reason": "The answer claims direct platform ownership not supported by repo sources.",
                                "feedback_for_regeneration": [
                                    "Remove unsupported claim about owning the platform roadmap.",
                                    "Ground the answer in workflow automation and AI product experience.",
                                ],
                                "source_refs": ["master_resume.md", "content/jd_parsed.json"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            meta = {"company": "TestCo", "role_title": "Engineer", "board": "ashby"}
            generate_draft_summary(out_dir, submit_dir, meta)

            md = (out_dir / "draft_summary.md").read_text(encoding="utf-8")
            self.assertIn("retry_with_feedback", md)
            self.assertIn("Remove unsupported claim about owning the platform roadmap.", md)
            self.assertIn("master_resume.md", md)
            self.assertIn("content/jd_parsed.json", md)

    def test_generate_draft_summary_surfaces_artifact_blocker_reason_and_expected_path(self):
        from draft_manager import generate_draft_summary

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()

            report = {
                "board": "ashby",
                "fields": [
                    {
                        "field_name": "app_name",
                        "label": "Full Name",
                        "kind": "text",
                        "required": True,
                        "status": "filled",
                        "value": "Jerrison Li",
                        "source": "master_resume.md",
                    }
                ],
            }
            (submit_dir / "ashby_autofill_report.json").write_text(json.dumps(report))
            (submit_dir / "pending_user_input.json").write_text(
                json.dumps(
                    {
                        "status": "pending_user_input",
                        "questions": [
                            {
                                "field_name": "pre_submit_screenshot",
                                "label": "Current-attempt pre-submit screenshot",
                                "kind": "artifact",
                                "source": "draft_proof_contract",
                                "required": True,
                                "status": "missing",
                                "artifact_key": "pre_submit_screenshot",
                                "planned_value": str(submit_dir / "ashby_autofill_pre_submit.png"),
                                "reason": "The current submit attempt is missing the required pre-submit screenshot proof.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            meta = {"company": "TestCo", "role_title": "Engineer", "board": "ashby"}
            generate_draft_summary(out_dir, submit_dir, meta)

            md = (out_dir / "draft_summary.md").read_text()
            self.assertIn("Artifact Key", md)
            self.assertIn("pre_submit_screenshot", md)
            self.assertIn("Expected Path", md)
            self.assertIn("missing the required pre-submit screenshot proof", md)

    def test_generate_draft_summary_includes_linked_resource_provenance(self):
        from draft_manager import generate_draft_summary

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()

            report = {
                "board": "ashby",
                "fields": [
                    {
                        "field_name": "sql_task",
                        "label": "SQL Task",
                        "kind": "textarea",
                        "required": True,
                        "status": "filled",
                        "value": "Top card is abc123.",
                        "source": "generated_application_answer",
                    }
                ],
            }
            (submit_dir / "ashby_autofill_report.json").write_text(json.dumps(report))
            (submit_dir / "application_answers.json").write_text(
                json.dumps(
                    {
                        "questions": [],
                        "answers": {"sql_task": "Top card is abc123."},
                        "linked_resources": {
                            "resources": [
                                {
                                    "field_name": "sql_task",
                                    "adapter": "db_fiddle",
                                    "url": "https://www.db-fiddle.com/f/example/1",
                                    "payload_json": str(submit_dir / "linked_resource_evidence" / "sql_task.json"),
                                }
                            ],
                            "failures": [],
                        },
                    }
                ),
                encoding="utf-8",
            )

            meta = {"company": "TestCo", "role_title": "Engineer", "board": "ashby"}
            generate_draft_summary(out_dir, submit_dir, meta)

            md = (out_dir / "draft_summary.md").read_text()
            self.assertIn("**Linked Resource:** db_fiddle via https://www.db-fiddle.com/f/example/1", md)
            self.assertIn("submit/sql_task.json", md)

    def test_generate_draft_summary_resolves_report_from_active_submit_dir_when_meta_board_is_wrong(self):
        from draft_manager import generate_draft_summary

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            submit_dir = out_dir / "submit-20260326T010203Z"
            submit_dir.mkdir()
            (out_dir / ".active_submit_dir").write_text("submit-20260326T010203Z\n", encoding="utf-8")

            report = {
                "board": "ashby",
                "fields": [
                    {
                        "field_name": "app_name",
                        "label": "Full Name",
                        "kind": "text",
                        "required": True,
                        "status": "filled",
                        "value": "Jerrison Li",
                        "source": "master_resume.md",
                    }
                ],
            }
            (submit_dir / "ashby_autofill_report.json").write_text(json.dumps(report))

            meta = {"company": "TestCo", "role_title": "Engineer", "board": "greenhouse"}
            generate_draft_summary(out_dir, submit_dir, meta)

            md = (out_dir / "draft_summary.md").read_text()
            self.assertIn("Full Name", md)
            self.assertIn("Jerrison Li", md)

    def test_generate_draft_summary_creates_png(self):
        """generate_draft_summary() should also produce a PNG via subprocess."""
        from draft_manager import generate_draft_summary

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()

            report = {
                "board": "ashby",
                "fields": [
                    {
                        "field_name": "app_name",
                        "label": "Full Name",
                        "kind": "text",
                        "required": True,
                        "status": "filled",
                        "value": "Jerrison Li",
                        "source": "master_resume.md",
                    },
                ],
            }
            (submit_dir / "ashby_autofill_report.json").write_text(json.dumps(report))

            meta = {"company": "TestCo", "role_title": "Engineer", "board": "ashby"}
            result = generate_draft_summary(out_dir, submit_dir, meta)

            self.assertIn("png", result)
            self.assertTrue(Path(result["png"]).exists())
            self.assertGreater(Path(result["png"]).stat().st_size, 0)

    def test_generate_draft_summary_records_stale_review_state(self):
        from draft_manager import generate_draft_summary

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            stale_submit = out_dir / "submit"
            stale_submit.mkdir()
            (stale_submit / "greenhouse_autofill_pre_submit.png").write_text("png", encoding="utf-8")

            submit_dir = out_dir / "submit-20260326T010203Z"
            submit_dir.mkdir()
            (out_dir / ".active_submit_dir").write_text("submit-20260326T010203Z\n", encoding="utf-8")
            (submit_dir / "greenhouse_autofill_report.json").write_text(json.dumps({"fields": []}), encoding="utf-8")

            meta = {"company": "TestCo", "role_title": "Engineer", "board": "greenhouse"}
            generate_draft_summary(out_dir, submit_dir, meta)

            summary_md = (out_dir / "draft_summary.md").read_text(encoding="utf-8")
            status_payload = json.loads((out_dir / "draft_status.json").read_text(encoding="utf-8"))

        self.assertIn("## Draft Review State", summary_md)
        self.assertIn("stale", summary_md.casefold())
        self.assertEqual(status_payload["draft_review_state"]["state"], "stale")


class DraftDiffTests(unittest.TestCase):
    def test_classify_answer_change(self):
        from draft_manager import classify_draft_edits

        original = "### 1. Salary (app_salary)\n- **Answer:** Yes\n- **Status:** filled"
        edited = "### 1. Salary (app_salary)\n- **Answer:** Open to negotiation\n- **Status:** filled"

        changes = classify_draft_edits(original, edited)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["field_key"], "app_salary")
        self.assertEqual(changes[0]["old_answer"], "Yes")
        self.assertEqual(changes[0]["new_answer"], "Open to negotiation")

    def test_classify_unfilled_to_filled(self):
        from draft_manager import classify_draft_edits

        original = "### 1. Pronouns (survey_pronouns)\n- **Answer:** —\n- **Status:** unfilled"
        edited = "### 1. Pronouns (survey_pronouns)\n- **Answer:** He/Him\n- **Status:** unfilled"

        changes = classify_draft_edits(original, edited)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["classification"], "missing_handler")

    def test_no_changes_returns_empty(self):
        from draft_manager import classify_draft_edits

        text = "### 1. Name (app_name)\n- **Answer:** Jerrison\n- **Status:** filled"
        changes = classify_draft_edits(text, text)
        self.assertEqual(changes, [])


class DraftOverrideTests(unittest.TestCase):
    def test_apply_edits_writes_overrides(self):
        from draft_manager import apply_draft_edits

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            changes = [
                {
                    "field_key": "app_salary",
                    "old_answer": "Yes",
                    "new_answer": "Open",
                    "old_status": "filled",
                    "classification": "wrong_answer",
                },
            ]
            apply_draft_edits(out_dir, changes)

            overrides = json.loads((out_dir / "draft_overrides.json").read_text())
            self.assertEqual(overrides["app_salary"], "Open")

    def test_apply_edits_generates_fix_report(self):
        from draft_manager import apply_draft_edits

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            changes = [
                {
                    "field_key": "survey_pronouns",
                    "old_answer": "\u2014",
                    "new_answer": "He/Him",
                    "old_status": "unfilled",
                    "classification": "missing_handler",
                },
            ]
            apply_draft_edits(out_dir, changes)

            report_path = out_dir / "draft_fix_report.md"
            self.assertTrue(report_path.exists())
            report = report_path.read_text()
            self.assertIn("survey_pronouns", report)
            self.assertIn("missing_handler", report)

    def test_apply_edits_preserves_existing_overrides(self):
        from draft_manager import apply_draft_edits

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            (out_dir / "draft_overrides.json").write_text(json.dumps({"existing_key": "keep"}))

            changes = [
                {
                    "field_key": "new_key",
                    "old_answer": "X",
                    "new_answer": "Y",
                    "old_status": "filled",
                    "classification": "wrong_answer",
                },
            ]
            apply_draft_edits(out_dir, changes)

            overrides = json.loads((out_dir / "draft_overrides.json").read_text())
            self.assertEqual(overrides["existing_key"], "keep")
            self.assertEqual(overrides["new_key"], "Y")


import sqlite3


class DraftCASTransitionTests(unittest.TestCase):
    def _create_test_db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE jobs (
            id INTEGER PRIMARY KEY, url TEXT NOT NULL DEFAULT '',
            source TEXT, source_url TEXT, board_url TEXT,
            canonical_url TEXT UNIQUE, company TEXT, role_title TEXT,
            board TEXT, status TEXT NOT NULL DEFAULT 'queued',
            priority INTEGER DEFAULT 0, provider TEXT, output_dir TEXT,
            notion_url TEXT, error_message TEXT, failure_type TEXT,
            auth_state TEXT,
            progress TEXT, fix_attempts INTEGER DEFAULT 0,
            retry_after TIMESTAMP DEFAULT '1970-01-01 00:00:00',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP)""")
        conn.execute("""CREATE TABLE events (
            id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
            event_type TEXT NOT NULL, detail TEXT, detail_json TEXT,
            initiator TEXT, process_info TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        return conn

    def test_approve_job_cas_rejects_processing(self):
        """Compare-and-swap: approve rejects jobs already processing."""
        from pipeline_orchestrator import approve_job

        with closing(self._create_test_db()) as conn:
            conn.execute("INSERT INTO jobs (id, status) VALUES (1, 'generating')")
            conn.commit()
            self.assertFalse(approve_job(conn, 1))

    def test_approve_job_cas_accepts_draft(self):
        """Compare-and-swap: approve works on draft status."""
        from pipeline_orchestrator import approve_job

        with closing(self._create_test_db()) as conn:
            conn.execute("INSERT INTO jobs (id, status) VALUES (1, 'draft')")
            conn.commit()
            self.assertTrue(approve_job(conn, 1))

    def test_approve_job_rejects_incomplete_draft_with_current_pending_user_input(self):
        from pipeline_orchestrator import approve_job

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            (submit_dir / "pending_user_input.json").write_text(
                json.dumps({"status": "pending_user_input", "questions": [{"label": "Pronouns"}]}),
                encoding="utf-8",
            )

            with closing(self._create_test_db()) as conn:
                conn.execute("INSERT INTO jobs (id, status, output_dir) VALUES (1, 'draft', ?)", (str(out_dir),))
                conn.commit()

                self.assertFalse(approve_job(conn, 1))

    def test_concurrent_approve_regenerate_one_wins(self):
        """Only one of approve/regenerate succeeds on the same draft."""
        from pipeline_orchestrator import approve_job, regenerate_job

        with closing(self._create_test_db()) as conn:
            conn.execute("INSERT INTO jobs (id, status) VALUES (1, 'draft')")
            conn.commit()
            # First call wins
            self.assertTrue(approve_job(conn, 1))
            # Second call fails — status is no longer 'draft'
            self.assertFalse(regenerate_job(conn, 1))

    def test_regenerate_job_marks_answer_refresh_pending(self):
        from answer_refresh_state import load_answer_refresh_state
        from pipeline_orchestrator import regenerate_job

        with tempfile.TemporaryDirectory() as tmp:
            with closing(self._create_test_db()) as conn:
                conn.execute("INSERT INTO jobs (id, status, output_dir) VALUES (1, 'draft', ?)", (tmp,))
                conn.commit()

                self.assertTrue(regenerate_job(conn, 1))

                state = load_answer_refresh_state(Path(tmp))
                self.assertEqual(state["status"], "pending")
                self.assertEqual(state["request_kind"], "full_regenerate")

    def test_regenerate_job_clears_stale_current_attempt_proof(self):
        from pipeline_orchestrator import regenerate_job

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            submit_dir = out_dir / "submit"
            submit_dir.mkdir()
            payload_path = submit_dir / "workable_autofill_payload.json"
            report_path = submit_dir / "workable_autofill_report.json"
            screenshot_path = submit_dir / "workable_autofill_pre_submit.png"
            unknown_questions_path = submit_dir / "workable_unknown_questions.json"
            application_page_path = submit_dir / "workable_application_page.html"
            pending_input_path = submit_dir / "pending_user_input.json"

            report_path.write_text("{}", encoding="utf-8")
            screenshot_path.write_text("png", encoding="utf-8")
            unknown_questions_path.write_text("{}", encoding="utf-8")
            application_page_path.write_text("<html></html>", encoding="utf-8")
            pending_input_path.write_text("{}", encoding="utf-8")
            payload_path.write_text(
                json.dumps(
                    {
                        "artifacts": {
                            "payload_path": str(payload_path),
                            "report_json": str(report_path),
                            "pre_submit_screenshot": str(screenshot_path),
                            "unknown_questions_json": str(unknown_questions_path),
                            "application_page_html": str(application_page_path),
                        }
                    }
                ),
                encoding="utf-8",
            )
            (out_dir / "draft_status.json").write_text(
                json.dumps({"status": "awaiting_review", "draft_review_state": {"state": "ready"}}),
                encoding="utf-8",
            )
            (out_dir / "draft_summary.md").write_text("# summary", encoding="utf-8")
            (out_dir / "draft_summary.original.md").write_text("# summary", encoding="utf-8")
            (out_dir / "draft_summary.png").write_text("png", encoding="utf-8")

            with closing(self._create_test_db()) as conn:
                conn.execute(
                    "INSERT INTO jobs (id, status, output_dir, board) VALUES (1, 'draft', ?, 'workable')",
                    (str(out_dir),),
                )
                conn.commit()

                self.assertTrue(regenerate_job(conn, 1))

            self.assertFalse(report_path.exists())
            self.assertFalse(screenshot_path.exists())
            self.assertFalse(pending_input_path.exists())
            self.assertFalse((out_dir / "draft_status.json").exists())
            self.assertFalse((out_dir / "draft_summary.md").exists())
            self.assertFalse((out_dir / "draft_summary.original.md").exists())
            self.assertFalse((out_dir / "draft_summary.png").exists())
            self.assertTrue(payload_path.exists())
            self.assertTrue(unknown_questions_path.exists())
            self.assertTrue(application_page_path.exists())


class DraftSummaryPngTests(unittest.TestCase):
    def test_build_summary_png_creates_file(self):
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            md_path = Path(tmp) / "draft_summary.md"
            md_path.write_text(
                "# Draft: Test Role — TestCo\n"
                "**Board:** ashby | **Generated:** 2026-03-15 00:00 UTC\n\n"
                "## Application Answers\n\n"
                "### 1. Full Name (app_name)\n"
                "- **Kind:** text | **Required:** yes | **Source:** master_resume.md\n"
                "- **Answer:** Jerrison Li\n"
                "- **Status:** filled\n\n"
                "### 2. Pronouns (survey_pronouns)\n"
                "- **Kind:** choice | **Required:** no | **Source:** —\n"
                "- **Answer:** —\n"
                "- **Status:** unfilled\n"
            )
            out_path = Path(tmp) / "draft_summary.png"

            result = subprocess.run(
                ["uv", "run", "python", "scripts/build_draft_summary.py", str(md_path), "-o", str(out_path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out_path.exists())
            self.assertGreater(out_path.stat().st_size, 0)

    def test_build_summary_png_dimensions(self):
        """Verify the PNG is ~800px wide and has reasonable height."""
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            md_path = Path(tmp) / "draft_summary.md"
            md_path.write_text(
                "# Draft: Engineer — Acme\n"
                "**Board:** greenhouse | **Generated:** 2026-03-15 00:00 UTC\n\n"
                "## Application Answers\n\n"
                "### 1. Name (app_name)\n"
                "- **Kind:** text | **Required:** yes | **Source:** master_resume.md\n"
                "- **Answer:** Jerrison\n"
                "- **Status:** filled\n"
            )
            out_path = Path(tmp) / "draft_summary.png"
            subprocess.run(
                ["uv", "run", "python", "scripts/build_draft_summary.py", str(md_path), "-o", str(out_path)],
                capture_output=True,
                text=True,
            )
            from PIL import Image

            with Image.open(out_path) as img:
                self.assertEqual(img.width, 800)
                self.assertGreater(img.height, 100)

    def test_build_summary_png_default_output(self):
        """When -o is not specified, output is draft_summary.png in same dir."""
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            md_path = Path(tmp) / "draft_summary.md"
            md_path.write_text(
                "# Draft: Test — Co\n"
                "**Board:** lever | **Generated:** 2026-03-15 00:00 UTC\n\n"
                "## Application Answers\n\n"
                "### 1. Name (app_name)\n"
                "- **Kind:** text | **Required:** yes | **Source:** —\n"
                "- **Answer:** Test\n"
                "- **Status:** filled\n"
            )
            result = subprocess.run(
                ["uv", "run", "python", "scripts/build_draft_summary.py", str(md_path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = Path(tmp) / "draft_summary.png"
            self.assertTrue(expected.exists())

    def test_build_summary_png_with_answer_refresh_section(self):
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            md_path = Path(tmp) / "draft_summary.md"
            md_path.write_text(
                "# Draft: Test Role — TestCo\n"
                "**Board:** ashby | **Generated:** 2026-03-26 18:30 UTC\n\n"
                "## Answer Refresh\n\n"
                "- **Status:** fresh\n"
                "- **Request:** Answers only\n"
                "- **Message:** Fresh answer generation proof recorded.\n"
                "- **Provider:** claude\n"
                "- **Answer Generated:** 2026-03-26 18:30 UTC\n"
                "- **Generated Answers:** 2\n\n"
                "## Application Answers\n\n"
                "### 1. Full Name (app_name)\n"
                "- **Kind:** text | **Required:** yes | **Source:** generated_application_answer\n"
                "- **Answer:** Jerrison Li\n"
                "- **Status:** filled\n"
            )
            out_path = Path(tmp) / "draft_summary.png"

            result = subprocess.run(
                ["uv", "run", "python", "scripts/build_draft_summary.py", str(md_path), "-o", str(out_path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out_path.exists())
