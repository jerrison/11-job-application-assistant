import importlib.util
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CompanyDetectionTests(unittest.TestCase):
    def test_parse_jd_extracts_company_from_about_header(self):
        parse_jd = load_module("parse_jd", "scripts/parse_jd.py")
        sample = """# Product Lead, Automation Platform

About Rippling
Rippling gives businesses one place to run HR, IT, and Finance.
"""
        self.assertEqual(parse_jd._extract_company(sample), "Rippling")

    def test_parse_jd_rejects_about_the_role_wrapper_as_company(self):
        parse_jd = load_module("parse_jd", "scripts/parse_jd.py")
        sample = """# Principal Product Manager

About The Role
You will lead platform strategy across internal tools.
"""
        self.assertEqual(parse_jd._extract_company(sample), "")

    def test_parse_jd_treats_job_board_suffix_as_generic_company(self):
        parse_jd = load_module("parse_jd", "scripts/parse_jd.py")
        self.assertTrue(parse_jd.company_name_looks_generic("Rubrik Job Board"))

    def test_parse_jd_extracts_company_from_lowercase_at_company_colon(self):
        parse_jd = load_module("parse_jd", "scripts/parse_jd.py")
        sample = """# Principal Product Manager, LLM Innovation

at Headspace:
We are seeking a Principal Product Manager, LLM Innovation to lead how Large Language Models are applied across Headspace.
"""
        self.assertEqual(parse_jd._extract_company(sample), "Headspace")

    def test_parse_jd_strips_job_board_suffix_from_company_line(self):
        parse_jd = load_module("parse_jd", "scripts/parse_jd.py")
        raw = """{
  "title": "Staff Platform Product Manager, Platform & Cloud Security",
  "company_name": "Rubrik Job Board",
  "location": {"name": "Palo Alto, CA"},
  "content": "&lt;p&gt;Rubrik&#39;s cloud product offerings meet enterprise expectations for security.&lt;/p&gt;"
}"""
        normalized = parse_jd._try_parse_greenhouse_json(raw)
        assert normalized is not None
        self.assertEqual(parse_jd._extract_company(normalized), "Rubrik")

    def test_run_pipeline_ignores_generic_ats_subdomain(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        url = "https://ats.rippling.com/rippling/jobs/e7b11f22-cf58-4266-8c57-fabb86d145ac"
        self.assertEqual(run_pipeline._company_slug_from_url(url), "rippling")

    def test_run_pipeline_can_infer_company_from_jd_text(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        sample = """About Rippling
Rippling gives businesses one place to run HR, IT, and Finance.
Rippling.com
"""
        self.assertEqual(run_pipeline._company_slug_from_text(sample), "rippling")

    def test_run_pipeline_extracts_company_name_from_linkedin_title(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        title = "Asurion hiring Principal Product Manager in San Francisco Bay Area | LinkedIn"
        self.assertEqual(run_pipeline._company_name_from_text(title), "Asurion")

    def test_run_pipeline_extracts_company_name_from_pipe_delimited_linkedin_title(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        title = "Sr. GenAI Product Manager, AI.x - Risk Management & Compliance | Charles Schwab | LinkedIn"
        self.assertEqual(run_pipeline._company_name_from_text(title), "Charles Schwab")

    def test_run_pipeline_extracts_company_name_from_leading_sentence(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        sample = "Vectara is the Enterprise Agent Platform for building reliable, accurate GenAI products."
        self.assertEqual(run_pipeline._company_name_from_text(sample), "Vectara")

    def test_run_pipeline_extracts_company_name_from_helping_sentence_before_wrapper_copy(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        sample = """# Founding Product Manager

Reducto helps AI teams ingest real-world enterprise data with state-of-the-art accuracy.
This is a founding PM role, not a feature-team PM role.
"""
        self.assertEqual(run_pipeline._company_name_from_text(sample), "Reducto")

    def test_run_pipeline_extracts_company_name_from_leading_at_company_sentence(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        sample = """# Staff Product Manager, AI

At Navan, “It’s all about the user. All of them.” We’re passionate about creating seamless, personalized travel experiences for business travelers around the globe.
"""
        self.assertEqual(run_pipeline._company_name_from_text(sample), "Navan")

    def test_run_pipeline_extracts_company_name_from_lowercase_at_company_colon(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        sample = """# Principal Product Manager, LLM Innovation

at Headspace:
We are seeking a Principal Product Manager, LLM Innovation to lead how Large Language Models are applied across Headspace.
"""
        self.assertEqual(run_pipeline._company_name_from_text(sample), "Headspace")

    def test_run_pipeline_normalizes_greenhouse_json_before_company_fallback(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        raw = """{
  "title": "Staff Platform Product Manager, Platform & Cloud Security",
  "company_name": "Rubrik Job Board",
  "location": {"name": "Palo Alto, CA"},
  "content": "&lt;p&gt;The Staff Product Manager role ensures that Rubrik&#39;s cloud product offerings meet enterprise expectations for security.&lt;/p&gt;"
}"""
        self.assertEqual(run_pipeline._company_name_from_text(raw), "Rubrik")

    def test_run_pipeline_rejects_generic_wrapper_heading_as_company_name(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        self.assertIsNone(run_pipeline._company_name_from_text("About The Role"))

    def test_run_pipeline_ignores_remote_location_suffix_when_extracting_company_name(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        sample = """# Staff AI Product Manager
**Company:** the Role
**Location:** United States • Los Angeles, CA • San Francisco, CA • United States | Remote

---

Staff AI Product Manager
United States • Los Angeles, CA • San Francisco, CA • United States | Remote
Product Management
Remote • Hybrid
Full-time
    About the Role
    We’re looking for a forward-thinking Staff AI Product Manager to help us build the future of Linktree—one where AI doesn’t just support the experience, it is the experience.
    Linktree has always been about helping people express who they are and drive value from their audience.
    You’ll work closely with design, engineering, data science, and marketing to reimagine how creators, businesses, and brands get value from their Linktree—from day one and every day after.
    AI-First Onboarding: Lead the development of a “Generated Linktree” experience that uses public signals to automatically generate an optimized Linktree tailored to a user’s goals.
    Linktree is committed to providing a competitive compensation package.
"""
        self.assertEqual(run_pipeline._company_name_from_text(sample), "Linktree")

    def test_run_pipeline_ignores_workday_loaded_page_wrapper_when_extracting_company_name(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        sample = """# Careers

Principal Product Manager page is loaded
Principal Product Manager

Position Overview
At Autodesk, you will define the long-term product vision for opportunity lifecycle tooling.
"""
        self.assertEqual(run_pipeline._company_name_from_text(sample), "Autodesk")

    def test_run_pipeline_ignores_workday_careers_wrapper_when_intro_uses_possessive_company(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")
        sample = """# Careers

Principal Product Manager
Skip to main content
Careers
English
Search for Jobs
Principal Product Manager page is loaded
Principal Product Manager

Position Overview
Autodesk's GTM Tech organization is hiring a Principal Product Manager to own the strategy.
"""
        self.assertEqual(run_pipeline._company_name_from_text(sample), "Autodesk")

    def test_run_pipeline_resolves_company_proper_without_text_company_name(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")

        resolved = run_pipeline._resolve_company_proper(
            jd_company="",
            company_slug="ramp",
            title_company_name=None,
            text_company_name=None,
            resolved_host="jobs.ashbyhq.com",
            is_url=True,
        )

        self.assertEqual(resolved, "Ramp")

    def test_run_pipeline_prefers_text_company_name_when_jd_company_is_missing(self):
        run_pipeline = load_module("run_pipeline", "scripts/run_pipeline.py")

        resolved = run_pipeline._resolve_company_proper(
            jd_company="",
            company_slug="ramp",
            title_company_name=None,
            text_company_name="Ramp",
            resolved_host="jobs.ashbyhq.com",
            is_url=True,
        )

        self.assertEqual(resolved, "Ramp")

    def test_scrape_job_does_not_treat_linkedin_host_as_company(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        url = "https://www.linkedin.com/jobs/view/4385754804/"
        self.assertEqual(scrape_job._company_from_url(url), "")

    def test_scrape_job_extracts_company_from_linkedin_title(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        title = "Asurion hiring Principal Product Manager in San Francisco Bay Area | LinkedIn"
        self.assertEqual(scrape_job._company_from_linkedin_title(title), "Asurion")

    def test_scrape_job_extracts_company_from_pipe_delimited_linkedin_title(self):
        scrape_job = load_module("scrape_job", "scripts/scrape_job.py")
        title = "Principal Product Builder, Agent Analytics & Optimization | Conviva | LinkedIn"
        self.assertEqual(scrape_job._company_from_linkedin_title(title), "Conviva")


if __name__ == "__main__":
    unittest.main()
