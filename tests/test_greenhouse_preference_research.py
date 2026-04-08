import importlib.util
import json
import sys
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
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class GreenhousePreferenceResearchTests(unittest.TestCase):
    def _question_specs(self) -> list[dict]:
        return [
            {
                "field_name": "question_preferred_roles",
                "label": "Which of these roles are you most interested in? Select up to 2.",
                "description": "",
                "required": True,
                "type": "multi_value_multi_select",
                "options": ["Product Manager", "Platform PM", "Growth PM"],
                "research_mode": "preference_ranking",
                "selection_limit": 2,
            }
        ]

    def test_prepare_preference_research_context_uses_research_mode_and_persists_validated_answers(self):
        research = load_module("greenhouse_preference_research", "scripts/greenhouse_preference_research.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            command_builder = mock.Mock(return_value=["openai", "responses", "create"])
            provider_output = json.dumps(
                {
                    "answers": {"question_preferred_roles": ["Platform PM", "Growth PM"]},
                    "evidence": {
                        "question_preferred_roles": {
                            "summary": "Platform and growth work align best with the current role scope.",
                            "supporting_evidence": [
                                "JD emphasizes platform leverage and product-led growth.",
                            ],
                        }
                    },
                }
            )

            with mock.patch.object(
                research.subprocess,
                "run",
                return_value=mock.Mock(returncode=0, stdout=provider_output, stderr=""),
            ):
                payload = research.prepare_preference_research_context(
                    out_dir,
                    meta={"company": "acme", "company_proper": "Acme", "jd_title": "Principal PM"},
                    question_specs=self._question_specs(),
                    provider="openai",
                    jd_parsed={"company": "Acme"},
                    resume_content={"summary": "Platform PM"},
                    research_cache={"company_context": "Acme builds product infrastructure."},
                    cover_letter_text="Cover letter context.",
                    master_resume_text="Resume context.",
                    work_stories_text="Work stories context.",
                    candidate_context_text="Candidate context.",
                    application_profile_text="Application profile context.",
                    command_builder=command_builder,
                    timeout_seconds=30,
                )

            self.assertEqual(
                payload["answers"]["question_preferred_roles"],
                ["Platform PM", "Growth PM"],
            )
            self.assertEqual(
                payload["questions"][0]["selected_options"],
                ["Platform PM", "Growth PM"],
            )
            self.assertEqual(payload["provider"], "openai")
            self.assertEqual(command_builder.call_args.kwargs["mode"], "research")
            self.assertTrue((out_dir / "submit" / "preference_research_context.json").exists())
            self.assertTrue((out_dir / "submit" / "preference_research_failures.json").exists())
            self.assertTrue((out_dir / "submit" / "preference_research_raw.txt").exists())

    def test_prepare_preference_research_context_blocks_invalid_live_option(self):
        research = load_module("greenhouse_preference_research_invalid", "scripts/greenhouse_preference_research.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            (out_dir / "submit").mkdir()
            provider_output = json.dumps(
                {
                    "answers": {"question_preferred_roles": ["Unknown Team"]},
                    "evidence": {
                        "question_preferred_roles": {
                            "summary": "This option sounded closest.",
                            "supporting_evidence": ["Weak evidence."],
                        }
                    },
                }
            )

            with mock.patch.object(
                research.subprocess,
                "run",
                return_value=mock.Mock(returncode=0, stdout=provider_output, stderr=""),
            ):
                payload = research.prepare_preference_research_context(
                    out_dir,
                    meta={"company": "acme", "company_proper": "Acme", "jd_title": "Principal PM"},
                    question_specs=self._question_specs(),
                    provider="openai",
                    jd_parsed={"company": "Acme"},
                    resume_content={"summary": "Platform PM"},
                    research_cache={"company_context": "Acme builds product infrastructure."},
                    cover_letter_text="Cover letter context.",
                    master_resume_text="Resume context.",
                    work_stories_text="Work stories context.",
                    candidate_context_text="Candidate context.",
                    application_profile_text="Application profile context.",
                    command_builder=mock.Mock(return_value=["openai", "responses", "create"]),
                    timeout_seconds=30,
                )

            self.assertEqual(payload["answers"], {})
            self.assertEqual(len(payload["failures"]), 1)
            self.assertTrue(payload["failures"][0]["required"])
            self.assertIn("current live options", payload["failures"][0]["failure_reason"])

    def test_prepare_preference_research_context_reuses_matching_previous_submit_artifacts(self):
        research = load_module("greenhouse_preference_research_cached", "scripts/greenhouse_preference_research.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            previous_submit = out_dir / "submit-20260328T010101Z"
            previous_submit.mkdir(parents=True)
            (out_dir / ".active_submit_dir").write_text(f"{previous_submit.name}\n", encoding="utf-8")
            provider_output = json.dumps(
                {
                    "answers": {"question_preferred_roles": ["Platform PM", "Growth PM"]},
                    "evidence": {
                        "question_preferred_roles": {
                            "summary": "Platform and growth work align best with the current role scope.",
                            "supporting_evidence": [
                                "JD emphasizes platform leverage and product-led growth.",
                            ],
                        }
                    },
                }
            )

            with mock.patch.object(
                research.subprocess,
                "run",
                return_value=mock.Mock(returncode=0, stdout=provider_output, stderr=""),
            ):
                first = research.prepare_preference_research_context(
                    out_dir,
                    meta={"company": "acme", "company_proper": "Acme", "jd_title": "Principal PM"},
                    question_specs=self._question_specs(),
                    provider="openai",
                    jd_parsed={"company": "Acme"},
                    resume_content={"summary": "Platform PM"},
                    research_cache={"company_context": "Acme builds product infrastructure."},
                    cover_letter_text="Cover letter context.",
                    master_resume_text="Resume context.",
                    work_stories_text="Work stories context.",
                    candidate_context_text="Candidate context.",
                    application_profile_text="Application profile context.",
                    command_builder=mock.Mock(return_value=["openai", "responses", "create"]),
                    timeout_seconds=30,
                    force_refresh=True,
                )

            (out_dir / ".active_submit_dir").write_text("submit\n", encoding="utf-8")

            second = research.prepare_preference_research_context(
                out_dir,
                meta={"company": "acme", "company_proper": "Acme", "jd_title": "Principal PM"},
                question_specs=self._question_specs(),
                provider="openai",
                jd_parsed={"company": "Acme"},
                resume_content={"summary": "Platform PM"},
                research_cache={"company_context": "Acme builds product infrastructure."},
                cover_letter_text="Cover letter context.",
                master_resume_text="Resume context.",
                work_stories_text="Work stories context.",
                candidate_context_text="Candidate context.",
                application_profile_text="Application profile context.",
                command_builder=mock.Mock(side_effect=AssertionError("should not rerun provider")),
                timeout_seconds=30,
            )

            self.assertEqual(first["cache_key"], second["cache_key"])
            self.assertTrue(second["used_cached_artifacts"])
            self.assertTrue((out_dir / "submit" / "preference_research_context.json").exists())
            self.assertTrue((out_dir / "submit" / "preference_research_raw.txt").exists())


if __name__ == "__main__":
    unittest.main()
